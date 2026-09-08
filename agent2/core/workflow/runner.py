# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2.core.workflow.runner
───────────────────────────
Where a validated graph becomes **rows that already exist** — Task 37 — and, since
Phase D1, a **projection over `core.dag.store`** rather than a second store.

A workflow run is, and has always been:

  * one `task_sessions` row — the run's own session;
  * one `agent_tasks` row **per node**, with `dependencies` holding the task ids
    the graph's `needs:` resolved to;
  * one `exec_workflows` row — the durable breadcrumb crash recovery reads.

All three tables, all three writers and every index they need were already here
before Phase 12. `exec_workflows` was created by migration 21 and its writers
(`execstate.workflow_started` / `workflow_step` / `workflow_finished`) were written,
tested and left **caller-less on purpose**, so that the phase which adds workflows
*calls* rather than *edits*. There is still no migration. What changed in Phase D1
is not what is stored, and not who stores it — only **who spells the writing**.

⚠️ **THE WRITES LIVE IN `core.dag.store` NOW, AND THIS MODULE TRANSLATES.** The
spec's final rule is *"ONE SHARED EXECUTION INFRASTRUCTURE · ONE SHARED TASK
MANAGEMENT SYSTEM · ONE SHARED RECOVERY/CHECKPOINT SYSTEM"*, and it permits a
per-feature graph layer only as *"a thin domain-level wrapper around the same core
DAG API"*. So the insert loop, the readiness ladder and the breadcrumb builder this
file used to own are **deleted rather than reimplemented** — `graph.py` did that for
the definition (`WorkflowDef is dag.Graph`), and this file does it for the run
(`Run is dag.GraphRun`). Two aliases and one projection is the whole wrapper, and
every ⚠️ below is now *stated* here and *enforced* in `core/dag/store.py` — read
that docstring before changing behaviour.

⚠️ **`PHASE_*` IS THE ONE SANCTIONED TRANSLATION TABLE IN THE PROJECT.**
`dag/model.py` names it by name: the nine node states are `tasks.TaskStatus`' own
strings precisely so that nothing needs translating, *"and a second vocabulary
(`"done"` for `"completed"`) is a translation table somebody has to keep —
`runner.py` keeps exactly one, for its own historical `PHASE_*` payload, and its
cost is visible there"*. `_PHASE_FOR` is that cost, made visible: five phases over
nine states, kept because `GET /api/workflows`, `/workflow status` and
`public/script.js` have read `phase` since Task 37 and *"preserve backward
compatibility wherever practical"* outranks a tidier word. A **new** consumer reads
`dag.store.GraphState` and gets the nine states with no table in between.

⚠️ **PAUSED PROJECTS TO `PHASE_BLOCKED`, AND THAT IS A FIX, NOT A RENAME.** The old
ladder asked `task.id in ready_ids` for anything neither terminal nor running — and
`tasks.ready()` releases a **paused** row, correctly, because readiness is a
statement about dependencies and a held node's dependencies are as settled as
anyone's. So a node a human deliberately paused read `PHASE_READY`, and every
surface offered it as the next thing to run. `store._state_of` settles it once by
testing PAUSED *before* readiness; this table only has to not undo it, which is why
it is total over `model.STATES` and falls back to `PHASE_BLOCKED` rather than to
anything a driver would act on.

⚠️ **A RUN GETS ITS OWN TASK SESSION, AND THAT IS LOAD-BEARING.** `tasks.ready()`
is scoped by `session_id`, so sharing the chat's session would put the user's ad-hoc
`/tasks` todos and the workflow's nodes in one ready pool — `ready()` would hand a
chat todo to the workflow driver as a runnable node, and hand a workflow node to
`update_todo` as something the model may tick off. Both look plausible from either
side. One session per run keeps "what may run now" a question about one graph.

