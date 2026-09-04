# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
Tests for command timeout, heartbeat and the stuck prompt (Task 6) —
``agent2/core/commands.py`` (`watch`/`defer`/`live_line`), the CLI runner
(``agent2/cli/runtime.py``) and the Web runner (``agent2/terminal.py``).

Run from the repo root:  python -m pytest .github/tests/test_cmd_timeout.py -v

Coverage
  - `watch()` decides and NEVER acts (no process is touched, nothing is settled)
  - execution timeout, idle timeout, and which one wins when both are due
  - the stuck REPORT is on by default while the two kills are off
  - `defer()` — [W]ait moves both ceilings, so a timeout cannot overrule the user
  - `live_line()` renders the spec's "Elapsed: 32s · Last output: 2s ago"
  - a real timed-out command: process tree gone, status TIMEOUT, rc 124
  - a real idle-timed-out command that is still ALIVE and merely quiet
  - the stuck prompt does not block the drain (output keeps flowing while armed)
  - [K] kills, [R] re-runs exactly once more, [W] extends and the command finishes
  - the agent is never frozen: a stuck command still ticks
  - the Web surface emits heartbeat/stuck/unstuck and honours the same ceilings

⚠️ The load-bearing tests here are
``test_the_stuck_prompt_does_not_block_the_drain`` (a blocking prompt would turn a
suspected hang into a real one), ``test_a_timeout_kills_the_whole_process_tree``
(a timeout that leaves grandchildren running has not stopped the work) and
``test_wait_defers_both_ceilings`` (a [W]ait that a pending timeout overrules is
not a choice at all).
"""

import re
import subprocess
import sys
import threading
import time

import pytest

from agent2 import config as cfg
from agent2.core import commands as C

# A long-lived, deliberately silent child: the shape every stuck-detection test
# needs. One level of quoting only — the shell parses this line, and nesting a
# quoted interpreter path inside it is a parse error on PowerShell.
QUIET = 'python -c "import time;time.sleep(60)"'


@pytest.fixture(autouse=True)
def _clean():
    from agent2.cli import state as _st
    C.reset()
    _st.clear_key_handler()
    yield
    _st.clear_key_handler()
    C.reset()


def _press_when_armed(*keys: str, timeout: float = 40.0) -> threading.Thread:
    """Deliver *keys* the way a real user does — through `state.dispatch_key`, the
    ONE key path the CLI has — one key per time the stuck prompt arms.

    Deliberately not a call into `_StuckPrompt._on_key`: the point of these tests
    is that the offer is reachable from the existing listener. A key per arming
    matters for [R], where the retry attempt puts a fresh prompt on screen and a
    single press would leave the re-run to sit out its full 60 s.
    """
    from agent2.cli import state as st

    def _go() -> None:
        end = time.monotonic() + timeout
        for key in keys:
            while time.monotonic() < end:
                if st.S.key_handler is not None:
                    st.dispatch_key(key)
                    break
                time.sleep(0.02)
            # Wait for this prompt to go away before answering the next one.
            while time.monotonic() < end and st.S.key_handler is not None:
                time.sleep(0.02)

    t = threading.Thread(target=_go, daemon=True)
    t.start()
    return t


@pytest.fixture()
def quick_stuck(monkeypatch):
    """Shrink the reporting threshold so a test does not have to be quiet for the
    shipped 20 s. Both surfaces read `config` live, so one patch covers them."""
    monkeypatch.setattr(cfg, "CMD_STUCK_AFTER", 1.0)
    monkeypatch.setattr(cfg, "CMD_HEARTBEAT_SEC", 0.3)
    return cfg


def _pid_alive(pid: int) -> bool:
    if sys.platform.startswith("win"):
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"],
                             capture_output=True, text=True, timeout=20).stdout
        return str(pid) in out
    try:
        import os
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _wait_for(pred, timeout=30.0, step=0.05):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        got = pred()
        if got:
            return got
        time.sleep(step)
    return None


# ── watch(): decide, never act ────────────────────────────────────────────────

def test_watch_returns_a_word_and_touches_nothing():
    """⚠️ The whole reason `watch` is safe to call from a streaming loop.

    It must not settle the execution, and it must not know how to kill: the
    decision lives with the state, the action lives with the runner that owns the
    handle. Sabotage check: make `watch` call `timed_out()` itself and the status
    assertion below fails.
    """
    c = C.create("sleep 60", timeout=0.01)
    C.start(c.id, 4321)
    time.sleep(0.05)

    assert C.watch(c.id) == C.W_TIMEOUT
    # Still streaming — the verdict is advice, not an act.
    assert C.get(c.id).status in C.ACTIVE
    assert C.get(c.id).exit_code is None
    # And it is idempotent: asking twice does not double-report or mutate.
    assert C.watch(c.id) == C.W_TIMEOUT
    assert C.get(c.id).status in C.ACTIVE


def test_watch_is_ok_while_inside_both_ceilings():
    c = C.create("build", timeout=60, idle_timeout=60)
    C.start(c.id, 1)
    assert C.watch(c.id) == C.W_OK


def test_watch_ignores_unstarted_and_settled_executions():
    """A command with no process cannot be stuck, and a finished one cannot be
    stuck either — both would be false alarms the user has to dismiss."""
    c = C.create("x", timeout=0.001)
    time.sleep(0.02)
    assert C.watch(c.id) == C.W_OK          # never started
    C.start(c.id, 1)
    C.complete(c.id, 0)
    time.sleep(0.02)
    assert C.watch(c.id) == C.W_OK          # already settled
    assert C.watch("no-such-id") == C.W_OK  # and unknown ids never raise


def test_idle_timeout_fires_on_silence_not_on_total_runtime():
    """The distinction the spec draws: a command may run for hours as long as it
    keeps talking, and 20 seconds of silence can be fatal."""
    c = C.create("npm test", idle_timeout=0.05)
    C.start(c.id, 1)
    for _ in range(4):
        time.sleep(0.03)
        C.heartbeat(c.id)
        assert C.watch(c.id) == C.W_OK      # chatty: never idle-timed-out
    time.sleep(0.08)
    assert C.watch(c.id) == C.W_IDLE


def test_the_execution_timeout_wins_when_both_are_due():
    """Reported cause matters: the hard ceiling is the more specific explanation,
    and 'no output for 1s' on a command that also blew a 1s total limit reads as
    the wrong diagnosis."""
    c = C.create("x", timeout=0.01, idle_timeout=0.01)
    C.start(c.id, 1)
    time.sleep(0.05)
    assert C.watch(c.id) == C.W_TIMEOUT


def test_stuck_is_reported_before_any_ceiling_is_reached():
    """⚠️ With no timeouts set at all, silence still gets REPORTED.

    This is the answer to "do not solve this merely by adding a timeout": the
    default behaviour is to tell the user, not to kill their build.
    """
    c = C.create("pip install torch")           # no timeout, no idle_timeout
    C.start(c.id, 1)
    time.sleep(0.06)
    assert C.watch(c.id, stuck_after=0.05) == C.W_STUCK
    # …and it is still just a report: nothing was settled.
    assert C.get(c.id).status in C.ACTIVE


def test_stuck_reporting_can_be_switched_off():
    c = C.create("x")
    C.start(c.id, 1)
    time.sleep(0.06)
    assert C.watch(c.id, stuck_after=0) == C.W_OK


def test_the_shipped_defaults_report_but_do_not_kill():
    """⚠️ The defaults are the policy. A default timeout would kill legitimate
    long commands (docker build, pip install torch, a 40-minute nmap) with no
    user involvement, so both kills ship OFF and only the report ships ON."""
    assert cfg.CMD_TIMEOUT == 0
    assert cfg.CMD_IDLE_TIMEOUT == 0
    assert cfg.CMD_STUCK_AFTER > 0
    assert cfg.CMD_MAX_RETRIES >= 1
    assert cfg.CMD_WAIT_GRACE > 0


def test_wait_defers_both_ceilings():
    """⚠️ [W]ait must be a real answer. `defer` moves the ceilings forward, so a
    timeout that was already due cannot fire a tick later and overrule the person
    who just asked to keep waiting.

    Sabotage check: make `defer` a no-op and this test fails on the first assert.
    """
    c = C.create("x", timeout=0.01, idle_timeout=0.01)
    C.start(c.id, 1)
    time.sleep(0.05)
    assert C.watch(c.id) == C.W_TIMEOUT

    C.defer(c.id, 30)
    assert C.watch(c.id) == C.W_OK
    snap = C.get(c.id)
    assert snap.timeout > 30 and snap.idle_timeout > 30
    # The clock itself is untouched: this defers the verdict, it does not lie
    # about how long the command has been running.
    assert snap.elapsed >= 0.05


def test_defer_also_clears_a_pending_stuck_report():
    c = C.create("x")
    C.start(c.id, 1)
    time.sleep(0.06)
    assert C.watch(c.id, stuck_after=0.05) == C.W_STUCK
    C.defer(c.id, 30)
    assert C.watch(c.id, stuck_after=0.05) == C.W_OK


def test_defer_never_revives_a_settled_execution():
    """Rule: whatever settled first wins. A [W]ait arriving after the command
    already timed out must not put it back into the running set."""
    c = C.create("x", timeout=1)
    C.start(c.id, 1)
    C.timed_out(c.id, reason="gone")
    C.defer(c.id, 60)
    assert C.get(c.id).status == C.CommandStatus.TIMEOUT
    assert C.get(c.id) not in C.list_active()


# ── The live line the spec asks for ───────────────────────────────────────────

def test_live_line_matches_the_spec_wording():
    """The spec's mockup, verbatim in shape:
        Elapsed: 32s
        Last output: 2s ago
    """
    c = C.create("x")
    C.start(c.id, 1)
    C.heartbeat(c.id)
    line = C.live_line(C.get(c.id))
    assert re.search(r"Elapsed: \d+", line), line
    assert "Last output:" in line and line.rstrip().endswith("ago"), line


def test_live_line_says_no_output_yet_rather_than_zero():
    """A command that has produced NOTHING is the interesting case; printing
    'Last output: 0s ago' for it would be a lie in the reassuring direction."""
    c = C.create("x")
    C.start(c.id, 1)
    time.sleep(0.02)
    assert "no output yet" in C.live_line(C.get(c.id))
    C.heartbeat(c.id)
    assert "no output yet" not in C.live_line(C.get(c.id))


def test_human_secs_reads_like_a_clock():
    assert C.human_secs(9) == "9s"
    assert C.human_secs(95) == "1m 35s"
    assert C.human_secs(3700).startswith("1h")
    assert C.human_secs(0) == "0s"


def test_snapshot_reports_stuck_commands_and_the_limits_in_force():
    """The dashboard half: `/api/commands` must be able to answer "is anything
    stuck right now?" without the browser keeping its own clock."""
    c = C.create("quiet", session_id="s6")
    C.start(c.id, 1)
    time.sleep(0.02)
    snap = C.snapshot(session_id="s6")
    assert snap["counts"]["active"] == 1
    assert "stuck" in snap["counts"] and "limits" in snap
    assert set(snap["limits"]) == {"timeout", "idle_timeout", "stuck_after"}


