# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2.core.dag.store
─────────────────────
A graph's rows, its live state, and what a crash left of it — Phase D1, items 7, 8,
9 and 10.

⚠️ **THERE IS NO DAG TABLE, AND THAT IS THE DESIGN.** A graph is one
`exec_workflows` row (migration 21) plus N `agent_tasks` rows, exactly as Task 37's
workflow runner already stored one — which means the checkpoints, the heartbeat,
`/recovery`, the Phase 8 crash scan, `/tasks`, the `chat_tasks` payload and every
metric a node's execution produces are the ones that **already exist**. A node that
was not a task row would be a node crash recovery cannot see, and *"ONE SHARED TASK
MANAGEMENT SYSTEM · ONE SHARED RECOVERY/CHECKPOINT SYSTEM"* is not satisfiable by a
second store that merely resembles the first. So: **no new table, no migration.**

Where each part of a graph lives:

| Graph fact | Column |
|---|---|
| the run | one `exec_workflows` row (`id`, `name`, `status`, `total_steps`, `state`) |
| a node | one `agent_tasks` row |
| the node's id | `checkpoint[CP_NODE]` — the only durable node id → task id link |
| the run it belongs to | `checkpoint[CP_WORKFLOW]` |
| its `kind` / `resource` | `checkpoint[CP_KIND]` / `checkpoint[CP_RESOURCE]` |
| its dependencies | `dependencies` — **task** ids, remapped at insert |
| its topological position | `seq` |
| its state | `status`, plus readiness derived over the pool |

⚠️ **STRUCTURE MAY NEVER LIVE IN `exec_workflows.state`.** `execstate._state_json`
degrades a blob over `MAX_STATE_CHARS` to `"{}"` — silently, by design, so a
runaway state write cannot corrupt the ledger — which means a graph stored there
would *lose its own edges* the moment it grew past the cap, and lose `source` and
`schema` with them. On the row, per node, is the only place a node's identity is
safe. `tasks.CP_WORKFLOW`'s comment says the same thing about the same column.

⚠️ **READY AND BLOCKED ARE DERIVED ON EVERY READ, NEVER STORED.** `tasks.ready()`
is the one declaration of "may this run now", asked over the rows already in hand
(the pool parameter exists for exactly this), so `load()` costs **one `qone` and one
`qall`** for a 3-node graph and the same for a 500-node one. A `state` column
holding `"ready"` would be a second answer that goes stale the instant an upstream
node settles, and the dispatcher would believe the stale one.

⚠️ **`dispatchable` IS NARROWER THAN READY, AND THE DIFFERENCE BELONGS HERE.** A
node is READY when the *graph* permits it; it is dispatchable when the graph permits
it **and** its row is still `pending` — because `queued` means a scheduler already
claimed it. Leaving that to the scheduler would put the claim rule in the layer that
has the race, and two workers reading READY at once is how one node runs twice.

⚠️ **RUNTIME GROWTH IS COUNTED, NOT STORED**: `growth = max(0, rows - total_steps)`.
`total_steps` is written **only** by `execstate.workflow_started()` — `workflow_step()`
physically cannot update that column — so it is a reliable record of *the graph as
declared*, and the difference is what was added while it ran. A counter of our own
would be a second number to keep, in the one column that degrades to `"{}"`.

⚠️ **A NAMING DEBT, WRITTEN DOWN RATHER THAN HIDDEN.** This module calls
`execstate.workflow_started()` / `workflow_step()` / `workflow_finished()` and writes
`tasks.CP_WORKFLOW`, and those are historical names for a generic run ledger that
predates the DAG core by five phases. They are the *only* feature words in the
package and they are **call sites, not logic** — nothing here branches on a feature,
and `test_dag.py` asserts that by inspecting branch conditions and defined names
rather than every occurrence of a string. Renaming the ledger would mean a migration
and a rewrite of `/recovery`, `crash.py` and both surfaces to buy a tidier word.

Nothing here raises into a turn: every write is guarded, every read degrades to an
empty `GraphState` — which reads as *no such run*, never as *every node is done*,
because `finished` is False on an empty state by construction.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from agent2 import config
from agent2.core import execstate as _exec
from agent2.core import sync
from agent2.core import tasks as _tasks

from . import model as _m
from . import validate as _v

#: Default `surface` recorded on a session opened by this store. A consumer passes
#: its own (`"workflow"`, `"ultracode"`) so `/tasks` and `/recovery` can say which
#: feature opened a plan — a label, never a branch.
SURFACE = "dag"


def _here() -> str:
    """This project's directory, for a caller that did not name one.

    ⚠️ **THE DEFAULT LIVES HERE BECAUSE AN UNSTAMPED SESSION IS AN INVISIBLE ONE.**
    `tasks.unfinished_sessions(cwd)` is the recovery *candidate* query, and both of
    its readers pass a project — `recovery.candidates()` uses `cwd or os.getcwd()`
    and `execstate.snapshot()` uses `workspace.root()`. A session opened with
    `cwd=""` is stored under the `''` shared sentinel, so it matches **neither**:
    an interrupted run is absent from `/recovery` and from `GET /api/recovery`'s
    `candidates` half, while `/workflow state` still reads perfectly — because the
    `exec_workflows` row is project-stamped independently by `execstate._project()`.
    Two derivations of "which project is this run in", one of them missing, and no
    error on either side. That shipped: **both** `instantiate()` call sites
    (`agent2cli._workflow_run`, `routes.api_run_workflow`) omitted `cwd=`.

    So it is defaulted **in the engine, not at the call site** — `budget.apply()`'s
    rule, for its reason: D4's dynamic planner and D5's UltraCode are two more
    callers, and "remember to pass the project" is exactly what a new one omits,
    invisibly until the run somebody needed to recover is the one that is not
    listed. A caller that names a `cwd` still wins; this only fills a silence.

    ⚠️ The raw root is right, not a normcased one: `tasks.open_session()` puts every
    `cwd` through `_project_key()` already, and that is the ONE canonicaliser
    (migration 10). Normalising here would be a second spelling of it.

    Total — a project directory that cannot be resolved leaves the old `''`, which
    is a session listed nowhere rather than a run that could not start.
    """
    try:
        from agent2.core import workspace as _ws
        return str(_ws.root())
    except Exception:
        return ""


