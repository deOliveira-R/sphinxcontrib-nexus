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

__all__ = ["Mesh"]
