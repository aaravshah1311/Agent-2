# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/cli/runtime.py
─────────────────────
The three things that run *while a turn is in flight*: the interrupt/queue key
listener, the spinner, and the streaming shell runner.

All three are cross-cutting between the input layer and the agent loop, and all
three read cancellation, so they live together and all reach it through
`agent2.cli.state` — never through a flag of their own.

⚠️ EVERY ONE OF THESE MUST DEGRADE, NOT RAISE.
`InputController` no-ops without a TTY; `Spinner` falls back to plain ANSI
without Rich; `run_cmd_stream` returns `(message, "", -1, duration)` rather than
propagating. A CLI that cannot draw a spinner must still answer the question.

⚠️ `run_cmd_stream` REGISTERS ITS CHILD PROCESS.
`state.register_proc` / `clear_proc` are what let ESC kill a running `nmap`
instantly instead of waiting for it to finish. `clear_proc` is identity-checked
inside `state`, so a slow process being cleared after a newer one registered
cannot unregister the live one.

⚠️ AND IT REGISTERS ITS EXECUTION with `core/commands.py` (Task 4).
`state` tracks the process HANDLE, which is what a kill needs; the command
registry tracks the execution's STATE — id, pid, status, and the
`last_output_at` heartbeat — which is what a reader needs to tell a slow command
from a hung one. Two registries because they answer different questions, and
collapsing them would put a lock used at output rate on the cancel path.

⚠️ THE WATCHDOG RIDES THE DRAINER'S TICKS (Task 6). No timer thread.
`procio.drain` already yields `("tick", "")` during silence so the caller can
act; timeout enforcement, the live "Elapsed / Last output" line and the
`⚠ Command appears stuck  [R] Retry [K] Kill [W] Wait` offer all hang off that
one signal. A watchdog thread would need its own handle and its own view of
liveness — two more places for "is it still running?" to disagree — and it would
be judging a loop it cannot see.

⚠️ AND THE OFFER NEVER BLOCKS. `_StuckPrompt` prints and arms a key handler; the
drain loop keeps draining and picks the answer up on a later tick. A blocking
`input()` there would stop emptying the pipes and turn a suspected hang into a
guaranteed one — the exact failure this phase exists to remove.

