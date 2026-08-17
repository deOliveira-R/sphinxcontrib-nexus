"""Type mapping constants and domain-aware reference resolution."""

from __future__ import annotations

import logging
import re

from typing import TYPE_CHECKING, Any

from sphinxcontrib.nexus.graph import EdgeType, NodeType

if TYPE_CHECKING:
    import networkx as nx

logger = logging.getLogger(__name__)

def node_id(domain: str, node_type: "NodeType | str", name: str) -> str:
    """Spell a node id: ``<domain>:<type>:<name>``.

    **The type segment IS the node's type.** That is the grammar
    ``server.py`` has always advertised to MCP clients
    (``"node_id_format": "<domain>:<type>:<qualified_name>"``) — and
    before this function it was advertised rather than enforced: ids
    were built at 27 inline sites plus three producer-private helpers
    that never imported one another, each free to put something else
    there.

    `[M]` 2026-08-16, ORPHEUS: **936** nodes broke the published
    grammar — 680 ``std:label:`` typed ``section``, 94 ``std:doc:`` and
    94 two-segment ``doc:`` typed ``file``, 68 ``py:property:`` typed
    ``attribute``. Every one is a *producer's* vocabulary leaking into
    the identity space: Sphinx says ``label``, nexus's type is
    ``section``, and the id took the first while the node took the
    second. It is also how one page came to have two nodes.

    ⚠ This spells; it does not JUDGE. Whether a type is declared is a
    question for the ONTOLOGY, which a project extends with its own
    ``[node.…]`` entries — and this function has no project to ask.
    Checking against ``graph.NodeType`` here (as it briefly did) answers
    the narrower "does nexus ship this type?" and warns at a project for
    declaring exactly what the extension tier exists to let it declare.
    The check lives in :func:`~sphinxcontrib.nexus.merge.check_node_types`,
    which runs once per build with the project's ontology loaded.
    """
    value = node_type.value if isinstance(node_type, NodeType) else str(node_type)
    return f"{domain}:{value}:{name}"


def doc_node_id(docname: str) -> str:
    """The id of a documentation page.

    ``std:file:<docname>`` — ``std`` is the domain Sphinx itself reports
    a document under, and ``file`` is nexus's type for one. Two helpers
    used to disagree: one spelled it ``doc:<docname>`` with no type
    segment at all, the domain walk spelled it ``std:doc:<docname>``, so
    `[M]` all 94 pages in ORPHEUS existed TWICE — the ``doc:`` node
    carrying every ``documents`` and ``contains`` edge, and the
    ``std:doc:`` twin carrying nothing at all.
    """
    return node_id("std", NodeType.FILE, docname)

