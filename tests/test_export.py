"""Unit tests for JSON and SQLite export/import round-trip."""

import pytest

from sphinxcontrib.nexus.export import (
    dict_to_graph,
    graph_to_dict,
    load_json,
    load_sqlite,
    write_json,
    write_sqlite,
)
from sphinxcontrib.nexus.graph import (
    EdgeType,
    GraphEdge,
    GraphNode,
    KnowledgeGraph,
    NodeType,
)
from sphinxcontrib.nexus.query import GraphQuery


def _make_graph() -> KnowledgeGraph:
    kg = KnowledgeGraph()
    kg.metadata = {"project": "test", "version": "1.0"}
    kg.add_node(GraphNode(
        id="doc:index", type=NodeType.FILE, name="index",
        display_name="Index", domain="std", docname="index",
    ))
    kg.add_node(GraphNode(
        id="py:function:foo", type=NodeType.FUNCTION, name="foo",
        display_name="foo()", domain="py", docname="api",
    ))
    kg.add_node(GraphNode(
        id="math:equation:euler", type=NodeType.EQUATION, name="euler",
        display_name="(1)", domain="math", docname="theory",
        metadata={"eqno": 1},
    ))
    kg.add_edge(GraphEdge(
        source="doc:index", target="py:function:foo",
        type=EdgeType.CONTAINS,
    ))
    kg.add_edge(GraphEdge(
        source="doc:index", target="math:equation:euler",
        type=EdgeType.EQUATION_REF,
        metadata={"reftype": "eq", "resolved": True},
    ))
    return kg


def test_graph_to_dict_format():
    kg = _make_graph()
    data = graph_to_dict(kg)
    assert data["directed"] is True
    assert data["multigraph"] is True
    assert "nodes" in data
    assert "edges" in data  # networkx 3.x uses "edges"
    assert data["graph"]["project"] == "test"


def test_graph_to_dict_node_count():
    kg = _make_graph()
    data = graph_to_dict(kg)
    assert len(data["nodes"]) == 3


def test_graph_to_dict_edge_count():
    kg = _make_graph()
    data = graph_to_dict(kg)
    assert len(data["edges"]) == 2


def test_round_trip_via_dict():
    kg = _make_graph()
    data = graph_to_dict(kg)
    kg2 = dict_to_graph(data)
    assert kg2.nxgraph.number_of_nodes() == 3
    assert kg2.nxgraph.number_of_edges() == 2
    assert kg2.metadata["project"] == "test"
    # Check node attributes survived
    assert kg2.nxgraph.nodes["py:function:foo"]["type"] == "function"
    assert kg2.nxgraph.nodes["math:equation:euler"]["eqno"] == 1


def test_round_trip_via_json(tmp_path):
    kg = _make_graph()
    path = tmp_path / "graph.json"
    write_json(kg, path)
    assert path.exists()

    kg2 = load_json(path)
    assert kg2.nxgraph.number_of_nodes() == 3
    assert kg2.nxgraph.number_of_edges() == 2
    assert kg2.nxgraph.nodes["doc:index"]["type"] == "file"


def test_round_trip_queryable(tmp_path):
    """Load from JSON, wrap in GraphQuery, run a query."""
    kg = _make_graph()
    path = tmp_path / "graph.json"
    write_json(kg, path)

    kg2 = load_json(path)
    q = GraphQuery(kg2)
    results = q.query("foo")
    assert any(r.id == "py:function:foo" for r in results)


def test_round_trip_preserves_edge_attrs(tmp_path):
    kg = _make_graph()
    path = tmp_path / "graph.json"
    write_json(kg, path)

    kg2 = load_json(path)
    # Find the equation_ref edge
    for _, _, data in kg2.nxgraph.edges(data=True):
        if data.get("type") == "equation_ref":
            assert data["reftype"] == "eq"
            assert data["resolved"] is True
            break
    else:
        pytest.fail("equation_ref edge not found after round-trip")


# --- SQLite round-trip tests ---


def test_sqlite_round_trip(tmp_path):
    kg = _make_graph()
    path = tmp_path / "graph.db"
    write_sqlite(kg, path)
    assert path.exists()

    kg2 = load_sqlite(path)
    assert kg2.nxgraph.number_of_nodes() == 3
    assert kg2.nxgraph.number_of_edges() == 2


def test_sqlite_preserves_node_attrs(tmp_path):
    kg = _make_graph()
    path = tmp_path / "graph.db"
    write_sqlite(kg, path)

    kg2 = load_sqlite(path)
    assert kg2.nxgraph.nodes["py:function:foo"]["type"] == "function"
    assert kg2.nxgraph.nodes["py:function:foo"]["name"] == "foo"
    assert kg2.nxgraph.nodes["math:equation:euler"]["eqno"] == 1


def test_sqlite_preserves_metadata(tmp_path):
    kg = _make_graph()
    path = tmp_path / "graph.db"
    write_sqlite(kg, path)

    kg2 = load_sqlite(path)
    assert kg2.metadata["project"] == "test"


def test_sqlite_preserves_edge_attrs(tmp_path):
    kg = _make_graph()
    path = tmp_path / "graph.db"
    write_sqlite(kg, path)

    kg2 = load_sqlite(path)
    for _, _, data in kg2.nxgraph.edges(data=True):
        if data.get("type") == "equation_ref":
            assert data["reftype"] == "eq"
            break
    else:
        pytest.fail("equation_ref edge not found after SQLite round-trip")


