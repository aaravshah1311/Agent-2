# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
Tests for Dynamic Workflow (``agent2/core/workflow/dynamic.py``, Phase D4).

Run from the repo root:  python -m pytest .github/tests/test_dynamic.py -v

What this suite is for
──────────────────────
Phase D4 turns a sentence into a graph. That makes it the one module in the DAG
family whose input is **somebody else's text**, so almost every test here is
about a boundary rather than a feature:

* ``test_auto_writes_nothing_until_a_human_says_start`` — D4.31's whole bar, and
  the reason PLAN is the default. It spies on ``runner.instantiate`` rather than
  ``WF.instantiate``, because ``dynamic`` reaches the writer through the
  ``runner`` module binding; a spy on the package re-export would watch a
  function nobody on this path calls and pass against a planner that wrote rows
  on every plan. ``test_workflowcmd.py``'s verb test drives ``auto`` through the
  CLI and is *not* this test's proof — its picker answers Esc, so it proves the
  keypress, not the API.
* ``test_the_planner_has_no_writer_of_its_own`` — structural, and it is what
  makes *"through the DAG API, never a second engine"* checkable rather than
  promised: no SQL, no ``database`` import, no ``INSERT`` anywhere in the module.
  The two write paths are named and asserted to be the only two.
* ``test_the_planner_holds_no_ordering_of_its_own`` — the mirror image for
  D4.26. ``_acyclic()`` filters *edges*; it does not compute levels, and the
  levels the draft reports are ``validate.levels_for()``'s own answer, compared
  against a direct call.
* ``test_a_replan_grows_the_graph_and_cannot_undo_settled_work`` — D4.28 on real
  rows: a completed node keeps its task id, a failed node keeps its error and
  its attempt count, and the graph is *larger*, never rewritten.
* ``test_a_failure_the_classifier_declines_earns_no_nodes`` — D4.29's other
  half, driven by denying the capability **after** the failure, which is the
  scenario ``safety.capability_for()``'s docstring exists for. A declined
  failure must be *named*, never silently skipped.
* ``test_rounds_are_read_off_the_row_not_counted_in_memory`` — D4.30. Dual mode
  is two processes over one ``agent2.db``, so a round counter in memory hands
  each of them a full allowance.
* ``test_the_planner_asks_the_broker_once_and_excludes_only_the_workflow_source``
  — D4.32. One ``assemble()``, so skills are selected once, and the only
  exclusion is the source that would describe the run being planned.
* ``test_no_model_degrades_to_the_goal_itself_and_names_which_thing_broke`` —
  four degradations, four distinguishable notes. A one-step plan that came from
  a missing key looks exactly like a deliberate one-step plan, and only the
  first is worth re-running.

Nothing here needs an API key: ``draft`` / ``start`` / ``replan`` all take an
injectable ``ask``, for the reason ``projectdoc.narrate()``'s is injectable.

conftest.py redirects AGENT2_DB to a throwaway temp DB, so nothing here touches a
developer's real agent2.db.
"""

import ast
import json
import re
from pathlib import Path

import pytest

from agent2 import config as cfg
from agent2 import database as db
from agent2.core import broker as B
from agent2.core import execstate as X
from agent2.core import tasks as T
from agent2.core.dag import levels_for
from agent2.core.dag import model as M
from agent2.core.workflow import dynamic as D
from agent2.core.workflow import runner as R

_SRC = Path(__file__).resolve().parent.parent.parent / "agent2"
_DYNAMIC = _SRC / "core" / "workflow" / "dynamic.py"

# A token that must never leave the definition it was written into.
SECRET_STEP = "STEP-DO-NOT-LEAK-4d31"


@pytest.fixture(autouse=True)
def _clean():
    """One temp DB is shared by every suite, and `live()` answers "the newest
    unsettled run in this project" — so a leftover row would make the next test
    re-plan somebody else's workflow."""
    db.init_db()
    X.reset()
    db.exe("DELETE FROM exec_workflows")
    X._failure_reported = False
    yield
    X.reset()
    db.exe("DELETE FROM exec_workflows")
    X._failure_reported = False


# ── Structural helpers ────────────────────────────────────────────────────────
#
# ⚠️ `ast`, never a regex over the file, and this is `core/dag/`'s own idiom for
# its feature-agnosticism tests. `dynamic.py`'s docstrings name `levels_for`,
# `plan_next` and `INSERT` on purpose — saying which module owns a fact is how
# this repo documents a boundary — so a text sweep would fail on the prose that
# describes the very rule it is checking.

def _tree(path=None):
    return ast.parse((path or _DYNAMIC).read_text(encoding="utf-8"))


def _docstrings(tree):
    kinds = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    return {ast.get_docstring(n, clean=False) for n in ast.walk(tree)
            if isinstance(n, kinds) and ast.get_docstring(n, clean=False)}


def _names(tree):
    """Every identifier the CODE touches — bare names, attributes, imports."""
    docs = _docstrings(tree)
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
        elif isinstance(node, ast.alias):
            out.add((node.asname or node.name).split(".")[-1])
        elif isinstance(node, ast.ImportFrom):
            out.add(str(node.module or ""))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value not in docs:
                out.add(node.value)
    return out


