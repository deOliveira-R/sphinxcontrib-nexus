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
    results = q.query("doc", limit=2)  # "doc" matches doc:index, doc:theory, doc:api
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


# ---------------------------------------------------------------------------
# verification_coverage: tiered test-source resolution
# ---------------------------------------------------------------------------


def _coverage_graph(build_fn) -> nx.MultiDiGraph:
    """Helper that builds a minimal verification-coverage graph. The
    caller passes a function that receives an empty ``MultiDiGraph``
    and populates it."""
    g = nx.MultiDiGraph()
    build_fn(g)
    return g


def _find_entry(cov, node_id: str):
    for entry in cov.entries:
        if entry.node.id == node_id:
            return entry
    raise AssertionError(f"{node_id} not in coverage entries")


def test_coverage_declared_tier_wins_over_heuristic():
    """An equation with a declared TESTS edge is verified; the
    ``code_to_1hop`` heuristic must NOT produce competing entries."""

    def build(g):
        g.add_node("math:equation:eq-1", type="equation", name="eq-1",
                   display_name="(1)", domain="math", docname="theory")
        g.add_node("py:function:impl", type="function", name="impl",
                   display_name="impl", domain="py")
        g.add_node("py:function:test_declared", type="function",
                   name="test_declared", display_name="test_declared",
                   domain="py", is_test=True)
        g.add_node("py:function:test_indirect", type="function",
                   name="test_indirect", display_name="test_indirect",
                   domain="py", is_test=True)
        # impl implements eq
        g.add_edge("py:function:impl", "math:equation:eq-1", type="implements")
        # declared: direct TESTS edge from test_declared
        g.add_edge("py:function:test_declared", "math:equation:eq-1",
                   type="tests", source="pytest.mark.verifies", confidence=1.0)
        # heuristic 1-hop: test_indirect calls impl
        g.add_edge("py:function:test_indirect", "py:function:impl",
                   type="calls")

    g = _coverage_graph(build)
    cov = GraphQuery(g).verification_coverage()
    entry = _find_entry(cov, "math:equation:eq-1")
    assert entry.status == "verified"
    # Only the declared test should be in the list.
    assert len(entry.tests) == 1
    assert entry.tests[0].id == "py:function:test_declared"
    assert entry.tests[0].source == "declared"
    assert entry.tests[0].confidence == 1.0


def test_coverage_heuristic_1hop_when_no_declared():
    def build(g):
        g.add_node("math:equation:eq-2", type="equation", name="eq-2",
                   display_name="(2)", domain="math", docname="theory")
        g.add_node("py:function:impl", type="function", name="impl",
                   display_name="impl", domain="py")
        g.add_node("py:function:test_caller", type="function",
                   name="test_caller", display_name="test_caller",
                   domain="py", is_test=True)
        g.add_edge("py:function:impl", "math:equation:eq-2", type="implements")
        g.add_edge("py:function:test_caller", "py:function:impl", type="calls")

    g = _coverage_graph(build)
    cov = GraphQuery(g).verification_coverage()
    entry = _find_entry(cov, "math:equation:eq-2")
    assert entry.status == "verified"
    assert len(entry.tests) == 1
    assert entry.tests[0].source == "heuristic-1hop"
    assert entry.tests[0].confidence == 0.7


def test_coverage_heuristic_multihop_finds_via_helper_chain():
    """Graph: test → helper → impl → equation.

    The classical 1-hop scan sees ``helper`` as the caller of ``impl``,
    which is not ``is_test``, so it misses the test entirely. The
    multi-hop BFS must walk back from ``impl`` until it reaches
    ``test_end_to_end`` at depth 2."""

    def build(g):
        g.add_node("math:equation:eq-3", type="equation", name="eq-3",
                   display_name="(3)", domain="math", docname="theory")
        g.add_node("py:function:impl", type="function", name="impl",
                   display_name="impl", domain="py")
        g.add_node("py:function:helper", type="function", name="helper",
                   display_name="helper", domain="py")
        g.add_node("py:function:test_end_to_end", type="function",
                   name="test_end_to_end", display_name="test_end_to_end",
                   domain="py", is_test=True)
        g.add_edge("py:function:impl", "math:equation:eq-3", type="implements")
        g.add_edge("py:function:helper", "py:function:impl", type="calls")
        g.add_edge("py:function:test_end_to_end", "py:function:helper",
                   type="calls")

    g = _coverage_graph(build)
    cov = GraphQuery(g).verification_coverage()
    entry = _find_entry(cov, "math:equation:eq-3")
    assert entry.status == "verified"
    assert any(t.source == "heuristic-multihop" for t in entry.tests), entry.tests


