# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
The generalized DAG core — Phase D1's acceptance suite.

Run from the repo root:  python -m pytest .github/tests/test_dag.py -v

What this suite is for
──────────────────────
`agent2/core/dag/` is ONE engine that knows nothing about the features built on
top of it, so every rule below is either a fact a consumer relies on or a silent
failure nobody else can see. Each bullet names the test that pins it.

* **A cycle is the one error that produces no error**, so it is pinned in BOTH
  directions: ``test_the_validator_refuses_a_ring_and_names_it`` proves the
  refusal, and ``test_hand_built_cyclic_rows_really_do_deadlock_readiness``
  proves what the refusal is *for* — `tasks.ready()` returning `[]` forever, with
  every row still PENDING, nothing raised and nothing logged. A validator whose
  refusal nobody demonstrated is a validator that could be deleted without a
  test failing.
* **An unknown dependency is fatal for the mirror-image reason.**
  ``test_an_unknown_dependency_is_fatal_here_and_tolerated_by_readiness`` shows
  the row layer happily releasing a task whose dependency does not exist, which
  is a node running *early* and a plan that quietly is not the plan written.
* **A long chain is reported, never recursed into.**
  ``test_find_cycles_reports_a_long_ring_instead_of_recursing`` walks 1 500 nodes
  and ``test_levels_for_resolves_a_long_chain_without_recursing`` 400 — a
  `RecursionError` on the turn path is the failure `loader._value_for` already
  shipped once.
* **A ceiling refuses and never truncates** —
  ``test_a_ceiling_refuses_a_graph_and_never_truncates_it``. A graph clipped to
  fit is a graph whose remaining `needs:` point at nodes that are gone.
* **READY and BLOCKED are derived on every read, never stored.**
  ``test_a_stored_pending_row_is_never_reported_as_pending`` and
  ``test_progress_is_re_derived_and_a_sabotaged_run_row_is_ignored`` — the run
  row is a breadcrumb, so a killed run reports what is true now.
* **Readiness cannot tell success from settlement.**
  ``test_a_failed_upstream_releases_the_node_and_says_so`` pins the surprise a
  scheduler must handle: a node whose upstream FAILED is READY and dispatchable,
  and `upstream_failed` is the only place that fact is stated.
* **A PAUSED row is still returned by `tasks.ready()`**, which is why
  ``test_a_held_node_reads_paused_even_though_readiness_still_lists_it`` exists:
  the projection tests `paused` *before* readiness, and reversing those two lines
  would hand a held node straight back to a worker.
* **A claim is the lock** — ``test_a_claim_is_the_lock_and_the_second_worker_loses``.
  Bounded parallelism is worthless if two workers can take one node.
* **…and a lock a dead process is still holding must be recoverable** —
  ``test_a_node_claimed_by_a_dead_worker_is_recovered_rather_than_stranded``.
  Because the claim IS the lock, a process that dies between `claim()` and the
  RUNNING mark leaves a QUEUED row that beats nothing, that no scheduler may
  re-take, and that reads **READY** on every surface. `tasks.HELD` is what makes
  worker recovery see it.
* **Releasing a crash-park clears the mark that made it one** —
  ``test_releasing_a_crash_park_clears_the_mark_so_a_later_hold_survives``.
  `CP_STOPPED` is the only durable difference between a crash and a `/pause`, so a
  release that left it behind fails the *second* time round: the next automatic
  sweep un-pauses a hold somebody chose.
* **Completed work stays completed** across a re-run attempt
  (``test_a_completed_node_is_never_re_run_and_is_absent_from_a_resume``), a
  mutation (``test_extending_a_run_adds_work_without_touching_what_finished``), a
  removal (``test_a_settled_node_may_not_be_removed``), a rewire
  (``test_a_settled_node_may_not_be_rewired``) and a cancel
  (``test_cancelling_a_run_leaves_completed_work_and_settles_the_run``).
* **Pause is not cancel** — ``test_pause_is_not_cancel``, and unpausing
  re-derives readiness rather than restoring a snapshot
  (``test_unpausing_re_derives_readiness_rather_than_restoring_a_snapshot``).
* **A dry run writes nothing** — ``test_a_plan_writes_nothing_and_still_says_what_would_run_first``,
  ``test_nothing_is_written_when_a_graph_is_refused``, and every refusal path is
  checked against row *counts* rather than against its own report.
* **The engine contains no business logic.** The four `ast` tests
  (``test_the_core_imports_nothing_that_knows_about_a_feature`` and friends) are
  the structural half of *"the DAG engine must NOT contain business-specific
  logic"* — a promise in a docstring is not a promise.
* **The package object shadows its own `validate` submodule**
  (``test_the_package_shadows_its_own_validate_submodule``), which is why this
  file imports the four validator names directly. `from agent2.core.dag import
  validate as V` then `V.levels_for(...)` is an `AttributeError` that only fires
  at run time.

