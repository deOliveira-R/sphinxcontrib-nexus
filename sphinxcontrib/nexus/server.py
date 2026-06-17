"""MCP server for sphinxcontrib-nexus knowledge graph.

Exposes the full GraphQuery API as MCP tools, making the knowledge
graph queryable by Claude and other MCP clients.

Usage:
    nexus serve --db _nexus/graph.db
    # or via MCP config: command = "nexus", args = ["serve", "--db", "path/to/graph.db"]
"""

from __future__ import annotations

import functools
import inspect
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from mcp.server.fastmcp import Context, FastMCP

from sphinxcontrib.nexus._serialize import (
    assemble_communities,
    assemble_context,
    assemble_impact,
    assemble_neighbors,
    assemble_processes,
    assemble_shortest_path,
    assemble_verification_coverage,
    to_dict,
    to_json,
)
from sphinxcontrib.nexus.export import load_sqlite
from sphinxcontrib.nexus.query import GraphQuery
from sphinxcontrib.nexus.workspace import (
    PROVENANCE_KEY,
    GitProvenance,
    Workspace,
    WorkspaceLayoutError,
    WorkspaceResolutionError,
    changed_files,
    checkout_containing,
    discover,
    resolve_checkout_root,
)

logger = logging.getLogger(__name__)

_mcp = FastMCP("nexus", instructions=(
    "Knowledge graph server for code and documentation. "
    "Query relationships between functions, classes, equations, "
    "theory pages, and external dependencies."
))

# Module-level state set by serve(). One server process serves one
# agent session, so the active workspace is process-local state:
# switching it (``use_workspace``) cannot leak across sessions.
_query: GraphQuery | None = None
_workspace: Workspace | None = None
_db_mtime: float = 0.0

# Reload coordination: FastMCP may dispatch tool calls concurrently,
# and the mid-reload state (new ``_query`` assigned but ``_db_mtime``
# not yet updated) is short but real. The lock serializes the
# reload path so a second concurrent caller sees the finalized
# swap, not a torn read.
_reload_lock = threading.Lock()


def _reload_if_stale() -> None:
    """Re-read the graph DB if it was modified since last load.

    Failure-tolerant: if the DB has vanished, become read-errored,
    or is mid-write at the moment we try to load it (SQLite
    corruption, schema-version rejection, disk flake), keep the
    previous in-memory snapshot serving rather than dropping
    ``_query`` on the floor. Warnings are logged at WARNING level
    so operators can see something went wrong without the MCP
    tool calls crashing.

    Thread-safe: a module-level lock serializes the mtime check
    and the atomic ``_query`` / ``_db_mtime`` swap so concurrent
    FastMCP tool dispatches can't observe a half-updated state.
    """
    global _query, _db_mtime
    if _workspace is None:
        return
    db_path = _workspace.db_path
    try:
        current_mtime = db_path.stat().st_mtime
    except OSError as e:
        # DB file missing or inaccessible — keep serving the
        # previous snapshot. No reload means no change; no warning
        # on every tool call, just on the reload we couldn't do.
        logger.debug("Stat failed for %s: %s — skipping reload", db_path, e)
        return
    if current_mtime <= _db_mtime:
        return

    with _reload_lock:
        # Re-check under the lock: another thread may have already
        # reloaded to the same mtime, or switched the active
        # workspace entirely (use_workspace) — in which case this
        # reload's pre-lock stat refers to the WRONG database and
        # must not clobber the switched-in graph.
        if _workspace is None or _workspace.db_path != db_path:
            return
        if current_mtime <= _db_mtime:
            return
        try:
            kg = load_sqlite(db_path)
            new_query = GraphQuery(kg)
        except Exception as e:
            logger.warning(
                "Nexus reload failed, keeping previous snapshot "
                "(db=%s, mtime=%s): %s",
                db_path, current_mtime, e,
            )
            return
        _query = new_query
        _db_mtime = current_mtime
        logger.info(
            "Reloaded graph: %d nodes, %d edges (db changed on disk)",
            kg.node_count, kg.edge_count,
        )


def _get_query() -> GraphQuery:
    if _query is None:
        raise RuntimeError("Graph not loaded. Call serve() first.")
    _reload_if_stale()
    return _query


def _active_root() -> Path | None:
    """Project root of the active workspace (``None`` when serving a
    bare database with no known root)."""
    return _workspace.root if _workspace is not None else None