def _calls(tree, base):
    """`_store.plan(...)` → `{"plan"}`. What this module asks another module for."""
    return {n.func.attr for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and isinstance(n.func.value, ast.Name) and n.func.value.id == base}


# ── Fixtures over rows ────────────────────────────────────────────────────────

def _steps(*specs):
    """`("id", "needs,needs")` pairs → the `steps` list a model would reply with."""
    out = []
    for spec in specs:
        nid, needs = (spec if isinstance(spec, tuple) else (spec, ""))
        out.append({"id": nid, "title": f"{nid} title", "instruction": f"do {nid}",
                    "needs": [w for w in str(needs).split(",") if w]})
    return out


def _answer(*specs, **extra):
    """An `ask` that replies with a plan and records every prompt it was given."""
    body = json.dumps({"steps": _steps(*specs), **extra})
    seen: list[str] = []

    def ask(prompt):
        seen.append(str(prompt))
        return body

    ask.seen = seen
    return ask


def _raw(text):
    seen: list[str] = []

    def ask(prompt):
        seen.append(str(prompt))
        return text

    ask.seen = seen
    return ask


def _counts():
    """Row counts across every table a run touches."""
    return (
        db.qone("SELECT COUNT(*) AS c FROM exec_workflows")["c"],
        db.qone("SELECT COUNT(*) AS c FROM task_sessions")["c"],
        db.qone("SELECT COUNT(*) AS c FROM agent_tasks")["c"],
    )


def _started(goal="ship the thing", *specs, chat_id="chat-dyn", **kw):
    """A real AUTO run, so the D4.28–D4.30 tests work on rows and not on a draft."""
    out = D.start(goal, mode=D.MODE_AUTO, chat_id=chat_id,
                  ask=_answer(*(specs or ("build",))), **kw)
    assert out.ok and out.started, (out.reason, out.note)
    return out


def _broken(*, error="boom", extra="ship"):
    """A run with ONE failed node and ONE node still open.

    ⚠️ The open node is not decoration. `replan()` refuses a **settled** run
    (`X_SETTLED`) before it looks at failures, and a run whose every node has
    settled is settled however those nodes ended — so a remediation plan only
    exists while there is still a run to remediate. A one-node run that failed
    is history; you start another one.
    """
    out = _started("ship the thing", "build", (extra, "build"))
    T.fail(out.run.node_tasks["build"], error=error)
    R.advance(out.run_id)
    fresh = R.state_for(out.run_id)
    assert fresh.finished is False, "the fixture left nothing to re-plan"
    return out


def _views(run_id):
    """The engine's own node views — `attempts`, `dispatchable`, `seq`.

    ⚠️ `runner.NodeState` is the workflow projection (phases, progress) and
    deliberately carries neither: *"a thin domain-level wrapper around the same
    core DAG API"* means the graph facts stay on `store.NodeView`, reached
    through `graph_state`, rather than being copied onto a second dataclass.
    """
    return {v.node: v for v in R.state_for(run_id).graph_state.nodes}


def _spy_instantiate(monkeypatch):
    """Watch the ONE writer. ⚠️ Patched on `runner`, which is the binding
    `dynamic` resolves at call time — see this suite's docstring."""
    calls: list[dict] = []
    real = R.instantiate

    def spy(defn, **kw):
        calls.append(dict(kw))
        return real(defn, **kw)

    monkeypatch.setattr(R, "instantiate", spy)
    return calls


# ── D4.26 · the planner decides WHAT, the DAG decides STRUCTURE ───────────────

def test_the_planner_decides_what_and_the_dag_decides_structure():
    """The steps are the model's; the order, the levels and the verdict are not.

    ⚠️ The comparison is against a **direct** `validate.levels_for()` call on the
    same nodes. A draft carrying its own idea of what may start first would be a
    second answer to `plan_next()`'s question, and the two would disagree only
    on the graphs nobody tests with.
    """
    out = D.draft("ship it", ask=_answer("build", ("test", "build"),
                                         ("docs", "build"), ("ship", "test,docs")))
    assert out.ok and out.source == D.SRC_MODEL and out.note == ""
    assert isinstance(out.defn, M.Graph), "a draft's definition is not the core Graph"
    assert [n.id for n in out.defn.nodes] == ["build", "test", "docs", "ship"]

    mine = levels_for(out.defn.nodes)
    assert out.validation.levels == mine, "the draft's levels are not the engine's"
    assert mine[0] == ("build",)
    ready = {n.node for n in out.preview if n.state == M.READY}
    assert ready == set(mine[0]), "the preview's READY set is not levels[0]"
    assert {n.node for n in out.preview if n.state == M.BLOCKED} == {"test", "docs", "ship"}
    # The DAG's own definition object, not a lookalike.
    assert out.defn.source == D.DEF_SOURCE and out.defn.name == out.name


