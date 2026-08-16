# CLI

Every command writes JSON to stdout unless noted, so they compose with
`jq` and drop into shell hooks. The default database is
`_nexus/graph.db` under the docs build directory; pass `--db` to point
elsewhere.

## Setup and serving

```bash
nexus setup                 # install skills for Claude Code / Cursor / Codex
nexus serve --db <path>     # MCP server over stdio
nexus analyze <src> --db <path>
nexus status --db <path>    # node/edge counts by type
```

`nexus analyze` indexes Python source without Sphinx, for projects that
have code but no docs build yet.

## Asking questions

```bash
nexus query <text> --db <path>
nexus context <node_id> --db <path>
nexus neighbors <node_id> --db <path> [--direction in|out|both] [--edge-types calls,imports]
nexus callers <node_id> --db <path> [--transitive] [--max-depth 3]
nexus callees <node_id> --db <path> [--transitive] [--max-depth 3]
nexus shortest-path <source> <target> --db <path> [--max-hops 8]
nexus graph-query "<pattern>" --db <path> [--limit 50]
```

## Structure and smells

```bash
nexus god-nodes --db <path> [--top-n 10]
nexus communities --db <path> [--min-size 3]
nexus bridges --db <path> [--top-n 10]
nexus processes --db <path> [--min-length 3]
nexus native-place --db <path>
nexus twin-paths --db <path>
nexus discriminations --db <path>
nexus dead-functions --db <path>
nexus protocol-conformers --db <path>
```

## Documentation health

```bash
nexus dead-references --db <path> [--limit 50] [--format text|json]
nexus staleness --db <path>
nexus coverage --db <path>
nexus audit --db <path> [--project-root .]
nexus gaps --db <path>
nexus provenance <node_id> --db <path>
nexus briefing --db <path>
```

`dead-references` takes two flags built for CI:

- `--quiet-when-clean` — print nothing when there are no findings, so a
  hook stays silent until it has something to say
- `--exit-code` — exit non-zero on findings, so it gates a pipeline

## Change safety

```bash
nexus changes --db <path> [--scope all|staged|unstaged|branch]
nexus retest --db <path> [--scope ...]
nexus impact <node_id> --db <path>
nexus rename <old> <new> --db <path> [--apply]
nexus trace <test_node_id> --db <path>
nexus migration <from> <to> --db <path>
```

`rename` is dry-run by default; `--apply` writes.

## Runtime overlay

```bash
nexus runtime-ingest <artifact> [--kind cprofile|coverage|viztracer] [--run NAME] [--source-prefix PFX ...] [--root DIR]
nexus runtime-runs --db <path>
nexus runtime-hotspots --db <path> [--run NAME[,NAME...]] [--by cumtime|ncalls|tottime]
nexus runtime-edges --db <path> [--mode dynamic_only|fired|dead] [--substantive-only]
nexus runtime-branches --db <path> [--all]
nexus runtime-timeline --db <path> [--max-depth N]
```

## Workspaces and the edit-time brief

```bash
nexus workspaces
nexus file-brief <path> --db <path>
```

`file-brief` reads SQLite directly rather than loading the graph, which
makes it fast enough for a `PostToolUse` hook — the ambient channel that
pushes what the graph knows about a file *with* the edit, instead of
waiting to be asked.

## Visualising

```bash
nexus visualize --db <path>     # interactive force-directed HTML
nexus ingest <file> --db <path> --llm-command <cmd>
```

A build also writes `_nexus/graph.html` automatically, and the
`.. nexus-graph::` directive embeds it in a page.
