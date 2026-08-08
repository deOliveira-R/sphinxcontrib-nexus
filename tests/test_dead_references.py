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
    kg.add_node(GraphNode(id="doc:page", type=NodeType.FILE, name="page",
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
    kg.add_node(GraphNode(id="citation:Bell1970", type=NodeType.UNRESOLVED,
                          name="Bell1970", display_name="Bell1970",
                          domain="citation"))

    def ref(target, edge_type=EdgeType.DOCUMENTS, **meta):
        kg.add_edge(GraphEdge(source="doc:page", target=target, type=edge_type,
                              metadata=meta))

    ref("py:class:pkg.Gone")                                   # dead
    ref("py:attribute:pkg.Sub.attr")                           # rescued: inherited
    ref("py:attribute:pkg.SubExt.attr")                        # undecidable: opaque base
    ref("py:class:pkg.Alias")                                  # rescued: re-export
    ref("py:class:numpy.gone")                                 # skipped: external root
    ref("math:equation:gone-label", EdgeType.EQUATION_REF)     # dead equation
    ref("math:equation:x_i", EdgeType.REFERENCES, reftype="math")  # inline math, skipped
    ref("citation:Bell1970", EdgeType.CITES)                   # citations skipped
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
    kg.add_node(GraphNode(id="doc:page", type=NodeType.FILE, name="page",
                          display_name="page", domain="std", docname="page"))
    _node(kg, "py:method:pkg.Sink.zeros_on", NodeType.UNRESOLVED,
          "pkg.Sink.zeros_on")
    kg.add_edge(GraphEdge(source="doc:page", target="py:method:pkg.Sink.zeros_on",
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
    assert by_name["pkg.Gone"].sites[0].source.id == "doc:page"


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
