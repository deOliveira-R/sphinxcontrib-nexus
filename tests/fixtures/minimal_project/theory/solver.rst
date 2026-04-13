Solver theory
=============

.. autoclass:: solver_pkg.helpers.Mesh

.. autofunction:: solver_pkg.solver.solve_attenuation

.. autofunction:: solver_pkg.solver.build_mesh

Attenuation
-----------

.. math::
   :label: fixture-attenuation

   \psi_\text{out} = \psi_\text{in} \cdot \exp(-\Sigma_t L / \mu)

Balance
-------

.. math::
   :label: fixture-balance

   L + A = Q

k-effective
-----------

.. math::
   :label: fixture-keff

   k = \nu\Sigma_f / \Sigma_a

.. implements:: fixture-keff
   :by: solver_pkg.solver.solve_keff

.. verifies:: fixture-attenuation
   :by: solver_tests.test_solver.test_end_to_end_via_helper_chain

   Directive-sourced verification edge added on top of the
   ``@pytest.mark.verifies`` marker for coverage of the test →
   equation path from prose rather than code.
