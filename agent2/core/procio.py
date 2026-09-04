# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/core/procio.py
─────────────────────
THE process-I/O layer (Task 5). Two jobs, both of which used to be duplicated
per-surface and got them subtly wrong in different ways:

  1. `drain()`      — read a child's stdout AND stderr concurrently, so neither
                      pipe can fill up and block the child.
  2. `terminate_tree()` — end a process AND its descendants.

⚠️ THE DEADLOCK THIS FIXES (reproduced before the fix, on Windows)
───────────────────────────────────────────────────────────────────
The CLI runner used to read stdout to EOF and only afterwards read stderr:

    for line in proc.stdout:   ...        # blocks until stdout closes
    stderr_lines = [proc.stderr.read()]   # never reached

A child that writes more to stderr than the OS pipe buffer holds (~4-64 KB)
blocks in `write()` waiting for someone to read it. Nobody will: the parent is
blocked reading stdout, which the child cannot reach because it is stuck writing
stderr. Both sides wait forever. A 400 KB stderr burst hung the runner
indefinitely — no error, no exit, no output. This is the "Agent2 becomes stuck
and never responds" report, and no timeout value fixes it because the process is
not slow, it is wedged.

The cure is structural: NEVER serialize the two streams. One reader thread per
pipe, each draining as fast as the child writes, both feeding one queue that the
caller consumes in order of arrival.

⚠️ WHY THREADS AND NOT `select`
The old POSIX branch multiplexed with `select.select`, which is correct — but
`select` does not work on Windows pipes, so Windows kept the broken serial path.
One mechanism that behaves identically on both platforms is worth more here than
the theoretically leaner one on half of them: the bug only ever existed on the
platform that got the fallback.

⚠️ THE CALLER IS NEVER BLOCKED PAST `poll`
`drain()` wakes at least every `poll` seconds even in total silence, so a caller
can check for cancellation, tick a heartbeat, or notice a stuck process while the
child is still running. A stuck subprocess must not freeze the agent.

⚠️ TREE KILL, NOT PROCESS KILL
`proc.terminate()` ends the shell, not what the shell started. `npm test`,
`pytest -n auto`, `docker build` and every security scanner spawn children, and
killing only the parent orphans them: they keep running, keep holding the pipes
open, and keep the command from ever being reported as finished. So the kill
walks the tree — `taskkill /T` on Windows, the process group on POSIX — and
always falls back to killing the direct child if the tree walk is unavailable.

⚠️ AND A KILL MUST STILL BE ABLE TO END THE DRAIN
Even a tree kill can leave a handle behind: a program the shell starts in the
window between the kill landing and the shell noticing inherits the write end, so
`readline()` blocks on a pipe nobody will ever write to again and the pane never
reports the command as finished. Closing our read end does NOT help — closing a
buffered reader another thread is blocked inside waits for that thread's lock, so
the close hangs too (measured: a full 60 s). The bound therefore lives in
`drain`, which stops waiting once the child is gone and both pipes have been
quiet for `after_exit` seconds. See `drain`.

This module owns process plumbing only. It records no state: `core/commands.py`
is where an execution's status lives, and it deliberately never touches a
process. Keeping the two apart is what stops an output-rate lock from ending up
on the cancel path.
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time

IS_WIN = sys.platform.startswith("win")

# How long a reader thread is given to notice a closed pipe before we stop
# waiting for it. The threads are daemons, so a wedged reader can never hold up
# process exit — this bound only keeps `drain` from lingering.
_JOIN_TIMEOUT = 1.0

# Popen kwargs that put the child somewhere the whole tree can be signalled.
#
# ⚠️ POSIX: `start_new_session` is MANDATORY, not a nicety. `terminate_tree`
# signals the child's process GROUP, and without a new session the child shares
# OUR group — so the kill would take down Agent2 itself. `_signal_group` refuses
# that case as a second line of defence, but the spawn is where it is prevented.
#
# ⚠️ Windows: deliberately EMPTY. `taskkill /T` walks the real parent-child tree
# and needs no group flag, while `CREATE_NEW_PROCESS_GROUP` would change how
# console Ctrl+C reaches children — a live behaviour change in the CLI, for no
# gain. The smallest change that works is the one that ships.
if IS_WIN:
    GROUP_KWARGS: dict = {}
else:
    GROUP_KWARGS = {"start_new_session": True}


def _pump(stream, tag: str, out: queue.Queue) -> None:
    """Read *stream* line by line into *out* until EOF, then post the EOF mark.

    Every exception is swallowed and turned into an EOF: a decode error or a pipe
    torn down under us must end this reader quietly, not kill the thread with a
    traceback and leave `drain` waiting for a sentinel that never arrives.
    """
    try:
        for line in iter(stream.readline, ""):
            out.put((tag, line))
    except Exception:
        pass
    finally:
        out.put((tag, None))
        try:
            stream.close()
        except Exception:
            pass


