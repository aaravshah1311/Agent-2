# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/core/commands.py
───────────────────────
THE command execution manager (Task 4). One lifecycle, one registry, shared by
the Web runner (`agent2/terminal.py`) and the CLI runner
(`agent2/cli/runtime.py`).

⚠️ THE BUG THIS ADDRESSES
──────────────────────────
"When Agent2 runs commands many times it sometimes becomes stuck and never
responds." Before this module there was no execution state at all: two runners,
each with a bare `Popen` and a blocking read loop, and nothing anywhere that
could answer "what is running, since when, and when did it last say anything?"
A hang was therefore not merely unhandled — it was *unobservable*, and an
arbitrary timeout bolted onto an unobservable process is a guess, not a fix.

So this module deliberately does ONE thing: it makes an execution describable.

  CREATED → STARTING → RUNNING → STREAMING → COMPLETED
                                ↘ FAILED · TIMEOUT · CANCELLED · KILLED

Every execution tracks exactly what the spec names: `command_id`, `process_id`,
`task_id`, `started_at`, `last_output_at`, `timeout`, `status`, `exit_code`,
`stdout`, `stderr`.

⚠️ `last_output_at` IS THE LOAD-BEARING FIELD.
Elapsed time cannot distinguish a healthy `docker build` from a deadlocked
`pytest`; both are "still going after 4 minutes". Time since the last byte can.
That is why `heartbeat()` is called by the runner on every output line and is
NOT derived here — a subprocess does not phone home, so the *absence* of a
heartbeat is itself the signal, and it only exists if the reader records it.

⚠️ STATE IS RECORDED WHEN IT HAPPENS, never on the way out.
Same reason as `core/tasks.py` checkpoints: a `SIGKILL` runs no handler.

⚠️ THIS MODULE NEVER TOUCHES A PROCESS.
No `Popen`, no `terminate()`, no `kill()`. Killing belongs to the runners, which
own the handles (`terminal.kill_proc`, `cli/state.kill_active_proc`). Keeping the
bookkeeping side-effect-free is what makes it safe to call from a streaming loop
and from a socket handler at the same time.

⚠️ AND THE WATCHDOG (Task 6) KEEPS THAT PROPERTY.
`watch()` returns one of four WORDS — `W_OK` / `W_STUCK` / `W_TIMEOUT` / `W_IDLE`
— and does nothing else. Deciding and acting are separated on purpose: the
decision needs the state (which lives here), the action needs the handle (which
lives in the runner), and a `watch()` that could kill would put process teardown
behind the same lock `heartbeat()` takes once per output line.

The ceilings themselves live in `config` (`CMD_TIMEOUT`, `CMD_IDLE_TIMEOUT`,
`CMD_STUCK_AFTER`) and BOTH DEFAULT TO OFF. "Do not solve this merely by adding
an arbitrary timeout": a `docker build` or a `pip install torch` is legitimately
silent for many minutes, so what is on by default is the REPORT — after
`CMD_STUCK_AFTER` seconds of silence the user is told and offered
[R]etry / [K]ill / [W]ait — not a kill. See `config.CMD_TIMEOUT`.

⚠️ EVERY PUBLIC FUNCTION IS BEST-EFFORT AND TOTAL.
An unknown `command_id` returns None rather than raising. Bookkeeping may never
be the reason a command fails to run, so callers are written as fire-and-forget.

Scope note: the registry is in-memory. Durable execution state across a restart
is Phase 8 (Task 24), and building it here would be implementing a later task
early. What is in memory today is already enough for the reliability work in
Tasks 5–7, which is all within one process lifetime.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field

# ⚠️ THE MODULE, NOT THE VALUES. `from agent2.config import CMD_STUCK_AFTER`
# would bind a snapshot at import and freeze it — the same failure mode as the
# CLI palette (`from .theme import PU`). Reading `_config.CMD_STUCK_AFTER` at call
# time is what lets a test or a caller change the threshold and be believed.
from agent2 import config as _config

# ── States ────────────────────────────────────────────────────────────────────


