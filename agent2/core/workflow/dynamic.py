"""Dynamic Workflow — a goal, turned into a graph, by asking a model once.

Phase D4 (tasks D4.26–D4.32). A human types `/workflow auto "<goal>"`; a model is
asked, once, off the turn path, for a list of steps; those steps become a
`WorkflowDef` — which **is** `dag.Graph` — and from there every line of machinery
is the one that already existed. Nothing in this module executes, schedules,
persists or derives readiness.

⚠️ **THE PLANNER DECIDES *WHAT*; THE DAG DECIDES *STRUCTURE*.** That is D4.26's
acceptance bar and it is the whole seam. A model's reply is coerced into step
records here, and then handed to `graph.make_def()` → `graph.validate()` →
`store.plan()` → `runner.instantiate()`. So a generated plan is bounded by
`WORKFLOW_MAX_NODES`, refused for a cycle by the same iterative `find_cycles()`,
laid out in the same `levels_for()` waves, dispatched by the same
`schedule.plan_next()` and recovered by the same crash scan as a workflow a human
wrote by hand in `.agent2/workflows/`. There is no second engine to keep in step,
which is D4.27 made structural rather than promised.

⚠️ **PLAN MODE WRITES ABSOLUTELY NOTHING.** `draft()` reaches `store.plan()`, whose
own docstring is *"Nothing is written"*, and stops. No run row, no task row, no
session, no capability spent. `start(mode=MODE_AUTO)` is the one path that commits,
through `runner.instantiate()`, which asks `chat` live. The spec's rule for
`/workflow` — *"must NOT execute anything"* — reaches its planner unchanged: a
`/workflow auto` with no mode word shows a plan and waits to be told.

⚠️ **EXPLICIT ACTIVATION, NEVER AUTOMATIC (D4.31).** No agent loop imports this
module and no context source collects from it. A normal prompt cannot reach a
planner, however plan-shaped it looks, because there is no code path from a turn to
this file — asserted structurally, exactly as `/ultracode`'s activation will be. The
env switch is `AGENT2_DYNAMIC_WORKFLOW`; off, `draft()` refuses by returning and no
run is ever instantiated from a goal.

⚠️ **A MODEL'S OUTPUT IS COERCED INTO SOMETHING VALID, NEVER TRUSTED TO BE.**
`_acyclic()` keeps only the `needs` edges that point *backwards* in declaration
order and reports every edge it dropped. That single rule buys three of the ten
validator problems by construction — no `P_CYCLE`, no `P_SELF_DEP` (a step's own id
is not yet in `seen` when its own `needs` are filtered) and no `P_UNKNOWN_DEP` — so
a wordy or confused reply degrades into a *shallower* plan rather than into a
refusal a user cannot act on. It is also what licenses `DYNAMIC_MAX_STEPS` to clip
a suffix: declaration order IS a topological order once every edge points
backwards, so dropping the tail leaves not one dangling dependency.

⚠️ **AND IDS ARE NORMALIZED, BECAUSE `NODE_ID_RE` IS NARROW AND A MODEL WRITES
PROSE.** `^[a-z0-9][a-z0-9._-]{0,63}$` refuses `"Build the parser"`, and a plan
refused for punctuation would be a planner that works only for models that happen
to write slugs. `_norm_id()` folds, substitutes, strips, clips, falls back to
`step-N` and suffixes on collision; `renames` records every change **first-wins** so
a `needs` that names the original title still resolves. What was renamed is
reported, never silent — a step whose dependency was rewritten to a different node
is a plan that is not the plan the model described.

⚠️ **A FAILURE IS CLASSIFIED, THEN NEW NODES — NEVER A BLIND RETRY (D4.29).** And
the classification really refuses, in four directions, which is the difference
between a gate and a comment:
  * an **open** node row classifies `R_SAFE_TO_RESUME`, and remediation is declined
    (`X_RESUMABLE`) because the checkpoint already holds the remaining steps and
    inventing work for it would duplicate what recovery is going to continue;
  * `never_repeats` (`safety.D_NEVER` — a push, a deployment, an external mutation)
    declines outright, because a remediation step is a thing that would redo it;
  * `permitted` is asked **live** through `core.permissions`, so a node whose
    operation this process may no longer perform earns nothing;
  * and `describe(found)` is what reaches the model, so the classifier's verdict
    *informs the nodes it gets back* rather than merely being logged next to them.
  ⚠️ This is the exact mirror of `schedule.may_repeat()`'s trap, and the two rules
  read backwards from each other on purpose: `may_repeat` must ask **while the row
  is still open**, because a settled row is `R_NON_RECOVERABLE`; `replan` must ask
  **after it settled**, because an open row is resumable. `R_NON_RECOVERABLE` /
  "already settled (failed)" is therefore the *licensed* case here — that row may
  never run again, which is precisely why remediation must be new nodes.

⚠️ **RE-PLANNING ONLY EVER ADDS, AND THE ROUNDS ARE BOUNDED AND REPORTED (D4.28,
D4.30).** `replan()` goes through `store.extend()` and nothing else, so
`validate_mutation()` is what enforces *completed work stays completed*: it refuses
to drop a settled node (`P_LOST_NODE`), to rewire one (`P_REWIRED`) or to grow past
`AGENT2_DAG_MAX_MUTATIONS` (`P_NO_GROWTH`). The round tally is the engine's
`GraphState.extensions` — durable, on the run row, because dual mode is two
processes over one `agent2.db` and an in-memory counter would hand each of them a
full allowance. `rounds_of()` is the ONE translation from that field to the ceiling
`config.DYNAMIC_MAX_ROUNDS` states, and `Draft.rounds`/`rounds_left` report it.

⚠️ **CONTEXT COMES FROM THE BROKER, AND THE SKILLS HALF COMES THROUGH IT (D4.32).**
One `broker.assemble()` call, excluding exactly one source — `workflow_state`,
because the planner is what *produces* a workflow and feeding it the state of the
run it is planning for is a loop. Everything else a turn would see, the planner
sees: the project doc, git, the standing memories and rules. ⚠️ There is
deliberately **no second `skills.for_turn()` call here**: the broker's own `skills`
collector is that call, so asking again would be a second discovery pass and a
second selection inside one prompt — `broker._collect_skills`' rule, from the other
side. And *"a worker gets only what it needs"* is enforced by the broker's own
`budget.apply()`, not by a second ceiling in this file.

⚠️ **`Draft` IS NOT `schedule.Plan`, AND THE NAMES STAY APART.** `schedule.Plan`
answers *which nodes may start now*; a `Draft` is *a goal turned into a graph*. Two
facts, and this repo lets no word carry both — the same rule that made the engine's
round tally `extensions`. `X_*` is likewise a fourth closed vocabulary and shares no
value with `schedule.HOLD_CODES`, `schedule.REASONS`, `select.REASONS`' whys or
`classify.R_*`.

Nothing here raises: every entry point returns a `Draft` whose `ok` is False and
whose `reason` is drawn from `REFUSALS`, and a model that is absent, unreachable or
incoherent degrades to a one-step plan that is the goal itself (`SRC_GOAL`) with a
`note` saying which of the four things went wrong. Hence its written BLE001/S110
exemption.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from agent2 import config
from agent2.core.dag import model as _m
from agent2.core.dag import store as _store
from agent2.core.workflow import graph as _graph
from agent2.core.workflow import runner as _runner

# ── Modes ─────────────────────────────────────────────────────────────────────
#
# ⚠️ TWO, and the default is the one that writes nothing. PLAN is *show me what you
# would do*; AUTO is *and then do it*. `/workflow auto <goal>` with no mode word is
# PLAN, because the spec's rule for the whole command is that it executes nothing
# unless a verb says so.

MODE_PLAN = "plan"
MODE_AUTO = "auto"
MODES = (MODE_PLAN, MODE_AUTO)

# Where a draft's steps came from. ⚠️ `SRC_GOAL` is not an error — it is the honest
# floor: no model was reachable, so the goal is the single step, which is a usable
# one-node workflow rather than a refusal. `projectdoc.narrate()`'s posture.
SRC_MODEL = "model"
SRC_GOAL = "goal"
SOURCES = (SRC_MODEL, SRC_GOAL)

#: What `graph.make_def(source=…)` stamps on a generated definition, and what
#: `runner.instantiate(surface=…)` records on the run. A run a planner authored is
#: distinguishable from one a file authored, for the whole life of the row.
SURFACE = "dynamic"
DEF_SOURCE = "planner"

#: `truncated_by`, in the loader's vocabulary shape and sharing no word with it.
T_STEPS = "steps"

# ── Why the planner declined ──────────────────────────────────────────────────
#
# ⚠️ A FOURTH CLOSED VOCABULARY. No value here appears in `schedule.HOLD_CODES`
# ("workers", "max_running", "kind_limit", "kind_budget", "resource_held",
# "claimed", "upstream_failed"), in `schedule.REASONS` ("finished", "cancelled",
# "paused", "held", "blocked", "budget", "exhausted", "no_run"), in
# `select.REASONS`' whys ("disabled", "shadowed", "cap", "chars", "empty") or in
# `classify.R_*`. A shared spelling would make two different questions read as one
# answer, which is the drift `extensions` was renamed to avoid.

X_OFF = "planner_off"            # AGENT2_DYNAMIC_WORKFLOW=0
X_NO_GOAL = "no_goal"            # nothing to plan for
X_BAD_MODE = "bad_mode"          # a mode word outside MODES
X_NOTHING_PLANNED = "nothing_planned"    # every step coerced away to nothing
X_UNRUNNABLE = "unrunnable_plan"         # the validator refused the graph
X_UNKNOWN_RUN = "unknown_run"            # replan: no such run
X_SETTLED = "run_settled"                # replan: every node already settled
X_NO_FAILURE = "no_failure"              # replan: nothing failed to react to
X_ROUNDS = "rounds_spent"                # replan: DYNAMIC_MAX_ROUNDS reached
X_RESUMABLE = "still_resumable"          # replan: the row is open, recovery owns it
X_NON_REPEATABLE = "non_repeatable"      # replan: safety.D_NEVER
X_FORBIDDEN = "not_permitted"            # replan: the capability is gone
X_ENGINE = "engine_refused"              # the DAG said no; its reason is carried

REFUSALS = (X_OFF, X_NO_GOAL, X_BAD_MODE, X_NOTHING_PLANNED, X_UNRUNNABLE,
            X_UNKNOWN_RUN, X_SETTLED, X_NO_FAILURE, X_ROUNDS, X_RESUMABLE,
            X_NON_REPEATABLE, X_FORBIDDEN, X_ENGINE)

# ── Ceilings that belong to the coercion, not to the plan ─────────────────────
#
# These bound how much of somebody else's text this module will carry, and none of
# them is a second copy of a declared ceiling: the *plan* is bounded by
# `config.DYNAMIC_MAX_STEPS` and `config.WORKFLOW_MAX_NODES`, the *prompt* by the
# broker's own budget, and the *round count* by `config.DYNAMIC_MAX_ROUNDS`.

#: Characters of a model reply we will look at. A 24-step plan is a few kB; this is
#: an order of magnitude over, and it stops a runaway generation from becoming a
#: regex pass over a megabyte.
MAX_REPLY_CHARS = 24_000
#: Failed nodes described to the model in one re-plan. Past it the rest are counted,
#: because a prompt listing forty failures is a prompt that plans for none of them.
MAX_FAILURES = 6
#: Existing node ids named to the model in one re-plan, so a remediation step can
#: depend on work that already ran. `for_turn()`'s `NAMED_NODES` rule, wider because
#: this is off the turn path.
MAX_NAMED = 24


# ── The one thing every entry point returns ───────────────────────────────────

@dataclass
class Draft:
    """A goal, turned into a graph — and, in AUTO, the run that graph became.

    ⚠️ **ONE RETURN TYPE FOR ALL THREE ENTRY POINTS**, so a surface renders a
    plan, a started run and a re-plan with one renderer. `draft()` leaves `run`
    None (nothing was written); `start(mode=MODE_AUTO)` and `replan()` set it, and
    both get it from a function that returns the same `store.GraphRun`.

    ⚠️ `nodes`, `started`, `run_id`, `rounds` and `rounds_left` are **properties**,
    never fields. A `Draft` carrying its own copy of what `defn.nodes` or
    `run.extensions` already says is a second declaration that drifts the first
    time somebody sets one and forgets the other — `RunState.done`'s rule.

    ⚠️ There is no `Step` dataclass, deliberately. `dag.model.Node` is what a step
    IS and `store.NodeView` is what a step LOOKS LIKE before it runs; a third
    spelling here would be the shape the one-declaration rule exists to prevent,
    and it is the same reasoning that makes `WorkflowDef` literally `dag.Graph`.
    """

    ok: bool = False
    reason: str = ""
    mode: str = MODE_PLAN
    goal: str = ""
    name: str = ""
    #: The graph itself — a `WorkflowDef`, which IS a `dag.Graph`. For `replan()`
    #: this is the graph **after** the addition, read back from the rows.
    defn: _m.Graph | None = None
    validation: _m.Validation | None = None
    #: `store.NodeView` per node: the display half, READY/BLOCKED as the first wave
    #: falls out. Empty when the validator refused, because there is no order to
    #: read from — `store.plan()`'s `PENDING` rule.
    preview: list = field(default_factory=list)
    source: str = SRC_GOAL
    note: str = ""
    #: Ids added by this draft. For `draft()` that is every node; for `replan()`
    #: only the remediation ones, which is what lets a surface show what changed.
    added: tuple[str, ...] = ()
    #: `{"step": id, "needs": id}` per dependency `_acyclic()` removed, and
    #: `{"from": raw, "to": id}` per id `_norm_id()` rewrote. Reported, never
    #: silent: a plan whose edges were quietly dropped is not the plan described.
    dropped: list = field(default_factory=list)
    renames: list = field(default_factory=list)
    truncated: bool = False
    truncated_by: str = ""
    #: Set only when something was written. A `runner.Run`, which IS a
    #: `store.GraphRun`.
    run: _store.GraphRun | None = None
    #: Failed nodes a `replan()` declined to remediate, each with the classifier's
    #: own reason. Never silence: a failure absent from a re-plan with no stated
    #: reason is indistinguishable from a planner that did not look.
    declined: list = field(default_factory=list)

    @property
    def nodes(self) -> tuple:
        return tuple(getattr(self.defn, "nodes", ()) or ())

    @property
    def steps(self) -> int:
        return len(self.nodes)

    @property
    def started(self) -> bool:
        """Was anything written? ⚠️ Not `mode == MODE_AUTO` — an AUTO draft the
        engine refused wrote nothing, and reading the request as the outcome is how
        a refusal gets rendered as a running workflow."""
        return bool(self.run is not None and getattr(self.run, "ok", False))

    @property
    def run_id(self) -> str:
        return str(getattr(self.run, "run_id", "") or "")

    @property
    def rounds(self) -> int:
        """Re-plans this run has had. The engine's `extensions`, in planner words."""
        return int(getattr(self.run, "extensions", 0) or 0)

    @property
    def rounds_left(self) -> int:
        return max(0, int(config.DYNAMIC_MAX_ROUNDS) - self.rounds)

    @property
    def runnable(self) -> bool:
        """⚠️ TWO FACTS, `authoring.Authored`'s rule: a draft can be produced
        successfully and still not form a graph the runner would accept."""
        return bool(self.validation is not None and self.validation.ok)

    def to_payload(self) -> dict:
        v = self.validation
        return {
            "ok": self.ok,
            "reason": self.reason,
            "mode": self.mode,
            "goal": self.goal,
            "name": self.name,
            "source": self.source,
            "note": self.note,
            "steps": self.steps,
            "added": list(self.added),
            "dropped": list(self.dropped),
            "renames": list(self.renames),
            "truncated": self.truncated,
            "truncated_by": self.truncated_by,
            "runnable": self.runnable,
            "started": self.started,
            "run_id": self.run_id,
            "rounds": self.rounds,
            "rounds_left": self.rounds_left,
            "max_rounds": int(config.DYNAMIC_MAX_ROUNDS),
            "declined": list(self.declined),
            "problems": list(v.problems) if v is not None else [],
            "warnings": list(v.warnings) if v is not None else [],
            "plan": [n.to_payload() for n in self.preview],
        }


