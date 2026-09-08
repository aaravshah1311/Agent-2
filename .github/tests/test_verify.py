# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
Tests for the shared verification system (``agent2/core/verify.py``).

Run from the repo root:  python -m pytest .github/tests/test_verify.py -v

What this suite is for
──────────────────────
The user's standing rule for this whole phase is one sentence — *"Never trust:
Worker says Done → Agent2 says Done"* — and the spec restates it as *"'Done' is not
verification."* That makes this module unusual: its job is to **disagree** with a
status column, and a verifier that quietly agreed with everything would look
identical to a working one on every green run. So almost every test here builds a
unit that **claims success** and then plants a record that contradicts it.

The five things worth knowing before editing:

* ``test_a_completed_claim_with_no_evidence_is_unconfirmed_never_confirmed`` — the
  whole point. Zero evidence is `V_UNCONFIRMED`, a third value that is neither yes
  nor no. Collapsing it into `V_CONFIRMED` would make the module a no-op that
  still passes a "does it return confirmed" test; collapsing it into a *problem*
  would report every read-only node as broken.
* ``test_the_status_check_can_never_produce_a_problem_by_construction`` — written
  as a loop over every status, because `_check_status` is the one check whose
  docstring promises it cannot contradict anything. If it ever could, the verdict
  would be derivable from the claim alone and this module would have no reason to
  exist.
* ``test_no_confirmed_finding_may_carry_a_check_with_a_problem`` and
  ``test_every_report_problem_line_traces_to_a_check_that_holds_it`` — the pair
  that pins `core.health._rows()`'s rule in verifier shape: the verdict a monitor
  reads and the lines a human reads are derived from each other, so they cannot
  drift. Either half alone would also pass against a verifier that reported
  nothing at all.
* ``test_an_identical_rewrite_is_a_warning_and_never_a_contradiction`` — the write
  check is the sharpest one here and this is its calibration. `projectdoc`'s rule
  is that an identical rewrite is not a write, so a byte-for-byte no-op is
  something to mention, not a lie to report.
* ``test_the_ledgers_are_read_twice_whatever_the_node_count`` — the cost pin. A
  per-node ledger read is invisible at the size a developer tests with and 48
  round trips at the size a user writes, which is `workflow.for_turn()`'s rule.