⚠️ **NOTHING IS WRITTEN UNTIL THE GRAPH VALIDATES.** `store.create()` validates
first and returns a refusal rather than a partial run, because every failure
validation catches is *durable* once rows exist: a cycle deadlocks `ready()` forever
with no error, and an unknown dependency releases a node to run out of order.
Cleaning either up is a human deleting task rows by hand. The ceiling, the knob its
message names and the noun its prose uses are **injected** from
`config.WORKFLOW_MAX_NODES` / `graph.KNOB` / `graph.LABEL`, so the refusals a
workflow author reads are the sentences Task 37 shipped, word for word, while the
engine contains no `if workflow:`.

⚠️ **THE TASK ROWS ARE THE TRUTH; `exec_workflows` IS THE BREADCRUMB.**
`state_for()` derives progress from the task rows every time and never from the run
row's counters, so "how far along is this workflow" has exactly one answer. The run
row exists because `execstate.interrupted()` / `sweep()` scan it to find runs whose
process died — a fact no `agent_tasks` row records, since a task can be interrupted
without its workflow being.

⚠️ **COMPLETED NODES CANNOT RE-RUN, AND NOT BECAUSE THIS MODULE IS CAREFUL.**
`tasks.set_status()` refuses to move a terminal task without `force=True`, so a
resume that asked for every node would still only advance the open ones. `resume()`
returns the open set, and `test_workflow.py` proves the guarantee by attempting the
wrong thing rather than by trusting the right thing — the user's standing rule that
recovery must never re-run completed work.

Everything here is total: a workflow is a convenience, and a broken run record may
not raise into a turn. `state_for()` on a missing run returns an empty state
(`exists=False`, which reads as *no such run* and never as *every node is done*),
and `stats()` swallows and reports rather than propagating — hence this module's
written BLE001/S110 exemption.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agent2 import config
from agent2.core import execstate as _exec
from agent2.core import tasks as _tasks
from agent2.core import verify as _verify
from agent2.core.dag import model as _m
from agent2.core.dag import schedule as _schedule
from agent2.core.dag import store as _store
from agent2.core.workflow import graph as _graph

#: The `task_sessions.surface` value a workflow run stamps. `/tasks` and the
#: recovery panels both show a session's surface, so a run is distinguishable from
#: a chat's todo list without asking a second table. ⚠️ Passed to `store.create()`
#: rather than taken from `store.SURFACE` (which is `"dag"`): the core's default
#: names the *engine*, and a consumer that has said who it is should be recorded as
#: itself — a label, never a branch.
SURFACE = "workflow"

#: The five phases this module's payload has used since Task 37.
PHASE_DONE = "done"
PHASE_FAILED = "failed"
PHASE_RUNNING = "running"
PHASE_READY = "ready"
PHASE_BLOCKED = "blocked"

#: The nine core states → those five phases. ⚠️ Total over `model.STATES`, and
#: `_phase_for()` falls back to `PHASE_BLOCKED` for anything absent, because a state
#: this table has never heard of must read as *not runnable yet*: the alternative is
#: a surface offering an unknown state as the next thing to run. `PAUSED` is the
#: entry the old ladder could not express — see the docstring.
_PHASE_FOR: dict[str, str] = {
    _m.COMPLETED: PHASE_DONE,
    _m.FAILED: PHASE_FAILED,
    _m.CANCELLED: PHASE_FAILED,
    _m.SKIPPED: PHASE_FAILED,
    _m.RUNNING: PHASE_RUNNING,
    _m.READY: PHASE_READY,
    _m.BLOCKED: PHASE_BLOCKED,
    _m.PENDING: PHASE_BLOCKED,
    _m.PAUSED: PHASE_BLOCKED,
}

#: ⚠️ An alias, for `WorkflowDef is dag.Graph`'s reason. `store.GraphRun` already has
#: these seven fields and a byte-identical `to_payload()`, so a second dataclass
#: would be a second declaration of *what asking for a run returned* — and the two
#: would drift the first time the core grew a field. A refusal stays a **return
#: value**, not an exception: both callers (`/workflow` and
#: `POST /api/workflows/<name>/run`) are printing surfaces, so `ok=False` plus a
#: `reason` a human can read, and no rows.
Run = _store.GraphRun


