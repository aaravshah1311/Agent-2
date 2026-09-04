# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
Tests for workspace isolation — ``agent2/core/broker/isolation.py`` (Task 23) and
the three subsystems that apply it: ``core.memory``, ``core.rules`` and the
broker's task-results collector.

Run from the repo root:  python -m pytest .github/tests/test_isolation.py -v

What this file is really about
──────────────────────────────
"Workspace A must not use workspace B's memory, rules or task results unless
explicitly shared" is a rule with **no error state**. Every failure mode here is
silent: a leak shows the model a fact or an instruction from a checkout the user
is not in, and an over-tight scope hides the user's own data behind an empty
list. Neither raises, neither logs, and both look correct from one side. So the
tests below are mostly *cross-project* — they assert what project A sees while
project B holds the row — because a single-project test passes identically
whether the predicate is applied or not.

Coverage
  - the vocabulary: ``SHARED`` is ``''`` AND the column default, and the two
    governed tables carry the column ``NOT NULL DEFAULT ''``
  - the policy: the mode is read live from the environment, is env-ONLY (never a
    `settings` row), and an unrecognised value falls back to `project` — never to
    `off`
  - ``canonical()`` delegates to ``core.context.project_key`` and blank input
    NEVER reaches it (⚠️ `project_key("")` means "the current directory")
  - ``stamp()``: the current project, ``SHARED`` on `shared=True`, and ⚠️
    ``SHARED`` — not the current project — while isolation is OFF
  - ``allows()``: total, and asymmetric on purpose (unknown owner allowed, known
    foreign owner refused)
  - ``scope_sql()``: always valid SQL, executable in both modes, and its
    ``IS NULL`` arm actually matches
  - the schema (rules 22/23): migrations 17/18 in the ledger, the scoped indexes
    added and the unscoped pair KEPT, and a **pre-Task-23 database upgraded in
    place keeps every row visible from every project**
  - memories: reads, the prompt block, the *dedup lookup*, and every destructive
    operation (delete / bulk delete / clear / prune) scoped
  - rules: the same, plus ``toggle_rule`` refusing a foreign rule without leaking
    its content
  - task results: ``broker._session_visible`` and the collector that consumes it
  - invalidation: a workspace switch rebuilds BOTH cached prompt blocks
  - one declaration: the broker's project key IS ``isolation.current()``, and
    neither scoped module carries a predicate of its own

⚠️ THE LOAD-BEARING TESTS IN THIS FILE
  * ``test_a_workspace_switch_rebuilds_the_memories_block`` and
    ``…_the_rules_block`` — sabotage-verified. The two prompt caches are
    process-global with ``ttl=0``, and nothing about a workspace switch bumps the
    `memories`/`rules` resources, so dropping the ``on_invalidate`` registration
    keeps serving project A's memories AND project A's rules to project B. Every
    query is correctly scoped and the leak still happens, through the cache.
  * ``test_the_dedup_cannot_swallow_another_projects_save`` — sabotage-verified.
    An unscoped dedup lookup finds project B's identical row, "reinforces" it, and
    returns it: A's save reports success, writes nothing A can see, and the memory
    is gone from A forever with no error anywhere.
  * ``test_a_pre_task_23_database_upgrades_with_every_row_shared`` —
    sabotage-verified. Stamping the migration with the project that happened to be
    open would empty a user's memory store in every other checkout (rule 22).
  * ``test_with_isolation_off_a_new_row_is_stamped_shared`` — sabotage-verified.
    Stamping the current project while the rule is off means turning isolation ON
    later hides rows that were created under global semantics.
  * ``test_clear_memories_cannot_empty_another_projects_store`` —
    sabotage-verified. This one is data loss, not a leak (rule 22).
  * ``test_toggle_rule_from_another_project_changes_nothing_and_leaks_nothing`` —
    sabotage-verified. A rule is an instruction the model obeys, which makes
    leaking one worse than leaking a memory.