Everything is exercised through real rows: real `task_sessions` / `agent_tasks`
rows from `core.tasks`, real `exec_tool_calls` rows written by
`execstate.tool_started` / `tool_finished` against real files on disk, and raw
inserts only where a *crash* shape is being reproduced that no healthy writer can
produce (a command row that never settled, an `interrupted` park).
"""

from __future__ import annotations

import ast
import json
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest

from agent2 import config as cfg
from agent2 import database as db
from agent2.core import execstate as X
from agent2.core import tasks as T
from agent2.core import verify as V

SECRET_BODY = "sk-live-VERIFY-MUST-NOT-ECHO-THIS-9f31c2"

_TABLES = ("exec_commands", "exec_tool_calls", "agent_tasks", "task_sessions")


@pytest.fixture(autouse=True)
def _clean():
    db.init_db()
    X.reset()
    for table in _TABLES:
        db.exe(f"DELETE FROM {table}")
    yield
    X.reset()
    for table in _TABLES:
        db.exe(f"DELETE FROM {table}")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _uid() -> str:
    return uuid.uuid4().hex[:12]


def _session() -> str:
    return T.open_session(chat_id="verify-" + _uid(), cwd="/proj/verify")


def _task(session_id: str, title: str = "unit", *, status: str = "",
          error: str = "", checkpoint: dict | None = None) -> T.Task:
    task = T.create(session_id, title, checkpoint=checkpoint)
    if status:
        T.set_status(task.id, status, error=error)
        task = T.get(task.id)
    return task


def _cmd_row(session_id: str, task_id: str, *, status: str, exit_code=None,
             error: str = "", command: str = "echo hello") -> str:
    """A raw `exec_commands` row — the only way to reproduce a crash shape.

    `record_command()` takes a live execution object and `commands._settle()`
    refuses to move a settled row, so "still running with nobody left to settle
    it" is a state no healthy writer can produce. That state is exactly what
    `_check_commands` exists to catch, so it is built by hand here.
    """
    row_id = "cmd-" + _uid()
    db.exe(
        "INSERT INTO exec_commands(id, instance, project, session_id, task_id,"
        " surface, term_id, command, process_id, status, exit_code, output_lines,"
        " error, created_at, started_at, last_output_at, completed_at, updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))",
        (row_id, X.INSTANCE, "", session_id, task_id, "cli", "", command, 4242,
         status, exit_code, 0, error, "", "", "", ""),
    )
    return row_id


def _tool_row(session_id: str, task_id: str, *, tool: str = "read_file",
              status: str = T.STEP_COMPLETED, ok=1, error: str = "",
              detail: dict | None = None) -> str:
    """A raw `exec_tool_calls` row, for the shapes a healthy writer cannot make."""
    row_id = "tc-" + _uid()
    body = "" if detail is None else json.dumps(detail, separators=(",", ":"))
    db.exe(
        "INSERT INTO exec_tool_calls(id, instance, project, session_id, task_id,"
        " chat_id, surface, tool, detail, destructive, status, ok, error,"
        " started_at, completed_at, updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))",
        (row_id, X.INSTANCE, "", session_id, task_id, "", "cli", tool, body, 1,
         status, ok, error, "", ""),
    )
    return row_id


def _file_call(session_id: str, task_id: str, tool: str, path: Path, *,
               after, ok: bool | None = True) -> str:
    """One REAL tool call recorded end to end, with `after` deciding the outcome.

    `after` runs between `tool_started` and `tool_finished`, which is exactly where
    the filesystem either did or did not move — so the `pre`/`post` digest pair the
    write check reads is measured by the shipping writer, not fabricated here.
    """
    call_id = X.tool_started(tool, {"path": str(path)}, session_id=session_id,
                             task_id=task_id, surface="cli", destructive=True)
    after()
    X.tool_finished(call_id, ok=ok)
    return call_id


@contextmanager
def _queries(monkeypatch):
    calls = {"qall": 0, "qone": 0}
    real_qall, real_qone = db.qall, db.qone

    def wrap(key, fn):
        def inner(*a, **k):
            calls[key] += 1
            return fn(*a, **k)
        return inner

    monkeypatch.setattr(db, "qall", wrap("qall", real_qall))
    monkeypatch.setattr(db, "qone", wrap("qone", real_qone))
    try:
        yield calls
    finally:
        monkeypatch.setattr(db, "qall", real_qall)
        monkeypatch.setattr(db, "qone", real_qone)


def _statuses() -> list[str]:
    return [getattr(T.TaskStatus, n) for n in dir(T.TaskStatus)
            if n.isupper() and isinstance(getattr(T.TaskStatus, n), str)]


# ══ 1. The vocabulary, and what `describe()` is derived from ══════════════════

def test_verdicts_are_worst_first_and_the_tuple_is_complete():
    """Every declared verdict is in `VERDICTS`, worst first.

    The order is load-bearing: a caller that renders the first entry renders the
    thing a human most needs to see. A verdict missing from the tuple would still
    be *returned* by `_verdict` and would simply never appear in `counts`.
    """
    declared = {V.V_OPEN, V.V_UNSUCCESSFUL, V.V_CONFIRMED,
                V.V_CONTRADICTED, V.V_UNCONFIRMED}
    assert set(V.VERDICTS) == declared
    assert len(V.VERDICTS) == len(declared)          # no duplicate spellings
    assert V.VERDICTS[0] == V.V_CONTRADICTED
    assert V.VERDICTS[-1] == V.V_CONFIRMED
    assert V.VERDICTS.index(V.V_CONTRADICTED) < V.VERDICTS.index(V.V_UNCONFIRMED)
    assert V.VERDICTS.index(V.V_UNSUCCESSFUL) < V.VERDICTS.index(V.V_OPEN)


def test_only_confirmed_is_trusted_and_unconfirmed_is_not_a_yes():
    """`TRUSTED` is one word. Unknown is not yes — `llm/capabilities.py`'s rule."""
    assert V.TRUSTED == frozenset((V.V_CONFIRMED,))
    assert V.V_UNCONFIRMED not in V.TRUSTED
    assert V.V_OPEN not in V.TRUSTED
    assert V.V_UNSUCCESSFUL not in V.TRUSTED
    assert V.V_CONTRADICTED not in V.TRUSTED


def test_checks_order_puts_the_claim_first_and_the_narrowest_last():
    assert V.CHECKS == (V.C_STATUS, V.C_STEPS, V.C_COMMANDS, V.C_TOOLS, V.C_WRITES)


def test_the_units_own_status_is_not_counted_as_evidence_for_itself():
    """`C_STATUS` is excluded from the evidence count — the circularity guard.

    If the claim counted as support for itself every completed row would carry
    `evidence >= 1` and `_verdict` would return `V_CONFIRMED` for a unit nothing
    ever recorded, which is the one state the spec forbids.
    """
    assert V.C_STATUS not in V._EVIDENCE_CHECKS
    assert set(V._EVIDENCE_CHECKS) == {V.C_STEPS, V.C_COMMANDS, V.C_TOOLS, V.C_WRITES}

    sid = _session()
    task = _task(sid, "claims only", status=T.TaskStatus.COMPLETED)
    finding = V.verify_task(task)
    status_check = next(c for c in finding.checks if c.name == V.C_STATUS)
    assert status_check.checked == 1              # it WAS examined …
    assert finding.evidence == 0                  # … and it is not evidence
    assert finding.verdict == V.V_UNCONFIRMED


