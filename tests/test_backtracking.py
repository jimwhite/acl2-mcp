"""Regression tests for session backtracking, theorem checking, and book loading.

These tests run a real ACL2 (taken from the ACL2 environment variable, or
``acl2`` on PATH).  They pin down behaviour that used to be off by one:
undoing removed the wrong number of events, restoring checkpoints discarded
the checkpoint's own event, and retry_proof could delete the last successful
event.
"""

import asyncio
from pathlib import Path

import pytest

from acl2_mcp.server import call_tool, session_manager

pytestmark = pytest.mark.asyncio


async def _start(name: str) -> str:
    result = await call_tool("start_session", {"name": name})
    text = result[0].text
    assert "Session started successfully" in text, text
    return text.split("ID: ")[1].strip()


async def _end(session_id: str) -> None:
    await call_tool("end_session", {"session_id": session_id})


async def _eval(session_id: str, code: str) -> str:
    result = await call_tool("evaluate", {"code": code, "session_id": session_id})
    return result[0].text


async def _world(session_id: str, limit: int = 20) -> str:
    result = await call_tool(
        "get_world_state", {"session_id": session_id, "limit": limit}
    )
    return result[0].text


async def test_undo_removes_exactly_the_requested_events() -> None:
    """Undo 1 must remove one event, not zero and not two."""
    session_id = await _start("undo-exact")
    try:
        await _eval(session_id, "(defun func-a (x) x)")
        await _eval(session_id, "(defun func-b (x) x)")

        result = await call_tool("undo", {"session_id": session_id, "count": 1})
        text = result[0].text
        assert "Undone 1 event(s)" in text, text
        assert "now at event 1" in text, text

        world = await _world(session_id)
        assert "FUNC-A" in world, world
        assert "FUNC-B" not in world, world
    finally:
        await _end(session_id)


async def test_undo_more_than_available_is_rejected() -> None:
    """Asking to undo more events than exist reports an error, not :ubt 0."""
    session_id = await _start("undo-too-far")
    try:
        await _eval(session_id, "(defun only-func (x) x)")

        result = await call_tool("undo", {"session_id": session_id, "count": 5})
        text = result[0].text
        assert "Cannot undo 5 event(s)" in text, text
        assert "only 1 event(s)" in text, text

        # The failed request must not have disturbed the world.
        world = await _world(session_id)
        assert "ONLY-FUNC" in world, world
    finally:
        await _end(session_id)


async def test_undo_at_empty_world_is_rejected() -> None:
    """With no user events there is nothing to undo."""
    session_id = await _start("undo-empty")
    try:
        result = await call_tool("undo", {"session_id": session_id, "count": 1})
        assert "no events to undo" in result[0].text.lower(), result[0].text
    finally:
        await _end(session_id)


async def test_restore_checkpoint_keeps_the_checkpoint_event() -> None:
    """Restoring to a checkpoint must keep the event it was saved after."""
    session_id = await _start("restore-keeps")
    try:
        await _eval(session_id, "(defun keep-me (x) x)")
        save = await call_tool(
            "save_checkpoint",
            {"session_id": session_id, "checkpoint_name": "after-keep"},
        )
        assert "saved at event 1" in save[0].text, save[0].text

        await _eval(session_id, "(defun drop-me (x) x)")

        restore = await call_tool(
            "restore_checkpoint",
            {"session_id": session_id, "checkpoint_name": "after-keep"},
        )
        text = restore[0].text
        assert "Restored to checkpoint 'after-keep'" in text, text
        assert "event 1" in text, text

        world = await _world(session_id)
        assert "KEEP-ME" in world, world
        assert "DROP-ME" not in world, world
    finally:
        await _end(session_id)


async def test_restore_when_already_there_is_a_no_op() -> None:
    """Restoring to the current position should not undo anything."""
    session_id = await _start("restore-noop")
    try:
        await _eval(session_id, "(defun stay-put (x) x)")
        await call_tool(
            "save_checkpoint",
            {"session_id": session_id, "checkpoint_name": "here"},
        )

        restore = await call_tool(
            "restore_checkpoint",
            {"session_id": session_id, "checkpoint_name": "here"},
        )
        assert "already at checkpoint 'here'" in restore[0].text, restore[0].text

        world = await _world(session_id)
        assert "STAY-PUT" in world, world
    finally:
        await _end(session_id)


