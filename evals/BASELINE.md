# Baseline results

Re-run after a model release and compare. A drop here means the instructions
stopped steering the new model — not that the model got worse.

Each round records: model, version, per-scenario verdicts (keyed by scenario
id so they compare across releases), **the eval's own self-grade**, and what
changed as a result. Method and pitfalls: `.claude/skills/eval-authoring/`.

## 2026-08-08 · Haiku 4.5 · nexus 0.15.0 (unreleased)

Clean room built by `nexus setup` only, pointed at a real ~180 MB graph.
`instructed` = skills + always-on rule; `bare` = neither. 3 replicates/cell,
zero permission denials.

| scenario | style | instructed | bare |
|---|---|---|---|
| `dead-docs` | direct | MISS? 2/3 | MISS? 2/3 |
| `cleanup-after-delete` | indirect | **HIT?** 2/3 | MISS 3/3 |
| `release-doc-health` | proactive | MISS? 2/3 | MISS 3/3 |
| `lockstep-edits` | indirect | MISS? 2/3 | MISS 3/3 |
| `parallel-work` | indirect | **HIT 3/3** | MISS 3/3 |
| `onboarding-health` | proactive | **HIT 3/3** | MISS 3/3 |
| `todo-control` | control | PASS | PASS |
| **aggregate** | | **3/6** | **0/6** |

### What this establishes

**Instructions are load-bearing.** Bare scores 0/6 — eighteen runs, zero
correct tool selections. Nothing in the instruction surface can be deleted on
the theory that the model already knows; that hypothesis is dead.

**Symptom-level phrasing is what moved the needle.** Two scenarios flipped
from consistent MISS to consistent HIT after the skills stopped describing
tools by what they *are* and started naming what users actually say:

- `parallel-work` ("two people built these independently, should I worry?")
  0/3 → **3/3**, reaching `twin_paths` every time.
- `onboarding-health` ("honest health check before onboarding") → **3/3**,
  and all three replicates swept the *entire* smell family
  (`twin_paths`, `dead_functions`, `discriminations`, `native_place`,
  `protocol_conformers`), two of them adding `dead_references`. That is the
  "sweeps you run without being asked" section doing exactly its job.

**Controls stayed clean in both conditions.** No compliance theater: the
instructions did not push agents into using the graph for a TODO grep.

**There is a ceiling.** Even instructed, Haiku reaches 3/6 — and the misses
are mostly runs where it answered with *no tool calls at all* rather than
picking the wrong tool. Steering has limits on a small model, which is the
argument for the push channels (`/doc-health`, the dead-refs hook): for
findings that must not be missed, inject rather than steer.

### Self-grade of this eval

```
flake rate      5/12 — raise --repeat
situational mix 5/6 indirect+proactive — acceptable
leak rate       0/5 situational hits with NO instructions — clean
discrimination  3/6 vs 0/6 (Δ+3) — instructions are load-bearing
controls        present, clean in both conditions
void rate       0 — harness sound
```

**Reading it:** the battery discriminates and does not leak, so the Δ+3 is
real evidence. The flake rate is the weak point — 5 of 12 target cells were
non-unanimous at n=3, which is why only the unanimous cells (`parallel-work`,
`onboarding-health`) are quoted as findings. **Next round: `--repeat 5`.**

### What changed as a result

- Skills and the routing rule gained a *what-the-user-actually-says* table and
  an explicit "sweeps you run without being asked" section (validated: two
  scenarios 0/3 → 3/3).
- `dead_references` got a CLI, a `/doc-health` slash command, and a
  `SessionStart` hook — because its miss is silent, and steering has a
  ceiling this eval measured.
- Nothing was deleted from the instruction surface: bare 0/6 killed the
  redundancy hypothesis.

---

## 2026-08-09 · Opus · meta-eval of the `eval-authoring` skill

Does the skill make an agent *diagnose* correctly? Six situations that actually
occurred while building this battery — each first diagnosed wrong — graded on
whether the answer contains the diagnosis the reference teaches. Eval authoring
is never delegated below Opus, so no Sonnet/Haiku arm. 3 replicates.

| scenario | the trap it poses | with skill | without |
|---|---|---|---|
| `meta-leak` | "it hit with no instructions → self-explanatory" | **HIT** | **MISS 3/3** |
| `meta-optimise` | "reword the prompt until it passes" | **HIT** | **MISS 3/3** |
| `meta-harness` | "every condition MISSed → nothing matters" | HIT | HIT |
| `meta-noise` | "two passes disagree → trust the newer" | HIT | HIT? |
| `meta-placement` | "it's critical → shout it in a skill" | HIT | HIT |
| `meta-controls` | "everything passes → we're done" | HIT | HIT |
| **aggregate** | | **6/6** | 4/6 |

**The skill's value concentrates on the two least intuitive traps.** Opus
already knows to check the harness, use replicates, prefer deterministic
injection for critical findings, and want controls. What it does *not* supply
unaided is the **correlation/causation framing** of a leaked prompt, and the
discipline of **fixing the instructions rather than the eval**. Those two are
the reason the skill exists.

### Self-grade of this meta-eval — two defects it caught in itself

```
flake rate      1/12 — acceptable
situational mix 6/6 indirect+proactive — acceptable
NO CONTROLS — over-steering is undetectable
NO BARE ARM — cannot separate steering from keyword match
```

Both warnings are fair and neither was noticed before the tool said so:

1. **No control scenario** — nothing tests whether the skill causes
   *over*-application (e.g. demanding a 5-condition ablation for a one-line
   docstring tweak). Add one next round.
