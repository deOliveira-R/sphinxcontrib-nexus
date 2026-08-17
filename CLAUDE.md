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

⚠ **Run the suite with THIS repo's venv, not a consumer's.** ORPHEUS's
venv lacks the optional `sphinx-proof`, so the proof-relation tests are
silently skipped there — a green **691** that looks exactly like a green
**727**. A skip is not a pass, and the count is the only thing that
tells you which one you got.

A consumer-side editable install may exist in the ORPHEUS venv
(`~/git/nuclear/ORPHEUS/.venv`); refresh it with
`uv pip install -e . --python <that venv>/bin/python` after changes you
want to exercise against real ORPHEUS data.

⚠ **The MCP server serves the code it imported at startup.** After any
commit here, an agent session using nexus must reconnect (`/mcp`) before
its tools reflect the change. `use_workspace` re-points the graph, not
the code.

## `.nexus/` — the project's whole graph surface

```
<project root>/.nexus/
    config.toml     BEHAVIOUR — how much nexus says, how long it waits
    ontology.toml   SEMANTICS — which types exist, what may connect to what
    graph.db        the graph. DERIVED from the root, never declared
    traces/         runtime overlays. Durable state, not build output
```

**Remember the division; it is the question you will get wrong.**

| Adding something? | Ask | Home |
|---|---|---|
| A number | Does changing it change how much nexus **says** or how long it **waits**? | `config.toml` |
| A number | Does changing it change what nexus **means**? | a constant in code |
| A node/edge type | Always | `ontology.toml` |

Reply sizes, list lengths and timeouts are settings. Schema versions,
the id grammar and the fingerprint shingle size are constants: changing
one does not tune nexus, it makes two installations disagree about what
a graph says.

### `config.toml` — tunables

`project.DEFAULTS` holds every shipped value; `project.KNOWN_KEYS`
declares which names are accepted. **Adding a tunable is one line in
each, and a test pins the two to each other** — a key in `DEFAULTS` but
not `KNOWN_KEYS` is an unsettable default; the reverse is a setting with
no value. Read one with `config.tunable("[table].key")`, or from a query
with `q.tunable(...)`.

⚠ **Never declare a key you have not wired.** An unwired key is a lie in
the file a reader trusts — worse than not offering it. (I shipped two
that way for twenty minutes.)

Tables today: `[replies]` (payload sizes), `[briefing]` (what the
session briefing shows), `[graph]`, `[scope]`, `[catalog]`. Full
reference: `docs/guide/configuration.md`.

### `ontology.toml` — the vocabulary, and it is EXTENSIBLE

The base file ships with nexus; a project adds `.nexus/ontology.toml`
with two verbs — declare something new (`[node.foo]`, `[edge.bar]`,
`[attribute.baz]`) or **widen** a base entry (`[extend.edge.implements]
range = [...]`). Widening is monotone and narrowing is refused, so no
pass written against the base vocabulary can be invalidated by a
project.

⚠⚠ **Anything asking "is this a real type?" or "is this a placeholder?"
must ask the ONTOLOGY, loaded with the project root — never
`graph.NodeType`.** The enum answers the narrower "does nexus *ship*
this type?" and gets a project's declared type wrong, which is the
entire point of the extension tier. Use `Ontology.node_types` /
`Ontology.placeholder_types`, or `q.placeholder_types` from a query.

This is the single easiest mistake to make here, because the enum is
right in front of you and works on every test fixture. It shipped once
and told a project to "declare it in ontology.toml" when it already had.

`graph.NodeType`/`EdgeType` carry the *names*, the base `ontology.toml`
carries the *semantics*, and a test pins the two so neither drifts.

## The id grammar

```
<domain>:<type>:<name>          py:function:pkg.mod.solve
                                std:file:api/data
                                math:equation:transport-balance
```

**The type segment IS the node's type.** `server.py` advertises this to
every MCP client, so it is a contract, not a convention. One exception,
and it is principled: a **placeholder** (`external` / `unresolved`) keeps
the type its NAME denotes in the id while `type` records that nothing
was found — `py:function:foo` typed `unresolved` is correct.

- Build ids with `_mappings.node_id()` / `doc_node_id()`. Never
  f-string one; that is how `py:property:` and a two-segment `doc:`
  namespace got in, and how one page ended up as two nodes.