def test_the_planner_holds_no_ordering_of_its_own():
    """Structural: `dynamic.py` computes no order, no readiness and no cycle.

    ⚠️ This is D4.26's half of `core/dag/`'s feature-agnosticism tests, pointed
    the other way. `_acyclic()` filters *edges* in declaration order — three of
    the validator's problems become unreachable as a side effect — but the
    levels, the waves and the verdict are `validate`'s, and a second topological
    sort here would be the *"one generalized DAG"* rule broken from above.

    ⚠️ Over the **AST**, not the text: this module's docstrings name
    `levels_for` and `plan_next` to say who owns them, and a text sweep would
    fail on the sentence that documents the boundary it is checking.
    """
    used = _names(_tree())
    for banned in ("levels_for", "find_cycles", "validate", "validate_mutation",
                   "plan_next", "ready", "blockers", "dispatchable", "waves"):
        assert banned not in used, f"dynamic.py derives {banned!r} itself"
    # …and it does reach the engine for exactly those answers.
    assert "plan" in _calls(_tree(), "_store")
    assert "make_def" in _calls(_tree(), "_graph")


def test_a_generated_plan_is_always_valid_by_shape_and_says_what_it_dropped():
    """Somebody else's text cannot produce a graph the engine would refuse.

    A self-dependency, an unknown id and a forward reference are the three
    `validate` problems a generated plan would otherwise hit, and each one is
    **reported** — a plan quietly missing the edge the model described is a plan
    a human cannot check.
    """
    out = D.draft("ship it", ask=_answer(("build", "build"),           # self
                                         ("test", "nowhere"),          # unknown
                                         ("docs", "ship"),             # forward
                                         ("ship", "build")))
    assert out.ok and out.validation.problems == []
    dropped = {(d["step"], d["needs"]) for d in out.dropped}
    assert dropped == {("build", "build"), ("test", "nowhere"), ("docs", "ship")}
    assert {n.id: tuple(n.needs) for n in out.defn.nodes} == {
        "build": (), "test": (), "docs": (), "ship": ("build",)}


def test_every_rename_is_reported_and_one_id_means_one_node():
    """Two steps spelled the same way are two nodes, and the fold is stated."""
    out = D.draft("ship it", ask=_answer("Build Step", "build step", "build-step"))
    ids = [n.id for n in out.defn.nodes]
    assert len(set(ids)) == len(ids) == 3, ids
    assert all(M.NODE_ID_RE.match(i) for i in ids), ids
    renames = {(r["from"], r["to"]) for r in out.renames}
    assert renames, "a folded id was not reported"
    assert all(f != t for f, t in renames)


def test_the_plan_is_clipped_at_max_steps_and_names_the_ceiling():
    """⚠️ Clipped, not refused — `_acyclic()` is what licenses it. Every edge
    points backwards, so declaration order IS a topological order and the tail
    can go without leaving one dangling `needs`."""
    out = D.draft("ship it", max_steps=2,
                  ask=_answer("one", ("two", "one"), ("three", "two"), ("four", "three")))
    assert out.ok and out.steps == 2
    assert out.truncated is True and out.truncated_by == D.T_STEPS
    assert [n.id for n in out.defn.nodes] == ["one", "two"]
    assert out.validation.problems == []      # the clip left no dangling edge


def test_a_plan_writes_nothing_at_all():
    """`draft()` is the dry run — `store.plan()`'s own guarantee, end to end."""
    before = _counts()
    out = D.draft("ship it", ask=_answer("build", ("test", "build")))
    assert out.ok and out.started is False and out.run_id == ""
    assert _counts() == before


# ── D4.27 · through the DAG API, never a second engine ────────────────────────

def test_auto_writes_nothing_until_a_human_says_start(monkeypatch):
    """D4.31's bar, measured at the one writer.

    ⚠️ PLAN must not reach `runner.instantiate()` even once. `Draft.started` is
    derived from the run object rather than from `mode == AUTO`, so an AUTO draft
    the engine refused still reports "nothing was written" — reading the request
    as the outcome is how a refusal gets rendered as a running workflow.
    """
    calls = _spy_instantiate(monkeypatch)
    before = _counts()

    quiet = D.start("ship the thing", ask=_answer("build", ("test", "build")))
    assert quiet.mode == D.MODE_PLAN and quiet.ok
    assert calls == [] and _counts() == before
    assert quiet.started is False and quiet.run_id == "" and quiet.rounds == 0

    loud = D.start("ship the thing", mode=D.MODE_AUTO, chat_id="chat-dyn",
                   ask=_answer("build", ("test", "build")))
    assert loud.started is True and loud.run_id
    assert len(calls) == 1, f"AUTO reached the writer {len(calls)} times"
    assert calls[0]["surface"] == D.SURFACE, "the run is not stamped as the planner's"
    runs, sessions, tasks = _counts()
    assert (runs, sessions, tasks) == (before[0] + 1, before[1] + 1, before[2] + 2)


def test_the_planner_has_no_writer_of_its_own():
    """Structural: the module cannot write, so it cannot be a second engine.

    ⚠️ *"ONE SHARED EXECUTION INFRASTRUCTURE"* is a property of what this file
    is *unable* to do. `runner.instantiate()` and `store.extend()` are the two
    write paths, and both carry the validator, the capability gate and the
    mutation rules with them.
    """
    tree = _tree()
    used = _names(tree)
    for banned in ("sqlite3", "database", "exe", "exemany", "batch", "qall", "qone",
                   "set_status", "complete", "fail", "checkpoint"):
        assert banned not in used, f"dynamic.py writes state itself ({banned!r})"
    for text in (s for s in used if isinstance(s, str)):
        assert not re.search(r"\b(INSERT|UPDATE|DELETE|CREATE TABLE)\b", text), text
    assert _calls(tree, "_store") == {"plan", "extend"}, _calls(tree, "_store")
    assert _calls(tree, "_runner") == {"instantiate", "state_for"}, _calls(tree, "_runner")


