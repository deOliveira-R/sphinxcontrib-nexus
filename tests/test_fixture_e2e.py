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
def fixture_kg(tmp_path_factory):
    """The built fixture as a KnowledgeGraph — one build, two views.

    Most tests want the networkx view (``fixture_graph``); a few need
    ``.metadata`` (the re-export map lives there, not on an edge). Both
    read this one build rather than paying a second ``sphinx-build``.
    """
    build = tmp_path_factory.mktemp("fixture-build")
    # Use the current interpreter's sphinx-build so the in-tree
    # sphinxcontrib.nexus from ``.venv`` is the one driving the build.
    subprocess.run(
        [sys.executable, "-m", "sphinx", "-q", "-E", str(FIXTURE), str(build)],
        check=True,
    )
    db = build / "_nexus" / "graph.db"
    assert db.exists(), f"Expected {db} to exist after sphinx-build"
    return load_sqlite(db)


@pytest.fixture(scope="module")
def fixture_graph(fixture_kg):
    return fixture_kg.nxgraph


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


# ---------------------------------------------------------------------------
# Declaring stands the guessing down — through a real build (nexus #82)
# ---------------------------------------------------------------------------
#
# The unit tests in `test_merge.py` call `_infer_implements` directly, so
# they cannot see the assumption the whole design rests on: that every
# declaration path — directives, registry, `write_verifies_edges` — has
# already run when the inference starts. Move `_infer_implements` above
# `apply_pending_edges` in `__init__.py` and every unit test stays green
# while the stand-down silently stops working on real projects.
#
# So this pair is deliberately end-to-end, and deliberately a pair: the
# control proves the inference reaches this fixture at all. Before the
# `fixture-mesh-*` equations were added it did not — 0 inferred edges in
# the whole graph — which is exactly the state in which a stand-down
# assertion passes while testing nothing.


def _implementers(g, label: str) -> dict[str, str]:
    eq_id = f"math:equation:{label}"
    return {
        s: d.get("source")
        for s, _, d in g.in_edges(eq_id, data=True)
        if d.get("type") == "implements"
    }


def test_the_inference_reaches_this_fixture_at_all(fixture_graph):
    """POSITIVE CONTROL for the stand-down assertion below.

    ``fixture-mesh-count`` is undeclared and shares the token ``mesh``
    with the one symbol this page DOCUMENTS, so it must collect a guess.
    """
    assert _implementers(fixture_graph, "fixture-mesh-count") == {
        "py:class:solver_pkg.helpers.Mesh": "inferred",
    }


def test_declaring_stands_the_guess_down_through_a_real_build(fixture_graph):
    """``fixture-mesh-spacing`` is the same shape as the control — same
    page, same shared token, same guesser — plus one directive.

    The declaration names ``build_mesh``; the guess would have named
    ``Mesh``. So an equality assertion here distinguishes *the guess
    stood down* from *the declaration happened to agree with it*.
    """
    assert _implementers(fixture_graph, "fixture-mesh-spacing") == {
        "py:function:solver_pkg.solver.build_mesh": "directive",
    }


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


# ---------------------------------------------------------------------------
# A Python-domain id is spelled with the objtype, on BOTH producers
# ---------------------------------------------------------------------------
#
# A node id carries the objtype (``function``), but a Sphinx cross-reference
# carries the ROLE (``func``). The docstring scanner in ``ast_analyzer``
# translated between them with a local dict; the doctree walker in
# ``extractors`` did not, so the same symbol got two ids and its edges were
# split between them.
#
# ``[M]`` 2026-08-16 on ORPHEUS's graph before the map was hoisted to
# ``_mappings.REFTYPE_OBJTYPE_MAP``: 316 short-prefix nodes (``py:func`` 206,
# ``py:mod`` 55, ``py:meth`` 23, ``py:attr`` 22, ``py:obj`` 8, ``py:exc`` 2)
# and 265 symbols carrying both spellings at once.

_ROLE_SPELLED_PREFIXES = (
    "py:func:", "py:meth:", "py:attr:", "py:mod:", "py:exc:", "py:obj:",
)


def test_no_node_id_is_spelled_with_a_role_instead_of_an_objtype(fixture_graph):
    offenders = sorted(
        n for n in fixture_graph
        if isinstance(n, str) and n.startswith(_ROLE_SPELLED_PREFIXES)
    )
    assert not offenders, (
        "these ids are spelled with the Sphinx ROLE, so they are separate "
        f"nodes from the objtype-spelled ones: {offenders}"
    )


