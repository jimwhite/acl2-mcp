"""Tests for in-process parinfer balancing."""

from typing import Any

import pytest

from acl2_mcp.server import call_tool, parinfer_available

pytestmark = pytest.mark.skipif(
    not parinfer_available(), reason="parinfer shared library is not available"
)


def text(result: list[Any]) -> str:
    return "".join(block.text for block in result)


async def start_session(**arguments: Any) -> str:
    result = await call_tool("start_session", arguments)
    return text(result).split("ID: ")[1].strip()


@pytest.mark.asyncio
async def test_parinfer_tool_balances_missing_paren() -> None:
    """Indent mode derives the missing closing parens from indentation."""
    result = await call_tool("parinfer", {"code": "(defun f (x)\n  (* 2 x)\n"})

    output = text(result)
    assert not output.startswith("Error:")
    assert output.startswith("(defun f (x)")
    assert output.rstrip().endswith("))")


@pytest.mark.asyncio
async def test_parinfer_paren_mode_reports_unclosed_paren() -> None:
    """Paren mode reports unclosed parens instead of fixing them."""
    result = await call_tool(
        "parinfer", {"code": "(defun f (x)\n  (* 2 x)\n", "mode": "paren"}
    )

    assert "unclosed" in text(result).lower()


@pytest.mark.asyncio
async def test_parinfer_ignores_quotes_in_line_comments() -> None:
    """Common Lisp comments end at newline; prose quotes are not strings."""
    code = '(defun f (x)\n  ; "quoted prose\n  x)\n'

    output = text(await call_tool("parinfer", {"code": code}))

    assert not output.startswith("Error:")


@pytest.mark.asyncio
async def test_session_auto_balances_and_exports_balanced_source() -> None:
    """Sessions balance by default and export the code ACL2 actually got."""
    session_id = await start_session()
    try:
        result = await call_tool(
            "evaluate",
            {"code": "(defun triple (x)\n  (* 3 x)\n", "session_id": session_id},
        )
        output = text(result)

        assert output.startswith("[parinfer adjusted the code]")
        assert "TRIPLE" in output.upper()

        exported = text(
            await call_tool("export_session_code", {"session_id": session_id})
        )
        assert "(* 3 x))" in exported
    finally:
        await call_tool("end_session", {"session_id": session_id})


@pytest.mark.asyncio
async def test_per_call_parinfer_false_skips_balancing() -> None:
    """parinfer=false submits the code unchanged, so the extra paren fails."""
    session_id = await start_session()
    try:
        result = await call_tool(
            "evaluate",
            {
                "code": "(defun bad (x) (* 2 x)))",
                "session_id": session_id,
                "parinfer": False,
                "timeout": 10,
            },
        )

        assert "parinfer adjusted" not in text(result)
        exported = text(
            await call_tool("export_session_code", {"session_id": session_id})
        )
        assert exported.startswith("No accepted code")
    finally:
        await call_tool("end_session", {"session_id": session_id})


@pytest.mark.asyncio
async def test_session_parinfer_false_skips_balancing() -> None:
    """A session can disable parinfer for all its submissions."""
    session_id = await start_session(parinfer=False)
    try:
        result = await call_tool(
            "evaluate",
            {"code": "(defun bad (x) (* 2 x)))", "session_id": session_id},
        )

        assert "parinfer adjusted" not in text(result)
        exported = text(
            await call_tool("export_session_code", {"session_id": session_id})
        )
        assert exported.startswith("No accepted code")
    finally:
        await call_tool("end_session", {"session_id": session_id})