- A kind that is not a type goes in **metadata**, not the id. Storing
  `prf:theorem:` cost a 15-way prefix scan plus a whole-graph fallback
  every time a bare `:prf:ref:` had to resolve; `prf_type` in metadata
  costs one lookup.
- `merge.check_node_types` reports undeclared segments once per build.

## Two producers, one symbol

`ast_analyzer.py` reads the source; `extractors.py` reads the Sphinx
domains. They see the same symbols and **disagree**, and the arbiter is
`_mappings.DOMAIN_TYPE_MAP` — one line per (domain, objtype).

- A producer's own vocabulary (`property`, `label`, `doc`, `func`) must
  be translated *before* it reaches an id, or the same symbol gets two
  nodes with its edges split between them.
- `merge.merge_graphs` unions attributes with the AST winning per key —
  it read a five-key whitelist until 2026-08 and silently dropped
  `decorators`, `verifies`, `catches`, `vv_level` for every symbol
  Sphinx had also seen. An **unset** field is not knowledge: `""` must
  not displace a real value, and `False` must still cross.
- Ask "which producer OWNS this fact?" before adding a type. Whether a
  class is an exception is a fact about its bases — the AST's — so a
  type only Sphinx could assign was the wrong shape and is retired.

## A tool answer lands in a context and stays there

Payload size is a correctness property here, not a nicety.

- Every MCP reply passes `server._fit_budget`, capped by
  `[replies].max_characters`. Over-budget replies are trimmed and carry
  a `truncated` block with the true totals.
- Node dicts are compacted in `_serialize.to_dict`: anything derivable
  from the id is dropped. Keep it that way — it is the most repeated
  structure in the whole surface.
- Rank placeholders LAST. "What does `solve_sn` call?" is not usefully
  answered with `isinstance` and `float`.
- Prefer counts plus a pointer over payloads. `session_briefing` is an
  index; it was 10,564 tokens before it was one.

## Design invariants (violate knowingly or not at all)

- **A graph database is a snapshot of ONE checkout.** Every graph-write
  site stamps `metadata["provenance"]` via `workspace.stamp_provenance`.
  New write paths MUST stamp too.
- **A `GraphQuery` carries the `Workspace` it was loaded from.** Derived
  per-checkout state hangs off it as a `cached_property` — `positions`,
  `settings`, `ontology`, `placeholder_types` — because the object is a
  pure function of (graph, root) and therefore IS the cache key.
- **One server process serves one agent session**, so the active
  workspace is process-local; `use_workspace` swaps it atomically under
  `_reload_lock`.
- **Failure tolerance at tool-call time**: git missing, db corrupt, file
  vanished → degrade, never raise out of an MCP tool.
- **AST analysis never crosses into a nested git tree** (worktree /
  submodule / clone) — that was a 51% graph-contamination bug.
- **Producer-side normalization**: stamp/derive at the write site, not
  in each consumer.
- **A marker never conjures what it names.** `verifies` and `catches`
  resolve onto nodes declared elsewhere (`.. math:: :label:`,
  `.. error-entry::`) and warn when absent. If a typo could mint its own
  target, a miss reads as coverage.

## Traps — the shapes that cost real time here

- **"Nothing found" and "I looked in the wrong place" must not print the
  same thing.** Any consumer that can return empty needs to name what it
  looked for. This has bitten at least nine times in this codebase.
- **A gate that cannot fail.** Before trusting green, ask what input
  existing *today* it rejects. Several gates here were written against
  fixtures blind to the axis under test — and a fixture is blind on the
  axis where its author had only one example (one path spelling, one
  graph state, one file layout). A fixture more regular than the world
  is the warning sign.
- **Shadowing a module-level name with a local.** `for node_id in ...`
  inside a function that also calls `node_id(...)` makes the callable
  local to the whole function → `UnboundLocalError`. Pyright flags it;
  it is not the known cross-tree import noise.
- **`asdict()` flattens nested dataclasses before you can inspect
  them.** Walk `fields()` if you need to treat a nested type specially,
  or your transform silently no-ops one level down.
- **One sentinel, two meanings.** `""` is "no type at all" on a stored
  node and "type equalled the id segment, dropped as redundant" in a
  compacted result. Folding them inverted a ranking.
- **Retiring a symbol is three searches**: graph callers, text grep
  across code + tests + `docs/`, and direct constructors. Sphinx does
  not warn on unresolved Python-domain roles at any severity.

