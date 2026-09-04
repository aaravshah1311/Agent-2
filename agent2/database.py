# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/database.py
──────────────────
SQLite connection helpers, schema creation, and migrations.
All DB access goes through qall / qone / exe / exemany / batch() so the rest of
the app never touches sqlite3 directly.

POOLING + WAL — why both halves can hammer one agent2.db
────────────────────────────────────────────────────────
Agent2 keeps the CLI and the Web UI on ONE database file, with overlapping
readers permanently. Pooling plus WAL is what keeps that off "database is
locked".

  * Pooled connections (AGENT2_DB_POOL, default 8, floor 4). One caller holds a
    connection at a time, which is what makes `check_same_thread=False` safe.
  * Idle eviction (AGENT2_DB_POOL_IDLE, 300 s; 0 keeps every one warm). It rides
    the WAL-checkpoint tick instead of adding a thread. A checked-out connection
    is not in the pool, so eviction cannot reach one in use. `pool_stats()` is
    diagnostic ONLY — nothing reads it to make a decision.
  * WAL + synchronous=NORMAL — readers never block the writer.
  * busy_timeout=10000 — contention blocks inside SQLite rather than raising.
  * A process-wide write lock makes the read-modify-write helpers atomic.

⚠️ FAILSAFE: A CONNECTION THAT RAISED IS DISCARDED, NEVER POOLED.
`_release()` rolls back any open transaction first. sqlite3 opens an implicit
transaction on a failing statement; pooling that connection would let a later,
unrelated `commit()` flush the partial write from the statement that failed.
(test_db_pool_failsafe.py, test_db_pool_idle.py)

BACKGROUND WAL CHECKPOINTING
────────────────────────────
Daemon thread `a2-wal-checkpoint`, started from `init_db()` so no mode can miss
it. Runs `wal_checkpoint(TRUNCATE)` during a lull: SQLite's own auto-checkpoint
is passive and never shrinks the file. Skipped under AGENT2_WAL_MIN_BYTES or
within 2 s of a write; `busy` falls back to PASSIVE.

⚠️ `wal_autocheckpoint` STAYS ENABLED. This thread is layered OVER a working
default, so disabling it — or having this thread die — returns previous
behaviour rather than breaking anything. (test_wal_checkpoint.py)

INDEXES
───────
10 indexes, applied ONE AT A TIME so a failure costs one index rather than nine,
and ⚠️ applied AFTER `_apply_migrations()`: `idx_chats_cwd_updated` and
`idx_memories_rank` index columns that migrations add, so reordering breaks a
fresh DB. `_log_index_failure` is a deliberate graceful-degradation path — an
app with a slow query still works.

SCHEMA MIGRATIONS — a versioned ledger, not ALTERs in try/except: pass
─────────────────────────────────────────────────────────────────────
The old form could not tell an expected duplicate-column error from a locked DB
or a disk fault, so a FAILED migration looked identical to a successful one.

  * `_MIGRATIONS` — ordered (version, name, step), unique and contiguous from 1.
    SCHEMA_VERSION is DERIVED, never hand-edited.
  * Idempotent by CHECKING, not catching. `_add_column()` consults
    `PRAGMA table_info`, so re-running is a no-op *because the column is there*.
    A current DB attempts ZERO ALTERs on start.
  * `schema_migrations` records what ran. The step and its ledger row commit in
    ONE transaction, so a crash cannot half-record a migration.
  * ⚠️ A failing migration is logged and RE-RAISED. This is deliberately NOT the
    graceful-degradation path: there is no meaningful degraded mode for a wrong
    schema. (Contrast `_log_index_failure`, which is one.)
  * Identifiers are validated against `_IDENT_OK` — names cannot be bound as
    parameters, so they are checked rather than interpolated blind.
  * `ensure_column(table, col, typedef)` — public entry for a module that owns
    its own table. Returns True only if THAT call added the column.

⚠️ MIGRATION 5 (`providers.user_agent`) IS SPECIAL — DO NOT "CLEAN UP".
`init_db()` runs before `llm/providers.py` creates the `providers` table, so the
step no-ops and STILL records as applied. That is correct in itself, because the
later `CREATE TABLE` declares the column. But the ledger then reads "applied" on
a DB that genuinely lacks it, and v5 is never revisited. `init_providers_table()`
calling `ensure_column()` is the ONLY repair for that DB — do not delete it as
redundant, and do not wrap it in try/except.