# Map (domain_name, obj_type) from Domain.get_objects() to our NodeType.
DOMAIN_TYPE_MAP: dict[tuple[str, str], NodeType] = {
    # Python domain
    ("py", "function"): NodeType.FUNCTION,
    ("py", "class"): NodeType.CLASS,
    ("py", "method"): NodeType.METHOD,
    ("py", "attribute"): NodeType.ATTRIBUTE,
    ("py", "module"): NodeType.MODULE,
    ("py", "data"): NodeType.DATA,
    ("py", "exception"): NodeType.CLASS,   # an exception IS a class
    # A property is a METHOD, not an attribute — and this line is the
    # arbiter between the two producers. The AST sees `def ng(self)`
    # inside a class and types it `method`; autodoc reports objtype
    # `property`. While that mapped to `attribute` the two disagreed
    # about what one symbol IS, so [M] 68 properties on ORPHEUS existed
    # as two nodes: the AST's holding `calls` and `type_uses`, the
    # Sphinx one holding `documents`. Neither view was complete and
    # which you got depended on the spelling you asked with.
    #
    # `method` wins because a property has a body, a file position and
    # callers, and `attribute` nodes have none of those — and because
    # `staticmethod` and `classmethod`, which are the same shape (a
    # decorated def in a class), already map here. That it is ACCESSED
    # like an attribute is a fact about call sites, not about what the
    # definition is.
    ("py", "property"): NodeType.METHOD,
    ("py", "staticmethod"): NodeType.METHOD,
    ("py", "classmethod"): NodeType.METHOD,
    ("py", "type"): NodeType.TYPE,
    # C domain
    ("c", "function"): NodeType.FUNCTION,
    ("c", "type"): NodeType.TYPE,
    ("c", "macro"): NodeType.FUNCTION,
    ("c", "var"): NodeType.DATA,
    ("c", "member"): NodeType.ATTRIBUTE,
    ("c", "enum"): NodeType.TYPE,
    ("c", "enumerator"): NodeType.DATA,
    # C++ domain
    ("cpp", "function"): NodeType.FUNCTION,
    ("cpp", "class"): NodeType.CLASS,
    ("cpp", "type"): NodeType.TYPE,
    ("cpp", "var"): NodeType.DATA,
    ("cpp", "member"): NodeType.ATTRIBUTE,
    ("cpp", "enum"): NodeType.TYPE,
    ("cpp", "enumerator"): NodeType.DATA,
    ("cpp", "namespace"): NodeType.MODULE,
    # JavaScript domain
    ("js", "function"): NodeType.FUNCTION,
    ("js", "class"): NodeType.CLASS,
    ("js", "method"): NodeType.METHOD,
    ("js", "attribute"): NodeType.ATTRIBUTE,
    ("js", "module"): NodeType.MODULE,
    ("js", "data"): NodeType.DATA,
    # Standard domain
    ("std", "term"): NodeType.TERM,
    ("std", "label"): NodeType.SECTION,
    ("std", "doc"): NodeType.FILE,
}

# What a NON-Python reftype denotes, for a reference that did not resolve.
#
# An unresolved node's id records what the NAME DENOTES while its ``type``
# records that nothing was found — the id/type pair the grammar explicitly
# allows for placeholders, and what lets a later pass upgrade
# ``std:section:foo`` in place once the label appears. Before this, the raw
# reftype went into the id (``std:ref:foo``, ``std:any:foo``), so the segment
# named a ROLE, which is a thing no node is ever typed as.
#
# Anything unlisted denotes something nexus has no type for, and says so.
REFTYPE_NODETYPE_MAP: dict[str, NodeType] = {
    "ref": NodeType.SECTION,
    "numref": NodeType.SECTION,
    "keyword": NodeType.SECTION,
    "term": NodeType.TERM,
    "doc": NodeType.FILE,
}

# ``sphinx-proof`` environment names. The upstream ``prf`` domain exposes
# neither ``object_types`` nor ``get_objects()`` — everything lives in
# ``env.proof_list`` — so the list can't be derived from the domain and is
# kept here instead. It is used to resolve a bare ``:prf:ref:`` label (which
# carries no environment name) to a ``prf:<type>:<label>`` node id.
#
# Not imported from ``sphinx_proof``: a graph built elsewhere can contain
# prf nodes while the local environment has no sphinx-proof installed, so
# the list has to stand on its own. ``resolve_target_id`` falls back to a
# prefix scan for any type added upstream after this was written.
# ⚠ RETIRED as a resolution mechanism 2026-08-16 — `resolve_proof_id`
# is one lookup now. Kept only as the documented vocabulary of
# environments a `prf_type` may hold; nothing branches on it.
PRF_OBJECT_TYPES: tuple[str, ...] = (
    "algorithm",
    "assumption",
    "axiom",
    "conjecture",
    "corollary",
    "criterion",
    "definition",
    "example",
    "lemma",
    "notation",
    "observation",
    "property",
    "proposition",
    "remark",
    "theorem",
)

