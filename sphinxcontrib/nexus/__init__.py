"""Sphinxcontrib-nexus: extract a knowledge graph from Sphinx builds."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sphinx.application import Sphinx
    from sphinx.environment import BuildEnvironment

__version__ = "0.4.2"

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

    for src_dir in source_dirs:
        ast_graph = analyze_directory(
            source_dir=src_dir,
            project_root=project_root,
            sys_path_dirs=source_dirs,
            exclude_patterns=["tests/*", "docs/*", ".venv/*", "__pycache__/*"],
        )
        merge_graphs(graph, ast_graph)

    # Infer IMPLEMENTS edges once, after all AST merges complete
    from sphinxcontrib.nexus.merge import _infer_implements
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
    app.add_config_value("nexus_max_viz_nodes", 300, "env")
    app.add_directive("nexus-graph", NexusGraphDirective)

    app.connect("env-check-consistency", _on_env_check_consistency)
    app.connect("build-finished", _on_build_finished)

    return {
        "version": __version__,
        "parallel_read_safe": True,
        "parallel_write_safe": True,
    }
