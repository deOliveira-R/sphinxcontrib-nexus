"""A page's sections and its equations were SIBLINGS, so no claim knew
its anchor.

The founding measurement (`evals/FIDELITY.md` F8, chain 3): going from
"I am editing this code" to "I have the doc section open" took **4
calls and did not close** — `node_at` → `context` → read the page ids
→ `neighbors(page, contains)` to list sections → then a guess, because
`contains` attached both sections and equations straight to the page.
On ORPHEUS the right section was found only when it happened to share
a name with the equation, which is a coincidence across two different
label namespaces; where the coincidence failed, recovery ended in
`awk` over an RST line range.

Two halves are gated here: the EXTRACTION that nests a labelled
statement under its section, and `doc_impact` — `retest`'s dual, which
walks the same cone and ends in doc claims instead of tests.
"""

from __future__ import annotations

import json
from pathlib import Path

import networkx as nx
import pytest
from docutils import nodes as dn

from sphinxcontrib.nexus.extractors import (
    _nest_labelled_statements,
    _section_anchor_index,
)
from sphinxcontrib.nexus.graph import (
    EdgeType,
    GraphEdge,
    GraphNode,
    KnowledgeGraph,
    NodeType,
)
from sphinxcontrib.nexus.query import GraphQuery


# ── the extraction half ─────────────────────────────────────────────


def _page_with_sections() -> tuple[dn.document, KnowledgeGraph]:
    """A page shaped like a real one, which means IRREGULAR.

    Three sections and only ONE of them labelled, because that is the
    ratio a real corpus has — `.. _label:` is written where a
    cross-reference is wanted, not everywhere. A fixture where every
    section is labelled cannot tell the anchor half (which works for
    all of them) from the edge half (which needs a graph node), and
    that conflation is exactly what capped the first version of this
    pass at 266 of 869 equations.
    """
    kg = KnowledgeGraph()
    kg.add_node(GraphNode(
        id="std:file:theory/page", type=NodeType.FILE, name="theory/page",
        domain="std", docname="theory/page",
    ))
    kg.add_node(GraphNode(
        id="std:section:labelled-part", type=NodeType.SECTION,
        name="labelled-part", domain="std", docname="theory/page",
        anchor="labelled-part",
    ))
    for label in ("eq-in-labelled", "eq-in-plain", "eq-nested-deep", "eq-loose"):
        kg.add_node(GraphNode(
            id=f"math:equation:{label}", type=NodeType.EQUATION, name=label,
            domain="math", docname="theory/page",
        ))

    doc = dn.document(None, None)

    labelled = dn.section(ids=["a-titled-heading", "labelled-part"])
    labelled.line = 10
    eq1 = dn.math_block(label="eq-in-labelled")
    eq1.line = 12
    labelled += eq1
    # a DEEPER subsection with no label of its own: its equation must
    # still reach the labelled ancestor
    deep = dn.section(ids=["a-subsection-nobody-labelled"])
    deep.line = 20
    eq3 = dn.math_block(label="eq-nested-deep")
    eq3.line = 22
    deep += eq3
    labelled += deep

    plain = dn.section(ids=["only-an-auto-slug"])
    plain.line = 40
    eq2 = dn.math_block(label="eq-in-plain")
    eq2.line = 42
    plain += eq2

    loose = dn.math_block(label="eq-loose")  # outside every section
    loose.line = 60

    doc += labelled
    doc += plain
    doc += loose
    doc.note_source("theory/page.rst", 0)
    return doc, kg


def _nest() -> KnowledgeGraph:
    doc, kg = _page_with_sections()
    anchors = _section_anchor_index(kg).get("theory/page", {})
    _nest_labelled_statements(doc, kg, anchors)
    return kg


def test_every_equation_in_a_section_learns_its_anchor():
    """The anchor is where a reader OPENS the page, and an unlabelled
    section has one just as much as a labelled one does."""
    g = _nest().nxgraph
    assert g.nodes["math:equation:eq-in-labelled"]["anchor"] == "a-titled-heading"
    assert g.nodes["math:equation:eq-in-plain"]["anchor"] == "only-an-auto-slug"
    assert (
        g.nodes["math:equation:eq-nested-deep"]["anchor"]
        == "a-subsection-nobody-labelled"
    ), "the INNERMOST section is the precise fragment"


def test_an_equation_outside_every_section_claims_no_anchor():
    g = _nest().nxgraph
    assert not g.nodes["math:equation:eq-loose"].get("anchor")