Most tests here build their own throwaway database: the destructive operations
under test (`clear_memories`, `prune_memories`) are scoped but still delete, and
the session DB holds `SHARED` rows other modules' tests rely on.
"""

import os
import sqlite3

import pytest

from agent2 import database as db
from agent2.core import broker as B_
from agent2.core import memory as mem
from agent2.core import rules as rul
from agent2.core import sync as S
from agent2.core import workspace as W
from agent2.core.broker import isolation as iso

# Two project keys that are not this process's workspace and never will be. They
# are pure path arithmetic — the directories do not need to exist, because
# `canonical()` is normcase(abspath(...)) and nothing here stats them.
PROJ_A = iso.canonical(os.path.join(os.sep, "proj", "alpha"))
PROJ_B = iso.canonical(os.path.join(os.sep, "proj", "bravo"))


@pytest.fixture(autouse=True)
def _own_db(tmp_path, monkeypatch):
    """A throwaway database, a known isolation mode, and cold caches.

    `monkeypatch.setattr(db, "DB", …)` works because `database._conn()` reads the
    module global at call time, so `core.memory`'s import-bound `qall`/`exe` land
    on this file too (the pattern `test_migrations.py` established).

    The mode is *deleted* rather than set, so every test starts in the default
    `project` mode and the ones that care set it explicitly.
    """
    monkeypatch.delenv(iso.ENV_MODE, raising=False)
    path = tmp_path / "iso.db"
    monkeypatch.setattr(db, "DB", path)
    db.close_all()
    db._init_done.clear()
    db.init_db()
    # A real scoped read is what installs the workspace hooks (they are lazy);
    # calling it here means a test that switches workspaces exercises the wiring
    # rather than racing its installation.
    iso.current()
    iso.invalidate()
    B_.MEM_CACHE.invalidate()
    B_.RULES_CACHE.invalidate()
    yield path
    try:
        W.set_workspace(os.getcwd())
    except Exception:
        pass
    iso.invalidate()
    B_.MEM_CACHE.invalidate()
    B_.RULES_CACHE.invalidate()
    db.close_all()
    db._init_done.clear()


def _legacy_db(tmp_path, monkeypatch):
    """A database in its PRE-migration-17/18 shape, holding one memory and one rule.

    Hand-built with `sqlite3` because `init_db()` cannot produce this state any
    more — the columns are in the fresh-DB DDL as well as in the migrations. This
    is the only way to test the upgrade path rule 22 is about.
    """
    path = tmp_path / "legacy.db"
    con = sqlite3.connect(str(path))
    con.executescript(
        """
        CREATE TABLE memories (
            id         TEXT PRIMARY KEY,
            content    TEXT,
            importance INTEGER DEFAULT 5,
            tags       TEXT DEFAULT '',
            created_at TEXT DEFAULT(datetime('now'))
        );
        CREATE TABLE rules (
            id         TEXT PRIMARY KEY,
            content    TEXT,
            active     INTEGER DEFAULT 1,
            created_at TEXT DEFAULT(datetime('now'))
        );
        """)
    con.execute("INSERT INTO memories(id, content, importance) "
                "VALUES('m-old', 'the API key lives in the vault', 8)")
    con.execute("INSERT INTO rules(id, content, active) "
                "VALUES('r-old', 'always run the linter before committing', 1)")
    con.commit()
    con.close()
    monkeypatch.setattr(db, "DB", path)
    db.close_all()
    db._init_done.clear()
    db.init_db()
    return path


def _cols(table: str) -> dict:
    return {r["name"]: r for r in db.qall(f"PRAGMA table_info({table})")}


# ══════════════════════════════════════════════════════════════════════════════
# The vocabulary
# ══════════════════════════════════════════════════════════════════════════════

def test_shared_is_the_empty_string():
    assert iso.SHARED == "", (
        "`''` is the marker AND the column default — see the module note. A "
        "sentinel like '*' would need a migration to backfill, and any row it "
        "missed would become invisible in every project.")


def test_shared_is_also_the_column_default_on_every_governed_table():
    """⚠️ Rule 22 lives in this default: an unstamped row is visible, never hidden.

    Any writer that reaches these tables without going through `core.memory` /
    `core.rules` — a future surface, a repair script, a hand-run `INSERT` — leaves
    the column alone. It has to land on "everyone sees it".
    """
    db.exe("INSERT INTO memories(id, content) VALUES('m1', 'raw insert')")
    db.exe("INSERT INTO rules(id, content) VALUES('r1', 'raw insert')")
    assert db.qone("SELECT project FROM memories WHERE id='m1'")["project"] == iso.SHARED
    assert db.qone("SELECT project FROM rules WHERE id='r1'")["project"] == iso.SHARED


def test_every_governed_table_carries_the_column_not_null():
    for table in iso.SCOPED_TABLES:
        col = _cols(table).get(iso.COLUMN)
        assert col is not None, f"{table} is declared scoped but has no {iso.COLUMN}"
        assert col["notnull"] == 1, f"{table}.{iso.COLUMN} must be NOT NULL"
        assert col["dflt_value"] in ("''", "\"\""), (
            f"{table}.{iso.COLUMN} default is {col['dflt_value']!r}, not the "
            f"shared marker — every pre-Task-23 row would change visibility")


def test_the_scoped_table_list_names_the_tables_that_actually_have_the_column():
    """The declaration and the schema must agree in both directions.

    A table listed here without the column makes `scope_sql()` produce SQL that
    cannot run; a table with the column that is NOT listed is one nobody knows is
    governed.
    """
    for table in iso.SCOPED_TABLES:
        assert iso.COLUMN in _cols(table)
    assert set(iso.SCOPED_TABLES) == {"memories", "rules"}, (
        "Phase 11's skill state joins this tuple — update the docstring with it")


# ══════════════════════════════════════════════════════════════════════════════
# The policy
# ══════════════════════════════════════════════════════════════════════════════

def test_the_mode_defaults_to_project():
    assert iso.mode() == iso.MODE_PROJECT
    assert iso.enabled() is True
    assert iso.DEFAULT_MODE == iso.MODE_PROJECT


def test_the_mode_is_read_live_from_the_environment(monkeypatch):
    """Read at call time, not at import — a test and `/api/health` must see what
    is actually in force, not what happened to be set when the module loaded."""
    monkeypatch.setenv(iso.ENV_MODE, "off")
    assert iso.mode() == iso.MODE_OFF
    assert iso.enabled() is False
    monkeypatch.setenv(iso.ENV_MODE, "project")
    assert iso.mode() == iso.MODE_PROJECT
    assert iso.enabled() is True


@pytest.mark.parametrize("raw", ["", "  ", "global", "none", "OFFF", "0", "yes",
                                 "disabled", "true"])
def test_an_unrecognised_mode_falls_back_to_project_never_off(monkeypatch, raw):
    """⚠️ The fallback points at the strict end, like `AGENT2_WEB_ROLE`.

    A typo in a launcher script must not silently pool every checkout's memories
    into one store — that failure is invisible until a user reads a fact from a
    project they are not in.
    """
    monkeypatch.setenv(iso.ENV_MODE, raw)
    assert iso.mode() == iso.MODE_PROJECT, f"{raw!r} disabled scoping"
    assert iso.enabled() is True


def test_the_mode_is_accepted_case_and_space_insensitively(monkeypatch):
    monkeypatch.setenv(iso.ENV_MODE, "  OFF ")
    assert iso.mode() == iso.MODE_OFF


def test_the_mode_is_env_only_and_never_a_settings_row():
    """⚠️ A stored "off" is a persistent silent downgrade — see the module note.

    Asserted against the source because there is no state that distinguishes
    "reads a setting we have not written" from "does not read settings at all".
    """
    src = open(iso.__file__, encoding="utf-8").read()
    assert "get_setting" not in src, (
        "isolation must not read a DB setting: an operator who turns scoping off "
        "once would have it off after every restart, with nothing on screen")
    assert "set_setting" not in src


# ══════════════════════════════════════════════════════════════════════════════
# canonical()
# ══════════════════════════════════════════════════════════════════════════════

def test_canonical_delegates_to_project_key(tmp_path):
    """⚠️ Migration 10 exists because a raw path was stored once: a row written
    from `C:\\…` was never found again by a read for `c:\\…`."""
    from agent2.core.context import project_key
    assert iso.canonical(tmp_path) == project_key(str(tmp_path))
    assert iso.canonical(str(tmp_path)) == project_key(str(tmp_path))


@pytest.mark.parametrize("blank", [None, "", "   ", "\t\n"])
def test_blank_input_is_shared_and_never_reaches_project_key(blank):
    """⚠️ LOAD-BEARING SHORT-CIRCUIT. `project_key("")` reads an empty argument as
    "the current directory", so passing blank through would turn "shared" into
    "wherever this process happens to be standing" — and every shared row would
    silently become local to whichever directory the caller was launched from."""
    from agent2.core.context import project_key
    assert iso.canonical(blank) == iso.SHARED
    assert project_key("") != iso.SHARED, (
        "the premise of the short-circuit: project_key('') is the CWD, not ''")


def test_canonical_is_idempotent():
    """A key is written, read back, and re-canonicalised on every comparison."""
    once = iso.canonical(os.getcwd())
    assert iso.canonical(once) == once


@pytest.mark.skipif(os.path.normcase("A") != "a",
                    reason="normcase is identity on this platform")
def test_canonical_folds_case_on_a_case_insensitive_platform():
    p = os.path.join(os.sep, "Proj", "Alpha")
    assert iso.canonical(p.upper()) == iso.canonical(p.lower())


def test_is_shared_is_canonical_against_the_marker():
    assert iso.is_shared("") is True
    assert iso.is_shared(None) is True
    assert iso.is_shared(PROJ_A) is False


# ══════════════════════════════════════════════════════════════════════════════
# current()
# ══════════════════════════════════════════════════════════════════════════════

def test_current_is_the_active_workspace_as_a_project_key(tmp_path):
    ws = tmp_path / "here"
    ws.mkdir()
    W.set_workspace(str(ws))
    try:
        assert iso.current() == iso.canonical(W.root())
    finally:
        W.set_workspace(os.getcwd())


def test_current_is_cached_between_calls():
    """Asked on every scoped read and write, and resolving it stats the root."""
    iso.invalidate()
    first = iso.current()
    assert iso._cached_project == first
    assert iso.current() is iso._cached_project


def test_current_is_total_when_the_workspace_cannot_be_resolved(monkeypatch):
    """A scoping rule that raises would take the turn down with it."""
    import agent2.core.workspace as real_ws

    def _boom():
        raise RuntimeError("no workspace")

    monkeypatch.setattr(real_ws, "root", _boom)
    iso.invalidate()
    assert isinstance(iso.current(), str)


# ══════════════════════════════════════════════════════════════════════════════
# stamp()
# ══════════════════════════════════════════════════════════════════════════════

def test_a_new_row_is_stamped_with_the_current_project():
    assert iso.stamp() == iso.current()


def test_shared_true_stamps_the_shared_marker():
    assert iso.stamp(shared=True) == iso.SHARED


def test_stamp_honours_an_explicit_project():
    assert iso.stamp(project=PROJ_A) == PROJ_A


def test_shared_wins_over_an_explicit_project():
    """"Every project may see this" is not a place — it outranks a target key."""
    assert iso.stamp(shared=True, project=PROJ_A) == iso.SHARED


def test_with_isolation_off_a_new_row_is_stamped_shared(monkeypatch):
    """⚠️ SABOTAGE TARGET. NOT the current project.

    Stamping the project key while the rule is off means that turning isolation
    back on later hides rows the user created under global semantics — absent
    scoping quietly becoming scoping-after-the-fact. Every row written with
    `off` has to stay visible when the mode changes.
    """
    monkeypatch.setenv(iso.ENV_MODE, "off")
    assert iso.stamp() == iso.SHARED
    assert iso.stamp(project=PROJ_A) == iso.SHARED, (
        "an explicit project must not re-enable scoping for one write")


def test_a_memory_written_with_isolation_off_survives_turning_it_on(monkeypatch):
    """The end-to-end form of the rule above, which is the one a user notices."""
    monkeypatch.setenv(iso.ENV_MODE, "off")
    mem.add_memory("written while pooled", importance=6)
    monkeypatch.setenv(iso.ENV_MODE, "project")
    seen = [m["content"] for m in mem.list_memories(project=PROJ_A)]
    assert "written while pooled" in seen, (
        "turning isolation ON hid rows created before it was on")


# ══════════════════════════════════════════════════════════════════════════════
# allows()
# ══════════════════════════════════════════════════════════════════════════════

def test_allows_a_shared_row_from_anywhere():
    assert iso.allows(iso.SHARED, PROJ_A) is True
    assert iso.allows(iso.SHARED, PROJ_B) is True


def test_allows_a_row_this_project_owns():
    assert iso.allows(PROJ_A, PROJ_A) is True


def test_refuses_a_row_owned_by_a_different_project():
    assert iso.allows(PROJ_B, PROJ_A) is False, (
        "the one case that is actually a leak")


@pytest.mark.parametrize("owner", [None, "", "   "])
def test_allows_an_unknown_owner_because_that_is_pre_task_23_data(owner):
    """⚠️ The two fallbacks point in OPPOSITE directions on purpose.

    An unknown owner is history — hiding a user's own rows is worse than showing
    them. Only a *known different* project is refused.
    """
    assert iso.allows(owner, PROJ_A) is True


def test_with_isolation_off_every_row_is_allowed(monkeypatch):
    monkeypatch.setenv(iso.ENV_MODE, "off")
    assert iso.allows(PROJ_B, PROJ_A) is True


def test_allows_defaults_to_the_current_project():
    assert iso.allows(iso.current()) is True
    assert iso.allows(PROJ_A) is False


@pytest.mark.parametrize("junk", [123, 4.5, object(), b"bytes", ["a"]])
def test_allows_is_total(junk):
    """Called from a render path and from the middle of a turn."""
    assert isinstance(iso.allows(junk, PROJ_A), bool)


# ══════════════════════════════════════════════════════════════════════════════
# scope_sql()
# ══════════════════════════════════════════════════════════════════════════════

def test_scope_sql_off_is_the_unscoped_predicate(monkeypatch):
    monkeypatch.setenv(iso.ENV_MODE, "off")
    assert iso.scope_sql() == ("1=1", ())


def test_scope_sql_is_always_valid_sql_in_both_modes(monkeypatch):
    """⚠️ ALWAYS an expression, never '' — a caller interpolates it into a WHERE
    unconditionally instead of building the clause two ways and getting the ANDs
    wrong in one of them."""
    for value in ("project", "off"):
        monkeypatch.setenv(iso.ENV_MODE, value)
        where, params = iso.scope_sql()
        assert where.strip()
        assert where.count("?") == len(params)
        # It runs. That is the whole point of returning `1=1`.
        db.qall(f"SELECT id FROM memories WHERE {where}", params)
        db.qall(f"SELECT id FROM rules WHERE {where}", params)


def test_the_predicate_names_only_the_declared_column():
    where, params = iso.scope_sql(PROJ_A)
    assert where == (f"({iso.COLUMN}=? OR {iso.COLUMN}=? OR {iso.COLUMN} IS NULL)")
    assert params == (PROJ_A, iso.SHARED)


def test_the_predicate_matches_mine_and_shared_and_null():
    """The `IS NULL` arm needs a table that permits NULL — the governed tables do
    not, which is exactly why the arm is there: it covers a row an *older* writer
    left NULL on a DB whose column arrived by ALTER rather than by CREATE."""
    db.exe("CREATE TABLE t_scope (id TEXT, project TEXT)")
    db.exe("INSERT INTO t_scope(id, project) VALUES('mine', ?)", (PROJ_A,))
    db.exe("INSERT INTO t_scope(id, project) VALUES('shared', '')")
    db.exe("INSERT INTO t_scope(id, project) VALUES('legacy', NULL)")
    db.exe("INSERT INTO t_scope(id, project) VALUES('theirs', ?)", (PROJ_B,))
    where, params = iso.scope_sql(PROJ_A)
    got = {r["id"] for r in db.qall(f"SELECT id FROM t_scope WHERE {where}", params)}
    assert got == {"mine", "shared", "legacy"}


def test_scope_sql_falls_back_to_unscoped_when_the_project_is_unknown(monkeypatch):
    """"We cannot tell where we are" must show more, not less — an empty memory
    list looks exactly like data loss, and a `clear` run against it is."""
    monkeypatch.setattr(iso, "current", lambda: "")
    assert iso.scope_sql() == ("1=1", ())


# ══════════════════════════════════════════════════════════════════════════════
# The schema (rules 22 and 23)
# ══════════════════════════════════════════════════════════════════════════════

def test_the_ledger_records_migrations_17_and_18():
    rows = {r["version"]: r["name"] for r in
            db.qall("SELECT version, name FROM schema_migrations")}
    assert rows.get(17) == "memories.project"
    assert rows.get(18) == "rules.project"
    assert db.schema_version() == db.SCHEMA_VERSION >= 18


def test_the_scoped_indexes_exist_and_the_unscoped_pair_is_kept():
    """⚠️ KEPT, not replaced: `AGENT2_CONTEXT_ISOLATION=off` still issues the
    unscoped query, and `_apply_migrations()` runs BEFORE `_INDEXES`, so a DB that
    stopped at 16 for any reason would otherwise lose an index it still uses."""
    names = {r["name"] for r in
             db.qall("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"idx_memories_project_rank", "idx_rules_project_active"} <= names
    assert {"idx_memories_rank", "idx_rules_active"} <= names


def test_a_pre_task_23_database_upgrades_with_every_row_shared(tmp_path, monkeypatch):
    """⚠️ SABOTAGE TARGET — this is rule 22 for Task 23.

    Defaulting to "whichever project happened to be open when the migration ran"
    would have made a user's entire memory store and every standing rule vanish
    from every other checkout, with no error and nothing to point at.
    """
    _legacy_db(tmp_path, monkeypatch)
    assert db.schema_version() == db.SCHEMA_VERSION
    assert iso.COLUMN in _cols("memories")
    assert iso.COLUMN in _cols("rules")
    assert db.qone("SELECT project FROM memories WHERE id='m-old'")["project"] == ""
    assert db.qone("SELECT project FROM rules WHERE id='r-old'")["project"] == ""


def test_a_legacy_row_stays_visible_from_every_project(tmp_path, monkeypatch):
    """The observable half of the test above: not just `''` in a column, but still
    in the prompt, from a checkout that has never been opened before."""
    _legacy_db(tmp_path, monkeypatch)
    for project in (PROJ_A, PROJ_B, iso.current()):
        seen = [m["content"] for m in mem.list_memories(project=project)]
        assert "the API key lives in the vault" in seen, f"lost in {project}"
        active = [r["content"] for r in rul.list_rules(active_only=True,
                                                       project=project)]
        assert "always run the linter before committing" in active, (
            f"a standing rule stopped applying in {project}")
    assert "the API key lives in the vault" in mem.memory_prompt_block(project=PROJ_A)
    assert "always run the linter" in rul.rules_prompt_block(project=PROJ_A)


# ══════════════════════════════════════════════════════════════════════════════
# Memories
# ══════════════════════════════════════════════════════════════════════════════

def test_a_memory_written_in_one_project_is_invisible_in_another():
    mem.add_memory("bravo uses postgres", importance=7, project=PROJ_B)
    assert [m["content"] for m in mem.list_memories(project=PROJ_B)] == \
        ["bravo uses postgres"]
    assert mem.list_memories(project=PROJ_A) == [], (
        "the leak: alpha was told a fact that belongs to bravo")


def test_a_shared_memory_is_visible_from_every_project():
    """"Unless explicitly shared" — this is the other half of the sentence."""
    mem.add_memory("the vault lives at vault.internal", importance=9, shared=True)
    for project in (PROJ_A, PROJ_B, iso.current()):
        seen = [m["content"] for m in mem.list_memories(project=project)]
        assert "the vault lives at vault.internal" in seen, f"missing in {project}"


def test_top_memories_and_count_memories_are_both_scoped():
    """⚠️ The count has to be scoped too, or the prompt block lies — see below."""
    mem.add_memory("bravo fact", importance=9, project=PROJ_B)
    mem.add_memory("alpha fact", importance=9, project=PROJ_A)
    assert [m["content"] for m in mem.top_memories(project=PROJ_A)] == ["alpha fact"]
    assert mem.count_memories(project=PROJ_A) == 1
    assert mem.count_memories(project=PROJ_B) == 1


def test_the_memories_prompt_block_carries_only_visible_memories():
    mem.add_memory("bravo's schema is sharded", importance=9, project=PROJ_B)
    mem.add_memory("alpha's schema is flat", importance=9, project=PROJ_A)
    block = mem.memory_prompt_block(project=PROJ_A)
    assert "alpha's schema is flat" in block
    assert "bravo's schema is sharded" not in block, (
        "the leak reaches the model: the block IS the prompt")


def test_the_withheld_count_only_counts_memories_this_project_can_see():
    """An unscoped `count_memories` would announce another project's rows as
    "not shown", and the model would keep asking for facts this checkout does
    not have."""
    for i in range(3):
        mem.add_memory(f"bravo fact {i}", importance=5, project=PROJ_B)
    for i in range(3):
        mem.add_memory(f"alpha fact {i}", importance=5, project=PROJ_A)
    block = mem.memory_prompt_block(limit=1, project=PROJ_A)
    assert "(+2 older" in block, block


def test_the_dedup_cannot_swallow_another_projects_save():
    """⚠️ SABOTAGE TARGET — the subtle half of the rule.

    An unscoped `WHERE content=?` finds project B's identical row, "reinforces"
    it and returns it: A's save reports success, writes nothing A can see, and
    the memory is gone from A forever with no error anywhere.
    """
    theirs = mem.add_memory("deploys run from the release branch", project=PROJ_B)
    mine = mem.add_memory("deploys run from the release branch", project=PROJ_A)
    assert mine is not None, "the save reported failure"
    assert mine["id"] != theirs["id"], (
        "alpha's save reinforced bravo's row — successful call, lost memory")
    assert mine["project"] == PROJ_A
    assert "deploys run from the release branch" in \
        [m["content"] for m in mem.list_memories(project=PROJ_A)]


def test_the_dedup_prefers_this_projects_own_row_over_a_shared_one():
    """Both rows are visible here, so ordering is what decides which is
    reinforced — re-saving in alpha must not quietly raise the shared row's
    importance and leave alpha's own row behind.

    Hand-inserted because `add_memory` cannot produce this state: the second
    call would dedup onto the first.
    """
    db.exe("INSERT INTO memories(id, content, importance, project, created_at) "
           "VALUES('m-shared', 'the CI runner is self-hosted', 4, '', "
           "datetime('now','-2 days'))")
    db.exe("INSERT INTO memories(id, content, importance, project, created_at) "
           "VALUES('m-mine', 'the CI runner is self-hosted', 4, ?, "
           "datetime('now','-1 days'))", (PROJ_A,))
    got = mem.add_memory("the CI runner is self-hosted", importance=9,
                         project=PROJ_A)
    assert got["id"] == "m-mine", "the older shared row won on created_at alone"
    assert db.qone("SELECT importance FROM memories WHERE id='m-shared'"
                   )["importance"] == 4


def test_saving_shared_promotes_the_projects_existing_row():
    """⚠️ The one direction visibility moves implicitly — a second row with
    identical content would render twice in every prompt."""
    row = mem.add_memory("the vault is rotated monthly", project=PROJ_A)
    promoted = mem.add_memory("the vault is rotated monthly", shared=True,
                              project=PROJ_A)
    assert promoted["id"] == row["id"]
    assert promoted["project"] == iso.SHARED
    assert "the vault is rotated monthly" in \
        [m["content"] for m in mem.list_memories(project=PROJ_B)]


def test_an_ordinary_save_never_narrows_a_shared_memory():
    """The reverse never happens: a fact the user made global stays global."""
    shared = mem.add_memory("licences renew in March", shared=True)
    again = mem.add_memory("licences renew in March", project=PROJ_A)
    assert again["id"] == shared["id"]
    assert again["project"] == iso.SHARED, (
        "an ordinary save in alpha took a shared memory away from bravo")


def test_share_memory_is_scoped_in_both_directions():
    mine = mem.add_memory("staging sits behind the VPN", project=PROJ_A)
    mem.share_memory(mine["id"], True, project=PROJ_A)
    assert "staging sits behind the VPN" in \
        [m["content"] for m in mem.list_memories(project=PROJ_B)]

    theirs = mem.add_memory("bravo's own secret", project=PROJ_B)
    mem.share_memory(theirs["id"], True, project=PROJ_A)
    assert db.qone("SELECT project FROM memories WHERE id=?",
                   (theirs["id"],))["project"] == PROJ_B, (
        "alpha published bravo's memory to every checkout")


def test_delete_memory_cannot_reach_another_projects_row():
    row = mem.add_memory("bravo keeps its own notes", project=PROJ_B)
    mem.delete_memory(row["id"], project=PROJ_A)
    assert db.qone("SELECT id FROM memories WHERE id=?", (row["id"],)) is not None, (
        "reading another project's memory is a leak; deleting it is data loss")
    mem.delete_memory(row["id"], project=PROJ_B)
    assert db.qone("SELECT id FROM memories WHERE id=?", (row["id"],)) is None


def test_bulk_delete_is_scoped_even_though_its_count_is_not_the_proof():
    """⚠️ `delete_memories()` returns the number REQUESTED, not the number
    removed — so the rows have to be the assertion, not the return value."""
    theirs = mem.add_memory("bravo's deploy key", project=PROJ_B)
    mine = mem.add_memory("alpha's deploy key", project=PROJ_A)
    mem.delete_memories([theirs["id"], mine["id"]], project=PROJ_A)
    assert db.qone("SELECT id FROM memories WHERE id=?",
                   (theirs["id"],)) is not None
    assert db.qone("SELECT id FROM memories WHERE id=?", (mine["id"],)) is None


def test_clear_memories_cannot_empty_another_projects_store():
    """⚠️ SABOTAGE TARGET — this one is data loss, not a leak (rule 22)."""
    mem.add_memory("bravo fact one", project=PROJ_B)
    mem.add_memory("bravo fact two", project=PROJ_B)
    mem.add_memory("alpha fact", project=PROJ_A)
    removed = mem.clear_memories(project=PROJ_A)
    assert removed == 1, "the count must be what it actually deleted"
    assert len(mem.list_memories(project=PROJ_B)) == 2, (
        "a `clear` in the wrong checkout emptied the right one")


def test_prune_cannot_prune_another_projects_memories():
    for i in range(4):
        mem.add_memory(f"bravo note {i}", importance=1, project=PROJ_B)
    mem.add_memory("alpha note", importance=1, project=PROJ_A)
    removed = mem.prune_memories(keep=0, min_importance=1, project=PROJ_A)
    assert removed == 1
    assert len(mem.list_memories(project=PROJ_B)) == 4


# ══════════════════════════════════════════════════════════════════════════════
# Rules
# ══════════════════════════════════════════════════════════════════════════════

def test_a_rule_written_in_one_project_does_not_apply_in_another():
    rul.add_rule("always deploy with bravo's release script", project=PROJ_B)
    assert [r["content"] for r in rul.list_rules(project=PROJ_B)] == \
        ["always deploy with bravo's release script"]
    assert rul.list_rules(project=PROJ_A) == [], (
        "an instruction nobody in this checkout ever wrote")


def test_a_shared_rule_applies_everywhere():
    rul.add_rule("never commit to main", shared=True)
    for project in (PROJ_A, PROJ_B, iso.current()):
        active = [r["content"] for r in rul.list_rules(active_only=True,
                                                       project=project)]
        assert "never commit to main" in active, f"stopped applying in {project}"


def test_the_rules_prompt_block_carries_only_visible_rules():
    rul.add_rule("bravo: never touch main", project=PROJ_B)
    rul.add_rule("alpha: squash before merging", project=PROJ_A)
    block = rul.rules_prompt_block(project=PROJ_A)
    assert "squash before merging" in block
    assert "never touch main" not in block, (
        "a leaked rule silently changes how the model behaves here")


def test_toggle_rule_from_another_project_changes_nothing_and_leaks_nothing():
    """⚠️ SABOTAGE TARGET. Two halves, and the second is the one that regressed:
    `PUT /api/rules/<rid>` used to answer from its own unscoped `SELECT`, so a
    toggle this module correctly refused still handed the caller the other
    project's rule text."""
    row = rul.add_rule("bravo: run the migration first", project=PROJ_B)
    assert rul.toggle_rule(row["id"], project=PROJ_A) is None, (
        "the refused toggle returned the foreign rule's content")
    assert db.qone("SELECT active FROM rules WHERE id=?",
                   (row["id"],))["active"] == 1, "alpha disabled bravo's rule"
    flipped = rul.toggle_rule(row["id"], project=PROJ_B)
    assert flipped is not None and flipped["active"] == 0


