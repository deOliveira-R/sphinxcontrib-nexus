# Baseline results

Re-run after a model release and compare. A drop here means the instructions
stopped steering the new model — not that the model got worse.

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
