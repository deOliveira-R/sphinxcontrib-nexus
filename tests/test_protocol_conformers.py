"""protocol_conformers — undeclared structural Protocol conformers."""
from __future__ import annotations

import itertools

import networkx as nx

from sphinxcontrib.nexus.query import GraphQuery

_key = itertools.count()


def _graph() -> nx.MultiDiGraph:
    g = nx.MultiDiGraph()

    def cls(name, is_test=False):
        g.add_node(f"py:class:{name}", type="class", name=name,
                   domain="py", is_test=is_test)

    def meth(cls_name, mname):
        mid = f"py:method:{cls_name}.{mname}"
        g.add_node(mid, type="method", name=f"{cls_name}.{mname}", domain="py")
        g.add_edge(f"py:class:{cls_name}", mid, key=next(_key), type="contains")

    def inherit(child, parent):
        g.add_edge(f"py:class:{child}", f"py:class:{parent}",
                   key=next(_key), type="inherits")

    # P is a Protocol with {apply, solve}
    g.add_node("py:class:typing.Protocol", type="class",
               name="typing.Protocol", domain="py")
    cls("m.P")
    inherit("m.P", "typing.Protocol")
    meth("m.P", "apply")
    meth("m.P", "solve")

    cls("m.C")                        # conformer: apply + solve, no inherit
    meth("m.C", "apply")
    meth("m.C", "solve")

    cls("m.D")                        # declared: inherits P
    meth("m.D", "apply")
    meth("m.D", "solve")
    inherit("m.D", "m.P")

    cls("m.E")                        # incomplete: only apply
    meth("m.E", "apply")

    cls("m.B")                        # B inherits P (transitive base)
    inherit("m.B", "m.P")
    cls("m.F")                        # F has the methods AND inherits B -> declared via B
    meth("m.F", "apply")
    meth("m.F", "solve")
    inherit("m.F", "m.B")
    return g


def test_undeclared_conformer_found():
    res = GraphQuery(_graph()).protocol_conformers()
    p = next(r for r in res if r.protocol.id == "py:class:m.P")
    ids = {c.id for c in p.conformers}
    assert "py:class:m.C" in ids           # structural conformer, undeclared
    assert sorted(p.methods) == ["apply", "solve"]


def test_declared_subclass_excluded():
    res = GraphQuery(_graph()).protocol_conformers()
    ids = {c.id for r in res for c in r.conformers}
    assert "py:class:m.D" not in ids       # directly inherits P
    assert "py:class:m.F" not in ids       # inherits P transitively via B


def test_incomplete_class_not_conformer():
    res = GraphQuery(_graph()).protocol_conformers()
    ids = {c.id for r in res for c in r.conformers}
    assert "py:class:m.E" not in ids       # missing 'solve'


def test_min_methods_threshold():
    assert GraphQuery(_graph()).protocol_conformers(min_methods=3) == []


def test_exclude_and_is_test():
    g = _graph()
    g.add_node("py:class:tests.t.FakeOp", type="class",
               name="tests.t.FakeOp", domain="py", is_test=True)
    g.add_node("py:method:tests.t.FakeOp.apply", type="method",
               name="tests.t.FakeOp.apply", domain="py")
    g.add_node("py:method:tests.t.FakeOp.solve", type="method",
               name="tests.t.FakeOp.solve", domain="py")
    g.add_edge("py:class:tests.t.FakeOp", "py:method:tests.t.FakeOp.apply",
               key=next(_key), type="contains")
    g.add_edge("py:class:tests.t.FakeOp", "py:method:tests.t.FakeOp.solve",
               key=next(_key), type="contains")
    res = GraphQuery(g).protocol_conformers()
    ids = {c.id for r in res for c in r.conformers}
    assert "py:class:tests.t.FakeOp" not in ids
