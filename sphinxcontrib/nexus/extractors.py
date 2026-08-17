"""Extract nodes and edges from a Sphinx BuildEnvironment."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from docutils import nodes as docutils_nodes
from sphinx import addnodes

from sphinxcontrib.nexus._mappings import (
    _normalize_wrapped_target,
    DOMAIN_TYPE_MAP,
    REFTYPE_EDGE_MAP,
    REFTYPE_NODETYPE_MAP,
    REFTYPE_OBJTYPE_MAP,
    doc_node_id,
    node_id,
    resolve_target_id,
)
from sphinxcontrib.nexus.graph import (
    EdgeType,
    GraphEdge,
    GraphNode,
    KnowledgeGraph,
    NodeType,
)

if TYPE_CHECKING:
    import networkx as nx
    from sphinx.environment import BuildEnvironment

logger = logging.getLogger(__name__)


# ``_node_id`` / ``_doc_node_id`` were private spellings here until
# 2026-08-16. Both are now `_mappings.node_id` / `_mappings.doc_node_id`,
# so the doctree side, the directives and the reference resolver cannot
# disagree about how an id is spelled — which is exactly how they came
# to spell one page two ways.


def extract_documents(env: BuildEnvironment, graph: KnowledgeGraph) -> None:
    """Create a FILE node for every document and CONTAINS edges from toctree."""
    for docname in env.all_docs:
        nid = doc_node_id(docname)
        title = str(env.titles.get(docname, docname))
        graph.add_node(GraphNode(
            id=nid,
            type=NodeType.FILE,
            name=docname,
            display_name=title,
            domain="std",
            docname=docname,
        ))

    toctree_includes = getattr(env, "toctree_includes", {})
    for parent, children in toctree_includes.items():
        parent_id = doc_node_id(parent)
        for child in children:
            child_id = doc_node_id(child)
            if graph.has_node(parent_id) and graph.has_node(child_id):
                graph.add_edge(GraphEdge(
                    source=parent_id,
                    target=child_id,
                    type=EdgeType.CONTAINS,
                ))


def extract_domain_objects(env: BuildEnvironment, graph: KnowledgeGraph) -> None:
    """Walk all domains and create a node for each object."""
    for domain in env.domains.values():
        domain_name = domain.name
        for name, dispname, obj_type, docname, anchor, _prio in domain.get_objects():
            node_type = DOMAIN_TYPE_MAP.get(
                (domain_name, obj_type), obj_type
            )
            # The id takes the MAPPED type, never the producer's raw
            # objtype. Taking the raw one is what put `property`,
            # `label` and `doc` — none of them node types — into the
            # identity space, and split one page across two nodes.
            nid = node_id(domain_name, node_type, name)
            graph.add_node(GraphNode(
                id=nid,
                type=node_type,
                name=name,
                display_name=dispname,
                domain=domain_name,
                docname=docname,
                anchor=anchor,
            ))

            doc_id = doc_node_id(docname)
            # `doc_id == nid` for the std domain's own `doc` objects: it
            # reports each document as an object OF that document, so the
            # page would contain itself. Harmless-looking, and it makes
            # every containment query report a page as its own child.
            #
            # ⚠ This only became reachable when `doc:X` and `std:doc:X`
            # stopped being two nodes — the edge used to run between the
            # twins, which is what made the duplication survive: it did
            # not look like a self-loop, it looked like structure.
            if doc_id != nid and graph.has_node(doc_id):
                graph.add_edge(GraphEdge(
                    source=doc_id,
                    target=nid,
                    type=EdgeType.CONTAINS,
                ))

    # Math domain: get_objects() returns empty, read data directly
    math_domain = env.domains.get("math")
    if math_domain is not None:
        equations = getattr(math_domain, "data", {}).get("objects", {})
        for label, (docname, eqno) in equations.items():
            nid = node_id("math", "equation", label)
            graph.add_node(GraphNode(
                id=nid,
                type=NodeType.EQUATION,
                name=label,
                display_name=f"({eqno})",
                domain="math",
                docname=docname,
                metadata={"eqno": eqno},
            ))
            doc_id = doc_node_id(docname)
            if graph.has_node(doc_id):
                graph.add_edge(GraphEdge(
                    source=doc_id,
                    target=nid,
                    type=EdgeType.CONTAINS,
                ))


def _is_auto_proof_label(label: str, realtype: str) -> bool:
    """True for the synthetic label sphinx-proof gives an unlabelled block.

    An environment written without ``:label:`` gets ``<type>-<serialno>``,
    where the serial is a per-build counter — it shifts whenever anything
    above it moves. Such a block is also unreferenceable by design
    (``noindex`` is forced on). Admitting them would churn node ids on
    every edit and add nodes no query can reach.
    """
    if not label.startswith(f"{realtype}-"):
        return False
    return label[len(realtype) + 1:].isdigit()


def _proof_title(title: str) -> str:
    """The environment's own title, without the rendering wrapper.

    sphinx-proof stores the title already formatted by
    ``proof_title_format``, whose default ``"(%t)"`` produces
    ``"(Angular flux)"`` so it reads as *Definition 1 (Angular flux)* on
    the page. Only the name belongs in a node's display_name, so a
    single matched outer pair of parens comes off.
    """
    title = title.strip()
    if len(title) > 2 and title.startswith("(") and title.endswith(")"):
        inner = title[1:-1]
        # Bail on "(a) and (b)" — the outer parens aren't a wrapper there.
        if "(" not in inner and ")" not in inner:
            return inner.strip()
    return title


def _proof_statement_text(node: docutils_nodes.Element, limit: int = 400) -> str:
    """The environment's prose, whitespace-collapsed and truncated.

    ``env.proof_list`` records where a theorem lives but not what it
    says, and what it says is the part worth reading — in ``context``
    output today, as an embedding target once semantic search lands.
    """
    for child in node.children:
        if isinstance(child, docutils_nodes.section):
            text = " ".join(child.astext().split())
            if len(text) > limit:
                return text[:limit].rstrip() + "…"
            return text
    return ""


def extract_proof_objects(env: BuildEnvironment, graph: KnowledgeGraph) -> None:
    """Create a node for every labelled ``sphinx-proof`` environment.

    The ``prf`` domain implements neither ``object_types`` nor
    ``get_objects()`` — every environment is recorded on
    ``env.proof_list`` instead — so the generic domain walk in
    :func:`extract_domain_objects` sees nothing and these have to be
    read directly, exactly as the math domain is.

    No-op when sphinx-proof isn't installed or no environment is used.
    """
    proof_list = getattr(env, "proof_list", None) or {}
    for label, info in proof_list.items():
        realtype = info.get("realtype", "")
        docname = info.get("docname", "")
        if not realtype or _is_auto_proof_label(label, realtype):
            continue

        nid = node_id("prf", NodeType.PROOF_OBJECT, label)
        graph.add_node(GraphNode(
            id=nid,
            type=NodeType.PROOF_OBJECT,
            name=label,
            display_name=label,
            domain="prf",
            docname=docname,
            anchor=label,
            metadata={
                "prf_type": realtype,
                "numbered": not info.get("nonumber", False),
            },
        ))

        doc_id = doc_node_id(docname)
        if graph.has_node(doc_id):
            graph.add_edge(GraphEdge(
                source=doc_id,
                target=nid,
                type=EdgeType.CONTAINS,
            ))


def _enrich_proof_nodes(doctree: docutils_nodes.document, graph: KnowledgeGraph) -> None:
    """Attach title and statement text to proof nodes from one doctree.

    Piggy-backs on the doctree load :func:`extract_references` already
    pays for. Proof nodes are matched structurally (a ``realtype`` and a
    ``label`` attribute) so the optional extension is never imported.
    """
    for node in doctree.findall(docutils_nodes.Element):
        realtype = node.get("realtype")
        label = node.get("label")
        if not realtype or not label:
            continue
        attrs = graph.nxgraph.nodes.get(node_id("prf", NodeType.PROOF_OBJECT, label))
        if attrs is None:
            continue
        title = _proof_title(node.get("title") or "")
        if title:
            attrs["display_name"] = title
        statement = _proof_statement_text(node)
        if statement:
            attrs["statement"] = statement


def _section_anchor_index(graph: KnowledgeGraph) -> dict[str, dict[str, str]]:
    """``{docname: {anchor: section_node_id}}`` — built once, not per page.

    Section nodes already carry the anchor Sphinx assigned them, which is
    also one of the ``ids`` docutils puts on the section node in the
    doctree. That shared spelling is the whole join; nothing here has to
    re-derive a slug.
    """
    index: dict[str, dict[str, str]] = {}
    for nid, attrs in graph.nxgraph.nodes(data=True):
        if attrs.get("type") != NodeType.SECTION.value:
            continue
        anchor, docname = attrs.get("anchor"), attrs.get("docname")
        if anchor and docname:
            index.setdefault(docname, {})[anchor] = nid
    return index


def _enclosing_sections(
    node: docutils_nodes.Node,
) -> list[docutils_nodes.Element]:
    """Every ``section`` containing ``node``, innermost first."""
    out: list[docutils_nodes.Element] = []
    parent = node.parent
    while parent is not None:
        if isinstance(parent, docutils_nodes.section):
            out.append(parent)
        parent = parent.parent
    return out


def _nest_labelled_statements(
    doctree: docutils_nodes.document,
    graph: KnowledgeGraph,
    anchors: dict[str, str],
) -> int:
    """Give every labelled statement on a page its enclosing anchor.

    A page's sections and its equations arrived as SIBLINGS: both were
    attached straight to the page node, so an equation did not know
    which anchor it sits under and "which section documents this?"
    ended in reading an RST line range by hand.

    Two facts land here, both from the doctree the reference pass has
    already loaded — this adds no build cost beyond one traversal:

    * ``lineno`` on sections and on labelled statements, so an answer
      can say *page:line* instead of *page*;
    * a ``contains`` edge from the enclosing section to the statement.

    ⚠ ADDITIVE. The page keeps its direct ``contains`` edge, because
    containment here is genuinely transitive and because that edge is
    what ``merge._infer_implements`` reads to find a page's equations —
    re-parenting would have taken ``implements`` to zero silently. It is
    also how ``contains`` already behaves for a documented method, which
    has both its class and its page as parents.

    Note the vocabulary anticipated this: ``[edge.contains]`` has listed
    ``section`` in its domain since the ontology was written, and no
    producer had ever emitted one.
    """
    g = graph.nxgraph
    for section in doctree.findall(docutils_nodes.section):
        for anchor in section.get("ids", ()):
            nid = anchors.get(anchor)
            if nid is not None and section.line and not g.nodes[nid].get("lineno"):
                g.nodes[nid]["lineno"] = section.line

    nested = 0
    for element in doctree.findall(docutils_nodes.Element):
        label = element.get("label")
        if not label or not isinstance(label, str):
            continue
        target_id = _labelled_statement_id(label, g)
        if target_id is None:
            continue
        if element.line and not g.nodes[target_id].get("lineno"):
            g.nodes[target_id]["lineno"] = element.line

        enclosing = _enclosing_sections(element)
        if not enclosing:
            continue

        # Two different facts, and conflating them is what made the
        # first version of this cover only 266 of 869 equations.
        #
        # The ANCHOR is where a reader opens the page, and every
        # section has one whether or not anyone labelled it — most
        # do not, since `.. _label:` is written where a cross-
        # reference is wanted, not everywhere. Take the innermost.
        # ⚠ Not `setdefault`: `GraphNode` gives every node an `anchor`
        # key that is None for an equation, so the key EXISTS and
        # `setdefault` is a silent no-op. Measured: 0 of 903 stamped.
        innermost_ids = enclosing[0].get("ids", ())
        if innermost_ids and not g.nodes[target_id].get("anchor"):
            g.nodes[target_id]["anchor"] = innermost_ids[0]

        # The EDGE is graph structure, so it needs a node at the other
        # end — the nearest ANCESTOR that is one. Walking up rather
        # than giving up keeps an equation under an unlabelled
        # subsection attached to its labelled parent, which is true
        # containment and the answer a reader wants anyway.
        for section in enclosing:
            nid = next(
                (anchors[a] for a in section.get("ids", ()) if a in anchors),
                None,
            )
            if nid is None:
                continue
            graph.add_edge(GraphEdge(
                source=nid, target=target_id, type=EdgeType.CONTAINS,
            ))
            nested += 1
            break
    return nested


def _labelled_statement_id(label: str, g: "nx.MultiDiGraph") -> str | None:
    """The graph node a labelled doctree element denotes, or ``None``.

    Equations and proof environments both carry a ``label`` and are
    spelled in different domains, so the lookup tries each rather than
    branching on the element's CLASS — which would tie this pass to
    `sphinxcontrib-proof` being importable.
    """
    for candidate in (
        node_id("math", NodeType.EQUATION, label),
        node_id("prf", NodeType.PROOF_OBJECT, label),
    ):
        if candidate in g:
            return candidate
    return None


def _is_valid_identifier(reftarget: str) -> bool:
    """Check if reftarget looks like a real Python/RST identifier.

    Filters out napoleon parsing artifacts like "0 = P0 isotropic",
    "+ 1", '"bicgstab".', etc.
    """
    if not reftarget:
        return False
    if " " in reftarget:
        return False
    if '"' in reftarget or "'" in reftarget:
        return False
    if not (reftarget[0].isalpha() or reftarget[0] == "_"):
        return False
    if reftarget in (".", "..") or "/" in reftarget:
        return False
    return True


def _build_external_names() -> frozenset[str]:
    """Build the set of names that are external to any project.

    Combines:
    - Python builtins (int, float, str, Exception, ...)
    - typing module names (Any, Optional, Union, ...)
    - stdlib module names (os, json, collections, ...)
    - Third-party installed packages (numpy, scipy, ...)
    """
    import builtins
    import importlib.metadata
    import sys
    import typing

    names: set[str] = set()

    # All builtins (types, exceptions, constants)
    names.update(dir(builtins))

    # typing module (Any, Optional, Union, Callable, etc.)
    names.update(name for name in dir(typing) if not name.startswith("_"))

    # stdlib modules (os, json, collections, pathlib, etc.)
    names.update(sys.stdlib_module_names)

    # Third-party packages (numpy, scipy, matplotlib, etc.)
    try:
        pkg_map = importlib.metadata.packages_distributions()
        names.update(pkg_map.keys())
    except Exception:
        pass

    return frozenset(names)


# Built once at import time — the set of external names available
# in the current Python environment.
_EXTERNAL_NAMES: frozenset[str] = _build_external_names()


def _classify_unresolved(
    reftarget: str,
    project_modules: frozenset[str],
) -> NodeType | None:
    """Classify an unresolved reference as EXTERNAL, UNRESOLVED, or None (noise).

    Uses the Python environment to detect builtins, stdlib, and installed
    packages. project_modules is the set of top-level module names
    documented by the Sphinx project (to avoid classifying them as external).

    Returns None for references that are clearly not valid identifiers.
    """
    if not _is_valid_identifier(reftarget):
        return None

    # Extract top-level name: "numpy.ndarray" → "numpy", "int" → "int"
    top_level = reftarget.split(".")[0]

    # If the top-level is a documented project module, it's not external
    if top_level in project_modules:
        return NodeType.UNRESOLVED

    # Check against builtins, stdlib, typing, and installed packages
    if top_level in _EXTERNAL_NAMES or reftarget in _EXTERNAL_NAMES:
        return NodeType.EXTERNAL

    return NodeType.UNRESOLVED


def _get_project_modules(env: BuildEnvironment) -> frozenset[str]:
    """Get top-level module names documented by the project's Python domain."""
    py_domain = env.domains.get("py")
    if py_domain is None:
        return frozenset()
    modules: set[str] = set()
    for name, _dispname, obj_type, _docname, _anchor, _prio in py_domain.get_objects():
        if obj_type == "module":
            modules.add(name.split(".")[0])
    return frozenset(modules)


