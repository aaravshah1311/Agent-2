# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
Tests for the workflow subsystem (``agent2/core/workflow/``, Task 37).

Run from the repo root:  python -m pytest .github/tests/test_workflow.py -v

What this suite is for
──────────────────────
Task 37's bar is *"not a second execution engine; nodes are tasks, with the
existing checkpoints and recovery"*, so most of what a workflow needs was already
here: `agent_tasks.dependencies` is the edge list, `tasks.ready()` is the
scheduler, `exec_workflows` is the durable breadcrumb and `tasks.set_status()` is
the reason completed work stays completed. That makes this suite unusual — it
mostly proves that **nothing was rebuilt**, and it pins the two genuine defects a
*declared* graph introduces that an ad-hoc task list does not:

* ``test_a_cycle_is_refused_here_because_ready_deadlocks_on_it_forever`` — written
  as a PAIR. It refuses the cycle at validation, then hand-builds the cyclic task
  rows and shows `tasks.ready()` returning `[]` *forever*: no exception, no error
  row, no log line, just a screen of PENDING work that never starts. Either half
  alone would also pass against a validator that refused everything.
* ``test_an_unknown_dependency_is_fatal_here_and_deliberately_not_in_ready`` — the
  same shape in the opposite direction. `ready()` ignores a dangling dependency on
  purpose, which is right for a todo list whose third item was deleted and wrong
  for `needs: [tets]`, where it releases the node to run **immediately, out of
  order**, with a plausible result.
* ``test_a_level_orders_the_way_ready_does`` — the graph's answer to "what runs
  next" and the scheduler's answer are checked against each other on real rows,
  because two orderings would each look correct alone.
* ``test_a_completed_node_is_not_re_run_by_a_resume`` — the user's standing rule
  that recovery never repeats finished work. It attempts the wrong thing
  (`set_status` back to RUNNING) rather than trusting the right thing.
* ``test_a_held_node_reads_blocked_and_a_resume_does_not_un_hold_it`` — the nine
  core states project onto five phases here, and PAUSED is the one the old ladder
  could not express. `tasks.ready()` still returns a paused row (PAUSED is OPEN),
  so a projection reading it as READY would offer a surface the one node a human
  deliberately held — and `resume()` filters on the phase, so the drift would
  travel.
* ``test_settle_reads_the_verdict_off_disk_not_from_its_caller`` — "worker says
  done ⇒ Agent2 says done" is forbidden, so a driver claiming success over a FAILED
  node row must lose.
* ``test_a_turn_with_no_workflow_pays_one_indexed_read`` and
  ``test_the_block_inlines_only_the_current_node_and_names_the_rest`` — the context
  bar. Idle costs one query; busy costs one node's prose, never the whole plan.
* ``test_no_node_instruction_ever_reaches_a_payload`` — a node instruction is prose
  a human wrote in their own checkout, and `GET /api/workflows` is a browser
  surface.

conftest.py redirects AGENT2_DB to a throwaway temp DB, so nothing here touches a
developer's real agent2.db.
"""

import json
from contextlib import contextmanager

import pytest

from agent2 import config as cfg
from agent2 import database as db
from agent2.core import broker as B
from agent2.core import execstate as X
from agent2.core import tasks as T
from agent2.core import workflow as WF
from agent2.core.dag import model as M
from agent2.core.workflow import graph as G
from agent2.core.workflow import runner as R

# A token that must never leave the task row it was written to.
SECRET_NOTE = "INSTRUCTION-DO-NOT-LEAK-9f31"


@pytest.fixture(autouse=True)
def _clean():
    """Workflow runs are file-backed and project-scoped — clear them both ways.

    Rows are deleted rather than the file replaced: the suites share one temp
    database, and `live()` deliberately answers "the newest unsettled run in this
    project", so one leftover row from a previous test would make the next one
    read somebody else's workflow.
    """
    db.init_db()
    X.reset()
    db.exe("DELETE FROM exec_workflows")
    X._failure_reported = False
    yield
    X.reset()
    db.exe("DELETE FROM exec_workflows")
    X._failure_reported = False


# ── Helpers ───────────────────────────────────────────────────────────────────

def _def(name="demo", nodes=(), **kw):
    """A definition through the same coercion a Task 38 file will use."""
    return G.make_def({"name": name, "nodes": list(nodes), **kw})


def _node(nid, *, needs=(), instruction="do it", **kw):
    return {"id": nid, "title": nid.title(), "instruction": instruction,
            "needs": list(needs), **kw}


def _chain(*ids, instruction="do it"):
    """`a → b → c`: each node needs the one before it."""
    out = []
    prev = None
    for nid in ids:
        out.append(_node(nid, needs=(prev,) if prev else (),
                         instruction=f"{instruction} ({nid})"))
        prev = nid
    return out


def _phases(state):
    return [(n.node, n.phase) for n in state.nodes]


def _counts():
    """Row counts across every table a run touches."""
    return (
        db.qone("SELECT COUNT(*) AS c FROM exec_workflows")["c"],
        db.qone("SELECT COUNT(*) AS c FROM task_sessions")["c"],
        db.qone("SELECT COUNT(*) AS c FROM agent_tasks")["c"],
    )


def _run(nodes, *, name="demo", chat_id="chat-wf", **kw):
    run = R.instantiate(_def(name, nodes, **kw), chat_id=chat_id)
    assert run.ok, run.reason
    return run


@contextmanager
def _queries(monkeypatch):
    """Count reads for the duration of the block.

    Valid because `execstate._rows()` and `tasks` resolve `qall`/`qone` from the
    `database` module at call time rather than binding them at import.
    """
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


# ── The graph: the two defects a declaration introduces ───────────────────────

def test_a_cycle_is_refused_here_because_ready_deadlocks_on_it_forever():
    """⚠️ THE LOAD-BEARING PAIR. Half one: the validator refuses the ring.

    Half two: the same ring written straight into `agent_tasks` — which is what a
    runner without this check would produce — and `tasks.ready()` returns `[]`
    every time it is asked. There is no exception and no error row to find, so the
    plan simply never advances. That is why `validate()` runs BEFORE any write.
    """
    v = G.validate(_def("ring", [_node("a", needs=("b",)), _node("b", needs=("a",))]))
    assert not v.ok
    assert [p["code"] for p in v.problems] == [G.P_CYCLE]
    assert v.cycles and set(v.cycles[0]) == {"a", "b"}
    assert v.levels == ()                      # no honest order exists
    assert "a" in v.summary() and "→" in v.summary()

    # And this is the state that refusal prevents.
    sess = T.open_session(chat_id="cyc", goal="ring")
    t1 = T.create(sess, "a")
    t2 = T.create(sess, "b", dependencies=[t1.id])
    db.exe("UPDATE agent_tasks SET dependencies=? WHERE id=?",
           (json.dumps([t2.id]), t1.id))
    assert T.ready(sess) == []
    assert T.ready(sess) == []                 # …and it stays empty, forever
    assert {t.status for t in T.list_tasks(sess)} == {T.TaskStatus.PENDING}


def test_an_unknown_dependency_is_fatal_here_and_deliberately_not_in_ready():
    """The mirror image: the same tolerance that protects a todo list corrupts a
    declared graph, so the check lives at the declaration and `ready()` is left
    exactly as it is."""
    v = G.validate(_def("typo", [_node("build"), _node("test", needs=("tets",))]))
    assert not v.ok
    assert [p["code"] for p in v.problems] == [G.P_UNKNOWN_DEP]
    assert v.problems[0]["needs"] == "tets" and v.problems[0]["node"] == "test"

    # `ready()` releases a dangling reference immediately — by design, documented in
    # its own docstring. In a workflow that is a node running out of order.
    sess = T.open_session(chat_id="dangle", goal="typo")
    t = T.create(sess, "test", dependencies=["tets"])
    assert [r.id for r in T.ready(sess)] == [t.id]


def test_a_self_dependency_is_its_own_problem_not_a_cycle():
    """A ring of one is a typo with an obvious fix, and `_find_cycles` never sees
    it because the edge is filtered out — so it needs its own code, or it would be
    reported as "the graph has no runnable order"."""
    v = G.validate(_def("self", [_node("a", needs=("a",))]))
    assert not v.ok
    assert [p["code"] for p in v.problems] == [G.P_SELF_DEP]
    assert v.cycles == ()


def test_levels_are_a_topological_order_not_a_declaration_order():
    v = G.validate(_def("build", [
        _node("report", needs=("scan", "notes")),
        _node("scan"),
        _node("notes"),
    ]))
    assert v.ok, v.summary()
    assert v.levels[0] == ("scan", "notes") or v.levels[0] == ("notes", "scan")
    assert v.levels[1] == ("report",)
    # Every dependency sits in a strictly earlier level — the property Task 41 needs.
    at = {nid: i for i, level in enumerate(v.levels) for nid in level}
    assert at["report"] > at["scan"] and at["report"] > at["notes"]


def test_a_level_orders_the_way_ready_does():
    """⚠️ Pinned against real rows, not asserted twice.

    `levels_for()` and `tasks.ready()` answer the same question for different
    callers. A graph that ordered two runnable nodes differently from the scheduler
    that dispatches them would be self-consistent on both sides and wrong in the
    middle, so the two orders are compared on the rows a real run wrote.
    """
    nodes = [_node("late", priority=9), _node("early", priority=1),
             _node("mid", priority=5)]
    v = G.validate(_def("prio", nodes))
    assert v.levels == (("early", "mid", "late"),)

    run = _run(nodes, name="prio")
    by_task = {tid: nid for nid, tid in run.node_tasks.items()}
    assert [by_task[t.id] for t in T.ready(run.session_id)] == ["early", "mid", "late"]


def test_node_ids_are_folded_once_so_two_spellings_are_one_node():
    """⚠️ Cross-platform, not cosmetic: Task 38 takes a workflow's name from its
    filename, and `Audit.yaml` is one file on Windows and two on Linux."""
    assert G.fold_id("  Build-Step ") == "build-step"
    v = G.validate(_def("dupes", [_node("Build"), _node("build")]))
    assert not v.ok
    assert [p["code"] for p in v.problems] == [G.P_DUPLICATE]
    # And a `needs:` written in the other case still resolves.
    ok = G.validate(_def("mixed", [_node("build"), _node("test", needs=("BUILD",))]))
    assert ok.ok, ok.summary()


def test_the_node_cap_refuses_rather_than_truncating(monkeypatch):
    """⚠️ A graph missing its tail is a graph whose `needs:` no longer resolve, so
    a silent trim turns one honest refusal into a fistful of unknown-dependency
    errors pointing at nodes the author did write."""
    monkeypatch.setattr(cfg, "WORKFLOW_MAX_NODES", 3)
    v = G.validate(_def("big", _chain("a", "b", "c", "d")))
    assert not v.ok
    codes = [p["code"] for p in v.problems]
    assert codes == [G.P_TOO_MANY]              # …and not four unknown-dependency ones
    assert v.problems[0]["count"] == 4 and v.problems[0]["limit"] == 3
    assert "AGENT2_WORKFLOW_MAX_NODES" in v.problems[0]["message"]


def test_an_old_schema_still_runs_and_a_newer_one_is_only_a_warning():
    """Task 38's bar, checked at the model it validates against."""
    old = G.validate(_def("v", [_node("a")], version=G.MIN_SCHEMA))
    assert old.ok and not [w for w in old.warnings if w["code"] == G.W_NEW_SCHEMA]

    ahead = G.validate(_def("v", [_node("a")], version=G.SCHEMA_VERSION + 7))
    assert ahead.ok, ahead.summary()            # a newer file is not a one-way door
    assert [w["code"] for w in ahead.warnings if w["code"] == G.W_NEW_SCHEMA] \
        == [G.W_NEW_SCHEMA]

    behind = G.validate(_def("v", [_node("a")], version=G.MIN_SCHEMA - 1))
    assert not behind.ok
    assert G.P_SCHEMA in [p["code"] for p in behind.problems]


