"""runtime ingest — the (file, line) → node join, overlay, and sidecar store."""
from __future__ import annotations

from pathlib import Path

import networkx as nx

from sphinxcontrib.nexus.position import PositionIndex
from sphinxcontrib.nexus.runtime import (
    RuntimeRun,
    RuntimeStore,
    ingest_coverage,
    merge_runs,
    overlay_coverage,
    overlay_cprofile,
    overlay_viztracer,
)

SRC = "/proj/pkg/mod.py"


def _graph() -> nx.MultiDiGraph:
    """One file, laid out so the decorator join can actually FAIL.

    ⚠ This fixture was re-spaced 2026-08-16, and the old spacing is the
    point. Its defs sat at 10 / 30 / 52 — gaps of 20 and 22 against a
    ``DECORATOR_WINDOW`` of 8 — so no definition could ever reach into
    another's decorator lines, and ``test_resolve_decorator_line_above_def``
    was green and *structurally unable* to fail. Real ``@property``
    blocks sit ~5 lines apart (ORPHEUS ``mixture.py``: 108/113/118/123),
    which is exactly the configuration that mis-bound 456 of 3530
    decorated definitions. ``prop_a`` / ``prop_b`` below reproduce it.

    ==================  ==========================================
    ``foo``             decorator 9, def 10, end 20
    ``bar``             def 30, end 40 — undecorated
    ``C``               a class, 45-80 — never a join target
    ``C.prop_a``        decorator 51, def 52, end 55
    ``C.prop_b``        decorator 57, def 58, end 61 — 6 lines below
                        ``prop_a``'s def, i.e. INSIDE the window that
                        used to let it steal line 51
    ``C.legacy``        def 70, end 73, **no** ``decorator_lineno`` —
                        stands in for a graph built before the analyzer
                        recorded it, so the fallback stays exercised
    ``C.after``         def 76, end 79, also without one
    ==================  ==========================================
    """
    g = nx.MultiDiGraph()
    g.add_node("py:function:mod.foo", type="function", name="mod.foo",
               file_path=SRC, lineno=10, end_lineno=20, decorator_lineno=9)
    g.add_node("py:function:mod.bar", type="function", name="mod.bar",
               file_path=SRC, lineno=30, end_lineno=40)
    g.add_node("py:method:mod.C.prop_a", type="method", name="mod.C.prop_a",
               file_path=SRC, lineno=52, end_lineno=55, decorator_lineno=51)
    g.add_node("py:method:mod.C.prop_b", type="method", name="mod.C.prop_b",
               file_path=SRC, lineno=58, end_lineno=61, decorator_lineno=57)
    g.add_node("py:method:mod.C.legacy", type="method", name="mod.C.legacy",
               file_path=SRC, lineno=70, end_lineno=73)
    g.add_node("py:method:mod.C.after", type="method", name="mod.C.after",
               file_path=SRC, lineno=76, end_lineno=79)
    # a class node + a no-position node must be ignored by the join
    g.add_node("py:class:mod.C", type="class", name="mod.C",
               file_path=SRC, lineno=45, end_lineno=80)
    g.add_node("py:function:mod.nofile", type="function", name="mod.nofile")
    return g


# ── the join ────────────────────────────────────────────────────────


def test_only_positioned_functions_methods_can_be_bound():
    idx = PositionIndex(_graph())
    ids = {d.node_id for d in idx.definitions_in(SRC) or ()}
    assert ids == {"py:function:mod.foo", "py:function:mod.bar",
                   "py:method:mod.C.prop_a", "py:method:mod.C.prop_b",
                   "py:method:mod.C.legacy", "py:method:mod.C.after"}
    # the class and the position-less node are not join targets
    assert "py:class:mod.C" not in ids
    assert "py:function:mod.nofile" not in ids


def test_defined_at_exact_def_line():
    idx = PositionIndex(_graph())
    assert idx.defined_at(SRC, 10) == "py:function:mod.foo"
    assert idx.defined_at(SRC, 30) == "py:function:mod.bar"


