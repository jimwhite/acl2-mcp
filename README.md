# ACL2 MCP Server

> ⚠️ **Early Beta - Development in Progress**
> This is an early beta version that was rapidly developed. While functional and tested, expect rough edges, potential bugs, and breaking changes. Use at your own risk in production environments.

A Model Context Protocol (MCP) server that provides tools for interacting with the ACL2 theorem prover.

Execution is delegated to the [ACL2 Jupyter kernel](https://github.com/jimwhite/acl2-jupyter-kernel), which runs *inside* the ACL2 process (on SBCL or CCL) and speaks the Jupyter wire protocol. The server is a Jupyter client: values, proof output, reader errors, and per-cell event metadata arrive as protocol messages rather than scraped prompts. Each persistent session owns one kernel process; one-off tools start a fresh kernel, run one cell, and shut it down.

## Features

This MCP server exposes 19 tools for working with ACL2, including support for persistent sessions that enable incremental development:

### Session Management Tools
- **start_session**: Create a persistent ACL2 session (one Jupyter kernel process) for incremental development. Accepts `parinfer` (default true) and `parinfer_mode` (default `indent`).
- **end_session**: End a persistent session and clean up resources
- **list_sessions**: List all active sessions with their status

### Code-based Tools
- **prove**: Submit ACL2 theorems (defthm) for proof
- **evaluate**: Evaluate arbitrary ACL2 expressions and definitions
- **check_syntax**: Check ACL2 code for reader/parser errors. The code is balanced with parinfer first (indent mode) unless `parinfer: false`; on success, `return_source: true` returns the balanced source, `output_path` (or its alias `file_path`) writes a different file, and `in_place: true` rewrites the input file. The kernel's reader-completeness request classifies the text before anything runs, so unbalanced input is rejected without executing or writing. Readable code is then run for a bounded time; admission failures and aborts are reported separately (see **Important behaviors** below).
- **admit**: Test if an ACL2 event would be admitted, without keeping it. In a session the event is checked in context and then rolled back with `:ubt`; without a session a fresh kernel is used and discarded.
- **retry_proof**: Submit another proof attempt in a session, typically with changed hints. It balances with parinfer by default, does not undo the previous attempt, and records successful events for export.
- **parinfer**: Balance Lisp/ACL2 code in-process with parinfer (`indent`, `paren`, or `smart` mode; indent is the default). Returns the balanced source, or writes it to `output_path`/`file_path` or back to the input with `in_place`. `paren` mode reports mismatched parens instead of repairing them.

All code-based tools accept either inline `code` or an `input_path` file (exactly one). `prove`, `evaluate`, `admit`, and `retry_proof` accept a `session_id`; `check_syntax` and `parinfer` do not. All of them balance with parinfer by default; pass `parinfer: false` (per call) or disable it on the session to submit code unchanged. `include_book` balances its optional post-load `code` too.

### File-based Tools
- **certify_book**: Certify an ACL2 book file (loads and verifies all definitions and theorems; loads an adjacent `<book>.acl2`/`cert.acl2` and honors `; cert-flags:` when present)
- **include_book**: Load an ACL2 book and optionally evaluate additional code. An existing local book path is used directly; any other name is resolved against ACL2's system book directory.
- **check_theorem**: Check a specific theorem in an ACL2 file by name (file path includes `.lisp`)

### Query and Verification Tools
- **query_event**: Query information about a defined function, theorem, or event (uses :pe). Pass `session_id` to query a persistent session's world.
- **verify_guards**: Verify guards for a function to ensure efficient execution. Pass `session_id` to verify a function in a persistent session.

### Session State Management Tools
- **undo**: Undo the last N events in a session
- **save_checkpoint**: Save a named checkpoint of the current session state
- **restore_checkpoint**: Restore a session to a previously saved checkpoint
- **get_world_state**: Display current session state (recent definitions and theorems)
- **export_session_code**: Export the source of events accepted into the session that are still in its current world. This is a server-tracked submission log: bare evaluations and events removed by undo/restore are omitted, and events added outside these tools are not recovered. A multi-event submission is tracked as one unit, so undoing part of it drops the whole recorded form. Returns the source, or writes it to `file_path` and returns a confirmation with line and character counts.

## Important behaviors

- **Output comes from the kernel.** There are no interactive prompts to scrape. ACL2's startup banner is written to a per-kernel log file (`/tmp/acl2-mcp-kernel-<name>.log` by default) so it cannot corrupt the MCP stdio transport. `ld`-style announcements and `Bye.`/`:EOF` trailers are removed from results. Set `ACL2_MCP_RAW_OUTPUT=1` to keep the raw cell transcript, and `ACL2_MCP_KERNEL_LOG_DIR` to redirect the child-process log.
- **Sessions start from a bare ACL2 world.** No project books or customization files are loaded; use `include_book` or a session `evaluate` call for that. `ACL2_SYSTEM_BOOKS` and `ACL2_CUSTOMIZATION` are passed to the kernel process.
- **A session command timeout terminates the session.** The kernel is stopped rather than left mid-form. Start a new session to continue.
- **`check_syntax` asks the reader first.** The kernel reports `complete`, `incomplete`, or `invalid` without executing unfinished input; only readable code is run for the bounded timeout (default 5s). A result saying "No syntax errors detected, but ACL2 reported errors..." means the code was readable but admission/proof failed.
- **Default parinfer mode may repair parens.** With indent mode, an extra `)` can be dropped or a missing `)` added before checking. Pass `parinfer: false` to validate the literal source; use `paren` mode with the `parinfer` tool to detect mismatches. An unbalanced cell submitted with `parinfer: false` now returns a reader error immediately instead of hanging.
- **`admit` is a test.** In a session it rolls world events back, so they do not appear in the world or in `export_session_code`. Command state that is not part of the world (for example `set-guard-checking`) is not undone.
- **Book certificates are implementation-specific.** Certify and include books with the same Lisp that built the ACL2 image. Using an SBCL ACL2 against a books tree certified by CCL fails with a logical-world mismatch, so keep one implementation per repository/books tree and install the matching kernelspec.

### Tool argument conventions

- `code` vs `input_path`: exactly one, for `prove`, `evaluate`, `check_syntax`, `admit`, `retry_proof`, and `parinfer`.
- `file_path` is **not** uniform:
  - input file: `query_event`, `verify_guards`, `check_theorem`
  - output alias for `output_path`: `check_syntax`, `parinfer`
  - output file: `export_session_code`
  - book path: `certify_book`, `include_book`
- `output_path`/`in_place`/`overwrite` apply to `check_syntax` and `parinfer`; `in_place` requires `input_path` and cannot be combined with `output_path`/`file_path`.
- Timeouts are clamped to 1–300 seconds.

## Prerequisites

- Python 3.10 or later. `jupyter_client` and `pyzmq` are installed as package dependencies.
- ACL2 built on **SBCL or CCL**, with Quicklisp available. The kernel quickloads itself from `quicklisp/setup.lisp` at startup.
- An installed ACL2 Jupyter kernelspec for that ACL2 image (see below).
- `libzmq` for the kernel's ZeroMQ transport.
- The parinfer-rust shared library is required for the default balancing behavior. The default path is `/usr/local/lib/libparinfer_rust.so`; set `PARINFER_LIB` to override it. Without it, `prove`/`evaluate`/`admit`/`retry_proof`/`check_syntax` submit code unchanged and say so, while the `parinfer` tool returns an error.

### Kernel selection

The server picks a kernelspec from the environment (an explicit override wins, then the `ACL2` launcher script, then `LISP`):

| Variable | Effect |
| --- | --- |
| `ACL2` | Path to `saved_acl2`. Its `exec` line selects CCL (`-I`) or SBCL (`--core`), and must exist and be executable. |
| `LISP` (or `ACL2_LISP`) | Used when the script is ambiguous: `ccl` → kernel `acl2`, `sbcl` → kernel `acl2-sbcl`. |
| `ACL2_MCP_KERNEL` (or `ACL2_MCP_KERNEL_NAME`) | Explicit kernelspec name; overrides detection. |
| `ACL2_SYSTEM_BOOKS` | Passed to the kernel; controls `:dir :system` resolution. |
| `ACL2_CUSTOMIZATION` | Passed to the kernel; defaults to `NONE`. |
| `ACL2_MCP_KERNEL_LOG_DIR` | Directory for kernel child-process logs (default `/tmp`). |
| `ACL2_MCP_RAW_OUTPUT` | Keep raw cell transcripts in tool results. |

### Install a kernelspec

The kernel is linked as the submodule `external/acl2-jupyter-kernel`. From
there:

```bash
cd external/acl2-jupyter-kernel

# For the ACL2 image you use (CCL example: /workspaces/acl2/saved_acl2)
ACL2=/workspaces/acl2/saved_acl2 \
ACL2_HOME=/workspaces/acl2 \
QUICKLISP_SETUP="$HOME/quicklisp/setup.lisp" \
  ./install-kernelspec.sh
```

This installs the kernel under the name **`acl2`**. To keep a second implementation available as well, give it a distinct name:

```bash
# SBCL image elsewhere; installs kernelspec "acl2-sbcl"
ACL2=/path/to/sbcl-saved_acl2 ACL2_HOME=/path/to/acl2 \
ACL2_JUPYTER_KERNEL_NAME=acl2-sbcl \
ACL2_JUPYTER_DISPLAY_NAME="ACL2 (SBCL)" \
QUICKLISP_SETUP="$HOME/quicklisp/setup.lisp" \
  ./install-kernelspec.sh
```

Remember that a books tree is certified for one Lisp implementation. Do not point an SBCL kernel at a CCL-certified books tree (or vice versa).

## Installation

```bash
# Clone the repository
cd acl2-mcp

# Get the linked repository dependencies (ACL2 Jupyter kernel, parinfer-rust)
git submodule update --init --recursive

# Create and activate virtual environment
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install the package plus test/type-check tooling
pip install -e '.[dev]'
```

### Linked repositories

`git submodule update --init --recursive` provides:

| Path | Purpose |
| --- | --- |
| `external/acl2-jupyter-kernel` | The ACL2 Jupyter kernel used for all execution; install its kernelspec as described above. |
| `external/parinfer-rust` | Source for `libparinfer_rust.so` (optional). Build with `cargo build --release`; install the library or point `PARINFER_LIB` at it. |

The superproject records the pinned commit for each submodule. To move a pin,
check out the desired commit inside the submodule and commit the updated
gitlink in `acl2-mcp`.

### Running the tests

The ACL2-dependent tests are marked `acl2` and skip unless both a working `ACL2` launcher and the matching kernelspec are available. To run the complete suite:

```bash
export ACL2=/workspaces/acl2/saved_acl2
export ACL2_SYSTEM_BOOKS=/workspaces/acl2/books
pytest
```

Without those prerequisites, tests that need a real ACL2 kernel are skipped instead of failing; pure validation/parinfer tests still run.

## Usage

### Running the Server

The server can be run directly:

```bash
acl2-mcp
```

Or via Python:

```bash
python -m acl2_mcp.server
```

### Running as HTTP Service

For HTTP clients (for example the ACL2 `mcp-client` book), put `mcp-proxy` in
front of the stdio server. This is a local, unauthenticated endpoint; keep it
bound to localhost unless you add authentication.

```bash
pip install mcp-proxy

ACL2=/path/to/acl2/saved_acl2 \
ACL2_SYSTEM_BOOKS=/path/to/acl2/books \
  mcp-proxy acl2-mcp --transport streamablehttp --port 8000 \
  --allow-origin='*' --pass-environment
```

Streamable HTTP requires an initialization handshake, the returned
`Mcp-Session-Id` on every later request, and an `Accept` header that includes
`text/event-stream`. A call made without initializing first returns
`400 Bad Request: Missing session ID`.

```bash
# 1. Initialize and capture the session id.
SID=$(curl -sS -D - -o /tmp/acl2-init.json \
  -X POST http://localhost:8000/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{
        "protocolVersion":"2024-11-05","capabilities":{},
        "clientInfo":{"name":"curl-client","version":"1.0.0"}}}' \
  | awk 'tolower($1)=="mcp-session-id:" {print $2}' | tr -d '\r')

# 2. Reuse that session id for tools/list and tools/call.
curl -sS -X POST http://localhost:8000/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H "Mcp-Session-Id: $SID" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'

curl -sS -X POST http://localhost:8000/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H "Mcp-Session-Id: $SID" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{
        "name":"evaluate","arguments":{"code":"(+ 1 2)"}}}'
```

Stop `mcp-proxy` to end the HTTP endpoint; ACL2 sessions it created are
terminated with the server process.

### Configuring in ZCode

ZCode reads MCP servers from the workspace config (`<repo>/.zcode/config.json`) or from its user config (`~/.zcode/cli/config.json`); workspace-scoped servers connect automatically. Add the server to the workspace config:

```json
{
  "mcp": {
    "servers": {
      "acl2": {
        "type": "stdio",
        "command": "/path/to/acl2-mcp/venv/bin/acl2-mcp",
        "args": [],
        "env": {
          "ACL2_SYSTEM_BOOKS": "/path/to/acl2/books",
          "ACL2": "/path/to/acl2/saved_acl2",
          "ACL2_CUSTOMIZATION": "NONE",
          "LISP": "ccl"
        }
      }
    }
  }
}
```

You can also add it through **Settings → MCP** (user scope). Workspace scope is the better choice when the ACL2 binary lives inside a project container.

### Configuring in Claude Desktop

Add this to your Claude Desktop configuration file:

**macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
**Windows**: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "acl2": {
      "command": "/path/to/acl2-mcp/venv/bin/acl2-mcp",
      "env": {
        "ACL2": "/path/to/acl2/saved_acl2",
        "ACL2_SYSTEM_BOOKS": "/path/to/acl2/books",
        "PARINFER_LIB": "/usr/local/lib/libparinfer_rust.so"
      }
    }
  }
}
```

Replace `/path/to/acl2-mcp` with the actual path to your installation directory.
The `env` block is needed unless `acl2` and the parinfer library are already
reachable through the client's environment.

### Configuring in Claude Code

**Recommended: Using the CLI** (Simplest method)

Claude Code provides a CLI command to add MCP servers:

```bash
claude mcp add acl2 /path/to/acl2-mcp/venv/bin/acl2-mcp
```

Replace `/path/to/acl2-mcp` with the actual path to your installation directory.

This will automatically configure the server in your Claude Code settings.

**Alternative: Manual Configuration**

You can also manually edit the Claude Code MCP settings file:

**macOS/Linux**: `~/.config/claude-code/mcp_settings.json`
**Windows**: `%APPDATA%\claude-code\mcp_settings.json`

**Option 1: Using the installed executable** (Recommended)
```json
{
  "mcpServers": {
    "acl2": {
      "command": "/path/to/acl2-mcp/venv/bin/acl2-mcp",
      "env": {
        "ACL2": "/path/to/acl2/saved_acl2",
        "ACL2_SYSTEM_BOOKS": "/path/to/acl2/books"
      }
    }
  }
}
```

**Option 2: Using Python module**
```json
{
  "mcpServers": {
    "acl2": {
      "command": "/path/to/acl2-mcp/venv/bin/python",
      "args": [
        "-m",
        "acl2_mcp.server"
      ],
      "env": {
        "ACL2": "/path/to/acl2/saved_acl2",
        "ACL2_SYSTEM_BOOKS": "/path/to/acl2/books"
      }
    }
  }
}
```

Replace `/path/to/acl2-mcp` with the actual path to your installation directory.
When using the CLI form, also pass the ACL2 environment variables through your
client's env/MCP settings (the exact flag varies by Claude Code version).

### Example Tool Usage

#### Persistent Session Workflow (Recommended for Interactive Development)

For incremental development where you build up definitions and theorems step-by-step, use persistent sessions:

**1. Start a session:**
```
Tool: start_session
Arguments:
  name: "natural-numbers-proof"  (optional, for easy identification)

