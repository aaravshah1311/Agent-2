"""UltraCode's driver — the loop that turns one objective into a settled graph.

`plan.py` says what an UltraCode run *looks* like (its kinds, its gate, its check)
and `stages.py` says which of the eleven stages a run is *in*. This module is the
only thing that **moves** one: start it, work it, re-plan it when the graph stalls,
settle it, and report every refusal as a word rather than an exception.

The spec's cycle is UNDERSTAND → INSPECT → DISCOVER SKILLS → PLAN → BUILD DAG →
EXECUTE → OBSERVE → ANALYZE → VERIFY → (pass ⇒ continue · fail ⇒ diagnose →
RE-PLAN → UPDATE DAG → EXECUTE), and every arrow in it is already owned by a module
that existed before this one:

⚠️ DISCOVER SKILLS sits **before** PLAN and that ordering is the spec's own
(D5.34 goal → D5.35 project → D5.36 skills → D5.37 planning), not a convenience:
`plan.brief()` gathers all three of the first arrows in one call precisely so the
skills this project has are already in hand when `dynamic.draft()` is asked for
steps. `stages.STAGES` is the one declaration of the order; this table is prose
about it, and the two are pinned equal by `test_ultracode.py`.

| The arrow | Who owns it |
|-----------|-------------|
| UNDERSTAND · INSPECT · DISCOVER SKILLS | `plan.brief()` |
| PLAN · RE-PLAN | `core.workflow.dynamic` (one model call, off the turn path) |
| BUILD DAG · UPDATE DAG | `dag.store.create()` / `store.extend()` |
| EXECUTE · OBSERVE | `dag.schedule.run()` — the bounded pump |
| VERIFY | `core.verify` — reached as a **node**, never as a post-pass |
| which stage any of that is | `stages.stage_of()`, derived on every read |

⚠️ **SO THIS MODULE HOLDS NO FACT OF ITS OWN.** It sequences the five it borrows
and it counts one thing nobody else counts — cycles. Everything else is a
projection: `Cycle`'s counters are properties off the pump's `Outcome`, `Launch`'s
gate and check ids are properties off `plan.graph_for()`'s report, and `Loop.spent`
is `len(cycles)`. A copied field here would be the second declaration that drifts
while both halves keep looking right, and there are four dataclasses in this file
precisely so no surface has to assemble one itself.

⚠️ **THE APPROVAL GATE IS A NODE, AND `start()` PAUSES IT.** `plan.graph_for()`
rewires every root of the plan to need the gate, so "nothing runs until a person
says so" is a property of the *graph*. But `store.create()` ends with `advance()`,
and a gate node with no dependencies is immediately dispatchable — the worker's
`GATE_KINDS` branch would complete it the instant the first cycle ran. So `start()`
parks it with `tasks.pause()`, which is safe in the one way that matters:
`pause()` writes **no** `CP_STOPPED`, so `stages.awaiting_approval()` still sees it
and `release_interrupted()` never can. `approve()` is then a single
`set_status(PENDING)` on that one row — never `store.unpause_run()`, which would
release every PAUSED node in the graph, and never `tasks.reopen()`, because PAUSED
is not a terminal status.

⚠️ **ONE CLOCK, AND IT IS CHECKED BETWEEN CYCLES.** `config.ULTRACODE_BUDGET_SEC`
is owned by `drive()` and is never passed into `schedule.run()`: the pump already
bounds a single node with `DAG_NODE_TIMEOUT`, and two clocks over one node is two
answers about which one killed it. So the budget is *reported* (`U_BUDGET`) at a
cycle boundary and never enforced by killing a worker mid-write.

⚠️ **CYCLES ARE COUNTED HERE; ROUNDS ARE READ OFF THE ROWS.** A cycle is one turn
of this loop and lives in memory, because it describes *this* driver. A re-plan is
durable (`GraphState.extensions`, via `dynamic.rounds_of()`) because dual mode is
two processes over one `agent2.db` and a counter would hand each of them a full
allowance. They are three different words for three different facts —
`config.ULTRACODE_MAX_CYCLES` says why the third one had to be new.

⚠️ **`U_*` IS A CLOSED VOCABULARY THAT SHARES NO STRING WITH ANY OTHER.** Not with
`dynamic.X_*` (`U_ENGINE` is `"dag_refused"` where `X_ENGINE` is
`"engine_refused"`), not with `schedule.HOLD_CODES`/`REASONS`/`EVENTS`, not with
`verify.VERDICTS`, not with `stages.S_*`/`K_*`. The rule binds the *values*, not
the constant names: one shared word and two payloads that mean different things
read as the same fact.

⚠️ **THE PUMP'S OWN VERDICT IS CARRIED, NEVER TRANSLATED.** `Cycle.reason` holds
`schedule.R_*` verbatim, and `Cycle.ok` answers a different question — *did the
pump get to run at all*. A held run (`R_HELD`) is a cycle that worked and a
ceiling that bound; an `U_GATE` is a cycle that never started. Folding those into
one boolean is what would make `drive()` re-plan a graph whose only problem is
that two commands may not run at once.

⚠️ **NOTHING HERE RAISES INTO A CALLER.** Every entry point refuses by *returning*
one of the four dataclasses with a `reason` on it, exactly as `store.create()`,
`dynamic.draft()` and `schedule.run()` do — both surfaces are printers, and the
`reason` is the word they print. Hence this module's written BLE001/S110 exemption
in `pyproject.toml`.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from time import monotonic

from agent2 import config
from agent2.core import tasks as _tasks
from agent2.core import verify as _verify
from agent2.core.dag import model as _m
from agent2.core.dag import schedule as _sched
from agent2.core.dag import store as _store
from agent2.core.workflow import dynamic as _dyn

from . import plan as _plan
from . import stages as _st

__all__ = [
    "REFUSALS",
    "U_BUDGET",
    "U_CYCLES",
    "U_ENGINE",
    "U_GATE",
    "U_LIVE",
    "U_MISSING",
    "U_NO_AIM",
    "U_NO_GATE",
    "U_OFF",
    "U_OPEN",
    "U_PLANNER",
    "U_SETTLED",
    "U_UNSHAPED",
    "WORKER",
    "Cycle",
    "Finish",
    "Launch",
    "Loop",
    "approve",
    "cancel",
    "describe",
    "drive",
    "finalize",
    "replan",
    "start",
    "state",
    "work",
]

# The worker id every UltraCode claim is stamped with. It is not `schedule.WORKER`
# ("dag"), so `exec_*` rows and a held node's `claimed` entry name which driver was
# holding it — the only way to tell an UltraCode run from a `/workflow run` in a
# ledger both of them write to.
WORKER = "ultracode"

# ── Refusals ────────────────────────────────────────────────────────────────
# Thirteen words, disjoint from every other closed vocabulary in the DAG family.
U_OFF = "ultracode_off"            # AGENT2_ULTRACODE=0            (vs X_OFF)
U_NO_AIM = "no_objective"          # nothing to work towards       (vs X_NO_GOAL)
U_PLANNER = "planner_refused"      # `dynamic` declined; its own code is in `note`
U_UNSHAPED = "not_shaped"          # `plan.graph_for()` produced nothing runnable
U_ENGINE = "dag_refused"           # `store.create()`/`schedule.run()` said no
U_MISSING = "no_such_run"          # no rows under that run id     (vs R_NO_RUN)
U_SETTLED = "already_settled"      # the graph is finished         (vs X_SETTLED)
U_OPEN = "still_running"           # `finalize()` on an unfinished graph
U_GATE = "approval_pending"        # a human has not released the gate node
U_NO_GATE = "no_gate"              # `approve()` with nothing awaiting approval
U_CYCLES = "cycles_spent"          # ULTRACODE_MAX_CYCLES          (vs X_ROUNDS)
U_BUDGET = "time_spent"            # ULTRACODE_BUDGET_SEC, checked between cycles
U_LIVE = "run_live"                # another run is unsettled here (mirrors the 409)

REFUSALS = (
    U_OFF, U_NO_AIM, U_PLANNER, U_UNSHAPED, U_ENGINE, U_MISSING, U_SETTLED,
    U_OPEN, U_GATE, U_NO_GATE, U_CYCLES, U_BUDGET, U_LIVE,
)

# The two pump verdicts a re-plan can actually answer. `R_HELD` is deliberately
# absent: a ceiling is binding, so more nodes cannot help and would make the
# runaway worse. `R_PAUSED` is absent because a human is holding it on purpose.
_REPLAN_ON = frozenset((_sched.R_BLOCKED, _sched.R_EXHAUSTED))

_NOTE_CHARS = 400


def _text(value: object, limit: int = _NOTE_CHARS) -> str:
    """One short line, for a `note` a human reads. Total over any input."""
    try:
        out = " ".join(str(value or "").split())
    except Exception:
        return ""
    return out[: max(0, limit)]


def _load(run_id: str) -> _store.GraphState | None:
    """The run's rows, or `None` when the READ itself broke.

    ⚠️ `None` is not "no such run" — `store.load()` already answers that with a
    state whose `exists` is False. Keeping them apart is what lets `_stage()`
    refuse to guess: a stage is a fact about a graph, and a failed read has none.
    """
    if not run_id:
        return None
    try:
        return _store.load(run_id)
    except Exception:
        return None


def _stage(st: _store.GraphState | None, fallback: str = _st.S_EXECUTE) -> str:
    """The run's stage, derived — never `stage_of(None)`.

    `stage_of()` is total and answers `"planning"` for anything it cannot read,
    which is the right default for junk and the wrong answer for a graph that was
    executing a moment ago. So a missing state takes *the caller's* fallback.
    """
    if st is None:
        return fallback
    try:
        return _st.stage_of(st) or fallback
    except Exception:
        return fallback


def _work_ids(st: _store.GraphState | None) -> list[str]:
    """The task ids of every node that is neither the gate nor the check.

    ⚠️ Keyed on "not gate, not verify" rather than on `WORK_KINDS`, because a node
    a planner produced without a kind still IS work: reading membership the other
    way round would quietly verify nothing on exactly the graphs a model wrote.
    """
    out: list[str] = []
    if st is None:
        return out
    try:
        views = list(st.nodes)
    except Exception:
        return out
    for view in views:
        kind = getattr(view, "kind", "") or _st.K_BUILD
        if kind in _st.GATE_KINDS or kind in _st.VERIFY_KINDS:
            continue
        tid = getattr(view, "task_id", "") or ""
        if tid:
            out.append(tid)
    return out


def _ids(views: object) -> list[str]:
    """Node ids off a list of `NodeView`s, for a payload that names them."""
    out: list[str] = []
    try:
        for view in views or ():
            nid = getattr(view, "node", "") or ""
            if nid:
                out.append(nid)
    except Exception:
        return out
    return out


# ── What each entry point hands back ────────────────────────────────────────


@dataclass
class Launch:
    """What starting a run produced — or the one word that stopped it.

    `shape` is `plan.graph_for()`'s own report, stored whole; `gate` and `check`
    are **properties** off it rather than copied fields, for the reason every
    other derived fact in this file is a property.
    """

    ok: bool = False
    reason: str = ""
    note: str = ""
    run_id: str = ""
    session_id: str = ""
    goal: str = ""
    name: str = ""
    stage: str = _st.S_UNDERSTAND
    nodes: int = 0
    shape: dict = field(default_factory=dict)
    brief: dict = field(default_factory=dict)
    draft: dict = field(default_factory=dict)
    state: _store.GraphState | None = None

    @property
    def gate(self) -> str:
        """The approval node's id, or `""` when the run needs no approval."""
        return str(self.shape.get("approval", "") or "")

    @property
    def check(self) -> str:
        """The verification node's id, or `""` when the graph had no terminal."""
        return str(self.shape.get("check", "") or "")

    @property
    def awaiting(self) -> bool:
        """Whether a human must release the gate before anything at all runs."""
        return bool(self.gate)

    def to_payload(self) -> dict:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "note": self.note,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "goal": self.goal,
            "name": self.name,
            "stage": self.stage,
            "stage_label": _st.STAGE_LABEL.get(self.stage, self.stage),
            "nodes": self.nodes,
            "gate": self.gate,
            "check": self.check,
            "awaiting": self.awaiting,
            "shape": dict(self.shape),
            "brief": dict(self.brief),
            "draft": dict(self.draft),
            "state": self.state.to_payload() if self.state is not None else None,
        }


