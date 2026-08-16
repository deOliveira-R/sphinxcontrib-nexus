"""The push channels must work in the environment they actually run in.

Both channels exist to deliver a finding to an agent that never asked for
it. A channel that silently produces nothing is worse than no channel: it
looks installed, reports clean, and the drift it was meant to surface goes
on being invisible.

The shipped `/doc-health` command did exactly that in its first release —
it invoked a bare `nexus`, which is not on PATH in the normal layout
(the binary lives in the project's `.venv/bin/`). Every `!` line resolved
to `command not found` and the command injected an empty report. Nothing
caught it, because nothing ever ran those lines.

So these tests EXECUTE the shipped shell, with `nexus` deliberately absent
from PATH, against a real graph.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from sphinxcontrib.nexus.export import write_sqlite
from sphinxcontrib.nexus.graph import (
    EdgeType,
    GraphEdge,
    GraphNode,
    KnowledgeGraph,
    NodeType,
)

PACKAGE = Path(__file__).resolve().parents[1] / "sphinxcontrib" / "nexus"
COMMAND = PACKAGE / "commands" / "doc-health.md"
HOOK = PACKAGE / "hooks" / "nexus-dead-refs.sh"


def _graph_with_a_dead_reference(db_path: Path) -> None:
    """A graph whose docs cite a symbol that does not exist."""
    kg = KnowledgeGraph()
    kg.add_node(GraphNode(
        id="py:module:pkg", type=NodeType.MODULE, name="pkg",
        display_name="pkg", domain="py",
        metadata={"file_path": "/x/pkg/__init__.py"},
    ))
    kg.add_node(GraphNode(
        id="py:class:pkg.mod.Thing", type=NodeType.CLASS, name="pkg.mod.Thing",
        display_name="Thing", domain="py",
        metadata={"file_path": "/x/pkg/mod.py", "lineno": 1},
    ))
    kg.add_node(GraphNode(
        id="py:class:pkg.gone.Missing", type=NodeType.UNRESOLVED,
        name="pkg.gone.Missing", display_name="Missing", domain="py",
    ))
    kg.add_edge(GraphEdge(
        source="py:class:pkg.mod.Thing", target="py:class:pkg.gone.Missing",
        type=EdgeType.REFERENCES,
    ))
    write_sqlite(kg, db_path)


def _venv_layout(tmp_path: Path) -> Path:
    """A project whose nexus lives in `.venv/bin/`, as after `nexus setup`.

    The console script is a shim onto the interpreter running the tests, so
    the shipped resolution logic is exercised without building a venv.
    """
    binary = tmp_path / ".venv" / "bin" / "nexus"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text(
        "#!/bin/sh\n"
        f'exec "{sys.executable}" -m sphinxcontrib.nexus.cli "$@"\n'
    )
    binary.chmod(0o755)
    return binary


def _shell_lines(markdown: str) -> list[str]:
    """The `` !`…` `` lines a slash command executes at invocation."""
    return re.findall(r"^!`(.+)`$", markdown, re.MULTILINE)


def _scrubbed_env(project: Path) -> dict[str, str]:
    """PATH without any `nexus`, plus the project dir the command reads.

    This is the whole point: the failure only appears when the binary is
    NOT globally installed, which is the normal case.
    """
    env = dict(os.environ)
    env["PATH"] = "/usr/bin:/bin"
    env["CLAUDE_PROJECT_DIR"] = str(project)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    return env


def test_doc_health_command_runs_without_nexus_on_path(tmp_path):
    _venv_layout(tmp_path)
    # Staged at the CONVENTION, spelled out as a literal rather than
    # derived — an independently-authored path is what keeps this an
    # external pin on the store's location instead of a tautology
    # against the same helper the hook uses.
    db = tmp_path / ".nexus" / "graph.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    _graph_with_a_dead_reference(db)

    lines = _shell_lines(COMMAND.read_text())
    assert lines, "the command has no `!` lines — it injects nothing"

    first = subprocess.run(
        ["bash", "-c", lines[0]],
        cwd=tmp_path, env=_scrubbed_env(tmp_path),
        capture_output=True, text=True,
    )
    assert first.returncode == 0, first.stderr
    # The finding itself must be present — not a "command not found" and
    # not the graceful fallback, both of which would inject nothing useful.
    assert "DEAD DOCUMENTATION REFERENCES" in first.stdout, first.stdout
    assert "pkg.gone.Missing" in first.stdout
    assert "not found" not in first.stderr.lower()


def test_doc_health_degrades_readably_when_there_is_no_graph(tmp_path):
    """A project that hasn't built docs yet must get an explanation, not a
    silent blank or a shell error."""
    _venv_layout(tmp_path)
    lines = _shell_lines(COMMAND.read_text())
    proc = subprocess.run(
        ["bash", "-c", lines[0]],
        cwd=tmp_path, env=_scrubbed_env(tmp_path),
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "not found" in proc.stdout.lower()
    assert "nexus setup" in proc.stdout


def test_hook_emits_additional_context_without_nexus_on_path(tmp_path):
    _venv_layout(tmp_path)
    # Staged at the CONVENTION, spelled out as a literal rather than
    # derived — an independently-authored path is what keeps this an
    # external pin on the store's location instead of a tautology
    # against the same helper the hook uses.
    db = tmp_path / ".nexus" / "graph.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    _graph_with_a_dead_reference(db)

    proc = subprocess.run(
        ["bash", str(HOOK)],
        cwd=tmp_path, env=_scrubbed_env(tmp_path),
        capture_output=True, text=True, input="",
    )
    assert proc.returncode == 0, proc.stderr
    assert "additionalContext" in proc.stdout
    assert "pkg.gone.Missing" in proc.stdout


def test_hook_stays_silent_on_a_clean_project(tmp_path):
    """No findings must cost zero context, or the channel trains agents to
    skim past it on the day it matters."""
    _venv_layout(tmp_path)
    # Staged at the CONVENTION, spelled out as a literal rather than
    # derived — an independently-authored path is what keeps this an
    # external pin on the store's location instead of a tautology
    # against the same helper the hook uses.
    db = tmp_path / ".nexus" / "graph.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    write_sqlite(KnowledgeGraph(), db)

    proc = subprocess.run(
        ["bash", str(HOOK)],
        cwd=tmp_path, env=_scrubbed_env(tmp_path),
        capture_output=True, text=True, input="",
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_no_shipped_channel_assumes_nexus_is_on_path():
    """Guards the class of bug rather than the instance: every shipped
    invocation must resolve the binary, never assume PATH."""
    for path in (COMMAND, HOOK):
        text = path.read_text()
        for line in _shell_lines(text) if path is COMMAND else [text]:
            assert ".venv/bin/nexus" in line, (
                f"{path.name} invokes nexus without resolving it against "
                f"the project venv; PATH is not a safe assumption"
            )