conftest.py redirects AGENT2_DB to a throwaway temp DB, so nothing here touches a
developer's real agent2.db.
"""

from __future__ import annotations

import ast
import json
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from agent2 import config as cfg
from agent2 import database as db
from agent2.core import dag as D
from agent2.core import execstate as X
from agent2.core import tasks as T
from agent2.core.dag import model as M
from agent2.core.dag import schedule as SC
from agent2.core.dag import store as S
from agent2.core.dag.validate import (
    find_cycles,
    levels_for,
    validate,
    validate_mutation,
)
from agent2.core.recovery import crash as CR

#: A node instruction is prose a human wrote. It reaches a worker and nothing else
#: — not a payload, not the run row. Any test that can leak looks for this.
SECRET_NOTE = "INSTRUCTION-DO-NOT-LEAK-9f31"


@pytest.fixture(autouse=True)
def _clean():
    db.init_db()
    X.reset()
    db.exe("DELETE FROM exec_workflows")
    X._failure_reported = False
    yield
    X.reset()
    db.exe("DELETE FROM exec_workflows")
    X._failure_reported = False


# ── helpers ───────────────────────────────────────────────────────────────────
def _node(nid, *, needs=(), instruction="do it", **kw):
    """A node literal in the shape a human writes one."""
    return {"id": nid, "title": str(nid).title(), "instruction": instruction,
            "needs": list(needs), **kw}


def _chain(*ids, instruction="do it"):
    """`a → b → c`: each node depends on the one declared before it."""
    out = []
    for i, nid in enumerate(ids):
        out.append(_node(nid, needs=[ids[i - 1]] if i else [], instruction=instruction))
    return out


def _graph(name="demo", nodes=(), **kw):
    return M.make_graph({"name": name, "nodes": list(nodes), **kw})


def _states(st):
    return {n.node: n.state for n in st.nodes}


def _counts():
    """Every table a run writes to. Refusals are checked against the DELTA."""
    return (
        db.qone("SELECT COUNT(*) AS c FROM exec_workflows")["c"],
        db.qone("SELECT COUNT(*) AS c FROM task_sessions")["c"],
        db.qone("SELECT COUNT(*) AS c FROM agent_tasks")["c"],
    )


def _run(nodes, *, name="demo", chat_id="chat-dag", **kw):
    run = S.create(_graph(name=name, nodes=nodes), chat_id=chat_id, **kw)
    assert run.ok, run.reason
    return run


# ══════════════════════════════════════════════════════════════════════════════
# 1 · The node and edge model
# ══════════════════════════════════════════════════════════════════════════════
def test_a_bare_string_is_a_node_and_its_id_is_folded():
    n = M.make_node("  Fetch-Deps  ", seq=3)
    assert n.id == "fetch-deps"
    assert n.title == "Fetch-Deps"
    assert n.instruction == ""
    assert n.needs == ()
    assert n.priority == 5
    assert n.seq == 3
    assert M.make_node(None).id == ""
    assert M.make_node(42).id == "42"


def test_every_dependency_spelling_folds_into_one_field():
    g = _graph(name="spellings", nodes=[
        {"id": "a"},
        {"id": "b", "needs": ["a"]},
        {"id": "c", "depends_on": ["a"]},
        {"id": "d", "after": "a"},
        {"id": "e", "requires": ["a", "a", " A "]},
        {"id": "f", "needs": "a, b  c"},
    ])
    assert g.edge_map() == {"a": [], "b": ["a"], "c": ["a"], "d": ["a"],
                            "e": ["a"], "f": ["a", "b", "c"]}
    # A repeated dependency is one edge, not two — a second would inflate fan-in.
    assert g.node("e").needs == ("a",)


def test_an_edge_points_from_the_dependency_to_the_dependent():
    g = _graph(name="direction", nodes=[_node("a"), _node("b", needs=["a"])])
    assert [(e.src, e.dst) for e in g.edges()] == [("a", "b")]
    assert g.dependents() == {"a": ["b"], "b": []}
    assert g.edge_map() == {"a": [], "b": ["a"]}


def test_an_edge_is_a_view_and_never_a_second_record():
    assert "edges" not in M.Graph.__dataclass_fields__
    g = _graph(name="views", nodes=[_node("a"), _node("b")])
    once = g.with_edge("a", "b")
    twice = once.with_edge("a", "b")
    assert once.node("b").needs == ("a",)
    assert twice.node("b").needs == ("a",)          # idempotent, never doubled
    assert g.node("b").needs == ()                  # and the original is untouched
    assert twice.without_edge("a", "b").node("b").needs == ()
    assert twice.without_edge("a", "nowhere").node("b").needs == ("a",)
    assert g.with_edge("", "b") is g
    assert g.with_edge("a", "") is g


def test_a_mapping_of_nodes_lets_the_key_win_over_an_inner_id():
    g = M.make_graph({"name": "mapping", "nodes": {
        "build": {"id": "ignored", "instruction": "compile"},
        "test": "run the suite",
    }})
    assert set(g.ids()) == {"build", "test"}
    assert g.node("build").instruction == "compile"
    assert g.node("test").instruction == "run the suite"
    assert g.node("ignored") is None


def test_an_unmapped_top_level_field_is_kept_rather_than_dropped():
    g = M.make_graph({"name": "extras", "nodes": [_node("a")],
                      "owner": "aarav", "budget": 12})
    assert "owner" not in M.GRAPH_KEYS
    assert g.extra == {"owner": "aarav", "budget": 12}
    assert g.to_payload()["extra"] == {"owner": "aarav", "budget": 12}


def test_dropping_a_node_also_drops_every_reference_to_it():
    g = _graph(name="drop", nodes=_chain("a", "b", "c"))
    assert g.edge_map() == {"a": [], "b": ["a"], "c": ["b"]}
    smaller = g.without_nodes(["a"])
    assert set(smaller.ids()) == {"b", "c"}
    assert smaller.edge_map() == {"b": [], "c": ["b"]}
    assert g.without_nodes([]) is g
    assert g.without_nodes(["nowhere"]).ids() == g.ids()


def test_a_replaced_node_keeps_its_place_and_a_new_one_goes_last():
    g = _graph(name="merge", nodes=_chain("a", "b", "c"))
    grown = g.with_nodes([{"id": "b", "instruction": "rewritten"}, _node("d")])
    assert [n.id for n in grown.nodes] == ["a", "b", "c", "d"]
    assert grown.node("b").seq == 1
    assert grown.node("b").instruction == "rewritten"
    assert grown.node("d").seq == 3
    assert g.node("b").instruction == "do it"       # the original is untouched


def test_the_core_declares_no_node_type_of_its_own():
    # Node *types* (tool, LLM, MCP, human approval …) are a consumer's vocabulary
    # built on top of the core. The core carries the label and never reads it.
    assert [x for x in dir(M) if x.startswith("KIND")] == []
    n = M.make_node({"id": "a", "kind": "  MCP  ", "resource": "Repo Lock"})
    assert n.kind == "mcp"
    assert n.resource == "repo lock"
    assert M.make_node({"id": "b", "type": "human approval"}).kind == "human approval"
    assert M.make_node({"id": "c"}).kind == ""


# ══════════════════════════════════════════════════════════════════════════════
# 2 · Validation and cycle detection
# ══════════════════════════════════════════════════════════════════════════════
def test_the_validator_refuses_a_ring_and_names_it():
    v = validate(_graph(name="ring", nodes=[
        _node("a", needs=["c"]), _node("b", needs=["a"]), _node("c", needs=["b"]),
    ]))
    assert v.ok is False
    assert v.codes() == (M.P_CYCLE,)
    assert v.levels == ()                     # never a partial order
    assert len(v.cycles) == 1
    assert set(v.cycles[0]) == {"a", "b", "c"}
    assert "circular dependency" in v.summary()
    assert "→" in v.summary()
    assert v.problems[0]["nodes"] == list(v.cycles[0])


def test_hand_built_cyclic_rows_really_do_deadlock_readiness():
    # The other half of the pair. Readiness is "every dependency has settled", so a
    # ring is not an error — it is silence. Nothing raises, nothing is logged, and
    # the run sits at 0/3 forever. THAT is what the validator above prevents.
    sess = T.open_session(chat_id="dag-ring", goal="a ring of three")
    a = T.create(sess, "a")
    b = T.create(sess, "b", dependencies=[a.id])
    c = T.create(sess, "c", dependencies=[b.id])
    db.exe("UPDATE agent_tasks SET dependencies=? WHERE id=?",
           (json.dumps([c.id]), a.id))
    assert T.ready(sess) == []
    assert T.ready(sess) == []                # …and it stays empty, forever
    assert {t.status for t in T.list_tasks(sess)} == {T.TaskStatus.PENDING}


def test_a_node_that_depends_on_itself_is_refused_without_a_ring():
    v = validate(_graph(name="selfish", nodes=[_node("a", needs=["a"])]))
    assert v.codes() == (M.P_SELF_DEP,)
    assert v.cycles == ()                     # one mistake, one message
    assert v.problems[0]["node"] == "a"
    assert v.levels == ()


def test_find_cycles_reports_a_long_ring_instead_of_recursing():
    size = 1500                               # far past sys.getrecursionlimit()
    edges = {f"n{i}": ([f"n{i - 1}"] if i else []) for i in range(size)}
    assert find_cycles(edges) == ()
    edges["n0"] = [f"n{size - 1}"]            # close the chain into one huge ring
    rings = find_cycles(edges)
    assert len(rings) == 1
    assert len(rings[0]) == size
    assert set(rings[0]) == set(edges)


def test_levels_for_resolves_a_long_chain_without_recursing():
    size = 400
    nodes = [M.make_node({"id": f"n{i}", "needs": [f"n{i - 1}"] if i else []}, seq=i)
             for i in range(size)]
    levels = levels_for(nodes)
    assert len(levels) == size
    assert levels[0] == ("n0",)
    assert levels[-1] == (f"n{size - 1}",)


def test_an_unknown_dependency_is_fatal_here_and_tolerated_by_readiness():
    v = validate(_graph(name="typo", nodes=[_node("a"), _node("b", needs=["tets"])]))
    assert v.ok is False
    assert v.codes() == (M.P_UNKNOWN_DEP,)
    assert v.problems[0]["node"] == "b"
    assert v.problems[0]["needs"] == "tets"    # the FIELD the author has to fix

    # …and this is why it may not be a warning: nothing downstream would say so.
    sess = T.open_session(chat_id="dag-typo", goal="a dangling need")
    t = T.create(sess, "b", dependencies=["no-such-task"])
    assert [x.id for x in T.ready(sess)] == [t.id]      # released immediately
    assert T.blockers(t) == []


def test_a_ceiling_refuses_a_graph_and_never_truncates_it():
    g = _graph(name="big", nodes=[_node(f"n{i}") for i in range(5)])
    v = validate(g, max_nodes=3)
    assert v.ok is False
    assert v.codes() == (M.P_TOO_MANY,)
    assert v.problems[0]["count"] == 5
    assert v.problems[0]["limit"] == 3
    assert "AGENT2_DAG_MAX_NODES" in v.problems[0]["message"]
    assert len(g.nodes) == 5                  # the graph itself is not clipped


def test_the_knob_and_the_noun_are_injected_never_learned():
    # One validator, many vocabularies — the alternative is `if workflow:` deciding
    # which ceiling and which noun apply.
    assert validate(_graph(name="empty"), label="workflow").summary() == \
        "the workflow declares no nodes"
    assert validate(None, label="plan").summary() == "not a plan definition"
    v = validate(_graph(name="big", nodes=[_node("a"), _node("b")]),
                 max_nodes=1, knob="AGENT2_WORKFLOW_MAX_NODES", label="workflow")
    assert "AGENT2_WORKFLOW_MAX_NODES" in v.summary()
    assert "the ceiling is 1" in v.summary()


def test_an_older_schema_is_refused_a_newer_one_is_only_a_warning():
    body = (M.make_node({"id": "a", "title": "A", "instruction": "do it"}),)
    v = validate(M.Graph(name="old", version=0, nodes=body))
    assert v.ok is False
    assert v.codes() == (M.P_SCHEMA,)
    assert v.problems[0]["version"] == 0
    assert v.problems[0]["min"] == M.MIN_SCHEMA

    w = validate(M.Graph(name="new", version=M.SCHEMA_VERSION + 5, nodes=body))
    assert w.ok is True                       # a colleague's newer file still runs
    assert [p["code"] for p in w.warnings] == [M.W_NEW_SCHEMA]
    assert w.warnings[0]["version"] == M.SCHEMA_VERSION + 5
    assert w.warnings[0]["max"] == M.SCHEMA_VERSION
    assert w.levels == (("a",),)


def test_an_unreadable_schema_version_reads_as_older_rather_than_raising():
    v = validate(M.Graph(name="junk", version="nonsense", nodes=(
        M.make_node({"id": "a", "instruction": "do it"}),)))
    assert v.ok is False
    assert M.P_SCHEMA in v.codes()
    assert v.problems[0]["version"] == 0


def test_a_refused_id_is_not_also_audited_for_its_dependencies():
    v = validate(_graph(name="badid", nodes=[
        _node("Bad Id", needs=["nowhere"]), _node("fine"),
    ]))
    assert v.codes() == (M.P_BAD_ID,)
    assert v.problems[0]["node"] == "bad id"
    assert M.P_UNKNOWN_DEP not in v.codes()   # one mistake, one message


def test_a_duplicate_node_id_is_reported_once():
    v = validate(_graph(name="dup", nodes=[_node("a"), _node("a"), _node("b")]))
    assert v.codes() == (M.P_DUPLICATE,)
    assert v.problems[0]["node"] == "a"


def test_a_graph_needs_a_usable_name():
    assert validate(_graph(name="", nodes=[_node("a")])).codes() == (M.P_NO_NAME,)
    bad = validate(M.Graph(name="not a name", nodes=(
        M.make_node({"id": "a", "instruction": "do it"}),)))
    assert bad.codes() == (M.P_BAD_NAME,)
    assert bad.problems[0]["name"] == "not a name"


@pytest.mark.parametrize("junk", [None, "nonsense", 42, [], {"name": "x"}, object()])
def test_validation_is_data_and_never_an_exception(junk):
    v = validate(junk)
    assert v.ok is False
    assert v.codes() == (M.P_NO_NODES,)
    assert v.summary()
    assert v.levels == ()
    assert validate_mutation(junk, junk).ok is False


def test_warnings_never_decide_whether_a_graph_runs():
    v = validate(_graph(name="terse", nodes=[{"id": "a"}, {"id": "b", "needs": ["a"]}]))
    assert v.ok is True
    assert {p["code"] for p in v.warnings} == {M.W_NO_INSTRUCTION}
    assert v.levels == (("a",), ("b",))


# ══════════════════════════════════════════════════════════════════════════════
# 3 · Dependency resolution and parallel readiness
# ══════════════════════════════════════════════════════════════════════════════
def test_the_specs_fan_out_becomes_three_levels():
    # The spec's own example: sequential prose in, parallel structure out.
    v = validate(_graph(name="analyze", nodes=[
        _node("analyze"),
        _node("dependencies", needs=["analyze"]),
        _node("tests", needs=["analyze"]),
        _node("security-scan", needs=["analyze"]),
        _node("report", needs=["dependencies", "tests", "security-scan"]),
    ]))
    assert v.ok, v.summary()
    assert v.levels == (("analyze",),
                        ("dependencies", "tests", "security-scan"),
                        ("report",))


def test_a_level_orders_by_priority_then_declaration_then_id():
    v = validate(_graph(name="ordering", nodes=[
        _node("mid-b", priority=5), _node("mid-a", priority=5),
        _node("late", priority=9), _node("early", priority=1),
    ]))
    assert v.ok, v.summary()
    assert v.levels == (("early", "mid-b", "mid-a", "late"),)


def test_a_level_orders_the_way_readiness_orders_the_rows_it_wrote():
    # Two orderings for one graph is two answers, and the one nobody renders is the
    # one that stays wrong. The plan's first level IS what the row layer releases.
    nodes = [_node("mid-b", priority=5), _node("mid-a", priority=5),
             _node("late", priority=9), _node("early", priority=1)]
    v = validate(_graph(name="agree", nodes=nodes))
    run = _run(nodes, name="agree")
    pool = T.list_tasks(run.session_id)
    assert [S._node_of(t) for t in T.ready(run.session_id, pool)] == list(v.levels[0])


def test_an_unresolvable_order_is_empty_rather_than_partial():
    # A caller handed the first two levels of a graph whose tail is a ring would
    # start work on a plan that can never complete.
    assert levels_for([], edges={"a": [], "b": ["a"], "c": ["d"], "d": ["c"]}) == ()


def test_levels_for_takes_the_union_of_the_pool_and_the_edge_map():
    nodes = [M.make_node({"id": "a"}, seq=0), M.make_node({"id": "b"}, seq=1)]
    # `c` is named only by the map, `b` only by the pool. Either alone is a hole:
    # the first would drop a node out of the plan, the second would raise KeyError.
    levels = levels_for(nodes, edges={"a": [], "c": ["a"]})
    assert levels == (("a", "b"), ("c",))


# ══════════════════════════════════════════════════════════════════════════════
# 4 · Persistence
# ══════════════════════════════════════════════════════════════════════════════
def test_a_plan_writes_nothing_and_still_says_what_would_run_first():
    before = _counts()
    v, views = S.plan(_graph(name="dryrun", nodes=_chain("a", "b", "c")))
    assert v.ok, v.summary()
    assert _counts() == before
    assert [n.node for n in views] == ["a", "b", "c"]
    assert [n.state for n in views] == [M.READY, M.BLOCKED, M.BLOCKED]
    assert [n.task_id for n in views] == ["", "", ""]
    assert [n.node for n in views if n.dispatchable] == ["a"]


def test_a_plan_of_an_unrunnable_graph_reports_pending_and_dispatches_nothing():
    v, views = S.plan(_graph(name="dryring", nodes=[
        _node("a", needs=["c"]), _node("b")]))
    assert v.ok is False
    assert v.codes() == (M.P_UNKNOWN_DEP,)
    assert [n.state for n in views] == [M.PENDING, M.PENDING]
    assert not any(n.dispatchable for n in views)


def test_nothing_is_written_when_a_graph_is_refused():
    before = _counts()
    out = S.create(_graph(name="ring", nodes=[
        _node("a", needs=["b"]), _node("b", needs=["a"])]), chat_id="dag-refuse")
    assert out.ok is False
    assert out.run_id == ""
    assert out.session_id == ""
    assert out.name == "ring"
    assert out.validation.codes() == (M.P_CYCLE,)
    assert _counts() == before


def test_a_run_needs_the_ledger_and_the_refusal_names_the_knob(monkeypatch):
    monkeypatch.setattr(cfg, "EXEC_PERSIST", False)
    before = _counts()
    out = S.create(_graph(name="offledger", nodes=_chain("a", "b")))
    assert out.ok is False
    assert "AGENT2_EXEC_PERSIST=0" in out.reason
    assert out.run_id == ""
    assert _counts() == before
    # The noun is injected here too — a consumer's refusal reads in its own words.
    assert S.create(_graph(name="offledger", nodes=_chain("a", "b")),
                    label="workflow").reason.startswith("workflows need")


def test_progress_is_re_derived_and_a_sabotaged_run_row_is_ignored():
    run = _run(_chain("a", "b", "c"), name="rederived")
    S.mark(run.run_id, "a", T.TaskStatus.COMPLETED)
    db.exe("UPDATE exec_workflows SET state=?, step_index=? WHERE id=?",
           (json.dumps({"done": 99, "total": 99, "failed": 7}), 99, run.run_id))
    st = S.load(run.run_id)
    assert st.done == 1
    assert st.total == 3
    assert st.failed == []
    assert st.finished is False
    assert st.to_payload()["done"] == 1
    assert st.to_payload()["failed"] == 0


def test_structure_is_never_stored_in_the_run_state():
    run = _run(_chain("a", "b", "c"), name="breadcrumb")
    S.mark(run.run_id, "a", T.TaskStatus.COMPLETED)
    stored = json.loads(
        db.qone("SELECT state FROM exec_workflows WHERE id=?", (run.run_id,))["state"]
        or "{}")
    assert set(stored) <= {"done", "total", "failed", "running", "ready",
                           "source", "schema"}
    assert "levels" not in stored
    assert "nodes" not in stored
    assert stored["done"] == 1
    assert stored["total"] == 3


def test_provenance_survives_the_first_node_transition():
    # `execstate.workflow_step()` REPLACES the state column rather than merging, so
    # `source`/`schema` — written once when the run opened — are carried forward by
    # the one builder or they are gone by the first transition.
    g = M.Graph(name="prov", source="test-file", version=1, nodes=tuple(
        M.make_node(n, seq=i) for i, n in enumerate(_chain("a", "b"))))
    run = S.create(g, chat_id="dag-prov")
    assert run.ok, run.reason
    assert S.load(run.run_id).source == "test-file"
    S.mark(run.run_id, "a", T.TaskStatus.COMPLETED)
    st = S.load(run.run_id)
    assert st.source == "test-file"
    assert st.schema == 1


def test_a_run_records_what_was_declared_so_growth_is_knowable():
    run = _run(_chain("a", "b", "c"), name="declared")
    st = S.load(run.run_id)
    assert st.exists is True
    assert st.declared == 3
    assert st.total == 3
    assert st.growth == 0
    assert st.name == "declared"
    assert st.session_id == run.session_id
    assert st.chat_id == "chat-dag"


# ══════════════════════════════════════════════════════════════════════════════
# 5 · Node state — nine words, all derived
# ══════════════════════════════════════════════════════════════════════════════
def test_the_core_declares_the_nine_states_the_spec_names():
    nine = (M.PENDING, M.READY, M.RUNNING, M.COMPLETED, M.FAILED,
            M.BLOCKED, M.PAUSED, M.CANCELLED, M.SKIPPED)
    assert len(set(nine)) == 9
    assert set(M.STATES) == set(nine)
    assert M.TERMINAL == {M.COMPLETED, M.FAILED, M.CANCELLED, M.SKIPPED}
    assert M.OPEN == {M.PENDING, M.BLOCKED, M.READY, M.RUNNING, M.PAUSED}
    assert M.TERMINAL | M.OPEN == set(M.STATES)
    assert M.UNSUCCESSFUL < M.TERMINAL          # COMPLETED is the only success


@pytest.mark.parametrize("status", sorted(T.ALL_STATUSES))
def test_every_stored_status_projects_onto_one_of_the_nine_states(status):
    run = _run(_chain("a", "b"), name=f"project-{status}")
    T.set_status(S.load(run.run_id).node("a").task_id, status, force=True)
    assert S.load(run.run_id).node("a").state in M.STATES


def test_a_stored_pending_row_is_never_reported_as_pending():
    # READY and BLOCKED are derived on every read. A row says only `pending`; which
    # of the two it *means* is a question about its dependencies, asked now.
    run = _run(_chain("a", "b", "c"), name="derived")
    st = S.load(run.run_id)
    assert {n.status for n in st.nodes} == {T.TaskStatus.PENDING}
    assert M.PENDING not in set(_states(st).values())
    assert _states(st) == {"a": M.READY, "b": M.BLOCKED, "c": M.BLOCKED}
    assert st.node("b").blocked_by == ("a",)
    assert st.node("c").blocked_by == ("b",)
    assert st.node("a").blocked_by == ()


def test_a_held_node_reads_paused_even_though_readiness_still_lists_it():
    # ⚠️ PAUSED is an OPEN status, so `tasks.ready()` returns a paused row. The
    # projection therefore tests `paused` BEFORE readiness — swap those two lines
    # and a held node is handed straight back to a worker.
    run = _run(_chain("a", "b"), name="held")
    tid = S.load(run.run_id).node("a").task_id
    T.pause(tid)
    pool = T.list_tasks(run.session_id)
    assert tid in {t.id for t in T.ready(run.session_id, pool)}
    st = S.load(run.run_id)
    assert st.node("a").state == M.PAUSED
    assert st.node("a").dispatchable is False
    assert st.dispatchable == []
    assert S.resumable(run.run_id) == []


def test_a_claim_is_the_lock_and_the_second_worker_loses():
    run = _run([_node("a"), _node("b")], name="lock")
    assert S.claim(run.run_id, "a", worker_id="w1") is True
    assert S.claim(run.run_id, "a", worker_id="w2") is False
    st = S.load(run.run_id)
    assert st.node("a").status == T.TaskStatus.QUEUED
    assert st.node("a").state == M.READY          # still READY as a graph fact…
    assert st.node("a").dispatchable is False     # …and taken as a dispatch fact
    assert [n.node for n in st.dispatchable] == ["b"]


def test_a_node_claimed_by_a_dead_worker_is_recovered_rather_than_stranded(tmp_path):
    """A QUEUED row is `tasks.HELD`, so worker recovery parks it and the run can end.

    ⚠️ The pin for a defect that produced **no error on any surface**. `claim()`
    writes `pending → queued` *first*, because the status write IS the lock;
    `schedule._start()` marks RUNNING immediately afterwards. A process that dies
    inside that window leaves a row `tasks.heartbeat()` never touches (it writes
    only to a RUNNING row), that no scheduler may re-take (`dispatchable` needs
    PENDING), and that neither `release_interrupted()` nor `unpause_run()` can see
    (both key on PAUSED) — and which projects to **READY**, so the node reads
    healthy while the run sits at N-1/N forever.

    Turns red when `tasks.HELD` narrows back to RUNNING alone: `stale_running()`
    then returns nothing, every assertion below fails, and the graph still reports
    a perfectly healthy READY node.

    ⚠️ The `cwd=` is not decoration. This file's `_clean` fixture drops
    `exec_workflows` only, so QUEUED rows from other tests (the one directly above
    leaves one) outlive it — and a sweep of *this* project would collect them too.
    """
    here = str(tmp_path)
    run = _run([_node("a"), _node("b")], name="crashclaim", chat_id="chat-claim",
               cwd=here)
    assert S.claim(run.run_id, "a", worker_id="dead") is True

    # The window itself: taken from every scheduler, and indistinguishable from a
    # healthy node on the graph surface.
    st = S.load(run.run_id)
    tid = st.node("a").task_id
    assert st.node("a").status == T.TaskStatus.QUEUED
    assert st.node("a").state == M.READY
    assert st.node("a").dispatchable is False
    assert st.interrupted == []
    assert S.release_interrupted(run.run_id) == []

    # Worker recovery sweeps it *because* QUEUED is in `HELD`.
    assert tid in {t.id for t in T.stale_running(cwd=here, older_than=0)}
    entries = CR.recover_workers(cwd=here, older_than=0)
    assert [e["ref_id"] for e in entries] == [tid]

    # …and `interrupt()` parks it with the one thing that tells a crash from a hold.
    st = S.load(run.run_id)
    assert st.node("a").status == T.TaskStatus.PAUSED
    assert st.node("a").state == M.PAUSED
    assert st.node("a").interrupted is True
    assert T.CP_STOPPED in (T.get(tid).checkpoint or {})
    assert [n.node for n in st.interrupted] == ["a"]

    # From there the automatic path — the one `/workflow state` drives — frees it.
    assert [n.node for n in S.release_interrupted(run.run_id)] == ["a"]
    st = S.load(run.run_id)
    assert st.node("a").status == T.TaskStatus.PENDING
    assert st.node("a").dispatchable is True


def test_releasing_a_crash_park_clears_the_mark_so_a_later_hold_survives(tmp_path):
    """Freeing a parked node drops `CP_STOPPED`, or the *next* hold is undone.

    ⚠️ `CP_STOPPED` is the only durable difference between *a crash abandoned this*
    and *a human held this* — `tasks.interrupt()` writes it, `tasks.pause()` writes
    nothing at all, and `_stopped_of()` is its only reader. A release that left the
    mark behind therefore did not fail *now*: the row is PENDING, so nothing is
    parked twice and every existing assertion stays green. It fails the **second**
    time round, when somebody `/pause`s that same node on purpose and the next
    automatic sweep reads PAUSED **and** interrupted and un-pauses a hold a person
    chose — silently, and only in a run that had already crashed once.

    Turns red when the `clear_stopped()` call is dropped from
    `release_interrupted()`: the mark survives, `interrupted` reads True on a
    deliberate hold, and the second release frees a node nobody abandoned.

    ⚠️ The checkpoint assertions are the other half. `clear_stopped()` rewrites with
    `merge=False`, because `merge=True` can only ever *add* a key — so the surviving
    keys are re-seeded from the row, and a bare `merge=False` would blank the resume
    position (`CP_STEPS`) and the node's whole identity (`CP_NODE`, `CP_WORKFLOW`,
    `CP_KIND`, `CP_RESOURCE`), which is a node the graph can no longer find.
    """
    here = str(tmp_path)
    run = _run([_node("a", kind="command", resource="shell"), _node("b")],
               name="stalemark", chat_id="chat-mark", cwd=here)
    tid = S.load(run.run_id).node("a").task_id
    T.record_step(tid, "one")                          # a resume position to keep

    T.start(tid)
    T.interrupt(run.session_id)                        # a dead process parked it
    assert T.CP_STOPPED in (T.get(tid).checkpoint or {})

    assert [n.node for n in S.release_interrupted(run.run_id)] == ["a"]
    cp = T.get(tid).checkpoint or {}
    assert T.CP_STOPPED not in cp
    assert [s["name"] for s in cp[T.CP_STEPS]] == ["one"]   # …and nothing went with it
    assert cp[T.CP_NODE] == "a"
    assert cp[T.CP_WORKFLOW] == run.run_id
    assert cp[T.CP_KIND] == "command"
    assert cp[T.CP_RESOURCE] == "shell"

    # The defect itself: a human holds that same node, afterwards.
    T.pause(tid)
    st = S.load(run.run_id)
    assert st.node("a").state == M.PAUSED
    assert st.node("a").interrupted is False
    assert st.interrupted == []
    assert S.release_interrupted(run.run_id) == []
    assert T.get(tid).status == T.TaskStatus.PAUSED

    # ⚠️ Idempotent means **no write**: `save_checkpoint()` stamps `updated_at`,
    # which is exactly what `stale_running()` measures, so a second call must not
    # make a parked row look freshly touched.
    db.exe("UPDATE agent_tasks SET updated_at=? WHERE id=?", ("2000-01-01 00:00:00", tid))
    assert T.clear_stopped(tid) is not None
    assert T.get(tid).updated_at == "2000-01-01 00:00:00"
    assert T.clear_stopped("no-such-task") is None


def test_a_completed_node_is_never_re_run_and_is_absent_from_a_resume():
    run = _run(_chain("a", "b", "c"), name="norerun")
    S.mark(run.run_id, "a", T.TaskStatus.COMPLETED)
    S.mark(run.run_id, "a", T.TaskStatus.PENDING)      # a re-run attempt
    st = S.load(run.run_id)
    assert st.node("a").state == M.COMPLETED
    assert st.node("a").progress == 1.0
    assert [n.node for n in st.completed] == ["a"]
    assert [n.node for n in S.resumable(run.run_id)] == ["b"]


def test_a_failed_upstream_releases_the_node_and_says_so():
    # ⚠️ Readiness is "every dependency has SETTLED", not "succeeded". So a node
    # whose upstream failed is READY and dispatchable, and `upstream_failed` is the
    # only place a scheduler can learn the difference.
    run = _run(_chain("a", "b"), name="upstream")
    S.mark(run.run_id, "a", T.TaskStatus.FAILED, error="boom")
    st = S.load(run.run_id)
    assert st.node("a").state == M.FAILED
    b = st.node("b")
    assert b.state == M.READY
    assert b.dispatchable is True
    assert b.upstream_failed == ("a",)
    assert b.blocked_by == ()
    assert [n.node for n in st.failed] == ["a"]


def test_no_node_instruction_ever_reaches_a_payload():
    run = _run([_node("a", instruction=SECRET_NOTE), _node("b")], name="secret")
    st = S.load(run.run_id)
    assert st.node("a").instruction == SECRET_NOTE     # the worker still gets it
    assert SECRET_NOTE not in json.dumps(st.to_payload())
    assert SECRET_NOTE not in json.dumps(st.node("a").to_payload())
    assert SECRET_NOTE not in json.dumps(run.to_payload())
    assert SECRET_NOTE not in (
        db.qone("SELECT state FROM exec_workflows WHERE id=?", (run.run_id,))["state"]
        or "")


def test_a_task_row_without_a_node_id_is_not_a_graph_node():
    run = _run(_chain("a", "b"), name="foreign")
    T.create(run.session_id, "a plain todo row in the same session")
    st = S.load(run.run_id)
    assert [n.node for n in st.nodes] == ["a", "b"]
    assert st.total == 2


# ══════════════════════════════════════════════════════════════════════════════
# 6 · Runtime mutation
# ══════════════════════════════════════════════════════════════════════════════
def test_extending_a_run_adds_work_without_touching_what_finished():
    run = _run(_chain("a", "b"), name="grow")
    S.mark(run.run_id, "a", T.TaskStatus.COMPLETED)
    a_task = S.load(run.run_id).node("a").task_id

    out = S.extend(run.run_id, [_node("c", needs=["b"])])
    assert out.ok, out.reason
    assert out.run_id == run.run_id
    assert out.session_id == run.session_id
    assert set(out.node_tasks) == {"a", "b", "c"}
    assert out.node_tasks["a"] == a_task          # the same row, not a new one

    st = S.load(run.run_id)
    assert st.total == 3
    assert st.declared == 2
    assert st.growth == 1
    assert _states(st) == {"a": M.COMPLETED, "b": M.READY, "c": M.BLOCKED}
    assert st.node("c").seq == 2                  # appended, never renumbering


def test_a_new_node_may_depend_on_one_that_has_already_finished():
    run = _run(_chain("a", "b"), name="late")
    S.mark(run.run_id, "a", T.TaskStatus.COMPLETED)
    out = S.extend(run.run_id, [_node("c", needs=["a"])])
    assert out.ok, out.reason
    st = S.load(run.run_id)
    assert st.node("c").needs == ("a",)
    assert st.node("c").state == M.READY          # its dependency already settled


def test_a_cycle_introduced_at_runtime_is_refused_before_anything_is_written():
    run = _run(_chain("a", "b", "c"), name="ring")
    before = _counts()
    out = S.extend(run.run_id, [], edges=[("c", "a")])
    assert out.ok is False
    assert out.validation.codes() == (M.P_CYCLE,)
    assert "→" in out.reason
    assert _counts() == before
    assert _states(S.load(run.run_id)) == {"a": M.READY, "b": M.BLOCKED,
                                           "c": M.BLOCKED}


def test_a_settled_node_may_not_be_removed():
    run = _run(_chain("a", "b", "c"), name="keep")
    S.mark(run.run_id, "a", T.TaskStatus.COMPLETED)
    st = S.load(run.run_id)
    v = validate_mutation(st.graph(), st.graph().without_nodes(["a"]),
                          settled=st.settled_ids)
    assert v.ok is False
    assert v.codes() == (M.P_LOST_NODE,)
    assert v.problems[0]["node"] == "a"


def test_a_settled_node_may_not_be_rewired():
    run = _run([_node("a"), _node("b", needs=["a"]), _node("c", needs=["b"]),
                _node("d")], name="rewire")
    S.mark(run.run_id, "a", T.TaskStatus.COMPLETED)
    S.mark(run.run_id, "b", T.TaskStatus.COMPLETED)
    before = _counts()
    out = S.extend(run.run_id, [], edges=[("d", "b")])
    assert out.ok is False
    assert out.validation.codes() == (M.P_REWIRED,)
    assert out.validation.problems[0]["node"] == "b"
    assert _counts() == before


def test_growth_is_bounded_and_the_refusal_names_its_knob(monkeypatch):
    # A re-planning loop does not crash — it grows one node at a time and never
    # finishes, and nothing else in the system would call that an error.
    run = _run(_chain("a", "b"), name="bounded")
    monkeypatch.setattr(cfg, "DAG_MAX_MUTATIONS", 0)
    before = _counts()
    out = S.extend(run.run_id, [_node("c", needs=["b"])])
    assert out.ok is False
    assert out.validation.codes() == (M.P_NO_GROWTH,)
    assert "AGENT2_DAG_MAX_MUTATIONS" in out.reason
    assert out.validation.problems[0]["growth"] == 1
    assert out.validation.problems[0]["limit"] == 0
    assert _counts() == before

    monkeypatch.setattr(cfg, "DAG_MAX_MUTATIONS", 4)      # read live, not latched
    assert S.extend(run.run_id, [_node("c", needs=["b"])]).ok is True


def test_a_finished_run_refuses_to_grow():
    run = _run(_chain("a", "b"), name="closed")
    S.mark(run.run_id, "a", T.TaskStatus.COMPLETED)
    S.mark(run.run_id, "b", T.TaskStatus.COMPLETED)
    assert S.load(run.run_id).finished is True
    before = _counts()
    out = S.extend(run.run_id, [_node("c")])
    assert out.ok is False
    assert "already finished" in out.reason
    assert out.run_id == run.run_id
    assert _counts() == before


def test_an_edge_between_two_pending_nodes_is_a_dependency_rewrite():
    run = _run([_node("a"), _node("b")], name="wire")
    assert len(S.load(run.run_id).dispatchable) == 2
    before = _counts()
    out = S.extend(run.run_id, [], edges=[("a", "b")])
    assert out.ok, out.reason
    assert _counts() == before                    # an edge is not a row
    st = S.load(run.run_id)
    assert st.node("b").needs == ("a",)
    assert _states(st) == {"a": M.READY, "b": M.BLOCKED}
    assert [n.node for n in st.dispatchable] == ["a"]
    assert st.growth == 0


def test_extending_with_nothing_new_is_a_no_op_that_still_maps_the_run():
    run = _run(_chain("a", "b"), name="noop")
    before = _counts()
    out = S.extend(run.run_id, [])
    assert out.ok, out.reason
    assert set(out.node_tasks) == {"a", "b"}
    assert _counts() == before
    again = S.extend(run.run_id, [_node("a")])    # a re-declaration, not a new node
    assert again.ok, again.reason
    assert _counts() == before
    assert S.load(run.run_id).total == 2


def test_an_unknown_run_cannot_be_grown():
    assert S.extend("no-such-run", [_node("x")]).reason == "no such graph run"
    assert S.extend("no-such-run", [_node("x")], label="workflow").reason == \
        "no such workflow run"


# ══════════════════════════════════════════════════════════════════════════════
# 7 · Recovery
# ══════════════════════════════════════════════════════════════════════════════
def test_the_graph_is_reconstructed_from_the_rows_alone():
    declared = _graph(name="rebuild", nodes=[
        _node("analyze", kind="tool", resource="repo"),
        _node("deps", needs=["analyze"], priority=2),
        _node("tests", needs=["analyze"], priority=7),
        _node("report", needs=["deps", "tests"]),
    ])
    run = S.create(declared, chat_id="dag-rebuild")
    assert run.ok, run.reason
    S.mark(run.run_id, "analyze", T.TaskStatus.COMPLETED)

    rebuilt = S.load(run.run_id).graph()      # a fresh process has only the rows
    assert set(rebuilt.ids()) == set(declared.ids())
    assert rebuilt.edge_map() == declared.edge_map()
    assert rebuilt.node("analyze").kind == "tool"
    assert rebuilt.node("analyze").resource == "repo"
    assert rebuilt.node("deps").priority == 2
    assert rebuilt.node("tests").priority == 7
    assert validate(rebuilt).ok is True


def test_a_resume_picks_up_only_what_was_in_flight_or_next():
    run = _run([_node("a"), _node("b"), _node("c", needs=["a", "b"]),
                _node("d", needs=["c"])], name="resume")
    S.mark(run.run_id, "a", T.TaskStatus.COMPLETED)
    assert S.claim(run.run_id, "b") is True
    S.mark(run.run_id, "b", T.TaskStatus.RUNNING)
    st = S.load(run.run_id)
    assert _states(st) == {"a": M.COMPLETED, "b": M.RUNNING,
                           "c": M.BLOCKED, "d": M.BLOCKED}
    assert [n.node for n in S.resumable(run.run_id)] == ["b"]
    S.mark(run.run_id, "b", T.TaskStatus.COMPLETED)
    assert [n.node for n in S.resumable(run.run_id)] == ["c"]


def test_an_unknown_run_reads_as_absent_and_never_as_finished():
    # `finished` on an empty state must be False by construction: absent and
    # "every node is done" are the same shape and opposite facts.
    st = S.load("no-such-run")
    assert st.exists is False
    assert st.finished is False
    assert st.nodes == []
    assert st.total == 0
    assert st.done == 0
    assert st.current() is None
    assert st.settled_ids == ()
    assert S.load("").exists is False
    assert S.resumable("no-such-run") == []
    before = _counts()
    assert S.settle("no-such-run").exists is False
    assert _counts() == before


# ══════════════════════════════════════════════════════════════════════════════
# 8 · Cancellation, pause and resume
# ══════════════════════════════════════════════════════════════════════════════
def test_cancelling_a_run_leaves_completed_work_and_settles_the_run():
    run = _run([_node("a"), _node("b"), _node("c", needs=["a"])], name="cancel")
    S.mark(run.run_id, "a", T.TaskStatus.COMPLETED)
    st = S.cancel_run(run.run_id, reason="user asked")
    assert st.node("a").state == M.COMPLETED      # not rewritten, not re-run
    assert st.node("b").state == M.CANCELLED
    assert st.node("c").state == M.CANCELLED
    assert st.finished is True
    assert T.ready(run.session_id) == []
    row = X.workflow(run.run_id)
    assert row["status"] == T.STEP_FAILED
    assert row["completed_at"]


def test_pausing_a_run_holds_what_has_not_started_and_leaves_a_runner_alone():
    run = _run([_node("a"), _node("b"), _node("c", needs=["b"])], name="hold")
    assert S.claim(run.run_id, "a") is True
    S.mark(run.run_id, "a", T.TaskStatus.RUNNING)
    st = S.pause_run(run.run_id, reason="user asked")
    assert st.node("a").state == M.RUNNING        # tearing down a live node is
    assert st.node("b").state == M.PAUSED        # a destructive act nobody asked
    assert st.node("c").state == M.PAUSED        # for — pause gates STARTING
    assert st.finished is False
    assert st.dispatchable == []
    assert [n.node for n in S.resumable(run.run_id)] == ["a"]


def test_a_paused_node_can_be_neither_dispatched_nor_claimed():
    run = _run([_node("a"), _node("b")], name="frozen")
    S.pause_run(run.run_id)
    st = S.load(run.run_id)
    assert _states(st) == {"a": M.PAUSED, "b": M.PAUSED}
    assert st.dispatchable == []
    assert S.resumable(run.run_id) == []
    assert S.claim(run.run_id, "a") is False
    assert S.load(run.run_id).node("a").state == M.PAUSED


def test_unpausing_re_derives_readiness_rather_than_restoring_a_snapshot():
    run = _run(_chain("a", "b", "c"), name="rederive")
    S.pause_run(run.run_id)
    assert set(_states(S.load(run.run_id)).values()) == {M.PAUSED}
    S.mark(run.run_id, "a", T.TaskStatus.FAILED, error="boom")
    st = S.unpause_run(run.run_id)
    assert st.node("a").state == M.FAILED
    assert st.node("b").state == M.READY          # released by a SETTLED upstream
    assert st.node("b").upstream_failed == ("a",)
    assert st.node("c").state == M.BLOCKED
    assert [n.node for n in st.dispatchable] == ["b"]


def test_pause_is_not_cancel():
    run = _run([_node("a"), _node("b")], name="notcancel")
    S.pause_run(run.run_id)
    row = X.workflow(run.run_id)
    assert row["status"] not in (T.STEP_COMPLETED, T.STEP_FAILED)
    assert not row["completed_at"]
    st = S.unpause_run(run.run_id)
    assert _states(st) == {"a": M.READY, "b": M.READY}
    assert len(st.dispatchable) == 2


# ══════════════════════════════════════════════════════════════════════════════
# 9 · Scheduling — what may run now, and what held the rest (Phase D2)
# ══════════════════════════════════════════════════════════════════════════════
#: Every wait in this section is bounded, so a broken pump fails the test rather
#: than hanging the suite. Generous because CI is slow, never because it is a timer.
_WAIT = 10.0


class _Recorder:
    """A worker that remembers what it was asked to do. `fn` is the real work.

    Records `(node, attempt)` per call, so "never started twice" and "the retry knew
    it was a retry" are both read off one list.
    """

    def __init__(self, fn=None):
        self.fn = fn
        self.lock = threading.Lock()
        self.calls: list[tuple[str, int]] = []

    def __call__(self, ctx):
        with self.lock:
            self.calls.append((ctx.node, ctx.attempt))
        return self.fn(ctx) if self.fn is not None else None

    @property
    def nodes(self) -> list[str]:
        return [n for n, _ in self.calls]

    @property
    def attempts(self) -> list[int]:
        return [a for _, a in self.calls]


def _codes(plan_or_outcome) -> list[str]:
    return [str(h.get("code") or "") for h in plan_or_outcome.held]


# ── D2.11 · readiness has ONE declaration ─────────────────────────────────────
def test_the_scheduler_derives_no_readiness_of_its_own():
    """⚠️ `tasks.ready()` through the store projection, or the answers disagree.

    A second predicate here would be a second definition of "may this run now" — and
    the two would disagree silently, because each looks right alone. Asserted with
    `ast` rather than by reading the docstring: the module may not *call* readiness.
    """
    tree = ast.parse(Path(SC.__file__).read_text(encoding="utf-8"))
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            called.add(fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", ""))
    assert "ready" not in called, "readiness is the store's projection, never re-derived"
    assert "blockers" not in called, "so is what blocks a node"


def test_the_plan_is_a_subset_of_what_the_graph_offered():
    run = _run([_node("a"), _node("b"), _node("c", needs=["a"])], name="subset")
    st = S.load(run.run_id)
    offered = {n.node for n in st.dispatchable}
    assert offered == {"a", "b"}                       # c is BLOCKED, so never offered
    plan = SC.plan_next(st, caps=SC.Limits(max_workers=4, max_running=8))
    assert {s.node for s in plan.slots} <= offered
    assert "c" not in _codes(plan) and all(h["node"] != "c" for h in plan.held)


# ── D2.12 · priority, then sequence ───────────────────────────────────────────
def test_the_plan_takes_priority_first_and_declaration_order_to_break_a_tie():
    run = _run([_node("low", priority=9), _node("high", priority=1),
                _node("mid", priority=5), _node("mid2", priority=5)], name="prio")
    plan = SC.plan_next(S.load(run.run_id),
                        caps=SC.Limits(max_workers=8, max_running=8))
    assert [s.node for s in plan.slots] == ["high", "mid", "mid2", "low"]
    assert [s.priority for s in plan.slots] == [1, 5, 5, 9]
    # ⚠️ `seq` is a node's place in the VALIDATED order, not its line in the file:
    # `levels_for()` applied `(priority, seq)` when the rows were written, so one key
    # orders both sides and a plan's seqs come out ascending rather than shuffled.
    assert [s.seq for s in plan.slots] == [0, 1, 2, 3]
    # And with nothing to separate them, the tie is the order they were declared in —
    # never the order they happen to sort in as text.
    run2 = _run([_node("zulu"), _node("yankee"), _node("xray")], name="tie")
    plan2 = SC.plan_next(S.load(run2.run_id),
                         caps=SC.Limits(max_workers=8, max_running=8))
    assert [s.node for s in plan2.slots] == ["zulu", "yankee", "xray"]


# ── D2.13 · every ceiling, and each one names itself ──────────────────────────
def test_the_running_ceiling_holds_the_rest_and_names_itself():
    run = _run([_node("a"), _node("b"), _node("c")], name="running")
    plan = SC.plan_next(S.load(run.run_id),
                        caps=SC.Limits(max_workers=8, max_running=1))
    assert len(plan.slots) == 1
    assert _codes(plan) == [SC.H_RUNNING, SC.H_RUNNING]
    assert plan.caps.max_running == 1


def test_the_worker_ceiling_is_a_different_hold_from_the_running_one():
    """Two ceilings, two words. One code for both would make a full pool and a full
    install indistinguishable — and only one of them is this process's to widen."""
    run = _run([_node("a"), _node("b"), _node("c")], name="workers")
    plan = SC.plan_next(S.load(run.run_id),
                        caps=SC.Limits(max_workers=8, max_running=8), workers=1)
    assert len(plan.slots) == 1
    assert _codes(plan) == [SC.H_WORKERS, SC.H_WORKERS]
    assert plan.reason == "" and bool(plan) is True     # something ran; it is not a stall


