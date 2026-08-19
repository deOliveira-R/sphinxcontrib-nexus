"""Score the `implements` inference against a hand-verified ground truth.

The token-matching inference has never had a labelled set, so "how good
is it?" has only ever been answered by eyeballing a few rows. This is
the labelled set and the scorer.

**Provenance.** 82 true implementers across 56 equations on two ORPHEUS
theory pages — `theory/methods/sn/loss_representation` (17 equations)
and `theory/foundations/operator_algebra` (39; `keff-as-integrated-rates`
is deliberately absent, its equation being present-tense false at the
time). Each was determined by reading the equation TEXT first and then
the code, by two `explorer` agents, 2026-08-17, against ORPHEUS
`22542238`. The existing inferred edges were used only as a candidate
pool to triage, never as evidence.

**An empty list is data, not a gap.** 11 of the 56 equations have NO
implementer and are recorded as `[]` — they are identities, laws
enforced by absence, canonical forms nothing realizes, or definitions.
Guesses aimed at them are wrong by construction, and counting them is
half the point.

⚠ **This file rots against ORPHEUS.** A truth that no longer resolves to
a node means the symbol was renamed or retired, NOT that the inference
improved — so the scorer refuses to report a score until every truth
resolves, rather than silently scoring a shrinking set. That failure
mode (an instrument that reports progress when it has merely gone blind)
is the one this whole eval directory exists to catch.

Usage::

    python evals/implements_ground_truth.py <path-to-graph.db>
    python evals/implements_ground_truth.py <db> --variants
"""
import argparse
import collections
import pathlib
import re
import sys

from sphinxcontrib.nexus.export import load_sqlite
from sphinxcontrib.nexus.graph import NO_IMPLEMENTATION_ATTR

CODE_TYPES = {"function", "method", "class"}

TRUTH = {
    "loss-rep-LpC": [
        "sn.operators.streaming.StreamingCollisionOperator",
        "sn.operators.streaming.StreamingOperator",
    ],
    "loss-rep-resolution-a": [
        "sn.loss_representation._LossRepresentation.streaming_action",
        "sn.operators.streaming.StreamingOperator.apply",
        "sn.operators.streaming.StreamingCollisionOperator.apply",
        "sn.operators.streaming.StreamingCollisionOperator.apply_transpose",
    ],
    "loss-rep-affine-cell": [
        "transport.spatial.diamond.DiamondDifference.residual_kernel_batch",
        "sn.loss_representation._OneDimScanWalk._apply_walk",
    ],
    "loss-rep-affine": [
        "sn.loss_representation._LossRepresentation.streaming_action",
        "sn.loss_representation._LossRepresentation.streaming_action_transpose",
    ],
    "loss-rep-leaf-sum": [],
    "loss-rep-removal-sigma": [],
    "loss-rep-facewise-separable": [],
    "loss-rep-affine-kernel-maps": [
        "sn.loss_representation.assembly._probe_coefficient_blocks",
        "transport.spatial.diamond.DiamondDifference.residual_kernel_batch",
        "transport.spatial.linear_discontinuous.LinearDiscontinuous.residual_kernel_batch",
    ],
    "loss-rep-walk-order-rows": [
        "sn.loss_representation.assembly.assemble_ordinate_blocks",
    ],
    "loss-rep-sweep-global-conjugation": [
        "sn.loss_representation.assembly.assemble_ordinate_blocks",
    ],
    "loss-rep-scanmarch": [
        "sn.loss_representation.ScanMarch",
        "sn.loss_representation.ScanMarch._sweep_interior",
        "sn.loss_representation.ScanMarch._loss_action_interior",
    ],
    "loss-rep-scanmarch-solve": [
        "transport.spatial.diamond.DiamondDifference._cartesian_streaming_diagonal",
    ],
    "loss-rep-scanmarch-solve-affine": [
        "transport.spatial.diamond.DiamondDifference.cartesian_scan_coefficients",
        "transport.spatial.scheme.DiscretizationSchemeBase.source_emission",
        "sn.loss_representation.ScanMarch._sweep_interior",
    ],
    "loss-rep-scanmarch-apply": [
        "transport.spatial.diamond.DiamondDifference._reflection_coeffs",
        "transport.spatial.diamond.DiamondDifference.reflect_scan_coefficients",
    ],
    "loss-rep-scanmarch-apply-residual": [
        "transport.spatial.diamond.DiamondDifference.residual_kernel_batch",
    ],
    "loss-rep-adjoint-inverse-swap": [
        "numerics.operator._AdjointOperator.inverse",
    ],
    "loss-rep-metric-adjoint-solve": [
        "numerics.operator._AdjointOperator.apply",
        "sn.operators.sweep_operator.SweepOperator.apply_transpose",
    ],
}

