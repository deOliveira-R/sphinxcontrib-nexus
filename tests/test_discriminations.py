"""discriminations — tags discriminated at multiple sites (missing types)."""
from __future__ import annotations

import networkx as nx

from sphinxcontrib.nexus.query import GraphQuery


def _graph() -> nx.MultiDiGraph:
    g = nx.MultiDiGraph()

    def fn(name, is_test=False):
        g.add_node(f"py:function:{name}", type="function", name=name,
                   domain="py", is_test=is_test)

    def tag(name):
        g.add_node(f"py:tag:{name}", type="tag", name=name, domain="py")

    def disc(fn_name, tag_name, cases, key):
        g.add_edge(f"py:function:{fn_name}", f"py:tag:{tag_name}",
                   key=key, type="discriminates_on", cases=tuple(cases))

    for nm in ("m.a", "m.b", "m.c"):
        fn(nm)
    tag("geometry")
    tag("kind")
    disc("m.a", "geometry", ["spherical"], 0)
    disc("m.b", "geometry", ["cylindrical"], 1)
    disc("m.c", "geometry", ["cartesian"], 2)
    disc("m.a", "kind", ["x"], 3)        # single site — below default min_sites
    return g


def test_multi_site_tag_surfaces():
    res = GraphQuery(_graph()).discriminations()
    geo = next(r for r in res if r.tag == "geometry")
    assert geo.site_count == 3
    assert set(geo.cases) == {"spherical", "cylindrical", "cartesian"}
    assert len(geo.sites) == 3


def test_single_site_below_threshold_dropped():
    res = GraphQuery(_graph()).discriminations(min_sites=2)
    assert all(r.tag != "kind" for r in res)


def test_min_sites_param():
    assert GraphQuery(_graph()).discriminations(min_sites=4) == []


def test_is_test_and_exclude_dropped():
    g = _graph()
    g.add_node("py:function:tests.t.test_geo", type="function",
               name="tests.t.test_geo", domain="py", is_test=True)
    g.add_edge("py:function:tests.t.test_geo", "py:tag:geometry",
               key=9, type="discriminates_on", cases=("spherical",))
    g.add_node("py:function:scratch.s.probe", type="function",
               name="scratch.s.probe", domain="py")
    g.add_edge("py:function:scratch.s.probe", "py:tag:geometry",
               key=10, type="discriminates_on", cases=("x",))
    res = GraphQuery(g).discriminations(exclude=("scratch",))
    geo = next(r for r in res if r.tag == "geometry")
    site_ids = {s.id for s in geo.sites}
    assert "py:function:tests.t.test_geo" not in site_ids   # is_test flag
    assert "py:function:scratch.s.probe" not in site_ids    # exclude substring
    assert geo.site_count == 3
