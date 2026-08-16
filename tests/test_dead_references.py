"""Dead-reference detection — the silent-drift gate.

The drift shape (observed on ORPHEUS): a class is deleted or renamed,
but theory pages, docstrings, and quoted type annotations still
reference the old dotted path. Sphinx renders those references as plain
text with no warning at any severity. These tests pin the whole
pipeline that makes the graph gate-able on that shape:

1. role-target parsing survives docstring line-wrapping;
2. attributes and module constants exist as graph nodes, so live
   ``:attr:``/``:data:`` references reconcile instead of looking dead;
3. re-export aliases fold public-path phantoms onto defining nodes;
4. project-rooted phantoms are never classified ``external`` just
   because the project is pip-installed in its own build venv;
5. ``GraphQuery.dead_references`` reports what is left, with
   inheritance/re-export rescue passes guarding precision.
"""

from __future__ import annotations

from sphinxcontrib.nexus.ast_analyzer import (
    _chase_reexports,
    _parse_role_target,
    analyze_directory,
)
from sphinxcontrib.nexus.graph import (
    EdgeType,
    GraphEdge,
    GraphNode,
    KnowledgeGraph,
    NodeType,
)
from sphinxcontrib.nexus.query import GraphQuery


# ---------------------------------------------------------------------------
# 1. Role-target parsing under line wrapping
# ---------------------------------------------------------------------------


def test_parse_wrapped_dotted_target():
    """A dotted path wrapped across docstring lines collapses to a
    clean target instead of forging a phantom with a newline in it."""
    raw = "pkg.numerics.coupled_system.\n        CoupledField"
    assert _parse_role_target(raw) == "pkg.numerics.coupled_system.CoupledField"


def test_parse_wrapped_title_target_form():
    """``title <target>`` spanning a line break still yields the
    target between the angle brackets."""
    raw = "pkg.mesh.Mesh.from_material\n        <pkg.mesh.Mesh.from_material>"
    assert _parse_role_target(raw) == "pkg.mesh.Mesh.from_material"


def test_parse_leading_dot_relative_target():
    """Sphinx's ``.relative.target`` convention: dots are display
    syntax, not part of the dotted path."""
    assert _parse_role_target(".Quadrature.product") == "Quadrature.product"


def test_parse_latex_body_untouched():
    """Whitespace inside non-dotted content (inline math) is
    meaningful and must not be collapsed."""
    assert _parse_role_target("x + y") == "x + y"


def test_parse_suppressed_and_tilde_still_work():
    assert _parse_role_target("!pkg.mod.foo") is None
    assert _parse_role_target("~pkg.mod.foo") == "pkg.mod.foo"


# ---------------------------------------------------------------------------
# 2. Attribute / data binding nodes
# ---------------------------------------------------------------------------


def _analyze_source(tmp_path, source: str) -> KnowledgeGraph:
    (tmp_path / "pkg").mkdir(exist_ok=True)
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "mod.py").write_text(source)
    return analyze_directory(tmp_path, exclude_patterns=[])


def test_annotated_class_attribute_becomes_node(tmp_path):
    graph = _analyze_source(
        tmp_path,
        "class Config:\n"
        "    retries: int = 3\n"
        "    name = 'x'\n",
    )
    g = graph.nxgraph
    assert "py:attribute:pkg.mod.Config.retries" in g
    assert "py:attribute:pkg.mod.Config.name" in g
    retries = g.nodes["py:attribute:pkg.mod.Config.retries"]
    assert retries["type"] == NodeType.ATTRIBUTE.value
    assert retries["annotation"] == "int"
    assert retries["file_path"].endswith("mod.py")
    # CONTAINS from the class
    assert any(
        d.get("type") == EdgeType.CONTAINS.value
        for _, _, d in g.edges("py:class:pkg.mod.Config", data=True)
    )


def test_module_constant_becomes_data_node(tmp_path):
    graph = _analyze_source(tmp_path, "LIMIT = 10\nA, B = 1, 2\n")
    g = graph.nxgraph
    for name in ("LIMIT", "A", "B"):
        nid = f"py:data:pkg.mod.{name}"
        assert nid in g
        assert g.nodes[nid]["type"] == NodeType.DATA.value


def test_instance_attribute_in_init_becomes_node(tmp_path):
    graph = _analyze_source(
        tmp_path,
        "class Solver:\n"
        "    def __init__(self):\n"
        "        self.mesh = None\n"
        "        self.tol, self.max_iter = 1e-6, 100\n",
    )
    g = graph.nxgraph
    for attr in ("mesh", "tol", "max_iter"):
        assert f"py:attribute:pkg.mod.Solver.{attr}" in g


def test_annotated_and_init_assignment_collapse_to_one_node(tmp_path):
    graph = _analyze_source(
        tmp_path,
        "class Solver:\n"
        "    tol: float\n"
        "    def __init__(self):\n"
        "        self.tol = 1e-6\n",
    )
    g = graph.nxgraph
    nodes = [n for n in g.nodes if n == "py:attribute:pkg.mod.Solver.tol"]
    assert len(nodes) == 1
    # The annotated declaration won (it was emitted first, with the
    # annotation recorded).
    assert g.nodes[nodes[0]]["annotation"] == "float"


def test_docstring_math_label_defines_equation_node(tmp_path):
    """A ``.. math:: :label:`` block in a docstring DEFINES the label.
    Sphinx only sees labels on rendered pages, so without AST-side
    extraction every ``:eq:`` reference to a label defined in an
    un-rendered docstring looks dead."""
    graph = _analyze_source(
        tmp_path,
        'def derive():\n'
        '    r"""The derivation.\n'
        '\n'
        '    .. math::\n'
        '       :label: my-neat-identity\n'
        '\n'
        '       a^2 + b^2 = c^2\n'
        '\n'
        '    Used via :eq:`my-neat-identity` and :eq:`gone-label`.\n'
        '    """\n',
    )
    g = graph.nxgraph
    assert g.nodes["math:equation:my-neat-identity"]["type"] \
        == NodeType.EQUATION.value

    result = GraphQuery(graph).dead_references()
    dead_names = {d.target_name for d in result.dead}
    assert "my-neat-identity" not in dead_names
    assert "gone-label" in dead_names


def test_module_docstring_label_and_refs_are_extracted(tmp_path):
    """Module-level docstrings were never scanned at all — a label
    defined in module prose (the ORPHEUS derivation-module shape) made
    every reference to it look dead."""
    graph = _analyze_source(
        tmp_path,
        '"""Derivation module.\n'
        '\n'
        '.. math::\n'
        '   :label: module-level-identity\n'
        '\n'
        '   e = mc^2\n'
        '\n'
        'See :class:`pkg.mod.Config`.\n'
        '"""\n'
        '\n'
        'def use():\n'
        '    """Applies :eq:`module-level-identity`."""\n'
        '\n'
        'class Config:\n'
        '    pass\n',
    )
    g = graph.nxgraph
    assert g.nodes["math:equation:module-level-identity"]["type"] \
        == NodeType.EQUATION.value
    # The module docstring's :class: reference became an edge too.
    assert any(
        d.get("type") == EdgeType.REFERENCES.value
        and tgt == "py:class:pkg.mod.Config"
        for _, tgt, d in g.out_edges("py:module:pkg.mod", data=True)
    )
    result = GraphQuery(graph).dead_references()
    assert "module-level-identity" not in {d.target_name for d in result.dead}


# ---------------------------------------------------------------------------
# 3. Re-export aliases
# ---------------------------------------------------------------------------


