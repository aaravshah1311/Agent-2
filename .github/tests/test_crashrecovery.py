# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
Tests for Phase 8's crash and safe recovery — ``agent2/core/recovery/safety.py``
(Task 26), ``agent2/core/recovery/classify.py`` (Task 25 §3 / Task 26 §3),
``agent2/core/recovery/crash.py`` (Task 25) and the `/api/recovery/*` routes that
expose them.

Run from the repo root:  python -m pytest .github/tests/test_crashrecovery.py -v

What this suite is for
──────────────────────
`test_execstate.py` proves the ledger *records* what was in flight. This file
proves the three questions asked of those rows by a LATER process are answered
correctly: *what kind of operation was it* (safety), *may it be repeated* (classify)
and *what did we actually do about it* (crash). Every scenario is built by writing
ledger rows with a controlled instance and a backdated `updated_at` — ageing a row
stands in for the passage of time, which is what makes "the previous process died
here" deterministic instead of a hand-run `kill -9`.

The load-bearing tests
──────────────────────
* ``test_a_chained_delete_is_a_delete_not_a_read`` — Task 26's own example.
  `ls && rm -rf build` starts with `ls`, and a first-word classifier calls the whole
  line a READ, which licenses recovery to re-run a delete. Written as a PAIR
  (`ls -la` must stay READ) because the strict assertion alone would also pass
  against a classifier that answered DELETE for everything.
* ``test_the_disposition_table_covers_every_kind`` — the assertion
  `safety._DISPOSITION`'s own docstring asks for. The fallback is already safe, so a
  missing row produces no symptom; without this the next person reasonably concludes
  the default IS the rule.
* ``test_d_never_still_verifies`` — `D_NEVER` is a refinement of "verify", not a
  contradiction of it. Collapse the two and a `git push` reaches a human with no
  evidence attached to the entry they have to judge.
* ``test_the_capability_is_rechecked_live`` — Task 26 §9. A permission taken away
  after the crash must be honoured; a remembered "it was allowed then" is what turns
  recovery into a way to launder `AGENT2_DENY_CAPS`.
* ``test_a_live_owner_is_never_touched`` — dual mode's whole safety margin, and a
  PAIR for the same reason as above: the fresh row must be skipped AND the orphaned
  copy of it must be processed, or a reader that found nothing would pass.
* ``test_the_scan_is_idempotent`` — a second scan (a manual trigger, the other
  surface, a relaunch) must converge on the same row rather than re-decide it.
* ``test_completed_work_is_never_re_run`` — rule 20, from the ledger side.
* ``test_the_task_verifier_reads_the_verdict_word_off_the_dict`` — the drift
  `classify._verify_task` documents: `verify_step()` returns an evidence *dict*, and
  appending it instead of its `verdict` makes `_fold` raise `unhashable type:
  'dict'`, which `verify()` swallows into `V_UNVERIFIABLE`. It fails in the SAFE
  direction, so nothing else in the system would ever report it.
* ``test_no_command_line_reaches_the_recovery_surface`` — the payload is written on
  a path that has just proven the machine's state is unknown, and `run_command` argv
  routinely carries a token.
* ``test_the_scan_route_is_not_the_plan_route`` — the pin `routes.py` asks for by
  name: `/api/recovery/scan` sits in front of `/api/recovery/<sid>`.

conftest.py redirects AGENT2_DB to a throwaway temp DB, so nothing here touches the
developer's real agent2.db.
"""

import json
import os
import time

import pytest
from flask import Flask

from agent2 import config as cfg
from agent2 import database as db
from agent2.core import commands as C
from agent2.core import execstate as X
from agent2.core import permissions as perms
from agent2.core import procio
from agent2.core import recovery as R
from agent2.core import tasks as T
from agent2.core.recovery import classify as CL
from agent2.core.recovery import crash as CR
from agent2.core.recovery import safety as S
from agent2.server.routes import register_routes

DEAD = "99999-deadbeef"          # an instance id no live process can own
LEGACY = "no-pid-at-all"         # an instance string carrying no pid

# A value that must never appear in any recovery payload. One token, so a single
# `json.dumps()` scan can prove it.
SECRET = "sk-live-DO-NOT-PERSIST-4f2b91"

TABLES = ("exec_commands", "exec_tool_calls", "exec_workflows", "exec_recovery")


@pytest.fixture(autouse=True)
def _clean():
    """Clear both halves — the ledger and the recovery record.

    ⚠️ `crash.reset()` deliberately keeps `exec_recovery` rows (they are the durable
    record of decisions already taken), so the rows are deleted here explicitly. The
    suite shares one database and other files write exec rows as a side effect of
    exercising `commands.py` and `dispatch_tool`.
    """
    db.init_db()
    X.reset()
    C.reset()
    CR.reset()
    for table in TABLES:
        db.exe(f"DELETE FROM {table}")
    X._failure_reported = False
    yield
    for table in TABLES:
        db.exe(f"DELETE FROM {table}")
    X.reset()
    C.reset()
    CR.reset()
    X._failure_reported = False


@pytest.fixture
def proj(tmp_path):
    """A project of our own, so a scan cannot see another test's rows."""
    return str(tmp_path)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _stamp(ago: float = 0.0) -> str:
    """A `core/tasks._now()`-shaped UTC stamp *ago* seconds in the past."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - ago))


def _command(row_id: str = "cmd-1", *, command: str = "ls -la",
             status: str = C.CommandStatus.RUNNING, pid=None, project: str = "",
             session_id: str = "", task_id: str = "", lines: int = 0,
             instance: str = DEAD, age: float = 3600.0) -> str:
    """One `exec_commands` row as a dead process would have left it."""
    stamp = _stamp(age)
    db.exe(
        "INSERT INTO exec_commands(id, instance, project, session_id, task_id,"
        " surface, term_id, command, process_id, status, output_lines,"
        " created_at, started_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (row_id, instance, project, session_id, task_id, "cli", "t1", command, pid,
         status, lines, stamp, stamp, stamp),
    )
    return row_id


def _detail(tool: str, paths, *, pre=None, post=None, post_seen: bool = False) -> str:
    """`execstate._detail_json`'s shape, built by hand so pre/post are controlled."""
    every = [str(p) for p in (paths or [])]
    return json.dumps({
        "tool": tool, "target": (every[0] if every else ""),
        "paths": every, "paths_total": len(every),
        "pre": dict(pre or {}),
        "post": (dict(post or {}) if post_seen else None),
    })


