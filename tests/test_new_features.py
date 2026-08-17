"""Tests for community detection, detect_changes, rename, and MCP server."""

from __future__ import annotations

import json
import subprocess

import networkx as nx
import pytest

from sphinxcontrib.nexus.query import GraphQuery
from sphinxcontrib.nexus.workspace import NoWorkspaceError, Workspace


# ---------------------------------------------------------------------------
# Shared fixture: a richer graph for testing new features
# ---------------------------------------------------------------------------


@pytest.fixture()
def rich_graph():
    """Build a graph with enough structure for community/rename/impact tests.

    Structure:
        Module A: solve(), helper() — solve calls helper
        Module B: compute(), transform() — compute calls transform
        Class C extends Base
        A.solve uses type numpy.ndarray
        Cross-module: solve calls compute
    """
    g = nx.MultiDiGraph()

    nodes = {
        "py:module:alpha": {"type": "module", "name": "alpha", "file_path": "alpha.py", "source": "ast"},
        "py:module:beta": {"type": "module", "name": "beta", "file_path": "beta.py", "source": "ast"},
        "py:function:alpha.solve": {"type": "function", "name": "alpha.solve", "display_name": "solve", "file_path": "alpha.py", "lineno": 5, "source": "ast"},
        "py:function:alpha.helper": {"type": "function", "name": "alpha.helper", "display_name": "helper", "file_path": "alpha.py", "lineno": 15, "source": "ast"},
        "py:function:beta.compute": {"type": "function", "name": "beta.compute", "display_name": "compute", "file_path": "beta.py", "lineno": 3, "source": "ast"},
        "py:function:beta.transform": {"type": "function", "name": "beta.transform", "display_name": "transform", "file_path": "beta.py", "lineno": 10, "source": "ast"},
        "py:class:beta.Child": {"type": "class", "name": "beta.Child", "display_name": "Child", "file_path": "beta.py", "lineno": 20, "source": "ast"},
        "py:class:Base": {"type": "class", "name": "Base", "display_name": "Base"},
        "py:class:numpy.ndarray": {"type": "external", "name": "numpy.ndarray", "display_name": "numpy.ndarray"},
    }
    for nid, attrs in nodes.items():
        g.add_node(nid, **attrs)

    edges = [
        ("py:module:alpha", "py:function:alpha.solve", {"type": "contains"}),
        ("py:module:alpha", "py:function:alpha.helper", {"type": "contains"}),
        ("py:module:beta", "py:function:beta.compute", {"type": "contains"}),
        ("py:module:beta", "py:function:beta.transform", {"type": "contains"}),
        ("py:module:beta", "py:class:beta.Child", {"type": "contains"}),
        ("py:function:alpha.solve", "py:function:alpha.helper", {"type": "calls"}),
        ("py:function:alpha.solve", "py:function:beta.compute", {"type": "calls"}),
        ("py:function:beta.compute", "py:function:beta.transform", {"type": "calls"}),
        ("py:class:beta.Child", "py:class:Base", {"type": "inherits"}),
        ("py:function:alpha.solve", "py:class:numpy.ndarray", {"type": "type_uses"}),
        ("py:module:alpha", "py:module:beta", {"type": "imports"}),
    ]
    for src, tgt, data in edges:
        g.add_edge(src, tgt, **data)

    return g


# ---------------------------------------------------------------------------
# Community detection tests
# ---------------------------------------------------------------------------


def test_communities_detected(rich_graph):
    q = GraphQuery(rich_graph)
    results = q.communities(min_size=2)
    assert len(results) > 0
    total_members = sum(c.size for c in results)
    assert total_members > 0


def test_communities_sorted_by_size(rich_graph):
    q = GraphQuery(rich_graph)
    results = q.communities(min_size=2)
    if len(results) >= 2:
        assert results[0].size >= results[1].size


def test_communities_have_labels(rich_graph):
    q = GraphQuery(rich_graph)
    results = q.communities(min_size=2)
    for c in results:
        assert c.label  # non-empty label


# ---------------------------------------------------------------------------
# Rename tests
# ---------------------------------------------------------------------------


def test_rename_dry_run_finds_graph_refs(rich_graph):
    q = GraphQuery(rich_graph)
    result = q.rename("alpha.solve", "alpha.solve_system")
    assert result.total_edits > 0
    assert all(e.confidence == "high" for e in result.edits)


