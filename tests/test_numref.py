"""``:numref:`` crosswalks — figures, tables, and equations (#38, #45).

``:numref:`` names a target by its NUMBER rather than its title, but it
names the same object every other role does. Nexus was minting a
``std:numref:``/``math:numref:`` placeholder beside the real node, so one
figure's references split across two ids and the placeholder showed up as
a dead reference.

Driven by a real ``sphinx-build`` rather than a hand-built graph: the
whole question is what Sphinx's std domain publishes for a ``:name:``-ed
figure, which a fixture guess would have to assume.

ORPHEUS — the corpus every other change in this area was measured on —
contains **zero** ``.. figure::`` or ``.. table::`` directives, so it
cannot exercise this at all. That is why the fixture exists.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from sphinxcontrib.nexus.export import load_sqlite

ROOT = Path(__file__).parent / "roots" / "test-numref"


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    out = tmp_path_factory.mktemp("numref")
    proc = subprocess.run(
        [sys.executable, "-m", "sphinx", "-q", "-E", str(ROOT), str(out)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout

    # A `:numref:` Sphinx itself refuses to resolve would warn, and the
    # edge below would then be missing for a reason no assertion names.
    # Checked against this fixture's own file rather than with ``-W``, so
    # an unrelated upstream deprecation doesn't redden the module.
    noise = [
        line for line in (proc.stdout + proc.stderr).splitlines()
        if "WARNING" in line and "index.rst" in line
    ]
    assert not noise, noise

    db = out / "_nexus" / "graph.db"
    assert db.exists(), f"no graph at {db}"
    return load_sqlite(db).nxgraph


def _refs(graph, source="doc:index"):
    return {
        t for _, t, d in graph.out_edges(source, data=True)
        if d.get("type") in ("references", "equation_ref")
    }


def test_figure_numref_binds_to_its_label(built):
    assert "std:label:fig-mesh" in _refs(built)


def test_table_numref_binds_to_its_label(built):
    assert "std:label:tab-quadrature" in _refs(built)


def test_equation_numref_binds_to_the_equation(built):
    """`:math:numref:` is the math-domain spelling — the #38 case.

    Worth pinning next to the std cases: these are two different roles
    that look alike, and ORPHEUS uses only this one.
    """
    assert "math:equation:transport-balance" in _refs(built)


def test_no_numref_placeholders_survive(built):
    """The twin-node shape the crosswalk exists to prevent."""
    leftovers = [
        n for n in built
        if isinstance(n, str) and ":numref:" in n
    ]
    assert not leftovers, leftovers


def test_the_labels_are_real_nodes_not_phantoms(built):
    """Binding to a placeholder would satisfy the assertions above while
    still being wrong — check what was bound TO."""
    for nid in ("std:label:fig-mesh", "std:label:tab-quadrature"):
        assert built.nodes[nid].get("type") not in ("unresolved", "external")


def test_unknown_numref_target_stays_unresolved(built):
    """The crosswalk is an existence check, not a rewrite.

    A ``:numref:`` naming nothing must not be handed a fabricated label
    node — that would convert a visible unknown into a silent wrong
    binding, which is the failure mode this whole area keeps producing.
    """
    from sphinxcontrib.nexus._mappings import resolve_target_id

    assert resolve_target_id(built, None, "std", "numref", "fig-absent") is None
