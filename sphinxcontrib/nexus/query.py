"""Query interface over a KnowledgeGraph.

No Sphinx imports — usable standalone with a loaded JSON graph.
"""

from __future__ import annotations

import re
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import networkx as nx

from sphinxcontrib.nexus.graph import KnowledgeGraph


@dataclass
class NodeResult:
    """A node in query results."""

    id: str
    type: str = ""
    name: str = ""
    display_name: str = ""
    domain: str = ""
    docname: str = ""
    degree: int = 0


@dataclass
class EdgeResult:
    """An edge in query results."""

    source: str
    target: str
    type: str = ""
    key: str = ""


@dataclass
class PathResult:
    """A path between two nodes."""

    nodes: list[str]
    edges: list[EdgeResult]
    length: int


@dataclass
class ImpactResult:
    """Blast radius analysis result."""

    target: str
    direction: str
    by_depth: dict[int, list[NodeResult]] = field(default_factory=dict)
    total_affected: int = 0


@dataclass
class StatsResult:
    """Graph statistics."""

    node_count: int
    edge_count: int
    nodes_by_type: dict[str, int]
    edges_by_type: dict[str, int]
    connected_components: int
    density: float


@dataclass
class CommunityResult:
    """A detected functional community/module."""

    id: int
    members: list[NodeResult]
    size: int
    label: str = ""
    cohesion: float = 0.0


@dataclass
class ProcessStep:
    """One step in an execution flow."""

    node: NodeResult
    step_number: int
    calls_next: str = ""


@dataclass
class ProcessResult:
    """A named execution flow (sequence of function calls)."""

    name: str
    entry_point: NodeResult
    steps: list[ProcessStep]
    length: int


@dataclass
class BridgeResult:
    """A bridge node connecting otherwise-separate communities."""

    node: NodeResult
    communities_connected: list[int]
    betweenness: float


@dataclass
class ChangeEntry:
    """A symbol affected by a git change."""

    node: NodeResult
    change_type: str  # "added", "modified", "deleted"
    file_path: str = ""


@dataclass
class DetectChangesResult:
    """Result of git-diff impact analysis."""

    changed_symbols: list[ChangeEntry]
    affected_symbols: list[NodeResult]
    total_changed: int
    total_affected: int


@dataclass
class RenameEdit:
    """A proposed rename edit."""

    file_path: str
    old_text: str
    new_text: str
    lineno: int = 0
    confidence: str = "high"  # "high" (graph-found), "medium" (regex-found)


@dataclass
class RenameResult:
    """Result of a safe rename analysis."""

    old_name: str
    new_name: str
    edits: list[RenameEdit]
    total_edits: int


@dataclass
class ProvenanceStep:
    """One step in a provenance chain."""

    node: NodeResult
    edge_type: str
    depth: int


@dataclass
class ProvenanceResult:
    """Full citation → equation → code traceability chain."""

    target: str
    chain: list[ProvenanceStep]
    equations: list[NodeResult]
    citations: list[str]


@dataclass
class CoverageEntry:
    """Verification coverage status of one equation or function."""

    node: NodeResult
    status: str  # "verified", "tested", "implemented", "documented", "orphan_code"
    equation: NodeResult | None = None
    implementing_code: list[NodeResult] = field(default_factory=list)
    tests: list[NodeResult] = field(default_factory=list)


@dataclass
class CoverageResult:
    """Verification coverage report."""

    entries: list[CoverageEntry]
    summary: dict[str, int]


@dataclass
class StalenessEntry:
    """A stale documentation page."""

    doc_node: NodeResult
    stale_reason: str
    code_modified: str  # ISO timestamp or "unknown"
    doc_modified: str
    affected_symbols: list[str]


@dataclass
class StalenessResult:
    """Doc-code drift analysis."""

    stale_docs: list[StalenessEntry]
    total_stale: int
    total_checked: int


@dataclass
class BriefingResult:
    """Session briefing for an AI agent."""

    graph_stats: StatsResult
    god_nodes: list[NodeResult]
    stale_docs: list[StalenessEntry]
    coverage_gaps: list[CoverageEntry]
    recent_changes: list[ChangeEntry]
    unresolved_count: int
    external_count: int


@dataclass
class RetestResult:
    """Minimum set of tests to re-run."""

    must_retest: list[NodeResult]
    should_retest: list[NodeResult]
    changed_symbols: list[str]
    total_tests: int
    safe_to_skip: int


@dataclass
class TraceErrorResult:
    """Equation-to-error trace from a failing test."""

    test_node: str
    call_chain: list[NodeResult]
    equations_on_path: list[NodeResult]
    citations: list[str]


@dataclass
class MigrationPhase:
    """One phase of a dependency migration plan."""

    phase: int
    label: str
    functions: list[NodeResult]
    blast_radius: int


@dataclass
class MigrationResult:
    """Dependency migration plan."""

    from_dep: str
    to_dep: str
    phases: list[MigrationPhase]
    doc_updates: list[NodeResult]
    total_functions: int


