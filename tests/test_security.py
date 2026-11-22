"""Security tests for ACL2 MCP server."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from acl2_mcp.server import (
    validate_timeout,
    validate_acl2_identifier,
    escape_acl2_string,
    validate_file_path,
    validate_output_path,
    acl2_output_failed,
    call_tool,
    handle_call_tool,
)


def test_acl2_output_failed_recognizes_error_headers() -> None:
    """ACL2's error headers are failures."""
    assert acl2_output_failed("ACL2 Error in ( DEFUN U ...): boom")
    assert acl2_output_failed("ACL2 Error [Failure] in ( DEFTHM T ...): no")
    assert acl2_output_failed("  ACL2 Error: no context")
    assert acl2_output_failed("******** FAILED ********")
    assert acl2_output_failed("HARD ACL2 ERROR in FOO")


def test_acl2_output_failed_ignores_user_text() -> None:
    """Output containing the words, but not an ACL2 error header, is a success."""
    assert not acl2_output_failed("ACL2 Error")
    assert not acl2_output_failed("nil\nACL2 Error\n")
    assert not acl2_output_failed("The ACL2 Error messages are documented.")


def test_validate_timeout_clamps_max() -> None:
    """Test that timeout is clamped to maximum."""
    assert validate_timeout(1000) == 300


def test_validate_timeout_clamps_min() -> None:
    """Test that timeout is clamped to minimum."""
    assert validate_timeout(0) == 1
    assert validate_timeout(-10) == 1


def test_validate_timeout_handles_float() -> None:
    """Test that float timeouts are converted to int."""
    assert validate_timeout(5.7) == 5


def test_validate_timeout_handles_invalid_type() -> None:
    """Test that invalid types return default."""
    assert validate_timeout("invalid") == 30  # type: ignore


def test_validate_acl2_identifier_rejects_quotes() -> None:
    """Test that identifiers with quotes are rejected."""
    with pytest.raises(ValueError, match="Invalid ACL2 identifier"):
        validate_acl2_identifier('malicious")(+ 1 1)')


def test_validate_acl2_identifier_rejects_parens() -> None:
    """Test that identifiers with parens are rejected."""
    with pytest.raises(ValueError, match="Invalid ACL2 identifier"):
        validate_acl2_identifier("malicious)(+ 1 1)")


def test_validate_acl2_identifier_rejects_empty() -> None:
    """Test that empty identifiers are rejected."""
    with pytest.raises(ValueError, match="cannot be empty"):
        validate_acl2_identifier("")


def test_validate_acl2_identifier_rejects_whitespace_and_semicolons() -> None:
    """Names cannot break out of the surrounding form via newline/comment."""
    for bad in ("append\n:pe car", "append\r:pe car", "append; comment", "a b"):
        with pytest.raises(ValueError, match="Invalid ACL2 identifier"):
            validate_acl2_identifier(bad)


def test_validate_acl2_identifier_accepts_valid() -> None:
    """Test that valid identifiers are accepted."""
    assert validate_acl2_identifier("my-function") == "my-function"
    assert validate_acl2_identifier("my_function") == "my_function"
    assert validate_acl2_identifier("my-function-123") == "my-function-123"


def test_escape_acl2_string_escapes_quotes() -> None:
    """Test that quotes are properly escaped."""
    assert escape_acl2_string('test"quote') == 'test\\"quote'


def test_escape_acl2_string_escapes_backslashes() -> None:
    """Test that backslashes are properly escaped."""
    assert escape_acl2_string('test\\path') == 'test\\\\path'


def test_escape_acl2_string_escapes_both() -> None:
    """Test that both backslashes and quotes are escaped."""
    assert escape_acl2_string('test\\"path') == 'test\\\\\\"path'


def test_validate_file_path_rejects_empty() -> None:
    """Test that empty paths are rejected."""
    with pytest.raises(ValueError, match="cannot be empty"):
        validate_file_path("")


def test_validate_file_path_rejects_nonexistent() -> None:
    """Test that nonexistent files are rejected."""
    with pytest.raises(ValueError, match="not found"):
        validate_file_path("/tmp/nonexistent_file_12345.lisp")


def test_validate_file_path_rejects_directory(tmp_path: Any) -> None:
    """Test that directories are rejected."""
    with pytest.raises(ValueError, match="not a file"):
        validate_file_path(str(tmp_path))