# ── Real commands, CLI runner ─────────────────────────────────────────────────

def test_a_timeout_kills_the_whole_process_tree():
    """⚠️ THE LOAD-BEARING ONE. A timeout must stop the WORK, not just stop us
    watching it. The shell spawns a 60 s sleep; when the ceiling lands, that
    grandchild has to be gone too — otherwise "timed out" means an orphan is still
    burning CPU with nothing tracking it.

    Sabotage check: replace `_kill_active_proc()` on the timeout path with
    `proc.terminate()` and the grandchild assertion below fails.
    """
    from agent2.cli import runtime as rt

    code = (
        "import subprocess, sys, time\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(60)'])\n"
        "print(p.pid, flush=True)\n"
        "time.sleep(60)\n"
    )
    t0 = time.time()
    out, _err, rc, _dur = rt.run_cmd_stream(
        f'python -c "{code}"', session_id="t6-timeout", timeout=2.0,
    )
    elapsed = time.time() - t0

    assert rc == 124, f"expected the timeout convention, got {rc}"
    assert elapsed < 25, f"the timeout did not bound the run ({elapsed:.1f}s)"
    assert "execution timeout" in out
    rows = C.list_all(session_id="t6-timeout")
    assert rows and rows[0].status == C.CommandStatus.TIMEOUT
    assert "execution timeout" in (rows[0].error or "")

    pid = int(next(ln for ln in out.splitlines() if ln.strip().isdigit()).strip())
    assert not _pid_alive(pid), f"the timeout orphaned grandchild pid {pid}"


