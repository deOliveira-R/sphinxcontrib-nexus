"""Unit tests for graph merge."""

from __future__ import annotations

from sphinxcontrib.nexus.graph import (
    EdgeType,
    GraphEdge,
    GraphNode,
    KnowledgeGraph,
    NodeType,
)
from sphinxcontrib.nexus.merge import (
    _infer_implements,
    merge_graphs,
    write_verifies_edges,
)


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


# ---------------------------------------------------------------------------
# write_verifies_edges
# ---------------------------------------------------------------------------


def _graph_with_equation_and_test(verifies: tuple[str, ...]) -> KnowledgeGraph:
    """Build a minimal KG with one equation node and one test function
    tagged ``@pytest.mark.verifies(<labels>)``."""
    kg = KnowledgeGraph()
    kg.add_node(GraphNode(
        id="math:equation:eq-1",
        type=NodeType.EQUATION,
        name="eq-1",
        display_name="eq-1",
        domain="math",
        metadata={"docname": "theory/solver"},
    ))
    kg.add_node(GraphNode(
        id="py:function:tests.test_solver.test_attenuation",
        type=NodeType.FUNCTION,
        name="tests.test_solver.test_attenuation",
        display_name="test_attenuation",
        domain="py",
        metadata={"is_test": True, "verifies": verifies, "vv_level": "L0"},
    ))
    return kg


def test_write_verifies_edges_writes_tests_edge():
    kg = _graph_with_equation_and_test(("eq-1",))
    count = write_verifies_edges(kg.nxgraph)
    assert count == 1
    edges = [
        (s, t, d.get("source"))
        for s, t, d in kg.nxgraph.edges(data=True)
        if d.get("type") == EdgeType.TESTS.value
    ]
    assert (
        "py:function:tests.test_solver.test_attenuation",
        "math:equation:eq-1",
        "pytest.mark.verifies",
    ) in edges


def test_write_verifies_edges_skips_missing_equation(caplog):
    kg = _graph_with_equation_and_test(("eq-missing",))
    count = write_verifies_edges(kg.nxgraph)
    assert count == 0
    # No phantom equation node gets created.
    assert "math:equation:eq-missing" not in kg.nxgraph


def test_write_verifies_edges_is_idempotent():
    kg = _graph_with_equation_and_test(("eq-1",))
    first = write_verifies_edges(kg.nxgraph)
    second = write_verifies_edges(kg.nxgraph)
    assert first == 1
    assert second == 0  # no duplicates on re-run
    tests_edges = [
        (s, t)
        for s, t, d in kg.nxgraph.edges(data=True)
        if d.get("type") == EdgeType.TESTS.value
    ]
    assert len(tests_edges) == 1


# ---------------------------------------------------------------------------
# _infer_implements guard against duplication
# ---------------------------------------------------------------------------


def test_infer_implements_skips_explicit_tests_edge():
    """Given a pre-existing ``pytest.mark.verifies``-sourced TESTS
    edge, the token-intersection heuristic must NOT add a duplicate
    inferred IMPLEMENTS edge for the same (code, equation) pair."""
    kg = KnowledgeGraph()
    kg.add_node(GraphNode(
        id="doc:theory/transport",
        type=NodeType.FILE,
        name="theory/transport",
        domain="std",
        docname="theory/transport",
    ))
    kg.add_node(GraphNode(
        id="math:equation:transport-cartesian",
        type=NodeType.EQUATION,
        name="transport-cartesian",
        display_name="transport-cartesian",
        domain="math",
        metadata={"docname": "theory/transport"},
    ))
    kg.add_node(GraphNode(
        id="py:function:solver.solve_transport_cartesian",
        type=NodeType.FUNCTION,
        name="solver.solve_transport_cartesian",
        display_name="solve_transport_cartesian",
        domain="py",
    ))
    # Doc contains the equation and documents the function — this is
    # what would otherwise trigger the inferred implements edge.
    kg.add_edge(GraphEdge(
        source="doc:theory/transport",
        target="math:equation:transport-cartesian",
        type=EdgeType.CONTAINS,
    ))
    kg.add_edge(GraphEdge(
        source="doc:theory/transport",
        target="py:function:solver.solve_transport_cartesian",
        type=EdgeType.DOCUMENTS,
    ))
    # Pre-existing explicit TESTS edge (as if from a different test
    # node, or from write_verifies_edges — the guard should not care).
    kg.nxgraph.add_edge(
        "py:function:solver.solve_transport_cartesian",
        "math:equation:transport-cartesian",
        type="tests",
        source="pytest.mark.verifies",
        confidence=1.0,
    )

    _infer_implements(kg.nxgraph)

    edges_between = kg.nxgraph.get_edge_data(
        "py:function:solver.solve_transport_cartesian",
        "math:equation:transport-cartesian",
    )
    types = [d.get("type") for d in edges_between.values()]
    # The TESTS edge must still be there…
    assert "tests" in types
    # …and no duplicate inferred IMPLEMENTS edge should have been added.
    assert "implements" not in types