def _tool_call(row_id: str = "tc-1", *, tool: str = "write_file", paths=(),
               pre=None, post=None, post_seen: bool = False,
               status: str = T.STEP_RUNNING, project: str = "",
               session_id: str = "", task_id: str = "", destructive: int = 1,
               instance: str = DEAD, age: float = 3600.0, detail=None) -> str:
    """One `exec_tool_calls` row. `post_seen=False` ⇒ `post: null` — the crash signal."""
    stamp = _stamp(age)
    body = _detail(tool, paths, pre=pre, post=post, post_seen=post_seen) \
        if detail is None else detail
    db.exe(
        "INSERT INTO exec_tool_calls(id, instance, project, session_id, task_id,"
        " surface, tool, detail, destructive, status, started_at, updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (row_id, instance, project, session_id, task_id, "cli", tool, body,
         destructive, status, stamp, stamp),
    )
    return row_id


def _workflow(row_id: str = "wf-1", *, name: str = "security-audit",
              step: str = "zap scan", index: int = 3, total: int = 8,
              status: str = T.STEP_RUNNING, project: str = "",
              instance: str = DEAD, age: float = 3600.0) -> str:
    stamp = _stamp(age)
    db.exe(
        "INSERT INTO exec_workflows(id, instance, project, name, status, step,"
        " step_index, total_steps, started_at, updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (row_id, instance, project, name, status, step, index, total, stamp, stamp),
    )
    return row_id


def _ledger(table: str, row_id: str) -> dict:
    return db.qone(f"SELECT * FROM {table} WHERE id=?", (row_id,)) or {}


def _rec(kind: str, ref_id: str) -> dict:
    return db.qone("SELECT * FROM exec_recovery WHERE id=?",
                   (f"{kind}:{ref_id}",)) or {}


def _unit(report: dict, ref_id: str) -> dict:
    for entry in report.get("units") or []:
        if entry.get("ref_id") == ref_id:
            return entry
    return {}


def _file(tmp_path, name: str, body: str = "before") -> str:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return str(path)


def _assessment(operation: str, classification: str, verdict: str = ""):
    """A hand-built `Assessment` — for the pure-predicate tests."""
    return CL.Assessment(kind=CL.K_TOOL_CALL, ref_id="hand", operation=operation,
                         disposition=S.disposition(operation),
                         classification=classification, verdict=verdict)


# ═══════════════════════════════════════════════════════════════════════════════
# Task 26 §1 — safety.py: may this KIND of operation be repeated?
# ═══════════════════════════════════════════════════════════════════════════════

def test_a_chained_delete_is_a_delete_not_a_read():
    """⚠️ Task 26's example, and the reason `kind_for_command` folds every segment.

    A PAIR: the harmless line must stay READ, or a classifier that answered DELETE
    for everything would pass the half that matters.
    """
    assert S.kind_for_command("ls -la") == S.READ
    assert S.kind_for_command("ls && rm -rf build") == S.DELETE
    # Every operator that can chain, not just `&&`.
    assert S.kind_for_command("ls; rm -rf build") == S.DELETE
    assert S.kind_for_command("cat x || rm x") == S.DELETE
    assert S.kind_for_command("ls\nrm -rf build") == S.DELETE
    assert S.kind_for_command("find . -name '*.o' | xargs rm") == S.DELETE
    # And the strictest segment wins wherever it sits, not just last.
    assert S.kind_for_command("git push origin main && ls") == S.GIT_PUSH


def test_the_disposition_table_covers_every_kind():
    """The assertion `_DISPOSITION`'s docstring asks for by name.

    The fallback is D_VERIFY, which is safe — so a missing row has NO symptom, and
    "safe by accident" is how the next reader concludes the default is the rule.
    """
    for kind in S.KINDS:
        assert kind in S._DISPOSITION, f"no disposition for {kind}"
        assert S._DISPOSITION[kind] in S.DISPOSITIONS
        assert S.capability_for(kind) in perms.ALL_CAPS
    assert len(S.table()) == len(S.KINDS)
    assert {row["kind"] for row in S.table()} == set(S.KINDS)


def test_a_redirection_promotes_a_read_to_a_write():
    """`cat a > b` reads nothing of interest and writes `b`."""
    assert S.kind_for_command("cat notes.txt") == S.READ
    assert S.kind_for_command("cat notes.txt > copy.txt") == S.WRITE
    assert S.kind_for_command("echo hi >> log.txt") == S.WRITE
    # ⚠️ It PROMOTES; it never demotes. A delete with a log redirect is still a
    # delete, which is what `_rank(found) < _rank(WRITE)` is guarding.
    assert S.kind_for_command("rm -rf build > log.txt") == S.DELETE


def test_d_never_still_verifies():
    """⚠️ `D_NEVER` refines "verify"; it does not contradict it."""
    for kind in (S.GIT_PUSH, S.EXTERNAL_API_MUTATION, S.DEPLOYMENT):
        assert S.disposition(kind) == S.D_NEVER
        assert S.needs_verification(kind) is True, kind
        assert S.never_repeats(kind) is True, kind
        assert S.may_retry(kind) is False, kind
    # The contrast: a pure read is the only thing that skips verification.
    assert S.needs_verification(S.READ) is False
    assert S.may_retry(S.READ) is True
    assert S.never_repeats(S.DELETE) is False   # verified, then possibly retried


def test_an_unknown_tool_falls_to_the_strict_end():
    """A tool this build has never heard of is UNKNOWN/D_VERIFY, never READ/D_RETRY."""
    assert S.kind_for_tool("frobnicate_the_thing") == S.UNKNOWN
    assert S.disposition(S.UNKNOWN) == S.D_VERIFY
    assert S.may_retry(S.UNKNOWN) is False
    # `run_command` routes through the argv classifier, so it answers SHELL here.
    assert S.kind_for_tool("run_command") == S.SHELL
    # An MCP tool drives an external product, and half of them start scans.
    assert S.kind_for_tool("burp_send_to_repeater") == S.EXTERNAL_API_MUTATION
    assert S.kind_for_tool("zap_active_scan") == S.EXTERNAL_API_MUTATION
    # Every named tool resolves to a real kind — a hole is not distinguishable
    # from a wrong answer.
    for tool, kind in S._TOOL_KIND.items():
        assert kind in S.KINDS
        assert S.kind_for_tool(tool.upper()) == kind, tool


def test_a_prefix_is_not_the_verb():
    """`sudo`, `env` and a leading `VAR=value` are prefixes — walk past them."""
    assert S.kind_for_command("sudo rm -rf /tmp/x") == S.DELETE
    assert S.kind_for_command("env FOO=1 rm /tmp/x") == S.DELETE
    assert S.kind_for_command("FOO=bar ls") == S.READ
    assert S.kind_for_command("nohup rm /tmp/x") == S.DELETE
    # A full path and a Windows extension are the same verb.
    assert S.kind_for_command("/usr/bin/rm -f x") == S.DELETE
    assert S.kind_for_command("C:\\Windows\\System32\\where.exe python") == S.READ


def test_a_package_managers_subcommand_decides():
    assert S.kind_for_command("pip list") == S.READ
    assert S.kind_for_command("pip install requests") == S.DEPLOYMENT
    assert S.kind_for_command("npm ls") == S.READ
    assert S.kind_for_command("npm ci") == S.DEPLOYMENT
    assert S.kind_for_command("cargo") == S.DEPLOYMENT      # no subcommand ⇒ strict


