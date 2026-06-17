"""runtime ingest — the (file, line) → node join, overlay, and sidecar store."""
from __future__ import annotations

import networkx as nx

from sphinxcontrib.nexus.runtime import (
    RuntimeRun,
    RuntimeStore,
    build_node_index,
    ingest_coverage,
    merge_runs,
    overlay_coverage,
    overlay_cprofile,
    overlay_viztracer,
    resolve_node,
)

SRC = "/proj/pkg/mod.py"


def _graph() -> nx.MultiDiGraph:
    """Two functions and a property-like method in one file.

    foo: def at line 10 (one decorator at 9), body to 20.
    bar: def at line 30, body to 40.
    prop: a @property method, def at 52 (decorator @property at 51), body to 55.
    """
    g = nx.MultiDiGraph()
    g.add_node("py:function:mod.foo", type="function", name="mod.foo",
               file_path=SRC, lineno=10, end_lineno=20)
    g.add_node("py:function:mod.bar", type="function", name="mod.bar",
               file_path=SRC, lineno=30, end_lineno=40)
    g.add_node("py:method:mod.C.prop", type="method", name="mod.C.prop",
               file_path=SRC, lineno=52, end_lineno=55)
    # a class node + a no-position node must be ignored by the index
    g.add_node("py:class:mod.C", type="class", name="mod.C",
               file_path=SRC, lineno=45, end_lineno=60)
    g.add_node("py:function:mod.nofile", type="function", name="mod.nofile")
    return g


# ── the join ────────────────────────────────────────────────────────


def test_index_only_positioned_functions_methods():
    idx = build_node_index(_graph())
    ids = {nid for spans in idx.values() for _, _, nid in spans}
    assert ids == {"py:function:mod.foo", "py:function:mod.bar",
                   "py:method:mod.C.prop"}   # class + no-file excluded


def test_resolve_exact_def_line():
    idx = build_node_index(_graph())
    assert resolve_node(idx, SRC, 10) == "py:function:mod.foo"
    assert resolve_node(idx, SRC, 30) == "py:function:mod.bar"


def test_resolve_decorator_line_above_def():
    # cProfile reports co_firstlineno at the decorator line (9 / 51), one
    # above the AST def line — the join must still land on the function.
    idx = build_node_index(_graph())
    assert resolve_node(idx, SRC, 9) == "py:function:mod.foo"
    assert resolve_node(idx, SRC, 51) == "py:method:mod.C.prop"


def test_resolve_body_line():
    idx = build_node_index(_graph())
    assert resolve_node(idx, SRC, 15) == "py:function:mod.foo"


def test_resolve_unmapped_returns_none():
    idx = build_node_index(_graph())
    assert resolve_node(idx, SRC, 100) is None      # past every range
    assert resolve_node(idx, "/other.py", 10) is None  # unknown file


# ── cProfile overlay ────────────────────────────────────────────────


def _stats(records):
    """records: {(file,line,func): (nc, tt, ct, callers_dict)} -> pstats dict."""
    return {
        key: (nc, nc, tt, ct, callers)
        for key, (nc, tt, ct, callers) in records.items()
    }


def test_overlay_cprofile_joins_and_builds_edges():
    idx = build_node_index(_graph())
    foo = (SRC, 10, "foo")
    bar = (SRC, 30, "bar")
    stats = _stats({
        bar: (5, 0.2, 0.2, {foo: (5, 5, 0.0, 0.0)}),     # foo calls bar ×5
        foo: (1, 0.1, 0.5, {}),
    })
    run = overlay_cprofile(stats, idx, "r", source_prefix=SRC)
    assert run.calls["py:function:mod.bar"]["ncalls"] == 5
    assert run.calls["py:function:mod.foo"]["cumtime"] == 0.5
    assert ("py:function:mod.foo", "py:function:mod.bar", 5) in run.edges


def test_overlay_cprofile_source_prefix_drops_out_of_scope():
    idx = build_node_index(_graph())
    stats = _stats({
        ("/usr/lib/python/json.py", 1, "loads"): (9, 0.0, 0.0, {}),
        (SRC, 10, "foo"): (1, 0.1, 0.1, {}),
    })
    run = overlay_cprofile(stats, idx, "r", source_prefix=SRC)
    assert set(run.calls) == {"py:function:mod.foo"}
    assert run.unresolved == 0