Returns: "Session started successfully. Use the ID below as the session_id argument in later calls.
ID: <uuid>" (use the UUID after "ID: ")
```

**2. Define functions incrementally:**
```lisp
Tool: evaluate
Arguments:
  session_id: "a1b2c3d4-..."
  code: "(defun plus (x y) (if (zp x) y (plus (1- x) (1+ y))))"

Tool: evaluate
Arguments:
  session_id: "a1b2c3d4-..."
  code: "(plus 2 3)"  // Test the function
```

**3. Build on previous definitions:**
```lisp
Tool: evaluate
Arguments:
  session_id: "a1b2c3d4-..."
  code: "(defun times (x y) (if (zp y) 0 (plus x (times x (1- y)))))"
```

**4. Prove theorems interactively:**
```lisp
Tool: prove
Arguments:
  session_id: "a1b2c3d4-..."
  code: "(defthm plus-commutative (equal (plus x y) (plus y x)))"
```

**5. If proof fails, retry with hints:**
```lisp
Tool: retry_proof
Arguments:
  session_id: "a1b2c3d4-..."
  code: "(defthm plus-commutative
          (equal (plus x y) (plus y x))
          :hints ((\"Goal\" :induct (plus x y))))"
```
`retry_proof` just submits the new attempt (balanced with parinfer by
default); it does not undo the previous one.

**6. Save checkpoints before risky steps:**
```lisp
Tool: save_checkpoint
Arguments:
  session_id: "a1b2c3d4-..."
  checkpoint_name: "before-induction"
```

**7. Restore if needed:**
```lisp
Tool: restore_checkpoint
Arguments:
  session_id: "a1b2c3d4-..."
  checkpoint_name: "before-induction"
```

**8. Inspect session state:**
```lisp
Tool: get_world_state
Arguments:
  session_id: "a1b2c3d4-..."
  limit: 20  (show last 20 events)
```

**9. Undo mistakes:**
```lisp
Tool: undo
Arguments:
  session_id: "a1b2c3d4-..."
  count: 1  (undo last event)
```

**10. Export accepted code, then end the session:**
```
Tool: export_session_code
Arguments:
  session_id: "a1b2c3d4-..."
  file_path: "path/to/session.lisp"  (optional)

Tool: end_session
Arguments:
  session_id: "a1b2c3d4-..."
```
The export is a log of accepted events still in the current world, not a dump
of every form ever submitted.

**Benefits of persistent sessions:**
- ✅ No need to wrap everything in `progn`
- ✅ Test functions immediately after defining them
- ✅ Build complex proofs incrementally
- ✅ Try different proof strategies without re-submitting entire files
- ✅ Save/restore checkpoints for experimentation
- ⚡ Sessions auto-timeout after 30 minutes of inactivity

#### Code-based Tools (One-off Execution)

**Prove a Theorem:**
```lisp
(defthm append-nil
  (implies (true-listp x)
           (equal (append x nil) x)))
```

**Evaluate Expressions:**
```lisp
(defun factorial (n)
  (if (zp n)
      1
    (* n (factorial (- n 1)))))

(factorial 5)
```

**Check Syntax:**
```lisp
(defun my-function (x y)
  (+ x y))
```
This checks readability, not admission. With the default indent-mode parinfer,
mismatched parens may be repaired before the check; pass `parinfer: false` to
check the literal source. A timeout, reader error, or "ACL2 reported errors"
message is not a successful check.

#### File-based Tools

**Certify a Book:**
```
Tool: certify_book
Arguments:
  file_path: "path/to/mybook"  (without .lisp extension)
  timeout: 120  (optional)
```

**Include a Book and Run Code:**
```
Tool: include_book
Arguments:
  file_path: "path/to/mybook"  (without .lisp extension)
  code: "(thm (equal (+ 1 1) 2))"  (optional; balanced with parinfer by default)
  timeout: 60  (optional)
```
An existing local file is included directly; otherwise the name is resolved
against `ACL2_SYSTEM_BOOKS` (ACL2's `:dir :system`).

**Check a Specific Theorem:**
```
Tool: check_theorem
Arguments:
  file_path: "path/to/myfile.lisp"
  theorem_name: "my-theorem-name"
  timeout: 60  (optional)
```

#### Query and Verification Tools

**Admit an Event:**
```
Tool: admit
Arguments:
  code: "(defun my-func (x) (+ x 1))"
  timeout: 30  (optional)

Returns whether the event would be admitted successfully. In a session the
test event is rolled back, so it is not kept in the world.
```

**Query an Event:**
```
Tool: query_event
Arguments:
  name: "append"
  file_path: "path/to/file.lisp"  (optional, if function is in a file)
  session_id: "a1b2c3d4-..."  (optional; query this session's world instead)
  timeout: 30  (optional)

Returns the definition and properties of the named event. Use either
`file_path` or `session_id`, not both.
```

**Verify Guards:**
```
Tool: verify_guards
Arguments:
  function_name: "my-function"
  file_path: "path/to/file.lisp"  (optional, if function is in a file)
  session_id: "a1b2c3d4-..."  (optional; verify in this session's world)
  timeout: 60  (optional)

Verifies that the function's guards are satisfied. Use either `file_path` or
`session_id`, not both.
```

## Development

### Type Checking

This project uses strict static typing with mypy:

```bash
pip install -e '.[dev]'
mypy acl2_mcp/
```

### Running Tests

```bash
export ACL2=/path/to/acl2/saved_acl2
export ACL2_SYSTEM_BOOKS=/path/to/acl2/books
pytest
```

Tests marked `acl2` skip when no ACL2 launcher or matching kernelspec is
configured, so the suite does not fail on a machine without ACL2 or the
Jupyter kernel.

## How It Works

All execution goes through a Jupyter kernel (`acl2` or `acl2-sbcl`) started with `jupyter_client`:

### One-off Execution (Default)
When no `session_id` is provided, each tool call:
1. Starts a fresh ACL2 kernel process (child stdout/stderr go to a log file, not the MCP transport)
2. Sends the code as one `execute_request`
3. Collects `stream`, `execute_result`, `error`, and `display_data` messages
4. Reads the vendor event metadata (`application/vnd.acl2.events+json`) for event counts/forms
5. Shuts the kernel down (on timeout it is killed and the error says so)

### Persistent Sessions (Incremental Development)
When using sessions:
1. `start_session` starts one long-lived ACL2 kernel
2. Each tool call sends one cell; output and values arrive as Jupyter messages (no prompt scraping)
3. After every command the server asks ACL2 for its current relative command number, so `undo`/checkpoints stay exact even for multi-event commands and failed proofs
4. `undo`/checkpoint restore send `:ubt` and prune checkpoints and exported forms above the new position
5. `get_world_state` reads ACL2's command history directly
6. A command timeout shuts the kernel down instead of leaving a desynchronized reader
7. Sessions auto-cleanup after 30 minutes of inactivity or when explicitly ended
8. Up to 50 concurrent sessions are supported

Per-tool timeout defaults are 30–120 seconds (see each tool); values are clamped
to 1–300 seconds. `check_syntax` defaults to a 5-second bounded run after the
kernel's reader check.

### Kernel backend code

`acl2_mcp/kernel_backend.py` contains the transport: kernelspec resolution,
environment handling, `KernelClient` (execute, completeness check, command
number), and one-shot helpers. `server.py` keeps the tool API and uses those
primitives.

## License

BSD 3-Clause License - See [LICENSE](LICENSE) for details.
