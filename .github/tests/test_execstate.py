# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
Tests for the durable execution ledger (``agent2/core/execstate.py``, Task 24).

Run from the repo root:  python -m pytest .github/tests/test_execstate.py -v

What this suite is for
──────────────────────
Everything Agent2 knew about what it was doing lived in memory — `core/commands.py`
holds a command's lifecycle, `ToolContext.note_tool` records a sub-step — and none
of it survives `kill -9`. `execstate` is the durable shadow of those registries,
and the question it exists to answer is asked by a DIFFERENT process: *what was in
flight when this stopped, and did it finish?*

So the tests here are written as **two processes**. The "first" one writes rows
under a monkeypatched ``INSTANCE``; the "second" one is the real module state, and
what it can conclude from the rows alone is the whole contract. Ageing a row's
``updated_at`` stands in for wall-clock time, which is what makes the scenarios
deterministic — spec 26 (Testing strategy) asks for exactly that rather than
"kill Agent2 by hand and see".

The load-bearing tests
──────────────────────
* ``test_this_processes_own_row_is_never_reported_as_a_crash`` and
  ``test_a_fresh_foreign_row_is_not_reported_as_a_crash`` — the two halves of the
  crash predicate. Drop the instance test and every surface reports its own
  in-flight work as a crash at every scan (in dual mode, twice); drop the
  staleness test and a `sleep 300` reads as interrupted while it is still running.
  Each is written as a PAIR — the row that must not be reported, then the same row
  mutated so that it must be — because either assertion alone would also pass
  against a reader that simply found nothing.
* ``test_interrupted_is_idempotent_across_a_sweep`` — `INTERRUPTED` is *settled*
  for the trim and *unsettled* for `interrupted()`. One set for both would make a
  swept crash invisible to the very reader that just reported it.
* ``test_a_status_change_is_never_throttled`` — the throttle exists for
  `heartbeat()`, which fires per output LINE; if it could also swallow a settle,
  every finished command would read as interrupted at the next launch.
* ``test_an_unfinished_tool_call_has_a_null_post`` — a `post` of `null` IS the
  crash signal. An empty dict would be indistinguishable from "finished, touched
  nothing".
* ``test_no_command_output_ever_reaches_the_ledger`` and
  ``test_a_content_bearing_argument_never_reaches_the_detail_column`` — the ledger
  is written on a path that has just proven the machine is in an unknown state,
  and it may never become the place credentials leak to.
* ``test_the_trim_never_evicts_an_unsettled_row`` — the one row a crash makes
  precious is exactly the one an unconditional trim would delete.
"""

import hashlib
import json
import os
import time

import pytest

from agent2 import config as cfg
from agent2 import database as db
from agent2.core import commands as C
from agent2.core import context as _ctx
from agent2.core import execstate as X
from agent2.core import recovery as _recovery
from agent2.core import tasks as T
from agent2.core import workspace

DEAD = "99999-deadbeef"          # an instance id no live process can own

# A value that must never appear in any row. Written as one token so a single
# `json.dumps(row)` scan can prove it.
SECRET = "sk-live-DO-NOT-PERSIST-4f2b91"

EXEC_TABLES = ("exec_commands", "exec_tool_calls", "exec_workflows")


@pytest.fixture(autouse=True)
def _clean():
    """The ledger is process-global and file-backed — clear both halves.

    Rows are deleted rather than the file replaced: these tests share the session
    database (conftest points `AGENT2_DB` at a temp file), and other suites write
    exec rows as a side effect of exercising `commands.py` and `dispatch_tool`.
    """
    db.init_db()
    X.reset()
    C.reset()
    for table in EXEC_TABLES:
        db.exe(f"DELETE FROM {table}")
    X._failure_reported = False
    yield
    X.reset()
    C.reset()
    for table in EXEC_TABLES:
        db.exe(f"DELETE FROM {table}")
    X._failure_reported = False


# ── Helpers ───────────────────────────────────────────────────────────────────

def _stamp(ago: float = 0.0) -> str:
    """A `core/tasks._now()`-shaped UTC stamp *ago* seconds in the past."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - ago))


def _age(table: str, row_id: str, seconds: float = 3600.0) -> str:
    """Backdate one row's `updated_at`. Stands in for the passage of time."""
    stamp = _stamp(seconds)
    db.exe(f"UPDATE {table} SET updated_at=? WHERE id=?", (stamp, row_id))
    return stamp


def _orphan(table: str, row_id: str, *, instance: str = DEAD,
            seconds: float = 3600.0) -> None:
    """Make a row look like work a dead process left behind."""
    _age(table, row_id, seconds)
    db.exe(f"UPDATE {table} SET instance=? WHERE id=?", (instance, row_id))


def _row(table: str, row_id: str) -> dict | None:
    return db.qone(f"SELECT * FROM {table} WHERE id=?", (row_id,))


def _sha(path) -> str:
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def _ids(rows) -> set:
    return {str(r.get("id") or "") for r in rows}


def _insert_tool_row(row_id: str, *, status: str, updated_at: str,
                     project: str = "", tool: str = "read_file") -> None:
    """A ledger row written straight to storage — for the trim tests, which need
    dozens of rows with controlled ages and must not have `_trim` firing between
    each insert."""
    db.exe(
        "INSERT INTO exec_tool_calls(id, instance, project, tool, status,"
        " started_at, updated_at) VALUES(?,?,?,?,?,?,?)",
        (row_id, DEAD, project, tool, status, updated_at, updated_at),
    )


# ── Identity and vocabulary ───────────────────────────────────────────────────

