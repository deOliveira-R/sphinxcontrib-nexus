"""Execution evidence adjudicating a coverage CLAIM.

A `verifies`/`catches` marker is authored, stamped `confidence=1.0` and
points at an equation rather than at code, so nothing in the graph could
contradict one. These gates pin the join that can — and, just as much,
pin that it refuses to adjudicate what it cannot see.
"""

import networkx as nx
import pytest

from sphinxcontrib.nexus.query import (
    CORROBORATED,
    NO_IMPLEMENTATION,
    OUT_OF_CAPTURE,
    REFUTED,
    GraphQuery,
)
from sphinxcontrib.nexus.runtime import RuntimeRun


def _graph() -> nx.MultiDiGraph:
    """Two equations. `eq_impl` has code; `eq_bare` has none.

    Three claimants on `eq_impl`, one per verdict: `t_ran` executed the
    implementation, `t_idle` was captured and executed nothing, `t_away`
    is outside the capture entirely.
    """
    g = nx.MultiDiGraph()
    for eid in ("math:equation:eq_impl", "math:equation:eq_bare"):
        g.add_node(eid, type="equation", name=eid.split(":")[-1],
                   domain="math", docname="theory/p", lineno=10)
    g.add_node("py:function:m.impl", type="function", name="m.impl",
               domain="py", file_path="/p/m.py", lineno=1, end_lineno=4)
    g.add_edge("py:function:m.impl", "math:equation:eq_impl",
               type="implements", source="declared")
    for t in ("ran", "idle", "away"):
        g.add_node(f"py:function:t.{t}", type="function", name=f"t.{t}",
                   domain="py", file_path="/p/t.py", lineno=1, end_lineno=2,
                   is_test=True)
        g.add_edge(f"py:function:t.{t}", "math:equation:eq_impl",
                   type="tests", confidence=1.0)
    # A claim on the equation nothing implements — unadjudicable for a
    # reason no capture can fix.
    g.add_edge("py:function:t.ran", "math:equation:eq_bare",
               type="tests", confidence=1.0)
    return g


def _run() -> RuntimeRun:
    """`t_ran` and `t_idle` were captured; `t_away` was not."""
    return RuntimeRun(
        name="cov", kind="coverage",
        exercised_by={
            "py:function:m.impl": ["py:function:t.ran"],
            # `t_idle` ran something — that is what puts it IN the
            # capture, which is what makes its verdict a refutation
            # rather than an absence.
            "py:function:m.other": ["py:function:t.idle"],
        },
    )


def _verdicts(coverage, equation_id):
    entry = next(e for e in coverage.entries if e.node.id == equation_id)
    return {t.id.rsplit(".", 1)[-1]: t.execution for t in entry.tests}


@pytest.fixture
def coverage():
    g = _graph()
    g.add_node("py:function:m.other", type="function", name="m.other",
               domain="py", file_path="/p/m.py", lineno=6, end_lineno=8)
    return GraphQuery(g).verification_coverage(run=_run())


def test_a_claim_whose_test_RAN_the_implementation_is_corroborated(coverage):
    assert _verdicts(coverage, "math:equation:eq_impl")["ran"] == CORROBORATED


def test_a_captured_claimant_that_ran_NONE_of_it_is_refuted(coverage):
    """The finding the whole join exists for. `[M]` 10 of ORPHEUS's 2748
    declared claim edges are in this state under `geom_ctx,num_ctx`."""
    assert _verdicts(coverage, "math:equation:eq_impl")["idle"] == REFUTED


def test_an_UNCAPTURED_claimant_is_not_adjudicated(coverage):
    """Not a refutation — the capture never ran it. `[M]` 1751 of 2748.
    Collapsing this into `refuted` would report a 99 %-refuted suite."""
    assert _verdicts(coverage, "math:equation:eq_impl")["away"] == OUT_OF_CAPTURE


def test_a_claim_on_an_equation_NOTHING_implements_is_not_adjudicated(coverage):
    """The second unadjudicable cause, and it needs a different repair:
    no width of capture fixes it, only a declared `implements` link.
    `[M]` 976 of 2748 — 35.5 %, so it is not a corner."""
    assert _verdicts(coverage, "math:equation:eq_bare")["ran"] == NO_IMPLEMENTATION


