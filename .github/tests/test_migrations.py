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

import re
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


def _index_migrations() -> list[tuple[int, str]]:
    """`(version, index_name)` for every migration built by `_create_index`.

    Read off the BUILT step's `__name__` (`_create_index` stamps `index_<name>`),
    never off the source text: a step that stopped creating an index would keep the
    name, but a step that was never registered cannot appear here at all — which is
    the direction that actually breaks.
    """
    return [(v, f.__name__[len("index_"):]) for v, _n, f in db._MIGRATIONS
            if getattr(f, "__name__", "").startswith("index_")]


def test_a_migration_added_index_is_also_declared_where_the_schema_is_read():
    """⚠️ BOTH HALVES, ALWAYS — `_create_index`'s docstring is the rule, this is the
    guard.

    An index on a migration-created table has two homes and needs both:

    * the table's `_*_DDL` constant (or `_INDEXES`, for a table the historical
      `executescript` owns) — what a reader of the schema sees, and what a **fresh**
      database gets; and
    * a numbered `_MIGRATIONS` step — the only thing an **existing** install ever
      runs, because `_create_table` returns early when the table is already there.

    Declaring only the DDL half ships an index that reaches fresh databases alone,
    so the install with a year of rows — the one whose scans actually hurt — is
    precisely the one that silently never gets it. Declaring only the migration half
    is subtler and also wrong: the schema a reader greps no longer describes the
    schema on disk, and the next person adding an index to that table copies a
    pattern that omits it.

    Migration 28 (`idx_agent_tasks_live`) is the house precedent; 30–32 follow it.
    """
    ddls = {k: v for k, v in vars(db).items()
            if k.endswith("_DDL") and isinstance(v, str)}
    declared = {label for label, _ddl in db._INDEXES}

    missing = []
    for version, name in _index_migrations():
        homes = [k for k, sql in ddls.items() if name in sql]
        if name in declared:
            homes.append("_INDEXES")
        if not homes:
            missing.append((version, name))

    assert not missing, (
        "these indexes exist as a migration but are declared in no table DDL and "
        "no _INDEXES entry, so the schema a reader greps disagrees with the schema "
        f"on disk: {missing}")


def test_an_existing_install_gets_an_index_added_after_its_table(tmp_path, monkeypatch):
    """The half of `_create_index` that a DDL-only edit would silently skip.

    Simulates the real upgrade rather than trusting the ledger: initialise fully,
    then DROP each migration-added index and delete its ledger row — exactly the
    state of an install that was created before those migrations existed, whose
    tables already exist and whose `_create_table` step therefore returns early.
    Re-running `init_db()` must put every one of them back.
    """
    _fresh_db(tmp_path, monkeypatch)
    added = _index_migrations()
    assert added, "no migration creates an index — _create_index has no callers"

    lowest = min(v for v, _ in added)
    for _version, name in added:
        db.exe(f"DROP INDEX IF EXISTS {name}")
    db.exe("DELETE FROM schema_migrations WHERE version>=?", (lowest,))

    def names():
        return {r["name"] for r in db.qall(
            "SELECT name FROM sqlite_master WHERE type='index'")}

    gone = sorted(n for _v, n in added if n in names())
    assert not gone, f"the test could not drop {gone}, so it proves nothing"

    db._init_done.clear()
    db.init_db()

    present = names()
    still_missing = sorted(n for _v, n in added if n not in present)
    assert not still_missing, (
        "an existing install never received these indexes — they are declared in a "
        f"table DDL that only a fresh database runs: {still_missing}")
    assert db.schema_version() == db.SCHEMA_VERSION


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


# ── Migrations 19-21: the durable execution ledger (Task 24) ──────────────────

EXEC_TABLES = ("exec_commands", "exec_tool_calls", "exec_workflows")