def test_the_containment_edge_needs_a_node_so_it_walks_UP():
    """Anchor and edge are different facts. The edge is graph
    structure, so it needs a node at the other end — the nearest
    LABELLED ancestor. Conflating the two capped the first version of
    this pass at 266 of 869 equations on ORPHEUS."""
    g = _nest().nxgraph
    parents = {
        u for u, v, d in g.edges(data=True)
        if v == "math:equation:eq-nested-deep" and d.get("type") == "contains"
    }
    assert parents == {"std:section:labelled-part"}
    # the plain section has no node, so its equation gets no edge —
    # and keeps its anchor, which is the whole point of splitting them
    assert not [
        u for u, v, d in g.edges(data=True)
        if v == "math:equation:eq-in-plain" and d.get("type") == "contains"
    ]
    assert g.nodes["math:equation:eq-in-plain"]["anchor"]


def test_lines_are_stamped_on_both_ends():
    g = _nest().nxgraph
    assert g.nodes["std:section:labelled-part"]["lineno"] == 10
    assert g.nodes["math:equation:eq-in-labelled"]["lineno"] == 12


def test_a_key_that_exists_but_is_EMPTY_still_gets_stamped():
    """`GraphNode` gives every node an `anchor` key, None for an
    equation — so `setdefault` is a silent no-op and stamped `[M]` 0 of
    903. The key existing is not the key having a value."""
    doc, kg = _page_with_sections()
    kg.nxgraph.nodes["math:equation:eq-in-labelled"]["anchor"] = None
    anchors = _section_anchor_index(kg).get("theory/page", {})
    _nest_labelled_statements(doc, kg, anchors)
    assert kg.nxgraph.nodes["math:equation:eq-in-labelled"]["anchor"]


def test_a_label_no_node_answers_to_is_left_alone():
    """A `:label:` on a page nexus did not index must not mint a node
    or an edge — the doctree is not the authority on what exists."""
    doc, kg = _page_with_sections()
    stray = dn.math_block(label="never-heard-of-it")
    stray.line = 5
    doc.children[0] += stray  # type: ignore[operator]
    before = kg.nxgraph.number_of_nodes()
    _nest_labelled_statements(
        doc, kg, _section_anchor_index(kg).get("theory/page", {})
    )
    assert kg.nxgraph.number_of_nodes() == before


# ── doc_impact: retest's dual ───────────────────────────────────────


def _cone_graph() -> nx.MultiDiGraph:
    """`caller → middle → leaf`, with claims hung at two depths and one
    of them verified, so ordering and the unverified count both have a
    witness."""
    g = nx.MultiDiGraph()
    for nid in ("py:function:pkg.leaf", "py:function:pkg.middle",
                "py:function:pkg.caller"):
        g.add_node(nid, type="function", name=nid.split(":", 2)[2], domain="py")
    g.add_edge("py:function:pkg.middle", "py:function:pkg.leaf", type="calls")
    g.add_edge("py:function:pkg.caller", "py:function:pkg.middle", type="calls")

    g.add_node("math:equation:near", type="equation", name="near", domain="math",
               docname="theory/page", anchor="the-near-part", lineno=12)
    g.add_node("math:equation:far", type="equation", name="far", domain="math",
               docname="theory/page", anchor="the-far-part", lineno=40)
    g.add_node("math:equation:unplaced", type="equation", name="unplaced",
               domain="math", docname="theory/page")
    g.add_edge("py:function:pkg.leaf", "math:equation:near",
               type="implements", source="directive")
    g.add_edge("py:function:pkg.middle", "math:equation:far",
               type="implements", source="inferred")
    g.add_edge("py:function:pkg.leaf", "math:equation:unplaced",
               type="implements", source="inferred")

    g.add_node("py:function:tests.t.test_far", type="function",
               name="tests.t.test_far", domain="py", is_test=True)
    g.add_edge("py:function:tests.t.test_far", "math:equation:far", type="tests")

    g.add_node("std:file:theory/page", type="file", name="theory/page",
               domain="std", docname="theory/page")
    g.add_edge("std:file:theory/page", "py:function:pkg.leaf", type="documents")
    return g


def _q() -> GraphQuery:
    return GraphQuery(_cone_graph())


def test_it_walks_the_same_cone_as_retest_and_ends_in_CLAIMS():
    r = _q().doc_impact("py:function:pkg.leaf")
    assert r.dependence_edges == ["calls", "type_uses", "inherits"]
    assert {c.equation.name for c in r.claims} == {"near", "far", "unplaced"}
    assert r.cone_size == 3, "the symbol plus the two that depend on it"