def test_describe_is_derived_from_the_declarations_and_not_hand_written():
    """A hand-written `describe()` goes stale the first time a word is added."""
    d = V.describe()
    assert d["verdicts"] == list(V.VERDICTS)
    assert d["trusted"] == sorted(V.TRUSTED)
    assert d["checks"] == list(V.CHECKS)
    assert d["evidence_checks"] == sorted(V._EVIDENCE_CHECKS)
    assert d["expect_present"] == sorted(V._EXPECT_PRESENT)
    assert d["expect_absent"] == sorted(V._EXPECT_ABSENT)
    assert d["unsuccessful"] == sorted(V.UNSUCCESSFUL_STATUS)
    assert d["max_rows"] == V._cap()
    assert d["reads_per_report"] == 2
    assert d["restats_disk"] is False


# ══ 2. "Done" is not verification ════════════════════════════════════════════

def test_a_completed_claim_with_no_evidence_is_unconfirmed_never_confirmed():
    """THE test. A status column alone may never produce agreement."""
    sid = _session()
    task = _task(sid, "said done", status=T.TaskStatus.COMPLETED)

    finding = V.verify_task(task)
    assert finding.status == T.TaskStatus.COMPLETED
    assert finding.verdict == V.V_UNCONFIRMED
    assert finding.verdict != V.V_CONFIRMED
    assert finding.trusted is False
    assert finding.evidence == 0
    assert finding.problems == ()                  # not a contradiction either

    report = V.verify_tasks([task])
    assert report.ok is True                       # nothing contradicted it …
    assert report.counts[V.V_UNCONFIRMED] == 1     # … and nothing confirmed it,
    assert report.counts[V.V_CONFIRMED] == 0       #     which is said out loud


def test_a_completed_claim_over_a_failed_sub_step_is_contradicted():
    """The loop's own checkpoint outranks the loop's own status column."""
    sid = _session()
    task = _task(sid, "half done")
    T.record_step(task.id, "write the file", T.STEP_FAILED)
    T.set_status(task.id, T.TaskStatus.COMPLETED)

    finding = V.verify_task(T.get(task.id))
    assert finding.verdict == V.V_CONTRADICTED
    assert any("failed" in p for p in finding.problems)

    report = V.verify_tasks([task.id])
    assert report.ok is False
    assert report.verified is False
    assert report.counts[V.V_CONTRADICTED] == 1
    assert any(line.startswith(task.id + ":") for line in report.problems)


def test_a_completed_claim_over_a_non_zero_exit_is_contradicted():
    """An exit code is the one thing a shell cannot fake."""
    sid = _session()
    task = _task(sid, "ran a build", status=T.TaskStatus.COMPLETED)
    _cmd_row(sid, task.id, status="completed", exit_code=1, command="make all")

    finding = V.verify_task(task)
    assert finding.verdict == V.V_CONTRADICTED
    assert any("exited 1" in p for p in finding.problems)
    assert finding.evidence >= 1


def test_a_command_that_never_settled_is_a_problem_not_a_warning():
    """The killed-mid-turn shape: a process whose verdict nobody ever wrote."""
    sid = _session()
    task = _task(sid, "started something", status=T.TaskStatus.COMPLETED)
    _cmd_row(sid, task.id, status="running", command="npm install")

    finding = V.verify_task(task)
    cmds = next(c for c in finding.checks if c.name == V.C_COMMANDS)
    assert cmds.problems, "an unsettled command must be a problem, not a warning"
    assert cmds.warnings == ()
    assert cmds.ok is False
    assert finding.verdict == V.V_CONTRADICTED


def test_an_interrupted_tool_call_contradicts_the_claim_that_it_finished():
    """`execstate.INTERRUPTED` is a park, and a park under a success claim is a lie."""
    sid = _session()
    task = _task(sid, "wrote a file", status=T.TaskStatus.COMPLETED)
    _tool_row(sid, task.id, tool="write_file", status=X.INTERRUPTED, ok=None,
              detail={"tool": "write_file", "target": "a.txt", "paths": ["a.txt"],
                      "paths_total": 1, "pre": {}, "post": None})

    finding = V.verify_task(task)
    assert finding.verdict == V.V_CONTRADICTED
    assert any("interrupted" in p for p in finding.problems)


def test_a_settled_tool_row_with_no_recorded_outcome_is_contradicted():
    """`post is null` on a settled row is the crash signal `_detail_json` names."""
    sid = _session()
    task = _task(sid, "claims a write", status=T.TaskStatus.COMPLETED)
    _tool_row(sid, task.id, tool="write_file", status=T.STEP_COMPLETED, ok=1,
              detail={"tool": "write_file", "target": "a.txt", "paths": ["a.txt"],
                      "paths_total": 1, "pre": {"a.txt": None}, "post": None})

    finding = V.verify_task(task)
    assert finding.verdict == V.V_CONTRADICTED
    assert any("never returned" in p for p in finding.problems)


# ══ 3. Problems alone decide — warnings never promote ═════════════════════════

