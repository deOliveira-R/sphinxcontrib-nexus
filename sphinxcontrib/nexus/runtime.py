"""Dynamic execution-flow overlay — runtime trace ingested onto the graph.

The static graph (``graph.db``) is *what can run*. A **runtime** overlay is
*what actually ran*: call counts, time, which edges fired, which polymorphic
implementation was reached, which branches were taken. It is a distinct graph
*species* that composes with the static graph **by join on node-ID** — it is
never written into ``graph.db`` (which is rebuilt on every ``sphinx-build``).
It lives in a sidecar, ``_nexus/traces/<run>.json``, and re-binds to the live
graph at query time because node IDs are stable across rebuilds.

This module is the **ingest + store** layer. The **overlay queries** that join
a :class:`RuntimeRun` against the static graph live on
:class:`~sphinxcontrib.nexus.query.GraphQuery` (``runtime_hotspots`` /
``runtime_edges`` / ``runtime_branches``), reached via the MCP ``runtime_*``
tools and the ``nexus runtime-*`` CLI.

Capture is **consumer-side** (project-specific): the project runs a canonical
workload under a tracer and hands the artifact here. Two backends:

* ``cProfile``/``pstats`` → call counts, self/cumulative time, dynamic call
  edges (the dispatch the static resolver can't see — see issue #16).
* ``coverage.py --branch`` → which lines/branch-arcs fired → the
  accidental-vs-essential branch signal (a conditional always taken one way
  across the production path is a *missing-type* suspect — the dynamic
  counterpart of the static ``discriminates_on`` smell).

No Sphinx import; usable standalone with a loaded graph.
"""

from __future__ import annotations

import json
import re
from bisect import bisect_right
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import networkx as nx

from sphinxcontrib.nexus.graph import KnowledgeGraph

# cProfile's ``co_firstlineno`` points at the first *decorator* line, while the
# AST records ``node.lineno`` as the *def* line — so a decorated function's
# trace line sits a few lines ABOVE its static lineno. The lines between the
# previous node's end and a def are only decorators/blanks/comments (no other
# node body lives there), so widening the effective start downward by this
# window absorbs the decorator stack without false matches. Measured to lift
# the cProfile→node join from 68% to 97% on a real ORPHEUS SN solve.
DECORATOR_WINDOW = 8

KIND_CPROFILE = "cprofile"
KIND_COVERAGE = "coverage"
KIND_VIZTRACER = "viztracer"
KIND_MERGED = "merged"


# ── The join: (file, line) trace record → static node id ────────────


def build_node_index(
    graph: KnowledgeGraph | nx.MultiDiGraph,
) -> dict[str, list[tuple[int, int, str]]]:
    """Index function/method nodes by file for the (file, line) join.

    Returns ``file_path -> sorted [(lineno, end_lineno, node_id)]``. Only
    ``function``/``method`` nodes participate: a property is a ``method`` node
    carrying ``file_path``/``lineno``, and class nodes would shadow their own
    methods in the enclosing-range fallback.
    """
    g = graph.nxgraph if isinstance(graph, KnowledgeGraph) else graph
    by_file: dict[str, list[tuple[int, int, str]]] = {}
    for node_id, attrs in g.nodes(data=True):
        if attrs.get("type") not in ("function", "method"):
            continue
        fp, ln = attrs.get("file_path"), attrs.get("lineno")
        if not fp or not ln:
            continue
        by_file.setdefault(fp, []).append((ln, attrs.get("end_lineno") or ln, node_id))
    for spans in by_file.values():
        spans.sort()
    return by_file


def resolve_node(
    index: dict[str, list[tuple[int, int, str]]],
    filename: str,
    firstlineno: int,
) -> str | None:
    """Map a ``(file, def-line)`` trace record onto a static node id.

    Exact def-line hit first; then a decorator-window / enclosing-body hit
    (innermost wins). ``None`` for records with no node — by design this is
    lambdas, comprehensions, and nested closures the AST attributes to their
    enclosing function rather than giving a node of their own.
    """
    spans = index.get(filename)
    if not spans:
        return None
    best: str | None = None
    starts = [s[0] for s in spans]
    # Only spans whose def line is at or below firstlineno + the decorator
    # window can contain it; scan those, innermost (latest start) wins.
    hi = bisect_right(starts, firstlineno + DECORATOR_WINDOW)
    for ln, end, node_id in spans[:hi]:
        if ln == firstlineno:
            return node_id
        if ln - DECORATOR_WINDOW <= firstlineno <= end:
            best = node_id
    return best


