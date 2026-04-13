"""AST-based Python source code analyzer.

Extracts code-level relationships (calls, imports, inheritance, type usage)
from Python source files and writes them to the same graph as Sphinx extraction.

No Sphinx dependency — usable standalone via CLI.
"""

from __future__ import annotations

import ast
import logging
import re
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from sphinxcontrib.nexus.graph import (
    EdgeType,
    GraphEdge,
    GraphNode,
    KnowledgeGraph,
    NodeType,
)

logger = logging.getLogger(__name__)

# Regex for Sphinx cross-reference roles in docstrings.
# Captures the role name and the raw content between backticks; the
# content is parsed further by ``_parse_role_target`` to handle the
# ``title <target>`` form, leading ``~`` (strip-module display
# hint), and leading ``!`` (suppress-link convention).
_SPHINX_ROLE_RE = re.compile(r":(\w+):`([^`]+)`")

# ``title <target>`` form: Sphinx allows a cross-reference role to
# declare a display title distinct from the target, like
# ``:func:`display name <pkg.mod.actual>```. The target inside the
# angle brackets is what we want for graph resolution; the title is
# presentation noise.
_ROLE_TITLE_TARGET_RE = re.compile(r"^.*?<(?P<target>[^>]+)>\s*$")


def _parse_role_target(raw: str) -> str | None:
    """Extract the resolvable target from a role-body string.

    ``raw`` is whatever sat between the backticks of a
    ``:role:`...``` reference. This function normalizes it into
    the actual target the graph should resolve, or returns
    ``None`` when the role should be skipped entirely (e.g. the
    ``!`` suppression form).

    Handles, in order:

    1. ``!foo``   — suppressed link; Sphinx renders it as ``foo``
       but creates no cross-reference. Return ``None``.
    2. ``title <target>`` — display-title form; return ``target``.
    3. ``~pkg.mod.foo`` — strip-module display hint; return
       ``pkg.mod.foo`` (with the leading ``~`` removed).
    4. Plain ``foo`` — return as-is.
    """
    stripped = raw.strip()
    if not stripped:
        return None

    # ``!foo`` — suppress-link convention. Sphinx renders the text
    # but emits no pending_xref, so there's nothing for the graph
    # to resolve. Drop.
    if stripped.startswith("!"):
        return None

    # ``display title <target>`` — dig out the actual target.
    m = _ROLE_TITLE_TARGET_RE.match(stripped)
    if m:
        inner = m.group("target").strip()
        # The inner target can still carry a ``~`` hint.
        if inner.startswith("~"):
            inner = inner[1:]
        return inner or None

    # Plain target, possibly with a leading ``~`` display hint.
    if stripped.startswith("~"):
        return stripped[1:] or None
    return stripped


# ---------------------------------------------------------------------------
# Decorator parsing helpers
# ---------------------------------------------------------------------------


def _render_decorator(node: ast.expr) -> str:
    """Serialize a decorator AST node to its source-like string.

    Handles bare names (``@foo``), attribute chains (``@pytest.mark.l0``),
    calls with positional args (``@verifies("label-1", "label-2")``),
    and keyword args (``@verify.l0(catches=["ERR-003"])``). Falls back
    to ``<unparseable>`` for anything ``ast.unparse`` can't handle.
    """
    try:
        return ast.unparse(node)
    except Exception:
        return "<unparseable>"


def _dotted_name(node: ast.expr) -> str | None:
    """Reconstruct a dotted identifier like ``pytest.mark.l0`` from an
    ``Attribute`` / ``Name`` chain. Returns ``None`` if the chain
    contains anything else (calls, subscripts, etc.)."""
    parts: list[str] = []
    curr: ast.expr = node
    while isinstance(curr, ast.Attribute):
        parts.append(curr.attr)
        curr = curr.value
    if not isinstance(curr, ast.Name):
        return None
    parts.append(curr.id)
    parts.reverse()
    return ".".join(parts)


