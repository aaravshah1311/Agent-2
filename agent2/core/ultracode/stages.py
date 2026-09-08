"""UltraCode's own vocabulary — the stages of the loop and the kinds of its nodes.

THE one new fact Phase D5 declares. Everything else UltraCode needs already has an
owner: the graph is `core/dag/`, the plan is `core/workflow/dynamic.py`, readiness is
`core.tasks.ready()`, the verdict is `core/verify.py`, the risk class of an operation
is `core/recovery/safety.py`. What none of them can supply is *which stage of an
adaptive engineering loop a run is in*, because no other consumer has one.

⚠️ **THE STAGE IS DERIVED FROM THE ROWS AND NEVER STORED.** `store._state()` is the
DAG core's one builder for `exec_workflows.state` and `execstate.workflow_step()`
**replaces** that column on every node transition, so a stage written into it would be
gone by the next node — that is not a hypothetical, it is the defect the first test to
read `source`/`schema` back out of that column found. `stage_of()` therefore reads a
`GraphState` and answers from the nodes, exactly as `runner.state_for()` recomputes
progress instead of trusting the breadcrumb. The consequence is the one that matters
for D5.45: a run killed mid-flight reports the stage that is true *now*, and there is
no stale copy for a resume to reconcile.

⚠️ **THE STAGES ARE GERUNDS ON PURPOSE, AND THAT IS NOT COSMETIC.** This repo keeps
several closed vocabularies that share no word — `schedule.HOLD_CODES` (why one node
waited) vs `schedule.REASONS` (how a run ended) vs `skills.select.REASONS` (why a
skill was omitted) vs `dynamic.REFUSALS` vs `classify.R_*` vs `verify.VERDICTS` — for
one reason: a word that carries two meanings makes two different facts read as one
field. The spec names the stages PLAN, EXECUTE, VERIFY, ANALYZE. Spelled that way
`"plan"` would already be `dynamic.MODE_PLAN`, `"verify"` would already be
`safety.D_VERIFY`, `"analyze"` would already be `safety.ANALYZE`, and `"done"` would
already be `runner.PHASE_DONE` — four collisions in eleven words, in a module whose
whole job is to sit between those systems. A stage is also a thing a run is *doing*,
so the gerund is the more accurate word as well as the freer one. `STAGE_LABEL`
carries the spec's own uppercase spelling for anything a human reads.

⚠️ **THE GERUNDS BUY NINE WORDS OUT OF ELEVEN, NOT ELEVEN, AND THE TWO EXCEPTIONS ARE
WRITTEN DOWN HERE BECAUSE THEY ARE REAL.** Measured across every module-level string
vocabulary in the package: `verifying` also spells `crash.S_VERIFYING` (a recovery
*record* being checked), and `finished` also spells `schedule.R_FINISHED` (a pump
returned because nothing was dispatchable). The first pair never meets — a recovery
record and a stage are read from different tables by different surfaces. The second
pair **does**: `engine.Cycle.to_payload()` carries `reason` (an `R_*` word) and `stage`
(an `S_*` word) in one flat dict, where `"finished"` under `reason` means *this cycle
drained* — true at the end of every cycle a re-plan then follows — and under `stage`
means *the loop is over*. Two keys, two facts, and only the keys tell them apart.
It stays that way rather than being renamed because every candidate is worse:
`complete` is one letter from `model.STATES`' `completed` (a near-miss reads as one
fact, which is the failure being avoided), `done` collides with `runner.PHASE_DONE`
*and* `schedule.EV_DONE`, `settled` is already a sibling key in that same payload, and
`finishing` would twin with `finalizing` one row above it. Renaming would also ripple
through `STAGE_LABEL`, the three stage sets, both route payloads and the CLI renderer —
a behaviour change made to render a comment true. `test_ultracode.py` asserts the
vocabularies pair-by-pair with exactly these two exceptions named, so a *third*
collision cannot arrive in silence; before that test existed this paragraph claimed
`(asserted)` and nothing asserted it, which is how it came to be wrong about
`verifying` as well.

⚠️ **THE KINDS ARE A LABEL THE DAG CORE STORES AND NEVER BRANCHES ON.** `Node.kind`
exists precisely so a consumer can carry its own taxonomy through the engine
(`store._insert` writes `tasks.CP_KIND`, `NodeView.kind` reads it back), and PART 1's
rule is that the core must contain no `if ultracode:`. So the *dispatch* on kind lives
here and in `engine.work()`, one layer above an engine that only ever looks a kind up
in an injected `kind_limits` table. Four kinds, and the fourth is the point: a `fix`
node is a `build` node the driver did not plan, so telling them apart is how a re-plan
is legible after the fact without a second record of which batch a node arrived in.

Four of the eleven stages are deliberately **not reachable** from `stage_of()` —
`understanding`, `inspecting`, `discovering` and `replanning` all happen inside one
synchronous call, before a row exists or between two graphs, so no row can prove them.
They are reported by the call that walks them (`engine.start()`, `engine.replan()`)
and are absent from a read-back, which is the honest answer: this module never guesses
a stage a run cannot demonstrate.
"""

