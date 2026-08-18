"""CLI entry point for sphinxcontrib-nexus."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from sphinxcontrib.nexus import __version__
from sphinxcontrib.nexus.project import find_project_root, resolve_db
from sphinxcontrib.nexus.install import MANIFEST_NAME, Payload
from sphinxcontrib.nexus.workspace import Workspace

if TYPE_CHECKING:
    from sphinxcontrib.nexus.query import GraphQuery

_DESCRIPTION = """\
Nexus — unified code + documentation knowledge graph.

Extract a knowledge graph from Sphinx builds and Python AST analysis.
Query relationships between functions, classes, equations, theory pages,
and external dependencies via MCP, CLI, or Python API.

Quick start:
  nexus setup                    Install skills + show MCP config
  nexus analyze src/             Index Python source files
  nexus serve                    Start the MCP server
  nexus status                   Show graph summary
  nexus query "solve"            Search the graph
  nexus workspaces               Show checkouts (worktrees) + their graphs
  nexus config db                Print where the graph lives (for scripts)

Every command finds the graph on its own, through `.nexus/config.toml`.
Pass --db only to override that deliberately.
"""

# The settings a consumer OUTSIDE Python may ask for by name. Each maps to
# exactly one resolved answer, with the precedence chain already applied,
# so a shell script never re-implements it.
#
# ``db`` is the load-bearing entry. Before this verb existed the only way
# for a hook to find the graph was to hardcode the path — which is what
# three of them did, and all three broke the moment a project moved
# ``[graph].output``. Two failed SILENTLY (their contract is a quiet
# ``exit 0`` when the graph is absent), so the breakage read as "no graph
# here" rather than "I looked in the wrong place".
_CONFIG_KEYS = (
    "root",
    "config",
    "db",
    "output",
    "scope.prefixes",
    "catalog.errors",
)


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
        help="Install to ~/.claude/skills/ (global, all projects). "
        "Skips the always-on routing rule.",
    )
    setup_cmd.add_argument(
        "--check", action="store_true",
        help="Report install state per file (missing / stale / locally "
        "modified) and exit non-zero if anything needs attention. "
        "Writes nothing.",
    )
    setup_cmd.add_argument(
        "--diff", action="store_true",
        help="Show what the consumer changed in locally-modified files "
        "('+' lines are theirs). Writes nothing.",
    )
    setup_cmd.add_argument(
        "--force", action="store_true",
        help="Overwrite locally-modified files (keeps a .bak). Read "
        "--diff first: a local edit is often the better version.",
    )
    setup_cmd.add_argument(
        "--no-rules", dest="no_rules", action="store_true",
        help="Do not install the always-on routing rule into "
        ".claude/rules/.",
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
        "--db", type=Path, default=None,
        help="SQLite database path. Defaults to the project's own graph, <project root>/.nexus/graph.db — `nexus config db` prints it. "
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
        help="Start MCP server (stdio) over the knowledge graph",
    )
    serve_cmd.add_argument(
        "--db", type=Path, default=None,
        help="SQLite database path. Defaults to the project's own graph, <project root>/.nexus/graph.db — `nexus config db` prints it.",
    )
    serve_cmd.add_argument(
        "--project-root", type=Path, default=None,
        help="Project root for git operations and file searches.",
    )
    serve_cmd.add_argument("-v", "--verbose", action="store_true")

    # --- workspaces ---
    workspaces_cmd = sub.add_parser(
        "workspaces",
        help="List project checkouts (git worktrees) and their graphs",
    )
    workspaces_cmd.add_argument(
        "--db", type=Path, default=None,
        help="SQLite database path of the active checkout.",
    )
    workspaces_cmd.add_argument(
        "--project-root", type=Path, default=None,
        help="Checkout root the database belongs to (default: cwd).",
    )
    workspaces_cmd.add_argument("-v", "--verbose", action="store_true")

    # --- config ---
    config_cmd = sub.add_parser(
        "config",
        help="Print resolved project settings (where the graph lives, etc.)",
    )
    config_cmd.add_argument(
        "key", nargs="?", default=None,
        help=(
            "Print just this setting's value, bare, for shell capture "
            f"({', '.join(_CONFIG_KEYS)}). Omit to print them all."
        ),
    )
    config_cmd.add_argument(
        "--project-root", type=Path, default=None,
        help="Where to start looking for .nexus/ (default: cwd).",
    )

    # --- file-brief ---
    file_brief_cmd = sub.add_parser(
        "file-brief",
        help="Edit-time brief: what the graph knows about one source "
        "file (direct SQLite read — fast enough for a hook)",
    )
    file_brief_cmd.add_argument(
        "file", type=Path,
        help="Source file; relative paths resolve against --project-root.",
    )
    file_brief_cmd.add_argument(
        "--db", type=Path, default=None,
        help="SQLite database path. Defaults to the project's own graph, <project root>/.nexus/graph.db — `nexus config db` prints it.",
    )
    file_brief_cmd.add_argument(
        "--project-root", type=Path, default=None,
        help="Checkout root for path resolution and the git staleness "
        "check (default: cwd).",
    )
    file_brief_cmd.add_argument(
        "--json", dest="json_out", action="store_true",
        help="Emit the full brief as JSON instead of the ≤6-line text.",
    )

    # --- status ---
    status_cmd = sub.add_parser(
        "status",
        help="Show graph summary: node/edge counts by type",
    )
    status_cmd.add_argument(
        "--db", type=Path, default=None,
        help="SQLite database path. Defaults to the project's own graph, <project root>/.nexus/graph.db — `nexus config db` prints it.",
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
        "--db", type=Path, default=None,
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
        "--db", type=Path, default=None,
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
    impact_cmd.add_argument(
        "--limit-per-depth", type=int, default=50,
        help="Max nodes listed per depth bucket, most-connected first "
             "(default: 50; 0 = no cap).",
    )
    impact_cmd.add_argument(
        "--only", choices=["tests", "code"], default=None,
        help="Keep one role in the listing: 'tests' (what pytest "
             "collects, each with a runnable id) or 'code'. The walk is "
             "unchanged either way — total_affected still counts every "
             "node reached.",
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
        "--db", type=Path, default=None,
    )

    # --- coverage ---
    cov_cmd = sub.add_parser(
        "coverage",
        help="Verification coverage: which equations have code + tests",
    )
    cov_cmd.add_argument(
        "--db", type=Path, default=None,
    )
    cov_cmd.add_argument(
        "--status", default="",
        help="Filter: verified, tested, implemented, documented, orphan_code.",
    )
    cov_cmd.add_argument(
        "--limit", type=int, default=0,
        help="Max entries to print in the human listing. 0 = unlimited.",
    )
    cov_cmd.add_argument(
        "--offset", type=int, default=0,
        help="Skip this many entries from the start of the list.",
    )

    # --- staleness ---
    stale_cmd = sub.add_parser(
        "staleness",
        help="Detect documentation pages that drifted from code",
    )
    stale_cmd.add_argument(
        "--db", type=Path, default=None,
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
        "--db", type=Path, default=None,
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
        "--db", type=Path, default=None,
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
        "--db", type=Path, default=None,
    )
    viz_cmd.add_argument(
        "--output", type=Path, default=None,
        help="Output HTML file (default: alongside graph.db, i.e. in .nexus/; the Sphinx build instead writes it under the HTML output so the page can be served).",
    )
    viz_cmd.add_argument(
        "--max-nodes", type=int, default=500,
        help="Maximum nodes to include (default: 500, top by degree).",
    )
    viz_cmd.add_argument("-v", "--verbose", action="store_true")

    # --- briefing ---
    briefing_cmd = sub.add_parser(
        "briefing",
        help="Session briefing: stats, stale docs, coverage gaps, recent changes (JSON)",
    )
    briefing_cmd.add_argument(
        "--db", type=Path, default=None,
    )
    briefing_cmd.add_argument(
        "--project-root", type=Path, default=None,
    )

    # --- context ---
    context_cmd = sub.add_parser(
        "context",
        help="360-degree view of a node: attributes + all connections (JSON)",
    )
    context_cmd.add_argument(
        "node_id",
        help="Node ID (e.g., 'py:function:orpheus.sn.solver.solve_sn').",
    )
    context_cmd.add_argument(
        "--db", type=Path, default=None,
    )
    context_cmd.add_argument(
        "--limit-per-type", type=int, default=25,
        help="Max entries per edge-type bucket, most-connected first "
             "(default: 25; 0 = no cap).",
    )

    # --- neighbors ---
    neighbors_cmd = sub.add_parser(
        "neighbors",
        help="Direct connections of a node (JSON)",
    )
    neighbors_cmd.add_argument(
        "node_id",
        help="Node ID to query.",
    )
    neighbors_cmd.add_argument(
        "--db", type=Path, default=None,
    )
    neighbors_cmd.add_argument(
        "--direction", default="both",
        choices=["in", "out", "both"],
        help="Edge direction: in, out, or both (default: both).",
    )
    neighbors_cmd.add_argument(
        "--edge-types", default="",
        help="Comma-separated edge types to filter (e.g., 'calls,imports').",
    )

    # --- trace ---
    trace_cmd = sub.add_parser(
        "trace",
        help="Trace from a failing test to equations on its call path (JSON)",
    )
    trace_cmd.add_argument(
        "test_node_id",
        help="Node ID of the failing test function.",
    )
    trace_cmd.add_argument(
        "--db", type=Path, default=None,
    )

    # --- doc-impact ---
    doc_cmd = sub.add_parser(
        "doc-impact",
        help="Which documented claims a change to this symbol puts in question",
    )
    doc_cmd.add_argument(
        "target",
        help="Node ID of the symbol being changed.",
    )
    doc_cmd.add_argument("--db", type=Path, default=None)
    doc_cmd.add_argument("--project-root", type=Path, default=None)
    doc_cmd.add_argument(
        "--unverified-only", action="store_true",
        help="Only claims no test declares it verifies.",
    )

    # --- retest ---
    retest_cmd = sub.add_parser(
        "retest",
        help="Minimum set of tests to re-run after changes (JSON)",
    )
    retest_cmd.add_argument(
        "--db", type=Path, default=None,
    )
    retest_cmd.add_argument(
        "--project-root", type=Path, default=None,
    )
    retest_cmd.add_argument(
        "--scope", default="all",
        choices=["staged", "unstaged", "all", "branch"],
        help="Git diff scope (default: all).",
    )

    # --- changes ---
    changes_cmd = sub.add_parser(
        "changes",
        help="Detect which symbols changed in git and their impact (JSON)",
    )
    changes_cmd.add_argument(
        "--db", type=Path, default=None,
    )
    changes_cmd.add_argument(
        "--project-root", type=Path, default=None,
    )
    changes_cmd.add_argument(
        "--scope", default="all",
        choices=["staged", "unstaged", "all", "branch"],
        help="Git diff scope (default: all).",
    )

    # --- communities ---
    communities_cmd = sub.add_parser(
        "communities",
        help="Detect functional communities of tightly connected symbols (JSON)",
    )
    communities_cmd.add_argument(
        "--db", type=Path, default=None,
    )
    communities_cmd.add_argument(
        "--min-size", type=int, default=3,
        help="Minimum community size (default: 3).",
    )

    # --- bridges ---
    bridges_cmd = sub.add_parser(
        "bridges",
        help="Find bridge nodes connecting separate communities (JSON)",
    )
    bridges_cmd.add_argument(
        "--db", type=Path, default=None,
    )
    bridges_cmd.add_argument(
        "--top-n", type=int, default=10,
        help="Number of bridges to return (default: 10).",
    )

    # --- god-nodes ---
    god_cmd = sub.add_parser(
        "god-nodes",
        help="Most connected nodes by degree (JSON)",
    )
    god_cmd.add_argument(
        "--db", type=Path, default=None,
    )
    god_cmd.add_argument(
        "--top-n", type=int, default=10,
        help="Number of nodes to return (default: 10).",
    )

    # --- native-place ---
    np_cmd = sub.add_parser(
        "native-place",
        help="Functions that may belong inside a class — Feature Envy / 'native place' (JSON)",
    )
    np_cmd.add_argument(
        "--db", type=Path, default=None,
    )
    np_cmd.add_argument(
        "--min-callers", type=int, default=1,
        help="Minimum considered (non-test) method callers (default: 1).",
    )
    np_cmd.add_argument(
        "--exclude", type=str, default="",
        help="Comma-separated substrings to drop, on top of is_test "
             "(e.g. 'scratch,derivations').",
    )
    np_cmd.add_argument(
        "--limit", type=int, default=50,
        help="Max candidates (default: 50; 0 = all).",
    )

    # --- twin-paths ---
    tp_cmd = sub.add_parser(
        "twin-paths",
        help="Independent implementations of the same computation — twin paths (JSON)",
    )
    tp_cmd.add_argument(
        "--db", type=Path, default=None,
    )
    tp_cmd.add_argument(
        "--min-similarity", type=float, default=0.7,
        help="Minimum Jaccard shingle overlap, 0.0-1.0 (default: 0.7; "
             "lower to ~0.6 for structurally-similar siblings).",
    )
    tp_cmd.add_argument(
        "--min-tokens", type=int, default=35,
        help="Minimum body token count; thinner stubs ignored (default: 35).",
    )
    tp_cmd.add_argument(
        "--exclude", type=str, default="",
        help="Comma-separated substrings to drop, on top of is_test "
             "(e.g. 'derivations,scratch').",
    )
    tp_cmd.add_argument(
        "--limit", type=int, default=50,
        help="Max pairs (default: 50; 0 = all).",
    )

    # --- discriminations ---
    disc_cmd = sub.add_parser(
        "discriminations",
        help="Tags discriminated at multiple sites — candidate missing types (JSON)",
    )
    disc_cmd.add_argument(
        "--db", type=Path, default=None,
    )
    disc_cmd.add_argument(
        "--min-sites", type=int, default=2,
        help="Minimum distinct discriminating functions per tag (default: 2).",
    )
    disc_cmd.add_argument(
        "--exclude", type=str, default="",
        help="Comma-separated substrings to drop, on top of is_test "
             "(e.g. 'derivations,scratch').",
    )
    disc_cmd.add_argument(
        "--limit", type=int, default=50,
        help="Max tags (default: 50; 0 = all).",
    )

    # --- dead-functions ---
    dead_cmd = sub.add_parser(
        "dead-functions",
        help="Functions/methods with no static callers — dead-code candidates (JSON)",
    )
    dead_cmd.add_argument(
        "--db", type=Path, default=None,
    )
    dead_cmd.add_argument(
        "--exclude", type=str, default="",
        help="Comma-separated substrings to drop (function or caller), on top "
             "of is_test (e.g. 'derivations,scratch').",
    )
    dead_cmd.add_argument(
        "--limit", type=int, default=50,
        help="Max results (default: 50; 0 = all).",
    )

    # --- dead-references ---
    # Deliberately offers a --format text mode, unlike its JSON-only
    # siblings: this is the command projects inject into an agent's
    # context with `!` (slash commands) or a hook. Pushing the FINDING
    # beats steering an agent to go look for it — steering is
    # probabilistic, injection is not, and a dead reference draws no
    # Sphinx warning at any severity, so nothing else raises it.
    deadref_cmd = sub.add_parser(
        "dead-references",
        help="Docs/docstrings citing symbols or equation labels that no "
             "longer exist (Sphinx renders these as plain text, no warning)",
    )
    deadref_cmd.add_argument(
        "--db", type=Path, default=None,
    )
    deadref_cmd.add_argument(
        "--limit", type=int, default=50,
        help="Max dead targets, most-referenced first (default: 50; 0 = all).",
    )
    deadref_cmd.add_argument(
        "--format", choices=("json", "text"), default="json",
        help="'text' is a compact human/agent-readable digest for context "
             "injection; 'json' is the full payload with every site.",
    )
    deadref_cmd.add_argument(
        "--quiet-when-clean", action="store_true",
        help="Print nothing when there are no dead references. For hooks "
             "and `!` injection: an empty finding must cost zero context.",
    )
    deadref_cmd.add_argument(
        "--exit-code", action="store_true",
        help="Exit 1 when any dead reference is found, so this can gate CI.",
    )

    # --- errors ---
    # Carries --format text for the same reason dead-references does: it
    # is a finding an agent did not ask for, and an uncaught catalogued
    # defect draws no warning from any build.
    errors_cmd = sub.add_parser(
        "errors",
        help="The catalogued failure modes (`.. error-entry::`) and the "
             "tests that catch them, least-covered first",
    )
    errors_cmd.add_argument(
        "--db", type=Path, default=None,
    )
    errors_cmd.add_argument(
        "--limit", type=int, default=50,
        help="Max entries, uncaught first (default: 50; 0 = all).",
    )
    errors_cmd.add_argument(
        "--format", choices=("json", "text"), default="json",
        help="'text' is a compact digest for context injection; 'json' is "
             "the full payload with every catcher.",
    )
    errors_cmd.add_argument(
        "--quiet-when-clean", action="store_true",
        help="Print nothing when every entry has a catcher and no marker "
             "dangles. For hooks and `!` injection: an empty finding must "
             "cost zero context.",
    )
    errors_cmd.add_argument(
        "--exit-code", action="store_true",
        help="Exit 1 when a catalogued defect has no catcher, or a marker "
             "names no entry, so this can gate CI.",
    )

    # --- protocol-conformers ---
    pc_cmd = sub.add_parser(
        "protocol-conformers",
        help="Classes satisfying a Protocol's method-set without declaring it (JSON)",
    )
    pc_cmd.add_argument(
        "--db", type=Path, default=None,
    )
    pc_cmd.add_argument(
        "--min-methods", type=int, default=2,
        help="Minimum Protocol method-set size (default: 2).",
    )
    pc_cmd.add_argument(
        "--exclude", type=str, default="",
        help="Comma-separated substrings to drop, on top of is_test.",
    )
    pc_cmd.add_argument(
        "--limit", type=int, default=50,
        help="Max Protocols (default: 50; 0 = all).",
    )

    # --- runtime overlay (dynamic execution-flow) ---
    rt_ing = sub.add_parser(
        "runtime-ingest",
        help="Ingest a cProfile/coverage trace and overlay it on the graph",
    )
    rt_ing.add_argument("artifact", type=Path,
                        help="cProfile/pstats dump, coverage json --branch "
                             "report, or viztracer JSON trace.")
    rt_ing.add_argument("--db", type=Path, default=None)
    rt_ing.add_argument("--kind", choices=["cprofile", "coverage", "viztracer", "pytest"],
                        default="cprofile")
    rt_ing.add_argument("--run", type=str, default="default",
                        help="Name to store under (re-ingest overwrites).")
    rt_ing.add_argument("--source-prefix", action="append", default=None,
                        metavar="PATH",
                        help="Keep only records under this path prefix "
                             "(drops stdlib/3rd-party frames). REPEATABLE, and "
                             "usually must be: profiling a test suite yields "
                             "tests->package records, so either directory "
                             "alone drops one endpoint of every one of them.")
    rt_ing.add_argument("--root", type=Path, default=None,
                        help="Directory that relative paths in the artifact "
                             "are relative to — i.e. the working directory the "
                             "traced run used. `coverage json` emits relative "
                             "keys and records this nowhere, so it cannot be "
                             "recovered from the artifact. Defaults to the "
                             "project root, else the current directory.")
    rt_ing.add_argument("--note", type=str, default="",
                        help="Free-text note of the workload, stored in metadata "
                             "under 'command'.")

    rt_runs = sub.add_parser(
        "runtime-runs", help="List ingested runtime runs (JSON)",
    )
    rt_runs.add_argument("--db", type=Path, default=None)

    rt_hot = sub.add_parser(
        "runtime-hotspots",
        help="Nodes ranked by an observed runtime metric — the dynamic stage DAG (JSON)",
    )
    rt_hot.add_argument("--db", type=Path, default=None)
    rt_hot.add_argument("--run", type=str, default="default",
                        help="Run name, or comma-separated names to union.")
    rt_hot.add_argument("--by", choices=["cumtime", "ncalls", "tottime"],
                        default="cumtime")
    rt_hot.add_argument("--limit", type=int, default=20,
                        help="Max nodes (default: 20; 0 = all).")

    rt_edges = sub.add_parser(
        "runtime-edges",
        help="Runtime call edges overlaid on static CALLS — dispatch/dead detection (JSON)",
    )
    rt_edges.add_argument("--db", type=Path, default=None)
    rt_edges.add_argument("--run", type=str, default="default",
                          help="Run name, or comma-separated names to union "
                               "(real cross-suite dead-code for --mode dead).")
    rt_edges.add_argument("--mode", choices=["dynamic_only", "fired", "dead"],
                          default="dynamic_only")
    rt_edges.add_argument("--node", type=str, default="",
                          help="Restrict to edges whose source id contains this.")
    rt_edges.add_argument("--substantive-only", action="store_true",
                          help="Drop edges where either endpoint is a property/"
                               "trivial accessor (surfaces polymorphic dispatch).")
    rt_edges.add_argument("--limit", type=int, default=50,
                          help="Max edges (default: 50; 0 = all).")

    rt_br = sub.add_parser(
        "runtime-branches",
        help="Per-node branch coverage — the missing-type / accidental-branch signal (JSON)",
    )
    rt_br.add_argument("--db", type=Path, default=None)
    rt_br.add_argument("--run", type=str, default="default",
                       help="Run name, or comma-separated names to union.")
    rt_br.add_argument("--node", type=str, default="",
                       help="Restrict to node ids containing this substring.")
    rt_br.add_argument("--all", action="store_true",
                       help="Include fully-covered nodes (default: partial only).")
    rt_br.add_argument("--limit", type=int, default=50,
                       help="Max nodes (default: 50; 0 = all).")

    rt_ex = sub.add_parser(
        "runtime-exercisers",
        help="Which tests EXECUTED a node — the falsifier for a coverage claim (JSON)",
    )
    rt_ex.add_argument("--db", type=Path, default=None)
    rt_ex.add_argument("--run", type=str, default="default",
                       help="Run name, or comma-separated names to union.")
    rt_ex.add_argument("--node", type=str, default="",
                       help="Restrict to node ids containing this substring.")
    rt_ex.add_argument("--limit", type=int, default=50,
                       help="Max nodes (default: 50; 0 = all).")

    rt_tl = sub.add_parser(
        "runtime-timeline",
        help="Observed execution sequence from a viztracer run — the stage DAG (JSON)",
    )
    rt_tl.add_argument("--db", type=Path, default=None)
    rt_tl.add_argument("--run", type=str, default="default")
    rt_tl.add_argument("--max-depth", type=int, default=-1,
                       help="Keep nodes with stack depth <= this (-1 = all; "
                            "small values give high-level stages).")
    rt_tl.add_argument("--limit", type=int, default=50,
                       help="Max nodes (default: 50; 0 = all).")

    # --- processes ---
    processes_cmd = sub.add_parser(
        "processes",
        help="Detect execution flows from entry points (JSON)",
    )
    processes_cmd.add_argument(
        "--db", type=Path, default=None,
    )
    processes_cmd.add_argument(
        "--min-length", type=int, default=3,
        help="Minimum chain length (default: 3).",
    )
    processes_cmd.add_argument(
        "--limit", type=int, default=0,
        help="Max chains to return. 0 = unlimited.",
    )
    processes_cmd.add_argument(
        "--offset", type=int, default=0,
        help="Skip this many chains from the start of the list.",
    )

    # --- shortest-path ---
    sp_cmd = sub.add_parser(
        "shortest-path",
        help="Find shortest path between two nodes (JSON)",
    )
    sp_cmd.add_argument(
        "source",
        help="Source node ID.",
    )
    sp_cmd.add_argument(
        "target",
        help="Target node ID.",
    )
    sp_cmd.add_argument(
        "--db", type=Path, default=None,
    )
    sp_cmd.add_argument(
        "--max-hops", type=int, default=8,
        help="Maximum path length (default: 8).",
    )

    # --- graph-query ---
    gq_cmd = sub.add_parser(
        "graph-query",
        help="Structured graph traversal query (JSON)",
    )
    gq_cmd.add_argument(
        "pattern",
        help="Query pattern, e.g. 'function -calls-> function'.",
    )
    gq_cmd.add_argument(
        "--db", type=Path, default=None,
    )
    gq_cmd.add_argument(
        "--limit", type=int, default=50,
        help="Maximum results (default: 50).",
    )

    # --- rename ---
    rename_cmd = sub.add_parser(
        "rename",
        help="Safe rename analysis: find all references (JSON)",
    )
    rename_cmd.add_argument(
        "old_name",
        help="Current symbol name.",
    )
    rename_cmd.add_argument(
        "new_name",
        help="New symbol name.",
    )
    rename_cmd.add_argument(
        "--db", type=Path, default=None,
    )
    rename_cmd.add_argument(
        "--project-root", type=Path, default=None,
    )
    rename_cmd.add_argument(
        "--apply", dest="apply_rename", action="store_true",
        help="Apply the renames (default: dry run).",
    )

    # --- callers ---
    callers_cmd = sub.add_parser(
        "callers",
        help="Functions that call this symbol (JSON)",
    )
    callers_cmd.add_argument(
        "node_id",
        help="Node ID of the function.",
    )
    callers_cmd.add_argument(
        "--db", type=Path, default=None,
    )
    callers_cmd.add_argument(
        "--transitive", action="store_true",
        help="Include indirect callers (depth 2+).",
    )
    callers_cmd.add_argument(
        "--max-depth", type=int, default=3,
        help="Max depth for transitive search (default: 3).",
    )

    # --- callees ---
    callees_cmd = sub.add_parser(
        "callees",
        help="Functions that this symbol calls (JSON)",
    )
    callees_cmd.add_argument(
        "node_id",
        help="Node ID of the function.",
    )
    callees_cmd.add_argument(
        "--db", type=Path, default=None,
    )
    callees_cmd.add_argument(
        "--transitive", action="store_true",
        help="Include indirect callees (depth 2+).",
    )
    callees_cmd.add_argument(
        "--max-depth", type=int, default=3,
        help="Max depth for transitive search (default: 3).",
    )

    # --- audit ---
    audit_cmd = sub.add_parser(
        "audit",
        help="Complete V&V audit: coverage + staleness + gaps (JSON)",
    )
    audit_cmd.add_argument(
        "--db", type=Path, default=None,
    )
    audit_cmd.add_argument(
        "--project-root", type=Path, default=None,
    )
    audit_cmd.add_argument(
        "--group-by",
        choices=["level", "module", "equation"],
        default=None,
        help="Bucket gaps by V&V level, Python module, or equation id.",
    )
    audit_cmd.add_argument(
        "--include-tests",
        action="store_true",
        help="Report tests_declared / tests_inferred counts in the summary.",
    )

    # --- gaps ---
    gaps_cmd = sub.add_parser(
        "gaps",
        help="V&V gaps: untagged tests, unverified equations, missing err catchers (JSON)",
    )
    gaps_cmd.add_argument(
        "--db", type=Path, default=None,
    )
    gaps_cmd.add_argument(
        "--module", default=None,
        help="Top-level Python package filter (e.g. 'orpheus').",
    )
    gaps_cmd.add_argument(
        "--level",
        choices=["L0", "L1", "L2", "L3"],
        default=None,
        help="V&V level filter.",
    )

    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 1

    # Resolve --db ONCE, here, instead of as an argparse default repeated
    # on each of the 36 subparsers. argparse cannot consult
    # ``.nexus/config.toml``, and a literal repeated 36 times is a literal
    # with 36 chances to drift. Post-parse, ``None`` unambiguously means
    # "the user said nothing", which is what the precedence chain needs.
    if hasattr(args, "db"):
        args.db = resolve_db(args.db, getattr(args, "project_root", None))

    verbose = getattr(args, "verbose", False)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    dispatch = {
        "setup": _run_setup,
        "analyze": _run_analyze,
        "serve": _run_serve,
        "workspaces": _run_workspaces,
        "config": _run_config,
        "file-brief": _run_file_brief,
        "status": _run_status,
        "query": _run_query,
        "impact": _run_impact,
        "provenance": _run_provenance,
        "ingest": _run_ingest,
        "visualize": _run_visualize,
        "coverage": _run_coverage,
        "staleness": _run_staleness,
        "migration": _run_migration,
        "briefing": _run_briefing,
        "context": _run_context,
        "neighbors": _run_neighbors,
        "trace": _run_trace,
        "doc-impact": _run_doc_impact,
        "retest": _run_retest,
        "changes": _run_changes,
        "communities": _run_communities,
        "bridges": _run_bridges,
        "god-nodes": _run_god_nodes,
        "native-place": _run_native_place,
        "twin-paths": _run_twin_paths,
        "discriminations": _run_discriminations,
        "dead-functions": _run_dead_functions,
        "dead-references": _run_dead_references,
        "errors": _run_errors,
        "protocol-conformers": _run_protocol_conformers,
        "runtime-ingest": _run_runtime_ingest,
        "runtime-runs": _run_runtime_runs,
        "runtime-hotspots": _run_runtime_hotspots,
        "runtime-edges": _run_runtime_edges,
        "runtime-branches": _run_runtime_branches,
        "runtime-exercisers": _run_runtime_exercisers,
        "runtime-timeline": _run_runtime_timeline,
        "processes": _run_processes,
        "shortest-path": _run_shortest_path,
        "graph-query": _run_graph_query,
        "rename": _run_rename,
        "callers": _run_callers,
        "callees": _run_callees,
        "audit": _run_audit,
        "gaps": _run_gaps,
    }
    handler = dispatch.get(args.command)
    if handler:
        return handler(args)
    return 0


# ------------------------------------------------------------------
# Command handlers
# ------------------------------------------------------------------


def _run_config(args: argparse.Namespace) -> int:
    """Resolved settings, for consumers that cannot import Python.

    With a ``key``, prints that value bare and nothing else, so a shell
    can capture it: ``db=$(nexus config db --project-root "$root")``.
    A list prints one item per line, which is what ``read -r`` and
    ``$(...)`` word-splitting expect.

    Without a key, prints every setting as ``key = value`` for a human.

    Exit codes carry the meaning a script needs: ``0`` the value is
    known, ``1`` unset or no such key. An UNSET setting prints nothing
    and fails, rather than printing a default the caller would then have
    no way to distinguish from a declared value.
    """
    from sphinxcontrib.nexus.project import ProjectConfig

    start = args.project_root or Path.cwd()
    config = ProjectConfig.load(start)
    resolved: dict[str, object | None] = {
        "root": config.root,
        "config": config.source,
        # `db` is the one entry that always answers: an unconfigured
        # project still has a graph, at the legacy default. Resolving it
        # HERE rather than letting the caller fall back keeps the default
        # in one place too.
        #
        # Anchored to `start`, not to the cwd. `resolve_db` returns the
        # legacy default as a RELATIVE path — correct for the CLI's own
        # verbs, which have always read it relative to wherever they were
        # invoked — but this verb's whole contract is to answer for the
        # project root it was given, and a relative answer would be
        # resolved against the CALLER's directory instead.
        "db": (start / resolve_db(None, start)).resolve(),
        "output": config.output,
        "scope.prefixes": config.scope_prefixes,
        "catalog.errors": config.resolved_catalog_errors(),
    }

    def emit(value: object) -> None:
        for item in value if isinstance(value, list) else [value]:
            print(item)

    def render(value: object | None) -> str:
        if value is None:
            return "(unset)"
        if isinstance(value, list):
            return " ".join(str(item) for item in value)
        return str(value)

    if args.key is None:
        for name, value in resolved.items():
            print(f"{name} = {render(value)}")
        return 0

    if args.key not in resolved:
        print(
            f"Error: unknown setting {args.key!r} — known: "
            f"{', '.join(_CONFIG_KEYS)}",
            file=sys.stderr,
        )
        return 1

    value = resolved[args.key]
    if value is None:
        print(f"Error: {args.key} is not set", file=sys.stderr)
        return 1
    emit(value)
    return 0


def _workspace_for(args: argparse.Namespace) -> Workspace:
    """The checkout this invocation is about, paired with its graph.

    One home for a resolution that had been written out six times as
    ``args.project_root or Path.cwd()`` and omitted everywhere else —
    so most query subcommands could not answer a git-aware question at
    all, and the six that could were wrong when run from a
    subdirectory: ``git diff`` reports repository-relative paths, which
    then fail to match nodes made relative to ``docs/`` and produce a
    confident "nothing changed". Falling back to the discovered project
    root rather than the cwd is what makes the two halves agree.
    """
    declared = getattr(args, "project_root", None)
    root = declared or find_project_root(Path.cwd()) or Path.cwd()
    return Workspace(db_path=args.db.resolve(), root=Path(root).resolve())


def _load_query(args: argparse.Namespace) -> GraphQuery:
    from sphinxcontrib.nexus.export import load_sqlite
    from sphinxcontrib.nexus.query import GraphQuery

    db_path = args.db
    if not db_path.exists():
        # Say WHICH of the two causes this is. Most query subcommands take
        # no --project-root, so they resolve from the cwd — and running one
        # from outside the project produced "does not exist / run analyze
        # first", which reads as "the graph was never built" when the truth
        # is "you are in the wrong directory". A wrong remediation is worse
        # than none: it sends the reader off to rebuild a graph they have.
        from sphinxcontrib.nexus.project import CONFIG_DIR

        print(f"Error: {db_path} does not exist", file=sys.stderr)
        if find_project_root(Path.cwd()) is None:
            print(
                f"No {CONFIG_DIR}/ directory above {Path.cwd()}, so this is "
                "the built-in default rather than a declared path. If the "
                "project is elsewhere, run this from inside it.",
                file=sys.stderr,
            )
        else:
            print(
                "That is the path this project declares (see 'nexus "
                "config'). Run 'nexus analyze' or 'sphinx-build' to "
                "create it.",
                file=sys.stderr,
            )
        sys.exit(1)
    return GraphQuery(load_sqlite(db_path), workspace=_workspace_for(args))


def _setup_targets(
    args: argparse.Namespace,
) -> tuple[Path, Path | None, Path | None, Path | None, Path]:
    """Resolve targets for (skills, rules, commands, hooks, manifest).

    ``rules_target`` is ``None`` when the caller declined the always-on
    routing rule, or when installing globally — a rule that auto-loads
    into EVERY project must be an explicit per-project choice, not a
    side effect of a global skill install.
    """
    if args.target:
        skills_target = args.target.resolve()
        claude_dir = skills_target.parent
    elif args.global_install:
        claude_dir = Path.home() / ".claude"
        skills_target = claude_dir / "skills"
    else:
        claude_dir = Path.cwd() / ".claude"
        skills_target = claude_dir / "skills"

    rules_target: Path | None = claude_dir / "rules"
    commands_target: Path | None = claude_dir / "commands"
    hooks_target: Path | None = claude_dir / "hooks"
    if getattr(args, "no_rules", False) or args.global_install:
        rules_target = None
    # Commands and hooks are project-shaped (they reference the
    # project's graph path), so a global install never places them.
    if args.global_install:
        commands_target = hooks_target = None
    return (skills_target, rules_target, commands_target, hooks_target,
            claude_dir / MANIFEST_NAME)


def _run_setup(args: argparse.Namespace) -> int:
    import shutil

    from sphinxcontrib.nexus.install import (
        classify,
        diff_payload,
        install_payload,
        iter_payloads,
        load_manifest,
        write_manifest,
    )

    package_root = Path(__file__).parent
    if not (package_root / "skills").is_dir():
        print(
            f"Error: bundled skills not found at {package_root / 'skills'}",
            file=sys.stderr,
        )
        return 1

    (skills_target, rules_target, commands_target, hooks_target,
     manifest_path) = _setup_targets(args)
    manifest = load_manifest(manifest_path)
    payloads = list(iter_payloads(
        package_root, skills_target, rules_target,
        commands_target, hooks_target,
    ))
    statuses = {p.key: classify(p, manifest) for p in payloads}

    # --- read-only modes -------------------------------------------------
    if args.check or args.diff:
        modified = [p for p in payloads
                    if statuses[p.key].state.startswith("modified")]
        stale = [p for p in payloads if statuses[p.key].state == "stale"]
        missing = [p for p in payloads if statuses[p.key].state == "missing"]

        if args.diff:
            if not modified:
                print("No locally-modified files — nothing to harvest.")
            for payload in modified:
                lines = diff_payload(payload)
                if not lines:
                    continue
                print(f"\n=== {payload.key} "
                      f"({statuses[payload.key].state}) ===")
                print("".join(lines), end="")
            if modified:
                print(
                    "\n'+' lines are the consumer's and not ours — the "
                    "harvest direction. A local edit may be the better "
                    "version; read before overwriting."
                )
            return 1 if modified else 0

        print(f"nexus {__version__} — {len(payloads)} bundled files")
        print(f"  skills → {skills_target}")
        if rules_target is not None:
            print(f"  rules  → {rules_target}")
        for label, group in (
            ("missing (never installed)", missing),
            ("stale (safe to update)", stale),
            ("locally modified (NOT overwritten without --force)", modified),
        ):
            if not group:
                continue
            print(f"\n{label}: {len(group)}")
            for payload in group:
                version = statuses[payload.key].installed_version or "unknown"
                print(f"  {payload.key}  (installed from {version})")
        if not (missing or stale or modified):
            print("\nEverything up to date.")
            return 0
        if modified:
            print("\nRun 'nexus setup --diff' to see what the consumer changed.")
        return 1

    # --- install ---------------------------------------------------------
    written: list[Payload] = []
    skipped: list[Payload] = []
    for payload in payloads:
        state = statuses[payload.key].state
        if state.startswith("modified") and not args.force:
            skipped.append(payload)
            continue
        # --force over a local edit keeps a .bak: the edit may be the
        # better version and this tool must not be able to destroy it.
        install_payload(
            payload, backup=args.force and state.startswith("modified"),
        )
        written.append(payload)

    write_manifest(manifest_path, written, manifest)

    skill_names = sorted({
        p.key.split("/")[1] for p in payloads if p.key.startswith("skills/")
    })
    print(f"Installed {len(written)} files "
          f"({len(skill_names)} skills) to {skills_target}/")
    for name in skill_names:
        print(f"  {name}/")
    if rules_target is not None:
        rule_files = [p for p in written if p.key.startswith("rules/")]
        if rule_files:
            print(f"\nInstalled always-on routing rule to {rules_target}/")
            for payload in rule_files:
                print(f"  {payload.dest.name}")
            print("  (reference it from CLAUDE.md so it auto-loads; "
                  "skip with --no-rules)")

    cmd_files = [p for p in written if p.key.startswith("commands/")]
    hook_files = [p for p in written if p.key.startswith("hooks/")]
    if cmd_files or hook_files:
        print("\nPush channels — these inject findings instead of waiting "
              "to be asked:")
        for payload in cmd_files:
            print(f"  /{payload.dest.stem}  (slash command; its `!` lines "
                  f"run at invocation)")
        for payload in hook_files:
            print(f"  {payload.dest}  (wire into .claude/settings.json; "
                  f"see the header)")

    if skipped:
        print(f"\nSKIPPED {len(skipped)} locally-modified file(s) — not "
              f"overwritten:")
        for payload in skipped:
            print(f"  {payload.key}")
        print("  Review with 'nexus setup --diff' (their edit may be the "
              "better version), then 'nexus setup --force' to replace "
              "(keeps a .bak).")

    # Install MCP server configuration. Paths are anchored on
    # ${CLAUDE_PROJECT_DIR} (set by Claude Code in the spawned server's
    # environment, with `:-.` as the fallback for other MCP clients)
    # rather than on the spawn cwd, which is unspecified.
    project_dir = "${CLAUDE_PROJECT_DIR:-.}"
    nexus_cmd = shutil.which("nexus") or f"{project_dir}/.venv/bin/nexus"
    # No --db: the server resolves it from --project-root via
    # .nexus/config.toml, falling back to the legacy default. Emitting one
    # here would re-introduce a second declaration of the graph path that
    # nothing can detect diverging from where the build actually writes.
    nexus_server_config = {
        "command": nexus_cmd,
        "args": ["serve", "--project-root", project_dir],
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
        # Project-level: add to .mcp.json. An EXISTING nexus entry is
        # left alone unless --force: projects legitimately point the
        # server somewhere other than the default (a non-standard build
        # dir, another checkout's graph, an absolute interpreter path),
        # and silently rewriting that to the template breaks every query
        # in the project with no error — the server starts fine, it just
        # answers from a database that isn't there. Same no-clobber
        # discipline the skills and rules already get.
        mcp_json = Path.cwd() / ".mcp.json"
        if mcp_json.exists():
            try:
                existing = json.loads(mcp_json.read_text())
            except ValueError:
                print(f"\n{mcp_json} is not valid JSON — leaving it alone. "
                      f"Add the nexus server manually:", file=sys.stderr)
                print(json.dumps({"nexus": nexus_server_config}, indent=2))
                existing = None
            if existing is not None:
                servers = existing.setdefault("mcpServers", {})
                if "nexus" in servers and servers["nexus"] != nexus_server_config:
                    if args.force:
                        servers["nexus"] = nexus_server_config
                        mcp_json.write_text(json.dumps(existing, indent=2) + "\n")
                        print(f"\nReplaced the customized nexus entry in "
                              f"{mcp_json} (--force)")
                    else:
                        print(f"\nKept the existing customized nexus entry in "
                              f"{mcp_json} — pass --force to replace it with "
                              f"the default.")
                else:
                    servers["nexus"] = nexus_server_config
                    mcp_json.write_text(json.dumps(existing, indent=2) + "\n")
                    print(f"\nUpdated {mcp_json} with nexus MCP server "
                          f"(project-level)")
        else:
            mcp_json.write_text(json.dumps({"mcpServers": {"nexus": nexus_server_config}}, indent=2) + "\n")
            print(f"\nCreated {mcp_json} with nexus MCP server (project-level)")

    # Suggest a PostToolUse hook for auto-rebuild after git commit.
    # Field semantics per the Claude Code hooks schema: "if" filters
    # with permission-rule syntax on the individual hook entry;
    # "async" runs without blocking (no shell '&' needed); "timeout"
    # is in SECONDS (default 600) so it is omitted here.
    settings_dir = Path.cwd() / ".claude"
    settings_dir.mkdir(exist_ok=True)
    print("\nTo auto-rebuild the graph after git commits, add this hook to .claude/settings.json:")
    print("""
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "if": "Bash(git commit:*)",
            "command": ".venv/bin/python -m sphinx -b html docs docs/_build/html -q",
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
    from sphinxcontrib.nexus.merge import merge_graphs, reconcile_unresolved

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
        # merge_graphs no longer reconciles — that decision needs the
        # whole graph, which here is everything merged above.
        reconcile_unresolved(merged)
        print(f"Merged with existing graph from {args.db}")
    else:
        merged = ast_graph

    from sphinxcontrib.nexus.workspace import stamp_provenance
    stamp_provenance(merged, project_root.resolve())

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

    serve(db_path=db_path, project_root=_workspace_for(args).root)
    return 0


