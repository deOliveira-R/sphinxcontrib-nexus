"""Sphinx configuration for the sphinxcontrib-nexus documentation.

The extension documents itself: ``sphinxcontrib.nexus`` is enabled
below, so every build writes a knowledge graph of this project into
``_build/html/_nexus/graph.db``. That is deliberate — it dogfoods the
extension against a real codebase on every docs build, and the graph is
available to query while working on nexus itself.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sphinxcontrib.nexus import __version__  # noqa: E402

project = "sphinxcontrib-nexus"
author = "Rodrigo de Oliveira"
release = __version__
version = __version__

extensions = [
    "myst_parser",
    "sphinx.ext.autodoc",
    # The codebase writes Google-style docstrings (``Args:`` blocks).
    # Without napoleon, docutils reads those as an unexpected indent.
    "sphinx.ext.napoleon",
    "sphinx.ext.intersphinx",
    "sphinx.ext.viewcode",
    "sphinxcontrib.nexus",
]

myst_enable_extensions = ["colon_fence", "deflist"]

templates_path = ["_templates"]
exclude_patterns = ["_build", "notes/*"]

html_theme = "alabaster"
html_static_path = []

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    "sphinx": ("https://www.sphinx-doc.org/en/master", None),
}

# -- Nexus ----------------------------------------------------------------
# `notes/` holds design spikes, not shipped source. Nothing else to
# exclude: the package IS the thing being documented.
nexus_source_exclude_patterns = []