def test_chase_reexports_follows_chains_and_prefixes():
    reexports = {
        "pkg.Thing": "pkg.geometry.Thing",
        "pkg.geometry.Thing": "pkg.geometry.mesh.Thing",
    }
    assert _chase_reexports("pkg.Thing", reexports) == "pkg.geometry.mesh.Thing"
    # Prefix rewriting: a member accessed through the alias.
    assert (
        _chase_reexports("pkg.Thing.method", reexports)
        == "pkg.geometry.mesh.Thing.method"
    )


def test_nested_package_relative_reexport_resolves_fully(tmp_path):
    """Regression: ``from .directional import Quadrature`` inside
    ``pkg/numerics/quadrature/__init__.py`` must resolve against the
    package ITSELF, not its parent — the parent-anchored resolution
    dropped one segment from every re-export value in a nested
    package (observed on ORPHEUS: 2,326 poisoned aliases)."""
    root = tmp_path / "pkg" / "numerics" / "quadrature"
    root.mkdir(parents=True)
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "numerics" / "__init__.py").write_text("")
    (root / "__init__.py").write_text("from .directional import Quadrature\n")
    (root / "directional.py").write_text("class Quadrature:\n    pass\n")
    graph = analyze_directory(tmp_path, exclude_patterns=[])
    assert graph.metadata["reexports"]["pkg.numerics.quadrature.Quadrature"] \
        == "pkg.numerics.quadrature.directional.Quadrature"


def test_subscripted_generic_base_gets_inherits_edge(tmp_path):
    """Regression: ``class Full(Composite[A, B])`` — a Subscript base
    — must still produce the INHERITS edge, or inherited-member
    resolution breaks for every Generic-parameterized class."""
    graph = _analyze_source(
        tmp_path,
        "from typing import Generic, TypeVar\n"
        "T = TypeVar('T')\n"
        "class Composite(Generic[T]):\n"
        "    def __mul__(self, s):\n"
        "        return self\n"
        "    def scale(self, s):\n"
        "        return self\n"
        "class Full(Composite[int]):\n"
        "    pass\n",
    )
    g = graph.nxgraph
    inherits = {
        tgt for _, tgt, d in g.out_edges("py:class:pkg.mod.Full", data=True)
        if d.get("type") == EdgeType.INHERITS.value
    }
    assert "py:class:pkg.mod.Composite" in inherits


def test_inherited_member_through_generic_base_is_live(tmp_path):
    graph = _analyze_source(
        tmp_path,
        "from typing import Generic, TypeVar\n"
        "T = TypeVar('T')\n"
        "class Composite(Generic[T]):\n"
        "    def scale(self, s):\n"
        "        return self\n"
        "class Full(Composite[int]):\n"
        '    """See :meth:`pkg.mod.Full.scale` and\n'
        '    :meth:`pkg.mod.Full.__mul__` and\n'
        '    :meth:`pkg.mod.Full.vanished`."""\n',
    )
    result = GraphQuery(graph).dead_references()
    dead_names = {d.target_name for d in result.dead}
    # ``scale`` lives on the generic base; ``__mul__`` is an object-
    # provided dunder on an existing class; ``vanished`` is genuinely
    # missing — and typing.Generic must NOT make it undecidable.
    assert "pkg.mod.Full.scale" not in dead_names
    assert "pkg.mod.Full.__mul__" not in dead_names
    assert "pkg.mod.Full.vanished" in dead_names


def test_chase_reexports_survives_cycles():
    reexports = {"a.X": "b.X", "b.X": "a.X"}
    # Must terminate; landing on either side of the cycle is fine.
    assert _chase_reexports("a.X", reexports) in ("a.X", "b.X")


