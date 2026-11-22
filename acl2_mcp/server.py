"""ACL2 MCP Server implementation."""

import asyncio
import ctypes
import json
import logging
import os
import re
import signal
import threading
import time
import uuid
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path
from typing import Any, Sequence, Optional
from dataclasses import dataclass, field

from .kernel_backend import (
    CellResult,
    KernelClient,
    KernelConfigError,
    KernelDied,
    KernelStartError,
    KernelTimeout,
    check_syntax_once,
    execute_once,
    resolve_kernel_name,
)

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent, ListToolsResult, CallToolResult

# Set up file logging for debugging (ACL2_MCP_LOG_FILE overrides the path).
# The default includes the uid so a root-run server and a vscode-run server do
# not fight over one file; if the file cannot be opened, the fallback handler
# must still stay at INFO or DEBUG transcript noise floods the MCP client's
# stderr.
_uid = getattr(os, "getuid", lambda: "user")()
_log_file = os.environ.get(
    "ACL2_MCP_LOG_FILE", f"/tmp/acl2-mcp-debug-{_uid}.log"
)
try:
    _file_handler = logging.FileHandler(_log_file)
    _file_handler.setLevel(logging.DEBUG)
    _stream_handler = logging.StreamHandler()
    _stream_handler.setLevel(logging.INFO)
    _log_handlers: list[logging.Handler] = [_file_handler, _stream_handler]
except OSError:
    _fallback_handler = logging.StreamHandler()
    _fallback_handler.setLevel(logging.INFO)
    _log_handlers = [_fallback_handler]

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=_log_handlers,
)
# Jupyter clients are chatty at DEBUG and would flood stderr.
logging.getLogger("traitlets").setLevel(logging.WARNING)
logging.getLogger("jupyter_client").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

try:
    __version__ = package_version("acl2-mcp")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0.1.0"


# Security constants
MAX_TIMEOUT = 300  # 5 minutes maximum
MIN_TIMEOUT = 1
MAX_CODE_LENGTH = 1_000_000  # 1MB of code
SESSION_INACTIVITY_TIMEOUT = 1800  # 30 minutes
MAX_SESSIONS = 50  # Maximum concurrent sessions
MAX_CHECKPOINT_NAME_LENGTH = 100  # Maximum checkpoint name length
MAX_CHECKPOINTS_PER_SESSION = 50  # Maximum checkpoints per session
MAX_SESSION_NAME_LENGTH = 100  # Maximum session name length
MAX_EXPORT_LENGTH = 10_000_000  # Maximum accumulated source returned by an export
DEFAULT_PARINFER_LIB = "/usr/local/lib/libparinfer_rust.so"
PARINFER_MODES = ("indent", "paren", "smart")
DEFAULT_PARINFER_MODE = "indent"  # indent mode: derive parens from indentation
DEFAULT_CHECK_SYNTAX_TIMEOUT = 5
MAX_STDERR_LINES = 200
ACL2_PROMPT = "ACL2 !>"
# Set ACL2_MCP_RAW_OUTPUT=1 to keep the startup banner and prompts in results.
RAW_OUTPUT = os.environ.get("ACL2_MCP_RAW_OUTPUT", "").lower() not in (
    "",
    "0",
    "false",
    "no",
)
_ACL2_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9!$%&*+\-./:<=>?@^_~]+$")


def validate_timeout(timeout: int) -> int:
    """
    Validate and clamp timeout value.

    Args:
        timeout: Requested timeout in seconds

    Returns:
        Validated timeout value
    """
    if not isinstance(timeout, (int, float)):
        return 30
    return max(MIN_TIMEOUT, min(int(timeout), MAX_TIMEOUT))


def validate_acl2_identifier(identifier: str) -> str:
    """
    Validate that a string is a safe ACL2 identifier.

    Args:
        identifier: The identifier to validate

    Returns:
        The validated identifier

    Raises:
        ValueError: If identifier is not safe
    """
    if not identifier:
        raise ValueError("Identifier cannot be empty")

    # Accept only a plain ACL2 symbol.  Besides the obvious quote/paren cases,
    # this rejects whitespace, newlines, and ';' so a name cannot terminate the
    # surrounding form or inject an extra top-level command.
    if not _ACL2_IDENTIFIER_RE.match(identifier):
        raise ValueError(f"Invalid ACL2 identifier: {identifier}")

    return identifier


def escape_acl2_string(s: str) -> str:
    """
    Escape a string for safe use in ACL2 code.

    Args:
        s: String to escape

    Returns:
        Escaped string safe for use in ACL2
    """
    # Escape backslashes first, then quotes
    return s.replace("\\", "\\\\").replace('"', '\\"')


def validate_file_path(file_path: str) -> Path:
    """
    Validate file path and check it exists.

    Args:
        file_path: Path to validate

    Returns:
        Resolved absolute path

    Raises:
        ValueError: If path is invalid or doesn't exist
    """
    if not file_path:
        raise ValueError("File path cannot be empty")

    # Resolve to absolute path
    abs_path = Path(file_path).resolve()

    # Check that file exists
    if not abs_path.exists():
        raise ValueError(f"File not found: {abs_path.name}")

    # Check that it's a file (not a directory)
    if not abs_path.is_file():
        raise ValueError(f"Path is not a file: {abs_path.name}")

    return abs_path


def resolve_book_reference(file_path: str) -> tuple[str, bool]:
    """
    Resolve a book argument to what should be handed to ACL2's include-book.

    Returns:
        Tuple of (book_reference, is_local_file).  An existing .lisp or .cert
        file resolves to an absolute path (without extension).  A name that is
        not a local file is passed through unchanged so ACL2 can resolve it
        against its own book directories (and gets :dir :system).

    Raises:
        ValueError: If the path is empty or an absolute path that is not a file
    """
    raw = file_path[:-len(".lisp")] if file_path.lower().endswith(".lisp") else file_path
    if not raw.strip():
        raise ValueError("Book path cannot be empty")

    path = Path(raw).expanduser()
    for candidate in (Path(f"{raw}.lisp"), Path(f"{raw}.cert")):
        try:
            resolved = candidate.resolve()
        except (OSError, RuntimeError):
            continue
        if resolved.exists():
            if not resolved.is_file():
                raise ValueError(f"Path is not a file: {resolved.name}")
            return str(resolved.with_suffix("")), True

    if path.is_absolute():
        raise ValueError(f"File not found: {path.name}")

    # Not a local file: let ACL2 look in its system book directory.
    return raw, False


def validate_output_path(file_path: str, overwrite: bool = False) -> Path:
    """
    Validate a destination path for a file the server will write.

    Args:
        file_path: Path to validate
        overwrite: Whether an existing file may be replaced

    Returns:
        Resolved absolute path

    Raises:
        ValueError: If the path is invalid or the file exists without overwrite
    """
    if not file_path or not file_path.strip():
        raise ValueError("File path cannot be empty")

    abs_path = Path(file_path).expanduser().resolve()

    if abs_path.exists():
        if abs_path.is_dir():
            raise ValueError(f"Path is a directory: {abs_path.name}")
        if not overwrite:
            raise ValueError(
                f"File already exists: {abs_path.name} (set overwrite=true to replace it)"
            )

    if not abs_path.parent.is_dir():
        raise ValueError(f"Parent directory does not exist: {abs_path.parent}")

    return abs_path


def read_source_argument(arguments: Any) -> tuple[str, Optional[Path]]:
    """
    Get source text from either ``code`` or ``input_path``.

    Returns:
        Tuple of (source, resolved input path or None).  Exactly one of the
        two arguments must be supplied.

    Raises:
        ValueError: If the arguments are ambiguous, missing, or unreadable
    """
    code = arguments.get("code")
    input_path = arguments.get("input_path")

    if code is not None and input_path is not None:
        raise ValueError("Provide either code or input_path, not both")
    if code is None and input_path is None:
        raise ValueError("Provide either code or input_path")

    if input_path is not None:
        abs_path = validate_file_path(str(input_path))
        try:
            code = abs_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise ValueError(f"File is not valid UTF-8: {abs_path.name}")
        except OSError as e:
            raise ValueError(f"Could not read {abs_path.name}: {e}")
        if len(code) > MAX_CODE_LENGTH:
            raise ValueError(
                f"Source exceeds maximum length of {MAX_CODE_LENGTH} characters"
            )
        if not code.strip():
            raise ValueError(f"File is empty: {abs_path.name}")
        return code, abs_path

    if not isinstance(code, str):
        raise ValueError("code must be a string")
    if len(code) > MAX_CODE_LENGTH:
        raise ValueError(
            f"Code exceeds maximum length of {MAX_CODE_LENGTH} characters"
        )
    if not code.strip():
        raise ValueError("Source is empty")
    return code, None


def resolve_write_destination(
    arguments: Any, input_abs: Optional[Path]
) -> Optional[Path]:
    """
    Resolve where balanced source should be written, if anywhere.

    Accepts ``in_place`` (rewrite the input file), ``output_path`` (a
    different file), or ``file_path`` as a backward-compatible alias for
    ``output_path``.  Returns None when the caller just wants the source.

    Raises:
        ValueError: If the options conflict or the destination is not writable
    """
    in_place = bool(arguments.get("in_place", False))
    output_path = arguments.get("output_path")
    legacy_path = arguments.get("file_path")

    if (
        output_path is not None
        and legacy_path is not None
        and str(output_path) != str(legacy_path)
    ):
        raise ValueError("Provide only one of output_path and file_path")
    destination = output_path if output_path is not None else legacy_path

    if in_place:
        if input_abs is None:
            raise ValueError("in_place requires input_path")
        if destination is not None:
            raise ValueError(
                "in_place cannot be combined with output_path/file_path"
            )
        # Rewriting the input is explicit consent to replace it.
        return input_abs

    if destination is None:
        return None

    return validate_output_path(
        str(destination), overwrite=bool(arguments.get("overwrite", False))
    )


def write_balanced_source(destination: Path, source: str) -> str:
    """
    Write balanced source to destination, ensuring a trailing newline.

    Returns:
        A short confirmation message for the caller.

    Raises:
        ValueError: If the file cannot be written
    """
    text = source if source.endswith("\n") else source + "\n"
    try:
        destination.write_text(text, encoding="utf-8")
    except OSError as e:
        raise ValueError(f"Could not write {destination.name}: {e}")
    return (
        f"Wrote {len(text.splitlines())} lines "
        f"({len(text)} characters) to {destination}."
    )


