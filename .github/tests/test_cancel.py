# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
Tests for command cancellation (Task 7) — ``agent2/cli/state.py``
(`record_cancel` / `stop_active_command` / `trigger_cancel`),
``agent2/cli/runtime.py`` (`interrupt`, the key listener) and
``agent2/terminal.py`` (`cancel_sid`).

Run from the repo root:  python -m pytest .github/tests/test_cancel.py -v

The one sentence this file exists to defend: **Ctrl+C stops the COMMAND, not the
SESSION.** Everything below is a way of asking that from a different angle.

Coverage
  - the three spec lines are printed, in order, only when something was running
  - the process tree really dies — grandchild included
  - the command state is settled CANCELLED, and settled BEFORE the kill
  - a cancel with nothing running prints nothing and settles nothing (idempotent)
  - the key listener SURVIVES ESC — the session stays typeable
  - a second Ctrl+C inside the window is still the way out (`exit_event`)
  - the Web half: `stop_agent` and a disconnect both end live commands
  - `cancel_sid` records every pane before it kills any of them

⚠️ The load-bearing tests are ``test_esc_does_not_stop_the_key_listener`` (a
listener that exits leaves the user unable to type — the session is alive in name
only), ``test_the_cancel_is_flagged_and_recorded_before_the_tree_is_killed``
(record-after-kill loses the race and reports the shell's exit code instead of the
user's cancel) and ``test_a_web_disconnect_ends_the_command`` (forgetting a handle
is not killing a process).
"""

import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from agent2.core import commands as C

# A long-lived, deliberately silent child. One level of quoting only — the shell
# parses this line, and nesting a quoted interpreter path inside it is a parse
# error on PowerShell.
QUIET = 'python -c "import time;time.sleep(60)"'


@pytest.fixture(autouse=True)
def _clean():
    from agent2.cli import state as st
    C.reset()
    st.cancel_event.clear()
    st.exit_event.clear()
    st.S.last_ctrl_c = 0.0
    st.clear_key_handler()
    yield
    st.clear_key_handler()
    st.cancel_event.clear()
    st.exit_event.clear()
    st.S.last_ctrl_c = 0.0
    st.S.active_proc = None
    C.reset()


def _pid_alive(pid: int) -> bool:
    if sys.platform.startswith("win"):
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"],
                             capture_output=True, text=True, check=False)
        return str(pid) in out.stdout
    try:
        import os
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _force_kill(pid: int) -> None:
    try:
        if sys.platform.startswith("win"):
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, check=False)
        else:
            import os
            import signal
            os.kill(pid, signal.SIGKILL)
    except Exception:
        pass


def _run_in_thread(cmd: str, session_id: str) -> tuple[threading.Thread, dict]:
    from agent2.cli import runtime
    box: dict = {}
    t = threading.Thread(
        target=lambda: box.update(r=runtime.run_cmd_stream(cmd, session_id=session_id)),
        daemon=True)
    t.start()
    return t, box


def _await_running(session_id: str, timeout: float = 30.0) -> C.CommandExecution:
    """Block until the execution exists AND has a pid, so a cancel lands on a real
    process rather than in the spawn window."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        rows = C.list_all(session_id=session_id)
        if rows and rows[0].process_id:
            return rows[0]
        time.sleep(0.05)
    raise AssertionError(f"no running command for {session_id}")


# ── The spec's three lines ────────────────────────────────────────────────────

def test_interrupt_prints_the_three_lines_in_order(capsys):
    """The exact sequence the task asks for, and in the order the work happens.

        Stopping current command...
        Terminating process tree...
        ✓ Command stopped
    """
    from agent2.cli import runtime

    t, box = _run_in_thread(QUIET, "t7-lines")
    try:
        _await_running("t7-lines")
        assert runtime.interrupt() is True
        t.join(timeout=30)
        assert not t.is_alive(), "run_cmd_stream did not return after the cancel"
    finally:
        from agent2.cli import state as st
        st.cancel_event.clear()

    out = capsys.readouterr().out
    i_stop = out.find("Stopping current command...")
    i_tree = out.find("Terminating process tree...")
    i_done = out.find("Command stopped")
    assert i_stop >= 0, out
    assert i_tree > i_stop, out
    assert i_done > i_tree, out


