"""Dynamic execution-flow overlay — runtime trace ingested onto the graph.

The static graph (``graph.db``) is *what can run*. A **runtime** overlay is
*what actually ran*: call counts, time, which edges fired, which polymorphic
implementation was reached, which branches were taken. It is a distinct graph
*species* that composes with the static graph **by join on node-ID** — it is
never written into ``graph.db`` (which is rebuilt on every ``sphinx-build``).
It lives in a sidecar, ``.nexus/traces/<run>.json``, and re-binds to the live
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
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import networkx as nx

# Aliased: five functions in this module bind a LOCAL `node_id` string,
# and a shadowed import is a landmine for the next editor. The alias is
# the verb `_mappings.node_id`'s own docstring uses.
from sphinxcontrib.nexus._mappings import node_id as spell_node_id
from sphinxcontrib.nexus.graph import KnowledgeGraph, NodeType
from sphinxcontrib.nexus.position import Definition, PositionIndex
from sphinxcontrib.nexus.workspace import canonical_path

# ``DECORATOR_WINDOW`` lived here until 2026-08-16. The window was a GUESS
# at where a decorated definition starts; the analyzer now records the
# answer (`decorator_lineno`), so the join is an exact match and the
# constant survives only as `position.DECORATOR_WINDOW`, the fallback for
# graphs built before that. ⚠ Its old note claimed the window absorbed the
# decorator stack "without false matches" — [M] 2026-08-16 it mis-bound 456
# of ORPHEUS's 3530 decorated definitions, always to the next sibling down.

KIND_CPROFILE = "cprofile"
KIND_COVERAGE = "coverage"
KIND_VIZTRACER = "viztracer"
KIND_PYTEST = "pytest"
KIND_MERGED = "merged"


# ── The join: (file, line) trace record → static node id ────────────


# ``build_node_index`` and ``resolve_node`` lived here until 2026-08-16.
# Both are now :class:`~sphinxcontrib.nexus.position.PositionIndex` — the
# join is the same concept the navigation verb ``GraphQuery.node_at``
# needed, and keeping two of them is what let them answer differently.


@dataclass
class JoinLedger:
    """Why a trace's records did or did not become node bindings.

    An ingest that binds nothing is indistinguishable from a workload that
    genuinely touched nothing indexed — unless it can say WHY each record
    was dropped. Without that, ``nodes: 0`` reads as a measurement, and
    every number derived from the overlay inherits an unverified
    denominator.

    The unit is one **lookup**. Backends ask different questions: coverage
    asks about a whole file at a time, cProfile and viztracer about a
    single ``(file, line)`` code object. So the counts are comparable
    within a run, not across kinds.
    """

    #: the lookup found a node (or, for coverage, an indexed file)
    bound: int = 0
    #: filtered out by ``source_prefixes`` — usually stdlib / third-party
    outside_scope: int = 0
    #: in scope, but the graph has no nodes for that file at all. The
    #: signature of a key-space mismatch when it is ~100% of lookups.
    unindexed_file: int = 0
    #: file is indexed, but no function/method span contains the line.
    #: By design this is lambdas, comprehensions and nested closures.
    no_enclosing_node: int = 0
    #: a coverage CONTEXT named no node — the per-test attribution's own
    #: recall gap. Deliberately NOT part of :attr:`considered`: every other
    #: field counts one ``(file, line)`` lookup, while this counts one
    #: context NAME. Folding two units into one denominator is the
    #: unverified-denominator defect this class exists to prevent, so
    #: :meth:`diagnosis` goes on reasoning about lookups alone.
    unknown_context: int = 0

    @property
    def considered(self) -> int:
        return (
            self.bound
            + self.outside_scope
            + self.unindexed_file
            + self.no_enclosing_node
        )

    def add(self, other: "JoinLedger") -> None:
        self.bound += other.bound
        self.outside_scope += other.outside_scope
        self.unindexed_file += other.unindexed_file
        self.no_enclosing_node += other.no_enclosing_node
        self.unknown_context += other.unknown_context

    def diagnosis(self) -> str | None:
        """The likely cause when an ingest bound nothing, or ``None``.

        Named separately from the raw counts because a caller that bound
        zero nodes needs to be told what to DO, and the three zero-yield
        shapes have different remedies.
        """
        if self.bound:
            return None
        if not self.considered:
            return "the artifact contained no trace records at all"
        if self.unindexed_file and not self.outside_scope:
            return (
                "every in-scope file was missing from the graph — the two "
                "sides are probably in different key spaces (coverage.py "
                "emits paths relative to where it ran, while the graph "
                "indexes absolute paths). Pass --root pointing at the "
                "directory the traced run used as its working directory."
            )
        if self.outside_scope and not self.unindexed_file:
            return (
                "every record was filtered out by --source-prefix. Note a "
                "profiled test suite produces tests -> package records, so "
                "either prefix ALONE drops one endpoint of every one of "
                "them; --source-prefix may be repeated."
            )
        if self.no_enclosing_node:
            return (
                "files were found but no record landed inside a "
                "function/method span — check the graph is current "
                "(rebuild) and that it was built from the same checkout "
                "the trace was captured on."
            )
        return "no record bound, and no single reason dominates"


class NodeBinder:
    """The ``(file, line) -> node-ID`` join: one key space, one ledger.

    Every backend performs this join, and before this class each did it
    its own way — cProfile through a local ``in_scope`` helper, coverage
    and viztracer through inline ``startswith`` — with three *different*
    accounting behaviours. Two counted unresolved records; coverage
    counted nothing at all, so a total join failure printed
    ``nodes: 0 / edges: 0 / unresolved: 0`` and exited 0.

    Two things are unified here because they are one concept:

    **The key space.** Node ``file_path`` is absolute; ``coverage json``
    emits keys relative to the directory it ran in; cProfile's
    ``co_filename`` and viztracer's event names are absolute. Comparing
    them raw drops every coverage file silently. Everything is
    absolutised against ``root`` on the way in, once, so the mismatch is
    unspellable rather than fixed in one backend.

    ⚠ ``root`` is the TRACE's tree — the directory the profiled run used
    — which is not the checkout the graph's stored paths are relative
    to. The two are usually the same and are not the same concept; the
    stored side is canonicalised by
    :class:`~sphinxcontrib.nexus.position.PositionIndex`, this side
    here, and they meet as absolutes.

    **Scope.** ``source_prefixes`` is a LIST, because no single prefix
    works: profiling a test suite yields ``tests -> package`` records, and
    either directory alone drops one endpoint of every one of them, while
    the repository root sweeps in the virtualenv. Containment is tested
    with :meth:`~pathlib.PurePath.is_relative_to`, not ``startswith`` — a
    string prefix matches ``/repo/orpheus_scratch`` against ``/repo/orpheus``.
    """

    def __init__(
        self,
        index: PositionIndex,
        *,
        source_prefixes: list[str] | None = None,
        root: Path | str | None = None,
    ) -> None:
        self.root = Path(root).resolve() if root is not None else Path.cwd()
        self.ledger = JoinLedger()
        self._cache: dict[str, str] = {}
        self._index = index
        self._prefixes = [
            Path(self._abs(p)) for p in (source_prefixes or [])
        ] or None

    def _abs(self, filename: str) -> str:
        """The trace's spelling as a comparable key, memoised.

        :func:`~sphinxcontrib.nexus.workspace.canonical_path` against
        the TRACE's root, which is not the workspace root and must not
        be confused with it: a ``coverage json`` key is relative to the
        directory the traced run used, while a stored ``file_path`` is
        relative to the checkout.  Same contract, two different trees —
        so the two sides canonicalise separately and meet as absolutes.

        Memoised because this runs once per trace record.
        """
        hit = self._cache.get(filename)
        if hit is None:
            hit = str(canonical_path(filename, self.root))
            self._cache[filename] = hit
        return hit

    def _in_scope(self, path: Path) -> bool:
        if self._prefixes is None:
            return True
        return any(path.is_relative_to(p) for p in self._prefixes)

    def definitions(self, filename: str) -> tuple[Definition, ...] | None:
        """Every indexed definition in one file, or ``None`` — coverage's
        unit.

        Records into the ledger, so a caller that skips the file still
        leaves a trace of WHY.
        """
        absolute = self._abs(filename)
        if not self._in_scope(Path(absolute)):
            self.ledger.outside_scope += 1
            return None
        defs = self._index.definitions_in(absolute)
        if defs is None:
            self.ledger.unindexed_file += 1
            return None
        self.ledger.bound += 1
        return defs

    def node(self, filename: str, lineno: int) -> str | None:
        """One ``(file, line)`` code object to a node — the tracers' unit."""
        absolute = self._abs(filename)
        if not self._in_scope(Path(absolute)):
            self.ledger.outside_scope += 1
            return None
        if not self._index.knows(absolute):
            self.ledger.unindexed_file += 1
            return None
        node_id = self._index.defined_at(absolute, lineno)
        if node_id is None:
            self.ledger.no_enclosing_node += 1
            return None
        self.ledger.bound += 1
        return node_id

    def peek(self, filename: str, lineno: int) -> str | None:
        """:meth:`node` without touching the ledger.

        For lookups that are a second view of a record already counted —
        cProfile's ``callers`` map, where each entry re-states a call whose
        callee is its own stats row. Counting both would double the
        denominator that :meth:`JoinLedger.diagnosis` reasons about.
        """
        absolute = self._abs(filename)
        if not self._in_scope(Path(absolute)):
            return None
        return self._index.defined_at(absolute, lineno)


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
    #: node_id -> {marker name: value}  (pytest) — markers as pytest
    #: RESOLVED them at collection, which is a different and larger set
    #: than the decorators an AST walk can see. A test node here may
    #: carry several pytest ids (parametrisation); the marker set is
    #: unioned across them, and ``pytest_ids`` keeps the originals so a
    #: caller can still run one case.
    markers: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: node_id -> [pytest node id, …]  (pytest) — the runnable spelling
    pytest_ids: dict[str, list[str]] = field(default_factory=dict)
    #: node_id -> [test node id, …]  (coverage, with contexts) — which
    #: tests EXECUTED this node. Every other family answers "was this
    #: reached by the run"; this one answers "by which test", which is
    #: the only evidence that can falsify a coverage CLAIM.
    #:
    #: ⚠ It reaches *executed*, never *asserted*. A test that imports a
    #: module and never looks at it is recorded here exactly like the one
    #: that pins its every branch — no edge quality separates those
    #: rungs, and only mutation can. Read it as a NECESSARY condition on
    #: a catcher (a test that never ran the line cannot catch a defect
    #: in it), not as a sufficient one.
    exercised_by: dict[str, list[str]] = field(default_factory=dict)
    #: why records did or did not bind — the ingest's own audit trail
    ledger: JoinLedger = field(default_factory=JoinLedger)

    @property
    def unresolved(self) -> int:
        """In-scope records that found no node — the recall-gap audit.

        Derived, not stored. It was a bare field until the ledger existed,
        and on its own it could not distinguish "the trace reached files
        the graph knows, but landed between functions" from "the two sides
        were never in the same key space" — the second reads as a clean
        zero. Kept as a property because it is the one number the CLI and
        the MCP server have always reported.
        """
        return self.ledger.no_enclosing_node

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RuntimeRun":
        raw = data.get("ledger")
        if raw is None:
            # A sidecar written before the ledger existed. Its lone
            # `unresolved` count is precisely today's no_enclosing_node;
            # the other three reasons were never measured, and must stay
            # ZERO rather than be guessed — an invented denominator is
            # exactly the defect the ledger was added to prevent.
            ledger = JoinLedger(no_enclosing_node=data.get("unresolved", 0))
        else:
            ledger = JoinLedger(**raw)
        return cls(
            name=data["name"],
            kind=data["kind"],
            meta=data.get("meta", {}),
            calls=data.get("calls", {}),
            edges=[tuple(e) for e in data.get("edges", [])],
            coverage=data.get("coverage", {}),
            timeline=data.get("timeline", {}),
            markers=data.get("markers", {}),
            pytest_ids=data.get("pytest_ids", {}),
            exercised_by=data.get("exercised_by", {}),
            ledger=ledger,
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
    * **exercised_by** — a plain set union: a node exercised by test ``T`` in
      ANY run is exercised by ``T``. Exact, unlike ``lines_hit`` above, because
      what is stored is the test SET rather than a count of it.
    * **timeline** — NOT merged (timestamps are per-run and incomparable);
      use a single run for ``runtime_timeline``.

    A single-run list returns that run unchanged.
    """
    if len(runs) == 1:
        return runs[0]
    if not runs:
        return RuntimeRun(name=name, kind=KIND_MERGED)
    # `merged_from` names WHICH runs; `command` says HOW each was
    # captured, and that half used to be dropped. It is not decoration:
    # a run taken under `-m "not slow"` or a parameter subset can make a
    # genuine dependence look unexercised, so a consumer that reports
    # "no test executed this" needs the invocation to qualify it. Losing
    # it precisely when captures are UNIONED — the normal case for a
    # whole-suite ledger — is the wrong way round.
    notes = [
        f"{r.name}: {(r.meta or {})['command']}"
        for r in runs if (r.meta or {}).get("command")
    ]
    merged = RuntimeRun(name=name, kind=KIND_MERGED,
                        meta={"merged_from": [r.name for r in runs]})
    if notes:
        merged.meta["command"] = " | ".join(notes)

    for run in runs:
        for node_id, m in run.calls.items():
            agg = merged.calls.setdefault(
                node_id, {"ncalls": 0, "tottime": 0.0, "cumtime": 0.0})
            agg["ncalls"] += m["ncalls"]
            agg["tottime"] += m["tottime"]
            agg["cumtime"] = max(agg["cumtime"], m["cumtime"])
        merged.ledger.add(run.ledger)

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

    exercised: dict[str, set[str]] = {}
    for run in runs:
        for node_id, tests in run.exercised_by.items():
            exercised.setdefault(node_id, set()).update(tests)
    merged.exercised_by = {n: sorted(t) for n, t in exercised.items()}
    return merged


#: Overlay family → the attribute carrying it, and the ingest kinds that
#: can produce it. A run is a bag of orthogonal overlays: a ``cprofile``
#: ingest fills ``calls``/``edges``, a ``coverage`` ingest fills
#: ``coverage``, a ``viztracer`` ingest fills ``timeline``.
#:
#: Lives here rather than in the MCP server because it is a property of
#: RUNS, and both front ends need it: the server refused a wrong-kind run
#: while the CLI answered as though nothing were covered — the same
#: ``lessons-L56`` confusion the refusal exists to remove, surviving on
#: the surface that did not share the author.
RUNTIME_FAMILIES: dict[str, tuple[str, tuple[str, ...]]] = {
    "calls": ("calls", ("cprofile",)),
    "edges": ("edges", ("cprofile",)),
    "coverage": ("coverage", ("coverage",)),
    "timeline": ("timeline", ("viztracer",)),
    "markers": ("markers", ("pytest",)),
    # A coverage run carries this only when the capture asked for
    # contexts, which is why the "runs that have it" list is filtered on
    # the payload rather than on the kind.
    "exercised_by": ("exercised_by", ("coverage",)),
}


def require_family(
    run: "RuntimeRun",
    family: str,
    view: str,
    store: "RuntimeStore | None" = None,
) -> None:
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
    attribute, kinds = RUNTIME_FAMILIES[family]
    if getattr(run, attribute, None):
        return
    # The naming of alternatives is a bonus; the REFUSAL is the point.
    # Letting a store lookup fail here would replace a precise "wrong
    # run" message with an AttributeError — turning the one answer that
    # explains itself into the one that explains nothing.
    try:
        available = store.list_runs() if store is not None else []
    except Exception:                    # no workspace, no store, no matter
        available = []
    # Filtered on the FAMILY the run actually carries, not on its kind:
    # two `coverage` runs differ on whether contexts were captured, and
    # naming one that cannot answer re-creates the very confusion this
    # refusal exists to remove. `families` is absent from sidecars listed
    # by an older store, so fall back to the kind rather than to nothing.
    usable = [
        r["name"] for r in available
        if (family in r["families"] if "families" in r
            else r.get("kind") in kinds)
    ] or [f"(none — capture one with kind={kinds[0]!r} and runtime_ingest)"]
    raise ValueError(
        f"run {run.name!r} was ingested as kind={run.kind!r} and carries no "
        f"{family!r} data, so {view} has nothing to read — this is not an "
        f"empty result, it is the wrong run. {family!r} comes from "
        f"{' or '.join(repr(k) for k in kinds)}; runs that have it: {usable}"
    )


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
    index: PositionIndex,
    name: str,
    meta: dict[str, Any] | None = None,
    source_prefixes: list[str] | None = None,
    root: Path | str | None = None,
) -> RuntimeRun:
    """Join a ``pstats``-format stats dict onto node IDs.

    ``stats`` is :attr:`pstats.Stats.stats`:
    ``{(file, line, func): (cc, nc, tt, ct, callers)}`` where ``callers`` is
    ``{(file, line, func): (cc, nc, tt, ct)}``. ``nc`` is the (recursion-
    inclusive) call count, ``tt`` self time, ``ct`` cumulative time.

    Records outside ``source_prefixes`` (when given) are dropped —
    stdlib/3rd-party frames collapse away, leaving the project's own stage
    DAG. Metrics aggregate **by node-ID** (a node may own several code
    objects): ncalls and tottime sum (both additive), cumtime takes the max
    (summing cumulative double-counts nested frames).

    See :class:`NodeBinder` for the key space and why the scope filter is a
    LIST. ``root`` anchors relative paths; cProfile's ``co_filename`` is
    absolute, so it matters here only for the prefixes.
    """
    run = RuntimeRun(name=name, kind=KIND_CPROFILE, meta=dict(meta or {}))
    binder = NodeBinder(index, source_prefixes=source_prefixes, root=root)

    edge_counts: dict[tuple[str, str], int] = {}
    for (filename, lineno, _func), (_cc, nc, tt, ct, callers) in stats.items():
        node_id = binder.node(filename, lineno)
        if node_id is None:
            continue
        m = run.calls.setdefault(
            node_id, {"ncalls": 0, "tottime": 0.0, "cumtime": 0.0}
        )
        m["ncalls"] += nc
        m["tottime"] += tt
        m["cumtime"] = max(m["cumtime"], ct)

        for (cfile, cline, _cfunc), (_ccc, cnc, _ctt, _cct) in callers.items():
            # A caller lookup is not a record of its own — it is a second
            # view of an edge whose callee already counted. Binding it
            # through the ledger would double-count every call site and
            # inflate the denominator the diagnosis reasons about.
            caller_id = binder.peek(cfile, cline)
            if caller_id is None or caller_id == node_id:
                continue  # out of scope, unresolved, or a recursion self-loop
            key = (caller_id, node_id)
            edge_counts[key] = edge_counts.get(key, 0) + cnc

    run.edges = [(u, v, c) for (u, v), c in edge_counts.items()]
    run.ledger = binder.ledger
    return run


def ingest_cprofile(
    artifact: Path | str,
    graph: KnowledgeGraph | nx.MultiDiGraph,
    name: str,
    meta: dict[str, Any] | None = None,
    source_prefixes: list[str] | None = None,
    root: Path | str | None = None,
) -> RuntimeRun:
    """Load a ``cProfile`` ``.pstats``/``.prof`` artifact and overlay it."""
    import pstats

    stats = pstats.Stats(str(artifact))
    return overlay_cprofile(
        stats.stats, PositionIndex(graph), name,  # type: ignore[attr-defined]
        meta=meta, source_prefixes=source_prefixes, root=root,
    )


# ── coverage.py --branch backend ────────────────────────────────────

#: A pytest node id — ``tests/g/test_x.py::TestFoo::test_bar[case]`` — as
#: emitted by ``pytest-cov --cov-context=test``. The other capture route,
#: coverage.py's own ``dynamic_context = test_function``, already emits the
#: dotted qualname, so only this one needs normalising.
_PYTEST_NODEID = re.compile(r"^(?P<file>[^:]+\.py)::(?P<rest>.+)$")


def _context_qualname(context: str) -> str:
    """One spelling for a coverage context, whichever route captured it.

    Two tools stamp contexts and they disagree. coverage.py's
    ``dynamic_context = test_function`` writes the dotted qualname
    (``tests.geometry.test_bc.TestFoo.test_bar``); ``pytest-cov
    --cov-context=test`` writes the pytest node id
    (``tests/geometry/test_bc.py::TestFoo::test_bar``). Normalising both
    to the qualname HERE, once, is Pattern 7 — a consumer that did it
    per call site would be one convention drift per consumer.

    Parametrisation is dropped: ``test_bar[case-a]`` and ``test_bar[b]``
    are two pytest ids and ONE graph node, the same many-to-one
    :func:`overlay_pytest` documents. (coverage.py's own route already
    collapses them — `[M]` 0 of 426 contexts on an ORPHEUS slice carried
    a bracket.)
    """
    # pytest-cov appends the PHASE — `…::test_bar|setup`, `|run`,
    # `|teardown` — so the same test arrives as up to three contexts.
    # They are one node; the phase is dropped rather than kept, because
    # nothing downstream asks "did this run in setup".
    context = context.split("|", 1)[0]
    m = _PYTEST_NODEID.match(context)
    if m:
        module = m.group("file")[: -len(".py")].replace("/", ".")
        context = f"{module}.{m.group('rest').replace('::', '.')}"
    return context.split("[", 1)[0]


def _context_node(context: str, index: PositionIndex) -> str | None:
    """The test node a coverage context names, or ``None``.

    A context is a NAME, not a ``(file, line)`` record, so it cannot go
    through :meth:`NodeBinder.node` — but the name it carries is exactly a
    node id's final segment, so the join is a lookup rather than a parse.
    The id is spelled with the canonical
    :func:`~sphinxcontrib.nexus._mappings.node_id` (the grammar has one
    author) and then CHECKED against the index, so a spelling that does
    not exist returns ``None`` and is ledgered — this can fail to
    resolve, it cannot resolve wrongly.
    """
    qualname = _context_qualname(context)
    for node_type in (NodeType.METHOD, NodeType.FUNCTION):
        candidate = spell_node_id("py", node_type, qualname)
        if index.knows_node(candidate):
            return candidate
    return None


def overlay_coverage(
    cov_json: dict[str, Any],
    index: PositionIndex,
    name: str,
    meta: dict[str, Any] | None = None,
    source_prefixes: list[str] | None = None,
    root: Path | str | None = None,
) -> RuntimeRun:
    """Join a ``coverage json`` (format 3) report onto node IDs.

    Produced by ``coverage run --branch`` followed by ``coverage json`` —
    the ``--branch`` belongs to the RUN, not the report, and ``coverage
    json --branch`` is rejected outright.

    Per-file ``executed_branches`` / ``missing_branches`` are ``[from, to]``
    arcs; an arc is attributed to the node whose ``[lineno, end_lineno]``
    contains its ``from`` line. A node with missing arcs and ≥2 branch arcs is
    a *partial-branch* suspect — a conditional not exercised both ways in this
    run, the runtime evidence behind the accidental-vs-essential distinction.

    ⚠ ``coverage json`` keys its files **relative to the directory it ran
    in**, while the graph indexes absolute paths, so the two sides do not
    compare raw — every file drops and the run binds nothing. ``root`` is
    what closes that gap, and it must be supplied by the caller: the
    report's own ``meta`` carries only ``format``/``version``/
    ``timestamp``/``branch_coverage``/``show_contexts``, and records the
    rundir **nowhere**.

    **Per-test attribution.** When the report carries ``contexts`` — the
    capture asked for them — each is resolved to the test node that
    executed the line and the result lands in
    :attr:`RuntimeRun.exercised_by`. That is the only family here that can
    falsify a coverage CLAIM: every other one says a node was reached,
    this one says by whom. Capture it with ``dynamic_context =
    test_function`` in the coverage config (there is no CLI flag) and
    ``coverage json --show-contexts``; ``pytest-cov --cov-context=test``
    is normalised too. A context naming no node is counted in
    :attr:`JoinLedger.unknown_context` rather than dropped, so a capture
    whose spelling this does not understand reports a number instead of
    an empty family.

    ⚠ **Contexts make the REPORT enormous, not the overlay.** `[M]`
    2026-08-18 on ORPHEUS ``tests/geometry`` (19 files, 792 tests): the
    ``--show-contexts`` JSON is **265 MB**, and the ``exercised_by`` it
    reduces to is **1.44 MB** — 184×. So the cost is transient and lands
    on whoever runs the capture; slice the suite rather than expecting one
    whole-suite report to be manageable.
    """
    run = RuntimeRun(name=name, kind=KIND_COVERAGE, meta=dict(meta or {}))
    binder = NodeBinder(index, source_prefixes=source_prefixes, root=root)

    # Resolve every distinct context ONCE, before the file walk. Contexts
    # repeat on thousands of lines and across files, so resolving them in
    # place would re-ask the same question per line AND count the same
    # miss once per line — an inflated denominator in the one field whose
    # job is to be an honest one.
    test_node: dict[str, str | None] = {}
    for fdata in cov_json.get("files", {}).values():
        for contexts in (fdata.get("contexts") or {}).values():
            for context in contexts:
                # "" is coverage's context for a line executed outside any
                # test — import time, collection, a session fixture. It
                # names no test and is not a failure to resolve one.
                if context and context not in test_node:
                    test_node[context] = _context_node(context, index)
    binder.ledger.unknown_context = sum(1 for v in test_node.values() if v is None)

    for filename, fdata in cov_json.get("files", {}).items():
        defs = binder.definitions(filename)
        if defs is None:
            continue
        exec_lines = set(fdata.get("executed_lines", []))
        miss_lines = set(fdata.get("missing_lines", []))
        exec_arcs = [tuple(a) for a in fdata.get("executed_branches", [])]
        miss_arcs = [tuple(a) for a in fdata.get("missing_branches", [])]
        ctx_lines = [
            (int(ln), contexts)
            for ln, contexts in (fdata.get("contexts") or {}).items()
        ]

        for d in defs:
            # The BODY range, not the decorator extent: a decorator line
            # executes at import time and coverage attributes it to the
            # module, not to the function it decorates.
            lineno, end = d.def_line, d.end_line
            node_id = d.node_id
            lines_hit = sum(1 for ln in exec_lines if lineno <= ln <= end)
            lines_miss = sum(1 for ln in miss_lines if lineno <= ln <= end)
            hit_arcs = [a for a in exec_arcs if lineno <= a[0] <= end]
            missing = [a for a in miss_arcs if lineno <= a[0] <= end]
            total_arcs = len(hit_arcs) + len(missing)
            # Attribution is recorded BEFORE the coverage guard below,
            # because the two answer different questions. `# pragma: no
            # cover` puts a line in `excluded_lines` — removed from
            # coverage's numerator AND denominator — while coverage goes
            # on stamping contexts on it, since it did run. Scoring and
            # dependence are not the same fact: a pragma says "do not
            # grade this", never "this did not execute", and for "which
            # tests re-run if I change this" the execution IS the answer.
            # `[M]` 2026-08-18: gating on the guard drops exactly 4
            # ORPHEUS nodes on the tests/geometry slice, every one a
            # pragma'd guard — `DiscreteMeasure.__post_init__` is run by
            # **131** tests and would have reported none.
            exercisers = {
                resolved
                for ln, contexts in ctx_lines
                if lineno <= ln <= end
                for resolved in (test_node.get(c) for c in contexts)
                if resolved is not None
            }
            if exercisers:
                run.exercised_by[node_id] = sorted(exercisers)
            if lines_hit + lines_miss == 0 and total_arcs == 0:
                continue  # node not present in this coverage file's scope
            run.coverage[node_id] = {
                "lines_hit": lines_hit,
                "lines_total": lines_hit + lines_miss,
                "branches_hit": len(hit_arcs),
                "branches_total": total_arcs,
                "missing_arcs": [list(a) for a in missing],
            }
    run.ledger = binder.ledger
    return run


def ingest_coverage(
    artifact: Path | str,
    graph: KnowledgeGraph | nx.MultiDiGraph,
    name: str,
    meta: dict[str, Any] | None = None,
    source_prefixes: list[str] | None = None,
    root: Path | str | None = None,
) -> RuntimeRun:
    """Load a ``coverage json`` artifact and overlay it."""
    cov_json = json.loads(Path(artifact).read_text())
    return overlay_coverage(
        cov_json, PositionIndex(graph), name,
        meta=meta, source_prefixes=source_prefixes, root=root,
    )


# ── pytest backend (markers as COLLECTION resolved them) ────────────


def overlay_pytest(
    manifest: dict[str, Any],
    index: PositionIndex,
    name: str,
    meta: dict[str, Any] | None = None,
    source_prefixes: list[str] | None = None,
    root: Path | str | None = None,
) -> RuntimeRun:
    """Join a collection manifest onto node IDs.

    Produced by ``pytest --collect-only`` under
    :mod:`sphinxcontrib.nexus.pytest_manifest`. It answers a question an
    AST walk cannot: what markers does a test ACTUALLY carry, after
    module-level ``pytestmark``, class marks, and whatever a project's
    ``conftest.py`` decides during collection.

    `[M]` on ORPHEUS the two answers are not close. The AST path finds
    four hard-coded marker names and reports **0** nodes for
    ``foundation``, ``cap``, ``regression`` and ``sentinel``; the
    manifest resolves **6899 / 2889 / 111 / 39** — and ``regression`` is
    the one that project's re-baseline adjudication turns on. A new
    marker used to cost a nexus release; here it costs nothing, because
    nothing is enumerated.

    ⚠ Parametrisation makes this many-to-one: ``test_foo[a]`` and
    ``test_foo[b]`` are two pytest ids and ONE graph node. Markers are
    unioned across them — a parametrised case that carries ``slow`` on
    one id makes the node ``slow``, which is the conservative reading
    and the one a scheduler wants.
    """
    run = RuntimeRun(name=name, kind=KIND_PYTEST, meta=dict(meta or {}))
    binder = NodeBinder(index, source_prefixes=source_prefixes, root=root)

    for record in manifest.get("tests", []):
        filename = record.get("file")
        lineno = record.get("lineno")
        if not filename or not lineno:
            continue
        node_id = binder.node(filename, int(lineno))
        if node_id is None:
            continue
        marks = run.markers.setdefault(node_id, {})
        for key, value in (record.get("markers") or {}).items():
            if key not in marks:
                marks[key] = value
        ids = run.pytest_ids.setdefault(node_id, [])
        nodeid = record.get("nodeid")
        if nodeid and nodeid not in ids:
            ids.append(nodeid)

    run.ledger = binder.ledger
    return run


def ingest_pytest(
    artifact: Path | str,
    graph: KnowledgeGraph | nx.MultiDiGraph,
    name: str,
    meta: dict[str, Any] | None = None,
    source_prefixes: list[str] | None = None,
    root: Path | str | None = None,
) -> RuntimeRun:
    """Load a collection manifest and overlay it."""
    manifest = json.loads(Path(artifact).read_text())
    # The manifest records the rootdir pytest used, so unlike a coverage
    # report it can say where its relative paths came from. Honour an
    # explicit `root` (a caller who moved the artifact knows better),
    # then fall back to what the capture recorded.
    return overlay_pytest(
        manifest, PositionIndex(graph), name,
        meta=meta, source_prefixes=source_prefixes,
        root=root or manifest.get("rootdir"),
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
    index: PositionIndex,
    name: str,
    meta: dict[str, Any] | None = None,
    source_prefixes: list[str] | None = None,
    root: Path | str | None = None,
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
    binder = NodeBinder(index, source_prefixes=source_prefixes, root=root)
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
        node_id = binder.node(filename, lineno)
        if node_id is None:
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
    run.ledger = binder.ledger
    return run


def ingest_viztracer(
    artifact: Path | str,
    graph: KnowledgeGraph | nx.MultiDiGraph,
    name: str,
    meta: dict[str, Any] | None = None,
    source_prefixes: list[str] | None = None,
    root: Path | str | None = None,
) -> RuntimeRun:
    """Load a viztracer JSON artifact (Chrome-trace format) and overlay it."""
    data = json.loads(Path(artifact).read_text())
    return overlay_viztracer(
        data.get("traceEvents", []), PositionIndex(graph), name,
        meta=meta, source_prefixes=source_prefixes, root=root,
    )


# ── Sidecar store: .nexus/traces/<run>.json ─────────────────────────


class RuntimeStore:
    """The ``.nexus/traces/`` directory of ingested runs (one JSON each)."""

    def __init__(self, directory: Path | str) -> None:
        self.dir = Path(directory)

    @classmethod
    def beside(cls, db_path: Path | str) -> "RuntimeStore":
        """The store belonging to the graph at ``db_path``.

        Every surface that has a database has a store, and this is the one
        place that says how to get from one to the other — the CLI and the
        MCP server both derive it here rather than each spelling
        ``db.parent / "traces"`` for itself.
        """
        from sphinxcontrib.nexus.project import TRACES_DIR_NAME

        return cls(Path(db_path).parent / TRACES_DIR_NAME)

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

    #: Metric families a listing reports presence of. `kind` records
    #: provenance and does NOT determine these — `merge_runs` produces a
    #: run carrying several, and a `coverage` run has `exercised_by` only
    #: when the capture asked for contexts. So "which runs can answer
    #: this?" must be read off the payload, never inferred from the kind.
    FAMILIES = ("calls", "edges", "coverage", "timeline", "markers",
                "exercised_by")

    def list_runs(self) -> list[dict[str, Any]]:
        """Name + kind + meta + which FAMILIES each stored run carries.

        ``families`` exists because kind is provenance, not capability:
        two runs both ``kind="coverage"`` differ on whether contexts were
        captured, and a caller told only the kind would be pointed at a
        run that cannot answer — the same "nothing found" vs "wrong
        place" confusion (``lessons-L56``) that
        :func:`~sphinxcontrib.nexus.server._require_family` exists to
        remove, one level down.
        """
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
                "families": [f for f in self.FAMILIES if data.get(f)],
            })
        return out

    def delete(self, name: str) -> bool:
        path = self._path(name)
        if path.is_file():
            path.unlink()
            return True
        return False