# Parinfer is loaded in-process from the parinfer-rust cdylib; no CLI subprocess.
_parinfer_lib: Optional[ctypes.CDLL] = None
_parinfer_lib_error: Optional[str] = None
_parinfer_lib_lock = threading.Lock()


def _load_parinfer_lib() -> Optional[ctypes.CDLL]:
    """Load and cache the parinfer shared library, or return None."""
    global _parinfer_lib, _parinfer_lib_error

    with _parinfer_lib_lock:
        if _parinfer_lib is not None:
            return _parinfer_lib
        if _parinfer_lib_error is not None:
            return None

        lib_path = os.environ.get("PARINFER_LIB", DEFAULT_PARINFER_LIB)
        try:
            lib = ctypes.CDLL(lib_path)
            lib.run_parinfer.restype = ctypes.c_char_p
            lib.run_parinfer.argtypes = [ctypes.c_char_p]
        except OSError as e:
            _parinfer_lib_error = f"could not load parinfer library {lib_path}: {e}"
            logger.warning(_parinfer_lib_error)
            return None

        _parinfer_lib = lib
        logger.debug(f"Loaded parinfer library from {lib_path}")
        return lib


def parinfer_available() -> bool:
    """Whether the in-process parinfer library can be loaded."""
    return _load_parinfer_lib() is not None


def _parinfer_sync(code: str, mode: str) -> tuple[bool, str, Optional[str]]:
    """Run parinfer synchronously. Returns (success, text, error_message)."""
    lib = _load_parinfer_lib()
    if lib is None:
        return False, code, _parinfer_lib_error or "parinfer library unavailable"

    # JSON input bypasses parinfer's language-default table, so spell out the
    # Common Lisp/ACL2 features here. lineCommentQuotes=false is the CL fix
    # (upstream PR 157): ';' comments end at the newline, so a quote inside one
    # is prose, not an unterminated string.
    request = {
        "mode": mode,
        "text": code,
        "options": {
            "commentChar": ";",
            "stringDelimiters": ['"'],
            "lispVlineSymbols": True,
            "lispBlockComments": True,
            "lineCommentQuotes": False,
        },
    }

    try:
        raw = lib.run_parinfer(json.dumps(request).encode("utf-8"))
        answer = json.loads(raw)
    except Exception as e:
        logger.warning(f"parinfer call failed: {e}")
        return False, code, f"parinfer call failed: {e}"

    if answer.get("success"):
        return True, answer.get("text", code), None

    error = answer.get("error") or {}
    message = error.get("message") or "parinfer reported an error"
    line_no = error.get("lineNo")
    if isinstance(line_no, int):
        message = f"{message} (line {line_no + 1})"
    return False, code, message


async def run_parinfer(
    code: str, mode: str = DEFAULT_PARINFER_MODE
) -> tuple[bool, str, Optional[str]]:
    """Run parinfer off the event loop. Returns (success, text, error_message)."""
    return await asyncio.to_thread(_parinfer_sync, code, mode)


async def balance_if_enabled(
    code: str, enabled: bool, mode: str = DEFAULT_PARINFER_MODE
) -> tuple[str, Optional[str]]:
    """Balance code when enabled.

    Returns (code_to_send, error). If parinfer is disabled, unavailable, or the
    code is over the size limit, the original code is returned unchanged.
    """
    if not enabled or len(code) > MAX_CODE_LENGTH:
        return code, None
    if not parinfer_available():
        logger.warning("parinfer library unavailable; submitting code unchanged")
        return code, None
    success, balanced, error = await run_parinfer(code, mode)
    if not success:
        return code, error
    return balanced, None


_ACL2_FAILURE_MARKERS = (
    "ACL2 Error",
    "ACL2 Halted",
    "HARD ACL2 ERROR",
    "ABORTING from raw Lisp",
    "******** FAILED ********",
)


def acl2_output_failed(output: str) -> bool:
    """Whether ACL2 output indicates the submission was not accepted."""
    # Server-generated failures ("Error: ...") may follow a parinfer note, so
    # look for them at the start of any line, not just at the start of output.
    if any(line.startswith("Error: ") for line in output.splitlines()):
        return True
    return any(marker in output for marker in _ACL2_FAILURE_MARKERS)


def clean_acl2_output(output: str) -> str:
    """Remove the startup banner and interactive prompts from a transcript.

    The prompts and the version banner are noise for tool callers and make
    values hard to parse.  Set ACL2_MCP_RAW_OUTPUT=1 to keep the raw transcript
    for debugging.
    """
    if RAW_OUTPUT or not output:
        return output

    # Drop the startup banner through the first prompt, but only when this
    # really looks like a fresh-process transcript (not an error message).
    if "Welcome to" in output[:2000]:
        prompt_at = output.find(ACL2_PROMPT)
        if prompt_at != -1:
            output = output[prompt_at + len(ACL2_PROMPT) :]

    # Prompts appear at the start of a line (sometimes immediately before the
    # value printed by the next top-level form).  The kernel does not emit
    # prompts, but ld-based code can still produce this block.
    output = re.sub(r"(?m)^ACL2 !>+", "", output)
    output = re.sub(r"(?m)^ACL2 >+", "", output)
    # (ld "file") prints a startup-like directory block and an ld trailer.
    output = re.sub(
        r"(?ms)^Project-dir-alist:\n.*?^Type \(quit\) to quit completely "
        r"out of ACL2\.\n*",
        "",
        output,
    )
    output = re.sub(r"(?m)^Bye\.\s*$\n?", "", output)
    output = re.sub(r"(?mi)^\s*:EOF\s*$\n?", "", output)
    lines = output.splitlines()
    # Remove blank lines left behind by the final prompt, but preserve
    # meaningful internal blank lines.
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def parinfer_unavailable_note(enabled: bool) -> str:
    """A visible note when balancing was requested but cannot be performed."""
    if enabled and not parinfer_available():
        return (
            "[parinfer unavailable; code submitted unchanged. "
            "Set PARINFER_LIB to enable balancing.]\n\n"
        )
    return ""


def validate_checkpoint_name(name: str) -> str:
    """
    Validate checkpoint name for safety.

    Args:
        name: Checkpoint name to validate

    Returns:
        Validated checkpoint name

    Raises:
        ValueError: If name is invalid
    """
    if not name:
        raise ValueError("Checkpoint name cannot be empty")

    if len(name) > MAX_CHECKPOINT_NAME_LENGTH:
        raise ValueError(f"Checkpoint name exceeds maximum length of {MAX_CHECKPOINT_NAME_LENGTH}")

    # Only allow alphanumeric, hyphens, underscores
    if not re.match(r'^[a-zA-Z0-9_-]+$', name):
        raise ValueError("Checkpoint name can only contain letters, numbers, hyphens, and underscores")

    return name


def validate_session_name(name: str) -> str:
    """
    Validate session name for safety.

    Args:
        name: Session name to validate

    Returns:
        Validated session name

    Raises:
        ValueError: If name is invalid
    """
    if not name:
        return name

    if len(name) > MAX_SESSION_NAME_LENGTH:
        raise ValueError(f"Session name exceeds maximum length of {MAX_SESSION_NAME_LENGTH}")

    # Only allow alphanumeric, hyphens, underscores, spaces
    if not re.match(r'^[a-zA-Z0-9_\- ]+$', name):
        raise ValueError("Session name can only contain letters, numbers, hyphens, underscores, and spaces")

    return name


def validate_integer_parameter(value: int, min_value: int, max_value: int, name: str) -> int:
    """
    Validate integer parameter is within bounds.

    Args:
        value: Value to validate
        min_value: Minimum allowed value
        max_value: Maximum allowed value
        name: Parameter name for error messages

    Returns:
        Validated integer

    Raises:
        ValueError: If value is out of bounds
    """
    if not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")

    if value < min_value or value > max_value:
        raise ValueError(f"{name} must be between {min_value} and {max_value}")

    return value


@dataclass
class SessionCheckpoint:
    """Represents a saved checkpoint in an ACL2 session."""
    name: str
    event_number: int
    timestamp: float


@dataclass
class AcceptedForm:
    """Source accepted into a session, tagged with the last event it created."""
    code: str
    event_number: int


@dataclass
class ACL2Session:
    """A persistent ACL2 session backed by a Jupyter kernel process."""
    session_id: str
    name: Optional[str]
    kernel: KernelClient
    created_at: float
    last_activity: float
    checkpoints: dict[str, SessionCheckpoint] = field(default_factory=dict)
    event_counter: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    parinfer_enabled: bool = True
    parinfer_mode: str = DEFAULT_PARINFER_MODE
    accepted_code: list[AcceptedForm] = field(default_factory=list)
    accepted_code_size: int = 0
    alive: bool = True
    last_cell: Optional[CellResult] = None

    def is_alive(self) -> bool:
        """Whether the underlying ACL2 kernel can still accept commands."""
        return self.alive and self.kernel.is_alive()

    def prune_checkpoints_after(self, event_number: int, keep: Optional[str] = None) -> None:
        """Drop checkpoints that a backtrack has made unreachable."""
        for name in list(self.checkpoints):
            if name != keep and self.checkpoints[name].event_number > event_number:
                logger.info(
                    f"Session {self.session_id}: dropping unreachable checkpoint {name!r} "
                    f"(saved at event {self.checkpoints[name].event_number}, now at {event_number})"
                )
                del self.checkpoints[name]

    def prune_accepted_after(self, event_number: int) -> None:
        """Drop exported forms whose events are no longer in the world."""
        kept: list[AcceptedForm] = []
        for form in self.accepted_code:
            if form.event_number > event_number:
                self.accepted_code_size -= len(form.code)
            else:
                kept.append(form)
        self.accepted_code = kept

    async def send_command(self, command: str, timeout: int = 30) -> str:
        """Run COMMAND in the session kernel and return its output.

        The session's event counter is refreshed from the kernel after every
        command.  ACL2 evaluations do not create command numbers, so the probe
        itself does not disturb history.
        """
        async with self.lock:
            self.last_activity = time.time()

            if not self.is_alive():
                self.alive = False
                return "Error: Session process has terminated (connection lost)."

            if len(command) > MAX_CODE_LENGTH:
                return f"Error: Command exceeds maximum length of {MAX_CODE_LENGTH} characters"

            timeout = validate_timeout(timeout)
            before = self.event_counter

            try:
                result = await asyncio.to_thread(self.kernel.execute, command, timeout)
            except KernelTimeout as exc:
                self.alive = False
                return f"Error: {exc}"
            except (KernelDied, KernelConfigError, KernelStartError) as exc:
                self.alive = False
                return f"Error: {exc}"
            except Exception:
                logger.exception(
                    f"Session {self.session_id}: unexpected kernel failure"
                )
                self.alive = False
                return "Error: Failed to execute command in session"

            self.last_cell = result

            try:
                self.event_counter = await asyncio.to_thread(
                    self.kernel.command_number, 10
                )
            except Exception:
                # Fall back to the metadata diff if the probe cannot run.
                self.event_counter = before + len(result.events)

            if self.event_counter < before:
                self.prune_checkpoints_after(self.event_counter)
                self.prune_accepted_after(self.event_counter)

            text = clean_acl2_output(result.text)
            if result.error and not text.startswith("Error:"):
                ename = result.error.get("ename", "Error")
                evalue = result.error.get("evalue", "")
                text = f"Error: {ename}: {evalue}".rstrip(": ")
            return text

    def record_accepted(self, code: str, event_number: int) -> None:
        """Record source that created event(s), up to the export size cap."""
        if self.accepted_code_size + len(code) > MAX_EXPORT_LENGTH:
            logger.warning(
                f"Session {self.session_id}: accepted source cap "
                f"({MAX_EXPORT_LENGTH} characters) reached; not recording more"
            )
            return
        self.accepted_code.append(AcceptedForm(code=code, event_number=event_number))
        self.accepted_code_size += len(code)

    async def terminate(self) -> None:
        """Shut down the session's kernel process."""
        self.alive = False
        try:
            await asyncio.to_thread(self.kernel.shutdown)
        except Exception:
            logger.debug(
                f"Session {self.session_id}: kernel shutdown failed", exc_info=True
            )


