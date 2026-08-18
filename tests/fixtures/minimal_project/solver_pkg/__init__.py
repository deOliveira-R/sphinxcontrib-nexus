"""Toy solver package for the nexus self-hosting fixture.

Re-exports ``Mesh`` from ``helpers`` so downstream code can write
``from solver_pkg import Mesh`` without reaching into the submodule.
This is the exact shape the nexus#3 re-export canonicalization
pass was built to collapse: after the build, only the canonical
``py:class:solver_pkg.helpers.Mesh`` should exist, not the
``py:class:solver_pkg.Mesh`` re-export duplicate.
"""

from typing import TYPE_CHECKING

from .helpers import Mesh

if TYPE_CHECKING:  # NOT a runtime public path — nexus#88
    from .typing_only import FluxProfile

    # ...and the BOTH-WAYS case: `.helpers` is imported at runtime just
    # above AND type-only here, so the two IMPORTS edges are the same
    # (source, target) pair differing only in `type_checking`. Without
    # that field on the entry they serialise identically and collapse
    # into `times: 2`, which asserts a sameness that does not hold.
    from .helpers import Mesh as _MeshForAnnotations

__all__ = ["Mesh"]
