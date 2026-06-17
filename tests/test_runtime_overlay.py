"""runtime overlay queries — joining a RuntimeRun onto the static graph."""
from __future__ import annotations

import itertools

import networkx as nx
import pytest

from sphinxcontrib.nexus._serialize import to_dict, to_json
from sphinxcontrib.nexus.query import GraphQuery
from sphinxcontrib.nexus.runtime import RuntimeRun

_key = itertools.count()


def _graph() -> nx.MultiDiGraph:
    """A -> B static call; B -> D static call; A discriminates on tag 'geometry'.

    Runtime (below) will fire A->B (matches static), A->C (dynamic-only, no
    static edge), and leave B->D unfired (dead in the run). C is a @property
    (accessor); A/B/D are substantive (wide line spans).
    """
    g = nx.MultiDiGraph()
    spans = {"A": (10, 80), "B": (100, 180), "C": (200, 201), "D": (300, 380)}
    for n in "ABCD":
        lo, hi = spans[n]
        g.add_node(f"py:function:m.{n}", type="function", name=f"m.{n}",
                   domain="py", file_path="/p/m.py", lineno=lo, end_lineno=hi)
    # C is a property -> accessor (the classifier's primary signal)
    g.nodes["py:function:m.C"]["decorators"] = ["property"]
    g.add_edge("py:function:m.A", "py:function:m.B", key=next(_key), type="calls")
    g.add_edge("py:function:m.B", "py:function:m.D", key=next(_key), type="calls")
    # A discriminates on a tag -> missing-type cross-ref for runtime_branches
    g.add_node("py:tag:geometry", type="tag", name="geometry", domain="py")
    g.add_edge("py:function:m.A", "py:tag:geometry", key=next(_key),
               type="discriminates_on")
    return g


def _call_run() -> RuntimeRun:
    return RuntimeRun(
        name="r", kind="cprofile",
        calls={
            "py:function:m.A": {"ncalls": 1, "tottime": 0.1, "cumtime": 0.9},
            "py:function:m.B": {"ncalls": 8, "tottime": 0.5, "cumtime": 0.5},
            "py:function:m.C": {"ncalls": 4, "tottime": 0.2, "cumtime": 0.2},
        },
        edges=[
            ("py:function:m.A", "py:function:m.B", 8),   # fired (static)
            ("py:function:m.A", "py:function:m.C", 4),   # dynamic-only
        ],
    )


# ── runtime_hotspots ────────────────────────────────────────────────


def test_hotspots_by_cumtime():
    res = GraphQuery(_graph()).runtime_hotspots(_call_run(), by="cumtime")
    assert res[0].node.id == "py:function:m.A"   # 0.9 cumulative
    assert res[0].cumtime == 0.9


def test_hotspots_by_ncalls():
    res = GraphQuery(_graph()).runtime_hotspots(_call_run(), by="ncalls")
    assert res[0].node.id == "py:function:m.B"   # 8 calls
    assert res[0].ncalls == 8


def test_hotspots_limit_and_bad_metric():
    q = GraphQuery(_graph())
    assert len(q.runtime_hotspots(_call_run(), limit=1)) == 1
    with pytest.raises(ValueError):
        q.runtime_hotspots(_call_run(), by="bogus")


def test_hotspots_empty_on_coverage_run():
    cov = RuntimeRun(name="c", kind="coverage",
                     coverage={"py:function:m.A": {"branches_total": 0}})
    assert GraphQuery(_graph()).runtime_hotspots(cov) == []


# ── runtime_edges ───────────────────────────────────────────────────


def test_edges_dynamic_only():
    res = GraphQuery(_graph()).runtime_edges(_call_run(), mode="dynamic_only")
    pairs = {(r.source.id, r.target.id) for r in res}
    assert pairs == {("py:function:m.A", "py:function:m.C")}
    assert all(r.in_static is False for r in res)


def test_edges_fired():
    res = GraphQuery(_graph()).runtime_edges(_call_run(), mode="fired")
    pairs = {(r.source.id, r.target.id): r.count for r in res}
    assert pairs == {("py:function:m.A", "py:function:m.B"): 8}
    assert all(r.in_static for r in res)


def test_edges_dead_in_run():
    # B->D is static, both endpoints are run-reachable (B called, D... not).
    # D is NOT reachable -> B->D should NOT count as dead (target unreached).
    res = GraphQuery(_graph()).runtime_edges(_call_run(), mode="dead")
    assert res == []
    # make D reachable -> now B->D is a genuine dead-in-run edge.
    run = _call_run()
    run.calls["py:function:m.D"] = {"ncalls": 1, "tottime": 0.0, "cumtime": 0.0}
    res = GraphQuery(_graph()).runtime_edges(run, mode="dead")
    assert {(r.source.id, r.target.id) for r in res} == {
        ("py:function:m.B", "py:function:m.D")}
    assert res[0].count == 0


def test_edges_node_filter_and_bad_mode():
    q = GraphQuery(_graph())
    assert q.runtime_edges(_call_run(), mode="dynamic_only", node="m.B") == []
    assert q.runtime_edges(_call_run(), mode="dynamic_only", node="m.A")
    with pytest.raises(ValueError):
        q.runtime_edges(_call_run(), mode="bogus")


# ── edge classifier (accessor vs substantive) ───────────────────────


def test_edges_accessor_flag_set_for_property_target():
    # A->C: C is a @property -> the edge is plumbing, flagged accessor.
    res = GraphQuery(_graph()).runtime_edges(_call_run(), mode="dynamic_only")
    edge = next(r for r in res if r.target.id == "py:function:m.C")
    assert edge.accessor is True


