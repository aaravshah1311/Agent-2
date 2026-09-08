# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/core/recovery/classify.py
────────────────────────────────
Task 25 §3 — *what may be done with ONE interrupted unit of work*, and Task 25 §6
— *did it actually happen?*

Two questions, one module, deliberately. They are the same question asked twice:
`assess()` decides which class of answer is even available given the KIND of
operation, and `verify()` goes and looks at the real world to pick within it.
Splitting them across two files would let a future caller verify without
classifying — and "verified as not applied" for a `git push` would then read as a
licence to push, which is precisely what `safety.D_NEVER` exists to forbid.

The five classes are Task 25 §3's own list:

    R_SAFE_TO_RETRY          repeating it cannot make anything worse
    R_SAFE_TO_RESUME         there is a checkpoint; continue from it
    R_REQUIRES_VERIFICATION  evidence exists; consult it before deciding
    R_NON_RECOVERABLE        nothing can be done unattended
    R_UNKNOWN                we could not even classify it

⚠️ THE VERDICT VOCABULARY IS THE PACKAGE'S, NOT THIS MODULE'S. `V_NOT_APPLIED`,
`V_LIKELY_APPLIED`, `V_UNCERTAIN` and `V_UNVERIFIABLE` are imported from
`recovery/__init__.py`, where Task 3's `verify_step()` already declared and tested
them. Two spellings of "likely applied" is the drift rule 30 forbids — and it
would be invisible, because each reader would keep agreeing with itself. That
import is also why **`__init__.py` must never import this module at import time**:
the package body has to finish executing before a submodule can read a name out of
it. `crash.py` imports both, which is the shape that keeps the dependency
one-directional.

⚠️ THE LEDGER'S DIGESTS OUTRANK `verify_step()`'s mtime HEURISTIC, ALWAYS.
Task 3 could only ask "does the target exist and is it newer than the step?",
because a checkpoint step records a path and nothing else. Task 24's ledger
records a real content digest taken *before* the tool ran, so `verify()` can
answer the much stronger "is the file byte-for-byte what it was?". Where both have
an opinion this one wins; `verify_step()` remains the fallback for a step the
ledger never saw (a task that predates Phase 8, or a call made while
`EXEC_PERSIST` was off). The weaker reader may never overrule the stronger, and
nothing here calls it.

⚠️ A PARTIALLY-APPLIED MULTI-PATH CALL IS `V_UNCERTAIN`, NEVER A MAJORITY VOTE.
`multi_edit_files` writes N files; a crash can land after three of five. Every
per-path outcome is folded with `_fold()`, and any DISAGREEMENT between them
produces `V_UNCERTAIN` — the verdict that routes to a human. Returning
"mostly applied" would be a confident answer to the one question where confidence
is the failure: re-running the edit would then re-apply the three that landed.

⚠️ `post is None` IS THE CRASH SIGNAL, and it is checked before any digest.
`execstate._detail_json()` writes `null` until the call returns, so a row with a
`post` dict finished its work and merely lost the settle write — which is
`V_LIKELY_APPLIED` on the strongest possible evidence, recorded by the process
that did it. An empty dict would be indistinguishable from "finished, touched
nothing", which is why that column is a tri-state and not a boolean.

Everything here is total. A malformed `detail`, a path that cannot be stat'd and a
row from a table this build does not know all produce an assessment — because this
runs inside the startup scan, and a classifier that raised would leave the
remaining units marked interrupted with no process left to look at them. Hence the
module's BLE001/S110 allowance, reviewed for the same reason `broker/budget.py`
carries one.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agent2.core import commands as _cmds
from agent2.core import execstate as _exec
from agent2.core import tasks as _tasks
from agent2.core.recovery import (
    V_LIKELY_APPLIED,
    V_NOT_APPLIED,
    V_UNCERTAIN,
    V_UNVERIFIABLE,
)
from agent2.core.recovery import safety as _safety

# ── The five classes (Task 25 §3) ─────────────────────────────────────────────