def test_a_cancel_with_nothing_running_prints_nothing(capsys):
    """⚠️ Idempotence. Ctrl+C is delivered twice for one physical press (the SIGINT
    handler AND the raw key reader), and ESC can be held down. A sequence printed
    per delivery would tell the user two commands were stopped when there was
    none."""
    from agent2.cli import runtime
    from agent2.cli import state as st

    capsys.readouterr()
    assert runtime.interrupt() is False
    assert runtime.interrupt(is_ctrl_c=True) is False
    out = capsys.readouterr().out
    assert "Stopping current command" not in out
    assert "Command stopped" not in out
    # The flag is still set — the *turn* is cancelled either way.
    assert st.cancelled()


def test_the_second_press_still_prints_nothing_but_arms_the_exit(capsys):
    """The first press cancels; a genuine second press inside the window is the
    documented way out. Neither reprints the sequence."""
    from agent2.cli import runtime
    from agent2.cli import state as st

    runtime.interrupt(is_ctrl_c=True)
    time.sleep(0.1)                       # past the 50 ms duplicate-delivery guard
    capsys.readouterr()
    runtime.interrupt(is_ctrl_c=True)
    assert st.exit_event.is_set()
    assert "Stopping current command" not in capsys.readouterr().out


# ── What a cancel actually does ───────────────────────────────────────────────

def test_the_command_state_is_settled_cancelled():
    """`/api/commands` must stop reporting it as live. A row left STREAMING after
    its process died is the "what is running?" view lying."""
    from agent2.cli import runtime

    t, box = _run_in_thread(QUIET, "t7-state")
    try:
        _await_running("t7-state")
        runtime.interrupt()
        t.join(timeout=30)
    finally:
        from agent2.cli import state as st
        st.cancel_event.clear()

    rows = C.list_all(session_id="t7-state")
    assert len(rows) == 1
    assert rows[0].status == C.CommandStatus.CANCELLED
    assert rows[0].exit_code == 130           # the shell's SIGINT convention
    assert not rows[0].is_active
    assert C.active_count(session_id="t7-state") == 0


def test_the_cancel_is_flagged_and_recorded_before_the_tree_is_killed(monkeypatch):
    """⚠️ FLAG AND RECORD, *THEN* KILL — the load-bearing order of this task.

    `kill_active_proc` blocks until the tree is dead; the moment it dies the pipes
    close and `_run_once` falls out of `drain` and asks what happened. Kill first
    and it finds `cancelled()` False, takes the ordinary path, calls `proc.wait()`
    and settles the execution with the SHELL'S exit code — the record reads
    FAILED / "exit 1" with nothing saying the user cancelled anything, and that
    fabricated failure is what goes back to the model as the tool result.

    This asserts the state visible AT THE MOMENT the kill begins: flag set, row
    already CANCELLED.

    Sabotage check: in `state.trigger_cancel`, move `cancel_event.set()` and
    `stop_active_command(...)`'s record below the kill (call `kill_active_proc()`
    first) and this fails.
    """
    from agent2.cli import runtime
    from agent2.cli import state as st
    from agent2.core import procio

    seen: dict = {}
    real_kill = procio.terminate_tree

    def _spy(proc, **kw):
        # Snapshot the world AS THE KILL BEGINS.
        seen.setdefault("flag", st.cancelled())
        seen.setdefault("statuses",
                        [c.status for c in C.list_all(session_id="t7-order")])
        return real_kill(proc, **kw)

    monkeypatch.setattr(procio, "terminate_tree", _spy)

    t, box = _run_in_thread(QUIET, "t7-order")
    try:
        _await_running("t7-order")
        runtime.interrupt()
        t.join(timeout=30)
    finally:
        st.cancel_event.clear()

    assert "flag" in seen, "terminate_tree was never called"
    assert seen["flag"] is True, "the tree was killed before the cancel flag was set"
    assert seen["statuses"] == [C.CommandStatus.CANCELLED], (
        f"the kill ran before the record: {seen['statuses']}")
    # And the outcome the model sees is a cancel, not a fabricated failure.
    assert box["r"][2] == 130
    assert C.list_all(session_id="t7-order")[0].status == C.CommandStatus.CANCELLED


