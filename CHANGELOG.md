# Changelog

All notable changes to sphinxcontrib-nexus.

## Unreleased

### Added — the math has structure now (#19, #21)

Equations were graph leaves. The graph knew which code implemented an equation
and which test verified it, but nothing about how the equations relate to each
other — so `provenance_chain` returned a flat list where a validator wants a
spine.

**Statement relations** (#19). Three directives declare that structure:

```rst
.. math::
   :label: sn-dd-closure

   \psi_c = \tfrac{1}{2}(\psi_L + \psi_R)

.. discretizes:: sn-transport-continuous
```

- `discretizes` — discrete form → the continuous one it discretizes
- `derives-from` — specialization → its parent
- `approximates` — closure/truncation → the exact form it stands in for

Each names its target as the argument; the source comes from `:label:` or, when
omitted, from the nearest preceding labeled statement — which is where these get
written in practice, directly under the equation they describe. New `EdgeType`
values `DISCRETIZES` / `DERIVES_FROM` / `APPROXIMATES`, tagged
`source="directive"` like the existing verification directives and surviving
incremental builds through the same pending queue.

Misuse warns and is dropped, never breaking the build: no bindable source, a
statement related to itself, or a target that doesn't exist.

**sphinx-proof environments** (#21). Every *labeled* `prf:` environment —
definition, theorem, algorithm, and the dozen others — becomes a `proof_object`
node carrying its title, its statement text, and its kind in
`metadata["prf_type"]`. The `prf` domain exposes neither `object_types` nor
`get_objects()`, so these are read from `env.proof_list` and enriched from the
doctree; `:prf:ref:` targets resolve to the new nodes, which matters because an
unresolved one would otherwise surface as a false dead reference.

One node type rather than fifteen: the environment kind is an attribute and the
node id (`prf:theorem:thm-balance`) already carries it. Unlabeled environments
are skipped — sphinx-proof gives them a serial-numbered synthetic label that
renumbers whenever anything above them moves, and nothing can reference them.

Both ends of a relation may be an equation *or* a proof environment, so
*Theorem 3.4 derives-from Definition 3.2* needs no new syntax.

**Consumed by `provenance_chain`**, which now returns a `relations` list of
`source ─relation→ target` triples reachable from the queried node — from a
code symbol, an equation, or a proof environment. Each link is reported once,
in its authored direction.

Tested against a real `sphinx-proof` build (`tests/roots/test-proof-relations`)
rather than a fixture guess, since the whole point is what the upstream
extension actually publishes. `sphinx-proof` is now a test-only dependency.

### Fixed — test helpers no longer absorb production references

Bare-name fuzzy matching is deliberately more permissive than Sphinx: an api
page writing ``:class:`CPMesh``` with no `currentmodule` fails Sphinx's own
lookup and renders as plain text, while nexus resolves it. That generosity
recovers thousands of real doc-page-to-class links and is kept.

The test tree is where it went wrong — test modules are full of short generic
names, so they acted as a magnet for any bare name with no better candidate. On
ORPHEUS a reactor-physics operator's ``:attr:`K``` bound to a test class's
attribute, and ``record`` to `tests._harness.registry`. The direction is the
discriminator: a test-tree candidate is refused for a phantom that anything
outside the test tree references. Test-to-test references are untouched, and
explicit fully-qualified references never create a phantom so are unaffected.

Two supporting fixes: `is_test` was never set on test-tree nodes when the test
root was itself the analysed source dir (patterns are project-relative, paths
were source-dir-relative), and `merge_graphs` dropped the flag for any test
symbol that was also autodoc'd. A new `in_test_file` marker answers "does this
live in the test tree?", which is a different question from `is_test`'s "is this
a test case?" — the latter is name-based and load-bearing for `retest` and
`dead_functions`.

This surfaced one true dead reference on ORPHEUS that leaf-matching had been
hiding: a docstring naming `tests.sn.test_scattering_operator` after the module
moved under `operators/`.

### Added — `:numref:` binds to figure and table labels (#45)

`:numref:` names a target by its number rather than its title, but it names the
same object every other role does. The equation case landed in #38; figures and
tables were still minting a placeholder beside the real node, splitting one
figure's references across two ids and surfacing the placeholder as a dead
reference. They carry `:name:` labels, which Sphinx's std domain publishes as
ordinary labels — an exact key lookup, guarded by an existence check, so a
`:numref:` naming nothing stays unresolved rather than being handed a fabricated
node.

Validated by fixture (`tests/roots/test-numref`, a real `sphinx-build`) rather
than against ORPHEUS, which contains zero `.. figure::` or `.. table::`
directives and uses only the math-domain `:math:numref:` spelling.

### Changed — reconciliation decides once, over the whole graph (#46)

`merge_graphs` runs once per source directory, and unresolved-node
reconciliation lived inside it — a decision needing project-wide knowledge,
taken from a single slice. The previous fix widened its index to span both
graphs, which repaired the observed binding but left the failure mode: a rival
arriving in a *later* slice could not retroactively make an earlier decision
ambiguous. It was safe only because ORPHEUS merges its main tree first.

Now `merge.reconcile_unresolved(graph)`, called once after every merge in both
the Sphinx and CLI paths. `merge_graphs` is a pure merge again.

### Added — dead references name the code that minted them (#39)

`DeadReference.minted_by` lists the source files whose own code — an import,
call, annotation, or base class — created a placeholder. Non-empty means the
target is not simply absent: it exists as a name only because those files
reference it, and a role elsewhere then bound to it. When they sit in one
unmaintained corner of the tree, that directory is the finding rather than the
reference.

Prose does not count as minting. A page mentioning a vanished symbol is the
reference being reported, not its cause.

### Added — relative references resolve against their namespace (#37)

Half of a real project's Python references are relative —
``:meth:`Quadrature.product```, ``:class:`SNMesh```, ``:meth:`apply``` — and
Sphinx resolves those against the current module and class. The graph already
knew each reference's source node, so the context was present and simply never
consulted.

`_resolve_relative_references` reproduces `PythonDomain.find_obj` with
`searchmode=0`: `modname.classname.target`, then `modname.target`, then
`target` **as a fully qualified key**. That last step is the counterintuitive
one and is now encoded — the registry is keyed by full dotted names, so a bare
``:func:`solve``` does not resolve merely because its module was
`automodule`-d, which is why adding autodoc coverage cannot fix relative
references from the consumer side.

**It retargets the edge, not the node.** A phantom is shared by every referrer
that spelled the same name: on ORPHEUS, 532 bare phantoms have more than one
distinct source and ``:meth:`apply``` alone is referenced from 132 docstrings.
Folding the node would force one answer on all of them; only the edge carries
the namespace that decides. Each operator class's ``:meth:`apply``` now binds
to its own method.

Runs before `_canonicalize_phantoms` — namespace context is an answer,
leaf-matching is a guess, and an answer must not lose to a guess that sorts
first.

Measured on ORPHEUS: **89.6%** of Python-to-Python references land on a real
definition (10215/11407). The remainder is the honest bucket — builtins,
third-party (`numpy.linalg.eig`, `mpmath.quad`), and relative names whose own
namespace genuinely lacks them, none of which Sphinx would link either.
### Added — attributes bound after the class body, and `#:` comment prose (#40, #38)

Landed together because #38 documents the ordering hazard between them: widening
the scanned reference surface before the indexer can see an attribute
manufactures false dead references.

**Post-class-body attributes.** The standard way to build enum-like singletons
on a non-Enum class was invisible to the indexer::

    @dataclass(frozen=True)
    class BC:
        kind: str

    BC.vacuum = BC("vacuum")   # type: ignore[attr-defined]

These are real class attributes at import time — autodoc documents them and
``:data:`BC.vacuum``` renders as a working link — but the class body holds no
trace, so every reference was reported dead. Bound only for classes defined in
the same module: ``os.environ = {}`` is somebody else's namespace.

**`#:` attribute comments.** Sphinx's attribute-comment form carries real
cross-references (168 py-domain roles on ORPHEUS) and `ast` discards comments
outright, so the token stream is the only way to read them. Uses `tokenize`
rather than a line regex, so a ``#:`` inside a string literal is not mistaken
for one; handles both the leading-block and trailing (``x = 1  #: doc``) forms.

**`:numref:` crosswalk.** ``:numref:`peierls-3d``` on an equation label names the
same target as ``:eq:``; it was minting a `math:numref:` placeholder beside the
real equation node, splitting one equation's references across two ids. Guarded
by an existence check, so a figure reference cannot fabricate an equation.
`:numref:` to figures and tables remains unresolved — that is the std-label
namespace and separate work.

### Fixed — reference resolution stops inventing answers (#36)

Four passes independently decide "which node did this name mean?" — Sphinx
`pending_xref` resolution, the post-merge phantom fold, merge-time type
conflicts, and merge-time unresolved reconciliation. They carried three
separate rank tables (two byte-identical) and, between them, three ways to
pick wrongly.

**Ranking, not insertion order.** `resolve_target_id` returned the first node
whose name ended in the reftarget, so a placeholder minted from a retired
import path could beat a real definition by luck of graph order. On ORPHEUS a
bare ``:func:`compute_G_bc``` bound to a phantom instead of the live function,
and `dead_references` then reported the phantom the resolver had just invented.
The exact-match fast path had the same defect one level up: an exact hit on a
bare tombstone short-circuited before ranking ran.

**One ranking, shared.** `_mappings.candidate_rank` is now the single answer —
real definition over placeholder, then the role's type preference, concreteness,
file-backed, shortest qualified name, node id. `candidates_are_ambiguous` names
the other half: the last two levels make the sort total, not correct. Passes
that rewire the graph decline on ambiguity; passes that must return something
take the minimum.

**Ambiguity judged across passes.** `merge_graphs` runs once per source
directory, so a name with rivals elsewhere looks unique inside any one slice.
ORPHEUS merges `tests` after the main tree, and a docstring's bare
``:mod:`derivations``` bound to the TEST package — unambiguous within the
slice, wrong across the project. The candidate index now spans the incoming
slice and the accumulated graph.

Measured across three real ORPHEUS rebuilds: dead references 14 → 7. The
last step raised the count from 6, and that is the intended direction — a
silent misattribution became a visible unknown. 281 nodes that were being
wrongly folded now stand alone, almost all docstring prose fragments
(`optional`, `N`, `ng`) that had been attaching to real symbols and
manufacturing false edges.

Remaining ORPHEUS findings are tracked in #40 (attributes bound after the
class body) and #37 (namespace-aware resolution).

## 0.16.1 — 2026-08-09

### Fixed

- **`/doc-health` produced an empty report on every project.** The shipped
  slash command invoked a bare `nexus`, which is not on PATH in the normal
  layout — the binary lives in the project's `.venv/bin/`. Both `!` lines
  resolved to `command not found`, so the command looked installed, reported
  nothing, and the drift it exists to surface stayed invisible. It now
  resolves the binary the way the hook already did (project venv first, PATH
  as fallback), honours `NEXUS_DB`, and prints an explanation instead of a
  blank when the graph or binary is missing.

  A push channel that silently emits nothing is worse than no channel, so
  `tests/test_push_channels.py` now **executes the shipped shell** — the
  command's `!` lines and the hook — against a real graph with `nexus`
  deliberately absent from PATH. Verified to fail against the 0.16.0 form.

## 0.16.0 — 2026-08-09

**Headline:** a new doc-drift gate (`dead_references`, MCP tools 39 → 40), a
`nexus setup` that treats the consuming project as a peer rather than a cache,
push channels for findings that must not be missed, a measured eval suite for
the instruction surface — and the mcp 2.x port that ends two months of shipping
a server that could not start.

**Upgrade note:** this release requires **`mcp>=2.0`**. Environments with an
editable or pinned older `mcp` must upgrade (`pip install "mcp>=2.0"`), or the
MCP server will fail at import. Installs of 0.15.0 and earlier that resolved
`mcp` 2.x have a non-functional server and should upgrade to this release.

### Changed — ported to the mcp 2.x server API

`mcp` 2.0.0 removed `mcp.server.fastmcp`. The previous release bounded the
dependency `<2` to stop shipping a dead server; this completes the move.

- `server.py` now builds on `mcp.server.mcpserver.MCPServer` (the `FastMCP`
  successor). The decorator surface is unchanged — `@_mcp.tool()`,
  `@_mcp.resource(uri)`, `Context`, `ctx.session` — so the 40 tools, 4
  resources and the journaling wrapper port without restructuring. Verified:
  `functools.wraps` transparency still holds, and `Context` parameters are
  still excluded from public tool schemas.
- **Breaking upstream rename**: `Tool.inputSchema` → `Tool.input_schema`.
  Only the registry drift-guard read it.
- Dependency floor is now `mcp>=2.0`, pinned to the import by a test — the
  floor and the import are one fact stored twice, and a future bump must
  move both.

### Added — the guards that would have caught this two months earlier

- **`tests/test_server_spawn.py`** launches the server as a real subprocess
  and completes a JSON-RPC handshake. Every other server test inspects the
  registry in-process, which is precisely why mcp 2.0.0 shipped a dead
  server undetected: the package imported fine, the CLI worked, and no test
  ever launched the process a client launches. Verified to fail when pointed
  at the removed module. Unmarked and unskippable — a test you can skip is a
  test that will be skipped on the day it matters.
- **`.github/workflows/upstream-deps.yml`** installs with the newest
  resolvable dependencies weekly and opens (or comments on) an issue when
  that fails. CI runs only on pushes and PRs, so a break arriving through
  the resolver rather than a commit is invisible by construction — this one
  sat from 2026-06-17 to 2026-08-09.

### Dead-reference detection — the silent doc-drift gate

A deleted or renamed symbol leaves its doc references behind, and Sphinx
renders them as plain text with **no warning at any severity**. The graph
already contained the signal (reference edges whose target reconciled to
nothing), but on ORPHEUS the bucket ran ~83% false positives. This release
makes it precise enough to gate on and surfaces it (MCP tools 39 → 40).
Validated against ORPHEUS with import-resolution as ground truth: every
reported Python target is either genuinely missing or a code-less
README-only namespace directory; zero false positives on real code, and
symbols that merely MOVED (old paths still referenced by unqualified
docstring refs) resolve to their new homes instead of being reported.

### Added

- **`dead_references` MCP tool + `GraphQuery.dead_references()`** — every
  doc/docstring/type-annotation reference whose project-rooted target no
  longer exists, with all referencing sites (file/line or docname).
  Decidability follows the ORPHEUS `check_docstring_xrefs` discipline: only
  project-rooted names are judged; external references are never reported.
  Three rescue passes guard precision: exact-name match, re-export chase,
  and an INHERITS walk (a member found on any ancestor is live; a class
  with an un-analyzed base is *undecidable*, never dead). Dead `:eq:`
  labels are audited too; `:math:` roles are ignored as presentation.
- **`staleness` now carries a dead-reference summary** (top 10 +
  total) alongside timestamp drift — dead references are the harder
  failure of the two, and they work without git or project_root.
- **Attribute and module-constant nodes.** The AST walker now emits
  `ATTRIBUTE` nodes for class-level `x: T = ...` / `x = ...` and
  `self.x = ...` bindings, and `DATA` nodes for module-level assignments —
  previously live `:attr:`/`:data:` references were indistinguishable from
  references to deleted code (176 of ORPHEUS's 212 referenced phantoms were
  this and the two fixes below).
- **Re-export alias map.** Module-scope `from X import Y` aliases are
  recorded (`graph.metadata["reexports"]`) and chased during phantom
  canonicalization and query-time rescue — `pkg.api.Thing` now folds onto
  `pkg.core.thing.Thing` even when the module paths don't overlap, which
  the leaf-name fold could never prove.

### Changed — skill steering, from a measured tool-selection eval

A 14-scenario headless battery (one `claude -p` session per scenario against
the real ORPHEUS graph, journaling every MCP call) measured whether an agent
reaches the *intended* tool from a natural-language symptom. Run on three
model tiers: **Opus 14/14, Sonnet 13/14, Haiku 12/14**.

⚠ **Read those numbers as a floor, not a result.** Those scenarios are
`direct`-style: they paraphrase the tools' own descriptions ("copy-paste
*twins* that have drifted", "the same implicit interface without a base
class"), so they largely measure keyword matching rather than routing
judgement — `dead_references` was reached even with **no instructions
installed at all**. The `indirect` and `proactive` scenario tiers added in
`evals/` are the ones that predict real use; see `evals/README.md`. The
skill changes below were still worth making (the A/B moved Haiku from 21
flailing calls to 1), but the headline scores are inflated by prompt design.

- **`nexus-exploring` gains a "Symptom → tool" table.** The
  architecture-smell family (`dead_functions`, `twin_paths`,
  `discriminations`, `protocol_conformers`, `native_place`,
  `bridges`/`communities`/`god_nodes`) and the whole `runtime_*` family
  existed only in `reference.md` tables — no SKILL.md body named them, and
  the skill description matched no smell-hunting phrasing.
- **`nexus-verification` documents the two kinds of doc drift**, and now
  names `dead_references`, `verification_gaps`, and `verification_audit` —
  previously absent from the skill that owns doc-drift questions.
- **`nexus-guide` routing table** covers smells and runtime.
- **Stale-line references removed** from the skills, matching the
  file-brief change above.

### Changed — instructions now name symptoms, not tools (measured)

Replicated ablation on Haiku (clean room from `nexus setup` only, real graph,
3 replicates/cell, zero permission denials): **instructed 3/6 vs bare 0/6.**
Eighteen bare runs produced zero correct tool selections, so no part of the
instruction surface can be trimmed on the theory that models already know.

The gap was that every table described tools by what they ARE ("copy-paste
twins that have drifted"), which only fires when the user already knows the
vocabulary. Nobody says "I suspect a twin path" — they say "two people built
this separately". `nexus-exploring` and the routing rule now carry:

- a **what-the-user-actually-says** table mapping complaints to tools
  ("we keep changing these classes in lockstep", "things live in surprising
  places", "the docs feel out of date");
- **sweeps that are part of the job, not a request** — after any delete or
  rename, before a release, and on any health-check or onboarding review.

Measured effect: `parallel-work` ("two people built these independently")
went 0/3 → **3/3** reaching `twin_paths`; `onboarding-health` → **3/3**, with
every replicate sweeping the entire smell family. Controls stayed clean in
both conditions — no compliance theater. Baseline recorded in
`evals/BASELINE.md` for comparison after the next model release.

### Added — push channels: inject the finding, don't hope it gets asked for

Steering an agent to *go looking* is probabilistic — measured here, whether a
tool gets reached depends on how the user happens to phrase the request, and
an agent nobody asks will never look. A dead documentation reference draws no
Sphinx warning at any severity, so if the agent doesn't look, **nothing**
reports it. For that class of finding, push beats pull.

- **`nexus dead-references` CLI** — the tool shipped as MCP-only, unlike every
  sibling (`twin-paths`, `dead-functions`, `staleness`), which also made it
  unreachable from the `!` and hook mechanisms. Adds `--format text` (a digest
  written to be read by an agent that did not ask for it: it leads with what
  the finding is and what to do about it), `--quiet-when-clean` (a clean
  project must cost zero context, or the channel trains agents to skim past
  it), and `--exit-code` to gate CI.
- **`/doc-health` slash command** — its `!` lines execute at invocation, so
  the findings are already in context. It does not tell the agent to run a
  tool; that would inherit the same probabilistic steering it exists to bypass.
- **`nexus-dead-refs.sh` hook** — wire to `SessionStart` (or `PostToolUse` on
  edits) so every session opens knowing the current dead references. Same
  quiet-exit-0 failure contract as the file-brief hook.
- `nexus setup` installs both, and marks hooks executable — a hook that lands
  non-executable fails silently at fire time, the worst failure mode for an
  ambient channel.

### Added — `nexus setup` treats the consumer as a peer, not a cache

Instruction files ship downstream and then evolve there against real
sessions. The old `setup` overwrote unconditionally with no record of what
it wrote, so a consumer's field-tested edit could be destroyed silently and
upstream had no way to see it. Setup is now a two-way channel:

- **Ships an always-on routing rule** (`.claude/rules/nexus-tools.md`):
  the question→tool table including when `grep`/`Read` is the *correct*
  choice, plus the deferred-`mcp__nexus__*` gotcha. Positive routing has to
  be always-on — a skill the agent never invokes cannot steer it. Skipped
  by `--no-rules`, and never installed by `--global` (a rule that
  auto-loads into every project must be a per-project choice).
- **`nexus setup --check`** — per-file state (missing / stale / locally
  modified / modified-and-stale), non-zero exit when anything needs
  attention, so it can gate CI.
- **`nexus setup --diff`** — what the consumer changed, printed shipped→
  installed so `+` lines are *theirs*. This is the harvest direction.
- **Locally-modified files are never overwritten** without `--force`,
  which still leaves a `.bak`. A local edit is often the better version.
- **A customized `.mcp.json` nexus entry is left alone** too. Setup used to
  rewrite it unconditionally to the default template — and a project that
  legitimately points the server elsewhere (another checkout's graph, a
  non-standard build dir, an absolute interpreter) would then have every
  query silently answer from a database that isn't there: the server starts
  fine, so there is no error to notice. This clobber invalidated 21 eval runs
  before it was caught. Other servers in the file are never disturbed, and an
  unparseable `.mcp.json` is left untouched rather than destroyed.
- Tracking is a **manifest** (`.claude/nexus-install-manifest.json`)
  recording the hash of the *shipped* content at install time — that is
  what separates "the consumer edited this" from "we shipped a new
  version". Stamping a version into the files themselves would edit the
  very content whose modification we are trying to detect.

### Added — harvested from the consuming project's skill evolution

Skills are shipped downstream by `nexus setup` and then *evolve there*
against real sessions. Diffing the consumer's copies against the shipped
ones found deliberate downstream development worth bringing upstream —
this direction of sync had never been done, and the shipped copies were
wrong in ways only real use reveals:

- **"Replaces Grep" is retired across all five skills** that carried it.
  The original strong language existed to fight a system-prompt directive
  (`ALWAYS use Grep for search tasks`) that **no longer exists** — the
  standalone Grep/Glob tools were removed from the probed scaffolds
  (verified 2026-06-14) and models route freely. The overclaim now costs
  precision instead of buying it, so it becomes "use Nexus for structural
  queries; use Grep freely for text search."
- **`behavioral-auto-regression` is demoted to a break-glass diagnostic**
  and carries the historical note above. Its old premise (reclassify code
  exploration as "not a search task") is obsolete. What replaces it: the
  dominant live cause of Nexus-avoidance is agents treating **deferred**
  `mcp__nexus__*` tools as unavailable — one `ToolSearch("select:…")`
  loads them — plus the opposite failure the old skill never named,
  over-using Nexus where a plain `Read`/`grep` was correct.
- **New `nexus-elegance` skill**: the map from each structural-review axis
  to the graph query that corroborates it, with a **false-positive table**
  — when a graph signal is NOT a finding (symmetric apply/adjoint pairs,
  retained test oracles, decorated indirectly-invoked functions,
  method-name-only protocol matches, stale-graph phantom provenance).
  This is the connective tissue the smell family lacked: the tools were
  discoverable but nothing said how to avoid false findings with them.
  The graph is a witness, never the accuser.
- **`nexus-guide` gains a route-by-question-shape table** including when
  grep/`Read` is the *correct* choice, and the deferred-tools note.

### Fixed — skill reference drift

- `reference.md` claimed "Tools (35)" against a registry of 40 and
  documented neither `node_at` nor `workspaces`/`use_workspace`. Added,
  corrected, and **pinned by a new drift-guard test** alongside the
  existing README↔registry guard.

### Changed — usage-evidence tunings (issue #15)

Seven weeks of real ORPHEUS sessions (806 journaled tool calls, 842 injected
edit-time briefs, zero tool failures) settled the deferred tuning decisions:

- **Ambient file-brief drops its staleness line.** The line fired on 842 of
  842 injected briefs — the hook runs post-edit, where "file changed since
  graph build" is tautologically true. The ``changed_since_build`` field
  stays on the dataclass/JSON.
- **Sibling-graph warning now fires only when a sibling's graph is FRESHER
  than the active one.** Existence alone produced 39 warnings against 4
  actual workspace switches. Siblings are still listed either way.
- **No token budgets added to `callers`/`callees`/`neighbors`/`graph_query`**
  — the journal shows no payload bombs (slow calls were non-transitive on
  ordinary nodes; the latency tail is database reload after a rebuild, not
  hub payloads).

### Fixed

- **Package-relative imports resolved one level short.** `ImportTracker`
  anchored every relative import at the module's parent — correct for
  `pkg/mod.py`, off by one inside a nested package's `__init__.py`, so
  `from .directional import Quadrature` in
  `orpheus/numerics/quadrature/__init__.py` resolved to
  `orpheus.numerics.directional.Quadrature` (2,326 poisoned aliases on
  ORPHEUS). This also silently mangled base-class and call-target
  resolution in nested-package `__init__` modules, fabricating
  dead-looking names for symbols that had merely moved.
- **Subscripted generic bases dropped.** `class Full(Composite[A, B])`
  produced no INHERITS edge, severing inherited-member resolution for
  every `Generic`-parameterized class.
- **Module-level docstrings were never scanned.** `visit_Module` skipped
  reference extraction entirely — ORPHEUS derivation modules keep whole
  `.. math:: :label:` derivations in module prose, invisible to the graph.
- **Equation labels defined in docstrings.** Sphinx only learns labels
  from pages it renders; the AST scanner now emits equation nodes for
  `.. math:: :label:` definitions in any docstring, so `:eq:` references
  to un-rendered derivations resolve.
- **Project-rooted names misclassified as `external`.** Both phantom
  classifiers checked installed packages before project membership, and the
  analyzed project is usually pip-installed in its own build venv — 333
  `orpheus.*` nodes sat in the external bucket, hidden from any gate.
  Module-typed graph nodes now define project membership and win.
- **Docstring role targets that wrap across lines** (`:class:`pkg.\n
  Thing``) parsed to phantom names containing newlines; both the
  `title <target>` form and plain dotted targets now normalize wrap
  whitespace, and un-parseable role bodies are skipped instead of forging
  unresolvable nodes.

## 0.15.0 — 2026-06-17

### Runtime overlay — dynamic execution-flow on the static graph (issue #26)

The static graph is *what can run*; a **runtime** overlay is *what actually
ran*. A new `runtime.py` ingests a trace of a canonical workload and overlays
it on the graph **by node-ID**, the dynamic counterpart to the static
"missing abstraction" family (`native_place` / `twin_paths` /
`discriminations`). Six MCP tools + `nexus runtime-*` CLI (MCP tools 33 → 39).

- **The join works — 97% on a real solve.** Trace records map onto static
  node IDs by `(file_path, lineno)`. The one gotcha: `cProfile`'s
  `co_firstlineno` points at the first *decorator* line while the AST records
  the *def* line, so a naive range check drops every decorated function /
  property — a decorator-window rule fixes it (measured 68% → 97%; the
  residual is lambdas/closures that by design have no node).
- **Sidecar, never `graph.db`.** Runs live in `_nexus/traces/<run>.json`
  (one JSON each) and re-bind to the live graph at query time — so they
  survive the `sphinx-build` rebuild that regenerates `graph.db`.
- **`runtime_ingest`** — `cProfile`/`pstats` (counts + self/cumulative time +
  call edges) or `coverage json --branch` (line/branch coverage). Metrics
  aggregate by node-ID (a node may own several code objects); `source_prefix`
  drops stdlib/third-party frames.
- **`runtime_hotspots`** (`by` = cumtime/ncalls/tottime) — the dominant
  *observed* call chain (the dynamic stage DAG, better than `processes`'
  static heuristic for a traced run) and the iteration-count / recompute
  smell.
- **`runtime_edges`** (`mode` = dynamic_only/fired/dead) — `dynamic_only`
  recovers the dispatch the static resolver can't see: annotation-mediated
  dispatch through `self`/typed locals (issue #16) and the resolved face of
  polymorphism (which concrete impl actually ran). On a real ORPHEUS SN solve
  this surfaced `_OneDimScanWalk._apply_walk` dispatching to
  `DiamondDifference` / `MorelMontryAngularSweep` ×10,992 — edges with **zero**
  static counterpart. `dead` is dead *in this run* (union runs for a verdict).
- **`runtime_branches`** (a `coverage --branch` run) — nodes that didn't take
  every conditional outcome, with those that also `discriminates_on` a tag
  ranked first: a discrimination always taken one way is a missing type, the
  dynamic counterpart of `discriminations`.
- **`runtime_runs`** — list ingested runs.

Phase 3 rounds out the overlay (still 0.15.0):

- **Multi-run union** — the query tools accept comma-separated run names and
  union them into the canonical-suite aggregate (`merge_runs`). `dead` then
  means fired in NO run — the real cross-suite dead-code signal that
  corroborates the static `dead_functions` — and a branch is *missing* only if
  no run ever took it (the still-missing arcs are the intersection).
- **Edge classifier** — `runtime_edges(substantive_only=True)` drops edges
  where either endpoint is a property / trivial accessor, so the polymorphic
  dispatch (the #16 payoff) is no longer buried under property-getter call
  edges, which dominate `dynamic_only` raw. A `@property`/`@cached_property`
  is an accessor by construction; a conservative ≤2-line-body fallback catches
  undecorated one-liner getters.
- **viztracer backend + `runtime_timeline`** — a third capture kind
  (`kind=viztracer`) keeps temporal order: call-stack depth is reconstructed
  by interval nesting, and `runtime_timeline` returns nodes in order of first
  entry — the observed stage sequence (mesh → discretize → sweep → iterate →
  result) — with a `max_depth` filter for just the high-level stages.

## 0.14.0 — 2026-06-17

### `dead_functions` + `protocol_conformers` diagnostics

Two more read-only diagnostics in the "missing abstraction" family, both over
existing edges (no re-analyze): `dead_functions` / `nexus dead-functions` and
`protocol_conformers` / `nexus protocol-conformers` (MCP tools 31 → 33).

- **`dead_functions`** — functions/methods with no static callers (zero
  incoming `calls` from non-test, non-excluded code) = dead-code candidates.
  A **candidate list, not a verdict**: dynamic dispatch (registry /
  `getattr` / callbacks) is invisible to the static call graph, and public
  entry points are legitimately uncalled internally. Dunders are excluded
  (invoked implicitly); each result carries `public` + `decorated` flags for
  the false-positive sources, and a private/undecorated/plain function with no
  caller — the strongest signal — is ranked first.
- **`protocol_conformers`** — classes that define (by name) every non-dunder
  method a `Protocol` declares yet do not inherit it. `Protocol`s are
  satisfied *structurally*, but the `inherits` edge records only explicit
  subclassing, so a structural conformer has no edge — "is every
  implementation connected to its Protocol?" is otherwise unanswerable. A
  method-NAME-set heuristic (signatures ignored, direct methods only); the
  authoritative check is a type checker (pyright / LSP `goToImplementation`).
  On ORPHEUS it cleanly recovers the `LinearOperator` Protocol's conformers
  (operators that inherit a mixin, not the Protocol).

### `discriminates_on` edge + `discriminations` diagnostic

New AST edge type **`discriminates_on`** (`function → tag`) and the
`discriminations` MCP tool / `nexus discriminations` CLI command. Makes the
coding-elegance smell *"a repeated conditional is a missing type —
discriminate once, at the boundary"* machine-checkable.

- The extractor emits `function --discriminates_on--> tag` whenever a function
  branches on a string/enum **tag**: `if x == "lit"` / `x == Enum.MEMBER`,
  `x in ("a", "b")`, or `match x:` over literal/enum/class patterns, where `x`
  is a name or attribute (the leaf name keys the tag, so `self.geometry` and
  `mesh.geometry` share one `tag:geometry` node). Synthetic `tag` nodes are a
  new node type; the matched case labels ride on the edge as `cases`.
- One edge records one site; **repetition is counted by the query** —
  `discriminations(min_sites=2)` ranks tags by how many distinct functions
  discriminate on them (the smell: one dispatch / type should replace the
  repeated tests). The raw edge is also queryable via
  `graph_query "function -discriminates_on-> tag"`.
- Validated on ORPHEUS: `geometry`/coord discriminated across 13 sites, plus
  stringly-typed solver-dispatch tags (`inner_solver`, `inner_schedule`)
  surface as multi-site fan-in.
- Re-analyze required (built during AST analysis). Edge-types schema 12 → 13.

### `twin_paths` — twin-path / clone diagnostic + AST body fingerprint

New read-only `GraphQuery.twin_paths()`, exposed as the `twin_paths` MCP tool
and the `nexus twin-paths` CLI command. Surfaces **twin paths** — two
functions whose bodies independently implement the same computation (a
Type-2/3 clone), the coding-elegance Pattern-2 / single-source-of-truth smell.

- Backed by a new **AST body fingerprint** (`sphinxcontrib/nexus/fingerprint.py`):
  each function/method node now carries normalized k-gram structural shingles
  (`body_shingles`) + a token count (`body_ntokens`) in its metadata, stamped
  by the extractor. Identifiers and literals are normalized (rename-invariant,
  Type-2), while operators (`@`), attribute/method names (`einsum`, `solve`)
  and subscripting are kept — capturing the array math the **call graph cannot
  see** (operator overloads and indexing produce no call edges).
- Pairs are ranked by descending Jaccard shingle overlap, cross-module first.
  Functions that directly call each other are dropped (delegation, not an
  independent reimplementation); a minimum-token gate removes thin stubs.
- Validated on a real graph (ORPHEUS): found genuine cross-module duplicates
  (a per-axis cosine accessor reimplemented across modules at similarity 1.0;
  a BC-registry resolver copied across two solvers at 0.9) while leaving
  symmetric-by-design pairs (`apply`/`apply_transpose`) for human judgment.
- Knobs: `min_similarity` (default 0.7), `min_tokens` (35), `exclude`, `limit`.

### `native_place` — Feature-Envy / "native place" diagnostic

New read-only `GraphQuery.native_place_candidates()`, exposed as the
`native_place` MCP tool and the `nexus native-place` CLI command. Surfaces
module-level functions whose every non-test caller is a method of a SINGLE
class — candidates to move into that class.

- Ranked lexicographically by descending strength: genuine relocations
  before tested free-primitives, then **cross-module** before same-module,
  private before public, fewer excluded (test) callers, and finally more
  single-class callers (stronger coupling) as a tiebreak.
- Derived flag **`likely_free_primitive`** — a *public* function tested at
  least as much as it is used in production (`excluded_callers >=
  caller_count`) is an independently verified free-function primitive that
  is *correctly* free, not a relocation candidate. Such rows are kept but
  ranked last, so the suppression is explicit rather than implicit in the
  numbers. Private helpers never flag (a private symbol used by one class is
  a genuine relocation signal regardless of test coverage).
- Test callers are recognised via the `is_test` node flag (from
  `nexus_test_patterns`), excluded from the single-class criterion, and
  reported as `excluded_callers`.
- Knobs: `min_callers`, `exclude` (extra substrings on top of `is_test`),
  `limit`.

## 0.13.0 — 2026-06-12

Token-budgeted tool outputs and the LSP↔graph parity oracle.

### Token budgets for `context` and `impact`

Measured on a real production graph (ORPHEUS): `context` on a
degree-3429 hub node serialized to **2.7 MB** of JSON and depth-3
`impact` to 1–2 MB — far beyond what an MCP tool consumer can read,
and enough to blow an agent's context outright.

- `assemble_context(per_type_limit=25)`: per-edge-type buckets sorted
  most-connected-first, capped, with an honest `omitted` block
  (per-bucket drop counts + escape-hatch hint) present only when
  something was dropped. Entries are now compact node dicts — in the
  grouped view the edge dict was pure redundancy (type = bucket key,
  direction = outgoing/incoming key, endpoints = queried node + the
  entry); `neighbors` keeps the flat node+edge view. Empty sentinel
  fields (`""`/`0`) are dropped from bulk entries (~25% of payload).
- New `assemble_impact(per_depth_limit=50)` — single source for MCP
  and CLI; `total_affected` is ALWAYS the true traversal count.
- MCP: `context(limit_per_type=25)`, `impact(limit_per_depth=50)`,
  `0` = uncapped. CLI: `--limit-per-type` / `--limit-per-depth` with
  `... (+N more)` markers.
- Result: hub context 2716 KB → 12 KB (226×); hub impact ~2 MB →
  ~67 KB.

### LSP↔graph parity oracle (`tests/test_lsp_parity.py`)

Pyright re-derives symbols and call edges through a fully independent
implementation; the oracle asserts the AST analyzer's graph agrees.
The 51%-worktree-contamination bug class from 0.12.0 would now be
caught automatically. Per-file `documentSymbol` set-EQUALITY (the
analyzer's deliberate granularity — closures excluded — is encoded in
the reducers, not a weakened assertion); `incomingCalls` vs graph
`callers` with static calls exact and dynamic dispatch directional
(graph ⊆ pyright). Finding: the analyzer resolves same-class
`self.method()` dispatch (now pinned), so the enrichment gap (#F4) is
only annotation-mediated dispatch. Skips without `pyright-langserver`;
runs in the CI pyright job.

### Notes

- README documents the `verification_coverage` tier-calibration
  caveat (closes #5 per its own option 4): measured 2 of 972
  test-bearing entries (0.2%) carry multihop-only credit; the remedy
  is an explicit `verifies` marker on driver tests, not heuristic
  tuning.
- 442 tests; `pyright sphinxcontrib/` 0/0.

## 0.12.0 — 2026-06-11

Git-worktree (workspace) support. A graph database is a snapshot of
ONE checkout, but agent harnesses (Claude Code) spawn the MCP server
against the MAIN checkout and never restart it when a session moves
into a worktree — so worktree sessions silently query the wrong
branch's graph. Observed in production: the main checkout's graph was
26 days stale while the active worktree rebuilt its own graph on
every docs build; every session answered from the stale one. This
release makes the mismatch visible and switchable.

### Added

- **Provenance stamping.** Every graph-write site (the Sphinx
  ``build-finished`` handler and ``nexus analyze``) now stamps
  ``graph.metadata["provenance"]`` with ``source_root``, ``built_at``,
  and — when the tree is a git checkout — ``git_branch``,
  ``git_commit`` (short), ``git_dirty``. Graphs are self-describing:
  any consumer can tell which tree, branch, and commit a database is
  a snapshot of. Non-git trees still get ``source_root``/``built_at``.
- **``workspace`` module.** ``Workspace`` (a checkout paired with its
  graph database via a root-relative layout), ``discover()``
  (enumerate all checkouts via ``git worktree list --porcelain`` and
  report each one's graph status, branch, and provenance),
  ``git_provenance()``, ``list_worktrees()``, ``stamp_provenance()``.
  All git access is subprocess-based and failure-tolerant: missing
  git / non-repository roots degrade to "active workspace only",
  never to a tool-call exception.
- **``read_sqlite_metadata()``** in ``export`` — metadata-table-only
  peek (no node/edge loading) so discovery can read provenance from
  every sibling database cheaply. Deliberately not gated on
  ``schema_version`` (the metadata table is where the version itself
  lives); ``load_sqlite`` now delegates its metadata pass to it.
- **MCP tool ``workspaces``** — list every checkout (main + linked
  worktrees) with branch, graph presence, build time, provenance,
  and which one is active.
- **MCP tool ``use_workspace(root)``** — atomically re-point the
  server at the graph built inside another checkout. Safe because
  each agent session owns its server process (verified: Claude Code
  spawns one ``nexus serve`` per session). Fails loud with a
  build-it-first hint when the target checkout has no graph; the
  active graph is untouched by a failed switch. Auto-reload tracks
  the new database afterwards; a workspace-switch guard inside the
  reload lock prevents a stale pre-lock ``stat`` from clobbering a
  freshly switched graph. Accepts a worktree directory name or a
  branch name in addition to an absolute root path
  (``workspace.resolve_checkout_root``); unknown or ambiguous names
  fail with the list of known checkouts.
- **``session_briefing`` workspace block** (MCP tool and
  ``nexus://briefing`` resource). Reports the active workspace's
  provenance and sibling checkouts, with warnings when (a) the graph
  carries no provenance stamp, (b) the graph was built on a different
  branch than the checkout now has, or (c) sibling worktrees carry
  graphs of their own — the wrong-tree tripwire fires on the
  session's first turn instead of never.
- **CLI ``nexus workspaces``** — same discovery, human-readable.
- **MCP tool ``node_at(file, line)``** — map a file position to the
  innermost enclosing graph node (module-scope positions return the
  module node). The bridge from position-speaking tools — language
  servers, stack traces, editors — into the graph: resolve a symbol
  precisely with LSP, then hand its position here and continue with
  ``context`` / ``impact`` / ``provenance_chain`` for the
  cross-domain picture LSP cannot see. Tool count is now 28.
- **CI runs pyright** alongside the pytest matrix (which now includes
  Python 3.14); the type check builds the same ``./.venv`` layout
  ``pyrightconfig.json`` points at locally.
- **Usage journal.** Every MCP tool call appends one JSON line
  (timestamp, tool, args repr-truncated, duration, outcome, active
  workspace, pid) to ``~/.nexus/usage.jsonl`` — ``NEXUS_USAGE_LOG``
  overrides the path, an empty value disables. The self-observation
  channel: tool adoption gets evaluated from recorded behavior instead
  of anyone's memory. Journaling is failure-tolerant and never blocks
  a tool call; the registration wrapper is schema-transparent
  (guarded by test).
- **behavioral-auto-regression skill updated to the current prompt
  landscape**: the historical Grep ``ALWAYS`` directive is gone from
  current agent prompts; the live regression vectors are habit
  (counter: claim the "dedicated tools" category), search delegation
  to generic grep-wielding sub-agents (counter: project explorer
  agent + deny built-in Explore), and deferred MCP tools (counter:
  ToolSearch loading — deferral is not unavailability).
- **Roots-based workspace auto-alignment.** ``session_briefing`` asks
  the client (MCP ``roots/list``) which directory the session was
  launched from; when that lies inside a different checkout that has
  a graph, the server switches to it automatically and reports the
  switch under ``workspace.auto_align`` (or the build-it-first hint
  when the checkout has no graph). Failure-tolerant: clients without
  roots support, foreign paths, and already-aligned sessions all
  degrade to "no block, no switch". Verified over real MCP stdio
  against ORPHEUS: a roots-advertising client launched in a worktree
  gets that worktree's graph on its first briefing with no manual
  call; mid-session worktree entry still uses ``use_workspace``
  (roots updates there are undocumented client behavior).

### Fixed

- **AST analysis no longer ingests nested git working trees.**
  ``analyze_directory`` prunes any subdirectory carrying a ``.git``
  entry (gitlink file = linked worktree / submodule, directory =
  vendored clone); the analyzed root itself is exempt. Found while
  end-to-end-testing this release on ORPHEUS: the main checkout's
  graph contained 30,049 nodes of which **15,420 (51%) were
  worktree copies** (``py:attribute:.claude.worktrees.<name>.orpheus...``)
  — every Claude Code session worktree's full source tree was being
  re-analyzed under mangled module paths, polluting query results,
  caller counts, impact analysis, and god_nodes. The clean rebuild
  matches the worktree-side build node-for-node class. Re-include a
  nested tree deliberately via ``nexus_extra_source_dirs`` if you
  ever need one analyzed.

- **MCP ``impact`` / ``neighbors`` validate ``direction`` at the tool
  boundary** — an invalid value now returns a self-describing error
  payload instead of leaking a bare string into ``Literal``-typed
  query internals.
- **Branch-scope diffs resolve the repository's actual default
  branch.** ``detect_changes`` / ``retest`` / ``session_briefing``
  with ``scope="branch"`` used a hardcoded ``main``-then-``master``
  fallback that conflated "ref does not exist" with "no .py files
  changed" and never saw unconventionally named defaults. The base is
  now ``workspace.default_branch()``: the ``origin/HEAD`` symbolic
  ref when set, else the first of ``main``/``master`` that exists.
- **Edge-key collision when wrapping an existing graph.**
  ``KnowledgeGraph`` now accepts an existing ``nx.MultiDiGraph`` and
  continues the auto-incremented edge-key sequence past its highest
  integer key; previously both wrap sites (``dict_to_graph`` and the
  MCP ``ingest`` tool's private-attribute poke) reset the counter to
  0, so a later ``add_edge`` between an already-connected pair could
  silently UPDATE an existing parallel edge instead of adding one.
  ``GraphQuery`` keeps the ``KnowledgeGraph`` it was built from
  (``knowledge_graph`` property, metadata included) so the ``ingest``
  tool mutates the real object instead of reconstructing a wrapper.

### Changed

- **Repo is pyright-clean** (``pyrightconfig.json`` points at the
  project venv; CI-checkable). Fixed along the way: a quoted
  forward-reference in ``cli.py``, ``VerificationGapsResult.filters``
  typing (it carries an ``int`` count), edge-attribute restoration in
  ``export.load_sqlite`` now goes through ``g.edges[u, v, key]``,
  ``_add_docstring_refs`` declares the actual ``ast.get_docstring``
  domain instead of ``ast.AST``.
- **Server state model.** The four smeared module globals
  (``_db_path``, ``_project_root``, ...) collapse into one named
  concept: ``_workspace: Workspace``. ``serve()`` resolves its paths
  at startup.
- **``nexus setup`` MCP config template** now anchors ``command``,
  ``--db``, and ``--project-root`` on ``${CLAUDE_PROJECT_DIR:-.}``
  instead of bare relative paths — Claude Code sets that variable for
  spawned MCP servers, so resolution no longer depends on the
  (unspecified) spawn cwd; the ``:-.`` fallback keeps other MCP
  clients working.
- ``nexus serve --help`` no longer hardcodes a tool count.
- **Version is single-sourced** from ``__init__.__version__``;
  ``pyproject.toml`` declares ``dynamic = ["version"]`` (flit extracts
  the literal at build time). Previously the two copies had to be
  bumped in lockstep — pure drift surface.

## 0.11.0 — 2026-04-14

Public escape hatch for downstream projects that need to keep
directories out of AST analysis without monkey-patching private
internals. Closes #13.

### Added

- **``nexus_source_exclude_patterns``** Sphinx config value
  (default ``[]``). POSIX glob patterns evaluated with ``fnmatch``
  against paths relative to each source directory — same semantics
  as ``nexus_test_patterns``. Patterns are appended to the
  exclusion list passed to ``analyze_directory`` for both the main
  source pass and the ``nexus_extra_source_dirs`` pass.

  Concrete motivating case (ORPHEUS): ``student_resources/``
  contains pedagogical scripts that intentionally shadow
  ``orpheus.*`` class names. Sphinx's py-domain xref resolver was
  matching the short names against both the real package and the
  tutorial copies, so the AST extractor recorded ``documents``
  edges to both — which then made the staleness tracker count the
  tutorial file's mtime against every API page that documented an
  affected ``orpheus`` module. With this option, downstream
  projects can drop the shadowing source out of analysis from
  ``conf.py`` directly:

  ```python
  nexus_source_exclude_patterns = ["student_resources/*"]
  ```

  No more reaching into ``_BASE_EXCLUDE_PATTERNS``.

### Changed

- **``_compute_exclude_patterns``** gained an optional
  ``user_patterns`` parameter (default ``None``). When provided,
  the patterns are appended unconditionally — independent of the
  ``analyze_tests`` gate, so user excludes still apply when tests
  are being analyzed. Pre-0.11 callers that pass only positional
  ``analyze_tests``/``test_patterns`` continue to work unchanged.

## 0.10.0 — 2026-04-14

LLM-orientation pass on ``session_briefing``. Three additive fields
teach the agent the node-ID grammar on the first turn, surface the
handful of nodes most likely to be queried next, and emit a paste-
ready ``ToolSearch`` invocation for Nexus's deferred MCP tools.
Drop-in upgrade from 0.9.0: no existing field was removed or
re-shaped.

### Added

- **``id_grammar``** in ``BriefingResult``. For each
  ``(domain, type)`` pair actually present in the graph (excluding
  the noise types ``external`` and ``unresolved``), emits one
  representative node with the median degree in that bucket.
  Max-degree nodes are already in ``god_nodes``; min-degree nodes
  are obscure; the median is the useful teaching example. Examples
  are sorted by ``(domain, type)`` ascending and are deterministic
  across calls on an unchanged graph.
- **``hot_nodes``** in ``BriefingResult``. Nodes that (a) appear in
  ``recent_changes`` (same data ``session_briefing`` already uses,
  i.e. ``detect_changes(scope="branch")`` against main/master — no
  separate window), (b) have degree at or above the graph median
  so "hot" implies both recent *and* central, and (c) are not
  already in ``god_nodes[:5]`` (to avoid duplicating the signal
  that field already carries). Top 5 by degree, tiebreak on id.
  Each entry carries a free-form ``reason`` drawn from a small
  stable vocabulary (``"modified in current branch"``, etc).
- **``preload_hint``** in ``BriefingResult``. A static, graph-
  independent ``select:`` string listing the eight most-used Nexus
  MCP tools (``query``, ``callers``, ``callees``, ``context``,
  ``impact``, ``provenance_chain``, ``shortest_path``,
  ``neighbors``). Paste it into a single ``ToolSearch`` call on
  the first turn that touches Nexus instead of loading schemas
  one tool at a time.

All three fields flow through the existing ``to_dict`` /
``asdict`` path as nested dataclasses, so ``session_briefing`` MCP
and CLI responses pick them up automatically — no serializer
changes required.

### Round-trip contract verified

Before wiring ``id_grammar``, ran an empirical probe that walked
every ``god_nodes`` entry from a realistic briefing and fed its
``id`` back into ``assemble_context``. All five round-tripped
cleanly — confirming that ``NodeResult.id`` (the
``<domain>:<type>:<name>`` graph key) is directly accepted by
``context`` and, by extension, every other MCP tool that takes a
node id. The contract that ``id_grammar.examples[*].id`` is
usable verbatim now has a test
(``test_briefing_id_grammar_round_trip``) pinning it.

## 0.9.0 — 2026-04-13

Session 4 of the ORPHEUS V&V integration: infrastructure hardening.
No blocking workflow changes — this is QoL, correctness, and
operator-debuggability work built on top of the v0.8.x behavior.
Drop-in upgrade from 0.8.2.

### Added

- **SQLite schema version field** (``SCHEMA_VERSION = 1``) written
  on every ``write_sqlite`` call into the ``metadata`` table.
  ``load_sqlite`` validates it via ``_check_schema_version`` and
  raises ``SchemaVersionError`` when the stored version exceeds
  this build's ``SCHEMA_VERSION``. Missing key is tolerated (pre-
  schema_version databases are treated as v1). A user-supplied
  ``schema_version`` in ``graph.metadata`` cannot override the
  authoritative value.
- **V&V integration docs** in the README walk through the full
  declarative-verification pipeline end-to-end: pytest markers →
  AST metadata → TESTS/IMPLEMENTS edges → audit/gaps queries.
  Includes copy-paste examples for each of the four declaration
  paths (markers, directives, registry YAML, query consumption).
- **Parallel-build regression test**:
  ``test_parallel_build_matches_serial`` in the fixture harness
  runs a ``sphinx-build -j 2`` against ``minimal_project`` and
  pins that it produces the same node set, edge count, and edge-
  type distribution as a serial build. The extension has always
  declared ``parallel_write_safe=True`` but the claim was never
  load-bearing on a test until now.

### Changed

- **``_SPHINX_ROLE_RE`` / docstring-role parser hardened.**
  Introduces ``_parse_role_target`` that normalizes the raw
  backtick content into the resolvable target, handling four
  cases the old parser missed:

  1. ``:role:`!foo``` (suppress-link convention) → returns
     ``None``, no edge emitted.
  2. ``:role:`title <target>``` → returns ``target`` (display
     title is presentation noise).
  3. ``:role:`~pkg.mod.foo``` → strips the tilde and returns the
     dotted name. The tilde inside a title-target form is also
     handled.
  4. Plain ``:role:`foo``` → returned as-is.

  Before this change, ``:func:`compute fn <pkg.mod.compute>``
  produced a target id of ``py:function:compute fn <pkg.mod.compute>``
  — unresolvable garbage — and ``:func:`!noref`` created a
  ``py:function:noref`` edge despite the suppression intent.

- **``_reload_if_stale`` is now thread-safe and failure-tolerant.**
  Wraps the ``load_sqlite`` call in a try/except so a corrupt
  DB, schema-version rejection, or mid-write race keeps the
  previous in-memory snapshot serving instead of crashing the
  MCP tool dispatch. A module-level ``threading.Lock`` serializes
  concurrent reload attempts; a double-check of the mtime under
  the lock avoids redundant loads. Failure cases log at WARNING
  level with the DB path and raised exception.

### Notes

- 294 → 316 tests (+22). Split across:
  - ``test_export.py`` (+6 schema version)
  - ``test_ast_analyzer.py`` (+9 role-target parse + end-to-end)
  - ``test_fixture_e2e.py`` (+1 parallel-build equivalence)
  - ``test_reload.py`` (new file, 6 reload failure / lock tests)
- No API or schema changes. Session 4 is pure hardening on top
  of 0.8.2 behavior.

## 0.8.2 — 2026-04-13

Fixes nexus#3 — re-exported classes appearing as multiple parallel
graph nodes. Two-round fix after the first-round implementation
was flagged by ORPHEUS cross-validation as regressing the
canonical class type. ORPHEUS reported ``Mesh1D`` showing up as four
distinct nodes in the 0.6.0 graph (two ``py:class:``, one
``py:function:``, one unresolved phantom). This release collapses
all four bug shapes into a single canonical class via a new
leaf-name-plus-path-overlap fold pass.

### Fixed

- **nexus#3** — ``analyze_directory`` now runs a new
  ``_canonicalize_phantoms`` pass after ``_classify_phantom_nodes``
  that folds re-export and mis-typed phantoms into their canonical
  AST counterparts. The pass:

  1. Builds a leaf-name index over every concrete
     class/function/method node.
  2. For each phantom (``unresolved``/``external``/untyped with a
     dotted name), looks up the leaf name and filters candidates
     to those whose module path overlaps the phantom's via a
     prefix OR suffix relationship.
  3. If exactly one candidate survives, retargets all incoming
     and outgoing edges onto the canonical and drops the phantom.

  The module-path-overlap guard is what distinguishes "re-export
  or short-import of the same symbol" from "genuine external
  leaf-name collision". A reference like ``numpy.ndarray`` does
  NOT fold into a project-local ``local.ndarray`` because
  ``numpy`` is neither a prefix nor a suffix of ``local``; but
  ``pkg.geometry.Thing`` DOES fold into
  ``pkg.geometry.mesh.Thing`` because the former's module path
  is a prefix of the latter's.

  All four ORPHEUS bug shapes are handled by the single pass:

  - ``py:class:orpheus.geometry.Mesh1D`` (re-export via __init__)
    — folded via prefix overlap.
  - ``py:function:orpheus.geometry.Mesh1D`` (class called as
    Call, hardcoded ``py:function:`` prefix) — folded via prefix
    overlap.
  - ``py:class:geometry.mesh.Mesh1D`` (short-import phantom from
    test files that put the project root on ``sys.path``) —
    folded via suffix overlap.
  - ``py:class:orpheus.geometry.mesh.Mesh1D`` (canonical) —
    untouched; remains the single surviving node.

### Scope note

The handoff listed an optional ``nexus_package_aliases`` config
for projects with weird import layouts. The leaf-name-plus-
overlap rule already handles every bug shape the ORPHEUS repro
exhibited (including the short-import case via the suffix-match
branch), so the config isn't needed. Leaves the API smaller; can
be added later if a real project hits a case this pass can't
resolve.

### Round 2 — type upgrade during merge and type-ranked fold

ORPHEUS cross-validation of the first-round fix found three
interacting bugs:

1. **``merge_graphs`` didn't upgrade types.** When Sphinx had a
   placeholder ``py:class:pkg.mod.Thing`` with
   ``type=unresolved`` (from a pending_xref that couldn't
   resolve at parse time, or from NetworkX auto-creating an
   edge target before domain extraction ran) AND the AST side
   had the same id typed as ``class`` with ``file_path``, the
   merged node kept ``type=unresolved``. Downstream type
   filters broke and the canonicalization leaf-index skipped
   the canonical.
2. **The fold's canonical recognition was too strict.** Even
   after merge was fixed, a node whose ID prefix and
   ``file_path`` proved a concrete type could still be
   bypassed if some earlier classification step had stamped
   its type attr as ``unresolved``. The leaf-index only looked
   at the type attr, so such nodes weren't considered canonical.
3. **Bare-name phantoms and same-leaf ambiguity.** Phantoms
   with a bare leaf name (e.g. ``py:function:Mesh1D`` from a
   ``from pkg import Mesh1D`` call site) had no module path to
   feed into the overlap filter and were always skipped. And
   when multiple canonical candidates shared a leaf, the fold
   picked by iteration order instead of by concreteness.

Round-2 fixes:

- ``merge_graphs`` step 1 consults a ``_MERGE_TYPE_RANK`` table
  and upgrades the type whenever AST's type is more concrete
  than Sphinx's. ``class > exception > method > function >
  type > attribute > data > module > external > unresolved``.
  Downgrades are explicitly protected against.
- ``_canonicalize_phantoms`` runs a new
  ``_upgrade_types_from_signals`` pre-pass that inspects every
  node whose ID prefix (``py:class:``, ``py:function:``,
  ``py:method:``, …) plus ``file_path`` signals an
  authoritative concrete type, and upgrades the type attr in
  place. Bare phantoms without ``file_path`` are untouched.
- The fold's canonical-selection step now picks by concreteness
  rank tie-broken on ``file_path`` presence. Genuinely
  ambiguous leaves (two candidates tied on both signals) are
  left alone — no auto-collapse.
- Bare-name phantoms (no dots in name) now fold into the unique
  leaf-matched canonical across the whole graph without the
  module-path-overlap filter.
- ``_run_ast_analysis`` invokes ``_canonicalize_phantoms`` a
  second time after the last ``merge_graphs`` call, so Sphinx-
  side phantoms that the per-directory AST pass couldn't see
  get collapsed against their merged canonicals. The pass is
  idempotent.

### Fixture expansion

``tests/fixtures/minimal_project/conf.py`` now enables
``sphinx.ext.autodoc`` and ``theory/solver.rst`` runs
``.. autoclass:: solver_pkg.helpers.Mesh`` plus
``.. autofunction::`` blocks for the solver functions. This
ensures the e2e harness exercises the full Sphinx
domain-objects → AST merge → canonicalization pipeline that the
ORPHEUS build runs through, not just the AST-only path.

### Tests

281 → 294 (+13). New regression coverage split across:

- ``test_reexport.py`` (8 assertions total) — pins every bug
  shape from both round 1 and round 2 in isolation:
  - round 1: synthetic 3-level re-export project
  - round 2: ``test_canonical_with_unresolved_type_but_file_path_is_foldable_target``
    simulates the ORPHEUS shape A, ``test_bare_name_phantom_folds_to_unique_canonical``
    pins shape C1.
- ``test_merge.py`` (+2 assertions) —
  ``test_merge_upgrades_placeholder_type_from_ast`` pins the
  merge-layer type upgrade, ``test_merge_does_not_downgrade_concrete_type``
  guards the inverse.
- ``test_fixture_e2e.py`` (+3 assertions from round 1) — pins
  the round-1 re-export shape end-to-end through a real
  ``sphinx-build``.

## 0.8.1 — 2026-04-13

Two bug fixes caught by ORPHEUS cross-validation of 0.8.0.

### Fixed

- **nexus#7**: explicit-source edge dedup. Every write-time pass
  (``merge.write_verifies_edges``, ``directives.apply_pending_edges``,
  ``registry._apply_verifications`` / ``_apply_implementations``)
  previously only skipped duplication against its **own** source.
  A ``(test, equation)`` pair declared by both a
  ``@pytest.mark.verifies`` decorator AND a matching registry
  entry therefore produced two parallel ``tests`` edges, inflating
  per-equation test counts by one (the exact 86 → 87 regression
  reported from the ORPHEUS ``matrix-eigenvalue`` equation).

  All four passes now skip if ANY edge of the same type has a
  non-inference source already present. ``source="inferred"``
  edges remain weak and can still coexist with explicit assertions.

- **Query-time dedup layer** in ``verification_coverage`` tracks
  seen ``(src, tgt)`` pairs per edge-type so each ``(code,
  equation)`` or ``(test, equation)`` relationship contributes at
  most one entry to the result. This is defense-in-depth for
  graphs loaded from older nexus versions that may still carry
  duplicate edges.

- **nexus#8**: module/class-level ``pytest.mark.*`` propagation
  now requires the target function to qualify as a test. Previously
  a module ``pytestmark = pytest.mark.verifies("eq-1")`` tagged
  **every** function in the file — including private helpers like
  ``_build_homogeneous_mesh`` — and ``write_verifies_edges`` then
  wrote spurious ``tests`` edges from those helpers to the
  equation. ORPHEUS's declared coverage inflated by ~5-10% because
  of this. Inherited markers are now gated on ``is_test=True``
  (name matches ``test``/``test_*`` AND the file matches the
  project's test-pattern globs). Function-level decorators still
  apply unconditionally.

### Notes

- 272 → 281 tests (+9). New assertions split across
  ``test_registry.py`` (+4 write-time dedup), ``test_query.py``
  (+2 query-time dedup), and ``test_ast_analyzer.py`` (+3
  helper-propagation regressions).
- No API or schema changes. Drop-in upgrade from 0.8.0.
- The ``_visit_source`` helper in ``tests/test_ast_analyzer.py``
  gains an ``is_test_file`` parameter so Session 2 propagation
  tests can keep exercising their contract under the tighter gate.

## 0.8.0 — 2026-04-13

Session 3 of the ORPHEUS V&V integration: non-LLM verification
registry, Sphinx directives for declarative edges, and extended
audit/gaps surface.

### Added

- **Non-LLM verification registry** (``sphinxcontrib.nexus.registry``).
  A deterministic YAML-driven path for declaring ``TESTS`` and
  ``IMPLEMENTS`` edges independent of the LLM-powered
  ``ingest.py``. Schema is ``version: 1`` with ``verifications``
  and ``implementations`` lists; each entry names a test or function
  id and the equation labels it covers. Registry edges are tagged
  ``source="registry"`` with confidence 1.0 and honored by the
  ``_infer_implements`` guard. Missing nodes log warnings and skip
  rather than raising. Loader is idempotent.
- **Config: ``nexus_verification_registry``** (list of paths
  relative to ``conf.py``, default ``[]``). Paths point at YAML
  files loaded during ``_run_ast_analysis``, after the AST merge
  and before ``_infer_implements``.
- **Sphinx directives** ``.. verifies:: <label> :by: <symbol>`` and
  ``.. implements:: <label> :by: <symbol>``. Declarative edges
  expressed in theory docs rather than in test code or YAML. The
  ``:by:`` option accepts a bare dotted name
  (``orpheus.sn.solve_sn``) or an already-prefixed node id; if
  omitted, the directive falls back to ``env.ref_context``
  inspection so usage nested inside ``.. py:function::`` /
  ``.. autofunction::`` picks up the signature automatically.
- **Incremental-build-safe directive queue.** The pending-edge
  registry is keyed by docname and persists across incremental
  builds. An ``env-purge-doc`` handler drops stale entries when a
  doctree is about to be re-parsed; an ``env-merge-info`` handler
  folds parallel-build worker envs back into the main env. Fixes
  the same caching trap that bit the 0.7.0 upgrade.
- **``verification_audit`` grouping**. The query method and its MCP
  / CLI exposures gain two keyword-only arguments:
  ``group_by`` (one of ``"level"`` / ``"module"`` / ``"equation"``)
  which buckets the flat ``gaps`` list into a dict keyed by the
  chosen dimension; and ``include_tests`` which populates
  ``summary["tests_declared"]`` / ``summary["tests_inferred"]`` so
  consumers can weigh how much verification is declarative vs.
  heuristic.
- **``verification_gaps``** — a new query method surfacing three
  buckets:
  - ``untagged_tests`` — test nodes with no ``vv_level`` marker
  - ``unverified_equations`` — equations in the
    ``implemented`` / ``documented`` bucket
  - ``missing_err_catchers`` — members of an optional
    ``error_catalog`` set that no test's ``catches`` metadata
    references
  Filters by ``module`` and ``level``. Exposed as both a new MCP
  tool and a ``nexus gaps`` CLI subcommand.
- **``tests/fixtures/minimal_project/registry.yaml``** and a matching
  directive block in ``theory/solver.rst`` — the e2e harness pins
  both the registry pipeline and the directive lifecycle against a
  real ``sphinx-build``.

### Changed

- **MCP tool count: 24 → 25** (``verification_gaps`` added).
- **CLI subcommand count: 28 → 29** (``nexus gaps`` added).
- ``nexus_verification_registry`` paths resolve relative to
  ``app.srcdir`` (the directory holding ``conf.py``) so config
  entries colocated with theory docs work naturally. Users with a
  standard ``docs/conf.py`` layout can point at a repo-root
  registry via ``"../verification.yaml"``.
- ``verification_audit`` raises ``ValueError`` on an invalid
  ``group_by`` instead of silently ignoring it.

### Notes

- 251 → 272 tests (+21). Split across ``test_registry.py`` (new,
  17), ``test_directives.py`` (new, 20), extensions to
  ``test_query.py`` (+12), and the fixture harness (+9).
- ``.. verified-by::`` — the third directive the handoff spec
  listed — is NOT in this release. It would need "enclosing
  equation" detection, which is a different Sphinx lifecycle
  problem from py-object detection. Users can write
  ``.. verifies:: label :by: test`` from either side, or use the
  registry YAML, to cover the same relationship.
- PyYAML joins the core dependencies for the registry loader.

## 0.7.0 — 2026-04-13

Session 2 of the ORPHEUS V&V integration: pytest-marker ingestion,
declared TESTS edges, and multi-tier verification coverage.

### Added

- **Decorator parsing in the AST walker.** ``CodeVisitor`` now reads
  ``decorator_list`` on every function, method, and class. A flat
  ``decorators`` tuple of serialized source strings is recorded on the
  node's metadata, plus structured fields extracted by
  ``_parse_pytest_markers``:
  - ``vv_level`` (``"L0"`` / ``"L1"`` / ``"L2"`` / ``"L3"``) — from
    ``@pytest.mark.lN`` or ``@verify.lN(...)`` sugar.
  - ``verifies`` / ``catches`` — tuples of string literals extracted
    from ``@pytest.mark.verifies(...)`` / ``catches(...)`` args, or
    from ``equations=[...]`` / ``catches=[...]`` kwargs on
    ``@verify.lN(...)``.
  - ``slow`` — boolean flag from ``@pytest.mark.slow``.
- **Class- and module-level marker propagation.** ``@pytest.mark.lN``
  on a class or a module-level ``pytestmark = ...`` assignment
  propagates to contained methods / functions. Precedence is
  module < class < function, so a function-level marker always wins.
  Nested classes don't leak state upward.
- **``merge.write_verifies_edges``** — a post-merge pass that turns
  every ``@pytest.mark.verifies("label")`` marker into a real
  ``EdgeType.TESTS`` edge (source ``"pytest.mark.verifies"``,
  confidence 1.0). Missing equations are logged and skipped. The pass
  is idempotent on re-runs.
- **New config ``nexus_infer_implements``** (default ``True``) —
  turns off the token-intersection inference entirely for projects
  with full explicit coverage.
- **New ``TestReference`` dataclass** in ``query.py`` carrying ``id``,
  ``source``, ``confidence``, and ``display_name``. Returned by the
  tiered verification coverage search.
- **``tests/fixtures/minimal_project``** — a tiny self-hosting Sphinx
  project used by ``tests/test_fixture_e2e.py`` to regression-test
  every Session 1 and Session 2 feature through a real
  ``sphinx-build`` invocation.

### Changed

- **``verification_coverage`` uses three-tier test resolution.** Tier 1
  walks ``EdgeType.TESTS`` edges directly (source ``"declared"``,
  confidence 1.0). Tier 2 is the legacy 1-hop ``calls``-from-test
  scan (source ``"heuristic-1hop"``, confidence 0.7). Tier 3 is a
  bounded BFS up the ``calls`` graph from the implementing code node
  (source ``"heuristic-multihop"``, confidence 0.5, ``max_depth=3``).
  Heuristic tiers are only consulted when the declared tier is empty
  for that equation, so registry / marker / directive evidence
  short-circuits inference.
- **``CoverageEntry.tests`` type.** Changed from ``list[NodeResult]``
  to ``list[TestReference]``. Direct consumers that only read ``.id``
  (notably ``verification_audit``) keep working unchanged.
- **Semantic change to the ``verified`` status.** An equation is now
  ``verified`` if it has at least one test (declared or heuristic),
  regardless of whether intermediate implementing code is tracked.
  Previously ``verified`` required BOTH code AND a test, which
  silently demoted declarative-only evidence to ``documented``.
- **``_infer_implements`` now honors pre-existing explicit edges.**
  Any ``(code, equation)`` pair with an ``implements`` or ``tests``
  edge whose ``source`` is not ``"inferred"`` is skipped by the
  token-intersection heuristic so declared evidence never gets a
  duplicate inferred companion.

### Notes

- 202 → 214 tests; the new assertions are split across
  ``test_decorators.py``, ``test_ast_analyzer.py``,
  ``test_merge.py``, ``test_query.py``, and ``test_fixture_e2e.py``.
- No schema change. The new metadata fields ride on
  ``node_attrs`` which is already key-value-typed.
- **Incremental-build gotcha.** Sphinx caches AST analysis per-file,
  so adding a ``@pytest.mark.verifies(...)`` marker to an existing
  test file and re-running ``sphinx-build`` may leave the graph
  looking unchanged — the visitor doesn't re-parse files whose
  source hash hasn't moved relative to Sphinx's own tracking. A
  clean ``rm -rf docs/_build && sphinx-build`` picks up the new
  markers reliably. If you're validating a decorator change end-to-
  end and the graph doesn't show what you expect, rebuild from
  scratch before debugging the visitor.

## 0.6.0 — 2026-04-13

Released earlier on the same day. See the GitHub Release for details
— bug fixes in AST analysis (``:math:`` role routing, nested
``tests/`` exclusion, ``is_test`` false positives), new
``nexus_analyze_tests`` / ``nexus_test_patterns`` config values, and
the end of silent 20-entry truncation on ``verification_coverage`` /
``processes`` with opt-in ``limit`` / ``offset`` pagination.
