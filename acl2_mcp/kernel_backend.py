"""Jupyter-kernel transport for ACL2.

The ACL2 Jupyter kernel runs *inside* the ACL2 process and speaks the Jupyter
wire protocol.  Using it instead of scraping a piped ACL2 read-eval-print loop
removes prompt handling entirely: output and values arrive as Jupyter messages,
cell metadata reports the events a cell added, and code completeness is a
protocol request rather than a heuristic.

Kernel selection follows the same environment the ACL2 build uses:

* ``ACL2_MCP_KERNEL`` (or ``ACL2_MCP_KERNEL_NAME``) — explicit kernelspec name.
* ``ACL2`` — launcher script; its ``exec`` line selects CCL (``-I``) or SBCL
  (``--core``), which maps to the ``acl2`` / ``acl2-sbcl`` kernelspecs.
* ``LISP`` (or ``ACL2_LISP``) — used only when the script is ambiguous.

``ACL2_SYSTEM_BOOKS`` and ``ACL2_CUSTOMIZATION`` are passed through to the
kernel process.
"""

from __future__ import annotations

import os
import queue
import re
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

MIME_EVENTS = "application/vnd.acl2.events+json"

# ACL2 expressions used for bookkeeping.  Evaluations do not create command
# numbers, so probing this does not change history.
COMMAND_NUMBER_FORM = (
    "(acl2::absolute-to-relative-command-number"
    " (acl2::max-absolute-command-number (acl2::w acl2::state))"
    " (acl2::w acl2::state))"
)

DEFAULT_KERNEL_NAME = "acl2"
SBCL_KERNEL_NAME = "acl2-sbcl"
STARTUP_TIMEOUT = 120


class KernelError(RuntimeError):
    """Base class for kernel transport failures."""


class KernelConfigError(KernelError):
    """The selected kernelspec is missing or unusable."""


class KernelStartError(KernelError):
    """The kernel process could not be started."""


class KernelDied(KernelError):
    """The kernel process exited while a request was in flight."""


class KernelTimeout(KernelError):
    """A request exceeded its timeout; the kernel was shut down."""


@dataclass
class CellResult:
    """Everything a single Jupyter execute_request produced."""

    stdout: str = ""
    stderr: str = ""
    results: list[str] = field(default_factory=list)
    error: Optional[dict[str, Any]] = None
    events: list[str] = field(default_factory=list)
    forms: list[str] = field(default_factory=list)
    package: str = "ACL2"

    @property
    def text(self) -> str:
        """A flat transcript suitable for the MCP tools."""
        parts: list[str] = []
        if self.stdout:
            parts.append(self.stdout.rstrip("\n"))
        if self.stderr:
            parts.append(self.stderr.rstrip("\n"))
        for value in self.results:
            parts.append(value)
        if self.error:
            ename = self.error.get("ename", "Error")
            evalue = self.error.get("evalue", "")
            parts.append(f"Error: {ename}: {evalue}".rstrip(": "))
        return "\n".join(part for part in parts if part)


def _acl2_script_lisp(path: str) -> Optional[str]:
    """Inspect a saved_acl2 launcher and return 'ccl', 'sbcl', or None."""
    try:
        with open(path, encoding="utf-8", errors="replace") as stream:
            for line in stream:
                if line.startswith("exec "):
                    if "--core" in line:
                        return "sbcl"
                    if re.search(r"(?:^|\s)(?:-I|--image-name)(?:\s|=)", line):
                        return "ccl"
                    return None
    except OSError:
        return None
    return None


def _validate_acl2_environment() -> None:
    """Fail clearly when ACL2 names something that cannot be launched."""
    acl2 = os.environ.get("ACL2")
    if not acl2:
        return
    path = Path(acl2)
    if not (path.is_file() and os.access(path, os.X_OK)):
        raise KernelConfigError(
            f"Could not start ACL2 executable '{acl2}': file not found or not "
            "executable. Set the ACL2 environment variable to the ACL2 launcher "
            "(for example /workspaces/acl2/saved_acl2)."
        )


def resolve_kernel_name() -> str:
    """Choose the kernelspec name from ACL2_MCP_KERNEL, ACL2, or LISP."""
    explicit = os.environ.get("ACL2_MCP_KERNEL") or os.environ.get(
        "ACL2_MCP_KERNEL_NAME"
    )
    if explicit:
        return explicit

    acl2 = os.environ.get("ACL2", "")
    detected = _acl2_script_lisp(acl2) if acl2 else None
    if detected == "ccl":
        return DEFAULT_KERNEL_NAME
    if detected == "sbcl":
        return SBCL_KERNEL_NAME

    lisp = (os.environ.get("LISP") or os.environ.get("ACL2_LISP") or "").lower()
    if lisp.startswith("sbcl"):
        return SBCL_KERNEL_NAME
    if lisp.startswith("ccl"):
        return DEFAULT_KERNEL_NAME
    return DEFAULT_KERNEL_NAME


