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

## MCP Tools (24)

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
- **`verification_audit`** — complete V&V audit: coverage + staleness + prioritized gap list
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

## Storage

The graph is stored in two formats:

- **SQLite** (primary) — indexed queries, FTS5 full-text search, 0.05ms neighbor lookups
- **JSON** (secondary) — human-readable, NetworkX node-link format

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
