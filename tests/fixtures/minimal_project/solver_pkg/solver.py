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
    """Compute :math:`fixture-keff` for a homogeneous medium.

    The medium multiplies when :math:`k > 1`, is critical at :math:`k = 1`,
    and the flux lives on :math:`[0, R]`. Those three are inline MATH: they
    typeset an expression and name no equation, so none may become a
    reference. None carries a backslash or a brace, which is exactly why the
    old blocklist admitted all three.

    A body that wraps across source
    lines like :math:`a_0
    > 0` is the same thing with a newline in it, and is how an id came to
    carry whitespace.

    The control is :math:`fixture-balance`, which names a label that really
    is declared — the forgiving route must keep working for it.
    """
    return nu_sigma_f / sigma_a
