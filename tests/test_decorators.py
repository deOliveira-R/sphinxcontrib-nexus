"""Unit tests for decorator parsing helpers in ast_analyzer.

These cover ``_parse_pytest_markers`` in isolation; end-to-end
integration through ``CodeVisitor`` is exercised in
``test_ast_analyzer.py`` and the e2e fixture suite.
"""

from __future__ import annotations

import ast

from sphinxcontrib.nexus.ast_analyzer import (
    _parse_pytest_markers,
    _render_decorator,
)


def _decs(src: str) -> list[ast.expr]:
    """Parse ``src`` as a function body and return its decorator list."""
    mod = ast.parse(src)
    fn = mod.body[0]
    assert isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    return fn.decorator_list


# ---------------------------------------------------------------------------
# _render_decorator
# ---------------------------------------------------------------------------


def test_render_bare_name():
    decs = _decs("@mark\ndef f(): pass\n")
    assert _render_decorator(decs[0]) == "mark"


def test_render_attribute_chain():
    decs = _decs("@pytest.mark.l0\ndef f(): pass\n")
    assert _render_decorator(decs[0]) == "pytest.mark.l0"


def test_render_call_with_args():
    decs = _decs('@pytest.mark.verifies("a", "b")\ndef f(): pass\n')
    assert _render_decorator(decs[0]) == "pytest.mark.verifies('a', 'b')"


def test_render_call_with_kwargs():
    decs = _decs('@verify.l1(equations=["e1"], catches=["FM-01"])\ndef f(): pass\n')
    rendered = _render_decorator(decs[0])
    assert rendered.startswith("verify.l1(")
    assert "equations=['e1']" in rendered
    assert "catches=['FM-01']" in rendered


# ---------------------------------------------------------------------------
# _parse_pytest_markers — vv_level
# ---------------------------------------------------------------------------


def test_parse_level_bare_mark():
    decs = _decs("@pytest.mark.l0\ndef f(): pass\n")
    assert _parse_pytest_markers(decs) == {"vv_level": "L0"}


def test_parse_level_l2():
    decs = _decs("@pytest.mark.l2\ndef f(): pass\n")
    assert _parse_pytest_markers(decs)["vv_level"] == "L2"


def test_parse_level_all_four():
    for lvl in ("l0", "l1", "l2", "l3"):
        decs = _decs(f"@pytest.mark.{lvl}\ndef f(): pass\n")
        assert _parse_pytest_markers(decs)["vv_level"] == lvl.upper()


# ---------------------------------------------------------------------------
# _parse_pytest_markers — verifies
# ---------------------------------------------------------------------------


def test_parse_verifies_single():
    decs = _decs('@pytest.mark.verifies("transport-cartesian")\ndef f(): pass\n')
    assert _parse_pytest_markers(decs)["verifies"] == ("transport-cartesian",)


def test_parse_verifies_multi():
    decs = _decs('@pytest.mark.verifies("a", "b", "c")\ndef f(): pass\n')
    assert _parse_pytest_markers(decs)["verifies"] == ("a", "b", "c")


def test_parse_catches():
    decs = _decs('@pytest.mark.catches("FM-07", "ERR-003")\ndef f(): pass\n')
    assert _parse_pytest_markers(decs)["catches"] == ("FM-07", "ERR-003")


def test_parse_slow():
    decs = _decs("@pytest.mark.slow\ndef f(): pass\n")
    assert _parse_pytest_markers(decs)["slow"] is True


def test_parse_combined_markers():
    decs = _decs(
        "@pytest.mark.l0\n"
        '@pytest.mark.verifies("eq-1")\n'
        '@pytest.mark.catches("FM-01")\n'
        "def f(): pass\n"
    )
    meta = _parse_pytest_markers(decs)
    assert meta["vv_level"] == "L0"
    assert meta["verifies"] == ("eq-1",)
    assert meta["catches"] == ("FM-01",)


# ---------------------------------------------------------------------------
# verify.lN sugar
# ---------------------------------------------------------------------------


def test_verify_sugar_level():
    decs = _decs("@verify.l0()\ndef f(): pass\n")
    assert _parse_pytest_markers(decs)["vv_level"] == "L0"


def test_verify_sugar_with_equations():
    decs = _decs(
        '@verify.l1(equations=["fixture-attenuation", "fixture-balance"])\n'
        "def f(): pass\n"
    )
    meta = _parse_pytest_markers(decs)
    assert meta["vv_level"] == "L1"
    assert meta["verifies"] == ("fixture-attenuation", "fixture-balance")


def test_verify_sugar_with_catches():
    decs = _decs(
        '@verify.l2(equations=["e1"], catches=["FM-07", "ERR-003"])\n'
        "def f(): pass\n"
    )
    meta = _parse_pytest_markers(decs)
    assert meta["vv_level"] == "L2"
    assert meta["verifies"] == ("e1",)
    assert meta["catches"] == ("FM-07", "ERR-003")


# ---------------------------------------------------------------------------
# Safety: unrecognized and unsafe decorators are ignored
# ---------------------------------------------------------------------------


def test_parse_ignores_unknown_decorator():
    decs = _decs("@dataclass\ndef f(): pass\n")
    assert _parse_pytest_markers(decs) == {}


def test_parse_ignores_non_literal_verifies_arg():
    # The ``verifies`` arg is a variable — we must not evaluate it.
    mod = ast.parse(
        "label = 'secret'\n"
        "@pytest.mark.verifies(label)\n"
        "def f(): pass\n"
    )
    fn = mod.body[1]
    assert isinstance(fn, ast.FunctionDef)
    meta = _parse_pytest_markers(fn.decorator_list)
    # No ``verifies`` key because the argument isn't a literal.
    assert "verifies" not in meta


def test_parse_ignores_non_pytest_mark_namespace():
    decs = _decs("@mylib.mark.l0\ndef f(): pass\n")
    assert _parse_pytest_markers(decs) == {}


def test_parse_nested_list_literal():
    decs = _decs('@pytest.mark.verifies("a")\n@pytest.mark.verifies("b")\ndef f(): pass\n')
    # Two separate verifies calls accumulate.
    assert _parse_pytest_markers(decs)["verifies"] == ("a", "b")
