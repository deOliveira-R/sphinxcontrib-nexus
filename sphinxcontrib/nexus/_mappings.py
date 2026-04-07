"""Type mapping constants and domain-aware reference resolution."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sphinxcontrib.nexus.graph import EdgeType, NodeType

if TYPE_CHECKING:
    import networkx as nx

# Map (domain_name, obj_type) from Domain.get_objects() to our NodeType.
DOMAIN_TYPE_MAP: dict[tuple[str, str], NodeType] = {
    # Python domain
    ("py", "function"): NodeType.FUNCTION,
    ("py", "class"): NodeType.CLASS,
    ("py", "method"): NodeType.METHOD,
    ("py", "attribute"): NodeType.ATTRIBUTE,
    ("py", "module"): NodeType.MODULE,
    ("py", "data"): NodeType.DATA,
    ("py", "exception"): NodeType.EXCEPTION,
    ("py", "property"): NodeType.ATTRIBUTE,
    ("py", "staticmethod"): NodeType.METHOD,
    ("py", "classmethod"): NodeType.METHOD,
    ("py", "type"): NodeType.TYPE,
    # C domain
    ("c", "function"): NodeType.FUNCTION,
    ("c", "type"): NodeType.TYPE,
    ("c", "macro"): NodeType.FUNCTION,
    ("c", "var"): NodeType.DATA,
    ("c", "member"): NodeType.ATTRIBUTE,
    ("c", "enum"): NodeType.TYPE,
    ("c", "enumerator"): NodeType.DATA,
    # C++ domain
    ("cpp", "function"): NodeType.FUNCTION,
    ("cpp", "class"): NodeType.CLASS,
    ("cpp", "type"): NodeType.TYPE,
    ("cpp", "var"): NodeType.DATA,
    ("cpp", "member"): NodeType.ATTRIBUTE,
    ("cpp", "enum"): NodeType.TYPE,
    ("cpp", "enumerator"): NodeType.DATA,
    ("cpp", "namespace"): NodeType.MODULE,
    # JavaScript domain
    ("js", "function"): NodeType.FUNCTION,
    ("js", "class"): NodeType.CLASS,
    ("js", "method"): NodeType.METHOD,
    ("js", "attribute"): NodeType.ATTRIBUTE,
    ("js", "module"): NodeType.MODULE,
    ("js", "data"): NodeType.DATA,
    # Standard domain
    ("std", "term"): NodeType.TERM,
    ("std", "label"): NodeType.SECTION,
    ("std", "doc"): NodeType.FILE,
}

# Map pending_xref reftype to EdgeType.
REFTYPE_EDGE_MAP: dict[str, EdgeType] = {
    "ref": EdgeType.REFERENCES,
    "doc": EdgeType.REFERENCES,
    "func": EdgeType.DOCUMENTS,
    "meth": EdgeType.DOCUMENTS,
    "class": EdgeType.DOCUMENTS,
    "mod": EdgeType.DOCUMENTS,
    "attr": EdgeType.DOCUMENTS,
    "data": EdgeType.DOCUMENTS,
    "exc": EdgeType.DOCUMENTS,
    "type": EdgeType.DOCUMENTS,
    "obj": EdgeType.DOCUMENTS,
    "term": EdgeType.REFERENCES,
    "eq": EdgeType.EQUATION_REF,
    "numref": EdgeType.REFERENCES,
    "keyword": EdgeType.REFERENCES,
    "token": EdgeType.REFERENCES,
    "option": EdgeType.REFERENCES,
    "envvar": EdgeType.REFERENCES,
    "citation": EdgeType.CITES,
}


def resolve_target_id(
    nxgraph: nx.MultiDiGraph,
    domain: Any | None,
    refdomain: str,
    reftype: str,
    reftarget: str,
) -> str | None:
    """Resolve a pending_xref's attributes to a node ID in the graph.

    Uses the domain's object_types to programmatically map reftypes
    (e.g. "func") to obj_types (e.g. "function") instead of hardcoding.
    Falls back to suffix matching for short-name references
    (e.g. "CPMesh" matching "collision_probability.CPMesh").

    Returns the node ID if found, None if unresolved.
    """
    # Special cases: :doc: and :eq: have their own ID schemes
    if reftype == "doc":
        nid = f"doc:{reftarget}"
        return nid if nid in nxgraph else None
    if reftype == "eq":
        nid = f"math:equation:{reftarget}"
        return nid if nid in nxgraph else None

    # Collect candidate obj_types for this reftype
    candidate_objtypes = [reftype]
    if domain is not None:
        for obj_type_name, obj_type in getattr(domain, "object_types", {}).items():
            if reftype in obj_type.roles and obj_type_name not in candidate_objtypes:
                candidate_objtypes.append(obj_type_name)

    # Try exact match first
    for objtype in candidate_objtypes:
        nid = f"{refdomain}:{objtype}:{reftarget}"
        if nid in nxgraph:
            return nid

    # Suffix match: "CPMesh" matches "collision_probability.CPMesh"
    suffix = f".{reftarget}"
    for objtype in candidate_objtypes:
        prefix = f"{refdomain}:{objtype}:"
        for node_id in nxgraph:
            if isinstance(node_id, str) and node_id.startswith(prefix):
                name = node_id[len(prefix):]
                if name.endswith(suffix) or name == reftarget:
                    return node_id

    return None
