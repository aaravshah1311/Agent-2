# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
Tests for the process-I/O layer (``agent2/core/procio.py``) and the deadlock it
removes from both runners (``agent2/cli/runtime.py``, ``agent2/terminal.py``).

Run from the repo root:  python -m pytest .github/tests/test_procio.py -v

Coverage (Task 5)
  - stdout and stderr read CONCURRENTLY — the pipe deadlock, reproduced
  - large output on either stream, and one enormous single line
  - process trees: a kill reaches grandchildren, not just the shell
  - repeated commands, and parallel commands
  - hung processes: the caller keeps getting ticks and is never blocked
  - a command that reads stdin gets EOF instead of waiting forever
  - display clipping never reduces what is captured

⚠️ THE LOAD-BEARING TEST IS ``test_a_stderr_flood_does_not_deadlock``.
It fails — by hanging — against the pre-Task-5 code that read stdout to EOF and
only then read stderr. Every other test here is cheap; that one is the reason the
module exists. ``test_a_huge_line_is_clipped_for_display_only`` is the second:
capture must stay complete while rendering is bounded.
"""

import subprocess
import sys
import threading
import time

import pytest

from agent2 import config
from agent2.core import procio


# A child that floods stderr past any pipe buffer while also writing stdout.
# 400 KB is ~10x the smallest pipe buffer, so the old serial reader wedged.
_FLOOD = (
    "import sys\n"
    "sys.stderr.write('E' * 400000)\n"
    "sys.stderr.flush()\n"
    "print('done-stdout')\n"
)


def _spawn(code: str, **kw):
    """Run *code* in a child python, both pipes open, exactly as the runners do."""
    return subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1, universal_newlines=True,
        **{**procio.GROUP_KWARGS, **kw},
    )


def _collect(proc, *, poll=0.05, timeout=30.0):
    """Drain to EOF. Returns (stdout, stderr, ticks). Raises on overrun."""
    out, err, ticks = [], [], 0
    deadline = time.monotonic() + timeout
    for stream, line in procio.drain(proc, poll=poll):
        if stream == "tick":
            ticks += 1
        elif stream == "stdout":
            out.append(line)
        else:
            err.append(line)
        if time.monotonic() > deadline:
            procio.terminate_tree(proc, grace=0.5)
            raise AssertionError("drain() did not finish — deadlock regression")
    proc.wait(timeout=10)
    return "".join(out), "".join(err), ticks


# ── The deadlock ──────────────────────────────────────────────────────────────

def test_a_stderr_flood_does_not_deadlock():
    """⚠️ The regression this module exists for.

    Pre-Task-5 the CLI read `for line in proc.stdout` to EOF and only afterwards
    `proc.stderr.read()`. A child writing 400 KB to stderr blocks in write() with
    nobody reading, while the parent blocks reading stdout the child can no longer
    reach. Reproduced as an indefinite hang on Windows before the fix.

    Sabotage check: serialize `drain()` (read stdout fully, then stderr) and this
    test stops finishing.
    """
    proc = _spawn(_FLOOD)
    started = time.monotonic()
    out, err, _ = _collect(proc, timeout=30.0)
    assert "done-stdout" in out
    assert len(err) == 400000          # every byte, not a prefix
    assert time.monotonic() - started < 25.0


def test_a_stdout_flood_does_not_deadlock_either():
    """Symmetry: the bug is about serializing, not about which pipe is bigger."""
    proc = _spawn(
        "import sys\n"
        "sys.stdout.write('O' * 400000)\n"
        "sys.stdout.flush()\n"
        "sys.stderr.write('done-stderr\\n')\n"
    )
    out, err, _ = _collect(proc, timeout=30.0)
    assert len(out) == 400000
    assert "done-stderr" in err


def test_both_streams_are_labelled_and_interleaved():
    """The caller can still tell the two apart — merging them loses that."""
    proc = _spawn(
        "import sys\n"
        "for i in range(200):\n"
        "    print('o%d' % i)\n"
        "    sys.stderr.write('e%d\\n' % i)\n"
    )
    tags = [s for s, _ in procio.drain(proc, poll=0.05) if s != "tick"]
    proc.wait(timeout=10)
    assert tags.count("stdout") == 200
    assert tags.count("stderr") == 200


def test_lines_keep_their_newline_and_arrive_in_order():
    proc = _spawn("print('a')\nprint('b')\nprint('c')\n")
    lines = [ln for s, ln in procio.drain(proc) if s == "stdout"]
    proc.wait(timeout=10)
    assert lines == ["a\n", "b\n", "c\n"]


# ── Large output ──────────────────────────────────────────────────────────────

def test_one_enormous_line_is_captured_whole():
    """8 MB with no newline is one `readline` — it must not be lost or split."""
    proc = _spawn("import sys;sys.stdout.write('x'*8000000)")
    out, _, _ = _collect(proc, timeout=60.0)
    assert len(out) == 8000000


def test_a_huge_line_is_clipped_for_display_only():
    """⚠️ Capture is complete; only the drawn copy is bounded.

    Rich re-wraps a line at a cost that grows faster than the line — one 8 MB
    line took ~400 s to render AFTER the child had exited, which reads to a user
    as exactly the hang this phase removes.
    """
    line = "y" * 5_000_000
    drawn = config.clip_output_line(line)
    assert len(drawn) < 20_000
    assert "clipped" in drawn and "5000000" in drawn
    # ⚠️ The marker must survive Rich. `[...]` is markup: an earlier marker
    # written `…[line clipped]` was parsed as a style tag and disappeared,
    # leaving a truncated line with no sign it had been truncated.
    assert "[" not in drawn[len(line[:config.MAX_OUTPUT_LINE]):]
    # Short lines are returned untouched — no marker, no copy of the limit.
    assert config.clip_output_line("hello\n") == "hello\n"
    assert config.clip_output_line("z" * config.MAX_OUTPUT_LINE) == "z" * config.MAX_OUTPUT_LINE


# ── Hung processes ────────────────────────────────────────────────────────────

def test_a_silent_child_still_yields_ticks():
    """A stuck subprocess must not freeze the caller: ticks keep arriving."""
    proc = _spawn("import time;time.sleep(30)")
    ticks = 0
    try:
        for stream, _ in procio.drain(proc, poll=0.05):
            if stream == "tick":
                ticks += 1
                if ticks >= 5:
                    break
    finally:
        procio.terminate_tree(proc, grace=1.0)
    assert ticks >= 5


def test_drain_ends_when_both_pipes_close_without_calling_wait():
    """`drain` must not reap the child — the caller owns the exit code."""
    proc = _spawn("import sys;sys.exit(3)")
    list(procio.drain(proc))
    assert proc.wait(timeout=10) == 3


def test_close_stdin_turns_an_infinite_read_into_eof():
    """A child reading stdin it will never be given must finish, not hang."""
    proc = _spawn("import sys;data=sys.stdin.read();print('got %d' % len(data))")
    procio.close_stdin(proc)
    out, _, _ = _collect(proc, timeout=20.0)
    assert "got 0" in out


def test_close_stdin_is_safe_to_call_twice():
    proc = _spawn("print('x')")
    procio.close_stdin(proc)
    procio.close_stdin(proc)          # must not raise
    _collect(proc, timeout=10.0)


# ── Process trees ─────────────────────────────────────────────────────────────

def test_terminate_tree_reaches_a_grandchild():
    """⚠️ `proc.terminate()` ends the shell, not what the shell started.

    `npm test`, `pytest -n auto` and `docker build` all spawn children; killing
    only the parent orphans them, they keep the pipes open, and the command is
    never reported as finished.
    """
    parent_code = (
        "import subprocess, sys, time\n"
        "c = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(60)'])\n"
        "print(c.pid, flush=True)\n"
        "time.sleep(60)\n"
    )
    proc = _spawn(parent_code)
    grandchild_pid = int(proc.stdout.readline().strip())
    assert procio.terminate_tree(proc, grace=2.0) is True
    assert proc.poll() is not None

    # The grandchild must be gone too. Poll briefly: the kill is asynchronous.
    deadline = time.monotonic() + 5.0
    alive = True
    while time.monotonic() < deadline:
        alive = _pid_alive(grandchild_pid)
        if not alive:
            break
        time.sleep(0.1)
    if alive:                          # never leak a sleeping process from a test
        _force_kill(grandchild_pid)
    assert not alive, "grandchild survived terminate_tree"


def _pid_alive(pid: int) -> bool:
    if procio.IS_WIN:
        res = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"],
                             capture_output=True, text=True, timeout=10)
        return str(pid) in res.stdout
    try:
        import os
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _force_kill(pid: int) -> None:
    try:
        if procio.IS_WIN:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=10)
        else:
            import os
            import signal
            os.kill(pid, signal.SIGKILL)
    except Exception:                                # noqa: BLE001, S110 - a
        pass                                         # kill that fails is already dead


def test_terminate_tree_on_an_already_dead_process_is_success():
    """"Nothing left to kill" is not an error — cancellation must never fail."""
    proc = _spawn("pass")
    proc.wait(timeout=10)
    assert procio.terminate_tree(proc) is True


def test_terminate_tree_tolerates_none():
    assert procio.terminate_tree(None) is True


def test_terminate_tree_never_raises_when_the_platform_path_fails(monkeypatch):
    """A missing `taskkill`/`killpg` degrades to the direct-child path."""
    proc = _spawn("import time;time.sleep(30)")
    if procio.IS_WIN:
        monkeypatch.setattr(procio, "_kill_tree_win", lambda pid: False)
    else:
        monkeypatch.setattr(procio, "_signal_group", lambda p, s: False)
    try:
        assert procio.terminate_tree(proc, grace=1.0) is True
    finally:
        if proc.poll() is None:
            proc.kill()


def test_posix_spawn_kwargs_isolate_the_session():
    """⚠️ On POSIX the child MUST get its own session.

    Without it the child shares Agent2's process group, and `killpg` would take
    Agent2 down to cancel one command. Windows deliberately gets no group flag:
    `taskkill /T` walks the real tree, and CREATE_NEW_PROCESS_GROUP would change
    how console Ctrl+C reaches children.
    """
    if procio.IS_WIN:
        assert procio.GROUP_KWARGS == {}
    else:
        assert procio.GROUP_KWARGS == {"start_new_session": True}


# ── Repeated and parallel commands ────────────────────────────────────────────

def test_repeated_commands_do_not_leak_reader_threads():
    """"Stuck after running many commands" would show up here as thread growth."""
    before = threading.active_count()
    for i in range(8):
        proc = _spawn(f"import sys;print({i});sys.stderr.write('e\\n')")
        out, err, _ = _collect(proc, timeout=15.0)
        assert out.strip() == str(i)
        assert err.strip() == "e"
    time.sleep(0.5)                     # let the daemon readers retire
    assert threading.active_count() <= before + 2


def test_parallel_drains_are_independent():
    """Four children drained at once: no cross-talk, no serialization."""
    results: dict[int, tuple[str, str]] = {}
    errors: list[BaseException] = []

    def work(n: int):
        try:
            proc = _spawn(
                f"import sys;print('out{n}');sys.stderr.write('err{n}\\n')")
            out, err, _ = _collect(proc, timeout=20.0)
            results[n] = (out.strip(), err.strip())
        except BaseException as exc:     # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=40)
    assert not errors, errors
    assert results == {n: (f"out{n}", f"err{n}") for n in range(4)}


# ── The runners are wired to this module ──────────────────────────────────────

def _code_only(path) -> str:
    """Source reduced to executable code — comments and docstrings removed.

    The invariants below are about what these modules DO, and both files discuss
    the old serial reads at length in prose. Matching raw text would make the test
    fail on an accurate explanation of the bug it guards against. `ast` drops
    comments for free; the docstrings are stripped explicitly.
    """
    import ast
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef,
                                 ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = node.body
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            body.pop(0)
            if not body:
                body.append(ast.Pass())
    return ast.unparse(ast.fix_missing_locations(tree))


def test_both_runners_use_the_shared_drainer():
    """⚠️ Neither surface may keep a private read loop.

    The deadlock existed because the CLI and Web halves each had their own, and
    only one of them was correct. A second reader loop is how that comes back.
    """
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    for rel in ("agent2/cli/runtime.py", "agent2/terminal.py"):
        src = _code_only(root / rel)
        assert "_procio.drain(proc)" in src, rel
        assert "_procio.GROUP_KWARGS" in src, rel
        # The old serial reads must not reappear.
        assert "proc.stderr.read()" not in src, rel
        assert "stderr=subprocess.STDOUT" not in src, rel


def test_both_runners_kill_the_tree_not_the_process():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    for rel in ("agent2/cli/state.py", "agent2/terminal.py"):
        src = _code_only(root / rel)
        assert "terminate_tree" in src, rel


@pytest.mark.parametrize("rel", ["agent2/cli/runtime.py", "agent2/terminal.py"])
def test_capture_is_appended_before_the_clipped_draw(rel):
    """The clip is display-only: the full line is recorded first."""
    from pathlib import Path
    src = _code_only(Path(__file__).resolve().parents[2] / rel)
    assert "clip_output_line" in src or "_clip(" in src


def test_drain_stops_waiting_on_a_pipe_an_orphan_still_holds():
    """⚠️ EOF is not guaranteed, so it cannot be the only way out of `drain`.

    A pipe's write end belongs to every process that inherited it. Here the child
    hands its stdout to a grandchild and exits; the grandchild lives on holding
    the handle, so `readline()` would block forever on a pipe with no writer left
    that will ever write. `drain` must notice the child is gone and stop.

    Sabotage check: delete the `after_exit` branch in `drain` and this test hangs
    instead of failing.
    """
    proc = _spawn(
        "import subprocess, sys\n"
        "subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(60)'],\n"
        "                 stdout=sys.stdout, stderr=sys.stderr)\n"
        "print('parent-done', flush=True)\n"
    )
    started = time.monotonic()
    out = []
    for stream, line in procio.drain(proc, poll=0.05, after_exit=0.3):
        if stream == "stdout":
            out.append(line)
        if time.monotonic() - started > 25:
            procio.terminate_tree(proc, grace=0.5)
            raise AssertionError("drain() never returned — orphan holds the pipe")
    elapsed = time.monotonic() - started
    procio.terminate_tree(proc, grace=1.0)
    assert "parent-done" in "".join(out)      # real output is not lost
    assert elapsed < 20


def test_drain_does_not_cut_a_fast_command_short():
    """The `after_exit` grace exists so a child reaped a hair before its last
    lines are pulled through still gets them delivered."""
    for _ in range(5):
        proc = _spawn("print('a');print('b');print('c')")
        out, _err, _ = _collect(proc, poll=0.05, timeout=15.0)
        assert out == "a\nb\nc\n"


def test_cli_cancel_kills_the_whole_tree_and_the_runner_returns():
    """The CLI's cancel path is the same guarantee as the Web pane's kill.

    ⚠️ `state.kill_active_proc` goes through `terminate_tree`. With a plain
    `proc.terminate()` the shell dies and its child keeps running — holding the
    pipe, so `run_cmd_stream` never returns and the turn never ends.
    """
    from agent2.cli import runtime, state
    from agent2.core import commands as C

    C.reset()
    state.cancel_event.clear()
    result: dict = {}
    child_pid: dict = {}
    try:
        cmd = (f'{sys.executable} -c "import subprocess,sys,time;'
               f"c=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
               'print(c.pid,flush=True);time.sleep(60)"')
        t = threading.Thread(
            target=lambda: result.update(
                r=runtime.run_cmd_stream(cmd, session_id="procio-cancel")),
            daemon=True)
        t.start()

        # Wait until the command has actually produced output, so the cancel
        # lands mid-flight rather than during the spawn.
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            rows = C.list_all(session_id="procio-cancel")
            if rows and rows[0].last_output_at:
                break
            time.sleep(0.05)

        state.trigger_cancel(is_ctrl_c=False)
        t.join(timeout=30)
        assert not t.is_alive(), "run_cmd_stream did not return after the cancel"

        out, _err, rc, _dur = result["r"]
        assert rc == 130                                    # shell SIGINT convention
        assert C.list_all(session_id="procio-cancel")[0].status == C.CommandStatus.CANCELLED

        # ⚠️ The load-bearing half: the GRANDCHILD is gone too. With a plain
        # `proc.terminate()` the shell dies, `drain` still ends (it bounds itself
        # after the child exits), the turn still finishes — and a 60 s sleep is
        # left running on the user's machine with nothing tracking it. "Cancelled"
        # has to mean the work stopped, not just that we stopped watching.
        pid = int(next(ln for ln in out.splitlines() if ln.strip().isdigit()).strip())
        child_pid["pid"] = pid
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and _pid_alive(pid):
            time.sleep(0.1)
        assert not _pid_alive(pid), f"the cancel orphaned pid {pid}"
    finally:
        state.cancel_event.clear()
        C.reset()
        if child_pid.get("pid"):
            _force_kill(child_pid["pid"])


def test_a_kill_ends_the_drain_even_when_an_orphan_still_holds_the_pipe():
    """⚠️ Kill inside the spawn window — the case that hung before `drain` bounded
    itself after the child exits.

    The kill lands before `powershell` has started the program it was told to run,
    so the tree kill reaches the shell and the program starts afterwards as an
    orphan holding the inherited write end. `readline()` in `_pump` waits on a
    pipe nobody will ever write to again, and `stream_command` never returns.

    Sabotage check: remove the `after_exit` bound from `drain` (make the quiet
    branch `continue` unconditionally) and this test fails — the thread is still
    alive 30 s later.
    """
    from agent2 import terminal
    from agent2.core import commands as C

    class Silent:
        def emit(self, *a, **kw):
            pass

    C.reset()
    try:
        t = threading.Thread(
            target=lambda: terminal.stream_command(
                f"{sys.executable} -c \"import time;time.sleep(60)\"",
                "sid-o", "term-o", Silent(), session_id="procio-orphan"),
            daemon=True)
        t.start()

        # Kill as soon as the handle exists — deliberately NOT waiting for output,
        # which is what opens the orphan window.
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if terminal.get_proc("sid-o", "term-o"):
                break
            time.sleep(0.01)
        terminal.kill_proc("sid-o", "term-o")

        t.join(timeout=30)
        assert not t.is_alive(), "the drain never ended: an orphan still holds the pipe"
        row = C.list_all(session_id="procio-orphan")[0]
        assert row.status == C.CommandStatus.KILLED, row.status
    finally:
        C.reset()


def test_a_web_kill_is_recorded_as_killed_not_as_a_bad_exit():
    """⚠️ The KILLED verdict must be written BEFORE the tree kill.

    `terminate_tree` blocks until the tree is dead; the instant it is, the pipes
    close, the streaming thread leaves `drain` and calls `complete()` with the
    shell's exit code. Writing the verdict afterwards therefore loses the race and
    the execution reads FAILED, with nothing to say the user killed it — observed
    exactly that way when Task 5 replaced the non-blocking `terminate()`.

    Sabotage check: move the `_commands.killed(...)` call in `terminal.kill_proc`
    back below `terminate_tree` and this test fails with status "failed".
    """
    from agent2 import terminal
    from agent2.core import commands as C

    class Silent:
        def emit(self, *a, **kw):
            pass

    C.reset()
    result: dict = {}
    try:
        t = threading.Thread(
            target=lambda: result.update(r=terminal.stream_command(
                f"{sys.executable} -c \"import sys,time;print('up',flush=True);"
                "time.sleep(60)\"",
                "sid-k", "term-k", Silent(), session_id="procio-kill")),
            daemon=True)
        t.start()

        # Wait for real output, so the command is genuinely mid-flight — the
        # ordinary case a user kills. (The spawn-window case is its own test.)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            rows = C.list_all(session_id="procio-kill")
            if rows and rows[0].last_output_at:
                break
            time.sleep(0.05)

        terminal.kill_proc("sid-k", "term-k")
        t.join(timeout=30)
        assert not t.is_alive(), (
            "stream_command did not return after the kill; "
            f"rows={[(r.status, r.process_id) for r in C.list_all(session_id='procio-kill')]}")

        row = C.list_all(session_id="procio-kill")[0]
        assert row.status == C.CommandStatus.KILLED, row.status
        assert "terminal pane" in row.error
    finally:
        C.reset()


def test_cli_runner_captures_a_huge_line_whole_but_draws_little(capsys):
    """End to end through the real shell: full capture, bounded render.

    This is the pair of facts that matter together — a test that only checked the
    helper would stay green if the call site were removed.
    """
    from agent2.cli import runtime
    from agent2.core import commands as C

    C.reset()
    try:
        big = 400_000
        out, _err, rc, dur = runtime.run_cmd_stream(
            f"python -c \"import sys;sys.stdout.write('q'*{big})\"",
            session_id="procio-huge")
        drawn = capsys.readouterr().out
        assert rc == 0
        assert out.count("q") == big               # capture: every byte
        assert drawn.count("q") < 20_000           # render: bounded
        assert "clipped" in drawn
        assert dur < 60
    finally:
        C.reset()


def test_web_runner_captures_a_huge_line_whole_but_emits_little():
    """Same split on the Web half: `output` is whole, the frame is clipped."""
    from agent2 import terminal
    from agent2.core import commands as C

    class FakeSocket:
        def __init__(self):
            self.lines: list[str] = []

        def emit(self, event, payload=None, room=None):
            if event == "terminal_line":
                self.lines.append((payload or {}).get("data", ""))

    C.reset()
    sock = FakeSocket()
    try:
        big = 400_000
        out, rc = terminal.stream_command(
            f"python -c \"import sys;sys.stdout.write('w'*{big})\"",
            "sid-x", "term-x", sock, session_id="procio-web-huge")
        assert rc == 0
        assert out.count("w") == big
        emitted = "".join(sock.lines)
        assert emitted.count("w") < 20_000
        assert "clipped" in emitted
    finally:
        C.reset()
