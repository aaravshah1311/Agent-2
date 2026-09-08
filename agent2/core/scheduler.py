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
from agent2.core import metrics as _metrics

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

    ⚠️ QUEUE WAIT IS MEASURED HERE (Task 27) BECAUSE PICKUP IS THE ONLY MOMENT IT
    IS KNOWABLE. `submit()` records the enqueue stamp and cannot know when a
    worker will get to the job; `stats()` can report the queue's *depth* but depth
    is not latency — a queue of one behind a ten-minute turn is a worse wait than
    a queue of twenty behind fast ones. A job cancelled while queued is recorded
    too, under its own label: it waited, and how long callers wait before giving
    up is the number that says the pool is too small.
    """
    me = threading.get_ident()
    while True:
        job = _q.get()
        try:
            if job is None:                     # shutdown sentinel
                return
            waited = (time.monotonic() - job.enqueued_at) * 1000.0
            with _state_lock:
                if job in _pending:
                    _pending.remove(job)
                dropped = job.cancelled.is_set()
                if dropped:
                    _stats["cancelled"] += 1
                else:
                    _running[me] = job
            # ⚠️ Outside `_state_lock`. Nothing that is not scheduler state runs
            # inside it — a metric is cheap, but "cheap" is how a lock held on the
            # hot path of every turn starts.
            _metrics.observe(_metrics.QUEUE_WAIT, waited,
                             "cancelled" if dropped else "run")
            if dropped:
                continue
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


def _drain_queue() -> int:
    """Empty `_q`, cancelling the real jobs and discarding stale sentinels.

    Returns how many queued jobs were discarded, so `shutdown()` can account for
    them instead of leaving `_pending` and `_q` describing different queues.
    """
    dropped = 0
    while True:
        try:
            item = _q.get_nowait()
        except queue.Empty:
            return dropped
        try:
            if item is None:                    # a sentinel nobody claimed
                continue
            item.cancelled.set()
            dropped += 1
        finally:
            _q.task_done()


def shutdown(timeout: float = 2.0) -> bool:
    """Stop the workers. Used by tests and orderly exit, not on the hot path.

    Returns True when every worker this call knew about has exited. False means at
    least one was still busy: it keeps its sentinel and stops when its turn
    returns, and it stays *tracked* until then.

    ⚠️ THE ORDER IS DRAIN → SIGNAL → JOIN → FORGET ONLY WHAT DIED. Each step is
    here because the obvious version failed silently, in three different ways:

    * A sentinel posted onto a FULL queue raises `queue.Full`, and swallowing that
      meant shutting down a **saturated** pool told nobody to stop — and a
      saturated pool is the one anybody shuts down. Draining first also clears any
      sentinel an earlier shutdown left behind, which a freshly spawned worker
      would otherwise take as its own stop order.
    * `_workers.clear()` made `stats()["workers"]` read 0 whether or not the
      threads exited, so a worker that outlived its join became unreachable: it
      could never be signalled again, and it raced the NEXT pool's sentinels. Two
      pools then drained one queue and the concurrency ceiling this module exists
      to enforce was quietly doubled — the failure the module's own docstring
      calls capacity that never comes back, from the other end.
    * Clearing `_pending` while `_q` still held those jobs left the two describing
      different queues: `stats()["queued"]` read 0, and a restarted pool ran turns
      whose callers had already been told they were discarded.

    A still-busy worker is counted as capacity by `start()` in the meantime, which
    is what stops a restart from stacking a second pool on the first.
    """
    ok = True
    with _start_lock:
        discarded = _drain_queue()
        alive = [t for t in _workers if t.is_alive()]
        posted = 0
        deadline = time.monotonic() + max(0.0, timeout)
        while posted < len(alive):
            try:
                _q.put_nowait(None)
            except queue.Full:
                # `maxsize` can legitimately be smaller than the worker count
                # (AGENT2_MAX_QUEUED_TURNS=1 against eight workers), so room only
                # appears as workers consume. Wait for it against the deadline
                # rather than dropping the stop order on the floor.
                if time.monotonic() >= deadline:
                    _log_failure("shutdown", RuntimeError(
                        f"{len(alive) - posted} of {len(alive)} workers could not "
                        "be told to stop — the queue stayed full"))
                    ok = False
                    break
                time.sleep(0.005)
                continue
            posted += 1
        for t in alive:
            t.join(timeout)
        _workers[:] = [t for t in _workers if t.is_alive()]
        if _workers:
            ok = False
    with _state_lock:
        _pending.clear()
        _running.clear()
        _stats["cancelled"] += discarded
    return ok
