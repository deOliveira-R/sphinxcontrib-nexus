"""Markers as pytest RESOLVED them, not as they were spelled (#61).

An AST walk sees a decorator. pytest sees module-level `pytestmark`,
class marks, and whatever a `conftest.py` attaches during collection —
and `[M]` on ORPHEUS that difference is the whole answer: the AST path
reports 0 nodes for `foundation`, `cap`, `regression` and `sentinel`
while the resolved manifest finds 3709 / 1707 / 111 / 39.
"""
from __future__ import annotations

import json

import networkx as nx
import pytest

from sphinxcontrib.nexus import runtime as rt
from sphinxcontrib.nexus.query import GraphQuery


def _graph(tmp_path) -> nx.MultiDiGraph:
    f = tmp_path / "test_thing.py"
    f.write_text("def test_a():\n    pass\n\n\ndef test_b():\n    pass\n")
    g = nx.MultiDiGraph()
    g.add_node("py:function:test_thing.test_a", type="function",
               name="test_thing.test_a", domain="py", is_test=True,
               file_path=str(f), lineno=1, end_lineno=2)
    g.add_node("py:function:test_thing.test_b", type="function",
               name="test_thing.test_b", domain="py", is_test=True,
               file_path=str(f), lineno=5, end_lineno=6)
    return g


def _manifest(tmp_path, records) -> str:
    path = tmp_path / "markers.json"
    path.write_text(json.dumps({
        "schema": 1, "rootdir": str(tmp_path),
        "collected": len(records), "with_markers": len(records),
        "tests": records,
    }))
    return str(path)


def test_a_module_level_marker_reaches_the_graph(tmp_path):
    """The founding case: `pytestmark = [pytest.mark.foundation]` at
    module scope is on no decorator, so the AST path reports 0 for it —
    [M] on ORPHEUS, across 254 files."""
    g = _graph(tmp_path)
    f = str(tmp_path / "test_thing.py")
    art = _manifest(tmp_path, [
        {"nodeid": "test_thing.py::test_a", "file": f, "lineno": 1,
         "markers": {"foundation": True, "l1": True}},
    ])
    run = rt.ingest_pytest(art, g, "markers", source_prefixes=[str(tmp_path)])

    assert run.kind == "pytest"
    assert run.markers["py:function:test_thing.test_a"]["foundation"] is True
    assert run.ledger.bound == 1
    assert run.unresolved == 0


def test_parametrised_ids_collapse_to_one_node_and_UNION_their_markers(tmp_path):
    """`test_a[x]` and `test_a[y]` are two pytest ids and ONE graph node.
    Unioning is the conservative reading: if one case is `slow`, running
    the node is slow."""
    g = _graph(tmp_path)
    f = str(tmp_path / "test_thing.py")
    art = _manifest(tmp_path, [
        {"nodeid": "test_thing.py::test_a[x]", "file": f, "lineno": 1,
         "markers": {"l1": True}},
        {"nodeid": "test_thing.py::test_a[y]", "file": f, "lineno": 1,
         "markers": {"slow": True}},
    ])
    run = rt.ingest_pytest(art, g, "markers", source_prefixes=[str(tmp_path)])

    node = "py:function:test_thing.test_a"
    assert set(run.markers[node]) == {"l1", "slow"}
    assert run.pytest_ids[node] == [
        "test_thing.py::test_a[x]", "test_thing.py::test_a[y]",
    ]


def test_the_answer_is_RUNNABLE_not_a_set_of_graph_ids(tmp_path):
    """The join every consumer re-derived by hand. A caller asking
    "what pins this?" wants a command, not ids to translate."""
    g = _graph(tmp_path)
    f = str(tmp_path / "test_thing.py")
    art = _manifest(tmp_path, [
        {"nodeid": "test_thing.py::test_a[x]", "file": f, "lineno": 1,
         "markers": {"verifies": "eq-one"}},
        {"nodeid": "test_thing.py::test_b", "file": f, "lineno": 5,
         "markers": {"slow": True}},
    ])
    run = rt.ingest_pytest(art, g, "markers", source_prefixes=[str(tmp_path)])

    rows = GraphQuery(g).runtime_markers(run, marker="verifies")
    assert len(rows) == 1
    assert rows[0].invocation == 'pytest "test_thing.py::test_a[x]"'
    assert rows[0].markers["verifies"] == "eq-one"


def test_no_marker_name_is_enumerated_anywhere(tmp_path):
    """A project's own marker must cost no nexus release — the AST path
    recognised exactly four names, which is why `regression` (the marker
    ORPHEUS's re-baseline adjudication turns on) resolved to 0 nodes."""
    g = _graph(tmp_path)
    f = str(tmp_path / "test_thing.py")
    art = _manifest(tmp_path, [
        {"nodeid": "test_thing.py::test_a", "file": f, "lineno": 1,
         "markers": {"a_marker_nexus_has_never_heard_of": "yes"}},
    ])
    run = rt.ingest_pytest(art, g, "markers", source_prefixes=[str(tmp_path)])

    rows = GraphQuery(g).runtime_markers(
        run, marker="a_marker_nexus_has_never_heard_of")
    assert len(rows) == 1


def test_a_run_that_cannot_carry_markers_is_refused_not_emptied(monkeypatch):
    """The #59 contract, applied to this family: asking a coverage run
    for markers must not print like a suite with no markers.

    ⚠ Asserts the refusal NAMES the family and the kind that carries it.
    A bare "it raised" passes on an unrelated error — the first version
    of this test did exactly that, catching a "No active workspace" from
    the store lookup and reading it as a correct refusal."""
    import sphinxcontrib.nexus.server as S

    monkeypatch.setattr(S, "_get_runtime_store", lambda: None)
    coverage_run = rt.RuntimeRun(name="cov", kind="coverage",
                                 coverage={"py:function:x": {}})
    with pytest.raises(ValueError) as excinfo:
        S._require_family(coverage_run, "markers", "runtime_markers")

    message = str(excinfo.value)
    assert "markers" in message and "pytest" in message, message
