"""Query interface over a KnowledgeGraph.

No Sphinx imports — usable standalone with a loaded JSON graph.
"""

from __future__ import annotations

import re
import subprocess
from collections import Counter
from dataclasses import dataclass, field, replace
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import networkx as nx

from sphinxcontrib.nexus.fingerprint import jaccard
from sphinxcontrib.nexus.graph import EdgeType, KnowledgeGraph, NodeType
from sphinxcontrib.nexus.position import PositionIndex
from sphinxcontrib.nexus.workspace import (
    PROVENANCE_KEY,
    GitProvenance,
    NoWorkspaceError,
    Workspace,
    changed_files,
    default_branch,
)

if TYPE_CHECKING:
    from sphinxcontrib.nexus.runtime import RuntimeRun


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
    file_path: str = ""
    """Source file of an AST-derived node — the reverse bridge: any
    query result can be fed straight to an editor, LSP request, or
    Read without a text-search round-trip. Empty for doc-domain
    nodes, which live in pages, not files."""
    lineno: int = 0
    """1-based definition line in ``file_path``; 0 when unknown."""


@dataclass
class EdgeResult:
    """An edge in query results."""

    source: str
    target: str
    type: str = ""
    key: str = ""
    evidence: str = ""
    """WHERE this edge came from — the difference between a fact and a
    guess, and until now the graph knew it and no reply said it.

    ``"inferred"`` means nobody declared this: it was minted because a
    code symbol's name shares a token with an equation label. `[M]` on
    ORPHEUS **14004 of 14004** ``implements`` edges are inferred — not
    one is declared — so "which code implements this equation?" has
    never once been answered from a declaration there, while
    ``tests`` edges are 2748 of 2748 declared (a real
    ``@pytest.mark.verifies``).

    Anything else names the declaring mechanism
    (``pytest.mark.verifies``, ``directive``, ``ast``). Empty means the
    producer recorded nothing."""

    via: list[str] = field(default_factory=list)
    """For an inferred edge, the shared tokens that produced the guess.

    The single most decision-relevant field on a weak edge: seeing that
    ``ScatteringOperator.kernel`` was matched to
    ``ld-ubld-divv-scale-free-kernel`` on the word ``kernel`` settles in
    one glance what a confidence number cannot."""


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
class NativePlaceResult:
    """A function that may belong inside a class.

    Feature-Envy / "native place" candidate: every considered (non-test)
    caller of ``function`` is a method of the single class ``target_class``.
    """

    function: NodeResult
    target_class: NodeResult
    caller_count: int
    cross_module: bool
    private: bool
    excluded_callers: int = 0
    likely_free_primitive: bool = field(init=False)
    """A *public* function tested at least as much as it is used in
    production (``excluded_callers >= caller_count``): an independently
    verified free-function primitive that is *correctly* free, not a
    relocation candidate. Derived, not set — these rows are kept but
    ranked last so the suppression is explicit, not implicit in the
    numbers. Private helpers never flag (a private symbol used by one
    class is a genuine relocation signal regardless of test coverage)."""

    def __post_init__(self) -> None:
        self.likely_free_primitive = (
            not self.private and self.excluded_callers >= self.caller_count
        )


@dataclass
class TwinPathResult:
    """Two functions that independently implement the same computation.

    Twin-path / Type-2-3 clone: the bodies of ``a`` and ``b`` share a high
    fraction of structural shingles (:attr:`similarity`) yet neither calls
    the other — the coding-elegance Pattern-2 smell. ``cross_module`` pairs
    (the two live in different modules) are the strongest signal; a duplicate
    that drifted across a module boundary is the classic single-source-of-
    truth violation.
    """

    a: NodeResult
    b: NodeResult
    similarity: float
    cross_module: bool


@dataclass
class DiscriminationResult:
    """A tag discriminated at multiple sites — a candidate missing type.

    ``tag`` (e.g. ``geometry``, ``inner_solver``) is branched on by
    ``site_count`` distinct functions via ``if``/``match`` over its
    ``cases``. The coding-elegance smell "a repeated conditional is a
    missing type — discriminate once, at the boundary": one dispatch (a
    type / single registry) should usually replace the repeated tag tests.
    """

    tag: str
    site_count: int
    cases: list[str]
    sites: list[NodeResult]


@dataclass
class DeadFunctionResult:
    """A function with no static callers — a dead-code candidate.

    This is a CANDIDATE, not a verdict: the static call graph cannot see
    dynamic dispatch (registry / ``getattr`` / callback passed to scipy),
    and public entry points are legitimately uncalled internally. The
    flags below carry that uncertainty so judgment, not the tool, decides.
    """

    function: NodeResult
    is_method: bool
    public: bool
    """No leading underscore — likely public API / an entry point, so
    being uncalled internally is expected. Private + uncalled is the
    stronger dead signal."""
    decorated: bool
    """Carries a decorator — often registry/route/property/dispatch
    machinery that invokes it indirectly (invisible to the call graph)."""

    unresolved_calls: int = 0
    """Calls naming this function that landed on an unresolved node.

    Non-zero means the zero-caller finding rests on a resolver the
    graph can see failing: somebody calls something by this name and it
    was not placed. `[M]` such a row is a false positive far more often
    than a real one — treat it as *"look here"*, never as *"delete"*."""


@dataclass
class ProtocolConformerResult:
    """A Protocol whose method-set is satisfied by classes that don't
    declare conformance.

    Python ``Protocol``s are satisfied *structurally*; the AST ``inherits``
    edge only records *explicit* subclassing, so a structural conformer has
    no edge to be missing. This matches by method NAME set (signatures
    ignored), so it is a heuristic — the authoritative check is a type
    checker (pyright / LSP ``goToImplementation``). The conformers are
    candidates to either declare conformance or confirm the Protocol is
    load-bearing.
    """

    protocol: NodeResult
    methods: list[str]
    conformers: list[NodeResult]


@dataclass
class HotspotResult:
    """A node ranked by a runtime metric — the dynamic stage DAG / hot path.

    ``cumtime`` (cumulative, incl. callees) surfaces the dominant *observed*
    chain; ``ncalls`` surfaces iteration counts and the recompute/caching
    smell (a property called tens of thousands of times per run); ``tottime``
    surfaces self-time hotspots.
    """

    node: NodeResult
    ncalls: int
    tottime: float
    cumtime: float


@dataclass
class RuntimeEdgeResult:
    """A runtime call edge overlaid on the static graph.

    ``in_static`` distinguishes the three overlay modes: ``dynamic_only``
    edges (``in_static`` False) are calls the static resolver could not see —
    annotation-mediated dispatch through ``self``/typed locals (issue #16) and
    the resolved face of polymorphism; ``fired`` edges (True) are static edges
    confirmed to execute, now carrying a call ``count``; ``dead`` edges
    (``count`` 0) are static edges among run-reachable nodes that never fired.
    """

    source: NodeResult
    target: NodeResult
    count: int
    in_static: bool
    accessor: bool = False
    """True when either endpoint is a property/trivial accessor — plumbing,
    not substantive dispatch. ``runtime_edges(substantive_only=True)`` drops
    these so the architecturally-interesting polymorphic dispatch (the #16
    payoff) is not buried under property-getter call edges."""


@dataclass
class TimelineEntry:
    """A node in the observed execution sequence (a viztracer run).

    ``first_ts`` is milliseconds from the start of the trace; ``depth`` the
    shallowest call-stack depth the node appeared at, so a small ``max_depth``
    filter yields the high-level stages (mesh → discretize → sweep → …).
    """

    node: NodeResult
    first_ts: float
    count: int
    depth: int


@dataclass
class MarkedTestResult:
    """A test node with the markers pytest RESOLVED for it.

    Different from what a decorator walk sees, and the difference is the
    whole point: module-level ``pytestmark``, class marks, and marks a
    ``conftest.py`` attaches during collection all land here and none of
    them appears in the test's own source text.

    ``pytest_ids`` makes the answer runnable — several of them when the
    test is parametrised.
    """

    node: NodeResult
    markers: dict[str, Any]
    pytest_ids: list[str]

    @property
    def invocation(self) -> str:
        """A copy-pasteable ``pytest`` command for this node.

        The one join that turns "which tests pin this" into something a
        caller can act on. Every consumer measured so far re-derived
        this transform by hand from ``file_path`` plus the dotted name.
        """
        return "pytest " + " ".join(f'"{i}"' for i in self.pytest_ids)


@dataclass
class BranchCoverageResult:
    """A node's branch coverage in a run — the missing-type signal.

    A node with ``branches_hit < branches_total`` did not exercise every
    conditional outcome in the canonical run. When that node also
    ``discriminates_on`` a tag (``discriminates`` non-empty), it is the
    strongest accidental-vs-essential / missing-type suspect: a discrimination
    always taken one way is a type the code is faking with a conditional.
    """

    node: NodeResult
    lines_hit: int
    lines_total: int
    branches_hit: int
    branches_total: int
    discriminates: list[str] = field(default_factory=list)


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


#: Authored relations between mathematical statements. Stored
#: specific → general, so following an edge forwards climbs toward the
#: continuous / exact / parent form.
STATEMENT_RELATIONS: frozenset[str] = frozenset({
    "discretizes", "derives_from", "approximates",
})

#: Node types that carry a mathematical statement — equations and the
#: sphinx-proof environments (definition, theorem, algorithm, …).
STATEMENT_TYPES: frozenset[str] = frozenset({"equation", "proof_object"})


@dataclass
class ProvenanceStep:
    """One step in a provenance chain."""

    node: NodeResult
    edge_type: str
    depth: int


@dataclass
class StatementRelation:
    """One authored link between two mathematical statements.

    Always read forwards — ``source`` *discretizes* / *derives_from* /
    *approximates* ``target`` — so no inverse vocabulary is needed and
    an edge appears once however it was reached.
    """

    source: NodeResult
    relation: str
    target: NodeResult


@dataclass
class ProvenanceResult:
    """Full citation → equation → code traceability chain."""

    target: str
    chain: list[ProvenanceStep]
    equations: list[NodeResult]
    citations: list[str]
    relations: list[StatementRelation] = field(default_factory=list)
    """Authored math-to-math structure reachable from this node — the
    spine a validator walks when asking what a test actually pins down.
    Empty unless the project declares ``discretizes`` / ``derives-from``
    / ``approximates`` relations."""


@dataclass
class TestReference:
    """A test that verifies some target, tagged with its provenance.

    ``source`` distinguishes tiers:

    - ``"declared"``     — a ``tests`` edge written from
                           ``@pytest.mark.verifies`` / registry /
                           directive. Confidence 1.0.
    - ``"heuristic-1hop"`` — inferred: ``is_test=True`` function
                             directly ``calls`` the implementing
                             code. Confidence 0.7.
    - ``"heuristic-multihop"`` — inferred: ``is_test=True`` function
                                 reaches the implementing code through
                                 a helper chain. Confidence 0.5.
    """

    id: str
    source: str = "declared"
    confidence: float = 1.0
    display_name: str = ""


@dataclass
class CoverageEntry:
    """Verification coverage status of one equation or function."""

    node: NodeResult
    status: str  # "verified", "tested", "implemented", "documented", "orphan_code"
    equation: NodeResult | None = None
    implementing_code: list[NodeResult] = field(default_factory=list)
    tests: list[TestReference] = field(default_factory=list)
    code_evidence: str = ""
    """How the code side was established: ``declared`` / ``inferred`` /
    ``mixed``, empty when there is no implementing code.

    ⚠ ``status`` does not say this, and the difference is large. An
    equation whose only ``implements`` edges were minted because a
    symbol's NAME shares a token with the label reads as
    ``implemented`` exactly like one a directive declared. `[M]` on
    ORPHEUS every single one is the first kind — 14004 of 14004 — so
    the whole code side of that matrix is currently ``inferred`` and
    the status column cannot say so.

    The test side has carried ``source``/``confidence`` on every
    :class:`TestReference` since it was written; this is the missing
    half."""


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
class DeadReferenceSite:
    """One place that still references a vanished target."""

    source: NodeResult
    edge_type: str
    reftype: str = ""


@dataclass
class DeadReference:
    """A referenced code symbol or equation label that no longer exists.

    The silent-drift shape: the target was deleted or renamed, the
    references outlived it, and Sphinx renders them as plain text
    without any warning.
    """

    target_id: str
    target_name: str
    kind: str  # "python" | "equation"
    site_count: int
    sites: list[DeadReferenceSite]
    #: Source files whose own code (an import, call, or annotation)
    #: minted this placeholder. Non-empty means the target is not simply
    #: absent — it exists as a name only because these files reference
    #: it, and a bare role elsewhere then bound to it. When they all sit
    #: in one unmaintained corner of the tree, that directory is the
    #: finding, not the reference. See ``minting_files``.
    minted_by: list[str] = field(default_factory=list)


@dataclass
class DeadReferencesResult:
    """Dead-reference audit over the whole graph."""

    dead: list[DeadReference]
    total_dead: int
    total_sites: int
    total_checked: int
    rescued: int
    undecidable: int
    project_modules: list[str]