def test_an_idle_timeout_ends_a_command_that_is_alive_but_silent():
    """The case a total-runtime ceiling cannot catch: the process is healthy, it
    simply stopped saying anything. It prints, then goes quiet forever."""
    from agent2.cli import runtime as rt

    script = ("import time, sys\n"
              "sys.stdout.write('working\\n')\n"
              "sys.stdout.flush()\n"
              "time.sleep(60)\n")
    out, _err, rc, _dur = rt.run_cmd_stream(
        f'python -c "{script}"', session_id="t6-idle", idle_timeout=2.0,
    )
    assert rc == 124
    assert "working" in out              # the real output is not thrown away
    assert "no output for" in out
    rows = C.list_all(session_id="t6-idle")
    assert rows[0].status == C.CommandStatus.TIMEOUT


def test_a_chatty_command_is_never_idle_timed_out():
    """The false-positive direction, which matters more than the true-positive
    one: a command that keeps printing must survive a short idle ceiling."""
    from agent2.cli import runtime as rt

    script = ("import time, sys\n"
              "for i in range(12):\n"
              "    sys.stdout.write(str(i) + '\\n')\n"
              "    sys.stdout.flush()\n"
              "    time.sleep(0.15)\n")
    _out, _err, rc, _dur = rt.run_cmd_stream(
        f'python -c "{script}"', session_id="t6-chatty", idle_timeout=1.0,
    )
    assert rc == 0
    assert C.list_all(session_id="t6-chatty")[0].status == C.CommandStatus.COMPLETED


