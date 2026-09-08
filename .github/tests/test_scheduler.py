# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
Tests for the bounded turn scheduler (agent2.core.scheduler).

Run from the repo root:  python -m pytest .github/tests/test_scheduler.py -v

Why this exists
───────────────
Every `chat_message` used to spawn a raw `threading.Thread`. Nothing bounded the
count, so N tabs (or one reconnect loop) meant N concurrent Gemini calls, each
holding DB connections, an HTTP client, and possibly child processes.

A queue is only an improvement if it cannot lose work. The tests below are
weighted accordingly: the interesting cases are not "does it run a job" but

  * a turn past the backlog limit is REJECTED loudly, never silently dropped
  * the pool being off/unstartable still runs the turn (the failsafe)
  * a crashing turn does not cost a worker permanently
  * Stop reaches a turn that is still QUEUED — which `sessions.cancel()`
    structurally cannot, because a queued turn has no Session yet
"""

import threading
import time

import pytest

from agent2.core import scheduler


@pytest.fixture(autouse=True)
def _clean_scheduler():
    """Each test gets a pool with no history and no leftover workers."""
    scheduler.shutdown()
    for k in scheduler._stats:
        scheduler._stats[k] = 0
    yield
    scheduler.shutdown()


def _saturate(gate: threading.Event, n: int | None = None) -> None:
    """Occupy every worker so the next submit() is guaranteed to queue.

    Returns once the workers are actually blocked, not merely submitted — the
    difference is what makes the queueing tests deterministic instead of timing
    dependent.
    """
    n = n if n is not None else scheduler._MAX_WORKERS
    started = threading.Semaphore(0)

    def blocker():
        started.release()
        gate.wait(10)

    for _ in range(n):
        scheduler.submit(blocker, sid="saturate")
    for _ in range(n):
        assert started.acquire(timeout=5), "a worker never picked up its job"


# ── The declared bound ────────────────────────────────────────────────────────
# Deliberately FIRST in the file. Every test below drives the real pool, so with
# an absurd cap they hang in the fixture's shutdown() long before reaching an
# assertion — under `-x` a bad default would surface as a 2-minute timeout rather
# than a failed test. Checking the constant up front turns that into an instant,
# readable failure.

def test_the_default_worker_cap_is_modest():
    """Pins the cap against an INDEPENDENT number, not against itself.

    `test_concurrency_is_actually_bounded` compares the observed peak to
    `scheduler._MAX_WORKERS` — the same value a bad change would raise — so it
    verifies the queue mechanism honours its own declared cap but says nothing
    about whether that cap is sane. This is the guard against "fixing" pressure
    by setting the default to 500, which reintroduces exactly the unbounded
    concurrency the pool exists to prevent.

    The literals are deliberately loose: this asserts a bound exists and is in
    the right order of magnitude, not that it is precisely 8.
    """
    assert 1 <= scheduler._MAX_WORKERS <= 32, (
        f"default worker cap is {scheduler._MAX_WORKERS}; each worker can hold a "
        f"DB connection, an HTTP client and child processes, so a large default "
        f"is unbounded concurrency wearing a bound's clothes"
    )
    assert 1 <= scheduler._MAX_QUEUE <= 4096, (
        f"default backlog is {scheduler._MAX_QUEUE}; an enormous queue defers "
        f"rejection instead of applying backpressure"
    )


# ── The basics ────────────────────────────────────────────────────────────────

def test_a_submitted_turn_runs():
    done = threading.Event()
    assert scheduler.submit(done.set, sid="s1", chat_id="c1") == scheduler.QUEUED
    assert done.wait(5), "the job never ran"


def test_args_and_kwargs_reach_the_job():
    got = {}
    scheduler.submit(lambda *a, **k: got.update(args=a, kw=k),
                     1, 2, sid="s1", chat_id="c1")
    for _ in range(50):
        if got:
            break
        time.sleep(0.02)
    # sid/chat_id are scheduler bookkeeping and must NOT leak into the callable.
    assert got == {"args": (1, 2), "kw": {}}, got


def test_concurrency_is_actually_bounded():
    """The whole point. More submissions than workers must not all run at once.

    Note this compares against `scheduler._MAX_WORKERS` itself, so it pins the
    mechanism (the pool never exceeds its declared cap) rather than the value —
    `test_the_default_worker_cap_is_modest` above is what pins the value.
    """
    live = threading.Semaphore(0)
    gate = threading.Event()
    peak = {"n": 0}
    lock = threading.Lock()

    def job():
        with lock:
            peak["n"] += 1
            current = peak["n"]
        live.release()
        gate.wait(10)
        with lock:
            peak["n"] -= 1
        return current

    for _ in range(scheduler._MAX_WORKERS + 10):
        scheduler.submit(job, sid="s1")

    for _ in range(scheduler._MAX_WORKERS):
        assert live.acquire(timeout=5)
    time.sleep(0.2)          # give any unbounded extra a chance to appear
    with lock:
        assert peak["n"] <= scheduler._MAX_WORKERS, (
            f"{peak['n']} turns ran concurrently with a cap of "
            f"{scheduler._MAX_WORKERS}"
        )
    gate.set()


# ── Backpressure ──────────────────────────────────────────────────────────────

def test_a_full_backlog_is_rejected_not_dropped(monkeypatch):
    """REJECTED is a promise to the caller that it must tell the user.

    Silently discarding the turn would leave the browser spinning forever, which
    is strictly worse than an explicit failure.
    """
    gate = threading.Event()
    try:
        _saturate(gate)
        # Fill the queue to its limit, then one more.
        accepted = 0
        for _ in range(scheduler._MAX_QUEUE):
            if scheduler.submit(gate.wait, 10, sid="filler") == scheduler.QUEUED:
                accepted += 1
        assert accepted >= 1, "premise broken — nothing queued"
        assert scheduler.submit(gate.wait, 10, sid="overflow") == scheduler.REJECTED
        assert scheduler.stats()["rejected"] >= 1
    finally:
        gate.set()


def test_a_rejected_turn_is_not_left_in_the_pending_list():
    """A rejected job must not count against the queue depth forever."""
    gate = threading.Event()
    try:
        _saturate(gate)
        for _ in range(scheduler._MAX_QUEUE):
            scheduler.submit(gate.wait, 10, sid="filler")
        before = scheduler.stats()["queued"]
        assert scheduler.submit(gate.wait, 10, sid="ghost") == scheduler.REJECTED
        assert scheduler.stats()["queued"] == before, (
            "a rejected job stayed in the pending list and inflated queue depth"
        )
    finally:
        gate.set()


def test_queue_depth_is_observable():
    gate = threading.Event()
    try:
        _saturate(gate)
        scheduler.submit(gate.wait, 10, sid="q1")
        scheduler.submit(gate.wait, 10, sid="q2")
        s = scheduler.stats()
        assert s["queued"] >= 2, s
        assert s["running"] == scheduler._MAX_WORKERS, s
        assert s["high_water_queue"] >= 2, s
    finally:
        gate.set()


# ── Cancellation of QUEUED work ───────────────────────────────────────────────

def test_stop_reaches_a_queued_turn():
    """The failure mode a queue introduces.

    `sessions.open()` is called INSIDE the agent loop, so a queued turn owns no
    cancel token and `sessions.cancel()` cannot see it. Before this, Stop on a
    queued turn did nothing and the agent started talking once a worker freed up.
    """
    gate = threading.Event()
    ran = threading.Event()
    try:
        _saturate(gate)
        assert scheduler.submit(ran.set, sid="victim", chat_id="c9") == scheduler.QUEUED
        assert scheduler.cancel("victim", "c9") == 1
    finally:
        gate.set()

    time.sleep(0.5)          # let the workers drain
    assert not ran.is_set(), "a cancelled turn ran anyway"
    assert scheduler.stats()["cancelled"] >= 1


def test_cancel_is_scoped_to_the_socket():
    """One tab pressing Stop must not cancel another tab's queued turn."""
    gate = threading.Event()
    mine = threading.Event()
    theirs = threading.Event()
    try:
        _saturate(gate)
        scheduler.submit(mine.set, sid="tab-a", chat_id="c1")
        scheduler.submit(theirs.set, sid="tab-b", chat_id="c1")
        assert scheduler.cancel("tab-a") == 1
    finally:
        gate.set()

    assert theirs.wait(5), "the wrong socket's turn was cancelled"
    assert not mine.is_set()