def test_a_generated_node_is_a_task_row_like_any_other():
    """No new table, no migration: a planned node is `agent_tasks`, so the
    checkpoints, the heartbeat and the crash scan already cover it."""
    out = _started("ship the thing", "build", ("test", "build"))
    st = R.state_for(out.run_id)
    assert st.exists and st.total == 2 and st.source == D.DEF_SOURCE
    row = X.workflow(out.run_id)
    assert row and json.loads(row["state"] or "{}").get("source") == D.DEF_SOURCE

    for node in st.nodes:
        assert node.task_id, node.node
        task = T.get(node.task_id)
        assert task is not None, node.node
        assert task.checkpoint[T.CP_NODE] == node.node
        assert task.checkpoint[T.CP_WORKFLOW] == out.run_id
    # And readiness is `tasks.ready()`'s answer, reached through the graph.
    assert {v.node for v in st.graph_state.dispatchable} == {"build"}


# ── D4.28 · completed work stays completed ───────────────────────────────────

def test_a_replan_grows_the_graph_and_cannot_undo_settled_work():
    """D4.28 on real rows. ⚠️ `store.extend()` holds the rule, not this module:
    a settled node cannot be dropped (`P_LOST_NODE`) or rewired (`P_REWIRED`),
    so growth is the only thing a re-plan can express."""
    out = _started("ship the thing", "build", ("test", "build"), ("ship", "test"))
    tasks = dict(out.run.node_tasks)
    T.complete(tasks["build"])
    # ⚠️ RUNNING first, then failed — `attempt_count` bumps on the way INTO
    # RUNNING (`tasks._apply`), so a node failed without ever being started
    # carries 0 attempts and the assertion below would hold vacuously.
    T.set_status(tasks["test"], T.TaskStatus.RUNNING)
    T.fail(tasks["test"], error="linker exploded")
    R.advance(out.run_id)

    grown = D.replan(out.run_id, ask=_answer(("relink", "test"), "smoke-test"))
    assert grown.ok, (grown.reason, grown.note, grown.declined)
    assert set(grown.added) == {"relink", "smoke-test"}

    st = R.state_for(out.run_id)
    assert st.total == 5, [n.node for n in st.nodes]
    by_node = {n.node: n for n in st.nodes}
    views = _views(out.run_id)
    # The completed node is untouched — same row, same verdict.
    assert by_node["build"].task_id == tasks["build"]
    assert by_node["build"].state == M.COMPLETED
    # …and so is the failure that caused the re-plan: no blind retry happened.
    assert by_node["test"].task_id == tasks["test"]
    assert by_node["test"].state == M.FAILED
    assert by_node["test"].error == "linker exploded"
    assert views["test"].attempts == 1, "the failed node was re-attempted"
    assert st.finished is False


def test_a_replan_step_may_depend_on_work_that_already_ran():
    """⚠️ `_acyclic(known=existing)` is why remediation attaches to what it is
    remediating. Without the seed the one useful edge — onto the node that
    failed — is the first thing dropped, and the new step runs immediately."""
    out = _started("ship the thing", "build", ("test", "build"), ("ship", "test"))
    T.complete(out.run.node_tasks["build"])
    T.fail(out.run.node_tasks["test"], error="assertion failed")
    R.advance(out.run_id)

    grown = D.replan(out.run_id, ask=_answer(("retest", "test"), ("report", "retest")))
    assert grown.ok and grown.dropped == [], (grown.reason, grown.dropped)
    edges = {n.node: tuple(n.needs) for n in _views(out.run_id).values()}
    assert edges["retest"] == ("test",) and edges["report"] == ("retest",)


def test_a_replan_step_that_reuses_an_existing_id_is_renamed_and_rewired():
    """The other half of `known=` — and the reason it is passed INTO `_steps_from`
    rather than applied after it. ⚠️ Ids and `needs` are resolved in ONE pass: a
    step calling itself `test` when a settled `test` already exists is renamed
    *before* the rename map is built, so the follower's `needs: test` resolves to
    the NEW node. Applied afterwards, the id changed and the edge did not — the
    follower would have waited on the FAILED node, forever, with nothing to see."""
    out = _started("ship the thing", "build", ("test", "build"), ("ship", "test"))
    T.complete(out.run.node_tasks["build"])
    T.set_status(out.run.node_tasks["test"], T.TaskStatus.RUNNING)
    T.fail(out.run.node_tasks["test"], error="assertion failed")
    R.advance(out.run_id)

    grown = D.replan(out.run_id, ask=_answer("test", ("report", "test")))
    assert grown.ok, (grown.reason, grown.declined)
    fresh = str(grown.added[0])
    assert fresh != "test", grown.added
    assert {"from": "test", "to": fresh} in list(grown.renames), grown.renames
    edges = {n.node: tuple(n.needs) for n in _views(out.run_id).values()}
    assert edges["report"] == (fresh,), edges
    assert edges["test"] == ("build",), "a settled node was rewired"


