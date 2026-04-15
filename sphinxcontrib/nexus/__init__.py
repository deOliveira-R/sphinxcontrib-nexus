"""Sphinxcontrib-nexus: extract a knowledge graph from Sphinx builds."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sphinx.application import Sphinx
    from sphinx.environment import BuildEnvironment

__version__ = "0.11.0"

logger = logging.getLogger(__name__)


def _on_env_check_consistency(app: Sphinx, env: BuildEnvironment) -> None:
    """Build the knowledge graph after all docs are read."""
    from sphinxcontrib.nexus.extractors import build_graph

    logger.info("Building knowledge graph...")
    graph = build_graph(env)
    graph.metadata = {
        "sphinx_project": app.config.project,
        "sphinx_version": app.config.version,
        "build_time": datetime.now(timezone.utc).isoformat(),
    }
    # Store on env — standard Sphinx pattern for extension state
    env.nexus_graph = graph  # type: ignore[attr-defined]
    logger.info(
        "Knowledge graph: %d nodes, %d edges",
        graph.node_count,
        graph.edge_count,
    )


def _finalize_graph(graph: Any) -> None:
    """Final cleanup before export: confidence scores, phantom nodes."""
    from sphinxcontrib.nexus.extractors import _EXTERNAL_NAMES
    from sphinxcontrib.nexus.graph import NodeType

    g = graph.nxgraph
    # Tag confidence on all edges
    for _, _, data in g.edges(data=True):
        if "confidence" not in data:
            data["confidence"] = 1.0

    # Classify phantom nodes (created by add_edge to nonexistent targets)
    for node_id in list(g.nodes):
        attrs = g.nodes[node_id]
        if attrs.get("type") and attrs["type"] not in ("", "unknown"):
            continue
        parts = node_id.split(":", 2)
        name = parts[2] if len(parts) == 3 else node_id
        top_level = name.split(".")[0]
        attrs["type"] = NodeType.EXTERNAL.value if top_level in _EXTERNAL_NAMES else NodeType.UNRESOLVED.value
        if "name" not in attrs or not attrs["name"]:
            attrs["name"] = name
            attrs["display_name"] = name
            attrs["domain"] = parts[0] if len(parts) >= 2 else "py"


# Non-source patterns that are always excluded from AST analysis,
# independent of user config.
_BASE_EXCLUDE_PATTERNS: tuple[str, ...] = (
    "docs/*",
    ".venv/*",
    "__pycache__/*",
)

# Default glob patterns (POSIX-style, matched against the path relative
# to each source dir via fnmatch) used to identify Python test modules.
DEFAULT_TEST_PATTERNS: tuple[str, ...] = (
    "tests/*",
    "*/tests/*",
    "test_*.py",
    "*/test_*.py",
)


def _compute_exclude_patterns(
    analyze_tests: bool,
    test_patterns: list[str],
    user_patterns: list[str] | None = None,
) -> list[str]:
    """Build the exclusion list for ``analyze_directory``.

    Base exclusions (docs, venv, __pycache__) are always applied. If
    ``analyze_tests`` is False, ``test_patterns`` is appended so test
    modules are skipped entirely. ``user_patterns`` (from
    ``nexus_source_exclude_patterns``) is appended unconditionally —
    these are downstream-project escape hatches for directories that
    are neither tests nor build artifacts (tutorials, vendored code,
    legacy modules).
    """
    patterns = list(_BASE_EXCLUDE_PATTERNS)
    if not analyze_tests:
        patterns.extend(test_patterns)
    if user_patterns:
        patterns.extend(user_patterns)
    return patterns


def _run_ast_analysis(app: Sphinx, graph: Any) -> None:
    """Run AST analysis on project source and merge into the doc graph."""
    import sys

    from sphinxcontrib.nexus.ast_analyzer import analyze_directory
    from sphinxcontrib.nexus.merge import merge_graphs

    # Determine the project root and let ModuleResolver auto-detect
    # source directories. For projects that add dirs to sys.path in
    # conf.py, we pass those as explicit sys_path_dirs.
    project_root = Path(app.srcdir).parent
    _skip = {".venv", "venv", ".tox", "__pycache__", "site-packages",
             "_build", "node_modules", "dist", "build", ".git", ".egg-info"}

    # Collect sys.path entries under project root (set by conf.py)
    conf_sys_paths: list[Path] = []
    for p in sys.path:
        pp = Path(p).resolve()
        if not pp.is_dir() or pp == project_root:
            continue
        if _skip & set(pp.parts):
            continue
        try:
            pp.relative_to(project_root)
            conf_sys_paths.append(pp)
        except ValueError:
            continue

    # If conf.py added directories to sys.path, analyze each one.
    # Otherwise analyze project_root and let ModuleResolver auto-detect.
    if conf_sys_paths:
        source_dirs = conf_sys_paths
    else:
        source_dirs = [project_root]

    test_patterns = list(app.config.nexus_test_patterns)
    analyze_tests = bool(app.config.nexus_analyze_tests)
    user_excludes = list(getattr(app.config, "nexus_source_exclude_patterns", []) or [])
    exclude_patterns = _compute_exclude_patterns(
        analyze_tests, test_patterns, user_excludes,
    )

    # Scan main source directories.
    for src_dir in source_dirs:
        ast_graph = analyze_directory(
            source_dir=src_dir,
            project_root=project_root,
            sys_path_dirs=source_dirs,
            exclude_patterns=exclude_patterns,
        )
        merge_graphs(graph, ast_graph)

    # Scan user-specified extra dirs (e.g. out-of-tree source roots).
    # Test exclusion still follows the nexus_analyze_tests gate.
    all_sys_paths = source_dirs[:]
    for extra in app.config.nexus_extra_source_dirs:
        extra_path = (project_root / extra).resolve()
        if not extra_path.is_dir():
            logger.warning("nexus_extra_source_dirs: %s not found, skipping", extra)
            continue
        all_sys_paths.append(extra_path)
        ast_graph = analyze_directory(
            source_dir=extra_path,
            project_root=project_root,
            sys_path_dirs=all_sys_paths,
            exclude_patterns=exclude_patterns,
        )
        merge_graphs(graph, ast_graph)

    # Write declared TESTS edges (from @pytest.mark.verifies) first,
    # then apply any non-LLM verification registries. Both paths run
    # BEFORE ``_infer_implements`` so the token-intersection heuristic
    # honors every explicit edge as "already-known".
    from sphinxcontrib.nexus.ast_analyzer import _canonicalize_phantoms
    from sphinxcontrib.nexus.directives import apply_pending_edges
    from sphinxcontrib.nexus.merge import (
        _infer_implements,
        write_verifies_edges,
    )
    from sphinxcontrib.nexus.registry import (
        RegistryError,
        load_registry,
    )

    # Re-run canonicalization on the merged graph so Sphinx-side
    # phantoms that only ``analyze_directory``'s per-directory pass
    # couldn't see get collapsed into their AST-derived canonicals.
    # This is the "post-merge" pass that catches the ORPHEUS
    # re-export shape where both sides contribute half of the bug.
    _canonicalize_phantoms(graph)

    write_verifies_edges(graph.nxgraph)
    apply_pending_edges(app.env, graph.nxgraph)

    registry_paths = list(
        getattr(app.config, "nexus_verification_registry", []) or []
    )
    # Paths are resolved relative to ``app.srcdir`` — the directory
    # that holds ``conf.py``. This matches how Sphinx handles most
    # config-driven paths and lets users colocate their registry with
    # the theory docs that reference it. For projects where the
    # registry lives above ``docs/``, ``"../verification.yaml"`` works.
    srcdir = Path(app.srcdir)
    for entry in registry_paths:
        rpath = (srcdir / entry).resolve()
        if not rpath.is_file():
            logger.warning(
                "nexus_verification_registry: %s not found, skipping",
                rpath,
            )
            continue
        try:
            written = load_registry(rpath, graph.nxgraph)
        except RegistryError as err:
            logger.warning(
                "nexus_verification_registry: %s failed to load: %s",
                rpath, err,
            )
            continue
        logger.info(
            "nexus_verification_registry: loaded %d edges from %s",
            written, rpath,
        )

    if getattr(app.config, "nexus_infer_implements", True):
        _infer_implements(graph.nxgraph)

    logger.info(
        "After AST merge: %d nodes, %d edges",
        graph.node_count,
        graph.edge_count,
    )


def _on_build_finished(app: Sphinx, exception: Exception | None) -> None:
    """Run AST analysis, merge, and write the graph to disk."""
    if exception is not None:
        return

    graph = getattr(app.env, "nexus_graph", None)
    if graph is None:
        return

    # Run AST analysis and merge into the doc graph
    if app.config.nexus_ast_analyze:
        _run_ast_analysis(app, graph)

    # Final cleanup: ensure all edges have confidence, classify phantom nodes
    _finalize_graph(graph)

    from sphinxcontrib.nexus.export import write_json, write_sqlite

    outdir = Path(app.outdir) / app.config.nexus_output
    outdir.mkdir(parents=True, exist_ok=True)

    db_path = outdir / "graph.db"
    write_sqlite(graph, db_path)
    logger.info("Knowledge graph (SQLite) written to %s", db_path)

    json_path = outdir / "graph.json"
    write_json(graph, json_path)
    logger.info("Knowledge graph (JSON) written to %s", json_path)

    # Generate interactive HTML visualization
    from sphinxcontrib.nexus.visualize import generate_html
    html_path = generate_html(db_path, max_nodes=app.config.nexus_max_viz_nodes)
    logger.info("Knowledge graph (HTML viz) written to %s", html_path)


def setup(app: Sphinx) -> dict[str, Any]:
    from docutils import nodes
    from sphinx.util.docutils import SphinxDirective

    class NexusGraphDirective(SphinxDirective):
        """Sphinx directive: ``.. nexus-graph::`` embeds the interactive graph."""

        has_content = False
        required_arguments = 0
        optional_arguments = 0
        option_spec = {
            "height": lambda x: x,
        }

        def run(self):
            height = self.options.get("height", "800px")
            nexus_output = self.env.config.nexus_output
            graph_url = f"{nexus_output}/graph.html"

            raw_html = (
                f'<div style="border:1px solid #333;border-radius:8px;overflow:hidden;margin:20px 0;">'
                f'<iframe src="{graph_url}" '
                f'style="width:100%;height:{height};border:none;" '
                f'loading="lazy"></iframe>'
                f'</div>'
            )
            raw_node = nodes.raw("", raw_html, format="html")
            return [raw_node]

    app.add_config_value("nexus_output", "_nexus", "env")
    app.add_config_value("nexus_ast_analyze", True, "env")
    app.add_config_value("nexus_extra_source_dirs", [], "env")
    app.add_config_value("nexus_max_viz_nodes", 300, "env")
    app.add_config_value("nexus_analyze_tests", True, "env")
    app.add_config_value(
        "nexus_test_patterns", list(DEFAULT_TEST_PATTERNS), "env"
    )
    app.add_config_value("nexus_infer_implements", True, "env")
    app.add_config_value("nexus_verification_registry", [], "env")
    app.add_config_value("nexus_source_exclude_patterns", [], "env")
    app.add_directive("nexus-graph", NexusGraphDirective)

    from sphinxcontrib.nexus import directives as _directives_module
    _directives_module.register(app)

    app.connect("env-check-consistency", _on_env_check_consistency)
    app.connect("build-finished", _on_build_finished)

    return {
        "version": __version__,
        # Parallel-safe per a serial-vs-``-j N`` round-trip audit:
        # both modes produce bit-identical node and edge sets on
        # the ``minimal_project`` fixture (Session 4.3). The AST
        # analysis runs once in ``build-finished`` on the main
        # process AFTER all worker envs have been merged, so the
        # per-worker env state that Sphinx parallelizes never
        # touches the graph we ultimately write. The directive
        # layer's ``env-merge-info`` handler correctly folds
        # pending-edge entries from worker envs.
        #
        # A regression test in ``tests/test_fixture_e2e.py``
        # builds the fixture with ``-j 2`` and asserts the result
        # matches the serial build.
        "parallel_read_safe": True,
        "parallel_write_safe": True,
    }
