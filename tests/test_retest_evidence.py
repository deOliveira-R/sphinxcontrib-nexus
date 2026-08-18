"""`retest` answering from evidence where a capture can speak.

The static cone is a poor instrument for this question — `[M]` 12-15 %
recall against execution on ORPHEUS, and 0 of 300 proven test-symbol
pairs have any path over it. These gates pin what replaces it, and,
harder, pin the boundary of what a partial capture is allowed to claim.
"""

import subprocess

import networkx as nx
import pytest

from sphinxcontrib.nexus.query import EXECUTED, REACHABLE, GraphQuery
from sphinxcontrib.nexus.runtime import RuntimeRun
from sphinxcontrib.nexus.workspace import Workspace


def _repo(tmp_path):
    """A git repo with one dirty file, so `detect_changes` sees a change."""
    for cmd in (["git", "init"], ["git", "config", "user.email", "t@t.com"],
                ["git", "config", "user.name", "T"]):
        subprocess.run(cmd, cwd=tmp_path, capture_output=True)
    f = tmp_path / "m.py"
    f.write_text("def touched(): pass\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "i"], cwd=tmp_path, capture_output=True)
    f.write_text("def touched(): return 1\n")
    return f


def _graph(src) -> nx.MultiDiGraph:
    """`touched` is the change. Three tests reach it by `calls`.

    `t_in_cap_ran` and `t_in_cap_idle` are inside the capture; `t_out`
    is not. That third one is the whole point — a capture that never
    collected it cannot say anything about it, in either direction.
    """
    g = nx.MultiDiGraph()
    g.add_node("py:function:m.touched", type="function", name="m.touched",
               domain="py", file_path=str(src), lineno=1, end_lineno=2)
    for t in ("t_in_cap_ran", "t_in_cap_idle", "t_out"):
        g.add_node(f"py:function:k.{t}", type="function", name=f"k.{t}",
                   domain="py", file_path="/p/k.py", lineno=1, end_lineno=2,
                   is_test=True)
        g.add_edge(f"py:function:k.{t}", "py:function:m.touched", type="calls")
    return g


def _run() -> RuntimeRun:
    """`t_in_cap_ran` executed the change; `t_in_cap_idle` ran something
    else, which is what puts it in the capture. `t_out` is absent."""
    return RuntimeRun(
        name="cov", kind="coverage",
        exercised_by={
            "py:function:m.touched": ["py:function:k.t_in_cap_ran"],
            "py:function:m.elsewhere": ["py:function:k.t_in_cap_idle"],
        },
        coverage={"py:function:m.touched": {
            "lines_hit": 1, "lines_total": 1, "branches_hit": 0,
            "branches_total": 0, "missing_arcs": []}},
    )


@pytest.fixture
def q(tmp_path):
    src = _repo(tmp_path)
    g = _graph(src)
    g.add_node("py:function:m.elsewhere", type="function", name="m.elsewhere",
               domain="py", file_path="/p/other.py", lineno=1, end_lineno=2)
    return GraphQuery(g, workspace=Workspace.for_root(tmp_path))


def _rows(result):
    return {e.test: e.warrant
            for e in result.must_retest + result.should_retest}


def test_a_test_PROVEN_to_have_run_the_change_is_warranted_by_evidence(q):
    rows = _rows(q.retest(scope="unstaged", run=_run()))
    assert rows["py:function:k.t_in_cap_ran"] == EXECUTED


def test_a_capture_that_RAN_a_test_and_saw_nothing_drops_it(q):
    """The precision half. `t_in_cap_idle` reaches the change through
    `calls`, was collected by the capture, and executed none of it —
    `[M]` 944 such pairs on ORPHEUS, and excluding builtin hubs removes
    none of them, so no filter on the cone can find this."""
    assert "py:function:k.t_in_cap_idle" not in _rows(
        q.retest(scope="unstaged", run=_run()))


def test_a_capture_may_NOT_certify_a_test_it_never_collected(q):
    """⛔ The near-miss this file exists for.

    `t_out` is in the cone and outside the capture. Treating "the
    capture covers the changed symbol" as licence to drop the whole cone
    silently certifies every test the run never collected. `[M]` two
    ORPHEUS slices covering 1499 of 5278 tests reported
    `safe_to_skip = 5161` for a geometry change under exactly that
    reading — 3779 tests declared safe on the strength of never having
    been looked at.
    """
    r = q.retest(scope="unstaged", run=_run())
    rows = _rows(r)
    assert rows["py:function:k.t_out"] == REACHABLE
    # Exactly ONE of the three is knowably skippable — `t_in_cap_idle`,
    # which the capture ran and cleared. `t_out` is not skippable and
    # not evidence; it is the unknown, and it must stay in the answer.
    assert r.safe_to_skip == 1


def test_an_inference_row_is_never_labelled_as_evidence(q):
    """The two warrants are different KINDS of claim, and the reply must
    not blur them — the static cone's rows are a 12-15 %-recall guess."""
    rows = _rows(q.retest(scope="unstaged", run=_run()))
    assert rows["py:function:k.t_out"] == REACHABLE
    assert rows["py:function:k.t_in_cap_ran"] == EXECUTED


def test_with_no_run_the_answer_is_the_static_one(q):
    """A project that has captured nothing keeps the verb it had."""
    r = q.retest(scope="unstaged")
    assert r.capture is None
    assert {e.warrant for e in r.must_retest} == {REACHABLE}
    assert r.evidence_symbols == 0 and r.inferred_symbols == 0
    # …and the capture-refuted row is BACK, because nothing refuted it.
    assert "py:function:k.t_in_cap_idle" in _rows(r)


def test_the_reply_says_how_much_of_it_rests_on_evidence(q):
    r = q.retest(scope="unstaged", run=_run())
    assert r.capture is not None
    assert r.capture.runs == ["cov"]
    assert r.evidence_symbols == 1        # `touched` is covered
    assert r.inferred_symbols == 0
    assert r.capture.captured_tests == 2


def test_limit_truncates_and_SAYS_it_did(q):
    """The verb's own truncation note used to advise `limit`/`offset`
    arguments it did not have. `[M]` its reply was 225 071 characters on
    one ORPHEUS file, trimmed by the budget to 14 of 250 rows."""
    r = q.retest(scope="unstaged", run=_run(), limit=1)
    assert len(r.must_retest) <= 1 and len(r.should_retest) <= 1
    assert r.omitted >= 1
    assert q.retest(scope="unstaged", run=_run()).omitted == 0


def test_changed_symbols_are_node_ids_a_caller_can_paste(q):
    """They were display NAMES, so nothing in the reply could be fed to
    another node-id-taking tool."""
    assert q.retest(scope="unstaged").changed_symbols == [
        "py:function:m.touched"]


def test_a_row_is_a_runnable_selector_not_a_node_payload(tmp_path):
    """The sibling verb's landed fix, applied here: a `NodeResult` costs
    ~250 characters against a selector's ~70."""
    src = _repo(tmp_path)
    g = _graph(src)
    # A selector is only built when the node's dotted name AGREES with
    # its file — `pytest_selector` returns None rather than fabricating
    # one, and the fallback is the node id (never an empty string).
    g.add_node("py:function:test_k.test_named_right", type="function",
               name="test_k.test_named_right", domain="py",
               file_path=str(tmp_path / "test_k.py"), lineno=4, end_lineno=5,
               is_test=True)
    g.add_edge("py:function:test_k.test_named_right",
               "py:function:m.touched", type="calls")
    q = GraphQuery(g, workspace=Workspace.for_root(tmp_path))
    rows = {e.test for e in q.retest(scope="unstaged").must_retest}
    assert "test_k.py::test_named_right" in rows
    # …and the node whose name disagrees with its file falls back.
    assert "py:function:k.t_out" in rows
