"""native_place_candidates — Feature-Envy / 'native place' detector."""
from __future__ import annotations

import networkx as nx

from sphinxcontrib.nexus.query import GraphQuery


def _graph() -> nx.MultiDiGraph:
    g = nx.MultiDiGraph()
    # class C (module m1) with one method
    g.add_node("py:class:m1.C", type="class", name="m1.C", domain="py")
    g.add_node("py:method:m1.C.do", type="method", name="m1.C.do", domain="py")
    g.add_edge("py:class:m1.C", "py:method:m1.C.do", type="contains")
    # class D (module m1) with one method
    g.add_node("py:class:m1.D", type="class", name="m1.D", domain="py")
    g.add_node("py:method:m1.D.go", type="method", name="m1.D.go", domain="py")
    g.add_edge("py:class:m1.D", "py:method:m1.D.go", type="contains")

    # helper in a DIFFERENT module, called only by C.do -> cross-module candidate
    g.add_node("py:function:m2.helper", type="function", name="m2.helper", domain="py")
    g.add_edge("py:method:m1.C.do", "py:function:m2.helper", type="calls")

    # pure_rule (module m3): called by C.do AND a test -> candidate, test excluded
    g.add_node("py:function:m3.pure_rule", type="function",
               name="m3.pure_rule", domain="py")
    g.add_node("py:function:tests.t.test_it", type="function",
               name="tests.t.test_it", domain="py", is_test=True)
    g.add_edge("py:method:m1.C.do", "py:function:m3.pure_rule", type="calls")
    g.add_edge("py:function:tests.t.test_it", "py:function:m3.pure_rule", type="calls")

    # _priv_rule (module m4): PRIVATE, called by C.do + TWO tests. A private
    # helper used by one class is a genuine relocation signal regardless of
    # test coverage -> must NOT flag as likely_free_primitive.
    g.add_node("py:function:m4._priv_rule", type="function",
               name="m4._priv_rule", domain="py")
    g.add_node("py:function:tests.t.test_p1", type="function",
               name="tests.t.test_p1", domain="py", is_test=True)
    g.add_node("py:function:tests.t.test_p2", type="function",
               name="tests.t.test_p2", domain="py", is_test=True)
    g.add_edge("py:method:m1.C.do", "py:function:m4._priv_rule", type="calls")
    g.add_edge("py:function:tests.t.test_p1", "py:function:m4._priv_rule", type="calls")
    g.add_edge("py:function:tests.t.test_p2", "py:function:m4._priv_rule", type="calls")

    # shared (module m1): called by methods of TWO classes -> NOT a candidate
    g.add_node("py:function:m1.shared", type="function", name="m1.shared", domain="py")
    g.add_edge("py:method:m1.C.do", "py:function:m1.shared", type="calls")
    g.add_edge("py:method:m1.D.go", "py:function:m1.shared", type="calls")

    # free_fn: only caller is another free function -> NOT a candidate
    g.add_node("py:function:m1.free_fn", type="function", name="m1.free_fn", domain="py")
    g.add_node("py:function:m1.caller_fn", type="function",
               name="m1.caller_fn", domain="py")
    g.add_edge("py:function:m1.caller_fn", "py:function:m1.free_fn", type="calls")
    return g


def test_finds_cross_module_single_class_candidate():
    by_id = {r.function.id: r for r in GraphQuery(_graph()).native_place_candidates()}

    assert "py:function:m2.helper" in by_id
    helper = by_id["py:function:m2.helper"]
    assert helper.target_class.id == "py:class:m1.C"
    assert helper.cross_module is True
    assert helper.caller_count == 1
    assert helper.excluded_callers == 0

    # multi-class callers and free-function-only callers are NOT candidates
    assert "py:function:m1.shared" not in by_id
    assert "py:function:m1.free_fn" not in by_id


def test_test_callers_excluded_but_reported():
    by_id = {r.function.id: r for r in GraphQuery(_graph()).native_place_candidates()}
    assert "py:function:m3.pure_rule" in by_id
    rule = by_id["py:function:m3.pure_rule"]
    assert rule.target_class.id == "py:class:m1.C"
    assert rule.caller_count == 1       # only the production method
    assert rule.excluded_callers == 1   # the test caller


def test_min_callers_and_cross_module_ranking():
    g = _graph()
    # every candidate has a single considered caller -> min_callers=2 drops all
    assert GraphQuery(g).native_place_candidates(min_callers=2) == []
    # cross-module candidates rank first
    res = GraphQuery(g).native_place_candidates()
    assert res and res[0].cross_module is True


def test_exclude_substring_drops_candidate():
    res = GraphQuery(_graph()).native_place_candidates(exclude=("m2.",))
    assert all(r.function.id != "py:function:m2.helper" for r in res)


def test_likely_free_primitive_flag():
    by_id = {r.function.id: r for r in GraphQuery(_graph()).native_place_candidates()}

    # public, tested at least as much as used in production (1 prod + 1 test)
    assert by_id["py:function:m3.pure_rule"].likely_free_primitive is True
    # public but NOT independently tested (excluded_callers 0 < caller_count 1)
    assert by_id["py:function:m2.helper"].likely_free_primitive is False
    # private helper: never auto-suppressed, even with 2 test callers
    priv = by_id["py:function:m4._priv_rule"]
    assert priv.private is True
    assert priv.excluded_callers == 2
    assert priv.likely_free_primitive is False


def test_genuine_candidates_rank_above_free_primitives():
    res = GraphQuery(_graph()).native_place_candidates()
    ids = [r.function.id for r in res]
    # the tested free-primitive sinks below the genuine relocations
    free = ids.index("py:function:m3.pure_rule")
    assert free > ids.index("py:function:m2.helper")
    assert free > ids.index("py:function:m4._priv_rule")
    # private cross-module helper outranks the public cross-module one
    assert ids.index("py:function:m4._priv_rule") < ids.index("py:function:m2.helper")