class CommandStatus:
    """The nine states. Strings, not an Enum — they round-trip through JSON,
    Socket.IO payloads and (later) SQLite text columns with no conversion, the
    same reasoning as `core/tasks.TaskStatus`."""

    CREATED = "created"
    STARTING = "starting"
    RUNNING = "running"
    STREAMING = "streaming"
    COMPLETED = "completed"
    # Failure states.
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    KILLED = "killed"


ALL_STATUSES = (
    CommandStatus.CREATED, CommandStatus.STARTING, CommandStatus.RUNNING,
    CommandStatus.STREAMING, CommandStatus.COMPLETED, CommandStatus.FAILED,
    CommandStatus.TIMEOUT, CommandStatus.CANCELLED, CommandStatus.KILLED,
)

# Reached the end of its life: no further transition may move it.
TERMINAL = frozenset((
    CommandStatus.COMPLETED, CommandStatus.FAILED, CommandStatus.TIMEOUT,
    CommandStatus.CANCELLED, CommandStatus.KILLED,
))

# Ended in a way the user did not ask for. Kept separate from TERMINAL because
# "finished" and "finished badly" are different questions.
FAILURE = frozenset((
    CommandStatus.FAILED, CommandStatus.TIMEOUT, CommandStatus.CANCELLED,
    CommandStatus.KILLED,
))

# A live process exists (or is about to).
ACTIVE = frozenset((
    CommandStatus.STARTING, CommandStatus.RUNNING, CommandStatus.STREAMING,
))

MAX_SNAPSHOT = 4000      # stdout/stderr kept per execution, matching MAX_TOOL_OUTPUT's spirit
MAX_COMMAND = 2000
MAX_KEPT = 300           # registry ceiling; oldest settled entries are dropped first