def _run_workspaces(args: argparse.Namespace) -> int:
    from sphinxcontrib.nexus.workspace import GitProvenance, discover

    for status in discover(_workspace_for(args)):
        entry = status.to_payload()
        marker = "*" if entry["is_active"] else " "
        graph = (
            f"graph built {entry['graph_built']}"
            if entry["has_graph"] else "no graph"
        )
        prov = GitProvenance.from_stamp(entry["provenance"])
        stamped = f"  (from {prov.branch}@{prov.commit})" if prov else ""
        print(f"{marker} {entry['root']}  [{entry['branch']}]  {graph}{stamped}")
    return 0


def _run_file_brief(args: argparse.Namespace) -> int:
    from dataclasses import asdict

    from sphinxcontrib.nexus.brief import file_brief, render_text

    if not args.db.is_file():
        print(f"No graph database at {args.db}", file=sys.stderr)
        return 1
    brief = file_brief(
        args.db, args.file, project_root=_workspace_for(args).root,
    )
    if brief is None:
        print(
            f"{args.file} is not in the graph (new file, excluded "
            f"tree, or stale build)",
            file=sys.stderr,
        )
        return 1
    if args.json_out:
        print(json.dumps(asdict(brief), indent=2))
    else:
        print(render_text(brief))
    return 0


