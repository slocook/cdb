# cdb

AI-friendly debugger built on lldb/gdb. Designed to be the **first** tool an AI agent
reaches for when investigating a crash in a compiled binary — not grep, not reading source files.

One command gets you from "I have a binary" to a structured crash report with the
crashing frame, local variables, and watchpoint suggestions. No text parsing, no
syntax memorization, no multi-step setup.

## Requirements

- macOS with Xcode or Command Line Tools (`xcode-select --install`), or Linux with `gdb`
- [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`)

lldb/gdb are discovered automatically at runtime — no pip install needed.

## Setup (fresh clone)

```bash
git clone <repo-url> cdb && cd cdb

# That's it. No install step needed.
# uv creates the virtualenv and installs dependencies on first run.
```

## Quick start

```bash
# One-shot crash analysis (CLI)
uv run cdb-mcp              # start the MCP server (agents connect to this)
PYTHONPATH=src python3 -m cdb.lldb_backend /path/to/binary   # direct lldb (no MCP)
PYTHONPATH=src python3 -m cdb.gdb_backend  /path/to/binary   # direct gdb  (no MCP)
```

## Architecture

```
                           ┌─────────────────────────┐
MCP client (Claude)  ────▶│  mcp_server.py           │ Python 3.12 (uv)
                           │  (MCP protocol)          │
                           └────────┬────────────────-┘
                                    │ JSON-RPC stdin/stdout
                           ┌────────▼─────────────────┐
                           │  lldb_backend.py (macOS)  │ system Python 3.9
                           │  gdb_backend.py  (Linux)  │ any Python 3.9+
                           └────────┬─────────────────-┘
                                    │ lldb SB API / GDB MI
                                    ▼
                           your binary / core dump
```

**Why two Pythons?** The lldb Python bindings (`_lldb.cpython-39-darwin.so`) are
ABI-locked to the system Python 3.9 and can't load in Python 3.12+. The `mcp`
package requires Python 3.10+. The subprocess bridge solves this — the MCP server
spawns the backend using the system Python and communicates over JSON-RPC.

The GDB backend doesn't have this constraint — it talks to GDB via the MI
subprocess protocol, so it works with any Python version.

Override the backend with `CDB_BACKEND=lldb|gdb` or `CDB_PYTHON=/path/to/python3`.

The backends also work standalone — any agent framework can spawn them as a
subprocess and talk over line-delimited JSON without needing MCP.

## Integration with Claude Code

### One-line install (no checkout required)

```bash
curl -fsSL https://raw.githubusercontent.com/slocook/cdb/main/install.sh | bash
```

This clones the repo to `~/.local/share/cdb`, registers the MCP server, and
installs the skill. Set `CDB_HOME` to change the install location.

### Install from a local checkout

```bash
bash install.sh
```

Same thing, but uses the current directory instead of cloning.

Both methods register the MCP server (`--scope user`, available in all projects)
and install the skill to `~/.claude/skills/cdb/SKILL.md`.

### Manual setup

```bash
# Register MCP server
claude mcp add --scope user cdb -- \
  uv run --directory /absolute/path/to/cdb cdb-mcp

# Install skill (optional — teaches Claude to reach for cdb first)
mkdir -p ~/.claude/skills/cdb
cp /path/to/cdb/SKILL.md ~/.claude/skills/cdb/SKILL.md
```

`uv run` handles virtualenv creation, dependency installation, and Python
version management automatically on the first call.

### Verify setup

```bash
# List registered MCP servers
claude mcp list

# Inside Claude Code, check server status
/mcp
```

---

## JSON-RPC protocol (server mode)

One JSON object per line in, one JSON object per line out.

**Startup:** server emits `{"ready": true, "commands": [...]}` immediately.

**Request:**
```json
{"id": 1, "command": "launch", "binary": "/path/to/binary", "args": []}
```

**Success response:**
```json
{"id": 1, "ok": true, "session_id": "a1b2c3d4", "process_state": "stopped", "crash_summary": {...}}
```

**Error response:**
```json
{"id": 1, "ok": false, "error": "binary_not_found", "detail": "No file at: /path/to/binary", "suggestion": "Provide the full path to a compiled executable"}
```

All errors include a `"suggestion"` field where actionable next steps exist.

---

## Commands

### `launch` — Start a process and run to first stop

```json
{
  "command": "launch",
  "binary": "/path/to/binary",
  "args": ["--flag", "value"],
  "env": {"MY_VAR": "1"},
  "working_dir": "/path/to/workdir",
  "stdin": "/path/to/input/file",
  "run_to_crash": true
}
```

`run_to_crash: true` (default) — launches immediately and returns on any stop.
`run_to_crash: false` — creates the session without launching; set breakpoints first, then call `go`.

**Response includes `crash_summary` automatically on any stop.**

---

### `attach_core` — Load a core dump

```json
{"command": "attach_core", "binary": "/path/to/binary", "core": "/path/to/core"}
```

Returns `session_id` and `crash_summary` immediately. No need to run anything.
Primary workflow for CI crash triage.

---

### `attach` — Attach to a running process

```json
{"command": "attach", "pid": 12345}
```

Stops the process on attach and returns `session_id` + `crash_summary` with the
current backtrace and locals. When `kill_session` is called, the process is
**detached** (not killed) so it can continue running.

---

### `crash_summary` — Full structured crash report

```json
{"command": "crash_summary", "session_id": "a1b2c3d4"}
```

Returns:
```json
{
  "ok": true,
  "process_state": "stopped",
  "stop_reason": "exception",
  "signal": null,
  "crash_address": "0x0",
  "interpretation": "null pointer dereference (EXC_BAD_ACCESS at address 0x0)",
  "stop_description": "EXC_BAD_ACCESS (code=1, address=0x0)",
  "crashing_frame": {
    "index": 0,
    "function": "Node::print()",
    "file": "crash_null.cpp",
    "line": 9,
    "locals": [
      {"name": "this", "type": "Node *", "is_null": true, "address": "0x0"}
    ]
  },
  "backtrace": [...],
  "num_threads": 1,
  "suggested_watchpoints": [
    {
      "expr": "this",
      "reason": "this is null at crash site",
      "suggestion": "Set a watchpoint in a caller frame where this is assigned"
    }
  ]
}
```

`crash_summary` is returned automatically by `launch` and `go` on any stop.

---

### `backtrace` — Call stack

```json
{"command": "backtrace", "session_id": "a1b2c3d4"}
{"command": "backtrace", "session_id": "a1b2c3d4", "all_threads": true}
```

Returns structured frames. Frame 0 includes locals. Each frame has:
`index`, `function`, `file`, `file_path`, `line`, `column`, `module`, `pc`

---

### `inspect` — Evaluate an expression

```json
{"command": "inspect", "session_id": "a1b2c3d4", "expr": "config->size"}
{"command": "inspect", "session_id": "a1b2c3d4", "expr": "*(MyStruct*)ptr", "frame": 2}
```

Returns typed value. For pointers: `address`, `is_null`, and dereferenced value.
For structs: `children` array (capped at 24 per level, 3 levels deep).

On failure, returns `"error"` + `"suggestion"` (e.g., wrong scope → which frame to use).

---

### `watch` — Watchpoint by expression name

```json
{"command": "watch", "session_id": "a1b2c3d4", "expr": "config->refcount"}
{"command": "watch", "session_id": "a1b2c3d4", "expr": "buf", "read": true, "write": true}
```

Resolves the address and size automatically — no manual `&var` or size calculation.
Hardware limit: 4 watchpoints (x86/arm64). Returns `watchpoint_id`.
After setting, call `go` to run until the watchpoint fires.

---

### `unwatch` — Remove a watchpoint

```json
{"command": "unwatch", "session_id": "a1b2c3d4", "watchpoint_id": 1}
```

---

### `break_at` — Set a breakpoint

```json
{"command": "break_at", "session_id": "a1b2c3d4", "location": "MyClass::method"}
{"command": "break_at", "session_id": "a1b2c3d4", "location": "foo.cpp:42", "condition": "i > 100"}
```

Optional `condition`: native expression (e.g. C/C++) — breakpoint only fires when true.
Returns `breakpoint_id` and `num_locations` (0 means unresolved — check spelling).

---

### `log_point` — Printf-debugging without recompilation

```json
{
  "command": "log_point",
  "session_id": "a1b2c3d4",
  "location": "parser.cpp:142",
  "exprs": ["token.type", "token.value", "parser_state"],
  "condition": "token.type == 3",
  "label": "parser-entry"
}
```

Sets a breakpoint that evaluates expressions and auto-continues — the process
never stops. If `exprs` is empty, all local variables are captured automatically.
Returns a `log_point_id`. Use `get_logs` to retrieve collected entries.

---

### `get_logs` — Retrieve log point entries

```json
{"command": "get_logs", "session_id": "a1b2c3d4"}
{"command": "get_logs", "session_id": "a1b2c3d4", "log_point_id": 1}
```

Returns collected log entries:
```json
{
  "ok": true,
  "entries": [
    {"hit": 1, "log_point_id": 1, "label": "parser-entry", "location": "parser.cpp:142",
     "values": {"token.type": "3", "token.value": "\"foo\"", "parser_state": "0x2"}}
  ],
  "count": 1
}
```

---

### `go` — Continue to next stop

```json
{"command": "go", "session_id": "a1b2c3d4"}
```

If process not yet launched (`run_to_crash: false`), this launches it.
Returns `process_state` and `crash_summary` on any stop, or `exit_code` on exit.

---

### `step` — Step execution

```json
{"command": "step", "session_id": "a1b2c3d4", "kind": "over"}
```

`kind`: `"over"` (default), `"into"`, `"out"`. Returns new source location.

---

### `state` — Session overview

```json
{"command": "state", "session_id": "a1b2c3d4"}
```

Returns `process_state`, `current_location`, active `breakpoints`, active `watchpoints`.

---

### `sessions` — List all sessions

```json
{"command": "sessions"}
```

---

### `kill_session` — Destroy a session

```json
{"command": "kill_session", "session_id": "a1b2c3d4"}
```

Kills the process and frees all resources. Always call when done.

---

## Common workflows

### Crash triage (live binary)

```
launch → read crash_summary → inspect locals → kill_session
```

### Crash triage (core dump)

```
attach_core → read crash_summary → inspect locals → kill_session
```

### Watchpoint trace (finding who writes a variable)

```
launch (run_to_crash=false) → break_at entry point → go → watch expr → go (repeating) → crash_summary → kill_session
```

### Printf-debugging (no recompilation)

```
launch (run_to_crash=false) → log_point location exprs → go → get_logs → kill_session
```

### Attach to running process

```
attach pid → inspect locals → kill_session (detaches, process continues)
```

### Step-through debugging

```
launch (run_to_crash=false) → break_at function → go → inspect → step (repeating) → kill_session
```

