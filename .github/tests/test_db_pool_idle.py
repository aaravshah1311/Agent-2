# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
Tests for pool idle eviction and instrumentation (agent2.database).

Run from the repo root:  python -m pytest .github/tests/test_db_pool_idle.py -v

Why this exists
───────────────
The pool kept every connection it ever opened for the life of the process. Each
one holds a file handle and its own ~8 MB page cache (PRAGMA cache_size=-8000),
so a burst of concurrent turns would fill the pool to AGENT2_DB_POOL and hold
that memory for hours of an otherwise idle dual-mode session.

Eviction closes connections nobody has used recently. That makes it a pooling
change, which is the category the failsafe rule calls out, so the tests below
lean on the two structural reasons it is safe:

  • A checked-out connection is NOT in the pool, so eviction cannot reach one a
    caller is holding.
  • Closing an idle connection is never fatal — the next `_take()` just opens a
    new one. The worst case is the cost of a reconnect.

`test_db_pool_failsafe.py` covers the durability guarantees pooling must not
weaken; this file covers only what eviction and the counters add.
"""

import threading
import time

import pytest

from agent2 import database as db


@pytest.fixture(autouse=True)
def _clean_pool():
    """Start from a known pool, and never leak a temp-DB connection onward."""
    db.init_db()
    db.stop_wal_checkpointer()
    db.close_all()
    for k in db._pool_stats:
        db._pool_stats[k] = 0
    yield
    db.close_all()


def _fill_pool(n: int = 3) -> None:
    """Park *n* connections in the pool by using them concurrently.

    Sequential queries would reuse the same connection and leave the pool at 1,
    so the threads are held open together until every one has checked out.
    """
    ready = threading.Barrier(n + 1, timeout=10)

    def worker():
        with db._checkout() as (c, _owned):
            c.execute("SELECT 1").fetchone()
            ready.wait()          # hold the connection until all n are out

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    ready.wait()
    for t in threads:
        t.join(10)


# ── Eviction ──────────────────────────────────────────────────────────────────

def test_idle_connections_are_evicted(_clean_pool):
    _fill_pool(3)
    assert len(db.pooled_connections()) >= 2, "setup did not park connections"

    # max_idle=0 would mean "disabled", so a tiny window is used instead.
    time.sleep(0.05)
    closed = db.evict_idle_connections(max_idle=0.01)
    assert closed >= 2
    assert db.pooled_connections() == []


def test_recently_used_connections_are_kept(_clean_pool):
    """Eviction must not undo pooling. A connection released this instant is
    exactly the one the next query wants."""
    db.qall("SELECT 1")
    before = len(db.pooled_connections())
    assert before >= 1

    assert db.evict_idle_connections(max_idle=300) == 0
    assert len(db.pooled_connections()) == before


def test_eviction_can_be_disabled(_clean_pool):
    """AGENT2_DB_POOL_IDLE=0 restores keep-everything-warm behaviour exactly."""
    db.qall("SELECT 1")
    assert db.evict_idle_connections(max_idle=0) == 0
    assert len(db.pooled_connections()) >= 1


def test_the_layer_still_works_after_eviction(_clean_pool):
    """The point of the failsafe: an evicted connection is transparently replaced."""
    db.exe("INSERT INTO memories(id, content) VALUES('ev','before')")
    time.sleep(0.05)
    db.evict_idle_connections(max_idle=0.01)
    assert db.pooled_connections() == []

    assert db.qone("SELECT content FROM memories WHERE id='ev'")["content"] == "before"
    db.exe("INSERT INTO memories(id, content) VALUES('ev2','after')")
    assert db.qone("SELECT content FROM memories WHERE id='ev2'")["content"] == "after"
    db.exe("DELETE FROM memories WHERE id IN ('ev','ev2')")


def test_eviction_cannot_close_a_checked_out_connection(_clean_pool):
    """The structural safety property. A connection in use is not in the pool,
    so the most aggressive possible eviction cannot reach it."""
    with db._checkout() as (c, _owned):
        c.execute("SELECT 1").fetchone()
        assert c not in db.pooled_connections(), "a live connection was left in the pool"
        db.evict_idle_connections(max_idle=0.0001)
        # Still usable — an evicted connection here would raise ProgrammingError.
        assert c.execute("SELECT 42").fetchone()[0] == 42


def test_eviction_during_a_batch_leaves_the_batch_alone(_clean_pool):
    """A batch owns its connection for the whole block, including its transaction."""
    _fill_pool(3)
    with db.batch():
        db.exe("INSERT INTO memories(id, content) VALUES('b_ev','x')")
        db.evict_idle_connections(max_idle=0.0001)
        assert db.qone("SELECT content FROM memories WHERE id='b_ev'")["content"] == "x"
    assert db.qone("SELECT content FROM memories WHERE id='b_ev'")["content"] == "x"
    db.exe("DELETE FROM memories WHERE id='b_ev'")


def test_eviction_is_safe_while_other_threads_query(_clean_pool):
    """Eviction takes the pool lock, so it cannot race a checkout."""
    errors: list[Exception] = []
    stop = threading.Event()

    def reader():
        try:
            while not stop.is_set():
                db.qall("SELECT 1")
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()
    try:
        for _ in range(60):
            db.evict_idle_connections(max_idle=0.0001)
    finally:
        stop.set()
        for t in threads:
            t.join(10)

    assert not errors, f"eviction broke concurrent readers: {errors}"


def test_eviction_holds_the_pool_lock_for_the_whole_pass(_clean_pool):
    """Eviction reads the pool, then writes it back. Both must be under one lock.

    Unlocked, this interleaving loses a connection to two owners at once:

        eviction: reads _pool                  -> [A]
        _take():  pops A, hands it to a caller -> pool empty, A checked out
        eviction: writes its list back         -> [A]   ← A is now BOTH

    The caller and the next borrower would then share one connection, which is
    precisely what `check_same_thread=False` is only safe without.

    Asserting on the lock's observable effect rather than on timing: a `_take()`
    started mid-pass must not be able to complete until eviction is done.
    """
    db.qall("SELECT 1")
    pooled = db.pooled_connections()
    assert pooled, "premise broken — nothing pooled"

    gate = threading.Event()
    entered = threading.Event()

    class BlockingTimestamp:
        """Stands in for a pooled entry's timestamp and stalls the comparison.

        This is the seam that makes the test deterministic: `entered` proves
        eviction is *inside* its read-modify-write, not merely about to start,
        so what the lock state says at that moment is not a matter of timing.
        """
        def __gt__(self, _other):        # the `keep` comprehension
            entered.set()
            gate.wait(5)
            return True                  # keep it — nothing gets closed
        def __le__(self, _other):        # the `stale` comprehension
            return False

    with db._pool_lock:
        db._pool[:] = [(pooled[0], BlockingTimestamp())]

    t = threading.Thread(target=db.evict_idle_connections, args=(0.01,))
    t.start()
    try:
        assert entered.wait(5), "eviction never reached the pool scan"
        free = db._pool_lock.acquire(blocking=False)
        if free:
            db._pool_lock.release()
        assert not free, (
            "the pool lock was FREE while eviction was mid-pass — it reads the "
            "pool and writes it back unsynchronized, so a concurrent _take() can "
            "pop a connection that eviction then re-publishes, leaving it both "
            "checked out and pooled"
        )
    finally:
        gate.set()
        t.join(5)


def test_a_close_failure_during_eviction_is_survivable(_clean_pool, monkeypatch):
    """Eviction is housekeeping; a stubborn close must not propagate."""
    db.qall("SELECT 1")
    assert db.pooled_connections(), "premise broken — nothing pooled to evict"

    class Stubborn:
        def close(self):
            raise OSError("cannot close")

    with db._pool_lock:
        db._pool[:] = [(Stubborn(), 0.0)]

    assert db.evict_idle_connections(max_idle=0.01) == 1
    assert db.pooled_connections() == []
    assert db.qall("SELECT 1") == [{"1": 1}]


# ── Instrumentation ───────────────────────────────────────────────────────────

def test_reuse_is_counted_separately_from_creation(_clean_pool):
    """The number that answers 'is AGENT2_DB_POOL the right size?'."""
    db.qall("SELECT 1")
    assert db._pool_stats["created"] >= 1
    created_after_first = db._pool_stats["created"]

    for _ in range(20):
        db.qall("SELECT 1")

    assert db._pool_stats["reused"] >= 20, "sequential queries should reuse"
    assert db._pool_stats["created"] == created_after_first, (
        "the pool opened new connections for queries it could have served warm"
    )


def test_discards_are_counted(_clean_pool):
    import sqlite3
    with pytest.raises(sqlite3.Error):
        db.exe("INSERT INTO nonexistent_table(x) VALUES(1)")
    assert db._pool_stats["discarded"] >= 1


def test_evictions_are_counted(_clean_pool):
    db.qall("SELECT 1")
    time.sleep(0.05)
    n = db.evict_idle_connections(max_idle=0.01)
    assert db._pool_stats["evicted"] == n


def test_high_water_tracks_the_peak(_clean_pool):
    _fill_pool(3)
    peak = db._pool_stats["high_water"]
    assert peak >= 2
    db.close_all()
    db.qall("SELECT 1")
    assert db._pool_stats["high_water"] == peak, "high water mark should not fall"


def test_stats_are_reportable(_clean_pool):
    db.qall("SELECT 1")
    s = db.pool_stats()
    for key in ("idle", "max", "idle_timeout_sec", "oldest_idle_sec",
                "created", "reused", "discarded", "evicted", "closed_full",
                "high_water"):
        assert key in s, f"pool_stats() is missing {key!r}"
    assert s["idle"] == len(db.pooled_connections())
    assert s["max"] == db._POOL_MAX


def test_stats_never_raise_on_an_empty_pool(_clean_pool):
    db.close_all()
    s = db.pool_stats()
    assert s["idle"] == 0
    assert s["oldest_idle_sec"] == 0.0


def test_the_pool_ceiling_still_holds(_clean_pool):
    """Instrumentation must not change the bound it reports on."""
    _fill_pool(db._POOL_MAX + 4)
    assert len(db.pooled_connections()) <= db._POOL_MAX
    assert db._pool_stats["closed_full"] >= 1, (
        "connections over the ceiling should be closed, not pooled"
    )


# ── Wiring ────────────────────────────────────────────────────────────────────

def test_the_checkpoint_tick_also_evicts(_clean_pool, monkeypatch):
    """Eviction rides the existing housekeeping thread rather than adding one."""
    called = threading.Semaphore(0)

    def spy(*_a, **_k):
        called.release()
        return 0

    monkeypatch.setattr(db, "evict_idle_connections", spy)
    monkeypatch.setattr(db, "_WAL_INTERVAL", 0.01)
    monkeypatch.setattr(db, "_maybe_checkpoint", lambda: "small")
    try:
        db.start_wal_checkpointer()
        assert called.acquire(timeout=5), "the housekeeping tick never evicted"
    finally:
        db.stop_wal_checkpointer()


def test_a_failing_eviction_does_not_kill_the_housekeeping_thread(_clean_pool,
                                                                 monkeypatch):
    """A bad eviction must not take WAL checkpointing down with it."""
    ticks = threading.Semaphore(0)

    def exploding(*_a, **_k):
        ticks.release()
        raise RuntimeError("nope")

    monkeypatch.setattr(db, "evict_idle_connections", exploding)
    monkeypatch.setattr(db, "_WAL_INTERVAL", 0.01)
    monkeypatch.setattr(db, "_maybe_checkpoint", lambda: "small")
    try:
        db.start_wal_checkpointer()
        assert ticks.acquire(timeout=5)
        assert ticks.acquire(timeout=5), "the thread died on an eviction failure"
        assert db._ckpt_thread.is_alive()
    finally:
        db.stop_wal_checkpointer()
