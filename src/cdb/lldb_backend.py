#!/usr/bin/env python3
"""
cdb - AI-friendly debugger — lldb backend

Server mode (for AI agents):
  python3 cdb.py [--server]
  Reads newline-delimited JSON from stdin, writes newline-delimited JSON to stdout.

CLI mode (quick crash analysis):
  python3 cdb.py <binary> [args...]
"""

import sys
import os
import re
import signal as _signal

from cdb.backend import Session, Backend, run_server, run_cli

# ---------------------------------------------------------------------------
# Discover lldb Python bindings
# ---------------------------------------------------------------------------

def _find_lldb_python():
    candidates = [
        "/Library/Developer/CommandLineTools/Library/PrivateFrameworks/"
        "LLDB.framework/Versions/A/Resources/Python",
        "/Applications/Xcode.app/Contents/SharedFrameworks/"
        "LLDB.framework/Versions/A/Resources/Python",
    ]
    for path in candidates:
        if os.path.isdir(os.path.join(path, "lldb")):
            return path
    try:
        import subprocess
        lldb_bin = subprocess.run(
            ["xcrun", "--find", "lldb"], capture_output=True, text=True
        ).stdout.strip()
        if lldb_bin:
            fw = os.path.normpath(os.path.join(
                os.path.dirname(lldb_bin), "..", "..",
                "Library", "PrivateFrameworks",
                "LLDB.framework", "Versions", "A", "Resources", "Python",
            ))
            if os.path.isdir(os.path.join(fw, "lldb")):
                return fw
    except Exception:
        pass
    return None


_lldb_python = _find_lldb_python()
if _lldb_python and _lldb_python not in sys.path:
    sys.path.insert(0, _lldb_python)

try:
    import lldb
except ImportError:
    import json
    print(json.dumps({
        "ok": False, "error": "lldb_not_found",
        "detail": "Could not import lldb Python bindings.",
        "suggestion": "Install Xcode or Command Line Tools: xcode-select --install",
    }), flush=True)
    sys.exit(1)


# ---------------------------------------------------------------------------
# lldb-specific session
# ---------------------------------------------------------------------------

class LldbSession(Session):
    def __init__(self, session_id, binary):
        super().__init__(session_id, binary)
        self.debugger = lldb.SBDebugger.Create()
        self.debugger.SetAsync(False)
        self.target = None
        self.process = None
        self._log_bp_ids = {}  # breakpoint_id -> log_point_id

    def destroy(self):
        if self.process and self.process.IsValid():
            if self.attached:
                self.process.Detach()
            else:
                self.process.Kill()
        lldb.SBDebugger.Destroy(self.debugger)


# ---------------------------------------------------------------------------
# lldb helpers
# ---------------------------------------------------------------------------

_STOP_REASON_NAMES = {
    lldb.eStopReasonInvalid: "invalid",
    lldb.eStopReasonNone: "none",
    lldb.eStopReasonTrace: "trace",
    lldb.eStopReasonBreakpoint: "breakpoint",
    lldb.eStopReasonWatchpoint: "watchpoint",
    lldb.eStopReasonSignal: "signal",
    lldb.eStopReasonException: "exception",
    lldb.eStopReasonExec: "exec",
    lldb.eStopReasonPlanComplete: "plan_complete",
    lldb.eStopReasonThreadExiting: "thread_exiting",
}

_STATE_NAMES = {
    lldb.eStateInvalid: "invalid",
    lldb.eStateUnloaded: "unloaded",
    lldb.eStateConnected: "connected",
    lldb.eStateAttaching: "attaching",
    lldb.eStateLaunching: "launching",
    lldb.eStateStopped: "stopped",
    lldb.eStateRunning: "running",
    lldb.eStateStepping: "stepping",
    lldb.eStateCrashed: "crashed",
    lldb.eStateDetached: "detached",
    lldb.eStateExited: "exited",
    lldb.eStateSuspended: "suspended",
}

