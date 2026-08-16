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
    reconcile_unresolved,
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
        id="std:file:api/solver",
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
        source="std:file:api/solver",
        target="py:function:solver.solve",
        type=EdgeType.CONTAINS,
    ))
    # Edge: doc references unresolved CPMesh
    kg.add_edge(GraphEdge(
        source="std:file:api/solver",
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
    reconcile_unresolved(merged)
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
    assert ("std:file:api/solver", "py:function:solver.solve") in contains


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
        id="std:file:theory/transport",
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
        source="std:file:theory/transport",
        target="math:equation:transport-cartesian",
        type=EdgeType.CONTAINS,
    ))
    kg.add_edge(GraphEdge(
        source="std:file:theory/transport",
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
        id="std:file:theory/transport",
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
        source="std:file:theory/transport",
        target="math:equation:transport-cartesian",
        type=EdgeType.CONTAINS,
    ))
    kg.add_edge(GraphEdge(
        source="std:file:theory/transport",
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


# ---------------------------------------------------------------------------
# Ambiguous short names must not be reconciled by iteration order
# ---------------------------------------------------------------------------
#
# The unresolved-reconciliation index used to be a plain dict:
#
#     ast_by_short_name[short] = node_id
#
# so when several nodes shared a short name, whichever the walk reached
# LAST silently became the answer. Measured on ORPHEUS: a docstring's
# bare ``:mod:`derivations``` — whose leaf matches `orpheus.derivations`,
# `tests.derivations`, and a sibling `...origins.derivations`, all real —
# bound to the TEST package. The dead-reference count went *down*,
# because a visible unknown had become a silent misattribution.
#
# There is no symptom to notice downstream: the edge looks legitimate and
# points at a node that exists. Only refusing to guess makes it visible.


def _ambiguous_pair() -> tuple[KnowledgeGraph, KnowledgeGraph]:
    sphinx = KnowledgeGraph()
    sphinx.add_node(GraphNode(id="std:file:page", type=NodeType.FILE, name="page",
                              display_name="page", domain="std"))
    sphinx.add_node(GraphNode(
        id="py:module:derivations", type=NodeType.UNRESOLVED,
        name="derivations", display_name="derivations", domain="py",
    ))
    sphinx.add_edge(GraphEdge(source="std:file:page", target="py:module:derivations",
                              type=EdgeType.REFERENCES))

    ast_g = KnowledgeGraph()
    for full in ("pkg.derivations", "tests.derivations",
                 "pkg.deep.origins.derivations"):
        ast_g.add_node(GraphNode(
            id=f"py:module:{full}", type=NodeType.MODULE, name=full,
            display_name="derivations", domain="py",
            metadata={"file_path": f"/{full.replace('.', '/')}/__init__.py"},
        ))
    return sphinx, ast_g


def test_ambiguous_short_name_is_left_unresolved():
    merged = merge_graphs(*_ambiguous_pair())
    reconcile_unresolved(merged)
    g = merged.nxgraph
    assert "py:module:derivations" in g, (
        "an ambiguous short name must stay unresolved and be reported, "
        "not be folded onto an arbitrary same-leaf candidate"
    )
    targets = {t for _, t, d in g.edges(data=True)
               if d.get("type") == EdgeType.REFERENCES.value}
    assert "py:module:tests.derivations" not in targets


def test_unambiguous_short_name_still_reconciles():
    """Declining on ambiguity must not disable reconciliation."""
    sphinx, ast_g = _ambiguous_pair()
    # Drop the competitors: one candidate left, so the answer is certain.
    ast_g.nxgraph.remove_node("py:module:tests.derivations")
    ast_g.nxgraph.remove_node("py:module:pkg.deep.origins.derivations")

    merged = merge_graphs(sphinx, ast_g)
    reconcile_unresolved(merged)
    g = merged.nxgraph
    assert "py:module:derivations" not in g
    targets = {t for _, t, d in g.edges(data=True)
               if d.get("type") == EdgeType.REFERENCES.value}
    assert "py:module:pkg.derivations" in targets


def test_placeholder_never_wins_reconciliation():
    """A real definition outranks a same-named placeholder."""
    sphinx = KnowledgeGraph()
    sphinx.add_node(GraphNode(
        id="py:function:widget", type=NodeType.UNRESOLVED, name="widget",
        display_name="widget", domain="py",
    ))
    ast_g = KnowledgeGraph()
    ast_g.add_node(GraphNode(
        id="py:function:gone.widget", type=NodeType.UNRESOLVED,
        name="gone.widget", display_name="widget", domain="py",
    ))
    ast_g.add_node(GraphNode(
        id="py:function:pkg.widget", type=NodeType.FUNCTION, name="pkg.widget",
        display_name="widget", domain="py",
        metadata={"file_path": "/pkg/__init__.py"},
    ))
    merged = merge_graphs(sphinx, ast_g)
    reconcile_unresolved(merged)
    g = merged.nxgraph
    assert "py:function:pkg.widget" in g
    assert "py:function:widget" not in g


def test_ambiguity_is_judged_after_every_merge_not_during_one():
    """Reconciliation decides once, against the complete graph.

    It used to run inside every ``merge_graphs`` call — once per source
    directory — so it judged ambiguity against a single slice. Widening
    the index to span both graphs fixed the observed ORPHEUS case but
    left the order-sensitivity: a rival arriving in a LATER slice still
    could not retroactively make an earlier decision ambiguous.

    This is that case, and the previous fix could not catch it — the
    rival module arrives only in pass 2, so any decision taken during
    pass 1 is already wrong by the time it exists.
    """
    sphinx = KnowledgeGraph()
    sphinx.add_node(GraphNode(id="std:file:page", type=NodeType.FILE, name="page",
                              display_name="page", domain="std"))
    sphinx.add_node(GraphNode(
        id="py:module:derivations", type=NodeType.UNRESOLVED,
        name="derivations", display_name="derivations", domain="py",
    ))
    sphinx.add_edge(GraphEdge(source="std:file:page", target="py:module:derivations",
                              type=EdgeType.REFERENCES))

    def slice_with(*fulls):
        kg = KnowledgeGraph()
        for full in fulls:
            kg.add_node(GraphNode(
                id=f"py:module:{full}", type=NodeType.MODULE, name=full,
                display_name="derivations", domain="py",
                metadata={"file_path": f"/{full.replace('.', '/')}/__init__.py"},
            ))
        return kg

    # Pass 1 offers exactly ONE candidate — unambiguous in isolation.
    merged = merge_graphs(sphinx, slice_with("pkg.derivations"))
    # Pass 2 brings the rival that makes the name ambiguous.
    merged = merge_graphs(merged, slice_with("tests.derivations"))

    # Only now is the decision taken, and it sees both.
    reconcile_unresolved(merged)

    g = merged.nxgraph
    targets = {t for _, t, d in g.edges(data=True)
               if d.get("type") == EdgeType.REFERENCES.value}
    assert "py:module:pkg.derivations" not in targets, (
        "pass 1 saw one candidate and would have folded; deciding after "
        "every merge is what makes the rival visible"
    )
    assert "py:module:tests.derivations" not in targets
    assert "py:module:derivations" in g


def test_merge_graphs_no_longer_reconciles_on_its_own():
    """The phase boundary is the point — pin it."""
    merged = merge_graphs(*_ambiguous_pair())
    assert "py:module:derivations" in merged.nxgraph


# ---------------------------------------------------------------------------
# IMPLEMENTS is not what a test does to an equation (#49)
# ---------------------------------------------------------------------------
#
# `_infer_implements` pairs a doc page's equations with the code symbols
# it documents, on shared name tokens. Nothing excluded test code, so a
# test class reaching a theory page became an implementation candidate —
# and test classes are unusually prone to it, since
# `TestSlabViaUnifiedDiscrepancyDiagnostic` shares `slab`/`peierls`/
# `multigroup` with half the equations on its page.
#
# Measured on ORPHEUS: 2722 such edges, 195 of them surviving purely
# from correctly-qualified references that no resolution change can
# remove.


def _implements_fixture(mark_test: bool = True) -> KnowledgeGraph:
    kg = KnowledgeGraph()
    kg.add_node(GraphNode(id="std:file:theory/slab", type=NodeType.FILE,
                          name="theory/slab", display_name="slab",
                          domain="std", docname="theory/slab"))
    kg.add_node(GraphNode(id="math:equation:peierls-slab-polar",
                          type=NodeType.EQUATION, name="peierls-slab-polar",
                          display_name="peierls-slab-polar", domain="math"))
    kg.add_edge(GraphEdge(source="std:file:theory/slab",
                          target="math:equation:peierls-slab-polar",
                          type=EdgeType.CONTAINS))

    test_meta = {"file_path": "/tests/test_slab.py"}
    if mark_test:
        test_meta["in_test_file"] = True
    kg.add_node(GraphNode(
        id="py:class:tests.test_slab.TestSlabPolar", type=NodeType.CLASS,
        name="tests.test_slab.TestSlabPolar", display_name="TestSlabPolar",
        domain="py", metadata=test_meta,
    ))
    kg.add_edge(GraphEdge(source="std:file:theory/slab",
                          target="py:class:tests.test_slab.TestSlabPolar",
                          type=EdgeType.DOCUMENTS))

    # A real implementation on the same page, sharing the same token.
    kg.add_node(GraphNode(
        id="py:function:proj.slab.solve_slab_polar", type=NodeType.FUNCTION,
        name="proj.slab.solve_slab_polar", display_name="solve_slab_polar",
        domain="py", metadata={"file_path": "/proj/slab.py"},
    ))
    kg.add_edge(GraphEdge(source="std:file:theory/slab",
                          target="py:function:proj.slab.solve_slab_polar",
                          type=EdgeType.DOCUMENTS))
    return kg


def _implements_targets(g, source):
    return {
        t for _, t, d in g.out_edges(source, data=True)
        if d.get("type") == EdgeType.IMPLEMENTS.value
    }


def test_test_class_does_not_implement_an_equation():
    kg = _implements_fixture()
    _infer_implements(kg.nxgraph)
    assert not _implements_targets(
        kg.nxgraph, "py:class:tests.test_slab.TestSlabPolar",
    ), "a test class was inferred to IMPLEMENT an equation"


def test_real_implementation_on_the_same_page_still_infers():
    """Excluding tests must not disable the inference itself."""
    kg = _implements_fixture()
    _infer_implements(kg.nxgraph)
    assert "math:equation:peierls-slab-polar" in _implements_targets(
        kg.nxgraph, "py:function:proj.slab.solve_slab_polar",
    )


def test_the_equation_is_not_left_looking_implemented_by_a_test():
    """The consequence that makes this a false ALIVE.

    An equation whose only IMPLEMENTS edge points at a test class reads
    as implemented when nothing implements it — and unlike a false DEAD,
    nothing announces it.
    """
    kg = _implements_fixture()
    # Drop the real implementation: the test class is the only candidate.
    kg.nxgraph.remove_node("py:function:proj.slab.solve_slab_polar")
    _infer_implements(kg.nxgraph)

    implementers = {
        s for s, _, d in kg.nxgraph.in_edges("math:equation:peierls-slab-polar",
                                             data=True)
        if d.get("type") == EdgeType.IMPLEMENTS.value
    }
    assert not implementers, (
        "the equation reports an implementer that is only a test"
    )


def test_unmarked_test_node_is_still_inferred():
    """The exclusion keys on `in_test_file`, nothing heuristic.

    Pinned so the rule cannot quietly start guessing from names — a
    production class called `TestHarness` is not test code.
    """
    kg = _implements_fixture(mark_test=False)
    _infer_implements(kg.nxgraph)
    assert _implements_targets(
        kg.nxgraph, "py:class:tests.test_slab.TestSlabPolar",
    )