def test_instance_names_this_process_and_is_not_just_its_pid():
    """⚠️ pid alone is not enough: pids are recycled, and a container that
    restarts often reuses pid 1 — so two "different" processes would claim the
    same instance and neither could tell its own abandoned rows from the other's.
    """
    assert X.INSTANCE.startswith(f"{os.getpid()}-")
    suffix = X.INSTANCE.split("-", 1)[1]
    assert len(suffix) == 8 and suffix != "0" * 8
    # And it is stable within the process — a per-call value would make every row
    # this process wrote look foreign to it a moment later.
    assert X.INSTANCE == X.INSTANCE


def test_statuses_are_reused_from_their_owners_not_redeclared():
    """One new word (`interrupted`) is added, and only here.

    Two spellings of "completed" would make a joined recovery report contradict
    itself, which is why the command table speaks `CommandStatus` and the tool /
    workflow tables speak `tasks.STEP_*`.
    """
    assert X.INTERRUPTED == "interrupted"
    assert X._TERMINAL["exec_commands"] == frozenset(C.TERMINAL)
    assert X._TERMINAL["exec_tool_calls"] == frozenset(
        (T.STEP_COMPLETED, T.STEP_FAILED))
    assert T.STEP_COMPLETED in X._TERMINAL["exec_workflows"]
    assert X.TABLES == EXEC_TABLES


def test_interrupted_is_settled_for_the_trim_and_unsettled_for_the_reader():
    """⚠️ THE SPLIT BETWEEN `_TERMINAL` AND `_SETTLED`, stated as an assertion.

    A swept row must stay findable by the reader that reported it (so the sweep is
    idempotent) while becoming eligible for the trim (so the table does not grow
    without bound). One set could not express both.
    """
    for table in EXEC_TABLES:
        assert X.INTERRUPTED not in X._TERMINAL[table], table
        assert X.INTERRUPTED in X._SETTLED[table], table
        assert X._TERMINAL[table] < X._SETTLED[table], table


def test_the_scope_filters_name_columns_each_table_really_has():
    """⚠️ `exec_commands` has no `chat_id` and `exec_workflows` has no `task_id`.

    Asking for a column a table lacks is a hard SQLite error, and `_rows()` is
    documented as total — so the generic reader consults `_FILTERS` instead of
    assuming a uniform shape. A wrong entry here turns every scoped read into an
    empty list, silently.
    """
    for table, cols in X._FILTERS.items():
        real = {r["name"] for r in db.qall(f"PRAGMA table_info({table})")}
        assert set(cols) <= real, f"{table} has no {set(cols) - real}"
    assert "chat_id" not in X._FILTERS["exec_commands"]
    assert "task_id" not in X._FILTERS["exec_workflows"]


def test_the_project_default_is_the_strict_direction():
    """A reader with no `project=` sees THIS project; `ANY_PROJECT` lifts it.

    The same direction an unknown `AGENT2_WEB_ROLE` falls in — a reader that
    pooled every checkout by default would break project isolation for every
    caller that simply forgot the argument.
    """
    assert X.ANY_PROJECT == "*"
    cmd = C.create("echo scoped")
    assert cmd.id in _ids(X.commands())
    assert cmd.id in _ids(X.commands(project=X.ANY_PROJECT))
    assert cmd.id not in _ids(X.commands(project="Z:/somewhere-else"))


# ── Commands: the mirror of the lifecycle ─────────────────────────────────────

def test_creating_a_command_writes_a_durable_row_immediately():
    """CREATED is persisted, not just RUNNING.

    `create()` runs BEFORE the `Popen`, so a spawn that dies with the process
    still leaves something a restart can see.
    """
    cmd = C.create("echo hello", task_id="t1", session_id="s1", surface="cli")
    row = X.command(cmd.id)
    assert row is not None
    assert row["status"] == C.CommandStatus.CREATED
    assert row["command"] == "echo hello"
    assert row["task_id"] == "t1" and row["session_id"] == "s1"
    assert row["instance"] == X.INSTANCE
    assert row["created_at"]


def test_every_lifecycle_edge_is_persisted_into_one_row():
    """CREATED → STARTING → RUNNING → STREAMING → COMPLETED, upserted by id.

    One row per execution whatever the heartbeat count — the primary key is what
    keeps a `make -j8` from writing a row per output line.
    """
    cmd = C.create("build")
    C.starting(cmd.id)
    assert X.command(cmd.id)["status"] == C.CommandStatus.STARTING
    C.start(cmd.id, process_id=4242)
    started = X.command(cmd.id)
    assert started["status"] == C.CommandStatus.RUNNING
    assert started["process_id"] == 4242
    assert started["started_at"]
    C.heartbeat(cmd.id)
    assert X.command(cmd.id)["status"] == C.CommandStatus.STREAMING
    C.complete(cmd.id, 0)
    done = X.command(cmd.id)
    assert done["status"] == C.CommandStatus.COMPLETED
    assert done["exit_code"] == 0
    assert done["completed_at"]
    assert db.qone("SELECT COUNT(*) AS n FROM exec_commands "
                   "WHERE id=?", (cmd.id,))["n"] == 1


def test_a_failure_records_the_verdict_and_the_exit_code():
    cmd = C.create("false")
    C.start(cmd.id, process_id=1)
    C.complete(cmd.id, 1)
    row = X.command(cmd.id)
    assert row["status"] == C.CommandStatus.FAILED
    assert row["exit_code"] == 1