from __future__ import annotations

from agent2.core.dag import model as _m

__all__ = [
    "DERIVED",
    "GATE_KINDS",
    "KINDS",
    "KIND_LABEL",
    "KIND_STAGE",
    "K_APPROVAL",
    "K_BUILD",
    "K_CHECK",
    "K_FIX",
    "STAGES",
    "STAGE_LABEL",
    "S_ANALYZE",
    "S_DISCOVER",
    "S_EXECUTE",
    "S_FINALIZE",
    "S_FINISHED",
    "S_INSPECT",
    "S_OBSERVE",
    "S_PLAN",
    "S_REPLAN",
    "S_UNDERSTAND",
    "S_VERIFY",
    "TERMINAL_STAGES",
    "TRANSIENT",
    "VERIFY_KINDS",
    "WORK_KINDS",
    "awaiting_approval",
    "describe",
    "rank",
    "stage_of",
]


# ── The loop's stages ─────────────────────────────────────────────────────────
# In loop order, which is the order the spec states them in (D5.34 goal → D5.35
# project → D5.36 skills → D5.37 planning):
#   UNDERSTAND → INSPECT → DISCOVER SKILLS → PLAN → BUILD DAG → EXECUTE →
#   OBSERVE → ANALYZE → VERIFY → (pass ⇒ continue | fail ⇒ RE-PLAN → EXECUTE)
#
# ⚠️ DISCOVER SKILLS SITS BEFORE PLAN, AND `STAGES` BELOW IS THE ONE DECLARATION OF
# THAT ORDER — this comment is prose about it, `rank()` derives from the constant, and
# `test_ultracode.py` pins the two equal. The skills a project has must be in hand
# *before* a model is asked for steps, or the plan is drafted blind to them.
#
# ⚠️ "BUILD DAG" is not a stage here and its absence is deliberate: building the graph
# is `store.create()`'s single write at the end of `planning`, not a phase a run can
# be observed sitting in. A stage nothing can ever report is a stage that lies.

#: A goal arrived and is being read. Reported by `engine.start()`; no rows yet.
S_UNDERSTAND = "understanding"
#: The project is being read — `projectscan.scan()` + the project doc. Still no rows.
S_INSPECT = "inspecting"
#: Which of this project's skills apply — `core.skills.for_turn()`, never a second walk.
S_DISCOVER = "discovering"
#: A model is turning the goal into steps — `dynamic.draft()`. Nothing is written.
S_PLAN = "planning"
#: Nodes are running, or may start now.
S_EXECUTE = "executing"
#: Nothing may start and nothing failed — a gate or a hold is standing.
S_OBSERVE = "observing"
#: Something failed and nothing can proceed — the diagnosis pass (D5.42).
S_ANALYZE = "analyzing"
#: The graph's own verification nodes are what is left to run.
S_VERIFY = "verifying"
#: A failure is being handed back to the planner — `engine.replan()`, between graphs.
S_REPLAN = "replanning"
#: Every node settled; the final verdict has not been taken yet.
S_FINALIZE = "finalizing"
#: Settled and judged. ⚠️ `finalize()` reports this; `stage_of()` never does, because
#: "a verdict was taken" is not a fact any task row records.
S_FINISHED = "finished"

