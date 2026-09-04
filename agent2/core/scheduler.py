# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2.core.scheduler
─────────────────────
A bounded worker pool for agent turns.

The problem
  Every `chat_message` spawned a raw `threading.Thread`. Nothing bounded how many
  ran at once, so N browser tabs (or one reconnect loop, or a script hammering the
  socket) meant N concurrent Gemini calls, each holding DB connections, an HTTP
  client, and possibly child processes. The failure mode is not a clean error —
  it is the whole machine degrading until turns start timing out.

What this adds
  A fixed pool of worker threads drains a bounded queue. Past the queue limit a
  turn is REJECTED with a message the user can see, rather than silently dropped
  or accepted into an unbounded backlog. Backpressure you can observe beats
  collapse you cannot.

Failsafe posture (the rule this repo runs on: an optimization layered OVER a
working default, never a replacement for one)
  * `AGENT2_MAX_CONCURRENT_TURNS=0` disables the pool entirely. `submit()` then
    returns DISABLED and the caller runs the turn exactly the way it did before
    this module existed — a plain daemon thread.
  * If worker threads cannot be started (thread exhaustion), `submit()` returns
    DISABLED too. Housekeeping must never be the reason a turn will not run.
  * A worker that catches an exception keeps looping. A crash in one turn must
    not permanently cost the pool a worker — that would leak capacity until the
    pool silently reached zero and every turn queued forever.

Cancellation — the reason this module owns a cancel path of its own
  `sessions.open()` is called INSIDE the agent loop, so a turn that is still
  queued has no Session and therefore no cancel token. `sessions.cancel()` cannot
  reach it. Without the `cancel()` here, pressing Stop on a queued turn would look
  dead, and the agent would then start talking minutes later. That window was
  milliseconds before there was a queue; it is now however long the backlog is.