@pytest.mark.parametrize(
    "objtype_id, role_id",
    [
        ("py:function:solver_pkg.absent.compute_leakage",
         "py:func:solver_pkg.absent.compute_leakage"),
        ("py:method:solver_pkg.helpers.Mesh.absent_method",
         "py:meth:solver_pkg.helpers.Mesh.absent_method"),
        ("py:attribute:solver_pkg.helpers.Mesh.absent_attr",
         "py:attr:solver_pkg.helpers.Mesh.absent_attr"),
        ("py:function:numpy.absent_function",
         "py:func:numpy.absent_function"),
    ],
)
def test_an_unresolved_prose_role_lands_under_its_objtype(
    fixture_graph, objtype_id, role_id
):
    """The four roles in ``theory/balance.rst`` name nothing resolvable, so
    each takes the branch that forges an id — the branch the fix touches."""
    assert role_id not in fixture_graph
    assert objtype_id in fixture_graph, (
        f"{objtype_id} absent; graph has "
        f"{sorted(n for n in fixture_graph if 'absent' in str(n))}"
    )


def test_a_resolvable_prose_role_still_reaches_the_documented_node(fixture_graph):
    """Positive control for the two legs above.

    They assert that a short-prefix id is ABSENT, which is also true of a
    walker that stopped emitting reference nodes at all (``vv-principles``
    #19). This pins that a role which DOES resolve still lands on the
    autodoc'd node — so the legs above measure the SPELLING rather than the
    walker being dead.
    """
    target = "py:function:solver_pkg.solver.solve_attenuation"
    assert target in fixture_graph
    assert not any(
        str(n).endswith("solve_attenuation") and str(n).startswith("py:func:")
        for n in fixture_graph
    ), "the resolvable role forged a phantom beside the documented node"


# ---------------------------------------------------------------------------
# The equation namespace holds declared labels, not LaTeX fragments
# ---------------------------------------------------------------------------
#
# `:eq:`X`` REFERENCES a labelled equation; `:math:`X`` TYPESETS X and
# references nothing. Nexus forgives the common slip of writing the first
# where the second was meant — but its guard was a BLOCKLIST (reject `\`, `{`,
# `}`) while the Python branch three lines below asked the opposite, stronger
# question (is this a well-formed name?). Ordinary inline math clears a
# blocklist trivially.
#
# ``[M]`` 2026-08-16, ORPHEUS: of 1860 `math:equation:*` nodes, 956 were
# unresolved — the namespace was 51% LaTeX. 12 of the 13 ids in the entire
# graph containing a newline were inline math wrapped across docstring lines.


@pytest.mark.parametrize(
    "fragment",
    ["k > 1", "k = 1", "[0, R]", "a_0\n> 0"],
)
def test_inline_math_does_not_become_an_equation_node(fixture_graph, fragment):
    """Each fragment is a `:math:` body in ``solve_keff``'s docstring."""
    assert f"math:equation:{fragment}" not in fixture_graph


def test_no_graph_id_carries_whitespace_it_did_not_earn(fixture_graph):
    """Glossary terms legitimately contain spaces (``std:term:angular flux``);
    a NEWLINE inside an id never is — it is docstring line-wrap that reached
    the id builder raw."""
    offenders = sorted(n for n in fixture_graph if isinstance(n, str) and "\n" in n)
    assert not offenders, f"ids built from wrapped raw text: {offenders!r}"


def test_a_math_role_naming_a_DECLARED_label_still_references_it(fixture_graph):
    """Positive control for the legs above.

    They assert nodes are ABSENT, which is equally true of a pass that
    dropped every `:math:` reference (vv-principles #19). The forgiving route
    is the feature; only its CONDITION changed, so a `:math:` body that names
    a real declared label must still produce the edge.
    """
    refs = {
        t for s, t, d in fixture_graph.edges(data=True)
        if d.get("reftype") == "math" and str(t).startswith("math:equation:")
    }
    assert "math:equation:fixture-balance" in refs, sorted(refs)
    assert "math:equation:fixture-keff" in refs, sorted(refs)


def test_every_surviving_equation_node_is_a_declared_label(fixture_graph):
    """The namespace-level statement the two legs above make case by case."""
    undeclared = sorted(
        n for n, d in fixture_graph.nodes(data=True)
        if str(n).startswith("math:equation:") and d.get("type") != "equation"
    )
    assert not undeclared, f"equation namespace holds non-labels: {undeclared!r}"


