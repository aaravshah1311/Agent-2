# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2.core.dag.schedule
────────────────────────
THE dispatcher — Phase D2. `store.GraphState.dispatchable` says which nodes *the
graph* would allow to start; this module decides which of them actually do, and the
one sentence it exists to enforce is the spec's own: **"never blindly run every
READY node"**.

Three steps, three names, and each one is useful on its own:

| Step | Writes | Answers |
|---|---|---|
| `plan_next(state)` | nothing | which nodes may start right now, and **why the rest may not** |
| `dispatch(run_id)` | the claim | which of them this worker actually took |
| `run(run_id, worker)` | everything | drives a whole graph to a verdict, bounded |

⚠️ **IT DERIVES NO GRAPH FACT OF ITS OWN.** Readiness is `tasks.ready()`, projected
by `store.load()`; the order is `(priority, seq)`, the key `tasks.ready()` and
`validate.levels_for()` already share; the lock is `store.claim()`; every status
write is `tasks._apply()` through `store.mark()`. A second readiness predicate here
would release a blocked node with a plausible result and no error anywhere, which is
the failure `tasks.ready()`'s own docstring is shaped around. What this module owns is
*restraint* — the ceilings, the resource exclusion and the report of what they held.

⚠️ **THE FIVE SPEC CEILINGS ARE TWO SHAPES, NOT FIVE FIELDS** (`config.DAG_*`):
`max_workers` and `max_concurrent_tasks` bound a run, `max_concurrent_commands` and
`max_concurrent_mcp_calls` bound one **kind** of node at a time, and
`max_model_calls` bounds what a run may **spend** on one kind over its whole life.
Two label tables express all three, so every decision here is a `dict.get(kind)` and
never an `if kind ==` — a field per node type would be the `if workflow:` of
concurrency, and this package is asserted to contain none.

⚠️ **IN FLIGHT IS DERIVED FROM THE ROWS, NEVER COUNTED IN MEMORY** — RUNNING, plus
READY-and-not-dispatchable, which is precisely a node somebody has claimed and not
yet settled. Two consequences, both load-bearing: the ceilings are honoured across
*processes* (dual mode is two processes over one `agent2.db`, and an in-memory
counter would let each half spend the whole allowance), and a pump that resumes a
crashed run cannot hand out slots that are still occupied.

⚠️ **THE CLAIM IS TAKEN AGAINST A FRESH READ, NEVER THE PLANNING STATE.** Planning
reads once and decides; a claim is a lock, and a lock taken against a state old
enough to have missed another pump's claim is not a lock. Two indexed reads per
dispatched node is what that costs, and the count is bounded by the very ceilings
being enforced.

⚠️ **NOTHING IS HELD IN SILENCE.** Every dispatchable node that was not dispatched
carries a hold code — `upstream_failed` · `workers` · `max_running` · `kind_limit` ·
`kind_budget` · `resource_held` · `claimed` — because a node that is ready, absent
from the running set and unexplained is indistinguishable from a scheduler that lost
it, and that is the one failure a user cannot debug. Item 18 is a *report*, not a
check.

⚠️ **A NODE WHOSE DEPENDENCY FAILED IS DECLINED, AND THAT IS THIS MODULE'S CALL TO
MAKE.** `tasks.ready()` releases a node once its dependencies have *settled*, so a
node behind a failure reads READY and dispatchable — `NodeView.upstream_failed` is
the only place the difference is stated, and D1 states it precisely so a scheduler
can decline work the graph is willing to hand over. Declining is what SKIPPED is for:
`plan_next()` holds the node with `upstream_failed` (it writes nothing) and the pump
records the SKIPPED transition, so a failed branch **finishes** instead of holding a
run open against nodes `ready()` will offer forever. `settle()` still names every
skipped node, so nothing is quietly dropped — and `Limits.skip_failed_upstream=False`
is how a consumer that wants best-effort execution says so, injected rather than
branched on.

⚠️ **A RETRY IS BOUNDED AND CLASSIFIED, AND IT IS ASKED BEFORE THE FAILURE IS
RECORDED.** `core/recovery/classify.py` already owns "may this operation be repeated"
and `classify._assess_task()` answers `R_NON_RECOVERABLE` for any row that is already
settled — so asking after `mark(FAILED)` would make every retry structurally
impossible while looking correct. The order is: worker reports → assess the still-open
row → `verify()` if it says verification is required → `safe_to_continue()` (which
consults the live permission gate, so a retry can no more bypass it than the first
attempt could) → then, and only then, PENDING or FAILED. `config.DAG_MAX_ATTEMPTS`
bounds how many times something already judged safe may be tried; either half alone
is the blind retry the spec forbids.

⚠️ **A TIMEOUT RECORDS ITS VERDICT BEFORE IT SIGNALS THE WORKER**, because a Python
thread cannot be killed. `cli/state.trigger_cancel()`'s order, for its reason: kill
first and the worker's own late verdict lands on the row instead, so a killed node
reports whatever it happened to return. `set_status()`' refusal to move a settled row
is what makes "whichever verdict lands first wins" true rather than hopeful.

⚠️ **CANCELLATION GOES THROUGH THE PATH THAT ALREADY EXISTS** — `store.cancel_run()`,
which is `tasks.cancel_open()` plus `settle()`. Record, then signal. Pause is
`store.pause_run()` / `unpause_run()` and it is **not** `/pause`: that parks a
conversation, and the user's standing rule is that resuming *execution* must never be
something a human has to ask for. A node already in flight when a graph is paused
keeps running; the hold gates *starting*.

