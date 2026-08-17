"""An answer about a test should end in something you can RUN.

The founding measurement (2026-08-16 field trial, `evals/FIDELITY.md`
F8): every hop of `equation → tests → pytest invocation` worked and the
chain still did not close — 34 equations reached their tests and **0**
handed over a runnable id, so two independent agents re-derived the
same `file_path` + dotted-name join by hand, one of them in 12 lines.
The same reply also withheld `vv_level` / `verifies` / `catches`, which
the graph has always held (F4, "silent knowledge"): an agent asking
"is this L0 evidence or an L4 cross-check?" had to open the SQLite
database directly.

These gates pin the join, the block that carries it, and — the part
that is easy to lose — the REFUSALS, because a fabricated pytest id is
worse than none at all.
"""

from __future__ import annotations

from pathlib import Path

import networkx as nx
import pytest

from sphinxcontrib.nexus._serialize import (
    _compact_node,
    assemble_context,
    assemble_impact,
    assemble_neighbors,
)
from sphinxcontrib.nexus.position import pytest_selector
from sphinxcontrib.nexus.query import GraphQuery
from sphinxcontrib.nexus.workspace import Workspace

ROOT = Path("/repo")


# ── the join itself ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("file_path", "dotted", "kind", "expected"),
    [
        # a plain test function
        ("/repo/tests/sn/test_alpha.py", "tests.sn.test_alpha.test_dome",
         "function", "tests/sn/test_alpha.py::test_dome"),
        # a method — pytest separates the class with `::`, not `.`
        ("/repo/tests/sn/test_alpha.py", "tests.sn.test_alpha.Case.test_dome",
         "method", "tests/sn/test_alpha.py::Case::test_dome"),
        # a nested class, so the `.`→`::` rewrite is not a single swap
        ("/repo/tests/sn/test_alpha.py", "tests.sn.test_alpha.A.B.test_x",
         "method", "tests/sn/test_alpha.py::A::B::test_x"),
        # a path already relative to the root
        ("tests/sn/test_alpha.py", "tests.sn.test_alpha.test_dome",
         "function", "tests/sn/test_alpha.py::test_dome"),
    ],
)
def test_a_definition_projects_to_the_name_pytest_collects_it_under(
    file_path, dotted, kind, expected,
):
    assert pytest_selector(file_path, dotted, kind, ROOT) == expected


@pytest.mark.parametrize(
    ("file_path", "dotted", "kind", "root", "why"),
    [
        # The load-bearing refusal. A name that disagrees with its own
        # file is exactly where a fabricated id would be silently wrong,
        # and it is reachable: re-exports and hand-built graphs produce
        # it. Without this arm the guard has no witness at all.
        ("/repo/tests/sn/test_alpha.py", "tests.sn.test_beta.test_dome",
         "function", ROOT, "name disagrees with its file"),
        # A prefix that merely SHARES a leading segment is not a prefix.
        ("/repo/tests/sn/test_alpha.py", "tests.sn.test_alpha_extra.test_x",
         "function", ROOT, "sibling module with a longer name"),
        # Outside the root: nothing to be relative to.
        ("/elsewhere/tests/test_a.py", "tests.test_a.test_x", "function",
         ROOT, "file is outside the project root"),
        # No root at all, absolute path — a pytest id is relative.
        ("/repo/tests/test_a.py", "tests.test_a.test_x", "function", None,
         "no root"),
        # Nodes that carry no file at all (doc-minted, hand-built).
        ("", "tests.test_a.test_x", "function", ROOT, "no file_path"),
        ("/repo/tests/test_a.py", "", "function", ROOT, "no name"),
        # The kinds the analyzer flags `is_test` that pytest cannot run.
        # `[M]` ORPHEUS: data 0/935 and attribute 0/279 resolve; a
        # `class` would resolve 810 of 882, which is the guess-shaped
        # answer this refuses to emit.
        ("/repo/tests/test_a.py", "tests.test_a.REGISTRY", "data", ROOT,
         "module-level data is not a pytest item"),
        ("/repo/tests/test_a.py", "tests.test_a.Scan.labels", "attribute",
         ROOT, "an attribute is not a pytest item"),
        ("/repo/tests/test_a.py", "tests.test_a.TestCase", "class", ROOT,
         "a class is a container, right only 92% of the time"),
    ],
)
def test_it_REFUSES_rather_than_guessing(file_path, dotted, kind, root, why):
    assert pytest_selector(file_path, dotted, kind, root) is None, why


# ── the block that carries it ───────────────────────────────────────


