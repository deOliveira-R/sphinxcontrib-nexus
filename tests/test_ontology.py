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