R_SAFE_TO_RETRY = "safe_to_retry"
R_SAFE_TO_RESUME = "safe_to_resume"
R_REQUIRES_VERIFICATION = "requires_verification"
R_NON_RECOVERABLE = "non_recoverable"
R_UNKNOWN = "unknown"

CLASSES = (R_SAFE_TO_RETRY, R_SAFE_TO_RESUME, R_REQUIRES_VERIFICATION,
           R_NON_RECOVERABLE, R_UNKNOWN)

# The kinds of unit that can be interrupted. `command`/`tool_call`/`workflow` come
# from Task 24's ledger; `task` and `session` come from `core/tasks.py`, which was
# already durable before Phase 8.
K_COMMAND = "command"
K_TOOL_CALL = "tool_call"
K_WORKFLOW = "workflow"
K_TASK = "task"
K_SESSION = "session"

KINDS = (K_COMMAND, K_TOOL_CALL, K_WORKFLOW, K_TASK, K_SESSION)

# Which ledger table backs each kind (`task`/`session` are read through
# `core.tasks`, so they have no entry — that absence is what stops a caller from
# querying `agent_tasks` from in here).
TABLE_FOR: dict[str, str] = {
    K_COMMAND: "exec_commands",
    K_TOOL_CALL: "exec_tool_calls",
    K_WORKFLOW: "exec_workflows",
}

# How firmly each verdict claims the operation landed. Used only by `_fold()`.
_CONFIDENCE: dict[str, int] = {
    V_NOT_APPLIED: 0,
    V_LIKELY_APPLIED: 1,
    V_UNCERTAIN: 2,
    V_UNVERIFIABLE: 3,
}

# Cap on how much evidence one assessment carries into the DB. Digests are ~71
# chars each, so this bounds the `evidence` column without a second cap knob.
MAX_EVIDENCE_PATHS = _exec.MAX_DETAIL_PATHS


@dataclass
class Assessment:
    """One interrupted unit, classified — and, once `verify()` has run, verified.

    ⚠️ `verdict` is `""` until something has actually looked, which is NOT the same
    fact as `V_UNVERIFIABLE` ("we looked and could not tell"). `crash.py`'s state
    machine distinguishes them: an empty verdict on a unit that needs verification
    means the VERIFYING step has not happened yet, and treating it as
    "unverifiable" would skip straight to review without the evidence.
    """

    kind: str
    ref_id: str
    classification: str = R_UNKNOWN
    operation: str = _safety.UNKNOWN
    disposition: str = _safety.D_VERIFY
    reason: str = ""
    verdict: str = ""
    evidence: dict = field(default_factory=dict)
    session_id: str = ""
    task_id: str = ""

    @property
    def needs_verification(self) -> bool:
        return self.classification == R_REQUIRES_VERIFICATION

    @property
    def never_repeats(self) -> bool:
        """True when no verdict may license an unattended repeat (`safety.D_NEVER`)."""
        return self.disposition == _safety.D_NEVER

    @property
    def permitted(self) -> bool:
        """Does this process STILL hold the capability this operation needs?

        Asked through `safety.permitted()`, i.e. through `core/permissions.py`,
        every time it is read — never cached onto the row. Task 26 §9.
        """
        return _safety.permitted(self.operation)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "ref_id": self.ref_id,
            "classification": self.classification,
            "operation": self.operation,
            "disposition": self.disposition,
            "reason": self.reason,
            "verdict": self.verdict,
            "evidence": dict(self.evidence),
            "session_id": self.session_id,
            "task_id": self.task_id,
            "needs_verification": self.needs_verification,
            "never_repeats": self.never_repeats,
            "permitted": self.permitted,
        }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get(row, key: str, default=""):
    try:
        val = row.get(key) if hasattr(row, "get") else None
    except Exception:
        return default
    return default if val is None else val


def _str(row, key: str) -> str:
    try:
        return str(_get(row, key, "") or "")
    except Exception:
        return ""


