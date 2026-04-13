"""Sphinx directives for declaring verification and implementation edges.

Two user-facing directives ship in v0.8.0:

- ``.. verifies:: <equation_label>`` — declares that a Python object
  verifies (tests) a math equation. Emits an ``EdgeType.TESTS`` edge.
- ``.. implements:: <equation_label>`` — declares that a Python object
  implements a math equation. Emits an ``EdgeType.IMPLEMENTS`` edge.

Both take an optional ``:by:`` role that names the Python symbol. When
omitted, the directive falls back to ``env.ref_context`` inspection so
usage nested inside an ``.. py:function::`` or ``.. autofunction::``
block picks up the enclosing signature automatically.

**Timing**: directives run during ``doctree-read``, before
``env.nexus_graph`` exists. They stash pending edge payloads on
``env.nexus_pending_edges``. The ``env-check-consistency`` hook
reconciles the queue into the freshly-built doc graph, so every AST
merge and heuristic pass that follows sees the directive-sourced
edges alongside everything else.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from docutils import nodes
from docutils.parsers.rst import directives as rst_directives
from sphinx.util.docutils import SphinxDirective

from sphinxcontrib.nexus.graph import EdgeType

if TYPE_CHECKING:
    import networkx as nx
    from sphinx.application import Sphinx
    from sphinx.environment import BuildEnvironment

logger = logging.getLogger(__name__)


def _init_pending_queue(
    env: "BuildEnvironment",
    docname: str,
) -> list[dict[str, Any]]:
    """Lazy-initialize (and return) the per-docname entry of the
    per-env pending-edges registry.

    The registry is stored as ``env.nexus_pending_edges``, a mapping
    from docname to a list of pending edge descriptors. Keying by
    docname lets us wire ``env-purge-doc`` to drop stale entries
    when a file is about to be re-parsed on an incremental build —
    without that, the directive edges would only fire on fresh
    builds and silently disappear otherwise (the same caching trap
    that bit ORPHEUS during the 0.7.0 roll-out).
    """
    registry: dict[str, list[dict[str, Any]]] | None = getattr(
        env, "nexus_pending_edges", None
    )
    if registry is None:
        registry = {}
        env.nexus_pending_edges = registry  # type: ignore[attr-defined]
    return registry.setdefault(docname, [])


def _resolve_enclosing_py_symbol(env: "BuildEnvironment") -> str | None:
    """Best-effort: reconstruct the fully-qualified name of the Python
    object currently on Sphinx's ``py:`` ref_context stack.

    Returns ``None`` if not inside a recognized ``py:`` domain context
    — callers must then use the ``:by:`` option explicitly.
    """
    ref_ctx = getattr(env, "ref_context", {}) or {}
    module = ref_ctx.get("py:module") or ""
    classes = ref_ctx.get("py:classes") or []
    # ``py:class`` is set inside `.. py:class::`, ``py:function`` / `py:method`
    # are set inside the matching directives by autodoc. The "most specific"
    # key wins — check in that order.
    for key in ("py:method", "py:function", "py:class", "py:attribute"):
        name = ref_ctx.get(key)
        if name:
            parts: list[str] = []
            if module:
                parts.append(module)
            if key != "py:class" and classes:
                parts.extend(classes)
            parts.append(name)
            return ".".join(parts)
    return None


def _node_id_for_target(target: str, graph: "nx.MultiDiGraph") -> str | None:
    """Resolve a user-supplied Python symbol name to a concrete node
    id in the graph. Accepts either a bare dotted name (``pkg.mod.func``)
    or an already-prefixed node id (``py:function:pkg.mod.func``).

    Returns the resolved id if a matching function/method/class node
    exists, otherwise ``None``.
    """
    if target in graph:
        return target
    for prefix in ("py:function:", "py:method:", "py:class:"):
        candidate = f"{prefix}{target}"
        if candidate in graph:
            return candidate
    return None


class _VerificationDirectiveBase(SphinxDirective):
    """Common plumbing for ``.. verifies::`` and ``.. implements::``."""

    required_arguments = 1
    has_content = True
    option_spec = {
        "by": rst_directives.unchanged,
    }

    #: Subclasses set this to ``"verifies"`` or ``"implements"``.
    kind: str = ""

    def run(self) -> list[nodes.Node]:
        label = self.arguments[0].strip()
        target = self.options.get("by", "").strip() or _resolve_enclosing_py_symbol(
            self.env
        )
        if not target:
            msg = self.reporter.warning(
                f".. {self.kind}:: {label!r} needs a ':by:' option "
                f"(no enclosing Python object in ref_context)",
                line=self.lineno,
            )
            return [msg]

        pending = _init_pending_queue(self.env, self.env.docname)
        pending.append({
            "kind": self.kind,
            "label": label,
            "target": target,
            "docname": self.env.docname,
            "lineno": self.lineno,
        })
        # Directives emit nothing visible in the rendered doc by default.
        # Subclasses with body content get a transparent container so
        # users can still write prose inside the block.
        if self.content:
            container = nodes.container()
            self.state.nested_parse(self.content, self.content_offset, container)
            return [container]
        return []


class VerifiesDirective(_VerificationDirectiveBase):
    """Declare that a Python object verifies (tests) a math equation.

    Syntax::

        .. verifies:: <equation_label>
           :by: <python.symbol>

           Optional prose explaining the verification.

    Emits an ``EdgeType.TESTS`` edge from the named test to
    ``math:equation:<label>`` with ``source="directive"`` and
    ``confidence=1.0``.
    """

    kind = "verifies"


class ImplementsDirective(_VerificationDirectiveBase):
    """Declare that a Python object implements a math equation.

    Syntax::

        .. implements:: <equation_label>
           :by: <python.symbol>

    Emits an ``EdgeType.IMPLEMENTS`` edge from the code symbol to
    ``math:equation:<label>`` with ``source="directive"`` and
    ``confidence=1.0``. Because the edge is explicit, the inference
    heuristic in ``merge._infer_implements`` skips this pair.
    """

    kind = "implements"


def apply_pending_edges(
    env: "BuildEnvironment",
    graph: "nx.MultiDiGraph",
) -> int:
    """Replay ``env.nexus_pending_edges`` against the graph.

    Resolves each entry's ``target`` string to a concrete node id and
    writes the corresponding edge. Missing nodes (target or equation)
    are logged and skipped — directive misuse should be loud without
    breaking the build.

    The registry is **not** cleared after replay: directive payloads
    persist across incremental builds so a cached doctree still
    contributes its edges even when its source didn't change. An
    ``env-purge-doc`` handler drops per-docname entries whenever
    Sphinx is about to re-parse that file, so the replay stays
    consistent with the current RST source.

    Returns the number of edges newly written (the function is
    idempotent — re-applying to the same graph is a no-op thanks to
    the ``source="directive"`` guard below).
    """
    registry: dict[str, list[dict[str, Any]]] | None = getattr(
        env, "nexus_pending_edges", None
    )
    if not registry:
        return 0

    written = 0
    for docname, entries in registry.items():
        for entry in entries:
            kind = entry["kind"]
            label = entry["label"]
            target = entry["target"]
            lineno = entry.get("lineno", "?")
            ctx = f"{docname}:{lineno}"

            resolved = _node_id_for_target(target, graph)
            if resolved is None:
                logger.warning(
                    ".. %s:: %s [%s]: target %r not found in graph — skipping",
                    kind, label, ctx, target,
                )
                continue

            eq_id = f"math:equation:{label}"
            if eq_id not in graph:
                logger.warning(
                    ".. %s:: %s [%s]: equation %s not found in graph — skipping",
                    kind, label, ctx, eq_id,
                )
                continue

            edge_type = (
                EdgeType.TESTS.value
                if kind == "verifies"
                else EdgeType.IMPLEMENTS.value
            )

            # Skip if ANY explicit edge already links this pair on
            # the same edge type — registry, marker, a prior replay,
            # or any future explicit source. Inference-sourced edges
            # (``source="inferred"``) are weak and don't block the
            # directive's deterministic assertion from joining them.
            existing = graph.get_edge_data(resolved, eq_id, default={})
            if any(
                d.get("type") == edge_type
                and d.get("source") not in (None, "inferred")
                for d in existing.values()
            ):
                continue

            graph.add_edge(
                resolved,
                eq_id,
                type=edge_type,
                source="directive",
                confidence=1.0,
            )
            written += 1

    if written:
        logger.info("directives: wrote %d edges from pending registry", written)
    return written


def purge_doc(app: "Sphinx", env: "BuildEnvironment", docname: str) -> None:
    """Drop any stashed directive payloads for ``docname``.

    Hooked to Sphinx's ``env-purge-doc`` event, which fires before a
    source file is re-parsed. If we didn't purge, directives removed
    from the RST would leave zombie entries in ``env.nexus_pending_edges``
    that the next ``apply_pending_edges`` would still write as edges.
    """
    registry: dict[str, list[dict[str, Any]]] | None = getattr(
        env, "nexus_pending_edges", None
    )
    if registry and docname in registry:
        del registry[docname]


def merge_env(
    app: "Sphinx",
    env: "BuildEnvironment",
    docnames: list[str],
    other: "BuildEnvironment",
) -> None:
    """Merge pending-edge registries from a parallel-build worker.

    Hooked to ``env-merge-info``. Each worker sees a subset of
    docnames; we take whatever entries that worker accumulated for
    those docnames and fold them into the main env's registry.
    """
    other_registry: dict[str, list[dict[str, Any]]] | None = getattr(
        other, "nexus_pending_edges", None
    )
    if not other_registry:
        return
    main_registry: dict[str, list[dict[str, Any]]] = getattr(
        env, "nexus_pending_edges", None
    ) or {}
    for docname in docnames:
        if docname in other_registry:
            main_registry[docname] = list(other_registry[docname])
    env.nexus_pending_edges = main_registry  # type: ignore[attr-defined]


def register(app: "Sphinx") -> None:
    """Register the verification directives and their env handlers."""
    app.add_directive("verifies", VerifiesDirective)
    app.add_directive("implements", ImplementsDirective)
    app.connect("env-purge-doc", purge_doc)
    app.connect("env-merge-info", merge_env)