⚠️ **BOUNDED THREADS, AND `0` MEANS INLINE.** `AGENT2_DAG_MAX_WORKERS=0` runs every
node on the calling thread, one at a time — `core/scheduler.py`'s DISABLED posture,
and the reason the pump treats inline execution as "a thread that has already
finished" rather than as a second code path. `core/scheduler.py` is not duplicated
here and must not be: it bounds *turns*, whose queue outlives any one graph, while
this bounds the nodes of one run and dies with it.

Everything is total. `plan_next()` degrades to "dispatch nothing, and say a ceiling
could not be read"; `run()` refuses a missing run by *returning* `reason="no_run"`;
a worker that raises is one FAILED node with the exception text as its error, never
an exception in the caller's turn. That is what the BLE001/S110 allowance in
`pyproject.toml` buys, and it is why the allowance is scoped to this file and
`store.py` rather than to the package.
"""

from __future__ import annotations

import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from queue import Empty, SimpleQueue

from agent2 import config
from agent2.core import tasks as _tasks

from . import model as _m
from . import store as _store

# ── Vocabulary ────────────────────────────────────────────────────────────────

#: Default `worker_id` recorded on a claimed row. A human reading `/recovery` should
#: be able to tell "the graph engine was holding this" from "a turn was holding this".
WORKER = "dag"

# Hold codes — item 18's report. A node in `dispatchable` that is absent from
# `Plan.slots` carries exactly one of these, and `HOLD_CODES` is what a surface
# renders from rather than a string it invents.
H_WORKERS = "workers"            # this pump has no free execution slot this round
H_RUNNING = "max_running"        # `max_concurrent_tasks` — the run-wide ceiling
H_KIND = "kind_limit"            # this kind's concurrency ceiling is full
H_BUDGET = "kind_budget"         # this kind's LIFETIME allowance is spent
H_RESOURCE = "resource_held"     # another in-flight node names the same resource
H_CLAIMED = "claimed"            # the row was taken between planning and claiming
H_UPSTREAM = "upstream_failed"   # a dependency settled unsuccessfully — see below
HOLD_CODES = (H_UPSTREAM, H_WORKERS, H_RUNNING, H_KIND, H_BUDGET, H_RESOURCE,
              H_CLAIMED)

# Why a `run()` stopped. ⚠️ `blocked` and `held` are deliberately different words:
# *blocked* is a graph that can never advance again (an upstream failed and closed
# the branch), *held* is one that could advance if a ceiling were raised. Reporting
# the second as the first would send a user to fix a plan when they needed a knob.
R_FINISHED = "finished"          # every node settled
R_CANCELLED = "cancelled"        # the caller's token was set
R_PAUSED = "paused"              # the graph is held; nothing may start
R_HELD = "held"                  # a ceiling binds with nothing in flight
R_BLOCKED = "blocked"            # nothing is runnable and the graph is not done
R_BUDGET = "budget"              # the pump's wall-clock budget was spent
R_EXHAUSTED = "exhausted"        # the unproductive-round backstop engaged
R_NO_RUN = "no_run"              # no such run
REASONS = (R_FINISHED, R_CANCELLED, R_PAUSED, R_HELD, R_BLOCKED, R_BUDGET,
           R_EXHAUSTED, R_NO_RUN)

# Events a caller may render. Presentation only — nothing here decides anything, and
# a listener that raises costs its own event and nothing else.
EV_DISPATCH = "dispatch"
EV_SETTLE = "settle"
EV_RETRY = "retry"
EV_TIMEOUT = "timeout"
EV_SKIP = "skip"
EV_CANCEL = "cancel"
EV_DONE = "done"
EVENTS = (EV_DISPATCH, EV_SETTLE, EV_RETRY, EV_TIMEOUT, EV_SKIP, EV_CANCEL, EV_DONE)

#: How long the pump waits on a completion before looking around again. Also how
#: fast a cancellation token is noticed, which is why it is a fifth of a second and
#: not five: a human pressing Ctrl+C is waiting for this.
POLL_SEC = 0.2

#: How stale the pump's view of a run may get while it waits. It re-reads on this
#: tick — not on every poll — because another process may pause, cancel or extend the
#: run, and 20 reads a second to notice that would make waiting cost more than
#: working.
REFRESH_SEC = 1.0

#: How often a live node's row is beaten. ⚠️ The PUMP beats, not the worker: it is
#: what holds the thread, so it is what knows the node is alive — and a consumer that
#: forgot would have its nodes read as stale to `tasks.stale_running()` and to the
#: crash scan. A worker may still beat itself through `Ctx.beat()`.
BEAT_EVERY = max(5.0, _tasks.STALE_AFTER / 6.0)

#: Characters kept from a failure message. Long enough for an exception line, short
#: enough that a node's error stays readable beside 63 others.
MAX_ERROR = 400

#: Statuses a worker may report. Anything else is read as a plain success, because a
#: worker that returned a value and no error did the work — while a status this build
#: does not have is not a fact anybody can act on.
REPORTABLE = frozenset((_m.COMPLETED, _m.FAILED, _m.SKIPPED, _m.CANCELLED,
                        _m.PAUSED, _m.PENDING))


def _now() -> float:
    """Monotonic seconds. Never the wall clock: a pump must survive a clock change."""
    return time.monotonic()


def _int(value, fallback: int, floor: int) -> int:
    """A ceiling, coerced. Junk falls back to the declared default, never to zero."""
    try:
        out = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(floor, out)


def _labels(value, fallback: dict[str, int]) -> dict[str, int]:
    """A `{kind: ceiling}` table, copied and coerced.

    ⚠️ Copied on purpose: a `Limits` a consumer holds may be edited, and the module
    tables in `config` are read by every other run in the process.
    """
    try:
        items = list(dict(value).items())
    except (TypeError, ValueError):
        return {str(k): int(v) for k, v in fallback.items()}
    out: dict[str, int] = {}
    for key, raw in items:
        name = str(key or "").strip().lower()
        if not name:
            continue
        out[name] = _int(raw, 1, 1)
    return out


def _text(value, limit: int = MAX_ERROR) -> str:
    return str(value or "")[:limit]


def _exc_text(exc: BaseException) -> str:
    """A worker's exception, as a node error. The type is half the answer."""
    return _text(f"{type(exc).__name__}: {exc}")