# Second labelled page: `theory/foundations/operator_algebra`.
# `keff-as-integrated-rates` is deliberately ABSENT — its equation is
# present-tense false at HEAD, so it has no defensible truth value yet.
TRUTH_OPALG = {
    "operator-apply": ["numerics.operator.LinearOperator.apply"],
    "operator-solve": [],
    "operator-apply-transpose": ["operator.SupportsAdjoint.apply_transpose"],
    "operator-fixed-source": [
        "sn.solver.SNSolver._solve_source_iteration",
        "sn.solver.SNSolver._solve_krylov",
    ],
    "operator-eigenvalue": [
        "sn.solver.SNSolver.compute_fission_source",
        "iteration.KEigenvalue.compute_fission_source",
    ],
    "operator-within-group-composition": [
        "coupled_system.build_within_group_system",
    ],
    "diagonal-operator-action": ["operator.DiagonalOperator.apply"],
    "multiplication-operator-action": [
        "MultiplicationOperator._apply_impl",
    ],
    "multiplication-operator-embedding": [
        "multiplication_operator.MultiplicationOperator",
    ],
    "inverse-as-operator": [
        "operator.InverseOperator.apply",
        "sweep_operator.SweepOperator.apply",
    ],
    "carrier-grid-operator-typing": ["numerics.operator.LinearOperator"],
    "apply-solve-source-iteration-series": ["iteration.SourceIteration.solve"],
    "apply-solve-within-group-balance": [
        "streaming.StreamingCollisionOperator",
    ],
    "apply-solve-cell-resolvent": [
        "diamond.DiamondDifference.update",
        "diamond.DiamondDifference.cell_kernel_batch",
        "diamond.DiamondDifference.affine_scan_coefficients",
        "diamond.DiamondDifference.cartesian_scan_coefficients",
    ],
    "streaming-action-cell-balance": [
        "cell_balance.cell_balance_for_streaming",
        "cell_balance.cell_balance_terms",
        "diamond.DiamondDifference._cartesian_streaming_diagonal",
        "diamond.DiamondDifference.affine_scan_coefficients",
        "diamond.DiamondDifference.residual_kernel_batch",
    ],
    "harmonic-frame-is-galerkin": [
        "harmonic_frame.HarmonicFrame",
        "harmonic_frame.HarmonicFrame.from_galerkin",
    ],
    "streaming-action-pure-l": [
        "_LossRepresentation.streaming_action",
    ],
    "product-solve-reroute": ["operator.OperatorProduct.solve"],
    "tensor-product-space-agreement": [
        "numerics.operator._agreed_space",
        "operator.TensorProductOperator.domain",
        "operator.TensorProductOperator.codomain",
    ],
    "scattering-as-tensor-product-sum": [
        "scattering.LegendreMomentScattering",
        "scattering.LegendreMomentScattering.apply",
    ],
    "production-rate-functional": [
        "reaction_rate_functional.ReactionRateFunctional",
    ],
    "apply-distributes": ["operator.OperatorSum.apply"],
    "scattering-carrier-grid": [
        "harmonic_frame.HarmonicFrame.analyse",
        "scattering.LegendreMomentScattering.apply",
        "harmonic_frame.HarmonicFrame.reconstruct",
    ],
    "scattering-aniso-composite": [
        "ScatteringOperator.build_aniso_source",
        "ScatteringOperator.kernel",
        "ScatteringOperator._apply_impl",
    ],
    "reaction-rate-kinf-oracle": [
        "eigenvalue._infinite_medium_matrices",
        "eigenvalue.kinf_homogeneous",
        "eigenvalue.kinf_and_spectrum_homogeneous",
    ],
    "fission-as-dyad": [
        "fission.FissionOperator.kernel",
        "fission.FissionOperator.production_rate",
    ],
    "trace-half-decomposition": [
        "AngularTraceSpace.inflow_indices_for_face",
        "AngularTraceSpace.outflow_indices_for_face",
    ],
    "per-face-inflow-mask": [
        "angular_trace_space.build_omega_dot_n",
        "AngularTraceSpace.inflow_indices_for_face",
    ],
    "integral-kernel-category": [
        "IntegralKernelOperator.kernel",
    ],
    "tensor-product-adjoint-distributivity": [
        "operator.TensorProductOperator.apply_transpose",
    ],
    "tensor-product-inverse": ["operator.TensorProductOperator.inverse"],
    "tensor-product-action": ["operator.TensorProductOperator.apply"],
    "apply-solve-parallel-identity": [],
    "apply-solve-neumann-series": [],
    "apply-solve-neumann-expansion": [],
    "apply-solve-denominator-inequality": [],
    "solve-does-not-distribute": [],
    "streaming-as-tensor-product-sum": [],
    "carrier-grid-cell": [],
}

