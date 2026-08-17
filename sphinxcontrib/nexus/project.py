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

#: The graph database, inside :data:`CONFIG_DIR`. A **convention**, not a
#: setting: every surface derives it from the project root, so there is no
#: second place for it to be declared and therefore no way for two surfaces
#: to disagree about where the graph is.
GRAPH_DB_NAME = "graph.db"

#: The JSON export, beside the database.
GRAPH_JSON_NAME = "graph.json"

#: The runtime-overlay sidecar directory, beside the database.
#:
#: This is *why* the store lives here rather than under the Sphinx output
#: directory. Three artefacts had been sharing that one directory with three
#: different lifetimes: ``graph.db``/``graph.json`` are derived and rewritten
#: on every build; ``graph.html`` is derived *and* must be served from the
#: HTML tree; but a trace is **durable state** — a profiled suite run costs
#: minutes to reproduce, and :mod:`~sphinxcontrib.nexus.runtime` says the
#: sidecar exists precisely so it survives the rebuild that replaces the
#: database. Sitting in the build tree, it did not: ``rm -rf docs/_build``
#: destroyed it. A directory's lifetime is its most-derived member's.
TRACES_DIR_NAME = "traces"

#: Every key this loader understands, by table. A key outside this map is
#: reported (see :meth:`ProjectConfig.unknown_keys`) rather than silently
#: dropped — a typo that is ignored without comment is the same defect as
#: an empty result that reads like a measurement.
KNOWN_KEYS: Mapping[str, frozenset[str]] = {
    "graph": frozenset({
        "output",
        "git_timeout_seconds",
        "extra_source_dirs",
        "exclude_patterns",
        "analyze_tests",
        "test_patterns",
        "infer_implements",
        "verification_registry",
        "max_viz_nodes",
    }),
    "scope": frozenset({"prefixes"}),
    "catalog": frozenset({"errors"}),
    # How much a tool may say. A reply lands in an agent's context and
    # stays there, so these decide what a session can afford — and they
    # are exactly the numbers that will want raising as context windows
    # grow, which is why they are settings rather than constants.
    "replies": frozenset({
        "max_characters",
        "items_per_list",
        "items_per_brief_line",
        "neighbors_per_edge_type",
        "nodes_per_impact_depth",
    }),
    # What the session briefing shows. It is produced before anyone has
    # asked anything, so every session pays for it whether it is read or
    # not.
    "briefing": frozenset({
        "project_hubs",
        "stale_pages",
        "symbols_per_stale_page",
        "coverage_gaps",
    }),
}

#: Shipped values for every tunable, in one place, so the default and the
#: setting that overrides it cannot drift — and so a reader can see what
#: they get without a config file. Each is `[table].key`.
#:
#: `replies.max_characters` is a CHARACTER count because that is what the
#: code can measure without a tokenizer; at roughly 4 characters a token,
#: 20000 is about 5000 tokens. It exists because [M] 2026-08-16
#: `processes()` returned 1,238,013 tokens — several times any context
#: window — and eight tools exceeded 12,000.
DEFAULTS: Mapping[str, Any] = {
    "replies.max_characters": 20_000,
    "replies.items_per_list": 50,
    "replies.items_per_brief_line": 3,
    "replies.neighbors_per_edge_type": 25,
    "replies.nodes_per_impact_depth": 50,
    "briefing.project_hubs": 5,
    "briefing.stale_pages": 5,
    "briefing.symbols_per_stale_page": 3,
    "briefing.coverage_gaps": 5,
    "graph.git_timeout_seconds": 10,
}

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


def graph_db_in(root: Path | str) -> Path:
    """Where the project rooted at ``root`` keeps its graph database.

    The convention in one expression. Every other spelling of it —
    :meth:`ProjectConfig.resolved_db`, :meth:`Workspace.for_root` — reads
    it here, so the layout that Track 0.6 made *derivable* does not get
    re-declared in as many places as the retired ``[graph].db`` key used
    to be.
    """
    return (Path(root) / CONFIG_DIR / GRAPH_DB_NAME).resolve()


def resolve_db(
    explicit: Path | str | None = None,
    start: Path | str | None = None,
) -> Path:
    """Which graph database to open: an explicit flag, else the project's.

    Shared by the CLI and the MCP server so the answer cannot differ
    between them — a server pointed at a different graph than the CLI is
    a confusing failure that looks like a stale graph.

    There is no third case any more. The location is a convention
    (:data:`GRAPH_DB_NAME` inside :data:`CONFIG_DIR`), so it is *derived*
    from the project root rather than declared, and the only way to open a
    different graph is to say so explicitly.
    """
    if explicit is not None:
        return Path(explicit)
    return ProjectConfig.load(start or Path.cwd()).resolved_db()


