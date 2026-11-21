"""Protocol-level tests for the ACL2 MCP server.

These connect to the server as a real MCP client and assert that it negotiates
the current (2026-07-28) wire era rather than falling back to the legacy
handshake, and that tool errors surface as tool results.
"""

import os
import sys

import pytest

from mcp import Client, StdioServerParameters


def _server_params() -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "acl2_mcp.server"],
        env={**os.environ},
    )


@pytest.mark.asyncio
async def test_connects_in_modern_era_and_lists_tools() -> None:
    """The server must negotiate 2026-07-28 and expose every tool."""
    async with Client(_server_params(), mode="auto") as client:
        assert client.protocol_version == "2026-07-28"

        result = await client.list_tools()
        names = {tool.name for tool in result.tools}

        assert len(names) == 17
        assert "evaluate" in names
        assert "prove" in names


@pytest.mark.asyncio
async def test_unknown_tool_is_a_tool_error() -> None:
    """An unknown tool is reported via isError, not a JSON-RPC failure."""
    async with Client(_server_params(), mode="auto") as client:
        result = await client.call_tool("nonexistent_tool", {})

        assert result.is_error
        text = "".join(getattr(block, "text", "") for block in result.content)
        assert "Unknown tool" in text
