"""CLI entry point for sphinxcontrib-nexus."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from sphinxcontrib.nexus import __version__

_DESCRIPTION = """\
Nexus — unified code + documentation knowledge graph.

Extract a knowledge graph from Sphinx builds and Python AST analysis.
Query relationships between functions, classes, equations, theory pages,
and external dependencies via MCP, CLI, or Python API.

Quick start:
  nexus setup                    Install skills + show MCP config
  nexus analyze src/             Index Python source files
  nexus serve --db graph.db      Start the MCP server
  nexus status --db graph.db     Show graph summary
  nexus query --db graph.db "solve"   Search the graph
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="nexus",
        description=_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-V", "--version",
        action="version",
        version=f"sphinxcontrib-nexus {__version__}",
    )
    sub = parser.add_subparsers(dest="command")

    # --- setup ---
    setup_cmd = sub.add_parser(
        "setup",
        help="One-time setup: install skills for Claude Code, Cursor, Codex",
    )
    setup_cmd.add_argument(
        "--target", type=Path, default=None,
        help="Target skills directory (default: .claude/skills/).",
    )
    setup_cmd.add_argument(
        "--global", dest="global_install", action="store_true",
        help="Install to ~/.claude/skills/ (global, all projects).",
    )
    setup_cmd.add_argument("-v", "--verbose", action="store_true")

    # --- analyze ---
    analyze = sub.add_parser(
        "analyze",
        help="Index Python source files into the knowledge graph",
    )
    analyze.add_argument(
        "source_dir", type=Path,
        help="Directory to scan for .py files.",
    )
    analyze.add_argument(
        "--db", type=Path, default=Path("_nexus/graph.db"),
        help="SQLite database path (default: _nexus/graph.db). "
        "Merges with existing graph if present.",
    )
    analyze.add_argument(
        "--project-root", type=Path, default=None,
        help="Root for module name resolution (default: source_dir).",
    )
    analyze.add_argument(
        "--sys-path", type=Path, nargs="*", default=None,
        help="Additional directories on the Python path for module resolution.",
    )
    analyze.add_argument(
        "--auto-sys-path", action="store_true",
        help="Auto-detect sys.path from numbered directory pattern.",
    )
    analyze.add_argument(
        "--json", type=Path, default=None,
        help="Also write JSON output to this path.",
    )
    analyze.add_argument(
        "--exclude", nargs="*", default=None,
        help="Glob patterns to exclude (default: docs/*, .venv/*).",
    )
    analyze.add_argument("-v", "--verbose", action="store_true")

    # --- serve ---
    serve_cmd = sub.add_parser(
        "serve",
        help="Start MCP server (stdio) — 16 tools, 4 resources",
    )
    serve_cmd.add_argument(
        "--db", type=Path, default=Path("_nexus/graph.db"),
        help="SQLite database path (default: _nexus/graph.db).",
    )
    serve_cmd.add_argument(
        "--project-root", type=Path, default=None,
        help="Project root for git operations and file searches.",
    )
    serve_cmd.add_argument("-v", "--verbose", action="store_true")

    # --- status ---
    status_cmd = sub.add_parser(
        "status",
        help="Show graph summary: node/edge counts by type",
    )
    status_cmd.add_argument(
        "--db", type=Path, default=Path("_nexus/graph.db"),
        help="SQLite database path (default: _nexus/graph.db).",
    )

    # --- query ---
    query_cmd = sub.add_parser(
        "query",
        help="Search the knowledge graph for symbols matching a keyword",
    )
    query_cmd.add_argument(
        "text",
        help="Search text (case-insensitive substring match).",
    )
    query_cmd.add_argument(
        "--db", type=Path, default=Path("_nexus/graph.db"),
        help="SQLite database path.",
    )
    query_cmd.add_argument(
        "--type", dest="node_types", default="",
        help="Comma-separated node types to filter (e.g., 'function,class').",
    )
    query_cmd.add_argument(
        "--limit", type=int, default=20,
        help="Maximum results (default: 20).",
    )

    # --- impact ---
    impact_cmd = sub.add_parser(
        "impact",
        help="Blast radius analysis: what breaks if you change a symbol",
    )
    impact_cmd.add_argument(
        "target",
        help="Node ID of the symbol (e.g., 'py:function:sn_solver.solve_sn').",
    )
    impact_cmd.add_argument(
        "--db", type=Path, default=Path("_nexus/graph.db"),
    )
    impact_cmd.add_argument(
        "--direction", default="upstream",
        choices=["upstream", "downstream"],
        help="upstream = what depends on this; downstream = what this depends on.",
    )
    impact_cmd.add_argument(
        "--depth", type=int, default=3,
        help="Maximum traversal depth (default: 3).",
    )

    # --- provenance ---
    prov_cmd = sub.add_parser(
        "provenance",
        help="Trace citation → equation → code chain for a symbol",
    )
    prov_cmd.add_argument(
        "target",
        help="Node ID of a code symbol or equation.",
    )
    prov_cmd.add_argument(
        "--db", type=Path, default=Path("_nexus/graph.db"),
    )

    # --- coverage ---
    cov_cmd = sub.add_parser(
        "coverage",
        help="Verification coverage: which equations have code + tests",
    )
    cov_cmd.add_argument(
        "--db", type=Path, default=Path("_nexus/graph.db"),
    )
    cov_cmd.add_argument(
        "--status", default="",
        help="Filter: verified, tested, implemented, documented, orphan_code.",
    )

    # --- staleness ---
    stale_cmd = sub.add_parser(
        "staleness",
        help="Detect documentation pages that drifted from code",
    )
    stale_cmd.add_argument(
        "--db", type=Path, default=Path("_nexus/graph.db"),
    )
    stale_cmd.add_argument(
        "--project-root", type=Path, default=None,
    )

    # --- migration ---
    mig_cmd = sub.add_parser(
        "migration",
        help="Plan a dependency migration (e.g., numpy → jax)",
    )
    mig_cmd.add_argument(
        "--from", dest="from_dep", required=True,
        help="Package to migrate from (e.g., 'numpy').",
    )
    mig_cmd.add_argument(
        "--to", dest="to_dep", default="",
        help="Package to migrate to (e.g., 'jax.numpy').",
    )
    mig_cmd.add_argument(
        "--db", type=Path, default=Path("_nexus/graph.db"),
    )

    # --- ingest ---
    ingest_cmd = sub.add_parser(
        "ingest",
        help="Ingest a document (PDF, text) into the graph via LLM extraction",
    )
    ingest_cmd.add_argument(
        "file", type=Path,
        help="Document to ingest (PDF, txt, md, rst, tex).",
    )
    ingest_cmd.add_argument(
        "--db", type=Path, default=Path("_nexus/graph.db"),
    )
    ingest_cmd.add_argument(
        "--llm", default=None,
        help="LLM command (default: 'claude -p'). Must accept prompt on stdin.",
    )
    ingest_cmd.add_argument("-v", "--verbose", action="store_true")

    # --- visualize ---
    viz_cmd = sub.add_parser(
        "visualize",
        help="Open interactive graph explorer in browser (Sigma.js WebGL)",
    )
    viz_cmd.add_argument(
        "--db", type=Path, default=Path("_nexus/graph.db"),
    )
    viz_cmd.add_argument(
        "--output", type=Path, default=None,
        help="Output HTML file (default: alongside graph.db).",
    )
    viz_cmd.add_argument(
        "--max-nodes", type=int, default=500,
        help="Maximum nodes to include (default: 500, top by degree).",
    )
    viz_cmd.add_argument("-v", "--verbose", action="store_true")

    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 1

    verbose = getattr(args, "verbose", False)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    dispatch = {
        "setup": _run_setup,
        "analyze": _run_analyze,
        "serve": _run_serve,
        "status": _run_status,
        "query": _run_query,
        "impact": _run_impact,
        "provenance": _run_provenance,
        "ingest": _run_ingest,
        "visualize": _run_visualize,
        "coverage": _run_coverage,
        "staleness": _run_staleness,
        "migration": _run_migration,
    }
    handler = dispatch.get(args.command)
    if handler:
        return handler(args)
    return 0