(test_migrations.py — 18 tests, sabotage-verified)
"""

import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager

from agent2.config import DB
# Task 16. Credentials are stored as references, not values — see
# `agent2/core/secrets.py`. Safe to import at module scope: that module depends
# only on `config` and `core.logging`, neither of which imports this one, so there
# is no cycle to defer around.
from agent2.core import secrets as _secrets


# ── Connection pool ────────────────────────────────────────────────────────────
# Agent2 runs several surfaces against ONE agent2.db at once: in dual mode the
# Flask/Socket.IO server (a thread per turn, plus per-request handlers) and the
# CLI child process both read and write continuously, while PIL prediction fires
# on every keystroke. The old layer opened a fresh sqlite3 connection for EVERY
# qall/qone/exe and closed it again, which meant a file open + page-cache reload
# + commit fsync per query — the dominant cost on the hot path.
#
# Now connections are pooled and reused:
#   • Connections are created with check_same_thread=False but are only ever
#     handed to ONE caller at a time (checked out of the pool), so no connection
#     is ever used concurrently — that is what the flag's check is protecting.
#   • WAL journaling lets readers run concurrently with a writer, which is what
#     makes CLI + Web against one file actually work instead of throwing
#     "database is locked".
#   • busy_timeout makes any residual contention block-and-retry inside SQLite
#     rather than surfacing as an exception.
_POOL_MAX = max(4, int(os.environ.get("AGENT2_DB_POOL", "8")))

# How long a connection may sit unused before it is closed. Each idle connection
# holds a file handle and its own ~8 MB page cache (see PRAGMA cache_size), so a
# pool that filled during one burst of activity used to keep that memory for the
# life of the process. Set AGENT2_DB_POOL_IDLE=0 to keep every connection warm.
_POOL_IDLE_SEC = float(os.environ.get("AGENT2_DB_POOL_IDLE", "300"))

# (connection, idle_since). The timestamp is stamped on release, so eviction can
# tell a connection parked minutes ago from one returned during this turn.
# Checked-out connections are NOT in this list — that is what makes eviction
# inherently safe: it can only ever close a connection nobody holds.
_pool: list[tuple[sqlite3.Connection, float]] = []
_pool_lock = threading.Lock()

# Diagnostics only. Nothing reads these to make a decision, so a wrong count can
# never change behaviour — it is a reporting surface for /api/health.
_pool_stats = {"created": 0, "reused": 0, "discarded": 0, "evicted": 0,
               "closed_full": 0, "high_water": 0}

# Writes are serialized process-wide. SQLite allows only one writer at a time
# anyway; taking the lock ourselves converts lock contention (which costs a
# busy-wait inside SQLite) into a cheap in-process queue, and makes
# read-modify-write helpers atomic with respect to each other.
_write_lock = threading.RLock()

# A `batch()` binds one pooled connection to the calling thread for its whole
# duration, so nested qall/qone/exe on that thread join the same transaction.
_local = threading.local()

_init_done = threading.Event()


def _configure(c: sqlite3.Connection) -> None:
    """Apply per-connection pragmas. Cheap; runs once per pooled connection."""
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    # Block inside SQLite instead of raising on contention (dual mode).
    c.execute("PRAGMA busy_timeout=10000")
    # WAL + NORMAL: durable across crashes, one fsync per checkpoint rather than
    # one per commit. This is the single biggest write-throughput win.
    c.execute("PRAGMA synchronous=NORMAL")
    # Negative = KiB of page cache; ~8 MB keeps the hot tables resident.
    c.execute("PRAGMA cache_size=-8000")
    c.execute("PRAGMA temp_store=MEMORY")


def _new_conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(DB), timeout=10.0, check_same_thread=False)
    _configure(c)
    return c


@contextmanager
def _checkout():
    """Borrow a pooled connection for the duration of one statement.

    Inside a `batch()` this yields the batch's connection instead, so the whole
    batch stays on one transaction.

    FAILSAFE: pooling must not make an error outlive the statement that caused it.
    The pre-pool code opened and closed a connection per query, so a failure was
    always self-contained. Reusing connections reintroduces two hazards, both
    handled here:
      1. A raising statement leaves sqlite3's IMPLICIT transaction open (e.g.
         `exe()` fails inside c.execute() and never reaches c.commit()). Returning
         that connection to the pool would hand the next borrower an open write
         transaction — and a later unrelated commit() would flush the partial
         write. So a connection that raised is discarded, never pooled.
      2. A connection can be broken outright (disk error, closed underneath us).
         Same treatment: discard.
    """
    amb = getattr(_local, "conn", None)
    if amb is not None:
        yield amb, False           # (conn, owned) — batch owns commit/return
        return
    c = _take()
    try:
        yield c, True
    except BaseException:
        _discard(c)
        raise
    else:
        _release(c)


def _take() -> sqlite3.Connection:
    """Borrow a connection from the pool, opening one if the pool is empty."""
    with _pool_lock:
        entry = _pool.pop() if _pool else None
    if entry is not None:
        _pool_stats["reused"] += 1
        return entry[0]
    _pool_stats["created"] += 1
    return _new_conn()


def _discard(c: sqlite3.Connection) -> None:
    """Close a connection instead of pooling it. Used after any failure."""
    _pool_stats["discarded"] += 1
    try:
        c.rollback()
    except Exception:
        pass
    try:
        c.close()
    except Exception:
        pass


def _release(c: sqlite3.Connection) -> None:
    """Return a healthy connection to the pool, or close it if the pool is full.

    Any still-open transaction is rolled back first: a pooled connection must
    always be handed over clean. If it cannot be cleaned, it is discarded rather
    than reused.
    """
    try:
        if c.in_transaction:
            c.rollback()
    except Exception:
        _discard(c)
        return
    with _pool_lock:
        if len(_pool) < _POOL_MAX:
            _pool.append((c, time.monotonic()))
            _pool_stats["high_water"] = max(_pool_stats["high_water"], len(_pool))
            return
    _pool_stats["closed_full"] += 1
    try:
        c.close()
    except Exception:
        pass


def evict_idle_connections(max_idle: float | None = None) -> int:
    """Close pooled connections unused for longer than *max_idle*. Returns how
    many were closed.

    A pool that filled during one burst kept every connection — and its ~8 MB
    page cache — for the life of the process. Long-running dual-mode sessions are
    idle most of the time, so that memory sat unused for hours.

    FAILSAFE: this can only ever close connections sitting IN the pool, and a
    checked-out connection is not in the pool by construction, so eviction cannot
    touch one a caller is using. Closing an idle connection is never fatal in any
    case — `_take()` transparently opens a new one. Setting AGENT2_DB_POOL_IDLE=0
    disables eviction entirely, which restores the previous keep-everything-warm
    behaviour exactly.
    """
    limit = _POOL_IDLE_SEC if max_idle is None else max_idle
    if limit <= 0:
        return 0
    cutoff = time.monotonic() - limit
    with _pool_lock:
        keep = [(c, ts) for c, ts in _pool if ts > cutoff]
        stale = [c for c, ts in _pool if ts <= cutoff]
        _pool[:] = keep
    for c in stale:
        try:
            c.close()
        except Exception:
            pass
    _pool_stats["evicted"] += len(stale)
    return len(stale)


def pooled_connections() -> list[sqlite3.Connection]:
    """The connections currently idle in the pool.

    An accessor so callers (tests, diagnostics) do not depend on `_pool`'s
    internal element shape.
    """
    with _pool_lock:
        return [c for c, _ts in _pool]


def pool_stats() -> dict:
    """Pool counters, for diagnostics and the health endpoint."""
    with _pool_lock:
        idle = len(_pool)
        oldest = min((ts for _c, ts in _pool), default=None)
    return {"idle": idle,
            "max": _POOL_MAX,
            "idle_timeout_sec": _POOL_IDLE_SEC,
            "oldest_idle_sec": round(time.monotonic() - oldest, 1) if oldest else 0.0,
            **_pool_stats}


def _conn() -> sqlite3.Connection:
    """Back-compat alias — returns a fresh, unpooled connection.

    Kept because callers that expect to own and close the connection themselves
    (init_db, ad-hoc migrations) still use it.
    """
    return _new_conn()


def close_all() -> None:
    """Close every pooled connection. For shutdown and for tests that need the
    DB file released (Windows will not unlink an open file).

    Also stops the WAL checkpointer: it holds no connection between ticks, but it
    resolves `DB` at tick time, so leaving it running after a test repoints `DB`
    would let it checkpoint a file the caller believes it has finished with.
    """
    stop_wal_checkpointer()
    with _pool_lock:
        conns, _pool[:] = [c for c, _ts in _pool], []
    for c in conns:
        try:
            c.close()
        except Exception:
            pass


@contextmanager
def batch():
    """Run all qall/qone/exe on this thread inside a single transaction/connection.

    Reads see the batch's own uncommitted writes. Commits once on clean exit,
    rolls back on exception. Nested use reuses the outer batch. Best used for
    bulk writes where the per-statement commit overhead dominates.

    Holds the process write lock for its duration so a batch is atomic against
    other writers.
    """
    if getattr(_local, "conn", None) is not None:
        yield                      # already inside a batch — reuse it
        return
    c = _take()
    _write_lock.acquire()
    _local.conn = c
    ok = False
    try:
        yield
        c.commit()
        ok = True
    except BaseException:
        try:
            c.rollback()
        except Exception:
            pass
        raise
    finally:
        _local.conn = None
        _write_lock.release()
        # A batch that failed (or whose rollback failed) must not be pooled —
        # see the FAILSAFE note in _checkout().
        _release(c) if ok else _discard(c)
        if ok:
            _touch_write()


def qall(sql: str, p: tuple = ()) -> list[dict]:
    """Execute SELECT and return all rows as plain dicts."""
    with _checkout() as (c, _owned):
        return [dict(r) for r in c.execute(sql, p).fetchall()]


def qone(sql: str, p: tuple = ()) -> dict | None:
    """Execute SELECT and return the first row as a dict, or None."""
    with _checkout() as (c, _owned):
        r = c.execute(sql, p).fetchone()
        return dict(r) if r else None


def exe(sql: str, p: tuple = ()) -> None:
    """Execute a write statement (INSERT / UPDATE / DELETE). Inside a `batch()`
    the commit is deferred to the batch; otherwise it commits immediately."""
    with _write_lock, _checkout() as (c, owned):
        c.execute(sql, p)
        if owned:
            c.commit()
    _touch_write()


def exemany(sql: str, seq) -> int:
    """Execute one write statement over many parameter tuples in a single
    transaction. Use for bulk upserts (PIL learning, pre-training) — one commit
    instead of N. Returns the number of parameter sets applied."""
    rows = list(seq)
    if not rows:
        return 0
    with _write_lock, _checkout() as (c, owned):
        c.executemany(sql, rows)
        if owned:
            c.commit()
    _touch_write()
    return len(rows)


# ── WAL checkpointing ──────────────────────────────────────────────────────────
# WAL mode buys one fsync per checkpoint instead of one per commit, at the cost
# of a `-wal` sidecar that has to be folded back into the main DB periodically.
# SQLite does that itself every `wal_autocheckpoint` pages (1000, ≈4 MB), but
# that automatic pass is PASSIVE: it copies what it can, gives up the instant a
# reader is mid-transaction, and never shrinks the file it already grew.
#
# Agent2 is precisely the workload where that stalls. In dual mode the web
# thread, the CLI child process and PIL (a read per keystroke) keep overlapping
# readers on one file more or less permanently, so the `-wal` can sit at hundreds
# of MB long after the writes inside it were applied — a slow read for every
# reader that has to scan it, and disk that is never handed back.
#
# So a daemon thread runs `wal_checkpoint(TRUNCATE)` during a lull, which folds
# the WAL back AND resets the file to zero bytes.
#
# FAILSAFE: `wal_autocheckpoint` is deliberately left at its default, so SQLite's
# own passive checkpointing stays on underneath this. If the thread is disabled,
# fails to start, dies, or can never win the lock, the WAL is still checkpointed
# exactly as it was before — this is an optimization layered OVER a working
# default, never a replacement for one. Nothing here is on the query path.
_WAL_INTERVAL = float(os.environ.get("AGENT2_WAL_CHECKPOINT_SEC", "60"))
# Under this, the -wal is just doing its job as a write buffer; truncating it
# only forces the next writes to grow it again.
_WAL_MIN_BYTES = int(os.environ.get("AGENT2_WAL_MIN_BYTES", str(2 * 1024 * 1024)))
# A checkpoint contends with writers, so it waits for writes to pause first.
_WAL_IDLE_SEC = 2.0

_last_write = 0.0
_ckpt_thread: threading.Thread | None = None
_ckpt_stop = threading.Event()
_ckpt_lock = threading.Lock()
_ckpt_stats = {"runs": 0, "truncated": 0, "busy": 0, "errors": 0, "skipped": 0}


def _touch_write() -> None:
    """Note that a write just landed, so the checkpointer waits for a lull."""
    global _last_write
    _last_write = time.monotonic()


def wal_size() -> int:
    """Bytes in the `-wal` sidecar. 0 when absent or not in WAL mode."""
    try:
        return os.path.getsize(str(DB) + "-wal")
    except OSError:
        return 0


def checkpoint_wal(mode: str = "TRUNCATE") -> str:
    """Fold the WAL back into the main DB. Returns 'truncated'/'busy'/'error'.

    Runs on its OWN short-lived connection, not a pooled one. A checkpoint is
    slower than a query and contends for the write lock, so borrowing a pooled
    connection would hold a slot for the duration, and a failure would discard a
    connection the app was about to reuse.

    Never raises: this is housekeeping, and there is nothing useful for a caller
    to do about a failed checkpoint beyond letting the next tick retry.
    """
    if mode not in ("PASSIVE", "FULL", "RESTART", "TRUNCATE"):
        mode = "TRUNCATE"
    c = None
    try:
        c = sqlite3.connect(str(DB), timeout=1.0)
        # Deliberately short: background work must yield to real queries rather
        # than queue up in front of them.
        c.execute("PRAGMA busy_timeout=1000")
        row = c.execute(f"PRAGMA wal_checkpoint({mode})").fetchone()
        # (busy, wal_pages, reclaimed_pages) — busy=1 means someone held the WAL
        # and it was NOT fully reclaimed.
        return "busy" if (row and row[0]) else "truncated"
    except Exception:
        return "error"
    finally:
        if c is not None:
            try:
                c.close()
            except Exception:
                pass


def _maybe_checkpoint() -> str:
    """One tick. Returns the decision taken, which is what the tests assert on."""
    if wal_size() < _WAL_MIN_BYTES:
        _ckpt_stats["skipped"] += 1
        return "small"
    if time.monotonic() - _last_write < _WAL_IDLE_SEC:
        _ckpt_stats["skipped"] += 1
        return "writing"

    _ckpt_stats["runs"] += 1
    result = checkpoint_wal("TRUNCATE")
    if result == "truncated":
        _ckpt_stats["truncated"] += 1
    elif result == "busy":
        # A reader held the WAL, so it could not be reset to zero. PASSIVE still
        # folds back whatever it can, which stops the file growing further.
        _ckpt_stats["busy"] += 1
        checkpoint_wal("PASSIVE")
    else:
        _ckpt_stats["errors"] += 1
    return result


def _ckpt_loop() -> None:
    while not _ckpt_stop.wait(_WAL_INTERVAL):
        try:
            _maybe_checkpoint()
        except Exception:
            # The thread must outlive any single bad tick. Letting it die would
            # silently return the process to unbounded WAL growth, and the next
            # tick may well succeed.
            _ckpt_stats["errors"] += 1
        try:
            # Piggybacked on this tick rather than given its own thread: both are
            # periodic housekeeping on the same cadence, and a second daemon
            # thread would cost more than the work it does. If this loop is
            # disabled the pool simply stays warm — i.e. exactly how it behaved
            # before eviction existed.
            evict_idle_connections()
        except Exception:
            _ckpt_stats["errors"] += 1


def start_wal_checkpointer() -> bool:
    """Start the background checkpointer. Idempotent; True if one is running.

    Set `AGENT2_WAL_CHECKPOINT_SEC=0` to disable — SQLite's own auto-checkpoint
    then handles the WAL exactly as it did before this existed.
    """
    global _ckpt_thread
    if _WAL_INTERVAL <= 0:
        return False
    with _ckpt_lock:
        if _ckpt_thread is not None and _ckpt_thread.is_alive():
            return True
        _ckpt_stop.clear()
        try:
            t = threading.Thread(target=_ckpt_loop, name="a2-wal-checkpoint",
                                 daemon=True)
            t.start()
        except Exception:
            # Thread exhaustion. Degrade to SQLite's automatic checkpointing
            # rather than failing startup over housekeeping.
            _log_checkpoint_failure()
            return False
        _ckpt_thread = t
        return True


def stop_wal_checkpointer(timeout: float = 2.0) -> None:
    """Stop the checkpointer. For shutdown, and for tests that repoint `DB`."""
    global _ckpt_thread
    _ckpt_stop.set()
    with _ckpt_lock:
        t, _ckpt_thread = _ckpt_thread, None
    if t is not None and t.is_alive():
        t.join(timeout)


def _log_checkpoint_failure() -> None:
    """Best-effort audit line; imported lazily like the other log helpers here."""
    try:
        from agent2.core import logging as alog
        alog.exception("wal checkpointer could not start")
    except Exception:
        pass


def wal_stats() -> dict:
    """Checkpointer state, for diagnostics and the health endpoint."""
    t = _ckpt_thread
    return {"wal_bytes": wal_size(),
            "interval_sec": _WAL_INTERVAL,
            "min_bytes": _WAL_MIN_BYTES,
            "running": t is not None and t.is_alive(),
            **_ckpt_stats}



# ── Migrations ─────────────────────────────────────────────────────────────────
# This replaces a loop of `ALTER TABLE ... ADD COLUMN` wrapped in
# try/except/pass. That pattern worked, but it could not tell the expected
# "duplicate column name" apart from a locked DB, a typo'd type, or a disk
# error — every one of them read as "already applied", so a genuinely failed
# migration looked identical to a successful one and the app carried on against
# a schema that was silently wrong.
#
# Two changes fix that:
#   1. Idempotence comes from CHECKING (`PRAGMA table_info`), not from catching.
#      An exception is therefore a real fault and is allowed to be one.
#   2. Applied versions are recorded in `schema_migrations`, so a normal start
#      does one indexed read instead of re-running every historical ALTER.
#
# Adding a migration: append to _MIGRATIONS with the next version number. Never
# renumber or edit an applied one — the ledger records what already ran.

_IDENT_OK = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _column_exists(c: sqlite3.Connection, table: str, col: str) -> bool:
    """True if *table* already has *col*. Also False when the table is absent."""
    rows = c.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == col for r in rows)


def _table_exists(c: sqlite3.Connection, table: str) -> bool:
    row = c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,)).fetchone()
    return row is not None


def _add_column(table: str, col: str, typedef: str):
    """Build an idempotent 'add this column if missing' migration step.

    Identifiers cannot be bound as SQL parameters, so they are validated against
    a strict identifier pattern rather than interpolated blind. The values are
    literals in this file today; the check is what keeps that true if a caller
    ever passes something dynamic.
    """
    if not (_IDENT_OK.match(table) and _IDENT_OK.match(col)):
        raise ValueError(f"unsafe identifier in migration: {table}.{col}")

    def step(c: sqlite3.Connection) -> None:
        # Re-checked inside the migration's own transaction, so two processes
        # starting at once cannot both add the column: the loser sees it present.
        if not _table_exists(c, table):
            return          # table arrives already-correct on a fresh DB
        if _column_exists(c, table, col):
            return
        c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typedef}")

    step.__name__ = f"add_{table}_{col}"
    return step


def _create_table(table: str, ddl: str):
    """Build an idempotent 'create this table if missing' migration step.

    Why a migration rather than another entry in `init_db()`'s executescript:
    that script is the historical schema, and a reader cannot tell from it WHEN
    a table arrived. A numbered step is dated by its ledger row, and — unlike
    migration 5 — it does real work on both a fresh and an existing DB, because
    `_apply_migrations()` runs after the executescript and the executescript does
    not declare these tables.

    Idempotence still comes from CHECKING (`sqlite_master`), not from catching:
    two processes starting at once cannot both create the table, and a genuine
    fault (locked DB, bad DDL) is allowed to raise like any other migration.
    """
    if not _IDENT_OK.match(table):
        raise ValueError(f"unsafe identifier in migration: {table}")

    def step(c: sqlite3.Connection) -> None:
        if _table_exists(c, table):
            return
        c.executescript(ddl)

    step.__name__ = f"create_{table}"
    return step


# ── Task-engine schema (migrations 8 and 9) ───────────────────────────────────
# The persistent task model. Before this, a run's checklist lived only in
# `Session.todos` — a Python list — so Ctrl+C, a crash, or simply a new process
# lost every completed step and the next run started from task 1. These two
# tables are what make "resume without repeating completed work" possible.

_TASK_SESSIONS_DDL = """
    CREATE TABLE IF NOT EXISTS task_sessions (
        id           TEXT PRIMARY KEY,
        chat_id      TEXT DEFAULT '',
        cwd          TEXT DEFAULT '',
        workspace_id TEXT DEFAULT '',
        model        TEXT DEFAULT '',
        mode         TEXT DEFAULT '',
        goal         TEXT DEFAULT '',
        status       TEXT DEFAULT 'active',
        surface      TEXT DEFAULT '',
        created_at   TEXT DEFAULT(datetime('now')),
        updated_at   TEXT DEFAULT(datetime('now'))
    );
"""

# `seq` is the display order the model asked for; `status` is one of
# core.tasks.TaskStatus. `dependencies` and `checkpoint` are JSON text because
# SQLite has no array/object type and both are read whole, never queried into.
_AGENT_TASKS_DDL = """
    CREATE TABLE IF NOT EXISTS agent_tasks (
        id             TEXT PRIMARY KEY,
        session_id     TEXT NOT NULL,
        parent_task_id TEXT DEFAULT '',
        seq            INTEGER DEFAULT 0,
        title          TEXT NOT NULL,
        description    TEXT DEFAULT '',
        status         TEXT DEFAULT 'pending',
        priority       INTEGER DEFAULT 5,
        dependencies   TEXT DEFAULT '[]',
        created_at     TEXT DEFAULT(datetime('now')),
        started_at     TEXT DEFAULT '',
        completed_at   TEXT DEFAULT '',
        attempt_count  INTEGER DEFAULT 0,
        progress       REAL DEFAULT 0.0,
        result         TEXT DEFAULT '',
        error          TEXT DEFAULT '',
        checkpoint     TEXT DEFAULT '{}'
    );
"""


def _normalize_task_session_cwd(c: sqlite3.Connection) -> None:
    """Rewrite every `task_sessions.cwd` through the canonical project key.

    ⚠️ Data migration, not schema. `ToolContext.task_session()` passes
    `workspace.root()` — which is NOT normcased — while chats store the normcased
    form, so on Windows a session was written as `C:\\Users\\…` and then looked up
    as `c:\\users\\…`. Every single-row read still worked, so nothing looked
    broken; only the project-scoped recovery query silently matched nothing.

    Normalising in Python rather than SQL because the rule is platform-specific:
    `normcase` lowercases and flips separators on Windows and is the identity on
    POSIX, so a `lower(cwd)` here would corrupt case-sensitive Linux paths.
    """
    if not _table_exists(c, "task_sessions"):
        return
    from agent2.core.context import project_key
    for sid, cwd in c.execute(
            "SELECT id, cwd FROM task_sessions WHERE cwd IS NOT NULL AND cwd <> ''"
    ).fetchall():
        canon = project_key(str(cwd))
        if canon != cwd:
            c.execute("UPDATE task_sessions SET cwd=? WHERE id=?", (canon, sid))


# ── MCP state schema (migration 11) ───────────────────────────────────────────
# Which MCP servers auto-connect, PER PROJECT (Task 9). Before this the answer
# lived in two global `settings` rows (`burp_auto_connect`, `zap_auto_connect`),
# so enabling ZAP while pentesting one target also armed it in every other
# checkout — the isolation this table exists to give back.
#
# ⚠️ `project` IS `core.context.project_key(...)`, NOT a raw path. It is the same
# canonical form `chats.cwd` and `task_sessions.cwd` use, for the same reason:
# `workspace.root()` is not normcased, so on Windows a row written as
# `C:\Users\…` would never be found again by a lookup for `c:\users\…`, and the
# read would silently fall through to the global default instead of erroring.
#
# There is deliberately NO row for "the global default": an install that predates
# this table keeps its `settings` value as the fallback for every project that has
# no row yet (rule 22), and a migration cannot invent which project that setting
# was meant for.
_MCP_STATE_DDL = """
    CREATE TABLE IF NOT EXISTS mcp_state (
        project    TEXT NOT NULL,
        server     TEXT NOT NULL,
        enabled    INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT DEFAULT(datetime('now')),
        PRIMARY KEY (project, server)
    );
"""


# ── MCP endpoint config schema (migration 12) ─────────────────────────────────
# WHERE an MCP server is and what credential reaches it. Deliberately NOT keyed by
# project, unlike `mcp_state` above: `mcp_state` answers "should ZAP auto-connect
# in THIS checkout", which is a per-target decision, while this table answers
# "which ZAP, on which port, with which security key" — one running ZAP per
# machine. Keying it per project would force the user to retype the same endpoint
# in every checkout, which is the exact friction Task 9 removed.
#
# ⚠️ THE ENDPOINT IS STORED AS A URL, NOT AS host+port+scheme COLUMNS. The CLI and
# the Web UI both offer a "Port" field because that is the field ZAP's own options
# panel shows, but both edit the URL through `McpBridge.set_port()`. A separate
# `port` column could disagree with the port inside `url`, and the loser would be
# whichever one the connect path happened not to read.
#
# ⚠️ `security_key` HOLDS AN `a2s:` REFERENCE, NOT A CREDENTIAL. Migration 14
# sealed this column along with `api_keys.api_key` and `providers.api_key`, so what
# is written here is a pointer resolved by `core.secrets.resolve()` on the way out.
# `seal()` reads back what it wrote and returns the plaintext if the round trip
# disagrees, so a caller cannot persist a dangling reference — which is also why a
# legacy row still holding a raw value keeps working: `resolve()` passes a non-ref
# through unchanged. It is recorded here rather than left memory-only because the
# user must not have to re-enter ZAP's key every session, and an env var is not
# editable from the UI.
# What must stay true: this column is never returned by `status()`, never carried
# by `/api/mcp`, and never logged. Surfaces get `key_set` plus a **constant**
# `••••••••` from `mask_secret()` whose length is not derived from the value — the
# old `k[:6]…k[-4:]` form printed most of a ten-character ZAP key, and would print
# six characters of ciphertext now that the column holds a reference.
_MCP_CONFIG_DDL = """
    CREATE TABLE IF NOT EXISTS mcp_config (
        server       TEXT PRIMARY KEY,
        url          TEXT DEFAULT '',
        security_key TEXT DEFAULT '',
        updated_at   TEXT DEFAULT(datetime('now'))
    );