# Files changed since the active graph's build commit, cached per
# (root, db_mtime, commit): the set can only change when the graph is
# rebuilt, the active workspace switches, or the checkout moves to a
# different stamped commit — one git subprocess per reload, not per
# tool call. ``None`` set value means the diff itself failed
# (unknown ≠ unchanged).
_changed_cache: tuple[tuple[Path, float, str], frozenset[Path] | None] | None = None


def _build_commit() -> str | None:
    """Commit the active graph's provenance stamp records, ``None``
    when the graph is unloaded, unstamped, or built from a non-git
    tree."""
    if _query is None:
        return None
    prov = GitProvenance.from_stamp(
        _query.knowledge_graph.metadata.get(PROVENANCE_KEY)
    )
    return prov.commit if prov is not None else None


def _files_changed_since_build() -> frozenset[Path] | None:
    """Working-tree files that differ from the active graph's stamped
    commit. ``None`` when unknowable: no root, no provenance stamp,
    or git failure."""
    global _changed_cache
    root = _active_root()
    commit = _build_commit()
    if root is None or commit is None:
        return None
    key = (root, _db_mtime, commit)
    if _changed_cache is None or _changed_cache[0] != key:
        _changed_cache = (key, changed_files(root, commit))
    return _changed_cache[1]


def _position_staleness_warning(file: str) -> str | None:
    """Warning text when ``file`` has changed since the graph was
    built — positions in a build-time snapshot drift with edits, and
    the (file, line) → node mapping fails SILENTLY otherwise."""
    root = _active_root()
    changed = _files_changed_since_build()
    if root is None or not changed:
        return None
    queried = Path(file)
    if not queried.is_absolute():
        queried = root / queried
    if queried.resolve() not in changed:
        return None
    return (
        f"{file} changed since the graph was built "
        f"(commit {_build_commit()}) — positions may map to the wrong "
        f"symbol; rebuild the graph (sphinx-build / nexus analyze) "
        f"for faithful answers"
    )


def _workspace_payload() -> dict[str, Any]:
    """Workspace block for briefings: which tree the active graph was
    built from, whether it still matches the checkout, and which
    sibling checkouts (git worktrees) carry graphs of their own.

    This is the wrong-tree tripwire: a graph is a snapshot of ONE
    checkout, and the mismatch between "graph built on branch X" and
    "checkout now on branch Y" — or between the server's checkout and
    the session's worktree — is otherwise invisible because every
    query still returns plausible answers.
    """
    assert _workspace is not None
    statuses = discover(_workspace)
    active = next(s for s in statuses if s.is_active)
    others = [s.to_payload() for s in statuses if not s.is_active]
    payload: dict[str, Any] = {
        "active": active.to_payload(),
        "others": others,
    }

    warnings: list[str] = []
    if active.provenance is None:
        warnings.append(
            "the active graph carries no provenance stamp (built by "
            "nexus < 0.12) — rebuild it to make the graph self-describing"
        )
    else:
        prov = GitProvenance.from_stamp(active.provenance)
        if (
            prov is not None
            and active.branch
            and prov.branch
            and prov.branch != active.branch
        ):
            warnings.append(
                f"the active graph was built on branch {prov.branch!r} "
                f"but the checkout is now on {active.branch!r} — rebuild "
                f"the graph, or switch with use_workspace if you meant "
                f"another checkout"
            )
    if any(o["has_graph"] for o in others):
        warnings.append(
            "sibling worktrees with their own graphs exist — if your "
            "session is working inside one of them, call "
            "use_workspace(<its root>) so queries answer from that tree"
        )
    if warnings:
        payload["warnings"] = warnings
    return payload


# ------------------------------------------------------------------
# Usage journal — the self-observation channel
# ------------------------------------------------------------------

#: Override the journal location; set EMPTY to disable journaling.
USAGE_JOURNAL_ENV = "NEXUS_USAGE_LOG"


def _usage_journal_path() -> Path | None:
    raw = os.environ.get(USAGE_JOURNAL_ENV)
    if raw is not None:
        return Path(raw).expanduser() if raw.strip() else None
    return Path.home() / ".nexus" / "usage.jsonl"