def test_a_per_kind_ceiling_holds_only_its_own_kind():
    run = _run([_node("c1", kind="command"), _node("c2", kind="command"),
                _node("m1", kind="mcp"), _node("t1", kind="tool")], name="kinds")
    plan = SC.plan_next(S.load(run.run_id),
                        caps=SC.Limits(max_workers=8, max_running=8,
                                       kind_limits={"command": 1, "mcp": 1}))
    assert {s.node for s in plan.slots} == {"c1", "m1", "t1"}
    assert [(h["node"], h["code"], h["kind"]) for h in plan.held] == \
           [("c2", SC.H_KIND, "command")]
    assert "1/1" in plan.held[0]["detail"]              # the ceiling is in the reason


def test_a_per_kind_lifetime_budget_is_spent_by_attempts_not_by_a_counter():
    """⚠️ `max_model_calls` is a *budget*, so it is charged from `attempt_count` —
    a retry really is a second call, and a crash-resumed run gets no fresh allowance."""
    run = _run([_node("x", kind="llm"), _node("y", kind="llm")], name="budget")
    assert SC.spend(S.load(run.run_id)) == {}
    assert S.claim(run.run_id, "x", worker_id="probe") is True
    S.mark(run.run_id, "x", M.RUNNING, advance_run=False)
    st = S.load(run.run_id)
    assert SC.spend(st) == {"llm": 1}
    plan = SC.plan_next(st, caps=SC.Limits(max_workers=8, max_running=8,
                                           kind_budgets={"llm": 1}))
    assert plan.slots == []
    assert [(h["node"], h["code"]) for h in plan.held] == [("y", SC.H_BUDGET)]
    assert plan.reason == SC.H_BUDGET                   # nothing ran, and this is why


