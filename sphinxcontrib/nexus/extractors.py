"""Extract nodes and edges from a Sphinx BuildEnvironment."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sphinx import addnodes

from sphinxcontrib.nexus._mappings import (
    DOMAIN_TYPE_MAP,
    REFTYPE_EDGE_MAP,
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
    from sphinx.environment import BuildEnvironment

logger = logging.getLogger(__name__)


def _node_id(domain: str, obj_type: str, name: str) -> str:
    return f"{domain}:{obj_type}:{name}"


def _doc_node_id(docname: str) -> str:
    return f"doc:{docname}"


def extract_documents(env: BuildEnvironment, graph: KnowledgeGraph) -> None:
    """Create a FILE node for every document and CONTAINS edges from toctree."""
    for docname in env.all_docs:
        node_id = _doc_node_id(docname)
        title = str(env.titles.get(docname, docname))
        graph.add_node(GraphNode(
            id=node_id,
            type=NodeType.FILE,
            name=docname,
            display_name=title,
            domain="std",
            docname=docname,
        ))

    toctree_includes = getattr(env, "toctree_includes", {})
    for parent, children in toctree_includes.items():
        parent_id = _doc_node_id(parent)
        for child in children:
            child_id = _doc_node_id(child)
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
            node_id = _node_id(domain_name, obj_type, name)
            graph.add_node(GraphNode(
                id=node_id,
                type=node_type,
                name=name,
                display_name=dispname,
                domain=domain_name,
                docname=docname,
                anchor=anchor,
            ))

            doc_id = _doc_node_id(docname)
            if graph.has_node(doc_id):
                graph.add_edge(GraphEdge(
                    source=doc_id,
                    target=node_id,
                    type=EdgeType.CONTAINS,
                ))

    # Math domain: get_objects() returns empty, read data directly
    math_domain = env.domains.get("math")
    if math_domain is not None:
        equations = getattr(math_domain, "data", {}).get("objects", {})
        for label, (docname, eqno) in equations.items():
            node_id = _node_id("math", "equation", label)
            graph.add_node(GraphNode(
                id=node_id,
                type=NodeType.EQUATION,
                name=label,
                display_name=f"({eqno})",
                domain="math",
                docname=docname,
                metadata={"eqno": eqno},
            ))
            doc_id = _doc_node_id(docname)
            if graph.has_node(doc_id):
                graph.add_edge(GraphEdge(
                    source=doc_id,
                    target=node_id,
                    type=EdgeType.CONTAINS,
                ))


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

    for docname in env.all_docs:
        try:
            doctree = env.get_doctree(docname)
        except Exception:
            logger.debug("Could not load doctree for %s", docname)
            continue

        source_id = _doc_node_id(docname)
        seen_edges: set[tuple[str, str, str]] = set()  # (source, target, edge_type)

        for ref_node in doctree.findall(addnodes.pending_xref):
            refdomain = ref_node.get("refdomain", "")
            reftype = ref_node.get("reftype", "")
            reftarget = ref_node.get("reftarget", "")

            if not reftarget:
                continue

            # Citations: refdomain="citation", reftype="ref"
            if refdomain == "citation":
                target_id = f"citation:{reftarget}"
                if not graph.has_node(target_id):
                    graph.add_node(GraphNode(
                        id=target_id,
                        type=NodeType.UNRESOLVED,
                        name=reftarget,
                        display_name=reftarget,
                        domain="citation",
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
                # Classify the unresolved target
                node_type = _classify_unresolved(reftarget, project_modules)
                if node_type is None:
                    # Noise: napoleon artifact, not a real identifier
                    continue
                if reftype == "doc":
                    target_id = _doc_node_id(reftarget)
                elif reftype == "eq":
                    target_id = _node_id("math", "equation", reftarget)
                else:
                    target_id = _node_id(
                        refdomain or "std", reftype or "any", reftarget,
                    )
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



def build_graph(env: BuildEnvironment) -> KnowledgeGraph:
    """Run all extractors and return the complete graph."""
    graph = KnowledgeGraph()
    extract_documents(env, graph)
    extract_domain_objects(env, graph)
    extract_references(env, graph)
    return graph
