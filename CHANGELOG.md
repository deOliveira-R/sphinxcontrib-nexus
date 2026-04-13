# Changelog

All notable changes to sphinxcontrib-nexus.

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
