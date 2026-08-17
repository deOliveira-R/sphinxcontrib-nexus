"""Shared serialization logic for CLI and MCP server.

Both pathways call these functions so their JSON output is
identical by construction.
"""

from __future__ import annotations

import json
from dataclasses import asdict, fields
from typing import Any, Literal

from sphinxcontrib.nexus.query import GraphQuery


def to_dict(obj: Any) -> Any:
    """Convert dataclass results to JSON-safe dicts.

    Every :class:`~sphinxcontrib.nexus.query.NodeResult` goes through
    :func:`_compact_node` on the way out — ONE choke point, because a
    node appears in nearly every reply and only two of the assemblers
    used to compact it. Everything else serializes whole.
    """
    if hasattr(obj, "__dataclass_fields__"):
        if type(obj).__name__ == "NodeResult":
            return _compact_node(asdict(obj))
        # Walk the FIELDS, not `asdict(obj)`. `asdict` recurses and
        # flattens every nested dataclass to a plain dict first, so a
        # NodeResult inside a StalenessEntry arrives here already
        # anonymous and escapes compaction — which is exactly what
        # happened: the tools compacted, the briefing did not, and it
        # is the briefing that loads every session.
        #
        # `None` fields are dropped: `"equation": null` states the same
        # thing as saying nothing, in 18 characters. Empty LISTS stay —
        # `"tests": []` on a coverage entry is the finding, not padding.
        return {
            f.name: to_dict(v)
            for f in fields(obj)
            if (v := getattr(obj, f.name)) is not None
        }
    if isinstance(obj, (list, tuple)):
        return [to_dict(x) for x in obj]
    if isinstance(obj, dict):
        return {k: to_dict(v) for k, v in obj.items()}
    return obj


def to_json(data: Any) -> str:
    """Serialize to indented JSON string."""
    return json.dumps(data, indent=2)


def _compact_node(node: Any) -> dict:
    """A node dict where every field says something the id does not.

    A node result is the most repeated structure in the whole tool
    surface, so its redundancy multiplies through everything. The full
    shape has nine fields and four of them say nothing:

    .. code-block:: json

        {"id": "py:function:orpheus.sn.solver.solve_sn",
         "type": "function",                            ← id segment 2
         "name": "orpheus.sn.solver.solve_sn",          ← id segment 3
         "display_name": "orpheus.sn.solver.solve_sn",  ← == name
         "domain": "py",                                ← id segment 1
         "docname": "",                                 ← empty
         "degree": 374, "file_path": "…", "lineno": 2337}

    Since the strict grammar landed the id IS ``<domain>:<type>:<name>``
    — `[M]` 2026-08-16, ``name`` and ``domain`` reproduce their segments
    on **22848 of 22848** nodes. So they go.

    ``type`` is kept, and that is not inconsistency: it matches its
    segment on only **80%**, and the 20% where it differs is exactly the
    placeholder case the grammar allows — the id says what the name
    DENOTES, ``type`` says nothing was found under it. There it is the
    most informative field in the dict.

    ``display_name`` survives only when it differs from the name, which
    is the case worth reading (``n_points`` for a method) rather than
    the case that repeats it.

    Consumers must ``.get()`` optional fields — which they already had
    to, since ``""``/``0`` were sentinel non-values anyway.
    """
    # Emptiness is dropped at EVERY level, not just the top one: a
    # nested block (``test``) states the same thing about its own
    # absent fields that the record states about its own, so it obeys
    # the same rule rather than a special case for one key.
    d = {}
    for key, value in to_dict(node).items():
        if isinstance(value, dict):
            value = {k: v for k, v in value.items() if v}
        if value:
            d[key] = value
    parts = d.get("id", "").split(":", 2)
    if len(parts) == 3:
        domain, type_segment, name = parts
        if d.get("name") == name:
            d.pop("name", None)
        if d.get("domain") == domain:
            d.pop("domain", None)
        if d.get("type") == type_segment:
            d.pop("type", None)
        if d.get("display_name") in (name, name.rsplit(".", 1)[-1]):
            d.pop("display_name", None)
        # A doc node's `docname` IS its name segment
        # (`std:file:api/geometry` → `api/geometry`). On an equation or
        # a section it is the PAGE that contains it, which the id does
        # not say — so this drops the copy and keeps the container.
        if d.get("docname") == name:
            d.pop("docname", None)
    return d


