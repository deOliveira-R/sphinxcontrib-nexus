"""AST-based Python source code analyzer.

Extracts code-level relationships (calls, imports, inheritance, type usage)
from Python source files and writes them to the same graph as Sphinx extraction.

No Sphinx dependency — usable standalone via CLI.
"""

from __future__ import annotations

import ast
import logging
import re
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

# Regex for Sphinx cross-reference roles in docstrings
_SPHINX_ROLE_RE = re.compile(r":(\w+):`~?([^`]+)`")


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

    def __init__(self, module_name: str, file_path: str) -> None:
        self._module_name = module_name
        self._file_path = file_path
        self._scope: list[str] = [module_name]
        self._imports = ImportTracker(module_name)
        self.nodes: list[GraphNode] = []
        self.edges: list[GraphEdge] = []

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
        """Visit only direct body statements of the module."""
        for child in node.body:
            self.visit(child)

    def _node_id(self, node_type: str, name: str) -> str:
        return f"py:{node_type}:{name}"

    def _add_docstring_refs(self, node: ast.AST, source_id: str) -> None:
        """Extract Sphinx role references from docstring."""
        docstring = ast.get_docstring(node)
        if not docstring:
            return
        for match in _SPHINX_ROLE_RE.finditer(docstring):
            role, target = match.group(1), match.group(2)
            target = target.lstrip("~")  # remove tilde prefix
            resolved = self._imports.resolve(target)
            # Map role to node type prefix
            type_map = {
                "func": "function", "meth": "method", "class": "class",
                "mod": "module", "attr": "attribute", "data": "data",
            }
            obj_type = type_map.get(role, role)
            target_id = f"py:{obj_type}:{resolved}"
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

        self.nodes.append(GraphNode(
            id=class_id,
            type=NodeType.CLASS,
            name=qname,
            display_name=node.name,
            domain="py",
            metadata={
                "file_path": self._file_path,
                "lineno": node.lineno,
                "end_lineno": node.end_lineno,
                "source": "ast",
            },
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

        is_test = node.name.startswith("test_") or node.name.startswith("test")
        self.nodes.append(GraphNode(
            id=func_id,
            type=node_type,
            name=qname,
            display_name=node.name,
            domain="py",
            metadata={
                "file_path": self._file_path,
                "lineno": node.lineno,
                "end_lineno": node.end_lineno,
                "source": "ast",
                **({"is_test": True} if is_test else {}),
            },
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


def analyze_directory(
    source_dir: Path,
    project_root: Path | None = None,
    sys_path_dirs: list[Path] | None = None,
    exclude_patterns: list[str] | None = None,
) -> KnowledgeGraph:
    """Analyze all Python files in a directory and return a KnowledgeGraph.

    Args:
        source_dir: Directory to scan for .py files.
        project_root: Root for module name resolution. Defaults to source_dir.
        sys_path_dirs: Extra directories on the Python path.
        exclude_patterns: Glob patterns to exclude (default: tests, docs, venv).
    """
    if project_root is None:
        project_root = source_dir
    if exclude_patterns is None:
        exclude_patterns = ["docs/*", ".venv/*", "__pycache__/*"]

    resolver = ModuleResolver(project_root, sys_path_dirs)
    graph = KnowledgeGraph()

    # Pre-compute exclusion directory names for fast filtering
    _skip_dirs = {".venv", "venv", "__pycache__", "node_modules", ".tox", ".git"}
    py_files = sorted(source_dir.rglob("*.py"))
    for filepath in py_files:
        # Skip files under excluded directories
        if _skip_dirs & set(filepath.parts):
            continue
        rel = str(filepath.relative_to(source_dir))
        if any(filepath.match(pat) for pat in exclude_patterns):
            continue

        try:
            source = filepath.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(filepath))
        except (SyntaxError, UnicodeDecodeError) as e:
            logger.warning("Skipping %s: %s", filepath, e)
            continue

        module_name = resolver.file_to_module(filepath)
        visitor = CodeVisitor(module_name, str(filepath))
        visitor.visit(tree)

        for node in visitor.nodes:
            graph.add_node(node)
        for edge in visitor.edges:
            graph.add_edge(edge)

    # Classify phantom nodes created by add_edge for targets not in the graph.
    # These are external functions/modules (numpy.array, scipy.integrate.quad, etc.)
    _classify_phantom_nodes(graph)

    logger.info(
        "AST analysis: %d nodes, %d edges from %d files",
        graph.node_count, graph.edge_count, len(py_files),
    )
    return graph


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