def test_ok_is_decided_by_problems_alone():
    """`core/health.py`'s rule: every configuration that can legitimately occur and
    still run must be expressible without tripping the refusal."""
    v = G.validate(G.make_def({"name": "bare", "nodes": ["a", "b"]}))
    assert v.ok and v.levels
    codes = {w["code"] for w in v.warnings}
    assert G.W_NO_INSTRUCTION in codes          # cosmetic gaps, reported not refused
    assert not v.problems


def test_a_deep_graph_is_reported_never_raised(monkeypatch):
    """`_find_cycles` is an explicit stack because a `RecursionError` escaping a
    validator would break this module's one promise."""
    monkeypatch.setattr(cfg, "WORKFLOW_MAX_NODES", 2000)
    ids = [f"n{i:03d}" for i in range(500)]
    v = G.validate(_def("deep", _chain(*ids)))
    assert v.ok, v.summary()
    assert len(v.levels) == 500

    # The same depth, closed into a ring.
    nodes = _chain(*ids)
    nodes[0]["needs"] = [ids[-1]]
    ring = G.validate(_def("deepring", nodes))
    assert not ring.ok
    assert G.P_CYCLE in [p["code"] for p in ring.problems]
    assert ring.levels == ()


def test_a_node_literal_means_one_thing_however_it_is_written():
    """Coercion lives in `make_node`/`make_def`, so Task 38's loader and an inline
    caller cannot disagree about what a workflow literal means."""
    for key in ("nodes", "steps", "tasks", "graph"):
        d = G.make_def({"name": "shapes", key: [{"id": "a"},
                                                {"id": "b", "after": "a"}]})
        assert d.ids() == ("a", "b") and d.node("b").needs == ("a",)
    # A bare string list is the shorthand people reach for first.
    assert G.make_def({"name": "s", "nodes": ["a", "b"]}).ids() == ("a", "b")
    # `needs` as a string, and every alias for it.
    for alias in ("needs", "depends_on", "after", "requires"):
        d = G.make_def({"name": "n", "nodes": [{"id": "a"},
                                               {"id": "b", alias: "a, a"}]})
        assert d.node("b").needs == ("a",)      # deduped, never a second edge
    # Instruction aliases.
    for alias in ("instruction", "prompt", "description", "do"):
        d = G.make_def({"name": "i", "nodes": [{"id": "a", alias: "run tests"}]})
        assert d.node("a").instruction == "run tests"


def test_a_mapping_key_wins_over_an_inner_id():
    """Otherwise one node answers to two names and `needs:` resolves to whichever
    was read last."""
    d = G.make_def({"name": "m", "nodes": {"build": {"id": "other",
                                                     "instruction": "go"}}})
    assert d.ids() == ("build",)
    assert d.node("build").instruction == "go"


def test_unmapped_keys_are_kept_rather_than_dropped():
    """`skills.normalize`'s `extra`, for its reason: a file written by a newer build
    must survive a round trip through an older one intact enough to be reported."""
    d = G.make_def({"name": "x", "nodes": ["a"], "on_failure": "stop",
                    "max_workers": 3})
    assert d.extra == {"on_failure": "stop", "max_workers": 3}


@pytest.mark.parametrize("junk", [None, 0, "", [], "workflow", {"nodes": 7},
                                  {"name": None, "nodes": [None, 3, {}]}])
def test_validate_never_raises_on_junk(junk):
    v = G.validate(G.make_def(junk))
    assert isinstance(v, G.Validation) and not v.ok
    assert v.summary()                          # …and it always says something