def test_delete_rule_cannot_reach_another_projects_rule():
    row = rul.add_rule("bravo: tag every release", project=PROJ_B)
    rul.delete_rule(row["id"], project=PROJ_A)
    assert db.qone("SELECT id FROM rules WHERE id=?", (row["id"],)) is not None
    rul.delete_rule(row["id"], project=PROJ_B)
    assert db.qone("SELECT id FROM rules WHERE id=?", (row["id"],)) is None


def test_bulk_rule_delete_is_scoped():
    theirs = rul.add_rule("bravo: freeze on Fridays", project=PROJ_B)
    mine = rul.add_rule("alpha: freeze on Fridays", project=PROJ_A)
    rul.delete_rules([theirs["id"], mine["id"]], project=PROJ_A)
    assert db.qone("SELECT id FROM rules WHERE id=?",
                   (theirs["id"],)) is not None
    assert db.qone("SELECT id FROM rules WHERE id=?", (mine["id"],)) is None


def test_set_rules_active_cannot_deactivate_another_projects_rule():
    """The bulk write is scoped too — `/rules` sends both arms uniformly."""
    theirs = rul.add_rule("bravo: run the migration first", project=PROJ_B)
    mine = rul.add_rule("alpha: run the linter first", project=PROJ_A)
    rul.set_rules_active([theirs["id"], mine["id"]], False, project=PROJ_A)
    assert db.qone("SELECT active FROM rules WHERE id=?",
                   (theirs["id"],))["active"] == 1
    assert db.qone("SELECT active FROM rules WHERE id=?",
                   (mine["id"],))["active"] == 0


