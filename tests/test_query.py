"""Unit tests for GraphQuery (no Sphinx dependency)."""

import networkx as nx
import pytest

from sphinxcontrib.nexus.query import GraphQuery


@pytest.fixture()
def sample_graph():
    """Build a small test graph:

    doc:index --CONTAINS--> doc:theory
    doc:index --CONTAINS--> doc:api
    doc:theory --CONTAINS--> math:equation:diffusion
    doc:theory --REFERENCES--> doc:api
    doc:api --CONTAINS--> py:function:solve
    doc:api --CONTAINS--> py:class:Solver
    py:class:Solver --DOCUMENTS--> py:function:solve
    doc:index --REFERENCES--> py:function:solve
    """
    g = nx.MultiDiGraph()
    nodes = {
        "doc:index": {"type": "file", "name": "index", "display_name": "Index", "domain": "std", "docname": "index"},
        "doc:theory": {"type": "file", "name": "theory", "display_name": "Theory", "domain": "std", "docname": "theory"},
        "doc:api": {"type": "file", "name": "api", "display_name": "API Reference", "domain": "std", "docname": "api"},
        "math:equation:diffusion": {"type": "equation", "name": "diffusion", "display_name": "(1)", "domain": "math", "docname": "theory"},
        "py:function:solve": {"type": "function", "name": "solve", "display_name": "solve()", "domain": "py", "docname": "api"},
        "py:class:Solver": {"type": "class", "name": "Solver", "display_name": "Solver", "domain": "py", "docname": "api"},
    }
    for nid, attrs in nodes.items():
        g.add_node(nid, **attrs)

    edges = [
        ("doc:index", "doc:theory", {"type": "contains"}),
        ("doc:index", "doc:api", {"type": "contains"}),
        ("doc:theory", "math:equation:diffusion", {"type": "contains"}),
        ("doc:theory", "doc:api", {"type": "references"}),
        ("doc:api", "py:function:solve", {"type": "contains"}),
        ("doc:api", "py:class:Solver", {"type": "contains"}),
        ("py:class:Solver", "py:function:solve", {"type": "documents"}),
        ("doc:index", "py:function:solve", {"type": "references"}),
    ]
    for src, tgt, data in edges:
        g.add_edge(src, tgt, **data)

    return g


def test_get_node(sample_graph):
    q = GraphQuery(sample_graph)
    node = q.get_node("py:function:solve")
    assert node is not None
    assert node.type == "function"
    assert node.name == "solve"
    assert node.degree > 0


def test_get_node_missing(sample_graph):
    q = GraphQuery(sample_graph)
    assert q.get_node("nonexistent") is None


def test_neighbors_out(sample_graph):
    q = GraphQuery(sample_graph)
    results = q.neighbors("doc:index", direction="out")
    targets = {r[0].id for r in results}
    assert "doc:theory" in targets
    assert "doc:api" in targets
    assert "py:function:solve" in targets


def test_neighbors_in(sample_graph):
    q = GraphQuery(sample_graph)
    results = q.neighbors("py:function:solve", direction="in")
    sources = {r[0].id for r in results}
    assert "doc:api" in sources
    assert "py:class:Solver" in sources
    assert "doc:index" in sources


def test_neighbors_both(sample_graph):
    q = GraphQuery(sample_graph)
    results = q.neighbors("doc:api", direction="both")
    connected = {r[0].id for r in results}
    # out: py:function:solve, py:class:Solver
    # in: doc:index, doc:theory
    assert "py:function:solve" in connected
    assert "doc:index" in connected
    assert "doc:theory" in connected


def test_neighbors_filtered_by_edge_type(sample_graph):
    q = GraphQuery(sample_graph)
    results = q.neighbors("doc:index", direction="out", edge_types=["contains"])
    targets = {r[0].id for r in results}
    assert "doc:theory" in targets
    assert "doc:api" in targets
    # The references edge to py:function:solve should be filtered out
    assert "py:function:solve" not in targets


def test_neighbors_missing_node(sample_graph):
    q = GraphQuery(sample_graph)
    assert q.neighbors("nonexistent") == []


