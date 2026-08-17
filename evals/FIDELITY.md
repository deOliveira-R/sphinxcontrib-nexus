# Response evals — is what Nexus GIVES BACK useful?

> A **separate battery** from [README.md](README.md), not a second axis
> of it. They share a directory and nothing else: different subject
> under test, different thing you change to improve the score, different
> reason to re-run.

|  | tool-selection ([README](README.md)) | **responses (this file)** |
|---|---|---|
| **subject under test** | the instruction surface — skills, rules, agent definitions | **Nexus itself** |
| **improve the score by** | rewording instructions | changing Nexus's code and what it returns |
| **model-dependent?** | **yes** — the skills are tuned for Claude, not Codex or Grok | **no** — agent-independent |
| **re-run when** | a model ships | Nexus's behaviour changes |
| **cost / cadence** | ~$6–60, per model release | free, **every commit** |

That last row is the practical consequence of agent-independence: this
battery needs no model, no API key and no headless session, so it can
gate a merge the way `pytest` does. The routing battery cannot.

`run_evals.py` asks **"did the agent reach the right tool?"** and grades
the *journal* — which tools were called, in what order. That is the
reachability question, and it is answered well.

It cannot see whether the tool then told the truth.

> ⭐ **The founding measurement, 2026-08-16.** A field trial found nine
> defects in Nexus. The routing battery would have scored **every one of
> them a HIT**: the agent reached `provenance_chain`, and the answer was
> 112 equations of the wrong granularity; the agent reached `callers`,
> and the answer was `0` for a method called one frame away in its own
> file. Routing was perfect. The instrument was lying.

It exists because a knowledge graph fails **silently and in the
reassuring direction**: every defect found so far returned a `0`, a
large flattering number, or silence — never an error. That is the
failure mode that survives longest, because nothing prompts anyone to
look.

## The two things a response is graded on

Every probe and every finding belongs to one of these. Keep them
separate: they have different fixes, and a reply can be perfect on one
and useless on the other.

**SIGNAL — do you get what you need?** Is the answer true, at the right
granularity, complete, and free enough of noise to act on? A reply that
is correct but buries the decisive row under 110 irrelevant ones has
failed on signal just as surely as a wrong one.

**ERGONOMICS — can you reach this tool, *and* the tool it chains to?**
Two halves, and the second is the one that gets forgotten. Reachability
is whether the tool can be called at all for the situation you are in —
`file_brief` fails here, because it arrives only by a hook and has no
MCP twin. **Chainability** is whether the answer hands you the next
call: a reply that names an equation but not in a form you can paste
into the next tool has broken the chain, and a chain that needs a hand
transform, a `grep`, or a fresh lookup at any hop does not close.

⟹ `F8` measures chains end to end, because a battery of per-tool scores
can be all-green while no *question* can actually be answered: each hop
is fine and no hop connects. `[M]` 2026-08-16 the founding project
scored **0 of 4 chains closed** while every individual tool worked.

---

## The method, in one paragraph

Fidelity has two halves and needs both. **Probes** are deterministic,
free, and re-runnable — they turn each failure class into a number that
moves as the instrument improves, so rounds are comparable by
construction. **Field trials** dispatch real specialist agents at real
work and harvest the friction; they are expensive, stochastic, and they
are the only half that finds a class nobody thought of yet. Probes
regress what you know; trials discover what you don't. A round that runs
only probes will confirm your existing beliefs beautifully.

---

## Part 1 — The probes (cheap, deterministic, every release)

```bash
./evals/fidelity_probes.py --project ~/git/myproject
./evals/fidelity_probes.py --project ~/git/myproject --json > round.json
```

No model, no headless session, seconds to run. Each probe is named for
the failure class it measures (below), so a number always arrives with
its meaning attached. **A number without its class is a curiosity** —
record the class or don't record the number.

### ⚠ Probes are not independent — read the dependency before the value

`F2` (untested equations) is computed over `implements` edges, and `F5`
says those edges run **10:1 inferred-to-declared** on the founding
project. So F2's *absolute* value is a measurement of a population that
is mostly guesses; only its **trend** is safe to read, and only while F5
is unchanged. When F5 moves, F2's history is void and must be
re-baselined, not compared.

State every such coupling when you add a probe. A scoreboard whose rows
silently depend on each other produces the compound-`[M]` defect: two
honest numbers, one false conclusion.

---

## Part 2 — The field trial (expensive, periodic, finds new classes)

This is the procedure that produced the founding round. It is written
out because its value is entirely in the details that look optional.

### 2.1 Choose real work, not a test of the tool