@dataclass
class GraphRun:
    """The result of trying to persist a graph. A refusal is a RETURN VALUE.

    ⚠️ Never an exception, because both callers of the callers are printing
    surfaces: `projectdoc.apply()`'s rule. `node_tasks` is the node id → task id map
    this store just wrote, handed back so a caller can dispatch without re-reading.
    """

    ok: bool = False
    reason: str = ""
    run_id: str = ""
    session_id: str = ""
    name: str = ""
    node_tasks: dict = field(default_factory=dict)
    validation: _m.Validation | None = None
    #: Times this graph has been **extended**, carried back so a caller bounding
    #: re-planning does not have to re-read the run it just grew.
    #:
    #: ⚠️ NOT `schedule.Outcome.rounds`, and the two must never share a word: that
    #: one counts a pump's loop iterations over a graph nobody changed, this one
    #: counts changes to the graph itself. `HOLD_CODES` vs `REASONS`, for its reason.
    extensions: int = 0

    def to_payload(self) -> dict:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "name": self.name,
            "nodes": dict(self.node_tasks),
            "extensions": self.extensions,
            "validation": self.validation.to_payload() if self.validation else None,
        }


@dataclass
class NodeView:
    """One node as it stands right now: its row, its derived state, its reason.

    `state` is one of `model.STATES`; `status` is the *stored* `agent_tasks.status`
    it was derived from, kept beside it so nothing is lost in the projection — a
    `queued` row reads as READY with `dispatchable` False and `status` still saying
    it was claimed.
    """

    node: str
    task_id: str = ""
    title: str = ""
    state: str = _m.PENDING
    status: str = _tasks.TaskStatus.PENDING
    progress: float = 0.0
    error: str = ""
    #: The node ids this one is still waiting for. Only computed when BLOCKED —
    #: there is nothing to explain about a node that may already run.
    blocked_by: tuple[str, ...] = ()
    #: Dependencies that settled *unsuccessfully*. ⚠️ Readiness cannot tell success
    #: from settlement — `tasks.ready()` releases a node once every dependency is
    #: TERMINAL — so a node whose upstream FAILED reads READY and `dispatchable`,
    #: indistinguishable there from one whose upstream succeeded. This field is the
    #: only place that difference is stated, and it is how a scheduler declines work
    #: the graph is perfectly willing to hand it.
    upstream_failed: tuple[str, ...] = ()
    #: True when the graph permits it AND the row is unclaimed — see the docstring.
    dispatchable: bool = False
    instruction: str = ""
    kind: str = ""
    resource: str = ""
    priority: int = _tasks.DEFAULT_PRIORITY
    seq: int = 0
    #: Dependencies as **node** ids, so a caller never handles task ids.
    needs: tuple[str, ...] = ()
    attempts: int = 0
    #: The row was parked by `tasks.interrupt()` — a process died holding it — rather
    #: than held by a human. ⚠️ Both land on PAUSED, and a resume must tell them
    #: apart: releasing a crash-park is *continuing unfinished work*, releasing a
    #: deliberate hold is undoing somebody's decision. The distinguishing fact is
    #: durable and already written (`tasks.CP_STOPPED`, which `pause()` never
    #: writes), so this is read, never inferred from timing.
    interrupted: bool = False

    def to_payload(self) -> dict:
        return {
            "node": self.node,
            "task_id": self.task_id,
            "title": self.title,
            "state": self.state,
            "status": self.status,
            "progress": round(self.progress, 3),
            "error": self.error,
            "blocked_by": list(self.blocked_by),
            "upstream_failed": list(self.upstream_failed),
            "dispatchable": self.dispatchable,
            "kind": self.kind,
            "resource": self.resource,
            "priority": self.priority,
            "seq": self.seq,
            "needs": list(self.needs),
            "attempts": self.attempts,
            "interrupted": self.interrupted,
        }