# ── Coercion: somebody else's text into something the engine accepts ──────────

_ID_BAD = re.compile(r"[^a-z0-9._-]+")


def _text(val, limit: int) -> str:
    out = " ".join(str(val or "").split()).strip()
    return out[:limit]


def _norm_id(raw, *, seq: int, taken) -> str:
    """One id, folded into `NODE_ID_RE` or replaced by `step-N`.

    ⚠️ `strip("-._")` is what makes the first character legal: every remaining
    character came from `[a-z0-9._-]`, so removing the three that may not lead
    leaves one that may. The clip is last, and a trailing `-`/`.`/`_` is legal, so
    it needs no second strip.
    """
    base = _ID_BAD.sub("-", _m.fold_id(raw)).strip("-._")[:_ID_MAX]
    if not base:
        base = f"step-{seq}"
    out, n = base, 1
    while out in taken:
        n += 1
        suffix = f"-{n}"
        out = f"{base[:_ID_MAX - len(suffix)]}{suffix}"
    return out


_ID_MAX = 64


def _steps_from(reply, *, max_steps: int, known=(),
                kind: str = "") -> tuple[list, list, list, bool]:
    """A model's step list → node records the engine will accept, plus the receipts.

    Returns `(nodes, dropped, renames, truncated)`. Every node in `nodes` is a plain
    dict `make_node()` reads; nothing here builds a `Node`, because `make_node` is
    the one declaration of what a node's fields mean.

    ⚠️ **`kind` IS THE DRIVER'S STAMP AND IT OUTRANKS THE MODEL'S WORD, and `""`
    means the driver did not stamp one.** A `Node.kind` is a *consumer's* taxonomy
    (`core/dag/model.py` stores it and never branches on it), so who is entitled to
    name one is a real question with one answer: whoever knows what the batch IS.
    The re-plan prompt never asks for a kind, so a word arriving in the reply is the
    model guessing — and the guess that matters is `check`, which
    `ultracode.stages.VERIFY_KINDS` would then treat as verification, i.e. the model
    grading its own remediation. That is the one failure *"UltraCode must not trust
    its own generated conclusion"* names. Default `""` falls straight through to the
    reply, so a workflow re-plan behaves exactly as it did before this parameter
    existed.

    ⚠️ **`known` (the ids that ALREADY EXIST, for a `replan`) IS APPLIED HERE, IN
    PASS ONE, BECAUSE THERE IS ONLY ONE `_acyclic` PASS AND IT DELETES.** A second
    pass outside this function cannot restore an edge the first one removed, so a
    remediation step naming the very node that failed had its one useful dependency
    dropped and then ran immediately, in parallel with the failure it was meant to
    follow — and `dropped` reported the legitimate edge as junk. Seeding `taken` from
    the same list is the other half: a new step that reuses an existing id is renamed
    *before* `lookup` is built, so a `needs` on the raw spelling resolves to the new
    node rather than silently onto the settled one.
    """
    raw_steps = []
    if isinstance(reply, dict):
        for key in ("steps", "nodes", "plan", "tasks"):
            got = reply.get(key)
            if isinstance(got, list):
                raw_steps = got
                break
    elif isinstance(reply, list):
        raw_steps = reply

    truncated = len(raw_steps) > max_steps
    raw_steps = raw_steps[:max_steps]

    # Pass one: an id for every step, and a rename map so a `needs` naming the raw
    # spelling still resolves. ⚠️ FIRST WINS — two steps titled the same thing get
    # two ids, and a `needs` on that title can only mean the earlier one, which is
    # the only reading that keeps every edge pointing backwards.
    taken: set[str] = {str(k) for k in known if str(k or "")}
    renames: list = []
    ids: list[str] = []
    bodies: list = []
    for i, step in enumerate(raw_steps):
        body = step if isinstance(step, dict) else {"title": str(step or "")}
        raw_id = ""
        for key in ("id", "name", "node", "step"):
            if str(body.get(key) or "").strip():
                raw_id = str(body.get(key))
                break
        title = _text(body.get("title") or body.get("summary") or raw_id, _m.MAX_TITLE)
        nid = _norm_id(raw_id or title, seq=i + 1, taken=taken)
        taken.add(nid)
        ids.append(nid)
        bodies.append(body)
        for spelling in (raw_id, title):
            folded = _m.fold_id(spelling)
            if folded and folded != nid and folded not in _renames_index(renames):
                renames.append({"from": folded, "to": nid})

    lookup = {r["from"]: r["to"] for r in renames}
    lookup.update({nid: nid for nid in ids})

    # Pass two: bodies, with `needs` filtered by `_acyclic`.
    nodes: list = []
    for i, (nid, body) in enumerate(zip(ids, bodies, strict=True)):
        title = _text(body.get("title") or body.get("summary") or nid, _m.MAX_TITLE)
        nodes.append({
            "id": nid,
            "title": title or nid,
            "instruction": _text(body.get("instruction") or body.get("prompt")
                                 or body.get("description") or body.get("do")
                                 or title, _m.MAX_TEXT),
            "needs": _needs_of(body, lookup),
            "kind": _text(kind or body.get("kind") or body.get("type"), _m.MAX_LABEL),
            "resource": _text(body.get("resource") or body.get("lock"), _m.MAX_LABEL),
            "priority": body.get("priority"),
            "seq": i,
        })
    nodes, dropped = _acyclic(nodes, known=known)
    return nodes, dropped, renames, truncated