@dataclass
class StalenessResult:
    """Doc-code drift analysis."""

    stale_docs: list[StalenessEntry]
    total_stale: int
    total_checked: int
    dead_references: list[DeadReference] = field(default_factory=list)
    total_dead_references: int = 0


@dataclass
class IdGrammarExample:
    """One representative node ID for a (domain, type) pair."""

    domain: str
    type: str
    id: str
    display_name: str


@dataclass
class IdGrammar:
    """Representative node IDs teaching the LLM the ID grammar."""

    description: str
    examples: list[IdGrammarExample]


@dataclass
class HotNode:
    """A node likely to be queried next in the current session."""

    id: str
    type: str
    degree: int
    reason: str


@dataclass
class HotNodes:
    """Recently-touched, high-degree nodes worth surfacing early."""

    description: str
    nodes: list[HotNode]


@dataclass
class PreloadHint:
    """A static ToolSearch invocation to preload common Nexus tools."""

    description: str
    tool_search_call: str


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
    #: Section → how much of it you are seeing, and which tool has the
    #: rest. The briefing shows examples, so without this a reader
    #: cannot tell five stale pages from five HUNDRED — an absence that
    #: does not name what it looked for (``lessons-L56``).
    showing: dict[str, str] = field(default_factory=dict)
    id_grammar: IdGrammar = field(
        default_factory=lambda: IdGrammar(description="", examples=[]),
    )
    hot_nodes: HotNodes = field(
        default_factory=lambda: HotNodes(description="", nodes=[]),
    )
    preload_hint: PreloadHint = field(
        default_factory=lambda: PreloadHint(description="", tool_search_call=""),
    )


@dataclass
class RetestResult:
    """Minimum set of tests to re-run.

    ``safe_to_skip`` is the complement: every collectable test that is
    not in ``must_retest`` or ``should_retest``. It is reported as a
    count rather than ~5000 node ids, so the two fields that make it
    AUDITABLE travel with it — ``dependence_edges`` says what counted as
    a dependency, and ``cone_depth`` how far the walk actually had to go
    (it runs to a fixed point, so this is a measurement, not a setting).
    """

    must_retest: list[NodeResult]
    should_retest: list[NodeResult]
    changed_symbols: list[str]
    total_tests: int
    safe_to_skip: int
    dependence_edges: list[str] = field(default_factory=list)
    cone_depth: int = 0


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


@dataclass
class UnresolvedCallers:
    """Calls that NAME this symbol but landed nowhere real.

    The resolver mints a node per receiver SPELLING (#55), so
    ``quad.foo()``, ``q.foo()`` and ``good.foo()`` become three
    different unresolved nodes and none of them is the real ``foo``.
    Those call edges exist — they are parked where the target cannot
    see them.

    This is what turns an empty answer from a claim into a lead. Not
    "possibly incomplete", but: `[M]` on ORPHEUS,
    ``Quadrature.ordinate_permutation`` has **0** resolved callers and
    **40** calls sitting on five same-named phantoms.
    """

    count: int
    """``calls`` edges arriving at same-named unresolved nodes."""

    spellings: list[str]
    """The unresolved node ids carrying them, most-called first."""

    note: str = ""
    """One line a reader can act on without knowing the internals."""


@dataclass
class CallersResult:
    """Callers (or callees) of a function."""

    target: str
    direction: str  # "callers" or "callees"
    nodes: list[NodeResult]
    total: int
    unresolved: UnresolvedCallers | None = None
    """Present only when the resolver may be blind here.

    ⚠ Its ABSENCE is not a completeness guarantee — it means this one
    mechanism found nothing. Annotation-mediated dispatch (#16) leaves
    no same-named phantom behind, so a call through
    ``self.scheme.step()`` is invisible to this check too."""


@dataclass
class AuditGap:
    """A single V&V gap with actionable context."""

    equation_id: str
    status: str
    theory_page: str
    implementing_code: list[str]
    nearest_tests: list[str]
    is_stale: bool


@dataclass
class VerificationAuditResult:
    """Complete V&V audit in a single call.

    ``grouped`` is populated when ``verification_audit`` was called
    with a ``group_by`` argument. It holds the same gaps as ``gaps``,
    keyed by the requested dimension (``"L0"``/``"L1"``/… for
    ``group_by="level"``; top-level Python module for
    ``group_by="module"``; the equation id itself for
    ``group_by="equation"``). When ``group_by`` is ``None``, this is
    an empty dict.
    """

    summary: dict[str, int]
    gaps: list[AuditGap]
    stale_pages: list[StalenessEntry]
    total_equations: int
    group_by: str | None = None
    grouped: dict[str, list[AuditGap]] = field(default_factory=dict)


@dataclass
class VerificationGap:
    """One gap reported by ``verification_gaps``."""

    kind: str  # "untagged_test" | "unverified_equation" | "missing_err_catcher"
    node_id: str
    display_name: str = ""
    module: str = ""
    level: str = ""
    detail: str = ""


@dataclass
class VerificationGapsResult:
    """Buckets returned by ``verification_gaps``."""

    untagged_tests: list[VerificationGap] = field(default_factory=list)
    unverified_equations: list[VerificationGap] = field(default_factory=list)
    missing_err_catchers: list[VerificationGap] = field(default_factory=list)
    filters: dict[str, str | int | None] = field(default_factory=dict)


#: A node body of at most this many lines, with no recognised accessor
#: decorator, is treated as a trivial getter by ``_is_accessor``. Kept tight
#: to avoid misclassifying a genuine short polymorphic dispatcher.
_ACCESSOR_MAX_SPAN = 2


def _module_of(graph: nx.MultiDiGraph, node_id: str) -> str:
    """Dotted module of a code node, i.e. its qualified name minus the leaf.

    ``orpheus.sn.axis.SNMesh.trace`` → ``orpheus.sn.axis.SNMesh``. Used to
    decide whether two nodes are cross-module; an empty/leaf name returns
    itself.
    """
    name = graph.nodes.get(node_id, {}).get("name", "")
    return name.rsplit(".", 1)[0] if "." in name else name


def _leaf_name(graph: nx.MultiDiGraph, node_id: str) -> str:
    """Final dotted component of a code node's qualified name.

    ``orpheus.sn.axis.SNMesh.trace`` → ``trace``. The leaf is what reveals
    privacy (leading ``_``), dunders, and method-name conformance.
    """
    name = graph.nodes.get(node_id, {}).get("name", "")
    return name.rsplit(".", 1)[-1] if name else node_id


def _is_dunder(leaf: str) -> bool:
    return leaf.startswith("__") and leaf.endswith("__")


