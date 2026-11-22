"""Tests for exporting the code accepted into a persistent session."""

from pathlib import Path
from typing import Any

import pytest

from acl2_mcp.server import call_tool

pytestmark = pytest.mark.acl2


def text(result: list[Any]) -> str:
    return "".join(block.text for block in result)


async def start_session(**arguments: Any) -> str:
    result = await call_tool("start_session", arguments)
    return text(result).split("ID: ")[1].strip()


@pytest.mark.asyncio
async def test_export_only_accepted_code(tmp_path: Path) -> None:
    """Failing submissions are not exported; accepted ones are, in order."""
    session_id = await start_session()
    try:
        await call_tool(
            "evaluate", {"code": "(defun first-fn (x) (+ x 1))", "session_id": session_id}
        )
        await call_tool(
            "evaluate", {"code": '(defthm bogus (equal 1 2))', "session_id": session_id}
        )
        await call_tool(
            "evaluate", {"code": "(defun second-fn (x) (+ x 2))", "session_id": session_id}
        )

        exported = text(
            await call_tool("export_session_code", {"session_id": session_id})
        )

        assert "(defun first-fn (x) (+ x 1))" in exported
        assert "(defun second-fn (x) (+ x 2))" in exported
        assert "bogus" not in exported
        assert exported.index("first-fn") < exported.index("second-fn")
    finally:
        await call_tool("end_session", {"session_id": session_id})


@pytest.mark.asyncio
async def test_export_empty_session() -> None:
    session_id = await start_session()
    try:
        exported = text(
            await call_tool("export_session_code", {"session_id": session_id})
        )
        assert exported.startswith("No accepted code")
    finally:
        await call_tool("end_session", {"session_id": session_id})


@pytest.mark.asyncio
async def test_export_to_file_returns_confirmation_only(tmp_path: Path) -> None:
    session_id = await start_session()
    try:
        await call_tool(
            "evaluate", {"code": "(defun exported-fn (x) x)", "session_id": session_id}
        )

        destination = tmp_path / "session.lisp"
        result = text(
            await call_tool(
                "export_session_code",
                {"session_id": session_id, "file_path": str(destination)},
            )
        )

        assert "Exported 1 accepted form(s)" in result
        assert str(destination) in result
        assert "defun" not in result  # confirmation only, not the source

        contents = destination.read_text(encoding="utf-8")
        assert "(defun exported-fn (x) x)" in contents
        assert contents.endswith("\n")
    finally:
        await call_tool("end_session", {"session_id": session_id})


@pytest.mark.asyncio
async def test_export_refuses_overwrite_and_allows_it(tmp_path: Path) -> None:
    session_id = await start_session()
    try:
        await call_tool(
            "evaluate", {"code": "(defun overwrite-fn (x) x)", "session_id": session_id}
        )

        destination = tmp_path / "session.lisp"
        destination.write_text("old\n", encoding="utf-8")

        refused = text(
            await call_tool(
                "export_session_code",
                {"session_id": session_id, "file_path": str(destination)},
            )
        )
        assert refused.startswith("Error: File already exists")
        assert destination.read_text(encoding="utf-8") == "old\n"

        allowed = text(
            await call_tool(
                "export_session_code",
                {
                    "session_id": session_id,
                    "file_path": str(destination),
                    "overwrite": True,
                },
            )
        )
        assert "Exported 1 accepted form(s)" in allowed
        assert "(defun overwrite-fn (x) x)" in destination.read_text(encoding="utf-8")
    finally:
        await call_tool("end_session", {"session_id": session_id})


@pytest.mark.asyncio
async def test_export_rejects_missing_parent(tmp_path: Path) -> None:
    session_id = await start_session()
    try:
        await call_tool(
            "evaluate", {"code": "(defun parent-fn (x) x)", "session_id": session_id}
        )

        result = text(
            await call_tool(
                "export_session_code",
                {
                    "session_id": session_id,
                    "file_path": str(tmp_path / "missing" / "session.lisp"),
                },
            )
        )

        assert result.startswith("Error: Parent directory does not exist")
    finally:
        await call_tool("end_session", {"session_id": session_id})
