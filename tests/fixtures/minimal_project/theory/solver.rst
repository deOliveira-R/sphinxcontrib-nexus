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

A third equation completes the family, and it is the one nothing can
implement. Same page, same shared token, same guesser — so it collects a
guess exactly like the other two, and the ONLY thing that stands it down
is the declaration below.

.. math::
   :label: fixture-mesh-widths-sum

   \sum_i \Delta x_i = L

.. no-implementation:: fixture-mesh-widths-sum
   :kind: identity

   A statement of what the mesh's widths equal, not a computation any
   symbol performs. ``build_mesh`` produces a mesh for which this holds;
   it does not implement the identity, and neither does anything else.

The error catalogue
-------------------

Two entries, and the pairing is the point. ``FM-01`` is named by
``test_attenuation_vacuum_source``'s ``@pytest.mark.catches``, so
declaring it here turns that marker from a string into a real
``catches`` edge. ``FM-99`` is declared and named by nothing, so it is
the UNCAUGHT case — which is the finding ``errors`` exists to report and
the question ``nexus#63`` was filed for.

Both have to live in a real build: the edge is written by
``merge.write_catches_edges`` only after ``apply_declared_nodes`` has
minted the node, and nothing else pins that order.

.. error-entry:: FM-01
   :title: Attenuation with a vacuum inlet returns a non-zero flux

   The marker on ``test_attenuation_vacuum_source`` resolves here.

.. error-entry:: FM-99
   :title: A catalogued defect that no test claims

   Deliberately uncaught. If a test ever carries
   ``@pytest.mark.catches("FM-99")``, the ``uncaught`` assertion in
   ``test_fixture_e2e.py`` is what will notice.