def test_defined_at_decorator_line_above_def():
    # cProfile reports co_firstlineno at the decorator line (9 / 51), above
    # the AST def line — the join must still land on the function.
    idx = PositionIndex(_graph())
    assert idx.defined_at(SRC, 9) == "py:function:mod.foo"
    assert idx.defined_at(SRC, 51) == "py:method:mod.C.prop_a"


def test_a_decorator_line_is_not_STOLEN_by_the_next_definition():
    """The 456-misbinding defect, as a gate.

    ``prop_a``'s decorator is line 51; ``prop_b`` starts 6 lines below its
    def, inside the old fixed-width window. The retired ``resolve_node``
    wrote the window and the body test as one condition and took the
    LATEST start, so ``prop_b`` matched line 51 and, being scanned later,
    won it — while ``prop_a`` received nothing.

    [M] 2026-08-16 on ORPHEUS: 456 of 3530 decorated definitions, 291 of
    them ``@property``, always stolen by the next sibling down the file.
    """
    idx = PositionIndex(_graph())
    assert idx.defined_at(SRC, 51) == "py:method:mod.C.prop_a"
    assert idx.defined_at(SRC, 57) == "py:method:mod.C.prop_b"
    # and the bodies still belong to their own definitions
    assert idx.defined_at(SRC, 53) == "py:method:mod.C.prop_a"
    assert idx.defined_at(SRC, 59) == "py:method:mod.C.prop_b"


def test_a_graph_without_decorator_linenos_still_binds_the_line_above():
    """The fallback, and it must not steal either.

    ``legacy`` (70-73) and ``after`` (76-79) carry no ``decorator_lineno``
    — the shape of a graph built before the analyzer recorded it. A trace
    line at 69 is ``legacy``'s decorator; the nearest definition BELOW
    wins, not the last one whose window happens to reach back.
    """
    idx = PositionIndex(_graph())
    assert idx.defined_at(SRC, 69) == "py:method:mod.C.legacy"
    assert idx.defined_at(SRC, 75) == "py:method:mod.C.after"


def test_defined_at_body_line():
    idx = PositionIndex(_graph())
    assert idx.defined_at(SRC, 15) == "py:function:mod.foo"


def test_defined_at_unmapped_returns_none():
    idx = PositionIndex(_graph())
    assert idx.defined_at(SRC, 100) is None      # past every range
    assert idx.defined_at("/other.py", 10) is None  # unknown file


def test_a_relative_spelling_finds_the_same_definition():
    """The old index keyed on the RAW stored path, so a relative query
    silently found nothing while ``node_at`` found the node — one of the
    three measured disagreements."""
    idx = PositionIndex(_graph(), root=Path("/proj"))
    assert idx.defined_at("pkg/mod.py", 10) == "py:function:mod.foo"
    assert idx.defined_at(SRC, 10) == "py:function:mod.foo"


# ── cProfile overlay ────────────────────────────────────────────────


def _stats(records):
    """records: {(file,line,func): (nc, tt, ct, callers_dict)} -> pstats dict."""
    return {
        key: (nc, nc, tt, ct, callers)
        for key, (nc, tt, ct, callers) in records.items()
    }


def test_overlay_cprofile_joins_and_builds_edges():
    idx = PositionIndex(_graph())
    foo = (SRC, 10, "foo")
    bar = (SRC, 30, "bar")
    stats = _stats({
        bar: (5, 0.2, 0.2, {foo: (5, 5, 0.0, 0.0)}),     # foo calls bar ×5
        foo: (1, 0.1, 0.5, {}),
    })
    run = overlay_cprofile(stats, idx, "r", source_prefixes=[SRC])
    assert run.calls["py:function:mod.bar"]["ncalls"] == 5
    assert run.calls["py:function:mod.foo"]["cumtime"] == 0.5
    assert ("py:function:mod.foo", "py:function:mod.bar", 5) in run.edges