def _journal_usage(
    tool: str, args: tuple, kwargs: dict, ms: float, outcome: str,
) -> None:
    """Append one usage record; never raises into the tool call."""
    try:
        path = _usage_journal_path()
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "tool": tool,
            "args": repr(args)[:200] if args else "",
            "kwargs": repr(kwargs)[:200] if kwargs else "",
            "ms": round(ms, 1),
            "outcome": outcome,
            "workspace": str(_active_root()) if _workspace is not None else None,
            "pid": os.getpid(),
        }
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        logger.debug("Usage journal write failed", exc_info=True)


def nexus_tool(fn):
    """Register an MCP tool with usage journaling.

    The journal (``~/.nexus/usage.jsonl``; ``NEXUS_USAGE_LOG`` overrides
    the path, empty value disables) is ground truth for evaluating which
    tools agents actually reach for — per call: timestamp, tool, args
    (repr-truncated), duration, outcome, active workspace, server pid.
    Tool evaluation then rests on recorded behavior instead of anyone's
    memory. Journaling never blocks or fails a tool call.
    """
    def record(args: tuple, kwargs: dict, started: float, outcome: str) -> None:
        _journal_usage(
            fn.__name__, args, kwargs,
            (time.perf_counter() - started) * 1000, outcome,
        )

    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def async_wrapper(*args, **kwargs):
            started = time.perf_counter()
            outcome = "ok"
            try:
                return await fn(*args, **kwargs)
            except Exception:
                outcome = "exception"
                raise
            finally:
                record(args, kwargs, started, outcome)
        return _mcp.tool()(async_wrapper)

    @functools.wraps(fn)
    def sync_wrapper(*args, **kwargs):
        started = time.perf_counter()
        outcome = "ok"
        try:
            return fn(*args, **kwargs)
        except Exception:
            outcome = "exception"
            raise
        finally:
            record(args, kwargs, started, outcome)
    return _mcp.tool()(sync_wrapper)


# ------------------------------------------------------------------
# MCP Tools
# ------------------------------------------------------------------


@nexus_tool
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
    return to_json(to_dict(results))


@nexus_tool
def node_at(file: str, line: int) -> str:
    """Map a file position to the graph node enclosing it.

    The bridge from position-speaking tools into the graph: language
    servers, stack traces, and editors report (file, line); the graph
    speaks node IDs. Feed a position from goToDefinition /
    findReferences / a traceback here, get the innermost enclosing
    function / method / class node (module-scope positions return the
    module node), then continue with ``context``, ``impact``,
    ``provenance_chain``, ``callers`` for the wider picture.

    Args:
        file: File path, absolute or relative to the project root.
        line: 1-based line number, as editors and LSP report it.
    """
    q = _get_query()
    result = q.node_at(file, line, project_root=_active_root())
    warning = _position_staleness_warning(file)
    if result is None:
        payload: dict[str, Any] = {
            "error": f"No graph node encloses {file}:{line}",
            "hint": (
                "Either the file is outside the analyzed tree, or the "
                "graph predates it — rebuild (sphinx-build / nexus "
                "analyze) and retry. Line numbers shift with edits: a "
                "stale graph maps positions to the wrong symbol."
            ),
        }
    else:
        payload = to_dict(result)
    if warning is not None:
        payload["warning"] = warning
    return to_json(payload)


@nexus_tool
def context(node_id: str, limit_per_type: int = 25) -> str:
    """Get a 360-degree view of a node: its attributes and all connections.

    Shows the node's properties plus all incoming and outgoing edges
    grouped by type. This is the primary tool for understanding a symbol.

    Each edge-type bucket is sorted most-connected-first and capped at
    ``limit_per_type`` entries; when anything is dropped, an ``omitted``
    block reports per-bucket counts. A hub node's full context is
    megabytes of JSON — use ``neighbors(node_id, edge_types=...)`` for a
    complete single-type list instead of removing the cap.

    Args:
        node_id: Node ID (e.g., "py:function:sn_solver.solve_sn").
        limit_per_type: Max entries per edge-type bucket (default 25).
            ``0`` means no cap — expect a huge payload on hub nodes.
    """
    q = _get_query()
    return to_json(
        assemble_context(
            q,
            node_id,
            per_type_limit=limit_per_type if limit_per_type > 0 else None,
        )
    )