def test_overlay_cprofile_aggregates_by_node_id():
    # two code objects (decorator line + def line) map to ONE node:
    # ncalls + tottime sum, cumtime takes the max (no double-count).
    idx = build_node_index(_graph())
    stats = _stats({
        (SRC, 10, "foo"): (3, 0.1, 0.4, {}),
        (SRC, 9, "foo_wrapped"): (2, 0.2, 0.9, {}),
    })
    run = overlay_cprofile(stats, idx, "r", source_prefix=SRC)
    m = run.calls["py:function:mod.foo"]
    assert m["ncalls"] == 5 and abs(m["tottime"] - 0.3) < 1e-9
    assert m["cumtime"] == 0.9


def test_overlay_cprofile_recursion_self_loop_dropped():
    idx = build_node_index(_graph())
    foo = (SRC, 10, "foo")
    stats = _stats({foo: (2, 0.1, 0.1, {foo: (2, 2, 0.0, 0.0)})})
    run = overlay_cprofile(stats, idx, "r", source_prefix=SRC)
    assert run.edges == []


def test_overlay_cprofile_unresolved_counted():
    idx = build_node_index(_graph())
    stats = _stats({(SRC, 999, "ghost"): (1, 0.0, 0.0, {})})
    run = overlay_cprofile(stats, idx, "r", source_prefix=SRC)
    assert run.unresolved == 1 and run.calls == {}


# ── coverage overlay (format-3 --branch JSON) ───────────────────────


def _cov_json():
    # foo (10-20): one branch at line 12 took both arcs (full);
    # bar (30-40): branch at 32 took one arc, missed the other (partial).
    return {
        "meta": {"format": 3, "branch_coverage": True},
        "files": {
            SRC: {
                "executed_lines": [10, 12, 13, 30, 32, 33],
                "missing_lines": [35],
                "executed_branches": [[12, 13], [12, 20], [32, 33]],
                "missing_branches": [[32, 35]],
            },
        },
    }


def test_overlay_coverage_branch_attribution():
    idx = build_node_index(_graph())
    run = overlay_coverage(_cov_json(), idx, "c", source_prefix=SRC)
    foo = run.coverage["py:function:mod.foo"]
    bar = run.coverage["py:function:mod.bar"]
    assert foo["branches_total"] == 2 and foo["branches_hit"] == 2   # full
    assert bar["branches_total"] == 2 and bar["branches_hit"] == 1   # partial
    assert bar["missing_arcs"] == [[32, 35]]


def test_overlay_coverage_lines():
    idx = build_node_index(_graph())
    run = overlay_coverage(_cov_json(), idx, "c", source_prefix=SRC)
    bar = run.coverage["py:function:mod.bar"]
    assert bar["lines_hit"] == 3 and bar["lines_total"] == 4  # 30,32,33 hit; 35 miss


def test_ingest_coverage_from_file(tmp_path):
    import json
    art = tmp_path / "cov.json"
    art.write_text(json.dumps(_cov_json()))
    run = ingest_coverage(art, _graph(), "c", source_prefix=SRC)
    assert run.kind == "coverage"
    assert "py:function:mod.bar" in run.coverage


# ── sidecar store ───────────────────────────────────────────────────


def test_store_round_trip(tmp_path):
    store = RuntimeStore(tmp_path / "traces")
    run = RuntimeRun(name="r", kind="cprofile",
                     meta={"command": "x"},
                     calls={"py:function:mod.foo": {"ncalls": 3, "tottime": 0.1,
                                                    "cumtime": 0.2}},
                     edges=[("a", "b", 5)])
    store.write(run)
    back = store.load("r")
    assert back is not None
    assert back.calls == run.calls
    assert back.edges == [("a", "b", 5)]      # tuples survive json round-trip


# ── multi-run union ─────────────────────────────────────────────────


def _cprofile_run(name, calls, edges):
    return RuntimeRun(name=name, kind="cprofile", calls=calls, edges=edges)


def test_merge_single_run_is_identity():
    r = _cprofile_run("r", {"a": {"ncalls": 1, "tottime": 0.0, "cumtime": 0.0}}, [])
    assert merge_runs([r]) is r


def test_merge_unions_calls_and_edges():
    r1 = _cprofile_run(
        "r1",
        {"a": {"ncalls": 3, "tottime": 0.1, "cumtime": 0.9},
         "b": {"ncalls": 1, "tottime": 0.2, "cumtime": 0.2}},
        [("a", "b", 3), ("a", "c", 1)])
    r2 = _cprofile_run(
        "r2",
        {"a": {"ncalls": 5, "tottime": 0.3, "cumtime": 0.4}},
        [("a", "b", 2)])
    m = merge_runs([r1, r2])
    assert m.calls["a"]["ncalls"] == 8                 # 3 + 5 sum
    assert abs(m.calls["a"]["tottime"] - 0.4) < 1e-9   # 0.1 + 0.3
    assert m.calls["a"]["cumtime"] == 0.9              # max(0.9, 0.4)
    edges = {(u, v): c for u, v, c in m.edges}
    assert edges[("a", "b")] == 5                      # 3 + 2
    assert edges[("a", "c")] == 1                      # only r1