def kernel_environment(extra: Optional[dict[str, str]] = None) -> dict[str, str]:
    """Environment for a kernel process, preserving ACL2's configuration."""
    env = os.environ.copy()
    for name in ("ACL2", "ACL2_SYSTEM_BOOKS", "ACL2_CUSTOMIZATION", "LISP"):
        if name in os.environ:
            env[name] = os.environ[name]
    # ACL2 uses ACL2_CUSTOMIZATION=NONE to skip a user customization file.
    env.setdefault("ACL2_CUSTOMIZATION", "NONE")
    # common-lisp-jupyter writes kernel logs under XDG_RUNTIME_DIR.  When it is
    # unset, uiop can fall back to a relative path, littering the server's
    # working directory with common-lisp-jupyter/ logs.
    if not env.get("XDG_RUNTIME_DIR"):
        runtime_dir = os.path.join(
            tempfile.gettempdir(), f"acl2-mcp-runtime-{os.getuid()}"
        )
        try:
            os.makedirs(runtime_dir, mode=0o700, exist_ok=True)
        except OSError:
            pass
        env["XDG_RUNTIME_DIR"] = runtime_dir
    if extra:
        env.update(extra)
    return env


def _kernel_spec_env(kernel_name: str) -> dict[str, str]:
    """Return the env section of the installed kernelspec (empty if absent)."""
    try:
        from jupyter_client.kernelspec import KernelSpecManager
    except ImportError as exc:  # pragma: no cover - dependency error path
        raise KernelConfigError(
            "jupyter_client is required for the ACL2 kernel backend; "
            "install it with 'pip install jupyter_client pyzmq'"
        ) from exc
    try:
        spec = KernelSpecManager().get_kernel_spec(kernel_name)
    except Exception as exc:
        raise KernelConfigError(
            f"No Jupyter kernelspec named {kernel_name!r}. Install one with "
            "acl2-jupyter-kernel/install-kernelspec.sh using the desired "
            f"saved_acl2 (ACL2=...), or set ACL2_MCP_KERNEL. ({exc})"
        ) from exc
    return dict(spec.env or {})