def _run_doc_impact(args: argparse.Namespace) -> int:
    q = _load_query(args)
    result = q.doc_impact(args.target)
    claims = [c for c in result.claims if not (args.unverified_only and c.verified)]
    if not claims and not result.pages:
        print(f"No documented claim reachable from {args.target}")
        return 0
    for c in claims:
        mark = " " if c.verified else "!"
        guess = "  (inferred)" if c.inferred else ""
        print(f"  {mark} {c.location:58s} {c.equation.name}{guess}")
        print(f"      via {c.implemented_by.name}  (depth {c.depth})")
    if result.pages:
        print("\n  pages documenting the cone (no anchor — page-level edge):")
        for p in result.pages:
            print(f"      {p.id}")
    print(f"\nCone: {result.cone_size} symbols; claims: {len(result.claims)}"
          f" ({result.unverified} unverified, marked !)")
    return 0


def _run_status(args: argparse.Namespace) -> int:
    q = _load_query(args)
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
    q = _load_query(args)
    types = [t.strip() for t in args.node_types.split(",") if t.strip()] or None
    results = q.query(args.text, node_types=types, limit=args.limit)
    if not results:
        print("No results found.")
        return 0
    for r in results:
        print(f"  {r.id:55s}  type={r.type:12s}  degree={r.degree}")
    return 0


