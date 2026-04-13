"""Toy solver functions with :math: docstring refs."""
from .helpers import _exp_decay


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
