"""Fixture tests exercising every pytest-marker feature ingested by
sphinxcontrib-nexus. Run as part of the Sphinx build under the
fixture project — NOT collected by the top-level test suite (the
outer ``conftest.py`` ignores ``tests/fixtures``)."""
import pytest

from solver_pkg.helpers import run_case
from solver_pkg.solver import solve_attenuation, solve_balance, solve_keff


@pytest.mark.l0
@pytest.mark.verifies("fixture-attenuation")
@pytest.mark.catches("FM-01")
def test_attenuation_vacuum_source():
    """L0: Verify :math:`fixture-attenuation` with vacuum inlet."""
    assert solve_attenuation(0.0, 1.0, 1.0, 1.0) == 0.0


@pytest.mark.l1
class TestL1Balance:
    """L1: Verify :math:`fixture-balance`."""

    @pytest.mark.verifies("fixture-balance")
    def test_balance_zero_residual(self):
        assert solve_balance(0.5, 0.5, 1.0) == 0.0


@pytest.mark.l1
@pytest.mark.verifies("fixture-keff", "fixture-leakage")
def test_keff_critical():
    """L1: Verify :math:`fixture-keff`."""
    assert solve_keff(1.0, 1.0) == pytest.approx(1.0)


@pytest.mark.l2
def test_end_to_end_via_helper_chain():
    """L2: test → helper → solver chain.

    No explicit verifies — exists so multi-hop verification_coverage
    has teeth against ``fixture-absorption``, which is reachable only
    via the calls chain, not via any pytest.mark.verifies label.
    """
    result = run_case(
        {"psi_in": 1.0, "sigma_t": 0.1, "length": 1.0, "mu": 1.0}
    )
    assert result > 0
