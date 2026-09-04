# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/terminal.py
──────────────────
Interactive terminal process management:
  - stream_command(): spawn a shell command, stream output via WebSocket
  - stdin injection: send input to a waiting process
  - kill: terminate a running process
  - stop events: allow the agent loop to be cancelled mid-flight

⚠️ EVERY EXECUTION IS REGISTERED WITH `core/commands.py` (Task 4).
The registry is what gives a command an identity (`command_id`), a pid, a
status, and — critically — a `last_output_at` heartbeat. Nothing here decides
what to do about a stuck process; it only records enough for something else to
be able to tell. A `stream_command` that forgot to register would be invisible
to every later reader, which is exactly the "stuck and never responds" state
the manager exists to make observable.

The registration calls are best-effort: bookkeeping may never be the reason a
command fails to run.

⚠️ TASK 6 — THE LIVE STATE IS PUSHED, NEVER POLLED BY THE BROWSER.
`command_heartbeat` (throttled to `config.CMD_HEARTBEAT_SEC`) and `command_stuck`
/ `command_unstuck` are emitted from the drainer's ticks, because only this side
knows whether the process is still alive. A pane counting seconds on its own
would keep incrementing "Elapsed" after the command died — the surest way to
report a hang that is not happening. The heartbeat is deliberately NOT emitted
per output line: a 100k-line build would put one Socket.IO frame per line on the
hot path, and the pane needs a clock, not a firehose.
"""

import os
import threading
import subprocess

from agent2 import config as _cfg
from agent2.config import ROOT, SHELL_LABEL, clip_output_line, shell_argv
from agent2.core import commands as _commands
from agent2.core import permissions as _perms
from agent2.core import procio as _procio

# ── Shared state (module-level singletons, imported where needed) ──────────────

# sid -> { term_id -> {"proc": Popen, "lock": Lock} }
_procs: dict[str, dict] = {}

# sid -> threading.Event  (set = stop requested)
_stop_events: dict[str, threading.Event] = {}

_procs_lock = threading.Lock()


# ── Process store helpers ──────────────────────────────────────────────────────

def store_proc(sid: str, term_id: str, proc: subprocess.Popen,
               command_id: str = "") -> None:
    """Register a live process for (sid, term_id).

    `command_id` is carried alongside the handle so `kill_proc` — which is
    reached from a Socket.IO handler that knows only the pane — can record the
    KILLED transition against the right execution. Without it the registry would
    show a command as STREAMING forever after the user killed it.
    """
    with _procs_lock:
        if sid not in _procs:
            _procs[sid] = {}
        _procs[sid][term_id] = {"proc": proc, "lock": threading.Lock(),
                                "command_id": command_id}


def get_proc(sid: str, term_id: str) -> dict | None:
    return _procs.get(sid, {}).get(term_id)


def del_proc(sid: str, term_id: str) -> None:
    with _procs_lock:
        _procs.get(sid, {}).pop(term_id, None)


def cleanup_sid(sid: str) -> None:
    """Remove all proc entries for a disconnected session."""
    with _procs_lock:
        _procs.pop(sid, None)


def cancel_sid(sid: str, reason: str = "cancelled by user") -> list[str]:
    """Stop every live process of a session. Returns the command ids settled.

    ⚠️ TASK 7 — THIS IS THE WEB HALF OF A CANCEL, and it was missing.
    `stop_agent` only sets the loop's stop event, which a `run_command` already
    inside `stream_command` never reads: the child kept running to completion,
    kept streaming into the pane, and kept its execution ACTIVE — the browser's
    version of "Stop did nothing". The stop event ends the *agent loop*; only this
    ends the *command*.

    ⚠️ ALL THE RECORDS ARE WRITTEN BEFORE ANY KILL, not paired per pane.
    `terminate_tree` blocks for up to `grace`, and the moment a tree dies its
    pipes close and that pane's streaming thread settles the execution itself with
    the shell's exit code. Pairing record-and-kill per pane would leave the second
    pane's row unwritten for that whole second — long enough to lose the race and
    report "exit 1" on a command the user cancelled.

    ⚠️ AND IT NEVER RAISES. A cancel that fails halfway leaves a live process with
    nothing tracking it, so every step is best-effort and the loop always runs to
    the end.
    """
    with _procs_lock:
        panes = list(_procs.get(sid, {}).items())
    settled: list[str] = []
    for _term_id, entry in panes:
        cmd_id = entry.get("command_id") or ""
        if not cmd_id:
            continue
        try:
            row = _commands.cancel(cmd_id, reason=reason)
        except Exception:
            continue
        if row is not None and row.status == _commands.CommandStatus.CANCELLED:
            settled.append(cmd_id)
    for term_id, entry in panes:
        try:
            _procio.terminate_tree(entry["proc"], grace=1.0)
        except Exception:
            pass
        del_proc(sid, term_id)
    return settled


# ── Stop-event helpers (used by agent.py) ─────────────────────────────────────

def make_stop(sid: str) -> threading.Event:
    ev = threading.Event()
    _stop_events[sid] = ev
    return ev


def stop_agent(sid: str) -> None:
    ev = _stop_events.get(sid)
    if ev:
        ev.set()


def clear_stop(sid: str) -> None:
    _stop_events.pop(sid, None)


# ── Main command runner ────────────────────────────────────────────────────────

def stream_command(
    command: str,
    sid: str,
    term_id: str,
    socketio,           # passed in to avoid circular import
    *,
    task_id: str = "",
    session_id: str = "",
    timeout: float | None = None,
    idle_timeout: float | None = None,
) -> tuple[str, int]:
    """
    Run *command* in the platform shell.
    Stream each output line via WebSocket to the given sid.
    Returns (full_output, returncode).

    ⚠️ Web parity (item 6): this now emits `chat_cmd_result` after the command
    completes, reporting stdout/stderr/exit/duration distinctly. The CLI's
    `run_cmd_stream` already separates streams and measures wall-clock; this
    brings the Web half to the same contract.

    ⚠️ THE RETURN CONTRACT IS UNCHANGED — `(output, returncode)`. Both agent
    loops destructure it positionally, so the Task 4 bookkeeping is added
    strictly alongside: the `command_id` lives in `core/commands.py` and is
    emitted on the socket events, never folded into the tuple.

    The keyword arguments are all optional. A caller that knows nothing about
    tasks (the raw `run_raw_command` handler) still gets a fully tracked
    execution — just one with an empty `task_id`.
    """
    import time
    output: list[str] = []      # merged, in arrival order — this is what is returned
    errs: list[str] = []        # stderr only, for the result card
    start = time.time()

    # ⚠️ Task 15: the capability gate for shell execution on this surface.
    # `run_command` is the one agent tool that does NOT go through
    # `tools.dispatch_tool`, so the gate there cannot see it — this is the only
    # place a web-side execution can be refused. Refused BEFORE `commands.create`
    # on purpose: an execution that was never permitted is not a FAILED execution,
    # and recording one would put a command in `/api/commands` that no process ever
    # backed. rc 126 is the shell's own "found but not executable".
    if not _perms.process_allows(_perms.CAP_EXEC):
        _perms.audit_use(_perms.CAP_EXEC, ok=False, what="stream_command",
                         detail=str(command)[:120], surface="web")
        msg = _perms.refusal(_perms.CAP_EXEC, what="running a shell command")
        # Mirror the error path's three events exactly: a browser terminal that
        # never receives `terminal_done` shows a running prompt forever, so a
        # refusal that skipped it would look like a hang rather than a refusal.
        for event, payload in (
            ("terminal_line", {"data": f"[refused] {msg}", "term_id": term_id}),
            ("terminal_done", {"returncode": 126, "term_id": term_id}),
            ("proc_ended", {"term_id": term_id}),
        ):
            try:
                socketio.emit(event, payload, room=sid)
            except Exception:
                pass
        return msg, 126

    argv = shell_argv(command)

    # Task 6: an unset ceiling falls back to the configured default — which is OFF
    # unless the operator set one. See `config.CMD_TIMEOUT` for why no number is
    # the right default.
    limit = timeout if timeout is not None else _cfg.CMD_TIMEOUT
    idle_limit = idle_timeout if idle_timeout is not None else _cfg.CMD_IDLE_TIMEOUT

    # Task 4: the execution exists BEFORE the spawn, so a Popen that raises is a
    # visible FAILED execution rather than a command that never happened.
    cmd_id = _commands.create(
        command, task_id=task_id, session_id=session_id, surface="web",
        term_id=term_id, timeout=limit, idle_timeout=idle_limit,
    ).id

    # Use the current workspace root as the working directory so the terminal
    # operates in the user's project, not the Agent2 installation directory.
    try:
        from agent2.core.workspace import root as workspace_root
        cwd = str(workspace_root())
    except Exception:
        cwd = str(ROOT)  # fallback to Agent2 dir if workspace unavailable

    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            universal_newlines=True,
            env=os.environ.copy(),
            cwd=cwd,
            # Task 5: own session/group so a kill reaches the whole tree.
            **_procio.GROUP_KWARGS,
        )
        # ⚠️ THE HANDLE IS STORED BEFORE THE REGISTRY IS TOLD IT STARTED.
        # `kill_proc` reaches the process through `_procs`; a kill arriving in the
        # window between these two calls used to find nothing there and return
        # silently, leaving the command running with the pane showing no reason
        # why. Storing first makes the window harmless instead: the kill finds the
        # handle, settles the execution as KILLED, and `start()` is then refused
        # by the terminal-state guard — which is the truthful record either way.
        store_proc(sid, term_id, proc, command_id=cmd_id)
        _commands.start(cmd_id, proc.pid)

        socketio.emit("terminal_start", {
            "command": command,
            "shell":   SHELL_LABEL,
            "term_id": term_id,
            "command_id": cmd_id,
        }, room=sid)
        socketio.emit("proc_started", {"term_id": term_id, "command_id": cmd_id},
                      room=sid)

        # ⚠️ Task 5: both pipes are drained CONCURRENTLY by `core/procio.drain`.
        # stderr is now its own pipe rather than being folded into stdout, which
        # is only safe because the drainer reads both at once — reading them in
        # sequence is the classic deadlock (see `core/procio.py`).
        #
        # Every line still goes to the pane, and `output` stays the MERGED text in
        # arrival order: both agent loops feed it to the model as the tool result,
        # so dropping stderr from it would hide every error message from the model.
        verdict = ""            # Task 6: what ended it, if not the process itself
        last_beat = 0.0
        stuck_sent = False

        for stream, line in _procio.drain(proc):
            if stream == "tick":
                # ⚠️ Task 6: the watchdog rides the drainer's ticks — no extra
                # thread, and therefore no second opinion about whether the
                # process is still alive. `drain` guarantees a tick even in total
                # silence, which is exactly when a judgement is needed.
                call = _commands.watch(cmd_id, stuck_after=_cfg.CMD_STUCK_AFTER)
                if call in (_commands.W_TIMEOUT, _commands.W_IDLE):
                    verdict = call
                    break
                snap = _commands.get(cmd_id)
                if snap is None:
                    continue
                if call == _commands.W_STUCK and not stuck_sent:
                    # The browser's half of the [R]/[K]/[W] offer: the pane already
                    # has a kill button, so the event carries the state the user
                    # needs to decide rather than a second control surface.
                    stuck_sent = True
                    socketio.emit("command_stuck", {
                        "term_id": term_id, "command_id": cmd_id,
                        "command": command,
                        "elapsed": round(snap.elapsed, 1),
                        "idle": round(snap.idle, 1),
                        "state": _commands.live_line(snap),
                    }, room=sid)
                now = time.monotonic()
                if now - last_beat >= _cfg.CMD_HEARTBEAT_SEC:
                    # ⚠️ THROTTLED, and emitted only on ticks. One frame per
                    # output line would put the live state on the hot path of a
                    # 100k-line build; the pane needs a clock, not a firehose.
                    last_beat = now
                    socketio.emit("command_heartbeat", {
                        "term_id": term_id, "command_id": cmd_id,
                        "elapsed": round(snap.elapsed, 1),
                        "idle": round(snap.idle, 1),
                        "state": _commands.live_line(snap),
                        "stuck": call == _commands.W_STUCK,
                    }, room=sid)
                continue
            _commands.heartbeat(cmd_id)
            if stuck_sent:
                # It spoke again: withdraw the warning rather than leaving a stale
                # "appears stuck" badge on a command that has recovered.
                stuck_sent = False
                socketio.emit("command_unstuck", {
                    "term_id": term_id, "command_id": cmd_id,
                }, room=sid)
            # ⚠️ THE EMIT IS CLIPPED, THE CAPTURE IS NOT. One 8 MB output line
            # becomes one Socket.IO frame the browser must lay out, which stalls
            # the pane long after the process exited. `output` below keeps every
            # byte, so the model's tool result is unaffected — the same split the
            # CLI makes in `cli/runtime.py`.
            socketio.emit("terminal_line", {
                "data":    clip_output_line(line).rstrip("\n"),
                "term_id": term_id,
                "command_id": cmd_id,
                "stream":  stream,
            }, room=sid)
            output.append(line)
            if stream == "stderr":
                errs.append(line)

        if verdict:
            # ⚠️ Task 6: RECORD, THEN KILL — the same order `kill_proc` uses and
            # for the same reason. `terminate_tree` blocks until the tree is dead;
            # the moment it dies the pipes close and the settle below would
            # otherwise be the shell's exit code, leaving the record saying "exit 1"
            # with nothing to say a timeout caused it.
            reason = (f"execution timeout after {_commands.human_secs(limit)}"
                      if verdict == _commands.W_TIMEOUT
                      else f"no output for {_commands.human_secs(idle_limit)}")
            _commands.timed_out(cmd_id, reason=reason)
            socketio.emit("terminal_line", {
                "data": f"⚠ {reason} — terminating process tree",
                "term_id": term_id, "command_id": cmd_id, "stream": "stderr",
            }, room=sid)
            _procio.terminate_tree(proc, grace=1.0)
            del_proc(sid, term_id)
            duration = time.time() - start
            note = f"\n[{reason}]"
            socketio.emit("terminal_line", {
                "data": "✓ command stopped", "term_id": term_id,
                "command_id": cmd_id, "stream": "stderr",
            }, room=sid)
            socketio.emit("terminal_done", {"returncode": 124, "term_id": term_id,
                                            "command_id": cmd_id}, room=sid)
            socketio.emit("proc_ended", {"term_id": term_id, "command_id": cmd_id},
                          room=sid)
            try:
                socketio.emit("chat_cmd_result", {
                    "cmd": command,
                    "stdout": ("".join(output) + note)[:4000],
                    "stderr": "".join(errs)[:4000],
                    "exit_code": 124,
                    "duration": duration,
                    "command_id": cmd_id,
                }, room=sid)
            except Exception:
                pass
            # 124 is `timeout(1)`'s convention, and the note is what the MODEL
            # reads: without it a timed-out command looks like one that simply
            # produced less output than expected.
            return "".join(output) + note, 124

        proc.wait()
        del_proc(sid, term_id)
        duration = time.time() - start
        _commands.complete(cmd_id, proc.returncode,
                           stdout="".join(output), stderr="".join(errs))

        socketio.emit("terminal_done", {
            "returncode": proc.returncode,
            "term_id":    term_id,
            "command_id": cmd_id,
        }, room=sid)
        socketio.emit("proc_ended", {"term_id": term_id, "command_id": cmd_id},
                      room=sid)

        # Item 6 (Web parity): the command-result card. The CLI's
        # `run_cmd_stream` already separated stdout/stderr; as of Task 5 this half
        # does too, so `stderr` here is the real thing instead of always "".
        try:
            socketio.emit("chat_cmd_result", {
                "cmd": command,
                "stdout": "".join(output)[:4000],
                "stderr": "".join(errs)[:4000],
                "exit_code": proc.returncode,
                "duration": duration,
                "command_id": cmd_id,
            }, room=sid)
        except Exception:
            pass

        return "".join(output), proc.returncode

    except Exception as exc:
        err = str(exc)
        del_proc(sid, term_id)
        _commands.fail(cmd_id, err, exit_code=-1)
        socketio.emit("terminal_line", {"data": f"[error] {err}", "term_id": term_id}, room=sid)
        socketio.emit("terminal_done", {"returncode": -1, "term_id": term_id}, room=sid)
        socketio.emit("proc_ended", {"term_id": term_id}, room=sid)
        return err, -1


# ── Stdin injection ────────────────────────────────────────────────────────────

def send_stdin(sid: str, term_id: str, text: str, socketio) -> None:
    entry = get_proc(sid, term_id)
    if not entry:
        socketio.emit("terminal_line", {
            "data": "[no active process to send input to]",
            "term_id": term_id,
        }, room=sid)
        return
    try:
        with entry["lock"]:
            entry["proc"].stdin.write(text + "\n")
            entry["proc"].stdin.flush()
        socketio.emit("terminal_line", {
            "data": f"[stdin] {text}",
            "term_id": term_id,
        }, room=sid)
    except Exception as exc:
        socketio.emit("terminal_line", {
            "data": f"[stdin error] {exc}",
            "term_id": term_id,
        }, room=sid)


# ── Kill ───────────────────────────────────────────────────────────────────────

def kill_proc(sid: str, term_id: str) -> None:
    """Terminate the process in a pane and record the KILLED transition.

    ⚠️ THE REGISTRY WRITE HAPPENS **BEFORE** THE KILL, and the order is the whole
    correctness argument.

    `_mutate` refuses to move an execution that has already settled, so whichever
    verdict lands first wins. `terminate_tree` BLOCKS until the tree is dead
    (up to `grace` plus the final wait) — and the moment it dies the pipes close,
    the streaming thread falls out of `drain`, and it calls `complete()` with the
    shell's exit code. Killing first therefore loses the race: the execution reads
    FAILED / "exit 1", with no record that anyone killed anything. Observed
    exactly that way once `terminate()` was replaced by the blocking tree kill in
    Task 5 — the write used to sit after a call that returned immediately.

    Recording first is also the truthful order: the user's kill is the CAUSE of
    the exit that follows, not a competing explanation for it.

    ⚠️ And it happens even if the kill raises. A process that has already exited
    raises here, and treating that as "nothing to record" would leave the
    execution stuck at STREAMING for the rest of the session.
    """
    entry = get_proc(sid, term_id)
    if not entry:
        return
    cmd_id = entry.get("command_id") or ""
    if cmd_id:
        _commands.killed(cmd_id, reason="killed from the terminal pane")
    # ⚠️ Task 5: the whole tree, not just the shell. `terminate()` on the shell
    # left its children running — they held the pipes open, so the pane never
    # reported the command as finished and the execution stayed STREAMING.
    _procio.terminate_tree(entry["proc"], grace=1.0)