@nexus_tool
def impact(
    target: str,
    direction: str = "upstream",
    max_depth: int = 3,
    edge_types: str = "",
    limit_per_depth: int = 50,
) -> str:
    """Analyze blast radius: what depends on this symbol (upstream)
    or what this symbol depends on (downstream).

    Results are grouped by depth:
    - depth=1: WILL BREAK — direct callers/importers
    - depth=2: LIKELY AFFECTED — indirect dependents
    - depth=3: MAY NEED TESTING — transitive

    Each depth bucket is sorted most-connected-first and capped at
    ``limit_per_depth`` nodes; ``total_affected`` is always the TRUE
    traversal count, and an ``omitted`` block reports per-depth drops.

    Args:
        target: Node ID of the symbol to analyze.
        direction: "upstream" (what depends on this) or "downstream" (what this depends on).
        max_depth: Maximum traversal depth (default 3).
        edge_types: Comma-separated edge types to follow (e.g., "calls,imports").
                    Empty means all edge types.
        limit_per_depth: Max nodes per depth bucket (default 50).
            ``0`` means no cap — expect a huge payload on hub nodes.
    """
    if direction not in ("upstream", "downstream"):
        return to_json({
            "error": f"direction must be 'upstream' or 'downstream', "
                     f"got {direction!r}",
        })
    q = _get_query()
    types = [t.strip() for t in edge_types.split(",") if t.strip()] or None
    return to_json(
        assemble_impact(
            q,
            target,
            direction=direction,
            max_depth=max_depth,
            edge_types=types,
            per_depth_limit=limit_per_depth if limit_per_depth > 0 else None,
        )
    )


@nexus_tool
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
    return to_json(assemble_shortest_path(q, source, target, max_hops=max_hops))


@nexus_tool
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
    if direction not in ("in", "out", "both"):
        return to_json({
            "error": f"direction must be 'in', 'out', or 'both', "
                     f"got {direction!r}",
        })
    q = _get_query()
    types = [t.strip() for t in edge_types.split(",") if t.strip()] or None
    return to_json(assemble_neighbors(q, node_id, direction=direction, edge_types=types))


@nexus_tool
def god_nodes(top_n: int = 10) -> str:
    """Get the most connected nodes in the graph.

    These are the central concepts/symbols with the most relationships.

    Args:
        top_n: Number of nodes to return (default 10).
    """
    q = _get_query()
    results = q.god_nodes(top_n=top_n)
    return to_json(to_dict(results))


@nexus_tool
def stats() -> str:
    """Get graph-level statistics: node/edge counts by type, density, etc."""
    q = _get_query()
    return to_json(to_dict(q.stats()))


@nexus_tool
def communities(min_size: int = 3) -> str:
    """Detect functional communities (groups of tightly connected symbols).

    Uses greedy modularity optimization to find natural groupings.

    Args:
        min_size: Minimum community size to include (default 3).
    """
    q = _get_query()
    return to_json(assemble_communities(q, min_size=min_size))


@nexus_tool
def native_place(min_callers: int = 1, exclude: str = "", limit: int = 50) -> str:
    """Find functions that may belong inside a class (Feature-Envy / 'native place').

    A module-level function whose every non-test caller is a method of a
    SINGLE class is a candidate to move into that class. Cross-module
    candidates are the strongest; same-module private helpers are weaker
    (often a fine idiom). A pure, independently-tested free function consumed
    by one class is usually correct as-is — a high `excluded_callers` count
    flags that case, so weight it down.

    Args:
        min_callers: Minimum considered (non-test) method callers (default 1).
        exclude: Comma-separated substrings; functions/callers whose id
            contains any are ignored, on top of the built-in is_test flag
            (e.g. "scratch,derivations").
        limit: Max candidates (default 50; 0 = all).
    """
    q = _get_query()
    toks = tuple(t.strip() for t in exclude.split(",") if t.strip())
    results = q.native_place_candidates(
        min_callers=min_callers, exclude=toks, limit=limit,
    )
    return to_json(to_dict(results))