# Map pending_xref reftype to EdgeType.
REFTYPE_EDGE_MAP: dict[str, EdgeType] = {
    "ref": EdgeType.REFERENCES,
    "doc": EdgeType.REFERENCES,
    "func": EdgeType.DOCUMENTS,
    "meth": EdgeType.DOCUMENTS,
    "class": EdgeType.DOCUMENTS,
    "mod": EdgeType.DOCUMENTS,
    "attr": EdgeType.DOCUMENTS,
    "data": EdgeType.DOCUMENTS,
    "exc": EdgeType.DOCUMENTS,
    "type": EdgeType.DOCUMENTS,
    "obj": EdgeType.DOCUMENTS,
    "term": EdgeType.REFERENCES,
    "eq": EdgeType.EQUATION_REF,
    "numref": EdgeType.REFERENCES,
    "keyword": EdgeType.REFERENCES,
    "token": EdgeType.REFERENCES,
    "option": EdgeType.REFERENCES,
    "envvar": EdgeType.REFERENCES,
    "citation": EdgeType.CITES,
}

# Map a Python-domain reftype (the ROLE as written, ``:func:``) to the
# objtype Sphinx registers the object under (``function``). A node id is
# built from the objtype, so skipping this step spells the same symbol two
# ways and splits its edges between them.
#
# It lives here because BOTH producers need it and they are in different
# modules: the docstring scanner in ``ast_analyzer`` and the doctree walker
# in ``extractors``. It was a local dict inside the first of those until
# 2026-08-16, so the second built ids from the raw role — measured on
# ORPHEUS: 316 short-prefix nodes (``py:func`` 206, ``py:mod`` 55,
# ``py:meth`` 23, ``py:attr`` 22, ``py:obj`` 8, ``py:exc`` 2) and **265
# symbols carrying both spellings at once**, each holding part of the
# symbol's edges.
#
# ``obj`` is deliberately not an identity: ``:obj:`` is the generic
# cross-reference and the Python domain files those under ``function``.
REFTYPE_OBJTYPE_MAP: dict[str, str] = {
    "func": "function",
    "meth": "method",
    "class": "class",
    "mod": "module",
    "attr": "attribute",
    "data": "data",
    "exc": "class",       # `:exc:` names a class; see NodeType
    "obj": "function",
}


# ---------------------------------------------------------------------------
# Candidate ranking — the one answer to "which node did this name mean?"
# ---------------------------------------------------------------------------
#
# Three passes independently ask that question, at three different moments:
#
#   * ``resolve_target_id`` (below) — a Sphinx ``pending_xref`` at
#     doctree-read time, with a reftype and domain in hand.
#   * ``ast_analyzer._canonicalize_phantoms`` — a post-merge fold of
#     placeholder nodes onto the real definitions they shadow.
#   * ``merge.merge_graphs`` — the same node id typed differently by the
#     Sphinx and AST sides.
#
# They used to carry three separate rank tables; two were byte-identical
# copies and the third was a binary real-vs-placeholder test. Divergence
# between them is how a graph starts disagreeing with itself about what a
# name refers to, so the ranking lives here once and the passes differ
# only in what they do with the verdict.

#: Concreteness ranking. Lower is more concrete, so a real definition
#: sorts ahead of the placeholders (``external`` / ``unresolved`` /
#: untyped) that exist only because some reference could not be placed.
#: ``class`` beats ``function`` so a mistyped constructor call lands on
#: the class rather than a same-leaf function elsewhere.
TYPE_RANK: dict[str, int] = {
    NodeType.CLASS.value: 0,
    NodeType.METHOD.value: 2,
    NodeType.FUNCTION.value: 3,
    NodeType.TYPE.value: 4,
    NodeType.ATTRIBUTE.value: 5,
    NodeType.DATA.value: 6,
    NodeType.MODULE.value: 7,
    NodeType.EQUATION.value: 8,
    NodeType.SECTION.value: 9,
    NodeType.TERM.value: 10,
    NodeType.FILE.value: 11,
    NodeType.EXTERNAL.value: 12,
    NodeType.UNRESOLVED.value: 13,
    "": 14,
}

#: Node types that stand in for a symbol nexus could not place. They are
#: minted so a reference has *something* to point at; none of them is a
#: definition, and binding a live reference to one manufactures a dead
#: reference out of a symbol that exists.
PLACEHOLDER_TYPES: frozenset[str] = frozenset(
    {NodeType.UNRESOLVED.value, NodeType.EXTERNAL.value, ""}
)