def _renames_index(renames) -> set:
    return {r["from"] for r in renames}


def _needs_of(body, lookup) -> list:
    raw = body.get("needs")
    for key in ("depends_on", "after", "requires"):
        if raw in (None, "", (), []):
            raw = body.get(key)
    if isinstance(raw, str):
        raw = [p for p in re.split(r"[,\s]+", raw) if p]
    if not isinstance(raw, (list, tuple)):
        raw = []
    out: list = []
    for dep in raw:
        folded = _m.fold_id(dep)
        mapped = lookup.get(folded, folded)
        if mapped and mapped not in out:
            out.append(mapped)
    return out


def _acyclic(nodes, *, known=()) -> tuple[list, list]:
    """Keep only the edges that point BACKWARDS in declaration order.

    ⚠️ **THIS IS THE WHOLE REASON A GENERATED PLAN IS ALWAYS VALID-BY-SHAPE**, and
    it buys three of the validator's ten problems at once: a step's own id is not in
    `seen` when its `needs` are filtered (no `P_SELF_DEP`), an id that never appears
    earlier is dropped (no `P_UNKNOWN_DEP`), and an edge can only ever point at
    something already declared (no `P_CYCLE`). It is also what licenses
    `DYNAMIC_MAX_STEPS` to clip a suffix instead of refusing the plan: with every
    edge pointing backwards, declaration order IS a topological order, so the tail
    can go without leaving one dangling `needs`.

    *known* seeds `seen` with ids that already exist — how a `replan()` step is
    allowed to depend on work that has already run.
    """
    seen = {str(k) for k in known if str(k or "")}
    out: list = []
    dropped: list = []
    for node in nodes:
        kept: list = []
        for dep in node.get("needs") or ():
            if dep in seen:
                kept.append(dep)
            else:
                dropped.append({"step": node["id"], "needs": dep})
        node = dict(node)
        node["needs"] = kept
        out.append(node)
        seen.add(node["id"])
    return out, dropped


