#!/usr/bin/env python3
"""
cdb - AI-friendly debugger — gdb backend (GDB/MI protocol)

Uses GDB's Machine Interface (MI) over subprocess, so no Python version
constraints — any Python 3.9+ works. GDB does not need a Python API module.

Server mode:
  python3 cdb_gdb.py [--server]

CLI mode:
  python3 cdb_gdb.py <binary> [args...]
"""

import sys
import os
import re
import json
import signal as _signal
import subprocess
import threading

from cdb.backend import Session, Backend, run_server, run_cli


# ---------------------------------------------------------------------------
# GDB/MI subprocess wrapper
# ---------------------------------------------------------------------------

class GdbMI:
    """Talks to a GDB process via the MI protocol."""

    def __init__(self, gdb_path="gdb"):
        self.gdb_path = gdb_path
        self._proc = None
        self._lock = threading.Lock()
        self._token = 0

    def start(self, binary=None):
        cmd = [self.gdb_path, "--interpreter=mi3", "-q"]
        if binary:
            cmd.append(binary)
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        # Drain startup output until we see the (gdb) prompt
        self._read_until_prompt()

    def command(self, cmd):
        """Send a MI command, return list of output records."""
        with self._lock:
            self._token += 1
            token = self._token
            full_cmd = f"{token}{cmd}\n"
            self._proc.stdin.write(full_cmd)
            self._proc.stdin.flush()
            return self._read_until_result(token)

    def _read_until_prompt(self):
        lines = []
        while True:
            line = self._proc.stdout.readline()
            if not line:
                break
            line = line.rstrip("\n")
            lines.append(line)
            if line == "(gdb)":
                break
        return lines

    def _read_until_result(self, token):
        """Read MI output until we get the result record for our token."""
        records = []
        result = None
        token_str = str(token)
        while True:
            line = self._proc.stdout.readline()
            if not line:
                break
            line = line.rstrip("\n")
            if line == "(gdb)":
                if result is not None:
                    break
                continue
            records.append(line)
            # Result record starts with our token number
            if line.startswith(token_str + "^"):
                result = line
        return {"result": result, "records": records}

    def kill(self):
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.stdin.write("-gdb-exit\n")
                self._proc.stdin.flush()
                self._proc.wait(timeout=3)
            except Exception:
                self._proc.kill()


# ---------------------------------------------------------------------------
# MI output parsing
# ---------------------------------------------------------------------------

def _parse_mi_value(s, pos=0):
    """Parse a GDB/MI value (string, tuple, or list) starting at pos.
    Returns (value, next_pos)."""
    if pos >= len(s):
        return None, pos

    if s[pos] == '"':
        # C string
        end = pos + 1
        while end < len(s):
            if s[end] == '\\':
                end += 2
            elif s[end] == '"':
                break
            else:
                end += 1
        val = s[pos + 1:end].replace('\\n', '\n').replace('\\t', '\t').replace('\\"', '"').replace('\\\\', '\\')
        return val, end + 1

    if s[pos] == '{':
        # Tuple → dict
        return _parse_mi_tuple(s, pos)

    if s[pos] == '[':
        # List
        return _parse_mi_list(s, pos)

    return None, pos


def _parse_mi_tuple(s, pos=0):
    """Parse {key=val,key=val,...}"""
    if s[pos] != '{':
        return None, pos
    pos += 1
    result = {}
    while pos < len(s) and s[pos] != '}':
        if s[pos] in (',', ' '):
            pos += 1
            continue
        # key=value
        eq = s.index('=', pos)
        key = s[pos:eq]
        val, pos = _parse_mi_value(s, eq + 1)
        result[key] = val
    return result, pos + 1  # skip }


