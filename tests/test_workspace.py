"""Tests for the workspace model (worktree support).

Covers, bottom-up:

1. ``git_provenance`` — branch / commit / dirty detection, and the
   ``None`` degradation for non-repositories.
2. ``stamp_provenance`` — the build-time stamp, and its round-trip
   through ``write_sqlite`` / ``read_sqlite_metadata`` / ``load_sqlite``.
3. ``Workspace`` — the root-relative database layout invariant
   (``db_relpath``) and its transplant to siblings (``sibling``).
4. ``list_worktrees`` / ``discover`` — enumeration of checkouts via
   git worktrees, per-checkout graph status, active-flagging, and the
   degradations (no git, db outside root).
5. The MCP server's ``use_workspace`` / ``workspaces`` tools and the
   ``session_briefing`` workspace block — the wrong-tree tripwire:
   a session in a worktree must be able to SEE that the active graph
   was built from another tree and SWITCH to its own.

All git interaction runs against throwaway repositories under
``tmp_path``; no test touches the network or the user's git config.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from sphinxcontrib.nexus import server as server_mod
from sphinxcontrib.nexus.export import (
    load_sqlite,
    read_sqlite_metadata,
    write_sqlite,
)
from sphinxcontrib.nexus.graph import GraphNode, KnowledgeGraph, NodeType
from sphinxcontrib.nexus.query import GraphQuery
from sphinxcontrib.nexus.workspace import (
    PROVENANCE_KEY,
    GitProvenance,
    Workspace,
    WorkspaceLayoutError,
    WorkspaceResolutionError,
    canonical_path,
    changed_files,
    checkout_containing,
    default_branch,
    discover,
    git_provenance,
    list_worktrees,
    resolve_checkout_root,
    stamp_provenance,
)

#: Deliberately NOT the shipped convention (``.nexus/graph.db`` since
#: Track 0.6) — it is the pre-0.6 location, kept because ``db_relpath``
#: and ``sibling`` are layout-AGNOSTIC and a constant equal to the
#: convention could not show that. Read it as "some root-relative
#: layout", never as where a graph lives; ``for_root`` below is what
#: pins the real one.
DB_RELPATH = Path("docs/_build/html/_nexus/graph.db")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True, capture_output=True, text=True,
    )


def _make_graph(label: str) -> KnowledgeGraph:
    kg = KnowledgeGraph()
    kg.add_node(GraphNode(
        id=f"py:function:{label}",
        type=NodeType.FUNCTION,
        name=label,
        display_name=label,
        domain="py",
    ))
    return kg


def _write_graph(root: Path, label: str, *, stamp: bool = True) -> Path:
    """Build a one-node graph database at the conventional location
    inside ``root``, stamped from ``root`` unless ``stamp=False``."""
    kg = _make_graph(label)
    if stamp:
        stamp_provenance(kg, root)
    db = root / DB_RELPATH
    write_sqlite(kg, db)
    return db


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A git repository on branch ``main`` with one commit."""
    root = tmp_path / "main"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Test")
    (root / "tracked.txt").write_text("content\n")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "initial")
    return root


@pytest.fixture()
def worktree(repo: Path, tmp_path: Path) -> Path:
    """A linked worktree of ``repo`` on branch ``feature``."""
    wt = tmp_path / "wt-feature"
    _git(repo, "worktree", "add", str(wt), "-b", "feature")
    return wt


# ---------------------------------------------------------------------------
# git_provenance
# ---------------------------------------------------------------------------


def test_git_provenance_clean_repo(repo):
    prov = git_provenance(repo)
    assert prov is not None
    assert prov.branch == "main"
    assert len(prov.commit) >= 7
    assert prov.dirty is False


def test_git_provenance_dirty_repo(repo):
    (repo / "tracked.txt").write_text("modified\n")
    prov = git_provenance(repo)
    assert prov is not None
    assert prov.dirty is True


def test_git_provenance_non_repo(tmp_path):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    assert git_provenance(plain) is None


# ---------------------------------------------------------------------------
# stamp_provenance + round-trip
# ---------------------------------------------------------------------------


def test_stamp_provenance_records_tree_state(repo):
    kg = _make_graph("solver")
    stamp_provenance(kg, repo)
    stamp = kg.metadata[PROVENANCE_KEY]
    assert stamp["source_root"] == str(repo.resolve())
    assert stamp["git_branch"] == "main"
    assert stamp["git_dirty"] is False
    assert "built_at" in stamp