def _run_impact(args: argparse.Namespace) -> int:
    from sphinxcontrib.nexus._serialize import assemble_impact

    q = _load_query(args)
    result = assemble_impact(
        q,
        args.target,
        direction=args.direction,
        max_depth=args.depth,
        per_depth_limit=args.limit_per_depth if args.limit_per_depth > 0 else None,
        only=args.only,
    )
    if result["total_affected"] == 0:
        print(f"No {'upstream' if args.direction == 'upstream' else 'downstream'} "
              f"dependents found for {args.target}")
        return 0
    omitted = result.get("omitted", {})
    for depth, nodes in result["by_depth"].items():
        label = {1: "WILL BREAK", 2: "LIKELY AFFECTED", 3: "MAY NEED TESTING"}.get(
            depth, f"depth={depth}",
        )
        print(f"  d={depth} ({label}):")
        for n in nodes:
            # The id opens with the type, so `type=function` beside
            # `py:function:…` restated it. The position is what you act
            # on next, so that is what earns the column — and for a test
            # what you act on next is the command, not the line.
            test = n.get("test", {})
            if test.get("pytest_id"):
                at = f"  {test['pytest_id']}"
            elif n.get("file_path"):
                at = f"  {n['file_path']}:{n['lineno']}"
            else:
                at = ""
            print(f"    {n['id']:55s}{at}")
        if depth in omitted:
            print(f"    ... (+{omitted[depth]} more; --limit-per-depth 0 for all)")
    print(f"\nTotal affected: {result['total_affected']}")
    if result.get("only"):
        print(f"Shown ({result['only']}): {result['total_in_role']}")
    return 0


