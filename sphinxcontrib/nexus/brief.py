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

Content principle: everything the brief names is a HANDLE — node ids
that paste into ``context`` / ``impact`` / ``provenance_chain``, and a
pytest target that runs. Lists show three items and then name the tool
that returns the rest, because this arrives as an injection and there
is no prompt at which to ask. Absence of a section means absence of
data; the brief never pads.

Size, `[M]` 2026-08-17 over all 858 briefable ORPHEUS files (456 of
them holding gates): median **4 lines / 367 chars**, p90 5, max
**7 / 811**. The renderer's own worst case is 8 — a file carrying
equations AND docs AND gates AND catches, all clipped — which ORPHEUS
happens not to contain; those are two different claims and both are
pinned in ``test_brief.py``. This is paid on every edit whether or not
it is read, so that distribution is the budget: a new section has to
earn its line, and ``catches`` did not (it rides on the gates line).
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sphinxcontrib.nexus.export import get_connection, read_sqlite_metadata
from sphinxcontrib.nexus.position import COLLECTABLE_TYPES
from sphinxcontrib.nexus.workspace import (
    PROVENANCE_KEY,
    GitProvenance,
    canonical_path,
    changed_files,
)

#: Most items a rendered list shows before collapsing to ``+N``.
#: Fallback for a render with no checkout to ask; the live value is
#: `[replies].items_per_brief_line`.
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
class GateSummary:
    """What the graph knows about the GATES in one file.

    A test file's brief used to read like a production file's: its
    "hub" was whichever fixture had the highest degree, and the line
    that would say what the file VERIFIES did not exist — so the brief
    was shortest exactly where a verification claim lives. That is not
    a data gap: the graph carries a ``tests`` edge per claim and a
    level, a ``verifies`` list and a ``catches`` list per gate.

    Present iff the file contains at least one gate. There is no
    "kind of file" flag anywhere here, deliberately: a file that
    contains gates gets this section and a file that implements
    equations gets those, and a file doing both reports both. Branching
    on a file KIND would be a conditional standing in for a type, and
    it would be wrong for the file that is both.
    """

    count: int
    """Gates — nodes pytest would collect."""

    helpers: int
    """Nodes in the test tree that pytest collects nothing from:
    fixtures, builders, constants. `[M]` ORPHEUS carries 2225 of them,
    and one of them is what the old brief called the file's "hub"."""

    levels: dict[str, int]
    """``vv_level`` → gate count. The ``""`` key counts gates whose
    SOURCE TEXT carries no level, which is not the same as untagged —
    see :attr:`~.query.TestFacts.vv_level`."""

    equation_ids: list[str]
    """Equations the file's gates claim, most-claimed first."""

    catches: list[str]
    """Catalogued defect ids the file's gates claim to catch."""

    pytest_target: str | None
    """The whole file as one pytest invocation, when derivable."""


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

    equation_ids: list[str]
    """Equations any in-file node ``implements`` — the math this file
    is accountable to.

    Node IDS, not labels. The brief used to render the label it had
    just looked up, which is a handle the reader cannot use: `[M]`
    **0 of 50** pasted into any tool, because every one needed a
    ``math:equation:`` prefix the emitter knew and withheld."""

    equation_test_count: int
    """``tests`` edges landing on those equations: the verification
    chain runs code → equation → test, not code → test."""

    doc_page_ids: list[str]
    """Doc pages documenting in-file nodes, most-referencing first —
    the pages owed an update when this file's behavior changes. Node
    ids for the same reason as :attr:`equation_ids`; a bare docname
    additionally hid which extension the source file carries."""

    gates: GateSummary | None
    """Present iff the file contains tests — see :class:`GateSummary`."""

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
    query path may be either too. Both sides go through
    :func:`~sphinxcontrib.nexus.workspace.canonical_path`, the one
    path-equality contract every asker shares.

    Two-tier lookup, because the hook latency budget forbids
    normalizing thousands of stored paths per call: first an exact
    SQL match against the spellings the analyzers actually write
    (values are ``json.dumps(path)``, so string equality IS path
    equality for those spellings); only on a miss, a
    basename-prefiltered scan that normalizes the handful of
    survivors (symlinked roots, mixed separators).
    """

    # Only the LOOKUP is SQL-space (this module must never load the
    # graph); the path-equality contract itself is shared.
    wanted = canonical_path(file_path, project_root)
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
        if canonical_path(json.loads(row["value"]), project_root) == wanted
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
        # One fetch for every per-node attribute the brief reads.
        # Fetching them together keeps the hook to a fixed number of
        # indexed queries no matter how many facts a section wants.
        attrs: dict[str, dict[str, Any]] = {}
        for row in conn.execute(
            f"SELECT node_id, key, value FROM node_attrs "
            f"WHERE key IN ('lineno', 'is_test', 'in_test_file', "
            f"'vv_level', 'catches') AND node_id IN ({ph})",
            ids,
        ):
            attrs.setdefault(row["node_id"], {})[row["key"]] = json.loads(
                row["value"]
            )
        linenos = {
            node_id: int(a["lineno"])
            for node_id, a in attrs.items()
            if a.get("lineno") is not None
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

        doc_page_ids = [
            doc_id
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

        gates = _gate_summary(
            core, attrs, outgoing, file_path, project_root,
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
        equation_ids=equation_ids,
        equation_test_count=equation_test_count,
        doc_page_ids=doc_page_ids,
        gates=gates,
        build_commit=prov.commit if prov is not None else None,
        changed_since_build=changed_since_build,
    )


def _gate_summary(
    core: dict[str, tuple[str, str]],
    attrs: dict[str, dict[str, Any]],
    outgoing: list,
    file_path: Path | str,
    project_root: Path | None,
) -> GateSummary | None:
    """The gate section, or ``None`` when the file holds no gates.

    A gate is ``is_test`` AND a kind pytest collects. The type check is
    not redundant: the analyzer sets ``is_test`` on module-level data
    and attributes too (`[M]` 1214 on ORPHEUS), and counting a constant
    as a gate would inflate the one number this section exists to
    report. See :data:`~.position.COLLECTABLE_TYPES`.

    Claims come from the ``tests`` EDGE rather than the ``verifies``
    attribute, so what lands in the brief is an equation node id the
    reader can paste. ``catches`` has no edge to read (nexus#63), so it
    is the attribute, and it is a bare defect id by nature.
    """
    gate_ids = {
        node_id
        for node_id, (node_type, _) in core.items()
        if attrs.get(node_id, {}).get("is_test")
        and node_type in COLLECTABLE_TYPES
    }
    if not gate_ids:
        return None

    helpers = sum(
        1
        for node_id in core
        if node_id not in gate_ids
        and attrs.get(node_id, {}).get("in_test_file")
    )
    levels = Counter(
        attrs.get(node_id, {}).get("vv_level") or "" for node_id in gate_ids
    )
    claimed = Counter(
        row["target"]
        for row in outgoing
        if row["type"] == "tests" and row["source"] in gate_ids
    )
    catches = Counter(
        err
        for node_id in gate_ids
        for err in (attrs.get(node_id, {}).get("catches") or ())
    )
    return GateSummary(
        count=len(gate_ids),
        helpers=helpers,
        levels=dict(sorted(levels.items())),
        equation_ids=[eq for eq, _ in claimed.most_common()],
        catches=[err for err, _ in catches.most_common()],
        pytest_target=_relative_target(file_path, project_root),
    )


def _relative_target(
    file_path: Path | str, project_root: Path | None,
) -> str | None:
    """The whole file as one pytest target, or ``None``.

    A file is always a legal invocation (``pytest tests/x.py`` runs
    every gate in it), so this needs none of
    :func:`~.position.pytest_selector`'s name checks — only the same
    root-relative projection, since pytest ids are relative.
    """
    path = Path(file_path)
    if project_root is None:
        return path.as_posix() if not path.is_absolute() else None
    try:
        return canonical_path(path, project_root).relative_to(
            project_root.resolve()
        ).as_posix()
    except ValueError:
        return None


def _clipped(items: list[str], budget: int = _LIST_BUDGET) -> str:
    """``a, b, c (+4)`` — never more than ``budget`` spelled out."""
    shown = ", ".join(items[:budget])
    rest = len(items) - budget
    return f"{shown} (+{rest})" if rest > 0 else shown


def _gate_lines(gates: GateSummary, budget: int) -> list[str]:
    """The test-side lines — what this file VERIFIES, and how to run it.

    The old brief had none of these. It reported a test module by its
    highest-degree node, which in a test file is always a fixture, so
    the one file kind whose whole purpose is a verification claim got
    the shortest and least relevant brief of any.
    """
    levels = " · ".join(
        f"{level or 'no level in source'} {n}"
        for level, n in gates.levels.items()
    )
    # `catches` rides on this line rather than owning one: it is
    # usually one short id, and it belongs with the levels as "what
    # these gates say about themselves". `verifies` earns its own line
    # because its entries are pastable node ids, not tags.
    caught = f"; catches {_clipped(gates.catches, budget)}" if gates.catches else ""
    lines = [
        f"gates: {gates.count} ({levels}); {gates.helpers} helpers{caught}"
    ]
    if gates.equation_ids:
        lines.append(f"verifies: {_clipped(gates.equation_ids, budget)}")
    if gates.pytest_target:
        lines.append(f'run: pytest "{gates.pytest_target}"')
    return lines


def _was_clipped(brief: FileBrief, budget: int) -> bool:
    """Whether any rendered list showed fewer members than it has."""
    lists = [brief.equation_ids, brief.doc_page_ids]
    if brief.gates is not None:
        lists += [brief.gates.equation_ids, brief.gates.catches]
    return any(len(items) > budget for items in lists)


def render_text(brief: FileBrief, project_root: Path | None = None) -> str:
    """The ambient form — what a hook prints into a transcript.

    Line for line: identity + blast radius; the hub node (the one ID
    most worth feeding to ``impact``/``context``); the math the file
    implements and how tested it is; the doc pages owed an update; and,
    when the file holds gates, what they claim and how to run them.

    Every clipped list ends in a follow-up naming the tool that returns
    the members. A bare ``(+45)`` is a fact the reader cannot act on,
    and this arrives as an INJECTION — there is no prompt to ask at.
    """
    budget = _LIST_BUDGET
    if project_root is not None:
        from sphinxcontrib.nexus.project import ProjectConfig

        budget = int(
            ProjectConfig.load(project_root).tunable("replies.items_per_brief_line")
        )

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
    if brief.equation_ids:
        lines.append(
            f"implements: {_clipped(brief.equation_ids, budget)} — "
            f"{brief.equation_test_count} tests verify these equations"
        )
    if brief.doc_page_ids:
        lines.append(f"docs: {_clipped(brief.doc_page_ids, budget)}")
    if brief.gates is not None:
        lines.extend(_gate_lines(brief.gates, budget))
    if _was_clipped(brief, budget):
        lines.append(
            f'full lists: file_brief("{brief.file_path}") — every member, '
            f"unclipped"
        )
    # No per-file staleness line here, deliberately: the ambient form's
    # consumer is the POST-EDIT hook, where "file changed since graph
    # build" is tautologically true (the agent just edited it). Issue
    # #15's usage evaluation measured the line at a 100% fire rate —
    # 842 of 842 injected briefs — i.e. zero information. The
    # ``changed_since_build`` field stays on the dataclass/JSON for
    # consumers that ask at other times.
    return "\n".join(lines)