#: How many leading components of :func:`candidate_rank` describe *what
#: kind of thing* a candidate is, as opposed to which of several equally
#: good ones to pick. Two candidates agreeing across this slice are
#: genuinely ambiguous — see :func:`candidates_are_ambiguous`.
_IDENTITY_RANK_WIDTH = 4


def candidate_rank(
    node_id: str,
    name: str,
    attrs: dict[str, Any],
    objtype_rank: int = 0,
) -> tuple[int, int, int, int, int, str]:
    """Rank one candidate node as the referent of a name.

    Sorted ascending; the smallest key wins. In order:

    1. **Real definition over placeholder.** Unconditional, and ahead of
       the role's own preference: a ``:exc:`` role must not bind to an
       ``unresolved`` ``py:exception:`` tombstone when a real class of
       that name exists.
    2. **The role's type preference** (``objtype_rank``), for callers
       that have one — a ``:class:`` role prefers a class. Callers
       without reftype context pass ``0`` and skip this level.
    3. **Concreteness** (:data:`TYPE_RANK`).
    4. **File-backed over not** — a node with a ``file_path`` came from
       a real definition site.
    5. **Shortest qualified name** — prefer ``pkg.solve`` to
       ``pkg.deep.nested.solve`` when nothing above separates them.
    6. **Node id**, so the order is total and stable across builds.
    """
    node_type = attrs.get("type") or ""
    return (
        1 if node_type in PLACEHOLDER_TYPES else 0,
        objtype_rank,
        TYPE_RANK.get(node_type, 99),
        0 if attrs.get("file_path") else 1,
        len(name),
        node_id,
    )


def test_node_is_off_limits(
    nxgraph: "nx.MultiDiGraph", candidate_id: str, phantom_id: str,
) -> bool:
    """True when a test-tree node must not absorb this phantom.

    Bare-name fuzzy matching is deliberately more permissive than
    Sphinx: an api page writing ``:class:`CPMesh``` with no
    ``currentmodule`` fails Sphinx's own lookup and renders as plain
    text, while nexus resolves it. That generosity is worth keeping —
    it recovers thousands of real doc-page-to-class links.

    The test tree is where it goes wrong. Test modules are full of short
    generic names (``K``, ``record``, ``slab``, ``omega_dot_n``), so
    they act as a magnet for any bare name with no better candidate.
    Measured on ORPHEUS: 578 bare references from production code landed
    on test helpers — a reactor-physics operator's ``:attr:`K``` bound
    to a test class's attribute.

    Production code does not reference test helpers, so the direction is
    the discriminator, not the name. Test-to-test bare references are
    untouched; only a candidate inside the test tree, for a phantom that
    something outside it references, is refused.
    """
    if not (nxgraph.nodes.get(candidate_id) or {}).get("in_test_file"):
        return False
    # The phantom is shared by every referrer that spelled the name, so
    # one production referrer is enough to rule the candidate out.
    return any(
        not (nxgraph.nodes.get(src) or {}).get("in_test_file")
        for src, _ in nxgraph.in_edges(phantom_id)
    )


def candidates_are_ambiguous(ranked: list[tuple[Any, ...]]) -> bool:
    """True when the top candidates are indistinguishable in kind.

    Levels 5 and 6 of :func:`candidate_rank` (name length, node id) exist
    to make the sort total, not to justify a choice — two candidates
    separated only by those are a coin flip. Callers that must not guess
    check this and decline; callers that must return *something* take the
    minimum and accept the tiebreak.
    """
    if len(ranked) < 2:
        return False
    head = ranked[0][:_IDENTITY_RANK_WIDTH]
    return sum(1 for key in ranked if key[:_IDENTITY_RANK_WIDTH] == head) > 1