def test_a_read_only_unit_finishes_the_run_and_its_unconfirmed_count_is_reported():
    """The documented calibration, pinned in both directions.

    `Finding.trusted` is `V_CONFIRMED` alone, and `Report.verified` deliberately
    is **not** `all(f.trusted …)` — a node whose whole job is to read and reason
    records nothing measurable, so requiring confirmation would refuse to finish
    every run containing one. The asymmetry is only honest because the count is
    *reported* rather than hidden, which is `health.py`'s "a warning is reported
    beside the verdict and never promoted into it". Assert both halves, or one of
    them can quietly become the other.
    """
    sid = _session()
    task = _task(sid, "read and reasoned", status=T.TaskStatus.COMPLETED)
    report = V.verify_tasks([task])

    assert report.problems == ()                    # ⚠️ a tuple, not a list
    assert report.ok is True
    assert report.complete is True                  # every unit has settled …
    assert report.verified is True                  # … so the run may say Complete
    assert report.findings[0].trusted is False      # while the unit is NOT trusted
    assert report.counts[V.V_UNCONFIRMED] == 1      # and the payload says so
    assert report.to_payload()["counts"][V.V_UNCONFIRMED] == 1


def test_a_stale_error_string_under_a_completed_status_is_a_warning_only():
    """A node retried after a failure legitimately keeps the earlier message."""
    sid = _session()
    task = _task(sid, "retried", status=T.TaskStatus.COMPLETED,
                 error="connection reset (first attempt)")
    finding = V.verify_task(T.get(task.id))
    status_check = next(c for c in finding.checks if c.name == V.C_STATUS)
    assert status_check.problems == ()
    assert any("error still recorded" in w for w in status_check.warnings)
    assert finding.problems == ()
    assert finding.verdict != V.V_CONTRADICTED

    report = V.verify_tasks([task])
    assert report.ok is True
    assert report.warnings                          # said out loud, just not fatal


def test_the_status_check_can_never_produce_a_problem_by_construction():
    """Looped over every status, with and without an error string.

    `_check_status`'s docstring promises a claim cannot contradict itself. If it
    ever could, the verdict would be derivable from the claim alone.
    """
    class _Row:
        def __init__(self, status, error):
            self.status = status
            self.error = error

    for status in _statuses():
        for err in ("", "boom", "x" * 400):
            check = V._check_status(_Row(status, err))
            assert check.problems == (), f"{status!r} + {err[:8]!r} produced a problem"
            assert check.checked == 1


def test_a_check_with_warnings_only_still_reads_as_ok():
    assert V.Check(V.C_TOOLS, checked=3, warnings=("hm",)).ok is True
    assert V.Check(V.C_TOOLS, checked=3, problems=("no",)).ok is False


# ══ 4. Three booleans, three different questions ═════════════════════════════

def test_an_empty_report_is_not_complete():
    """`complete` requires findings — "nothing was checked" is not "all clear"."""
    report = V.verify_tasks([])
    assert report.findings == ()
    assert report.complete is False
    assert report.ok is True                        # nothing contradicted anything
    assert report.verified is False                 # and nothing was verified
    assert report.counts == dict.fromkeys(V.VERDICTS, 0)


def test_an_open_unit_keeps_the_report_incomplete_while_ok_stays_true():
    sid = _session()
    done = _task(sid, "finished", status=T.TaskStatus.COMPLETED)
    running = _task(sid, "still going", status=T.TaskStatus.RUNNING)

    report = V.verify_tasks([done, running])
    verdicts = {f.ref: f.verdict for f in report.findings}
    assert verdicts[running.id] == V.V_OPEN
    assert report.ok is True
    assert report.complete is False
    assert report.verified is False


def test_a_failed_unit_is_unsuccessful_so_ok_is_true_and_verified_is_false():
    """A failure honestly reported is not a contradiction — it is a failure."""
    sid = _session()
    bad = _task(sid, "blew up", status=T.TaskStatus.FAILED, error="exit 2")

    report = V.verify_tasks([bad])
    assert report.findings[0].verdict == V.V_UNSUCCESSFUL
    assert report.unsuccessful == 1
    assert report.ok is True
    assert report.complete is True
    assert report.verified is False, "an unsuccessful unit may never read as verified"


def test_every_unsuccessful_status_is_treated_as_one():
    sid = _session()
    assert set(V.UNSUCCESSFUL_STATUS) <= set(T.TERMINAL)
    for status in sorted(V.UNSUCCESSFUL_STATUS):
        task = _task(sid, "unit " + status, status=status)
        assert V.verify_task(T.get(task.id)).verdict == V.V_UNSUCCESSFUL


# ══ 5. The verdict traces to the lines a reader sees — both directions ════════

