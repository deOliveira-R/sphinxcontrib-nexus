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
    Workspace,
    WorkspaceLayoutError,
    WorkspaceResolutionError,
    default_branch,
    discover,
    git_provenance,
    list_worktrees,
    resolve_checkout_root,
    stamp_provenance,
)

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
        server_mod, "_workspace", Workspace(db_path=db, root=repo),
    )
    monkeypatch.setattr(server_mod, "_query", GraphQuery(load_sqlite(db)))
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
    q, ws = server_mod._query, server_mod._workspace
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


def test_briefing_workspace_block_flags_branch_mismatch(server_on_main):
    """A graph stamped on another branch than the checkout triggers
    the mismatch warning — the L22-class tripwire."""
    repo = server_on_main
    # Re-stamp the graph as if it had been built on a different branch.
    db = repo / DB_RELPATH
    kg = load_sqlite(db)
    kg.metadata[PROVENANCE_KEY]["git_branch"] = "stale-branch"
    write_sqlite(kg, db)

    block = server_mod._workspace_payload()
    assert any("stale-branch" in w for w in block["warnings"])


def test_briefing_workspace_block_notes_sibling_graphs(server_on_main, worktree):
    _write_graph(worktree, "feature_node")
    block = server_mod._workspace_payload()
    assert any("use_workspace" in w for w in block["warnings"])
    assert block["active"]["branch"] == "main"
    assert len(block["others"]) == 1


def test_briefing_workspace_block_quiet_when_matching(server_on_main):
    """No worktrees, graph built on the current branch: no warnings."""
    block = server_mod._workspace_payload()
    assert "warnings" not in block


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