def test_rename_finds_regex_matches(rich_graph, tmp_path):
    # Create a file that references the name
    src = tmp_path / "test_file.py"
    src.write_text("from alpha import solve\nresult = solve(data)\n")

    q = GraphQuery(rich_graph, workspace=Workspace.for_root(tmp_path))
    result = q.rename("solve", "solve_system")
    # Should find regex matches in the test file
    regex_edits = [e for e in result.edits if e.confidence == "medium"]
    assert len(regex_edits) > 0


def test_rename_apply(rich_graph, tmp_path):
    src = tmp_path / "code.py"
    src.write_text("def solve():\n    pass\n\nresult = solve()\n")

    q = GraphQuery(rich_graph, workspace=Workspace.for_root(tmp_path))
    q.rename("solve", "solve_system", dry_run=False)

    content = src.read_text()
    assert "solve_system" in content
    assert "def solve()" not in content


# ---------------------------------------------------------------------------
# Detect changes tests (with a real git repo)
# ---------------------------------------------------------------------------


def test_detect_changes_on_git_repo(rich_graph, tmp_path):
    """Create a git repo, make a change, and detect it."""
    # Init git repo
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=tmp_path, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=tmp_path, capture_output=True,
    )

    # Create and commit a file
    py_file = tmp_path / "alpha.py"
    py_file.write_text("def solve(): pass\n")
    subprocess.run(["git", "add", "alpha.py"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True)

    # Modify the file
    py_file.write_text("def solve(): return 42\n")

    # Update graph nodes to point to tmp_path files
    rich_graph.nodes["py:function:alpha.solve"]["file_path"] = str(py_file)

    q = GraphQuery(rich_graph, workspace=Workspace.for_root(tmp_path))
    result = q.detect_changes(scope="unstaged")
    assert result.total_changed > 0


def test_detect_changes_empty_repo(rich_graph, tmp_path):
    """No git repo → no changes detected."""
    q = GraphQuery(rich_graph, workspace=Workspace.for_root(tmp_path))
    result = q.detect_changes(scope="all")
    assert result.total_changed == 0


def _repo_with_an_edit(root, rich_graph):
    """A committed ``alpha.py`` with an uncommitted edit, and every
    ``alpha`` node pointing at it by ABSOLUTE path.

    The absolute spelling is not decoration — it is what a real graph
    holds (`[M]` 2026-08-16: 16527 of 16527 ``file_path`` values in
    ORPHEUS's graph are absolute, 0 relative), and the pair below is
    blind without it. ``rich_graph`` writes relative literals
    (``"alpha.py"``), which happen to equal git's repository-relative
    output, so those nodes match whatever root they are asked about and
    the control cannot fail. Measured the hard way: the control passed
    against the intended-wrong configuration until the fixture was
    fixed.
    """
    subprocess.run(["git", "init"], cwd=root, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=root,
                   capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root,
                   capture_output=True)
    py_file = root / "alpha.py"
    py_file.write_text("def solve(): pass\n")
    subprocess.run(["git", "add", "."], cwd=root, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, capture_output=True)
    py_file.write_text("def solve(): return 42\n")
    for _, attrs in rich_graph.nodes(data=True):
        if attrs.get("file_path") == "alpha.py":
            attrs["file_path"] = str(py_file)
    return root


def test_a_git_verb_answers_about_the_repository_root(rich_graph, tmp_path):
    """The positive leg of the pair below: rooted at the checkout, the
    edit is found."""
    root = _repo_with_an_edit(tmp_path, rich_graph)
    q = GraphQuery(rich_graph, workspace=Workspace.for_root(root))
    assert q.detect_changes(scope="unstaged").total_changed > 0


def test_a_git_verb_rooted_at_a_SUBDIRECTORY_reports_nothing_changed(
    rich_graph, tmp_path,
):
    """The control, and the defect it licenses the repair for.

    ``git -C <subdir> diff --name-status`` succeeds and reports paths
    relative to the REPOSITORY root (`[M]` git 2.50.1: ``diff.relative``
    is off by default, so the cwd does not change the output), while
    the node paths are made relative to the root we were handed —
    ``relative_to`` raises, the absolute spelling is kept, and nothing
    matches. The verb answers a confident ``total_changed == 0``:
    empty and broken, indistinguishable, in the reassuring direction.

    This is why :func:`cli._workspace_for` falls back to the discovered
    project root rather than to ``Path.cwd()``: a CLI run from ``docs/``
    used to land in exactly this state. The wrong answer is asserted
    here on purpose — it is a property of asking about the wrong tree,
    not a bug to be fixed inside the verb.
    """
    root = _repo_with_an_edit(tmp_path, rich_graph)
    subdir = root / "docs"
    subdir.mkdir()

    q = GraphQuery(rich_graph, workspace=Workspace.for_root(subdir))
    assert q.detect_changes(scope="unstaged").total_changed == 0


def test_a_git_verb_refuses_a_query_that_has_no_tree(rich_graph):
    """Two arms, mutated separately: each verb that DIFFS must refuse,
    and must name itself while doing so.

    The retired signature made "no tree" expressible per call, so this
    surfaced as an empty diff or an obscure subprocess error rather than
    a refusal.
    """
    q = GraphQuery(rich_graph)
    assert q.workspace is None and q.project_root is None

    with pytest.raises(NoWorkspaceError, match="detect_changes"):
        q.detect_changes(scope="all")
    with pytest.raises(NoWorkspaceError, match="detect_changes"):
        q.retest(scope="all")


def test_a_treeless_query_still_answers_what_it_can(rich_graph):
    """The complement: verbs that merely DEGRADE without a tree must not
    refuse. ``staleness`` keeps its dead-reference signal, ``rename``
    keeps its graph half — refusing here would be the opposite defect."""
    q = GraphQuery(rich_graph)
    assert q.staleness().total_checked == 0
    assert q.rename("alpha.solve", "alpha.solve_system").total_edits > 0


# ---------------------------------------------------------------------------
# MCP server tool tests (test the tool functions directly)
# ---------------------------------------------------------------------------


def test_mcp_query_tool():
    """Test the MCP query tool function directly."""
    from sphinxcontrib.nexus import server

    g = nx.MultiDiGraph()
    g.add_node("py:function:foo", type="function", name="foo", display_name="foo()", domain="py", docname="api")
    g.add_node("py:class:Bar", type="class", name="Bar", display_name="Bar", domain="py", docname="api")

    server._query = GraphQuery(g)
    result = json.loads(server.query("foo"))
    assert len(result) > 0
    assert result[0]["id"] == "py:function:foo"

    # Cleanup
    server._query = None


def test_mcp_stats_tool():
    from sphinxcontrib.nexus import server

    g = nx.MultiDiGraph()
    g.add_node("a", type="function", name="a")
    g.add_node("b", type="class", name="b")
    g.add_edge("a", "b", type="calls")

    server._query = GraphQuery(g)
    result = json.loads(server.stats())
    assert result["node_count"] == 2
    assert result["edge_count"] == 1

    server._query = None


def test_mcp_context_tool():
    from sphinxcontrib.nexus import server

    g = nx.MultiDiGraph()
    g.add_node("a", type="function", name="a", display_name="a()", domain="py", docname="")
    g.add_node("b", type="function", name="b", display_name="b()", domain="py", docname="")
    g.add_edge("a", "b", type="calls")

    server._query = GraphQuery(g)
    result = json.loads(server.context("a"))
    assert result["node"]["id"] == "a"
    assert "calls" in result["outgoing"]

    server._query = None


def test_mcp_impact_tool():
    from sphinxcontrib.nexus import server

    g = nx.MultiDiGraph()
    g.add_node("a", type="function", name="a", display_name="a()", domain="py", docname="")
    g.add_node("b", type="function", name="b", display_name="b()", domain="py", docname="")
    g.add_edge("a", "b", type="calls")

    server._query = GraphQuery(g)
    result = json.loads(server.impact("b", direction="upstream"))
    assert result["total_affected"] > 0

    server._query = None


def test_mcp_schema_resource():
    from sphinxcontrib.nexus import server

    g = nx.MultiDiGraph()
    server._query = GraphQuery(g)
    result = json.loads(server.resource_schema())
    assert "function" in result["node_types"]
    assert "calls" in result["edge_types"]

    server._query = None


# ---------------------------------------------------------------------------
# Dream feature tests — provenance, coverage, staleness, etc.
# ---------------------------------------------------------------------------


@pytest.fixture()
def dream_graph():
    """Graph with equations, implements edges, test functions, and doc pages."""
    g = nx.MultiDiGraph()

    # Doc page
    g.add_node("std:file:theory/transport", type="file", name="theory/transport",
               display_name="Transport Theory", domain="std", docname="theory/transport")
    # Equation
    g.add_node("math:equation:alpha-recursion", type="equation", name="alpha-recursion",
               display_name="(1)", domain="math", docname="theory/transport")
    # Code
    g.add_node("py:function:sweep.sweep_spherical", type="function", name="sweep.sweep_spherical",
               display_name="sweep_spherical", domain="py", docname="api/sweep",
               file_path="sweep.py", lineno=10, source="both")
    g.add_node("py:function:sweep.helper", type="function", name="sweep.helper",
               display_name="helper", domain="py", file_path="sweep.py", lineno=30, source="ast")
    # External dep
    g.add_node("py:function:numpy.array", type="external", name="numpy.array",
               display_name="numpy.array", domain="py")
    # Test function
    g.add_node("py:function:test_sweep.test_spherical", type="function",
               name="test_sweep.test_spherical", display_name="test_spherical",
               domain="py", file_path="tests/test_sweep.py", lineno=5, is_test=True)
    # Citation
    g.add_node("std:citation:Bailey2009", type="unresolved", name="Bailey2009",
               display_name="Bailey2009", domain="std")

    # Two more equations on the SAME page — the discriminators, without
    # which this fixture cannot see nexus#72 at all.
    #
    # It carried exactly one equation, and it was the one
    # `sweep_spherical` implements, so page-contents and
    # implements-targets were the SAME SET: every assertion below passed
    # whether `provenance_chain` followed `implements` or walked the
    # page. `beta-closure` is on the page and implemented by nothing;
    # `gamma-balance` is implemented by a DECLARED edge, so declared-first
    # ordering has something to order (the original lone edge is
    # `source="inferred"`).
    g.add_node("math:equation:beta-closure", type="equation", name="beta-closure",
               display_name="(2)", domain="math", docname="theory/transport")
    g.add_node("math:equation:gamma-balance", type="equation", name="gamma-balance",
               display_name="(3)", domain="math", docname="theory/transport")

    # Edges
    g.add_edge("std:file:theory/transport", "math:equation:alpha-recursion", type="contains")
    g.add_edge("std:file:theory/transport", "math:equation:beta-closure", type="contains")
    g.add_edge("std:file:theory/transport", "math:equation:gamma-balance", type="contains")
    g.add_edge("std:file:theory/transport", "py:function:sweep.sweep_spherical", type="documents")
    g.add_edge("std:file:theory/transport", "std:citation:Bailey2009", type="cites")
    g.add_edge("py:function:sweep.sweep_spherical", "math:equation:alpha-recursion",
               type="implements", source="inferred", shared_tokens=["sweep"])
    g.add_edge("py:function:sweep.sweep_spherical", "math:equation:gamma-balance",
               type="implements", source="directive")
    g.add_edge("py:function:sweep.sweep_spherical", "py:function:sweep.helper", type="calls")
    g.add_edge("py:function:sweep.sweep_spherical", "py:function:numpy.array", type="calls")
    g.add_edge("py:function:sweep.helper", "py:function:numpy.array", type="type_uses")
    g.add_edge("py:function:test_sweep.test_spherical",
               "py:function:sweep.sweep_spherical", type="calls")

    return g


def test_provenance_chain_from_code(dream_graph):
    q = GraphQuery(dream_graph)
    result = q.provenance_chain("py:function:sweep.sweep_spherical")
    assert len(result.equations) > 0
    eq_ids = {e.id for e in result.equations}
    assert "math:equation:alpha-recursion" in eq_ids
    # Citations survive the narrowing: `cites` is minted from the doc
    # node, so the route is now code -> implements -> equation ->
    # contained_by -> page -> cites, not "every page mentioning me".
    assert "Bailey2009" in result.citations


def test_provenance_chain_from_equation(dream_graph):
    q = GraphQuery(dream_graph)
    result = q.provenance_chain("math:equation:alpha-recursion")
    assert len(result.chain) > 0
    # Should find the implementing code
    chain_ids = {s.node.id for s in result.chain}
    assert "py:function:sweep.sweep_spherical" in chain_ids


# ---------------------------------------------------------------------------
# provenance follows IMPLEMENTS, not page adjacency (nexus#72)
# ---------------------------------------------------------------------------
#
# `provenance_chain` used to walk `code <-documents- page -contains->
# every equation on that page`. Sharing a page is not implementing, and
# the gap was not marginal: on ORPHEUS one class returned 148 equations
# for the 9 it implements, and `dd-curvilinear-scalar` — implemented by
# NOTHING — reported 40 implementers, a false ALIVE.


def test_an_equation_merely_on_the_page_is_not_implemented(dream_graph):
    """The discriminating case, and the whole of nexus#72.

    `beta-closure` sits on the same page as the target and is implemented
    by nothing. Page-walking returns it; following `implements` does not.
    """
    q = GraphQuery(dream_graph)
    result = q.provenance_chain("py:function:sweep.sweep_spherical")

    eq_ids = {e.id for e in result.equations}
    assert eq_ids == {
        "math:equation:alpha-recursion",
        "math:equation:gamma-balance",
    }, "page neighbours leaked into the implements answer"
    assert "math:equation:beta-closure" not in eq_ids


def test_the_page_survives_as_its_own_labelled_bucket(dream_graph):
    """Page context is a different question, kept and labelled as one.

    It is also what makes an empty `equations` legible — *documented
    here, implementing nothing known* versus *nothing known at all*.
    """
    q = GraphQuery(dream_graph)
    result = q.provenance_chain("py:function:sweep.sweep_spherical")
    assert [p.id for p in result.also_on_these_pages] == [
        "std:file:theory/transport",
    ]


def test_a_declared_edge_sorts_first_and_is_not_marked(dream_graph):
    """#74: a guess must not read like a fact.

    Declared is the SILENT default — `inferred` absent, no `via` — so a
    fact costs no bytes and only a guess announces itself, with the
    tokens that produced it.
    """
    q = GraphQuery(dream_graph)
    result = q.provenance_chain("py:function:sweep.sweep_spherical")

    steps = [s for s in result.chain if s.edge_type == "implements"]
    assert [s.node.id for s in steps] == [
        "math:equation:gamma-balance",      # declared
        "math:equation:alpha-recursion",    # inferred
    ], "declared must sort ahead of guessed"
    assert steps[0].inferred is None and steps[0].via is None
    assert steps[1].inferred is True and steps[1].via == ["sweep"]


def test_an_equation_names_only_its_real_implementers(dream_graph):
    """The mirror direction, where the defect was worse because the step
    is literally named `implemented_by`.

    `beta-closure` shares a page with `sweep_spherical` and is implemented
    by nothing, so its implementer list must be EMPTY — the false-ALIVE
    case, `[M]` 40 phantom implementers on ORPHEUS before this.
    """
    q = GraphQuery(dream_graph)
    result = q.provenance_chain("math:equation:beta-closure")
    assert [s for s in result.chain if s.edge_type == "implemented_by"] == []

    implemented = q.provenance_chain("math:equation:gamma-balance")
    named = [s for s in implemented.chain if s.edge_type == "implemented_by"]
    assert [s.node.id for s in named] == ["py:function:sweep.sweep_spherical"]
    assert named[0].inferred is None


def test_verification_coverage(dream_graph):
    q = GraphQuery(dream_graph)
    result = q.verification_coverage()
    assert len(result.entries) > 0
    # alpha-recursion has code + test → should be "verified"
    eq_entry = next(
        (e for e in result.entries if e.node.id == "math:equation:alpha-recursion"),
        None,
    )
    assert eq_entry is not None
    assert eq_entry.status == "verified"


def test_verification_coverage_filter(dream_graph):
    q = GraphQuery(dream_graph)
    result = q.verification_coverage(status_filter="orphan_code")
    # helper has no equation → orphan_code
    orphans = {e.node.name for e in result.entries}
    assert "sweep.helper" in orphans


def test_trace_error(dream_graph):
    q = GraphQuery(dream_graph)
    result = q.trace_error("py:function:test_sweep.test_spherical")
    assert len(result.call_chain) > 0
    assert len(result.equations_on_path) > 0
    eq_ids = {e.id for e in result.equations_on_path}
    assert "math:equation:alpha-recursion" in eq_ids
    assert "Bailey2009" in result.citations


def test_migration_plan(dream_graph):
    q = GraphQuery(dream_graph)
    result = q.migration_plan("numpy")
    assert result.total_functions > 0
    assert len(result.phases) > 0
    # All functions using numpy should be in some phase
    all_funcs = {f.id for p in result.phases for f in p.functions}
    assert "py:function:sweep.sweep_spherical" in all_funcs


def test_session_briefing(dream_graph):
    q = GraphQuery(dream_graph)
    result = q.session_briefing()
    assert result.graph_stats.node_count > 0
    assert len(result.god_nodes) > 0


# ---------------------------------------------------------------------------
# session_briefing: LLM-orientation fields (id_grammar / hot_nodes / preload_hint)
# ---------------------------------------------------------------------------


def test_briefing_id_grammar_shape(dream_graph):
    q = GraphQuery(dream_graph)
    grammar = q.session_briefing().id_grammar
    assert grammar.description
    assert isinstance(grammar.examples, list)
    assert len(grammar.examples) > 0
    seen_pairs = set()
    for ex in grammar.examples:
        assert ex.id
        assert ex.type
        pair = (ex.domain, ex.type)
        assert pair not in seen_pairs, f"duplicate pair {pair}"
        seen_pairs.add(pair)


def test_briefing_id_grammar_excludes_external_and_unresolved(dream_graph):
    q = GraphQuery(dream_graph)
    grammar = q.session_briefing().id_grammar
    for ex in grammar.examples:
        assert ex.type not in ("external", "unresolved"), (
            f"external/unresolved leaked into id_grammar: {ex.id}"
        )


def test_briefing_id_grammar_round_trip(dream_graph):
    """Every example id must be directly usable by context()."""
    from sphinxcontrib.nexus._serialize import assemble_context

    q = GraphQuery(dream_graph)
    grammar = q.session_briefing().id_grammar
    for ex in grammar.examples:
        result = assemble_context(q, ex.id)
        assert "error" not in result, f"round-trip failed for {ex.id}: {result}"
        assert result["node"]["id"] == ex.id


def test_briefing_id_grammar_sorted(dream_graph):
    q = GraphQuery(dream_graph)
    grammar = q.session_briefing().id_grammar
    pairs = [(ex.domain, ex.type) for ex in grammar.examples]
    assert pairs == sorted(pairs)


def test_briefing_id_grammar_empty_graph():
    q = GraphQuery(nx.MultiDiGraph())
    grammar = q.session_briefing().id_grammar
    assert grammar.examples == []
    assert grammar.description  # description stays populated


def test_briefing_id_grammar_only_external_unresolved():
    g = nx.MultiDiGraph()
    g.add_node("py:external:foo", type="external", name="foo", domain="py")
    g.add_node("std:cite:Bar", type="unresolved", name="Bar", domain="std")
    q = GraphQuery(g)
    grammar = q.session_briefing().id_grammar
    assert grammar.examples == []


def test_briefing_hot_nodes_empty_without_project_root(dream_graph):
    q = GraphQuery(dream_graph)
    hot = q.session_briefing().hot_nodes
    assert hot.description
    assert hot.nodes == []


def _init_git_branch(tmp_path, file_rel, new_content):
    """Init a git repo, commit file, switch to feature branch, modify it."""
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, capture_output=True)
    f = tmp_path / file_rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("def placeholder(): pass\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "checkout", "-b", "feature"], cwd=tmp_path, capture_output=True)
    f.write_text(new_content)
    subprocess.run(["git", "commit", "-am", "change"], cwd=tmp_path, capture_output=True)
    return f


def test_briefing_hot_nodes_with_changes(tmp_path):
    """Recently-changed high-degree nodes should surface in hot_nodes."""
    # 5 untouched hubs (degrees 20..16) occupy god_top[:5]. changed_hub
    # has degree 5 — above graph median (1) but well below god_top — and
    # lives in a changed file, so it should appear in hot_nodes.
    g = nx.MultiDiGraph()
    for h in range(5):
        hub = f"py:function:pkg.untouched_hub{h}"
        g.add_node(hub, type="function", name=f"pkg.untouched_hub{h}", domain="py")
        for i in range(20 - h):
            leaf = f"py:function:pkg.u{h}_leaf{i}"
            g.add_node(leaf, type="function", name=leaf, domain="py")
            g.add_edge(hub, leaf, type="calls")

    changed_file = tmp_path / "pkg" / "changed.py"
    g.add_node("py:function:pkg.changed_hub", type="function",
               name="pkg.changed_hub", domain="py", file_path=str(changed_file))
    for i in range(5):
        leaf = f"py:function:pkg.c_leaf{i}"
        g.add_node(leaf, type="function", name=f"pkg.c_leaf{i}", domain="py")
        g.add_edge("py:function:pkg.changed_hub", leaf, type="calls")

    _init_git_branch(tmp_path, "pkg/changed.py", "def changed_hub(): return 1\n")

    q = GraphQuery(g, workspace=Workspace.for_root(tmp_path))
    briefing = q.session_briefing()
    hot_ids = {n.id for n in briefing.hot_nodes.nodes}
    assert "py:function:pkg.changed_hub" in hot_ids
    target = next(
        n for n in briefing.hot_nodes.nodes
        if n.id == "py:function:pkg.changed_hub"
    )
    assert target.reason == "modified in current branch"
    assert target.degree == 5
    # And must not have collided with a god_top slot.
    god_top = {n.id for n in briefing.god_nodes[:5]}
    assert "py:function:pkg.changed_hub" not in god_top


def test_briefing_hot_nodes_excludes_god_top(tmp_path):
    """A node in god_nodes[:5] must not also appear in hot_nodes."""
    hub_file = tmp_path / "pkg" / "hub.py"
    g = nx.MultiDiGraph()
    g.add_node("py:function:pkg.hub", type="function", name="pkg.hub",
               domain="py", file_path=str(hub_file))
    for i in range(3):
        leaf = f"py:function:pkg.leaf{i}"
        g.add_node(leaf, type="function", name=f"pkg.leaf{i}", domain="py")
        g.add_edge("py:function:pkg.hub", leaf, type="calls")

    _init_git_branch(tmp_path, "pkg/hub.py", "def hub(): return 1\n")

    q = GraphQuery(g, workspace=Workspace.for_root(tmp_path))
    briefing = q.session_briefing()
    god_top = {n.id for n in briefing.god_nodes[:5]}
    hot_ids = {n.id for n in briefing.hot_nodes.nodes}
    assert "py:function:pkg.hub" in god_top
    assert "py:function:pkg.hub" not in hot_ids


def test_briefing_preload_hint_is_static(dream_graph):
    q = GraphQuery(dream_graph)
    hint = q.session_briefing().preload_hint
    assert hint.description
    assert hint.tool_search_call.startswith("select:")
    expected = {
        "mcp__nexus__query", "mcp__nexus__callers", "mcp__nexus__callees",
        "mcp__nexus__context", "mcp__nexus__impact",
        "mcp__nexus__provenance_chain", "mcp__nexus__shortest_path",
        "mcp__nexus__neighbors",
    }
    selection = set(hint.tool_search_call[len("select:"):].split(","))
    assert selection == expected


def test_briefing_deterministic(dream_graph):
    """Two successive calls on an unchanged graph produce identical output."""
    from sphinxcontrib.nexus._serialize import to_dict

    q = GraphQuery(dream_graph)
    a = to_dict(q.session_briefing())
    b = to_dict(q.session_briefing())
    assert a["id_grammar"] == b["id_grammar"]
    assert a["hot_nodes"] == b["hot_nodes"]
    assert a["preload_hint"] == b["preload_hint"]


def test_briefing_backward_compatible_fields(dream_graph):
    """All pre-existing fields must still be present and unchanged in shape."""
    q = GraphQuery(dream_graph)
    result = q.session_briefing()
    for attr in (
        "graph_stats", "god_nodes", "stale_docs", "coverage_gaps",
        "recent_changes", "unresolved_count", "external_count",
    ):
        assert hasattr(result, attr)


def test_retest_with_git(dream_graph, tmp_path):
    """Retest needs a git repo; verify it runs without crashing."""
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, capture_output=True)

    f = tmp_path / "sweep.py"
    f.write_text("def sweep_spherical(): pass\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True)
    f.write_text("def sweep_spherical(): return 42\n")

    dream_graph.nodes["py:function:sweep.sweep_spherical"]["file_path"] = str(f)

    q = GraphQuery(dream_graph, workspace=Workspace.for_root(tmp_path))
    result = q.retest(scope="unstaged")
    # Should find the test that calls sweep_spherical
    must_ids = {t.id for t in result.must_retest}
    assert "py:function:test_sweep.test_spherical" in must_ids


# ── retest: the cone is walked to a fixed point (#62) ────────────────


def _retest_repo(tmp_path):
    """A git repo with one tracked, then modified, source file."""
    for cmd in (["git", "init"], ["git", "config", "user.email", "t@t.com"],
                ["git", "config", "user.name", "T"]):
        subprocess.run(cmd, cwd=tmp_path, capture_output=True)
    f = tmp_path / "kernel.py"
    f.write_text("def kernel(): pass\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True)
    f.write_text("def kernel(): return 42\n")
    return f


def _deep_cone_graph(kernel_file) -> nx.MultiDiGraph:
    """``kernel`` reached from a test FIVE hops up, and three decoys.

    The chain length is the point: with the retired ``max_depth=3`` the
    deep test is invisible and gets counted as ``safe_to_skip``. The
    decoys pin the other two claims — a test that only *references* the
    kernel is not in its dependence cone, and the class / module-level
    constant that live in a test file carry ``is_test`` but are not
    things pytest can collect.
    """
    g = nx.MultiDiGraph()
    g.add_node("py:function:kernel.kernel", type="function", name="kernel.kernel",
               domain="py", file_path=str(kernel_file), lineno=1)

    prev = "py:function:kernel.kernel"
    for i in range(1, 5):                       # h1 ← h2 ← h3 ← h4
        nid = f"py:function:pkg.h{i}"
        g.add_node(nid, type="function", name=f"pkg.h{i}", domain="py")
        g.add_edge(nid, prev, type="calls")
        prev = nid

    def _test(name, target, edge):
        nid = f"py:function:test_k.{name}"
        g.add_node(nid, type="function", name=f"test_k.{name}", domain="py",
                   is_test=True, in_test_file=True)
        g.add_edge(nid, target, type=edge)
        return nid

    _test("test_deep", prev, "calls")                                   # depth 5
    _test("test_direct", "py:function:kernel.kernel", "calls")          # depth 1
    _test("test_only_mentions", "py:function:kernel.kernel", "references")

    # `is_test` is set on these too, and pytest collects neither.
    g.add_node("py:class:test_k.TestBag", type="class", name="test_k.TestBag",
               domain="py", is_test=True, in_test_file=True)
    g.add_node("py:data:test_k.CASES", type="data", name="test_k.CASES",
               domain="py", is_test=True, in_test_file=True)
    return g


def test_a_test_five_hops_up_is_not_reported_safe_to_skip(tmp_path):
    """[M] on ORPHEUS the retired `max_depth=3` missed 232 collectable
    tests for `geometry.mesh.BC` and 18 for `warn_if_unconverged` —
    while missing 0 for `solve_sn`, whose shallow cone is exactly why a
    spot-check certified the cap."""
    q = GraphQuery(_deep_cone_graph(_retest_repo(tmp_path)),
                   workspace=Workspace.for_root(tmp_path))
    result = q.retest(scope="unstaged")

    assert {t.id for t in result.must_retest} == {"py:function:test_k.test_direct"}
    assert "py:function:test_k.test_deep" in {t.id for t in result.should_retest}
    assert result.cone_depth >= 5, result.cone_depth


def test_a_test_that_only_MENTIONS_the_change_stays_skippable(tmp_path):
    """`references` is a mention, not a dependence. [M] including it puts
    the fixed point at 78 % of ORPHEUS's suite from any starting symbol —
    "re-run everything", which is the answer that needs no tool."""
    q = GraphQuery(_deep_cone_graph(_retest_repo(tmp_path)),
                   workspace=Workspace.for_root(tmp_path))
    result = q.retest(scope="unstaged")

    reached = {t.id for t in result.must_retest} | {t.id for t in result.should_retest}
    assert "py:function:test_k.test_only_mentions" not in reached
    assert "references" not in result.dependence_edges


def test_the_suite_is_counted_in_COLLECTABLE_tests(tmp_path):
    """`is_test` is also set on classes, module data and attributes in
    test files — [M] 7305 nodes carry it on ORPHEUS and only 5273 are
    collectable, so counting by the flag inflates `safe_to_skip` by
    every one of the other 2032."""
    q = GraphQuery(_deep_cone_graph(_retest_repo(tmp_path)),
                   workspace=Workspace.for_root(tmp_path))
    result = q.retest(scope="unstaged")

    assert result.total_tests == 3, "the class and the constant are not tests"
    assert result.safe_to_skip == 1                       # test_only_mentions