@dataclass
class Cycle:
    """One turn of the loop: the pump ran the graph as far as it could get.

    ⚠️ `ok` says the pump *ran*, and `reason` is the pump's own `R_*` word for how
    far it got — two facts, because a held run is a cycle that worked while an
    `U_GATE` is a cycle that never started. `settled` is the pump's verdict.
    """

    ok: bool = False
    reason: str = ""
    note: str = ""
    run_id: str = ""
    stage: str = ""
    released: list[str] = field(default_factory=list)
    awaiting: list[str] = field(default_factory=list)
    outcome: _sched.Outcome | None = None
    state: _store.GraphState | None = None

    @property
    def settled(self) -> bool:
        """The pump's own `ok` — whether the graph reached a finished verdict."""
        return bool(self.outcome is not None and self.outcome.ok)

    @property
    def rounds(self) -> int:
        return int(getattr(self.outcome, "rounds", 0) or 0)

    @property
    def dispatched(self) -> int:
        return int(getattr(self.outcome, "dispatched", 0) or 0)

    @property
    def completed(self) -> int:
        return int(getattr(self.outcome, "completed", 0) or 0)

    @property
    def failed(self) -> int:
        return int(getattr(self.outcome, "failed", 0) or 0)

    @property
    def retried(self) -> int:
        return int(getattr(self.outcome, "retried", 0) or 0)

    @property
    def timed_out(self) -> int:
        return int(getattr(self.outcome, "timed_out", 0) or 0)

    @property
    def skipped(self) -> int:
        return int(getattr(self.outcome, "skipped", 0) or 0)

    @property
    def held(self) -> list[dict]:
        """Every node the scheduler declined, with the reason it declined it."""
        return list(getattr(self.outcome, "held", ()) or ())

    def to_payload(self) -> dict:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "note": self.note,
            "run_id": self.run_id,
            "stage": self.stage,
            "stage_label": _st.STAGE_LABEL.get(self.stage, self.stage),
            "settled": self.settled,
            "released": list(self.released),
            "awaiting": list(self.awaiting),
            "rounds": self.rounds,
            "dispatched": self.dispatched,
            "completed": self.completed,
            "failed": self.failed,
            "retried": self.retried,
            "timed_out": self.timed_out,
            "skipped": self.skipped,
            "held": self.held,
            "state": self.state.to_payload() if self.state is not None else None,
        }


