"""README ↔ MCP registry drift guard.

The tool count and tool list in README.md have drifted from the
MCP registry repeatedly (serve's help said 16, a consumer's docs
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


REFERENCE = (
    Path(__file__).resolve().parents[1]
    / "sphinxcontrib" / "nexus" / "skills" / "nexus-exploring" / "reference.md"
)


def test_skill_reference_documents_every_tool():
    """The skills' shared reference table is the agent-facing tool list —
    the surface a session actually reads to decide what to call. It drifted
    to "Tools (35)" while the registry held 40, with `node_at`,
    `workspaces` and `use_workspace` documented nowhere. Pin it like the
    README."""
    text = REFERENCE.read_text()
    declared = re.search(r"^## Tools \((\d+)\)", text, re.MULTILINE)
    assert declared is not None, "reference.md has no '## Tools (N)' header"

    registry_tools = {t.name for t in asyncio.run(_mcp.list_tools())}
    # Table rows are `| `tool` | ... |`; restrict to real tool names so the
    # edge-type and node-type tables in the same file don't count.
    documented = set(re.findall(r"^\| `(\w+)`", text, re.MULTILINE)) & registry_tools

    assert documented == registry_tools, (
        "undocumented in reference.md: "
        f"{sorted(registry_tools - documented)}"
    )
    assert int(declared.group(1)) == len(registry_tools)


def test_journal_wrapper_preserves_parameter_schemas():
    """The nexus_tool journaling wrapper must stay schema-transparent:
    MCPServer introspects through functools.wraps/__wrapped__, so tool
    parameters survive and Context params stay excluded."""
    schemas = {t.name: t.input_schema for t in asyncio.run(_mcp.list_tools())}
    assert set(schemas["impact"]["properties"]) == {
        "target", "direction", "max_depth", "edge_types", "limit_per_depth",
        "only",
    }
    assert set(schemas["context"]["properties"]) == {
        "node_id", "limit_per_type",
    }
    assert set(schemas["node_at"]["properties"]) == {"file", "line"}
    assert "ctx" not in schemas["session_briefing"].get("properties", {})
