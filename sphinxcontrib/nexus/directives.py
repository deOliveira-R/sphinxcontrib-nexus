"""Sphinx directives for declaring verification and implementation edges.

Two families of user-facing directives ship here.

**Math ↔ code** (v0.8.0):

- ``.. verifies:: <equation_label>`` — declares that a Python object
  verifies (tests) a math equation. Emits an ``EdgeType.TESTS`` edge.
- ``.. implements:: <equation_label>`` — declares that a Python object
  implements a math equation. Emits an ``EdgeType.IMPLEMENTS`` edge.

Both take an optional ``:by:`` role that names the Python symbol. When
omitted, the directive falls back to ``env.ref_context`` inspection so
usage nested inside an ``.. py:function::`` or ``.. autofunction::``
block picks up the enclosing signature automatically.

**Math ↔ math** (v0.17.0): equations were graph leaves — the graph knew
code implements them and tests verify them, but nothing about how the
math itself hangs together. Three directives declare that structure:

- ``.. discretizes:: <label>`` — this discrete form discretizes that
  continuous one.
- ``.. derives-from:: <label>`` — this specialization/reduction derives
  from that parent.
- ``.. approximates:: <label>`` — this closure/truncation approximates
  that exact form.

Each names its *target* as the argument and takes the *source*
statement as ``:label:``. Omit ``:label:`` and the directive binds to
the nearest preceding labelled equation in the same document, which is
where these are written in practice — directly under the equation they
describe. Both ends may name a ``math`` equation or a ``sphinx-proof``
environment, so "Theorem 3.4 derives-from Definition 3.2" works with
the same syntax.

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

from sphinxcontrib.nexus._mappings import doc_node_id, resolve_proof_id
from sphinxcontrib.nexus.extractors import _is_auto_proof_label
from sphinxcontrib.nexus.graph import EdgeType, NodeType

if TYPE_CHECKING:
    import networkx as nx
    from sphinx.application import Sphinx
    from sphinx.environment import BuildEnvironment

logger = logging.getLogger(__name__)

#: Directive name → edge type for the statement-to-statement relations.
#: Every entry must have a query that consumes it (``provenance_chain``
#: walks all three); the set stays small on purpose.
EQUATION_RELATIONS: dict[str, EdgeType] = {
    "discretizes": EdgeType.DISCRETIZES,
    "derives-from": EdgeType.DERIVES_FROM,
    "approximates": EdgeType.APPROXIMATES,
}


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


def _resolve_statement_id(label: str, graph: "nx.MultiDiGraph") -> str | None:
    """Resolve a label naming a mathematical statement to a node id.

    A statement is either a ``math`` equation or a ``sphinx-proof``
    environment; both are written as a bare label at the directive, so
    which one it is only becomes knowable against the built graph.
    """
    eq_id = f"math:equation:{label}"
    if eq_id in graph:
        return eq_id
    return resolve_proof_id(graph, label)


def _nearest_preceding_label(state: Any) -> str | None:
    """The label of the last labelled statement parsed before this point.

    These directives are written directly beneath the equation they talk
    about, so the enclosing statement is nearly always the one just
    above — the same ergonomics ``:by:`` gets from ``ref_context``.
    Only content already parsed is attached to the document, so the last
    match in document order *is* the nearest preceding one.

    Matches labelled ``.. math::`` blocks and sphinx-proof environments
    (identified structurally by their ``realtype`` attribute, so no
    import of the optional extension is needed). Synthetic labels from
    unlabelled proof environments are skipped — they name nothing the
    graph will hold, so binding to one would only produce a confusing
    "not found" at replay.
    """
    document = getattr(state, "document", None)
    if document is None:
        return None
    found: str | None = None
    for node in document.findall(nodes.Element):
        label = node.get("label")
        if not label:
            continue
        realtype = node.get("realtype")
        if realtype:
            if not _is_auto_proof_label(label, realtype):
                found = label
        elif isinstance(node, nodes.math_block):
            found = label
    return found


class _StatementRelationDirective(SphinxDirective):
    """Common plumbing for the statement-to-statement relations.

    Deliberately not a subclass of ``_VerificationDirectiveBase``: the
    two share only the pending queue. This family takes no ``:by:``
    (neither end is code) and its source is a label rather than a
    ref_context lookup, so inheriting would mean overriding everything
    that matters.
    """

    required_arguments = 1
    has_content = True
    option_spec = {
        "label": rst_directives.unchanged,
    }

    #: Subclasses set this to a key of ``EQUATION_RELATIONS``.
    kind: str = ""

    def run(self) -> list[nodes.Node]:
        to_label = self.arguments[0].strip()
        from_label = (
            self.options.get("label", "").strip()
            or _nearest_preceding_label(self.state)
            or ""
        )
        if not from_label:
            msg = self.reporter.warning(
                f".. {self.kind}:: {to_label!r} needs a ':label:' option "
                f"(no labelled equation or proof environment precedes it)",
                line=self.lineno,
            )
            return [msg]
        if from_label == to_label:
            msg = self.reporter.warning(
                f".. {self.kind}:: {to_label!r} relates a statement to "
                f"itself — check the ':label:' option",
                line=self.lineno,
            )
            return [msg]

        pending = _init_pending_queue(self.env, self.env.docname)
        pending.append({
            "kind": self.kind,
            "from_label": from_label,
            "to_label": to_label,
            "docname": self.env.docname,
            "lineno": self.lineno,
        })
        if self.content:
            container = nodes.container()
            self.state.nested_parse(self.content, self.content_offset, container)
            return [container]
        return []


class DiscretizesDirective(_StatementRelationDirective):
    """Declare that a discrete form discretizes a continuous one.

    Syntax::

        .. math::
           :label: sn-dd-closure

           \\dots

        .. discretizes:: sn-transport-continuous

    Emits ``EdgeType.DISCRETIZES`` from ``sn-dd-closure`` to
    ``sn-transport-continuous``.
    """

    kind = "discretizes"


class DerivesFromDirective(_StatementRelationDirective):
    """Declare that a specialization derives from a parent statement.

    Emits ``EdgeType.DERIVES_FROM`` from the specialized statement to
    the general one it was reduced from.
    """

    kind = "derives-from"


class ApproximatesDirective(_StatementRelationDirective):
    """Declare that a closure or truncation approximates an exact form.

    Emits ``EdgeType.APPROXIMATES`` from the approximation to the exact
    statement it stands in for.
    """

    kind = "approximates"


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


class ErrorEntryDirective(SphinxDirective):
    """Declare a catalogued failure mode, so a test can be linked to it.

    Syntax::

        .. error-entry:: ERR-051
           :title: Galerkin idempotency asserted without the 4π convention

           Prose describing the failure mode, how it was found, and what
           prevents it now.

    Creates a ``vv:error:<id>`` node. ``@pytest.mark.catches("ERR-051")``
    then resolves to it (:func:`~sphinxcontrib.nexus.merge.write_catches_edges`),
    which is what makes *"which tests catch ERR-051?"* a graph question
    instead of a grep.

    ⚠ **Not** named ``.. error::`` — that is docutils' own admonition,
    and claiming it would silently change how every ``.. error::`` block
    renders in every project that installs nexus.

    This is nexus's only DECLARING directive; every other one asserts a
    relation between things that already exist. It is a declaration for
    the same reason ``.. math:: :label:`` is: a marker must never conjure
    the thing it names, or a typo in ``catches`` would mint a catalogue
    entry nobody wrote — and the miss would then look like coverage.
    """

    required_arguments = 1
    has_content = True
    option_spec = {
        "title": rst_directives.unchanged,
    }

    def run(self) -> list[nodes.Node]:
        entry_id = self.arguments[0].strip()
        title = self.options.get("title", "").strip()

        pending = _init_pending_queue(self.env, self.env.docname)
        pending.append({
            "kind": "error-entry",
            "id": entry_id,
            "title": title,
            "docname": self.env.docname,
            "lineno": self.lineno,
        })

        # Unlike the relation directives, this one renders: a catalogue
        # whose entries are invisible is not a catalogue.
        container = nodes.container()
        container += nodes.rubric(
            "", f"{entry_id} — {title}" if title else entry_id,
        )
        if self.content:
            self.state.nested_parse(self.content, self.content_offset, container)
        return [container]


def apply_declared_nodes(
    env: "BuildEnvironment",
    graph: "nx.MultiDiGraph",
) -> int:
    """Create the nodes that declaring directives asked for.

    Shares ``env.nexus_pending_edges`` with :func:`apply_pending_edges`
    rather than keeping a second registry — one store means
    ``env-purge-doc`` and ``env-merge-info`` keep working unchanged, and
    the two cannot disagree about which docname contributed what.

    MUST run before :func:`apply_pending_edges` and
    ``merge.write_catches_edges``: both resolve a marker onto a node and
    warn when it is missing, so a declaration that has not landed yet
    reads exactly like a typo.

    Returns the number of nodes created (idempotent — re-applying to the
    same graph is a no-op).
    """
    registry: dict[str, list[dict[str, Any]]] | None = getattr(
        env, "nexus_pending_edges", None
    )
    if not registry:
        return 0

    created = 0
    for docname, entries in registry.items():
        for entry in entries:
            if entry.get("kind") != "error-entry":
                continue
            entry_id = entry["id"]
            node_id = f"vv:{NodeType.ERROR.value}:{entry_id}"
            if node_id in graph:
                continue
            graph.add_node(
                node_id,
                type=NodeType.ERROR.value,
                name=entry_id,
                display_name=entry.get("title") or entry_id,
                domain="vv",
                docname=docname,
                title=entry.get("title", ""),
            )
            created += 1
            page = doc_node_id(docname)
            if page in graph:
                graph.add_edge(
                    page, node_id,
                    type=EdgeType.CONTAINS.value,
                    source="directive",
                )
    return created


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


def _apply_relation(
    entry: dict[str, Any],
    graph: "nx.MultiDiGraph",
    ctx: str,
) -> int:
    """Write one statement-to-statement edge. Returns 1 if it landed.

    Both ends must already exist as nodes. A label that resolves to
    nothing is a dead reference in prose — logged and skipped, the same
    treatment the verification directives give a missing equation.
    """
    kind = entry["kind"]
    from_label = entry["from_label"]
    to_label = entry["to_label"]

    source_id = _resolve_statement_id(from_label, graph)
    if source_id is None:
        logger.warning(
            ".. %s:: [%s]: source statement %r not found in graph — skipping",
            kind, ctx, from_label,
        )
        return 0

    target_id = _resolve_statement_id(to_label, graph)
    if target_id is None:
        logger.warning(
            ".. %s:: [%s]: target statement %r not found in graph — skipping",
            kind, ctx, to_label,
        )
        return 0

    edge_type = EQUATION_RELATIONS[kind].value
    existing = graph.get_edge_data(source_id, target_id, default={})
    if any(d.get("type") == edge_type for d in existing.values()):
        return 0

    graph.add_edge(
        source_id,
        target_id,
        type=edge_type,
        source="directive",
        confidence=1.0,
    )
    return 1


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
            lineno = entry.get("lineno", "?")
            ctx = f"{docname}:{lineno}"

            if kind in EQUATION_RELATIONS:
                written += _apply_relation(entry, graph, ctx)
                continue

            label = entry["label"]
            target = entry["target"]

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
    """Register the nexus directives and their env handlers."""
    app.add_directive("error-entry", ErrorEntryDirective)
    app.add_directive("verifies", VerifiesDirective)
    app.add_directive("implements", ImplementsDirective)
    app.add_directive("discretizes", DiscretizesDirective)
    app.add_directive("derives-from", DerivesFromDirective)
    app.add_directive("approximates", ApproximatesDirective)
    app.connect("env-purge-doc", purge_doc)
    app.connect("env-merge-info", merge_env)
