# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
Tests for background WAL checkpointing (agent2.database).

Run from the repo root:  python -m pytest .github/tests/test_wal_checkpoint.py -v

Why this exists
───────────────
WAL mode trades one fsync per commit for one per checkpoint, at the cost of a
`-wal` sidecar. SQLite folds that back automatically every 1000 pages, but that
pass is PASSIVE: it yields to any reader mid-transaction and never shrinks a file
it already grew. Agent2 keeps overlapping readers on one DB more or less
permanently (web thread + CLI process + a PIL read per keystroke), so the `-wal`
can stay large long after the writes in it were applied.

A daemon thread now runs `wal_checkpoint(TRUNCATE)` during a lull. The tests below
pin the two things that make that safe rather than merely fast:

  • It is an optimization layered over a working default. `wal_autocheckpoint` is
    left enabled, and nothing on the query path depends on the thread existing.
  • It never raises into a caller and never dies on a bad tick.
"""

import os
import sqlite3
import threading
import time

import pytest

from agent2 import database as db


@pytest.fixture
def wal_db(tmp_path, monkeypatch):
    """A real WAL-mode DB on disk, with no checkpointer thread running.

    The thread is stopped for the duration: these tests drive `_maybe_checkpoint`
    directly so the decisions are deterministic, rather than sleeping and hoping
    a tick landed.
    """
    path = tmp_path / "wal.db"
    monkeypatch.setattr(db, "DB", path)
    db.close_all()
    db._init_done.clear()
    db.init_db()
    db.stop_wal_checkpointer()
    for k in db._ckpt_stats:
        db._ckpt_stats[k] = 0
    yield path
    db.close_all()
    db._init_done.clear()


def _bloat_wal(target: int = 3 * 1024 * 1024) -> None:
    """Write until the -wal exceeds *target* bytes.

    An open read transaction is held throughout so SQLite's own auto-checkpoint
    cannot reclaim the file behind us — which is exactly the production condition
    this feature addresses.
    """
    blocker = sqlite3.connect(str(db.DB), timeout=5.0)
    blocker.execute("BEGIN")
    blocker.execute("SELECT count(*) FROM chats").fetchone()
    try:
        blob = "x" * 4000
        for i in range(2000):
            db.exe("INSERT INTO memories(id, content) VALUES(?,?)",
                   (f"m{i}", blob))
            if i % 100 == 0 and db.wal_size() > target:
                break
    finally:
        blocker.close()


# ── The mechanics ─────────────────────────────────────────────────────────────

def test_wal_size_reports_the_sidecar(wal_db):
    assert db.wal_size() >= 0
    db.exe("INSERT INTO memories(id, content) VALUES('a','b')")
    assert os.path.exists(str(wal_db) + "-wal"), "WAL mode did not engage"


def test_checkpoint_truncates_a_bloated_wal(wal_db):
    _bloat_wal()
    before = db.wal_size()
    assert before > db._WAL_MIN_BYTES, f"setup failed to grow the WAL ({before}B)"

    assert db.checkpoint_wal("TRUNCATE") == "truncated"
    after = db.wal_size()
    assert after < before, f"WAL not reclaimed: {before}B → {after}B"


def test_data_survives_a_checkpoint(wal_db):
    """A checkpoint moves pages into the main DB. Losing one would lose a message."""
    db.exe("INSERT INTO memories(id, content) VALUES('keep','important')")
    db.checkpoint_wal("TRUNCATE")
    row = db.qone("SELECT content FROM memories WHERE id='keep'")
    assert row is not None and row["content"] == "important"


def test_checkpoint_never_raises_on_a_broken_db(tmp_path, monkeypatch):
    """Housekeeping must not throw; there is nothing a caller could do with it."""
    monkeypatch.setattr(db, "DB", tmp_path / "nope" / "missing.db")
    assert db.checkpoint_wal("TRUNCATE") == "error"


def test_an_unknown_mode_falls_back_instead_of_injecting(wal_db):
    """The mode is interpolated into the PRAGMA, so it is whitelisted."""
    assert db.checkpoint_wal("TRUNCATE); DROP TABLE chats; --") == "truncated"
    assert db.qone("SELECT 1 AS x FROM sqlite_master WHERE name='chats'") is not None


# ── The tick decisions ────────────────────────────────────────────────────────

def test_a_small_wal_is_left_alone(wal_db):
    """Below the threshold the -wal is just doing its job as a write buffer;
    truncating only forces the next writes to grow it again."""
    db.exe("INSERT INTO memories(id, content) VALUES('s','small')")
    assert db._maybe_checkpoint() == "small"
    assert db._ckpt_stats["runs"] == 0


def test_a_checkpoint_waits_for_writes_to_pause(wal_db):
    """A checkpoint contends with writers, so an active writer defers it."""
    _bloat_wal()
    db._touch_write()                      # pretend a write just landed
    assert db._maybe_checkpoint() == "writing"
    assert db._ckpt_stats["runs"] == 0, "checkpointed while writes were in flight"


def test_a_bloated_idle_wal_is_checkpointed(wal_db, monkeypatch):
    _bloat_wal()
    monkeypatch.setattr(db, "_last_write", time.monotonic() - 60)
    assert db._maybe_checkpoint() == "truncated"
    assert db._ckpt_stats["runs"] == 1
    assert db._ckpt_stats["truncated"] == 1


def test_a_busy_checkpoint_falls_back_to_passive(wal_db, monkeypatch):
    """TRUNCATE needs the whole WAL free. A reader holding it is normal, not an
    error — PASSIVE still folds back what it can, so the file stops growing."""
    _bloat_wal()
    monkeypatch.setattr(db, "_last_write", time.monotonic() - 60)

    calls = []

    def fake(mode="TRUNCATE"):
        calls.append(mode)
        return "busy" if mode == "TRUNCATE" else "truncated"

    monkeypatch.setattr(db, "checkpoint_wal", fake)
    assert db._maybe_checkpoint() == "busy"
    assert calls == ["TRUNCATE", "PASSIVE"], f"no passive fallback: {calls}"
    assert db._ckpt_stats["busy"] == 1


def test_writes_update_the_idle_marker(wal_db):
    db._last_write = 0.0
    db.exe("INSERT INTO memories(id, content) VALUES('t','touch')")
    assert db._last_write > 0.0, "exe() did not mark a write"

    db._last_write = 0.0
    db.exemany("INSERT INTO memories(id, content) VALUES(?,?)",
               [("t1", "a"), ("t2", "b")])
    assert db._last_write > 0.0, "exemany() did not mark a write"

    db._last_write = 0.0
    with db.batch():
        db.exe("INSERT INTO memories(id, content) VALUES('t3','c')")
    assert db._last_write > 0.0, "batch() did not mark a write"


# ── Thread lifecycle and failsafe ─────────────────────────────────────────────

def test_starting_is_idempotent(wal_db):
    try:
        assert db.start_wal_checkpointer() is True
        first = db._ckpt_thread
        assert db.start_wal_checkpointer() is True
        assert db._ckpt_thread is first, "a second thread was started"
        assert db.wal_stats()["running"] is True
    finally:
        db.stop_wal_checkpointer()
    assert db.wal_stats()["running"] is False


def test_the_thread_is_a_daemon(wal_db):
    """It must never hold the process open at exit."""
    try:
        db.start_wal_checkpointer()
        assert db._ckpt_thread.daemon is True
    finally:
        db.stop_wal_checkpointer()


def test_it_can_be_disabled(wal_db, monkeypatch):
    """Opting out returns the DB to plain SQLite auto-checkpointing."""
    monkeypatch.setattr(db, "_WAL_INTERVAL", 0)
    assert db.start_wal_checkpointer() is False
    assert db._ckpt_thread is None


def test_sqlite_autocheckpoint_is_left_enabled(wal_db):
    """THE failsafe. This thread is an optimization over a working default, not a
    replacement for one: if it is disabled, never starts, or dies, SQLite must
    still checkpoint on its own exactly as it did before this existed.

    Read through `qone` on purpose. `wal_autocheckpoint` is a PER-CONNECTION
    setting, not a property of the file (unlike journal_mode), so asking a raw
    sqlite3.connect() would report the stock default no matter what `_configure`
    does — it would pass even with autocheckpoint switched off everywhere the app
    actually writes. This asks a connection the app itself would use.
    """
    row = db.qone("PRAGMA wal_autocheckpoint")
    assert row, "PRAGMA returned no row"
    pages = next(iter(row.values()))
    assert pages > 0, (
        "wal_autocheckpoint was disabled — the background thread is now the ONLY "
        "thing checkpointing, so if it stops the WAL grows without bound"
    )


def test_a_failing_tick_does_not_kill_the_thread(wal_db, monkeypatch):
    """One bad tick must not silently return the process to unbounded WAL growth."""
    ticks = threading.Semaphore(0)
    seen = {"n": 0}

    def exploding():
        seen["n"] += 1
        ticks.release()
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(db, "_maybe_checkpoint", exploding)
    monkeypatch.setattr(db, "_WAL_INTERVAL", 0.01)
    try:
        db.start_wal_checkpointer()
        assert ticks.acquire(timeout=5), "the checkpointer never ticked"
        assert ticks.acquire(timeout=5), "the thread died on its first failure"
        assert db._ckpt_thread.is_alive()
        assert db._ckpt_stats["errors"] >= 2
    finally:
        db.stop_wal_checkpointer()


def test_thread_exhaustion_degrades_instead_of_failing_startup(wal_db, monkeypatch):
    """Housekeeping must never be the reason the app will not start."""
    def no_threads(*_a, **_k):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(db.threading, "Thread", no_threads)
    assert db.start_wal_checkpointer() is False
    # And the DB layer is still fully usable.
    db.exe("INSERT INTO memories(id, content) VALUES('after','still works')")
    assert db.qone("SELECT content FROM memories WHERE id='after'") is not None


def test_init_db_starts_the_checkpointer(tmp_path, monkeypatch):
    """All three modes call init_db(), so that is where it is wired in — per
    entry point would be three chances to miss one."""
    monkeypatch.setattr(db, "DB", tmp_path / "started.db")
    db.close_all()
    db._init_done.clear()
    try:
        db.init_db()
        assert db.wal_stats()["running"] is True
    finally:
        db.close_all()
        db._init_done.clear()


def test_close_all_stops_the_checkpointer(tmp_path, monkeypatch):
    """It resolves DB at tick time, so it must not outlive the DB it was started
    for — otherwise it checkpoints a file the caller thinks it is done with."""
    monkeypatch.setattr(db, "DB", tmp_path / "stopme.db")
    db._init_done.clear()
    db.init_db()
    assert db.wal_stats()["running"] is True
    db.close_all()
    assert db.wal_stats()["running"] is False
    db._init_done.clear()


def test_stats_are_reportable(wal_db):
    s = db.wal_stats()
    for key in ("wal_bytes", "interval_sec", "min_bytes", "running",
                "runs", "truncated", "busy", "errors", "skipped"):
        assert key in s, f"wal_stats() is missing {key!r}"