_STOPPED_STATES = {lldb.eStateStopped, lldb.eStateCrashed}


def _stop_reason_name(r):
    return _STOP_REASON_NAMES.get(r, f"unknown({r})")


def _state_name(s):
    return _STATE_NAMES.get(s, f"unknown({s})")


def _is_internal_stop(thread):
    reason = thread.GetStopReason()
    if reason in (lldb.eStopReasonNone, lldb.eStopReasonInvalid):
        return True
    if reason == lldb.eStopReasonBreakpoint:
        for i in range(thread.GetStopReasonDataCount() // 2):
            bp_id = thread.GetStopReasonDataAtIndex(i * 2)
            bp = thread.GetProcess().GetTarget().FindBreakpointByID(bp_id)
            if bp.IsValid() and bp.IsInternal():
                return True
    return False


def _run_to_user_stop(process, max_internal=20):
    for _ in range(max_internal):
        state = process.GetState()
        if state not in _STOPPED_STATES:
            break
        thread = process.GetSelectedThread()
        if not _is_internal_stop(thread):
            break
        process.Continue()
    return process


def _format_value(v, depth=0, max_depth=3):
    if not v or not v.IsValid():
        return None
    result = {
        "name": v.GetName(),
        "type": v.GetTypeName(),
        "value": v.GetValue(),
        "summary": v.GetSummary(),
    }
    typ = v.GetType()
    if typ.IsPointerType():
        addr = v.GetValueAsUnsigned(0)
        result["address"] = hex(addr)
        result["is_null"] = addr == 0
        if addr != 0 and depth < max_depth:
            deref = v.Dereference()
            if deref.IsValid():
                result["deref"] = _format_value(deref, depth + 1, max_depth)
    n = v.GetNumChildren()
    if n > 0 and depth < max_depth:
        cap = min(n, 24)
        result["children"] = [
            _format_value(v.GetChildAtIndex(i), depth + 1, max_depth)
            for i in range(cap)
        ]
        if n > cap:
            result["children_truncated"] = True
            result["total_children"] = n
    return result


def _format_frame(frame, detailed=False):
    result = {
        "index": frame.GetFrameID(),
        "pc": hex(frame.GetPC()),
        "function": frame.GetFunctionName() or "??",
    }
    le = frame.GetLineEntry()
    if le.IsValid():
        result["file"] = le.GetFileSpec().GetFilename()
        result["file_path"] = str(le.GetFileSpec())
        result["line"] = le.GetLine()
        result["column"] = le.GetColumn()
    mod = frame.GetModule()
    if mod.IsValid():
        result["module"] = mod.GetFileSpec().GetFilename()
    if detailed:
        vars_ = frame.GetVariables(True, True, False, True)
        result["locals"] = [
            _format_value(vars_.GetValueAtIndex(i))
            for i in range(vars_.GetSize())
        ]
    return result


def _interpret_crash(thread, top_frame):
    reason = thread.GetStopReason()

    if reason == lldb.eStopReasonSignal:
        sig = thread.GetStopReasonDataAtIndex(0)
        try:
            sig_name = _signal.Signals(sig).name
        except (ValueError, AttributeError):
            sig_name = str(sig)
        if sig == _signal.SIGSEGV:
            vars_ = top_frame.GetVariables(True, True, False, True)
            for i in range(vars_.GetSize()):
                v = vars_.GetValueAtIndex(i)
                if v.GetName() == "this" and v.GetValueAsUnsigned(0) == 0:
                    return "null pointer dereference: method called on null this pointer"
            return "null pointer dereference or invalid memory access (SIGSEGV)"
        if sig == _signal.SIGABRT:
            return (
                "process aborted — likely assert() failure, std::terminate(), "
                "throw from noexcept, or heap corruption detected by allocator"
            )
        if sig == _signal.SIGBUS:
            return "bus error: misaligned memory access"
        if sig == _signal.SIGFPE:
            return "arithmetic exception: division by zero or integer overflow"
        if sig == _signal.SIGILL:
            return "illegal instruction: corrupt function pointer, vtable, or stack smash"
        return f"killed by signal {sig_name}"

    if reason == lldb.eStopReasonException:
        desc = thread.GetStopDescription(256)
        if "EXC_BAD_ACCESS" in desc:
            m = re.search(r"address=(0x[0-9a-fA-F]+)", desc)
            addr_str = m.group(1) if m else "unknown"
            if addr_str in ("0x0", "0x00000000", "0x0000000000000000"):
                return "null pointer dereference (EXC_BAD_ACCESS at address 0x0)"
            return f"bad memory access at {addr_str} (EXC_BAD_ACCESS)"
        return desc

    if reason == lldb.eStopReasonBreakpoint:
        return "stopped at breakpoint"
    if reason == lldb.eStopReasonWatchpoint:
        wp_id = thread.GetStopReasonDataAtIndex(0)
        return f"watchpoint {wp_id} triggered"
    return None


def _build_crash_summary(session):
    process = session.process
    state = process.GetState()

    crash_thread = None
    for i in range(process.GetNumThreads()):
        t = process.GetThreadAtIndex(i)
        if t.GetStopReason() not in (lldb.eStopReasonNone, lldb.eStopReasonInvalid):
            crash_thread = t
            break
    if crash_thread is None:
        crash_thread = process.GetSelectedThread()

    reason = crash_thread.GetStopReason()

    signal_name = None
    if reason == lldb.eStopReasonSignal:
        sig = crash_thread.GetStopReasonDataAtIndex(0)
        try:
            signal_name = _signal.Signals(sig).name
        except (ValueError, AttributeError):
            signal_name = str(sig)

    crash_addr = None
    if reason == lldb.eStopReasonException:
        desc = crash_thread.GetStopDescription(256)
        m = re.search(r"address=(0x[0-9a-fA-F]+)", desc)
        crash_addr = m.group(1) if m else None

    top_frame = crash_thread.GetFrameAtIndex(0) if crash_thread.GetNumFrames() > 0 else None
    backtrace = [
        _format_frame(crash_thread.GetFrameAtIndex(i), detailed=(i == 0))
        for i in range(crash_thread.GetNumFrames())
    ]

    suggested_watchpoints = []
    if top_frame:
        vars_ = top_frame.GetVariables(True, True, False, True)
        for i in range(min(vars_.GetSize(), 8)):
            v = vars_.GetValueAtIndex(i)
            if v.GetType().IsPointerType() and v.GetValueAsUnsigned(0) == 0:
                suggested_watchpoints.append({
                    "expr": v.GetName(),
                    "reason": f"{v.GetName()} is null at crash site",
                    "suggestion": "Set a watchpoint in a caller frame where this is assigned",
                })

    return {
        "process_state": _state_name(state),
        "thread_id": crash_thread.GetThreadID(),
        "stop_reason": _stop_reason_name(reason),
        "signal": signal_name,
        "crash_address": crash_addr,
        "stop_description": crash_thread.GetStopDescription(512),
        "interpretation": _interpret_crash(crash_thread, top_frame) if top_frame else None,
        "crashing_frame": _format_frame(top_frame, detailed=True) if top_frame else None,
        "backtrace": backtrace,
        "num_threads": process.GetNumThreads(),
        "suggested_watchpoints": suggested_watchpoints,
    }


# ---------------------------------------------------------------------------
# lldb backend
# ---------------------------------------------------------------------------

class LldbBackend(Backend):

    def _get_process_state(self, session):
        if session.process and session.process.IsValid():
            return _state_name(session.process.GetState())
        return "not_launched"

    def _require_stopped(self, session):
        if not session.process or not session.process.IsValid():
            return {"ok": False, "error": "no_process", "detail": "No running process in this session"}
        state = session.process.GetState()
        if state not in _STOPPED_STATES:
            return {
                "ok": False,
                "error": "process_not_stopped",
                "detail": f"Process is in state '{_state_name(state)}', not stopped",
                "suggestion": "Use 'go' to run until next stop",
            }
        return None

    def cmd_launch(self, params):
        binary = params.get("binary")
        if not binary:
            return {"ok": False, "error": "missing_param", "detail": "binary is required"}
        if not os.path.isfile(binary):
            return {
                "ok": False, "error": "binary_not_found",
                "detail": f"No file at: {binary}",
                "suggestion": "Provide the full path to a compiled executable",
            }

        args = list(params.get("args", []))
        env = dict(params.get("env", {}))
        stdin_path = params.get("stdin")
        working_dir = params.get("working_dir")
        run_to_crash = params.get("run_to_crash", True)

        sid = self._new_session_id()
        session = LldbSession(sid, binary)

        error = lldb.SBError()
        target = session.debugger.CreateTarget(binary, None, None, True, error)
        if not error.Success() or not target.IsValid():
            return {"ok": False, "error": "target_creation_failed",
                    "detail": error.GetCString() or "Failed to create target"}
        session.target = target
        self._sessions[sid] = session

        if not run_to_crash:
            return {
                "ok": True, "session_id": sid, "process_state": "ready",
                "note": "Process not launched yet. Set breakpoints with 'break_at', then call 'go'.",
            }

        launch_info = lldb.SBLaunchInfo([binary] + args)
        if env:
            launch_info.SetEnvironmentEntries([f"{k}={v}" for k, v in env.items()], True)
        if working_dir:
            launch_info.SetWorkingDirectory(working_dir)
        if stdin_path:
            launch_info.SetStdinPath(stdin_path)

        error = lldb.SBError()
        process = target.Launch(launch_info, error)
        if (not error.Success()) and (not process or not process.IsValid()):
            return {"ok": False, "error": "launch_failed", "detail": error.GetCString()}
        session.process = process

        self._run_to_user_stop_with_logs(session)
        state = process.GetState()

        result = {"ok": True, "session_id": sid, "process_state": _state_name(state)}
        if state in _STOPPED_STATES:
            result["crash_summary"] = _build_crash_summary(session)
        elif state == lldb.eStateExited:
            result["exit_code"] = process.GetExitStatus()
            result["exit_description"] = process.GetExitDescription() or ""
        return result

    def cmd_attach_core(self, params):
        binary = params.get("binary")
        core = params.get("core")
        if not binary or not core:
            return {"ok": False, "error": "missing_param", "detail": "binary and core are both required"}
        for path, name in [(binary, "binary"), (core, "core")]:
            if not os.path.isfile(path):
                return {"ok": False, "error": f"{name}_not_found", "detail": f"No file at: {path}"}

        sid = self._new_session_id()
        session = LldbSession(sid, binary)

        error = lldb.SBError()
        target = session.debugger.CreateTarget(binary, None, None, True, error)
        if not error.Success() or not target.IsValid():
            return {"ok": False, "error": "target_creation_failed", "detail": error.GetCString()}
        session.target = target

        process = target.LoadCore(core)
        if not process or not process.IsValid():
            return {"ok": False, "error": "core_load_failed", "detail": f"Failed to load core: {core}"}
        session.process = process
        self._sessions[sid] = session

        return {
            "ok": True, "session_id": sid,
            "process_state": _state_name(process.GetState()),
            "crash_summary": _build_crash_summary(session),
        }

    def cmd_attach(self, params):
        pid = params.get("pid")
        if not pid:
            return {"ok": False, "error": "missing_param", "detail": "pid is required"}
        pid = int(pid)

        sid = self._new_session_id()
        session = LldbSession(sid, "")
        session.attached = True

        # Empty target — lldb resolves from the attached process
        target = session.debugger.CreateTarget("")
        if not target.IsValid():
            return {"ok": False, "error": "target_creation_failed", "detail": "Failed to create empty target"}
        session.target = target

        error = lldb.SBError()
        process = target.AttachToProcessWithID(lldb.SBListener(), pid, error)
        if not error.Success() or not process or not process.IsValid():
            return {"ok": False, "error": "attach_failed",
                    "detail": error.GetCString() or f"Failed to attach to PID {pid}",
                    "suggestion": "Check that the PID is valid and you have permission to attach."}
        session.process = process
        session.binary = process.GetTarget().GetExecutable().GetFilename() or ""
        self._sessions[sid] = session

        state = process.GetState()
        result = {"ok": True, "session_id": sid, "process_state": _state_name(state)}
        if state in _STOPPED_STATES:
            result["crash_summary"] = _build_crash_summary(session)
        return result

    def cmd_crash_summary(self, params):
        s = self._get_session(params)
        if isinstance(s, dict):
            return s
        err = self._require_stopped(s)
        if err:
            return err
        return {"ok": True, **_build_crash_summary(s)}

    def cmd_backtrace(self, params):
        s = self._get_session(params)
        if isinstance(s, dict):
            return s
        err = self._require_stopped(s)
        if err:
            return err

        if params.get("all_threads"):
            threads = []
            for i in range(s.process.GetNumThreads()):
                t = s.process.GetThreadAtIndex(i)
                threads.append({
                    "thread_id": t.GetThreadID(),
                    "stop_reason": _stop_reason_name(t.GetStopReason()),
                    "frames": [_format_frame(t.GetFrameAtIndex(j)) for j in range(t.GetNumFrames())],
                })
            return {"ok": True, "threads": threads}

        t = s.process.GetSelectedThread()
        return {
            "ok": True,
            "thread_id": t.GetThreadID(),
            "stop_reason": _stop_reason_name(t.GetStopReason()),
            "stop_description": t.GetStopDescription(256),
            "frames": [_format_frame(t.GetFrameAtIndex(i)) for i in range(t.GetNumFrames())],
        }

    def cmd_select_thread(self, params):
        s = self._get_session(params)
        if isinstance(s, dict):
            return s
        err = self._require_stopped(s)
        if err:
            return err

        thread_id = params.get("thread_id")
        if thread_id is None:
            return {"ok": False, "error": "missing_param", "detail": "thread_id is required"}
        thread_id = int(thread_id)

        found = False
        for i in range(s.process.GetNumThreads()):
            t = s.process.GetThreadAtIndex(i)
            if t.GetThreadID() == thread_id:
                s.process.SetSelectedThread(t)
                found = True
                break

        if not found:
            available = [s.process.GetThreadAtIndex(i).GetThreadID()
                         for i in range(s.process.GetNumThreads())]
            return {
                "ok": False, "error": "thread_not_found",
                "detail": f"No thread with ID {thread_id}",
                "available_thread_ids": available,
            }

        thread = s.process.GetSelectedThread()
        frame = thread.GetFrameAtIndex(0)
        result = {
            "ok": True,
            "thread_id": thread.GetThreadID(),
            "stop_reason": _stop_reason_name(thread.GetStopReason()),
            "num_frames": thread.GetNumFrames(),
            "location": _format_frame(frame, detailed=True),
        }
        return result

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
        thread = s.process.GetSelectedThread()
        frame = thread.GetFrameAtIndex(frame_idx)

        opts = lldb.SBExpressionOptions()
        opts.SetTimeoutInMicroSeconds(5_000_000)
        opts.SetTryAllThreads(False)
        result = frame.EvaluateExpression(expr, opts)

        if result.GetError().Fail():
            detail = result.GetError().GetCString()
            suggestion = None
            if "use of undeclared identifier" in detail:
                suggestion = (
                    f"'{expr}' is not in scope at frame {frame_idx}. "
                    "Use 'backtrace' to find the right frame, then pass frame=<N>."
                )
            elif "optimized" in detail.lower():
                suggestion = "Variable was optimized out. Rebuild with -O0 -g."
            return {"ok": False, "error": "expression_failed", "detail": detail, "suggestion": suggestion}

        return {"ok": True, "expr": expr, "result": _format_value(result)}

    def cmd_watch(self, params):
        s = self._get_session(params)
        if isinstance(s, dict):
            return s
        err = self._require_stopped(s)
        if err:
            return err

        expr = params.get("expr")
        if not expr:
            return {"ok": False, "error": "missing_param", "detail": "expr is required (variable or expression)"}

        watch_read = params.get("read", False)
        watch_write = params.get("write", True)
        frame_idx = params.get("frame", 0)

        thread = s.process.GetSelectedThread()
        frame = thread.GetFrameAtIndex(frame_idx)

        opts = lldb.SBExpressionOptions()
        opts.SetTimeoutInMicroSeconds(5_000_000)
        addr_result = frame.EvaluateExpression(f"&({expr})", opts)
        if addr_result.GetError().Fail():
            return {
                "ok": False, "error": "cannot_get_address",
                "detail": addr_result.GetError().GetCString(),
                "suggestion": f"'{expr}' must be an lvalue in the current frame (frame {frame_idx}).",
            }

        addr = addr_result.GetValueAsUnsigned(0)
        if addr == 0:
            return {"ok": False, "error": "null_address", "detail": f"Address of '{expr}' is 0"}

        val_result = frame.EvaluateExpression(expr, opts)
        size = val_result.GetByteSize() if val_result.IsValid() and val_result.GetByteSize() > 0 else 8

        error = lldb.SBError()
        wp = s.target.WatchAddress(addr, size, watch_read, watch_write, error)
        if not error.Success() or not wp.IsValid():
            detail = error.GetCString() or "Failed to set watchpoint"
            suggestion = None
            if "hardware" in detail.lower() or "resource" in detail.lower():
                suggestion = (
                    "Hardware watchpoint limit reached (4 on x86/arm64). "
                    "Remove one with 'unwatch' first."
                )
            return {"ok": False, "error": "watchpoint_failed", "detail": detail, "suggestion": suggestion}

        wp_id = wp.GetID()
        mode = ("r" if watch_read else "") + ("w" if watch_write else "")
        desc = f"watch[{mode}] {expr} @ {hex(addr)} (size={size})"
        s.watchpoints[wp_id] = desc

        return {
            "ok": True, "watchpoint_id": wp_id, "expr": expr,
            "address": hex(addr), "size": size, "mode": mode, "description": desc,
        }

    def cmd_unwatch(self, params):
        s = self._get_session(params)
        if isinstance(s, dict):
            return s
        wp_id = params.get("watchpoint_id")
        if wp_id is None:
            return {"ok": False, "error": "missing_param", "detail": "watchpoint_id is required"}
        s.target.DeleteWatchpoint(wp_id)
        s.watchpoints.pop(wp_id, None)
        return {"ok": True, "watchpoint_id": wp_id}

    def cmd_break_at(self, params):
        s = self._get_session(params)
        if isinstance(s, dict):
            return s

        location = params.get("location")
        if not location:
            return {
                "ok": False, "error": "missing_param", "detail": "location is required",
                "examples": ["main", "MyClass::method", "foo.cpp:42"],
            }

        bp = None
        if ":" in location and "::" not in location:
            parts = location.rsplit(":", 1)
            try:
                bp = s.target.BreakpointCreateByLocation(parts[0], int(parts[1]))
            except ValueError:
                pass

        if bp is None or not bp.IsValid():
            bp = s.target.BreakpointCreateByName(location)

        if not bp.IsValid() or bp.GetNumLocations() == 0:
            return {
                "ok": False, "error": "breakpoint_not_resolved",
                "detail": f"Could not resolve '{location}'",
                "suggestion": "Check spelling. For C++ use 'ClassName::method'. For file:line use exact filename.",
            }

        condition = params.get("condition", "")
        if condition:
            bp.SetCondition(condition)

        bp_id = bp.GetID()
        s.breakpoints[bp_id] = location
        result = {"ok": True, "breakpoint_id": bp_id, "location": location, "num_locations": bp.GetNumLocations()}
        if condition:
            result["condition"] = condition
        return result

    def cmd_delete_breakpoint(self, params):
        s = self._get_session(params)
        if isinstance(s, dict):
            return s
        bp_id = params.get("breakpoint_id")
        if bp_id is None:
            return {"ok": False, "error": "missing_param", "detail": "breakpoint_id is required"}
        bp_id = int(bp_id)
        if not s.target.BreakpointDelete(bp_id):
            return {"ok": False, "error": "breakpoint_not_found", "detail": f"No breakpoint with ID {bp_id}"}
        s.breakpoints.pop(bp_id, None)
        # Also clean up if it was a log point
        lp_id = s._log_bp_ids.pop(bp_id, None)
        if lp_id is not None:
            s.log_points.pop(lp_id, None)
        return {"ok": True, "breakpoint_id": bp_id}

    def cmd_disable_breakpoint(self, params):
        s = self._get_session(params)
        if isinstance(s, dict):
            return s
        bp_id = params.get("breakpoint_id")
        if bp_id is None:
            return {"ok": False, "error": "missing_param", "detail": "breakpoint_id is required"}
        bp = s.target.FindBreakpointByID(int(bp_id))
        if not bp.IsValid():
            return {"ok": False, "error": "breakpoint_not_found", "detail": f"No breakpoint with ID {bp_id}"}
        bp.SetEnabled(False)
        return {"ok": True, "breakpoint_id": int(bp_id), "enabled": False}

    def cmd_enable_breakpoint(self, params):
        s = self._get_session(params)
        if isinstance(s, dict):
            return s
        bp_id = params.get("breakpoint_id")
        if bp_id is None:
            return {"ok": False, "error": "missing_param", "detail": "breakpoint_id is required"}
        bp = s.target.FindBreakpointByID(int(bp_id))
        if not bp.IsValid():
            return {"ok": False, "error": "breakpoint_not_found", "detail": f"No breakpoint with ID {bp_id}"}
        bp.SetEnabled(True)
        return {"ok": True, "breakpoint_id": int(bp_id), "enabled": True}

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

        # Create breakpoint at location
        bp = None
        if ":" in location and "::" not in location:
            parts = location.rsplit(":", 1)
            try:
                bp = s.target.BreakpointCreateByLocation(parts[0], int(parts[1]))
            except ValueError:
                pass

        if bp is None or not bp.IsValid():
            bp = s.target.BreakpointCreateByName(location)

        if not bp.IsValid() or bp.GetNumLocations() == 0:
            return {
                "ok": False, "error": "breakpoint_not_resolved",
                "detail": f"Could not resolve '{location}'",
                "suggestion": "Check spelling. For C++ use 'ClassName::method'. For file:line use exact filename.",
            }

        if condition:
            bp.SetCondition(condition)

        lp_id = s.next_log_point_id()
        bp_id = bp.GetID()
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

    def _collect_log_point(self, session, thread):
        """Check if the current stop is a log point, collect data. Returns True if it was."""
        reason = thread.GetStopReason()
        if reason != lldb.eStopReasonBreakpoint:
            return False

        for i in range(thread.GetStopReasonDataCount() // 2):
            bp_id = thread.GetStopReasonDataAtIndex(i * 2)
            lp_id = session._log_bp_ids.get(bp_id)
            if lp_id is None:
                continue

            lp = session.log_points.get(lp_id)
            if lp is None:
                continue

            frame = thread.GetFrameAtIndex(0)
            exprs = lp.get("exprs", [])
            values = {}

            if not exprs:
                variables = frame.GetVariables(True, True, False, True)
                for j in range(variables.GetSize()):
                    v = variables.GetValueAtIndex(j)
                    if v.IsValid():
                        values[v.GetName()] = v.GetValue() or v.GetSummary() or ""
            else:
                opts = lldb.SBExpressionOptions()
                opts.SetTimeoutInMicroSeconds(1_000_000)
                opts.SetTryAllThreads(False)
                for expr in exprs:
                    result = frame.EvaluateExpression(expr, opts)
                    if result.GetError().Fail():
                        values[expr] = "<error: %s>" % result.GetError().GetCString()
                    else:
                        values[expr] = result.GetValue() or result.GetSummary() or ""

            hit = len([e for e in session.log_entries if e["log_point_id"] == lp_id]) + 1
            session.log_entries.append({
                "hit": hit,
                "log_point_id": lp_id,
                "label": lp.get("label", ""),
                "location": lp.get("location", ""),
                "values": values,
            })
            return True

        return False

    def _run_to_user_stop_with_logs(self, session):
        """Run process, collecting log points and skipping internal stops."""
        process = session.process
        max_loops = 10000
        for _ in range(max_loops):
            state = process.GetState()
            if state not in _STOPPED_STATES:
                break
            thread = process.GetSelectedThread()
            if _is_internal_stop(thread):
                process.Continue()
                continue
            if self._collect_log_point(session, thread):
                process.Continue()
                continue
            break

    def cmd_go(self, params):
        s = self._get_session(params)
        if isinstance(s, dict):
            return s

        if not s.process or not s.process.IsValid():
            args = list(params.get("args", []))
            env = dict(params.get("env", {}))
            working_dir = params.get("working_dir")

            launch_info = lldb.SBLaunchInfo([s.binary] + args)
            if env:
                launch_info.SetEnvironmentEntries([f"{k}={v}" for k, v in env.items()], True)
            if working_dir:
                launch_info.SetWorkingDirectory(working_dir)

            error = lldb.SBError()
            process = s.target.Launch(launch_info, error)
            if (not error.Success()) and (not process or not process.IsValid()):
                return {"ok": False, "error": "launch_failed", "detail": error.GetCString()}
            s.process = process
        else:
            s.process.Continue()

        self._run_to_user_stop_with_logs(s)
        state = s.process.GetState()

        result = {"ok": True, "process_state": _state_name(state)}
        if state in _STOPPED_STATES:
            result["crash_summary"] = _build_crash_summary(s)
        elif state == lldb.eStateExited:
            result["exit_code"] = s.process.GetExitStatus()
            result["exit_description"] = s.process.GetExitDescription() or ""
        return result

    def cmd_step(self, params):
        s = self._get_session(params)
        if isinstance(s, dict):
            return s
        err = self._require_stopped(s)
        if err:
            return err

        kind = params.get("kind", "over")
        thread = s.process.GetSelectedThread()

        if kind == "into":
            thread.StepInto()
        elif kind == "out":
            thread.StepOut()
        else:
            thread.StepOver()

        state = s.process.GetState()
        result = {"ok": True, "process_state": _state_name(state)}
        if state in _STOPPED_STATES:
            t = s.process.GetSelectedThread()
            frame = t.GetFrameAtIndex(0)
            result["location"] = _format_frame(frame, detailed=False)
        elif state == lldb.eStateExited:
            result["exit_code"] = s.process.GetExitStatus()
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

        if s.process and s.process.IsValid():
            state = s.process.GetState()
            if state in _STOPPED_STATES:
                thread = s.process.GetSelectedThread()
                frame = thread.GetFrameAtIndex(0)
                le = frame.GetLineEntry()
                result["current_location"] = {
                    "function": frame.GetFunctionName() or "??",
                    "file": str(le.GetFileSpec()) if le.IsValid() else None,
                    "line": le.GetLine() if le.IsValid() else None,
                }

        return result


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

# Module-level handle_request for backward compatibility with mcp_server.py
_backend = LldbBackend()
handle_request = _backend.handle_request

if __name__ == "__main__":
    argv = sys.argv[1:]
    if not argv or argv == ["--server"]:
        run_server(_backend)
    else:
        run_cli(_backend, argv[0], argv[1:])
