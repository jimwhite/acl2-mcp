# ACL2 MCP Server

> ⚠️ **Early Beta - Development in Progress**
> This is an early beta version that was rapidly developed. While functional and tested, expect rough edges, potential bugs, and breaking changes. Use at your own risk in production environments.

A Model Context Protocol (MCP) server that provides tools for interacting with the ACL2 theorem prover.

## Features

This MCP server exposes 19 tools for working with ACL2, including support for persistent sessions that enable incremental development:

### Session Management Tools
- **start_session**: Create a persistent ACL2 session for incremental development. Accepts `parinfer` (default true) and `parinfer_mode` (default `indent`).
- **end_session**: End a persistent session and clean up resources
- **list_sessions**: List all active sessions with their status

### Code-based Tools
- **prove**: Submit ACL2 theorems (defthm) for proof
- **evaluate**: Evaluate arbitrary ACL2 expressions and definitions
- **check_syntax**: Check ACL2 code for reader/parser errors. The code is balanced with parinfer first (indent mode) unless `parinfer: false`; on success, `return_source: true` returns the balanced source, `output_path` (or its alias `file_path`) writes a different file, and `in_place: true` rewrites the input file. Only reader errors count as syntax errors, and a timeout or reader error never writes. Admission failures and aborts are reported separately (see **Important behaviors** below).
- **admit**: Test if an ACL2 event would be admitted, without keeping it. In a session the event is checked in context and then rolled back with `:ubt`; without a session a fresh process is used.
- **retry_proof**: Submit another proof attempt in a session, typically with changed hints. It balances with parinfer by default, does not undo the previous attempt, and records successful events for export.
- **parinfer**: Balance Lisp/ACL2 code in-process with parinfer (`indent`, `paren`, or `smart` mode; indent is the default). Returns the balanced source, or writes it to `output_path`/`file_path` or back to the input with `in_place`. `paren` mode reports mismatched parens instead of repairing them.

All code-based tools accept either inline `code` or an `input_path` file (exactly one). `prove`, `evaluate`, `admit`, and `retry_proof` accept a `session_id`; `check_syntax` and `parinfer` do not. All of them balance with parinfer by default; pass `parinfer: false` (per call) or disable it on the session to submit code unchanged. `include_book` balances its optional post-load `code` too.

### File-based Tools
- **certify_book**: Certify an ACL2 book file (loads and verifies all definitions and theorems; loads an adjacent `<book>.acl2`/`cert.acl2` and honors `; cert-flags:` when present)
- **include_book**: Load an ACL2 book and optionally evaluate additional code. An existing local book path is used directly; any other name is resolved against ACL2's system book directory.
- **check_theorem**: Check a specific theorem in an ACL2 file by name (file path includes `.lisp`)

### Query and Verification Tools
- **query_event**: Query information about a defined function, theorem, or event (uses :pe)
- **verify_guards**: Verify guards for a function to ensure efficient execution

### Session State Management Tools
- **undo**: Undo the last N events in a session
- **save_checkpoint**: Save a named checkpoint of the current session state
- **restore_checkpoint**: Restore a session to a previously saved checkpoint
- **get_world_state**: Display current session state (recent definitions and theorems)
- **export_session_code**: Export the source of events accepted into the session that are still in its current world. This is a server-tracked submission log: bare evaluations and events removed by undo/restore are omitted, and events added outside these tools are not recovered. A multi-event submission is tracked as one unit, so undoing part of it drops the whole recorded form. Returns the source, or writes it to `file_path` and returns a confirmation with line and character counts.

## Important behaviors

