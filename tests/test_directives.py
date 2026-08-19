"""Unit tests for directive edge application.

The directives themselves only run under Sphinx (lifecycle, env,
ref_context), so end-to-end coverage lives in ``test_fixture_e2e.py``.
Here we exercise the pure functions — ``_resolve_enclosing_py_symbol``,
``_node_id_for_target``, ``apply_pending_edges``, and the env handlers
(``purge_doc`` / ``merge_env``) — against synthetic envs and graphs.
"""

from __future__ import annotations

import logging
import types

import networkx as nx
import pytest
from docutils import nodes

from sphinxcontrib.nexus.graph import NO_IMPLEMENTATION_ATTR
from sphinxcontrib.nexus.directives import (
    _node_id_for_target,
    _where,
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
            # Distinct linenos, so a mint that drops the field cannot be
            # mistaken for one that carries it: every entry would read
            # the same number either way if they all shared one.
            {"kind": "error-entry", "id": eid, "title": title,
             "docname": docname, "lineno": 100 + i}
            for i, (eid, title) in enumerate(entries)
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
    # The directive records where it was written; the mint must carry it.
    # Without this every entry sat at line 0, which reads as a POSITION
    # rather than as "unknown", and `errors()` reported it to every
    # caller — 79 of 79 on ORPHEUS.
    assert node["lineno"] == 100


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


def test_the_relation_replay_SKIPS_a_declaration_instead_of_crashing():
    """The shared registry holds both kinds; only one has a `label`.

    Regression, 2026-08-17. `apply_pending_edges` read `entry["label"]`
    for every payload that was not an equation relation, so the first
    real `.. error-entry::` in any project raised `KeyError: 'label'`
    out of the `build-finished` handler and took the whole build down.

    The nine sibling tests above could not see it: every one of them
    calls `apply_declared_nodes` directly, and the crash is in the OTHER
    function that walks the same queue. Only a real Sphinx build runs
    both — which is why `test_fixture_e2e` now declares two entries.
    """
    env = _env_with_error_entries(("ERR-051", "x"))
    # A relation payload alongside it, so the replay has real work to do
    # and a silent early return cannot pass for a skip.
    env.nexus_pending_edges["catalogue"].append({
        "kind": "implements", "label": "eq-a", "target": "pkg.fn",
        "docname": "catalogue", "lineno": 2,
    })
    g = nx.MultiDiGraph()
    g.add_node("math:equation:eq-a", type="equation", name="eq-a")
    g.add_node("py:function:pkg.fn", type="function", name="pkg.fn")

    assert apply_pending_edges(env, g) == 1  # the relation, not the entry

    edge = list(g.get_edge_data(
        "py:function:pkg.fn", "math:equation:eq-a",
    ).values())[0]
    assert edge["type"] == "implements"
    # And the declaration is still the other function's job, untouched.
    assert "vv:error:ERR-051" not in g


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


# ---------------------------------------------------------------------------
# apply_pending_edges — the ontology admits a DECLARATION too (nexus#86)
# ---------------------------------------------------------------------------
#
# The guessing path consulted the ontology at the producer; this one wrote
# unconditionally. Backwards: a guess lands at confidence 0.7 and an
# authored declaration at 1.0, so the stronger claim was the unchecked one.


def _graph_with_a_typevar() -> nx.MultiDiGraph:
    """The measured case. `[edge.implements].domain` is
    function/method/class, so a TypeVar (`data`) is not an admissible
    implementer — and `carrier-grid-operator-typing` on ORPHEUS acquired
    exactly these edges, on a `-W` build that stayed green."""
    g = _graph_for_edge_tests()
    g.add_node("py:data:pkg.Domain", type="data", name="pkg.Domain",
               display_name="Domain", domain="py")
    return g


def _entry(kind, target, label="eq-1"):
    return {"kind": kind, "label": label, "target": target,
            "docname": "theory/index", "lineno": 42}


def _apply(g, *entries, project_root=None):
    env = types.SimpleNamespace()
    env.nexus_pending_edges = {"theory/index": list(entries)}
    return apply_pending_edges(env, g, project_root=project_root)


def test_the_PREFIXED_workaround_no_longer_skips_the_type_check(caplog):
    """⛔ The hole this closes.

    `_node_id_for_target` returns `target` unchanged when it is already a
    node id, so a fully-prefixed `:by: py:data:...` matched before any
    type filter ran. The bare spelling was REJECTED ("target not found in
    graph" — false, the node is right there) and the workaround was
    ACCEPTED with no check at all. Worst of both.
    """
    g = _graph_with_a_typevar()
    with caplog.at_level("WARNING"):
        written = _apply(g, _entry("implements", "py:data:pkg.Domain"))
    assert written == 0
    assert not [d for _, _, d in g.edges(data=True)
                if d.get("type") == "implements"]
    assert "ontology refuses" in caplog.text
    assert "domain is ['function', 'method', 'class']" in caplog.text


def test_the_refusal_names_the_RULE_so_the_author_can_act(caplog):
    """The author asserted a fact and the schema disagrees; only they can
    decide which is wrong. A bare "skipping" would not let them."""
    g = _graph_with_a_typevar()
    with caplog.at_level("WARNING"):
        _apply(g, _entry("implements", "py:data:pkg.Domain"))
    assert "source is 'data'" in caplog.text          # what was wrong
    assert "[edge.implements]" in caplog.text          # which rule
    assert "ontology.toml" in caplog.text              # where to change it


def test_declaring_a_TEST_as_an_implementer_is_refused(caplog):
    """`forbid_source_attr = {in_test_file = true}`: a test VERIFIES an
    equation, it does not implement one. The inference path has refused
    this since it was written — an equation whose only implementer is a
    test reads as implemented when nothing implements it, a false ALIVE.
    The declaring path accepted it."""
    g = _graph_for_edge_tests()
    g.nodes["py:function:pkg.test_solve"]["in_test_file"] = True
    with caplog.at_level("WARNING"):
        written = _apply(g, _entry("implements", "pkg.test_solve"))
    assert written == 0
    assert "ontology refuses" in caplog.text
    assert "in_test_file" in caplog.text


def test_the_SAME_test_node_is_still_a_legal_VERIFIER():
    """The refusal is per-edge-type, not a blanket ban on test nodes —
    `[edge.tests]` is exactly what a test may carry."""
    g = _graph_for_edge_tests()
    g.nodes["py:function:pkg.test_solve"]["in_test_file"] = True
    assert _apply(g, _entry("verifies", "pkg.test_solve")) == 1


def test_a_refused_declaration_leaves_the_INFERENCE_free_to_proceed():
    """The safety property behind skipping rather than writing.

    `_infer_implements` stands its guesses down for any equation that
    already carries a non-inferred `implements` edge, and it runs AFTER
    this pass. Writing the refused edge would suppress the guesses; so
    would recording the equation as declared. Skipping does neither, so
    a refused declaration costs the equation nothing it had.
    """
    g = _graph_with_a_typevar()
    _apply(g, _entry("implements", "py:data:pkg.Domain"))
    declared = {
        t for _, t, d in g.edges(data=True)
        if d.get("type") == "implements" and d.get("source") != "inferred"
    }
    assert "math:equation:eq-1" not in declared


def test_a_legal_declaration_still_lands_silently(caplog):
    """The positive control. A check that refuses everything would pass
    every gate above."""
    g = _graph_with_a_typevar()
    with caplog.at_level("WARNING"):
        written = _apply(g, _entry("implements", "pkg.solve"))
    assert written == 1
    assert "ontology refuses" not in caplog.text


def test_the_resolver_admits_what_the_PROJECT_ontology_admits(tmp_path):
    """⭐ The drift this removes, and it needs a project extension to be
    observable at all — the base `domain` IS the three the resolver used
    to hard-code, so the two agree today and would silently disagree the
    moment either moved. Widening `implements` to accept `data` makes the
    bare `:by: pkg.Domain` resolve, with no change to the resolver.
    """
    (tmp_path / ".nexus").mkdir()
    # `[extend.edge.…]`, not a redefinition — widening is monotone and
    # the base entries are kept, which is what makes it safe.
    (tmp_path / ".nexus" / "ontology.toml").write_text(
        '[extend.edge.implements]\ndomain = ["data"]\n'
    )
    g = _graph_with_a_typevar()
    # Base ontology: the bare name does not resolve and `data` is refused.
    assert _apply(g, _entry("implements", "pkg.Domain")) == 0
    # The project's own ontology says a TypeVar may carry it. Same input.
    assert _apply(g, _entry("implements", "pkg.Domain"),
                  project_root=tmp_path) == 1
    assert ("py:data:pkg.Domain", "math:equation:eq-1") in [
        (s, t) for s, t, d in g.edges(data=True)
        if d.get("type") == "implements"
    ]


def test_where_degrades_to_the_DOCNAME_when_no_line_is_known():
    """The pending registry defaults `lineno` to "?", and Sphinx renders
    a `(docname, lineno)` pair as a source reference — a non-integer
    there produces a broken one, so the pair is only formed when the
    line is really known."""
    assert _where("theory/index", 42) == ("theory/index", 42)
    assert _where("theory/index", "?") == "theory/index"


# ---------------------------------------------------------------------------
# `.. no-implementation::` — an equation declares that nothing implements it
# ---------------------------------------------------------------------------
#
# `apply_declared_annotations` is FOUR early-return arms wearing one name, so
# it is mutated and gated arm by arm (vv-principles #17's granularity trap): a
# whole-guard mutation reddens on whichever arm the fixture happens to reach
# and certifies the other three. Each test below names its arm.


def _env_declaring_nothing(label, kind, docname="theory/index", lineno=7):
    env = types.SimpleNamespace()
    env.docname = docname
    env.nexus_pending_edges = {
        docname: [{
            "kind": "no-implementation",
            "label": label,
            "no_implementation_kind": kind,
            "docname": docname,
            "lineno": lineno,
        }]
    }
    return env


def test_declaring_no_implementation_annotates_the_equation():
    from sphinxcontrib.nexus.directives import apply_declared_annotations

    g = _graph_for_edge_tests()
    annotated = apply_declared_annotations(
        _env_declaring_nothing("eq-1", "identity"), g,
    )
    assert annotated == 1
    assert g.nodes["math:equation:eq-1"][NO_IMPLEMENTATION_ATTR] == "identity"
    # It writes NO edge — that is the whole reason it is not an
    # `implements` variant. A sentinel "nothing" node would sit in every
    # traversal that never asked about it.
    assert [d for _, _, d in g.edges(data=True)] == []


def test_declaring_no_implementation_is_idempotent():
    from sphinxcontrib.nexus.directives import apply_declared_annotations

    g = _graph_for_edge_tests()
    env = _env_declaring_nothing("eq-1", "identity")
    assert apply_declared_annotations(env, g) == 1
    assert apply_declared_annotations(env, g) == 0


def test_ARM_unknown_kind_is_refused_and_names_the_legal_set(caplog):
    """ARM 2 — the closed vocabulary. An unrecognised kind must not land:
    a free-text kind drifts into prose and stops being queryable, which
    is the defect the enumeration exists to prevent."""
    from sphinxcontrib.nexus.directives import apply_declared_annotations

    g = _graph_for_edge_tests()
    with caplog.at_level(logging.WARNING):
        annotated = apply_declared_annotations(
            _env_declaring_nothing("eq-1", "vibes"), g,
        )
    assert annotated == 0
    assert NO_IMPLEMENTATION_ATTR not in g.nodes["math:equation:eq-1"]
    text = caplog.text
    assert "vibes" in text
    assert "identity" in text          # the legal set is named…
    assert "extend.attribute" in text  # …and so is the way to add one


def test_ARM_a_missing_statement_is_refused(caplog):
    """ARM 3 — a label that resolves to nothing is a typo in prose, and
    it must be as loud as any other directive misuse rather than
    silently annotating a node into existence."""
    from sphinxcontrib.nexus.directives import apply_declared_annotations

    g = _graph_for_edge_tests()
    with caplog.at_level(logging.WARNING):
        annotated = apply_declared_annotations(
            _env_declaring_nothing("eq-does-not-exist", "identity"), g,
        )
    assert annotated == 0
    assert "eq-does-not-exist" in caplog.text
    assert "math:equation:eq-does-not-exist" not in g


def test_ARM_a_declared_implementer_CONTRADICTS_it_and_wins_nothing(caplog):
    """ARM 4 — both are authored assertions at confidence 1.0. Silently
    preferring one would discard the other, and the annotation is the
    one that would then suppress the inference too."""
    from sphinxcontrib.nexus.directives import apply_declared_annotations

    g = _graph_for_edge_tests()
    g.add_edge(
        "py:function:pkg.solve", "math:equation:eq-1",
        type="implements", source="directive", confidence=1.0,
    )
    with caplog.at_level(logging.WARNING):
        annotated = apply_declared_annotations(
            _env_declaring_nothing("eq-1", "identity"), g,
        )
    assert annotated == 0
    assert NO_IMPLEMENTATION_ATTR not in g.nodes["math:equation:eq-1"]
    assert "contradicted" in caplog.text
    assert "py:function:pkg.solve" in caplog.text


def test_an_INFERRED_implementer_does_not_contradict_it():
    """The mirror of ARM 4, and the case that makes it useful: a GUESS is
    exactly what this declaration exists to remove, so letting one block
    the declaration would make the feature unusable on precisely the
    equations that need it (`[M]` ORPHEUS: 244 guesses across the eleven
    hand-verified no-implementer equations)."""
    from sphinxcontrib.nexus.directives import apply_declared_annotations

    g = _graph_for_edge_tests()
    g.add_edge(
        "py:function:pkg.solve", "math:equation:eq-1",
        type="implements", source="inferred", confidence=0.7,
    )
    annotated = apply_declared_annotations(
        _env_declaring_nothing("eq-1", "identity"), g,
    )
    assert annotated == 1
    assert g.nodes["math:equation:eq-1"][NO_IMPLEMENTATION_ATTR] == "identity"


def test_the_relation_replay_SKIPS_a_no_implementation_declaration():
    """It shares one pending registry with the edge directives, so the
    replay must skip it — its payload has no ``target`` to read, and
    reading on would ``KeyError`` the whole build."""
    g = _graph_for_edge_tests()
    written = apply_pending_edges(_env_declaring_nothing("eq-1", "identity"), g)
    assert written == 0


def test_ARM_a_missing_kind_is_refused_at_the_directive():
    """ARM 1 — the only arm that fires before the pending queue, so it is
    the only one `apply_declared_annotations` can never see.

    ⚠ It had no witness when written, and neither does its sibling
    (``.. implements::``'s "needs a ':by:'" arm is untested to this day).
    That is the shape vv-principles #17 warns about: four arms wearing
    one name, where a whole-guard mutation reddens on whichever arm the
    fixture happens to reach.

    Constructed rather than built, because a deliberately-broken
    directive in the fixture project would emit a warning on every
    build of it. ``run()`` touches only these four attributes on this
    path.
    """
    from sphinxcontrib.nexus.directives import NoImplementationDirective

    warned: list[str] = []
    d = NoImplementationDirective.__new__(NoImplementationDirective)
    d.arguments = ["eq-1"]
    d.options = {}                       # no :kind:
    d.lineno = 12
    d.reporter = types.SimpleNamespace(
        warning=lambda msg, line=None: warned.append(msg) or nodes.system_message(
            "", type="WARNING", level=2,
        )
    )

    out = d.run()
    assert len(out) == 1                 # the system_message, not a container
    assert len(warned) == 1
    assert "kind" in warned[0]
    assert "suppression" in warned[0]    # it says WHY, not just that it must


def test_a_blank_kind_is_refused_like_a_missing_one():
    """``:kind:`` with nothing after it parses to the empty string, which
    is a different value from absent and the same mistake."""
    from sphinxcontrib.nexus.directives import NoImplementationDirective

    warned: list[str] = []
    d = NoImplementationDirective.__new__(NoImplementationDirective)
    d.arguments = ["eq-1"]
    d.options = {"kind": "   "}
    d.lineno = 12
    d.reporter = types.SimpleNamespace(
        warning=lambda msg, line=None: warned.append(msg) or nodes.system_message(
            "", type="WARNING", level=2,
        )
    )
    d.run()
    assert len(warned) == 1
