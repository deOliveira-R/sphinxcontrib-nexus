"""Unit tests for Sphinx-side config plumbing in ``__init__.py``."""

from __future__ import annotations

from sphinxcontrib.nexus import (
    DEFAULT_TEST_PATTERNS,
    _compute_exclude_patterns,
)


def test_default_test_patterns_cover_common_layouts():
    # Sanity: the defaults should at least catch project-root tests/ dirs
    # and top-level ``test_*.py`` modules.
    assert "tests/*" in DEFAULT_TEST_PATTERNS
    assert any(p.startswith("test_") for p in DEFAULT_TEST_PATTERNS)


def test_compute_exclude_patterns_keeps_tests_when_analyzing():
    patterns = _compute_exclude_patterns(
        analyze_tests=True,
        test_patterns=list(DEFAULT_TEST_PATTERNS),
    )
    assert "docs/*" in patterns
    # No test patterns should be injected when tests are to be analyzed.
    for tp in DEFAULT_TEST_PATTERNS:
        assert tp not in patterns


def test_compute_exclude_patterns_excludes_tests_when_disabled():
    patterns = _compute_exclude_patterns(
        analyze_tests=False,
        test_patterns=["tests/*", "benchmarks/*"],
    )
    assert "docs/*" in patterns
    assert "tests/*" in patterns
    assert "benchmarks/*" in patterns


def test_compute_exclude_patterns_honors_custom_test_patterns():
    # A project with an unconventional test dir name
    patterns = _compute_exclude_patterns(
        analyze_tests=False,
        test_patterns=["qa/*", "integration_tests/*"],
    )
    assert "qa/*" in patterns
    assert "integration_tests/*" in patterns
    # Default tests/ dir should not be excluded if user didn't list it.
    assert "tests/*" not in patterns


def test_compute_exclude_patterns_appends_user_patterns():
    """``nexus_source_exclude_patterns`` is appended unconditionally,
    independent of the analyze_tests gate."""
    patterns = _compute_exclude_patterns(
        analyze_tests=True,
        test_patterns=list(DEFAULT_TEST_PATTERNS),
        user_patterns=["student_resources/*", "legacy/*"],
    )
    assert "student_resources/*" in patterns
    assert "legacy/*" in patterns
    # Base patterns still present.
    assert "docs/*" in patterns
    # Test patterns still excluded by the analyze_tests=True gate.
    for tp in DEFAULT_TEST_PATTERNS:
        assert tp not in patterns


def test_compute_exclude_patterns_user_patterns_combine_with_test_exclusion():
    patterns = _compute_exclude_patterns(
        analyze_tests=False,
        test_patterns=["tests/*"],
        user_patterns=["tutorials/*"],
    )
    assert "tests/*" in patterns
    assert "tutorials/*" in patterns


def test_compute_exclude_patterns_none_user_patterns_is_noop():
    """Passing ``None`` (the default) preserves pre-0.10 behavior."""
    patterns = _compute_exclude_patterns(
        analyze_tests=True,
        test_patterns=list(DEFAULT_TEST_PATTERNS),
        user_patterns=None,
    )
    assert patterns == [
        "docs/*", ".venv/*", "__pycache__/*",
    ]


def test_source_exclude_patterns_skip_directory(tmp_path):
    """End-to-end: a directory matching a user pattern is not analyzed."""
    from sphinxcontrib.nexus.ast_analyzer import analyze_directory

    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "real.py").write_text("def real_fn(): pass\n")

    (tmp_path / "student_resources").mkdir()
    (tmp_path / "student_resources" / "__init__.py").write_text("")
    (tmp_path / "student_resources" / "tutorial.py").write_text(
        "def shadow_fn(): pass\n"
    )

    user_excludes = ["student_resources/*"]
    excludes = _compute_exclude_patterns(
        analyze_tests=True,
        test_patterns=list(DEFAULT_TEST_PATTERNS),
        user_patterns=user_excludes,
    )

    graph = analyze_directory(tmp_path, exclude_patterns=excludes)
    nids = set(graph.nxgraph.nodes)

    assert any("real_fn" in n for n in nids)
    assert not any("shadow_fn" in n for n in nids)
    assert not any("tutorial" in n and "student_resources" in n for n in nids)
