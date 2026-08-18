"""The execution ledger — which tests EXECUTED which code.

The relation that can contradict a coverage CLAIM. Every gate here has a
witness measured on ORPHEUS 2026-08-18 and named in its docstring, so a
reader can tell a test of a real property from a test of an accident.
"""

import networkx as nx
import pytest

from sphinxcontrib.nexus.query import (
    EXECUTED,
    OBSERVED,
    UNOBSERVED,
    GraphQuery,
)
from sphinxcontrib.nexus.runtime import RuntimeRun, merge_runs


def _graph() -> nx.MultiDiGraph:
    """A class with two methods, a loose function, an attribute, a test.

    Shaped after the measured ORPHEUS case: the CLASS node binds nothing
    (coverage attributes lines, and a `class` statement owns none of its
    methods'), while its methods do.
    """
    g = nx.MultiDiGraph()
    g.add_node("py:class:m.C", type="class", name="m.C", domain="py",
               file_path="/p/m.py", lineno=1, end_lineno=20)
    for leaf, ln in (("ran", 3), ("never_ran", 8)):
        g.add_node(f"py:method:m.C.{leaf}", type="method", name=f"m.C.{leaf}",
                   domain="py", file_path="/p/m.py", lineno=ln, end_lineno=ln + 2)
        g.add_edge("py:class:m.C", f"py:method:m.C.{leaf}", type="contains")
    # An attribute of the same class: `contains` reaches it, but coverage
    # can never bind it, so the descent must not invent evidence for it.
    g.add_node("py:attribute:m.C.field", type="attribute", name="m.C.field",
               domain="py", file_path="/p/m.py", lineno=2, end_lineno=2)
    g.add_edge("py:class:m.C", "py:attribute:m.C.field", type="contains")
    # A class `contains` its docstring's equations too — `[M]` ORPHEUS
    # carries 1 such edge beside 2394 attributes and 4809 methods.
    g.add_node("math:equation:m-C-law", type="equation", name="m-C-law",
               domain="math", docname="theory/x", lineno=4)
    g.add_edge("py:class:m.C", "math:equation:m-C-law", type="contains")
    # A class the capture MEASURED but no test reached: the witness for
    # the OBSERVED half of the lift, which `m.C` cannot supply because
    # one of its methods ran.
    g.add_node("py:class:m.Cold", type="class", name="m.Cold", domain="py",
               file_path="/p/m.py", lineno=25, end_lineno=30)
    g.add_node("py:method:m.Cold.chilly", type="method", name="m.Cold.chilly",
               domain="py", file_path="/p/m.py", lineno=26, end_lineno=27)
    g.add_edge("py:class:m.Cold", "py:method:m.Cold.chilly", type="contains")
    # A class the capture never measured at all.
    g.add_node("py:class:m.Unseen", type="class", name="m.Unseen", domain="py",
               file_path="/p/other.py", lineno=1, end_lineno=5)
    g.add_node("py:method:m.Unseen.meth", type="method", name="m.Unseen.meth",
               domain="py", file_path="/p/other.py", lineno=2, end_lineno=3)
    g.add_edge("py:class:m.Unseen", "py:method:m.Unseen.meth", type="contains")
    g.add_node("py:function:t.test_it", type="function", name="t.test_it",
               domain="py", file_path="/p/t.py", lineno=1, end_lineno=3,
               is_test=True)
    return g


def _run() -> RuntimeRun:
    """`C.ran` executed by the test; `C.never_ran` measured and not run.

    The two together are the discriminator: without `coverage` carrying
    the second, "no test ran it" and "the capture never looked" would be
    the same reply (``lessons-L56``).
    """
    return RuntimeRun(
        name="cov", kind="coverage",
        exercised_by={"py:method:m.C.ran": ["py:function:t.test_it"]},
        coverage={
            "py:method:m.C.ran": {"lines_hit": 2, "lines_total": 2,
                                  "branches_hit": 0, "branches_total": 0,
                                  "missing_arcs": []},
            "py:method:m.C.never_ran": {"lines_hit": 0, "lines_total": 2,
                                        "branches_hit": 0, "branches_total": 0,
                                        "missing_arcs": []},
            "py:method:m.Cold.chilly": {"lines_hit": 0, "lines_total": 2,
                                        "branches_hit": 0, "branches_total": 0,
                                        "missing_arcs": []},
        },
    )


@pytest.fixture
def ledger():
    return GraphQuery(_graph()).execution_ledger(_run())


def test_a_class_is_credited_with_what_its_METHODS_ran(ledger):
    """The witness for the descent, and the reason it must exist.

    `[M]` ORPHEUS 2026-08-18: **0 of 438** production classes bind in
    either context-carrying run, so without this every class-level
    question answers a FALSE ZERO — `PermutationOperator` reports 0
    directly and 95 through 7 of its 8 methods under
    ``geom_ctx,num_ctx``.
    """
    assert ledger.tests_for("py:class:m.C") == {"py:function:t.test_it"}
    assert ledger.state("py:class:m.C") == EXECUTED
    # …and the reply can still say the class itself never bound, which is
    # what lets an auditor see WHY it has evidence.
    assert "py:class:m.C" not in ledger.direct
    assert "py:method:m.C.ran" in ledger.direct


def test_the_three_states_are_distinguishable(ledger):
    """`executed` / `observed` / `unobserved` — and the third is the one
    that must never print as the second. `[M]` 2720 of 2748 ORPHEUS claim
    edges are unadjudicated; read as refutations they would condemn the
    whole suite."""
    assert ledger.state("py:method:m.C.ran") == EXECUTED
    assert ledger.state("py:method:m.C.never_ran") == OBSERVED
    assert ledger.state("py:method:m.Unseen.meth") == UNOBSERVED