def _parse_mi_list(s, pos=0):
    """Parse [val,val,...] or [key=val,key=val,...]"""
    if s[pos] != '[':
        return None, pos
    pos += 1
    result = []
    while pos < len(s) and s[pos] != ']':
        if s[pos] in (',', ' '):
            pos += 1
            continue
        # Check if it's key=value (list of results) or just value
        eq_pos = s.find('=', pos)
        bracket_pos = s.find('{', pos)
        quote_pos = s.find('"', pos)
        if eq_pos != -1 and (bracket_pos == -1 or eq_pos < bracket_pos) and (quote_pos == -1 or eq_pos < quote_pos):
            # key=value
            key = s[pos:eq_pos]
            val, pos = _parse_mi_value(s, eq_pos + 1)
            result.append({key: val})
        else:
            val, pos = _parse_mi_value(s, pos)
            result.append(val)
    return result, pos + 1


def _parse_mi_record(line):
    """Parse a MI result/async record into (class, dict).
    E.g. '42^done,bkpt={...}' → ('done', {'bkpt': {...}})
    """
    # Strip token prefix
    m = re.match(r'^\d*[\^*+=~@&]', line)
    if not m:
        return None, {}
    rest = line[m.end():]
    # Split class from results
    parts = rest.split(',', 1)
    cls = parts[0]
    if len(parts) == 1:
        return cls, {}
    # Parse remaining key=value pairs
    kvs = {}
    remaining = parts[1]
    pos = 0
    while pos < len(remaining):
        if remaining[pos] in (' ', ','):
            pos += 1
            continue
        eq = remaining.find('=', pos)
        if eq == -1:
            break
        key = remaining[pos:eq]
        val, pos = _parse_mi_value(remaining, eq + 1)
        kvs[key] = val
    return cls, kvs


def _mi_result_ok(response):
    """Check if a MI response was successful."""
    if not response or not response.get("result"):
        return False
    return "^done" in response["result"] or "^running" in response["result"]


# ---------------------------------------------------------------------------
# GDB session
# ---------------------------------------------------------------------------

class GdbSession(Session):
    def __init__(self, session_id, binary, gdb_path="gdb"):
        super().__init__(session_id, binary)
        self.mi = GdbMI(gdb_path)
        self.mi.start(binary if binary else None)
        self.running = False
        self.exited = False
        self.exit_code = None
        self.stop_reason = None
        self.stop_signal = None
        self.stop_records = []
        self._log_bp_ids = {}  # breakpoint_id -> log_point_id

    def destroy(self):
        if self.attached:
            self.mi.command("-target-detach")
        self.mi.kill()


# ---------------------------------------------------------------------------
# GDB backend
# ---------------------------------------------------------------------------