def test_git_is_classified_by_its_verb():
    assert S.kind_for_command("git status --porcelain") == S.READ
    assert S.kind_for_command("git commit -m 'wip'") == S.GIT_COMMIT
    assert S.kind_for_command("git push origin main") == S.GIT_PUSH
    assert S.kind_for_command("git reset --hard HEAD~1") == S.DELETE
    assert S.kind_for_command("git") == S.READ              # bare `git` prints help
    assert S.kind_for_command("git frobnicate") == S.SHELL   # unknown verb ⇒ verify


def test_an_interpreter_is_shell():
    """An interpreter's effects are its script's, which we cannot read."""
    for line in ("python build.py", "node deploy.js", "bash setup.sh",
                 "powershell -File x.ps1"):
        assert S.kind_for_command(line) == S.SHELL, line
    assert S.disposition(S.SHELL) == S.D_VERIFY


def test_the_capability_is_rechecked_live(monkeypatch):
    """Task 26 §9 — a permission taken away AFTER the crash is still honoured.

    `permissions._env()` reads `os.environ` on every call, so this is the real gate
    answering, not a test double.
    """
    monkeypatch.delenv("AGENT2_DENY_CAPS", raising=False)
    assert S.permitted(S.DELETE) is True
    assert S.capability_for(S.DELETE) == perms.CAP_FS_DELETE

    monkeypatch.setenv("AGENT2_DENY_CAPS", "fs.delete")
    assert S.permitted(S.DELETE) is False
    assert S.permitted(S.READ) is True          # only what was subtracted is gone

    monkeypatch.setenv("AGENT2_DENY_CAPS", "all")
    assert S.permitted(S.READ) is True          # `all` spares read, by design
    assert S.permitted(S.WRITE) is False


def test_the_classifier_is_total():
    """A classifier that raised would take down the scan that called it."""
    for junk in (None, "", "   ", 0, [], {}, b"rm -rf /"):
        assert S.kind_for_command(junk) in S.KINDS
        assert S.disposition(S.kind_for_command(junk)) in S.DISPOSITIONS
    for junk in (None, "", 123, object()):
        assert S.kind_for_tool(junk) in S.KINDS
    assert S.kind_for_command("rm -rf x " + ("a" * 1_000_000)) == S.DELETE
    assert S.disposition(None) == S.D_VERIFY
    assert S.capability_for("no-such-kind") == perms.CAP_DESTRUCTIVE


def test_describe_folds_an_unknown_word_to_unknown():
    assert S.describe("no-such-kind")["kind"] == S.UNKNOWN
    assert S.describe("")["kind"] == S.UNKNOWN
    row = S.describe(S.GIT_PUSH)
    assert (row["disposition"], row["never_repeats"], row["needs_verification"]) \
        == (S.D_NEVER, True, True)


# ═══════════════════════════════════════════════════════════════════════════════
# Task 25 §3 / Task 26 §3 — classify.py: what happened, and may we repeat it?
# ═══════════════════════════════════════════════════════════════════════════════

def test_a_command_that_never_started_is_safe_to_retry():
    """No pid AND no start means no child process ever existed."""
    _command("c-new", command="rm -rf build", status=C.CommandStatus.CREATED)
    out = CL.assess(CL.K_COMMAND, _ledger("exec_commands", "c-new"))
    assert out.classification == CL.R_SAFE_TO_RETRY
    assert "never started" in out.reason
    # ⚠️ Even for a DELETE: nothing can be half-done when nothing ran.
    assert out.operation == S.DELETE
    assert CL.safe_to_repeat(out) is True


def test_a_chained_delete_command_requires_verification():
    """The kind comes from the argv, not from "it is a command"."""
    _command("c-del", command="ls && rm -rf build", pid=424242,
             status=C.CommandStatus.RUNNING, lines=3)
    out = CL.assess(CL.K_COMMAND, _ledger("exec_commands", "c-del"))
    assert out.operation == S.DELETE
    assert out.classification == CL.R_REQUIRES_VERIFICATION
    assert out.evidence["output_lines"] == 3
    assert CL.safe_to_repeat(out) is False      # no verdict yet


def test_a_settled_row_is_never_recovered():
    """Rule 20 at the classifier: COMPLETED work is not recoverable work."""
    _command("c-done", status=C.CommandStatus.COMPLETED)
    out = CL.assess(CL.K_COMMAND, _ledger("exec_commands", "c-done"))
    assert out.classification == CL.R_NON_RECOVERABLE
    assert "already settled" in out.reason
    _tool_call("t-done", status=T.STEP_COMPLETED)
    out = CL.assess(CL.K_TOOL_CALL, _ledger("exec_tool_calls", "t-done"))
    assert out.classification == CL.R_NON_RECOVERABLE


def test_a_read_only_tool_call_is_safe_to_retry():
    _tool_call("t-read", tool="read_file", paths=("/tmp/x",), destructive=0)
    out = CL.assess(CL.K_TOOL_CALL, _ledger("exec_tool_calls", "t-read"))
    assert out.classification == CL.R_SAFE_TO_RETRY
    assert out.operation == S.READ


def test_a_tool_call_with_no_evidence_at_all_is_non_recoverable():
    """A legacy row, or one written while EXEC_PERSIST was off. Honest end of road."""
    _tool_call("t-blind", tool="delete_file", detail="")
    out = CL.assess(CL.K_TOOL_CALL, _ledger("exec_tool_calls", "t-blind"))
    assert out.classification == CL.R_NON_RECOVERABLE
    assert "no recorded evidence" in out.reason


def test_a_recorded_post_state_means_only_the_settle_was_lost(tmp_path):
    """The strongest evidence there is — the call finished, the settle did not."""
    path = _file(tmp_path, "out.txt", "after")
    _tool_call("t-post", tool="write_file", paths=(path,),
               pre={path: None}, post={path: X.digests([path])[path]},
               post_seen=True)
    row = _ledger("exec_tool_calls", "t-post")
    out = CL.verify(CL.assess(CL.K_TOOL_CALL, row), row)
    assert out.verdict == R.V_LIKELY_APPLIED
    assert "only the settle was lost" in out.reason
    assert CL.safe_to_repeat(out) is False


def test_an_untouched_target_is_verified_not_applied_and_may_be_repeated(tmp_path):
    """pre == disk ⇒ the write never landed ⇒ the one licence to repeat."""
    path = _file(tmp_path, "keep.txt", "before")
    _tool_call("t-clean", tool="write_file", paths=(path,),
               pre={path: X.digests([path])[path]})
    row = _ledger("exec_tool_calls", "t-clean")
    out = CL.verify(CL.assess(CL.K_TOOL_CALL, row), row)
    assert out.verdict == R.V_NOT_APPLIED
    assert out.evidence["checked"] == 1 and out.evidence["changed"] == 0
    assert CL.safe_to_repeat(out) is True


