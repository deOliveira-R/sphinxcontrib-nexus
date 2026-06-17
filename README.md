# sphinxcontrib-nexus

A unified code + documentation knowledge graph extracted from Sphinx builds and Python AST analysis. Queryable via MCP, CLI, and Python API.

**What makes it unique:** Nexus is the only tool that puts code structure (call graphs, imports, inheritance, type annotations) and documentation structure (equations, cross-references, citations, theory pages) in the same graph. This enables queries that are impossible with code-only or doc-only tools — like tracing from a literature citation through an equation to the function that implements it.

## Quick Start

```bash
pip install sphinxcontrib-nexus
```

### As a Sphinx Extension

Add to your `docs/conf.py`:

```python
extensions = ['sphinxcontrib.nexus']
```

After `sphinx-build`, find the graph at `<outdir>/_nexus/graph.db` (SQLite) and `<outdir>/_nexus/graph.json`.

### Standalone AST Analysis (no Sphinx needed)

```bash
nexus analyze src/ --db graph.db
```

### MCP Server (for Claude Code / AI agents)

```bash
nexus serve --db graph.db --project-root /path/to/project
```

### Install Skills + MCP Server for Claude Code

```bash
nexus setup           # project-level: .mcp.json + .claude/skills/
nexus setup --global  # user-level: ~/.claude.json + ~/.claude/skills/ (all projects)
```

### Ingest a Paper

```bash
nexus ingest paper.pdf --db graph.db     # extracts concepts, equations, citations via LLM
```

### Interactive Graph Visualizer

```bash
nexus visualize --db graph.db            # opens HTML graph explorer in browser
```

## Configuration

| Config value | Default | Description |
|---|---|---|
| `nexus_output` | `_nexus` | Output directory relative to build output |
| `nexus_ast_analyze` | `True` | Run AST analysis during Sphinx build |
| `nexus_max_viz_nodes` | `300` | Max nodes in auto-generated graph.html |
| `nexus_extra_source_dirs` | `[]` | Extra directories (relative to project root) to analyze in addition to autodetected source roots. Useful for out-of-tree test suites or separate module roots. |
| `nexus_analyze_tests` | `True` | Whether Python test modules are merged into the graph. Set to `False` to exclude them entirely (e.g. to keep coverage numbers focused on production code). |
| `nexus_test_patterns` | `["tests/*", "*/tests/*", "test_*.py", "*/test_*.py"]` | Glob patterns (POSIX, evaluated with `fnmatch` against the path relative to each source dir) identifying Python test modules. Used both by `nexus_analyze_tests=False` exclusion and by the `is_test` flag on function nodes — a function is marked as a test only when its name follows the `test`/`test_*` convention **and** it lives in a file matching one of these patterns. |
| `nexus_source_exclude_patterns` | `[]` | Extra glob patterns (POSIX, same `fnmatch` semantics as `nexus_test_patterns`) listing directories or files to exclude from AST analysis entirely. Use this for tutorial scripts, vendored copies, legacy modules, or any other source that lives in the project tree but should not contribute nodes or edges to the graph. Patterns are applied in addition to the always-on base exclusions (`docs/*`, `.venv/*`, `__pycache__/*`) and to `nexus_test_patterns` when `nexus_analyze_tests=False`. |
| `nexus_infer_implements` | `True` | Whether to run the token-intersection heuristic in `merge._infer_implements`. Set `False` when explicit registry / marker / directive coverage is complete and the heuristic's inferred edges are noise. |
| `nexus_verification_registry` | `[]` | List of paths (relative to `conf.py`) to YAML files declaring explicit verification and implementation edges. See `schema version 1` in the README's V&V section. Missing nodes are logged and skipped; schema errors raise `RegistryError` at build time. |

## Supported Project Layouts

Nexus works with any Python project:

- **Standard packages**: `myproject/mypackage/__init__.py` — detected automatically
- **src layout**: `src/mypackage/` — detected automatically
- **Flat modules**: directories with `.py` files but no `__init__.py` — detected automatically
- **Custom sys.path**: projects that add directories to `sys.path` in `conf.py` — picked up from the Sphinx build environment

## What the Graph Contains

### Node Types (14)

