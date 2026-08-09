"""End-to-end: does the MCP server actually START and serve tools?

Every other server test imports `server.py` in-process and inspects the
registry. That is exactly the blind spot that let mcp 2.0.0 ship a dead
server for two months: `mcp.server.fastmcp` had been removed, so an
install resolved to 2.x could not spawn at all — yet the package
imported fine, the CLI worked, and every unit test passed, because
nothing ever launched the process an MCP client would launch.

This test launches it the way a client does — a subprocess speaking
JSON-RPC over stdio — and completes the handshake. It is deliberately
the slowest test in the suite (~10s); that cost buys the one signal the
fast tests structurally cannot give. No marker gates it — this repo runs
plain pytest with no marker taxonomy, and a test you can skip is a test
that will be skipped on the day it matters.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from mcp import Client  # noqa: E402
from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: E402

from sphinxcontrib.nexus.export import write_sqlite  # noqa: E402
from sphinxcontrib.nexus.graph import (  # noqa: E402
    EdgeType,
    GraphEdge,
    GraphNode,
    KnowledgeGraph,
    NodeType,
)


def _tiny_graph(db_path: Path) -> None:
    kg = KnowledgeGraph()
    kg.add_node(GraphNode(
        id="py:module:pkg", type=NodeType.MODULE, name="pkg",
        display_name="pkg", domain="py",
        metadata={"file_path": "/x/pkg/__init__.py"},
    ))
    kg.add_node(GraphNode(
        id="py:function:pkg.solve", type=NodeType.FUNCTION, name="pkg.solve",
        display_name="solve", domain="py",
        metadata={"file_path": "/x/pkg/a.py", "lineno": 1},
    ))
    kg.add_edge(GraphEdge(
        source="py:module:pkg", target="py:function:pkg.solve",
        type=EdgeType.CONTAINS,
    ))
    write_sqlite(kg, db_path)


async def _handshake(db_path: Path) -> tuple[list[str], str]:
    """Spawn `nexus serve` and complete a real client handshake."""
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "sphinxcontrib.nexus.cli", "serve", "--db", str(db_path)],
    )
    # `stdio_client(params)` IS the transport (an async context manager
    # yielding the stream pair); Client enters it. Passing a string here
    # would mean an HTTP URL, not a command.
    async with Client(stdio_client(params)) as client:
        listed = await client.list_tools()
        result = await client.call_tool("stats", {})
    # The client returns the protocol result model, not a bare list —
    # iterating the model itself yields (field, value) pairs, which is a
    # quiet way to get nonsense rather than an error.
    tools = getattr(listed, "tools", listed)
    text = ""
    for block in result.content:
        text += getattr(block, "text", "")
    return sorted(t.name for t in tools), text


def test_server_spawns_and_serves_tools(tmp_path):
    db = tmp_path / "graph.db"
    _tiny_graph(db)

    try:
        names, stats_text = asyncio.run(asyncio.wait_for(_handshake(db), 90))
    except asyncio.TimeoutError:  # pragma: no cover - CI hang guard
        pytest.fail("MCP server did not complete a handshake within 90s")

    # The registry is asserted elsewhere; here the point is that a CLIENT
    # can reach it at all, over a real process boundary.
    assert names, "server started but advertised no tools"
    assert "stats" in names
    assert "dead_references" in names
    # And that a call round-trips actual graph content, not just metadata.
    assert "pkg.solve" in stats_text or "function" in stats_text


def test_spawned_tool_count_matches_the_in_process_registry(tmp_path):
    """A client sees exactly the tools the module registers — catches a
    transport/serialization regression that drops or renames tools."""
    from sphinxcontrib.nexus.server import _mcp

    db = tmp_path / "graph.db"
    _tiny_graph(db)
    names, _ = asyncio.run(asyncio.wait_for(_handshake(db), 90))
    in_process = sorted(t.name for t in asyncio.run(_mcp.list_tools()))
    assert names == in_process


def test_cli_module_is_executable():
    """`python -m sphinxcontrib.nexus.cli` is how the spawn test (and any
    venv-less MCP config) launches the server."""
    assert shutil.which(sys.executable)
    from sphinxcontrib.nexus import cli

    assert hasattr(cli, "main")
