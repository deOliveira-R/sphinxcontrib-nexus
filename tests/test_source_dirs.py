"""Which directories the AST analysis scans: the ones conf.py declared, never the interpreter's own entry.

The founding case: Read the Docs builds with ``cd docs && python -m sphinx . out``. ``python -m`` puts the
working directory at the head of ``sys.path``, so the docs directory sat on ``sys.path`` under the project
root, was read as a directory ``conf.py`` declared, and became the ONLY source root: on ORPHEUS the graph's
module count fell from 1243 to 876 and 409 ``.. implements::`` directives warned (2026-10-25). Run from the project
root, the same build warned 0 times.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sphinxcontrib.nexus import _interpreter_path_entry, _source_dirs
from sphinxcontrib.nexus.ast_analyzer import analyze_directory, is_tool_tree


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    for d in ("docs", "src", "tests", ".venv/lib/site-packages"):
        (tmp_path / d).mkdir(parents=True)
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "build.py").touch()
    return tmp_path.resolve()


# --- which entry the interpreter prepended ---------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["python", "-m", "sphinx", ".", "out"],
        ["python", "-O", "-m", "sphinx"],
        ["python", "-Om", "sphinx"],
        ["python", "-msphinx"],
        ["python", "-W", "error", "-m", "sphinx"],
        ["python", "-Werror", "-m", "sphinx"],
        ["python", "-X", "dev", "-m", "sphinx"],
        ["python", "--check-hash-based-pycs", "always", "-m", "sphinx"],
        ["python", "-c", "import sphinx"],
        ["python", "-"],
        ["python"],
    ],
)
def test_module_command_stdin_and_prompt_prepend_the_working_directory(tree: Path, argv: list[str]) -> None:
    assert _interpreter_path_entry(argv, tree / "docs", safe_path=False) == tree / "docs"


def test_a_script_prepends_its_own_directory(tree: Path) -> None:
    argv = ["python", "-O", "tools/build.py", "-m"]
    assert _interpreter_path_entry(argv, tree, safe_path=False) == tree / "tools"


def test_an_option_argument_is_not_read_as_the_script(tree: Path) -> None:
    argv = ["python", "-W", "ignore", "tools/build.py"]
    assert _interpreter_path_entry(argv, tree, safe_path=False) == tree / "tools"


def test_a_directory_script_is_itself_the_entry(tree: Path) -> None:
    assert _interpreter_path_entry(["python", "tools"], tree, safe_path=False) == tree / "tools"


def test_safe_path_prepends_nothing(tree: Path) -> None:
    assert _interpreter_path_entry(["python", "-P", "-m", "sphinx"], tree / "docs", safe_path=True) is None


# --- which directories are scanned -----------------------------------------------------------------


def test_the_interpreter_entry_is_not_a_declared_source_root(tree: Path) -> None:
    """Read the Docs' shape: ``cd docs && python -m sphinx``, with conf.py adding only the project root."""
    sys_path = [str(tree / "docs"), str(tree), str(tree / ".venv/lib/site-packages")]
    assert _source_dirs(tree, sys_path, tree / "docs") == [tree]


def test_without_the_interpreter_entry_set_aside_the_docs_directory_is_scanned_alone(tree: Path) -> None:
    """The defect, spelled: the same sys.path read as if conf.py had declared every entry."""
    sys_path = [str(tree / "docs"), str(tree)]
    assert _source_dirs(tree, sys_path, None) == [tree / "docs"]


def test_a_directory_conf_py_also_declared_is_kept(tree: Path) -> None:
    """conf.py inserting the working directory adds a second occurrence; only one is set aside."""
    sys_path = [str(tree / "docs"), str(tree / "docs"), str(tree)]
    assert _source_dirs(tree, sys_path, tree / "docs") == [tree / "docs"]


def test_declared_directories_are_kept_in_order_once_each(tree: Path) -> None:
    sys_path = [str(tree / "src"), str(tree / "tests"), str(tree / "src"), str(tree / "docs")]
    assert _source_dirs(tree, sys_path, tree / "docs") == [tree / "src", tree / "tests"]


def test_tool_trees_and_outside_paths_are_never_source(tree: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
    outside = tmp_path_factory.mktemp("elsewhere")
    sys_path = [str(tree / ".venv/lib/site-packages"), str(outside), str(tree / "missing")]
    assert _source_dirs(tree, sys_path, None) == [tree]


def test_a_checkout_under_a_directory_named_build_is_still_source(tmp_path: Path) -> None:
    root = (tmp_path / "build" / "proj").resolve()
    (root / "src").mkdir(parents=True)
    assert _source_dirs(root, [str(root / "src")], None) == [root / "src"]


# --- which files are tools' trees ------------------------------------------------------------------


@pytest.mark.parametrize(
    "parts",
    [("build", "lib", "pkg"), ("dist",), (".venv", "lib"), ("pkg", "__pycache__"), ("pkg.egg-info",), ("docs", "_build")],
)
def test_tool_trees(parts: tuple[str, ...]) -> None:
    assert is_tool_tree(parts)


@pytest.mark.parametrize("parts", [(), ("pkg",), ("pkg", "build"), ("pkg", "dist", "x"), ("builder",)])
def test_project_directories(parts: tuple[str, ...]) -> None:
    assert not is_tool_tree(parts)


def test_the_analyzer_skips_the_setuptools_copy_and_keeps_a_build_subpackage(tmp_path: Path) -> None:
    """``pip install .`` leaves ``build/lib/<pkg>``; Read the Docs installs exactly so (2026-10-25: the ORPHEUS graph
    indexed every module twice, once as ``build.lib.orpheus.*``). Rooted under a directory itself named ``build``."""
    root = tmp_path / "build" / "proj"
    for d in ("pkg/build", "build/lib/pkg"):
        (root / d).mkdir(parents=True)
        (root / d / "__init__.py").write_text("")
    (root / "pkg" / "__init__.py").write_text("def f():\n    return 1\n")
    (root / "pkg" / "build" / "steps.py").write_text("def g():\n    return 2\n")
    (root / "build" / "lib" / "pkg" / "__init__.py").write_text("def f():\n    return 1\n")
    ids = set(analyze_directory(root, project_root=root, exclude_patterns=[]).nxgraph.nodes)
    assert any(i.endswith("pkg.build.steps.g") for i in ids), sorted(ids)
    assert any(i.endswith(":pkg.f") for i in ids), sorted(ids)
    assert not any("build.lib" in i for i in ids), sorted(i for i in ids if "build.lib" in i)
