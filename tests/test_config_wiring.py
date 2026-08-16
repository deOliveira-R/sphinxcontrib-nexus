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
    """Control: no ``.nexus/`` at all, so the project is UNANCHORED."""
    root = tmp_path_factory.mktemp("unconfigured")
    out = root / "out"
    _build(_project(root, None), out)
    return root, out


@pytest.fixture(scope="module")
def configured(tmp_path_factory):
    """Same project, plus a config file that renames the explorer output."""
    root = tmp_path_factory.mktemp("configured")
    out = root / "out"
    _build(_project(root, '[graph]\noutput = "kg"\n'), out)
    return root, out


def test_control_puts_everything_in_the_build_output(unconfigured):
    """Unanchored: nothing declared a project, so nothing outlives the build.

    ``root`` is a *guess* here — the source's parent, because nothing above
    it claimed the tree — so writing a store there would drop a graph into
    whatever directory the docs happen to sit under. The artefacts stay
    with the output instead.
    """
    root, out = unconfigured
    assert (out / "_nexus" / "graph.db").exists()
    assert not (root / ".nexus").exists()


def test_a_config_file_anchors_the_store_outside_the_build(configured):
    """The witness: only the config file differs between these two trees.

    It buys two separable things — the store lifts OUT of the build tree
    (which is what makes `traces/` survive a clean build), and ``output``
    renames the explorer directory, now that key's only job.
    """
    root, out = configured
    assert (root / ".nexus" / "graph.db").exists(), "the store did not anchor"
    assert (out / "kg" / "graph.html").exists(), "the explorer did not move"
    assert not (out / "_nexus").exists(), (
        "the default location was still written — conf.py is winning over "
        ".nexus/config.toml, so the precedence chain is not wired"
    )


def test_the_graph_is_otherwise_the_same(unconfigured, configured):
    """Relocating the store must not change what is in the graph."""
    from sphinxcontrib.nexus.export import load_sqlite

    a = load_sqlite(unconfigured[1] / "_nexus" / "graph.db").nxgraph
    b = load_sqlite(configured[0] / ".nexus" / "graph.db").nxgraph

    assert set(a.nodes) == set(b.nodes)
    assert a.number_of_edges() == b.number_of_edges()


def test_config_toml_is_found_from_a_nested_srcdir(tmp_path):
    """``docs/`` is one level down; the loader must walk up to the root."""
    root = tmp_path / "proj"
    srcdir = _project(root, '[graph]\noutput = "kg"\n')
    assert srcdir.parent == root  # the config sits above srcdir, not beside it
    out = tmp_path / "out"
    _build(srcdir, out)
    assert (root / ".nexus" / "graph.db").exists()
    assert (out / "kg" / "graph.html").exists()


def test_an_unreadable_config_fails_the_build_loudly(tmp_path):
    """A malformed settings file must not be silently ignored."""
    root = tmp_path / "proj"
    srcdir = _project(root, "[graph\noutput =")
    with pytest.raises(subprocess.CalledProcessError):
        _build(srcdir, tmp_path / "out")


def test_cli_opens_the_graph_the_build_wrote_with_nothing_declared(tmp_path):
    """Producer and consumer agree on the store WITHOUT being told.

    This is the end-to-end pin the retired ``[graph].db`` key used to need
    a human to maintain: the build writes, the CLI reads, and the only
    thing joining them is the convention. Nothing in this test names a
    path, so nothing in it can go stale when the store moves.

    Run from a SUBDIRECTORY with no ``--db``, so a pass also means root
    discovery walked up — the cwd-relative default could not have found it.
    """
    root = tmp_path / "proj"
    srcdir = _project(root, "")  # an empty config still anchors the project
    _build(srcdir, root / "site")

    workdir = root / "docs"
    result = subprocess.run(
        [sys.executable, "-m", "sphinxcontrib.nexus.cli", "status"],
        cwd=workdir, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "nodes" in result.stdout, result.stdout


def test_a_cli_verb_is_anchored_at_the_project_not_the_cwd(tmp_path, monkeypatch):
    """Which tree does a CLI invocation answer about?

    Every git-aware verb used to resolve ``args.project_root or
    Path.cwd()`` — six independent copies — so running one from
    ``docs/`` handed the query a subdirectory as its "project root".
    ``git diff`` still reports repository-relative paths from there, so
    the node paths (absolute in every real graph) fail to match and the
    verb reports a confident "nothing changed". See the query-level pair
    in ``test_new_features.py`` for that mechanism measured directly.

    The control is the cwd itself: this asserts the answer is NOT the
    directory the command ran in, which is the only thing that
    distinguishes the repair from what it replaced.
    """
    import argparse

    from sphinxcontrib.nexus.cli import _workspace_for

    root = tmp_path / "proj"
    _project(root, "")  # an empty config still anchors the project
    workdir = root / "docs"
    monkeypatch.chdir(workdir)

    ws = _workspace_for(argparse.Namespace(db=root / ".nexus" / "graph.db"))

    assert ws.root == root.resolve()
    assert ws.root != workdir.resolve(), "the verb answered about the cwd"


def test_an_explicit_project_root_still_wins(tmp_path, monkeypatch):
    """Discovery is the FALLBACK, not an override — otherwise
    ``--project-root`` would be silently ignored inside any project."""
    import argparse

    from sphinxcontrib.nexus.cli import _workspace_for

    root = tmp_path / "proj"
    _project(root, "")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(root)

    ws = _workspace_for(argparse.Namespace(
        db=root / ".nexus" / "graph.db", project_root=elsewhere,
    ))

    assert ws.root == elsewhere.resolve()


def test_no_subparser_carries_a_hardcoded_db_default():
    """Every ``--db`` must default to None so post-parse resolution runs.

    This pins a CLASS of defect, not the instance that produced it. The
    original sweep replaced a multi-line spelling
    (``"--db", type=Path, default=Path(...),``) and silently missed six
    single-line ones (``add_argument("--db", type=Path, default=Path(...))``),
    so those verbs kept the legacy CWD-relative default and ignored the
    config — returning an empty result that reads exactly like "nothing
    found".

    A grep is the wrong instrument: it can only match spellings someone
    thought of, and the miss above was purely a line-break. The AST sees
    every ``add_argument`` call however it is written.

    ⚠ The BETTER gate is walking the constructed parser, which would also
    catch a default set through some path the AST cannot see. That needs
    ``main()``'s inline parser extracted into a ``build_parser()`` — worth
    doing, and deliberately not bundled here.
    """
    import ast
    from pathlib import Path as _Path

    import sphinxcontrib.nexus.cli as cli_mod

    source = cli_mod.__file__
    assert source is not None, "cli module is not file-backed"
    tree = ast.parse(_Path(source).read_text())
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "add_argument"):
            continue
        if not any(
            isinstance(a, ast.Constant) and a.value == "--db" for a in node.args
        ):
            continue
        for kw in node.keywords:
            if kw.arg == "default" and not (
                isinstance(kw.value, ast.Constant) and kw.value.value is None
            ):
                offenders.append(f"line {node.lineno}: {ast.unparse(kw.value)}")

    assert offenders == [], (
        "these --db arguments hardcode a default, so post-parse resolution "
        f"never runs and .nexus/config.toml cannot reach them: {offenders}"
    )


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


