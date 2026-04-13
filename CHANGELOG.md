# Changelog

All notable changes to sphinxcontrib-nexus.

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

## 0.6.0 — 2026-04-13

Released earlier on the same day. See the GitHub Release for details
— bug fixes in AST analysis (``:math:`` role routing, nested
``tests/`` exclusion, ``is_test`` false positives), new
``nexus_analyze_tests`` / ``nexus_test_patterns`` config values, and
the end of silent 20-entry truncation on ``verification_coverage`` /
``processes`` with opt-in ``limit`` / ``offset`` pagination.
