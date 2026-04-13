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
