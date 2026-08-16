"""Statement-to-statement relations (#19) and sphinx-proof nodes (#21).

Both features only exist as a consequence of a real Sphinx build: the
relation directives run at ``doctree-read`` and replay against a graph
that doesn't exist yet when they fire, and the ``prf`` domain publishes
nothing through ``Domain.get_objects()`` — the environments live on
``env.proof_list`` and in the doctree. A hand-built fixture would test
our idea of sphinx-proof rather than sphinx-proof, so the module builds
``tests/roots/test-proof-relations`` once with the real extension.

The pure helpers get direct unit tests below the build-backed ones.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import networkx as nx
import pytest
from docutils import nodes as docutils_nodes
from docutils.frontend import get_default_settings
from docutils.parsers.rst import Parser as RSTParser
from docutils.utils import new_document

from sphinxcontrib.nexus._mappings import resolve_proof_id, resolve_target_id
from sphinxcontrib.nexus.directives import (
    EQUATION_RELATIONS,
    _apply_relation,
    _nearest_preceding_label,
    _resolve_statement_id,
)
from sphinxcontrib.nexus.export import load_sqlite
from sphinxcontrib.nexus.extractors import (
    _is_auto_proof_label,
    _proof_title,
)
from sphinxcontrib.nexus.query import GraphQuery

ROOT = Path(__file__).parent / "roots" / "test-proof-relations"

pytest.importorskip(
    "sphinx_proof",
    reason="sphinx-proof drives the prf half of this module",
)


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """The knowledge graph from one real build of the fixture root."""
    out = tmp_path_factory.mktemp("proof-relations")
    proc = subprocess.run(
        [sys.executable, "-m", "sphinx", "-q", "-E", str(ROOT), str(out)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout

    # A directive that can't bind its source, or a prf label that doesn't
    # resolve, warns and is dropped — the edge would then be missing for
    # a reason no assertion below would name. Checked against the
    # fixture's own labels rather than with ``-W``, so an unrelated
    # upstream deprecation doesn't redden this module.
    noise = [
        line for line in (proc.stdout + proc.stderr).splitlines()
        if "WARNING" in line and "index.rst" in line
    ]
    assert not noise, noise

    db = out / "_nexus" / "graph.db"
    assert db.exists(), f"no graph at {db}"
    return load_sqlite(db)


@pytest.fixture(scope="module")
def graph(built):
    return built.nxgraph


@pytest.fixture(scope="module")
def query(built):
    return GraphQuery(built)


def _relation_edges(graph: nx.MultiDiGraph) -> set[tuple[str, str, str]]:
    return {
        (src, data.get("type", ""), tgt)
        for src, tgt, data in graph.edges(data=True)
        if data.get("type") in EQUATION_RELATIONS.values()
    }


# ---------------------------------------------------------------------------
# #19 — equation → equation relations
# ---------------------------------------------------------------------------


def test_explicit_label_binds_the_named_source(graph):
    """``:label:`` names the source no matter where the directive sits."""
    assert (
        "math:equation:p1-closure",
        "approximates",
        "math:equation:transport-continuous",
    ) in _relation_edges(graph)


def test_omitted_label_binds_the_preceding_equation(graph):
    """The ergonomic case: written under the equation it describes.

    ``.. discretizes:: transport-sn`` in the fixture carries no
    ``:label:`` and sits directly beneath ``dd-closure``.
    """
    assert (
        "math:equation:dd-closure",
        "discretizes",
        "math:equation:transport-sn",
    ) in _relation_edges(graph)


def test_all_three_relations_are_registered(graph):
    """Each directive name must reach the graph as its own edge type."""
    found = {relation for _, relation, _ in _relation_edges(graph)}
    assert found == {"discretizes", "derives_from", "approximates"}


def test_relations_carry_directive_provenance(graph):
    for src, tgt, data in graph.edges(data=True):
        if data.get("type") in EQUATION_RELATIONS.values():
            assert data.get("source") == "directive", (src, tgt)
            assert data.get("confidence") == 1.0


def test_relation_between_proof_environments(graph):
    """Both ends may be proof objects — same syntax, no new directive."""
    assert (
        "prf:proof_object:thm-balance",
        "derives_from",
        "prf:proof_object:def-angular-flux",
    ) in _relation_edges(graph)


def test_equations_are_no_longer_leaves(graph):
    """The point of #19: the math has structure of its own."""
    out = [
        d.get("type")
        for _, _, d in graph.out_edges("math:equation:transport-sn", data=True)
    ]
    assert "derives_from" in out