def test_coverage_multihop_depth_limit_excludes_deep_chains():
    """A chain longer than ``max_depth=3`` must not contribute."""

    def build(g):
        g.add_node("math:equation:eq-deep", type="equation", name="eq-deep",
                   display_name="(deep)", domain="math", docname="theory")
        # chain: test → h1 → h2 → h3 → h4 → impl  (5 hops)
        for name in ("impl", "h1", "h2", "h3", "h4"):
            g.add_node(f"py:function:{name}", type="function", name=name,
                       display_name=name, domain="py")
        g.add_node("py:function:test_deep", type="function", name="test_deep",
                   display_name="test_deep", domain="py", is_test=True)
        g.add_edge("py:function:impl", "math:equation:eq-deep", type="implements")
        g.add_edge("py:function:h4", "py:function:impl", type="calls")
        g.add_edge("py:function:h3", "py:function:h4", type="calls")
        g.add_edge("py:function:h2", "py:function:h3", type="calls")
        g.add_edge("py:function:h1", "py:function:h2", type="calls")
        g.add_edge("py:function:test_deep", "py:function:h1", type="calls")

    g = _coverage_graph(build)
    cov = GraphQuery(g).verification_coverage()
    entry = _find_entry(cov, "math:equation:eq-deep")
    # impl→h4→h3→h2 reaches depth 3; test_deep is at depth 5. Not found.
    assert entry.status == "implemented"
    assert entry.tests == []


def test_coverage_declared_without_implementing_code_is_verified():
    """A declared TESTS edge directly to an equation that has no
    IMPLEMENTS links is still verified — the test claims it."""

    def build(g):
        g.add_node("math:equation:eq-direct", type="equation",
                   name="eq-direct", display_name="(direct)", domain="math",
                   docname="theory")
        g.add_node("py:function:test_direct", type="function",
                   name="test_direct", display_name="test_direct",
                   domain="py", is_test=True)
        g.add_edge("py:function:test_direct", "math:equation:eq-direct",
                   type="tests", source="pytest.mark.verifies", confidence=1.0)

    g = _coverage_graph(build)
    cov = GraphQuery(g).verification_coverage()
    entry = _find_entry(cov, "math:equation:eq-direct")
    assert entry.status == "verified"
    assert entry.implementing_code == []
    assert entry.tests[0].source == "declared"


def test_coverage_documented_when_no_code_no_tests():
    def build(g):
        g.add_node("math:equation:eq-orphan", type="equation",
                   name="eq-orphan", display_name="(orphan)", domain="math",
                   docname="theory")

    g = _coverage_graph(build)
    cov = GraphQuery(g).verification_coverage()
    entry = _find_entry(cov, "math:equation:eq-orphan")
    assert entry.status == "documented"


def test_coverage_dedupes_duplicate_tests_edges():
    """Regression for nexus#7 (query-time layer).

    When two TESTS edges link the same (test, equation) pair —
    e.g. one from ``pytest.mark.verifies`` and one from a registry
    entry that was written before the write-time dedup was
    hardened — the coverage result must collapse them to a single
    ``TestReference``. Otherwise the per-equation test count
    double-counts (the exact 86 → 87 bug from the 0.8.0 cross-
    validation).
    """
    def build(g):
        g.add_node("math:equation:eq-dup", type="equation", name="eq-dup",
                   display_name="(dup)", domain="math", docname="theory")
        g.add_node("py:function:test_t", type="function", name="test_t",
                   display_name="test_t", domain="py", is_test=True)
        # Two explicit TESTS edges from different sources — this is
        # the bug shape the write-time fix prevents, but the query
        # layer must still be robust to a graph loaded from an older
        # nexus version that has this state.
        g.add_edge("py:function:test_t", "math:equation:eq-dup",
                   type="tests", source="pytest.mark.verifies",
                   confidence=1.0)
        g.add_edge("py:function:test_t", "math:equation:eq-dup",
                   type="tests", source="registry", confidence=1.0)

    g = _coverage_graph(build)
    cov = GraphQuery(g).verification_coverage()
    entry = _find_entry(cov, "math:equation:eq-dup")
    assert len(entry.tests) == 1
    assert entry.tests[0].id == "py:function:test_t"