class GraphQuery:
    """Query interface over a KnowledgeGraph or raw nx.MultiDiGraph.

    Designed to be usable standalone (no Sphinx dependency).
    """

    def __init__(self, graph: KnowledgeGraph | nx.MultiDiGraph) -> None:
        if isinstance(graph, KnowledgeGraph):
            self._g = graph.nxgraph
        else:
            self._g = graph

    def _node_result(self, node_id: str) -> NodeResult:
        """Build a NodeResult from a node ID."""
        attrs = self._g.nodes.get(node_id, {})
        return NodeResult(
            id=node_id,
            type=attrs.get("type", ""),
            name=attrs.get("name", ""),
            display_name=attrs.get("display_name", ""),
            domain=attrs.get("domain", ""),
            docname=attrs.get("docname", ""),
            degree=self._g.degree(node_id),
        )

    def _edge_result(
        self, source: str, target: str, key: str | int, data: dict,
    ) -> EdgeResult:
        """Build an EdgeResult from edge data."""
        return EdgeResult(
            source=source,
            target=target,
            type=data.get("type", ""),
            key=str(key),
        )

    def get_node(self, node_id: str) -> NodeResult | None:
        """Get a single node by ID."""
        if node_id not in self._g:
            return None
        return self._node_result(node_id)

    def neighbors(
        self,
        node_id: str,
        direction: Literal["in", "out", "both"] = "both",
        edge_types: list[str] | None = None,
    ) -> list[tuple[NodeResult, EdgeResult]]:
        """Direct connections of a node, optionally filtered by edge type."""
        if node_id not in self._g:
            return []

        results: list[tuple[NodeResult, EdgeResult]] = []

        if direction in ("out", "both"):
            for _src, tgt, key, data in self._g.out_edges(node_id, keys=True, data=True):
                if edge_types and data.get("type") not in edge_types:
                    continue
                results.append((
                    self._node_result(tgt),
                    self._edge_result(node_id, tgt, key, data),
                ))

        if direction in ("in", "both"):
            for src, _tgt, key, data in self._g.in_edges(node_id, keys=True, data=True):
                if edge_types and data.get("type") not in edge_types:
                    continue
                results.append((
                    self._node_result(src),
                    self._edge_result(src, node_id, key, data),
                ))

        return results

    def impact(
        self,
        target: str,
        direction: Literal["upstream", "downstream"] = "upstream",
        max_depth: int = 3,
        edge_types: list[str] | None = None,
    ) -> ImpactResult:
        """Transitive blast radius via BFS.

        - upstream: follow in-edges (what depends on this)
        - downstream: follow out-edges (what this depends on)
        """
        if target not in self._g:
            return ImpactResult(target=target, direction=direction)

        # Build a filtered view if edge_types specified
        if edge_types:
            def edge_filter(u: str, v: str, k: str | int) -> bool:
                return self._g.edges[u, v, k].get("type") in edge_types
            view = nx.subgraph_view(self._g, filter_edge=edge_filter)
        else:
            view = self._g

        # BFS traversal in the appropriate direction
        if direction == "upstream":
            traversal = nx.bfs_layers(view.reverse(copy=False), [target])
        else:
            traversal = nx.bfs_layers(view, [target])

        by_depth: dict[int, list[NodeResult]] = {}
        total = 0
        for depth, layer in enumerate(traversal):
            if depth == 0:
                continue  # skip the target itself
            if depth > max_depth:
                break
            by_depth[depth] = [self._node_result(n) for n in layer]
            total += len(layer)

        return ImpactResult(
            target=target,
            direction=direction,
            by_depth=by_depth,
            total_affected=total,
        )

    def shortest_path(
        self,
        source: str,
        target: str,
        max_hops: int = 8,
    ) -> PathResult | None:
        """Find shortest path between two nodes (undirected connectivity)."""
        if source not in self._g or target not in self._g:
            return None

        undirected = self._g.to_undirected(as_view=True)
        try:
            path = nx.shortest_path(undirected, source, target, weight=None)
        except nx.NetworkXNoPath:
            return None

        if len(path) - 1 > max_hops:
            return None

        # Collect edges along the path
        edges: list[EdgeResult] = []
        for u, v in zip(path[:-1], path[1:]):
            # Get first edge between u and v in either direction
            if self._g.has_edge(u, v):
                edge_data = next(iter(self._g[u][v].values()))
                key = next(iter(self._g[u][v]))
                edges.append(self._edge_result(u, v, key, edge_data))
            elif self._g.has_edge(v, u):
                edge_data = next(iter(self._g[v][u].values()))
                key = next(iter(self._g[v][u]))
                edges.append(self._edge_result(v, u, key, edge_data))

        return PathResult(nodes=path, edges=edges, length=len(path) - 1)

    def query(
        self,
        text: str,
        node_types: list[str] | None = None,
        limit: int = 20,
    ) -> list[NodeResult]:
        """Keyword search across node IDs, names, and display_names.

        Handles multi-word queries by requiring ALL tokens to match
        somewhere in the searchable text. Normalizes underscores, dots,
        and colons to spaces for matching, so "collision probability"
        matches "collision_probability.CPMesh".
        """
        # Tokenize query: split on spaces, underscores, dots
        tokens = re.split(r"[\s_.:]+", text.lower())
        tokens = [t for t in tokens if t]
        if not tokens:
            return []

        results: list[NodeResult] = []

        for node_id, attrs in self._g.nodes(data=True):
            if node_types and attrs.get("type") not in node_types:
                continue
            # Build searchable text from ID + name + display_name
            name = attrs.get("name", "")
            display_name = attrs.get("display_name", "")
            searchable = f"{node_id} {name} {display_name}".lower()
            # Normalize separators to spaces for token matching
            searchable = re.sub(r"[_.:]+", " ", searchable)

            if all(t in searchable for t in tokens):
                results.append(self._node_result(node_id))

        results.sort(key=lambda r: r.degree, reverse=True)
        return results[:limit]

    def god_nodes(self, top_n: int = 10) -> list[NodeResult]:
        """Most connected nodes by total degree."""
        degree_pairs = sorted(
            self._g.degree(), key=lambda x: x[1], reverse=True,
        )
        return [self._node_result(nid) for nid, _ in degree_pairs[:top_n]]

    def stats(self) -> StatsResult:
        """Graph-level statistics."""
        nodes_by_type: Counter[str] = Counter()
        for _, attrs in self._g.nodes(data=True):
            nodes_by_type[attrs.get("type", "unknown")] += 1

        edges_by_type: Counter[str] = Counter()
        for _, _, attrs in self._g.edges(data=True):
            edges_by_type[attrs.get("type", "unknown")] += 1

        return StatsResult(
            node_count=self._g.number_of_nodes(),
            edge_count=self._g.number_of_edges(),
            nodes_by_type=dict(nodes_by_type),
            edges_by_type=dict(edges_by_type),
            connected_components=nx.number_weakly_connected_components(self._g),
            density=nx.density(self._g),
        )

    # ------------------------------------------------------------------
    # Community detection
    # ------------------------------------------------------------------

    def communities(self, min_size: int = 2) -> list[CommunityResult]:
        """Detect functional communities using greedy modularity.

        Returns communities sorted by size (largest first) with cohesion
        scores indicating how tightly connected each community is.
        """
        undirected = self._g.to_undirected()
        try:
            raw = nx.community.greedy_modularity_communities(undirected)
        except Exception:
            return []

        results: list[CommunityResult] = []
        for i, members in enumerate(raw):
            if len(members) < min_size:
                continue
            member_nodes = [self._node_result(m) for m in members]
            # Label: most common non-file node type + most connected member
            type_counts = Counter(n.type for n in member_nodes if n.type != "file")
            top_type = type_counts.most_common(1)[0][0] if type_counts else "mixed"
            top_member = max(member_nodes, key=lambda n: n.degree)
            label = f"{top_type}:{top_member.name}"
            # Cohesion: density of the subgraph induced by this community
            subgraph = undirected.subgraph(members)
            cohesion = nx.density(subgraph) if len(members) > 1 else 1.0
            results.append(CommunityResult(
                id=i,
                members=member_nodes,
                size=len(members),
                label=label,
                cohesion=round(cohesion, 4),
            ))

        results.sort(key=lambda c: c.size, reverse=True)
        return results

    # ------------------------------------------------------------------
    # Git-diff impact analysis
    # ------------------------------------------------------------------

    def detect_changes(
        self,
        project_root: Path | str,
        scope: str = "staged",
    ) -> DetectChangesResult:
        """Detect which symbols changed in git and their impact.

        Args:
            project_root: Root of the git repository.
            scope: "staged" (git diff --cached), "unstaged" (git diff),
                   "all" (both), or "branch" (diff against main/master).
        """
        project_root = Path(project_root)
        changed_files = self._git_changed_files(project_root, scope)

        # Find graph nodes that live in changed files
        changed_symbols: list[ChangeEntry] = []
        for node_id, attrs in self._g.nodes(data=True):
            file_path = attrs.get("file_path", "")
            if not file_path:
                continue
            try:
                rel = str(Path(file_path).relative_to(project_root))
            except (ValueError, TypeError):
                rel = file_path
            if rel in changed_files:
                changed_symbols.append(ChangeEntry(
                    node=self._node_result(node_id),
                    change_type=changed_files[rel],
                    file_path=rel,
                ))

        # Compute upstream impact of all changed symbols
        affected_ids: set[str] = set()
        for entry in changed_symbols:
            result = self.impact(entry.node.id, direction="upstream", max_depth=2)
            for nodes in result.by_depth.values():
                for n in nodes:
                    affected_ids.add(n.id)
        # Remove the changed symbols themselves from affected
        changed_ids = {e.node.id for e in changed_symbols}
        affected_ids -= changed_ids

        return DetectChangesResult(
            changed_symbols=changed_symbols,
            affected_symbols=[self._node_result(nid) for nid in affected_ids],
            total_changed=len(changed_symbols),
            total_affected=len(affected_ids),
        )

    @staticmethod
    def _git_changed_files(
        project_root: Path, scope: str,
    ) -> dict[str, str]:
        """Run git diff and return changed files with change type."""
        files: dict[str, str] = {}

        def _run_diff(args: list[str]) -> None:
            try:
                result = subprocess.run(
                    ["git"] + args,
                    capture_output=True, text=True,
                    cwd=project_root, timeout=10,
                )
                for line in result.stdout.strip().splitlines():
                    parts = line.split("\t", 1)
                    if len(parts) == 2:
                        status, path = parts
                        if path.endswith(".py"):
                            change = {"A": "added", "M": "modified", "D": "deleted"}.get(
                                status[0], "modified",
                            )
                            files[path] = change
            except (subprocess.SubprocessError, FileNotFoundError):
                pass

        if scope in ("staged", "all"):
            _run_diff(["diff", "--cached", "--name-status"])
        if scope in ("unstaged", "all"):
            _run_diff(["diff", "--name-status"])
        if scope == "branch":
            # Diff against main or master
            for base in ("main", "master"):
                _run_diff(["diff", f"{base}...HEAD", "--name-status"])
                if files:
                    break

        return files

    # ------------------------------------------------------------------
    # Safe rename
    # ------------------------------------------------------------------

    def rename(
        self,
        old_name: str,
        new_name: str,
        project_root: Path | str | None = None,
        dry_run: bool = True,
    ) -> RenameResult:
        """Analyze or execute a safe rename across the codebase.

        Finds all references via the graph (high confidence) and
        via regex search in source files (medium confidence).

        Args:
            old_name: Current symbol name (e.g., "solve_sn" or "SNSolver").
            new_name: New name to rename to.
            project_root: Root for file searches. If None, only graph analysis.
            dry_run: If True, return edits without applying. If False, apply.
        """
        edits: list[RenameEdit] = []

        # 1. Graph-based: find all nodes and edges referencing old_name
        matching_nodes = [
            (nid, attrs) for nid, attrs in self._g.nodes(data=True)
            if old_name in attrs.get("name", "")
        ]

        for node_id, attrs in matching_nodes:
            file_path = attrs.get("file_path", "")
            lineno = attrs.get("lineno", 0)
            if file_path:
                edits.append(RenameEdit(
                    file_path=file_path,
                    old_text=old_name,
                    new_text=new_name,
                    lineno=lineno,
                    confidence="high",
                ))

        # 2. Regex-based: search source files for the name
        if project_root is not None:
            project_root = Path(project_root)
            pattern = re.compile(r'\b' + re.escape(old_name) + r'\b')
            for py_file in project_root.rglob("*.py"):
                if ".venv" in py_file.parts or "__pycache__" in py_file.parts:
                    continue
                try:
                    content = py_file.read_text(encoding="utf-8")
                except (UnicodeDecodeError, OSError):
                    continue
                for i, line in enumerate(content.splitlines(), 1):
                    if pattern.search(line):
                        rel = str(py_file.relative_to(project_root))
                        # Skip if already found by graph
                        if not any(
                            e.file_path == rel and e.lineno == i
                            for e in edits
                        ):
                            edits.append(RenameEdit(
                                file_path=rel,
                                old_text=old_name,
                                new_text=new_name,
                                lineno=i,
                                confidence="medium",
                            ))

        if not dry_run and project_root is not None:
            self._apply_renames(edits, Path(project_root))

        return RenameResult(
            old_name=old_name,
            new_name=new_name,
            edits=edits,
            total_edits=len(edits),
        )

    @staticmethod
    def _apply_renames(edits: list[RenameEdit], project_root: Path) -> None:
        """Apply rename edits to files."""
        # Group by file
        by_file: dict[str, list[RenameEdit]] = {}
        for edit in edits:
            by_file.setdefault(edit.file_path, []).append(edit)

        for rel_path, file_edits in by_file.items():
            fpath = project_root / rel_path
            try:
                content = fpath.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            # Simple global replace — the rename is the same for all edits
            old = file_edits[0].old_text
            new = file_edits[0].new_text
            pattern = re.compile(r'\b' + re.escape(old) + r'\b')
            new_content = pattern.sub(new, content)
            if new_content != content:
                fpath.write_text(new_content, encoding="utf-8")

    # ------------------------------------------------------------------
    # Feature 1: Provenance Chain
    # ------------------------------------------------------------------

    def provenance_chain(self, node_id: str) -> ProvenanceResult:
        """Trace citation → equation → code for a symbol.

        Given a code symbol, find which doc pages reference it, what
        equations those pages contain, and what citations they use.
        Given an equation, find the code documented on the same page.

        Traverses: code ←DOCUMENTS– doc –CONTAINS→ equations
                                        –CITES→ citations
        """
        equations: list[NodeResult] = []
        citations: list[str] = []
        chain: list[ProvenanceStep] = []
        seen_eqs: set[str] = set()
        seen_citations: set[str] = set()

        node = self.get_node(node_id)
        if node is None:
            return ProvenanceResult(target=node_id, chain=[], equations=[], citations=[])

        chain.append(ProvenanceStep(node=node, edge_type="target", depth=0))

        # Find doc pages connected to this node
        seen_docs: set[str] = set()
        seen_code: set[str] = set()
        doc_pages: list[str] = []

        if node.type in ("function", "method", "class", "module", "attribute"):
            for src, _, data in self._g.in_edges(node_id, data=True):
                src_type = self._g.nodes.get(src, {}).get("type", "")
                if src_type == "file" and data.get("type") in ("documents", "contains"):
                    if src not in seen_docs:
                        seen_docs.add(src)
                        doc_pages.append(src)
                        chain.append(ProvenanceStep(
                            node=self._node_result(src),
                            edge_type="documented_by", depth=1,
                        ))

        elif node.type == "equation":
            seen_eqs.add(node_id)
            equations.append(node)
            for src, _, data in self._g.in_edges(node_id, data=True):
                src_type = self._g.nodes.get(src, {}).get("type", "")
                if src_type == "file" and data.get("type") == "contains":
                    if src not in seen_docs:
                        seen_docs.add(src)
                        doc_pages.append(src)
                        chain.append(ProvenanceStep(
                            node=self._node_result(src),
                            edge_type="contained_by", depth=1,
                        ))
                        for _, tgt, d2 in self._g.out_edges(src, data=True):
                            tgt_type = self._g.nodes.get(tgt, {}).get("type", "")
                            if tgt_type in ("function", "method", "class") and d2.get("type") == "documents":
                                if tgt not in seen_code:
                                    seen_code.add(tgt)
                                    chain.append(ProvenanceStep(
                                        node=self._node_result(tgt),
                                        edge_type="implemented_by", depth=2,
                                    ))

        # From doc pages, collect equations and citations
        for doc_id in doc_pages:
            for _, tgt, data in self._g.out_edges(doc_id, data=True):
                tgt_type = self._g.nodes.get(tgt, {}).get("type", "")
                edge_type = data.get("type", "")

                if tgt_type == "equation" and tgt not in seen_eqs:
                    seen_eqs.add(tgt)
                    eq_node = self._node_result(tgt)
                    equations.append(eq_node)
                    chain.append(ProvenanceStep(
                        node=eq_node, edge_type="equation_on_page", depth=2,
                    ))

                if edge_type == "cites":
                    tgt_name = self._g.nodes.get(tgt, {}).get("name", tgt)
                    if tgt_name not in seen_citations:
                        seen_citations.add(tgt_name)
                        citations.append(tgt_name)
                        chain.append(ProvenanceStep(
                            node=self._node_result(tgt),
                            edge_type="cites", depth=3,
                        ))

        return ProvenanceResult(
            target=node_id,
            chain=chain,
            equations=equations,
            citations=list(set(citations)),
        )

    # ------------------------------------------------------------------
    # Feature 2: Verification Coverage Map
    # ------------------------------------------------------------------

    def verification_coverage(
        self, status_filter: str | None = None,
    ) -> CoverageResult:
        """Map verification coverage: equation → code → test chain.

        Status values:
        - "verified": equation + code + test
        - "tested": code + test, no equation link
        - "implemented": equation + code, no test
        - "documented": equation only, no implementing code
        - "orphan_code": code with no equation
        """
        code_types = {"function", "method", "class"}
        entries: list[CoverageEntry] = []
        summary: Counter[str] = Counter()

        # Build equation → implementing code mapping
        eq_to_code: dict[str, list[str]] = {}
        code_to_eq: dict[str, list[str]] = {}
        for src, tgt, data in self._g.edges(data=True):
            if data.get("type") == "implements":
                eq_to_code.setdefault(tgt, []).append(src)
                code_to_eq.setdefault(src, []).append(tgt)

        # Build code → test mapping (tests are functions with is_test=True that call code)
        code_to_tests: dict[str, list[str]] = {}
        for src, tgt, data in self._g.edges(data=True):
            if data.get("type") == "calls":
                src_attrs = self._g.nodes.get(src, {})
                if src_attrs.get("is_test"):
                    code_to_tests.setdefault(tgt, []).append(src)

        # Classify equations
        for node_id, attrs in self._g.nodes(data=True):
            if attrs.get("type") != "equation":
                continue
            implementing = eq_to_code.get(node_id, [])
            has_code = len(implementing) > 0
            has_test = any(
                code_to_tests.get(c) for c in implementing
            )
            if has_code and has_test:
                status = "verified"
            elif has_code:
                status = "implemented"
            else:
                status = "documented"

            if status_filter and status != status_filter:
                continue

            tests = []
            for c in implementing:
                tests.extend(self._node_result(t) for t in code_to_tests.get(c, []))

            entries.append(CoverageEntry(
                node=self._node_result(node_id),
                status=status,
                implementing_code=[self._node_result(c) for c in implementing],
                tests=tests,
            ))
            summary[status] += 1

        # Classify code symbols with no equation
        if status_filter in (None, "tested", "orphan_code"):
            for node_id, attrs in self._g.nodes(data=True):
                if attrs.get("type") not in code_types:
                    continue
                if node_id in code_to_eq:
                    continue  # already covered via equation
                has_test = bool(code_to_tests.get(node_id))
                status = "tested" if has_test else "orphan_code"
                if status_filter and status != status_filter:
                    continue
                entries.append(CoverageEntry(
                    node=self._node_result(node_id),
                    status=status,
                    tests=[self._node_result(t) for t in code_to_tests.get(node_id, [])],
                ))
                summary[status] += 1

        return CoverageResult(entries=entries, summary=dict(summary))

    # ------------------------------------------------------------------
    # Feature 3: Staleness Detector
    # ------------------------------------------------------------------

    def staleness(
        self, project_root: Path | str | None = None,
    ) -> StalenessResult:
        """Detect documentation pages that drifted from code.

        Compares git modification timestamps of doc files vs. the code
        files they reference.
        """
        stale: list[StalenessEntry] = []
        checked = 0

        if project_root is None:
            return StalenessResult(stale_docs=[], total_stale=0, total_checked=0)

        project_root = Path(project_root)
        timestamps = self._git_file_timestamps(project_root)

        for doc_id, attrs in self._g.nodes(data=True):
            if attrs.get("type") != "file":
                continue
            docname = attrs.get("docname", "")
            if not docname:
                continue

            # Find the RST file timestamp
            doc_ts = None
            for ext in (".rst", ".md"):
                doc_path = f"docs/{docname}{ext}"
                if doc_path in timestamps:
                    doc_ts = timestamps[doc_path]
                    break
            if doc_ts is None:
                continue

            checked += 1

            # Find code symbols documented by this page
            stale_symbols: list[str] = []
            latest_code_ts = ""
            for _, tgt, data in self._g.out_edges(doc_id, data=True):
                tgt_attrs = self._g.nodes.get(tgt, {})
                file_path = tgt_attrs.get("file_path", "")
                if not file_path:
                    continue
                try:
                    rel = str(Path(file_path).relative_to(project_root))
                except (ValueError, TypeError):
                    rel = file_path
                code_ts = timestamps.get(rel, "")
                if code_ts and code_ts > doc_ts:
                    stale_symbols.append(tgt_attrs.get("name", tgt))
                    if code_ts > latest_code_ts:
                        latest_code_ts = code_ts

            if stale_symbols:
                stale.append(StalenessEntry(
                    doc_node=self._node_result(doc_id),
                    stale_reason=f"{len(stale_symbols)} documented symbol(s) modified after doc",
                    code_modified=latest_code_ts,
                    doc_modified=doc_ts,
                    affected_symbols=stale_symbols,
                ))

        return StalenessResult(
            stale_docs=stale,
            total_stale=len(stale),
            total_checked=checked,
        )

    @staticmethod
    def _git_file_timestamps(project_root: Path) -> dict[str, str]:
        """Get last-modified ISO timestamps for all tracked files."""
        try:
            result = subprocess.run(
                ["git", "log", "--format=%aI", "--name-only", "--diff-filter=ACMR"],
                capture_output=True, text=True,
                cwd=project_root, timeout=30,
            )
            timestamps: dict[str, str] = {}
            current_ts = ""
            for line in result.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                if line.startswith("20"):  # ISO timestamp
                    current_ts = line
                elif current_ts and line not in timestamps:
                    timestamps[line] = current_ts
            return timestamps
        except (subprocess.SubprocessError, FileNotFoundError):
            return {}

    # ------------------------------------------------------------------
    # Feature 4: Session Briefing
    # ------------------------------------------------------------------

    def session_briefing(
        self, project_root: Path | str | None = None,
    ) -> BriefingResult:
        """Generate a structured briefing for an AI agent starting a session."""
        stats_result = self.stats()
        top_nodes = self.god_nodes(top_n=5)

        # Staleness
        stale_result = self.staleness(project_root)

        # Coverage gaps (equations with code but no tests)
        coverage = self.verification_coverage(status_filter="implemented")
        gaps = coverage.entries[:10]  # top 10 gaps

        # Recent changes
        changes_result = DetectChangesResult(
            changed_symbols=[], affected_symbols=[],
            total_changed=0, total_affected=0,
        )
        if project_root:
            changes_result = self.detect_changes(project_root, scope="branch")

        # Counts
        unresolved = sum(
            1 for _, a in self._g.nodes(data=True)
            if a.get("type") == "unresolved"
        )
        external = sum(
            1 for _, a in self._g.nodes(data=True)
            if a.get("type") == "external"
        )

        return BriefingResult(
            graph_stats=stats_result,
            god_nodes=top_nodes,
            stale_docs=stale_result.stale_docs[:5],
            coverage_gaps=gaps,
            recent_changes=changes_result.changed_symbols[:10],
            unresolved_count=unresolved,
            external_count=external,
        )

    # ------------------------------------------------------------------
    # Feature 5: Minimum Retest Set
    # ------------------------------------------------------------------

    def retest(
        self, project_root: Path | str,
        scope: str = "all",
    ) -> RetestResult:
        """Compute the minimum set of tests to re-run after changes."""
        changes = self.detect_changes(project_root, scope=scope)
        changed_ids = {e.node.id for e in changes.changed_symbols}

        # Find all test functions
        all_tests = {
            nid for nid, attrs in self._g.nodes(data=True)
            if attrs.get("is_test")
        }

        must_retest: set[str] = set()
        should_retest: set[str] = set()

        for entry in changes.changed_symbols:
            # Direct test callers (depth 1)
            result = self.impact(entry.node.id, direction="upstream", max_depth=3)
            for depth, nodes in result.by_depth.items():
                for n in nodes:
                    if n.id in all_tests:
                        if depth == 1:
                            must_retest.add(n.id)
                        else:
                            should_retest.add(n.id)

        # Remove overlap
        should_retest -= must_retest
        safe_to_skip = len(all_tests) - len(must_retest) - len(should_retest)

        return RetestResult(
            must_retest=[self._node_result(t) for t in must_retest],
            should_retest=[self._node_result(t) for t in should_retest],
            changed_symbols=[e.node.name for e in changes.changed_symbols],
            total_tests=len(all_tests),
            safe_to_skip=max(0, safe_to_skip),
        )

    # ------------------------------------------------------------------
    # Feature 6: Equation-to-Error Tracer
    # ------------------------------------------------------------------

    def trace_error(self, test_node_id: str) -> TraceErrorResult:
        """Trace from a failing test to the equations on its call path."""
        call_chain: list[NodeResult] = []
        equations: list[NodeResult] = []
        citations: list[str] = []
        seen: set[str] = set()

        def _walk_calls(node_id: str, depth: int = 0) -> None:
            if node_id in seen or depth > 10:
                return
            seen.add(node_id)
            node = self._node_result(node_id)
            call_chain.append(node)

            # Check for IMPLEMENTS edges (code → equation)
            for _, tgt, data in self._g.out_edges(node_id, data=True):
                if data.get("type") == "implements":
                    eq = self._node_result(tgt)
                    if eq.id not in {e.id for e in equations}:
                        equations.append(eq)
                        # Get citations for this equation's doc page
                        prov = self.provenance_chain(tgt)
                        citations.extend(prov.citations)

            # Follow CALLS edges
            for _, tgt, data in self._g.out_edges(node_id, data=True):
                if data.get("type") == "calls" and tgt not in seen:
                    _walk_calls(tgt, depth + 1)

        _walk_calls(test_node_id)

        return TraceErrorResult(
            test_node=test_node_id,
            call_chain=call_chain,
            equations_on_path=equations,
            citations=list(set(citations)),
        )

    # ------------------------------------------------------------------
    # Feature 7: Migration Planner
    # ------------------------------------------------------------------

    def migration_plan(
        self, from_dep: str, to_dep: str = "",
    ) -> MigrationResult:
        """Plan a dependency migration (e.g., numpy → jax).

        Groups affected functions into phases by blast radius:
        Phase 1 (leaf): no upstream callers outside the dep
        Phase 2 (mid): limited blast radius
        Phase 3 (core): high blast radius
        """
        # Find all nodes that use the dependency
        dep_nodes: list[str] = []
        for src, tgt, data in self._g.edges(data=True):
            if data.get("type") in ("calls", "type_uses", "imports"):
                tgt_name = self._g.nodes.get(tgt, {}).get("name", "")
                if tgt_name.startswith(from_dep + ".") or tgt_name == from_dep:
                    if src not in dep_nodes:
                        dep_nodes.append(src)

        # Compute blast radius for each affected function
        node_radius: list[tuple[str, int]] = []
        for nid in dep_nodes:
            result = self.impact(nid, direction="upstream", max_depth=2)
            node_radius.append((nid, result.total_affected))

        # Sort by blast radius (ascending = leaf first)
        node_radius.sort(key=lambda x: x[1])

        # Split into phases
        phases: list[MigrationPhase] = []
        if node_radius:
            third = max(1, len(node_radius) // 3)
            slices = [
                ("leaf (safe to change first)", node_radius[:third]),
                ("mid-level (moderate blast radius)", node_radius[third:2 * third]),
                ("core (high blast radius, change last)", node_radius[2 * third:]),
            ]
            for i, (label, items) in enumerate(slices, 1):
                if items:
                    phases.append(MigrationPhase(
                        phase=i,
                        label=label,
                        functions=[self._node_result(nid) for nid, _ in items],
                        blast_radius=sum(r for _, r in items),
                    ))

        # Find doc pages that reference the dependency
        doc_updates: list[NodeResult] = []
        for nid in dep_nodes:
            for src, _, data in self._g.in_edges(nid, data=True):
                if data.get("type") in ("documents", "references"):
                    src_type = self._g.nodes.get(src, {}).get("type", "")
                    if src_type == "file" and src not in {d.id for d in doc_updates}:
                        doc_updates.append(self._node_result(src))

        return MigrationResult(
            from_dep=from_dep,
            to_dep=to_dep,
            phases=phases,
            doc_updates=doc_updates,
            total_functions=len(dep_nodes),
        )

    # ------------------------------------------------------------------
    # Execution Flows / Process Detection
    # ------------------------------------------------------------------

    def processes(self, min_length: int = 3) -> list[ProcessResult]:
        """Detect named execution flows from entry points.

        An entry point is a function with no incoming CALLS edges (or only
        from test/demo code). Each flow follows the dominant call path
        (most-connected successor at each step) and is labeled by its
        module context and primary action.

        Returns flows sorted by length, with descriptive labels like:
        "SN Transport: main → solve_sn → transport_sweep → sweep_1d"
        """
        call_graph = nx.DiGraph()
        for src, tgt, data in self._g.edges(data=True):
            if data.get("type") == "calls":
                call_graph.add_edge(src, tgt)

        if not call_graph:
            return []

        # Entry points: in-degree 0 in call graph, excluding externals
        entry_points = []
        for n in call_graph.nodes:
            if call_graph.in_degree(n) == 0:
                ntype = self._g.nodes.get(n, {}).get("type", "")
                if ntype in ("function", "method"):
                    entry_points.append(n)

        results: list[ProcessResult] = []
        for entry in entry_points:
            chain = self._dominant_call_chain(call_graph, entry)
            if len(chain) < min_length:
                continue

            entry_node = self._node_result(entry)

            # Generate descriptive label from the chain
            label = self._label_process(chain)

            steps = []
            for i, node_id in enumerate(chain):
                calls_next = chain[i + 1] if i + 1 < len(chain) else ""
                steps.append(ProcessStep(
                    node=self._node_result(node_id),
                    step_number=i + 1,
                    calls_next=calls_next,
                ))

            results.append(ProcessResult(
                name=label,
                entry_point=entry_node,
                steps=steps,
                length=len(chain),
            ))

        results.sort(key=lambda p: p.length, reverse=True)
        return results

    @staticmethod
    def _dominant_call_chain(
        call_graph: nx.DiGraph, start: str,
    ) -> list[str]:
        """Follow the dominant path: at each step, pick the successor
        with the highest out-degree (most connections = most important)."""
        chain = [start]
        visited = {start}
        current = start
        while True:
            successors = [
                s for s in call_graph.successors(current)
                if s not in visited
            ]
            if not successors:
                break
            # Pick successor with highest out-degree
            best = max(successors, key=lambda s: call_graph.out_degree(s))
            chain.append(best)
            visited.add(best)
            current = best
            if len(chain) > 20:
                break
        return chain

    def _label_process(self, chain: list[str]) -> str:
        """Generate a human-readable label for a call chain."""
        # Extract module context from entry point
        entry_name = self._g.nodes.get(chain[0], {}).get("name", chain[0])
        parts = entry_name.split(".")

        # Find the most descriptive non-trivial function in the chain
        key_functions = []
        for node_id in chain[1:3]:  # look at first 2 callees
            name = self._g.nodes.get(node_id, {}).get("name", "")
            short = name.split(".")[-1] if name else ""
            if short and not short.startswith("_") and short not in ("main", "run"):
                key_functions.append(short)

        if key_functions:
            action = " → ".join(key_functions)
        else:
            action = parts[-1] if parts else "unknown"

        # Module context
        module = parts[0] if parts else "unknown"
        step_names = " → ".join(
            self._g.nodes.get(n, {}).get("name", n).split(".")[-1]
            for n in chain[:4]
        )
        if len(chain) > 4:
            step_names += " → ..."

        return f"{module}: {step_names}"

    # ------------------------------------------------------------------
    # Bridge Nodes / Surprising Connections
    # ------------------------------------------------------------------

    def bridges(self, top_n: int = 10) -> list[BridgeResult]:
        """Find bridge nodes connecting otherwise-separate communities.

        These are architectural hotspots — high betweenness centrality
        nodes that sit between communities. Changing them has outsized
        impact.
        """
        undirected = self._g.to_undirected()
        if undirected.number_of_nodes() == 0:
            return []

        # Compute betweenness centrality (approximate for large graphs)
        k = min(100, undirected.number_of_nodes())
        try:
            bc = nx.betweenness_centrality(undirected, k=k)
        except Exception:
            return []

        # Get community membership
        try:
            raw_communities = nx.community.greedy_modularity_communities(undirected)
        except Exception:
            return []

        node_to_community: dict[str, int] = {}
        for i, members in enumerate(raw_communities):
            for m in members:
                node_to_community[m] = i

        # Find nodes with high betweenness that connect multiple communities
        results: list[BridgeResult] = []
        for node_id, score in sorted(bc.items(), key=lambda x: -x[1]):
            if score < 0.001:
                continue
            # Which communities do this node's neighbors belong to?
            neighbor_communities = set()
            for nbr in undirected.neighbors(node_id):
                if nbr in node_to_community:
                    neighbor_communities.add(node_to_community[nbr])
            own = node_to_community.get(node_id)
            if own is not None:
                neighbor_communities.add(own)

            if len(neighbor_communities) >= 2:
                results.append(BridgeResult(
                    node=self._node_result(node_id),
                    communities_connected=sorted(neighbor_communities),
                    betweenness=round(score, 6),
                ))

            if len(results) >= top_n:
                break

        return results

    # ------------------------------------------------------------------
    # Graph Query (Cypher-like)
    # ------------------------------------------------------------------

    def graph_query(
        self,
        pattern: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Execute a structured graph query (mini query language).

        Pattern syntax:
            MATCH source_type -> edge_type -> target_type
            WHERE field=value
            RETURN fields

        Examples:
            "function -calls-> function"
                → all function-to-function call edges
            "file -contains-> equation"
                → all equations contained by doc pages
            "* -implements-> equation WHERE name=transport*"
                → code that implements transport equations
            "function -type_uses-> external WHERE name=numpy*"
                → functions using numpy types

        Wildcards: * matches any type/name. name=prefix* for prefix match.
        """
        parts = self._parse_pattern(pattern)
        if parts is None:
            return [{"error": f"Could not parse pattern: {pattern}"}]

        src_type, edge_type, tgt_type, where_field, where_value = parts
        results: list[dict[str, Any]] = []

        for src, tgt, data in self._g.edges(data=True):
            # Filter edge type
            if edge_type != "*" and data.get("type") != edge_type:
                continue
            # Filter source type
            src_attrs = self._g.nodes.get(src, {})
            if src_type != "*" and src_attrs.get("type") != src_type:
                continue
            # Filter target type
            tgt_attrs = self._g.nodes.get(tgt, {})
            if tgt_type != "*" and tgt_attrs.get("type") != tgt_type:
                continue
            # WHERE clause
            if where_field and where_value:
                # Check both source and target attrs
                src_val = str(src_attrs.get(where_field, ""))
                tgt_val = str(tgt_attrs.get(where_field, ""))
                if where_value.endswith("*"):
                    prefix = where_value[:-1].lower()
                    if not (src_val.lower().startswith(prefix) or tgt_val.lower().startswith(prefix)):
                        continue
                else:
                    if src_val.lower() != where_value.lower() and tgt_val.lower() != where_value.lower():
                        continue

            results.append({
                "source": {"id": src, "type": src_attrs.get("type", ""), "name": src_attrs.get("name", "")},
                "edge_type": data.get("type", ""),
                "target": {"id": tgt, "type": tgt_attrs.get("type", ""), "name": tgt_attrs.get("name", "")},
            })
            if len(results) >= limit:
                break

        return results

    @staticmethod
    def _parse_pattern(
        pattern: str,
    ) -> tuple[str, str, str, str, str] | None:
        """Parse a query pattern into components."""
        import re as _re

        where_field = ""
        where_value = ""

        # Split off WHERE clause
        if " WHERE " in pattern.upper():
            pattern, where_clause = _re.split(r"\s+WHERE\s+", pattern, maxsplit=1, flags=_re.IGNORECASE)
            if "=" in where_clause:
                where_field, where_value = where_clause.split("=", 1)
                where_field = where_field.strip()
                where_value = where_value.strip()

        # Parse: source_type -edge_type-> target_type
        m = _re.match(r"(\S+)\s+-(\S+)->\s+(\S+)", pattern.strip())
        if m:
            return m.group(1), m.group(2), m.group(3), where_field, where_value

        # Also accept: source_type -> target_type (any edge)
        m = _re.match(r"(\S+)\s+->\s+(\S+)", pattern.strip())
        if m:
            return m.group(1), "*", m.group(2), where_field, where_value

        return None
