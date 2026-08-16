"""Project configuration read from ``.nexus/`` — shared by every surface.

Nexus has three surfaces and, until now, only one of them could be
configured. The Sphinx extension reads ``nexus_*`` options out of
``conf.py``; the CLI and the MCP server read *nothing* and take every
setting as a flag. So a project fact — which source prefixes are in
scope, where the error catalogue lives, whether ``implements`` should be
inferred — has no home that all three can see, and the CLI ends up
carrying the same flags on every invocation.

This module is that home. It reads ``.nexus/config.toml`` from the
project root and exposes the settings as ``None``-when-unset properties,
so a caller can apply the precedence

    explicit flag  >  .nexus/config.toml  >  conf.py option  >  default

with :func:`resolve`. Nothing here *applies* a setting; consumers do
that, which keeps the loader free of Sphinx and CLI imports and testable
on its own.

Why ``.nexus/`` and not ``pyproject.toml``: the directory also holds
``ontology.toml`` and (once built) the graph and its overlays, so the
project's whole graph surface sits in one shallow, obvious place rather
than being split between a table in ``pyproject.toml`` and artefacts
elsewhere. Being tracked in git, it is present in every worktree with no
scaffolding step.
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, TypeVar

logger = logging.getLogger(__name__)

#: The configuration directory's name, relative to the project root.
CONFIG_DIR = ".nexus"

#: The settings file inside :data:`CONFIG_DIR`.
CONFIG_NAME = "config.toml"

#: Every key this loader understands, by table. A key outside this map is
#: reported (see :meth:`ProjectConfig.unknown_keys`) rather than silently
#: dropped — a typo that is ignored without comment is the same defect as
#: an empty result that reads like a measurement.
KNOWN_KEYS: Mapping[str, frozenset[str]] = {
    "graph": frozenset({
        "output",
        "db",
        "extra_source_dirs",
        "exclude_patterns",
        "analyze_tests",
        "test_patterns",
        "infer_implements",
        "max_viz_nodes",
    }),
    "scope": frozenset({"prefixes"}),
    "catalog": frozenset({"errors"}),
}

#: Where the graph lands when neither a flag nor a config file says
#: otherwise. Relative to the working directory, which is why it is
#: almost never right for a real project — the artefacts sit under the
#: Sphinx output directory. Kept as the pre-config behaviour.
LEGACY_DB = Path("_nexus/graph.db")

T = TypeVar("T")


def resolve(*candidates: Any, default: T) -> T:
    """First non-``None`` candidate, else ``default``.

    Encodes the precedence chain at the call site, in order, so a reader
    sees which source wins without tracing the loader::

        resolve(cli_flag, cfg.infer_implements, sphinx_value, default=True)
    """
    for candidate in candidates:
        if candidate is not None:
            return candidate  # type: ignore[return-value]
    return default


def resolve_db(
    explicit: Path | str | None = None,
    start: Path | str | None = None,
) -> Path:
    """Which graph database to open: flag > config > legacy default.

    Shared by the CLI and the MCP server so the answer cannot differ
    between them — a server pointed at a different graph than the CLI is
    a confusing failure that looks like a stale graph.
    """
    if explicit is not None:
        return Path(explicit)
    declared = ProjectConfig.load(start or Path.cwd()).resolved_db()
    return declared if declared is not None else LEGACY_DB


def find_project_root(start: Path | str) -> Path | None:
    """Nearest ancestor of ``start`` (inclusive) containing ``.nexus/``.

    Walks upward so the CLI works from any subdirectory, the way ``git``
    does. Returns ``None`` when no configuration directory exists, which
    is a supported state — nexus must keep working on an unconfigured
    project.
    """
    current = Path(start).resolve()
    for candidate in (current, *current.parents):
        if (candidate / CONFIG_DIR).is_dir():
            return candidate
    return None


@dataclass(frozen=True)
class ProjectConfig:
    """Settings read from ``.nexus/config.toml``.

    Every accessor returns ``None`` when the key was not set, so callers
    can distinguish "the project chose this value" from "the project said
    nothing" — a distinction the precedence chain needs and a plain
    defaulted dataclass would destroy.
    """

    #: Directory containing ``.nexus/``. Relative paths resolve against it.
    root: Path
    #: The file actually read, or ``None`` when absent (defaults only).
    source: Path | None
    #: Parsed TOML, tables intact.
    data: Mapping[str, Any]

    # -- construction ----------------------------------------------------

    @classmethod
    def load(cls, start: Path | str) -> "ProjectConfig":
        """Load the config governing ``start``.

        Never raises for a missing directory or file — an unconfigured
        project yields an empty config whose accessors all return
        ``None``. A malformed TOML file *does* raise: a settings file that
        cannot be parsed is a mistake to fix, not a state to tolerate.
        """
        root = find_project_root(start)
        if root is None:
            return cls(root=Path(start).resolve(), source=None, data={})

        path = root / CONFIG_DIR / CONFIG_NAME
        if not path.is_file():
            return cls(root=root, source=None, data={})

        with path.open("rb") as handle:
            data = tomllib.load(handle)

        config = cls(root=root, source=path, data=data)
        for table, key in config.unknown_keys():
            logger.warning(
                "%s: unknown key %r in [%s] — known keys: %s",
                path,
                key,
                table,
                ", ".join(sorted(KNOWN_KEYS.get(table, frozenset()))) or "(none)",
            )
        return config

    # -- introspection ---------------------------------------------------

    def unknown_keys(self) -> list[tuple[str, str]]:
        """``(table, key)`` pairs this loader does not understand.

        Includes keys in unknown tables, so a mistyped table name is
        reported rather than passing silently as an empty section.
        """
        found: list[tuple[str, str]] = []
        for table, entries in self.data.items():
            if not isinstance(entries, dict):
                found.append(("<root>", table))
                continue
            known = KNOWN_KEYS.get(table)
            if known is None:
                found.extend((table, key) for key in entries)
                continue
            found.extend((table, key) for key in entries if key not in known)
        return found

    def _get(self, table: str, key: str) -> Any:
        section = self.data.get(table)
        if not isinstance(section, dict):
            return None
        return section.get(key)

    # -- [graph] ---------------------------------------------------------

    @property
    def output(self) -> str | None:
        """Directory name the graph artefacts are written to."""
        return self._get("graph", "output")

    @property
    def db(self) -> str | None:
        """Project-relative path to the graph database.

        The CLI and the MCP server cannot derive this: the artefacts land
        under the *Sphinx output directory*, which only the build knows.
        So it is stated once here rather than retyped as ``--db`` on every
        invocation. The extension warns when what it wrote does not match
        what this declares, which is the only way the two can disagree.
        """
        return self._get("graph", "db")

    def resolved_db(self) -> Path | None:
        path = self.db
        return None if path is None else (self.root / path).resolve()

    @property
    def extra_source_dirs(self) -> list[str] | None:
        return self._get("graph", "extra_source_dirs")

    @property
    def exclude_patterns(self) -> list[str] | None:
        return self._get("graph", "exclude_patterns")

    @property
    def analyze_tests(self) -> bool | None:
        return self._get("graph", "analyze_tests")

    @property
    def test_patterns(self) -> list[str] | None:
        return self._get("graph", "test_patterns")

    @property
    def infer_implements(self) -> bool | None:
        """Whether to infer ``implements`` from shared name tokens.

        Worth setting explicitly. Inferred edges land at
        ``confidence=0.7`` and, on a large corpus, the large majority rest
        on a *single* shared token — which is a search relation, not a
        proof one. A project that declares ``.. implements::`` should turn
        inference off rather than let the two mix under one edge type.
        """
        return self._get("graph", "infer_implements")

    @property
    def max_viz_nodes(self) -> int | None:
        return self._get("graph", "max_viz_nodes")

    # -- [scope] ---------------------------------------------------------

    @property
    def scope_prefixes(self) -> list[str] | None:
        """Path prefixes considered in-scope for runtime ingestion.

        A *list*, deliberately. A single prefix cannot express the common
        case: profiling a test suite produces ``tests → package`` edges,
        and either directory alone drops one endpoint of every one of
        them, while the repository root sweeps in the virtualenv.
        """
        return self._get("scope", "prefixes")

    def resolved_prefixes(self) -> list[Path] | None:
        """:attr:`scope_prefixes` as absolute paths under :attr:`root`."""
        prefixes = self.scope_prefixes
        if prefixes is None:
            return None
        return [(self.root / prefix).resolve() for prefix in prefixes]

    # -- [catalog] -------------------------------------------------------

    @property
    def catalog_errors(self) -> str | None:
        """Project-relative path to the error catalogue, if any."""
        return self._get("catalog", "errors")

    def resolved_catalog_errors(self) -> Path | None:
        path = self.catalog_errors
        return None if path is None else (self.root / path).resolve()
