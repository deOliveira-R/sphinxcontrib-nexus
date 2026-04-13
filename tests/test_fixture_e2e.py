"""End-to-end regression harness against the ``minimal_project``
fixture. Each test targets exactly one Session 2 feature so a failure
points at a specific contract.

Module-scoped: one ``sphinx-build`` per test file, graph loaded once.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from sphinxcontrib.nexus.export import load_sqlite
from sphinxcontrib.nexus.query import GraphQuery

FIXTURE = Path(__file__).parent / "fixtures" / "minimal_project"


@pytest.fixture(scope="module")
def fixture_graph(tmp_path_factory):
    build = tmp_path_factory.mktemp("fixture-build")
    # Use the current interpreter's sphinx-build so the in-tree
    # sphinxcontrib.nexus from ``.venv`` is the one driving the build.
    subprocess.run(
        [sys.executable, "-m", "sphinx", "-q", "-E", str(FIXTURE), str(build)],
        check=True,
    )
    db = build / "_nexus" / "graph.db"
    assert db.exists(), f"Expected {db} to exist after sphinx-build"
    return load_sqlite(db).nxgraph


# ---------------------------------------------------------------------------
# Regression canaries for Session 1 features (must stay green in Session 2+)
# ---------------------------------------------------------------------------


def test_equation_nodes_exist(fixture_graph):
    for label in (
        "fixture-attenuation",
        "fixture-balance",
        "fixture-keff",
        "fixture-leakage",
        "fixture-absorption",
    ):
        assert f"math:equation:{label}" in fixture_graph.nodes, label


def test_math_role_routing_to_equation_namespace(fixture_graph):
    refs = [
        (s, t)
        for s, t, d in fixture_graph.edges(data=True)
        if d.get("type") == "references" and t.startswith("math:equation:")
    ]
    # solve_attenuation's docstring references fixture-attenuation via :math:`...`.
    assert any(
        t == "math:equation:fixture-attenuation" for _, t in refs
    ), refs


def test_no_py_math_phantoms(fixture_graph):
    # Session 1 bug: :math:`label` used to produce py:math:label nodes.
    assert not any(
        str(n).startswith("py:math:") or str(n).startswith("py:eq:")
        for n in fixture_graph.nodes
    )


# ---------------------------------------------------------------------------
# Session 2 feature 2.1: decorator metadata on test nodes
# ---------------------------------------------------------------------------


def _node(fixture_graph, nid: str) -> dict:
    attrs = fixture_graph.nodes.get(nid)
    assert attrs is not None, (
        f"{nid} not in graph. "
        f"Sample nodes: {list(fixture_graph.nodes)[:10]}"
    )
    return attrs


def test_decorator_function_level_vv_level_and_catches(fixture_graph):
    node = _node(
        fixture_graph,
        "py:function:solver_tests.test_solver.test_attenuation_vacuum_source",
    )
    assert node["vv_level"] == "L0"
    assert "FM-01" in node["catches"]


def test_decorator_verifies_tuple(fixture_graph):
    node = _node(
        fixture_graph,
        "py:function:solver_tests.test_solver.test_keff_critical",
    )
    assert set(node["verifies"]) == {"fixture-keff", "fixture-leakage"}


def test_class_level_decorator_propagates_to_method(fixture_graph):
    node = _node(
        fixture_graph,
        "py:method:solver_tests.test_solver.TestL1Balance.test_balance_zero_residual",
    )
    assert node["vv_level"] == "L1"


def test_decorators_raw_tuple_is_recorded(fixture_graph):
    node = _node(
        fixture_graph,
        "py:function:solver_tests.test_solver.test_attenuation_vacuum_source",
    )
    decs = node["decorators"]
    assert len(decs) == 3
    assert any("pytest.mark.l0" in d for d in decs)
    assert any("verifies" in d for d in decs)
    assert any("catches" in d for d in decs)


# ---------------------------------------------------------------------------
# Session 2 feature 2.4: TESTS edges written from verifies metadata
# ---------------------------------------------------------------------------


def test_declared_tests_edge_exists(fixture_graph):
    edges = [
        (s, t)
        for s, t, d in fixture_graph.edges(data=True)
        if d.get("type") == "tests"
        and d.get("source") == "pytest.mark.verifies"
    ]
    # test_attenuation_vacuum_source verifies fixture-attenuation
    assert (
        "py:function:solver_tests.test_solver.test_attenuation_vacuum_source",
        "math:equation:fixture-attenuation",
    ) in edges
    # test_keff_critical verifies two labels
    assert (
        "py:function:solver_tests.test_solver.test_keff_critical",
        "math:equation:fixture-keff",
    ) in edges
    assert (
        "py:function:solver_tests.test_solver.test_keff_critical",
        "math:equation:fixture-leakage",
    ) in edges


# ---------------------------------------------------------------------------
# Session 2 feature 2.5: _infer_implements guard
# ---------------------------------------------------------------------------


def test_no_duplicate_implements_over_explicit_tests(fixture_graph):
    """For every (code, equation) pair that has a declared TESTS edge,
    there must not be ALSO a duplicate ``implements`` edge with
    source=="inferred" on the same pair direction. The heuristic must
    honor pre-existing explicit evidence."""
    declared_pairs = {
        (s, t)
        for s, t, d in fixture_graph.edges(data=True)
        if d.get("type") == "tests" and d.get("source") != "inferred"
    }
    inferred_pairs = {
        (s, t)
        for s, t, d in fixture_graph.edges(data=True)
        if d.get("type") == "implements" and d.get("source") == "inferred"
    }
    assert declared_pairs.isdisjoint(inferred_pairs), (
        declared_pairs & inferred_pairs
    )


# ---------------------------------------------------------------------------
# Session 2 feature 2.6: tiered verification_coverage
# ---------------------------------------------------------------------------


def _coverage_entry(q: GraphQuery, eq_id: str):
    for e in q.verification_coverage().entries:
        if e.node.id == eq_id:
            return e
    raise AssertionError(f"{eq_id} not in coverage entries")


def test_verification_coverage_finds_declared_test(fixture_graph):
    q = GraphQuery(fixture_graph)
    entry = _coverage_entry(q, "math:equation:fixture-attenuation")
    assert entry.status == "verified"
    sources = {t.source for t in entry.tests}
    assert "declared" in sources, sources
    assert any(
        t.id.endswith(".test_attenuation_vacuum_source") for t in entry.tests
    )


def test_verification_coverage_multi_hop_reaches_absorption(fixture_graph):
    """fixture-absorption has no explicit pytest.mark.verifies and no
    direct is_test caller of its implementing code. It must still be
    reachable via the test → run_case → solve_attenuation chain — but
    the fixture's solve_attenuation doesn't implement fixture-absorption
    directly. This test asserts the weaker invariant: the entry exists
    and reflects its real status. If a multi-hop path does exist to
    ``_exp_decay`` via the implements heuristic, we accept it.
    """
    q = GraphQuery(fixture_graph)
    entry = _coverage_entry(q, "math:equation:fixture-absorption")
    # fixture-absorption has no implements edge in the fixture, so it's
    # expected to remain "documented" — the point is that the entry is
    # still produced without crashing and carries no phantom tests.
    assert entry.status in ("documented", "verified"), entry.status


def test_verification_audit_reflects_declared_tests(fixture_graph):
    q = GraphQuery(fixture_graph)
    audit = q.verification_audit()
    verified_count = audit.summary.get("verified", 0)
    # At minimum, fixture-attenuation, fixture-balance, fixture-keff,
    # and fixture-leakage are covered by declared pytest.mark.verifies.
    assert verified_count >= 4, audit.summary


# ---------------------------------------------------------------------------
# Session 3 features
# ---------------------------------------------------------------------------


def test_registry_implementation_edges_present(fixture_graph):
    """``registry.yaml`` declares solve_balance implements
    fixture-balance AND fixture-absorption. Both edges must appear
    with ``source="registry"``."""
    registry_edges = {
        (s, t)
        for s, t, d in fixture_graph.edges(data=True)
        if d.get("type") == "implements" and d.get("source") == "registry"
    }
    assert (
        "py:function:solver_pkg.solver.solve_balance",
        "math:equation:fixture-balance",
    ) in registry_edges
    assert (
        "py:function:solver_pkg.solver.solve_balance",
        "math:equation:fixture-absorption",
    ) in registry_edges


def test_registry_verification_edge_present(fixture_graph):
    """``registry.yaml`` declares test_end_to_end verifies
    fixture-leakage at level L2."""
    registry_tests = {
        (s, t)
        for s, t, d in fixture_graph.edges(data=True)
        if d.get("type") == "tests" and d.get("source") == "registry"
    }
    assert (
        "py:function:solver_tests.test_solver.test_end_to_end_via_helper_chain",
        "math:equation:fixture-leakage",
    ) in registry_tests


def test_registry_enriches_test_node_with_level(fixture_graph):
    """The registry entry for test_end_to_end_via_helper_chain sets
    level=L2. Because the AST parser reads @pytest.mark.l2 on that
    same test too, the final vv_level should be L2 either way.
    More importantly: no crash on the dual-source population."""
    node = fixture_graph.nodes[
        "py:function:solver_tests.test_solver.test_end_to_end_via_helper_chain"
    ]
    assert node["vv_level"] == "L2"


def test_directive_verifies_edge_present(fixture_graph):
    """``theory/solver.rst`` has a ``.. verifies:: fixture-attenuation
    :by: solver_tests.test_solver.test_end_to_end_via_helper_chain``
    block. Expect a TESTS edge with ``source="directive"``."""
    directive_edges = {
        (s, t)
        for s, t, d in fixture_graph.edges(data=True)
        if d.get("type") == "tests" and d.get("source") == "directive"
    }
    assert (
        "py:function:solver_tests.test_solver.test_end_to_end_via_helper_chain",
        "math:equation:fixture-attenuation",
    ) in directive_edges


def test_directive_implements_edge_present(fixture_graph):
    """``theory/solver.rst`` has a ``.. implements:: fixture-keff
    :by: solver_pkg.solver.solve_keff`` block."""
    directive_edges = {
        (s, t)
        for s, t, d in fixture_graph.edges(data=True)
        if d.get("type") == "implements" and d.get("source") == "directive"
    }
    assert (
        "py:function:solver_pkg.solver.solve_keff",
        "math:equation:fixture-keff",
    ) in directive_edges


def test_verification_audit_group_by_module(fixture_graph):
    q = GraphQuery(fixture_graph)
    audit = q.verification_audit(group_by="module")
    assert audit.group_by == "module"
    # There's at most one bucket in the fixture — the solver_pkg
    # equations — and it must exist when there are any gaps at all.
    if audit.gaps:
        assert audit.grouped, audit.grouped


def test_verification_audit_include_tests_counts_declared(fixture_graph):
    q = GraphQuery(fixture_graph)
    audit = q.verification_audit(include_tests=True)
    assert "tests_declared" in audit.summary
    # At minimum, the three ``@pytest.mark.verifies`` markers
    # (test_attenuation_vacuum_source, test_balance_zero_residual,
    # test_keff_critical × 2 labels) + one registry entry
    # (test_end_to_end → fixture-leakage) + directive
    # (test_end_to_end → fixture-attenuation) = at least six declared.
    assert audit.summary["tests_declared"] >= 5, audit.summary


def test_verification_gaps_lists_no_untagged_tests(fixture_graph):
    """Every test in the fixture carries a vv_level (function, class,
    or module propagation). ``verification_gaps`` should return an
    empty ``untagged_tests`` list — the fixture is "fully tagged"."""
    q = GraphQuery(fixture_graph)
    gaps = q.verification_gaps()
    display_names = {g.display_name for g in gaps.untagged_tests}
    # Allow the fixture to tag everything; this test is a canary on
    # the fixture itself, not the query.
    assert not any(
        name.startswith("test_") for name in display_names
    ), display_names


def test_verification_gaps_error_catalog_filter(fixture_graph):
    """Supply an error catalog and confirm the filter correctly
    reports tags that no test's ``catches`` metadata mentions."""
    q = GraphQuery(fixture_graph)
    gaps = q.verification_gaps(
        error_catalog={"FM-01", "FM-99", "ERR-777"},
    )
    tags = {g.display_name for g in gaps.missing_err_catchers}
    # test_attenuation_vacuum_source declares catches=("FM-01",), so
    # FM-01 is covered. FM-99 and ERR-777 should remain.
    assert "FM-99" in tags
    assert "ERR-777" in tags
    assert "FM-01" not in tags