| Type | Source | Example |
|------|--------|---------|
| `file` | Sphinx | RST/doc pages |
| `section` | Sphinx | Labeled sections (`:ref:` targets) |
| `equation` | Sphinx | Labeled math equations (`:eq:` targets) |
| `term` | Sphinx | Glossary terms |
| `function` | Sphinx + AST | Python functions |
| `class` | Sphinx + AST | Python classes |
| `method` | Sphinx + AST | Python methods |
| `attribute` | Sphinx + AST | Class attributes |
| `module` | Sphinx + AST | Python modules |
| `data` | Sphinx | Module-level data |
| `exception` | Sphinx | Exception classes |
| `type` | Sphinx | Type aliases |
| `external` | Auto-detected | stdlib, builtins, installed packages (numpy, scipy, ...) |
| `unresolved` | Auto-detected | Referenced but not documented symbols |

### Edge Types (13)

| Edge | Meaning | Source |
|------|---------|--------|
| `contains` | Parent → child (toctree, module→function, class→method) | Sphinx + AST |
| `references` | Cross-reference (`:ref:`, `:term:`) | Sphinx |
| `documents` | Doc page → code symbol (`:func:`, `:class:`) | Sphinx |
| `equation_ref` | Doc → equation (`:eq:`) | Sphinx |
| `cites` | Doc → citation | Sphinx |
| `implements` | Code → equation (inferred from co-occurrence in docs) | Merge |
| `calls` | Function → function | AST |
| `imports` | Module → module | AST |
| `inherits` | Class → parent class | AST |
| `type_uses` | Function → type (from annotations) | AST |
| `tests` | Test → tested function | AST |
| `derives` | Derivation → equation | AST |
| `discriminates_on` | Function → tag it branches on (`if x == "..."`, `match`) | AST |

## MCP Tools (39)