def test_reexport_map_collected_and_persisted(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text(
        "from .core import Thing\n"
    )
    (tmp_path / "pkg" / "core.py").write_text("class Thing:\n    pass\n")
    graph = analyze_directory(tmp_path, exclude_patterns=[])
    assert graph.metadata["reexports"]["pkg.Thing"] == "pkg.core.Thing"


def test_nonoverlapping_reexport_phantom_folds(tmp_path):
    """The shape the leaf/module-path-overlap fold cannot catch:
    ``pkg.api`` re-exports from ``pkg.core.thing`` — the module paths
    neither prefix nor suffix each other, so only the alias map can
    prove ``pkg.api.Thing`` is the same symbol."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "api").mkdir()
    (tmp_path / "pkg" / "api" / "__init__.py").write_text(
        "from pkg.core.thing import Thing\n"
    )
    (tmp_path / "pkg" / "core").mkdir()
    (tmp_path / "pkg" / "core" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "core" / "thing.py").write_text(
        "class Thing:\n    pass\n"
    )
    (tmp_path / "pkg" / "user.py").write_text(
        'def use():\n'
        '    """Uses :class:`pkg.api.Thing`."""\n'
        '    return None\n'
    )
    graph = analyze_directory(tmp_path, exclude_patterns=[])
    g = graph.nxgraph
    # The phantom folded onto the defining node…
    assert "py:class:pkg.api.Thing" not in g
    # …and the docstring reference follows it there.
    ref_targets = {
        tgt for _, tgt, d in g.out_edges("py:function:pkg.user.use", data=True)
        if d.get("type") == EdgeType.REFERENCES.value
    }
    assert "py:class:pkg.core.thing.Thing" in ref_targets


# ---------------------------------------------------------------------------
# 4. Project-rooted names never classify as external
# ---------------------------------------------------------------------------


def test_project_rooted_phantom_never_external(tmp_path, monkeypatch):
    """The project is usually pip-installed in its own build venv, so
    the installed-packages check alone would classify the project's own
    dangling references as ``external`` and hide them from gating."""
    import sphinxcontrib.nexus.extractors as extractors

    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "mod.py").write_text(
        'def f():\n'
        '    """See :class:`pkg.deleted.Gone`."""\n'
        '    return None\n'
    )
    # Simulate the project being importable in the build environment.
    monkeypatch.setattr(
        extractors,
        "_EXTERNAL_NAMES",
        frozenset(extractors._EXTERNAL_NAMES | {"pkg"}),
    )
    graph = analyze_directory(tmp_path, exclude_patterns=[])
    attrs = graph.nxgraph.nodes["py:class:pkg.deleted.Gone"]
    assert attrs["type"] == NodeType.UNRESOLVED.value


# ---------------------------------------------------------------------------
# 5. dead_references query
# ---------------------------------------------------------------------------


def _node(graph, nid, ntype, name, **meta):
    graph.add_node(GraphNode(
        id=nid, type=ntype, name=name, display_name=name.rsplit(".", 1)[-1],
        domain="py", metadata=meta,
    ))


def _build_query_fixture() -> KnowledgeGraph:
    kg = KnowledgeGraph()
    _node(kg, "py:module:pkg", NodeType.MODULE, "pkg", file_path="/x/pkg/__init__.py")
    _node(kg, "py:class:pkg.Base", NodeType.CLASS, "pkg.Base", file_path="/x/pkg/b.py")
    _node(kg, "py:attribute:pkg.Base.attr", NodeType.ATTRIBUTE, "pkg.Base.attr",
          file_path="/x/pkg/b.py")
    _node(kg, "py:class:pkg.Sub", NodeType.CLASS, "pkg.Sub", file_path="/x/pkg/s.py")
    kg.add_edge(GraphEdge(source="py:class:pkg.Sub", target="py:class:pkg.Base",
                          type=EdgeType.INHERITS))
    _node(kg, "py:class:pkg.SubExt", NodeType.CLASS, "pkg.SubExt",
          file_path="/x/pkg/s.py")
    _node(kg, "py:class:numpy.ndarray", NodeType.EXTERNAL, "numpy.ndarray")
    kg.add_edge(GraphEdge(source="py:class:pkg.SubExt",
                          target="py:class:numpy.ndarray",
                          type=EdgeType.INHERITS))
    _node(kg, "py:class:pkg.mod.Thing", NodeType.CLASS, "pkg.mod.Thing",
          file_path="/x/pkg/mod.py")
    kg.metadata["reexports"] = {"pkg.Alias": "pkg.mod.Thing"}

    # The doc page holding the references.
    kg.add_node(GraphNode(id="std:file:page", type=NodeType.FILE, name="page",
                          display_name="page", domain="std", docname="page"))

    # Phantom targets.
    _node(kg, "py:class:pkg.Gone", NodeType.UNRESOLVED, "pkg.Gone")
    _node(kg, "py:attribute:pkg.Sub.attr", NodeType.UNRESOLVED, "pkg.Sub.attr")
    _node(kg, "py:attribute:pkg.SubExt.attr", NodeType.UNRESOLVED,
          "pkg.SubExt.attr")
    _node(kg, "py:class:pkg.Alias", NodeType.UNRESOLVED, "pkg.Alias")
    _node(kg, "py:class:numpy.gone", NodeType.EXTERNAL, "numpy.gone")
    kg.add_node(GraphNode(id="math:equation:gone-label", type=NodeType.UNRESOLVED,
                          name="gone-label", display_name="gone-label",
                          domain="math"))
    kg.add_node(GraphNode(id="math:equation:x_i", type=NodeType.UNRESOLVED,
                          name="x_i", display_name="x_i", domain="math"))
    kg.add_node(GraphNode(id="cite:citation:Bell1970", type=NodeType.CITATION,
                          name="Bell1970", display_name="Bell1970",
                          domain="cite"))

    def ref(target, edge_type=EdgeType.DOCUMENTS, **meta):
        kg.add_edge(GraphEdge(source="std:file:page", target=target, type=edge_type,
                              metadata=meta))

    ref("py:class:pkg.Gone")                                   # dead
    ref("py:attribute:pkg.Sub.attr")                           # rescued: inherited
    ref("py:attribute:pkg.SubExt.attr")                        # undecidable: opaque base
    ref("py:class:pkg.Alias")                                  # rescued: re-export
    ref("py:class:numpy.gone")                                 # skipped: external root
    ref("math:equation:gone-label", EdgeType.EQUATION_REF)     # dead equation
    ref("math:equation:x_i", EdgeType.REFERENCES, reftype="math")  # inline math, skipped
    ref("cite:citation:Bell1970", EdgeType.CITES)              # citations: not a phantom type
    return kg


def test_inherited_member_via_public_reexport_path_is_live():
    """Regression: ``pkg.Sink.zeros_on`` where ``pkg.Sink`` is a
    re-export of ``pkg.impl.Sink`` AND ``zeros_on`` is only defined on
    that class's base — the inheritance walk must run on the chased
    spelling, not just the name as written."""
    kg = KnowledgeGraph()
    _node(kg, "py:module:pkg", NodeType.MODULE, "pkg", file_path="/x/p/__init__.py")
    _node(kg, "py:class:pkg.impl.Sink", NodeType.CLASS, "pkg.impl.Sink",
          file_path="/x/p/impl.py")
    _node(kg, "py:class:pkg.impl.BaseField", NodeType.CLASS, "pkg.impl.BaseField",
          file_path="/x/p/impl.py")
    _node(kg, "py:method:pkg.impl.BaseField.zeros_on", NodeType.METHOD,
          "pkg.impl.BaseField.zeros_on", file_path="/x/p/impl.py")
    kg.add_edge(GraphEdge(source="py:class:pkg.impl.Sink",
                          target="py:class:pkg.impl.BaseField",
                          type=EdgeType.INHERITS))
    kg.metadata["reexports"] = {"pkg.Sink": "pkg.impl.Sink"}
    kg.add_node(GraphNode(id="std:file:page", type=NodeType.FILE, name="page",
                          display_name="page", domain="std", docname="page"))
    _node(kg, "py:method:pkg.Sink.zeros_on", NodeType.UNRESOLVED,
          "pkg.Sink.zeros_on")
    kg.add_edge(GraphEdge(source="std:file:page", target="py:method:pkg.Sink.zeros_on",
                          type=EdgeType.DOCUMENTS))

    result = GraphQuery(kg).dead_references()
    assert result.total_dead == 0
    assert result.rescued == 1


def test_dead_references_verdicts():
    q = GraphQuery(_build_query_fixture())
    result = q.dead_references()

    dead_names = {d.target_name for d in result.dead}
    assert dead_names == {"pkg.Gone", "gone-label"}
    assert result.total_dead == 2
    assert result.rescued == 2          # inherited attr + re-export alias
    assert result.undecidable == 1      # opaque external base
    assert result.total_checked == 5    # everything project-decidable
    assert "pkg" in result.project_modules

    by_name = {d.target_name: d for d in result.dead}
    assert by_name["pkg.Gone"].kind == "python"
    assert by_name["gone-label"].kind == "equation"
    assert by_name["pkg.Gone"].sites[0].source.id == "std:file:page"


def test_staleness_carries_dead_references():
    q = GraphQuery(_build_query_fixture())
    result = q.staleness()  # no project_root: timestamp part skipped
    assert result.total_dead_references == 2
    assert {d.target_name for d in result.dead_references} == {
        "pkg.Gone", "gone-label",
    }


def test_dead_references_end_to_end(tmp_path):
    """Full pipeline: source → AST analysis → canonicalization →
    query. One genuinely dead reference survives; live references to
    an annotated attribute, a re-exported class, and a line-wrapped
    target are all rescued."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text(
        "from .core import Thing\n"
    )
    (tmp_path / "pkg" / "core.py").write_text(
        'class Thing:\n'
        '    """A thing.\n'
        '\n'
        '    See :class:`pkg.deleted.Gone` for history, and\n'
        '    :attr:`pkg.core.Config.retries` plus :class:`pkg.Thing`\n'
        '    and the wrapped :class:`pkg.core.\n'
        '        Config` reference.\n'
        '    """\n'
        '\n'
        '    limit: int = 3\n'
        '\n'
        '\n'
        'class Config:\n'
        '    retries: int = 5\n'
    )
    graph = analyze_directory(tmp_path, exclude_patterns=[])
    result = GraphQuery(graph).dead_references()
    dead_names = {d.target_name for d in result.dead}
    assert dead_names == {"pkg.deleted.Gone"}


# ---------------------------------------------------------------------------
# 6. CLI text mode — the payload that gets PUSHED into an agent's context
# ---------------------------------------------------------------------------


def test_cli_text_mode_leads_with_the_imperative(tmp_path, capsys):
    """This text is read by an agent that did not ask for it, so it must
    state what the finding IS and what to do about it before any detail.
    A bare list of names reads as trivia and gets skimmed past."""
    import argparse

    from sphinxcontrib.nexus.cli import _run_dead_references
    from sphinxcontrib.nexus.export import write_sqlite

    kg = _build_query_fixture()
    db = tmp_path / "graph.db"
    write_sqlite(kg, db)

    code = _run_dead_references(argparse.Namespace(
        db=db, limit=50, format="text",
        quiet_when_clean=False, exit_code=False,
    ))
    out = capsys.readouterr().out
    assert code == 0
    assert out.startswith("DEAD DOCUMENTATION REFERENCES")
    assert "no warning" in out          # why nothing else caught it
    assert "pkg.Gone" in out