Give each agent a task **someone would actually pay for**, in a
genuinely complicated subsystem, and require it to be done. An agent
asked to "evaluate the tools" produces opinions; an agent asked to
answer *"what breaks if this operator's signature changes?"* produces
the moment where it gave up and ran `grep`. That moment is the finding.

Pick subjects with these properties, or the trial is toothless:

- a file with **many nodes and many external callers** (the answer must
  not fit in a glance);
- at least one **test** subject and one **documentation** subject — the
  brief and half the tools are production-shaped, and nothing reveals
  that until a test file is the subject;
- at least one subsystem reached by **dynamic dispatch** (registry,
  attribute-held polymorphism), because that is where a call graph is
  blind and the blindness prints as `0`.

### 2.2 Dispatch specialists, not generalists, and dispatch them in parallel

Use the agent whose *duties* differ, not whose prompt differs — an
archivist, a test-architect and an explorer ask structurally different
questions and therefore hit different defects. In the founding round the
three overlapped on only one finding (`provenance_chain`) and found the
rest independently.

### 2.3 The report schema — verbatim, because the sections are load-bearing

Require exactly these, and say that density beats completeness:

| section | what it is for |
|---|---|
| **TOOLS USED** | ordered, one line each, marked **SIGNAL / GARBAGE / MIXED**. The mark forces a verdict per call rather than a mood at the end. |
| **DEAD ENDS** | every call that returned something useless or misleading, **and why** — too big, wrong granularity, ids that could not be pasted onward. |
| **THE INJECTION** | what of the ambient brief was actionable *as-is*, and what it named that needed another call. |
| **FRICTION LOG** | ⭐ **the highest-value section.** Every moment a second call was needed to make a first answer usable, and every fact derived by hand that the graph already knew. |
| **WISHLIST** | concrete changes, each with the question it would answer. |

**Why FRICTION LOG is the one that matters:** a tool that is *wrong*
gets noticed. A tool that is *right but needs three follow-ups* is
experienced as normal work, and never reported unless a section exists
whose title demands it. Every "I had to open the DB directly to read
`vv_level`" line in the founding round came from that section.

Forbid edits and commits. The trial must not change the subject it is
measuring.

### 2.4 Reproduce the ambient surfaces without triggering them

The edit-time brief fires from a `PostToolUse` hook, so an agent
forbidden to edit will never see it. Have the trial invoke the same
command the hook does:

```bash
.venv/bin/nexus file-brief <abs path .py> --db .nexus/graph.db --project-root .
```

### 2.5 Verify before you act — the trial's own results are claims

Two of the founding round's most alarming numbers were **wrong, and
mine**. A probe that followed every edge type read "91 % of tests
falsely marked safe-to-skip"; restricted to the relation that means
*executes*, the real figure reproduced the issue's own `18`. Reporting
the first would have impeached a correct issue.

⟹ **A failed reproduction is not a refutation until you have diagnosed
whose failure it is.** Re-derive a relayed number yourself, and when
your own check disagrees, suspect your fixture first.

---

## Part 3 — The failure taxonomy

The durable output of the founding round. Classify every finding; a
class with no probe is the next probe to write.

| id | graded on | class | the tell | founding case |
|---|---|---|---|---|
| **F1** | signal | **False zero** — *unresolvable* printed as *absent* | a `0` that grep contradicts | `callers(_OneDimScanWalk._run)` = 0; called one frame away. A capitalisation heuristic decided "is this a class?" |
| **F2** | signal | **Flattering aggregate** — the count hides its own refutation | a big number summed over members, no member list | "91 tests verify these equations" for a module with **15 of 24** equations uncovered |
| **F3** | ergonomics | **Unaddressable handle** — the reply names what it will not let you use | a string that needs a transform the emitter knows | equation labels emitted bare; must be prefixed `math:equation:` by hand. `[M]` **0 of 50** usable as ids |
| **F4** | signal | **Silent knowledge** — the graph holds it, no tool says it | an agent opening the DB directly | `vv_level` / `verifies` / `catches` on every test node; the test-file brief carries **0** of them |
| **F5** | signal | **Undeclared inference** — a guess rendered as a fact | two edge kinds, one font | `tests` (declared, from a marker) and `implements` (name-matched) look identical. `[M]` 1 : 10 |
| **F6** | ergonomics | **Push-only surface** — arrives unbidden, cannot be requested | a hook with no MCP twin | `file_brief`: deduped once per session, unrecoverable after a compaction |
| **F7** | signal | **Structurally degenerate answer** — a result that cannot vary | 100 % of rows in one bucket | `verification_audit(group_by="level")`: gaps are *defined* by having no test, and the grouping keys on the nearest test's level |
| **F8** | ergonomics | **Broken chain** — every hop works, the question still cannot be answered | a hop needing a hand transform, a `grep`, or a fact the reply withheld | `symbol → doc page → section`: `[M]` **680** sections and **0** `section→equation` edges, so recovery ends in `awk` over a line range |