# ── D2.14 / D2.18 · resources, conflicts, and in-flight ───────────────────────
def test_two_nodes_naming_one_resource_are_never_planned_together():
    run = _run([_node("w1", resource="repo"), _node("w2", resource="repo"),
                _node("w3", resource="db"), _node("w4")], name="resource")
    caps = SC.Limits(max_workers=8, max_running=8)
    plan = SC.plan_next(S.load(run.run_id), caps=caps)
    assert {s.node for s in plan.slots} == {"w1", "w3", "w4"}
    assert [(h["node"], h["code"], h["resource"]) for h in plan.held] == \
           [("w2", SC.H_RESOURCE, "repo")]
    # And the exclusion holds against a row somebody else is already holding.
    assert S.claim(run.run_id, "w2", worker_id="other") is True
    S.mark(run.run_id, "w2", M.RUNNING, advance_run=False)
    again = SC.plan_next(S.load(run.run_id), caps=caps)
    assert "w1" not in [s.node for s in again.slots]
    assert (SC.H_RESOURCE, "w1") in [(h["code"], h["node"]) for h in again.held]


def test_in_flight_is_derived_from_the_rows_never_counted_in_memory():
    """A claimed row reads READY and loses `dispatchable`; a RUNNING row is obvious.
    Counting in memory would give each process of a dual install a full allowance."""
    run = _run([_node("a"), _node("b")], name="inflight")
    assert SC.in_flight(S.load(run.run_id)) == []
    assert S.claim(run.run_id, "a", worker_id="w") is True
    st = S.load(run.run_id)
    assert [n.node for n in SC.in_flight(st)] == ["a"]
    assert st.node("a").state == M.READY and st.node("a").dispatchable is False
    S.mark(run.run_id, "a", M.RUNNING, advance_run=False)
    assert [n.node for n in SC.in_flight(S.load(run.run_id))] == ["a"]
    S.mark(run.run_id, "a", M.COMPLETED)
    assert SC.in_flight(S.load(run.run_id)) == []