def _graph() -> nx.MultiDiGraph:
    """A test tree that is NOT uniform, because a uniform one cannot
    tell a working guard from an absent one.

    Deliberately irregular in five ways, one per arm below: a fully
    tagged test, one the source text tags with nothing, a helper in the
    test tree
    but is not collected, a test whose name disagrees with its file so
    no id can be derived for it, and a module-level CONSTANT that the
    analyzer flags ``is_test`` although pytest can never run it.
    """
    g = nx.MultiDiGraph()
    g.add_node("py:function:pkg.solve", type="function", name="pkg.solve",
               domain="py", file_path="/repo/pkg/solve.py", lineno=10)

    g.add_node(
        "py:method:tests.test_solve.Case.test_tagged",
        type="method", name="tests.test_solve.Case.test_tagged", domain="py",
        file_path="/repo/tests/test_solve.py", lineno=20,
        is_test=True, in_test_file=True,
        vv_level="L1", verifies=("sn-balance",), catches=("ERR-062",),
    )
    g.add_node(
        "py:function:tests.test_solve.test_untagged",
        type="function", name="tests.test_solve.test_untagged", domain="py",
        file_path="/repo/tests/test_solve.py", lineno=40,
        is_test=True, in_test_file=True,
    )
    # in the test tree, but pytest collects nothing here
    g.add_node(
        "py:function:tests.test_solve._build_mesh",
        type="function", name="tests.test_solve._build_mesh", domain="py",
        file_path="/repo/tests/test_solve.py", lineno=5,
        in_test_file=True,
    )
    # a test whose name does not match its file — no id is derivable
    g.add_node(
        "py:function:tests.test_other.test_reexported",
        type="function", name="tests.test_other.test_reexported", domain="py",
        file_path="/repo/tests/test_solve.py", lineno=60,
        is_test=True, in_test_file=True, vv_level="L0",
    )
    # `is_test` over-claims at the producer: the analyzer sets it on
    # module-level data in a test file. `[M]` ORPHEUS 935 such nodes,
    # 0 of them collectable.
    g.add_node(
        "py:data:tests.test_solve.REGISTRY",
        type="data", name="tests.test_solve.REGISTRY", domain="py",
        file_path="/repo/tests/test_solve.py", lineno=1,
        is_test=True, in_test_file=True,
    )
    for caller in (
        "py:method:tests.test_solve.Case.test_tagged",
        "py:function:tests.test_solve.test_untagged",
        "py:function:tests.test_solve._build_mesh",
        "py:function:tests.test_other.test_reexported",
        "py:data:tests.test_solve.REGISTRY",
    ):
        g.add_edge(caller, "py:function:pkg.solve", type="calls")
    return g


def _query() -> GraphQuery:
    return GraphQuery(_graph(), workspace=Workspace(db_path=ROOT / "g.db",
                                                    root=ROOT))


def test_a_test_node_carries_its_level_its_claims_and_a_runnable_id():
    facts = _query().get_node(
        "py:method:tests.test_solve.Case.test_tagged"
    ).test
    assert facts is not None
    assert facts.pytest_id == "tests/test_solve.py::Case::test_tagged"
    assert facts.vv_level == "L1"
    assert facts.verifies == ["sn-balance"]
    assert facts.catches == ["ERR-062"]


def test_production_nodes_carry_no_test_block_at_all():
    """The block is free on the ~15000 nodes that are not tests, which
    is the only reason it can be attached at the one choke point every
    node record flows through."""
    assert _query().get_node("py:function:pkg.solve").test is None


def test_a_HELPER_in_the_test_tree_is_not_a_test():
    """`is_test` (pytest collects it) and `in_test_file` (it lives in
    the test tree) differ by `[M]` 9594 − 7369 = 2225 nodes on ORPHEUS,
    and every one of them is a fixture or helper with no pytest id
    worth handing anybody."""
    assert _query().get_node("py:function:tests.test_solve._build_mesh").test is None


def test_a_CONSTANT_the_analyzer_calls_a_test_is_still_not_one():
    """`is_test` over-claims at the producer, so trusting it alone
    would put a `test` block — and, worse, a pytest command — on 1214
    ORPHEUS nodes that no pytest run can collect. Filed as a producer
    defect; until it is fixed the reply layer must not amplify it."""
    assert _query().get_node("py:data:tests.test_solve.REGISTRY").test is None


def test_a_level_the_SOURCE_does_not_carry_is_absent_not_invented():
    """This block reports what the analyzer could READ, and an AST walk
    cannot see module-level `pytestmark`, class marks, or marks a
    conftest applies at collection. `[M]` on ORPHEUS that gap is 1524
    against 5273, so synthesising an "untagged" here would state a
    falsehood about 254 files' worth of gates rather than decline to
    answer. `runtime_markers` is where the resolved answer lives."""
    facts = _query().get_node("py:function:tests.test_solve.test_untagged").test
    assert facts.vv_level == ""
    assert facts.verifies == []