def test_no_confirmed_finding_may_carry_a_check_with_a_problem(tmp_path):
    """`health._rows()`'s rule, verifier-shaped: a row may not say fail while
    the aggregate says ok, and a verdict may not say confirmed while a check
    it was derived from holds a problem."""
    sid = _session()
    clean = _task(sid, "honest write")
    target = tmp_path / "clean.txt"
    target.write_text("before\n", encoding="utf-8")
    _file_call(sid, clean.id, "write_file", target,
               after=lambda: target.write_text("after\n", encoding="utf-8"))
    T.set_status(clean.id, T.TaskStatus.COMPLETED)
    clean = T.get(clean.id)               # ⚠️ the row moved; the object did not

    lying = _task(sid, "lied", status=T.TaskStatus.COMPLETED)
    _cmd_row(sid, lying.id, status="failed", exit_code=7, command="false")

    silent = _task(sid, "no evidence", status=T.TaskStatus.COMPLETED)
    broke = _task(sid, "gave up", status=T.TaskStatus.FAILED)

    report = V.verify_tasks([clean, lying, silent, broke])
    by_ref = {f.ref: f for f in report.findings}

    assert by_ref[clean.id].verdict == V.V_CONFIRMED
    assert by_ref[lying.id].verdict == V.V_CONTRADICTED
    assert by_ref[silent.id].verdict == V.V_UNCONFIRMED
    assert by_ref[broke.id].verdict == V.V_UNSUCCESSFUL

    for finding in report.findings:
        if finding.problems:
            assert finding.verdict != V.V_CONFIRMED
            assert finding.trusted is False
        if finding.trusted:
            assert finding.problems == ()
            assert finding.evidence > 0
            assert all(c.ok for c in finding.checks)


def test_every_report_problem_line_traces_to_a_check_that_holds_it():
    """The other direction: no line appears that no check produced, and no
    check's problem is silently dropped on the way to the report."""
    sid = _session()
    a = _task(sid, "a", status=T.TaskStatus.COMPLETED)
    b = _task(sid, "b", status=T.TaskStatus.COMPLETED)
    _cmd_row(sid, a.id, status="completed", exit_code=3, command="ls /nope")
    _cmd_row(sid, b.id, status="running", command="sleep 9")
    _cmd_row(sid, "", status="failed", exit_code=9, command="loose")

    report = V.verify_tasks([a, b])
    owned = [f"{f.ref}: {line}" for f in report.findings for line in f.problems]
    loose = [f"session: {line}" for c in report.session for line in c.problems]
    assert list(report.problems) == owned + loose
    assert len(report.problems) == len(owned) + len(loose)
    assert owned and loose, "the fixture must produce both kinds"
    for finding in report.findings:
        for line in finding.problems:
            assert any(line in c.problems for c in finding.checks)


# ══ 6. The writes check — the pre/post digest pair ═══════════════════════════

def test_a_write_that_reported_success_with_nothing_on_disk_is_contradicted(tmp_path):
    """The sharpest check: it said it wrote, and the target is not there."""
    sid = _session()
    task = _task(sid, "writes then vanishes")
    target = tmp_path / "gone.txt"
    target.write_text("here\n", encoding="utf-8")
    _file_call(sid, task.id, "write_file", target, after=lambda: target.unlink())
    T.set_status(task.id, T.TaskStatus.COMPLETED)

    finding = V.verify_task(T.get(task.id))
    writes = next(c for c in finding.checks if c.name == V.C_WRITES)
    assert writes.checked == 1
    assert any("is not there" in p for p in writes.problems)
    assert finding.verdict == V.V_CONTRADICTED


def test_a_delete_that_reported_success_with_the_file_still_there_is_contradicted(tmp_path):
    sid = _session()
    task = _task(sid, "deletes nothing")
    target = tmp_path / "stubborn.txt"
    target.write_text("still here\n", encoding="utf-8")
    _file_call(sid, task.id, "delete_file", target, after=lambda: None)
    T.set_status(task.id, T.TaskStatus.COMPLETED)

    finding = V.verify_task(T.get(task.id))
    writes = next(c for c in finding.checks if c.name == V.C_WRITES)
    assert any("still there" in p for p in writes.problems)
    assert finding.verdict == V.V_CONTRADICTED


def test_a_real_write_and_a_real_delete_both_confirm(tmp_path):
    """The calibration in the honest direction — the module must be able to agree."""
    sid = _session()
    wrote = _task(sid, "really wrote")
    made = tmp_path / "made.txt"
    made.write_text("v1\n", encoding="utf-8")
    _file_call(sid, wrote.id, "write_file", made,
               after=lambda: made.write_text("v2\n", encoding="utf-8"))
    T.set_status(wrote.id, T.TaskStatus.COMPLETED)

    removed = _task(sid, "really deleted")
    doomed = tmp_path / "doomed.txt"
    doomed.write_text("bye\n", encoding="utf-8")
    _file_call(sid, removed.id, "delete_file", doomed, after=lambda: doomed.unlink())
    T.set_status(removed.id, T.TaskStatus.COMPLETED)

    report = V.verify_tasks([T.get(wrote.id), T.get(removed.id)])
    assert report.problems == ()
    assert report.ok is True
    assert report.complete is True
    assert report.verified is True
    assert report.counts[V.V_CONFIRMED] == 2
    for finding in report.findings:
        assert finding.trusted is True
        assert finding.evidence > 0