def test_a_partly_applied_call_is_uncertain(tmp_path):
    """Targets disagree ⇒ the call landed only partly ⇒ never repeated."""
    same = _file(tmp_path, "same.txt", "before")
    moved = _file(tmp_path, "moved.txt", "before")
    pre = {same: X.digests([same])[same], moved: X.digests([moved])[moved]}
    (tmp_path / "moved.txt").write_text("AFTER", encoding="utf-8")
    _tool_call("t-part", tool="multi_edit_files", paths=(same, moved), pre=pre)
    row = _ledger("exec_tool_calls", "t-part")
    out = CL.verify(CL.assess(CL.K_TOOL_CALL, row), row)
    assert out.verdict == R.V_UNCERTAIN
    assert out.evidence["changed"] == 1
    assert CL.safe_to_repeat(out) is False


def test_a_delete_whose_target_is_gone_is_likely_applied(tmp_path):
    """Absence IS the desired end state — however it was reached."""
    path = str(tmp_path / "gone.txt")
    _tool_call("t-del", tool="delete_file", paths=(path,),
               pre={path: "sha256:" + "0" * 64})
    row = _ledger("exec_tool_calls", "t-del")
    out = CL.verify(CL.assess(CL.K_TOOL_CALL, row), row)
    assert out.operation == S.DELETE
    assert out.verdict == R.V_LIKELY_APPLIED
    assert CL.safe_to_repeat(out) is False


def test_a_command_is_honestly_unverifiable():
    """Task 24 records no target paths for a shell command, so there is no evidence.

    The correct answer is "we cannot tell", not a confident guess.
    """
    _command("c-shell", command="./deploy.sh", pid=9, lines=12)
    row = _ledger("exec_commands", "c-shell")
    out = CL.verify(CL.assess(CL.K_COMMAND, row), row)
    assert out.verdict == R.V_UNVERIFIABLE
    assert out.evidence["output_lines"] == 12
    assert CL.safe_to_repeat(out) is False


def test_verify_is_a_no_op_for_a_class_that_cannot_use_one():
    """⚠️ Only `R_REQUIRES_VERIFICATION` is verified.

    Attaching evidence to any other class reads like a licence next to a class that
    is not one.
    """
    for cls in (CL.R_SAFE_TO_RETRY, CL.R_SAFE_TO_RESUME, CL.R_NON_RECOVERABLE,
                CL.R_UNKNOWN):
        out = CL.verify(_assessment(S.DELETE, cls), {})
        assert out.verdict == "", cls


def test_verification_writes_nothing(tmp_path):
    """⚠️ READ-ONLY, WITHOUT EXCEPTION — a verifier that could act would be an
    approval gate nobody designed."""
    keep = _file(tmp_path, "a.txt", "one")
    other = _file(tmp_path, "b.txt", "two")
    ghost = str(tmp_path / "never.txt")

    def snapshot():
        return sorted((n, os.path.getsize(tmp_path / n),
                       int(os.path.getmtime(tmp_path / n)))
                      for n in os.listdir(tmp_path))

    before = snapshot()
    _tool_call("t-ro", tool="write_file", paths=(keep, other, ghost),
               pre={keep: "sha256:" + "1" * 64})
    row = _ledger("exec_tool_calls", "t-ro")
    CL.verify(CL.assess(CL.K_TOOL_CALL, row), row)
    assert snapshot() == before
    assert not os.path.exists(ghost)


def test_safe_to_repeat_needs_every_condition(monkeypatch):
    """⚠️ THE ONLY PLACE the five conditions are ANDed. Each one, alone, is a veto."""
    monkeypatch.delenv("AGENT2_DENY_CAPS", raising=False)
    # 1. the class permits it
    assert CL.safe_to_repeat(_assessment(S.READ, CL.R_SAFE_TO_RETRY)) is True
    assert CL.safe_to_repeat(_assessment(S.READ, CL.R_SAFE_TO_RESUME)) is False
    assert CL.safe_to_repeat(_assessment(S.READ, CL.R_NON_RECOVERABLE)) is False
    # 2. the kind is not D_NEVER — even classified as retryable
    assert CL.safe_to_repeat(_assessment(S.GIT_PUSH, CL.R_SAFE_TO_RETRY)) is False
    # 4. a unit needing verification must actually have been verified
    assert CL.safe_to_repeat(
        _assessment(S.WRITE, CL.R_REQUIRES_VERIFICATION)) is False
    assert CL.safe_to_repeat(
        _assessment(S.WRITE, CL.R_REQUIRES_VERIFICATION, R.V_NOT_APPLIED)) is True
    # 5. nothing verified as applied, partly applied or unmeasurable
    for verdict in (R.V_LIKELY_APPLIED, R.V_UNCERTAIN, R.V_UNVERIFIABLE):
        assert CL.safe_to_repeat(
            _assessment(S.WRITE, CL.R_REQUIRES_VERIFICATION, verdict)) is False
    # 3. the capability, asked live
    monkeypatch.setenv("AGENT2_DENY_CAPS", "fs.write")
    assert CL.safe_to_repeat(
        _assessment(S.WRITE, CL.R_REQUIRES_VERIFICATION, R.V_NOT_APPLIED)) is False
    assert CL.safe_to_repeat(_assessment(S.READ, CL.R_SAFE_TO_RETRY)) is True


def test_a_workflow_resumes_and_never_restarts():
    """⚠️ Completed nodes stay completed — the resume position is `step_index`."""
    _workflow("w-mid", index=3, total=8)
    out = CL.assess(CL.K_WORKFLOW, _ledger("exec_workflows", "w-mid"))
    assert out.classification == CL.R_SAFE_TO_RESUME
    assert out.evidence["step_index"] == 3
    assert "resume at step 3 of 8" in out.reason
    # Nothing completed yet is the ONE case where starting over is honest.
    _workflow("w-zero", index=0, total=8)
    out = CL.assess(CL.K_WORKFLOW, _ledger("exec_workflows", "w-zero"))
    assert out.classification == CL.R_SAFE_TO_RETRY


def test_a_destructive_in_flight_step_demotes_a_task_to_verification(proj, tmp_path):
    """Rule 21 from the classifier's side: that step is exactly the one not to replay."""
    sid = T.open_session(chat_id="c-1", cwd=proj, goal="tidy up")
    T.sync_list(sid, [{"task": "delete the build", "status": "in_progress"}])
    task = T.list_tasks(sid)[0]
    T.record_step(task.id, "delete_file", T.STEP_RUNNING,
                  detail=_file(tmp_path, "build.log"), destructive=True)
    out = CL.assess(CL.K_TASK, T.get(task.id))
    assert out.classification == CL.R_REQUIRES_VERIFICATION
    assert out.operation == S.UNKNOWN            # we no longer claim to know
    assert out.evidence["destructive_pending"] is True
    assert "destructive step was in flight" in out.reason


