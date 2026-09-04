# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
Tests for automatic task recovery (``agent2/core/recovery.py``) and the panel that
draws it (``agent2/cli/taskview.render_recovery``).

Run from the repo root:  python -m pytest .github/tests/test_recovery.py -v

Coverage (Task 3)
  - detection: an interrupted session in THIS project is found; a finished one is
    not, and the live session excludes itself
  - the six interruption points the plan names: during the FIRST task, during a
    MIDDLE task, during a command, during a file modification, the recovery
    itself, and completed tasks never running again
  - verification: `verify_step()`'s four verdicts against the real filesystem
  - adoption: only the interrupted task moves, and it moves to PENDING
  - the model brief: durable, consumed exactly once, and explicit about what must
    not be repeated
  - `abandon()`: declining is a real answer with a real effect

⚠️ The load-bearing tests in this file are
``test_completed_tasks_are_never_handed_back_as_work`` (rule 20) and
``test_an_in_flight_destructive_step_is_verified_not_replayed`` (rule 21). Both
are sabotage-verified: making `plan()` treat a terminal task as resumable turns
the first red, and dropping the `destructive_pending` gate in `plan()` turns the
second red.

⚠️ ``test_recovery_does_not_touch_chat_pause_or_resume`` pins rules 17-18 — the
one requirement no amount of correct recovery behaviour would reveal on its own.

conftest.py redirects AGENT2_DB to a throwaway temp DB, so these tests never
touch the developer's real agent2.db.
"""

import os

import pytest

from agent2 import database as db
from agent2.core import recovery as R
from agent2.core import tasks as T


@pytest.fixture(autouse=True)
def _schema():
    db.init_db()
    yield


@pytest.fixture
def proj(tmp_path):
    """A project directory of our own, so detection cannot see other tests."""
    return str(tmp_path)


def _session(proj, goal="ship it", chat_id=""):
    return T.open_session(chat_id=chat_id or ("chat-" + T._uid()),
                          cwd=proj, goal=goal)


def _plan(proj, rows, goal="ship it", chat_id=""):
    """A session whose checklist is *rows* — (title, status) pairs."""
    sid = _session(proj, goal, chat_id)
    T.sync_list(sid, [{"task": t, "status": s} for t, s in rows])
    return sid


def statuses(sid):
    return [(t.title, t.status) for t in T.list_tasks(sid)]


# ── Detection ─────────────────────────────────────────────────────────────────

def test_an_interrupted_session_is_found(proj):
    sid = _plan(proj, [("one", "completed"), ("two", "in_progress")])
    found = R.candidates(proj)
    assert [c.session_id for c in found] == [sid]
    assert found[0].goal == "ship it"
    assert (found[0].total, found[0].completed, found[0].open) == (2, 1, 1)


def test_a_finished_session_is_not_offered(proj):
    _plan(proj, [("one", "completed"), ("two", "completed")])
    assert R.candidates(proj) == []
    assert R.latest(proj) is None


def test_the_live_session_excludes_itself(proj):
    """A running CLI must not offer to recover the plan it is executing."""
    sid = _plan(proj, [("one", "in_progress")])
    assert [c.session_id for c in R.candidates(proj)] == [sid]
    assert R.candidates(proj, exclude=sid) == []


def test_detection_is_scoped_to_the_project(proj, tmp_path):
    """Rule 30: another directory's interrupted work is not this project's."""
    other = str(tmp_path / "elsewhere")
    os.makedirs(other, exist_ok=True)
    _plan(proj, [("mine", "in_progress")])
    assert [c.cwd for c in R.candidates(other)] == []


def test_detection_never_raises_when_the_store_is_broken(monkeypatch):
    """FAILSAFE: a launcher that died reading a recovery hint is worse than one
    that shows nothing."""
    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(T, "unfinished_sessions", boom)
    assert R.candidates("/anywhere") == []
    assert R.latest("/anywhere") is None


# ── The six interruption points the plan names ────────────────────────────────

def test_interruption_during_the_first_task(proj):
    """Nothing is done yet, so there is nothing to protect — and the plan still
    resumes rather than starting the list over from a blank slate."""
    sid = _plan(proj, [("one", "in_progress"), ("two", "pending"),
                       ("three", "pending")])
    rp = R.plan(sid)
    assert rp.done == []
    assert rp.resume.title == "one"
    assert [t.title for t in rp.waiting] == ["two", "three"]


def test_interruption_during_a_middle_task(proj):
    """The plan's own example: tasks 1-3 done, 4 interrupted, 5 waiting."""
    sid = _plan(proj, [("t1", "completed"), ("t2", "completed"),
                       ("t3", "completed"), ("t4", "in_progress"),
                       ("t5", "pending")])
    rp = R.plan(sid)
    assert [t.title for t in rp.done] == ["t1", "t2", "t3"]
    assert rp.resume.title == "t4"
    assert [t.title for t in rp.waiting] == ["t5"]