@nexus_tool
def twin_paths(
    min_similarity: float = 0.7,
    min_tokens: int = 35,
    exclude: str = "",
    limit: int = 50,
) -> str:
    """Find twin paths — independent implementations of the same computation.

    Two functions whose AST bodies share a high fraction of structural
    shingles (a Type-2/3 clone) but where neither calls the other: the
    coding-elegance Pattern-2 / single-source-of-truth smell. The fingerprint
    captures the array math (`@`, `einsum`, slicing) the call graph cannot
    see. Cross-module pairs are the strongest signal.

    Surfaces candidates; judgment decides. Symmetric-by-design pairs
    (`apply`/`apply_transpose`, `domain`/`codomain`) and shared small
    templates (a one-line convergence check) legitimately resemble each other.

    Args:
        min_similarity: Minimum Jaccard shingle overlap, 0.0-1.0 (default
            0.7). Genuine duplicates score >= 0.8; lower to ~0.6 to surface
            structurally-similar siblings.
        min_tokens: Minimum body token count; thinner stubs are ignored
            (default 35).
        exclude: Comma-separated substrings; functions whose id contains any
            are ignored, on top of the built-in is_test flag (e.g.
            "derivations,scratch").
        limit: Max pairs (default 50; 0 = all).
    """
    q = _get_query()
    toks = tuple(t.strip() for t in exclude.split(",") if t.strip())
    results = q.twin_paths(
        min_similarity=min_similarity, min_tokens=min_tokens,
        exclude=toks, limit=limit,
    )
    return to_json(to_dict(results))


@nexus_tool
def detect_changes(scope: str = "all") -> str:
    """Detect which symbols changed in git and what they affect.

    Maps git changes to graph symbols and computes upstream impact.

    Args:
        scope: "staged", "unstaged", "all", or "branch" (diff vs the
            merge-base with the repository's default branch).
    """
    q = _get_query()
    root = _active_root()
    if root is None:
        return to_json({"error": "project_root not set"})
    result = q.detect_changes(root, scope=scope)
    return to_json(to_dict(result))


@nexus_tool
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
        project_root=_active_root(),
        dry_run=dry_run,
    )
    return to_json(to_dict(result))


@nexus_tool
def provenance_chain(node_id: str) -> str:
    """Trace the full citation → equation → code chain for a symbol.

    Given a code function, shows which equations it implements and which
    literature citations those equations come from. The complete
    mathematical provenance.

    Args:
        node_id: Node ID of a code symbol or equation.
    """
    q = _get_query()
    return to_json(to_dict(q.provenance_chain(node_id)))


@nexus_tool
def verification_coverage(
    status_filter: str = "",
    limit: int = 0,
    offset: int = 0,
) -> str:
    """Map verification coverage: equation → code → test chains.

    Shows which equations are verified (have code + tests), which are
    implemented but untested, which are documented but unimplemented.

    Args:
        status_filter: Filter by status: "verified", "tested", "implemented",
                      "documented", "orphan_code". Empty = all.
        limit: Max number of entries to return. ``0`` (default) means
            no limit — return every matching entry. Use with ``offset``
            to page through very large result sets.
        offset: Number of entries to skip from the start of the list.
    """
    q = _get_query()
    filt = status_filter if status_filter else None
    return to_json(
        assemble_verification_coverage(
            q,
            status_filter=filt,
            limit=limit if limit > 0 else None,
            offset=offset,
        )
    )


@nexus_tool
def staleness() -> str:
    """Detect documentation pages that drifted from code.

    Compares git timestamps: if code was modified after its documentation
    page, the doc is flagged as stale. Only works with git and project_root set.
    """
    q = _get_query()
    result = q.staleness(_active_root())
    return to_json(to_dict(result))


def _briefing_payload() -> dict[str, Any]:
    """Briefing body shared by the tool and the ``nexus://briefing``
    resource."""
    q = _get_query()
    result = q.session_briefing(_active_root())
    payload = to_dict(result)
    if _workspace is not None:
        payload["workspace"] = _workspace_payload()
    return payload


def _path_from_file_uri(uri: str) -> Path | None:
    parsed = urlparse(uri)
    if parsed.scheme != "file" or not parsed.path:
        return None
    return Path(unquote(parsed.path))


async def _auto_align_workspace(ctx: Context) -> dict[str, Any] | None:
    """Roots-based wrong-tree self-correction.

    Claude Code answers the MCP ``roots/list`` request with the
    directory the SESSION was launched from — which, for a session
    working in a git worktree, differs from the main checkout this
    server was spawned against. When a reported root lies inside a
    sibling checkout that has a graph, switch to it; when it has no
    graph, report that instead of switching to nothing.

    Returns an info block describing what happened, or ``None`` when
    there is nothing to do (roots unsupported by the client, root
    outside every checkout, or already aligned). Roots updates on
    mid-session worktree entry are undocumented, so the manual
    ``use_workspace`` tool remains the fallback.
    """
    if _workspace is None or _workspace.root is None:
        return None
    try:
        roots = (await ctx.session.list_roots()).roots
    except Exception:
        return None  # client does not support roots — nothing to detect
    for root in roots:
        session_path = _path_from_file_uri(str(root.uri))
        if session_path is None:
            continue
        checkout = checkout_containing(_workspace, session_path)
        if checkout is None:
            continue
        if checkout == _workspace.root.resolve():
            return None  # session works in the active checkout
        outcome = _switch_workspace(checkout)
        info: dict[str, Any] = {
            "session_root": str(session_path),
            "detected_checkout": str(checkout),
        }
        if outcome.get("switched"):
            info["switched"] = True
        else:
            info["switched"] = False
            info["reason"] = outcome.get("error")
            if "hint" in outcome:
                info["hint"] = outcome["hint"]
        return info
    return None


