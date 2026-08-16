# Tools: 40 MCP tools by the question they answer

This page is for whoever drives the MCP server — an agent, or the person
configuring one. Tools are grouped by the question, not by implementation,
because picking the right one is the hard part.

Every tool takes node ids in the form described in {doc}`vocabulary`.

:::{tip}
Start a session with `session_briefing`. It returns stale docs, V&V gaps,
and recent changes in one call, which is usually enough to know which of
the tools below you actually want.
:::

## Finding a starting point

| Tool | Answers | Key args |
|---|---|---|
| `query` | Which symbols match this keyword? | `text`, `node_types`, `limit` |
| `node_at` | Which node encloses this file position? Takes an LSP result or a stack-trace frame. Warns when the file changed since the graph was built | `file`, `line` |
| `stats` | How big is this graph, and of what? | — |
| `session_briefing` | What should I know before starting? | — |

## Understanding one symbol

| Tool | Answers | Key args |
|---|---|---|
| `context` | Everything about this node — attributes and all connections | `node_id` |
| `neighbors` | What is directly connected? | `node_id`, `direction`, `edge_types` |
| `callers` | What calls this? | `node_id`, `transitive`, `max_depth` |
| `callees` | What does this call? | `node_id`, `transitive`, `max_depth` |
| `shortest_path` | How do these two things connect at all? | `source`, `target`, `max_hops` |

## Understanding the shape of the codebase

| Tool | Answers | Key args |
|---|---|---|
| `communities` | What are the functional groupings? | `min_size` |
| `bridges` | Which nodes hold communities together? | `top_n` |
| `god_nodes` | What is most connected — and therefore riskiest? | `top_n` |
| `processes` | What are the execution flows from entry points? | `min_length` |
| `graph_query` | Anything else — a structured traversal | `pattern`, `limit` |

`graph_query` takes a Cypher-flavoured pattern:

```
source_type -edge_type-> target_type [WHERE field=value]

function -calls-> function
file -contains-> equation
* -implements-> equation
function -type_uses-> external WHERE name=numpy*
```

`*` matches any type; `name=prefix*` does a prefix match.

## Changing things safely

| Tool | Answers | Key args |
|---|---|---|
| `impact` | What breaks if I change this? | `target`, `direction`, `max_depth`, `edge_types` |
| `detect_changes` | What did my git diff actually touch, in graph terms? | `scope` |
| `retest` | What is the minimum test set for these changes? | `scope` |
| `rename` | Rename across files, safely | `old_name`, `new_name`, `dry_run` |
| `migration_plan` | How do I get from this dependency to that one? | `from_dep`, `to_dep` |

## Architecture smells (the missing-abstraction family)

Each answers "is there a type or class that should exist here but doesn't?"

| Tool | Answers | Key args |
|---|---|---|
| `native_place` | Which functions want to be methods? (Feature-Envy) | `min_callers`, `exclude`, `limit` |
| `twin_paths` | Which computations are independently reimplemented? | `min_similarity`, `min_tokens`, `exclude`, `limit` |
| `discriminations` | Which tags are branched on in many places? A tag discriminated at ten sites is usually a missing type | `min_sites`, `exclude`, `limit` |
| `protocol_conformers` | Which classes satisfy a Protocol without declaring it? | `min_methods`, `exclude`, `limit` |
| `dead_functions` | Which functions have no static callers? | `exclude`, `limit` |

All take `exclude` for substring filtering, on top of the built-in
`is_test` filter.

## Runtime overlay — what actually ran

The static graph is *what can run*; an overlay is *what did*. Capture is
consumer-side: run a workload under a tracer, then `runtime_ingest` joins
the artifact onto node ids and stores it in a sidecar
(`<project root>/.nexus/traces/<run>.json`) — never in `graph.db`, which is
rewritten on every build. The sidecar sits beside the database and outside
the Sphinx build output on purpose: a trace is durable state that costs
minutes to reproduce, and a directory a clean build deletes would take it
with the derived artefacts.

| Tool | Answers | Key args |
|---|---|---|
| `runtime_ingest` | Overlay a `cProfile` / `coverage --branch` / `viztracer` artifact | `artifact`, `kind`, `run`, `source_prefix` (list), `root` |
| `runtime_runs` | Which runs have been ingested? | — |
| `runtime_hotspots` | Where did time and iterations go? | `run`, `by`, `limit` |
| `runtime_edges` | `dynamic_only` (dispatch the static graph missed), `fired`, `dead` | `run`, `mode`, `node`, `substantive_only` |
| `runtime_branches` | Which branches never fired? Discriminators ranked first | `run`, `node`, `partial_only` |
| `runtime_timeline` | In what order did things execute? | `run`, `max_depth`, `limit` |

The query tools accept `run` as one name or a comma-separated list, so a
canonical suite can be unioned.

## Code + documentation fusion

This is what the unified graph is *for* — none of these are answerable
from code or docs alone.

| Tool | Answers | Key args |
|---|---|---|
| `provenance_chain` | The citation → equation → code chain, plus the maths-to-maths spine in `relations` | `node_id` |
| `verification_coverage` | Which equations have code and tests? | `status_filter` |
| `verification_audit` | The complete V&V picture, one call | — |
| `verification_gaps` | Untagged tests, unverified equations, missing error catchers | `module`, `level` |
| `staleness` | Which docs drifted from their code? (git timestamps) | — |
| `dead_references` | Which references name something that no longer exists? | `limit` |
| `trace_error` | A failing test → the equations on its path | `test_node_id` |

### On `dead_references`

Sphinx renders a broken reference as plain text with no warning at any
severity, so this is the only place that drift surfaces. Findings may
carry `minted_by` — the files whose own code created the placeholder the
reference bound to. See {ref}`dead-references` for how to read it.

## Workspaces

A graph is a snapshot of one checkout. A session working in a worktree
must query *that* worktree's graph.

| Tool | Answers | Key args |
|---|---|---|
| `workspaces` | Every checkout, which graph each carries, how fresh | — |
| `use_workspace` | Switch the active graph | `ref` |

## Ingestion

| Tool | Answers | Key args |
|---|---|---|
| `ingest` | Add a paper or PDF to the graph | `file_path`, `llm_command` |

## Resources (4)

Read-only, no arguments:

| Resource | Content |
|---|---|
| `nexus://graph/stats` | Node and edge counts by type, density |
| `nexus://graph/communities` | Functional areas with top members |
| `nexus://graph/schema` | Node types, edge types, id format |
| `nexus://briefing` | Session briefing |

## Failure behaviour

Tools degrade rather than raise. Git missing, database corrupt, file
vanished — you get an error payload and the previous snapshot is kept. A
tool call never breaks the session, which means an empty result means
"nothing found", not "something went wrong".
