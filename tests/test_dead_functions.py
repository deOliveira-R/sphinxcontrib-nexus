"""dead_functions — functions/methods with no static callers."""
from __future__ import annotations

import networkx as nx

from sphinxcontrib.nexus.query import GraphQuery


def _graph() -> nx.MultiDiGraph:
    g = nx.MultiDiGraph()

    def fn(nid, name, ntype="function", decorators=None, is_test=False):
        attrs = dict(type=ntype, name=name, domain="py", is_test=is_test)
        if decorators:
            attrs["decorators"] = decorators
        g.add_node(nid, **attrs)

    fn("py:function:m.caller", "m.caller")
    fn("py:function:m.live", "m.live")                  # called by caller
    fn("py:function:m._dead", "m._dead")                # private, no caller
    fn("py:function:m.public_dead", "m.public_dead")    # public, no caller
    fn("py:function:m._decorated", "m._decorated", decorators=("property",))
    fn("py:method:m.C.__init__", "m.C.__init__", ntype="method")   # dunder
    fn("py:function:m._test_only", "m._test_only")      # only a test caller
    fn("py:function:tests.t.test_x", "tests.t.test_x", is_test=True)

    g.add_edge("py:function:m.caller", "py:function:m.live", key=0, type="calls")
    g.add_edge("py:function:tests.t.test_x", "py:function:m._test_only",
               key=1, type="calls")
    return g


def _by_id(res):
    return {r.function.id: r for r in res}


def test_called_function_not_dead():
    assert "py:function:m.live" not in _by_id(GraphQuery(_graph()).dead_functions())


def test_zero_caller_function_is_dead_with_flags():
    by = _by_id(GraphQuery(_graph()).dead_functions())
    dead = by["py:function:m._dead"]
    assert dead.public is False
    assert dead.decorated is False
    assert dead.is_method is False
    assert by["py:function:m.public_dead"].public is True
    assert by["py:function:m._decorated"].decorated is True


def test_dunder_excluded():
    assert "py:method:m.C.__init__" not in _by_id(GraphQuery(_graph()).dead_functions())


def test_test_only_caller_counts_as_dead():
    by = _by_id(GraphQuery(_graph()).dead_functions())
    assert "py:function:m._test_only" in by          # caller is is_test
    assert "py:function:tests.t.test_x" not in by     # the test itself is dropped


def test_private_undecorated_ranked_first():
    res = GraphQuery(_graph()).dead_functions()
    assert res[0].public is False
    assert res[0].decorated is False


def test_exclude_drops_function_and_caller():
    # excluding the caller leaves m.live with no non-excluded caller -> dead
    by = _by_id(GraphQuery(_graph()).dead_functions(exclude=("caller",)))
    assert "py:function:m.live" in by
    # excluding a function by substring drops it from results
    by2 = _by_id(GraphQuery(_graph()).dead_functions(exclude=("public_dead",)))
    assert "py:function:m.public_dead" not in by2


# ── a zero that means UNRESOLVABLE, not UNCALLED (#59) ──────────────


def _graph_with_phantoms() -> nx.MultiDiGraph:
    """A live method whose callers all landed on phantoms, plus a
    genuinely uncalled control.

    The resolver mints one node per receiver SPELLING, so `quad.perm()`
    and `q.perm()` become two unresolved nodes and neither is the real
    `Quadrature.perm`. `[M]` on ORPHEUS this is exactly
    `Quadrature.ordinate_permutation`: 0 resolved callers, 40 calls on
    five phantoms named for the caller's local variable.
    """
    g = nx.MultiDiGraph()
    g.add_node("py:method:pkg.Quadrature.perm", type="method",
               name="pkg.Quadrature.perm", domain="py")
    # the control: same shape, nobody calls it by any spelling
    g.add_node("py:method:pkg.Quadrature.unused", type="method",
               name="pkg.Quadrature.unused", domain="py")
    g.add_node("py:function:pkg.site_a", type="function", name="pkg.site_a",
               domain="py")
    g.add_node("py:function:pkg.site_b", type="function", name="pkg.site_b",
               domain="py")

    for spelling, callers in (("quad.perm", ["py:function:pkg.site_a"] * 3),
                              ("q.perm", ["py:function:pkg.site_b"])):
        nid = f"py:function:{spelling}"
        g.add_node(nid, type="unresolved", name=spelling, domain="py")
        for c in callers:
            g.add_edge(c, nid, type="calls")

    # ⚠ Load-bearing: a REAL, resolved method sharing the leaf name
    # `perm`, with a caller of its own. Without it the fixture cannot
    # tell "only placeholders count" from "everything counts" —
    # dropping the placeholder filter reddened 0 of 9 tests until this
    # node existed. A same-named method on an unrelated class is the
    # normal case in any real tree, and counting it would be a false
    # alarm on the one tool whose job is to stop false confidence.
    g.add_node("py:method:other.Grid.perm", type="method",
               name="other.Grid.perm", domain="py")
    g.add_edge("py:function:pkg.site_a", "py:method:other.Grid.perm",
               type="calls")
    return g


def test_an_empty_caller_list_says_when_the_resolver_is_BLIND():
    """[M] ORPHEUS: `Quadrature.ordinate_permutation` reports 0 callers
    while 40 calls to that name sit on five phantoms, and
    `dead_functions` then offers it for deletion. The two honest
    answers — "nothing calls this" and "I cannot see who calls this" —
    call for opposite next steps and printed identically."""
    q = GraphQuery(_graph_with_phantoms())
    r = q.callers("py:method:pkg.Quadrature.perm")

    assert r.total == 0
    assert r.unresolved is not None
    assert r.unresolved.count == 4                       # 3 + 1
    assert r.unresolved.spellings == [
        "py:function:quad.perm", "py:function:q.perm",   # most-called first
    ]
    assert "perm" in r.unresolved.note


def test_a_genuinely_uncalled_symbol_raises_no_false_alarm():
    """The control. A warning that fires on everything is not a
    warning — `unresolved` must be ABSENT when nothing names it."""
    q = GraphQuery(_graph_with_phantoms())
    r = q.callers("py:method:pkg.Quadrature.unused")

    assert r.total == 0
    assert r.unresolved is None


def test_dead_code_candidates_the_resolver_may_have_LOST_rank_last():
    """An unresolved call naming the function is evidence AGAINST
    deleting it, and it outranks every other flag: `public` and
    `decorated` say "being uncalled is expected", this says "it is
    probably called and I could not see it". [M] on ORPHEUS 780 of 2946
    candidates (26 %) carry one."""
    q = GraphQuery(_graph_with_phantoms())
    rows = q.dead_functions(limit=0)
    by_name = {r.function.name: r for r in rows}

    assert by_name["pkg.Quadrature.perm"].unresolved_calls == 4
    assert by_name["pkg.Quadrature.unused"].unresolved_calls == 0
    # the one we may have lost sorts BELOW the one we genuinely did not
    order = [r.function.name for r in rows]
    assert order.index("pkg.Quadrature.unused") < order.index("pkg.Quadrature.perm")
