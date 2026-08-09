Misused Relations
=================

Nothing labelled precedes this, and no ``:label:`` names a source:

.. discretizes:: some-continuous-form

.. math::
   :label: eq-a

   x = 1

Binds to ``eq-a`` above, which is also the target:

.. derives-from:: eq-a

A target that does not exist anywhere in the project:

.. approximates:: never-written
   :label: eq-a