def test_cli_quiet_when_clean_costs_zero_context(tmp_path, capsys):
    """A clean project must print nothing, or the channel trains agents
    to skim past it on the day it matters."""
    import argparse

    from sphinxcontrib.nexus.cli import _run_dead_references
    from sphinxcontrib.nexus.export import write_sqlite
    from sphinxcontrib.nexus.graph import KnowledgeGraph

    db = tmp_path / "clean.db"
    write_sqlite(KnowledgeGraph(), db)

    _run_dead_references(argparse.Namespace(
        db=db, limit=50, format="text",
        quiet_when_clean=True, exit_code=False,
    ))
    assert capsys.readouterr().out == ""


def test_cli_exit_code_gates_ci(tmp_path, capsys):
    import argparse

    from sphinxcontrib.nexus.cli import _run_dead_references
    from sphinxcontrib.nexus.export import write_sqlite

    db = tmp_path / "graph.db"
    write_sqlite(_build_query_fixture(), db)
    code = _run_dead_references(argparse.Namespace(
        db=db, limit=50, format="text",
        quiet_when_clean=False, exit_code=True,
    ))
    capsys.readouterr()
    assert code == 1


# ---------------------------------------------------------------------------
# Suffix-match ranking (issue #36)
# ---------------------------------------------------------------------------
#
# The shape that produced a 46 % false-positive rate on ORPHEUS: a
# retired module path survives in prototype/archive source, so the AST
# pass mints ``py:function:orpheus.derivations.peierls_geometry.
# compute_G_bc`` as a placeholder — while the live definition sits at
# ``...peierls_nystrom.geometry.compute_G_bc``. A bare ``:func:`` role
# suffix-matches BOTH. Picking the placeholder turns a live symbol into
# a reported dead reference; the graph invents the drift it then flags.


class _ObjType:
    def __init__(self, *roles: str) -> None:
        self.roles = roles


class _StubPyDomain:
    """The slice of ``PythonDomain`` the resolver reads.

    ``resolve_target_id`` widens a reftype ("func") into candidate
    obj_types ("function") through ``domain.object_types``. Passing
    ``None`` would leave only the literal reftype, and ``py:func:``
    matches no node — so the stub is what makes these tests exercise
    the suffix path at all.
    """

    object_types = {
        "function": _ObjType("func", "obj"),
        "class": _ObjType("class", "exc", "obj"),
        "method": _ObjType("meth", "obj"),
    }


def _ranking_graph() -> KnowledgeGraph:
    kg = KnowledgeGraph()
    _node(kg, "py:function:pkg.retired.compute_G_bc", NodeType.UNRESOLVED,
          "pkg.retired.compute_G_bc")
    _node(kg, "py:function:pkg.live.geometry.compute_G_bc", NodeType.FUNCTION,
          "pkg.live.geometry.compute_G_bc")
    return kg


def test_suffix_match_prefers_definition_over_placeholder():
    from sphinxcontrib.nexus._mappings import resolve_target_id

    resolved = resolve_target_id(
        _ranking_graph().nxgraph, _StubPyDomain(), "py", "func", "compute_G_bc",
    )
    assert resolved == "py:function:pkg.live.geometry.compute_G_bc"


def test_suffix_match_ranking_ignores_insertion_order():
    """The placeholder inserted FIRST must still lose.

    The pre-fix resolver returned whichever candidate ``nxgraph``
    yielded first, so the answer rode on build order.
    """
    from sphinxcontrib.nexus._mappings import resolve_target_id

    kg = KnowledgeGraph()
    _node(kg, "py:function:a.retired.thing", NodeType.UNRESOLVED,
          "a.retired.thing")
    _node(kg, "py:function:z.live.thing", NodeType.FUNCTION, "z.live.thing")
    assert resolve_target_id(kg.nxgraph, _StubPyDomain(), "py", "func", "thing") == (
        "py:function:z.live.thing"
    )


def test_suffix_match_still_resolves_when_only_placeholder_exists():
    """Ranking must not become a filter.

    A placeholder is still the best available answer when nothing real
    shares the suffix — dropping to ``None`` here would mint a SECOND
    phantom rather than reuse the one already standing.
    """
    from sphinxcontrib.nexus._mappings import resolve_target_id

    kg = KnowledgeGraph()
    _node(kg, "py:function:pkg.retired.only", NodeType.UNRESOLVED,
          "pkg.retired.only")
    assert resolve_target_id(kg.nxgraph, _StubPyDomain(), "py", "func", "only") == (
        "py:function:pkg.retired.only"
    )


def test_suffix_match_breaks_ties_on_shortest_path():
    """Two live definitions: deterministic, and the shallower wins."""
    from sphinxcontrib.nexus._mappings import resolve_target_id

    kg = KnowledgeGraph()
    _node(kg, "py:function:pkg.deep.nested.here.solve", NodeType.FUNCTION,
          "pkg.deep.nested.here.solve")
    _node(kg, "py:function:pkg.solve", NodeType.FUNCTION, "pkg.solve")
    assert resolve_target_id(kg.nxgraph, _StubPyDomain(), "py", "func", "solve") == (
        "py:function:pkg.solve"
    )


def test_exact_placeholder_does_not_shadow_real_definition():
    """The bare tombstone must not win by being exact.

    On ORPHEUS a bare ``:func:`compute_G_bc`` had minted
    ``py:function:compute_G_bc``; the exact-match lookup returned it
    before suffix ranking ever ran, so the live definition was
    unreachable and the reference reported dead.
    """
    from sphinxcontrib.nexus._mappings import resolve_target_id

    kg = KnowledgeGraph()
    _node(kg, "py:function:compute_G_bc", NodeType.UNRESOLVED, "compute_G_bc")
    _node(kg, "py:function:pkg.live.geometry.compute_G_bc", NodeType.FUNCTION,
          "pkg.live.geometry.compute_G_bc")
    assert resolve_target_id(
        kg.nxgraph, _StubPyDomain(), "py", "func", "compute_G_bc",
    ) == "py:function:pkg.live.geometry.compute_G_bc"


def test_exact_placeholder_wins_over_suffix_placeholder():
    """With nothing real anywhere, prefer the name as written.

    Reusing the exact tombstone keeps one phantom per missing symbol
    instead of splitting references across two.
    """
    from sphinxcontrib.nexus._mappings import resolve_target_id

    kg = KnowledgeGraph()
    _node(kg, "py:function:widget", NodeType.UNRESOLVED, "widget")
    _node(kg, "py:function:pkg.retired.widget", NodeType.UNRESOLVED,
          "pkg.retired.widget")
    assert resolve_target_id(
        kg.nxgraph, _StubPyDomain(), "py", "func", "widget",
    ) == "py:function:widget"


def test_exact_real_match_still_short_circuits():
    """The fast path survives: an exact hit on a real definition wins
    outright, without paying for a graph scan."""
    from sphinxcontrib.nexus._mappings import resolve_target_id

    kg = KnowledgeGraph()
    _node(kg, "py:function:pkg.mod.solve", NodeType.FUNCTION, "pkg.mod.solve")
    _node(kg, "py:function:other.solve", NodeType.FUNCTION, "other.solve")
    assert resolve_target_id(
        kg.nxgraph, _StubPyDomain(), "py", "func", "pkg.mod.solve",
    ) == "py:function:pkg.mod.solve"


# ---------------------------------------------------------------------------
# One ranking, three resolvers
# ---------------------------------------------------------------------------
#
# Reference resolution happens in three passes at three moments (Sphinx
# pending_xref, post-merge phantom fold, merge-time type conflict). They
# once carried three rank tables — two byte-identical copies and a binary
# real-vs-placeholder test. Divergence between them is how a graph starts
# disagreeing with itself about what a name refers to.


