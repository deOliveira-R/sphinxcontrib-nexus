"""The ontology, and the pin that stops it drifting from the enums.

The load-bearing test here is
:func:`test_ontology_describes_exactly_the_shipped_vocabulary`. The
ontology carries the *semantics* while ``graph.NodeType`` /
``graph.EdgeType`` carry the *names*, which is only safe as long as the
two cannot diverge — so the pin is bidirectional: an enum member with no
description fails, and a description with no enum member fails.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from sphinxcontrib.nexus.graph import EdgeType, NodeType
from sphinxcontrib.nexus.ontology import (
    ANY,
    Enforcement,
    Ontology,
)


# ---------------------------------------------------------------------------
# The anti-drift pin
# ---------------------------------------------------------------------------


def test_ontology_describes_exactly_the_shipped_vocabulary():
    onto = Ontology.load()

    enum_nodes = {member.value for member in NodeType}
    enum_edges = {member.value for member in EdgeType}

    undescribed_nodes, undescribed_edges = onto.undescribed(enum_nodes, enum_edges)
    orphan_nodes, orphan_edges = onto.orphaned(enum_nodes, enum_edges)

    assert undescribed_nodes == [], (
        "NodeType members with no ontology entry — describe them in "
        f"ontology.toml: {undescribed_nodes}"
    )
    assert undescribed_edges == [], (
        "EdgeType members with no ontology entry — describe them in "
        f"ontology.toml: {undescribed_edges}"
    )
    assert orphan_nodes == [], (
        f"ontology.toml describes node types that no longer exist: {orphan_nodes}"
    )
    assert orphan_edges == [], (
        f"ontology.toml describes edge types that no longer exist: {orphan_edges}"
    )


def test_every_edge_names_a_domain_and_range():
    onto = Ontology.load()
    for name, spec in onto.edges.items():
        assert spec.domain, f"{name} declares no domain"
        assert spec.range, f"{name} declares no range"


def test_declared_domain_and_range_name_real_node_types():
    """A typo in a domain list would silently widen the rule to nothing."""
    onto = Ontology.load()
    known = set(onto.nodes) | {ANY}
    for name, spec in onto.edges.items():
        assert set(spec.domain) <= known, f"{name}: unknown type in domain"
        assert set(spec.range) <= known, f"{name}: unknown type in range"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_domain_violation_is_reported():
    onto = Ontology.load()
    # `discriminates_on` may only target a tag.
    violation = onto.check_edge("discriminates_on", "function", "equation")
    assert violation is not None
    assert "range" in violation.reason


def test_admissible_edge_is_silent():
    onto = Ontology.load()
    assert onto.check_edge("implements", "function", "equation") is None
    assert onto.check_edge("tests", "method", "equation") is None


def test_a_test_may_not_implement_an_equation():
    """The #49 rule, as a declared constraint rather than a hardcoded one."""
    onto = Ontology.load()
    violation = onto.check_edge(
        "implements",
        "class",
        "equation",
        source_attrs={"in_test_file": True},
        source_id="py:class:tests.test_slab.TestSlab",
    )
    assert violation is not None
    assert "in_test_file" in violation.reason

    # …and the same edge from production code is admissible.
    assert (
        onto.check_edge(
            "implements", "class", "equation", source_attrs={"in_test_file": False}
        )
        is None
    )


def test_unknown_edge_type_is_not_a_violation():
    """An undescribed type is drift, reported elsewhere — not a bad edge.

    Failing here would make every extractor addition red before its
    description lands, which trains people to disable the check.
    """
    onto = Ontology.load()
    assert onto.check_edge("exercises", "function", "function") is None


def test_enforcement_none_never_reports():
    onto = Ontology.load()
    assert onto.edges["references"].enforcement is Enforcement.NONE
    assert onto.check_edge("references", "tag", "tag") is None


# ---------------------------------------------------------------------------
# Project extension
# ---------------------------------------------------------------------------


