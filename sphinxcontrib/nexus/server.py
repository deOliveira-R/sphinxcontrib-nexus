"""MCP server for sphinxcontrib-nexus knowledge graph.

Exposes the full GraphQuery API as MCP tools, making the knowledge
graph queryable by Claude and other MCP clients.

Usage:
    nexus serve                      # opens <project root>/.nexus/graph.db
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

from mcp.server.mcpserver import Context, MCPServer

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
    GitProvenance,
    Workspace,
    WorkspaceLayoutError,
    WorkspaceResolutionError,
    checkout_containing,
    discover,
    files_changed_since,
    resolve_checkout_root,
)

logger = logging.getLogger(__name__)

_mcp = MCPServer("nexus", instructions=(
    "Knowledge graph server for code and documentation. "
    "Query relationships between functions, classes, equations, "
    "theory pages, and external dependencies."
))

# Module-level state set by serve(). One server process serves one
# agent session, so the active workspace is process-local state:
# switching it (``use_workspace``) cannot leak across sessions.
#
# ONE global, not two. The active graph and the checkout it answers
# about were separate module globals until 2026-08-16, and they had
# already disagreed in production: the server kept serving a loaded
# snapshot while ``_workspace.db_path`` named a file that had moved, so
# every reload's ``stat`` failed and the pair was permanently
# inconsistent. ``GraphQuery`` now carries its own ``Workspace``, which
# makes that state unrepresentable — there is nothing left to fall out
# of step with.
_query: GraphQuery | None = None
_db_mtime: float = 0.0

# Reload coordination: the MCP server may dispatch tool calls
# concurrently,
# and the mid-reload state (new ``_query`` assigned but ``_db_mtime``
# not yet updated) is short but real. The lock serializes the
# reload path so a second concurrent caller sees the finalized
# swap, not a torn read.
_reload_lock = threading.Lock()

#: Databases already reported unreadable, so :func:`_report_unreadable_db`
#: fires once per path rather than once per tool call. Discarded on the
#: first successful stat, so a database that vanishes again is reported
#: again. This is a log ledger, not graph state — it says what has been
#: SAID, which is why it does not belong on the query.
_unreadable_dbs: set[Path] = set()


def _active_workspace() -> Workspace | None:
    """The checkout-and-database pair the server answers from.

    Derived from the active query rather than stored beside it: one
    object, one home. ``None`` before :func:`serve`, and for a query
    constructed without a workspace."""
    return _query.workspace if _query is not None else None


def _report_unreadable_db(db_path: Path, error: OSError) -> None:
    """Announce, once, that the active graph can no longer be refreshed.

    A vanished database is a hard and actionable condition — the store
    moved, the checkout was cleaned, the file was replaced by a
    directory — and the server goes on answering from the snapshot it
    loaded at startup with every answer looking exactly as authoritative
    as before. This was logged at DEBUG (to avoid a message per tool
    call), which is how a server spent a session serving a graph whose
    file no longer existed.
    """
    if db_path in _unreadable_dbs:
        logger.debug("Stat still failing for %s: %s", db_path, error)
        return
    _unreadable_dbs.add(db_path)
    logger.warning(
        "Cannot read the graph database at %s (%s) — still answering from "
        "the snapshot loaded earlier, which can no longer be refreshed. "
        "Check `nexus config db`; if the store moved, restart the server.",
        db_path, error,
    )


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
    MCP tool dispatches can't observe a half-updated state.
    """
    global _query, _db_mtime
    ws = _active_workspace()
    if ws is None:
        return
    db_path = ws.db_path
    try:
        current_mtime = db_path.stat().st_mtime
    except OSError as e:
        # DB file missing or inaccessible — keep serving the previous
        # snapshot, and say so once (see _report_unreadable_db).
        _report_unreadable_db(db_path, e)
        return
    _unreadable_dbs.discard(db_path)
    if current_mtime <= _db_mtime:
        return

    with _reload_lock:
        # Re-check under the lock: another thread may have already
        # reloaded to the same mtime, or switched the active
        # workspace entirely (use_workspace) — in which case this
        # reload's pre-lock stat refers to the WRONG database and
        # must not clobber the switched-in graph.
        current = _active_workspace()
        if current is None or current.db_path != db_path:
            return
        if current_mtime <= _db_mtime:
            return
        try:
            kg = load_sqlite(db_path)
            new_query = GraphQuery(kg, workspace=ws)
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


def _list_limit(limit: int) -> int:
    """A tool's `limit`, with 0 meaning the project's setting.

    `[replies].items_per_list` is the one place to raise every list
    length at once — the number most likely to want changing as context
    windows grow. A caller can still pass an explicit value, and -1
    still means uncapped.
    """
    if limit != 0:
        return limit
    if _query is None:
        from sphinxcontrib.nexus.project import DEFAULTS
        return int(DEFAULTS["replies.items_per_list"])
    return int(_query.tunable("replies.items_per_list"))


def _get_query() -> GraphQuery:
    if _query is None:
        raise RuntimeError("Graph not loaded. Call serve() first.")
    _reload_if_stale()
    return _query


