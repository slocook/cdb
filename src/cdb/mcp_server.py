"""
cdb MCP server — exposes cdb debugging commands as MCP tools for Claude.

Architecture: This process (Python 3.10+) handles MCP protocol via the `mcp`
package. It spawns the appropriate backend (cdb.py for lldb, cdb_gdb.py for gdb)
as a subprocess and communicates via line-delimited JSON-RPC over stdin/stdout.

Backend selection (automatic):
  - macOS: uses lldb_backend.py (lldb) with system Python (for lldb bindings)
  - Linux: uses gdb_backend.py (gdb) with the current Python

Override with CDB_BACKEND=lldb|gdb or CDB_SCRIPT=/path/to/backend.py

Registration (Claude Code):
  claude mcp add cdb -- uv run --directory /path/to/cdb python -m cdb.mcp_server
"""

import json
import os
import platform
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Subprocess bridge to backend (cdb.py or cdb_gdb.py)
# ---------------------------------------------------------------------------

_PROJECT_DIR = Path(__file__).parent


def _detect_backend():
    """Pick the right backend script and Python interpreter."""
    explicit = os.environ.get("CDB_BACKEND", "").lower()
    explicit_script = os.environ.get("CDB_SCRIPT")

    if explicit_script:
        # User pointed at a specific script
        script = explicit_script
        python = os.environ.get("CDB_PYTHON", sys.executable)
        return script, python

    if explicit == "gdb" or (not explicit and platform.system() == "Linux"):
        script = str(_PROJECT_DIR / "gdb_backend.py")
        python = os.environ.get("CDB_PYTHON", sys.executable)
        return script, python

    # Default: lldb (macOS). Needs system Python for lldb bindings.
    script = str(_PROJECT_DIR / "lldb_backend.py")
    python = os.environ.get("CDB_PYTHON", "/usr/bin/python3")
    return script, python


_CDB_SCRIPT, _SYSTEM_PYTHON = _detect_backend()


class CdbBridge:
    """Manages a long-running cdb.py subprocess and sends JSON-RPC requests."""

    def __init__(self):
        self._proc = None
        self._lock = threading.Lock()
        self._req_id = 0

    def _ensure_started(self):
        if self._proc is not None and self._proc.poll() is None:
            return
        # Set PYTHONPATH so the subprocess can import the cdb package
        env = os.environ.copy()
        src_dir = str(_PROJECT_DIR.parent)  # src/ directory
        env["PYTHONPATH"] = src_dir + (os.pathsep + env.get("PYTHONPATH", ""))
        self._proc = subprocess.Popen(
            [_SYSTEM_PYTHON, _CDB_SCRIPT, "--server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # line-buffered
            env=env,
        )
        # Read the {"ready": true} announcement
        ready_line = self._proc.stdout.readline()
        if not ready_line:
            stderr = self._proc.stderr.read()
            raise RuntimeError(f"cdb.py failed to start: {stderr}")
        ready = json.loads(ready_line)
        if not ready.get("ready"):
            raise RuntimeError(f"cdb.py sent unexpected startup message: {ready}")

    def call(self, command: str, **params) -> dict:
        with self._lock:
            self._ensure_started()
            self._req_id += 1
            req = {"id": self._req_id, "command": command, **params}
            self._proc.stdin.write(json.dumps(req) + "\n")
            self._proc.stdin.flush()
            line = self._proc.stdout.readline()
            if not line:
                # Process died — collect stderr and report
                stderr = self._proc.stderr.read()
                self._proc = None
                return {
                    "ok": False,
                    "error": "cdb_process_died",
                    "detail": stderr or "cdb.py process exited unexpectedly",
                }
            return json.loads(line)

    def close(self):
        if self._proc and self._proc.poll() is None:
            self._proc.stdin.close()
            self._proc.wait(timeout=5)


_bridge = CdbBridge()


def _run(command: str, **params) -> str:
    result = _bridge.call(command, **params)
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# MCP server definition
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "cdb",
    instructions=(
        "AI-friendly debugger built on lldb/gdb. "
        "Start with cdb_launch, cdb_attach_core, or cdb_attach (PID) to get a session_id. "
        "launch returns a crash_summary automatically — read it before doing anything else. "
        "Use cdb_inspect and cdb_watch to investigate further. "
        "Use cdb_log_point as a printf-debugging replacement (no recompilation needed). "
        "Call cdb_kill_session when done."
    ),
)


@mcp.tool()
def cdb_launch(
    binary: str,
    args: list = [],
    env: dict = {},
    working_dir: str = "",
    stdin: str = "",
    run_to_crash: bool = True,
) -> str:
    """Launch a binary and run it to the first crash or stop.

    Returns a session_id and, if the process crashed or hit a breakpoint,
    a full crash_summary with backtrace, crashing frame locals, and watchpoint suggestions.
    Pass run_to_crash=False to set breakpoints before launching (then call cdb_go).
    """
    params = {"binary": binary, "args": args, "env": env, "run_to_crash": run_to_crash}
    if working_dir:
        params["working_dir"] = working_dir
    if stdin:
        params["stdin"] = stdin
    return _run("launch", **params)


@mcp.tool()
def cdb_attach_core(binary: str, core: str) -> str:
    """Load a core dump for post-mortem analysis.

    Returns a session_id and immediate crash_summary — no need to run anything.
    This is the primary workflow for CI crash triage.
    """
    return _run("attach_core", binary=binary, core=core)