STAGES = (S_UNDERSTAND, S_INSPECT, S_DISCOVER, S_PLAN, S_EXECUTE, S_OBSERVE,
          S_ANALYZE, S_VERIFY, S_REPLAN, S_FINALIZE, S_FINISHED)

#: The spec's own spelling, for anything a human reads. Presentation only — never a key.
STAGE_LABEL = {
    S_UNDERSTAND: "UNDERSTAND",
    S_INSPECT: "INSPECT",
    S_DISCOVER: "DISCOVER SKILLS",
    S_PLAN: "PLAN",
    S_EXECUTE: "EXECUTE",
    S_OBSERVE: "OBSERVE",
    S_ANALYZE: "ANALYZE",
    S_VERIFY: "VERIFY",
    S_REPLAN: "RE-PLAN",
    S_FINALIZE: "FINALIZE",
    S_FINISHED: "COMPLETE",
}

#: The stages a `GraphState` can prove. Everything else is reported by the call that
#: walks it — see the module docstring.
DERIVED = frozenset((S_PLAN, S_EXECUTE, S_OBSERVE, S_ANALYZE, S_VERIFY, S_FINALIZE))
TRANSIENT = frozenset((S_UNDERSTAND, S_INSPECT, S_DISCOVER, S_REPLAN))
TERMINAL_STAGES = frozenset((S_FINISHED,))


def rank(stage: str) -> int:
    """Where *stage* sits in the loop. `-1` for a word that is not a stage.

    An ordering hint for a renderer, never a comparison the driver makes: the loop
    goes *backwards* from `analyzing` to `replanning` to `executing` by design, so a
    higher rank is not progress.
    """
    try:
        return STAGES.index(stage)
    except ValueError:
        return -1


# ── The kinds of node UltraCode builds ────────────────────────────────────────
# ⚠️ Carried on `Node.kind`, which the DAG core stores (`tasks.CP_KIND`) and never
# branches on. A kind reaches the engine in exactly two shapes — a key in the
# `kind_limits` / `kind_budgets` tables `schedule.limits(**over)` accepts, and a
# string on `NodeView.kind` — and in neither of them does the engine know what the
# word means. That is what PART 1's "no `if ultracode:`" buys, and it is why adding a
# fifth kind here needs no change anywhere below this package.

#: A step from the plan: read, write, run, reason — an ordinary agent turn.
K_BUILD = "build"
#: The graph's own verification. ⚠️ It is a NODE and not a post-pass, because
#: *"'Done' is not verification"*: a check that ran outside the graph could not hold
#: work back, and a downstream node would proceed while the check was still pending.
K_CHECK = "check"
#: The human gate (D5.43). Created and immediately PAUSED; the graph's roots depend on
#: it, so nothing at all can start until a person releases it.
K_APPROVAL = "approval"
#: A remediation step a re-plan added. Structurally a `build`; named apart so a graph
#: read back after the fact still shows which work was planned and which was reactive.
K_FIX = "fix"

KINDS = (K_BUILD, K_CHECK, K_APPROVAL, K_FIX)

#: Kinds that do the work — what `engine.work()` runs an agent turn for.
WORK_KINDS = frozenset((K_BUILD, K_FIX))
#: Kinds that judge it. ⚠️ A check node asks `core/verify.py`; it never asks a model
#: whether the work went well, because *"UltraCode must not trust its own generated
#: conclusion"* — a model grading its own output is the failure that rule names.
VERIFY_KINDS = frozenset((K_CHECK,))
#: Kinds that only ever wait for a human.
GATE_KINDS = frozenset((K_APPROVAL,))