def resolve_parinfer(arguments: Any, session: Optional[ACL2Session]) -> tuple[bool, str]:
    """Resolve the per-call parinfer override against the session setting."""
    override = arguments.get("parinfer")
    if isinstance(override, bool):
        enabled = override
    elif session is not None:
        enabled = session.parinfer_enabled
    else:
        enabled = True
    mode = session.parinfer_mode if session is not None else DEFAULT_PARINFER_MODE
    return enabled, mode


class SessionManager:
    """Manages persistent ACL2 sessions."""

    def __init__(self) -> None:
        self.sessions: dict[str, ACL2Session] = {}
        self._cleanup_task: Optional[asyncio.Task[None]] = None

    async def start_session(
        self,
        name: Optional[str] = None,
        parinfer_enabled: bool = True,
        parinfer_mode: str = DEFAULT_PARINFER_MODE,
    ) -> tuple[str, str]:
        """
        Start a new persistent ACL2 session.

        Args:
            name: Optional human-readable name for the session
            parinfer_enabled: Whether to balance submitted code with parinfer
            parinfer_mode: Parinfer mode (indent, paren, or smart)

        Returns:
            Tuple of (session_id, message)
        """
        if len(self.sessions) >= MAX_SESSIONS:
            return "", f"Error: Maximum number of sessions ({MAX_SESSIONS}) reached"

        # SECURITY: Validate session name
        if name:
            try:
                name = validate_session_name(name)
            except ValueError as e:
                return "", f"Error: Invalid session name - {e}"

        if parinfer_mode not in PARINFER_MODES:
            return "", f"Error: parinfer_mode must be one of {', '.join(PARINFER_MODES)}"

        session_id = str(uuid.uuid4())

        try:
            logger.debug(
                f"Starting ACL2 kernel session with kernel={resolve_kernel_name()}, "
                f"ACL2={os.environ.get('ACL2')}, "
                f"LISP={os.environ.get('LISP')}, "
                f"ACL2_SYSTEM_BOOKS={os.environ.get('ACL2_SYSTEM_BOOKS')}"
            )

            kernel = await asyncio.to_thread(KernelClient)

            session = ACL2Session(
                session_id=session_id,
                name=name,
                kernel=kernel,
                created_at=time.time(),
                last_activity=time.time(),
                parinfer_enabled=parinfer_enabled,
                parinfer_mode=parinfer_mode,
            )

            self.sessions[session_id] = session
            logger.info(f"Session {session_id} created (kernel {kernel.kernel_name})")

            # Start cleanup task if not already running
            if self._cleanup_task is None:
                self._cleanup_task = asyncio.create_task(self._cleanup_inactive_sessions())

            return session_id, (
                "Session started successfully. Use the ID below as the "
                f"session_id argument in later calls.\nID: {session_id}"
            )

        except (KernelConfigError, KernelStartError) as e:
            logger.error(f"Failed to start session: {e}")
            return "", f"Error: {e}"
        except Exception as e:
            # SECURITY: Don't leak internal error details
            logger.error(f"Failed to start session: {e}")
            return "", "Error: Failed to start session"

    async def end_session(self, session_id: str) -> str:
        """
        End a persistent ACL2 session.

        Args:
            session_id: ID of the session to end

        Returns:
            Status message
        """
        session = self.sessions.get(session_id)
        if not session:
            return f"Error: Session {session_id} not found"

        await session.terminate()
        del self.sessions[session_id]

        return f"Session {session_id} ended successfully"

    def list_sessions(self) -> str:
        """
        List all active sessions.

        Returns:
            Formatted list of sessions
        """
        if not self.sessions:
            return "No active sessions"

        lines = ["Active sessions:"]
        for session_id, session in self.sessions.items():
            age = time.time() - session.created_at
            idle = time.time() - session.last_activity
            name_str = f" ({session.name})" if session.name else ""
            status = (
                ""
                if session.is_alive()
                else " [terminated] (call end_session to free the slot)"
            )
            lines.append(
                f"  {session_id}{name_str}: "
                f"age={age:.0f}s, idle={idle:.0f}s, events={session.event_counter}, "
                f"parinfer={'on' if session.parinfer_enabled else 'off'}{status}"
            )

        return "\n".join(lines)

    def get_session(self, session_id: str) -> Optional[ACL2Session]:
        """Get a session by ID.

        Dead sessions are returned (and flagged in list_sessions) so callers
        get a precise "terminated" error; the cleanup task reaps them.
        """
        return self.sessions.get(session_id)

    async def cleanup_all(self) -> None:
        """Clean up all sessions."""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

        # Terminate all sessions concurrently to avoid blocking
        session_list = list(self.sessions.values())
        if session_list:
            await asyncio.gather(
                *[session.terminate() for session in session_list],
                return_exceptions=True  # Don't let one failure stop others
            )
        self.sessions.clear()

    async def _cleanup_inactive_sessions(self) -> None:
        """Background task to clean up inactive sessions."""
        while True:
            try:
                await asyncio.sleep(60)  # Check every minute

                now = time.time()
                to_remove = []

                # SECURITY: Create snapshot to avoid race conditions
                sessions_snapshot = list(self.sessions.items())

                for session_id, session in sessions_snapshot:
                    if (not session.is_alive()
                            or now - session.last_activity > SESSION_INACTIVITY_TIMEOUT):
                        to_remove.append((session_id, session))

                # SECURITY: Check if session still exists before removing
                for session_id, session in to_remove:
                    if session_id in self.sessions:
                        try:
                            await session.terminate()
                            del self.sessions[session_id]
                        except Exception:
                            # Log failure but continue cleanup
                            pass

            except asyncio.CancelledError:
                break
            except Exception:
                # Continue cleanup loop even if there's an error
                pass


# Global session manager
session_manager = SessionManager()