def test_rank_tables_are_not_duplicated():
    """The table lives in _mappings and nowhere else."""
    import inspect

    from sphinxcontrib.nexus import ast_analyzer, merge

    for module in (ast_analyzer, merge):
        src = inspect.getsource(module)
        assert "NodeType.UNRESOLVED.value: 13" not in src, (
            f"{module.__name__} redeclares the concreteness ranking; "
            f"import TYPE_RANK from _mappings instead"
        )


def test_all_three_passes_share_one_ranking():
    from sphinxcontrib.nexus import merge
    from sphinxcontrib.nexus._mappings import TYPE_RANK
    from sphinxcontrib.nexus.ast_analyzer import _canonical_rank_key

    # The phantom fold delegates rather than reimplementing.
    key = _canonical_rank_key("py:class:pkg.Thing", "pkg.Thing",
                              {"type": "class", "file_path": "/x.py"})
    assert key[2] == TYPE_RANK["class"]
    assert key[3] == 0  # file-backed
    # merge_graphs consults the same object, not a copy.
    assert merge.TYPE_RANK is TYPE_RANK


def test_placeholder_ranks_below_every_real_type():
    from sphinxcontrib.nexus._mappings import PLACEHOLDER_TYPES, TYPE_RANK

    worst_real = max(
        rank for ntype, rank in TYPE_RANK.items()
        if ntype not in PLACEHOLDER_TYPES
    )
    assert all(
        TYPE_RANK[ntype] > worst_real for ntype in PLACEHOLDER_TYPES
    ), "a placeholder must never outrank a real definition"


def test_ambiguity_is_decided_on_kind_not_tiebreak():
    """Name length and node id order the sort; they don't justify it."""
    from sphinxcontrib.nexus._mappings import (
        candidate_rank,
        candidates_are_ambiguous,
    )

    attrs = {"type": "module", "file_path": "/x.py"}
    # Three real modules sharing a leaf — the ORPHEUS `derivations` case.
    tied = sorted(
        candidate_rank(f"py:module:{n}", n, attrs)
        for n in ("orpheus.derivations", "tests.derivations",
                  "orpheus.deep.origins.derivations")
    )
    assert candidates_are_ambiguous(tied)

    # A real definition against a placeholder is NOT ambiguous.
    decided = sorted([
        candidate_rank("py:module:a.thing", "a.thing", attrs),
        candidate_rank("py:module:thing", "thing", {"type": "unresolved"}),
    ])
    assert not candidates_are_ambiguous(decided)
    assert decided[0][-1] == "py:module:a.thing"


def test_single_candidate_is_never_ambiguous():
    from sphinxcontrib.nexus._mappings import (
        candidate_rank,
        candidates_are_ambiguous,
    )

    assert not candidates_are_ambiguous([])
    assert not candidates_are_ambiguous(
        [candidate_rank("py:class:pkg.A", "pkg.A", {"type": "class"})]
    )


# ---------------------------------------------------------------------------
# Relative reference resolution (issue #37)
# ---------------------------------------------------------------------------
#
# Half of a real project's py-domain references are relative
# (:meth:`Quadrature.product`, :class:`SNMesh`, :meth:`apply`) and Sphinx
# resolves them against the current module and class. The graph already
# knows each reference's source node, so that context is present — it
# just was not consulted.


def _ns_graph() -> KnowledgeGraph:
    """A module with a class, both defining an `apply`."""
    kg = KnowledgeGraph()

    def mod(name):
        kg.add_node(GraphNode(id=f"py:module:{name}", type=NodeType.MODULE,
                              name=name, display_name=name.rsplit(".", 1)[-1],
                              domain="py", metadata={"file_path": f"/{name}.py"}))

    def sym(kind, ntype, name, parent):
        nid = f"py:{kind}:{name}"
        kg.add_node(GraphNode(id=nid, type=ntype, name=name,
                              display_name=name.rsplit(".", 1)[-1], domain="py",
                              metadata={"file_path": "/x.py"}))
        kg.add_edge(GraphEdge(source=parent, target=nid, type=EdgeType.CONTAINS))
        return nid

    mod("pkg.alpha")
    mod("pkg.beta")
    sym("class", NodeType.CLASS, "pkg.alpha.Solver", "py:module:pkg.alpha")
    sym("method", NodeType.METHOD, "pkg.alpha.Solver.apply",
        "py:class:pkg.alpha.Solver")
    sym("class", NodeType.CLASS, "pkg.beta.Filter", "py:module:pkg.beta")
    sym("method", NodeType.METHOD, "pkg.beta.Filter.apply",
        "py:class:pkg.beta.Filter")
    return kg


def _phantom_ref(kg, source_id, phantom_id, name):
    if phantom_id not in kg.nxgraph:
        kg.add_node(GraphNode(id=phantom_id, type=NodeType.UNRESOLVED,
                              name=name, display_name=name, domain="py"))
    kg.add_edge(GraphEdge(source=source_id, target=phantom_id,
                          type=EdgeType.REFERENCES))


def test_same_bare_name_resolves_differently_per_namespace():
    """The load-bearing case: one phantom, two correct answers.

    Measured on ORPHEUS: 532 bare phantoms have more than one distinct
    referrer, and `:meth:`apply`` alone is referenced from 132. Folding
    the shared NODE would force one answer on all of them — only the
    edge carries the namespace that decides.
    """
    from sphinxcontrib.nexus.ast_analyzer import _resolve_relative_references

    kg = _ns_graph()
    _phantom_ref(kg, "py:method:pkg.alpha.Solver.apply", "py:function:apply", "apply")
    _phantom_ref(kg, "py:method:pkg.beta.Filter.apply", "py:function:apply", "apply")

    assert _resolve_relative_references(kg) == 2
    g = kg.nxgraph
    out = lambda s: {t for _, t, d in g.out_edges(s, data=True)
                     if d.get("type") == EdgeType.REFERENCES.value}
    assert out("py:method:pkg.alpha.Solver.apply") == {"py:method:pkg.alpha.Solver.apply"}
    assert out("py:method:pkg.beta.Filter.apply") == {"py:method:pkg.beta.Filter.apply"}
    # Fully dereferenced, so the phantom is gone.
    assert "py:function:apply" not in g


def test_class_namespace_beats_module_namespace():
    """find_obj tries modname.classname.target before modname.target."""
    from sphinxcontrib.nexus.ast_analyzer import _resolve_relative_references

    kg = _ns_graph()
    # A module-level `apply` competing with the class's.
    kg.add_node(GraphNode(id="py:function:pkg.alpha.apply", type=NodeType.FUNCTION,
                          name="pkg.alpha.apply", display_name="apply", domain="py",
                          metadata={"file_path": "/x.py"}))
    kg.add_edge(GraphEdge(source="py:module:pkg.alpha",
                          target="py:function:pkg.alpha.apply",
                          type=EdgeType.CONTAINS))
    _phantom_ref(kg, "py:method:pkg.alpha.Solver.apply", "py:function:apply", "apply")

    _resolve_relative_references(kg)
    targets = {t for _, t, d in kg.nxgraph.out_edges(
        "py:method:pkg.alpha.Solver.apply", data=True)
        if d.get("type") == EdgeType.REFERENCES.value}
    assert targets == {"py:method:pkg.alpha.Solver.apply"}


