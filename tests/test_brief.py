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

import json
import subprocess
from pathlib import Path

import pytest

from sphinxcontrib.nexus.ast_analyzer import analyze_directory
from sphinxcontrib.nexus.brief import (
    BriefNode,
    FileBrief,
    GateSummary,
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
from sphinxcontrib.nexus.query import GraphQuery
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


def test_symlinked_root_resolves_through_both_lookup_strategies(analyzed_db):
    """A symlinked alias of the root must reach the same nodes through
    the SQL two-tier lookup and through a full graph scan.

    ⚠ Re-scoped 2026-08-16. This gate used to be described as catching
    DRIFT between two realizations of the path-equality contract; there
    is now one (``workspace.canonical_path``), so no input can make the
    two normalizations disagree and that claim is unfalsifiable. What
    survives is a real comparison: the *lookup strategies* are still
    independent — ``_in_file_node_ids`` matches pre-computed spellings
    in SQL and falls back to a basename-prefiltered scan, while
    ``node_at`` normalizes every stored path. This can still fail when
    the SQL tier anticipates the wrong spellings.

    The contract itself is pinned directly by
    ``test_workspace.py::TestCanonicalPath``, which asserts its laws
    against hand-written expectations rather than against a second
    implementation."""
    from sphinxcontrib.nexus.export import load_sqlite
    from sphinxcontrib.nexus.query import GraphQuery

    db, root, lib = analyzed_db
    alias = root.parent / "alias"
    alias.symlink_to(root, target_is_directory=True)
    aliased_lib = alias / lib.relative_to(root)

    brief = file_brief(db, aliased_lib, project_root=alias)
    assert brief is not None and len(brief.nodes) >= 2

    from sphinxcontrib.nexus.workspace import Workspace

    q = GraphQuery(load_sqlite(db), workspace=Workspace(db_path=db, root=alias))
    node = q.node_at(aliased_lib, 1)
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
        id="std:file:theory/balance", type=NodeType.FILE,
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
        source="std:file:theory/balance",
        target="py:function:solver.solve", type=EdgeType.DOCUMENTS,
    ))
    db = root / "graph.db"
    write_sqlite(kg, db)
    return db, root


def test_brief_equations_tests_and_docs(rich_db):
    """Ids, not labels. `[M]` 0 of 50 rendered labels pasted into any
    tool, because each needed a `math:equation:` prefix the emitter had
    and dropped — an unaddressable handle, in the one reply that
    arrives unasked and so cannot be re-queried for the real one."""
    db, root = rich_db
    brief = file_brief(db, root / "solver.py", project_root=root)
    assert brief is not None
    assert brief.equation_ids == ["math:equation:balance"]
    assert brief.equation_test_count == 2
    assert brief.doc_page_ids == ["std:file:theory/balance"]


def test_a_file_with_no_gates_has_no_gate_section(rich_db):
    """Presence, not a file-KIND flag: nothing here asks whether this
    is a test file, so a file that both implements equations and
    contains gates would report both."""
    db, root = rich_db
    brief = file_brief(db, root / "solver.py", project_root=root)
    assert brief.gates is None


@pytest.fixture()
def test_file_db(tmp_path: Path) -> tuple[Path, Path]:
    """(db, root): one test file with two gates, a fixture, and a
    module-level constant.

    The constant is the point. The analyzer flags every node in a test
    module ``is_test``, including data it can never collect (`[M]` 935
    such nodes on ORPHEUS), so a fixture with only real gates in it
    could not tell a correct count from one inflated by them.
    """
    root = tmp_path
    path = str(root / "tests" / "test_ld.py")
    kg = KnowledgeGraph()
    kg.add_node(GraphNode(
        id="py:module:tests.test_ld", type=NodeType.MODULE,
        name="tests.test_ld", domain="py", metadata={"file_path": path},
    ))
    kg.add_node(GraphNode(
        id="math:equation:ld-2d", type=NodeType.EQUATION,
        name="ld-2d", domain="math", docname="theory/ld",
    ))
    for name, level, catches in (
        ("test_order", "L1", ["ERR-062"]),
        ("test_value", "", []),
    ):
        node_id = f"py:function:tests.test_ld.{name}"
        kg.add_node(GraphNode(
            id=node_id, type=NodeType.FUNCTION,
            name=f"tests.test_ld.{name}", domain="py",
            metadata={
                "file_path": path, "lineno": 10, "is_test": True,
                "in_test_file": True, "vv_level": level, "catches": catches,
            },
        ))
        kg.add_edge(GraphEdge(
            source=node_id, target="math:equation:ld-2d", type=EdgeType.TESTS,
        ))
    kg.add_node(GraphNode(
        id="py:function:tests.test_ld._mesh", type=NodeType.FUNCTION,
        name="tests.test_ld._mesh", domain="py",
        metadata={"file_path": path, "lineno": 3, "in_test_file": True},
    ))
    kg.add_node(GraphNode(
        id="py:data:tests.test_ld.CASES", type=NodeType.DATA,
        name="tests.test_ld.CASES", domain="py",
        metadata={
            "file_path": path, "lineno": 1,
            "is_test": True, "in_test_file": True,
        },
    ))
    db = root / "graph.db"
    write_sqlite(kg, db)
    return db, root