def test_a_row_lost_between_planning_and_claiming_is_reported_as_claimed():
    """⚠️ The loser of a race needs to know it lost — a dropped slot is a node the
    other worker is running while this one reports it never happened."""
    run = _run([_node("a")], name="race")
    stale = S.load(run.run_id)                    # the node looks free in here
    caps = SC.Limits(max_workers=1, max_running=8)
    first = SC.dispatch(run.run_id, caps=caps, state=stale, workers=1)
    assert [s.node for s in first.slots] == ["a"]
    second = SC.dispatch(run.run_id, caps=caps, state=stale, workers=1)
    assert second.slots == []
    assert [(h["node"], h["code"]) for h in second.held] == [("a", SC.H_CLAIMED)]
    assert second.reason == SC.H_CLAIMED


def test_every_held_node_carries_exactly_one_reason_from_the_closed_vocabulary():
    """A node in the graph, absent from the plan, with no stated reason is
    indistinguishable from a broken planner — `select.REASONS`' rule, node-shaped."""
    run = _run([_node("c1", kind="command", resource="repo"),
                _node("c2", kind="command", resource="repo"),
                _node("t1", kind="tool"), _node("t2", kind="tool")], name="reasons")
    st = S.load(run.run_id)
    plan = SC.plan_next(st, caps=SC.Limits(max_workers=8, max_running=3,
                                           kind_limits={"command": 1}))
    offered = {n.node for n in st.dispatchable}
    taken = {s.node for s in plan.slots}
    held = [h["node"] for h in plan.held]
    assert taken | set(held) == offered                 # nobody is silently missing
    assert len(held) == len(set(held))                  # exactly one reason each
    assert set(_codes(plan)) <= set(SC.HOLD_CODES)
    assert plan.codes and tuple(dict.fromkeys(_codes(plan))) == plan.codes
    assert SECRET_NOTE not in json.dumps(plan.to_payload())


