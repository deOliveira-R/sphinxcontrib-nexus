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

#: Placeholders in scenario prompts, filled from --subject-* flags or the
#: subject file. Keeps the scenario set project-agnostic: the QUESTION
#: shape is universal, the symbols are not.
DEFAULT_SUBJECT = {
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
) -> None:
    out = out_root / condition / scenario["id"]
    out.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, NEXUS_USAGE_LOG=str(out / "journal.jsonl"))
    # No --allowedTools restriction ON PURPOSE: the control scenarios
    # can only demonstrate correct routing if grep/Read are available,
    # and free tool choice is the behaviour under test.
    proc = subprocess.run(
        [
            "claude", "-p", scenario["prompt"],
            "--model", model,
            "--output-format", "json",
            "--max-turns", str(max_turns),
        ],
        cwd=project, env=env, capture_output=True, text=True,
    )
    (out / "result.json").write_text(proc.stdout or "{}")
    if proc.stderr:
        (out / "stderr.log").write_text(proc.stderr)


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

    if forbidden:
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


def report(scenarios: list[dict], out_root: Path, conditions: list[str]) -> int:
    rows: dict[str, list[dict]] = {}
    for condition in conditions:
        rows[condition] = [
            grade_one(s, out_root / condition / s["id"]) for s in scenarios
        ]

    failed = 0
    for condition in conditions:
        graded = rows[condition]
        hits = sum(1 for g in graded if g["verdict"] == "HIT")
        targets = sum(1 for g in graded if g["verdict"] in ("HIT", "MISS"))
        theater = sum(1 for g in graded if g["verdict"] == "THEATER")
        controls = sum(1 for g in graded if g["verdict"] in ("PASS", "THEATER"))
        cost = sum(g["cost"] or 0 for g in graded)
        failed += (targets - hits) + theater
        print(f"\n== [{condition}] steering {hits}/{targets} · "
              f"controls clean {controls - theater}/{controls} · ${cost:.2f}")
        for g in graded:
            if g["verdict"] in ("HIT", "PASS"):
                continue
            print(f"   {g['verdict']:8} {g['id']:16} first={g['first']} "
                  f"calls={g['n_calls']} {g['detail'] or ''}")

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
            (s, path, args.model, args.out, name, args.max_turns)
            for name, path in conditions.items()
            for s in scenarios
        ]
        print(f"Running {len(jobs)} sessions on {args.model} "
              f"({args.parallel} at a time)…", file=sys.stderr)
        with ThreadPoolExecutor(max_workers=args.parallel) as pool:
            list(pool.map(lambda job: run_one(*job), jobs))

    return report(scenarios, args.out, list(conditions))


if __name__ == "__main__":
    raise SystemExit(main())