def _iso(when: float | None = None) -> str:
    """UTC display timestamp. Human-readable only — never used for arithmetic."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(when))


def _uid() -> str:
    return uuid.uuid4().hex[:12]


def _text(value, limit: int) -> str:
    return str(value or "")[:limit]


# ── The record ────────────────────────────────────────────────────────────────

@dataclass
class CommandExecution:
    """One command's lifecycle.

    ⚠️ Two clocks, on purpose. The `*_at` strings are for humans and for the
    wire; `*_mono` are `time.monotonic()` and are the ONLY thing `elapsed`/`idle`
    are computed from. A wall-clock subtraction would go negative across an NTP
    correction or a DST change, and "idle for -3600s" reads as healthy.
    """

    id: str
    command: str
    task_id: str = ""
    session_id: str = ""
    surface: str = ""            # "web" | "cli" — which runner owns the handle
    term_id: str = ""            # Web only: which terminal pane it streams to
    process_id: int | None = None
    status: str = CommandStatus.CREATED
    created_at: str = ""
    started_at: str = ""
    last_output_at: str = ""
    completed_at: str = ""
    created_mono: float = 0.0
    started_mono: float = 0.0
    last_output_mono: float = 0.0
    completed_mono: float = 0.0
    timeout: float | None = None        # hard ceiling, seconds
    idle_timeout: float | None = None   # ceiling on silence, seconds
    # Task 6: monotonic instant until which "appears stuck" is suppressed. Set by
    # `defer()` when the user answers [W]ait, so a command they have consciously
    # decided to keep waiting for does not re-ask every few seconds.
    waived_mono: float = 0.0
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    error: str = ""
    output_lines: int = 0
    meta: dict = field(default_factory=dict)

    # ── Derived ──────────────────────────────────────────────────────────────

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE

    @property
    def failed(self) -> bool:
        return self.status in FAILURE

    @property
    def elapsed(self) -> float:
        """Seconds the process has been (or was) alive."""
        if not self.started_mono:
            return 0.0
        end = self.completed_mono or time.monotonic()
        return max(0.0, end - self.started_mono)

    @property
    def idle(self) -> float:
        """Seconds since the last output line.

        Falls back to `elapsed` before the first line: a command that has
        printed nothing at all has been silent for its whole life, and reporting
        0.0 there would make the stuck-detector blind to exactly the worst case.
        """
        if not self.started_mono:
            return 0.0
        base = self.last_output_mono or self.started_mono
        end = self.completed_mono or time.monotonic()
        return max(0.0, end - base)

    def to_dict(self) -> dict:
        """The wire shape. One declaration, shared by every reader."""
        return {
            "id": self.id,
            "command": self.command,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "surface": self.surface,
            "term_id": self.term_id,
            "process_id": self.process_id,
            "status": self.status,
            "active": self.is_active,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "last_output_at": self.last_output_at,
            "completed_at": self.completed_at,
            "timeout": self.timeout,
            "idle_timeout": self.idle_timeout,
            "exit_code": self.exit_code,
            "elapsed": round(self.elapsed, 2),
            "idle": round(self.idle, 2),
            "waived": self.waived_mono > time.monotonic(),
            "output_lines": self.output_lines,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "error": self.error,
        }

    def copy(self) -> CommandExecution:
        """A detached snapshot. Readers get one of these, never the live row —
        a caller holding the real object could mutate status behind the lock."""
        clone = CommandExecution(**{k: v for k, v in self.__dict__.items()
                                    if k != "meta"})
        clone.meta = dict(self.meta)
        return clone


# ── The registry ──────────────────────────────────────────────────────────────
# One dict, one lock. Both surfaces write here: the Web runner from a per-command
# daemon thread, the CLI runner from the foreground thread, and `terminal_kill`
# from a Socket.IO handler — three writers for the same row, so every mutation
# takes the lock.
#
# ⚠️ NO `sync.notify()` HERE. `heartbeat()` fires once per output LINE; a
# `make -j8` would publish thousands of events and rebuild every subscriber's
# cache for each one. `core/tasks.py` notifies because a task changes a handful
# of times per turn — a command changes thousands. Readers poll instead.

_registry: dict[str, CommandExecution] = {}
_lock = threading.RLock()


def _prune_locked() -> None:
    """Drop the oldest SETTLED executions once the registry exceeds MAX_KEPT.

    ⚠️ Active executions are never dropped, whatever the count. A stuck process
    is the single most important row in the table, and evicting it to make space
    would delete the evidence the manager exists to preserve.
    """
    if len(_registry) <= MAX_KEPT:
        return
    settled = sorted(
        (c for c in _registry.values() if c.is_terminal),
        key=lambda c: c.completed_mono or c.created_mono,
    )
    for cmd in settled[:len(_registry) - MAX_KEPT]:
        _registry.pop(cmd.id, None)


def create(command: str, *, task_id: str = "", session_id: str = "",
           surface: str = "", term_id: str = "", timeout: float | None = None,
           idle_timeout: float | None = None,
           meta: dict | None = None) -> CommandExecution:
    """Register a command in CREATED and return its snapshot.

    Called BEFORE the `Popen`, so a spawn that raises is still a visible
    execution (it becomes FAILED) rather than a command that never existed.
    """
    now = time.monotonic()
    cmd = CommandExecution(
        id=_uid(),
        command=_text(command, MAX_COMMAND),
        task_id=_text(task_id, 64),
        session_id=_text(session_id, 64),
        surface=_text(surface, 16),
        term_id=_text(term_id, 64),
        status=CommandStatus.CREATED,
        created_at=_iso(),
        created_mono=now,
        timeout=float(timeout) if timeout and timeout > 0 else None,
        idle_timeout=float(idle_timeout) if idle_timeout and idle_timeout > 0 else None,
        meta=dict(meta or {}),
    )
    with _lock:
        _registry[cmd.id] = cmd
        _prune_locked()
        return cmd.copy()


def get(command_id: str) -> CommandExecution | None:
    with _lock:
        cmd = _registry.get(str(command_id or ""))
        return cmd.copy() if cmd else None


def _mutate(command_id: str, fn) -> CommandExecution | None:
    """Apply *fn* to the live row under the lock and return a snapshot.

    ⚠️ THE ONE WRITER. Every transition goes through here so the terminal-state
    guard cannot be forgotten by one helper and honoured by the others — the same
    single-writer discipline `core/tasks._apply` uses, for the same reason.
    """
    with _lock:
        cmd = _registry.get(str(command_id or ""))
        if cmd is None:
            return None
        if cmd.is_terminal:
            # ⚠️ A settled execution is never moved again. A cancel racing a
            # normal exit must not rewrite "exit 0" into "cancelled", or the
            # transcript would claim work was abandoned that in fact finished.
            return cmd.copy()
        fn(cmd)
        return cmd.copy()


def starting(command_id: str) -> CommandExecution | None:
    """CREATED → STARTING: the spawn is about to be attempted."""
    def _fn(cmd: CommandExecution) -> None:
        cmd.status = CommandStatus.STARTING
    return _mutate(command_id, _fn)


def start(command_id: str, process_id: int | None = None) -> CommandExecution | None:
    """→ RUNNING, with the OS pid. Called immediately after a successful spawn."""
    def _fn(cmd: CommandExecution) -> None:
        now = time.monotonic()
        cmd.status = CommandStatus.RUNNING
        cmd.process_id = int(process_id) if process_id is not None else None
        cmd.started_at = _iso()
        cmd.started_mono = now
        # last_output_* stays empty: nothing has been said yet, and seeding it
        # here would make `idle` read 0 for a command that is already silent.
    return _mutate(command_id, _fn)


def heartbeat(command_id: str, lines: int = 1) -> CommandExecution | None:
    """RUNNING → STREAMING and bump `last_output_at`.

    ⚠️ Called by the runner for every output line. This is the cheap path — a
    dict lookup and two field writes under an uncontended lock — because it runs
    at output rate. Nothing here allocates, logs, notifies or touches the DB.
    """
    def _fn(cmd: CommandExecution) -> None:
        cmd.last_output_mono = time.monotonic()
        cmd.last_output_at = _iso()
        cmd.output_lines += max(0, int(lines))
        if cmd.status in (CommandStatus.RUNNING, CommandStatus.STARTING):
            # STREAMING is a distinct state because "running, has produced
            # output" and "running, has produced nothing" need different answers
            # from a stuck-detector.
            cmd.status = CommandStatus.STREAMING
    return _mutate(command_id, _fn)


def _settle(command_id: str, status: str, *, exit_code: int | None = None,
            stdout: str = "", stderr: str = "",
            error: str = "") -> CommandExecution | None:
    def _fn(cmd: CommandExecution) -> None:
        cmd.status = status
        cmd.completed_at = _iso()
        cmd.completed_mono = time.monotonic()
        if exit_code is not None:
            cmd.exit_code = int(exit_code)
        if stdout:
            cmd.stdout = _text(stdout, MAX_SNAPSHOT)
        if stderr:
            cmd.stderr = _text(stderr, MAX_SNAPSHOT)
        if error:
            cmd.error = _text(error, MAX_COMMAND)
    return _mutate(command_id, _fn)


def complete(command_id: str, exit_code: int, stdout: str = "",
             stderr: str = "") -> CommandExecution | None:
    """The normal exit. COMPLETED on 0, FAILED on anything else.

    The exit code decides, not the caller: a runner that reported COMPLETED for
    `exit 1` would make "did that command work?" unanswerable from the registry.
    """
    status = (CommandStatus.COMPLETED if int(exit_code) == 0
              else CommandStatus.FAILED)
    return _settle(command_id, status, exit_code=exit_code,
                   stdout=stdout, stderr=stderr)


def fail(command_id: str, error: str, exit_code: int = -1,
         stdout: str = "", stderr: str = "") -> CommandExecution | None:
    """The command could not run at all (spawn failure, bad shell, no such cwd)."""
    return _settle(command_id, CommandStatus.FAILED, exit_code=exit_code,
                   stdout=stdout, stderr=stderr, error=error)


def cancel(command_id: str, reason: str = "cancelled by user",
           exit_code: int | None = 130) -> CommandExecution | None:
    """The user stopped it. 130 mirrors the shell's SIGINT convention."""
    return _settle(command_id, CommandStatus.CANCELLED, exit_code=exit_code,
                   error=reason)


