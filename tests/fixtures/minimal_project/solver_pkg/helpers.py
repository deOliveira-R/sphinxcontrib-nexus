"""Helper chain for multi-hop verification_coverage tests."""
import math


def _exp_decay(x):
    """Pure math helper — no equation label here, intentional."""
    return math.exp(-x)


def run_case(case):
    """Drive :func:`solver_pkg.solver.solve_attenuation` from a test
    harness. Exists so multi-hop verification_coverage has a real
    helper layer to traverse."""
    from .solver import solve_attenuation
    return solve_attenuation(**case)
