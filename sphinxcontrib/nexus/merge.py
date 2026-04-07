"""Merge AST-derived graph into Sphinx-derived graph."""

from __future__ import annotations

import logging

from sphinxcontrib.nexus.graph import KnowledgeGraph, NodeType

logger = logging.getLogger(__name__)


def merge_graphs(
    sphinx_kg: KnowledgeGraph,
    ast_kg: KnowledgeGraph,
) -> KnowledgeGraph:
    """Merge an AST-derived graph into a Sphinx-derived graph.

    Rules:
    1. Node in both: keep Sphinx attrs, add AST metadata (file_path, lineno)
    2. Node in AST only: add to Sphinx graph (undocumented symbol)
    3. Node in Sphinx only: keep as-is
    4. UNRESOLVED reconciliation: retarget edges from unresolved → concrete
    5. All edges from both graphs kept (MultiDiGraph)
    """
    sg = sphinx_kg.nxgraph
    ag = ast_kg.nxgraph

    # Step 1 & 2: merge nodes
    for node_id, ast_attrs in ag.nodes(data=True):
        if node_id in sg:
            # Enrich existing Sphinx node with AST metadata
            for key in ("file_path", "lineno", "end_lineno"):
                if key in ast_attrs:
                    sg.nodes[node_id][key] = ast_attrs[key]
            sg.nodes[node_id]["source"] = "both"
        else:
            # AST-only node — add it
            attrs = dict(ast_attrs)
            attrs["source"] = "ast_only"
            sg.add_node(node_id, **attrs)

    # Step 4: reconcile UNRESOLVED nodes
    # Build a lookup: short name → AST concrete node ID
    ast_by_short_name: dict[str, str] = {}
    for node_id, attrs in ag.nodes(data=True):
        name = attrs.get("name", "")
        if name:
            short = name.rsplit(".", 1)[-1]
            ast_by_short_name[short] = node_id
            ast_by_short_name[name] = node_id

    unresolved_to_remove: list[str] = []
    for node_id, attrs in list(sg.nodes(data=True)):
        if attrs.get("type") != NodeType.UNRESOLVED.value:
            continue
        name = attrs.get("name", "")
        # Try to find a concrete AST node matching this name
        concrete_id = ast_by_short_name.get(name)
        if concrete_id and concrete_id in sg and concrete_id != node_id:
            # Retarget all edges pointing to the unresolved node
            for src, _, key, data in list(sg.in_edges(node_id, keys=True, data=True)):
                sg.add_edge(src, concrete_id, **data)
                sg.remove_edge(src, node_id, key=key)
            for _, tgt, key, data in list(sg.out_edges(node_id, keys=True, data=True)):
                sg.add_edge(concrete_id, tgt, **data)
                sg.remove_edge(node_id, tgt, key=key)
            unresolved_to_remove.append(node_id)

    for node_id in unresolved_to_remove:
        sg.remove_node(node_id)

    if unresolved_to_remove:
        logger.info(
            "Reconciled %d UNRESOLVED nodes with AST-found symbols",
            len(unresolved_to_remove),
        )

    # Step 5: copy all AST edges
    for src, tgt, _key, data in ag.edges(keys=True, data=True):
        sg.add_edge(src, tgt, **data)

    # Step 6: infer IMPLEMENTS edges
    # NOTE: page-level inference is too coarse (produces false positives).
    # Requires section-level extraction for precision. Disabled until v0.3.0.
    # _infer_implements(sg)

    # Step 7: tag confidence scores on all edges
    _tag_confidence(sg)

    return sphinx_kg


def _tag_confidence(g: "nx.MultiDiGraph") -> None:
    """Tag confidence scores on ALL edges.

    - Sphinx-extracted (documents, references, contains, equation_ref, cites): 1.0
    - AST structural (calls, imports, inherits, type_uses): 1.0
    - Inferred (implements): 0.7 (already tagged in _infer_implements)
    """
    for _, _, data in g.edges(data=True):
        if "confidence" not in data:
            data["confidence"] = 1.0


def _infer_implements(g: "nx.MultiDiGraph") -> None:
    """Infer IMPLEMENTS edges from doc structure.

    Strategy: for each document page, find equations it CONTAINS and
    code symbols it DOCUMENTS (via :func:, :class:, :meth: roles).
    Only connect code to equations on the SAME theory page — not across
    api/ and theory/ pages.

    This avoids the combinatorial explosion of connecting every code
    symbol to every equation on loosely related pages.
    """
    code_types = {"function", "method", "class"}
    seen: set[tuple[str, str]] = set()
    count = 0

    # Only consider theory pages (pages that contain equations)
    for doc_id, attrs in g.nodes(data=True):
        if attrs.get("type") != "file":
            continue

        equations: list[str] = []
        code_symbols: list[str] = []

        for _, tgt, data in g.out_edges(doc_id, data=True):
            tgt_attrs = g.nodes.get(tgt, {})
            tgt_type = tgt_attrs.get("type", "")
            edge_type = data.get("type", "")

            if tgt_type == "equation":
                equations.append(tgt)
            # Only DOCUMENTS edges — these are explicit :func:/:class: references
            # from the theory page to code. CONTAINS and REFERENCES are too broad.
            elif tgt_type in code_types and edge_type == "documents":
                code_symbols.append(tgt)

        if not equations or not code_symbols:
            continue

        for code_id in code_symbols:
            for eq_id in equations:
                pair = (code_id, eq_id)
                if pair not in seen:
                    seen.add(pair)
                    g.add_edge(
                        code_id, eq_id,
                        type="implements", source="inferred",
                        confidence=0.7,
                    )
                    count += 1

    if count:
        logger.info("Inferred %d IMPLEMENTS edges (code → equation)", count)