def cancel_active(reason: str = "cancelled by user",
                  **filters) -> list[CommandExecution]:
    """Settle every still-active execution in scope as CANCELLED (Task 7).

    ⚠️ THIS IS THE "command state" HALF OF A CANCEL, and it exists because the
    runner cannot be relied on to do it alone. `_run_once` / `stream_command`
    settle their OWN execution when their drain loop notices the cancel — but a
    Ctrl+C that lands between two commands, a disconnect, or a double-tap exit
    never reaches that branch, and the row then reads STREAMING for the rest of
    the session. "What is running?" would answer with a command whose process
    died minutes ago.

    ⚠️ RECORD-ONLY, exactly like the rest of this module: it touches no process
    (see the module docstring). The caller kills the tree, and calls this FIRST —
    the same record-then-kill ordering `terminal.kill_proc` uses, because killing
    closes the pipes and whichever verdict lands first wins.

    Returns the rows it settled, so a caller can report how many there were.
    """
    out: list[CommandExecution] = []
    for cmd in list_active(**filters):
        settled = cancel(cmd.id, reason=reason)
        if settled is not None and settled.status == CommandStatus.CANCELLED:
            out.append(settled)
    return out


def timed_out(command_id: str, reason: str = "timeout") -> CommandExecution | None:
    """A ceiling was exceeded. Recorded here; enforced by the runner (Task 6)."""
    return _settle(command_id, CommandStatus.TIMEOUT, error=reason)