# ── The ceilings ──────────────────────────────────────────────────────────────

@dataclass
class Limits:
    """What one run may spend. Built by `limits()`; a consumer may override any of it.

    ⚠️ `max_workers` and `max_running` are two numbers and not one. Workers counts
    *this pump's threads*; running counts *claimed rows*, which includes another
    process's. Folding them would either under-use a machine (threads capped by other
    people's work) or over-run a ceiling (rows capped by this process's threads).
    """

    max_workers: int = 4
    max_running: int = 8
    kind_limits: dict[str, int] = field(default_factory=dict)
    kind_budgets: dict[str, int] = field(default_factory=dict)
    max_attempts: int = 2
    node_timeout: float = 0.0
    #: Decline a node whose dependency settled unsuccessfully, and skip it. ⚠️ A
    #: consumer's decision, not an operator's, so it has no environment knob: a
    #: best-effort graph passes `False` and gets every node the graph offers.
    skip_failed_upstream: bool = True

    @property
    def inline(self) -> bool:
        """No pool at all — every node on the calling thread, one at a time."""
        return self.max_workers <= 0

    @property
    def width(self) -> int:
        """Nodes this pump may have in flight: the tighter of workers and running."""
        return 1 if self.inline else min(self.max_running, self.max_workers)

    def to_payload(self) -> dict:
        return {
            "max_workers": self.max_workers,
            "max_running": self.max_running,
            "width": self.width,
            "inline": self.inline,
            "kind_limits": dict(self.kind_limits),
            "kind_budgets": dict(self.kind_budgets),
            "max_attempts": self.max_attempts,
            "node_timeout": self.node_timeout,
            "skip_failed_upstream": self.skip_failed_upstream,
        }


def limits(**over) -> Limits:
    """The live ceilings, with per-run overrides. Total — junk falls back, never up.

    ⚠️ **DERIVED FROM `config`, NEVER DECLARED HERE, AND READ LIVE** — the same rule
    `broker/budget.py` carries: a copied ceiling keeps working and drifts in the
    direction that over-runs, because nobody edits the copy. Overrides are how a
    consumer expresses its own vocabulary (`kind_limits={"scan": 1}`), which is the
    injection `validate(knob=…, label=…)` already uses instead of a branch.
    """
    return Limits(
        max_workers=_int(over.get("max_workers", config.DAG_MAX_WORKERS),
                         config.DAG_MAX_WORKERS, 0),
        max_running=_int(over.get("max_running", config.DAG_MAX_RUNNING),
                         config.DAG_MAX_RUNNING, 1),
        kind_limits=_labels(over.get("kind_limits", config.DAG_KIND_LIMITS),
                            config.DAG_KIND_LIMITS),
        kind_budgets=_labels(over.get("kind_budgets", config.DAG_KIND_BUDGETS),
                             config.DAG_KIND_BUDGETS),
        max_attempts=_int(over.get("max_attempts", config.DAG_MAX_ATTEMPTS),
                          config.DAG_MAX_ATTEMPTS, 1),
        node_timeout=max(0.0, float(over.get("node_timeout", config.DAG_NODE_TIMEOUT)
                                    or 0.0)),
        skip_failed_upstream=bool(over.get("skip_failed_upstream", True)),
    )


# ── The plan ──────────────────────────────────────────────────────────────────

@dataclass
class Slot:
    """One node this round may start, with the facts the ceilings were read from."""

    node: str
    task_id: str = ""
    kind: str = ""
    resource: str = ""
    priority: int = 5
    seq: int = 0
    attempts: int = 0
    view: _store.NodeView | None = None

    def to_payload(self) -> dict:
        return {"node": self.node, "task_id": self.task_id, "kind": self.kind,
                "resource": self.resource, "priority": self.priority,
                "seq": self.seq, "attempts": self.attempts}


def _hold(code: str, view: _store.NodeView, detail: str = "") -> dict:
    """One line of item 18's report: which node, why not, and against what."""
    return {"code": code, "node": view.node, "kind": view.kind,
            "resource": view.resource, "detail": _text(detail, 120)}


@dataclass
class Plan:
    """What may start now, what may not, and what it was measured against.

    ⚠️ It is the decision **and** the report, `budget.BudgetPlan`'s shape and for its
    reason: two objects means a surface can describe a dispatch that never happened.
    """

    run_id: str = ""
    slots: list[Slot] = field(default_factory=list)
    held: list[dict] = field(default_factory=list)
    running: int = 0
    in_flight: list[str] = field(default_factory=list)
    spent: dict[str, int] = field(default_factory=dict)
    caps: Limits | None = None
    reason: str = ""

    def __bool__(self) -> bool:
        return bool(self.slots)

    @property
    def codes(self) -> tuple[str, ...]:
        """Distinct hold codes, in the order they were first hit."""
        out: list[str] = []
        for item in self.held:
            code = str(item.get("code") or "")
            if code and code not in out:
                out.append(code)
        return tuple(out)

    def to_payload(self) -> dict:
        return {
            "run_id": self.run_id,
            "slots": [s.to_payload() for s in self.slots],
            "held": list(self.held),
            "codes": list(self.codes),
            "running": self.running,
            "in_flight": list(self.in_flight),
            "spent": dict(self.spent),
            "reason": self.reason,
            "limits": self.caps.to_payload() if self.caps else {},
        }