@nexus_tool
async def session_briefing(ctx: Context) -> str:
    """Generate a structured briefing for starting a new session.

    Combines: graph stats, most connected nodes, stale docs,
    verification gaps, recent changes, unresolved references, and a
    ``workspace`` block reporting which checkout (branch/commit) the
    active graph was built from — with warnings when the graph no
    longer matches the checkout or when sibling git worktrees carry
    graphs of their own.

    Asks the client (via MCP roots) which directory the session was
    launched from and, when that lies inside a DIFFERENT checkout that
    has a graph, switches to it automatically — the briefing then
    answers from the session's own tree and reports the switch under
    ``workspace.auto_align``. Sessions that enter a worktree later
    still switch manually with ``use_workspace``.
    """
    auto_align = await _auto_align_workspace(ctx)
    payload = _briefing_payload()
    if auto_align is not None:
        payload.setdefault("workspace", {})["auto_align"] = auto_align
    return to_json(payload)


@nexus_tool
def workspaces() -> str:
    """List every checkout of this project (main tree + linked git
    worktrees) and the state of each one's knowledge graph.

    A graph database is a snapshot of ONE checkout. Each entry
    reports: root, currently checked-out branch, whether a graph has
    been built there, when, and the provenance stamped into it at
    build time (branch / commit / dirty). The entry marked
    ``is_active`` is the graph this server is answering from. If your
    session's working tree is a DIFFERENT entry (e.g. you are working
    inside .claude/worktrees/<name>), switch with ``use_workspace``
    before trusting structural queries.
    """
    if _workspace is None:
        return to_json({"error": "Graph not loaded. Call serve() first."})
    return to_json({"workspaces": [s.to_payload() for s in discover(_workspace)]})


@nexus_tool
def use_workspace(root: str) -> str:
    """Switch this server to the graph built inside another checkout
    (a git worktree or sibling clone) of the same project.

    If your session works inside a git worktree (e.g.
    ``.claude/worktrees/<name>``) while this server was launched
    against the main checkout, every query answers from the MAIN
    checkout's branch — plausible but wrong. Call ``workspaces`` to
    see the candidates, then switch here. The switch lasts for this
    server process (one agent session); auto-reload then tracks the
    new database. Switching back is the same call with the original
    root.

    Args:
        root: The checkout to read from — its worktree directory name
            (e.g. ``sn-nd-layout``), its branch name, or its absolute
            root path. Its graph is expected at the same root-relative
            location as the active database
            (e.g. ``docs/_build/html/_nexus/graph.db``).
    """
    if _workspace is None:
        return to_json({"error": "Graph not loaded. Call serve() first."})
    try:
        target_root = resolve_checkout_root(_workspace, root)
    except WorkspaceResolutionError as e:
        return to_json({"error": str(e)})
    return to_json(_switch_workspace(target_root))


def _switch_workspace(target_root: Path) -> dict[str, Any]:
    """Atomically re-point the server at ``target_root``'s graph.

    The shared switch core behind the ``use_workspace`` tool and the
    roots-based auto-alignment.  Every failure path returns an error
    payload BEFORE any state is assigned, so the active graph is
    untouched by a failed switch.
    """
    global _query, _workspace, _db_mtime
    if _workspace is None:
        return {"error": "Graph not loaded. Call serve() first."}
    if not target_root.is_dir():
        return {"error": f"Not a directory: {target_root}"}
    try:
        target = _workspace.sibling(target_root)
    except WorkspaceLayoutError as e:
        return {"error": str(e)}
    if not target.db_path.is_file():
        return {
            "error": f"No graph database at {target.db_path}",
            "hint": (
                "Build the graph inside that checkout first — for a "
                "Sphinx project run its docs build there (the graph is "
                "written by sphinx-build), or run `nexus analyze` — "
                "then call use_workspace again."
            ),
        }
    with _reload_lock:
        try:
            kg = load_sqlite(target.db_path)
        except Exception as e:
            return {"error": f"Failed to load {target.db_path}: {e}"}
        _query = GraphQuery(kg)
        _workspace = target
        _db_mtime = target.db_path.stat().st_mtime
    logger.info(
        "Switched workspace to %s (%d nodes, %d edges)",
        target.root, kg.node_count, kg.edge_count,
    )
    return {
        "switched": True,
        "nodes": kg.node_count,
        "edges": kg.edge_count,
        "workspace": _workspace_payload(),
    }


