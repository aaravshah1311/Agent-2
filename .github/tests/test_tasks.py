# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
Tests for the persistent task engine (``agent2/core/tasks.py``) and the CLI
panel that renders it (``agent2/cli/taskview.py``).

Run from the repo root:  python -m pytest .github/tests/test_tasks.py -v

Coverage (Task 1)
  - creation, the full field set, and the eight states
  - persistence: a task survives a fresh read AND a simulated process restart
  - status transitions, including the timestamp/attempt bookkeeping
  - ordering: display order is stable and survives a re-merge
  - dependencies: resolution by index/title/id, and `ready()` gating
  - completion / failure / cancellation / skip
  - rendering: glyphs, the "n/m completed" line, and the no-duplicate-panel rule
  - the `update_todo` tool now writing THROUGH to the durable store

⚠️ The load-bearing test in this file is
``test_sync_list_never_regresses_a_completed_task``: the model re-sends the whole
checklist with stale statuses constantly, and honouring them is the exact bug
that made progress run backwards. Sabotage-verified — deleting the
`task.is_terminal` guard in `sync_list` turns it red.

conftest.py redirects AGENT2_DB to a throwaway temp DB, so these tests never
touch the developer's real agent2.db.
"""

import pytest

from agent2 import database as db
from agent2.core import sync
from agent2.core import tasks as T


@pytest.fixture(autouse=True)
def _schema():
    """Ensure the task tables exist (migrations 8/9) for every test."""
    db.init_db()
    yield


@pytest.fixture
def sess():
    """A fresh, empty task session."""
    return T.open_session(chat_id="chat-" + T._uid(), cwd="/proj/a", goal="demo")


def titles(session_id):
    return [t.title for t in T.list_tasks(session_id)]


def statuses(session_id):
    return [t.status for t in T.list_tasks(session_id)]


# ── Creation ──────────────────────────────────────────────────────────────────

def test_create_populates_every_required_field(sess):
    t = T.create(sess, "Analyze project", description="look around",
                 priority=2, dependencies=["x"])
    assert t.id and t.session_id == sess
    assert t.title == "Analyze project"
    assert t.description == "look around"
    assert t.status == T.TaskStatus.PENDING
    assert t.priority == 2
    assert t.dependencies == ["x"]
    assert t.created_at            # stamped
    assert t.started_at == "" and t.completed_at == ""
    assert t.attempt_count == 0 and t.progress == 0.0
    assert t.result == "" and t.error == "" and t.checkpoint == {}
    assert t.parent_task_id == ""


def test_create_requires_a_session_and_a_title(sess):
    with pytest.raises(ValueError):
        T.create("", "orphan")
    with pytest.raises(ValueError):
        T.create(sess, "   ")


def test_create_appends_to_the_end_of_the_list(sess):
    a = T.create(sess, "first")
    b = T.create(sess, "second")
    c = T.create(sess, "third")
    assert (a.seq, b.seq, c.seq) == (0, 1, 2)
    assert titles(sess) == ["first", "second", "third"]


def test_create_running_stamps_started_at(sess):
    t = T.create(sess, "go", status=T.TaskStatus.RUNNING)
    assert t.started_at and not t.completed_at


def test_parent_task_id_is_stored(sess):
    parent = T.create(sess, "parent")
    child = T.create(sess, "child", parent_task_id=parent.id)
    assert child.parent_task_id == parent.id


# ── Persistence ───────────────────────────────────────────────────────────────

def test_task_survives_a_fresh_read(sess):
    t = T.create(sess, "durable", description="body")
    again = T.get(t.id)
    assert again is not None
    assert (again.title, again.description) == ("durable", "body")


def test_task_survives_a_simulated_process_restart(sess):
    """The point of the whole module: a new process must see the same list.

    `db.close_all()` drops every pooled connection, so the reads below go
    through freshly opened ones — the closest in-process stand-in for a restart.
    """
    T.sync_list(sess, [{"task": "one", "status": "completed"},
                       {"task": "two", "status": "in_progress"}])
    db.close_all()
    assert titles(sess) == ["one", "two"]
    assert statuses(sess) == [T.TaskStatus.COMPLETED, T.TaskStatus.RUNNING]


def test_session_row_is_persisted_and_readable(sess):
    from agent2.core import context as core_context

    row = T.get_session(sess)
    # `cwd` is stored canonicalised — see the project-key tests at the bottom of
    # this file for why that matters and what it cost when it was not.
    assert row and row["cwd"] == core_context.project_key("/proj/a")
    assert row["goal"] == "demo"
    assert row["status"] == T.SESSION_ACTIVE


def test_session_for_chat_reuses_the_active_session():
    cid = "chat-reuse"
    a = T.session_for_chat(cid, cwd="/x")
    b = T.session_for_chat(cid, cwd="/x")
    assert a == b


def test_session_for_chat_opens_a_new_one_after_close():
    cid = "chat-closed"
    a = T.session_for_chat(cid, cwd="/x")
    T.close_session(a)
    b = T.session_for_chat(cid, cwd="/x")
    assert b != a


def test_empty_chat_id_does_not_share_a_session():
    """The CLI has no chat row on its first turn (CLAUDE.md). Two such sessions
    must not collide into one shared checklist."""
    a = T.session_for_chat("", cwd="/x")
    b = T.session_for_chat("", cwd="/x")
    assert a != b


# ── Status transitions ────────────────────────────────────────────────────────

def test_every_declared_status_is_reachable(sess):
    t = T.create(sess, "cycle")
    for status in (T.TaskStatus.QUEUED, T.TaskStatus.RUNNING, T.TaskStatus.PAUSED):
        assert T.set_status(t.id, status).status == status
    assert T.complete(t.id, "ok").status == T.TaskStatus.COMPLETED
    for status in (T.TaskStatus.FAILED, T.TaskStatus.CANCELLED, T.TaskStatus.SKIPPED):
        assert T.reopen(t.id).status == T.TaskStatus.PENDING
        assert T.set_status(t.id, status).status == status


def test_start_stamps_started_at_and_bumps_attempt(sess):
    t = T.create(sess, "work")
    started = T.start(t.id)
    assert started.status == T.TaskStatus.RUNNING
    assert started.started_at
    assert started.attempt_count == 1
    # A second start (after a pause) counts as another attempt.
    T.pause(t.id)
    assert T.start(t.id).attempt_count == 2


def test_repeated_start_does_not_inflate_attempts(sess):
    t = T.start(T.create(sess, "work").id)
    assert T.start(t.id).attempt_count == 1


def test_complete_stamps_completed_at_and_full_progress(sess):
    t = T.complete(T.create(sess, "work").id, "the result")
    assert t.status == T.TaskStatus.COMPLETED
    assert t.completed_at and t.progress == 1.0
    assert t.result == "the result"


def test_fail_records_the_error(sess):
    t = T.fail(T.create(sess, "work").id, "boom")
    assert t.status == T.TaskStatus.FAILED and t.error == "boom"
    assert t.completed_at


def test_cancel_and_skip_are_terminal(sess):
    a = T.cancel(T.create(sess, "a").id)
    b = T.skip(T.create(sess, "b").id, "not needed")
    assert a.is_terminal and b.is_terminal
    assert b.result == "not needed"


def test_terminal_tasks_do_not_move_without_force(sess):
    t = T.complete(T.create(sess, "done").id)
    assert T.set_status(t.id, T.TaskStatus.PENDING).status == T.TaskStatus.COMPLETED
    assert T.start(t.id).status == T.TaskStatus.COMPLETED


def test_reopen_is_the_only_way_out_of_terminal(sess):
    t = T.complete(T.create(sess, "done").id)
    assert T.reopen(t.id).status == T.TaskStatus.PENDING
    assert T.start(t.id).status == T.TaskStatus.RUNNING


def test_reopen_refuses_to_reopen_into_a_terminal_state(sess):
    t = T.complete(T.create(sess, "done").id)
    assert T.reopen(t.id, T.TaskStatus.FAILED).status == T.TaskStatus.PENDING


def test_unknown_status_falls_back_to_pending(sess):
    t = T.create(sess, "x")
    assert T.set_status(t.id, "sideways").status == T.TaskStatus.PENDING


def test_set_status_on_a_missing_task_returns_none():
    assert T.set_status("nope", T.TaskStatus.RUNNING) is None
    assert T.get("nope") is None


def test_progress_is_clamped(sess):
    t = T.create(sess, "x")
    assert T.set_progress(t.id, 5.0).progress == 1.0
    assert T.set_progress(t.id, -2.0).progress == 0.0
    assert T.set_progress(t.id, 0.5).progress == 0.5


def test_normalize_status_maps_model_vocabulary():
    assert T.normalize_status("in_progress") == T.TaskStatus.RUNNING
    assert T.normalize_status("DONE") == T.TaskStatus.COMPLETED
    assert T.normalize_status("  Completed ") == T.TaskStatus.COMPLETED
    assert T.normalize_status("nonsense") == T.TaskStatus.PENDING
    assert T.normalize_status(None) == T.TaskStatus.PENDING


def test_cancel_open_leaves_finished_work_alone(sess):
    T.sync_list(sess, [{"task": "a", "status": "completed"},
                       {"task": "b", "status": "in_progress"},
                       {"task": "c", "status": "pending"}])
    assert T.cancel_open(sess, "user stopped") == 2
    assert statuses(sess) == [T.TaskStatus.COMPLETED, T.TaskStatus.CANCELLED,
                             T.TaskStatus.CANCELLED]


# ── Ordering ──────────────────────────────────────────────────────────────────

def test_sync_list_sets_display_order(sess):
    T.sync_list(sess, ["one", "two", "three"])
    assert [t.seq for t in T.list_tasks(sess)] == [0, 1, 2]
    assert titles(sess) == ["one", "two", "three"]


def test_reordering_the_plan_reorders_the_list(sess):
    T.sync_list(sess, ["a", "b", "c"])
    T.sync_list(sess, ["c", "a", "b"])
    assert titles(sess) == ["c", "a", "b"]


def test_kept_terminal_tasks_move_to_the_front_in_original_order(sess):
    T.sync_list(sess, [{"task": "a", "status": "completed"},
                       {"task": "b", "status": "completed"},
                       {"task": "c", "status": "pending"}])
    T.sync_list(sess, ["c", "d"])          # model drops the two finished ones
    assert titles(sess) == ["a", "b", "c", "d"]
    assert statuses(sess)[:2] == [T.TaskStatus.COMPLETED, T.TaskStatus.COMPLETED]


# ── The merge: the load-bearing invariants ────────────────────────────────────

def test_sync_list_never_regresses_a_completed_task(sess):
    """⚠️ THE regression this module exists to prevent.

    The model re-sends the checklist with statuses that lag reality. A task that
    reached COMPLETED must stay completed, or the user watches progress run
    backwards and finished work gets redone on a resume.
    """
    T.sync_list(sess, [{"task": "step one", "status": "completed"},
                       {"task": "step two", "status": "in_progress"}])
    T.sync_list(sess, [{"task": "step one", "status": "pending"},
                       {"task": "step two", "status": "pending"}])
    assert statuses(sess) == [T.TaskStatus.COMPLETED, T.TaskStatus.PENDING]


@pytest.mark.parametrize("terminal", sorted(T.TERMINAL))
def test_no_terminal_status_is_regressed_by_a_resend(sess, terminal):
    t = T.create(sess, "step", status=terminal)
    T.sync_list(sess, [{"task": "step", "status": "pending"}])
    assert T.get(t.id).status == terminal


def test_sync_list_advances_an_open_task(sess):
    T.sync_list(sess, [{"task": "step", "status": "pending"}])
    T.sync_list(sess, [{"task": "step", "status": "in_progress"}])
    assert statuses(sess) == [T.TaskStatus.RUNNING]
    T.sync_list(sess, [{"task": "step", "status": "completed"}])
    assert statuses(sess) == [T.TaskStatus.COMPLETED]


def test_dropped_open_task_is_deleted_but_finished_work_is_kept(sess):
    T.sync_list(sess, [{"task": "keep", "status": "completed"},
                       {"task": "drop", "status": "pending"}])
    T.sync_list(sess, [{"task": "keep", "status": "completed"}])
    assert titles(sess) == ["keep"]


def test_titles_match_despite_whitespace_and_case(sess):
    T.sync_list(sess, [{"task": "Run  Tests", "status": "completed"}])
    T.sync_list(sess, [{"task": "run tests", "status": "pending"}])
    assert len(T.list_tasks(sess)) == 1
    assert statuses(sess) == [T.TaskStatus.COMPLETED]


def test_duplicate_titles_stay_two_tasks(sess):
    T.sync_list(sess, ["run tests", "build", "run tests"])
    assert titles(sess) == ["run tests", "build", "run tests"]
    assert len(T.list_tasks(sess)) == 3


def test_sync_list_accepts_bare_strings_and_dicts(sess):
    T.sync_list(sess, ["plain", {"task": "rich", "status": "completed"}])
    assert titles(sess) == ["plain", "rich"]
    assert statuses(sess) == [T.TaskStatus.PENDING, T.TaskStatus.COMPLETED]


def test_sync_list_ignores_empty_and_malformed_items(sess):
    T.sync_list(sess, ["", {"task": "   "}, {"nothing": 1}, "real"])
    assert titles(sess) == ["real"]


def test_sync_list_updates_description_and_priority(sess):
    T.sync_list(sess, [{"task": "x", "description": "first", "priority": 9}])
    T.sync_list(sess, [{"task": "x", "description": "second", "priority": 1}])
    t = T.list_tasks(sess)[0]
    assert t.description == "second" and t.priority == 1


def test_sync_list_on_an_empty_session_id_is_a_noop():
    assert T.sync_list("", ["a"]) == []


def test_repeat_sync_of_an_identical_list_is_stable(sess):
    T.sync_list(sess, ["a", "b"])
    ids = [t.id for t in T.list_tasks(sess)]
    T.sync_list(sess, ["a", "b"])
    assert [t.id for t in T.list_tasks(sess)] == ids     # no churn, same rows


# ── Dependencies ──────────────────────────────────────────────────────────────

def test_dependencies_resolve_by_one_based_index(sess):
    T.sync_list(sess, [{"task": "build"},
                       {"task": "test", "dependencies": [1]}])
    build, test = T.list_tasks(sess)
    assert test.dependencies == [build.id]


def test_dependencies_resolve_by_title(sess):
    T.sync_list(sess, [{"task": "build"},
                       {"task": "test", "dependencies": ["build"]}])
    build, test = T.list_tasks(sess)
    assert test.dependencies == [build.id]


def test_unresolvable_dependency_is_dropped_not_blocking(sess):
    T.sync_list(sess, [{"task": "test", "dependencies": ["ghost", 99]}])
    assert T.list_tasks(sess)[0].dependencies == []
    assert [t.title for t in T.ready(sess)] == ["test"]


def test_self_dependency_is_refused(sess):
    T.sync_list(sess, [{"task": "loop", "dependencies": ["loop"]}])
    assert T.list_tasks(sess)[0].dependencies == []


def test_ready_gates_on_unsettled_dependencies(sess):
    T.sync_list(sess, [{"task": "build"},
                       {"task": "test", "dependencies": [1]}])
    assert [t.title for t in T.ready(sess)] == ["build"]
    build = T.list_tasks(sess)[0]
    T.complete(build.id)
    assert [t.title for t in T.ready(sess)] == ["test"]


def test_ready_orders_by_priority_then_sequence(sess):
    T.sync_list(sess, [{"task": "low", "priority": 9},
                       {"task": "high", "priority": 1},
                       {"task": "mid", "priority": 5}])
    assert [t.title for t in T.ready(sess)] == ["high", "mid", "low"]


def test_blockers_reports_what_is_holding_a_task(sess):
    T.sync_list(sess, [{"task": "build"},
                       {"task": "test", "dependencies": [1]}])
    build, test = T.list_tasks(sess)
    assert [b.id for b in T.blockers(test)] == [build.id]
    T.complete(build.id)
    assert T.blockers(T.get(test.id)) == []


def test_a_skipped_dependency_unblocks_its_dependent(sess):
    """SKIPPED is settled, not pending — a plan must not deadlock behind it."""
    T.sync_list(sess, [{"task": "optional"},
                       {"task": "next", "dependencies": [1]}])
    T.skip(T.list_tasks(sess)[0].id)
    assert [t.title for t in T.ready(sess)] == ["next"]


# ── Summary / current / payload ───────────────────────────────────────────────

def test_summary_counts_and_progress_string(sess):
    T.sync_list(sess, [{"task": "a", "status": "completed"},
                       {"task": "b", "status": "completed"},
                       {"task": "c", "status": "in_progress"},
                       {"task": "d"}, {"task": "e"}])
    s = T.summary(sess)
    assert s["total"] == 5 and s["completed"] == 2
    assert s["progress"] == "2/5" and s["open"] == 3
    assert s["done"] is False
    assert s["counts"][T.TaskStatus.RUNNING] == 1


def test_summary_is_done_when_everything_settled(sess):
    T.sync_list(sess, [{"task": "a", "status": "completed"},
                       {"task": "b", "status": "skipped"}])
    s = T.summary(sess)
    assert s["done"] is True and s["settled"] == 2
    assert s["progress"] == "1/2"           # skipped is settled, not completed


def test_summary_of_an_empty_session(sess):
    s = T.summary(sess)
    assert s["total"] == 0 and s["progress"] == "0/0" and s["done"] is False


def test_current_prefers_running_then_next_open(sess):
    T.sync_list(sess, [{"task": "a", "status": "completed"},
                       {"task": "b"}, {"task": "c", "status": "in_progress"}])
    assert T.current(sess).title == "c"
    T.complete(T.list_tasks(sess)[2].id)
    assert T.current(sess).title == "b"
    T.complete(T.list_tasks(sess)[1].id)
    assert T.current(sess) is None


def test_payload_shape_is_complete(sess):
    T.sync_list(sess, [{"task": "a", "status": "completed"}])
    p = T.payload(sess)
    assert p["session_id"] == sess
    assert p["summary"]["progress"] == "1/1"
    row = p["tasks"][0]
    for key in ("id", "title", "status", "glyph", "seq", "dependencies",
                "progress", "checkpoint", "attempt_count"):
        assert key in row


def _own_project() -> str:
    """A cwd no other test shares.

    ⚠️ The recovery queries are project-scoped and the DB is shared by the whole
    module, so a test that asks about `/proj/a` (the `sess` fixture's cwd) is
    asking about ~90 sessions left behind by its neighbours. Two consequences,
    both of which bit: the `any(...)` assertion became a `LIMIT 20` lottery, and
    the `all(... != sess)` one could pass because the row fell out of the window
    rather than because the query excluded it. Owning the cwd makes the expected
    result set exact.
    """
    return "/proj/own-" + T._uid()


def test_unfinished_sessions_finds_open_work_by_cwd():
    cwd = _own_project()
    sess = T.open_session(chat_id="c-" + T._uid(), cwd=cwd)
    T.sync_list(sess, [{"task": "a", "status": "completed"}, {"task": "b"}])
    assert [(r["id"], r["open_tasks"]) for r in T.unfinished_sessions(cwd=cwd)] \
        == [(sess, 1)]
    assert T.unfinished_sessions(cwd=_own_project()) == []


def test_unfinished_sessions_ignores_a_fully_settled_plan():
    cwd = _own_project()
    sess = T.open_session(chat_id="c-" + T._uid(), cwd=cwd)
    T.sync_list(sess, [{"task": "a", "status": "completed"}])
    assert T.unfinished_sessions(cwd=cwd) == []


def test_unfinished_sessions_orders_newest_first_within_one_second(monkeypatch):
    """⚠️ `updated_at` is second-resolution, so sessions opened in the same second
    tie. Recovery offers the newest, and `LIMIT` makes an untied sort free to drop
    exactly that row — sabotage: remove `s.rowid DESC` and this goes red.

    `_now` is frozen rather than raced: the bug needs a full tie, and a test that
    hopes five writes land inside one second is a test that goes red on a slow
    machine for a reason that has nothing to do with the invariant.
    """
    monkeypatch.setattr(T, "_now", lambda: "2026-01-01 00:00:00")
    cwd = _own_project()
    made = []
    for _ in range(5):
        sid = T.open_session(chat_id="c-" + T._uid(), cwd=cwd)
        T.sync_list(sid, ["work"])
        made.append(sid)
    stamps = {r["updated_at"] for r in T.unfinished_sessions(cwd=cwd)}
    assert stamps == {"2026-01-01 00:00:00"}, "premise broken — writes are not tied"
    assert [r["id"] for r in T.unfinished_sessions(cwd=cwd)] == made[::-1]
    assert [r["id"] for r in T.unfinished_sessions(cwd=cwd, limit=2)] == made[:-3:-1]


def test_delete_session_tasks_clears_the_list(sess):
    T.sync_list(sess, ["a", "b"])
    assert T.delete_session_tasks(sess) == 2
    assert T.list_tasks(sess) == []


# ── Checkpoints (the durable half Task 2 builds on) ───────────────────────────

def test_checkpoint_round_trips(sess):
    t = T.create(sess, "x")
    T.save_checkpoint(t.id, {"step": "reading", "files": ["a.py"]})
    assert T.load_checkpoint(t.id) == {"step": "reading", "files": ["a.py"]}


def test_checkpoint_merges_by_default(sess):
    t = T.create(sess, "x")
    T.save_checkpoint(t.id, {"a": 1})
    T.save_checkpoint(t.id, {"b": 2})
    assert T.load_checkpoint(t.id) == {"a": 1, "b": 2}


def test_checkpoint_can_replace_wholesale(sess):
    t = T.create(sess, "x")
    T.save_checkpoint(t.id, {"a": 1})
    T.save_checkpoint(t.id, {"b": 2}, merge=False)
    assert T.load_checkpoint(t.id) == {"b": 2}


def test_checkpoint_survives_a_reconnect(sess):
    t = T.create(sess, "x")
    T.save_checkpoint(t.id, {"step": 3})
    db.close_all()
    assert T.load_checkpoint(t.id) == {"step": 3}


def test_corrupt_json_columns_degrade_to_defaults(sess):
    """A hand-edited or truncated row must not crash a read."""
    t = T.create(sess, "x")
    db.exe("UPDATE agent_tasks SET checkpoint=?, dependencies=? WHERE id=?",
           ("{not json", "[[[", t.id))
    again = T.get(t.id)
    assert again.checkpoint == {} and again.dependencies == []


# ── sync notifications ────────────────────────────────────────────────────────

def test_tasks_is_a_registered_sync_resource():
    assert "tasks" in sync.RESOURCES


def test_sync_list_publishes_exactly_one_notification(sess):
    """⚠️ One merge, one redraw. A per-row notify would make the CLI panel
    repaint N times for a single `update_todo` — the duplicate-list symptom."""
    seen = []
    sync.subscribe("tasks", lambda topic, payload: seen.append(payload))
    try:
        T.sync_list(sess, ["a", "b", "c"])
    finally:
        sync.unsubscribe("tasks", seen.append)
    assert len([s for s in seen if s.get("event") == "synced"]) == 1


def test_status_change_notifies(sess):
    t = T.create(sess, "x")
    seen = []
    fn = lambda topic, payload: seen.append(payload.get("event"))   # noqa: E731
    sync.subscribe("tasks", fn)
    try:
        T.complete(t.id)
    finally:
        sync.unsubscribe("tasks", fn)
    assert T.TaskStatus.COMPLETED in seen


# ── Rendering (CLI panel) ─────────────────────────────────────────────────────

def test_every_status_has_a_glyph():
    for status in T.ALL_STATUSES:
        assert T.GLYPHS[status]
    assert len(set(T.GLYPHS.values())) == len(T.ALL_STATUSES)


def test_glyphs_match_the_documented_symbols():
    assert T.GLYPHS[T.TaskStatus.PENDING] == "○"
    assert T.GLYPHS[T.TaskStatus.RUNNING] == "●"
    assert T.GLYPHS[T.TaskStatus.COMPLETED] == "✓"


def test_task_glyph_property_tracks_status(sess):
    t = T.create(sess, "x")
    assert t.glyph == "○"
    assert T.start(t.id).glyph == "●"
    assert T.complete(t.id).glyph == "✓"


def test_panel_renders_every_task_and_the_progress_line(sess, capsys, monkeypatch):
    from agent2.cli import taskview
    monkeypatch.setattr(taskview, "_RICH", False)
    taskview.forget()
    T.sync_list(sess, [{"task": "Analyze project", "status": "completed"},
                       {"task": "Implement changes", "status": "in_progress"},
                       {"task": "Run tests"}])
    assert taskview.render(sess) is True
    out = capsys.readouterr().out
    assert "Analyze project" in out and "Run tests" in out
    assert "✓" in out and "●" in out and "○" in out
    assert "1/3 completed" in out


def test_panel_does_not_reprint_an_unchanged_list(sess, capsys, monkeypatch):
    """⚠️ The model re-sends the list constantly; printing every time would
    scroll the terminal with near-identical copies."""
    from agent2.cli import taskview
    monkeypatch.setattr(taskview, "_RICH", False)
    taskview.forget()
    T.sync_list(sess, ["a", "b"])
    assert taskview.render(sess) is True
    capsys.readouterr()
    assert taskview.render(sess) is False
    assert capsys.readouterr().out == ""


def test_panel_reprints_when_a_status_changes(sess, capsys, monkeypatch):
    from agent2.cli import taskview
    monkeypatch.setattr(taskview, "_RICH", False)
    taskview.forget()
    T.sync_list(sess, ["a", "b"])
    taskview.render(sess)
    capsys.readouterr()
    T.complete(T.list_tasks(sess)[0].id)
    assert taskview.render(sess) is True
    assert "1/2 completed" in capsys.readouterr().out


def test_panel_force_always_prints(sess, capsys, monkeypatch):
    from agent2.cli import taskview
    monkeypatch.setattr(taskview, "_RICH", False)
    taskview.forget()
    T.sync_list(sess, ["a"])
    taskview.render(sess)
    capsys.readouterr()
    assert taskview.render(sess, force=True) is True
    assert "a" in capsys.readouterr().out


def test_panel_shows_a_failure_reason(sess, capsys, monkeypatch):
    from agent2.cli import taskview
    monkeypatch.setattr(taskview, "_RICH", False)
    taskview.forget()
    T.sync_list(sess, ["a"])
    T.fail(T.list_tasks(sess)[0].id, "exit code 1")
    taskview.render(sess, force=True)
    out = capsys.readouterr().out
    assert "✗" in out and "exit code 1" in out


def test_panel_on_an_empty_or_unknown_session_prints_nothing(sess, capsys):
    from agent2.cli import taskview
    assert taskview.render(sess) is False
    assert taskview.render("no-such-session") is False
    assert capsys.readouterr().out == ""


def test_render_for_result_ignores_an_unpersisted_result(capsys):
    from agent2.cli import taskview
    assert taskview.render_for_result({"persisted": False}) is False
    assert taskview.render_for_result({}) is False
    assert taskview.render_for_result(None) is False
    assert capsys.readouterr().out == ""


# ── The update_todo tool now writes through ───────────────────────────────────

def test_update_todo_persists_through_the_tool():
    from agent2 import tools as tools_mod
    ctx = tools_mod.ToolContext(sid="s", chat_id="tool-chat-1")
    r = tools_mod.dispatch_tool("update_todo", {"todos": [
        {"task": "scan", "status": "completed"},
        {"task": "report", "status": "pending"}]}, ctx)
    assert r["persisted"] is True and r["progress"] == "1/2"
    assert titles(r["session_id"]) == ["scan", "report"]


def test_update_todo_reports_the_stored_state_not_the_request():
    """⚠️ The tool result is what the MODEL reads next. Echoing the request back
    would tell the model a completed task is pending again."""
    from agent2 import tools as tools_mod
    ctx = tools_mod.ToolContext(sid="s", chat_id="tool-chat-2")
    tools_mod.dispatch_tool("update_todo",
                            {"todos": [{"task": "a", "status": "completed"}]}, ctx)
    r = tools_mod.dispatch_tool("update_todo",
                                {"todos": [{"task": "a", "status": "pending"}]}, ctx)
    assert r["todos"][0]["status"] == T.TaskStatus.COMPLETED
    assert r["progress"] == "1/1"


# ── The two surfaces can actually reach the stored list ───────────────────────
#
# ⚠️ These are the "both surfaces are wired" pins. The engine and its tests can
# all be green while the checklist is invisible to a human — which is exactly the
# state this task started in: `update_todo` persisted nothing and neither surface
# had a render path.

def test_cli_exposes_a_tasks_command():
    """`/tasks` must be dispatchable AND advertised.

    A command missing from SLASH_COMMANDS still works if typed, but it is absent
    from /help and from tab-completion, so in practice nobody finds it.
    """
    import agent2cli as cli
    from agent2.cli.render import SLASH_COMMANDS

    bases = {base for _tok, base, _desc in SLASH_COMMANDS}
    assert "/tasks" in bases
    src = (cli.__file__ or "")
    with open(src, encoding="utf-8") as fh:
        body = fh.read()
    assert 'low == "/tasks"' in body


def test_tasks_command_renders_the_stored_list(capsys, monkeypatch):
    """The panel `/tasks` prints comes from the DB, not from a live turn."""
    from agent2.cli import taskview
    monkeypatch.setattr(taskview, "_RICH", False)
    taskview.forget()
    sess = T.open_session(chat_id="cmd-chat", cwd="/proj/cmd")
    T.sync_list(sess, [{"task": "compile", "status": "completed"},
                       {"task": "link"}])
    # No process boundary needed: nothing about the session is held in memory.
    assert taskview.render(sess, force=True) is True
    out = capsys.readouterr().out
    assert "compile" in out and "link" in out and "1/2 completed" in out


@pytest.fixture
def web():
    """A Flask test client on the migrated DB (same shape as test_health.py)."""
    from flask import Flask

    from agent2.server.routes import register_routes
    db.init_db()
    app = Flask(__name__)
    register_routes(app)
    with app.test_client() as c:
        yield c


def test_chat_tasks_endpoint_returns_the_persisted_checklist(web):
    """⚠️ The browser reload path. `chat_tasks` only reaches a page that is
    already open, so without this endpoint a refresh loses a plan that is still
    sitting in the database."""
    sess = T.open_session(chat_id="web-chat-1", cwd="/proj/web")
    T.sync_list(sess, [{"task": "enumerate", "status": "completed"},
                       {"task": "exploit", "status": "in_progress"}])

    body = web.get("/api/chats/web-chat-1/tasks").get_json()

    assert body["session_id"] == sess
    assert [t["title"] for t in body["tasks"]] == ["enumerate", "exploit"]
    assert [t["status"] for t in body["tasks"]] == [
        T.TaskStatus.COMPLETED, T.TaskStatus.RUNNING]
    assert body["summary"]["progress"] == "1/2"


def test_chat_tasks_endpoint_is_empty_not_an_error_for_an_unplanned_chat(web):
    """A chat where the model never planned anything is a normal state."""
    resp = web.get("/api/chats/no-such-chat/tasks")
    assert resp.status_code == 200
    assert resp.get_json() == {"session_id": "", "tasks": [], "summary": {}}


def test_chat_tasks_endpoint_carries_the_glyph_from_the_engine(web):
    """⚠️ One glyph declaration. If the payload dropped it, the browser would
    grow a second status→symbol map and the two surfaces would drift."""
    sess = T.open_session(chat_id="web-chat-2", cwd="/proj/web")
    T.sync_list(sess, ["a"])
    T.fail(T.list_tasks(sess)[0].id, "boom")

    body = web.get("/api/chats/web-chat-2/tasks").get_json()

    assert body["tasks"][0]["glyph"] == T.GLYPHS[T.TaskStatus.FAILED]
    assert body["tasks"][0]["error"] == "boom"


def test_background_task_endpoint_still_lists_live_turns(web):
    """⚠️ Regression pin. `/api/tasks` (in-flight agent turns from
    core.session) and `/api/chats/<cid>/tasks` (the durable checklist) are
    DIFFERENT things. Adding the second must not shadow the first."""
    resp = web.get("/api/tasks")
    assert resp.status_code == 200
    assert isinstance(resp.get_json(), list)


def test_web_client_listens_for_chat_tasks():
    """The server emits `chat_tasks` from both agent loops; a payload nothing
    listens for is the same as no feature at all."""
    from pathlib import Path

    from agent2.config import ROOT
    js = Path(ROOT, "public", "script.js").read_text(encoding="utf-8")
    assert "socket.on('chat_tasks'" in js
    assert "function renderTaskPanel" in js
    # Updated in place, keyed by session — never appended, or the transcript
    # fills with near-identical cards (the browser twin of the CLI fingerprint).
    assert "getElementById(id)" in js and "loadTaskPanel" in js


def test_web_client_renders_the_checkpoint_trail():
    """The browser half of Task 2. `payload()` ships `checkpoints`; if the client
    ignored them, the Web surface would show WHICH task stopped but never where
    inside it — and would silently drop the destructive-step warning that rule 21
    depends on."""
    from pathlib import Path

    from agent2.config import ROOT
    js = Path(ROOT, "public", "script.js").read_text(encoding="utf-8")
    assert "function cpRows" in js
    assert "d.checkpoints" in js and "cpRows(cps[t.id],t.status)" in js
    # Same wording rule as the CLI: a step caught in flight MAY not have landed.
    assert "was: " in js and "did: " not in js
    assert "destructive_pending" in js
    # Only an unfinished task has anything to resume.
    assert "['running','paused','failed'].includes(status)" in js
    css = Path(ROOT, "public", "style.css").read_text(encoding="utf-8")
    for cls in (".tk-cp", ".tk-cp-done", ".tk-cp-cur", ".tk-cp-stop", ".tk-cp-warn"):
        assert cls in css, f"{cls} has no styling — the rows would render unreadable"


# ══ Task 2 — persistent checkpoints ═══════════════════════════════════════════
#
# A checkpoint answers one question: if we stopped now, where were we? These
# tests pin the three properties that make the answer trustworthy — it is written
# as work happens (so a SIGKILL keeps it), it never walks backwards, and it never
# claims an interrupted destructive step succeeded.

# ── The sub-step model ────────────────────────────────────────────────────────

def test_plan_steps_declares_every_step_as_pending(sess):
    t = T.create(sess, "deploy")
    T.plan_steps(t.id, ["build", "test", "ship"])
    view = T.checkpoint_view(t.id)
    assert view["remaining"] == ["build", "test", "ship"]
    assert view["completed"] == [] and view["current"] == ""
    assert view["total"] == 3


def test_record_step_moves_a_declared_step_without_reordering(sess):
    t = T.create(sess, "deploy")
    T.plan_steps(t.id, ["build", "test", "ship"])
    T.record_step(t.id, "build", T.STEP_COMPLETED)
    T.record_step(t.id, "test", T.STEP_RUNNING)
    view = T.checkpoint_view(t.id)
    assert view["completed"] == ["build"]
    assert view["current"] == "test"
    assert view["remaining"] == ["ship"]


def test_record_step_appends_an_undeclared_step(sess):
    """Callers that discover their steps as they go must not have to pre-declare
    them — the agent loop cannot know which tools the model will call."""
    t = T.create(sess, "explore")
    T.record_step(t.id, "read_file", T.STEP_COMPLETED)
    T.record_step(t.id, "grep_search", T.STEP_COMPLETED)
    assert T.checkpoint_view(t.id)["completed"] == ["read_file", "grep_search"]


def test_record_step_matches_a_retyped_name(sess):
    """Same normalisation as task titles: the code that starts a step and the
    code that finishes it should not have to agree on whitespace or case."""
    t = T.create(sess, "x")
    T.record_step(t.id, "Write File", T.STEP_RUNNING)
    T.record_step(t.id, "  write   file  ", T.STEP_COMPLETED)
    view = T.checkpoint_view(t.id)
    assert view["completed"] == ["Write File"]     # original spelling preserved
    assert view["current"] == "" and view["total"] == 1


def test_a_finished_step_is_never_walked_backwards(sess):
    """⚠️ Same invariant as terminal tasks, one level down. A retry that
    re-reports an earlier step must not un-finish it."""
    t = T.create(sess, "x")
    T.record_step(t.id, "build", T.STEP_COMPLETED)
    T.record_step(t.id, "build", T.STEP_RUNNING)
    T.record_step(t.id, "build", T.STEP_PENDING)
    assert T.checkpoint_view(t.id)["completed"] == ["build"]


def test_a_failed_step_may_be_retried_to_completion(sess):
    """Forward motion is always allowed — the guard is against regression, not
    against a retry that actually succeeds."""
    t = T.create(sess, "x")
    T.record_step(t.id, "build", T.STEP_FAILED)
    assert T.checkpoint_view(t.id)["failed"] == ["build"]
    T.record_step(t.id, "build", T.STEP_COMPLETED)
    view = T.checkpoint_view(t.id)
    assert view["completed"] == ["build"] and view["failed"] == []


def test_save_checkpoint_merges_rather_than_replacing(sess):
    """A caller recording one new key must not blank the rest by omission."""
    t = T.create(sess, "x")
    T.save_checkpoint(t.id, {"cursor": 41, "file": "a.py"})
    T.save_checkpoint(t.id, {"cursor": 42})
    cp = T.load_checkpoint(t.id)
    assert cp == {"cursor": 42, "file": "a.py"}


def test_save_checkpoint_can_replace_when_asked(sess):
    t = T.create(sess, "x")
    T.save_checkpoint(t.id, {"a": 1})
    T.save_checkpoint(t.id, {"b": 2}, merge=False)
    assert T.load_checkpoint(t.id) == {"b": 2}


def test_unknown_checkpoint_keys_survive_step_writes(sess):
    """`steps` is one reserved key among whatever else a caller stores."""
    t = T.create(sess, "x")
    T.save_checkpoint(t.id, {"scan_cursor": "10.0.0.7"})
    T.record_step(t.id, "nmap", T.STEP_COMPLETED)
    assert T.load_checkpoint(t.id)["scan_cursor"] == "10.0.0.7"
    assert T.checkpoint_view(t.id)["completed"] == ["nmap"]


def test_checkpoint_view_of_a_missing_task_is_empty_not_an_error(sess):
    view = T.checkpoint_view("no-such-task")
    assert view["completed"] == [] and view["total"] == 0


# ── Durability: the actual claim ──────────────────────────────────────────────

def test_checkpoint_survives_a_simulated_process_restart(sess):
    """⚠️ THE Task 2 claim. Written as work happens, so a process that never
    gets to run an exit handler still leaves the position on disk."""
    t = T.create(sess, "migrate database")
    T.start(t.id)
    T.plan_steps(t.id, ["dump", "transform", "load", "verify"])
    T.record_step(t.id, "dump", T.STEP_COMPLETED)
    T.record_step(t.id, "transform", T.STEP_COMPLETED)
    T.record_step(t.id, "load", T.STEP_RUNNING)

    db.close_all()          # every pooled connection gone, as after a hard kill

    view = T.checkpoint_view(t.id)
    assert view["completed"] == ["dump", "transform"]
    assert view["current"] == "load"
    assert view["remaining"] == ["verify"]


def test_a_task_running_at_kill_time_is_still_running_afterwards(sess):
    """⚠️ No exit handler runs on SIGKILL, so the RUNNING row IS the signal that
    the previous process died mid-task. Task 3 recovers from exactly this."""
    t = T.create(sess, "long job")
    T.start(t.id)
    T.record_step(t.id, "step one", T.STEP_COMPLETED)

    db.close_all()          # no interrupt(), no atexit — a hard kill

    after = T.get(t.id)
    assert after.status == T.TaskStatus.RUNNING
    assert T.checkpoint_view(after)["completed"] == ["step one"]
    # Newest-first, so this session heads the list — a bare truthiness check here
    # would pass on some *other* test's leftovers and prove nothing.
    assert T.unfinished_sessions("/proj/a")[0]["id"] == sess


# ── interrupt(): the graceful stops ───────────────────────────────────────────

def test_interrupt_parks_a_running_task_and_records_the_reason(sess):
    t = T.create(sess, "scan")
    T.start(t.id)
    T.record_step(t.id, "nmap", T.STEP_RUNNING)

    parked = T.interrupt(sess, "interrupted (Ctrl+C)")

    assert [p.id for p in parked] == [t.id]
    after = T.get(t.id)
    assert after.status == T.TaskStatus.PAUSED
    view = T.checkpoint_view(after)
    assert view["stopped"]["reason"] == "interrupted (Ctrl+C)"
    assert view["stopped"]["at"]
    # ⚠️ "was running" is preserved as the current step, NOT promoted to done.
    assert view["current"] == "nmap"
    assert view["completed"] == []


def test_interrupt_leaves_completed_tasks_alone(sess):
    """Rule 20: recovery must not re-run finished work, so an interrupt may not
    un-finish anything."""
    T.sync_list(sess, [{"task": "a", "status": "completed"},
                       {"task": "b", "status": "in_progress"},
                       {"task": "c"}])
    T.interrupt(sess, "stopped by user")
    assert statuses(sess) == [T.TaskStatus.COMPLETED, T.TaskStatus.PAUSED,
                              T.TaskStatus.PENDING]


def test_a_paused_task_is_still_open_for_recovery(sess):
    """PAUSED is an honest 'started, not finished'. If it counted as settled the
    plan would look complete and recovery would never offer it."""
    t = T.create(sess, "a")
    T.start(t.id)
    T.interrupt(sess, "chat paused")
    assert T.get(t.id).is_open is True
    assert T.get(t.id).is_terminal is False
    assert T.unfinished_sessions("/proj/a")[0]["id"] == sess


def test_interrupt_on_an_idle_session_changes_nothing(sess):
    T.sync_list(sess, ["a", "b"])
    assert T.interrupt(sess, "stopped") == []
    assert statuses(sess) == [T.TaskStatus.PENDING, T.TaskStatus.PENDING]


def test_interrupt_fires_exactly_one_notification(sess):
    """Same rule as sync_list: one user-visible change, one notify."""
    for name in ("a", "b", "c"):
        T.start(T.create(sess, name).id)
    seen = []
    fn = lambda topic, payload: seen.append(payload)   # noqa: E731
    sync.subscribe("tasks", fn)
    try:
        T.interrupt(sess, "stopped")
    finally:
        sync.unsubscribe("tasks", fn)
    assert len(seen) == 1
    assert seen[0]["event"] == "interrupt" and seen[0]["count"] == 3


# ── Rule 21: never blindly retry an uncertain destructive operation ───────────

def test_an_interrupted_destructive_step_is_flagged(sess):
    """⚠️ A `write_file` that was in flight at kill time may or may not have
    landed. `destructive_pending` is what lets recovery ask instead of guess."""
    t = T.create(sess, "patch config")
    T.start(t.id)
    T.record_step(t.id, "read_file", T.STEP_COMPLETED, destructive=False)
    T.record_step(t.id, "write_file", T.STEP_RUNNING, destructive=True)

    T.interrupt(sess, "interrupted (Ctrl+C)")

    assert T.checkpoint_view(t.id)["destructive_pending"] is True


def test_a_completed_destructive_step_is_not_pending(sess):
    t = T.create(sess, "patch config")
    T.start(t.id)
    T.record_step(t.id, "write_file", T.STEP_COMPLETED, destructive=True)
    T.interrupt(sess, "stopped")
    assert T.checkpoint_view(t.id)["destructive_pending"] is False


def test_the_destructive_tool_set_covers_every_mutating_tool():
    """A tool missing from this set is one recovery would silently repeat."""
    for tool in ("write_file", "multi_edit_files", "delete_file", "run_command"):
        assert tool in T.DESTRUCTIVE_TOOLS
    for tool in ("read_file", "list_dir", "grep_search", "web_search"):
        assert tool not in T.DESTRUCTIVE_TOOLS


# ── The automatic half: tools checkpoint themselves ───────────────────────────

def test_a_tool_call_is_recorded_against_the_running_task():
    from agent2 import tools as tools_mod
    ctx = tools_mod.ToolContext(sid="s", chat_id="cp-chat-1")
    r = tools_mod.dispatch_tool("update_todo", {"todos": [
        {"task": "inspect", "status": "in_progress"}]}, ctx)
    sess = r["session_id"]

    tools_mod.dispatch_tool("list_dir", {"path": "."}, ctx)

    task = T.current(sess)
    assert T.checkpoint_view(task)["completed"] == ["list_dir"]


def test_the_step_detail_names_the_target_not_the_payload():
    """A resume line says 'was writing config.py', never the file's contents."""
    from agent2 import tools as tools_mod
    ctx = tools_mod.ToolContext(sid="s", chat_id="cp-chat-2")
    r = tools_mod.dispatch_tool("update_todo", {"todos": [
        {"task": "inspect", "status": "in_progress"}]}, ctx)
    tools_mod.dispatch_tool("list_dir", {"path": "some/dir"}, ctx)

    steps = T.checkpoint_view(T.current(r["session_id"]))["steps"]
    assert steps[0]["detail"] == "some/dir"


def test_a_destructive_tool_is_flagged_automatically():
    """⚠️ The flag comes from the tool NAME at the chokepoint, so no caller can
    forget to set it — that omission is what would make recovery unsafe."""
    from agent2 import tools as tools_mod
    ctx = tools_mod.ToolContext(sid="s", chat_id="cp-chat-3")
    r = tools_mod.dispatch_tool("update_todo", {"todos": [
        {"task": "edit", "status": "in_progress"}]}, ctx)
    tools_mod.dispatch_tool("delete_file", {"path": "no-such-file-xyz"}, ctx)

    steps = T.checkpoint_view(T.current(r["session_id"]))["steps"]
    assert steps[0]["name"] == "delete_file"
    assert steps[0]["destructive"] is True


def test_a_destructive_tool_is_marked_in_flight_BEFORE_it_runs(monkeypatch):
    """⚠️ Rule 21 depends on this and nothing else.

    A process killed *during* `delete_file` never reaches the post-call note, so
    if the step were only recorded on return it would not exist at all — and
    `destructive_pending` would read False, i.e. recovery would report "nothing
    was in flight" and let the model delete the file a second time. The pre-call
    note is the whole mechanism; this test kills the tool mid-call to prove the
    breadcrumb is already on disk by then.
    """
    from agent2 import tools as tools_mod
    ctx = tools_mod.ToolContext(sid="s", chat_id="cp-chat-8")
    r = tools_mod.dispatch_tool("update_todo", {"todos": [
        {"task": "remove it", "status": "in_progress"}]}, ctx)
    sess = r["session_id"]

    def die(*a, **k):
        raise KeyboardInterrupt("killed mid-write")

    monkeypatch.setattr(tools_mod.REGISTRY, "call", die)
    with pytest.raises(KeyboardInterrupt):
        tools_mod.dispatch_tool("delete_file", {"path": "doomed.py"}, ctx)

    view = T.checkpoint_view(T.current(sess))
    assert view["current"] == "delete_file"
    assert view["destructive_pending"] is True
    assert view["steps"][0]["detail"] == "doomed.py"


def test_a_read_only_tool_is_recorded_once_not_twice():
    """The pre-call note is bought for destructive tools only: a `read_file`
    cannot leave the workspace in an unknown state, so a second checkpoint write
    per read would be pure cost."""
    from agent2 import tools as tools_mod
    ctx = tools_mod.ToolContext(sid="s", chat_id="cp-chat-9")
    r = tools_mod.dispatch_tool("update_todo", {"todos": [
        {"task": "look", "status": "in_progress"}]}, ctx)

    calls = []
    real = T.note_tool
    T_note = T.note_tool

    def counting(session_id, tool, **kw):
        calls.append((tool, kw.get("ok")))
        return T_note(session_id, tool, **kw)

    T.note_tool = counting
    try:
        tools_mod.dispatch_tool("list_dir", {"path": "."}, ctx)
        tools_mod.dispatch_tool("delete_file", {"path": "no-such-file-xyz"}, ctx)
    finally:
        T.note_tool = real

    assert calls == [("list_dir", True),
                     ("delete_file", None), ("delete_file", False)]
    # One STEP each, either way — the pre-call note updates the same row.
    assert T.checkpoint_view(T.current(r["session_id"]))["total"] == 2


def test_both_loops_mark_run_command_in_flight_before_running_it():
    """⚠️ `run_command` is the one tool that does NOT pass through
    `dispatch_tool`, so it does not inherit the pre-call note — each loop has to
    do it itself, and a loop that forgot would lose the in-flight breadcrumb for
    the least reversible tool there is.

    Asserted on source order because neither loop can be driven here without a
    live Socket.IO server and a real shell; the fact under test is precisely
    *which side of `stream_command`* the note sits on.

    The call is matched on `stream_command(` alone, WITHOUT its first argument:
    pinning `stream_command(cmd,` made this test fail the moment the call was
    wrapped across lines, which is a formatting change and not the invariant.
    """
    import inspect
    from agent2 import agent as agent_mod
    from agent2.llm import provider_agent as pa_mod

    for mod in (agent_mod, pa_mod):
        src = inspect.getsource(mod)
        pre = src.find('note_tool("run_command", detail=')
        call = src.find("stream_command(", pre)
        post = src.find('note_tool("run_command", ok=')
        assert pre != -1, f"{mod.__name__}: no pre-call run_command note"
        assert call != -1, f"{mod.__name__}: no stream_command call found"
        assert 0 < pre < call < post, \
            f"{mod.__name__}: run_command note is not recorded before the command runs"


def test_update_todo_is_not_recorded_as_its_own_sub_step():
    """It DEFINES the checklist; logging it inside the checklist is noise, and it
    would show as the current sub-step every time the model re-planned."""
    from agent2 import tools as tools_mod
    ctx = tools_mod.ToolContext(sid="s", chat_id="cp-chat-4")
    r = tools_mod.dispatch_tool("update_todo", {"todos": [
        {"task": "work", "status": "in_progress"}]}, ctx)
    tools_mod.dispatch_tool("update_todo", {"todos": [
        {"task": "work", "status": "in_progress"}]}, ctx)

    assert T.checkpoint_view(T.current(r["session_id"]))["total"] == 0


def test_a_tool_call_with_no_running_task_records_nothing():
    from agent2 import tools as tools_mod
    ctx = tools_mod.ToolContext(sid="s", chat_id="cp-chat-5")
    r = tools_mod.dispatch_tool("update_todo",
                                {"todos": [{"task": "later"}]}, ctx)   # pending
    tools_mod.dispatch_tool("list_dir", {"path": "."}, ctx)
    for task in T.list_tasks(r["session_id"]):
        assert T.checkpoint_view(task)["total"] == 0


def test_checkpointing_never_creates_a_task_session():
    """⚠️ Checkpointing runs on EVERY tool call. If it created sessions, every
    turn that never planned anything would leave an empty session row behind —
    and those rows are exactly what recovery offers the user."""
    from agent2 import tools as tools_mod
    ctx = tools_mod.ToolContext(sid="s", chat_id="cp-chat-6")
    tools_mod.dispatch_tool("list_dir", {"path": "."}, ctx)
    tools_mod.dispatch_tool("read_file", {"path": "nope-xyz"}, ctx)
    assert T.latest_session_for_chat("cp-chat-6") is None
    assert ctx.existing_task_session() == ""


def test_a_failing_checkpoint_does_not_break_the_tool(monkeypatch):
    """Graceful degradation: a breadcrumb that cannot be written must never turn
    a working tool call into an error."""
    from agent2 import tools as tools_mod
    ctx = tools_mod.ToolContext(sid="s", chat_id="cp-chat-7")
    r = tools_mod.dispatch_tool("update_todo", {"todos": [
        {"task": "work", "status": "in_progress"}]}, ctx)
    assert r["persisted"] is True

    def boom(*a, **k):
        raise RuntimeError("db is on fire")

    monkeypatch.setattr(T, "note_tool", boom)
    out = tools_mod.dispatch_tool("list_dir", {"path": "."}, ctx)
    assert "error" not in out


# ── The stop hooks on both surfaces ───────────────────────────────────────────

def test_pausing_a_chat_stamps_the_task_checkpoint():
    """⚠️ /pause stays a CHAT-SESSION control (rules 17-18). It records where the
    task was; it does not drive recovery, retry or resume."""
    from agent2.core import context as core_context
    from agent2 import tools as tools_mod

    db.exe("INSERT INTO chats(id, title, model, mode, cwd, status)"
           " VALUES(?,?,?,?,?,?)",
           ("pause-chat", "t", "2.5-flash", "pro", "/proj/p", "active"))
    ctx = tools_mod.ToolContext(sid="s", chat_id="pause-chat")
    r = tools_mod.dispatch_tool("update_todo", {"todos": [
        {"task": "long job", "status": "in_progress"}]}, ctx)

    core_context.pause_chat("pause-chat")

    assert db.qone("SELECT status FROM chats WHERE id=?",
                   ("pause-chat",))["status"] == "paused"
    task = T.list_tasks(r["session_id"])[0]
    assert task.status == T.TaskStatus.PAUSED
    assert T.checkpoint_view(task)["stopped"]["reason"] == "chat paused"


def test_resuming_a_chat_does_not_touch_task_state():
    """⚠️ Rule 18 pinned. /resume reactivates the CHAT; it must not restart,
    reopen or re-run a single task."""
    from agent2.core import context as core_context
    from agent2 import tools as tools_mod

    db.exe("INSERT INTO chats(id, title, model, mode, cwd, status)"
           " VALUES(?,?,?,?,?,?)",
           ("resume-chat", "t", "2.5-flash", "pro", "/proj/p", "active"))
    ctx = tools_mod.ToolContext(sid="s", chat_id="resume-chat")
    r = tools_mod.dispatch_tool("update_todo", {"todos": [
        {"task": "a", "status": "completed"},
        {"task": "b", "status": "in_progress"}]}, ctx)
    core_context.pause_chat("resume-chat")
    before = statuses(r["session_id"])

    core_context.resume_chat("resume-chat")

    assert statuses(r["session_id"]) == before
    assert before == [T.TaskStatus.COMPLETED, T.TaskStatus.PAUSED]


def test_cli_stamps_the_checkpoint_on_interrupt(monkeypatch):
    """The Ctrl+C path. `_checkpoint_stop` is the CLI's only job here — the
    sub-steps are already on disk."""
    import agent2cli as cli
    from agent2 import tools as tools_mod

    ctx = tools_mod.ToolContext(sid="s", chat_id="cli-int-chat")
    r = tools_mod.dispatch_tool("update_todo", {"todos": [
        {"task": "build", "status": "in_progress"}]}, ctx)
    monkeypatch.setattr(cli.tooling, "tool_ctx", lambda: ctx)

    cli._checkpoint_stop("interrupted (Ctrl+C)")

    task = T.list_tasks(r["session_id"])[0]
    assert task.status == T.TaskStatus.PAUSED
    assert T.checkpoint_view(task)["stopped"]["reason"] == "interrupted (Ctrl+C)"


def test_cli_interrupt_is_silent_when_there_is_no_plan(monkeypatch, capsys):
    import agent2cli as cli
    from agent2 import tools as tools_mod

    ctx = tools_mod.ToolContext(sid="s", chat_id="cli-noplan-chat")
    monkeypatch.setattr(cli.tooling, "tool_ctx", lambda: ctx)
    cli._checkpoint_stop("interrupted (Ctrl+C)")     # must not raise
    assert T.latest_session_for_chat("cli-noplan-chat") is None


def test_web_stop_agent_stamps_the_checkpoint():
    """The browser's Stop button reaches the same recording as Ctrl+C."""
    from agent2.server import sockets
    from agent2 import tools as tools_mod

    ctx = tools_mod.ToolContext(sid="s", chat_id="web-stop-chat")
    r = tools_mod.dispatch_tool("update_todo", {"todos": [
        {"task": "scan", "status": "in_progress"}]}, ctx)

    sockets._checkpoint_stop("web-stop-chat", "stopped by user")

    task = T.list_tasks(r["session_id"])[0]
    assert task.status == T.TaskStatus.PAUSED
    assert T.checkpoint_view(task)["stopped"]["reason"] == "stopped by user"


def test_web_stop_on_an_unknown_chat_is_a_no_op():
    from agent2.server import sockets
    sockets._checkpoint_stop("no-such-chat", "stopped by user")     # must not raise
    sockets._checkpoint_stop(None, "stopped by user")


# ── Surfacing it ──────────────────────────────────────────────────────────────

def test_payload_carries_the_checkpoint_view(sess):
    t = T.create(sess, "deploy")
    T.start(t.id)
    T.plan_steps(t.id, ["build", "ship"])
    T.record_step(t.id, "build", T.STEP_COMPLETED)

    view = T.payload(sess)["checkpoints"][t.id]

    assert view["completed"] == ["build"] and view["remaining"] == ["ship"]


def test_payload_omits_checkpoints_for_tasks_that_have_none(sess):
    """Attaching an empty view to every task would triple the payload to say
    nothing."""
    T.sync_list(sess, ["a", "b", "c"])
    assert T.payload(sess)["checkpoints"] == {}


def test_tasks_command_shows_where_execution_stopped(sess, capsys, monkeypatch):
    """What `/tasks` prints after an interrupted run."""
    from agent2.cli import taskview
    monkeypatch.setattr(taskview, "_RICH", False)
    taskview.forget()
    t = T.create(sess, "Migrate database")
    T.start(t.id)
    T.plan_steps(t.id, ["dump", "transform", "load"])
    T.record_step(t.id, "dump", T.STEP_COMPLETED)
    T.record_step(t.id, "transform", T.STEP_RUNNING)
    T.interrupt(sess, "interrupted (Ctrl+C)")

    assert taskview.render(sess, force=True, detail=True) is True

    out = capsys.readouterr().out
    assert "done: dump" in out
    assert "was: transform" in out           # not "completed" — it was in flight
    assert "left: load" in out
    assert "interrupted (Ctrl+C)" in out


def test_the_live_panel_stays_quiet_about_sub_steps(sess, capsys, monkeypatch):
    """⚠️ Sub-steps advance on every tool call. Showing them in the live panel
    would force a redraw per tool call — the duplicate-list scrolling all over
    again. `/tasks` opts in; the automatic panel does not."""
    from agent2.cli import taskview
    monkeypatch.setattr(taskview, "_RICH", False)
    taskview.forget()
    t = T.create(sess, "Migrate database")
    T.start(t.id)
    T.record_step(t.id, "dump", T.STEP_COMPLETED)

    taskview.render(sess, force=True)
    out = capsys.readouterr().out

    assert "Migrate database" in out
    assert "dump" not in out


def test_a_sub_step_alone_does_not_reprint_the_panel(sess, capsys, monkeypatch):
    from agent2.cli import taskview
    monkeypatch.setattr(taskview, "_RICH", False)
    taskview.forget()
    t = T.create(sess, "a")
    T.start(t.id)
    taskview.render(sess)
    capsys.readouterr()

    T.record_step(t.id, "step one", T.STEP_COMPLETED)

    assert taskview.render(sess) is False
    assert capsys.readouterr().out == ""


def _rich_render(monkeypatch, session_id, **kw) -> str:
    """Render through the REAL rich path and return the text it drew.

    The other rendering tests force `_RICH = False`, which is the plain fallback
    — so the branch an actual themed terminal takes was going unexercised. That
    gap was real: a sabotage that deleted the detail rows from `_render_rich`
    alone left the whole suite green.
    """
    from rich.console import Console
    from agent2.cli import taskview
    rec = Console(record=True, width=100, force_terminal=False, no_color=True)
    monkeypatch.setattr(taskview, "_RICH", True)
    monkeypatch.setattr(taskview, "_con", rec)
    taskview.forget()
    assert taskview.render(session_id, **kw) is True
    return rec.export_text()


def test_the_rich_panel_shows_the_same_checkpoint_lines(sess, monkeypatch):
    """One `_CP_STYLE` table, two renderers: a terminal WITH colour must never
    show less than one without it."""
    pytest.importorskip("rich")
    t = T.create(sess, "Migrate database")
    T.start(t.id)
    T.plan_steps(t.id, ["dump", "transform", "load"])
    T.record_step(t.id, "dump", T.STEP_COMPLETED)
    T.record_step(t.id, "transform", T.STEP_RUNNING)
    T.interrupt(sess, "interrupted (Ctrl+C)")

    out = _rich_render(monkeypatch, sess, force=True, detail=True)

    assert "done: dump" in out
    assert "was: transform" in out
    assert "left: load" in out
    assert "interrupted (Ctrl+C)" in out


def test_the_rich_live_panel_also_stays_quiet(sess, monkeypatch):
    pytest.importorskip("rich")
    t = T.create(sess, "Migrate database")
    T.start(t.id)
    T.record_step(t.id, "dump", T.STEP_COMPLETED)

    out = _rich_render(monkeypatch, sess, force=True)

    assert "Migrate database" in out
    assert "dump" not in out


# ── The project key: one canonical form, or recovery finds nothing ────────────
#
# ⚠️ Found by a two-process probe, not by a unit test: task sessions stored
# `workspace.root()` verbatim while chats stored the normcased form, so on Windows
# the write said `C:\Users\…` and the read asked for `c:\users\…`. Every
# individual row read fine — only the project-scoped recovery query went quiet.

def test_a_session_stores_the_canonical_project_key():
    import os
    from agent2.core import context as core_context

    raw = os.path.abspath(os.getcwd())
    sid = T.open_session(chat_id="pk-chat", cwd=raw)
    stored = T.get_session(sid)["cwd"]
    assert stored == core_context.project_key(raw)
    assert stored == core_context.current_cwd()


def test_recovery_finds_a_session_stored_under_any_spelling():
    """The lookup normalises too, so neither side has to remember to."""
    import os

    raw = os.path.abspath(os.getcwd())
    sid = T.open_session(chat_id="pk-chat-2", cwd=raw)
    T.start(T.create(sid, "unfinished work").id)

    for spelling in (raw, raw.upper(), raw.lower(), raw + os.sep):
        found = [r["id"] for r in T.unfinished_sessions(spelling)]
        assert sid in found, f"recovery lost the session for {spelling!r}"


def test_a_chat_and_its_task_session_agree_on_the_project():
    """The two tables are joined by project in the recovery path, so a
    disagreement here is a recovery that silently returns nothing."""
    from agent2.core import context as core_context
    from agent2 import tools as tools_mod

    chat = core_context.new_chat("2.5-flash", "pro")
    ctx = tools_mod.ToolContext(sid="s", chat_id=str(chat["id"]))
    r = tools_mod.dispatch_tool("update_todo", {"todos": [
        {"task": "work", "status": "in_progress"}]}, ctx)

    assert T.get_session(r["session_id"])["cwd"] == chat["cwd"]


def test_migration_10_repairs_a_session_written_the_old_way():
    """Rule 22: an existing DB keeps its data — the row is repaired, not dropped."""
    import os
    from agent2.core import context as core_context

    raw = os.path.abspath(os.getcwd())
    if raw == core_context.project_key(raw):
        pytest.skip("paths are already canonical on this platform")

    sid = T.open_session(chat_id="pk-legacy", cwd=raw)
    db.exe("UPDATE task_sessions SET cwd=? WHERE id=?", (raw, sid))   # pre-fix write
    T.start(T.create(sid, "stranded").id)
    # The bug, reproduced: written as `C:\…`, queried as `c:\…`, never matched.
    assert sid not in [r["id"] for r in T.unfinished_sessions(raw)]

    with db._checkout() as (conn, _owned):            # the migration step
        db._normalize_task_session_cwd(conn)
        conn.commit()

    assert sid in [r["id"] for r in T.unfinished_sessions(raw)]
    assert T.get_session(sid)["chat_id"] == "pk-legacy"     # nothing else touched


def test_migration_10_is_registered_and_contiguous():
    versions = [v for v, _, _ in db._MIGRATIONS]
    assert versions == list(range(1, len(versions) + 1))
    assert (10, "task_sessions.cwd_normalized") == tuple(
        x for x in db._MIGRATIONS if x[0] == 10)[0][:2]
    assert db.SCHEMA_VERSION >= 10


def test_update_todo_keeps_chats_independent():
    from agent2 import tools as tools_mod
    a = tools_mod.ToolContext(sid="s", chat_id="indep-a")
    b = tools_mod.ToolContext(sid="s", chat_id="indep-b")
    tools_mod.dispatch_tool("update_todo", {"todos": [{"task": "only-a"}]}, a)
    tools_mod.dispatch_tool("update_todo", {"todos": [{"task": "only-b"}]}, b)
    assert titles(a.task_session()) == ["only-a"]
    assert titles(b.task_session()) == ["only-b"]
    assert a.task_session() != b.task_session()


def test_update_todo_without_a_ctx_still_works():
    """FAILSAFE: a raw call (no session) degrades to the in-memory list."""
    from agent2 import tools as tools_mod
    tools_mod._TODO_FALLBACK.clear()
    r = tools_mod.dispatch_tool("update_todo",
                                {"todos": [{"task": "x", "status": "completed"}]})
    assert r["persisted"] is False and r["progress"] == "1/1"


def test_update_todo_degrades_when_persistence_fails(monkeypatch):
    """FAILSAFE: a DB fault must not fail the tool call and lose the update."""
    from agent2 import tools as tools_mod
    from agent2.core import tasks as tasks_mod

    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(tasks_mod, "sync_list", boom)
    ctx = tools_mod.ToolContext(sid="s", chat_id="degrade")
    r = tools_mod.dispatch_tool("update_todo", {"todos": [{"task": "x"}]}, ctx)
    assert r["persisted"] is False and r["total"] == 1


def test_update_todo_mirror_carries_ids():
    """The mirror the agent loops read must expose the durable id, so a later
    checkpoint or status write can target the right row."""
    from agent2 import tools as tools_mod
    ctx = tools_mod.ToolContext(sid="s", chat_id="mirror")
    tools_mod.dispatch_tool("update_todo", {"todos": [{"task": "x"}]}, ctx)
    assert ctx.todos and ctx.todos[0]["id"]
    assert T.get(ctx.todos[0]["id"]) is not None
