"""Shared pytest configuration.

Tests that start a real ACL2 are marked ``acl2`` and are skipped when no ACL2
executable is configured, instead of failing with FileNotFoundError.  Configure
one with the ``ACL2`` environment variable or by putting ``acl2`` on PATH.
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


ACL2_AVAILABLE = _acl2_available()


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "acl2: test requires a working ACL2 executable "
        "(ACL2 environment variable or acl2 on PATH)",
    )


@pytest.fixture(autouse=True)
def require_acl2(request: pytest.FixtureRequest) -> None:
    if request.node.get_closest_marker("acl2") and not ACL2_AVAILABLE:
        pytest.skip(
            "ACL2 is not configured; set ACL2 to a working ACL2 binary "
            "or put acl2 on PATH"
        )
