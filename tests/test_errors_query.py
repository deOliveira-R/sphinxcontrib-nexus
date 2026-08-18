"""The error catalogue as a graph question (`nexus#63`).

`catches` was a string attribute pointing at nothing, so *"which
catalogued defect has no catcher?"* was a grep against a markdown file.
These tests pin the query that replaces it, plus the two CLI modes.

End-to-end coverage — the real `.. error-entry::` directive minting a
real node and a real edge through a real build — lives in
`test_fixture_e2e.py`; the fixture declares one caught entry and one
uncaught one for exactly that purpose.
"""

from __future__ import annotations

import argparse

import networkx as nx
import pytest

from sphinxcontrib.nexus._serialize import to_dict
from sphinxcontrib.nexus.graph import EdgeType, NodeType
from sphinxcontrib.nexus.query import GraphQuery


def _graph(*, entries=(), markers=()):
    """`entries` are (id, title); `markers` are (test_name, (ids,))."""
    g = nx.MultiDiGraph()
    declared = set()
    for eid, title in entries:
        declared.add(eid)
        g.add_node(
            f"vv:{NodeType.ERROR.value}:{eid}", type=NodeType.ERROR.value,
            name=eid, display_name=title, domain="vv",
            docname="catalogue", title=title,
        )
    for name, ids in markers:
        node = f"py:function:tests.{name}.{name}"
        g.add_node(node, type="function", name=name, is_test=True,
                   catches=tuple(ids))
        for eid in ids:
            if eid in declared:
                g.add_edge(node, f"vv:{NodeType.ERROR.value}:{eid}",
                           type=EdgeType.CATCHES.value,
                           source="pytest.mark.catches", confidence=1.0)
    return g


def test_an_uncaught_entry_LEADS_the_answer():
    """The finding sorts first, so a truncated reply keeps it.

    An entry nobody catches is the whole point of the query; putting it
    behind 78 covered rows would let the reply budget drop the answer.
    """
    g = _graph(
        entries=[("ERR-001", "caught"), ("ERR-002", "never caught")],
        markers=[("test_a", ("ERR-001",))],
    )
    result = GraphQuery(g).errors()

    assert [e.name for e in result.entries] == ["ERR-002", "ERR-001"]
    assert result.uncaught == 1
    assert result.total_entries == 2
    assert result.total_catchers == 1


def test_the_title_SURVIVES_serialisation():
    """`ErrorEntry` is not a `NodeResult`, and this is why.

    `_compact_node` drops every falsy value AND drops the fields that
    merely repeat an id segment, so a `NodeResult` for `vv:error:ERR-001`
    serialises to `{"id": ...}` alone — losing `title`, the one field a
    reader actually scans. Pinning it here means a future "simplify this
    to a NodeResult" reds instead of silently emptying the reply.
    """
    g = _graph(entries=[("ERR-001", "Galerkin idempotency without the 4pi")])
    payload = to_dict(GraphQuery(g).errors())

    assert payload["entries"][0]["title"] == "Galerkin idempotency without the 4pi"
    assert payload["entries"][0]["id"] == "vv:error:ERR-001"


def test_a_marker_naming_NO_entry_is_reported_not_dropped():
    """It reads as coverage in a grep and is not one.

    The build warns about these, but a warning is gone by the time
    anyone audits the catalogue — so the query carries them.
    """
    g = _graph(entries=[("ERR-001", "x")],
               markers=[("test_a", ("ERR-001", "ERR-404"))])
    result = GraphQuery(g).errors()

    assert result.unresolved_markers == ["ERR-404"]
    assert result.uncaught == 0  # ERR-001 IS caught; the dangler is separate


def test_an_empty_catalogue_is_not_a_clean_one():
    """`total_entries == 0` means nothing is DECLARED.

    A project that declared nothing and a project whose every entry is
    caught both report `uncaught == 0`. `unresolved_markers` is what
    tells them apart, so the zero must not read as a clean bill.
    """
    g = _graph(markers=[("test_a", ("ERR-001",))])
    result = GraphQuery(g).errors()

    assert result.total_entries == 0
    assert result.uncaught == 0
    assert result.unresolved_markers == ["ERR-001"]


def test_the_catcher_cap_never_moves_the_TRUE_count():
    g = _graph(
        entries=[("ERR-001", "x")],
        markers=[(f"test_{i}", ("ERR-001",)) for i in range(5)],
    )
    result = GraphQuery(g).errors(max_catchers_per_entry=2)

    assert len(result.entries[0].catchers) == 2
    assert result.entries[0].catcher_count == 5
    assert result.total_catchers == 5


def test_the_catalogue_defaults_the_gaps_bucket():
    """`verification_gaps` no longer needs to be TOLD the catalogue.

    It reported `error_catalog_size: None` on every project for as long
    as a caller-supplied set was its only source — the surface `nexus#63`
    filed as stubbed-and-never-wired.
    """
    g = _graph(
        entries=[("ERR-001", "caught"), ("ERR-002", "never caught")],
        markers=[("test_a", ("ERR-001",))],
    )
    result = GraphQuery(g).verification_gaps()

    assert result.filters["error_catalog_size"] == 2
    assert [gap.display_name for gap in result.missing_err_catchers] == ["ERR-002"]


# ---------------------------------------------------------------------------
# CLI — the `!`-injection surface
# ---------------------------------------------------------------------------


def _run_cli(tmp_path, graph, **overrides):
    from sphinxcontrib.nexus.cli import _run_errors
    from sphinxcontrib.nexus.export import write_sqlite
    from sphinxcontrib.nexus.graph import KnowledgeGraph

    kg = KnowledgeGraph(graph)
    db = tmp_path / "graph.db"
    write_sqlite(kg, db)
    args = argparse.Namespace(
        db=db, limit=50, format="text",
        quiet_when_clean=False, exit_code=False,
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    return _run_errors(args)


def test_cli_text_mode_leads_with_the_imperative(tmp_path, capsys):
    g = _graph(entries=[("ERR-001", "caught"), ("ERR-002", "never caught")],
               markers=[("test_a", ("ERR-001",))])
    _run_cli(tmp_path, g)
    out = capsys.readouterr().out

    assert out.startswith("ERROR CATALOGUE — 1 of 2")
    assert "ERR-002" in out
    assert "ERR-001" not in out  # covered entries are not the finding


def test_cli_quiet_when_clean_costs_zero_context(tmp_path, capsys):
    g = _graph(entries=[("ERR-001", "caught")],
               markers=[("test_a", ("ERR-001",))])
    _run_cli(tmp_path, g, quiet_when_clean=True)

    assert capsys.readouterr().out == ""


def test_cli_exit_code_gates_ci(tmp_path):
    uncaught = _graph(entries=[("ERR-002", "never caught")])
    assert _run_cli(tmp_path, uncaught, exit_code=True) == 1

    clean = _graph(entries=[("ERR-001", "caught")],
                   markers=[("test_a", ("ERR-001",))])
    assert _run_cli(tmp_path, clean, exit_code=True) == 0


def test_cli_says_WHERE_it_looked_when_nothing_is_declared(tmp_path, capsys):
    """An absence must state its search (`nexus#59`)."""
    g = _graph(markers=[("test_a", ("ERR-001",))])
    rc = _run_cli(tmp_path, g, exit_code=True)
    out = capsys.readouterr().out

    assert "No error catalogue" in out
    assert "resolve to nothing" in out
    assert rc == 1