async def list_tools() -> list[Tool]:
    """List available ACL2 tools."""
    return [
        Tool(
            name="start_session",
            description="Start a new persistent ACL2 session. This creates a long-running ACL2 process that maintains state across multiple tool calls. Use this when you want to incrementally build up definitions and theorems without having to wrap everything in progn. Sessions auto-timeout after 30 minutes of inactivity.",
            input_schema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Optional human-readable name for the session. Example: 'natural-numbers-proof'",
                    },
                    "parinfer": {
                        "type": "boolean",
                        "description": "Balance submitted code with parinfer before sending (default: true)",
                        "default": True,
                    },
                    "parinfer_mode": {
                        "type": "string",
                        "enum": list(PARINFER_MODES),
                        "description": "Parinfer mode for this session (default: indent)",
                        "default": DEFAULT_PARINFER_MODE,
                    },
                },
            },
        ),
        Tool(
            name="end_session",
            description="End a persistent ACL2 session and clean up resources. Use this when you're done with incremental development. Sessions also auto-cleanup after 30 minutes of inactivity.",
            input_schema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "ID of the session to end",
                    },
                },
                "required": ["session_id"],
            },
        ),
        Tool(
            name="list_sessions",
            description="List all active ACL2 sessions with their IDs, names, age, idle time, and event count. Use this to see which sessions are available and their current state.",
            input_schema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="prove",
            description="Submit an ACL2 theorem (defthm) for proof. Use this to prove mathematical properties. Example: (defthm append-nil (implies (true-listp x) (equal (append x nil) x))). Input is either inline code or an input_path file. The theorem will be proven and added to the ACL2 world. Returns detailed ACL2 proof output. Can optionally use a persistent session for incremental development.",
            input_schema={
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "ACL2 code to prove (e.g., defthm form). Provide this or input_path (not both).",
                    },
                    "input_path": {
                        "type": "string",
                        "description": "Read the code to prove from this file instead of passing code in the call",
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Timeout in seconds (default: 30)",
                        "default": 30,
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Optional: ID of persistent session to use. If not provided, creates a fresh ACL2 session for this command only.",
                    },
                    "parinfer": {
                        "type": "boolean",
                        "description": "Override the session's parinfer setting for this call (default: session setting; true without a session)",
                    },
                },
            },
        ),
        Tool(
            name="evaluate",
            description="Evaluate ACL2 expressions or define functions (defun). Use this for: 1) Defining functions, 2) Computing values, 3) Testing expressions. Example: (defun factorial (n) (if (zp n) 1 (* n (factorial (- n 1))))) or (+ 1 2). Input is either inline code or an input_path file. Returns the ACL2 evaluation result. Can optionally use a persistent session for incremental development.",
            input_schema={
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "ACL2 code to evaluate. Provide this or input_path (not both).",
                    },
                    "input_path": {
                        "type": "string",
                        "description": "Read the code to evaluate from this file instead of passing code in the call",
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Timeout in seconds (default: 30)",
                        "default": 30,
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Optional: ID of persistent session to use. If not provided, creates a fresh ACL2 session for this command only.",
                    },
                    "parinfer": {
                        "type": "boolean",
                        "description": "Override the session's parinfer setting for this call (default: session setting; true without a session)",
                    },
                },
            },
        ),
        Tool(
            name="check_syntax",
            description="Check ACL2 code for reader/syntax errors. Input is either inline code or an input_path file. By default the code is first balanced with parinfer (indent mode), so mismatched parens may be silently repaired; pass parinfer=false to validate the literal source. The kernel then reports reader completeness directly: incomplete or invalid text is a syntax error and is not executed, while readable code is run for a bounded time and admission failures are reported separately. On success, return the balanced source, write it to a different file (output_path/file_path), or rewrite the input file in place (in_place). Use 'admit' or 'prove' to establish that events are accepted.",
            input_schema={
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "ACL2 code to check. Provide this or input_path (not both).",
                    },
                    "input_path": {
                        "type": "string",
                        "description": "Read the code from this file instead of passing code in the call",
                    },
                    "parinfer": {
                        "type": "boolean",
                        "description": "Balance the code with parinfer before checking (default: true)",
                        "default": True,
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Bounded ACL2 run time in seconds (default: 5, max: 300)",
                        "default": DEFAULT_CHECK_SYNTAX_TIMEOUT,
                    },
                    "return_source": {
                        "type": "boolean",
                        "description": "On success, return the balanced source instead of the usual diagnostics (default: false). Cannot be combined with writing to a file.",
                        "default": False,
                    },
                    "output_path": {
                        "type": "string",
                        "description": "On success, write the balanced source to this path (a different file) and return a confirmation",
                    },
                    "file_path": {
                        "type": "string",
                        "description": "Alias for output_path, kept for backward compatibility",
                    },
                    "in_place": {
                        "type": "boolean",
                        "description": "On success, rewrite input_path with the balanced source (requires input_path; cannot be combined with output_path/file_path)",
                        "default": False,
                    },
                    "overwrite": {
                        "type": "boolean",
                        "description": "Allow replacing an existing output_path/file_path (default: false). in_place always replaces the input.",
                        "default": False,
                    },
                },
            },
        ),
        Tool(
            name="certify_book",
            description="Certify an ACL2 book (a collection of definitions and theorems in a .lisp file). This verifies all proofs and creates a certificate for the book. Use this after creating a complete ACL2 book file. Provide the path WITHOUT the .lisp extension (a trailing .lisp is also accepted). An adjacent <book>.acl2 or cert.acl2 file is loaded first when present, and a '; cert-flags:' comment in it is honored.",
            input_schema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path to the book, normally WITHOUT .lisp extension. Example: '/path/to/mybook' for file '/path/to/mybook.lisp'",
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Timeout in seconds (default: 120)",
                        "default": 120,
                    },
                },
                "required": ["file_path"],
            },
        ),
        Tool(
            name="include_book",
            description="Load a certified ACL2 book to use its definitions and theorems. Use this to import existing ACL2 libraries before proving new theorems. Optionally run additional code after loading. A local book path (with or without .lisp) is used directly; any other name is resolved against ACL2's system book directory. IMPORTANT: Provide path WITHOUT .lisp extension.",
            input_schema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path to the book WITHOUT .lisp extension. Example: 'std/lists/append' for ACL2 standard library",
                    },
                    "code": {
                        "type": "string",
                        "description": "Optional ACL2 code to run after loading the book. Example: (thm (equal (+ 1 1) 2))",
                    },
                    "parinfer": {
                        "type": "boolean",
                        "description": "Balance the optional code with parinfer before running it (default: true)",
                        "default": True,
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Timeout in seconds (default: 60)",
                        "default": 60,
                    },
                },
                "required": ["file_path"],
            },
        ),
        Tool(
            name="check_theorem",
            description="Verify a specific theorem from a file. Use this to re-check a single theorem after making changes, without re-proving everything in the file. The file is loaded first, then the named theorem is proven. File path INCLUDES .lisp extension.",
            input_schema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Full path to the .lisp file (WITH extension). Example: '/path/to/theorems.lisp'",
                    },
                    "theorem_name": {
                        "type": "string",
                        "description": "Exact name of the theorem to check. Example: 'append-associative'",
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Timeout in seconds (default: 60)",
                        "default": 60,
                    },
                },
                "required": ["file_path", "theorem_name"],
            },
        ),
        Tool(
            name="admit",
            description="Test if an ACL2 event would be accepted without keeping it. In a persistent session the event is checked in the session's context and then rolled back with :ubt, so it does not change the world and is not exported. Without a session, a fresh ACL2 kernel is used and discarded. Input is either an inline event or an input_path file. Returns success/failure. Use 'prove' or 'evaluate' when you want the event to persist.",
            input_schema={
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "Single ACL2 event to test. Example: (defun my-func (x) (+ x 1)) or (defthm my-thm (equal (+ 1 1) 2)). Provide this or input_path (not both).",
                    },
                    "input_path": {
                        "type": "string",
                        "description": "Read the event to test from this file instead of passing code in the call",
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Timeout in seconds (default: 30)",
                        "default": 30,
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Optional: ID of persistent session to use. If not provided, creates a fresh ACL2 session for this command only.",
                    },
                    "parinfer": {
                        "type": "boolean",
                        "description": "Override the session's parinfer setting for this call (default: session setting; true without a session)",
                    },
                },
            },
        ),
        Tool(
            name="query_event",
            description="Look up the definition and properties of an ACL2 function, theorem, or macro. Use this to understand what's already defined before writing new code, or to check the signature of existing functions. Works with built-in ACL2 functions (e.g., 'append', 'len') or user-defined ones. Uses ACL2's :pe (print-event) command. Pass session_id to query a persistent session's world; otherwise a fresh kernel is used, optionally after loading file_path.",
            input_schema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Name of function/theorem to query. Examples: 'append', 'len', 'my-custom-function'",
                    },
                    "file_path": {
                        "type": "string",
                        "description": "Optional: Load this file first (WITH .lisp extension) before querying. Use if the event is defined in a specific file. Cannot be combined with session_id.",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Optional: ID of a persistent session. Query the session's current world instead of starting a fresh kernel. Cannot be combined with file_path.",
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Timeout in seconds (default: 30)",
                        "default": 30,
                    },
                },
                "required": ["name"],
            },
        ),
        Tool(
            name="verify_guards",
            description="Verify that a function's guards are satisfied, enabling efficient execution in raw Common Lisp. Guards are conditions that ensure a function is called with valid inputs. Use this after defining a function to enable faster execution. Pass session_id to verify a function defined in a persistent session; otherwise a fresh kernel is used, optionally after loading file_path. Common workflow: 1) Define function with 'evaluate', 2) Verify guards with this tool. Example: After defining (defun my-div (x y) (/ x y)), verify guards to ensure y is never zero.",
            input_schema={
                "type": "object",
                "properties": {
                    "function_name": {
                        "type": "string",
                        "description": "Name of the function to verify. Example: 'my-div'",
                    },
                    "file_path": {
                        "type": "string",
                        "description": "Optional: File containing the function (WITH .lisp extension). Load this before verifying. Cannot be combined with session_id.",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Optional: ID of a persistent session. Verify a function in the session's current world instead of starting a fresh kernel. Cannot be combined with file_path.",
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Timeout in seconds (default: 60)",
                        "default": 60,
                    },
                },
                "required": ["function_name"],
            },
        ),
        Tool(
            name="undo",
            description="Undo the last ACL2 event in a persistent session. This removes the most recent definition, theorem, or command from the session's world. Use this to backtrack and try alternative approaches. Uses ACL2's :ubt (undo-back-through) command. Only works with persistent sessions.",
            input_schema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "ID of the session to undo in",
                    },
                    "count": {
                        "type": "number",
                        "description": "Number of events to undo (default: 1)",
                        "default": 1,
                    },
                },
                "required": ["session_id"],
            },
        ),
        Tool(
            name="save_checkpoint",
            description="Save a named checkpoint of the current ACL2 world state in a session. You can later restore to this checkpoint to try alternative proof strategies. Use this before attempting risky proof steps or when you want to preserve a known-good state.",
            input_schema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "ID of the session",
                    },
                    "checkpoint_name": {
                        "type": "string",
                        "description": "Name for this checkpoint. Example: 'before-induction-proof'",
                    },
                },
                "required": ["session_id", "checkpoint_name"],
            },
        ),
        Tool(
            name="restore_checkpoint",
            description="Restore a session to a previously saved checkpoint. This undoes all events that occurred after the checkpoint was created. Use this to backtrack to a known-good state and try a different approach.",
            input_schema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "ID of the session",
                    },
                    "checkpoint_name": {
                        "type": "string",
                        "description": "Name of the checkpoint to restore",
                    },
                },
                "required": ["session_id", "checkpoint_name"],
            },
        ),
        Tool(
            name="get_world_state",
            description="Display the current ACL2 world state in a session, showing all definitions, theorems, and events. Use this to see what's currently defined in your session. Reads ACL2's command history for the requested window.",
            input_schema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "ID of the session",
                    },
                    "limit": {
                        "type": "number",
                        "description": "Maximum number of recent events to show (default: 20)",
                        "default": 20,
                    },
                },
                "required": ["session_id"],
            },
        ),
        Tool(
            name="retry_proof",
            description="Submit another proof attempt in a session, typically with different hints or strategies after a failed proof. Input is either inline code or an input_path file. This tool submits the code as-is; it does not undo a previous attempt or verify that one failed. It balances with parinfer by default, like the other code tools. Successful submissions that add events are included in export_session_code.",
            input_schema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "ID of the session to submit the proof attempt to",
                    },
                    "code": {
                        "type": "string",
                        "description": "New proof attempt with different hints. Example: (defthm my-thm (equal x y) :hints ((\"Goal\" :use (:instance lemma)))). Provide this or input_path (not both).",
                    },
                    "input_path": {
                        "type": "string",
                        "description": "Read the new proof attempt from this file instead of passing code in the call",
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Timeout in seconds (default: 60)",
                        "default": 60,
                    },
                    "parinfer": {
                        "type": "boolean",
                        "description": "Override the session's parinfer setting for this call (default: session setting; true without a session)",
                    },
                },
                "required": ["session_id"],
            },
        ),
        Tool(
            name="parinfer",
            description="Balance Lisp/ACL2 parentheses and indentation with parinfer. Input is either inline code or an input_path file. Indent mode is the default: parentheses are derived from indentation, which suits code written in bulk (LLMs are usually good at indentation but miss parens). Use 'paren' mode to validate existing parens and fix indentation instead. Returns the balanced source, or writes it to a different file (output_path/file_path) or back to the input file (in_place).",
            input_schema={
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "Lisp/ACL2 source to balance. Provide this or input_path (not both).",
                    },
                    "input_path": {
                        "type": "string",
                        "description": "Read the source from this file instead of passing code in the call",
                    },
                    "mode": {
                        "type": "string",
                        "enum": list(PARINFER_MODES),
                        "description": "Parinfer mode (default: indent)",
                        "default": DEFAULT_PARINFER_MODE,
                    },
                    "language": {
                        "type": "string",
                        "description": "Language dialect. Only 'lisp' is supported, which matches ACL2 (vline symbols and block comments enabled; quotes in ';' comments ignored).",
                        "default": "lisp",
                    },
                    "output_path": {
                        "type": "string",
                        "description": "On success, write the balanced source to this path (a different file) and return a confirmation",
                    },
                    "file_path": {
                        "type": "string",
                        "description": "Alias for output_path, kept for backward compatibility",
                    },
                    "in_place": {
                        "type": "boolean",
                        "description": "On success, rewrite input_path with the balanced source (requires input_path; cannot be combined with output_path/file_path)",
                        "default": False,
                    },
                    "overwrite": {
                        "type": "boolean",
                        "description": "Allow replacing an existing output_path/file_path (default: false). in_place always replaces the input.",
                        "default": False,
                    },
                },
            },
        ),
        Tool(
            name="export_session_code",
            description="Export the source of events accepted into a persistent session that are still in its current world. This is a server-tracked submission log, not a full world dump: non-event evaluations are skipped, forms removed by undo/checkpoint restore are pruned, and events added outside these tools (for example by an included book) are not recovered. A submission that added several events is tracked as one unit, so undoing part of it drops the whole recorded form. Without file_path, returns the source text. With file_path, writes the source to that file and returns only a confirmation with the path, line count, and character count.",
            input_schema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "ID of the session to export",
                    },
                    "file_path": {
                        "type": "string",
                        "description": "Optional: write the accepted source to this path instead of returning it",
                    },
                    "overwrite": {
                        "type": "boolean",
                        "description": "Allow replacing an existing file when file_path is given (default: false)",
                        "default": False,
                    },
                },
                "required": ["session_id"],
            },
        ),
    ]


async def run_acl2(code: str, timeout: int = 30, cwd: str | None = None) -> str:
    """Run ACL2 code in a fresh Jupyter kernel and return its output.

    Each call starts a kernel, runs one cell, and shuts the kernel down, which
    preserves the old "fresh ACL2 world" semantics for the file-based tools.
    Persistent sessions use a long-lived KernelClient instead.
    """
    if len(code) > MAX_CODE_LENGTH:
        return f"Error: Code exceeds maximum length of {MAX_CODE_LENGTH} characters"

    timeout = validate_timeout(timeout)

    try:
        result = await asyncio.to_thread(execute_once, code, timeout, cwd)
    except KernelTimeout as exc:
        return f"Error: {exc}"
    except (KernelConfigError, KernelStartError, KernelDied) as exc:
        return f"Error: {exc}"
    except Exception:
        logger.exception("Unexpected failure running ACL2 kernel")
        return "Error: Failed to execute ACL2 code"

    return clean_acl2_output(result.text)


async def run_acl2_file(file_path: str, timeout: int = 60) -> str:
    """
    Run ACL2 with a file using ld (load).

    Args:
        file_path: Path to the ACL2 file
        timeout: Timeout in seconds

    Returns:
        Output from ACL2
    """
    try:
        abs_path = validate_file_path(file_path)
    except ValueError as e:
        return f"Error: {e}"

    # Escape the path for safe use in ACL2 code
    escaped_path = escape_acl2_string(str(abs_path))

    # Use ld to load the file
    code = f'(ld "{escaped_path}")'
    return await run_acl2(code, timeout)


async def certify_acl2_book(file_path: str, timeout: int = 120) -> str:
    """
    Certify an ACL2 book.

    Args:
        file_path: Path to the book (without .lisp extension)
        timeout: Timeout in seconds

    Returns:
        Output from ACL2
    """
    # Remove .lisp extension if present
    book_path = str(Path(file_path).with_suffix(""))
    lisp_path = Path(book_path).with_suffix(".lisp")

    try:
        abs_path = validate_file_path(str(lisp_path))
    except ValueError as e:
        return f"Error: {e}"

    # Get the directory and check for .acl2 files
    book_dir = str(Path(abs_path).parent)
    book_name = Path(abs_path).stem
    
    # Check for .acl2 files (cert.acl2 or bookname.acl2)
    book_acl2 = Path(book_dir) / f"{book_name}.acl2"
    cert_acl2 = Path(book_dir) / "cert.acl2"
    acl2_file = book_acl2 if book_acl2.exists() else (cert_acl2 if cert_acl2.exists() else None)
    
    # Escape the book path for safe use in ACL2 code
    escaped_book_path = escape_acl2_string(book_path)
    
    # Parse cert-flags from .acl2 file if present
    cert_flags = "? t :ttags :all"  # Default: compile, allow all ttags
    if acl2_file:
        try:
            with open(acl2_file, 'r') as f:
                content = f.read()
                # Look for cert-flags comment: ; cert-flags: <flags>
                import re
                match = re.search(r';\s*cert-flags:\s*(.+?)$', content, re.MULTILINE)
                if match:
                    cert_flags = match.group(1).strip()
                    logger.info(f"CERTIFY_BOOK: Found cert-flags: {cert_flags}")
        except Exception as e:
            logger.warning(f"CERTIFY_BOOK: Could not read cert-flags from {acl2_file}: {e}")
    
    # Build code: if .acl2 file exists, load it first (from book directory) then certify
    if acl2_file:
        acl2_filename = acl2_file.name
        escaped_acl2_filename = escape_acl2_string(acl2_filename)
        logger.info(f"CERTIFY_BOOK: Loading .acl2 file: {acl2_filename} from directory: {book_dir}")
        code = f'(ld "{escaped_acl2_filename}") (certify-book "{escaped_book_path}" {cert_flags})'
    else:
        logger.info(f"CERTIFY_BOOK: No .acl2 file found for {book_name}")
        code = f'(certify-book "{escaped_book_path}" {cert_flags})'
    
    logger.info(f"CERTIFY_BOOK: code={code}, cwd={book_dir}")
    # Run from book's directory so .acl2 file's relative paths work
    return await run_acl2(code, timeout, cwd=book_dir)


async def include_acl2_book(file_path: str, additional_code: str = "", timeout: int = 60) -> str:
    """
    Include an ACL2 book and optionally run additional code.

    Args:
        file_path: Path to the book (without .lisp extension)
        additional_code: Optional code to run after including
        timeout: Timeout in seconds

    Returns:
        Output from ACL2
    """
    # Resolve the book reference: a local file becomes an absolute path;
    # anything else is left for ACL2 to find among its system books.
    try:
        book_ref, is_local = resolve_book_reference(file_path)
    except ValueError as e:
        return f"Error: {e}"

    # Escape the book path for safe use in ACL2 code
    escaped_book_path = escape_acl2_string(book_ref)

    # Build code to include book and run additional commands
    if is_local:
        code = f'(include-book "{escaped_book_path}")'
    else:
        code = f'(include-book "{escaped_book_path}" :dir :system)'
    if additional_code.strip():
        # Validate additional code length
        if len(additional_code) > MAX_CODE_LENGTH:
            return f"Error: Additional code exceeds maximum length of {MAX_CODE_LENGTH} characters"
        code += f"\n{additional_code}"

    return await run_acl2(code, timeout)


async def query_acl2_event(name: str, file_path: str = "", timeout: int = 30) -> str:
    """
    Query information about an ACL2 event (function, theorem, etc.).

    Args:
        name: Name of the event to query
        file_path: Optional file to load first
        timeout: Timeout in seconds

    Returns:
        Output from ACL2 showing the event definition and properties
    """
    # Validate the event name
    try:
        validated_name = validate_acl2_identifier(name)
    except ValueError as e:
        return f"Error: {e}"

    # Build code to load file (if provided) and query the event
    code = ""
    cwd = None
    if file_path:
        try:
            abs_path = validate_file_path(file_path)
        except ValueError as e:
            return f"Error: {e}"

        escaped_path = escape_acl2_string(str(abs_path))
        code += f'(ld "{escaped_path}")\n'
        cwd = str(Path(abs_path).parent)

    # Use :pe (print event) to show the definition
    code += f":pe {validated_name}"

    return await run_acl2(code, timeout, cwd=cwd)


async def verify_function_guards(function_name: str, file_path: str = "", timeout: int = 60) -> str:
    """
    Verify guards for a function.

    Args:
        function_name: Name of the function
        file_path: Optional file containing the function
        timeout: Timeout in seconds

    Returns:
        Output from ACL2
    """
    # Validate the function name
    try:
        validated_name = validate_acl2_identifier(function_name)
    except ValueError as e:
        return f"Error: {e}"

    # Build code to load file (if provided) and verify guards
    code = ""
    cwd = None
    if file_path:
        try:
            abs_path = validate_file_path(file_path)
        except ValueError as e:
            return f"Error: {e}"

        escaped_path = escape_acl2_string(str(abs_path))
        code += f'(ld "{escaped_path}")\n'
        cwd = str(Path(abs_path).parent)

    # Use verify-guards command
    code += f"(verify-guards {validated_name})"

    return await run_acl2(code, timeout, cwd=cwd)


async def handle_list_tools(_ctx: Any, _params: Any) -> ListToolsResult:
    """MCP 2.x tools/list handler: wrap the tool list in a result object."""
    return ListToolsResult(tools=await list_tools())


async def call_tool(name: str, arguments: Any) -> Sequence[TextContent]:
    """Handle tool calls."""
    if name == "start_session":
        session_name = arguments.get("name")
        parinfer_enabled = arguments.get("parinfer", True)
        parinfer_mode = arguments.get("parinfer_mode", DEFAULT_PARINFER_MODE)
        session_id, message = await session_manager.start_session(
            session_name,
            parinfer_enabled=bool(parinfer_enabled),
            parinfer_mode=parinfer_mode,
        )
        if session_id and bool(parinfer_enabled) and not parinfer_available():
            message += (
                "\nWarning: parinfer library unavailable; submissions will not be "
                "balanced. Set PARINFER_LIB to the parinfer-rust shared library."
            )
        return [
            TextContent(
                type="text",
                text=message,
            )
        ]

    elif name == "end_session":
        session_id = arguments["session_id"]
        message = await session_manager.end_session(session_id)
        return [
            TextContent(
                type="text",
                text=message,
            )
        ]

    elif name == "list_sessions":
        message = session_manager.list_sessions()
        return [
            TextContent(
                type="text",
                text=message,
            )
        ]

    elif name == "undo":
        session_id = arguments["session_id"]
        count = arguments.get("count", 1)

        # SECURITY: Validate count parameter
        try:
            count = validate_integer_parameter(count, 1, 10000, "count")
        except ValueError as e:
            return [
                TextContent(
                    type="text",
                    text=f"Error: {e}",
                )
            ]

        session = session_manager.get_session(session_id)
        if not session:
            return [
                TextContent(
                    type="text",
                    text=f"Error: Session {session_id} not found",
                )
            ]

        # :ubt n undoes the commands from the most recent one back through n.
        # To remove `count` events ending at the current event E we must target
        # E - count + 1 (event 0 is system initialization and cannot be undone).
        current = session.event_counter
        if current <= 0:
            return [
                TextContent(
                    type="text",
                    text="Error: There are no events to undo in this session.",
                )
            ]
        if count > current:
            return [
                TextContent(
                    type="text",
                    text=(
                        f"Error: Cannot undo {count} event(s); the session has only "
                        f"{current} event(s)."
                    ),
                )
            ]

        target = current - count + 1
        output = await session.send_command(f":ubt {target}")

        if acl2_output_failed(output):
            return [
                TextContent(
                    type="text",
                    text=f"Error: Undo failed; the session is still at event "
                    f"{session.event_counter}.\n\n{output}",
                )
            ]

        session.prune_checkpoints_after(session.event_counter)

        return [
            TextContent(
                type="text",
                text=(
                    f"Undone {count} event(s); the session is now at event "
                    f"{session.event_counter}.\n\n{output}"
                ),
            )
        ]

    elif name == "save_checkpoint":
        session_id = arguments["session_id"]
        checkpoint_name = arguments["checkpoint_name"]

        # SECURITY: Validate checkpoint name
        try:
            checkpoint_name = validate_checkpoint_name(checkpoint_name)
        except ValueError as e:
            return [
                TextContent(
                    type="text",
                    text=f"Error: {e}",
                )
            ]

        session = session_manager.get_session(session_id)
        if not session:
            return [
                TextContent(
                    type="text",
                    text=f"Error: Session {session_id} not found",
                )
            ]

        # SECURITY: Limit number of checkpoints per session
        if len(session.checkpoints) >= MAX_CHECKPOINTS_PER_SESSION:
            return [
                TextContent(
                    type="text",
                    text=f"Error: Maximum number of checkpoints ({MAX_CHECKPOINTS_PER_SESSION}) reached for this session",
                )
            ]

        new_checkpoint = SessionCheckpoint(
            name=checkpoint_name,
            event_number=session.event_counter,
            timestamp=time.time(),
        )
        session.checkpoints[checkpoint_name] = new_checkpoint

        return [
            TextContent(
                type="text",
                text=f"Checkpoint '{checkpoint_name}' saved at event {session.event_counter}",
            )
        ]

    elif name == "restore_checkpoint":
        session_id = arguments["session_id"]
        checkpoint_name = arguments["checkpoint_name"]

        # SECURITY: Validate checkpoint name
        try:
            checkpoint_name = validate_checkpoint_name(checkpoint_name)
        except ValueError as e:
            return [
                TextContent(
                    type="text",
                    text=f"Error: {e}",
                )
            ]

        session = session_manager.get_session(session_id)
        if not session:
            return [
                TextContent(
                    type="text",
                    text=f"Error: Session {session_id} not found",
                )
            ]

        checkpoint: Optional[SessionCheckpoint] = session.checkpoints.get(checkpoint_name)
        if checkpoint is None:
            available = ", ".join(session.checkpoints.keys()) if session.checkpoints else "none"
            return [
                TextContent(
                    type="text",
                    text=f"Error: Checkpoint '{checkpoint_name}' not found. Available: {available}",
                )
            ]

        # :ubt n removes command n itself, so keep the checkpoint's event by
        # undoing back through the command *after* it.  A checkpoint above the
        # current event is unreachable (something was undone past it).
        current = session.event_counter
        if checkpoint.event_number > current:
            return [
                TextContent(
                    type="text",
                    text=(
                        f"Error: Checkpoint '{checkpoint_name}' was saved at event "
                        f"{checkpoint.event_number}, but the session is now at event "
                        f"{current}; restore to an earlier state first."
                    ),
                )
            ]

        if checkpoint.event_number == current:
            return [
                TextContent(
                    type="text",
                    text=(
                        f"Session is already at checkpoint '{checkpoint_name}' "
                        f"(event {checkpoint.event_number})."
                    ),
                )
            ]

        output = await session.send_command(f":ubt {checkpoint.event_number + 1}")

        if acl2_output_failed(output):
            return [
                TextContent(
                    type="text",
                    text=(
                        f"Error: Restore to checkpoint '{checkpoint_name}' failed; the "
                        f"session is still at event {session.event_counter}.\n\n{output}"
                    ),
                )
            ]

        session.prune_checkpoints_after(session.event_counter, keep=checkpoint_name)

        return [
            TextContent(
                type="text",
                text=(
                    f"Restored to checkpoint '{checkpoint_name}' (event "
                    f"{session.event_counter}):\n\n{output}"
                ),
            )
        ]

    elif name == "get_world_state":
        session_id = arguments["session_id"]
        limit = arguments.get("limit", 20)

        # SECURITY: Validate limit parameter to prevent DoS
        try:
            limit = validate_integer_parameter(limit, 1, 1000, "limit")
        except ValueError as e:
            return [
                TextContent(
                    type="text",
                    text=f"Error: {e}",
                )
            ]

        session = session_manager.get_session(session_id)
        if not session:
            return [
                TextContent(
                    type="text",
                    text=f"Error: Session {session_id} not found",
                )
            ]

        # Show the recent command forms.  get-command-sequence returns the
        # source of commands start..end (relative command numbers), which is
        # more reliable than :pbt through the kernel's trans-eval path.
        end_event = session.event_counter
        if end_event <= 0:
            return [
                TextContent(
                    type="text",
                    text="World state: no commands yet (boot-strap world only).",
                )
            ]
        start_event = max(1, end_event - limit + 1)
        output = await session.send_command(
            f"(acl2::get-command-sequence {start_event} {end_event})"
        )

        return [
            TextContent(
                type="text",
                text=(
                    f"World state (last {end_event - start_event + 1} event(s), "
                    f"commands {start_event}-{end_event}):\n\n{output}"
                ),
            )
        ]

    elif name == "retry_proof":
        session_id = arguments["session_id"]
        timeout = arguments.get("timeout", 60)

        try:
            code, _ = read_source_argument(arguments)
        except ValueError as e:
            return [
                TextContent(
                    type="text",
                    text=f"Error: {e}",
                )
            ]

        session = session_manager.get_session(session_id)
        if not session:
            return [
                TextContent(
                    type="text",
                    text=f"Error: Session {session_id} not found",
                )
            ]

        use_parinfer, parinfer_mode = resolve_parinfer(arguments, session)
        balanced, parinfer_error = await balance_if_enabled(
            code, use_parinfer, parinfer_mode
        )
        if parinfer_error:
            return [
                TextContent(
                    type="text",
                    text=f"Error: parinfer could not balance the code: {parinfer_error}\n"
                    "(Use parinfer=false to submit it unchanged.)",
                )
            ]

        before = session.event_counter
        output = await session.send_command(balanced, timeout)
        if not acl2_output_failed(output) and session.event_counter > before:
            session.record_accepted(balanced, session.event_counter)

        note = parinfer_unavailable_note(use_parinfer)
        if balanced != code:
            note += "[parinfer adjusted the code]\n\n"
        return [
            TextContent(
                type="text",
                text=f"Retry proof result:\n\n{note}{output}",
            )
        ]

    elif name == "prove":
        timeout = arguments.get("timeout", 30)
        session_id = arguments.get("session_id")

        try:
            code, _ = read_source_argument(arguments)
        except ValueError as e:
            return [
                TextContent(
                    type="text",
                    text=f"Error: {e}",
                )
            ]

        session = None
        if session_id:
            session = session_manager.get_session(session_id)
            if not session:
                return [
                    TextContent(
                        type="text",
                        text=f"Error: Session {session_id} not found",
                    )
                ]

        use_parinfer, parinfer_mode = resolve_parinfer(arguments, session)
        balanced, parinfer_error = await balance_if_enabled(code, use_parinfer, parinfer_mode)
        if parinfer_error:
            return [
                TextContent(
                    type="text",
                    text=f"Error: parinfer could not balance the code: {parinfer_error}\n"
                    "(Use parinfer=false to submit it unchanged.)",
                )
            ]

        if session is not None:
            before = session.event_counter
            output = await session.send_command(balanced, timeout)
            if not acl2_output_failed(output) and session.event_counter > before:
                session.record_accepted(balanced, session.event_counter)
        else:
            output = await run_acl2(balanced, timeout)

        note = parinfer_unavailable_note(use_parinfer)
        if balanced != code:
            note += "[parinfer adjusted the code]\n\n"
        return [
            TextContent(
                type="text",
                text=note + output,
            )
        ]

    elif name == "evaluate":
        timeout = arguments.get("timeout", 30)
        session_id = arguments.get("session_id")

        try:
            code, _ = read_source_argument(arguments)
        except ValueError as e:
            return [
                TextContent(
                    type="text",
                    text=f"Error: {e}",
                )
            ]

        session = None
        if session_id:
            session = session_manager.get_session(session_id)
            if not session:
                return [
                    TextContent(
                        type="text",
                        text=f"Error: Session {session_id} not found",
                    )
                ]

        use_parinfer, parinfer_mode = resolve_parinfer(arguments, session)
        balanced, parinfer_error = await balance_if_enabled(code, use_parinfer, parinfer_mode)
        if parinfer_error:
            return [
                TextContent(
                    type="text",
                    text=f"Error: parinfer could not balance the code: {parinfer_error}\n"
                    "(Use parinfer=false to submit it unchanged.)",
                )
            ]

        if session is not None:
            before = session.event_counter
            output = await session.send_command(balanced, timeout)
            if not acl2_output_failed(output) and session.event_counter > before:
                session.record_accepted(balanced, session.event_counter)
        else:
            output = await run_acl2(balanced, timeout)

        note = parinfer_unavailable_note(use_parinfer)
        if balanced != code:
            note += "[parinfer adjusted the code]\n\n"
        return [
            TextContent(
                type="text",
                text=note + output,
            )
        ]

    elif name == "check_syntax":
        use_parinfer = arguments.get("parinfer", True)
        return_source = bool(arguments.get("return_source", False))
        timeout = validate_timeout(
            arguments.get("timeout", DEFAULT_CHECK_SYNTAX_TIMEOUT)
        )

        # Input is either inline code or a file; output is optional.
        try:
            code, input_abs = read_source_argument(arguments)
            destination = resolve_write_destination(arguments, input_abs)
        except ValueError as e:
            return [
                TextContent(
                    type="text",
                    text=f"Error: {e}",
                )
            ]

        if return_source and destination is not None:
            return [
                TextContent(
                    type="text",
                    text="Error: return_source cannot be combined with writing to a file; choose one.",
                )
            ]

        # Balance first (indent mode): LLM-written code tends to have good
        # indentation but missing/extra parens. The syntax check then runs on
        # the balanced source, which is what gets returned or written.
        balanced, parinfer_error = await balance_if_enabled(
            code, bool(use_parinfer), DEFAULT_PARINFER_MODE
        )
        if parinfer_error:
            return [
                TextContent(
                    type="text",
                    text=f"Error: parinfer could not balance the code: {parinfer_error}\n"
                    "(Use parinfer=false to check it unchanged.)",
                )
            ]

        # The kernel answers reader completeness directly, so unbalanced input
        # is rejected without executing anything (and without hanging).
        try:
            syntax_status, cell = await asyncio.to_thread(
                check_syntax_once, balanced, timeout
            )
        except KernelTimeout:
            return [
                TextContent(
                    type="text",
                    text=(
                        f"Syntax check timed out after {timeout} seconds; no conclusion "
                        "was reached and no file was written. Increase the timeout or "
                        "check a smaller fragment."
                    ),
                )
            ]
        except (KernelConfigError, KernelStartError, KernelDied) as e:
            return [
                TextContent(
                    type="text",
                    text=f"Syntax check could not run: {e}",
                )
            ]
        except Exception:
            logger.exception("Unexpected failure during syntax check")
            return [
                TextContent(
                    type="text",
                    text="Syntax check could not run: unexpected kernel failure",
                )
            ]

        source_note = f" in {input_abs}" if input_abs is not None else ""
        if syntax_status == "incomplete":
            return [
                TextContent(
                    type="text",
                    text=(
                        f"Syntax errors found{source_note}: the form is incomplete "
                        "(unbalanced parentheses or unexpected end of file)."
                    ),
                )
            ]
        if syntax_status == "invalid":
            return [
                TextContent(
                    type="text",
                    text=(
                        f"Syntax errors found{source_note}: the text is not readable "
                        "as ACL2 (unmatched close parenthesis or invalid token)."
                    ),
                )
            ]

        output = clean_acl2_output(cell.text) if cell is not None else ""

        notes = parinfer_unavailable_note(bool(use_parinfer))
        if balanced != code:
            notes += "[parinfer adjusted the code]\n\n"

        execution_failed = acl2_output_failed(output)
        if execution_failed:
            status = (
                f"No syntax errors detected{source_note}, but ACL2 reported errors "
                "while executing the balanced source. Review the output before "
                "relying on the code; use admit/prove for acceptance.\n\n"
            )
        elif not output.strip():
            status = (
                f"Syntax check was inconclusive{source_note}: ACL2 produced no "
                "output.\n\n"
            )
        else:
            status = f"No syntax errors detected{source_note}.\n\n"

        if destination is not None:
            try:
                confirmation = write_balanced_source(destination, balanced)
            except ValueError as e:
                return [
                    TextContent(
                        type="text",
                        text=f"Error: {e}",
                    )
                ]
            return [
                TextContent(
                    type="text",
                    text=confirmation + notes + (status if execution_failed else ""),
                )
            ]

        if return_source:
            return [
                TextContent(
                    type="text",
                    text=balanced,
                )
            ]

        return [
            TextContent(
                type="text",
                text=notes + status + output,
            )
        ]

    elif name == "certify_book":
        file_path = arguments["file_path"]
        timeout = arguments.get("timeout", 120)

        output = await certify_acl2_book(file_path, timeout)

        return [
            TextContent(
                type="text",
                text=output,
            )
        ]

    elif name == "include_book":
        file_path = arguments["file_path"]
        additional_code = arguments.get("code", "")
        timeout = arguments.get("timeout", 60)
        use_parinfer = bool(arguments.get("parinfer", True))

        balanced, parinfer_error = await balance_if_enabled(
            additional_code, use_parinfer, DEFAULT_PARINFER_MODE
        )
        if parinfer_error:
            return [
                TextContent(
                    type="text",
                    text=f"Error: parinfer could not balance the code: {parinfer_error}\n"
                    "(Use parinfer=false to submit it unchanged.)",
                )
            ]

        output = await include_acl2_book(file_path, balanced, timeout)

        note = parinfer_unavailable_note(
            use_parinfer and bool(additional_code.strip())
        )
        if balanced != additional_code:
            note += "[parinfer adjusted the code]\n\n"
        return [
            TextContent(
                type="text",
                text=note + output,
            )
        ]

    elif name == "check_theorem":
        file_path = arguments["file_path"]
        theorem_name = arguments["theorem_name"]
        timeout = arguments.get("timeout", 60)

        # Validate inputs
        try:
            abs_path = validate_file_path(file_path)
            validated_theorem = validate_acl2_identifier(theorem_name)
            # The name is used as a bare ACL2 symbol (in 'name and as a
            # command descriptor), so reject anything needing |...| quoting.
            if not re.match(r"^[A-Za-z0-9!$%&*+\-./:<=>?@^_~]+$", validated_theorem):
                raise ValueError(
                    f"Theorem name must be a plain ACL2 symbol: {theorem_name}"
                )
        except ValueError as e:
            return [
                TextContent(
                    type="text",
                    text=f"Error: {e}",
                )
            ]

        # Escape the path and build code
        escaped_path = escape_acl2_string(str(abs_path))

        # Get the directory containing the file (for .lisp/.acl2 lookup)
        file_dir = str(Path(abs_path).parent)

        # Load the file, stash the named event's form in a state global (these
        # survive :ubt), undo back through that event, then re-submit the form.
        # Undoing the event first means the re-proof cannot lean on the very
        # theorem being checked; re-reading the stored form preserves hints.
        check_var = "acl2::acl2-mcp-check-event"
        no_event_marker = "___ACL2_MCP_NO_SUCH_EVENT___"
        undo_failed_marker = "___ACL2_MCP_UNDO_FAILED___"
        reproof_marker = "___ACL2_MCP_REPROOF_BEGIN___"
        code = (
            f'(ld "{escaped_path}")\n'
            f"(acl2::assign {check_var} "
            f"(acl2::get-event '{validated_theorem} (acl2::w acl2::state)))\n"
            f":ubt {validated_theorem}\n"
            f'(acl2::cw "{reproof_marker}~%")\n'
            f"(acl2::if (acl2::@ {check_var})\n"
            f"    (acl2::if (acl2::get-event '{validated_theorem} (acl2::w acl2::state))\n"
            f'        (acl2::cw "{undo_failed_marker}~%")\n'
            f"      (acl2::ld (acl2::list (acl2::@ {check_var}))))\n"
            f'    (acl2::cw "{no_event_marker}~%"))'
        )
        output = await run_acl2(code, timeout, cwd=file_dir)
        cleaned = clean_acl2_output(output)
        # (ld "file") leaves a bare package result at the front of the transcript.
        cleaned = re.sub(r'^\s*"ACL2"\s*\n', "", cleaned)

        # Split at the internal marker before removing it; the prefix is the
        # file load and the suffix is the actual re-proof.
        if reproof_marker in cleaned:
            load_output, reproof_output = cleaned.split(reproof_marker, 1)
        else:
            # A timeout or launch failure never reached the marker.
            load_output, reproof_output = "", cleaned
        cleaned = re.sub(
            r"(?m)^" + re.escape(reproof_marker) + r"\s*$\n?", "", cleaned
        ).strip()

        if no_event_marker in output:
            load_output = cleaned.replace(no_event_marker, "")
            # The generated code still issues :ubt for a name that does not
            # exist; drop that expected error (and its trailing NILs) so the
            # real diagnosis is not buried.
            load_output = re.sub(
                r"(?ms)^ACL2 Error in :UBT:.*\Z", "", load_output
            ).strip()
            return [
                TextContent(
                    type="text",
                    text=(
                        f"Error: No event named '{validated_theorem}' was introduced "
                        f"by {abs_path}. The name may be misspelled, or an earlier "
                        "form in the file may have failed to admit. Load output:\n\n"
                        f"{load_output}"
                    ),
                )
            ]

        if undo_failed_marker in output:
            undo_detail = cleaned.replace(undo_failed_marker, "").strip()
            return [
                TextContent(
                    type="text",
                    text=(
                        f"Error: Could not undo the event '{validated_theorem}' "
                        f"(it may come from an included book rather than {abs_path.name}).\n\n"
                        f"{undo_detail}"
                    ),
                )
            ]

        load_failed = bool(load_output.strip()) and acl2_output_failed(load_output)
        if acl2_output_failed(reproof_output):
            status = f"Theorem '{validated_theorem}' FAILED to re-prove."
        elif load_failed:
            status = (
                f"Theorem '{validated_theorem}' re-proved successfully, but "
                "loading the file reported errors."
            )
        else:
            status = f"Theorem '{validated_theorem}' re-proved successfully."

        # On a clean load, return just the re-proof; otherwise keep the load
        # transcript so its errors are visible.
        body = cleaned if load_failed else reproof_output.strip()
        return [
            TextContent(
                type="text",
                text=f"{status}\n\n{body}",
            )
        ]

    elif name == "admit":
        timeout = arguments.get("timeout", 30)
        session_id = arguments.get("session_id")

        try:
            code, _ = read_source_argument(arguments)
        except ValueError as e:
            return [
                TextContent(
                    type="text",
                    text=f"Error: {e}",
                )
            ]

        session = None
        if session_id:
            session = session_manager.get_session(session_id)
            if not session:
                return [
                    TextContent(
                        type="text",
                        text=f"Error: Session {session_id} not found",
                    )
                ]

        use_parinfer, parinfer_mode = resolve_parinfer(arguments, session)
        balanced, parinfer_error = await balance_if_enabled(code, use_parinfer, parinfer_mode)
        if parinfer_error:
            return [
                TextContent(
                    type="text",
                    text=f"Error: parinfer could not balance the code: {parinfer_error}\n"
                    "(Use parinfer=false to submit it unchanged.)",
                )
            ]

        rolled_back = False
        if session is not None:
            before = session.event_counter
            output = await session.send_command(balanced, timeout)
            success = not acl2_output_failed(output)

            # admit is a test: undo any events the submission managed to add,
            # whether it succeeded or failed part-way through.
            if session.is_alive() and session.event_counter > before:
                undo_output = await session.send_command(f":ubt {before + 1}", timeout)
                if acl2_output_failed(undo_output):
                    return [
                        TextContent(
                            type="text",
                            text=(
                                "Error: Admit ran but the test event(s) could not be "
                                f"rolled back:\n\n{undo_output}"
                            ),
                        )
                    ]
                rolled_back = True
        else:
            output = await run_acl2(balanced, timeout)
            success = not acl2_output_failed(output)

        note = parinfer_unavailable_note(use_parinfer)
        if balanced != code:
            note += "[parinfer adjusted the code]\n\n"
        rollback_note = " (test event(s) rolled back)" if rolled_back else ""
        return [
            TextContent(
                type="text",
                text=(
                    f"{note}Admit {'succeeded' if success else 'failed'}"
                    f"{rollback_note}:\n\n{output}"
                ),
            )
        ]

    elif name == "query_event":
        name_arg = arguments["name"]
        file_path = arguments.get("file_path", "")
        session_id = arguments.get("session_id")
        timeout = arguments.get("timeout", 30)

        if session_id:
            if file_path:
                return [
                    TextContent(
                        type="text",
                        text="Error: session_id and file_path cannot be combined.",
                    )
                ]
            session = session_manager.get_session(session_id)
            if not session:
                return [
                    TextContent(
                        type="text",
                        text=f"Error: Session {session_id} not found",
                    )
                ]
            try:
                validated_name = validate_acl2_identifier(name_arg)
            except ValueError as e:
                return [
                    TextContent(
                        type="text",
                        text=f"Error: {e}",
                    )
                ]
            output = await session.send_command(f":pe {validated_name}", timeout)
        else:
            output = await query_acl2_event(name_arg, file_path, timeout)

        return [
            TextContent(
                type="text",
                text=output,
            )
        ]

    elif name == "verify_guards":
        function_name = arguments["function_name"]
        file_path = arguments.get("file_path", "")
        session_id = arguments.get("session_id")
        timeout = arguments.get("timeout", 60)

        if session_id:
            if file_path:
                return [
                    TextContent(
                        type="text",
                        text="Error: session_id and file_path cannot be combined.",
                    )
                ]
            session = session_manager.get_session(session_id)
            if not session:
                return [
                    TextContent(
                        type="text",
                        text=f"Error: Session {session_id} not found",
                    )
                ]
            try:
                validated_name = validate_acl2_identifier(function_name)
            except ValueError as e:
                return [
                    TextContent(
                        type="text",
                        text=f"Error: {e}",
                    )
                ]
            command = f"(verify-guards {validated_name})"
            before = session.event_counter
            output = await session.send_command(command, timeout)
            if not acl2_output_failed(output) and session.event_counter > before:
                session.record_accepted(command, session.event_counter)
        else:
            output = await verify_function_guards(function_name, file_path, timeout)

        return [
            TextContent(
                type="text",
                text=output,
            )
        ]

    elif name == "parinfer":
        mode = arguments.get("mode", DEFAULT_PARINFER_MODE)
        language = arguments.get("language", "lisp")

        if mode not in PARINFER_MODES:
            return [
                TextContent(
                    type="text",
                    text=f"Error: mode must be one of {', '.join(PARINFER_MODES)}",
                )
            ]

        if language != "lisp":
            return [
                TextContent(
                    type="text",
                    text="Error: only language 'lisp' is supported (ACL2 is Common Lisp)",
                )
            ]

        try:
            code, input_abs = read_source_argument(arguments)
            destination = resolve_write_destination(arguments, input_abs)
        except ValueError as e:
            return [
                TextContent(
                    type="text",
                    text=f"Error: {e}",
                )
            ]

        if not parinfer_available():
            return [
                TextContent(
                    type="text",
                    text=f"Error: {_parinfer_lib_error or 'parinfer library unavailable'}\n"
                    "Set PARINFER_LIB to the parinfer-rust shared library path.",
                )
            ]

        success, balanced, error = await run_parinfer(code, mode)
        if not success:
            return [
                TextContent(
                    type="text",
                    text=f"Error: parinfer failed: {error}",
                )
            ]

        if destination is not None:
            try:
                confirmation = write_balanced_source(destination, balanced)
            except ValueError as e:
                return [
                    TextContent(
                        type="text",
                        text=f"Error: {e}",
                    )
                ]
            return [
                TextContent(
                    type="text",
                    text=confirmation,
                )
            ]

        return [
            TextContent(
                type="text",
                text=balanced,
            )
        ]

    elif name == "export_session_code":
        session_id = arguments["session_id"]
        file_path = arguments.get("file_path")
        overwrite = bool(arguments.get("overwrite", False))

        session = session_manager.get_session(session_id)
        if not session:
            return [
                TextContent(
                    type="text",
                    text=f"Error: Session {session_id} not found",
                )
            ]

        if not session.accepted_code:
            return [
                TextContent(
                    type="text",
                    text=f"No accepted code in session {session_id}.",
                )
            ]

        source = "\n\n".join(form.code.rstrip() for form in session.accepted_code) + "\n"

        if file_path:
            try:
                out_path = validate_output_path(file_path, overwrite=overwrite)
            except ValueError as e:
                return [
                    TextContent(
                        type="text",
                        text=f"Error: {e}",
                    )
                ]

            try:
                out_path.write_text(source, encoding="utf-8")
            except OSError as e:
                return [
                    TextContent(
                        type="text",
                        text=f"Error: could not write {out_path.name}: {e}",
                    )
                ]

            return [
                TextContent(
                    type="text",
                    text=f"Exported {len(session.accepted_code)} accepted form(s) to {out_path} "
                    f"({len(source.splitlines())} lines, {len(source)} characters).",
                )
            ]

        return [
            TextContent(
                type="text",
                text=source,
            )
        ]

    else:
        raise ValueError(f"Unknown tool: {name}")