Measurement discipline for all of the above lives in
`.claude/rules/measurement-discipline.md` — read it before claiming a
change worked.

## Architecture map

```
sphinxcontrib/nexus/
    __init__.py       Sphinx extension entry (build-finished writes the
                      graph, stamps provenance), __version__, pass ORDER
    graph.py          KnowledgeGraph (nx.MultiDiGraph), NodeType/EdgeType
    project.py        .nexus/config.toml — ProjectConfig, DEFAULTS,
                      KNOWN_KEYS, graph_db_in (the derived db location)
    ontology.py       .nexus/ontology.toml — the vocabulary and its
                      extension tier; check_edge, node_types,
                      placeholder_types
    _mappings.py      node_id/doc_node_id, DOMAIN_TYPE_MAP (the producer
                      arbiter), reference resolution
    extractors.py     Sphinx BuildEnvironment → nodes/edges
    ast_analyzer.py   Python AST walker; prunes nested git trees
    merge.py          union Sphinx + AST, infer implements, write
                      verifies/catches edges, check_node_types
    position.py       PositionIndex — (file, line) → node, for the
                      navigator (`enclosing`) and tracers (`defined_at`)
    query.py          GraphQuery: the read surface, 20+ methods
    _serialize.py     to_dict/to_json, node compaction, the assemblers
    server.py         MCP server; one workspace per process; the tool
                      boundary (journal, staleness, payload budget)
    cli.py            `nexus` CLI (setup, analyze, serve, workspaces, …)
    brief.py          edit-time file brief — SQL only, never loads the graph
    directives.py     .. verifies/implements/discretizes/error-entry ..
    registry.py       verification edges declared in project YAML
    runtime.py        runtime overlays (cprofile/coverage/viztracer)
    workspace.py      checkout ↔ graph pairing, provenance, canonical_path
    export.py         JSON + SQLite (FTS5) export/import
    fingerprint.py    body shingles for twin-path detection
    ingest.py         LLM-powered paper/PDF ingestion
    install.py        `nexus setup` — hooks, skills, settings
    visualize.py      interactive force-directed graph HTML
    skills/           Claude Code skills   ┐
    commands/         slash commands       ├ installed into a consumer
    hooks/            shell hooks          │ project by `nexus setup`
    rules/            behavioural rules    ┘
```

⚠ Those last four are **shipped payload, not this repo's own config**.
Editing `sphinxcontrib/nexus/rules/nexus-tools.md` changes what every
consumer project gets told; editing `.claude/rules/` changes how an
agent behaves *here*. They are easy to confuse and they have opposite
audiences.

## Drift surfaces (guarded by tests — keep them green)

- README "MCP Tools (N)" header and tool bullets ↔ MCP registry
  (`tests/test_server_registry.py`).
- `graph.NodeType`/`EdgeType` ↔ base `ontology.toml`
  (`tests/test_ontology.py`), bidirectional.
- `project.DEFAULTS` ↔ `project.KNOWN_KEYS`
  (`tests/test_project_config.py`).
- Version single-sourced in `sphinxcontrib/nexus/__init__.py`;
  `pyproject.toml` declares it `dynamic`.

## Release process

1. Bump `__version__` in `sphinxcontrib/nexus/__init__.py`.
2. Update `CHANGELOG.md` (Added / Fixed / Changed).
3. Merge to `main` via PR (no direct commits to main).
4. Tag `vX.Y.Z` — CI publishes to PyPI on tags.
5. Delete the merged feature branch.

## Project rules (`.claude/rules/`)

Behavioural rules live in `.claude/rules/` — in-repo and
instruction-authority, so they survive a clone and bind every session
rather than one machine. Everything else under `.claude/` is local.

- **`measurement-discipline.md`** — how to know a change worked. A count
  is not a fitness function; a byte-identical result is inert, not
  conservative; verify the test against the unfixed code; measure an
  issue's premise before implementing it; prove a gate can fail before
  trusting it green. Written from the 0.17.0 cycle, where the
  dead-reference count fell twice *because resolution got worse*.

## Git workflow

- Branch naming: `<type>/<topic>` (`feature|fix|docs|refactor|test|chore`).
- Conventional Commits: `<type>(<scope>): <summary>`.
- `main` stays green.