def test_cancel_is_scoped_to_the_chat_when_given_one():
    gate = threading.Event()
    c1 = threading.Event()
    c2 = threading.Event()
    try:
        _saturate(gate)
        scheduler.submit(c1.set, sid="s1", chat_id="chat-1")
        scheduler.submit(c2.set, sid="s1", chat_id="chat-2")
        assert scheduler.cancel("s1", "chat-1") == 1
    finally:
        gate.set()

    assert c2.wait(5), "an unrelated chat's turn was cancelled"
    assert not c1.is_set()


def test_cancelling_an_unknown_socket_is_a_noop():
    assert scheduler.cancel("nobody") == 0
    assert scheduler.cancel("nobody", "nochat") == 0


def test_a_running_turn_is_not_reported_as_cancelled():
    """`scheduler.cancel` only claims queued work; a running turn belongs to
    `sessions.cancel`. Over-reporting here would hide a broken Stop button."""
    gate = threading.Event()
    try:
        _saturate(gate, n=1)
        assert scheduler.cancel("saturate") == 0, (
            "a turn already running was counted as cancelled"
        )
    finally:
        gate.set()


# ── Failsafe ──────────────────────────────────────────────────────────────────

def test_disabling_the_pool_reports_disabled(monkeypatch):
    """AGENT2_MAX_CONCURRENT_TURNS=0 must hand the work back to the caller.

    DISABLED is a contract: the socket handler runs the turn on its own thread,
    exactly as it did before this module existed. Anything else would mean
    turning the pool off silently stops answering messages.
    """
    monkeypatch.setattr(scheduler, "_MAX_WORKERS", 0)
    ran = threading.Event()
    assert scheduler.submit(ran.set, sid="s1") == scheduler.DISABLED
    time.sleep(0.2)
    assert not ran.is_set(), "a disabled pool ran the job anyway"