@pytest.mark.parametrize("settle,status", [
    (lambda cid: C.cancel(cid), C.CommandStatus.CANCELLED),
    (lambda cid: C.timed_out(cid), C.CommandStatus.TIMEOUT),
    (lambda cid: C.killed(cid), C.CommandStatus.KILLED),
    (lambda cid: C.fail(cid, "no such shell"), C.CommandStatus.FAILED),
])
def test_every_failure_verdict_reaches_the_ledger(settle, status):
    """All four failure states, not just the happy path — spec 25.2 says a
    CANCELLED execution must never be recovered, and it can only be excluded if
    the word reached the row."""
    cmd = C.create("sleep 5")
    C.start(cmd.id, process_id=2)
    settle(cmd.id)
    assert X.command(cmd.id)["status"] == status


def test_a_status_change_is_never_throttled():
    """⚠️ THE LIFECYCLE EDGES ARE ALWAYS WRITTEN, whatever `EXEC_BEAT_SEC` says.

    `config.py` states this as a contract, and it is the difference between a
    ledger and a sampler: if a settle could be throttled away, every finished
    command would still read RUNNING at the next launch and recovery would offer
    to repeat work that completed.
    """
    cfg_beat = 10_000.0
    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(cfg, "EXEC_BEAT_SEC", cfg_beat)
        cmd = C.create("edge")
        C.start(cmd.id, process_id=3)
        C.heartbeat(cmd.id)
        C.complete(cmd.id, 0)
    row = X.command(cmd.id)
    assert row["status"] == C.CommandStatus.COMPLETED
    assert row["exit_code"] == 0


def test_a_repeat_of_the_same_status_is_throttled(monkeypatch):
    """The other direction: `heartbeat()` fires once per output LINE.

    Without the throttle a `make -j8` writes one UPSERT per line of build output,
    on a path whose own docstring promises *"nothing inside the lock allocates,
    logs, notifies or touches the DB"*. The registry keeps counting; only the
    durable mirror lags — and the settle below reconciles it, which is why lagging
    is acceptable and losing the settle would not be.
    """
    monkeypatch.setattr(cfg, "EXEC_BEAT_SEC", 10_000.0)
    cmd = C.create("make -j8")
    C.start(cmd.id, process_id=11)
    C.heartbeat(cmd.id)                     # RUNNING → STREAMING: a change, written
    assert X.command(cmd.id)["output_lines"] == 1
    for _ in range(9):
        C.heartbeat(cmd.id)                 # same status, immediately: throttled
    assert C.get(cmd.id).output_lines == 10, "the registry must still be counting"
    assert X.command(cmd.id)["output_lines"] == 1

    C.complete(cmd.id, 0)                   # a settle is an edge — never throttled
    done = X.command(cmd.id)
    assert done["status"] == C.CommandStatus.COMPLETED
    assert done["output_lines"] == 10


def test_a_multiline_error_is_flattened_to_one_line():
    """`error` holds our own short reason, one line, capped.

    A spawn failure's message can carry a traceback or a shell's stderr, and
    neither belongs in the database (DDL invariant 3).
    """
    cmd = C.create("bad-shell")
    C.start(cmd.id, process_id=4)
    C.fail(cmd.id, "spawn failed\nTraceback (most recent call last):\n  File …")
    assert "\n" in (C.get(cmd.id).error or ""), "the registry kept the whole thing"
    row = X.command(cmd.id)
    assert row["error"] == "spawn failed"


def test_no_command_output_ever_reaches_the_ledger():
    """⚠️ THERE IS NO stdout/stderr COLUMN, AND THAT IS THE POINT.

    The ledger is written on a path that has just proven the machine is in an
    unknown state. Persisting command output would write tokens, passwords and
    `env` dumps straight into the file `core/secrets.py` exists to protect.
    """
    cmd = C.create("env")
    C.start(cmd.id, process_id=6)
    C.complete(cmd.id, 0, stdout=f"AWS_SECRET_ACCESS_KEY={SECRET}\n",
               stderr=f"warning: leaked {SECRET}")
    # The registry DOES hold it — so what follows is a statement about the ledger,
    # not about a value that never existed anywhere.
    assert SECRET in (C.get(cmd.id).stdout or "")
    assert SECRET in (C.get(cmd.id).stderr or "")

    row = X.command(cmd.id)
    assert SECRET not in json.dumps(row, default=str)
    assert not [c for c in row
                if c in ("stdout", "stderr", "output", "content", "snapshot")]


# ── The crash predicate: both halves ──────────────────────────────────────────

def test_this_processes_own_row_is_never_reported_as_a_crash():
    """⚠️ HALF ONE — `instance <> INSTANCE`.

    A row this process wrote is this process's business, however long it has been
    silent: a `sleep 300` and a `nmap -p-` print nothing for minutes, so silence is
    not evidence of death when the asker is the owner. Drop this test and every
    surface reports its own in-flight work at every scan — and dual mode, two
    processes over one `agent2.db`, does it twice.
    """
    cmd = C.create("sleep 300")
    C.start(cmd.id, process_id=99)
    _age("exec_commands", cmd.id, 86_400)          # a whole day of silence
    assert cmd.id not in _ids(X.interrupted(stale_after=0)["commands"])

    # The SAME row, attributed to a process that cannot exist, IS reported. Without
    # this the assertion above would also pass for a reader that found nothing.
    db.exe("UPDATE exec_commands SET instance=? WHERE id=?", (DEAD, cmd.id))
    assert cmd.id in _ids(X.interrupted(stale_after=0)["commands"])