TRUTH.update(TRUTH_OPALG)



def tok(name: str) -> set[str]:
    """The shipped tokenizer: split on separators, keep tokens >= 3 chars."""
    return {t for t in re.split(r"[-_.:]+", name.lower()) if len(t) >= 3}


def leaf(name: str) -> str:
    """The symbol's own name — `A.B.Cls.meth` -> `Cls.meth`.

    A method's identity is its class AND its name; dropping to the bare
    method name over-merges every `apply` in the tree.
    """
    parts = name.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else name


def resolve(g) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """Bind each truth to its node id. Returns (bound, unresolved)."""
    py_nodes = [n for n in g.nodes if n.startswith("py:")]
    bound, unresolved = {}, []
    for label, truths in TRUTH.items():
        for t in truths:
            hits = [n for n in py_nodes if n.split(":", 2)[-1].endswith(t)]
            if len(hits) == 1:
                bound[f"{label}||{t}"] = hits[0]
            else:
                unresolved.append((label, f"{t} ({len(hits)} matches)"))
    return bound, unresolved


def infer(g, tokens_of, min_shared: int) -> dict[str, set[str]]:
    """Re-run the inference with a pluggable tokenizer and threshold."""
    out: dict[str, set[str]] = collections.defaultdict(set)
    for doc_id, attrs in g.nodes(data=True):
        if attrs.get("type") != "file":
            continue
        eqs, codes = {}, {}
        for _, tgt, d in g.out_edges(doc_id, data=True):
            ta = g.nodes.get(tgt, {})
            ttype, tname = ta.get("type", ""), ta.get("name", "")
            if ttype == "equation":
                eqs.setdefault(tgt, tok(tname))
            elif (
                ttype in CODE_TYPES
                and d.get("type") == "documents"
                and not ta.get("in_test_file")
            ):
                codes.setdefault(tgt, tokens_of(tname))
        for cid, ctoks in codes.items():
            for eid, etoks in eqs.items():
                if len(ctoks & etoks) >= min_shared:
                    out[eid].add(cid)
    return out


def score(guesses: dict[str, set[str]]) -> dict:
    """Precision and recall over the labelled set."""
    tp = fp = fn = 0
    disjoint = none_eq_guesses = 0
    for label, truths in TRUTH.items():
        got = {n.split(":", 2)[-1] for n in guesses.get(f"math:equation:{label}", set())}
        hits = {t for t in truths if any(g.endswith(t) for g in got)}
        tp += len(hits)
        fn += len(truths) - len(hits)
        fp += len(got) - len(hits)
        if truths and not hits:
            disjoint += 1
        if not truths:
            none_eq_guesses += len(got)
    return {
        "true_positives": tp,
        "false_positives": fp,
        "false_negatives": fn,
        "precision_pct": round(100 * tp / max(tp + fp, 1), 1),
        "recall_pct": round(100 * tp / max(tp + fn, 1), 1),
        "equations_with_disjoint_pool": disjoint,
        "guesses_on_unimplementable_equations": none_eq_guesses,
    }