def test_coverage_dedupes_duplicate_implements_edges():
    """Same regression shape for IMPLEMENTS: duplicate edges must not
    inflate ``implementing_code``."""
    def build(g):
        g.add_node("math:equation:eq-impl-dup", type="equation",
                   name="eq-impl-dup", display_name="(impl-dup)",
                   domain="math", docname="theory")
        g.add_node("py:function:impl", type="function", name="impl",
                   display_name="impl", domain="py")
        g.add_edge("py:function:impl", "math:equation:eq-impl-dup",
                   type="implements", source="directive",
                   confidence=1.0)
        g.add_edge("py:function:impl", "math:equation:eq-impl-dup",
                   type="implements", source="registry",
                   confidence=1.0)

    g = _coverage_graph(build)
    cov = GraphQuery(g).verification_coverage()
    entry = _find_entry(cov, "math:equation:eq-impl-dup")
    assert len(entry.implementing_code) == 1
    assert entry.implementing_code[0].id == "py:function:impl"


def test_coverage_implemented_when_code_but_no_tests():
    def build(g):
        g.add_node("math:equation:eq-5", type="equation", name="eq-5",
                   display_name="(5)", domain="math", docname="theory")
        g.add_node("py:function:impl", type="function", name="impl",
                   display_name="impl", domain="py")
        g.add_edge("py:function:impl", "math:equation:eq-5", type="implements")

    g = _coverage_graph(build)
    cov = GraphQuery(g).verification_coverage()
    entry = _find_entry(cov, "math:equation:eq-5")
    assert entry.status == "implemented"


# ---------------------------------------------------------------------------
# verification_audit: group_by and include_tests
# ---------------------------------------------------------------------------


def _audit_graph() -> nx.MultiDiGraph:
    """Two unverified equations, plus one verified one, across two
    pretend modules (orpheus.sn, orpheus.cp). Each equation has
    implementing code; some have declared tests at a level."""
    g = nx.MultiDiGraph()
    # Equations
    for label in ("balance", "transport-cartesian", "keff"):
        g.add_node(f"math:equation:{label}", type="equation", name=label,
                   display_name=f"({label})", domain="math",
                   docname="theory")
    # SN module implementations
    g.add_node("py:function:orpheus.sn.solve_transport", type="function",
               name="orpheus.sn.solve_transport", display_name="solve_transport",
               domain="py")
    g.add_node("py:function:orpheus.sn.enforce_balance", type="function",
               name="orpheus.sn.enforce_balance", display_name="enforce_balance",
               domain="py")
    g.add_edge("py:function:orpheus.sn.solve_transport",
               "math:equation:transport-cartesian", type="implements")
    g.add_edge("py:function:orpheus.sn.enforce_balance",
               "math:equation:balance", type="implements")
    # CP module implementation
    g.add_node("py:function:orpheus.cp.compute_keff", type="function",
               name="orpheus.cp.compute_keff", display_name="compute_keff",
               domain="py")
    g.add_edge("py:function:orpheus.cp.compute_keff",
               "math:equation:keff", type="implements")
    # A declared test verifying keff at L1
    g.add_node("py:function:tests.test_cp.test_keff", type="function",
               name="tests.test_cp.test_keff", display_name="test_keff",
               domain="py", is_test=True, vv_level="L1",
               verifies=("keff",))
    g.add_edge("py:function:tests.test_cp.test_keff",
               "math:equation:keff", type="tests",
               source="pytest.mark.verifies", confidence=1.0)
    return g


def test_audit_flat_gaps_sorted_by_status():
    q = GraphQuery(_audit_graph())
    audit = q.verification_audit()
    assert audit.group_by is None
    assert audit.grouped == {}
    statuses = [g.status for g in audit.gaps]
    # balance + transport-cartesian are "implemented" (have code, no tests).
    assert statuses.count("implemented") == 2
    # keff is verified — not in gaps.
    assert not any(g.equation_id == "math:equation:keff" for g in audit.gaps)


def test_audit_group_by_module_buckets_gaps():
    q = GraphQuery(_audit_graph())
    audit = q.verification_audit(group_by="module")
    assert audit.group_by == "module"
    assert "orpheus" in audit.grouped
    # All SN gaps end up under "orpheus" (top-level package).
    orpheus_gaps = audit.grouped["orpheus"]
    eq_ids = {g.equation_id for g in orpheus_gaps}
    assert "math:equation:balance" in eq_ids
    assert "math:equation:transport-cartesian" in eq_ids