def test_UNADJUDICABLE_is_decided_BEFORE_refuted(coverage):
    """The branch ORDER is the semantics, and this is its witness.

    `t_away` executed no implementation — read naively that is exactly
    the refuted predicate. It must not be, because it was never run.
    `[M]` with the branches the other way round, 2727 of ORPHEUS's 2748
    claims move into `refuted` and the audit reports a false catastrophe.
    """
    v = _verdicts(coverage, "math:equation:eq_impl")
    assert v["away"] == OUT_OF_CAPTURE and v["away"] != REFUTED
    assert _verdicts(coverage, "math:equation:eq_bare")["ran"] != REFUTED


def test_with_NO_run_the_report_is_the_pre_evidence_one():
    """Every project that has captured nothing must keep its audit."""
    c = GraphQuery(_graph()).verification_coverage()
    assert c.capture is None
    assert all(t.execution == "" for e in c.entries for t in e.tests)
    assert not [k for k in c.summary if k.startswith("claims_")]


def test_a_run_that_can_adjudicate_NOTHING_refutes_nothing():
    """A cProfile run carries no attribution. Joined naively it would
    mark every claim refuted — the instrument failing in the confident
    direction (vv #17), against 2748 claims at once."""
    c = GraphQuery(_graph()).verification_coverage(
        run=RuntimeRun(name="prof", kind="cprofile"))
    assert all(t.execution == "" for e in c.entries for t in e.tests)


def test_the_summary_agrees_with_its_own_ROWS(coverage):
    """Counted once over the finished entries, not at each site that
    builds them. Tallying inside the loops let the equation branch be the
    only one that incremented: `claims_refuted` read 212 while the rows
    carried 1231."""
    from collections import Counter
    # Two tallies, and the split is the point: `claims_*` counts rows
    # somebody ASSERTED, `executed_unclaimed` counts rows evidence
    # minted. Every stamped row lands in exactly one.
    walked = Counter(t.execution for e in coverage.entries
                     for t in e.tests if t.execution and t.source != "executed")
    minted = sum(1 for e in coverage.entries for t in e.tests
                 if t.execution and t.source == "executed")
    for verdict, n in walked.items():
        assert coverage.summary[f"claims_{verdict}"] == n
    assert coverage.summary.get("executed_unclaimed", 0) == minted
    stamped = sum(1 for e in coverage.entries for t in e.tests if t.execution)
    assert sum(v for k, v in coverage.summary.items()
               if k.startswith("claims_")) + minted == stamped


def test_the_capture_states_what_it_could_have_adjudicated(coverage):
    """A rate without its denominator is how "11 corroborated" gets read
    as "the suite verifies almost nothing"."""
    cap = coverage.capture
    assert cap is not None
    assert cap.runs == ["cov"]
    assert cap.claimants_total >= cap.claimants_in_capture > 0
    assert cap.captured_tests == 2          # t_ran and t_idle


def test_a_refuted_claim_travels_with_the_LINKs_provenance(coverage):
    """`[M]` 12999 of 13084 ORPHEUS `implements` edges are inferred from a
    shared name token. A refutation against a guessed link refutes the
    GUESS, not the test — so `code_evidence` must be readable beside the
    verdict, or the audit reports nexus#82 as a V&V finding."""
    entry = next(e for e in coverage.entries
                 if e.node.id == "math:equation:eq_impl")
    assert entry.code_evidence == "declared"
    assert any(t.execution == REFUTED for t in entry.tests)


def test_a_CODE_LEVEL_row_is_adjudicated_against_the_node_itself(coverage):
    """A `tested`/`orphan_code` row has no equation between the test and
    the thing it ran, so the node IS the implementation to check.

    Its own witness, because the equation-level gates cannot supply one:
    `[M]` on ORPHEUS the code-side rows are most of the difference
    between the declared-only tally (11/10) and the full one
    (5994/1231), so leaving this arm unstamped would silently empty the
    larger half of the report.
    """
    entry = next(e for e in coverage.entries
                 if e.node.id == "py:function:m.other")
    assert entry.status == "tested"
    assert [t.execution for t in entry.tests] == [CORROBORATED]


