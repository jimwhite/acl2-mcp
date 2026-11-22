"""Tests for first-run provisioning (parinfer build and kernelspec install)."""

import json
import os
import uuid
from pathlib import Path

import pytest

from acl2_mcp import provision


def test_parinfer_library_name_is_platform_specific() -> None:
    name = provision.parinfer_library_name()
    assert name.startswith(("libparinfer_rust", "parinfer_rust"))
    assert name.endswith((".so", ".dylib", ".dll"))


def test_find_parinfer_library_honors_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = tmp_path / provision.parinfer_library_name()
    library.write_bytes(b"not really a library")
    monkeypatch.setenv("PARINFER_LIB", str(library))
    assert provision.find_parinfer_library() == library

    monkeypatch.setenv("PARINFER_LIB", str(tmp_path / "missing.so"))
    assert provision.find_parinfer_library() is None


def test_find_kernel_dir_honors_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / provision.KERNEL_ASD).write_text("(asdf:defsystem \"x\")\n")
    monkeypatch.setenv("ACL2_MCP_KERNEL_DIR", str(tmp_path))
    assert provision.find_kernel_dir() == tmp_path


def test_build_parinfer_can_be_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACL2_MCP_SKIP_PARINFER_BUILD", "1")
    assert provision.build_parinfer() is None


def test_kernelspec_install_requires_quicklisp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    acl2 = os.environ.get("ACL2")
    if not acl2 or not os.path.isfile(acl2):
        pytest.skip("ACL2 is not configured")
    monkeypatch.setenv("ACL2", acl2)
    monkeypatch.setenv("JUPYTER_DATA_DIR", str(tmp_path / "jupyter"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("ACL2_MCP_QUICKLISP_SETUP", str(tmp_path / "missing" / "setup.lisp"))
    with pytest.raises(provision.ProvisionError, match="quicklisp"):
        provision.ensure_kernelspec(f"acl2-mcp-missing-{uuid.uuid4().hex[:8]}")


def test_install_kernelspec_and_launch_a_kernel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: install a kernelspec from the bundle, then run ACL2 in it."""
    acl2 = os.environ.get("ACL2")
    if not acl2 or not os.path.isfile(acl2):
        pytest.skip("ACL2 is not configured")
    quicklisp = Path.home() / "quicklisp" / "setup.lisp"
    if not quicklisp.is_file():
        pytest.skip("Quicklisp is not available")
    if provision.find_kernel_dir() is None:
        pytest.skip("bundled kernel sources are not available")

    name = f"acl2-mcp-test-{uuid.uuid4().hex[:8]}"
    monkeypatch.setenv("ACL2", acl2)
    monkeypatch.setenv("JUPYTER_DATA_DIR", str(tmp_path / "jupyter"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("ACL2_MCP_QUICKLISP_SETUP", str(quicklisp))

    provision.ensure_kernelspec(name)

    spec_path = tmp_path / "jupyter" / "kernels" / name / "kernel.json"
    assert spec_path.is_file()
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    assert any(
        isinstance(argument, str) and "asdf:*central-registry*" in argument
        for argument in spec["argv"]
    )

    from acl2_mcp.kernel_backend import KernelClient

    client = KernelClient(kernel_name=name)
    try:
        result = client.execute("(+ 20 22)", 120)
        assert result.text.strip() == "42"
    finally:
        client.shutdown()


def test_ensure_parinfer_is_idempotent() -> None:
    first = provision.ensure_parinfer()
    second = provision.ensure_parinfer()
    assert first == second
    if first is not None:
        assert first.is_file()