def test_an_ATTRIBUTE_has_no_descent_target_and_stays_unobserved(ledger):
    """`contains` reaches it; coverage cannot bind it. `[M]` for
    ``orpheus/numerics/operator.py`` 111 of 255 nodes are kinds with no
    descent target (32 classes' worth of attributes, data, the module),
    so this is the common case and must not be silently reported as
    'nothing ran it'."""
    assert ledger.tests_for("py:attribute:m.C.field") == frozenset()
    assert ledger.state("py:attribute:m.C.field") == UNOBSERVED


def test_a_class_whose_methods_were_never_measured_stays_unobserved(ledger):
    """The descent lifts OBSERVED as well as EXECUTED, so a class is not
    quietly promoted by having members."""
    assert ledger.state("py:class:m.Unseen") == UNOBSERVED


def test_a_class_the_capture_MEASURED_but_no_test_reached_is_observed(ledger):
    """The OBSERVED half of the lift, and it needs its own class.

    `m.C` cannot witness it — one of its methods ran, so it is EXECUTED
    whether or not the OBSERVED lift works at all. Without `m.Cold` this
    arm would be inert: deleting the lift would redden nothing, and a
    whole-guard mutation would have certified it off the EXECUTED arm
    (vv #17's granularity trap).
    """
    assert ledger.state("py:class:m.Cold") == OBSERVED
    assert ledger.tests_for("py:class:m.Cold") == frozenset()


def test_contained_methods_returns_only_what_can_CARRY_evidence():
    """The descent's type filter, gated where it is actually falsifiable.

    Mutating this filter reddens nothing at the LEDGER level, and that is
    not a coverage gap — it is arithmetic: `[M]` a class `contains` only
    methods (4809), attributes (2394) and equations (1) on ORPHEUS, and
    neither of the latter two can ever appear in a capture, so including
    them changes no union. The filter's real job is the helper's
    CONTRACT — a function named `_contained_methods` must not return an
    equation — and keeping the lift's inputs disjoint from its outputs,
    which is what makes the class loop order-independent.

    So the gate lives here rather than on the ledger. Testing it through
    the ledger would have been a gate that cannot fail (vv #17).
    """
    methods = GraphQuery(_graph())._contained_methods("py:class:m.C")
    assert set(methods) == {"py:method:m.C.ran", "py:method:m.C.never_ran"}


def test_the_ledger_never_SHRINKS_a_nodes_raw_exerciser_set():
    """A strict lift: the join may add (descent) and may drop unresolvable
    ids, but a node present in both must not lose a test. Verified on the
    real corpus at 0 of 1936 direct nodes."""
    run, g = _run(), _graph()
    ledger = GraphQuery(g).execution_ledger(run)
    for node_id, tests in run.exercised_by.items():
        in_graph = {t for t in tests if t in g}
        if node_id in g and in_graph:
            assert in_graph <= ledger.tests_for(node_id)


def test_an_exerciser_absent_from_the_GRAPH_is_dropped():
    """A ledger that names ids nothing can resolve cannot be joined to a
    claim, so the unresolvable half is filtered rather than carried."""
    run = RuntimeRun(
        name="cov", kind="coverage",
        exercised_by={
            "py:method:m.C.ran": ["py:function:t.test_it", "py:function:t.gone"],
            "py:function:m.gone": ["py:function:t.test_it"],
        },
    )
    ledger = GraphQuery(_graph()).execution_ledger(run)
    assert ledger.tests_for("py:method:m.C.ran") == {"py:function:t.test_it"}
    assert ledger.tests_for("py:function:m.gone") == frozenset()
    assert ledger.captured_tests == {"py:function:t.test_it"}


def test_is_empty_says_the_run_can_adjudicate_NOTHING():
    """A run with no attribution must be reportable as such. Otherwise a
    consumer joins it to 2748 claims and reports every one refuted — the
    instrument failing in the confident direction (vv #17)."""
    empty = GraphQuery(_graph()).execution_ledger(
        RuntimeRun(name="prof", kind="cprofile"))
    assert empty.is_empty
    assert GraphQuery(_graph()).execution_ledger(_run()).is_empty is False


def test_the_ledger_carries_the_captures_own_INVOCATION():
    """A verdict of 'no test executed this' is only sound for the workload
    that was captured, so the invocation travels with the ledger."""
    run = _run()
    run.meta = {"command": 'tests/geometry -m "not slow"'}
    ledger = GraphQuery(_graph()).execution_ledger(run)
    assert ledger.note == 'tests/geometry -m "not slow"'
    assert ledger.runs == ("cov",)


def test_merging_captures_keeps_EACH_ones_invocation():
    """The half that used to be dropped. ``merged_from`` named which runs
    were unioned; nothing said HOW each was captured — and a union is
    exactly the normal case for a whole-suite ledger, so the qualifier
    disappeared precisely when it was most needed."""
    a = RuntimeRun(name="geom", kind="coverage",
                   exercised_by={"n": ["t1"]}, meta={"command": "tests/geometry"})
    b = RuntimeRun(name="num", kind="coverage",
                   exercised_by={"n": ["t2"]}, meta={"command": "tests/numerics"})
    m = merge_runs([a, b])
    assert m.meta["merged_from"] == ["geom", "num"]
    assert m.meta["command"] == "geom: tests/geometry | num: tests/numerics"


def test_merging_runs_that_carry_no_invocation_adds_no_empty_key():
    """A merged run must not sprout a `command` that says nothing — an
    empty qualifier reads as 'captured with no restrictions'."""
    m = merge_runs([RuntimeRun(name="a", kind="coverage"),
                    RuntimeRun(name="b", kind="coverage")])
    assert "command" not in m.meta