def test_merge_upgrades_placeholder_type_from_ast():
    """Regression for nexus#3 round 2 (0.8.2 cross-validation).

    When the Sphinx side has a placeholder ``py:class:pkg.mod.Thing``
    with ``type=unresolved`` (created by a pending_xref that
    couldn't resolve at parse time, or by NetworkX auto-creating
    the target of an edge before domain extraction ran) and the
    AST side has the same id typed as ``class`` with a
    ``file_path`` and ``lineno``, the merge step must upgrade the
    merged node's type from ``unresolved`` to ``class``.

    Before this fix the merged node kept ``type=unresolved``,
    which broke downstream type filters and made
    ``_canonicalize_phantoms`` refuse to treat the canonical as a
    fold target — the leaf_index skipped any phantom-typed node.
    """
    sphinx = KnowledgeGraph()
    # Sphinx-side placeholder — the bug shape.
    sphinx.add_node(GraphNode(
        id="py:class:pkg.mod.Thing",
        type=NodeType.UNRESOLVED,
        name="pkg.mod.Thing",
        display_name="Thing",
        domain="py",
    ))
    ast_g = KnowledgeGraph()
    ast_g.add_node(GraphNode(
        id="py:class:pkg.mod.Thing",
        type=NodeType.CLASS,
        name="pkg.mod.Thing",
        display_name="Thing",
        domain="py",
        metadata={
            "file_path": "/project/pkg/mod.py",
            "lineno": 42,
            "end_lineno": 50,
        },
    ))

    merged = merge_graphs(sphinx, ast_g)
    node = merged.nxgraph.nodes["py:class:pkg.mod.Thing"]
    assert node["type"] == "class", node
    assert node["file_path"] == "/project/pkg/mod.py"
    assert node["lineno"] == 42
    assert node["source"] == "both"


def test_merge_does_not_downgrade_concrete_type():
    """Inverse test: when the Sphinx side already has a concrete
    type (``class`` from autodoc) and the AST side happens to
    report a weaker type (shouldn't happen in practice, but guard
    against it), the merge must NOT regress the type."""
    sphinx = KnowledgeGraph()
    sphinx.add_node(GraphNode(
        id="py:class:pkg.mod.Thing",
        type=NodeType.CLASS,
        name="pkg.mod.Thing",
        display_name="Thing",
        domain="py",
        docname="api/mod",
    ))
    ast_g = KnowledgeGraph()
    ast_g.add_node(GraphNode(
        id="py:class:pkg.mod.Thing",
        type=NodeType.UNRESOLVED,
        name="pkg.mod.Thing",
        display_name="Thing",
        domain="py",
    ))

    merged = merge_graphs(sphinx, ast_g)
    assert merged.nxgraph.nodes["py:class:pkg.mod.Thing"]["type"] == "class"


def test_infer_implements_still_fires_without_explicit_edge():
    """Sanity check: the guard must not break the normal flow."""
    kg = KnowledgeGraph()
    kg.add_node(GraphNode(
        id="doc:theory/transport",
        type=NodeType.FILE,
        name="theory/transport",
        domain="std",
        docname="theory/transport",
    ))
    kg.add_node(GraphNode(
        id="math:equation:transport-cartesian",
        type=NodeType.EQUATION,
        name="transport-cartesian",
        display_name="transport-cartesian",
        domain="math",
        metadata={"docname": "theory/transport"},
    ))
    kg.add_node(GraphNode(
        id="py:function:solver.solve_transport_cartesian",
        type=NodeType.FUNCTION,
        name="solver.solve_transport_cartesian",
        display_name="solve_transport_cartesian",
        domain="py",
    ))
    kg.add_edge(GraphEdge(
        source="doc:theory/transport",
        target="math:equation:transport-cartesian",
        type=EdgeType.CONTAINS,
    ))
    kg.add_edge(GraphEdge(
        source="doc:theory/transport",
        target="py:function:solver.solve_transport_cartesian",
        type=EdgeType.DOCUMENTS,
    ))

    _infer_implements(kg.nxgraph)

    edges = kg.nxgraph.get_edge_data(
        "py:function:solver.solve_transport_cartesian",
        "math:equation:transport-cartesian",
    )
    types = [d.get("type") for d in edges.values()]
    assert "implements" in types