def _int(row, key: str):
    raw = _get(row, key, None)
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except Exception:
        return None


def _fold(found: list[str]) -> str:
    """One verdict from many per-path outcomes.

    ⚠️ DISAGREEMENT IS `V_UNCERTAIN`, not the majority and not the strictest.
    Partial application is its own fact and the only correct report of it, because
    it is the one case where both "retry" and "mark complete" are wrong.
    """
    words = [w for w in found if w]
    if not words:
        return V_UNVERIFIABLE
    unique = set(words)
    if len(unique) == 1:
        return words[0]
    real = unique - {V_UNVERIFIABLE}
    if len(real) == 1:
        # Some paths could not be read, the rest agreed. The agreement is not
        # trustworthy over the gap, so this is uncertainty, not the agreement.
        return V_UNCERTAIN
    return V_UNCERTAIN


def _confidence(verdict: str) -> int:
    return _CONFIDENCE.get(str(verdict or ""), _CONFIDENCE[V_UNVERIFIABLE])


def _path_verdict(operation: str, before, now) -> str:
    """One path's outcome, from its digest before the call and its digest now.

    `None` means the path does not exist — `execstate._digest()`'s contract, and
    the reason a delete can be verified at all.
    """
    gone_before = before is None
    gone_now = now is None
    if operation == _safety.DELETE:
        # Desired end state is absence. Absent now ⇒ reached, however it got there.
        if gone_now:
            return V_LIKELY_APPLIED
        return V_NOT_APPLIED if before == now else V_UNCERTAIN
    if gone_now and not gone_before:
        # A write whose target vanished. Something happened, and it was not the
        # write — a truncate-then-crash, or a user deleting it afterwards.
        return V_UNCERTAIN
    if gone_now and gone_before:
        return V_NOT_APPLIED          # never created
    if gone_before and not gone_now:
        return V_LIKELY_APPLIED       # created by the call
    return V_NOT_APPLIED if before == now else V_LIKELY_APPLIED


# ── Assessment (Task 25 §3) ───────────────────────────────────────────────────

def assess(kind: str, row) -> Assessment:
    """Classify one interrupted unit. Total: never raises, always returns.

    ⚠️ Called ONLY for a unit already known to be interrupted. A settled row
    reaching here is answered `R_NON_RECOVERABLE` / "already settled" rather than
    trusted, because Task 25 §2 is explicit that COMPLETED, CANCELLED and SKIPPED
    work is never recovered — and a defensive answer here means that rule holds
    even if a future scan's filter is wrong.
    """
    word = str(kind or "")
    try:
        if word == K_COMMAND:
            return _assess_command(row)
        if word == K_TOOL_CALL:
            return _assess_tool_call(row)
        if word == K_WORKFLOW:
            return _assess_workflow(row)
        if word == K_TASK:
            return _assess_task(row)
        if word == K_SESSION:
            return _assess_session(row)
    except Exception as exc:  # a classifier fault may not end the scan; see docstring
        return Assessment(kind=word, ref_id=_str(row, "id"),
                          classification=R_UNKNOWN,
                          reason=f"classification failed: {str(exc)[:120]}")
    return Assessment(kind=word or "?", ref_id=_str(row, "id"),
                      classification=R_UNKNOWN, reason="unknown unit kind")


def _base(kind: str, row, operation: str) -> Assessment:
    return Assessment(
        kind=kind, ref_id=_str(row, "id"), operation=operation,
        disposition=_safety.disposition(operation),
        session_id=_str(row, "session_id"), task_id=_str(row, "task_id"),
    )


def _settled(assessment: Assessment, status: str, terminal) -> bool:
    if status and status in terminal:
        assessment.classification = R_NON_RECOVERABLE
        assessment.reason = f"already settled ({status})"
        return True
    return False