@dataclass
class NodeState:
    """One node, as it stands now. Derived from its task row on every read.

    A `dag.store.NodeView` in this module's older, narrower vocabulary — see
    `_project_node()` for what is deliberately dropped.
    """

    node: str
    task_id: str = ""
    title: str = ""
    status: str = _tasks.TaskStatus.PENDING
    phase: str = PHASE_BLOCKED
    #: The same node in the core's **nine-word** vocabulary (`dag.model.STATES`).
    #: ⚠️ Carried **beside** `phase`, never instead of it — three shipped readers key
    #: on `phase` and *"preserve backward compatibility wherever practical"* outranks
    #: a tidier word — and it is not `status` either: six of the nine ARE
    #: `agent_tasks.status` verbatim, but READY and BLOCKED are derived on every read
    #: and no column holds them. A renderer that wants to say why a node is waiting
    #: needs the distinction `phase` folds away (PENDING · BLOCKED · PAUSED all read
    #: `blocked`), and a five-word answer cannot be un-folded by the reader.
    state: str = _m.PENDING
    progress: float = 0.0
    error: str = ""
    blocked_by: tuple[str, ...] = ()
    #: Direct dependencies that settled UNSUCCESSFULLY. ⚠️ Reported, never acted on
    #: here: `tasks.ready()` treats *settled* as satisfied, so a node whose upstream
    #: failed is genuinely runnable by the one declaration of readiness, and adding
    #: a second predicate that withheld it would be exactly the drift this codebase
    #: is shaped against. Whether to skip it is a policy a driver applies through
    #: `tasks.skip()` — Task 39's decision, and Task 42's verification gate.
    upstream_failed: tuple[str, ...] = ()
    #: The node's own instruction. ⚠️ Deliberately absent from `to_payload()`, like
    #: `Skill.to_payload(body=False)`: it is prose a human wrote in their own
    #: checkout, and `GET /api/workflows` is a browser surface. It is carried here so
    #: `for_turn()` costs no extra query — the row it came from was already read.
    instruction: str = ""

    def to_payload(self) -> dict:
        return {
            "node": self.node,
            "task_id": self.task_id,
            "title": self.title,
            "status": self.status,
            "phase": self.phase,
            "state": self.state,
            "progress": round(self.progress, 3),
            "error": self.error,
            "blocked_by": list(self.blocked_by),
            "upstream_failed": list(self.upstream_failed),
        }