def test_an_identical_rewrite_is_a_warning_and_never_a_contradiction(tmp_path):
    """`projectdoc`'s rule: an identical rewrite is not a write."""
    sid = _session()
    task = _task(sid, "wrote the same bytes")
    target = tmp_path / "same.txt"
    target.write_text("unchanged\n", encoding="utf-8")
    _file_call(sid, task.id, "write_file", target, after=lambda: None)
    T.set_status(task.id, T.TaskStatus.COMPLETED)

    finding = V.verify_task(T.get(task.id))
    writes = next(c for c in finding.checks if c.name == V.C_WRITES)
    assert writes.problems == ()
    assert any("byte-for-byte" in w for w in writes.warnings)
    assert finding.verdict == V.V_CONFIRMED, "a no-op write is still a write"


def test_a_row_that_already_failed_is_reported_once_not_twice(tmp_path):
    """A refused `write_file` legitimately leaves nothing behind."""
    sid = _session()
    task = _task(sid, "refused")
    target = tmp_path / "never.txt"
    target.write_text("x\n", encoding="utf-8")
    _file_call(sid, task.id, "write_file", target, after=lambda: target.unlink(),
               ok=False)
    T.set_status(task.id, T.TaskStatus.COMPLETED)

    finding = V.verify_task(T.get(task.id))
    tools = next(c for c in finding.checks if c.name == V.C_TOOLS)
    writes = next(c for c in finding.checks if c.name == V.C_WRITES)
    assert len(tools.problems) == 1
    assert writes.checked == 0, "the failed row must be skipped, not re-reported"
    assert writes.problems == ()


def test_unmeasured_paths_are_named_as_unverifiable_rather_than_assumed(tmp_path):
    sid = _session()
    task = _task(sid, "named more than it measured", status=T.TaskStatus.COMPLETED)
    good = tmp_path / "one.txt"
    good.write_text("ok\n", encoding="utf-8")
    _tool_row(sid, task.id, tool="write_file", status=T.STEP_COMPLETED, ok=1,
              detail={"tool": "write_file", "target": "one.txt",
                      "paths": [str(good), "b.txt", "c.txt"], "paths_total": 3,
                      "pre": {}, "post": {str(good): "abc123"}})

    finding = V.verify_task(task)
    writes = next(c for c in finding.checks if c.name == V.C_WRITES)
    assert writes.problems == ()
    assert any("verification of the rest is not possible" in w for w in writes.warnings)


def test_a_kind_the_digest_pair_says_nothing_about_is_not_judged():
    """`read_file` leaves no expectation about presence, so it is skipped."""
    sid = _session()
    task = _task(sid, "just read", status=T.TaskStatus.COMPLETED)
    _tool_row(sid, task.id, tool="read_file", status=T.STEP_COMPLETED, ok=1,
              detail={"tool": "read_file", "target": "a.txt", "paths": ["a.txt"],
                      "paths_total": 1, "pre": {}, "post": {"a.txt": None}})

    finding = V.verify_task(task)
    writes = next(c for c in finding.checks if c.name == V.C_WRITES)
    assert writes.checked == 0
    assert writes.problems == ()
    assert finding.verdict == V.V_CONFIRMED     # the tools check still saw it


# ══ 7. Session-level evidence is its own check ═══════════════════════════════

def test_an_unattributed_command_lands_on_the_report_not_on_every_node():
    """One failed command may not contradict a whole graph."""
    sid = _session()
    a = _task(sid, "a", status=T.TaskStatus.COMPLETED)
    b = _task(sid, "b", status=T.TaskStatus.COMPLETED)
    _cmd_row(sid, "", status="failed", exit_code=1, command="orphan")

    report = V.verify_tasks([a, b], ref="run-1")
    assert report.ref == "run-1"
    assert [f.verdict for f in report.findings] == [V.V_UNCONFIRMED] * 2
    for finding in report.findings:
        assert finding.problems == ()
    assert report.session, "the loose row must land somewhere"
    assert any("session: " in line for line in report.problems)
    assert report.ok is False, "and it must still cost the report its ok"


def test_with_neither_a_session_nor_a_task_list_nothing_is_read_at_all(monkeypatch):
    """An unscoped ledger read is not a verification of anything."""
    with _queries(monkeypatch) as calls:
        ev = V.evidence_for()
    assert ev.reads == 0
    assert ev.by_task == {}
    assert ev.session == {"commands": [], "tools": []}
    assert ev.truncated is False
    assert calls == {"qall": 0, "qone": 0}


def test_a_session_id_replaces_the_project_filter_rather_than_adding_to_it():
    """A run started elsewhere must still be verifiable from here."""
    sid = _session()
    task = _task(sid, "elsewhere", status=T.TaskStatus.COMPLETED)
    row_id = _cmd_row(sid, task.id, status="completed", exit_code=0)
    db.exe("UPDATE exec_commands SET project=? WHERE id=?",
           ("/some/other/checkout", row_id))

    ev = V.evidence_for(session_id=sid, task_ids=(task.id,))
    assert ev.reads == 2
    assert len(ev.for_task(task.id)["commands"]) == 1
    assert V.verify_task(task).verdict == V.V_CONFIRMED