def test_a_replan_that_plans_nothing_refuses_and_grows_nothing():
    """⚠️ An *empty* list, not a junk step: `_steps_from` derives an id from a
    step's title when it has no `id`, so `{"title": "x"}` is a node. Nothing
    planned means the model returned no steps at all — pinned both ways below."""
    out = _broken()
    before = _counts()

    grown = D.replan(out.run_id, ask=_raw('{"steps": []}'))
    assert grown.ok is False and grown.reason == D.X_NOTHING_PLANNED
    assert _counts() == before
    assert R.state_for(out.run_id).total == 2

    # …and the other half of the same fact: a title alone IS a step.
    titled = D.replan(out.run_id, ask=_raw('{"steps": [{"title": "no id at all"}]}'))
    assert titled.ok and titled.added == ("no-id-at-all",), titled.added


# ── D4.29 · a failure classified, then new nodes ──────────────────────────────

def test_a_replan_carries_the_error_and_the_classifiers_own_words():
    """⚠️ *"A failure classified, then new nodes — never a blind retry."* The
    prompt must carry what went wrong AND what `core/recovery` decided about it;
    a planner given only the node name can do nothing but propose it again."""
    out = _started("ship the thing", "build", ("test", "build"))
    T.fail(out.run.node_tasks["test"], error="ImportError: no module named yaml")
    R.advance(out.run_id)

    ask = _answer(("vendor-yaml", "test"))
    grown = D.replan(out.run_id, ask=ask)
    assert grown.ok, (grown.reason, grown.declined)
    prompt = ask.seen[0]
    assert "test" in prompt and "ImportError: no module named yaml" in prompt
    assert "already settled" in prompt, "the classifier's verdict is not in the prompt"
    # The nodes that already exist are NAMED, so the model cannot re-propose them.
    assert "build" in prompt and "do build" not in prompt


def test_a_failure_the_classifier_declines_earns_no_nodes(monkeypatch):
    """⚠️ Denied **after** the failure — `safety.capability_for()`'s own
    scenario: *"a previously authorized operation should not automatically
    bypass current permissions."* And the decline is **named**: a failure absent
    from a re-plan with no stated reason is indistinguishable from a planner
    that did not look."""
    out = _broken()
    before = _counts()

    monkeypatch.setenv("AGENT2_DENY_CAPS", "chat")
    ask = _answer("remediate")
    grown = D.replan(out.run_id, ask=ask)

    assert grown.ok is False and grown.reason == D.X_FORBIDDEN
    assert ask.seen == [], "the model was asked about a failure nobody licensed"
    assert [d["node"] for d in grown.declined] == ["build"]
    assert grown.declined[0]["reason"] == D.X_FORBIDDEN
    assert grown.declined[0]["why"], "a decline with no reason is silence"
    assert _counts() == before


def test_nothing_failed_is_a_refusal_not_an_empty_replan():
    out = _started("ship the thing", "build", ("test", "build"))
    ask = _answer("extra")
    grown = D.replan(out.run_id, ask=ask)
    assert grown.ok is False and grown.reason == D.X_NO_FAILURE
    assert ask.seen == [] and R.state_for(out.run_id).total == 2


def test_a_settled_run_and_an_unknown_run_are_two_different_refusals():
    assert D.replan("no-such-run").reason == D.X_UNKNOWN_RUN
    out = _started("ship the thing", "build")
    T.complete(out.run.node_tasks["build"])
    R.advance(out.run_id)
    assert R.state_for(out.run_id).finished
    assert D.replan(out.run_id).reason == D.X_SETTLED


# ── D4.30 · bounded rounds, reported ─────────────────────────────────────────

def test_rounds_are_bounded_and_the_refusal_names_the_count(monkeypatch):
    monkeypatch.setattr(cfg, "DYNAMIC_MAX_ROUNDS", 1)
    out = _broken()

    first = D.replan(out.run_id, ask=_answer("fix-one"))
    assert first.ok and first.rounds == 1 and first.rounds_left == 0
    assert set(first.added) == {"fix-one"}

    T.fail(_views(out.run_id)["fix-one"].task_id, error="boom again")
    ask = _answer("fix-two")
    second = D.replan(out.run_id, ask=ask)
    assert second.ok is False and second.reason == D.X_ROUNDS
    assert "1 of 1" in second.note, second.note
    assert ask.seen == [], "a spent budget still paid for a model call"
    assert R.state_for(out.run_id).total == 3


def test_rounds_are_read_off_the_row_not_counted_in_memory():
    """⚠️ Dual mode is two processes over one `agent2.db`. A counter in memory
    hands each of them a full allowance and the ceiling silently doubles — so
    `rounds_of()` reads `extensions` off the run, and reads it from a **fresh**
    state object here to prove it."""
    out = _broken()
    assert D.rounds_of(R.state_for(out.run_id)) == 0

    grown = D.replan(out.run_id, ask=_answer("fix-one"))
    assert grown.ok, (grown.reason, grown.declined)
    fresh = R.state_for(out.run_id)
    assert D.rounds_of(fresh) == 1
    assert D.rounds_of(fresh.graph_state) == 1          # either object, one answer
    assert json.loads(X.workflow(out.run_id)["state"] or "{}").get("extensions") == 1
    assert D.rounds_of(None) == 0                       # total, like every reader here