def _literal_strings(node: ast.expr) -> tuple[str, ...] | None:
    """Extract a tuple of string literals from a ``Constant(str)``, a
    list/tuple literal of string constants, or return ``None`` if the
    expression contains anything else. Used so we never evaluate
    arbitrary expressions in decorator arguments."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return (node.value,)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        out: list[str] = []
        for elt in node.elts:
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                out.append(elt.value)
            else:
                return None
        return tuple(out)
    return None


_PYTEST_LEVELS = frozenset({"l0", "l1", "l2", "l3"})


def _parse_pytest_markers(
    decorators: list[ast.expr],
) -> dict[str, object]:
    """Extract structured pytest-marker metadata from a decorator list.

    Returns a dict with any of these keys present when recognized:

    - ``vv_level``: ``"L0" / "L1" / "L2" / "L3"`` — from
      ``@pytest.mark.lN`` or ``@verify.lN(...)``.
    - ``verifies``: ``tuple[str, ...]`` — string args from
      ``@pytest.mark.verifies(...)`` or ``equations=[...]`` kwarg
      in ``@verify.lN(...)``.
    - ``catches``: ``tuple[str, ...]`` — same, but from
      ``@pytest.mark.catches(...)`` or ``catches=[...]`` kwarg.
    - ``slow``: ``True`` if ``@pytest.mark.slow`` is present.

    Only extracts constant-string literals (bare or in list/tuple/set
    literals). Unrecognized decorators are silently ignored here; they
    still appear in the flat ``decorators`` metadata emitted by
    ``_render_decorator``.
    """
    meta: dict[str, object] = {}
    verifies: list[str] = []
    catches: list[str] = []

    for dec in decorators:
        target = dec.func if isinstance(dec, ast.Call) else dec
        dotted = _dotted_name(target)
        if dotted is None:
            continue

        parts = dotted.split(".")

        # ``pytest.mark.*`` family
        if len(parts) >= 3 and parts[0] == "pytest" and parts[1] == "mark":
            mark = parts[2]
            if mark in _PYTEST_LEVELS:
                meta["vv_level"] = mark.upper()
            elif mark == "slow":
                meta["slow"] = True
            elif mark == "verifies" and isinstance(dec, ast.Call):
                for arg in dec.args:
                    lits = _literal_strings(arg)
                    if lits:
                        verifies.extend(lits)
            elif mark == "catches" and isinstance(dec, ast.Call):
                for arg in dec.args:
                    lits = _literal_strings(arg)
                    if lits:
                        catches.extend(lits)
            continue

        # ``verify.lN(...)`` sugar
        if len(parts) >= 2 and parts[0] == "verify" and parts[1] in _PYTEST_LEVELS:
            meta["vv_level"] = parts[1].upper()
            if isinstance(dec, ast.Call):
                for kw in dec.keywords:
                    if kw.arg == "equations":
                        lits = _literal_strings(kw.value)
                        if lits:
                            verifies.extend(lits)
                    elif kw.arg == "catches":
                        lits = _literal_strings(kw.value)
                        if lits:
                            catches.extend(lits)
            continue

    if verifies:
        meta["verifies"] = tuple(verifies)
    if catches:
        meta["catches"] = tuple(catches)
    return meta


def _collect_pytestmark_assignments(
    body: list[ast.stmt],
) -> dict[str, object]:
    """Find ``pytestmark = ...`` at the given scope and parse its value
    as if it were a decorator. Supports single-mark and list-of-marks
    forms (``pytestmark = pytest.mark.l0`` and
    ``pytestmark = [pytest.mark.l0, pytest.mark.slow]``)."""
    for stmt in body:
        if not isinstance(stmt, ast.Assign):
            continue
        if len(stmt.targets) != 1:
            continue
        tgt = stmt.targets[0]
        if not isinstance(tgt, ast.Name) or tgt.id != "pytestmark":
            continue
        value = stmt.value
        if isinstance(value, (ast.List, ast.Tuple)):
            marks = list(value.elts)
        else:
            marks = [value]
        return _parse_pytest_markers(marks)
    return {}


# ---------------------------------------------------------------------------
# ModuleResolver — file path → qualified module name
# ---------------------------------------------------------------------------


class ModuleResolver:
    """Convert file paths to qualified Python module names.

    Handles three common project layouts:

    1. **Standard Python packages**: `myproject/module/file.py`
       → `module.file` (project_root contains packages with __init__.py)
    2. **Flat modules**: `src/solver.py` → `solver`
       (source directories on sys.path)
    3. **Non-standard layouts**: directories manually added to sys.path
       (e.g., numbered directories like `01.Solvers/solver.py` → `solver`)

    The resolver tries sys_path_dirs first (if provided), then auto-detects
    by looking for directories containing .py files, and always falls back
    to project_root.
    """

    def __init__(
        self,
        project_root: Path,
        sys_path_dirs: list[Path] | None = None,
    ) -> None:
        self._project_root = project_root.resolve()
        if sys_path_dirs is not None:
            self._roots = [d.resolve() for d in sys_path_dirs]
        else:
            self._roots = self._auto_detect_roots()
        # Always include project_root as a fallback
        if self._project_root not in self._roots:
            self._roots.append(self._project_root)

    def _auto_detect_roots(self) -> list[Path]:
        """Auto-detect source roots under the project.

        Strategy:
        1. If project has a `src/` directory, use it (src layout)
        2. If project has directories containing .py files that aren't
           Python packages (no __init__.py in project root), add them
           as individual roots (flat layout / non-standard like numbered dirs)
        3. Otherwise project_root itself is the root (standard package layout)
        """
        roots: list[Path] = []

        # Check for src layout
        src_dir = self._project_root / "src"
        if src_dir.is_dir():
            roots.append(src_dir)
            return roots

        # Check for directories containing .py files
        # These could be packages (have __init__.py) or flat module dirs
        for d in sorted(self._project_root.iterdir()):
            if not d.is_dir():
                continue
            # Skip common non-source directories
            if d.name.startswith((".", "_")) or d.name in (
                "docs", "tests", "test", "venv", "node_modules", "build", "dist",
            ):
                continue
            # If directory has .py files, it's a potential source root
            has_py = any(d.glob("*.py"))
            has_init = (d / "__init__.py").exists()
            if has_py and not has_init:
                # Flat module directory (no __init__.py) — add as sys.path root
                # This handles numbered dirs, src-less layouts, etc.
                roots.append(d)

        return roots

    def file_to_module(self, filepath: Path) -> str:
        """Convert an absolute file path to a qualified module name."""
        filepath = filepath.resolve()
        for root in self._roots:
            try:
                rel = filepath.relative_to(root)
            except ValueError:
                continue
            parts = list(rel.with_suffix("").parts)
            if parts and parts[-1] == "__init__":
                parts.pop()
            if parts:
                return ".".join(parts)
        # Fallback: just use the stem
        return filepath.stem


# ---------------------------------------------------------------------------
# ImportTracker — per-file import alias resolution
# ---------------------------------------------------------------------------


class ImportTracker:
    """Track import aliases for a single file.

    Maps local names to their fully qualified targets:
        import numpy as np          → np → numpy
        from scipy.sparse import csr_matrix  → csr_matrix → scipy.sparse.csr_matrix
        from . import foo           → foo → <parent_module>.foo
    """

    def __init__(self, module_name: str) -> None:
        self._module_name = module_name
        self._aliases: dict[str, str] = {}
        self._has_future_annotations = False

    @property
    def has_future_annotations(self) -> bool:
        return self._has_future_annotations

    def add_import(self, node: ast.Import) -> None:
        for alias in node.names:
            local_name = alias.asname or alias.name
            self._aliases[local_name] = alias.name

    def add_import_from(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if module == "__future__":
            for alias in node.names:
                if alias.name == "annotations":
                    self._has_future_annotations = True
            return
        # Handle relative imports
        if node.level > 0:
            parts = self._module_name.rsplit(".", node.level)
            base = parts[0] if parts else ""
            module = f"{base}.{module}" if module else base
        for alias in node.names:
            local_name = alias.asname or alias.name
            qualified = f"{module}.{alias.name}" if module else alias.name
            self._aliases[local_name] = qualified

    def resolve(self, name: str) -> str:
        """Resolve a possibly-aliased name to its fully qualified form.

        "np.ndarray" → "numpy.ndarray"
        "csr_matrix" → "scipy.sparse.csr_matrix"
        "solve"      → "solve" (no alias, returned as-is)
        """
        parts = name.split(".")
        top = parts[0]
        if top in self._aliases:
            resolved_top = self._aliases[top]
            if len(parts) > 1:
                return f"{resolved_top}.{'.'.join(parts[1:])}"
            return resolved_top
        return name

    def imported_modules(self) -> list[str]:
        """Return all top-level module names imported by this file."""
        return list(set(v.split(".")[0] for v in self._aliases.values()))


# ---------------------------------------------------------------------------
# Annotation parser — extract type names from AST annotation nodes
# ---------------------------------------------------------------------------


def _extract_type_names(
    node: ast.expr | None,
    imports: ImportTracker,
) -> list[str]:
    """Recursively extract all type names from an annotation AST node."""
    if node is None:
        return []

    # String annotation (from __future__ import annotations)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            parsed = ast.parse(node.value, mode="eval")
            return _extract_type_names(parsed.body, imports)
        except SyntaxError:
            return []

    # Simple name: int, str, MyClass
    if isinstance(node, ast.Name):
        return [imports.resolve(node.id)]

    # Dotted name: np.ndarray, scipy.sparse.csr_matrix
    if isinstance(node, ast.Attribute):
        full = _unparse_attribute(node)
        return [imports.resolve(full)]

    # Subscript: list[int], Optional[str], dict[str, int]
    if isinstance(node, ast.Subscript):
        names = _extract_type_names(node.value, imports)
        names.extend(_extract_type_names(node.slice, imports))
        return names

    # PEP 604 union: X | Y
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        names = _extract_type_names(node.left, imports)
        names.extend(_extract_type_names(node.right, imports))
        return names

    # Tuple (used in subscript slices for dict[K, V])
    if isinstance(node, ast.Tuple):
        names: list[str] = []
        for elt in node.elts:
            names.extend(_extract_type_names(elt, imports))
        return names

    return []


def _unparse_attribute(node: ast.Attribute) -> str:
    """Reconstruct a dotted name from nested ast.Attribute nodes."""
    parts: list[str] = []
    curr: ast.expr = node
    while isinstance(curr, ast.Attribute):
        parts.append(curr.attr)
        curr = curr.value
    if isinstance(curr, ast.Name):
        parts.append(curr.id)
    parts.reverse()
    return ".".join(parts)


def _resolve_call_target(node: ast.Call, imports: ImportTracker) -> str | None:
    """Extract the function name from a Call node and resolve aliases."""
    func = node.func
    if isinstance(func, ast.Name):
        return imports.resolve(func.id)
    if isinstance(func, ast.Attribute):
        full = _unparse_attribute(func)
        # Skip self.method() — resolve to ClassName.method in the visitor
        if full.startswith("self."):
            return None  # handled specially in the visitor
        return imports.resolve(full)
    return None


# ---------------------------------------------------------------------------
# CodeVisitor — single-pass AST visitor per file
# ---------------------------------------------------------------------------


class CodeVisitor(ast.NodeVisitor):
    """Walk a Python file's AST and extract nodes and edges."""

    def __init__(
        self,
        module_name: str,
        file_path: str,
        is_test_file: bool = False,
    ) -> None:
        self._module_name = module_name
        self._file_path = file_path
        self._is_test_file = is_test_file
        self._scope: list[str] = [module_name]
        self._imports = ImportTracker(module_name)
        self.nodes: list[GraphNode] = []
        self.edges: list[GraphEdge] = []
        # Pytest-marker metadata stashed at module and class scope.
        # When a function is visited, these layer underneath its own
        # decorator metadata (module lowest, function highest precedence).
        self._module_pytest_meta: dict[str, object] = {}
        self._current_class_pytest_meta: dict[str, object] = {}

        # Create module node
        self.nodes.append(GraphNode(
            id=f"py:module:{module_name}",
            type=NodeType.MODULE,
            name=module_name,
            display_name=module_name,
            domain="py",
            metadata={"file_path": file_path, "source": "ast"},
        ))

    @property
    def _qualified_name(self) -> str:
        return ".".join(self._scope)

    @property
    def _current_class(self) -> str | None:
        """Return the current class name if inside a class scope."""
        for i in range(len(self._scope) - 1, 0, -1):
            # Check if scope[i] looks like a class (starts uppercase)
            if self._scope[i][0:1].isupper():
                return ".".join(self._scope[: i + 1])
        return None

    def visit_Module(self, node: ast.Module) -> None:
        """Visit only direct body statements of the module.

        Before walking, scan for a top-level ``pytestmark = ...``
        assignment and stash its parsed markers as the module-level
        default. Contained functions and methods layer this underneath
        their own markers (module < class < function).
        """
        self._module_pytest_meta = _collect_pytestmark_assignments(node.body)
        for child in node.body:
            self.visit(child)

    def _node_id(self, node_type: str, name: str) -> str:
        return f"py:{node_type}:{name}"

    def _add_docstring_refs(self, node: ast.AST, source_id: str) -> None:
        """Extract Sphinx role references from docstring.

        Python-domain roles produce ``py:<objtype>:<name>`` target IDs that
        reconcile against AST-discovered symbols. The math roles ``:math:``
        and ``:eq:`` instead point at Sphinx math equation labels in the
        ``math:equation:<label>`` namespace, which is what Sphinx's math
        extractor produces for ``.. math:: :label: foo`` blocks.
        """
        docstring = ast.get_docstring(node)
        if not docstring:
            return
        # Python-domain role → objtype
        py_type_map = {
            "func": "function", "meth": "method", "class": "class",
            "mod": "module", "attr": "attribute", "data": "data",
            "exc": "exception", "obj": "function",
        }
        for match in _SPHINX_ROLE_RE.finditer(docstring):
            role, raw = match.group(1), match.group(2)
            target = _parse_role_target(raw)
            if target is None:
                # ``!foo`` suppression, empty body, or otherwise
                # unresolvable — skip.
                continue

            if role in ("math", "eq"):
                # `:math:` and `:eq:` both name an equation label. Skip
                # LaTeX-source targets (which contain backslashes or
                # braces) — those are inline math, not label references.
                if any(c in target for c in "\\{}"):
                    continue
                target_id = f"math:equation:{target}"
            elif role in py_type_map:
                resolved = self._imports.resolve(target)
                target_id = f"py:{py_type_map[role]}:{resolved}"
            else:
                # Unknown or unsupported role — skip rather than forge a
                # bogus `py:<role>:...` node that can never resolve.
                continue

            self.edges.append(GraphEdge(
                source=source_id,
                target=target_id,
                type=EdgeType.REFERENCES,
                metadata={"reftype": role, "reftarget": target, "source": "ast"},
            ))

    def visit_Import(self, node: ast.Import) -> None:
        self._imports.add_import(node)
        module_id = self._node_id("module", self._module_name)
        for alias in node.names:
            target_module = alias.name.split(".")[0]
            self.edges.append(GraphEdge(
                source=module_id,
                target=self._node_id("module", target_module),
                type=EdgeType.IMPORTS,
                metadata={"full_import": alias.name, "source": "ast"},
            ))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self._imports.add_import_from(node)
        if node.module and node.module != "__future__":
            module_id = self._node_id("module", self._module_name)
            target_module = node.module.split(".")[0]
            # Resolve relative imports
            if node.level > 0:
                parts = self._module_name.rsplit(".", node.level)
                base = parts[0] if parts else ""
                target_module = base.split(".")[0] if base else target_module
            self.edges.append(GraphEdge(
                source=module_id,
                target=self._node_id("module", target_module),
                type=EdgeType.IMPORTS,
                metadata={"full_import": node.module, "source": "ast"},
            ))

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scope.append(node.name)
        qname = self._qualified_name
        class_id = self._node_id("class", qname)

        class_meta: dict[str, object] = {
            "file_path": self._file_path,
            "lineno": node.lineno,
            "end_lineno": node.end_lineno,
            "source": "ast",
        }
        if node.decorator_list:
            class_meta["decorators"] = tuple(
                _render_decorator(dec) for dec in node.decorator_list
            )

        # Stash class-level pytest markers so contained methods pick
        # them up as defaults (function-level markers still win per the
        # precedence rule in ``_visit_function``). Save/restore the
        # previous value so nested classes don't leak state upward.
        prev_class_meta = self._current_class_pytest_meta
        cls_markers = _parse_pytest_markers(node.decorator_list)
        # Also honor a ``pytestmark`` assignment at class scope.
        cls_markers.update(_collect_pytestmark_assignments(node.body))
        self._current_class_pytest_meta = cls_markers

        self.nodes.append(GraphNode(
            id=class_id,
            type=NodeType.CLASS,
            name=qname,
            display_name=node.name,
            domain="py",
            metadata=class_meta,
        ))

        # CONTAINS from parent scope
        parent_id = self._node_id("module", self._scope[0])
        if len(self._scope) > 2:
            # Nested class — parent is enclosing class
            parent_name = ".".join(self._scope[:-1])
            parent_id = self._node_id("class", parent_name)
        self.edges.append(GraphEdge(
            source=parent_id, target=class_id, type=EdgeType.CONTAINS,
            metadata={"source": "ast"},
        ))

        # INHERITS from base classes
        for base in node.bases:
            if isinstance(base, ast.Name):
                base_name = self._imports.resolve(base.id)
            elif isinstance(base, ast.Attribute):
                base_name = self._imports.resolve(_unparse_attribute(base))
            else:
                continue
            self.edges.append(GraphEdge(
                source=class_id,
                target=self._node_id("class", base_name),
                type=EdgeType.INHERITS,
                metadata={"source": "ast"},
            ))

        self._add_docstring_refs(node, class_id)
        # Visit only direct body statements (methods, nested classes)
        # NOT generic_visit which recurses into all descendants and can
        # blow the stack on files with deeply nested expressions.
        for child in node.body:
            self.visit(child)
        self._scope.pop()
        self._current_class_pytest_meta = prev_class_meta

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._scope.append(node.name)
        qname = self._qualified_name

        # Determine if this is a method (inside a class) or a function
        is_method = self._current_class is not None and len(self._scope) >= 3
        node_type = NodeType.METHOD if is_method else NodeType.FUNCTION
        type_str = "method" if is_method else "function"
        func_id = self._node_id(type_str, qname)

        # A function is a test only when both the name follows the
        # unittest/pytest convention AND it lives in a file that matches
        # the project's test-module patterns. The second condition keeps
        # production helpers like ``tested_value`` or ``testify`` from
        # being mistaken for tests.
        _name = node.name
        _name_looks_like_test = _name == "test" or _name.startswith("test_")
        is_test = self._is_test_file and _name_looks_like_test

        # Decorator metadata: raw serialized forms plus structured
        # pytest-marker fields. Function-level markers win over any
        # class- or module-level pytestmark stashed in the scope, so we
        # layer them: module (lowest) → class → function (highest).
        #
        # Inherited markers (module and class scope) only propagate to
        # functions that qualify as tests. A helper like
        # ``_build_homogeneous_mesh`` living in a test module must NOT
        # pick up the module's ``pytestmark = pytest.mark.verifies(...)``
        # — inheriting it would write spurious TESTS edges from the
        # helper and inflate declared coverage. Function-level
        # decorators are always respected because they're explicit.
        meta: dict[str, object] = {
            "file_path": self._file_path,
            "lineno": node.lineno,
            "end_lineno": node.end_lineno,
            "source": "ast",
        }
        if is_test:
            meta["is_test"] = True
            if self._module_pytest_meta:
                meta.update(self._module_pytest_meta)
            if self._current_class_pytest_meta:
                meta.update(self._current_class_pytest_meta)
        if node.decorator_list:
            meta["decorators"] = tuple(
                _render_decorator(dec) for dec in node.decorator_list
            )
            meta.update(_parse_pytest_markers(node.decorator_list))

        self.nodes.append(GraphNode(
            id=func_id,
            type=node_type,
            name=qname,
            display_name=node.name,
            domain="py",
            metadata=meta,
        ))

        # CONTAINS from parent scope
        if is_method:
            parent_name = ".".join(self._scope[:-1])
            parent_id = self._node_id("class", parent_name)
        else:
            parent_id = self._node_id("module", self._scope[0])
        self.edges.append(GraphEdge(
            source=parent_id, target=func_id, type=EdgeType.CONTAINS,
            metadata={"source": "ast"},
        ))

        # TYPE_USES from parameter annotations
        for arg in (
            node.args.args + node.args.posonlyargs + node.args.kwonlyargs
        ):
            if arg.annotation:
                for type_name in _extract_type_names(arg.annotation, self._imports):
                    self.edges.append(GraphEdge(
                        source=func_id,
                        target=self._node_id("class", type_name),
                        type=EdgeType.TYPE_USES,
                        metadata={
                            "param": arg.arg, "source": "ast",
                        },
                    ))
        if node.args.vararg and node.args.vararg.annotation:
            for type_name in _extract_type_names(node.args.vararg.annotation, self._imports):
                self.edges.append(GraphEdge(
                    source=func_id, target=self._node_id("class", type_name),
                    type=EdgeType.TYPE_USES,
                    metadata={"param": f"*{node.args.vararg.arg}", "source": "ast"},
                ))
        if node.args.kwarg and node.args.kwarg.annotation:
            for type_name in _extract_type_names(node.args.kwarg.annotation, self._imports):
                self.edges.append(GraphEdge(
                    source=func_id, target=self._node_id("class", type_name),
                    type=EdgeType.TYPE_USES,
                    metadata={"param": f"**{node.args.kwarg.arg}", "source": "ast"},
                ))

        # TYPE_USES from return annotation
        if node.returns:
            for type_name in _extract_type_names(node.returns, self._imports):
                self.edges.append(GraphEdge(
                    source=func_id,
                    target=self._node_id("class", type_name),
                    type=EdgeType.TYPE_USES,
                    metadata={"param": "return", "source": "ast"},
                ))

        self._add_docstring_refs(node, func_id)

        # Walk body for CALLS edges
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                target = _resolve_call_target(child, self._imports)
                if target is None and isinstance(child.func, ast.Attribute):
                    # self.method() → ClassName.method()
                    attr = child.func
                    if isinstance(attr.value, ast.Name) and attr.value.id == "self":
                        cls = self._current_class
                        if cls:
                            target = f"{cls}.{attr.attr}"
                if target:
                    # Determine target ID — could be function, method, or class (constructor)
                    target_id = self._node_id("function", target)
                    self.edges.append(GraphEdge(
                        source=func_id,
                        target=target_id,
                        type=EdgeType.CALLS,
                        metadata={
                            "lineno": getattr(child, "lineno", 0),
                            "source": "ast",
                        },
                    ))

        # Don't call generic_visit — we already walked the body for Call nodes
        self._scope.pop()


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