# ---------------------------------------------------------------------------
# #21 — sphinx-proof environments as nodes
# ---------------------------------------------------------------------------


def test_labelled_environments_become_nodes(graph):
    for node_id in (
        "prf:proof_object:def-angular-flux",
        "prf:proof_object:thm-balance",
        "prf:proof_object:alg-sweep",
    ):
        assert node_id in graph.nodes, node_id
        assert graph.nodes[node_id]["type"] == "proof_object"


def test_environment_kind_is_queryable(graph):
    """One node type, with the environment kept as an attribute — an
    ``algorithm`` has to be findable without parsing node ids."""
    kinds = {
        attrs.get("prf_type")
        for _, attrs in graph.nodes(data=True)
        if attrs.get("type") == "proof_object"
    }
    assert kinds == {"definition", "theorem", "algorithm"}


def test_unlabelled_environment_is_not_a_node(graph):
    """sphinx-proof gives it a serial-numbered synthetic label that
    renumbers on every edit above it, and nothing can reference it.

    ⚠ Re-expressed 2026-08-16. This used to filter on `prf:remark:`,
    which only identified the fixture's unlabelled environment because
    the id carried the ENVIRONMENT KIND. Ids are now
    `prf:proof_object:<label>`, so the claim is stated where it always
    belonged — against the synthetic LABEL, via the one helper that
    decides what synthetic means.
    """
    from sphinxcontrib.nexus.extractors import _is_auto_proof_label

    prf = [
        (n, a) for n, a in graph.nodes(data=True)
        if isinstance(n, str) and n.startswith("prf:")
    ]
    assert prf, "fixture has no proof objects — the gate would be vacuous"
    synthetic = [
        n for n, a in prf
        if _is_auto_proof_label(a.get("name", ""), a.get("prf_type", ""))
    ]
    assert synthetic == []


def test_environments_are_contained_by_their_page(graph):
    assert graph.has_edge("std:file:index", "prf:proof_object:thm-balance")


def test_title_and_statement_come_from_the_doctree(graph):
    """``env.proof_list`` records where a theorem lives, not what it
    says; the prose only exists in the doctree."""
    attrs = graph.nodes["prf:proof_object:def-angular-flux"]
    assert attrs["display_name"] == "Angular flux"
    assert "number of particles" in attrs["statement"]


def test_prf_ref_resolves_instead_of_going_dead(graph):
    """A ``:prf:ref:`` names a label but not an environment. If that
    doesn't resolve, every cross-reference in a sphinx-proof project
    becomes a false dead reference."""
    assert graph.has_edge("std:file:index", "prf:proof_object:def-angular-flux")
    unresolved = [
        n for n, a in graph.nodes(data=True)
        if a.get("type") in ("unresolved", "external")
    ]
    assert unresolved == []


def test_no_dead_references_reported(query):
    """The whole fixture is internally consistent; anything reported
    here would be a false positive from the new node/edge types."""
    result = query.dead_references()
    assert result.total_dead == 0, [d.target for d in result.dead]


# ---------------------------------------------------------------------------
# Consumption — the edges have to reach a query to have earned their place
# ---------------------------------------------------------------------------


def test_provenance_from_an_equation_returns_the_spine(query):
    result = query.provenance_chain("math:equation:dd-closure")
    hops = {(r.source.name, r.relation, r.target.name) for r in result.relations}
    assert ("dd-closure", "discretizes", "transport-sn") in hops
    # Transitive: dd-closure → transport-sn → transport-continuous.
    assert ("transport-sn", "derives_from", "transport-continuous") in hops


def test_provenance_from_code_reaches_the_spine(query):
    """The motivating query: this test verifies the discrete form, which
    discretizes this continuous one."""
    result = query.provenance_chain("py:function:solver.sweep")
    hops = {(r.source.name, r.relation, r.target.name) for r in result.relations}
    assert ("dd-closure", "discretizes", "transport-sn") in hops