async def test_get_world_state_with_limit_one() -> None:
    """A one-event window must not ask :pbt for a command past the end."""
    session_id = await _start("world-limit")
    try:
        await _eval(session_id, "(defun first-func (x) x)")
        await _eval(session_id, "(defun last-func (x) x)")

        world = await _world(session_id, limit=1)
        assert "ACL2 Error" not in world, world
        assert "LAST-FUNC" in world, world
        assert "FIRST-FUNC" not in world, world
    finally:
        await _end(session_id)


async def test_retry_proof_keeps_the_previous_successful_event() -> None:
    """A failed proof adds no event, so retrying must not undo anything."""
    session_id = await _start("retry-safe")
    try:
        await _eval(session_id, "(defun before-failure (x) x)")

        failed = await call_tool(
            "prove",
            {
                "code": "(defthm failing-thm (equal (car (cons a b)) b))",
                "session_id": session_id,
            },
        )
        assert "FAILED" in failed[0].text or "ACL2 Error" in failed[0].text

        retried = await call_tool(
            "retry_proof",
            {
                "session_id": session_id,
                "code": "(defthm succeeding-thm (equal (before-failure x) x))",
            },
        )
        assert "ACL2 Error" not in retried[0].text, retried[0].text

        world = await _world(session_id)
        assert "BEFORE-FAILURE" in world, world
        assert "SUCCEEDING-THM" in world, world
    finally:
        await _end(session_id)


async def test_check_theorem_reproves_without_using_itself(tmp_path: Path) -> None:
    """check_theorem must undo and re-prove the theorem, not call it as a term."""
    book = tmp_path / "checkme.lisp"
    book.write_text(
        "(in-package \"ACL2\")\n"
        "\n"
        "(defun chk-app (x y)\n"
        "  (if (consp x)\n"
        "      (cons (car x) (chk-app (cdr x) y))\n"
        "    y))\n"
        "\n"
        "(defthm chk-app-assoc\n"
        "  (equal (chk-app (chk-app x y) z)\n"
        "         (chk-app x (chk-app y z))))\n",
        encoding="utf-8",
    )

    result = await call_tool(
        "check_theorem",
        {"file_path": str(book), "theorem_name": "chk-app-assoc"},
    )
    text = result[0].text
    assert "Q.E.D." in text, text
    assert "neither a function nor macro definition" not in text, text


async def test_check_theorem_reports_missing_event(tmp_path: Path) -> None:
    """A name that the file never introduces gets a clear error."""
    book = tmp_path / "nomatch.lisp"
    book.write_text("(in-package \"ACL2\")\n(defun present (x) x)\n", encoding="utf-8")

    result = await call_tool(
        "check_theorem",
        {"file_path": str(book), "theorem_name": "absent-name"},
    )
    text = result[0].text
    assert "No event named 'absent-name'" in text, text


async def test_include_book_resolves_system_books() -> None:
    """The documented 'std/lists/append' spelling must work."""
    result = await call_tool(
        "include_book",
        {
            "file_path": "std/lists/append",
            "code": "(thm (equal (append '(1) '(2)) '(1 2)))",
        },
    )
    text = result[0].text
    assert "File not found" not in text, text
    assert "Proof succeeded" in text, text


async def test_dead_session_is_reaped() -> None:
    """A session whose ACL2 process died should not linger as a zombie."""
    session_id = await _start("dead-session")
    session = session_manager.get_session(session_id)
    assert session is not None

    session.process.kill()
    await session.process.wait()
    # Give the child watcher a moment to publish the return code.
    await asyncio.sleep(0.2)

    result = await call_tool(
        "evaluate", {"code": "(+ 1 2)", "session_id": session_id}
    )
    text = result[0].text
    assert "terminated" in text.lower() or "connection lost" in text.lower(), text

    sessions = await call_tool("list_sessions", {})
    listed = sessions[0].text
    assert "[terminated]" in listed, listed

    # The reaper (or an explicit end) should clean it up.
    await call_tool("end_session", {"session_id": session_id})
    sessions = await call_tool("list_sessions", {})
    assert session_id not in sessions[0].text, sessions[0].text
