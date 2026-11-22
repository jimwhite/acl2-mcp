"""Unit tests for kernel selection and ACL2 environment handling."""

from pathlib import Path

import pytest

from acl2_mcp.kernel_backend import (
    CellResult,
    KernelConfigError,
    _acl2_script_lisp,
    _validate_acl2_environment,
    kernel_environment,
    resolve_kernel_name,
)


def _script(tmp_path: Path, exec_line: str) -> Path:
    path = tmp_path / "saved_acl2"
    path.write_text(f"#!/bin/sh\nexport X=1\nexec {exec_line}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_detects_sbcl_from_core_argument(tmp_path: Path) -> None:
    script = _script(tmp_path, '"/usr/bin/sbcl" --core "/x/saved_acl2.core"')
    assert _acl2_script_lisp(str(script)) == "sbcl"


def test_detects_ccl_from_image_argument(tmp_path: Path) -> None:
    script = _script(tmp_path, '"/usr/local/lib/ccl/arm64cl" -I "/x/saved_acl2.image"')
    assert _acl2_script_lisp(str(script)) == "ccl"


def test_ambiguous_script_returns_none(tmp_path: Path) -> None:
    script = _script(tmp_path, '"/usr/bin/lisp"')
    assert _acl2_script_lisp(str(script)) is None


def test_acl2_script_wins_over_lisp_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _script(tmp_path, '"/usr/local/lib/ccl/arm64cl" -I "/x/image"')
    monkeypatch.setenv("ACL2", str(script))
    monkeypatch.setenv("LISP", "sbcl")
    monkeypatch.delenv("ACL2_MCP_KERNEL", raising=False)
    assert resolve_kernel_name() == "acl2"


def test_lisp_env_resolves_ambiguity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _script(tmp_path, '"/usr/bin/lisp"')
    monkeypatch.setenv("ACL2", str(script))
    monkeypatch.setenv("LISP", "sbcl")
    monkeypatch.delenv("ACL2_MCP_KERNEL", raising=False)
    assert resolve_kernel_name() == "acl2-sbcl"


def test_explicit_kernel_name_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACL2_MCP_KERNEL", "custom-kernel")
    monkeypatch.setenv("LISP", "sbcl")
    assert resolve_kernel_name() == "custom-kernel"


def test_missing_acl2_executable_is_a_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ACL2", "/nonexistent-acl2-script")
    with pytest.raises(KernelConfigError) as excinfo:
        _validate_acl2_environment()
    assert "Could not start ACL2 executable" in str(excinfo.value)
    assert "ACL2 environment variable" in str(excinfo.value)


def test_acl2_environment_is_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _script(tmp_path, '"/usr/bin/sbcl" --core "/x/core"')
    monkeypatch.setenv("ACL2", str(script))
    monkeypatch.setenv("ACL2_SYSTEM_BOOKS", "/books")
    monkeypatch.delenv("ACL2_CUSTOMIZATION", raising=False)
    _validate_acl2_environment()
    env = kernel_environment()
    assert env["ACL2"] == str(script)
    assert env["ACL2_SYSTEM_BOOKS"] == "/books"
    assert env["ACL2_CUSTOMIZATION"] == "NONE"


def test_cell_result_text_prefixes_kernel_errors() -> None:
    result = CellResult(
        stdout="before\n",
        error={"ename": "SIMPLE-ERROR", "evalue": "boom"},
    )
    text = result.text
    assert "before" in text
    assert "Error: SIMPLE-ERROR: boom" in text
