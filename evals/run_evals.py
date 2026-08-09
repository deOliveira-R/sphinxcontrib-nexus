#!/usr/bin/env python
"""Tool-selection eval harness — does the instruction surface steer agents?

Every scenario is a natural-language SYMPTOM a user would actually type,
run as an isolated headless ``claude -p`` session against a real graph
with ``NEXUS_USAGE_LOG`` pointed at a per-run journal. Grading reads the
journal, not the prose: did the session reach a tool from the scenario's
``intended`` set, and was it the FIRST substantive call?

Why this exists
---------------

Skills and the routing rule are load-bearing instructions, and their
correctness is an empirical question that changes as models change.
Running this after a model release answers "do our instructions still
steer?" without waiting for a consumer to notice they don't.

Two scenario shapes, both required:

* ``intended`` — reaching a dedicated tool. Missing it means the agent
  hand-rolls with ``query``/``context``: ten calls approximating one.
* ``forbidden`` (control scenarios) — questions where ``grep``/``Read``
  is the CORRECT answer. A graph call here is *compliance theater*, the
  opposite failure, and instructions that over-steer will show up only
  in these rows.

Prompt style is what makes or breaks validity
---------------------------------------------

A prompt that paraphrases a tool's own description tests **keyword
matching**, not routing judgement — "find docs referencing things that
no longer exist" will reach ``dead_references`` with no instructions at
all, because the words line up. Such a scenario cannot tell you whether
the tool is reachable in real use, and a battery built only from them
reports a flattering number that predicts nothing.

Every scenario therefore carries a ``style``:

* ``direct`` — paraphrases the tool description. A floor, not evidence.
* ``indirect`` — describes the SITUATION and never names the concept.
* ``proactive`` — never asks for the tool at all; using it is part of
  doing the stated job well. This is the bar for an agent expected to
  hunt problems rather than wait to be told.

Scores are reported per style. Optimise the indirect and proactive rows;
a direct-only improvement is usually just a better-matching phrase.

Ablation
--------

``--conditions`` runs each scenario against several instruction sets
(e.g. skills+rule vs skills only vs neither) to find the MINIMAL
sufficient surface. Instructions that change no outcome are cost without
benefit — they consume context every session and drift silently.

Usage
-----

    # one condition, from a project that already has nexus set up
    ./evals/run_evals.py --project ~/git/myproject --model opus

    # ablation across instruction layers (build the dirs first, see --help)
    ./evals/run_evals.py --conditions both:~/ab/both neither:~/ab/neither

    # grade an existing run without re-running anything
    ./evals/run_evals.py --grade-only --out evals/results

Cost is real: a full battery is roughly $60 on Opus, $25 on Sonnet, $6 on
Haiku. Start with ``--model haiku`` — weaker models fail first, so they
localize instruction gaps most cheaply.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent

#: Tools pre-approved for every run. BOTH families must be present or
#: the measurement is void: a headless session cannot prompt for
#: permission, so anything unlisted is silently denied and the agent
#: falls back to whatever it *can* reach. Allowing only the graph makes
#: every control scenario trivially pass; allowing only text tools makes
#: every target scenario trivially miss. Free choice between them is the
#: behaviour under test. Read-only by construction — these scenarios ask
#: questions, and a run that edits the subject tree has corrupted the
#: fixture for every later run.
#: ``Bash`` is unrestricted rather than narrowed to ``Bash(grep:*)``
#: style rules: the standalone ``Grep``/``Glob`` tools no longer exist
#: in current scaffolds, so text search IS Bash, and per-command rules
#: fail to match the pipelines agents actually write — which silently
#: voids every control scenario. Mutation tools (``Write``, ``Edit``)
#: are deliberately absent: scenarios ask read-only questions, and a run
#: that edits the subject tree corrupts the fixture for every later run.
#: An agent DID attempt a ``Write`` during development; the allowlist
#: blocked it, which is the safety this list is for.
ALLOWED_TOOLS = [
    "mcp__nexus__*",
    "Read", "Grep", "Glob", "Bash",
]

#: Placeholders in scenario prompts, filled from the ``--subject`` file.
#: Keeps the scenario set project-agnostic: the QUESTION shape is
#: universal, the symbols are not.
DEFAULT_SUBJECT = {
    "deleted_symbol": "a module we removed last month",
    "smell_module": "the largest package in this project",
    "impact_symbol": "the most-called public function in this project",
    "equation_label": "any labeled equation in the docs",
    "module_a": "one subsystem",
    "module_b": "another subsystem",
    "source_dir": "the main source",
    "known_file": "the project's main entry-point module",
}


def load_scenarios(path: Path, subject: dict[str, str]) -> list[dict]:
    scenarios = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        scenario = json.loads(line)
        scenario["prompt"] = scenario["prompt"].format(**subject)
        scenarios.append(scenario)
    return scenarios


def run_one(
    scenario: dict,
    project: Path,
    model: str,
    out_root: Path,
    condition: str,
    max_turns: int,
    replicate: int = 0,
) -> None:
    out = out_root / condition / scenario["id"] / f"r{replicate}"
    out.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, NEXUS_USAGE_LOG=str(out / "journal.jsonl"))
    proc = subprocess.run(
        [
            "claude", "-p", scenario["prompt"],
            "--model", model,
            "--output-format", "json",
            "--max-turns", str(max_turns),
            "--allowedTools", *ALLOWED_TOOLS,
        ],
        cwd=project, env=env, capture_output=True, text=True,
    )
    (out / "result.json").write_text(proc.stdout or "{}")
    if proc.stderr:
        (out / "stderr.log").write_text(proc.stderr)


def denied_tools(out: Path) -> list[str]:
    """Tools the session TRIED to call and was refused.

    An empty journal has two very different causes: the agent chose not
    to reach for the graph (a real MISS about steering), or it reached
    and was blocked (a VOID run that says nothing about steering).
    Conflating them once produced an entire ablation of uniform MISSes
    that read as "no instruction layer matters" — when the agents had
    named the right tool in their prose and simply could not call it.

    ``permission_denials`` in the result JSON is the authoritative
    signal; a headless session cannot prompt, so every unlisted tool
    lands here rather than in the journal.
    """
    try:
        result = json.loads((out / "result.json").read_text())
    except (OSError, ValueError):
        return []
    return sorted({
        d.get("tool_name", "")
        for d in (result.get("permission_denials") or [])
        if d.get("tool_name")
    })


def journal_tools(out: Path) -> list[str]:
    path = out / "journal.jsonl"
    if not path.exists():
        return []
    tools = []
    for line in path.read_text().splitlines():
        try:
            tools.append(json.loads(line)["tool"])
        except (ValueError, KeyError):
            continue
    return tools


def grade_one(scenario: dict, out: Path) -> dict:
    """Verdict for one run. ``session_briefing`` is discounted as an
    ambient warm-up call, never the answer to a question."""
    tools = journal_tools(out)
    substantive = [t for t in tools if t != "session_briefing"]
    result = {}
    try:
        result = json.loads((out / "result.json").read_text())
    except (OSError, ValueError):
        pass

    intended = set(scenario.get("intended") or [])
    forbidden = set(scenario.get("forbidden") or [])

    denied = denied_tools(out)
    if denied and not substantive:
        # Blocked, not steered — says nothing about the instructions.
        verdict, detail = "VOID", denied[:4]
    elif forbidden:
        # Control scenario: success is NOT touching the graph.
        used = sorted(forbidden & set(substantive))
        verdict = "THEATER" if used else "PASS"
        detail = used
    elif not tools and not (out / "result.json").exists():
        verdict, detail = "NO-RUN", []
    else:
        hit = sorted(intended & set(substantive))
        verdict = "HIT" if hit else "MISS"
        detail = hit

    return {
        "id": scenario["id"],
        "family": scenario.get("family", ""),
        "verdict": verdict,
        "detail": detail,
        "first": substantive[0] if substantive else None,
        "n_calls": len(substantive),
        "cost": result.get("total_cost_usd"),
        "subtype": result.get("subtype"),
    }


#: Verdicts ranked worst-first for majority reporting. A tie between
#: HIT and MISS is reported as FLAKY, never rounded to either: an
#: instruction that works half the time is a real and separate finding
#: from one that works or one that doesn't.
def _consensus(verdicts: list[str]) -> tuple[str, str]:
    """Collapse replicate verdicts into (verdict, annotation).

    Returns e.g. ``("HIT", "3/3")`` or ``("FLAKY", "1/2")``. Agent
    behaviour is stochastic; a single run per cell cannot tell a real
    steering effect from sampling noise — an early ablation here flipped
    three cells between two otherwise-identical passes.
    """
    if not verdicts:
        return "NO-RUN", ""
    if "VOID" in verdicts:
        return "VOID", f"{verdicts.count('VOID')}/{len(verdicts)}"
    counts = {v: verdicts.count(v) for v in set(verdicts)}
    top = max(counts.values())
    winners = sorted(v for v, n in counts.items() if n == top)
    if len(winners) > 1:
        return "FLAKY", f"{top}/{len(verdicts)}"
    winner = winners[0]
    tag = f"{top}/{len(verdicts)}"
    if top < len(verdicts):
        return (winner if winner in ("PASS", "THEATER") else f"{winner}?"), tag
    return winner, tag


def grade_cell(scenario: dict, cell: Path) -> dict:
    """Grade every replicate under a scenario dir and collapse them."""
    reps = sorted(p for p in cell.glob("r*") if p.is_dir()) if cell.exists() else []
    if not reps:  # pre-replicate layout
        reps = [cell]
    graded = [grade_one(scenario, r) for r in reps]
    verdict, tag = _consensus([g["verdict"] for g in graded])
    return {
        "id": scenario["id"],
        "verdict": verdict,
        "consensus": tag,
        "n": len(graded),
        "detail": graded[0]["detail"],
        "first": graded[0]["first"],
        "n_calls": graded[0]["n_calls"],
        "cost": sum(g["cost"] or 0 for g in graded),
    }


def report(scenarios: list[dict], out_root: Path, conditions: list[str]) -> int:
    rows: dict[str, list[dict]] = {}
    for condition in conditions:
        rows[condition] = [
            grade_cell(s, out_root / condition / s["id"]) for s in scenarios
        ]

    style_of = {s["id"]: s.get("style", "direct") for s in scenarios}
    failed = 0
    for condition in conditions:
        graded = rows[condition]
        theater = sum(1 for g in graded if g["verdict"].startswith("THEATER"))
        controls = sum(1 for g in graded if style_of[g["id"]] == "control")
        cost = sum(g["cost"] or 0 for g in graded)
        void = sum(1 for g in graded if g["verdict"] == "VOID")
        hits = sum(1 for g in graded if g["verdict"].startswith("HIT"))
        targets = sum(
            1 for g in graded
            if g["verdict"].startswith(("HIT", "MISS", "FLAKY"))
        )
        failed += (targets - hits) + theater
        if void:
            print(f"\n!! {void} VOID run(s) — permission-denied, not steered. "
                  f"The measurement is invalid; fix the allowlist and re-run.")
        print(f"\n== [{condition}] steering {hits}/{targets} · "
              f"controls clean {controls - theater}/{controls} · ${cost:.2f}")

        # Per-style breakdown. Direct prompts paraphrase the tool's own
        # description, so they measure keyword matching; indirect and
        # proactive prompts are the ones that predict real use. Reporting
        # a single blended number hides exactly that difference.
        for style in ("direct", "indirect", "proactive"):
            group = [g for g in graded if style_of[g["id"]] == style]
            if not group:
                continue
            got = sum(1 for g in group if g["verdict"].startswith("HIT"))
            print(f"     {style:10} {got}/{len(group)}")
        for g in graded:
            if g["verdict"] in ("HIT", "PASS"):
                continue
            print(f"   {g['verdict']:8} {g['id']:18} "
                  f"[{style_of[g['id']]}] first={g['first']} "
                  f"calls={g['n_calls']} {g['consensus']} {g['detail'] or ''}")

    if len(conditions) > 1:
        print("\n== ablation: verdict by condition "
              "(a layer that changes nothing is cost without benefit) ==")
        width = max(len(c) for c in conditions)
        header = " " * 18 + "".join(f"{c:>{width + 2}}" for c in conditions)
        print(header)
        for i, scenario in enumerate(scenarios):
            cells = "".join(
                f"{rows[c][i]['verdict']:>{width + 2}}" for c in conditions
            )
            print(f"  {scenario['id']:16}{cells}")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--project", type=Path,
                        help="Project dir to run in (must have nexus MCP configured).")
    parser.add_argument("--conditions", nargs="*", default=None,
                        help="Ablation: NAME:PATH pairs, one per instruction set.")
    parser.add_argument("--model", default="haiku",
                        help="Model under test (default: haiku — fails first, "
                             "so it localizes gaps most cheaply).")
    parser.add_argument("--scenarios", type=Path, default=HERE / "scenarios.jsonl")
    parser.add_argument("--out", type=Path, default=HERE / "results")
    parser.add_argument("--subject", type=Path, default=None,
                        help="JSON file of prompt placeholders for this project.")
    parser.add_argument("--only", nargs="*", default=None,
                        help="Scenario ids to run (default: all).")
    parser.add_argument("--parallel", type=int, default=3)
    parser.add_argument("--repeat", type=int, default=1,
                        help="Replicates per cell. Agent behaviour is "
                             "stochastic — n=1 cannot separate a steering "
                             "effect from sampling noise. Use >=3 before "
                             "deleting an instruction on ablation evidence.")
    parser.add_argument("--max-turns", type=int, default=10)
    parser.add_argument("--grade-only", action="store_true",
                        help="Grade an existing --out tree; run nothing.")
    args = parser.parse_args()

    subject = dict(DEFAULT_SUBJECT)
    if args.subject:
        subject.update(json.loads(args.subject.read_text()))

    scenarios = load_scenarios(args.scenarios, subject)
    if args.only:
        scenarios = [s for s in scenarios if s["id"] in set(args.only)]
    if not scenarios:
        print("No scenarios selected.", file=sys.stderr)
        return 2

    if args.conditions:
        conditions = {c.split(":", 1)[0]: Path(c.split(":", 1)[1]).expanduser()
                      for c in args.conditions}
    elif args.project:
        conditions = {"default": args.project.expanduser()}
    elif args.grade_only:
        conditions = {p.name: p for p in sorted(args.out.iterdir()) if p.is_dir()}
    else:
        print("Need --project, --conditions, or --grade-only.", file=sys.stderr)
        return 2

    if not args.grade_only:
        jobs = [
            (s, path, args.model, args.out, name, args.max_turns, rep)
            for name, path in conditions.items()
            for s in scenarios
            for rep in range(args.repeat)
        ]
        print(f"Running {len(jobs)} sessions on {args.model} "
              f"({args.parallel} at a time)…", file=sys.stderr)
        with ThreadPoolExecutor(max_workers=args.parallel) as pool:
            list(pool.map(lambda job: run_one(*job), jobs))

    return report(scenarios, args.out, list(conditions))


if __name__ == "__main__":
    raise SystemExit(main())
