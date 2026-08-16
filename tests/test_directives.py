"""Unit tests for directive edge application.

The directives themselves only run under Sphinx (lifecycle, env,
ref_context), so end-to-end coverage lives in ``test_fixture_e2e.py``.
Here we exercise the pure functions — ``_resolve_enclosing_py_symbol``,
``_node_id_for_target``, ``apply_pending_edges``, and the env handlers
(``purge_doc`` / ``merge_env``) — against synthetic envs and graphs.
"""

from __future__ import annotations

import types

import networkx as nx
import pytest

from sphinxcontrib.nexus.directives import (
    _node_id_for_target,
    _resolve_enclosing_py_symbol,
    apply_pending_edges,
    merge_env,
    purge_doc,
)


def _env(**ref_ctx):
    """Build a stand-in BuildEnvironment that exposes ``ref_context``."""
    env = types.SimpleNamespace()
    env.ref_context = dict(ref_ctx)
    env.docname = "index"
    return env


# ---------------------------------------------------------------------------
# _resolve_enclosing_py_symbol
# ---------------------------------------------------------------------------


def test_resolve_empty_context_returns_none():
    assert _resolve_enclosing_py_symbol(_env()) is None


def test_resolve_bare_function():
    env = _env(**{"py:module": "pkg.mod", "py:function": "solve"})
    assert _resolve_enclosing_py_symbol(env) == "pkg.mod.solve"


def test_resolve_method_inside_class():
    env = _env(
        **{
            "py:module": "pkg.mod",
            "py:classes": ["Solver"],
            "py:method": "run",
        }
    )
    assert _resolve_enclosing_py_symbol(env) == "pkg.mod.Solver.run"


def test_resolve_class_itself_does_not_stack_classes():
    env = _env(
        **{
            "py:module": "pkg.mod",
            "py:classes": ["Solver"],
            "py:class": "Solver",
        }
    )
    # ``py:class`` resolving itself should yield module.ClassName,
    # not module.Solver.Solver.
    assert _resolve_enclosing_py_symbol(env) == "pkg.mod.Solver"


def test_resolve_most_specific_key_wins():
    env = _env(
        **{
            "py:module": "pkg",
            "py:function": "legacy",
            "py:method": "current",
            "py:classes": ["Widget"],
        }
    )
    # ``py:method`` takes precedence over ``py:function``.
    assert _resolve_enclosing_py_symbol(env) == "pkg.Widget.current"


# ---------------------------------------------------------------------------
# _node_id_for_target
# ---------------------------------------------------------------------------


def _graph_with_symbol(node_id: str) -> nx.MultiDiGraph:
    g = nx.MultiDiGraph()
    g.add_node(node_id, type=node_id.split(":", 2)[1], name="x", domain="py")
    return g


def test_node_id_for_target_exact_match():
    g = _graph_with_symbol("py:function:pkg.solve")
    assert _node_id_for_target("py:function:pkg.solve", g) == "py:function:pkg.solve"


def test_node_id_for_target_dotted_function():
    g = _graph_with_symbol("py:function:pkg.solve")
    assert _node_id_for_target("pkg.solve", g) == "py:function:pkg.solve"


def test_node_id_for_target_dotted_method():
    g = _graph_with_symbol("py:method:pkg.Widget.run")
    assert _node_id_for_target("pkg.Widget.run", g) == "py:method:pkg.Widget.run"


def test_node_id_for_target_dotted_class():
    g = _graph_with_symbol("py:class:pkg.Widget")
    assert _node_id_for_target("pkg.Widget", g) == "py:class:pkg.Widget"


def test_node_id_for_target_missing_returns_none():
    g = _graph_with_symbol("py:function:pkg.solve")
    assert _node_id_for_target("pkg.unknown", g) is None


# ---------------------------------------------------------------------------
# apply_pending_edges
# ---------------------------------------------------------------------------


