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
    """A caller whose builtins are inserted FIRST — and that order is
    load-bearing, not incidental.

    ⚠ Do not "tidy" this by adding the project helpers first. Both
    ranking gates below were BLIND until 2026-08-16 precisely because
    they were: insertion order already put project symbols on top, so
    deleting the sort entirely left every gate green. `[M]` mutating
    `_rank_entries` to a no-op reddened **0 of 21** tests before this
    reordering and 2 after. The fixture was more regular than the world
    it stood in for, which is the one thing a fixture may never be.
    """
    g = nx.MultiDiGraph()
    g.add_node("py:function:pkg.caller", type="function", name="pkg.caller",
               domain="py")
    for name in ("isinstance", "float", "len"):
        g.add_node(f"py:function:{name}", type="external", name=name, domain="py")
        # a builtin is called many times, so degree alone would rank it first
        for _ in range(5):
            g.add_edge("py:function:pkg.caller", f"py:function:{name}",
                       type="calls")
    for i in range(3):
        g.add_node(f"py:function:pkg.helper{i}", type="function",
                   name=f"pkg.helper{i}", domain="py")
        g.add_edge("py:function:pkg.caller", f"py:function:pkg.helper{i}",
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


# ── the flat adjacency view (#67) ───────────────────────────────────
#
# `[M]` 2026-08-16 on ORPHEUS, `neighbors(solve_sn)`: 213 962 B when the
# issue was filed, 179 102 B after node compaction alone, 40 521 B here
# — past the issue's own 46 402 B (−78 %) target. The lever was not the
# node: it was that an entry restated the QUESTION (both endpoints, the
# pinned direction, the pinned edge type) and carried a node's location
# on disk for 220 nodes to serve the one you open.


def _adjacency_graph() -> nx.MultiDiGraph:
    """One node reached two ways, and one reached twice the same way.

    The pair is the discriminator for the dedupe key: `calls` and
    `type_uses` to the SAME node are two facts and must stay two
    entries, while two `calls` edges are one fact with a count.
    """
    g = nx.MultiDiGraph()
    g.add_node("py:function:pkg.caller", type="function", name="pkg.caller",
               domain="py", file_path="/abs/pkg/a.py", lineno=10)
    g.add_node("py:class:pkg.Thing", type="class", name="pkg.Thing",
               domain="py", file_path="/abs/pkg/b.py", lineno=20)
    g.add_node("py:function:pkg.callee", type="function", name="pkg.callee",
               domain="py", file_path="/abs/pkg/c.py", lineno=30)
    g.add_edge("py:function:pkg.caller", "py:class:pkg.Thing", type="calls")
    g.add_edge("py:function:pkg.caller", "py:class:pkg.Thing", type="type_uses")
    g.add_edge("py:function:pkg.caller", "py:function:pkg.callee", type="calls")
    g.add_edge("py:function:pkg.caller", "py:function:pkg.callee", type="calls")
    g.add_edge("py:module:pkg", "py:function:pkg.caller", type="contains")
    g.add_node("py:module:pkg", type="module", name="pkg", domain="py")
    return g


def test_an_entry_states_the_relation_not_both_endpoints():
    """`source` is the node you asked about and `target` is the id on
    the line above it — [M] the edge dict was 46 % of the reply and one
    of its four fields carried information. Naming the DIRECTION is what
    lets both endpoints go."""
    from sphinxcontrib.nexus._serialize import assemble_neighbors

    q = GraphQuery(_adjacency_graph())
    entries = assemble_neighbors(q, "py:function:pkg.caller")

    assert entries, "fixture produced no neighbours"
    for e in entries:
        assert "edge" not in e and "node" not in e, e
        assert not {"source", "target", "key"} & set(e), e
    incoming = [e for e in entries if e["direction"] == "in"]
    assert [e["id"] for e in incoming] == ["py:module:pkg"]


def test_a_field_the_QUERY_pinned_is_not_repeated_on_every_entry():
    """Ask for one direction and every entry would say "out"; filter to
    one edge type and every entry would say "calls". A field the caller
    fixed in the question is not part of the answer."""
    from sphinxcontrib.nexus._serialize import assemble_neighbors

    q = GraphQuery(_adjacency_graph())
    node = "py:function:pkg.caller"

    both = assemble_neighbors(q, node)
    assert all("direction" in e and "edge_type" in e for e in both)

    one_way = assemble_neighbors(q, node, direction="out")
    assert all("direction" not in e for e in one_way)
    assert all("edge_type" in e for e in one_way)

    one_type = assemble_neighbors(q, node, direction="out", edge_types=["calls"])
    assert all("edge_type" not in e and "direction" not in e for e in one_type)

    # ...but TWO types leave the field carrying information, so it stays
    two_types = assemble_neighbors(
        q, node, direction="out", edge_types=["calls", "type_uses"],
    )
    assert all("edge_type" in e for e in two_types)


def test_the_same_node_by_two_relations_stays_two_entries():
    """The dedupe key is the whole entry, not the id. In `context` the
    bucket has already fixed type+direction so the id would do; here it
    has not, and collapsing on id alone would silently ANSWER WRONG —
    reporting that caller only `type_uses` Thing."""
    from sphinxcontrib.nexus._serialize import assemble_neighbors

    q = GraphQuery(_adjacency_graph())
    out = assemble_neighbors(q, "py:function:pkg.caller", direction="out")

    thing = [e for e in out if e["id"] == "py:class:pkg.Thing"]
    assert sorted(e["edge_type"] for e in thing) == ["calls", "type_uses"]
    assert all("times" not in e for e in thing), thing

    callee = [e for e in out if e["id"] == "py:function:pkg.callee"]
    assert len(callee) == 1 and callee[0]["times"] == 2


def test_adjacency_is_not_location():
    """A neighbour's position on disk answers "where is this defined?",
    which is `context`/`node_at`'s question — [M] 26 % of the reply,
    paid on all 220 neighbours to serve the one or two you open."""
    from sphinxcontrib.nexus._serialize import assemble_context, assemble_neighbors

    q = GraphQuery(_adjacency_graph())
    entries = assemble_neighbors(q, "py:function:pkg.caller")
    assert all("file_path" not in e and "lineno" not in e for e in entries)

    # ...and the tool that DOES answer that question still does.
    ctx = assemble_context(q, "py:function:pkg.caller")
    assert ctx["node"]["file_path"] == "/abs/pkg/a.py"
    assert ctx["outgoing"]["calls"][0]["file_path"].startswith("/abs/")


def test_a_flat_answer_is_ranked_so_truncation_keeps_the_useful_half():
    """The boundary budget truncates a hub node's list, so the ordering
    decides what survives. Unranked, an arbitrary tail is dropped."""
    from sphinxcontrib.nexus._serialize import assemble_neighbors

    q = GraphQuery(_graph_with_builtins())
    entries = assemble_neighbors(q, "py:function:pkg.caller", direction="out")

    kinds = [e.get("type", "project") for e in entries]
    assert kinds == sorted(kinds, key=lambda k: k == "external"), entries
    assert kinds[0] == "project"


def test_context_buckets_are_unchanged_by_the_shared_dedupe_key():
    """`_dedupe_parallel` now keys on the whole entry rather than the
    id. Inside a context bucket those are the same thing — entries for
    one node are identical dicts — and this pins that they stay so."""
    from sphinxcontrib.nexus._serialize import assemble_context

    q = GraphQuery(_graph_with_builtins())
    calls = assemble_context(q, "py:function:pkg.caller")["outgoing"]["calls"]

    ids = [e["id"] for e in calls]
    assert len(ids) == len(set(ids)), ids
    assert next(e for e in calls if e["id"] == "py:function:isinstance")["times"] == 5


def test_a_docname_that_merely_repeats_the_id_goes_but_a_container_stays():
    """`std:file:api/geometry` carries `docname: "api/geometry"` — the
    id's own name segment. On an equation the same field names the PAGE
    that contains it, which the id does not say."""
    page = _compact_node(NodeResult(
        id="std:file:api/geometry", type="file", name="api/geometry",
        display_name="Geometry", domain="std", docname="api/geometry",
    ))
    assert "docname" not in page
    assert page["display_name"] == "Geometry"

    equation = _compact_node(NodeResult(
        id="math:equation:sn-within-group", type="equation",
        name="sn-within-group", display_name="(1)", domain="math",
        docname="theory/conventions/indexing_and_layout",
    ))
    assert equation["docname"] == "theory/conventions/indexing_and_layout"


def test_folding_parallel_edges_loses_no_edge():
    """``times`` is a redistribution, not a filter — the fold is only
    legitimate because every raw edge is still accounted for. `[M]` on
    ORPHEUS, `BC`'s 1699 raw edges fold to 417 entries whose ``times``
    sum back to exactly 1699 (`solve_sn` 374→220, `orpheus` 5299→743,
    all exact)."""
    from sphinxcontrib.nexus._serialize import assemble_neighbors

    q = GraphQuery(_adjacency_graph())
    raw = len(q.neighbors("py:function:pkg.caller", direction="both"))
    entries = assemble_neighbors(q, "py:function:pkg.caller")

    assert raw > len(entries), "fixture has no parallel edges to fold"
    assert sum(e.get("times", 1) for e in entries) == raw


# ── who breaks vs what pins me ──────────────────────────────────────


def _graph_with_test_callers() -> nx.MultiDiGraph:
    """One production caller, one test, and one HELPER inside a test
    file — the third is the discriminator between the two flags.

    Insertion order is adversarial on purpose (see
    `_graph_with_builtins`): the production caller goes in LAST and has
    the LOWEST degree, so neither insertion order nor degree can pass
    this gate by accident.
    """
    g = nx.MultiDiGraph()
    g.add_node("py:method:pkg.mod.Thing.build", type="method",
               name="pkg.mod.Thing.build", domain="py")

    g.add_node("py:function:tests.t.test_it", type="function",
               name="tests.t.test_it", domain="py",
               is_test=True, in_test_file=True)
    # is_test is FALSE here — a fixture helper, not a test case. [M] on
    # ORPHEUS this class is what made `solve_sn` report 3 production
    # callers and `LinearDiscontinuous` 7; both true counts are 0.
    g.add_node("py:function:tests.t._make_mesh", type="function",
               name="tests.t._make_mesh", domain="py",
               is_test=False, in_test_file=True)
    g.add_node("py:method:pkg.other.Caller.run", type="method",
               name="pkg.other.Caller.run", domain="py")

    for caller, times in (("py:function:tests.t.test_it", 4),
                          ("py:function:tests.t._make_mesh", 3),
                          ("py:method:pkg.other.Caller.run", 1)):
        for _ in range(times):
            g.add_edge(caller, "py:method:pkg.mod.Thing.build", type="calls")
    return g


def test_the_production_caller_leads_the_incoming_bucket():
    """"Who breaks if I change this?" and "what pins this?" are two
    questions sharing one bucket, and [M] on ORPHEUS the tests swamp it
    — 17 of 18 incoming calls for `LossKernelGauge.for_mesh`, 22 of 25
    for `solve_sn`. Unranked, that file's single production caller sat
    at rank 27 of 44, below any truncation."""
    from sphinxcontrib.nexus._serialize import assemble_context

    q = GraphQuery(_graph_with_test_callers())
    inc = assemble_context(q, "py:method:pkg.mod.Thing.build")["incoming"]["calls"]

    assert inc[0]["id"] == "py:method:pkg.other.Caller.run", [e["id"] for e in inc]


def test_a_HELPER_in_a_test_file_is_test_material_too():
    """`in_test_file`, not `is_test`. [M] by `is_test`, ORPHEUS's
    `LinearDiscontinuous` reports 7 production callers and every one is
    a `_ld_mesh`-style helper defined in a test module; the true count
    is 0. The wrong flag overstates "what breaks" by 7×."""
    from sphinxcontrib.nexus._serialize import assemble_context

    q = GraphQuery(_graph_with_test_callers())
    inc = assemble_context(q, "py:method:pkg.mod.Thing.build")["incoming"]["calls"]
    order = [e["id"] for e in inc]

    helper = order.index("py:function:tests.t._make_mesh")
    production = order.index("py:method:pkg.other.Caller.run")
    assert production < helper, order


def test_from_a_TEST_node_nothing_is_demoted():
    """Demotion is relative to the asker. From production, tests are
    the safety net; from a test, test material IS the subject, and
    sinking it would bury the answer to the question actually asked."""
    from sphinxcontrib.nexus._serialize import assemble_context

    g = _graph_with_test_callers()
    g.add_edge("py:function:tests.t.test_it", "py:function:tests.t._make_mesh",
               type="calls")
    g.add_edge("py:function:tests.t.test_it", "py:method:pkg.other.Caller.run",
               type="calls")

    out = assemble_context(GraphQuery(g), "py:function:tests.t.test_it")["outgoing"]
    ids = [e["id"] for e in out["calls"]]
    assert "py:function:tests.t._make_mesh" in ids
    # ranked on degree alone, as before — the helper is not sunk
    degrees = [e.get("degree", 0) for e in out["calls"]]
    assert degrees == sorted(degrees, reverse=True), out["calls"]


# ── a guess must not read like a fact (#74) ─────────────────────────


def _graph_with_mixed_evidence() -> nx.MultiDiGraph:
    """One equation reached by a DECLARED edge and a GUESSED one."""
    g = nx.MultiDiGraph()
    g.add_node("math:equation:scale-free-kernel", type="equation",
               name="scale-free-kernel", domain="math", docname="theory/ld")
    g.add_node("py:function:pkg.assemble_ubld", type="function",
               name="pkg.assemble_ubld", domain="py")
    g.add_node("py:method:pkg.Scattering.kernel", type="method",
               name="pkg.Scattering.kernel", domain="py")
    g.add_node("py:function:tests.t.test_it", type="function",
               name="tests.t.test_it", domain="py", is_test=True,
               in_test_file=True)

    # declared: somebody wrote @pytest.mark.verifies("scale-free-kernel")
    g.add_edge("py:function:tests.t.test_it", "math:equation:scale-free-kernel",
               type="tests", source="pytest.mark.verifies")
    # guessed: the name shares a token with the label
    g.add_edge("py:function:pkg.assemble_ubld", "math:equation:scale-free-kernel",
               type="implements", source="inferred", confidence=0.7,
               shared_tokens=["kernel"])
    g.add_edge("py:method:pkg.Scattering.kernel", "math:equation:scale-free-kernel",
               type="implements", source="inferred", confidence=0.7,
               shared_tokens=["kernel"])
    return g


def test_an_inferred_edge_says_so_AND_says_what_it_guessed_from():
    """[M] on ORPHEUS **14004 of 14004** `implements` edges are
    inferred — not one is declared — so a reader who assumes the default
    is wrong every single time. `via` is what settles it: seeing
    `Scattering.kernel` matched to a scale-free-kernel equation on the
    word "kernel" needs no further investigation."""
    from sphinxcontrib.nexus._serialize import assemble_context

    q = GraphQuery(_graph_with_mixed_evidence())
    inc = assemble_context(q, "math:equation:scale-free-kernel")["incoming"]

    guesses = inc["implements"]
    assert all(e["inferred"] is True for e in guesses), guesses
    assert all(tuple(e["via"]) == ("kernel",) for e in guesses), guesses


def test_a_DECLARED_edge_is_not_annotated_at_all():
    """Declared is the silent default. Marking it would spend bytes on
    every reply to say "normal", and the payload discipline is that a
    field must say something the reader cannot assume."""
    from sphinxcontrib.nexus._serialize import assemble_context

    q = GraphQuery(_graph_with_mixed_evidence())
    inc = assemble_context(q, "math:equation:scale-free-kernel")["incoming"]

    declared = inc["tests"]
    assert declared, "fixture produced no declared edge"
    assert all("inferred" not in e and "via" not in e for e in declared), declared


def test_the_evidence_survives_to_the_flat_view_too():
    from sphinxcontrib.nexus._serialize import assemble_neighbors

    q = GraphQuery(_graph_with_mixed_evidence())
    entries = assemble_neighbors(q, "math:equation:scale-free-kernel")

    by_type = {e["edge_type"]: e for e in entries}
    assert by_type["implements"]["inferred"] is True
    assert "inferred" not in by_type["tests"]


def test_an_annotated_entry_is_still_HASHABLE_for_the_parallel_fold():
    """⚠ `_dedupe_parallel` keys an entry on `tuple(sorted(items()))`,
    so a list value anywhere in an entry raises TypeError on every reply
    that carries one. `via` is a tuple for exactly this reason — the
    first version of it was a list and would have crashed `context` on
    any project with inferred edges, which is all of them."""
    from sphinxcontrib.nexus._serialize import _dedupe_parallel, assemble_context

    q = GraphQuery(_graph_with_mixed_evidence())
    entries = assemble_context(q, "math:equation:scale-free-kernel")["incoming"]

    doubled = entries["implements"] + entries["implements"]
    folded = _dedupe_parallel(doubled)          # must not raise
    assert all(e.get("times") == 2 for e in folded), folded
