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

Mesh spacing
------------

The two equations below exist to exercise the *inference*, which nothing
else in this fixture reaches: they share the token ``mesh`` with
``solver_pkg.helpers.Mesh``, the one code symbol this page DOCUMENTS (as
opposed to merely contains), so each attracts a guess.

One of them is then declared, and the other is not. That pairing is the
end-to-end witness for the equation-level stand-down — see
``test_fixture_e2e.py``. It has to live in a real build because the
behaviour depends on ORDER: declarations are applied before the
inference runs, and nothing else pins that.

.. math::
   :label: fixture-mesh-count

   N_\text{cells} = L / \Delta x

.. math::
   :label: fixture-mesh-spacing

   \Delta x = L / N_\text{cells}

.. implements:: fixture-mesh-spacing
   :by: solver_pkg.solver.build_mesh

   Declared deliberately onto a DIFFERENT symbol than the one the
   inference would have guessed (``Mesh``), so the assertion can tell a
   stood-down guess from a coincidence.