def test_each_relation_is_reported_once(query):
    """Roots are typically every statement on one page, so both ends of
    a link are usually roots. Dedup is by edge, so the link survives —
    and appears exactly once, not once per direction."""
    result = query.provenance_chain("math:equation:dd-closure")
    keys = [(r.source.id, r.relation, r.target.id) for r in result.relations]
    assert len(keys) == len(set(keys))


def test_provenance_accepts_a_proof_object(query):
    result = query.provenance_chain("prf:proof_object:thm-balance")
    assert result.chain[0].node.id == "prf:proof_object:thm-balance"
    assert any(
        r.target.id == "prf:proof_object:def-angular-flux" for r in result.relations
    )


def test_graph_query_reaches_the_new_types(query):
    """The generic pattern language is the escape hatch for questions no
    dedicated tool covers; new types must be addressable there."""
    hits = query.graph_query("equation -discretizes-> equation")
    assert [h["source"]["id"] for h in hits] == ["math:equation:dd-closure"]
    prf = query.graph_query("proof_object -derives_from-> proof_object")
    assert [h["target"]["id"] for h in prf] == ["prf:proof_object:def-angular-flux"]


def test_keyword_search_finds_an_environment_by_title(query):
    """The title is the human handle — ``def-angular-flux`` is not what
    anyone types."""
    assert [n.id for n in query.query("angular flux")] == [
        "prf:proof_object:def-angular-flux"
    ]


def test_proof_objects_do_not_pollute_the_equations_field(query):
    """``equations`` is a named contract; proof objects ride in the
    chain and relations instead."""
    result = query.provenance_chain("py:function:solver.sweep")
    assert all(e.id.startswith("math:equation:") for e in result.equations)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_auto_proof_label_detection():
    assert _is_auto_proof_label("remark-3", "remark")
    assert _is_auto_proof_label("theorem-12", "theorem")
    assert not _is_auto_proof_label("thm-balance", "theorem")
    # A hand-written label that merely starts with the type name.
    assert not _is_auto_proof_label("theorem-of-the-mean", "theorem")


def test_proof_title_unwraps_the_render_format():
    assert _proof_title("(Angular flux)") == "Angular flux"
    assert _proof_title("Angular flux") == "Angular flux"
    assert _proof_title("") == ""
    # Not a wrapper — leave it alone rather than mangle the title.
    assert _proof_title("(a) and (b)") == "(a) and (b)"


def test_resolve_proof_id_tries_every_environment():
    g = nx.MultiDiGraph()
    g.add_node("prf:proof_object:sweep")
    assert resolve_proof_id(g, "sweep") == "prf:proof_object:sweep"
    assert resolve_proof_id(g, "nope") is None


def test_an_environment_nexus_has_never_heard_of_still_resolves():
    """An environment sphinx-proof adds after nexus shipped must still
    resolve, or its references read as dead.

    ⚠ Re-expressed 2026-08-16, and the mechanism it used to pin is
    GONE. The id carried the environment (`prf:brandnewtype:thing`), so
    resolving a bare `:prf:ref:` meant trying all fifteen known
    environments and then scanning every node in the graph for a
    sixteenth. The id now carries the TYPE, which is `proof_object` for
    every environment there will ever be — so the property this test
    protects holds by construction and resolution is one lookup. The
    unknown kind rides in metadata, where it is not part of identity.
    """
    g = nx.MultiDiGraph()
    g.add_node("prf:proof_object:thing", prf_type="brandnewtype")
    assert resolve_proof_id(g, "thing") == "prf:proof_object:thing"
    assert resolve_proof_id(g, "absent") is None


def test_prf_xrefs_route_through_the_proof_resolver():
    g = nx.MultiDiGraph()
    g.add_node("prf:proof_object:balance")
    assert resolve_target_id(g, None, "prf", "ref", "balance") == (
        "prf:proof_object:balance"
    )


def test_statement_id_prefers_an_equation():
    g = nx.MultiDiGraph()
    g.add_node("math:equation:x")
    g.add_node("prf:proof_object:y")
    assert _resolve_statement_id("x", g) == "math:equation:x"
    assert _resolve_statement_id("y", g) == "prf:proof_object:y"
    assert _resolve_statement_id("z", g) is None


# ---------------------------------------------------------------------------
# Misuse must be loud, and must not break the build
# ---------------------------------------------------------------------------


