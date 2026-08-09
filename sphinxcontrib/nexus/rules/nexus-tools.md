# Nexus & code-exploration tools

This project ships **Nexus** (`sphinxcontrib-nexus`) — one knowledge graph unifying
code structure (call graphs, imports, inheritance, type annotations) with documentation
structure (equations, cross-references, citations, theory pages). It runs as an MCP
server; the graph rebuilds on every docs build and the server auto-reloads when the
database changes.

**Nexus is the structural code-intelligence layer.** It answers relationship questions
that text search fundamentally cannot.

**Why a graph beats grep for structure:** grep matches *text*; it misses relationships —
inline imports, `TYPE_CHECKING` blocks, late imports inside functions, aliased imports
(`from numpy import linalg as la`), re-exports, and docstring references. Nexus captures
all of these as graph edges.

You have **freedom of tool choice** — route by what the question actually is:

| Question | Tool | Why |
|---|---|---|
| Callers / dependents / call chains / blast radius | Nexus `callers`, `impact`, `processes` | graph traversal; text can't follow edges |
| Equation / citation traceability | Nexus `provenance_chain` | links code ↔ docs |
| Verification coverage | Nexus `verification_audit` | maps equation → code → test |
| Docs referencing symbols that no longer exist | Nexus `dead_references` | renders as plain text; no build warning |
| Failing-test diagnosis | Nexus `trace_error` | walks the call graph to the suspect equation |
| Safe rename / refactor | Nexus `rename`, `impact` | finds references by graph, not text |
| "Who uses dependency X" (incl. aliased imports) | Nexus `graph_query` / `type_uses` | grep misses aliased / late / `TYPE_CHECKING` imports |
| Structural smells (clones, dead code, missing types) | Nexus `twin_paths`, `dead_functions`, `discriminations`, `native_place`, `protocol_conformers` | whole-graph sweeps |
| What actually RAN (hotspots, real dispatch) | Nexus `runtime_*` | dynamic overlay; static graph can't see it |
| A file position (LSP result, stack trace) | Nexus `node_at` | position → graph node |
| Literal text / regex / config values | `grep`/`rg` via **Bash** | finds raw strings |
| TODO / FIXME / inline comments | `grep` via **Bash** | Nexus doesn't index comments |
| Known file / known symbol body | **Read** (or `find` via Bash) | don't rediscover what you already know |
| Unknown symbol location | either — Nexus `query` or `grep` | your call |

Over-using Nexus where a plain `Read` or `grep` was correct is as much a misselection as
grepping for a relationship question. Do not perform compliance theater.

**Users describe symptoms, not tools.** "We keep changing these classes in lockstep",
"two people built this separately", "things live in surprising places", "the docs feel
out of date" are all graph questions — route them to `protocol_conformers` / `twin_paths`
/ `native_place` / `dead_references` respectively rather than reading files until a
pattern appears.

**Some checks are part of the job, not a request.** After you delete or rename anything,
run `dead_references` before calling it done — green tests do not cover prose, and a dead
documentation reference produces no build warning at any severity, so nothing else will
catch it. Before a release, and for any "health check" or onboarding review, sweep the
smell family (`twin_paths`, `discriminations`, `native_place`, `protocol_conformers`,
`dead_functions`) alongside `dead_references` and `staleness`.

**Invoke the Nexus *skills*, not raw MCP tools** — they encode the complete workflows
(`nexus-exploring`, `nexus-impact`, `nexus-debugging`, `nexus-refactoring`,
`nexus-verification`, `nexus-elegance`, `nexus-guide`).

**Operational notes**

- **Deferred tools:** if `mcp__nexus__*` surface as deferred, ONE
  `ToolSearch("select:mcp__nexus__<name>")` loads them — deferral is NOT unavailability.
  This is the most common cause of an agent silently avoiding the graph.
- **Stale graph:** rebuild the docs first; the MCP server auto-reloads.
- **Git worktrees:** the session's MCP server may have been launched against the MAIN
  checkout's graph, so every query answers from the wrong branch until you switch. Build
  inside the worktree, then `use_workspace(<worktree root>)`. `session_briefing` warns
  when a sibling checkout carries a fresher graph.
