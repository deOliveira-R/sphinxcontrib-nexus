"""Workspace model — which checkout's graph is being read?

A *workspace* is one checkout of a project — the main working tree or
any linked ``git worktree`` — paired with the knowledge-graph database
that a build inside that checkout produces.  The pairing matters
because a graph database is a snapshot of ONE tree: an agent session
working inside a worktree on a feature branch gets plausible-but-wrong
answers if its server reads the main checkout's graph.  This module
gives that failure mode a name and a remedy:

* :func:`stamp_provenance` — at graph-write time, record WHICH tree
  (root, branch, commit, dirty) the graph was built from, so every
  database is self-describing.
* :class:`Workspace` / :func:`discover` — at serve time, enumerate the
  sibling checkouts via ``git worktree list`` and report which of them
  have built graphs, on which branch, stamped from where.
* The MCP server's ``use_workspace`` tool (in :mod:`.server`) swaps
  the active workspace atomically; ``workspaces`` lists candidates.

Git access is subprocess-based and failure-tolerant: a missing ``git``
binary or a non-repository root degrades to "no provenance / only the
active workspace", never to an exception at tool-call time.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePath
from typing import TYPE_CHECKING, Any, Mapping

from sphinxcontrib.nexus.export import read_sqlite_metadata

if TYPE_CHECKING:
    from sphinxcontrib.nexus.graph import KnowledgeGraph

logger = logging.getLogger(__name__)

#: Key under which the build-time stamp lives in ``graph.metadata``
#: (and therefore in the SQLite ``metadata`` table).
PROVENANCE_KEY = "provenance"

#: Keys inside the stamp dict. Single vocabulary shared by the writer
#: (:func:`stamp_provenance`) and every reader
#: (:meth:`GitProvenance.from_stamp`, workspace payloads, file briefs)
#: so the stamp's shape cannot drift between sites.
STAMP_SOURCE_ROOT = "source_root"
STAMP_BUILT_AT = "built_at"
STAMP_GIT_BRANCH = "git_branch"
STAMP_GIT_COMMIT = "git_commit"
STAMP_GIT_DIRTY = "git_dirty"

_GIT_TIMEOUT_S = 10


class WorkspaceLayoutError(ValueError):
    """The workspace layout cannot support the requested operation
    (e.g. mapping a sibling checkout when the database does not live
    inside the project root)."""


class WorkspaceResolutionError(ValueError):
    """A checkout reference (name / branch / path) did not resolve to
    exactly one checkout of the project."""


def _git(root: Path, *args: str) -> str | None:
    """Run a git command at ``root``; ``None`` on any failure."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, timeout=_GIT_TIMEOUT_S,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


# ------------------------------------------------------------------
# Provenance — stamped at the producer (every graph-write site)
# ------------------------------------------------------------------


@dataclass(frozen=True)
class GitProvenance:
    """State of a checkout at a point in time."""

    branch: str | None
    """Checked-out branch; ``None`` when HEAD is detached."""

    commit: str
    """Short commit hash of HEAD."""

    dirty: bool
    """Uncommitted changes present (staged or unstaged)."""

    @classmethod
    def from_stamp(cls, stamp: Mapping[str, Any] | None) -> GitProvenance | None:
        """Read back what :func:`stamp_provenance` wrote.

        ``None`` when there is no stamp at all or the stamp carries no
        git keys (graph built from a non-git tree) — the same absence
        contract as :func:`git_provenance`.
        """
        if stamp is None:
            return None
        commit = stamp.get(STAMP_GIT_COMMIT)
        if not commit:
            return None
        return cls(
            branch=stamp.get(STAMP_GIT_BRANCH) or None,
            commit=str(commit),
            dirty=bool(stamp.get(STAMP_GIT_DIRTY)),
        )


def git_provenance(root: Path) -> GitProvenance | None:
    """Current git state of the checkout at ``root``.

    ``None`` when ``root`` is not inside a git repository, git is not
    installed, or the repository has no commits yet.
    """
    commit = _git(root, "rev-parse", "--short", "HEAD")
    if commit is None:
        return None
    branch_out = _git(root, "branch", "--show-current")
    status_out = _git(root, "status", "--porcelain")
    return GitProvenance(
        branch=(branch_out or "").strip() or None,
        commit=commit.strip(),
        dirty=bool((status_out or "").strip()),
    )


