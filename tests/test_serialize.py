"""Tests for ``_serialize`` assembly functions: the pagination
contract (uncapped by default, opt-in ``limit``/``offset``, truthful
``total``/``returned`` metadata) and the token-budget contract for
``context``/``impact`` (capped by default, most-connected-first,
honest ``omitted`` counts)."""

from __future__ import annotations

from sphinxcontrib.nexus._serialize import (
    _slice,
    assemble_context,
    assemble_impact,
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


# ---------------------------------------------------------------------------
# assemble_context / assemble_impact — token budgets.
#
# A hub node's full context serializes to megabytes (measured 2.7 MB
# for degree-3429 numpy.array on the real ORPHEUS graph), so both
# tools default to per-bucket caps. The contract under test: buckets
# are sorted most-connected-first, caps are honest (an ``omitted``
# block reports every drop), totals stay true, and ``None`` uncaps.
# ---------------------------------------------------------------------------


def _build_hub_graph(n_callers: int = 30, n_meta: int = 10) -> KnowledgeGraph:
    """A hub with ``n_callers`` direct callers of strictly increasing
    degree (caller_i also calls i filler sinks), plus ``n_meta``
    second-level callers of caller_000 for impact depth-2 content."""
    from sphinxcontrib.nexus.graph import GraphEdge, GraphNode

    g = KnowledgeGraph()

    def add_function(nid: str) -> None:
        g.add_node(GraphNode(
            id=nid,
            type=NodeType.FUNCTION,
            name=nid.split(":", 2)[-1],
            display_name=nid.rsplit(".", 1)[-1],
            domain="py",
            metadata={},
        ))

    hub = "py:function:mod.hub"
    add_function(hub)
    for i in range(n_callers):
        caller = f"py:function:mod.caller_{i:03d}"
        add_function(caller)
        g.add_edge(GraphEdge(source=caller, target=hub, type=EdgeType.CALLS, metadata={}))
        for k in range(i):  # degree spread: caller_i has degree 1 + i
            sink = f"py:function:mod.sink_{i:03d}_{k:03d}"
            add_function(sink)
            g.add_edge(GraphEdge(source=caller, target=sink, type=EdgeType.CALLS, metadata={}))
    for j in range(n_meta):
        meta = f"py:function:mod.meta_{j:03d}"
        add_function(meta)
        g.add_edge(GraphEdge(
            source=meta, target="py:function:mod.caller_000",
            type=EdgeType.CALLS, metadata={},
        ))
    return g


HUB = "py:function:mod.hub"


def test_context_caps_buckets_and_reports_omissions():
    g = _build_hub_graph(n_callers=30)
    q = GraphQuery(g.nxgraph)
    out = assemble_context(q, HUB)  # default per_type_limit=25
    assert len(out["incoming"]["calls"]) == 25
    assert out["omitted"] == {"incoming": {"calls": 5}}
    assert "hint" in out


def test_context_buckets_sorted_most_connected_first():
    g = _build_hub_graph(n_callers=30)
    q = GraphQuery(g.nxgraph)
    out = assemble_context(q, HUB)
    degrees = [e["degree"] for e in out["incoming"]["calls"]]
    assert degrees == sorted(degrees, reverse=True)
    # caller_029 has the largest degree spread — it must survive the cap
    assert out["incoming"]["calls"][0]["id"] == "py:function:mod.caller_029"


def test_context_uncapped_when_limit_none():
    g = _build_hub_graph(n_callers=30)
    q = GraphQuery(g.nxgraph)
    out = assemble_context(q, HUB, per_type_limit=None)
    assert len(out["incoming"]["calls"]) == 30
    assert "omitted" not in out
    assert "hint" not in out


def test_context_under_cap_node_has_no_omitted_key():
    g = _build_hub_graph(n_callers=30)
    q = GraphQuery(g.nxgraph)
    out = assemble_context(q, "py:function:mod.caller_005")
    assert "omitted" not in out
    # shape unchanged for the common small-node case
    assert set(out) == {"node", "outgoing", "incoming"}


def test_impact_caps_depth_buckets_and_keeps_true_total():
    g = _build_hub_graph(n_callers=30, n_meta=10)
    q = GraphQuery(g.nxgraph)
    out = assemble_impact(
        q, HUB, direction="upstream", max_depth=2,
        edge_types=["calls"], per_depth_limit=5,
    )
    assert len(out["by_depth"][1]) == 5
    assert len(out["by_depth"][2]) == 5
    assert out["omitted"] == {1: 25, 2: 5}
    # total_affected is the TRUE traversal count, not the capped one
    assert out["total_affected"] == 40
    assert "hint" in out


def test_impact_buckets_sorted_most_connected_first():
    g = _build_hub_graph(n_callers=30)
    q = GraphQuery(g.nxgraph)
    out = assemble_impact(
        q, HUB, direction="upstream", max_depth=1, per_depth_limit=5,
    )
    degrees = [n["degree"] for n in out["by_depth"][1]]
    assert degrees == sorted(degrees, reverse=True)
    assert out["by_depth"][1][0]["id"] == "py:function:mod.caller_029"


def test_impact_uncapped_matches_raw_query_result():
    g = _build_hub_graph(n_callers=30, n_meta=10)
    q = GraphQuery(g.nxgraph)
    out = assemble_impact(
        q, HUB, direction="upstream", max_depth=2, per_depth_limit=None,
    )
    raw = q.impact(HUB, direction="upstream", max_depth=2)
    assert "omitted" not in out
    assert out["total_affected"] == raw.total_affected
    for depth, nodes in raw.by_depth.items():
        assert {n["id"] for n in out["by_depth"][depth]} == {n.id for n in nodes}


def test_impact_under_cap_has_no_omitted_key():
    g = _build_hub_graph(n_callers=30, n_meta=10)
    q = GraphQuery(g.nxgraph)
    out = assemble_impact(q, HUB, direction="upstream", max_depth=2)
    # 30 and 10 are both under the default 50-per-depth budget
    assert "omitted" not in out
    assert set(out) == {"target", "direction", "by_depth", "total_affected"}