def _run_provenance(args: argparse.Namespace) -> int:
    q = _load_query(args)
    result = q.provenance_chain(args.target)
    if not result.chain:
        print(f"No provenance chain found for {args.target}")
        return 0
    for step in result.chain:
        indent = "  " * step.depth
        # A guess wears its reason. Declared prints nothing extra, which
        # is what makes the marked ones visible at a glance.
        guess = (
            f"  (inferred via {', '.join(step.via or [])})"
            if step.inferred else ""
        )
        print(f"{indent}{step.edge_type}: {step.node.id} ({step.node.type}){guess}")
    if result.citations:
        print(f"\nCitations: {', '.join(result.citations)}")
    if result.also_on_these_pages:
        # Labelled, and named for what is weak about it: being on a page
        # is not implementing. Printed even when `equations` is empty —
        # that pairing IS the signal for "documented here, implementing
        # nothing known", as opposed to "nothing known at all".
        print("\n  also on these pages (adjacency, not implementation):")
        for page in result.also_on_these_pages:
            print(f"      {page.id}")
    return 0


def _run_coverage(args: argparse.Namespace) -> int:
    q = _load_query(args)
    filt = args.status if args.status else None
    result = q.verification_coverage(status_filter=filt)
    print("Summary:")
    for status, count in sorted(result.summary.items()):
        print(f"  {status:20s} {count}")
    print()
    if result.entries:
        offset = max(args.offset, 0)
        if args.limit > 0:
            shown = result.entries[offset : offset + args.limit]
        else:
            shown = result.entries[offset:]
        for e in shown:
            print(f"  [{e.status:12s}] {e.node.id}")
        total = len(result.entries)
        if len(shown) < total - offset or offset > 0:
            print(f"  ... ({len(shown)} shown / {total} total)")
    return 0