def test_overlay_cprofile_source_prefix_drops_out_of_scope():
    idx = PositionIndex(_graph())
    stats = _stats({
        ("/usr/lib/python/json.py", 1, "loads"): (9, 0.0, 0.0, {}),
        (SRC, 10, "foo"): (1, 0.1, 0.1, {}),
    })
    run = overlay_cprofile(stats, idx, "r", source_prefixes=[SRC])
    assert set(run.calls) == {"py:function:mod.foo"}
    assert run.unresolved == 0


def test_overlay_cprofile_aggregates_by_node_id():
    # two code objects (decorator line + def line) map to ONE node:
    # ncalls + tottime sum, cumtime takes the max (no double-count).
    idx = PositionIndex(_graph())
    stats = _stats({
        (SRC, 10, "foo"): (3, 0.1, 0.4, {}),
        (SRC, 9, "foo_wrapped"): (2, 0.2, 0.9, {}),
    })
    run = overlay_cprofile(stats, idx, "r", source_prefixes=[SRC])
    m = run.calls["py:function:mod.foo"]
    assert m["ncalls"] == 5 and abs(m["tottime"] - 0.3) < 1e-9
    assert m["cumtime"] == 0.9


def test_overlay_cprofile_recursion_self_loop_dropped():
    idx = PositionIndex(_graph())
    foo = (SRC, 10, "foo")
    stats = _stats({foo: (2, 0.1, 0.1, {foo: (2, 2, 0.0, 0.0)})})
    run = overlay_cprofile(stats, idx, "r", source_prefixes=[SRC])
    assert run.edges == []


def test_overlay_cprofile_unresolved_counted():
    idx = PositionIndex(_graph())
    stats = _stats({(SRC, 999, "ghost"): (1, 0.0, 0.0, {})})
    run = overlay_cprofile(stats, idx, "r", source_prefixes=[SRC])
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
    idx = PositionIndex(_graph())
    run = overlay_coverage(_cov_json(), idx, "c", source_prefixes=[SRC])
    foo = run.coverage["py:function:mod.foo"]
    bar = run.coverage["py:function:mod.bar"]
    assert foo["branches_total"] == 2 and foo["branches_hit"] == 2   # full
    assert bar["branches_total"] == 2 and bar["branches_hit"] == 1   # partial
    assert bar["missing_arcs"] == [[32, 35]]


def test_overlay_coverage_lines():
    idx = PositionIndex(_graph())
    run = overlay_coverage(_cov_json(), idx, "c", source_prefixes=[SRC])
    bar = run.coverage["py:function:mod.bar"]
    assert bar["lines_hit"] == 3 and bar["lines_total"] == 4  # 30,32,33 hit; 35 miss


def test_ingest_coverage_from_file(tmp_path):
    import json
    art = tmp_path / "cov.json"
    art.write_text(json.dumps(_cov_json()))
    run = ingest_coverage(art, _graph(), "c", source_prefixes=[SRC])
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
    idx = PositionIndex(_graph())
    run = overlay_viztracer(_viz_events(), idx, "v", source_prefixes=[SRC])
    foo = run.timeline["py:function:mod.foo"]
    bar = run.timeline["py:function:mod.bar"]
    assert foo["min_depth"] == 0 and bar["min_depth"] == 1   # bar nested in foo
    assert foo["first_ts"] == 0.0                            # earliest = t0
    assert bar["first_ts"] == 0.1                            # (1100-1000)/1000 ms
    assert bar["count"] == 2


def test_overlay_viztracer_scope_and_unresolved():
    idx = PositionIndex(_graph())
    run = overlay_viztracer(_viz_events(), idx, "v", source_prefixes=[SRC])
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
    run = overlay_viztracer(events, PositionIndex(_graph()), "v", source_prefixes=[SRC])
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


# ── the key space, and the ledger that makes a zero-join legible ─────
#
# `coverage json` emits file keys RELATIVE to the directory it ran in;
# the graph indexes ABSOLUTE paths. Compared raw, every file drops — and
# the drop happened upstream of any counter, so a total join failure
# printed `nodes: 0 / edges: 0 / unresolved: 0` and exited 0. That is
# indistinguishable from a workload that genuinely touched nothing, and
# it points in the reassuring direction: a consumer reads it as a
# measurement.
#
# Note the asymmetry that hid it for so long: cProfile's `co_filename`
# and viztracer's event names are absolute, so those backends never hit
# this path. A clean cProfile ingest is NOT evidence coverage works.