def test_interruption_during_a_command(proj):
    """A shell command's effects cannot be read back, so it must surface as a
    question rather than a silent retry."""
    sid = _plan(proj, [("build", "in_progress")])
    T.note_tool(sid, "run_command", detail="git commit -m wip")
    rp = R.plan(sid)
    assert rp.view["destructive_pending"] is True
    assert [c["verdict"] for c in rp.checks] == [R.V_UNVERIFIABLE]
    assert rp.needs_verification is True


def test_interruption_during_a_file_modification(proj, tmp_path):
    """The write landed on disk before the process died: reporting it as
    `not_applied` would rewrite finished work (rule 21)."""
    target = tmp_path / "written.py"
    sid = _plan(proj, [("edit", "in_progress")])
    T.note_tool(sid, "write_file", detail=str(target))
    target.write_text("done", encoding="utf-8")          # it actually happened

    rp = R.plan(sid)
    assert [c["verdict"] for c in rp.checks] == [R.V_LIKELY_APPLIED]
    assert rp.needs_verification is True


def test_recovery_resumes_the_interrupted_task(proj):
    sid = _plan(proj, [("one", "completed"), ("two", "in_progress"),
                       ("three", "pending")])
    rp = R.recover(sid)
    assert rp.resume.title == "two"
    # PENDING, not RUNNING: nothing is executing at the moment of adoption, and
    # RUNNING would make the NEXT crash believe a step was in flight.
    assert rp.resume.status == T.TaskStatus.PENDING
    assert T.checkpoint_view(rp.resume)["recovered"]["attempt"] == 1


def test_completed_tasks_are_never_handed_back_as_work(proj):
    """⚠️ Rule 20, from three directions at once: the completed task is not in
    `resume`, not in `waiting`, and still COMPLETED after adoption."""
    sid = _plan(proj, [("one", "completed"), ("two", "completed"),
                       ("three", "in_progress")])
    rp = R.recover(sid)

    assert rp.resume.title == "three"
    assert [t.title for t in rp.waiting] == []
    assert {t.title for t in rp.done} == {"one", "two"}
    assert statuses(sid) == [("one", T.TaskStatus.COMPLETED),
                             ("two", T.TaskStatus.COMPLETED),
                             ("three", T.TaskStatus.PENDING)]
    # And the words the model reads first say so explicitly.
    assert "do NOT run these again" in R.describe(rp)


# ── Verification (rule 21) ────────────────────────────────────────────────────

def test_a_write_whose_target_is_absent_is_safe_to_repeat(tmp_path):
    step = {"name": "write_file", "detail": str(tmp_path / "nope.py"),
            "at": T._now()}
    v = R.verify_step(step)
    assert v["verdict"] == R.V_NOT_APPLIED
    assert v["verdict"] in R.SAFE_TO_REPEAT


def test_a_delete_whose_target_survived_is_safe_to_repeat(tmp_path):
    f = tmp_path / "still-here.py"
    f.write_text("x", encoding="utf-8")
    v = R.verify_step({"name": "delete_file", "detail": str(f), "at": T._now()})
    assert v["verdict"] == R.V_NOT_APPLIED


def test_a_delete_whose_target_is_gone_is_not_repeated(tmp_path):
    v = R.verify_step({"name": "delete_file", "detail": str(tmp_path / "gone.py"),
                       "at": T._now()})
    assert v["verdict"] == R.V_LIKELY_APPLIED
    assert v["verdict"] not in R.SAFE_TO_REPEAT


