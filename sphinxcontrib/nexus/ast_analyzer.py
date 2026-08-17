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
from typing import Any, Iterator

from sphinxcontrib.nexus._mappings import (
    _DOTTED_TARGET_RE,
    _normalize_wrapped_target,
    PLACEHOLDER_TYPES,
    REFTYPE_OBJTYPE_MAP,
    candidate_rank,
    candidates_are_ambiguous,
    test_node_is_off_limits,
)
from sphinxcontrib.nexus.fingerprint import body_fingerprint
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
# presentation noise. DOTALL because a docstring role body wraps
# across lines (``:meth:`Foo.bar\n    <pkg.mod.Foo.bar>```) and the
# title part must still match up to the ``<``.
_ROLE_TITLE_TARGET_RE = re.compile(r"^.*?<(?P<target>[^>]+)>\s*$", re.DOTALL)



def _is_dotted_identifier(name: str) -> bool:
    """True when ``name`` is a well-formed dotted Python path."""
    return bool(name) and all(part.isidentifier() for part in name.split("."))


#: ``:label: <name>`` option line of a ``.. math::`` directive embedded
#: in a docstring. Sphinx only learns equation labels from pages it
#: RENDERS — a label defined in a never-automodule'd docstring is
#: invisible to it, and every ``:eq:`` reference to that label would
#: look dead. The AST layer reads all docstrings, so it extracts the
#: definitions too.
_MATH_LABEL_RE = re.compile(r"^\s*:label:\s*(\S+)\s*$", re.MULTILINE)


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
        # A leading ``.`` marks a Sphinx relative/suffix-match target
        # (``:meth:`.Quadrature.product```); the dots themselves are
        # display syntax, not part of the dotted path.
        inner = _normalize_wrapped_target(inner).lstrip(".")
        return inner or None

    # Plain target, possibly with a leading ``~`` display hint.
    if stripped.startswith("~"):
        stripped = stripped[1:]
    stripped = _normalize_wrapped_target(stripped)
    # Same relative-target convention as the title form above.
    if stripped.startswith(".") and _DOTTED_TARGET_RE.fullmatch(stripped.lstrip(".")):
        stripped = stripped.lstrip(".")
    return stripped or None


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
    contains anything else (calls, subscripts, etc.).

    ``None`` is the load-bearing half of the contract, not an edge case.
    A chain rooted in a run-time value — ``get_thing().method``,
    ``items[0].method``, ``"".join`` — names nothing a static reader can
    resolve, and the truncated tail (``"method"``, ``"join"``) is a
    *different symbol* that merely shares a leaf.

    Returning that tail is how call resolution invented edges: until
    2026-08-16 a twin of this function, ``_unparse_attribute``, dropped an
    unresolvable root and returned the tail, which the phantom folder then
    bound to whichever unrelated symbol owned that leaf. It failed in the
    false-ALIVE direction — inventing callers, so ``impact``/``retest``
    over-report and ``dead_functions`` cannot flag a symbol whose only
    "callers" are fabricated. Measured on ORPHEUS's graph before the twin
    was retired: 510 attributed ``calls`` edges came from such sites, 85
    landed on a real indexed symbol, and **62 symbols had no incoming call
    edge that was not fabricated** — including a self-loop claiming
    ``SumOfTensorProductsOperator.apply`` calls itself.
    """
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

    def __init__(self, module_name: str, is_package: bool = False) -> None:
        self._module_name = module_name
        self._is_package = is_package
        self._aliases: dict[str, str] = {}
        self._has_future_annotations = False

    @property
    def has_future_annotations(self) -> bool:
        return self._has_future_annotations

    def relative_anchor(self, level: int) -> str:
        """The package a ``level``-dot relative import resolves against.

        One dot means the CURRENT package: the module itself when it is
        a package (``__init__.py``), its parent otherwise — getting this
        wrong drops a path segment from every symbol imported relatively
        inside a nested package's ``__init__.py``. Each additional dot
        climbs one parent. Clamps at the top level rather than raising
        on a malformed over-deep import.
        """
        anchor = self._module_name
        climbs = level - 1 if self._is_package else level
        for _ in range(climbs):
            if "." not in anchor:
                break
            anchor = anchor.rsplit(".", 1)[0]
        return anchor

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
            base = self.relative_anchor(node.level)
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
        full = _dotted_name(node)
        return [imports.resolve(full)] if full is not None else []

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


def _resolve_call_target(node: ast.Call, imports: ImportTracker) -> str | None:
    """The name a call site names, resolved through imports — or ``None``.

    ``None`` means *this call names no static target*, which covers both a
    callee that is a run-time value (``get_thing().method()``) and
    ``self.method()``, whose enclosing class the visitor supplies.
    """
    full = _dotted_name(node.func)
    if full is None:
        return None  # the callee is a run-time value; claim no edge
    if full.startswith("self."):
        return None  # handled specially in the visitor
    return imports.resolve(full)


# ---------------------------------------------------------------------------
# Tag discrimination — "a repeated conditional is a missing type"
# ---------------------------------------------------------------------------
#
# A function that branches on a string/enum *tag* (``if geometry ==
# "spherical"``, ``match kind:``) is discriminating on that tag. Recording
# ``function --discriminates_on--> tag`` makes the coding-elegance smell
# queryable: the SAME tag discriminated at many sites is a missing type /
# absent single dispatch. The edge records one site; repetition is counted
# by the query (fan-in), so detection stays cheap and local.


def _discriminant_name(node: ast.expr) -> str | None:
    """Leaf name of a discriminant (plain variable or attribute).

    ``self.geometry`` and ``mesh.geometry`` both reduce to ``geometry`` so
    sites discriminating on the same concept share one tag node. Call
    results (``x.kind()``) are not discriminants.
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _tag_literal(node: ast.expr) -> str | None:
    """The case label if ``node`` is a string literal or an enum member."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    # Enum member access — a Capitalized base, e.g. Geometry.SPHERICAL.
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id[:1].isupper()
    ):
        return f"{node.value.id}.{node.attr}"
    return None


def _if_discrimination(test: ast.expr) -> tuple[str, tuple[str, ...]] | None:
    """``(tag, cases)`` if ``test`` is a tag comparison, else ``None``.

    Recognizes ``x == "lit"`` / ``x == Enum.MEMBER`` and ``x in
    ("a", "b")`` (plus the ``not in`` form), where ``x`` is a name or
    attribute. ``elif`` chains are separate ``ast.If`` nodes and are walked
    by the caller.
    """
    if not isinstance(test, ast.Compare) or len(test.ops) != 1:
        return None
    name = _discriminant_name(test.left)
    if name is None:
        return None
    op = test.ops[0]
    right = test.comparators[0]
    if isinstance(op, ast.Eq):
        lit = _tag_literal(right)
        if lit is not None:
            return name, (lit,)
    elif isinstance(op, (ast.In, ast.NotIn)) and isinstance(
        right, (ast.Tuple, ast.List, ast.Set)
    ):
        labels = [_tag_literal(e) for e in right.elts]
        if labels and all(lab is not None for lab in labels):
            return name, tuple(lab for lab in labels if lab is not None)
    return None


def _case_patterns(pattern: ast.pattern):
    """Flatten ``case A | B`` alternatives into their leaf patterns."""
    if isinstance(pattern, ast.MatchOr):
        for sub in pattern.patterns:
            yield from _case_patterns(sub)
    else:
        yield pattern


def _match_discrimination(node: ast.Match) -> tuple[str, tuple[str, ...]] | None:
    """``(tag, cases)`` if a ``match`` dispatches a name/attribute on
    literal/enum/class patterns; ``None`` for a pure capture/wildcard match."""
    name = _discriminant_name(node.subject)
    if name is None:
        return None
    cases: set[str] = set()
    matched = False
    for case in node.cases:
        for pattern in _case_patterns(case.pattern):
            if isinstance(pattern, ast.MatchValue):
                lit = _tag_literal(pattern.value)
                if lit is not None:
                    cases.add(lit)
                matched = True
            elif isinstance(pattern, ast.MatchClass):
                matched = True
    if not matched:
        return None
    return name, tuple(sorted(cases))


def _discriminated_tags(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
) -> dict[str, tuple[str, ...]]:
    """Map each tag discriminated in ``func`` to its case labels.

    Walks the whole function body, matching the analyzer's CALLS model:
    nested functions are not separate nodes here, so a nested helper's tag
    dispatch is attributed to the enclosing function (just as its calls
    are). Multiple sites for one tag union their cases.
    """
    found: dict[str, set[str]] = {}
    for node in ast.walk(func):
        result = None
        if isinstance(node, ast.If):
            result = _if_discrimination(node.test)
        elif isinstance(node, ast.Match):
            result = _match_discrimination(node)
        if result is not None:
            tag, cases = result
            found.setdefault(tag, set()).update(cases)
    return {tag: tuple(sorted(cases)) for tag, cases in found.items()}


# ---------------------------------------------------------------------------
# CodeVisitor — single-pass AST visitor per file
# ---------------------------------------------------------------------------


def attribute_comments(source: str) -> dict[int, str]:
    """Map each ``#:``-documented statement's line to its comment prose.

    Sphinx's attribute-comment form documents the statement below it::

        #: Spatial axis names, positional-by-axis — the crosswalk that
        #: :class:`FaceLayout` and :attr:`SNMesh.bc` both key on.
        AXIS_NAMES = ("x", "y", "z")

    That prose carries real cross-references (168 py-domain roles on
    ORPHEUS) and ``ast`` discards comments outright, so the token stream
    is the only way to see it. ``tokenize`` rather than a line regex
    specifically so a ``#:`` inside a string literal is not mistaken for
    an attribute comment.

    A trailing form (``x = 1  #: doc``) documents the statement it sits
    on; a leading block documents the next one. Both are returned keyed
    by the documented statement's line number.
    """
    import io
    import tokenize as _tokenize

    result: dict[int, str] = {}
    block: list[str] = []
    block_start_seen = False
    try:
        tokens = list(_tokenize.generate_tokens(io.StringIO(source).readline))
    except (_tokenize.TokenError, IndentationError, SyntaxError):
        # Malformed source: ast.parse already reports it. Losing the
        # comments is strictly better than failing the whole analysis.
        return result

    prev_end_row = 0
    for tok in tokens:
        if tok.type == _tokenize.COMMENT:
            text = tok.string
            if not text.startswith("#:"):
                block = []
                block_start_seen = False
                continue
            prose = text[2:].strip()
            # A comment sharing a line with code is a trailing comment:
            # it documents THAT line, not the next statement.
            if tok.start[1] > 0 and tok.line[:tok.start[1]].strip():
                result[tok.start[0]] = prose
                block = []
                block_start_seen = False
            else:
                block.append(prose)
                block_start_seen = True
        elif tok.type in (_tokenize.NL, _tokenize.COMMENT):
            continue
        elif tok.type in (_tokenize.NEWLINE, _tokenize.INDENT,
                          _tokenize.DEDENT, _tokenize.ENDMARKER):
            continue
        elif block_start_seen:
            # First real token after a ``#:`` block — the statement it
            # documents.
            result[tok.start[0]] = " ".join(block)
            block = []
            block_start_seen = False
        prev_end_row = tok.end[0]
    return result


def _iter_name_targets(target: ast.expr) -> Iterator[str]:
    """Yield plain names bound by an assignment target.

    Handles ``x = ...``, ``x, y = ...`` and ``[x, y] = ...``. Attribute
    and subscript targets are someone else's binding, not a new name in
    this scope.
    """
    if isinstance(target, ast.Name):
        yield target.id
    elif isinstance(target, (ast.Tuple, ast.List)):
        for elt in target.elts:
            if isinstance(elt, ast.Name):
                yield elt.id


def _iter_self_attr_names(stmt: ast.Assign | ast.AnnAssign) -> Iterator[str]:
    """Yield attribute names bound on ``self`` by an assignment.

    Covers ``self.x = ...``, ``self.x: T = ...`` and tuple unpacking
    ``self.a, self.b = ...``.
    """
    targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
    for tgt in targets:
        elts = tgt.elts if isinstance(tgt, (ast.Tuple, ast.List)) else [tgt]
        for elt in elts:
            if (
                isinstance(elt, ast.Attribute)
                and isinstance(elt.value, ast.Name)
                and elt.value.id == "self"
            ):
                yield elt.attr


class CodeVisitor(ast.NodeVisitor):
    """Walk a Python file's AST and extract nodes and edges."""

    def __init__(
        self,
        module_name: str,
        file_path: str,
        is_test_file: bool = False,
        attr_comments: dict[int, str] | None = None,
    ) -> None:
        self._module_name = module_name
        self._file_path = file_path
        self._is_test_file = is_test_file
        self._scope: list[str] = [module_name]
        self._imports = ImportTracker(
            module_name,
            is_package=file_path.endswith("__init__.py"),
        )
        self.nodes: list[GraphNode] = []
        self.edges: list[GraphEdge] = []
        # Pytest-marker metadata stashed at module and class scope.
        # When a function is visited, these layer underneath its own
        # decorator metadata (module lowest, function highest precedence).
        self._module_pytest_meta: dict[str, object] = {}
        self._current_class_pytest_meta: dict[str, object] = {}
        # Synthetic tag nodes are shared across the functions that
        # discriminate on them; emit each at most once per file.
        self._tags_emitted: set[str] = set()
        # Attribute/data binding nodes already emitted, keyed by node
        # id — a class-level annotation and a ``self.x = ...`` in
        # ``__init__`` describe the same attribute once.
        self._bindings_emitted: set[str] = set()
        # Depth of the class-scope stack. Assignment visitors use it to
        # tell module-level bindings (DATA) from class-level ones
        # (ATTRIBUTE). Function bodies are never visited statement-wise
        # (only ``ast.walk``-ed for calls), so these visitors cannot
        # fire at function scope.
        self._class_depth = 0
        # Indices into `_scope` that are CLASS scopes. The structural
        # answer to "am I inside a class?", which `_current_class` used
        # to guess from capitalisation — see its docstring for what that
        # cost. `_class_depth` counts; this one remembers WHERE, which
        # is what naming the enclosing class requires.
        self._class_scopes: list[int] = []
        # Classes defined in THIS module: local name → qualified name.
        # Used to bind ``Cls.attr = ...`` statements that appear after
        # the class body back onto the class they extend. Restricted to
        # locally-defined classes on purpose — ``mod.CONST = 1`` on an
        # imported module is somebody else's namespace, not ours.
        self._classes_defined: dict[str, str] = {}
        # Line number of a ``#:``-documented statement → its prose.
        # Comments are gone from the AST, so this is pre-scanned from
        # the token stream and joined back on by line.
        self._attr_comments: dict[int, str] = attr_comments or {}
        # Module-scope ``from X import Y`` aliases: public dotted path
        # → defining dotted path. ``analyze_directory`` aggregates
        # these into graph metadata so phantom canonicalization can
        # chase re-export chains.
        self.reexports: dict[str, str] = {}

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
        """The innermost enclosing CLASS scope, or ``None`` outside one.

        ⛔ This used to answer "is this scope a class?" with
        ``scope[i][0:1].isupper()`` — a NAMING CONVENTION standing in
        for a structural fact the visitor already holds, since
        :meth:`visit_ClassDef` is what pushed the scope. Any class whose
        name does not start with a capital was invisible, and a leading
        underscore is the common case: `[M]` on ORPHEUS 2026-08-16 it
        cost **195** methods their type. They were emitted as
        ``py:function:pkg._Private.meth``, and because the call resolver
        forms method edges only into ``py:method:`` ids, **191 of the
        195 had zero incoming calls**.

        The damage was not confined to typing. ``callers`` answered 0
        for `_OneDimScanWalk._run`, which is called one frame away at
        ``loss_representation/__init__.py:2986``; an explorer asking
        "what breaks if the per-cell operator's signature changes" got
        **0 of ~10** production sites from the graph and all of them
        from grep. ``dead_functions`` lists such a method as a removal
        candidate, and ``retest`` walks a cone that cannot reach it.

        ⚠ A zero that means UNRESOLVABLE must never print identically to
        a zero that means UNCALLED — this one did, in the direction that
        reads as "safe to delete".
        """
        if not self._class_scopes:
            return None
        return ".".join(self._scope[: self._class_scopes[-1] + 1])

    def visit_Module(self, node: ast.Module) -> None:
        """Visit only direct body statements of the module.

        Before walking, scan for a top-level ``pytestmark = ...``
        assignment and stash its parsed markers as the module-level
        default. Contained functions and methods layer this underneath
        their own markers (module < class < function).
        """
        self._module_pytest_meta = _collect_pytestmark_assignments(node.body)
        # The module docstring carries references and equation-label
        # definitions like any other docstring — ORPHEUS derivation
        # modules keep the entire ``.. math:: :label:`` derivation in
        # module-level prose.
        self._add_docstring_refs(
            node, self._node_id("module", self._module_name),
        )
        for child in node.body:
            self.visit(child)

    def _node_id(self, node_type: str, name: str) -> str:
        return f"py:{node_type}:{name}"

    def _add_docstring_refs(
        self,
        node: ast.AsyncFunctionDef | ast.FunctionDef | ast.ClassDef | ast.Module,
        source_id: str,
    ) -> None:
        """Extract Sphinx role references from a docstring."""
        docstring = ast.get_docstring(node)
        if docstring:
            self._add_text_refs(docstring, source_id)

    def _add_text_refs(self, docstring: str, source_id: str) -> None:
        """Extract Sphinx role references from documentation prose.

        Python-domain roles produce ``py:<objtype>:<name>`` target IDs that
        reconcile against AST-discovered symbols. The math roles ``:math:``
        and ``:eq:`` instead point at Sphinx math equation labels in the
        ``math:equation:<label>`` namespace, which is what Sphinx's math
        extractor produces for ``.. math:: :label: foo`` blocks.

        Takes text rather than a node because docstrings are not the only
        place documentation prose lives — ``#:`` attribute comments carry
        the same roles and are invisible to ``ast`` entirely.
        """

        # Equation labels DEFINED in this docstring (``.. math::``
        # with ``:label:``). Emitted as concrete equation nodes so
        # ``:eq:`` references to them resolve regardless of whether
        # Sphinx ever renders the docstring.
        for label_match in _MATH_LABEL_RE.finditer(docstring):
            label = label_match.group(1)
            eq_id = f"math:equation:{label}"
            self.nodes.append(GraphNode(
                id=eq_id,
                type=NodeType.EQUATION,
                name=label,
                display_name=label,
                domain="math",
                metadata={"file_path": self._file_path, "source": "ast"},
            ))
            self.edges.append(GraphEdge(
                source=source_id, target=eq_id, type=EdgeType.CONTAINS,
                metadata={"source": "ast"},
            ))

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
            elif role in REFTYPE_OBJTYPE_MAP:
                resolved = self._imports.resolve(target)
                if not _is_dotted_identifier(resolved):
                    # Not a well-formed dotted path even after
                    # wrap-normalization — forging a node for it
                    # would create a phantom nothing can resolve.
                    continue
                target_id = f"py:{REFTYPE_OBJTYPE_MAP[role]}:{resolved}"
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
                base = self._imports.relative_anchor(node.level)
                target_module = base.split(".")[0] if base else target_module
            self.edges.append(GraphEdge(
                source=module_id,
                target=self._node_id("module", target_module),
                type=EdgeType.IMPORTS,
                metadata={"full_import": node.module, "source": "ast"},
            ))
        # Record module-scope aliases as re-export candidates:
        # ``from .mesh import Thing`` in ``pkg/__init__.py`` makes
        # ``pkg.Thing`` a live public path for ``pkg.mesh.Thing``.
        # ImportTracker has just registered the alias, so resolving
        # the local name yields the defining dotted path.
        if self._class_depth == 0 and node.module != "__future__":
            for alias in node.names:
                if alias.name == "*":
                    continue
                local = alias.asname or alias.name
                qualified = self._imports.resolve(local)
                public = f"{self._module_name}.{local}"
                if qualified != local and qualified != public:
                    self.reexports[public] = qualified

    def _emit_binding(
        self,
        name: str,
        lineno: int,
        annotation: str | None = None,
    ) -> None:
        """Emit an ATTRIBUTE (class scope) or DATA (module scope) node.

        These bindings are what ``:attr:`` / ``:data:`` doc references
        point at; without them every live reference to an annotated
        attribute or module constant is indistinguishable from a
        reference to deleted code.
        """
        if name == "_":
            return
        owner = self._qualified_name
        if self._class_depth > 0:
            type_str = "attribute"
            node_type = NodeType.ATTRIBUTE
            parent_id = self._node_id("class", owner)
        else:
            type_str = "data"
            node_type = NodeType.DATA
            parent_id = self._node_id("module", owner)
        qname = f"{owner}.{name}"
        binding_id = self._node_id(type_str, qname)
        if binding_id in self._bindings_emitted:
            return
        self._bindings_emitted.add(binding_id)

        meta: dict[str, object] = {
            "file_path": self._file_path,
            "lineno": lineno,
            "source": "ast",
        }
        if self._is_test_file:
            meta["is_test"] = True
        if annotation:
            meta["annotation"] = annotation
        self.nodes.append(GraphNode(
            id=binding_id,
            type=node_type,
            name=qname,
            display_name=name,
            domain="py",
            metadata=meta,
        ))
        self.edges.append(GraphEdge(
            source=parent_id, target=binding_id, type=EdgeType.CONTAINS,
            metadata={"source": "ast"},
        ))
        self._attach_attr_comment(binding_id, lineno)

    def _attach_attr_comment(self, binding_id: str, lineno: int) -> None:
        """Hang a ``#:`` comment block's prose on the binding it documents.

        The references inside it are indistinguishable from docstring
        references once extracted, so they go through the same path.
        """
        prose = self._attr_comments.get(lineno)
        if prose:
            self._add_text_refs(prose, binding_id)

    def _emit_instance_attribute(
        self,
        cls_qname: str,
        name: str,
        lineno: int,
        annotation: str | None = None,
    ) -> None:
        """Emit an ATTRIBUTE node for a ``self.<name>`` binding.

        Same node namespace as class-level bindings, so an annotated
        declaration and the ``__init__`` assignment collapse into one
        node. Also used for ``Cls.attr = ...`` bound after the class
        body, which lands in the same namespace for the same reason.
        """
        attr_id = self._node_id("attribute", f"{cls_qname}.{name}")
        if attr_id in self._bindings_emitted:
            return
        self._bindings_emitted.add(attr_id)
        meta: dict[str, object] = {
            "file_path": self._file_path,
            "lineno": lineno,
            "source": "ast",
        }
        if self._is_test_file:
            meta["is_test"] = True
        if annotation:
            meta["annotation"] = annotation
        self.nodes.append(GraphNode(
            id=attr_id,
            type=NodeType.ATTRIBUTE,
            name=f"{cls_qname}.{name}",
            display_name=name,
            domain="py",
            metadata=meta,
        ))
        self.edges.append(GraphEdge(
            source=self._node_id("class", cls_qname),
            target=attr_id,
            type=EdgeType.CONTAINS,
            metadata={"source": "ast"},
        ))
        self._attach_attr_comment(attr_id, lineno)

    def _emit_post_class_attribute(
        self,
        target: ast.expr,
        lineno: int,
        annotation: str | None = None,
    ) -> bool:
        """Bind ``Cls.attr = ...`` written after the class body.

        The standard way to build enum-like singletons on a non-Enum
        class, and how a lot of code registers defaults and sentinels::

            @dataclass(frozen=True)
            class BC:
                kind: str

            BC.vacuum = BC("vacuum")      # type: ignore[attr-defined]

        These are real class attributes at import time — autodoc picks
        them up and ``:data:`BC.vacuum``` renders as a working link — but
        the class body holds no trace of them, so the graph reported
        every such reference as dead. The ``type: ignore`` markers are a
        good tell: the author already knows static tools cannot see it.

        Returns True when the target was handled as a class attribute.
        """
        if not isinstance(target, ast.Attribute):
            return False
        owner = target.value
        if not isinstance(owner, ast.Name):
            return False
        cls_qname = self._classes_defined.get(owner.id)
        if cls_qname is None:
            return False
        self._emit_instance_attribute(
            cls_qname, target.attr, lineno, annotation=annotation,
        )
        return True

    def visit_Assign(self, node: ast.Assign) -> None:
        for tgt in node.targets:
            if self._emit_post_class_attribute(tgt, node.lineno):
                continue
            for name in _iter_name_targets(tgt):
                self._emit_binding(name, node.lineno)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        try:
            annotation = ast.unparse(node.annotation)
        except Exception:
            annotation = None
        if self._emit_post_class_attribute(
            node.target, node.lineno, annotation=annotation,
        ):
            return
        if isinstance(node.target, ast.Name):
            self._emit_binding(
                node.target.id, node.lineno, annotation=annotation,
            )

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scope.append(node.name)
        self._class_scopes.append(len(self._scope) - 1)
        qname = self._qualified_name
        class_id = self._node_id("class", qname)
        # Register before visiting the body so a later ``Cls.attr = ...``
        # can find it. Only top-level classes are addressable by a bare
        # name at module scope, which is the form this binds.
        if self._class_depth == 0:
            self._classes_defined[node.name] = qname

        class_meta: dict[str, object] = {
            "file_path": self._file_path,
            "lineno": node.lineno,
            "end_lineno": node.end_lineno,
            "source": "ast",
        }
        # Any class in a test module is test code (test doubles / fakes that
        # conform to a Protocol have arbitrary names, so unlike test
        # functions there is no name convention to key on — the file is the
        # signal). Lets class-level diagnostics drop the test tree by default.
        if self._is_test_file:
            class_meta["is_test"] = True
        if node.decorator_list:
            class_meta["decorators"] = tuple(
                _render_decorator(dec) for dec in node.decorator_list
            )
            class_meta["decorator_lineno"] = min(
                dec.lineno for dec in node.decorator_list
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
            # A generic base ``Composite[Bulk, Boundary]`` inherits
            # from ``Composite`` — dropping Subscript bases severed
            # the INHERITS chain for every Generic-parameterized
            # class, which broke inherited-member resolution.
            if isinstance(base, ast.Subscript):
                base = base.value
            if isinstance(base, ast.Name):
                base_name = self._imports.resolve(base.id)
            elif isinstance(base, ast.Attribute):
                dotted = _dotted_name(base)
                if dotted is None:
                    continue  # a run-time base class names no target
                base_name = self._imports.resolve(dotted)
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
        self._class_depth += 1
        for child in node.body:
            self.visit(child)
        self._class_depth -= 1
        self._class_scopes.pop()
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
            # Where the definition's SOURCE starts, which is not where its
            # `def` is. CPython records this as `co_firstlineno`, so it is
            # the line every tracer reports for a decorated function —
            # keeping only the rendered decorator NAMES threw away the one
            # number the runtime join needs, and left it guessing with a
            # fixed-width window. See `position.Definition`.
            meta["decorator_lineno"] = min(
                dec.lineno for dec in node.decorator_list
            )
            meta.update(_parse_pytest_markers(node.decorator_list))

        # Structural body fingerprint — the seed for twin-path (clone)
        # detection. Stored only when the body has enough substance to
        # compare; trivial stubs carry no shingles.
        body_shingles, body_ntokens = body_fingerprint(node)
        if body_shingles:
            meta["body_shingles"] = body_shingles
            meta["body_ntokens"] = body_ntokens

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

        # DISCRIMINATES_ON — tags this function branches on (one edge per
        # tag; the query counts cross-function fan-in).
        for tag, cases in _discriminated_tags(node).items():
            tag_id = self._node_id("tag", tag)
            if tag_id not in self._tags_emitted:
                self._tags_emitted.add(tag_id)
                self.nodes.append(GraphNode(
                    id=tag_id,
                    type=NodeType.TAG,
                    name=tag,
                    display_name=tag,
                    domain="py",
                    metadata={"source": "ast"},
                ))
            self.edges.append(GraphEdge(
                source=func_id, target=tag_id, type=EdgeType.DISCRIMINATES_ON,
                metadata={"source": "ast", "cases": cases},
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

        # Walk body for CALLS edges and, in methods, instance
        # attributes bound on ``self``.
        cls_qname = ".".join(self._scope[:-1]) if is_method else None
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
            elif cls_qname and isinstance(child, (ast.Assign, ast.AnnAssign)):
                # ``self.x = ...`` declares an instance attribute of
                # the enclosing class — the target of ``:attr:`` doc
                # references, so it must exist as a graph node.
                for attr_name in _iter_self_attr_names(child):
                    self._emit_instance_attribute(
                        cls_qname, attr_name, child.lineno,
                    )

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


def _nested_git_trees(source_dir: Path) -> set[Path]:
    """Roots of git working trees nested INSIDE ``source_dir``.

    A directory carrying a ``.git`` entry — a directory for nested
    clones, a gitlink file for linked worktrees and submodules — is a
    DIFFERENT checkout: its files are never part of this project's
    import tree and must not contribute nodes to this project's graph.
    The canonical instance is Claude Code session worktrees under
    ``.claude/worktrees/<name>``, each holding a full copy of the
    project source; without this pruning they duplicate every symbol
    under mangled module paths (observed on ORPHEUS: 51% of all graph
    nodes were worktree copies). ``source_dir`` itself is exempt —
    being a repository root is normal.
    """
    return {
        git_entry.parent
        for git_entry in source_dir.rglob(".git")
        if git_entry.parent != source_dir
    }


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
    # Public dotted path → defining dotted path, from module-scope
    # ``from X import Y`` statements. Persisted in graph metadata so
    # both the in-pipeline canonicalization passes and query-time
    # consumers can chase re-export chains.
    reexports: dict[str, str] = {}

    # Pre-compute exclusion directory names for fast filtering
    _skip_dirs = {".venv", "venv", "__pycache__", "node_modules", ".tox", ".git"}
    nested_trees = _nested_git_trees(source_dir)
    py_files = sorted(source_dir.rglob("*.py"))
    for filepath in py_files:
        # Skip files under excluded directories
        if _skip_dirs & set(filepath.parts):
            continue
        # Skip files inside nested git working trees (worktrees,
        # vendored clones, submodules) — foreign checkouts, not this
        # project's import tree.
        if nested_trees and not nested_trees.isdisjoint(filepath.parents):
            continue
        rel = filepath.relative_to(source_dir).as_posix()
        try:
            rel_to_root = filepath.relative_to(project_root).as_posix()
        except ValueError:
            rel_to_root = rel
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
        # Test patterns are written project-relative (``tests/*``), but
        # ``rel`` is relative to the SOURCE DIR — and when that dir is
        # itself the test root (``nexus_extra_source_dirs = ['tests']``)
        # the ``tests/`` prefix is gone, so ``tests/*`` cannot match and
        # only the ``test_*.py`` filename patterns fire. Helper modules
        # inside the test tree (``_harness/registry.py``, snapshot
        # generators) were therefore never flagged: 2113 of ORPHEUS's
        # test-tree nodes carried no ``is_test``. Match both spellings.
        is_test_file = any(
            fnmatch(rel, pat) or fnmatch(rel_to_root, pat)
            for pat in test_patterns
        )
        visitor = CodeVisitor(
            module_name, str(filepath), is_test_file=is_test_file,
            attr_comments=attribute_comments(source),
        )
        visitor.visit(tree)

        if is_test_file:
            # ``is_test`` means "this IS a test" — name-based for
            # functions, so a helper like ``_harness.registry.record``
            # never carries it, and ``retest`` / ``dead_functions``
            # depend on that meaning. ``in_test_file`` is the different
            # question — "does this live in the test tree?" — which is
            # what fuzzy name matching needs in order not to let a test
            # helper absorb a reference from production code.
            for node in visitor.nodes:
                node.metadata["in_test_file"] = True

        for node in visitor.nodes:
            graph.add_node(node)
        for edge in visitor.edges:
            graph.add_edge(edge)
        reexports.update(visitor.reexports)

    graph.metadata["reexports"] = reexports

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
    NodeType.TYPE.value,
    NodeType.ATTRIBUTE.value,
    NodeType.DATA.value,
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
    "py:type:": NodeType.TYPE.value,
    "py:data:": NodeType.DATA.value,
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


def _chase_reexports(name: str, reexports: dict[str, str]) -> str:
    """Resolve ``name`` through re-export aliases to its defining path.

    Follows chains (``pkg.Thing`` → ``pkg.geometry.Thing`` →
    ``pkg.geometry.mesh.Thing``) and rewrites dotted prefixes, so
    ``pkg.Thing.method`` resolves through the ``pkg.Thing`` alias too.
    A seen-set guards against alias cycles.
    """
    seen: set[str] = set()
    current = name
    while current not in seen:
        seen.add(current)
        if current in reexports:
            current = reexports[current]
            continue
        # Longest dotted prefix that is itself an alias.
        parts = current.split(".")
        for cut in range(len(parts) - 1, 0, -1):
            prefix = ".".join(parts[:cut])
            if prefix in reexports:
                current = ".".join([reexports[prefix], *parts[cut:]])
                break
        else:
            break
    return current


def _canonical_rank_key(
    cid: str, cname: str, attrs: dict,
) -> tuple[int, int, int, int, int, str]:
    """Sort key for canonical-candidate tie-breaking.

    Delegates to :func:`_mappings.candidate_rank`, the single ranking
    shared with Sphinx-side reference resolution — this pass has no
    reftype to express a preference with, so it takes the default
    ``objtype_rank``. See that function for the ordering.
    """
    return candidate_rank(cid, cname, attrs)


#: Python-domain node id prefixes, in the order a lookup should prefer
#: them when the role itself does not say. ``py:obj:``-style roles and
#: docstring prose rarely name the objtype correctly, so a candidate is
#: accepted from any of these.
_PY_LOOKUP_PREFIXES: tuple[str, ...] = (
    "py:class:", "py:exception:", "py:method:", "py:function:",
    "py:attribute:", "py:data:", "py:type:", "py:module:",
)


def _namespace_of(g: "Any", node_id: str) -> tuple[str, str]:
    """The ``(modname, classname)`` a reference in this node resolves against.

    Sphinx resolves a relative Python reference against the *current*
    module and class. For a docstring that context is the namespace of
    the node the docstring belongs to, which the graph already records
    as ``contains`` edges — walk them up rather than re-deriving it from
    the file path.

    Returns empty strings for either component that does not apply (a
    module-level function has no class; a module is its own modname).
    """
    modname = classname = ""
    current = node_id
    for _ in range(4):  # module > class > method is the deepest real chain
        attrs = g.nodes.get(current) or {}
        ntype = attrs.get("type", "")
        name = attrs.get("name", "") or ""
        if ntype == NodeType.MODULE.value:
            modname = name
            break
        if ntype == NodeType.CLASS.value:
            classname = name.rsplit(".", 1)[-1]
        # A symbol has more than one ``contains`` parent: its lexical
        # owner (module/class) AND every doc page that documents it.
        # Only the Python chain carries a namespace — following a
        # ``doc:`` parent dead-ends on a node with no module, which
        # reads as "no context" and silently declines the reference.
        parents = [
            src for src, _, data in g.in_edges(current, data=True)
            if data.get("type") == EdgeType.CONTAINS.value
            and isinstance(src, str) and src.startswith("py:")
        ]
        if not parents:
            break
        current = parents[0]
    return modname, classname


def _find_in_namespace(
    g: "Any",
    target: str,
    modname: str,
    classname: str,
    preferred_prefix: str,
) -> str | None:
    """Resolve ``target`` the way ``PythonDomain.find_obj`` would.

    Search order with ``searchmode=0`` (the default — ``refspecific`` is
    set only by a leading dot):

    1. ``modname.classname.target``
    2. ``modname.target``
    3. ``target`` — **as a fully qualified key**, not as a bare name

    Step 3 is the counterintuitive one and it is load-bearing: the domain
    registry is keyed by full dotted names, so a bare ``:func:`solve```
    does NOT resolve merely because its module was ``automodule``-d. That
    is why "just add autodoc coverage" cannot fix relative references,
    and why this pass has to exist.
    """
    candidates = []
    if modname and classname:
        candidates.append(f"{modname}.{classname}.{target}")
    if modname:
        candidates.append(f"{modname}.{target}")
    candidates.append(target)

    prefixes = (
        (preferred_prefix, *(p for p in _PY_LOOKUP_PREFIXES if p != preferred_prefix))
        if preferred_prefix in _PY_LOOKUP_PREFIXES
        else _PY_LOOKUP_PREFIXES
    )
    for qualname in candidates:
        for prefix in prefixes:
            nid = f"{prefix}{qualname}"
            node = g.nodes.get(nid)
            if node is not None and node.get("type") not in PLACEHOLDER_TYPES:
                return nid
    return None


def _resolve_relative_references(graph: KnowledgeGraph) -> int:
    """Bind relative Python references using the referrer's namespace.

    Half of a real project's py-domain references are relative
    (``:meth:`Quadrature.product```, ``:class:`SNMesh```, ``:meth:`apply```)
    and mean different things in different modules. The graph already
    knows each reference's source node, so the context Sphinx would use
    is already present — it just was not consulted.

    **Retargets the edge, not the node.** A phantom is shared by every
    referrer that spelled the same name: measured on ORPHEUS, 532 bare
    phantoms have more than one distinct source and ``:meth:`apply```
    alone is referenced from 132. Folding the node would force one
    answer on all of them; only the edge carries the namespace that
    decides. Phantoms left with no remaining references are dropped.

    Runs BEFORE ``_canonicalize_phantoms``: namespace context is an
    answer, leaf-matching is a guess, and an answer must not lose to a
    guess that happens to sort first.
    """
    g = graph.nxgraph
    resolved = 0
    namespaces: dict[str, tuple[str, str]] = {}
    touched: set[str] = set()

    for src, tgt, key, data in list(g.edges(keys=True, data=True)):
        tgt_attrs = g.nodes.get(tgt) or {}
        if tgt_attrs.get("type") not in PLACEHOLDER_TYPES:
            continue
        if not isinstance(tgt, str) or not tgt.startswith("py:"):
            continue
        target_name = tgt_attrs.get("name", "") or ""
        if not target_name:
            continue

        if src not in namespaces:
            namespaces[src] = _namespace_of(g, src)
        modname, classname = namespaces[src]
        if not modname:
            # No namespace to resolve against — an .rst page with no
            # ``currentmodule``, or a node the contains chain never
            # reached a module from. Report it as-is rather than guess.
            continue

        prefix = tgt[:tgt.index(":", 3) + 1] if tgt.count(":") >= 2 else ""
        found = _find_in_namespace(g, target_name, modname, classname, prefix)
        if found is None or found == tgt:
            continue

        g.remove_edge(src, tgt, key=key)
        g.add_edge(src, found, **data)
        touched.add(tgt)
        resolved += 1

    # Drop phantoms nothing points at any more. A phantom with surviving
    # references is still the honest answer for those referrers.
    for nid in touched:
        if nid in g and g.in_degree(nid) == 0 and g.out_degree(nid) == 0:
            g.remove_node(nid)

    if resolved:
        logger.info(
            "Resolved %d relative references against their namespace",
            resolved,
        )
    return resolved


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

    # Build leaf-name → and full-name → (canonical_id, canonical_name)
    # indexes over the concrete nodes phantoms may fold into.
    leaf_index: dict[str, list[tuple[str, str]]] = {}
    name_index: dict[str, list[tuple[str, str]]] = {}
    for nid, attrs in g.nodes(data=True):
        if attrs.get("type") not in _CANONICAL_TYPES:
            continue
        name = attrs.get("name") or ""
        if not name:
            continue
        leaf = name.rsplit(".", 1)[-1]
        leaf_index.setdefault(leaf, []).append((nid, name))
        name_index.setdefault(name, []).append((nid, name))

    reexports: dict[str, str] = graph.metadata.get("reexports") or {}

    removed = 0
    for nid in list(g.nodes):
        attrs = g.nodes[nid]
        ntype = attrs.get("type", "")
        if ntype not in _PHANTOM_TYPES:
            continue
        name = attrs.get("name") or ""
        if not name:
            continue

        # Exact resolution through re-export aliases first: a phantom
        # named by a public path (``pkg.Thing``) folds onto the node
        # at its defining path (``pkg.geometry.mesh.Thing``) with no
        # leaf-collision heuristics involved.
        matched: list[tuple[str, str]] = []
        if reexports:
            resolved = _chase_reexports(name, reexports)
            if resolved != name:
                matched = [
                    (cid, cname)
                    for cid, cname in name_index.get(resolved, [])
                    if cid != nid
                ]

        if not matched:
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

        # A test helper must not absorb a name that production code
        # references — see ``test_node_is_off_limits``.
        matched = [
            pair for pair in matched
            if not test_node_is_off_limits(g, pair[0], nid)
        ]

        if not matched:
            continue

        # Pick the best canonical by type-rank + file_path.
        matched.sort(key=lambda pair: _canonical_rank_key(
            pair[0], pair[1], g.nodes[pair[0]],
        ))
        # Two candidates that differ only in name length or node id are
        # a coin flip, and this pass rewires edges — guessing wrong
        # silently reattributes a reference. Decline instead.
        #
        # ORPHEUS has a live example: a bare ``:mod:`derivations``` whose
        # leaf matches ``orpheus.derivations``, ``tests.derivations`` and
        # a sibling ``...origins.derivations``, all real modules. Folding
        # onto the shortest would be wrong — Sphinx resolves it to the
        # sibling, which only the referring node's namespace can tell you.
        if candidates_are_ambiguous([
            _canonical_rank_key(pair[0], pair[1], g.nodes[pair[0]])
            for pair in matched
        ]):
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


def _project_module_tops(g: "Any") -> set[str]:
    """Top-level segments of every module node in the graph.

    Module-typed nodes only exist for modules the AST layer analyzed
    or Sphinx documented — never for phantoms — so this is the set of
    names that belong to THIS project. It must be consulted before any
    installed-packages check: the project itself is usually pip-
    installed in its own build environment (editable install), so an
    environment lookup alone classifies the project's own dangling
    references as ``external`` and hides them from staleness gating.
    """
    tops = {
        (attrs.get("name") or "").split(".")[0]
        for _, attrs in g.nodes(data=True)
        if attrs.get("type") == NodeType.MODULE.value
    }
    tops.discard("")
    return tops


def _classify_phantom_nodes(graph: KnowledgeGraph) -> None:
    """Add type/name attributes to nodes auto-created by NetworkX.

    When add_edge references a node that doesn't exist, NetworkX creates
    it with no attributes. We classify these as EXTERNAL (stdlib/packages)
    or UNRESOLVED for project-internal symbols. Project-rooted names win
    over the environment check — see ``_project_module_tops``.
    """
    from sphinxcontrib.nexus.extractors import _EXTERNAL_NAMES

    g = graph.nxgraph
    project_tops = _project_module_tops(g)
    for node_id in list(g.nodes):
        attrs = g.nodes[node_id]
        if attrs.get("type") and attrs["type"] not in ("", "unknown"):
            continue  # already classified

        # Extract name from node ID: "py:function:numpy.array" → "numpy.array"
        parts = node_id.split(":", 2)
        name = parts[2] if len(parts) == 3 else node_id
        top_level = name.split(".")[0]

        if top_level in project_tops:
            node_type = NodeType.UNRESOLVED.value
        elif top_level in _EXTERNAL_NAMES:
            node_type = NodeType.EXTERNAL.value
        else:
            node_type = NodeType.UNRESOLVED.value

        attrs["type"] = node_type
        attrs["name"] = name
        attrs["display_name"] = name
        attrs["domain"] = parts[0] if len(parts) >= 2 else "py"
        attrs["source"] = "ast_inferred"