def killed(command_id: str, reason: str = "killed") -> CommandExecution | None:
    """The process tree had to be destroyed — terminate() was not enough."""
    return _settle(command_id, CommandStatus.KILLED, error=reason)


# ── The watchdog (Task 6) ─────────────────────────────────────────────────────
# The ceilings live in `config`; applying them lives HERE, in one function both
# runners call. Two copies of this comparison would drift the first time one
# surface learned about a new ceiling — and the drift would be invisible, because
# each surface still looks right on its own.
#
# ⚠️ STILL NO PROCESS ACCESS. `watch()` returns a WORD; killing is the runner's
# job, because the runner is the only side that holds the handle. That split is
# what keeps a per-line lock off the cancel path (see the module docstring), and
# it is why this function is safe to call from a streaming loop.

W_OK = ""              # nothing to do
W_STUCK = "stuck"      # silent past the reporting threshold → ASK the user
W_TIMEOUT = "timeout"  # hard ceiling exceeded → terminate the tree
W_IDLE = "idle"        # silence ceiling exceeded → terminate the tree


def human_secs(seconds: float) -> str:
    """`32s` · `4m 12s` · `1h 03m`. One wording, both surfaces."""
    total = int(max(0.0, float(seconds or 0)))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m {total % 60:02d}s"
    return f"{total // 3600}h {(total % 3600) // 60:02d}m"


def live_line(cmd: CommandExecution) -> str:
    """The live state line the spec asks for, as ONE string.

    `Elapsed: 32s · Last output: 2s ago` — or `no output yet`, which is the case
    a stuck-detector cares about most and the one a bare "0s ago" would hide.
    """
    if cmd is None:
        return ""
    last = (f"Last output: {human_secs(cmd.idle)} ago" if cmd.last_output_mono
            else f"no output yet ({human_secs(cmd.idle)})")
    return f"Elapsed: {human_secs(cmd.elapsed)} · {last}"


def watch(command_id_or_cmd, *, stuck_after: float | None = None) -> str:
    """Classify a live execution: `W_OK` / `W_TIMEOUT` / `W_IDLE` / `W_STUCK`.

    Accepts an id or a snapshot, so a caller that already has the row does not pay
    for a second lookup at output rate.

    ⚠️ ORDER IS DELIBERATE: the two KILL verdicts are tested before the ASK
    verdict. A command that has blown a ceiling the caller set explicitly must be
    terminated, not offered a "keep waiting" button that would silently overrule
    the ceiling.

    ⚠️ A SETTLED OR UNKNOWN EXECUTION IS ALWAYS `W_OK`. The watchdog is fired from
    a loop that can still be draining a pipe after the child exited, and a verdict
    there would kill a tree that is already gone and rewrite a finished record.
    """
    cmd = (command_id_or_cmd if isinstance(command_id_or_cmd, CommandExecution)
           else get(command_id_or_cmd))
    if cmd is None or not cmd.is_active:
        return W_OK
    if cmd.timeout and cmd.elapsed >= cmd.timeout:
        return W_TIMEOUT
    if cmd.idle_timeout and cmd.idle >= cmd.idle_timeout:
        return W_IDLE
    if stuck_after and stuck_after > 0 and cmd.idle >= stuck_after:
        # ⚠️ The waiver is checked LAST so it can only ever suppress the ASK, not
        # a ceiling. `defer()` extends the ceilings separately and visibly.
        if cmd.waived_mono and time.monotonic() < cmd.waived_mono:
            return W_OK
        return W_STUCK
    return W_OK


