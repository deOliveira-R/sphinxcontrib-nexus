# Changelog

All notable changes to sphinxcontrib-nexus.

## 0.8.2 — 2026-04-13

Fixes nexus#3 — re-exported classes appearing as multiple parallel
graph nodes. ORPHEUS reported ``Mesh1D`` showing up as four
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

### Tests

281 → 290 (+9). New regression coverage split across:

- ``test_reexport.py`` (new, 6 assertions) — pins every bug shape
  in isolation against a synthetic 3-level re-export project.
- ``test_fixture_e2e.py`` (+3 assertions) — pins the same shapes
  end-to-end through a real ``sphinx-build`` by adding a ``Mesh``
  class to ``solver_pkg.helpers``, re-exporting it via
  ``solver_pkg.__init__``, and having ``solver.build_mesh`` call
  it through the re-export path.

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
