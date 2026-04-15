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

### Edge Types (12)

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

## MCP Tools (25)

### Exploration
- **`query`** — keyword search across node names
- **`context`** — 360-degree view of a symbol (all connections grouped by type)
- **`neighbors`** — direct connections with direction and type filtering
- **`callers`** — functions that call a given node (optionally transitive)
- **`callees`** — functions called by a given node (optionally transitive)
- **`shortest_path`** — how two concepts connect
- **`god_nodes`** — most connected nodes (entry points)
- **`stats`** — graph-level statistics

### Safety & Refactoring
- **`impact`** — blast radius analysis (what breaks if you change X)
- **`detect_changes`** — map git diff to affected symbols
- **`rename`** — safe multi-file rename with confidence tagging
- **`retest`** — minimum set of tests to re-run after changes
- **`communities`** — detect functional groupings with cohesion scores
- **`graph_query`** — Cypher-like pattern matching (`"function -calls-> function"`)
- **`bridges`** — find architectural hotspots connecting communities

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