#: Which stage a node of each kind places the run in. The lookup `stage_of()` uses
#: instead of asking what a kind means.
KIND_STAGE = {
    K_BUILD: S_EXECUTE,
    K_FIX: S_EXECUTE,
    K_CHECK: S_VERIFY,
    K_APPROVAL: S_OBSERVE,
}

KIND_LABEL = {
    K_BUILD: "build",
    K_CHECK: "verify",
    K_APPROVAL: "approval",
    K_FIX: "fix",
}


def _kinds_of(views) -> set:
    return {(v.kind or K_BUILD) for v in views}


def stage_of(state) -> str:
    """The stage *state*'s rows prove. Total: any junk reads as `planning`.

    Order of the tests is the whole content of this function, and two of them are
    ordered against intuition on purpose:

    * `finished` is tested **before** anything else about the nodes, because a settled
      graph has no running and no dispatchable node and would otherwise fall through
      to `observing` — reading as *waiting for a human* when in fact there is nothing
      left to do.
    * a failure is tested **after** dispatchability, because `tasks.ready()` releases a
      node once its dependencies are *settled* rather than successful: a graph with one
      failed branch and three live ones is executing, not diagnosing. `analyzing` is
      what "something failed and nothing can proceed" looks like, which is exactly the
      condition D5.42's diagnosis pass exists for.

    ⚠️ No query. `GraphState` was already read by the caller and every field consulted
    here is derived from the nodes it holds, so a surface may ask for the stage on
    every render — `for_turn()`'s flatness rule, one layer up.
    """
    nodes = list(getattr(state, "nodes", None) or ())
    if state is None or not getattr(state, "exists", False) or not nodes:
        return S_PLAN
    if getattr(state, "finished", False):
        return S_FINALIZE

    running = [v for v in nodes if v.state == _m.RUNNING]
    if running:
        return S_VERIFY if _kinds_of(running) <= VERIFY_KINDS else S_EXECUTE

    open_now = [v for v in nodes if v.dispatchable]
    if open_now:
        return S_VERIFY if _kinds_of(open_now) <= VERIFY_KINDS else S_EXECUTE

    if any(v.state in _m.UNSUCCESSFUL for v in nodes):
        return S_ANALYZE
    return S_OBSERVE


def awaiting_approval(state) -> list:
    """The gate nodes still holding this run — `NodeView`s, best-priority first.

    ⚠️ PAUSED, and read from the state rather than from a flag. A gate is a paused row
    that `tasks.pause()` created, so it carries **no** `tasks.CP_STOPPED` stamp and
    `store.release_interrupted()` can never mistake it for work a dead process
    abandoned — which is what makes the hold survive a crash instead of being
    helpfully undone by recovery. `NodeView.interrupted` is that stamp, so a gate whose
    row somehow *does* carry one is a crash-park and is excluded here.
    """
    out = [v for v in (getattr(state, "nodes", None) or ())
           if v.state == _m.PAUSED and not v.interrupted
           and (v.kind or "") in GATE_KINDS]
    out.sort(key=lambda v: (v.priority, v.seq, v.node))
    return out


def describe() -> dict:
    """The vocabulary itself, for a surface that renders it. No state, no query."""
    return {
        "stages": list(STAGES),
        "labels": dict(STAGE_LABEL),
        "derived": sorted(DERIVED),
        "transient": sorted(TRANSIENT),
        "kinds": list(KINDS),
        "kind_labels": dict(KIND_LABEL),
        "kind_stage": dict(KIND_STAGE),
        "work_kinds": sorted(WORK_KINDS),
        "verify_kinds": sorted(VERIFY_KINDS),
        "gate_kinds": sorted(GATE_KINDS),
    }
