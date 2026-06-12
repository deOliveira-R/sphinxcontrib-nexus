"""README ↔ MCP registry drift guard.

The tool count and tool list in README.md have drifted from the
FastMCP registry repeatedly (serve's help said 16, a consumer's docs
said 20, README said 27 — all at the same time). The registry is the
single source of truth; this module pins the README to it so the next
added/renamed tool fails CI instead of silently drifting.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from sphinxcontrib.nexus.server import _mcp  # noqa: E402

README = Path(__file__).resolve().parents[1] / "README.md"


def _section(text: str, header_pattern: str) -> tuple[int, str]:
    """The declared count from a ``## Header (N)`` line and the body
    up to the next ``## `` header."""
    match = re.search(rf"^{header_pattern} \((\d+)\)\n(.*?)(?=^## )",
                      text, re.MULTILINE | re.DOTALL)
    assert match is not None, f"README section {header_pattern!r} not found"
    return int(match.group(1)), match.group(2)


def test_readme_tools_match_registry():
    declared_count, body = _section(README.read_text(), "## MCP Tools")
    readme_tools = set(re.findall(r"^- \*\*`(\w+)`\*\*", body, re.MULTILINE))

    registry_tools = {t.name for t in asyncio.run(_mcp.list_tools())}

    assert readme_tools == registry_tools
    assert declared_count == len(registry_tools)


def test_readme_resource_count_matches_registry():
    declared_count, body = _section(README.read_text(), "## MCP Resources")
    readme_resources = set(re.findall(r"`(nexus://[^`]+)`", body))

    registry_resources = {
        str(r.uri) for r in asyncio.run(_mcp.list_resources())
    }

    assert readme_resources == registry_resources
    assert declared_count == len(registry_resources)