def defer(command_id: str, grace: float,
          *, extend_limits: bool = True) -> CommandExecution | None:
    """[W]ait — the user has seen the warning and chooses to keep waiting.

    Suppresses the "appears stuck" verdict for *grace* seconds and pushes any
    ceiling out by the same amount, so answering [W]ait cannot be immediately
    overruled by a timeout landing a second later. Extending is the honest
    reading of the answer: the user was asked whether to keep waiting and said
    yes.
    """
    extra = max(0.0, float(grace or 0))

    def _fn(cmd: CommandExecution) -> None:
        cmd.waived_mono = time.monotonic() + extra
        if extend_limits:
            if cmd.timeout:
                cmd.timeout += extra
            if cmd.idle_timeout:
                cmd.idle_timeout += extra
    return _mutate(command_id, _fn)


# ── Reads ─────────────────────────────────────────────────────────────────────

def _filtered(*, session_id: str = "", task_id: str = "",
              surface: str = "") -> list[CommandExecution]:
    with _lock:
        items = [c.copy() for c in _registry.values()]
    if session_id:
        items = [c for c in items if c.session_id == str(session_id)]
    if task_id:
        items = [c for c in items if c.task_id == str(task_id)]
    if surface:
        items = [c for c in items if c.surface == str(surface)]
    return items


def list_active(**kw) -> list[CommandExecution]:
    """Live executions, oldest first — the order a "what is running?" view wants."""
    out = [c for c in _filtered(**kw) if c.is_active]
    out.sort(key=lambda c: c.started_mono or c.created_mono)
    return out


def list_all(limit: int = 100, **kw) -> list[CommandExecution]:
    """Every tracked execution, newest first."""
    out = _filtered(**kw)
    out.sort(key=lambda c: c.created_mono, reverse=True)
    return out[:max(0, int(limit))]


def active_count(**kw) -> int:
    return len(list_active(**kw))


def snapshot(limit: int = 50, **kw) -> dict:
    """The payload `/api/commands` returns. One declaration, both surfaces.

    ⚠️ Task 6: `stuck` is computed HERE, from `watch()`, rather than stored on the
    row. It is a function of the clock — a command becomes stuck by the passage of
    time, with nothing happening to write a flag — so a stored copy would be stale
    the moment nobody wrote to it, which is exactly the silence it must detect.
    """
    everything = list_all(limit=limit, **kw)
    live = [c for c in everything if c.is_active]
    stuck = [c for c in live
             if watch(c, stuck_after=_config.CMD_STUCK_AFTER) == W_STUCK]
    return {
        "commands": [c.to_dict() for c in everything],
        "active": [c.to_dict() for c in live],
        "stuck": [{**c.to_dict(), "state": live_line(c)} for c in stuck],
        "limits": {
            "timeout": _config.CMD_TIMEOUT or None,
            "idle_timeout": _config.CMD_IDLE_TIMEOUT or None,
            "stuck_after": _config.CMD_STUCK_AFTER or None,
        },
        "counts": {
            "total": len(everything),
            "active": len(live),
            "stuck": len(stuck),
            "failed": sum(1 for c in everything if c.failed),
        },
    }


# ── Housekeeping ──────────────────────────────────────────────────────────────

def forget(command_id: str) -> bool:
    """Drop one execution. Cleanup, not a state transition."""
    with _lock:
        return _registry.pop(str(command_id or ""), None) is not None


def reset() -> None:
    """Empty the registry. For tests and for a full session teardown."""
    with _lock:
        _registry.clear()