### Exploration
- **`query`** — keyword search across node names
- **`node_at`** — map a file position (LSP result, stack trace) to the innermost enclosing graph node; warns when the file changed since the graph was built (positions in a snapshot drift with edits)
- **`context`** — 360-degree view of a symbol: connections grouped by type, each bucket most-connected-first and token-budgeted (`limit_per_type`, default 25; honest `omitted` counts — a hub node's full context is megabytes)
- **`neighbors`** — direct connections with direction and type filtering
- **`callers`** — functions that call a given node (optionally transitive)
- **`callees`** — functions called by a given node (optionally transitive)
- **`shortest_path`** — how two concepts connect
- **`god_nodes`** — most connected nodes (entry points)
- **`stats`** — graph-level statistics

### Safety & Refactoring
- **`impact`** — blast radius analysis (what breaks if you change X); depth buckets token-budgeted (`limit_per_depth`, default 50) while `total_affected` stays the true count
- **`detect_changes`** — map git diff to affected symbols
- **`rename`** — safe multi-file rename with confidence tagging
- **`retest`** — minimum set of tests to re-run after changes
- **`communities`** — detect functional groupings with cohesion scores
- **`graph_query`** — Cypher-like pattern matching (`"function -calls-> function"`)
- **`bridges`** — find architectural hotspots connecting communities
- **`native_place`** — functions that may belong inside a class (Feature-Envy / "native place"): every non-test caller is a method of one class. Ranked by strength (genuine relocations first, cross-module before same-module, private before public); public functions tested at least as much as used in production are flagged `likely_free_primitive` and ranked last (a verified free-function primitive is *correctly* free)
- **`twin_paths`** — independent implementations of the same computation (Type-2/3 clones / single-source-of-truth violations): function bodies sharing a high fraction of AST structural shingles where neither calls the other. The fingerprint captures the array math (`@`, `einsum`, slicing) the call graph cannot see; cross-module pairs ranked first
- **`discriminations`** — tags discriminated at multiple sites (candidate missing types): the same string/enum tag (`if geometry == "..."`, `match kind:`) branched on by many functions. Makes the coding-elegance smell "a repeated conditional is a missing type — discriminate once, at the boundary" machine-checkable; ranked by site fan-in
- **`dead_functions`** — functions/methods with no static callers (dead-code candidates): zero incoming `calls` edges from non-test code. A candidate list, not a verdict (dynamic dispatch is invisible to the static graph); `public`/`decorated` flags carry the false-positive sources, private+undecorated ranked first
- **`protocol_conformers`** — classes satisfying a `Protocol`'s method-set without declaring it: `Protocol`s are satisfied structurally but `inherits` records only explicit subclassing, so a structural conformer has no edge. Matches by method-name set (a heuristic — the type checker / LSP `goToImplementation` is authoritative)

### Runtime overlay (dynamic execution-flow)
The static graph is *what can run*; a runtime overlay is *what actually ran*. Capture is consumer-side (run a canonical workload under a tracer), then ingest the artifact; the overlay is stored in a sidecar (`_nexus/traces/<run>.json`) keyed by node-ID and re-binds to the live graph at query time — it is never written into `graph.db`. The query tools accept comma-separated run names to **union the canonical suite** (so `dead` means fired in NO run, a branch is missing only if no run took it).
- **`runtime_ingest`** — ingest a `cProfile`/`pstats` dump (counts + time + call edges), a `coverage json --branch` report (line/branch coverage), or a `viztracer` JSON trace (temporal order) and overlay it on the graph by node-ID, joining on `(file_path, lineno)` with a decorator-window rule (97% join on a real solve). `source_prefix` drops stdlib/third-party frames
- **`runtime_runs`** — list ingested runs (name, kind, metadata, node/edge counts)
- **`runtime_hotspots`** — nodes ranked by an observed metric: `cumtime` is the dominant *observed* call chain (the dynamic stage DAG, better than `processes`' static heuristic for a traced run); `ncalls` the iteration-count / recompute smell (a property called 10k×/run = a caching opportunity); `tottime` self-time
- **`runtime_edges`** — runtime call edges overlaid on static `calls`: `dynamic_only` are fired edges the static resolver couldn't see — annotation-mediated dispatch through `self`/typed locals and the resolved face of polymorphism (which concrete impl ran); `fired` are static edges confirmed live with counts; `dead` are static edges among run-reachable nodes that never fired. `substantive_only` drops edges where either endpoint is a property/trivial accessor, surfacing the polymorphic dispatch above property-getter noise
- **`runtime_branches`** — per-node branch coverage (a `coverage --branch` run): nodes that didn't take every conditional outcome, with those that also `discriminates_on` a tag flagged and ranked first — a discrimination always taken one way is a missing type, the dynamic counterpart of `discriminations`
- **`runtime_timeline`** — the observed execution sequence from a `viztracer` run: nodes in order of first entry (mesh → discretize → sweep → iterate → result), with a `max_depth` filter for just the high-level stages

### Code + Doc Fusion (unique to Nexus)
- **`provenance_chain`** — citation → equation → code traceability
- **`verification_coverage`** — equation → code → test coverage map (supports `limit`/`offset` pagination)
- **`verification_audit`** — complete V&V audit: coverage + staleness + prioritized gap list (supports `group_by` and `include_tests`)
- **`verification_gaps`** — untagged tests, unverified equations, missing err catchers (supports `module` and `level` filters)
- **`staleness`** — detect docs that drifted from code
- **`session_briefing`** — AI agent context restoration
- **`trace_error`** — trace from failing test to equations on call path
- **`migration_plan`** — plan dependency migration with phased blast radius
- **`ingest`** — LLM-powered paper/PDF ingestion into the graph
- **`processes`** — detect named execution flows through the codebase (supports `limit`/`offset` pagination)

### Workspaces (git worktrees)
- **`workspaces`** — list every checkout of the project (main tree + linked git worktrees) with branch, graph presence, and build provenance
- **`use_workspace`** — switch the server to the graph built inside another checkout, referenced by worktree name, branch name, or absolute root path (per-session; auto-reload follows)

Node results from AST-derived symbols carry `file_path` and `lineno`,
so any query answer can be fed straight back to an editor, LSP
request, or file read — the position → node bridge (`node_at`) runs
in both directions.

### Edit-time file brief (the ambient channel)

```bash
nexus file-brief path/to/module.py --db _nexus/graph.db --project-root .
```

Prints ≤6 lines of graph context for one source file — node count and
external callers, the highest-degree node's copy-pasteable ID, the
equations the file implements and how many tests verify them, the doc
pages documenting it, and a staleness flag when the file changed since
the graph was built. It reads the SQLite database directly (no graph
load, ~100 ms warm), which makes it cheap enough to wire into an
edit-time hook (e.g. a Claude Code `PostToolUse` hook on
`Edit|Write`): graph context then arrives WITH every edit, the way a
language server pushes diagnostics, instead of waiting to be asked.
`--json` emits the full structured brief.

### Usage journal

Every tool call appends one JSON line to `~/.nexus/usage.jsonl`
(timestamp, tool, args, duration, outcome, active workspace) so tool
adoption can be evaluated from recorded behavior. Set
`NEXUS_USAGE_LOG=<path>` to relocate it, or set it empty to disable.
Journaling never blocks or fails a tool call.

## MCP Resources (4)

| Resource | Content |
|----------|---------|
| `nexus://graph/stats` | Node/edge counts by type |
| `nexus://graph/communities` | Functional area summaries |
| `nexus://graph/schema` | Node types, edge types, ID format |
| `nexus://briefing` | Session briefing for AI agents |

## Skills (9)

Installed via `nexus setup`. Each skill triggers on natural language:

| Skill | Triggers on |
|-------|------------|
| `nexus-exploring` | "How does X work?", "What calls this?" |
| `nexus-impact` | "Is it safe to change X?", "What tests to re-run?" |
| `nexus-debugging` | "Why is X failing?", "Which equation is wrong?" |
| `nexus-refactoring` | "Rename this", "Extract this into a module" |
| `nexus-verification` | "What's verified?", "Which docs are stale?" |
| `nexus-migration` | "Plan numpy→jax migration" |
| `nexus-guide` | "What Nexus tools are available?" |
| `nexus-cli` | "Analyze the codebase", "Start the server" |
| `behavioral-auto-regression` | "Agent is using Grep instead of Nexus", "Tool selection is wrong" |

## V&V Integration

Nexus turns pytest markers, RST directives, and repository-level YAML into typed verification edges in the graph, so audit tools can answer "which equations are actually verified, and by which tests, at what V&V level?" without hand-wiring.

### From pytest markers (zero config)

Add standard pytest markers to your tests and they flow through to the graph automatically:

```python
import pytest

@pytest.mark.l0
@pytest.mark.verifies("transport-cartesian")
@pytest.mark.catches("FM-07")
def test_attenuation_vacuum_source():
    ...
```

After the next Sphinx build, the corresponding test node carries `vv_level="L0"`, `verifies=("transport-cartesian",)`, and `catches=("FM-07",)` in its metadata. A `merge.write_verifies_edges` pass then walks every function with a `verifies` tuple and emits real `EdgeType.TESTS` edges from the test to `math:equation:transport-cartesian`. Class-level and module-level `pytestmark` declarations propagate to contained test methods (gated on `is_test=True` — private helpers don't inherit).

The `@verify.l0(equations=[...], catches=[...])` sugar form is also recognized.

### From RST directives

Declare verification edges directly in theory prose:

```rst
.. math::
   :label: transport-cartesian

   \dots

.. implements:: transport-cartesian
   :by: orpheus.sn.solve_sn

.. verifies:: transport-cartesian
   :by: tests.test_sn.test_transport
```

Both directives accept an explicit `:by:` option naming the Python symbol. When omitted, they fall back to inspecting `env.ref_context` so usage nested inside `.. py:function::` / `.. autofunction::` blocks picks up the enclosing signature automatically. Directive edges are tagged `source="directive"` and survive incremental builds via a docname-keyed pending queue with an `env-purge-doc` handler.

### From a registry YAML

For bulk declarative facts that live with the repo rather than the tests, drop a `verification.yaml` somewhere and point `nexus_verification_registry` at it:

```yaml
version: 1

verifications:
  - test: py:function:tests.test_solver.test_attenuation
    verifies: [transport-cartesian]
    level: L0
    catches: [FM-07]

implementations:
  - function: py:function:orpheus.sn.solve_sn
    implements: [transport-cartesian]
    confidence: 1.0
```

Schema errors raise `RegistryError` at build time with a path-and-field context. Missing nodes (test / function / equation) are logged and skipped — the registry can name symbols that don't exist yet without breaking the build.

### Querying the result

Every path above produces the same `EdgeType.TESTS` / `EdgeType.IMPLEMENTS` edges, so the audit tools don't care which source they came from. The `source` attribute distinguishes `pytest.mark.verifies`, `directive`, `registry`, and the fallback `inferred` heuristic.

**Tier calibration caveat** (#5): the `declared` tier is the precision instrument; the heuristic tiers are a best-effort safety net. When an equation's `implements` anchor lands on a low-level primitive (token overlap favors it), tests that exercise the equation through a user-facing driver get credited as `heuristic-multihop` rather than `heuristic-1hop` — the coverage is real, but the confidence label reads weaker than it is. Measured on a mature declared-tier project (ORPHEUS, 972 test-bearing entries): 2 equations (0.2%) show this signature. The remedy is an explicit `@pytest.mark.verifies("label")` on the driver tests, not heuristic tuning — if a multihop-only count surprises you, declare the link.

```python
from sphinxcontrib.nexus.query import GraphQuery
from sphinxcontrib.nexus.export import load_sqlite

q = GraphQuery(load_sqlite("docs/_build/html/_nexus/graph.db"))

# Full audit bucketed by V&V level
audit = q.verification_audit(group_by="level", include_tests=True)
for level, gaps in audit.grouped.items():
    print(f"{level}: {len(gaps)} unverified equations")
print(f"declared: {audit.summary['tests_declared']}  heuristic: {audit.summary['tests_inferred']}")

# Gap hunt
gaps = q.verification_gaps(module="orpheus.sn", level="L0")
print(f"untagged tests in orpheus.sn: {len(gaps.untagged_tests)}")
print(f"unverified L0 equations:     {len(gaps.unverified_equations)}")
```

Same surface on the MCP side (`verification_audit`, `verification_gaps`) and the CLI (`nexus audit`, `nexus gaps`).

## Git Worktrees & Workspaces

A graph database is a snapshot of **one** checkout. Agent harnesses
(e.g. Claude Code) spawn the MCP server against the main checkout and
keep it running when a session moves into a git worktree — so without
help, worktree sessions silently query the wrong branch's graph.
Nexus closes that hole in four layers:

1. **Provenance stamping.** Every graph write (Sphinx build, `nexus
   analyze`) stamps `metadata["provenance"]` with `source_root`,
   `built_at`, `git_branch`, `git_commit`, `git_dirty`. Every database
   says which tree it is a snapshot of.
2. **Discovery.** `workspaces` (MCP) / `nexus workspaces` (CLI)
   enumerate all checkouts via `git worktree list` and report which
   have graphs, on which branch, built from where.
3. **Switching + tripwire.** `use_workspace(root)` re-points the
   server at another checkout's graph (one server per agent session,
   so the switch is session-scoped); it accepts a worktree directory
   name, a branch name, or an absolute root path. `session_briefing`
   carries a `workspace` block that warns when the graph's branch no
   longer matches the checkout or when sibling worktrees have graphs
   of their own — the wrong-tree mismatch surfaces on the session's
   first turn.
4. **Roots auto-alignment.** `session_briefing` asks the client (MCP
   `roots/list`) which directory the session was launched from; when
   that lies inside a different checkout that has a graph, the server
   switches to it automatically and reports the switch under
   `workspace.auto_align`. Sessions *launched inside* a worktree need
   no manual step at all.

Recommended agent protocol for sessions that enter a worktree
*mid-session* (roots updates there are client-dependent): build the
docs (or run `nexus analyze`) inside the worktree, then call
`use_workspace(<worktree name>)`.

## Storage

The graph is stored in two formats:

- **SQLite** (primary) — indexed queries, FTS5 full-text search, 0.05ms neighbor lookups. Written with a `schema_version` row in the `metadata` table. `load_sqlite` rejects databases written by a future nexus release with `SchemaVersionError`, so downgrading consumers fail loud instead of silently misreading.
- **JSON** (secondary) — human-readable, NetworkX node-link format.

## Python API

```python
from sphinxcontrib.nexus.export import load_sqlite
from sphinxcontrib.nexus.query import GraphQuery

kg = load_sqlite("_nexus/graph.db")
q = GraphQuery(kg)

# What uses numpy.ndarray?
q.query("ndarray", node_types=["external"])

# Blast radius of changing a function
q.impact("py:function:sn_solver.solve_sn", direction="upstream")

# Citation → equation → code chain
q.provenance_chain("py:function:sn_sweep.sweep_spherical")

# Migration plan
q.migration_plan("numpy", "jax")
```

## License

MIT