def test_share_rule_is_scoped_in_both_directions():
    mine = rul.add_rule("alpha: sign every commit", project=PROJ_A)
    rul.share_rule(mine["id"], True, project=PROJ_A)
    assert "alpha: sign every commit" in \
        [r["content"] for r in rul.list_rules(project=PROJ_B)]

    theirs = rul.add_rule("bravo: skip the linter", project=PROJ_B)
    rul.share_rule(theirs["id"], True, project=PROJ_A)
    assert db.qone("SELECT project FROM rules WHERE id=?",
                   (theirs["id"],))["project"] == PROJ_B, (
        "alpha made bravo's instruction apply to every checkout")


def test_with_isolation_off_both_tables_are_one_pooled_store(monkeypatch):
    """Rule 8 — the pre-Task-23 behaviour is still available, whole."""
    monkeypatch.setenv(iso.ENV_MODE, "off")
    mem.add_memory("pooled fact", project=PROJ_B)
    rul.add_rule("pooled rule", project=PROJ_B)
    assert "pooled fact" in [m["content"] for m in mem.list_memories(project=PROJ_A)]
    assert "pooled rule" in [r["content"] for r in rul.list_rules(project=PROJ_A)]


# ══════════════════════════════════════════════════════════════════════════════
# Task results — the fourth thing the rule names
# ══════════════════════════════════════════════════════════════════════════════

