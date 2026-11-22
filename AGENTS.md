# AGENTS.md

This file provides guidance to AI coding agents (ZCode and other AGENTS.md-aware tools) when working with code in this repository.

## Container user

In this dev container, run every server, test, and ACL2 command as the
`vscode` user, never as root. The ACL2 image, kernelspecs, Quicklisp, and
virtualenv live under `/home/vscode`; root-owned files there break the kernel
and the MCP server.

From a root shell:

```bash
export ACL2=/workspaces/acl2/saved_acl2
export ACL2_SYSTEM_BOOKS=/workspaces/acl2/books
export ACL2_CUSTOMIZATION=NONE
export LISP=ccl
sudo -n -E -u vscode env HOME=/home/vscode \
  /home/vscode/venv/bin/python -m pytest
```

Use the same `sudo -n -E -u vscode env HOME=/home/vscode ...` form for any other
command. This is also how the workspace `.zcode/config.json` launches the MCP
server.

## Project Overview

This is an MCP (Model Context Protocol) server that provides tools for interacting with the ACL2 theorem prover. The server enables AI assistants to work with ACL2 through 19 different tools, supporting both one-off execution and persistent sessions for incremental development.

## Architecture

### Core Components

- **[acl2_mcp/server.py](acl2_mcp/server.py)**: Main server implementation containing:
  - Tool definitions and handlers (`list_tools()`, `call_tool()`)
  - ACL2 execution functions (`run_acl2()`, `run_acl2_file()`)
  - Session management (`SessionManager`, `ACL2Session`)
  - Security validation functions (timeout, file path, identifiers, etc.)
- **[acl2_mcp/kernel_backend.py](acl2_mcp/kernel_backend.py)**: Jupyter
  transport, kernel selection, and per-kernel logging.
- **[acl2_mcp/provision.py](acl2_mcp/provision.py)**: Builds/locates the
  parinfer library and installs the ACL2 Jupyter kernelspec on first use; also
  the `acl2-mcp-install` entry point.
- **[setup.py](setup.py)**: `build_py` hook that stages the bundled kernel
  sources and builds parinfer. **[MANIFEST.in](MANIFEST.in)** ships the
  submodule sources in the sdist.

### Execution Modes

1. **One-off execution** (default): Each tool call starts a fresh Jupyter
   kernel (`KernelClient`), runs one cell, and shuts the kernel down
   (`execute_once`). The file-based tools wrap this with `ld`, `include-book`,
   or `certify-book`. The ACL2 world starts bare each time.

2. **Persistent sessions**: One long-lived Jupyter kernel per session
   - Managed by `SessionManager` (singleton: `session_manager`)
   - Each session owns a `KernelClient` speaking the Jupyter wire protocol over
     ZeroMQ; there is no stdin/stdout pipe or marker protocol
   - Sessions maintain ACL2 world state across commands
   - Auto-cleanup after 30 minutes inactivity; dead sessions are reaped within
     about a minute
   - Max 50 concurrent sessions

### Session Communication

- Commands are sent as Jupyter `execute_request` messages; output, values,
  errors, and per-cell event metadata arrive as protocol messages.
- Completion is the Jupyter `status: idle` message for that request, not a
  marker or a scraped prompt.
- `KernelClient.command_number()` runs `COMMAND_NUMBER_FORM` after a command to
  refresh the session's relative event count, which drives `undo`,
  checkpoints, and export pruning.
- A command timeout shuts the kernel down (`_shutdown_unlocked`) and marks the
  session dead. This is deliberate: leaving the kernel inside an incomplete
  form would desynchronize every later command.
- The kernel child's stdout/stderr are redirected to
  `/tmp/acl2-mcp-kernel-<kernel>.log` (`ACL2_MCP_KERNEL_LOG_DIR`) so ACL2 output
  cannot corrupt the MCP stdio transport. The log rotates to `.1` above
  `ACL2_MCP_KERNEL_LOG_MAX_BYTES` (default 10 MB).

### Output Cleaning

`clean_acl2_output` removes the startup banner and `ACL2 !>` prompts from
results.  `ACL2_MCP_RAW_OUTPUT=1` keeps the raw transcript for debugging.

### Security Features