#: Default glob patterns used to recognize Python test modules when
#: callers don't supply their own. These match the same POSIX-style
#: semantics as ``exclude_patterns`` and are shared by ``CodeVisitor``
#: to decide whether a function's ``is_test`` flag can be set.
DEFAULT_TEST_PATTERNS: tuple[str, ...] = (
    "tests/*",
    "*/tests/*",
    "test_*.py",
    "*/test_*.py",
)


def analyze_directory(
    source_dir: Path,
    project_root: Path | None = None,
    sys_path_dirs: list[Path] | None = None,
    exclude_patterns: list[str] | None = None,
    test_patterns: list[str] | None = None,
) -> KnowledgeGraph:
    """Analyze all Python files in a directory and return a KnowledgeGraph.

    Args:
        source_dir: Directory to scan for .py files.
        project_root: Root for module name resolution. Defaults to source_dir.
        sys_path_dirs: Extra directories on the Python path.
        exclude_patterns: Glob patterns to exclude (default: docs, venv).
        test_patterns: Glob patterns that identify test modules. Files
            matching these are still analyzed (unless separately excluded)
            but functions inside them are eligible for the ``is_test``
            flag.
    """
    if project_root is None:
        project_root = source_dir
    if exclude_patterns is None:
        exclude_patterns = ["docs/*", ".venv/*", "__pycache__/*"]
    if test_patterns is None:
        test_patterns = list(DEFAULT_TEST_PATTERNS)

    resolver = ModuleResolver(project_root, sys_path_dirs)
    graph = KnowledgeGraph()

    # Pre-compute exclusion directory names for fast filtering
    _skip_dirs = {".venv", "venv", "__pycache__", "node_modules", ".tox", ".git"}
    py_files = sorted(source_dir.rglob("*.py"))
    for filepath in py_files:
        # Skip files under excluded directories
        if _skip_dirs & set(filepath.parts):
            continue
        rel = filepath.relative_to(source_dir).as_posix()
        # Match exclude patterns against the relative POSIX path, not the
        # path tail (Path.match anchors to the right, which silently
        # skips nested matches for patterns like ``tests/*``).
        if any(fnmatch(rel, pat) for pat in exclude_patterns):
            continue

        try:
            source = filepath.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(filepath))
        except (SyntaxError, UnicodeDecodeError) as e:
            logger.warning("Skipping %s: %s", filepath, e)
            continue

        module_name = resolver.file_to_module(filepath)
        is_test_file = any(fnmatch(rel, pat) for pat in test_patterns)
        visitor = CodeVisitor(module_name, str(filepath), is_test_file=is_test_file)
        visitor.visit(tree)

        for node in visitor.nodes:
            graph.add_node(node)
        for edge in visitor.edges:
            graph.add_edge(edge)

    # Classify phantom nodes created by add_edge for targets not in the graph.
    # These are external functions/modules (numpy.array, scipy.integrate.quad, etc.)
    _classify_phantom_nodes(graph)

    # Fold re-export phantoms into their canonical AST counterpart.
    # A call site like ``Thing()`` inside ``pkg.user`` that imports
    # ``Thing`` via ``pkg.__init__`` / ``pkg.geometry.__init__`` emits
    # a ``py:function:pkg.geometry.Thing`` phantom (because the call
    # resolver hardcodes a ``py:function:`` prefix regardless of type).
    # ``_canonicalize_phantoms`` detects those by leaf-name match
    # against typed class/function/method nodes and retargets their
    # edges onto the canonical.
    _canonicalize_phantoms(graph)

    logger.info(
        "AST analysis: %d nodes, %d edges from %d files",
        graph.node_count, graph.edge_count, len(py_files),
    )
    return graph