class _FakeTasks:
    """Just enough of `core.tasks` for `_session_visible`, which takes it as an
    argument precisely so this check is testable without a task session."""

    def __init__(self, row):
        self._row = row

    def get_session(self, _sid):
        return self._row


def test_a_task_session_from_another_workspace_is_refused():
    """⚠️ A chat id is NOT project-scoped: the same chat reopens anywhere, and
    `latest_session_for_chat` happily returns the session it ran in another
    checkout. Left unchecked, project B's plan and completed-step summaries
    arrive here as this project's standing state."""
    req = B_.request(project=PROJ_A)
    assert B_._session_visible("s1", req,
                               _FakeTasks({"id": "s1", "cwd": PROJ_B})) is False


def test_a_task_session_in_this_project_is_visible():
    req = B_.request(project=PROJ_A)
    assert B_._session_visible("s1", req,
                               _FakeTasks({"id": "s1", "cwd": PROJ_A})) is True


@pytest.mark.parametrize("cwd", [None, "", "   "])
def test_a_session_that_cannot_be_placed_is_treated_as_shared(cwd):
    """Fail-open, like `allows()` — an unknown owner is pre-Task-23 data or a
    hand-made row, and only a KNOWN different project is rejected."""
    req = B_.request(project=PROJ_A)
    assert B_._session_visible("s1", req, _FakeTasks({"cwd": cwd})) is True


