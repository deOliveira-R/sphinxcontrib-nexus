"""Integration test for the CLI entry point."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from sphinxcontrib.nexus.cli import main
from sphinxcontrib.nexus.export import load_sqlite, write_sqlite
from sphinxcontrib.nexus.graph import KnowledgeGraph


def test_cli_analyze(tmp_path):
    """Create a small project, run CLI, verify output."""
    # Create source files
    src = tmp_path / "src"
    src.mkdir()
    (src / "alpha.py").write_text(
        "import numpy as np\n\n"
        "def compute(x: np.ndarray) -> float:\n"
        "    return x.sum()\n"
    )
    (src / "beta.py").write_text(
        "from alpha import compute\n\n"
        "class Solver:\n"
        "    def run(self):\n"
        "        self.compute()\n"
        "    def compute(self):\n"
        "        pass\n"
    )

    db_path = tmp_path / "graph.db"
    result = main(["analyze", str(src), "--db", str(db_path)])
    assert result == 0
    assert db_path.exists()

    # Load and verify
    kg = load_sqlite(db_path)
    nids = set(kg.nxgraph.nodes)
    assert "py:module:alpha" in nids
    assert "py:module:beta" in nids
    assert "py:function:alpha.compute" in nids
    assert "py:class:beta.Solver" in nids

    # Verify edge types present
    edge_types = {d.get("type") for _, _, d in kg.nxgraph.edges(data=True)}
    assert "imports" in edge_types
    assert "contains" in edge_types
    assert "type_uses" in edge_types


def test_cli_no_args():
    """CLI with no arguments should return 1 (help)."""
    assert main([]) == 1


# ------------------------------------------------------------------
# JSON CLI subcommands (Phase 1: CLI parity with MCP tools)
# ------------------------------------------------------------------


@pytest.fixture()
def small_graph(tmp_path):
    """Build a small graph with enough structure to exercise all commands."""
    src = tmp_path / "src"
    src.mkdir()

    # Module with function, class, imports
    (src / "solver.py").write_text(
        "import numpy as np\n\n"
        "def solve(x: np.ndarray) -> float:\n"
        "    return _helper(x)\n\n"
        "def _helper(x):\n"
        "    return x.sum()\n"
    )
    (src / "runner.py").write_text(
        "from solver import solve\n\n"
        "class Runner:\n"
        "    def run(self):\n"
        "        return solve([1, 2, 3])\n"
    )

    db_path = tmp_path / "graph.db"
    assert main(["analyze", str(src), "--db", str(db_path)]) == 0
    return db_path


def _cli_json(args: list[str], capsys) -> dict | list:
    """Run CLI, capture stdout, parse JSON."""
    result = main(args)
    assert result == 0
    captured = capsys.readouterr()
    return json.loads(captured.out)


class TestJsonCli:
    """Each test verifies the CLI exits 0 and returns valid JSON."""

    def test_briefing(self, small_graph, capsys):
        data = _cli_json(["briefing", "--db", str(small_graph)], capsys)
        assert "graph_stats" in data
        assert data["graph_stats"]["node_count"] > 0

    def test_context_found(self, small_graph, capsys):
        data = _cli_json(
            ["context", "py:function:solver.solve", "--db", str(small_graph)],
            capsys,
        )
        assert data["node"]["id"] == "py:function:solver.solve"
        assert "outgoing" in data
        assert "incoming" in data

    def test_context_not_found(self, small_graph, capsys):
        data = _cli_json(
            ["context", "py:function:nonexistent", "--db", str(small_graph)],
            capsys,
        )
        assert "error" in data

    def test_neighbors(self, small_graph, capsys):
        data = _cli_json(
            ["neighbors", "py:function:solver.solve", "--db", str(small_graph)],
            capsys,
        )
        assert isinstance(data, list)
        for entry in data:
            assert "node" in entry
            assert "edge" in entry

    def test_neighbors_direction(self, small_graph, capsys):
        data = _cli_json(
            ["neighbors", "py:function:solver.solve", "--direction", "out",
             "--db", str(small_graph)],
            capsys,
        )
        assert isinstance(data, list)

    def test_god_nodes(self, small_graph, capsys):
        data = _cli_json(
            ["god-nodes", "--db", str(small_graph), "--top-n", "3"],
            capsys,
        )
        assert isinstance(data, list)
        assert len(data) <= 3

    def test_communities(self, small_graph, capsys):
        data = _cli_json(
            ["communities", "--db", str(small_graph), "--min-size", "2"],
            capsys,
        )
        assert isinstance(data, list)

    def test_bridges(self, small_graph, capsys):
        data = _cli_json(
            ["bridges", "--db", str(small_graph), "--top-n", "3"],
            capsys,
        )
        assert isinstance(data, list)

    def test_processes(self, small_graph, capsys):
        data = _cli_json(
            ["processes", "--db", str(small_graph), "--min-length", "2"],
            capsys,
        )
        assert isinstance(data, list)

    def test_graph_query(self, small_graph, capsys):
        data = _cli_json(
            ["graph-query", "* -calls-> *", "--db", str(small_graph)],
            capsys,
        )
        assert isinstance(data, list)

    def test_shortest_path_found(self, small_graph, capsys):
        # runner.Runner → solve via calls chain
        data = _cli_json(
            ["shortest-path",
             "py:class:runner.Runner",
             "py:function:solver.solve",
             "--db", str(small_graph)],
            capsys,
        )
        # May or may not find a path depending on graph structure
        assert isinstance(data, dict)

    def test_shortest_path_not_found(self, small_graph, capsys):
        data = _cli_json(
            ["shortest-path",
             "py:function:solver.solve",
             "py:function:nonexistent.xyz",
             "--db", str(small_graph)],
            capsys,
        )
        assert data.get("error") == "No path found"

    def test_rename_dry_run(self, small_graph, capsys):
        data = _cli_json(
            ["rename", "solve", "compute", "--db", str(small_graph)],
            capsys,
        )
        assert "old_name" in data
        assert data["old_name"] == "solve"
        assert data["new_name"] == "compute"

    def test_trace(self, small_graph, capsys):
        data = _cli_json(
            ["trace", "py:function:solver.solve", "--db", str(small_graph)],
            capsys,
        )
        assert isinstance(data, dict)