def test_a_shell_command_is_never_called_safe(tmp_path):
    v = R.verify_step({"name": "run_command", "detail": "rm -rf build",
                       "at": T._now()})
    assert v["verdict"] == R.V_UNVERIFIABLE
    assert v["verdict"] not in R.SAFE_TO_REPEAT


def test_a_file_that_predates_the_step_is_uncertain_not_safe(tmp_path):
    """It exists, but it may be the OLD content — which is exactly the state that
    must not be resolved by guessing."""
    f = tmp_path / "old.py"
    f.write_text("old", encoding="utf-8")
    os.utime(f, (1_600_000_000, 1_600_000_000))          # long before the step
    v = R.verify_step({"name": "write_file", "detail": str(f), "at": T._now()})
    assert v["verdict"] == R.V_UNCERTAIN
    assert v["verdict"] not in R.SAFE_TO_REPEAT


def test_only_not_applied_is_safe_to_repeat():
    """The whole gate, in one assertion: three of the four verdicts are questions."""
    assert R.SAFE_TO_REPEAT == frozenset((R.V_NOT_APPLIED,))
    for v in (R.V_LIKELY_APPLIED, R.V_UNCERTAIN, R.V_UNVERIFIABLE):
        assert v not in R.SAFE_TO_REPEAT


def test_an_in_flight_destructive_step_is_verified_not_replayed(proj, tmp_path):
    """⚠️ Rule 21 end to end: the checks reach the plan, the plan reports that it
    needs verification, and the brief tells the model to look before repeating."""
    f = tmp_path / "half.py"
    f.write_text("partial", encoding="utf-8")
    sid = _plan(proj, [("write it", "in_progress")])
    T.note_tool(sid, "write_file", detail=str(f))

    rp = R.plan(sid)
    assert rp.checks and rp.needs_verification is True
    brief = R.describe(rp)
    assert "MAY ALREADY HAVE HAPPENED" in brief
    assert "never blindly retry" in brief


def test_a_completed_step_is_not_verified_again(proj, tmp_path):
    """Only a step caught RUNNING is uncertain. One that finished is history.

    The two `note_tool` calls are exactly what `dispatch_tool` does around a
    destructive tool: one before, one after.
    """
    sid = _plan(proj, [("write it", "in_progress")])
    T.note_tool(sid, "write_file", detail=str(tmp_path / "a.py"))
    T.note_tool(sid, "write_file", ok=True, detail=str(tmp_path / "a.py"))
    rp = R.plan(sid)
    assert rp.view["destructive_pending"] is False
    assert rp.checks == []
    assert rp.needs_verification is False


def test_a_non_destructive_step_raises_no_question(proj):
    sid = _plan(proj, [("look around", "in_progress")])
    T.note_tool(sid, "read_file", detail="README.md")
    rp = R.plan(sid)
    assert rp.checks == [] and rp.needs_verification is False


# ── Adoption ──────────────────────────────────────────────────────────────────

def test_adopt_touches_only_the_interrupted_task(proj):
    """⚠️ Rule 20's other half. The witness is the `recovered` stamp, not a
    timestamp: `adopt()` writes that stamp on exactly the task it adopted, so a
    stamp on any other row means recovery reached somewhere it must not."""
    sid = _plan(proj, [("one", "completed"), ("two", "in_progress"),
                       ("three", "pending")])
    rp = R.plan(sid)
    assert R.adopt(rp) is True

    after = {t.title: t for t in T.list_tasks(sid)}
    assert after["one"].status == T.TaskStatus.COMPLETED     # untouched
    assert after["two"].status == T.TaskStatus.PENDING       # the one adopted
    assert after["three"].status == T.TaskStatus.PENDING
    assert T.checkpoint_view(after["one"])["recovered"] == {}
    assert T.checkpoint_view(after["three"])["recovered"] == {}
    assert T.checkpoint_view(after["two"])["recovered"]["attempt"] == 1


