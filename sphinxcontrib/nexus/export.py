"""Export and import KnowledgeGraph as JSON and SQLite."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import networkx as nx

from sphinxcontrib.nexus.graph import KnowledgeGraph

# ---------------------------------------------------------------------------
# JSON export/import (kept for debugging and interop)
# ---------------------------------------------------------------------------


def graph_to_dict(graph: KnowledgeGraph) -> dict:
    """Convert graph to networkx node-link format."""
    data = nx.node_link_data(graph.nxgraph, edges="edges")
    data["graph"] = graph.metadata
    return data


def dict_to_graph(data: dict) -> KnowledgeGraph:
    """Load a KnowledgeGraph from networkx node-link format."""
    nxg = nx.node_link_graph(data, edges="edges")
    kg = KnowledgeGraph()
    kg._graph = nxg
    kg.metadata = data.get("graph", {})
    return kg


def write_json(graph: KnowledgeGraph, path: Path) -> None:
    """Write graph to a JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = graph_to_dict(graph)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str))


def load_json(path: Path) -> KnowledgeGraph:
    """Load graph from a JSON file."""
    data = json.loads(path.read_text())
    return dict_to_graph(data)


# ---------------------------------------------------------------------------
# SQLite export/import (primary format for performance)
# ---------------------------------------------------------------------------

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS nodes (
    id          TEXT PRIMARY KEY,
    type        TEXT NOT NULL DEFAULT '',
    name        TEXT NOT NULL DEFAULT '',
    display_name TEXT NOT NULL DEFAULT '',
    domain      TEXT NOT NULL DEFAULT '',
    docname     TEXT NOT NULL DEFAULT '',
    anchor      TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS node_attrs (
    node_id TEXT NOT NULL,
    key     TEXT NOT NULL,
    value   TEXT NOT NULL,
    PRIMARY KEY (node_id, key),
    FOREIGN KEY (node_id) REFERENCES nodes(id)
);
CREATE TABLE IF NOT EXISTS edges (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    source  TEXT NOT NULL,
    target  TEXT NOT NULL,
    type    TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS edge_attrs (
    edge_id INTEGER NOT NULL,
    key     TEXT NOT NULL,
    value   TEXT NOT NULL,
    PRIMARY KEY (edge_id, key),
    FOREIGN KEY (edge_id) REFERENCES edges(id)
);
CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target);
CREATE INDEX IF NOT EXISTS idx_edges_type   ON edges(type);
CREATE INDEX IF NOT EXISTS idx_nodes_type   ON nodes(type);
CREATE INDEX IF NOT EXISTS idx_nodes_domain ON nodes(domain);
"""

_FTS_SCHEMA = """\
CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
    name, display_name, content=nodes, content_rowid=rowid
);
INSERT INTO nodes_fts(nodes_fts) VALUES('rebuild');
"""

_NODE_CORE_FIELDS = {"id", "type", "name", "display_name", "domain", "docname", "anchor"}


def write_sqlite(graph: KnowledgeGraph, path: Path) -> None:
    """Write graph to a SQLite database with indexes and FTS."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()

    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_SCHEMA)

        # Metadata
        for key, value in graph.metadata.items():
            conn.execute(
                "INSERT INTO metadata (key, value) VALUES (?, ?)",
                (key, json.dumps(value, default=str)),
            )

        # Nodes
        g = graph.nxgraph
        for node_id, attrs in g.nodes(data=True):
            conn.execute(
                "INSERT INTO nodes (id, type, name, display_name, domain, docname, anchor) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(node_id),
                    str(attrs.get("type", "")),
                    str(attrs.get("name", "")),
                    str(attrs.get("display_name", "")),
                    str(attrs.get("domain", "")),
                    str(attrs.get("docname", "")),
                    str(attrs.get("anchor", "")),
                ),
            )
            # Extra attributes → node_attrs
            for key, value in attrs.items():
                if key not in _NODE_CORE_FIELDS:
                    conn.execute(
                        "INSERT INTO node_attrs (node_id, key, value) VALUES (?, ?, ?)",
                        (str(node_id), key, json.dumps(value, default=str)),
                    )

        # Edges
        for source, target, _key, attrs in g.edges(keys=True, data=True):
            cursor = conn.execute(
                "INSERT INTO edges (source, target, type) VALUES (?, ?, ?)",
                (str(source), str(target), str(attrs.get("type", ""))),
            )
            edge_id = cursor.lastrowid
            for key, value in attrs.items():
                if key != "type":
                    conn.execute(
                        "INSERT INTO edge_attrs (edge_id, key, value) VALUES (?, ?, ?)",
                        (edge_id, key, json.dumps(value, default=str)),
                    )

        # FTS index for keyword search
        conn.executescript(_FTS_SCHEMA)

        conn.commit()
    finally:
        conn.close()


def load_sqlite(path: Path) -> KnowledgeGraph:
    """Load graph from a SQLite database into a KnowledgeGraph."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        kg = KnowledgeGraph()

        # Metadata
        for row in conn.execute("SELECT key, value FROM metadata"):
            kg.metadata[row["key"]] = json.loads(row["value"])

        g = kg.nxgraph

        # Nodes
        for row in conn.execute("SELECT * FROM nodes"):
            attrs = {k: row[k] for k in row.keys() if k != "id" and row[k]}
            g.add_node(row["id"], **attrs)

        # Node extra attributes
        for row in conn.execute("SELECT node_id, key, value FROM node_attrs"):
            if row["node_id"] in g:
                g.nodes[row["node_id"]][row["key"]] = json.loads(row["value"])

        # Edges — track the NetworkX key for each SQLite edge_id
        edge_nx_keys: dict[int, tuple[str, str, int]] = {}
        for row in conn.execute("SELECT id, source, target, type FROM edges"):
            nx_key = g.add_edge(row["source"], row["target"], type=row["type"])
            edge_nx_keys[row["id"]] = (row["source"], row["target"], nx_key)

        # Edge extra attributes — restore to the correct edge via tracked key
        for row in conn.execute("SELECT edge_id, key, value FROM edge_attrs"):
            eid = row["edge_id"]
            if eid in edge_nx_keys:
                src, tgt, nx_key = edge_nx_keys[eid]
                g[src][tgt][nx_key][row["key"]] = json.loads(row["value"])

        return kg
    finally:
        conn.close()


def get_connection(path: Path) -> sqlite3.Connection:
    """Get a read-only SQLite connection for direct queries.

    Use this for high-performance queries that don't need the full
    NetworkX graph in memory (neighbors, impact at small depth).
    """
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn
