"""Build hooks for the acl2-mcp package.

The wheel bundles the ACL2 Jupyter kernel sources and a freshly built
parinfer-rust shared library so a plain ``pip install`` is enough on a machine
that already has ACL2 and its Lisp implementation.  Set
``ACL2_MCP_SKIP_PARINFER_BUILD=1`` to install without parinfer balancing.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from setuptools import setup
from setuptools.command.build_py import build_py as _build_py
from wheel.bdist_wheel import bdist_wheel as _bdist_wheel

from acl2_mcp.provision import (
    KERNEL_ASD,
    PACKAGE_DIR,
    ProvisionError,
    build_parinfer,
    find_kernel_dir,
    parinfer_source_available,
)

KERNEL_DEST = PACKAGE_DIR / "_kernel"


def _stage_kernel_sources() -> None:
    source = find_kernel_dir()
    if source is None:
        raise RuntimeError(
            "ACL2 Jupyter kernel sources were not found. Build from a source "
            "checkout with 'git submodule update --init --recursive' or set "
            "ACL2_MCP_KERNEL_DIR."
        )
    if source.resolve() == KERNEL_DEST.resolve():
        return
    if KERNEL_DEST.exists():
        shutil.rmtree(KERNEL_DEST)
    KERNEL_DEST.mkdir(parents=True, exist_ok=True)
    for pattern in ("*.lisp", "*.asd"):
        for path in source.glob(pattern):
            shutil.copy2(path, KERNEL_DEST / path.name)


def _build_parinfer_library() -> None:
    if not parinfer_source_available():
        raise RuntimeError(
            "parinfer-rust sources were not found. Build from a source checkout "
            "with 'git submodule update --init --recursive', or set "
            "ACL2_MCP_SKIP_PARINFER_BUILD=1 to install without balancing."
        )
    try:
        build_parinfer()
    except ProvisionError as exc:
        raise RuntimeError(str(exc)) from exc


class build_py(_build_py):
    """Stage package data and build the Rust library before copying packages."""

    def run(self) -> None:
        _stage_kernel_sources()
        _build_parinfer_library()
        super().run()


class bdist_wheel(_bdist_wheel):
    """Tag the wheel for this platform because it bundles a native library.

    parinfer-rust is loaded with ctypes, so it is ABI-independent (``py3`` /
    ``none``) but it is still a platform-specific shared object.
    """

    def finalize_options(self) -> None:
        super().finalize_options()
        self.root_is_pure = False

    def get_tag(self) -> tuple[str, str, str]:
        _, _, platform_tag = super().get_tag()
        return "py3", "none", platform_tag


cmdclass: dict[str, Any] = {"build_py": build_py, "bdist_wheel": bdist_wheel}

setup(cmdclass=cmdclass)