def test_sqlite_queryable(tmp_path):
    """Load from SQLite, wrap in GraphQuery, run a query."""
    kg = _make_graph()
    path = tmp_path / "graph.db"
    write_sqlite(kg, path)

    kg2 = load_sqlite(path)
    q = GraphQuery(kg2)
    results = q.query("foo")
    assert any(r.id == "py:function:foo" for r in results)


def test_sqlite_fts(tmp_path):
    """Verify FTS5 full-text search works."""
    import sqlite3

    kg = _make_graph()
    path = tmp_path / "graph.db"
    write_sqlite(kg, path)

    conn = sqlite3.connect(str(path))
    rows = conn.execute(
        "SELECT id FROM nodes WHERE rowid IN "
        "(SELECT rowid FROM nodes_fts WHERE nodes_fts MATCH ?)",
        ("foo",),
    ).fetchall()
    conn.close()
    assert any(r[0] == "py:function:foo" for r in rows)


def test_sqlite_indexed_neighbors(tmp_path):
    """Verify indexed edge queries work."""
    import sqlite3

    kg = _make_graph()
    path = tmp_path / "graph.db"
    write_sqlite(kg, path)

    conn = sqlite3.connect(str(path))
    rows = conn.execute(
        "SELECT target, type FROM edges WHERE source = ?",
        ("doc:index",),
    ).fetchall()
    conn.close()
    targets = {r[0] for r in rows}
    assert "py:function:foo" in targets
    assert "math:equation:euler" in targets


# ---------------------------------------------------------------------------
# Schema version enforcement (Session 4.1)
# ---------------------------------------------------------------------------


def test_write_sqlite_sets_schema_version(tmp_path):
    import json as _json
    import sqlite3

    from sphinxcontrib.nexus.export import SCHEMA_VERSION

    kg = _make_graph()
    path = tmp_path / "graph.db"
    write_sqlite(kg, path)

    conn = sqlite3.connect(str(path))
    row = conn.execute(
        "SELECT value FROM metadata WHERE key = 'schema_version'"
    ).fetchone()
    conn.close()
    assert row is not None, "schema_version missing from metadata table"
    assert _json.loads(row[0]) == SCHEMA_VERSION


def test_load_sqlite_accepts_current_schema_version(tmp_path):
    kg = _make_graph()
    path = tmp_path / "graph.db"
    write_sqlite(kg, path)
    # Must not raise.
    reloaded = load_sqlite(path)
    assert reloaded.node_count == kg.node_count


def test_load_sqlite_accepts_missing_schema_version(tmp_path):
    """Databases written by pre-schema_version nexus releases have
    no ``schema_version`` key in ``metadata``. The loader tolerates
    that and treats the DB as v1."""
    import sqlite3

    kg = _make_graph()
    path = tmp_path / "graph.db"
    write_sqlite(kg, path)
    # Strip the schema_version row.
    conn = sqlite3.connect(str(path))
    conn.execute("DELETE FROM metadata WHERE key = 'schema_version'")
    conn.commit()
    conn.close()
    # Load must not raise.
    reloaded = load_sqlite(path)
    assert reloaded.node_count == kg.node_count


def test_load_sqlite_rejects_future_schema_version(tmp_path):
    import json as _json
    import sqlite3

    from sphinxcontrib.nexus.export import SchemaVersionError

    kg = _make_graph()
    path = tmp_path / "graph.db"
    write_sqlite(kg, path)
    # Force a future version into the DB.
    conn = sqlite3.connect(str(path))
    conn.execute(
        "UPDATE metadata SET value = ? WHERE key = 'schema_version'",
        (_json.dumps(999),),
    )
    conn.commit()
    conn.close()

    with pytest.raises(SchemaVersionError, match="999"):
        load_sqlite(path)


def test_load_sqlite_rejects_non_integer_schema_version(tmp_path):
    import json as _json
    import sqlite3

    from sphinxcontrib.nexus.export import SchemaVersionError

    kg = _make_graph()
    path = tmp_path / "graph.db"
    write_sqlite(kg, path)
    conn = sqlite3.connect(str(path))
    conn.execute(
        "UPDATE metadata SET value = ? WHERE key = 'schema_version'",
        (_json.dumps("not-a-number"),),
    )
    conn.commit()
    conn.close()

    with pytest.raises(SchemaVersionError):
        load_sqlite(path)


def test_graph_metadata_cannot_override_schema_version(tmp_path):
    """A user that stuffs a bogus ``schema_version`` into
    ``graph.metadata`` should not be able to clobber the
    authoritative version written by ``write_sqlite``."""
    import json as _json
    import sqlite3

    from sphinxcontrib.nexus.export import SCHEMA_VERSION

    kg = _make_graph()
    kg.metadata["schema_version"] = 999
    path = tmp_path / "graph.db"
    write_sqlite(kg, path)

    conn = sqlite3.connect(str(path))
    row = conn.execute(
        "SELECT value FROM metadata WHERE key = 'schema_version'"
    ).fetchone()
    conn.close()
    assert _json.loads(row[0]) == SCHEMA_VERSION
