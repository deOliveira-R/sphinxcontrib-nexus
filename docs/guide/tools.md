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

:::{note}
**Any** result carrying a position says so when that position is
suspect. A graph is a snapshot of one checkout, so an edit above a
definition moves it without moving the stored line — whenever a returned
`file_path` has changed since the graph was built, a `stale` key appears
beside it naming the build commit. Nothing appears when the graph is
fresh, so its presence is the signal.
:::

## Finding a starting point

| Tool | Answers | Key args |
|---|---|---|
| `query` | Which symbols match this keyword? | `text`, `node_types`, `limit` |
| `file_brief` | What does the graph know about this FILE? The one tool addressed by a path rather than a node id — every list unclipped, every handle a pastable id | `file` |
| `node_at` | Which node encloses this file position? Takes an LSP result or a stack-trace frame | `file`, `line` |
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

`context` and `neighbors` serve the same relations two ways, and the
division is not stylistic — measured, each wins a different mode.

Reach for **`context` first**, especially straight off a `file_brief`.
It **groups** by edge type and direction, which is the axis your next
decision splits on: "if I change this, who breaks?" is `incoming.calls`,
one key, no filtering. A flat list makes you rebuild that grouping in
your head, and — because the reply budget truncates — ranking an
*incoming call* against an *outgoing type-use* on one scalar buries the
answer. Measured on ORPHEUS: for `LossKernelGauge.for_mesh` the single
production caller sat at rank 27 of 44 flat entries, while the top slots
went to `SNMesh` (degree 1633, adjacent to everything).

Inside a bucket, **production entries lead and test-tree entries
follow**. Tests swamp incoming calls — 17 of 18 for `for_mesh`, 22 of 25
for `solve_sn` — so without this the one caller you must not break sits
below the fold. Demotion is relative to the asker: query a test node and
nothing is demoted, because there test material is the subject. It keys
on `in_test_file`, not `is_test`, since a `_ld_mesh`-style helper
defined in a test module is test material too (by `is_test`,
`LinearDiscontinuous` reports 7 production callers; the true count is
0).

Reach for **`neighbors` when you want one relation, complete**:
`neighbors(id, direction="in", edge_types=["calls"])` is the uncapped
single-bucket list `context` will not give you. In that mode its entries
collapse to `{id, degree}` — the direction and edge type were your
question, so they are not repeated in the answer.

Two things a `neighbors` entry deliberately omits. Parallel edges (three
`isinstance` calls) collapse into one entry carrying `times`, and no
entry carries `file_path`/`lineno` — adjacency is not location, so ask
`context` or `node_at` about the one neighbour you go on to open.

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
| `doc_impact` | Which documented claims did my change put in question? The same cone as `retest`, ending in equations instead of tests | `node_id`, `limit` |
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
| `runtime_exercisers` | Which tests EXECUTED this node? The falsifier for a coverage claim | `run`, `node`, `limit` |
| `runtime_markers` | Which tests carry this marker — **as pytest resolved it** | `run`, `marker`, `node` |
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

## Markers as pytest resolved them

Nexus lifts markers by AST-parsing decorators, which sees what was
*spelled* on a function. pytest resolves more: module-level `pytestmark`,
class marks, and marks a `conftest.py` attaches during collection. On one
real project the AST path reports **0** nodes for `foundation`, `cap`,
`regression` and `sentinel`; the resolved manifest finds **3709 / 1707 /
111 / 39**.

Capture is consumer-side and costs seconds, because nothing executes:

```bash
pytest --collect-only -q -p sphinxcontrib.nexus.pytest_manifest \
       --nexus-manifest=.nexus/traces/markers.json
nexus runtime-ingest .nexus/traces/markers.json --kind pytest --run markers
```

Then `runtime_markers(run="markers", marker="regression")` answers "which
tests carry this claim", and each result carries the pytest node ids plus
a runnable `invocation` — not graph ids you have to translate.

No marker name is enumerated anywhere in nexus, so your own markers work
without a nexus release. Parametrised cases are several pytest ids on one
graph node; their markers are unioned, which is the conservative reading
(if one case is `slow`, running the node is slow).