@dataclass
class RunState:
    """A whole run, derived from its task rows. The ONE progress answer.

    ⚠️ `done`/`total` are counted from the rows, not read from
    `exec_workflows.step_index`. The run row is a breadcrumb written *from* this
    derivation; deriving one and storing the other independently is two answers to
    one question, and the stored one is the one nobody re-checks.
    """

    run_id: str = ""
    name: str = ""
    status: str = ""
    session_id: str = ""
    chat_id: str = ""
    nodes: list[NodeState] = field(default_factory=list)
    exists: bool = False
    error: str = ""
    started_at: str = ""
    updated_at: str = ""
    #: Where the definition came from and which schema it declared — read back off
    #: the run row. ⚠️ Carried on this object, and re-written by `advance()`, because
    #: `execstate.workflow_step(state=…)` **replaces** the column: provenance that
    #: is written once at `workflow_started` and not carried forward survives exactly
    #: zero node transitions, which is one fewer than the first read of it. The one
    #: builder that carries it is `dag.store._state()`.
    source: str = ""
    schema: int = 0
    #: The `GraphState` this was projected from, when it was projected from one.
    #: ⚠️ **HELD, NEVER RE-LOADED, AND THAT IS WHAT KEEPS THE TURN PATH FLAT.**
    #: `plan()`, `dispatchable`, `held`, `waves` and `width` are all derived from rows
    #: this object already has, so a 24-node run costs a 3-node run's queries — the
    #: guarantee `test_workflow.py` pins at `2 qall + 1 qone` for a live run of any
    #: size. Re-`load()`ing inside a property would be one extra read per question a
    #: renderer asks, which is invisible at the size a developer tests with.
    #: Private, `repr=False` and `compare=False`: two `RunState`s still compare on
    #: what they *mean*, and a hand-built one (a test, an empty state) simply has none
    #: — which is why every reader of it goes through `graph_state`.
    _graph_state: _store.GraphState | None = field(default=None, repr=False,
                                                   compare=False)

    @property
    def total(self) -> int:
        return len(self.nodes)

    @property
    def done(self) -> int:
        return sum(1 for n in self.nodes if n.phase in (PHASE_DONE, PHASE_FAILED))

    @property
    def failed(self) -> list[NodeState]:
        return [n for n in self.nodes if n.phase == PHASE_FAILED]

    @property
    def running(self) -> list[NodeState]:
        return [n for n in self.nodes if n.phase == PHASE_RUNNING]

    @property
    def ready(self) -> list[NodeState]:
        return [n for n in self.nodes if n.phase == PHASE_READY]

    @property
    def finished(self) -> bool:
        """Every node settled. Not the same as `status`, which a crash can stale."""
        return bool(self.nodes) and self.done == self.total

    # ── The graph underneath (Phase D3) ───────────────────────────────────────
    #
    # Five derived answers, all of them free: the rows were read once, so the
    # scheduler's view of them costs nothing further. ⚠️ Every one of these is the
    # ENGINE's answer, forwarded — this module derives no readiness, no ordering and
    # no ceiling of its own, exactly as `dag/schedule.py` derives no readiness of its
    # own. That is the whole of *"ONE GENERALIZED DAG · ONE SHARED SCHEDULER"* at
    # this seam.

    @property
    def graph_state(self) -> _store.GraphState:
        """The core state behind this projection — an empty one when there is none.

        Total by construction: a hand-built `RunState` has no graph, and an empty
        `GraphState` reads as *no such run* (nothing dispatchable, no waves), never as
        *every node is done* — `store.GraphState.finished`'s rule, inherited.
        """
        return self._graph_state or _store.GraphState(run_id=self.run_id)

    @property
    def waves(self) -> tuple[tuple[str, ...], ...]:
        """The topological levels — which nodes this plan PERMITS to run together.

        THE parallelism item 21 asks to be *derived*: an author writes `needs:` and
        nothing else, and the width of their plan falls out of the graph. ⚠️ A graph
        fact, never a dispatch decision — `plan()` is what decides, because a wave
        knows nothing of worker limits, a kind's budget or `Node.resource`.
        """
        return self.graph_state.waves

    @property
    def width(self) -> int:
        """The widest wave — how much parallelism this workflow could ever offer."""
        return self.graph_state.width

    def plan(self, *, caps: _schedule.Limits | None = None,
             workers: int | None = None) -> _schedule.Plan:
        """Which nodes may start NOW, and what held each of the rest. Zero queries.

        ⚠️ **THE SCHEDULER'S ANSWER, NOT A SECOND ONE.** `store.GraphState.dispatchable`
        says which nodes *could* run and `schedule.plan_next()` says which of them
        actually do — *"never blindly run every READY node"* — so `/workflow` shows a
        plan it did not compute and cannot disagree with. Writes nothing, which is why
        a listing surface may call it.
        """
        return _schedule.plan_next(self.graph_state, caps=caps, workers=workers)

    @property
    def dispatchable(self) -> list[NodeState]:
        """Nodes the graph offers as runnable — before any ceiling is applied."""
        offered = {v.node for v in self.graph_state.dispatchable}
        return [n for n in self.nodes if n.node in offered]

    @property
    def interrupted(self) -> list[NodeState]:
        """Nodes a dead process left parked — what `resume()` may release.

        ⚠️ Not `blocked`, and not every PAUSED node: a hold somebody *chose* is not
        unfinished work. See `dag.store.release_interrupted()`.
        """
        parked = {v.node for v in self.graph_state.interrupted}
        return [n for n in self.nodes if n.node in parked]

    def node(self, node_id: str) -> NodeState | None:
        """One node by id, or None. Bounded scan of a list already in hand."""
        for found in self.nodes:
            if found.node == node_id:
                return found
        return None

    def current(self) -> NodeState | None:
        """Where the run is up to: what is RUNNING, else what may START now.

        ⚠️ **THE SECOND BRANCH IS THE SCHEDULER'S, AND THAT IS THE POINT OF ITEM 22.**
        `self.ready` is row order, while `plan_next()` offers `(priority, seq)` order
        and withholds a node whose upstream failed, whose `resource` another node
        holds, or whose kind is out of budget. Answering from `ready[0]` was therefore
        a *second* selection rule: it named a node the scheduler would refuse to
        start, and it ignored a priority the author had declared.

        ⚠️ **AND THE THIRD BRANCH MAY NOT BE DELETED.** When every runnable node is
        *held*, `slots` is empty — most importantly for `H_UPSTREAM`, the one hold a
        human most needs to read about — and a `current()` of None would blank the
        "where is it up to" line on all four surfaces at exactly that moment. So a
        held plan still names the node it is holding: the scheduler decides what
        *runs*, this decides what a human is *told*, and those are two questions.
        """
        if self.running:
            return self.running[0]
        for slot in self.plan().slots:
            found = self.node(slot.node)
            if found is not None:
                return found
        return (self.ready or [None])[0]

    def to_payload(self) -> dict:
        cur = self.current()
        step = self.plan()
        return {
            "run_id": self.run_id,
            "name": self.name,
            "status": self.status,
            "session_id": self.session_id,
            "chat_id": self.chat_id,
            "exists": self.exists,
            "error": self.error,
            "source": self.source,
            "schema": self.schema,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "total": self.total,
            "done": self.done,
            "failed": len(self.failed),
            "finished": self.finished,
            "current": cur.node if cur else "",
            # Phase D3 — additive, and additive is the whole contract here: every
            # existing reader (`render_workflow_run`, `_collect_workflow`, the five
            # route handlers, `for_turn`) uses `.get()` with a default, so a key that
            # arrives is ignored by anything that has not learned it. ⚠️ Ids and
            # counters only — no `instruction`, for `NodeState.to_payload()`'s reason.
            "width": self.width,
            "waves": [list(w) for w in self.waves],
            "next": [s.node for s in step.slots],
            "held": list(step.held),
            # ⚠️ REPORTED SO A SURFACE NEED NOT ASK. `recover()` costs a `load()`, and
            # a read command that spent one on every call just to discover there was
            # nothing parked would be a query per `/workflow state` in the ordinary
            # case. These ids come from rows this object already holds, so a surface
            # can tell *whether* a dead process left anything behind for free, and
            # only then act.
            "interrupted": [n.node for n in self.interrupted],
            "nodes": [n.to_payload() for n in self.nodes],
        }


