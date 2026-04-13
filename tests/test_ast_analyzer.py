"""Unit tests for AST analyzer (no Sphinx dependency)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from sphinxcontrib.nexus.ast_analyzer import (
    CodeVisitor,
    ImportTracker,
    ModuleResolver,
    _extract_type_names,
    analyze_directory,
)


# ---------------------------------------------------------------------------
# ImportTracker tests
# ---------------------------------------------------------------------------


def test_import_alias():
    tracker = ImportTracker("mymod")
    tree = ast.parse("import numpy as np")
    tracker.add_import(tree.body[0])
    assert tracker.resolve("np") == "numpy"
    assert tracker.resolve("np.ndarray") == "numpy.ndarray"


def test_import_no_alias():
    tracker = ImportTracker("mymod")
    tree = ast.parse("import json")
    tracker.add_import(tree.body[0])
    assert tracker.resolve("json") == "json"
    assert tracker.resolve("json.loads") == "json.loads"


def test_import_from():
    tracker = ImportTracker("mymod")
    tree = ast.parse("from scipy.sparse import csr_matrix")
    tracker.add_import_from(tree.body[0])
    assert tracker.resolve("csr_matrix") == "scipy.sparse.csr_matrix"


def test_import_from_alias():
    tracker = ImportTracker("mymod")
    tree = ast.parse("from scipy.sparse import csr_matrix as sp_csr")
    tracker.add_import_from(tree.body[0])
    assert tracker.resolve("sp_csr") == "scipy.sparse.csr_matrix"


def test_future_annotations_detected():
    tracker = ImportTracker("mymod")
    tree = ast.parse("from __future__ import annotations")
    tracker.add_import_from(tree.body[0])
    assert tracker.has_future_annotations


def test_unknown_name_passes_through():
    tracker = ImportTracker("mymod")
    assert tracker.resolve("Foo") == "Foo"
    assert tracker.resolve("bar.baz") == "bar.baz"


# ---------------------------------------------------------------------------
# Annotation extraction tests
# ---------------------------------------------------------------------------


def test_extract_simple_type():
    tracker = ImportTracker("mymod")
    node = ast.parse("int", mode="eval").body
    names = _extract_type_names(node, tracker)
    assert "int" in names


def test_extract_dotted_type():
    tracker = ImportTracker("mymod")
    tree = ast.parse("import numpy as np")
    tracker.add_import(tree.body[0])
    node = ast.parse("np.ndarray", mode="eval").body
    names = _extract_type_names(node, tracker)
    assert "numpy.ndarray" in names


def test_extract_pep604_union():
    tracker = ImportTracker("mymod")
    node = ast.parse("int | float", mode="eval").body
    names = _extract_type_names(node, tracker)
    assert "int" in names
    assert "float" in names


def test_extract_subscript():
    tracker = ImportTracker("mymod")
    node = ast.parse("list[int]", mode="eval").body
    names = _extract_type_names(node, tracker)
    assert "list" in names
    assert "int" in names


def test_extract_string_annotation():
    tracker = ImportTracker("mymod")
    # Simulate from __future__ import annotations: annotation is a string constant
    node = ast.Constant(value="int | float")
    names = _extract_type_names(node, tracker)
    assert "int" in names
    assert "float" in names


# ---------------------------------------------------------------------------
# ModuleResolver tests
# ---------------------------------------------------------------------------


def test_simple_file_to_module(tmp_path):
    (tmp_path / "foo.py").touch()
    resolver = ModuleResolver(tmp_path)
    assert resolver.file_to_module(tmp_path / "foo.py") == "foo"


def test_package_file_to_module(tmp_path):
    pkg = tmp_path / "data" / "macro_xs"
    pkg.mkdir(parents=True)
    (pkg / "mixture.py").touch()
    resolver = ModuleResolver(tmp_path)
    assert resolver.file_to_module(pkg / "mixture.py") == "data.macro_xs.mixture"


def test_numbered_dir_sys_path(tmp_path):
    subdir = tmp_path / "02.Collision.Probability"
    subdir.mkdir()
    (subdir / "solver.py").touch()
    resolver = ModuleResolver(tmp_path)  # auto-detects numbered dirs
    assert resolver.file_to_module(subdir / "solver.py") == "solver"


def test_init_file(tmp_path):
    pkg = tmp_path / "geometry"
    pkg.mkdir()
    (pkg / "__init__.py").touch()
    resolver = ModuleResolver(tmp_path)
    assert resolver.file_to_module(pkg / "__init__.py") == "geometry"


# ---------------------------------------------------------------------------
# CodeVisitor tests
# ---------------------------------------------------------------------------


def _visit_source(source: str, module_name: str = "testmod") -> CodeVisitor:
    tree = ast.parse(source)
    visitor = CodeVisitor(module_name, "test.py")
    visitor.visit(tree)
    return visitor


def _node_ids(visitor: CodeVisitor) -> set[str]:
    return {n.id for n in visitor.nodes}


def _edge_tuples(visitor: CodeVisitor, edge_type: str | None = None) -> list[tuple[str, str, str]]:
    return [
        (e.source, e.target, e.type.value if hasattr(e.type, "value") else e.type)
        for e in visitor.edges
        if edge_type is None or (e.type.value if hasattr(e.type, "value") else e.type) == edge_type
    ]


def test_function_node():
    v = _visit_source("def compute(x): pass")
    assert "py:function:testmod.compute" in _node_ids(v)


def test_class_node():
    v = _visit_source("class Widget: pass")
    assert "py:class:testmod.Widget" in _node_ids(v)


def test_method_node():
    v = _visit_source("class Widget:\n    def run(self): pass")
    assert "py:method:testmod.Widget.run" in _node_ids(v)


def test_contains_function():
    v = _visit_source("def compute(x): pass")
    edges = _edge_tuples(v, "contains")
    assert ("py:module:testmod", "py:function:testmod.compute", "contains") in edges


def test_contains_class_method():
    v = _visit_source("class Widget:\n    def run(self): pass")
    edges = _edge_tuples(v, "contains")
    assert ("py:class:testmod.Widget", "py:method:testmod.Widget.run", "contains") in edges


def test_inherits_edge():
    v = _visit_source("class Child(Parent): pass")
    edges = _edge_tuples(v, "inherits")
    assert ("py:class:testmod.Child", "py:class:Parent", "inherits") in edges


def test_inherits_dotted():
    v = _visit_source("import abc\nclass Foo(abc.ABC): pass")
    edges = _edge_tuples(v, "inherits")
    assert ("py:class:testmod.Foo", "py:class:abc.ABC", "inherits") in edges


def test_imports_edge():
    v = _visit_source("import numpy")
    edges = _edge_tuples(v, "imports")
    assert ("py:module:testmod", "py:module:numpy", "imports") in edges


def test_imports_from_edge():
    v = _visit_source("from scipy.sparse import csr_matrix")
    edges = _edge_tuples(v, "imports")
    assert ("py:module:testmod", "py:module:scipy", "imports") in edges


def test_calls_edge():
    v = _visit_source("import json\ndef foo():\n    json.loads('{}')")
    edges = _edge_tuples(v, "calls")
    targets = {e[1] for e in edges}
    assert "py:function:json.loads" in targets


def test_calls_self_method():
    v = _visit_source(
        "class Solver:\n"
        "    def run(self):\n"
        "        self.step()\n"
        "    def step(self): pass"
    )
    edges = _edge_tuples(v, "calls")
    targets = {e[1] for e in edges}
    assert "py:function:testmod.Solver.step" in targets


def test_calls_aliased():
    v = _visit_source("import numpy as np\ndef foo():\n    np.array([1, 2])")
    edges = _edge_tuples(v, "calls")
    targets = {e[1] for e in edges}
    assert "py:function:numpy.array" in targets


def test_type_uses_param():
    v = _visit_source("def foo(x: int): pass")
    edges = _edge_tuples(v, "type_uses")
    targets = {e[1] for e in edges}
    assert "py:class:int" in targets


def test_type_uses_return():
    v = _visit_source("def foo() -> str: pass")
    edges = _edge_tuples(v, "type_uses")
    targets = {e[1] for e in edges}
    assert "py:class:str" in targets


def test_type_uses_aliased():
    v = _visit_source("import numpy as np\ndef foo(x: np.ndarray): pass")
    edges = _edge_tuples(v, "type_uses")
    targets = {e[1] for e in edges}
    assert "py:class:numpy.ndarray" in targets


def test_type_uses_pep604():
    v = _visit_source("def foo(x: int | float): pass")
    edges = _edge_tuples(v, "type_uses")
    targets = {e[1] for e in edges}
    assert "py:class:int" in targets
    assert "py:class:float" in targets


def test_docstring_sphinx_role():
    v = _visit_source('def foo():\n    """:class:`Widget` does stuff."""\n    pass')
    edges = _edge_tuples(v, "references")
    targets = {e[1] for e in edges}
    assert "py:class:Widget" in targets


def test_docstring_tilde_role():
    v = _visit_source('def foo():\n    """:func:`~mymod.compute` ref."""\n    pass')
    edges = _edge_tuples(v, "references")
    targets = {e[1] for e in edges}
    assert "py:function:mymod.compute" in targets


def test_docstring_math_role_targets_equation_namespace():
    """`:math:` in a docstring refers to a Sphinx math equation label,
    not a Python object. Target ID must be math:equation:<label>."""
    v = _visit_source(
        'def solve_cp():\n'
        '    """Implements :math:`transport-cartesian`."""\n'
        '    pass'
    )
    edges = _edge_tuples(v, "references")
    targets = {e[1] for e in edges}
    assert "math:equation:transport-cartesian" in targets
    assert "py:math:transport-cartesian" not in targets


def test_docstring_eq_role_targets_equation_namespace():
    v = _visit_source(
        'def solve():\n'
        '    """See :eq:`boltzmann`."""\n'
        '    pass'
    )
    edges = _edge_tuples(v, "references")
    targets = {e[1] for e in edges}
    assert "math:equation:boltzmann" in targets
    assert "py:eq:boltzmann" not in targets


def test_docstring_math_role_skips_latex_source():
    """A `:math:` role whose target is LaTeX source (not a label) must
    not produce a bogus equation reference. We use a raw docstring in
    the fixture so the backslashes survive Python parsing."""
    v = _visit_source(
        'def foo():\n'
        '    r""":math:`\\alpha + \\beta`."""\n'
        '    pass'
    )
    edges = _edge_tuples(v, "references")
    targets = {e[1] for e in edges}
    assert not any(t.startswith("math:equation:") for t in targets)


def test_docstring_math_role_skips_braced_latex():
    v = _visit_source(
        'def foo():\n'
        '    """:math:`{n}`."""\n'
        '    pass'
    )
    edges = _edge_tuples(v, "references")
    targets = {e[1] for e in edges}
    assert not any(t.startswith("math:equation:") for t in targets)


def test_docstring_all_python_roles_stay_in_py_namespace():
    src = (
        'def f():\n'
        '    """:func:`g` :meth:`C.m` :class:`C` :mod:`pkg` '
        ':attr:`x` :data:`D`."""\n'
        '    pass'
    )
    v = _visit_source(src)
    targets = {e[1] for e in _edge_tuples(v, "references")}
    assert "py:function:g" in targets
    assert "py:method:C.m" in targets
    assert "py:class:C" in targets
    assert "py:module:pkg" in targets
    assert "py:attribute:x" in targets
    assert "py:data:D" in targets