def test_the_whole_process_tree_dies():
    """⚠️ A cancel that leaves the grandchild running has not cancelled the work,
    it has only stopped watching it. `nmap`, `npm test` and `pytest -n auto` all
    put the real work one level down."""
    from agent2.cli import runtime

    cmd = (f'{sys.executable} -c "import subprocess,sys,time;'
           f"c=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
           'print(c.pid,flush=True);time.sleep(60)"')
    grand: dict = {}
    t, box = _run_in_thread(cmd, "t7-tree")
    try:
        # Wait for the pid line, so the grandchild definitely exists.
        end = time.monotonic() + 30
        while time.monotonic() < end:
            rows = C.list_all(session_id="t7-tree")
            if rows and rows[0].last_output_at:
                break
            time.sleep(0.05)
        runtime.interrupt()
        t.join(timeout=30)
        assert not t.is_alive()

        out = box["r"][0]
        pid = int(next(ln for ln in out.splitlines() if ln.strip().isdigit()).strip())
        grand["pid"] = pid
        end = time.monotonic() + 10
        while time.monotonic() < end and _pid_alive(pid):
            time.sleep(0.1)
        assert not _pid_alive(pid), f"the cancel orphaned pid {pid}"
    finally:
        from agent2.cli import state as st
        st.cancel_event.clear()
        if grand.get("pid"):
            _force_kill(grand["pid"])


def test_the_runner_returns_the_cancel_contract():
    """rc 130 and the note the MODEL reads. Without the note a cancelled command
    looks like one that simply produced little output."""
    from agent2.cli import runtime

    t, box = _run_in_thread(QUIET, "t7-contract")
    try:
        _await_running("t7-contract")
        runtime.interrupt()
        t.join(timeout=30)
    finally:
        from agent2.cli import state as st
        st.cancel_event.clear()

    out, err, rc, dur = box["r"]
    assert rc == 130
    assert "[cancelled by user]" in out
    assert isinstance(err, str)
    assert dur >= 0


def test_a_cancel_between_commands_settles_a_leaked_row():
    """⚠️ The sweep, not a single known id. A Ctrl+C that lands between two tool
    calls never reaches `_run_once`'s cancelled branch, so nothing would settle a
    row the runner already abandoned — and it would read STREAMING for the rest of
    the session."""
    from agent2.cli import state as st

    row = C.create("sleep 999", session_id="t7-leak", surface="cli")
    C.start(row.id, 424242)
    C.heartbeat(row.id)
    assert C.get(row.id).is_active

    ids = st.record_cancel("cancelled by user")

    assert ids == [row.id]
    assert C.get(row.id).status == C.CommandStatus.CANCELLED
    assert C.active_count(session_id="t7-leak") == 0


def test_the_sweep_leaves_other_surfaces_alone():
    """⚠️ Scoped to `surface="cli"`. In DUAL mode one process hosts both surfaces
    and one registry holds both sets of executions: an unscoped sweep would have a
    CLI Ctrl+C cancel the browser's running scan."""
    from agent2.cli import state as st

    web = C.create("nmap -p- target", session_id="t7-web", surface="web")
    C.start(web.id, 999001)
    cli = C.create("sleep 999", session_id="t7-cli", surface="cli")
    C.start(cli.id, 999002)

    st.record_cancel()

    assert C.get(web.id).status != C.CommandStatus.CANCELLED
    assert C.get(web.id).is_active
    assert C.get(cli.id).status == C.CommandStatus.CANCELLED


