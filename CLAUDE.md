# sphinxcontrib-nexus — development guide

Sphinx extension + Python AST analyzer + MCP server that builds one
queryable knowledge graph unifying **code structure** (call graphs,
imports, inheritance, type annotations) and **documentation structure**
(equations, cross-references, citations, theory pages). Published on
PyPI; primary consumer is the ORPHEUS reactor-physics project, but the
tool is project-agnostic.

This file describes THIS repo. It is not ORPHEUS — none of the ORPHEUS
cardinal rules, agents, or V&V conventions apply here unless restated
below.

## Environment & tests

- Local venv: `.venv` (pyright is pointed at it via `pyrightconfig.json`).
- Tests: `.venv/bin/python -m pytest tests/ -q` — plain pytest, no `-O`
  convention, no marker taxonomy.
- Type check: `pyright sphinxcontrib/` (uses the repo venv).
- A consumer-side editable install may exist in the ORPHEUS venv
  (`~/git/nuclear/ORPHEUS/.venv`); refresh it with
  `uv pip install -e . --python <that venv>/bin/python` after changes
  you want to exercise against real ORPHEUS data.

## Architecture map

```
sphinxcontrib/nexus/
    __init__.py       — Sphinx extension entry (build-finished hook writes
                        the graph; stamps provenance), __version__
    graph.py          — KnowledgeGraph (nx.MultiDiGraph), NodeType/EdgeType
    _mappings.py      — domain-aware reference resolution
    extractors.py     — Sphinx BuildEnvironment extraction
    ast_analyzer.py   — Python AST walker (calls/imports/inherits/type_uses);
                        prunes nested git trees (worktrees, submodules, clones)
    merge.py          — unify Sphinx + AST graphs, infer IMPLEMENTS edges
    query.py          — GraphQuery: 20+ query methods over the graph
    workspace.py      — checkout ↔ graph-db pairing: Workspace, provenance
                        stamping, `git worktree` discovery
    export.py         — JSON + SQLite (FTS5) export/import,
                        read_sqlite_metadata (metadata peek without full load)
    server.py         — FastMCP server; module-global state = ONE workspace
                        per server process (one agent session); mtime-based
                        auto-reload under _reload_lock
    cli.py            — `nexus` CLI (setup, analyze, serve, workspaces, …)
    ingest.py         — LLM-powered paper/PDF ingestion
    visualize.py      — interactive force-directed graph HTML
    skills/           — Claude Code skills installed by `nexus setup`
```

## Design invariants (violate knowingly or not at all)

- **A graph database is a snapshot of ONE checkout.** Every graph-write
  site stamps `metadata["provenance"]` (source_root, built_at,
  git_branch, git_commit, git_dirty) via `workspace.stamp_provenance`.
  New write paths MUST stamp too.
- **One server process serves one agent session**, so the active
  workspace is process-local state; `use_workspace` swaps it atomically
  under `_reload_lock`. Any new mutation of `_query`/`_workspace`/
  `_db_mtime` must take the lock and re-check identity (see
  `_reload_if_stale`).
- **Failure tolerance at tool-call time**: git missing, db corrupt,
  file vanished → degrade (keep previous snapshot, return an error
  payload), never raise out of an MCP tool.
- **AST analysis never crosses into a nested git tree** (worktree /
  submodule / clone under the analyzed root) — that was a 51%
  graph-contamination bug on ORPHEUS.
- **Producer-side normalization**: stamp/derive at the write site, not
  in each consumer.

## Drift surfaces (guarded by tests — keep them green)

- README "MCP Tools (N)" header and tool bullets ↔ FastMCP registry
  (`tests/test_server_registry.py`).
- Version appears in BOTH `pyproject.toml` and `__init__.__version__` —
  bump both together.

## Release process

1. Bump version in `pyproject.toml` AND `sphinxcontrib/nexus/__init__.py`.
2. Update `CHANGELOG.md` (Added / Fixed / Changed).
3. Merge to `main` via PR (no direct commits to main).
4. Tag `vX.Y.Z` — CI publishes to PyPI on tags.
5. Delete the merged feature branch.

## Git workflow

- Branch naming: `<type>/<topic>` (`feature|fix|docs|refactor|test|chore`).
- Conventional Commits: `<type>(<scope>): <summary>`.
- `main` stays green.