@dataclass
class Finish:
    """A settled run and what the durable record says about it.

    ⚠️ `reason` is a **refusal**, not a verdict: a run that settled with failed
    nodes carries no reason at all, because `report` is the thing entitled to say
    why. The one exception is `cancel()`, which reports `schedule.R_CANCELLED` —
    readable as such precisely because `U_*` and `schedule.REASONS` share no word.
    And `ok` is not `report.verified` — a run of pure reasoning nodes is
    legitimately unverifiable and still did its job (`core/verify.py`'s rule).
    """

    ok: bool = False
    reason: str = ""
    note: str = ""
    run_id: str = ""
    stage: str = ""
    report: _verify.Report | None = None
    state: _store.GraphState | None = None

    @property
    def verified(self) -> bool:
        """Whether the record positively confirmed the work. Not `ok`."""
        return bool(self.report is not None and self.report.verified)

    @property
    def failed(self) -> list[str]:
        return _ids(getattr(self.state, "failed", ()) or ())

    def to_payload(self) -> dict:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "note": self.note,
            "run_id": self.run_id,
            "stage": self.stage,
            "stage_label": _st.STAGE_LABEL.get(self.stage, self.stage),
            "verified": self.verified,
            "failed": self.failed,
            "report": self.report.to_payload() if self.report is not None else None,
            "state": self.state.to_payload() if self.state is not None else None,
        }