"""


# ── Web session schema (migration 13) ─────────────────────────────────────────
# Task 14. Server-side records for browser sessions, so a session can be
# EXPIRED, ROTATED and REVOKED — none of which a self-contained signed cookie can
# do. Before this the web surface had no session concept at all: every route was
# anonymous and `AGENT2_HOST` defaults to `0.0.0.0`, so anything that could route
# to the box could drive `run_raw_command`.
#
# ⚠️ `id` IS THE SHA-256 OF THE COOKIE VALUE, NEVER THE COOKIE VALUE. The same
# goes for `csrf_hash`. This table is therefore not a credential store: reading
# `agent2.db` (a backup, a mounted Docker volume, a screen-shared `sqlite3`
# session) hands over no session anybody can replay. A cookie is verified by
# hashing what arrived and looking THAT up — the reverse direction does not exist.
# The one-way property is the whole point, so nothing here may ever be widened to
# hold the plaintext "for debugging".
#
# ⚠️ There is deliberately no `token` column. The access token that grants a
# session lives in memory (or `AGENT2_WEB_TOKEN`) and is written to disk nowhere —
# see `agent2/server/auth.py`. Persisting it would turn every DB copy into a
# permanent key to the agent's shell.
#
# `expires_at` is an idle deadline that each authenticated request pushes
# forward; `rotated_from` records the previous row's id so a rotation is
# traceable without keeping the old session usable.
_WEB_SESSIONS_DDL = """
    CREATE TABLE IF NOT EXISTS web_sessions (
        id           TEXT PRIMARY KEY,
        csrf_hash    TEXT DEFAULT '',
        ip           TEXT DEFAULT '',
        user_agent   TEXT DEFAULT '',
        created_at   TEXT DEFAULT(datetime('now')),
        last_seen_at TEXT DEFAULT(datetime('now')),
        expires_at   TEXT DEFAULT(datetime('now')),
        rotated_from TEXT DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_web_sessions_expires
        ON web_sessions(expires_at);
"""


# ── Model capability + fallback schema (migrations 15, 16) ────────────────────
# Phase 6. Two tables with deliberately different lifetimes.
#
# `model_caps` is CONFIGURATION: one row per model key the user has corrected by
# hand (Task 17's "support updating configured model metadata"). It is sparse —
# a model nobody edited has no row, and `capabilities.get()` answers from the
# catalog. ⚠️ The payload is JSON in one `meta` column rather than a column per
# field, and that is the right call here for once: the field list belongs to
# `capabilities.FIELDS`, and mirroring it as ten columns would mean a migration
# every time a capability is added, with the schema and the module free to
# disagree in between. Nothing joins or filters on these values.
_MODEL_CAPS_DDL = """
    CREATE TABLE IF NOT EXISTS model_caps (
        model_key  TEXT PRIMARY KEY,
        meta       TEXT DEFAULT '{}',
        updated_at TEXT DEFAULT(datetime('now'))
    );
"""

# `model_attempts` is a LEDGER: one row per model call outcome (Task 19's
# "Record: primary_model, fallback_model, failure_reason, latency"). It is what
# makes "the fallback worked" checkable after the fact instead of a claim.
#
# ⚠️ IT IS BOUNDED BY THE WRITER, NOT BY HOPE. `router.record_attempt()` trims to
# `ROUTER_LEDGER_MAX` rows on insert. An unbounded per-call log on a busy dual-mode
# install grows the DB forever, and the one thing a diagnostic table must never do
# is become the problem it was added to diagnose.
#
# ⚠️ `fallback_of` IS THE LINK, AND IT IS WHAT MAKES A ROW READABLE. A row with
# `model='2.5-flash', fallback_of='2.5-pro'` says "flash ran because pro failed";
# the same row without it says only "flash ran", and the sequence that explains a
# turn is unrecoverable. `primary_model` is the model the TURN asked for, which is
# not always `fallback_of` — a second fallback hop has a different predecessor.
_MODEL_ATTEMPTS_DDL = """
    CREATE TABLE IF NOT EXISTS model_attempts (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id       TEXT DEFAULT '',
        session_id    TEXT DEFAULT '',
        primary_model TEXT DEFAULT '',
        model         TEXT DEFAULT '',
        fallback_of   TEXT DEFAULT '',
        ok            INTEGER DEFAULT 0,
        failure_kind  TEXT DEFAULT '',
        failure_reason TEXT DEFAULT '',
        latency_ms    INTEGER DEFAULT 0,
        created_at    TEXT DEFAULT(datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS idx_model_attempts_created
        ON model_attempts(created_at DESC);
"""


def _seal_existing_secrets(c) -> None:
    """Replace plaintext credentials with `a2s:` references (Task 16, migration 14).

    Three columns across three tables hold live credentials: `api_keys.api_key`,
    `providers.api_key` and `mcp_config.security_key`. Each is read, sealed and
    written back.

    ⚠️ VERIFY-THEN-REPLACE, ONE ROW AT A TIME, AND NEVER DESTRUCTIVE ON DOUBT.
    A row is rewritten only when `secrets.seal()` returned something that is both a
    reference AND resolves back to the exact original value. `seal()` already
    performs that round trip and hands back the plaintext if it fails, so the guard
    here is `is_ref(...)`: a backend that could not store the value leaves the row
    exactly as it was. "The key is still readable in the DB" is a bad outcome;
    "the user's only API key is gone" is an unrecoverable one, and this migration
    can only ever produce the first.

    ⚠️ A MISSING TABLE IS NOT A FAILURE. `providers` is created by
    `llm/providers.py`, not by `init_db()` (see migration 5's note), so on a fresh
    install it does not exist yet — and a migration that raised on that would break
    every first run. Nothing needs sealing in a table with no rows.

    ⚠️ THIS MIGRATION IS IDEMPOTENT AND SAFE TO LOSE. `resolve()` passes a non-ref
    through unchanged, so an install where migration 14 never ran, or ran
    half-way, keeps working — every reader handles both shapes. That is why this
    can be a data migration at all rather than a hard cutover.
    """
    targets = (
        ("api_keys", "api_key", "label"),
        ("providers", "api_key", "id"),
        ("mcp_config", "security_key", "server"),
    )
    for table, column, key_col in targets:
        try:
            if not _table_exists(c, table) or not _column_exists(c, table, column):
                continue
            rows = c.execute(
                f"SELECT {key_col} AS k, {column} AS v FROM {table}"
            ).fetchall()
        except Exception:
            continue
        for row in rows:
            try:
                value = str(row["v"] or "")
                if not value or _secrets.is_ref(value):
                    continue
                ref = _secrets.seal(value, namespace=table, name=str(row["k"]))
                if not _secrets.is_ref(ref):
                    continue                      # backend refused — leave it be
                c.execute(
                    f"UPDATE {table} SET {column}=? WHERE {key_col}=?",
                    (ref, row["k"]))
            except Exception:
                # One unconvertible row must not stop the other credentials being
                # protected, and it stays perfectly usable as plaintext.
                continue


# (version, name, step). Ordered, applied once, recorded.
_MIGRATIONS: list[tuple[int, str, object]] = [
    (1, "chats.model",       _add_column("chats", "model", "TEXT DEFAULT 'gemini-2.5-flash'")),
    (2, "chats.mode",        _add_column("chats", "mode", "TEXT DEFAULT 'pro'")),
    (3, "chats.cwd",         _add_column("chats", "cwd", "TEXT DEFAULT ''")),
    (4, "chats.status",      _add_column("chats", "status", "TEXT DEFAULT 'active'")),
    (5, "providers.user_agent", _add_column("providers", "user_agent", "TEXT DEFAULT ''")),
    # 6 and 7 back fields the `save_memory` tool schema has always advertised to
    # the model and then silently discarded. `importance` is also what ranks the
    # prompt block and pruning, so a memory the user called critical is the last
    # thing dropped rather than an arbitrary one.
    (6, "memories.importance", _add_column("memories", "importance", "INTEGER DEFAULT 5")),
    (7, "memories.tags",       _add_column("memories", "tags", "TEXT DEFAULT ''")),
    # 8 and 9 add the persistent task engine (agent2/core/tasks.py). A checklist
    # used to live only in Session.todos, so every interruption lost it.
    (8, "task_sessions",       _create_table("task_sessions", _TASK_SESSIONS_DDL)),
    (9, "agent_tasks",         _create_table("agent_tasks", _AGENT_TASKS_DDL)),
    # 10 repairs sessions written before `core.tasks` canonicalised `cwd`. Without
    # it those rows keep a path the recovery query can never match again.
    (10, "task_sessions.cwd_normalized", _normalize_task_session_cwd),
    # 11 makes MCP auto-connect per-project (agent2/integrations/state.py). The
    # two legacy global settings stay READABLE as the fallback for a project with
    # no row, so an existing install does not wake up with its bridges off.
    (11, "mcp_state",          _create_table("mcp_state", _MCP_STATE_DDL)),
    # 12 makes the MCP endpoint (url/port) and ZAP's security key editable and
    # durable from both surfaces. Before it, a non-default ZAP port or key could
    # only be set with an environment variable, so `/mcp zap config` had nowhere
    # to write and the key was lost on every restart.
    (12, "mcp_config",         _create_table("mcp_config", _MCP_CONFIG_DDL)),
    # 13 gives the web surface a session it can expire, rotate and revoke (Task
    # 14). It stores hashes only — see the DDL comment for why that is load-bearing
    # rather than merely tidy.
    (13, "web_sessions",       _create_table("web_sessions", _WEB_SESSIONS_DDL)),
    # 14 replaces every plaintext credential with a `a2s:` reference (Task 16).
    # It is a DATA migration with no schema change, and it is the one migration in
    # this list that must never destroy what it cannot replace — see
    # `_seal_existing_secrets`.
    (14, "secrets.sealed",     _seal_existing_secrets),
    # 15 and 16 are Phase 6. `model_caps` lets a user correct what Agent2 believes
    # about a model (Task 17); `model_attempts` records every model call outcome so
    # a fallback is auditable rather than merely asserted (Task 19).
    (15, "model_caps",         _create_table("model_caps", _MODEL_CAPS_DDL)),
    (16, "model_attempts",     _create_table("model_attempts", _MODEL_ATTEMPTS_DDL)),
    # 17 and 18 scope a memory and a rule to the workspace that created it (Task
    # 23 — `core/broker/isolation.py` owns what the column MEANS). Two columns,
    # two versions, exactly like 6 and 7: the ledger records what was applied,
    # and one row cannot describe two tables.
    # ⚠️ The default is `''`, which `isolation.py` reads as "every project sees
    # it". That is what keeps rule 22 true through this upgrade: every memory and
    # rule written before Task 23 stays visible in every checkout, instead of
    # being silently reassigned to whichever project happened to be open when the
    # migration ran and vanishing from all the others.
    (17, "memories.project",   _add_column("memories", "project", "TEXT NOT NULL DEFAULT ''")),
    (18, "rules.project",      _add_column("rules", "project", "TEXT NOT NULL DEFAULT ''")),
]

SCHEMA_VERSION = max(v for v, _, _ in _MIGRATIONS)


def schema_version() -> int:
    """Highest applied migration version (0 on a DB that predates the ledger)."""
    try:
        row = qone("SELECT MAX(version) AS v FROM schema_migrations")
        return int((row or {}).get("v") or 0)
    except Exception:
        return 0


def _applied_versions(c: sqlite3.Connection) -> set[int]:
    try:
        return {r[0] for r in c.execute("SELECT version FROM schema_migrations")}
    except Exception:
        return set()


def _apply_migrations(c: sqlite3.Connection) -> list[int]:
    """Run every unapplied migration in order. Returns the versions applied.

    Each step commits with its ledger row in ONE transaction, so a crash can
    never leave a migration half-recorded: either the schema change and its
    receipt are both there, or neither is.

    A failing step is logged and re-raised — deliberately. A missing column
    surfaces later as a confusing query error in an unrelated feature, which is
    far worse to debug than a loud failure here. This is NOT the graceful
    degradation path: there is no meaningful degraded mode for a wrong schema.
    """
    done = _applied_versions(c)
    applied: list[int] = []
    with _write_lock:
        for version, name, step in _MIGRATIONS:
            if version in done:
                continue
            try:
                step(c)
                c.execute(
                    "INSERT OR REPLACE INTO schema_migrations(version, name) VALUES(?,?)",
                    (version, name))
                c.commit()
                applied.append(version)
            except Exception:
                try:
                    c.rollback()
                except Exception:
                    pass
                _log_migration_failure(version, name)
                raise
    return applied


def _log_migration_failure(version: int, name: str) -> None:
    """Best-effort audit line. Imported lazily: core.logging imports config, and
    database.py is imported very early in startup."""
    try:
        from agent2.core import logging as alog
        alog.exception("schema migration failed", version=version, name=name)
    except Exception:
        pass


def ensure_column(table: str, col: str, typedef: str) -> bool:
    """Add *col* to *table* if missing. True only if this call added it.

    The public entry point for a module that owns its own table (providers) and
    wants the checked behaviour without registering a numbered migration.

    A missing table returns False rather than raising: the caller's CREATE TABLE
    already declares the column, so there is nothing to back-fill.
    """
    with _write_lock, _checkout() as (c, owned):
        if not _table_exists(c, table) or _column_exists(c, table, col):
            return False
        _add_column(table, col, typedef)(c)
        if owned:
            c.commit()
        return True


# Index DDL, one per entry so a failure costs one index instead of the batch.
# The comment on each is the query it backs — delete the query, delete the index.
_INDEXES: list[tuple[str, str]] = [
    # build_context(): messages for one chat, newest first.
    ("idx_messages_chat_created",
     "CREATE INDEX IF NOT EXISTS idx_messages_chat_created"
     " ON messages(chat_id, created_at DESC)"),
    # msg_count subquery in core.context: counts user/assistant rows per chat.
    ("idx_messages_chat_role",
     "CREATE INDEX IF NOT EXISTS idx_messages_chat_role ON messages(chat_id, role)"),
    # get_active_chat() / list_chats_for_cwd(): project-scoped, newest first.
    ("idx_chats_cwd_updated",
     "CREATE INDEX IF NOT EXISTS idx_chats_cwd_updated"
     " ON chats(cwd, status, updated_at DESC)"),
    ("idx_chats_updated",
     "CREATE INDEX IF NOT EXISTS idx_chats_updated ON chats(updated_at DESC)"),
    # list_rules(active_only=True) on every system_prompt() build.
    ("idx_rules_active",
     "CREATE INDEX IF NOT EXISTS idx_rules_active ON rules(active, created_at)"),
    # top_memories(): the prompt block takes the most important N, so this is the
    # ordering the hot path actually asks for. ⚠️ Indexes are created AFTER
    # _apply_migrations() for exactly this reason — `importance` arrives in
    # migration 6.
    ("idx_memories_rank",
     "CREATE INDEX IF NOT EXISTS idx_memories_rank"
     " ON memories(importance DESC, created_at DESC)"),
    # Task 23 scopes both of those reads to a project, so the same two orderings
    # are wanted again with the project key leading. The unscoped pair above is
    # KEPT rather than replaced: `AGENT2_CONTEXT_ISOLATION=off` still issues the
    # unscoped query, and `_apply_migrations()` runs before this list, so a DB
    # that stopped at 16 for any reason would otherwise lose an index it still
    # uses. Two small indexes on two small tables is the cheaper mistake.
    ("idx_memories_project_rank",
     "CREATE INDEX IF NOT EXISTS idx_memories_project_rank"
     " ON memories(project, importance DESC, created_at DESC)"),
    ("idx_rules_project_active",
     "CREATE INDEX IF NOT EXISTS idx_rules_project_active"
     " ON rules(project, active, created_at)"),
    # PIL ranking queries: ORDER BY confidence*(frequency+1).
    ("idx_pil_vocab_rank",
     "CREATE INDEX IF NOT EXISTS idx_pil_vocab_rank"
     " ON pil_vocab(confidence DESC, frequency DESC)"),
    ("idx_pil_phrases_rank",
     "CREATE INDEX IF NOT EXISTS idx_pil_phrases_rank"
     " ON pil_phrases(confidence DESC, frequency DESC)"),
    ("idx_pil_ngrams_prev",
     "CREATE INDEX IF NOT EXISTS idx_pil_ngrams_prev"
     " ON pil_ngrams(prev, confidence DESC, frequency DESC)"),
    ("idx_pil_prefs_cat",
     "CREATE INDEX IF NOT EXISTS idx_pil_prefs_cat ON pil_prefs(category, weight DESC)"),
    # fileintel op-log history view.
    ("idx_file_ops_created",
     "CREATE INDEX IF NOT EXISTS idx_file_ops_created ON file_ops(created_at DESC)"),
    # core.tasks.list_tasks(): every read of a checklist is "this session, in
    # display order". Without it the panel full-scans agent_tasks on each redraw.
    ("idx_agent_tasks_session",
     "CREATE INDEX IF NOT EXISTS idx_agent_tasks_session"
     " ON agent_tasks(session_id, seq)"),
    # core.tasks.resume_session(): find the newest resumable session for a cwd.
    ("idx_task_sessions_cwd",
     "CREATE INDEX IF NOT EXISTS idx_task_sessions_cwd"
     " ON task_sessions(cwd, status, updated_at DESC)"),
    # latest_session_for_chat(): the CLI/web bind a checklist by chat.
    ("idx_task_sessions_chat",
     "CREATE INDEX IF NOT EXISTS idx_task_sessions_chat"
     " ON task_sessions(chat_id, updated_at DESC)"),
]


def _log_index_failure(label: str) -> None:
    """An index is an optimization: losing one degrades speed, not correctness,
    so this is the graceful-degradation path (unlike a failed migration)."""
    try:
        from agent2.core import logging as alog
        alog.exception("index creation failed", index=label)
    except Exception:
        pass


# ── Schema ─────────────────────────────────────────────────────────────────────

def init_db() -> None:
    """Create tables, indexes, and run any pending migrations."""
    c = _conn()

    # WAL is a persistent property of the database FILE, not the connection, so
    # it only needs setting once — but it must be set before other connections
    # start writing. Readers then never block the writer and vice versa, which
    # is what lets dual mode (web thread + CLI process) share one file.
    wal_on = False
    try:
        row = c.execute("PRAGMA journal_mode=WAL").fetchone()
        wal_on = bool(row) and str(row[0]).lower() == "wal"
    except Exception:
        pass  # e.g. a network filesystem that refuses WAL — fall back to default

    c.executescript("""
        CREATE TABLE IF NOT EXISTS chats (
            id         TEXT PRIMARY KEY,
            title      TEXT DEFAULT 'New Chat',
            model      TEXT DEFAULT 'gemini-2.5-flash',
            mode       TEXT DEFAULT 'pro',
            created_at TEXT DEFAULT(datetime('now')),
            updated_at TEXT DEFAULT(datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS messages (
            id         TEXT PRIMARY KEY,
            chat_id    TEXT,
            role       TEXT,      -- user | assistant | tool_call | tool_result
            content    TEXT,
            meta       TEXT DEFAULT '{}',
            created_at TEXT DEFAULT(datetime('now')),
            FOREIGN KEY(chat_id) REFERENCES chats(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS memories (
            id         TEXT PRIMARY KEY,
            content    TEXT,
            importance INTEGER DEFAULT 5,
            tags       TEXT DEFAULT '',
            -- Which workspace owns this memory; '' = every project sees it.
            -- ⚠️ Also declared as migration 17, and both are required: this
            -- statement is what a FRESH database gets (migrations only ever run
            -- against a DB that is already behind), and the migration is what an
            -- existing one gets. Drop either half and one of the two populations
            -- has a table `core.memory` queries a column it does not have.
            project    TEXT NOT NULL DEFAULT '',
            created_at TEXT DEFAULT(datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS rules (
            id         TEXT PRIMARY KEY,
            content    TEXT,
            active     INTEGER DEFAULT 1,
            project    TEXT NOT NULL DEFAULT '',   -- see memories.project (migration 18)
            created_at TEXT DEFAULT(datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS key_usage (
            key_label       TEXT PRIMARY KEY,
            total_tokens    INTEGER DEFAULT 0,
            total_requests  INTEGER DEFAULT 0,
            last_used       TEXT DEFAULT(datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        );

        -- Gemini API keys live here now (replaces the old .env storage). All
        -- API credentials — Gemini keys AND custom-provider keys — are kept in
        -- this single SQLite DB so the Web UI and CLI share one source of truth.
        CREATE TABLE IF NOT EXISTS api_keys (
            label      TEXT PRIMARY KEY,
            api_key    TEXT UNIQUE,
            name       TEXT,
            active     INTEGER DEFAULT 1,
            created_at TEXT DEFAULT(datetime('now'))
        );

        -- File Intelligence op-log: one row per file operation the agent runs
        -- (see agent2/fileintel/oplog.py). Used for the history/audit trail.
        CREATE TABLE IF NOT EXISTS file_ops (
            id           TEXT PRIMARY KEY,
            tool         TEXT,
            operation    TEXT,
            path         TEXT,
            output_paths TEXT DEFAULT '[]',
            duration_ms  INTEGER DEFAULT 0,
            ok           INTEGER DEFAULT 1,
            error        TEXT DEFAULT '',
            created_at   TEXT DEFAULT(datetime('now'))
        );

        -- ── Personal Intelligence Layer (PIL) ──────────────────────────────
        -- ONE centralized, offline personal-memory store shared by all three
        -- PIL modules (prediction, grammar, prompt-improvement). No module keeps
        -- its own memory — every table below is written by the shared learning
        -- engine (agent2.core.pil.learning) and read by every module.

        -- Learned vocabulary: single tokens the user actually uses. `frequency`
        -- is a usage count; `confidence` (0..1) is nudged by the learning engine
        -- from passive accept/ignore/delete signals. `kind` tags the token
        -- (word | tech | framework | library | project | company | abbrev).
        CREATE TABLE IF NOT EXISTS pil_vocab (
            token       TEXT PRIMARY KEY,
            display     TEXT,                 -- original casing to emit
            kind        TEXT DEFAULT 'word',
            frequency   INTEGER DEFAULT 1,
            confidence  REAL DEFAULT 0.5,
            lang        TEXT DEFAULT '',       -- optional language context
            project     TEXT DEFAULT '',       -- optional project (cwd) context
            updated_at  TEXT DEFAULT(datetime('now'))
        );

        -- Learned multi-word phrases / prompt templates. `body` is the full
        -- phrase; prediction matches the user's typed tail against its prefix
        -- and suggests the remainder.
        CREATE TABLE IF NOT EXISTS pil_phrases (
            id          TEXT PRIMARY KEY,
            body        TEXT UNIQUE,
            frequency   INTEGER DEFAULT 1,
            confidence  REAL DEFAULT 0.5,
            project     TEXT DEFAULT '',
            updated_at  TEXT DEFAULT(datetime('now'))
        );

        -- N-gram transitions: given `prev` (one or two space-joined tokens),
        -- how often `next` followed. Powers context-aware next-word prediction.
        CREATE TABLE IF NOT EXISTS pil_ngrams (
            prev        TEXT,
            next        TEXT,
            frequency   INTEGER DEFAULT 1,
            confidence  REAL DEFAULT 0.5,
            updated_at  TEXT DEFAULT(datetime('now')),
            PRIMARY KEY (prev, next)
        );

        -- Learned user preferences that Module 3 uses to enrich prompts. `value`
        -- is a preference token (e.g. a framework name or "dark mode"); `weight`
        -- reflects how strongly it's proven. `category` groups them
        -- (framework | library | style | length | workflow | language).
        CREATE TABLE IF NOT EXISTS pil_prefs (
            id          TEXT PRIMARY KEY,
            category    TEXT,
            value       TEXT,
            weight      REAL DEFAULT 0.5,
            frequency   INTEGER DEFAULT 1,
            updated_at  TEXT DEFAULT(datetime('now')),
            UNIQUE(category, value)
        );

        -- ── Cross-surface synchronization ──────────────────────────────────
        -- One row per shared resource ('memories', 'rules', 'api_keys', …)
        -- holding a monotonically increasing version. Any surface that mutates
        -- a resource bumps its version; other surfaces (the CLI process, other
        -- browser tabs) poll cheaply and refresh only what actually changed.
        -- This is the cross-PROCESS half of agent2.core.sync — in-process
        -- listeners are notified directly via its event bus.
        CREATE TABLE IF NOT EXISTS sync_state (
            resource   TEXT PRIMARY KEY,
            version    INTEGER DEFAULT 0,
            updated_at TEXT DEFAULT(datetime('now')),
            origin     TEXT DEFAULT ''
        );

        -- Which schema migrations have run. One row per applied version; the
        -- highest is schema_version(). This is the ledger that lets startup do
        -- ONE read instead of re-attempting every historical migration, and it
        -- makes "why does this DB have that column" answerable.
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    INTEGER PRIMARY KEY,
            name       TEXT,
            applied_at TEXT DEFAULT(datetime('now'))
        );
    """)

    # Migration 5 targets `providers`, a table owned by llm/providers.py and
    # created after this function runs. When it is absent the step is a no-op
    # and still records — correct, because the CREATE TABLE that follows already
    # includes `user_agent`. On an OLD db the table is present here and the
    # column is added for real. init_providers_table() calls ensure_column()
    # either way as the backstop.
    _apply_migrations(c)

    # ── Indexes ───────────────────────────────────────────────────────────────
    # Every one of these backs a query the app runs on a hot path. Without them
    # SQLite full-scans `messages` (the largest table by far) on each turn.
    #
    # Created AFTER _apply_migrations(), because idx_chats_cwd_updated indexes
    # `cwd`/`status` — columns that migrations 3 and 4 add. Reordering breaks a
    # fresh DB.
    #
    # Applied one at a time rather than as one executescript: an index is a
    # performance optimization, so a single failure should cost that ONE index,
    # not the other nine. As one script, the first error aborts the rest.
    for label, ddl in _INDEXES:
        try:
            c.execute(ddl)
        except Exception:
            _log_index_failure(label)

    c.commit()
    c.close()
    _init_done.set()

    # Only meaningful in WAL mode — there is no -wal file to fold back otherwise.
    # Started here rather than per entry point because all three modes (web, CLI,
    # dual) call init_db() and all three write, so wiring it into each launcher
    # would be three chances to miss one. Failure to start is not fatal: SQLite's
    # own auto-checkpoint remains enabled underneath.
    if wal_on:
        start_wal_checkpointer()

    # One-time migration of any legacy .env Gemini keys into the DB.
    try:
        migrate_env_keys()
    except Exception:
        pass


# ── Settings (small key-value store, shared by web + CLI) ───────────────────────

def get_setting(key: str, default: str | None = None) -> str | None:
    """Return a persisted setting value, or *default* if unset."""
    try:
        row = qone("SELECT value FROM settings WHERE key=?", (key,))
        return row["value"] if row else default
    except Exception:
        return default


def set_setting(key: str, value: str) -> None:
    """Persist a setting (upsert)."""
    exe("INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)))
    bump_version("settings")


# ── Cross-process sync version counters ─────────────────────────────────────────
# The `sync_state` table is how two OS processes sharing one agent2.db (dual
# mode: web server + CLI child) learn that the other changed something. A writer
# bumps the resource's version; readers compare the version they last saw. This
# is deliberately dumber and cheaper than a notification channel: a bump is one
# UPSERT, a poll is one indexed primary-key lookup.

def bump_version(resource: str, origin: str = "") -> int:
    """Increment a resource's sync version. Returns the new version (0 on error).

    Called by every mutating helper for a shared resource. Never raises — a
    failed bump degrades to "other surfaces refresh a little later", not a crash.
    """
    resource = (resource or "").strip()
    if not resource:
        return 0
    try:
        with _write_lock, _checkout() as (c, owned):
            c.execute(
                "INSERT INTO sync_state(resource, version, updated_at, origin) "
                "VALUES(?, 1, datetime('now'), ?) "
                "ON CONFLICT(resource) DO UPDATE SET "
                "  version = version + 1, "
                "  updated_at = datetime('now'), "
                "  origin = excluded.origin",
                (resource, origin or ""),
            )
            row = c.execute("SELECT version FROM sync_state WHERE resource=?",
                            (resource,)).fetchone()
            if owned:
                c.commit()
        return int(row["version"]) if row else 0
    except Exception:
        return 0


def get_version(resource: str) -> int:
    """Current sync version for one resource (0 when never bumped)."""
    try:
        row = qone("SELECT version FROM sync_state WHERE resource=?", ((resource or "").strip(),))
        return int(row["version"]) if row else 0
    except Exception:
        return 0


def all_versions() -> dict[str, int]:
    """Every resource's version in one query — what pollers should use so a
    refresh check costs a single round-trip regardless of resource count."""
    try:
        return {r["resource"]: int(r["version"])
                for r in qall("SELECT resource, version FROM sync_state")}
    except Exception:
        return {}


# ── Gemini API keys (stored in DB — no more .env) ───────────────────────────────
# The whole app now keeps Gemini keys in the `api_keys` table. These helpers are
# the single, failsafe gateway; every one swallows errors and returns a sane
# default so a corrupt/locked DB can never crash the agent.

def _next_key_label() -> str:
    """Smallest positive integer label not already taken."""
    try:
        used = {r["label"] for r in qall("SELECT label FROM api_keys")}
    except Exception:
        used = set()
    n = 1
    while str(n) in used:
        n += 1
    return str(n)


def list_api_keys() -> list[dict]:
    """All stored Gemini keys, ordered by creation, or [] on any failure.

    ⚠️ Task 16: `api_key` comes back RESOLVED — the caller gets a usable key
    whether the row holds a `a2s:` reference or a legacy plaintext value. Resolving
    here rather than in `llm/keys.py` is deliberate: this function is the single
    gateway to the table, so there is exactly one place that has to know secrets
    are stored indirectly, and `KeyRotator.reload()` needs no change at all.

    `resolve()` returns a non-ref unchanged, so a row written before the migration
    (or by a build without this module) keeps working untouched.
    """
    try:
        rows = qall("SELECT * FROM api_keys ORDER BY created_at, label")
    except Exception:
        return []
    for r in rows:
        try:
            r["api_key"] = _secrets.resolve(r.get("api_key"))
        except Exception:
            # A resolve fault must not blank the whole list: the other keys are
            # still usable, and `reload()` already drops empty values.
            r["api_key"] = ""
    return rows


def add_api_key(api_key: str, name: str | None = None) -> tuple[bool, str]:
    """Insert a Gemini key. Returns (ok, label_or_reason).

    ⚠️ THE DEDUP COMPARES RESOLVED VALUES, NOT STORED ONES (Task 16).
    The old `WHERE api_key=?` cannot work once rows hold references: AES-GCM uses a
    fresh nonce per call, so sealing the same key twice yields two different
    ciphertexts and the duplicate check would silently never match — the user would
    add one key five times and watch the rotator round-robin through five copies of
    a single quota. There is no index to lose here (`api_keys` has none on
    `api_key`), and the table holds a handful of rows.
    """
    api_key = (api_key or "").strip().replace(" ", "").replace("\n", "")
    if len(api_key) < 15:
        return False, "too_short"
    try:
        for existing in list_api_keys():
            if (existing.get("api_key") or "") == api_key:
                return False, "already_exists"
        label = _next_key_label()
        exe("INSERT INTO api_keys(label, api_key, name, active) VALUES(?,?,?,1)",
            (label, _secrets.seal(api_key, namespace="api_keys", name=label),
             name or f"Key {label}"))
        bump_version("api_keys")
        return True, label
    except Exception as ex:
        return False, str(ex)[:120]


def remove_api_key(label: str) -> None:
    """Delete a key row, and the material behind it.

    ⚠️ `secrets.forget` first, while the row can still be read. An encrypted ref
    carries its own ciphertext so deleting the row is enough, but a keyring ref
    points at an OS credential-store entry that would otherwise be orphaned — the
    user would later find a stale `agent2` entry in their Keychain with no way to
    tell what it was for.
    """
    try:
        row = qone("SELECT api_key FROM api_keys WHERE label=?", (str(label),))
        if row:
            _secrets.forget(str(row.get("api_key") or ""))
    except Exception:
        pass
    try:
        exe("DELETE FROM api_keys WHERE label=?", (str(label),))
        bump_version("api_keys")
    except Exception:
        pass


def set_api_key_name(label: str, name: str) -> None:
    try:
        exe("UPDATE api_keys SET name=? WHERE label=?", (name, str(label)))
        bump_version("api_keys")
    except Exception:
        pass


def migrate_env_keys() -> int:
    """One-time import of any GEMINI_API_KEY* values from a legacy .env file
    (or the environment) into the DB, then neutralise the .env file. Returns the
    number of keys migrated. Safe to call on every startup — it no-ops once done."""
    migrated = 0
    try:
        from agent2.config import ENV  # local import to avoid cycles
    except Exception:
        ENV = None

    # Collect candidate keys from both the process env and the .env file.
    candidates: list[str] = []
    for i in ([""] + [f"_{n}" for n in range(2, 10)]):
        import os as _os
        v = _os.environ.get(f"GEMINI_API_KEY{i}", "").strip()
        if v:
            candidates.append(v)
    if ENV is not None:
        try:
            if ENV.exists():
                for line in ENV.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        if k.strip().startswith("GEMINI_API_KEY"):
                            candidates.append(v.strip().strip('"').strip("'"))
        except Exception:
            pass

    placeholder = "your_gemini_api_key_here"
    seen: set[str] = set()
    for v in candidates:
        v = v.strip()
        if not v or v == placeholder or len(v) < 15 or v in seen:
            continue
        seen.add(v)
        ok, _ = add_api_key(v)
        if ok:
            migrated += 1

    # Retire the legacy .env so it is never read again.
    try:
        if ENV is not None and ENV.exists():
            ENV.rename(ENV.with_suffix(".env.migrated"))
    except Exception:
        pass

    return migrated