def _write_project_ontology(tmp_path, body: str):
    (tmp_path / ".nexus").mkdir()
    (tmp_path / ".nexus" / "ontology.toml").write_text(textwrap.dedent(body))
    return tmp_path


def test_project_extension_adds_types(tmp_path):
    root = _write_project_ontology(
        tmp_path,
        """
        [edge.exercises]
        description = "This code ran under this test."
        domain = ["function", "method"]
        range = ["function", "method"]
        enforcement = "warn"
        sources = ["runtime"]
        """,
    )
    onto = Ontology.load(root)
    assert "exercises" in onto.edges
    assert onto.edges["exercises"].sources == ("runtime",)
    # The base is still there.
    assert "implements" in onto.edges
    assert len(onto.sources) == 2

    # And it now validates.
    violation = onto.check_edge("exercises", "function", "equation")
    assert violation is not None


def test_project_extension_may_not_redefine_a_base_type(tmp_path):
    root = _write_project_ontology(
        tmp_path,
        """
        [edge.implements]
        description = "something else entirely"
        domain = ["*"]
        range = ["*"]
        """,
    )
    with pytest.raises(ValueError, match="may not be redefined"):
        Ontology.load(root)


def test_attribute_groups_survive_the_round_trip(tmp_path):
    root = _write_project_ontology(
        tmp_path,
        """
        [attribute.claim_kind]
        applies_to = "edge:tests"
        values = ["symbolic", "invariant", "analytical", "regression"]
        groups.THEOREM = ["symbolic", "invariant"]
        groups.REFERENCE = ["analytical"]
        groups.RECORD = ["regression"]
        """,
    )
    onto = Ontology.load(root)
    spec = onto.attributes["claim_kind"]
    assert spec.applies_to == "edge:tests"
    assert spec.groups["THEOREM"] == ("symbolic", "invariant")
    assert set(spec.values) == {"symbolic", "invariant", "analytical", "regression"}


def test_load_without_a_project_yields_the_base_alone():
    onto = Ontology.load()
    assert len(onto.sources) == 1
    assert onto.sources[0].name == "ontology.toml"


# ---------------------------------------------------------------------------
# `extend` — a project may WIDEN a base declaration, never narrow it (#69)
# ---------------------------------------------------------------------------
#
# Before this, `_guard_redefinition` was the ONLY rule, so the extension tier
# supported exactly one verb: add a new name. A project could declare its own
# node type and then had no way to say that type is a valid target of a BASE
# edge — the two-tier vocabulary was not expressible. The only escape was the
# `ANY = "*"` wildcard, which buys openness by giving up range checking exactly
# where a project is most likely to get it wrong.


def test_an_extension_widens_a_base_edges_range(tmp_path):
    root = _write_project_ontology(
        tmp_path,
        """
        [node.equation_variant]
        description = "A project-specific flavour of equation."

        [extend.edge.implements]
        range = ["equation_variant"]
        """,
    )
    onto = Ontology.load(root)
    spec = onto.edges["implements"]
    assert spec.admits_target("equation_variant")
    # The base entries are kept, not replaced.
    assert "equation" in spec.range


def test_widening_is_monotone_over_every_node_type(tmp_path):
    """The property that makes widening safe, asserted rather than argued.

        base.admits_target(t)  ⟹  extended.admits_target(t)     ∀ t

    Union gives this by construction — which is exactly why narrowing must be
    refused rather than discouraged. A project able to REMOVE a type from a
    range would silently invalidate every pass written against the base, and
    the breakage would surface as a missing edge, not as an error.
    """
    base = Ontology.load()
    root = _write_project_ontology(
        tmp_path,
        """
        [node.equation_variant]
        description = "A project-specific flavour of equation."

        [extend.edge.implements]
        domain = ["module"]
        range = ["equation_variant"]
        """,
    )
    extended = Ontology.load(root)

    universe = sorted(set(base.nodes) | set(extended.nodes))
    assert universe, "no node types to quantify over — the check would be vacuous"

    for name, base_spec in base.edges.items():
        ext_spec = extended.edges[name]
        for node_type in universe:
            assert not (
                base_spec.admits_source(node_type)
                and not ext_spec.admits_source(node_type)
            ), f"{name}: extension NARROWED domain, losing {node_type!r}"
            assert not (
                base_spec.admits_target(node_type)
                and not ext_spec.admits_target(node_type)
            ), f"{name}: extension NARROWED range, losing {node_type!r}"

    # …and the widening actually happened, so the loop above is not vacuous.
    assert extended.edges["implements"].admits_source("module")
    assert not base.edges["implements"].admits_source("module")