@dataclass
class GraphState:
    """Everything true of one run *now*, derived from its rows on every read.

    ⚠️ **PROGRESS IS NEVER READ OUT OF THE RUN ROW.** `exec_workflows.state` holds a
    breadcrumb for a surface that has not asked for detail; the numbers here are
    recomputed from `agent_tasks`, so a run killed mid-flight reports what is true
    now rather than what was true when it died. That is also what makes recovery
    free: there is no stale copy to reconcile.
    """

    run_id: str
    name: str = ""
    status: str = ""
    session_id: str = ""
    chat_id: str = ""
    nodes: list[NodeView] = field(default_factory=list)
    exists: bool = False
    error: str = ""
    started_at: str = ""
    updated_at: str = ""
    #: Provenance, written once by `create()` and carried forward by `_state()`.
    source: str = ""
    schema: int = 0
    #: How many times this graph has been **extended** — bumped by `extend()` and
    #: carried forward by `_state()` exactly as `source`/`schema` are.
    #:
    #: ⚠️ A SECOND FACT FROM `growth`, and neither implies the other: `growth` counts
    #: nodes and this counts *decisions to add some*. A re-planning loop that adds one
    #: node twenty times is far under `AGENT2_DAG_MAX_MUTATIONS` and is still the
    #: runaway the ceiling exists to stop, so a consumer that bounds re-planning needs
    #: this number and cannot compute it from the rows: `_insert()` writes a fixed set
    #: of checkpoint keys and `Node.meta` deliberately never reaches a row, so there is
    #: nowhere on a node to record which batch it arrived in.
    #:
    #: ⚠️ Counted here, **bounded by the consumer**. How many times a *planner* may
    #: re-plan is a planner's fact, so the ceiling is injected at that layer the way
    #: `knob`/`label` are — the engine keeps the tally and judges nothing.
    #:
    #: ⚠️ AND IT IS NOT `schedule.Outcome.rounds`. That word is already taken, by a
    #: pump's loop count over a graph nobody changed; this is a count of changes to
    #: the graph. Two closed vocabularies that share no word — `HOLD_CODES` vs
    #: `REASONS`, for exactly its reason.
    extensions: int = 0
    #: Nodes the graph was created with (`exec_workflows.total_steps`).
    declared: int = 0

    # ── Derived ───────────────────────────────────────────────────────────────

    @property
    def total(self) -> int:
        return len(self.nodes)

    @property
    def growth(self) -> int:
        """Nodes added after the graph was created. See the module docstring."""
        return max(0, self.total - self.declared) if self.declared else 0

    @property
    def done(self) -> int:
        """Settled — successfully or not. What "12 of 20" means to a human."""
        return sum(1 for n in self.nodes if n.state in _m.TERMINAL)

    @property
    def completed(self) -> list[NodeView]:
        return [n for n in self.nodes if n.state == _m.COMPLETED]

    @property
    def failed(self) -> list[NodeView]:
        return [n for n in self.nodes if n.state in _m.UNSUCCESSFUL]

    @property
    def running(self) -> list[NodeView]:
        return [n for n in self.nodes if n.state == _m.RUNNING]

    @property
    def ready(self) -> list[NodeView]:
        return [n for n in self.nodes if n.state == _m.READY]

    @property
    def blocked(self) -> list[NodeView]:
        return [n for n in self.nodes if n.state == _m.BLOCKED]

    @property
    def paused(self) -> list[NodeView]:
        return [n for n in self.nodes if n.state == _m.PAUSED]

    @property
    def dispatchable(self) -> list[NodeView]:
        """Nodes a scheduler may claim right now, best-priority first.

        ⚠️ The graph's answer, and only the graph's: it says nothing about worker
        limits, resource contention or permissions. *"Never blindly run every READY
        node"* is the scheduler's rule, and this list is the input it filters.
        """
        out = [n for n in self.nodes if n.dispatchable]
        out.sort(key=lambda n: (n.priority, n.seq, n.node))
        return out

    @property
    def finished(self) -> bool:
        """Every node settled. ⚠️ False on an empty state, by construction."""
        return bool(self.nodes) and self.done == self.total

    @property
    def interrupted(self) -> list[NodeView]:
        """Nodes a dead process left parked — what a resume may release.

        ⚠️ **NOT `paused`, AND THE DIFFERENCE IS THE WHOLE POINT.** Both read PAUSED,
        and `unpause_run()` cannot tell them apart because it is a human asking for
        every hold to end. This list is the other question — *"continue unfinished
        work"* — and it is answered from the durable stamp `tasks.interrupt()` writes
        and `tasks.pause()` does not, so a crash-park is released and a deliberate
        hold is left exactly where its owner put it.
        """
        return [n for n in self.nodes if n.state == _m.PAUSED and n.interrupted]

    @property
    def waves(self) -> tuple[tuple[str, ...], ...]:
        """The topological levels of the graph these rows describe. Zero queries.

        THE parallelism the spec asks to be **exposed**: every id inside one wave has
        no dependency on any other id in that wave, so the graph permits them to run
        together — which is how a plan a human wrote as a flat list of steps becomes a
        graph with width, without the author declaring anything but `needs:`.

        ⚠️ **A GRAPH FACT, NEVER A DISPATCH DECISION.** `schedule.plan_next()` still
        decides what actually starts, because a wave knows nothing of worker limits,
        `Node.resource` or a kind's budget — *"never blindly run every READY node"*.
        Derived from `graph()`, so it costs no query and stays true for a run whose
        definition file has since been edited or deleted.
        """
        try:
            return _v.levels_for(self.graph().nodes)
        except Exception:      # an ordering hint may never cost a caller its state
            return ()

    @property
    def width(self) -> int:
        """The widest wave — how much parallelism this graph could ever offer."""
        return max((len(w) for w in self.waves), default=0)

    @property
    def settled_ids(self) -> tuple[str, ...]:
        """Node ids that will not run again — what `validate_mutation()` protects."""
        return tuple(n.node for n in self.nodes if n.state in _m.TERMINAL)

    def node(self, node_id: str) -> NodeView | None:
        want = _m.fold_id(node_id)
        for n in self.nodes:
            if n.node == want:
                return n
        return None

    def current(self) -> NodeView | None:
        """What "where is it up to" means: running first, then whatever may start."""
        return (self.running or self.ready or [None])[0]

    def graph(self) -> _m.Graph:
        """The graph itself, reconstructed from the rows. THE recovery primitive.

        Item 10 in one method — *"Restore DAG → restore states → restore checkpoints
        → validate external state → continue unfinished work"* — and the reason the
        original declaration does not have to be on disk for a run to be resumable.
        A file may have been edited or deleted since; what actually ran is what the
        rows say, and a resume against a re-read file would be a resume of a
        different plan.
        """
        return _m.Graph(
            name=self.name or self.run_id,
            version=self.schema or _m.SCHEMA_VERSION,
            source=self.source or "rows",
            nodes=tuple(_m.Node(id=n.node, title=n.title, instruction=n.instruction,
                                needs=tuple(n.needs), priority=n.priority,
                                resource=n.resource, seq=n.seq, kind=n.kind)
                        for n in self.nodes),
        )

    def to_payload(self) -> dict:
        cur = self.current()
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
            "declared": self.declared,
            "growth": self.growth,
            "extensions": self.extensions,
            "done": self.done,
            "failed": len(self.failed),
            "finished": self.finished,
            "width": self.width,
            "waves": [list(w) for w in self.waves],
            "current": cur.node if cur else "",
            "nodes": [n.to_payload() for n in self.nodes],
        }


# ── Creating a run ─────────────────────────────────────────────────────────────

