"""Type mapping constants and domain-aware reference resolution."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sphinxcontrib.nexus.graph import EdgeType, NodeType

if TYPE_CHECKING:
    import networkx as nx

# Map (domain_name, obj_type) from Domain.get_objects() to our NodeType.
DOMAIN_TYPE_MAP: dict[tuple[str, str], NodeType] = {
    # Python domain
    ("py", "function"): NodeType.FUNCTION,
    ("py", "class"): NodeType.CLASS,
    ("py", "method"): NodeType.METHOD,
    ("py", "attribute"): NodeType.ATTRIBUTE,
    ("py", "module"): NodeType.MODULE,
    ("py", "data"): NodeType.DATA,
    ("py", "exception"): NodeType.EXCEPTION,
    ("py", "property"): NodeType.ATTRIBUTE,
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
    NodeType.EXCEPTION.value: 1,
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

    ``:prf:ref:`sn-sweep``` names the label but not the environment, so
    the id has to be reconstructed by trying each known environment.
    Returns ``None`` when no proof object carries the label.
    """
    for objtype in PRF_OBJECT_TYPES:
        nid = f"prf:{objtype}:{label}"
        if nid in nxgraph:
            return nid
    # An environment sphinx-proof grew after PRF_OBJECT_TYPES was
    # written. Scan rather than report a live reference as dead.
    tail = f":{label}"
    for node_id in nxgraph:
        if (
            isinstance(node_id, str)
            and node_id.startswith("prf:")
            and node_id.endswith(tail)
        ):
            return node_id
    return None


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
        nid = f"doc:{reftarget}"
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
        for node_id in nxgraph:
            if not isinstance(node_id, str) or not node_id.startswith(prefix):
                continue
            name = node_id[len(prefix):]
            if not (name.endswith(suffix) or name == reftarget):
                continue
            key = candidate_rank(
                node_id, name, nxgraph.nodes[node_id], objtype_rank,
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
