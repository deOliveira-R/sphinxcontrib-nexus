"""Unit tests for the MCP server's graph auto-reload path.

Exercises ``_reload_if_stale`` in isolation against synthetic
databases on disk. The reload helper must:

1. Detect that the DB mtime has advanced and reload.
2. Be idempotent when called after a successful reload.
3. Survive a missing DB file (keep the previous snapshot).
4. Survive a corrupt DB / load failure (keep the previous
   snapshot, log a warning, not raise).
5. Survive a schema-version rejection (same as corrupt — keep
   serving the old snapshot).
6. Serialize concurrent calls via the module-level lock.
"""

from __future__ import annotations

import json as _json
import os
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from sphinxcontrib.nexus import server as server_mod
from sphinxcontrib.nexus.export import write_sqlite
from sphinxcontrib.nexus.graph import (
    GraphNode,
    KnowledgeGraph,
    NodeType,
)
from sphinxcontrib.nexus.query import GraphQuery


def _make_small_graph(label: str = "foo") -> KnowledgeGraph:
    kg = KnowledgeGraph()
    kg.add_node(GraphNode(
        id=f"py:function:{label}",
        type=NodeType.FUNCTION,
        name=label,
        display_name=label,
        domain="py",
    ))
    return kg


def _bump_mtime(path: Path) -> None:
    """Forcibly advance the mtime so ``_reload_if_stale`` sees
    a change even if the content was rewritten within the same
    filesystem tick."""
    now = time.time() + 1
    os.utime(path, (now, now))


@pytest.fixture()
def server_state(tmp_path, monkeypatch):
    """Reset the server module state between tests so stale ``_query``
    / ``_db_mtime`` from one test doesn't leak into the next."""
    db = tmp_path / "graph.db"
    write_sqlite(_make_small_graph("initial"), db)

    monkeypatch.setattr(server_mod, "_db_path", db)
    monkeypatch.setattr(
        server_mod, "_query",
        GraphQuery(_make_small_graph("initial")),
    )
    monkeypatch.setattr(server_mod, "_db_mtime", db.stat().st_mtime)
    yield db


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_reload_picks_up_new_mtime(server_state, monkeypatch):
    db = server_state
    # Overwrite the file with a different graph and advance mtime.
    write_sqlite(_make_small_graph("updated"), db)
    _bump_mtime(db)

    server_mod._reload_if_stale()

    # The new graph's function node should be queryable.
    q = server_mod._query
    assert q is not None
    node = q.get_node("py:function:updated")
    assert node is not None
    # And the old one is gone.
    assert q.get_node("py:function:initial") is None


def test_reload_idempotent_when_mtime_unchanged(server_state):
    q_before = server_mod._query
    server_mod._reload_if_stale()
    # Nothing changed on disk — the query object is the same instance.
    assert server_mod._query is q_before


# ---------------------------------------------------------------------------
# Failure tolerance
# ---------------------------------------------------------------------------


def test_reload_survives_missing_db(server_state, caplog):
    db = server_state
    prior = server_mod._query
    db.unlink()
    with caplog.at_level("DEBUG", logger="sphinxcontrib.nexus.server"):
        server_mod._reload_if_stale()
    # Previous snapshot is still serving.
    assert server_mod._query is prior


def test_reload_survives_corrupt_db(server_state, caplog):
    db = server_state
    prior = server_mod._query
    # Overwrite with garbage so sqlite3 can't parse it.
    db.write_bytes(b"\x00\x00\x00 not a sqlite database \x00\x00\x00")
    _bump_mtime(db)

    with caplog.at_level("WARNING", logger="sphinxcontrib.nexus.server"):
        server_mod._reload_if_stale()

    assert server_mod._query is prior
    assert "reload failed" in caplog.text.lower()


def test_reload_survives_schema_version_rejection(server_state, caplog):
    db = server_state
    prior = server_mod._query
    # Rewrite with a future schema_version so load_sqlite raises.
    write_sqlite(_make_small_graph("nextgen"), db)
    conn = sqlite3.connect(str(db))
    conn.execute(
        "UPDATE metadata SET value = ? WHERE key = 'schema_version'",
        (_json.dumps(999),),
    )
    conn.commit()
    conn.close()
    _bump_mtime(db)

    with caplog.at_level("WARNING", logger="sphinxcontrib.nexus.server"):
        server_mod._reload_if_stale()

    assert server_mod._query is prior
    assert "reload failed" in caplog.text.lower()


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def test_concurrent_reload_serializes_through_lock(server_state):
    """Two threads both detect a stale mtime and race into
    ``_reload_if_stale``. The lock ensures both see a consistent
    final ``_query`` — neither ends up reading torn state — and
    only one of them actually runs the load."""
    db = server_state
    write_sqlite(_make_small_graph("racy"), db)
    _bump_mtime(db)

    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def _runner():
        try:
            barrier.wait()
            server_mod._reload_if_stale()
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=_runner) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert not errors, errors
    q = server_mod._query
    assert q is not None
    assert q.get_node("py:function:racy") is not None