def test_bare_name_is_looked_up_as_a_full_key():
    """The counterintuitive rule, encoded.

    The domain registry is keyed by full dotted names, so a bare
    `:func:`helper`` resolves only if a TOP-LEVEL `helper` exists — not
    merely because its module was automodule'd. This is why "add autodoc
    coverage" cannot fix relative references.
    """
    from sphinxcontrib.nexus.ast_analyzer import _resolve_relative_references

    kg = _ns_graph()
    # `zzz` exists nowhere in pkg.alpha's namespace and has no top-level
    # definition — it must stay unresolved.
    _phantom_ref(kg, "py:module:pkg.alpha", "py:function:zzz", "zzz")
    assert _resolve_relative_references(kg) == 0
    assert "py:function:zzz" in kg.nxgraph

    # Now give it a genuine top-level definition: step 3 finds it.
    kg2 = _ns_graph()
    kg2.add_node(GraphNode(id="py:function:zzz", type=NodeType.UNRESOLVED,
                           name="zzz", display_name="zzz", domain="py"))
    kg2.add_node(GraphNode(id="py:function:toplevel", type=NodeType.FUNCTION,
                           name="toplevel", display_name="toplevel", domain="py",
                           metadata={"file_path": "/t.py"}))
    kg2.add_edge(GraphEdge(source="py:module:pkg.alpha",
                           target="py:function:toplevel_ref",
                           type=EdgeType.REFERENCES))
    kg2.add_node(GraphNode(id="py:function:toplevel_ref", type=NodeType.UNRESOLVED,
                           name="toplevel", display_name="toplevel", domain="py"))
    assert _resolve_relative_references(kg2) == 1
    targets = {t for _, t, d in kg2.nxgraph.out_edges("py:module:pkg.alpha", data=True)
               if d.get("type") == EdgeType.REFERENCES.value}
    assert "py:function:toplevel" in targets


def test_phantom_survives_while_any_referrer_still_needs_it():
    """Partial resolution must not delete the node out from under the rest."""
    from sphinxcontrib.nexus.ast_analyzer import _resolve_relative_references

    kg = _ns_graph()
    # One referrer can resolve it; one has no namespace at all.
    kg.add_node(GraphNode(id="std:file:page", type=NodeType.FILE, name="page",
                          display_name="page", domain="std"))
    _phantom_ref(kg, "py:method:pkg.alpha.Solver.apply", "py:function:apply", "apply")
    _phantom_ref(kg, "std:file:page", "py:function:apply", "apply")

    assert _resolve_relative_references(kg) == 1
    g = kg.nxgraph
    assert "py:function:apply" in g, (
        "an .rst page with no currentmodule has no namespace to resolve "
        "against; its reference must survive, not vanish"
    )
    assert {t for _, t, d in g.out_edges("std:file:page", data=True)} == {"py:function:apply"}


def test_no_namespace_means_no_guess():
    """A page with no module context is declined, not guessed."""
    from sphinxcontrib.nexus.ast_analyzer import _resolve_relative_references

    kg = _ns_graph()
    kg.add_node(GraphNode(id="std:file:page", type=NodeType.FILE, name="page",
                          display_name="page", domain="std"))
    _phantom_ref(kg, "std:file:page", "py:function:apply", "apply")
    assert _resolve_relative_references(kg) == 0


def test_real_targets_are_never_retargeted():
    from sphinxcontrib.nexus.ast_analyzer import _resolve_relative_references

    kg = _ns_graph()
    kg.add_edge(GraphEdge(source="py:module:pkg.beta",
                          target="py:method:pkg.alpha.Solver.apply",
                          type=EdgeType.REFERENCES))
    assert _resolve_relative_references(kg) == 0


def test_namespace_walk_ignores_documenting_doc_pages():
    """A symbol's ``contains`` parents include every doc page that
    documents it, not just its lexical owner.

    Ordering is what makes this bite: Sphinx extraction runs before AST
    analysis, so the ``doc:`` parent edge is inserted FIRST and leads
    ``in_edges``. Measured on ORPHEUS, `CoupledOperator.solve` lists
    `doc:api/numerics` ahead of `py:class:...CoupledOperator`. Following
    it dead-ends on a node with no module, which is indistinguishable
    from "no namespace" — so the reference was declined for the wrong
    reason and the whole pass looked like a near-no-op.
    """
    from sphinxcontrib.nexus.ast_analyzer import (
        _namespace_of,
        _resolve_relative_references,
    )

    kg = KnowledgeGraph()
    kg.add_node(GraphNode(id="py:module:pkg.alpha", type=NodeType.MODULE,
                          name="pkg.alpha", display_name="alpha", domain="py",
                          metadata={"file_path": "/a.py"}))
    kg.add_node(GraphNode(id="std:file:api/alpha", type=NodeType.FILE,
                          name="api/alpha", display_name="alpha", domain="std"))
    kg.add_node(GraphNode(id="py:class:pkg.alpha.Solver", type=NodeType.CLASS,
                          name="pkg.alpha.Solver", display_name="Solver",
                          domain="py", metadata={"file_path": "/a.py"}))
    kg.add_node(GraphNode(id="py:method:pkg.alpha.Solver.apply",
                          type=NodeType.METHOD, name="pkg.alpha.Solver.apply",
                          display_name="apply", domain="py",
                          metadata={"file_path": "/a.py"}))
    # Doc-page parents first — the Sphinx-then-AST insertion order.
    kg.add_edge(GraphEdge(source="std:file:api/alpha",
                          target="py:class:pkg.alpha.Solver",
                          type=EdgeType.CONTAINS))
    kg.add_edge(GraphEdge(source="std:file:api/alpha",
                          target="py:method:pkg.alpha.Solver.apply",
                          type=EdgeType.CONTAINS))
    kg.add_edge(GraphEdge(source="py:module:pkg.alpha",
                          target="py:class:pkg.alpha.Solver",
                          type=EdgeType.CONTAINS))
    kg.add_edge(GraphEdge(source="py:class:pkg.alpha.Solver",
                          target="py:method:pkg.alpha.Solver.apply",
                          type=EdgeType.CONTAINS))

    assert _namespace_of(kg.nxgraph, "py:method:pkg.alpha.Solver.apply") == (
        "pkg.alpha", "Solver",
    )
    _phantom_ref(kg, "py:method:pkg.alpha.Solver.apply", "py:function:apply",
                 "apply")
    assert _resolve_relative_references(kg) == 1
# Attributes bound after the class body (issue #40)
# ---------------------------------------------------------------------------
#
# The standard way to build enum-like singletons on a non-Enum class.
# They are real class attributes at import time — autodoc documents them
# and `:data:`BC.vacuum`` renders as a working link — but the class body
# holds no trace, so every such reference was reported dead. 6 of the 7
# findings remaining on ORPHEUS after #36 were this one shape.


def test_post_class_body_attribute_is_indexed(tmp_path):
    graph = _analyze_source(
        tmp_path,
        "from dataclasses import dataclass\n"
        "\n"
        "@dataclass(frozen=True)\n"
        "class BC:\n"
        "    kind: str\n"
        "\n"
        "BC.vacuum = BC('vacuum')          # type: ignore[attr-defined]\n"
        "BC.reflective = BC('reflective')  # type: ignore[attr-defined]\n",
    )
    g = graph.nxgraph
    for attr in ("kind", "vacuum", "reflective"):
        nid = f"py:attribute:pkg.mod.BC.{attr}"
        assert nid in g, f"{attr} missing"
        assert g.nodes[nid]["type"] == NodeType.ATTRIBUTE.value
    # Bound to the class, not left floating at module scope.
    children = {
        t for _, t, d in g.out_edges("py:class:pkg.mod.BC", data=True)
        if d.get("type") == EdgeType.CONTAINS.value
    }
    assert "py:attribute:pkg.mod.BC.vacuum" in children
    assert "py:data:pkg.mod.BC.vacuum" not in g