# ── The projection ─────────────────────────────────────────────────────────────

def _phase_for(state: str) -> str:
    """One of `model.STATES` in this module's five-word vocabulary."""
    return _PHASE_FOR.get(state, PHASE_BLOCKED)


def _project_node(view: _store.NodeView) -> NodeState:
    """One `NodeView` as a `NodeState`. Pure — no queries, no clock."""
    return NodeState(
        node=view.node, task_id=view.task_id, title=view.title, status=view.status,
        phase=_phase_for(view.state), state=view.state, progress=view.progress,
        error=view.error,
        blocked_by=tuple(view.blocked_by), upstream_failed=tuple(view.upstream_failed),
        instruction=view.instruction,
    )


def _project(st: _store.GraphState) -> RunState:
    """A `GraphState` as a `RunState` — the projection, and the only one.

    Field-for-field, and deliberately **narrower** in what it *flattens*: `declared`,
    `growth`, `kind`, `resource`, `priority`, `seq`, `needs` and `attempts` are facts
    the core added in Phase D1 that this payload never carried, and copying them onto
    a second dataclass would change `GET /api/workflows`' shape for every existing
    reader while creating one more field to keep in step.

    ⚠️ **SO THE STATE ITSELF IS CARRIED INSTEAD OF COPIED** (Phase D3). A consumer that
    wants those facts asks the graph — `rs.graph_state`, `rs.plan()`, `rs.waves` — and
    gets the engine's own answer with no second declaration and, because the rows were
    already read, no second query. The alternative that Phase D1 anticipated ("a
    consumer should hold the `GraphState`, which is one `dag.store.load()` away") is
    exactly one `load()` too many on the turn path: `for_turn()` is pinned flat at
    `2 qall + 1 qone` whether the graph has 3 nodes or 24.

    ⚠️ The node order is the store's (`seq`, then id — the topological order the rows
    were written in, extended at the end by anything added since). Re-sorting here
    would be a second ordering of one plan; the *dispatch* order is the scheduler's
    (`priority`, then `seq`), and those two disagreeing is intended —
    `sources.ORDER` vs `sources.PRIORITY`, node-shaped.
    """
    return RunState(
        run_id=st.run_id, name=st.name, status=st.status, session_id=st.session_id,
        chat_id=st.chat_id, exists=st.exists, error=st.error,
        started_at=st.started_at, updated_at=st.updated_at,
        source=st.source, schema=st.schema,
        nodes=[_project_node(n) for n in st.nodes],
        _graph_state=st,
    )