def test_a_fresh_foreign_row_is_not_reported_as_a_crash():
    """⚠️ HALF TWO — `updated_at < cutoff`.

    Foreign and unsettled is not enough: in dual mode the other surface is alive
    and its command is genuinely running. Only silence past `EXEC_STALE_SEC` turns
    a foreign row into abandoned work.
    """
    cmd = C.create("nmap -p- 10.0.0.1")
    C.start(cmd.id, process_id=1234)
    db.exe("UPDATE exec_commands SET instance=? WHERE id=?", (DEAD, cmd.id))
    assert cmd.id not in _ids(X.interrupted(stale_after=3600)["commands"])

    _age("exec_commands", cmd.id, 7200)
    assert cmd.id in _ids(X.interrupted(stale_after=3600)["commands"])


@pytest.mark.parametrize("status", sorted(C.TERMINAL))
def test_a_settled_row_is_never_offered_for_recovery(status):
    """⚠️ spec 25.2 — COMPLETED / CANCELLED work is never recovered.

    All four failure verdicts count as settled, not just COMPLETED: a command the
    user cancelled is the one thing recovery must never helpfully re-run.
    """
    cmd = C.create("echo settled")
    C.start(cmd.id, process_id=5)
    db.exe("UPDATE exec_commands SET status=? WHERE id=?", (status, cmd.id))
    _orphan("exec_commands", cmd.id)
    assert cmd.id not in _ids(X.interrupted(stale_after=0)["commands"])


def test_interrupted_is_idempotent_across_a_sweep():
    """⚠️ `INTERRUPTED` IS SETTLED FOR THE TRIM AND UNSETTLED FOR THE READER.

    Three facts in one scenario, and they only hold together: the sweep marks the
    row, the reader that reported it still finds it, `updated_at` is not bumped —
    it is the forensic record of when the dead process last spoke — and a second
    sweep does not re-count what it already marked.
    """
    cmd = C.create("half a migration")
    C.start(cmd.id, process_id=8)
    _orphan("exec_commands", cmd.id)
    before = _row("exec_commands", cmd.id)["updated_at"]

    first = X.sweep(stale_after=0)
    assert first["commands"] == 1 and first["total"] == 1
    assert X.command(cmd.id)["status"] == X.INTERRUPTED
    assert cmd.id in _ids(X.interrupted(stale_after=0)["commands"])
    assert X.command(cmd.id)["updated_at"] == before

    assert X.sweep(stale_after=0) == {**first, "commands": 0, "total": 0,
                                      "cutoff": X._cutoff(0)}


def test_a_sweep_reports_counters_and_never_a_command_line(monkeypatch):
    """The structured log spec 26 asks for — and nothing more than counters."""
    seen: dict = {}
    monkeypatch.setattr(X.alog, "exec_interrupted", lambda **kw: seen.update(kw))
    cmd = C.create(f"curl -H 'Authorization: Bearer {SECRET}' https://example.test")
    C.start(cmd.id, process_id=7)
    _orphan("exec_commands", cmd.id)

    X.sweep(stale_after=0)
    assert seen.get("commands") == 1
    assert seen.get("tool_calls") == 0 and seen.get("workflows") == 0
    assert seen.get("cutoff")
    assert SECRET not in json.dumps(seen, default=str)


# ── Tool calls: the pre/post pair ─────────────────────────────────────────────

def test_an_unfinished_tool_call_has_a_null_post(tmp_path):
    """⚠️ `post: null` IS THE CRASH SIGNAL.

    An empty dict would be indistinguishable from "finished, touched nothing" —
    and that difference is the whole of Task 26's "did this write already happen?".
    """
    target = tmp_path / "out.txt"
    call_id = X.tool_started("write_file", {"path": str(target), "content": "x"},
                             session_id="s1", surface="cli")
    assert call_id
    row = _row("exec_tool_calls", call_id)
    assert row["status"] == T.STEP_RUNNING and row["completed_at"] == ""
    body = X.parse_detail(row["detail"])
    assert body["post"] is None
    assert body["pre"] == {os.path.abspath(str(target)): None}

    target.write_text("x", encoding="utf-8")
    X.tool_finished(call_id, ok=True)
    row = _row("exec_tool_calls", call_id)
    assert row["status"] == T.STEP_COMPLETED and row["ok"] == 1
    body = X.parse_detail(row["detail"])
    assert body["post"] == {os.path.abspath(str(target)): _sha(target)}
    assert body["pre"] == {os.path.abspath(str(target)): None}


def test_a_delete_records_the_disappearance(tmp_path):
    """`None` in `post` means ABSENT, which is exactly what a completed delete
    looks like — and it is deliberately not the value an unreadable path yields."""
    victim = tmp_path / "gone.txt"
    victim.write_text("bye", encoding="utf-8")
    digest = _sha(victim)
    key = os.path.abspath(str(victim))

    call_id = X.tool_started("delete_file", {"path": str(victim)})
    victim.unlink()
    X.tool_finished(call_id, ok=True)

    body = X.parse_detail(_row("exec_tool_calls", call_id)["detail"])
    assert body["pre"] == {key: digest}
    assert body["post"] == {key: None}


def test_a_content_bearing_argument_never_reaches_the_detail_column(tmp_path):
    """⚠️ `detail` holds DIGESTS of the files a tool touched, never their bytes —
    and never its arguments' bytes either."""
    target = tmp_path / "creds.env"
    payload = f"OPENAI_API_KEY={SECRET}"
    call_id = X.tool_started("write_file", {"path": str(target), "content": payload})
    target.write_text(payload, encoding="utf-8")
    X.tool_finished(call_id, ok=True)

    row = _row("exec_tool_calls", call_id)
    assert SECRET not in json.dumps(row, default=str)
    # The PATH is recorded, so the row is still useful — it simply is not content.
    assert "creds.env" in str(row["detail"])