@pytest.mark.parametrize(
    "body, because",
    [
        (
            "[extend.edge.implements]\nenforcement = \"error\"\n",
            "a scalar has no wider value; setting one is a redefinition",
        ),
        (
            "[extend.edge.implements]\ndefault_confidence = 0.9\n",
            "likewise a scalar",
        ),
        (
            "[extend.edge.implements]\nforbid_source_attr = {is_test = true}\n",
            "a set whose members SUBTRACT — adding one narrows the edge",
        ),
    ],
)
def test_an_extension_may_not_touch_a_non_widenable_field(tmp_path, body, because):
    root = _write_project_ontology(tmp_path, body)
    with pytest.raises(ValueError, match="may only widen"):
        Ontology.load(root)


def test_extending_something_that_does_not_exist_is_an_error(tmp_path):
    """A typo must not silently mint a half-declared edge."""
    root = _write_project_ontology(
        tmp_path, "[extend.edge.implments]\nrange = [\"equation\"]\n"
    )
    with pytest.raises(ValueError, match="does not exist"):
        Ontology.load(root)


def test_the_redefinition_error_now_names_the_way_forward(tmp_path):
    """The guard that used to be the only rule must point at the new verb.

    Its message is what an author reads when they try the wrong thing, so it
    is the discoverability surface for `extend` — a guard that refuses without
    naming the alternative teaches the author that the thing is impossible.
    """
    root = _write_project_ontology(
        tmp_path,
        """
        [edge.implements]
        description = "redefining a base edge"
        range = ["equation"]
        """,
    )
    with pytest.raises(ValueError, match=r"\[extend\.edge\.implements\]"):
        Ontology.load(root)


# ---------------------------------------------------------------------------
# The origin pin — a declared type must be producible by who it names
# ---------------------------------------------------------------------------
#
# `origin` tells a reader (and an agent, and a doc author) WHICH producer
# assigns a type. Nothing checked it, and [M] 2026-08-16 two of the
# fourteen were false: `exception` and `type` both declared `origin =
# "ast"` while `NodeType.EXCEPTION` / `NodeType.TYPE` appear in
# `ast_analyzer.py` only inside lookup tables, never at an assignment
# site. Both are reachable ONLY through `DOMAIN_TYPE_MAP`, i.e. only when
# autodoc happened to document the symbol.
#
# The consequence is not cosmetic: on ORPHEUS exactly 2 classes were
# typed `exception` while 24 more exception classes were typed `class`,
# because the discriminator was "did someone write a docs page for it".
#
# These gates ask the question by RUNNING the producer, not by grepping
# for the enum — grepping is what makes a lookup-table mention look like
# an assignment.


#: Every code construct nexus claims an `origin = "ast"` type for.
_AST_KITCHEN_SINK = '''
"""A module exercising every construct the AST analyzer types."""

CONSTANT = 3                      # -> data
TypeAlias = int                   # -> type?  (declared origin: ast)


class Thing:                      # -> class
    attr: int = 1                 # -> attribute

    def method(self):             # -> method
        pass

    @property
    def prop(self):               # -> ? (declared nowhere)
        return 1


class Boom(ValueError):           # -> exception?  (declared origin: ast)
    pass


def free(geometry):               # -> function
    if geometry == "slab":        # -> tag (discriminates_on)
        return 1
    return 2
'''


