# Changelog

All notable changes to sphinxcontrib-nexus.

## 0.12.0 — 2026-06-11

Git-worktree (workspace) support. A graph database is a snapshot of
ONE checkout, but agent harnesses (Claude Code) spawn the MCP server
against the MAIN checkout and never restart it when a session moves
into a worktree — so worktree sessions silently query the wrong
branch's graph. Observed in production: the main checkout's graph was
26 days stale while the active worktree rebuilt its own graph on
every docs build; every session answered from the stale one. This
release makes the mismatch visible and switchable.

### Added

- **Provenance stamping.** Every graph-write site (the Sphinx
  ``build-finished`` handler and ``nexus analyze``) now stamps
  ``graph.metadata["provenance"]`` with ``source_root``, ``built_at``,
  and — when the tree is a git checkout — ``git_branch``,
  ``git_commit`` (short), ``git_dirty``. Graphs are self-describing:
  any consumer can tell which tree, branch, and commit a database is
  a snapshot of. Non-git trees still get ``source_root``/``built_at``.
- **``workspace`` module.** ``Workspace`` (a checkout paired with its
  graph database via a root-relative layout), ``discover()``
  (enumerate all checkouts via ``git worktree list --porcelain`` and
  report each one's graph status, branch, and provenance),
  ``git_provenance()``, ``list_worktrees()``, ``stamp_provenance()``.
  All git access is subprocess-based and failure-tolerant: missing
  git / non-repository roots degrade to "active workspace only",
  never to a tool-call exception.
- **``read_sqlite_metadata()``** in ``export`` — metadata-table-only
  peek (no node/edge loading) so discovery can read provenance from
  every sibling database cheaply. Deliberately not gated on
  ``schema_version`` (the metadata table is where the version itself
  lives); ``load_sqlite`` now delegates its metadata pass to it.
- **MCP tool ``workspaces``** — list every checkout (main + linked
  worktrees) with branch, graph presence, build time, provenance,
  and which one is active.
- **MCP tool ``use_workspace(root)``** — atomically re-point the
  server at the graph built inside another checkout. Safe because
  each agent session owns its server process (verified: Claude Code
  spawns one ``nexus serve`` per session). Fails loud with a
  build-it-first hint when the target checkout has no graph; the
  active graph is untouched by a failed switch. Auto-reload tracks
  the new database afterwards; a workspace-switch guard inside the
  reload lock prevents a stale pre-lock ``stat`` from clobbering a
  freshly switched graph. Accepts a worktree directory name or a
  branch name in addition to an absolute root path
  (``workspace.resolve_checkout_root``); unknown or ambiguous names
  fail with the list of known checkouts.
- **``session_briefing`` workspace block** (MCP tool and
  ``nexus://briefing`` resource). Reports the active workspace's
  provenance and sibling checkouts, with warnings when (a) the graph
  carries no provenance stamp, (b) the graph was built on a different
  branch than the checkout now has, or (c) sibling worktrees carry
  graphs of their own — the wrong-tree tripwire fires on the
  session's first turn instead of never.
- **CLI ``nexus workspaces``** — same discovery, human-readable.
- **MCP tool ``node_at(file, line)``** — map a file position to the
  innermost enclosing graph node (module-scope positions return the
  module node). The bridge from position-speaking tools — language
  servers, stack traces, editors — into the graph: resolve a symbol
  precisely with LSP, then hand its position here and continue with
  ``context`` / ``impact`` / ``provenance_chain`` for the
  cross-domain picture LSP cannot see. Tool count is now 28.
- **CI runs pyright** alongside the pytest matrix (which now includes
  Python 3.14); the type check builds the same ``./.venv`` layout
  ``pyrightconfig.json`` points at locally.
- **Usage journal.** Every MCP tool call appends one JSON line
  (timestamp, tool, args repr-truncated, duration, outcome, active
  workspace, pid) to ``~/.nexus/usage.jsonl`` — ``NEXUS_USAGE_LOG``
  overrides the path, an empty value disables. The self-observation
  channel: tool adoption gets evaluated from recorded behavior instead
  of anyone's memory. Journaling is failure-tolerant and never blocks
  a tool call; the registration wrapper is schema-transparent
  (guarded by test).
- **behavioral-auto-regression skill updated to the current prompt
  landscape**: the historical Grep ``ALWAYS`` directive is gone from
  current agent prompts; the live regression vectors are habit
  (counter: claim the "dedicated tools" category), search delegation
  to generic grep-wielding sub-agents (counter: project explorer
  agent + deny built-in Explore), and deferred MCP tools (counter:
  ToolSearch loading — deferral is not unavailability).
- **Roots-based workspace auto-alignment.** ``session_briefing`` asks
  the client (MCP ``roots/list``) which directory the session was
  launched from; when that lies inside a different checkout that has
  a graph, the server switches to it automatically and reports the
  switch under ``workspace.auto_align`` (or the build-it-first hint
  when the checkout has no graph). Failure-tolerant: clients without
  roots support, foreign paths, and already-aligned sessions all
  degrade to "no block, no switch". Verified over real MCP stdio
  against ORPHEUS: a roots-advertising client launched in a worktree
  gets that worktree's graph on its first briefing with no manual
  call; mid-session worktree entry still uses ``use_workspace``
  (roots updates there are undocumented client behavior).

### Fixed

- **AST analysis no longer ingests nested git working trees.**
  ``analyze_directory`` prunes any subdirectory carrying a ``.git``
  entry (gitlink file = linked worktree / submodule, directory =
  vendored clone); the analyzed root itself is exempt. Found while
  end-to-end-testing this release on ORPHEUS: the main checkout's
  graph contained 30,049 nodes of which **15,420 (51%) were
  worktree copies** (``py:attribute:.claude.worktrees.<name>.orpheus...``)
  — every Claude Code session worktree's full source tree was being
  re-analyzed under mangled module paths, polluting query results,
  caller counts, impact analysis, and god_nodes. The clean rebuild
  matches the worktree-side build node-for-node class. Re-include a
  nested tree deliberately via ``nexus_extra_source_dirs`` if you
  ever need one analyzed.

- **MCP ``impact`` / ``neighbors`` validate ``direction`` at the tool
  boundary** — an invalid value now returns a self-describing error
  payload instead of leaking a bare string into ``Literal``-typed
  query internals.
- **Branch-scope diffs resolve the repository's actual default
  branch.** ``detect_changes`` / ``retest`` / ``session_briefing``
  with ``scope="branch"`` used a hardcoded ``main``-then-``master``
  fallback that conflated "ref does not exist" with "no .py files
  changed" and never saw unconventionally named defaults. The base is
  now ``workspace.default_branch()``: the ``origin/HEAD`` symbolic
  ref when set, else the first of ``main``/``master`` that exists.
- **Edge-key collision when wrapping an existing graph.**
  ``KnowledgeGraph`` now accepts an existing ``nx.MultiDiGraph`` and
  continues the auto-incremented edge-key sequence past its highest
  integer key; previously both wrap sites (``dict_to_graph`` and the
  MCP ``ingest`` tool's private-attribute poke) reset the counter to
  0, so a later ``add_edge`` between an already-connected pair could
  silently UPDATE an existing parallel edge instead of adding one.
  ``GraphQuery`` keeps the ``KnowledgeGraph`` it was built from
  (``knowledge_graph`` property, metadata included) so the ``ingest``
  tool mutates the real object instead of reconstructing a wrapper.

### Changed

- **Repo is pyright-clean** (``pyrightconfig.json`` points at the
  project venv; CI-checkable). Fixed along the way: a quoted
  forward-reference in ``cli.py``, ``VerificationGapsResult.filters``
  typing (it carries an ``int`` count), edge-attribute restoration in
  ``export.load_sqlite`` now goes through ``g.edges[u, v, key]``,
  ``_add_docstring_refs`` declares the actual ``ast.get_docstring``
  domain instead of ``ast.AST``.
- **Server state model.** The four smeared module globals
  (``_db_path``, ``_project_root``, ...) collapse into one named
  concept: ``_workspace: Workspace``. ``serve()`` resolves its paths
  at startup.
- **``nexus setup`` MCP config template** now anchors ``command``,
  ``--db``, and ``--project-root`` on ``${CLAUDE_PROJECT_DIR:-.}``
  instead of bare relative paths — Claude Code sets that variable for
  spawned MCP servers, so resolution no longer depends on the
  (unspecified) spawn cwd; the ``:-.`` fallback keeps other MCP
  clients working.
- ``nexus serve --help`` no longer hardcodes a tool count.
- **Version is single-sourced** from ``__init__.__version__``;
  ``pyproject.toml`` declares ``dynamic = ["version"]`` (flit extracts
  the literal at build time). Previously the two copies had to be
  bumped in lockstep — pure drift surface.

## 0.11.0 — 2026-04-14

Public escape hatch for downstream projects that need to keep
directories out of AST analysis without monkey-patching private
internals. Closes #13.

### Added

- **``nexus_source_exclude_patterns``** Sphinx config value
  (default ``[]``). POSIX glob patterns evaluated with ``fnmatch``
  against paths relative to each source directory — same semantics
  as ``nexus_test_patterns``. Patterns are appended to the
  exclusion list passed to ``analyze_directory`` for both the main
  source pass and the ``nexus_extra_source_dirs`` pass.

  Concrete motivating case (ORPHEUS): ``student_resources/``
  contains pedagogical scripts that intentionally shadow
  ``orpheus.*`` class names. Sphinx's py-domain xref resolver was
  matching the short names against both the real package and the
  tutorial copies, so the AST extractor recorded ``documents``
  edges to both — which then made the staleness tracker count the
  tutorial file's mtime against every API page that documented an
  affected ``orpheus`` module. With this option, downstream
  projects can drop the shadowing source out of analysis from
  ``conf.py`` directly:

  ```python
  nexus_source_exclude_patterns = ["student_resources/*"]
  ```

  No more reaching into ``_BASE_EXCLUDE_PATTERNS``.

### Changed

- **``_compute_exclude_patterns``** gained an optional
  ``user_patterns`` parameter (default ``None``). When provided,
  the patterns are appended unconditionally — independent of the
  ``analyze_tests`` gate, so user excludes still apply when tests
  are being analyzed. Pre-0.11 callers that pass only positional
  ``analyze_tests``/``test_patterns`` continue to work unchanged.

## 0.10.0 — 2026-04-14

LLM-orientation pass on ``session_briefing``. Three additive fields
teach the agent the node-ID grammar on the first turn, surface the
handful of nodes most likely to be queried next, and emit a paste-
ready ``ToolSearch`` invocation for Nexus's deferred MCP tools.
Drop-in upgrade from 0.9.0: no existing field was removed or
re-shaped.

### Added

- **``id_grammar``** in ``BriefingResult``. For each
  ``(domain, type)`` pair actually present in the graph (excluding
  the noise types ``external`` and ``unresolved``), emits one
  representative node with the median degree in that bucket.
  Max-degree nodes are already in ``god_nodes``; min-degree nodes
  are obscure; the median is the useful teaching example. Examples
  are sorted by ``(domain, type)`` ascending and are deterministic
  across calls on an unchanged graph.
- **``hot_nodes``** in ``BriefingResult``. Nodes that (a) appear in
  ``recent_changes`` (same data ``session_briefing`` already uses,
  i.e. ``detect_changes(scope="branch")`` against main/master — no
  separate window), (b) have degree at or above the graph median
  so "hot" implies both recent *and* central, and (c) are not
  already in ``god_nodes[:5]`` (to avoid duplicating the signal
  that field already carries). Top 5 by degree, tiebreak on id.
  Each entry carries a free-form ``reason`` drawn from a small
  stable vocabulary (``"modified in current branch"``, etc).
- **``preload_hint``** in ``BriefingResult``. A static, graph-
  independent ``select:`` string listing the eight most-used Nexus
  MCP tools (``query``, ``callers``, ``callees``, ``context``,
  ``impact``, ``provenance_chain``, ``shortest_path``,
  ``neighbors``). Paste it into a single ``ToolSearch`` call on
  the first turn that touches Nexus instead of loading schemas
  one tool at a time.

All three fields flow through the existing ``to_dict`` /
``asdict`` path as nested dataclasses, so ``session_briefing`` MCP
and CLI responses pick them up automatically — no serializer
changes required.

### Round-trip contract verified

Before wiring ``id_grammar``, ran an empirical probe that walked
every ``god_nodes`` entry from a realistic briefing and fed its
``id`` back into ``assemble_context``. All five round-tripped
cleanly — confirming that ``NodeResult.id`` (the
``<domain>:<type>:<name>`` graph key) is directly accepted by
``context`` and, by extension, every other MCP tool that takes a
node id. The contract that ``id_grammar.examples[*].id`` is
usable verbatim now has a test
(``test_briefing_id_grammar_round_trip``) pinning it.

## 0.9.0 — 2026-04-13

Session 4 of the ORPHEUS V&V integration: infrastructure hardening.
No blocking workflow changes — this is QoL, correctness, and
operator-debuggability work built on top of the v0.8.x behavior.
Drop-in upgrade from 0.8.2.

### Added

- **SQLite schema version field** (``SCHEMA_VERSION = 1``) written
  on every ``write_sqlite`` call into the ``metadata`` table.
  ``load_sqlite`` validates it via ``_check_schema_version`` and
  raises ``SchemaVersionError`` when the stored version exceeds
  this build's ``SCHEMA_VERSION``. Missing key is tolerated (pre-
  schema_version databases are treated as v1). A user-supplied
  ``schema_version`` in ``graph.metadata`` cannot override the
  authoritative value.
- **V&V integration docs** in the README walk through the full
  declarative-verification pipeline end-to-end: pytest markers →
  AST metadata → TESTS/IMPLEMENTS edges → audit/gaps queries.
  Includes copy-paste examples for each of the four declaration
  paths (markers, directives, registry YAML, query consumption).
- **Parallel-build regression test**:
  ``test_parallel_build_matches_serial`` in the fixture harness
  runs a ``sphinx-build -j 2`` against ``minimal_project`` and
  pins that it produces the same node set, edge count, and edge-
  type distribution as a serial build. The extension has always
  declared ``parallel_write_safe=True`` but the claim was never
  load-bearing on a test until now.

### Changed

- **``_SPHINX_ROLE_RE`` / docstring-role parser hardened.**
  Introduces ``_parse_role_target`` that normalizes the raw
  backtick content into the resolvable target, handling four
  cases the old parser missed:

  1. ``:role:`!foo``` (suppress-link convention) → returns
     ``None``, no edge emitted.
  2. ``:role:`title <target>``` → returns ``target`` (display
     title is presentation noise).
  3. ``:role:`~pkg.mod.foo``` → strips the tilde and returns the
     dotted name. The tilde inside a title-target form is also
     handled.
  4. Plain ``:role:`foo``` → returned as-is.

  Before this change, ``:func:`compute fn <pkg.mod.compute>``
  produced a target id of ``py:function:compute fn <pkg.mod.compute>``
  — unresolvable garbage — and ``:func:`!noref`` created a
  ``py:function:noref`` edge despite the suppression intent.

- **``_reload_if_stale`` is now thread-safe and failure-tolerant.**
  Wraps the ``load_sqlite`` call in a try/except so a corrupt
  DB, schema-version rejection, or mid-write race keeps the
  previous in-memory snapshot serving instead of crashing the
  MCP tool dispatch. A module-level ``threading.Lock`` serializes
  concurrent reload attempts; a double-check of the mtime under
  the lock avoids redundant loads. Failure cases log at WARNING
  level with the DB path and raised exception.

### Notes

- 294 → 316 tests (+22). Split across:
  - ``test_export.py`` (+6 schema version)
  - ``test_ast_analyzer.py`` (+9 role-target parse + end-to-end)
  - ``test_fixture_e2e.py`` (+1 parallel-build equivalence)
  - ``test_reload.py`` (new file, 6 reload failure / lock tests)
- No API or schema changes. Session 4 is pure hardening on top
  of 0.8.2 behavior.

## 0.8.2 — 2026-04-13

Fixes nexus#3 — re-exported classes appearing as multiple parallel
graph nodes. Two-round fix after the first-round implementation
was flagged by ORPHEUS cross-validation as regressing the
canonical class type. ORPHEUS reported ``Mesh1D`` showing up as four
distinct nodes in the 0.6.0 graph (two ``py:class:``, one
``py:function:``, one unresolved phantom). This release collapses
all four bug shapes into a single canonical class via a new
leaf-name-plus-path-overlap fold pass.

### Fixed

- **nexus#3** — ``analyze_directory`` now runs a new
  ``_canonicalize_phantoms`` pass after ``_classify_phantom_nodes``
  that folds re-export and mis-typed phantoms into their canonical
  AST counterparts. The pass:

  1. Builds a leaf-name index over every concrete
     class/function/method node.
  2. For each phantom (``unresolved``/``external``/untyped with a
     dotted name), looks up the leaf name and filters candidates
     to those whose module path overlaps the phantom's via a
     prefix OR suffix relationship.
  3. If exactly one candidate survives, retargets all incoming
     and outgoing edges onto the canonical and drops the phantom.

  The module-path-overlap guard is what distinguishes "re-export
  or short-import of the same symbol" from "genuine external
  leaf-name collision". A reference like ``numpy.ndarray`` does
  NOT fold into a project-local ``local.ndarray`` because
  ``numpy`` is neither a prefix nor a suffix of ``local``; but
  ``pkg.geometry.Thing`` DOES fold into
  ``pkg.geometry.mesh.Thing`` because the former's module path
  is a prefix of the latter's.

  All four ORPHEUS bug shapes are handled by the single pass:

  - ``py:class:orpheus.geometry.Mesh1D`` (re-export via __init__)
    — folded via prefix overlap.
  - ``py:function:orpheus.geometry.Mesh1D`` (class called as
    Call, hardcoded ``py:function:`` prefix) — folded via prefix
    overlap.
  - ``py:class:geometry.mesh.Mesh1D`` (short-import phantom from
    test files that put the project root on ``sys.path``) —
    folded via suffix overlap.
  - ``py:class:orpheus.geometry.mesh.Mesh1D`` (canonical) —
    untouched; remains the single surviving node.

### Scope note

The handoff listed an optional ``nexus_package_aliases`` config
for projects with weird import layouts. The leaf-name-plus-
overlap rule already handles every bug shape the ORPHEUS repro
exhibited (including the short-import case via the suffix-match
branch), so the config isn't needed. Leaves the API smaller; can
be added later if a real project hits a case this pass can't
resolve.

### Round 2 — type upgrade during merge and type-ranked fold

ORPHEUS cross-validation of the first-round fix found three
interacting bugs:

1. **``merge_graphs`` didn't upgrade types.** When Sphinx had a
   placeholder ``py:class:pkg.mod.Thing`` with
   ``type=unresolved`` (from a pending_xref that couldn't
   resolve at parse time, or from NetworkX auto-creating an
   edge target before domain extraction ran) AND the AST side
   had the same id typed as ``class`` with ``file_path``, the
   merged node kept ``type=unresolved``. Downstream type
   filters broke and the canonicalization leaf-index skipped
   the canonical.
2. **The fold's canonical recognition was too strict.** Even
   after merge was fixed, a node whose ID prefix and
   ``file_path`` proved a concrete type could still be
   bypassed if some earlier classification step had stamped
   its type attr as ``unresolved``. The leaf-index only looked
   at the type attr, so such nodes weren't considered canonical.
3. **Bare-name phantoms and same-leaf ambiguity.** Phantoms
   with a bare leaf name (e.g. ``py:function:Mesh1D`` from a
   ``from pkg import Mesh1D`` call site) had no module path to
   feed into the overlap filter and were always skipped. And
   when multiple canonical candidates shared a leaf, the fold
   picked by iteration order instead of by concreteness.

Round-2 fixes:

- ``merge_graphs`` step 1 consults a ``_MERGE_TYPE_RANK`` table
  and upgrades the type whenever AST's type is more concrete
  than Sphinx's. ``class > exception > method > function >
  type > attribute > data > module > external > unresolved``.
  Downgrades are explicitly protected against.
- ``_canonicalize_phantoms`` runs a new
  ``_upgrade_types_from_signals`` pre-pass that inspects every
  node whose ID prefix (``py:class:``, ``py:function:``,
  ``py:method:``, …) plus ``file_path`` signals an
  authoritative concrete type, and upgrades the type attr in
  place. Bare phantoms without ``file_path`` are untouched.
- The fold's canonical-selection step now picks by concreteness
  rank tie-broken on ``file_path`` presence. Genuinely
  ambiguous leaves (two candidates tied on both signals) are
  left alone — no auto-collapse.
- Bare-name phantoms (no dots in name) now fold into the unique
  leaf-matched canonical across the whole graph without the
  module-path-overlap filter.
- ``_run_ast_analysis`` invokes ``_canonicalize_phantoms`` a
  second time after the last ``merge_graphs`` call, so Sphinx-
  side phantoms that the per-directory AST pass couldn't see
  get collapsed against their merged canonicals. The pass is
  idempotent.

### Fixture expansion

``tests/fixtures/minimal_project/conf.py`` now enables
``sphinx.ext.autodoc`` and ``theory/solver.rst`` runs
``.. autoclass:: solver_pkg.helpers.Mesh`` plus
``.. autofunction::`` blocks for the solver functions. This
ensures the e2e harness exercises the full Sphinx
domain-objects → AST merge → canonicalization pipeline that the
ORPHEUS build runs through, not just the AST-only path.

### Tests

281 → 294 (+13). New regression coverage split across:

- ``test_reexport.py`` (8 assertions total) — pins every bug
  shape from both round 1 and round 2 in isolation:
  - round 1: synthetic 3-level re-export project
  - round 2: ``test_canonical_with_unresolved_type_but_file_path_is_foldable_target``
    simulates the ORPHEUS shape A, ``test_bare_name_phantom_folds_to_unique_canonical``
    pins shape C1.
- ``test_merge.py`` (+2 assertions) —
  ``test_merge_upgrades_placeholder_type_from_ast`` pins the
  merge-layer type upgrade, ``test_merge_does_not_downgrade_concrete_type``
  guards the inverse.
- ``test_fixture_e2e.py`` (+3 assertions from round 1) — pins
  the round-1 re-export shape end-to-end through a real
  ``sphinx-build``.

## 0.8.1 — 2026-04-13

Two bug fixes caught by ORPHEUS cross-validation of 0.8.0.

### Fixed

- **nexus#7**: explicit-source edge dedup. Every write-time pass
  (``merge.write_verifies_edges``, ``directives.apply_pending_edges``,
  ``registry._apply_verifications`` / ``_apply_implementations``)
  previously only skipped duplication against its **own** source.
  A ``(test, equation)`` pair declared by both a
  ``@pytest.mark.verifies`` decorator AND a matching registry
  entry therefore produced two parallel ``tests`` edges, inflating
  per-equation test counts by one (the exact 86 → 87 regression
  reported from the ORPHEUS ``matrix-eigenvalue`` equation).

  All four passes now skip if ANY edge of the same type has a
  non-inference source already present. ``source="inferred"``
  edges remain weak and can still coexist with explicit assertions.

- **Query-time dedup layer** in ``verification_coverage`` tracks
  seen ``(src, tgt)`` pairs per edge-type so each ``(code,
  equation)`` or ``(test, equation)`` relationship contributes at
  most one entry to the result. This is defense-in-depth for
  graphs loaded from older nexus versions that may still carry
  duplicate edges.

- **nexus#8**: module/class-level ``pytest.mark.*`` propagation
  now requires the target function to qualify as a test. Previously
  a module ``pytestmark = pytest.mark.verifies("eq-1")`` tagged
  **every** function in the file — including private helpers like
  ``_build_homogeneous_mesh`` — and ``write_verifies_edges`` then
  wrote spurious ``tests`` edges from those helpers to the
  equation. ORPHEUS's declared coverage inflated by ~5-10% because
  of this. Inherited markers are now gated on ``is_test=True``
  (name matches ``test``/``test_*`` AND the file matches the
  project's test-pattern globs). Function-level decorators still
  apply unconditionally.

### Notes

- 272 → 281 tests (+9). New assertions split across
  ``test_registry.py`` (+4 write-time dedup), ``test_query.py``
  (+2 query-time dedup), and ``test_ast_analyzer.py`` (+3
  helper-propagation regressions).
- No API or schema changes. Drop-in upgrade from 0.8.0.
- The ``_visit_source`` helper in ``tests/test_ast_analyzer.py``
  gains an ``is_test_file`` parameter so Session 2 propagation
  tests can keep exercising their contract under the tighter gate.

## 0.8.0 — 2026-04-13

Session 3 of the ORPHEUS V&V integration: non-LLM verification
registry, Sphinx directives for declarative edges, and extended
audit/gaps surface.

### Added

- **Non-LLM verification registry** (``sphinxcontrib.nexus.registry``).
  A deterministic YAML-driven path for declaring ``TESTS`` and
  ``IMPLEMENTS`` edges independent of the LLM-powered
  ``ingest.py``. Schema is ``version: 1`` with ``verifications``
  and ``implementations`` lists; each entry names a test or function
  id and the equation labels it covers. Registry edges are tagged
  ``source="registry"`` with confidence 1.0 and honored by the
  ``_infer_implements`` guard. Missing nodes log warnings and skip
  rather than raising. Loader is idempotent.
- **Config: ``nexus_verification_registry``** (list of paths
  relative to ``conf.py``, default ``[]``). Paths point at YAML
  files loaded during ``_run_ast_analysis``, after the AST merge
  and before ``_infer_implements``.
- **Sphinx directives** ``.. verifies:: <label> :by: <symbol>`` and
  ``.. implements:: <label> :by: <symbol>``. Declarative edges
  expressed in theory docs rather than in test code or YAML. The
  ``:by:`` option accepts a bare dotted name
  (``orpheus.sn.solve_sn``) or an already-prefixed node id; if
  omitted, the directive falls back to ``env.ref_context``
  inspection so usage nested inside ``.. py:function::`` /
  ``.. autofunction::`` picks up the signature automatically.
- **Incremental-build-safe directive queue.** The pending-edge
  registry is keyed by docname and persists across incremental
  builds. An ``env-purge-doc`` handler drops stale entries when a
  doctree is about to be re-parsed; an ``env-merge-info`` handler
  folds parallel-build worker envs back into the main env. Fixes
  the same caching trap that bit the 0.7.0 upgrade.
- **``verification_audit`` grouping**. The query method and its MCP
  / CLI exposures gain two keyword-only arguments:
  ``group_by`` (one of ``"level"`` / ``"module"`` / ``"equation"``)
  which buckets the flat ``gaps`` list into a dict keyed by the
  chosen dimension; and ``include_tests`` which populates
  ``summary["tests_declared"]`` / ``summary["tests_inferred"]`` so
  consumers can weigh how much verification is declarative vs.
  heuristic.
- **``verification_gaps``** — a new query method surfacing three
  buckets:
  - ``untagged_tests`` — test nodes with no ``vv_level`` marker
  - ``unverified_equations`` — equations in the
    ``implemented`` / ``documented`` bucket
  - ``missing_err_catchers`` — members of an optional
    ``error_catalog`` set that no test's ``catches`` metadata
    references
  Filters by ``module`` and ``level``. Exposed as both a new MCP
  tool and a ``nexus gaps`` CLI subcommand.
- **``tests/fixtures/minimal_project/registry.yaml``** and a matching
  directive block in ``theory/solver.rst`` — the e2e harness pins
  both the registry pipeline and the directive lifecycle against a
  real ``sphinx-build``.

### Changed

- **MCP tool count: 24 → 25** (``verification_gaps`` added).
- **CLI subcommand count: 28 → 29** (``nexus gaps`` added).
- ``nexus_verification_registry`` paths resolve relative to
  ``app.srcdir`` (the directory holding ``conf.py``) so config
  entries colocated with theory docs work naturally. Users with a
  standard ``docs/conf.py`` layout can point at a repo-root
  registry via ``"../verification.yaml"``.
- ``verification_audit`` raises ``ValueError`` on an invalid
  ``group_by`` instead of silently ignoring it.

### Notes

- 251 → 272 tests (+21). Split across ``test_registry.py`` (new,
  17), ``test_directives.py`` (new, 20), extensions to
  ``test_query.py`` (+12), and the fixture harness (+9).
- ``.. verified-by::`` — the third directive the handoff spec
  listed — is NOT in this release. It would need "enclosing
  equation" detection, which is a different Sphinx lifecycle
  problem from py-object detection. Users can write
  ``.. verifies:: label :by: test`` from either side, or use the
  registry YAML, to cover the same relationship.
- PyYAML joins the core dependencies for the registry loader.

## 0.7.0 — 2026-04-13

Session 2 of the ORPHEUS V&V integration: pytest-marker ingestion,
declared TESTS edges, and multi-tier verification coverage.

### Added

- **Decorator parsing in the AST walker.** ``CodeVisitor`` now reads
  ``decorator_list`` on every function, method, and class. A flat
  ``decorators`` tuple of serialized source strings is recorded on the
  node's metadata, plus structured fields extracted by
  ``_parse_pytest_markers``:
  - ``vv_level`` (``"L0"`` / ``"L1"`` / ``"L2"`` / ``"L3"``) — from
    ``@pytest.mark.lN`` or ``@verify.lN(...)`` sugar.
  - ``verifies`` / ``catches`` — tuples of string literals extracted
    from ``@pytest.mark.verifies(...)`` / ``catches(...)`` args, or
    from ``equations=[...]`` / ``catches=[...]`` kwargs on
    ``@verify.lN(...)``.
  - ``slow`` — boolean flag from ``@pytest.mark.slow``.
- **Class- and module-level marker propagation.** ``@pytest.mark.lN``
  on a class or a module-level ``pytestmark = ...`` assignment
  propagates to contained methods / functions. Precedence is
  module < class < function, so a function-level marker always wins.
  Nested classes don't leak state upward.
- **``merge.write_verifies_edges``** — a post-merge pass that turns
  every ``@pytest.mark.verifies("label")`` marker into a real
  ``EdgeType.TESTS`` edge (source ``"pytest.mark.verifies"``,
  confidence 1.0). Missing equations are logged and skipped. The pass
  is idempotent on re-runs.
- **New config ``nexus_infer_implements``** (default ``True``) —
  turns off the token-intersection inference entirely for projects
  with full explicit coverage.
- **New ``TestReference`` dataclass** in ``query.py`` carrying ``id``,
  ``source``, ``confidence``, and ``display_name``. Returned by the
  tiered verification coverage search.
- **``tests/fixtures/minimal_project``** — a tiny self-hosting Sphinx
  project used by ``tests/test_fixture_e2e.py`` to regression-test
  every Session 1 and Session 2 feature through a real
  ``sphinx-build`` invocation.

### Changed

- **``verification_coverage`` uses three-tier test resolution.** Tier 1
  walks ``EdgeType.TESTS`` edges directly (source ``"declared"``,
  confidence 1.0). Tier 2 is the legacy 1-hop ``calls``-from-test
  scan (source ``"heuristic-1hop"``, confidence 0.7). Tier 3 is a
  bounded BFS up the ``calls`` graph from the implementing code node
  (source ``"heuristic-multihop"``, confidence 0.5, ``max_depth=3``).
  Heuristic tiers are only consulted when the declared tier is empty
  for that equation, so registry / marker / directive evidence
  short-circuits inference.
- **``CoverageEntry.tests`` type.** Changed from ``list[NodeResult]``
  to ``list[TestReference]``. Direct consumers that only read ``.id``
  (notably ``verification_audit``) keep working unchanged.
- **Semantic change to the ``verified`` status.** An equation is now
  ``verified`` if it has at least one test (declared or heuristic),
  regardless of whether intermediate implementing code is tracked.
  Previously ``verified`` required BOTH code AND a test, which
  silently demoted declarative-only evidence to ``documented``.
- **``_infer_implements`` now honors pre-existing explicit edges.**
  Any ``(code, equation)`` pair with an ``implements`` or ``tests``
  edge whose ``source`` is not ``"inferred"`` is skipped by the
  token-intersection heuristic so declared evidence never gets a
  duplicate inferred companion.

### Notes

- 202 → 214 tests; the new assertions are split across
  ``test_decorators.py``, ``test_ast_analyzer.py``,
  ``test_merge.py``, ``test_query.py``, and ``test_fixture_e2e.py``.
- No schema change. The new metadata fields ride on
  ``node_attrs`` which is already key-value-typed.
- **Incremental-build gotcha.** Sphinx caches AST analysis per-file,
  so adding a ``@pytest.mark.verifies(...)`` marker to an existing
  test file and re-running ``sphinx-build`` may leave the graph
  looking unchanged — the visitor doesn't re-parse files whose
  source hash hasn't moved relative to Sphinx's own tracking. A
  clean ``rm -rf docs/_build && sphinx-build`` picks up the new
  markers reliably. If you're validating a decorator change end-to-
  end and the graph doesn't show what you expect, rebuild from
  scratch before debugging the visitor.

## 0.6.0 — 2026-04-13

Released earlier on the same day. See the GitHub Release for details
— bug fixes in AST analysis (``:math:`` role routing, nested
``tests/`` exclusion, ``is_test`` false positives), new
``nexus_analyze_tests`` / ``nexus_test_patterns`` config values, and
the end of silent 20-entry truncation on ``verification_coverage`` /
``processes`` with opt-in ``limit`` / ``offset`` pagination.