def test_describe_reports_what_this_build_accepts():
    d = WF.describe()
    assert d["schema"] == G.SCHEMA_VERSION and d["min_schema"] == G.MIN_SCHEMA
    assert d["yaml"] is False                   # stdlib only until Task 38 says so
    assert G.P_CYCLE in d["problems"] and G.W_NEW_SCHEMA in d["warnings"]
    assert d["max_nodes"] == cfg.WORKFLOW_MAX_NODES
    assert isinstance(d.get("runs"), dict)


# ── The runner: rows that already existed ─────────────────────────────────────

def test_nothing_is_written_when_the_graph_does_not_validate():
    """⚠️ Every failure validation catches is durable once rows exist, and cleaning
    it up is a human deleting task rows by hand."""
    before = _counts()
    run = R.instantiate(_def("ring", [_node("a", needs=("b",)),
                                      _node("b", needs=("a",))]))
    assert not run.ok
    assert "circular" in run.reason
    assert run.run_id == "" and run.session_id == ""
    assert run.validation is not None and not run.validation.ok
    assert _counts() == before


def test_every_node_becomes_a_task_row_with_dependencies_as_task_ids():
    """The remap happens once, at creation. `tasks.ready()` ignores a dependency it
    cannot resolve, so an id that resolved to nothing would run out of order."""
    run = _run(_chain("build", "test", "ship"))
    rows = T.list_tasks(run.session_id)
    assert len(rows) == 3
    assert [r.seq for r in rows] == [0, 1, 2]          # topological order
    by_id = {r.id: r for r in rows}
    node_of = {tid: nid for nid, tid in run.node_tasks.items()}
    for row in rows:
        for dep in row.dependencies:
            assert dep in by_id, "a dependency must be a real task id"
        assert [node_of[d] for d in row.dependencies] == \
            list(_def("x", _chain("build", "test", "ship")).node(
                node_of[row.id]).needs)
    # And the run row knows how much work it opened with.
    assert X.workflow(run.run_id)["total_steps"] == 3


def test_the_node_identity_lives_on_the_task_row():
    """⚠️ Not in `exec_workflows.state`: that column degrades to `{}` past
    `MAX_STATE_CHARS`, and the node→task link is the one fact a resume cannot
    re-derive."""
    run = _run(_chain("build", "test"))
    for nid, tid in run.node_tasks.items():
        cp = T.get(tid).checkpoint
        assert cp[T.CP_NODE] == nid
        assert cp[T.CP_WORKFLOW] == run.run_id
    # `state` holds provenance and nothing that could outgrow the column.
    state = json.loads(X.workflow(run.run_id)["state"] or "{}")
    assert state.get("source") == "inline" and state.get("schema") == G.SCHEMA_VERSION


def test_provenance_survives_a_node_transition():
    """⚠️ `workflow_step()` REPLACES `exec_workflows.state`, so provenance written
    once at `workflow_started` would be gone by the first completed node — one read
    short of ever being useful. `instantiate()` declines to store the topological
    levels *specifically* to keep `source` and `schema` inside the column's cap, so
    losing them a call later would spend that decision for nothing."""
    run = _run(_chain("build", "test"))
    T.complete(run.node_tasks["build"])
    R.advance(run.run_id)
    after_step = json.loads(X.workflow(run.run_id)["state"] or "{}")
    assert after_step.get("source") == "inline"
    assert after_step.get("schema") == G.SCHEMA_VERSION
    assert after_step.get("done") == 1              # …alongside the progress

    T.complete(run.node_tasks["test"])
    R.advance(run.run_id)                           # settles the run
    after_settle = json.loads(X.workflow(run.run_id)["state"] or "{}")
    assert after_settle.get("source") == "inline"
    assert after_settle.get("schema") == G.SCHEMA_VERSION
    # And it is readable without parsing the column at a call site.
    st = R.state_for(run.run_id)
    assert (st.source, st.schema) == ("inline", G.SCHEMA_VERSION)
    assert st.to_payload()["source"] == "inline"


def test_a_run_gets_its_own_session_and_a_stray_todo_is_not_a_node():
    """⚠️ `ready()` is session-scoped, so sharing a chat's session would hand a chat
    todo to the workflow driver as a runnable node — and a workflow node to
    `update_todo` as something the model may tick off."""
    run = _run(_chain("build", "test"), chat_id="chat-shared")
    sess = T.get_session(run.session_id)
    assert sess["surface"] == R.SURFACE
    assert sess["chat_id"] == "chat-shared"

    # A hand-written todo lands in the same session; it is excluded because it
    # carries no node id, never because of what its title says.
    stray = T.create(run.session_id, "build")          # same title as a real node
    state = R.state_for(run.run_id)
    assert stray.id not in [n.task_id for n in state.nodes]
    assert [n.node for n in state.nodes] == ["build", "test"]
    assert R._node_of(stray) == ""


def test_progress_is_derived_from_the_rows_not_from_the_run_row():
    """⚠️ `done`/`total` are counted every read. The breadcrumb is written *from*
    this derivation, and the stored copy is the one nobody re-checks."""
    run = _run(_chain("build", "test"))
    db.exe("UPDATE exec_workflows SET step_index=99, step='nonsense' WHERE id=?",
           (run.run_id,))
    state = R.state_for(run.run_id)
    assert (state.done, state.total) == (0, 2)
    assert state.current().node == "build"
    assert state.to_payload()["done"] == 0

    T.complete(run.node_tasks["build"])
    R.advance(run.run_id)
    assert R.state_for(run.run_id).done == 1
    assert X.workflow(run.run_id)["step_index"] == 1     # the breadcrumb caught up
    assert X.workflow(run.run_id)["step"] == "test"


def test_state_for_derives_the_five_phases_and_says_what_blocks_what():
    run = _run([_node("build"), _node("test", needs=("build",)),
                _node("ship", needs=("test",))])
    assert _phases(R.state_for(run.run_id)) == [
        ("build", R.PHASE_READY), ("test", R.PHASE_BLOCKED),
        ("ship", R.PHASE_BLOCKED)]
    blocked = {n.node: n.blocked_by for n in R.state_for(run.run_id).nodes}
    assert blocked["test"] == ("build",)      # node ids, not task ids
    assert blocked["build"] == ()

    T.start(run.node_tasks["build"])
    state = R.state_for(run.run_id)
    assert dict(_phases(state))["build"] == R.PHASE_RUNNING
    assert state.current().node == "build"    # running outranks ready

    T.complete(run.node_tasks["build"])
    state = R.state_for(run.run_id)
    assert dict(_phases(state)) == {"build": R.PHASE_DONE, "test": R.PHASE_READY,
                                    "ship": R.PHASE_BLOCKED}


def test_a_completed_node_is_not_re_run_by_a_resume():
    """The user's standing rule, proved by attempting the wrong thing.

    ⚠️ The guarantee does not live in `resume()` — it lives in `tasks.set_status()`,
    which refuses to move a terminal row without `force=True`. So a driver that
    ignored this list entirely would still advance only the open nodes.
    """
    run = _run(_chain("build", "test"))
    T.complete(run.node_tasks["build"], result="ok")
    R.advance(run.run_id)

    assert [n.node for n in R.resume(run.run_id)] == ["test"]

    # The wrong thing, attempted:
    again = T.set_status(run.node_tasks["build"], T.TaskStatus.RUNNING)
    assert again.status == T.TaskStatus.COMPLETED
    assert T.get(run.node_tasks["build"]).attempt_count == 0
    assert dict(_phases(R.state_for(run.run_id)))["build"] == R.PHASE_DONE


def test_settle_reads_the_verdict_off_disk_not_from_its_caller():
    """⚠️ "Worker says done ⇒ Agent2 says done" is forbidden, so `ok` is decided by
    the node rows even when the caller hands in a state object."""
    run = _run([_node("build"), _node("docs")])
    T.complete(run.node_tasks["docs"])
    T.fail(run.node_tasks["build"], error="compiler exploded")
    R.advance(run.run_id)                       # a driver's normal call

    row = X.workflow(run.run_id)
    assert row["status"] == T.STEP_FAILED
    assert "build" in row["error"] and "compiler exploded" in row["error"]
    assert row["step_index"] == 2 and row["completed_at"]
    assert T.get_session(run.session_id)["status"] == T.SESSION_DONE

    # A caller insisting otherwise changes nothing: the rows are re-read.
    lie = R.RunState(run_id=run.run_id, exists=True, session_id=run.session_id)
    R.settle(run.run_id, state=lie)             # `finished` is False on a lie: no-op
    assert X.workflow(run.run_id)["status"] == T.STEP_FAILED