@dataclass
class Loop:
    """The whole driven run: every cycle, every re-plan, and how it ended.

    `spent` and `replans` are properties over the two lists, never counters —
    a stored count and a list that disagree is a report nobody can trust.
    """

    ok: bool = False
    reason: str = ""
    note: str = ""
    run_id: str = ""
    stage: str = ""
    elapsed: float = 0.0
    cycles: list[Cycle] = field(default_factory=list)
    drafts: list[dict] = field(default_factory=list)
    finish: Finish | None = None

    @property
    def spent(self) -> int:
        """Cycles spent — what `ULTRACODE_MAX_CYCLES` bounds."""
        return len(self.cycles)

    @property
    def replans(self) -> int:
        """Re-plans attempted. ⚠️ Not `GraphState.extensions`, which counts nodes."""
        return len(self.drafts)

    @property
    def verified(self) -> bool:
        return bool(self.finish is not None and self.finish.verified)

    def to_payload(self) -> dict:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "note": self.note,
            "run_id": self.run_id,
            "stage": self.stage,
            "stage_label": _st.STAGE_LABEL.get(self.stage, self.stage),
            "elapsed": self.elapsed,
            "spent": self.spent,
            "replans": self.replans,
            "verified": self.verified,
            "cycles": [c.to_payload() for c in self.cycles],
            "drafts": list(self.drafts),
            "finish": self.finish.to_payload() if self.finish is not None else None,
        }


# ── Starting ────────────────────────────────────────────────────────────────