Security validation is extensive (see `SECURITY.md`):
- Input validation: code length (1MB max), timeout (1-300s), file paths, identifiers
- Identifier validation accepts only plain ACL2 symbols (letters, digits, and
  the usual symbol punctuation); whitespace, newlines, quotes, parens, and `;`
  are rejected
- Command injection prevention: ACL2 strings escaped, identifiers validated
- DoS prevention: session limits, timeout enforcement, checkpoint limits
- Missing ACL2 executables and missing required arguments are reported as tool
  errors (`isError`) instead of uncaught Python exceptions
- Unexpected internal exceptions are logged and returned generically

## Development Commands

### Setup
```bash
# Create virtual environment and install (dev extras include pytest + mypy)
python3 -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -e '.[dev]'

# ACL2 is configured through the environment, not a hardcoded PATH lookup
export ACL2=/path/to/acl2/saved_acl2
export ACL2_SYSTEM_BOOKS=/path/to/acl2/books
# Optional: force a parinfer library instead of the one built by setup.py
# export PARINFER_LIB=/path/to/libparinfer_rust.so
```

`pip install` builds `external/parinfer-rust` into `acl2_mcp/_lib` and bundles
`external/acl2-jupyter-kernel` into `acl2_mcp/_kernel`; on first use the server
installs the kernelspec from that bundle (`acl2-mcp-install` does it
explicitly). Set `ACL2_MCP_SKIP_PARINFER_BUILD=1` to skip the Rust build.

### Running the Server
```bash
# Direct execution
acl2-mcp

# Via Python module
python -m acl2_mcp.server
```

### Testing
```bash
# Run all tests (ACL2-dependent tests skip if ACL2 is not configured)
pytest

# Run specific test file
python -m pytest tests/test_server.py -v

# Run with verbose output and short traceback
python -m pytest tests/ -v --tb=short
```

Tests that need a real ACL2 carry the `acl2` marker (see `tests/conftest.py`);
without `ACL2` (or `acl2` on PATH) they skip rather than fail.

### Type Checking
```bash
# Strict mode is configured in pyproject.toml; this should report no issues
python -m mypy acl2_mcp/
```

## Testing Architecture

Test organization:
- **[tests/conftest.py](tests/conftest.py)**: `acl2` marker, skip logic, and kernelspec provisioning
- **[tests/test_provision.py](tests/test_provision.py)**: Parinfer lookup/build and end-to-end kernelspec provisioning
- **[tests/test_server.py](tests/test_server.py)**: Core functionality tests (tools, ACL2 execution)
- **[tests/test_security.py](tests/test_security.py)**: Validation, injection, launch-error, and tool-error tests
- **[tests/test_sessions.py](tests/test_sessions.py)**: Session management tests
- **[tests/test_backtracking.py](tests/test_backtracking.py)**: Undo/checkpoint/theorem/book regression tests
- **[tests/test_parinfer.py](tests/test_parinfer.py)**: In-process parinfer balancing and session integration
- **[tests/test_export.py](tests/test_export.py)**: Accepted-event export (text and file modes)
- **[tests/test_file_input.py](tests/test_file_input.py)**: `input_path`/`output_path`/`in_place` across tools
- **[tests/test_syntax.py](tests/test_syntax.py)**: Parinfer-aware check_syntax source/file output
- **[tests/test_regressions.py](tests/test_regressions.py)**: Admit rollback, export pruning, retry balancing, syntax status, timeout termination
- **[tests/test_protocol.py](tests/test_protocol.py)**: Real MCP client negotiation and `isError` behavior

All async tests use `pytest-asyncio`; the fixture loop scope is configured in
`pyproject.toml`.

## Tool Implementation Pattern

When adding/modifying tools:

1. Add tool definition to `list_tools()` with:
   - Clear description (optimized for AI understanding)
   - Complete `input_schema` with types and descriptions (the MCP 2.x
     Python field name; it serializes to `inputSchema` on the wire)
   - Required fields marked in schema

2. Add handler in `call_tool()`:
   - Extract and validate all arguments
   - Use appropriate validation functions
   - Use session if `session_id` provided, otherwise one-off execution
   - Return `Sequence[TextContent]` (usually single-element list)

