# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2.core.dag
───────────────
THE generalized DAG core — Phases D1–D2. One graph engine for the whole application,
and *"the DAG engine must NOT contain business-specific logic"*.

Four modules, one job each:

| Module | Owns |
|---|---|
| `model.py` | what a graph, a node, an edge and a node **state** are |
| `validate.py` | whether a graph can ever finish, and the order it may run in |
| `store.py` | a graph's rows, its live state, and what a crash left of it |
| `schedule.py` | which of the runnable nodes actually run, and what held the rest |

⚠️ **THIS PACKAGE KNOWS NOTHING ABOUT WORKFLOWS, ULTRACODE, SECURITY OR SKILLS,
AND THAT IS ASSERTED RATHER THAN PROMISED.** `test_dag.py` walks the package with
`ast` and fails if any branch condition or defined name mentions a feature, and if
anything here imports `core.workflow`, `core.skills`, `integrations` or
`fileintel`. The forbidden shapes are the spec's own: `if workflow:`, `if
ultracode:`, `if security:`, `if zap:`, `if skill:`. Domain vocabulary still
travels through the engine — as **data** it never reads: `Node.kind` (`"tool"`,
`"llm"`, `"approval"`, `"mcp"`, `"subagent"`), `Node.resource` (a mutual-exclusion
token) and `Node.meta` (a consumer's own dict). That is what makes the node model
extensible without the core learning a node type.

⚠️ **AND IT KNOWS NOTHING ABOUT ITS CONSUMERS' KNOBS.** `validate()` takes
`max_nodes`, the `knob` its message should name, and the `label` its prose should
use, so `core.workflow.graph.validate()` still refuses a 4-node graph with a message
naming `AGENT2_WORKFLOW_MAX_NODES` and the word "workflow" — one validator, two
vocabularies, and no branch deciding which ceiling applies.

⚠️ **A CONSUMER IS A THIN WRAPPER, NEVER A SECOND ENGINE.** *"No `WorkflowDAG` /
`UltraCodeDAG` / `SecurityDAG` / `DynamicWorkflowDAG` unless these are merely thin
domain-level wrappers around the same core DAG API."* `core.workflow.graph` is that
wrapper today: `WorkflowDef` is `Graph` under its own name, and every function it
exports delegates here.

⚠️ **NOTHING NEW IS STORED.** A graph is one `exec_workflows` row plus N
`agent_tasks` rows, so *"ONE SHARED TASK MANAGEMENT SYSTEM · ONE SHARED
RECOVERY/CHECKPOINT SYSTEM"* is satisfied by reusing them rather than by a second
store that resembles them — no new table and no migration. `store.py`'s docstring
maps every graph fact to its column and explains why structure may never live in
`exec_workflows.state`.

Where the states come from, since this is the question every consumer asks:
**six** are read straight off `agent_tasks.status` (the four terminal ones, RUNNING
and PAUSED), ⚠️ **READY and BLOCKED are derived on every read, never written** —
`tasks.ready()` is the one declaration of "may this run now", and a stored copy goes
stale the instant an upstream node settles — and PENDING is what `plan()` reports
for a graph with no runnable order at all, so it is the one state that never comes
out of a row. `dispatchable` is narrower still (READY **and** the row unclaimed), and
it lives in the core because two workers reading one READY list is how a node runs
twice.

⚠️ **AND `dispatchable` IS WHERE THE ENGINE STOPS AND THE SCHEDULER STARTS.** It is
the graph's answer — it knows nothing of worker limits, resource contention or a
consumer's budget — and *"never blindly run every READY node"* is `schedule.py`'s
rule over it: `plan_next()` filters that list against the ceilings and hands back a
reason for every node it did not take, `dispatch()` turns the survivors into claims,
and `run()` drives a whole graph to one `Outcome`. ⚠️ It is **one** scheduler for
every consumer, exactly as this is one graph: `core/scheduler.py` is untouched and
must stay so — that pool bounds *turns*, whose queue outlives any graph, while this
one bounds the nodes of a single run and dies with it.
"""

from __future__ import annotations

from . import schedule as _schedule
from .model import (
    BLOCKED,
    CANCELLED,
    COMPLETED,
    FAILED,
    MIN_SCHEMA,
    OPEN,
    PAUSED,
    PENDING,
    PROBLEM_CODES,
    READY,
    RUNNING,
    SCHEMA_VERSION,
    SKIPPED,
    STATES,
    TERMINAL,
    UNSUCCESSFUL,
    WARNING_CODES,
    Edge,
    Graph,
    Node,
    Validation,
    fold_id,
    make_graph,
    make_node,
    problem,
)
from .schedule import (
    EVENTS,
    HOLD_CODES,
    REASONS,
    WORKER,
    Ctx,
    Limits,
    NodeOutcome,
    Outcome,
    Plan,
    Slot,
    dispatch,
    in_flight,
    limits,
    may_repeat,
    outcome_of,
    plan_next,
    run,
    spend,
)
from .store import (
    SURFACE,
    GraphRun,
    GraphState,
    NodeView,
    advance,
    cancel_run,
    claim,
    create,
    extend,
    live,
    load,
    mark,
    pause_run,
    plan,
    release_interrupted,
    resumable,
    runs,
    settle,
    stats,
    unpause_run,
)
from .validate import find_cycles, levels_for, validate, validate_mutation

__all__ = [
    "BLOCKED", "CANCELLED", "COMPLETED", "EVENTS", "FAILED", "HOLD_CODES",
    "MIN_SCHEMA", "OPEN", "PAUSED", "PENDING", "PROBLEM_CODES", "READY", "REASONS",
    "RUNNING", "SCHEMA_VERSION", "SKIPPED", "STATES", "SURFACE", "TERMINAL",
    "UNSUCCESSFUL", "WARNING_CODES", "WORKER",
    "Ctx", "Edge", "Graph", "GraphRun", "GraphState", "Limits", "Node",
    "NodeOutcome", "NodeView", "Outcome", "Plan", "Slot", "Validation",
    "advance", "cancel_run", "claim", "create", "describe", "dispatch", "extend",
    "find_cycles", "fold_id", "in_flight", "levels_for", "limits", "live", "load",
    "make_graph", "make_node", "mark", "may_repeat", "outcome_of", "pause_run",
    "plan", "plan_next", "problem", "release_interrupted", "resumable", "run",
    "runs", "settle", "spend", "stats", "unpause_run", "validate",
    "validate_mutation",
]


def describe() -> dict:
    """What this build's DAG engine is, as data. For `/health` and a payload.

    ⚠️ Built from the declarations themselves — the state tuple, the code tables,
    the live config — never a hand-written list. A document is the one form of this
    answer that can go stale while the engine changes underneath it, which is
    `loader.SUBSET`'s rule and the reason it is stated as a payload at all.
    """
    return {
        "schema": SCHEMA_VERSION,
        "min_schema": MIN_SCHEMA,
        "states": list(STATES),
        "terminal": sorted(TERMINAL),
        "open": sorted(OPEN),
        "problems": list(PROBLEM_CODES),
        "warnings": list(WARNING_CODES),
        # ⚠️ Nested under its own key rather than merged: the scheduler's ceilings are
        # a different subsystem's facts, and `stats()` here is the store's. Flattening
        # them would put two `limits` in one namespace the day a consumer adds a third.
        "scheduler": _schedule.stats(),
        **stats(),
    }