# ── Asking a model, once, off the turn path ───────────────────────────────────

_PROMPT = """You are planning a multi-step engineering task for an autonomous agent.

GOAL
{goal}

{context}
Reply with ONE JSON object and nothing else:

{{"name": "<short-slug>", "steps": [
  {{"id": "<short-slug>", "title": "<one line>",
   "instruction": "<what to do, self-contained>",
   "needs": ["<id of a step that must finish first>"],
   "kind": "<command|model|mcp — optional>"}}
]}}

RULES
- At most {max_steps} steps. Fewer is better; do not pad.
- Every "needs" entry must name a step declared EARLIER in the list.
- Each "instruction" must stand alone: the worker running it sees that step only.
- Ids are lowercase, and use only letters, digits, dot, dash and underscore.
"""

_REPLAN = """A running multi-step plan has failures. Add REMEDIATION steps.

ORIGINAL GOAL
{goal}

WHAT FAILED (already classified — the failed steps themselves will NOT be re-run)
{failures}

STEPS THAT ALREADY EXIST (a new step may depend on any of these)
{named}

{context}
Reply with ONE JSON object and nothing else, holding ONLY the NEW steps:

{{"steps": [
  {{"id": "<short-slug>", "title": "<one line>",
   "instruction": "<what to do, self-contained>",
   "needs": ["<id of an existing or earlier new step>"]}}
]}}

RULES
- At most {max_steps} new steps. Add nothing that merely repeats a failed step.
- Every "needs" entry must name an existing step or one declared earlier here.
- Each "instruction" must stand alone.
"""