def test_settle_is_idempotent():
    run = _run([_node("only")])
    T.complete(run.node_tasks["only"])
    R.advance(run.run_id)
    first = X.workflow(run.run_id)
    R.settle(run.run_id)
    R.settle(run.run_id)
    second = X.workflow(run.run_id)
    assert first["status"] == second["status"] == T.STEP_COMPLETED
    assert first["error"] == second["error"] == ""
    assert R.state_for(run.run_id).finished


def test_a_failed_upstream_is_reported_because_ready_releases_on_settled():
    """⚠️ Reported, never acted on here. `tasks.ready()` treats *settled* as
    satisfied, so the node genuinely is runnable by the one declaration of
    readiness; withholding it here would be a second predicate. Whether to skip is
    Task 39's policy and Task 42's gate — this is the sentence that stops a turn
    building confidently on a broken input."""
    run = _run(_chain("build", "test", "ship"))
    T.fail(run.node_tasks["build"], error="compiler exploded")
    R.advance(run.run_id)

    state = R.state_for(run.run_id)
    assert _phases(state) == [("build", R.PHASE_FAILED), ("test", R.PHASE_READY),
                              ("ship", R.PHASE_BLOCKED)]
    by_node = {n.node: n for n in state.nodes}
    assert by_node["test"].upstream_failed == ("build",)
    assert by_node["ship"].upstream_failed == ()      # its own dep has not settled
    assert "⚠ Upstream" in WF.for_turn(chat_id="chat-wf")["text"]


def test_a_held_node_reads_blocked_and_a_resume_does_not_un_hold_it():
    """⚠️ The projection is a NARROWING — nine core states onto five phases — and
    PAUSED is the one the old ladder could not express at all.

    ⚠️ `tasks.ready()` returns a paused row, because PAUSED is an OPEN status: the
    hold gates *starting* the node, not listing it. So a projection that read it as
    READY would put the one node a human deliberately held at the top of "what runs
    next", and `resume()` filters on the phase, so the drift would travel from the
    payload into what a resumed run actually does. Both halves are pinned here — the
    held node is absent, and the sibling nobody held is not.
    """
    run = _run([_node("build"), _node("docs")])       # siblings, both runnable
    T.pause(run.node_tasks["build"])

    assert R._PHASE_FOR[M.PAUSED] == R.PHASE_BLOCKED  # declared, not fallen back to
    state = R.state_for(run.run_id)
    assert _phases(state) == [("build", R.PHASE_BLOCKED), ("docs", R.PHASE_READY)]
    assert state.current().node == "docs"             # never the held node
    assert [n.node for n in R.resume(run.run_id)] == ["docs"]
    assert not state.finished                        # held is not done

    # ⚠️ Re-derived on every read, never latched: releasing the hold returns it to
    # the list without anything having stored a phase.
    T.set_status(run.node_tasks["build"], T.TaskStatus.PENDING)
    assert [n.node for n in R.resume(run.run_id)] == ["build", "docs"]


def test_state_for_an_unknown_run_is_empty_and_quiet():
    for rid in ("", "no-such-run", None):
        state = R.state_for(rid)
        assert not state.exists and state.nodes == [] and state.total == 0
        assert state.finished is False              # not "everything is done"
        assert state.to_payload()["exists"] is False
    assert R.resume("no-such-run") == []
    assert R.advance("no-such-run").exists is False


def test_a_disabled_feature_writes_nothing(monkeypatch):
    monkeypatch.setattr(cfg, "WORKFLOW_ENABLED", False)
    before = _counts()
    run = R.instantiate(_def("demo", [_node("a")]))
    assert not run.ok and "AGENT2_WORKFLOWS" in run.reason
    assert _counts() == before
    assert WF.for_turn()["active"] is False


def test_the_ledger_being_off_refuses_before_it_writes_a_session(monkeypatch):
    """⚠️ Refused BEFORE the session row. `workflow_started()` returns `""` when the
    ledger is off, and a run with no id is a run no surface can read and crash
    recovery cannot find — so orphan task rows that look like an abandoned plan are
    the worse of the two answers."""
    monkeypatch.setattr(cfg, "EXEC_PERSIST", False)
    before = _counts()
    run = R.instantiate(_def("demo", [_node("a")]))
    assert not run.ok and "AGENT2_EXEC_PERSIST" in run.reason
    assert _counts() == before


def test_an_unrecordable_run_is_rolled_back_rather_than_left_orphaned(monkeypatch):
    """The same failure arriving the other way — the ledger accepted the call and
    handed back no id. The session must not survive as a plan nobody can run."""
    monkeypatch.setattr(R._exec, "workflow_started", lambda *a, **k: "")
    tasks_before = db.qone("SELECT COUNT(*) AS c FROM agent_tasks")["c"]
    run = R.instantiate(_def("demo", [_node("a")]))
    assert not run.ok and "could not be recorded" in run.reason
    assert db.qone("SELECT COUNT(*) AS c FROM agent_tasks")["c"] == tasks_before
    abandoned = db.qall("SELECT status FROM task_sessions WHERE surface=?",
                        (R.SURFACE,))
    assert abandoned and abandoned[-1]["status"] == T.SESSION_ABANDONED


def test_a_denied_capability_refuses_the_run(monkeypatch):
    """A run drives the agent, so it is rated `chat` — the same rating
    `/api/workflows` carries. `AGENT2_DENY_CAPS=chat` must stop it here too."""
    from agent2.core import permissions as P
    monkeypatch.setattr(P, "process_allows", lambda cap: cap != P.CAP_CHAT)
    before = _counts()
    run = R.instantiate(_def("demo", [_node("a")]))
    assert not run.ok and "not permitted" in run.reason
    assert _counts() == before


def test_runs_and_live_are_scoped_and_ignore_a_settled_run():
    run = _run([_node("only")])
    assert R.live() is not None and R.live().run_id == run.run_id
    assert run.run_id in {r["id"] for r in R.runs(active=True)}

    T.complete(run.node_tasks["only"])
    R.advance(run.run_id)                        # auto-settles a finished run
    assert R.live() is None
    assert run.run_id in {r["id"] for r in R.runs()}          # still on record
    assert run.run_id not in {r["id"] for r in R.runs(active=True)}
    assert R.stats()["recent"] >= 1


def test_stats_are_counters_and_never_a_node_title():
    run = _run([_node("secret-node", instruction=SECRET_NOTE)])
    blob = json.dumps(R.stats()) + json.dumps(WF.stats())
    assert SECRET_NOTE not in blob and "secret-node" not in blob
    assert R.stats()["active"] >= 1
    assert run.run_id not in blob


# ── The turn path: what a node's worker is actually told ───────────────────────

def test_a_turn_with_no_workflow_pays_one_indexed_read(monkeypatch):
    """⚠️ The idle cost is the whole reason `live()` exists as one query. This runs
    on every turn of every project, most of which will never use a workflow."""
    with _queries(monkeypatch) as calls:
        view = WF.for_turn(chat_id="chat-quiet")
    assert view == {"active": False, "text": "", "truncated": False,
                    "run": {}, "omitted": 0}
    assert calls == {"qall": 1, "qone": 0}


