"""Helper chain for multi-hop verification_coverage tests."""
import math


class Mesh:
    """Toy 1D mesh. Exists to exercise nexus#3 re-export
    canonicalization — the canonical class lives here and is
    re-exported from ``solver_pkg/__init__.py``."""

    def __init__(self, size: int = 10):
        self.size = size

    def cell_count(self) -> int:
        return self.size


def _exp_decay(x):
    """Pure math helper — no equation label here, intentional."""
    return math.exp(-x)


def run_case(case):
    """Drive :func:`solver_pkg.solver.solve_attenuation` from a test
    harness. Exists so multi-hop verification_coverage has a real
    helper layer to traverse."""
    from .solver import solve_attenuation
    return solve_attenuation(**case)