@nexus_tool
def retest(scope: str = "all") -> str:
    """Compute the minimum set of tests to re-run after changes.

    Uses git diff to find changed symbols, then traces upstream through
    the call graph to find all test functions that depend on them.

    Args:
        scope: "staged", "unstaged", "all", or "branch".
    """
    q = _get_query()
    root = _active_root()
    if root is None:
        return to_json({"error": "project_root not set"})
    result = q.retest(root, scope=scope)
    return to_json(to_dict(result))


@nexus_tool
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
    return to_json(to_dict(result))


@nexus_tool
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
    return to_json(to_dict(result))


@nexus_tool
def processes(
    min_length: int = 3,
    limit: int = 0,
    offset: int = 0,
) -> str:
    """Detect execution flows: maximal call chains from entry points.

    Returns named sequences showing how functions call each other
    from entry point to leaf. Useful for understanding how code executes.

    Args:
        min_length: Minimum chain length to include (default 3).
        limit: Max number of chains to return. ``0`` (default) means
            no limit — return every chain meeting ``min_length``.
        offset: Number of chains to skip from the start of the list.
    """
    q = _get_query()
    return to_json(
        assemble_processes(
            q,
            min_length=min_length,
            limit=limit if limit > 0 else None,
            offset=offset,
        )
    )


@nexus_tool
def graph_query(pattern: str, limit: int = 50) -> str:
    """Execute a structured graph traversal query.

    Mini query language for finding edges matching a pattern.

    Syntax:
        source_type -edge_type-> target_type [WHERE field=value]

    Examples:
        "function -calls-> function" — all function-to-function calls
        "file -contains-> equation" — all equations in doc pages
        "* -implements-> equation" — code implementing equations
        "function -type_uses-> external WHERE name=numpy*" — numpy usage
        "* -cites-> *" — all citation edges

    Wildcards: * matches any type. name=prefix* for prefix match.

    Args:
        pattern: Query pattern (see examples above).
        limit: Maximum results (default 50).
    """
    q = _get_query()
    results = q.graph_query(pattern, limit=limit)
    return to_json(results)


@nexus_tool
def ingest(file_path: str, llm_command: str = "") -> str:
    """Ingest a document (PDF, paper, text) into the knowledge graph.

    Uses an LLM to extract concepts, equations, relationships, and
    citations from the document and adds them as graph nodes/edges.

    Args:
        file_path: Path to the document (PDF, txt, md, rst, tex).
        llm_command: Shell command for LLM (default: 'claude -p').
    """
    from sphinxcontrib.nexus.ingest import ingest_file

    q = _get_query()
    kg = q.knowledge_graph

    p = Path(file_path)
    root = _active_root()
    if not p.is_absolute() and root is not None:
        p = root / p

    result = ingest_file(p, kg, llm_command=llm_command or None)
    return to_json({
        "source_file": result.source_file,
        "concepts_added": result.concepts_added,
        "equations_added": result.equations_added,
        "relationships_added": result.relationships_added,
        "citations_added": result.citations_added,
    })


@nexus_tool
def bridges(top_n: int = 10) -> str:
    """Find bridge nodes connecting separate communities.

    These are architectural hotspots with high betweenness centrality.
    Changing them has outsized impact across the codebase.

    Args:
        top_n: Number of bridges to return (default 10).
    """
    q = _get_query()
    results = q.bridges(top_n=top_n)
    return to_json(to_dict(results))


@nexus_tool
def callers(node_id: str, transitive: bool = False, max_depth: int = 3) -> str:
    """Get functions that call this symbol.

    Returns a clean list of caller nodes. Set transitive=True to walk
    the call graph up to max_depth.

    Args:
        node_id: Node ID of the function to query.
        transitive: If True, include indirect callers (depth 2+).
        max_depth: Maximum depth for transitive search (default 3).
    """
    q = _get_query()
    return to_json(to_dict(q.callers(node_id, transitive=transitive, max_depth=max_depth)))