# ------------------------------------------------------------------
# Command handlers
# ------------------------------------------------------------------


def _load_query(db_path: Path) -> "GraphQuery":
    from sphinxcontrib.nexus.export import load_sqlite
    from sphinxcontrib.nexus.query import GraphQuery

    if not db_path.exists():
        print(f"Error: {db_path} does not exist", file=sys.stderr)
        print("Run 'nexus analyze' or 'sphinx-build' first.", file=sys.stderr)
        sys.exit(1)
    return GraphQuery(load_sqlite(db_path))


def _run_setup(args: argparse.Namespace) -> int:
    import shutil

    if args.target:
        target = args.target.resolve()
    elif args.global_install:
        target = Path.home() / ".claude" / "skills"
    else:
        target = Path.cwd() / ".claude" / "skills"

    skills_src = Path(__file__).parent / "skills"
    if not skills_src.exists():
        print(f"Error: bundled skills not found at {skills_src}", file=sys.stderr)
        return 1

    installed = []
    for skill_dir in sorted(skills_src.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.exists():
            continue
        dest_dir = target / skill_dir.name
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(skill_file, dest_dir / "SKILL.md")
        installed.append(skill_dir.name)

    print(f"Installed {len(installed)} skills to {target}/")
    for name in installed:
        print(f"  {name}/SKILL.md")

    # Install MCP server configuration
    nexus_cmd = shutil.which("nexus") or ".venv/bin/nexus"
    db_path = "docs/_build/html/_nexus/graph.db"
    nexus_server_config = {
        "command": nexus_cmd,
        "args": ["serve", "--db", db_path, "--project-root", "."],
    }

    if args.global_install:
        # User-level: add to ~/.claude.json mcpServers
        claude_json = Path.home() / ".claude.json"
        if claude_json.exists():
            data = json.loads(claude_json.read_text())
            data.setdefault("mcpServers", {})["nexus"] = nexus_server_config
            claude_json.write_text(json.dumps(data, indent=2) + "\n")
            print(f"\nAdded nexus MCP server to {claude_json} (user-level, all projects)")
        else:
            data = {"mcpServers": {"nexus": nexus_server_config}}
            claude_json.write_text(json.dumps(data, indent=2) + "\n")
            print(f"\nCreated {claude_json} with nexus MCP server (user-level)")
    else:
        # Project-level: add to .mcp.json
        mcp_json = Path.cwd() / ".mcp.json"
        if mcp_json.exists():
            existing = json.loads(mcp_json.read_text())
            existing.setdefault("mcpServers", {})["nexus"] = nexus_server_config
            mcp_json.write_text(json.dumps(existing, indent=2) + "\n")
            print(f"\nUpdated {mcp_json} with nexus MCP server (project-level)")
        else:
            mcp_json.write_text(json.dumps({"mcpServers": {"nexus": nexus_server_config}}, indent=2) + "\n")
            print(f"\nCreated {mcp_json} with nexus MCP server (project-level)")

    # Install PostToolUse hook for auto-rebuild after git commit
    settings_dir = Path.cwd() / ".claude"
    settings_dir.mkdir(exist_ok=True)
    print(f"\nTo auto-rebuild the graph after git commits, add this hook to .claude/settings.json:")
    print("""
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "if": "Bash(git commit:*)",
            "command": ".venv/bin/python -m sphinx -b html docs docs/_build/html -q 2>/dev/null &",
            "timeout": 5000,
            "async": true,
            "statusMessage": "Rebuilding knowledge graph..."
          }
        ]
      }
    ]
  }""")

    return 0


def _run_analyze(args: argparse.Namespace) -> int:
    from sphinxcontrib.nexus.ast_analyzer import analyze_directory
    from sphinxcontrib.nexus.export import load_sqlite, write_json, write_sqlite
    from sphinxcontrib.nexus.merge import merge_graphs

    source_dir = args.source_dir.resolve()
    if not source_dir.is_dir():
        print(f"Error: {source_dir} is not a directory", file=sys.stderr)
        return 1

    project_root = args.project_root or source_dir
    sys_path_dirs = args.sys_path if not args.auto_sys_path else None

    ast_graph = analyze_directory(
        source_dir=source_dir,
        project_root=project_root.resolve(),
        sys_path_dirs=sys_path_dirs,
        exclude_patterns=args.exclude,
    )

    if args.db.exists():
        sphinx_graph = load_sqlite(args.db)
        merged = merge_graphs(sphinx_graph, ast_graph)
        print(f"Merged with existing graph from {args.db}")
    else:
        merged = ast_graph

    write_sqlite(merged, args.db)
    print(f"Written to {args.db}")
    print(f"  Nodes: {merged.node_count}")
    print(f"  Edges: {merged.edge_count}")

    if args.json:
        write_json(merged, args.json)
        print(f"  JSON: {args.json}")

    from collections import Counter
    edge_types = Counter(
        data.get("type", "unknown")
        for _, _, data in merged.nxgraph.edges(data=True)
    )
    print("  Edge types:")
    for t, c in edge_types.most_common():
        print(f"    {t:20s} {c}")
    return 0


def _run_serve(args: argparse.Namespace) -> int:
    from sphinxcontrib.nexus.server import serve

    db_path = args.db.resolve()
    if not db_path.exists():
        print(f"Error: {db_path} does not exist", file=sys.stderr)
        print("Run 'nexus analyze' or 'sphinx-build' first.", file=sys.stderr)
        return 1

    project_root = (args.project_root or Path.cwd()).resolve()
    serve(db_path=db_path, project_root=project_root)
    return 0


def _run_status(args: argparse.Namespace) -> int:
    q = _load_query(args.db)
    s = q.stats()
    print(f"Graph: {s.node_count} nodes, {s.edge_count} edges")
    print(f"Density: {s.density:.6f}")
    print(f"Components: {s.connected_components}")
    print()
    print("Nodes by type:")
    for t, c in sorted(s.nodes_by_type.items(), key=lambda x: -x[1]):
        print(f"  {t:20s} {c}")
    print()
    print("Edges by type:")
    for t, c in sorted(s.edges_by_type.items(), key=lambda x: -x[1]):
        print(f"  {t:20s} {c}")
    return 0


def _run_query(args: argparse.Namespace) -> int:
    q = _load_query(args.db)
    types = [t.strip() for t in args.node_types.split(",") if t.strip()] or None
    results = q.query(args.text, node_types=types, limit=args.limit)
    if not results:
        print("No results found.")
        return 0
    for r in results:
        print(f"  {r.id:55s}  type={r.type:12s}  degree={r.degree}")
    return 0


def _run_impact(args: argparse.Namespace) -> int:
    q = _load_query(args.db)
    result = q.impact(args.target, direction=args.direction, max_depth=args.depth)
    if result.total_affected == 0:
        print(f"No {'upstream' if args.direction == 'upstream' else 'downstream'} "
              f"dependents found for {args.target}")
        return 0
    for depth, nodes in result.by_depth.items():
        label = {1: "WILL BREAK", 2: "LIKELY AFFECTED", 3: "MAY NEED TESTING"}.get(
            depth, f"depth={depth}",
        )
        print(f"  d={depth} ({label}):")
        for n in nodes:
            print(f"    {n.id:55s}  type={n.type}")
    print(f"\nTotal affected: {result.total_affected}")
    return 0


def _run_provenance(args: argparse.Namespace) -> int:
    q = _load_query(args.db)
    result = q.provenance_chain(args.target)
    if not result.chain:
        print(f"No provenance chain found for {args.target}")
        return 0
    for step in result.chain:
        indent = "  " * step.depth
        print(f"{indent}{step.edge_type}: {step.node.id} ({step.node.type})")
    if result.citations:
        print(f"\nCitations: {', '.join(result.citations)}")
    return 0


def _run_coverage(args: argparse.Namespace) -> int:
    q = _load_query(args.db)
    filt = args.status if args.status else None
    result = q.verification_coverage(status_filter=filt)
    print("Summary:")
    for status, count in sorted(result.summary.items()):
        print(f"  {status:20s} {count}")
    print()
    if result.entries:
        shown = result.entries[:30]
        for e in shown:
            print(f"  [{e.status:12s}] {e.node.id}")
        if len(result.entries) > 30:
            print(f"  ... ({len(result.entries)} total)")
    return 0


def _run_staleness(args: argparse.Namespace) -> int:
    q = _load_query(args.db)
    project_root = args.project_root or Path.cwd()
    result = q.staleness(project_root)
    if not result.stale_docs:
        print(f"No stale docs found ({result.total_checked} checked).")
        return 0
    print(f"Stale docs: {result.total_stale} / {result.total_checked}")
    for entry in result.stale_docs:
        print(f"\n  {entry.doc_node.id}")
        print(f"    Reason: {entry.stale_reason}")
        print(f"    Code modified: {entry.code_modified}")
        print(f"    Doc modified:  {entry.doc_modified}")
        for sym in entry.affected_symbols[:5]:
            print(f"    - {sym}")
    return 0


def _run_migration(args: argparse.Namespace) -> int:
    q = _load_query(args.db)
    result = q.migration_plan(args.from_dep, args.to_dep)
    if not result.phases:
        print(f"No functions found using {args.from_dep}")
        return 0
    print(f"Migration: {args.from_dep} → {args.to_dep or '?'}")
    print(f"Total functions affected: {result.total_functions}")
    for phase in result.phases:
        print(f"\n  Phase {phase.phase}: {phase.label}")
        print(f"  Blast radius: {phase.blast_radius}")
        for f in phase.functions[:10]:
            print(f"    {f.id}")
        if len(phase.functions) > 10:
            print(f"    ... ({len(phase.functions)} total)")
    if result.doc_updates:
        print(f"\n  Documentation updates needed:")
        for d in result.doc_updates:
            print(f"    {d.id}")
    return 0


def _run_ingest(args: argparse.Namespace) -> int:
    from sphinxcontrib.nexus.export import load_sqlite, write_sqlite
    from sphinxcontrib.nexus.ingest import ingest_file

    file_path = args.file.resolve()
    if not file_path.exists():
        print(f"Error: {file_path} does not exist", file=sys.stderr)
        return 1

    if args.db.exists():
        from sphinxcontrib.nexus.export import load_sqlite
        graph = load_sqlite(args.db)
    else:
        from sphinxcontrib.nexus.graph import KnowledgeGraph
        graph = KnowledgeGraph()

    result = ingest_file(file_path, graph, llm_command=args.llm)
    write_sqlite(graph, args.db)

    print(f"Ingested: {result.source_file}")
    print(f"  Concepts:      {result.concepts_added}")
    print(f"  Equations:     {result.equations_added}")
    print(f"  Relationships: {result.relationships_added}")
    print(f"  Citations:     {result.citations_added}")
    return 0


def _run_visualize(args: argparse.Namespace) -> int:
    from sphinxcontrib.nexus.visualize import serve_visualization

    db_path = args.db.resolve()
    if not db_path.exists():
        print(f"Error: {db_path} does not exist", file=sys.stderr)
        print("Run 'nexus analyze' or 'sphinx-build' first.", file=sys.stderr)
        return 1

    serve_visualization(db_path, max_nodes=args.max_nodes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