def _get_runtime_store():
    """The runtime-overlay sidecar store (``.nexus/traces/``), beside the
    active graph DB. Dynamic-trace runs live here, NOT in ``graph.db``
    (which is rebuilt on every ``sphinx-build``), and re-bind to the live
    graph by node-ID at query time."""
    from sphinxcontrib.nexus.runtime import RuntimeStore

    ws = _active_workspace()
    if ws is None:
        raise RuntimeError("No active workspace. Call serve() first.")
    return RuntimeStore.beside(ws.db_path)


def _load_run(name: str):
    """Load one stored run, or raise a clear error naming the alternatives."""
    store = _get_runtime_store()
    run = store.load(name)
    if run is None:
        available = [r["name"] for r in store.list_runs()]
        # Built outside the f-string: an expression split across physical
        # lines INSIDE the braces is PEP 701 syntax (3.12+), and this
        # package supports 3.10.
        alternatives = available or "(none — ingest one with runtime_ingest)"
        raise ValueError(f"no runtime run {name!r}; available: {alternatives}")
    return run


def _load_runs(names: str):
    """Load one OR a comma-separated set of runs, merging the set into the
    canonical-suite aggregate (so `dead` means fired in NO run, etc.)."""
    from sphinxcontrib.nexus.runtime import load_and_merge

    return load_and_merge(names, _load_run)


#: Overlay family → the attribute carrying it, and the ingest kinds that
#: can produce it. A run is a bag of orthogonal overlays: a `coverage`
#: ingest fills `coverage`, a `cprofile` ingest fills `calls` and
#: `edges`, a `viztracer` ingest fills `timeline`.
_RUNTIME_FAMILIES: dict[str, tuple[str, tuple[str, ...]]] = {
    "calls": ("calls", ("cprofile",)),
    "edges": ("edges", ("cprofile",)),
    "coverage": ("coverage", ("coverage",)),
    "timeline": ("timeline", ("viztracer",)),
}


def _require_family(run: Any, family: str, view: str) -> None:
    """Refuse a view of a run that cannot carry it, and say what can.

    Asking a cProfile run for branch coverage used to return ``[]`` —
    identical to a workload that genuinely exercised nothing. The
    docstrings even said so ("a coverage run has no timing and returns
    ``[]``"), which documents the ambiguity rather than removing it.

    `[M]` 2026-08-16, four of nexus's own tools failed this way on
    ORPHEUS's stored runs: ``runtime_timeline`` and ``runtime_branches``
    on a cProfile run, ``runtime_hotspots`` and ``runtime_edges`` on a
    coverage run. Every one answered ``[]``.

    This is ``lessons-L56`` — "nothing found" and "I looked in the wrong
    place" must not print the same thing — and the remedy is the same
    one line: name the thing you looked for.
    """
    attribute, kinds = _RUNTIME_FAMILIES[family]
    if getattr(run, attribute, None):
        return
    store = _get_runtime_store()
    usable = [
        r["name"] for r in store.list_runs() if r.get("kind") in kinds
    ] or [f"(none — capture one with kind={kinds[0]!r} and runtime_ingest)"]
    raise ValueError(
        f"run {run.name!r} was ingested as kind={run.kind!r} and carries no "
        f"{family!r} data, so {view} has nothing to read — this is not an "
        f"empty result, it is the wrong run. {family!r} comes from "
        f"{' or '.join(repr(k) for k in kinds)}; runs that have it: {usable}"
    )


def _active_root() -> Path | None:
    """Project root of the active workspace (``None`` when serving a
    bare database with no known root)."""
    return _query.project_root if _query is not None else None


#: Key added beside a stale ``file_path``. One spelling, so a consumer
#: can test for it and the tests can pin it.
STALE_KEY = "stale"

#: Largest payload a tool may return, in characters (~5k tokens).
#:
#: A tool's answer lands in an agent's context and stays there. Measured
#: on ORPHEUS 2026-08-16, BEFORE this existed:
#:
#:     processes()            1,238,013 tokens
#:     verification_audit()      41,901
#:     staleness()               27,529
#:     callers(transitive)       20,089
#:     impact()                  15,886
#:
#: `processes()` alone is several times any context window — it defaulted
#: to `limit=None`, meaning "every call chain in the graph". A tool that
#: can destroy the session it was called from is not a usable tool, and
#: no amount of per-tool care prevents the next one: this is the backstop
#: that makes the failure bounded instead of fatal.
#: Fallback when no query (and therefore no checkout, and therefore no
#: config) is loaded yet. The live value is
#: `[replies].max_characters` — see `project.DEFAULTS`.
TOOL_PAYLOAD_BUDGET = 20_000


def _reply_budget() -> int:
    """The active checkout's `[replies].max_characters`."""
    if _query is None:
        return TOOL_PAYLOAD_BUDGET
    return int(_query.tunable("replies.max_characters"))

#: Key carrying what the budget dropped. Present ONLY when something was.
BUDGET_KEY = "truncated"


