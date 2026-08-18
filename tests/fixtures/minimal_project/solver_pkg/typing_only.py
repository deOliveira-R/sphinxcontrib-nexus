"""A module that exists ONLY to be referenced from annotations.

Nothing imports it at runtime — every importer guards it with
``if TYPE_CHECKING:``. That makes it the fixture's witness for
nexus#88: the IMPORTS edge pointing here is the one that must carry
``type_checking = true``, and it is unambiguous because no runtime
edge to this module exists to be confused with it.
"""

from typing import Protocol


class FluxProfile(Protocol):
    """Structural type for anything with a per-cell flux array."""

    def cell_count(self) -> int: ...
