"""Unit tests for the non-LLM verification registry loader."""

from __future__ import annotations

import networkx as nx
import pytest

from sphinxcontrib.nexus.registry import (
    REGISTRY_SCHEMA_VERSION,
    RegistryError,
    load_registry,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _base_graph() -> nx.MultiDiGraph:
    """Build a minimal graph with one equation, one implementing
    function, and one test — all named so the registry can target
    them by id."""
    g = nx.MultiDiGraph()
    g.add_node(
        "math:equation:eq-1",
        type="equation",
        name="eq-1",
        display_name="(1)",
        domain="math",
        docname="theory",
    )
    g.add_node(
        "math:equation:eq-2",
        type="equation",
        name="eq-2",
        display_name="(2)",
        domain="math",
        docname="theory",
    )
    g.add_node(
        "py:function:solver.solve",
        type="function",
        name="solver.solve",
        display_name="solve",
        domain="py",
    )
    g.add_node(
        "py:function:tests.test_solver.test_solve",
        type="function",
        name="tests.test_solver.test_solve",
        display_name="test_solve",
        domain="py",
        is_test=True,
    )
    return g


def _write_yaml(tmp_path, content: str):
    path = tmp_path / "registry.yaml"
    path.write_text(content)
    return path


# ---------------------------------------------------------------------------
# Happy-path: verifications
# ---------------------------------------------------------------------------


def test_registry_verifications_writes_tests_edge(tmp_path):
    path = _write_yaml(tmp_path, """
version: 1
verifications:
  - test: py:function:tests.test_solver.test_solve
    verifies: [eq-1]
    level: L0
    catches: [FM-07]
""")
    g = _base_graph()
    written = load_registry(path, g)
    assert written == 1

    edges = [
        (s, t, d)
        for s, t, d in g.edges(data=True)
        if d.get("type") == "tests"
    ]
    assert len(edges) == 1
    s, t, d = edges[0]
    assert s == "py:function:tests.test_solver.test_solve"
    assert t == "math:equation:eq-1"
    assert d["source"] == "registry"
    assert d["confidence"] == 1.0


def test_registry_enriches_test_node_metadata(tmp_path):
    path = _write_yaml(tmp_path, """
version: 1
verifications:
  - test: py:function:tests.test_solver.test_solve
    verifies: [eq-1, eq-2]
    level: L1
    catches: [ERR-020]
""")
    g = _base_graph()
    load_registry(path, g)
    node = g.nodes["py:function:tests.test_solver.test_solve"]
    assert node["vv_level"] == "L1"
    assert node["verifies"] == ("eq-1", "eq-2")
    assert node["catches"] == ("ERR-020",)


def test_registry_does_not_overwrite_existing_ast_metadata(tmp_path):
    """The registry is strictly additive: if a test already has a
    ``vv_level`` baked in by ``_parse_pytest_markers``, the registry
    must leave it alone."""
    path = _write_yaml(tmp_path, """
version: 1
verifications:
  - test: py:function:tests.test_solver.test_solve
    verifies: [eq-1]
    level: L3
""")
    g = _base_graph()
    g.nodes["py:function:tests.test_solver.test_solve"]["vv_level"] = "L0"
    load_registry(path, g)
    assert g.nodes["py:function:tests.test_solver.test_solve"]["vv_level"] == "L0"


def test_registry_multiple_labels_write_multiple_edges(tmp_path):
    path = _write_yaml(tmp_path, """
version: 1
verifications:
  - test: py:function:tests.test_solver.test_solve
    verifies: [eq-1, eq-2]
""")
    g = _base_graph()
    assert load_registry(path, g) == 2


# ---------------------------------------------------------------------------
# Happy-path: implementations
# ---------------------------------------------------------------------------


def test_registry_implementations_writes_implements_edge(tmp_path):
    path = _write_yaml(tmp_path, """
version: 1
implementations:
  - function: py:function:solver.solve
    implements: [eq-1]
    confidence: 0.9
""")
    g = _base_graph()
    written = load_registry(path, g)
    assert written == 1
    edges = [
        (s, t, d)
        for s, t, d in g.edges(data=True)
        if d.get("type") == "implements"
    ]
    assert len(edges) == 1
    s, t, d = edges[0]
    assert s == "py:function:solver.solve"
    assert t == "math:equation:eq-1"
    assert d["source"] == "registry"
    assert d["confidence"] == 0.9


def test_registry_implementations_default_confidence(tmp_path):
    path = _write_yaml(tmp_path, """
version: 1
implementations:
  - function: py:function:solver.solve
    implements: [eq-1]
""")
    g = _base_graph()
    load_registry(path, g)
    edges = [
        d for _, _, d in g.edges(data=True) if d.get("type") == "implements"
    ]
    assert edges[0]["confidence"] == 1.0


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_registry_is_idempotent(tmp_path):
    path = _write_yaml(tmp_path, """
version: 1
verifications:
  - test: py:function:tests.test_solver.test_solve
    verifies: [eq-1]
implementations:
  - function: py:function:solver.solve
    implements: [eq-1]
""")
    g = _base_graph()
    first = load_registry(path, g)
    second = load_registry(path, g)
    assert first == 2
    assert second == 0
    # Still exactly two edges total
    total = sum(
        1
        for _, _, d in g.edges(data=True)
        if d.get("source") == "registry"
    )
    assert total == 2


# ---------------------------------------------------------------------------
# Missing nodes are warnings, not errors
# ---------------------------------------------------------------------------


def test_registry_missing_test_logged_not_raised(tmp_path, caplog):
    path = _write_yaml(tmp_path, """
version: 1
verifications:
  - test: py:function:nonexistent.test_ghost
    verifies: [eq-1]
""")
    g = _base_graph()
    with caplog.at_level("WARNING"):
        written = load_registry(path, g)
    assert written == 0
    assert "not in the graph" in caplog.text


def test_registry_missing_equation_logged(tmp_path, caplog):
    path = _write_yaml(tmp_path, """
version: 1
verifications:
  - test: py:function:tests.test_solver.test_solve
    verifies: [eq-missing, eq-1]
""")
    g = _base_graph()
    with caplog.at_level("WARNING"):
        written = load_registry(path, g)
    # Only the eq-1 edge is written; eq-missing is skipped with a warning.
    assert written == 1
    assert "eq-missing" in caplog.text


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


def test_registry_empty_file_is_ok(tmp_path):
    path = _write_yaml(tmp_path, "")
    g = _base_graph()
    assert load_registry(path, g) == 0


def test_registry_wrong_version_raises(tmp_path):
    path = _write_yaml(tmp_path, """
version: 99
verifications: []
""")
    g = _base_graph()
    with pytest.raises(RegistryError, match="schema version"):
        load_registry(path, g)


def test_registry_missing_version_raises(tmp_path):
    path = _write_yaml(tmp_path, """
verifications:
  - test: py:function:tests.test_solver.test_solve
    verifies: [eq-1]
""")
    g = _base_graph()
    with pytest.raises(RegistryError, match="schema version"):
        load_registry(path, g)


def test_registry_non_dict_top_level_raises(tmp_path):
    path = _write_yaml(tmp_path, "- just\n- a\n- list\n")
    g = _base_graph()
    with pytest.raises(RegistryError, match="mapping"):
        load_registry(path, g)


def test_registry_missing_test_field_raises(tmp_path):
    path = _write_yaml(tmp_path, """
version: 1
verifications:
  - verifies: [eq-1]
""")
    g = _base_graph()
    with pytest.raises(RegistryError, match="test"):
        load_registry(path, g)


def test_registry_non_string_in_verifies_raises(tmp_path):
    path = _write_yaml(tmp_path, """
version: 1
verifications:
  - test: py:function:tests.test_solver.test_solve
    verifies: [eq-1, 42]
""")
    g = _base_graph()
    with pytest.raises(RegistryError, match="must be a string"):
        load_registry(path, g)


def test_registry_invalid_yaml_raises(tmp_path):
    path = _write_yaml(tmp_path, "version: 1\n  bad: [unclosed\n")
    g = _base_graph()
    with pytest.raises(RegistryError, match="invalid YAML"):
        load_registry(path, g)


def test_registry_unreadable_file_raises(tmp_path):
    ghost = tmp_path / "does_not_exist.yaml"
    g = _base_graph()
    with pytest.raises(RegistryError, match="cannot read"):
        load_registry(ghost, g)