def test_adopting_twice_counts_the_attempts(proj):
    """A second crash during a recovery is still a second attempt.

    `attempt` counts ADOPTIONS, and `rp.attempt` reads the task's own
    `attempt_count`, which the status writer bumps when a task actually starts
    running. So the honest second-crash sequence is adopt → run → crash → adopt.
    """
    sid = _plan(proj, [("flaky", "in_progress")])
    first = R.recover(sid)
    assert T.checkpoint_view(first.resume)["recovered"]["attempt"] == 1

    T.start(first.resume.id)                       # picked back up, then dies
    T.interrupt(sid, "killed again")
    second = R.recover(sid)
    assert T.checkpoint_view(second.resume)["recovered"]["attempt"] == 2


def test_a_plan_that_never_started_still_recovers(proj):
    """A process killed before the first tool ran left everything PENDING. That
    is the case `/resume` structurally cannot handle."""
    sid = _plan(proj, [("one", "pending"), ("two", "pending")])
    rp = R.plan(sid)
    assert rp.resume.title == "one"
    assert [t.title for t in rp.waiting] == ["two"]
    assert rp.checks == []


def test_an_empty_plan_adopts_nothing(proj):
    sid = _plan(proj, [("done", "completed")])
    rp = R.plan(sid)
    assert rp.is_empty()
    assert R.adopt(rp) is False


def test_recovery_records_its_verdicts_in_the_checkpoint(proj, tmp_path):
    """The trail must outlive the panel: a user who dismissed the terminal output
    can still see what was uncertain."""
    sid = _plan(proj, [("risky", "in_progress")])
    T.note_tool(sid, "delete_file", detail=str(tmp_path / "gone.py"))
    rp = R.recover(sid)
    stamp = T.checkpoint_view(rp.resume)["recovered"]
    assert [c["verdict"] for c in stamp["checks"]] == [R.V_LIKELY_APPLIED]
    assert stamp["reason"] and stamp["at"]


# ── The model brief ───────────────────────────────────────────────────────────

def test_the_brief_names_done_resume_and_waiting(proj):
    sid = _plan(proj, [("one", "completed"), ("two", "in_progress"),
                       ("three", "pending")])
    text = R.describe(R.plan(sid))
    assert "ALREADY COMPLETED" in text and "✓ one" in text
    assert "INTERRUPTED — resume here: two" in text
    assert "NOT STARTED" in text and "○ three" in text


def test_the_brief_is_delivered_exactly_once(proj):
    """⚠️ Durable and consumed once. An in-process flag would lose it in the one
    case recovery exists for — a process that closes again before the user types."""
    sid = _plan(proj, [("one", "in_progress")])
    R.recover(sid)
    assert R.pending_brief(sid)
    first = R.consume_brief(sid)
    assert "RECOVERED SESSION" in first
    db.close_all()                                   # stand-in for a restart
    assert R.consume_brief(sid) == ""
    assert R.pending_brief(sid) == ""


def test_no_brief_is_owed_without_a_recovery(proj):
    """A session nobody adopted must not brief the model: the user never agreed
    to resume it."""
    sid = _plan(proj, [("one", "in_progress")])
    assert R.pending_brief(sid) == ""
    assert R.consume_brief(sid) == ""


def test_brief_for_chat_finds_the_session_and_never_raises(proj):
    sid = _plan(proj, [("one", "in_progress")], chat_id="chat-brief")
    R.recover(sid)
    assert "RECOVERED SESSION" in R.brief_for_chat("chat-brief")
    assert R.brief_for_chat("chat-brief") == ""       # consumed
    assert R.brief_for_chat("") == ""
    assert R.brief_for_chat("no-such-chat") == ""


def test_brief_for_chat_survives_a_broken_store(monkeypatch):
    """FAILSAFE: the agent loops call this on the way INTO a turn, so a fault
    here would be a recovery system causing the outage."""
    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(T, "active_session_for_chat", boom)
    assert R.brief_for_chat("anything") == ""


def test_an_empty_plan_briefs_nothing(proj):
    sid = _plan(proj, [("one", "completed")])
    assert R.describe(R.plan(sid)) == ""


# ── Declining ─────────────────────────────────────────────────────────────────