def _relative_cov_json():
    """The same report coverage.py actually writes: keys relative."""
    return {
        "meta": {"format": 3, "branch_coverage": True},
        "files": {
            "pkg/mod.py": {          # SRC is /proj/pkg/mod.py
                "executed_lines": [10, 12, 13],
                "missing_lines": [],
                "executed_branches": [[12, 13]],
                "missing_branches": [],
            },
        },
    }


def test_relative_coverage_keys_bind_when_given_a_root():
    """The #56 repair: the two sides are put in one key space."""
    run = overlay_coverage(
        _relative_cov_json(), PositionIndex(_graph()), "c", root="/proj",
    )
    assert "py:function:mod.foo" in run.coverage
    assert run.ledger.bound == 1
    assert run.ledger.diagnosis() is None


def test_relative_coverage_keys_bind_NOTHING_against_a_wrong_root():
    """Control: identical input, only `root` differs.

    Without this the test above could pass for reasons unrelated to the
    key space — and this is also the regression itself, so it pins the
    SHAPE of the failure (every file unindexed) rather than merely that
    it failed.
    """
    run = overlay_coverage(
        _relative_cov_json(), PositionIndex(_graph()), "c",
        root="/somewhere/else",
    )
    assert run.coverage == {}
    assert run.ledger.bound == 0
    assert run.ledger.unindexed_file == 1
    assert "different key spaces" in (run.ledger.diagnosis() or "")


def test_a_zero_join_is_never_silent():
    """The load-bearing claim: `nodes: 0` must carry a reason.

    `diagnosis()` returning None is what the CLI and the MCP server use
    to decide between "store it" and "refuse and exit non-zero".
    """
    empty = overlay_coverage({"files": {}}, PositionIndex(_graph()), "c")
    assert empty.ledger.considered == 0
    assert empty.ledger.diagnosis() is not None


def test_ledger_tells_the_three_drop_reasons_apart():
    """One count would collapse three different remedies into one number."""
    cov = {"files": {
        "/proj/pkg/mod.py": {"executed_lines": [10]},       # binds
        "/proj/other/z.py": {"executed_lines": [1]},        # in scope, unindexed
        "/elsewhere/q.py": {"executed_lines": [1]},         # out of scope
    }}
    run = overlay_coverage(
        cov, PositionIndex(_graph()), "c", source_prefixes=["/proj"],
    )
    assert (run.ledger.bound, run.ledger.unindexed_file,
            run.ledger.outside_scope) == (1, 1, 1)
    assert run.ledger.considered == 3


def test_scope_accepts_several_prefixes():
    """A profiled suite yields tests -> package records.

    Either prefix ALONE drops one endpoint of every one of them, which is
    why this is a list and not a string.
    """
    g = _graph()
    g.add_node("py:function:t.test_foo", type="function", name="t.test_foo",
               file_path="/proj/tests/test_foo.py", lineno=5, end_lineno=8)
    idx = PositionIndex(g)
    cov = {"files": {
        "/proj/pkg/mod.py": {"executed_lines": [10]},
        "/proj/tests/test_foo.py": {"executed_lines": [5]},
    }}

    both = overlay_coverage(cov, idx, "c",
                            source_prefixes=["/proj/pkg", "/proj/tests"])
    assert both.ledger.bound == 2

    one = overlay_coverage(cov, idx, "c", source_prefixes=["/proj/pkg"])
    assert one.ledger.bound == 1, "a single prefix must drop the other endpoint"
    assert one.ledger.outside_scope == 1