def test_a_task_with_no_destructive_step_resumes_from_its_checkpoint(proj):
    sid = T.open_session(chat_id="c-2", cwd=proj, goal="read things")
    T.sync_list(sid, [{"task": "read the docs", "status": "in_progress"}])
    task = T.list_tasks(sid)[0]
    T.record_step(task.id, "read_file", T.STEP_COMPLETED, detail="README.md")
    out = CL.assess(CL.K_TASK, T.get(task.id))
    assert out.classification == CL.R_SAFE_TO_RESUME
    assert out.reason == "checkpoint holds the remaining steps"


def test_the_task_verifier_reads_the_verdict_word_off_the_dict(proj, tmp_path):
    """⚠️ THE DOCUMENTED DRIFT TRAP.

    `verify_step()` returns an evidence DICT — `{tool, target, at, verdict,
    evidence}`. Appending the dict instead of its `verdict` makes `_fold()` raise
    `unhashable type: 'dict'` on `set(words)`; `verify()` swallows that into
    `V_UNVERIFIABLE` and the reason below becomes unreachable dead code. It fails in
    the SAFE direction, which is exactly why nothing else would ever report it.
    """
    sid = T.open_session(chat_id="c-3", cwd=proj, goal="tidy up")
    T.sync_list(sid, [{"task": "delete the build", "status": "in_progress"}])
    task = T.list_tasks(sid)[0]
    target = _file(tmp_path, "still-here.log")
    T.record_step(task.id, "delete_file", T.STEP_RUNNING, detail=target,
                  destructive=True)
    out = CL.verify(CL.assess(CL.K_TASK, T.get(task.id)), T.get(task.id))

    assert out.verdict in (R.V_NOT_APPLIED, R.V_LIKELY_APPLIED, R.V_UNCERTAIN,
                           R.V_UNVERIFIABLE)
    assert out.reason.startswith("checkpoint mtime heuristic"), out.reason
    assert "verification failed" not in out.reason
    assert "unhashable" not in out.reason
    assert out.evidence["steps_checked"] == 1
    # The file is still there, so the delete demonstrably did not land.
    assert out.verdict == R.V_NOT_APPLIED
    assert os.path.exists(target)


def test_assess_never_raises():
    """It runs inside the startup scan; a fault must not orphan the other units."""
    for row in (None, {}, {"id": "x"}, object(), 7, "row"):
        for kind in (*CL.KINDS, "", "no-such-kind"):
            out = CL.assess(kind, row)
            assert out.classification in CL.CLASSES
    assert CL.assess("no-such-kind", {"id": "x"}).classification == CL.R_UNKNOWN
    assert CL.describe(CL.assess("", {})).strip() != ""


# ═══════════════════════════════════════════════════════════════════════════════
# Task 25 — crash.py: the scan, the state machine, the human actions
# ═══════════════════════════════════════════════════════════════════════════════

def test_a_live_owner_is_never_touched(proj):
    """⚠️ Dual mode's whole safety margin, and a PAIR.

    A silent `sleep 300` in the CLI half out-ages `EXEC_STALE_SEC` and is swept by
    the web half. If the CLI process is still there, the scan leaves it alone — and
    writes NO recovery row, so there is nothing to explain away afterwards.
    """
    mine = f"{os.getpid()}-cafebabe"        # matches the pid regex, is not INSTANCE
    _command("c-live", command="sleep 300", pid=None,
             status=C.CommandStatus.RUNNING, project=proj, instance=mine)

    first = CR.scan(project=proj, workers=False)
    entry = _unit(first, "c-live")
    assert entry["skipped"] == "owner process is still running"
    assert entry["state"] == CR.S_NORMAL
    assert first["live_owner"] == 1 and first["processed"] == 0
    assert _rec(CL.K_COMMAND, "c-live") == {}          # nothing written at all

    # The same row, now owned by a process that is gone: it MUST be processed.
    db.exe("UPDATE exec_commands SET instance=? WHERE id=?", (DEAD, "c-live"))
    second = CR.scan(project=proj, workers=False)
    entry = _unit(second, "c-live")
    assert entry["skipped"] == ""
    assert second["processed"] == 1
    assert _rec(CL.K_COMMAND, "c-live")["status"] in CR.RESOLVED


def test_the_scan_is_idempotent(proj):
    """A manual trigger, the other surface and a relaunch all converge on one row."""
    _command("c-idem", command="git push origin main", pid=None,
             status=C.CommandStatus.RUNNING, project=proj)
    first = CR.scan(project=proj, workers=False)
    assert first["processed"] == 1
    row = _rec(CL.K_COMMAND, "c-idem")
    decided_at, attempts, decision = row["updated_at"], row["attempts"], row["decision"]

    before = CR.counters()["attempts"]
    second = CR.scan(project=proj, workers=False)
    entry = _unit(second, "c-idem")
    assert entry["skipped"] == "already resolved"
    assert second["processed"] == 0 and second["skipped"] == 1
    assert CR.counters()["attempts"] == before          # no second decision
    again = _rec(CL.K_COMMAND, "c-idem")
    assert (again["attempts"], again["decision"], again["updated_at"]) \
        == (attempts, decision, decided_at)


def test_completed_work_is_never_re_run(proj):
    """Rule 20, from the ledger side: settled rows are not even candidates."""
    _command("c-ok", command="rm -rf build", status=C.CommandStatus.COMPLETED,
             project=proj)
    _tool_call("t-ok", tool="delete_file", status=T.STEP_COMPLETED, project=proj)
    _workflow("w-ok", status=T.STEP_COMPLETED, project=proj)
    _command("c-open", command="rm -rf dist", pid=None,
             status=C.CommandStatus.RUNNING, project=proj)

    report = CR.scan(project=proj, workers=False)
    assert [e["ref_id"] for e in report["units"]] == ["c-open"]
    assert report["found"] == 1
    for kind, ref in ((CL.K_COMMAND, "c-ok"), (CL.K_TOOL_CALL, "t-ok"),
                      (CL.K_WORKFLOW, "w-ok")):
        assert _rec(kind, ref) == {}, ref
    # And their ledger rows are untouched by the sweep.
    assert _ledger("exec_commands", "c-ok")["status"] == C.CommandStatus.COMPLETED
    assert _ledger("exec_tool_calls", "t-ok")["status"] == T.STEP_COMPLETED


