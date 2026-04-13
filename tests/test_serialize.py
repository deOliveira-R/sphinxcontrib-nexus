"""Tests for ``_serialize`` assembly functions, especially the
pagination contract: uncapped by default, opt-in ``limit``/``offset``,
and truthful ``total``/``returned`` metadata so clients can detect
truncation."""

from __future__ import annotations

from sphinxcontrib.nexus._serialize import (
    _slice,
    assemble_processes,
    assemble_verification_coverage,
)
from sphinxcontrib.nexus.graph import EdgeType, KnowledgeGraph, NodeType
from sphinxcontrib.nexus.query import GraphQuery


# ---------------------------------------------------------------------------
# _slice helper
# ---------------------------------------------------------------------------


def test_slice_returns_all_when_limit_is_none():
    assert _slice(list(range(10)), None, 0) == list(range(10))


def test_slice_applies_offset_only():
    assert _slice(list(range(10)), None, 3) == [3, 4, 5, 6, 7, 8, 9]


def test_slice_applies_offset_and_limit():
    assert _slice(list(range(10)), 3, 2) == [2, 3, 4]


def test_slice_limit_zero_returns_empty():
    assert _slice(list(range(10)), 0, 0) == []


def test_slice_negative_offset_normalized():
    assert _slice(list(range(5)), None, -2) == list(range(5))


# ---------------------------------------------------------------------------
# Fixture: a graph with enough equations + code + tests to exercise
# verification_coverage and processes above the old 20-entry cap.
# ---------------------------------------------------------------------------


def _build_verification_graph(n_equations: int = 50) -> KnowledgeGraph:
    from sphinxcontrib.nexus.graph import GraphEdge, GraphNode

    g = KnowledgeGraph()
    for i in range(n_equations):
        eq_id = f"math:equation:eq-{i:03d}"
        g.add_node(GraphNode(
            id=eq_id,
            type=NodeType.EQUATION,
            name=f"eq-{i:03d}",
            display_name=f"eq-{i:03d}",
            domain="math",
            metadata={"docname": f"theory/page-{i}"},
        ))
        code_id = f"py:function:mod.impl_{i}"
        g.add_node(GraphNode(
            id=code_id,
            type=NodeType.FUNCTION,
            name=f"mod.impl_{i}",
            display_name=f"impl_{i}",
            domain="py",
            metadata={},
        ))
        g.add_edge(GraphEdge(
            source=code_id, target=eq_id,
            type=EdgeType.IMPLEMENTS, metadata={"confidence": 1.0},
        ))
    return g


# ---------------------------------------------------------------------------
# assemble_verification_coverage
# ---------------------------------------------------------------------------


def test_verification_coverage_is_uncapped_by_default():
    g = _build_verification_graph(n_equations=50)
    q = GraphQuery(g.nxgraph)
    out = assemble_verification_coverage(q)
    assert out["total_entries"] == 50
    assert out["returned"] == 50
    assert len(out["entries"]) == 50
    assert out["limit"] is None


def test_verification_coverage_honors_limit():
    g = _build_verification_graph(n_equations=50)
    q = GraphQuery(g.nxgraph)
    out = assemble_verification_coverage(q, limit=10)
    assert out["total_entries"] == 50
    assert out["returned"] == 10
    assert len(out["entries"]) == 10
    assert out["limit"] == 10
    assert out["offset"] == 0


def test_verification_coverage_honors_offset():
    g = _build_verification_graph(n_equations=50)
    q = GraphQuery(g.nxgraph)
    out = assemble_verification_coverage(q, limit=10, offset=20)
    assert out["returned"] == 10
    first_id = out["entries"][0]["node"]["id"]
    # The 21st entry in iteration order — the exact id depends on the
    # underlying ordering but it must NOT be the first one.
    assert first_id != "math:equation:eq-000"


# ---------------------------------------------------------------------------
# assemble_processes
# ---------------------------------------------------------------------------


def _build_process_graph(n_chains: int = 25) -> KnowledgeGraph:
    from sphinxcontrib.nexus.graph import GraphEdge, GraphNode

    g = KnowledgeGraph()
    for i in range(n_chains):
        # Linear chain: entry_i → step1_i → step2_i → step3_i
        ids = [
            f"py:function:mod.entry_{i}",
            f"py:function:mod.step1_{i}",
            f"py:function:mod.step2_{i}",
            f"py:function:mod.step3_{i}",
        ]
        for nid in ids:
            g.add_node(GraphNode(
                id=nid,
                type=NodeType.FUNCTION,
                name=nid.split(":", 2)[-1],
                display_name=nid.rsplit(".", 1)[-1],
                domain="py",
                metadata={},
            ))
        for s, t in zip(ids, ids[1:]):
            g.add_edge(GraphEdge(
                source=s, target=t, type=EdgeType.CALLS, metadata={},
            ))
    return g


def test_processes_is_uncapped_by_default():
    g = _build_process_graph(n_chains=25)
    q = GraphQuery(g.nxgraph)
    out = assemble_processes(q, min_length=3)
    # All 25 chains must appear — historically this was silently
    # capped at 20.
    assert out["total"] >= 25
    assert out["returned"] == out["total"]
    assert len(out["processes"]) == out["total"]
    assert out["limit"] is None


def test_processes_honors_limit_and_offset():
    g = _build_process_graph(n_chains=25)
    q = GraphQuery(g.nxgraph)
    out = assemble_processes(q, min_length=3, limit=5, offset=10)
    assert out["returned"] == 5
    assert out["limit"] == 5
    assert out["offset"] == 10