def _run_staleness(args: argparse.Namespace) -> int:
    q = _load_query(args)
    result = q.staleness()
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
    q = _load_query(args)
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


# ------------------------------------------------------------------
# JSON CLI commands — mirror MCP tools for ! injection
#
# These use shared assembly functions from _serialize.py so CLI and
# MCP server produce identical JSON by construction.
# ------------------------------------------------------------------


def _json_out(data) -> int:
    """Print JSON to stdout and return 0."""
    from sphinxcontrib.nexus._serialize import to_json
    print(to_json(data))
    return 0


def _run_briefing(args: argparse.Namespace) -> int:
    from sphinxcontrib.nexus._serialize import to_dict
    q = _load_query(args)
    return _json_out(to_dict(q.session_briefing()))


def _run_native_place(args: argparse.Namespace) -> int:
    from sphinxcontrib.nexus._serialize import to_dict
    q = _load_query(args)
    toks = tuple(t.strip() for t in args.exclude.split(",") if t.strip())
    results = q.native_place_candidates(
        min_callers=args.min_callers, exclude=toks, limit=args.limit,
    )
    return _json_out(to_dict(results))


def _run_twin_paths(args: argparse.Namespace) -> int:
    from sphinxcontrib.nexus._serialize import to_dict
    q = _load_query(args)
    toks = tuple(t.strip() for t in args.exclude.split(",") if t.strip())
    results = q.twin_paths(
        min_similarity=args.min_similarity, min_tokens=args.min_tokens,
        exclude=toks, limit=args.limit,
    )
    return _json_out(to_dict(results))