def test_work_that_had_already_landed_is_marked_complete_not_repeated(proj, tmp_path):
    """The `post`-recorded case end to end: settled as COMPLETED, never retried."""
    path = _file(tmp_path, "done.txt", "after")
    _tool_call("t-landed", tool="write_file", paths=(path,), project=proj,
               pre={path: None}, post={path: X.digests([path])[path]}, post_seen=True)
    report = CR.scan(project=proj, workers=False)
    entry = _unit(report, "t-landed")
    assert entry["verdict"] == R.V_LIKELY_APPLIED
    assert entry["decision"] == CR.A_MARK_COMPLETE
    assert entry["state"] == CR.S_RECOVERED
    # ⚠️ The ledger row is settled, so a later scan cannot pick it up again.
    assert _ledger("exec_tool_calls", "t-landed")["status"] == T.STEP_COMPLETED
    assert report["retried"] == 0


def test_an_untouched_write_is_the_one_thing_recovery_retries(proj, tmp_path):
    path = _file(tmp_path, "pending.txt", "before")
    _tool_call("t-retry", tool="write_file", paths=(path,), project=proj,
               pre={path: X.digests([path])[path]})
    report = CR.scan(project=proj, workers=False)
    entry = _unit(report, "t-retry")
    assert entry["verdict"] == R.V_NOT_APPLIED
    assert entry["decision"] == CR.A_RETRY
    assert report["retried"] == 1
    assert _ledger("exec_tool_calls", "t-retry")["status"] == T.STEP_FAILED


def test_a_never_repeatable_operation_reaches_a_human(proj):
    """§26.5 — no automatic force push, no unattended deployment."""
    _command("c-push", command="git push --force origin main", pid=None,
             status=C.CommandStatus.RUNNING, project=proj)
    report = CR.scan(project=proj, workers=False)
    entry = _unit(report, "c-push")
    assert entry["classification"] == CL.R_REQUIRES_VERIFICATION
    assert entry["verdict"] == R.V_UNVERIFIABLE       # verified, and still unknown
    assert entry["decision"] == CR.A_REVIEW
    assert report["review"] == 1 and report["retried"] == 0
    assert [r["ref_id"] for r in CR.review_queue(project=proj)] == ["c-push"]


def test_a_command_whose_process_is_still_alive_goes_to_review(proj):
    """§25.5 — pids are recycled, so an automatic scan never kills one."""
    _command("c-pid", command="rm -rf build", pid=os.getpid(),
             status=C.CommandStatus.RUNNING, project=proj)
    report = CR.scan(project=proj, workers=False)
    entry = _unit(report, "c-pid")
    assert entry["decision"] == CR.A_REVIEW
    assert entry["reason"] == "the command's process is still alive"
    assert entry["state"] == CR.S_REVIEW


def test_a_repeat_is_refused_when_the_capability_is_gone(proj, tmp_path, monkeypatch):
    """Task 26 §9 through the whole machine: the reason names the capability."""
    path = _file(tmp_path, "denied.txt", "before")
    _tool_call("t-denied", tool="delete_file", paths=(path,), project=proj,
               pre={path: X.digests([path])[path]})
    monkeypatch.setenv("AGENT2_DENY_CAPS", "fs.delete")
    report = CR.scan(project=proj, workers=False)
    entry = _unit(report, "t-denied")
    assert entry["verdict"] == R.V_NOT_APPLIED       # verified as NOT applied …
    assert entry["decision"] == CR.A_REVIEW          # … and still not repeated
    assert _ledger("exec_tool_calls", "t-denied")["status"] == X.INTERRUPTED


def test_no_command_line_reaches_the_recovery_surface(proj):
    """⚠️ `run_command` argv routinely carries a bearer token.

    The payload is written on a path that has just proven the machine's state is
    unknown, and every surface reads it — so the argv, the tool arguments and the
    file contents stay in the ledger and out of the report. A target *path* is the
    one thing that does travel, because "which file was half-written" is the whole
    of what a human reviewing the queue has to go on.
    """
    _command("c-secret", command=f"curl -H 'Authorization: {SECRET}' https://x/y",
             pid=None, status=C.CommandStatus.RUNNING, project=proj)
    _tool_call("t-secret", tool="write_file", project=proj,
               paths=(f"{proj}/report.txt",))
    report = CR.scan(project=proj, workers=False)
    assert report["processed"] == 2
    for blob in (json.dumps(report), json.dumps(CR.report(project=proj)),
                 json.dumps(CR.rows(project=proj)), json.dumps(CR.stats())):
        assert SECRET not in blob
        assert "Authorization" not in blob
        assert "curl" not in blob
    # The payload is deliberately thin — no `command` column joined in.
    rows = {r["ref_id"]: r for r in CR.rows(project=proj)}
    assert "command" not in set(rows["c-secret"])
    # …and the evidence a reviewer needs IS there: which file, and how many.
    evidence = rows["t-secret"]["evidence"]
    assert list(evidence["pre"]) == [f"{proj}/report.txt"]
    assert (evidence["paths"], evidence["paths_total"]) == (1, 1)


def test_recovery_disabled_writes_nothing(proj, monkeypatch):
    _command("c-off", project=proj)
    monkeypatch.setattr(cfg, "RECOVERY_ENABLED", False)
    report = CR.scan(project=proj)
    assert report["enabled"] is False
    assert "AGENT2_RECOVERY" in report["reason"]
    assert report["units"] == [] and report["found"] == 0
    assert db.qall("SELECT id FROM exec_recovery") == []
    assert CR.recover_workers() == []
    assert _ledger("exec_commands", "c-off")["status"] == C.CommandStatus.RUNNING


def test_the_scan_never_raises_when_the_ledger_is_broken(proj, monkeypatch):
    """A crashed PREVIOUS run must not become a launch failure for this one."""
    def boom(*a, **k):
        raise RuntimeError("ledger down")

    monkeypatch.setattr(X, "interrupted", boom)
    report = CR.scan(project=proj, workers=False)
    assert "ledger down" in report["error"]
    assert report["units"] == []
    assert CR.counters()["errors"] >= 1
    assert CR.stats()["problems"]           # THIS is a health problem


def test_a_unit_that_cannot_be_classified_does_not_end_the_scan(proj, monkeypatch):
    _command("c-a", command="ls", pid=None, status=C.CommandStatus.RUNNING,
             project=proj)
    _command("c-b", command="ls", pid=None, status=C.CommandStatus.RUNNING,
             project=proj)
    calls = {"n": 0}
    real = CL.assess

    def flaky(kind, row):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("classifier exploded")
        return real(kind, row)

    monkeypatch.setattr(CL, "assess", flaky)
    report = CR.scan(project=proj, workers=False)
    states = {e["ref_id"]: e["state"] for e in report["units"]}
    assert set(states) == {"c-a", "c-b"}
    assert CR.S_FAILED in states.values()
    assert CR.S_RECOVERED in states.values()    # the second unit still ran


