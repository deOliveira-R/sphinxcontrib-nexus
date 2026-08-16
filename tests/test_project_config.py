"""Project configuration read from ``.nexus/config.toml``.

Distinct from ``test_config.py``, which covers the Sphinx-side
``nexus_*`` option plumbing. This covers the file-based layer that the
CLI and the MCP server can also read.
"""

from __future__ import annotations

import logging
import textwrap
from pathlib import Path

import pytest

from sphinxcontrib.nexus.project import (
    CONFIG_DIR,
    GRAPH_DB_NAME,
    ProjectConfig,
    find_project_root,
    resolve,
    resolve_db,
)


def _write(tmp_path, body: str):
    (tmp_path / CONFIG_DIR).mkdir(parents=True, exist_ok=True)
    (tmp_path / CONFIG_DIR / "config.toml").write_text(textwrap.dedent(body))
    return tmp_path


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_missing_config_dir_is_a_supported_state(tmp_path):
    """Nexus must keep working on an unconfigured project."""
    cfg = ProjectConfig.load(tmp_path)
    assert cfg.source is None
    assert cfg.output is None
    assert cfg.scope_prefixes is None
    assert cfg.infer_implements is None


def test_config_dir_without_a_file_is_also_fine(tmp_path):
    (tmp_path / CONFIG_DIR).mkdir()
    cfg = ProjectConfig.load(tmp_path)
    assert cfg.source is None
    assert cfg.root == tmp_path.resolve()


def test_root_is_found_from_a_subdirectory(tmp_path):
    """The CLI must work from anywhere in the tree, the way git does."""
    _write(tmp_path, '[graph]\noutput = "graph"\n')
    deep = tmp_path / "src" / "pkg" / "sub"
    deep.mkdir(parents=True)

    assert find_project_root(deep) == tmp_path.resolve()
    assert ProjectConfig.load(deep).output == "graph"


def test_no_config_anywhere_returns_none_root(tmp_path):
    assert find_project_root(tmp_path) is None


# ---------------------------------------------------------------------------
# Where the walk STOPS. `.nexus/` names two different things — a project's
# settings directory and, at `$HOME`, the machine's usage-log directory —
# so an unbounded walk adopts whichever it reaches first.
# ---------------------------------------------------------------------------


def test_the_home_directory_is_never_a_project_root(tmp_path, monkeypatch):
    """``~/.nexus/`` is machine state: it holds ``usage.jsonl``.

    It therefore exists on every machine that has ever run the MCP server,
    and before this bound, ANY tree beneath ``$HOME`` without a config of
    its own resolved its root to the home directory. Measured 2026-08-16 in
    this very repository: ``find_project_root("<repo>/tests/roots")``
    returned ``/Users/rodrigo``, and a docs-fixture build wrote ``graph.db``
    and ``graph.json`` into the home directory.

    The read failure had been live and silent for far longer than the write
    one: answering with the wrong project's settings looks like an answer.
    """
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / CONFIG_DIR).mkdir()
    deep = tmp_path / "some" / "project"
    deep.mkdir(parents=True)

    assert find_project_root(deep) is None


def test_a_checkout_boundary_stops_the_walk(tmp_path, monkeypatch):
    """A ``.nexus/`` above a checkout belongs to a different project."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "nb"))
    (tmp_path / CONFIG_DIR).mkdir()  # an outer project's settings
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    deep = repo / "src" / "pkg"
    deep.mkdir(parents=True)

    assert find_project_root(deep) is None


def test_a_worktree_boundary_stops_the_walk_too(tmp_path, monkeypatch):
    """In a worktree or submodule ``.git`` is a FILE, not a directory."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "nb"))
    (tmp_path / CONFIG_DIR).mkdir()
    repo = tmp_path / "wt"
    repo.mkdir()
    (repo / ".git").write_text("gitdir: /elsewhere/.git/worktrees/wt\n")

    assert find_project_root(repo) is None


def test_the_checkouts_own_config_is_still_found(tmp_path, monkeypatch):
    """Positive control: the bounds must not make discovery inert.

    Without this, all three tests above would pass just as well if
    ``find_project_root`` had been made to return ``None`` unconditionally
    — which is the reading a bounded search is most likely to produce by
    accident.
    """
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "nb"))
    (tmp_path / CONFIG_DIR).mkdir()  # the outer project, which must NOT win
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / CONFIG_DIR).mkdir()
    deep = repo / "src" / "pkg"
    deep.mkdir(parents=True)

    assert find_project_root(deep) == repo.resolve()


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------


def test_settings_are_read(tmp_path):
    root = _write(
        tmp_path,
        """
        [graph]
        output = "graph"
        extra_source_dirs = ["tests"]
        exclude_patterns = ["scratch/*"]
        infer_implements = false
        max_viz_nodes = 500

        [scope]
        prefixes = ["pkg", "tests"]

        [catalog]
        errors = "tests/l0_error_catalog.md"
        """,
    )
    cfg = ProjectConfig.load(root)

    assert cfg.output == "graph"
    assert cfg.extra_source_dirs == ["tests"]
    assert cfg.exclude_patterns == ["scratch/*"]
    assert cfg.infer_implements is False
    assert cfg.max_viz_nodes == 500
    assert cfg.scope_prefixes == ["pkg", "tests"]
    assert cfg.catalog_errors == "tests/l0_error_catalog.md"