def create(graph: _m.Graph, *, chat_id: str = "", cwd: str = "",
           workspace_id: str = "", model: str = "", mode: str = "",
           surface: str = SURFACE, goal: str = "", label: str = "graph",
           max_nodes: int | None = None,
           knob: str = "AGENT2_DAG_MAX_NODES",
           event: str = "graph_start") -> GraphRun:
    """Persist *graph* as one run plus N task rows. Validates first; writes or not.

    On any refusal — an unrunnable graph, a denied capability, the ledger switched
    off — **nothing at all is written** and `reason` says why. A half-written graph
    would be a plan whose dependencies no longer close, which deadlocks in silence.

    ⚠️ `event` is injected for `label`/`knob`'s reason. The **resource** is what
    invalidates a cache (`"tasks"`, always); the event word is the diagnostic label
    a `sync` subscriber sees, and `core.workflow.runner` has emitted
    `"workflow_start"` since Task 37. Deriving it from `label` would couple a bus
    name to prose, and hard-coding one here would rename an event a consumer's
    listeners may already be reading — silently, since nothing keys on it.

    ⚠️ Nodes are inserted in **topological order** so a node's `dependencies` can
    hold real task ids by the time its row exists. That remap (node id → task id)
    happens here and nowhere else: `tasks.ready()` *ignores* an unresolvable
    dependency by design, so a mistranslated id does not error — it releases the
    node to run early, with a plausible result and nothing to point at.
    """
    if not config.EXEC_PERSIST:
        # ⚠️ Refused BEFORE the session row. `workflow_started()` returns `""` when
        # the ledger is off, and a run with no id is a run no surface can read and
        # whose nodes crash recovery cannot find — so the honest answer is a refusal
        # naming the switch, not orphan rows that look like an abandoned plan.
        return GraphRun(reason=f"{label}s need the execution ledger "
                               f"(AGENT2_EXEC_PERSIST=0 is set)")

    # The capability gate, asked live. A run drives the agent, so it is `chat`; the
    # tools a node's work reaches for are gated again, per call, by `dispatch_tool`.
    # ⚠️ Guarded because a missing gate may not be the reason a plan cannot start.
    try:
        from agent2.core import permissions as _perm
        if not _perm.process_allows(_perm.CAP_CHAT):
            return GraphRun(reason="not permitted: this process may not run agent turns")
    except Exception:
        pass

    v = _v.validate(graph, max_nodes=max_nodes, knob=knob, label=label)
    if not v.ok:
        return GraphRun(reason=v.summary() or f"the {label} definition is not runnable",
                        validation=v,
                        name=getattr(graph, "name", "") if graph else "")

    order = [nid for level in v.levels for nid in level]
    by_id = {n.id: n for n in graph.nodes}

    session_id = _tasks.open_session(
        chat_id=chat_id, cwd=cwd or _here(), workspace_id=workspace_id,
        model=model, mode=mode,
        goal=(goal or graph.description
              or f"{label}: {graph.name}")[:_tasks.MAX_TEXT],
        surface=surface,
    )
    # ⚠️ `state` stays small on purpose — `execstate._state_json` degrades a blob over
    # `MAX_STATE_CHARS` to `"{}"`, so anything stored here has to be worth losing. The
    # topological levels are NOT stored: they are re-derivable from the rows (`seq`,
    # `dependencies`), and storing them would push a large graph past the cap and
    # silently drop `source` and `schema` along with them.
    run_id = _exec.workflow_started(
        graph.name, session_id=session_id, chat_id=chat_id, total_steps=len(order),
        state={"source": graph.source, "schema": graph.version},
    )
    if not run_id:
        _tasks.close_session(session_id, _tasks.SESSION_ABANDONED)
        return GraphRun(reason=f"the {label} run could not be recorded")

    node_tasks = _insert(graph, order, by_id, run_id=run_id, session_id=session_id,
                         start=0)
    _tasks.touch_session(session_id)
    sync.notify("tasks", session_id=session_id, event=event)
    run = GraphRun(ok=True, run_id=run_id, session_id=session_id, name=graph.name,
                   node_tasks=node_tasks, validation=v)
    advance(run_id)
    return run


def _insert(graph: _m.Graph, order, by_id, *, run_id: str, session_id: str,
            start: int, node_tasks: dict | None = None) -> dict:
    """Write *order*'s nodes as task rows. THE one writer, shared by create/extend.

    `node_tasks` may carry ids written by an earlier call, which is what lets a node
    added at runtime depend on one that has already finished — the remap has to see
    the whole run, not just this batch.
    """
    mapped: dict[str, str] = dict(node_tasks or {})
    for offset, nid in enumerate(order):
        node = by_id.get(nid)
        if node is None:                    # unreachable: order comes from levels
            continue
        checkpoint = {_tasks.CP_WORKFLOW: run_id, _tasks.CP_NODE: nid}
        # ⚠️ Written only when set, so a plain node's checkpoint stays the two keys
        # Task 37 wrote and a `checkpoint_view` reader sees no new noise.
        if node.kind:
            checkpoint[_tasks.CP_KIND] = node.kind
        if node.resource:
            checkpoint[_tasks.CP_RESOURCE] = node.resource
        row = _tasks.create(
            session_id, node.title or node.id,
            description=node.instruction,
            priority=node.priority,
            dependencies=[mapped[d] for d in node.needs if d in mapped],
            seq=start + offset,
            checkpoint=checkpoint,
            notify=False,
        )
        mapped[nid] = row.id
    return mapped


# ── Reading a run ──────────────────────────────────────────────────────────────

def _node_of(task: _tasks.Task) -> str:
    """The node id a task row carries, or `""` — total, and it never guesses.

    A task whose checkpoint holds no node id is simply not a graph node (an
    `update_todo` row in the same session, or a row written before Task 37). It is
    excluded rather than inferred from its title, because a title is prose a human or
    a model may rewrite and a node id is a key two tables agree on.
    """
    try:
        cp = task.checkpoint if isinstance(task.checkpoint, dict) else {}
        return _m.fold_id(cp.get(_tasks.CP_NODE) or "")
    except Exception:
        return ""


