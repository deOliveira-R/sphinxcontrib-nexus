---
name: nexus-guide
description: "Use when the user asks about Nexus itself — available tools, how to query the knowledge graph, MCP resources, graph schema, or workflow reference. Examples: \"What Nexus tools are available?\", \"How do I use Nexus?\", \"What can I query?\""
---

# Nexus Guide

## Skills

| Task | Skill |
|------|-------|
| "How does X work?" | `nexus-exploring` |
| "What breaks if I change X?" | `nexus-impact` |
| "Why is X failing?" | `nexus-debugging` |
| Rename / extract / refactor | `nexus-refactoring` |
| V&V status / "Which docs are stale?" | `nexus-verification` |
| Dependency migration | `nexus-migration` |
| CLI commands (analyze, serve) | `nexus-cli` |

## Quick Start

```
1. READ nexus://briefing                    → Session overview
2. Match task to skill above
3. Follow that skill's workflow
```

## Bridges into the graph

- **From a position** (LSP result, stack trace, editor line):
  `node_at({file, line})` → graph node → `context`/`impact`.
- **From a node result**: AST-derived results carry
  `file_path`/`lineno` — open the source directly.
- **From an edit**: projects may inject `nexus file-brief` output
  via an edit-time hook — the brief's node IDs are entry points,
  its `stale:` line means rebuild before trusting positions.
- **From a worktree**: after EnterWorktree, `use_workspace(<name>)`
  so queries answer from that checkout's graph.

## Full Reference

See [../nexus-exploring/reference.md](../nexus-exploring/reference.md) for
complete tool, resource, edge type, node ID format, graph_query syntax,
and CLI command reference.