def in_flight(state: _store.GraphState) -> list[_store.NodeView]:
    """Nodes that occupy a slot right now — RUNNING, plus claimed-and-not-yet-started.

    ⚠️ **DERIVED, NEVER COUNTED.** A claimed row reads READY (`store._state_of()`
    projects `queued` that way) and loses only `dispatchable`, so READY-without-
    dispatchable is exactly "somebody took this and has not settled it". Counting in
    memory instead would give each process of a dual-mode install its own full
    allowance, and would hand a resumed run slots that a dead worker still holds.
    """
    return [n for n in state.nodes
            if n.state == _m.RUNNING or (n.state == _m.READY and not n.dispatchable)]


def spend(state: _store.GraphState) -> dict[str, int]:
    """What this run has spent per kind over its whole life, from `attempt_count`.

    ⚠️ Derived from the rows for `DAG_MAX_MUTATIONS`' reason — it needs no counter to
    keep, a retry is charged because it really is a second call, and a crash-resumed
    run does not silently get a fresh allowance.
    """
    out: Counter[str] = Counter()
    for view in state.nodes:
        if view.kind and view.attempts:
            out[view.kind] += int(view.attempts)
    return dict(out)


def plan_next(state: _store.GraphState, *, caps: Limits | None = None,
              workers: int | None = None) -> Plan:
    """Which dispatchable nodes may start now — and a reason for every one that may not.

    Writes nothing, so a surface can show it and a test can assert on it. *workers* is
    how many nodes the caller can actually service this round; `None` means "do not
    consider it", which is the right answer for a report and the wrong one for a pump.

    ⚠️ **A HELD NODE IS SKIPPED, NOT A STOP SIGN** — `budget.plan()`'s rule: a
    lower-priority node whose resource is free may still run, so one contended
    resource may not strip everything below it. The two run-wide ceilings are the
    exception and hold *the rest*, because when there is no slot left there is no
    slot left for anybody.
    """
    caps = caps if isinstance(caps, Limits) else limits()
    plan = Plan(run_id=state.run_id, caps=caps)
    flight = in_flight(state)
    plan.in_flight = [n.node for n in flight]
    plan.running = len(flight)
    plan.spent = spend(state)

    used: Counter[str] = Counter(n.kind for n in flight if n.kind)
    taken = {n.resource for n in flight if n.resource}
    spent: Counter[str] = Counter(plan.spent)

    room = max(0, caps.max_running - plan.running)
    free = room if workers is None else min(room, max(0, int(workers)))
    # Which of the two run-wide ceilings is the binding one, stated once so the
    # report cannot blame the wrong knob.
    binding = H_WORKERS if (workers is not None and free < room) else H_RUNNING

    for view in state.dispatchable:
        if caps.skip_failed_upstream and view.upstream_failed:
            # Declined before it is costed: this is not work, so it may not spend a
            # slot, a kind's allowance or another node's turn.
            plan.held.append(_hold(H_UPSTREAM, view, ", ".join(view.upstream_failed)))
            continue
        if len(plan.slots) >= free:
            plan.held.append(_hold(binding, view,
                                   f"{plan.running + len(plan.slots)} in flight"))
            continue
        kind = view.kind
        cap = caps.kind_limits.get(kind) if kind else None
        if cap is not None and used[kind] >= cap:
            plan.held.append(_hold(H_KIND, view, f"{kind} {used[kind]}/{cap} in flight"))
            continue
        allowance = caps.kind_budgets.get(kind) if kind else None
        if allowance is not None and spent[kind] >= allowance:
            plan.held.append(_hold(H_BUDGET, view, f"{kind} {spent[kind]}/{allowance} spent"))
            continue
        if view.resource and view.resource in taken:
            plan.held.append(_hold(H_RESOURCE, view, view.resource))
            continue
        plan.slots.append(Slot(node=view.node, task_id=view.task_id, kind=kind,
                               resource=view.resource, priority=view.priority,
                               seq=view.seq, attempts=view.attempts, view=view))
        if kind:
            used[kind] += 1
            spent[kind] += 1
        if view.resource:
            taken.add(view.resource)

    if not plan.slots:
        plan.reason = plan.codes[0] if plan.codes else ""
    return plan


def dispatch(run_id: str, *, caps: Limits | None = None,
             state: _store.GraphState | None = None, workers: int | None = None,
             worker_id: str = WORKER, execution_id: str = "") -> Plan:
    """Plan, then take the rows. `Plan.slots` is what this worker actually holds.

    ⚠️ Each claim is made against a **fresh** read inside `store.claim()`, never
    against the planning state: planning decides, a claim locks, and a lock taken
    against a stale view is not a lock. A row lost in that window is reported as
    `claimed` rather than dropped — two workers reading one READY list is how a node
    runs twice, and the loser needs to know it lost.

    ⚠️ It claims and stops there. The RUNNING transition belongs to whoever holds the
    thread, which is `run()` — `commands.watch()`'s split: this returns a decision,
    the runner acts, because the runner owns the handle.
    """
    st = state if state is not None else _store.load(run_id)
    plan = plan_next(st, caps=caps, workers=workers)
    plan.run_id = st.run_id or str(run_id or "")
    if not st.exists:
        plan.slots = []
        plan.reason = R_NO_RUN
        return plan
    kept: list[Slot] = []
    for slot in plan.slots:
        try:
            took = _store.claim(plan.run_id, slot.node, worker_id=worker_id,
                                execution_id=execution_id)
        except Exception:
            took = False
        if took:
            kept.append(slot)
        else:
            view = slot.view
            plan.held.append(_hold(H_CLAIMED, view, "taken by another worker")
                             if view is not None
                             else {"code": H_CLAIMED, "node": slot.node, "kind": slot.kind,
                                   "resource": slot.resource, "detail": "taken"})
    plan.slots = kept
    if not plan.slots and not plan.reason:
        plan.reason = plan.codes[0] if plan.codes else ""
    return plan


