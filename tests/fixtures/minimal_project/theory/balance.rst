Balance components
==================

Leakage
-------

.. math::
   :label: fixture-leakage

   L = \sum_\text{faces} J \cdot \hat{n}

Absorption
----------

.. math::
   :label: fixture-absorption

   A = \Sigma_a \phi V

Python-domain roles from prose
------------------------------

These exist so a node id built on the doctree path can be compared with one
built by the docstring scanner. The roles below name targets Sphinx cannot
resolve, which is the branch that forges an id from the reftype:
:func:`solver_pkg.absent.compute_leakage` (project-internal),
:meth:`solver_pkg.helpers.Mesh.absent_method`,
:attr:`solver_pkg.helpers.Mesh.absent_attr`, and
:func:`numpy.absent_function` (external).

The control is a role that DOES resolve —
:func:`solver_pkg.solver.solve_attenuation` — which must keep pointing at the
autodoc'd node rather than gaining a phantom of its own.

And one whose body WRAPS across source lines, which is how a newline reaches
an id builder as raw text: :meth:`solver_pkg.helpers.Mesh.absent_
wrapped_method` must be spelled without the wrap.
