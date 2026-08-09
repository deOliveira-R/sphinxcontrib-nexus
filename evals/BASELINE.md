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
