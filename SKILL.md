---
name: cdb
description: Use the cdb debugger as the first action when investigating crashes, hangs, or memory corruption in compiled binaries, or when you need printf-style logging without recompiling.
---

# Debugging with cdb

When investigating a crash, hang, or memory corruption in a compiled binary (C, C++, Rust, Go, etc.):

**Always use cdb tools first. Do not read source files to guess at the bug.**

## Decision rule

| Situation | First action |
|-----------|-------------|
| Have a binary that crashes | `cdb_launch` with full path to the binary |
| Have a core dump | `cdb_attach_core` |
| Need to debug a running process | `cdb_attach` with the process PID |
| Need to find who writes a variable | `cdb_watch` after stopping at a breakpoint |
| Need to trace execution | `cdb_break_at` + `cdb_go` + `cdb_step` |
| Need a variable's value | `cdb_inspect` |
| Need printf-style logging without recompiling | `cdb_log_point` + `cdb_go` + `cdb_get_logs` |

## Workflow

1. `cdb_launch`, `cdb_attach_core`, or `cdb_attach` — always the first call. Pass the **absolute path** to the binary (or PID for attach).
2. Read `crash_summary` in the response before doing anything else — it has the answer most of the time.
3. Check `crashing_frame.locals` for null pointers and bad values.
4. Use `cdb_inspect` to dig into specific expressions (pointer dereferences, struct fields, casts).
5. If the crash site is a symptom (e.g., use-after-free), use `cdb_watch` to trace back to the root cause.
6. To trace values without stopping, use `cdb_log_point` instead of recompiling with printf. Call `cdb_get_logs` after `cdb_go` to retrieve entries.
7. Only after identifying the bug via the debugger should you read source to understand context for the fix.
8. `cdb_kill_session` when done.

## Important behaviors

- `launch` and `go` return `crash_summary` automatically on any stop — you do not need to call `cdb_crash_summary` separately
- `cdb_attach` stops the process on attach and returns `crash_summary`; `kill_session` detaches (process continues) rather than killing
- `cdb_watch` resolves addresses by expression name — just pass the variable name, no `&` needed
- `suggested_watchpoints` in the crash summary tells you what to watch next
- `inspect` errors include a `suggestion` field — read it before retrying
- Hardware watchpoint limit is 4; call `cdb_unwatch` to free slots
- Use `frame=N` on `cdb_inspect` and `cdb_watch` to evaluate in a caller frame (shown in backtrace)
- `cdb_break_at` supports a `condition` parameter — the breakpoint only fires when the expression is true
- `cdb_log_point` with empty `exprs` captures all local variables automatically