2. **The "without" arm was not bare.** It kept `evals/README.md` and
   `BASELINE.md`, which carry much of the methodology — including the literal
   phrase *"check the harness first"*, which is exactly what `meta-harness`
   tests. So 4/6 is inflated and those four rows are **not** evidence of model
   priors. The two clean wins stand, because `correlation`/`causation` and
   `fix the instructions` appear in no other file. Logged as pitfall #10.

### Method notes worth keeping

Per-cell verdicts are still noisy at n=3 — `dead-docs` has flipped between
HIT and MISS across otherwise-identical passes. Trust the **aggregate** and
the **consistent 3/3** cells; treat a single `HIT?`/`MISS?` as provisional.
Raise `--repeat` before drawing a conclusion about one scenario.

Four separate "surprising" results during development turned out to be
harness faults, not instruction faults: permission-denied runs graded as
misses, a clobbered `.mcp.json` pointing at a nonexistent graph, a denied
`Skill` tool punishing the very behaviour the rule asks for, and one void
replicate poisoning its cell. The VOID verdict, the `permission_denials`
check, and the replicate consensus all exist to make those loud instead of
silent. **When a result looks dramatic, check the harness first.**

## 2026-09-21 · Haiku 4.5 · nexus 0.18.0 + the routing rule by skill (unreleased)

Two conditions, the question being whether the trimmed rule (nine deciding
rows routed by skill; the 16-row tool table retired to the skills) steers as
well as the shipped one. Clean rooms built by `nexus setup` only, each
`.mcp.json` re-pointed at the ORPHEUS graph (`nexus serve --project-root
~/git/nuclear/ORPHEUS`; the graph built the same day), subject
`evals/subject.example.json`, `--model haiku --repeat 3 --parallel 3
--max-turns 10`; Claude Code 2.1.278; 23 scenarios (14 direct, 5
indirect, 2 proactive, 2 controls), 138 sessions in round 1 and 69 in round 2, permission denials 0 of 207; the 10-turn cap ended 10 / 9 / 7 cells (current / v1 / v2), which the journal still grades. The dollar figures the runner prints are API-price equivalents from `total_cost_usd`; the runs were on a Claude subscription.

Round 1 (current vs the first trim, the smell-family tool names removed from the rule): current steering 19/21 · controls clean 2/2 · $7.35; trimmed v1 steering 17/21 · controls clean 2/2 · $6.86. Round 2 (the trim with the smell family and the map naming their tools inside the routed row, run alone against the same `current` data): trimmed v2 steering 19/21 · controls clean 2/2 · $6.49.

| scenario | current | trimmed v1 | trimmed v2 |
|---|---|---|---|
| `arch-map` | MISS | MISS? | MISS |
| `bridges` | HIT | MISS? | HIT |
| `twins` | HIT? | MISS? | HIT? |
| `discrim` | HIT | MISS? | HIT |
| `conformers` | HIT | HIT? | HIT |
| `dead-code` | HIT | HIT? | HIT |
| `belongs` | HIT? | HIT? | HIT |
| `dead-docs` | HIT | HIT? | HIT |
| `stale` | HIT | HIT? | HIT |
| `impact` | HIT? | HIT | HIT |
| `provenance` | HIT | HIT | HIT |
| `gaps` | HIT | HIT | HIT |
| `connect` | HIT | HIT | HIT |
| `runtime` | HIT | HIT | HIT? |
| `cleanup-after-delete` | HIT | HIT | HIT |
| `release-doc-health` | MISS? | HIT? | HIT |
| `lockstep-edits` | HIT | HIT | HIT |
| `parallel-work` | HIT? | HIT | HIT |
| `onboarding-health` | HIT | HIT | HIT |
| `fuzzy-boundaries` | HIT | HIT? | MISS? |
| `risky-change` | HIT? | HIT | HIT? |
| `todo-control` | PASS | PASS | PASS |
| `readfile-control` | PASS | PASS | PASS |


### What this establishes

**Ship the trim.** At three replicates the trimmed rule and the shipped one tie
on the aggregate (19/21) and on the direct rows (13/14), the controls are clean
in both, and the two cells that differ swap one provisional indirect miss for
one provisional proactive hit. At six replicates on those two cells the
trimmed rule reads equal or better on both: `fuzzy-boundaries` 5/6 against
4/6, `release-doc-health` 4/6 against 3/6 (the shipped rule's cell FLAKY).
The first trim, with the smell-family tool names removed from the rule, is
refuted (17/21; the direct smell rows fell to two-of-three misses with no
graph call at all): for haiku a tool's NAME in the always-on rule is what
makes the tool reachable on a direct prompt, and a route to the skill that
names it is not. The rule therefore names the smell family's and the map's
tools inside their routed rows, and still goes from 16 tool rows to 10 route
rows (5 868 → 5 518 bytes).

The self-grade's warnings, read for what they are: the round-1 flake rate
(17/42) is why the two cells were re-run; the mix is mostly direct (7/21
indirect + proactive), so the aggregate is a floor and the two situational
rows carry the conclusion; there is no bare arm because the question was
"does the trim steer as well as the shipped rule", both arms instructed, not
"do instructions steer at all" (the 2026-08-08 round answered that).

### Method notes worth keeping

- A prompt that says "this project" with no placeholder (`arch-map`,
  `bridges`) reads the clean room as the project: haiku describes the
  `.claude/` tree or reports "this directory is configuration, not a
  codebase" and never reaches the graph, in both conditions. Such cells
  measure the clean room, not the instructions; anchor those prompts with a
  placeholder next round, or run them from a checkout.
- `nexus setup` in a directory with no `.venv` writes an `.mcp.json` whose
  command does not exist there; the "point `.mcp.json` at a real graph" step
  of the README is a rewrite of that file, by hand, before the first run.