def test_a_settled_row_is_not_reopened_by_a_later_cancel():
    """A command that finished before the Ctrl+C landed keeps its verdict. The
    guard lives in `_mutate`; this is the Task 7 caller honouring it."""
    from agent2.cli import state as st

    row = C.create("echo done", session_id="t7-done", surface="cli")
    C.start(row.id, 111)
    C.complete(row.id, 0)

    assert st.record_cancel() == []
    assert C.get(row.id).status == C.CommandStatus.COMPLETED
    assert C.get(row.id).exit_code == 0


# ── The session survives ──────────────────────────────────────────────────────

def _drive_listener(monkeypatch, keys: list[str], *, posix: bool):
    """Run ONE of the real listener loops over a scripted key sequence.

    ⚠️ IT DRIVES THE SHIPPED LOOP, and that is the whole point of the rewrite.
    The first version of this test re-implemented the loop's shape in a local
    helper and asserted on that — so it passed happily with `break` back in
    `_listen_posix`. A test that contains its own copy of the code under test
    cannot fail; it is the tautology trap CLAUDE.md warns about, and this file
    walked straight into it.

    The console is the only thing faked. `msvcrt` is injected into `sys.modules`
    (it does not exist off Windows, and needs a real console where it does), and
    the POSIX path gets a fake `select` plus a one-char-at-a-time `stdin` — the
    loop, its ESC branch and its buffer handling are the module's own.

    Returns the queued messages, so a listener that stopped consuming keys is
    visible as a line that never got queued.
    """
    from agent2.cli import runtime

    script = list(keys)
    ctrl = runtime.InputController()

    if posix:
        stdin = SimpleNamespace(
            fileno=lambda: 0,
            read=lambda n: script.pop(0) if script else "",
        )
        monkeypatch.setattr(runtime.sys, "stdin", stdin)
        # Always "ready": the script running dry then reads as EOF, which is the
        # loop's own way out. Reporting "not ready" instead would spin forever.
        monkeypatch.setitem(sys.modules, "select",
                            SimpleNamespace(select=lambda r, w, x, t: (r, [], [])))
        # No termios/tty: the loop already degrades to raw reads when they are
        # unavailable, which is exactly the shape we want under pytest.
        monkeypatch.setitem(sys.modules, "termios", None)
        monkeypatch.setitem(sys.modules, "tty", None)
        ctrl._listen_posix()
    else:
        def _kbhit():
            if script:
                return True
            ctrl._stop.set()      # the Windows loop has no EOF — end it here
            return False
        monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(
            kbhit=_kbhit,
            getwch=lambda: script.pop(0),
        ))
        ctrl._listen_win()

    return ctrl.drain()


@pytest.mark.parametrize("posix", [False, True], ids=["win", "posix"])
def test_esc_does_not_stop_the_key_listener(monkeypatch, posix):
    """⚠️ THE LOAD-BEARING TEST OF THIS TASK.

    Both listener loops used to `break` on ESC/Ctrl+C. The process died, the turn
    ended — and the user could no longer press ESC, type a queued message, or
    answer the stuck prompt for the rest of the turn. "The session remains alive"
    has to mean the keyboard does too.

    Two ESCs with a typed line after each: a listener that breaks out queues the
    first line and nothing else, and never sees the second ESC at all.

    Sabotage check: put `break` back after `interrupt(...)` in `_listen_win` or
    `_listen_posix` and the matching id fails — `["one"] != ["one", "two"]`.
    """
    from agent2.cli import state as st

    queued = _drive_listener(
        monkeypatch,
        ["o", "n", "e", "\r", "\x1b", "t", "w", "o", "\r", "\x1b"],
        posix=posix,
    )
    assert queued == ["one", "two"], (
        f"the listener stopped consuming keys after ESC: {queued}")
    assert st.cancelled(), "ESC no longer cancels"