def _assess_command(row) -> Assessment:
    """A shell command that was in flight (Task 25 §5).

    ⚠️ The KIND comes from the argv, not from the fact that it is a command —
    `safety.kind_for_command()` folds every chained segment, so `ls && rm -rf x`
    is a DELETE. Classifying all commands uniformly as "shell" would have been the
    simpler code and would have let recovery re-run a delete.
    """
    command = _str(row, "command")
    operation = _safety.kind_for_command(command)
    out = _base(K_COMMAND, row, operation)
    status = _str(row, "status")
    if _settled(out, status, _cmds.TERMINAL):
        return out

    pid = _int(row, "process_id")
    if not pid and status in (_cmds.CommandStatus.CREATED, ""):
        # ⚠️ Never launched: `commands.start()` is what assigns a pid, so no pid
        # AND no start means no child process ever existed. This is the one command
        # case where a repeat is safe whatever the operation kind — nothing to be
        # half-done, because nothing ran.
        out.classification = R_SAFE_TO_RETRY
        out.reason = "never started (no process was spawned)"
        out.evidence = {"process_id": None, "status": status or "created"}
        return out

    if not command:
        out.classification = R_NON_RECOVERABLE
        out.reason = "no command recorded"
        return out

    if _safety.may_retry(operation):
        out.classification = R_SAFE_TO_RETRY
        out.reason = f"{operation} command has no external effect"
    else:
        out.classification = R_REQUIRES_VERIFICATION
        out.reason = f"{operation} command was in flight; effects unknown"
    out.evidence = {"process_id": pid, "status": status,
                    "output_lines": _int(row, "output_lines") or 0}
    return out


def _assess_tool_call(row) -> Assessment:
    """A local agent tool call that never returned (Task 25 §3, Task 26 §3)."""
    tool = _str(row, "tool")
    operation = _safety.kind_for_tool(tool)
    out = _base(K_TOOL_CALL, row, operation)
    status = _str(row, "status")
    if _settled(out, status, (_tasks.STEP_COMPLETED, _tasks.STEP_FAILED)):
        return out

    detail = _exec.parse_detail(_get(row, "detail", ""))
    paths = [p for p in (detail.get("paths") or []) if p]
    post = detail.get("post")
    out.evidence = {"tool": tool, "paths": len(paths),
                    "paths_total": int(detail.get("paths_total") or len(paths)),
                    "post_recorded": post is not None}

    if _safety.may_retry(operation):
        out.classification = R_SAFE_TO_RETRY
        out.reason = f"{operation} tool has no external effect"
        return out

    if post is None and not paths and not detail:
        # No `detail` at all — a legacy row, or one written while EXEC_PERSIST was
        # off. There is nothing to check and the operation may not be repeated
        # blind, so this is the honest end of the road.
        out.classification = R_NON_RECOVERABLE
        out.reason = "no recorded evidence for a non-repeatable operation"
        return out

    out.classification = R_REQUIRES_VERIFICATION
    out.reason = (f"{operation} tool call did not complete; "
                  f"{'post-state recorded' if post is not None else 'post-state missing'}")
    return out


def _assess_workflow(row) -> Assessment:
    """A workflow run (Task 25 §7/§8).

    ⚠️ ALWAYS RESUME, NEVER RESTART — Task 25 §7 in one line: *"completed nodes
    stay completed"*. The resume position is `step_index`, which the executor
    stamped as it advanced, and it is carried in `evidence` rather than acted on
    here because Phase 12 owns the executor. Recovery's job today is to make sure
    the position survives and that nothing restarts the run from zero.
    """
    out = _base(K_WORKFLOW, row, _safety.GENERATE)
    status = _str(row, "status")
    if _settled(out, status, (_tasks.STEP_COMPLETED, _tasks.STEP_FAILED,
                              _cmds.CommandStatus.CANCELLED)):
        return out
    index = _int(row, "step_index") or 0
    total = _int(row, "total_steps") or 0
    out.evidence = {"step": _str(row, "step"), "step_index": index,
                    "total_steps": total, "name": _str(row, "name")}
    if index <= 0:
        out.classification = R_SAFE_TO_RETRY
        out.reason = "no step completed; the run may start over"
        return out
    out.classification = R_SAFE_TO_RESUME
    out.reason = f"resume at step {index}" + (f" of {total}" if total else "")
    return out