def _label_of(task: _tasks.Task, key: str) -> str:
    try:
        cp = task.checkpoint if isinstance(task.checkpoint, dict) else {}
        return str(cp.get(key) or "")[:_m.MAX_LABEL]
    except Exception:
        return ""


def _stopped_of(task: _tasks.Task) -> bool:
    """Was this row parked by an interruption? The one reader of that fact.

    ⚠️ `tasks.interrupt()` writes `CP_STOPPED` and then pauses; `tasks.pause()` writes
    nothing. That asymmetry is the only durable difference between "a process died
    holding this" and "somebody held this", and it already existed — so the two are
    told apart by *reading*, never by comparing timestamps or guessing from a run's
    status. A checkpoint that cannot be read at all is not a claim that a node was
    interrupted, so the answer is False.
    """
    try:
        cp = task.checkpoint if isinstance(task.checkpoint, dict) else {}
        return bool(cp.get(_tasks.CP_STOPPED))
    except Exception:
        return False


def _state_of(task: _tasks.Task, ready_ids: set[str]) -> str:
    """One stored status → one of `model.STATES`. THE projection.

    ⚠️ `paused` is tested **before** readiness and that ordering is the rule, not an
    accident: `tasks.ready()` includes a paused row because PAUSED is *open*, so
    asking readiness first would report a deliberately-held node as free to run —
    and a resume would start it. `queued` keeps its READY reading (the graph really
    does permit it) and loses only `dispatchable`, because a claim is the
    scheduler's fact and the stored `status` still carries it.
    """
    status = task.status
    if status in _tasks.TERMINAL:
        return status                       # completed · failed · cancelled · skipped
    if status == _tasks.TaskStatus.RUNNING:
        return _m.RUNNING
    if status == _tasks.TaskStatus.PAUSED:
        return _m.PAUSED
    return _m.READY if task.id in ready_ids else _m.BLOCKED


def load(run_id: str) -> GraphState:
    """Everything about one run, derived from its rows. Never raises.

    ⚠️ **Two queries, and only two** — the run row, then the session's tasks. The
    runnable subset comes from `tasks.ready(session_id, pool)` over the rows already
    in hand: the one declaration of "ready", asked without a second read. Re-deriving
    the predicate here would be the drift; re-reading the rows to call it would be
    the waste. The cost is therefore **flat in node count**, which matters because a
    per-node read is invisible at the size a developer tests with and 500 round trips
    per turn at the size a planner generates.
    """
    st = GraphState(run_id=str(run_id or ""))
    if not st.run_id:
        return st
    try:
        row = _exec.workflow(st.run_id)
    except Exception:
        row = None
    if not row:
        return st
    st.exists = True
    st.name = str(row.get("name") or "")
    st.status = str(row.get("status") or "")
    st.session_id = str(row.get("session_id") or "")
    st.chat_id = str(row.get("chat_id") or "")
    st.error = str(row.get("error") or "")
    st.started_at = str(row.get("started_at") or "")
    st.updated_at = str(row.get("updated_at") or "")
    try:
        st.declared = max(0, int(row.get("total_steps") or 0))
    except (TypeError, ValueError):
        st.declared = 0
    # Provenance, parsed once from the row already in hand — no second query. A
    # `state` column that degraded to `{}` leaves both fields at their defaults,
    # which reads as "not stated" rather than as a claim about where this came from.
    try:
        stored = json.loads(row.get("state") or "{}")
        if isinstance(stored, dict):
            st.source = str(stored.get("source") or "")
            st.schema = int(stored.get("schema") or 0)
            st.extensions = max(0, int(stored.get("extensions") or 0))
    except Exception:
        pass
    if not st.session_id:
        return st

    try:
        pool = _tasks.list_tasks(st.session_id)
        ready_ids = {t.id for t in _tasks.ready(st.session_id, pool)}
    except Exception:
        return st

    by_task = {t.id: t for t in pool}
    node_by_task = {t.id: _node_of(t) for t in pool}
    for task in pool:
        nid = node_by_task.get(task.id) or ""
        if not nid:
            continue
        state = _state_of(task, ready_ids)
        blocked_by = tuple(
            node_by_task.get(b.id) or b.id for b in _tasks.blockers(task, pool)
        ) if state == _m.BLOCKED else ()
        upstream = () if state in _m.TERMINAL else tuple(
            node_by_task.get(dep) or dep
            for dep in task.dependencies
            if dep in by_task and by_task[dep].is_terminal
            and by_task[dep].status != _tasks.TaskStatus.COMPLETED
        )
        st.nodes.append(NodeView(
            node=nid, task_id=task.id, title=task.title, state=state,
            status=task.status, progress=task.progress, error=task.error,
            blocked_by=blocked_by, upstream_failed=upstream,
            dispatchable=(state == _m.READY
                          and task.status == _tasks.TaskStatus.PENDING),
            instruction=task.description,
            kind=_label_of(task, _tasks.CP_KIND),
            resource=_label_of(task, _tasks.CP_RESOURCE),
            priority=task.priority, seq=task.seq,
            needs=tuple(node_by_task.get(dep) or dep for dep in task.dependencies
                        if dep in by_task),
            attempts=task.attempt_count,
            interrupted=_stopped_of(task),
        ))
    # Declaration order, which for a graph's own session is `seq` — the topological
    # order `create()` wrote, extended at the end by anything added since.
    st.nodes.sort(key=lambda n: (n.seq, n.node))
    return st