# ══ 8. Cost, totality, and what may never appear ═════════════════════════════

def test_the_ledgers_are_read_twice_whatever_the_node_count(monkeypatch):
    """A per-node ledger read is invisible at 3 nodes and 48 queries at 24."""
    small_sid = _session()
    small = [_task(small_sid, f"s{i}", status=T.TaskStatus.COMPLETED) for i in range(3)]
    big_sid = _session()
    big = [_task(big_sid, f"b{i}", status=T.TaskStatus.COMPLETED) for i in range(24)]

    with _queries(monkeypatch) as calls:
        V.verify_tasks(small)
        low = dict(calls)
        calls.update(qall=0, qone=0)
        V.verify_tasks(big)
        high = dict(calls)

    assert low == high, f"cost scaled with node count: {low} vs {high}"
    assert low == {"qall": 2, "qone": 0}
    assert V.describe()["reads_per_report"] == 2


def test_an_id_that_names_nothing_is_open_and_does_not_raise():
    finding = V.verify_task("no-such-task-id")
    assert finding.ref == "no-such-task-id"
    assert finding.verdict == V.V_OPEN
    assert finding.checks == ()
    assert finding.problems == ()

    report = V.verify_tasks(["no-such-task-id", ""])
    assert len(report.findings) == 1
    assert report.complete is False
    assert report.verified is False


def test_an_unreadable_ledger_errs_toward_unconfirmed_never_confirmed(monkeypatch):
    """Totality with a direction: the asymmetry IS the design."""
    sid = _session()
    task = _task(sid, "unverifiable", status=T.TaskStatus.COMPLETED)

    def boom(*a, **k):
        raise RuntimeError("ledger is gone")

    monkeypatch.setattr(X, "commands", boom)
    monkeypatch.setattr(X, "tool_calls", boom)

    ev = V.evidence_for(session_id=sid, task_ids=(task.id,))
    assert ev.reads == 0
    assert ev.by_task == {}

    finding = V.verify_task(task)
    assert finding.verdict == V.V_UNCONFIRMED
    assert finding.verdict != V.V_CONFIRMED
    assert finding.trusted is False
    report = V.verify_tasks([task])
    assert report.counts[V.V_CONFIRMED] == 0
    assert report.counts[V.V_UNCONFIRMED] == 1
    assert all(not f.trusted for f in report.findings)


def test_a_legacy_detail_column_reads_as_cannot_verify_not_as_agreement():
    sid = _session()
    task = _task(sid, "old row", status=T.TaskStatus.COMPLETED)
    _tool_row(sid, task.id, tool="write_file", status=T.STEP_COMPLETED, ok=1,
              detail=None)

    finding = V.verify_task(task)
    tools = next(c for c in finding.checks if c.name == V.C_TOOLS)
    writes = next(c for c in finding.checks if c.name == V.C_WRITES)
    assert tools.problems == ()
    assert any("predates structured recording" in w for w in tools.warnings)
    assert writes.checked == 0


def test_the_row_cap_floors_at_fifty_and_is_read_live(monkeypatch):
    """A knob bound at import would freeze, so `_cap()` reads the module.

    ⚠️ The floor is why the truncation test below has to insert fifty rows: a knob
    of 1 cannot produce a cap of 1. A cap small enough to hide most of a run's
    evidence would make "nothing contradicted it" mean "we barely looked".
    """
    monkeypatch.setattr(cfg, "VERIFY_MAX_ROWS", 1, raising=False)
    assert V._cap() == 50
    monkeypatch.setattr(cfg, "VERIFY_MAX_ROWS", 900, raising=False)
    assert V._cap() == 900
    monkeypatch.setattr(cfg, "VERIFY_MAX_ROWS", "nonsense", raising=False)
    cap = V._cap()
    assert cap >= 50, "an unreadable knob must land on a bounded default, not raise"
    monkeypatch.delattr(cfg, "VERIFY_MAX_ROWS", raising=False)
    assert V._cap() >= 50, "a knob a build has not got yet must not be a zero cap"


def test_the_cap_is_reported_rather_than_quietly_confirming_what_was_not_read(monkeypatch):
    monkeypatch.setattr(cfg, "VERIFY_MAX_ROWS", 1, raising=False)
    sid = _session()
    task = _task(sid, "very busy", status=T.TaskStatus.COMPLETED)
    for _ in range(V._cap()):
        _cmd_row(sid, task.id, status="completed", exit_code=0)

    ev = V.evidence_for(session_id=sid, task_ids=(task.id,))
    assert ev.truncated is True

    report = V.verify_tasks([task])
    assert report.truncated is True
    assert any("AGENT2_VERIFY_MAX_ROWS" in w for w in report.warnings)
    assert report.to_payload()["truncated"] is True


