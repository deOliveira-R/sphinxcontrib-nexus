"""Tests for community detection, detect_changes, rename, and MCP server."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import networkx as nx
import pytest

from sphinxcontrib.nexus.query import GraphQuery


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

    q = GraphQuery(rich_graph)
    result = q.rename("solve", "solve_system", project_root=tmp_path)
    # Should find regex matches in the test file
    regex_edits = [e for e in result.edits if e.confidence == "medium"]
    assert len(regex_edits) > 0


def test_rename_apply(rich_graph, tmp_path):
    src = tmp_path / "code.py"
    src.write_text("def solve():\n    pass\n\nresult = solve()\n")

    q = GraphQuery(rich_graph)
    q.rename("solve", "solve_system", project_root=tmp_path, dry_run=False)

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

    q = GraphQuery(rich_graph)
    result = q.detect_changes(tmp_path, scope="unstaged")
    assert result.total_changed > 0


def test_detect_changes_empty_repo(rich_graph, tmp_path):
    """No git repo → no changes detected."""
    q = GraphQuery(rich_graph)
    result = q.detect_changes(tmp_path, scope="all")
    assert result.total_changed == 0


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
    g.add_node("doc:theory/transport", type="file", name="theory/transport",
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

    # Edges
    g.add_edge("doc:theory/transport", "math:equation:alpha-recursion", type="contains")
    g.add_edge("doc:theory/transport", "py:function:sweep.sweep_spherical", type="documents")
    g.add_edge("doc:theory/transport", "std:citation:Bailey2009", type="cites")
    g.add_edge("py:function:sweep.sweep_spherical", "math:equation:alpha-recursion",
               type="implements", source="inferred")
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
    assert "Bailey2009" in result.citations


def test_provenance_chain_from_equation(dream_graph):
    q = GraphQuery(dream_graph)
    result = q.provenance_chain("math:equation:alpha-recursion")
    assert len(result.chain) > 0
    # Should find the implementing code
    chain_ids = {s.node.id for s in result.chain}
    assert "py:function:sweep.sweep_spherical" in chain_ids


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

    q = GraphQuery(dream_graph)
    result = q.retest(tmp_path, scope="unstaged")
    # Should find the test that calls sweep_spherical
    must_ids = {t.id for t in result.must_retest}
    assert "py:function:test_sweep.test_spherical" in must_ids