def test_a_running_workflow_costs_the_same_whatever_its_size(monkeypatch):
    """⚠️ FLAT, not merely small. `state_for()` reads the run row and the session's
    tasks — two reads and a `ready()` call over the pool already in hand. A per-node
    read would be invisible on the three-node graph a developer tests with and
    would cost 64 round trips per turn on the graph a user writes."""
    _run(_chain("build", "test", "ship"), chat_id="chat-small")
    with _queries(monkeypatch) as small:
        assert WF.for_turn(chat_id="chat-small")["active"] is True

    db.exe("DELETE FROM exec_workflows")
    _run([_node(f"n{i:02d}") for i in range(24)], name="big", chat_id="chat-big")
    with _queries(monkeypatch) as big:
        view = WF.for_turn(chat_id="chat-big")
    assert view["active"] is True and view["run"]["total"] == 24
    assert small == big == {"qall": 2, "qone": 1}


def test_the_block_inlines_only_the_current_node_and_names_the_rest(monkeypatch):
    """⚠️ THE CONTEXT BAR. A turn is doing one node, so one node's prose is what it
    gets; inlining the whole plan every turn is how a worker ends up carrying the
    entire parent conversation."""
    monkeypatch.setattr(WF, "NAMED_NODES", 2)
    nodes = _chain("build", "test", "docs", "ship", "tag")
    nodes[0]["instruction"] = SECRET_NOTE
    run = _run(nodes, chat_id="chat-wf")

    view = WF.for_turn(chat_id="chat-wf")
    text = view["text"]
    assert view["active"] and view["run"]["run_id"] == run.run_id
    assert "`build`" in text and SECRET_NOTE in text          # the current node
    for other in ("test", "docs", "ship", "tag"):             # …and only that one
        assert f"do it ({other})" not in text
    assert "`test`" in text and "`docs`" in text              # named
    assert view["omitted"] == 2 and "and 2 more" in text
    assert "`tag`" not in text
    assert "Do the current node only." in text
    assert view["truncated"] is False


def test_the_state_cap_is_reported_never_silent(monkeypatch):
    """"A cap that engages is reported" — the same rule `/init`'s `truncated_by`
    and `skills.truncated_by` follow."""
    monkeypatch.setattr(cfg, "WORKFLOW_STATE_CHARS", 90)
    _run(_chain("build", "test", "docs"), chat_id="chat-wf")
    view = WF.for_turn(chat_id="chat-wf")
    assert view["truncated"] is True
    assert len(view["text"]) <= 90 and view["text"].endswith("…")


def test_the_broker_renders_the_block_and_derives_no_fact_of_its_own():
    """⚠️ The `_collect_skills` split, for its reason: the broker may hold no
    workflow fact it could drift on."""
    run = _run(_chain("build", "test"), chat_id="chat-broker")
    bundle = B.assemble(chat_id="chat-broker", message="carry on")
    items = bundle.get(B.SOURCE_WORKFLOW)
    assert len(items) == 1
    item = items[0]

    view = WF.for_turn(chat_id="chat-broker")
    assert view["text"] in item.text                  # rendered, not re-derived
    assert item.text.startswith("\n\n## WORKFLOW IN PROGRESS")
    assert item.relevance == 1.0                      # pre-stated, never word-overlap
    assert item.meta["run_id"] == run.run_id
    assert item.meta["name"] == "demo"
    assert item.meta["current"] == "build"
    assert (item.meta["done"], item.meta["total"]) == (0, 2)
    assert "WORKFLOW IN PROGRESS" in bundle.prompt_tail()
    # ⚠️ Scored, not pinned: `ALWAYS` stays the two standing instruction blocks, so
    # a tight budget may still drop this ahead of the user's own rules.
    assert B.SOURCE_WORKFLOW not in B.ALWAYS


def test_no_node_instruction_ever_reaches_a_payload():
    """A node instruction is prose a human wrote in their own checkout, and
    `GET /api/workflows` is a browser surface — `Skill.to_payload(body=False)`'s
    rule, for its reason."""
    run = _run([_node("build", instruction=SECRET_NOTE)], chat_id="chat-priv")
    blobs = [
        json.dumps(R.state_for(run.run_id).to_payload()),
        json.dumps(run.to_payload()),
        json.dumps(WF.for_turn(chat_id="chat-priv")["run"]),
        json.dumps(B.assemble(chat_id="chat-priv").to_payload()),
    ]
    for blob in blobs:
        assert SECRET_NOTE not in blob
    # It is carried in memory so `for_turn()` costs no extra query…
    assert R.state_for(run.run_id).nodes[0].instruction == SECRET_NOTE
    # …and it reaches the prompt, which is the one place it belongs.
    assert SECRET_NOTE in WF.for_turn(chat_id="chat-priv")["text"]


def test_a_broken_workflow_source_costs_the_block_and_not_the_turn(monkeypatch):
    """Two guards, one per layer. `for_turn()` absorbs a broken reader; the broker's
    per-collector guard absorbs a broken `for_turn()`."""
    def boom(*a, **k):
        raise RuntimeError("ledger on fire")

    monkeypatch.setattr(R, "live", boom)
    assert WF.for_turn(chat_id="c1") == {"active": False, "text": "",
                                         "truncated": False, "run": {}, "omitted": 0}

    monkeypatch.setattr(WF, "for_turn", boom)
    bundle = B.assemble(chat_id="c1", message="hello")
    assert bundle.get(B.SOURCE_WORKFLOW) == []
    assert B.SOURCE_WORKFLOW in bundle.errors
    assert "ledger on fire" in bundle.errors[B.SOURCE_WORKFLOW]
    # The one thing the broker exists to prevent: one bad source costing the rest.
    assert bundle.get(B.SOURCE_MEMORY) is not None
    assert isinstance(bundle.prompt_tail(), str)
    assert B.SOURCE_WORKFLOW in bundle.to_payload()["errors"]


# ══════════════════════════════════════════════════════════════════════════════
# Phase D3 — Workflow is a CONSUMER of the one generalized DAG core
# ══════════════════════════════════════════════════════════════════════════════
#
# Everything above was written when `core/workflow/` owned its own graph. D3 moved
# the graph, the validator, the store and the scheduler into `core/dag/` and left
# this package as the *vocabulary*: `WorkflowDef` **is** `dag.Graph`, `validate()`
# injects two words into the core's validator, `RunState` projects a `GraphState`.
#
# The four bars pinned here are the ones that stay true only while nothing was
# copied. Each of them fails **silently** if it regresses — a second validator that
# agrees today, a stored wave that goes stale tomorrow, a recovery that quietly
# undoes a human's `/pause`, a verifier that believes a claim — so none of them
# would be caught by the suite above, which asks only whether a workflow runs.


# ── D3.20 · ONE validator, and the consumer supplies only nouns ───────────────