Layer: env / theme / models / state / render → runtime.
"""

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import ClassVar

from agent2.cli.env import (
    IS_WIN,
    Progress,
    SpinnerColumn,
    TextColumn,
    _con,
    _RICH,
)
from agent2.cli.models import shell_argv
from agent2.cli.render import status_line, tw
from agent2.cli.state import (
    cancelled,
    clear_key_handler as _clear_key_handler,
    clear_proc as _clear_proc,
    dispatch_key as _dispatch_key,
    has_active_proc as _has_active_proc,
    kill_active_proc as _kill_active_proc,
    register_proc as _register_proc,
    set_key_handler as _set_key_handler,
    trigger_cancel as _trigger_cancel,
)
from agent2.cli.theme import B, D, GR, P, R, RD, YW
from agent2 import config as _cfg
from agent2.config import clip_output_line as _clip
from agent2.core import commands as _commands
from agent2.core import permissions as _perms
from agent2.core import procio as _procio


# ── Task 7: cancellation ──────────────────────────────────────────────────────
# The three lines the spec asks for, in the order the work actually happens:
#
#     Stopping current command...
#     Terminating process tree...
#     ✓ Command stopped
#
# ⚠️ THEY ARE PRINTED AROUND THE STEPS, NOT INSTEAD OF THEM. Each line is emitted
# by the side that is about to do (or has just done) the thing it names, so the
# transcript cannot claim a tree was terminated that is still running. That is
# also why the printing lives here and the work lives in `cli/state`: `state` is
# below the renderer in the import order and must stay callable from a signal
# handler, where printing is not safe.
#
# ⚠️ AND THE WORDING IS DECLARED ONCE. Two call sites print this sequence — the
# key/SIGINT path below and `_run_once`'s cancelled branch — and a second copy of
# the strings would drift the first time one of them was reworded.

CANCEL_STOPPING = "Stopping current command..."
CANCEL_KILLING = "Terminating process tree..."
CANCEL_STOPPED = "✓ Command stopped"


def _cancel_say(plain: str) -> None:
    """Print one step of the cancel sequence. Never raises — a cancel that fails
    because the console could not be written to would be a cancel that did not
    happen."""
    try:
        if _RICH:
            style = "bold #3ddc84" if plain is CANCEL_STOPPED else "bold #ffb454"
            _con.print(f"  [{style}]{plain}[/]")
        else:
            col = GR if plain is CANCEL_STOPPED else YW
            print(f"  {col}{plain}{R}")
    except Exception:
        pass


def interrupt(is_ctrl_c: bool = False, *,
              reason: str = "cancelled by user") -> bool:
    """ESC / Ctrl+C. Cancels the CURRENT WORK and keeps the session alive.

    Returns True when there was a running command to stop.

    ⚠️ THE SESSION SURVIVES THIS, and that is the whole task. Nothing here raises
    `KeyboardInterrupt`, calls `sys.exit`, or unwinds the REPL: it settles the
    command state, tears down the process tree, and sets `cancel_event` so the
    turn loop returns to the prompt with the conversation still loaded. The user
    can type the next message immediately. (A *second* Ctrl+C inside
    `state.DOUBLE_TAP_WINDOW` is the deliberate way out — that one sets
    `exit_event`, which `process_turn` honours.)

    ⚠️ AND IT IS IDEMPOTENT. ESC held down, or Ctrl+C delivered twice for one
    press (the SIGINT handler *and* the raw key reader both fire), must not print
    the sequence again: with the process already gone `_has_active_proc()` is
    False, so the three lines are skipped while the flag and the double-tap
    bookkeeping still run.
    """
    running = _has_active_proc()
    if running:
        _cancel_say(CANCEL_STOPPING)
        _cancel_say(CANCEL_KILLING)
    # ⚠️ ONE call, not a record here and a kill there. `trigger_cancel` sets the
    # flag, settles the command state and tears down the tree in the order those
    # three have to happen (see `state.trigger_cancel`); splitting them across
    # this function would put a second, competing copy of that order here.
    _trigger_cancel(is_ctrl_c, reason=reason)
    if running:
        _cancel_say(CANCEL_STOPPED)
    return running


class InputController:
    """Runs while the agent is working. It lets the user:
      • press ESC to interrupt the current turn, AND
      • TYPE A MESSAGE + Enter to QUEUE it — it runs as soon as the
        current turn (and any earlier queued messages) finish.

    Queued messages are collected in a thread-safe list drained by the main
    loop. Fully cross-platform: msvcrt on Windows, select() on POSIX. Degrades
    to a no-op if stdin isn't an interactive TTY.

    ⚠️ THIS IS THE ONLY KEY READER DURING A TURN, and Task 6's stuck prompt
    ([R]etry / [K]ill / [W]ait) therefore goes THROUGH it rather than beside it:
    `state.dispatch_key` gets first refusal on every character. Two readers on the
    same console steal characters from one another, so a second `getwch()` loop
    would have made ESC intermittently dead — the one key that must always work.
    """
    def __init__(self):
        self._stop  = threading.Event()
        self._queue: list[str] = []
        self._qlock = threading.Lock()
        self._t = threading.Thread(target=self._listen, daemon=True)
        try:
            self._tty = sys.stdin.isatty()
        except Exception:
            self._tty = False

    def start(self):
        # On Windows, msvcrt reads the physical console directly, so ESC/typing
        # detection works even when sys.stdin.isatty() is False (common when the
        # CLI is launched by run.py as a child process). On POSIX we need a real
        # TTY for cbreak mode via termios.
        if IS_WIN or self._tty:
            self._t.start()

    def stop(self):
        self._stop.set()

    def drain(self) -> list[str]:
        with self._qlock:
            msgs = self._queue[:]
            self._queue.clear()
        return msgs

    def _enqueue(self, text: str):
        text = (text or "").strip()
        if not text:
            return
        with self._qlock:
            self._queue.append(text)
        try:
            status_line(f"queued — will run after the current turn: {text[:60]}", "info")
        except Exception:
            pass

    def _listen(self):
        try:
            if IS_WIN:
                self._listen_win()
            else:
                self._listen_posix()
        except Exception:
            pass

    def _listen_win(self):
        import msvcrt
        buf: list[str] = []
        while not self._stop.is_set():
            if msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch in ("\x00", "\xe0"):            # arrow/fn key → swallow the code char
                    if msvcrt.kbhit():
                        msvcrt.getwch()
                    continue
                if ch in ("\x1b", "\x03"):            # ESC / Ctrl+C → cancel turn
                    # ⚠️ Task 7: NO `break`. The listener stays up so the user can
                    # keep typing the moment the command dies — "the session
                    # remains alive" has to mean the keyboard does too. Leaving the
                    # loop here cost ESC and message-queueing for the rest of the
                    # turn, and it swallowed the second Ctrl+C of the double tap on
                    # any console that does not also raise SIGINT.
                    interrupt(is_ctrl_c=(ch == "\x03"))
                    buf = []
                    continue
                if _dispatch_key(ch):                 # Task 6: [R]/[K]/[W] prompt
                    continue
                if ch in ("\r", "\n"):                # Enter → queue the line
                    self._enqueue("".join(buf)); buf = []
                elif ch in ("\x08", "\x7f"):          # Backspace
                    if buf: buf.pop()
                elif ch >= " ":
                    buf.append(ch)
            else:
                time.sleep(0.03)

    def _listen_posix(self):
        # Put the TTY in cbreak mode so we get keystrokes immediately (bare ESC
        # included) and can read char-by-char WITHOUT blocking — mirroring the
        # Windows path. termios state is always restored on exit.
        import select
        try:
            import termios
            import tty
        except Exception:
            termios = tty = None

        fd = None
        saved = None
        try:
            fd = sys.stdin.fileno()
            if tty is not None:
                saved = termios.tcgetattr(fd)
                tty.setcbreak(fd)
        except Exception:
            saved = None

        buf: list[str] = []
        try:
            while not self._stop.is_set():
                try:
                    r, _, _ = select.select([sys.stdin], [], [], 0.2)
                except Exception:
                    return
                if not r:
                    continue
                ch = sys.stdin.read(1)
                if ch == "":                          # EOF
                    return
                if ch in ("\x1b", "\x03"):            # ESC / Ctrl+C → cancel turn
                    # ⚠️ Task 7: NO `break` — see `_listen_win`. The listener
                    # outlives the cancel so the keyboard survives it too.
                    interrupt(is_ctrl_c=(ch == "\x03"))
                    buf = []
                    continue
                if _dispatch_key(ch):                 # Task 6: [R]/[K]/[W] prompt
                    continue
                if ch in ("\r", "\n"):                # Enter → queue the line
                    self._enqueue("".join(buf)); buf = []
                elif ch in ("\x08", "\x7f"):          # Backspace / DEL
                    if buf: buf.pop()
                elif ch >= " ":
                    buf.append(ch)
        finally:
            if fd is not None and saved is not None and termios is not None:
                try:
                    termios.tcsetattr(fd, termios.TCSADRAIN, saved)
                except Exception:
                    pass


# ── Spinner ────────────────────────────────────────────────────────────────────
class Spinner:
    """Live progress line. Static text, or a live callable for stage tracking.

    ⚠️ `msg` MAY BE A CALLABLE, and that is the whole point.
    A fixed string is what made every wait read "Thinking…". Passing
    `ux.progress_stages.label` instead lets the line follow the turn — Planning →
    Reading Files → Calling Model → Editing Files — with elapsed time, without
    this class knowing anything about stages.

    ⚠️ THE CALLABLE IS INVOKED PER FRAME (~12×/s) AND MUST DO NO I/O.
    A DB query or a subprocess there would make the spinner the slowest thing in
    the turn. `ProgressStages.label()` is pure string formatting for this reason.
    A raising callable degrades to the last good text rather than killing the
    render thread.
    """

    _frames: ClassVar[list[str]] = ["⠋","⠙","⠹","⠸","⠼","⠴","⠦","⠧","⠇","⠏"]

    def __init__(self, msg="Thinking"):
        self._msg  = msg
        self._last = msg if isinstance(msg, str) else "Working"
        self._stop = threading.Event()
        self._t    = None
        self._prog = None
        self._task = None

    def _text(self) -> str:
        """Current line. Total — a broken provider never breaks the spinner."""
        if callable(self._msg):
            try:
                got = self._msg()
                if got:
                    self._last = str(got)
            except Exception:
                pass
            return self._last
        return self._msg

    def start(self):
        if _RICH:
            self._prog = Progress(SpinnerColumn(), TextColumn("[dim]{task.description}"),
                                  transient=True, console=_con)
            self._prog.start()
            self._task = self._prog.add_task(self._text())
            # Rich runs its own render thread; watch for a cancel so the spinner
            # visibly clears the instant the user interrupts, even while the
            # blocking network call it wraps hasn't returned yet. The same thread
            # refreshes the description, which is how a live label reaches Rich.
            self._t = threading.Thread(target=self._watch_cancel, daemon=True)
            self._t.start()
        else:
            self._t = threading.Thread(target=self._spin, daemon=True)
            self._t.start()

    def stop(self):
        self._stop.set()
        if _RICH and self._prog:
            self._prog.stop()
        if self._t:
            self._t.join(timeout=0.5)
        if not _RICH:
            print(f"\r{' ' * (tw() - 2)}\r", end="", flush=True)

    def _watch_cancel(self):
        while not self._stop.is_set():
            if cancelled() and self._prog:
                try:    self._prog.stop()
                except Exception: pass
                return
            if self._prog is not None and self._task is not None:
                try:
                    self._prog.update(self._task, description=self._text())
                except Exception:
                    pass
            time.sleep(0.12)

    def _spin(self):
        i = 0
        while not self._stop.is_set():
            if cancelled():           # clear promptly on interrupt
                break
            text = self._text()
            line = f"\r  {P.PU}{self._frames[i % len(self._frames)]}{R} {D}{text}…{R}"
            # Pad to the previous width so a shortening label leaves no residue.
            print(line.ljust(len(line) + 12), end="", flush=True)
            i += 1
            time.sleep(0.08)


# ── Run command (streaming) ────────────────────────────────────────────────────

class _StatusLine:
    """ONE transient terminal line, redrawn in place and cleared before anything
    else prints. This is how Task 6's live state reaches the user:

        ⏱ running · Elapsed: 32s · Last output: 2s ago

    ⚠️ Deliberately NOT Rich. Rich's Progress owns the cursor and the spinner
    already holds one, so a second Live would fight it; a bare `\\r` write
    composes with both. With stdout redirected there is no cursor to move, so it
    degrades to printing NOTHING rather than filling a log file with control
    characters and half-overwritten status lines.
    """

    def __init__(self):
        self._len = 0
        try:
            self._tty = sys.stdout.isatty()
        except Exception:
            self._tty = False

    def draw(self, text: str) -> None:
        if not self._tty:
            return
        text = text[:max(20, tw() - 6)]
        pad = " " * max(0, self._len - len(text))
        print(f"\r  {D}{text}{R}{pad}", end="", flush=True)
        self._len = len(text)

    def clear(self) -> None:
        """Erase the line. Called before ANY other print, so output never lands
        halfway through a status line and stays there."""
        if not self._tty or not self._len:
            return
        print("\r" + " " * (self._len + 4) + "\r", end="", flush=True)
        self._len = 0


class _StuckPrompt:
    """The `⚠ Command appears stuck  [R] Retry  [K] Kill  [W] Wait` offer.

    ⚠️ NON-BLOCKING BY CONSTRUCTION. Nothing here waits for a keystroke: `arm()`
    prints the offer and installs a handler, the drain loop keeps draining, and
    the answer is picked up on a later tick. A blocking `input()` would freeze the
    very loop that is keeping the child's pipes empty — it would turn a suspected
    hang into a real one, which is the opposite of this task.

    ⚠️ AND IT DOES NOT OWN A KEY READER. The keys arrive through
    `state.dispatch_key`, i.e. the InputController thread that is already reading
    the console (see its docstring). A second reader would steal ESC.

    ⚠️ NO OPTIONS ARE OFFERED WHERE NO KEY CAN ARRIVE. Without a console reader
    the warning is still printed — the user must know — but the three keys are not
    advertised, because an offer nothing can accept is worse than no offer.
    """

    def __init__(self):
        self.armed = False
        self.answer = ""
        self._id = ""
        try:
            self._keys = IS_WIN or sys.stdin.isatty()
        except Exception:
            self._keys = False

    @property
    def answered(self) -> bool:
        """True only for the two answers that END the attempt. [W]ait resets."""
        return bool(self.answer)

    def arm(self, command_id: str, snap) -> None:
        if self.armed or self.answered:
            return
        self.armed = True
        self._id = command_id
        detail = _commands.live_line(snap) if snap else ""
        head = "⚠ Command appears stuck"
        if _RICH:
            _con.print(f"  [bold #ffb454]{head}[/]  [dim]{detail}[/]")
            if self._keys:
                _con.print("  [dim]\\[R] Retry   \\[K] Kill   \\[W] Wait[/]")
        else:
            print(f"  {YW}{B}{head}{R}  {D}{detail}{R}")
            if self._keys:
                print(f"  {D}[R] Retry   [K] Kill   [W] Wait{R}")
        if self._keys:
            _set_key_handler(self._on_key)

    def disarm(self) -> None:
        if self.armed:
            self.armed = False
            _clear_key_handler()

    def _on_key(self, ch: str) -> bool:
        """Runs on the key-listener thread. Records an intent; acts on nothing."""
        key = (ch or "").strip().lower()
        if key not in ("r", "k", "w"):
            return False
        if key == "w":
            # Keep waiting: the ceilings move with the answer, so a timeout landing
            # a second later cannot overrule the choice the user just made.
            _commands.defer(self._id, _cfg.CMD_WAIT_GRACE)
            self.answer = ""
            self.disarm()
            grace = _commands.human_secs(_cfg.CMD_WAIT_GRACE)
            if _RICH: _con.print(f"  [dim]… waiting another {grace}[/]")
            else:     print(f"  {D}… waiting another {grace}{R}")
            return True
        if key == "r":
            # ⚠️ Rule 21 in one line: the user is told, before anything re-runs,
            # that the killed attempt may already have changed something. Agent2
            # never decides this on its own — a retry is only ever a keypress.
            if _RICH:
                _con.print("  [dim]↻ retry requested — the stopped attempt may "
                           "already have changed something[/]")
            else:
                print(f"  {D}↻ retry requested — the stopped attempt may "
                      f"already have changed something{R}")
        self.answer = "retry" if key == "r" else "kill"
        self.disarm()
        return True


def run_cmd_stream(cmd: str, cwd: str | None = None, *, task_id: str = "",
                   session_id: str = "", timeout: float | None = None,
                   idle_timeout: float | None = None
                   ) -> tuple[str, str, int, float]:
    """Run a shell command with live streaming. Returns (stdout, stderr, rc, duration).

    ⚠️ Task 6: this is the RETRY wrapper. One attempt is `_run_once`, which
    returns `(result, retry_requested)`; a retry only ever happens because the
    user pressed [R] at the stuck prompt, and is capped at `CMD_MAX_RETRIES`.
    Nothing here retries on its own — an automatic re-run of a command whose
    effects we cannot see is precisely what rule 21 forbids, and a shell command
    is the least reversible thing the agent does.

    ⚠️ stdout and stderr are SEPARATED, not merged.
    Item 6 requires both to be reported distinctly in `ux.print_command_result`, and
    merging them into one stream (the old `stderr=subprocess.STDOUT`) makes that
    impossible — you cannot later tell which lines came from which fd.

    ⚠️ DURATION is wall-clock seconds, measured here.
    The timing must happen in this function because the agent loop has no visibility
    into when the process started vs when it returned — a long-running command that
    printed nothing for 30 s would report 0.01 s if timed from outside.

    ⚠️ THE 4-TUPLE RETURN IS UNCHANGED. Three call sites in `agent2cli.py`
    destructure it positionally, so the Task 4 `command_id` is deliberately NOT
    added to it — the execution is looked up in `core/commands.py` instead. The
    new keyword arguments are all optional for the same reason: `/run` and the
    tool loop must keep working untouched.

    ⚠️ Task 15: the capability gate sits OUTSIDE the retry loop. Inside it, a
    refusal would be retried up to `CMD_MAX_RETRIES` times and print the "↻
    retrying" line at a user whose deployment forbids shell execution outright —
    retrying a decision that cannot change. `AGENT2_DENY_CAPS` is process-wide by
    design (see `core/permissions.py`), so this reaches the CLI as well as the web
    half; the default install denies nothing and this costs one set lookup.
    """
    if not _perms.process_allows(_perms.CAP_EXEC):
        _perms.audit_use(_perms.CAP_EXEC, ok=False, what="run_cmd_stream",
                         detail=str(cmd)[:120], surface="cli")
        msg = _perms.refusal(_perms.CAP_EXEC, what="running a shell command")
        if _RICH: _con.print(f"  [red]✗[/] {msg}")
        else:     print(f"  ✗ {msg}")
        return "", msg, 126, 0.0

    attempts = max(0, int(_cfg.CMD_MAX_RETRIES)) + 1
    for attempt in range(attempts):
        result, retry = _run_once(cmd, cwd, task_id=task_id, session_id=session_id,
                                  timeout=timeout, idle_timeout=idle_timeout,
                                  attempt=attempt)
        if not retry or attempt == attempts - 1:
            return result
        if _RICH: _con.print(f"  [dim]↻ retrying (attempt {attempt + 2}/{attempts})[/]")
        else:     print(f"  {D}↻ retrying (attempt {attempt + 2}/{attempts}){R}")
    return result


def _run_once(cmd: str, cwd: str | None = None, *, task_id: str = "",
              session_id: str = "", timeout: float | None = None,
              idle_timeout: float | None = None, attempt: int = 0
              ) -> tuple[tuple[str, str, int, float], bool]:
    """One execution attempt. Returns `(the 4-tuple, retry_requested)`.

    ⚠️ THE WATCHDOG RUNS ON THE DRAIN'S OWN TICKS — no extra thread.
    `procio.drain` already yields `("tick", "")` at least every `poll` seconds
    precisely so the caller can act during silence, so timeout enforcement,
    the heartbeat line and the stuck prompt all hang off it. A watchdog thread
    would need its own handle on the process and its own view of the state, which
    is two more places for "is it still running?" to disagree.
    """
    work_dir = str(Path(cwd).expanduser()) if cwd else str(Path.cwd())
    stdout_lines = []
    stderr_lines = []
    start = time.time()
    status = _StatusLine()
    prompt = _StuckPrompt()

    # Task 6: an unset per-command ceiling falls back to the configured default,
    # which is OFF (0) unless the operator set one — see `config.CMD_TIMEOUT`.
    limit = timeout if timeout is not None else _cfg.CMD_TIMEOUT
    idle_limit = idle_timeout if idle_timeout is not None else _cfg.CMD_IDLE_TIMEOUT

    # Task 4: registered before the spawn, so a bad shell or a missing cwd is a
    # FAILED execution on the record rather than a command with no trace at all.
    cmd_id = _commands.create(
        cmd, task_id=task_id, session_id=session_id, surface="cli",
        timeout=limit, idle_timeout=idle_limit,
    ).id

    if _RICH:
        _con.print(f"  [dim]$ {cmd}[/]")
    else:
        print(f"  {D}$ {cmd}{R}")

    def _say(rich_text: str, plain: str) -> None:
        """Print above the status line without leaving half of it on screen."""
        status.clear()
        if _RICH: _con.print(rich_text)
        else:     print(plain)

    try:
        proc = subprocess.Popen(
            shell_argv(cmd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True, bufsize=1, universal_newlines=True,
            env=os.environ.copy(), cwd=work_dir,
            # Task 5: own session/group, so a cancel can reach the CHILDREN this
            # command spawns (npm, pytest -n, docker) and not just the shell.
            **_procio.GROUP_KWARGS,
        )
        _register_proc(proc)
        _commands.start(cmd_id, proc.pid)

        verdict = ""            # what ended it, if not the process itself
        last_draw = 0.0

        # ⚠️ Task 5: BOTH pipes are drained concurrently by `core/procio.drain`.
        # This used to be two branches — `select` on POSIX, and on Windows a
        # blocking read of stdout to EOF followed by `proc.stderr.read()`. That
        # Windows path deadlocked for real: a child writing more to stderr than
        # the pipe buffer holds blocks in write(), while we block reading stdout
        # it can no longer reach. Reproduced with a 400 KB stderr burst — hung
        # indefinitely, no output, no exit. One drainer, one behaviour, both OSes.
        _procio.close_stdin(proc)
        for stream, line in _procio.drain(proc):
            if cancelled():
                break
            if stream == "tick":
                # Silence — the only moment worth judging. `last_output_at` keeps
                # ageing, and that staleness is the sole evidence of a hang.
                call = _commands.watch(cmd_id, stuck_after=_cfg.CMD_STUCK_AFTER)
                if call in (_commands.W_TIMEOUT, _commands.W_IDLE):
                    verdict = call
                    break
                if call == _commands.W_STUCK and not prompt.answered:
                    # ⚠️ ASK, DO NOT ACT. The agent is NOT frozen here: the prompt
                    # is drawn, the key handler is armed, and this loop keeps
                    # draining. A `pip install` that has been quiet for 30 s is
                    # usually working, so the only safe automatic action is to
                    # tell the user and let them choose.
                    status.clear()
                    prompt.arm(cmd_id, _commands.get(cmd_id))
                if prompt.answer == "kill":
                    verdict = "killed"
                    break
                if prompt.answer == "retry":
                    verdict = "retry"
                    break
                snap = _commands.get(cmd_id)
                now = time.monotonic()
                if (snap and not prompt.armed
                        and now - last_draw >= _cfg.CMD_HEARTBEAT_SEC
                        and snap.elapsed >= _cfg.CMD_HEARTBEAT_SEC):
                    # The live state the spec asks for. Only while the command is
                    # QUIET: a chatty command scrolls its own evidence of life,
                    # and a status line fighting that output helps nobody.
                    status.draw(f"⏱ running · {_commands.live_line(snap)}")
                    last_draw = now
                continue
            status.clear()
            if stream == "stdout":
                stdout_lines.append(line)
                # ⚠️ CAPTURE IN FULL, DRAW CLIPPED. Rich re-wraps each line to the
                # terminal width at a cost that grows faster than the line, so one
                # 8 MB line (a minified bundle, a base64 blob) froze the CLI for
                # ~7 minutes AFTER the process had already exited — indistinguish-
                # able from the hang this phase exists to remove. `stdout_lines`
                # above is appended first and uncut: the model still sees it all.
                stripped = _clip(line).rstrip("\n")
                if _RICH: _con.print(f"  [dim]│[/] {stripped}")
                else:     print(f"  {D}│{R} {stripped}")
            else:
                # stderr is captured but not printed inline — it goes into the
                # result card, not scrollback.
                stderr_lines.append(line)
            # Either stream counts as a heartbeat: a build that logs only to
            # stderr is very much alive, and a detector fed by stdout alone
            # would call it hung.
            _commands.heartbeat(cmd_id)
            # Output means it was never stuck (or is no longer). Disarming here is
            # what stops a command that pauses, is questioned, then resumes from
            # leaving a stale prompt on screen for the rest of its life.
            prompt.disarm()

        prompt.disarm()
        status.clear()

        if cancelled():
            # ⚠️ Task 7: ANNOUNCE ONLY IF NOBODY HAS. A cancel that arrived through
            # `interrupt()` has already printed the three lines and already settled
            # this row, so repeating them here would report a second stop that
            # never happened. The flag can also already be set when this attempt
            # spawns — the next tool call after a cancel — and that case has had no
            # announcement at all, so it gets one.
            snap = _commands.get(cmd_id)
            fresh = not (snap and snap.status == _commands.CommandStatus.CANCELLED)
            if fresh:
                _cancel_say(CANCEL_STOPPING)
            _commands.cancel(cmd_id)
            if fresh:
                _cancel_say(CANCEL_KILLING)
            _kill_active_proc()
            _clear_proc(proc)
            duration = time.time() - start
            if fresh:
                _cancel_say(CANCEL_STOPPED)
            return (("".join(stdout_lines) + "\n[cancelled by user]",
                     "".join(stderr_lines), 130, duration), False)

        if verdict:
            # ⚠️ THE VERDICT IS RECORDED BEFORE THE BLOCKING TREE KILL, for the
            # same reason `terminal.kill_proc` does it in that order: killing the
            # tree closes the pipes, and whichever settle lands first wins. Writing
            # afterwards would leave "exit 1" on the record with nothing saying a
            # timeout or a user's [K] was the cause.
            reason = {
                "timeout": f"execution timeout after {_commands.human_secs(limit)}",
                "idle": f"no output for {_commands.human_secs(idle_limit)}",
                "killed": "killed by user at the stuck prompt",
                "retry": "retried by user at the stuck prompt",
            }[verdict]
            if verdict in ("timeout", "idle"):
                _commands.timed_out(cmd_id, reason=reason)
            elif verdict == "killed":
                _commands.killed(cmd_id, reason=reason)
            else:
                _commands.cancel(cmd_id, reason=reason)
            _say(f"  [bold #ffb454]■ {reason} — terminating process tree[/]",
                 f"  {YW}■ {reason} — terminating process tree{R}")
            _kill_active_proc()
            _clear_proc(proc)
            duration = time.time() - start
            _say("  [bold #ffb454]✓ command stopped[/]", f"  {YW}✓ command stopped{R}")
            note = f"\n[{reason}]"
            rc = 124 if verdict in ("timeout", "idle") else 130
            return (("".join(stdout_lines) + note, "".join(stderr_lines), rc,
                     duration), verdict == "retry")

        proc.wait()
        _clear_proc(proc)
        duration = time.time() - start
        rc  = proc.returncode
        _commands.complete(cmd_id, rc, stdout="".join(stdout_lines),
                           stderr="".join(stderr_lines))
        sym = "✓" if rc == 0 else "✗"
        col_r = GR if rc == 0 else RD

        if _RICH:
            style = "bold #3ddc84" if rc == 0 else "bold #ff5555"
            _con.print(f"  [{style}]{sym} exit {rc}  {duration:.1f}s[/]")
        else:
            print(f"  {col_r}{B}{sym} exit {rc}  {duration:.1f}s{R}")

        return (("".join(stdout_lines), "".join(stderr_lines), rc, duration), False)

    except Exception as ex:
        duration = time.time() - start
        msg = str(ex)
        _commands.fail(cmd_id, msg, exit_code=-1)
        if _RICH: _con.print(f"  [bold #ff5555]✗ {msg}[/]")
        else:     print(f"  {RD}✗ {msg}{R}")
        return ((msg, "", -1, duration), False)
    finally:
        prompt.disarm()
        status.clear()