def test_the_listener_source_has_no_break_on_the_cancel_path():
    """A second angle on the test above: the shipped loops must `continue`.

    Kept alongside the behavioural test because it names the regression directly
    in its failure message, and because it covers the whole ESC branch rather
    than the one path the fake console happens to take.
    """
    import inspect
    from agent2.cli import runtime

    for fn in (runtime.InputController._listen_win,
               runtime.InputController._listen_posix):
        src = inspect.getsource(fn)
        _head, _, tail = src.partition("interrupt(is_ctrl_c=")
        assert tail, f"{fn.__name__} no longer routes ESC through interrupt()"
        # ⚠️ `startswith`, NOT `!= "break"`. The first version compared stripped
        # lines for equality, so a sabotage of `break   # SABOTAGE` — or any
        # trailing comment — sailed past it while the listener really did exit.
        after = [ln.strip() for ln in tail.splitlines()[1:5] if ln.strip()]
        offenders = [ln for ln in after if ln.startswith(("break", "return", "raise"))]
        assert not offenders, f"{fn.__name__} still leaves the loop on ESC: {offenders}"


def test_the_turn_loop_never_exits_on_a_single_ctrl_c():
    """`process_turn` returns to the prompt with history intact; only
    `exit_event` — the double tap — reaches `sys.exit`."""
    import inspect
    import agent2cli as cli
    from agent2.cli import state as st

    src = inspect.getsource(cli.process_turn)
    # The one exit in the loop is guarded by the double-tap event.
    exit_lines = [ln for ln in src.splitlines() if "sys.exit" in ln]
    assert len(exit_lines) == 1, exit_lines
    assert "_exit_event.is_set()" in src
    # And the handler installed for SIGINT is the flag-setting one.
    assert "_interrupt(is_ctrl_c=True)" in src
    assert not st.exit_event.is_set()


def test_a_cancelled_turn_stamps_the_task_checkpoint(monkeypatch):
    """⚠️ Task state is the fourth thing a cancel must clean. Ctrl+C no longer
    raises `KeyboardInterrupt`, so `main()`'s handler can no longer stamp this —
    the flag path in `process_turn` has to, or a resumed session shows a task
    parked with no reason and `/tasks` still calls it RUNNING.

    Sabotage check: delete the `_checkpoint_stop(...)` call from the
    `_cancel_event.is_set()` branch of `process_turn` and this fails.
    """
    import inspect
    import agent2cli as cli

    src = inspect.getsource(cli.process_turn)
    tail = src.partition("if _cancel_event.is_set():")[2]
    assert "_checkpoint_stop(" in tail, "a cancelled turn no longer stamps the task"


# ── The Web half ──────────────────────────────────────────────────────────────

class _Sock:
    """Minimal Socket.IO stand-in — records (event, payload)."""

    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def emit(self, event, payload=None, room=None, **kw):
        self.events.append((event, payload or {}))

    def of(self, name):
        return [p for e, p in self.events if e == name]


def _web_in_thread(cmd: str, sid: str, term_id: str = "t1"):
    from agent2 import terminal
    sock = _Sock()
    box: dict = {}
    t = threading.Thread(
        target=lambda: box.update(
            r=terminal.stream_command(cmd, sid, term_id, sock, session_id=sid)),
        daemon=True)
    t.start()
    return t, box, sock


