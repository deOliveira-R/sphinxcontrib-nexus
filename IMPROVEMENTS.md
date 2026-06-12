# Nexus improvement backlog — observed during the v0.12.0 worktree session (2026-06-12)

UNTRACKED scratch file for cross-compaction pickup. Each entry was noticed
while building/testing `feature/workspace-worktrees`; none are speculative.

## Where things stand (recap for a fresh session)

- Branch `feature/workspace-worktrees` @ `63e1bfa` PUSHED to origin
  (NOT merged to main, no PR yet). v0.12.0 = workspace/worktree support
  (`workspace.py`, `workspaces`/`use_workspace` MCP tools, provenance
  stamping, briefing tripwire) + nested-git-tree pruning in the AST
  analyzer (ORPHEUS main's graph was 51% worktree-copy contamination).
  358 tests green; elegance-enforcer PASS; verified end-to-end over the
  real MCP stdio protocol against ORPHEUS (script pattern preserved in
  ORPHEUS job tmp: `mcp_e2e_test.py` — stdio_client → list_tools →
  briefing → workspaces → use_workspace → re-briefing).
- ORPHEUS side: branch `worktree-nexus-workspace-wiring` PUSHED
  (.mcp.json `${CLAUDE_PROJECT_DIR:-.}` anchoring, CLAUDE.md worktree
  protocol, lessons L22 item 4) — awaiting ff-merge to ORPHEUS main.
  ORPHEUS venv editable install refreshed (dist metadata was 26 releases
  stale: 0.5.1 vs 0.11.0). Main + wiring-worktree graphs rebuilt clean
  and stamped; `sn-nd-layout`'s graph is still pre-0.12 (unstamped) until
  its next sphinx build.
- Running MCP server processes only pick up new TOOLS on session restart
  (auto-reload refreshes data, not code).

## High value

1. **Stray `CLAUDE.md` at this repo root is an OLD ORPHEUS copy** (untracked).
   It actively misleads sessions working in this repo (wrong cardinal rules,
   wrong agents, wrong paths). Replace with a real nexus CLAUDE.md: test
   invocation (`pytest tests/ -q`, no `-O` convention here), release process
   (version in BOTH `pyproject.toml` and `__init__.py`, CHANGELOG discipline,
   tag-triggered PyPI publish), PR-based merges, architecture map
   (module-per-concern list like the one in README/ORPHEUS memory).
2. **Workspace auto-detection via MCP roots.** Today the agent must call
   `use_workspace` manually (instructions live in ORPHEUS CLAUDE.md). Claude
   Code answers the MCP `roots/list` request with the session's launch
   directory — at serve startup (and opportunistically later), ask for roots
   and auto-select the workspace whose root contains it. Makes the manual
   call a fallback instead of a protocol step. Caveats researched 2026-06-12:
   roots updates on EnterWorktree are UNDOCUMENTED; CLAUDE_PROJECT_DIR stays
   = main checkout. Needs empirical probing of FastMCP's roots API.
3. **Tool-count drift keeps recurring** (serve help said 16; ORPHEUS CLAUDE.md
   said 20; README now says 27 and WILL drift again). Single-source it: a
   test asserting README's "MCP Tools (N)" equals the FastMCP registry count
   (`len(_mcp._tool_manager.list_tools())` or public equivalent), or generate
   the README tool list from tool docstrings.
4. **`use_workspace` ergonomics: accept a worktree NAME.** Agents see
   `.claude/worktrees/sn-nd-layout` in `workspaces` output; let
   `use_workspace("sn-nd-layout")` resolve by unique basename/branch match
   instead of requiring the absolute root path.
5. **Sibling-graph warning is unconditional noise for main-checkout sessions.**
   Any worktree with a graph triggers the briefing warning even when the
   session IS in main and main's graph is fresh. Tune: warn only when a
   sibling graph is FRESHER than the active one (mtime comparison), or
   demote the always-on variant to an info field. Watch real usage first —
   one session of evidence before changing the contract.

## Correctness / robustness

6. **`detect_changes` / `session_briefing` / `retest` diff base is hardcoded
   `main`/`master`** (`query.py::_git_changed_files`, scope="branch"). In a
   worktree whose branch forked from another feature branch, "recent changes"
   over-reports. Use `git merge-base HEAD <default-branch>` (or a configurable
   base) instead of `diff main...HEAD` with a fallback chain.
7. **`server.py` ingest tool pokes a private attr**: builds
   `KnowledgeGraph()` then assigns `kg._graph = q._g` (around the `ingest`
   tool). Should use a public constructor/adapter; also decide whether
   ingest-time graphs should be provenance-stamped (currently `nexus ingest`
   CLI path does NOT stamp — deliberate for now, since ingest mutates an
   existing db whose stamp describes the build; revisit).
8. **Pre-existing pyright reds worth clearing while touching these files**:
   `cli.py` `_load_query(...) -> "GraphQuery"` forward-ref without import
   (use `TYPE_CHECKING` import); `server.py` `impact(direction: str)` passed
   to a `Literal["upstream","downstream"]` param (validate/narrow at the tool
   boundary — also gives the agent a better error than scipy-style breakage).
9. **`nexus setup` prints a PostToolUse hook suggestion with an `"if":` field**
   — that is not the documented Claude Code hooks schema (matcher/hooks
   shape). Verify against current docs and fix the printed advice; stale
   advice in a setup command propagates into user configs.
10. **Provenance key strings** (`"git_branch"` etc.) are written in
    `workspace.stamp_provenance` and read by string in
    `server._workspace_payload` (elegance review nit, deferred at 1 consumer).
    On the SECOND consumer (e.g. `nexus status` showing provenance, or
    freshness logic from item 5), extract named key constants or a
    `GitProvenance.from_stamp()` reader.

## Cleanups / DX

11. **Version is duplicated** (`pyproject.toml` + `__init__.py.__version__`).
    Single-source: `__version__ = importlib.metadata.version("sphinxcontrib-nexus")`
    or hatch dynamic versioning. Caused no bug yet; pure drift surface.
12. **`GraphQuery` drops `KnowledgeGraph.metadata`** (keeps only the nxgraph).
    Workspace code reads provenance from disk instead (fine), but the moment
    an in-memory consumer appears, add `GraphQuery.metadata` rather than a
    second disk read path.
13. **CI coverage**: tests run on tag-publish, but consider PR CI with the
    full pytest suite + pyright on changed files (would have caught the
    pre-existing reds in item 8 at introduction time).
14. **Upstream open issues**: #5 (verification_coverage heuristic tiers
    under-report when `implements` anchors on a low-level primitive) and
    #1 (semantic search / local embedding model). #5 is the more
    ORPHEUS-relevant one — the V&V matrix consumes that signal.
15. **ORPHEUS steering plan phase 2 (approved 2026-04-08) still pending**:
    MCP convenience functions validated by agent-loop evidence
    (`who_tests`, `who_calls`, `what_implements`, `untested`). Revisit
    against actual agent transcripts before building — the plan's own rule.

## Follow-through (not nexus code)

- Merge `worktree-nexus-workspace-wiring` into ORPHEUS main (ff-only), then
  remove that worktree; restart sessions to get the new tools.
- Rebuild Sphinx in `sn-nd-layout` so its graph gets stamped.
- After merging this branch to nexus main: tag v0.12.0 for the PyPI publish,
  delete the feature branch (both per repo convention).
- ORPHEUS#224 (orphan `verifies` labels: streaming-equilibrium, sn-streaming)
  — found during the graph rebuilds, ORPHEUS-side fix.