def test_a_tool_call_that_never_returned_reads_as_interrupted_next_launch(tmp_path):
    """The tool-call half of the crash scenario, from the next process's side."""
    target = tmp_path / "half-written.txt"
    call_id = X.tool_started("write_file", {"path": str(target), "content": "…"},
                             session_id="s9", chat_id="c9", surface="web")
    _orphan("exec_tool_calls", call_id)

    found = X.interrupted(stale_after=0)
    assert call_id in _ids(found["tool_calls"])
    row = _row("exec_tool_calls", call_id)
    assert row["destructive"] == 1 and row["completed_at"] == ""
    assert X.parse_detail(row["detail"])["post"] is None


def test_a_settle_after_the_pending_map_was_dropped_still_records_the_post(tmp_path):
    """⚠️ `_pending` is a CACHE, not the record.

    A prune, a workspace switch, or a settle reached from another process must not
    cost the POST digests: the row itself carries the paths, so `_reload()` can
    re-derive them.
    """
    target = tmp_path / "late.txt"
    call_id = X.tool_started("write_file", {"path": str(target), "content": "x"})
    X.reset()                                   # the in-memory half is gone
    target.write_text("x", encoding="utf-8")
    X.tool_finished(call_id, ok=True)

    body = X.parse_detail(_row("exec_tool_calls", call_id)["detail"])
    assert body["post"] == {os.path.abspath(str(target)): _sha(target)}


def test_the_human_label_recorded_at_start_survives_the_settle(tmp_path):
    """`target` is passed in, not re-derived — `tools._step_detail()` owns that one
    string — so the settle must carry it forward rather than blank it."""
    call_id = X.tool_started("write_file", {"path": str(tmp_path / "x.txt")},
                             target="x.txt")
    X.tool_finished(call_id, ok=True)
    assert X.parse_detail(_row("exec_tool_calls", call_id)["detail"])["target"] == "x.txt"


def test_a_failed_tool_call_is_settled_as_failed_with_its_reason():
    call_id = X.tool_started("write_file", {"path": "nowhere/at/all.txt"})
    X.tool_finished(call_id, ok=False, error="path outside workspace")
    row = _row("exec_tool_calls", call_id)
    assert row["status"] == T.STEP_FAILED and row["ok"] == 0
    assert row["error"] == "path outside workspace"


# ── Which paths a tool's arguments name ───────────────────────────────────────

def test_target_paths_names_every_edit_of_a_multi_edit(tmp_path):
    """⚠️ EVERY edit's path. `tools._step_detail()` records only the first, which
    is exactly why `recovery.verify_step()` lands on `uncertain` for a multi-file
    edit today — the fact Task 26 needs was never recorded."""
    args = {"edits": [{"path": str(tmp_path / name)}
                      for name in ("a.py", "b.py", "c.py")]}
    found = X.target_paths("multi_edit_files", args)
    assert len(found) == 3
    assert {os.path.basename(p) for p in found} == {"a.py", "b.py", "c.py"}


def test_run_command_declares_no_target_paths():
    """⚠️ `[]` ON PURPOSE. A shell command's targets are not knowable from its
    argv, and guessing them would hand Task 26 a confident wrong answer where
    `exec_commands.status` gives it a right one."""
    assert X.target_paths("run_command", {"command": "rm -rf /tmp/whatever"}) == []


def test_a_converters_output_path_is_a_target_too(tmp_path):
    """`convert_file` writes somewhere its `path` argument does not name — the
    coverage gap `diffs.capture_for()` documents as a deliberate blind spot."""
    found = X.target_paths("convert_file", {
        "path": str(tmp_path / "in.md"),
        "options": {"output_path": str(tmp_path / "out.pdf")}})
    assert len(found) == 2
    assert any(p.endswith("out.pdf") for p in found)


def test_target_paths_is_total_for_junk():
    for args in (None, "not a dict", 17, {"edits": "not a list"}, {}):
        assert X.target_paths("multi_edit_files", args) == []
    assert X.target_paths("", {"path": "x"}) == []


def test_parse_detail_is_total():
    assert X.parse_detail(None) == {}
    assert X.parse_detail("") == {}
    assert X.parse_detail("not json at all") == {}
    assert X.parse_detail("[1,2,3]") == {}          # JSON, but not a dict
    assert X.parse_detail('{"tool":"write_file"}')["tool"] == "write_file"


# ── Digests ───────────────────────────────────────────────────────────────────

def test_a_file_too_large_to_hash_degrades_to_size_and_mtime(tmp_path, monkeypatch):
    """Degrades to exactly the heuristic `recovery.verify_step()` already uses,
    rather than reading a gigabyte on the hot path."""
    monkeypatch.setattr(X, "MAX_DIGEST_BYTES", 4)
    big = tmp_path / "big.bin"
    big.write_bytes(b"0123456789")
    digest = X._digest(str(big))
    assert str(digest).startswith("size:10,mtime:")
    assert "0123456789" not in str(digest)


def test_a_directory_reports_that_it_exists_and_an_absence_reports_none(tmp_path):
    assert str(X._digest(str(tmp_path))).startswith("kind:dir,")
    assert X._digest(str(tmp_path / "nothing-here")) is None


# ── The trim ──────────────────────────────────────────────────────────────────

