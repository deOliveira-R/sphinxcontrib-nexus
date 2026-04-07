"""Knowledge graph data model backed by NetworkX MultiDiGraph."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

import networkx as nx


class NodeType(str, Enum):
    FILE = "file"
    SECTION = "section"
    FUNCTION = "function"
    CLASS = "class"
    METHOD = "method"
    ATTRIBUTE = "attribute"
    MODULE = "module"
    EQUATION = "equation"
    TERM = "term"
    DATA = "data"
    EXCEPTION = "exception"
    TYPE = "type"
    EXTERNAL = "external"
    UNRESOLVED = "unresolved"


class EdgeType(str, Enum):
    REFERENCES = "references"
    DOCUMENTS = "documents"
    IMPLEMENTS = "implements"
    CONTAINS = "contains"
    CITES = "cites"
    EQUATION_REF = "equation_ref"
    CALLS = "calls"
    IMPORTS = "imports"
    INHERITS = "inherits"
    TYPE_USES = "type_uses"
    TESTS = "tests"
    DERIVES = "derives"


@dataclass
class GraphNode:
    """Construction helper for adding nodes to the graph."""

    id: str
    type: NodeType | str
    name: str
    display_name: str = ""
    domain: str = ""
    docname: str = ""
    anchor: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class GraphEdge:
    """Construction helper for adding edges to the graph."""

    source: str
    target: str
    type: EdgeType
    metadata: dict[str, Any] = field(default_factory=dict)


class KnowledgeGraph:
    """Knowledge graph backed by networkx.MultiDiGraph.

    Nodes and edges are stored in the NetworkX graph with their attributes
    flattened into node/edge data dicts. Enum values are stored as strings
    for clean serialization.
    """

    def __init__(self) -> None:
        self._graph: nx.MultiDiGraph = nx.MultiDiGraph()
        self.metadata: dict[str, Any] = {}
        self._edge_key: int = 0

    @property
    def nxgraph(self) -> nx.MultiDiGraph:
        """Direct access to the underlying NetworkX graph."""
        return self._graph

    def add_node(self, node: GraphNode) -> None:
        """Add a node, flattening dataclass fields into nx attributes."""
        attrs = asdict(node)
        node_id = attrs.pop("id")
        meta = attrs.pop("metadata", {})
        attrs.update(meta)
        if isinstance(attrs.get("type"), Enum):
            attrs["type"] = attrs["type"].value
        self._graph.add_node(node_id, **attrs)

    def add_edge(self, edge: GraphEdge) -> None:
        """Add an edge with an auto-incremented unique key."""
        attrs = asdict(edge)
        source = attrs.pop("source")
        target = attrs.pop("target")
        meta = attrs.pop("metadata", {})
        attrs.update(meta)
        if isinstance(attrs.get("type"), Enum):
            attrs["type"] = attrs["type"].value
        key = self._edge_key
        self._edge_key += 1
        self._graph.add_edge(source, target, key=key, **attrs)

    def has_node(self, node_id: str) -> bool:
        return node_id in self._graph

    @property
    def node_count(self) -> int:
        return self._graph.number_of_nodes()

    @property
    def edge_count(self) -> int:
        return self._graph.number_of_edges()
