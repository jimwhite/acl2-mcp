"""Tests for file-based input (and file output where applicable) across tools.

Every code-oriented tool accepts either inline ``code`` or an ``input_path``;
check_syntax and parinfer additionally support writing their balanced result
to a different file (``output_path``/``file_path``) or back to the input
(``in_place``).
"""

from pathlib import Path
from typing import Any

import pytest

from acl2_mcp.server import call_tool, parinfer_available

needs_parinfer = pytest.mark.skipif(
    not parinfer_available(), reason="parinfer shared library is not available"
)

pytestmark = [pytest.mark.asyncio, pytest.mark.acl2]


def text(result: list[Any]) -> str:
    return "".join(block.text for block in result)


def write(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


async def test_evaluate_reads_input_path(tmp_path: Path) -> None:
    source = write(
        tmp_path / "defs.lisp",
        "(defun from-file (x)\n  (* 2 x))\n",
    )

    result = text(
        await call_tool("evaluate", {"input_path": str(source), "parinfer": False})
    )
    assert "FROM-FILE" in result, result


async def test_prove_reads_input_path(tmp_path: Path) -> None:
    source = write(
        tmp_path / "thm.lisp",
        "(defthm file-thm\n"
        "  (equal (append (list a) b) (cons a b)))\n",
    )

    result = text(await call_tool("prove", {"input_path": str(source)}))
    assert "Q.E.D." in result, result


async def test_admit_reads_input_path(tmp_path: Path) -> None:
    source = write(tmp_path / "event.lisp", "(defun admitted-fn (x) x)\n")

    result = text(await call_tool("admit", {"input_path": str(source)}))
    assert "Admit succeeded" in result, result


async def test_retry_proof_reads_input_path(tmp_path: Path) -> None:
    started = await call_tool("start_session", {"name": "retry-file"})
    session_id = started[0].text.split("ID: ")[1].strip()
    try:
        await call_tool(
            "evaluate",
            {"code": "(defun keeps-existing (x) x)", "session_id": session_id},
        )
        await call_tool(
            "prove",
            {
                "code": "(defthm nope (equal (car (cons a b)) b))",
                "session_id": session_id,
            },
        )

        source = write(
            tmp_path / "retry.lisp",
            "(defthm from-file-thm (equal (keeps-existing x) x))\n",
        )
        result = text(
            await call_tool(
                "retry_proof", {"session_id": session_id, "input_path": str(source)}
            )
        )
        assert "ACL2 Error" not in result, result

        world = text(
            await call_tool(
                "get_world_state", {"session_id": session_id, "limit": 10}
            )
        )
        assert "KEEPS-EXISTING" in world.upper(), world
        assert "FROM-FILE-THM" in world.upper(), world
    finally:
        await call_tool("end_session", {"session_id": session_id})


async def test_check_syntax_accepts_input_path(tmp_path: Path) -> None:
    source = write(tmp_path / "ok.lisp", "(defun ok (x) x)\n")

    result = text(await call_tool("check_syntax", {"input_path": str(source)}))
    assert "No syntax errors detected" in result, result
    assert str(source) in result, result


@needs_parinfer
async def test_check_syntax_writes_input_in_place(tmp_path: Path) -> None:
    source = write(tmp_path / "fixme.lisp", "(defun g (x)\n  (* 2 x)\n")

    result = text(
        await call_tool(
            "check_syntax", {"input_path": str(source), "in_place": True}
        )
    )
    assert result.startswith("Wrote"), result
    assert source.read_text(encoding="utf-8") == "(defun g (x)\n  (* 2 x))\n"


@needs_parinfer
async def test_check_syntax_writes_to_a_different_file(tmp_path: Path) -> None:
    source = write(tmp_path / "source.lisp", "(defun g (x)\n  (* 2 x)\n")
    destination = tmp_path / "balanced.lisp"

    result = text(
        await call_tool(
            "check_syntax",
            {
                "input_path": str(source),
                "output_path": str(destination),
            },
        )
    )
    assert result.startswith("Wrote"), result
    assert destination.read_text(encoding="utf-8") == "(defun g (x)\n  (* 2 x))\n"
    # The input is left untouched unless in_place is requested.
    assert source.read_text(encoding="utf-8") == "(defun g (x)\n  (* 2 x)\n"


async def test_check_syntax_in_place_does_not_write_on_failure(tmp_path: Path) -> None:
    original = "(defun bad (x) (* 2 x)))"
    source = write(tmp_path / "bad.lisp", original)

    result = text(
        await call_tool(
            "check_syntax",
            {"input_path": str(source), "parinfer": False, "in_place": True},
        )
    )
    assert result.startswith("Syntax errors found"), result
    assert source.read_text(encoding="utf-8") == original


@needs_parinfer
async def test_parinfer_accepts_input_path(tmp_path: Path) -> None:
    source = write(tmp_path / "unbalanced.lisp", "(defun f (x)\n  (* 2 x)\n")

    result = text(await call_tool("parinfer", {"input_path": str(source)}))
    assert result.rstrip().endswith("))")


@needs_parinfer
async def test_parinfer_writes_input_in_place(tmp_path: Path) -> None:
    source = write(tmp_path / "unbalanced.lisp", "(defun f (x)\n  (* 2 x)\n")

    result = text(
        await call_tool("parinfer", {"input_path": str(source), "in_place": True})
    )
    assert result.startswith("Wrote"), result
    assert source.read_text(encoding="utf-8") == "(defun f (x)\n  (* 2 x))\n"


@needs_parinfer
async def test_parinfer_writes_to_a_different_file(tmp_path: Path) -> None:
    source = write(tmp_path / "unbalanced.lisp", "(defun f (x)\n  (* 2 x)\n")
    destination = tmp_path / "balanced.lisp"

    result = text(
        await call_tool(
            "parinfer",
            {"input_path": str(source), "output_path": str(destination)},
        )
    )
    assert result.startswith("Wrote"), result
    assert destination.read_text(encoding="utf-8") == "(defun f (x)\n  (* 2 x))\n"


async def test_code_and_input_path_are_mutually_exclusive(tmp_path: Path) -> None:
    source = write(tmp_path / "both.lisp", "(defun both (x) x)\n")

    result = text(
        await call_tool(
            "evaluate", {"code": "(+ 1 2)", "input_path": str(source)}
        )
    )
    assert "either code or input_path, not both" in result, result


async def test_missing_source_is_reported() -> None:
    result = text(await call_tool("evaluate", {}))
    assert "either code or input_path" in result, result


async def test_in_place_requires_input_path() -> None:
    result = text(
        await call_tool("check_syntax", {"code": "(defun x () x)", "in_place": True})
    )
    assert "in_place requires input_path" in result, result


async def test_in_place_conflicts_with_output(tmp_path: Path) -> None:
    source = write(tmp_path / "conflict.lisp", "(defun x () x)\n")
    destination = tmp_path / "other.lisp"

    result = text(
        await call_tool(
            "check_syntax",
            {
                "input_path": str(source),
                "in_place": True,
                "output_path": str(destination),
            },
        )
    )
    assert "in_place cannot be combined" in result, result


async def test_certify_book_accepts_dotted_book_name(tmp_path: Path) -> None:
    """A dot in the book name is part of the name, not the .lisp extension."""
    write(
        tmp_path / "my.book.lisp",
        '(in-package "ACL2")\n(defun dotted-book-fn (x) x)\n',
    )

    result = text(
        await call_tool("certify_book", {"file_path": str(tmp_path / "my.book")})
    )

    assert "CERTIFICATION ATTEMPT" in result, result
    assert "File not found" not in result, result
    assert (tmp_path / "my.book.cert").exists(), result


async def test_check_syntax_write_reports_execution_failure(tmp_path: Path) -> None:
    """Writing the balanced source must not hide why execution failed."""
    source = write(
        tmp_path / "fails.lisp", "(defthm write-impossible (equal 1 2))\n"
    )
    destination = tmp_path / "written.lisp"

    result = text(
        await call_tool(
            "check_syntax",
            {"input_path": str(source), "output_path": str(destination)},
        )
    )

    assert result.startswith("Wrote"), result
    assert "ACL2 reported errors" in result, result
    assert "ACL2 Error" in result or "FAILED" in result, result
    assert destination.exists()