#: Node types that count as "concrete" for canonicalization purposes —
#: phantoms fold INTO these, never the other way around.
_CANONICAL_TYPES: frozenset[str] = frozenset({
    NodeType.CLASS.value,
    NodeType.FUNCTION.value,
    NodeType.METHOD.value,
    NodeType.MODULE.value,
    NodeType.EXCEPTION.value,
    NodeType.TYPE.value,
})

#: Node types that MAY be folded into a canonical by
#: ``_canonicalize_phantoms``. External / unresolved / empty-typed
#: nodes are folded; anything with a concrete type stays put.
_PHANTOM_TYPES: frozenset[str] = frozenset({
    NodeType.UNRESOLVED.value,
    NodeType.EXTERNAL.value,
    "",
})

#: Map the ``py:<kind>:`` ID prefix to the corresponding concrete
#: node type. Used by ``_upgrade_types_from_signals`` to rescue a
#: node whose ID says it's a class/function/method but whose type
#: attribute is still ``unresolved`` because an earlier stage
#: (Sphinx pending_xref placeholder, NetworkX auto-creation from an
#: add_edge target, etc.) never upgraded it.
_ID_PREFIX_TO_TYPE: dict[str, str] = {
    "py:class:": NodeType.CLASS.value,
    "py:function:": NodeType.FUNCTION.value,
    "py:method:": NodeType.METHOD.value,
    "py:module:": NodeType.MODULE.value,
    "py:attribute:": NodeType.ATTRIBUTE.value,
    "py:exception:": NodeType.EXCEPTION.value,
    "py:type:": NodeType.TYPE.value,
    "py:data:": NodeType.DATA.value,
}