def test_a_fast_command_is_untouched_by_the_watchdog():
    """Regression guard for the whole feature: the common case must not change."""
    from agent2.cli import runtime as rt

    script = ("import sys\n"
              "sys.stdout.write('hi\\n')\n")
    out, err, rc, dur = rt.run_cmd_stream(
        f'python -c "{script}"',
        session_id="t6-fast", timeout=30, idle_timeout=30,
    )
    _T6_MARK = None  # noqa: F841  (anchor for the block appended below)
    assert (rc, out.strip(), err) == (0, "hi", "")
    assert dur < 20
    assert C.list_all(session_id="t6-fast")[0].status == C.CommandStatus.COMPLETED


# ── The stuck prompt: it must ASK without stopping the drain ──────────────────

def test_the_stuck_prompt_does_not_block_the_drain(quick_stuck):
    """⚠️ THE LOAD-BEARING ONE FOR "do not freeze the agent".

    The command goes quiet long enough to be reported as stuck, then dumps far
    more than a pipe buffer holds and exits. If the prompt blocked — an `input()`
    waiting for [R]/[K]/[W] — the loop would stop emptying the pipes, the child
    would block in `write()` with a full pipe, and this would hang forever. The
    whole burst arriving, a clean exit 0 and the wall-clock bound are the evidence
    that the loop kept draining while the offer was on screen.

    Sabotage check: make `_StuckPrompt.arm` sleep (or call `input()`) and this
    test fails on the elapsed bound — and with a real blocking read, deadlocks.
    """
    from agent2.cli import runtime as rt

    burst = 200_000                          # comfortably past any pipe buffer
    script = ("import time, sys\n"
              "sys.stdout.write('before\\n')\n"
              "sys.stdout.flush()\n"
              "time.sleep(2.0)\n"
              f"sys.stdout.write('x' * {burst} + '\\n')\n"
              "sys.stdout.write('after\\n')\n"
              "sys.stdout.flush()\n")
    t0 = time.time()
    out, _err, rc, _dur = rt.run_cmd_stream(
        f'python -c "{script}"', session_id="t6-nonblock",
    )
    elapsed = time.time() - t0
    assert rc == 0, out
    assert "before" in out and "after" in out, out[:400]
    assert out.count("x") == burst, "the drain lost output while the prompt was up"
    assert elapsed < 15, f"the prompt blocked the drain ({elapsed:.1f}s)"
    assert C.list_all(session_id="t6-nonblock")[0].status == C.CommandStatus.COMPLETED


def test_a_stuck_command_still_ticks_and_is_visible_to_the_other_surface():
    """"Do not freeze the agent" from the observer's side: while one thread is
    parked on a silent command, `/api/commands` must still be able to say what it
    is doing — which means the registry keeps ageing, not that a thread is stuck
    holding the lock."""
    from agent2.cli import runtime as rt

    done = threading.Event()

    def _run():
        try:
            rt.run_cmd_stream(QUIET, session_id="t6-visible", timeout=4.0)
        finally:
            done.set()

    threading.Thread(target=_run, daemon=True).start()
    try:
        snap = _wait_for(
            lambda: (C.snapshot(session_id="t6-visible")["counts"]["active"] == 1
                     and C.snapshot(session_id="t6-visible")),
            timeout=15)
        assert snap, "the running command never became visible"
        row = snap["active"][0]
        assert row["status"] in (C.CommandStatus.RUNNING, C.CommandStatus.STREAMING)
        # The clock is real: it keeps moving with nobody writing to the row.
        first = C.get(row["id"]).elapsed
        time.sleep(0.4)
        assert C.get(row["id"]).elapsed > first
        assert C.live_line(C.get(row["id"]))
    finally:
        assert done.wait(40), "the timed-out command never returned"