def start(
    goal: str,
    *,
    cwd: str = "",
    chat_id: str = "",
    model: str = "",
    mode_key: str = "",
    name: str = "",
    approval: bool | None = None,
    force: bool = False,
    ask=None,
) -> Launch:
    """One objective → a persisted graph, gated and checked. Writes rows.

    UNDERSTAND / INSPECT / DISCOVER SKILLS is `plan.brief()`, PLAN is one
    `dynamic.draft()` (which writes nothing), BUILD DAG is the single
    `store.create()` at the end — and `stages.py` has no `"building"` stage for
    exactly that reason: a stage nothing can ever report is a stage that lies.

    ⚠️ It refuses `U_LIVE` while any graph run in this project is unsettled, the
    same way `POST /api/workflows/<name>/run` answers 409: two live runs make
    "the current node" ambiguous for `for_turn()`, and a driver that started a
    second one would be racing the first over one `agent_tasks` table.

    ⚠️ *approval* overrides `AGENT2_ULTRACODE_APPROVAL` for this run only, and
    `None` means "whatever the operator configured". There is no value that means
    "approve as it goes": a gate a machine can satisfy is not approval.
    """
    out = Launch(goal=_text(goal, 2000))
    if not config.ULTRACODE_ENABLED:
        out.reason = U_OFF
        return out
    if not out.goal:
        out.reason = U_NO_AIM
        return out

    live = _load_live()
    if live is not None:
        out.reason = U_LIVE
        out.run_id = getattr(live, "run_id", "") or ""
        out.note = f"a run is still open here ({out.run_id or 'unknown'})"
        out.state = live
        out.stage = _stage(live)
        return out

    out.stage = _st.S_INSPECT
    brief = None
    try:
        brief = _plan.brief(out.goal, root=cwd or None, force=force)
        out.brief = brief.to_payload()
    except Exception as exc:
        # A brief is context, never permission: losing it costs the planner its
        # evidence and must not cost the user their run.
        out.note = _text(f"brief unavailable: {exc}")
    out.stage = _st.S_DISCOVER

    out.stage = _st.S_PLAN
    draft = _dyn.draft(
        out.goal,
        name=name or "",
        model=model or "",
        mode_key=mode_key or "",
        chat_id=chat_id or "",
        ask=ask,
    )
    out.draft = draft.to_payload()
    defn = getattr(draft, "defn", None)
    if not draft.ok or defn is None:
        out.reason = U_PLANNER
        out.note = _text(f"planner: {draft.reason or 'no plan'}")
        return out

    # ⚠️ Re-stamped, so `state()` can tell an UltraCode run from a plain
    # `/workflow auto` one — both are `dynamic` definitions until this line.
    try:
        defn = dataclasses.replace(defn, source=_plan.DEF_SOURCE)
    except Exception as exc:
        out.note = _text(f"source not stamped: {exc}")

    graph, shape = _plan.graph_for(
        defn,
        brief_=brief,
        approval=_plan.approval_needed(override=approval),
        check=True,
    )
    out.shape = dict(shape or {})
    if graph is None or not getattr(graph, "nodes", ()):
        out.reason = U_UNSHAPED
        out.note = _text(out.shape.get("note", "") or "the plan produced no node")
        return out

    run = _store.create(
        graph,
        chat_id=chat_id or "",
        cwd=cwd or "",
        model=model or "",
        mode=mode_key or "",
        surface=_plan.SURFACE,
        goal=out.goal,
        label=_plan.LABEL,
        knob=_plan.KNOB,
        max_nodes=_plan.max_nodes(),
        event=_plan.EVENT,
    )
    if not run.ok:
        out.reason = U_ENGINE
        # The validator's own sentence, carried verbatim — never re-derived here.
        note = ""
        if run.validation is not None:
            note = run.validation.summary()
        out.note = _text(note or run.reason)
        return out

    out.run_id = run.run_id
    out.session_id = run.session_id
    out.name = run.name
    out.nodes = len(run.node_tasks)

    # ⚠️ PARK THE GATE. `create()` ends with `advance()`, so an unparked gate is
    # dispatchable and the worker would complete it on the first cycle. `pause()`
    # writes no `CP_STOPPED`, which is what keeps `awaiting_approval()` able to
    # see it and `release_interrupted()` unable to touch it.
    gate = out.gate
    if gate:
        tid = run.node_tasks.get(gate, "")
        if tid:
            try:
                _tasks.pause(tid, notify=False)
            except Exception as exc:
                out.note = _text(f"gate not parked: {exc}")
    try:
        _store.advance(out.run_id)
    except Exception:
        pass

    out.state = _load(out.run_id)
    out.stage = _stage(out.state, _st.S_EXECUTE)
    out.ok = True
    return out


def _load_live() -> _store.GraphState | None:
    """The one unsettled graph run in this project, whoever started it.

    ⚠️ Deliberately **not** filtered on `plan.DEF_SOURCE`: `start()` asks "may a
    run begin here", and a live `/workflow run` is just as much a reason to say
    no. `state()` is the one that filters, because it asks a different question.
    """
    try:
        st = _store.live()
    except Exception:
        return None
    if st is None or not getattr(st, "exists", False):
        return None
    if st.finished:
        return None
    return st


# ── The gate ────────────────────────────────────────────────────────────────


def approve(run_id: str) -> Cycle:
    """Release the approval node a human was holding. Nothing else moves.

    ⚠️ `set_status(PENDING)` on **that one row** — never `store.unpause_run()`,
    which releases every PAUSED node in the graph (including work a person paused
    on purpose), and never `tasks.reopen()`, which is for a *terminal* status
    while PAUSED is open. `advance()` then recomputes readiness the one way the
    engine ever does.
    """
    out = Cycle(run_id=str(run_id or ""))
    if not config.ULTRACODE_ENABLED:
        out.reason = U_OFF
        return out
    st = _load(out.run_id)
    if st is None or not st.exists:
        out.reason = U_MISSING
        return out
    out.state = st
    out.stage = _stage(st)
    if st.finished:
        out.reason = U_SETTLED
        return out

    try:
        gates = list(_st.awaiting_approval(st))
    except Exception:
        gates = []
    if not gates:
        out.reason = U_NO_GATE
        out.note = "no node is awaiting approval"
        return out

    released: list[str] = []
    for view in gates:
        tid = getattr(view, "task_id", "") or ""
        if not tid:
            continue
        try:
            _tasks.set_status(tid, _tasks.TaskStatus.PENDING)
        except Exception as exc:
            out.note = _text(f"{getattr(view, 'node', '')}: {exc}")
            continue
        released.append(getattr(view, "node", "") or "")
    if not released:
        out.reason = U_NO_GATE
        out.note = out.note or "the gate node carries no task row"
        return out

    try:
        _store.advance(out.run_id)
    except Exception:
        pass
    out.released = released
    out.state = _load(out.run_id) or st
    out.stage = _stage(out.state)
    out.ok = True
    return out