#: Provenance values that mean "somebody DECLARED this" — a marker, a
#: directive, a registry entry. They are the default and cost no bytes;
#: only a guess is worth marking, and marking the guess is the whole
#: point (#74).
_INFERRED = "inferred"


def _mark_evidence(entry: dict, edge: Any) -> dict:
    """Flag an entry whose edge is a GUESS, and say what produced it.

    Declared is the silent default: an edge minted from a
    ``@pytest.mark.verifies`` or a directive needs no annotation, and
    annotating it would cost bytes on every reply to say "normal".

    An inferred edge is different in kind, not degree — it exists
    because two names share a word. `[M]` on ORPHEUS **12999 of 13084**
    ``implements`` edges are inferred (2026-08-17; 14004 of 14004 before
    the first declarations landed), so a reader who assumes the default
    is wrong almost every time; ``via`` is what lets them see it
    (``ScatteringOperator.kernel`` matched to a UBLD kernel equation on
    the token ``kernel``).

    ``via`` is a tuple rather than a list for no reason a reader needs
    to know: :func:`_dedupe_parallel` no longer requires it (see
    :func:`_content_key`), and JSON renders either as an array.
    """
    if getattr(edge, "evidence", "") != _INFERRED:
        return entry
    entry["inferred"] = True
    if getattr(edge, "via", None):
        entry["via"] = tuple(edge.via)
    return entry


#: Fields that describe a node rather than the ADJACENCY, dropped from
#: the flat `neighbors` view only. Both are answers to "tell me about
#: this symbol" — the question `context`, `node_at` and `impact` exist
#: for — and in a flat dump they are paid on every neighbour to serve
#: the one or two you will actually open. Position measured `[M]` at
#: 26% of the payload (72 B of 278 per neighbour on `solve_sn`); the
#: `test` block is the same trade, one node kind later.
_DOSSIER_FIELDS = frozenset({"file_path", "lineno", "test"})


#: Fallback when no query is at hand. The live set comes from the
#: ontology via `GraphQuery.placeholder_types`, so a project that
#: declares its own placeholder kind is ranked correctly too.
_PLACEHOLDER_TYPES = frozenset({"external", "unresolved"})