def test_the_trim_never_evicts_an_unsettled_row(monkeypatch):
    """⚠️ THE ONE ROW A CRASH MAKES PRECIOUS IS THE ONE AN UNCONDITIONAL TRIM
    WOULD DELETE.

    `llm/router.py` trims a pure history and may drop its oldest row blind; this
    table holds live state, so it is allowed to exceed the cap instead.
    """
    monkeypatch.setattr(cfg, "EXEC_LEDGER_MAX", 50)
    for i in range(60):
        _insert_tool_row(f"live-{i:03d}", status=T.STEP_RUNNING,
                         updated_at=_stamp(i * 60))
    X._trim("exec_tool_calls")
    assert db.qone("SELECT COUNT(*) AS n FROM exec_tool_calls")["n"] == 60


def test_the_trim_evicts_the_oldest_settled_rows_first(monkeypatch):
    """And the cap IS enforced — against the rows it is safe to enforce it on."""
    monkeypatch.setattr(cfg, "EXEC_LEDGER_MAX", 50)
    for i in range(20):
        _insert_tool_row(f"live-{i:03d}", status=T.STEP_RUNNING,
                         updated_at=_stamp(i * 60))
    for i in range(60):
        _insert_tool_row(f"done-{i:03d}", status=T.STEP_COMPLETED,
                         updated_at=_stamp(i * 60))

    X._trim("exec_tool_calls")
    left = {str(r["id"]) for r in db.qall("SELECT id FROM exec_tool_calls")}
    assert len(left) == 50
    assert {f"live-{i:03d}" for i in range(20)} <= left
    assert {f"done-{i:03d}" for i in range(30)} <= left     # the newest 30 stayed
    assert not any(f"done-{i:03d}" in left for i in range(30, 60))


def test_a_swept_row_becomes_trimmable(monkeypatch):
    """The other side of the `_TERMINAL` / `_SETTLED` split: once a crash has been
    recorded, the row is history and the table may reclaim it."""
    monkeypatch.setattr(cfg, "EXEC_LEDGER_MAX", 50)
    for i in range(60):
        _insert_tool_row(f"gone-{i:03d}", status=X.INTERRUPTED,
                         updated_at=_stamp(i * 60))
    X._trim("exec_tool_calls")
    assert db.qone("SELECT COUNT(*) AS n FROM exec_tool_calls")["n"] == 50


# ── Workflows: declared now, called in Phase 12 ───────────────────────────────

def test_a_workflow_run_is_recorded_end_to_end():
    """No caller until Phase 12 — declared and tested here for the same reason the
    Context Broker declares `skills` with an empty collector: a thing that does not
    exist yet still has a name, a shape and a slot in the report."""
    run_id = X.workflow_started("deploy", session_id="s1", chat_id="c1",
                                total_steps=3, state={"stage": "init"})
    assert run_id
    row = _row("exec_workflows", run_id)
    assert row["status"] == T.STEP_RUNNING and row["total_steps"] == 3
    assert row["instance"] == X.INSTANCE

    X.workflow_step(run_id, step="build", step_index=1, state={"stage": "build"})
    mid = _row("exec_workflows", run_id)
    assert mid["step"] == "build" and mid["step_index"] == 1
    assert json.loads(mid["state"])["stage"] == "build"

    X.workflow_finished(run_id, ok=True, state={"stage": "done"})
    end = _row("exec_workflows", run_id)
    assert end["status"] == T.STEP_COMPLETED and end["completed_at"]


def test_a_workflow_interrupted_mid_step_keeps_its_position():
    """⚠️ spec 25.7 — completed nodes stay completed and the whole workflow is not
    restarted. The recorded position is what makes that possible at all."""
    run_id = X.workflow_started("migrate", total_steps=5)
    X.workflow_step(run_id, step="node-3", step_index=3)
    _orphan("exec_workflows", run_id)

    assert run_id in _ids(X.interrupted(stale_after=0)["workflows"])
    row = _row("exec_workflows", run_id)
    assert row["step"] == "node-3" and row["step_index"] == 3
    assert row["total_steps"] == 5


def test_parallel_workflow_runs_are_independent_rows():
    """spec 25.8 — parallel recovery has to be able to tell the legs apart."""
    ids = [X.workflow_started(f"branch-{i}", total_steps=2) for i in range(4)]
    for i, rid in enumerate(ids):
        X.workflow_step(rid, step=f"step-{i}", step_index=i)
    X.workflow_finished(ids[0], ok=True)
    X.workflow_finished(ids[1], ok=False, error="node failed")
    for rid in ids[2:]:
        _orphan("exec_workflows", rid)

    assert _ids(X.interrupted(stale_after=0)["workflows"]) == set(ids[2:])
    assert _row("exec_workflows", ids[0])["status"] == T.STEP_COMPLETED
    assert _row("exec_workflows", ids[1])["status"] == T.STEP_FAILED
    assert _row("exec_workflows", ids[1])["error"] == "node failed"


def test_an_oversized_workflow_state_is_dropped_not_truncated():
    """Truncated JSON is unparseable, and a reader that cannot parse it cannot
    tell "too big" from "corrupt". `{}` is the honest answer."""
    run_id = X.workflow_started("huge", state={"blob": "x" * (X.MAX_STATE_CHARS + 100)})
    assert json.loads(_row("exec_workflows", run_id)["state"]) == {}


# ── §24.8 — persistence across a simulated termination ────────────────────────

