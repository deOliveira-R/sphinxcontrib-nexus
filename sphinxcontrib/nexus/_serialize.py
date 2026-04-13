"""Shared serialization logic for CLI and MCP server.

Both pathways call these functions so their JSON output is
identical by construction.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from sphinxcontrib.nexus.query import GraphQuery


def to_dict(obj: Any) -> Any:
    """Convert dataclass results to JSON-safe dicts."""
    if hasattr(obj, "__dataclass_fields__"):
        return asdict(obj)
    if isinstance(obj, (list, tuple)):
        return [to_dict(x) for x in obj]
    return obj


def to_json(data: Any) -> str:
    """Serialize to indented JSON string."""
    return json.dumps(data, indent=2)


# ------------------------------------------------------------------
# Assembly functions — one per MCP tool with non-trivial assembly
# ------------------------------------------------------------------


def assemble_context(q: GraphQuery, node_id: str) -> dict:
    """360-degree view: node + edges grouped by type and direction."""
    node = q.get_node(node_id)
    if node is None:
        return {"error": f"Node '{node_id}' not found"}

    neighbors = q.neighbors(node_id, direction="both")

    outgoing: dict[str, list[dict]] = {}
    incoming: dict[str, list[dict]] = {}
    for neighbor, edge in neighbors:
        entry = {"node": to_dict(neighbor), "edge": to_dict(edge)}
        if edge.source == node_id:
            outgoing.setdefault(edge.type, []).append(entry)
        else:
            incoming.setdefault(edge.type, []).append(entry)

    return {
        "node": to_dict(node),
        "outgoing": outgoing,
        "incoming": incoming,
    }


def assemble_neighbors(
    q: GraphQuery,
    node_id: str,
    direction: str = "both",
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
