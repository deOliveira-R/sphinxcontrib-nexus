"""What a tool ANSWER costs, and whether it is worth the cost.

A tool's reply lands in an agent's context and stays there, so payload
size is a correctness property of the tool, not a nicety. Measured on
ORPHEUS 2026-08-16, before any of this existed:

    processes()            1,238,013 tokens
    verification_audit()      41,901
    staleness()               27,529
    callers(transitive)       20,089
    session_briefing()        10,564

`processes()` alone is several times any context window — it defaulted
to `limit=None`, "every call chain in the graph".

These gates pin the three properties that fixed it: a hard ceiling at
the tool boundary, node dicts that repeat nothing the id already says,
and answers ordered so a truncated one keeps the project's own symbols.
"""

from __future__ import annotations

import json

import networkx as nx
import pytest

from sphinxcontrib.nexus._serialize import _compact_node, to_dict, to_json
from sphinxcontrib.nexus.query import GraphQuery, NodeResult


# ── the boundary budget ─────────────────────────────────────────────


def test_an_oversized_reply_is_trimmed_and_says_so():
    import sphinxcontrib.nexus.server as S

    payload = to_json({"processes": [{"chain": ["x"] * 20} for _ in range(4000)]})
    assert len(payload) > S.TOOL_PAYLOAD_BUDGET
    fitted = S._fit_budget(payload, "processes")

    assert len(fitted) <= S.TOOL_PAYLOAD_BUDGET
    note = json.loads(fitted)[S.BUDGET_KEY]
    assert note["tool"] == "processes"
    assert note["lists"]["processes"]["of"] == 4000
    assert note["lists"]["processes"]["kept"] < 4000
    assert "how_to_get_the_rest" in note


def test_a_reply_within_budget_is_untouched():
    """Byte-identical, not merely equal — the budget must be inert on
    the payloads that do not need it."""
    import sphinxcontrib.nexus.server as S

    payload = to_json({"nodes": [{"id": "py:function:a"}]})
    assert S._fit_budget(payload, "query") is payload


def test_the_budget_trims_EVERY_heavy_list_not_just_the_biggest():
    """`impact` spreads across `by_depth` and `context` across one
    bucket per edge type. Trimming only the single largest left them at
    [M] 63k and 48k characters against a 20k budget — the first version
    of this did exactly that."""
    import sphinxcontrib.nexus.server as S

    payload = to_json({"by_depth": {
        "1": [{"id": f"py:function:a{i}", "file_path": "/x/y.py"} for i in range(900)],
        "2": [{"id": f"py:function:b{i}", "file_path": "/x/y.py"} for i in range(900)],
        "3": [{"id": f"py:function:c{i}", "file_path": "/x/y.py"} for i in range(900)],
    }})
    fitted = S._fit_budget(payload, "impact")
    assert len(fitted) <= S.TOOL_PAYLOAD_BUDGET
    trimmed = json.loads(fitted)[S.BUDGET_KEY]["lists"]
    assert len(trimmed) == 3, trimmed      # all three, not one


def test_an_untrimmable_payload_is_returned_whole():
    """Over budget beats invalid: a reply with no list to shorten is
    passed through rather than mangled."""
    import sphinxcontrib.nexus.server as S

    payload = to_json({"prose": "x" * (S.TOOL_PAYLOAD_BUDGET + 100)})
    assert S._fit_budget(payload, "whatever") == payload


# ── the node dict ───────────────────────────────────────────────────


def test_a_node_repeats_nothing_the_id_already_says():
    """[M] `name` and `domain` reproduce their id segments on 22848 of
    22848 ORPHEUS nodes, so they are pure payload."""
    d = _compact_node(NodeResult(
        id="py:function:orpheus.sn.solver.solve_sn",
        type="function", name="orpheus.sn.solver.solve_sn",
        display_name="orpheus.sn.solver.solve_sn", domain="py",
        degree=374, file_path="/p/solver.py", lineno=2337,
    ))
    assert d == {
        "id": "py:function:orpheus.sn.solver.solve_sn",
        "degree": 374, "file_path": "/p/solver.py", "lineno": 2337,
    }


def test_a_type_that_CONTRADICTS_the_id_survives():
    """The 20% where `type` differs from its segment is the placeholder
    case — the id says what the name denotes, `type` says nothing was
    found under it. There it is the most informative field present, so
    dropping it by symmetry with `name` would delete the signal."""
    d = _compact_node(NodeResult(
        id="py:function:numpy.asarray", type="external",
        name="numpy.asarray", domain="py", degree=99,
    ))
    assert d["type"] == "external"
    assert "name" not in d and "domain" not in d


def test_a_display_name_worth_reading_survives():
    d = _compact_node(NodeResult(
        id="py:method:pkg.C.n_points", name="pkg.C.n_points",
        display_name="n_points", type="method", domain="py", degree=5,
    ))
    assert "display_name" not in d          # == the leaf, so redundant
    d2 = _compact_node(NodeResult(
        id="std:file:api/data", name="api/data",
        display_name="Data Package", type="file", domain="std", degree=9,
    ))
    assert d2["display_name"] == "Data Package"


