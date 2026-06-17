# Issue #26 — dynamic execution-flow overlay: spike findings

**Status:** spike complete + **Phase 1+2+3 BUILT in 0.15.0** (2026-06-17) — the
cProfile, coverage, AND viztracer backends; the node-ID sidecar; the `runtime_*`
overlay queries; multi-run union (`merge_runs`); the accessor edge-classifier
(`substantive_only`); and `runtime_timeline`. This doc is the originating
exploration record; see `CHANGELOG.md` 0.15.0 and
`sphinxcontrib/nexus/runtime.py` for the shipped API.
**Substrate:** ORPHEUS SN solver (`solve_sn_fixed_source`, sphere reflective, GL-8,
12 cells, Krylov inner) traced under `cProfile`, joined against the live ORPHEUS
static graph (`graph.db`: 12,927 nodes, 30,068 CALLS edges, 6,005 fn/method nodes).
Spike script: `scratch/nexus26_trace_spike.py` in the ORPHEUS worktree.

## The make-or-break question

Can a runtime trace be **joined onto static node IDs** reliably? If the join is
lossy, the whole overlay is untrustworthy. (Same risk shape as the #23 fingerprint
spike.)

## Verdict: the join works. 97%.

Join key = `(file_path, lineno)`. Both the static graph (every fn/method node carries
absolute `file_path` + `lineno`/`end_lineno`) and `cProfile` (`co_filename`,
`co_firstlineno`) speak it.

- **One real gotcha:** `cProfile`'s `co_firstlineno` points at the **first decorator
  line**; AST's `node.lineno` is the **`def` line**. A naive `[lineno, end_lineno]`
  range check drops *every decorated function/property* (off-by-the-decorator-stack).
  Fix = widen the effective start down by a decorator window (`lineno - K`); the lines
  above a `def` are only decorators/blanks, so no false matches. This lifted the node
  join **68% → 97%**.
- The residual 3% unmapped is **by design**: `<lambda>`s and nested closures named `_`,
  which the AST analyzer attributes to the enclosing function (no separate node). Not a
  defect — the correct behavior.

## What the overlay reveals (4 of the issue's 5 capabilities, from cProfile alone)

1. **Dispatch-gap recovery (quantifies #16).** Only **27%** of distinct dynamic
   call edges matched a static CALLS edge; **193 were dynamic-only**. Headline:
   `_OneDimScanWalk._apply_walk` has **zero** static out-edges yet dispatches to
   `DiamondDifference.outgoing_face_from_average` and
   `MorelMontryAngularSweep.cell_contribution` **×10,992 each** — pure
   `self.scheme.method()` annotation-mediated dispatch the static resolver can't see.
   The trace recovers it. *This is #26's core value, exactly as the issue predicted.*
2. **Which polymorphic impl actually ran.** Static says "any `DiscretizationScheme`";
   the trace says **DiamondDifference + MorelMontryAngularSweep** for this run.
3. **Hot path / dynamic stage DAG.** cumtime ranking surfaces the true dominant chain:
   `solve_sn_fixed_source → _solve_fixed_source_krylov → KrylovAcceleration.solve →
   OperatorSum.apply → StreamingOperator.apply → CumprodScan.loss_action →
   _OneDimScanWalk._apply_walk`. This is the *observed* dominant path — strictly better
   than `processes`' static "highest-out-degree" heuristic for traced runs.
4. **Iteration-count signal.** `face_labels ×22,946`, `SNMesh.spatial_shape ×11,473`,
   `SNMesh.ng ×11,472`, `cell_balance_for_streaming ×10,992`. Falls out for free as an
   actionable ORPHEUS perf finding: hot **properties recomputed 11k+ times per solve**
   = a caching opportunity (file as an ORPHEUS `module:sn` perf issue).

The 5th capability — **branch-taken / accidental-vs-essential** (a discrimination
always taken one way across the production run = missing-type suspect, the dynamic
counterpart of #24 `discriminates_on`) — needs `coverage.py --branch`, not cProfile.
Strong candidate for a 2nd phase; ties directly to the static-smell family.

## Weak signal (honest)

- **Single-run dead edges:** 4 of 74 static CALLS edges among traced nodes didn't fire.
  Mechanically works, but "dead in ONE run" ≠ dead code. Becomes meaningful only by
  **unioning N canonical runs** → a multi-run aggregation design axis.
- **Edge overlay is property-dispatch-heavy.** Much of the 193 dynamic-only is
  `accessor → accessor`. It genuinely quantifies #16, but for *architectural* signal an
  edge classifier (accessor vs substantive call) would surface scheme-polymorphism
  above the property noise. Open design decision.

## Proposed architecture — sidecar joined at query time

**Hard constraint:** `graph.db` is rebuilt on every `sphinx-build`. Dynamic data
**must not** live in it. It lives in a **separate sidecar keyed by node-ID**, joined
against the live graph at query time. Because node IDs are stable across rebuilds, the
overlay re-binds automatically after each build.

- **Capture (consumer-side, NOT Nexus):** the project runs a canonical workload under a
  tracer and hands Nexus the artifact. Tiers: `cProfile`/`pstats` (counts+time+edges —
  spiked ✅), `coverage.py --branch` (branches — phase 2), `viztracer` (temporal order —
  maybe-later).
- **Ingest (Nexus):** `nexus trace ingest <artifact> --kind {cprofile,coverage} --run
  <name>` → parse → join via the `(file_path, lineno)` + decorator-window rule → write
  `_nexus/traces/<run>.db` (per-node {ncalls, tottime, cumtime}; per-edge {count};
  per-run provenance: commit, command). **Aggregate by node-ID** (one node can own
  several code objects).
- **Overlay queries (Nexus):** `trace_hotpath(run)`, `trace_dead_edges(run[, runs])`,
  `trace_dispatch(run, node)` (fills #16), `trace_counts(run, node)`,
  `trace_branches(run, node)` (coverage backend; the #24 dynamic counterpart). Each is a
  GraphQuery method → `@nexus_tool` → CLI subcommand, the established 3-layer pattern.

The dynamic graph is a **distinct species** that composes with the static graph by join
and never mutates it — the clean complement to the static-smell family
(`native_place`/`twin_paths`/`discriminates_on`).

## Suggested phasing

- **Phase 1 (tractable first PR):** cProfile backend — ingest + sidecar + 4 overlay
  queries (hotpath, counts, dispatch, single-run dead-edges). Delivers 4/5 capabilities
  and the #16 recovery.
- **Phase 2:** coverage `--branch` backend → `trace_branches` → accidental-vs-essential
  missing-type detector (the #24 tie-in). Highest *novel* architectural value.
- **Phase 3 (optional):** multi-run union (real dead-code), viztracer temporal order,
  edge classifier to lift scheme-polymorphism above accessor noise.
