"""Edit-time file brief — the graph's ambient push channel.

Language servers push diagnostics WITH every edit; the knowledge graph
historically answered only when asked. This module closes that gap:
:func:`file_brief` distills what the graph knows about ONE source file
into a few lines cheap enough to inject on every edit (a PostToolUse
hook, an editor save action), so blast radius, implemented equations,
verification coverage, and owning doc pages arrive in an agent's
context at exactly the moment the file changes.

The latency budget is a hook's, not a query session's: everything here
reads the SQLite database DIRECTLY (:func:`~.export.get_connection`,
read-only) — no NetworkX graph load, no server round-trip. The whole
brief is a handful of indexed SQL aggregations plus at most one git
subprocess for the staleness check.

Content principle (token-budgeted, ≤6 rendered lines): node IDs are
copy-pasteable into the MCP tools (``context``, ``impact``,
``provenance_chain``), lists never show more than three items
(``+N`` counts the rest), and absence of a section means absence of
data — the brief never pads.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from sphinxcontrib.nexus.export import get_connection, read_sqlite_metadata
from sphinxcontrib.nexus.workspace import (
    PROVENANCE_KEY,
    GitProvenance,
    changed_files,
)

#: Most items a rendered list shows before collapsing to ``+N``.
_LIST_BUDGET = 3


@dataclass(frozen=True)
class BriefNode:
    """One in-file node — a copy-pasteable handle into the graph."""

    id: str
    type: str
    name: str
    lineno: int
    degree: int


@dataclass(frozen=True)
class FileBrief:
    """What the graph knows about one source file.

    Field semantics mirror the rendered lines of :func:`render_text`;
    ``None`` / empty values mean the graph has nothing to say, and the
    corresponding line is simply absent.
    """

    file_path: str
    """The queried path, as given."""

    module_id: str | None
    """The file's module node, when the analyzer produced one."""

    nodes: list[BriefNode]
    """All in-file nodes, highest degree first (the hub leads)."""

    external_caller_count: int
    """``calls`` edges arriving from OUTSIDE the file — the blast
    radius an edit here propagates to."""

    equation_labels: list[str]
    """Labels of equations any in-file node ``implements`` — the
    math this file is accountable to."""

    equation_test_count: int
    """``tests`` edges landing on those equations: the verification
    chain runs code → equation → test, not code → test."""

    doc_pages: list[str]
    """Docnames documenting in-file nodes, most-referencing first —
    the pages owed an update when this file's behavior changes."""

    build_commit: str | None
    """Commit the graph's provenance stamp records, when present."""

    changed_since_build: bool | None
    """``True``: the file differs from the build commit (positions
    are suspect). ``False``: verified unchanged. ``None``: unknown —
    no provenance, no git, or no project root to ask from."""


