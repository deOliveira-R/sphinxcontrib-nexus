"""How a definition is ADDRESSED outside the graph — positions in,
selectors out.

Every tool that is not nexus names a definition by where it lives, not
by node id: an editor and a traceback say ``(file, line)``; pytest says
``path::Class::test``. Both directions of that translation live here,
so neither is re-derived at a call site.

INBOUND — a position is a ``(file, line)`` pair in a checkout, and two
different kinds of asker turn one into a node:

**Navigation.** A language server, a stack trace, an editor: *what am I
looking at?*  The answer is the innermost definition whose extent
contains the line — of any kind — falling back to the module, because
"between two defs" is still somewhere.  :meth:`PositionIndex.enclosing`.

**Trace binding.** cProfile, coverage, viztracer: *which definition does
this record name?*  The answer must be an executable node (a function or
a method) or nothing at all: a class node would shadow its own methods,
and a module is not something a code object can be.
:meth:`PositionIndex.defined_at`.

Those really are two questions, so they are two verbs and neither is a
flag on the other.  What they SHARE is the key space and the extents,
and that sharing is the reason this module exists: the same join used to
be written twice (``GraphQuery.node_at`` scanning every node,
``runtime.build_node_index`` + ``resolve_node`` over a dict of tuples)
with three different answers to the same position — measured 2026-08-16
at **3 of 4** probed positions, two of them by design and one a defect.

OUTBOUND — :func:`pytest_selector` projects a node back to the name
pytest collects it under, which is the last join between "which tests
pin this symbol?" and a command you can run.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from sphinxcontrib.nexus.workspace import canonical_path

if TYPE_CHECKING:  # pragma: no cover - typing only
    import networkx as nx

    from sphinxcontrib.nexus.graph import KnowledgeGraph

#: Nodes a trace record may name. A class would shadow its own methods
#: in the enclosing-extent search; a module has no code object.
EXECUTABLE_TYPES = ("function", "method")

#: How far ABOVE a ``def`` a trace line may sit and still mean that def,
#: used ONLY for graphs built before nodes carried ``decorator_lineno``.
#: A current graph records the decorator line itself, so the join is an
#: exact match and never consults this. See :meth:`PositionIndex.defined_at`.
DECORATOR_WINDOW = 8

#: Node kinds pytest addresses as a runnable ITEM. Measured on ORPHEUS
#: 2026-08-17 against a full ``--collect-only`` pass: of the nodes the
#: analyzer flags ``is_test``, methods resolve **2669 / 2669** and
#: functions **2604 / 2604** — every collected test has a node and
#: every derived id resolves.
#:
#: Nothing else does. ``data`` (**0 / 935**) and ``attribute``
#: (**0 / 279**) inherit the flag from living in a test module and are
#: not tests at all: ``pytest tests/_harness/registry.py::TEST_REGISTRY``
#: matches nothing. ``class`` is the interesting refusal — 810 of 882
#: ARE valid selectors (a class is a container of items), so an id for
#: one would be right 92 % of the time, which is precisely the shape
#: this project refuses to emit: a guess that reads like a fact.
COLLECTABLE_TYPES = ("function", "method")


def pytest_selector(
    file_path: str | Path,
    dotted_name: str,
    node_type: str,
    project_root: Path | None = None,
) -> str | None:
    """The name pytest collects this definition under, or ``None``.

    ``tests/sn/test_alpha.py`` + ``tests.sn.test_alpha.Case.test_dome``
    becomes ``tests/sn/test_alpha.py::Case::test_dome`` — a
    copy-pasteable pytest target.

    The transform is mechanical and every consumer measured so far had
    re-derived it by hand, which is the whole reason it lives in one
    place. What is NOT mechanical is the guarantee, so this returns
    ``None`` rather than guessing whenever it cannot prove the answer:

    * ``node_type`` must be one pytest runs — see
      :data:`COLLECTABLE_TYPES`.
    * ``dotted_name`` must actually begin with the module path the file
      implies. A node whose name disagrees with its own file is exactly
      the case where a fabricated selector would be silently wrong.
    * Without ``project_root`` an absolute stored path cannot be made
      relative, and pytest ids are relative.
    * A node with no ``file_path`` at all has nothing to be relative
      to. This is not hypothetical: a test node minted from a doc
      directive or a hand-built graph carries none.

    ⚠ Relative to the PROJECT ROOT, which is pytest's ``rootdir`` in
    every layout nexus has been run against but is not guaranteed to
    be. The ids a collection manifest records
    (:mod:`~sphinxcontrib.nexus.pytest_manifest`) are what pytest
    actually resolved — including the ``[param]`` suffixes, which no
    static derivation can know. This is the derivation; that is the
    fact, and where both exist the fact wins.
    """
    if node_type not in COLLECTABLE_TYPES:
        return None
    if not file_path or not dotted_name:
        return None
    path = Path(file_path)
    if not path.name:
        return None
    if project_root is not None:
        wanted = canonical_path(path, project_root)
        try:
            path = wanted.relative_to(project_root.resolve())
        except ValueError:
            return None
    elif path.is_absolute():
        return None

    module_dotted = ".".join(path.with_suffix("").parts)
    prefix = module_dotted + "."
    if not dotted_name.startswith(prefix):
        return None
    tail = dotted_name[len(prefix):].replace(".", "::")
    return f"{path.as_posix()}::{tail}"


@dataclass(frozen=True)
class Definition:
    """One definition's extent in one source file.

    **Two beginnings, because a decorated definition has two**, and
    different consumers need different ones:

    ``def_line``
        where the AST puts the ``def`` / ``class`` keyword.  This is
        what an editor jumps to and what coverage attributes body lines
        against.

    ``first_line``
        where the definition's SOURCE begins — its first decorator, or
        ``def_line`` when there are none.  This is the line CPython
        stores as ``co_firstlineno`` and therefore the line every
        tracer reports.

    Conflating the two is not a hypothetical: measured on ORPHEUS
    2026-08-16, **456 of 3530** decorated definitions bound to the wrong
    node — 291 of them ``@property`` — because the join had only
    ``def_line`` and reached for the decorator with a fixed-width guess.
    """

    node_id: str
    node_type: str
    first_line: int
    def_line: int
    end_line: int

    @property
    def extent(self) -> int:
        """Lines spanned — the innermost-wins ordering key."""
        return self.end_line - self.first_line

    def contains(self, line: int) -> bool:
        """Whether ``line`` falls anywhere in the definition, decorators
        included."""
        return self.first_line <= line <= self.end_line


class PositionIndex:
    """``(file, line)`` → node, for every asker, over one checkout.

    Built once from a graph and queried many times, so the extents are
    grouped by file and sorted at construction.  File keys are
    :func:`~sphinxcontrib.nexus.workspace.canonical_path` of the stored
    path, which is what lets a caller ask with whatever spelling it has
    — the older ``build_node_index`` keyed on the RAW stored path, so a
    relative query silently found nothing.

    ``root`` is the checkout the STORED paths are relative to.  A trace's
    own key space is a different tree (``coverage json`` emits keys
    relative to the directory the run used) and is the caller's business
    — see :class:`~sphinxcontrib.nexus.runtime.NodeBinder`, which
    canonicalises against the trace root and then asks here with an
    absolute path.
    """

    def __init__(
        self,
        graph: KnowledgeGraph | nx.MultiDiGraph,
        root: Path | None = None,
    ) -> None:
        # `getattr` rather than `isinstance`: `KnowledgeGraph` is a
        # TYPE_CHECKING-only import here (importing it at runtime would
        # close a cycle), so the duck-typed unwrap is deliberate. The
        # annotation is what tells a checker what comes out of it.
        g = cast("nx.MultiDiGraph", getattr(graph, "nxgraph", graph))
        self._root = root
        by_file: dict[Path, list[Definition]] = {}
        modules: dict[Path, str] = {}
        for node_id, attrs in g.nodes(data=True):
            stored = attrs.get("file_path")
            if not stored:
                continue
            key = canonical_path(stored, root)
            node_type = attrs.get("type")
            if node_type == "module":
                modules[key] = node_id
                continue
            def_line, end_line = attrs.get("lineno"), attrs.get("end_lineno")
            if not def_line or end_line is None:
                continue
            by_file.setdefault(key, []).append(
                Definition(
                    node_id=node_id,
                    node_type=node_type or "",
                    # Absent on graphs built before decorator extents were
                    # recorded; the definition then starts at its `def`,
                    # and `defined_at` falls back to the window.
                    first_line=attrs.get("decorator_lineno") or def_line,
                    def_line=def_line,
                    end_line=end_line,
                )
            )
        self._by_file: dict[Path, tuple[Definition, ...]] = {
            key: tuple(sorted(defs, key=lambda d: (d.first_line, d.end_line)))
            for key, defs in by_file.items()
        }
        self._modules = modules
        self._positioned = frozenset(
            d.node_id for defs in self._by_file.values() for d in defs
        )

    # ── the two questions ───────────────────────────────────────────

    def knows_node(self, node_id: str) -> bool:
        """Is this node id one the index positioned?

        The third question, and the one asked by NAME rather than by
        position: a coverage *context* arrives as a dotted qualname with
        no line attached, so its node cannot be found by
        :meth:`defined_at`. Answering from the set this index already
        built keeps one authority over "which nodes have a position" —
        the alternative was threading a second view of the same graph
        into every overlay that needs to check a name.

        Module nodes are deliberately excluded, matching
        :meth:`defined_at`: only something with a def/end span is here.
        """
        return node_id in self._positioned

    def enclosing(self, file: Path | str, line: int) -> str | None:
        """The innermost node containing a position — the navigator's
        question.

        Any node type may answer, and a position in module scope
        (imports, constants, the gap between two defs) answers with the
        module: the file IS in the graph, and saying so is more useful
        than ``None``.  ``None`` only when the file is unknown.
        """
        key = canonical_path(file, self._root)
        found = self._innermost(self._by_file.get(key, ()), line)
        return found.node_id if found else self._modules.get(key)

    def defined_at(self, file: Path | str, line: int) -> str | None:
        """The executable definition a trace record at this position
        names — the tracers' question.

        Only a function or a method may answer, and a miss falls to the
        decorator window rather than to anything enclosing: ``None`` is
        a real answer here, and by design it is lambdas, comprehensions
        and nested closures, which have no node of their own.

        A tracer reports ``co_firstlineno``, which for a decorated
        definition is its FIRST DECORATOR line — and since
        :attr:`Definition.first_line` is where the extent begins, that
        line is simply *contained* by the definition.  No exact-match
        special case is needed, and one must not be added back: `[M]`
        2026-08-16, an exact-match-first pass changed the answer on
        **0 of 1 830 000** realizable positions (siblings disjoint,
        nested defs strictly inside their parent), so it was pure
        duplication of the search below it.
        """
        key = canonical_path(file, self._root)
        defs = tuple(
            d
            for d in self._by_file.get(key, ())
            if d.node_type in EXECUTABLE_TYPES
        )
        if not defs:
            return None
        found = self._innermost(defs, line)
        return found.node_id if found else self._nearest_below(defs, line)

    # ── the search both questions share ─────────────────────────────

    @staticmethod
    def _innermost(
        defs: tuple[Definition, ...], line: int
    ) -> Definition | None:
        """The tightest definition containing ``line``, or ``None``.

        ``defs`` is sorted by ``first_line``, so the scan stops at the
        first definition starting after the line.  Innermost is the
        smallest extent, ties broken by the latest start.

        ⚠ The two verbs used to run two DIFFERENT versions of this — the
        navigator ranking by extent, the trace join by latest start.
        `[M]` 2026-08-16 they agree on **all 1 830 000** realizable
        positions probed; they can differ only for extents that overlap
        without nesting, which no source file produces. Two spellings of
        one search, which is how they came to disagree elsewhere.
        """
        best: Definition | None = None
        for d in defs:
            if d.first_line > line:
                break
            if d.contains(line) and (
                best is None
                or (d.extent, -d.first_line) < (best.extent, -best.first_line)
            ):
                best = d
        return best

    def _nearest_below(
        self, defs: tuple[Definition, ...], line: int
    ) -> str | None:
        """The definition just below ``line``, within
        :data:`DECORATOR_WINDOW` — the pre-``decorator_lineno`` fallback.

        ⚠ The rule is NEAREST BELOW, not "the last one that matched".
        The retired ``resolve_node`` wrote the window and the body test
        as one condition (``ln - WINDOW <= ask <= end``) and took the
        latest start, so a LATER SIBLING could claim a line sitting in an
        earlier definition's decorators and win it:

        .. code-block:: text

            ask = 288, the decorator of n_points
               289-291  n_points   <- the decorator's own def
               294-301  dim        <- 294-8 <= 288 <= 301, scanned later, WON

        That is the shape of all 456 measured misbindings, and it is
        always a theft by the NEXT definition down the file.
        """
        starts = [d.first_line for d in defs]
        i = bisect_left(starts, line)
        if i < len(defs) and line < defs[i].first_line <= line + DECORATOR_WINDOW:
            return defs[i].node_id
        return None

    # ── the file-level view (coverage's unit) ───────────────────────

    def definitions_in(
        self, file: Path | str
    ) -> tuple[Definition, ...] | None:
        """Every executable definition in one file, or ``None`` when the
        file has none indexed.

        ``None`` rather than an empty tuple because the two are different
        answers to a caller keeping a ledger: a file the graph has never
        heard of is a key-space problem, an empty one is not.
        """
        key = canonical_path(file, self._root)
        defs = tuple(
            d
            for d in self._by_file.get(key, ())
            if d.node_type in EXECUTABLE_TYPES
        )
        return defs or None

    def knows(self, file: Path | str) -> bool:
        """Whether any definition in this file is indexed."""
        return self.definitions_in(file) is not None