@mcp.tool()
def cdb_attach(pid: int) -> str:
    """Attach to a running process by PID.

    The process is stopped on attach. Returns session_id and crash_summary with
    current backtrace and locals. When the session is killed, the process is
    detached (not terminated) so it can continue running.
    """
    return _run("attach", pid=pid)


@mcp.tool()
def cdb_crash_summary(session_id: str) -> str:
    """Get a full structured crash report for the current stop.

    Includes: stop_reason, signal, crash_address, interpretation, crashing_frame
    with locals, full backtrace, and suggested watchpoints.
    Already called automatically by cdb_launch and cdb_go on any stop.
    """
    return _run("crash_summary", session_id=session_id)


@mcp.tool()
def cdb_backtrace(session_id: str, all_threads: bool = False) -> str:
    """Get the call stack for the stopped process.

    Set all_threads=True to see stacks for every thread.
    Each frame includes function name, file, line, and module.
    Frame 0 (top) also includes local variables.
    """
    return _run("backtrace", session_id=session_id, all_threads=all_threads)


@mcp.tool()
def cdb_inspect(session_id: str, expr: str, frame: int = 0) -> str:
    """Evaluate a native expression and return its typed value.

    expr can be any valid expression in the current frame: variable names,
    pointer dereferences (*ptr), casts, struct field access (obj.field), etc.
    Use frame=N to evaluate in a specific backtrace frame if out of scope at frame 0.
    Returns type, value, address (for pointers), null status, and children (for structs).
    """
    return _run("inspect", session_id=session_id, expr=expr, frame=frame)


@mcp.tool()
def cdb_watch(
    session_id: str,
    expr: str,
    read: bool = False,
    write: bool = True,
    frame: int = 0,
) -> str:
    """Set a watchpoint on a variable or expression by name (not address).

    The tool resolves the address and size automatically.
    write=True (default) triggers when the value is written.
    read=True triggers on reads (expensive — use sparingly).
    Returns a watchpoint_id. Hardware limit is 4 watchpoints on x86/arm64.
    After setting, call cdb_go to run until the watchpoint fires.
    """
    return _run("watch", session_id=session_id, expr=expr, read=read, write=write, frame=frame)


@mcp.tool()
def cdb_unwatch(session_id: str, watchpoint_id: int) -> str:
    """Remove a watchpoint by its ID (returned by cdb_watch)."""
    return _run("unwatch", session_id=session_id, watchpoint_id=watchpoint_id)


@mcp.tool()
def cdb_break_at(session_id: str, location: str, condition: str = "") -> str:
    """Set a breakpoint, optionally with a condition.

    location formats:
      "main"              - function name
      "MyClass::method"   - C++ method
      "foo.cpp:42"        - file and line number
    condition: native expression that must be true for the breakpoint to fire (e.g. "i > 100").
    Returns a breakpoint_id and number of resolved locations.
    Call cdb_go after setting breakpoints to run to them.
    """
    params = {"session_id": session_id, "location": location}
    if condition:
        params["condition"] = condition
    return _run("break_at", **params)


@mcp.tool()
def cdb_log_point(
    session_id: str,
    location: str,
    exprs: list = [],
    condition: str = "",
    label: str = "",
) -> str:
    """Set a log point — a breakpoint that captures values and auto-continues.

    This is a printf-debugging replacement that requires no recompilation.
    The process never stops at a log point; values are collected silently.

    location: where to log (same formats as cdb_break_at)
    exprs: list of native expressions to evaluate (e.g. ["x", "buf->len"]).
           If empty, captures all local variables automatically.
    condition: only log when this native expression is true (e.g. "i > 100")
    label: optional name for this log point (for filtering in get_logs)

    After setting, call cdb_go to run. Then call cdb_get_logs to retrieve entries.
    """
    params = {"session_id": session_id, "location": location, "exprs": exprs}
    if condition:
        params["condition"] = condition
    if label:
        params["label"] = label
    return _run("log_point", **params)


@mcp.tool()
def cdb_get_logs(session_id: str, log_point_id: int = 0) -> str:
    """Retrieve log entries collected by log points.

    Returns all entries, or filter by log_point_id (returned by cdb_log_point).
    Each entry contains: hit number, log_point_id, label, location, and
    a values dict mapping expression names to their evaluated string values.
    """
    params = {"session_id": session_id}
    if log_point_id:
        params["log_point_id"] = log_point_id
    return _run("get_logs", **params)


@mcp.tool()
def cdb_go(session_id: str) -> str:
    """Continue execution until the next stop (breakpoint, watchpoint, crash, or exit).

    If the process hasn't been launched yet (run_to_crash=False), this launches it.
    Returns process_state and, on any stop, a full crash_summary.
    On exit, returns exit_code.
    """
    return _run("go", session_id=session_id)


@mcp.tool()
def cdb_step(session_id: str, kind: str = "over") -> str:
    """Step execution.

    kind: "over" (step over function calls), "into" (step into calls), "out" (step out of current function)
    Returns the new location after stepping.
    """
    return _run("step", session_id=session_id, kind=kind)


@mcp.tool()
def cdb_state(session_id: str) -> str:
    """Get the current session state.

    Returns: process_state, current source location, all active breakpoints and watchpoints.
    Useful for orientation after a series of steps or to audit what's set up.
    """
    return _run("state", session_id=session_id)


@mcp.tool()
def cdb_sessions() -> str:
    """List all active debug sessions with their process state."""
    return _run("sessions")


@mcp.tool()
def cdb_kill_session(session_id: str) -> str:
    """Kill the process and destroy the session. Always call this when done."""
    return _run("kill_session", session_id=session_id)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