def test_thread_exhaustion_degrades_to_disabled(monkeypatch):
    """If workers cannot start, the caller must be told to run it itself."""
    scheduler.shutdown()

    def no_threads(*_a, **_k):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(scheduler.threading, "Thread", no_threads)
    assert scheduler.submit(lambda: None, sid="s1") == scheduler.DISABLED


def test_a_crashing_turn_does_not_kill_its_worker():
    """A lost worker is capacity that never returns — the pool would bleed down
    to zero and every turn would queue forever."""
    # The fixture shuts the pool down, so measure AFTER it is up — otherwise the
    # comparison below is 0 == 0 and would hold even with every worker dead.
    assert scheduler.start() is True
    before = scheduler.stats()["workers"]
    assert before >= 1, "premise broken — no workers running"

    for _ in range(5):
        scheduler.submit(lambda: (_ for _ in ()).throw(RuntimeError("boom")),
                         sid="s1")

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and scheduler.stats()["failed"] < 5:
        time.sleep(0.02)

    assert scheduler.stats()["failed"] == 5
    assert scheduler.stats()["workers"] == before, (
        "a crashing turn permanently cost the pool a worker"
    )
    # And the pool still works.
    ok = threading.Event()
    scheduler.submit(ok.set, sid="s1")
    assert ok.wait(5), "the pool stopped accepting work after failures"