async def handle_call_tool(_ctx: Any, params: Any) -> CallToolResult:
    """MCP 2.x tools/call handler: unwrap the params and wrap the result.

    Unknown-tool and validation failures are reported as tool results with
    ``isError`` set, not as JSON-RPC errors, so the model sees a usable error
    message instead of a protocol failure.
    """
    try:
        content = await call_tool(params.name, params.arguments or {})
    except ValueError as e:
        return CallToolResult(
            content=[TextContent(type="text", text=f"Error: {e}")],
            is_error=True,
        )
    except KeyError as e:
        # The low-level server does not validate inputSchema itself; a client
        # that omits a required field reaches the handler as a KeyError.
        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=f"Error: Missing required argument: {e.args[0]!s}",
                )
            ],
            is_error=True,
        )
    except Exception:
        logger.exception("Unhandled error in tool %s", params.name)
        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text="Error: Internal server error while executing the tool.",
                )
            ],
            is_error=True,
        )
    # Tool handlers report validation and execution failures as text.  Mirror
    # that in isError so MCP clients can react without string matching.  ACL2's
    # own failures say "ACL2 Error" or "******** FAILED ********" rather than
    # starting with "Error:" (and a parinfer note may precede either), so use
    # the shared failure detector; the syntax-check statuses are not ACL2
    # transcripts and need their own prefixes.
    combined = "\n".join(
        block.text for block in content if isinstance(block, TextContent)
    )
    is_error = acl2_output_failed(combined) or combined.startswith(
        (
            "Syntax errors found",
            "Syntax check timed out",
            "Syntax check could not run",
        )
    )
    return CallToolResult(content=list(content), is_error=is_error)


# Global server instance. Handlers are registered through the constructor, the
# MCP 2.x API; the @app.list_tools()/@app.call_tool() decorators were removed
# after the 1.x series.
app: Server = Server(
    "acl2-mcp",
    version=__version__,
    on_list_tools=handle_list_tools,
    on_call_tool=handle_call_tool,
)


async def run() -> None:
    """Run the server."""
    try:
        async with stdio_server() as (read_stream, write_stream):
            await app.run(
                read_stream,
                write_stream,
                app.create_initialization_options(),
            )
    finally:
        # Clean up all sessions on shutdown
        await session_manager.cleanup_all()


def main() -> None:
    """Main entry point for the server."""
    # Ignore SIGPIPE to prevent broken pipe errors when clients disconnect
    # This is safe on Unix-like systems; on Windows SIGPIPE doesn't exist
    if hasattr(signal, 'SIGPIPE'):
        signal.signal(signal.SIGPIPE, signal.SIG_IGN)

    asyncio.run(run())


if __name__ == "__main__":
    main()
