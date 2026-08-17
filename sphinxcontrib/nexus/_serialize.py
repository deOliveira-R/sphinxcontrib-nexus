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
    d = {k: v for k, v in to_dict(node).items() if v}
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
    return d


#: Fallback when no query is at hand. The live set comes from the
#: ontology via `GraphQuery.placeholder_types`, so a project that
#: declares its own placeholder kind is ranked correctly too.
_PLACEHOLDER_TYPES = frozenset({"external", "unresolved"})


def _dedupe_parallel(entries: list[dict]) -> list[dict]:
    """Collapse repeats of one node into a single entry with a count.

    The graph is a MultiDiGraph, so three `isinstance(...)` calls in one
    function are three edges — correct, and three identical dicts in a
    reply. `[M]` on `context(solve_sn)`, `isinstance` appeared 3× and
    `AngularSourceSink.from_isotropic` 2× in a 16-entry bucket.

    The repetition is not noise in itself — "this function tests types
    three times" is a real signal — so it becomes ``times``, kept only
    when it is more than one.
    """
    seen: dict[str, dict] = {}
    for e in entries:
        first = seen.get(e["id"])
        if first is None:
            seen[e["id"]] = dict(e)
        else:
            first["times"] = first.get("times", 1) + 1
    return list(seen.values())


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

    Entries are compact node dicts: in this grouped view an edge dict
    would be pure redundancy (its type is the bucket key, its
    direction the outgoing/incoming key, and its endpoints the queried
    node and the entry itself). ``neighbors`` serves the flat
    node+edge view.
    """
    node = q.get_node(node_id)
    if node is None:
        return {"error": f"Node '{node_id}' not found"}

    neighbors = q.neighbors(node_id, direction="both")

    outgoing: dict[str, list[dict]] = {}
    incoming: dict[str, list[dict]] = {}
    for neighbor, edge in neighbors:
        buckets = outgoing if edge.source == node_id else incoming
        buckets.setdefault(edge.type, []).append(_compact_node(neighbor))

    placeholders = getattr(q, "placeholder_types", _PLACEHOLDER_TYPES)

    omitted: dict[str, dict[str, int]] = {}
    for direction_name, buckets in (("outgoing", outgoing), ("incoming", incoming)):
        for edge_type, entries in list(buckets.items()):
            entries = _dedupe_parallel(entries)
            # Project symbols first, placeholders last. A MEASURED
            # problem, not a preference: "what does solve_sn call?"
            # answered with `float`, `isinstance` ×3, `type`, `tuple`,
            # `getattr`, `TypeError` — [M] 8 of 16 entries were Python
            # builtins. They are real edges and stay, but they must not
            # be what a truncated answer keeps.
            #
            # .get: _compact_node strips a zero degree as absent, and
            # `type` survives only ON a placeholder, which is exactly
            # the flag this sorts by.
            entries.sort(key=lambda e: (
                e.get("type", "") in placeholders,
                -e.get("degree", 0),
                e["id"],
            ))
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
        target, direction=direction, max_depth=max_depth, edge_types=edge_types,
    )

    by_depth: dict[int, list[dict]] = {}
    omitted: dict[int, int] = {}
    for depth, nodes in result.by_depth.items():
        ordered = sorted(nodes, key=lambda n: (-n.degree, n.id))
        if per_depth_limit is not None and len(ordered) > per_depth_limit:
            omitted[depth] = len(ordered) - per_depth_limit
            ordered = ordered[:per_depth_limit]
        by_depth[depth] = [_compact_node(n) for n in ordered]

    payload = {
        "target": result.target,
        "direction": result.direction,
        "by_depth": by_depth,
        "total_affected": result.total_affected,
    }
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
    """Direct connections as list of {node, edge} dicts."""
    results = q.neighbors(node_id, direction=direction, edge_types=edge_types)
    return [
        {"node": to_dict(n), "edge": to_dict(e)}
        for n, e in results
    ]


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
