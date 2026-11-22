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

        assert len(names) == 19
        assert "evaluate" in names
        assert "prove" in names
        assert "parinfer" in names
        assert "export_session_code" in names


@pytest.mark.asyncio
async def test_validation_error_is_a_tool_error() -> None:
    """Validation failures are also reported via isError."""
    async with Client(_server_params(), mode="auto") as client:
        result = await client.call_tool("query_event", {"name": 'append")(+ 1 1)'})

        assert result.is_error
        text = "".join(getattr(block, "text", "") for block in result.content)
        assert "Invalid ACL2 identifier" in text


@pytest.mark.asyncio
async def test_unknown_tool_is_a_tool_error() -> None:
    """An unknown tool is reported via isError, not a JSON-RPC failure."""
    async with Client(_server_params(), mode="auto") as client:
        result = await client.call_tool("nonexistent_tool", {})

        assert result.is_error
        text = "".join(getattr(block, "text", "") for block in result.content)
        assert "Unknown tool" in text


@pytest.mark.acl2
@pytest.mark.asyncio
async def test_acl2_proof_failure_is_a_tool_error() -> None:
    """ACL2 proofs fail with "ACL2 Error"/"FAILED", not "Error:", but the
    result must still be marked isError."""
    async with Client(_server_params(), mode="auto") as client:
        result = await client.call_tool(
            "prove",
            {"code": "(defthm false-thm (equal (len (append x y)) (len x)))"},
        )

        assert result.is_error
        text = "".join(getattr(block, "text", "") for block in result.content)
        assert "FAILED" in text or "ACL2 Error" in text


@pytest.mark.acl2
@pytest.mark.asyncio
async def test_syntax_check_timeout_is_a_tool_error() -> None:
    """A syntax check that times out is not a successful check."""
    async with Client(_server_params(), mode="auto") as client:
        code = (
            "(defun exp-slow (n)"
            " (if (zp n) 1 (+ (exp-slow (1- n)) (exp-slow (1- n)))))\n"
            "(exp-slow 60)"
        )
        result = await client.call_tool(
            "check_syntax", {"code": code, "timeout": 1, "parinfer": False}
        )

        assert result.is_error
        text = "".join(getattr(block, "text", "") for block in result.content)
        assert "timed out" in text