def plan(graph: _m.Graph, *, max_nodes: int | None = None,
         knob: str = "AGENT2_DAG_MAX_NODES",
         label: str = "graph") -> tuple[_m.Validation, list[NodeView]]:
    """What *graph* would look like before anything is written. Nothing is written.

    The dry run: `levels[0]`'s nodes come back READY and the rest BLOCKED, so a
    surface can show an execution plan — which the spec **requires** between
    `/workflow run` and execution — without a row, a session or a permission.
    ⚠️ `PENDING` is what a node in an unrunnable graph gets: there is no order to
    read from, and calling those nodes BLOCKED would imply a dependency chain the
    validator just refused to believe in.
    """
    v = _v.validate(graph, max_nodes=max_nodes, knob=knob, label=label)
    first = set(v.levels[0]) if v.levels else set()
    out: list[NodeView] = []
    for n in getattr(graph, "nodes", ()):
        state = _m.PENDING if not v.ok else (_m.READY if n.id in first else _m.BLOCKED)
        out.append(NodeView(
            node=n.id, title=n.title, state=state, status=_tasks.TaskStatus.PENDING,
            dispatchable=state == _m.READY, instruction=n.instruction, kind=n.kind,
            resource=n.resource, priority=n.priority, seq=n.seq,
            needs=tuple(n.needs),
        ))
    out.sort(key=lambda n: (n.seq, n.node))
    return v, out


# ── Growing a run while it is live ─────────────────────────────────────────────

def extend(run_id: str, nodes=(), *, edges=(), label: str = "graph",
           max_nodes: int | None = None, knob: str = "AGENT2_DAG_MAX_NODES",
           max_growth: int | None = None) -> GraphRun:
    """Add nodes (and edges) to a live run. Validated before anything is written.

    THE dynamic-modification primitive every later phase builds on — a failed test
    earning a diagnosis node, a re-plan adding a regression check. Three properties
    it guarantees, in this order:

    1. **completed work stays completed.** New rows are *inserted*; nothing existing
       is rewritten, so `tasks.set_status()`'s refusal to move a terminal row is
       never even approached and no finished node can be re-run.
    2. **the change is judged before it is accepted.** The candidate graph is built
       in memory, `validate_mutation()` compares it against what has already
       settled, and only a clean verdict reaches the database. A cycle introduced by
       a new edge is refused here, because after the write it would be a run that
       simply stops advancing with nothing logged.
    3. **it cannot grow forever.** `AGENT2_DAG_MAX_MUTATIONS` bounds what a
       re-planning loop may add, and the count is derived from the rows — see the
       module docstring. A successful growth also bumps `GraphState.extensions`, which is
       the *other* half of that bound and the one a consumer supplies the ceiling for:
       nodes and decisions-to-add-nodes are two different runaways.

    ⚠️ New nodes may depend on nodes that have already finished, and that is not a
    contradiction: `tasks.ready()` releases on *settled*, so such a dependency is
    satisfied the moment the row is written. It is how "re-run the tests after this
    fix" attaches to a build that already succeeded.
    """
    st = load(run_id)
    if not st.exists:
        return GraphRun(reason=f"no such {label} run")
    if not st.session_id:
        return GraphRun(reason=f"this {label} run has no task session")
    if st.finished:
        # A settled run has closed its session and written its verdict. Extending it
        # would resurrect a plan whose totals have already been reported.
        return GraphRun(reason=f"this {label} run has already finished",
                        run_id=st.run_id, session_id=st.session_id, name=st.name,
                        extensions=st.extensions)

    if max_growth is None:
        max_growth = config.DAG_MAX_MUTATIONS
    before = st.graph()
    after = before.with_nodes(nodes)
    for src, dst in list(edges or ()):
        after = after.with_edge(src, dst)
    fresh = [nid for nid in after.ids() if nid not in set(before.ids())]

    v = _v.validate_mutation(
        before, after, settled=st.settled_ids, max_nodes=max_nodes, knob=knob,
        label=label, growth=st.growth + len(fresh), max_growth=max_growth)
    if not v.ok:
        return GraphRun(reason=v.summary() or f"the {label} change is not runnable",
                        run_id=st.run_id, session_id=st.session_id, name=st.name,
                        validation=v, extensions=st.extensions)
    if not fresh and not list(edges or ()):
        # ⚠️ Nothing changed, so this is NOT a round. A re-planner whose model returned
        # the plan it already has must not be charged for it — otherwise a bounded loop
        # spends its allowance on the one answer that cost the run nothing.
        return GraphRun(ok=True, run_id=st.run_id, session_id=st.session_id,
                        name=st.name, validation=v, extensions=st.extensions,
                        node_tasks={n.node: n.task_id for n in st.nodes})

    # ⚠️ Existing rows keep their task ids, so the remap starts from what is already
    # stored — a new node depending on an existing one must resolve to that row and
    # not to a second copy of it.
    mapped = {n.node: n.task_id for n in st.nodes if n.task_id}
    by_id = {n.id: n for n in after.nodes}
    order = [nid for level in (v.levels or ()) for nid in level if nid in set(fresh)]
    mapped = _insert(after, order, by_id, run_id=st.run_id,
                     session_id=st.session_id,
                     start=max((n.seq for n in st.nodes), default=-1) + 1,
                     node_tasks=mapped)

    # An edge added between two *existing* nodes is a `dependencies` rewrite, which
    # is the one case that touches a stored row. `validate_mutation()` has already
    # refused to let it touch a settled one.
    _rewire(st, after, mapped)

    _tasks.touch_session(st.session_id)
    sync.notify("tasks", session_id=st.session_id, event="graph_extend")
    # ⚠️ The tally is bumped on the state `advance()` writes FROM, never after it.
    # `advance(run_id)` re-loads, so it would read the old count out of the very
    # blob it is about to replace and the increment would be lost on every call —
    # `execstate.workflow_step()` replaces that column, it does not merge (see
    # `_state()`). This re-load is the one `advance(st.run_id)` was already paying.
    grown = load(st.run_id)
    grown.extensions = st.extensions + 1
    advance(grown.run_id, state=grown)
    return GraphRun(ok=True, run_id=st.run_id, session_id=st.session_id, name=st.name,
                    node_tasks=mapped, validation=v, extensions=grown.extensions)


