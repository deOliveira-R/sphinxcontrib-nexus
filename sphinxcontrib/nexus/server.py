"""MCP server for sphinxcontrib-nexus knowledge graph.

Exposes the full GraphQuery API as MCP tools, making the knowledge
graph queryable by Claude and other MCP clients.

Usage:
    nexus serve --db _nexus/graph.db
    # or via MCP config: command = "nexus", args = ["serve", "--db", "path/to/graph.db"]
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from sphinxcontrib.nexus.export import load_sqlite
from sphinxcontrib.nexus.graph import KnowledgeGraph
from sphinxcontrib.nexus.query import GraphQuery

logger = logging.getLogger(__name__)

_mcp = FastMCP("nexus", instructions=(
    "Knowledge graph server for code and documentation. "
    "Query relationships between functions, classes, equations, "
    "theory pages, and external dependencies."
))

# Module-level state set by serve()
_query: GraphQuery | None = None
_db_path: Path | None = None
_project_root: Path | None = None


def _get_query() -> GraphQuery:
    if _query is None:
        raise RuntimeError("Graph not loaded. Call serve() first.")
    return _query


def _to_dict(obj: Any) -> Any:
    """Convert dataclass results to JSON-safe dicts."""
    if hasattr(obj, "__dataclass_fields__"):
        d = asdict(obj)
        return d
    if isinstance(obj, list):
        return [_to_dict(x) for x in obj]
    if isinstance(obj, tuple):
        return [_to_dict(x) for x in obj]
    return obj


# ------------------------------------------------------------------
# MCP Tools
# ------------------------------------------------------------------


@_mcp.tool()
def query(text: str, node_types: str = "", limit: int = 20) -> str:
    """Search the knowledge graph by keyword.

    Searches node names and display names. Returns nodes sorted by
    degree (most connected first).

    Args:
        text: Search text (case-insensitive substring match).
        node_types: Comma-separated node types to filter (e.g., "function,class").
                    Empty string means all types.
        limit: Maximum number of results (default 20).
    """
    q = _get_query()
    types = [t.strip() for t in node_types.split(",") if t.strip()] or None
    results = q.query(text, node_types=types, limit=limit)
    return json.dumps(_to_dict(results), indent=2)


@_mcp.tool()
def context(node_id: str) -> str:
    """Get a 360-degree view of a node: its attributes and all connections.

    Shows the node's properties plus all incoming and outgoing edges
    grouped by type. This is the primary tool for understanding a symbol.

    Args:
        node_id: Node ID (e.g., "py:function:sn_solver.solve_sn").
    """
    q = _get_query()
    node = q.get_node(node_id)
    if node is None:
        return json.dumps({"error": f"Node '{node_id}' not found"})

    neighbors = q.neighbors(node_id, direction="both")

    # Group by edge type and direction
    outgoing: dict[str, list[dict]] = {}
    incoming: dict[str, list[dict]] = {}
    for neighbor, edge in neighbors:
        entry = {"node": _to_dict(neighbor), "edge": _to_dict(edge)}
        if edge.source == node_id:
            outgoing.setdefault(edge.type, []).append(entry)
        else:
            incoming.setdefault(edge.type, []).append(entry)

    return json.dumps({
        "node": _to_dict(node),
        "outgoing": outgoing,
        "incoming": incoming,
    }, indent=2)


@_mcp.tool()
def impact(
    target: str,
    direction: str = "upstream",
    max_depth: int = 3,
    edge_types: str = "",
) -> str:
    """Analyze blast radius: what depends on this symbol (upstream)
    or what this symbol depends on (downstream).

    Results are grouped by depth:
    - depth=1: WILL BREAK — direct callers/importers
    - depth=2: LIKELY AFFECTED — indirect dependents
    - depth=3: MAY NEED TESTING — transitive

    Args:
        target: Node ID of the symbol to analyze.
        direction: "upstream" (what depends on this) or "downstream" (what this depends on).
        max_depth: Maximum traversal depth (default 3).
        edge_types: Comma-separated edge types to follow (e.g., "calls,imports").
                    Empty means all edge types.
    """
    q = _get_query()
    types = [t.strip() for t in edge_types.split(",") if t.strip()] or None
    result = q.impact(target, direction=direction, max_depth=max_depth, edge_types=types)
    return json.dumps(_to_dict(result), indent=2)


@_mcp.tool()
def shortest_path(source: str, target: str, max_hops: int = 8) -> str:
    """Find the shortest path between two nodes.

    Useful for understanding how concepts connect:
    "How does theory/collision_probability relate to scipy.special.expn?"

    Args:
        source: Source node ID.
        target: Target node ID.
        max_hops: Maximum path length (default 8).
    """
    q = _get_query()
    result = q.shortest_path(source, target, max_hops=max_hops)
    if result is None:
        return json.dumps({"error": "No path found", "source": source, "target": target})
    return json.dumps(_to_dict(result), indent=2)


@_mcp.tool()
def neighbors(
    node_id: str,
    direction: str = "both",
    edge_types: str = "",
) -> str:
    """Get direct connections of a node.

    Args:
        node_id: Node ID to query.
        direction: "in" (incoming), "out" (outgoing), or "both".
        edge_types: Comma-separated edge types to filter (e.g., "calls,contains").
    """
    q = _get_query()
    types = [t.strip() for t in edge_types.split(",") if t.strip()] or None
    results = q.neighbors(node_id, direction=direction, edge_types=types)
    return json.dumps([
        {"node": _to_dict(n), "edge": _to_dict(e)}
        for n, e in results
    ], indent=2)


@_mcp.tool()
def god_nodes(top_n: int = 10) -> str:
    """Get the most connected nodes in the graph.

    These are the central concepts/symbols with the most relationships.

    Args:
        top_n: Number of nodes to return (default 10).
    """
    q = _get_query()
    results = q.god_nodes(top_n=top_n)
    return json.dumps(_to_dict(results), indent=2)


@_mcp.tool()
def stats() -> str:
    """Get graph-level statistics: node/edge counts by type, density, etc."""
    q = _get_query()
    return json.dumps(_to_dict(q.stats()), indent=2)


@_mcp.tool()
def communities(min_size: int = 3) -> str:
    """Detect functional communities (groups of tightly connected symbols).

    Uses greedy modularity optimization to find natural groupings.

    Args:
        min_size: Minimum community size to include (default 3).
    """
    q = _get_query()
    results = q.communities(min_size=min_size)
    # Return summary (full member lists can be huge)
    summaries = []
    for c in results:
        top_members = sorted(c.members, key=lambda n: n.degree, reverse=True)[:5]
        summaries.append({
            "id": c.id,
            "label": c.label,
            "size": c.size,
            "top_members": _to_dict(top_members),
        })
    return json.dumps(summaries, indent=2)


@_mcp.tool()
def detect_changes(scope: str = "all") -> str:
    """Detect which symbols changed in git and what they affect.

    Maps git changes to graph symbols and computes upstream impact.

    Args:
        scope: "staged", "unstaged", "all", or "branch" (diff vs main).
    """
    q = _get_query()
    if _project_root is None:
        return json.dumps({"error": "project_root not set"})
    result = q.detect_changes(_project_root, scope=scope)
    return json.dumps(_to_dict(result), indent=2)


@_mcp.tool()
def rename(old_name: str, new_name: str, dry_run: bool = True) -> str:
    """Analyze or execute a safe rename across the codebase.

    Finds all references via graph (high confidence) and regex (medium confidence).
    Set dry_run=False to apply the changes.

    Args:
        old_name: Current symbol name (e.g., "solve_sn").
        new_name: New name (e.g., "solve_discrete_ordinates").
        dry_run: If True, preview changes. If False, apply them.
    """
    q = _get_query()
    result = q.rename(
        old_name, new_name,
        project_root=_project_root,
        dry_run=dry_run,
    )
    return json.dumps(_to_dict(result), indent=2)


@_mcp.tool()
def provenance_chain(node_id: str) -> str:
    """Trace the full citation → equation → code chain for a symbol.

    Given a code function, shows which equations it implements and which
    literature citations those equations come from. The complete
    mathematical provenance.

    Args:
        node_id: Node ID of a code symbol or equation.
    """
    q = _get_query()
    return json.dumps(_to_dict(q.provenance_chain(node_id)), indent=2)


@_mcp.tool()
def verification_coverage(status_filter: str = "") -> str:
    """Map verification coverage: equation → code → test chains.

    Shows which equations are verified (have code + tests), which are
    implemented but untested, which are documented but unimplemented.

    Args:
        status_filter: Filter by status: "verified", "tested", "implemented",
                      "documented", "orphan_code". Empty = all.
    """
    q = _get_query()
    filt = status_filter if status_filter else None
    result = q.verification_coverage(status_filter=filt)
    # Return summary + first 20 entries (full list can be huge)
    return json.dumps({
        "summary": result.summary,
        "entries": _to_dict(result.entries[:20]),
        "total_entries": len(result.entries),
    }, indent=2)


@_mcp.tool()
def staleness() -> str:
    """Detect documentation pages that drifted from code.

    Compares git timestamps: if code was modified after its documentation
    page, the doc is flagged as stale. Only works with git and project_root set.
    """
    q = _get_query()
    result = q.staleness(_project_root)
    return json.dumps(_to_dict(result), indent=2)


@_mcp.tool()
def session_briefing() -> str:
    """Generate a structured briefing for starting a new session.

    Combines: graph stats, most connected nodes, stale docs,
    verification gaps, recent changes, and unresolved references.
    """
    q = _get_query()
    result = q.session_briefing(_project_root)
    return json.dumps(_to_dict(result), indent=2)


@_mcp.tool()
def retest(scope: str = "all") -> str:
    """Compute the minimum set of tests to re-run after changes.

    Uses git diff to find changed symbols, then traces upstream through
    the call graph to find all test functions that depend on them.

    Args:
        scope: "staged", "unstaged", "all", or "branch".
    """
    q = _get_query()
    if _project_root is None:
        return json.dumps({"error": "project_root not set"})
    result = q.retest(_project_root, scope=scope)
    return json.dumps(_to_dict(result), indent=2)


@_mcp.tool()
def trace_error(test_node_id: str) -> str:
    """Trace from a failing test back to the equations on its call path.

    Follows CALLS edges from the test function through the solver chain,
    collecting every equation and citation along the way. Helps diagnose
    which equation might be wrong when a test fails.

    Args:
        test_node_id: Node ID of the failing test function.
    """
    q = _get_query()
    result = q.trace_error(test_node_id)
    return json.dumps(_to_dict(result), indent=2)


@_mcp.tool()
def migration_plan(from_dep: str, to_dep: str = "") -> str:
    """Plan a dependency migration (e.g., numpy → jax).

    Finds all functions that use the dependency, groups them into phases
    by blast radius (leaf first, core last), and identifies documentation
    pages that need updating.

    Args:
        from_dep: Package to migrate from (e.g., "numpy", "scipy.special").
        to_dep: Package to migrate to (e.g., "jax.numpy"). Optional.
    """
    q = _get_query()
    result = q.migration_plan(from_dep, to_dep)
    return json.dumps(_to_dict(result), indent=2)


@_mcp.tool()
def processes(min_length: int = 3) -> str:
    """Detect execution flows: maximal call chains from entry points.

    Returns named sequences showing how functions call each other
    from entry point to leaf. Useful for understanding how code executes.

    Args:
        min_length: Minimum chain length to include (default 3).
    """
    q = _get_query()
    results = q.processes(min_length=min_length)
    summaries = []
    for p in results[:20]:
        summaries.append({
            "name": p.name,
            "length": p.length,
            "steps": [
                {"step": s.step_number, "node": s.node.id, "calls_next": s.calls_next}
                for s in p.steps
            ],
        })
    return json.dumps(summaries, indent=2)


@_mcp.tool()
def bridges(top_n: int = 10) -> str:
    """Find bridge nodes connecting separate communities.

    These are architectural hotspots with high betweenness centrality.
    Changing them has outsized impact across the codebase.

    Args:
        top_n: Number of bridges to return (default 10).
    """
    q = _get_query()
    results = q.bridges(top_n=top_n)
    return json.dumps(_to_dict(results), indent=2)


# ------------------------------------------------------------------
# MCP Resources
# ------------------------------------------------------------------


@_mcp.resource("nexus://graph/stats")
def resource_stats() -> str:
    """Graph overview: node/edge counts, types, density."""
    q = _get_query()
    return json.dumps(_to_dict(q.stats()), indent=2)


@_mcp.resource("nexus://graph/communities")
def resource_communities() -> str:
    """All detected functional communities."""
    q = _get_query()
    results = q.communities(min_size=2)
    summaries = []
    for c in results:
        member_names = [m.name for m in sorted(c.members, key=lambda n: n.degree, reverse=True)[:10]]
        summaries.append({"id": c.id, "label": c.label, "size": c.size, "top_members": member_names})
    return json.dumps(summaries, indent=2)


@_mcp.resource("nexus://briefing")
def resource_briefing() -> str:
    """Session briefing: what you need to know right now."""
    q = _get_query()
    result = q.session_briefing(_project_root)
    return json.dumps(_to_dict(result), indent=2)


@_mcp.resource("nexus://graph/schema")
def resource_schema() -> str:
    """Graph schema: available node types and edge types."""
    from sphinxcontrib.nexus.graph import EdgeType, NodeType
    return json.dumps({
        "node_types": [t.value for t in NodeType],
        "edge_types": [t.value for t in EdgeType],
        "node_id_format": "<domain>:<type>:<qualified_name>",
        "examples": {
            "function": "py:function:sn_solver.solve_sn",
            "class": "py:class:collision_probability.CPMesh",
            "equation": "math:equation:diffusion-eq",
            "document": "doc:theory/discrete_ordinates",
            "external": "py:class:numpy.ndarray",
        },
    }, indent=2)


# ------------------------------------------------------------------
# Server entry point
# ------------------------------------------------------------------


def serve(
    db_path: Path,
    project_root: Path | None = None,
) -> None:
    """Load the graph and start the MCP server."""
    global _query, _db_path, _project_root

    _db_path = db_path
    _project_root = project_root

    kg = load_sqlite(db_path)
    _query = GraphQuery(kg)

    logger.info(
        "Loaded graph: %d nodes, %d edges from %s",
        kg.node_count, kg.edge_count, db_path,
    )

    _mcp.run()
