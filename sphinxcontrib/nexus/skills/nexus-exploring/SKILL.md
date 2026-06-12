---
name: nexus-exploring
description: "Use when the user asks how code works, wants to understand architecture, trace execution flows, or explore unfamiliar parts of the codebase. Examples: \"How does X work?\", \"What calls this function?\", \"Show me the auth flow\", \"How does this equation connect to code?\""
---

# Exploring with Nexus

IMPORTANT: This skill is the dedicated tool for code exploration. It
replaces Grep for all architecture, dependency, and flow questions.

## Workflow

```
1. query({text: "<concept>"})                        → Find symbols
2. context({node_id: "<symbol>"})                    → 360-degree view
3. provenance_chain({node_id: "<symbol>"})            → Citation → equation → code
4. shortest_path({source: "<A>", target: "<B>"})      → How concepts connect
5. Read source files for implementation details
```

## Checklist

- [ ] `query` for the concept you want to understand
- [ ] `context` on key symbols for callers/callees/docs
- [ ] `provenance_chain` to trace mathematical origins
- [ ] `communities` to see functional groupings
- [ ] Read source files for implementation details

## Key Tools

**query** — find symbols by keyword:
```
query({text: "collision probability"})
→ Functions, classes, equations matching the search, sorted by connectivity
```

**context** — 360-degree view of a symbol:
```
context({node_id: "py:function:orpheus.sn.solver.solve_sn"})
→ All incoming/outgoing edges grouped by type
```

**provenance_chain** — mathematical traceability:
```
provenance_chain({node_id: "py:function:orpheus.sn.sweep.transport_sweep"})
→ Bailey2009 → Eq.transport-cartesian → transport_sweep
```

**shortest_path** — how concepts connect:
```
shortest_path({source: "doc:theory/collision_probability", target: "py:class:numpy.ndarray"})
→ Theory page → function → numpy dependency
```

## The position bridge (LSP ↔ graph)

The language server and the graph are complementary: LSP resolves
precisely (definitions, references, live unsaved state, alias-aware);
the graph sees what LSP cannot (equations, tests, doc pages, V&V
chains). Bridge in BOTH directions:

- **Position → node**: any (file, line) — an LSP result, a stack
  trace, an editor cursor — feeds `node_at({file, line})` and returns
  the innermost enclosing graph node. Continue with `context` /
  `impact` / `provenance_chain` / `callers` for the cross-domain
  picture.
- **Node → position**: every AST-derived node result carries
  `file_path` + `lineno` — feed them straight to Read / the editor /
  an LSP request. No text-search round-trip.
- **Honesty**: the graph is a build-time snapshot. `node_at` WARNS
  when the queried file changed since the graph's stamped commit —
  positions may then map to the wrong symbol. Rebuild
  (sphinx-build / nexus analyze) and re-ask; do not trust a warned
  mapping for surgical work.

## Worktrees and ambient context

- A session inside a git worktree must query THAT worktree's graph:
  auto-alignment handles sessions launched there; after a mid-session
  EnterWorktree, call `use_workspace(<worktree name>)`. The
  `session_briefing` workspace block warns on branch mismatch.
- Language-server identity errors mentioning `.claude.worktrees.*`
  module paths are wrong-rooted-server noise, not code bugs —
  discount them.
- Projects may wire `nexus file-brief` into an edit-time hook: a few
  lines of graph context (callers, equations, tests, docs, staleness)
  appear automatically after you edit a file. The node IDs in a brief
  are copy-pasteable entry points — follow the hub ID with `context`
  or `impact` before reshaping a file, and treat a `stale:` line as
  "rebuild the graph before trusting positions".

See [reference.md](reference.md) for full tool/schema/CLI reference.