# ── What a worker is given, and what it may report ────────────────────────────

@dataclass
class Ctx:
    """One node, handed to a worker. Read-only about the graph; live about the run.

    A worker gets what it needs to do this node and nothing about the plan around it —
    `workflow.for_turn()`'s rule, applied to the execution half.

    ⚠️ There is no `meta` here, deliberately: `Node.meta` is a consumer's own dict and
    is **not stored**, so it is not a fact the rows can hand back. A consumer that
    needs it holds its own definition and looks the node up by `ctx.node` — which is
    also why the engine cannot start reading it by accident.
    """

    run_id: str
    view: _store.NodeView
    worker_id: str = WORKER
    attempt: int = 1
    deadline: float = 0.0
    cancel: threading.Event = field(default_factory=threading.Event)

    @property
    def node(self) -> str:
        return self.view.node

    @property
    def task_id(self) -> str:
        return self.view.task_id

    @property
    def instruction(self) -> str:
        return self.view.instruction

    @property
    def kind(self) -> str:
        return self.view.kind

    def cancelled(self) -> bool:
        """True once the pump has given up on this node. Cooperative by necessity."""
        return self.cancel.is_set()

    def expired(self) -> bool:
        return bool(self.deadline) and _now() >= self.deadline

    def remaining(self) -> float:
        """Seconds left, or `-1.0` when there is no deadline. Never a fake number."""
        if not self.deadline:
            return -1.0
        return max(0.0, self.deadline - _now())

    def beat(self) -> bool:
        """Say the node is still being worked on. The pump does this too, on its tick."""
        try:
            return bool(_tasks.heartbeat(self.task_id, worker_id=self.worker_id))
        except Exception:
            return False

    def progress(self, value: float) -> bool:
        """Report progress without a transition — `tasks.set_progress()`, the one writer."""
        try:
            return _tasks.set_progress(self.task_id, float(value)) is not None
        except Exception:
            return False


@dataclass
class NodeOutcome:
    """One node's verdict. `Outcome` is the run's — two words, two scopes."""

    status: str = _m.COMPLETED
    result: str = ""
    error: str = ""
    progress: float | None = None

    def to_payload(self) -> dict:
        return {"status": self.status, "result": _text(self.result, MAX_ERROR),
                "error": _text(self.error), "progress": self.progress}


def outcome_of(raw) -> NodeOutcome:
    """Whatever a worker returned, as a verdict. Total — every shape has a reading.

    `None` and any plain value are success (it did the work and said something about
    it); `False` is failure without a message; a `NodeOutcome` or a dict is taken as
    written. ⚠️ A status this build does not have is read as success rather than
    guessed at: `REPORTABLE` is the closed vocabulary, and inventing a transition from
    an unknown word is how a node ends up in a state no reader can project.
    """
    if isinstance(raw, NodeOutcome):
        out = NodeOutcome(status=raw.status, result=raw.result, error=raw.error,
                          progress=raw.progress)
    elif raw is None:
        out = NodeOutcome()
    elif isinstance(raw, bool):
        out = NodeOutcome(status=_m.COMPLETED if raw else _m.FAILED)
    elif isinstance(raw, dict):
        out = NodeOutcome(status=str(raw.get("status") or _m.COMPLETED),
                          result=_text(raw.get("result"), _m.MAX_TEXT),
                          error=_text(raw.get("error")),
                          progress=raw.get("progress"))
    else:
        out = NodeOutcome(result=_text(raw, _m.MAX_TEXT))
    status = str(out.status or "").strip().lower()
    out.status = status if status in REPORTABLE else _m.COMPLETED
    out.result = _text(out.result, _m.MAX_TEXT)
    out.error = _text(out.error)
    if out.progress is not None:
        try:
            out.progress = float(out.progress)
        except (TypeError, ValueError):
            out.progress = None
    return out


def may_repeat(task_id: str) -> tuple[bool, str]:
    """May this node be started again? `core/recovery/classify.py` decides, not us.

    ⚠️ **ASK WHILE THE ROW IS STILL OPEN.** `classify._assess_task()` returns
    `R_NON_RECOVERABLE` for anything already settled, so asking after the FAILED mark
    would make every retry impossible while every line of this function still looked
    right.

    ⚠️ **AND ASK `safe_to_continue()`, NOT `safe_to_repeat()`.** The narrower
    predicate is the one place five conditions are ANDed for a *repeat*, and an open
    task row never classifies as `R_SAFE_TO_RETRY` — it classifies as
    `R_SAFE_TO_RESUME`, because the checkpoint is the resume point. Asking the
    narrower one would therefore return False for every node forever: a bounded
    retry that reads as implemented and cannot fire once. `safe_to_continue()` is
    that composition, and it lives beside the classifier so this module holds no
    recovery rule of its own.

    ⚠️ Imported lazily. `store` sits on the turn path through the broker's live-run
    source, and `core.recovery` is a package that pulls the crash scanner and the
    safety table in with it — a cost worth paying on the first failure and not on
    every prompt. `health.py`'s "every import is local to its builder", for the same
    reason.

    Fails **closed**: no row, an unreadable one or a classifier fault all read as
    "do not repeat". *"Never blind"* cuts this way — an unknown is not a yes.
    """
    if not str(task_id or ""):
        return False, "no task row"
    try:
        from agent2.core.recovery import classify as _classify
    except Exception as exc:
        return False, _exc_text(exc)
    try:
        task = _tasks.get(task_id)
        if task is None:
            return False, "no task row"
        found = _classify.assess(_classify.K_TASK, task)
        if found.classification == _classify.R_REQUIRES_VERIFICATION:
            found = _classify.verify(found, task)
        return bool(_classify.safe_to_continue(found)), _text(found.reason, 120)
    except Exception as exc:
        return False, _exc_text(exc)