def test_audit_group_by_equation_keys_by_eq_id():
    q = GraphQuery(_audit_graph())
    audit = q.verification_audit(group_by="equation")
    assert audit.group_by == "equation"
    assert "math:equation:balance" in audit.grouped
    assert len(audit.grouped["math:equation:balance"]) == 1


def test_audit_group_by_level_buckets_unassigned_gaps():
    q = GraphQuery(_audit_graph())
    audit = q.verification_audit(group_by="level")
    # Both gap equations have no tests, so they land in "unassigned".
    assert "unassigned" in audit.grouped
    assert len(audit.grouped["unassigned"]) == 2


def test_audit_invalid_group_by_raises():
    q = GraphQuery(_audit_graph())
    with pytest.raises(ValueError, match="group_by"):
        q.verification_audit(group_by="bogus")


def test_audit_include_tests_adds_counts():
    q = GraphQuery(_audit_graph())
    audit = q.verification_audit(include_tests=True)
    assert "tests_declared" in audit.summary
    assert "tests_inferred" in audit.summary
    # One declared test (test_keff → keff), zero inferred
    # (implementing code has no is_test callers).
    assert audit.summary["tests_declared"] == 1


def test_audit_without_include_tests_omits_counts():
    q = GraphQuery(_audit_graph())
    audit = q.verification_audit()
    assert "tests_declared" not in audit.summary


# ---------------------------------------------------------------------------
# verification_gaps
# ---------------------------------------------------------------------------


def _gaps_graph() -> nx.MultiDiGraph:
    g = nx.MultiDiGraph()
    # Equation with no tests anywhere
    g.add_node("math:equation:eq-orphan", type="equation", name="eq-orphan",
               display_name="(orphan)", domain="math", docname="theory")
    # Equation with code but no tests
    g.add_node("math:equation:eq-impl", type="equation", name="eq-impl",
               display_name="(impl)", domain="math", docname="theory")
    g.add_node("py:function:pkg.solve", type="function",
               name="pkg.solve", display_name="solve", domain="py")
    g.add_edge("py:function:pkg.solve", "math:equation:eq-impl",
               type="implements")
    # Two test functions — one tagged L0, one untagged
    g.add_node("py:function:tests.test_a.test_one", type="function",
               name="tests.test_a.test_one", display_name="test_one",
               domain="py", is_test=True, vv_level="L0")
    g.add_node("py:function:tests.test_a.test_two", type="function",
               name="tests.test_a.test_two", display_name="test_two",
               domain="py", is_test=True)  # no vv_level → untagged
    return g


def test_gaps_lists_untagged_tests():
    q = GraphQuery(_gaps_graph())
    result = q.verification_gaps()
    names = {g.display_name for g in result.untagged_tests}
    assert "test_two" in names
    assert "test_one" not in names  # L0-tagged, not untagged


def test_gaps_lists_unverified_equations():
    q = GraphQuery(_gaps_graph())
    result = q.verification_gaps()
    ids = {g.node_id for g in result.unverified_equations}
    assert "math:equation:eq-orphan" in ids
    assert "math:equation:eq-impl" in ids


def test_gaps_module_filter_on_untagged():
    q = GraphQuery(_gaps_graph())
    result = q.verification_gaps(module="tests")
    # test_two is in tests.* — kept
    names = {g.display_name for g in result.untagged_tests}
    assert "test_two" in names
    # The equation filter falls back to implementing code (pkg.solve)
    # which is under "pkg", not "tests" — so equations should be gone.
    assert all(
        g.node_id != "math:equation:eq-impl"
        for g in result.unverified_equations
    )


def test_gaps_missing_err_catchers():
    g = _gaps_graph()
    # Mark test_one as catching FM-01 only
    g.nodes["py:function:tests.test_a.test_one"]["catches"] = ("FM-01",)
    q = GraphQuery(g)
    result = q.verification_gaps(
        error_catalog={"FM-01", "FM-07", "ERR-020"},
    )
    tags = {g.display_name for g in result.missing_err_catchers}
    assert tags == {"FM-07", "ERR-020"}


def test_gaps_missing_err_catchers_empty_when_no_catalog():
    q = GraphQuery(_gaps_graph())
    result = q.verification_gaps()
    assert result.missing_err_catchers == []