def _rewire(st: GraphState, after: _m.Graph, mapped: dict) -> None:
    """Bring an existing row's `dependencies` in line with *after*. Guarded.

    Only reached for an edge added between two nodes that already had rows — a node
    `_insert` just wrote already carries the right list. `tasks.set_dependencies`
    is the writer (it no-ops when nothing changed), and `validate_mutation()` has
    already refused to let this touch a **settled** node.
    """
    for view in st.nodes:
        node = after.node(view.node)
        if node is None or not view.task_id:
            continue
        want = [mapped[d] for d in node.needs if d in mapped]
        if want == list(view.needs and [mapped.get(d, d) for d in view.needs]):
            continue
        try:
            _tasks.set_dependencies(view.task_id, want, notify=False)
        except Exception:
            continue


# ── Advancing, settling, resuming ──────────────────────────────────────────────

def _state(st: GraphState, **progress) -> dict:
    """The `exec_workflows.state` blob — progress, plus the provenance carried on.

    ⚠️ ONE BUILDER, because `execstate.workflow_step()` / `workflow_finished()`
    **replace** that column rather than merging into it. `source` and `schema` are
    written once by `create()` and would otherwise be gone by the first node
    transition — and `create()` gives up storing the topological levels specifically
    in order to keep them, so losing them one call later would spend that decision
    for nothing. Re-reading the row to merge would cost a query per transition; these
    values are already on the state object the transition was derived from.
    """
    out = dict(progress)
    if st.source:
        out["source"] = st.source
    if st.schema:
        out["schema"] = st.schema
    if st.extensions:
        out["extensions"] = st.extensions
    return out


def advance(run_id: str, *, state: GraphState | None = None) -> GraphState:
    """Refresh the run's breadcrumb from its rows, and settle it if it is done.

    Called after every transition. It writes no state a reader depends on — `load()`
    re-derives everything — so a failed write costs a stale breadcrumb and never a
    wrong answer.
    """
    st = state if state is not None else load(run_id)
    if not st.exists:
        return st
    cur = st.current()
    try:
        _exec.workflow_step(
            st.run_id, step=(cur.node if cur else ""), step_index=st.done,
            state=_state(st, done=st.done, total=st.total, failed=len(st.failed),
                         running=[n.node for n in st.running],
                         ready=[n.node for n in st.ready]),
        )
    except Exception:
        pass
    if st.finished:
        return settle(run_id, state=st)
    return st


def settle(run_id: str, *, state: GraphState | None = None) -> GraphState:
    """Close the run and its session. Idempotent — a second call writes nothing new.

    The error line names **every** unsuccessful node rather than the first: a graph
    fans out, so "which branch failed" is usually more than one answer and a reader
    who sees only the first would fix one of three.
    """
    st = state if state is not None else load(run_id)
    if not st.exists:
        return st
    failed = st.failed
    try:
        _exec.workflow_finished(
            st.run_id, ok=not failed,
            error="; ".join(f"{n.node}: {n.error or n.state}" for n in failed)[:400]
                  if failed else "",
            state=_state(st, done=st.done, total=st.total, failed=len(failed)),
        )
    except Exception:
        pass
    if st.session_id:
        try:
            _tasks.close_session(st.session_id)
        except Exception:
            pass
    return st


def resumable(run_id: str) -> list[NodeView]:
    """What a resume would pick up: nodes running when the process died, plus ready.

    ⚠️ **A COMPLETED NODE IS NEVER HERE, AND THAT IS THE WHOLE POINT** — the states
    are read from the rows, and a settled row is settled. That is what makes *"do
    not restart completed work"* a property of the data rather than a promise in a
    resume path. A PAUSED node is likewise absent: it was held on purpose, and a
    crash is not permission to start it — a node parked *by* the crash is a different
    fact, and `release_interrupted()` is the one thing allowed to act on it.
    """
    st = load(run_id)
    return [n for n in st.nodes if n.state in (_m.READY, _m.RUNNING)]


# ── Node state transitions (item 7) ────────────────────────────────────────────
#
# Every transition is `core.tasks`' — this layer only translates a node id into the
# task id that holds it and refreshes the run's breadcrumb afterwards. ⚠️ There is
# no second status writer: `tasks._apply()` stays the one place a row's status
# changes, which is what keeps its refusal to move a settled row (`set_status`'s
# `force`) in force for a graph node too. A `mark(..., COMPLETED)` on a finished
# node is therefore a no-op rather than a re-run, without this module knowing why.

def mark(run_id: str, node: str, status: str, *, state: GraphState | None = None,
         result=None, error=None, progress=None,
         advance_run: bool = True) -> GraphState:
    """Move one node to a stored *status*, then refresh the run. Never raises.

    Returns the run's state **after** the transition, because that is the query the
    caller was about to make anyway — a scheduler needs the next dispatch set, and a
    surface needs the new counts. ⚠️ `state=` lets a caller that already holds a
    state skip the read: one `load()` per transition rather than two, the same
    reason `tasks.ready()` takes a pool and `set_dependencies()` takes a row.
    """
    st = state if state is not None else load(run_id)
    view = st.node(node)
    if view is None or not view.task_id:
        return st
    try:
        _tasks.set_status(view.task_id, status, result=result, error=error,
                          progress=progress)
    except Exception:
        return st
    return advance(st.run_id) if advance_run else load(st.run_id)


def claim(run_id: str, node: str, *, state: GraphState | None = None,
          worker_id: str = "", execution_id: str = "") -> bool:
    """Take a dispatchable node for a worker. False when it was not takeable.

    ⚠️ **THE CLAIM IS A STATUS WRITE, AND IT COMES FIRST.** `pending` → `queued`
    is what makes `dispatchable` False for everyone else, so two workers reading the
    same READY list cannot both start the same node — the row is the lock. Recording
    the worker and execution id afterwards is bookkeeping `/recovery` reads; doing it
    first would leave a window in which the node still looks free.
    """
    st = state if state is not None else load(run_id)
    view = st.node(node)
    if view is None or not view.task_id or not view.dispatchable:
        return False
    try:
        if _tasks.queue(view.task_id, notify=False) is None:
            return False
        _tasks.claim(view.task_id, worker_id=worker_id, execution_id=execution_id)
    except Exception:
        return False
    return True