def test_scope_is_path_containment_not_string_prefix():
    """`/proj/pkg_scratch` is not inside `/proj/pkg`, though it startswith it."""
    g = _graph()
    g.add_node("py:function:s.f", type="function", name="s.f",
               file_path="/proj/pkg_scratch/s.py", lineno=1, end_lineno=3)
    cov = {"files": {"/proj/pkg_scratch/s.py": {"executed_lines": [1]}}}
    run = overlay_coverage(cov, PositionIndex(g), "c",
                           source_prefixes=["/proj/pkg"])
    assert run.ledger.outside_scope == 1
    assert run.ledger.bound == 0


def test_cprofile_caller_lookups_do_not_inflate_the_ledger():
    """A caller entry re-states a call whose callee already counted.

    Counting both would double the denominator `diagnosis()` reasons
    about, so a half-bound run could look fully bound.
    """
    stats = {
        (SRC, 30, "bar"): (1, 1, 0.0, 0.0, {(SRC, 10, "foo"): (1, 1, 0.0, 0.0)}),
        (SRC, 10, "foo"): (1, 1, 0.0, 0.0, {}),
    }
    run = overlay_cprofile(stats, PositionIndex(_graph()), "r")
    assert run.ledger.considered == 2, "2 stats rows, not 3 with the caller"
    assert run.ledger.bound == 2
    assert run.edges == [("py:function:mod.foo", "py:function:mod.bar", 1)]


def test_a_pre_ledger_sidecar_still_loads():
    """Old sidecars carry a bare `unresolved` and no ledger.

    Its count is exactly today's `no_enclosing_node`; the other three
    reasons were never measured and must stay ZERO rather than be
    guessed — an invented denominator is the defect the ledger exists to
    prevent.
    """
    run = RuntimeRun.from_dict({
        "name": "old", "kind": "cprofile", "unresolved": 7,
        "calls": {"x": {"ncalls": 1, "tottime": 0.0, "cumtime": 0.0}},
    })
    assert run.unresolved == 7
    assert run.ledger.no_enclosing_node == 7
    assert run.ledger.bound == 0
    assert run.ledger.unindexed_file == 0


def test_merge_sums_every_ledger_reason():
    a = RuntimeRun(name="a", kind="cprofile")
    a.ledger.bound, a.ledger.outside_scope = 3, 1
    b = RuntimeRun(name="b", kind="cprofile")
    b.ledger.bound, b.ledger.unindexed_file = 4, 2
    merged = merge_runs([a, b])
    assert merged.ledger.bound == 7
    assert merged.ledger.outside_scope == 1
    assert merged.ledger.unindexed_file == 2


def _graph_with_relative_paths() -> nx.MultiDiGraph:
    """A graph as ``nexus analyze`` writes one: source-root-RELATIVE
    ``file_path``.

    Sphinx builds store absolute paths and every other fixture here does
    too, which makes them all blind to the key-space axis — the exact
    shape that hid a defect in Track 1.1 (``rich_graph``'s relative
    paths matching whatever root they were asked about, inverted).
    """
    g = nx.MultiDiGraph()
    g.add_node("py:function:mod.rel", type="function", name="mod.rel",
               file_path="pkg/mod.py", lineno=10, end_lineno=20)
    return g


def test_a_relatively_stored_path_is_found_by_its_absolute_spelling():
    """The index keys stored paths through ``canonical_path`` too, not
    only queried ones — otherwise a ``nexus analyze`` graph answers every
    absolute trace record with ``None``, silently."""
    idx = PositionIndex(_graph_with_relative_paths(), root=Path("/proj"))
    assert idx.defined_at("/proj/pkg/mod.py", 10) == "py:function:mod.rel"
    assert idx.defined_at("pkg/mod.py", 10) == "py:function:mod.rel"


def _graph_with_a_long_decorator_stack() -> nx.MultiDiGraph:
    """A definition whose decorators span more than ``DECORATOR_WINDOW``.

    Stacked ``@pytest.mark.parametrize`` blocks routinely run 10-20 lines;
    ORPHEUS has many. This is the case the fixed-width window CANNOT
    reach however the window is ordered, and therefore the case that
    makes ``decorator_lineno`` load-bearing rather than merely tidier.
    """
    g = nx.MultiDiGraph()
    g.add_node("py:function:mod.heavy", type="function", name="mod.heavy",
               file_path=SRC, lineno=55, end_lineno=60, decorator_lineno=40)
    return g