def drain(proc: subprocess.Popen, *, poll: float = 0.2, after_exit: float = 0.5):
    """Yield `(stream, line)` for a child's output as it arrives.

    *stream* is `"stdout"` or `"stderr"`; *line* keeps its trailing newline.
    Yields `("tick", "")` at least every *poll* seconds while the child is still
    producing, so a silent command still gives the caller a chance to act.

    ⚠️ BOTH PIPES ARE READ CONCURRENTLY. That is the entire point — see the module
    docstring. Do not "simplify" this into reading one stream and then the other.

    Normally ends when both pipes hit EOF.

    ⚠️ BUT EOF IS NOT GUARANTEED, so it is not the only exit.
    A pipe's write end is held by every process that inherited it, not just the
    one we spawned. Kill a shell in the window before it starts its program and
    that program comes up afterwards as an orphan still holding the handle:
    `readline()` then blocks forever on a pipe with no writer left that will ever
    write. Observed as a kill that returned promptly while the pane sat there
    claiming the command was still running.

    So once the child has exited AND neither pipe has produced anything for
    *after_exit* seconds, this stops waiting and returns. The reader threads are
    daemons parked in `readline`, so abandoning them costs nothing and cannot
    delay interpreter exit. The grace period is what keeps this from truncating a
    normal fast command, where the child is reaped a hair before its last lines
    have been pulled through the pipe.

    It does NOT call `wait()`: the caller owns the exit code, and reaping here
    would hide a non-zero status from it.
    """
    q: queue.Queue = queue.Queue()
    threads = []
    for stream, tag in ((proc.stdout, "stdout"), (proc.stderr, "stderr")):
        if stream is None:
            continue
        t = threading.Thread(target=_pump, args=(stream, tag, q), daemon=True)
        t.start()
        threads.append(t)

    open_streams = len(threads)
    if not open_streams:
        return

    quiet_since: float | None = None
    while open_streams:
        try:
            tag, line = q.get(timeout=poll)
        except queue.Empty:
            # Silence is not an error: hand the caller a tick so it can check for
            # cancellation or a stuck process, then keep waiting — unless the
            # child is gone and has been quiet long enough that the only thing
            # still holding the pipe is an orphan that will never write to it.
            yield ("tick", "")
            if proc.poll() is None:
                quiet_since = None
                continue
            now = time.monotonic()
            if quiet_since is None:
                quiet_since = now
            elif now - quiet_since >= after_exit:
                return
            continue
        quiet_since = None
        if line is None:
            open_streams -= 1
            continue
        yield (tag, line)

    for t in threads:
        t.join(timeout=_JOIN_TIMEOUT)


def close_stdin(proc: subprocess.Popen) -> None:
    """Close the child's stdin, best-effort.

    A child that reads stdin and is never given any (`cat`, an interactive
    installer, `git commit` with no message) waits forever on input the agent is
    never going to send. Closing the pipe turns that infinite wait into an EOF the
    child can act on, which is the difference between "finished" and "hung".
    """
    try:
        if proc.stdin and not proc.stdin.closed:
            proc.stdin.close()
    except Exception:
        pass


def _kill_tree_win(pid: int) -> bool:
    """`taskkill /T /F` — the only reliable way to reach a tree on Windows."""
    try:
        res = subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=5, check=False,
        )
        return res.returncode == 0
    except Exception:
        return False


def _signal_group(proc: subprocess.Popen, sig) -> bool:
    """Send *sig* to the child's whole process group (POSIX).

    ⚠️ Refuses to signal our OWN group. If the child was spawned without
    `start_new_session` its group is ours, and `killpg` would deliver the signal
    to Agent2 as well — killing the app to cancel one command. Returning False
    sends the caller down the single-process path instead, which is merely less
    thorough rather than fatal.
    """
    try:
        pgid = os.getpgid(proc.pid)
        if pgid == os.getpgrp():
            return False
        os.killpg(pgid, sig)
        return True
    except Exception:
        return False


def terminate_tree(proc: subprocess.Popen, *, grace: float = 2.0) -> bool:
    """End *proc* and every process it started. Returns True if it is gone.

    Polite first, forceful second: the tree gets `grace` seconds to exit on a
    terminate before being killed outright, so a well-behaved tool still gets to
    flush its output and clean up its temp files.

    ⚠️ IT DOES NOT — AND MUST NOT — CLOSE THE PIPES. Killing the tree is not
    enough on its own: an orphan that escaped the kill still holds the write end,
    and the reader would block on it forever. But closing our read end from here
    does not help either, because closing a buffered reader that another thread is
    blocked inside waits for that thread's lock, so the close hangs too (measured:
    a full 60 s). The bound therefore lives in `drain`, which stops waiting once
    the child is gone and both pipes have been quiet for `after_exit` seconds. A
    cancel that leaves the caller blocked is not a cancel — see `drain`.

    ⚠️ NEVER RAISES. Cancellation must not fail. A process that has already
    exited, a pid that has been recycled, a platform without `killpg` — all of
    them mean "there is nothing left to kill", which is success from the caller's
    point of view, not an error to propagate into a turn.
    """
    if proc is None:
        return True
    if proc.poll() is not None:
        return True

    if IS_WIN:
        _kill_tree_win(proc.pid)
    else:
        import signal
        if not _signal_group(proc, signal.SIGTERM):
            try:
                proc.terminate()
            except Exception:
                pass

    deadline = time.monotonic() + max(0.0, grace)
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return True
        time.sleep(0.05)

    # Still alive: stop asking.
    if IS_WIN:
        _kill_tree_win(proc.pid)
        try:
            proc.kill()
        except Exception:
            pass
    else:
        import signal
        if not _signal_group(proc, signal.SIGKILL):
            try:
                proc.kill()
            except Exception:
                pass

    try:
        proc.wait(timeout=2.0)
    except Exception:
        pass
    return proc.poll() is not None