def test_the_workflow_wrapper_holds_no_second_validator_and_no_second_cycle_finder():
    """⚠️ D3.20 — `graph.py` is a *vocabulary*, not an implementation.

    Pinned over the AST rather than the text on purpose: this file's docstring,
    `graph.py`'s and `dag/validate.py`'s all discuss cycle finding in prose, so a
    substring search for `cycle` or `levels` hits three paragraphs and zero
    implementations — the trap `test_dag.py` documents as *"a docstring may name a
    consumer's knob"*.

    What is asserted instead is the shape: four module-level functions, no classes,
    and every body a single `return`. The breaks it goes red for: re-implement
    `find_cycles`/`levels_for` here and `defined` gains a name; inline the ceiling or
    the unknown-dependency check into `validate()` and that body stops being one
    statement; wrap the core's function instead of re-exporting it and the identity
    assertions fail.
    """
    import ast
    from importlib import import_module
    from pathlib import Path

    # ⚠️ `from agent2.core.dag import validate` yields the re-exported **function**,
    # not this submodule — `dag/__init__.py` binds that name and `import … as` prefers
    # the attribute. Every test below reaches the module the unambiguous way.
    DV = import_module("agent2.core.dag.validate")

    tree = ast.parse(Path(G.__file__).read_text(encoding="utf-8"))
    defined = {n.name for n in tree.body
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert defined == {"make_def", "validate", "validate_mutation", "describe"}
    assert [n.name for n in tree.body if isinstance(n, ast.ClassDef)] == []

    for fn in tree.body:
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = [s for s in fn.body
                if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
        assert len(body) == 1 and isinstance(body[0], ast.Return), \
            f"{fn.name} does more than hand the core its nouns"

    # The four graph algorithms arrive from the core, by name…
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "agent2.core.dag.validate":
            imported.update(a.name for a in node.names)
    assert {"find_cycles", "levels_for", "validate", "validate_mutation"} <= imported

    # …and what this module exports IS the core's object, not a look-alike beside it.
    assert G.find_cycles is DV.find_cycles
    assert G.levels_for is DV.levels_for
    assert G.WorkflowDef is M.Graph


def test_a_cyclic_workflow_and_a_cyclic_bare_graph_are_refused_identically():
    """⚠️ D3.20 — one set of problem codes, whoever asks.

    The cycle refusal is already pinned for workflows further up. What is new is the
    other end of the same call: the **bare** engine, handed the same ring with no
    domain vocabulary at all, produces the same codes, the same messages and the same
    ring. A consumer carrying a validator of its own could agree today and drift on
    the first code the core adds; this cannot.
    """
    from importlib import import_module

    DV = import_module("agent2.core.dag.validate")

    ring = [_node("a", needs=("b",)), _node("b", needs=("a",))]
    wf = G.validate(_def("ring", ring))
    bare = DV.validate(M.make_graph({"name": "ring", "nodes": ring}))

    assert wf.ok is False and bare.ok is False
    assert [p["code"] for p in wf.problems] == [p["code"] for p in bare.problems] \
        == [M.P_CYCLE]
    assert [p["message"] for p in wf.problems] == [p["message"] for p in bare.problems]
    assert wf.cycles == bare.cycles and set(wf.cycles[0]) == {"a", "b"}
    assert wf.levels == bare.levels == ()


def test_the_refusal_names_the_consumers_knob_because_the_words_are_injected(monkeypatch):
    """⚠️ D3.20 — the sentence is the CORE's, with the consumer's noun in it.

    `test_the_node_cap_refuses_rather_than_truncating` pins the workflow half of the
    ceiling message. This is the half that makes it an *injection* rather than a
    coincidence: the very same function object, asked with no `knob`/`label`, names
    the engine's own variable and calls the thing a graph. An `if workflow:` inside
    the validator would satisfy one half and fail the other.
    """
    from importlib import import_module

    DV = import_module("agent2.core.dag.validate")

    monkeypatch.setattr(cfg, "WORKFLOW_MAX_NODES", 1)
    pair = [_node("a"), _node("b")]
    wf = G.validate(_def("big", pair))
    bare = DV.validate(M.make_graph({"name": "big", "nodes": pair}), max_nodes=1)

    assert [p["code"] for p in wf.problems] == [p["code"] for p in bare.problems] \
        == [M.P_TOO_MANY]
    assert "AGENT2_WORKFLOW_MAX_NODES" in wf.problems[0]["message"]
    assert "AGENT2_DAG_MAX_NODES" in bare.problems[0]["message"]
    assert "AGENT2_WORKFLOW_MAX_NODES" not in bare.problems[0]["message"]
    # Same arithmetic, so only the noun differs — the message is one f-string.
    assert wf.problems[0]["message"].replace(G.KNOB, "AGENT2_DAG_MAX_NODES") \
        == bare.problems[0]["message"]

    # The `label` travels the same way, into a completely different sentence.
    typo = [_node("build", needs=("tets",))]
    named = G.validate(_def("typo", typo))
    plain = DV.validate(M.make_graph({"name": "typo", "nodes": typo}))
    assert "not a node of this workflow" in named.problems[0]["message"]
    assert "not a node of this graph" in plain.problems[0]["message"]


def test_the_core_never_spells_a_consumers_knob_where_it_could_be_evaluated():
    """⚠️ D3.20's structural half — *"the DAG engine contains no feature name"*.

    Asserted over evaluated string literals **only**. `AGENT2_WORKFLOW_MAX_NODES`
    genuinely appears three times inside `core/dag/` — in the docstrings that explain
    the injection — so the raw-text form of this test passes today and would keep
    passing after somebody hard-coded the knob into a message. Docstrings are
    therefore excluded and everything else is not.

    ⚠️ The bare word `"workflow"` is deliberately *not* asserted against: it is an
    evaluated literal in `model.GRAPH_KEYS`, where it is a **document key alias**
    (`workflow: build` naming a graph) rather than a branch — and `test_dag.py`
    already pins the no-branch-on-a-feature-name half. The two facts pinned here are
    the ones injection depends on: no knob in code, and the core's own defaults are
    the engine's.
    """
    import ast
    import inspect
    from importlib import import_module
    from pathlib import Path

    from agent2.core import dag as D

    DV = import_module("agent2.core.dag.validate")
    root = Path(D.__file__).parent
    for name in ("__init__.py", "model.py", "validate.py", "store.py", "schedule.py"):
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        docs = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)) and node.body:
                first = node.body[0]
                if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                    docs.add(id(first.value))
        live = [n.value for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
                and id(n) not in docs]
        assert not [s for s in live if "AGENT2_WORKFLOW" in s], \
            f"{name} evaluates a workflow knob"

    # An un-injected call cannot accidentally speak a consumer's language.
    for fn in (DV.validate, DV.validate_mutation):
        params = inspect.signature(fn).parameters
        assert params["knob"].default == "AGENT2_DAG_MAX_NODES"
        assert params["label"].default == "graph"
        assert params["label"].default != G.LABEL and params["knob"].default != G.KNOB


# ── D3.21 · parallelism is DERIVED from `needs:`, never declared ──────────────

def test_parallelism_is_derived_from_needs_and_is_never_stored():
    """⚠️ D3.21 — an author writes `needs:` and the width of their plan falls out.

    Nothing declares a wave and nothing persists one, which is why the answer cannot
    go stale: settling a node does not reshape the topology, and the breadcrumb column
    holds progress and provenance and deliberately not the levels.
    """
    flat = _run([_node("a"), _node("b"), _node("c")], name="flat", chat_id="chat-flat")
    st = R.state_for(flat.run_id)
    assert st.waves == (("a", "b", "c"),)          # one wave: no edge between them
    assert st.width == 3
    assert [n.node for n in st.dispatchable] == ["a", "b", "c"]

    stored = json.loads(
        db.qone("SELECT state FROM exec_workflows WHERE id=?",
                (flat.run_id,))["state"] or "{}")
    assert not {"waves", "width", "levels"} & set(stored)

    # A topology, not a progress report.
    T.complete(flat.node_tasks["a"])
    settled = R.state_for(flat.run_id)
    assert settled.waves == (("a", "b", "c"),) and settled.width == 3
    assert [n.node for n in settled.dispatchable] == ["b", "c"]

    chain = _run(_chain("x", "y", "z"), name="chain", chat_id="chat-chain")
    deep = R.state_for(chain.run_id)
    assert deep.waves == (("x",), ("y",), ("z",))
    assert deep.width == 1
    assert [n.node for n in deep.dispatchable] == ["x"]
    # The two runs share one engine and one database and still answer separately.
    assert R.state_for(flat.run_id).width == 3


def test_a_wave_is_what_the_graph_permits_and_not_what_the_scheduler_starts():
    """⚠️ D3.21's other half, and the spec's rule — *never blindly run every READY
    node*. `width` is what the plan COULD offer; `plan()` is what may start now, and
    every node it declines carries a code. Deriving the second from the first would be
    a workflow-shaped scheduler, which is exactly what D2 exists to prevent.
    """
    run = _run([_node("a"), _node("b"), _node("c")], name="wide", chat_id="chat-wide")
    st = R.state_for(run.run_id)
    assert st.width == 3

    step = st.plan(workers=1)
    assert len(step.slots) == 1
    assert len(step.held) == 2
    # `taken | held == offered`: a planner may not drop a node without saying so.
    assert {s.node for s in step.slots} | {h["node"] for h in step.held} \
        == {"a", "b", "c"}
    assert all(h.get("code") for h in step.held)
    # Widening the allowance changes the plan and not the graph.
    assert len(st.plan(workers=3).slots) == 3
    assert st.plan(workers=3).held == []
    assert st.width == 3


