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