def _graph_for_edge_tests() -> nx.MultiDiGraph:
    g = nx.MultiDiGraph()
    g.add_node("math:equation:eq-1", type="equation", name="eq-1",
               display_name="(1)", domain="math", docname="theory")
    g.add_node("py:function:pkg.solve", type="function", name="pkg.solve",
               display_name="solve", domain="py")
    g.add_node("py:function:pkg.test_solve", type="function",
               name="pkg.test_solve", display_name="test_solve",
               domain="py", is_test=True)
    return g


def test_apply_verifies_writes_tests_edge():
    g = _graph_for_edge_tests()
    env = types.SimpleNamespace()
    env.nexus_pending_edges = {
        "theory/index": [
            {
                "kind": "verifies",
                "label": "eq-1",
                "target": "pkg.test_solve",
                "docname": "theory/index",
                "lineno": 42,
            }
        ]
    }
    written = apply_pending_edges(env, g)
    assert written == 1
    edges = [
        (s, t, d.get("source"))
        for s, t, d in g.edges(data=True)
        if d.get("type") == "tests"
    ]
    assert (
        "py:function:pkg.test_solve",
        "math:equation:eq-1",
        "directive",
    ) in edges


def test_apply_implements_writes_implements_edge():
    g = _graph_for_edge_tests()
    env = types.SimpleNamespace()
    env.nexus_pending_edges = {
        "theory/index": [
            {
                "kind": "implements",
                "label": "eq-1",
                "target": "py:function:pkg.solve",
                "docname": "theory/index",
                "lineno": 7,
            }
        ]
    }
    written = apply_pending_edges(env, g)
    assert written == 1
    edges = [
        (s, t, d.get("source"))
        for s, t, d in g.edges(data=True)
        if d.get("type") == "implements"
    ]
    assert (
        "py:function:pkg.solve",
        "math:equation:eq-1",
        "directive",
    ) in edges


def test_apply_is_idempotent():
    g = _graph_for_edge_tests()
    env = types.SimpleNamespace()
    env.nexus_pending_edges = {
        "theory/index": [
            {
                "kind": "verifies",
                "label": "eq-1",
                "target": "pkg.test_solve",
                "docname": "theory/index",
                "lineno": 1,
            }
        ]
    }
    first = apply_pending_edges(env, g)
    second = apply_pending_edges(env, g)
    assert first == 1
    assert second == 0
    # Registry is NOT drained; replay is safe because of the
    # source="directive" guard.
    assert env.nexus_pending_edges == {
        "theory/index": [
            {
                "kind": "verifies",
                "label": "eq-1",
                "target": "pkg.test_solve",
                "docname": "theory/index",
                "lineno": 1,
            }
        ]
    }


def test_apply_missing_target_logs_and_skips(caplog):
    g = _graph_for_edge_tests()
    env = types.SimpleNamespace()
    env.nexus_pending_edges = {
        "theory/index": [
            {
                "kind": "verifies",
                "label": "eq-1",
                "target": "pkg.does_not_exist",
                "docname": "theory/index",
                "lineno": 3,
            }
        ]
    }
    with caplog.at_level("WARNING"):
        written = apply_pending_edges(env, g)
    assert written == 0
    assert "does_not_exist" in caplog.text


def test_apply_missing_equation_logs_and_skips(caplog):
    g = _graph_for_edge_tests()
    env = types.SimpleNamespace()
    env.nexus_pending_edges = {
        "theory/index": [
            {
                "kind": "verifies",
                "label": "eq-missing",
                "target": "pkg.test_solve",
                "docname": "theory/index",
                "lineno": 4,
            }
        ]
    }
    with caplog.at_level("WARNING"):
        written = apply_pending_edges(env, g)
    assert written == 0
    assert "eq-missing" in caplog.text


def test_apply_empty_registry_is_noop():
    g = _graph_for_edge_tests()
    env = types.SimpleNamespace()
    assert apply_pending_edges(env, g) == 0


# ---------------------------------------------------------------------------
# purge_doc
# ---------------------------------------------------------------------------