# ── Starting a run ─────────────────────────────────────────────────────────────

def instantiate(defn: _graph.WorkflowDef, *, chat_id: str = "", cwd: str = "",
                workspace_id: str = "", model: str = "", mode: str = "",
                surface: str = SURFACE) -> Run:
    """Turn a definition into task rows and a run record. Validates first.

    Returns a `Run`. On a refusal — a bad graph, a denied capability, the feature
    switched off — nothing at all is written and `reason` says why.

    ⚠️ **ONLY THE FIRST CHECK IS THIS MODULE'S**, and that split is what the whole
    wrapper turns on: `AGENT2_WORKFLOWS` is a *workflow* master switch, so the
    engine may not know it exists, while the ledger, the capability gate, the
    validator, the topological insert and the node id → task id remap are identical
    for every consumer and therefore live once, in `dag.store.create()`. The three
    refusal sentences it produces at `label="workflow"` are the ones Task 37 shipped,
    character for character — that is what injecting `label`/`knob`/`max_nodes` buys.
    """
    if not config.WORKFLOW_ENABLED:
        return Run(reason="workflows are disabled (AGENT2_WORKFLOWS=0)")
    # ⚠️ No `goal=`: `create()` derives it as `description or f"{label}: {name}"`,
    # which reproduces the old `f"workflow: {defn.name}"` exactly. Passing one would
    # be a second spelling of a string two surfaces show as a session's goal.
    return _store.create(
        defn, chat_id=chat_id, cwd=cwd, workspace_id=workspace_id, model=model,
        mode=mode, surface=surface, label=_graph.LABEL, knob=_graph.KNOB,
        max_nodes=config.WORKFLOW_MAX_NODES, event="workflow_start",
    )


# ── Reading a run ──────────────────────────────────────────────────────────────

#: ⚠️ The store's function, by name. A second reader of `checkpoint[CP_NODE]` would
#: be a second answer to "is this row a graph node", and the mapping between a task
#: and a node is exactly the kind of fact that must have one home. Private on both
#: sides on purpose: it has no caller outside the two modules and the test that pins
#: the empty answer for a stray row.
_node_of = _store._node_of


def state_for(run_id: str) -> RunState:
    """Everything about one run, derived from its rows. Never raises.

    Two queries, and only two — `dag.store.load()`'s guarantee, unchanged: the run
    row, then the session's tasks, with `tasks.ready(session_id, pool)` asked over
    the rows already in hand. The cost is flat in node count, which is what makes
    `for_turn()` affordable on the turn path at the size a planner generates.
    """
    return _project(_store.load(run_id))


