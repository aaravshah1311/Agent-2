# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
Tests for versioned schema migrations (agent2.database).

Run from the repo root:  python -m pytest .github/tests/test_migrations.py -v

What replaced what
──────────────────
Startup used to run a list of `ALTER TABLE ... ADD COLUMN` statements wrapped in
`try/except: pass`, with the comment "column already exists". That worked, but
it could not distinguish the expected duplicate-column error from a locked DB, a
typo'd type, or a disk fault — all four read as "already applied". A migration
that genuinely failed looked exactly like one that succeeded, and the app went
on serving queries against a schema that was silently wrong.

The runner now derives idempotence from CHECKING (`PRAGMA table_info`) rather
than from catching, records applied versions in `schema_migrations`, and lets a
real fault raise. These tests pin all three properties, plus the ordering
constraint that indexes are created only after the columns they index exist.

Migrations touch the DB FILE, so most tests here build their own throwaway
database rather than using the session one.
"""

import sqlite3

import pytest

from agent2 import database as db


@pytest.fixture(autouse=True)
def _no_leaked_connections():
    """Every test here repoints `db.DB` at a throwaway file.

    A connection opened against that file must not survive in the shared pool —
    a later test (or another module) borrowing it would silently query the wrong
    database. Closing on both sides keeps the pool honest, and releases the file
    so Windows can unlink tmp_path.
    """
    db.close_all()
    yield
    db.close_all()
    db._init_done.clear()


# ── The migration registry itself ─────────────────────────────────────────────

def test_versions_are_unique_and_contiguous():
    """A duplicate or skipped version means the ledger cannot be trusted."""
    versions = [v for v, _, _ in db._MIGRATIONS]
    assert versions == sorted(versions), "migrations must be listed in order"
    assert len(set(versions)) == len(versions), f"duplicate version in {versions}"
    assert versions == list(range(1, len(versions) + 1)), (
        f"versions must be contiguous from 1: {versions}"
    )


def test_schema_version_constant_matches_the_registry():
    assert max(v for v, _, _ in db._MIGRATIONS) == db.SCHEMA_VERSION


def test_identifiers_in_migrations_are_validated():
    """Table/column names cannot be bound as SQL params, so they are checked."""
    with pytest.raises(ValueError):
        db._add_column("chats", "bad col", "TEXT")
    with pytest.raises(ValueError):
        db._add_column("chats; DROP TABLE chats", "x", "TEXT")


# ── Applying to a real database file ─────────────────────────────────────────

def _fresh_db(tmp_path, monkeypatch):
    """Point agent2.database at a brand-new DB file and initialise it."""
    path = tmp_path / "fresh.db"
    monkeypatch.setattr(db, "DB", path)
    db.close_all()
    db._init_done.clear()
    db.init_db()
    return path


def test_a_fresh_db_lands_on_the_current_version(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    assert db.schema_version() == db.SCHEMA_VERSION
    rows = db.qall("SELECT version, name FROM schema_migrations ORDER BY version")
    assert [r["version"] for r in rows] == [v for v, _, _ in db._MIGRATIONS]
    assert all(r["name"] for r in rows), "every ledger row should name its migration"


def test_migrated_columns_exist_on_a_fresh_db(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    cols = {r["name"] for r in db.qall("PRAGMA table_info(chats)")}
    assert {"model", "mode", "cwd", "status"} <= cols


def test_indexes_are_created_after_the_columns_they_index(tmp_path, monkeypatch):
    """idx_chats_cwd_updated indexes `cwd`/`status`, which migrations add.

    CLAUDE.md calls this out: creating indexes before the migration loop breaks
    a fresh DB. This asserts the ordering actually holds rather than trusting
    the comment.
    """
    _fresh_db(tmp_path, monkeypatch)
    names = {r["name"] for r in db.qall(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_chats_cwd_updated" in names, (
        "the cwd/status index is missing — indexes ran before the migrations"
    )
    for label, _ddl in db._INDEXES:
        assert label in names, f"index {label} was not created"


def test_rerunning_init_db_attempts_no_alters(tmp_path, monkeypatch):
    """The point of the ledger: a current DB does zero migration work.

    The old loop attempted every ALTER on every start and threw away the errors.
    Counting happens on the connection `init_db` uses, since sqlite3.Connection
    is a C type whose methods cannot be patched.
    """
    _fresh_db(tmp_path, monkeypatch)

    seen: list[str] = []
    real_conn = db._conn

    class Counting:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *a, **k):
            if "ADD COLUMN" in str(sql).upper():
                seen.append(str(sql))
            return self._inner.execute(sql, *a, **k)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    monkeypatch.setattr(db, "_conn", lambda: Counting(real_conn()))
    db.init_db()

    assert seen == [], f"a current DB still attempted ALTERs: {seen}"
    assert db.schema_version() == db.SCHEMA_VERSION


def test_ledger_is_not_rewritten_on_restart(tmp_path, monkeypatch):
    """applied_at must keep recording when a migration FIRST ran."""
    _fresh_db(tmp_path, monkeypatch)
    before = db.qall("SELECT version, applied_at FROM schema_migrations ORDER BY version")
    db.init_db()
    after = db.qall("SELECT version, applied_at FROM schema_migrations ORDER BY version")
    assert after == before


# ── Upgrading a legacy database ──────────────────────────────────────────────

LEGACY_SQL = """
    CREATE TABLE chats (id TEXT PRIMARY KEY, title TEXT,
                        created_at TEXT, updated_at TEXT);
    CREATE TABLE providers (id TEXT PRIMARY KEY, name TEXT, base_url TEXT,
                            api_key TEXT, model_id TEXT, format TEXT,
                            created_at TEXT);
    INSERT INTO chats(id, title) VALUES('c1', 'old chat');
    INSERT INTO providers(id, name, model_id) VALUES('p1', 'old', 'gpt-4');