# ── The pump ──────────────────────────────────────────────────────────────────

@dataclass
class Outcome:
    """What one `run()` did. `reason` is why it stopped; `state` is what is true now."""

    run_id: str = ""
    ok: bool = False
    reason: str = ""
    rounds: int = 0
    dispatched: int = 0
    completed: int = 0
    failed: int = 0
    retried: int = 0
    timed_out: int = 0
    skipped: int = 0
    held: list[dict] = field(default_factory=list)
    state: _store.GraphState | None = None

    def to_payload(self) -> dict:
        out = {
            "run_id": self.run_id, "ok": self.ok, "reason": self.reason,
            "rounds": self.rounds, "dispatched": self.dispatched,
            "completed": self.completed, "failed": self.failed,
            "retried": self.retried, "timed_out": self.timed_out,
            "skipped": self.skipped, "held": list(self.held),
        }
        if self.state is not None:
            out["state"] = self.state.to_payload()
        return out


@dataclass
class _Live:
    """A node this pump is holding. `thread` is None for an inline node."""

    ctx: Ctx
    thread: threading.Thread | None = None
    beat: float = 0.0


def _work(ctx: Ctx, worker, done: SimpleQueue) -> None:
    """Run one node and post its verdict. The one place a worker's exception lands."""
    raw = None
    text = ""
    try:
        raw = worker(ctx)
    except Exception as exc:
        text = _exc_text(exc)
    try:
        done.put((ctx, raw, text))
    except Exception:
        pass


