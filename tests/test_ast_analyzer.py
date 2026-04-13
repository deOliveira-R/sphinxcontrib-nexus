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


def test_is_test_requires_test_file_and_conventional_name(tmp_path):
    """``is_test`` must be True only for functions in a test file whose
    name is ``test`` or starts with ``test_``. Production modules with
    incidental names like ``tested_value`` or ``testify`` must not be
    flagged, even if they happen to start with ``test``."""
    src = tmp_path / "mymod.py"
    src.write_text(
        "def tested_value(): return 1\n"
        "def testify(): return 2\n"
        "def test_actual_unit(): pass\n"
    )
    test_file = tmp_path / "tests" / "test_unit.py"
    test_file.parent.mkdir()
    test_file.write_text(
        "def test_something(): pass\n"
        "def _helper(): pass\n"
        "def fixture_setup(): pass\n"
    )
    graph = analyze_directory(tmp_path, exclude_patterns=[])
    nodes = {n: d for n, d in graph.nxgraph.nodes(data=True)}

    # Production module: no function should be flagged is_test, even
    # though two of them start with "test".
    assert nodes["py:function:mymod.tested_value"].get("is_test") is not True
    assert nodes["py:function:mymod.testify"].get("is_test") is not True
    assert nodes["py:function:mymod.test_actual_unit"].get("is_test") is not True

    # Test file: test_something is a real test; helpers are not.
    assert nodes["py:function:tests.test_unit.test_something"].get("is_test") is True
    assert nodes["py:function:tests.test_unit._helper"].get("is_test") is not True
    assert nodes["py:function:tests.test_unit.fixture_setup"].get("is_test") is not True


def _find_node(visitor: CodeVisitor, node_id: str):
    for n in visitor.nodes:
        if n.id == node_id:
            return n
    raise AssertionError(f"{node_id} not in visitor.nodes")


def test_function_level_decorators_captured():
    src = (
        "import pytest\n"
        "\n"
        "@pytest.mark.l0\n"
        '@pytest.mark.verifies("eq-1")\n'
        '@pytest.mark.catches("FM-07")\n'
        "def test_attenuation():\n"
        "    pass\n"
    )
    v = _visit_source(src)
    node = _find_node(v, "py:function:testmod.test_attenuation")
    assert node.metadata["vv_level"] == "L0"
    assert node.metadata["verifies"] == ("eq-1",)
    assert node.metadata["catches"] == ("FM-07",)
    # ``decorators`` records the raw serialized form of every decorator.
    decs = node.metadata["decorators"]
    assert len(decs) == 3
    assert "pytest.mark.l0" in decs
    assert any("verifies" in d for d in decs)


def test_decorator_metadata_absent_when_no_decorators():
    v = _visit_source("def plain(): pass\n")
    node = _find_node(v, "py:function:testmod.plain")
    assert "decorators" not in node.metadata
    assert "vv_level" not in node.metadata


def test_decorator_on_method():
    src = (
        "import pytest\n"
        "class Foo:\n"
        "    @pytest.mark.slow\n"
        "    def test_it(self): pass\n"
    )
    v = _visit_source(src)
    node = _find_node(v, "py:method:testmod.Foo.test_it")
    assert node.metadata.get("slow") is True


def test_class_level_decorator_propagates_to_methods():
    src = (
        "import pytest\n"
        "\n"
        "@pytest.mark.l1\n"
        "class TestBalance:\n"
        "    def test_zero_residual(self): pass\n"
    )
    v = _visit_source(src)
    node = _find_node(
        v, "py:method:testmod.TestBalance.test_zero_residual"
    )
    assert node.metadata["vv_level"] == "L1"


def test_method_level_decorator_overrides_class():
    src = (
        "import pytest\n"
        "\n"
        "@pytest.mark.l1\n"
        "class TestBalance:\n"
        "    @pytest.mark.l2\n"
        "    def test_integration(self): pass\n"
        "    def test_inherits(self): pass\n"
    )
    v = _visit_source(src)
    overridden = _find_node(
        v, "py:method:testmod.TestBalance.test_integration"
    )
    inherited = _find_node(
        v, "py:method:testmod.TestBalance.test_inherits"
    )
    assert overridden.metadata["vv_level"] == "L2"
    assert inherited.metadata["vv_level"] == "L1"


def test_class_pytestmark_assignment_propagates():
    src = (
        "import pytest\n"
        "\n"
        "class TestStuff:\n"
        "    pytestmark = pytest.mark.l2\n"
        "    def test_one(self): pass\n"
    )
    v = _visit_source(src)
    node = _find_node(v, "py:method:testmod.TestStuff.test_one")
    assert node.metadata["vv_level"] == "L2"


def test_module_pytestmark_propagates():
    src = (
        "import pytest\n"
        "pytestmark = pytest.mark.l2\n"
        "\n"
        "def test_one(): pass\n"
        "def test_two(): pass\n"
    )
    v = _visit_source(src)
    for name in ("test_one", "test_two"):
        node = _find_node(v, f"py:function:testmod.{name}")
        assert node.metadata["vv_level"] == "L2", name


def test_module_pytestmark_list_form():
    src = (
        "import pytest\n"
        "pytestmark = [pytest.mark.l1, pytest.mark.slow]\n"
        "\n"
        "def test_one(): pass\n"
    )
    v = _visit_source(src)
    node = _find_node(v, "py:function:testmod.test_one")
    assert node.metadata["vv_level"] == "L1"
    assert node.metadata["slow"] is True


def test_function_marker_beats_module_pytestmark():
    src = (
        "import pytest\n"
        "pytestmark = pytest.mark.l0\n"
        "\n"
        "@pytest.mark.l3\n"
        "def test_strict(): pass\n"
    )
    v = _visit_source(src)
    node = _find_node(v, "py:function:testmod.test_strict")
    assert node.metadata["vv_level"] == "L3"


def test_nested_class_does_not_leak_markers_upward():
    src = (
        "import pytest\n"
        "\n"
        "class Outer:\n"
        "    @pytest.mark.l2\n"
        "    class Inner:\n"
        "        def test_inside(self): pass\n"
        "    def test_outside(self): pass\n"
    )
    v = _visit_source(src)
    inner = _find_node(v, "py:method:testmod.Outer.Inner.test_inside")
    outer = _find_node(v, "py:method:testmod.Outer.test_outside")
    assert inner.metadata["vv_level"] == "L2"
    assert "vv_level" not in outer.metadata


def test_verify_sugar_decorator_on_function():
    src = (
        "from tests._harness import verify\n"
        "\n"
        "@verify.l1(equations=['fixture-attenuation'], catches=['FM-01'])\n"
        "def test_vacuum(): pass\n"
    )
    v = _visit_source(src)
    node = _find_node(v, "py:function:testmod.test_vacuum")
    assert node.metadata["vv_level"] == "L1"
    assert node.metadata["verifies"] == ("fixture-attenuation",)
    assert node.metadata["catches"] == ("FM-01",)


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