def test_a_node_nested_in_another_result_is_compacted_too():
    """The bug that made the first version of this inert: `asdict`
    recurses and flattens nested dataclasses BEFORE the NodeResult
    check can see them, so the tools compacted and the briefing — the
    one reply loaded every session — did not."""
    from dataclasses import dataclass

    @dataclass
    class Wrapper:
        node: NodeResult
        note: str

    d = to_dict(Wrapper(
        node=NodeResult(id="py:class:pkg.C", type="class", name="pkg.C",
                        domain="py", degree=3),
        note="hi",
    ))
    assert d["node"] == {"id": "py:class:pkg.C", "degree": 3}


def test_a_none_field_is_dropped_but_an_empty_list_is_not():
    """`"equation": null` says what silence says, in 18 characters.
    `"tests": []` on a coverage entry IS the finding."""
    from dataclasses import dataclass, field

    @dataclass
    class Entry:
        equation: str | None = None
        tests: list = field(default_factory=list)

    assert to_dict(Entry()) == {"tests": []}


# ── ordering: a truncated answer keeps the project's own symbols ────


def _graph_with_builtins() -> nx.MultiDiGraph:
    g = nx.MultiDiGraph()
    g.add_node("py:function:pkg.caller", type="function", name="pkg.caller",
               domain="py")
    for i in range(3):
        g.add_node(f"py:function:pkg.helper{i}", type="function",
                   name=f"pkg.helper{i}", domain="py")
        g.add_edge("py:function:pkg.caller", f"py:function:pkg.helper{i}",
                   type="calls")
    for name in ("isinstance", "float", "len"):
        g.add_node(f"py:function:{name}", type="external", name=name, domain="py")
        # a builtin is called many times, so degree alone would rank it first
        for _ in range(5):
            g.add_edge("py:function:pkg.caller", f"py:function:{name}",
                       type="calls")
    return g


def test_builtins_sort_below_project_symbols():
    """[M] "what does solve_sn call?" answered with `float`,
    `isinstance` ×3, `type`, `tuple`, `getattr`, `TypeError` — 8 of 16
    entries were Python builtins, and they outrank project symbols on
    raw degree. They are real edges and stay; they must not be what a
    truncated answer keeps."""
    from sphinxcontrib.nexus._serialize import assemble_context

    q = GraphQuery(_graph_with_builtins())
    calls = assemble_context(q, "py:function:pkg.caller")["outgoing"]["calls"]
    kinds = [e.get("type", "project") for e in calls]
    assert kinds == sorted(kinds, key=lambda k: k == "external"), calls
    assert kinds[0] == "project"


def test_parallel_edges_collapse_to_one_entry_with_a_count():
    """Three `isinstance(...)` calls are one fact about the function,
    not three identical dicts — but the count is real signal, so it is
    kept rather than discarded."""
    from sphinxcontrib.nexus._serialize import assemble_context

    q = GraphQuery(_graph_with_builtins())
    calls = assemble_context(q, "py:function:pkg.caller")["outgoing"]["calls"]
    ids = [e["id"] for e in calls]
    assert len(ids) == len(set(ids)), ids
    builtin = next(e for e in calls if e["id"] == "py:function:isinstance")
    assert builtin["times"] == 5


def test_god_nodes_answers_about_the_project_not_about_python():
    """[M] 9 of ORPHEUS's top 10 by raw degree were stdlib or installed
    packages — `numpy.array`, `float`, `int`, `numpy.ndarray`. That
    answers "what does Python have", which nobody asked."""
    q = GraphQuery(_graph_with_builtins())
    hubs = q.god_nodes(top_n=5)
    assert all(n.type not in ("external", "unresolved") for n in hubs), hubs
    assert any(
        n.type == "external"
        for n in q.god_nodes(top_n=10, include_placeholders=True)
    )


# ── "nothing found" vs "you asked the wrong run" (lessons-L56) ──────


class _Run:
    """A stored run carrying only the families its kind can fill."""

    def __init__(self, name, kind, **families):
        self.name, self.kind = name, kind
        for f in ("calls", "edges", "coverage", "timeline"):
            setattr(self, f, families.get(f) or {})


def test_a_view_refuses_a_run_that_cannot_carry_it(monkeypatch):
    """[M] 2026-08-16, four of nexus's own tools returned `[]` here:
    `runtime_timeline`/`runtime_branches` on a cProfile run, and
    `runtime_hotspots`/`runtime_edges` on a coverage run — identical to
    a workload that genuinely exercised nothing. The docstrings even
    said so, which documents the ambiguity instead of removing it.
    """
    import sphinxcontrib.nexus.server as S

    class _Store:
        def list_runs(self):
            return [{"name": "prof", "kind": "cprofile"},
                    {"name": "cov", "kind": "coverage"}]

    monkeypatch.setattr(S, "_get_runtime_store", lambda: _Store())
    profile_run = _Run("prof", "cprofile", calls={"py:function:a": {}})

    with pytest.raises(ValueError) as excinfo:
        S._require_family(profile_run, "coverage", "runtime_branches")
    message = str(excinfo.value)
    assert "prof" in message and "cprofile" in message      # what you asked
    assert "coverage" in message                            # what it lacks
    assert "'cov'" in message                               # what would work
    assert "not an empty result" in message                 # and that it is not one


def test_a_view_the_run_CAN_carry_is_silent(monkeypatch):
    import sphinxcontrib.nexus.server as S

    monkeypatch.setattr(S, "_get_runtime_store", lambda: None)
    S._require_family(
        _Run("prof", "cprofile", calls={"py:function:a": {}}),
        "calls", "runtime_hotspots",
    )   # must not raise