# ── D2.11 (the surprise) · a failed upstream is not work ──────────────────────
def test_a_node_behind_a_failure_is_declined_and_skipped_never_run():
    """`tasks.ready()` cannot tell success from settlement, so the graph offers this
    node. Declining it is the scheduler's job, and SKIPPED is what stops the run
    holding itself open forever on a branch that will never be work."""
    run = _run([_node("a"), _node("b", needs=["a"]), _node("c", needs=["b"])],
               name="upstream")
    rec = _Recorder(lambda ctx: False if ctx.node == "a" else None)
    events: list[tuple[str, dict]] = []
    out = SC.run(run.run_id, rec,
                 caps=SC.Limits(max_workers=2, max_running=2, max_attempts=1),
                 on_event=lambda ev, p: events.append((ev, p)))
    assert rec.nodes == ["a"]                           # b and c never ran
    assert out.skipped == 2 and out.failed == 1 and out.ok is False
    assert out.reason == SC.R_FINISHED                  # settled, just not successfully
    assert _states(S.load(run.run_id)) == {"a": M.FAILED, "b": M.SKIPPED, "c": M.SKIPPED}
    assert "upstream failed" in (S.load(run.run_id).node("b").error or "")
    assert SC.EV_SKIP in [ev for ev, _ in events]


def test_a_best_effort_graph_still_runs_the_node_behind_a_failure():
    """⚠️ A consumer's decision, not an operator's — hence a `Limits` field and no
    environment knob. `False` means "give me every node the graph offers"."""
    run = _run([_node("a"), _node("b", needs=["a"])], name="besteffort")
    rec = _Recorder(lambda ctx: False if ctx.node == "a" else None)
    out = SC.run(run.run_id, rec,
                 caps=SC.Limits(max_workers=1, max_running=1, max_attempts=1,
                                skip_failed_upstream=False))
    assert rec.nodes == ["a", "b"]
    assert out.skipped == 0
    assert _states(S.load(run.run_id)) == {"a": M.FAILED, "b": M.COMPLETED}


# ── D2.15 · retries and timeouts: bounded AND classified ──────────────────────
def test_a_retry_is_bounded_and_the_attempt_is_visible_to_the_worker():
    run = _run([_node("flaky")], name="retry")
    rec = _Recorder(lambda ctx: None if ctx.attempt > 1 else False)
    events: list[tuple[str, dict]] = []
    out = SC.run(run.run_id, rec,
                 caps=SC.Limits(max_workers=1, max_running=1, max_attempts=2),
                 on_event=lambda ev, p: events.append((ev, p)))
    assert rec.calls == [("flaky", 1), ("flaky", 2)]
    assert out.retried == 1 and out.completed == 1 and out.ok is True
    assert S.load(run.run_id).node("flaky").state == M.COMPLETED
    why = [p.get("why") for ev, p in events if ev == SC.EV_RETRY]
    assert why and why[0], "a retry states its reason — never blind"


def test_the_attempt_ceiling_ends_the_node_rather_than_parking_it():
    run = _run([_node("doomed")], name="ceiling")
    rec = _Recorder(lambda ctx: False)
    out = SC.run(run.run_id, rec,
                 caps=SC.Limits(max_workers=1, max_running=1, max_attempts=2))
    assert rec.attempts == [1, 2]                       # bounded: never a third
    assert out.retried == 1 and out.failed == 1 and out.ok is False
    assert S.load(run.run_id).node("doomed").state == M.FAILED


def test_a_retry_the_classifier_refuses_becomes_a_failure_carrying_the_reason(monkeypatch):
    """⚠️ A refused retry may never be a row parked PENDING for a pump that has
    stopped counting — that is a run which reads live forever with nobody watching."""
    run = _run([_node("blocked")], name="refused")
    monkeypatch.setattr(SC, "may_repeat", lambda tid: (False, "the capability is gone"))
    rec = _Recorder(lambda ctx: False)
    out = SC.run(run.run_id, rec,
                 caps=SC.Limits(max_workers=1, max_running=1, max_attempts=5))
    assert rec.attempts == [1]
    assert out.retried == 0 and out.failed == 1
    view = S.load(run.run_id).node("blocked")
    assert view.state == M.FAILED
    assert "not repeatable" in view.error and "capability is gone" in view.error


def test_may_repeat_fails_closed_on_a_row_that_is_not_there():
    assert SC.may_repeat("") == (False, "no task row")
    assert SC.may_repeat("no-such-task") == (False, "no task row")


def test_safe_to_continue_accepts_a_resume_that_safe_to_repeat_still_refuses():
    """⚠️ THE PAIR THAT MADE THE RETRY PATH DEAD ONCE. An open node row classifies
    `R_SAFE_TO_RESUME` — the checkpoint *is* the resume point — and `safe_to_repeat()`
    refuses that class by design (pinned in `test_crashrecovery.py`). A scheduler that
    asked the narrower predicate would therefore be structurally unable to retry
    anything, ever: a bounded retry that reads as implemented and cannot fire once.
    """
    from agent2.core.recovery import classify as CL
    run = _run([_node("a")], name="classify")
    tid = S.load(run.run_id).node("a").task_id
    assert S.claim(run.run_id, "a", worker_id="w") is True
    S.mark(run.run_id, "a", M.RUNNING, advance_run=False)
    found = CL.assess(CL.K_TASK, T.get(tid))
    assert found.classification == CL.R_SAFE_TO_RESUME
    assert CL.safe_to_repeat(found) is False            # unchanged, and still pinned
    assert CL.safe_to_continue(found) is True           # the scheduler's question
    assert SC.may_repeat(tid)[0] is True


def test_a_timeout_records_the_verdict_before_it_signals_the_worker():
    """⚠️ A Python thread cannot be killed, so the row settles FIRST and the worker
    is told second. The other order leaves the verdict waiting on a worker that may
    never look — and `commands.py`'s record-then-kill exists for the same reason."""
    run = _run([_node("slow")], name="deadline")
    seen: dict = {}
    woke = threading.Event()

    def worker(ctx):
        ctx.cancel.wait(_WAIT)
        seen["cancelled"] = ctx.cancelled()
        seen["state"] = S.load(ctx.run_id).node("slow").state
        woke.set()
        return "too late"

    events: list[str] = []
    out = SC.run(run.run_id, worker,
                 caps=SC.Limits(max_workers=1, max_running=1, max_attempts=1,
                                node_timeout=0.05),
                 on_event=lambda ev, p: events.append(ev))
    assert woke.wait(_WAIT), "the worker was never signalled"
    assert out.timed_out == 1 and out.ok is False
    assert SC.EV_TIMEOUT in events
    view = S.load(run.run_id).node("slow")
    assert view.state == M.FAILED and "timed out" in view.error
    assert seen["cancelled"] is True
    assert seen["state"] == M.FAILED                    # already recorded when told


# ── D2.16 · cancellation ──────────────────────────────────────────────────────
def test_a_cancel_records_the_run_before_it_signals_the_worker():
    run = _run([_node("a")], name="cancelrun")
    stop = threading.Event()
    seen: dict = {}
    woke = threading.Event()

    def worker(ctx):
        stop.set()                                      # ask from inside a node
        ctx.cancel.wait(_WAIT)
        seen["state"] = S.load(ctx.run_id).node("a").state
        seen["ledger"] = (X.workflow(ctx.run_id) or {}).get("status")
        woke.set()
        return None

    out = SC.run(run.run_id, worker, cancel=stop,
                 caps=SC.Limits(max_workers=1, max_running=1, max_attempts=1))
    assert woke.wait(_WAIT), "the worker was never signalled"
    assert out.reason == SC.R_CANCELLED and out.ok is False
    assert seen["state"] == M.CANCELLED                 # recorded, then killed
    assert seen["ledger"]


def test_a_pre_cancelled_run_never_calls_the_worker():
    run = _run([_node("a"), _node("b")], name="precancel")
    stop = threading.Event()
    stop.set()
    rec = _Recorder()
    out = SC.run(run.run_id, rec, cancel=stop,
                 caps=SC.Limits(max_workers=2, max_running=2, max_attempts=1))
    assert rec.calls == [] and out.dispatched == 0
    assert out.reason == SC.R_CANCELLED
    assert _states(S.load(run.run_id)) == {"a": M.CANCELLED, "b": M.CANCELLED}