class _Pump:
    """One `run()` in progress. Private — `run()` is the supported entry point."""

    def __init__(self, run_id: str, worker, caps: Limits, *,
                 cancel: threading.Event | None = None, on_event=None,
                 worker_id: str = WORKER, budget_sec: float = 0.0,
                 max_rounds: int = 0) -> None:
        self.run_id = str(run_id or "")
        self.worker = worker
        self.caps = caps
        self.cancel = cancel
        self.on_event = on_event
        self.worker_id = _text(worker_id or WORKER, 64)
        self.budget_sec = max(0.0, float(budget_sec or 0.0))
        self.max_rounds = max(0, int(max_rounds or 0))
        self.live: dict[str, _Live] = {}
        self.done: SimpleQueue = SimpleQueue()
        self.started = _now()
        self.counters = {"rounds": 0, "dispatched": 0, "completed": 0, "failed": 0,
                         "retried": 0, "timed_out": 0, "skipped": 0}

    # ── plumbing ──
    def _emit(self, event: str, payload: dict) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(event, dict(payload))
        except Exception:
            pass

    def _stop_requested(self) -> bool:
        return self.cancel is not None and self.cancel.is_set()

    # ── one node ──
    def _start(self, slot: Slot) -> _store.GraphState | None:
        """Mark the claimed node RUNNING, then run it. Inline is a finished thread.

        ⚠️ The RUNNING mark happens **here**, in the pump's own thread, immediately
        after the claim — never inside the worker. A row left QUEUED while a worker
        works is a row that beats nothing (`tasks.heartbeat` writes only to a RUNNING
        row) and that no scheduler will re-take (`dispatchable` needs PENDING), so a
        crash would leave it looking as though it had never started. Worker recovery
        does now sweep it — `tasks.HELD` covers QUEUED precisely because this window
        exists and a process can die inside it — but that is the **backstop**, not the
        design: it costs `STALE_AFTER` seconds of a run sitting at N-1/N. It is also
        what bumps `attempt_count`, which is the retry budget and the per-kind spend,
        both derived from it.
        """
        view = slot.view
        if view is None:
            return None
        st = _store.mark(self.run_id, slot.node, _m.RUNNING, advance_run=False)
        fresh = st.node(slot.node) or view
        ctx = Ctx(run_id=self.run_id, view=fresh, worker_id=self.worker_id,
                  attempt=max(1, int(fresh.attempts or slot.attempts + 1)),
                  deadline=_now() + self.caps.node_timeout if self.caps.node_timeout else 0.0)
        item = _Live(ctx=ctx, beat=_now())
        self.live[slot.node] = item
        if self.caps.inline:
            _work(ctx, self.worker, self.done)
            return st
        try:
            item.thread = threading.Thread(target=_work, args=(ctx, self.worker, self.done),
                                           name=f"{WORKER}-{slot.node}"[:60], daemon=True)
            item.thread.start()
        except Exception:
            # A machine that cannot start a thread still has a caller's thread:
            # `core/scheduler.py`'s DISABLED degradation, node-shaped.
            item.thread = None
            _work(ctx, self.worker, self.done)
        return st

    def _settle(self, ctx: Ctx, out: NodeOutcome,
                timed_out: bool = False) -> _store.GraphState:
        """Record one node's verdict, retrying only what is bounded AND classified."""
        node = ctx.node
        status = out.status
        retry = status in (_m.FAILED, _m.PENDING)
        why = ""
        if retry and ctx.attempt < self.caps.max_attempts:
            allowed, why = may_repeat(ctx.task_id)
            if allowed:
                self.counters["retried"] += 1
                self._emit(EV_RETRY, {"node": node, "attempt": ctx.attempt,
                                      "of": self.caps.max_attempts, "why": why,
                                      "error": out.error})
                return _store.mark(self.run_id, node, _m.PENDING,
                                   error=out.error or None, advance_run=False)
            why = f"not repeatable: {why}" if why else "not repeatable"
        elif retry and status == _m.PENDING:
            why = "attempt ceiling reached"
        if status == _m.PENDING:
            # A worker asked to be run again and may not be. That is a failure with a
            # reason, never a row parked PENDING for a pump that has stopped counting.
            status = _m.FAILED
            out.error = out.error or why or "retry refused"
        if status == _m.FAILED and why and why not in (out.error or ""):
            out.error = f"{out.error} ({why})" if out.error else why
        if status == _m.COMPLETED:
            self.counters["completed"] += 1
        elif status == _m.FAILED:
            self.counters["failed"] += 1
        if timed_out:
            self.counters["timed_out"] += 1
        self._emit(EV_SETTLE, {"node": node, "status": status,
                               "attempt": ctx.attempt, "error": out.error,
                               "timed_out": timed_out})
        return _store.mark(self.run_id, node, status, result=out.result or None,
                           error=out.error or None, progress=out.progress)

    # ── rounds ──
    def _expire(self) -> _store.GraphState | None:
        """Time out what ran too long. ⚠️ Record the verdict, THEN signal the worker."""
        if not self.caps.node_timeout:
            return None
        st = None
        for node, item in list(self.live.items()):
            if not item.ctx.expired():
                continue
            self.live.pop(node, None)
            st = self._settle(item.ctx,
                              NodeOutcome(status=_m.FAILED,
                                          error=f"timed out after {self.caps.node_timeout:g}s"),
                              timed_out=True)
            item.ctx.cancel.set()
            self._emit(EV_TIMEOUT, {"node": node, "seconds": self.caps.node_timeout})
        return st

    def _drain(self, block: bool) -> _store.GraphState | None:
        """Apply every finished node. Blocks at most `POLL_SEC`, and only if asked."""
        st = None
        first = True
        while True:
            try:
                if block and first:
                    ctx, raw, text = self.done.get(timeout=POLL_SEC)
                else:
                    ctx, raw, text = self.done.get_nowait()
            except Empty:
                break
            first = False
            if self.live.pop(ctx.node, None) is None:
                # A node this pump already settled (a timeout, a cancel). Its verdict
                # would be refused by `set_status` anyway; dropping it keeps the
                # counters describing what the pump decided.
                continue
            out = (NodeOutcome(status=_m.FAILED, error=text) if text
                   else outcome_of(raw))
            st = self._settle(ctx, out)
        return st

    def _skip(self, plan: Plan) -> _store.GraphState | None:
        """Record the decline the planner made: an upstream failed, so this is not work.

        ⚠️ The planner says why and writes nothing; SKIPPED is a transition, so it is
        the pump's to record. Without it a failed branch would hold the run open
        forever — `tasks.ready()` keeps offering those nodes, and every round would
        hold them again with the same reason. SKIPPED rather than FAILED because the
        node never ran, and `settle()` names it either way, so nothing is hidden.
        """
        st = None
        for item in plan.held:
            if item.get("code") != H_UPSTREAM:
                continue
            node = str(item.get("node") or "")
            detail = str(item.get("detail") or "")
            self.counters["skipped"] += 1
            self._emit(EV_SKIP, {"node": node, "detail": detail})
            st = _store.mark(self.run_id, node, _m.SKIPPED,
                             error=_text(f"upstream failed: {detail or '?'}"))
        return st

    def _beat(self) -> None:
        """Keep live rows visibly alive, at most every `BEAT_EVERY` per node."""
        now = _now()
        for item in self.live.values():
            if now - item.beat < BEAT_EVERY:
                continue
            item.beat = now
            item.ctx.beat()

    def _abandon(self) -> None:
        """Tell every live node the pump has stopped watching. It does not lie for them.

        No verdict is written: those nodes really are still running, and a fabricated
        status is worse than an honest RUNNING row the crash scan can classify.
        """
        for item in self.live.values():
            item.ctx.cancel.set()

    # ── the loop ──
    def go(self) -> Outcome:
        st = _store.load(self.run_id)
        if not st.exists:
            return Outcome(run_id=self.run_id, reason=R_NO_RUN, state=st)
        # A backstop, not a schedule: every round either reaps, dispatches, waits on
        # something live or breaks, so this can only engage on a defect. Workflow's
        # agent cap, node-shaped.
        # ⚠️ IT COUNTS UNPRODUCTIVE ROUNDS, NEVER ITERATIONS, AND THE DIFFERENCE IS
        # THE WHOLE POINT. A round spent waiting on a live node costs exactly one
        # `POLL_SEC` block in `_drain(block=True)` below, so charging every iteration
        # made this a wall-clock ceiling wearing a round count: two nodes at
        # `max_attempts=1` gave a guard of 20 == **4 seconds**, after which a healthy
        # node three seconds into its work was declared `exhausted` and cancelled by
        # `_abandon()`. Wall clock is `budget_sec`'s job and a hung node is
        # `caps.node_timeout`'s — both deliberately off by default, because a node may
        # legitimately be a 40-minute build, and a backstop that silently overrode
        # that default made the documented posture a lie. What is left is the one
        # shape that really would spin forever: a round that settles nothing, skips
        # nothing and leaves nothing running. Only that one is charged.
        guard = self.max_rounds or (8 + st.total * (self.caps.max_attempts + 2) * 2)
        loaded = _now()
        reason = ""
        idle = 0
        # ⚠️ Accumulated, never overwritten per round. A hold repeats every round the
        # node stays held, so the tally is keyed on (code, node) and keeps the FIRST
        # sighting — `Plan.codes`' order rule, run-shaped. Assigning the latest round's
        # list reported whichever round the loop happened to break after, which for a
        # finished run is the round *before* the last (`st.finished` breaks above
        # `dispatch`), and dropped every hold a long run had already cleared.
        held: dict[tuple[str, str], dict] = {}
        while True:
            self.counters["rounds"] += 1
            if self._stop_requested():
                self._emit(EV_CANCEL, {"run_id": self.run_id})
                st = _store.cancel_run(self.run_id, reason=R_CANCELLED)
                self._abandon()
                reason = R_CANCELLED
                break
            if self.budget_sec and _now() - self.started >= self.budget_sec:
                reason = R_BUDGET
                break

            fresh = self._expire()
            fresh = self._drain(block=False) or fresh
            settled = fresh is not None
            if fresh is not None:
                st, loaded = fresh, _now()
            elif _now() - loaded >= REFRESH_SEC:
                st, loaded = _store.load(self.run_id), _now()
            if not st.exists:
                reason = R_NO_RUN
                break
            if st.finished:
                reason = R_FINISHED
                break

            plan = dispatch(self.run_id, caps=self.caps, state=st,
                            workers=max(0, self.caps.width - len(self.live)),
                            worker_id=self.worker_id)
            for item in plan.held:
                held.setdefault((str(item.get("code") or ""), str(item.get("node") or "")), item)
            declined = self._skip(plan)
            if declined is not None:
                st, loaded = declined, _now()
            if plan.slots:
                self._emit(EV_DISPATCH, {"nodes": [s.node for s in plan.slots],
                                         "held": len(plan.held),
                                         "running": plan.running})
            for slot in plan.slots:
                started = self._start(slot)
                self.counters["dispatched"] += 1
                if started is not None:
                    st, loaded = started, _now()

            # Something settled, something was skipped, or something is running: this
            # round did work, whether or not it dispatched. See `guard` above for why
            # a waiting round may not be charged.
            if settled or declined is not None or self.live:
                idle = 0
            else:
                idle += 1
                if idle > guard:
                    reason = R_EXHAUSTED
                    break

            if not self.live and not plan.slots:
                if declined is not None:
                    # A settled node releases its dependents, so the graph this round
                    # judged no longer exists. Look again before calling it stuck.
                    continue
                if st.paused:
                    reason = R_PAUSED
                elif plan.held:
                    reason = R_HELD
                else:
                    reason = R_BLOCKED
                break
            if self.live:
                self._beat()
                fresh = self._drain(block=True)
                if fresh is not None:
                    st, loaded = fresh, _now()

        if reason not in (R_FINISHED, R_CANCELLED):
            self._abandon()
        final = _store.load(self.run_id)
        if final.exists:
            st = final
        out = Outcome(run_id=self.run_id, reason=reason, state=st,
                      held=list(held.values()),
                      ok=reason == R_FINISHED and not st.failed, **self.counters)
        self._emit(EV_DONE, {"run_id": self.run_id, "reason": reason, "ok": out.ok})
        return out