def test_a_claim_says_where_to_OPEN_it():
    r = _q().doc_impact("py:function:pkg.leaf")
    near = next(c for c in r.claims if c.equation.name == "near")
    assert near.location == "theory/page:12#the-near-part"


def test_a_claim_with_no_position_degrades_instead_of_lying():
    r = _q().doc_impact("py:function:pkg.leaf")
    unplaced = next(c for c in r.claims if c.equation.name == "unplaced")
    assert unplaced.location == "theory/page"


def test_a_claim_does_not_repeat_what_its_equation_node_already_says():
    """`docname` and `lineno` live on the equation node and are embedded
    in `location`; a third copy says nothing a reader cannot see. `[M]`
    duplicated on 11 of 11 claims before they became `InitVar`s — found
    by reading an actual MCP reply, not the code."""
    from sphinxcontrib.nexus._serialize import to_dict

    payload = to_dict(_q().doc_impact("py:function:pkg.leaf"))
    for claim in payload["claims"]:
        assert "docname" not in claim
        assert "lineno" not in claim
        # …and nothing was lost: both are still reachable
        assert claim["equation"]["docname"]
    near = next(c for c in payload["claims"]
                if c["equation"]["id"].endswith(":near"))
    assert near["location"] == "theory/page:12#the-near-part"


def test_location_is_a_FIELD_because_a_property_never_reaches_a_reply():
    """`to_dict` walks `fields()`, so a derived value spelled as a
    property is invisible to every JSON reply. `MarkedTestResult`
    carried `invocation` that way while its tool docstring promised
    callers would receive it."""
    from sphinxcontrib.nexus._serialize import to_dict

    r = _q().doc_impact("py:function:pkg.leaf")
    payload = to_dict(r)
    assert all("location" in c for c in payload["claims"])


def test_unverified_and_nearest_sort_first():
    """A claim about the symbol you just edited that nothing tests is
    where a reader should start."""
    r = _q().doc_impact("py:function:pkg.leaf")
    assert [(c.depth, c.verified) for c in r.claims] == sorted(
        (c.depth, c.verified) for c in r.claims
    )
    assert r.claims[0].depth == 0 and not r.claims[0].verified


def test_a_verified_claim_is_marked_as_such():
    r = _q().doc_impact("py:function:pkg.leaf")
    assert next(c for c in r.claims if c.equation.name == "far").verified
    assert not next(c for c in r.claims if c.equation.name == "near").verified
    assert r.unverified == 2


def test_a_GUESSED_claim_says_so():
    """`[M]` on ORPHEUS all 14004 `implements` edges are inferred, so a
    reader who assumes otherwise is wrong every time."""
    r = _q().doc_impact("py:function:pkg.leaf")
    assert not next(c for c in r.claims if c.equation.name == "near").inferred
    assert next(c for c in r.claims if c.equation.name == "far").inferred


def test_pages_are_the_COARSE_half_and_carry_no_anchor():
    """A `documents` edge lands on a page, not a section — so these
    cannot be dressed up as located claims."""
    r = _q().doc_impact("py:function:pkg.leaf")
    assert [p.id for p in r.pages] == ["std:file:theory/page"]


def test_an_unknown_symbol_answers_empty_rather_than_raising():
    r = _q().doc_impact("py:function:pkg.nope")
    assert r.claims == [] and r.cone_size == 0


def test_the_tool_reports_what_it_dropped():
    import sphinxcontrib.nexus.server as server

    server._query = _q()
    try:
        payload = json.loads(
            server.doc_impact.__wrapped__("py:function:pkg.leaf", limit=1)
        )
    finally:
        server._query = None
    assert len(payload["claims"]) == 1
    assert payload["omitted"] == 2


def test_the_marked_test_invocation_reaches_a_reply_too():
    """Same defect, found in the same pass: a property is invisible to
    the serializer, and `runtime_markers`' docstring said otherwise."""
    from sphinxcontrib.nexus._serialize import to_dict
    from sphinxcontrib.nexus.query import MarkedTestResult, NodeResult

    m = MarkedTestResult(
        node=NodeResult(id="py:function:t.test_x"), markers={},
        pytest_ids=["t.py::test_x"],
    )
    assert to_dict(m)["invocation"] == 'pytest "t.py::test_x"'
