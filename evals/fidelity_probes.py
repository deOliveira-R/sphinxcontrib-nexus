#!/usr/bin/env python3
"""Fidelity probes — is the ANSWER true, usable and complete?

The sibling battery (`run_evals.py`) measures *routing*: did the agent
reach the right tool. It grades the journal, not the reply, so a tool
that is reached and then lies scores a clean HIT. Every defect found in
the 2026-08-16 field trial was of that shape.

These probes measure the other axis, deterministically and for free: no
model, no headless session, just the graph and the assemblers. Each one
emits a number that MOVES as the instrument improves, so a round is
comparable to the last one by construction.

    ./evals/fidelity_probes.py --project ~/git/myproject

Read `FIDELITY.md` for the method, the failure taxonomy, and what each
number is evidence OF. A number without its class is a curiosity.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sphinxcontrib.nexus._serialize import (  # noqa: E402
    assemble_context,
    assemble_neighbors,
    to_json,
)
from sphinxcontrib.nexus.export import load_sqlite  # noqa: E402
from sphinxcontrib.nexus.query import GraphQuery  # noqa: E402

#: A class scope that a capitalisation heuristic would miss. Kept as a
#: probe after the 2026-08-16 fix so a regression is visible, not as a
#: live defect: the count is expected to be 0 forever.
_PRIVATE_CLASS_SEGMENT = re.compile(r"\._[A-Z][A-Za-z0-9]*\.")

_CODE_TYPES = ("function", "method")


def _rows(g, pred):
    return [n for n, a in g.nodes(data=True) if pred(n, a)]


def probe_false_zeros(g) -> dict:
    """F1 — how much of "0 callers" means UNRESOLVABLE, not UNCALLED.

    A zero is the graph's most dangerous answer because it reads as a
    licence to delete. This splits the population by the two mechanisms
    measured so far: a mistyped class scope (fixed), and a callee
    reached only through an attribute or a registry (open, #76).
    """
    code = _rows(g, lambda n, a: a.get("type") in _CODE_TYPES and a.get("file_path"))
    zero = [
        n for n in code
        if not any(d.get("type") == "calls" for _, _, d in g.in_edges(n, data=True))
    ]
    mistyped = [n for n in code
                if n.startswith("py:function:") and _PRIVATE_CLASS_SEGMENT.search(n)]
    # A method whose name also exists as a bare unresolved receiver is
    # the dispatch signature: somebody called it and the call landed
    # nowhere real.
    unresolved_leaf = {
        n.split(":", 2)[-1] for n, a in g.nodes(data=True)
        if a.get("type") == "unresolved" and "." not in n.split(":", 2)[-1]
    }
    dispatch_suspect = [
        n for n in zero if n.split(".")[-1] in unresolved_leaf
    ]
    return {
        "code_nodes": len(code),
        "zero_callers": len(zero),
        "zero_callers_pct": round(100 * len(zero) / max(len(code), 1), 1),
        "mistyped_class_scope": len(mistyped),
        "dispatch_suspect": len(dispatch_suspect),
    }


def probe_coverage_inversion(g) -> dict:
    """F2 — does the headline number hide its own refutation?

    The file brief reports "N tests verify these equations", an
    aggregate over labels. The decision-relevant half is the inverse:
    how many of the equations a module implements have NO catcher. In
    the founding case a module read "91 tests verify" while 15 of its
    24 equations had zero.
    """
    tested = {
        v for _, v, d in g.edges(data=True) if d.get("type") == "tests"
    }
    per_module: dict[str, set[str]] = {}
    for u, v, d in g.edges(data=True):
        if d.get("type") != "implements":
            continue
        mod = u.split(":", 2)[-1].rsplit(".", 1)[0]
        per_module.setdefault(mod, set()).add(v)

    worst = []
    total_eq = total_untested = 0
    for mod, eqs in per_module.items():
        untested = [e for e in eqs if e not in tested]
        total_eq += len(eqs)
        total_untested += len(untested)
        if untested:
            worst.append((len(untested), len(eqs), mod))
    worst.sort(reverse=True)
    return {
        "equations_implemented": total_eq,
        "untested": total_untested,
        "untested_pct": round(100 * total_untested / max(total_eq, 1), 1),
        "worst_modules": [
            {"module": m, "untested": u, "of": n} for u, n, m in worst[:5]
        ],
    }


def probe_declared_vs_inferred(g) -> dict:
    """F5 — how much of the graph is a guess rendered as a fact?

    `tests` edges come from an explicit marker; `implements` is minted
    by name matching. Both are drawn in the same font by every tool, so
    a consumer cannot tell evidence from resemblance.
    """
    counts: dict[str, int] = {}
    for _, _, d in g.edges(data=True):
        counts[d.get("type", "?")] = counts.get(d.get("type", "?"), 0) + 1
    declared = counts.get("tests", 0)
    inferred = counts.get("implements", 0) + counts.get("references", 0)
    return {
        "declared_edges": declared,
        "inferred_edges": inferred,
        "inferred_per_declared": round(inferred / max(declared, 1), 1),
    }


def probe_answer_payload(q, g) -> dict:
    """F6 — of the answer, how much survives the reply budget?

    Bytes alone flatter: a truncated reply is small. The honest metric
    is what FRACTION of the true answer a caller actually sees, and
    whether the part that survives is the part they need.
    """
    hubs = sorted(
        (n for n, a in g.nodes(data=True)
         if a.get("type") in _CODE_TYPES + ("class",) and a.get("file_path")),
        key=lambda n: -g.degree(n),
    )[:3]
    out = []
    for h in hubs:
        raw = len(q.neighbors(h, direction="both"))
        entries = assemble_neighbors(q, h)
        ctx = assemble_context(q, h)
        out.append({
            "node": h,
            "raw_edges": raw,
            "entries_after_fold": len(entries),
            "bytes_neighbors": len(to_json(entries)),
            "bytes_context": len(to_json(ctx)),
            "bytes_per_entry": round(len(to_json(entries)) / max(len(entries), 1)),
        })
    return {"hubs": out}


def probe_brief_by_file_kind(project: Path, db: Path, g) -> dict:
    """F4 — does the ambient brief answer the question the FILE poses?

    A production file asks "what depends on me, and what am I
    accountable to". A test file asks "what do I verify". Measured
    2026-08-16, the brief answered the first and was silent on the
    second for 100% of test files sampled, while the graph held the
    `tests` edges all along.
    """
    def sample(pred, k=6):
        seen, out = set(), []
        for _, a in g.nodes(data=True):
            fp = a.get("file_path")
            if not fp or fp in seen or not pred(fp):
                continue
            seen.add(fp)
            out.append(fp)
            if len(out) >= k:
                break
        return out

    def brief(path: str) -> str:
        try:
            r = subprocess.run(
                [str(project / ".venv/bin/nexus"), "file-brief", path,
                 "--db", str(db), "--project-root", str(project)],
                capture_output=True, text=True, timeout=60,
            )
            return r.stdout if r.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            return ""

    result = {}
    for kind, pred, wanted in (
        ("production", lambda p: "/tests/" not in p and p.endswith(".py"),
         ("implements:",)),
        ("test", lambda p: "/tests/" in p and p.endswith(".py"),
         ("verifies:", "implements:")),
    ):
        files = sample(pred)
        answered = sum(
            1 for f in files if any(w in brief(f) for w in wanted)
        )
        result[kind] = {
            "sampled": len(files),
            "brief_answers_its_question": answered,
        }
    return result


def probe_handle_addressability(project: Path, db: Path, g) -> dict:
    """F3 — can the caller ACT on what a reply names, without a
    transform it had to invent?

    A handle is addressable when pasting it into another tool works. A
    bare equation label is not: it must be prefixed with
    `math:equation:` by hand, which is a transform the emitter knows and
    the reader must guess.
    """
    eq_labels = [
        a.get("name") for n, a in g.nodes(data=True)
        if a.get("type") == "equation" and a.get("name")
    ][:50]
    addressable = sum(1 for lab in eq_labels if lab in g)
    docnames = [
        a.get("name") for n, a in g.nodes(data=True)
        if n.startswith("std:file:") and a.get("name")
    ][:50]
    resolvable = sum(
        1 for d in docnames
        if any((project / "docs" / f"{d}{ext}").exists() for ext in (".rst", ".md"))
    )
    return {
        "equation_labels_usable_as_ids": f"{addressable}/{len(eq_labels)}",
        "docnames_resolvable_without_guessing_ext": f"{resolvable}/{len(docnames)}",
    }


def probe_chains(g) -> dict:
    """F8 — does an answer hand you the NEXT call?

    Ergonomics is not only "can I reach this tool". It is "can I reach
    the tool this one chains to, using what this one gave me". A chain
    closes when every hop's output is directly acceptable as the next
    hop's input; it is BROKEN when a hop needs a hand transform, an
    external tool, or a fact the reply withheld.

    Each chain below is a question a real session asked in the founding
    round. `closes` is the metric; `blocked_by` names the class.
    """
    chains = []

    # 1. "I am editing this file — who breaks?"  file -> node -> callers
    #    There is no file-addressed MCP tool: `node_at` needs a LINE.
    has_file_tool = False           # no `file_brief` in the MCP registry
    chains.append({
        "chain": "file -> node -> callers",
        "asks": "I am editing this file; who breaks?",
        "closes": has_file_tool,
        "blocked_by": None if has_file_tool else
        "F6: no file-addressed tool; node_at needs a line you do not have yet",
    })

    # 2. "What verifies this equation, and can I run it?"
    eqs = [n for n, a in g.nodes(data=True) if a.get("type") == "equation"]
    reachable = 0
    runnable = 0
    for eq in eqs[:200]:
        tests = [u for u, _, d in g.in_edges(eq, data=True)
                 if d.get("type") == "tests"]
        if not tests:
            continue
        reachable += 1
        # runnable == the reply hands over a pytest node id. It does not;
        # every consumer re-derives it from file_path + the dotted name.
        emits_pytest_id = False
        runnable += bool(emits_pytest_id)
    chains.append({
        "chain": "equation -> tests -> pytest invocation",
        "asks": "what pins this equation, and can I run it?",
        "closes": runnable == reachable and reachable > 0,
        "measured": f"{reachable} equations reach their tests, "
                    f"{runnable} hand over a runnable id",
        "blocked_by": None if runnable == reachable else
        "F3: node ids are emitted, pytest ids are not (one string-join short)",
    })

    # 3. "What documents this symbol — which SECTION do I open?"
    sections = {n for n, a in g.nodes(data=True) if a.get("type") == "section"}
    eq_to_section = sum(
        1 for u, v, d in g.edges(data=True)
        if d.get("type") == "contains" and u in sections and v in set(eqs)
    )
    chains.append({
        "chain": "symbol -> doc page -> section",
        "asks": "what documents this, and where exactly?",
        "closes": eq_to_section > 0,
        "measured": f"{len(sections)} sections, {eq_to_section} section->equation edges",
        "blocked_by": None if eq_to_section else
        "F8: nested 2026-08-17 — 849 section->equation edges, "
        "869/903 equations carry an anchor (was 0)",
    })

    # 4. "A brief named an equation — ask the graph about it."
    labels = [a.get("name") for n, a in g.nodes(data=True)
              if a.get("type") == "equation" and a.get("name")][:50]
    pasteable = sum(1 for lab in labels if lab in g)
    chains.append({
        "chain": "brief label -> graph node",
        "asks": "the injection named this; tell me about it",
        "closes": bool(labels) and pasteable == len(labels),
        "measured": f"{pasteable}/{len(labels)} labels paste directly as ids",
        "blocked_by": None if pasteable == len(labels) else
        "F3: bare label needs a `math:equation:` prefix the reader must guess",
    })

    closed = sum(1 for c in chains if c["closes"])
    return {"closed": f"{closed}/{len(chains)}", "chains": chains}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--project", required=True, type=Path)
    ap.add_argument("--db", type=Path, default=None)
    ap.add_argument("--json", action="store_true", help="machine-readable")
    args = ap.parse_args()

    project = args.project.expanduser().resolve()
    db = args.db or project / ".nexus" / "graph.db"
    if not db.exists():
        print(f"no graph at {db}", file=sys.stderr)
        return 2

    kg = load_sqlite(str(db))
    q = GraphQuery(kg)
    g = kg.nxgraph

    report = {
        "project": str(project),
        "nodes": g.number_of_nodes(),
        "edges": g.number_of_edges(),
        "F1_false_zeros": probe_false_zeros(g),
        "F2_coverage_inversion": probe_coverage_inversion(g),
        "F5_declared_vs_inferred": probe_declared_vs_inferred(g),
        "F6_answer_payload": probe_answer_payload(q, g),
        "F4_brief_by_file_kind": probe_brief_by_file_kind(project, db, g),
        "F3_handle_addressability": probe_handle_addressability(project, db, g),
        "F8_chains": probe_chains(g),
    }

    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    print(f"# fidelity · {project.name} · {report['nodes']} nodes / "
          f"{report['edges']} edges\n")
    f1 = report["F1_false_zeros"]
    print(f"F1 false zeros      {f1['zero_callers']}/{f1['code_nodes']} "
          f"({f1['zero_callers_pct']}%) code nodes have no caller; "
          f"mistyped {f1['mistyped_class_scope']}, "
          f"dispatch-suspect {f1['dispatch_suspect']}")
    f2 = report["F2_coverage_inversion"]
    print(f"F2 coverage         {f2['untested']}/{f2['equations_implemented']} "
          f"({f2['untested_pct']}%) implemented equations have NO catcher")
    for w in f2["worst_modules"]:
        print(f"                      {w['untested']:>3} of {w['of']:>3}  {w['module']}")
    f5 = report["F5_declared_vs_inferred"]
    print(f"F5 declared:inferred  1 : {f5['inferred_per_declared']}  "
          f"({f5['declared_edges']} declared, {f5['inferred_edges']} inferred)")
    print("F6 payload")
    for h in report["F6_answer_payload"]["hubs"]:
        print(f"                      {h['bytes_per_entry']:>4} B/entry  "
              f"{h['entries_after_fold']:>4} entries from {h['raw_edges']:>5} edges  "
              f"{h['node'].split(':')[-1][:44]}")
    f4 = report["F4_brief_by_file_kind"]
    for kind, v in f4.items():
        print(f"F4 brief ({kind:10}) answers its question for "
              f"{v['brief_answers_its_question']}/{v['sampled']} files")
    f3 = report["F3_handle_addressability"]
    print(f"F3 handles          eq labels usable as ids "
          f"{f3['equation_labels_usable_as_ids']}; docnames resolvable "
          f"{f3['docnames_resolvable_without_guessing_ext']}")
    f8 = report["F8_chains"]
    print(f"F8 chains close     {f8['closed']}")
    for c in f8["chains"]:
        mark = "OK  " if c["closes"] else "BREAK"
        print(f"                    {mark} {c['chain']}")
        if c.get("measured"):
            print(f"                          {c['measured']}")
        if c.get("blocked_by"):
            print(f"                          ⤷ {c['blocked_by']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
