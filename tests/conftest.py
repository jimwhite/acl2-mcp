"""Shared pytest configuration.

Tests that start a real ACL2 are marked ``acl2`` and are skipped unless both
an ACL2 launcher (``ACL2`` or ``acl2`` on PATH) and the matching ACL2 Jupyter
kernelspec are available.  Install a kernelspec with
``acl2-jupyter-kernel/install-kernelspec.sh``.
"""

import os
import shutil

import pytest


def _acl2_available() -> bool:
    executable = os.environ.get("ACL2") or shutil.which("acl2")
    if not executable:
        return False
    if os.sep in executable or executable.startswith("."):
        return os.path.isfile(executable) and os.access(executable, os.X_OK)
    return shutil.which(executable) is not None


def _kernel_available() -> bool:
    try:
        from acl2_mcp.kernel_backend import KernelConfigError, resolve_kernel_name
    except Exception:
        return False
    try:
        from jupyter_client.kernelspec import KernelSpecManager
    except Exception:
        return False
    try:
        KernelSpecManager().get_kernel_spec(resolve_kernel_name())
        return True
    except Exception:
        return False


ACL2_AVAILABLE = _acl2_available() and _kernel_available()


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "acl2: test requires a working ACL2 launcher and an installed "
        "ACL2 Jupyter kernelspec",
    )


@pytest.fixture(autouse=True)
def require_acl2(request: pytest.FixtureRequest) -> None:
    if request.node.get_closest_marker("acl2") and not ACL2_AVAILABLE:
        pytest.skip(
            "ACL2 is not configured for the kernel backend; set ACL2 to a "
            "working saved_acl2 and install the matching kernelspec"
        )
