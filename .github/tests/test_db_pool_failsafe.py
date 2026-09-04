# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
Failsafe tests for the pooled SQLite layer (agent2.database).

Connection pooling is a performance change, so these tests exist to prove it did
NOT weaken the durability/isolation guarantees the open-per-query code had:

  * a statement that raises must not leave an open transaction in the pool
  * a failed write must not be visible to the next caller (no partial flush)
  * a batch() that raises must roll back completely
  * concurrent readers/writers must not corrupt each other or the pool
"""

import sqlite3
import threading

import pytest

from agent2 import database as db


@pytest.fixture(autouse=True)
def _schema():
    db.init_db()
    db.exe("DELETE FROM memories")
    yield


def test_pool_reuses_connections():
    """Repeated queries should not grow the pool without bound."""
    for _ in range(50):
        db.qall("SELECT 1")
    assert len(db.pooled_connections()) <= db._POOL_MAX


def test_failed_write_leaves_no_open_transaction():
    """A raising write must not park an open transaction in the pool.

    This is the regression that pooling could introduce: exe() calls execute()
    then commit(). If execute() raises, sqlite3 has already begun a transaction
    implicitly and commit() is skipped.
    """
    with pytest.raises(sqlite3.Error):
        db.exe("INSERT INTO memories(id, content) VALUES(?, ?)", ("dup",))  # bad param count

    # The connection that raised is discarded, so the pool may be empty here —
    # in which case the loop below would iterate zero times and prove nothing.
    # A successful query repopulates it, making the assertion real.
    db.qall("SELECT 1")
    pooled = db.pooled_connections()
    assert pooled, "premise broken — nothing in the pool to check"
    for c in pooled:
        assert not c.in_transaction, "pooled connection still inside a transaction"


def test_failed_write_is_not_visible_later():
    """The partial work of a failed statement must never become durable."""
    db.exe("INSERT INTO memories(id, content) VALUES(?,?)", ("m1", "first"))

    # Violate the primary key — the statement fails after a transaction opened.
    with pytest.raises(sqlite3.Error):
        db.exe("INSERT INTO memories(id, content) VALUES(?,?)", ("m1", "second"))

    rows = db.qall("SELECT content FROM memories WHERE id='m1'")
    assert [r["content"] for r in rows] == ["first"]

    # A later unrelated write must commit only itself.
    db.exe("INSERT INTO memories(id, content) VALUES(?,?)", ("m2", "third"))
    assert db.qone("SELECT COUNT(*) AS n FROM memories")["n"] == 2


def test_batch_rolls_back_fully_on_error():
    with pytest.raises(RuntimeError):
        with db.batch():
            db.exe("INSERT INTO memories(id, content) VALUES(?,?)", ("b1", "x"))
            db.exe("INSERT INTO memories(id, content) VALUES(?,?)", ("b2", "y"))
            raise RuntimeError("boom")

    assert db.qone("SELECT COUNT(*) AS n FROM memories")["n"] == 0
    pooled = db.pooled_connections()
    assert pooled, "premise broken — nothing in the pool to check"
    for c in pooled:
        assert not c.in_transaction


def test_batch_commits_once_on_success():
    with db.batch():
        for i in range(25):
            db.exe("INSERT INTO memories(id, content) VALUES(?,?)", (f"c{i}", str(i)))
    assert db.qone("SELECT COUNT(*) AS n FROM memories")["n"] == 25


def test_batch_reads_own_uncommitted_writes():
    with db.batch():
        db.exe("INSERT INTO memories(id, content) VALUES(?,?)", ("z1", "inside"))
        assert db.qone("SELECT content FROM memories WHERE id='z1'")["content"] == "inside"


def test_exemany_is_atomic_and_counts():
    rows = [(f"e{i}", str(i)) for i in range(100)]
    n = db.exemany("INSERT INTO memories(id, content) VALUES(?,?)", rows)
    assert n == 100
    assert db.qone("SELECT COUNT(*) AS n FROM memories")["n"] == 100

    # A conflicting bulk write must leave the table untouched.
    with pytest.raises(sqlite3.Error):
        db.exemany("INSERT INTO memories(id, content) VALUES(?,?)",
                   [("new1", "a"), ("e5", "dup"), ("new2", "b")])
    assert db.qone("SELECT COUNT(*) AS n FROM memories")["n"] == 100


def test_exemany_empty_is_noop():
    assert db.exemany("INSERT INTO memories(id, content) VALUES(?,?)", []) == 0


def test_concurrent_writers_all_land():
    """WAL + the process write lock should let many threads write without loss."""
    errors: list[Exception] = []

    def worker(base: int):
        try:
            for i in range(20):
                db.exe("INSERT INTO memories(id, content) VALUES(?,?)",
                       (f"t{base}_{i}", "x"))
        except Exception as exc:      # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent writes raised: {errors}"
    assert db.qone("SELECT COUNT(*) AS n FROM memories")["n"] == 160
    assert len(db.pooled_connections()) <= db._POOL_MAX


def test_concurrent_readers_and_writers():
    db.exemany("INSERT INTO memories(id, content) VALUES(?,?)",
               [(f"r{i}", str(i)) for i in range(50)])
    errors: list[Exception] = []
    stop = threading.Event()

    def reader():
        try:
            while not stop.is_set():
                db.qall("SELECT * FROM memories LIMIT 10")
        except Exception as exc:      # pragma: no cover
            errors.append(exc)

    readers = [threading.Thread(target=reader) for _ in range(4)]
    for t in readers:
        t.start()
    try:
        for i in range(40):
            db.exe("INSERT INTO memories(id, content) VALUES(?,?)", (f"w{i}", "w"))
    finally:
        stop.set()
        for t in readers:
            t.join()

    assert not errors, f"reads failed during writes: {errors}"


def test_failed_statements_do_not_exhaust_the_pool():
    """Many consecutive failures must not leak connections or wedge the pool."""
    for _ in range(60):
        with pytest.raises(sqlite3.Error):
            db.exe("INSERT INTO nonexistent_table(x) VALUES(1)")
    # The layer is still fully usable afterwards.
    db.exe("INSERT INTO memories(id, content) VALUES(?,?)", ("after", "ok"))
    assert db.qone("SELECT content FROM memories WHERE id='after'")["content"] == "ok"
    assert len(db.pooled_connections()) <= db._POOL_MAX


def test_a_connection_that_raised_is_discarded_not_pooled():
    """Pins `_checkout`'s discard directly, at the mechanism.

    The durability tests above cannot pin it alone, because `_release` ALSO rolls
    back before pooling — so swapping the discard for a release keeps the
    user-visible behaviour correct and every durability test green. The two
    guards are deliberately redundant, which is exactly why each needs its own
    test: otherwise a refactor can delete either one and see nothing fail.
    """
    captured = []
    with pytest.raises(RuntimeError):
        with db._checkout() as (c, _owned):
            captured.append(c)
            c.execute("INSERT INTO memories(id, content) VALUES('disc','x')")
            raise RuntimeError("boom")

    assert captured, "premise broken — no connection was checked out"
    assert captured[0] not in db.pooled_connections(), (
        "a connection that raised was returned to the pool"
    )
    # And it really was closed, not merely withheld.
    with pytest.raises(sqlite3.ProgrammingError):
        captured[0].execute("SELECT 1")
    # The write it was mid-way through must not have survived.
    assert db.qone("SELECT 1 AS x FROM memories WHERE id='disc'") is None


def test_release_rolls_back_before_pooling():
    """Pins `_release`'s rollback directly, for the same reason.

    A pooled connection must always be handed over clean. If one is parked with a
    transaction still open, the next borrower's unrelated `commit()` flushes this
    caller's partial write.
    """
    c = db._take()
    c.execute("BEGIN")
    c.execute("INSERT INTO memories(id, content) VALUES('dirty','uncommitted')")
    assert c.in_transaction, "premise broken — no open transaction to roll back"

    db._release(c)

    for p in db.pooled_connections():
        assert not p.in_transaction, "a connection was pooled mid-transaction"
    assert db.qone("SELECT 1 AS x FROM memories WHERE id='dirty'") is None, (
        "an uncommitted write became visible"
    )


def test_wal_and_pragmas_active():
    c = db._new_conn()
    try:
        assert c.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert c.execute("PRAGMA busy_timeout").fetchone()[0] == 10000
    finally:
        c.close()


def test_close_all_releases_and_layer_recovers():
    db.qall("SELECT 1")
    db.close_all()
    assert db.pooled_connections() == []
    # Next use transparently reopens.
    assert db.qall("SELECT 1") == [{"1": 1}]


def test_version_counters_monotonic():
    before = db.get_version("memories")
    db.bump_version("memories", origin="test")
    assert db.get_version("memories") == before + 1
    assert "memories" in db.all_versions()
