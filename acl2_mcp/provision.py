"""First-run provisioning for the ACL2 MCP server.

A pip install of this package is expected to be self-sufficient apart from an
ACL2 image and its Lisp implementation:

* the vendored parinfer-rust source is built into the package (``_lib``) by
  the build backend, and :func:`ensure_parinfer` can rebuild it for editable
  installs;
* the ACL2 Jupyter kernel sources are bundled (``_kernel``), and
  :func:`ensure_kernelspec` installs the kernelspec on first use with the
  ACL2 image named by ``ACL2`` (or ``ACL2_HOME/saved_acl2``).

The kernel itself still depends on Quicklisp being able to load
``common-lisp-jupyter``; an ACL2 development container provides that.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

PACKAGE_DIR = Path(__file__).resolve().parent
REPO_DIR = PACKAGE_DIR.parent
KERNEL_SUBMODULE = REPO_DIR / "external" / "acl2-jupyter-kernel"
RUST_SUBMODULE = REPO_DIR / "external" / "parinfer-rust"
KERNEL_ASD = "acl2-jupyter-kernel.asd"
LEGACY_PARINFER_LIB = Path("/usr/local/lib/libparinfer_rust.so")
KERNEL_BUILD_TIMEOUT = 300
RUST_BUILD_TIMEOUT = 600
_TRUTHY = ("1", "true", "yes", "on")


class ProvisionError(RuntimeError):
    """Provisioning could not be completed."""


# ---------------------------------------------------------------------------
# Parinfer library
# ---------------------------------------------------------------------------


def parinfer_library_name() -> str:
    """The shared-library filename produced by cargo for this platform."""
    system = platform.system()
    if system == "Darwin":
        return "libparinfer_rust.dylib"
    if system == "Windows":
        return "parinfer_rust.dll"
    return "libparinfer_rust.so"


def parinfer_candidates() -> list[Path]:
    """Places to look for the built parinfer shared library, best first."""
    name = parinfer_library_name()
    return [
        PACKAGE_DIR / "_lib" / name,
        RUST_SUBMODULE / "target" / "release" / name,
        LEGACY_PARINFER_LIB,
    ]


def find_parinfer_library() -> Optional[Path]:
    """Return the parinfer library to load, honoring ``PARINFER_LIB``."""
    explicit = os.environ.get("PARINFER_LIB")
    if explicit:
        path = Path(explicit).expanduser()
        return path if path.is_file() else None
    for candidate in parinfer_candidates():
        if candidate.is_file():
            return candidate
    return None


def parinfer_source_available() -> bool:
    return (RUST_SUBMODULE / "Cargo.toml").is_file()


def build_parinfer() -> Optional[Path]:
    """Build the vendored parinfer-rust library into the package ``_lib``.

    Returns None when the build is explicitly skipped or no Rust source is
    present.  Raises ProvisionError when a build is attempted and fails.
    """
    skip = os.environ.get("ACL2_MCP_SKIP_PARINFER_BUILD", "").lower()
    if skip in _TRUTHY:
        logger.info("parinfer build skipped (ACL2_MCP_SKIP_PARINFER_BUILD)")
        return None
    if not parinfer_source_available():
        logger.warning(
            "parinfer Rust source is not available; balancing will be disabled"
        )
        return None

    cargo = shutil.which("cargo")
    if cargo is None:
        raise ProvisionError(
            "cargo was not found, so the vendored parinfer-rust library cannot "
            "be built. Install a Rust toolchain, point PARINFER_LIB at an "
            "existing libparinfer_rust, or set ACL2_MCP_SKIP_PARINFER_BUILD=1 "
            "to install without parinfer balancing."
        )

    logger.info("building parinfer-rust in %s", RUST_SUBMODULE)
    completed = subprocess.run(
        [cargo, "build", "--release", "--lib", "--locked"],
        cwd=str(RUST_SUBMODULE),
        capture_output=True,
        text=True,
        timeout=RUST_BUILD_TIMEOUT,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise ProvisionError(
            "cargo build --release --lib failed for parinfer-rust:\n" + detail
        )

    built = RUST_SUBMODULE / "target" / "release" / parinfer_library_name()
    if not built.is_file():
        raise ProvisionError(f"cargo did not produce {built}")
    destination = PACKAGE_DIR / "_lib" / parinfer_library_name()
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(built, destination)
    logger.info("built parinfer library at %s", destination)
    return destination


def ensure_parinfer() -> Optional[Path]:
    """Make a parinfer library available if possible, without hard failure."""
    found = find_parinfer_library()
    if found is not None:
        return found
    try:
        return build_parinfer()
    except (OSError, subprocess.SubprocessError, ProvisionError) as exc:
        logger.warning("could not prepare parinfer: %s", exc)
        return None


# ---------------------------------------------------------------------------
# ACL2 Jupyter kernelspec
# ---------------------------------------------------------------------------


def find_kernel_dir() -> Optional[Path]:
    """Return the directory holding the bundled kernel sources."""
    explicit = os.environ.get("ACL2_MCP_KERNEL_DIR")
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    candidates.extend([PACKAGE_DIR / "_kernel", KERNEL_SUBMODULE])
    for candidate in candidates:
        if (candidate / KERNEL_ASD).is_file():
            return candidate
    return None


def _kernelspec_manager() -> Any:
    from jupyter_client.kernelspec import KernelSpecManager

    return KernelSpecManager()


def kernelspec_exists(name: str) -> bool:
    try:
        return name in _kernelspec_manager().find_kernel_specs()
    except Exception:
        return False


def _acl2_executable() -> Path:
    configured = os.environ.get("ACL2")
    if configured:
        path = Path(configured).expanduser()
    else:
        home = os.environ.get("ACL2_HOME")
        path = Path(home).expanduser() / "saved_acl2" if home else Path("saved_acl2")
    if not (path.is_file() and os.access(path, os.X_OK)):
        raise ProvisionError(
            f"Could not find an executable ACL2 launcher at {path}. Set ACL2 "
            "to the saved_acl2 script (for example /workspaces/acl2/saved_acl2)."
        )
    return path.resolve()


def _quicklisp_setup() -> Path:
    configured = os.environ.get("ACL2_MCP_QUICKLISP_SETUP")
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured).expanduser())
    else:
        home = Path.home()
        candidates.extend(
            [home / "quicklisp" / "setup.lisp", home / ".quicklisp" / "setup.lisp"]
        )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise ProvisionError(
        "Could not find quicklisp/setup.lisp, which the ACL2 Jupyter kernel "
        "needs to load common-lisp-jupyter. Set ACL2_MCP_QUICKLISP_SETUP to "
        "the setup.lisp in your ACL2 development environment."
    )


def _lisp_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _install_script(quicklisp: Path, kernel_dir: Path) -> str:
    kernel_path = str(kernel_dir.resolve())
    return "\n".join(
        [
            "(value :q)",
            "#+sbcl (sb-ext:disable-debugger)",
            "#+ccl (setf ccl:*break-on-warnings* nil)",
            f"(load {_lisp_string(str(quicklisp))})",
            "(asdf:clear-source-registry)",
            f"(push #p{_lisp_string(kernel_path + '/')} asdf:*central-registry*)",
            "(ql:quickload :acl2-jupyter-kernel :silent t)",
            "(acl2-jupyter-kernel:install)",
            "#+sbcl (sb-ext:exit :code 0)",
            "#+ccl (ccl:quit 0)",
            "#-(or sbcl ccl) (uiop:quit 0)",
            "",
        ]
    )


def _patch_kernel_spec(name: str, kernel_dir: Path) -> None:
    """Make the installed kernelspec load the bundled kernel sources."""
    spec = _kernelspec_manager().get_kernel_spec(name)
    spec_path = Path(spec.resource_dir) / "kernel.json"
    data: dict[str, Any] = json.loads(spec_path.read_text(encoding="utf-8"))
    argv = data.get("argv")
    push = (
        "(push #p"
        + _lisp_string(str(kernel_dir.resolve()) + "/")
        + " asdf:*central-registry*)"
    )
    if isinstance(argv, list) and push not in argv:
        index = next(
            (
                i
                for i, arg in enumerate(argv)
                if isinstance(arg, str) and arg.startswith("(ql:quickload")
            ),
            len(argv),
        )
        # Insert a whole --eval pair, before the --eval that owns the
        # quickload form (not between that --eval and its argument).
        if index > 0 and argv[index - 1] == "--eval":
            index -= 1
        argv[index:index] = ["--eval", push]
        data["argv"] = argv
        spec_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        logger.info("updated kernelspec %s to use %s", name, kernel_dir)


def _install_kernelspec(name: str) -> None:
    acl2 = _acl2_executable()
    quicklisp = _quicklisp_setup()
    kernel_dir = find_kernel_dir()
    if kernel_dir is None:
        raise ProvisionError(
            "The bundled ACL2 Jupyter kernel sources were not found. Reinstall "
            "acl2-mcp from a source checkout with submodules, or set "
            "ACL2_MCP_KERNEL_DIR to an acl2-jupyter-kernel source directory."
        )

    env = os.environ.copy()
    env["ACL2"] = str(acl2)
    env.setdefault("ACL2_HOME", str(acl2.parent))
    env["ACL2_JUPYTER_KERNEL_NAME"] = name
    env.setdefault(
        "ACL2_JUPYTER_DISPLAY_NAME", "ACL2 (SBCL)" if "sbcl" in name else "ACL2"
    )
    env.setdefault("ACL2_JUPYTER_EVENT_FORMS", "1")
    env.setdefault("ACL2_JUPYTER_FULL_WORLD", "0")
    env.setdefault("ACL2_JUPYTER_DEEP_EVENTS", "0")
    env.setdefault("ACL2_JUPYTER_EXWORLD", "0")

    logger.info("installing ACL2 Jupyter kernelspec %r from %s", name, kernel_dir)
    completed = subprocess.run(
        [str(acl2)],
        input=_install_script(quicklisp, kernel_dir),
        capture_output=True,
        text=True,
        env=env,
        timeout=KERNEL_BUILD_TIMEOUT,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise ProvisionError(
            f"ACL2 failed while installing the {name!r} kernelspec "
            f"(exit {completed.returncode}):\n{detail[-4000:]}"
        )
    if not kernelspec_exists(name):
        detail = (completed.stdout or completed.stderr or "").strip()
        raise ProvisionError(
            f"The {name!r} kernelspec was not installed. ACL2 output:\n"
            f"{detail[-4000:]}"
        )
    _patch_kernel_spec(name, kernel_dir)


_PROVISION_LOCK = threading.Lock()
_ENSURED_KERNELSPECS: set[str] = set()


def ensure_kernelspec(name: str) -> None:
    """Install the kernelspec for NAME on first use, if it is missing."""
    if name in _ENSURED_KERNELSPECS or kernelspec_exists(name):
        _ENSURED_KERNELSPECS.add(name)
        return
    with _PROVISION_LOCK:
        if name in _ENSURED_KERNELSPECS or kernelspec_exists(name):
            _ENSURED_KERNELSPECS.add(name)
            return
        _install_kernelspec(name)
        _ENSURED_KERNELSPECS.add(name)


# ---------------------------------------------------------------------------
# Console entry point
# ---------------------------------------------------------------------------


def _main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("traitlets").setLevel(logging.ERROR)
    status = 0

    library = find_parinfer_library()
    if library is None:
        try:
            library = build_parinfer()
        except (OSError, subprocess.SubprocessError, ProvisionError) as exc:
            print(f"parinfer: {exc}", file=sys.stderr)
            status = 1
    if library is not None:
        print(f"parinfer: {library}")

    from .kernel_backend import resolve_kernel_name

    name = resolve_kernel_name()
    try:
        ensure_kernelspec(name)
        print(f"kernelspec: {name} ready")
    except (OSError, subprocess.SubprocessError, ProvisionError) as exc:
        print(f"kernelspec: {exc}", file=sys.stderr)
        status = 1
    return status


def main() -> None:
    """Console entry point for ``acl2-mcp-install``."""
    raise SystemExit(_main())
