"""Integration test for the CLI entry point."""

from __future__ import annotations

from pathlib import Path

from sphinxcontrib.nexus.cli import main
from sphinxcontrib.nexus.export import load_sqlite


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