def test_purge_doc_drops_only_named_docname():
    env = types.SimpleNamespace()
    env.nexus_pending_edges = {
        "theory/a": [{"kind": "verifies", "label": "eq-1", "target": "x"}],
        "theory/b": [{"kind": "verifies", "label": "eq-2", "target": "y"}],
    }
    purge_doc(None, env, "theory/a")
    assert "theory/a" not in env.nexus_pending_edges
    assert "theory/b" in env.nexus_pending_edges


def test_purge_doc_is_noop_when_docname_absent():
    env = types.SimpleNamespace()
    env.nexus_pending_edges = {"theory/a": []}
    purge_doc(None, env, "theory/missing")
    assert env.nexus_pending_edges == {"theory/a": []}


def test_purge_doc_handles_missing_registry():
    env = types.SimpleNamespace()
    # No nexus_pending_edges attribute at all — must not crash.
    purge_doc(None, env, "theory/a")


# ---------------------------------------------------------------------------
# merge_env (parallel builds)
# ---------------------------------------------------------------------------


def test_merge_env_copies_worker_entries_for_docnames():
    main = types.SimpleNamespace()
    main.nexus_pending_edges = {
        "theory/a": [{"kind": "verifies", "label": "eq-a", "target": "x"}]
    }
    other = types.SimpleNamespace()
    other.nexus_pending_edges = {
        "theory/b": [{"kind": "verifies", "label": "eq-b", "target": "y"}],
        "theory/c": [{"kind": "implements", "label": "eq-c", "target": "z"}],
    }
    merge_env(None, main, ["theory/b"], other)
    assert "theory/a" in main.nexus_pending_edges
    assert "theory/b" in main.nexus_pending_edges
    # theory/c wasn't in the requested docnames list — not merged.
    assert "theory/c" not in main.nexus_pending_edges


# ---------------------------------------------------------------------------
# `.. error-entry::` — the declaring directive, and the `catches` edge
# ---------------------------------------------------------------------------
#
# The V&V triangle had two corners. A test declares what it VERIFIES (an
# equation, which exists as a node) and what it CATCHES (a catalogued
# failure mode, which did not) — so [M] 2026-08-16 on ORPHEUS, 224 nodes
# carried a `catches` marker naming 78 distinct entries and none of the
# 78 was a node. "Which tests catch ERR-051?" was a grep, not a query.


def _env_with_error_entries(*entries, docname="catalogue"):
    """An env whose pending queue holds `.. error-entry::` payloads."""
    env = types.SimpleNamespace()
    env.docname = docname
    env.nexus_pending_edges = {
        docname: [
            {"kind": "error-entry", "id": eid, "title": title,
             "docname": docname, "lineno": 1}
            for eid, title in entries
        ]
    }
    return env


def test_an_error_entry_becomes_a_node():
    from sphinxcontrib.nexus.directives import apply_declared_nodes

    g = nx.MultiDiGraph()
    created = apply_declared_nodes(
        _env_with_error_entries(("ERR-051", "Galerkin idempotency")), g,
    )
    assert created == 1
    node = g.nodes["vv:error:ERR-051"]
    assert node["type"] == "error"
    assert node["name"] == "ERR-051"
    assert node["title"] == "Galerkin idempotency"


def test_declaring_the_same_entry_twice_is_idempotent():
    """The registry is replayed on every incremental build."""
    from sphinxcontrib.nexus.directives import apply_declared_nodes

    g = nx.MultiDiGraph()
    env = _env_with_error_entries(("ERR-051", "x"))
    assert apply_declared_nodes(env, g) == 1
    assert apply_declared_nodes(env, g) == 0
    assert g.number_of_nodes() == 1


def test_an_error_entry_is_contained_by_its_page():
    from sphinxcontrib.nexus.directives import apply_declared_nodes

    g = nx.MultiDiGraph()
    g.add_node("std:file:catalogue", type="file", name="catalogue")
    apply_declared_nodes(_env_with_error_entries(("ERR-051", "x")), g)
    assert any(
        d.get("type") == "contains"
        for _s, _t, d in g.edges("std:file:catalogue", data=True)
    )


