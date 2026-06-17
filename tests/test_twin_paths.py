"""twin_paths — independent implementations of the same computation."""
from __future__ import annotations

import networkx as nx

from sphinxcontrib.nexus.query import GraphQuery

# Shingle-hash sets; only their overlap matters, not the values.
TWIN = list(range(100, 140))          # 40 shingles
NEAR = list(range(105, 145))          # overlaps TWIN by 35/45 -> jaccard 0.777
FAR = list(range(900, 940))           # disjoint from TWIN


def _graph() -> nx.MultiDiGraph:
    g = nx.MultiDiGraph()

    def fn(nid, name, shingles, ntok=60, is_test=False):
        g.add_node(nid, type="function", name=name, domain="py",
                   body_shingles=list(shingles), body_ntokens=ntok, is_test=is_test)

    fn("py:function:m1.alpha", "m1.alpha", TWIN)     # cross-module twin of beta
    fn("py:function:m2.beta", "m2.beta", TWIN)
    fn("py:function:m1.delta", "m1.delta", NEAR)     # same-module near-twin of alpha
    fn("py:function:m3.gamma", "m3.gamma", FAR)      # unrelated
    return g


def _pairs(res):
    return {frozenset((r.a.id, r.b.id)) for r in res}


def test_identical_bodies_flagged_cross_module():
    res = GraphQuery(_graph()).twin_paths(min_similarity=0.9)
    twin = frozenset(("py:function:m1.alpha", "py:function:m2.beta"))
    assert twin in _pairs(res)
    r = next(r for r in res if {r.a.id, r.b.id} == set(twin))
    assert r.similarity == 1.0
    assert r.cross_module is True


def test_dissimilar_not_flagged():
    res = GraphQuery(_graph()).twin_paths(min_similarity=0.5)
    assert all("py:function:m3.gamma" not in p for p in _pairs(res))


def test_min_similarity_threshold():
    g = _graph()
    near = {"py:function:m1.alpha", "py:function:m1.delta"}
    lo = GraphQuery(g).twin_paths(min_similarity=0.5)
    hi = GraphQuery(g).twin_paths(min_similarity=0.99)
    assert len(hi) <= len(lo)
    assert any({r.a.id, r.b.id} == near for r in lo)   # 0.777 passes at 0.5
    assert all({r.a.id, r.b.id} != near for r in hi)   # ...drops at 0.99


def test_same_module_pair_not_cross_module():
    res = GraphQuery(_graph()).twin_paths(min_similarity=0.5)
    near = {"py:function:m1.alpha", "py:function:m1.delta"}
    r = next(r for r in res if {r.a.id, r.b.id} == near)
    assert r.cross_module is False


def test_min_tokens_filters_thin_bodies():
    g = _graph()
    g.add_node("py:function:m9.thin", type="function", name="m9.thin", domain="py",
               body_shingles=list(TWIN), body_ntokens=5)
    res = GraphQuery(g).twin_paths(min_similarity=0.9, min_tokens=35)
    assert all("m9.thin" not in r.a.id and "m9.thin" not in r.b.id for r in res)


def test_is_test_and_exclude_dropped():
    g = _graph()
    g.add_node("py:function:tests.t.test_twin", type="function",
               name="tests.t.test_twin", domain="py",
               body_shingles=list(TWIN), body_ntokens=60, is_test=True)
    g.add_node("py:function:scratch.s.probe", type="function",
               name="scratch.s.probe", domain="py",
               body_shingles=list(TWIN), body_ntokens=60)
    res = GraphQuery(g).twin_paths(min_similarity=0.9, exclude=("scratch",))
    flat = {r.a.id for r in res} | {r.b.id for r in res}
    assert "py:function:tests.t.test_twin" not in flat   # is_test flag
    assert "py:function:scratch.s.probe" not in flat      # exclude substring


def test_direct_call_suppressed():
    g = _graph()
    # alpha delegates to beta -> not an independent reimplementation
    g.add_edge("py:function:m1.alpha", "py:function:m2.beta", key=0, type="calls")
    res = GraphQuery(g).twin_paths(min_similarity=0.9)
    twin = {"py:function:m1.alpha", "py:function:m2.beta"}
    assert all({r.a.id, r.b.id} != twin for r in res)