VARIANTS = (
    ("A  full dotted path, >=1 token  (SHIPPED)", tok, 1),
    ("B  symbol's own name, >=1 token", lambda n: tok(leaf(n)), 1),
    ("C  symbol's own name, >=2 tokens", lambda n: tok(leaf(n)), 2),
    ("D  full dotted path, >=2 tokens", tok, 2),
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("db", type=pathlib.Path)
    ap.add_argument("--variants", action="store_true",
                    help="also score hypothetical narrowing rules")
    args = ap.parse_args()

    g = load_sqlite(args.db).nxgraph
    _bound, unresolved = resolve(g)
    n_truths = sum(len(v) for v in TRUTH.values())

    if unresolved:
        print(f"⛔ GROUND TRUTH IS STALE — {len(unresolved)} of {n_truths} "
              f"truths no longer resolve to exactly one node.\n"
              f"   Renamed or retired symbols make the score meaningless; "
              f"a shrinking labelled set reads as improvement.\n"
              f"   Re-derive the affected rows before trusting any number.\n")
        for label, what in unresolved:
            print(f"     {label:44s} {what}")
        return 1

    print(f"# implements inference vs ground truth · "
          f"{n_truths} truths / {len(TRUTH)} equations "
          f"({sum(1 for v in TRUTH.values() if not v)} with no implementer)\n")

    if not args.variants:
        # TWO questions, deliberately separated — conflating them makes a
        # SUCCESSFUL declaration campaign read as the rule getting worse.
        #
        # (1) How good is the RULE? A property of the rule, so it is
        #     simulated over the whole corpus, ignoring declarations. It
        #     must not move when an author DECLARES.
        #     ⚠ It does legitimately move when an author writes PROSE,
        #     because the candidate pool is the set of symbols a page
        #     documents: adding an xref adds a candidate. `[M]` the
        #     first declaration pass raised the guesses on the three
        #     equations it did NOT declare, 23 -> 24/25/24, because the
        #     new prose cross-referenced a symbol in order to say it is
        #     NOT the implementer. An undeclared equation gets worse
        #     every time its page is improved.
        # (2) How much of the corpus is still GUESSED at? A property of
        #     the corpus, and the thing declaring is meant to move.
        #
        # Reading the graph's live `source="inferred"` edges answers (2)
        # and looks like (1). It reported `precision 0.0 %` the first
        # time declarations landed — not because the rule had degraded
        # but because the equations it scored well on had left the
        # population. Same defect as the F5 re-scope one commit earlier:
        # a metric whose denominator moved under it.
        print("## the RULE (simulated over the whole corpus, "
              "declarations ignored — must not move when authors declare)")
        r = score(infer(g, tok, 1))
        for k, v in r.items():
            print(f"  {k:38s} {v}")

        live = collections.defaultdict(set)
        declared_eqs = set()
        for s, t, d in g.edges(data=True):
            if d.get("type") != "implements":
                continue
            if d.get("source") == "inferred":
                live[t].add(s)
            else:
                declared_eqs.add(t)
        # ⛔ An equation can be ANSWERED without an edge. `.. no-
        # implementation::` (nexus#85) writes a node attribute, because
        # there is no second end for an edge to reach — so counting
        # `declared` off edges alone is a PROXY that #85 removes, and it
        # fails in the direction that reads as unfinished work: the
        # eleven equations this labelled set records as having NO
        # implementer would sit at "45 of 56" forever, however
        # thoroughly they were declared. Third instance of the same
        # shape in this campaign (`plan-authoring` §10), and the first
        # in the ground-truth scorer built to be the honest instrument.
        answered_nothing = {
            n for n, a in g.nodes(data=True)
            if a.get(NO_IMPLEMENTATION_ATTR)
        }
        labelled = {f"math:equation:{k}" for k in TRUTH}
        still_guessed = sum(len(v) for k, v in live.items() if k in labelled)
        by_edge = labelled & declared_eqs
        by_attr = labelled & answered_nothing
        print("\n## the CORPUS (what declaring has actually removed)")
        print(f"  {'labelled equations answered':38s} "
              f"{len(by_edge | by_attr)} of {len(labelled)}")
        print(f"  {'  …by a declared implementer':38s} {len(by_edge)}")
        print(f"  {'  …by `.. no-implementation::`':38s} {len(by_attr)} "
              f"of {sum(1 for v in TRUTH.values() if not v)} with none")
        print(f"  {'inferred edges left on them':38s} {still_guessed}")
        return 0

    print(f"{'variant':44s} {'edges':>7} {'prec':>7} {'recall':>7}  disjoint pools")
    for name, tokens_of, k in VARIANTS:
        gs = infer(g, tokens_of, k)
        r = score(gs)
        edges = sum(len(v) for v in gs.values())
        print(f"{name:44s} {edges:>7} {r['precision_pct']:>6}% "
              f"{r['recall_pct']:>6}%  {r['equations_with_disjoint_pool']:>3}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