EXEC_INDEXES = (
    "idx_exec_commands_live", "idx_exec_commands_project",
    "idx_exec_tool_calls_live", "idx_exec_tool_calls_session",
    "idx_exec_workflows_live",
    # Migrations 30-32 closed the asymmetry: each ledger had one of the two shapes
    # its readers need and not the other. See
    # `test_every_exec_ledger_indexes_the_columns_its_readers_filter_on`.
    "idx_exec_commands_session",
    "idx_exec_tool_calls_project",
    "idx_exec_workflows_project",
)


def _tables():
    return {r["name"] for r in db.qall(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def test_the_exec_ledger_tables_exist_on_a_fresh_db(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    assert set(EXEC_TABLES) <= _tables()


def test_the_exec_ledger_tables_are_added_to_a_legacy_db(tmp_path, monkeypatch):
    """⚠️ The upgrade path, not just the greenfield one.

    A table created only by the `CREATE TABLE IF NOT EXISTS` block and never by a
    migration would appear on a fresh DB and be missing from every existing
    install — and `init_db()` would still report the current schema version, so
    nothing would look wrong until recovery found no rows to read.
    """
    _legacy_db(tmp_path, monkeypatch)
    db.init_db()
    assert set(EXEC_TABLES) <= _tables()
    assert db.schema_version() == db.SCHEMA_VERSION


def test_the_exec_ledger_indexes_exist(tmp_path, monkeypatch):
    """All eight, by name. `interrupted()` scans on (status, updated_at)."""
    _fresh_db(tmp_path, monkeypatch)
    names = {r["name"] for r in db.qall(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    for label in EXEC_INDEXES:
        assert label in names, f"{label} was not created"


# The exec-ledger indexes that shipped INSIDE their own table's CREATE, so every
# install receives them for free: `_create_table` runs the whole DDL when the table is
# absent. Anything else an exec DDL declares was added to a table that already exists
# out there, and needs its own numbered `_create_index` step or only FRESH databases
# ever get it — `_create_index`'s docstring is the rule and
# `test_an_exec_index_added_after_its_table_has_a_migration` is the guard.
EXEC_INDEXES_BORN_WITH_THEIR_TABLE = frozenset({
    "idx_exec_commands_live", "idx_exec_commands_project",       # migration 19
    "idx_exec_tool_calls_live", "idx_exec_tool_calls_session",   # migration 20
    "idx_exec_workflows_live",                                   # migration 21
})


def test_an_exec_index_added_after_its_table_has_a_migration():
    """⚠️ THE DIRECTION A DDL-ONLY EDIT BREAKS, AND IT BREAKS IT SILENTLY.

    `test_a_migration_added_index_is_also_declared_where_the_schema_is_read` walks
    migration → DDL. This walks DDL → migration, which is the half that actually
    ships broken: adding a `CREATE INDEX` line to `_EXEC_WORKFLOWS_DDL` makes every
    test on a fresh database pass, every plan look right, and every install older
    than the edit keep scanning — the install with the most rows being the one that
    never gets it.

    It is scoped to the three exec ledgers because that is where the asymmetry was:
    `exec_commands` had the project shape and not the session one, `exec_tool_calls`
    had the session shape and not the project one, and `exec_workflows` had neither,
    so migrations 30-32 exist. A fourth index on any of them added without a step
    lands here rather than in a user's startup path.
    """
    by_migration = {name for _v, name in _index_migrations()}
    for table in EXEC_TABLES:
        ddl = getattr(db, f"_{table.upper()}_DDL")
        for name in re.findall(r"CREATE INDEX IF NOT EXISTS (\w+)", ddl):
            assert name in EXEC_INDEXES_BORN_WITH_THEIR_TABLE or name in by_migration, (
                f"{name} is declared in {table}'s DDL but no migration creates it, so "
                "only a fresh database will ever have it — see _create_index's "
                "docstring, and add a numbered step beside the DDL line")


def _leading_columns(table: str) -> set[str]:
    """The first column of every index on `table`.

    A composite index only serves a predicate through its LEADING column, so this is
    the set a query's equality terms have to intersect for any index to apply.
    """
    lead = set()
    for row in db.qall(f"PRAGMA index_list({table})"):
        cols = db.qall(f"PRAGMA index_info({row['name']})")
        first = next((c["name"] for c in cols if c["seqno"] == 0), None)
        if first:
            lead.add(first)
    return lead


def test_every_exec_ledger_indexes_the_columns_its_readers_filter_on(tmp_path, monkeypatch):
    """⚠️ TWO READERS, TWO SHAPES, AND EACH LEDGER SHIPPED ONLY ONE OF THEM.

    Both queries against these tables are fixed and known:

    * `verify.evidence_for()` reads `exec_commands` and `exec_tool_calls` with
      `project=? AND session_id=?` — two queries per verification report, whatever
      the node count — so `session_id` is the selective term.
    * `execstate.interrupted()` reads all three on the STARTUP path with
      `status NOT IN (…) AND instance<>? AND updated_at<? AND project=?`. `NOT IN`
      is not an equality term and `<>`/`<` are not either, so `project` is the only
      one an index can serve.

    Before migrations 30-32, `exec_commands` had the project index and not the
    session one, `exec_tool_calls` had the session index and not the project one,
    and `exec_workflows` had neither. Nothing failed — each query simply fell back
    to a less selective index or a skip-scan, which is why an assertion on the
    SHAPE is what catches it. Measured cost is in `database.py`'s comment beside
    each index; at 50 000 ledger rows the worst case was 42.2 ms of the startup
    path, and `execstate._trim()` never evicts an unsettled row, so a ledger that
    size is a real install rather than a hypothetical.

    Asserted on leading columns rather than on a plan, because which of two
    applicable indexes SQLite picks depends on `ANALYZE` statistics this schema does
    not ship — a plan assertion would pass or fail on the statistics, not on the
    schema.
    """
    _fresh_db(tmp_path, monkeypatch)

    for table in EXEC_TABLES:
        lead = _leading_columns(table)
        assert "project" in lead, (
            f"{table} has no index led by `project`, so the crash scan in "
            "execstate.interrupted() has no equality term to search on and "
            "skip-scans this table on every startup")

    for table in ("exec_commands", "exec_tool_calls"):
        lead = _leading_columns(table)
        assert "session_id" in lead, (
            f"{table} has no index led by `session_id`, so verify.evidence_for() "
            "reads it by project — the same predicate served by a far less "
            "selective index than its twin uses")


def test_the_exec_ledger_readers_never_scan_a_whole_table(tmp_path, monkeypatch):
    """The plans themselves, for the two real queries. Complements the shape test.

    A `SCAN` here would mean the leading-column index exists and is not applicable —
    an ORDER BY or a predicate rewritten in a way that defeats it. Which index is
    chosen is left to the planner on purpose (see the shape test's docstring); that
    it searches rather than scans is not statistics-dependent.

    ⚠️ `USE TEMP B-TREE FOR LAST TERM OF ORDER BY` is EXPECTED and is not a defect:
    both readers order by `updated_at DESC, rowid DESC`, and SQLite appends rowid to
    a non-unique index ASCENDING, so no index of ours can satisfy that tie-break.
    The sort runs over matching rows only, which is the point of the search.
    """
    _fresh_db(tmp_path, monkeypatch)

    verify_q = ("SELECT * FROM {t} WHERE project=? AND session_id=? "
                "ORDER BY updated_at DESC, rowid DESC LIMIT ?")
    scan_q = ("SELECT * FROM {t} WHERE status NOT IN (?,?,?) AND instance<>? "
              "AND updated_at<? AND project=? ORDER BY updated_at DESC, rowid DESC "
              "LIMIT ?")
    cases = [(t, verify_q, ("p", "s", 5)) for t in ("exec_commands", "exec_tool_calls")]
    cases += [(t, scan_q, ("a", "b", "c", "i", 0, "p", 5)) for t in EXEC_TABLES]

    for table, sql, params in cases:
        rows = db.qall("EXPLAIN QUERY PLAN " + sql.format(t=table), params)
        plan = " | ".join(str(r["detail"]) for r in rows)
        assert f"SCAN {table}" not in plan, (
            f"{table} is read by a full table scan: {plan}")
        assert "USING INDEX" in plan or "USING COVERING INDEX" in plan, (
            f"{table} is not read through any index: {plan}")


def test_a_legacy_upgrade_to_the_exec_ledger_keeps_existing_rows(tmp_path, monkeypatch):
    """Rule 22 — three new tables may not cost the user their data."""
    _legacy_db(tmp_path, monkeypatch)
    db.init_db()

    assert db.qone("SELECT title FROM chats WHERE id='c1'")["title"] == "old chat"
    assert db.qone("SELECT model_id FROM providers WHERE id='p1'")["model_id"] == "gpt-4"
    for table in EXEC_TABLES:
        assert db.qone(f"SELECT COUNT(*) AS n FROM {table}")["n"] == 0


def test_exec_commands_columns_match_the_ddl(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    cols = {r["name"] for r in db.qall("PRAGMA table_info(exec_commands)")}
    assert cols == {
        "id", "instance", "project", "session_id", "task_id", "surface", "term_id",
        "command", "process_id", "status", "exit_code", "output_lines", "error",
        "created_at", "started_at", "last_output_at", "completed_at", "updated_at",
    }


def test_exec_tool_calls_columns_match_the_ddl(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    cols = {r["name"] for r in db.qall("PRAGMA table_info(exec_tool_calls)")}
    assert cols == {
        "id", "instance", "project", "session_id", "task_id", "chat_id", "surface",
        "tool", "detail", "destructive", "status", "ok", "error",
        "started_at", "completed_at", "updated_at",
    }


def test_exec_workflows_columns_match_the_ddl(tmp_path, monkeypatch):
    """Both `step` (the name) and `step_index` (the position) — they are not one column."""
    _fresh_db(tmp_path, monkeypatch)
    cols = {r["name"] for r in db.qall("PRAGMA table_info(exec_workflows)")}
    assert cols == {
        "id", "instance", "project", "session_id", "chat_id", "name", "status",
        "step", "step_index", "total_steps", "state", "error",
        "started_at", "completed_at", "updated_at",
    }


def test_no_exec_table_has_a_column_that_could_hold_output(tmp_path, monkeypatch):
    """⚠️ DDL invariant 3 (`database.py:922-928`), asserted rather than trusted.

    Persisting command output would write tokens, passwords and `env` dumps into
    the very file `core/secrets.py` exists to protect. The absence of the column
    is the enforcement, so a later migration that adds one has to break a test
    instead of silently shipping. `error` is exempt: it holds our own short reason
    string, never a process's stream.
    """
    _fresh_db(tmp_path, monkeypatch)
    forbidden = {"stdout", "stderr", "output", "content", "body", "data",
                 "text", "payload", "secret", "token", "api_key"}
    for table in EXEC_TABLES:
        cols = {r["name"] for r in db.qall(f"PRAGMA table_info({table})")}
        leak = cols & forbidden
        assert not leak, f"{table} grew a content-bearing column: {sorted(leak)}"


def test_the_exec_ledger_primary_keys_make_a_write_idempotent(tmp_path, monkeypatch):
    """One row per execution, whatever the heartbeat count.

    `record_command` upserts on `id`, so the PK is what keeps a `make -j8` from
    writing one row per output line.
    """
    _fresh_db(tmp_path, monkeypatch)
    for table in EXEC_TABLES:
        pk = [r["name"] for r in db.qall(f"PRAGMA table_info({table})") if r["pk"]]
        assert pk == ["id"], f"{table} primary key is {pk}"