def extract_references(env: BuildEnvironment, graph: KnowledgeGraph) -> None:
    """Walk doctrees for pending_xref nodes and create edges."""
    project_modules = _get_project_modules(env)
    # The enrichment pass is a full doctree traversal per page. Most
    # projects have no proof environments at all — check once rather
    # than pay for it on every document.
    enrich_proofs = any(
        isinstance(n, str) and n.startswith("prf:") for n in graph.nxgraph
    )
    anchors_by_doc = _section_anchor_index(graph)
    nested = 0

    for docname in env.all_docs:
        try:
            doctree = env.get_doctree(docname)
        except Exception:
            logger.debug("Could not load doctree for %s", docname)
            continue

        if enrich_proofs:
            _enrich_proof_nodes(doctree, graph)
        nested += _nest_labelled_statements(
            doctree, graph, anchors_by_doc.get(docname, {}),
        )

        source_id = doc_node_id(docname)
        seen_edges: set[tuple[str, str, str]] = set()  # (source, target, edge_type)

        for ref_node in doctree.findall(addnodes.pending_xref):
            refdomain = ref_node.get("refdomain", "")
            reftype = ref_node.get("reftype", "")
            reftarget = ref_node.get("reftarget", "")

            if not reftarget:
                continue

            # Citations. Docutils' own citation domain says
            # refdomain="citation"; sphinxcontrib-bibtex says
            # refdomain="cite" with reftype="p"/"t"/… — and only the
            # first was matched, so every bibtex citation fell through
            # to the generic branch below and minted `cite:p:<key>`
            # typed `unresolved`. [M] 2026-08-16 on ORPHEUS: 72 such
            # nodes, and ZERO reaching this branch.
            if refdomain in ("citation", "cite"):
                target_id = node_id("cite", NodeType.CITATION.value, reftarget)
                if not graph.has_node(target_id):
                    graph.add_node(GraphNode(
                        id=target_id,
                        type=NodeType.CITATION,
                        name=reftarget,
                        display_name=reftarget,
                        domain="cite",
                        docname=docname,
                    ))
                cite_key = (source_id, target_id, "cites")
                if cite_key not in seen_edges:
                    seen_edges.add(cite_key)
                    graph.add_edge(GraphEdge(
                        source=source_id,
                        target=target_id,
                        type=EdgeType.CITES,
                        metadata={"reftarget": reftarget},
                    ))
                continue

            edge_type = REFTYPE_EDGE_MAP.get(reftype, EdgeType.REFERENCES)

            # Resolve using domain-aware lookup
            domain_obj = env.domains.get(refdomain) if refdomain else None
            target_id = resolve_target_id(
                graph.nxgraph, domain_obj, refdomain, reftype, reftarget,
            )

            if target_id is None:
                # A role wrapped across source lines leaves a newline mid-name
                # (``:meth:`CoupledOperator.<newline>apply_transpose```).
                # Sphinx normalizes that away when it RESOLVES; when it cannot,
                # the raw text reaches us and would be forged into an id no
                # reference can ever match. The docstring scanner has collapsed
                # this since it was written — this path had not, the same
                # one-of-two-producers split as REFTYPE_OBJTYPE_MAP below.
                reftarget = _normalize_wrapped_target(reftarget)

                # Classify the unresolved target
                node_type = _classify_unresolved(reftarget, project_modules)
                if node_type is None:
                    # Noise: napoleon artifact, not a real identifier
                    continue
                if reftype == "doc":
                    target_id = doc_node_id(reftarget)
                elif reftype == "eq":
                    target_id = node_id("math", "equation", reftarget)
                else:
                    domain = refdomain or "std"
                    role = reftype or "any"
                    if domain == "py":
                        # An id is spelled with the TYPE, so a raw role
                        # here mints `py:func:X` beside the docstring
                        # scanner's `py:function:X` — one symbol, two
                        # nodes, its edges split between them.
                        objtype = REFTYPE_OBJTYPE_MAP.get(
                            role, NodeType.UNRESOLVED.value,
                        )
                    else:
                        objtype = REFTYPE_NODETYPE_MAP.get(
                            role, NodeType.UNRESOLVED,
                        )
                    target_id = node_id(domain, objtype, reftarget)
                if not graph.has_node(target_id):
                    graph.add_node(GraphNode(
                        id=target_id,
                        type=node_type,
                        name=reftarget,
                        display_name=reftarget,
                        domain=refdomain or "std",
                        docname="",
                    ))

            # Deduplicate: one edge per (source, target, type) per page.
            # Multiple :func:`solve_cp` on the same page → one DOCUMENTS edge.
            edge_key = (source_id, target_id, edge_type.value)
            if edge_key not in seen_edges:
                seen_edges.add(edge_key)
                graph.add_edge(GraphEdge(
                    source=source_id,
                    target=target_id,
                    type=edge_type,
                    metadata={
                        "refdomain": refdomain,
                        "reftype": reftype,
                        "reftarget": reftarget,
                    },
                ))

    if nested:
        logger.info("Nested %d labelled statement(s) under their section", nested)



def build_graph(env: BuildEnvironment) -> KnowledgeGraph:
    """Run all extractors and return the complete graph."""
    graph = KnowledgeGraph()
    extract_documents(env, graph)
    extract_domain_objects(env, graph)
    # Before references: an xref can only resolve to a node that exists.
    extract_proof_objects(env, graph)
    extract_references(env, graph)
    return graph