def pause_run(run_id: str, *, reason: str = "") -> GraphState:
    """Hold every node that has not started. Running nodes are left alone.

    ⚠️ Pausing a graph is not cancelling one, so a node already in flight keeps
    running: it holds a subprocess, a model call or an MCP session, and tearing that
    down to honour a preference about *starting* work is a destructive act nobody
    asked for (`integrations/state.py` documents the same rule for a live bridge).
    The held nodes read as PAUSED, which `_state_of` tests before readiness, so
    nothing here can be dispatched while the hold stands.

    ⚠️ Unrelated to `/pause`, which parks a **conversation**. This is item 17's
    graph-level hold, and the user's standing rule is that resuming *execution* must
    never be something a human has to ask for.
    """
    st = load(run_id)
    for view in st.nodes:
        if view.state in (_m.READY, _m.BLOCKED) and view.task_id:
            try:
                _tasks.pause(view.task_id, result=reason or None, notify=False)
            except Exception:
                continue
    if st.session_id:
        sync.notify("tasks", session_id=st.session_id, event="graph_pause")
    return advance(st.run_id)


def unpause_run(run_id: str) -> GraphState:
    """Release every held node back to PENDING. Readiness is re-derived, not stored.

    A node that was paused while blocked comes back blocked, because BLOCKED was
    never written down — `load()` asks `tasks.ready()` again over the rows as they
    now stand. That is why a hold can be released safely after an upstream node
    failed in the meantime: the graph, not this function, decides what may run.
    """
    st = load(run_id)
    for view in st.nodes:
        if view.state == _m.PAUSED and view.task_id:
            try:
                _tasks.set_status(view.task_id, _tasks.TaskStatus.PENDING,
                                  notify=False)
            except Exception:
                continue
    if st.session_id:
        sync.notify("tasks", session_id=st.session_id, event="graph_resume")
    return advance(st.run_id)


def release_interrupted(run_id: str) -> list[NodeView]:
    """Return only the crash-parked nodes to PENDING. A human's hold is untouched.

    THE automatic half of item 10 — *"continue unfinished work"* — and the reason the
    user's standing rule holds: *"recovery is automatic … the user should never need
    to run `/resume` just to prevent completed tasks from executing again."* A process
    that died holding a node left it PAUSED with `CP_STOPPED` stamped on it; this
    releases exactly those, so the graph re-derives readiness for them on the next
    read and the run carries on where it stopped.

    ⚠️ **`unpause_run()` IS THE OTHER FUNCTION, AND THEY MAY NOT MERGE.** That one is
    a human saying *release this graph*, so it releases every hold; this one is
    recovery saying *finish what was in flight*, so it may release nothing a human
    chose. One function with a flag would put the decision at the call site, and the
    call site that got it wrong would silently start work somebody had stopped.

    ⚠️ A COMPLETED NODE IS NEVER HERE — `interrupted` is derived from PAUSED rows
    alone, so `store.resumable()`'s guarantee is untouched and nothing already
    finished can be handed back. Returns the views it released, so a surface can say
    what happened rather than implying more than it did.

    ⚠️ **RELEASING CLEARS `CP_STOPPED`, AND THAT IS PART OF THE RELEASE.** The mark is
    the *only* durable difference between a crash-park and a human's hold, so a node
    freed here and later paused on purpose would come back reading PAUSED **and**
    interrupted — and the next call would un-pause a hold somebody chose. The clearing
    lives in `tasks.clear_stopped()` because `tasks.interrupt()` is the only writer of
    the key, and a deletion expressed here would be a second opinion about how a
    checkpoint is rewritten. It is best-effort in the same direction as the status
    write: the row is already PENDING and dispatchable, so a failed clear costs the
    *distinction* on a later hold, never the recovery.
    """
    st = load(run_id)
    freed: list[NodeView] = []
    for view in st.interrupted:
        if not view.task_id:
            continue
        try:
            _tasks.set_status(view.task_id, _tasks.TaskStatus.PENDING, notify=False)
        except Exception:
            continue
        try:
            _tasks.clear_stopped(view.task_id, notify=False)
        except Exception:
            pass
        freed.append(view)
    if freed and st.session_id:
        sync.notify("tasks", session_id=st.session_id, event="graph_recover")
    return freed


def cancel_run(run_id: str, *, reason: str = "cancelled") -> GraphState:
    """Cancel every still-open node and settle the run. Completed work is untouched.

    `tasks.cancel_open()` is one UPDATE whose WHERE clause names the open statuses,
    so it physically cannot regress a finished node — which is why cancelling a
    half-done graph leaves a truthful record of what did happen rather than a run
    that claims it was all abandoned.
    """
    st = load(run_id)
    if st.session_id:
        try:
            _tasks.cancel_open(st.session_id, reason)
        except Exception:
            pass
    return settle(st.run_id)


# ── Listing ────────────────────────────────────────────────────────────────────

def runs(*, project=None, chat_id: str = "", session_id: str = "",
         active_only: bool = False, limit: int = 20) -> list[dict]:
    """Recent run rows, newest first. Rows, not states — cheap by design."""
    try:
        return _exec.workflows(project=project, chat_id=chat_id,
                               session_id=session_id, active_only=active_only,
                               limit=limit)
    except Exception:
        return []


def live(*, project=None, chat_id: str = "") -> GraphState | None:
    """The one unsettled run here, if any. One indexed query, then `load()`."""
    rows = runs(project=project, chat_id=chat_id, active_only=True, limit=1)
    if not rows:
        return None
    st = load(str(rows[0].get("id") or ""))
    return st if st.exists else None


def stats() -> dict:
    """Counters for `/health` and `GET /api/health`. Never raises."""
    try:
        active = len(runs(project=_exec.ANY_PROJECT, active_only=True, limit=50))
        recent = len(runs(project=_exec.ANY_PROJECT, limit=50))
    except Exception:
        active = recent = 0
    return {
        "max_nodes": config.DAG_MAX_NODES,
        "max_mutations": config.DAG_MAX_MUTATIONS,
        "active": active,
        "recent": recent,
    }