def test_false_is_distinguishable_from_unset(tmp_path):
    """The precedence chain dies if `False` and "unset" collapse."""
    root = _write(tmp_path, "[graph]\ninfer_implements = false\n")
    assert ProjectConfig.load(root).infer_implements is False

    bare = _write(tmp_path / "other", "[graph]\n")
    assert ProjectConfig.load(bare).infer_implements is None


def test_paths_resolve_against_the_project_root(tmp_path):
    root = _write(
        tmp_path,
        """
        [scope]
        prefixes = ["pkg", "tests"]

        [catalog]
        errors = "docs/errors.md"
        """,
    )
    cfg = ProjectConfig.load(root)

    assert cfg.resolved_prefixes() == [
        (tmp_path / "pkg").resolve(),
        (tmp_path / "tests").resolve(),
    ]
    assert cfg.resolved_catalog_errors() == (tmp_path / "docs" / "errors.md").resolve()


def test_unset_paths_resolve_to_none(tmp_path):
    cfg = ProjectConfig.load(_write(tmp_path, "[graph]\n"))
    assert cfg.resolved_prefixes() is None
    assert cfg.resolved_catalog_errors() is None


# ---------------------------------------------------------------------------
# Typos are reported, not swallowed
# ---------------------------------------------------------------------------


def test_unknown_key_is_reported(tmp_path, caplog):
    root = _write(
        tmp_path,
        """
        [graph]
        output = "graph"
        ouptut = "typo"

        [scoop]
        prefixes = ["pkg"]
        """,
    )
    with caplog.at_level(logging.WARNING):
        cfg = ProjectConfig.load(root)

    reported = set(cfg.unknown_keys())
    assert ("graph", "ouptut") in reported
    assert ("scoop", "prefixes") in reported
    # …and the known key beside the typo still works.
    assert cfg.output == "graph"
    assert "ouptut" in caplog.text


def test_a_clean_config_reports_nothing(tmp_path):
    root = _write(tmp_path, '[graph]\noutput = "graph"\n')
    assert ProjectConfig.load(root).unknown_keys() == []


def test_malformed_toml_raises(tmp_path):
    """A settings file that cannot be parsed is a mistake, not a state."""
    (tmp_path / CONFIG_DIR).mkdir()
    (tmp_path / CONFIG_DIR / "config.toml").write_text("[graph\noutput =")
    with pytest.raises(Exception):
        ProjectConfig.load(tmp_path)


# ---------------------------------------------------------------------------
# Precedence
# ---------------------------------------------------------------------------


def test_resolve_takes_the_first_non_none():
    assert resolve(None, None, "file", "sphinx", default="fallback") == "file"
    assert resolve(None, None, None, default="fallback") == "fallback"


def test_resolve_does_not_treat_false_as_absent():
    """`infer_implements = false` must beat a `True` default."""
    assert resolve(None, False, default=True) is False
    assert resolve(None, 0, default=99) == 0


# ---------------------------------------------------------------------------
# Which graph the CLI and the server open
# ---------------------------------------------------------------------------


def test_explicit_db_flag_wins(tmp_path):
    root = _write(tmp_path, "[graph]\noutput = 'graph'\n")
    assert resolve_db("/somewhere/else.db", root) == Path("/somewhere/else.db")


def test_the_store_is_derived_from_the_root_not_declared(tmp_path):
    """The graph lives beside the settings, and nothing has to say so.

    This is the whole point of the convention: the surface already found
    ``.nexus/`` — that is how it read the settings — so it can name the
    database without being told, and there is no second declaration to
    disagree with the build.
    """
    root = _write(tmp_path, "[graph]\noutput = 'graph'\n")
    assert resolve_db(None, root) == (
        tmp_path / CONFIG_DIR / GRAPH_DB_NAME
    ).resolve()


def test_an_unconfigured_project_still_names_a_store(tmp_path):
    """No config file is a supported state, and it gets the same convention.

    Previously this returned a working-directory-relative ``_nexus/graph.db``
    that was "almost never right for a real project" by its own docstring.
    One convention everywhere is the point; a second spelling reachable only
    by having no config is a second spelling.
    """
    assert resolve_db(None, tmp_path) == (
        tmp_path / CONFIG_DIR / GRAPH_DB_NAME
    ).resolve()


def test_db_resolves_from_a_subdirectory(tmp_path):
    """Root discovery walks up, so the CLI works from anywhere in the tree."""
    root = _write(tmp_path, "[graph]\noutput = 'graph'\n")
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    assert resolve_db(None, deep) == (root / CONFIG_DIR / GRAPH_DB_NAME).resolve()


def test_the_retired_db_key_is_reported_not_silently_obeyed(tmp_path, caplog):
    """A leftover ``db =`` must be loud, because it reads as authoritative.

    The witness is real: ORPHEUS's own config carried
    ``db = "docs/_build/html/graph/graph.db"`` until this change. Dropping
    the key from ``KNOWN_KEYS`` is what turns a silently-ignored declaration
    — which would leave a reader believing they had redirected the graph —
    into a warning naming the key and the ones that survive.
    """
    root = _write(
        tmp_path, '[graph]\ndb = "docs/_build/html/graph/graph.db"\n'
    )
    with caplog.at_level(logging.WARNING):
        cfg = ProjectConfig.load(root)

    assert ("graph", "db") in cfg.unknown_keys()
    assert "unknown key 'db'" in caplog.text
    # …and the declaration has NO effect: the store is still the convention.
    assert cfg.resolved_db() == (tmp_path / CONFIG_DIR / GRAPH_DB_NAME).resolve()