def test_a_decorator_stack_longer_than_the_window_still_binds():
    idx = PositionIndex(_graph_with_a_long_decorator_stack())
    # cProfile reports line 40 for this function; 55 - 40 = 15 > 8, so no
    # window can find it. The recorded extent can.
    assert idx.defined_at(SRC, 40) == "py:function:mod.heavy"
    assert idx.defined_at(SRC, 47) == "py:function:mod.heavy"  # mid-stack


# ── per-test attribution (coverage contexts) ────────────────────────
#
# `exercised_by` is the only family that can falsify a coverage CLAIM, so
# these gates are written to catch the two ways it could lie: a context
# that resolves to the WRONG node, and a context that silently resolves to
# nothing. The second is the dangerous one — it reads as "this code is
# untested" rather than as "the join failed".

TESTSRC = "/proj/tests/test_x.py"


def _graph_with_tests() -> nx.MultiDiGraph:
    """`_graph()` plus two test nodes for contexts to resolve onto.

    One plain function and one method, because the resolver tries the two
    node types in order and a fixture with only functions would leave the
    method arm unexercised (vv #17: mutate/gate each arm separately).
    """
    g = _graph()
    g.add_node("py:function:tests.test_x.test_alpha", type="function",
               name="tests.test_x.test_alpha",
               file_path=TESTSRC, lineno=5, end_lineno=8)
    g.add_node("py:method:tests.test_x.TestK.test_beta", type="method",
               name="tests.test_x.TestK.test_beta",
               file_path=TESTSRC, lineno=12, end_lineno=15)
    return g


def _cov_json_ctx():
    """`_cov_json()` with a contexts map exercising every branch.

    * 10, 12 → ``foo``      — one test, then two on one line
    * 13     → ``foo``      — a context naming NO node (the recall gap)
    * 30     → ``bar``      — the method arm
    * 33     → ``bar``      — the EMPTY context (import time, not a test)
    * 71     → ``C.legacy`` — a line coverage EXCLUDED (`# pragma: no
      cover`), so the node has no coverage row at all, yet ran
    """
    d = _cov_json()
    d["meta"]["show_contexts"] = True
    d["files"][SRC]["contexts"] = {
        "10": ["tests.test_x.test_alpha"],
        "12": ["tests.test_x.test_alpha", "tests.test_x.TestK.test_beta"],
        "13": ["tests.test_x.test_vanished"],
        "30": ["tests.test_x.TestK.test_beta"],
        "33": [""],
        "71": ["tests.test_x.test_alpha"],
    }
    return d


def test_contexts_attribute_tests_to_the_nodes_they_executed():
    idx = PositionIndex(_graph_with_tests())
    run = overlay_coverage(_cov_json_ctx(), idx, "c", source_prefixes=[SRC])
    assert run.exercised_by["py:function:mod.foo"] == [
        "py:function:tests.test_x.test_alpha",
        "py:method:tests.test_x.TestK.test_beta",
    ]
    assert run.exercised_by["py:function:mod.bar"] == [
        "py:method:tests.test_x.TestK.test_beta",
    ]


def test_a_context_naming_no_node_is_counted_not_dropped():
    """The recall gap must be a NUMBER, not an empty family.

    A capture whose contexts this cannot resolve — a different tool, a
    stale graph — would otherwise report every node as exercised by
    nobody, which reads exactly like a suite that tests nothing.
    """
    idx = PositionIndex(_graph_with_tests())
    run = overlay_coverage(_cov_json_ctx(), idx, "c", source_prefixes=[SRC])
    assert run.ledger.unknown_context == 1          # test_vanished only
    # …and the empty context is NOT counted as one: it is coverage's
    # spelling for "no test was running", a fact rather than a miss.
    # (Were it counted, this would read 2 and the recall gap would be
    # overstated on every real report — 3761 such lines on the ORPHEUS
    # slice this was built from.)
    assert all(t for tests in run.exercised_by.values() for t in tests)