def resolve_proof_id(nxgraph: nx.MultiDiGraph, label: str) -> str | None:
    """Resolve a bare ``sphinx-proof`` label to its node id.

    One lookup, because the id is ``prf:proof_object:<label>``.

    Until 2026-08-16 the id carried the ENVIRONMENT — ``prf:theorem:X``,
    ``prf:lemma:X`` — which is a *kind*, not a node type, and was already
    recorded in ``metadata["prf_type"]``. Storing it twice cost exactly
    what a duplicated source of truth always costs: a bare
    ``:prf:ref:`sn-sweep``` names the label and NOT the environment, so
    resolution had to try all fifteen known environments in turn and then
    scan every node in the graph for anything sphinx-proof had grown
    since. That whole cascade was the id refusing to say what the node is.
    """
    nid = node_id("prf", NodeType.PROOF_OBJECT, label)
    return nid if nid in nxgraph else None


def resolve_target_id(
    nxgraph: nx.MultiDiGraph,
    domain: Any | None,
    refdomain: str,
    reftype: str,
    reftarget: str,
) -> str | None:
    """Resolve a pending_xref's attributes to a node ID in the graph.

    Uses the domain's object_types to programmatically map reftypes
    (e.g. "func") to obj_types (e.g. "function") instead of hardcoding.
    Falls back to suffix matching for short-name references
    (e.g. "CPMesh" matching "collision_probability.CPMesh").

    Returns the node ID if found, None if unresolved.
    """
    # Special cases: :doc: and :eq: have their own ID schemes
    if reftype == "doc":
        nid = doc_node_id(reftarget)
        return nid if nid in nxgraph else None
    if reftype == "eq":
        nid = f"math:equation:{reftarget}"
        return nid if nid in nxgraph else None
    if reftype == "numref":
        # ``:numref:`peierls-3d``` on an equation label is a legitimate
        # and common spelling — it renders the equation NUMBER rather
        # than the label, but it names the same target. Without this it
        # minted a ``math:numref:`` placeholder standing beside the real
        # equation node, splitting one equation's references across two
        # ids. Falls through to the ordinary std-domain path (figures,
        # tables, sections) when no such equation exists.
        nid = f"math:equation:{reftarget}"
        if nid in nxgraph:
            return nid
        # Figures and tables carry ``:name:`` labels, which Sphinx's std
        # domain publishes as ordinary labels — the same node a ``:ref:``
        # to that name would bind to. Exact key, no fuzzy matching, so
        # there is no ambiguity to get wrong: the reference either names
        # a label that exists or it does not.
        nid = node_id("std", NodeType.SECTION, reftarget)
        if nid in nxgraph:
            return nid
    if refdomain == "prf":
        return resolve_proof_id(nxgraph, reftarget)

    # Collect candidate obj_types for this reftype
    candidate_objtypes = [reftype]
    if domain is not None:
        for obj_type_name, obj_type in getattr(domain, "object_types", {}).items():
            if reftype in obj_type.roles and obj_type_name not in candidate_objtypes:
                candidate_objtypes.append(obj_type_name)

    # Try exact match first — the common case, and an O(1) lookup
    # rather than a scan of the whole graph.
    #
    # A placeholder does NOT short-circuit. ``py:function:compute_G_bc``
    # is a tombstone: it exists only because some bare role could not be
    # placed, so binding a live reference to it manufactures a dead
    # reference out of a symbol that does exist. Hold it as a fallback
    # and let the suffix scan look for a real definition first.
    exact_placeholder: str | None = None
    for objtype in candidate_objtypes:
        nid = f"{refdomain}:{objtype}:{reftarget}"
        if nid in nxgraph:
            if nxgraph.nodes[nid].get("type", "") not in PLACEHOLDER_TYPES:
                return nid
            if exact_placeholder is None:
                exact_placeholder = nid

    # Suffix match: "CPMesh" matches "collision_probability.CPMesh".
    #
    # Several nodes routinely share a suffix — a live definition and a
    # placeholder minted from some retired import path can both end in
    # ``.compute_G_bc``. Returning whichever the graph happens to yield
    # first makes resolution depend on insertion order, and picking the
    # placeholder invents a dead reference out of a live symbol, so every
    # candidate is ranked by the shared :func:`candidate_rank`.
    #
    # A reference must produce an edge, so an ambiguous best is taken
    # rather than declined — unlike the phantom fold, which rewires the
    # graph and must not guess. That difference in consequence, not in
    # ranking, is why the two passes read the same verdict differently.
    suffix = f".{reftarget}"
    best: tuple[int, int, int, int, int, str] | None = None
    for objtype_rank, objtype in enumerate(candidate_objtypes):
        prefix = f"{refdomain}:{objtype}:"
        # NOT `node_id` — that is the module-level id BUILDER, and
        # binding it here would make it local to this whole function,
        # so the call in the numref branch above raises
        # UnboundLocalError. (It did; pyright said so and was believed
        # to be cross-tree noise.)
        for candidate in nxgraph:
            if not isinstance(candidate, str) or not candidate.startswith(prefix):
                continue
            name = candidate[len(prefix):]
            if not (name.endswith(suffix) or name == reftarget):
                continue
            key = candidate_rank(
                candidate, name, nxgraph.nodes[candidate], objtype_rank,
            )
            if best is None or key < best:
                best = key

    if best is not None and best[0] == 0:
        return best[-1]

    # Nothing real matched anywhere. An exact-name placeholder is the
    # better answer than a suffix-matched one — equally uninformative,
    # but at least it is the name the author actually wrote, and reusing
    # it avoids minting a second phantom for the same symbol.
    if exact_placeholder is not None:
        return exact_placeholder
    return best[-1] if best is not None else None