def test_validate_file_path_accepts_valid_file(tmp_path: Any) -> None:
    """Test that valid files are accepted."""
    test_file = tmp_path / "test.lisp"
    test_file.write_text("(+ 1 1)")

    result = validate_file_path(str(test_file))
    assert result.exists()
    assert result.is_file()


@pytest.mark.asyncio
async def test_code_length_limit() -> None:
    """Test that extremely long code is rejected."""
    long_code = "a" * 2_000_000  # 2MB of code

    result = await call_tool("evaluate", {"code": long_code})

    assert len(result) == 1
    assert "exceeds maximum length" in result[0].text


@pytest.mark.asyncio
async def test_missing_acl2_executable_is_a_tool_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing ACL2 binary returns a clear error, not a traceback."""
    monkeypatch.setenv("ACL2", "/nonexistent-acl2")

    result = await call_tool("evaluate", {"code": "(+ 1 2)"})

    assert len(result) == 1
    assert "Could not start ACL2 executable" in result[0].text
    assert "ACL2 environment variable" in result[0].text


@pytest.mark.asyncio
async def test_start_session_missing_acl2_is_a_tool_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Starting a session with a bad ACL2 path reports the executable."""
    monkeypatch.setenv("ACL2", "/nonexistent-acl2")

    result = await call_tool("start_session", {"name": "missing-acl2"})

    assert len(result) == 1
    assert "Could not start ACL2 executable" in result[0].text


def test_validate_file_path_reports_the_full_path(tmp_path: Path) -> None:
    missing = tmp_path / "nested" / "missing.lisp"
    with pytest.raises(ValueError) as excinfo:
        validate_file_path(str(missing))
    assert str(missing) in str(excinfo.value)


def test_validate_output_path_reports_the_full_path(tmp_path: Path) -> None:
    existing = tmp_path / "existing.lisp"
    existing.write_text("(+ 1 1)\n", encoding="utf-8")
    with pytest.raises(ValueError) as excinfo:
        validate_output_path(str(existing))
    assert str(existing) in str(excinfo.value)


@pytest.mark.asyncio
async def test_unknown_argument_is_rejected() -> None:
    """A misspelled argument must not be silently ignored."""
    result = await handle_call_tool(
        None,
        SimpleNamespace(name="list_sessions", arguments={"sessionid": "typo"}),
    )
    assert result.is_error
    text = "".join(block.text for block in result.content)
    assert "Unknown argument" in text
    assert "sessionid" in text


@pytest.mark.asyncio
async def test_missing_required_argument_is_a_tool_error() -> None:
    """Omitting a required argument surfaces as isError, not a KeyError."""
    result = await handle_call_tool(
        None, SimpleNamespace(name="undo", arguments={})
    )

    assert result.is_error
    output = "".join(block.text for block in result.content)
    assert "Missing required argument" in output
    assert "session_id" in output


@pytest.mark.asyncio
async def test_query_event_injection_protection() -> None:
    """Test that query_event protects against code injection."""
    malicious_name = 'append")(+ 1 1)'

    result = await call_tool("query_event", {"name": malicious_name})

    assert len(result) == 1
    assert "Error" in result[0].text
    assert "Invalid" in result[0].text


@pytest.mark.asyncio
async def test_verify_guards_injection_protection() -> None:
    """Test that verify_guards protects against code injection."""
    malicious_name = 'len)(+ 1 1)'

    result = await call_tool("verify_guards", {"function_name": malicious_name})

    assert len(result) == 1
    assert "Error" in result[0].text
    assert "Invalid" in result[0].text


@pytest.mark.asyncio
async def test_check_theorem_injection_protection(tmp_path: Any) -> None:
    """Test that check_theorem protects against code injection."""
    # Create a valid file
    test_file = tmp_path / "test.lisp"
    test_file.write_text("(defthm test-thm (equal (+ 1 1) 2))")

    malicious_theorem = 'test-thm")(+ 1 1)'

    result = await call_tool(
        "check_theorem",
        {"file_path": str(test_file), "theorem_name": malicious_theorem},
    )

    assert len(result) == 1
    assert "Error" in result[0].text
    assert "Invalid" in result[0].text