#: Concreteness ranking used when a leaf-name fold has multiple
#: canonical candidates. Lower rank = more concrete — the fold
#: picks the winner with the lowest rank and ties break on
#: ``file_path``-bearing nodes. ``class`` beats ``function`` beats
#: everything else so mistyped class constructors land on the
#: class, not on a same-leaf function from a different module.
_TYPE_RANK: dict[str, int] = {
    NodeType.CLASS.value: 0,
    NodeType.EXCEPTION.value: 1,
    NodeType.METHOD.value: 2,
    NodeType.FUNCTION.value: 3,
    NodeType.TYPE.value: 4,
    NodeType.ATTRIBUTE.value: 5,
    NodeType.DATA.value: 6,
    NodeType.MODULE.value: 7,
    NodeType.EQUATION.value: 8,
    NodeType.SECTION.value: 9,
    NodeType.TERM.value: 10,
    NodeType.FILE.value: 11,
    NodeType.EXTERNAL.value: 12,
    NodeType.UNRESOLVED.value: 13,
    "": 14,
}


def _upgrade_types_from_signals(graph: KnowledgeGraph) -> int:
    """Rescue nodes whose ID and ``file_path`` prove a concrete type.

    Pattern: Sphinx creates a placeholder ``py:class:pkg.mod.Thing``
    node when a pending_xref can't be resolved and marks it
    ``unresolved``. Then the AST layer merges ``file_path`` and
    ``lineno`` from a ``ClassDef`` walk but ``merge_graphs`` does
    not copy the type. The final merged node looks like
    ``type=unresolved`` even though every signal
    (``py:class:`` ID prefix, real file/line) says it's a class.

    This pass walks every node and upgrades its type when the ID
    prefix is an authoritative concrete-type marker AND a
    ``file_path`` is set — that combination can only come from a
    real AST class/function/method definition. Runs idempotently;
    nodes already typed as the right concrete type are untouched.

    Returns the number of nodes upgraded.
    """
    g = graph.nxgraph
    upgraded = 0
    for nid in list(g.nodes):
        attrs = g.nodes[nid]
        if not attrs.get("file_path"):
            continue
        current = attrs.get("type", "")
        if current in _CANONICAL_TYPES:
            continue
        for prefix, concrete in _ID_PREFIX_TO_TYPE.items():
            if nid.startswith(prefix):
                attrs["type"] = concrete
                upgraded += 1
                break
    if upgraded:
        logger.info(
            "Upgraded %d node types from id-prefix+file_path signals", upgraded,
        )
    return upgraded