def test_session_visible_is_total_when_the_lookup_raises():
    class _Boom:
        def get_session(self, _sid):
            raise RuntimeError("database down")

    req = B_.request(project=PROJ_A)
    assert B_._session_visible("s1", req, _Boom()) is True


def test_the_tasks_collector_emits_nothing_for_a_foreign_session():
    """The end-to-end form: a real session, a real completed step, and the
    result text that must not reach the other project's prompt."""
    from agent2.core import tasks as T
    sid = T.open_session(chat_id="c1", cwd=os.path.join(os.sep, "proj", "bravo"))
    step = T.create(sid, "run the deploy")
    T.complete(step.id, "deployed to bravo")

    assert B_._collect_tasks(B_.request(project=PROJ_A, session_id=sid)) == []
    items = B_._collect_tasks(B_.request(project=PROJ_B, session_id=sid))
    assert items and "deployed to bravo" in items[0].text, (
        "the owning project must still get its own plan")


# ══════════════════════════════════════════════════════════════════════════════
# Invalidation — the leak that arrives through the cache
# ══════════════════════════════════════════════════════════════════════════════

def _two_workspaces(tmp_path):
    a, b = tmp_path / "alpha", tmp_path / "bravo"
    a.mkdir()
    b.mkdir()
    return a, b