def test_there_is_no_off_switch_for_verification():
    """`AGENT2_SKILLS=0` and `AGENT2_METRICS=0` exist; this may not.

    A verifier a deployment can silently disable is a verifier that reports
    "confirmed" by default, which is the one state the spec forbids.
    """
    text = Path(V.__file__).read_text(encoding="utf-8")
    assert "VERIFY_MAX_ROWS" in text
    for knob in ("VERIFY_ENABLED", "AGENT2_VERIFY=", "VERIFY_OFF", "VERIFY_DISABLED"):
        assert knob not in text, f"{knob} would make verification optional"


def test_verify_imports_neither_the_dag_nor_the_workflow_package():
    """The verifier is shared infrastructure, so it may not know its consumers.

    PART 10's rule is ONE shared verification system; an import of `core.dag` or
    `core.workflow` here would be the `if workflow:` the DAG core is structurally
    forbidden to contain, moved one module over. Checked on the **AST**, not on the
    text: the module's own docstring quotes the forbidden shape in order to explain
    it, and a substring scan would fail on the explanation rather than on the code.
    """
    tree = ast.parse(Path(V.__file__).read_text(encoding="utf-8"))
    seen: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            seen.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            seen.append(base)
            seen.extend(f"{base}.{a.name}" for a in node.names)
    banned = ("agent2.core.dag", "agent2.core.workflow")
    for name in seen:
        assert not any(name.startswith(b) for b in banned), f"forbidden import: {name}"

    features = ("workflow", "ultracode", "dag", "security", "zap", "burp", "skill")
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        for inner in ast.walk(node.test):
            word = ""
            if isinstance(inner, ast.Name):
                word = inner.id
            elif isinstance(inner, ast.Attribute):
                word = inner.attr
            elif isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                word = inner.value
            low = word.lower()
            assert not any(f in low for f in features), (
                f"a branch on the feature word {word!r} at line {node.lineno}")


def test_no_payload_carries_a_files_contents(tmp_path):
    """A report names paths and verdicts. It never quotes what was written."""
    sid = _session()
    task = _task(sid, "wrote a secret")
    target = tmp_path / "creds.txt"
    target.write_text("placeholder\n", encoding="utf-8")
    _file_call(sid, task.id, "write_file", target,
               after=lambda: target.write_text(SECRET_BODY + "\n", encoding="utf-8"))
    T.set_status(task.id, T.TaskStatus.COMPLETED)

    report = V.verify_tasks([T.get(task.id)])
    blob = json.dumps(report.to_payload())
    assert report.verified is True
    assert SECRET_BODY not in blob


def test_the_payload_reports_three_booleans_and_no_single_verdict():
    """`ok`, `complete` and `verified` are three questions, deliberately not one."""
    sid = _session()
    task = _task(sid, "unit", status=T.TaskStatus.COMPLETED)
    payload = V.verify_tasks([task]).to_payload()
    assert set(payload) == {"ref", "ok", "complete", "verified", "truncated",
                            "counts", "problems", "warnings", "findings", "session"}
    assert "verdict" not in payload, "a report is not one verdict"
    assert set(payload["counts"]) == set(V.VERDICTS)
    assert payload["findings"][0]["verdict"] == V.V_UNCONFIRMED
    assert payload["findings"][0]["trusted"] is False


# ══ 9. The audit line ════════════════════════════════════════════════════════

def test_every_verdict_is_logged_not_only_the_bad_one(monkeypatch):
    """Silence for the confirmed half would make the audit trail unreadable:
    a run with no `verification_reported` line would be indistinguishable from a
    run nobody verified."""
    reported: list[dict] = []
    contradicted: list[dict] = []
    monkeypatch.setattr(V.alog, "verification_reported",
                        lambda ref, **kw: reported.append({"ref": ref, **kw}))
    monkeypatch.setattr(V.alog, "verification_contradicted",
                        lambda ref, **kw: contradicted.append({"ref": ref, **kw}))

    sid = _session()
    good = _task(sid, "good", status=T.TaskStatus.COMPLETED)
    bad = _task(sid, "bad", status=T.TaskStatus.COMPLETED)
    _cmd_row(sid, bad.id, status="failed", exit_code=1, command="false")

    V.verify_tasks([good, bad])
    assert {r["ref"] for r in reported} == {good.id, bad.id}
    assert [r["ref"] for r in contradicted] == [bad.id]
    assert all("verdict" in r for r in reported)


def test_a_broken_audit_sink_costs_the_line_and_never_the_verdict(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("log is gone")

    monkeypatch.setattr(V.alog, "verification_reported", boom)
    monkeypatch.setattr(V.alog, "verification_contradicted", boom)

    sid = _session()
    task = _task(sid, "unit", status=T.TaskStatus.COMPLETED)
    report = V.verify_tasks([task])
    assert report.findings[0].verdict == V.V_UNCONFIRMED