# ---------------------------------------------------------------------
# `nexus config` — the seam for consumers that cannot import Python
# ---------------------------------------------------------------------
#
# Shell hooks need to know where the graph lives. Before this verb the
# only way was to hardcode the path, and three ORPHEUS hooks did exactly
# that; when `[graph].output` moved, two of them went silently dark (they
# `exit 0` when the graph is absent, so "wrong path" is indistinguishable
# from "no graph") and the third told every session to run a rebuild that
# could not fix it. The verb exists so a path is declared once.


def _config(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "sphinxcontrib.nexus.cli", "config", *args],
        cwd=cwd, capture_output=True, text=True,
    )


def test_config_db_reports_the_derived_path(tmp_path):
    """The witness for every shell consumer.

    ⚠ This test USED to be one of a differs-only-by-the-config-file pair,
    proving that ``[graph].db`` was read. That key is retired: the store is
    derived from the root, so the config file can no longer move it and no
    input exists that would make the two members of that pair disagree.
    Keeping both would be a control that cannot fail — so the pair is
    merged, and what survives is the property still worth pinning:
    **the path is anchored to the project root, not to the caller's cwd.**
    The call is deliberately made from a cwd that is not the root, so a
    relative answer, or one resolved against the caller, fails here.

    The coverage the pair used to give — that the build and the CLI open
    the SAME file — did not disappear with the key; it moved to
    ``test_cli_opens_the_graph_the_build_wrote_with_nothing_declared``,
    which is end-to-end and names no path at all.
    """
    root = tmp_path / "proj"
    _project(root, '[graph]\noutput = "kg"\n')

    result = _config("db", "--project-root", str(root), cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str((root / ".nexus/graph.db").resolve())


def test_config_lists_every_setting_when_given_no_key(tmp_path):
    root = tmp_path / "proj"
    _project(root, '[graph]\noutput = "kg"\n')

    result = _config("--project-root", str(root), cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    assert "output = kg" in result.stdout
    assert "catalog.errors = (unset)" in result.stdout, (
        "an unset setting must say so, not be omitted — a missing line "
        "reads as a missing FEATURE"
    )


def test_config_prints_a_list_one_item_per_line(tmp_path):
    """`$(...)` word-splitting and `read -r` both expect this shape."""
    root = tmp_path / "proj"
    _project(root, '[scope]\nprefixes = ["src", "tests"]\n')

    result = _config("scope.prefixes", "--project-root", str(root), cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["src", "tests"]


def test_config_fails_on_an_unset_setting(tmp_path):
    """Unset must be distinguishable from set — by the exit code.

    A script doing `x=$(nexus config k) || fallback` needs the failure;
    printing a default here would make "declared" and "defaulted" look
    identical to every caller.
    """
    root = tmp_path / "proj"
    _project(root, "")

    result = _config("catalog.errors", "--project-root", str(root), cwd=tmp_path)

    assert result.returncode == 1
    assert result.stdout.strip() == ""


def test_config_fails_on_an_unknown_key(tmp_path):
    root = tmp_path / "proj"
    _project(root, "")

    result = _config("no.such.key", "--project-root", str(root), cwd=tmp_path)

    assert result.returncode == 1
    assert "unknown setting" in result.stderr
    assert "scope.prefixes" in result.stderr, "the error must list what IS known"