def _content_key(value: Any) -> Any:
    """A hashable projection of any JSON-shaped value.

    :func:`_dedupe_parallel` identifies an entry by its CONTENT, which
    means hashing it — and an entry is a JSON object, so its values may
    legitimately be lists and nested objects, neither of which hashes.

    The invariant "every value an assembler puts in an entry must be
    hashable" is unenforceable and has now failed twice, both times as
    a `TypeError` raised from the reply path rather than from the code
    that broke it: first ``via`` (a list of tokens, fixed by spelling
    it as a tuple at that one site), then the ``test`` block (a nested
    object, which no per-field spelling can fix). Two instances is the
    signal to fix the mechanism instead of the instance — after this,
    an assembler may put any JSON value in an entry.
    """
    if isinstance(value, dict):
        return tuple(sorted((k, _content_key(v)) for k, v in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_content_key(v) for v in value)
    return value


def _dedupe_parallel(entries: list[dict]) -> list[dict]:
    """Collapse repeats of one node into a single entry with a count.

    The graph is a MultiDiGraph, so three `isinstance(...)` calls in one
    function are three edges — correct, and three identical dicts in a
    reply. `[M]` on `context(solve_sn)`, `isinstance` appeared 3× and
    `AngularSourceSink.from_isotropic` 2× in a 16-entry bucket.

    The repetition is not noise in itself — "this function tests types
    three times" is a real signal — so it becomes ``times``, kept only
    when it is more than one.

    Two entries are the same edge repeated exactly when they are the
    same dict — so the ENTRY is its own identity key, and this needs no
    caller to tell it what identifies one. That matters because the two
    callers disagree about it: in ``context`` the bucket has already
    fixed the edge type and direction, so ``id`` alone would do, while
    in ``neighbors`` the entry carries both and a node reached by
    ``calls`` AND ``type_uses`` is genuinely two facts. Keying on the
    content is correct for both without a flag deciding which.
    """
    seen: dict[Any, dict] = {}
    for e in entries:
        key = _content_key(e)
        first = seen.get(key)
        if first is None:
            seen[key] = dict(e)
        else:
            first["times"] = first.get("times", 1) + 1
    return list(seen.values())


def _test_material(q: GraphQuery, node_id: str, ids: set[str]) -> frozenset[str]:
    """Of ``ids``, those living in the test tree — unless the ASKER does.

    "Who depends on me?" has two audiences and they need opposite
    answers. From production code the actionable dependent is the
    production one and the tests are the safety net; from a test node,
    test material IS the subject and demoting it would bury the answer.
    So this is relative to the queried node, not absolute.

    `[M]` 2026-08-16 on ORPHEUS, incoming ``calls``: `for_mesh` 17 of
    18 are tests, `solve_sn` 22 of 25, `LinearDiscontinuous` 18 of 25 —
    and the ratio swings from 1 production caller to 7, so it cannot be
    guessed. Unranked, `for_mesh`'s single production caller sat at
    rank **27 of 44**, below any truncation, while the top slots went
    to `SNMesh` (degree 1633, adjacent to everything).

    ⚠ ``in_test_file``, NOT ``is_test``. `[M]` by ``is_test``,
    `solve_sn` reports 3 production callers and `LinearDiscontinuous`
    7 — every one of them a *helper* defined in a test file
    (`_ld_mesh`, `_sn_composite_triple`). Both true counts are 0. The
    wrong flag overstates "what breaks" by 3× and 7×.
    """
    g = getattr(q, "_g", None)
    if g is None:
        return frozenset()
    if g.nodes.get(node_id, {}).get("in_test_file"):
        return frozenset()
    return frozenset(
        i for i in ids if g.nodes.get(i, {}).get("in_test_file")
    )


def _rank_entries(
    entries: list[dict],
    placeholders: frozenset[str],
    demote: frozenset[str] = frozenset(),
) -> None:
    """Order entries so a TRUNCATED answer keeps the useful half.

    Project symbols first, placeholders last, most-connected first
    within each. A MEASURED problem, not a preference: "what does
    solve_sn call?" answered with `float`, `isinstance` ×3, `type`,
    `tuple`, `getattr`, `TypeError` — `[M]` 8 of 16 entries were Python
    builtins, and they outrank project symbols on raw degree. They are
    real edges and stay; they must not be what a truncated answer keeps.

    ``demote`` (see :func:`_test_material`) sinks test-tree entries the
    same way and for the same reason: they are real and they are not
    what you act on first.

    Sorts in place. ``.get`` throughout because :func:`_compact_node`
    strips a zero degree as absent, and ``type`` survives only ON a
    placeholder — which is exactly the flag this sorts by.
    """
    entries.sort(key=lambda e: (
        e.get("type", "") in placeholders,
        e["id"] in demote,
        -e.get("degree", 0),
        e["id"],
    ))


# ------------------------------------------------------------------
# Assembly functions — one per MCP tool with non-trivial assembly
# ------------------------------------------------------------------


def assemble_context(
    q: GraphQuery,
    node_id: str,
    per_type_limit: int | None = 25,
) -> dict:
    """360-degree view: node + edges grouped by type and direction.

    Each edge-type bucket is sorted most-connected-first (the same
    ordering convention as ``query``) and capped at ``per_type_limit``
    entries; ``None`` means uncapped. When anything is dropped, an
    ``omitted`` block reports truthful per-bucket drop counts and a
    ``hint`` names the escape hatches — the truncation is always
    visible, never silent. The cap exists because a hub node's full
    context serializes to megabytes of JSON, far beyond what a tool
    consumer can usefully read.

    Entries are compact node dicts: in this grouped view the edge's
    type is the bucket key and its direction the outgoing/incoming key,
    so nothing about it needs restating per entry. ``neighbors`` serves
    the same relations as one flat, ranked list, naming each entry's
    edge type and direction on the entry itself.
    """
    node = q.get_node(node_id)
    if node is None:
        return {"error": f"Node '{node_id}' not found"}

    neighbors = q.neighbors(node_id, direction="both")

    outgoing: dict[str, list[dict]] = {}
    incoming: dict[str, list[dict]] = {}
    for neighbor, edge in neighbors:
        buckets = outgoing if edge.source == node_id else incoming
        buckets.setdefault(edge.type, []).append(
            _mark_evidence(_compact_node(neighbor), edge)
        )

    placeholders = getattr(q, "placeholder_types", _PLACEHOLDER_TYPES)
    demote = _test_material(q, node_id, {n.id for n, _ in neighbors})

    omitted: dict[str, dict[str, int]] = {}
    for direction_name, buckets in (("outgoing", outgoing), ("incoming", incoming)):
        for edge_type, entries in list(buckets.items()):
            entries = _dedupe_parallel(entries)
            _rank_entries(entries, placeholders, demote)
            buckets[edge_type] = entries
            if per_type_limit is not None and len(entries) > per_type_limit:
                omitted.setdefault(direction_name, {})[edge_type] = (
                    len(entries) - per_type_limit
                )
                buckets[edge_type] = entries[:per_type_limit]

    result = {
        "node": to_dict(node),
        "outgoing": outgoing,
        "incoming": incoming,
    }
    if omitted:
        result["omitted"] = omitted
        result["hint"] = (
            "Buckets are sorted most-connected-first and capped at "
            f"{per_type_limit} per edge type. For a complete single-type "
            "list use neighbors(node_id, edge_types=...), or raise the "
            "limit (0 = uncapped)."
        )
    return result


def assemble_impact(
    q: GraphQuery,
    target: str,
    direction: Literal["upstream", "downstream"] = "upstream",
    max_depth: int = 3,
    edge_types: list[str] | None = None,
    per_depth_limit: int | None = 50,
    only: Literal["tests", "code"] | None = None,
) -> dict:
    """Blast radius with per-depth budgets.

    Each ``by_depth`` bucket is sorted most-connected-first and capped
    at ``per_depth_limit`` nodes; ``None`` means uncapped (same keys
    as serializing the raw ``ImpactResult``; entries are compact node
    dicts with empty fields omitted). ``total_affected`` is ALWAYS the
    true traversal count, so a capped response still answers "how big
    is the blast radius" exactly; ``omitted`` reports per-depth drop
    counts when anything was dropped. Depth-3 impact on a hub node
    serializes to megabytes uncapped — the budget keeps the answer
    readable.
    """
    result = q.impact(
        target, direction=direction, max_depth=max_depth,
        edge_types=edge_types, only=only,
    )

    by_depth: dict[int, list[dict]] = {}
    omitted: dict[int, int] = {}
    for depth, nodes in result.by_depth.items():
        ordered = sorted(nodes, key=lambda n: (-n.degree, n.id))
        if per_depth_limit is not None and len(ordered) > per_depth_limit:
            omitted[depth] = len(ordered) - per_depth_limit
            ordered = ordered[:per_depth_limit]
        by_depth[depth] = [_compact_node(n) for n in ordered]

    payload: dict[str, Any] = {
        "target": result.target,
        "direction": result.direction,
        "by_depth": by_depth,
        "total_affected": result.total_affected,
    }
    if result.only is not None:
        payload["only"] = result.only
        payload["total_in_role"] = result.total_in_role
    if omitted:
        payload["omitted"] = omitted
        payload["hint"] = (
            "Depth buckets are sorted most-connected-first and capped at "
            f"{per_depth_limit} per depth; total_affected is the true "
            "count. Raise the limit (0 = uncapped) for the full set."
        )
    return payload


def assemble_neighbors(
    q: GraphQuery,
    node_id: str,
    direction: Literal["in", "out", "both"] = "both",
    edge_types: list[str] | None = None,
) -> list[dict]:
    """Direct connections, one flat entry per neighbour.

    A neighbour is three facts — WHICH node, by WHAT relation, in WHICH
    direction — and an entry carries only the ones this query did not
    already fix. Ask ``direction="out"`` and every entry would say
    ``"out"``; ask ``edge_types=["calls"]`` and every entry would say
    ``"calls"``. A field the caller pinned in the question is not an
    answer, so it is omitted rather than repeated N times.

    `[M]` 2026-08-16 on ORPHEUS, ``solve_sn``'s 374 neighbours: the
    reply was **479 B per neighbour**, of which the edge dict was 46 %
    and carried one bit of information::

        "edge": {"source": "…solve_sn",     ← the node you asked about
                 "target": "py:class:dict", ← the id on the line above
                 "type": "type_uses",       ← the only content here
                 "key": "0"}                ← a MultiDiGraph internal

    ``source``/``target`` existed only because DIRECTION was otherwise
    unrecoverable — naming the direction is what lets both endpoints
    go. ``key`` distinguishes parallel edges, which
    :func:`_dedupe_parallel` now folds into ``times``.

    Entries carry no ``file_path``/``lineno``: adjacency is not
    location, and `[M]` position was 26 % of this reply — paid on every
    neighbour to serve the one or two you go on to open. Ask ``context``
    or ``node_at`` about that one.

    Entries are ranked (:func:`_rank_entries`) because the boundary
    budget truncates: on a hub node the ordering decides what survives,
    so an unranked list would drop an arbitrary tail.
    """
    results = q.neighbors(node_id, direction=direction, edge_types=edge_types)

    show_direction = direction == "both"
    show_edge_type = not (edge_types and len(set(edge_types)) == 1)

    entries = []
    for neighbor, edge in results:
        entry = {
            k: v for k, v in _compact_node(neighbor).items()
            if k not in _DOSSIER_FIELDS
        }
        if show_edge_type:
            entry["edge_type"] = edge.type
        if show_direction:
            # A self-loop reports "out"; it is both, and the pair is one
            # edge, so one of the two names has to win.
            entry["direction"] = "out" if edge.source == node_id else "in"
        entries.append(_mark_evidence(entry, edge))

    entries = _dedupe_parallel(entries)
    _rank_entries(
        entries,
        getattr(q, "placeholder_types", _PLACEHOLDER_TYPES),
        _test_material(q, node_id, {e["id"] for e in entries}),
    )
    return entries


def assemble_communities(q: GraphQuery, min_size: int = 3) -> list[dict]:
    """Communities with top-5 members summary."""
    results = q.communities(min_size=min_size)
    summaries = []
    for c in results:
        top_members = sorted(c.members, key=lambda n: n.degree, reverse=True)[:5]
        summaries.append({
            "id": c.id,
            "label": c.label,
            "size": c.size,
            "top_members": to_dict(top_members),
        })
    return summaries


def _slice(items: list, limit: int | None, offset: int) -> list:
    """Apply optional offset/limit to a list; ``limit=None`` returns all."""
    if offset < 0:
        offset = 0
    if limit is None:
        return items[offset:]
    return items[offset : offset + max(limit, 0)]


def assemble_processes(
    q: GraphQuery,
    min_length: int = 3,
    limit: int | None = None,
    offset: int = 0,
) -> dict:
    """Execution flows.

    Returns a dict with ``total``, ``offset``, ``limit`` metadata and a
    ``processes`` list. ``limit=None`` (the default) returns every
    process — callers opt in to pagination explicitly.
    """
    results = q.processes(min_length=min_length)
    window = _slice(results, limit, offset)
    summaries = [
        {
            "name": p.name,
            "length": p.length,
            "steps": [
                {"step": s.step_number, "node": s.node.id, "calls_next": s.calls_next}
                for s in p.steps
            ],
        }
        for p in window
    ]
    return {
        "processes": summaries,
        "total": len(results),
        "offset": offset,
        "limit": limit,
        "returned": len(summaries),
    }


def assemble_verification_coverage(
    q: GraphQuery,
    status_filter: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> dict:
    """Coverage summary plus the entries slice.

    ``limit=None`` (the default) returns every entry; pass an integer
    to opt in to pagination. ``total_entries`` is always the
    unfiltered count so clients can detect truncation.
    """
    result = q.verification_coverage(status_filter=status_filter)
    window = _slice(result.entries, limit, offset)
    return {
        "summary": result.summary,
        "entries": to_dict(window),
        "total_entries": len(result.entries),
        "offset": offset,
        "limit": limit,
        "returned": len(window),
    }


def assemble_shortest_path(
    q: GraphQuery,
    source: str,
    target: str,
    max_hops: int = 8,
) -> dict:
    """Shortest path or error dict."""
    result = q.shortest_path(source, target, max_hops=max_hops)
    if result is None:
        return {"error": "No path found", "source": source, "target": target}
    return to_dict(result)