Two properties make this list worth keeping. Every class is **stated as
a shape, not as a bug**, so it can be recognised in a tool nobody has
written yet. And every class was found by *use*, not by review — none of
them is visible in the code, because in each case the code does exactly
what it says.

---

## Part 4 — The scoreboard

Append a round; never edit an old one. A round is `(date, nexus
version, subject project, graph build commit)` — a fidelity number
without its subject is meaningless, since these measure the *pair*.

### 2026-08-16 · nexus 0.15.x (unreleased) · ORPHEUS @ `a7423799`

Graph: 22 848 nodes / 206 919 edges. Probes run **before** the analyzer
fix (`8fafd18`) landed in the graph, so F1's `mistyped` is the defect's
live value, not a regression.

| probe | value | reading |
|---|---|---|
| **F1** false zeros | **7421 / 10207 (72.7 %)** code nodes with no caller | mistyped class scope **195**, dispatch-suspect **781** |
| **F2** untested equations | **2441 / 4040 (60.4 %)** | ⚠ rides on F5; trend only |
| **F3** handles | eq labels usable as ids **0 / 50** | docnames resolvable **50 / 50** |
| **F4** brief answers its file's question | production **4 / 6**, **test 0 / 6** | the test-side brief is silent by construction |
| **F5** declared : inferred | **1 : 10.0** (2748 / 27 395) | |
| **F6** payload | **181–197 B/entry**; `BC` 417 entries folded from 1699 edges | post-#67 |
| **F8** chains closed | **0 / 4** | every hop works; no question closes |

The F8 rows, since the aggregate hides which hop fails:

| chain | breaks at | class |
|---|---|---|
| `file → node → callers` | hop 1 — no file-addressed MCP tool; `node_at` needs a line you do not have yet | F6 |
| `equation → tests → pytest invocation` | hop 3 — 34 equations reach their tests, **0** hand over a runnable id | F3 |
| `symbol → doc page → section` | hop 3 — 680 sections, **0** `section→equation` edges | F8 |
| `brief label → graph node` | hop 1 — **0/50** labels paste directly as ids | F3 |

**What changed as a result:** `8fafd18` (F1 — class scope decided
structurally, 110 methods retyped, 14 callers recovered);
`b6d99d2`/`ad200a4` (F6, and production callers ranked above test
callers); issues #72–#80 filed, one per class above.

**Expected at the next round** — stated so it is falsifiable: F1
`mistyped` → **0**; F1 total roughly flat, because the remaining zeros
are F1-by-dispatch (#76) and untouched; everything else unchanged until
its issue is worked.

---

## Part 5 — Standing rules, each earned

1. **Evaluate by USING it.** Not one of the nine findings is visible by
   reading the code. The sharpest arrived as *"grep answered and the
   graph didn't."*
2. **A zero is the most dangerous answer a graph gives**, because it
   reads as a licence to delete. Treat every new `0` as unexplained
   until a mechanism is named.
3. **When a result looks dramatic, check your instrument first.** Twice
   in the founding round the alarming number was the probe's fault —
   including a mutation battery whose `sed` no longer matched the code
   it was mutating, so it reported `FAILED=0` and *no experiment had
   run*. **A battery is code and rots like code.**
4. **Fix the instrument, not the probe.** The sibling rule from
   [README](README.md) ("fix the instructions, not the eval") applies
   unchanged: adjusting a probe until the number improves is how a
   scoreboard becomes decoration.
5. **A fixture more regular than the world makes its gate blind.** A
   ranking gate passed for months because the fixture's insertion order
   already matched the ranked order; deleting the ranking reddened 0 of
   21 tests. For any gate asserting an ORDER, build the fixture in the
   WRONG order.

---

## Part 6 — Improving this file

Same contract as `eval-authoring`: **when a round teaches something this
file does not already say, add it in the same commit as the finding.**

A round owes this file one of three things, and "nothing to add" is a
legitimate answer only when the round found no new class:

- a **new class** in the taxonomy (with the case that founded it);
- a **new probe** for a class that had none — the fastest way to spot
  the gap is a taxonomy row with no `F<n>` in `fidelity_probes.py`;
- a **retired** probe, when its number has been 0 for several rounds
  *and* its class has a permanent gate elsewhere. A probe that cannot
  move is costing a run and teaching nothing.

⭐ The target is falsifiable and it is not "all numbers improve": it is
that **a field trial stops finding classes the taxonomy lacks.** When
three specialists spend a round on real work and every finding lands in
an existing row, the taxonomy is complete for that generation of the
tool — and the trial can drop to an annual, with the probes carrying the
regression load in between.
