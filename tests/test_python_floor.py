"""The package must actually parse on the Python it claims to support.

`pyproject.toml` declares `requires-python = ">=3.10"`, and CI runs the
matrix — but CI ran red for nearly two months on a PEP 701 f-string
(an expression split across physical lines inside `{...}`, legal only on
3.12+) that every developer's newer interpreter accepted silently. The
floor is a promise to users installing on 3.10; this test is the promise
being checked locally, before CI.

Note the trap this file exists to avoid repeating: `ast.parse(...,
feature_version=(3, 10))` does NOT catch it. `feature_version` gates
some grammar features but does not downgrade the TOKENIZER, so PEP 701
f-strings parse clean and the scan returns a false negative. The only
reliable check is a real interpreter of the floor version, which is why
this test skips rather than pretends when one isn't installed.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _declared_floor() -> tuple[int, int]:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'requires-python\s*=\s*">=\s*(\d+)\.(\d+)"', text)
    assert match, "pyproject.toml has no parseable requires-python floor"
    return int(match.group(1)), int(match.group(2))


def _floor_interpreter(floor: tuple[int, int]) -> str | None:
    """A real interpreter at the declared floor, if one is available.

    Checks PATH and uv's managed installs (`uv python install 3.10`).
    """
    name = f"python{floor[0]}.{floor[1]}"
    found = shutil.which(name)
    if found:
        return found
    uv_root = Path.home() / ".local" / "share" / "uv" / "python"
    candidates = sorted(uv_root.glob(f"cpython-{floor[0]}.{floor[1]}*/bin/{name}"))
    return str(candidates[0]) if candidates else None


def test_sources_parse_on_the_declared_python_floor():
    floor = _declared_floor()
    if sys.version_info[:2] == floor:
        interpreter = sys.executable
    else:
        interpreter = _floor_interpreter(floor)
    if interpreter is None:
        pytest.skip(
            f"no Python {floor[0]}.{floor[1]} available "
            f"(install with: uv python install {floor[0]}.{floor[1]}); "
            f"CI's matrix still covers this"
        )

    script = (
        "import pathlib, sys\n"
        "bad = []\n"
        "for p in sorted(pathlib.Path('sphinxcontrib').rglob('*.py')):\n"
        "    try:\n"
        "        compile(p.read_text(encoding='utf-8'), str(p), 'exec')\n"
        "    except SyntaxError as e:\n"
        "        bad.append(f'{p}:{e.lineno} {e.msg}')\n"
        "print('\\n'.join(bad))\n"
    )
    proc = subprocess.run(
        [interpreter, "-c", script],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert not proc.stdout.strip(), (
        f"source does not parse on Python {floor[0]}.{floor[1]}, which "
        f"pyproject.toml promises to support:\n{proc.stdout}"
    )


def test_mcp_dependency_excludes_the_release_that_removed_fastmcp():
    """`mcp` 2.0.0 removed `mcp.server.fastmcp`, which `server.py` imports.

    An unpinned `mcp>=1.0` resolves to 2.x and produces an install whose
    MCP server cannot start — every tool silently unavailable, with the
    failure surfacing only at server spawn. Keep the bound until the
    server is ported to the 2.x API.
    """
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'"mcp>=[^"]*"', text)
    assert match, "mcp dependency not found in pyproject.toml"
    assert "<2" in match.group(0), (
        "mcp must stay bounded below 2.0 while server.py imports "
        "mcp.server.fastmcp, which 2.0.0 removed"
    )