# ── The ingested run (the sidecar payload) ──────────────────────────


@dataclass
class RuntimeRun:
    """One ingested trace, keyed by static node-ID, joined at query time.

    A **bag of orthogonal overlays**, not a tagged union: each ingest kind
    fills the families it can measure (``cprofile`` → ``calls`` + ``edges``;
    ``coverage`` → ``coverage``; ``viztracer`` → ``timeline``), and
    :func:`merge_runs` legitimately produces a run carrying *several* families
    at once. ``kind`` records provenance — it does not gate which families are
    present. A query reads only the family it needs (and returns empty if the
    run never measured it).
    """

    name: str
    kind: str
    meta: dict[str, Any] = field(default_factory=dict)
    #: node_id -> {"ncalls", "tottime", "cumtime"}  (cprofile)
    calls: dict[str, dict[str, float]] = field(default_factory=dict)
    #: [caller_id, callee_id, count]  (cprofile)
    edges: list[tuple[str, str, int]] = field(default_factory=list)
    #: node_id -> {"lines_hit","lines_total","branches_hit","branches_total",
    #:             "missing_arcs"}  (coverage)
    coverage: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: node_id -> {"first_ts","count","min_depth"}  (viztracer; first_ts is
    #: milliseconds from the start of the trace, min_depth the shallowest
    #: call-stack depth the node appeared at)
    timeline: dict[str, dict[str, float]] = field(default_factory=dict)
    #: trace records (in-scope) that found no node — recall-gap audit
    unresolved: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RuntimeRun":
        return cls(
            name=data["name"],
            kind=data["kind"],
            meta=data.get("meta", {}),
            calls=data.get("calls", {}),
            edges=[tuple(e) for e in data.get("edges", [])],
            coverage=data.get("coverage", {}),
            timeline=data.get("timeline", {}),
            unresolved=data.get("unresolved", 0),
        )


# ── Multi-run union ─────────────────────────────────────────────────


def merge_runs(runs: list[RuntimeRun], name: str = "merged") -> RuntimeRun:
    """Union several runs into one — the canonical-suite aggregate.

    A single run answers "dead in THIS run"; the union answers "fired in NO
    canonical run", the real dead-code signal that corroborates the static
    ``dead_functions``. Whatever metric families the inputs carry are unioned:

    * **calls** — ncalls/tottime sum, cumtime takes the max.
    * **edges** — call counts sum (an edge present in any run is present).
    * **coverage** — a branch is *hit* if hit in any run, so the merged
      ``missing_arcs`` is the INTERSECTION of each run's missing arcs (arcs no
      run ever took); ``branches_total`` is structural (max across runs).
    * **timeline** — NOT merged (timestamps are per-run and incomparable);
      use a single run for ``runtime_timeline``.

    A single-run list returns that run unchanged.
    """
    if len(runs) == 1:
        return runs[0]
    if not runs:
        return RuntimeRun(name=name, kind=KIND_MERGED)
    merged = RuntimeRun(name=name, kind=KIND_MERGED,
                        meta={"merged_from": [r.name for r in runs]})

    for run in runs:
        for node_id, m in run.calls.items():
            agg = merged.calls.setdefault(
                node_id, {"ncalls": 0, "tottime": 0.0, "cumtime": 0.0})
            agg["ncalls"] += m["ncalls"]
            agg["tottime"] += m["tottime"]
            agg["cumtime"] = max(agg["cumtime"], m["cumtime"])
        merged.unresolved += run.unresolved

    edge_counts: dict[tuple[str, str], int] = {}
    for run in runs:
        for u, v, c in run.edges:
            edge_counts[(u, v)] = edge_counts.get((u, v), 0) + c
    merged.edges = [(u, v, c) for (u, v), c in edge_counts.items()]

    # coverage: a node's still-missing arcs are those missing in EVERY run.
    cov_nodes = {n for run in runs for n in run.coverage}
    for node_id in cov_nodes:
        present = [run.coverage[node_id] for run in runs if node_id in run.coverage]
        total = max(c["branches_total"] for c in present)
        missing_sets = [
            {tuple(a) for a in c["missing_arcs"]} for c in present
        ]
        still_missing = set.intersection(*missing_sets) if missing_sets else set()
        lines_total = max(c["lines_total"] for c in present)
        # lines_hit: per-run max is an approximation (we store the count, not
        # the hit-line set, so a true union isn't reconstructable). The branch
        # union above IS exact — it's the arc set, which we do store.
        lines_hit = max(c["lines_hit"] for c in present)
        merged.coverage[node_id] = {
            "lines_hit": lines_hit,
            "lines_total": lines_total,
            "branches_hit": total - len(still_missing),
            "branches_total": total,
            "missing_arcs": [list(a) for a in sorted(still_missing)],
        }
    return merged


def load_and_merge(names: str, load) -> RuntimeRun:
    """Load one run, or merge a comma-separated set (the canonical-suite
    aggregate). ``load`` is a ``name -> RuntimeRun`` callable — the server and
    CLI bind their own (workspace-store / db-path) loader and share this
    split+merge convention rather than each re-deriving it."""
    wanted = [n.strip() for n in names.split(",") if n.strip()]
    return merge_runs([load(n) for n in wanted], name=",".join(wanted))


# ── cProfile backend ────────────────────────────────────────────────


def overlay_cprofile(
    stats: dict[tuple[str, int, str], tuple[int, int, float, float, dict]],
    index: dict[str, list[tuple[int, int, str]]],
    name: str,
    meta: dict[str, Any] | None = None,
    source_prefix: str | None = None,
) -> RuntimeRun:
    """Join a ``pstats``-format stats dict onto node IDs.

    ``stats`` is :attr:`pstats.Stats.stats`:
    ``{(file, line, func): (cc, nc, tt, ct, callers)}`` where ``callers`` is
    ``{(file, line, func): (cc, nc, tt, ct)}``. ``nc`` is the (recursion-
    inclusive) call count, ``tt`` self time, ``ct`` cumulative time.

    Records outside ``source_prefix`` (when given) are dropped — stdlib/3rd-
    party frames collapse away, leaving the project's own stage DAG. Metrics
    aggregate **by node-ID** (a node may own several code objects): ncalls and
    tottime sum (both additive), cumtime takes the max (summing cumulative
    double-counts nested frames).
    """
    run = RuntimeRun(name=name, kind=KIND_CPROFILE, meta=dict(meta or {}))

    def in_scope(filename: str) -> bool:
        return source_prefix is None or filename.startswith(source_prefix)

    edge_counts: dict[tuple[str, str], int] = {}
    for (filename, lineno, _func), (_cc, nc, tt, ct, callers) in stats.items():
        if not in_scope(filename):
            continue
        node_id = resolve_node(index, filename, lineno)
        if node_id is None:
            run.unresolved += 1
            continue
        m = run.calls.setdefault(
            node_id, {"ncalls": 0, "tottime": 0.0, "cumtime": 0.0}
        )
        m["ncalls"] += nc
        m["tottime"] += tt
        m["cumtime"] = max(m["cumtime"], ct)

        for (cfile, cline, _cfunc), (_ccc, cnc, _ctt, _cct) in callers.items():
            if not in_scope(cfile):
                continue
            caller_id = resolve_node(index, cfile, cline)
            if caller_id is None or caller_id == node_id:
                continue  # unresolved caller, or a recursion self-loop
            key = (caller_id, node_id)
            edge_counts[key] = edge_counts.get(key, 0) + cnc

    run.edges = [(u, v, c) for (u, v), c in edge_counts.items()]
    return run


def ingest_cprofile(
    artifact: Path | str,
    graph: KnowledgeGraph | nx.MultiDiGraph,
    name: str,
    meta: dict[str, Any] | None = None,
    source_prefix: str | None = None,
) -> RuntimeRun:
    """Load a ``cProfile`` ``.pstats``/``.prof`` artifact and overlay it."""
    import pstats

    stats = pstats.Stats(str(artifact))
    return overlay_cprofile(
        stats.stats, build_node_index(graph), name,  # type: ignore[attr-defined]
        meta=meta, source_prefix=source_prefix,
    )


# ── coverage.py --branch backend ────────────────────────────────────


def overlay_coverage(
    cov_json: dict[str, Any],
    index: dict[str, list[tuple[int, int, str]]],
    name: str,
    meta: dict[str, Any] | None = None,
    source_prefix: str | None = None,
) -> RuntimeRun:
    """Join a ``coverage json`` (format 3, ``--branch``) report onto node IDs.

    Per-file ``executed_branches`` / ``missing_branches`` are ``[from, to]``
    arcs; an arc is attributed to the node whose ``[lineno, end_lineno]``
    contains its ``from`` line. A node with missing arcs and ≥2 branch arcs is
    a *partial-branch* suspect — a conditional not exercised both ways in this
    run, the runtime evidence behind the accidental-vs-essential distinction.
    """
    run = RuntimeRun(name=name, kind=KIND_COVERAGE, meta=dict(meta or {}))

    for filename, fdata in cov_json.get("files", {}).items():
        if source_prefix is not None and not filename.startswith(source_prefix):
            continue
        spans = index.get(filename)
        if not spans:
            continue
        exec_lines = set(fdata.get("executed_lines", []))
        miss_lines = set(fdata.get("missing_lines", []))
        exec_arcs = [tuple(a) for a in fdata.get("executed_branches", [])]
        miss_arcs = [tuple(a) for a in fdata.get("missing_branches", [])]

        for lineno, end, node_id in spans:
            lines_hit = sum(1 for ln in exec_lines if lineno <= ln <= end)
            lines_miss = sum(1 for ln in miss_lines if lineno <= ln <= end)
            hit_arcs = [a for a in exec_arcs if lineno <= a[0] <= end]
            missing = [a for a in miss_arcs if lineno <= a[0] <= end]
            total_arcs = len(hit_arcs) + len(missing)
            if lines_hit + lines_miss == 0 and total_arcs == 0:
                continue  # node not present in this coverage file's scope
            run.coverage[node_id] = {
                "lines_hit": lines_hit,
                "lines_total": lines_hit + lines_miss,
                "branches_hit": len(hit_arcs),
                "branches_total": total_arcs,
                "missing_arcs": [list(a) for a in missing],
            }
    return run


def ingest_coverage(
    artifact: Path | str,
    graph: KnowledgeGraph | nx.MultiDiGraph,
    name: str,
    meta: dict[str, Any] | None = None,
    source_prefix: str | None = None,
) -> RuntimeRun:
    """Load a ``coverage json`` artifact and overlay it."""
    cov_json = json.loads(Path(artifact).read_text())
    return overlay_coverage(
        cov_json, build_node_index(graph), name,
        meta=meta, source_prefix=source_prefix,
    )


# ── viztracer backend (temporal order) ──────────────────────────────

#: viztracer names a function event ``"funcname (/abs/path.py:LINENO)"``.
_VIZ_NAME = re.compile(r"\((?P<file>.+):(?P<line>\d+)\)\s*$")


def _parse_viztracer_name(name: str) -> tuple[str, int] | None:
    m = _VIZ_NAME.search(name)
    if not m:
        return None
    return m.group("file"), int(m.group("line"))


def overlay_viztracer(
    events: list[dict[str, Any]],
    index: dict[str, list[tuple[int, int, str]]],
    name: str,
    meta: dict[str, Any] | None = None,
    source_prefix: str | None = None,
) -> RuntimeRun:
    """Join viztracer ``traceEvents`` onto node IDs, keeping temporal order.

    The unique thing viztracer adds over cProfile is *order*: it timestamps
    every call, so the overlay is the observed execution sequence (mesh →
    discretize → sweep → iterate → result) rather than aggregate counts.
    Complete (``ph == "X"``) events carry ``ts`` (µs) and ``dur``; call-stack
    **depth** is reconstructed by interval nesting (an event whose span is
    contained in another is its child). Per node we keep the first entry time
    (ms from trace start), the event count, and the shallowest depth seen — so
    ``runtime_timeline`` can show just the high-level stages.

    Depth assumes the strict nesting a real tracer produces: a callee's
    ``[ts, ts+dur)`` lies inside its caller's, and distinct frames have
    distinct ``ts`` (µs-resolution). The ``(ts, -dur)`` sort puts a container
    before its content even at an equal ``ts``; frames are popped when closed
    (``end <= ts``). Pathological identical intervals (same ts AND dur) are
    degenerate and not produced by viztracer.
    """
    run = RuntimeRun(name=name, kind=KIND_VIZTRACER, meta=dict(meta or {}))
    calls = [
        e for e in events
        if e.get("ph") == "X" and "ts" in e and "name" in e
    ]
    if not calls:
        return run
    calls.sort(key=lambda e: (e["ts"], -e.get("dur", 0.0)))
    t0 = calls[0]["ts"]

    open_ends: list[float] = []  # stack of end-times of currently-open frames
    for e in calls:
        ts = e["ts"]
        end = ts + e.get("dur", 0.0)
        while open_ends and open_ends[-1] <= ts:
            open_ends.pop()
        depth = len(open_ends)
        open_ends.append(end)

        parsed = _parse_viztracer_name(e["name"])
        if parsed is None:
            continue
        filename, lineno = parsed
        if source_prefix is not None and not filename.startswith(source_prefix):
            continue
        node_id = resolve_node(index, filename, lineno)
        if node_id is None:
            run.unresolved += 1
            continue
        rel_ms = (ts - t0) / 1000.0
        slot = run.timeline.get(node_id)
        if slot is None:
            run.timeline[node_id] = {
                "first_ts": rel_ms, "count": 1, "min_depth": depth,
            }
        else:
            slot["count"] += 1
            slot["min_depth"] = min(slot["min_depth"], depth)
            slot["first_ts"] = min(slot["first_ts"], rel_ms)
    return run


def ingest_viztracer(
    artifact: Path | str,
    graph: KnowledgeGraph | nx.MultiDiGraph,
    name: str,
    meta: dict[str, Any] | None = None,
    source_prefix: str | None = None,
) -> RuntimeRun:
    """Load a viztracer JSON artifact (Chrome-trace format) and overlay it."""
    data = json.loads(Path(artifact).read_text())
    return overlay_viztracer(
        data.get("traceEvents", []), build_node_index(graph), name,
        meta=meta, source_prefix=source_prefix,
    )


# ── Sidecar store: _nexus/traces/<run>.json ─────────────────────────


class RuntimeStore:
    """The ``_nexus/traces/`` directory of ingested runs (one JSON each)."""

    def __init__(self, directory: Path | str) -> None:
        self.dir = Path(directory)

    def _path(self, name: str) -> Path:
        return self.dir / f"{name}.json"

    def write(self, run: RuntimeRun) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self._path(run.name)
        path.write_text(json.dumps(run.to_dict(), indent=2, default=str))
        return path

    def load(self, name: str) -> RuntimeRun | None:
        path = self._path(name)
        if not path.is_file():
            return None
        return RuntimeRun.from_dict(json.loads(path.read_text()))

    def list_runs(self) -> list[dict[str, Any]]:
        """Name + kind + meta of every stored run (no metric payloads)."""
        if not self.dir.is_dir():
            return []
        out = []
        for path in sorted(self.dir.glob("*.json")):
            try:
                data = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            nodes = (data.get("calls") or data.get("coverage")
                     or data.get("timeline") or {})
            out.append({
                "name": data.get("name", path.stem),
                "kind": data.get("kind", ""),
                "meta": data.get("meta", {}),
                "nodes": len(nodes),
                "edges": len(data.get("edges", [])),
            })
        return out

    def delete(self, name: str) -> bool:
        path = self._path(name)
        if path.is_file():
            path.unlink()
            return True
        return False
