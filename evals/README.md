# Tool-selection evals

Skills and the routing rule are load-bearing instructions, and whether they
work is an **empirical question that changes as models change**. This suite
answers "do our instructions still steer agents to the right tool?" without
waiting for a consumer to notice they don't.

## What it measures

Each scenario is a natural-language *symptom* a user would type. It runs as an
isolated headless `claude -p` session against a real graph, with
`NEXUS_USAGE_LOG` capturing every MCP call. Grading reads the **journal**, not
the prose — did the session reach a dedicated tool, and was it the first
substantive call?

Two scenario shapes, both required:

| shape | success | the failure it catches |
|---|---|---|
| `intended` | reached a dedicated tool | hand-rolling: ten `query`/`context` calls approximating what one call answers |
| `forbidden` (controls) | did **not** touch the graph | **compliance theater** — using Nexus where `grep`/`Read` was correct |

Controls matter as much as targets. Instructions that over-steer produce
perfect `intended` scores and fail only here.

## Running it

```bash
# From a project that already has nexus set up. Start with haiku:
# weaker models fail first, so they localize gaps most cheaply.
./evals/run_evals.py --project ~/git/myproject --model haiku

# Re-grade without spending anything
./evals/run_evals.py --grade-only --out evals/results
```

Prompts carry `{placeholders}` for project-specific symbols so the scenario
set stays project-agnostic — the question shape is universal, the symbols are
not. Supply them with a subject file:

```bash
./evals/run_evals.py --project ~/git/myproject --subject evals/subject.example.json
```

Exit status is non-zero when any target is missed or any control shows
theater, so this can gate a release.

Rough cost for the full battery: **~$60 Opus, ~$25 Sonnet, ~$6 Haiku.**

## Ablation — finding the minimal sufficient instruction set

The point is not only "does it work" but "**what can we delete**". An
instruction layer that changes no outcome is cost without benefit: it consumes
context every session and drifts silently.

Build one directory per instruction set, then compare:

```bash
for cond in both skills-only rule-only neither; do
  mkdir -p /tmp/abl-$cond && (cd /tmp/abl-$cond && nexus setup)
  # then strip layers: rm -rf .claude/rules  (skills-only)
  #                    rm -rf .claude/skills (rule-only)
  # and point .mcp.json at a real graph
done

./evals/run_evals.py --model haiku \
  --conditions both:/tmp/abl-both skills-only:/tmp/abl-skills-only \
               rule-only:/tmp/abl-rule-only neither:/tmp/abl-neither
```

The report prints a verdict-by-condition matrix. Read it as:

- **same verdict in every condition** → that layer isn't carrying that
  scenario. Either the model already knows, or the steering lives elsewhere.
- **degrades when a layer is removed** → that layer is load-bearing for that
  scenario. Keep it, and keep this scenario as its regression test.

## When to re-run

- After a model release (the whole reason this exists).
- Before a release that changes skills, the routing rule, or tool docstrings.
- When adding a tool family — add a scenario for it in the same PR, or it
  will be discoverable only by luck.

## VOID runs — the trap that invalidates a whole battery

A headless session **cannot prompt for permission**, so any tool not in
`ALLOWED_TOOLS` is silently denied and the agent falls back to whatever it can
reach. An empty journal therefore has two completely different meanings:

- the agent didn't reach for the graph → a real **MISS** about steering;
- the agent reached and was refused → a **VOID** run that says nothing.

The harness reads `permission_denials` from the result JSON and reports VOID
separately, because conflating the two once produced an entire ablation of
uniform MISSes that read as *"no instruction layer matters at all."* The agents
had in fact named the correct tool in their prose and simply could not call it.

**Both tool families must be allowed.** Allow only the graph and every control
scenario passes trivially; allow only text tools and every target scenario
misses trivially. If a report shows VOID rows, the measurement is invalid —
fix the allowlist and re-run, don't interpret it.

## Interpreting a miss

A miss is rarely "the model is bad". In every case measured so far it was an
**instruction gap**: the tool existed and worked, but nothing in the always-on
surface named the symptom the user typed. Fix the instructions, then re-run
the failing scenario to confirm — don't fix the eval.
