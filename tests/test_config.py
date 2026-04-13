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