def _context_for(goal: str, *, chat_id: str = "", model: str = "",
                 mode_key: str = "") -> str:
    """What a planner is told about the world — the broker's answer, minus one source.

    ⚠️ **ONE `assemble()` CALL, AND THE SKILLS HALF ARRIVES INSIDE IT.** D4.32 asks
    for skills *and* the broker; the broker's `skills` collector already **is**
    `core.skills.for_turn()`, so calling it again here would be a second discovery
    pass and a second selection for one prompt. `workflow_state` is the only
    exclusion, because the planner is what produces a workflow and handing it the
    state of the run it is planning for is a loop.

    ⚠️ Imported lazily and reached only from an explicitly-invoked planner:
    `broker._collect_workflow` imports `core.workflow`, so an eager import here
    would close a cycle, and the broker's git subprocesses and project read are a
    cost that must never land on a turn.

    Total: no context is a thinner prompt, never a refusal.
    """
    try:
        from agent2.core import broker as _broker
        from agent2.core.broker import sources as _sources
    except Exception:
        return ""
    try:
        bundle = _broker.assemble(
            chat_id=chat_id, message=goal, model_key=model, mode_key=mode_key,
            surface=SURFACE, exclude=frozenset({_sources.SOURCE_WORKFLOW}),
        )
        return str(bundle.prompt_tail() or "")
    except Exception:
        return ""