"""

from __future__ import annotations

import logging as _logging
import os
import queue
import threading
import time
from dataclasses import dataclass, field

from agent2.core import logging as alog

# ── Tunables ──────────────────────────────────────────────────────────────────
# Turns are I/O-bound (waiting on a model), so this is not a CPU count — it caps
# how much concurrent *state* exists: DB connections, HTTP clients, subprocesses.
# 8 matches the default DB pool, so the common case never queues on connections.
_MAX_WORKERS = int(os.environ.get("AGENT2_MAX_CONCURRENT_TURNS", "8"))
_MAX_QUEUE = int(os.environ.get("AGENT2_MAX_QUEUED_TURNS", "64"))

# submit() outcomes. Strings rather than an enum so a caller can log one directly.
QUEUED = "queued"
DISABLED = "disabled"      # caller must run the work itself — see module docstring
REJECTED = "rejected"      # backlog full; the caller must tell the user


@dataclass(eq=False)
class _Job:
    """One queued turn. `cancelled` is what Stop reaches before a worker starts.

    `eq=False` on purpose: jobs are tracked in lists and removed by identity.
    Field-wise equality would let two identical submissions (same fn, args, sid,
    chat_id) compare equal, so `_pending.remove(job)` could drop the wrong one and
    leave a cancelled job looking live.
    """
    fn: object
    args: tuple
    kwargs: dict
    sid: str | None
    chat_id: str | None
    enqueued_at: float = field(default_factory=time.monotonic)
    cancelled: threading.Event = field(default_factory=threading.Event)


_q: queue.Queue = queue.Queue(maxsize=max(1, _MAX_QUEUE))
_workers: list[threading.Thread] = []
_start_lock = threading.Lock()
_state_lock = threading.Lock()
_pending: list[_Job] = []          # queued but not yet picked up
_running: dict[int, _Job] = {}     # worker thread ident → job in flight

_stats = {
    "submitted": 0,
    "completed": 0,
    "failed": 0,
    "rejected": 0,
    "cancelled": 0,
    "high_water_queue": 0,
    "worker_starts": 0,
}


def _log_failure(what: str, exc: BaseException) -> None:
    """Never let logging be the thing that breaks the pool."""
    try:
        alog.event("scheduler.error", level=_logging.WARNING,
                   what=what, error=str(exc)[:200])
    except Exception:
        pass


# ── Worker loop ───────────────────────────────────────────────────────────────

def _worker() -> None:
    """Drain the queue forever.

    The broad `except` is deliberate and is NOT a swallowed error: the job's own
    caller is responsible for reporting its failure (`_dispatch_agent` already
    does). What matters here is that this thread survives to take the next job.
    A worker lost to an escaped exception is capacity that never comes back.
    """
    me = threading.get_ident()
    while True:
        job = _q.get()
        try:
            if job is None:                     # shutdown sentinel
                return
            with _state_lock:
                if job in _pending:
                    _pending.remove(job)
                if job.cancelled.is_set():
                    _stats["cancelled"] += 1
                    continue
                _running[me] = job
            try:
                job.fn(*job.args, **job.kwargs)
                with _state_lock:
                    _stats["completed"] += 1
            except Exception as exc:
                with _state_lock:
                    _stats["failed"] += 1
                _log_failure("turn", exc)
            finally:
                with _state_lock:
                    _running.pop(me, None)
        finally:
            _q.task_done()


def start(n: int | None = None) -> bool:
    """Ensure the worker pool is running. Idempotent. Never raises.

    Returns False when the pool is disabled or could not be started — the caller
    then falls back to running turns on its own thread, i.e. the pre-scheduler
    behaviour.
    """
    want = _MAX_WORKERS if n is None else n
    if want <= 0:
        return False
    with _start_lock:
        # Dead threads are dropped first so a crashed worker is replaced rather
        # than counted. The subtraction is what makes start() idempotent: once the
        # pool is full this is range(0) and nothing is spawned. (An `if len(alive)
        # >= want: return True` short-circuit used to sit here too — it was exactly
        # equivalent to the empty range, so it was two guards for one property.)
        alive = [t for t in _workers if t.is_alive()]
        _workers[:] = alive
        for _ in range(want - len(alive)):
            try:
                t = threading.Thread(target=_worker, name="a2-turn", daemon=True)
                t.start()
            except Exception as exc:
                # Thread exhaustion. Whatever started is still useful; if nothing
                # did, report False so the caller degrades to a direct thread.
                _log_failure("worker start", exc)
                break
            _workers.append(t)
            with _state_lock:
                _stats["worker_starts"] += 1
        return bool(_workers)


def submit(fn, *args, sid: str | None = None, chat_id: str | None = None,
           **kwargs) -> str:
    """Queue *fn* for a worker. Returns QUEUED, REJECTED, or DISABLED.

    Never blocks: this runs on the Socket.IO handler thread, and blocking there
    would stall every other client, turning a busy queue into a dead server.
    """
    if not start():
        return DISABLED

    job = _Job(fn=fn, args=args, kwargs=kwargs, sid=sid, chat_id=chat_id)
    with _state_lock:
        _pending.append(job)
    try:
        _q.put_nowait(job)
    except queue.Full:
        with _state_lock:
            if job in _pending:
                _pending.remove(job)
            _stats["rejected"] += 1
        return REJECTED

    with _state_lock:
        _stats["submitted"] += 1
        depth = _q.qsize()
        _stats["high_water_queue"] = max(_stats["high_water_queue"], depth)
    return QUEUED


def cancel(sid: str, chat_id: str | None = None) -> int:
    """Cancel QUEUED turns for a socket (or one chat on it). Returns how many.

    Only queued work is reachable here. A turn already running owns a Session, so
    `sessions.cancel()` is what stops it — the two are complementary, and the
    socket handlers call both.
    """
    n = 0
    with _state_lock:
        for job in _pending:
            if job.sid != sid:
                continue
            if chat_id is not None and job.chat_id != chat_id:
                continue
            if not job.cancelled.is_set():
                job.cancelled.set()
                n += 1
    return n


def stats() -> dict:
    """Diagnostic snapshot. Nothing reads these to make a decision."""
    with _state_lock:
        pending = len(_pending)
        running = len(_running)
        snapshot = dict(_stats)
    snapshot.update({
        "workers": len([t for t in _workers if t.is_alive()]),
        "max_workers": _MAX_WORKERS,
        "max_queue": _MAX_QUEUE,
        "queued": pending,
        "running": running,
        "enabled": _MAX_WORKERS > 0,
    })
    return snapshot


def shutdown(timeout: float = 2.0) -> None:
    """Stop the workers. Used by tests and orderly exit, not on the hot path."""
    with _start_lock:
        alive = [t for t in _workers if t.is_alive()]
        for _ in alive:
            try:
                _q.put_nowait(None)
            except queue.Full:
                pass
        for t in alive:
            t.join(timeout)
        _workers.clear()
    with _state_lock:
        _pending.clear()
        _running.clear()