def test_stamp_provenance_non_git_tree(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    kg = _make_graph("solver")
    stamp_provenance(kg, plain)
    stamp = kg.metadata[PROVENANCE_KEY]
    assert stamp["source_root"] == str(plain.resolve())
    assert "git_branch" not in stamp
    assert "built_at" in stamp


def test_stamp_round_trips_through_sqlite(repo):
    db = _write_graph(repo, "solver")
    # Cheap metadata-only read sees the stamp...
    meta = read_sqlite_metadata(db)
    assert meta[PROVENANCE_KEY]["git_branch"] == "main"
    # ...and the full load carries the same stamp.
    kg = load_sqlite(db)
    assert kg.metadata[PROVENANCE_KEY] == meta[PROVENANCE_KEY]


def test_from_stamp_round_trips_git_provenance(repo):
    """Writer and reader share one vocabulary: what ``stamp_provenance``
    records, ``GitProvenance.from_stamp`` reconstructs exactly."""
    kg = _make_graph("solver")
    stamp_provenance(kg, repo)
    live = git_provenance(repo)
    assert GitProvenance.from_stamp(kg.metadata[PROVENANCE_KEY]) == live


def test_from_stamp_absent_or_gitless_is_none(tmp_path):
    assert GitProvenance.from_stamp(None) is None
    # A non-git tree stamps source_root/built_at but no git keys.
    kg = _make_graph("solver")
    stamp_provenance(kg, tmp_path)
    assert GitProvenance.from_stamp(kg.metadata[PROVENANCE_KEY]) is None


# ---------------------------------------------------------------------------
# changed_files — the staleness primitive
# ---------------------------------------------------------------------------


def test_changed_files_empty_when_tree_matches_commit(repo):
    prov = git_provenance(repo)
    assert prov is not None
    assert changed_files(repo, prov.commit) == frozenset()


def test_changed_files_sees_committed_and_uncommitted(repo):
    prov = git_provenance(repo)
    assert prov is not None
    committed = repo / "tracked.txt"
    committed.write_text("changed\n")
    _git(repo, "commit", "-am", "change tracked")
    uncommitted = repo / "loose.txt"
    uncommitted.write_text("new\n")
    _git(repo, "add", "loose.txt")  # staged but not committed
    changed = changed_files(repo, prov.commit)
    assert changed == {committed.resolve(), uncommitted.resolve()}


def test_changed_files_unknown_commit_is_none(repo):
    """``None`` means UNKNOWN — a commit this clone doesn't have must
    not read as 'nothing changed'."""
    assert changed_files(repo, "0000000") is None


def test_changed_files_non_repo_is_none(tmp_path):
    assert changed_files(tmp_path, "0000000") is None


# ---------------------------------------------------------------------------
# Workspace layout
# ---------------------------------------------------------------------------


def test_db_relpath_inside_root(repo):
    ws = Workspace(db_path=repo / DB_RELPATH, root=repo)
    assert ws.db_relpath == DB_RELPATH


def test_db_relpath_outside_root_is_none(repo, tmp_path):
    ws = Workspace(db_path=tmp_path / "elsewhere.db", root=repo)
    assert ws.db_relpath is None


def test_db_relpath_without_root_is_none(tmp_path):
    ws = Workspace(db_path=tmp_path / "graph.db")
    assert ws.db_relpath is None


def test_sibling_transplants_layout(repo, worktree):
    ws = Workspace(db_path=repo / DB_RELPATH, root=repo)
    sib = ws.sibling(worktree)
    assert sib.root == worktree.resolve()
    assert sib.db_path == worktree.resolve() / DB_RELPATH


def test_sibling_requires_relative_layout(repo, worktree, tmp_path):
    ws = Workspace(db_path=tmp_path / "elsewhere.db", root=repo)
    with pytest.raises(WorkspaceLayoutError):
        ws.sibling(worktree)


def test_for_root_derives_the_store_from_the_checkout(tmp_path):
    """A checkout determines its whole workspace.

    The value is written out here rather than imported from
    ``project``: this is the external pin for the convention, so it must
    fail if the store ever moves, and reading it from the code that
    defines it would make it agree by construction.
    """
    ws = Workspace.for_root(tmp_path)
    assert ws.root == tmp_path.resolve()
    assert ws.db_path == (tmp_path / ".nexus" / "graph.db").resolve()
    assert ws.db_relpath == Path(".nexus/graph.db")


# ---------------------------------------------------------------------------
# list_worktrees / discover
# ---------------------------------------------------------------------------


def test_list_worktrees_enumerates_checkouts(repo, worktree):
    entries = list_worktrees(repo)
    by_path = {e.path.resolve(): e.branch for e in entries}
    assert by_path[repo.resolve()] == "main"
    assert by_path[worktree.resolve()] == "feature"


def test_list_worktrees_non_repo(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert list_worktrees(plain) == []


def test_discover_reports_both_checkouts(repo, worktree):
    _write_graph(repo, "main_node")
    _write_graph(worktree, "feature_node")

    statuses = discover(Workspace(db_path=repo / DB_RELPATH, root=repo))
    by_branch = {s.branch: s for s in statuses}

    assert by_branch["main"].is_active
    assert not by_branch["feature"].is_active
    assert by_branch["main"].has_graph
    assert by_branch["feature"].has_graph
    # Each graph's provenance names the tree it was built from.
    main_prov = by_branch["main"].provenance
    feature_prov = by_branch["feature"].provenance
    assert main_prov is not None and main_prov["git_branch"] == "main"
    assert feature_prov is not None and feature_prov["git_branch"] == "feature"


def test_discover_flags_missing_graphs(repo, worktree):
    _write_graph(repo, "main_node")  # no graph in the worktree
    statuses = discover(Workspace(db_path=repo / DB_RELPATH, root=repo))
    by_branch = {s.branch: s for s in statuses}
    assert by_branch["main"].has_graph
    assert not by_branch["feature"].has_graph
    assert by_branch["feature"].provenance is None


def test_discover_degrades_without_git(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    db = plain / DB_RELPATH
    kg = _make_graph("standalone")
    write_sqlite(kg, db)

    statuses = discover(Workspace(db_path=db, root=plain))
    assert len(statuses) == 1
    assert statuses[0].is_active
    assert statuses[0].has_graph


def test_discover_degrades_without_root(tmp_path):
    db = tmp_path / "graph.db"
    write_sqlite(_make_graph("bare"), db)
    statuses = discover(Workspace(db_path=db))
    assert len(statuses) == 1
    assert statuses[0].is_active
    assert statuses[0].workspace.root is None


# ---------------------------------------------------------------------------
# default_branch — the integration target for branch-scope diffs
# ---------------------------------------------------------------------------


def test_default_branch_local_main(repo):
    assert default_branch(repo) == "main"


def test_default_branch_local_master(tmp_path):
    root = tmp_path / "legacy"
    root.mkdir()
    _git(root, "init", "-b", "master")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Test")
    (root / "f.txt").write_text("x\n")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "initial")
    assert default_branch(root) == "master"


def test_default_branch_from_origin_head(repo, tmp_path):
    """A clone resolves via the origin/HEAD symbolic ref — correct even
    for unconventionally named defaults."""
    _git(repo, "branch", "-m", "main", "trunk")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", str(repo), str(clone))
    assert default_branch(clone) == "trunk"


def test_default_branch_non_repo(tmp_path):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    assert default_branch(plain) is None


# ---------------------------------------------------------------------------
# checkout_containing — which checkout does a path live in?
# ---------------------------------------------------------------------------


def test_checkout_containing_prefers_nested_worktree(repo):
    """Claude Code worktrees live UNDER the main root — a path inside
    one is inside both checkouts; the deepest (the worktree) wins."""
    nested = repo / ".claude" / "worktrees" / "nested-wt"
    _git(repo, "worktree", "add", str(nested), "-b", "nested")
    inside = nested / "src"
    inside.mkdir(parents=True)
    active = Workspace(db_path=repo / DB_RELPATH, root=repo)
    assert checkout_containing(active, inside) == nested.resolve()
    assert checkout_containing(active, repo / "docs") == repo.resolve()


def test_checkout_containing_outside_every_checkout(repo, tmp_path):
    active = Workspace(db_path=repo / DB_RELPATH, root=repo)
    assert checkout_containing(active, tmp_path / "elsewhere") is None


# ---------------------------------------------------------------------------
# resolve_checkout_root — name / branch / path forms
# ---------------------------------------------------------------------------


def _active_on(repo: Path) -> Workspace:
    return Workspace(db_path=repo / DB_RELPATH, root=repo)


def test_resolve_absolute_path_passes_through(repo, worktree):
    resolved = resolve_checkout_root(_active_on(repo), str(worktree))
    assert resolved == worktree


def test_resolve_by_worktree_directory_name(repo, worktree):
    resolved = resolve_checkout_root(_active_on(repo), worktree.name)
    assert resolved.resolve() == worktree.resolve()


def test_resolve_by_branch_name(repo, worktree):
    resolved = resolve_checkout_root(_active_on(repo), "feature")
    assert resolved.resolve() == worktree.resolve()


def test_resolve_unknown_name_lists_candidates(repo, worktree):
    with pytest.raises(WorkspaceResolutionError) as exc:
        resolve_checkout_root(_active_on(repo), "no-such-checkout")
    # The error is self-correcting: it names the real candidates.
    assert worktree.name in str(exc.value)


def test_resolve_ambiguous_name_is_an_error(repo, tmp_path):
    # A worktree whose DIRECTORY name equals another worktree's BRANCH
    # name: the reference matches both checkouts.
    wt_a = tmp_path / "wt-a"
    _git(repo, "worktree", "add", str(wt_a), "-b", "shared")
    wt_b = tmp_path / "shared"
    _git(repo, "worktree", "add", str(wt_b), "-b", "other")
    with pytest.raises(WorkspaceResolutionError, match="ambiguous"):
        resolve_checkout_root(_active_on(repo), "shared")


def test_resolve_name_without_root_degrades_to_error(tmp_path):
    active = Workspace(db_path=tmp_path / "graph.db", root=None)
    with pytest.raises(WorkspaceResolutionError):
        resolve_checkout_root(active, "some-name")


# ---------------------------------------------------------------------------
# MCP server tools — the wrong-tree tripwire end to end
# ---------------------------------------------------------------------------


@pytest.fixture()
def server_on_main(repo, monkeypatch):
    """Server state as Claude Code creates it: launched against the
    MAIN checkout's graph."""
    db = _write_graph(repo, "main_node")
    monkeypatch.setattr(
        server_mod, "_query",
        GraphQuery(load_sqlite(db), workspace=Workspace(db_path=db, root=repo)),
    )
    monkeypatch.setattr(server_mod, "_db_mtime", db.stat().st_mtime)
    return repo


def test_workspaces_tool_lists_checkouts(server_on_main, worktree):
    _write_graph(worktree, "feature_node")
    payload = json.loads(server_mod.workspaces())
    by_branch = {w["branch"]: w for w in payload["workspaces"]}
    assert by_branch["main"]["is_active"]
    assert by_branch["feature"]["has_graph"]


def test_use_workspace_switches_the_graph(server_on_main, worktree):
    _write_graph(worktree, "feature_node")

    result = json.loads(server_mod.use_workspace(str(worktree)))
    assert result["switched"] is True

    # Queries now answer from the worktree's graph...
    q = server_mod._query
    assert q is not None
    assert q.get_node("py:function:feature_node") is not None
    assert q.get_node("py:function:main_node") is None
    # ...and the active workspace reports the worktree as active.
    active = result["workspace"]["active"]
    assert active["branch"] == "feature"
    assert active["is_active"]


def test_use_workspace_without_graph_fails_with_hint(server_on_main, worktree):
    result = json.loads(server_mod.use_workspace(str(worktree)))
    assert "error" in result
    assert "hint" in result
    # The graph and workspace are untouched by the failed switch.
    q, ws = server_mod._query, server_mod._active_workspace()
    assert q is not None and ws is not None
    assert q.get_node("py:function:main_node") is not None
    assert ws.root == server_on_main


def test_use_workspace_rejects_non_directory(server_on_main):
    result = json.loads(server_mod.use_workspace("/no/such/place"))
    assert "error" in result


def test_use_workspace_switches_by_worktree_name(server_on_main, worktree):
    """Agents see short names in ``workspaces`` output; the short name
    is enough to switch — no absolute path round-trip."""
    _write_graph(worktree, "feature_node")
    result = json.loads(server_mod.use_workspace(worktree.name))
    assert result["switched"] is True
    assert result["workspace"]["active"]["branch"] == "feature"


def test_use_workspace_switches_by_branch_name(server_on_main, worktree):
    _write_graph(worktree, "feature_node")
    result = json.loads(server_mod.use_workspace("feature"))
    assert result["switched"] is True
    assert result["workspace"]["active"]["branch"] == "feature"


def test_use_workspace_unknown_name_reports_candidates(server_on_main, worktree):
    result = json.loads(server_mod.use_workspace("no-such-checkout"))
    assert "error" in result
    assert worktree.name in result["error"]


def test_reload_tracks_switched_workspace(server_on_main, worktree):
    """After a switch, auto-reload watches the NEW database."""
    _write_graph(worktree, "feature_node")
    server_mod.use_workspace(str(worktree))

    # Rebuild the worktree's graph with different content and bump
    # the mtime past the recorded one.
    import os
    import time
    db = worktree / DB_RELPATH
    kg = _make_graph("feature_rebuilt")
    stamp_provenance(kg, worktree)
    write_sqlite(kg, db)
    now = time.time() + 1
    os.utime(db, (now, now))

    server_mod._reload_if_stale()
    q = server_mod._query
    assert q is not None
    assert q.get_node("py:function:feature_rebuilt") is not None


def _restamp_branch(repo, branch: str) -> None:
    """Re-stamp the active graph as if it had been built elsewhere."""
    db = repo / DB_RELPATH
    kg = load_sqlite(db)
    kg.metadata[PROVENANCE_KEY]["git_branch"] = branch
    write_sqlite(kg, db)


def test_a_merged_branch_name_alone_is_not_a_mismatch(server_on_main):
    """⛔ This used to warn on the branch NAME, which fires on the most
    ordinary workflow there is: fast-forward a branch into `main` and
    delete it, and every session is told to rebuild a graph that
    describes its checkout perfectly.

    [M] on ORPHEUS 2026-08-16 the warning fired while 25 files differed
    from the build commit and NONE of them was indexed — all 25 were
    agent memory and plan notes under `.claude/`. Rebuilding ORPHEUS's
    graph is a multi-minute sphinx-build, charged for nothing."""
    _restamp_branch(server_on_main, "merged-and-deleted")

    # Asserts SILENCE, not merely the absence of that branch name: a
    # false positive phrased any other way is the same false positive.
    # (The fixture has no sibling worktrees, so quiet means empty.)
    assert server_mod._workspace_payload().get("warnings", []) == []


def test_an_indexed_file_that_CHANGED_is_the_mismatch(repo, monkeypatch):
    """The actionable question is not which branch it was built on, but
    whether anything the graph INDEXES has moved since it was built.

    Note this test builds its own graph rather than using
    ``server_on_main``: that fixture's one node carries no
    ``file_path``, so the graph indexes NOTHING and a drift gate
    written on it could never fail."""
    src = repo / "kernel.py"
    src.write_text("def kernel(): pass\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "add kernel")

    kg = _make_graph("kernel")
    kg.nxgraph.nodes["py:function:kernel"]["file_path"] = str(src)
    stamp_provenance(kg, repo)
    kg.metadata[PROVENANCE_KEY]["git_branch"] = "some-other-branch"
    db = repo / DB_RELPATH
    db.parent.mkdir(parents=True, exist_ok=True)
    write_sqlite(kg, db)
    monkeypatch.setattr(
        server_mod, "_query",
        GraphQuery(load_sqlite(db), workspace=Workspace(db_path=db, root=repo)),
    )
    monkeypatch.setattr(server_mod, "_db_mtime", db.stat().st_mtime)

    assert server_mod._indexed_files() == {"kernel.py"}
    src.write_text("def kernel(): return 42\n")          # the drift

    warnings = server_mod._workspace_payload().get("warnings", [])
    assert any(
        "kernel.py" in w and "rebuild" in w.lower() for w in warnings
    ), warnings


def test_an_unresolvable_build_commit_still_warns(server_on_main, monkeypatch):
    """When the build commit cannot be resolved — an unmerged branch
    that was deleted, a re-cloned tree, a different repository — there
    is no diff to take, and "cannot tell" must not read as "nothing
    changed". The branch name survives for exactly this case."""
    _restamp_branch(server_on_main, "vanished-branch")
    monkeypatch.setattr(server_mod, "files_changed_since", lambda *a, **k: None)

    warnings = server_mod._workspace_payload().get("warnings", [])
    assert any("vanished-branch" in w for w in warnings), warnings


def test_briefing_workspace_block_notes_sibling_graphs(server_on_main, worktree):
    """A sibling graph FRESHER than the active one triggers the
    switch hint (this one was written after the active graph)."""
    _write_graph(worktree, "feature_node")
    block = server_mod._workspace_payload()
    assert any("use_workspace" in w for w in block["warnings"])
    assert block["active"]["branch"] == "main"
    assert len(block["others"]) == 1


def test_briefing_stale_sibling_graph_stays_quiet(server_on_main, worktree):
    """A sibling whose graph is OLDER than the active one is not
    worth a warning — existence alone fired 39 warnings against 4
    switches in real sessions (issue #15); freshness is the signal."""
    import os

    repo = server_on_main
    _write_graph(worktree, "feature_node")
    active_mtime = (repo / DB_RELPATH).stat().st_mtime
    stale = active_mtime - 100
    os.utime(worktree / DB_RELPATH, (stale, stale))

    block = server_mod._workspace_payload()
    assert not any(
        "use_workspace(<its root>)" in w
        for w in block.get("warnings", [])
    )
    # The sibling is still LISTED — only the warning is gated.
    assert len(block["others"]) == 1


def test_briefing_workspace_block_quiet_when_matching(server_on_main):
    """No worktrees, graph built on the current branch: no warnings."""
    block = server_mod._workspace_payload()
    assert "warnings" not in block


# ---------------------------------------------------------------------------
# Position staleness at the TOOL BOUNDARY
# ---------------------------------------------------------------------------
#
# A graph is a snapshot: its (file_path, lineno) pairs are true of the
# tree at build time, and an edit above a definition moves it without
# moving the stored line. The check used to live inside `node_at` — 1 of
# 40 tools — so every other tool handed back positions with nothing said.
# It now runs once, in the `@nexus_tool` wrapper.


@pytest.fixture()
def server_with_positions(repo, monkeypatch):
    """A server whose graph carries POSITIONS, in two tracked files.

    ``server_on_main``'s one-node graph has no ``file_path`` at all, so
    it cannot tell a flagged payload from an unflagged one — the pair of
    files is what makes per-file discrimination observable.
    """
    (repo / "other.txt").write_text("untouched\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "second file")

    kg = KnowledgeGraph()
    for label, filename in (("tracked_fn", "tracked.txt"), ("other_fn", "other.txt")):
        kg.add_node(GraphNode(
            id=f"py:function:{label}", type=NodeType.FUNCTION, name=label,
            display_name=label, domain="py",
            metadata={"file_path": str(repo / filename), "lineno": 1},
        ))
    stamp_provenance(kg, repo)
    db = repo / DB_RELPATH
    write_sqlite(kg, db)

    monkeypatch.setattr(
        server_mod, "_query",
        GraphQuery(load_sqlite(db), workspace=Workspace(db_path=db, root=repo)),
    )
    monkeypatch.setattr(server_mod, "_db_mtime", db.stat().st_mtime)
    return repo


def test_a_fresh_graph_payload_is_returned_UNTOUCHED(server_with_positions):
    """Identity, not equality: the ORIGINAL string is handed back, so a
    healthy server's payloads are byte-for-byte what they were before
    staleness was checked at all."""
    payload = server_mod.to_json({"file_path": "tracked.txt", "lineno": 1})
    assert server_mod._mark_stale_positions(payload) is payload
    assert server_mod.STALE_KEY not in server_mod.query("tracked_fn")


def test_a_fresh_graph_payload_is_not_even_PARSED(
    server_with_positions, monkeypatch,
):
    """The cost half of the same claim, which identity cannot show.

    Returning the original string is compatible with having parsed and
    walked it first — so the early return is a promise no output can
    witness. Counting the parse is the only instrument that can: on a
    fresh graph the pass must decide from the cached changed-set alone
    and touch the payload not at all.
    """
    parses = []

    class _CountingJson:
        JSONDecodeError = json.JSONDecodeError

        @staticmethod
        def loads(s, *a, **kw):
            parses.append(s)
            return json.loads(s, *a, **kw)

    monkeypatch.setattr(server_mod, "json", _CountingJson)
    server_mod.query("tracked_fn")
    assert parses == []


def test_a_DIRTY_tree_leaves_an_unaffected_payload_untouched(
    server_with_positions,
):
    """Identity again, in the case that actually reaches the walk.

    The test above returns early and so cannot see a pass that
    re-serialises what it did not change; this one walks the payload,
    marks nothing, and must still hand back the same object. Equality
    would not catch it — ``to_json(json.loads(x))`` round-trips to an
    equal string — which is why the assertion is ``is``.
    """
    (server_with_positions / "tracked.txt").write_text("edited\n")
    payload = server_mod.to_json({"file_path": "other.txt", "lineno": 1})
    assert server_mod._mark_stale_positions(payload) is payload


def test_a_stale_position_is_flagged_where_it_sits(server_with_positions):
    (server_with_positions / "tracked.txt").write_text("edited\n")
    payload = json.loads(server_mod.node_at("tracked.txt", 1))

    note = payload[server_mod.STALE_KEY]
    prov = git_provenance(server_with_positions)  # stamp == HEAD here
    assert prov is not None
    assert prov.commit in note and "rebuild" in note


def test_a_tool_OTHER_than_node_at_is_flagged_too(server_with_positions):
    """The whole point of moving the check to the boundary.

    ``query`` returns a bare JSON ARRAY of nodes — a shape the retired
    per-tool warning could not have annotated even if it had been called
    there, since there is no object to hang a summary key on. The flag
    goes on the entry itself.
    """
    (server_with_positions / "tracked.txt").write_text("edited\n")
    entries = json.loads(server_mod.query("tracked_fn"))

    assert isinstance(entries, list) and entries
    assert all(server_mod.STALE_KEY in e for e in entries)


def test_an_UNCHANGED_file_is_not_flagged_by_a_dirty_neighbour(
    server_with_positions,
):
    """The control: a dirty tree must not blanket-flag every position.

    Without this, "flag everything whenever anything changed" passes the
    test above and is useless — the agent learns nothing about WHICH
    position it should distrust.
    """
    (server_with_positions / "tracked.txt").write_text("edited\n")
    entries = json.loads(server_mod.query("other_fn"))

    assert entries, "the control needs a node to be unflagged"
    assert all(server_mod.STALE_KEY not in e for e in entries)


def test_the_no_match_answer_is_flagged_as_well(server_with_positions):
    """A stale graph is exactly when "no node encloses this" is least
    trustworthy — so the error payload names the file it looked for and
    is annotated by the same pass as a match."""
    (server_with_positions / "tracked.txt").write_text("edited\n")
    payload = json.loads(server_mod.node_at("tracked.txt", 9999))

    assert "error" in payload
    assert server_mod.STALE_KEY in payload


def test_changed_set_is_cached_per_query(server_on_main, monkeypatch):
    """One git subprocess per loaded graph — not per call.

    The cache needs no invalidation key because the query object IS
    the key: a reload or a workspace switch builds a new one. This
    replaced a module-global cache whose hand-built
    ``(root, db_mtime, commit)`` key existed only to notice that.
    """
    from sphinxcontrib.nexus import query as query_mod

    calls = []
    real = query_mod.changed_files
    monkeypatch.setattr(
        query_mod, "changed_files",
        lambda *a: calls.append(a) or real(*a),
    )
    q = server_mod._query
    assert q is not None
    assert q.files_changed_since_build == q.files_changed_since_build
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Roots-based auto-alignment — the session tells us where it lives
# ---------------------------------------------------------------------------


class _RootsClient:
    """Stub of the Context surface ``_auto_align_workspace`` touches:
    ``ctx.session.list_roots()`` returning ``.roots[*].uri``."""

    def __init__(self, uris: list[str] | None = None):
        self._uris = uris

    @property
    def session(self):
        return self

    async def list_roots(self):
        if self._uris is None:
            raise RuntimeError("client does not support roots")
        from types import SimpleNamespace
        return SimpleNamespace(
            roots=[SimpleNamespace(uri=u) for u in self._uris]
        )


def _auto_align(ctx) -> dict | None:
    import asyncio
    return asyncio.run(server_mod._auto_align_workspace(ctx))


def test_auto_align_switches_to_session_worktree(server_on_main, worktree):
    """A session launched inside a worktree (Claude Code reports its
    launch dir via roots) gets that worktree's graph without any
    manual use_workspace call."""
    _write_graph(worktree, "feature_node")
    info = _auto_align(_RootsClient([worktree.as_uri()]))
    assert info is not None and info["switched"] is True
    ws = server_mod._active_workspace()
    assert ws is not None and ws.root == worktree.resolve()


def test_auto_align_reports_missing_graph_without_switching(
    server_on_main, worktree,
):
    info = _auto_align(_RootsClient([worktree.as_uri()]))
    assert info is not None and info["switched"] is False
    assert "hint" in info
    ws = server_mod._active_workspace()
    assert ws is not None and ws.root == server_on_main  # untouched


def test_auto_align_quiet_when_session_in_active_checkout(server_on_main):
    subdir = server_on_main / "docs"
    subdir.mkdir(exist_ok=True)
    assert _auto_align(_RootsClient([subdir.as_uri()])) is None


def test_auto_align_quiet_without_roots_support(server_on_main):
    assert _auto_align(_RootsClient(None)) is None


def test_auto_align_quiet_for_foreign_paths(server_on_main, tmp_path):
    elsewhere = tmp_path / "unrelated"
    elsewhere.mkdir()
    assert _auto_align(_RootsClient([elsewhere.as_uri()])) is None


def test_session_briefing_reports_auto_align(server_on_main, worktree):
    import asyncio
    _write_graph(worktree, "feature_node")
    payload = json.loads(
        asyncio.run(server_mod.session_briefing(_RootsClient([worktree.as_uri()])))
    )
    align = payload["workspace"]["auto_align"]
    assert align["switched"] is True
    # The briefing itself already answers from the switched-to tree.
    assert payload["workspace"]["active"]["branch"] == "feature"


# ---------------------------------------------------------------------------
# AST analysis must not ingest nested git working trees
# ---------------------------------------------------------------------------


def _module_names(kg: KnowledgeGraph) -> set[str]:
    g = kg.nxgraph
    return {
        g.nodes[n]["name"]
        for n in g.nodes
        if g.nodes[n].get("type") == "module"
    }


def test_analyze_skips_nested_worktrees_and_clones(tmp_path):
    """A checkout nested inside the analyzed tree (Claude Code worktree
    = gitlink file; vendored clone = .git directory) is a FOREIGN tree:
    its files must contribute nothing to this project's graph.
    Observed on ORPHEUS: 51% of all nodes were worktree copies."""
    from sphinxcontrib.nexus.ast_analyzer import analyze_directory

    root = tmp_path / "project"
    root.mkdir()
    (root / ".git").mkdir()  # the analyzed tree IS a repo root — exempt
    (root / "real_module.py").write_text("def real():\n    pass\n")

    worktree = root / ".claude" / "worktrees" / "session-a"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: /elsewhere\n")  # gitlink FILE
    (worktree / "worktree_copy.py").write_text("def copied():\n    pass\n")

    clone = root / "vendor" / "somelib"
    clone.mkdir(parents=True)
    (clone / ".git").mkdir()  # nested clone: .git DIRECTORY
    (clone / "vendored.py").write_text("def vendored():\n    pass\n")

    kg = analyze_directory(source_dir=root, project_root=root)

    modules = _module_names(kg)
    assert "real_module" in modules
    assert not any("worktree_copy" in m for m in modules), modules
    assert not any("vendored" in m for m in modules), modules


# ── the path-equality contract ──────────────────────────────────────


class TestCanonicalPath:
    """The one law: two spellings name the same file exactly when
    ``canonical_path`` maps them to the same value.

    This is the pin the 2026-08-16 single-sourcing owed. Three private
    realizations (``node_at._norm``, ``_in_file_node_ids._norm``,
    ``NodeBinder._abs``) collapsed into one function, which demoted
    every gate that had been comparing the copies to each other — so
    the contract needs a gate that asserts its LAWS against
    hand-written expectations instead of against a second
    implementation.
    """

    def test_a_relative_spelling_resolves_against_the_root(self, tmp_path):
        root = tmp_path.resolve()
        assert canonical_path("pkg/mod.py", root) == root / "pkg" / "mod.py"

    def test_an_absolute_spelling_ignores_the_root(self, tmp_path):
        elsewhere = (tmp_path / "elsewhere").resolve()
        elsewhere.mkdir()
        target = elsewhere / "mod.py"
        assert canonical_path(target, tmp_path / "project") == target

    def test_the_two_spellings_of_one_file_agree(self, tmp_path):
        """The contract's whole purpose, stated directly."""
        root = tmp_path.resolve()
        (root / "pkg").mkdir()
        (root / "pkg" / "mod.py").write_text("x = 1\n")
        assert (
            canonical_path("pkg/mod.py", root)
            == canonical_path(root / "pkg" / "mod.py", root)
        )

    def test_a_symlinked_root_collapses_to_the_real_tree(self, tmp_path):
        """Why ``.resolve()`` is part of the contract and not an
        incidental tidy-up: a checkout reached through an alias must
        compare equal to the same file reached directly, or every
        stored position misses."""
        real = (tmp_path / "real").resolve()
        real.mkdir()
        (real / "mod.py").write_text("x = 1\n")
        alias = tmp_path / "alias"
        alias.symlink_to(real, target_is_directory=True)
        assert canonical_path("mod.py", alias) == real / "mod.py"

    def test_dot_segments_collapse(self, tmp_path):
        root = tmp_path.resolve()
        (root / "pkg").mkdir()
        assert canonical_path("pkg/../mod.py", root) == root / "mod.py"

    def test_it_is_idempotent(self, tmp_path):
        """A key that is re-keyed must not move — the index stores
        canonical paths and looks them up with canonical paths."""
        root = tmp_path.resolve()
        once = canonical_path("pkg/mod.py", root)
        assert canonical_path(once, root) == once

    def test_a_rootless_workspace_falls_back_to_the_cwd(self, tmp_path):
        """A server launched with a bare ``--db`` has a graph but no
        tree; a relative spelling can then only mean the process's own
        directory. Absolute spellings are unaffected, which is why a
        rootless workspace still works on a real (absolute) graph."""
        assert canonical_path("mod.py", None) == Path.cwd().resolve() / "mod.py"
        assert canonical_path(tmp_path / "mod.py", None) == (
            tmp_path / "mod.py"
        ).resolve()

    def test_the_workspace_method_is_the_bound_form(self, tmp_path):
        root = tmp_path.resolve()
        ws = Workspace(db_path=root / ".nexus" / "graph.db", root=root)
        assert ws.canonical_path("pkg/mod.py") == canonical_path(
            "pkg/mod.py", root
        )