def test_edges_substantive_only_drops_accessor_edges():
    q = GraphQuery(_graph())
    full = q.runtime_edges(_call_run(), mode="dynamic_only")
    subst = q.runtime_edges(_call_run(), mode="dynamic_only", substantive_only=True)
    assert any(r.target.id == "py:function:m.C" for r in full)      # present raw
    assert all(r.target.id != "py:function:m.C" for r in subst)     # dropped


def test_is_accessor_property_and_tiny_span():
    q = GraphQuery(_graph())
    assert q._is_accessor("py:function:m.C") is True       # @property
    assert q._is_accessor("py:function:m.A") is False      # wide span, no deco


# ── runtime_timeline (temporal order) ───────────────────────────────


def _viz_run() -> RuntimeRun:
    return RuntimeRun(
        name="v", kind="viztracer",
        timeline={
            "py:function:m.A": {"first_ts": 0.0, "count": 1, "min_depth": 0},
            "py:function:m.B": {"first_ts": 0.5, "count": 3, "min_depth": 1},
            "py:function:m.C": {"first_ts": 0.9, "count": 9, "min_depth": 2},
        },
    )


def test_timeline_ordered_by_first_entry():
    res = GraphQuery(_graph()).runtime_timeline(_viz_run())
    assert [r.node.id for r in res] == [
        "py:function:m.A", "py:function:m.B", "py:function:m.C"]
    assert res[1].count == 3 and res[1].depth == 1


def test_timeline_max_depth_keeps_high_level_stages():
    res = GraphQuery(_graph()).runtime_timeline(_viz_run(), max_depth=1)
    ids = {r.node.id for r in res}
    assert ids == {"py:function:m.A", "py:function:m.B"}   # depth-2 C dropped


def test_timeline_skips_stale_nodes():
    g = _graph()
    g.remove_node("py:function:m.C")
    res = GraphQuery(g).runtime_timeline(_viz_run())
    assert "py:function:m.C" not in {r.node.id for r in res}


# ── runtime_branches ────────────────────────────────────────────────


def _cov_run() -> RuntimeRun:
    return RuntimeRun(
        name="c", kind="coverage",
        coverage={
            # A discriminates on 'geometry' AND is partial -> missing-type suspect
            "py:function:m.A": {"lines_hit": 3, "lines_total": 4,
                                "branches_hit": 1, "branches_total": 2,
                                "missing_arcs": [[2, 5]]},
            # B partial, no discrimination
            "py:function:m.B": {"lines_hit": 2, "lines_total": 2,
                                "branches_hit": 1, "branches_total": 3,
                                "missing_arcs": [[3, 9], [3, 10]]},
            # C fully covered -> hidden when partial_only
            "py:function:m.C": {"lines_hit": 1, "lines_total": 1,
                                "branches_hit": 2, "branches_total": 2,
                                "missing_arcs": []},
        },
    )


def test_branches_partial_only_hides_full():
    res = GraphQuery(_graph()).runtime_branches(_cov_run())
    ids = {r.node.id for r in res}
    assert ids == {"py:function:m.A", "py:function:m.B"}   # C is fully covered


def test_branches_discriminator_ranked_first():
    res = GraphQuery(_graph()).runtime_branches(_cov_run())
    assert res[0].node.id == "py:function:m.A"
    assert res[0].discriminates == ["geometry"]


def test_branches_all_includes_full():
    res = GraphQuery(_graph()).runtime_branches(_cov_run(), partial_only=False)
    assert "py:function:m.C" in {r.node.id for r in res}


def test_branches_node_filter():
    res = GraphQuery(_graph()).runtime_branches(_cov_run(), node="m.B")
    assert {r.node.id for r in res} == {"py:function:m.B"}


# ── re-bind contract: a node resolved at ingest can vanish in a rebuild ──


def test_overlay_queries_survive_stale_nodes_after_rebuild():
    """The sidecar exists BECAUSE the graph is rebuilt between ingest and
    query; a traced node may be renamed/removed by query time. Every overlay
    query must skip stale endpoints and still serialize cleanly (a NodeResult
    built for a missing node carries an unserializable degree view)."""
    g = _graph()
    # the rebuild dropped C entirely (it was a dynamic_only target) and D
    g.remove_node("py:function:m.C")
    g.remove_node("py:function:m.D")
    q = GraphQuery(g)

    run = _call_run()
    run.calls["py:function:m.D"] = {"ncalls": 1, "tottime": 0.0, "cumtime": 0.0}

    # all three queries, every mode — must not raise AND must serialize
    for results in (
        q.runtime_hotspots(run, by="cumtime"),
        q.runtime_hotspots(run, by="ncalls"),
        q.runtime_edges(run, mode="dynamic_only"),
        q.runtime_edges(run, mode="fired"),
        q.runtime_edges(run, mode="dead"),
        q.runtime_branches(_cov_run(), partial_only=False),
    ):
        to_json(to_dict(results))   # would TypeError on a stale degree view
        ids = {getattr(r, "node", None) and r.node.id for r in results}
        assert "py:function:m.C" not in ids
        for r in results:
            if hasattr(r, "source"):
                assert r.source.id != "py:function:m.C"
                assert r.target.id != "py:function:m.C"

    # the surviving fired edge A->B still comes through
    fired = q.runtime_edges(run, mode="fired")
    assert {(r.source.id, r.target.id) for r in fired} == {
        ("py:function:m.A", "py:function:m.B")}