- **Output is cleaned.** The ACL2 startup banner and interactive `ACL2 !>` prompts are removed from results. Set `ACL2_MCP_RAW_OUTPUT=1` to keep the raw transcript for debugging.
- **Sessions start from a bare ACL2 world.** No project books or customization files are loaded; use `include_book` or a session `evaluate` call for that.
- **A session command timeout terminates the session.** This avoids leaving ACL2's reader mid-form, where every later command would hang. Start a new session to continue.
- **`check_syntax` is not a proof.** It runs ACL2 for a bounded time (default 5s, configurable with `timeout`) and classifies only reader errors as syntax errors. A result saying "No syntax errors detected, but ACL2 reported errors..." means the code was readable but admission/proof failed.
- **Default parinfer mode may repair parens.** With indent mode, an extra `)` can be dropped or a missing `)` added before checking. Pass `parinfer: false` to validate the literal source; use `paren` mode with the `parinfer` tool to detect mismatches.
- **`admit` is a test.** In a session it rolls the test event back, so it does not appear in the world or in `export_session_code`.

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

- Python 3.10 or later
- ACL2. Set `ACL2` to the ACL2 executable (for example `/path/to/acl2/saved_acl2`), or put `acl2` on `PATH`. Set `ACL2_SYSTEM_BOOKS` to the ACL2 `books` directory so `include_book` can find system books.
- The parinfer-rust shared library is required for the default balancing behavior. The default path is `/usr/local/lib/libparinfer_rust.so`; set `PARINFER_LIB` to override it. Without it, `prove`/`evaluate`/`admit`/`retry_proof`/`check_syntax` submit code unchanged and say so, while the `parinfer` tool returns an error.

## Installation

```bash
# Clone the repository
cd acl2-mcp

# Create and activate virtual environment
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install the package plus test/type-check tooling
pip install -e '.[dev]'
```

### Running the tests

The ACL2-dependent tests are marked `acl2` and skip when no executable is
configured. To run the complete suite:

```bash
export ACL2=/path/to/acl2/saved_acl2
export ACL2_SYSTEM_BOOKS=/path/to/acl2/books
pytest
```

Without those variables, tests that need a real ACL2 are skipped instead of
failing; pure validation/parinfer tests still run.

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
          "ACL2": "/path/to/acl2/saved_acl2"
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

Returns: "Session started successfully. ID: <uuid>" (parse out the UUID)
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
  timeout: 30  (optional)

Returns the definition and properties of the named event.
```

**Verify Guards:**
```
Tool: verify_guards
Arguments:
  function_name: "my-function"
  file_path: "path/to/file.lisp"  (optional, if function is in a file)
  timeout: 60  (optional)

Verifies that the function's guards are satisfied.
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

Tests marked `acl2` skip when no ACL2 executable is configured, so the suite
does not fail with `FileNotFoundError` on a machine without ACL2.

## How It Works

The server supports two execution modes:

### One-off Execution (Default)
When no `session_id` is provided, each tool call:
1. Writes ACL2 code to a temporary `.lisp` file
2. Starts a fresh ACL2 process with the code as input
3. Captures stdout/stderr and strips the startup banner/prompts
4. Cleans up the temporary file and terminates ACL2

If the ACL2 executable cannot be started, the tool returns an `Error: Could not
start ACL2 executable ...` message rather than raising a Python exception.

### Persistent Sessions (Incremental Development)
When using sessions:
1. `start_session` creates a long-running ACL2 process with persistent stdin/stdout pipes
2. A background task drains stderr so a full pipe cannot block ACL2
3. Each tool call sends commands to the existing process and reads until a random marker
4. The probe's own return value and prompt are consumed so they do not leak into the next result
5. The ACL2 world state accumulates across multiple commands
6. `undo`/checkpoint restore and raw `:ubt` commands prune checkpoints and exported forms
7. A command timeout terminates the process instead of leaving the reader desynchronized
8. Sessions auto-cleanup after 30 minutes of inactivity or when explicitly ended
9. Up to 50 concurrent sessions are supported

Per-tool timeout defaults are 30–120 seconds (see each tool); values are clamped
to 1–300 seconds. `check_syntax` defaults to a 5-second bounded run.

## License

BSD 3-Clause License - See [LICENSE](LICENSE) for details.