def test_abandon_cancels_the_open_work_and_closes_the_session(proj):
    sid = _plan(proj, [("one", "completed"), ("two", "in_progress"),
                       ("three", "pending")])
    assert R.abandon(sid) == 2
    assert statuses(sid) == [("one", T.TaskStatus.COMPLETED),
                             ("two", T.TaskStatus.CANCELLED),
                             ("three", T.TaskStatus.CANCELLED)]
    assert T.get_session(sid)["status"] == T.SESSION_ABANDONED


def test_a_declined_session_is_not_offered_again(proj):
    """Without this the same prompt returns at every launch, which trains the
    user to dismiss it unread."""
    sid = _plan(proj, [("one", "in_progress")])
    R.abandon(sid)
    assert R.candidates(proj) == []


def test_abandon_leaves_completed_work_completed(proj):
    sid = _plan(proj, [("one", "completed"), ("two", "in_progress")])
    R.abandon(sid)
    assert T.get(T.list_tasks(sid)[0].id).status == T.TaskStatus.COMPLETED


# ── Rules 17-18: chat pause/resume are NOT task recovery ──────────────────────

def test_recovery_does_not_touch_chat_pause_or_resume():
    """⚠️ Rules 17-18. `/pause` and `/resume` are CHAT controls; recovery reads
    task state only. Asserted on the module surface because no behavioural test
    would notice the day somebody wired the two together.

    Docstrings and comments are stripped before the scan: recovery's own
    docstring names `core/context.pause_chat` in order to say it stays away from
    it, and a check that a module may not *mention* the thing it promises not to
    call would punish the documentation instead of the wiring.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(R))
    called = {
        (n.func.attr if isinstance(n.func, ast.Attribute) else
         getattr(n.func, "id", ""))
        for n in ast.walk(tree) if isinstance(n, ast.Call)
    }
    for forbidden in ("pause_chat", "resume_chat"):
        assert forbidden not in called, \
            f"recovery reached into chat control: {forbidden}()"

    imported = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            # BOTH halves: the module it came from AND the names taken out of it.
            # Checking only the aliases misses `from agent2.core.context import
            # pause_chat`, which is exactly the wiring this test exists to catch.
            imported.add(n.module or "")
            imported.update(a.name for a in n.names)
        elif isinstance(n, ast.Import):
            imported.update(a.name for a in n.names)
    assert not any("context" in name for name in imported), \
        "recovery imported the chat-context module"
    for forbidden in ("pause_chat", "resume_chat"):
        assert forbidden not in imported, \
            f"recovery imported chat control: {forbidden}"


def test_chat_pause_still_works_and_is_unrelated_to_tasks(proj):
    """The other direction, and the subtler half of rules 17-18.

    ⚠️ `pause_chat` DOES park the running task — that is Task 2's `interrupt()`
    hook, and it is the point: pausing a chat records where the work stopped.
    What rules 17-18 forbid is the reverse wiring, i.e. `/pause` becoming a task
    control that ends work, or `/resume` becoming the way recovery happens. So
    the assertions here are that the task stays OPEN (hence still recoverable,
    never cancelled) and that `resume_chat` is a chat-row flip that decides
    nothing about tasks — recovery, not `/resume`, is what picks the work up.
    """
    from agent2.core import context as core_context

    chat = core_context.new_chat("2.5-flash", "pro")
    cid = str(chat["id"])
    sid = _plan(proj, [("one", "in_progress")], chat_id=cid)

    core_context.pause_chat(cid)
    assert statuses(sid) == [("one", T.TaskStatus.PAUSED)]
    assert T.TaskStatus.PAUSED in T.OPEN            # parked, not finished
    assert R.plan(sid).resume.title == "one"        # still recoverable

    core_context.resume_chat(cid)
    assert statuses(sid) == [("one", T.TaskStatus.PAUSED)]   # /resume runs nothing
    assert R.plan(sid).resume.title == "one"


# ── Presentation ──────────────────────────────────────────────────────────────
#
# ⚠️ BOTH renderers, always. A test that only forces `_RICH = False` proves the
# fallback works and says nothing about what a user actually sees — a rich style
# string containing a raw ANSI escape prints NOTHING and raises nowhere visible.

def _rows_for(proj, tmp_path):
    sid = _plan(proj, [("one", "completed"), ("two", "in_progress"),
                       ("three", "pending")])
    T.note_tool(sid, "delete_file", detail=str(tmp_path / "gone.py"))
    return R.plan(sid)


def test_recovery_rows_cover_done_resume_waiting_and_the_warning(proj, tmp_path):
    from agent2.cli import taskview

    rows = taskview._recovery_rows(_rows_for(proj, tmp_path))
    text = "\n".join(t for _, t in rows)
    assert "✓ one" in text and "already completed" in text
    assert "↻ two" in text and "resuming here" in text
    assert "○ three" in text and "waiting" in text
    assert "⚠ delete_file" in text
    assert "do not repeat blindly" in text


def test_the_plain_recovery_panel_prints(proj, tmp_path, capsys, monkeypatch):
    from agent2.cli import taskview

    monkeypatch.setattr(taskview, "_RICH", False)
    assert taskview.render_recovery(_rows_for(proj, tmp_path)) is True
    out = capsys.readouterr().out
    assert "Recovered session" in out and "✓ one" in out and "↻ two" in out
    assert "1 already done" in out


def test_the_rich_recovery_panel_prints(proj, tmp_path, monkeypatch):
    """⚠️ The path a real terminal takes. `P.PU` in a rich style string makes this
    print nothing at all, and `render_recovery` swallows the MarkupError."""
    from agent2.cli import taskview

    if not taskview._RICH:
        pytest.skip("rich is not installed")

    printed = []
    monkeypatch.setattr(taskview._con, "print", lambda *a, **k: printed.append(a))
    assert taskview.render_recovery(_rows_for(proj, tmp_path)) is True
    assert printed, "the rich panel printed nothing"


def test_an_empty_plan_draws_nothing(proj, capsys):
    from agent2.cli import taskview

    sid = _plan(proj, [("one", "completed")])
    assert taskview.render_recovery(R.plan(sid)) is False
    assert taskview.render_recovery(None) is False
    assert capsys.readouterr().out == ""


def test_the_recovery_panel_never_raises(proj, tmp_path, monkeypatch):
    """It runs at launch: a display bug must not stop the CLI from starting."""
    from agent2.cli import taskview

    def boom(*a, **k):
        raise RuntimeError("render fault")

    monkeypatch.setattr(taskview, "_recovery_rows", boom)
    assert taskview.render_recovery(_rows_for(proj, tmp_path)) is False


def test_the_recovery_panel_ignores_the_redraw_fingerprint(proj, tmp_path,
                                                           capsys, monkeypatch):
    """⚠️ The opposite of `render()`: a briefing the user has not read yet must
    print every time it is asked for, and it must not suppress the first live
    task panel afterwards either."""
    from agent2.cli import taskview

    monkeypatch.setattr(taskview, "_RICH", False)
    rp = _rows_for(proj, tmp_path)
    taskview.forget()
    assert taskview.render_recovery(rp) is True
    assert taskview.render_recovery(rp) is True          # again, unsuppressed
    assert capsys.readouterr().out.count("Recovered session") == 2
    # The live panel still owes the user its first draw.
    assert taskview.render(rp.session_id) is True


# ── The durable trail reaches the browser ─────────────────────────────────────

def test_payload_exposes_the_recovery_stamp(proj):
    """The Web half reads `checkpoints[…].recovered`, so it has to be in the
    payload — otherwise the browser silently shows a resumed task as a fresh one."""
    sid = _plan(proj, [("one", "in_progress")])
    rp = R.recover(sid)
    views = T.payload(sid)["checkpoints"]
    assert views[rp.resume.id]["recovered"]["attempt"] == 1


def test_to_dict_is_json_safe(proj, tmp_path):
    import json

    rp = _rows_for(proj, tmp_path)
    d = json.loads(json.dumps(rp.to_dict()))
    assert d["done"] == ["one"] and d["resume"] == "two" and d["waiting"] == ["three"]
    assert d["needs_verification"] is True
    assert json.loads(json.dumps(R.candidates(proj)[0].to_dict()))["open"] == 2
