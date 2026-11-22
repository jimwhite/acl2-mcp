"""Regression tests for the fixes in the session, export, syntax, and retry paths."""

from typing import Any

import pytest

from acl2_mcp.server import call_tool

pytestmark = [pytest.mark.asyncio, pytest.mark.acl2]


def text(result: list[Any]) -> str:
    return "".join(block.text for block in result)


async def start_session(**arguments: Any) -> str:
    result = await call_tool("start_session", arguments)
    return text(result).split("ID: ")[1].strip()


async def test_admit_in_session_is_rolled_back() -> None:
    """admit tests an event in context but must not leave it in the world."""
    session_id = await start_session()
    try:
        await call_tool(
            "evaluate", {"code": "(defun keep-me (x) x)", "session_id": session_id}
        )

        result = text(
            await call_tool(
                "admit",
                {"code": "(defun admit-temp (x) x)", "session_id": session_id},
            )
        )
        assert "Admit succeeded" in result
        assert "rolled back" in result

        world = text(
            await call_tool(
                "get_world_state", {"session_id": session_id, "limit": 10}
            )
        )
        assert "KEEP-ME" in world.upper()
        assert "ADMIT-TEMP" not in world.upper()

        exported = text(
            await call_tool("export_session_code", {"session_id": session_id})
        )
        assert "keep-me" in exported
        assert "admit-temp" not in exported
    finally:
        await call_tool("end_session", {"session_id": session_id})


async def test_export_prunes_undone_events_and_skips_non_events() -> None:
    """The export log follows the current world and omits bare evaluations."""
    session_id = await start_session()
    try:
        await call_tool(
            "evaluate", {"code": "(defun keep-a (x) x)", "session_id": session_id}
        )
        await call_tool(
            "evaluate", {"code": "(defun drop-b (x) x)", "session_id": session_id}
        )
        await call_tool(
            "evaluate", {"code": "(+ 2 3)", "session_id": session_id}
        )
        await call_tool("undo", {"session_id": session_id, "count": 1})

        exported = text(
            await call_tool("export_session_code", {"session_id": session_id})
        )
        assert "(defun keep-a (x) x)" in exported
        assert "drop-b" not in exported
        assert "(+ 2 3)" not in exported
    finally:
        await call_tool("end_session", {"session_id": session_id})


async def test_retry_proof_balances_and_exports() -> None:
    """retry_proof must balance code and count as an accepted event."""
    session_id = await start_session()
    try:
        result = text(
            await call_tool(
                "retry_proof",
                {
                    "session_id": session_id,
                    "code": "(defun retry-fn (x)\n  (* 3 x)\n",
                },
            )
        )
        assert "Retry proof result" in result
        assert "RETRY-FN" in result.upper()

        exported = text(
            await call_tool("export_session_code", {"session_id": session_id})
        )
        assert "retry-fn" in exported
        assert "(* 3 x))" in exported
    finally:
        await call_tool("end_session", {"session_id": session_id})


async def test_check_syntax_distinguishes_execution_errors() -> None:
    """A proof/admission failure is reported, but not as a reader error."""
    result = text(
        await call_tool(
            "check_syntax", {"code": "(defthm impossible (equal 1 2))"}
        )
    )
    assert "No syntax errors detected" in result
    assert "ACL2 reported errors" in result


async def test_check_syntax_timeout_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bounded run that times out must not look like a successful check."""

    from acl2_mcp.kernel_backend import KernelTimeout

    def fake_check_syntax(code: str, timeout: int = 5):
        raise KernelTimeout(f"timed out after {timeout} seconds")

    monkeypatch.setattr("acl2_mcp.server.check_syntax_once", fake_check_syntax)

    result = text(await call_tool("check_syntax", {"code": "(+ 1 2)", "timeout": 3}))
    assert result.startswith("Syntax check timed out after 3 seconds")
    assert "No syntax errors detected" not in result


async def test_session_timeout_terminates_session() -> None:
    """A timed-out command kills the process instead of desynchronizing it."""
    session_id = await start_session()
    try:
        first = text(
            await call_tool(
                "evaluate",
                {
                    "code": "(defun wedged (x)",
                    "session_id": session_id,
                    "parinfer": False,
                    "timeout": 5,
                },
            )
        )
        # The kernel reports a reader error immediately instead of hanging.
        assert "end of file" in first.lower() or "error" in first.lower()

        second = text(
            await call_tool(
                "evaluate",
                {"code": "(+ 1 2)", "session_id": session_id, "timeout": 5},
            )
        )
        assert "3" in second
    finally:
        await call_tool("end_session", {"session_id": session_id})