def _module_paths_overlap(phantom_name: str, canonical_name: str) -> bool:
    """Return True if ``phantom_name`` and ``canonical_name`` could
    plausibly refer to the same symbol by virtue of their module
    paths overlapping.

    Given two dotted names that share the same LEAF, the module
    path is the dotted prefix (everything before the leaf). Overlap
    holds when either of these module paths is a prefix or suffix
    of the other. Examples::

        pkg.geometry.Thing       vs  pkg.geometry.mesh.Thing   → True
            (``pkg.geometry`` is a prefix of ``pkg.geometry.mesh``)

        geometry.mesh.Thing      vs  pkg.geometry.mesh.Thing   → True
            (``geometry.mesh`` is a suffix of ``pkg.geometry.mesh``)

        numpy.ndarray            vs  local.ndarray             → False
            (``numpy`` neither prefix nor suffix of ``local``)

    The leaf-name fold uses this to distinguish same-symbol reshapes
    (re-exports, short-import paths) from genuine leaf-name
    collisions across unrelated modules.
    """
    def _prefix(name: str) -> str:
        return name.rsplit(".", 1)[0] if "." in name else ""

    p = _prefix(phantom_name)
    c = _prefix(canonical_name)
    if not p or not c:
        return False
    if p == c:
        return True
    # Prefix check: every dotted-segment of p is the head of c, or vice versa.
    p_parts = p.split(".")
    c_parts = c.split(".")
    if len(p_parts) <= len(c_parts) and c_parts[: len(p_parts)] == p_parts:
        return True
    if len(c_parts) <= len(p_parts) and p_parts[: len(c_parts)] == c_parts:
        return True
    # Suffix check: p_parts == tail of c_parts, or vice versa.
    if len(p_parts) <= len(c_parts) and c_parts[-len(p_parts):] == p_parts:
        return True
    if len(c_parts) <= len(p_parts) and p_parts[-len(c_parts):] == c_parts:
        return True
    return False


