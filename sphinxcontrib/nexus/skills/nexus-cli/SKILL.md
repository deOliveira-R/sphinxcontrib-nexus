---
name: nexus-cli
description: "Use when the user needs to run Nexus CLI commands like analyze source code, start the MCP server, or check graph status. Examples: \"Index this repo\", \"Analyze the codebase\", \"Start the nexus server\""
---

# Nexus CLI

## Where the graph lives

`<project root>/.nexus/graph.db` — the directory that holds `.nexus/`
is the project root, so every surface *derives* the path instead of
being told it. There is no config key for it and nothing to keep in
sync. `--db` is therefore optional on every command; pass it only to
open a *different* graph.

```bash
nexus config db          # print the derived path (what a script should ask)
```

The JSON export sits beside it at `.nexus/graph.json`, and the runtime
overlay sidecars at `.nexus/traces/<run>.json`.

## Commands

### Analyze Source Code

```bash
# Basic: analyze current directory into the project's graph
nexus analyze .

# With specific sys.path entries (for non-standard project layouts)
nexus analyze . --sys-path 01.Discrete.Ordinates 02.Collision.Probability

# Auto-detect numbered directories as sys.path entries
nexus analyze . --auto-sys-path

# Also write JSON
nexus analyze . --json .nexus/graph.json

# Merge into a graph somewhere else
nexus analyze src/ --db /path/to/other/graph.db
```

### Start MCP Server

```bash
# Start the MCP server on the project's own graph
nexus serve --project-root /path/to/project

# Or point it at an explicit database
nexus serve --db /path/to/graph.db --project-root /path/to/project
```

### MCP Configuration

Add to Claude Code's MCP config:
```json
{
  "mcpServers": {
    "nexus": {
      "command": "nexus",
      "args": ["serve", "--db", "/path/to/graph.db", "--project-root", "/path/to/project"]
    }
  }
}
```

## Sphinx Integration

Add to `docs/conf.py`:
```python
extensions = ['sphinxcontrib.nexus']

# Optional configuration
nexus_output = 'graph'            # default '_nexus'. Where the explorer page is
                                  # written, relative to the Sphinx HTML output
                                  # directory (default). It does NOT move the
                                  # database.
nexus_ast_analyze = True          # Run AST analysis during build (default: True)
```

After `sphinx-build`, `.nexus/graph.db` (SQLite) and `.nexus/graph.json`
are regenerated at the project root, and the explorer page is written to
`<html outdir>/graph/graph.html`.

**Why they are split.** Three artefacts used to share the build output
directory with three different lifetimes: `graph.db`/`graph.json` are
derived and rewritten on every build; `graph.html` is derived *and* must
be served from the HTML tree; but `traces/` is durable state — a
profiled run costs minutes to reproduce, and the sidecar exists
precisely so it survives the rebuild that replaces the database. Sitting
in the build tree it did not: `rm -rf docs/_build` destroyed it. A
directory's lifetime is its most-derived member's, so only the page that
must be served stays under the build output.

## Graph Freshness

The graph is automatically rebuilt during every `sphinx-build`. For standalone use:

```bash
# Re-analyze after code changes
nexus analyze .

# The MCP server loads from the database — restart it after re-analysis
```