# Shape of a plausible dotted reference target after line-wrap
# whitespace is removed: dotted identifiers, plus ``-`` for equation
# labels. Used to decide whether whitespace inside a role body is
# docstring wrapping (collapse it) or meaningful content like inline
# LaTeX (leave it alone).
_DOTTED_TARGET_RE = re.compile(r"[A-Za-z_][\w.-]*")


def _normalize_wrapped_target(candidate: str) -> str:
    """Collapse LINE-WRAP whitespace inside a dotted role target.

    A long ``:class:`pkg.mod.Thing``` reference wraps across source lines,
    leaving a newline + indent in the middle of the dotted path. Sphinx
    normalizes that away when it resolves the role; without the same
    normalization the graph forges a phantom whose name contains a newline —
    unresolvable by definition.

    Only whitespace runs that CONTAIN A NEWLINE are collapsed, and only when
    no whitespace survives. That is what separates a wrapped *name* from
    ordinary *text*, and the distinction is load-bearing:

    - ``"Foo.\\n    bar"`` → ``"Foo.bar"`` — a name broken by a line wrap.
    - ``"x + y"`` → unchanged — inline LaTeX; the spaces are content.
    - ``"dict mapping material ID to Mixture."`` → unchanged — prose, which
      napoleon emits as a ``:class:`` target from a malformed type line.

    ⚠ The third case is why the newline condition exists. This used to
    collapse ALL whitespace whenever the result matched ``_DOTTED_TARGET_RE``
    — and a sentence of letters, spaces and a full stop matches it once the
    spaces are gone. Downstream, ``_classify_unresolved`` rejects a target
    that is not a valid identifier as napoleon noise, so the space-bearing
    prose was being dropped exactly as intended; collapsing it first
    *disguised prose as an identifier* and walked it straight through that
    gate. ``[M]`` 2026-08-16: applying the loose version on the doctree path
    minted **48** junk classes on ORPHEUS — ``py:class:allkeyvariables.``,
    ``py:class:dictmappingmaterialIDtoMixture.``, ``py:class:default0``.
    Normalizing before classifying can only ever *add* things the classifier
    was built to refuse.
    """
    if not any(ch.isspace() for ch in candidate):
        return candidate
    collapsed = re.sub(r"\s*\n\s*", "", candidate)
    if any(ch.isspace() for ch in collapsed):
        # Whitespace that was not a line wrap survived — content, not a name.
        return candidate
    if _DOTTED_TARGET_RE.fullmatch(collapsed.lstrip(".")):
        return collapsed
    return candidate
