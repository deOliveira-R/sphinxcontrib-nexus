Numbered Cross-References
=========================

.. _mesh-overview:

Mesh overview
-------------

A labelled section. ``:numref:`` cannot number a plain section — Sphinx
warns — so it is referenced with ``:ref:`` here.

.. figure:: mesh.png
   :name: fig-mesh

   The spatial mesh.

.. table:: Quadrature orders
   :name: tab-quadrature

   ===  ===
   S_N  n
   ===  ===
   S2   2
   S4   4
   ===  ===

.. math::
   :label: transport-balance

   \Omega \cdot \nabla \psi + \Sigma_t \psi = q

References
----------

The mesh is shown in :numref:`fig-mesh`, its orders in
:numref:`tab-quadrature`, and the overview is :ref:`mesh-overview`.
The balance is :math:numref:`transport-balance`.