def advance(run_id: str) -> RunState:
    """Re-derive the run, write the breadcrumb, settle it once every node has.

    The breadcrumb write is the store's (`exec_workflows.step` / `step_index`), so
    the durable record cannot disagree with the rows it was derived from.

    ⚠️ Returns the state **as it was when the run settled**, not a fresh read —
    `store.advance()` hands its own object to `store.settle()` and returns that same
    object, which is the behaviour this function has had since Task 37 and the reason
    a caller printing `done`/`total` after a final transition sees `n of n` rather
    than paying two more queries to be told the same thing.
    """
    return _project(_store.advance(run_id))


def settle(run_id: str, *, state: RunState | None = None) -> RunState:
    """Close a run whose nodes have all settled. Idempotent.

    ⚠️ `ok` is decided by the **node rows**, never by whatever asked to settle. A
    driver reporting success while one node's task row says FAILED is precisely the
    "worker says done ⇒ Agent2 says done" the user forbade — so a handed-in *state*
    is honoured only as a **veto** ("not finished: do nothing"), and the verdict
    itself is always read back off disk.

    ⚠️ **THE `finished` GUARD IS THIS MODULE'S, NOT THE CORE'S.** `store.settle()`
    deliberately has none, because a cancelled graph is settled *with open nodes
    still in it* — that is `cancel_run()`'s whole shape. This entry point has meant
    "close a run that is complete" since Task 37 and is called by surfaces that have
    not checked, so it keeps the stricter contract.
    """
    if state is not None and (not state.exists or not state.finished):
        return state
    st = _store.load(run_id)
    if not st.exists or not st.finished:
        return _project(st)
    _store.settle(run_id, state=st)
    return _project(_store.load(run_id))


def resume(run_id: str) -> list[NodeState]:
    """The nodes a resumed run may still do — the open ones, in runnable order.

    ⚠️ **THE GUARANTEE IS NOT ENFORCED HERE.** `tasks.set_status()` refuses to move
    a terminal task without `force=True`, so a driver that ignored this list and
    tried every node would still advance only the unfinished ones. This returns the
    honest set so a surface can *say* what will happen; the invariant lives one
    layer down, in the one status writer, where nothing can route around it.

    ⚠️ A PAUSED node is deliberately absent — `store.resumable()`'s rule: resuming a
    run must not quietly un-hold what a human held, and the two phases below are the
    projection of exactly the two states that function returns.
    """
    st = state_for(run_id)
    return [n for n in st.nodes if n.phase in (PHASE_READY, PHASE_RUNNING)]


def recover(run_id: str) -> list[NodeState]:
    """Free the nodes a killed process left mid-flight, and say which. Never raises.

    A crash *parks* a running node rather than losing it: `tasks.interrupt()` writes a
    `CP_STOPPED` checkpoint and then PAUSES the row, so the work survives the process
    that was doing it. This releases exactly those rows back to PENDING — after which
    `tasks.ready()` offers them again — and returns them so a surface can say what it
    just un-stuck.

    ⚠️ **IT IS THE STORE'S `interrupted` SET, WHICH IS NOT EVERY PAUSED NODE.** A
    human's `/pause` writes no checkpoint, and the user's standing rule is that
    recovering *execution* may never quietly un-hold what a person held — so the
    predicate is *paused **and** carrying a stop checkpoint*, and it lives in
    `dag.store.release_interrupted()` where every consumer reads the same one.

    ⚠️ **A COMPLETED NODE IS UNREACHABLE FROM HERE BY CONSTRUCTION, NOT BY CARE.**
    Only an open row can be interrupted in the first place, and `tasks.set_status()`
    refuses to move a terminal task without `force=True` — so *"do not restart
    completed work"* is a state this function cannot produce, which is a stronger
    guarantee than a check it performs.
    """
    return [_project_node(view) for view in _store.release_interrupted(run_id)]