def test_every_draft_carries_the_round_budget_even_when_it_refused():
    """⚠️ Reported, not merely bounded. A user who hit the ceiling needs the
    number in the same payload as the refusal, or the only way to learn it is to
    read the source."""
    payloads = [
        D.draft("", ask=_answer("x")).to_payload(),
        D.start("goal", mode="atuo", ask=_answer("x")).to_payload(),
        D.draft("goal", ask=_answer("x")).to_payload(),
        D.replan("no-such-run").to_payload(),
    ]
    for pay in payloads:
        assert pay["max_rounds"] == int(cfg.DYNAMIC_MAX_ROUNDS)
        assert pay["rounds"] == 0 and pay["rounds_left"] == int(cfg.DYNAMIC_MAX_ROUNDS)
        assert set(pay) >= {"ok", "reason", "mode", "source", "note", "steps",
                            "dropped", "renames", "truncated", "truncated_by",
                            "runnable", "started", "run_id", "declined",
                            "problems", "warnings", "plan"}


# ── D4.31 · explicit activation ──────────────────────────────────────────────

def test_plan_is_the_default_and_a_typo_never_becomes_auto(monkeypatch):
    """⚠️ `AGENT2_WEB_ROLE`'s direction: an unrecognised word falls to the side
    that changes nothing. Coercing `atuo` to `auto` would make a misspelling
    create task rows."""
    calls = _spy_instantiate(monkeypatch)
    before = _counts()

    assert D.start("goal", ask=_answer("build")).mode == D.MODE_PLAN
    bad = D.start("goal", mode="atuo", ask=_answer("build"))
    assert bad.ok is False and bad.reason == D.X_BAD_MODE
    assert bad.mode == D.MODE_PLAN, "a refused mode was reported as the mode"
    assert D.start("", mode=D.MODE_AUTO, ask=_answer("build")).reason == D.X_NO_GOAL
    assert calls == [] and _counts() == before


def test_the_off_switch_refuses_every_entry_point_and_writes_nothing(monkeypatch):
    monkeypatch.setattr(cfg, "DYNAMIC_ENABLED", False)
    before = _counts()
    assert D.draft("goal", ask=_answer("build")).reason == D.X_OFF
    assert D.start("goal", mode=D.MODE_AUTO, ask=_answer("build")).reason == D.X_OFF
    assert D.replan("whatever").reason == D.X_OFF
    assert _counts() == before
    assert D.describe()["enabled"] is False


def test_no_agent_loop_reaches_the_planner():
    """⚠️ *"Explicit activation; never automatic for a normal prompt."* The
    planner is reached from the `/workflow auto` command, the route behind the
    browser's button, and Phase D5's engine — and none of the three is on the
    turn path.

    Asserted from the *importers*' side rather than from `dynamic`'s, because
    the failure this forbids is a turn loop deciding to plan by itself, and that
    edit would land in the loop.

    ⚠️ **`core/ultracode/engine.py` IS THE THIRD IMPORTER AND IT IS NOT A
    LOOPHOLE — IT IS THE SAME RULE ONE LAYER OUT.** UltraCode's PLAN and RE-PLAN
    arrows *are* `core.workflow.dynamic` (that is the whole reason it is a
    consumer rather than a second planner), so the engine imports it by design.
    What makes that safe is that the engine is behind exactly the same kind of
    explicit door: `/ultracode` and `POST /api/ultracode`, and nothing else in
    the tree reaches `core.ultracode` at all. So the two halves are asserted
    together — a turn loop that grew a planner import would fail the first half,
    and one that grew an *UltraCode* import (the way around it) fails the second.
    Before this test named the engine, the list said two importers and the tree
    had three, which is `dynamic.py`'s own foreshadowed *"exactly as
    `/ultracode`'s activation **will be**"* coming due.
    """
    turn_path = [
        _SRC / "agent.py", _SRC / "tools.py",
        _SRC / "llm" / "provider_agent.py", _SRC / "llm" / "router.py",
        _SRC / "cli" / "prompt.py", _SRC / "cli" / "runtime.py",
        _SRC / "core" / "scheduler.py", _SRC / "core" / "broker" / "__init__.py",
        _SRC / "server" / "sockets.py",
    ]
    for path in turn_path:
        text = path.read_text(encoding="utf-8")
        assert "workflow import dynamic" not in text, f"{path.name} imports the planner"
        assert "workflow.dynamic" not in text, f"{path.name} reaches the planner"
        assert "core import ultracode" not in text, f"{path.name} imports UltraCode"

    tree = [*_SRC.rglob("*.py"), _SRC.parent / "agent2cli.py"]
    found = sorted(
        p.relative_to(_SRC.parent).as_posix()
        for p in tree
        if p != _DYNAMIC and "workflow import dynamic" in p.read_text(encoding="utf-8")
    )
    assert found == [
        "agent2/core/ultracode/engine.py",
        "agent2/server/routes.py",
        "agent2cli.py",
    ], found

    # …and UltraCode's own two doors, which is what keeps that third entry honest.
    uc = _SRC / "core" / "ultracode"
    doors = sorted(
        p.relative_to(_SRC.parent).as_posix()
        for p in tree
        if uc not in p.parents
        and "core import ultracode" in p.read_text(encoding="utf-8")
    )
    assert doors == ["agent2/server/routes.py", "agent2cli.py"], doors