def test_a_running_task_and_its_command_read_as_interrupted_from_the_next_process():
    """THE headline scenario of spec 24.8, as two processes.

    Start a task, persist RUNNING, save a checkpoint, simulate termination, then
    ask what a restart can conclude from the rows alone. `core/tasks.py` still owns
    the plan; the ledger is what tells the next process that the plan's owner is
    gone rather than merely quiet.
    """
    session_id = T.open_session(cwd=str(workspace.root()), goal="ship it")
    done = T.create(session_id, "step one")
    T.complete(done.id, "landed")
    live = T.create(session_id, "step two")
    T.start(live.id)
    T.plan_steps(live.id, ["read the config", "write the config"])
    T.record_step(live.id, "read the config", T.STEP_COMPLETED)
    T.record_step(live.id, "write the config", T.STEP_RUNNING,
                  detail="config.toml", destructive=True)
    cmd = C.create("pytest -x", task_id=live.id, session_id=session_id)
    C.start(cmd.id, process_id=31337)
    C.heartbeat(cmd.id)

    # ── kill -9 lands here: no handler runs, nothing is settled ──
    _orphan("exec_commands", cmd.id)

    # …and this is the next process, with no registry to consult.
    assert cmd.id in _ids(X.interrupted(stale_after=0)["commands"])
    row = X.command(cmd.id)
    assert row["status"] == C.CommandStatus.STREAMING      # the last known state
    assert row["process_id"] == 31337
    assert row["task_id"] == live.id and row["session_id"] == session_id

    # The task half is exactly where `core/tasks` left it — and completed work is
    # still completed, which is the invariant recovery exists to protect.
    rp = _recovery.plan(session_id)
    assert [t.title for t in rp.done] == ["step one"]
    assert rp.resume is not None and rp.resume.id == live.id
    assert rp.view["completed"] == ["read the config"]
    assert rp.view["current"] == "write the config"
    assert rp.view["destructive_pending"] is True
    assert rp.needs_verification, "an in-flight destructive step must be verified"


@pytest.mark.parametrize("finish,status", [
    (T.complete, T.TaskStatus.COMPLETED),
    (T.fail, T.TaskStatus.FAILED),
])
def test_a_finished_task_is_never_handed_back_as_work(finish, status):
    session_id = T.open_session(cwd=str(workspace.root()), goal="one thing")
    task = T.create(session_id, "the only task")
    T.start(task.id)
    finish(task.id)
    assert T.get(task.id).status == status
    assert _recovery.plan(session_id).is_empty()
    assert session_id not in {str(r["id"])
                              for r in T.unfinished_sessions(str(workspace.root()))}


def test_a_pending_task_survives_a_restart_and_has_nothing_to_verify():
    session_id = T.open_session(cwd=str(workspace.root()), goal="not started")
    task = T.create(session_id, "never started")
    assert T.get(task.id).status == T.TaskStatus.PENDING

    rp = _recovery.plan(session_id)
    assert rp.resume is not None and rp.resume.id == task.id
    assert rp.checks == [] and not rp.needs_verification


def test_multiple_interrupted_tasks_all_survive_with_their_own_state():
    session_id = T.open_session(cwd=str(workspace.root()), goal="three of them")
    made = [T.create(session_id, f"task {i}") for i in range(3)]
    T.complete(made[0].id, "landed")
    T.start(made[1].id, progress=0.4)

    T.interrupt(session_id, "process died")

    rp = _recovery.plan(session_id)
    assert [t.title for t in rp.done] == ["task 0"]
    assert rp.resume is not None and rp.resume.id == made[1].id
    assert [t.title for t in rp.waiting] == ["task 2"]
    parked = T.get(made[1].id)
    assert parked.status == T.TaskStatus.PAUSED       # honest "started, not finished"
    assert parked.progress == 0.4                    # and how far it got


def test_parallel_commands_are_each_persisted_independently():
    """spec 24.8's parallel case: three legs, one settled, two abandoned — and the
    ledger can still say which task each belonged to."""
    session_id = T.open_session(cwd=str(workspace.root()), goal="fan out")
    legs = [T.create(session_id, f"leg {i}") for i in range(3)]
    cmds = []
    for i, task in enumerate(legs):
        T.start(task.id)
        cmd = C.create(f"work {i}", task_id=task.id, session_id=session_id)
        C.start(cmd.id, process_id=1000 + i)
        cmds.append(cmd)

    C.complete(cmds[0].id, 0)
    for cmd in cmds[1:]:
        _orphan("exec_commands", cmd.id)

    assert _ids(X.interrupted(stale_after=0)["commands"]) == {c.id for c in cmds[1:]}
    assert len(X.commands(session_id=session_id, limit=50)) == 3
    by_task = {X.command(c.id)["task_id"] for c in cmds}
    assert by_task == {t.id for t in legs}


def test_pausing_a_chat_still_pauses_the_chat_and_recovers_nothing():
    """⚠️ `/pause` AND `/resume` ARE CHAT CONTROLS AND PHASE 8 DOES NOT TOUCH THEM.

    A pause parks the task — that is `context.pause_chat()`'s existing breadcrumb,
    not recovery — and it neither adopts a recovery nor marks anything
    `interrupted`. Repurposing either one is on the phase's explicit do-not list.
    """
    chat = _ctx.new_chat("2.5-flash", "pro", title="phase 8 pause")
    session_id = T.open_session(chat_id=chat["id"], cwd=str(workspace.root()))
    task = T.create(session_id, "mid-flight")
    T.start(task.id)
    cmd = C.create("long thing", task_id=task.id, session_id=session_id)
    C.start(cmd.id, process_id=77)

    _ctx.pause_chat(chat["id"])
    assert db.qone("SELECT status FROM chats WHERE id=?",
                   (chat["id"],))["status"] == "paused"
    parked = T.get(task.id)
    assert parked.status == T.TaskStatus.PAUSED
    assert not (parked.checkpoint.get(T.CP_RECOVERED) or {})
    # The ledger still says whatever the runner last said — a pause is not a crash.
    assert X.command(cmd.id)["status"] == C.CommandStatus.RUNNING

    _ctx.resume_chat(chat["id"])
    assert db.qone("SELECT status FROM chats WHERE id=?",
                   (chat["id"],))["status"] == "active"


