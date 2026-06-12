---
name: behavioral-auto-regression
description: "Proactively use when agent behaviour regresses to using Grep instead of Nexus for code exploration. Diagnoses the root cause (system prompt bias), extracts the vocabulary needed to override it, and produces corrected AGENT.md blocks. Examples: \"Agent is using Grep instead of Nexus\", \"Tool selection is wrong\", \"Bias steering isn't working\""
---

# Behavioral Auto-Regression

Diagnose and fix agent tool-selection regression. When agents default to
Grep (or grep-wielding generic sub-agents) for code exploration instead
of Nexus MCP tools, the root cause is a mismatch between the agent's
LIVE system prompt vocabulary and the override vocabulary in AGENT.md /
CLAUDE.md. The system prompt is a moving target: re-probe after Claude
Code releases; the specifics below carry their probe date.

## When to Use

- An agent used Grep for a code exploration question (callers, dependents,
  architecture, equations) instead of Nexus
- An agent DELEGATED exploration to a generic search sub-agent that greps
- An agent skipped a deferred Nexus tool instead of loading its schema
- A new agent was added without the tool override block
- The system prompt changed and existing overrides stopped working

## Known regression vectors (probed 2026-06; re-verify per release)

Probe coverage 2026-06: Fable 5 (interactive main-agent scaffold) AND
Opus 4.8 (sub-agent scaffold, probed via dispatched introspection).
Cross-model findings: the delegation sentence and the ToolSearch
deferral mechanics are VERBATIM-IDENTICAL across both; base scaffolds
favor IMPORTANT/NEVER (CRITICAL is a project-layer token, VERY
IMPORTANT absent from both). The sub-agent scaffold has NO pro-Grep
default at all — its search guidance is fully tool-agnostic ("search
broadly", "use multiple search strategies"), and the dedicated-tools
idea appears only as the Bash tool's cat/head/tail/sed/awk/echo
avoidance note. Consequence: for sub-agents, Nexus steering is purely
ADDITIVE — there is no directive to override, so the affirmative
dispatch table and the deferral/delegation counters below are the
entire mechanism.

1. **Plain habit regression** (the original): statistical pull toward
   grep for any "find something in code" impulse. The historical trigger
   directive `ALWAYS use Grep for search tasks` is **GONE from current
   prompts** — today's language is "Prefer the dedicated file/search
   tools over shell commands when one fits", which steers shell-grep →
   Grep-tool and says nothing about MCP. The counter flips accordingly:
   don't fight a phantom ALWAYS — claim the live category: "**the Nexus
   MCP tools ARE this project's dedicated code-exploration tools**."
2. **Search delegation** (new): current prompts steer uncertain or
   multi-file searches toward dispatching an agent ("not confident you
   will find the right match in the first few tries — use this agent";
   "reading across several files — delegate it"). A generic sub-agent
   has no Nexus skills and greps. Counter: route exploration dispatches
   to the PROJECT explorer agent (Nexus skills preloaded) and deny the
   built-in Explore agent in settings (`"deny": ["Agent(Explore)"]` —
   ORPHEUS does this).
3. **Tool deferral** (new): MCP tools may surface as DEFERRED — listed
   by name, schema unloaded, direct calls fail with InputValidationError.
   The lazy path is "tool unavailable → grep". Counter, verbatim for
   override blocks: "If `mcp__nexus__*` tools are deferred, ONE
   `ToolSearch(\"select:mcp__nexus__<name>\")` call loads them.
   Deferral is NOT unavailability."

## What to Do

### 1. Identify the regression

Check the agent's tool usage report — and its DISPATCHES. Any Grep call
(or generic-agent search dispatch) for a question Nexus can answer
(callers, dependents, coverage, equations) is a regression. The server's
usage journal (`~/.nexus/usage.jsonl`, v0.12+) gives the Nexus side of
the ledger: a session whose journal is empty but whose transcript is
full of exploration was regressed throughout.

### 2. Probe the agent's system prompt vocabulary

Launch a fresh agent with the probe prompt in
[scripts/probe-prompt.md](scripts/probe-prompt.md). This extracts:
- Which keywords carry the most weight (IMPORTANT, MUST, NEVER, Do NOT —
  CRITICAL remains a strong tier even where the prompt favors IMPORTANT)
- Which directives steer toward Grep or toward search delegation, in
  their exact current phrasing
- What phrasing would effectively override those directives

### 3. Apply the vocabulary-alignment fix

The key mechanism is **alignment with the live prompt's own terms**, not
prohibition alone. Historically that meant reclassification ("exploration
tasks, NOT search tasks" — defusing the then-extant Grep `ALWAYS`);
against current prompts it means claiming their categories: Nexus IS the
"dedicated tool" for code exploration, the project explorer agent IS the
search delegate, ToolSearch-loading IS the response to deferral.

Every AGENT.md that uses Nexus skills MUST contain a CRITICAL Tool
Selection Override block. The correct block for each skill is in:
`<nexus-skill>/scripts/tool-override-block.md`

The block must:
1. Use `CRITICAL` + `OVERRIDE` — strongest vocabulary tier
2. Claim the live category (see vector 1): Nexus = the dedicated
   code-exploration tools; the legacy reclassification line is harmless
   but no longer load-bearing
3. Use `MUST`/`NEVER`/`ONLY permitted` — hard obligations
4. Include a dispatch table mapping questions to specific Nexus tools
5. Add a justification gate: "Before using Grep, you MUST state why
   Nexus is insufficient"
6. Cover vectors 2 and 3: exploration dispatches go to the project
   explorer agent, and deferred `mcp__nexus__*` tools are LOADED via
   ToolSearch, never skipped

### 4. Validate the fix

Launch the agent with a real task. Prompt must include:
"Report every tool you used and why."

If Grep count = 0, the fix works. If not, resume the agent and ask
why it chose Grep — this identifies missing convenience functions or
unclear tool signatures (Phase 2 candidates).

See [reference.md](reference.md) for the full procedure, rationale,
and validation results — it records the 2026-04 probe of a prompt
generation whose Grep `ALWAYS` directive no longer exists; treat its
specifics as historical and this file's vector list as current.