def test_a_catches_marker_reaches_its_declared_entry():
    """The mirror of `verifies` -> `tests`, end to end."""
    from sphinxcontrib.nexus.directives import apply_declared_nodes
    from sphinxcontrib.nexus.merge import write_catches_edges

    g = nx.MultiDiGraph()
    g.add_node("py:function:tests.test_x.test_thing", type="function",
               name="tests.test_x.test_thing", catches=("ERR-051",))
    apply_declared_nodes(_env_with_error_entries(("ERR-051", "x")), g)

    assert write_catches_edges(g) == 1
    edge = list(g.get_edge_data(
        "py:function:tests.test_x.test_thing", "vv:error:ERR-051",
    ).values())[0]
    assert edge["type"] == "catches"
    assert edge["source"] == "pytest.mark.catches"


def test_an_undeclared_entry_mints_nothing():
    """A typo in a marker must NOT invent the thing it claims to catch.

    The equation side refuses for the same reason: if `catches` could
    conjure its target, a misspelled marker would create a catalogue
    entry nobody wrote — and the miss would then read as coverage,
    which is the one failure direction a V&V graph must not have.
    """
    from sphinxcontrib.nexus.merge import write_catches_edges

    g = nx.MultiDiGraph()
    g.add_node("py:function:t.test_thing", type="function",
               name="t.test_thing", catches=("ERR-999",))
    assert write_catches_edges(g) == 0
    assert "vv:error:ERR-999" not in g
    assert g.number_of_nodes() == 1


def test_a_typo_warns_once_the_project_HAS_a_catalogue(caplog):
    """Per-marker warnings belong to a project that has ADOPTED the
    catalogue — there, a marker with no entry is a typo worth naming."""
    from sphinxcontrib.nexus.directives import apply_declared_nodes
    from sphinxcontrib.nexus.merge import write_catches_edges

    g = nx.MultiDiGraph()
    g.add_node("py:function:t.test_thing", type="function",
               name="t.test_thing", catches=("ERR-999",))
    apply_declared_nodes(_env_with_error_entries(("ERR-051", "x")), g)
    with caplog.at_level("WARNING"):
        assert write_catches_edges(g) == 0
    assert "ERR-999" in caplog.text


def test_a_project_with_no_catalogue_is_told_once_not_224_times(caplog):
    """The lessons-L56 shape: an absence must still name what it looked
    for, and must not say it once per marker.

    [M] ORPHEUS carries 243 `catches` markers, on 224 nodes, naming 78
    distinct entries, and has no `.. error-entry::` anywhere — its
    catalogue lives outside the corpus. Per-marker warnings would be 243
    lines on every build, which is how a real signal gets tuned out.
    (Three different numbers; the gate below asserts ONE line, not which.)
    """
    from sphinxcontrib.nexus.merge import write_catches_edges

    g = nx.MultiDiGraph()
    for i in range(3):
        g.add_node(f"py:function:t.test_{i}", type="function",
                   name=f"t.test_{i}", catches=("ERR-001", "ERR-002"))
    with caplog.at_level("INFO"):
        assert write_catches_edges(g) == 0
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1, warnings   # one line, not one per marker
    assert "ERR-001" in caplog.text and "ERR-002" in caplog.text
    assert "not in the corpus" in caplog.text


def test_write_catches_edges_is_idempotent():
    from sphinxcontrib.nexus.directives import apply_declared_nodes
    from sphinxcontrib.nexus.merge import write_catches_edges

    g = nx.MultiDiGraph()
    g.add_node("py:function:t.test_thing", type="function",
               name="t.test_thing", catches=("ERR-051",))
    apply_declared_nodes(_env_with_error_entries(("ERR-051", "x")), g)
    assert write_catches_edges(g) == 1
    assert write_catches_edges(g) == 0


def test_purge_drops_error_entries_too():
    """`error-entry` shares the pending registry with the relation
    directives precisely so the purge and parallel-merge handlers keep
    working without a second implementation."""
    env = _env_with_error_entries(("ERR-051", "x"))
    purge_doc(None, env, "catalogue")
    assert env.nexus_pending_edges == {}