def test_an_underivable_id_is_EMPTY_and_the_rest_of_the_block_survives():
    """A test whose name disagrees with its file still has a level and
    claims worth reporting. Losing them because one field could not be
    computed would be the wrong trade — and emitting a plausible id for
    it would be a worse one."""
    facts = _query().get_node("py:function:tests.test_other.test_reexported").test
    assert facts.pytest_id == ""
    assert facts.vv_level == "L0"


# ── what reaches the wire ───────────────────────────────────────────


def test_the_block_reaches_the_reply_with_its_empties_dropped():
    q = _query()
    tagged = _compact_node(
        q.get_node("py:method:tests.test_solve.Case.test_tagged")
    )
    assert tagged["test"] == {
        "pytest_id": "tests/test_solve.py::Case::test_tagged",
        "vv_level": "L1",
        "verifies": ["sn-balance"],
        "catches": ["ERR-062"],
    }
    untagged = _compact_node(
        q.get_node("py:function:tests.test_solve.test_untagged")
    )
    # no level, no verifies, no catches — said by not being there
    assert untagged["test"] == {
        "pytest_id": "tests/test_solve.py::test_untagged",
    }
    assert "test" not in _compact_node(q.get_node("py:function:pkg.solve"))


def test_a_NESTED_block_does_not_break_the_parallel_edge_fold():
    """`_dedupe_parallel` keys an entry on its content, so an entry
    holding a list or a nested object used to raise `TypeError` from
    the reply path. That failed once for `via` (fixed by spelling one
    field as a tuple) and again for this block (which no per-field
    spelling could fix), so the key itself was made total.

    The fixture must contain a REPEATED edge, or the fold never builds
    a key and the gate passes without exercising anything.
    """
    g = _graph()
    g.add_edge("py:method:tests.test_solve.Case.test_tagged",
               "py:function:pkg.solve", type="calls")
    q = GraphQuery(g, workspace=Workspace(db_path=ROOT / "g.db", root=ROOT))

    bucket = assemble_context(q, "py:function:pkg.solve")["incoming"]["calls"]
    tagged = next(e for e in bucket if e["id"].endswith("Case.test_tagged"))
    assert tagged["times"] == 2
    assert tagged["test"]["pytest_id"] == "tests/test_solve.py::Case::test_tagged"


def test_the_FLAT_adjacency_view_stays_an_adjacency():
    """`neighbors` already drops position for the same reason: a flat
    dump pays a dossier on every entry to serve the one or two you will
    open. `context` is where the dossier belongs."""
    entries = assemble_neighbors(_query(), "py:function:pkg.solve")
    assert entries, "fixture produced no neighbours"
    assert all("test" not in e for e in entries)
    assert all("file_path" not in e for e in entries)


# ── narrowing the answer, never the walk ────────────────────────────


def test_impact_can_return_only_the_gates():
    payload = assemble_impact(
        _query(), "py:function:pkg.solve", direction="upstream", only="tests",
    )
    ids = {e["id"] for e in payload["by_depth"][1]}
    assert ids == {
        "py:method:tests.test_solve.Case.test_tagged",
        "py:function:tests.test_solve.test_untagged",
        "py:function:tests.test_other.test_reexported",
    }
    assert payload["only"] == "tests"
    assert payload["total_in_role"] == 3


def test_the_filter_does_not_change_how_big_the_blast_radius_IS():
    """"How much depends on this?" and "which of it is tests?" are two
    questions. A filter that silently answered the first with the
    second's number would be wrong in the dangerous direction — it
    under-reports risk."""
    q = _query()
    unfiltered = assemble_impact(q, "py:function:pkg.solve", direction="upstream")
    filtered = assemble_impact(
        q, "py:function:pkg.solve", direction="upstream", only="tests",
    )
    assert unfiltered["total_affected"] == 5
    assert filtered["total_affected"] == 5
    assert filtered["total_in_role"] == 3


def test_only_code_is_the_complement_not_a_second_heuristic():
    payload = assemble_impact(
        _query(), "py:function:pkg.solve", direction="upstream", only="code",
    )
    assert {e["id"] for e in payload["by_depth"][1]} == {
        "py:function:tests.test_solve._build_mesh",
        "py:data:tests.test_solve.REGISTRY",
    }
    assert payload["total_in_role"] == 2


def test_an_unfiltered_answer_says_nothing_about_a_filter():
    payload = assemble_impact(_query(), "py:function:pkg.solve")
    assert "only" not in payload
    assert "total_in_role" not in payload


def test_the_tool_refuses_an_unknown_role():
    import json

    import sphinxcontrib.nexus.server as S

    error = json.loads(S.impact.__wrapped__(
        "py:function:pkg.solve", only="fixtures",
    ))
    assert "only must be" in error["error"]