# ── One cycle ───────────────────────────────────────────────────────────────


def work(
    run_id: str,
    turn,
    *,
    caps: _sched.Limits | None = None,
    cancel_event=None,
    on_event=None,
) -> Cycle:
    """EXECUTE + OBSERVE: drive the graph as far as the scheduler will take it.

    *turn* is called as `turn(ctx)` for every node that is neither the gate nor
    the check, and whatever it returns goes through `schedule.outcome_of()` — so
    a caller may return a `NodeOutcome`, a bool, a string or nothing at all. It
    is a **required positional**, because a driver with a default worker is a
    driver that can silently run a graph without doing any work.

    ⚠️ Recovery happens **first and unasked** (`release_interrupted()`), because
    the standing rule is that nobody should have to run a command to un-stick
    abandoned work — and it can never release the gate, which carries no
    `CP_STOPPED`. The nodes it released are named, since a silent release is
    indistinguishable from a node that was never stuck.
    """
    out = Cycle(run_id=str(run_id or ""))
    if not config.ULTRACODE_ENABLED:
        out.reason = U_OFF
        return out
    st = _load(out.run_id)
    if st is None or not st.exists:
        out.reason = U_MISSING
        return out
    if st.finished:
        out.state = st
        out.stage = _stage(st)
        out.reason = U_SETTLED
        return out

    try:
        released = _store.release_interrupted(out.run_id)
    except Exception:
        released = []
    if released:
        out.released = _ids(released)
        st = _load(out.run_id) or st

    try:
        gates = list(_st.awaiting_approval(st))
    except Exception:
        gates = []
    if gates:
        out.awaiting = _ids(gates)
        out.state = st
        out.stage = _stage(st)
        out.reason = U_GATE
        out.note = "approval is required before any node may start"
        return out

    session_id = getattr(st, "session_id", "") or ""

    def _check(_ctx) -> _sched.NodeOutcome:
        """VERIFY, as a node: ask `core.verify` whether the record agrees.

        ⚠️ Read live rather than off the captured state — a re-plan may have added
        nodes since this cycle began, and verifying the graph as it was would
        confirm a run that has since grown work nobody checked.
        """
        cur = _load(out.run_id) or st
        ids = _work_ids(cur)
        if not ids:
            return _sched.NodeOutcome(result="no work to verify")
        rep = _verify.verify_tasks(
            ids, session_id=getattr(cur, "session_id", "") or session_id, ref=out.run_id
        )
        counts = " ".join(f"{k}={v}" for k, v in sorted((rep.counts or {}).items()) if v)
        if not rep.ok:
            why = "; ".join(str(p) for p in (rep.problems or ()))
            return _sched.NodeOutcome(
                status=_m.FAILED, error=_text(why or "the record disagrees"), result=counts
            )
        word = "verified" if rep.verified else "unconfirmed"
        return _sched.NodeOutcome(result=_text(f"{word} {counts}".strip()))

    def _worker(ctx) -> _sched.NodeOutcome:
        kind = getattr(ctx, "kind", "") or _st.K_BUILD
        if kind in _st.GATE_KINDS:
            # A dispatchable gate has already been released by a human, so
            # completing it costs zero model calls and starts the run.
            return _sched.NodeOutcome(result="approved")
        if kind in _st.VERIFY_KINDS:
            return _check(ctx)
        return _sched.outcome_of(turn(ctx))

    try:
        res = _sched.run(
            out.run_id,
            _worker,
            caps=caps,
            cancel=cancel_event,
            on_event=on_event,
            worker_id=WORKER,
        )
    except Exception as exc:
        # `schedule.run()` returns for every outcome it knows about, so a raise
        # here is a bug in the pump — reported, never propagated into a printer.
        out.reason = U_ENGINE
        out.note = _text(exc)
        out.state = _load(out.run_id)
        out.stage = _stage(out.state)
        return out

    out.ok = True
    out.outcome = res
    out.reason = res.reason
    out.state = res.state if res.state is not None else _load(out.run_id)
    out.stage = _stage(out.state)
    return out


# ── Re-planning ─────────────────────────────────────────────────────────────


