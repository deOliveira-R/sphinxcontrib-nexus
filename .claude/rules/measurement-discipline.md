# Measurement discipline — how to know a change worked

Nexus is a tool that makes claims about a codebase. When it is wrong, it is wrong
*quietly*: a bad binding produces a well-formed edge pointing at a node that
exists, and nothing downstream can question it. That asymmetry is the reason this
file exists.

Written after the 0.17.0 cycle (2026-08-10/11), where ORPHEUS's dead-reference
report went 14 → 7 → 6 → 7 → 0 → 1 and **the 6 was the regression**. Every rule
below is a thing that actually went wrong, not a precaution.

## A count is not a fitness function

`dead_references` falls when resolution gets better **and** when it gets more
confidently wrong. The count only asks "is this target missing?", which a
wrong-but-present target answers "no".

- **Diff edge targets, not totals.** `SELECT source, target, type FROM edges WHERE
  source='<the referring node>'` for the specific references the change should
  affect. A movement in the total is a question, not a result.
- A finding *appearing* is often the win. The one true dead reference ORPHEUS has
  showed up only after a fix stopped hiding it — the count went UP.
- Beware proxy metrics in the same way. "578 wrong bindings" came from a
  `tests.*` id prefix that also counted legitimate test→test references; the real
  number was ~32. An overstated problem is the same failure as an understated one.

## A byte-identical result means the fix is inert, not conservative

If a change produces the same node and edge counts on a 200k-edge graph, it did
nothing. Do not report it as "no regressions". Go back to the mechanism — that
signal is what exposed the merge-slice bug after a first fix attempt changed
nothing at all.

## Explain deltas you did not predict, before shipping

An unexplained −2,486 edges is not noise. Isolate it: rebuild with only the new
rule disabled and diff by edge type. That A/B is four minutes and it is how
`implements` inference turned out to be the whole delta — which then became its
own issue rather than silent collateral.

## Verify the test against the unfixed code

Revert the fix — **only** the source file, never the whole worktree — run the
test, confirm red, restore. Stashing everything removes the new tests too, and an
ImportError is not a failing assertion.

Three tests in one session passed against broken code. Every time the fixture
captured the bug's *shape* but not the *ordering* that triggers it:

- cross-directory ambiguity: one candidate in pass 1 folds correctly, so nothing
  survived for pass 2 to get wrong
- doc-page parent: the fixture inserted the `py:` parent first, so `parents[0]`
  was right regardless
- a `:numref:` test asserted behaviour that never existed, before or after

When the cause is insertion order, iteration order, or phase order, the fixture's
own construction order silently decides the outcome. Assume it does.

## Measure the premise before implementing it

Two issues written in this cycle did not survive their own measurement
instructions:

- **#45** claimed ORPHEUS mints `std:numref:` placeholders. It mints zero — the
  project has no `.. figure::` directives at all. The gap was real for other
  projects; the stated evidence was not.
- **#49** claimed test-implements edges were corrupting the V&V surface.
  `verification_coverage` reported identical `implemented` and `verified` counts
  before and after. The mechanism was real, the impact latent.

Reasoning from a mechanism to an impact overstates the impact. Write the
measurement instruction into the issue, then actually follow it — including when
the issue is your own, and say so in the PR when the answer changes.

## Validate on ORPHEUS, and know what it cannot tell you

`~/git/nuclear/ORPHEUS` is the only large real corpus. Build serially (never
`-j`, its read phase crashes) with `PYTHONPATH` pointed at this checkout so its
venv stays untouched:

```bash
cd ~/git/nuclear/ORPHEUS && PYTHONPATH=<nexus checkout> \
  .venv/bin/python -m sphinx -b html docs docs/_build/html -E
```

It has blind spots. Zero figures, zero tables, no `currentmodule` directives
anywhere. When a change cannot be exercised there, say so plainly and build a
fixture that drives a real `sphinx-build` — a hand-built graph would have to
assume the answer to the very thing under test.

## When a gate reports zero, prove it can report one

`dead-references --exit-code` passing means nothing until an injected broken
reference makes it fail. Twice this cycle a gate looked green while being
structurally incapable of firing: once with `total_checked: 0`, once because RST
role syntax in a MyST file never parses into a reference at all.

Same for graph assertions — a build that silently extracted nothing still writes
a valid empty database. Assert the shape (`> 500 nodes`, expected types present),
not just that the file exists.

## Read the rendered artifact, not just the exit code

A clean `-W` Sphinx build does not mean the page says what you wrote. Every
`` :class:`X` `` in a MyST file rendered as literal text — the build was green,
the cross-references were dead. Grep the HTML for `<a class="reference internal"`
before believing a link exists.

Nexus cannot catch this class: a role that fails to parse never becomes a
reference, so there is nothing for `dead_references` to find. It catches
references whose *target* vanished, not references the *parser* never recognised.

## The recurring bug shape: deciding from a partial view

Nearly every defect in the 0.17.0 cycle was one thing — **a pass making a
confident choice from information it did not have**:

| Symptom | Missing information |
|---|---|
| Unranked suffix match | that a candidate was a placeholder |
| Three drifting rank tables | that another pass ranked differently |
| Ambiguity judged per source directory | the rivals in a later directory |
| Namespace walk on `parents[0]` | that doc-page parents lead `in_edges` |
| Reconciliation inside each merge | the completed graph |

When adding a pass that decides something, ask what it can see. If it runs
per-file, per-directory, or per-batch but the decision needs project-wide
knowledge, it belongs after the last merge — the shape `_canonicalize_phantoms`
and `_resolve_relative_references` already have.

## Branch from `main`, not from the branch you just finished

A branch cut from an open PR carries that PR's commits into its own diff, so a
reviewer re-reviews merged work without being told. It happened twice; the second
time was deliberate and labelled. If stacking is genuinely needed, say so at the
top of the PR body and rebase onto `main` before merging.