def _in_file_node_ids(
    conn, file_path: Path | str, project_root: Path | None,
) -> list[str]:
    """Node IDs whose stored ``file_path`` names the queried file.

    Stored paths come from the analyzer and may be absolute (Sphinx
    builds) or source-root-relative (bare ``nexus analyze``); the
    query path may be either too. Both sides normalize through
    ``project_root`` — the same resolution contract as
    ``GraphQuery.node_at``.

    Two-tier lookup, because the hook latency budget forbids
    normalizing thousands of stored paths per call: first an exact
    SQL match against the spellings the analyzers actually write
    (values are ``json.dumps(path)``, so string equality IS path
    equality for those spellings); only on a miss, a
    basename-prefiltered scan that normalizes the handful of
    survivors (symlinked roots, mixed separators).
    """

    # Same path-equality contract as GraphQuery.node_at's _norm,
    # realized in SQL-space because this module must never load the
    # graph — keep the two in lockstep; the symlink/spelling corner
    # tests in test_brief.py pin both.
    def _norm(p: Path | str) -> Path:
        p = Path(p)
        if not p.is_absolute() and project_root is not None:
            p = project_root / p
        return p.resolve()

    wanted = _norm(file_path)
    spellings = {json.dumps(str(wanted))}
    if project_root is not None:
        try:
            rel = wanted.relative_to(project_root.resolve())
        except ValueError:
            pass
        else:
            spellings.add(json.dumps(rel.as_posix()))
            spellings.add(json.dumps(str(rel)))
    ordered_spellings = sorted(spellings)
    exact = [
        row["node_id"]
        for row in conn.execute(
            f"SELECT node_id FROM node_attrs WHERE key = 'file_path' "
            f"AND value IN ({_placeholders(ordered_spellings)})",
            ordered_spellings,
        )
    ]
    if exact:
        return exact

    escaped = (
        wanted.name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
    return [
        row["node_id"]
        for row in conn.execute(
            "SELECT node_id, value FROM node_attrs "
            "WHERE key = 'file_path' AND value LIKE ? ESCAPE '\\'",
            (f'%{escaped}"',),
        )
        if _norm(json.loads(row["value"])) == wanted
    ]


def _placeholders(ids: list[str]) -> str:
    return ",".join("?" * len(ids))


def file_brief(
    db_path: Path,
    file_path: Path | str,
    project_root: Path | None = None,
) -> FileBrief | None:
    """The graph's view of one source file, or ``None`` when the file
    is not in the graph at all (new file, excluded tree, stale build).

    Args:
        db_path: SQLite graph database (read directly, never loaded).
        file_path: File of interest; relative paths resolve against
            ``project_root``.
        project_root: Checkout root — anchors path resolution and the
            git staleness check. Without it the brief still renders,
            minus staleness.
    """
    conn = get_connection(db_path)
    try:
        ids = _in_file_node_ids(conn, file_path, project_root)
        if not ids:
            return None
        ph = _placeholders(ids)

        core = {
            row["id"]: (row["type"], row["name"])
            for row in conn.execute(
                f"SELECT id, type, name FROM nodes WHERE id IN ({ph})", ids
            )
        }
        linenos = {
            row["node_id"]: int(json.loads(row["value"]))
            for row in conn.execute(
                f"SELECT node_id, value FROM node_attrs "
                f"WHERE key = 'lineno' AND node_id IN ({ph})",
                ids,
            )
        }

        # The file's whole edge neighborhood in two index-backed
        # fetches (per-type SQL predicates here tempt the planner
        # onto the huge type index — measured 15× slower); every
        # aggregate below is a Python fold over these rows.
        in_file = set(ids)
        incoming = conn.execute(
            f"SELECT source, target, type FROM edges WHERE target IN ({ph})",
            ids,
        ).fetchall()
        outgoing = conn.execute(
            f"SELECT source, target, type FROM edges WHERE source IN ({ph})",
            ids,
        ).fetchall()

        degrees: dict[str, int] = dict.fromkeys(ids, 0)
        for row in incoming:
            degrees[row["target"]] += 1
        for row in outgoing:
            degrees[row["source"]] += 1

        nodes = sorted(
            (
                BriefNode(
                    id=node_id,
                    type=node_type,
                    name=name,
                    lineno=linenos.get(node_id, 0),
                    degree=degrees[node_id],
                )
                for node_id, (node_type, name) in core.items()
            ),
            key=lambda n: (-n.degree, n.id),
        )
        module_id = next((n.id for n in nodes if n.type == "module"), None)

        external_callers = sum(
            1
            for row in incoming
            if row["type"] == "calls" and row["source"] not in in_file
        )

        equation_ids = sorted(
            {row["target"] for row in outgoing if row["type"] == "implements"}
        )
        doc_page_refs = Counter(
            row["source"] for row in incoming if row["type"] == "documents"
        )

        # One name lookup for everything outside the file the brief
        # mentions: equations and doc pages.
        foreign_ids = equation_ids + list(doc_page_refs)
        names = {
            row["id"]: row["name"]
            for row in conn.execute(
                f"SELECT id, name FROM nodes "
                f"WHERE id IN ({_placeholders(foreign_ids)})",
                foreign_ids,
            )
        } if foreign_ids else {}
        equation_labels = sorted(
            names.get(eq_id) or eq_id for eq_id in equation_ids
        )
        doc_pages = [
            names.get(doc_id) or doc_id
            for doc_id, _ in sorted(
                doc_page_refs.items(), key=lambda kv: (-kv[1], kv[0])
            )
        ]

        equation_test_count = 0
        if equation_ids:
            equation_test_count = sum(
                1
                for row in conn.execute(
                    f"SELECT type FROM edges "
                    f"WHERE target IN ({_placeholders(equation_ids)})",
                    equation_ids,
                )
                if row["type"] == "tests"
            )
    finally:
        conn.close()

    prov = GitProvenance.from_stamp(
        read_sqlite_metadata(db_path).get(PROVENANCE_KEY)
    )
    changed_since_build: bool | None = None
    if prov is not None and project_root is not None:
        changed = changed_files(project_root, prov.commit)
        if changed is not None:
            queried = Path(file_path)
            if not queried.is_absolute():
                queried = project_root / queried
            changed_since_build = queried.resolve() in changed

    return FileBrief(
        file_path=str(file_path),
        module_id=module_id,
        nodes=nodes,
        external_caller_count=external_callers,
        equation_labels=equation_labels,
        equation_test_count=equation_test_count,
        doc_pages=doc_pages,
        build_commit=prov.commit if prov is not None else None,
        changed_since_build=changed_since_build,
    )


def _clipped(items: list[str]) -> str:
    """``a, b, c (+4)`` — never more than ``_LIST_BUDGET`` spelled out."""
    shown = ", ".join(items[:_LIST_BUDGET])
    rest = len(items) - _LIST_BUDGET
    return f"{shown} (+{rest})" if rest > 0 else shown


def render_text(brief: FileBrief) -> str:
    """The ≤6-line ambient form — what a hook prints into a transcript.

    Line for line: identity + blast radius; the hub node (the one ID
    most worth feeding to ``impact``/``context``); the math the file
    implements and how tested it is; the doc pages owed an update;
    a staleness flag only when the graph is verifiably behind.
    """
    head = brief.module_id or brief.file_path
    lines = [
        f"nexus: {head} — {len(brief.nodes)} nodes in this file; "
        f"{brief.external_caller_count} external callers"
    ]
    hub = next((n for n in brief.nodes if n.type != "module"), None)
    if hub is not None:
        others = len(brief.nodes) - 2 if brief.module_id else len(brief.nodes) - 1
        more = f"; +{others} more nodes" if others > 0 else ""
        lines.append(f"hub: {hub.id} (degree {hub.degree}){more}")
    if brief.equation_labels:
        lines.append(
            f"implements: {_clipped(brief.equation_labels)} — "
            f"{brief.equation_test_count} tests verify these equations"
        )
    if brief.doc_pages:
        lines.append(f"docs: {_clipped(brief.doc_pages)}")
    if brief.changed_since_build:
        lines.append(
            f"stale: file changed since graph build "
            f"({brief.build_commit}) — node positions may be off; "
            f"rebuild to refresh"
        )
    return "\n".join(lines)
