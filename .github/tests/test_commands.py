# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
Tests for the command execution manager (``agent2/core/commands.py``) and the two
runners that feed it (``agent2/terminal.py`` for Web, ``agent2/cli/runtime.py``
for the CLI).

Run from the repo root:  python -m pytest .github/tests/test_commands.py -v

Coverage (Task 4)
  - the lifecycle: CREATED → STARTING → RUNNING → STREAMING → COMPLETED
  - the four failure states: FAILED, TIMEOUT, CANCELLED, KILLED
  - every tracked field: command_id, process_id, task_id, started_at,
    last_output_at, timeout, status, exit_code, stdout, stderr
  - the terminal-state guard (a settled execution is never rewritten)
  - `idle` vs `elapsed`, including the silent-command case
  - real subprocesses through BOTH runners, and a real kill
  - the existing return contracts are untouched (2-tuple / 4-tuple)

⚠️ The load-bearing tests here are
``test_a_settled_execution_is_never_rewritten`` (a kill racing a normal exit must
not be relabelled "exit 0") and ``test_idle_grows_for_a_silent_command`` (elapsed
time cannot tell a slow build from a hung one — only time-since-output can).
"""

import threading
import time

import pytest

from agent2.core import commands as C


@pytest.fixture(autouse=True)
def _clean():
    """Each test starts with an empty registry — it is process-global state."""
    C.reset()
    yield
    C.reset()


# ── The state vocabulary ──────────────────────────────────────────────────────

def test_the_spec_states_all_exist():
    """The exact names the Task 4 spec lists, no renaming."""
    assert C.CommandStatus.CREATED == "created"
    assert C.CommandStatus.STARTING == "starting"
    assert C.CommandStatus.RUNNING == "running"
    assert C.CommandStatus.STREAMING == "streaming"
    assert C.CommandStatus.COMPLETED == "completed"
    for name in ("FAILED", "TIMEOUT", "CANCELLED", "KILLED"):
        assert hasattr(C.CommandStatus, name), name


def test_terminal_and_active_sets_partition_the_lifecycle():
    """Every status is either active, terminal, or a pre-spawn state — no gaps."""
    pre = {C.CommandStatus.CREATED}
    assert C.TERMINAL | C.ACTIVE | pre == set(C.ALL_STATUSES)
    assert not (C.TERMINAL & C.ACTIVE)
    # FAILURE is a strict subset of TERMINAL: "ended" and "ended badly" differ.
    assert C.FAILURE < C.TERMINAL
    assert C.CommandStatus.COMPLETED not in C.FAILURE


# ── Tracked fields ────────────────────────────────────────────────────────────

def test_every_field_the_spec_names_is_tracked():
    cmd = C.create("echo hi", task_id="t1", session_id="s1", timeout=30)
    for field in ("id", "process_id", "task_id", "started_at", "last_output_at",
                  "timeout", "status", "exit_code", "stdout", "stderr"):
        assert hasattr(cmd, field), field
    assert cmd.command == "echo hi"
    assert cmd.task_id == "t1"
    assert cmd.session_id == "s1"
    assert cmd.timeout == 30.0
    assert cmd.status == C.CommandStatus.CREATED
    # Not started yet: no pid, no exit code, no timestamps to lie about.
    assert cmd.process_id is None
    assert cmd.exit_code is None
    assert cmd.started_at == ""
    assert cmd.last_output_at == ""


def test_command_ids_are_unique():
    ids = {C.create(f"cmd {i}").id for i in range(50)}
    assert len(ids) == 50


def test_a_zero_or_none_timeout_is_stored_as_no_limit():
    """0 means "no ceiling", not "expire immediately" — the latter would kill
    every command the moment a caller passed a falsy default through."""
    assert C.create("a", timeout=0).timeout is None
    assert C.create("b", timeout=None).timeout is None
    assert C.create("c", timeout=-5).timeout is None
    assert C.create("d", timeout=2.5).timeout == 2.5


# ── The happy path ────────────────────────────────────────────────────────────

def test_the_full_lifecycle_in_order():
    cmd = C.create("build", timeout=60)
    assert cmd.status == C.CommandStatus.CREATED

    assert C.starting(cmd.id).status == C.CommandStatus.STARTING

    running = C.start(cmd.id, process_id=4242)
    assert running.status == C.CommandStatus.RUNNING
    assert running.process_id == 4242
    assert running.started_at                      # stamped
    assert running.last_output_at == ""            # nothing said yet

    streaming = C.heartbeat(cmd.id)
    assert streaming.status == C.CommandStatus.STREAMING
    assert streaming.last_output_at
    assert streaming.output_lines == 1

    done = C.complete(cmd.id, 0, stdout="ok\n")
    assert done.status == C.CommandStatus.COMPLETED
    assert done.exit_code == 0
    assert done.stdout == "ok\n"
    assert done.completed_at


def test_a_nonzero_exit_is_failed_not_completed():
    """The exit code decides, not the caller — otherwise "did it work?" is
    unanswerable from the registry."""
    cmd = C.create("false")
    C.start(cmd.id, 1)
    out = C.complete(cmd.id, 1, stderr="boom")
    assert out.status == C.CommandStatus.FAILED
    assert out.exit_code == 1
    assert out.stderr == "boom"
    assert out.failed


def test_heartbeat_counts_lines_and_keeps_streaming():
    cmd = C.create("tail -f log")
    C.start(cmd.id, 7)
    for _ in range(5):
        C.heartbeat(cmd.id)
    assert C.get(cmd.id).output_lines == 5
    assert C.get(cmd.id).status == C.CommandStatus.STREAMING


# ── Failure states ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("fn,expected", [
    (C.cancel, C.CommandStatus.CANCELLED),
    (C.timed_out, C.CommandStatus.TIMEOUT),
    (C.killed, C.CommandStatus.KILLED),
])
def test_each_failure_state_is_reachable_and_records_why(fn, expected):
    cmd = C.create("sleep 999")
    C.start(cmd.id, 99)
    out = fn(cmd.id)
    assert out.status == expected
    assert out.is_terminal
    assert out.failed
    assert out.error, "a failure state with no reason tells the user nothing"
    assert out.completed_at


def test_cancel_records_the_shell_sigint_convention():
    cmd = C.create("sleep 999")
    C.start(cmd.id, 99)
    assert C.cancel(cmd.id).exit_code == 130


def test_a_spawn_failure_is_a_visible_failed_execution():
    """Registered BEFORE the Popen, so a command that could not start still has
    a record — otherwise the most common hard failure leaves no trace at all."""
    cmd = C.create("nosuchbinary --x")
    out = C.fail(cmd.id, "FileNotFoundError: nosuchbinary")
    assert out.status == C.CommandStatus.FAILED
    assert out.exit_code == -1
    assert "nosuchbinary" in out.error
    assert out.started_at == ""       # never ran; do not pretend it did


# ── The terminal-state guard ──────────────────────────────────────────────────

def test_a_settled_execution_is_never_rewritten():
    """⚠️ Load-bearing. `kill_proc` and the streaming thread both settle the same
    execution; whichever lands first is the truth. Without this guard a kill
    would be silently relabelled "exit 0" milliseconds later, and the transcript
    would claim a command succeeded that the user deliberately stopped."""
    cmd = C.create("sleep 999")
    C.start(cmd.id, 1234)
    C.killed(cmd.id, reason="user hit stop")

    # Everything the streaming thread might try next.
    C.complete(cmd.id, 0, stdout="finished!")
    C.heartbeat(cmd.id)
    C.cancel(cmd.id)
    C.timed_out(cmd.id)

    final = C.get(cmd.id)
    assert final.status == C.CommandStatus.KILLED
    assert final.error == "user hit stop"
    assert final.stdout == "", "a settled execution must not absorb later output"


def test_transitions_on_an_unknown_id_return_none_and_do_not_raise():
    """Bookkeeping may never be the reason a command fails to run, so every
    writer is safe to call fire-and-forget."""
    for fn in (C.starting, C.heartbeat, C.cancel, C.timed_out, C.killed):
        assert fn("nope") is None
    assert C.start("nope", 1) is None
    assert C.complete("nope", 0) is None
    assert C.fail("nope", "x") is None
    assert C.get("nope") is None
    assert C.get("") is None


def test_a_reader_cannot_mutate_the_registry_through_its_snapshot():
    cmd = C.create("echo hi")
    snap = C.get(cmd.id)
    snap.status = C.CommandStatus.COMPLETED
    snap.command = "rm -rf /"
    fresh = C.get(cmd.id)
    assert fresh.status == C.CommandStatus.CREATED
    assert fresh.command == "echo hi"


# ── elapsed vs idle ───────────────────────────────────────────────────────────

def test_idle_grows_for_a_silent_command():
    """⚠️ Load-bearing. This is the whole reason `last_output_at` exists: a
    command that has printed nothing has been silent for its entire life, and an
    `idle` of 0.0 there would blind every later reader to the worst case."""
    cmd = C.create("hang")
    C.start(cmd.id, 5)
    time.sleep(0.05)
    live = C.get(cmd.id)
    assert live.last_output_at == ""
    assert live.idle > 0
    assert live.idle == pytest.approx(live.elapsed, abs=0.02)


def test_a_heartbeat_resets_idle_but_not_elapsed():
    cmd = C.create("chatty")
    C.start(cmd.id, 5)
    time.sleep(0.06)
    C.heartbeat(cmd.id)
    live = C.get(cmd.id)
    assert live.elapsed >= 0.05
    assert live.idle < live.elapsed


def test_elapsed_freezes_once_settled():
    """A finished command's duration must not keep ticking up every time the
    dashboard is refreshed."""
    cmd = C.create("quick")
    C.start(cmd.id, 5)
    C.complete(cmd.id, 0)
    first = C.get(cmd.id).elapsed
    time.sleep(0.05)
    assert C.get(cmd.id).elapsed == pytest.approx(first, abs=1e-6)


def test_an_unstarted_command_reports_no_elapsed_or_idle():
    cmd = C.get(C.create("never ran").id)
    assert cmd.elapsed == 0.0
    assert cmd.idle == 0.0


# ── Reads ─────────────────────────────────────────────────────────────────────

def test_list_active_excludes_settled_and_unstarted_work():
    live = C.create("live")
    C.start(live.id, 1)
    done = C.create("done")
    C.start(done.id, 2)
    C.complete(done.id, 0)
    C.create("not spawned yet")

    ids = [c.id for c in C.list_active()]
    assert ids == [live.id]


def test_reads_filter_by_session_task_and_surface():
    a = C.create("a", session_id="s1", task_id="t1", surface="cli")
    b = C.create("b", session_id="s1", task_id="t2", surface="web")
    c = C.create("c", session_id="s2", task_id="t1", surface="web")
    for cmd in (a, b, c):
        C.start(cmd.id, 1)

    assert {x.id for x in C.list_all(session_id="s1")} == {a.id, b.id}
    assert {x.id for x in C.list_all(task_id="t1")} == {a.id, c.id}
    assert {x.id for x in C.list_all(surface="web")} == {b.id, c.id}
    assert C.active_count(session_id="s1") == 2


def test_list_all_is_newest_first_and_respects_limit():
    ids = []
    for i in range(5):
        ids.append(C.create(f"c{i}").id)
        time.sleep(0.001)
    got = [c.id for c in C.list_all(limit=3)]
    assert got == list(reversed(ids))[:3]


def test_snapshot_is_json_safe_and_counts_correctly():
    import json
    live = C.create("live", session_id="s1")
    C.start(live.id, 1)
    bad = C.create("bad", session_id="s1")
    C.start(bad.id, 2)
    C.complete(bad.id, 3)

    snap = C.snapshot(session_id="s1")
    # Exact, not a subset: the wire shape is a contract two surfaces read. Task 6
    # added `stuck` (a live command past the reporting threshold) — a fresh one is
    # never stuck, so the count is 0 here.
    assert snap["counts"] == {"total": 2, "active": 1, "failed": 1, "stuck": 0}
    assert [c["id"] for c in snap["active"]] == [live.id]
    json.dumps(snap)          # must survive the Flask jsonify path


def test_to_dict_exposes_idle_and_elapsed_for_the_dashboard():
    cmd = C.create("build", task_id="t9", timeout=120)
    C.start(cmd.id, 321)
    C.heartbeat(cmd.id)
    d = C.get(cmd.id).to_dict()
    assert d["process_id"] == 321
    assert d["task_id"] == "t9"
    assert d["timeout"] == 120
    assert d["status"] == C.CommandStatus.STREAMING
    assert d["active"] is True
    assert isinstance(d["idle"], float)
    assert isinstance(d["elapsed"], float)


# ── Housekeeping ──────────────────────────────────────────────────────────────

def test_pruning_never_evicts_an_active_execution():
    """⚠️ A stuck process is the single most valuable row in the table. Evicting
    it to stay under the ceiling would delete the evidence the manager exists to
    keep."""
    stuck = C.create("hung forever")
    C.start(stuck.id, 1)
    for i in range(C.MAX_KEPT + 40):
        done = C.create(f"noise {i}")
        C.start(done.id, 2)
        C.complete(done.id, 0)

    assert len(C._registry) <= C.MAX_KEPT
    assert C.get(stuck.id) is not None
    assert C.get(stuck.id).status == C.CommandStatus.RUNNING


def test_forget_and_reset():
    cmd = C.create("x")
    assert C.forget(cmd.id) is True
    assert C.forget(cmd.id) is False
    C.create("y")
    C.reset()
    assert C.list_all() == []


def test_long_output_is_truncated_not_unbounded():
    cmd = C.create("x" * 9000)
    assert len(C.get(cmd.id).command) == C.MAX_COMMAND
    C.start(cmd.id, 1)
    C.complete(cmd.id, 0, stdout="o" * 99_000, stderr="e" * 99_000)
    out = C.get(cmd.id)
    assert len(out.stdout) == C.MAX_SNAPSHOT
    assert len(out.stderr) == C.MAX_SNAPSHOT


def test_concurrent_writers_do_not_lose_heartbeats():
    """Three writers per row is the real shape: the streaming thread, a socket
    handler, and the CLI foreground."""
    cmd = C.create("parallel")
    C.start(cmd.id, 1)

    def beat():
        for _ in range(200):
            C.heartbeat(cmd.id)

    threads = [threading.Thread(target=beat) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert C.get(cmd.id).output_lines == 800


# ── The runners feed the registry ─────────────────────────────────────────────
# Real subprocesses, through the same entry points the Web and CLI surfaces use,
# asserting both that the registry sees the full lifecycle and that the public
# return contracts (2-tuple / 4-tuple) are untouched by Task 4.

def test_terminal_stream_command_tracks_and_streams(monkeypatch):
    """The Web runner: a real command, and every event a page would receive."""
    from agent2 import terminal

    events = []

    class FakeSocketIO:
        def emit(self, name, payload, room=""):
            events.append((name, payload))

    captured = {}

    # Wrap the REAL functions captured up-front. `terminal._commands` IS the
    # module object `C`, so a spy that called `C.start` by name would call
    # itself — the patch rebinds the very attribute it looks up.
    _real_start, _real_complete = C.start, C.complete

    def fake_start(command_id, process_id=None):
        captured["pid"] = process_id
        return _real_start(command_id, process_id)

    def fake_complete(command_id, exit_code, stdout="", stderr=""):
        captured["exit"] = exit_code
        captured["stdout"] = stdout
        return _real_complete(command_id, exit_code, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(C, "start", fake_start)
    monkeypatch.setattr(C, "complete", fake_complete)

    out, rc = terminal.stream_command(
        "python -c \"import sys; print('hello'); sys.exit(0)\"",
        "sid-1", "t1", FakeSocketIO(), task_id="task-9", session_id="sess-9")

    assert rc == 0
    assert "hello" in out
    names = [n for n, _ in events]
    assert "terminal_start" in names
    assert "proc_started" in names
    assert "terminal_done" in names
    assert "chat_cmd_result" in names

    rec = C.list_all(session_id="sess-9")
    assert len(rec) == 1
    assert rec[0].task_id == "task-9"
    assert rec[0].surface == "web"
    assert rec[0].status == C.CommandStatus.COMPLETED
    assert rec[0].exit_code == 0
    assert "hello" in rec[0].stdout
    assert captured["exit"] == 0
    assert isinstance(captured["pid"], int)


def test_terminal_stream_command_records_a_nonzero_exit_as_failed():
    """A command the shell CAN start but that fails: the shell itself exits
    non-zero, so this is a completed-with-failure, not a spawn error. The
    distinction matters — conflating them would hide "your tool is missing"
    behind "Agent2 could not run anything"."""
    from agent2 import terminal

    class FakeSocketIO:
        def emit(self, name, payload, room=""):
            pass

    out, rc = terminal.stream_command("definitely-not-a-real-command-xyz",
                                      "sid-2", "t1", FakeSocketIO())
    assert rc != 0
    rec = C.list_all(surface="web")
    assert rec and rec[0].status == C.CommandStatus.FAILED
    assert rec[0].exit_code == rc
    assert rec[0].process_id, "the shell really did start — record its pid"


def test_terminal_stream_command_records_a_spawn_failure(monkeypatch):
    """The real spawn-error path: the workspace directory is gone, so Popen
    raises. Registered BEFORE the spawn means this still leaves a record —
    without it, the most confusing failure mode produces no trace at all."""
    from agent2 import terminal
    from agent2.core import workspace

    monkeypatch.setattr(workspace, "root", lambda: "Z:/no-such-dir-xyz-123")

    class FakeSocketIO:
        def emit(self, name, payload, room=""):
            pass

    out, rc = terminal.stream_command("echo hi", "sid-3", "t1", FakeSocketIO())
    assert rc == -1
    assert out, "the caller must be told why, not handed an empty string"
    rec = C.list_all(surface="web")
    assert rec and rec[0].status == C.CommandStatus.FAILED
    assert rec[0].exit_code == -1
    assert rec[0].error
    assert rec[0].started_at == ""       # it never ran; don't pretend it did


def test_cli_run_cmd_stream_tracks_separated_streams():
    """The CLI runner: stdout/stderr are separated (the result card needs that),
    and the registry records them distinctly."""
    from agent2.cli import runtime

    script = ("import sys\n"
              "sys.stdout.write('to stdout\\n')\n"
              "sys.stderr.write('to stderr\\n')\n")
    out, err, rc, dur = runtime.run_cmd_stream(
        f'python -c "{script}"', task_id="t5", session_id="s5")

    assert rc == 0
    assert "to stdout" in out
    assert "to stderr" in err
    assert dur >= 0

    rec = C.list_all(session_id="s5")
    assert len(rec) == 1
    assert rec[0].surface == "cli"
    assert rec[0].task_id == "t5"
    assert rec[0].status == C.CommandStatus.COMPLETED
    assert "to stdout" in rec[0].stdout
    assert "to stderr" in rec[0].stderr


def test_cli_run_cmd_stream_cancel_is_recorded():
    """ESC during a command: the CLI returns 130 (its long-standing contract)
    and the registry says CANCELLED, not COMPLETED."""
    from agent2.cli import runtime
    from agent2.cli import state

    # A command long enough that the cancel check reliably fires mid-run.
    script = ("import time\n"
              "for _ in range(200):\n"
              "    print('tick', flush=True)\n"
              "    time.sleep(0.01)\n")
    try:
        state.trigger_cancel(False)          # ESC-press semantics
        out, err, rc, dur = runtime.run_cmd_stream(
            f'python -c "{script}"', session_id="s6")
    finally:
        state.cancel_event.clear()

    assert rc == 130
    assert "[cancelled by user]" in out
    rec = C.list_all(session_id="s6")
    assert rec and rec[0].status == C.CommandStatus.CANCELLED
    assert rec[0].exit_code == 130


def test_cli_run_cmd_stream_reports_a_spawn_error():
    """A shell that cannot even be started returns (-1, message) — degraded,
    never raised — and the execution is recorded FAILED with the reason.

    An invalid cwd is used because it fails in `Popen` itself. A merely unknown
    *command* is a different thing: the shell starts fine and exits non-zero.
    """
    from agent2.cli import runtime

    out, err, rc, dur = runtime.run_cmd_stream("echo hi", cwd="Z:/no-such-dir-xyz-123")
    assert rc == -1
    assert out and isinstance(out, str)
    rec = C.list_all(surface="cli")
    assert rec and rec[0].status == C.CommandStatus.FAILED
    assert rec[0].exit_code == -1
    assert rec[0].error
    assert rec[0].started_at == ""


def test_cli_run_cmd_stream_records_an_unknown_command_as_failed():
    """The other half of the pair above: the shell ran, the command did not
    exist, so the exit code (not an exception) is what marks it failed."""
    from agent2.cli import runtime

    out, err, rc, dur = runtime.run_cmd_stream("no-such-command-abc-123")
    assert rc != 0
    rec = C.list_all(surface="cli")
    assert rec and rec[0].status == C.CommandStatus.FAILED
    assert rec[0].exit_code == rc
    assert rec[0].process_id


# ── Real kill through the registry (Task 7's substrate) ───────────────────────

def test_kill_proc_records_killed_and_the_runner_does_not_overwrite():
    """The full race, live: kill_proc settles the execution while the streaming
    thread is mid-run; the thread's later complete() must lose."""
    import subprocess as _sp

    from agent2 import terminal
    from agent2.cli import runtime

    # Spin a child that outlives any sensible test window, then kill it via the
    # SAME socket-level path a browser kill button uses.
    proc = _sp.Popen(
        runtime.shell_argv("python -c \"import time; time.sleep(60)\""),
        stdin=_sp.PIPE, stdout=_sp.PIPE, stderr=_sp.STDOUT, text=True,
    )
    try:
        cmd_id = C.create("sleep 60", session_id="s-kill").id
        terminal.store_proc("sid-k", "t1", proc, command_id=cmd_id)
        C.start(cmd_id, proc.pid)
        C.heartbeat(cmd_id)

        terminal.kill_proc("sid-k", "t1")

        rec = C.get(cmd_id)
        assert rec.status == C.CommandStatus.KILLED
        assert rec.is_terminal
        # The racer: what the streaming thread would call once the pipe closes.
        C.complete(cmd_id, 0, stdout="won the race")
        assert C.get(cmd_id).status == C.CommandStatus.KILLED
        assert C.get(cmd_id).stdout == "", "posthumous output must not be absorbed"
        # `terminate()` is asynchronous — it asks, it does not wait. Poll for the
        # exit rather than asserting on it immediately; asserting instantly would
        # be a flaky test, not a stronger one. (Making the WAIT itself part of
        # the kill contract, and reaping the whole process tree, is Task 7.)
        proc.wait(timeout=10)
        assert proc.poll() is not None       # the child actually died
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)
        terminal.del_proc("sid-k", "t1")