# ---------------------------------------------------------------------------
# analyze_directory integration test
# ---------------------------------------------------------------------------


def test_analyze_directory(tmp_path):
    """Create real files and run full analysis."""
    # Module A
    (tmp_path / "alpha.py").write_text(
        "import beta\n\n"
        "class Base:\n"
        "    pass\n\n"
        "def run():\n"
        "    beta.compute()\n"
    )
    # Module B
    (tmp_path / "beta.py").write_text(
        "def compute() -> int:\n"
        "    return 42\n"
    )

    graph = analyze_directory(tmp_path, exclude_patterns=[])
    nids = set(graph.nxgraph.nodes)

    assert "py:module:alpha" in nids
    assert "py:module:beta" in nids
    assert "py:class:alpha.Base" in nids
    assert "py:function:alpha.run" in nids
    assert "py:function:beta.compute" in nids

    # Check edges
    edge_data = [
        (s, t, d.get("type"))
        for s, t, d in graph.nxgraph.edges(data=True)
    ]
    imports = [(s, t) for s, t, et in edge_data if et == "imports"]
    calls = [(s, t) for s, t, et in edge_data if et == "calls"]

    assert ("py:module:alpha", "py:module:beta") in imports
    # alpha.run calls beta.compute
    call_targets = {t for s, t in calls if "alpha.run" in s}
    assert "py:function:beta.compute" in call_targets