def _assess_task(task) -> Assessment:
    """A task left RUNNING by a dead worker (Task 25 §6).

    The checkpoint IS the resume point, so the default is RESUME — but a checkpoint
    holding a destructive step that was in flight demotes it to verification,
    because that step is exactly the one that must not be replayed.
    """
    out = Assessment(
        kind=K_TASK, ref_id=str(getattr(task, "id", "") or ""),
        operation=_safety.GENERATE, disposition=_safety.D_RETRY,
        session_id=str(getattr(task, "session_id", "") or ""),
        task_id=str(getattr(task, "id", "") or ""),
    )
    status = str(getattr(task, "status", "") or "")
    if status in _tasks.TERMINAL:
        out.classification = R_NON_RECOVERABLE
        out.reason = f"already settled ({status})"
        return out
    try:
        view = _tasks.checkpoint_view(task) or {}
    except Exception:
        view = {}
    pending = bool(view.get("destructive_pending"))
    out.evidence = {"completed": len(view.get("completed") or []),
                    "remaining": len(view.get("remaining") or []),
                    "destructive_pending": pending,
                    "status": status}
    if pending:
        out.operation = _safety.UNKNOWN
        out.disposition = _safety.disposition(_safety.UNKNOWN)
        out.classification = R_REQUIRES_VERIFICATION
        out.reason = "a destructive step was in flight when the worker died"
        return out
    out.classification = R_SAFE_TO_RESUME
    out.reason = "checkpoint holds the remaining steps"
    return out


def _assess_session(row) -> Assessment:
    out = _base(K_SESSION, row, _safety.GENERATE)
    out.session_id = _str(row, "id")
    out.classification = R_SAFE_TO_RESUME
    out.reason = "session holds open tasks"
    out.evidence = {"open_tasks": _int(row, "open_tasks") or 0}
    return out


# ── Verification (Task 25 §6, Task 26 §3) ─────────────────────────────────────

def verify(assessment: Assessment, row=None) -> Assessment:
    """Consult the real world and set `assessment.verdict`. Returns the same object.

    ⚠️ ONLY EVER CALLED FOR `R_REQUIRES_VERIFICATION`. A `R_SAFE_TO_RETRY` unit has
    nothing to verify and a `R_NON_RECOVERABLE` one has nothing that a verdict
    could change; running the verifier anyway would attach evidence that reads like
    a licence next to a class that is not one.

    ⚠️ READ-ONLY, WITHOUT EXCEPTION. It stats files and (in Task 26) asks `git`
    read-only questions. Nothing here writes, deletes, commits, pushes or calls a
    network endpoint — a verifier that could act would be an approval gate nobody
    designed, the same reason `cli/diffview.open_viewer()` may not write.
    """
    try:
        if assessment.classification != R_REQUIRES_VERIFICATION:
            return assessment
        if assessment.kind == K_TOOL_CALL:
            return _verify_tool_call(assessment, row)
        if assessment.kind == K_COMMAND:
            return _verify_command(assessment, row)
        if assessment.kind == K_TASK:
            return _verify_task(assessment, row)
        assessment.verdict = V_UNVERIFIABLE
        assessment.reason = f"{assessment.kind} effects are not knowable from here"
    except Exception as exc:  # a verifier fault means "we could not tell"; see docstring
        assessment.verdict = V_UNVERIFIABLE
        assessment.reason = f"verification failed: {str(exc)[:120]}"
    return assessment


