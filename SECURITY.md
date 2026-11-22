# Security

> ⚠️ **Early Beta Software**
> This is an early beta version developed rapidly. While security measures have been implemented and tested, this software has not undergone professional security audit. Use with caution and only with trusted code.

This document describes the security measures implemented in the ACL2 MCP server.

## Security Model

The ACL2 MCP server executes arbitrary ACL2 code provided by the client. This is inherently powerful and requires careful security considerations. The server is designed to be run locally by a trusted user (e.g., as part of ZCode or another trusted MCP client) and assumes the client has permission to execute ACL2 code on the system.

## Threat Model

**In Scope:**
- Command injection attacks through malicious inputs
- Path traversal attacks accessing unauthorized files
- Resource exhaustion through unbounded inputs
- Information disclosure through error messages
- Session hijacking and unauthorized access
- Race conditions in concurrent session access

**Out of Scope:**
- Network-based attacks (server runs locally via stdio)
- Multi-tenancy and isolation (single-user design)
- ACL2 itself (we trust ACL2's security model)
- Sandboxing ACL2 process execution (ACL2 has full system access)

## Security Measures

### 1. Input Validation

**ACL2 Identifiers** (`validate_acl2_identifier`):
- Accepts only plain ACL2 symbols: letters, digits, and the punctuation
  `! $ % & * + - . / : < = > ? @ ^ _ ~`
- Rejects quotes, parentheses, whitespace/newlines, and `;`, so a name cannot
  break out of the surrounding form or inject a top-level command
- Prevents code injection through function/theorem names

**Timeout Values** (`validate_timeout`):
- Clamps to range: 1-300 seconds
- Prevents negative or extreme values
- Handles invalid types gracefully

**Code Length**:
- Maximum 1MB of code per request (both one-off and session modes)
- Applied to all code submission tools
- Prevents resource exhaustion

**Session Names** (`validate_session_name`):
- Maximum 100 characters
- Only alphanumeric, hyphens, underscores, and spaces
- Prevents injection and display issues

**Checkpoint Names** (`validate_checkpoint_name`):
- Maximum 100 characters
- Only alphanumeric, hyphens, and underscores
- Prevents key collision attacks

**Integer Parameters** (`validate_integer_parameter`):
- Validates count, limit, and other integer inputs
- Enforces reasonable bounds to prevent DoS

### 2. String Escaping

**File Paths** (`escape_acl2_string`):
- Escapes backslashes and quotes in file paths
- Prevents breaking out of ACL2 string literals
- Applied to all file paths passed to ACL2

### 3. File Path Validation

**Path Validation** (`validate_file_path`):
- Resolves to absolute paths
- Verifies file exists
- Verifies path is a file (not a directory)
- Error messages only expose filename, not full path

**Current Limitations:**
- Does not restrict access to specific directories
- Allows reading any file the process can access
- Suitable for local single-user usage only

**Output Path Validation** (`validate_output_path`, used by `check_syntax`,
`parinfer`, and `export_session_code` when an output path is given):
- Resolves to absolute paths
- Requires the parent directory to exist
- Rejects directory targets
- Refuses to overwrite an existing file unless `overwrite: true`
- Writes are not confined to an allowed directory, same as reads

### 4. Process Isolation

- ACL2 runs as a separate Jupyter kernel subprocess for each one-off call or
  persistent session
- Timeout enforcement shuts down the kernel process
- No temporary `.lisp` files are created for execution
- The MCP transport's stdin/stdout are never shared with the kernel; the
  kernel's own stdout/stderr go to a log file

### 5. Session Security

**Resource Limits**:
- Maximum 50 concurrent sessions server-wide
- Maximum 50 checkpoints per session
- Sessions auto-terminate after 30 minutes of inactivity

**Timeout Protection**:
- True total timeout (not a per-line read) prevents slow-read attacks
- Commands travel as Jupyter `execute_request` messages; there is no marker or
  prompt string that untrusted code could forge
- A timed-out session command kills that ACL2 kernel, preventing a blocked
  reader from desynchronizing later commands (state is not preserved)
- Timeout applies to both one-off and session commands

**Concurrency Protection**:
- Per-session locks prevent concurrent access
- Race-condition-safe cleanup with session snapshots
- Safe shutdown handles all active sessions

**Kernel stderr handling**:
- The kernel child's stdout/stderr are redirected to a log file
  (`ACL2_MCP_KERNEL_LOG_DIR`, default `/tmp/acl2-mcp-kernel-<kernel>.log`)
- A chatty ACL2 process therefore cannot block on a pipe or corrupt the MCP
  stdio transport

### 6. Error Handling

- Missing ACL2 executables return a clear tool error with the executable path
- Missing required arguments are reported as tool errors, not KeyErrors
- Unknown tools and validation failures return `isError` tool results
- ACL2 failures (failed proofs, rejected events, failed book certification) are
  also reported as `isError`, even though ACL2 writes `ACL2 Error` or
  `******** FAILED ********` rather than `Error:`
- Unexpected internal exceptions are logged server-side and returned generically
- ACL2's own transcript is returned to the caller by design (it is the result)
- File-path errors may include the requested path so the user can correct it

## Tested Attack Vectors

The following attack vectors have been tested and mitigated:

1. **Code Injection via Identifiers**: `name='append")(+ 1 1)'`,
   `name='append\n:pe car'`, `name='append; comment'`
2. **Code Injection via Paths**: `path='test")(malicious-code)'`
3. **Resource Exhaustion**: 2MB code, 1000 second timeout
4. **Path Traversal**: Non-existent files, directories
5. **Argument Abuse**: Missing required fields (`undo {}`), invalid names and
   modes, conflicting `code`/`input_path`, and unwritable output paths

See `tests/test_security.py` for complete test coverage.

## Known Limitations and Risks

### Critical Limitations

**ACL2 Has Full System Access**:
- ACL2's `sys-call` function can execute arbitrary shell commands
- ACL2 can read/write arbitrary files via I/O functions
- No sandboxing is implemented around ACL2 processes
- **Mitigation**: Only use with trusted code and trusted users

**Session Hijacking Risk**:
- Session IDs are UUIDs but anyone with a session ID can access it
- No session ownership or authentication implemented
- Other processes/users could potentially enumerate session IDs
- **Mitigation**: Only run on single-user systems with trusted clients

**No Per-User Resource Limits**:
- One user/client could consume all 50 session slots
- No quotas on checkpoint creation beyond per-session limit
- **Mitigation**: Suitable for single-user local deployment only

**Write-Capable Tools**:
- `check_syntax` and `parinfer` write through `output_path`/`file_path` or
  `in_place`; `export_session_code` writes through `file_path`
- Existing files are only replaced when `overwrite: true` is passed (except
  `in_place`, which explicitly replaces the input file)
- No directory confinement: the server can write anywhere the process can
- **Mitigation**: Only use with trusted clients and users

**Kernel stderr content**:
- Kernel stderr is written to a per-kernel log file, not included in ordinary
  tool results
- Error information printed only to kernel stderr may therefore be lost from
  the result
- **Mitigation**: Inspect the kernel log under `ACL2_MCP_KERNEL_LOG_DIR` when a
  result is unexpectedly incomplete

## Best Practices for Deployment

1. **Run Locally Only**: Prefer stdio. If you use `mcp-proxy` for HTTP, keep it
   bound to localhost and add authentication before exposing it; the server has
   no authentication of its own
2. **Trusted Clients**: Only allow trusted clients (e.g., ZCode)
3. **Single User**: Do not use in multi-user or multi-tenant environments
4. **Trusted Code**: Only execute ACL2 code you trust (ACL2 has full system access)
5. **File Permissions**: Run with minimal necessary file access privileges
6. **Monitor Resources**: Watch for excessive CPU/memory usage
7. **Update Regularly**: Keep ACL2 and Python dependencies updated
8. **Session Hygiene**: End sessions when done to free resources

## Reporting Security Issues

If you discover a security vulnerability, please report it by opening an issue on GitHub with the "security" label. Do not publicly disclose the vulnerability until it has been addressed.

## Security Checklist for Code Review

When reviewing changes to this codebase, verify:

- [ ] All user inputs are validated before use
- [ ] File paths are validated with `validate_file_path()`
- [ ] ACL2 identifiers are validated with `validate_acl2_identifier()`
- [ ] Session names validated with `validate_session_name()`
- [ ] Checkpoint names validated with `validate_checkpoint_name()`
- [ ] Integer parameters validated with `validate_integer_parameter()`
- [ ] Strings interpolated into ACL2 code are escaped with `escape_acl2_string()`
- [ ] Timeouts are validated with `validate_timeout()`
- [ ] Code length checked against `MAX_CODE_LENGTH` (including sessions)
- [ ] No direct string interpolation of user input into ACL2 commands
- [ ] Error messages don't leak sensitive information (catch generic `Exception`)
- [ ] Session operations use locks to prevent race conditions
- [ ] Resource limits enforced (sessions, checkpoints, etc.)
- [ ] Timeout implementation is total, not per-line
- [ ] New functionality has corresponding security tests