def test_pressing_k_kills_the_command_and_returns_control(quick_stuck):
    """[K] — the answer that ends it. rc 130 (the SIGINT convention), status
    KILLED, and the reason says a person did it rather than a ceiling."""
    from agent2.cli import runtime as rt

    presser = _press_when_armed("k")
    out, _err, rc, _dur = rt.run_cmd_stream(QUIET, session_id="t6-kill")
    presser.join(timeout=5)

    assert rc == 130, out
    row = C.list_all(session_id="t6-kill")[0]
    assert row.status == C.CommandStatus.KILLED
    assert "stuck prompt" in (row.error or "")
    assert "command stopped" in out or "killed by user" in out


def test_pressing_r_reruns_the_command_exactly_once_more(quick_stuck, monkeypatch):
    """[R] — capped, and never automatic. Two executions on the record: the
    stopped one and the retry. ⚠️ Rule 21 lives here — Agent2 re-runs a command
    whose effects it cannot see ONLY because a person asked."""
    from agent2.cli import runtime as rt

    monkeypatch.setattr(cfg, "CMD_MAX_RETRIES", 1)      # one re-run, then stop
    presser = _press_when_armed("r", "k")
    _out, _err, rc, _dur = rt.run_cmd_stream(QUIET, session_id="t6-retry")
    presser.join(timeout=5)

    rows = C.list_all(session_id="t6-retry")
    assert len(rows) == 2, [r.status for r in rows]
    assert any("retried by user" in (r.error or "") for r in rows)
    assert {r.status for r in rows} == {C.CommandStatus.CANCELLED,
                                        C.CommandStatus.KILLED}
    assert rc == 130


def test_pressing_w_extends_and_the_command_is_allowed_to_finish(quick_stuck):
    """⚠️ [W] — the answer that must actually mean something.

    The command is quiet past the reporting threshold AND past a real idle
    ceiling: reported stuck at 1s, idle-killed at 2.5s, finishes at 4s. Pressing
    [W] has to move the ceiling, or the answer is overruled a second later by a
    timeout the user was never asked about — exit 0 and COMPLETED is the proof it
    was not.

    Sabotage check: make `defer` a no-op and this comes back rc 124 / TIMEOUT.
    """
    from agent2.cli import runtime as rt

    presser = _press_when_armed("w")
    script = ("import time, sys\n"
              "time.sleep(4.0)\n"
              "sys.stdout.write('finished\\n')\n"
              "sys.stdout.flush()\n")
    out, _err, rc, _dur = rt.run_cmd_stream(
        f'python -c "{script}"', session_id="t6-wait", idle_timeout=2.5,
    )
    presser.join(timeout=5)

    assert rc == 0, out
    assert "finished" in out
    assert C.list_all(session_id="t6-wait")[0].status == C.CommandStatus.COMPLETED


def test_an_unrelated_key_is_passed_back_to_the_normal_listener():
    """The prompt consumes [R]/[K]/[W] and NOTHING else — ESC-to-cancel and the
    mid-turn message queue keep working while the offer is on screen."""
    from agent2.cli import runtime as rt
    from agent2.cli import state as st

    prompt = rt._StuckPrompt()
    prompt._keys = True
    c = C.create("x")
    C.start(c.id, 1)
    prompt.arm(c.id, C.get(c.id))
    try:
        assert st.dispatch_key("q") is False
        assert st.dispatch_key("\x1b") is False
        assert prompt.answer == ""
        assert st.dispatch_key("K") is True      # and it is case-insensitive
        assert prompt.answer == "kill"
    finally:
        prompt.disarm()


def test_the_prompt_hides_the_keys_where_nothing_can_read_them():
    """⚠️ An offer nothing can accept is worse than no offer. With no key reader
    the warning is still printed — the user must be told — but [R]/[K]/[W] are
    not advertised."""
    from agent2.cli import runtime as rt

    prompt = rt._StuckPrompt()
    prompt._keys = False
    c = C.create("x")
    C.start(c.id, 1)
    prompt.arm(c.id, C.get(c.id))
    assert prompt.armed
    from agent2.cli import state as st
    assert st.S.key_handler is None, "a handler was installed with no reader for it"


