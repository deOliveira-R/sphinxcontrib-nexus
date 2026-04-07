"""Ingest external documents (papers, PDFs, text) into the knowledge graph.

Uses an LLM to extract concepts, equations, and relationships from
unstructured text, then adds them as nodes and edges to the graph.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sphinxcontrib.nexus.graph import (
    EdgeType,
    GraphEdge,
    GraphNode,
    KnowledgeGraph,
    NodeType,
)

logger = logging.getLogger(__name__)

_EXTRACTION_PROMPT = """\
You are a knowledge graph extractor. Given the following document text,
extract structured information as JSON.

Extract:
1. **concepts**: Named concepts, methods, algorithms mentioned
   - Each: {"name": "...", "type": "concept|equation|method|algorithm|term", "description": "..."}
2. **relationships**: How concepts relate to each other and to code
   - Each: {"source": "...", "target": "...", "type": "references|implements|derives|cites|related_to", "description": "..."}
3. **equations**: Named equations with their labels
   - Each: {"name": "...", "label": "...", "description": "..."}
4. **citations**: Literature references
   - Each: {"key": "Author2009", "full_ref": "Author, Title, Journal, Year"}

Return ONLY valid JSON with these four keys: concepts, relationships, equations, citations.

Document text:
---
{text}
---

JSON output:"""


@dataclass
class IngestResult:
    """Result of ingesting a document."""

    source_file: str
    concepts_added: int
    equations_added: int
    relationships_added: int
    citations_added: int


def ingest_file(
    file_path: Path,
    graph: KnowledgeGraph,
    llm_command: str | None = None,
) -> IngestResult:
    """Ingest a document file into the knowledge graph.

    Args:
        file_path: Path to the document (PDF, txt, md, rst).
        graph: Knowledge graph to add nodes/edges to.
        llm_command: Shell command that accepts prompt on stdin and returns
                     LLM response on stdout. If None, uses Claude CLI.
    """
    text = _extract_text(file_path)
    if not text:
        logger.warning("Could not extract text from %s", file_path)
        return IngestResult(str(file_path), 0, 0, 0, 0)

    # Truncate very long documents
    max_chars = 50000
    if len(text) > max_chars:
        text = text[:max_chars] + "\n\n[... truncated ...]"

    prompt = _EXTRACTION_PROMPT.format(text=text)
    response = _call_llm(prompt, llm_command)

    if not response:
        logger.warning("LLM returned empty response for %s", file_path)
        return IngestResult(str(file_path), 0, 0, 0, 0)

    return _add_to_graph(graph, response, file_path)


def _extract_text(file_path: Path) -> str:
    """Extract text from a file."""
    suffix = file_path.suffix.lower()

    if suffix == ".pdf":
        return _extract_pdf_text(file_path)
    elif suffix in (".txt", ".md", ".rst", ".tex"):
        return file_path.read_text(encoding="utf-8", errors="replace")
    else:
        return file_path.read_text(encoding="utf-8", errors="replace")


def _extract_pdf_text(file_path: Path) -> str:
    """Extract text from a PDF using pdftotext or Python fallback."""
    # Try pdftotext (poppler)
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", str(file_path), "-"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout
    except (FileNotFoundError, subprocess.SubprocessError):
        pass

    # Fallback: try PyPDF2 or pymupdf
    try:
        import fitz  # pymupdf
        doc = fitz.open(str(file_path))
        text = "\n".join(page.get_text() for page in doc)
        doc.close()
        return text
    except ImportError:
        pass

    try:
        from PyPDF2 import PdfReader
        reader = PdfReader(str(file_path))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except ImportError:
        pass

    logger.warning(
        "Cannot extract PDF text. Install pdftotext, pymupdf, or PyPDF2.",
    )
    return ""


def _call_llm(prompt: str, llm_command: str | None = None) -> str:
    """Send prompt to an LLM and return the response."""
    if llm_command:
        cmd = llm_command
    else:
        # Default: use Claude CLI (claude command)
        cmd = "claude -p"

    try:
        result = subprocess.run(
            cmd.split(),
            input=prompt,
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        logger.warning("LLM command failed: %s", result.stderr[:200])
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        logger.warning("LLM command error: %s", e)

    return ""


def _add_to_graph(
    graph: KnowledgeGraph,
    response: str,
    source_file: Path,
) -> IngestResult:
    """Parse LLM response and add extracted info to graph."""
    # Extract JSON from response (handle markdown code blocks)
    json_str = response
    if "```json" in json_str:
        json_str = json_str.split("```json")[1].split("```")[0]
    elif "```" in json_str:
        json_str = json_str.split("```")[1].split("```")[0]

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError:
        logger.warning("Could not parse LLM response as JSON")
        return IngestResult(str(source_file), 0, 0, 0, 0)

    source_name = source_file.stem
    concepts_added = 0
    equations_added = 0
    relationships_added = 0
    citations_added = 0

    # Create a document node for the ingested file
    doc_id = f"ingest:{source_name}"
    graph.add_node(GraphNode(
        id=doc_id,
        type=NodeType.FILE,
        name=source_name,
        display_name=source_file.name,
        domain="ingest",
        metadata={"file_path": str(source_file), "source": "ingested"},
    ))

    # Add concepts
    for concept in data.get("concepts", []):
        name = concept.get("name", "")
        if not name:
            continue
        ctype = concept.get("type", "concept")
        node_id = f"ingest:{ctype}:{name.lower().replace(' ', '-')}"
        type_map = {
            "equation": NodeType.EQUATION,
            "method": NodeType.FUNCTION,
            "algorithm": NodeType.FUNCTION,
            "term": NodeType.TERM,
        }
        graph.add_node(GraphNode(
            id=node_id,
            type=type_map.get(ctype, NodeType.UNRESOLVED),
            name=name,
            display_name=name,
            domain="ingest",
            metadata={
                "description": concept.get("description", ""),
                "source": "ingested",
                "source_file": str(source_file),
            },
        ))
        graph.add_edge(GraphEdge(
            source=doc_id, target=node_id, type=EdgeType.CONTAINS,
        ))
        concepts_added += 1

    # Add equations
    for eq in data.get("equations", []):
        name = eq.get("name", "")
        if not name:
            continue
        eq_id = f"ingest:equation:{name.lower().replace(' ', '-')}"
        graph.add_node(GraphNode(
            id=eq_id,
            type=NodeType.EQUATION,
            name=name,
            display_name=eq.get("label", name),
            domain="ingest",
            metadata={
                "description": eq.get("description", ""),
                "source": "ingested",
            },
        ))
        graph.add_edge(GraphEdge(
            source=doc_id, target=eq_id, type=EdgeType.CONTAINS,
        ))
        equations_added += 1

    # Add citations
    for cite in data.get("citations", []):
        key = cite.get("key", "")
        if not key:
            continue
        cite_id = f"citation:{key}"
        if not graph.has_node(cite_id):
            graph.add_node(GraphNode(
                id=cite_id,
                type=NodeType.UNRESOLVED,
                name=key,
                display_name=cite.get("full_ref", key),
                domain="citation",
                metadata={"source": "ingested"},
            ))
        graph.add_edge(GraphEdge(
            source=doc_id, target=cite_id, type=EdgeType.CITES,
        ))
        citations_added += 1

    # Add relationships
    for rel in data.get("relationships", []):
        src_name = rel.get("source", "")
        tgt_name = rel.get("target", "")
        if not src_name or not tgt_name:
            continue

        # Try to resolve names to existing graph nodes
        src_id = _resolve_name(graph, src_name) or f"ingest:concept:{src_name.lower().replace(' ', '-')}"
        tgt_id = _resolve_name(graph, tgt_name) or f"ingest:concept:{tgt_name.lower().replace(' ', '-')}"

        rel_type = rel.get("type", "related_to")
        edge_type_map = {
            "references": EdgeType.REFERENCES,
            "implements": EdgeType.IMPLEMENTS,
            "derives": EdgeType.DERIVES,
            "cites": EdgeType.CITES,
            "related_to": EdgeType.REFERENCES,
        }
        graph.add_edge(GraphEdge(
            source=src_id,
            target=tgt_id,
            type=edge_type_map.get(rel_type, EdgeType.REFERENCES),
            metadata={
                "description": rel.get("description", ""),
                "source": "ingested",
                "confidence": 0.6,
            },
        ))
        relationships_added += 1

    return IngestResult(
        source_file=str(source_file),
        concepts_added=concepts_added,
        equations_added=equations_added,
        relationships_added=relationships_added,
        citations_added=citations_added,
    )


def _resolve_name(graph: KnowledgeGraph, name: str) -> str | None:
    """Try to find an existing node matching a name."""
    name_lower = name.lower()
    for node_id, attrs in graph.nxgraph.nodes(data=True):
        node_name = attrs.get("name", "").lower()
        if node_name == name_lower or node_name.endswith(f".{name_lower}"):
            return node_id
    return None