# ── D3.24 · progress is recounted from the rows on every read ────────────────

def test_progress_is_recounted_from_the_rows_while_provenance_is_read_from_the_column():
    """⚠️ D3.24 — the breadcrumb may be wrong; the rows may not.

    Both halves are in one test on purpose. `source`/`schema` **are** read out of
    `exec_workflows.state`, so a forged column proves the column is genuinely
    consulted — which is what stops "progress is derived" from being an assertion
    about a field nobody reads. The forged `done: 99` sits in the same dict as the
    honoured `source: forged`.
    """
    run = _run(_chain("build", "test", "ship"))
    T.complete(run.node_tasks["build"])
    R.advance(run.run_id)

    db.exe("UPDATE exec_workflows SET state=?, step_index=?, total_steps=? WHERE id=?",
           (json.dumps({"done": 99, "total": 99, "failed": 42, "finished": True,
                        "source": "forged", "schema": 7}), 99, 99, run.run_id))

    st = R.state_for(run.run_id)
    assert (st.done, st.total, len(st.failed)) == (1, 3, 0)   # recounted from the rows
    assert st.finished is False
    assert (st.source, st.schema) == ("forged", 7)            # …and the column IS read
    cur = st.current()
    assert cur is not None and cur.node == "test"

    # A row moved through `tasks` — never SQL — is visible to the very next read.
    T.fail(run.node_tasks["test"], error="linker exploded")
    fresh = R.state_for(run.run_id)
    assert (fresh.done, len(fresh.failed)) == (2, 1)
    assert dict(_phases(fresh))["test"] == R.PHASE_FAILED
    assert fresh.finished is False

    T.complete(run.node_tasks["ship"])
    end = R.state_for(run.run_id)
    assert (end.done, end.total, end.finished) == (3, 3, True)


def test_the_payload_gained_the_d3_keys_and_still_carries_no_prose():
    """⚠️ D3.24's reporting half — additive keys, ids and counters only.

    Every existing reader uses `.get()` with a default, so a new key is ignored by
    anything that has not learned it. What may not happen is a node instruction
    arriving in one, and what must keep working is `json.dumps` — `held` is a list of
    plain dicts because `GET /api/workflows` returns it.
    """
    run = _run(_chain("build", "test", "ship"), chat_id="chat-pay")
    T.complete(run.node_tasks["build"])
    p = R.state_for(run.run_id).to_payload()

    assert {"width", "waves", "next", "held", "interrupted"} <= set(p)
    assert "state" in p["nodes"][0]               # the node's nine-word core state
    assert p["waves"] == [["build"], ["test"], ["ship"]] and p["width"] == 1
    assert p["next"] == ["test"] and p["held"] == [] and p["interrupted"] == []
    assert (p["done"], p["total"], p["current"], p["finished"]) == (1, 3, "test", False)

    node = p["nodes"][0]
    assert node["state"] in M.STATES              # the core's nine words…
    assert node["phase"] == R.PHASE_DONE          # …carried BESIDE the five, not instead
    assert node["state"] != node["phase"]
    assert "instruction" not in node
    assert "do it (build)" not in json.dumps(p)

    # Survives a route: round-tripped, not merely truthy.
    assert json.loads(json.dumps(p))["waves"] == [["build"], ["test"], ["ship"]]


# ── D3.25 · shared recovery — a crash park is not a hold somebody chose ───────

def test_recover_frees_a_crash_park_and_never_a_hold_a_human_chose():
    """⚠️ D3.25 — the load-bearing half of this test is the NEGATIVE one.

    Both nodes end up PAUSED, so nothing here can be told apart by status. The only
    difference is the durable stamp `tasks.interrupt()` writes and `tasks.pause()`
    does not, which is exactly the predicate `dag.store.release_interrupted()` keys
    on. `/pause` is a human decision and recovering *execution* may never quietly
    undo it.

    The two breaks this goes red for, named:

    * drop the `and n.interrupted` half of `GraphState.interrupted` (or point
      `recover()` at a plain unpause of the run) — `held` comes back PENDING and a
      hold has been silently undone;
    * drop the `CP_STOPPED` write in `tasks.interrupt()` — `freed` is empty and a
      crash-parked node never restarts, which is the failure automatic recovery
      exists to prevent.
    """
    run = _run([_node("crashed"), _node("held"), _node("finished")])
    T.complete(run.node_tasks["finished"])
    T.start(run.node_tasks["crashed"])
    T.interrupt(run.session_id)          # a dead process: only RUNNING rows are parked
    T.pause(run.node_tasks["held"])      # a human, afterwards: no checkpoint at all

    cps = {nid: T.get(tid).checkpoint for nid, tid in run.node_tasks.items()}
    assert T.CP_STOPPED in cps["crashed"] and T.CP_STOPPED not in cps["held"]
    assert T.get(run.node_tasks["crashed"]).status == T.TaskStatus.PAUSED
    assert T.get(run.node_tasks["held"]).status == T.TaskStatus.PAUSED

    before = R.state_for(run.run_id)
    assert [n.node for n in before.graph_state.paused] == ["crashed", "held"]
    assert [n.node for n in before.interrupted] == ["crashed"]

    freed = R.recover(run.run_id)
    assert [n.node for n in freed] == ["crashed"]

    after = {n.node: n for n in R.state_for(run.run_id).nodes}
    assert after["crashed"].state == M.READY          # runnable again…
    assert after["held"].state == M.PAUSED            # ⚠️ …and this one untouched
    assert after["finished"].state == M.COMPLETED     # never offered back (rule 20)
    assert T.get(run.node_tasks["held"]).status == T.TaskStatus.PAUSED

    assert R.recover(run.run_id) == []                # nothing is parked twice
    assert R.recover("no-such-run") == [] and R.recover("") == []


def test_asking_whether_a_dead_process_left_anything_costs_no_extra_query(monkeypatch):
    """⚠️ `interrupted` is reported off rows the state already holds, so a read command
    can say "nothing was left behind" for free and only *then* spend a `load()`.

    The break: re-`load()` inside `RunState.interrupted` (or call `recover()` to find
    out whether there is anything to recover) and the second block counts more than
    the first — a query per `/workflow state` in the ordinary, nothing-parked case.
    """
    run = _run([_node("a"), _node("b")])
    T.start(run.node_tasks["a"])
    T.interrupt(run.session_id)

    with _queries(monkeypatch) as load_only:
        R.state_for(run.run_id)
    with _queries(monkeypatch) as with_key:
        parked = R.state_for(run.run_id).to_payload()["interrupted"]

    assert parked == ["a"]
    assert with_key == load_only == {"qall": 1, "qone": 1}


# ── D3.25 · shared verifier — a claim is never its own confirmation ───────────

def test_verify_reports_against_the_ledger_and_a_bare_claim_is_never_confirmation():
    """⚠️ D3.25 — *"worker says done ⇒ Agent2 says done"* is forbidden at the run
    level too. A node that measured something is `confirmed`; a node that only reasoned
    is `unconfirmed` — reported, never promoted — and the report is keyed to the RUN,
    not to the session, so a surface holding a run id can ask.
    """
    from agent2.core import verify as V

    run = _run([_node("measured"), _node("reasoned")], chat_id="chat-ver")
    tid = run.node_tasks["measured"]
    T.start(tid)
    call = X.tool_started("read_file", {"path": "agent2/config.py"},
                          session_id=run.session_id, task_id=tid, surface=R.SURFACE)
    X.tool_finished(call, ok=True)
    T.complete(tid)
    T.complete(run.node_tasks["reasoned"])

    report = R.verify(run.run_id)
    assert isinstance(report, V.Report)
    assert report.ref == run.run_id and report.ref != run.session_id
    verdicts = {f.ref: f.verdict for f in report.findings}
    assert verdicts == {tid: V.V_CONFIRMED,
                        run.node_tasks["reasoned"]: V.V_UNCONFIRMED}
    assert report.ok and report.complete and report.verified
    assert V.V_CONFIRMED in V.TRUSTED and V.V_UNCONFIRMED not in V.TRUSTED


