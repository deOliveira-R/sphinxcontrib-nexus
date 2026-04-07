"""Unit tests for KnowledgeGraph (no Sphinx dependency)."""

from sphinxcontrib.nexus.graph import (
    EdgeType,
    GraphEdge,
    GraphNode,
    KnowledgeGraph,
    NodeType,
)


def test_add_node():
    kg = KnowledgeGraph()
    kg.add_node(GraphNode(
        id="py:function:foo",
        type=NodeType.FUNCTION,
        name="foo",
        display_name="foo()",
        domain="py",
        docname="api",
    ))
    assert kg.has_node("py:function:foo")
    attrs = kg.nxgraph.nodes["py:function:foo"]
    assert attrs["type"] == "function"
    assert attrs["name"] == "foo"
    assert attrs["display_name"] == "foo()"


def test_add_node_with_metadata():
    kg = KnowledgeGraph()
    kg.add_node(GraphNode(
        id="math:equation:euler",
        type=NodeType.EQUATION,
        name="euler",
        metadata={"eqno": 42},
    ))
    attrs = kg.nxgraph.nodes["math:equation:euler"]
    assert attrs["eqno"] == 42


def test_add_node_upserts():
    kg = KnowledgeGraph()
    kg.add_node(GraphNode(id="a", type=NodeType.FILE, name="old"))
    kg.add_node(GraphNode(id="a", type=NodeType.FILE, name="new"))
    assert kg.node_count == 1
    assert kg.nxgraph.nodes["a"]["name"] == "new"


def test_add_edge():
    kg = KnowledgeGraph()
    kg.add_node(GraphNode(id="a", type=NodeType.FILE, name="a"))
    kg.add_node(GraphNode(id="b", type=NodeType.FILE, name="b"))
    kg.add_edge(GraphEdge(source="a", target="b", type=EdgeType.CONTAINS))
    assert kg.edge_count == 1
    edges = list(kg.nxgraph.edges("a", data=True, keys=True))
    assert len(edges) == 1
    _, _, _, data = edges[0]
    assert data["type"] == "contains"


def test_add_edge_with_metadata():
    kg = KnowledgeGraph()
    kg.add_node(GraphNode(id="a", type=NodeType.FILE, name="a"))
    kg.add_node(GraphNode(id="b", type=NodeType.FUNCTION, name="b"))
    kg.add_edge(GraphEdge(
        source="a", target="b", type=EdgeType.DOCUMENTS,
        metadata={"reftype": "func", "resolved": True},
    ))
    edges = list(kg.nxgraph.edges("a", data=True, keys=True))
    _, _, _, data = edges[0]
    assert data["reftype"] == "func"
    assert data["resolved"] is True


def test_multi_edges():
    """MultiDiGraph allows multiple edges between same pair."""
    kg = KnowledgeGraph()
    kg.add_node(GraphNode(id="a", type=NodeType.FILE, name="a"))
    kg.add_node(GraphNode(id="b", type=NodeType.FUNCTION, name="b"))
    kg.add_edge(GraphEdge(source="a", target="b", type=EdgeType.CONTAINS))
    kg.add_edge(GraphEdge(source="a", target="b", type=EdgeType.DOCUMENTS))
    assert kg.edge_count == 2


def test_has_node():
    kg = KnowledgeGraph()
    assert not kg.has_node("x")
    kg.add_node(GraphNode(id="x", type=NodeType.FILE, name="x"))
    assert kg.has_node("x")


def test_counts():
    kg = KnowledgeGraph()
    assert kg.node_count == 0
    assert kg.edge_count == 0
    kg.add_node(GraphNode(id="a", type=NodeType.FILE, name="a"))
    kg.add_node(GraphNode(id="b", type=NodeType.FILE, name="b"))
    kg.add_edge(GraphEdge(source="a", target="b", type=EdgeType.CONTAINS))
    assert kg.node_count == 2
    assert kg.edge_count == 1


def test_enum_stored_as_string():
    """Enum values must be stored as strings for serialization."""
    kg = KnowledgeGraph()
    kg.add_node(GraphNode(id="n", type=NodeType.FUNCTION, name="n"))
    kg.add_edge(GraphEdge(source="n", target="n", type=EdgeType.REFERENCES))
    assert isinstance(kg.nxgraph.nodes["n"]["type"], str)
    edge_data = next(iter(kg.nxgraph.edges("n", data=True, keys=True)))[3]
    assert isinstance(edge_data["type"], str)
