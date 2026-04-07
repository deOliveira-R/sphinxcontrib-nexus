"""Integration tests using a real Sphinx build."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture()
def rootdir():
    return Path(__file__).parent / "roots"


@pytest.fixture()
def content(make_app, rootdir, tmp_path):
    srcdir = rootdir / "test-basic"
    app = make_app("html", srcdir=srcdir, freshenv=True, builddir=tmp_path / "build")
    app.build()
    assert not app._warning.getvalue(), f"Sphinx warnings: {app._warning.getvalue()}"
    return app


@pytest.fixture()
def math_content(make_app, rootdir, tmp_path):
    srcdir = rootdir / "test-math"
    app = make_app("html", srcdir=srcdir, freshenv=True, builddir=tmp_path / "build")
    app.build()
    assert not app._warning.getvalue(), f"Sphinx warnings: {app._warning.getvalue()}"
    return app


def _load_graph_json(app):
    outdir = Path(app.outdir)
    graph_path = outdir / "_nexus" / "graph.json"
    assert graph_path.exists(), f"Expected {graph_path} to exist"
    return json.loads(graph_path.read_text())


def _sqlite_path(app) -> Path:
    return Path(app.outdir) / "_nexus" / "graph.db"


# --- Basic extraction tests ---


def test_graph_json_created(content):
    data = _load_graph_json(content)
    assert data["directed"] is True
    assert len(data["nodes"]) > 0
    assert len(data["edges"]) > 0


def test_graph_sqlite_created(content):
    db_path = _sqlite_path(content)
    assert db_path.exists(), f"Expected {db_path} to exist"
    assert db_path.stat().st_size > 0


def test_document_nodes(content):
    data = _load_graph_json(content)
    node_ids = {n["id"] for n in data["nodes"]}
    assert "doc:index" in node_ids
    assert "doc:module" in node_ids


def test_python_domain_nodes(content):
    data = _load_graph_json(content)
    node_ids = {n["id"] for n in data["nodes"]}
    assert "py:function:mymodule.compute" in node_ids
    assert "py:class:mymodule.Widget" in node_ids
    assert "py:method:mymodule.Widget.run" in node_ids
    assert "py:module:mymodule" in node_ids


def test_contains_edges(content):
    data = _load_graph_json(content)
    contains = [
        (e["source"], e["target"])
        for e in data["edges"]
        if e["type"] == "contains"
    ]
    assert ("doc:index", "doc:module") in contains
    assert ("doc:module", "py:function:mymodule.compute") in contains


def test_reference_edges(content):
    data = _load_graph_json(content)
    ref_targets = {e["target"] for e in data["edges"] if e["type"] != "contains"}
    assert "py:function:mymodule.compute" in ref_targets


def test_glossary_term(content):
    data = _load_graph_json(content)
    node_ids = {n["id"] for n in data["nodes"]}
    assert "std:term:widget" in node_ids


# --- Math equation tests ---


def test_math_equation_nodes(math_content):
    data = _load_graph_json(math_content)
    node_ids = {n["id"] for n in data["nodes"]}
    assert "math:equation:diffusion-eq" in node_ids


def test_math_equation_ref_edges(math_content):
    data = _load_graph_json(math_content)
    eq_ref_edges = [
        e for e in data["edges"] if e["type"] == "equation_ref"
    ]
    assert len(eq_ref_edges) > 0
    targets = {e["target"] for e in eq_ref_edges}
    assert "math:equation:diffusion-eq" in targets


# --- Graph stored on env (no globals) ---


def test_graph_on_env(content):
    """Verify graph is stored on env, not a module global."""
    graph = getattr(content.env, "nexus_graph", None)
    assert graph is not None
    assert graph.node_count > 0


# --- NetworkX round-trip ---


def test_export_loadable(content):
    """Verify exported JSON loads into NetworkX."""
    import networkx as nx

    data = _load_graph_json(content)
    g = nx.node_link_graph(data)
    assert g.number_of_nodes() > 0
    assert g.number_of_edges() > 0


# --- SQLite round-trip ---


def test_sqlite_round_trip(content):
    """Load from SQLite, verify node/edge counts match JSON."""
    from sphinxcontrib.nexus.export import load_sqlite

    data = _load_graph_json(content)
    kg = load_sqlite(_sqlite_path(content))
    assert kg.nxgraph.number_of_nodes() == len(data["nodes"])
    assert kg.nxgraph.number_of_edges() == len(data["edges"])


def test_sqlite_fts_search(content):
    """Verify FTS5 keyword search works on SQLite."""
    import sqlite3

    conn = sqlite3.connect(str(_sqlite_path(content)))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id FROM nodes WHERE rowid IN "
        "(SELECT rowid FROM nodes_fts WHERE nodes_fts MATCH ?)",
        ("compute",),
    ).fetchall()
    conn.close()
    ids = {r["id"] for r in rows}
    assert "py:function:mymodule.compute" in ids


def test_sqlite_neighbor_query(content):
    """Verify indexed neighbor lookup works directly on SQLite."""
    import sqlite3

    conn = sqlite3.connect(str(_sqlite_path(content)))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT target, type FROM edges WHERE source = ?",
        ("doc:module",),
    ).fetchall()
    conn.close()
    targets = {r["target"] for r in rows}
    assert "py:function:mymodule.compute" in targets


# --- Query from build ---


def test_query_from_build(content):
    from sphinxcontrib.nexus.query import GraphQuery

    graph = content.env.nexus_graph
    q = GraphQuery(graph)

    results = q.neighbors("doc:module", direction="out", edge_types=["contains"])
    target_ids = {r[0].id for r in results}
    assert "py:function:mymodule.compute" in target_ids


def test_stats_from_build(content):
    from sphinxcontrib.nexus.query import GraphQuery

    graph = content.env.nexus_graph
    q = GraphQuery(graph)
    s = q.stats()
    assert s.node_count > 0
    assert s.edge_count > 0
    assert "file" in s.nodes_by_type
    assert "contains" in s.edges_by_type