def _ask(prompt: str, *, ask=None) -> tuple[dict, str, str]:
    """Ask once. Returns `(reply, source, note)` and never raises.

    Four distinct degradations, `projectdoc.narrate()`'s four, because a user who
    got a one-step plan needs to know *which* thing went wrong: the model package
    would not import, the call raised, nothing answered, or the answer was not
    usable.
    """
    if ask is None:
        try:
            from agent2.llm.capabilities import ask_one_shot as ask
        except Exception as exc:
            return {}, SRC_GOAL, f"model unavailable: {type(exc).__name__}"
    try:
        raw = ask(prompt) or ""
    except Exception as exc:
        return {}, SRC_GOAL, f"model failed: {type(exc).__name__}"
    if not str(raw).strip():
        return {}, SRC_GOAL, "no model answered"
    try:
        from agent2.llm.capabilities import extract_json
        reply = extract_json(str(raw)[:MAX_REPLY_CHARS])
    except Exception:
        reply = {}
    if not isinstance(reply, dict) or not reply:
        return {}, SRC_GOAL, "the model's reply was not usable"
    return reply, SRC_MODEL, ""


# ── D4.26 + D4.27: a goal becomes a graph ─────────────────────────────────────

def draft(goal: str, *, name: str = "", model: str = "", mode_key: str = "",
          chat_id: str = "", ask=None, max_steps: int | None = None) -> Draft:
    """A goal → a validated `WorkflowDef` and its plan preview. **Writes nothing.**

    The whole of PLAN mode. `store.plan()` is reached — its own docstring is
    *"Nothing is written"* — so a user sees the graph, the waves and every problem
    before a single row, session or capability is spent.

    *ask* is injectable for the same reason `projectdoc.narrate()`'s is: the
    degradation paths are the interesting half and a test may not depend on a key.
    """
    goal = _text(goal, _m.MAX_TEXT)
    out = Draft(mode=MODE_PLAN, goal=goal)
    if not config.DYNAMIC_ENABLED:
        out.reason = X_OFF
        return out
    if not goal:
        out.reason = X_NO_GOAL
        return out

    cap = int(max_steps if max_steps is not None else config.DYNAMIC_MAX_STEPS)
    cap = max(1, cap)
    context = _context_for(goal, chat_id=chat_id, model=model, mode_key=mode_key)
    reply, out.source, out.note = _ask(
        _PROMPT.format(goal=goal, context=(context + "\n") if context else "",
                       max_steps=cap), ask=ask)

    nodes, out.dropped, out.renames, out.truncated = _steps_from(reply, max_steps=cap)
    if out.truncated:
        out.truncated_by = T_STEPS
    if not nodes:
        # ⚠️ THE FLOOR IS A ONE-STEP PLAN, NOT A REFUSAL. A goal is a usable
        # instruction; refusing here would make "no key configured" mean "no
        # planner", when the honest answer is "nobody broke this down for you".
        nodes = [{"id": _norm_id(name or goal, seq=1, taken=set()),
                  "title": _text(goal, _m.MAX_TITLE), "instruction": goal, "seq": 0}]
        out.source = SRC_GOAL
        out.note = out.note or "no steps were proposed"

    out.name = _norm_id(name or reply.get("name") or goal, seq=1, taken=set())
    out.defn = _graph.make_def({"name": out.name, "description": goal, "nodes": nodes},
                               source=DEF_SOURCE, name=out.name)
    out.validation, out.preview = _store.plan(
        out.defn, max_nodes=config.WORKFLOW_MAX_NODES, knob=_graph.KNOB,
        label=_graph.LABEL)
    out.added = tuple(n.id for n in out.defn.nodes)
    out.ok = bool(out.validation.ok)
    if not out.ok:
        out.reason = X_UNRUNNABLE
    return out