def test_a_stale_running_task_is_interrupted_once_per_session(proj, monkeypatch):
    """⚠️ `interrupt()` operates on a SESSION. Once per task would write the same
    checkpoint N times and log N identical events for one crash."""
    sid = T.open_session(chat_id="c-w", cwd=proj, goal="two things")
    T.sync_list(sid, [{"task": "one", "status": "in_progress"},
                      {"task": "two", "status": "in_progress"}])
    seen: list = []
    real = T.interrupt
    monkeypatch.setattr(T, "interrupt",
                        lambda s, *a, **k: (seen.append(s), real(s, *a, **k))[1])

    entries = CR.recover_workers(cwd=proj, older_than=0)
    assert len(entries) == 2
    assert seen == [sid]
    assert {e["decision"] for e in entries} == {CR.A_RESUME}
    assert {t.status for t in T.list_tasks(sid)} == {T.TaskStatus.PAUSED}
    for task in T.list_tasks(sid):
        assert _rec(CL.K_TASK, task.id)["status"] == CR.S_RECOVERED


def test_paused_is_never_swept(proj):
    """⚠️ Sweeping PAUSED would hand recovery every chat the user paused on purpose
    — precisely the repurposing of `/pause` this phase is forbidden to do."""
    sid = T.open_session(chat_id="c-p", cwd=proj, goal="parked")
    T.sync_list(sid, [{"task": "parked", "status": "in_progress"}])
    task = T.list_tasks(sid)[0]
    T.set_status(task.id, T.TaskStatus.PAUSED)

    assert CR.recover_workers(cwd=proj, older_than=0) == []
    assert _rec(CL.K_TASK, task.id) == {}
    assert T.get(task.id).status == T.TaskStatus.PAUSED


def test_no_recovery_state_is_a_health_problem(proj):
    """⚠️ A row in NEEDS_REVIEW is recovery working as designed; it can sit there for
    weeks. Reporting it would pin `/api/health` at 503 until someone tidied a queue."""
    _command("c-review", command="git push origin main", pid=None,
             status=C.CommandStatus.RUNNING, project=proj)
    CR.scan(project=proj, workers=False)
    stats = CR.stats()
    assert stats["needs_review"] == 1
    assert stats["by_state"][CR.S_REVIEW] == 1
    assert stats["problems"] == []
    assert stats["enabled"] is True
    assert stats["stale_executions"] >= 1


def test_terminate_needs_the_exec_capability(proj, monkeypatch):
    """Ending a process tree is privileged whether or not the starter was allowed."""
    _command("c-kill", command="sleep 300", pid=os.getpid(),
             status=C.CommandStatus.RUNNING, project=proj)
    monkeypatch.setenv("AGENT2_DENY_CAPS", "exec")
    assert CR.terminate(CL.K_COMMAND, "c-kill") == {
        "ok": False, "error": "the 'exec' capability is not held"}
    # Only a command has a process tree at all.
    monkeypatch.delenv("AGENT2_DENY_CAPS", raising=False)
    assert CR.terminate(CL.K_TOOL_CALL, "c-kill")["error"] \
        == "only a command has a process tree"
    # With the capability held it gets as far as the pid — and this test never kills
    # its own interpreter, so the row it is pointed at has no pid recorded.
    _command("c-nopid", command="sleep 300", pid=None, project=proj)
    assert CR.terminate(CL.K_COMMAND, "c-nopid")["error"] == "no process was recorded"


def test_an_operator_retry_still_checks_the_capability(proj, tmp_path, monkeypatch):
    """⚠️ A human overrules the VERDICT, never the permission gate. Otherwise
    "retry" is a button that launders `AGENT2_DENY_CAPS` away."""
    path = _file(tmp_path, "op.txt", "before")
    _tool_call("t-op", tool="delete_file", paths=(path,), project=proj,
               pre={path: "sha256:" + "2" * 64})
    CR.scan(project=proj, workers=False)
    assert CR.state_of(CL.K_TOOL_CALL, "t-op") == CR.S_REVIEW

    monkeypatch.setenv("AGENT2_DENY_CAPS", "fs.delete")
    refused = CR.retry(CL.K_TOOL_CALL, "t-op")
    assert refused["ok"] is False
    assert "'fs.delete' capability is not held" in refused["error"]
    assert _ledger("exec_tool_calls", "t-op")["status"] == X.INTERRUPTED

    monkeypatch.delenv("AGENT2_DENY_CAPS", raising=False)
    allowed = CR.retry(CL.K_TOOL_CALL, "t-op")
    assert allowed == {"ok": True, "state": CR.S_RECOVERED, "decision": CR.A_RETRY}
    assert _ledger("exec_tool_calls", "t-op")["status"] == T.STEP_FAILED
    assert CR.retry(CL.K_COMMAND, "nope")["error"] == "no recovery record"


def test_acknowledge_does_not_settle_the_ledger_row(proj):
    """A human deciding not to act does not change what the crashed process did."""
    _command("c-ack", command="./deploy.sh", pid=None,
             status=C.CommandStatus.RUNNING, project=proj)
    CR.scan(project=proj, workers=False)
    assert CR.state_of(CL.K_COMMAND, "c-ack") == CR.S_REVIEW

    out = CR.acknowledge(CL.K_COMMAND, "c-ack", "looked at it")
    assert out == {"ok": True, "state": CR.S_RECOVERED}
    assert CR.state_of(CL.K_COMMAND, "c-ack") == CR.S_RECOVERED
    assert _rec(CL.K_COMMAND, "c-ack")["decision"] == CR.A_NONE
    # ⚠️ The ledger row keeps its honest "we do not know" status.
    assert _ledger("exec_commands", "c-ack")["status"] == X.INTERRUPTED
    assert CR.acknowledge(CL.K_COMMAND, "nope")["error"] == "no recovery record"


def test_owner_alive_has_three_answers():
    """`None` means "no information" and may never be read as "dead"."""
    assert CR.owner_alive({"instance": X.INSTANCE}) is True
    assert CR.owner_alive({"instance": DEAD}) is False
    assert CR.owner_alive({"instance": LEGACY}) is None
    assert CR.owner_alive({"instance": ""}) is None
    assert CR.owner_alive({}) is None
    assert CR.owner_alive(None) is None
    assert CR.owner_alive({"instance": f"{os.getpid()}-beefcafe"}) is True
    # ⚠️ AN ALIAS, NOT A COPY — one answer to "does this pid exist" on Windows.
    assert CR.pid_alive is procio.pid_alive


def test_the_scan_is_bounded(proj):
    """⚠️ AND IT SAYS SO. Sabotage-verified against the shipped code, which asked
    the reader for exactly `limit` rows: the loop reports `truncated` by reaching the
    cap with a row still in hand, so that made the flag unreachable and a scan that
    examined 1 of 3 units reported `truncated: False`. An operator reads that as
    "recovery looked at everything" — a silent cap on the one path that decides
    whether interrupted work is examined at all.
    """
    for n in range(3):
        _command(f"c-many-{n}", command="ls", pid=None,
                 status=C.CommandStatus.RUNNING, project=proj)
    report = CR.scan(project=proj, limit=1, workers=False)
    assert report["truncated"] is True
    assert len(report["units"]) == 1
    assert report["found"] >= 2          # with `truncated`, a floor — not a total

    # The PAIR: a ceiling nobody hit must not cry wolf, or the flag means nothing.
    # (The capped scan settled the one unit it retried, so what is left is what the
    # roomy scan sees — and "not truncated" must mean every found row got an entry.)
    roomy = CR.scan(project=proj, limit=50, workers=False)
    assert roomy["truncated"] is False
    assert len(roomy["units"]) == roomy["found"] == 2