def test_post_class_attribute_carries_annotation(tmp_path):
    graph = _analyze_source(
        tmp_path,
        "class Reg:\n"
        "    pass\n"
        "\n"
        "Reg.default: 'Reg' = Reg()\n",
    )
    node = graph.nxgraph.nodes["py:attribute:pkg.mod.Reg.default"]
    assert node["annotation"] == "'Reg'"


def test_attribute_on_imported_module_is_not_claimed(tmp_path):
    """``mod.CONST = 1`` is somebody else's namespace.

    Only classes defined in THIS module are extended — otherwise
    monkey-patching an import would forge attributes onto a foreign
    symbol the graph does not own.
    """
    graph = _analyze_source(
        tmp_path,
        "import os\n"
        "\n"
        "class Local:\n"
        "    pass\n"
        "\n"
        "os.environ = {}\n"
        "Local.ok = 1\n",
    )
    g = graph.nxgraph
    assert "py:attribute:pkg.mod.Local.ok" in g
    assert not any("environ" in str(n) for n in g)


def test_post_class_attribute_rescues_the_reference(tmp_path):
    """End to end: the reference stops being reported dead."""
    graph = _analyze_source(
        tmp_path,
        "class BC:\n"
        '    """Boundary tags.\n'
        "\n"
        "    See :data:`pkg.mod.BC.vacuum` and :data:`pkg.mod.BC.gone`.\n"
        '    """\n'
        "\n"
        "BC.vacuum = BC()\n",
    )
    dead = {d.target_name for d in GraphQuery(graph).dead_references().dead}
    assert "pkg.mod.BC.vacuum" not in dead
    assert "pkg.mod.BC.gone" in dead


def test_nested_class_attribute_binding_is_not_forged(tmp_path):
    """Only top-level classes are addressable by a bare name."""
    graph = _analyze_source(
        tmp_path,
        "class Outer:\n"
        "    class Inner:\n"
        "        pass\n"
        "\n"
        "Outer.tag = 1\n",
    )
    g = graph.nxgraph
    assert "py:attribute:pkg.mod.Outer.tag" in g
    # `Inner` is not bindable as a bare name at module scope.
    assert "py:attribute:pkg.mod.Outer.Inner.x" not in g


# ---------------------------------------------------------------------------
# `#:` attribute comments and the :numref: crosswalk (issue #38)
# ---------------------------------------------------------------------------


def test_attribute_comment_block_references_are_extracted(tmp_path):
    """`#:` prose carries real cross-references and `ast` drops it."""
    graph = _analyze_source(
        tmp_path,
        "class FaceLayout:\n"
        "    pass\n"
        "\n"
        "#: The axis crosswalk that :class:`pkg.mod.FaceLayout` keys on.\n"
        "#: Continued on a second line, citing :func:`pkg.mod.absent`.\n"
        "AXIS_NAMES = ('x', 'y', 'z')\n",
    )
    g = graph.nxgraph
    targets = {t for _, t, d in g.out_edges("py:data:pkg.mod.AXIS_NAMES", data=True)
               if d.get("type") == EdgeType.REFERENCES.value}
    assert "py:class:pkg.mod.FaceLayout" in targets
    assert "py:function:pkg.mod.absent" in targets


def test_attribute_comment_uses_tokens_not_a_line_regex(tmp_path):
    """A `#:` inside a string literal is not an attribute comment.

    The issue asked for `tokenize` over a line regex precisely for this.
    """
    graph = _analyze_source(
        tmp_path,
        "class Thing:\n"
        "    pass\n"
        "\n"
        "TEMPLATE = 'prefix #: :class:`pkg.mod.Thing` suffix'\n"
        "\n"
        "PLAIN = 1  # :class:`pkg.mod.Thing` in an ordinary comment\n",
    )
    g = graph.nxgraph
    for nid in ("py:data:pkg.mod.TEMPLATE", "py:data:pkg.mod.PLAIN"):
        refs = {t for _, t, d in g.out_edges(nid, data=True)
                if d.get("type") == EdgeType.REFERENCES.value}
        assert not refs, f"{nid} picked up a non-attribute comment"


def test_trailing_attribute_comment_documents_its_own_line(tmp_path):
    graph = _analyze_source(
        tmp_path,
        "class Thing:\n"
        "    pass\n"
        "\n"
        "LIMIT = 10  #: capped by :class:`pkg.mod.Thing`\n",
    )
    targets = {t for _, t, d in graph.nxgraph.out_edges(
        "py:data:pkg.mod.LIMIT", data=True)
        if d.get("type") == EdgeType.REFERENCES.value}
    assert "py:class:pkg.mod.Thing" in targets


def test_attribute_comment_citing_an_instance_attribute_is_not_dead(tmp_path):
    """The ordering hazard #38 documents, verified.

    PEP 526 records ``self.x: T`` nowhere at runtime — it is function-local
    and discarded — so an import-based checker sees no such attribute and
    calls this reference dead. An AST indexer has the ``AnnAssign`` right
    there, which is why widening the scanned surface is safe HERE and was
    not safe in the consumer-side tool.
    """
    graph = _analyze_source(
        tmp_path,
        "class SNMesh:\n"
        "    def __init__(self):\n"
        "        self.bc: dict = {}\n"
        "\n"
        "#: Keyed on :attr:`pkg.mod.SNMesh.bc`.\n"
        "AXIS_NAMES = ('x',)\n",
    )
    dead = {d.target_name for d in GraphQuery(graph).dead_references().dead}
    assert "pkg.mod.SNMesh.bc" not in dead


def test_numref_crosswalks_to_the_equation(tmp_path):
    """`:numref:`label`` on an equation names the same target as `:eq:`."""
    from sphinxcontrib.nexus._mappings import resolve_target_id

    kg = KnowledgeGraph()
    kg.add_node(GraphNode(id="math:equation:peierls-3d", type=NodeType.EQUATION,
                          name="peierls-3d", display_name="peierls-3d",
                          domain="math"))
    assert resolve_target_id(
        kg.nxgraph, None, "std", "numref", "peierls-3d",
    ) == "math:equation:peierls-3d"


def test_numref_does_not_hijack_non_equation_targets():
    """The crosswalk fires only when the equation actually exists.

    ``:numref:`fig-mesh``` names a figure. It did not resolve before this
    change and still does not (figures live in the std-label namespace,
    which is separate work) — what matters is that it is not silently
    bound to a fabricated equation node.
    """
    from sphinxcontrib.nexus._mappings import resolve_target_id

    kg = KnowledgeGraph()
    kg.add_node(GraphNode(id="std:section:fig-mesh", type=NodeType.SECTION,
                          name="fig-mesh", display_name="fig-mesh", domain="std"))
    resolved = resolve_target_id(kg.nxgraph, None, "std", "numref", "fig-mesh")
    assert resolved != "math:equation:fig-mesh"


# ---------------------------------------------------------------------------
# Placeholder provenance (issue #39)
# ---------------------------------------------------------------------------
#
# The ORPHEUS shape behind #36: a prototyping directory still imported a
# module retired months earlier, so nexus minted placeholders for the
# unresolvable path — and bare roles on unrelated theory pages bound to
# them. Ranking (#41/#42) stopped a placeholder from beating a real
# definition, so that harm is gone. What is still worth surfacing is the
# weaker case: the placeholder is the only match, the reference IS dead,
# and the reason is that some corner of the tree names a symbol nothing
# defines. Naming those files turns "this reference is dead" into "this
# directory is minting a namespace".


