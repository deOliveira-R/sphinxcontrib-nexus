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