def test_merge_coverage_branch_missing_only_if_missing_in_all():
    # arc [2,5] missing in r1 but taken in r2 -> hit in union;
    # arc [3,9] missing in BOTH -> still missing.
    def cov(missing):
        return RuntimeRun(name="x", kind="coverage", coverage={
            "n": {"lines_hit": 1, "lines_total": 2, "branches_hit": 2 - len(missing),
                  "branches_total": 2, "missing_arcs": missing}})
    m = merge_runs([cov([[2, 5], [3, 9]]), cov([[3, 9]])])
    c = m.coverage["n"]
    assert c["missing_arcs"] == [[3, 9]]               # intersection
    assert c["branches_hit"] == 1 and c["branches_total"] == 2


# ── viztracer overlay (temporal order) ──────────────────────────────


def _viz_events():
    # foo (10-20) outer; bar (30-40) called twice inside; a stdlib frame
    # (filtered by source_prefix); a ghost in-scope line (unresolved).
    def ev(name, ts, dur):
        return {"ph": "X", "name": name, "ts": ts, "dur": dur}
    return [
        ev(f"foo ({SRC}:10)", 1000.0, 500.0),
        ev(f"bar ({SRC}:30)", 1100.0, 100.0),
        ev(f"bar ({SRC}:30)", 1300.0, 50.0),
        ev("loads (/usr/lib/json.py:1)", 1000.0, 5.0),   # out of scope
        ev(f"ghost ({SRC}:999)", 1200.0, 1.0),           # in scope, no node
        {"ph": "M", "name": "process_name"},             # metadata, ignored
    ]


def test_overlay_viztracer_depth_and_order():
    idx = build_node_index(_graph())
    run = overlay_viztracer(_viz_events(), idx, "v", source_prefix=SRC)
    foo = run.timeline["py:function:mod.foo"]
    bar = run.timeline["py:function:mod.bar"]
    assert foo["min_depth"] == 0 and bar["min_depth"] == 1   # bar nested in foo
    assert foo["first_ts"] == 0.0                            # earliest = t0
    assert bar["first_ts"] == 0.1                            # (1100-1000)/1000 ms
    assert bar["count"] == 2


def test_overlay_viztracer_scope_and_unresolved():
    idx = build_node_index(_graph())
    run = overlay_viztracer(_viz_events(), idx, "v", source_prefix=SRC)
    # the stdlib frame is dropped silently; the in-scope ghost is counted
    assert run.unresolved == 1
    assert set(run.timeline) == {"py:function:mod.foo", "py:function:mod.bar"}


def test_overlay_viztracer_depth_shared_start_and_zero_dur():
    # corner: a child sharing its parent's start ts, plus a zero-duration
    # event. The (ts, -dur) sort puts the container first; neither breaks the
    # nesting depth. (Pins the reviewer's concern that the happy-path test only
    # used cleanly-separated intervals.)
    def ev(name, ts, dur):
        return {"ph": "X", "name": name, "ts": ts, "dur": dur}
    events = [
        ev(f"foo ({SRC}:10)", 1000.0, 100.0),   # outer [1000,1100]
        ev(f"bar ({SRC}:30)", 1000.0, 40.0),     # shares START -> still depth 1
        ev(f"bar ({SRC}:30)", 1050.0, 0.0),      # zero-dur, inside foo -> depth 1
    ]
    run = overlay_viztracer(events, build_node_index(_graph()), "v", source_prefix=SRC)
    assert run.timeline["py:function:mod.foo"]["min_depth"] == 0
    assert run.timeline["py:function:mod.bar"]["min_depth"] == 1
    assert run.timeline["py:function:mod.bar"]["count"] == 2


def test_store_list_and_delete(tmp_path):
    store = RuntimeStore(tmp_path / "traces")
    store.write(RuntimeRun(name="one", kind="cprofile",
                           calls={"x": {"ncalls": 1, "tottime": 0.0, "cumtime": 0.0}}))
    store.write(RuntimeRun(name="two", kind="coverage",
                           coverage={"y": {"branches_total": 0}}))
    names = {r["name"] for r in store.list_runs()}
    assert names == {"one", "two"}
    assert store.load("missing") is None
    assert store.delete("one") is True
    assert store.delete("one") is False
    assert {r["name"] for r in store.list_runs()} == {"two"}