@nexus_tool
def callees(node_id: str, transitive: bool = False, max_depth: int = 3) -> str:
    """Get functions that this symbol calls.

    Returns a clean list of callee nodes. Set transitive=True to walk
    the call graph down to max_depth.

    Args:
        node_id: Node ID of the function to query.
        transitive: If True, include indirect callees (depth 2+).
        max_depth: Maximum depth for transitive search (default 3).
    """
    q = _get_query()
    return to_json(to_dict(q.callees(node_id, transitive=transitive, max_depth=max_depth)))


@nexus_tool
def verification_audit(
    group_by: str = "",
    include_tests: bool = False,
) -> str:
    """Complete V&V audit in a single call.

    Combines verification_coverage + staleness into one actionable report.
    Returns: summary counts by status, prioritized gap list (equations
    without full verification chain), and optionally a ``grouped`` view
    bucketing those gaps by a chosen dimension.

    Args:
        group_by: Optional bucket dimension. One of ``"level"`` (by
            V&V level of the nearest test), ``"module"`` (by top-level
            Python package of the nearest implementing code node), or
            ``"equation"`` (by equation id). Empty string (default) —
            no grouping, flat ``gaps`` list only.
        include_tests: When True, the ``summary`` also reports
            ``tests_declared`` and ``tests_inferred`` counts so the
            caller can weigh how much of the "verified" total rides
            on explicit (marker/directive/registry) vs. heuristic
            evidence.
    """
    q = _get_query()
    result = q.verification_audit(
        _active_root(),
        group_by=group_by or None,
        include_tests=include_tests,
    )
    return to_json(to_dict(result))


@nexus_tool
def verification_gaps(
    module: str = "",
    level: str = "",
) -> str:
    """Surface per-bucket V&V gaps for this project.

    Returns three lists: untagged tests (no ``vv_level`` marker),
    unverified equations (no incoming TESTS edge), and missing
    error-catcher tags (only populated when a consumer supplies an
    error catalog via a future config path).

    Args:
        module: Optional top-level Python package filter
            (e.g. ``"orpheus"``). Empty = no module filter.
        level: Optional V&V level filter, one of ``"L0"`` / ``"L1"``
            / ``"L2"`` / ``"L3"``. Empty = no level filter.
    """
    q = _get_query()
    result = q.verification_gaps(
        module=module or None,
        level=level or None,
    )
    return to_json(to_dict(result))


# ------------------------------------------------------------------
# MCP Resources
# ------------------------------------------------------------------


@_mcp.resource("nexus://graph/stats")
def resource_stats() -> str:
    """Graph overview: node/edge counts, types, density."""
    q = _get_query()
    return to_json(to_dict(q.stats()))


@_mcp.resource("nexus://graph/communities")
def resource_communities() -> str:
    """All detected functional communities."""
    q = _get_query()
    results = q.communities(min_size=2)
    summaries = []
    for c in results:
        member_names = [m.name for m in sorted(c.members, key=lambda n: n.degree, reverse=True)[:10]]
        summaries.append({"id": c.id, "label": c.label, "size": c.size, "top_members": member_names})
    return to_json(summaries)


@_mcp.resource("nexus://briefing")
def resource_briefing() -> str:
    """Session briefing: what you need to know right now."""
    return to_json(_briefing_payload())


@_mcp.resource("nexus://graph/schema")
def resource_schema() -> str:
    """Graph schema: available node types and edge types."""
    from sphinxcontrib.nexus.graph import EdgeType, NodeType
    return to_json({
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
    })


# ------------------------------------------------------------------
# Server entry point
# ------------------------------------------------------------------


def serve(
    db_path: Path,
    project_root: Path | None = None,
) -> None:
    """Load the graph and start the MCP server."""
    global _query, _workspace, _db_mtime

    _workspace = Workspace(
        db_path=db_path.resolve(),
        root=project_root.resolve() if project_root is not None else None,
    )

    kg = load_sqlite(db_path)
    _query = GraphQuery(kg)
    _db_mtime = db_path.stat().st_mtime

    logger.info(
        "Loaded graph: %d nodes, %d edges from %s",
        kg.node_count, kg.edge_count, db_path,
    )

    _mcp.run()