def _verify_tool_call(assessment: Assessment, row) -> Assessment:
    detail = _exec.parse_detail(_get(row, "detail", ""))
    post = detail.get("post")
    pre = detail.get("pre") or {}
    paths = [p for p in (detail.get("paths") or []) if p][:MAX_EVIDENCE_PATHS]

    if post is not None:
        # ⚠️ The strongest evidence there is: the process that ran the call wrote
        # its post-state before dying, so the call itself finished. Only the settle
        # write was lost. Nothing needs re-doing.
        assessment.verdict = V_LIKELY_APPLIED
        assessment.reason = "the call recorded its post-state; only the settle was lost"
        assessment.evidence = dict(assessment.evidence, post_recorded=True)
        return assessment

    if not paths:
        assessment.verdict = V_UNVERIFIABLE
        assessment.reason = "no target path was recorded"
        return assessment

    now = _exec.digests(paths)
    outcomes: list[str] = []
    changed = 0
    for path in paths:
        before = pre.get(path, None) if isinstance(pre, dict) else None
        current = now.get(path, None)
        found = _path_verdict(assessment.operation, before, current)
        outcomes.append(found)
        if found != V_NOT_APPLIED:
            changed += 1
    verdict = _fold(outcomes)
    assessment.verdict = verdict
    assessment.evidence = dict(
        assessment.evidence,
        checked=len(paths), changed=changed,
        # Digests only — the whole point of recording a digest instead of content.
        pre={k: pre.get(k) for k in paths} if isinstance(pre, dict) else {},
        now=dict(now),
    )
    assessment.reason = {
        V_NOT_APPLIED: "every target is byte-identical to before the call",
        V_LIKELY_APPLIED: "every target already reflects the call",
        V_UNCERTAIN: "targets disagree — the call landed only partly",
        V_UNVERIFIABLE: "the targets could not be measured",
    }.get(verdict, "verification inconclusive")
    return assessment


def _verify_command(assessment: Assessment, row) -> Assessment:
    """A shell command's effects.

    ⚠️ HONESTLY `V_UNVERIFIABLE` IN TASK 25, and that is not a stub. Task 24
    records no target paths for `run_command` on purpose — *"a shell command's
    targets are not knowable from its argv, and guessing them would hand Task 26 a
    confident wrong answer"* — so there is no local evidence to compare. Task 26
    adds the two cases where evidence DOES exist off to the side: a `GIT_COMMIT`
    (ask `git log`) and a `DATABASE_MUTATION` (ask the migration ledger). Until
    then this returns the verdict that routes to a human, which is the correct
    answer rather than a missing one.
    """
    lines = int(_int(row, "output_lines") or 0)
    assessment.verdict = V_UNVERIFIABLE
    assessment.evidence = dict(assessment.evidence, output_lines=lines)
    assessment.reason = (
        "a shell command's effects are not knowable from its argv"
        if not lines else
        f"the command had already produced {lines} line(s) of output")
    return assessment


def _verify_task(assessment: Assessment, task) -> Assessment:
    """A task whose in-flight step was destructive.

    ⚠️ DELEGATES TO `verify_step()` — the mtime heuristic — and says so in the
    reason. This is the ONE place the weaker reader is correct to use: the ledger
    has no row for this unit (a checkpoint step is not a tool call), so its
    stronger evidence does not exist here. Where both have an opinion, the ledger's
    row is assessed as a `tool_call` and wins on its own merits.
    """
    from agent2.core import recovery as _pkg
    steps = []
    try:
        view = _tasks.checkpoint_view(task) or {}
        steps = list(view.get("steps") or [])
    except Exception:
        steps = []
    outcomes: list[str] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        if not step.get("destructive") or step.get("status") != _tasks.STEP_RUNNING:
            continue
        # ⚠️ `verify_step()` returns an EVIDENCE DICT — `{tool, target, at, verdict,
        # evidence}` — not a verdict word. Appending the dict itself makes `_fold`
        # raise `unhashable type: 'dict'` on its `set(words)`, `verify()` swallows
        # that into `V_UNVERIFIABLE`, and every destructive in-flight step then
        # reaches a human carrying a Python type error where its evidence should be.
        # It fails in the safe direction, which is exactly why nothing would report
        # it: the reason below is unreachable dead code the moment this drifts.
        found = _pkg.verify_step(step)
        outcomes.append(str((found or {}).get("verdict") or "") or V_UNVERIFIABLE)
    if not outcomes:
        assessment.verdict = V_UNVERIFIABLE
        assessment.reason = "no in-flight destructive step could be located"
        return assessment
    assessment.verdict = _fold(outcomes)
    assessment.evidence = dict(assessment.evidence, steps_checked=len(outcomes))
    assessment.reason = ("checkpoint mtime heuristic: "
                         + ", ".join(sorted(set(outcomes))))
    return assessment


