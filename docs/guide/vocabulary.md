# Vocabulary: nodes, edges, and ids

This page is the reference for reading a graph — what the node types mean,
what each edge asserts, and how ids are built. If you are writing the docs
that produce them, see {doc}`authoring`.

## Node ids

```
<domain>:<type>:<qualified_name>
```

```
py:function:pkg.sn.solver.solve_sn
py:class:pkg.cp.solver.CPMesh
py:method:pkg.cp.solver.CPMesh.compute_pinf_group
py:module:pkg.sn.solver
py:attribute:pkg.geometry.mesh.BC.vacuum
py:tag:geometry
math:equation:transport-balance
prf:theorem:thm-balance
doc:theory/discrete_ordinates
std:label:theory-collision-probability
citation:Bell1970
```

The id is stable across builds and is what every tool takes as input.
Ids are derived, not stored twice: `prf:theorem:thm-balance` tells you the
environment kind without a lookup.

## Node types (16)

**From documentation**

| Type | What it is |
|---|---|
| `file` | A doc page |
| `section` | A labelled section — a `:ref:` target |
| `equation` | A labelled math block — an `:eq:` target |
| `proof_object` | A labelled `sphinx-proof` environment. Kind in `metadata["prf_type"]`, prose in `metadata["statement"]` |
| `term` | A glossary term |

**From code**

| Type | What it is |
|---|---|
| `module` | A Python module |
| `class` | A class |
| `function` | A module-level function |
| `method` | A function bound to a class |
| `attribute` | A class or instance attribute |
| `data` | A module-level constant |
| `exception` | An exception class |
| `type` | A type alias |
| `tag` | A string or enum value a function branches on — the target of `discriminates_on` |

**Placeholders** — these are not definitions

| Type | What it is |
|---|---|
| `external` | stdlib, builtins, or an installed package (`numpy`, `scipy`) |
| `unresolved` | Referenced but never defined anywhere nexus scanned |

:::{note}
A placeholder exists so a reference has *something* to point at. It is
never a definition, and resolution always prefers a real node over one —
binding a live reference to a placeholder would manufacture a dead
reference out of a symbol that exists.
:::

## Edge types (16)

**Structure**

| Edge | Asserts | Source |
|---|---|---|
| `contains` | Parent → child: toctree, module → function, class → method | Sphinx + AST |
| `imports` | Module → module | AST |
| `inherits` | Class → base class | AST |

**Behaviour**

| Edge | Asserts | Source |
|---|---|---|
| `calls` | Function → function it calls | AST |
| `type_uses` | Function → a type in its annotations | AST |
| `discriminates_on` | Function → a tag it branches on (`if x == "..."`, `match`) | AST |

**Documentation**

| Edge | Asserts | Source |
|---|---|---|
| `documents` | Doc page → the code symbol it documents | Sphinx |
| `references` | A cross-reference (`:ref:`, `:term:`, and py-domain roles in docstrings) | Sphinx + AST |
| `equation_ref` | Doc → equation (`:eq:`, `:math:numref:`) | Sphinx |
| `cites` | Doc → citation | Sphinx |

**Code ↔ maths**

| Edge | Asserts | Source |
|---|---|---|
| `implements` | This code *is* this equation | Directive, or inferred at `confidence=0.7` |
| `tests` | This test *verifies* this equation | `@pytest.mark.verifies`, `.. verifies::` |
| `catches` | This test *catches* this catalogued failure mode | `@pytest.mark.catches` |
| `derives` | Paper-level lineage written by `ingest` | Ingest |

**Statement relations** — always specific → general

| Edge | Asserts | Source |
|---|---|---|
| `discretizes` | A discrete form → the continuous one it discretizes | Directive |
| `derives_from` | A specialization → the parent it was reduced from | Directive |
| `approximates` | A closure or truncation → the exact form it stands in for | Directive |

## Reading edge metadata

Two attributes carry most of the interpretive weight:

**`confidence`** — `1.0` for anything extracted or declared, `0.7` for an
inferred `implements`. Filter on it when a wrong edge would be expensive.

**`source`** — `"ast"`, `"directive"`, `"inferred"`, or a registry name.
An edge whose source is not `"inferred"` was asserted by a human.

## How the pieces make a chain

The reason these live in one graph rather than several:

```
citation:Bell1970
    ←cites—  doc:theory/transport
                 —contains→  math:equation:transport-balance
                                 ←implements—  py:function:pkg.sn.solve_sn
                                 ←tests—       py:function:tests.test_sn.test_balance
                                 ←discretizes— math:equation:sn-dd-closure
```

`provenance_chain` walks exactly this, in both directions, from any entry
point — a citation, an equation, or a function. The `relations` field
returns the maths-to-maths spine separately, since "what does this
discretize?" is a different question from "what implements it?".

## Node metadata worth knowing

| Key | Meaning |
|---|---|
| `file_path`, `lineno`, `end_lineno` | Definition site. Present ⇒ this came from real source |
| `is_test` | This **is** test material — name-based. Not the same as "collectable", see below |
| `in_test_file` | This **lives in** the test tree — file-based |
| `annotation` | The declared type of an attribute or data node |
| `prf_type`, `statement` | `sphinx-proof` environment kind and its prose |
| `decorators` | Rendered decorator list |

:::{note}
`is_test` and `in_test_file` are different questions. A helper in
`tests/_harness/registry.py` lives in the test tree but is not a test
case; `retest` and `dead_functions` depend on that distinction, and so
does the rule that stops test helpers absorbing production references.

⚠ And `is_test` is **not** the same as "pytest will collect this".
It is set on the classes, module-level constants and attributes of a
test file as well as on its test functions — measured on one real
project, 7305 nodes carry the flag and only **5273** (the `function`
and `method` subset) are collectable. Counting a suite by the flag
alone overstates it by 38 %, so filter by node type when the number is
going to mean something.
:::

## Edge evidence — declared or guessed

Not every edge is a fact. Each carries a `source` attribute recording
where it came from, and the difference is the difference between
evidence and resemblance:

| `source` | means |
|---|---|
| `pytest.mark.verifies`, `directive`, registry | **declared** — a human wrote this link |
| `ast` | read out of the syntax (imports, calls, inheritance) |
| `inferred` | **guessed** — a code symbol's name shares a token with an equation label |

An inferred edge also carries `confidence` and `shared_tokens`: the
words that produced the guess.

Replies mark only the guesses — declared is the silent default, since
annotating it would spend bytes on every entry to say "normal". In
`context` and `neighbors` an inferred entry gets `inferred: true` and
`via`; in `verification_coverage`, `code_evidence` says whether a row's
code side is `declared`, `inferred` or `mixed`.

:::{warning}
On one real project **12999 of 13084** `implements` edges are inferred
(2026-08-17; it was 14004 of 14004 before the first declarations landed).
So "which code implements this equation?" is answered there almost
entirely by shared-word matching, and a reader who
assumes otherwise is wrong every time. `tests` edges on the same
project are the opposite: 2748 of 2748 declared.
:::

## Provenance

Every graph is a snapshot of **one checkout**. `metadata["provenance"]`
records which:

```json
{
  "source_root": "/Users/me/proj",
  "built_at": "2026-08-10T11:09:46+00:00",
  "git_branch": "main",
  "git_commit": "52650a86",
  "git_dirty": true
}
```

If you work in git worktrees, this is what tells you whether the graph you
are querying matches the code you are editing — see `workspaces` and
`use_workspace` in {doc}`tools`.