# ── Reports ───────────────────────────────────────────────────────────────────

def test_stats_reports_counters_and_never_a_command_line():
    """`/api/health` will read this in Task 25, so it may carry nothing else."""
    cmd = C.create(f"curl -H 'Authorization: Bearer {SECRET}' https://example.test")
    C.start(cmd.id, process_id=3)
    call_id = X.tool_started("read_file", {"path": "x"})

    st = X.stats()
    assert st["instance"] == X.INSTANCE
    assert st["exec_commands"] == {"total": 1, "active": 1, "interrupted": 0,
                                   "by_status": {C.CommandStatus.RUNNING: 1}}
    assert st["exec_tool_calls"]["active"] == 1
    assert SECRET not in json.dumps(st, default=str)

    _orphan("exec_commands", cmd.id)
    X.sweep(stale_after=0)
    after = X.stats()
    assert after["exec_commands"]["interrupted"] == 1
    assert after["exec_commands"]["active"] == 0
    X.tool_finished(call_id, ok=True)


def test_snapshot_reads_sessions_through_core_tasks():
    """⚠️ Sessions come from `core.tasks`, never from a second query against
    `task_sessions` — that would be a second declaration of "unfinished"."""
    session_id = T.open_session(cwd=str(workspace.root()), goal="snapshot me")
    T.create(session_id, "open item")
    cmd = C.create("echo snap")

    snap = X.snapshot()
    assert snap["instance"] == X.INSTANCE and snap["persist"] is True
    assert cmd.id in _ids(snap["commands"])
    assert session_id in {str(r.get("id")) for r in snap["sessions"]}


# ── Totality ──────────────────────────────────────────────────────────────────

def test_persistence_can_be_turned_off_and_then_nothing_is_written(monkeypatch):
    """The knob is an escape hatch, not a half-measure: off means no row at all, so
    an operator who disables it does not get a ledger that lies by omission."""
    monkeypatch.setattr(cfg, "EXEC_PERSIST", False)
    cmd = C.create("echo quiet")
    C.start(cmd.id, process_id=2)
    C.complete(cmd.id, 0)
    assert X.command(cmd.id) is None
    assert X.tool_started("write_file", {"path": "x"}) == ""
    assert X.workflow_started("nope") == ""
    for table in EXEC_TABLES:
        assert db.qone(f"SELECT COUNT(*) AS n FROM {table}")["n"] == 0


def test_every_reader_is_total_when_the_database_cannot_be_read(monkeypatch):
    """⚠️ BOOKKEEPING MAY NEVER BE THE REASON A TURN FAILS.

    Every reader here is consulted from a startup path or a report, so an
    unreadable database has to read as "nothing to say".
    """
    def _boom(*a, **k):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(db, "qall", _boom)
    monkeypatch.setattr(db, "qone", _boom)

    assert X.commands() == [] and X.tool_calls() == [] and X.workflows() == []
    assert X.command("whatever") is None
    found = X.interrupted()
    assert found["total"] == 0 and found["commands"] == []
    assert X.stats()["exec_commands"]["total"] == 0
    snap = X.snapshot()
    assert snap["commands"] == [] and snap["sessions"] == []
    assert X.sweep()["total"] == 0
    assert X._rows("not_a_table_at_all") == []


def test_no_writer_raises_when_the_database_cannot_be_written(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(db, "exe", _boom)
    cmd = C.create("echo unwritable")          # `commands.py` must survive this
    C.start(cmd.id, process_id=1)
    C.complete(cmd.id, 0)
    assert C.get(cmd.id).status == C.CommandStatus.COMPLETED
    assert X.tool_started("write_file", {"path": "x"}) == ""
    X.tool_finished("some-id", ok=True)
    assert X.workflow_started("nope") == ""
    X.workflow_step("some-id", step="s")
    X.workflow_finished("some-id", ok=True)


def test_a_broken_ledger_is_reported_once_and_then_stays_quiet(monkeypatch):
    """A broken ledger must not be silent — but it must not be loud either. One
    WARNING per output line is the noise that gets a whole log ignored."""
    seen: list = []
    monkeypatch.setattr(X.alog, "exec_persist_failed",
                        lambda op, error: seen.append((op, error)))
    monkeypatch.setattr(db, "exe", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("disk full")))

    for _ in range(5):
        X.tool_started("read_file", {"path": "x"})
    assert len(seen) == 1
    assert SECRET not in json.dumps(seen, default=str)


def test_reset_clears_the_books_but_never_the_rows():
    """⚠️ The ledger's whole purpose is to OUTLIVE the process, so a "reset" that
    truncated it would delete the evidence the next launch reads."""
    cmd = C.create("echo keep")
    C.start(cmd.id, process_id=9)
    assert X._beats, "the throttle book should be tracking the live row"

    X.reset()
    assert not X._beats and not X._pending
    assert X.command(cmd.id) is not None
