"""The usage journal — the server's self-observation channel.

Every MCP tool call appends one JSON line so tool adoption can be
evaluated from recorded behavior. Journaling must never interfere
with the tool call itself.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from sphinxcontrib.nexus import server as server_mod  # noqa: E402
from sphinxcontrib.nexus.graph import KnowledgeGraph  # noqa: E402
from sphinxcontrib.nexus.query import GraphQuery  # noqa: E402
from sphinxcontrib.nexus.workspace import Workspace  # noqa: E402


@pytest.fixture()
def journal(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "usage.jsonl"
    monkeypatch.setenv(server_mod.USAGE_JOURNAL_ENV, str(path))
    monkeypatch.setattr(server_mod, "_query", GraphQuery(KnowledgeGraph()))
    monkeypatch.setattr(
        server_mod, "_workspace",
        Workspace(db_path=tmp_path / "graph.db", root=tmp_path),
    )
    monkeypatch.setattr(server_mod, "_db_mtime", 0.0)
    return path


def _lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_tool_call_is_journaled(journal):
    server_mod.stats()
    (record,) = _lines(journal)
    assert record["tool"] == "stats"
    assert record["outcome"] == "ok"
    assert record["ms"] >= 0
    assert record["workspace"] == str(journal.parent)


def test_async_tool_is_journaled(journal):
    class _NoRoots:
        @property
        def session(self):
            return self

        async def list_roots(self):
            raise RuntimeError("no roots support")

    asyncio.run(server_mod.session_briefing(_NoRoots()))
    tools = [r["tool"] for r in _lines(journal)]
    assert "session_briefing" in tools


def test_exception_outcome_is_journaled(journal, monkeypatch):
    monkeypatch.setattr(server_mod, "_query", None)
    with pytest.raises(RuntimeError):
        server_mod.stats()
    (record,) = _lines(journal)
    assert record["outcome"] == "exception"


def test_empty_env_disables_journal(journal, monkeypatch):
    monkeypatch.setenv(server_mod.USAGE_JOURNAL_ENV, "")
    server_mod.stats()
    assert not journal.exists()


def test_journal_failure_never_breaks_the_tool(journal, monkeypatch):
    # Point the journal at an unwritable location: a path whose parent
    # is a FILE, so mkdir(parents=True) raises inside _journal_usage.
    blocker = journal.parent / "blocker"
    blocker.write_text("x")
    monkeypatch.setenv(server_mod.USAGE_JOURNAL_ENV, str(blocker / "usage.jsonl"))
    server_mod.stats()  # must not raise