def test_a_failing_job_is_counted_not_swallowed():
    """A swallowed error with no counter is indistinguishable from success."""
    scheduler.submit(lambda: (_ for _ in ()).throw(ValueError("x")), sid="s1")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and scheduler.stats()["failed"] < 1:
        time.sleep(0.02)
    assert scheduler.stats()["failed"] == 1


def test_submit_never_blocks_the_caller():
    """submit() runs on the Socket.IO handler thread. Blocking there stalls every
    other client, turning a busy queue into a dead server."""
    gate = threading.Event()
    try:
        _saturate(gate)
        for _ in range(scheduler._MAX_QUEUE + 5):
            start = time.monotonic()
            scheduler.submit(gate.wait, 10, sid="filler")
            assert time.monotonic() - start < 1.0, "submit() blocked"
    finally:
        gate.set()


def test_workers_are_daemons():
    """They must never hold the process open at exit."""
    scheduler.submit(lambda: None, sid="s1")
    alive = [t for t in scheduler._workers if t.is_alive()]
    assert alive, "premise broken — no workers"
    for t in alive:
        assert t.daemon is True


def test_start_is_idempotent():
    assert scheduler.start() is True
    first = list(scheduler._workers)
    assert scheduler.start() is True
    assert len(scheduler._workers) == len(first), "start() spawned a second pool"


def test_stats_are_reportable():
    scheduler.start()
    s = scheduler.stats()
    for key in ("submitted", "completed", "failed", "rejected", "cancelled",
                "high_water_queue", "worker_starts", "workers", "max_workers",
                "max_queue", "queued", "running", "enabled"):
        assert key in s, f"stats() is missing {key!r}"


def test_shutdown_stops_the_workers():
    """`stats()` is not enough here, so this asserts on the threads themselves.

    `shutdown()` clears `_workers`, which makes `stats()["workers"]` read 0
    whether or not the threads actually stopped. Holding real references first is
    what distinguishes "the pool forgot its workers" from "the workers exited" —
    leaked non-daemon-like threads would otherwise pile up invisibly across a
    long-running process that restarts the pool.
    """
    scheduler.start()
    threads = [t for t in scheduler._workers if t.is_alive()]
    assert threads, "premise broken — no workers to stop"

    scheduler.shutdown()
    assert scheduler.stats()["workers"] == 0

    for t in threads:
        # shutdown() joins with a TIMEOUT, so it promises "asked them to stop and
        # waited", not "they have provably exited". Asserting is_alive() the
        # instant it returns asserts the stronger guarantee, and on a loaded
        # machine a worker can miss that window — this test failed exactly once
        # during a run where the whole suite took 6x its normal wall-clock.
        # Joining again here keeps what the test actually guards (the threads
        # END, rather than the pool merely forgetting them) without depending on
        # scheduling luck.
        t.join(5)
        assert not t.is_alive(), (
            "a worker thread was still running after shutdown() returned — the "
            "pool only forgot about it"
        )

    # And it comes back on demand.
    ran = threading.Event()
    scheduler.submit(ran.set, sid="s1")
    assert ran.wait(5), "the pool did not restart after shutdown"


def test_shutdown_stops_the_workers_even_when_the_queue_is_full():
    """The state a pool is actually shut down in.

    `shutdown()` used to post its stop sentinels with `put_nowait` and swallow
    `queue.Full`, so a saturated pool — the only kind anybody shuts down — was
    told nothing. The workers stayed blocked in `_q.get()` forever, the join
    timed out, and `_workers.clear()` then made them unreachable: they could
    never be signalled again and they raced the next pool for its sentinels.
    That is how eight tracked workers became sixteen live ones.
    """
    gate = threading.Event()
    try:
        _saturate(gate)
        for _ in range(scheduler._MAX_QUEUE):
            scheduler.submit(gate.wait, 10, sid="filler")
        assert scheduler.submit(gate.wait, 10, sid="overflow") == scheduler.REJECTED, (
            "premise broken — the queue is not full"
        )
        threads = [t for t in scheduler._workers if t.is_alive()]
        assert threads, "premise broken — no workers to stop"
    finally:
        gate.set()

    assert scheduler.shutdown() is True, (
        "shutdown() reported workers it could not stop"
    )
    for t in threads:
        t.join(5)
        assert not t.is_alive(), (
            "a worker survived a shutdown issued against a full queue — its "
            "sentinel was dropped"
        )
    assert not [t for t in scheduler._workers if t.is_alive()]