class GraphQuery:
    """Query interface over a KnowledgeGraph or raw nx.MultiDiGraph.

    Designed to be usable standalone (no Sphinx dependency).

    A query answers about ONE checkout — the tree the graph was built
    from — so it carries the :class:`~sphinxcontrib.nexus.workspace.Workspace`
    it was loaded from rather than being told the working tree again on
    each call.  Every git-aware verb (``detect_changes``, ``retest``,
    ``staleness``, ``rename``, ``node_at``, …) then reads one root that
    cannot disagree with the graph it is paired with.  ``workspace`` is
    optional because a graph is queryable without a tree: the CLI reads
    a bare ``--db``, and most callers ask purely structural questions
    that no working tree could inform.
    """

    def __init__(
        self,
        graph: KnowledgeGraph | nx.MultiDiGraph,
        workspace: Workspace | None = None,
    ) -> None:
        self._kg = (
            graph if isinstance(graph, KnowledgeGraph) else KnowledgeGraph(graph)
        )
        self._g = self._kg.nxgraph
        self._workspace = workspace

    @property
    def knowledge_graph(self) -> KnowledgeGraph:
        """The :class:`KnowledgeGraph` this query reads — including its
        ``metadata`` (provenance stamp, schema version). Mutating
        consumers (ingest) operate on this object rather than
        reconstructing a wrapper around the bare NetworkX graph."""
        return self._kg

    @property
    def workspace(self) -> Workspace | None:
        """The checkout-and-database pair this query answers about, or
        ``None`` when the graph was opened without one."""
        return self._workspace

    @property
    def project_root(self) -> Path | None:
        """Working tree this query answers about; ``None`` when
        unknown.  Verbs that merely *degrade* without a tree (staleness'
        timestamp signal, ``rename``'s regex sweep) read this and skip
        that half; verbs that are meaningless without one
        (``detect_changes``, ``retest`` — both are git diffs) call
        :meth:`_require_root` instead."""
        return self._workspace.root if self._workspace is not None else None

    def _require_root(self, verb: str) -> Path:
        """The working tree, or a refusal that names what is missing.

        A git diff against no tree has no meaningful answer, and the
        older signature made that state expressible by taking the root
        per call — so the failure surfaced as an obscure subprocess
        error at the bottom of the stack, or as an empty diff that read
        like "nothing changed".
        """
        root = self.project_root
        if root is None:
            raise NoWorkspaceError(
                f"{verb} diffs a working tree against git, and this graph "
                f"was opened without one — a server started with a bare "
                f"--db, or a query built with no Workspace root. Point "
                f"nexus at the checkout the graph was built from."
            )
        return root

    @cached_property
    def settings(self) -> Any:
        """This checkout's ``.nexus/config.toml``, loaded on first use.

        A :func:`~functools.cached_property` for the same reason as
        :attr:`positions`: the config is a pure function of the workspace
        root, and a query is a pure function of the same, so the object
        IS the cache key. A rootless query — a bare ``--db`` server —
        gets an empty config, whose ``tunable`` calls all return the
        shipped defaults.
        """
        from sphinxcontrib.nexus.project import ProjectConfig

        return ProjectConfig.load(self.project_root or Path.cwd())

    def tunable(self, dotted: str) -> Any:
        """A ``[table].key`` setting from this checkout, or its default."""
        return self.settings.tunable(dotted)

    @cached_property
    def ontology(self) -> Any:
        """The vocabulary in force here — base plus this project's own."""
        from sphinxcontrib.nexus.ontology import Ontology

        return Ontology.load(self.project_root)

    @cached_property
    def _undefined_types(self) -> frozenset[str]:
        """Placeholders PLUS the untyped node — "no definition found".

        ⚠ Deliberately not :attr:`placeholder_types`. ``""`` means two
        opposite things depending on where you read it: on a stored node
        it is a node with no type at all, and in a COMPACTED result dict
        it means the type equalled the id segment and was dropped as
        redundant — i.e. a perfectly concrete node. Folding the two cost
        a green suite exactly once: result ranking started sorting every
        project symbol below the builtins it exists to demote.
        """
        return self.placeholder_types | {""}

    @cached_property
    def placeholder_types(self) -> frozenset[str]:
        """Types meaning "no concrete definition found", per the ontology.

        A project that declares its own placeholder kind is honoured by
        every consumer at once — ranking, ``god_nodes``,
        ``dead_references``' phantom set — instead of by whichever call
        sites remembered to name it.
        """
        return self.ontology.placeholder_types

    @cached_property
    def positions(self) -> PositionIndex:
        """This graph's ``(file, line)`` → node index, built on first use.

        A :func:`~functools.cached_property` because the index is a pure
        function of (graph, root) and a ``GraphQuery`` is a pure function
        of the same two — so the object IS the cache key, and a reloaded
        graph gets a new query and therefore a new index without anyone
        having to remember to invalidate anything.
        """
        return PositionIndex(self._kg, self.project_root)

    @cached_property
    def build_commit(self) -> str | None:
        """Commit the graph's provenance stamp records; ``None`` when
        the graph is unstamped (built by nexus < 0.12) or was built
        from a non-git tree."""
        prov = GitProvenance.from_stamp(self._kg.metadata.get(PROVENANCE_KEY))
        return prov.commit if prov is not None else None

    @cached_property
    def files_changed_since_build(self) -> frozenset[Path] | None:
        """Working-tree files that differ from :attr:`build_commit`.

        ``None`` means UNKNOWN — no tree, no stamp, or git failed —
        and must not be collapsed into ``frozenset()`` ("verified
        unchanged"): a graph built at a commit this clone no longer has
        is *more* suspect, not less.

        Cached for the lifetime of the query, which needs no
        invalidation key because the object IS the key: the graph is
        immutable once loaded, and every reload or workspace switch
        builds a new :class:`GraphQuery`.  The server previously kept
        this beside the graph as a module global with a hand-built
        ``(root, mtime, commit)`` cache key — three values whose only
        job was to detect that the query had been replaced.
        """
        root = self.project_root
        commit = self.build_commit
        if root is None or commit is None:
            return None
        return changed_files(root, commit)

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
            file_path=attrs.get("file_path", ""),
            lineno=attrs.get("lineno") or 0,
        )

    def _edge_result(
        self, source: str, target: str, key: str | int, data: dict,
    ) -> EdgeResult:
        """Build an EdgeResult from edge data.

        ⚠ The edge's PROVENANCE is stored under the attribute
        ``source``, which on this dataclass already means the source
        NODE. It surfaces as ``evidence`` — the same fact under a name
        that does not collide.
        """
        return EdgeResult(
            source=source,
            target=target,
            type=data.get("type", ""),
            key=str(key),
            evidence=data.get("source", ""),
            via=list(data.get("shared_tokens") or ()),
        )

    def get_node(self, node_id: str) -> NodeResult | None:
        """Get a single node by ID."""
        if node_id not in self._g:
            return None
        return self._node_result(node_id)

    def node_at(
        self,
        file_path: Path | str,
        line: int,
    ) -> NodeResult | None:
        """The graph node enclosing a file position.

        The bridge from position-speaking tools (language servers,
        stack traces, editors) into the graph: a position maps to the
        INNERMOST function / method / class whose extent contains it.  A
        position in module scope (imports, constants, between defs) maps
        to the module node.  ``None`` when the file is not in the graph
        at all.

        The search itself is
        :meth:`~sphinxcontrib.nexus.position.PositionIndex.enclosing` —
        shared with the runtime backends' trace join, which asks the
        neighbouring question over the same extents.  Keeping the two
        searches separate is what let them answer differently.

        Args:
            file_path: File of interest; relative paths resolve
                against the workspace root.
            line: 1-based line number, as editors and LSP report it.
        """
        node_id = self.positions.enclosing(file_path, line)
        return self._node_result(node_id) if node_id else None

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

    @cached_property
    def _unresolved_calls_by_leaf(self) -> dict[str, list[tuple[int, str]]]:
        """leaf name → [(call count, unresolved node id)], most-called first.

        Built once per query, because ``dead_functions`` asks this of
        every candidate and a per-call scan of 22 k nodes would make an
        O(N·M) sweep out of an O(N) question. The object is its own
        cache key: the graph is immutable once loaded, and every reload
        or workspace switch builds a new :class:`GraphQuery`.
        """
        index: dict[str, list[tuple[int, str]]] = {}
        placeholders = self.placeholder_types
        for node, attrs in self._g.nodes(data=True):
            if attrs.get("type") not in placeholders:
                continue
            n = sum(
                1 for _, _, d in self._g.in_edges(node, data=True)
                if d.get("type") == "calls"
            )
            if n:
                index.setdefault(node.rsplit(".", 1)[-1], []).append((n, node))
        for entries in index.values():
            entries.sort(reverse=True)
        return index

    def unresolved_callers(self, node_id: str) -> UnresolvedCallers | None:
        """Calls naming this symbol that the resolver could not place.

        ⚠ An empty caller list is the graph's most dangerous answer,
        because it reads as a licence to delete and there is no way to
        tell it from *"the resolver is blind here"*. `[M]` on ORPHEUS,
        17.2 % of all ``calls`` edges terminate on unresolved nodes, and
        ``Quadrature.ordinate_permutation`` — which has real production
        callers — reports **0** while **40** of its calls sit on five
        phantoms named for the CALLER's variable
        (``quad.``, ``q.``, ``good.``, ``broken.``, ``quadrature.``).

        Matching on the leaf name is deliberately loose: a same-named
        method on an unrelated class will match too. That is the right
        trade — this exists to stop a confident zero, and a lead worth
        checking beats silence that reads as proof. The reply says
        "may be", never "is".
        """
        leaf = node_id.rsplit(".", 1)[-1]
        if not leaf or ":" in leaf:          # a module or bare-name node
            return None
        found = self._unresolved_calls_by_leaf.get(leaf)
        if not found:
            return None
        total = sum(n for n, _ in found)
        return UnresolvedCallers(
            count=total,
            spellings=[c for _, c in found],
            note=(
                f"{total} call(s) to a receiver spelled {leaf!r} did not "
                f"resolve and may belong here — the resolver mints one node "
                f"per receiver spelling. Confirm with grep before treating "
                f"an empty caller list as 'uncalled'."
            ),
        )

    def callers(
        self,
        node_id: str,
        transitive: bool = False,
        max_depth: int = 3,
    ) -> CallersResult:
        """Functions that call this symbol, optionally transitive.

        Returns a deduplicated list of caller nodes. If transitive=True,
        walks the call graph up to max_depth.

        Carries :attr:`CallersResult.unresolved` when calls naming this
        symbol landed on unresolved nodes, so an empty list cannot be
        read as "nothing calls this".
        """
        if node_id not in self._g:
            return CallersResult(target=node_id, direction="callers", nodes=[], total=0)

        if not transitive:
            seen: set[str] = set()
            nodes: list[NodeResult] = []
            for src, _tgt, _key, data in self._g.in_edges(node_id, keys=True, data=True):
                if data.get("type") == "calls" and src not in seen:
                    seen.add(src)
                    nodes.append(self._node_result(src))
            return CallersResult(
                target=node_id, direction="callers", nodes=nodes, total=len(nodes),
                unresolved=self.unresolved_callers(node_id),
            )

        # Transitive: BFS on calls edges only
        def edge_filter(u: str, v: str, k: str | int) -> bool:
            return self._g.edges[u, v, k].get("type") == "calls"
        view = nx.subgraph_view(self._g, filter_edge=edge_filter)

        all_nodes: list[NodeResult] = []
        for depth, layer in enumerate(nx.bfs_layers(view.reverse(copy=False), [node_id])):
            if depth == 0:
                continue
            if depth > max_depth:
                break
            all_nodes.extend(self._node_result(n) for n in layer)

        return CallersResult(
            target=node_id, direction="callers", nodes=all_nodes,
            unresolved=self.unresolved_callers(node_id), total=len(all_nodes),
        )

    def callees(
        self,
        node_id: str,
        transitive: bool = False,
        max_depth: int = 3,
    ) -> CallersResult:
        """Functions that this symbol calls, optionally transitive."""
        if node_id not in self._g:
            return CallersResult(target=node_id, direction="callees", nodes=[], total=0)

        if not transitive:
            seen: set[str] = set()
            nodes: list[NodeResult] = []
            for _src, tgt, _key, data in self._g.out_edges(node_id, keys=True, data=True):
                if data.get("type") == "calls" and tgt not in seen:
                    seen.add(tgt)
                    nodes.append(self._node_result(tgt))
            return CallersResult(
                target=node_id, direction="callees", nodes=nodes, total=len(nodes),
            )

        def edge_filter(u: str, v: str, k: str | int) -> bool:
            return self._g.edges[u, v, k].get("type") == "calls"
        view = nx.subgraph_view(self._g, filter_edge=edge_filter)

        all_nodes: list[NodeResult] = []
        for depth, layer in enumerate(nx.bfs_layers(view, [node_id])):
            if depth == 0:
                continue
            if depth > max_depth:
                break
            all_nodes.extend(self._node_result(n) for n in layer)

        return CallersResult(
            target=node_id, direction="callees", nodes=all_nodes, total=len(all_nodes),
        )

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

        ``max_depth=-1`` walks to the FIXED POINT — the same uncapped
        sentinel ``runtime_timeline`` uses. The traversal terminates on
        its own (``bfs_layers`` visits each node once); ``max_depth``
        only stops it early. Whether the fixed point is a useful answer
        depends entirely on ``edge_types``: over the dependence
        relations it converges in a handful of layers, while including
        ``references`` reaches `[M]` 78 % of ORPHEUS's tests from any
        starting symbol — everything mentions everything eventually.
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
            if max_depth >= 0 and depth > max_depth:
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

    def god_nodes(
        self, top_n: int = 10, include_placeholders: bool = False,
    ) -> list[NodeResult]:
        """The project's most connected symbols — its structural hubs.

        Placeholders are excluded by default, and the default is the
        whole point of the verb. Degree over the raw graph ranks
        ``numpy.array``, ``float``, ``int`` and ``numpy.ndarray`` above
        almost everything a project contains — `[M]` 2026-08-16 on
        ORPHEUS, **9 of the top 10** were stdlib or installed-package
        nodes. That answers "what does Python have", which nobody asked.

        Pass ``include_placeholders=True`` for the raw ranking; it is a
        real question ("what does this project lean on hardest?"), just
        a different one.
        """
        ranked = sorted(self._g.degree(), key=lambda x: x[1], reverse=True)
        out: list[NodeResult] = []
        for nid, _degree in ranked:
            if not include_placeholders:
                if self._g.nodes[nid].get("type") in self.placeholder_types:
                    continue
            out.append(self._node_result(nid))
            if len(out) >= top_n:
                break
        return out

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
        scope: str = "staged",
    ) -> DetectChangesResult:
        """Detect which symbols changed in git and their impact.

        Diffs the workspace's own checkout — the tree the graph was
        built from — so the changed files and the node positions they
        are matched against always speak about the same tree.

        Args:
            scope: "staged" (git diff --cached), "unstaged" (git diff),
                   "all" (both), or "branch" (diff against the
                   merge-base with the repository's default branch).
        """
        project_root = self._require_root("detect_changes")
        # Not ``changed_files`` — that name belongs to the module-level
        # git helper this file imports, and shadowing it here would
        # leave the two spellings one edit apart.
        change_type_by_path = self._git_changed_files(project_root, scope)

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
            if rel in change_type_by_path:
                changed_symbols.append(ChangeEntry(
                    node=self._node_result(node_id),
                    change_type=change_type_by_path[rel],
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
            # Three-dot diff = changes since the merge-base with the
            # repository's default branch (origin/HEAD when set, else
            # main/master). The old fallback chain conflated "ref does
            # not exist" with "no .py files changed" and never saw
            # unconventionally named defaults.
            base = default_branch(project_root)
            if base is not None:
                _run_diff(["diff", f"{base}...HEAD", "--name-status"])

        return files

    # ------------------------------------------------------------------
    # Safe rename
    # ------------------------------------------------------------------

    def rename(
        self,
        old_name: str,
        new_name: str,
        dry_run: bool = True,
    ) -> RenameResult:
        """Analyze or execute a safe rename across the codebase.

        Finds all references via the graph (high confidence) and
        via regex search in source files (medium confidence).  The
        regex sweep and the edit application both run over the
        workspace's checkout; without one, only the graph half runs.

        Args:
            old_name: Current symbol name (e.g., "solve_sn" or "SNSolver").
            new_name: New name to rename to.
            dry_run: If True, return edits without applying. If False, apply.
        """
        project_root = self.project_root
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
            self._apply_renames(edits, project_root)

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

    def _statement_relations(
        self,
        roots: list[str],
        max_depth: int = 4,
    ) -> list[StatementRelation]:
        """Collect authored math-to-math edges around statement nodes.

        Walks both directions: outgoing edges climb toward the general
        form — a discrete equation points at the continuous one it
        discretizes — and incoming edges descend toward the specific.
        The question this exists to answer ("what does this test
        actually pin down?") needs the whole spine, not one hop of it.

        Deduplication is by **edge**, not by node, because the roots are
        typically every statement on one page: a node-visited set would
        suppress exactly the links between them, which are the ones
        worth having. ``max_depth`` bounds hops away from the roots.
        """
        relations: list[StatementRelation] = []
        seen_edges: set[tuple[str, str, str]] = set()
        visited: set[str] = set()
        frontier: list[tuple[str, int]] = [(r, 0) for r in roots]

        while frontier:
            current, depth = frontier.pop(0)
            if current in visited or depth >= max_depth:
                continue
            visited.add(current)

            incident = list(self._g.out_edges(current, data=True))
            incident += list(self._g.in_edges(current, data=True))
            for src, tgt, data in incident:
                relation = data.get("type", "")
                if relation not in STATEMENT_RELATIONS:
                    continue
                key = (src, relation, tgt)
                if key in seen_edges:
                    continue
                seen_edges.add(key)
                relations.append(StatementRelation(
                    source=self._node_result(src),
                    relation=relation,
                    target=self._node_result(tgt),
                ))
                frontier.append((tgt if src == current else src, depth + 1))
        return relations

    def provenance_chain(self, node_id: str) -> ProvenanceResult:
        """Trace citation → equation → code for a symbol.

        Given a code symbol, find which doc pages reference it, what
        equations those pages contain, and what citations they use.
        Given an equation or proof environment, find the code documented
        on the same page.

        Traverses: code ←DOCUMENTS– doc –CONTAINS→ equations
                                        –CITES→ citations

        Every statement reached is then used as a root for
        :meth:`_statement_relations`, so any authored ``discretizes`` /
        ``derives-from`` / ``approximates`` structure comes back in
        ``relations``.
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

        elif node.type in STATEMENT_TYPES:
            seen_eqs.add(node_id)
            if node.type == "equation":
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

        # From doc pages, collect statements and citations
        statements: list[str] = (
            [node_id] if node.type in STATEMENT_TYPES else []
        )
        for doc_id in doc_pages:
            for _, tgt, data in self._g.out_edges(doc_id, data=True):
                tgt_type = self._g.nodes.get(tgt, {}).get("type", "")
                edge_type = data.get("type", "")

                if tgt_type in STATEMENT_TYPES and tgt not in seen_eqs:
                    seen_eqs.add(tgt)
                    statements.append(tgt)
                    stmt_node = self._node_result(tgt)
                    # ``equations`` is the historical field name and its
                    # consumers expect equations; proof environments ride
                    # in the chain and relation walk instead.
                    if tgt_type == "equation":
                        equations.append(stmt_node)
                    chain.append(ProvenanceStep(
                        node=stmt_node,
                        edge_type=(
                            "equation_on_page" if tgt_type == "equation"
                            else "statement_on_page"
                        ),
                        depth=2,
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

        relations = self._statement_relations(statements)

        return ProvenanceResult(
            target=node_id,
            chain=chain,
            equations=equations,
            citations=list(set(citations)),
            relations=relations,
        )

    # ------------------------------------------------------------------
    # Feature 2: Verification Coverage Map
    # ------------------------------------------------------------------

    def verification_coverage(
        self, status_filter: str | None = None,
    ) -> CoverageResult:
        """Map verification coverage: equation → code → test chain.

        Test evidence is collected in three tiers:

        1. **Declared** — a ``tests`` edge written by
           ``@pytest.mark.verifies`` / registry / directive. Any
           equation with a declared test is ``verified`` regardless
           of whether intermediate implementing code is tracked.
        2. **Heuristic 1-hop** — an ``is_test=True`` function directly
           ``calls`` the implementing code. Only consulted when no
           declared tests exist for the equation.
        3. **Heuristic multi-hop** — BFS up the ``calls`` graph from
           each implementing code node (max depth 3) finds an
           ancestor marked ``is_test=True``. Consulted after the
           1-hop tier, deduped against it.

        Status values:

        - ``"verified"``    — equation has at least one test (any tier).
        - ``"implemented"`` — equation has implementing code, no test.
        - ``"documented"``  — equation only, no implementing code.
        - ``"tested"``      — code with test, no equation link (orphan code).
        - ``"orphan_code"`` — code with no equation and no test.
        """
        code_types = {"function", "method", "class"}
        entries: list[CoverageEntry] = []
        summary: Counter[str] = Counter()

        # Index 1: equation → implementing code via IMPLEMENTS edges.
        eq_to_code: dict[str, list[str]] = {}
        code_to_eq: dict[str, list[str]] = {}
        # …and which of those links nobody declared. The V&V matrix's
        # code side had no evidence axis at all, while its TEST side has
        # carried `source`/`confidence` since it was written — so a row
        # implemented by a shared-word guess read exactly like one
        # implemented by a declaration. `[M]` on ORPHEUS that is not an
        # edge case: 14004 of 14004 `implements` edges are inferred.
        inferred_links: set[tuple[str, str]] = set()
        # Index 2: equation → declared tests via TESTS edges.
        declared_tests: dict[str, list[TestReference]] = {}
        # Index 3: code → direct test callers (1-hop heuristic).
        code_to_1hop_tests: dict[str, list[str]] = {}

        # Query-time dedup: multiple explicit edges can link the same
        # ``(source, target)`` pair — e.g. a registry entry that was
        # written before write-time dedup was hardened, or a graph
        # loaded from an older nexus version. Track seen pairs per
        # edge-type so each (code, equation) or (test, equation)
        # relationship contributes at most one entry to the coverage
        # result.
        _implements_seen: set[tuple[str, str]] = set()
        _declared_seen: set[tuple[str, str]] = set()

        for src, tgt, data in self._g.edges(data=True):
            etype = data.get("type", "")
            if etype == "implements":
                if (src, tgt) in _implements_seen:
                    continue
                _implements_seen.add((src, tgt))
                eq_to_code.setdefault(tgt, []).append(src)
                code_to_eq.setdefault(src, []).append(tgt)
                if data.get("source") == "inferred":
                    inferred_links.add((src, tgt))
            elif etype == "tests":
                if (src, tgt) in _declared_seen:
                    continue
                _declared_seen.add((src, tgt))
                declared_tests.setdefault(tgt, []).append(
                    TestReference(
                        id=src,
                        source="declared",
                        confidence=float(data.get("confidence", 1.0)),
                        display_name=self._g.nodes.get(src, {}).get(
                            "display_name", ""
                        ),
                    )
                )
            elif etype == "calls":
                if self._g.nodes.get(src, {}).get("is_test"):
                    code_to_1hop_tests.setdefault(tgt, []).append(src)

        def _multihop_tests(code_node: str, max_depth: int = 3) -> list[str]:
            """BFS up the ``calls`` graph from ``code_node`` until an
            ``is_test=True`` predecessor is found, bounded to
            ``max_depth`` hops. Returns the collected test node ids."""
            seen: set[str] = {code_node}
            frontier: set[str] = {code_node}
            found: list[str] = []
            for _ in range(max_depth):
                if not frontier:
                    break
                next_frontier: set[str] = set()
                for node in frontier:
                    for pred in self._g.predecessors(node):
                        if pred in seen:
                            continue
                        ed = self._g.get_edge_data(pred, node) or {}
                        if not any(
                            d.get("type") == "calls" for d in ed.values()
                        ):
                            continue
                        seen.add(pred)
                        if self._g.nodes.get(pred, {}).get("is_test"):
                            found.append(pred)
                        else:
                            next_frontier.add(pred)
                frontier = next_frontier
            return found

        # Classify equations
        for node_id, attrs in self._g.nodes(data=True):
            if attrs.get("type") != "equation":
                continue
            implementing = eq_to_code.get(node_id, [])
            tests: list[TestReference] = list(declared_tests.get(node_id, []))
            seen_test_ids: set[str] = {t.id for t in tests}

            if not tests:
                # No declared tests — fall back to heuristics. 1-hop
                # first, then multi-hop, deduped by id.
                for c in implementing:
                    for t in code_to_1hop_tests.get(c, []):
                        if t in seen_test_ids:
                            continue
                        seen_test_ids.add(t)
                        tests.append(TestReference(
                            id=t,
                            source="heuristic-1hop",
                            confidence=0.7,
                            display_name=self._g.nodes.get(t, {}).get(
                                "display_name", ""
                            ),
                        ))
                for c in implementing:
                    for t in _multihop_tests(c):
                        if t in seen_test_ids:
                            continue
                        seen_test_ids.add(t)
                        tests.append(TestReference(
                            id=t,
                            source="heuristic-multihop",
                            confidence=0.5,
                            display_name=self._g.nodes.get(t, {}).get(
                                "display_name", ""
                            ),
                        ))

            has_code = len(implementing) > 0
            has_test = len(tests) > 0
            if has_test:
                status = "verified"
            elif has_code:
                status = "implemented"
            else:
                status = "documented"

            if status_filter and status != status_filter:
                continue

            declared_n = sum(
                1 for c in implementing if (c, node_id) not in inferred_links
            )
            entries.append(CoverageEntry(
                node=self._node_result(node_id),
                status=status,
                implementing_code=[self._node_result(c) for c in implementing],
                tests=tests,
                code_evidence=(
                    "" if not implementing
                    else "declared" if declared_n == len(implementing)
                    else "inferred" if declared_n == 0
                    else "mixed"
                ),
            ))
            summary[status] += 1

        # Classify orphan / tested code symbols that don't link to any equation.
        if status_filter in (None, "tested", "orphan_code"):
            for node_id, attrs in self._g.nodes(data=True):
                if attrs.get("type") not in code_types:
                    continue
                if node_id in code_to_eq:
                    continue  # already covered via equation
                direct_tests = code_to_1hop_tests.get(node_id, [])
                has_test = bool(direct_tests)
                status = "tested" if has_test else "orphan_code"
                if status_filter and status != status_filter:
                    continue
                entries.append(CoverageEntry(
                    node=self._node_result(node_id),
                    status=status,
                    tests=[
                        TestReference(
                            id=t,
                            source="heuristic-1hop",
                            confidence=0.7,
                            display_name=self._g.nodes.get(t, {}).get(
                                "display_name", ""
                            ),
                        )
                        for t in direct_tests
                    ],
                ))
                summary[status] += 1

        return CoverageResult(entries=entries, summary=dict(summary))

    def verification_audit(
        self,
        *,
        group_by: str | None = None,
        include_tests: bool = False,
    ) -> VerificationAuditResult:
        """Complete V&V audit with optional grouping.

        Combines ``verification_coverage`` and ``staleness`` into one
        actionable report with gaps prioritized by closure difficulty
        (``implemented`` first, then ``documented``).

        Args:
            group_by: Optional grouping dimension. ``"level"`` buckets
                gaps by the ``vv_level`` of their nearest test (or
                ``"unassigned"`` when no level is known). ``"module"``
                buckets by the top-level Python package of the nearest
                implementing code node (or ``"unassigned"``).
                ``"equation"`` keys by ``equation_id`` directly — useful
                for cross-referencing audit output against the doc
                graph. ``None`` (default) keeps the flat ``gaps`` list.
            include_tests: When True, the audit also populates the
                ``summary["tests_declared"]`` / ``summary["tests_inferred"]``
                counts so consumers can judge how much of the
                verification claim is load-bearing declarative evidence.
        """
        coverage = self.verification_coverage()
        stale = self.staleness()

        # Build stale page set for cross-referencing
        stale_docnames = {e.doc_node.docname for e in stale.stale_docs}

        # Build gaps: every equation that is NOT verified
        gaps: list[AuditGap] = []
        for entry in coverage.entries:
            if entry.status == "verified":
                continue
            if entry.status in ("tested", "orphan_code"):
                continue  # these are code-level, not equation-level

            # Find the theory page this equation lives on
            theory_page = entry.node.docname or ""

            gaps.append(AuditGap(
                equation_id=entry.node.id,
                status=entry.status,
                theory_page=theory_page,
                implementing_code=[c.id for c in entry.implementing_code],
                nearest_tests=[t.id for t in entry.tests],
                is_stale=theory_page in stale_docnames,
            ))

        # Sort: implemented first (easiest to close), then documented
        priority = {"implemented": 0, "documented": 1}
        gaps.sort(key=lambda g: priority.get(g.status, 99))

        summary = dict(coverage.summary)
        if include_tests:
            declared = inferred = 0
            for entry in coverage.entries:
                for t in entry.tests:
                    if t.source == "declared":
                        declared += 1
                    else:
                        inferred += 1
            summary["tests_declared"] = declared
            summary["tests_inferred"] = inferred

        grouped: dict[str, list[AuditGap]] = {}
        if group_by == "level":
            for gap in gaps:
                level = self._level_for_gap(gap)
                grouped.setdefault(level, []).append(gap)
        elif group_by == "module":
            for gap in gaps:
                module = self._module_for_gap(gap)
                grouped.setdefault(module, []).append(gap)
        elif group_by == "equation":
            for gap in gaps:
                grouped.setdefault(gap.equation_id, []).append(gap)
        elif group_by is not None:
            raise ValueError(
                f"group_by must be one of 'level', 'module', 'equation', "
                f"or None — got {group_by!r}"
            )

        return VerificationAuditResult(
            summary=summary,
            gaps=gaps,
            stale_pages=stale.stale_docs,
            total_equations=sum(
                1 for _, a in self._g.nodes(data=True) if a.get("type") == "equation"
            ),
            group_by=group_by,
            grouped=grouped,
        )

    def _level_for_gap(self, gap: AuditGap) -> str:
        """Derive the V&V level of an audit gap by looking at its
        nearest test nodes' ``vv_level`` metadata. Returns
        ``"unassigned"`` when no test is known or none carries a level.
        """
        for test_id in gap.nearest_tests:
            lvl = self._g.nodes.get(test_id, {}).get("vv_level")
            if lvl:
                return str(lvl)
        return "unassigned"

    def _module_for_gap(self, gap: AuditGap) -> str:
        """Derive the top-level Python module of an audit gap by
        taking the first dotted prefix of its nearest implementing
        code node, or falling back to the nearest test. Returns
        ``"unassigned"`` when neither resolves."""
        for code_id in gap.implementing_code:
            name = self._g.nodes.get(code_id, {}).get("name", "")
            if name and "." in name:
                return name.split(".", 1)[0]
            if name:
                return name
        for test_id in gap.nearest_tests:
            name = self._g.nodes.get(test_id, {}).get("name", "")
            if name and "." in name:
                return name.split(".", 1)[0]
        return "unassigned"

    def verification_gaps(
        self,
        module: str | None = None,
        level: str | None = None,
        error_catalog: set[str] | None = None,
    ) -> VerificationGapsResult:
        """Surface per-bucket V&V gaps, optionally filtered.

        Returns three lists:

        - ``untagged_tests``: test nodes (``is_test=True``) that carry
          no ``vv_level`` metadata. These are the ones that need a
          ``@pytest.mark.lN`` before they can be audited.
        - ``unverified_equations``: equation nodes with no incoming
          ``tests`` edge from any tier (declared, 1-hop, multi-hop).
          Equivalent to the ``documented``/``implemented`` bucket of
          ``verification_audit``.
        - ``missing_err_catchers``: when ``error_catalog`` is supplied,
          any ``ERR-NNN`` / ``FM-NN`` tag in the catalog that no test's
          ``catches`` metadata mentions.

        Filters:

        - ``module`` — keeps only nodes whose name starts with this
          top-level Python package prefix.
        - ``level`` — keeps only entries whose nearest test has this
          ``vv_level``. Applied to ``unverified_equations`` by looking
          at the strongest-tier test it does have (if any); applied to
          ``untagged_tests`` is a no-op (they have no level to filter
          on, by definition).
        """

        def _matches_module(node_id: str) -> bool:
            if module is None:
                return True
            name = self._g.nodes.get(node_id, {}).get("name", "")
            if not name:
                return False
            top = name.split(".", 1)[0]
            return top == module

        # ---- untagged_tests --------------------------------------------
        untagged: list[VerificationGap] = []
        for node_id, attrs in self._g.nodes(data=True):
            if not attrs.get("is_test"):
                continue
            if attrs.get("vv_level"):
                continue
            if not _matches_module(node_id):
                continue
            name = attrs.get("name", "")
            untagged.append(VerificationGap(
                kind="untagged_test",
                node_id=node_id,
                display_name=attrs.get("display_name", "") or name,
                module=name.split(".", 1)[0] if "." in name else name,
                level="",
                detail="no @pytest.mark.l[0-3] marker",
            ))

        # ---- unverified_equations --------------------------------------
        unverified: list[VerificationGap] = []
        coverage = self.verification_coverage()
        for entry in coverage.entries:
            if entry.status not in ("implemented", "documented"):
                continue
            if not _matches_module(entry.node.id):
                # The equation's own module is "math", so the module
                # filter is instead applied to its nearest
                # implementing code — fall back to that.
                implementing_ids = [c.id for c in entry.implementing_code]
                if not any(_matches_module(c) for c in implementing_ids):
                    continue

            gap_level = ""
            if level is not None:
                # An "unverified" equation by definition has no strong
                # verification, so any test metadata lives on tests in
                # adjacent 1-hop / multi-hop tiers. Filter by whether
                # any of them matches the requested level.
                levels = {
                    self._g.nodes.get(t.id, {}).get("vv_level", "")
                    for t in entry.tests
                }
                if level not in levels:
                    continue
                gap_level = level

            unverified.append(VerificationGap(
                kind="unverified_equation",
                node_id=entry.node.id,
                display_name=entry.node.display_name or entry.node.name,
                module="math",
                level=gap_level,
                detail=f"status={entry.status}",
            ))

        # ---- missing_err_catchers --------------------------------------
        missing_err: list[VerificationGap] = []
        if error_catalog:
            covered: set[str] = set()
            for _, attrs in self._g.nodes(data=True):
                catches = attrs.get("catches")
                if catches:
                    covered.update(catches)
            for err_tag in sorted(error_catalog - covered):
                missing_err.append(VerificationGap(
                    kind="missing_err_catcher",
                    node_id=f"err:{err_tag}",
                    display_name=err_tag,
                    module="",
                    level="",
                    detail="no test declares @pytest.mark.catches for this tag",
                ))

        return VerificationGapsResult(
            untagged_tests=untagged,
            unverified_equations=unverified,
            missing_err_catchers=missing_err,
            filters={
                "module": module,
                "level": level,
                "error_catalog_size": (
                    len(error_catalog) if error_catalog else None
                ),
            },
        )

    # ------------------------------------------------------------------
    # Feature 3: Staleness Detector
    # ------------------------------------------------------------------

    def staleness(self) -> StalenessResult:
        """Detect documentation pages that drifted from code.

        Two independent drift signals:

        * timestamp drift — git says the code a page references was
          modified after the page (needs a workspace root + git);
        * dead references — prose still references symbols/equations
          that no longer exist (see :meth:`dead_references`; works on
          the graph alone). The harder failure of the two: Sphinx
          renders a dead reference as plain text with no warning.
        """
        dead = self.dead_references()

        stale: list[StalenessEntry] = []
        checked = 0

        project_root = self.project_root
        if project_root is None:
            return StalenessResult(
                stale_docs=[], total_stale=0, total_checked=0,
                dead_references=dead.dead[:10],
                total_dead_references=dead.total_dead,
            )

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

            # A page that documents a symbol through more than one edge
            # listed it once per edge — [M] `api/collision_probability`
            # reported `Mesh1D` twice — so the count in `stale_reason`
            # was edges, not symbols, and read as more drift than there
            # is. Order-preserving so the first-seen order survives.
            stale_symbols = list(dict.fromkeys(stale_symbols))

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
            dead_references=dead.dead[:10],
            total_dead_references=dead.total_dead,
        )

    #: Reference-carrying edge types eligible for the dead-reference
    #: audit. CALLS is deliberately absent: call-target resolution is
    #: heuristic (attribute calls, dynamic dispatch), so a phantom
    #: call target is weak evidence that the callee was deleted.
    _DEAD_REF_EDGE_TYPES = frozenset(
        {"references", "documents", "type_uses", "equation_ref"}
    )

    #: Fallback for a query with no checkout to ask. The live set is
    #: :attr:`placeholder_types`, read from the ontology — including a
    #: project's own — so "no concrete definition found" is declared in
    #: one file rather than re-typed in each module that cares.
    #: ``""`` is not an ontology type: it is a node with no type at all,
    #: which is likewise not a definition.
    _PHANTOM_NODE_TYPES = frozenset({"unresolved", "external", ""})

    #: Un-analyzed base classes that are known to add no user-visible
    #: members. Inheriting from these must NOT make member lookups
    #: undecidable — otherwise every Generic-parameterized or ABC
    #: subclass escapes the dead-reference gate entirely.
    _TRANSPARENT_BASES = frozenset({
        "object", "builtins.object",
        "typing.Generic", "typing_extensions.Generic",
        "typing.Protocol", "typing_extensions.Protocol",
        "abc.ABC",
    })

    #: Edge kinds that mean "this file's own code names the target",
    #: as opposed to prose that merely mentions it. An import or a call
    #: is what MINTS a placeholder; a doc reference only consumes one.
    _MINTING_EDGE_TYPES: frozenset[str] = frozenset({
        "imports", "calls", "type_uses", "inherits",
    })

    def _minting_files(self, target_id: str, limit: int = 5) -> list[str]:
        """Source files whose code created this placeholder.

        The ORPHEUS shape behind #36: a prototyping directory still
        imported a module retired months earlier, so nexus minted
        placeholder nodes for the unresolvable path — and bare
        ``:func:`name``` roles on unrelated theory pages then bound to
        them. Live symbols were reported dead against a namespace only a
        throwaway prototype defined.

        Ranking (#41/#42) stopped a placeholder from outranking a real
        definition, so that specific harm is gone. What remains worth
        surfacing is the weaker case: the placeholder is the ONLY match,
        the reference is genuinely reported dead, and the reason is that
        some corner of the tree names a symbol nothing defines. Pointing
        at those files turns "this reference is dead" into "this
        directory is minting a namespace", which is the actionable form.

        Empty when nothing in the codebase names the target — then it is
        simply absent, which is ordinary drift.
        """
        g = self._g
        if target_id not in g:
            return []
        files: list[str] = []
        seen: set[str] = set()
        for src, _, data in g.in_edges(target_id, data=True):
            if data.get("type") not in self._MINTING_EDGE_TYPES:
                continue
            path = (g.nodes.get(src) or {}).get("file_path")
            if not path or path in seen:
                continue
            seen.add(path)
            files.append(path)
            if len(files) >= limit:
                break
        return sorted(files)

    def dead_references(
        self, max_sites_per_target: int = 25,
    ) -> DeadReferencesResult:
        """Doc/docstring references whose target no longer exists.

        The drift shape this catches: a class, function, attribute, or
        equation label was deleted or renamed, but prose — a theory
        page, another docstring, a quoted type annotation — still
        references the old name. Sphinx renders such references as
        plain text and emits no warning, so nothing else surfaces them.

        Detection is static and graph-native. After merge and phantom
        canonicalization, a reference-carrying edge whose target is
        still a phantom node rooted in a PROJECT module names a symbol
        that neither the Sphinx domain nor the AST walker could find
        anywhere in the analyzed tree. References rooted outside the
        project (numpy, stdlib) are not decidable from this graph and
        are never reported. Three rescue passes keep precision high:

        * exact-name match against any concrete node (a property
          referenced as ``:attr:`` resolves to its method node);
        * re-export chase through the ``reexports`` metadata map
          (``pkg.Thing`` is live when ``pkg/__init__.py`` re-exports
          ``pkg.mesh.Thing``);
        * inheritance walk — ``Sub.attr`` is live when any ancestor
          class defines ``attr``; a class with an un-analyzed
          (external/unresolved) base is UNDECIDABLE, not dead.

        Equation references are audited from ``:eq:`` roles and
        ``equation_ref`` edges only — ``:math:`` roles are inline
        presentation, not label references. Sphinx sees every equation
        label in the doc set, so a phantom equation target has no
        blind spot.

        Known static-analysis limits (same as any import-free
        checker): symbols created dynamically (``__getattr__``,
        metaclass magic) can be reported dead despite resolving at
        runtime.
        """
        g = self._g

        project_tops = {
            (attrs.get("name") or "").split(".")[0]
            for _, attrs in g.nodes(data=True)
            if attrs.get("type") == "module"
        }
        project_tops.discard("")

        reexports: dict[str, str] = self._kg.metadata.get("reexports") or {}

        # Every dotted name that has a concrete (non-phantom) node.
        concrete_names: set[str] = set()
        for _, attrs in g.nodes(data=True):
            if attrs.get("type", "") not in self._undefined_types:
                name = attrs.get("name")
                if name:
                    concrete_names.add(name)

        # Candidate targets: phantom nodes referenced by eligible edges.
        sites_by_target: dict[str, list[DeadReferenceSite]] = {}
        kinds: dict[str, str] = {}
        for src, tgt, data in g.edges(data=True):
            edge_type = str(data.get("type", ""))
            if edge_type not in self._DEAD_REF_EDGE_TYPES:
                continue
            tattrs = g.nodes.get(tgt)
            if tattrs is None:
                continue
            # Citations need no special case here: they carry
            # `NodeType.CITATION`, which is not a phantom type, so the
            # check above already excludes them. Until 2026-08-16 they
            # were typed `unresolved` and a `domain == "citation"` test
            # sat here to undo that — the type now carries the fact.
            if tattrs.get("type", "") not in self._undefined_types:
                continue
            reftype = str(data.get("reftype", ""))
            if tgt.startswith("math:equation:"):
                if edge_type != "equation_ref" and reftype != "eq":
                    continue
                kinds[tgt] = "equation"
            elif tgt.startswith("py:"):
                name = tattrs.get("name") or ""
                if name.split(".")[0] not in project_tops:
                    continue
                kinds[tgt] = "python"
            else:
                continue
            sites_by_target.setdefault(tgt, []).append(DeadReferenceSite(
                source=self._node_result(src),
                edge_type=edge_type,
                reftype=reftype,
            ))

        dead: list[DeadReference] = []
        rescued = undecidable = 0
        for tgt, sites in sites_by_target.items():
            tattrs = g.nodes.get(tgt, {})
            name = tattrs.get("name") or tgt.split(":", 2)[-1]
            if kinds[tgt] == "python":
                verdict = self._dead_ref_verdict(
                    name, concrete_names, reexports,
                )
                if verdict == "live":
                    rescued += 1
                    continue
                if verdict == "undecidable":
                    undecidable += 1
                    continue
            dead.append(DeadReference(
                target_id=tgt,
                target_name=name,
                kind=kinds[tgt],
                site_count=len(sites),
                sites=sites[:max_sites_per_target],
                minted_by=self._minting_files(tgt),
            ))

        dead.sort(key=lambda d: (-d.site_count, d.target_name))
        return DeadReferencesResult(
            dead=dead,
            total_dead=len(dead),
            total_sites=sum(d.site_count for d in dead),
            total_checked=len(sites_by_target),
            rescued=rescued,
            undecidable=undecidable,
            project_modules=sorted(project_tops),
        )

    def _dead_ref_verdict(
        self,
        name: str,
        concrete_names: set[str],
        reexports: dict[str, str],
    ) -> str:
        """``"live"`` / ``"dead"`` / ``"undecidable"`` for a
        project-rooted dotted name with no concrete node of its own."""
        from sphinxcontrib.nexus.ast_analyzer import _chase_reexports

        # Judge the name both as written and through re-export
        # aliases: a member referenced by its public path
        # (``pkg.SourceSink.zeros_on``) may only be findable on the
        # defining class's ancestors, so every later check must run
        # on the chased spelling too.
        candidates = [name]
        if reexports:
            resolved = _chase_reexports(name, reexports)
            if resolved != name:
                candidates.append(resolved)

        saw_undecidable = False
        for candidate in candidates:
            if candidate in concrete_names:
                return "live"
            if "." not in candidate:
                continue
            class_path, leaf = candidate.rsplit(".", 1)
            if leaf.startswith("__") and leaf.endswith("__"):
                # Dunder members: ``object`` provides most of them
                # implicitly and ``pkg.mod.__init__`` names a module
                # file — when the owner exists at all, the reference
                # is live.
                if (
                    class_path in concrete_names
                    or f"py:class:{class_path}" in self._g
                ):
                    return "live"
            # The name may be an inherited member: ``Sub.attr`` where
            # ``attr`` is defined on an ancestor of ``Sub``.
            class_id = f"py:class:{class_path}"
            if class_id not in self._g:
                continue
            verdict = self._member_on_ancestors(class_id, leaf, concrete_names)
            if verdict == "live":
                return "live"
            if verdict == "undecidable":
                saw_undecidable = True
        return "undecidable" if saw_undecidable else "dead"

    def _member_on_ancestors(
        self,
        class_id: str,
        leaf: str,
        concrete_names: set[str],
    ) -> str:
        """Walk INHERITS edges looking for ``<ancestor>.<leaf>``.

        Returns ``"live"`` when an ancestor defines the member,
        ``"undecidable"`` when any ancestor is a phantom (an
        un-analyzed base could define anything), else ``"dead"``.
        """
        g = self._g
        visited = {class_id}
        stack = [class_id]
        saw_opaque_base = False
        while stack:
            current = stack.pop()
            for _, base, data in g.out_edges(current, data=True):
                if data.get("type") != "inherits" or base in visited:
                    continue
                visited.add(base)
                battrs = g.nodes.get(base, {})
                bname = battrs.get("name") or base.split(":", 2)[-1]
                if battrs.get("type", "") in self._undefined_types:
                    if bname not in self._TRANSPARENT_BASES:
                        saw_opaque_base = True
                    continue
                if f"{bname}.{leaf}" in concrete_names:
                    return "live"
                stack.append(base)
        return "undecidable" if saw_opaque_base else "dead"

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

    def session_briefing(self) -> BriefingResult:
        """Orientation for an agent starting a session.

        An INDEX, not a payload: every section is a count plus a few
        examples plus the tool that expands it. This is the one reply
        loaded before anyone has asked a question, so its cost is paid
        by every session whether or not it is used — `[M]` 2026-08-16 it
        was **10,564 tokens**, of which 82% were two uncapped lists.
        """
        # Every count here is a `[briefing]` setting — see
        # `project.DEFAULTS`. They are the numbers most likely to want
        # changing as context windows grow, so they belong in a config
        # file rather than in this function.
        symbols_per_page = self.tunable("briefing.symbols_per_stale_page")
        stats_result = self.stats()
        top_nodes = self.god_nodes(top_n=self.tunable("briefing.project_hubs"))

        stale_result = self.staleness()
        stale_docs = [
            replace(entry, affected_symbols=entry.affected_symbols[:symbols_per_page])
            for entry in stale_result.stale_docs[
                : self.tunable("briefing.stale_pages")
            ]
        ]

        # Coverage gaps (equations with code but no tests)
        coverage = self.verification_coverage(status_filter="implemented")
        gaps = coverage.entries[: self.tunable("briefing.coverage_gaps")]

        # Recent changes
        changes_result = DetectChangesResult(
            changed_symbols=[], affected_symbols=[],
            total_changed=0, total_affected=0,
        )
        if self.project_root is not None:
            changes_result = self.detect_changes(scope="branch")

        # Counts
        unresolved = sum(
            1 for _, a in self._g.nodes(data=True)
            if a.get("type") == "unresolved"
        )
        external = sum(
            1 for _, a in self._g.nodes(data=True)
            if a.get("type") == "external"
        )

        id_grammar = self._compute_id_grammar()
        hot_nodes = self._compute_hot_nodes(
            changes_result.changed_symbols,
            god_top_ids={n.id for n in top_nodes},
        )
        preload_hint = PreloadHint(
            description=(
                "Nexus MCP tool schemas are deferred (not loaded by default in "
                "LLM sessions). Paste the tool_search_call below into a single "
                "ToolSearch call on the first turn that uses Nexus — this loads "
                "the eight most-common tools in one round-trip instead of staged."
            ),
            tool_search_call=(
                "select:mcp__nexus__query,mcp__nexus__callers,mcp__nexus__callees,"
                "mcp__nexus__context,mcp__nexus__impact,mcp__nexus__provenance_chain,"
                "mcp__nexus__shortest_path,mcp__nexus__neighbors"
            ),
        )

        showing = {
            "god_nodes": (
                f"top {len(top_nodes)} project hubs by degree; "
                f"`god_nodes(top_n=…)` for more, "
                f"`include_placeholders=True` to rank stdlib too"
            ),
            "stale_docs": (
                f"{len(stale_docs)} of {len(stale_result.stale_docs)} drifted "
                f"pages, each showing up to "
                f"{symbols_per_page} of its affected symbols — "
                f"`staleness()` for all of both"
            ),
            "coverage_gaps": (
                f"{len(gaps)} of {len(coverage.entries)} equations that have "
                f"code but no test — `verification_audit()` for all, with "
                f"grouping"
            ),
            "recent_changes": (
                f"{len(changes_result.changed_symbols)} symbols changed on this "
                f"branch vs the default branch"
                + ("" if changes_result.changed_symbols else
                   " — none, so this branch has touched no indexed symbol")
                + "; `detect_changes()` / `retest()` for the blast radius"
            ),
        }

        return BriefingResult(
            graph_stats=stats_result,
            god_nodes=top_nodes,
            showing=showing,
            stale_docs=stale_docs,
            coverage_gaps=gaps,
            recent_changes=changes_result.changed_symbols[:10],
            unresolved_count=unresolved,
            external_count=external,
            id_grammar=id_grammar,
            hot_nodes=hot_nodes,
            preload_hint=preload_hint,
        )

    def _compute_id_grammar(self) -> IdGrammar:
        """Pick one median-degree node per (domain, type) pair as an example.

        Excludes the ``external`` and ``unresolved`` types — already
        surfaced as counts elsewhere and not useful for constructing
        queries.
        """
        description = (
            "One representative node ID per (domain, type) pair present in "
            "the graph. Use these to learn the ID grammar for constructing "
            "queries against Nexus tools that accept node IDs (callers, "
            "callees, context, impact, provenance_chain, shortest_path, "
            "neighbors). Each id is a verbatim graph key — it round-trips "
            "through context(id=...) without transformation."
        )

        buckets: dict[tuple[str, str], list[str]] = {}
        for nid, attrs in self._g.nodes(data=True):
            ntype = attrs.get("type", "")
            if ntype in ("external", "unresolved"):
                continue
            domain = attrs.get("domain", "")
            buckets.setdefault((domain, ntype), []).append(nid)

        examples: list[IdGrammarExample] = []
        for (domain, ntype) in sorted(buckets.keys()):
            ids = buckets[(domain, ntype)]
            if not ids:
                continue
            # Sort by (degree asc, id asc); pick the median element.
            ranked = sorted(ids, key=lambda n: (self._g.degree(n), n))
            pick = ranked[len(ranked) // 2]
            attrs = self._g.nodes[pick]
            examples.append(IdGrammarExample(
                domain=domain,
                type=ntype,
                id=pick,
                display_name=attrs.get("display_name", "") or attrs.get("name", ""),
            ))

        return IdGrammar(description=description, examples=examples)

    def _compute_hot_nodes(
        self,
        changed_symbols: list[ChangeEntry],
        god_top_ids: set[str],
    ) -> HotNodes:
        """Rank recently-changed, high-degree nodes as likely next queries.

        ``recent_changes`` in the briefing is computed from a git diff of
        the current branch vs its merge base on the repository's default
        branch (see :meth:`detect_changes` with ``scope="branch"``). This
        function uses the same data — there is no separate time/commit
        window.
        """
        description = (
            "Nodes most likely to be queried in this session. Computed from "
            "symbols changed in the current branch vs the default branch, "
            "filtered "
            "to those with degree above the graph median (so 'hot' means "
            "both recent and central). Empty on a fresh branch or when all "
            "changes land on obscure leaf symbols."
        )

        if not changed_symbols:
            return HotNodes(description=description, nodes=[])

        # Median degree over the whole graph, used as a centrality floor.
        all_degrees = sorted(d for _, d in self._g.degree())
        if not all_degrees:
            return HotNodes(description=description, nodes=[])
        median_degree = all_degrees[len(all_degrees) // 2]

        reason_map = {
            "added": "added in current branch",
            "modified": "modified in current branch",
            "deleted": "deleted in current branch",
        }

        seen: set[str] = set()
        candidates: list[HotNode] = []
        for entry in changed_symbols:
            nid = entry.node.id
            if nid in seen or nid in god_top_ids:
                continue
            if nid not in self._g:
                continue
            deg = self._g.degree(nid)
            if deg < median_degree:
                continue
            seen.add(nid)
            candidates.append(HotNode(
                id=nid,
                type=entry.node.type,
                degree=deg,
                reason=reason_map.get(entry.change_type, "changed in current branch"),
            ))

        candidates.sort(key=lambda h: (-h.degree, h.id))
        return HotNodes(description=description, nodes=candidates[:5])

    # ------------------------------------------------------------------
    # Feature 5: Minimum Retest Set
    # ------------------------------------------------------------------

    #: How a test's behaviour can DEPEND on a symbol: it calls it, it is
    #: typed by it, or it inherits from it. Deliberately excludes
    #: ``references``/``imports``, which are MENTION relations — `[M]`
    #: including ``references`` puts the fixed point at 78 % of ORPHEUS's
    #: suite from any starting symbol, i.e. "re-run everything", while
    #: adding ``type_uses``+``inherits`` to ``calls`` costs 0–7 tests and
    #: does not move the depth at which it converges.
    _RETEST_DEPENDENCE_EDGES = ("calls", "type_uses", "inherits")

    def retest(self, scope: str = "all") -> RetestResult:
        """Compute the minimum set of tests to re-run after changes.

        Walks the dependence cone (:data:`_RETEST_DEPENDENCE_EDGES`)
        upstream to a FIXED POINT, so ``safe_to_skip`` is a claim rather
        than an artefact of where the walk stopped.

        ⛔ It used to stop at a hard-coded ``max_depth=3``, and the harm
        was invisible to a spot check because cone depth is a property of
        the SYMBOL, not of the graph. `[M]` on ORPHEUS 2026-08-16,
        collectable tests the cap MISSED — i.e. called ``safe_to_skip``
        while they exercise the changed code:

        ==========================  ======  =====  ======
        changed symbol              capped   true  missed
        ==========================  ======  =====  ======
        ``solve_sn``                   117    117       0
        ``warn_if_unconverged``        347    365      18
        ``geometry.mesh.BC``           944   1176     232
        ==========================  ======  =====  ======

        ``solve_sn`` is the trap: its cone is shallow, so the obvious
        spot-check certifies a cap that is silently losing 232 tests one
        symbol over. Running to the fixed point costs `[M]` 42 ms vs
        39 ms over five hub symbols — the cap was never buying speed.
        """
        changes = self.detect_changes(scope=scope)

        # A COLLECTABLE test — pytest runs functions and methods. The
        # `is_test` flag is also set on the classes, module-level data
        # and attributes that live in test files: `[M]` 7305 nodes carry
        # it and only 5273 are collectable, so counting the suite by the
        # flag alone overstates it by 38 % and inflates `safe_to_skip`
        # by every one of those 2032 non-tests.
        #
        # Not `in_test_file` (`[M]` 9530), which answers the DIFFERENT
        # question "lives in the test tree" and so counts fixtures and
        # helpers too. What a retest set needs is what pytest will run.
        all_tests = {
            nid for nid, attrs in self._g.nodes(data=True)
            if attrs.get("is_test")
            and attrs.get("type") in ("function", "method")
        }

        must_retest: set[str] = set()
        should_retest: set[str] = set()
        deepest = 0

        for entry in changes.changed_symbols:
            result = self.impact(
                entry.node.id,
                direction="upstream",
                max_depth=-1,
                edge_types=list(self._RETEST_DEPENDENCE_EDGES),
            )
            for depth, nodes in result.by_depth.items():
                deepest = max(deepest, depth)
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
            dependence_edges=list(self._RETEST_DEPENDENCE_EDGES),
            cone_depth=deepest,
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
    # Native-place / Feature-Envy diagnostic
    # ------------------------------------------------------------------

    def native_place_candidates(
        self,
        min_callers: int = 1,
        exclude: tuple[str, ...] = (),
        limit: int = 50,
    ) -> list[NativePlaceResult]:
        """Functions whose every caller is a method of a SINGLE class.

        A module-level function called only by methods of one class ``C`` is
        a Feature-Envy / "native place" candidate — logically coupled to
        ``C`` and possibly belonging inside it. **Cross-module** candidates
        (function and class in different modules) are the strongest signal;
        same-module private helpers are weaker (an accepted idiom).

        This is a read-only structural heuristic that SURFACES candidates;
        judgment decides. A pure, independently-tested *free function*
        consumed by one class is usually correct as-is (a primitive, not a
        method) — a high ``excluded_callers`` count (e.g. direct test calls)
        is exactly that signal, so weight such rows down.

        Test callers are recognised via each node's ``is_test`` flag (set
        from ``nexus_test_patterns``); they never count toward the
        single-class criterion and are reported in ``excluded_callers``.

        Args:
            min_callers: Minimum considered (non-test) method callers
                required to surface a candidate.
            exclude: Extra substrings; a function OR caller whose node id
                contains one is ignored, on top of the ``is_test`` flag.
                Use for non-test-but-non-production trees (e.g.
                ``("scratch", "derivations")``).
            limit: Maximum candidates to return (0 = all). Ranked
                lexicographically by descending strength: genuine
                relocations before ``likely_free_primitive`` rows, then
                cross-module before same-module, private before public,
                fewer excluded (test) callers, and finally more
                single-class callers (stronger coupling) as a tiebreak.
        """
        def dropped(node_id: str) -> bool:
            if self._g.nodes.get(node_id, {}).get("is_test"):
                return True
            return any(tok in node_id for tok in exclude)

        # method -> owning class (CONTAINS: class contains method);
        # callee -> caller ids (CALLS). Single pass over edges.
        method_class: dict[str, str] = {}
        callers: dict[str, set[str]] = {}
        for src, tgt, data in self._g.edges(data=True):
            etype = data.get("type")
            if etype == EdgeType.CONTAINS:
                if (self._g.nodes.get(src, {}).get("type") == NodeType.CLASS
                        and self._g.nodes.get(tgt, {}).get("type") == NodeType.METHOD):
                    method_class[tgt] = src
            elif etype == EdgeType.CALLS:
                callers.setdefault(tgt, set()).add(src)

        out: list[NativePlaceResult] = []
        for node_id, attrs in self._g.nodes(data=True):
            if attrs.get("type") != NodeType.FUNCTION or dropped(node_id):
                continue
            all_callers = callers.get(node_id, set())
            considered = [c for c in all_callers if not dropped(c)]
            if len(considered) < min_callers:
                continue
            # every considered caller must be a method of one same class
            owners: set[str] = set()
            ok = True
            for c in considered:
                cattrs = self._g.nodes.get(c, {})
                if cattrs.get("type") == NodeType.METHOD and c in method_class:
                    owners.add(method_class[c])
                else:
                    ok = False
                    break
            if not ok or len(owners) != 1:
                continue
            target = next(iter(owners))
            short = attrs.get("name", "").rsplit(".", 1)[-1]
            out.append(NativePlaceResult(
                function=self._node_result(node_id),
                target_class=self._node_result(target),
                caller_count=len(considered),
                cross_module=_module_of(self._g, node_id) != _module_of(self._g, target),
                private=short.startswith("_"),
                excluded_callers=len(all_callers) - len(considered),
            ))

        # Descending strength. Every term reads "larger = stronger" so a
        # single reverse=True orders them all; the lone "fewer is stronger"
        # axis (test callers) is negated to fit that convention. The
        # leading term sinks tested free-primitives below genuine
        # relocations — the old caller-count-first key buried single-caller
        # relocations under noisier, well-tested primitives.
        out.sort(
            key=lambda r: (
                not r.likely_free_primitive,
                r.cross_module,
                r.private,
                -r.excluded_callers,
                r.caller_count,
            ),
            reverse=True,
        )
        return out if limit <= 0 else out[:limit]

    def twin_paths(
        self,
        min_similarity: float = 0.7,
        min_tokens: int = 35,
        exclude: tuple[str, ...] = (),
        limit: int = 50,
        max_bucket: int = 40,
    ) -> list[TwinPathResult]:
        """Pairs of functions that independently implement the same computation.

        A **twin path** is two functions whose bodies share a high fraction of
        structural shingles (a Type-2/3 clone) but where neither calls the
        other — the coding-elegance Pattern-2 / single-source-of-truth smell.
        The shingles come from the AST body fingerprint
        (:mod:`sphinxcontrib.nexus.fingerprint`) stamped on each function at
        build time, so this captures the array math — ``@``, ``einsum``,
        slicing — that the call graph cannot see.

        This is a read-only heuristic that SURFACES candidates; judgment
        decides. Symmetric-by-design pairs (``apply``/``apply_transpose``,
        ``domain``/``codomain``) and shared small templates (a one-line
        ``residual < tol`` convergence check) legitimately resemble each other
        — read the bodies before declaring a duplication.

        Functions that directly call each other are dropped (one delegating to
        the other is not an independent reimplementation); the minimum-token
        gate removes thin stubs whose structure is too sparse to compare.

        Args:
            min_similarity: Minimum Jaccard shingle overlap to report
                (0.0–1.0). Higher is stricter; genuine duplicates score
                ``>= 0.8`` while structurally-similar siblings sit near 0.6.
            min_tokens: Minimum body token count; functions below it are
                ignored (too trivial to judge).
            exclude: Extra substrings; a function whose node id contains one
                is ignored, on top of the ``is_test`` flag. Use for
                non-production trees (e.g. ``("derivations", "scratch")``).
            limit: Maximum pairs to return (0 = all). Sorted by descending
                similarity, cross-module pairs first on ties.
            max_bucket: Skip shingles shared by more than this many functions
                when generating candidate pairs — these ubiquitous fragments
                (loop/return boilerplate) would explode the pair count without
                adding signal. Similarity is still computed over the FULL
                shingle sets, so precision is unaffected; a genuine twin shares
                rarer shingles too and is still generated.
        """
        def dropped(node_id: str) -> bool:
            if self._g.nodes.get(node_id, {}).get("is_test"):
                return True
            return any(tok in node_id for tok in exclude)

        # Functions with a substantial fingerprint.
        fps: dict[str, set[int]] = {}
        for node_id, attrs in self._g.nodes(data=True):
            if attrs.get("type") not in (NodeType.FUNCTION, NodeType.METHOD):
                continue
            if dropped(node_id):
                continue
            shingles = attrs.get("body_shingles")
            if not shingles or attrs.get("body_ntokens", 0) < min_tokens:
                continue
            fps[node_id] = set(shingles)

        # Direct call adjacency — a pair where one calls the other is a
        # delegation, not an independent twin.
        calls: set[tuple[str, str]] = set()
        for src, tgt, data in self._g.edges(data=True):
            if data.get("type") == EdgeType.CALLS:
                calls.add((src, tgt))

        # Candidate pairs: functions sharing at least one non-ubiquitous
        # shingle. Inverted index keeps this near-linear instead of O(n^2).
        inverted: dict[int, list[str]] = {}
        for node_id, shingles in fps.items():
            for s in shingles:
                inverted.setdefault(s, []).append(node_id)
        candidates: set[tuple[str, str]] = set()
        for ids in inverted.values():
            if not 1 < len(ids) <= max_bucket:
                continue
            ids.sort()
            for i in range(len(ids)):
                for j in range(i + 1, len(ids)):
                    candidates.add((ids[i], ids[j]))

        out: list[TwinPathResult] = []
        for a, b in candidates:
            if (a, b) in calls or (b, a) in calls:
                continue
            sim = jaccard(fps[a], fps[b])
            if sim < min_similarity:
                continue
            out.append(TwinPathResult(
                a=self._node_result(a),
                b=self._node_result(b),
                similarity=round(sim, 4),
                cross_module=_module_of(self._g, a) != _module_of(self._g, b),
            ))

        out.sort(key=lambda r: (r.similarity, r.cross_module), reverse=True)
        return out if limit <= 0 else out[:limit]

    def discriminations(
        self,
        min_sites: int = 2,
        exclude: tuple[str, ...] = (),
        limit: int = 50,
    ) -> list[DiscriminationResult]:
        """Tags discriminated at multiple sites — candidate missing types.

        A function that branches on a string/enum *tag* (``if geometry ==
        "spherical"``, ``match kind:``) carries a ``discriminates_on`` edge to
        that tag (emitted by the AST extractor). The same tag discriminated by
        many functions is the coding-elegance smell "a repeated conditional is
        a missing type — discriminate once, at the boundary": the repeated
        tests should usually collapse to one dispatch (a type / single
        registry / polymorphic call).

        This is a read-only heuristic that SURFACES candidates; judgment
        decides. A genuinely open set with no shared behaviour (axis labels, a
        one-off parse) may legitimately stay a tag. Read the sites before
        concluding a type is missing.

        Args:
            min_sites: Minimum distinct discriminating functions to report a
                tag (default 2 — a single site is not yet a repetition).
            exclude: Substrings; a discriminating function whose node id
                contains one is ignored, on top of the ``is_test`` flag.
            limit: Maximum tags to return (0 = all). Sorted by descending
                site count, then tag name.
        """
        def dropped(node_id: str) -> bool:
            if self._g.nodes.get(node_id, {}).get("is_test"):
                return True
            return any(tok in node_id for tok in exclude)

        # tag node -> {function id: case labels}
        sites: dict[str, dict[str, tuple[str, ...]]] = {}
        for src, tgt, data in self._g.edges(data=True):
            if data.get("type") != EdgeType.DISCRIMINATES_ON or dropped(src):
                continue
            sites.setdefault(tgt, {})[src] = tuple(data.get("cases", ()))

        out: list[DiscriminationResult] = []
        for tag_id, by_func in sites.items():
            if len(by_func) < min_sites:
                continue
            cases = sorted({c for labels in by_func.values() for c in labels})
            tag_name = self._g.nodes.get(tag_id, {}).get("name", tag_id)
            out.append(DiscriminationResult(
                tag=tag_name,
                site_count=len(by_func),
                cases=cases,
                sites=[self._node_result(f) for f in sorted(by_func)],
            ))

        out.sort(key=lambda r: (r.site_count, r.tag), reverse=True)
        return out if limit <= 0 else out[:limit]

    def dead_functions(
        self,
        exclude: tuple[str, ...] = (),
        limit: int = 50,
    ) -> list[DeadFunctionResult]:
        """Functions/methods with no static callers — dead-code candidates.

        A function with zero incoming ``calls`` edges (from non-test, non-
        excluded code) is a candidate for removal. This is a **candidate
        list, not a verdict**: the static call graph cannot see dynamic
        dispatch (registry / ``getattr`` / a callback handed to ``solve_ivp``),
        and public entry points are legitimately uncalled internally. Each
        result carries ``public`` and ``decorated`` flags so judgment can
        weigh those false-positive sources; the strongest signal — a
        *private, undecorated* function with no caller — is ranked first.

        Dunder methods are excluded (they are invoked implicitly by the
        language, never via an explicit call edge, so they always look dead).

        Args:
            exclude: Substrings; a function whose node id contains one, OR a
                *caller* whose id contains one, is ignored — on top of the
                ``is_test`` flag (a function called only by tests still reads
                as dead).
            limit: Maximum results (0 = all). Ranked private/undecorated/
                plain-function first.
        """
        def dropped(node_id: str) -> bool:
            if self._g.nodes.get(node_id, {}).get("is_test"):
                return True
            return any(tok in node_id for tok in exclude)

        # Callees that have at least one non-dropped (non-test) caller.
        called: set[str] = set()
        for src, tgt, data in self._g.edges(data=True):
            if data.get("type") == EdgeType.CALLS and not dropped(src):
                called.add(tgt)

        out: list[DeadFunctionResult] = []
        for node_id, attrs in self._g.nodes(data=True):
            ntype = attrs.get("type")
            if ntype not in (NodeType.FUNCTION, NodeType.METHOD):
                continue
            if dropped(node_id) or node_id in called:
                continue
            leaf = _leaf_name(self._g, node_id)
            if _is_dunder(leaf):
                continue
            blind = self.unresolved_callers(node_id)
            out.append(DeadFunctionResult(
                function=self._node_result(node_id),
                is_method=ntype == NodeType.METHOD,
                public=not leaf.startswith("_"),
                decorated=bool(attrs.get("decorators")),
                unresolved_calls=blind.count if blind else 0,
            ))

        # Rows the resolver may simply have lost go LAST: an unresolved
        # call naming this function is evidence AGAINST deleting it, and
        # it outranks every other flag here — `public`/`decorated` say
        # "being uncalled is expected", while this says "it is probably
        # called and I could not see it". Then private → undecorated →
        # plain function (strongest dead signal), name as a tiebreak.
        out.sort(key=lambda r: (
            bool(r.unresolved_calls), r.public, r.decorated,
            r.is_method, r.function.name,
        ))
        return out if limit <= 0 else out[:limit]

    def protocol_conformers(
        self,
        min_methods: int = 2,
        exclude: tuple[str, ...] = (),
        limit: int = 50,
    ) -> list[ProtocolConformerResult]:
        """Classes that satisfy a Protocol's method-set without declaring it.

        Python ``Protocol``s are satisfied *structurally*, but the AST
        ``inherits`` edge records only *explicit* subclassing — so a
        structural conformer has no edge, and "is every implementation
        connected to its Protocol?" is unanswerable from ``inherits`` alone.
        This matches a class to a Protocol when the class defines (by NAME)
        every non-dunder method the Protocol declares yet does not inherit it.

        A heuristic, not authoritative: it compares method *names*, not
        signatures, and only direct methods (not those inherited from a
        mixin). The definitive check is a type checker — pyright or LSP
        ``goToImplementation``. Use this to find classes to either declare
        conformance on, or as evidence a Protocol is load-bearing.

        Args:
            min_methods: Minimum Protocol method-set size to consider
                (default 2 — single-method Protocols match too broadly).
            exclude: Substrings; a Protocol or candidate class whose node id
                contains one is ignored, on top of the ``is_test`` flag.
            limit: Maximum Protocols to return (0 = all). Sorted by
                descending conformer count.
        """
        def dropped(node_id: str) -> bool:
            if self._g.nodes.get(node_id, {}).get("is_test"):
                return True
            return any(tok in node_id for tok in exclude)

        inherits: dict[str, set[str]] = {}
        methods_of: dict[str, set[str]] = {}
        for src, tgt, data in self._g.edges(data=True):
            etype = data.get("type")
            if etype == EdgeType.INHERITS:
                inherits.setdefault(src, set()).add(tgt)
            elif etype == EdgeType.CONTAINS:
                if self._g.nodes.get(tgt, {}).get("type") == NodeType.METHOD:
                    leaf = _leaf_name(self._g, tgt)
                    if not _is_dunder(leaf):
                        methods_of.setdefault(src, set()).add(leaf)

        def is_protocol(class_id: str) -> bool:
            return any(
                _leaf_name(self._g, p) == "Protocol"
                for p in inherits.get(class_id, ())
            )

        def ancestors(class_id: str) -> set[str]:
            seen: set[str] = set()
            stack = list(inherits.get(class_id, ()))
            while stack:
                parent = stack.pop()
                if parent in seen:
                    continue
                seen.add(parent)
                stack.extend(inherits.get(parent, ()))
            return seen

        classes = [
            n for n, a in self._g.nodes(data=True)
            if a.get("type") == NodeType.CLASS and not dropped(n)
        ]
        anc = {c: ancestors(c) for c in classes}

        out: list[ProtocolConformerResult] = []
        for proto in classes:
            if not is_protocol(proto):
                continue
            proto_methods = methods_of.get(proto, set())
            if len(proto_methods) < min_methods:
                continue
            conformers = [
                c for c in classes
                if c != proto
                and not is_protocol(c)
                and proto not in anc[c]               # doesn't already declare it
                and proto_methods <= methods_of.get(c, set())
            ]
            if conformers:
                out.append(ProtocolConformerResult(
                    protocol=self._node_result(proto),
                    methods=sorted(proto_methods),
                    conformers=[self._node_result(c) for c in sorted(conformers)],
                ))

        out.sort(key=lambda r: (len(r.conformers), r.protocol.name), reverse=True)
        return out if limit <= 0 else out[:limit]

    # ------------------------------------------------------------------
    # Runtime overlay — join a RuntimeRun onto the static graph
    # ------------------------------------------------------------------

    def runtime_hotspots(
        self,
        run: "RuntimeRun",
        by: str = "cumtime",
        limit: int = 20,
    ) -> list[HotspotResult]:
        """Nodes ranked by an observed runtime metric — the dynamic stage DAG.

        ``by="cumtime"`` gives the dominant *observed* call chain (strictly
        better than the static ``processes`` out-degree heuristic for traced
        runs); ``by="ncalls"`` the iteration-count / recompute smell;
        ``by="tottime"`` self-time hotspots. Reads ``run.calls`` (a cProfile
        run); a coverage run has no timing and returns ``[]``.
        """
        if by not in ("cumtime", "ncalls", "tottime"):
            raise ValueError(f"by must be cumtime|ncalls|tottime, got {by!r}")
        out = [
            HotspotResult(
                node=self._node_result(node_id),
                ncalls=int(m["ncalls"]),
                tottime=m["tottime"],
                cumtime=m["cumtime"],
            )
            for node_id, m in run.calls.items()
            if node_id in self._g
        ]
        out.sort(key=lambda r: getattr(r, by), reverse=True)
        return out if limit <= 0 else out[:limit]

    def runtime_edges(
        self,
        run: "RuntimeRun",
        mode: str = "dynamic_only",
        node: str = "",
        substantive_only: bool = False,
        limit: int = 50,
    ) -> list[RuntimeEdgeResult]:
        """Overlay a run's call edges on the static CALLS edges.

        ``mode``:

        * ``dynamic_only`` — fired edges with NO static counterpart: the
          dispatch the static resolver can't see (annotation-mediated dispatch
          through ``self``/typed locals, issue #16) and the resolved face of
          polymorphism (which concrete impl actually ran). Ranked by count.
        * ``fired`` — fired edges that DO match a static edge, now carrying
          their call count (static structure confirmed live).
        * ``dead`` — static CALLS edges among run-reachable nodes that never
          fired (``count`` 0). A single run's dead set is "dead in THIS run",
          not dead code — union several canonical runs for a real verdict.

        ``node`` (a node-id substring) restricts to edges whose source matches.
        ``substantive_only`` drops edges where either endpoint is a
        property/trivial accessor — so the polymorphic dispatch (the #16
        payoff) is not buried under property-getter call edges, which dominate
        ``dynamic_only`` raw. Reads ``run.edges`` (a cProfile run).
        """
        if mode not in ("dynamic_only", "fired", "dead"):
            raise ValueError(
                f"mode must = dynamic_only|fired|dead, got {mode!r}"
            )
        static_calls = {
            (u, v) for u, v, d in self._g.edges(data=True)
            if d.get("type") == EdgeType.CALLS
        }
        dyn = {(u, v): c for u, v, c in run.edges}

        def keep(u: str) -> bool:
            return not node or node in u

        out: list[RuntimeEdgeResult] = []
        if mode == "dead":
            reachable = {u for u, _, _ in run.edges} | {v for _, v, _ in run.edges}
            reachable |= set(run.calls)
            for u, v in static_calls:
                if u in reachable and v in reachable and (u, v) not in dyn and keep(u):
                    out.append(self._runtime_edge(u, v, 0, in_static=True))
            out.sort(key=lambda r: r.source.name)
        else:
            want_static = mode == "fired"
            for (u, v), count in dyn.items():
                # Endpoints came from the sidecar, resolved at INGEST time; the
                # graph is rebuilt between ingest and query (the re-bind the
                # sidecar exists for), so a node may have been renamed/removed.
                # Skip stale endpoints — _node_result on a missing node yields
                # an unserializable degree view. (The `dead` branch is safe:
                # its endpoints come from the live static_calls.)
                if u not in self._g or v not in self._g:
                    continue
                if ((u, v) in static_calls) == want_static and keep(u):
                    out.append(self._runtime_edge(u, v, count, in_static=want_static))
            out.sort(key=lambda r: r.count, reverse=True)
        if substantive_only:
            out = [e for e in out if not e.accessor]
        return out if limit <= 0 else out[:limit]

    def _runtime_edge(
        self, source: str, target: str, count: int, in_static: bool,
    ) -> RuntimeEdgeResult:
        return RuntimeEdgeResult(
            source=self._node_result(source),
            target=self._node_result(target),
            count=count,
            in_static=in_static,
            accessor=self._is_accessor(source) or self._is_accessor(target),
        )

    def _is_accessor(self, node_id: str) -> bool:
        """A property / trivial getter — plumbing, not substantive logic.

        Primary signal is correct-by-construction: a ``@property`` /
        ``@cached_property`` IS an accessor by definition. The
        ``_ACCESSOR_MAX_SPAN``-line fallback catches undecorated one-liner
        getters; it is a heuristic with a known false-negative — a genuine
        ≤2-line polymorphic dispatcher would be misclassified and dropped by
        ``substantive_only`` — so keep the span tight.
        """
        attrs = self._g.nodes.get(node_id, {})
        if any("property" in d for d in (attrs.get("decorators") or [])):
            return True
        ln, end = attrs.get("lineno"), attrs.get("end_lineno")
        return bool(ln and end and (end - ln) <= _ACCESSOR_MAX_SPAN)

    def runtime_markers(
        self,
        run: "RuntimeRun",
        marker: str = "",
        node: str = "",
        limit: int = 50,
    ) -> list[MarkedTestResult]:
        """Tests carrying a marker, as pytest RESOLVED it at collection.

        Reads ``run.markers`` (a ``pytest`` run). The set is not the four
        names an AST walk knows — it is whatever the project registers,
        so a new marker costs nothing here. `[M]` on ORPHEUS the AST path
        reports **0** nodes for ``foundation``, ``cap``, ``regression``
        and ``sentinel``; this resolves **3709 / 1707 / 111 / 39**.

        ``marker`` filters by name (empty = every marked test);
        ``node`` restricts to node-ids containing the substring.

        Each result carries the pytest ids that produced it, so
        :attr:`MarkedTestResult.invocation` is a runnable command rather
        than a set of graph ids the caller must translate.
        """
        out: list[MarkedTestResult] = []
        for node_id, marks in run.markers.items():
            if node_id not in self._g:
                continue
            if marker and marker not in marks:
                continue
            if node and node not in node_id:
                continue
            out.append(MarkedTestResult(
                node=self._node_result(node_id),
                markers=dict(marks),
                pytest_ids=list(run.pytest_ids.get(node_id, [])),
            ))
        # Most-marked first: a test carrying several claims is the one
        # worth reading, and it keeps a truncated answer informative.
        out.sort(key=lambda r: (-len(r.markers), r.node.id))
        return out if limit <= 0 else out[:limit]

    def runtime_branches(
        self,
        run: "RuntimeRun",
        node: str = "",
        partial_only: bool = True,
        limit: int = 50,
    ) -> list[BranchCoverageResult]:
        """Branch coverage per node — the accidental-vs-essential signal.

        A node with ``branches_hit < branches_total`` did not take every
        conditional outcome in the run. Nodes that also ``discriminates_on`` a
        tag are flagged (``discriminates`` populated) and ranked first: a
        discrimination always taken one way is a missing type, the dynamic
        counterpart of the static ``discriminations`` smell. Reads
        ``run.coverage`` (a coverage run).

        ``partial_only`` (default) keeps only nodes with an unexercised branch;
        ``node`` restricts to node-ids containing the substring.
        """
        discriminated = self._discriminating_tags()
        out: list[BranchCoverageResult] = []
        for node_id, c in run.coverage.items():
            if node_id not in self._g:
                continue
            if node and node not in node_id:
                continue
            total, hit = c["branches_total"], c["branches_hit"]
            if partial_only and not (total >= 2 and hit < total):
                continue
            out.append(BranchCoverageResult(
                node=self._node_result(node_id),
                lines_hit=c["lines_hit"],
                lines_total=c["lines_total"],
                branches_hit=hit,
                branches_total=total,
                discriminates=sorted(discriminated.get(node_id, set())),
            ))
        # missing-type suspects (discriminate + partial) first, then by the
        # count of unexercised branches.
        out.sort(
            key=lambda r: (
                bool(r.discriminates),
                r.branches_total - r.branches_hit,
            ),
            reverse=True,
        )
        return out if limit <= 0 else out[:limit]

    def _discriminating_tags(self) -> dict[str, set[str]]:
        """node_id -> set of tag names it discriminates on (DISCRIMINATES_ON)."""
        out: dict[str, set[str]] = {}
        for src, tgt, data in self._g.edges(data=True):
            if data.get("type") == EdgeType.DISCRIMINATES_ON:
                name = self._g.nodes.get(tgt, {}).get("name", tgt)
                out.setdefault(src, set()).add(name)
        return out

    def runtime_timeline(
        self,
        run: "RuntimeRun",
        max_depth: int = -1,
        limit: int = 50,
    ) -> list[TimelineEntry]:
        """The observed execution sequence — nodes in order of first entry.

        The unique thing a viztracer run adds over cProfile is *order*: this is
        the actual stage sequence (mesh → discretize → sweep → iterate →
        result), not aggregate counts. ``max_depth`` (≥ 0) keeps only nodes at
        or above that call-stack depth, yielding just the high-level stages;
        the default ``-1`` keeps every depth. Reads ``run.timeline`` (a
        viztracer run).
        """
        out: list[TimelineEntry] = []
        for node_id, m in run.timeline.items():
            if node_id not in self._g:
                continue
            depth = int(m["min_depth"])
            if max_depth >= 0 and depth > max_depth:
                continue
            out.append(TimelineEntry(
                node=self._node_result(node_id),
                first_ts=m["first_ts"],
                count=int(m["count"]),
                depth=depth,
            ))
        out.sort(key=lambda e: e.first_ts)
        return out if limit <= 0 else out[:limit]

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