def test_an_attempt_ceiling_ends_in_review(proj, monkeypatch):
    """A unit that keeps failing recovery stops being retried and asks a human.

    ⚠️ The unit here ends in A_REVIEW deliberately: a decision that SETTLES the
    ledger row (retry ⇒ FAILED, complete ⇒ COMPLETED) makes the row terminal and the
    reader never offers it again, which is rule 20 working. Only a row still holding
    `interrupted` can reach a second attempt at all.
    """
    monkeypatch.setattr(cfg, "RECOVERY_MAX_ATTEMPTS", 1)
    _command("c-loop", command="./deploy.sh", pid=None,
             status=C.CommandStatus.RUNNING, project=proj)
    first = CR.scan(project=proj, workers=False)
    assert _unit(first, "c-loop")["attempt"] == 1
    assert _ledger("exec_commands", "c-loop")["status"] == X.INTERRUPTED
    # `force` re-decides an already-resolved unit — a manual re-run of the same
    # failing unit from `/api/recovery/scan`.
    second = CR.scan(project=proj, workers=False, force=True)
    entry = _unit(second, "c-loop")
    assert entry["attempt"] == 2
    assert entry["decision"] == CR.A_REVIEW
    assert "attempted 1 times already" in entry["reason"]


def test_the_task_scope_sentinel_is_translated():
    """⚠️ The two readers spell "every project" differently — `"*"` vs `""`.

    Passing `"*"` straight through narrows the task query to a project literally
    named `*`: worker recovery silently finds nothing and every stuck task stays
    RUNNING forever, with nothing raised.
    """
    assert CR._task_scope(X.ANY_PROJECT) == ""
    assert CR._task_scope("*") == ""
    assert CR._task_scope(None) == ""
    assert CR._task_scope("") == ""
    assert CR._task_scope("/some/project") == "/some/project"


def test_state_of_answers_for_healthy_work():
    """⚠️ "No row" IS the answer for work that never crashed."""
    assert CR.state_of(CL.K_COMMAND, "never-seen") == CR.S_NORMAL
    assert CR.S_NORMAL not in CR.RESOLVED


def test_the_report_is_readable_and_total(proj):
    _command("c-rep", command="git push origin main", pid=None,
             status=C.CommandStatus.RUNNING, project=proj)
    CR.scan(project=proj, workers=False)
    report = CR.report(project=proj)
    assert report["enabled"] is True
    assert report["instance"] == X.INSTANCE
    assert report["needs_review"] == 1
    assert report["last_scan"]["found"] == 1
    assert report["counters"]["scans"] == 1
    line = CR.describe(report["review"][0])
    assert "command c-rep" in line and "review" in line
    assert CR.describe({}).strip() == "?"
    assert CR.describe(None).strip() == "?"


# ═══════════════════════════════════════════════════════════════════════════════
# The web surface
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def client():
    db.init_db()
    app = Flask(__name__)
    register_routes(app)
    with app.test_client() as c:
        yield c


def test_the_scan_route_is_not_the_plan_route(client, proj):
    """⚠️ The pin `routes.py` asks for by name.

    `/api/recovery/scan` and `/api/recovery/units` sit in front of
    `/api/recovery/<sid>`; Werkzeug sorts static segments before converters, and this
    is the assertion that proves it rather than assuming it.
    """
    _command("c-http", command="ls", pid=None, status=C.CommandStatus.RUNNING,
             project=proj)
    resp = client.post("/api/recovery/scan",
                       json={"project": proj, "workers": False})
    assert resp.status_code == 200
    body = resp.get_json()
    # A scan report, NOT a recovery plan for a session called "scan".
    assert body["instance"] == X.INSTANCE
    assert {"found", "processed", "review", "units", "swept"} <= set(body)
    assert "resume" not in body and "waiting" not in body
    assert body["found"] == 1

    bad = client.post("/api/recovery/scan", json={"limit": "lots"})
    assert bad.status_code == 400
    assert bad.get_json()["error"] == "limit must be a number"


def test_the_units_route_reports_records_without_their_text(client, proj):
    _command("c-units", command=f"curl -H 'Authorization: {SECRET}' https://x",
             pid=None, status=C.CommandStatus.RUNNING, project=proj)
    client.post("/api/recovery/scan", json={"project": proj})

    resp = client.get(f"/api/recovery/units?project={proj}")
    assert resp.status_code == 200
    body = resp.get_json()
    assert [u["ref_id"] for u in body["units"]] == ["c-units"]
    assert body["counters"]["scans"] >= 1
    assert SECRET not in json.dumps(body)

    only = client.get(f"/api/recovery/units?project={proj}&state={CR.S_REVIEW}")
    assert [u["ref_id"] for u in only.get_json()["units"]] == ["c-units"]
    assert client.get("/api/recovery/units?limit=nope").status_code == 400


def test_the_unit_action_route_rejects_anything_else(client, proj):
    _command("c-act", command="./deploy.sh", pid=None,
             status=C.CommandStatus.RUNNING, project=proj)
    client.post("/api/recovery/scan", json={"project": proj})

    bad = client.post(f"/api/recovery/units/{CL.K_COMMAND}/c-act",
                      json={"action": "delete-everything"})
    assert bad.status_code == 400
    assert "acknowledge" in bad.get_json()["error"]

    ok = client.post(f"/api/recovery/units/{CL.K_COMMAND}/c-act",
                     json={"action": "acknowledge", "note": "seen"})
    assert ok.status_code == 200 and ok.get_json()["ok"] is True
    # A failed action is a 400 with a reason, never a silent success.
    missing = client.post(f"/api/recovery/units/{CL.K_COMMAND}/nope",
                          json={"action": "acknowledge"})
    assert missing.status_code == 400
    assert missing.get_json()["ok"] is False


def test_the_recovery_endpoint_still_carries_both_halves(client, proj):
    """⚠️ ONE endpoint, two halves — Task 3's `candidates` keeps its exact shape,
    because `public/script.js` reads it."""
    resp = client.get("/api/recovery")
    assert resp.status_code == 200
    body = resp.get_json()
    assert isinstance(body["candidates"], list)
    assert "error" not in body["crash"]
    assert body["crash"]["instance"] == X.INSTANCE
    assert {"counters", "needs_review", "review", "recent"} <= set(body["crash"])