"""


def _legacy_db(tmp_path, monkeypatch):
    """A pre-ledger DB: no schema_migrations, columns missing, rows present."""
    path = tmp_path / "legacy.db"
    raw = sqlite3.connect(str(path))
    raw.executescript(LEGACY_SQL)
    raw.commit()
    raw.close()
    monkeypatch.setattr(db, "DB", path)
    db.close_all()
    db._init_done.clear()
    return path


def test_legacy_db_is_upgraded_in_place(tmp_path, monkeypatch):
    _legacy_db(tmp_path, monkeypatch)
    db.init_db()

    assert db.schema_version() == db.SCHEMA_VERSION
    cols = {r["name"] for r in db.qall("PRAGMA table_info(chats)")}
    assert {"model", "mode", "cwd", "status"} <= cols


def test_legacy_upgrade_preserves_existing_rows(tmp_path, monkeypatch):
    """A migration must never cost the user their data."""
    _legacy_db(tmp_path, monkeypatch)
    db.init_db()

    assert db.qone("SELECT title FROM chats WHERE id='c1'")["title"] == "old chat"
    assert db.qone("SELECT model_id FROM providers WHERE id='p1'")["model_id"] == "gpt-4"
    # And the new column is readable with its default rather than absent.
    assert db.qone("SELECT status FROM chats WHERE id='c1'")["status"] == "active"


def test_legacy_providers_table_gains_user_agent(tmp_path, monkeypatch):
    """The ALTER that providers.py used to run under try/except/pass."""
    _legacy_db(tmp_path, monkeypatch)
    db.init_db()

    cols = {r["name"] for r in db.qall("PRAGMA table_info(providers)")}
    assert "user_agent" in cols


# ── A real fault must NOT be mistaken for "already applied" ──────────────────

def test_a_failing_migration_raises_instead_of_being_swallowed(tmp_path, monkeypatch):
    """The core defect this item fixed.

    Under try/except/pass a broken migration was indistinguishable from one that
    had already run, so startup continued against a wrong schema. There is no
    useful degraded mode for that — it must be loud.
    """
    _fresh_db(tmp_path, monkeypatch)

    def exploding(_c):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(db, "_MIGRATIONS",
                        [*db._MIGRATIONS, (99, "boom", exploding)])
    db._init_done.clear()

    with pytest.raises(sqlite3.OperationalError):
        db.init_db()


def test_a_failed_migration_is_not_recorded_as_applied(tmp_path, monkeypatch):
    """Schema change and ledger row commit together, or not at all."""
    _fresh_db(tmp_path, monkeypatch)

    def exploding(_c):
        raise sqlite3.OperationalError("nope")

    monkeypatch.setattr(db, "_MIGRATIONS",
                        [*db._MIGRATIONS, (99, "boom", exploding)])
    with pytest.raises(sqlite3.OperationalError):
        db.init_db()

    assert db.qone("SELECT 1 AS x FROM schema_migrations WHERE version=99") is None
    assert db.schema_version() == db.SCHEMA_VERSION


def test_ensure_column_reports_whether_it_changed_anything(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    assert db.ensure_column("chats", "brand_new_col", "TEXT DEFAULT ''") is True
    assert db.ensure_column("chats", "brand_new_col", "TEXT DEFAULT ''") is False
    cols = {r["name"] for r in db.qall("PRAGMA table_info(chats)")}
    assert "brand_new_col" in cols


def test_ensure_column_on_a_missing_table_is_a_noop(tmp_path, monkeypatch):
    """The providers case: the table may not exist yet when the step runs."""
    _fresh_db(tmp_path, monkeypatch)
    assert db.ensure_column("no_such_table", "col", "TEXT") is False


# ── providers.py owns its own table, so it carries its own backstop ──────────

def test_user_agent_backfill_runs_even_when_the_ledger_says_applied(tmp_path, monkeypatch):
    """Why providers.py still calls ensure_column despite migration 5 existing.

    `init_db()` runs before providers.py creates its table, so migration 5 finds
    `providers` absent, no-ops, and STILL records itself as applied — correct,
    because the CREATE TABLE that follows already declares the column. But that
    means the ledger can read "providers.user_agent: applied" on a database where
    the column is genuinely missing. Here the table arrives afterwards without it:
    CREATE TABLE IF NOT EXISTS is a no-op, and the ledger will never revisit
    version 5, so the backstop is the only thing that can fix the schema.
    """
    from agent2.llm import providers as P

    _fresh_db(tmp_path, monkeypatch)
    db.exe("""CREATE TABLE providers (id TEXT PRIMARY KEY, name TEXT,
                                      base_url TEXT, api_key TEXT, model_id TEXT,
                                      format TEXT, created_at TEXT)""")

    # Premise: the ledger is current, yet the column is missing.
    assert db.schema_version() == db.SCHEMA_VERSION
    cols = {r["name"] for r in db.qall("PRAGMA table_info(providers)")}
    assert "user_agent" not in cols, "premise broken — the table already has it"

    P.init_providers_table()

    cols = {r["name"] for r in db.qall("PRAGMA table_info(providers)")}
    assert "user_agent" in cols, (
        "the backfill was skipped; a ledger row masked a genuinely missing column"
    )


def test_a_failing_backfill_in_providers_is_not_swallowed(tmp_path, monkeypatch):
    """providers.py must not re-add the swallow that `ensure_column` removed.

    The old code was `try: exe(ALTER) except: pass`, which read a locked DB the
    same as "column already exists". Wrapping the new call in try/except would
    restore exactly that blindness, so the propagation is pinned here.

    Patched on the PROVIDERS module, not on `database`: providers.py does
    `from agent2.database import ensure_column`, which binds the function object
    at import time — patching `database.ensure_column` would not be seen.
    """
    from agent2.llm import providers as P

    _fresh_db(tmp_path, monkeypatch)

    def exploding(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(P, "ensure_column", exploding)
    with pytest.raises(sqlite3.OperationalError):
        P.init_providers_table()


# ── A lost index degrades speed, not correctness ──────────────────────────────

def test_one_bad_index_does_not_cost_the_others(tmp_path, monkeypatch):
    """Indexes are applied individually on purpose.

    As a single executescript, the first failure aborted the remaining nine —
    turning one unusable index into a fully unindexed database.
    """
    monkeypatch.setattr(db, "_INDEXES",
                        [("bad_index", "CREATE INDEX bad_index ON nonexistent(x)"),
                         *db._INDEXES])
    _fresh_db(tmp_path, monkeypatch)

    names = {r["name"] for r in db.qall(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    assert "bad_index" not in names
    for label, _ddl in db._INDEXES[1:]:
        assert label in names, f"{label} was lost to an unrelated index failure"