def test_misuse_warns_without_failing_the_build(tmp_path):
    """Three ways to get a relation wrong, each one reported.

    A directive that silently declares nothing is the failure mode this
    whole feature exists to prevent, so every drop is a warning — but
    prose mistakes must never take a docs build down with them.
    """
    proc = subprocess.run(
        [
            sys.executable, "-m", "sphinx", "-q", "-E",
            str(Path(__file__).parent / "roots" / "test-proof-relations-bad"),
            str(tmp_path / "out"),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    output = proc.stdout + proc.stderr

    # No source to bind to — caught at parse time.
    assert "needs a ':label:' option" in output
    # Source and target are the same statement.
    assert "relates a statement to itself" in output
    # Target doesn't exist — only knowable at replay, against the graph.
    assert "target statement 'never-written' not found" in output


class _FakeState:
    """Just enough of a docutils state to carry a document."""

    def __init__(self, document):
        self.document = document


def _empty_document():
    return new_document("<test>", get_default_settings(RSTParser()))


def _document_with(*labelled):
    """A document holding the given (node, label) pairs in order."""
    doc = _empty_document()
    for node, label in labelled:
        node["label"] = label
        doc += node
    return doc


def _proof_node(realtype):
    node = docutils_nodes.Element()
    node["realtype"] = realtype
    return node


def test_nearest_preceding_label_takes_the_last_math_block():
    doc = _document_with(
        (docutils_nodes.math_block(), "first"),
        (docutils_nodes.math_block(), "second"),
    )
    assert _nearest_preceding_label(_FakeState(doc)) == "second"


def test_nearest_preceding_label_ignores_unlabelled_math():
    doc = _document_with((docutils_nodes.math_block(), "only"))
    doc += docutils_nodes.math_block()  # no :label:, not referenceable
    assert _nearest_preceding_label(_FakeState(doc)) == "only"


def test_nearest_preceding_label_accepts_a_proof_environment():
    doc = _document_with((_proof_node("theorem"), "thm-balance"))
    assert _nearest_preceding_label(_FakeState(doc)) == "thm-balance"


def test_nearest_preceding_label_skips_synthetic_proof_labels():
    """Binding to an unlabelled remark's serial label would name
    something the graph never holds — a confusing "not found" at
    replay instead of the honest "needs a :label:" at parse."""
    doc = _document_with(
        (docutils_nodes.math_block(), "dd-closure"),
        (_proof_node("remark"), "remark-7"),
    )
    assert _nearest_preceding_label(_FakeState(doc)) == "dd-closure"


def test_nearest_preceding_label_none_when_nothing_precedes():
    assert _nearest_preceding_label(_FakeState(_empty_document())) is None


def _relation_graph() -> nx.MultiDiGraph:
    g = nx.MultiDiGraph()
    g.add_node("math:equation:a")
    g.add_node("math:equation:b")
    return g


def test_apply_relation_writes_one_edge():
    g = _relation_graph()
    entry = {"kind": "discretizes", "from_label": "a", "to_label": "b"}
    assert _apply_relation(entry, g, "index:1") == 1
    assert g.has_edge("math:equation:a", "math:equation:b")


def test_apply_relation_is_idempotent():
    """Directive payloads persist across incremental builds and replay
    against a graph that may already hold the edge."""
    g = _relation_graph()
    entry = {"kind": "discretizes", "from_label": "a", "to_label": "b"}
    assert _apply_relation(entry, g, "index:1") == 1
    assert _apply_relation(entry, g, "index:1") == 0
    assert g.number_of_edges("math:equation:a", "math:equation:b") == 1


def test_apply_relation_skips_a_missing_end():
    g = _relation_graph()
    assert _apply_relation(
        {"kind": "discretizes", "from_label": "a", "to_label": "gone"},
        g, "index:1",
    ) == 0
    assert _apply_relation(
        {"kind": "discretizes", "from_label": "gone", "to_label": "b"},
        g, "index:1",
    ) == 0
    assert g.number_of_edges() == 0


def test_distinct_relations_between_the_same_pair_coexist():
    """Two relations are two facts; the idempotence guard is per type."""
    g = _relation_graph()
    for kind in ("discretizes", "approximates"):
        assert _apply_relation(
            {"kind": kind, "from_label": "a", "to_label": "b"}, g, "index:1",
        ) == 1
    assert g.number_of_edges("math:equation:a", "math:equation:b") == 2
