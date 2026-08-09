---
name: nexus-elegance
description: "Use when reviewing recently-changed code for architectural decay (twin paths, duplicated concepts, broken doc-to-code provenance, incomplete retirement) and you want the knowledge graph to scope and corroborate the review. Examples: \"Review this diff for structural decay\", \"Is this a twin path or one source?\", \"Did the refactor actually retire the old pattern?\", \"What's the blast radius of this smell?\", \"Does this code still link to the equation it implements?\""
---

# Structural Review with Nexus

This skill owns exactly one thing: **the map from each structural review
axis to the graph query that corroborates it, plus the discriminators that
stop a graph signal from becoming a false finding.** It does not restate
your project's design principles — load those separately. For full tool
schemas see [../nexus-exploring/reference.md](../nexus-exploring/reference.md).

IMPORTANT: The graph is a WITNESS, never the accuser. Every finding
originates from reading the changed code. Nexus *scopes* the read (what
changed, what it touches) and *corroborates* the hypothesis (is this really
a twin? does this concept really live in two places?). **A finding whose
entire basis is a graph metric — with no code-level argument for why it
breeds bugs — is not a finding.**

## Precondition (READ FIRST)

The MCP server may answer from a different checkout's graph. Before any
query: `session_briefing` → confirm the graph's branch matches yours. In a
git worktree, rebuild there, then `use_workspace(<worktree root>)`. A stale
graph manufactures phantom "severed provenance" and "orphan" findings.

## Workflow

```
0. detect_changes(scope="branch")        → changed lines → node worklist
1. impact(<node>, "upstream")  per node  → blast radius = severity multiplier
2. per-axis corroboration (below)        → confirm/refute the code-read hypothesis
3. provenance_chain + verification_coverage → doc↔code↔test chain intact?
4. callers(<predecessor>) + retest       → retirement complete + tests rewired?
5. severity = (code-read finding strength) × (blast radius)
```

## Axis → tool map

**Axis 1 — Data structures.** `context(node_id)`, `neighbors(node_id,
direction="out", edge_types="type_uses")`. *Smell:* a function whose
`type_uses` edges are all bare containers (`np.ndarray`, `dict`, `tuple`)
and that crosses a module boundary — distinct quantities sharing one
representation, with no static check against an argument swap.

**Axis 2 — Path multiplicity.** `twin_paths(min_similarity=0.7)` for the
whole-graph sweep; `callees(A)` + `callees(B)` + `shortest_path(A, B)` to
adjudicate a pair. *Smell:* two functions whose AST bodies share a high
shingle fraction and that do not call each other → two implementations of
one computation. The fingerprint catches array math (`@`, einsum, slicing)
the call graph cannot see.

**Axis 3 — Procedural transcription.** `callees(fn, transitive=True,
max_depth=2)`. *Smell:* the transitive callee set is entirely numpy/stdlib
primitives with no domain operator → the code is *about* the algebra rather
than *being* the algebra.

**Axis 4 — Single source of truth / missing types.**
`discriminations(min_sites=2)`, `native_place(min_callers=1)`,
`graph_query("* -implements-> equation")`. *Smell:* the same tag branched on
at ≥2 sites (a missing type / absent dispatch); a module-level function
whose every caller is a method of one class (feature envy); one equation
with ≥2 `implements` edges (one concept in two places).

**Axis 5 — Doc/domain alignment.** `provenance_chain(node_id)`,
`verification_coverage()`, `dead_references()`. *Smell:* `provenance_chain`
empty or broken for a function that visibly implements a cited equation →
the change severed the code↔equation↔citation chain. `dead_references`
catches the inverse: prose still citing a symbol the change deleted (Sphinx
renders those as plain text with NO warning). A refactor that breaks
provenance is a correctness regression even when tests pass.

**Axis 6 — Retirement.** `dead_functions()`, `callers(predecessor)`,
`impact(predecessor, "upstream")`, `retest(scope="branch")`. *Smell:* the
"retired" symbol still has live non-test callers → retirement incomplete,
the old pattern lingers and invites accidental extension.

**Axis 7 — Conformance / unused weight.** `protocol_conformers(min_methods=2)`,
`dead_functions()`. *Smell:* classes satisfying a Protocol's method-set
without declaring it — the `inherits` edge only sees explicit subclassing, so
a structural conformer is invisible until declared.

## Severity from blast radius

`impact(<node>, "upstream")` does not *create* findings — it *scales* them.

```
severity = (code-read finding strength) × (upstream blast radius)
```

A confirmed twin path with 14 upstream dependents is a violation (14× the
divergence surface); the same twin with 1 caller is a concern. NEVER open a
finding whose only basis is a blast-radius number.

## Corroboration across tools

Two tools flagging the SAME symbol escalates harder than either alone — e.g.
`native_place` and `discriminations` both hitting one free function that is
coupled to a single class *and* branches on a tag. The agreement says "this
symbol is the seam of a missing abstraction," not two unrelated nits.

## False-positive table (graph says "smell"; READ before flagging)

| Graph signal | Looks like | Discriminator (what the graph can't see) | Verdict |
|---|---|---|---|
| `twin_paths` high similarity, or 2 nodes `implements` one equation | Twin path | One applies an operator, the other solves with it; or two genuine paths share one leaf | Shared leaf → PASS. Inlined into each → VIOLATION |
| 2 `calls` edges reach the SAME node | Twin path | Twin *delivery*, single-sourced at the callee | Byte-identical overlap → CONCERN, demand a removal trigger |
| `twin_paths` on `apply`/`apply_transpose`, `domain`/`codomain` | Twin path | Symmetric-by-design forward/adjoint or dual accessors | PASS unless they inline divergent arithmetic |
| `native_place` with `likely_free_primitive=true` | Feature envy | Public + independently tested = correctly free | PASS (leave free) |
| High upstream `impact` on a `god_nodes`/`bridges` hit | Risky change | The node is the single source doing its job | Amplifies a confirmed finding; NOT a finding alone |
| `provenance_chain` empty after a refactor | Severed doc chain | Stale graph (docs not rebuilt in this checkout) | Rebuild + `use_workspace`, re-check. Flag only if still empty |
| `dead_functions` / `callers` = test nodes only | Dead weight | Deliberate retained oracle for an equivalence test | Wired to a `tests` edge → PASS. No test edges → VIOLATION |
| `dead_functions` with `decorated=true` or `public=true` | Dead code | Registry/route/property decorator invokes it indirectly; public = entry point | Read for dynamic dispatch / external callers first |
| `protocol_conformers` match | Undeclared conformer | Method-NAME match only; signatures ignored | Confirm with the type checker (pyright / LSP goToImplementation) |
| `discriminations` single dispatcher reads one tag | Missing type | A guard and its dispatcher sharing one predicate cannot drift | Flag only a SECOND independent spelling |
| `dead_references` hit on a dynamic attribute | Dead doc reference | `__getattr__` / metaclass magic creates it at runtime | Static analysis can't see it — verify before flagging |

## Closing checklist

1. Every violation has BOTH a design-principle citation AND a bug-habitat
   argument. A graph metric alone is never the basis.
2. Twin-path findings were checked against the apply-vs-solve and
   twin-delivery discriminators.
3. Provenance/orphan findings were re-checked after confirming the graph
   matches the checkout — not against a stale one.
4. Retained-oracle nodes were checked for `tests` edges; `protocol_conformers`
   hits were confirmed with the type checker.
5. Blast radius scaled severity; it did not manufacture a finding.
6. Confirmed smells hand off to `nexus-refactoring` for the safe rename or
   extract; the required change names the destination, not the path.