3. For session commands:
   - Check session exists: `session_manager.get_session(session_id)`
   - Send command: `await session.send_command(code, timeout)`
   - Session lock already handled by `send_command()`

## Semantics Worth Preserving

- **admit** is a test, not a write: in a session it records the event count
  before and after, then `:ubt`s any added events. It does not call
  `record_accepted`.
- **export_session_code** tracks accepted *events* (`AcceptedForm` carries the
  event number). `send_command` prunes entries above the new event count on
  undo/restore/raw `:ubt`; bare evaluations are not recorded. When a
  multi-form submission adds events and also reports an ACL2 error (ACL2
  continues past a failed event), `accepted_submission_source` records the
  forms the kernel reported as accepted instead of dropping the submission.
  include-book expansions are recorded only when the whole submission
  succeeded.
- **include_book** is session-aware: with `session_id` it loads into that
  session's world (and records the load for export); without one it uses a
  throwaway kernel.
- **retry_proof** balances with parinfer and records successful events, like
  `prove`.
- **acl2_output_failed** matches full ACL2 error headers ("ACL2 Error in ...",
  "ACL2 Error [Failure] in ...", "ACL2 Error: ...") rather than the bare
  string, so user output such as `(cw "ACL2 Error")` does not turn a
  successful call into `isError`.
- **query_event** and **verify_guards** accept either a one-off world
  (optionally seeded with `file_path`) or a session's current world
  (`session_id`); the two sources are mutually exclusive. A successful session
  `verify_guards` records the event for export.
- **check_theorem** strips the `ld` prefix and the expected `:ubt` error for a
  missing name, and starts its result with an explicit re-proof summary.
- **check_syntax** classifies reader errors separately from admission failures,
  aborts, and timeouts, and does not write output on reader errors or timeouts.
  With `session_id`, readable code is executed in the session's context and any
  events it adds are rolled back (like `admit`); without one it uses a
  throwaway kernel.
- **Session timeout** shuts down the kernel
  (`KernelClient._shutdown_unlocked`) and marks the session dead; do not change
  this back to returning a timeout while leaving the kernel running.

## Key Constraints

- **ACL2 dependency**: The executable comes from `ACL2`, falling back to `acl2`
  on PATH. A missing executable produces a clear tool error; it does not shut
  the server down. `ACL2_SYSTEM_BOOKS` locates system books.
- **Provisioning** (`acl2_mcp/provision.py`): `setup.py`'s `build_py` hook
  builds `external/parinfer-rust` into `acl2_mcp/_lib` and stages
  `external/acl2-jupyter-kernel` into `acl2_mcp/_kernel`. On first use the
  server (and `acl2-mcp-install`) installs a missing kernelspec from the
  bundled sources via the ACL2 image and patches the spec's startup argv to
  put the bundled kernel directory on `asdf:*central-registry*`. Wheels are
  platform-tagged because they contain the native library.
- **Parinfer dependency**: Balancing needs the parinfer-rust shared library.
  Lookup order is `PARINFER_LIB`, the packaged `acl2_mcp/_lib`, the source
  build under `external/parinfer-rust/target/release`, then the legacy
  `/usr/local/lib/libparinfer_rust.so`. If it is unavailable, code tools
  submit unchanged and prepend a warning; the `parinfer` tool errors.
- **Python 3.10+**: Uses modern type hints (including `dict[str, X]` syntax)
- **Strict typing**: `mypy --strict` is configured in `pyproject.toml`; run it
  manually (there is no CI workflow in this repository yet)
- **File path conventions**:
  - Books: normally WITHOUT `.lisp` (certify_book, include_book); a trailing
    `.lisp` is accepted
  - Regular input files: WITH `.lisp` for check_theorem, query_event, verify_guards
  - `check_syntax`/`parinfer`: `input_path` is the input and
    `output_path`/`file_path` is the output destination
  - `export_session_code`: `file_path` is the output destination
- **Code tools**: `code` and `input_path` are mutually exclusive and exactly one
  is required (prove, evaluate, check_syntax, admit, retry_proof, parinfer).
  `check_syntax` and `include_book` also accept `session_id`.
- **Timeouts**: clamped to 1–300 seconds; a session timeout terminates that
  session
- **Async throughout**: All I/O operations use asyncio