def _types_the_ast_analyzer_emits(tmp_path) -> set[str]:
    from sphinxcontrib.nexus.ast_analyzer import analyze_directory

    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "mod.py").write_text(_AST_KITCHEN_SINK)
    kg = analyze_directory(tmp_path, exclude_patterns=[])
    return {
        a.get("type") for _n, a in kg.nxgraph.nodes(data=True) if a.get("type")
    }


def _types_assigned_in(module_name: str) -> set[str]:
    """Types a module assigns, by reading its source for ``type=``.

    ⚠ Deliberately NOT a hand-maintained list, and not a grep for
    ``NodeType.X``. A hand list makes this gate self-fulfilling — you add
    a type, you add it to the list, the gate agrees. A grep is worse: it
    matches lookup-table membership, which is exactly how `exception`
    looked producible for as long as it wasn't.

    A `type=` keyword argument is the one position that actually
    classifies a node, so that is what is matched.
    """
    import ast
    import importlib

    src = Path(importlib.import_module(module_name).__file__).read_text()
    found: set[str] = set()
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.keyword) or node.arg != "type":
            continue
        value = node.value
        if isinstance(value, ast.Attribute) and value.attr == "value":
            value = value.value            # NodeType.X.value -> NodeType.X
        if (
            isinstance(value, ast.Attribute)
            and isinstance(value.value, ast.Name)
            and value.value.id == "NodeType"
        ):
            found.add(getattr(NodeType, value.attr).value)
    return found


def _types_the_sphinx_side_emits() -> set[str]:
    """Types the doctree walker and the declaring directives can assign.

    Two sources, both measured: `DOMAIN_TYPE_MAP` translates whatever
    `domain.get_objects()` reports, and `extractors` / `directives`
    construct the rest directly.
    """
    from sphinxcontrib.nexus._mappings import DOMAIN_TYPE_MAP

    return (
        {t.value for t in DOMAIN_TYPE_MAP.values()}
        | _types_assigned_in("sphinxcontrib.nexus.extractors")
        | _types_assigned_in("sphinxcontrib.nexus.directives")
    )


def test_every_declared_origin_names_a_producer_that_can_emit_it(tmp_path):
    """`origin` is a claim about which producer assigns the type.

    A type whose declared origin cannot produce it is worse than an
    undocumented one: it tells a reader the graph will classify their
    code, and it will not. [M] 2026-08-16 this gate was RED on
    `exception` and `type`, both declaring `ast`.

    ⚠ What it does NOT catch, said plainly so it is not credited with
    more: a type BOTH producers can emit, declared to the wrong one.
    `function` is assignable by the AST walker AND through
    `DOMAIN_TYPE_MAP`, so flipping its origin passes here. The claim
    gated is "the named producer CAN emit this" — not "the named
    producer is the only, or the usual, one".
    """
    onto = Ontology.load()
    emitters = {
        "ast": _types_the_ast_analyzer_emits(tmp_path),
        "sphinx": _types_the_sphinx_side_emits(),
    }

    unproducible = []
    for name, spec in onto.nodes.items():
        producible = emitters.get(spec.origin)
        if producible is None:       # "derived" — post-processing, not gated here
            continue
        if name not in producible:
            unproducible.append((name, spec.origin))

    assert unproducible == [], (
        "node types whose declared `origin` cannot actually emit them — "
        "either the producer must learn to assign the type, or the "
        "declared origin is wrong:\n  "
        + "\n  ".join(f"[node.{n}] origin = {o!r}" for n, o in unproducible)
    )


def test_the_ast_analyzer_does_not_type_an_exception_class_as_one(tmp_path):
    """Pins the fact that made `exception`'s origin false, so the day
    the AST learns to assign it, this gate says so and the origin moves
    back with it.

    ⚠ This asserts a LIMITATION, deliberately. `exception` is slated for
    retirement (an exception IS a class — one realization, no morphism,
    so it fails the type-minting criterion), and whichever way that
    lands, this gate has to be revisited rather than silently kept
    green.
    """
    emitted = _types_the_ast_analyzer_emits(tmp_path)
    assert NodeType.CLASS.value in emitted
    assert NodeType.EXCEPTION.value not in emitted