def start(goal: str, *, mode: str = MODE_PLAN, chat_id: str = "", cwd: str = "",
          workspace_id: str = "", model: str = "", mode_key: str = "",
          name: str = "", ask=None, max_steps: int | None = None) -> Draft:
    """Plan, and in AUTO mode instantiate. The one entry point `/workflow auto` calls.

    ⚠️ **PLAN IS THE DEFAULT AND IT COMMITS NOTHING.** The spec's rule for
    `/workflow` is that the command executes nothing; AUTO is the explicit second
    word that says otherwise, and it still goes through `runner.instantiate()`,
    which asks the `chat` capability live and refuses by returning.
    """
    mode = _text(mode, 16).lower() or MODE_PLAN
    if mode not in MODES:
        return Draft(mode=MODE_PLAN, goal=_text(goal, _m.MAX_TEXT), reason=X_BAD_MODE)

    out = draft(goal, name=name, model=model, mode_key=mode_key, chat_id=chat_id,
                ask=ask, max_steps=max_steps)
    out.mode = mode
    if mode != MODE_AUTO or not out.ok:
        return out

    out.run = _runner.instantiate(out.defn, chat_id=chat_id, cwd=cwd,
                                  workspace_id=workspace_id, model=model,
                                  mode=mode_key, surface=SURFACE)
    if not getattr(out.run, "ok", False):
        # ⚠️ The engine's own words, carried rather than translated: a caller shown
        # `X_ENGINE` with no detail cannot tell a denied capability from a cycle.
        out.ok = False
        out.reason = str(getattr(out.run, "reason", "") or X_ENGINE)
    return out


# ── D4.28 + D4.29 + D4.30: re-planning a live run ─────────────────────────────

def rounds_of(state) -> int:
    """Re-plans this run has had — THE translation from `extensions` to "rounds".

    Accepts a `store.GraphState` (has `.extensions`), a `runner.RunState` (has
    `.graph_state`) or a `store.GraphRun`. One reader, so the ceiling
    `config.DYNAMIC_MAX_ROUNDS` states is measured against exactly one number.
    """
    for probe in (state, getattr(state, "graph_state", None)):
        got = getattr(probe, "extensions", None)
        if got is not None:
            try:
                return max(0, int(got))
            except (TypeError, ValueError):
                return 0
    return 0


def _classify(node) -> tuple[bool, str, str]:
    """May this failed node earn remediation steps? `core/recovery` decides.

    Returns `(licensed, code, reason)`. ⚠️ **ASK AFTER THE ROW HAS SETTLED** — the
    exact inverse of `schedule.may_repeat()`, whose docstring says to ask *while it
    is open*. Both readings are right for their question: a repeat needs the row
    open because a settled one is `R_NON_RECOVERABLE`; remediation needs it settled
    because an open one is `R_SAFE_TO_RESUME` and recovery already owns it.

    Fails **closed** in the direction that matters here: a classifier that cannot
    answer declines the node rather than inventing work for it.
    """
    task_id = str(getattr(node, "task_id", "") or "")
    if not task_id:
        return False, X_RESUMABLE, "no task row"
    try:
        from agent2.core import tasks as _tasks
        from agent2.core.recovery import classify as _classify_mod
    except Exception as exc:
        return False, X_RESUMABLE, f"classifier unavailable: {type(exc).__name__}"
    try:
        row = _tasks.get(task_id)
        if row is None:
            return False, X_RESUMABLE, "no task row"
        found = _classify_mod.assess(_classify_mod.K_TASK, row)
        if found.needs_verification:
            found = _classify_mod.verify(found, row)
        if found.never_repeats:
            return False, X_NON_REPEATABLE, _classify_mod.describe(found)
        if not found.permitted:
            return False, X_FORBIDDEN, _classify_mod.describe(found)
        if found.classification == _classify_mod.R_SAFE_TO_RESUME:
            return False, X_RESUMABLE, _classify_mod.describe(found)
        return True, "", _classify_mod.describe(found)
    except Exception as exc:
        return False, X_RESUMABLE, f"classifier failed: {type(exc).__name__}"


