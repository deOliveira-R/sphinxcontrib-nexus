"""Merge AST-derived graph into Sphinx-derived graph."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sphinxcontrib.nexus._mappings import (
    TYPE_RANK,
    candidate_rank,
    candidates_are_ambiguous,
    test_node_is_off_limits,
)
from sphinxcontrib.nexus.graph import EdgeType, KnowledgeGraph, NodeType

if TYPE_CHECKING:
    from pathlib import Path

    import networkx as nx

logger = logging.getLogger(__name__)


def merge_graphs(
    sphinx_kg: KnowledgeGraph,
    ast_kg: KnowledgeGraph,
) -> KnowledgeGraph:
    """Merge an AST-derived graph into a Sphinx-derived graph.

    Rules:
    1. Node in both: keep Sphinx attrs, add AST metadata (file_path, lineno)
    2. Node in AST only: add to Sphinx graph (undocumented symbol)
    3. Node in Sphinx only: keep as-is
    4. UNRESOLVED reconciliation: retarget edges from unresolved → concrete
    5. All edges from both graphs kept (MultiDiGraph)
    """
    sg = sphinx_kg.nxgraph
    ag = ast_kg.nxgraph

    # Carry the AST layer's re-export alias map over so the post-merge
    # canonicalization pass (and query-time consumers of the exported
    # metadata) can chase public-path references to defining paths.
    ast_reexports = ast_kg.metadata.get("reexports") or {}
    if ast_reexports:
        sphinx_kg.metadata.setdefault("reexports", {}).update(ast_reexports)

    # Step 1 & 2: merge nodes
    for node_id, ast_attrs in ag.nodes(data=True):
        if node_id in sg:
            # Enrich existing Sphinx node with AST metadata.
            # ``in_test_file`` / ``is_test`` are facts about the FILE a
            # symbol was defined in, which only the AST side can know —
            # the Sphinx side has no opinion on them. Without copying
            # them, any test symbol that is also autodoc'd loses the
            # flag, and the rule that stops test helpers absorbing
            # production references silently stops applying to it.
            for key in ("file_path", "lineno", "end_lineno",
                        "in_test_file", "is_test"):
                if key in ast_attrs:
                    sg.nodes[node_id][key] = ast_attrs[key]
            sg.nodes[node_id]["source"] = "both"
            # Upgrade type when AST has a more concrete one. This
            # rescues Sphinx-side placeholders from ``extract_references``
            # / NetworkX auto-creation, which leave nodes as
            # ``unresolved`` even though the AST layer has them as
            # ``class`` / ``function`` / ``method``. Before this pass
            # the canonical ``py:class:pkg.mod.Thing`` could end up
            # with ``type=unresolved`` after merge, which broke
            # downstream type filters and the canonicalization leaf
            # index.
            ast_type = ast_attrs.get("type", "")
            if ast_type:
                sphinx_type = sg.nodes[node_id].get("type", "")
                ast_rank = TYPE_RANK.get(ast_type, 99)
                sphinx_rank = TYPE_RANK.get(sphinx_type, 99)
                if ast_rank < sphinx_rank:
                    sg.nodes[node_id]["type"] = ast_type
        else:
            # AST-only node — add it
            attrs = dict(ast_attrs)
            attrs["source"] = "ast_only"
            sg.add_node(node_id, **attrs)

    # Step 5: copy all AST edges
    for src, tgt, _key, data in ag.edges(keys=True, data=True):
        sg.add_edge(src, tgt, **data)

    # NOTE: _infer_implements is called separately after all merges,
    # not here — because merge_graphs may be called per-directory.

    # Step 7: tag confidence scores on all edges
    _tag_confidence(sg)

    return sphinx_kg



def drop_inline_math_references(graph: KnowledgeGraph) -> int:
    """Retire equation references that a ``:math:`` role never made.

    ``:eq:`X``` **references** a labelled equation. ``:math:`X``` **typesets**
    ``X`` as inline math and references nothing — ``X`` is LaTeX source, not a
    name. The docstring scanner nonetheless routes both into the equation
    namespace, because writing ``:math:`` where ``:eq:`` was meant is a common
    authoring slip and forgiving it is worth a phantom or two.

    It was not a phantom or two. The scanner's guard was a *blocklist* —
    reject a body containing ``\\``, ``{`` or ``}`` — while the Python branch
    three lines below it asks the opposite, stronger question
    (``_is_dotted_identifier``: is this a well-formed name?). Ordinary inline
    math clears a blocklist trivially: ``c > 1``, ``x = 0``, ``[0, 1]``,
    ``(L+C)`` contain none of those characters.

    ``[M]`` 2026-08-16, ORPHEUS: of 1860 ``math:equation:*`` nodes, **956 were
    unresolved** — the equation namespace was 51 % LaTeX fragments. 12 of the
    13 ids in the whole graph containing a NEWLINE were inline math wrapped
    across docstring lines.

    The forgiveness is kept, and its condition is made the honest one: mint
    the reference when ``X`` **is** a declared label. That is a question about
    the declared set, not about spelling, so no lexical rule can answer it and
    it cannot be answered per-file — hence a pass here, after every producer
    has contributed and after phantom canonicalization has had its chance to
    fold ``X`` onto a real label.

    ⚠ Only ``reftype == "math"`` edges are considered. An ``:eq:`` naming an
    undeclared label is a **dead reference** and must survive to be reported
    as one — that is ``dead_references``' entire job. Dropping those would
    turn a broken document into a clean one.

    Returns the number of edges dropped.
    """
    g = graph.nxgraph
    declared = NodeType.EQUATION.value

    doomed = [
        (src, tgt, key)
        for src, tgt, key, data in g.edges(keys=True, data=True)
        if data.get("reftype") == "math"
        and isinstance(tgt, str)
        and tgt.startswith("math:equation:")
        and g.nodes.get(tgt, {}).get("type") != declared
    ]
    for src, tgt, key in doomed:
        g.remove_edge(src, tgt, key=key)

    orphaned = 0
    for _, tgt, _ in doomed:
        if tgt in g and g.in_degree(tgt) == 0 and g.out_degree(tgt) == 0:
            g.remove_node(tgt)
            orphaned += 1

    if doomed:
        logger.info(
            "Dropped %d :math: references that name no declared label "
            "(%d nodes left with no referrer)",
            len(doomed), orphaned,
        )
    return len(doomed)


def reconcile_unresolved(graph: KnowledgeGraph) -> int:
    """Fold UNRESOLVED nodes onto the real definitions that share a name.

    Runs ONCE over the complete graph, on purpose. This used to live
    inside :func:`merge_graphs`, which is called once per source
    directory — so it judged "is this name ambiguous?" against a single
    slice of the project, and a name with rivals elsewhere looked
    unique inside it. ORPHEUS merges ``tests`` after the main tree, and
    a docstring's bare ``:mod:`derivations``` bound to the TEST package:
    unambiguous within the slice that decided it, wrong across the
    project.

    Widening the index to span both graphs fixed the observed case but
    left the pass order-sensitive by construction — a rival arriving in
    a LATER slice still could not retroactively make an earlier decision
    ambiguous. Deciding after every merge removes the failure mode
    rather than narrowing it.

    Names are indexed under both their full and their short form, every
    candidate competes under the shared ranking, and an ambiguous name
    is declined: this rewires edges, and silently reattributing a
    reference to the wrong symbol is worse than leaving it unresolved,
    where ``dead_references`` will surface it.

    Returns the number of nodes folded away.
    """
    g = graph.nxgraph

    by_name: dict[str, list[tuple[str, str]]] = {}
    for node_id, attrs in g.nodes(data=True):
        name = attrs.get("name", "")
        if not name or attrs.get("type") == NodeType.UNRESOLVED.value:
            continue
        by_name.setdefault(name.rsplit(".", 1)[-1], []).append((node_id, name))
        by_name.setdefault(name, []).append((node_id, name))

    def _best_match(name: str, phantom_id: str) -> str | None:
        candidates = by_name.get(name)
        if not candidates:
            return None
        candidates = [
            (cid, cname) for cid, cname in candidates
            if not test_node_is_off_limits(g, cid, phantom_id)
        ]
        if not candidates:
            return None
        ranked = sorted(
            candidate_rank(cid, cname, g.nodes[cid])
            for cid, cname in candidates
        )
        if candidates_are_ambiguous(ranked):
            return None
        return ranked[0][-1]

    folded: list[str] = []
    for node_id, attrs in list(g.nodes(data=True)):
        if attrs.get("type") != NodeType.UNRESOLVED.value:
            continue
        concrete_id = _best_match(attrs.get("name", ""), node_id)
        if not concrete_id or concrete_id == node_id or concrete_id not in g:
            continue
        for src, _, key, data in list(g.in_edges(node_id, keys=True, data=True)):
            g.add_edge(src, concrete_id, **data)
            g.remove_edge(src, node_id, key=key)
        for _, tgt, key, data in list(g.out_edges(node_id, keys=True, data=True)):
            g.add_edge(concrete_id, tgt, **data)
            g.remove_edge(node_id, tgt, key=key)
        folded.append(node_id)

    for node_id in folded:
        g.remove_node(node_id)

    if folded:
        logger.info(
            "Reconciled %d UNRESOLVED nodes with AST-found symbols",
            len(folded),
        )
    return len(folded)


def _tag_confidence(g: "nx.MultiDiGraph") -> None:
    """Tag confidence scores on ALL edges.

    - Sphinx-extracted (documents, references, contains, equation_ref, cites): 1.0
    - AST structural (calls, imports, inherits, type_uses): 1.0
    - Inferred (implements): 0.7 (already tagged in _infer_implements)
    """
    for _, _, data in g.edges(data=True):
        if "confidence" not in data:
            data["confidence"] = 1.0


def _infer_implements(
    g: "nx.MultiDiGraph", project_root: "Path | str | None" = None
) -> None:
    """Infer IMPLEMENTS edges from doc structure (conservative).

    Strategy: for each theory page, find its equations and the code
    symbols it DOCUMENTS. Only create an IMPLEMENTS edge when the code
    symbol's name shares tokens with the equation label — indicating
    a genuine implementation relationship rather than just appearing
    on the same page.

    Examples of matches:
      - sweep_spherical ↔ transport-spherical (share "spherical")
      - compute_pinf_group ↔ p-inf (share "p"/"inf" — too weak, skip)
      - solve_cp ↔ collision-rate (share no tokens — skip)
      - _compute_slab_rcp ↔ surface-to-region (share no tokens — skip)
      - _compute_slab_rcp ↔ rcp-from-double-antideriv (share "rcp" — match)

    What may be inferred is **declared, not hardcoded here**: the domain
    this walks and the attributes that disqualify a source both come from
    ``[edge.implements]`` in the ontology. Two copies of that used to live
    in this function — a literal ``{"function", "method", "class"}`` and a
    hand-written ``in_test_file`` test — which is how a rule ends up
    described in one place and enforced in another.

    An edge type the ontology does not declare is not inferred at all. No
    declaration is no licence; falling back to a literal would restore the
    second copy this exists to remove.
    """
    import re as _re

    from sphinxcontrib.nexus.ontology import Ontology

    # With no root this reads the BASE ontology alone, so a project's
    # `.nexus/ontology.toml` extension would be silently inert — the
    # extension mechanism having no reachable consumer is the same defect
    # as the module having none.
    ontology = Ontology.load(project_root)
    spec = ontology.edges.get("implements")
    if spec is None:
        logger.warning(
            "ontology declares no 'implements' edge — skipping inference"
        )
        return

    code_types = set(spec.domain)
    seen: set[tuple[str, str]] = set()
    count = 0
    refused = 0

    def _tokenize(name: str) -> set[str]:
        """Split a name into meaningful tokens (min length 3)."""
        tokens = _re.split(r"[-_.:]+", name.lower())
        return {t for t in tokens if len(t) >= 3}

    for doc_id, attrs in g.nodes(data=True):
        if attrs.get("type") != "file":
            continue

        eq_map: dict[str, set[str]] = {}
        code_map: dict[str, set[str]] = {}

        for _, tgt, data in g.out_edges(doc_id, data=True):
            tgt_attrs = g.nodes.get(tgt, {})
            tgt_type = tgt_attrs.get("type", "")
            tgt_name = tgt_attrs.get("name", "")
            edge_type = data.get("type", "")

            if tgt_type == "equation" and tgt not in eq_map:
                eq_map[tgt] = _tokenize(tgt_name)
            elif (
                tgt_type in code_types
                and edge_type == "documents"
                and tgt not in code_map
            ):
                code_map[tgt] = _tokenize(tgt_name)

        equations = list(eq_map.items())
        code_symbols = list(code_map.items())

        if not equations or not code_symbols:
            continue

        for code_id, code_tokens in code_symbols:
            for eq_id, eq_tokens in equations:
                # Require at least one shared token of length >= 3
                shared = code_tokens & eq_tokens
                if not shared:
                    continue
                pair = (code_id, eq_id)
                if pair in seen:
                    continue
                # Skip if any explicit TESTS or IMPLEMENTS edge already
                # links these two nodes. An edge is "explicit" when its
                # source is NOT the string "inferred" — covers
                # registry-sourced, directive-sourced, and
                # pytest.mark.verifies-sourced edges alike.
                existing = g.get_edge_data(code_id, eq_id, default={})
                if any(
                    d.get("type") in ("implements", "tests")
                    and d.get("source") != "inferred"
                    for d in existing.values()
                ):
                    seen.add(pair)
                    continue
                seen.add(pair)

                # The ontology is the admission authority, consulted at the
                # producer rather than validated after the fact — an edge
                # that should not exist must never be created, and a
                # warn-after pass would let it ship.
                #
                # This is what refuses a test class: a test VERIFIES an
                # equation, it does not implement one, and the graph
                # already models that with TESTS edges. Inferring
                # IMPLEMENTS onto a test inverts the relation the V&V
                # surface reads — an equation whose only implementer is a
                # test reads as implemented when nothing implements it,
                # a false ALIVE. Test classes are unusually prone to it
                # because the match is on shared name tokens:
                # ``TestSlabViaUnifiedDiscrepancyDiagnostic`` shares
                # ``slab``/``peierls``/``multigroup`` with half the
                # equations on its page.
                src_attrs = g.nodes[code_id]
                refusal = ontology.check_edge(
                    "implements",
                    src_attrs.get("type", ""),
                    g.nodes[eq_id].get("type", ""),
                    source_attrs=src_attrs,
                    source_id=code_id,
                    target_id=eq_id,
                )
                if refusal is not None:
                    refused += 1
                    continue

                g.add_edge(
                    code_id, eq_id,
                    type="implements", source="inferred",
                    confidence=spec.default_confidence,
                    shared_tokens=sorted(shared),
                )
                count += 1

    if count or refused:
        # Report refusals too. A filter that drops silently is
        # indistinguishable from one that never fires.
        logger.info(
            "Inferred %d IMPLEMENTS edges (code → equation); "
            "%d candidate(s) refused by the ontology",
            count, refused,
        )


def write_verifies_edges(g: "nx.MultiDiGraph") -> int:
    """Write ``EdgeType.TESTS`` edges from ``@pytest.mark.verifies`` metadata.

    Walks every function/method node with a ``verifies`` tuple in its
    metadata (populated by ``ast_analyzer._parse_pytest_markers``). For
    each label in that tuple, looks up the ``math:equation:<label>``
    node in the graph. When found, adds a ``tests`` edge with
    confidence 1.0 and ``source="pytest.mark.verifies"``. When the
    equation node does not exist, logs a warning and skips — we do
    not create phantom equation nodes here.

    Returns the number of edges written.
    """
    count = 0
    for node_id, attrs in list(g.nodes(data=True)):
        labels = attrs.get("verifies")
        if not labels:
            continue
        for label in labels:
            eq_id = f"math:equation:{label}"
            if eq_id not in g:
                logger.warning(
                    "pytest.mark.verifies(%r) on %s has no matching "
                    "equation node %s — skipping",
                    label, node_id, eq_id,
                )
                continue
            # Skip if ANY explicit TESTS edge already links this
            # (test, equation) pair — registry, directive, a prior
            # run of this pass, or any future explicit source. A
            # ``source="inferred"`` edge is weak and is allowed to
            # coexist with the marker-declared edge. This guard
            # makes the pipeline pass-order irrelevant: whichever
            # explicit source runs first wins, the later ones are
            # no-ops.
            existing = g.get_edge_data(node_id, eq_id, default={})
            if any(
                d.get("type") == EdgeType.TESTS.value
                and d.get("source") not in (None, "inferred")
                for d in existing.values()
            ):
                continue
            g.add_edge(
                node_id,
                eq_id,
                type=EdgeType.TESTS.value,
                source="pytest.mark.verifies",
                confidence=1.0,
            )
            count += 1
    if count:
        logger.info("Wrote %d TESTS edges from @pytest.mark.verifies", count)
    return count


def write_catches_edges(g: "nx.MultiDiGraph") -> int:
    """Write ``EdgeType.CATCHES`` edges from ``@pytest.mark.catches``.

    The exact mirror of :func:`write_verifies_edges`: one marker says
    which equation a test VERIFIES, this one which catalogued failure
    mode it CATCHES. Only the first had anywhere to land until
    ``NodeType.ERROR`` existed — `[M]` 2026-08-16 on ORPHEUS, 224 nodes
    carried a ``catches`` marker naming 78 distinct entries, and not one
    of the 78 was a node. The marker was a string pointing at nothing.

    Entries are declared by ``.. error-entry::``. A missing one warns and
    is skipped: we do not create phantom error nodes, for the same reason
    the equation side does not — a typo in a marker must not be able to
    invent the thing it claims to catch, or the miss reads as coverage.

    Returns the number of edges written.
    """
    # Whether the project has ADOPTED the catalogue at all. Warning once
    # per unresolved marker is right when a declaration is missing by
    # mistake, and pure noise when the project simply has no catalogue —
    # [M] on ORPHEUS that is 243 markers, on 224 nodes, naming 78 distinct
    # entries, so 243 lines a build. An un-adopted project therefore gets
    # ONE line that names what was looked for (an absence must still say
    # where it looked), and an adopting one gets the per-marker warning a
    # typo deserves. Same shape as the `enrich_proofs` pre-check in
    # `extract_references`.
    adopted = any(
        a.get("type") == NodeType.ERROR.value for _n, a in g.nodes(data=True)
    )
    unresolved: set[str] = set()

    count = 0
    for node_id, attrs in list(g.nodes(data=True)):
        entries = attrs.get("catches")
        if not entries:
            continue
        for entry_id in entries:
            err_id = f"vv:{NodeType.ERROR.value}:{entry_id}"
            if err_id not in g:
                unresolved.add(entry_id)
                if adopted:
                    logger.warning(
                        "pytest.mark.catches(%r) on %s has no matching error "
                        "node %s — declare it with `.. error-entry:: %s`",
                        entry_id, node_id, err_id, entry_id,
                    )
                continue
            existing = g.get_edge_data(node_id, err_id, default={})
            if any(
                d.get("type") == EdgeType.CATCHES.value
                and d.get("source") not in (None, "inferred")
                for d in existing.values()
            ):
                continue
            g.add_edge(
                node_id,
                err_id,
                type=EdgeType.CATCHES.value,
                source="pytest.mark.catches",
                confidence=1.0,
            )
            count += 1
    if count:
        logger.info("Wrote %d CATCHES edges from @pytest.mark.catches", count)
    if unresolved and not adopted:
        # WARNING, not info: a project carrying `catches` markers plainly
        # intends to use them, so "none of them resolves" is a finding,
        # not a note — and a stdlib `info` is invisible under Sphinx's
        # default level, which would make this the silent-absence bug it
        # exists to prevent.
        logger.warning(
            "%d @pytest.mark.catches marker(s) name %d catalogue entries "
            "that no `.. error-entry::` declares, so none of them is a "
            "graph node: %s%s. The catalogue is not in the corpus — until "
            "it is, `catches` cannot be queried.",
            sum(len(a.get("catches") or ()) for _n, a in g.nodes(data=True)),
            len(unresolved),
            ", ".join(sorted(unresolved)[:5]),
            ", …" if len(unresolved) > 5 else "",
        )
    return count