def _canonical_rank_key(
    cid: str, cname: str, attrs: dict,
) -> tuple[int, int, str]:
    """Sort key for canonical-candidate tie-breaking.

    Lower sorts first (= winner). The composite is:

    1. ``_TYPE_RANK[type]`` — most-concrete-type wins. Class beats
       method beats function beats external beats unresolved.
    2. ``0`` if the node has ``file_path`` set, else ``1`` —
       file-backed nodes win over bare references.
    3. The node id as a final tiebreaker, so the sort is
       deterministic when all other signals are equal.
    """
    type_rank = _TYPE_RANK.get(attrs.get("type") or "", 99)
    has_file = 0 if attrs.get("file_path") else 1
    return (type_rank, has_file, cid)


def _canonicalize_phantoms(graph: KnowledgeGraph) -> int:
    """Fold re-export and mis-typed phantoms into canonical AST nodes.

    Pattern: a call site like ``Thing()`` inside ``pkg.user`` emits a
    ``py:function:pkg.geometry.Thing`` target (hardcoded prefix in
    ``_resolve_call_target``), even when ``Thing`` is actually a
    class that lives at ``py:class:pkg.geometry.mesh.Thing``. The
    phantom classifier marks the target ``unresolved`` because
    ``pkg`` is project-internal, and the merge-time full-name
    reconciler misses it because the dotted names differ.

    This pass walks every phantom (``unresolved`` / ``external`` /
    untyped node) and, when a unique same-leaf canonical exists in
    the same module-path neighborhood, retargets every incoming and
    outgoing edge onto the canonical and drops the phantom.

    **Canonical selection** uses a concreteness ranking — class >
    method > function > external > unresolved — tie-broken by
    whether the candidate has a ``file_path``. This matters when
    both a real class and a same-leaf call-site phantom are
    leaf-matched: the class always wins the fold even if it was
    entered into ``leaf_index`` via the lower-priority code path.

    **Canonical recognition** accepts both genuinely typed concrete
    nodes AND nodes whose ID prefix + ``file_path`` signal a
    concrete type even if the ``type`` attr is stale (e.g. a
    ``py:class:pkg.mod.Thing`` with ``type=unresolved`` because a
    Sphinx pending_xref placeholder was never upgraded post-merge).
    ``_upgrade_types_from_signals`` normalises this up front so the
    leaf index picks everything up.

    **Bare-name phantoms** (name has no dots, e.g. ``Thing`` from a
    ``Mesh1D(...)`` call where the imported name wasn't
    qualified) fold into the unique same-leaf canonical without
    the module-path-overlap check, since there's no module path
    to compare.

    The module-path-overlap guard is what prevents a legitimate
    reference like ``numpy.ndarray`` from being folded into a
    local project class that happens to share the ``ndarray``
    leaf name. ``numpy`` is neither a prefix nor a suffix of
    ``local``, so the pair doesn't match.

    Returns the number of phantom nodes removed.
    """
    g = graph.nxgraph

    # Up-front type rescue: a node whose id prefix is py:class:/
    # py:function:/py:method: and whose file_path is set is
    # canonical by definition, even if the type attr is stale.
    _upgrade_types_from_signals(graph)

    # Build leaf-name → list of (canonical_id, canonical_name) pairs.
    leaf_index: dict[str, list[tuple[str, str]]] = {}
    for nid, attrs in g.nodes(data=True):
        if attrs.get("type") not in _CANONICAL_TYPES:
            continue
        name = attrs.get("name") or ""
        if not name:
            continue
        leaf = name.rsplit(".", 1)[-1]
        leaf_index.setdefault(leaf, []).append((nid, name))

    removed = 0
    for nid in list(g.nodes):
        attrs = g.nodes[nid]
        ntype = attrs.get("type", "")
        if ntype not in _PHANTOM_TYPES:
            continue
        name = attrs.get("name") or ""
        if not name:
            continue

        leaf = name.rsplit(".", 1)[-1]
        all_candidates = [
            (cid, cname)
            for cid, cname in leaf_index.get(leaf, [])
            if cid != nid
        ]

        if "." in name:
            # Qualified phantom — filter by module-path overlap so
            # cross-module same-leaf collisions don't collapse.
            matched = [
                (cid, cname)
                for cid, cname in all_candidates
                if _module_paths_overlap(name, cname)
            ]
        else:
            # Bare-name phantom — the phantom has no module path, so
            # fall back to "unique leaf match across the whole graph".
            matched = list(all_candidates)

        if not matched:
            continue

        # Pick the best canonical by type-rank + file_path.
        matched.sort(key=lambda pair: _canonical_rank_key(
            pair[0], pair[1], g.nodes[pair[0]],
        ))
        # If multiple candidates share the best rank AND file_path
        # status, the leaf is ambiguous — skip rather than guess.
        best_key = _canonical_rank_key(
            matched[0][0], matched[0][1], g.nodes[matched[0][0]],
        )
        tied = [
            pair for pair in matched
            if _canonical_rank_key(pair[0], pair[1], g.nodes[pair[0]])[:2]
            == best_key[:2]
        ]
        if len(tied) > 1:
            continue

        canonical, _canonical_name = matched[0]
        for src, _, key, data in list(g.in_edges(nid, keys=True, data=True)):
            g.add_edge(src, canonical, **data)
            g.remove_edge(src, nid, key=key)
        for _, tgt, key, data in list(g.out_edges(nid, keys=True, data=True)):
            g.add_edge(canonical, tgt, **data)
            g.remove_edge(nid, tgt, key=key)
        g.remove_node(nid)
        removed += 1

    if removed:
        logger.info(
            "Canonicalized %d re-export / mis-typed phantom nodes", removed,
        )
    return removed


def _classify_phantom_nodes(graph: KnowledgeGraph) -> None:
    """Add type/name attributes to nodes auto-created by NetworkX.

    When add_edge references a node that doesn't exist, NetworkX creates
    it with no attributes. We classify these as EXTERNAL (stdlib/packages)
    or leave as-is for project-internal symbols.
    """
    from sphinxcontrib.nexus.extractors import _EXTERNAL_NAMES

    g = graph.nxgraph
    for node_id in list(g.nodes):
        attrs = g.nodes[node_id]
        if attrs.get("type") and attrs["type"] not in ("", "unknown"):
            continue  # already classified

        # Extract name from node ID: "py:function:numpy.array" → "numpy.array"
        parts = node_id.split(":", 2)
        name = parts[2] if len(parts) == 3 else node_id
        top_level = name.split(".")[0]

        if top_level in _EXTERNAL_NAMES:
            node_type = NodeType.EXTERNAL.value
        else:
            node_type = NodeType.UNRESOLVED.value

        attrs["type"] = node_type
        attrs["name"] = name
        attrs["display_name"] = name
        attrs["domain"] = parts[0] if len(parts) >= 2 else "py"
        attrs["source"] = "ast_inferred"