def default_branch(root: Path) -> str | None:
    """The repository's default branch (integration target).

    Resolution order: the ``origin/HEAD`` symbolic ref (set on clone,
    correct even for unconventionally named defaults), then the first
    of ``main`` / ``master`` that exists locally.  ``None`` when
    nothing resolves — no git, no remote, unusual naming.
    """
    out = _git(root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if out and out.strip():
        return out.strip().removeprefix("origin/")
    for name in ("main", "master"):
        if _git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"):
            return name
    return None


def stamp_provenance(graph: KnowledgeGraph, source_root: Path) -> None:
    """Record which tree this graph was built from.

    Written into ``graph.metadata[PROVENANCE_KEY]`` so it lands in the
    database's ``metadata`` table on the next write and is readable by
    :func:`discover` / the MCP server without loading the graph.
    Stamped at every graph-producing site (the Sphinx
    ``build-finished`` handler, ``nexus analyze``) because the write
    site is the one place that knows the source tree — normalise at
    the producer, not at each consumer.

    Non-git source trees still get ``source_root`` and ``built_at``;
    the ``git_*`` keys are simply absent.
    """
    root = source_root.resolve()
    stamp: dict[str, Any] = {
        STAMP_SOURCE_ROOT: str(root),
        STAMP_BUILT_AT: datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    prov = git_provenance(root)
    if prov is not None:
        stamp[STAMP_GIT_BRANCH] = prov.branch
        stamp[STAMP_GIT_COMMIT] = prov.commit
        stamp[STAMP_GIT_DIRTY] = prov.dirty
    graph.metadata[PROVENANCE_KEY] = stamp


def changed_files(root: Path, since_commit: str) -> frozenset[Path] | None:
    """Absolute paths of tracked files that differ between
    ``since_commit`` and the CURRENT working tree.

    ``git diff --name-only <commit>`` (no ``..HEAD``) diffs the commit
    against the working tree, so both committed-since and uncommitted
    edits are covered in one subprocess.  Untracked files never appear
    — but an untracked file is not in any built graph either, so the
    staleness question does not arise for it.

    ``None`` means UNKNOWN (git missing, not a repository, commit not
    found) — distinct from ``frozenset()`` which means "verified
    unchanged".  Callers must not collapse the two: a graph built at a
    commit this clone no longer has is *more* suspect, not less.

    If the graph was built from a dirty tree (stamp ``git_dirty``),
    files dirty at build time may appear here even though the graph
    already reflects them — the warning consumers phrase around "may".
    """
    out = _git(root, "diff", "--name-only", since_commit)
    if out is None:
        return None
    return frozenset(
        (root / line.strip()).resolve()
        for line in out.splitlines()
        if line.strip()
    )


# ------------------------------------------------------------------
# Workspaces — checkout ↔ database pairing and discovery
# ------------------------------------------------------------------


@dataclass(frozen=True)
class WorktreeEntry:
    """One line of ``git worktree list``: a checkout and its branch."""

    path: Path
    branch: str | None


def list_worktrees(root: Path) -> list[WorktreeEntry]:
    """All checkouts of the repository containing ``root``.

    The main working tree comes first (git's own ordering).  Returns
    ``[]`` when ``root`` is not a git repository or git is missing.
    """
    out = _git(root, "worktree", "list", "--porcelain")
    if out is None:
        return []
    entries: list[WorktreeEntry] = []
    path: Path | None = None
    branch: str | None = None
    for line in [*out.splitlines(), ""]:
        if line.startswith("worktree "):
            path = Path(line.removeprefix("worktree ").strip())
        elif line.startswith("branch "):
            branch = (
                line.removeprefix("branch ").strip().removeprefix("refs/heads/")
            )
        elif not line.strip():
            if path is not None:
                entries.append(WorktreeEntry(path=path, branch=branch))
            path, branch = None, None
    return entries


def checkout_containing(active: Workspace, path: Path) -> Path | None:
    """Root of the project checkout that contains ``path``.

    Picks the DEEPEST match among the ``git worktree list`` entries:
    Claude Code session worktrees live under
    ``<main root>/.claude/worktrees/<name>``, so a path inside one is
    inside the main checkout too — the nested worktree is the checkout
    the session actually works in.  ``None`` when ``path`` lies
    outside every checkout, or when there is no known root / git.
    """
    if active.root is None:
        return None
    path = path.resolve()
    containing = [
        root for root in (e.path.resolve() for e in list_worktrees(active.root))
        if root == path or root in path.parents
    ]
    return max(containing, key=lambda root: len(root.parts), default=None)


def resolve_checkout_root(active: Workspace, ref: str) -> Path:
    """Resolve a checkout reference to a checkout root path.

    Accepted forms, tried in order:

    1. **Absolute path** — used as-is (existence is the caller's check,
       so the caller can produce its own context-rich error).
    2. **Worktree directory name or branch name** — matched against
       ``git worktree list`` at the active root.  Agents see short
       names like ``sn-nd-layout`` in ``workspaces`` output; making
       them type the absolute root back is pure friction.
    3. **Relative path** — only when it exists as a directory, as a
       last resort (kept for symmetry with absolute paths; ambiguity
       with form 2 is resolved in favour of the worktree match, which
       is what the short name almost always means).

    Raises :class:`WorkspaceResolutionError` when a non-path reference
    matches zero or several checkouts; the message lists the known
    checkouts so the caller can self-correct without a second
    discovery round-trip.
    """
    candidate = Path(ref).expanduser()
    if candidate.is_absolute():
        return candidate

    entries = list_worktrees(active.root) if active.root is not None else []
    matches = [e for e in entries if ref in (e.path.name, e.branch)]
    if len(matches) == 1:
        return matches[0].path
    if len(matches) > 1:
        listing = ", ".join(str(e.path) for e in matches)
        raise WorkspaceResolutionError(
            f"{ref!r} is ambiguous — it matches several checkouts: "
            f"{listing}. Pass the absolute root path instead."
        )
    if candidate.is_dir():
        return candidate
    known = ", ".join(
        f"{e.path.name} ({e.branch or 'detached'})" for e in entries
    ) or "none discovered"
    raise WorkspaceResolutionError(
        f"No checkout named {ref!r}. Known checkouts: {known}. "
        f"Pass a worktree directory name, a branch name, or an "
        f"absolute root path."
    )


@dataclass(frozen=True)
class Workspace:
    """One checkout paired with its graph database.

    ``root`` is optional because ``nexus serve`` can be launched with
    a bare ``--db`` and no project root; such a server still has a
    graph to read — it just cannot do git-aware work and cannot map
    sibling worktrees.
    """

    db_path: Path
    root: Path | None = None

    @property
    def db_relpath(self) -> PurePath | None:
        """Database location relative to the checkout root — the
        layout invariant shared by every sibling worktree.  ``None``
        when no root is known or the database lives outside it."""
        if self.root is None:
            return None
        try:
            return self.db_path.relative_to(self.root)
        except ValueError:
            return None

    def sibling(self, root: Path) -> Workspace:
        """The same project layout transplanted to another checkout."""
        rel = self.db_relpath
        if rel is None:
            raise WorkspaceLayoutError(
                f"Cannot map a sibling checkout: the database "
                f"{self.db_path} does not live inside the project root "
                f"{self.root}, so there is no root-relative layout to "
                f"transplant."
            )
        root = root.resolve()
        return Workspace(db_path=root / rel, root=root)


@dataclass(frozen=True)
class WorkspaceStatus:
    """A workspace as seen by discovery: does it have a graph, how
    fresh is it, and what tree was it built from."""

    workspace: Workspace
    branch: str | None
    """Branch currently checked out at the workspace root."""

    is_active: bool
    """Is the server answering queries from THIS workspace's graph?"""

    graph_mtime: float | None
    """Database file mtime (epoch seconds); ``None`` = no graph built."""

    provenance: dict[str, Any] | None
    """The :func:`stamp_provenance` record, when the graph carries one."""

    @property
    def has_graph(self) -> bool:
        return self.graph_mtime is not None

    def to_payload(self) -> dict[str, Any]:
        """JSON-safe view for MCP / CLI output."""
        return {
            "root": str(self.workspace.root) if self.workspace.root else None,
            "db": str(self.workspace.db_path),
            "branch": self.branch,
            "is_active": self.is_active,
            "has_graph": self.has_graph,
            "graph_built": (
                datetime.fromtimestamp(self.graph_mtime, tz=timezone.utc)
                .isoformat(timespec="seconds")
                if self.graph_mtime is not None
                else None
            ),
            "provenance": self.provenance,
        }


def _status(ws: Workspace, branch: str | None, is_active: bool) -> WorkspaceStatus:
    """Inspect one workspace's database on disk."""
    mtime: float | None = None
    provenance: dict[str, Any] | None = None
    if ws.db_path.is_file():
        mtime = ws.db_path.stat().st_mtime
        try:
            meta = read_sqlite_metadata(ws.db_path)
        except Exception as e:  # corrupt / locked db — report presence only
            logger.debug("Could not read metadata of %s: %s", ws.db_path, e)
        else:
            provenance = meta.get(PROVENANCE_KEY)
    return WorkspaceStatus(
        workspace=ws,
        branch=branch,
        is_active=is_active,
        graph_mtime=mtime,
        provenance=provenance,
    )


def discover(active: Workspace) -> list[WorkspaceStatus]:
    """Every checkout of the project, the active workspace included.

    Enumerates checkouts via ``git worktree list`` at the active root
    and maps each sibling through the active workspace's root-relative
    database layout.  Degrades to "just the active workspace" when
    there is no known root, no git repository, or no root-relative
    layout to transplant.
    """

    def active_only() -> list[WorkspaceStatus]:
        prov = git_provenance(active.root) if active.root else None
        return [
            _status(active, branch=prov.branch if prov else None, is_active=True)
        ]

    if active.root is None or active.db_relpath is None:
        return active_only()
    entries = list_worktrees(active.root)
    if not entries:
        return active_only()

    active_root = active.root.resolve()
    statuses: list[WorkspaceStatus] = []
    for entry in entries:
        is_active = entry.path.resolve() == active_root
        ws = active if is_active else active.sibling(entry.path)
        statuses.append(_status(ws, branch=entry.branch, is_active=is_active))
    if not any(s.is_active for s in statuses):
        # The active root is not itself a worktree root (e.g. the
        # server was pointed at a subdirectory) — keep it visible.
        statuses = active_only() + statuses
    return statuses
