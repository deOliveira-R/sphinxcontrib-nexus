"""Sphinx config for the minimal self-hosting fixture project.

Small on purpose: three equations, three solver functions, one helper
chain, a handful of tests exercising every pytest-marker feature
introduced in session 2.
"""

import sys
from pathlib import Path

_here = Path(__file__).parent
sys.path.insert(0, str(_here))

project = "nexus-fixture"
extensions = [
    "sphinx.ext.mathjax",
    "sphinx.ext.autodoc",
    "sphinxcontrib.nexus",
]
master_doc = "index"
exclude_patterns = ["_build"]

# Expose the toy solver package to autodoc so its classes (Mesh in
# particular) enter the Sphinx graph via ``extract_domain_objects``
# — this is the pipeline path where the nexus#3 round-2 type
# demotion bug lives. Without autodoc, the Sphinx side never sees
# the class and the merge layer has nothing to collide with.
autodoc_default_options = {
    "members": True,
    "undoc-members": True,
}

nexus_output = "_nexus"
nexus_ast_analyze = True
nexus_extra_source_dirs = ["solver_pkg", "solver_tests"]
nexus_analyze_tests = True
nexus_test_patterns = ["solver_tests/*", "*/solver_tests/*", "test_*.py"]

# Session 3: registry loader — applies explicit verification and
# implementation edges before the inference heuristic runs.
nexus_verification_registry = ["registry.yaml"]