# ── D4.32 · the broker, once, minus one source ────────────────────────────────

def test_the_planner_asks_the_broker_once_and_excludes_only_the_workflow_source(
        monkeypatch):
    """⚠️ ONE `assemble()`. The broker's `skills` collector already **is**
    `core.skills.for_turn()`, so a second call here would be a second discovery
    pass and a second selection for one prompt — and `workflow_state` is the one
    exclusion, because handing the planner the state of the run it is planning
    for is a loop."""
    from agent2.core.broker import sources as S

    seen: list[dict] = []
    real = B.assemble

    def spy(**kw):
        seen.append(dict(kw))
        return real(**kw)

    monkeypatch.setattr(B, "assemble", spy)
    out = D.draft("add a health endpoint", chat_id="chat-dyn", model="2.5-flash",
                  mode_key="pro", ask=_answer("build"))
    assert out.ok

    assert len(seen) == 1, f"the planner assembled context {len(seen)} times"
    kw = seen[0]
    assert kw["surface"] == D.SURFACE
    assert kw["exclude"] == frozenset({S.SOURCE_WORKFLOW}), kw["exclude"]
    assert S.SOURCE_SKILLS not in kw["exclude"], "skills were excluded from a plan"
    assert kw["message"] == "add a health endpoint"
    assert (kw["chat_id"], kw["model_key"], kw["mode_key"]) == ("chat-dyn", "2.5-flash",
                                                               "pro")


def test_a_broken_broker_costs_context_and_never_the_plan(monkeypatch):
    """Totality, the broker's own rule pointed at its caller: no context is a
    thinner prompt, never a refusal."""
    def boom(**kw):
        raise RuntimeError("broker exploded")

    monkeypatch.setattr(B, "assemble", boom)
    out = D.draft("ship it", ask=_answer("build", ("test", "build")))
    assert out.ok and out.steps == 2 and out.source == D.SRC_MODEL


def test_the_planner_never_selects_skills_itself():
    """⚠️ The broker's `skills` collector already IS `core.skills.for_turn()`, so
    a second call here would be a second discovery pass and a second selection
    inside one prompt. `dynamic.py` reaches `core.skills` never — the whole
    subsystem arrives through `assemble()` or not at all."""
    used = _names(_tree())
    for banned in ("skills", "for_turn", "discover", "select_for", "SkillSet"):
        assert banned not in used, f"the planner reaches {banned!r} itself"


# ── Degradation, reporting, vocabulary ───────────────────────────────────────

def test_no_model_degrades_to_the_goal_itself_and_names_which_thing_broke():
    """⚠️ FOUR distinguishable notes, `projectdoc.narrate()`'s four. A one-step
    plan from a missing key looks exactly like a deliberate one-step plan, and
    only the first is worth re-running — so the floor is a usable plan *plus* a
    sentence saying nobody broke the goal down."""
    def raiser(prompt):
        raise RuntimeError("no key")

    cases = {
        "model failed": raiser,
        "no model answered": _raw("   "),
        "the model's reply was not usable": _raw("I am afraid I cannot do that."),
        "no steps were proposed": _raw('{"steps": []}'),
    }
    for expected, ask in cases.items():
        out = D.draft("add a health endpoint", ask=ask)
        assert out.ok, expected
        assert out.source == D.SRC_GOAL and out.steps == 1
        assert expected in out.note, (expected, out.note)
        node = out.defn.nodes[0]
        assert node.instruction == "add a health endpoint"
        assert out.validation.ok and out.runnable


def test_describe_states_the_posture_and_every_refusal():
    got = D.describe()
    assert got["modes"] == [D.MODE_PLAN, D.MODE_AUTO]
    assert got["default_mode"] == D.MODE_PLAN, "the default is not the safe mode"
    assert got["surface"] == D.SURFACE and got["sources"] == list(D.SOURCES)
    assert got["refusals"] == list(D.REFUSALS) and len(D.REFUSALS) == len(set(D.REFUSALS))
    assert (got["max_steps"], got["max_rounds"], got["max_nodes"]) == (
        int(cfg.DYNAMIC_MAX_STEPS), int(cfg.DYNAMIC_MAX_ROUNDS),
        int(cfg.WORKFLOW_MAX_NODES))
    # Every declared refusal is one this module can actually reach.
    src = _DYNAMIC.read_text(encoding="utf-8")
    for word in D.REFUSALS:
        name = next(k for k, v in vars(D).items() if k.startswith("X_") and v == word)
        assert len(re.findall(rf"\b{name}\b", src)) >= 2, f"{name} is declared and unused"