def test_exclude_patterns_match_nested_paths(tmp_path):
    """`tests/*` and `tests/**` must exclude files at arbitrary depth,
    not just direct children. Historically this was broken because the
    analyzer used ``Path.match`` which only matches the path tail."""
    (tmp_path / "src.py").write_text("def keeper(): pass\n")
    nested = tmp_path / "tests" / "unit" / "deep"
    nested.mkdir(parents=True)
    (nested / "test_foo.py").write_text("def test_one(): pass\n")
    (tmp_path / "tests" / "test_top.py").write_text("def test_two(): pass\n")

    graph = analyze_directory(tmp_path, exclude_patterns=["tests/*"])
    nids = set(graph.nxgraph.nodes)

    assert "py:function:src.keeper" in nids
    # Nested test file must NOT leak through.
    assert not any("test_one" in n for n in nids)
    # Top-level test file also excluded.
    assert not any("test_two" in n for n in nids)


def test_exclude_patterns_fnmatch_semantics(tmp_path):
    """Exclusion patterns are evaluated with fnmatch against the POSIX
    path relative to ``source_dir`` — so patterns like ``docs/*`` or
    ``*/vendor/*`` work as users would expect."""
    (tmp_path / "a.py").write_text("def a(): pass\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "conf.py").write_text("project = 'x'\n")
    pkg = tmp_path / "pkg" / "vendor"
    pkg.mkdir(parents=True)
    (pkg / "thirdparty.py").write_text("def vendored(): pass\n")

    graph = analyze_directory(
        tmp_path, exclude_patterns=["docs/*", "*/vendor/*"]
    )
    nids = set(graph.nxgraph.nodes)

    assert "py:function:a.a" in nids
    assert not any("vendored" in n for n in nids)
    assert not any("conf" in n and "docs" in n for n in nids)