# ── Reporting ─────────────────────────────────────────────────────────────────

def safe_to_repeat(assessment: Assessment) -> bool:
    """May recovery repeat this unit, unattended, right now?

    ⚠️ FIVE INDEPENDENT CONDITIONS, ALL REQUIRED, AND THIS IS THE ONLY PLACE THEY
    ARE ANDed TOGETHER. A caller that re-derived any one of them would be a second
    opinion on the question the whole phase exists to answer:

      1. the class permits it (`R_SAFE_TO_RETRY`, or verified `V_NOT_APPLIED`)
      2. the operation kind is not `D_NEVER` (`git push`, deployment, API write)
      3. this process still holds the capability (Task 26 §9, asked live)
      4. a unit needing verification has actually been verified (`verdict` set)
      5. nothing verified as applied, partly applied or unmeasurable

    Everything else in this phase reads this function's answer; nothing re-decides
    it.
    """
    try:
        if assessment.never_repeats:
            return False
        if not assessment.permitted:
            return False
        if assessment.classification == R_SAFE_TO_RETRY:
            return True
        if assessment.classification != R_REQUIRES_VERIFICATION:
            return False
        return assessment.verdict == V_NOT_APPLIED
    except Exception:
        return False


def safe_to_continue(assessment: Assessment) -> bool:
    """May this unit be **started again** unattended — by resuming *or* by repeating?

    ⚠️ **THIS IS NOT A SECOND `safe_to_repeat()` AND NOT A SECOND
    `crash._decide()`.** `safe_to_repeat()` answers a narrower question — *may this
    effect be allowed to happen twice* — and stays the only place its five
    conditions are ANDed; `_decide()` answers a wider one, *which of five ledger
    actions a human-facing row records*. This answers the one question a
    **scheduler** has: may I hand this unit to a worker again. It is composed from
    the other two rather than re-deriving either.

    ⚠️ `R_SAFE_TO_RESUME` IS A YES, AND THAT IS THE WHOLE REASON THIS EXISTS.
    `_assess_task()` returns it for every open task row (the checkpoint *is* the
    resume point, and a destructive step in flight is demoted to
    `R_REQUIRES_VERIFICATION` before ever reaching here), so a caller that asked
    `safe_to_repeat()` alone would be **structurally unable** to get a yes for a
    task — a retry ceiling that reads as bounded and is in fact dead. For a unit
    whose remaining work has not run, resuming and repeating are the same act.

    The two conditions that outrank the class are still asked, and asked *here*:
    a `D_NEVER` operation and a capability this process no longer holds both say no
    however the unit was classified.
    """
    try:
        if assessment.classification == R_SAFE_TO_RESUME:
            return not assessment.never_repeats and assessment.permitted
        return safe_to_repeat(assessment)
    except Exception:
        return False


def describe(assessment: Assessment) -> str:
    """One human line. Used by the CLI panel and the `reason` column."""
    try:
        bits = [assessment.classification.replace("_", " ")]
        if assessment.operation and assessment.operation != _safety.UNKNOWN:
            bits.append(f"({assessment.operation})")
        if assessment.verdict:
            bits.append(f"→ {assessment.verdict.replace('_', ' ')}")
        if assessment.reason:
            bits.append(f"— {assessment.reason}")
        return " ".join(bits)
    except Exception:
        return R_UNKNOWN