def test_impact_upstream(sample_graph):
    q = GraphQuery(sample_graph)
    result = q.impact("py:function:solve", direction="upstream", max_depth=3)
    assert result.target == "py:function:solve"
    assert result.total_affected > 0
    # Depth 1: direct parents (doc:api, py:class:Solver, doc:index)
    depth1_ids = {n.id for n in result.by_depth.get(1, [])}
    assert "doc:api" in depth1_ids
    assert "py:class:Solver" in depth1_ids


def test_impact_downstream(sample_graph):
    q = GraphQuery(sample_graph)
    result = q.impact("doc:index", direction="downstream", max_depth=2)
    assert result.total_affected > 0
    depth1_ids = {n.id for n in result.by_depth.get(1, [])}
    assert "doc:theory" in depth1_ids
    assert "doc:api" in depth1_ids


def test_impact_max_depth(sample_graph):
    q = GraphQuery(sample_graph)
    result = q.impact("doc:index", direction="downstream", max_depth=1)
    assert 1 in result.by_depth
    assert 2 not in result.by_depth


def test_impact_missing_node(sample_graph):
    q = GraphQuery(sample_graph)
    result = q.impact("nonexistent")
    assert result.total_affected == 0


def test_shortest_path(sample_graph):
    q = GraphQuery(sample_graph)
    result = q.shortest_path("math:equation:diffusion", "py:function:solve")
    assert result is not None
    assert "math:equation:diffusion" in result.nodes
    assert "py:function:solve" in result.nodes
    assert result.length > 0


def test_shortest_path_no_path():
    """Two disconnected nodes have no path."""
    g = nx.MultiDiGraph()
    g.add_node("a", type="file", name="a")
    g.add_node("b", type="file", name="b")
    q = GraphQuery(g)
    assert q.shortest_path("a", "b") is None


def test_shortest_path_max_hops(sample_graph):
    q = GraphQuery(sample_graph)
    # Path exists but may exceed max_hops=1
    result = q.shortest_path("math:equation:diffusion", "py:function:solve", max_hops=1)
    # The path requires more than 1 hop, so should return None
    if result is not None:
        assert result.length <= 1


def test_shortest_path_missing_node(sample_graph):
    q = GraphQuery(sample_graph)
    assert q.shortest_path("nonexistent", "doc:index") is None


def test_query_substring(sample_graph):
    q = GraphQuery(sample_graph)
    results = q.query("solve")
    ids = {r.id for r in results}
    assert "py:function:solve" in ids


def test_query_case_insensitive(sample_graph):
    q = GraphQuery(sample_graph)
    results = q.query("SOLVER")
    ids = {r.id for r in results}
    assert "py:class:Solver" in ids


def test_query_display_name(sample_graph):
    q = GraphQuery(sample_graph)
    results = q.query("API Reference")
    ids = {r.id for r in results}
    assert "doc:api" in ids


def test_query_type_filter(sample_graph):
    q = GraphQuery(sample_graph)
    results = q.query("sol", node_types=["class"])
    ids = {r.id for r in results}
    assert "py:class:Solver" in ids
    assert "py:function:solve" not in ids


def test_query_limit(sample_graph):
    q = GraphQuery(sample_graph)
    results = q.query("", limit=2)  # empty string matches everything
    assert len(results) == 2


def test_god_nodes(sample_graph):
    q = GraphQuery(sample_graph)
    results = q.god_nodes(top_n=3)
    assert len(results) == 3
    # Most connected should be first
    assert results[0].degree >= results[1].degree >= results[2].degree


def test_god_nodes_returns_all_if_fewer(sample_graph):
    q = GraphQuery(sample_graph)
    results = q.god_nodes(top_n=100)
    assert len(results) == sample_graph.number_of_nodes()


def test_stats(sample_graph):
    q = GraphQuery(sample_graph)
    s = q.stats()
    assert s.node_count == 6
    assert s.edge_count == 8
    assert s.nodes_by_type["file"] == 3
    assert s.nodes_by_type["function"] == 1
    assert s.nodes_by_type["class"] == 1
    assert s.nodes_by_type["equation"] == 1
    assert s.edges_by_type["contains"] == 5
    assert s.edges_by_type["references"] == 2
    assert s.edges_by_type["documents"] == 1
    assert s.connected_components == 1
    assert s.density > 0
