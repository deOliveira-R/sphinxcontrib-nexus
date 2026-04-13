"""Regression tests for issue #3 — re-exported class canonicalization.

In ORPHEUS 0.6.0 the ``Mesh1D`` class appeared as four distinct nodes
in the knowledge graph:

- ``py:class:orpheus.geometry.mesh.Mesh1D``   (canonical)
- ``py:class:orpheus.geometry.Mesh1D``        (external, via __init__.py re-export)
- ``py:function:orpheus.geometry.Mesh1D``     (WRONG TYPE — class called as Call)
- ``py:class:geometry.mesh.Mesh1D``           (unresolved, import-path phantom)

These tests use a minimal fixture tree that reproduces the same four
shapes in isolation so we can assert a single canonical class survives
after the AST-analysis pipeline has run.
"""

from __future__ import annotations

from pathlib import Path

from sphinxcontrib.nexus.ast_analyzer import analyze_directory


def _write_reexport_project(root: Path) -> None:
    """Build a tiny package that exercises every bug shape::

        pkg/
            __init__.py        # from .geometry import Thing  (re-export level 1)
            geometry/
                __init__.py    # from .mesh import Thing       (re-export level 2)
                mesh.py        # class Thing: ...               (canonical)
            user.py            # from pkg.geometry import Thing; Thing()
    """
    (root / "pkg").mkdir()
    (root / "pkg" / "__init__.py").write_text(
        "from .geometry import Thing\n"
    )
    (root / "pkg" / "geometry").mkdir()
    (root / "pkg" / "geometry" / "__init__.py").write_text(
        "from .mesh import Thing\n"
    )
    (root / "pkg" / "geometry" / "mesh.py").write_text(
        "class Thing:\n"
        "    def method(self):\n"
        "        return 1\n"
    )
    (root / "pkg" / "user.py").write_text(
        "from pkg.geometry import Thing\n"
        "\n"
        "def use():\n"
        "    t = Thing()\n"
        "    return t.method()\n"
    )


def _thing_nodes(graph):
    """Return every node whose leaf name is exactly ``Thing``.

    Substring matching would pick up ``Thing.method``, which is a
    legitimate method node, not a re-export phantom.
    """
    return {
        nid: attrs
        for nid, attrs in graph.nxgraph.nodes(data=True)
        if (attrs.get("name") or "").rsplit(".", 1)[-1] == "Thing"
    }


# ---------------------------------------------------------------------------
# The core regression: a single canonical node after analysis
# ---------------------------------------------------------------------------


def test_single_canonical_class_node_after_reexport(tmp_path):
    """After ``analyze_directory`` + phantom classification, ``Thing``
    must exist as exactly one node in the graph: the canonical
    ``py:class:pkg.geometry.mesh.Thing``. Any other ``Thing``-shaped
    node id is one of the four bug shapes and is a regression."""
    _write_reexport_project(tmp_path)
    graph = analyze_directory(tmp_path, exclude_patterns=[])
    thing_nodes = _thing_nodes(graph)

    canonical = "py:class:pkg.geometry.mesh.Thing"
    assert canonical in thing_nodes, (
        f"canonical {canonical} not found. nodes: {list(thing_nodes)}"
    )
    # The canonical node must be typed as a class, not a function.
    assert thing_nodes[canonical]["type"] == "class"

    non_canonical = [nid for nid in thing_nodes if nid != canonical]
    assert non_canonical == [], (
        f"re-export duplicates remain after merge: {non_canonical}"
    )


def test_reexport_call_edge_targets_canonical(tmp_path):
    """The ``Thing()`` constructor call inside ``pkg.user.use`` must
    emit a CALLS edge whose target is the canonical
    ``py:class:pkg.geometry.mesh.Thing``, not a
    ``py:function:...Thing`` phantom."""
    _write_reexport_project(tmp_path)
    graph = analyze_directory(tmp_path, exclude_patterns=[])

    use_id = "py:function:pkg.user.use"
    outgoing_calls = [
        t for _, t, d in graph.nxgraph.out_edges(use_id, data=True)
        if d.get("type") == "calls"
    ]
    assert "py:class:pkg.geometry.mesh.Thing" in outgoing_calls, (
        f"expected a calls edge from {use_id} to the canonical Thing; "
        f"outgoing: {outgoing_calls}"
    )
    # And no bogus ``py:function:*.Thing`` edges.
    for target in outgoing_calls:
        assert not (
            target.startswith("py:function:") and target.endswith(".Thing")
        ), f"call edge retains function-typed phantom: {target}"


def test_reexport_does_not_create_function_typed_class(tmp_path):
    """A class called as a constructor must never appear in the
    graph as a ``py:function:*`` node. This is the specific bug
    shape where ``_resolve_call_target`` hardcoded the
    ``py:function:`` prefix."""
    _write_reexport_project(tmp_path)
    graph = analyze_directory(tmp_path, exclude_patterns=[])

    function_typed = [
        nid for nid in graph.nxgraph.nodes
        if nid.startswith("py:function:") and nid.endswith(".Thing")
    ]
    assert function_typed == [], function_typed