def _await_web(sid: str, timeout: float = 30.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        rows = C.list_all(session_id=sid)
        if rows and rows[0].process_id:
            return rows[0]
        time.sleep(0.05)
    raise AssertionError(f"no running web command for {sid}")


def test_cancel_sid_ends_a_live_web_command():
    """The browser's Stop button must reach the subprocess, not just the loop's
    stop event. `stop_agent` is read BETWEEN agent iterations — a command already
    inside `stream_command` never sees it."""
    from agent2 import terminal

    t, box, _sock = _web_in_thread(QUIET, "t7-websid")
    _await_web("t7-websid")

    settled = terminal.cancel_sid("t7-websid", "stopped by user")
    t.join(timeout=30)

    assert not t.is_alive(), "stream_command did not return after cancel_sid"
    assert len(settled) == 1
    row = C.list_all(session_id="t7-websid")[0]
    assert row.status == C.CommandStatus.CANCELLED
    assert row.error == "stopped by user"
    assert terminal.get_proc("t7-websid", "t1") is None


def test_cancel_sid_records_every_pane_before_killing_any():
    """⚠️ All the records first, then all the kills. `terminate_tree` blocks for up
    to a second; pairing record-and-kill per pane would leave the second pane's row
    unwritten for that whole second — long enough for its own streaming thread to
    settle it as "exit 1" instead."""
    from agent2 import terminal
    from agent2.core import procio

    seen: dict = {"snapshots": []}
    real_kill = procio.terminate_tree

    def _spy(proc, **kw):
        seen["snapshots"].append(
            [c.status for c in C.list_all(session_id="t7-multi")])
        return real_kill(proc, **kw)

    t1, _b1, _s1 = _web_in_thread(QUIET, "t7-multi", "p1")
    t2, _b2, _s2 = _web_in_thread(QUIET, "t7-multi", "p2")
    # ⚠️ Wait for both HANDLES, not both rows. The row is created before the spawn
    # (so a Popen that raises is still a visible execution), so a row can exist
    # while `store_proc` has not run yet — and `cancel_sid` walks the handles.
    end = time.monotonic() + 30
    while time.monotonic() < end:
        if all(terminal.get_proc("t7-multi", p) for p in ("p1", "p2")):
            break
        time.sleep(0.05)

    import pytest as _pytest
    mp = _pytest.MonkeyPatch()
    mp.setattr(procio, "terminate_tree", _spy)
    try:
        settled = terminal.cancel_sid("t7-multi", "stopped by user")
    finally:
        mp.undo()
    t1.join(timeout=30)
    t2.join(timeout=30)

    assert len(settled) == 2
    assert seen["snapshots"], "terminate_tree was never called"
    # By the time the FIRST kill starts, both rows are already CANCELLED.
    assert set(seen["snapshots"][0]) == {C.CommandStatus.CANCELLED}, seen["snapshots"][0]


def test_cancel_sid_is_a_no_op_for_an_unknown_session():
    """Nothing to cancel is success, not an error — a cancel must never raise."""
    from agent2 import terminal
    assert terminal.cancel_sid("no-such-sid") == []


def test_a_web_disconnect_ends_the_command():
    """⚠️ Closing the tab mid-`nmap` used to leave the whole tree running with the
    one reference to it discarded — `cleanup_sid` forgets handles, it kills
    nothing — and the execution ACTIVE forever.

    Sabotage check: remove `_term_cancel(sid, ...)` from `on_disconnect` and this
    fails: the row is still active and the process is still alive.
    """
    import inspect
    from agent2 import terminal
    from agent2.server import sockets

    # The wiring: `_term_cancel` runs, and it runs BEFORE `_term_cleanup` — after
    # the cleanup there would be no handle left to kill.
    src = inspect.getsource(sockets.register_sockets)
    body = src.partition("def on_disconnect():")[2].partition("@socketio.on")[0]
    assert "_term_cancel(" in body, "a disconnect no longer cancels live commands"
    assert body.index("_term_cancel(") < body.index("_term_cleanup("), body

    # The behaviour, exercised through the same call the handler makes.
    t, _box, _sock = _web_in_thread(QUIET, "t7-disc")
    row = _await_web("t7-disc")
    pid = row.process_id
    terminal.cancel_sid("t7-disc", "browser disconnected")
    terminal.cleanup_sid("t7-disc")
    t.join(timeout=30)

    assert C.list_all(session_id="t7-disc")[0].status == C.CommandStatus.CANCELLED
    end = time.monotonic() + 10
    while time.monotonic() < end and _pid_alive(pid):
        time.sleep(0.1)
    assert not _pid_alive(pid), f"the disconnect orphaned pid {pid}"


def test_stop_agent_wires_the_command_cancel():
    """The Stop button's handler pairs the scheduler/session cancels with the
    command cancel. All four halves, or the user is told something stopped that
    did not."""
    import inspect
    from agent2.server import sockets

    src = inspect.getsource(sockets.register_sockets)
    body = src.partition("def on_stop_agent(data):")[2].partition("@socketio.on")[0]
    for needed in ("scheduler.cancel(", "sessions.cancel(", "_term_stop(",
                   "_term_cancel(", "_checkpoint_stop(", "agent_stopped"):
        assert needed in body, f"on_stop_agent no longer calls {needed}"
