"""``.nexus/config.toml`` actually drives a real Sphinx build.

The unit tests in ``test_project_config.py`` prove the loader parses. They
say nothing about whether anything *reads* it — a config file nothing
consumes is inert, and would pass every unit test it has.

So each test here runs a real ``sphinx-build`` over a project it
constructs, and each is paired with a control that differs only in the
config file. Without the control an assertion like "the artefacts are at
``kg/``" could pass for reasons unrelated to the setting.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

CONF = 'extensions = ["sphinxcontrib.nexus"]\nmaster_doc = "index"\n'
INDEX = """\
Fixture
=======

.. math::
   :label: fixture-balance

   a = b
"""


def _project(root: Path, config: str | None) -> Path:
    """A minimal Sphinx project, optionally carrying ``.nexus/config.toml``."""
    docs = root / "docs"
    docs.mkdir(parents=True)
    (docs / "conf.py").write_text(CONF)
    (docs / "index.rst").write_text(INDEX)
    if config is not None:
        (root / ".nexus").mkdir()
        (root / ".nexus" / "config.toml").write_text(config)
    return docs


def _build(srcdir: Path, outdir: Path) -> None:
    subprocess.run(
        [sys.executable, "-m", "sphinx", "-q", "-E", str(srcdir), str(outdir)],
        check=True,
    )


@pytest.fixture(scope="module")
def unconfigured(tmp_path_factory):
    """Control: no ``.nexus/`` at all."""
    root = tmp_path_factory.mktemp("unconfigured")
    out = root / "out"
    _build(_project(root, None), out)
    return out


@pytest.fixture(scope="module")
def configured(tmp_path_factory):
    """Same project, plus a config file that renames the output."""
    root = tmp_path_factory.mktemp("configured")
    out = root / "out"
    _build(_project(root, '[graph]\noutput = "kg"\n'), out)
    return out


def test_control_lands_at_the_default_output(unconfigured):
    assert (unconfigured / "_nexus" / "graph.db").exists()


def test_config_toml_moves_the_output(configured):
    """The witness: only the config file differs between these two trees."""
    assert (configured / "kg" / "graph.db").exists()
    assert not (configured / "_nexus").exists(), (
        "the default location was still written — conf.py is winning over "
        ".nexus/config.toml, so the precedence chain is not wired"
    )


def test_the_graph_is_otherwise_the_same(unconfigured, configured):
    """Renaming the output must not change what is in the graph."""
    from sphinxcontrib.nexus.export import load_sqlite

    a = load_sqlite(unconfigured / "_nexus" / "graph.db").nxgraph
    b = load_sqlite(configured / "kg" / "graph.db").nxgraph

    assert set(a.nodes) == set(b.nodes)
    assert a.number_of_edges() == b.number_of_edges()


def test_config_toml_is_found_from_a_nested_srcdir(tmp_path):
    """``docs/`` is one level down; the loader must walk up to the root."""
    root = tmp_path / "proj"
    srcdir = _project(root, '[graph]\noutput = "kg"\n')
    assert srcdir.parent == root  # the config sits above srcdir, not beside it
    out = tmp_path / "out"
    _build(srcdir, out)
    assert (out / "kg" / "graph.db").exists()


def test_an_unreadable_config_fails_the_build_loudly(tmp_path):
    """A malformed settings file must not be silently ignored."""
    root = tmp_path / "proj"
    srcdir = _project(root, "[graph\noutput =")
    with pytest.raises(subprocess.CalledProcessError):
        _build(srcdir, tmp_path / "out")


def test_cli_finds_the_graph_from_config_with_no_db_flag(tmp_path):
    """The CLI's whole reason for the config file.

    Runs from a SUBDIRECTORY with no ``--db``, so a pass means the loader
    walked up, read ``[graph].db``, and opened it — none of which the
    legacy CWD-relative default could have done.
    """
    root = tmp_path / "proj"
    srcdir = _project(root, "")  # config written below, once we know the path
    out = root / "site"
    _build(srcdir, out)

    rel = (out / "_nexus" / "graph.db").relative_to(root)
    (root / ".nexus").mkdir(exist_ok=True)
    (root / ".nexus" / "config.toml").write_text(f'[graph]\ndb = "{rel}"\n')

    workdir = root / "docs"
    result = subprocess.run(
        [sys.executable, "-m", "sphinxcontrib.nexus.cli", "status"],
        cwd=workdir, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "nodes" in result.stdout, result.stdout


class _StubConfig:
    """Just enough of ``app.config`` for ``_effective``'s ``getattr``."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


class _StubApp:
    def __init__(self, srcdir, **conf):
        self.srcdir = str(srcdir)
        self.config = _StubConfig(**conf)


@pytest.mark.parametrize("declared, expected", [(True, True), (False, False)])
def test_infer_implements_reaches_the_call_site(tmp_path, declared, expected):
    """The setting is carried from the file to `EffectiveSettings`.

    ⚠ HONEST SCOPE: this pins the WIRING, not the EFFECT. It does not show
    that turning inference off removes `implements` edges — that needs a
    fixture in which inference actually fires, and two attempts at one
    produced zero inferred edges. A behavioural gate is owed; a green test
    that cannot fail is worse than none, so it is not faked here.
    """
    from sphinxcontrib.nexus import _effective

    root = tmp_path / "proj"
    docs = _project(root, f"[graph]\ninfer_implements = {str(declared).lower()}\n")
    settings = _effective(_StubApp(docs, nexus_infer_implements=not declared))
    assert settings.infer_implements is expected


def test_verification_registry_reaches_the_call_site(tmp_path):
    """Same scope caveat as above — wiring, not effect."""
    from sphinxcontrib.nexus import _effective

    root = tmp_path / "proj"
    docs = _project(root, '[graph]\nverification_registry = ["reg.yaml"]\n')
    settings = _effective(_StubApp(docs, nexus_verification_registry=[]))
    assert settings.verification_registry == ["reg.yaml"]


def test_cli_without_config_still_uses_the_legacy_default(tmp_path):
    """Control: the same invocation must NOT find a graph unaided.

    Without this, the test above could pass because the CLI stumbled onto
    the graph some other way.
    """
    root = tmp_path / "proj"
    srcdir = _project(root, None)
    _build(srcdir, root / "site")

    result = subprocess.run(
        [sys.executable, "-m", "sphinxcontrib.nexus.cli", "status"],
        cwd=root / "docs", capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "does not exist" in (result.stdout + result.stderr)