def test_a_test_file_reports_its_gates_not_its_fixtures(test_file_db):
    db, root = test_file_db
    brief = file_brief(db, root / "tests" / "test_ld.py", project_root=root)
    assert brief is not None and brief.gates is not None
    gates = brief.gates
    assert gates.count == 2, "a constant pytest cannot collect is not a gate"
    assert gates.helpers == 2, "the fixture AND the constant"
    assert gates.levels == {"": 1, "L1": 1}
    assert gates.equation_ids == ["math:equation:ld-2d"]
    assert gates.catches == ["ERR-062"]
    assert gates.pytest_target == "tests/test_ld.py"


def test_the_mcp_tool_returns_every_node_and_pads_none_of_them(test_file_db):
    """Unclipped is not unshaped.

    The node list IS what the hook's `+79 more nodes` expands to, so
    clipping it would reopen the dead end it exists to close. But
    `BriefNode` never passed through the reply layer, so it re-emitted
    the redundancy every other tool had already dropped — `name`
    reproduces the id's third segment, `type` its second.

    `[M]` 2026-08-17 through the real MCP surface: those two fields
    were 18–29 % of the payload and the node list 63–81 % of it, and
    `orpheus/sn/solver.py` came to **20 976 chars against a 20 000
    budget** — the one production file most worth briefing was the one
    that arrived truncated. Compacting the entries: −32 to −41 %.

    Found by CALLING the tool, not by reading it. The hook's text form
    was fine throughout (median 4 lines), which is exactly why this
    survived the render tests.
    """
    from sphinxcontrib.nexus import server
    from sphinxcontrib.nexus.export import load_sqlite
    from sphinxcontrib.nexus.workspace import Workspace

    db, root = test_file_db
    server._query = GraphQuery(
        load_sqlite(db), workspace=Workspace(db_path=db, root=root)
    )
    try:
        payload = json.loads(
            server.file_brief.__wrapped__("tests/test_ld.py")
        )
    finally:
        server._query = None

    brief = file_brief(db, root / "tests" / "test_ld.py", project_root=root)
    assert len(payload["nodes"]) == len(brief.nodes), "no node may be dropped"
    for entry in payload["nodes"]:
        domain, node_type, name = entry["id"].split(":", 2)
        assert entry.get("name") != name
        assert entry.get("type") != node_type


def test_the_mcp_tool_omits_a_section_the_file_does_not_have(rich_db):
    """`"gates": null` says exactly what saying nothing says.

    This tool built its payload with `asdict`, which keeps `None`, so
    it was the one reply on the whole surface exempt from the shared
    serializer's own rule — found by reading a live MCP reply."""
    from sphinxcontrib.nexus import server
    from sphinxcontrib.nexus.export import load_sqlite
    from sphinxcontrib.nexus.workspace import Workspace

    db, root = rich_db
    server._query = GraphQuery(
        load_sqlite(db), workspace=Workspace(db_path=db, root=root)
    )
    try:
        payload = json.loads(server.file_brief.__wrapped__("solver.py"))
    finally:
        server._query = None

    assert file_brief(db, root / "solver.py", project_root=root).gates is None
    assert "gates" not in payload
    assert payload["equation_ids"] == ["math:equation:balance"]


def test_the_gate_section_survives_extraction_to_render(test_file_db):
    """Every other gate-render assertion hands `render_text` a
    hand-built `GateSummary`, so all of them are blind to whether
    extraction produces one at all — measured: killing `_gate_summary`
    reddened exactly one test. This is the leg that joins the halves.
    """
    db, root = test_file_db
    text = render_text(
        file_brief(db, root / "tests" / "test_ld.py", project_root=root)
    )
    assert "gates: 2 (" in text
    assert "verifies: math:equation:ld-2d" in text
    assert 'run: pytest "tests/test_ld.py"' in text


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
    # The FIELD carries the flag; the ambient TEXT deliberately does
    # not — its consumer is the post-edit hook, where "changed since
    # build" is tautologically true (issue #15: 842/842 briefs carried
    # the line — zero information).
    assert "stale" not in render_text(brief)


