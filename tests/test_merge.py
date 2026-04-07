"""Unit tests for graph merge."""

from __future__ import annotations

from sphinxcontrib.nexus.graph import (
    EdgeType,
    GraphEdge,
    GraphNode,
    KnowledgeGraph,
    NodeType,
)
from sphinxcontrib.nexus.merge import merge_graphs


def _make_sphinx_graph() -> KnowledgeGraph:
    kg = KnowledgeGraph()
    kg.add_node(GraphNode(
        id="py:function:solver.solve",
        type=NodeType.FUNCTION,
        name="solver.solve",
        display_name="solve()",
        domain="py",
        docname="api/solver",
    ))
    kg.add_node(GraphNode(
        id="doc:api/solver",
        type=NodeType.FILE,
        name="api/solver",
        domain="std",
        docname="api/solver",
    ))
    kg.add_node(GraphNode(
        id="py:class:solver:CPMesh",
        type=NodeType.UNRESOLVED,
        name="CPMesh",
        display_name="CPMesh",
        domain="py",
    ))
    # Edge: doc contains function
    kg.add_edge(GraphEdge(
        source="doc:api/solver",
        target="py:function:solver.solve",
        type=EdgeType.CONTAINS,
    ))
    # Edge: doc references unresolved CPMesh
    kg.add_edge(GraphEdge(
        source="doc:api/solver",
        target="py:class:solver:CPMesh",
        type=EdgeType.DOCUMENTS,
    ))
    return kg


def _make_ast_graph() -> KnowledgeGraph:
    kg = KnowledgeGraph()
    kg.add_node(GraphNode(
        id="py:function:solver.solve",
        type=NodeType.FUNCTION,
        name="solver.solve",
        metadata={"file_path": "solver.py", "lineno": 10, "end_lineno": 20, "source": "ast"},
    ))
    kg.add_node(GraphNode(
        id="py:function:solver._helper",
        type=NodeType.FUNCTION,
        name="solver._helper",
        metadata={"file_path": "solver.py", "lineno": 22, "end_lineno": 25, "source": "ast"},
    ))
    kg.add_node(GraphNode(
        id="py:class:collision_probability.CPMesh",
        type=NodeType.CLASS,
        name="collision_probability.CPMesh",
        metadata={"file_path": "cp.py", "lineno": 5, "source": "ast"},
    ))
    # Edge: solve calls _helper
    kg.add_edge(GraphEdge(
        source="py:function:solver.solve",
        target="py:function:solver._helper",
        type=EdgeType.CALLS,
        metadata={"source": "ast"},
    ))
    return kg


def test_merge_enriches_existing_node():
    sphinx = _make_sphinx_graph()
    ast_g = _make_ast_graph()
    merged = merge_graphs(sphinx, ast_g)
    attrs = merged.nxgraph.nodes["py:function:solver.solve"]
    # Should have Sphinx attrs
    assert attrs["docname"] == "api/solver"
    # Should have AST metadata added
    assert attrs["lineno"] == 10
    assert attrs["source"] == "both"


def test_merge_adds_ast_only_node():
    sphinx = _make_sphinx_graph()
    ast_g = _make_ast_graph()
    merged = merge_graphs(sphinx, ast_g)
    assert "py:function:solver._helper" in merged.nxgraph
    attrs = merged.nxgraph.nodes["py:function:solver._helper"]
    assert attrs["source"] == "ast_only"


def test_merge_reconciles_unresolved():
    sphinx = _make_sphinx_graph()
    ast_g = _make_ast_graph()
    merged = merge_graphs(sphinx, ast_g)
    # UNRESOLVED CPMesh should be gone
    assert "py:class:solver:CPMesh" not in merged.nxgraph
    # Concrete node should exist
    assert "py:class:collision_probability.CPMesh" in merged.nxgraph
    # Edge should be retargeted
    edge_targets = [
        t for _, t, d in merged.nxgraph.edges(data=True)
        if d.get("type") == "documents"
    ]
    assert "py:class:collision_probability.CPMesh" in edge_targets


def test_merge_preserves_sphinx_edges():
    sphinx = _make_sphinx_graph()
    ast_g = _make_ast_graph()
    merged = merge_graphs(sphinx, ast_g)
    contains = [
        (s, t) for s, t, d in merged.nxgraph.edges(data=True)
        if d.get("type") == "contains"
    ]
    assert ("doc:api/solver", "py:function:solver.solve") in contains


def test_merge_adds_ast_edges():
    sphinx = _make_sphinx_graph()
    ast_g = _make_ast_graph()
    merged = merge_graphs(sphinx, ast_g)
    calls = [
        (s, t) for s, t, d in merged.nxgraph.edges(data=True)
        if d.get("type") == "calls"
    ]
    assert ("py:function:solver.solve", "py:function:solver._helper") in calls