def test_a_node_claiming_done_over_a_failed_ledger_row_does_not_verify():
    """⚠️ The claim and the evidence disagree, so the verdict follows the evidence.
    `complete` is still true — the row settled — which is why the three booleans are
    three questions and not one."""
    from agent2.core import verify as V

    run = _run([_node("lies")], name="lies", chat_id="chat-lies")
    tid = run.node_tasks["lies"]
    T.start(tid)
    call = X.tool_started("read_file", {"path": "agent2/config.py"},
                          session_id=run.session_id, task_id=tid, surface=R.SURFACE)
    X.tool_finished(call, ok=False, error="disk on fire")
    T.complete(tid)                                   # the claim: done

    report = R.verify(run.run_id)
    assert [f.verdict for f in report.findings] == [V.V_CONTRADICTED]
    assert report.complete is True                    # settled…
    assert report.ok is False and report.verified is False        # …and not verified
    assert any("disk on fire" in line for line in report.problems)
    assert report.counts[V.V_CONFIRMED] == 0


def test_verify_is_total_and_an_empty_report_never_reads_as_verified():
    """⚠️ Unknown run ids are a read path, so they **return** rather than raise — and
    an empty report is not a clean bill of health: `complete` is False on no findings
    by construction, so nothing may print *Complete* for a run that does not exist."""
    from agent2.core import verify as V

    for rid in ("no-such-run", ""):
        rep = R.verify(rid)
        assert isinstance(rep, V.Report)
        assert rep.findings == ()
        assert rep.ok is True                # nothing contradicted anything…
        assert rep.complete is False         # …and nothing settled either
        assert rep.verified is False
    assert R.verify("no-such-run").ref == "no-such-run"


def test_verify_costs_the_same_at_any_size_and_a_handed_in_state_costs_less(monkeypatch):
    """⚠️ Flat in node count — the same guarantee the turn path carries, because the
    ledgers are read once and the task objects are passed down.

    Two breaks, named:

    * hand `verify_tasks` the bare ids instead of the objects (drop the
      `found.get(i, i)` resolution) and `verify_task` spends one `tasks.get()` per
      node — the 24-node block then counts 21 more reads than the 3-node one;
    * drop the `state.run_id == run_id` guard on the `state=` fast path and a state
      belonging to another run is believed, so the last block verifies the wrong
      run's nodes while still costing less.
    """
    small = _run(_chain("a", "b", "c"), name="small", chat_id="chat-c3")
    for nid in ("a", "b", "c"):
        T.complete(small.node_tasks[nid])
    big = _run([_node(f"n{i:02d}") for i in range(24)], name="big24", chat_id="chat-c24")
    for nid in big.node_tasks:
        T.complete(big.node_tasks[nid])

    with _queries(monkeypatch) as fresh:
        first = R.verify(small.run_id)
    held = R.state_for(small.run_id)
    with _queries(monkeypatch) as handed:
        second = R.verify(small.run_id, state=held)
    with _queries(monkeypatch) as wide:
        R.verify(big.run_id)

    assert fresh == {"qall": 4, "qone": 1}
    assert wide == fresh                     # 24 nodes, a 3-node run's queries
    assert sum(handed.values()) < sum(fresh.values())     # the state was not re-read
    assert [f.verdict for f in first.findings] == [f.verdict for f in second.findings]
    assert [f.ref for f in first.findings] == [f.ref for f in second.findings]

    # A state that names another run is ignored rather than trusted.
    mixed = R.verify(small.run_id, state=R.state_for(big.run_id))
    assert {f.ref for f in mixed.findings} == set(small.node_tasks.values())


# ══════════════════════════════════════════════════════════════════════════════
# D3 · An interrupted run is one a human is actually offered
# ══════════════════════════════════════════════════════════════════════════════
def test_an_interrupted_workflow_reaches_the_recovery_offer_a_human_reads():
    """The end-to-end half of D3.25, through the real `instantiate()`.

    ⚠️ Recovery for workflows is not "the rows survive" — Task 37 already had that.
    The bar is that a killed run is **put in front of somebody**, and that offer is
    `recovery.candidates(cwd)`, which reads `tasks.unfinished_sessions(cwd)`. That
    query filters on `task_sessions.cwd`, so a run opened without a project is
    absent from `/recovery` and from `GET /api/recovery`'s `candidates` half while
    `/workflow state` still reports it perfectly — two readers, one blind, no error.
    That shipped: neither `agent2cli._workflow_run` nor `routes.api_run_workflow`
    passed `cwd=`, and `dag.store.create()` now fills the silence.

    Turns red when: the default is removed from `create()` — `offered` goes empty
    while every other assertion here still holds.
    """
    from agent2.core import recovery as REC
    from agent2.core import workspace as WS

    here = str(WS.root())
    run = _run(_chain("build", "test", "ship"), name="rescue", chat_id="chat-rescue")
    T.complete(run.node_tasks["build"])
    R.advance(run.run_id)

    offered = [c for c in REC.candidates(cwd=here, limit=20)
               if c.session_id == run.session_id]
    assert len(offered) == 1, "an interrupted workflow must reach the recovery offer"
    cand = offered[0]
    assert cand.goal == "workflow: rescue"
    assert (cand.total, cand.completed, cand.open) == (3, 1, 2)
    assert cand.cwd == T._project_key(here)

    # The plan a human is shown never re-runs the node that finished (rule 20).
    plan = REC.plan(run.session_id)
    assert [t.title for t in plan.done] == ["Build"]
    resumed = plan.resume.title if plan.resume is not None else ""
    waiting = [t.title for t in plan.waiting]
    assert resumed != "Build" and "Build" not in waiting
    assert sorted([*waiting, resumed] if resumed else waiting) == ["Ship", "Test"]

    # And the two surfaces agree about the same run: `/workflow state` sees it too.
    st = R.state_for(run.run_id)
    assert (st.done, st.total) == (1, 3)
    assert st.session_id == run.session_id


def test_both_surfaces_hand_the_project_down_the_same_way():
    """The CLI and the web route reach `instantiate()` with no `cwd` — on purpose.

    ⚠️ A structural pin, not a style rule: the project is defaulted **in the
    engine** (`budget.apply()`'s rule), so a call site that spells its own
    `cwd=workspace.root()` would be a second declaration of which project a run
    belongs to — and D4's planner and D5's UltraCode are two more callers. This
    asserts the default is what both surfaces actually depend on, so removing it
    cannot pass unnoticed on either one.
    """
    import ast as _ast
    from pathlib import Path as _P

    import agent2cli as _cli
    from agent2.server import routes as _routes

    seen = {}
    for label, mod in (("cli", _cli), ("web", _routes)):
        tree = _ast.parse(_P(mod.__file__).read_text(encoding="utf-8"))
        for node in _ast.walk(tree):
            if not isinstance(node, _ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, _ast.Attribute) else getattr(fn, "id", "")
            if name != "instantiate":
                continue
            seen.setdefault(label, []).append({k.arg for k in node.keywords})

    assert sorted(seen) == ["cli", "web"], f"call sites moved: {sorted(seen)}"
    for label, calls in seen.items():
        assert len(calls) == 1, f"{label} grew a second instantiate() call site"
        assert "cwd" not in calls[0], f"{label} now names its own project"
        assert "chat_id" in calls[0]