def _fit_budget(payload: str, tool: str, budget: int | None = None) -> str:
    """Trim an over-budget payload to its largest list, and say so.

    Truncating silently would be the worst of both worlds — a partial
    answer that reads as complete (``lessons-L56``). So the reply keeps
    the head of the dominant list and carries a ``truncated`` block
    naming the tool, what was dropped, and the argument that would have
    returned it.

    Repeatedly shortens whichever list is currently largest — anywhere
    in the structure — until the whole reply fits. Trimming only the
    single biggest one is not enough: ``impact`` spreads across
    ``by_depth`` and ``context`` across one bucket per edge type, so a
    single cut leaves the rest of the payload intact and still oversized
    (measured: 63k and 48k characters against a 20k budget).

    If nothing can be trimmed the payload is returned untouched rather
    than mangled — over budget beats invalid.
    """
    budget = _reply_budget() if budget is None else budget
    if len(payload) <= budget:
        return payload
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return payload

    def lists_in(obj: Any, path: str = "") -> list[tuple[Any, Any, str]]:
        """(container, key, path) for every list, at any depth."""
        found: list[tuple[Any, Any, str]] = []
        if isinstance(obj, dict):
            for k, v in obj.items():
                here = f"{path}.{k}" if path else str(k)
                if isinstance(v, list):
                    found.append((obj, k, here))
                found.extend(lists_in(v, here))
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                found.extend(lists_in(v, f"{path}[]"))
        return found

    root = {"results": data} if isinstance(data, list) else data
    if not isinstance(root, dict):
        return payload
    slots = lists_in(root)
    if not slots:
        return payload

    original = {path: len(c[k]) for c, k, path in slots}
    # Cut the current largest by a third each pass. Converges quickly and
    # spreads the loss across whichever lists are actually heavy, rather
    # than gutting one and leaving another untouched.
    for _ in range(200):
        if len(to_json(root)) <= budget - 500:      # room for the note
            break
        biggest = max(slots, key=lambda s: len(to_json(s[0][s[1]])))
        container, key, _path = biggest
        current = container[key]
        if len(current) <= 1:
            remaining = [s for s in slots if len(s[0][s[1]]) > 1]
            if not remaining:
                break
            slots = remaining
            continue
        container[key] = current[: max(1, len(current) * 2 // 3)]

    dropped = {
        path: {"kept": len(c[k]), "of": original[path]}
        for c, k, path in slots
        if len(c[k]) < original[path]
    }
    if not dropped:
        return to_json(root)
    root[BUDGET_KEY] = {
        "tool": tool,
        "lists": dropped,
        "why": (
            f"the reply exceeded the {budget}-character tool budget, which "
            f"exists so one call cannot fill an agent's context"
        ),
        "how_to_get_the_rest": (
            "narrow the query, or use this tool's own limit/offset "
            "arguments — the counts above are the true totals"
        ),
    }
    return to_json(root)


def _mark_stale_positions(payload: str) -> str:
    """Flag every position in ``payload`` whose file has changed since
    the graph was built.

    A graph is a snapshot: its ``(file_path, lineno)`` pairs are true of
    the tree at build time, and an edit above a definition moves it
    without moving the stored line. The failure is silent — the position
    still looks like a position, and ``NodeResult``'s own docstring
    invites feeding it straight to an editor or ``Read``.

    Attached HERE rather than at the 51 producers that emit a position,
    or at each of the 40 tools: staleness is a property of the *server's
    two states* (graph versus working tree), not of any one query, and
    one site cannot drift from another. It marks each affected object in
    place rather than adding a summary key, because the payload may be a
    bare JSON array as easily as an object — and a flag on the item is
    what a reader needs anyway.

    Costs nothing on a fresh graph: the first line answers "is anything
    changed at all?" from a cached set, and returns the ORIGINAL string
    — not a re-serialisation — whenever there is nothing to say. So a
    healthy server's payloads are byte-identical to what they were
    before this existed. Measured 2026-08-16 on ORPHEUS's graph
    (23013 nodes) against the worst payload the server can produce,
    ``context`` on the top god node uncapped, **2059 KiB**: 0.00 ms
    fresh, 35.2 ms with 30 files dirty. The default ``limit_per_type``
    keeps a real payload two orders below that.
    """
    if _query is None:
        return payload
    changed = _query.files_changed_since_build
    root = _query.project_root
    if not changed or root is None:
        return payload
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return payload  # not every tool returns JSON

    verdict: dict[str, bool] = {}  # resolve() hits the disk — ask once per file

    def is_stale(raw: str) -> bool:
        if raw not in verdict:
            path = Path(raw)
            if not path.is_absolute():
                path = root / path
            verdict[raw] = path.resolve() in changed
        return verdict[raw]

    note = (
        f"changed since the graph was built (commit {_query.build_commit}) "
        f"— this position may name the wrong symbol; rebuild the graph "
        f"(sphinx-build / nexus analyze)"
    )
    marked = False

    def walk(node: Any) -> None:
        nonlocal marked
        if isinstance(node, dict):
            file_path = node.get("file_path")
            if isinstance(file_path, str) and file_path and is_stale(file_path):
                node[STALE_KEY] = note
                marked = True
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(data)
    return to_json(data) if marked else payload


def _indexed_files() -> set[str]:
    """Repo-relative paths of every file the active graph indexed.

    The graph knows what it read, so "is this graph still current?" can
    be asked about the files that MATTER rather than against a guessed
    extension list.
    """
    ws = _active_workspace()
    if ws is None or _query is None:
        return set()
    root = str(ws.root).rstrip("/") + "/"
    paths = set()
    for _, attrs in _query._g.nodes(data=True):
        p = attrs.get("file_path")
        if isinstance(p, str) and p.startswith(root):
            paths.add(p[len(root):])
    return paths


def _provenance_warnings(active: Any, prov: GitProvenance) -> list[str]:
    """Warn when the graph no longer describes the checkout.

    ⛔ This used to compare BRANCH NAMES, which is a proxy that fails in
    the most ordinary workflow there is: fast-forward a branch into
    `main` and delete it, and every session is told to rebuild a graph
    whose commit is now an ancestor of HEAD with no source change at
    all. Measured on ORPHEUS 2026-08-16 — 25 files differed from the
    build commit and **none of them was indexed**; all 25 were agent
    memory and plan notes under `.claude/`.

    The actionable question is not "which branch was this built on" but
    "did anything the graph INDEXES change since". One `git diff`
    against the build commit answers it, and it covers uncommitted work
    too, since diffing a commit compares it to the working tree.

    Same lesson the sibling-graph warning below already learned the
    expensive way: existence is noise, drift is signal.
    """
    changed = files_changed_since(active.workspace.root, prov.commit)
    if changed is None:
        # The build commit is unreachable — a deleted branch that was
        # never merged, a re-cloned tree, a different repository. That
        # is a real mismatch and the branch name is all we have left.
        if active.branch and prov.branch and prov.branch != active.branch:
            return [
                f"the active graph was built on branch {prov.branch!r} at "
                f"{prov.commit}, which this checkout ({active.branch!r}) "
                f"cannot resolve — rebuild the graph, or switch with "
                f"use_workspace if you meant another checkout"
            ]
        return []

    drifted = sorted(changed & _indexed_files())
    if not drifted:
        return []
    shown = ", ".join(drifted[:3])
    more = f" (+{len(drifted) - 3})" if len(drifted) > 3 else ""
    return [
        f"{len(drifted)} indexed file(s) changed since the active graph "
        f"was built at {prov.commit} — {shown}{more}. Rebuild with "
        f"sphinx-build to bring the graph up to date"
    ]


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
    ws = _active_workspace()
    if ws is None:
        raise RuntimeError("Graph not loaded. Call serve() first.")
    statuses = discover(ws)
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
        if prov is not None:
            warnings.extend(_provenance_warnings(active, prov))
    # Warn about sibling graphs only when one is FRESHER than the
    # active graph — the mere existence of sibling graphs fired 39
    # warnings across 6 real sessions against 4 workspace switches
    # (issue #15 evaluation): existence alone is noise, freshness is
    # the actionable signal.
    fresher = [
        s for s in statuses
        if not s.is_active
        and s.graph_mtime is not None
        and (active.graph_mtime is None or s.graph_mtime > active.graph_mtime)
    ]
    if fresher:
        roots = ", ".join(str(s.workspace.root) for s in fresher[:3])
        warnings.append(
            f"sibling checkout(s) carry a FRESHER graph than the active "
            f"one ({roots}) — if your session is working inside one of "
            f"them, call use_workspace(<its root>) so queries answer "
            f"from that tree"
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
            "workspace": str(root) if (root := _active_root()) else None,
            "pid": os.getpid(),
        }
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        logger.debug("Usage journal write failed", exc_info=True)


def nexus_tool(fn):
    """Register an MCP tool, journaled and staleness-checked.

    Two concerns ride here because both are properties of *every* tool
    call rather than of any tool, and both would otherwise be a line
    each tool author has to remember — which is how position staleness
    ended up applied by exactly 1 of the 40.

    The journal (``~/.nexus/usage.jsonl``; ``NEXUS_USAGE_LOG`` overrides
    the path, empty value disables) is ground truth for evaluating which
    tools agents actually reach for — per call: timestamp, tool, args
    (repr-truncated), duration, outcome, active workspace, server pid.
    Tool evaluation then rests on recorded behavior instead of anyone's
    memory. Journaling never blocks or fails a tool call.

    The staleness pass (:func:`_mark_stale_positions`) flags returned
    positions whose file has moved under the graph. It is a no-op, down
    to the exact bytes, whenever the graph is fresh.
    """
    def record(args: tuple, kwargs: dict, started: float, outcome: str) -> None:
        _journal_usage(
            fn.__name__, args, kwargs,
            (time.perf_counter() - started) * 1000, outcome,
        )

    def annotate(result: Any) -> Any:
        if not isinstance(result, str):
            return result
        # Budget LAST: staleness adds keys, so fitting before it could
        # push the reply back over.
        return _fit_budget(_mark_stale_positions(result), fn.__name__)

    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def async_wrapper(*args, **kwargs):
            started = time.perf_counter()
            outcome = "ok"
            try:
                return annotate(await fn(*args, **kwargs))
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
            return annotate(fn(*args, **kwargs))
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
    results = q.query(text, node_types=types, limit=_list_limit(limit))
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
    result = q.node_at(file, line)
    if result is None:
        payload: dict[str, Any] = {
            "error": f"No graph node encloses {file}:{line}",
            # Named, not only interpolated into the message: it is what
            # the staleness pass at the tool boundary keys on, so the
            # no-match answer — the least trustworthy one a stale graph
            # gives — is flagged by the same mechanism as a match.
            "file_path": file,
            "hint": (
                "Either the file is outside the analyzed tree, or the "
                "graph predates it — rebuild (sphinx-build / nexus "
                "analyze) and retry. Line numbers shift with edits: a "
                "stale graph maps positions to the wrong symbol."
            ),
        }
    else:
        payload = to_dict(result)
    return to_json(payload)


@nexus_tool
def context(node_id: str, limit_per_type: int = 0) -> str:
    """Get a 360-degree view of a node: its attributes and all connections.

    Shows the node's properties plus all incoming and outgoing edges
    grouped by type. This is the primary tool for understanding a symbol.

    Grouping by direction and edge type is the point: "if I change
    this, who breaks?" is ``incoming.calls``, one key — the flat
    ``neighbors`` view makes you rebuild that grouping yourself.

    Within a bucket, PRODUCTION entries lead and test-tree entries
    follow, then most-connected-first. Tests swamp incoming calls
    (measured: 17 of 18, 22 of 25 on real hubs), so the one caller you
    must not break would otherwise sit below the cap. Query a test node
    and nothing is demoted — there, test material is the subject.

    Each edge-type bucket is capped at ``limit_per_type`` entries; when
    anything is dropped, an ``omitted`` block reports per-bucket counts.
    A hub node's full context is megabytes of JSON — use
    ``neighbors(node_id, edge_types=...)`` for a complete single-type
    list instead of removing the cap.

    Args:
        node_id: Node ID (e.g., "py:function:sn_solver.solve_sn").
        limit_per_type: Max entries per edge-type bucket. Omit to use
            the project's `[replies].neighbors_per_edge_type` (default
            25); ``-1`` removes the cap, which on a hub node produces a
            payload the reply budget will then trim anyway.
    """
    q = _get_query()
    return to_json(
        assemble_context(
            q,
            node_id,
            per_type_limit=(
                q.tunable("replies.neighbors_per_edge_type")
                if limit_per_type == 0
                else (limit_per_type if limit_per_type > 0 else None)
            ),
        )
    )


@nexus_tool
def impact(
    target: str,
    direction: str = "upstream",
    max_depth: int = 3,
    edge_types: str = "",
    limit_per_depth: int = 0,
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
        limit_per_depth: Max nodes per depth bucket. Omit to use the
            project's `[replies].nodes_per_impact_depth` (default 50);
            ``-1`` removes the cap.
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
            per_depth_limit=(
                q.tunable("replies.nodes_per_impact_depth")
                if limit_per_depth == 0
                else (limit_per_depth if limit_per_depth > 0 else None)
            ),
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
    """Get direct connections of a node, as one flat entry each.

    An entry is the neighbouring node plus ``edge_type`` and
    ``direction`` — and only the ones your query left open: filter to a
    single edge type, or ask for one direction, and that field is
    omitted rather than repeated on every entry. Parallel edges (three
    ``isinstance`` calls) collapse into one entry carrying ``times``.
    Entries are ranked project-symbols-first, most-connected-first, so a
    budget-truncated answer keeps the useful half.

    Use ``context`` for the same relations grouped by edge type.

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
def god_nodes(top_n: int = 10, include_placeholders: bool = False) -> str:
    """The project's structural hubs — its most connected symbols.

    These are the concepts everything else hangs off, so they are where
    a change costs the most and where an unfamiliar codebase is best
    entered.

    stdlib and installed-package nodes are excluded by default: ranking
    the raw graph puts `numpy.array`, `float` and `int` at the top,
    which describes Python rather than the project.

    Args:
        top_n: Number of nodes to return (default 10).
        include_placeholders: Include stdlib/installed/unresolved nodes.
            A different and also-real question — "what does this project
            lean on hardest?"
    """
    q = _get_query()
    results = q.god_nodes(top_n=top_n, include_placeholders=include_placeholders)
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
def native_place(min_callers: int = 1, exclude: str = "", limit: int = 0) -> str:
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
        min_callers=min_callers, exclude=toks, limit=_list_limit(limit),
    )
    return to_json(to_dict(results))


@nexus_tool
def twin_paths(
    min_similarity: float = 0.7,
    min_tokens: int = 35,
    exclude: str = "",
    limit: int = 0,
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
        exclude=toks, limit=_list_limit(limit),
    )
    return to_json(to_dict(results))


@nexus_tool
def discriminations(min_sites: int = 2, exclude: str = "", limit: int = 0) -> str:
    """Find tags discriminated at multiple sites — candidate missing types.

    A function that branches on a string/enum tag (`if geometry ==
    "spherical"`, `match kind:`) discriminates on it. The SAME tag
    discriminated by many functions is the coding-elegance smell "a repeated
    conditional is a missing type — discriminate once, at the boundary": the
    repeated tests should usually collapse to one dispatch (a type / single
    registry / polymorphic call).

    Surfaces candidates; judgment decides. A genuinely open set with no shared
    behaviour (axis labels, a one-off parse) may legitimately stay a tag.

    Args:
        min_sites: Minimum distinct discriminating functions to report a tag
            (default 2).
        exclude: Comma-separated substrings; discriminating functions whose id
            contains any are ignored, on top of the built-in is_test flag.
        limit: Max tags (default 50; 0 = all).
    """
    q = _get_query()
    toks = tuple(t.strip() for t in exclude.split(",") if t.strip())
    results = q.discriminations(min_sites=min_sites, exclude=toks, limit=_list_limit(limit))
    return to_json(to_dict(results))


@nexus_tool
def dead_functions(exclude: str = "", limit: int = 0) -> str:
    """Find functions/methods with no static callers — dead-code candidates.

    Zero incoming `calls` edges (from non-test, non-excluded code) = a removal
    candidate. This is a CANDIDATE list, not a verdict: dynamic dispatch
    (registry / `getattr` / a callback passed to a solver) is invisible to the
    static call graph, and public entry points are legitimately uncalled
    internally. Each result carries `public` and `decorated` flags for those
    false-positive sources; the strongest signal — a private, undecorated
    function with no caller — is ranked first. Dunders are excluded (invoked
    implicitly).

    Args:
        exclude: Comma-separated substrings; a function OR a caller whose id
            contains any is ignored, on top of the built-in is_test flag.
        limit: Max results (default 50; 0 = all).
    """
    q = _get_query()
    toks = tuple(t.strip() for t in exclude.split(",") if t.strip())
    results = q.dead_functions(exclude=toks, limit=_list_limit(limit))
    return to_json(to_dict(results))


@nexus_tool
def protocol_conformers(min_methods: int = 2, exclude: str = "", limit: int = 0) -> str:
    """Find classes that satisfy a Protocol's method-set without declaring it.

    Python Protocols are satisfied structurally, but the AST `inherits` edge
    records only explicit subclassing — so a structural conformer has no edge,
    and "is every implementation connected to its Protocol?" is unanswerable
    from `inherits` alone. This matches a class to a Protocol when the class
    defines (by NAME) every non-dunder method the Protocol declares yet does
    not inherit it.

    A heuristic: it compares method NAMES, not signatures, and only direct
    methods. The authoritative check is a type checker (pyright / LSP
    goToImplementation). Use it to find classes to declare conformance on, or
    as evidence a Protocol is load-bearing.

    Args:
        min_methods: Minimum Protocol method-set size (default 2;
            single-method Protocols match too broadly).
        exclude: Comma-separated substrings; a Protocol or candidate class
            whose id contains any is ignored, on top of the is_test flag.
        limit: Max Protocols (default 50; 0 = all).
    """
    q = _get_query()
    toks = tuple(t.strip() for t in exclude.split(",") if t.strip())
    results = q.protocol_conformers(min_methods=min_methods, exclude=toks, limit=_list_limit(limit))
    return to_json(to_dict(results))


# ------------------------------------------------------------------
# Runtime overlay — dynamic execution-flow on the static graph
# ------------------------------------------------------------------


@nexus_tool
def runtime_ingest(
    artifact: str, kind: str = "cprofile", run: str = "default",
    source_prefix: str | list[str] = "", command: str = "",
    root: str = "",
) -> str:
    """Ingest a runtime trace and overlay it on the static graph by node-ID.

    The static graph is *what can run*; a runtime overlay is *what actually
    ran* — call counts, time, which edges fired, which polymorphic impl was
    reached, which branches were taken. Capture is consumer-side: run a
    canonical workload under a tracer, then hand the artifact here. Stored in
    `.nexus/traces/<run>.json` (a sidecar — never in graph.db, which is rebuilt
    on every sphinx-build) and re-bound to the live graph at query time.

    Args:
        artifact: Path to the trace file — a `cProfile`/`pstats` dump
            (`kind=cprofile`), a `coverage json --branch` report
            (`kind=coverage`), or a `viztracer` JSON trace (`kind=viztracer`).
        kind: "cprofile" (counts + time + call edges), "coverage" (line /
            branch coverage → the missing-type branch signal), or "viztracer"
            (temporal order → the observed stage sequence).
        run: Name to store under (re-ingesting the same name overwrites).
        source_prefix: Keep only trace records under these path prefixes
            (drops stdlib / third-party frames). Accepts a LIST, and usually
            needs one: profiling a test suite produces tests -> package
            records, so either directory alone drops one endpoint of every
            one of them, while the repository root sweeps in the virtualenv.
        command: Free-text note of the workload, recorded in run metadata.
        root: Directory that relative paths in the artifact are relative to —
            the working directory the traced run used. `coverage json` emits
            RELATIVE file keys and records the rundir nowhere in the report,
            so it cannot be recovered from the artifact; without this the
            join silently binds nothing. Defaults to the project root.
    """
    from sphinxcontrib.nexus import runtime as rt
    from sphinxcontrib.nexus.project import ProjectConfig

    ingesters = {
        rt.KIND_CPROFILE: (rt.ingest_cprofile, "calls"),
        rt.KIND_COVERAGE: (rt.ingest_coverage, "coverage"),
        rt.KIND_VIZTRACER: (rt.ingest_viztracer, "timeline"),
    }
    if kind not in ingesters:
        return to_json(
            {"error": f"kind must be one of {sorted(ingesters)}, got {kind!r}"})
    ingest, family = ingesters[kind]
    prefixes = (
        [source_prefix] if isinstance(source_prefix, str) and source_prefix
        else list(source_prefix) or None
    )
    q = _get_query()
    base = Path(root) if root else ProjectConfig.load(Path.cwd()).root
    meta = {"command": command} if command else {}
    r = ingest(artifact, q.knowledge_graph, run,
               meta=meta, source_prefixes=prefixes, root=base)

    ledger = r.ledger
    payload: dict[str, Any] = {
        "run": r.name, "kind": r.kind,
        # The family this KIND fills, not "whichever is non-empty" — the
        # latter cannot tell a successful run apart from an empty one.
        "nodes": len(getattr(r, family)),
        "edges": len(r.edges),
        "root": str(base),
        "lookups": {
            "considered": ledger.considered,
            "bound": ledger.bound,
            "outside_scope": ledger.outside_scope,
            "unindexed_file": ledger.unindexed_file,
            "no_enclosing_node": ledger.no_enclosing_node,
        },
    }
    diagnosis = ledger.diagnosis()
    if diagnosis is not None:
        # Not stored. An empty run listed by `runtime_runs` answers every
        # query with a confident "nothing fired".
        payload["error"] = f"ingested nothing: {diagnosis}"
        return to_json(payload)
    _get_runtime_store().write(r)
    return to_json(payload)


@nexus_tool
def runtime_runs() -> str:
    """List ingested runtime runs (name, kind, metadata, node/edge counts)."""
    return to_json(_get_runtime_store().list_runs())


@nexus_tool
def runtime_hotspots(run: str = "default", by: str = "cumtime", limit: int = 20) -> str:
    """Nodes ranked by an observed runtime metric — the dynamic stage DAG.

    `by="cumtime"` is the dominant OBSERVED call chain (better than the static
    `processes` out-degree heuristic for a traced run); `by="ncalls"` the
    iteration-count / recompute smell (a property called 10k×/run = a caching
    opportunity); `by="tottime"` self-time hotspots. Needs a `cprofile` run.

    Args:
        run: Stored run name (default "default").
        by: "cumtime" | "ncalls" | "tottime".
        limit: Max nodes (default 20; 0 = all).
    """
    q = _get_query()
    loaded = _load_runs(run)
    _require_family(loaded, "calls", "runtime_hotspots")
    results = q.runtime_hotspots(loaded, by=by, limit=_list_limit(limit))
    return to_json(to_dict(results))


@nexus_tool
def runtime_edges(
    run: str = "default", mode: str = "dynamic_only", node: str = "",
    substantive_only: bool = False, limit: int = 0,
) -> str:
    """Overlay a run's call edges on the static CALLS edges.

    `mode="dynamic_only"`: fired edges with NO static counterpart — the
    dispatch the static resolver can't see (annotation-mediated dispatch via
    `self`/typed locals, issue #16) and the resolved face of polymorphism
    (which concrete impl ran). `mode="fired"`: static edges confirmed live,
    with call counts. `mode="dead"`: static edges among run-reachable nodes
    that never fired. A single run's `dead` is "dead in THIS run" — pass
    several comma-separated runs in `run` to union them into the real
    cross-suite dead-code signal. Needs `cprofile` run(s).

    Args:
        run: Stored run name, or comma-separated names to union (the
            canonical-suite aggregate).
        mode: "dynamic_only" | "fired" | "dead".
        node: Restrict to edges whose source id contains this substring.
        substantive_only: Drop edges where either endpoint is a property /
            trivial accessor, so polymorphic dispatch isn't buried under
            property-getter call edges (which dominate `dynamic_only` raw).
        limit: Max edges (default 50; 0 = all).
    """
    q = _get_query()
    loaded = _load_runs(run)
    _require_family(loaded, "edges", "runtime_edges")
    results = q.runtime_edges(
        loaded, mode=mode, node=node,
        substantive_only=substantive_only, limit=_list_limit(limit))
    return to_json(to_dict(results))


@nexus_tool
def runtime_branches(
    run: str = "default", node: str = "", partial_only: bool = True, limit: int = 0,
) -> str:
    """Per-node branch coverage — the accidental-vs-essential / missing-type signal.

    A node with `branches_hit < branches_total` didn't take every conditional
    outcome. Nodes that ALSO `discriminates_on` a tag are flagged and ranked
    first: a discrimination always taken one way is a type the code fakes with
    a conditional — the dynamic counterpart of the static `discriminations`
    smell. Pass comma-separated runs in `run` to union them (a branch is
    *missing* only if no run ever took it). Needs `coverage` run(s)
    (`coverage json --branch`).

    Args:
        run: Stored run name, or comma-separated names to union.
        node: Restrict to node ids containing this substring.
        partial_only: Keep only nodes with an unexercised branch (default True).
        limit: Max nodes (default 50; 0 = all).
    """
    q = _get_query()
    loaded = _load_runs(run)
    _require_family(loaded, "coverage", "runtime_branches")
    results = q.runtime_branches(
        loaded, node=node, partial_only=partial_only, limit=_list_limit(limit))
    return to_json(to_dict(results))


@nexus_tool
def runtime_timeline(run: str = "default", max_depth: int = -1, limit: int = 0) -> str:
    """The observed execution sequence — nodes in order of first entry.

    The unique thing a `viztracer` run adds over cProfile is *order*: this is
    the actual stage sequence (mesh → discretize → sweep → iterate → result),
    not aggregate counts. `max_depth` (>= 0) keeps only nodes at/above that
    call-stack depth — the high-level stages; -1 (default) keeps every depth.
    Needs a `viztracer` run. (Single run only — timestamps are per-run.)

    Args:
        run: Stored viztracer run name.
        max_depth: Keep nodes with stack depth <= this (-1 = all depths).
        limit: Max nodes (default 50; 0 = all).
    """
    q = _get_query()
    loaded = _load_run(run)
    _require_family(loaded, "timeline", "runtime_timeline")
    results = q.runtime_timeline(loaded, max_depth=max_depth, limit=_list_limit(limit))
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
    result = q.detect_changes(scope=scope)
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
    result = q.rename(old_name, new_name, dry_run=dry_run)
    return to_json(to_dict(result))


@nexus_tool
def provenance_chain(node_id: str) -> str:
    """Trace the full citation → equation → code chain for a symbol.

    Given a code function, shows which equations it implements and which
    literature citations those equations come from. The complete
    mathematical provenance.

    ``relations`` carries the math-to-math spine where the project
    declares one: which continuous form a discrete equation
    ``discretizes``, which parent it ``derives_from``, which exact form
    it ``approximates`` — plus the inverses, so you can read the chain
    from either end. Use it to answer what a test actually pins down.

    Args:
        node_id: Node ID of a code symbol, equation, or sphinx-proof
            environment (``prf:theorem:...``).
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

    Two drift signals: timestamp drift (code modified after the page that
    documents it — needs git and project_root) and dead references (top 10;
    the full list lives in the dead_references tool).
    """
    q = _get_query()
    result = q.staleness()
    return to_json(to_dict(result))


@nexus_tool
def dead_references(limit: int = 0) -> str:
    """Doc/docstring references whose code target no longer exists.

    The silent-drift failure: a class/function/attribute/equation was
    deleted or renamed but theory pages, docstrings, or quoted type
    annotations still reference the old name — Sphinx renders those as
    plain text with no warning. Reports each dead target with every
    site that still references it (file/line for docstrings, docname
    for pages). Only project-rooted names are judged; external
    references and members of classes with un-analyzed bases are never
    reported. ``rescued``/``undecidable`` counts show how many
    candidate targets the precision passes filtered out.

    Args:
        limit: Maximum dead targets to return (most-referenced first).
            0 means no limit. Totals always reflect the full count.
    """
    q = _get_query()
    result = q.dead_references()
    payload = to_dict(result)
    if limit > 0:
        payload["dead"] = payload["dead"][:limit]
    return to_json(payload)


def _briefing_payload() -> dict[str, Any]:
    """Briefing body shared by the tool and the ``nexus://briefing``
    resource."""
    q = _get_query()
    result = q.session_briefing()
    payload = to_dict(result)
    if _active_workspace() is not None:
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
    ws = _active_workspace()
    if ws is None or ws.root is None:
        return None
    try:
        roots = (await ctx.session.list_roots()).roots
    except Exception:
        return None  # client does not support roots — nothing to detect
    for root in roots:
        session_path = _path_from_file_uri(str(root.uri))
        if session_path is None:
            continue
        checkout = checkout_containing(ws, session_path)
        if checkout is None:
            continue
        if checkout == ws.root.resolve():
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
    ws = _active_workspace()
    if ws is None:
        return to_json({"error": "Graph not loaded. Call serve() first."})
    return to_json({"workspaces": [s.to_payload() for s in discover(ws)]})


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
            (``.nexus/graph.db``).
    """
    ws = _active_workspace()
    if ws is None:
        return to_json({"error": "Graph not loaded. Call serve() first."})
    try:
        target_root = resolve_checkout_root(ws, root)
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
    global _query, _db_mtime
    active = _active_workspace()
    if active is None:
        return {"error": "Graph not loaded. Call serve() first."}
    if not target_root.is_dir():
        return {"error": f"Not a directory: {target_root}"}
    try:
        target = active.sibling(target_root)
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
        _query = GraphQuery(kg, workspace=target)
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

    Uses git diff to find changed symbols, then walks the DEPENDENCE
    cone upstream — ``calls``, ``type_uses``, ``inherits``, the three
    ways a test's behaviour can depend on a symbol — to a fixed point.
    ``references``/``imports`` are mention relations and are excluded:
    following them reaches 78 % of a real suite from any symbol.

    ``safe_to_skip`` is the complement, counted over COLLECTABLE tests
    (functions and methods). ``cone_depth`` reports how far the walk had
    to go, and ``dependence_edges`` what counted — together they make
    the skip set auditable without shipping thousands of node ids.

    Args:
        scope: "staged", "unstaged", "all", or "branch".
    """
    q = _get_query()
    result = q.retest(scope=scope)
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
def graph_query(pattern: str, limit: int = 0) -> str:
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
    results = q.graph_query(pattern, limit=_list_limit(limit))
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
    global _query, _db_mtime

    workspace = Workspace(
        db_path=db_path.resolve(),
        root=project_root.resolve() if project_root is not None else None,
    )

    kg = load_sqlite(db_path)
    _query = GraphQuery(kg, workspace=workspace)
    _db_mtime = db_path.stat().st_mtime

    logger.info(
        "Loaded graph: %d nodes, %d edges from %s",
        kg.node_count, kg.edge_count, db_path,
    )

    _mcp.run()