def test_a_workspace_switch_rebuilds_the_memories_block(tmp_path):
    """⚠️ SABOTAGE TARGET — and the sneakiest failure in this file.

    `MEM_CACHE` is process-global and keyed on the `memories` resource. Nothing
    about switching workspace bumps that resource, so without the
    `on_invalidate` registration every query below is correctly scoped and the
    leak still happens — through the cache, serving alpha's memories to bravo.
    """
    a, b = _two_workspaces(tmp_path)
    W.set_workspace(str(a))
    iso.invalidate()
    mem.add_memory("alpha's deploy runs on Fridays", importance=8)
    assert "alpha's deploy runs on Fridays" in B_.memory_block()

    W.set_workspace(str(b))          # nothing here notifies `memories`
    assert "alpha's deploy runs on Fridays" not in B_.memory_block(), (
        "the cached block outlived the workspace it belonged to")


def test_a_workspace_switch_rebuilds_the_rules_block(tmp_path):
    """⚠️ SABOTAGE TARGET. Same mechanism, worse consequence: a stale rules
    block keeps *instructing* the model with another project's standing orders."""
    a, b = _two_workspaces(tmp_path)
    W.set_workspace(str(a))
    iso.invalidate()
    rul.add_rule("alpha: never push without review")
    assert "alpha: never push without review" in B_.rules_block()

    W.set_workspace(str(b))
    assert "alpha: never push without review" not in B_.rules_block(), (
        "bravo is still being told to follow alpha's rule")