def find_project_root(start: Path | str) -> Path | None:
    """Nearest ancestor of ``start`` (inclusive) containing ``.nexus/``.

    Walks upward so the CLI works from any subdirectory, the way ``git``
    does. Returns ``None`` when no configuration directory exists, which
    is a supported state — nexus must keep working on an unconfigured
    project.

    ⚠ The walk **stops at the checkout**, for two reasons that are really
    one: ``.nexus/`` names two different things.

    *A repository boundary.* A ``.nexus/`` above a checkout belongs to a
    different project, so a directory holding ``.git`` ends the search —
    checked itself, then final. Without this an unconfigured sub-project
    silently adopts its parent's settings.

    *The user's own directory.* ``~/.nexus/`` exists on every machine that
    has ever run the MCP server, because that is where ``usage.jsonl``
    lives — it is machine state, not a project. Ascending into it made
    ``$HOME`` the resolved root of every unconfigured tree beneath it.
    Measured 2026-08-16: ``find_project_root("<repo>/tests/roots")``
    returned ``/Users/rodrigo``, and a docs-fixture build consequently
    wrote ``graph.db`` and ``graph.json`` into the home directory. Caught
    only because moving the store made the write observable; as a *read*
    it had been silently answering with the wrong project's config.
    """
    home = Path.home().resolve()
    current = Path(start).resolve()
    for candidate in (current, *current.parents):
        if candidate != home and (candidate / CONFIG_DIR).is_dir():
            return candidate
        # A checkout is the outermost thing a project can be. `.git` is a
        # directory in a normal clone and a FILE in a worktree or submodule,
        # so test existence, not kind.
        if (candidate / ".git").exists():
            return None
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

    def tunable(self, dotted: str) -> Any:
        """A ``[table].key`` setting, or its shipped default.

        One accessor rather than a property per key, so adding a tunable
        is a single line in :data:`DEFAULTS` and a single line in
        :data:`KNOWN_KEYS` — the two places that must agree, and which a
        test pins to each other. A property per key would be a third
        place to forget.

        Unlike the ``[graph]`` accessors this does NOT return ``None``
        for an unset key: these all have a meaningful shipped value, and
        every caller would otherwise repeat the same ``or DEFAULT``.
        """
        if dotted not in DEFAULTS:
            raise KeyError(
                f"unknown tunable {dotted!r}; known: {sorted(DEFAULTS)}"
            )
        table, key = dotted.split(".", 1)
        value = self._get(table, key)
        return DEFAULTS[dotted] if value is None else value

    # -- [graph] ---------------------------------------------------------

    @property
    def output(self) -> str | None:
        """Subdirectory of the Sphinx HTML output the *explorer page* is
        written to.

        This names one artefact, not the graph store. ``graph.html`` is the
        only piece that has to live under the HTML tree, because it is
        served: a page links it and the ``.. nexus-graph::`` directive
        iframes it. The database, its JSON export and the runtime traces are
        not served and do not belong to the build output — see
        :data:`TRACES_DIR_NAME` for what that cost.
        """
        return self._get("graph", "output")

    @property
    def is_anchored(self) -> bool:
        """Is there a real :data:`CONFIG_DIR` at :attr:`root`?

        ``False`` means no project declared itself anywhere above the source
        — :meth:`load` then falls back to the starting directory, so
        :attr:`root` is a guess rather than a discovery. The distinction
        matters to the build: an anchored project has somewhere durable to
        put its graph, an unanchored one has only its output directory.
        """
        return (self.root / CONFIG_DIR).is_dir()

    @property
    def graph_dir(self) -> Path:
        """Directory holding the graph store — the database, its JSON
        export and the runtime traces.

        It is :data:`CONFIG_DIR` itself, which makes the store *derivable*:
        every surface already finds this directory (that is how it found the
        settings), so none of them needs to be told where the graph is, and
        there is no second declaration to fall out of step with the first.

        Only meaningful when :attr:`is_anchored`. A build that is not
        anchored must not write here — :attr:`root` would be an ancestor it
        merely happens to sit under, which for a throwaway or vendored docs
        tree is somebody else's project.
        """
        return self.root / CONFIG_DIR

    def resolved_db(self) -> Path:
        """Absolute path of this project's graph database."""
        return graph_db_in(self.root)

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
    def verification_registry(self) -> list[str] | None:
        """Registry files declaring verification edges from outside the code."""
        return self._get("graph", "verification_registry")

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