def _minting_fixture() -> KnowledgeGraph:
    kg = KnowledgeGraph()
    kg.add_node(GraphNode(id="std:file:page", type=NodeType.FILE, name="page",
                          display_name="page", domain="std", docname="page"))
    # A prototype module that still imports something retired. Its
    # file_path is what makes ``proj`` a project-owned top level, so the
    # phantom below is project-internal rather than external.
    _node(kg, "py:module:proj.scratch.probe", NodeType.MODULE,
          "proj.scratch.probe", file_path="/proj/scratch/probe.py")
    _node(kg, "py:function:proj.retired.compute", NodeType.UNRESOLVED,
          "proj.retired.compute")
    kg.add_edge(GraphEdge(source="py:module:proj.scratch.probe",
                          target="py:function:proj.retired.compute",
                          type=EdgeType.IMPORTS))
    # A theory page references it too — this is the reported site.
    kg.add_edge(GraphEdge(source="std:file:page",
                          target="py:function:proj.retired.compute",
                          type=EdgeType.DOCUMENTS, metadata={"reftype": "func"}))
    return kg


def test_dead_reference_names_the_code_that_minted_it():
    result = GraphQuery(_minting_fixture()).dead_references()
    entry = next(d for d in result.dead
                 if d.target_name == "proj.retired.compute")
    assert entry.minted_by == ["/proj/scratch/probe.py"]


def test_ordinary_drift_has_no_minting_files():
    """A symbol nothing names is simply absent — that is normal drift."""
    kg = KnowledgeGraph()
    kg.add_node(GraphNode(id="std:file:page", type=NodeType.FILE, name="page",
                          display_name="page", domain="std", docname="page"))
    _node(kg, "py:module:proj.live", NodeType.MODULE, "proj.live",
          file_path="/proj/live.py")
    _node(kg, "py:class:proj.Gone", NodeType.UNRESOLVED, "proj.Gone")
    kg.add_edge(GraphEdge(source="std:file:page", target="py:class:proj.Gone",
                          type=EdgeType.DOCUMENTS, metadata={"reftype": "class"}))
    entry = next(d for d in GraphQuery(kg).dead_references().dead
                 if d.target_name == "proj.Gone")
    assert entry.minted_by == []


def test_prose_references_do_not_count_as_minting():
    """Only code that NAMES the target mints it.

    A doc page mentioning a vanished symbol is the reference being
    reported, not the cause of it — counting prose would make every
    finding look self-inflicted.
    """
    kg = _minting_fixture()
    kg.add_edge(GraphEdge(source="std:file:page",
                          target="py:function:proj.retired.compute",
                          type=EdgeType.REFERENCES))
    entry = next(d for d in GraphQuery(kg).dead_references().dead
                 if d.target_name == "proj.retired.compute")
    assert entry.minted_by == ["/proj/scratch/probe.py"]


# ---------------------------------------------------------------------------
# The test tree must not absorb production references
# ---------------------------------------------------------------------------
#
# Bare-name fuzzy matching is deliberately more permissive than Sphinx —
# it recovers thousands of real doc-page-to-class links that Sphinx
# renders as plain text. The test tree is where that generosity goes
# wrong: test modules are full of short generic names, so they act as a
# magnet for any bare name with no better candidate. Measured on
# ORPHEUS: 578 bare references from production code landed on test
# helpers, including a reactor-physics operator's `:attr:`K`` binding to
# a test class's attribute.


def _test_magnet_graph(referrer_is_test: bool = False) -> KnowledgeGraph:
    kg = KnowledgeGraph()
    _node(kg, "py:module:proj.solver", NodeType.MODULE, "proj.solver",
          file_path="/proj/solver.py", **({"in_test_file": True} if referrer_is_test else {}))
    # The only same-leaf candidate lives in the test tree.
    _node(kg, "py:attribute:tests.test_rate.Case.K", NodeType.ATTRIBUTE,
          "tests.test_rate.Case.K", file_path="/tests/test_rate.py",
          in_test_file=True)
    _node(kg, "py:attribute:K", NodeType.UNRESOLVED, "K")
    kg.add_edge(GraphEdge(source="py:module:proj.solver", target="py:attribute:K",
                          type=EdgeType.REFERENCES, metadata={"reftarget": "K"}))
    return kg


def test_production_reference_does_not_bind_to_a_test_helper():
    from sphinxcontrib.nexus.ast_analyzer import _canonicalize_phantoms

    kg = _test_magnet_graph()
    _canonicalize_phantoms(kg)
    targets = {t for _, t, d in kg.nxgraph.out_edges("py:module:proj.solver", data=True)
               if d.get("type") == EdgeType.REFERENCES.value}
    assert "py:attribute:tests.test_rate.Case.K" not in targets, (
        "a production docstring's bare name bound to a test helper"
    )
    assert "py:attribute:K" in targets


def test_test_to_test_bare_reference_still_binds():
    """The direction is the discriminator, not the name.

    A test module referencing a test helper is legitimate and must keep
    working — otherwise this rule would just be a blanket ban.
    """
    from sphinxcontrib.nexus.ast_analyzer import _canonicalize_phantoms

    kg = _test_magnet_graph(referrer_is_test=True)
    _canonicalize_phantoms(kg)
    targets = {t for _, t, d in kg.nxgraph.out_edges("py:module:proj.solver", data=True)
               if d.get("type") == EdgeType.REFERENCES.value}
    assert "py:attribute:tests.test_rate.Case.K" in targets


def test_non_test_candidate_is_unaffected():
    """Production-to-production bare matching keeps its generosity."""
    from sphinxcontrib.nexus.ast_analyzer import _canonicalize_phantoms

    kg = KnowledgeGraph()
    _node(kg, "py:module:proj.docs", NodeType.MODULE, "proj.docs",
          file_path="/proj/docs.py")
    _node(kg, "py:class:proj.cp.solver.CPMesh", NodeType.CLASS,
          "proj.cp.solver.CPMesh", file_path="/proj/cp/solver.py")
    _node(kg, "py:class:CPMesh", NodeType.UNRESOLVED, "CPMesh")
    kg.add_edge(GraphEdge(source="py:module:proj.docs", target="py:class:CPMesh",
                          type=EdgeType.REFERENCES, metadata={"reftarget": "CPMesh"}))
    _canonicalize_phantoms(kg)
    targets = {t for _, t, d in kg.nxgraph.out_edges("py:module:proj.docs", data=True)
               if d.get("type") == EdgeType.REFERENCES.value}
    assert "py:class:proj.cp.solver.CPMesh" in targets


def test_test_helpers_inside_the_test_root_are_flagged(tmp_path):
    """``is_test`` must cover helper modules, not just ``test_*.py``.

    Test patterns are written project-relative (``tests/*``) but paths
    were matched relative to the SOURCE DIR — and when that dir is
    itself the test root, the prefix is gone and only the filename
    patterns fire. 2113 of ORPHEUS's test-tree nodes carried no
    ``is_test``, and they were exactly the ones catching bad bindings.
    """
    from sphinxcontrib.nexus.ast_analyzer import analyze_directory

    (tmp_path / "tests" / "_harness").mkdir(parents=True)
    (tmp_path / "tests" / "__init__.py").write_text("")
    (tmp_path / "tests" / "_harness" / "__init__.py").write_text("")
    (tmp_path / "tests" / "_harness" / "registry.py").write_text(
        "def record():\n    pass\n"
    )
    # Analyzed as its own source dir, the way nexus_extra_source_dirs does.
    g = analyze_directory(
        source_dir=tmp_path / "tests", project_root=tmp_path, exclude_patterns=[],
    ).nxgraph
    node = next(n for n in g if str(n).endswith("registry.record"))
    assert g.nodes[node].get("in_test_file") is True