def replan(run_id: str, *, goal: str = "", ask=None,
           max_steps: int | None = None, kind: str = "") -> Draft:
    """A live run's failures → NEW nodes on the same graph. Only ever adds.

    ⚠️ **THROUGH `store.extend()` AND NOTHING ELSE**, so `validate_mutation()` is
    what keeps D4.28's bar: a settled node cannot be dropped (`P_LOST_NODE`) or
    rewired (`P_REWIRED`), and growth is bounded by `AGENT2_DAG_MAX_MUTATIONS`
    (`P_NO_GROWTH`). This function contains no rule about what may change.

    ⚠️ **BOUNDED ROUNDS, REPORTED (D4.30).** `config.DYNAMIC_MAX_ROUNDS` against the
    run's durable `extensions`; the refusal names the count, and every `Draft`
    carries `rounds`/`rounds_left` whether it refused or not.

    *kind* is the caller's stamp for every node this round adds — `""` (the default)
    leaves the reply's own word in place. See `_steps_from`: the driver knows the
    batch is remediation and the model does not, so `ultracode.engine` passes
    `stages.K_FIX` and a workflow re-plan passes nothing.
    """
    out = Draft(mode=MODE_AUTO, goal=_text(goal, _m.MAX_TEXT))
    if not config.DYNAMIC_ENABLED:
        out.reason = X_OFF
        return out

    rs = _runner.state_for(str(run_id or ""))
    if not rs.exists:
        out.reason = X_UNKNOWN_RUN
        return out
    out.name = rs.name
    out.goal = out.goal or _text(getattr(rs.graph_state, "description", ""), _m.MAX_TEXT) \
        or rs.name
    if rs.finished:
        out.reason = X_SETTLED
        return out

    spent = rounds_of(rs)
    if spent >= int(config.DYNAMIC_MAX_ROUNDS):
        out.reason = X_ROUNDS
        out.note = f"{spent} of {int(config.DYNAMIC_MAX_ROUNDS)} re-plans already used"
        return out

    failures = list(rs.failed)
    if not failures:
        out.reason = X_NO_FAILURE
        return out

    licensed: list = []
    for node in failures[:MAX_FAILURES]:
        allowed, code, why = _classify(node)
        if allowed:
            licensed.append((node, why))
        else:
            out.declined.append({"node": node.node, "reason": code, "why": why})
    if len(failures) > MAX_FAILURES:
        out.note = f"{len(failures) - MAX_FAILURES} further failures were not described"
    if not licensed:
        out.reason = str(out.declined[0]["reason"]) if out.declined else X_NO_FAILURE
        return out

    existing = [n.node for n in rs.nodes]
    cap = max(1, int(max_steps if max_steps is not None else config.DYNAMIC_MAX_STEPS))
    context = _context_for(out.goal, chat_id=rs.chat_id)
    reply, out.source, out.note = _ask(_REPLAN.format(
        goal=out.goal,
        failures="\n".join(
            f"- {n.node}: {_text(n.title, 120)} — {_text(n.error, 200) or 'no error text'}"
            f" [{_text(why, 200)}]" for n, why in licensed) or "- (none)",
        named=", ".join(existing[:MAX_NAMED]) or "(none)",
        context=(context + "\n") if context else "", max_steps=cap), ask=ask)

    # ⚠️ `known=existing` is what makes remediation attach to the work it is
    # remediating, and it is passed INTO `_steps_from` rather than applied after it:
    # ids and `needs` are derived together in one pass, and `_acyclic` only ever
    # deletes, so a correction outside this call arrives after the edge is gone.
    nodes, out.dropped, out.renames, out.truncated = _steps_from(
        reply, max_steps=cap, known=existing, kind=kind)
    if out.truncated:
        out.truncated_by = T_STEPS
    if not nodes:
        out.reason = X_NOTHING_PLANNED
        return out

    out.added = tuple(n["id"] for n in nodes)
    out.run = _store.extend(run_id, nodes, label=_graph.LABEL, knob=_graph.KNOB,
                            max_nodes=config.WORKFLOW_MAX_NODES)
    out.validation = getattr(out.run, "validation", None)
    if not getattr(out.run, "ok", False):
        out.ok = False
        out.reason = str(getattr(out.run, "reason", "") or X_ENGINE)
        return out
    grown = _runner.state_for(str(run_id or ""))
    out.defn = grown.graph_state.graph()
    _, out.preview = _store.plan(out.defn, max_nodes=config.WORKFLOW_MAX_NODES,
                                 knob=_graph.KNOB, label=_graph.LABEL)
    out.ok = True
    return out


# ── Reporting ─────────────────────────────────────────────────────────────────

def describe() -> dict:
    """The posture, for `/workflow auto` with no goal and for the health payload."""
    return {
        "enabled": bool(config.DYNAMIC_ENABLED),
        "modes": list(MODES),
        "default_mode": MODE_PLAN,
        "max_steps": int(config.DYNAMIC_MAX_STEPS),
        "max_rounds": int(config.DYNAMIC_MAX_ROUNDS),
        "max_nodes": int(config.WORKFLOW_MAX_NODES),
        "surface": SURFACE,
        "refusals": list(REFUSALS),
        "sources": list(SOURCES),
    }