def run(run_id: str, worker, *, caps: Limits | None = None,
        cancel: threading.Event | None = None, on_event=None,
        worker_id: str = WORKER, budget_sec: float = 0.0,
        max_rounds: int = 0) -> Outcome:
    """Drive one graph to a verdict, bounded. *worker* is called as `worker(ctx)`.

    A worker may return `None`, a value, a bool, a dict or a `NodeOutcome`; whatever
    it raises becomes that node's error and nothing else. It should consult
    `ctx.cancelled()` if it can, because a Python thread cannot be killed — a
    deadline and a cancellation are both recorded first and signalled second, so a
    worker that never looks keeps running against a row that already settled.

    ⚠️ Every ceiling is honoured here and none of them is unlimited: `caps.width`
    threads (`0` workers ⇒ inline, one node at a time), `caps.max_running` claimed
    rows across every process, a per-kind concurrency table, a per-kind lifetime
    allowance, `caps.max_attempts` starts per node and an optional per-node deadline.

    ⚠️ `budget_sec` and `caps.node_timeout` are the only WALL-CLOCK bounds, and both
    default to off, because a node may legitimately be a 40-minute build. `max_rounds`
    is **not** a third: it overrides a backstop counting *unproductive* rounds — ones
    that settle nothing, skip nothing and leave nothing running — so a slow node
    cannot spend it. Charging waiting rounds instead turned it into a covert
    `guard × POLL_SEC` deadline that cancelled healthy work after four seconds.
    A cancellation is still noticed within one `POLL_SEC`, whatever a node is doing.

    Returns rather than raises, for every outcome including "no such run" — both
    surfaces are printers, and `Outcome.reason` is the word they print.
    """
    caps = caps if isinstance(caps, Limits) else limits()
    return _Pump(run_id, worker, caps, cancel=cancel, on_event=on_event,
                 worker_id=worker_id, budget_sec=budget_sec,
                 max_rounds=max_rounds).go()


def stats() -> dict:
    """The live ceilings and the vocabulary, for `/health` and a payload. Never raises."""
    try:
        caps = limits().to_payload()
    except Exception:
        caps = {}
    return {"limits": caps, "holds": list(HOLD_CODES), "reasons": list(REASONS),
            "events": list(EVENTS), "poll_sec": POLL_SEC, "refresh_sec": REFRESH_SEC}