def test_the_empty_context_does_not_become_a_test():
    idx = PositionIndex(_graph_with_tests())
    run = overlay_coverage(_cov_json_ctx(), idx, "c", source_prefixes=[SRC])
    # line 33 is inside bar and carries ONLY the empty context, so bar's
    # exercisers come from line 30 alone.
    assert len(run.exercised_by["py:function:mod.bar"]) == 1


def test_a_pragma_excluded_node_still_reports_who_ran_it():
    """§6c witness: the case the ordering decision exists to catch.

    ``# pragma: no cover`` removes a line from coverage's numerator AND
    denominator, so ``C.legacy`` has no coverage row — but the code ran,
    and coverage says which test ran it. Scoring and dependence are
    different facts. Measured on ORPHEUS: gating attribution on the
    coverage guard drops 4 nodes, one of them a ``__post_init__`` that
    **131** tests execute.
    """
    idx = PositionIndex(_graph_with_tests())
    run = overlay_coverage(_cov_json_ctx(), idx, "c", source_prefixes=[SRC])
    assert "py:method:mod.C.legacy" not in run.coverage      # not scored…
    assert run.exercised_by["py:method:mod.C.legacy"] == [   # …but it RAN
        "py:function:tests.test_x.test_alpha",
    ]


def test_a_pytest_cov_nodeid_resolves_like_a_qualname():
    """The other capture route, normalised at the boundary (Pattern 7).

    ``pytest-cov --cov-context=test`` writes the pytest node id where
    coverage.py's own ``dynamic_context`` writes the dotted qualname.
    Both must land on one node, and parametrisation must collapse — two
    ids, one graph node, as ``overlay_pytest`` already documents.
    """
    d = _cov_json()
    d["files"][SRC]["contexts"] = {
        # `|setup` / `|run` / `|teardown` is pytest-cov's phase suffix:
        # ONE test arrives as up to three contexts and must collapse.
        "10": ["tests/test_x.py::TestK::test_beta[case-a]|run",
               "tests/test_x.py::TestK::test_beta[case-b]|setup"],
        "12": ["tests/test_x.py::test_alpha|run"],
    }
    idx = PositionIndex(_graph_with_tests())
    run = overlay_coverage(d, idx, "c", source_prefixes=[SRC])
    assert run.exercised_by["py:function:mod.foo"] == [
        "py:function:tests.test_x.test_alpha",
        "py:method:tests.test_x.TestK.test_beta",
    ]
    assert run.ledger.unknown_context == 0


def test_a_report_without_contexts_leaves_the_family_empty():
    """No contexts captured is not a join failure — it is no data."""
    idx = PositionIndex(_graph_with_tests())
    run = overlay_coverage(_cov_json(), idx, "c", source_prefixes=[SRC])
    assert run.exercised_by == {}
    assert run.ledger.unknown_context == 0


def test_merge_unions_the_exercisers():
    """Exact, unlike the merged ``lines_hit``: the test SET is stored."""
    a = RuntimeRun(name="a", kind="coverage",
                   exercised_by={"n": ["t1"], "only_a": ["t9"]})
    b = RuntimeRun(name="b", kind="coverage",
                   exercised_by={"n": ["t2", "t1"]})
    m = merge_runs([a, b])
    assert m.exercised_by["n"] == ["t1", "t2"]
    assert m.exercised_by["only_a"] == ["t9"]


def test_exercised_by_survives_the_sidecar_round_trip(tmp_path):
    store = RuntimeStore(tmp_path / "traces")
    run = RuntimeRun(name="r", kind="coverage",
                     exercised_by={"py:function:mod.foo": ["py:function:t.a"]})
    run.ledger.unknown_context = 3
    store.write(run)
    back = store.load("r")
    assert back is not None
    assert back.exercised_by == run.exercised_by
    assert back.ledger.unknown_context == 3
