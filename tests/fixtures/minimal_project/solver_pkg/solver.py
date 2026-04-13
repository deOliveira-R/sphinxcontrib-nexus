"""Toy solver functions with :math: docstring refs."""
from solver_pkg import Mesh  # re-export path — exercises nexus#3
from .helpers import _exp_decay


def build_mesh(size: int = 10) -> Mesh:
    """Constructor call through the re-export path.

    ``Mesh`` is imported as ``solver_pkg.Mesh`` but its canonical
    node is ``solver_pkg.helpers.Mesh``. The canonicalization pass
    must fold the re-export phantom so this function's CALLS edge
    ends up pointing at the canonical class.
    """
    return Mesh(size=size)


def solve_attenuation(psi_in, sigma_t, length, mu):
    """Evaluate :math:`fixture-attenuation` for a single track.

    Closed-form exponential attenuation along a straight path.
    """
    return psi_in * _exp_decay(sigma_t * length / mu)


def solve_balance(leakage, absorption, source):
    """Enforce :math:`fixture-balance` — L + A == Q.

    Decomposes into :math:`fixture-leakage` and :math:`fixture-absorption`
    contributions implicitly.
    """
    return leakage + absorption - source


def solve_keff(nu_sigma_f, sigma_a):
    """Compute :math:`fixture-keff` for a homogeneous medium."""
    return nu_sigma_f / sigma_a