def test_a_busy_worker_stays_tracked_after_a_timed_out_shutdown(monkeypatch):
    """A live worker the pool has forgotten is unstoppable, and it double-books.

    `shutdown()` cleared `_workers` whether or not the join succeeded, so a worker
    still running its turn vanished from the pool's own view: `stats()` read zero,
    nothing could ever signal it again, and the next `start()` spawned a full
    complement *beside* it — two pools draining one queue, with the concurrency
    ceiling this module exists to enforce quietly doubled.
    """
    monkeypatch.setattr(scheduler, "_MAX_WORKERS", 1)
    gate = threading.Event()
    worker = []
    try:
        _saturate(gate, n=1)
        worker = [t for t in scheduler._workers if t.is_alive()]
        assert len(worker) == 1, "premise broken — expected a single worker"

        assert scheduler.shutdown(timeout=0.2) is False, (
            "shutdown() claimed success while a worker was still running a turn"
        )
        assert scheduler.stats()["workers"] == 1, (
            "the pool forgot a worker that is still running"
        )
        assert scheduler.start() is True
        assert len([t for t in scheduler._workers if t.is_alive()]) == 1, (
            "start() spawned a second worker beside one the pool had forgotten"
        )
    finally:
        gate.set()

    # The stop order it was given still applies once its turn returns.
    worker[0].join(5)
    assert not worker[0].is_alive(), (
        "a busy worker never acted on the sentinel shutdown() left for it"
    )


def test_shutdown_does_not_hand_queued_work_to_the_next_pool():
    """`_pending` and `_q` must describe the same queue.

    `shutdown()` cleared `_pending` and left the jobs themselves in `_q`, so
    `stats()["queued"]` read 0 while real turns were still waiting — and the next
    pool ran them, seconds after their callers had been told they were gone.
    """
    gate = threading.Event()
    ran = threading.Event()
    try:
        _saturate(gate)                      # every worker is busy on the gate
        assert scheduler.submit(ran.set, sid="late") == scheduler.QUEUED
        assert scheduler.stats()["queued"] >= 1, "premise broken — nothing queued"

        scheduler.shutdown(timeout=0.2)
        assert scheduler.stats()["queued"] == 0, (
            "shutdown() cleared the pending list but left the job in the queue"
        )
        assert scheduler.stats()["cancelled"] >= 1, (
            "the discarded turn was not accounted for anywhere"
        )
    finally:
        gate.set()

    assert scheduler.start() is True
    time.sleep(0.3)
    assert not ran.is_set(), (
        "a turn shutdown() reported as discarded ran on the restarted pool"
    )


def test_a_stale_sentinel_cannot_outlive_a_shutdown(monkeypatch):
    """A leftover `None` is a stop order aimed at whoever spawns next.

    Draining the queue before signalling is what removes it. Without that, the
    first worker of a fresh pool takes the previous pool's sentinel and exits
    immediately — while `start()` reports a full complement, so the turn behind it
    waits on a pool with nobody in it.
    """
    monkeypatch.setattr(scheduler, "_MAX_WORKERS", 1)
    scheduler._q.put_nowait(None)
    assert scheduler.shutdown() is True

    assert scheduler.start() is True
    ran = threading.Event()
    assert scheduler.submit(ran.set, sid="s1") == scheduler.QUEUED
    assert ran.wait(5), "a stale sentinel killed the restarted pool's only worker"
