# Changelog

All notable changes to sphinxcontrib-nexus.

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
