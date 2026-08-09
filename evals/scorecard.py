#!/usr/bin/env python
"""Aggregate every eval run into one steering-capability view.

Answers "where do we actually stand?" across models and scenarios, keyed
by the tool each scenario is meant to reach. Reads result trees produced
by ``run_evals.py`` and grades them with the SAME grader, so the numbers
here cannot drift from the numbers a single run reports.

    ./evals/scorecard.py --results ~/runs/opus:opus ~/runs/haiku:haiku

Scores are split by prompt style, and that split is the point: `direct`
prompts paraphrase the tool's own description, so a high direct score
measures keyword matching and is a FLOOR. Only the situational rows
(`indirect`, `proactive`) predict whether a tool is reachable when a
user describes their actual problem. A blended headline number hides
exactly the difference you need to see.
"""

from __future__ import annotations

import argparse
import importlib.util
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent

def _load_harness():
    """Import run_evals.py as a module so the scorecard and a live run
    share one grader — two implementations would drift, and the whole
    point of this file is that its numbers match."""
    spec = importlib.util.spec_from_file_location("_re", HERE / "run_evals.py")
    if spec is None or spec.loader is None:  # pragma: no cover
        raise RuntimeError("cannot load evals/run_evals.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_re = _load_harness()


def iter_cells(root: Path):
    """Yield (scenario_id, replicate_dir) for any result-tree layout.

    Trees may be ``<root>/<scenario>/r0`` (replicates) or the older
    ``<root>/<scenario>``; conditions add one level above. Walk rather
    than assume, so historical runs stay comparable.
    """
    if not root.is_dir():
        return
    for path in sorted(root.rglob("result.json")):
        rep = path.parent
        is_replicate = rep.name.startswith("r") and rep.name[1:].isdigit()
        yield (rep.parent.name if is_replicate else rep.name), rep


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", nargs="+", required=True,
                        help="PATH:MODEL pairs (repeatable).")
    parser.add_argument("--scenarios", type=Path,
                        default=HERE / "scenarios.jsonl")
    args = parser.parse_args()

    scenarios = {
        s["id"]: s
        for s in _re.load_scenarios(args.scenarios, _re.DEFAULT_SUBJECT)
    }

    # (scenario, model) -> [verdict, ...]
    cells: dict[tuple[str, str], list[str]] = defaultdict(list)
    models: list[str] = []
    for spec in args.results:
        path_str, _, model = spec.rpartition(":")
        if model not in models:
            models.append(model)
        for sid, rep in iter_cells(Path(path_str).expanduser()):
            scenario = scenarios.get(sid)
            if scenario is None:
                continue
            cells[(sid, model)].append(_re.grade_one(scenario, rep)["verdict"])

    def cell(sid: str, model: str) -> str:
        verdicts = [v for v in cells.get((sid, model), []) if v != "VOID"]
        if not verdicts:
            return "·"
        good = sum(1 for v in verdicts if v in ("HIT", "PASS"))
        return f"{good}/{len(verdicts)}"

    for style, title in (
        ("direct", "DIRECT prompts — a FLOOR (they paraphrase the tool; "
                   "high scores here measure keyword matching)"),
        ("indirect", "INDIRECT prompts — the user describes a situation"),
        ("proactive", "PROACTIVE — nobody asked; using the tool is the job"),
        ("control", "CONTROLS — the graph is the WRONG answer "
                    "(PASS = correctly stayed out)"),
    ):
        rows = [s for s in scenarios.values() if s.get("style") == style]
        rows = [s for s in rows if any(cells.get((s["id"], m)) for m in models)]
        if not rows:
            continue
        print(f"\n## {title}\n")
        print(f"{'scenario':22}{'intended':34}" +
              "".join(f"{m:>9}" for m in models))
        print("-" * (56 + 9 * len(models)))
        for scenario in rows:
            want = ",".join(sorted(scenario.get("intended") or [])) or "—"
            print(f"{scenario['id']:22}{want[:33]:34}" +
                  "".join(f"{cell(scenario['id'], m):>9}" for m in models))

    print("\n## Aggregate by model and style\n")
    print(f"{'model':10}{'direct':>12}{'indirect':>12}"
          f"{'proactive':>12}{'controls':>12}")
    print("-" * 58)
    for model in models:
        out = [model.ljust(10)]
        for style in ("direct", "indirect", "proactive", "control"):
            verdicts = [
                v
                for sid, s in scenarios.items()
                if s.get("style") == style
                for v in cells.get((sid, model), [])
                if v != "VOID"
            ]
            if not verdicts:
                out.append("·".rjust(12))
                continue
            good = sum(1 for v in verdicts if v in ("HIT", "PASS"))
            out.append(f"{good}/{len(verdicts)}".rjust(12))
        print("".join(out))
    print("\nEmpty journal counts as a MISS (the agent answered without "
          "reaching the tool);\nonly permission-denied VOID runs are excluded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