def test_cancelling_leaves_completed_work_completed():
    """*"Completed work stays completed"* — the cancel path may not rewrite the past."""
    run = _run([_node("a"), _node("b", needs=["a"])], name="cancelkeep")
    S.mark(run.run_id, "a", M.COMPLETED, result="done")
    stop = threading.Event()
    stop.set()
    out = SC.run(run.run_id, _Recorder(), cancel=stop,
                 caps=SC.Limits(max_workers=1, max_running=1, max_attempts=1))
    assert out.reason == SC.R_CANCELLED
    assert _states(S.load(run.run_id)) == {"a": M.COMPLETED, "b": M.CANCELLED}


# ── D2.17 · the DAG's own pause ───────────────────────────────────────────────
def test_a_paused_graph_holds_and_the_worker_is_never_called():
    """⚠️ `/pause` is a chat control; this is the graph's. `tasks.ready()` still lists
    a PAUSED row, so the hold has to gate *starting* the node — which is exactly what
    the projection's paused-before-readiness order buys the scheduler."""
    run = _run([_node("a"), _node("b")], name="heldrun")
    S.pause_run(run.run_id)
    rec = _Recorder()
    caps = SC.Limits(max_workers=2, max_running=2, max_attempts=1)
    out = SC.run(run.run_id, rec, caps=caps)
    assert out.reason == SC.R_PAUSED
    assert rec.calls == [] and out.dispatched == 0
    assert not S.load(run.run_id).finished          # a hold is not a verdict
    S.unpause_run(run.run_id)
    again = SC.run(run.run_id, rec, caps=caps)
    assert again.ok is True and again.reason == SC.R_FINISHED
    assert sorted(rec.nodes) == ["a", "b"]


# ── Bounded by construction, and total ────────────────────────────────────────
def test_inline_workers_run_one_node_at_a_time_on_the_calling_thread():
    """`max_workers=0` is a supported answer, not a broken one: `core/scheduler.py`'s
    DISABLED degradation, node-shaped. Observed from the ROWS, never from a clock."""
    run = _run([_node("a"), _node("b"), _node("c")], name="inline")
    caps = SC.Limits(max_workers=0, max_running=8, max_attempts=1)
    assert caps.inline is True and caps.width == 1
    here = threading.current_thread().name
    seen: list[tuple[int, str]] = []

    def worker(ctx):
        seen.append((len(SC.in_flight(S.load(ctx.run_id))),
                     threading.current_thread().name))
        return None

    out = SC.run(run.run_id, worker, caps=caps)
    assert out.ok is True and out.completed == 3
    assert [n for n, _ in seen] == [1, 1, 1]
    assert {t for _, t in seen} == {here}


def test_a_worker_that_raises_is_one_failed_node_and_never_an_exception():
    run = _run([_node("a"), _node("b", needs=["a"])], name="boom")

    def worker(ctx):
        raise RuntimeError("worker exploded")

    out = SC.run(run.run_id, worker,
                 caps=SC.Limits(max_workers=1, max_running=1, max_attempts=1))
    assert out.ok is False and out.failed == 1 and out.skipped == 1
    view = S.load(run.run_id).node("a")
    assert view.state == M.FAILED
    assert view.error.startswith("RuntimeError: worker exploded")


def test_run_refuses_a_missing_run_by_returning():
    rec = _Recorder()
    out = SC.run("no-such-run", rec, caps=SC.Limits(max_workers=1, max_attempts=1))
    assert out.reason == SC.R_NO_RUN and out.ok is False
    assert rec.calls == [] and out.state is not None and out.state.exists is False


def test_a_bounded_diamond_finishes_and_never_exceeds_its_ceiling():
    run = _run([_node("a"), _node("b", needs=["a"]), _node("c", needs=["a"]),
                _node("d", needs=["b", "c"])], name="diamond")
    caps = SC.Limits(max_workers=2, max_running=2, max_attempts=1)
    lock = threading.Lock()
    seen: list[int] = []
    order: list[str] = []

    def worker(ctx):
        with lock:
            seen.append(len(SC.in_flight(S.load(ctx.run_id))))
            order.append(ctx.node)
        time.sleep(0.01)
        return f"{ctx.node} done"

    out = SC.run(run.run_id, worker, caps=caps)
    assert out.ok is True and out.reason == SC.R_FINISHED
    assert out.completed == 4 and out.failed == 0 and out.dispatched == 4
    assert _states(S.load(run.run_id)) == dict.fromkeys(("a", "b", "c", "d"), M.COMPLETED)
    assert order[0] == "a" and order[-1] == "d"
    assert max(seen) <= caps.width, f"more in flight than the ceiling allows: {seen}"
    assert X.workflow(run.run_id)["status"] == T.STEP_COMPLETED


def test_the_events_are_a_closed_vocabulary_and_the_run_always_ends_with_one():
    run = _run([_node("a"), _node("b", needs=["a"])], name="events")
    rec = _Recorder(lambda ctx: False if (ctx.node == "a" and ctx.attempt == 1) else None)
    seen: list[str] = []
    out = SC.run(run.run_id, rec,
                 caps=SC.Limits(max_workers=1, max_running=1, max_attempts=2),
                 on_event=lambda ev, p: seen.append(ev))
    assert out.ok is True
    assert set(seen) <= set(SC.EVENTS)
    assert {SC.EV_DISPATCH, SC.EV_RETRY, SC.EV_SETTLE} <= set(seen)
    assert seen[-1] == SC.EV_DONE


def test_a_broken_listener_never_costs_the_run():
    """Presentation may not raise into execution — `diffs.py`'s rule, pump-shaped."""
    run = _run([_node("a")], name="listener")

    def boom(ev, payload):
        raise RuntimeError("listener exploded")

    out = SC.run(run.run_id, _Recorder(), on_event=boom,
                 caps=SC.Limits(max_workers=1, max_running=1, max_attempts=1))
    assert out.ok is True and out.completed == 1


def test_outcome_of_reads_a_worker_that_says_nothing_as_success():
    """A worker may say anything; a node's status is a closed vocabulary regardless."""
    assert SC.outcome_of(None).status == M.COMPLETED
    assert SC.outcome_of("all good").result == "all good"
    assert SC.outcome_of(True).status == M.COMPLETED
    assert SC.outcome_of(False).status == M.FAILED
    assert SC.outcome_of({"status": M.FAILED, "error": "nope"}).error == "nope"
    assert SC.outcome_of({"status": "invented"}).status == M.COMPLETED
    assert SC.outcome_of({"progress": "junk"}).progress is None
    assert SC.outcome_of(SC.NodeOutcome(status=M.SKIPPED)).status == M.SKIPPED
    assert SC.outcome_of({"status": M.RUNNING}).status == M.COMPLETED   # not reportable


def test_an_instruction_reaches_the_worker_and_reaches_no_payload():
    run = _run([_node("a", instruction=SECRET_NOTE)], name="quiet")
    seen: dict = {}

    def worker(ctx):
        seen["instruction"] = ctx.instruction
        seen["kind"] = ctx.kind
        seen["remaining"] = ctx.remaining()
        ctx.progress(0.5)
        ctx.beat()
        return "ok"

    out = SC.run(run.run_id, worker,
                 caps=SC.Limits(max_workers=0, max_running=1, max_attempts=1))
    assert seen["instruction"] == SECRET_NOTE
    assert seen["remaining"] == -1.0                    # no deadline is not "0 left"
    assert out.ok is True
    assert SECRET_NOTE not in json.dumps(out.to_payload())
    assert SECRET_NOTE not in json.dumps(SC.stats())
    assert SECRET_NOTE not in json.dumps(D.describe())


def test_the_ceilings_are_read_live_from_config_and_junk_falls_back_never_up(monkeypatch):
    monkeypatch.setattr(cfg, "DAG_MAX_WORKERS", 3, raising=False)
    monkeypatch.setattr(cfg, "DAG_MAX_RUNNING", 5, raising=False)
    monkeypatch.setattr(cfg, "DAG_KIND_LIMITS", {"command": 1}, raising=False)
    monkeypatch.setattr(cfg, "DAG_KIND_BUDGETS", {"llm": 7}, raising=False)
    monkeypatch.setattr(cfg, "DAG_MAX_ATTEMPTS", 4, raising=False)
    monkeypatch.setattr(cfg, "DAG_NODE_TIMEOUT", 1.5, raising=False)
    live = SC.limits()
    assert (live.max_workers, live.max_running) == (3, 5)
    assert live.kind_limits == {"command": 1} and live.kind_budgets == {"llm": 7}
    assert (live.max_attempts, live.node_timeout) == (4, 1.5)
    assert live.width == 3 and live.inline is False
    # An override is a consumer's own vocabulary; junk in one falls back to config.
    assert SC.limits(kind_limits={"scan": 1}).kind_limits == {"scan": 1}
    assert SC.limits(max_workers="nonsense").max_workers == 3
    assert SC.limits(max_running=[]).max_running == 5
    assert SC.limits(kind_limits="nonsense").kind_limits == {"command": 1}
    # And the engine's own report reads the same numbers, through the same function.
    assert D.describe()["scheduler"]["limits"] == live.to_payload()
    assert D.describe()["scheduler"]["holds"] == list(SC.HOLD_CODES)


def test_the_vocabularies_are_closed_and_stay_two():
    assert len(set(SC.HOLD_CODES)) == len(SC.HOLD_CODES)
    assert len(set(SC.REASONS)) == len(SC.REASONS)
    assert len(set(SC.EVENTS)) == len(SC.EVENTS)
    # ⚠️ TWO VOCABULARIES, AND THEY SHARE NO WORD. A hold says why ONE node waited —
    # and a `Plan`'s own `reason` is the hold that bound it, so a planner speaks only
    # holds. A `REASONS` word says how a whole RUN ended. One shared spelling would
    # make `held[i]["code"]` and `Outcome.reason` read as the same fact at a glance,
    # and they answer questions at different scales.
    assert set(SC.HOLD_CODES) & set(SC.REASONS) == set()
    run = _run([_node("a"), _node("b")], name="vocab")
    plan = SC.plan_next(S.load(run.run_id), caps=SC.Limits(max_workers=1, max_running=1))
    assert plan.reason == "" and _codes(plan) == [SC.H_RUNNING]
    held = SC.plan_next(S.load(run.run_id), caps=SC.Limits(max_workers=1, max_running=1),
                        workers=0)
    assert held.reason in set(SC.HOLD_CODES) and bool(held) is False


def test_a_run_that_can_never_proceed_says_blocked_rather_than_spinning():
    """A guard, not a schedule: the loop always reaps, dispatches or breaks."""
    run = _run([_node("a"), _node("b", needs=["a"])], name="stuck")
    S.mark(run.run_id, "a", M.PAUSED, advance_run=False)
    out = SC.run(run.run_id, _Recorder(),
                 caps=SC.Limits(max_workers=1, max_running=1, max_attempts=1))
    assert out.reason in (SC.R_PAUSED, SC.R_BLOCKED, SC.R_HELD)
    assert out.rounds <= 8 + 2 * (1 + 2) * 2            # the declared backstop
    assert out.ok is False