def replan(run_id: str, *, goal: str = "", ask=None, max_steps: int | None = None):
    """A stalled graph's failures → new `fix` nodes on the SAME graph.

    Returns `dynamic.Draft` **directly**: the planner already reports every fact a
    caller needs (its own refusal code, the steps, the rounds left, what it
    dropped and what it renamed), and a fifth engine dataclass wrapping it would
    be a second spelling of all of them.

    ⚠️ Growth goes through `store.extend()` — inside `dynamic.replan()`, never
    from here — so `validate_mutation()` still refuses to drop or rewire a settled
    node, and `AGENT2_DAG_MAX_MUTATIONS` still bounds the total.
    """
    if not config.ULTRACODE_ENABLED:
        return _dyn.Draft(reason=U_OFF, goal=_text(goal, 2000))
    return _dyn.replan(
        str(run_id or ""), goal=goal, ask=ask, max_steps=max_steps, kind=_st.K_FIX
    )


# ── Settling ────────────────────────────────────────────────────────────────


def finalize(run_id: str) -> Finish:
    """Verify the settled graph, then close the run. The only reporter of FINISHED.

    ⚠️ It **re-reads** rather than trusting a state it was handed: `store.advance()`
    auto-settles a graph the moment it finishes, so anything a caller captured
    before the last node landed is already stale.

    ⚠️ It verifies **before** settling and refuses to settle nothing: `U_OPEN` when
    the graph is not finished, because a settle on an open run is the one write
    here that cannot be taken back.
    """
    out = Finish(run_id=str(run_id or ""))
    if not config.ULTRACODE_ENABLED:
        out.reason = U_OFF
        return out
    st = _load(out.run_id)
    if st is None or not st.exists:
        out.reason = U_MISSING
        return out
    out.state = st
    out.stage = _stage(st, _st.S_VERIFY)
    if not st.finished:
        out.reason = U_OPEN
        out.note = f"{st.done} of {st.total} nodes settled"
        return out

    ids = _work_ids(st)
    if ids:
        try:
            out.report = _verify.verify_tasks(
                ids, session_id=getattr(st, "session_id", "") or "", ref=out.run_id
            )
        except Exception as exc:
            out.note = _text(f"verification unavailable: {exc}")

    try:
        settled = _store.settle(out.run_id)
    except Exception as exc:
        out.note = out.note or _text(exc)
        settled = None
    if settled is not None and getattr(settled, "exists", False):
        out.state = settled

    rep = out.report
    out.ok = bool(rep is None or rep.ok) and not out.state.failed
    out.stage = _st.S_FINISHED
    return out


# ── The loop ────────────────────────────────────────────────────────────────


def drive(
    run_id: str,
    turn,
    *,
    cycles: int | None = None,
    budget_sec: float | None = None,
    caps: _sched.Limits | None = None,
    cancel_event=None,
    on_event=None,
    ask=None,
) -> Loop:
    """EXECUTE → OBSERVE → ANALYZE → (fail ⇒ RE-PLAN → UPDATE DAG) → repeat.

    The adaptive half of the spec, and the whole of it is four decisions:

    * the pump finished the graph  ⇒ `finalize()` and stop;
    * it stopped BLOCKED or EXHAUSTED ⇒ ask the planner for remediation nodes and
      go round again — but only if the graph actually **grew**, or the next cycle
      would stall on the same wall and spend a model call per lap;
    * it stopped HELD or PAUSED ⇒ stop and say so. A ceiling is binding and a hold
      is somebody's decision; new nodes answer neither;
    * the cycle or wall-clock allowance ran out ⇒ stop and name which one.

    ⚠️ **ONE CLOCK, CHECKED BETWEEN CYCLES.** `budget_sec` is never handed to
    `schedule.run()` — the pump owns per-node timeouts, and two clocks over one
    node is two answers. So a long node overruns the budget and is reported at the
    boundary rather than killed mid-write.
    """
    out = Loop(run_id=str(run_id or ""))
    started = monotonic()
    if not config.ULTRACODE_ENABLED:
        out.reason = U_OFF
        return out

    limit = int(cycles if cycles is not None else config.ULTRACODE_MAX_CYCLES)
    limit = max(1, limit)
    budget = float(budget_sec if budget_sec is not None else config.ULTRACODE_BUDGET_SEC)
    deadline = started + budget if budget > 0 else 0.0

    st = _load(out.run_id)
    if st is None or not st.exists:
        out.reason = U_MISSING
        out.elapsed = round(monotonic() - started, 3)
        return out

    done = bool(st.finished)
    while not done:
        if out.spent >= limit:
            out.reason = U_CYCLES
            out.note = f"{out.spent} of {limit} cycles spent"
            break
        if deadline and monotonic() >= deadline:
            out.reason = U_BUDGET
            out.note = f"{budget:g}s wall-clock allowance spent"
            break
        if cancel_event is not None and cancel_event.is_set():
            out.reason = _sched.R_CANCELLED
            break

        cyc = work(
            out.run_id,
            turn,
            caps=caps,
            cancel_event=cancel_event,
            on_event=on_event,
        )
        out.cycles.append(cyc)
        out.stage = cyc.stage or out.stage
        if not cyc.ok:
            # An engine refusal: the pump never ran, so there is nothing to adapt to.
            out.reason = cyc.reason
            out.note = cyc.note
            break

        st = cyc.state if cyc.state is not None else _load(out.run_id)
        if st is not None and st.exists and st.finished:
            done = True
            break

        if cyc.reason not in _REPLAN_ON:
            out.reason = cyc.reason
            out.note = cyc.note
            break

        before = int(getattr(st, "total", 0) or 0)
        draft = replan(out.run_id, ask=ask)
        out.drafts.append(draft.to_payload())
        if not draft.ok:
            out.reason = U_PLANNER
            out.note = _text(f"re-plan: {draft.reason or 'no plan'}")
            break
        st = _load(out.run_id)
        after = int(getattr(st, "total", 0) or 0) if st is not None else before
        if after <= before:
            out.reason = U_PLANNER
            out.note = f"the re-plan added no node ({before} before, {after} after)"
            break
        out.stage = _stage(st, out.stage or _st.S_EXECUTE)

    if not done:
        out.elapsed = round(monotonic() - started, 3)
        return out

    fin = finalize(out.run_id)
    out.finish = fin
    out.stage = fin.stage or out.stage
    out.ok = bool(fin.ok)
    if not fin.ok and fin.reason:
        out.reason = fin.reason
        out.note = out.note or fin.note
    out.elapsed = round(monotonic() - started, 3)
    return out