def test_evidence_ADDS_a_code_row_the_call_heuristic_never_proposed():
    """`orphan_code` must not mean "the `calls` graph could not see it".

    The code-side rows come from the 1-hop `calls` heuristic, and `[M]`
    that relation has 12-15 % recall against execution on ORPHEUS — a
    property, a dunder or a polymorphic call reaches this loop with an
    empty list. `m.lonely` below is run by a test and called by nobody,
    which is the shape of `Mesh1D.__post_init__`: static cone 0, and 81
    tests actually ran it.
    """
    g = nx.MultiDiGraph()
    g.add_node("py:function:m.lonely", type="function", name="m.lonely",
               domain="py", file_path="/p/m.py", lineno=1, end_lineno=3)
    g.add_node("py:function:t.ran", type="function", name="t.ran", domain="py",
               file_path="/p/t.py", lineno=1, end_lineno=2, is_test=True)
    q = GraphQuery(g)
    # No `calls` edge anywhere — the heuristic has nothing to offer.
    assert q.verification_coverage().entries[0].status == "orphan_code"

    run = RuntimeRun(name="cov", kind="coverage",
                     exercised_by={"py:function:m.lonely": ["py:function:t.ran"]})
    entry = next(e for e in q.verification_coverage(run=run).entries
                 if e.node.id == "py:function:m.lonely")
    assert entry.status == "tested"
    assert [(t.source, t.execution) for t in entry.tests] == [
        ("executed", CORROBORATED)]


def test_an_evidence_MINTED_row_is_not_counted_as_an_adjudicated_claim():
    """⛔ A row that exists BECAUSE execution says so cannot corroborate
    anything — its verdict is a tautology.

    Counting them took `claims_corroborated` from 5994 to **36466** on
    ORPHEUS: a headline that then moves with the SIZE OF THE CAPTURE
    rather than with how well the suite's assertions hold up. That is
    `plan-authoring` §10 — a metric invalidated by its own success — in
    a number this very change introduced. They are reported separately
    as `executed_unclaimed`, which is the honest reading: code a test
    ran that nothing claims. `[M]` 30 472 of them.
    """
    g = nx.MultiDiGraph()
    g.add_node("py:function:m.lonely", type="function", name="m.lonely",
               domain="py", file_path="/p/m.py", lineno=1, end_lineno=3)
    g.add_node("py:function:t.ran", type="function", name="t.ran", domain="py",
               file_path="/p/t.py", lineno=1, end_lineno=2, is_test=True)
    run = RuntimeRun(name="cov", kind="coverage",
                     exercised_by={"py:function:m.lonely": ["py:function:t.ran"]})
    c = GraphQuery(g).verification_coverage(run=run)
    assert c.summary.get("executed_unclaimed") == 1
    assert c.summary.get("claims_corroborated", 0) == 0, (
        "an evidence-minted row must not inflate the adjudicated tally")


def test_the_audit_carries_the_verdicts_and_the_scope():
    g = _graph()
    g.add_node("py:function:m.other", type="function", name="m.other",
               domain="py", file_path="/p/m.py", lineno=6, end_lineno=8)
    a = GraphQuery(g).verification_audit(run=_run())
    assert a.capture is not None
    assert a.summary["claims_corroborated"] >= 1
    assert a.summary["claims_refuted"] >= 1
    # …and without a run it stays the report it always was.
    assert GraphQuery(g).verification_audit().capture is None


def test_a_claim_is_corroborated_through_a_CLASS_it_implements():
    """The descent, at the audit level. A class never binds in a capture,
    so without it a claim whose implementation is a class is refuted no
    matter what ran. `[M]` on ORPHEUS this flips exactly one declared
    claim, corroborated 10 -> 11 and refuted 11 -> 10."""
    g = nx.MultiDiGraph()
    g.add_node("math:equation:eq", type="equation", name="eq", domain="math",
               docname="theory/p", lineno=1)
    g.add_node("py:class:m.C", type="class", name="m.C", domain="py",
               file_path="/p/m.py", lineno=1, end_lineno=9)
    g.add_node("py:method:m.C.meth", type="method", name="m.C.meth",
               domain="py", file_path="/p/m.py", lineno=3, end_lineno=5)
    g.add_edge("py:class:m.C", "py:method:m.C.meth", type="contains")
    g.add_edge("py:class:m.C", "math:equation:eq", type="implements",
               source="declared")
    g.add_node("py:function:t.ran", type="function", name="t.ran", domain="py",
               file_path="/p/t.py", lineno=1, end_lineno=2, is_test=True)
    g.add_edge("py:function:t.ran", "math:equation:eq", type="tests")
    run = RuntimeRun(name="cov", kind="coverage",
                     exercised_by={"py:method:m.C.meth": ["py:function:t.ran"]})
    c = GraphQuery(g).verification_coverage(run=run)
    assert _verdicts(c, "math:equation:eq")["ran"] == CORROBORATED
