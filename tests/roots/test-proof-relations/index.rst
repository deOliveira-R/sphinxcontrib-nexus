Discrete Ordinates
==================

The continuous transport equation:

.. math::
   :label: transport-continuous

   \Omega \cdot \nabla \psi + \Sigma_t \psi = q

Specializing to a discrete angular quadrature:

.. math::
   :label: transport-sn

   \Omega_n \cdot \nabla \psi_n + \Sigma_t \psi_n = q_n

.. derives-from:: transport-continuous

The diamond-difference closure, with no explicit ``:label:`` on the
directive so it binds to the equation directly above it:

.. math::
   :label: dd-closure

   \psi_c = \frac{1}{2} (\psi_L + \psi_R)

.. discretizes:: transport-sn

   The cell-centred flux is the arithmetic mean of the two faces.

A closure that stands in for the exact scattering integral, declared out
of order via an explicit ``:label:``:

.. math::
   :label: p1-closure

   \Sigma_s \phi_1 \approx \frac{1}{3} \nabla \phi_0

.. approximates:: transport-continuous
   :label: p1-closure

Typed environments
------------------

.. prf:definition:: Angular flux
   :label: def-angular-flux

   The angular flux :math:`\psi(\mathbf{r}, \Omega, E)` is the number of
   particles crossing unit area per unit solid angle per unit energy.

.. prf:theorem:: Transport balance
   :label: thm-balance

   Given :prf:ref:`def-angular-flux`, the balance in
   :eq:`transport-continuous` holds over any control volume.

.. derives-from:: def-angular-flux
   :label: thm-balance

.. prf:algorithm:: Transport sweep
   :label: alg-sweep

   Order the cells along :math:`\Omega_n` and solve each in turn.

.. prf:remark::

   An unlabelled environment. sphinx-proof gives it a serial-numbered
   synthetic label, and nothing can reference it.

Implementation
--------------

.. py:function:: solver.sweep(psi, sigma_t)

   Run one transport sweep.

.. implements:: dd-closure
   :by: solver.sweep