def _run_discriminations(args: argparse.Namespace) -> int:
    from sphinxcontrib.nexus._serialize import to_dict
    q = _load_query(args)
    toks = tuple(t.strip() for t in args.exclude.split(",") if t.strip())
    results = q.discriminations(
        min_sites=args.min_sites, exclude=toks, limit=args.limit,
    )
    return _json_out(to_dict(results))


def _run_errors(args: argparse.Namespace) -> int:
    from sphinxcontrib.nexus._serialize import to_dict

    q = _load_query(args)
    result = q.errors()
    entries = result.entries if args.limit <= 0 else result.entries[: args.limit]
    findings = result.uncaught + len(result.unresolved_markers)

    if args.format == "json":
        payload = to_dict(result)
        payload["entries"] = payload["entries"][: len(entries)]
        _json_out(payload)
        return 1 if (args.exit_code and findings) else 0

    # An absence must say where it looked: a project that has declared
    # nothing and a project whose catalogue is fully covered both report
    # zero findings, and they are not the same state.
    if not result.total_entries:
        if not args.quiet_when_clean:
            print(
                "No error catalogue: nothing declares `.. error-entry::`, so "
                f"{len(result.unresolved_markers)} `catches` marker id(s) "
                "resolve to nothing."
            )
        return 1 if (args.exit_code and result.unresolved_markers) else 0

    if not findings:
        if not args.quiet_when_clean:
            print(
                f"Error catalogue: {result.total_entries} entries, every one "
                f"caught ({result.total_catchers} catcher(s))."
            )
        return 0

    print(
        f"ERROR CATALOGUE — {result.uncaught} of {result.total_entries} "
        f"catalogued defect(s) have NO test claiming to catch them. A "
        f"catalogued defect with no catcher is an unpinned regression: "
        f"either write the gate, or say in the entry why none can exist."
    )
    for entry in entries:
        if entry.catcher_count:
            continue
        where = f"  ({entry.docname})" if entry.docname else ""
        print(f"\n  {entry.name} — {entry.title or '(untitled)'}{where}")
    if result.unresolved_markers:
        print(
            f"\n{len(result.unresolved_markers)} `catches` marker id(s) name "
            f"no declared entry — these READ as coverage and are not:"
        )
        for tag in result.unresolved_markers:
            print(f"      {tag}")
    return 1 if args.exit_code else 0


def _run_dead_references(args: argparse.Namespace) -> int:
    from sphinxcontrib.nexus._serialize import to_dict

    q = _load_query(args)
    result = q.dead_references()
    dead = result.dead if args.limit <= 0 else result.dead[: args.limit]

    if args.format == "json":
        payload = to_dict(result)
        payload["dead"] = payload["dead"][: len(dead)]
        _json_out(payload)
        return 1 if (args.exit_code and result.total_dead) else 0

    if not result.total_dead:
        if not args.quiet_when_clean:
            print("No dead documentation references.")
        return 0

    # Text mode leads with the imperative, not the count: this text is
    # read by an agent that did not ask for it, so it has to say what
    # the finding IS and what to do about it before any detail.
    print(
        f"DEAD DOCUMENTATION REFERENCES — {result.total_dead} target(s) "
        f"referenced from {result.total_sites} site(s). These docs cite "
        f"code or equations that no longer exist; Sphinx renders them as "
        f"plain text with no warning. Each needs updating or removing."
    )
    for entry in dead:
        print(f"\n  {entry.target_name}  [{entry.kind}] "
              f"— {entry.site_count} site(s)")
        for site in entry.sites[:4]:
            where = site.source.file_path or site.source.docname or ""
            line = f":{site.source.lineno}" if site.source.lineno else ""
            print(f"      {site.source.id}{f'  ({where}{line})' if where else ''}")
        if entry.site_count > 4:
            print(f"      … and {entry.site_count - 4} more")
        if entry.minted_by:
            # The target is not simply absent — this code names it, and a
            # reference elsewhere bound to the placeholder that created.
            # When these all sit in one unmaintained corner of the tree,
            # that directory is the finding, not the reference.
            print("      minted by code in:")
            for path in entry.minted_by:
                print(f"        {path}")
    if result.total_dead > len(dead):
        print(f"\n  … and {result.total_dead - len(dead)} more target(s)")
    return 1 if args.exit_code else 0


def _run_dead_functions(args: argparse.Namespace) -> int:
    from sphinxcontrib.nexus._serialize import to_dict
    q = _load_query(args)
    toks = tuple(t.strip() for t in args.exclude.split(",") if t.strip())
    results = q.dead_functions(exclude=toks, limit=args.limit)
    return _json_out(to_dict(results))


def _run_protocol_conformers(args: argparse.Namespace) -> int:
    from sphinxcontrib.nexus._serialize import to_dict
    q = _load_query(args)
    toks = tuple(t.strip() for t in args.exclude.split(",") if t.strip())
    results = q.protocol_conformers(
        min_methods=args.min_methods, exclude=toks, limit=args.limit,
    )
    return _json_out(to_dict(results))


def _runtime_store(db_path: Path):
    from sphinxcontrib.nexus.runtime import RuntimeStore
    return RuntimeStore.beside(db_path)


def _runtime_load(db_path: Path, name: str):
    store = _runtime_store(db_path)
    run = store.load(name)
    if run is None:
        avail = [r["name"] for r in store.list_runs()]
        print(f"Error: no runtime run {name!r}; available: {avail or '(none)'}",
              file=sys.stderr)
        sys.exit(1)
    return run


def _runtime_load_many(db_path: Path, names: str):
    """Load one run, or merge a comma-separated set (canonical-suite union)."""
    from sphinxcontrib.nexus.runtime import load_and_merge
    return load_and_merge(names, lambda n: _runtime_load(db_path, n))