def test_brief_other_files_changing_does_not_flag(stamped_repo):
    db, root, target = stamped_repo
    (root / "unrelated.txt").write_text("noise\n")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "unrelated")
    brief = file_brief(db, target, project_root=root)
    assert brief is not None
    assert brief.changed_since_build is False


# ---------------------------------------------------------------------------
# Rendering — the ambient-size contract
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
        equation_ids=["a", "b", "c", "d", "e"],
        equation_test_count=12,
        doc_page_ids=["std:file:theory/x"],
        gates=None,
        build_commit="abc1234",
        changed_since_build=True,
    )
    return replace(base, **overrides)


def _synthetic_gates(**overrides) -> GateSummary:
    from dataclasses import replace

    base = GateSummary(
        count=14,
        helpers=23,
        levels={"": 3, "L0": 2, "L1": 9},
        equation_ids=["math:equation:ld-cartesian-2d"],
        catches=["ERR-062"],
        pytest_target="tests/sn/test_ld.py",
    )
    return replace(base, **overrides)


def test_render_stays_ambient_sized():
    """This is an INJECTION on every edit, so its size is a cost paid
    whether or not it is read.

    Two different claims, both pinned. The RENDERER's worst case is 8
    lines — every section present and clipped. What ORPHEUS actually
    contains is smaller: `[M]` 2026-08-17 over all 858 briefable files,
    median 4 lines / 367 chars, p90 5, max 7 / 811. Neither number
    substitutes for the other, and the first is the one a new section
    has to be checked against.
    """
    everything = render_text(_synthetic_brief(gates=_synthetic_gates()))
    assert len(everything.splitlines()) <= 8
    assert len(everything) <= 900
    # the common shapes, which is what the median is made of
    assert len(render_text(_synthetic_brief()).splitlines()) <= 6
    with_gates = render_text(_synthetic_brief(
        equation_ids=[], equation_test_count=0, doc_page_ids=[],
        gates=_synthetic_gates(),
    ))
    assert len(with_gates.splitlines()) <= 6


def test_render_hub_is_the_top_non_module_node():
    text = render_text(_synthetic_brief())
    assert "hub: py:function:solver.solve (degree 7)" in text


def test_render_clips_lists_to_three_with_remainder():
    text = render_text(_synthetic_brief())
    assert "a, b, c (+2)" in text
    assert "d" not in text.split("implements:")[1].split("—")[0]


def test_a_clipped_list_names_the_tool_that_returns_the_rest():
    """`(+2)` on its own is a fact the reader cannot act on, and this
    reply arrives unasked — there is no prompt at which to ask for the
    members, and after a compaction it is gone."""
    text = render_text(_synthetic_brief())
    assert 'file_brief("solver.py")' in text


def test_nothing_clipped_means_no_follow_up_line():
    text = render_text(_synthetic_brief(equation_ids=["a"]))
    assert "file_brief(" not in text


def test_render_omits_empty_sections():
    text = render_text(_synthetic_brief(
        equation_ids=[], equation_test_count=0, doc_page_ids=[],
        changed_since_build=False,
    ))
    assert "implements" not in text
    assert "docs" not in text
    assert "stale" not in text
    assert len(text.splitlines()) == 2


def test_render_module_only_file_has_no_hub_line():
    text = render_text(_synthetic_brief(
        nodes=[BriefNode("py:module:solver", "module", "solver", 0, 2)],
        equation_ids=[], equation_test_count=0, doc_page_ids=[],
        changed_since_build=None,
    ))
    assert "hub:" not in text


def test_a_test_file_says_what_it_VERIFIES_and_how_to_run_it():
    """The founding defect: a test module's brief named its
    highest-degree node — always a fixture — and said nothing about the
    claim the file exists to make. It was SHORTEST for the one file
    kind whose whole purpose is a verification claim."""
    text = render_text(_synthetic_brief(gates=_synthetic_gates()))
    assert "gates: 14 (" in text
    assert "L1 9" in text
    assert "no level in source 3" in text
    assert "23 helpers" in text
    assert "catches ERR-062" in text
    assert "verifies: math:equation:ld-cartesian-2d" in text
    assert 'run: pytest "tests/sn/test_ld.py"' in text