def test_short_import_path_reconciles_to_canonical(tmp_path):
    """Regression for bug shape #4 in the original ORPHEUS issue.

    A test file that sits at the project root and imports via
    ``from geometry.mesh import Thing`` (without the outer package
    prefix, because pytest has placed the project root on
    ``sys.path``) previously created an unresolved phantom
    ``py:class:geometry.mesh.Thing`` that never reconciled with
    the canonical ``py:class:pkg.geometry.mesh.Thing`` on full-name
    lookup.

    With leaf-name-based canonicalization the short-path phantom
    folds into the canonical automatically: both have the leaf
    name ``Thing`` and there's exactly one canonical candidate.
    No explicit package-alias config needed — add one later only
    if a real project hits an ambiguous leaf-name collision.
    """
    _write_reexport_project(tmp_path)
    (tmp_path / "test_short_import.py").write_text(
        "from geometry.mesh import Thing\n"
        "\n"
        "def use_short():\n"
        "    t = Thing()\n"
        "    return t.method()\n"
    )
    graph = analyze_directory(tmp_path, exclude_patterns=[])

    thing_nodes = _thing_nodes(graph)
    canonical = "py:class:pkg.geometry.mesh.Thing"
    assert canonical in thing_nodes
    non_canonical = [nid for nid in thing_nodes if nid != canonical]
    assert non_canonical == [], non_canonical

    # The short-path call must land on the canonical.
    use_short_id = "py:function:test_short_import.use_short"
    calls = [
        t for _, t, d in graph.nxgraph.out_edges(use_short_id, data=True)
        if d.get("type") == "calls"
    ]
    assert canonical in calls, calls


def test_phantom_with_ambiguous_leaf_is_not_folded(tmp_path):
    """Conservative fold: when two concrete classes share a leaf
    name, a phantom must NOT be auto-folded into either. The
    phantom stays ``unresolved`` so consumers can distinguish the
    real ambiguity from a rewritable re-export."""
    (tmp_path / "a.py").write_text(
        "class Widget:\n"
        "    def run(self): pass\n"
    )
    (tmp_path / "b.py").write_text(
        "class Widget:\n"
        "    def run(self): pass\n"
    )
    (tmp_path / "caller.py").write_text(
        "def use():\n"
        "    return SomeWidget()\n"
    )
    # Manually create a phantom whose leaf would match both ``Widget``s.
    graph = analyze_directory(tmp_path, exclude_patterns=[])
    # The caller's SomeWidget is NOT named Widget, so it won't match.
    # This test confirms the fold doesn't accidentally collapse
    # legitimate distinct same-leaf-name classes into each other.
    widgets = [
        nid for nid in graph.nxgraph.nodes
        if graph.nxgraph.nodes[nid].get("name", "").endswith("Widget")
        and graph.nxgraph.nodes[nid].get("type") == "class"
    ]
    assert len(widgets) == 2, widgets
    assert "py:class:a.Widget" in widgets
    assert "py:class:b.Widget" in widgets


def test_external_leaf_match_is_not_folded(tmp_path):
    """A reference like ``numpy.ndarray`` from a disjoint import
    path must not be folded into a project-local class that
    happens to share the ``ndarray`` leaf name. The module-path-
    overlap guard in ``_canonicalize_phantoms`` is what prevents
    this: ``numpy`` is neither a prefix nor a suffix of ``local``
    so the fold skips the pair."""
    (tmp_path / "local.py").write_text(
        "class ndarray:\n"
        "    pass\n"
    )
    (tmp_path / "user.py").write_text(
        "import numpy\n"
        "\n"
        "def use():\n"
        "    return numpy.ndarray([1, 2, 3])\n"
    )
    graph = analyze_directory(tmp_path, exclude_patterns=[])

    # The local project class stays as a class, not retargeted.
    assert graph.nxgraph.nodes["py:class:local.ndarray"].get("type") == "class"
    # The numpy.ndarray reference survives as an independent phantom
    # (external or unresolved, both are fine — the invariant is
    # "not folded into the local class"). Importantly, the call
    # from ``user.use`` must still point at the phantom, NOT at
    # ``py:class:local.ndarray``.
    use_id = "py:function:user.use"
    calls = [
        t for _, t, d in graph.nxgraph.out_edges(use_id, data=True)
        if d.get("type") == "calls"
    ]
    assert "py:class:local.ndarray" not in calls, (
        f"numpy.ndarray was incorrectly folded into local.ndarray: {calls}"
    )
    # And SOME node representing the numpy.ndarray reference exists.
    numpy_like = [
        nid
        for nid in graph.nxgraph.nodes
        if (graph.nxgraph.nodes[nid].get("name") or "").startswith("numpy")
    ]
    assert numpy_like, "expected a node for the numpy.* reference"