class KernelClient:
    """A single ACL2 Jupyter kernel process."""

    def __init__(
        self,
        kernel_name: Optional[str] = None,
        cwd: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
        startup_timeout: int = STARTUP_TIMEOUT,
    ) -> None:
        _validate_acl2_environment()
        self.kernel_name = kernel_name or resolve_kernel_name()
        spec_env = _kernel_spec_env(self.kernel_name)
        merged_env = kernel_environment(spec_env)
        if env:
            merged_env.update(env)

        try:
            from jupyter_client.manager import KernelManager
        except ImportError as exc:  # pragma: no cover - dependency error path
            raise KernelConfigError(
                "jupyter_client is required for the ACL2 kernel backend; "
                "install it with 'pip install jupyter_client pyzmq'"
            ) from exc

        self._lock = threading.Lock()
        self._dead = False
        self.km = KernelManager(kernel_name=self.kernel_name)
        log_path = os.path.join(
            os.environ.get("ACL2_MCP_KERNEL_LOG_DIR", "/tmp"),
            f"acl2-mcp-kernel-{self.kernel_name}.log",
        )
        try:
            # A kernel launched with inherited stdout/stderr would write ACL2's
            # startup banner over the MCP server's stdio transport.  Send the
            # child's streams to a log file instead.
            with open(log_path, "ab", buffering=0) as kernel_log:
                self.km.start_kernel(
                    cwd=cwd,
                    env=merged_env,
                    stdout=kernel_log,
                    stderr=kernel_log,
                )
            self.kc = self.km.client()
            self.kc.start_channels()
            self.kc.wait_for_ready(timeout=startup_timeout)
        except Exception as exc:
            self._dead = True
            try:
                self.km.shutdown_kernel(now=True)
            except Exception:
                pass
            raise KernelStartError(
                f"Could not start ACL2 Jupyter kernel {self.kernel_name!r} "
                f"(ACL2={os.environ.get('ACL2', '<unset>')}, "
                f"LISP={os.environ.get('LISP', '<unset>')}): {exc}"
            ) from exc

    # -- liveness ---------------------------------------------------------

    def is_alive(self) -> bool:
        if self._dead:
            return False
        try:
            return bool(self.km.is_alive())
        except Exception:
            return False

    # -- requests ---------------------------------------------------------

    def execute(self, code: str, timeout: int = 30) -> CellResult:
        """Execute CODE as one cell and collect its complete output."""
        if not self.is_alive():
            self._dead = True
            raise KernelDied("ACL2 kernel process has terminated")

        with self._lock:
            if self._dead:
                raise KernelDied("ACL2 kernel process has terminated")
            try:
                msg_id = self.kc.execute(code)
            except Exception as exc:
                self._dead = True
                raise KernelDied(f"Could not send request to ACL2 kernel: {exc}") from exc

            result = CellResult()
            deadline = time.monotonic() + max(1, int(timeout))
            idle = False
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    message = self.kc.get_iopub_msg(timeout=min(0.5, remaining))
                except queue.Empty:
                    if not self.is_alive():
                        self._dead = True
                        raise KernelDied(
                            "ACL2 kernel process died while executing a request"
                        )
                    continue
                except Exception as exc:
                    self._dead = True
                    raise KernelDied(f"Lost connection to ACL2 kernel: {exc}") from exc

                if message.get("parent_header", {}).get("msg_id") != msg_id:
                    continue
                msg_type = message.get("msg_type")
                content = message.get("content", {})
                if msg_type == "stream":
                    if content.get("name") == "stderr":
                        result.stderr += content.get("text", "")
                    else:
                        result.stdout += content.get("text", "")
                elif msg_type == "execute_result":
                    data = content.get("data", {})
                    result.results.append(data.get("text/plain", ""))
                elif msg_type == "display_data":
                    data = content.get("data", {})
                    if MIME_EVENTS in data:
                        metadata = data[MIME_EVENTS] or {}
                        result.events = list(metadata.get("events", []))
                        result.forms = list(metadata.get("forms", []))
                        result.package = metadata.get("package", "ACL2")
                elif msg_type == "error":
                    result.error = content
                elif (
                    msg_type == "status"
                    and content.get("execution_state") == "idle"
                ):
                    idle = True
                    break

            # Drain the shell reply so it cannot leak into the next request.
            try:
                self.kc.get_shell_msg(timeout=1.0)
            except Exception:
                pass

            if not idle:
                self._shutdown_unlocked()
                raise KernelTimeout(
                    f"ACL2 execution timed out after {timeout} seconds. The kernel "
                    "was stopped to avoid a desynchronized reader; start a new "
                    "session to continue."
                )
            return result

    def is_complete(self, code: str, timeout: int = 10) -> str:
        """Ask the kernel whether CODE is 'complete', 'incomplete', or 'invalid'."""
        if not self.is_alive():
            self._dead = True
            raise KernelDied("ACL2 kernel process has terminated")

        with self._lock:
            try:
                msg_id = self.kc.is_complete(code)
            except Exception as exc:
                self._dead = True
                raise KernelDied(f"Could not send is_complete request: {exc}") from exc

            deadline = time.monotonic() + max(1, timeout)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise KernelTimeout("Timed out waiting for a completeness reply")
                try:
                    reply = self.kc.get_shell_msg(timeout=min(0.5, remaining))
                except queue.Empty:
                    if not self.is_alive():
                        self._dead = True
                        raise KernelDied("ACL2 kernel process died")
                    continue
                except Exception as exc:
                    self._dead = True
                    raise KernelDied(f"Lost connection to ACL2 kernel: {exc}") from exc
                if reply.get("msg_type") != "is_complete_reply":
                    continue
                parent = reply.get("parent_header", {}).get("msg_id")
                if parent and parent != msg_id:
                    continue
                return str(
                    reply.get("content", {}).get("status", "complete")
                )

    def command_number(self, timeout: int = 10) -> int:
        """Return ACL2's current relative command number."""
        result = self.execute(COMMAND_NUMBER_FORM, timeout)
        for value in reversed(result.results):
            match = re.search(r"-?\d+", value)
            if match:
                return int(match.group())
        raise KernelError(
            "Could not determine the current ACL2 command number"
            + (f": {result.text}" if result.text else "")
        )

    # -- shutdown ---------------------------------------------------------

    def _shutdown_unlocked(self) -> None:
        if self._dead:
            return
        self._dead = True
        try:
            self.kc.stop_channels()
        except Exception:
            pass
        try:
            self.km.shutdown_kernel(now=True)
        except Exception:
            pass

    def shutdown(self) -> None:
        with self._lock:
            self._shutdown_unlocked()


def execute_once(
    code: str,
    timeout: int = 30,
    cwd: Optional[str] = None,
    kernel_name: Optional[str] = None,
) -> CellResult:
    """Run CODE in a fresh kernel and always shut the kernel down."""
    client = KernelClient(kernel_name=kernel_name, cwd=cwd)
    try:
        return client.execute(code, timeout)
    finally:
        client.shutdown()


def check_syntax_once(code: str, timeout: int = 5) -> tuple[str, Optional[CellResult]]:
    """Check reader completeness, then run CODE only if it is complete.

    Returns (status, result) where status is 'complete', 'incomplete', or
    'invalid'.  The code is not executed for the latter two, so unbalanced
    input can never block a session.
    """
    client = KernelClient()
    try:
        status = client.is_complete(code)
        if status != "complete":
            return status, None
        return status, client.execute(code, timeout)
    finally:
        client.shutdown()