# ── Stopping, reading, describing ───────────────────────────────────────────


def cancel(run_id: str, *, reason: str = "") -> Finish:
    """Stop a run and settle it as cancelled.

    ⚠️ `ok` is False even when the cancel worked: `Finish.ok` answers *did this run
    do its job*, and a cancelled one did not. That the cancel took effect is
    `reason == schedule.R_CANCELLED` plus a settled `state`.

    ⚠️ And the stage it lands on is `finalizing`, never `finished`: cancelled rows
    are settled, so the graph really is finished *running*, but nobody took a
    verdict — and `S_FINISHED` means "settled **and judged**". `finalize()` still
    works on a cancelled run, which is how a human asks for that verdict.
    """
    out = Finish(run_id=str(run_id or ""))
    if not config.ULTRACODE_ENABLED:
        out.reason = U_OFF
        return out
    st = _load(out.run_id)
    if st is None or not st.exists:
        out.reason = U_MISSING
        return out
    out.state = st
    out.stage = _stage(st)
    if st.finished:
        out.reason = U_SETTLED
        return out
    try:
        after = _store.cancel_run(out.run_id)
    except Exception as exc:
        out.reason = U_ENGINE
        out.note = _text(exc)
        return out
    if after is not None and getattr(after, "exists", False):
        out.state = after
    out.reason = _sched.R_CANCELLED
    out.note = _text(reason)
    out.stage = _stage(out.state, _st.S_FINALIZE)
    return out


def state(run_id: str = "") -> dict:
    """What is happening right now, for a surface to print.

    ⚠️ Filtered on `plan.DEF_SOURCE`, unlike `start()`'s liveness check: this asks
    "is an **UltraCode** run live", where that one asks "may anything start here".
    Two questions, so two readers.
    """
    out: dict = {
        "ok": bool(config.ULTRACODE_ENABLED),
        "reason": "" if config.ULTRACODE_ENABLED else U_OFF,
        "run": None,
        "stage": "",
        "stage_label": "",
        "awaiting": [],
        "mine": False,
        "policy": describe(),
    }
    if not config.ULTRACODE_ENABLED:
        return out
    st = _load(run_id) if run_id else _load_live()
    if st is None or not getattr(st, "exists", False):
        return out
    mine = (getattr(st, "source", "") or "") == _plan.DEF_SOURCE
    stage = _stage(st, _st.S_EXECUTE)
    try:
        awaiting = _ids(_st.awaiting_approval(st))
    except Exception:
        awaiting = []
    out["run"] = st.to_payload()
    out["stage"] = stage
    out["stage_label"] = _st.STAGE_LABEL.get(stage, stage)
    out["awaiting"] = awaiting
    out["mine"] = mine
    return out


def describe() -> dict:
    """The posture, for `/ultracode` and `POST /api/ultracode` to print."""
    return {
        "enabled": bool(config.ULTRACODE_ENABLED),
        "max_cycles": int(config.ULTRACODE_MAX_CYCLES),
        "budget_sec": float(config.ULTRACODE_BUDGET_SEC),
        "approval": bool(config.ULTRACODE_APPROVAL),
        "worker": WORKER,
        "refusals": list(REFUSALS),
        "replan_on": sorted(_REPLAN_ON),
        "plan": _plan.describe(),
        "stages": _st.describe(),
    }