def test_the_status_line_is_silent_when_stdout_is_not_a_tty():
    """Under pytest stdout is a pipe. A status line there would write `\\r` and
    padding into whatever is capturing the output — including a log file."""
    from agent2.cli import runtime as rt

    line = rt._StatusLine()
    line._tty = False
    line.draw("⏱ running · Elapsed: 3s")
    line.clear()
    assert line._len == 0


# ── The Web surface: pushed, never polled ─────────────────────────────────────

class _Sock:
    """Minimal Socket.IO stand-in — records (event, payload)."""

    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def emit(self, event, payload=None, room=None, **kw):
        self.events.append((event, payload or {}))

    def of(self, name):
        return [p for e, p in self.events if e == name]


def test_the_web_surface_emits_heartbeat_and_then_stuck(quick_stuck):
    """The browser's half of the live state. Both events must carry the rendered
    state line, because the pane must NOT keep its own clock — see
    `terminal.py`'s Task 6 docstring."""
    from agent2 import terminal

    sock = _Sock()
    out, rc = terminal.stream_command(
        'python -c "import time;time.sleep(3)"', "sid-hb", "term-hb", sock,
        session_id="t6-web-hb",
    )
    assert rc == 0, out
    beats = sock.of("command_heartbeat")
    assert beats, "no heartbeat was pushed during a 3s silent command"
    assert "Elapsed:" in beats[0]["state"]
    assert beats[0]["term_id"] == "term-hb" and beats[0]["command_id"]

    stucks = sock.of("command_stuck")
    assert stucks, "a command silent for 3s past a 1s threshold was never reported"
    assert "Elapsed:" in stucks[0]["state"]
    # ⚠️ Reported ONCE, not once per tick: at 5 ticks/s the pane would be
    # unreadable and the warning would look like a fault of its own.
    assert len(stucks) == 1, f"the stuck warning repeated {len(stucks)}×"


def test_the_web_surface_withdraws_the_warning_when_output_resumes(quick_stuck):
    """A command that pauses and then speaks again is not stuck. Leaving the
    badge up would train the user to ignore it."""
    from agent2 import terminal

    script = ("import time, sys\n"
              "time.sleep(2.0)\n"
              "sys.stdout.write('alive\\n')\n"
              "sys.stdout.flush()\n"
              "time.sleep(0.2)\n")
    sock = _Sock()
    out, rc = terminal.stream_command(
        f'python -c "{script}"', "sid-un", "term-un", sock, session_id="t6-web-un",
    )
    assert rc == 0, out
    assert sock.of("command_stuck"), "never reported stuck, so nothing to withdraw"
    assert sock.of("command_unstuck"), "the warning was never withdrawn"


def test_the_web_surface_honours_the_same_ceilings_as_the_cli():
    """One policy, two surfaces: a timeout on the Web half must produce the same
    rc, the same TIMEOUT status and the same reason text."""
    from agent2 import terminal

    sock = _Sock()
    t0 = time.time()
    out, rc = terminal.stream_command(
        QUIET, "sid-to", "term-to", sock, session_id="t6-web-to", timeout=2.0,
    )
    assert rc == 124, out
    assert time.time() - t0 < 25
    assert "execution timeout" in out
    row = C.list_all(session_id="t6-web-to")[0]
    assert row.status == C.CommandStatus.TIMEOUT
    # The pane is told, and the result card carries the same exit code the tool
    # result does — a card saying "exit 0" on a timed-out command is worse than
    # no card.
    assert any("terminating process tree" in p.get("data", "")
               for p in sock.of("terminal_line"))
    assert sock.of("terminal_done")[-1]["returncode"] == 124
    assert sock.of("chat_cmd_result")[-1]["exit_code"] == 124
    assert sock.of("proc_ended"), "the pane was never told the process ended"


def test_a_fast_web_command_emits_no_stuck_warning():
    """The false-positive guard on the Web side."""
    from agent2 import terminal

    sock = _Sock()
    out, rc = terminal.stream_command(
        'python -c "print(1)"', "sid-ok", "term-ok", sock, session_id="t6-web-ok",
    )
    assert rc == 0, out
    assert not sock.of("command_stuck")
    assert C.list_all(session_id="t6-web-ok")[0].status == C.CommandStatus.COMPLETED