def test_a_workspace_switch_forgets_the_cached_project_key(tmp_path):
    a, b = _two_workspaces(tmp_path)
    W.set_workspace(str(a))
    assert iso.current() == iso.canonical(a)
    W.set_workspace(str(b))
    assert iso.current() == iso.canonical(b), "the key survived its own switch"


def test_the_broker_registered_its_two_caches_as_one_listener():
    assert B_._on_project_change in iso._listeners


def test_on_invalidate_dedups_so_an_import_cannot_double_fire_it():
    def _noop():
        pass

    iso.on_invalidate(_noop)
    iso.on_invalidate(_noop)
    try:
        assert iso._listeners.count(_noop) == 1
    finally:
        iso._listeners.remove(_noop)


def test_a_broken_listener_never_breaks_a_workspace_switch():
    """A scoping rule that raises would take the turn down with it."""
    def _boom():
        raise RuntimeError("listener exploded")

    iso.on_invalidate(_boom)
    try:
        iso.invalidate()                     # must not raise
        assert iso._cached_project is None
        assert isinstance(iso.current(), str)
    finally:
        iso._listeners.remove(_boom)


def test_the_workspace_sync_topic_is_one_the_poller_republishes():
    """The cross-process half: `invalidate()` is subscribed to `workspace`, and
    a topic missing from `RESOURCES` reaches the other surface never."""
    assert "workspace" in S.RESOURCES


# ══════════════════════════════════════════════════════════════════════════════
# One declaration
# ══════════════════════════════════════════════════════════════════════════════

def test_the_brokers_project_key_is_isolation_current():
    """Two copies of "which project am I in" is the drift this module ends."""
    assert B_._default_project() == iso.current()
    assert B_.request().project == iso.current()


def test_neither_scoped_module_carries_a_predicate_of_its_own():
    """Asserted against the source: a second predicate would keep working and
    disagree only in the cases nobody tests."""
    for module in (mem, rul):
        src = open(module.__file__, encoding="utf-8").read()
        assert "scope_sql" in src, f"{module.__name__} does not ask isolation"
        assert f"({iso.COLUMN}=?" not in src, (
            f"{module.__name__} builds its own scope predicate")


def test_describe_reports_the_policy_and_never_the_project_path():
    """⚠️ `/api/health` is counters-and-policy; the absolute path of a user's
    working directory is neither."""
    d = iso.describe()
    assert d["mode"] == iso.mode()
    assert d["enabled"] is iso.enabled()
    assert d["env"] == iso.ENV_MODE
    assert d["column"] == iso.COLUMN
    assert d["shared_marker"] == iso.SHARED
    assert d["scoped"] == list(iso.SCOPED_TABLES)
    here = iso.current()
    assert not here or here not in repr(d)


def test_the_broker_reports_the_live_isolation_policy(monkeypatch):
    monkeypatch.setenv(iso.ENV_MODE, "off")
    assert B_.stats()["isolation"]["mode"] == iso.MODE_OFF
