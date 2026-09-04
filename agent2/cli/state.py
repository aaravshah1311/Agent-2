# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/cli/state.py
───────────────────
The CLI's mutable session state, and the cancellation primitives that read it.

⚠️ WHY THIS MODULE EXISTS
──────────────────────────
Four values used to be module globals rebound through `global`:
`_CLI_CHAT`, `_active_proc`, `_last_ctrl_c`, and `_CURRENT_THEME`. That worked
only because every reader lived in one 3,400-line file — rebinding the global was
visible to all of them.

Split across modules it silently stops working: `from .state import _CLI_CHAT`
binds the value AT IMPORT, so `/load` would update the writer's copy and the
reader would keep seeing `None` forever, with no exception and no traceback.
Exactly the palette snapshot bug, in state rather than colour.

So the rebound values live as ATTRIBUTES on ONE shared object, `S`, reached as
`S.chat` / `S.active_proc`. `__slots__` makes a typo an AttributeError instead of
a dead attribute every reader ignores. (`_CURRENT_THEME` got the same treatment
one layer down, as `theme.P.THEME`.)

Events and locks are NOT on `S`: they are mutated in place, never rebound, so
importing them by name is safe and reads better at the call sites.
"""

import threading
import time

# ── Cancellation ───────────────────────────────────────────────────────────────
# Set by InputController when ESC/Ctrl+C is pressed during an agent turn.
# Cleared at the start of each process_turn call.
cancel_event = threading.Event()
exit_event = threading.Event()      # double Ctrl+C within the window → quit app
DOUBLE_TAP_WINDOW = 1.5             # seconds: two Ctrl+C within this → exit

# Guards `S.active_proc` — the process spawned by the current run_command, held
# so a cancel can kill it instantly instead of waiting for it to finish.
proc_lock = threading.Lock()


class _Session:
    """Everything about this CLI session that changes while it runs."""

    __slots__ = ("active_proc", "chat", "key_handler", "last_ctrl_c")

    def __init__(self):
        # ⚠️ `chat` starts as None and STAYS None until there is something worth
        # saving, so launching the CLI and quitting leaves no empty conversation
        # behind. Every reader must handle None — use `bind_chat(id)` rather than
        # `S.chat.update(...)`.
        self.chat: dict | None = None
        self.active_proc = None
        self.last_ctrl_c: float = 0.0
        # Task 6: set while a command is being reported as stuck, so the ONE key
        # listener can route [R]/[K]/[W] without a second reader.
        self.key_handler = None


S = _Session()


# ── Child-process registry ─────────────────────────────────────────────────────

def register_proc(proc) -> None:
    with proc_lock:
        S.active_proc = proc


def clear_proc(proc) -> None:
    """Clear the registry only if *proc* is still the one registered.

    The identity check matters: a slow process being cleared after a newer one
    registered would otherwise unregister the live process, and a later cancel
    would find nothing to kill.
    """
    with proc_lock:
        if S.active_proc is proc:
            S.active_proc = None


def has_active_proc() -> bool:
    """True while a registered shell process is still running.

    Used by the cancel path to decide whether there is anything to *announce*
    stopping. A "Stopping current command…" printed when no command is running is
    a lie the user has no way to check.
    """
    with proc_lock:
        proc = S.active_proc
    if proc is None:
        return False
    try:
        return proc.poll() is None
    except Exception:
        return False


def record_cancel(reason: str = "cancelled by user") -> list[str]:
    """Settle this surface's live executions as CANCELLED. Returns their ids.

    ⚠️ Task 7: RECORD-ONLY, and called BEFORE the tree kill — the ordering Tasks 5
    and 6 established. Killing closes the pipes, the runner falls out of `drain`
    and settles the execution itself, and `_mutate` refuses to move a settled row:
    so a record written afterwards loses the race and the transcript reads "exit 1"
    with nothing saying the user stopped it.

    ⚠️ AND IT SWEEPS, rather than settling one known id. `_run_once` settles its
    own execution when its drain loop sees the cancel — but a Ctrl+C between two
    commands, or the double-tap exit, never reaches that branch, and the row would
    read STREAMING for the rest of the session.
    """
    from agent2.core import commands
    return [c.id for c in commands.cancel_active(reason=reason, surface="cli")]


def stop_active_command(reason: str = "cancelled by user") -> None:
    """The whole silent cancel: command state first, then the process tree.

    The announcing version lives in `cli/runtime.interrupt` — printing belongs a
    layer up, and this must stay callable from anywhere, including a signal
    handler.
    """
    record_cancel(reason)
    kill_active_proc()


def kill_active_proc() -> None:
    """Terminate the currently-running shell process AND its children, if any.

    ⚠️ Task 5: this goes through `core/procio.terminate_tree`, not
    `proc.terminate()`. Terminating only the shell orphaned everything it had
    started — `npm test`, `pytest -n auto`, `docker build` — which kept running,
    kept the pipes open, and so kept the command from ever being reported as
    finished. A cancel that leaves the work running is not a cancel.
    """
    with proc_lock:
        proc = S.active_proc
    if proc is None:
        return
    from agent2.core import procio
    procio.terminate_tree(proc, grace=1.0)


def cancelled() -> bool:
    return cancel_event.is_set()


# ── Task 6: the stuck-command key handler ─────────────────────────────────────
# The InputController owns the only live key reader during a turn. When a command
# looks stuck it wants to offer [R]etry / [K]ill / [W]ait — but the controller is
# the one receiving the keys. So instead of a second competing reader (two
# `msvcrt.getwch()` readers steal characters from each other), the controller
# routes every key through this slot first: the handler returns True when it
# consumed the key, and only keys it passes on reach the queue/cancel logic.
#
# ⚠️ IT LIVES ON `S`, NOT AS A MODULE GLOBAL — see this module's docstring. A
# rebound global would be read as `None` forever by anything that imported it by
# name, silently and with no traceback.


def set_key_handler(fn) -> None:
    S.key_handler = fn


def clear_key_handler() -> None:
    S.key_handler = None


def dispatch_key(ch: str) -> bool:
    """Offer *ch* to the active handler. True when it was consumed.

    ⚠️ TOTAL. A raising handler must not kill the key listener — losing the
    listener would cost the user ESC-to-cancel for the rest of the turn, which is
    a far worse failure than a missed keypress.
    """
    fn = S.key_handler
    if fn is None:
        return False
    try:
        return bool(fn(ch))
    except Exception:
        return False



def trigger_cancel(is_ctrl_c: bool, reason: str = "cancelled by user") -> None:
    """Called from the SIGINT handler and the key-listener thread.

    A single press cancels the running turn; a second Ctrl+C within
    DOUBLE_TAP_WINDOW quits the whole app.

    ⚠️ Ctrl+C can be delivered TWICE for one physical press — the SIGINT handler
    and the raw key listener can both fire within a few milliseconds. Two
    triggers closer than 50 ms apart are treated as the same press, so a single
    Ctrl+C never trips the double-tap exit.

    ⚠️ Task 7: IT CANCELS THE COMMAND, IT DOES NOT END THE SESSION.
    Nothing here raises, exits, or unwinds anything: it sets a flag, settles the
    command state, and kills the child's process tree. The turn loop in
    `agent2cli.process_turn` notices the flag and returns to the prompt with the
    conversation intact. The announcing wrapper is `cli/runtime.interrupt` —
    printing lives a layer up so this stays safe to call from a signal handler.
    """
    now = time.monotonic()
    if is_ctrl_c:
        dt = now - S.last_ctrl_c
        # 50ms..window → a genuine second tap while already cancelling → exit.
        if 0.05 < dt < DOUBLE_TAP_WINDOW and cancel_event.is_set():
            exit_event.set()
        # Ignore a duplicate delivery of the same press (dt <= 50ms).
        if dt > 0.05:
            S.last_ctrl_c = now
    # ⚠️ THE FLAG IS SET BEFORE THE KILL, and that is the load-bearing order.
    # `kill_active_proc` blocks until the tree is dead; the moment it dies the
    # pipes close and the runner falls out of `drain`. It then asks `cancelled()`
    # what happened — and with the flag not yet set it takes the ordinary path,
    # calls `proc.wait()` and settles the execution with the *shell's* exit code.
    # The record would read FAILED / "exit 1", with nothing anywhere saying the
    # user cancelled it, and `run_cmd_stream` would return that to the model as a
    # genuine command failure.
    cancel_event.set()
    stop_active_command(reason)