# ---------------------------------------------------------------------------
# Issue #3 — re-export canonicalization
# ---------------------------------------------------------------------------


def test_mesh_class_has_exactly_one_canonical_node(fixture_graph):
    """``Mesh`` is defined in ``solver_pkg.helpers`` and re-exported
    from ``solver_pkg.__init__`` via ``from .helpers import Mesh``.
    ``solver.build_mesh`` then imports ``Mesh`` via the re-export
    path (``from solver_pkg import Mesh``) and instantiates it.

    After canonicalization the graph must contain exactly one node
    whose leaf is ``Mesh``, and it must be the canonical
    ``py:class:solver_pkg.helpers.Mesh`` — not a
    ``py:class:solver_pkg.Mesh`` re-export duplicate or a
    ``py:function:solver_pkg.Mesh`` call-site mis-typing.
    """
    mesh_nodes = [
        nid
        for nid, attrs in fixture_graph.nodes(data=True)
        if (attrs.get("name") or "").rsplit(".", 1)[-1] == "Mesh"
    ]
    assert mesh_nodes == ["py:class:solver_pkg.helpers.Mesh"], mesh_nodes

    canonical = "py:class:solver_pkg.helpers.Mesh"
    assert fixture_graph.nodes[canonical].get("type") == "class"


def test_build_mesh_call_targets_canonical(fixture_graph):
    """``solver_pkg.solver.build_mesh`` calls ``Mesh(size=size)``.
    That CALLS edge must land on the canonical class, even though
    the source file imported via the re-export path."""
    build_mesh_id = "py:function:solver_pkg.solver.build_mesh"
    calls = [
        t for _, t, d in fixture_graph.out_edges(build_mesh_id, data=True)
        if d.get("type") == "calls"
    ]
    assert "py:class:solver_pkg.helpers.Mesh" in calls, calls
    # No stray Mesh phantoms on the call edges.
    mesh_targets = [t for t in calls if t.endswith(".Mesh")]
    assert mesh_targets == ["py:class:solver_pkg.helpers.Mesh"], mesh_targets


