"""Tests for parinfer-aware check_syntax source/file output."""

from pathlib import Path
from typing import Any

import pytest

from acl2_mcp.server import call_tool

pytestmark = pytest.mark.acl2


def text(result: list[Any]) -> str:
    return "".join(block.text for block in result)


@pytest.mark.asyncio
async def test_check_syntax_returns_balanced_source() -> None:
    result = await call_tool(
        "check_syntax",
        {"code": "(defun g (x)\n  (* 2 x)\n", "return_source": True},
    )

    output = text(result)
    assert not output.startswith("Syntax errors found")
    assert output.startswith("(defun g (x)")
    assert output.rstrip().endswith("))")


@pytest.mark.asyncio
async def test_check_syntax_writes_balanced_source(tmp_path: Path) -> None:
    destination = tmp_path / "checked.lisp"

    result = await call_tool(
        "check_syntax",
        {"code": "(defun g (x)\n  (* 2 x)\n", "file_path": str(destination)},
    )

    output = text(result)
    assert output.startswith("Wrote 2 lines")
    assert str(destination) in output
    assert "defun" not in output  # confirmation only

    contents = destination.read_text(encoding="utf-8")
    assert "(defun g (x)\n  (* 2 x))\n" == contents


@pytest.mark.asyncio
async def test_check_syntax_refuses_overwrite_and_allows_it(tmp_path: Path) -> None:
    destination = tmp_path / "checked.lisp"
    destination.write_text("old\n", encoding="utf-8")

    code = "(defun g (x)\n  (* 2 x)\n"

    refused = text(
        await call_tool("check_syntax", {"code": code, "file_path": str(destination)})
    )
    assert refused.startswith("Error: File already exists")

    allowed = text(
        await call_tool(
            "check_syntax",
            {"code": code, "file_path": str(destination), "overwrite": True},
        )
    )
    assert allowed.startswith("Wrote 2 lines")
    assert destination.read_text(encoding="utf-8") == "(defun g (x)\n  (* 2 x))\n"


@pytest.mark.asyncio
async def test_check_syntax_failure_writes_nothing(tmp_path: Path) -> None:
    destination = tmp_path / "checked.lisp"

    result = await call_tool(
        "check_syntax",
        {"code": "(defun x () #q)", "file_path": str(destination)},
    )

    assert text(result).startswith("Syntax errors found")
    assert not destination.exists()


@pytest.mark.asyncio
async def test_check_syntax_parinfer_false_keeps_syntax_errors() -> None:
    result = await call_tool(
        "check_syntax",
        {"code": "(defun bad (x) (* 2 x)))", "parinfer": False},
    )

    assert text(result).startswith("Syntax errors found")


@pytest.mark.asyncio
async def test_check_syntax_incomplete_input_is_reported() -> None:
    """An unclosed form is rejected by the kernel reader, not by a timeout."""
    result = await call_tool(
        "check_syntax",
        {"code": "(defun missing-paren (x) (* 2 x)", "parinfer": False},
    )

    output = text(result)
    assert output.startswith("Syntax errors found")
    assert "incomplete" in output


@pytest.mark.asyncio
async def test_check_syntax_admission_error_is_not_a_syntax_error() -> None:
    """A missing book is readable code; it must not be called a syntax error."""
    result = await call_tool(
        "check_syntax",
        {"code": '(include-book "no/such/book" :dir :system)'},
    )

    output = text(result)
    assert "No syntax errors detected" in output
    assert "does not exist" in output