def test_a_slow_node_is_never_mistaken_for_a_spinning_pump():
    """⚠️ THE BACKSTOP COUNTS UNPRODUCTIVE ROUNDS, NEVER ITERATIONS.

    A round spent waiting on a live node costs exactly one `POLL_SEC` block in
    `_drain(block=True)`, so charging every iteration made the guard a wall-clock
    ceiling wearing a round count: two nodes at `max_attempts=1` gave a guard of
    20 == **4 seconds**, after which a healthy node three seconds into its work was
    declared `exhausted` and cancelled by `_abandon()`. Wall clock is
    `budget_sec`'s job and a hung node is `caps.node_timeout`'s — both off by
    default *because a node may legitimately be a 40-minute build*, so a backstop
    that silently overrode that default made the documented posture a lie.

    `max_rounds` compresses the covert deadline from ~4 s to ~0.6 s, so the test
    costs a second rather than five and still fails the whole way against the old
    arithmetic (`R_EXHAUSTED`, `completed == 0`).
    """
    slow = 5 * SC.POLL_SEC
    run = _run([_node("a"), _node("b")], name="slow")
    rec = _Recorder(lambda ctx: time.sleep(slow) or True)
    out = SC.run(run.run_id, rec,
                 caps=SC.Limits(max_workers=2, max_running=2, max_attempts=1),
                 max_rounds=3)
    assert out.reason == SC.R_FINISHED
    assert out.completed == 2 and out.ok is True
    assert sorted(rec.nodes) == ["a", "b"]
    # The counter still counts every iteration — `Outcome.rounds` means "this
    # pump's loop iterations" and nothing here may change that. It is the separate
    # unproductive tally the guard is charged against.
    assert out.rounds > 3


def test_a_hold_from_an_earlier_round_survives_into_the_outcome():
    """⚠️ `Outcome.held` is accumulated, never the last round's list.

    A hold repeats every round the node stays held, so the tally is keyed on
    (code, node) and keeps the FIRST sighting — `Plan.codes`' order rule,
    run-shaped. Assigning the latest round's list reported whichever round the
    loop happened to break after, and for a finished run that is the round
    *before* the last (`st.finished` breaks above `dispatch`) — so a run that
    queued two nodes behind a one-at-a-time ceiling reported no hold at all.

    ⚠️ Asserted on the NODES, not the hold word: `dispatch()` picks between
    `H_WORKERS` and `H_RUNNING` by which ceiling actually bound the round, and
    `go()` always passes a real `workers` count, so hard-coding either one would
    pin an implementation detail of the pump rather than the accumulation.
    """
    run = _run([_node("a"), _node("b"), _node("c")], name="tally")
    out = SC.run(run.run_id, _Recorder(),
                 caps=SC.Limits(max_workers=1, max_running=1, max_attempts=1))
    assert out.reason == SC.R_FINISHED and out.completed == 3
    assert {h["node"] for h in out.held} == {"b", "c"}
    # One entry per (code, node), so a hold seen in ten rounds is reported once.
    assert len(out.held) == len({(h["code"], h["node"]) for h in out.held})
    assert set(_codes(out)) <= set(SC.HOLD_CODES)


# ══════════════════════════════════════════════════════════════════════════════
# 10 · The engine knows about no feature
# ══════════════════════════════════════════════════════════════════════════════
_DAG_DIR = Path(S.__file__).resolve().parent
_DAG_MODULES = ("__init__.py", "model.py", "validate.py", "store.py", "schedule.py")

#: The words the spec forbids the engine from branching on.
_FEATURES = ("workflow", "ultracode", "security", "zap", "burp", "skill")

#: Packages that exist to serve one feature. The DAG core may not reach into them
#: — the dependency runs the other way, or the engine has learned a domain.
_FORBIDDEN_IMPORTS = ("agent2.core.workflow", "agent2.core.skills",
                      "agent2.integrations", "agent2.fileintel", "agent2.core.pil")

#: The ONE exception, and it is the ledger's own vocabulary rather than a feature:
#: `exec_workflows` (migration 21) and `execstate.workflow_*` predate this package
#: and are the shared execution ledger every run is recorded in.
_LEDGER_NAMES = frozenset((
    "workflow", "workflows", "workflow_started", "workflow_step",
    "workflow_finished", "CP_WORKFLOW",
))


def _trees():
    for name in _DAG_MODULES:
        yield name, ast.parse((_DAG_DIR / name).read_text(encoding="utf-8"))


def test_the_core_imports_nothing_that_knows_about_a_feature():
    for name, tree in _trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith(_FORBIDDEN_IMPORTS), \
                        f"{name} imports {alias.name}"
            elif isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith(_FORBIDDEN_IMPORTS), \
                    f"{name} imports from {node.module}"


def test_no_branch_in_the_core_is_decided_by_a_feature_name():
    # `if workflow:` / `if ultracode:` / `if security:` — the shapes the spec names.
    for name, tree in _trees():
        for node in ast.walk(tree):
            if not isinstance(node, (ast.If, ast.IfExp, ast.While)):
                continue
            test_src = ast.unparse(node.test).lower()
            for word in _FEATURES:
                assert word not in test_src, f"{name}: if {test_src}"


def test_nothing_in_the_core_is_named_after_a_feature():
    for name, tree in _trees():
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defined = node.name
            elif isinstance(node, ast.arg):
                defined = node.arg
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                defined = node.id
            else:
                continue
            for word in _FEATURES:
                assert word not in defined.lower(), f"{name} defines {defined}"


def test_the_only_feature_words_in_the_core_are_the_ledgers_own_names():
    # Over IDENTIFIERS, never raw text: a docstring may name a consumer's knob, and
    # the store must still be able to call the ledger it records runs in.
    for name, tree in _trees():
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                ident = node.attr
            elif isinstance(node, ast.Name):
                ident = node.id
            else:
                continue
            if any(word in ident.lower() for word in _FEATURES):
                found.add(ident)
        allowed = _LEDGER_NAMES if name == "store.py" else frozenset()
        assert found <= allowed, f"{name}: {sorted(found - allowed)}"


def test_the_package_shadows_its_own_validate_submodule():
    # ⚠️ `dag/__init__.py` re-exports a FUNCTION called `validate`, which shadows the
    # submodule of the same name on the package object. `from agent2.core.dag import
    # validate as V; V.levels_for(...)` is an AttributeError that only fires at run
    # time, so consumers import the four names directly — as this file does.
    assert D.validate is validate
    assert not isinstance(D.validate, types.ModuleType)
    assert not hasattr(D.validate, "levels_for")
    submodule = sys.modules["agent2.core.dag.validate"]
    assert isinstance(submodule, types.ModuleType)
    assert submodule.validate is validate
    assert submodule.levels_for is levels_for
    assert submodule.find_cycles is find_cycles
    assert submodule.validate_mutation is validate_mutation


def test_describe_is_built_from_the_declarations_and_reads_its_knobs_live(monkeypatch):
    d = D.describe()
    assert d["schema"] == M.SCHEMA_VERSION
    assert d["min_schema"] == M.MIN_SCHEMA
    assert d["states"] == list(M.STATES)
    assert d["terminal"] == sorted(M.TERMINAL)
    assert d["open"] == sorted(M.OPEN)
    assert d["problems"] == list(M.PROBLEM_CODES)
    assert d["warnings"] == list(M.WARNING_CODES)
    assert d["max_nodes"] == cfg.DAG_MAX_NODES
    assert d["max_mutations"] == cfg.DAG_MAX_MUTATIONS

    monkeypatch.setattr(cfg, "DAG_MAX_NODES", 7)
    monkeypatch.setattr(cfg, "DAG_MAX_MUTATIONS", 3)
    again = D.describe()
    assert again["max_nodes"] == 7
    assert again["max_mutations"] == 3
    assert S.stats()["max_nodes"] == 7


# ══════════════════════════════════════════════════════════════════════════════
# 12 · A run belongs to a project, or recovery cannot find it
# ══════════════════════════════════════════════════════════════════════════════
def _project_now() -> str:
    from agent2.core import workspace as _ws
    return str(_ws.root())


def test_a_run_is_stamped_with_this_project_so_the_recovery_query_finds_it():
    """`create()` fills an unstated `cwd`, because an unstamped session is invisible.

    ⚠️ This is the pin for a defect that SHIPPED and produced no error: both
    `instantiate()` call sites omitted `cwd=`, so `task_sessions.cwd` was `''` for
    every workflow run. `tasks.unfinished_sessions(cwd)` filters on that column and
    both of its readers pass a project (`recovery.candidates()` →
    `cwd or os.getcwd()`, `execstate.snapshot()` → `workspace.root()`), so an
    interrupted run was absent from `/recovery` and from `GET /api/recovery`'s
    `candidates` half — while `/workflow state` read perfectly, because the
    `exec_workflows` row is project-stamped independently by `execstate._project()`.

    Turns red when: `create()` passes `cwd=` straight through again — the project
    query drops to 0 rows while every assertion about the run row still passes,
    which is exactly the shape of the failure.
    """
    here = _project_now()
    run = _run(_chain("a", "b"), name="scoped", chat_id="chat-scope")

    row = db.qone("SELECT cwd FROM task_sessions WHERE id=?", (run.session_id,))
    assert row is not None
    # Canonicalised by `tasks._project_key` — the ONE canonicaliser (migration 10).
    assert row["cwd"] == T._project_key(here) != ""

    # The scoped reader — the one `/recovery` and `execstate.snapshot()` both use.
    scoped = [r["id"] for r in T.unfinished_sessions(here)]
    assert run.session_id in scoped

    # …and the unscoped form still lifts the filter, so nothing regressed there.
    assert run.session_id in [r["id"] for r in T.unfinished_sessions("")]

    # The two derivations agree: the ledger row names the same project.
    led = db.qone("SELECT project FROM exec_workflows WHERE id=?", (run.run_id,))
    assert led is not None and led["project"] == T._project_key(here)


def test_a_caller_that_names_a_project_still_wins_over_the_default():
    """The default fills a silence; it never overrules a stated `cwd`."""
    mine = _project_now()
    theirs = str(Path(mine).parent / "somewhere-else-entirely")
    run = _run(_chain("a"), name="elsewhere", chat_id="chat-else", cwd=theirs)

    row = db.qone("SELECT cwd FROM task_sessions WHERE id=?", (run.session_id,))
    assert row["cwd"] == T._project_key(theirs)
    assert row["cwd"] != T._project_key(mine)
    assert [r["id"] for r in T.unfinished_sessions(theirs)] == [run.session_id]
    assert run.session_id not in [r["id"] for r in T.unfinished_sessions(mine)]


def test_the_default_is_total_and_a_run_still_starts_when_the_root_is_unreadable():
    """A project that cannot be resolved costs the stamp, never the run.

    `_here()` degrades to `""`, which is the old behaviour — a session listed
    nowhere — rather than a plan that could not start.
    """
    import agent2.core.workspace as _ws

    def _boom():
        raise RuntimeError("no workspace")

    real, _ws.root = _ws.root, _boom
    try:
        assert S._here() == ""
        run = _run(_chain("a"), name="rootless", chat_id="chat-rootless")
    finally:
        _ws.root = real
    row = db.qone("SELECT cwd FROM task_sessions WHERE id=?", (run.session_id,))
    assert row["cwd"] == ""
    assert run.ok