def _run_runtime_ingest(args: argparse.Namespace) -> int:
    from sphinxcontrib.nexus import runtime as rt
    from sphinxcontrib.nexus.project import ProjectConfig

    # A table, not a conditional chain. It was
    # `ingest_cprofile if kind == "cprofile" else ingest_coverage` — a
    # BINARY branch over three registered choices, so `--kind viztracer`
    # ran the coverage ingester on a viztracer file, bound nothing, and
    # reported `nodes: 0` with exit 0. The same defect this command is
    # being fixed for, one layer up. The count comes from the table too,
    # because each kind fills a different family and the old
    # `len(calls) or len(coverage)` read 0 for a SUCCESSFUL viztracer run.
    backends = {
        "cprofile": (rt.ingest_cprofile, lambda r: len(r.calls)),
        "coverage": (rt.ingest_coverage, lambda r: len(r.coverage)),
        "viztracer": (rt.ingest_viztracer, lambda r: len(r.timeline)),
        "pytest": (rt.ingest_pytest, lambda r: len(r.markers)),
    }
    if not args.artifact.exists():
        print(f"Error: {args.artifact} does not exist", file=sys.stderr)
        return 1
    ingest, count = backends[args.kind]
    q = _load_query(args)
    root = args.root or ProjectConfig.load(Path.cwd()).root
    meta = {"command": args.note} if args.note else {}
    run = ingest(args.artifact, q.knowledge_graph, args.run,
                 meta=meta, source_prefixes=args.source_prefix, root=root)

    bound = count(run)
    ledger = run.ledger
    diagnosis = ledger.diagnosis()
    if diagnosis is None:
        path = _runtime_store(args.db).write(run)
        print(f"Ingested {args.kind} run '{run.name}' -> {path}")
    else:
        # Refuse to store a run that joined nothing. A stored empty run is
        # worse than no run: it appears in `runtime-runs`, and every query
        # against it answers "nothing fired" with total confidence.
        print(f"Ingested NOTHING from {args.artifact} ({args.kind}).",
              file=sys.stderr)

    print(f"  nodes:      {bound}")
    print(f"  edges:      {len(run.edges)}")
    print(f"  lookups:    {ledger.considered}")
    print(f"    bound:              {ledger.bound}")
    print(f"    outside scope:      {ledger.outside_scope}")
    print(f"    file not in graph:  {ledger.unindexed_file}")
    print(f"    no enclosing node:  {ledger.no_enclosing_node}")
    # Per-test attribution reports only when the capture carried contexts,
    # because for every other run the honest line is no line at all. When
    # it DID carry them, both numbers are printed even at zero: a silent
    # summary cannot distinguish "contexts resolved" from "every context
    # was a spelling I do not understand", which is the same
    # nothing-found-vs-wrong-place confusion the ledger exists to remove.
    if run.exercised_by or ledger.unknown_context:
        print(f"  tests attributed to {len(run.exercised_by)} node(s)")
        print(f"    unresolved contexts: {ledger.unknown_context}")

    if diagnosis is not None:
        print(f"\n  root was: {Path(root).resolve()}", file=sys.stderr)
        print(f"  {diagnosis}", file=sys.stderr)
        return 1
    return 0


def _run_runtime_runs(args: argparse.Namespace) -> int:
    return _json_out(_runtime_store(args.db).list_runs())


def _run_runtime_hotspots(args: argparse.Namespace) -> int:
    from sphinxcontrib.nexus._serialize import to_dict
    q = _load_query(args)
    run = _runtime_load_many(args.db, args.run)
    results = q.runtime_hotspots(run, by=args.by, limit=args.limit)
    return _json_out(to_dict(results))


def _run_runtime_edges(args: argparse.Namespace) -> int:
    from sphinxcontrib.nexus._serialize import to_dict
    q = _load_query(args)
    run = _runtime_load_many(args.db, args.run)
    results = q.runtime_edges(
        run, mode=args.mode, node=args.node,
        substantive_only=args.substantive_only, limit=args.limit)
    return _json_out(to_dict(results))


def _run_runtime_branches(args: argparse.Namespace) -> int:
    from sphinxcontrib.nexus._serialize import to_dict
    q = _load_query(args)
    run = _runtime_load_many(args.db, args.run)
    results = q.runtime_branches(
        run, node=args.node, partial_only=not args.all, limit=args.limit)
    return _json_out(to_dict(results))


def _run_runtime_exercisers(args: argparse.Namespace) -> int:
    from sphinxcontrib.nexus._serialize import to_dict
    q = _load_query(args)
    run = _runtime_load_many(args.db, args.run)
    results = q.runtime_exercisers(run, node=args.node, limit=args.limit)
    return _json_out(to_dict(results))


def _run_runtime_timeline(args: argparse.Namespace) -> int:
    from sphinxcontrib.nexus._serialize import to_dict
    q = _load_query(args)
    run = _runtime_load(args.db, args.run)
    results = q.runtime_timeline(run, max_depth=args.max_depth, limit=args.limit)
    return _json_out(to_dict(results))


def _run_context(args: argparse.Namespace) -> int:
    from sphinxcontrib.nexus._serialize import assemble_context
    q = _load_query(args)
    return _json_out(assemble_context(
        q,
        args.node_id,
        per_type_limit=args.limit_per_type if args.limit_per_type > 0 else None,
    ))


def _run_neighbors(args: argparse.Namespace) -> int:
    from sphinxcontrib.nexus._serialize import assemble_neighbors
    q = _load_query(args)
    types = [t.strip() for t in args.edge_types.split(",") if t.strip()] or None
    return _json_out(assemble_neighbors(q, args.node_id, direction=args.direction, edge_types=types))


def _run_trace(args: argparse.Namespace) -> int:
    from sphinxcontrib.nexus._serialize import to_dict
    q = _load_query(args)
    return _json_out(to_dict(q.trace_error(args.test_node_id)))


def _run_retest(args: argparse.Namespace) -> int:
    from sphinxcontrib.nexus._serialize import to_dict
    q = _load_query(args)
    return _json_out(to_dict(q.retest(scope=args.scope)))


def _run_changes(args: argparse.Namespace) -> int:
    from sphinxcontrib.nexus._serialize import to_dict
    q = _load_query(args)
    return _json_out(to_dict(q.detect_changes(scope=args.scope)))


def _run_communities(args: argparse.Namespace) -> int:
    from sphinxcontrib.nexus._serialize import assemble_communities
    q = _load_query(args)
    return _json_out(assemble_communities(q, min_size=args.min_size))


def _run_bridges(args: argparse.Namespace) -> int:
    from sphinxcontrib.nexus._serialize import to_dict
    q = _load_query(args)
    return _json_out(to_dict(q.bridges(top_n=args.top_n)))


def _run_god_nodes(args: argparse.Namespace) -> int:
    from sphinxcontrib.nexus._serialize import to_dict
    q = _load_query(args)
    return _json_out(to_dict(q.god_nodes(top_n=args.top_n)))


def _run_processes(args: argparse.Namespace) -> int:
    from sphinxcontrib.nexus._serialize import assemble_processes
    q = _load_query(args)
    return _json_out(
        assemble_processes(
            q,
            min_length=args.min_length,
            limit=args.limit if args.limit > 0 else None,
            offset=args.offset,
        )
    )


def _run_shortest_path(args: argparse.Namespace) -> int:
    from sphinxcontrib.nexus._serialize import assemble_shortest_path
    q = _load_query(args)
    return _json_out(assemble_shortest_path(q, args.source, args.target, max_hops=args.max_hops))


def _run_graph_query(args: argparse.Namespace) -> int:
    q = _load_query(args)
    return _json_out(q.graph_query(args.pattern, limit=args.limit))


def _run_rename(args: argparse.Namespace) -> int:
    from sphinxcontrib.nexus._serialize import to_dict
    q = _load_query(args)
    return _json_out(to_dict(q.rename(
        args.old_name, args.new_name,
        dry_run=not args.apply_rename,
    )))


def _run_callers(args: argparse.Namespace) -> int:
    from sphinxcontrib.nexus._serialize import to_dict
    q = _load_query(args)
    return _json_out(to_dict(q.callers(
        args.node_id, transitive=args.transitive, max_depth=args.max_depth,
    )))


def _run_callees(args: argparse.Namespace) -> int:
    from sphinxcontrib.nexus._serialize import to_dict
    q = _load_query(args)
    return _json_out(to_dict(q.callees(
        args.node_id, transitive=args.transitive, max_depth=args.max_depth,
    )))


def _run_audit(args: argparse.Namespace) -> int:
    from sphinxcontrib.nexus._serialize import to_dict
    q = _load_query(args)
    return _json_out(to_dict(q.verification_audit(
        group_by=args.group_by,
        include_tests=args.include_tests,
    )))


def _run_gaps(args: argparse.Namespace) -> int:
    from sphinxcontrib.nexus._serialize import to_dict
    q = _load_query(args)
    return _json_out(to_dict(q.verification_gaps(
        module=args.module,
        level=args.level,
    )))


if __name__ == "__main__":
    sys.exit(main())
