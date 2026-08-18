"""Integration test for the CLI entry point."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from sphinxcontrib.nexus.cli import main
from sphinxcontrib.nexus.export import load_sqlite, write_sqlite
from sphinxcontrib.nexus.graph import GraphNode, KnowledgeGraph


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
        # One flat entry per neighbour: the node, plus the relation and
        # which way it points. The `{"node": …, "edge": …}` pair this
        # replaced spent 46 % of the reply restating the question.
        assert data, "fixture produced no neighbours"
        for entry in data:
            assert {"id", "edge_type", "direction"} <= set(entry), entry
            assert entry["direction"] in ("in", "out")
            assert not {"node", "edge", "source", "target", "key"} & set(entry)

    def test_neighbors_direction(self, small_graph, capsys):
        data = _cli_json(
            ["neighbors", "py:function:solver.solve", "--direction", "out",
             "--db", str(small_graph)],
            capsys,
        )
        assert isinstance(data, list)
        # The CLI passes `direction` through, so the field the caller
        # pinned is not repeated on every entry.
        assert data, "fixture produced no outgoing neighbours"
        assert all("direction" not in e for e in data), data

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
        # ``assemble_processes`` now returns a dict with pagination
        # metadata plus a ``processes`` list (see tests/test_serialize.py).
        assert isinstance(data, dict)
        assert "processes" in data
        assert isinstance(data["processes"], list)
        assert data["limit"] is None
        assert data["returned"] == data["total"]

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


# ------------------------------------------------------------------
# Token budgets: context --limit-per-type / impact --limit-per-depth
# ------------------------------------------------------------------


@pytest.fixture()
def hub_graph(tmp_path):
    """The test_serialize hub graph (30 callers of one node) as sqlite."""
    from test_serialize import _build_hub_graph

    db_path = tmp_path / "hub.db"
    write_sqlite(_build_hub_graph(n_callers=30), db_path)
    return db_path


class TestTokenBudgetCli:
    HUB = "py:function:mod.hub"

    def test_context_caps_by_default(self, hub_graph, capsys):
        data = _cli_json(["context", self.HUB, "--db", str(hub_graph)], capsys)
        assert len(data["incoming"]["calls"]) == 25
        assert data["omitted"] == {"incoming": {"calls": 5}}

    def test_context_limit_zero_uncaps(self, hub_graph, capsys):
        data = _cli_json(
            ["context", self.HUB, "--db", str(hub_graph), "--limit-per-type", "0"],
            capsys,
        )
        assert len(data["incoming"]["calls"]) == 30
        assert "omitted" not in data

    def test_impact_caps_and_reports_omissions(self, hub_graph, capsys):
        assert main([
            "impact", self.HUB, "--db", str(hub_graph), "--limit-per-depth", "5",
        ]) == 0
        out = capsys.readouterr().out
        assert "... (+25 more" in out
        # 30 callers at depth 1 + 10 meta-callers at depth 2: the true
        # total survives the cap
        assert "Total affected: 40" in out

    def test_impact_limit_zero_uncaps(self, hub_graph, capsys):
        assert main([
            "impact", self.HUB, "--db", str(hub_graph), "--limit-per-depth", "0",
        ]) == 0
        out = capsys.readouterr().out
        assert "more" not in out
        assert out.count("py:function:mod.caller_") == 30


class TestRuntimeIngestReportsAZeroJoin:
    """`runtime-ingest` must never report a failed join as a success.

    It used to print `nodes: 0 / edges: 0 / unresolved: 0` and exit 0,
    which is indistinguishable from a workload that genuinely touched
    nothing indexed — and it pointed the reassuring way, so a consumer
    read it as a measurement.
    """

    @staticmethod
    def _graph_with_foo(src: str) -> KnowledgeGraph:
        kg = KnowledgeGraph()
        kg.add_node(GraphNode(
            id="py:function:mod.foo", type="function", name="mod.foo",
            metadata={"file_path": src, "lineno": 1, "end_lineno": 5},
        ))
        return kg

    @pytest.fixture
    def project(self, tmp_path):
        """A graph with one function, plus a coverage report keyed as
        coverage.py actually keys one: RELATIVE to the run directory."""
        kg = self._graph_with_foo(str(tmp_path / "pkg" / "mod.py"))
        db = tmp_path / "graph.db"
        write_sqlite(kg, db)
        art = tmp_path / "cov.json"
        art.write_text(json.dumps({
            "meta": {"format": 3, "branch_coverage": True},
            "files": {"pkg/mod.py": {"executed_lines": [1, 2],
                                     "missing_lines": []}},
        }))
        return db, art, tmp_path

    def test_a_bound_ingest_succeeds_and_is_stored(self, project, capsys):
        """Control — without it, the failure test could pass vacuously."""
        db, art, root = project
        rc = main(["runtime-ingest", str(art), "--kind", "coverage",
                   "--db", str(db), "--run", "ok", "--root", str(root)])
        out = capsys.readouterr().out
        assert rc == 0
        assert "nodes:      1" in out
        assert (db.parent / "traces" / "ok.json").exists()

    def test_a_zero_join_exits_nonzero_and_stores_nothing(self, project, capsys):
        """Identical input; only `--root` differs from the control."""
        db, art, _root = project
        rc = main(["runtime-ingest", str(art), "--kind", "coverage",
                   "--db", str(db), "--run", "bad", "--root", "/nowhere"])
        captured = capsys.readouterr()
        assert rc == 1, "a join that bound nothing is a FAILED ingest"
        assert "different key spaces" in captured.err
        assert not (db.parent / "traces" / "bad.json").exists(), (
            "an empty run must not be stored — it would appear in "
            "runtime-runs and answer every query with a confident "
            "'nothing fired'"
        )

    def test_the_ledger_accounts_for_every_lookup(self, project, capsys):
        db, art, _root = project
        main(["runtime-ingest", str(art), "--kind", "coverage",
              "--db", str(db), "--run", "bad", "--root", "/nowhere"])
        out = capsys.readouterr().out
        assert "lookups:    1" in out
        assert "file not in graph:  1" in out

    def test_viztracer_reaches_its_own_backend(self, tmp_path, capsys):
        """`--kind viztracer` is an advertised choice.

        The dispatch was a BINARY `cprofile if ... else coverage`, so a
        viztracer artifact was fed to the coverage ingester, bound
        nothing, and reported success — the same defect as above, one
        layer up. A registered choice with no arm is not a choice.
        """
        src = str(tmp_path / "pkg" / "mod.py")
        kg = self._graph_with_foo(src)
        db = tmp_path / "graph.db"
        write_sqlite(kg, db)
        art = tmp_path / "viz.json"
        art.write_text(json.dumps({"traceEvents": [
            {"ph": "X", "name": f"foo ({src}:1)", "ts": 1000.0, "dur": 5.0},
        ]}))

        rc = main(["runtime-ingest", str(art), "--kind", "viztracer",
                   "--db", str(db), "--run", "v"])
        out = capsys.readouterr().out
        assert rc == 0
        # The count must come from the family THIS kind fills. The old
        # `len(calls) or len(coverage)` read 0 for a successful viztracer
        # run, because viztracer fills `timeline`.
        assert "nodes:      1" in out


class TestRetestRefusesTheWrongRun:
    """The CLI's half of the wrong-run refusal.

    The MCP server has refused a run that cannot carry a family since
    2026-08-16; the CLI refused only a MISSING run, so a wrong-KIND one
    was answered as though nothing were covered — `lessons-L56`
    surviving on the surface that did not share the author. Both now
    call one `runtime.require_family`.
    """

    def _project(self, tmp_path):
        from sphinxcontrib.nexus.runtime import RuntimeRun, RuntimeStore
        kg = KnowledgeGraph()
        kg.add_node(GraphNode(
            id="py:function:m.f", type="function", name="m.f",
            metadata={"file_path": str(tmp_path / "m.py"), "lineno": 1},
        ))
        db = tmp_path / "graph.db"
        write_sqlite(kg, db)
        store = RuntimeStore.beside(db)
        # A cProfile run: real, stored, and structurally unable to say
        # which test executed anything.
        store.write(RuntimeRun(name="prof", kind="cprofile",
                               calls={"py:function:m.f": {
                                   "ncalls": 1, "tottime": 0.1, "cumtime": 0.1}}))
        return db

    def test_a_run_that_cannot_carry_attribution_is_REFUSED(self, tmp_path, capsys):
        db = self._project(tmp_path)
        with pytest.raises(SystemExit) as exc:
            main(["retest", "--db", str(db), "--project-root", str(tmp_path),
                  "--run", "prof"])
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "exercised_by" in err and "wrong run" in err
        assert "retest" in err, "the refusal names the view that asked"

    def test_a_MISSING_run_is_still_refused_by_name(self, tmp_path, capsys):
        db = self._project(tmp_path)
        with pytest.raises(SystemExit) as exc:
            main(["retest", "--db", str(db), "--project-root", str(tmp_path),
                  "--run", "nope"])
        assert exc.value.code == 1
        assert "no runtime run 'nope'" in capsys.readouterr().err