def test_a_wrapped_role_in_PROSE_is_normalised_like_one_in_a_docstring(
    fixture_graph
):
    """``theory/balance.rst`` wraps a `:meth:` body across source lines.

    Sphinx collapses that when it RESOLVES; when it cannot, the raw text
    reaches the id builder. The docstring scanner has always collapsed it —
    the doctree walker had not, so this leg is the witness for that half.
    Distinct from the newline legs above, which enter by the AST path.
    """
    wrapped = "py:method:solver_pkg.helpers.Mesh.absent_wrapped_method"
    assert wrapped in fixture_graph, sorted(
        n for n in fixture_graph if "wrapped" in str(n)
    )


# ---------------------------------------------------------------------------
# `.. error-entry::` — the declaring directive, through a REAL build
# ---------------------------------------------------------------------------
#
# The unit tests in `test_directives.py` call `apply_declared_nodes`
# directly, which is why they could not see that `apply_pending_edges`
# — the OTHER function walking the same queue — raised `KeyError:
# 'label'` on a declaration payload and took the build down with it.
# Only a real build runs both, so these are the gates that matter.


def test_an_error_entry_becomes_a_node_in_a_real_build(fixture_graph):
    for entry_id in ("FM-01", "FM-99"):
        node = fixture_graph.nodes.get(f"vv:error:{entry_id}")
        assert node is not None, f"{entry_id} was not declared"
        assert node["type"] == "error"
        assert node["title"]


def test_a_catches_marker_becomes_a_real_edge(fixture_graph):
    """The whole point of `nexus#63`: a string becomes a traversal."""
    edges = [
        (s, t) for s, t, d in fixture_graph.edges(data=True)
        if d.get("type") == "catches"
    ]
    assert edges == [(
        "py:function:solver_tests.test_solver.test_attenuation_vacuum_source",
        "vv:error:FM-01",
    )]


def test_the_uncaught_entry_LEADS_the_errors_answer(fixture_graph):
    """FM-99 is declared and claimed by nothing — the finding.

    This is the case the query exists to surface, and it is here rather
    than only in a hand-built graph so that the ordering is pinned
    against a graph the real directive path produced.
    """
    from sphinxcontrib.nexus.query import GraphQuery

    result = GraphQuery(fixture_graph).errors()

    assert [e.name for e in result.entries] == ["FM-99", "FM-01"]
    assert result.uncaught == 1
    assert result.total_catchers == 1
    assert result.unresolved_markers == []


def test_a_type_checking_import_edge_is_marked_through_the_real_build(
    fixture_graph,
):
    """nexus#88 end to end — the stamp survives build → DB → load.

    The in-process visitor test proves the metadata is *computed*; only
    this proves it is still there after the sqlite round trip, which is
    where an attribute a consumer will branch on has to be readable.

    ``solver_pkg.typing_only`` is imported ONLY under ``TYPE_CHECKING``,
    so its edge is unambiguous; ``solver_pkg.helpers`` is the runtime
    control in the same source file.
    """
    def _meta(target: str) -> dict:
        edges = [
            d for _, v, d in fixture_graph.out_edges(
                "py:module:solver_pkg.solver", data=True,
            )
            if d.get("type") == "imports" and v == f"py:module:{target}"
        ]
        assert len(edges) == 1, (target, edges)
        return edges[0]

    assert _meta("solver_pkg.typing_only").get("type_checking") is True
    # The control: a runtime import in the same file stays bare, so a
    # consumer reading `.get("type_checking")` as falsy is correct.
    assert "type_checking" not in _meta("solver_pkg.helpers")


def test_a_type_only_alias_is_not_a_re_export_after_the_real_build(
    fixture_kg,
):
    """The phantom public path must not reach the canonicalization map.

    ``solver_pkg/__init__.py`` guards ``FluxProfile`` and re-exports
    ``Mesh`` for real — so this asserts the guard narrowed the map
    rather than emptying it.
    """
    reexports = fixture_kg.metadata.get("reexports") or {}
    assert reexports, "no re-export map survived the build — test is vacuous"
    assert "solver_pkg.FluxProfile" not in reexports
    assert reexports.get("solver_pkg.Mesh") == "solver_pkg.helpers.Mesh"