def verify(run_id: str, *, state: RunState | None = None) -> _verify.Report:
    """Independent evidence that this run's nodes did what their status claims.

    The spec's flow is *Execute → **Verify** → Complete*, and its bar is *"'Done' is
    not verification"*. A node's status is a **claim** made by whatever ran it; this
    asks the two durable ledgers — `exec_commands` and `exec_tool_calls` — whether the
    work is actually there, and returns the five-verdict `Report` that answers.

    ⚠️ **THE VERIFIER KNOWS NOTHING ABOUT WORKFLOWS, AND THAT IS THE POINT.**
    `core.verify` verifies a **task row**, so Workflow, Dynamic Workflow and UltraCode
    share one verdict vocabulary with no `if workflow:` anywhere in it — the spec's
    *ONE SHARED VERIFICATION SYSTEM*. This function is the projection, exactly as
    `state_for()` is the projection of `dag.store.load()`.

    ⚠️ **THREE QUERIES, FLAT IN NODE COUNT.** `verify.verify_task()` handed an *id*
    spends one `tasks.get()` per node; handed the `Task` row a caller already has, it
    spends none. So the session's rows are read **once** and the objects are passed
    down — one `list_tasks` plus `evidence_for()`'s two, whether the graph holds 3
    nodes or 64. A per-node read would be invisible at the size a developer tests with
    and 130 round trips at the size a planner generates: `for_turn()`'s rule, applied
    to the one other place that walks every node.

    ⚠️ It is **not** wired into `settle()`. Settling is idempotent and may be reached
    twice, verification writes an audit line every time it runs, and a run is worth
    verifying when a *human or a driver asks* — so the call sits at the surfaces
    (`/workflow state`, and D4's pump), never on the turn path.
    """
    st = state if (state is not None and state.run_id == run_id) else state_for(run_id)
    ids = [n.task_id for n in st.nodes if n.task_id]
    found: dict = {}
    if ids and st.session_id:
        try:
            found = {t.id: t for t in _tasks.list_tasks(st.session_id)}
        except Exception:  # degrade to ids — one get() per node, the same verdicts
            found = {}
    return _verify.verify_tasks([found.get(i, i) for i in ids],
                                session_id=st.session_id, ref=run_id)


def runs(*, project=None, chat_id: str = "", session_id: str = "",
         active: bool | None = None, limit: int = 20) -> list[dict]:
    """Recorded workflow runs — a thin pass to `dag.store.runs()`.

    `project=None` means *this* project, which is `execstate`'s own default;
    `project=execstate.ANY_PROJECT` widens, exactly as `/api/recovery/scan` does.

    ⚠️ The parameter stays `active: bool | None`. The core spells it `active_only`,
    and `agent2cli.py`, `server/routes.py` and `workflow/__init__.py` have passed
    this one by keyword since Task 37 — where `None` and `False` both mean *do not
    filter*, which is what `bool()` preserves.
    """
    return _store.runs(project=project, chat_id=chat_id, session_id=session_id,
                       active_only=bool(active), limit=limit)


def live(*, project=None, chat_id: str = "") -> RunState | None:
    """The newest run that has not settled, or None. One indexed query when idle.

    ⚠️ This is the turn path's entry point (`for_turn()` renders it), so the cost of
    *no workflow running* has to be one cheap read: `idx_exec_workflows_live` covers
    `(status, updated_at DESC)`, and `runs(active_only=True, limit=1)` uses it. A
    turn in a project that has never run a workflow pays one indexed lookup that
    returns nothing — and never a `load()`.
    """
    st = _store.live(project=project, chat_id=chat_id)
    return _project(st) if st is not None else None


def stats() -> dict:
    """Counters for `/api/health` and `describe()`. No node text, ever.

    ⚠️ The keys are **workflow-spelled** (`enabled`, and `max_nodes` from
    `WORKFLOW_MAX_NODES`) rather than forwarded from `dag.store.stats()`, whose
    ceiling is the engine's own `DAG_MAX_NODES`. Reporting the engine's limit under
    a workflow health row would name a number `graph.validate()` does not apply.
    """
    out = {"enabled": config.WORKFLOW_ENABLED, "max_nodes": config.WORKFLOW_MAX_NODES,
           "active": 0, "recent": 0}
    try:
        out["active"] = len(runs(project=_exec.ANY_PROJECT, active=True, limit=50))
        out["recent"] = len(runs(project=_exec.ANY_PROJECT, limit=50))
    except Exception:  # a counter may never be the reason a health read fails
        pass
    return out