def test_the_refusal_vocabulary_shares_no_word_with_the_other_three():
    """⚠️ A FOURTH closed vocabulary. One shared spelling would make "why this
    node waited" and "why the planner declined" read as one answer."""
    from agent2.core.dag import schedule as SCH
    from agent2.core.skills import select as SEL

    mine = set(D.REFUSALS)
    assert not mine & set(SCH.HOLD_CODES), mine & set(SCH.HOLD_CODES)
    assert not mine & set(SCH.REASONS), mine & set(SCH.REASONS)
    assert not mine & set(SEL.REASONS), mine & set(SEL.REASONS)
    whys = {SEL.OUT_DISABLED, SEL.OUT_SHADOWED, SEL.OUT_CAP, SEL.OUT_CHARS, SEL.OUT_EMPTY}
    assert not mine & whys, mine & whys
    assert D.T_STEPS not in mine


def test_no_step_instruction_ever_reaches_a_payload():
    """A planned instruction is prose about somebody's own project, and the
    payload is what `POST /api/workflows/auto` hands a browser."""
    out = D.draft(f"ship it — {SECRET_STEP}",
                  ask=_answer("build", ("test", "build")))
    assert out.ok
    blob = json.dumps(out.to_payload())
    for node in out.preview:
        assert node.instruction, "the preview lost the instruction it needs"
        assert node.instruction not in blob, f"{node.node}'s instruction leaked"
    assert "instruction" not in blob


def test_the_terminal_prints_the_source_the_note_the_renames_and_the_drops(capsys):
    """The CLI's half of *reported, never silent* — four accounting facts, and a
    goal-sourced plan says so in words rather than by looking small."""
    from agent2.cli import render

    out = D.draft("ship it", max_steps=2,
                  ask=_answer(("Build Step", "nowhere"), "build step", "third"))
    assert render.render_dynamic_draft(out.to_payload(), header="Plan") is True
    text = capsys.readouterr().out
    assert "Plan" in text
    for node in out.preview:
        assert node.node in text
    assert "nowhere" in text, "a dropped edge was not printed"
    assert "steps" in text.lower(), "the clip was not named"

    poor = D.draft("ship it", ask=_raw(""))
    assert render.render_dynamic_draft(poor.to_payload()) is True
    said = capsys.readouterr().out
    assert "no model answered" in said and "not by a model" in said


def test_a_long_node_id_never_runs_into_the_instruction_beside_it(capsys, monkeypatch):
    """⚠️ THE BUG THE TEST ABOVE CANNOT SEE, IN THE FIRST REAL PLAN THIS EVER PRINTED.

    `render._plan_row` formatted its label as `f"{label:<8}"`, and a field width pads
    only a label *shorter* than the field. `render_dag_plan`'s three labels are
    `Wave n`, `Next` and `Held` — all under eight characters — so the gutter looked
    like a property of the format string. This renderer's label is `"1. "` plus a node
    id, and `/workflow auto` folds its ids out of the goal sentence, so a real plan
    printed:

        1. build-a-small-calculatorbuild a small calculator

    Two facts run together, with no error and no traceback. The test above asserts
    `node.node in text`, and a jammed row contains that substring — which is exactly
    how presentation defects survive a green suite. Both renderer branches are checked
    (`⚠️ EVERY PRINTER HAS TWO BODIES`), and the short labels are pinned to the column
    they already had, so the fix cannot silently move `Wave 1`/`Next`/`Held`.
    """
    from agent2.cli import render

    ansi = re.compile(r"\x1b\[[0-9;]*m")
    long_id, title = "build-a-small-calculator", "build a small calculator"
    pay = {"ok": True, "mode": "plan", "steps": 1, "runnable": True, "source": "model",
           "plan": [{"node": long_id, "title": title}]}

    # Whatever this install has, plus the plain branch — forcing Rich ON where it is
    # absent would test a `_con` that does not exist.
    for rich in ([True, False] if render._RICH else [False]):
        monkeypatch.setattr(render, "_RICH", rich)
        assert render.render_dynamic_draft(pay) is True
        text = ansi.sub("", capsys.readouterr().out)
        row = next((ln for ln in text.splitlines() if long_id in ln), "")
        assert row, f"the node id was not printed at all (rich={rich})"
        assert f"{long_id}{title}" not in row, (
            f"id and instruction ran together (rich={rich}): {row.strip()!r}")
        after = row.partition(long_id)[2]
        assert after[:1].isspace(), (
            f"nothing separates the id from what follows (rich={rich}): {row.strip()!r}")
        assert title in after, f"the instruction is not on that row (rich={rich})"

        # Eight characters is where the old form stopped separating anything at all.
        render._plan_row("12345678", "body")
        assert "12345678  body" in ansi.sub("", capsys.readouterr().out), rich

        # …and the three real short labels keep their column, to the byte.
        for label, pad in (("Wave 1", "  "), ("Next", "    "), ("Held", "    ")):
            render._plan_row(label, "body")
            assert f"{label}{pad}body" in ansi.sub("", capsys.readouterr().out), label


def test_the_renderer_is_total_on_junk():
    """Presentation may never raise into a turn — a hand-built payload, a
    missing key and a wrong type all print something."""
    from agent2.cli import render

    for pay in ({}, {"ok": True}, {"ok": True, "plan": None, "steps": "many"},
                {"ok": False, "reason": D.X_ROUNDS, "note": None},
                {"ok": True, "plan": [{"node": "x"}, "junk", None], "dropped": ["bad"],
                 "renames": [None], "problems": [{"message": "m"}, "s"]}):
        assert isinstance(render.render_dynamic_draft(pay), bool)
