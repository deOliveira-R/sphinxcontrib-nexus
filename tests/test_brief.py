"""Tests for the edit-time file brief (the ambient push channel).

Two fixture families, deliberately different in origin:

1. ``analyzed_db`` — a real ``analyze_directory`` pass over two
   modules, exported to SQLite. Positions, degrees, and the
   cross-module call are the analyzer's own, so the brief is tested
   against what production graphs actually contain.
2. ``rich_db`` — a hand-built graph with equations, tests, and doc
   pages wired to one file, because the AST analyzer alone cannot
   produce doc-domain structure. This is where the
   implements/tested-by/docs lines are pinned.

Staleness runs against a throwaway git repository: the brief's
``changed_since_build`` must distinguish verified-unchanged (False)
from changed (True) from unknowable (None).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from sphinxcontrib.nexus.ast_analyzer import analyze_directory
from sphinxcontrib.nexus.brief import (
    BriefNode,
    FileBrief,
    file_brief,
    render_text,
)
from sphinxcontrib.nexus.export import write_sqlite
from sphinxcontrib.nexus.graph import (
    EdgeType,
    GraphEdge,
    GraphNode,
    KnowledgeGraph,
    NodeType,
)
from sphinxcontrib.nexus.workspace import stamp_provenance


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True, capture_output=True, text=True,
    )


# ---------------------------------------------------------------------------
# Against a real analyzer pass
# ---------------------------------------------------------------------------


@pytest.fixture()
def analyzed_db(tmp_path: Path) -> tuple[Path, Path, Path]:
    """(db, project_root, target_file): two modules, ``user`` calling
    into ``lib`` so the brief's external-caller count has something
    real to count."""
    src = tmp_path / "pkg"
    src.mkdir()
    lib = src / "lib.py"
    lib.write_text(
        "def hub(x):\n"
        "    return helper(x) + helper(x)\n"
        "\n"
        "def helper(x):\n"
        "    return x + 1\n"
    )
    (src / "user.py").write_text(
        "from pkg.lib import hub\n"
        "\n"
        "def consume():\n"
        "    return hub(1)\n"
    )
    kg = analyze_directory(src, project_root=tmp_path)
    db = tmp_path / "graph.db"
    write_sqlite(kg, db)
    return db, tmp_path, lib


def test_brief_collects_in_file_nodes(analyzed_db):
    db, root, lib = analyzed_db
    brief = file_brief(db, lib, project_root=root)
    assert brief is not None
    names = {n.name for n in brief.nodes}
    assert {"hub", "helper"} <= {n.split(".")[-1] for n in names}
    assert brief.module_id is not None and "lib" in brief.module_id


def test_brief_orders_nodes_by_degree(analyzed_db):
    db, root, lib = analyzed_db
    brief = file_brief(db, lib, project_root=root)
    assert brief is not None
    degrees = [n.degree for n in brief.nodes]
    assert degrees == sorted(degrees, reverse=True)


def test_brief_counts_external_callers_only(analyzed_db):
    """``hub → helper`` is in-file and must NOT count; only
    ``user.consume → hub`` arrives from outside."""
    db, root, lib = analyzed_db
    brief = file_brief(db, lib, project_root=root)
    assert brief is not None
    assert brief.external_caller_count == 1


def test_brief_relative_and_absolute_queries_agree(analyzed_db):
    """Identical content either way; ``file_path`` alone echoes the
    query spelling (by design — the caller recognizes its own path)."""
    from dataclasses import replace

    db, root, lib = analyzed_db
    absolute = file_brief(db, lib, project_root=root)
    relative = file_brief(db, lib.relative_to(root), project_root=root)
    assert absolute is not None and relative is not None
    assert replace(absolute, file_path="") == replace(relative, file_path="")


def test_brief_unknown_file_is_none(analyzed_db):
    db, root, _ = analyzed_db
    assert file_brief(db, root / "elsewhere.py", project_root=root) is None


def test_brief_positions_come_from_the_analyzer(analyzed_db):
    db, root, lib = analyzed_db
    brief = file_brief(db, lib, project_root=root)
    assert brief is not None
    by_name = {n.name.split(".")[-1]: n for n in brief.nodes}
    assert by_name["hub"].lineno == 1
    assert by_name["helper"].lineno == 4


# ---------------------------------------------------------------------------
# Path-matching corners — the two _norm realizations stay in lockstep
# ---------------------------------------------------------------------------


def test_symlinked_root_resolves_in_both_norm_realizations(analyzed_db):
    """The path-equality contract lives twice — graph-space
    (GraphQuery.node_at) and SQL-space (brief._in_file_node_ids).
    A query through a symlinked alias of the root must land on the
    same nodes through BOTH, or the realizations have drifted."""
    from sphinxcontrib.nexus.export import load_sqlite
    from sphinxcontrib.nexus.query import GraphQuery

    db, root, lib = analyzed_db
    alias = root.parent / "alias"
    alias.symlink_to(root, target_is_directory=True)
    aliased_lib = alias / lib.relative_to(root)

    brief = file_brief(db, aliased_lib, project_root=alias)
    assert brief is not None and len(brief.nodes) >= 2

    node = GraphQuery(load_sqlite(db)).node_at(aliased_lib, 1, project_root=alias)
    assert node is not None
    assert node.id in {n.id for n in brief.nodes}


def test_fallback_tier_matches_unfamiliar_spelling(tmp_path):
    """A stored spelling the exact tier cannot anticipate (here a
    ``./``-prefixed relative path) must still resolve via the
    basename-prefiltered scan."""
    (tmp_path / "pkg").mkdir()
    kg = KnowledgeGraph()
    kg.add_node(GraphNode(
        id="py:function:pkg.mod.f", type=NodeType.FUNCTION,
        name="pkg.mod.f", domain="py",
        metadata={"file_path": "./pkg/mod.py", "lineno": 1},
    ))
    db = tmp_path / "graph.db"
    write_sqlite(kg, db)
    brief = file_brief(db, "pkg/mod.py", project_root=tmp_path)
    assert brief is not None
    assert [n.id for n in brief.nodes] == ["py:function:pkg.mod.f"]


def test_fallback_tier_escapes_like_wildcards(tmp_path):
    """``_`` in a basename is a LIKE wildcard; unescaped, querying
    ``my_mod.py`` would prefilter-match ``myxmod.py`` and (worse)
    an unescaped stored ``%``-pattern could over-match. The query
    for one must never return the other."""
    kg = KnowledgeGraph()
    for stem in ("my_mod", "myxmod"):
        kg.add_node(GraphNode(
            id=f"py:function:{stem}.f", type=NodeType.FUNCTION,
            name=f"{stem}.f", domain="py",
            metadata={"file_path": f"./{stem}.py", "lineno": 1},
        ))
    db = tmp_path / "graph.db"
    write_sqlite(kg, db)
    brief = file_brief(db, "my_mod.py", project_root=tmp_path)
    assert brief is not None
    assert [n.id for n in brief.nodes] == ["py:function:my_mod.f"]


# ---------------------------------------------------------------------------
# Against a hand-built graph — the doc-domain lines
# ---------------------------------------------------------------------------


@pytest.fixture()
def rich_db(tmp_path: Path) -> tuple[Path, Path]:
    """(db, root): one file whose function implements an equation,
    which is tested twice; a theory page documents the function."""
    root = tmp_path
    file_path = str(root / "solver.py")
    kg = KnowledgeGraph()
    kg.add_node(GraphNode(
        id="py:function:solver.solve", type=NodeType.FUNCTION,
        name="solver.solve", domain="py",
        metadata={"file_path": file_path, "lineno": 3},
    ))
    kg.add_node(GraphNode(
        id="math:equation:balance", type=NodeType.EQUATION,
        name="balance", domain="math", docname="theory/balance",
    ))
    kg.add_node(GraphNode(
        id="doc:theory/balance", type=NodeType.FILE,
        name="theory/balance", domain="doc",
    ))
    for i in (1, 2):
        kg.add_node(GraphNode(
            id=f"py:function:tests.test_{i}", type=NodeType.FUNCTION,
            name=f"tests.test_{i}", domain="py",
        ))
        kg.add_edge(GraphEdge(
            source=f"py:function:tests.test_{i}",
            target="math:equation:balance", type=EdgeType.TESTS,
        ))
    kg.add_edge(GraphEdge(
        source="py:function:solver.solve",
        target="math:equation:balance", type=EdgeType.IMPLEMENTS,
    ))
    kg.add_edge(GraphEdge(
        source="doc:theory/balance",
        target="py:function:solver.solve", type=EdgeType.DOCUMENTS,
    ))
    db = root / "graph.db"
    write_sqlite(kg, db)
    return db, root


def test_brief_equations_tests_and_docs(rich_db):
    db, root = rich_db
    brief = file_brief(db, root / "solver.py", project_root=root)
    assert brief is not None
    assert brief.equation_labels == ["balance"]
    assert brief.equation_test_count == 2
    assert brief.doc_pages == ["theory/balance"]


def test_brief_staleness_unknowable_without_git(rich_db):
    db, root = rich_db
    brief = file_brief(db, root / "solver.py", project_root=root)
    assert brief is not None
    assert brief.changed_since_build is None
    assert brief.build_commit is None


# ---------------------------------------------------------------------------
# Staleness — verified-unchanged vs changed vs unknowable
# ---------------------------------------------------------------------------


@pytest.fixture()
def stamped_repo(tmp_path: Path) -> tuple[Path, Path, Path]:
    """(db, root, file): a committed file, a graph stamped at that
    commit."""
    root = tmp_path / "proj"
    src = root / "pkg"
    src.mkdir(parents=True)
    target = src / "mod.py"
    target.write_text("def f():\n    return 1\n")
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "initial")
    kg = analyze_directory(src, project_root=root)
    stamp_provenance(kg, root)
    db = root / "graph.db"
    write_sqlite(kg, db)
    return db, root, target


def test_brief_verified_unchanged_is_false(stamped_repo):
    db, root, target = stamped_repo
    brief = file_brief(db, target, project_root=root)
    assert brief is not None
    assert brief.changed_since_build is False
    assert brief.build_commit


def test_brief_flags_file_changed_since_build(stamped_repo):
    db, root, target = stamped_repo
    target.write_text("def f():\n    return 2\n")
    brief = file_brief(db, target, project_root=root)
    assert brief is not None
    assert brief.changed_since_build is True
    assert "stale" in render_text(brief)


def test_brief_other_files_changing_does_not_flag(stamped_repo):
    db, root, target = stamped_repo
    (root / "unrelated.txt").write_text("noise\n")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "unrelated")
    brief = file_brief(db, target, project_root=root)
    assert brief is not None
    assert brief.changed_since_build is False


# ---------------------------------------------------------------------------
# Rendering — the ≤6-line contract
# ---------------------------------------------------------------------------


def _synthetic_brief(**overrides) -> FileBrief:
    from dataclasses import replace

    base = FileBrief(
        file_path="solver.py",
        module_id="py:module:solver",
        nodes=[
            BriefNode("py:module:solver", "module", "solver", 0, 9),
            BriefNode("py:function:solver.solve", "function",
                      "solver.solve", 3, 7),
            BriefNode("py:function:solver.aux", "function",
                      "solver.aux", 9, 1),
        ],
        external_caller_count=4,
        equation_labels=["a", "b", "c", "d", "e"],
        equation_test_count=12,
        doc_pages=["theory/x"],
        build_commit="abc1234",
        changed_since_build=True,
    )
    return replace(base, **overrides)


def test_render_stays_within_six_lines():
    assert len(render_text(_synthetic_brief()).splitlines()) <= 6


def test_render_hub_is_the_top_non_module_node():
    text = render_text(_synthetic_brief())
    assert "hub: py:function:solver.solve (degree 7)" in text


def test_render_clips_lists_to_three_with_remainder():
    text = render_text(_synthetic_brief())
    assert "a, b, c (+2)" in text
    assert "d" not in text.split("implements:")[1].split("—")[0]


def test_render_omits_empty_sections():
    text = render_text(_synthetic_brief(
        equation_labels=[], equation_test_count=0, doc_pages=[],
        changed_since_build=False,
    ))
    assert "implements" not in text
    assert "docs" not in text
    assert "stale" not in text
    assert len(text.splitlines()) == 2


def test_render_module_only_file_has_no_hub_line():
    text = render_text(_synthetic_brief(
        nodes=[BriefNode("py:module:solver", "module", "solver", 0, 2)],
        equation_labels=[], equation_test_count=0, doc_pages=[],
        changed_since_build=None,
    ))
    assert "hub:" not in text
