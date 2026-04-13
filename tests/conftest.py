import pytest

pytest_plugins = "sphinx.testing.fixtures"

# ``roots`` holds Sphinx fixture sources consumed via ``make_app``.
# ``fixtures`` holds full-project trees built with ``sphinx-build`` by
# ``test_fixture_e2e.py``; both must be ignored from pytest collection
# so pytest doesn't try to import the inner ``test_*.py`` files with
# the outer sys.path.
collect_ignore_glob = ["roots/**", "fixtures/**"]
collect_ignore = ["roots", "fixtures"]
