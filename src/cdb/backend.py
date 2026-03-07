"""
cdb backend interface — defines the contract between the protocol layer and
a debugger backend (lldb, gdb, etc.).

Each backend implements the Backend class. The protocol layer (server/MCP)
calls these methods and gets back plain dicts — no debugger-specific types
leak out.
"""

import os
import sys
import json
import uuid
import traceback as _traceback


class Session:
    """Base session — backends extend this with their own state."""

    def __init__(self, session_id, binary):
        self.session_id = session_id
        self.binary = binary
        self.attached = False       # True if we attached to a running process
        self.watchpoints = {}       # id -> description string
        self.breakpoints = {}       # id -> location string
        self.log_points = {}        # log_point_id -> {location, exprs, condition, label}
        self.log_entries = []       # [{hit, log_point_id, label, location, values}]
        self._next_log_point_id = 1

    def next_log_point_id(self):
        lp_id = self._next_log_point_id
        self._next_log_point_id += 1
        return lp_id

    def destroy(self):
        raise NotImplementedError


class Backend:
    """Abstract debugger backend. All methods return plain dicts."""

    def __init__(self):
        self._sessions = {}

    def _new_session_id(self):
        return str(uuid.uuid4())[:8]

    def _get_session(self, params):
        sid = params.get("session_id")
        if not sid:
            return {"ok": False, "error": "missing_param", "detail": "session_id is required"}
        s = self._sessions.get(sid)
        if not s:
            return {
                "ok": False,
                "error": "session_not_found",
                "detail": f"No session '{sid}'",
                "suggestion": "Use 'sessions' to list active sessions or 'launch' to create one",
            }
        return s

    # -- Commands that subclasses must implement --

    def cmd_launch(self, params):
        raise NotImplementedError

    def cmd_attach_core(self, params):
        raise NotImplementedError

    def cmd_crash_summary(self, params):
        raise NotImplementedError

    def cmd_backtrace(self, params):
        raise NotImplementedError

    def cmd_inspect(self, params):
        raise NotImplementedError

    def cmd_watch(self, params):
        raise NotImplementedError

    def cmd_unwatch(self, params):
        raise NotImplementedError

    def cmd_break_at(self, params):
        raise NotImplementedError

    def cmd_go(self, params):
        raise NotImplementedError

    def cmd_step(self, params):
        raise NotImplementedError

    def cmd_attach(self, params):
        raise NotImplementedError

    def cmd_state(self, params):
        raise NotImplementedError

    def cmd_select_thread(self, params):
        raise NotImplementedError

    def cmd_delete_breakpoint(self, params):
        raise NotImplementedError

    def cmd_disable_breakpoint(self, params):
        raise NotImplementedError

    def cmd_enable_breakpoint(self, params):
        raise NotImplementedError

    def cmd_log_point(self, params):
        raise NotImplementedError

    def cmd_get_logs(self, params):
        raise NotImplementedError

    # -- Commands with shared implementations --

    def cmd_sessions(self, params):
        return {
            "ok": True,
            "sessions": [
                {
                    "session_id": sid,
                    "binary": s.binary,
                    "process_state": self._get_process_state(s),
                }
                for sid, s in self._sessions.items()
            ],
        }

    def cmd_kill_session(self, params):
        s = self._get_session(params)
        if isinstance(s, dict):
            return s
        sid = s.session_id
        s.destroy()
        del self._sessions[sid]
        return {"ok": True, "session_id": sid}

    def _get_process_state(self, session):
        """Return a string process state. Subclasses should override."""
        return "unknown"

    # -- Dispatch --

    def handle_request(self, req):
        if not isinstance(req, dict):
            return {"ok": False, "error": "invalid_request", "detail": "Request must be a JSON object"}

        cmd = req.get("command")
        req_id = req.get("id")

        if not cmd:
            return {"ok": False, "id": req_id, "error": "missing_command"}

        handler = self._commands().get(cmd)
        if not handler:
            return {
                "ok": False,
                "id": req_id,
                "error": "unknown_command",
                "detail": f"Unknown command '{cmd}'",
                "available_commands": list(self._commands()),
            }

        try:
            result = handler(req)
            if req_id is not None:
                result["id"] = req_id
            return result
        except Exception as e:
            return {
                "ok": False,
                "id": req_id,
                "error": "internal_error",
                "detail": str(e),
                "traceback": _traceback.format_exc(),
            }

    def _commands(self):
        return {
            "launch": self.cmd_launch,
            "attach_core": self.cmd_attach_core,
            "crash_summary": self.cmd_crash_summary,
            "backtrace": self.cmd_backtrace,
            "inspect": self.cmd_inspect,
            "watch": self.cmd_watch,
            "unwatch": self.cmd_unwatch,
            "break_at": self.cmd_break_at,
            "go": self.cmd_go,
            "step": self.cmd_step,
            "attach": self.cmd_attach,
            "select_thread": self.cmd_select_thread,
            "delete_breakpoint": self.cmd_delete_breakpoint,
            "disable_breakpoint": self.cmd_disable_breakpoint,
            "enable_breakpoint": self.cmd_enable_breakpoint,
            "state": self.cmd_state,
            "log_point": self.cmd_log_point,
            "get_logs": self.cmd_get_logs,
            "sessions": self.cmd_sessions,
            "kill_session": self.cmd_kill_session,
        }


# ---------------------------------------------------------------------------
# Server entry point (shared by all backends)
# ---------------------------------------------------------------------------

def run_server(backend):
    """JSON-RPC server: one JSON object per line in, one JSON object per line out."""
    print(json.dumps({"ready": True, "commands": list(backend._commands())}), flush=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            print(json.dumps({"ok": False, "error": "json_parse_error", "detail": str(e)}), flush=True)
            continue
        print(json.dumps(backend.handle_request(req)), flush=True)


def run_cli(backend, binary, args):
    """One-shot crash analysis."""
    result = backend.handle_request({"command": "launch", "binary": binary, "args": args, "run_to_crash": True})
    print(json.dumps(result, indent=2))