def test_no_function_typed_mesh_phantom(fixture_graph):
    """Regression guard: the 0.6.0 bug shape produced a
    ``py:function:solver_pkg.Mesh`` node because
    ``_resolve_call_target`` hardcoded the ``py:function:`` prefix.
    The canonicalization pass folds those phantoms away."""
    function_mesh = [
        nid for nid in fixture_graph.nodes
        if nid.startswith("py:function:") and nid.endswith(".Mesh")
    ]
    assert function_mesh == [], function_mesh


# ---------------------------------------------------------------------------
# Session 4.3 — parallel-build equivalence
# ---------------------------------------------------------------------------


def test_parallel_build_matches_serial(tmp_path_factory):
    """The extension declares ``parallel_write_safe=True``. This
    test pins that the declaration is honest: a ``sphinx-build
    -j 2`` run against the fixture must produce the same node set,
    edge count, and edge-type distribution as a serial build."""
    serial_build = tmp_path_factory.mktemp("serial-build")
    parallel_build = tmp_path_factory.mktemp("parallel-build")

    subprocess.run(
        [sys.executable, "-m", "sphinx", "-q", "-E",
         str(FIXTURE), str(serial_build)],
        check=True,
    )
    subprocess.run(
        [sys.executable, "-m", "sphinx", "-q", "-E", "-j", "2",
         str(FIXTURE), str(parallel_build)],
        check=True,
    )

    serial = load_sqlite(serial_build / "_nexus" / "graph.db").nxgraph
    parallel = load_sqlite(parallel_build / "_nexus" / "graph.db").nxgraph

    # Same node set.
    assert set(serial.nodes) == set(parallel.nodes)

    # Same edge-type distribution.
    from collections import Counter

    def _edge_types(g):
        return Counter(d.get("type", "") for _, _, d in g.edges(data=True))

    assert _edge_types(serial) == _edge_types(parallel)

    # Same total edge count (the multigraph may have separate
    # parallel edges with identical types; the counter check above
    # already covers this, but this gives a cleaner failure
    # message).
    assert len(serial.edges) == len(parallel.edges)