class GdbBackend(Backend):

    def __init__(self, gdb_path=None):
        super().__init__()
        self.gdb_path = gdb_path or os.environ.get("CDB_GDB", "gdb")

    def _get_process_state(self, session):
        if session.exited:
            return "exited"
        if session.running:
            return "running"
        if session.stop_reason:
            return "stopped"
        return "not_launched"

    def _require_stopped(self, session):
        if session.exited:
            return {"ok": False, "error": "process_exited", "detail": f"Process exited with code {session.exit_code}"}
        if session.running:
            return {"ok": False, "error": "process_running", "detail": "Process is running", "suggestion": "Wait for it to stop or interrupt it"}
        if not session.stop_reason:
            return {"ok": False, "error": "no_process", "detail": "Process not launched yet"}
        return None

    def _process_stop(self, session, response):
        """Parse stop records from a MI response to update session state."""
        for rec in response.get("records", []):
            if rec.startswith("*stopped"):
                _, kvs = _parse_mi_record(rec)
                session.stop_reason = kvs.get("reason", "unknown")
                session.stop_records = [rec]
                session.running = False
                sig = kvs.get("signal-name")
                if sig:
                    session.stop_signal = sig
                if session.stop_reason == "exited-normally":
                    session.exited = True
                    session.exit_code = 0
                elif session.stop_reason == "exited":
                    session.exited = True
                    session.exit_code = int(kvs.get("exit-code", "0"), 8)  # MI uses octal
                elif session.stop_reason == "exited-signalled":
                    session.exited = True
                    session.exit_code = -1
                return kvs
            elif rec.startswith("*running"):
                session.running = True
        return None

    def _get_backtrace(self, session):
        resp = session.mi.command("-stack-list-frames")
        if not _mi_result_ok(resp):
            return []
        _, kvs = _parse_mi_record(resp["result"])
        stack = kvs.get("stack", [])
        frames = []
        for item in stack:
            f = item.get("frame", item) if isinstance(item, dict) else {}
            frames.append({
                "index": int(f.get("level", 0)),
                "pc": f.get("addr", "??"),
                "function": f.get("func", "??"),
                "file": f.get("file"),
                "file_path": f.get("fullname"),
                "line": int(f.get("line", 0)) if f.get("line") else None,
            })
        return frames

    def _get_locals(self, session, frame_idx=0):
        session.mi.command(f"-stack-select-frame {frame_idx}")
        resp = session.mi.command("-stack-list-variables --all-values")
        if not _mi_result_ok(resp):
            return []
        _, kvs = _parse_mi_record(resp["result"])
        variables = kvs.get("variables", [])
        result = []
        for v in variables:
            if isinstance(v, dict):
                var = v.get("variable", v)  # unwrap if needed
                if isinstance(var, dict):
                    result.append({
                        "name": var.get("name"),
                        "type": var.get("type"),
                        "value": var.get("value"),
                        "summary": None,
                    })
        return result

    def _interpret_stop(self, session, stop_kvs=None):
        reason = session.stop_reason
        if reason == "signal-received":
            sig = session.stop_signal or "unknown"
            if sig == "SIGSEGV":
                return "null pointer dereference or invalid memory access (SIGSEGV)"
            if sig == "SIGABRT":
                return "process aborted — likely assert() failure or std::terminate()"
            if sig == "SIGBUS":
                return "bus error: misaligned memory access"
            if sig == "SIGFPE":
                return "arithmetic exception: division by zero or integer overflow"
            return f"stopped by signal {sig}"
        if reason == "breakpoint-hit":
            return "stopped at breakpoint"
        if reason == "watchpoint-trigger":
            return "watchpoint triggered"
        if reason == "end-stepping-range":
            return "step completed"
        return reason

    def _build_crash_summary(self, session, stop_kvs=None):
        frames = self._get_backtrace(session)
        top = frames[0] if frames else None

        locals_ = self._get_locals(session, 0) if top else []
        if top:
            top["locals"] = locals_

        return {
            "process_state": "stopped",
            "stop_reason": session.stop_reason or "unknown",
            "signal": session.stop_signal,
            "crash_address": None,
            "stop_description": session.stop_reason,
            "interpretation": self._interpret_stop(session, stop_kvs),
            "crashing_frame": top,
            "backtrace": frames,
            "num_threads": 1,  # TODO: query thread count
            "suggested_watchpoints": [],
        }

    def cmd_launch(self, params):
        binary = params.get("binary")
        if not binary:
            return {"ok": False, "error": "missing_param", "detail": "binary is required"}
        if not os.path.isfile(binary):
            return {"ok": False, "error": "binary_not_found", "detail": f"No file at: {binary}",
                    "suggestion": "Provide the full path to a compiled executable"}

        args = list(params.get("args", []))
        run_to_crash = params.get("run_to_crash", True)

        sid = self._new_session_id()
        session = GdbSession(sid, binary, self.gdb_path)
        self._sessions[sid] = session

        if args:
            args_str = " ".join(args)
            session.mi.command(f'-exec-arguments {args_str}')

        if not run_to_crash:
            return {
                "ok": True, "session_id": sid, "process_state": "ready",
                "note": "Process not launched yet. Set breakpoints with 'break_at', then call 'go'.",
            }

        resp = session.mi.command("-exec-run")
        stop_kvs = self._process_stop(session, resp)

        if session.exited:
            return {
                "ok": True, "session_id": sid, "process_state": "exited",
                "exit_code": session.exit_code,
            }

        result = {"ok": True, "session_id": sid, "process_state": "stopped"}
        result["crash_summary"] = self._build_crash_summary(session, stop_kvs)
        return result

    def cmd_attach_core(self, params):
        binary = params.get("binary")
        core = params.get("core")
        if not binary or not core:
            return {"ok": False, "error": "missing_param", "detail": "binary and core are both required"}

        sid = self._new_session_id()
        session = GdbSession(sid, binary, self.gdb_path)
        self._sessions[sid] = session

        resp = session.mi.command(f'-target-select core {core}')
        if not _mi_result_ok(resp):
            return {"ok": False, "error": "core_load_failed", "detail": f"Failed to load core: {core}"}

        session.stop_reason = "core-dump"
        return {
            "ok": True, "session_id": sid, "process_state": "stopped",
            "crash_summary": self._build_crash_summary(session),
        }

    def cmd_attach(self, params):
        pid = params.get("pid")
        if not pid:
            return {"ok": False, "error": "missing_param", "detail": "pid is required"}
        pid = int(pid)

        sid = self._new_session_id()
        session = GdbSession(sid, "", self.gdb_path)
        session.attached = True
        self._sessions[sid] = session

        resp = session.mi.command(f"-target-attach {pid}")
        if not _mi_result_ok(resp):
            _, kvs = _parse_mi_record(resp["result"]) if resp.get("result") else ("", {})
            del self._sessions[sid]
            session.destroy()
            return {"ok": False, "error": "attach_failed",
                    "detail": kvs.get("msg", f"Failed to attach to PID {pid}"),
                    "suggestion": "Check that the PID is valid and you have permission to attach."}

        self._process_stop(session, resp)
        session.stop_reason = session.stop_reason or "attached"

        result = {"ok": True, "session_id": sid, "process_state": "stopped"}
        result["crash_summary"] = self._build_crash_summary(session)
        return result

    def cmd_crash_summary(self, params):
        s = self._get_session(params)
        if isinstance(s, dict):
            return s
        err = self._require_stopped(s)
        if err:
            return err
        return {"ok": True, **self._build_crash_summary(s)}

    def cmd_backtrace(self, params):
        s = self._get_session(params)
        if isinstance(s, dict):
            return s
        err = self._require_stopped(s)
        if err:
            return err

        frames = self._get_backtrace(s)
        return {"ok": True, "frames": frames}

    def cmd_inspect(self, params):
        s = self._get_session(params)
        if isinstance(s, dict):
            return s
        err = self._require_stopped(s)
        if err:
            return err

        expr = params.get("expr")
        if not expr:
            return {"ok": False, "error": "missing_param", "detail": "expr is required"}

        frame_idx = params.get("frame", 0)
        s.mi.command(f"-stack-select-frame {frame_idx}")
        resp = s.mi.command(f'-data-evaluate-expression {expr}')
        if not _mi_result_ok(resp):
            _, kvs = _parse_mi_record(resp["result"])
            detail = kvs.get("msg", "Expression evaluation failed")
            suggestion = None
            if "not in scope" in detail.lower() or "No symbol" in detail:
                suggestion = f"'{expr}' is not in scope at frame {frame_idx}. Try a different frame."
            elif "optimized out" in detail.lower():
                suggestion = "Variable was optimized out. Rebuild with -O0 -g."
            return {"ok": False, "error": "expression_failed", "detail": detail, "suggestion": suggestion}

        _, kvs = _parse_mi_record(resp["result"])
        return {
            "ok": True, "expr": expr,
            "result": {"name": expr, "type": None, "value": kvs.get("value"), "summary": None},
        }

    def cmd_watch(self, params):
        s = self._get_session(params)
        if isinstance(s, dict):
            return s
        err = self._require_stopped(s)
        if err:
            return err

        expr = params.get("expr")
        if not expr:
            return {"ok": False, "error": "missing_param", "detail": "expr is required"}

        watch_read = params.get("read", False)
        watch_write = params.get("write", True)

        if watch_read and watch_write:
            wp_type = "-a"  # access watchpoint
        elif watch_read:
            wp_type = "-r"  # read watchpoint
        else:
            wp_type = ""    # write watchpoint (default)

        resp = s.mi.command(f'-break-watch {wp_type} {expr}')
        if not _mi_result_ok(resp):
            _, kvs = _parse_mi_record(resp["result"])
            return {"ok": False, "error": "watchpoint_failed", "detail": kvs.get("msg", "Failed")}

        _, kvs = _parse_mi_record(resp["result"])
        # GDB returns wpt={number="N", exp="expr"}
        wpt = kvs.get("wpt") or kvs.get("hw-awpt") or kvs.get("hw-rwpt") or {}
        wp_id = int(wpt.get("number", 0))
        mode = ("r" if watch_read else "") + ("w" if watch_write else "")
        desc = f"watch[{mode}] {expr}"
        s.watchpoints[wp_id] = desc

        return {"ok": True, "watchpoint_id": wp_id, "expr": expr, "mode": mode, "description": desc}

    def cmd_unwatch(self, params):
        s = self._get_session(params)
        if isinstance(s, dict):
            return s
        wp_id = params.get("watchpoint_id")
        if wp_id is None:
            return {"ok": False, "error": "missing_param", "detail": "watchpoint_id is required"}
        s.mi.command(f"-break-delete {wp_id}")
        s.watchpoints.pop(wp_id, None)
        return {"ok": True, "watchpoint_id": wp_id}

    def cmd_break_at(self, params):
        s = self._get_session(params)
        if isinstance(s, dict):
            return s

        location = params.get("location")
        if not location:
            return {"ok": False, "error": "missing_param", "detail": "location is required"}

        resp = s.mi.command(f'-break-insert {location}')
        if not _mi_result_ok(resp):
            _, kvs = _parse_mi_record(resp["result"])
            return {
                "ok": False, "error": "breakpoint_not_resolved",
                "detail": kvs.get("msg", f"Could not resolve '{location}'"),
                "suggestion": "Check spelling. For C++ use 'ClassName::method'. For file:line use 'file.cpp:42'.",
            }

        _, kvs = _parse_mi_record(resp["result"])
        bkpt = kvs.get("bkpt", {})
        bp_id = int(bkpt.get("number", 0))

        condition = params.get("condition", "")
        if condition:
            s.mi.command(f'-break-condition {bp_id} {condition}')

        s.breakpoints[bp_id] = location
        result = {"ok": True, "breakpoint_id": bp_id, "location": location, "num_locations": 1}
        if condition:
            result["condition"] = condition
        return result

    def cmd_log_point(self, params):
        s = self._get_session(params)
        if isinstance(s, dict):
            return s

        location = params.get("location")
        if not location:
            return {"ok": False, "error": "missing_param", "detail": "location is required"}

        exprs = list(params.get("exprs", []))
        condition = params.get("condition", "")
        label = params.get("label", "")

        resp = s.mi.command(f'-break-insert {location}')
        if not _mi_result_ok(resp):
            _, kvs = _parse_mi_record(resp["result"])
            return {
                "ok": False, "error": "breakpoint_not_resolved",
                "detail": kvs.get("msg", f"Could not resolve '{location}'"),
                "suggestion": "Check spelling. For C++ use 'ClassName::method'. For file:line use 'file.cpp:42'.",
            }

        _, kvs = _parse_mi_record(resp["result"])
        bkpt = kvs.get("bkpt", {})
        bp_id = int(bkpt.get("number", 0))

        if condition:
            s.mi.command(f'-break-condition {bp_id} {condition}')

        lp_id = s.next_log_point_id()
        s._log_bp_ids[bp_id] = lp_id
        s.log_points[lp_id] = {
            "location": location,
            "exprs": exprs,
            "condition": condition,
            "label": label,
            "breakpoint_id": bp_id,
        }

        result = {"ok": True, "log_point_id": lp_id, "location": location, "breakpoint_id": bp_id}
        if label:
            result["label"] = label
        if condition:
            result["condition"] = condition
        if not exprs:
            result["capture"] = "all_locals"
        return result

    def _collect_log_point(self, session, bp_id):
        """Evaluate log point expressions and store entry. Returns True if this was a log point."""
        lp_id = session._log_bp_ids.get(bp_id)
        if lp_id is None:
            return False

        lp = session.log_points.get(lp_id)
        if lp is None:
            return False

        exprs = lp.get("exprs", [])
        values = {}

        if not exprs:
            # Capture all locals
            locals_ = self._get_locals(session, 0)
            for v in locals_:
                if isinstance(v, dict) and v.get("name"):
                    values[v["name"]] = v.get("value", "")
        else:
            for expr in exprs:
                resp = session.mi.command(f'-data-evaluate-expression {expr}')
                if _mi_result_ok(resp):
                    _, kvs = _parse_mi_record(resp["result"])
                    values[expr] = kvs.get("value", "")
                else:
                    _, kvs = _parse_mi_record(resp["result"])
                    values[expr] = f"<error: {kvs.get('msg', 'eval failed')}>"

        hit = len([e for e in session.log_entries if e["log_point_id"] == lp_id]) + 1
        session.log_entries.append({
            "hit": hit,
            "log_point_id": lp_id,
            "label": lp.get("label", ""),
            "location": lp.get("location", ""),
            "values": values,
        })
        return True

    def cmd_get_logs(self, params):
        s = self._get_session(params)
        if isinstance(s, dict):
            return s

        lp_id = params.get("log_point_id")
        if lp_id is not None:
            lp_id = int(lp_id)
            entries = [e for e in s.log_entries if e["log_point_id"] == lp_id]
        else:
            entries = list(s.log_entries)

        return {"ok": True, "entries": entries, "count": len(entries)}

    def cmd_go(self, params):
        s = self._get_session(params)
        if isinstance(s, dict):
            return s

        if not s.stop_reason:
            # First run
            resp = s.mi.command("-exec-run")
        else:
            resp = s.mi.command("-exec-continue")

        # Loop: if we hit a log point, collect data and continue
        while True:
            stop_kvs = self._process_stop(s, resp)

            if s.exited:
                return {"ok": True, "process_state": "exited", "exit_code": s.exit_code}

            # Check if this stop is a log point breakpoint
            if stop_kvs and stop_kvs.get("reason") == "breakpoint-hit":
                bp_id = int(stop_kvs.get("bkptno", 0))
                if bp_id and self._collect_log_point(s, bp_id):
                    resp = s.mi.command("-exec-continue")
                    continue

            break

        result = {"ok": True, "process_state": "stopped"}
        result["crash_summary"] = self._build_crash_summary(s, stop_kvs)
        return result

    def cmd_step(self, params):
        s = self._get_session(params)
        if isinstance(s, dict):
            return s
        err = self._require_stopped(s)
        if err:
            return err

        kind = params.get("kind", "over")
        if kind == "into":
            resp = s.mi.command("-exec-step")
        elif kind == "out":
            resp = s.mi.command("-exec-finish")
        else:
            resp = s.mi.command("-exec-next")

        self._process_stop(s, resp)

        state = self._get_process_state(s)
        result = {"ok": True, "process_state": state}
        if state == "stopped":
            frames = self._get_backtrace(s)
            if frames:
                result["location"] = frames[0]
        return result

    def cmd_state(self, params):
        s = self._get_session(params)
        if isinstance(s, dict):
            return s

        result = {
            "ok": True, "session_id": s.session_id, "binary": s.binary,
            "process_state": self._get_process_state(s),
            "breakpoints": [{"id": k, "location": v} for k, v in s.breakpoints.items()],
            "watchpoints": [{"id": k, "description": v} for k, v in s.watchpoints.items()],
        }

        if s.stop_reason and not s.exited:
            frames = self._get_backtrace(s)
            if frames:
                result["current_location"] = {
                    "function": frames[0].get("function", "??"),
                    "file": frames[0].get("file_path"),
                    "line": frames[0].get("line"),
                }

        return result


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

_backend = GdbBackend()
handle_request = _backend.handle_request

if __name__ == "__main__":
    argv = sys.argv[1:]
    if not argv or argv == ["--server"]:
        run_server(_backend)
    else:
        run_cli(_backend, argv[0], argv[1:])
