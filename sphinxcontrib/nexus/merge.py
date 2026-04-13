"""Merge AST-derived graph into Sphinx-derived graph."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sphinxcontrib.nexus.graph import EdgeType, KnowledgeGraph, NodeType

if TYPE_CHECKING:
    import networkx as nx

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

    # NOTE: _infer_implements is called separately after all merges,
    # not here — because merge_graphs may be called per-directory.

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
    """Infer IMPLEMENTS edges from doc structure (conservative).

    Strategy: for each theory page, find its equations and the code
    symbols it DOCUMENTS. Only create an IMPLEMENTS edge when the code
    symbol's name shares tokens with the equation label — indicating
    a genuine implementation relationship rather than just appearing
    on the same page.

    Examples of matches:
      - sweep_spherical ↔ transport-spherical (share "spherical")
      - compute_pinf_group ↔ p-inf (share "p"/"inf" — too weak, skip)
      - solve_cp ↔ collision-rate (share no tokens — skip)
      - _compute_slab_rcp ↔ surface-to-region (share no tokens — skip)
      - _compute_slab_rcp ↔ rcp-from-double-antideriv (share "rcp" — match)
    """
    import re as _re

    code_types = {"function", "method", "class"}
    seen: set[tuple[str, str]] = set()
    count = 0

    def _tokenize(name: str) -> set[str]:
        """Split a name into meaningful tokens (min length 3)."""
        tokens = _re.split(r"[-_.:]+", name.lower())
        return {t for t in tokens if len(t) >= 3}

    for doc_id, attrs in g.nodes(data=True):
        if attrs.get("type") != "file":
            continue

        eq_map: dict[str, set[str]] = {}
        code_map: dict[str, set[str]] = {}

        for _, tgt, data in g.out_edges(doc_id, data=True):
            tgt_attrs = g.nodes.get(tgt, {})
            tgt_type = tgt_attrs.get("type", "")
            tgt_name = tgt_attrs.get("name", "")
            edge_type = data.get("type", "")

            if tgt_type == "equation" and tgt not in eq_map:
                eq_map[tgt] = _tokenize(tgt_name)
            elif tgt_type in code_types and edge_type == "documents" and tgt not in code_map:
                code_map[tgt] = _tokenize(tgt_name)

        equations = list(eq_map.items())
        code_symbols = list(code_map.items())

        if not equations or not code_symbols:
            continue

        for code_id, code_tokens in code_symbols:
            for eq_id, eq_tokens in equations:
                # Require at least one shared token of length >= 3
                shared = code_tokens & eq_tokens
                if not shared:
                    continue
                pair = (code_id, eq_id)
                if pair in seen:
                    continue
                # Skip if any explicit TESTS or IMPLEMENTS edge already
                # links these two nodes. An edge is "explicit" when its
                # source is NOT the string "inferred" — covers
                # registry-sourced, directive-sourced, and
                # pytest.mark.verifies-sourced edges alike.
                existing = g.get_edge_data(code_id, eq_id, default={})
                if any(
                    d.get("type") in ("implements", "tests")
                    and d.get("source") != "inferred"
                    for d in existing.values()
                ):
                    seen.add(pair)
                    continue
                seen.add(pair)
                g.add_edge(
                    code_id, eq_id,
                    type="implements", source="inferred",
                    confidence=0.7,
                    shared_tokens=sorted(shared),
                )
                count += 1

    if count:
        logger.info("Inferred %d IMPLEMENTS edges (code → equation)", count)


def write_verifies_edges(g: "nx.MultiDiGraph") -> int:
    """Write ``EdgeType.TESTS`` edges from ``@pytest.mark.verifies`` metadata.

    Walks every function/method node with a ``verifies`` tuple in its
    metadata (populated by ``ast_analyzer._parse_pytest_markers``). For
    each label in that tuple, looks up the ``math:equation:<label>``
    node in the graph. When found, adds a ``tests`` edge with
    confidence 1.0 and ``source="pytest.mark.verifies"``. When the
    equation node does not exist, logs a warning and skips — we do
    not create phantom equation nodes here.

    Returns the number of edges written.
    """
    count = 0
    for node_id, attrs in list(g.nodes(data=True)):
        labels = attrs.get("verifies")
        if not labels:
            continue
        for label in labels:
            eq_id = f"math:equation:{label}"
            if eq_id not in g:
                logger.warning(
                    "pytest.mark.verifies(%r) on %s has no matching "
                    "equation node %s — skipping",
                    label, node_id, eq_id,
                )
                continue
            # Skip if ANY explicit TESTS edge already links this
            # (test, equation) pair — registry, directive, a prior
            # run of this pass, or any future explicit source. A
            # ``source="inferred"`` edge is weak and is allowed to
            # coexist with the marker-declared edge. This guard
            # makes the pipeline pass-order irrelevant: whichever
            # explicit source runs first wins, the later ones are
            # no-ops.
            existing = g.get_edge_data(node_id, eq_id, default={})
            if any(
                d.get("type") == EdgeType.TESTS.value
                and d.get("source") not in (None, "inferred")
                for d in existing.values()
            ):
                continue
            g.add_edge(
                node_id,
                eq_id,
                type=EdgeType.TESTS.value,
                source="pytest.mark.verifies",
                confidence=1.0,
            )
            count += 1
    if count:
        logger.info("Wrote %d TESTS edges from @pytest.mark.verifies", count)
    return count
