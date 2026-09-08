# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/core/recovery/crash.py
─────────────────────────────
Task 25 — **the startup scan, the one recovery state machine, and the durable
record of every decision it made.**

Task 24 gave the app a memory that outlives the process: `execstate.sweep()` can
say *"these rows were in flight and their owner is gone"*. This module is what
happens next — the part Task 25 §4 insists must exist in one place:

    INTERRUPTED → RECOVERY_PENDING → VERIFYING → { RECOVERED | NEEDS_REVIEW |
                                                   RECOVERY_FAILED }

⚠️ **ONE STATE MACHINE, AND ITS DECISIONS ARE WRITTEN DOWN.** §25.4 says it in as
many words: *"do not hide recovery decisions inside random exception handlers."*
Every unit that enters `_advance()` leaves a row in `exec_recovery` carrying the
classification, the operation kind, the verdict, the decision and the reason —
including the units where the answer was *"do nothing"*. A recovery that acts
without a record is indistinguishable from a bug, and a recovery that declines to
act without a record is indistinguishable from a recovery that never ran.

⚠️ **THE SCAN IS BOUNDED IN BOTH DIRECTIONS AND REPORTS WHEN IT STOPPED SHORT.**
§25.1 requires that recovery *"must not block startup indefinitely"*. There are two
ceilings — `RECOVERY_SCAN_LIMIT` on units and `RECOVERY_SCAN_BUDGET_SEC` on
wall-clock — and hitting either sets `truncated: True` in the report and in the log
line rather than silently returning a short answer. A partial scan that looks
complete is worse than one that says it is partial: the untouched rows stay in the
ledger and `report()` still lists them, so the next launch (or `POST
/api/recovery/scan`) finishes the job.

⚠️ **NOTHING HERE EXECUTES ANYTHING, AND NOTHING HERE KILLS ANYTHING.** The
decisions are `A_MARK_COMPLETE`, `A_RESUME`, `A_RETRY`, `A_REVIEW` and `A_NONE`,
and all five are *bookkeeping*: they settle the ledger row, park a task where the
existing Task 3 recovery offer can find it, or file a review entry. `A_RETRY` does
not run a command — it records that a repeat is safe, and the normal machinery (the
model's next turn, the user's `[R]etry` keypress) is what repeats it. §25.5's *"never
blindly execute a command again merely because its previous process disappeared"*
and the phase's *"do not automatically execute privileged commands merely because
they were previously running"* are not policies checked at the end; they are true
because this module has no execution path at all. `terminate()` exists for §25.5's
*"terminate safely if required"* and is reachable only from an explicit human
action, never from `scan()`.

⚠️ **A LIVE OWNER MEANS THIS IS NOT A CRASH, AND THE SCAN LEAVES IT ALONE.**
`execstate.INSTANCE` is `f"{pid}-{hex}"`, so a swept row names the process that
wrote it. Before deciding anything, `_advance()` asks `owner_alive()` whether a
process with that pid still exists; if one does, the unit is skipped entirely and
counted as `live_owner`. This matters in dual mode specifically — two processes over
one `agent2.db`, where a genuinely-running silent command in the CLI half can
out-age the staleness cutoff and be swept by the web half. Being wrong in this
direction costs a delayed recovery, which `report()` still shows; being wrong in the
other direction means recovery acting on work that is happening right now.

⚠️ **pid LIVENESS IS "A PROCESS EXISTS", NEVER "OUR PROCESS EXISTS".** pids are
recycled, and there is no portable way to read a process's start time without a
third-party dependency. `pid_alive()` says so in its docstring and every caller
treats a True as *"cannot be sure it is gone"* — which routes a command with a live
pid to `NEEDS_REVIEW`, never to a kill and never to a repeat.

⚠️ **ATTEMPTS ARE BOUNDED AND THE SCAN IS IDEMPOTENT.** A unit whose recovery row
is already `RECOVERED` / `RECOVERY_FAILED` / `NEEDS_REVIEW` is skipped on the next
scan — the recovery row is the gate, not the ledger row, which stays `interrupted`
so the forensic record survives. A unit that keeps failing exhausts
`RECOVERY_MAX_ATTEMPTS` and lands in `NEEDS_REVIEW`. §25.10's *"no infinite retry
loops"* therefore holds structurally: there is no loop, only a counter that only
ever goes up.

⚠️ **RECOVERY RE-ASKS THE PERMISSION LAYER, EVERY TIME.** `classify.safe_to_repeat`
reads `Assessment.permitted`, which calls `permissions.process_allows` live. An
operator who sets `AGENT2_DENY_CAPS=fs.delete` after the crash gets a review entry
instead of a replayed delete — Task 26 §9, and the security requirement that
recovery *"must never bypass permissions"*.

Everything is total. `scan()` catches per-unit, and the whole scan is wrapped, so a
fault in the recovery of one command cannot stop the recovery of the next one or
prevent the app from starting. That is the reason this module carries the same
written BLE001/S110 allowance as `broker/budget.py` and `gitstate.py`: a crash in
the crash handler is the one failure with nothing left to catch it.
"""

from __future__ import annotations

import json
import re
import threading
import time

from agent2 import config as _cfg
from agent2.core import commands as _cmds
from agent2.core import execstate as _exec
from agent2.core import logging as alog
from agent2.core import procio as _procio
from agent2.core import tasks as _tasks
from agent2.core.recovery import classify as _cls
from agent2.core.recovery import safety as _safety

# ── The stored recovery states (Task 24 §7) ───────────────────────────────────
# ⚠️ NORMAL / ACTIVE / STALE ARE DERIVED AND DELIBERATELY NOT IN THIS LIST. They
# are properties of the execution row — its status against the staleness cutoff —
# and a unit with no recovery row at all is NORMAL by construction. Storing a
# derived word would put a second opinion about liveness in the database, where it
# goes stale the moment the row it describes is touched.

S_INTERRUPTED = "interrupted"
S_PENDING = "recovery_pending"
S_VERIFYING = "verifying"
S_RECOVERING = "recovering"
S_RECOVERED = "recovered"
S_FAILED = "recovery_failed"
S_REVIEW = "needs_review"

STATES = (S_INTERRUPTED, S_PENDING, S_VERIFYING, S_RECOVERING, S_RECOVERED,
          S_FAILED, S_REVIEW)

# The derived words, exported so the CLI panel and `/api/recovery` spell them the
# same way this module would.
S_NORMAL = "normal"
S_ACTIVE = "active"
S_STALE = "stale"
DERIVED_STATES = (S_NORMAL, S_ACTIVE, S_STALE)

# ⚠️ A unit whose recovery row is in one of these is NEVER re-processed by a later
# scan. This — not the ledger row — is what makes `scan()` idempotent, and it is why
# the ledger row is left `interrupted` for anything that ends in review: the
# forensic record of the crash survives, and the decision not to act survives with
# it.
RESOLVED = frozenset((S_RECOVERED, S_FAILED, S_REVIEW))

# ── The decisions (Task 25 §4) ────────────────────────────────────────────────

A_MARK_COMPLETE = "mark_complete"
A_RESUME = "resume"
A_RETRY = "retry"
A_REVIEW = "review"
A_NONE = "none"

DECISIONS = (A_MARK_COMPLETE, A_RESUME, A_RETRY, A_REVIEW, A_NONE)

# Which ledger status each decision settles the unit's own row with. `A_RESUME` and
# `A_REVIEW` are absent on purpose: a resumed unit is still live work and a unit
# awaiting a human has no verdict yet, so in both cases the row keeps saying
# `interrupted`, which is the truth.
_SETTLE_AS: dict[str, dict[str, str]] = {
    A_MARK_COMPLETE: {
        _cls.K_COMMAND: _cmds.CommandStatus.COMPLETED,
        _cls.K_TOOL_CALL: _tasks.STEP_COMPLETED,
        _cls.K_WORKFLOW: _tasks.STEP_COMPLETED,
    },
    A_RETRY: {
        _cls.K_COMMAND: _cmds.CommandStatus.FAILED,
        _cls.K_TOOL_CALL: _tasks.STEP_FAILED,
        _cls.K_WORKFLOW: _tasks.STEP_FAILED,
    },
}

# Kinds this module can act on without help, in scan order. `task` and `session`
# are handled by `_recover_workers()` because they belong to `core/tasks.py`.
_LEDGER_KINDS = ((_cls.K_COMMAND, "commands"),
                 (_cls.K_TOOL_CALL, "tool_calls"),
                 (_cls.K_WORKFLOW, "workflows"))

MAX_REASON_CHARS = 300
MAX_EVIDENCE_CHARS = 4_000

_lock = threading.Lock()
_running = 0                      # concurrent `_advance()` calls, bounded below
_last: dict = {}                  # the last scan's report, for `stats()`
_counters: dict = {"scans": 0, "attempts": 0, "recovered": 0, "retried": 0,
                   "resumed": 0, "review": 0, "failed": 0, "skipped": 0,
                   "live_owner": 0, "workers": 0, "errors": 0}


# ── Small helpers ─────────────────────────────────────────────────────────────

def _now() -> str:
    return _exec._now()


def _text(value, limit: int = MAX_REASON_CHARS) -> str:
    try:
        out = str(value or "").replace("\n", " ").strip()
    except Exception:
        return ""
    return out[:limit]


def _json(value) -> str:
    try:
        raw = json.dumps(value, default=str, sort_keys=True)
    except Exception:
        return "{}"
    return raw if len(raw) <= MAX_EVIDENCE_CHARS else "{}"


def _loads(raw) -> dict:
    try:
        out = json.loads(str(raw or "") or "{}")
        return out if isinstance(out, dict) else {}
    except Exception:
        return {}


def _key(kind: str, ref_id: str) -> str:
    return f"{kind}:{ref_id}"


def enabled() -> bool:
    """Is crash recovery switched on at all? (`AGENT2_RECOVERY`)"""
    try:
        return bool(_cfg.RECOVERY_ENABLED)
    except Exception:
        return True


# ── Process liveness (Task 25 §5) ─────────────────────────────────────────────

_INSTANCE_PID = re.compile(r"^(\d+)-")

# ⚠️ AN ALIAS, NOT A COPY. The platform question — "does a process with this
# number exist" — is `core/procio.py`'s, because that module already owns the tree
# kill and the two have to agree about what "alive" means on Windows. Rebinding the
# name here is the same discipline `agent._MEM_CACHE` uses for the broker's caches:
# one object, two spellings, no drift. See `procio.pid_alive` for why `None` is a
# third answer and why a `True` is never "our process".
pid_alive = _procio.pid_alive


def owner_alive(row) -> bool | None:
    """Is the Agent2 process that wrote this row still running?

    A second opinion beyond the staleness cutoff, and the one that saves dual mode:
    `execstate.INSTANCE` is `f"{pid}-{hex}"`, so a swept row names its author's pid.
    A silent long-running command in the CLI half can out-age `EXEC_STALE_SEC` and be
    swept by the web half; if the CLI process is still there, this returns True and
    the scan leaves the unit alone.

    `None` when the instance string carries no pid (a legacy row, or a future
    instance format) — which the caller must treat as "no information", not as
    "dead".
    """
    try:
        text = str((row.get("instance") if hasattr(row, "get") else "") or "")
    except Exception:
        return None
    if text == _exec.INSTANCE:
        return True                     # our own row; definitionally alive
    found = _INSTANCE_PID.match(text)
    if not found:
        return None
    return pid_alive(found.group(1))


# ── The durable recovery record ───────────────────────────────────────────────

def _row(kind: str, ref_id: str) -> dict:
    try:
        from agent2.database import qone
        return qone("SELECT * FROM exec_recovery WHERE id=?",
                    (_key(kind, ref_id),)) or {}
    except Exception as exc:
        _oops("row", exc)
        return {}


def _write(kind: str, ref_id: str, **fields) -> None:
    """Upsert one recovery row. The ONLY writer of `exec_recovery`.

    An UPSERT rather than an INSERT-then-UPDATE because a scan is idempotent by
    design: the same unit may be seen by a later scan, by an operator's manual
    trigger and by the web surface, and all three have to converge on one row.
    """
    ident = _key(kind, ref_id)
    cols = {
        "kind": kind, "ref_id": str(ref_id or ""),
        "updated_at": _now(),
    }
    cols.update({k: v for k, v in fields.items() if v is not None})
    names = ", ".join(cols)
    marks = ", ".join("?" * (len(cols) + 1))
    sets = ", ".join(f"{k}=excluded.{k}" for k in cols)
    try:
        from agent2.database import exe
        exe(f"INSERT INTO exec_recovery (id, {names}) VALUES ({marks})"
            f" ON CONFLICT(id) DO UPDATE SET {sets}",
            (ident, *cols.values()))
    except Exception as exc:
        _oops("write", exc)


def _oops(op: str, exc: Exception) -> None:
    """One place the module's own failures are counted and logged."""
    with _lock:
        _counters["errors"] += 1
    try:
        alog.event("recovery.error", level=30, op=op, error=str(exc)[:200])
    except Exception:  # logging is best-effort by contract; see this file's allowance
        pass


def state_of(kind: str, ref_id: str) -> str:
    """The stored recovery state of one unit, or `S_NORMAL` if it has no row.

    ⚠️ "No row" IS the answer for healthy work — a normal install writes nothing
    here, so the absence of a row is not missing data.
    """
    row = _row(kind, ref_id)
    return str(row.get("status") or "") or S_NORMAL


# ── The state machine (Task 25 §4) ────────────────────────────────────────────

def _decide(assessment: _cls.Assessment) -> tuple[str, str]:
    """`(decision, reason)` for one assessed-and-possibly-verified unit.

    ⚠️ **THE WHOLE DECISION IS HERE, IN ONE FUNCTION, IN READING ORDER.** §25.4
    forbids scattering it, and the reason is that every one of these branches looks
    reasonable on its own: "verified applied ⇒ mark complete" is obviously right,
    and so is "safe to retry ⇒ retry" — but the *precedence* between them is the
    part that can be wrong, and precedence is only visible when the branches sit
    together.
    """
    cls = assessment.classification
    verdict = assessment.verdict

    if cls == _cls.R_NON_RECOVERABLE:
        return A_REVIEW, assessment.reason or "not recoverable unattended"

    if cls == _cls.R_UNKNOWN:
        return A_REVIEW, assessment.reason or "could not be classified"

    if cls == _cls.R_SAFE_TO_RESUME:
        return A_RESUME, assessment.reason or "resuming from the checkpoint"

    # ⚠️ `safe_to_repeat()` is consulted BEFORE the verdict is read, because it is
    # the one place the five conditions are ANDed — capability included. Reading
    # the verdict first and asking about permission afterwards is how a
    # `V_NOT_APPLIED` delete would get retried by a process that has since had
    # `fs.delete` taken away.
    if _cls.safe_to_repeat(assessment):
        return A_RETRY, assessment.reason or "verified as not applied"

    if cls == _cls.R_SAFE_TO_RETRY:
        # It classified as repeatable but `safe_to_repeat()` said no. The only ways
        # that happens are a `D_NEVER` kind or a capability this process no longer
        # holds, and both are a human's call.
        if not assessment.permitted:
            return A_REVIEW, (f"a repeat needs the "
                              f"'{_safety.capability_for(assessment.operation)}' "
                              f"capability, which this process does not hold")
        return A_REVIEW, assessment.reason or "a repeat is not permitted unattended"

    if cls == _cls.R_REQUIRES_VERIFICATION:
        if not verdict:
            return A_REVIEW, "verification did not run"
        if verdict == _verdicts().LIKELY:
            if assessment.never_repeats:
                return A_REVIEW, ("the operation appears to have landed and may not "
                                  "be repeated unattended")
            return A_MARK_COMPLETE, assessment.reason or "the work had already landed"
        # V_UNCERTAIN and V_UNVERIFIABLE both mean the final state is unknown, and
        # Task 26's first line is that such an operation is never repeated.
        return A_REVIEW, assessment.reason or f"final state {verdict}"

    return A_REVIEW, "no rule matched"


class _Verdicts:
    """The verdict vocabulary, read from the package that declares it.

    Resolved lazily and by attribute so this module never becomes a second spelling
    of `V_LIKELY_APPLIED` — the same reason `classify.py` imports them rather than
    restating them, and lazily because `recovery/__init__.py` may not import this
    module at import time (the dependency runs one way only).
    """

    __slots__ = ()

    @property
    def LIKELY(self) -> str:            # a constant's spelling, deliberately
        from agent2.core import recovery as _pkg
        return _pkg.V_LIKELY_APPLIED

    @property
    def NOT_APPLIED(self) -> str:       # a constant's spelling, deliberately
        from agent2.core import recovery as _pkg
        return _pkg.V_NOT_APPLIED

    @property
    def UNCERTAIN(self) -> str:         # a constant's spelling, deliberately
        from agent2.core import recovery as _pkg
        return _pkg.V_UNCERTAIN

    @property
    def UNVERIFIABLE(self) -> str:      # a constant's spelling, deliberately
        from agent2.core import recovery as _pkg
        return _pkg.V_UNVERIFIABLE


_VERDICTS = _Verdicts()


def _verdicts() -> _Verdicts:
    return _VERDICTS


def _slot_taken() -> bool:
    """Is `RECOVERY_MAX_PARALLEL` already fully committed?

    A ceiling rather than a queue, on purpose: this can be called on the startup
    path, and blocking there is exactly the *"must not block startup indefinitely"*
    that §25.1 forbids. A caller that finds no slot is told so and the unit stays in
    the ledger for the next scan.
    """
    global _running
    try:
        cap = max(1, int(_cfg.RECOVERY_MAX_PARALLEL))
    except Exception:
        cap = 2
    with _lock:
        if _running >= cap:
            return True
        _running += 1
        return False


def _slot_free() -> None:
    global _running
    with _lock:
        _running = max(0, _running - 1)


def _advance(kind: str, row, *, force: bool = False) -> dict:
    """Run ONE interrupted unit through the whole machine. Never raises.

    Returns `{"kind", "ref_id", "state", "decision", "classification", "verdict",
    "reason", "attempt", "skipped"}` — the shape `scan()` counts and `report()`
    renders.
    """
    ref_id = str((row.get("id") if hasattr(row, "get") else "") or "")
    out = {"kind": kind, "ref_id": ref_id, "state": S_INTERRUPTED,
           "decision": A_NONE, "classification": "", "verdict": "",
           "reason": "", "attempt": 0, "skipped": ""}
    if not ref_id:
        out["skipped"] = "no id"
        return out

    prior = _row(kind, ref_id)
    status = str(prior.get("status") or "")
    if status in RESOLVED and not force:
        out.update(state=status, skipped="already resolved",
                   decision=str(prior.get("decision") or A_NONE),
                   reason=_text(prior.get("reason")))
        return out

    # ⚠️ A live owner is not a crash. Checked before ANY state is written, so a
    # dual-mode false positive leaves no recovery row behind to explain away.
    if owner_alive(row) is True and not force:
        out.update(skipped="owner process is still running", state=S_NORMAL)
        return out

    attempt = int(prior.get("attempts") or 0) + 1
    try:
        cap = max(1, int(_cfg.RECOVERY_MAX_ATTEMPTS))
    except Exception:
        cap = 3
    out["attempt"] = attempt

    if _slot_taken():
        out["skipped"] = "recovery is at its parallelism ceiling"
        return out

    try:
        session_id = str((row.get("session_id") if hasattr(row, "get") else "") or "")
        task_id = str((row.get("task_id") if hasattr(row, "get") else "") or "")
        interrupted_at = str(
            (row.get("updated_at") if hasattr(row, "get") else "") or "")

        # ── INTERRUPTED ────────────────────────────────────────────────────────
        _write(kind, ref_id, status=S_INTERRUPTED,
               project=str((row.get("project") if hasattr(row, "get") else "") or ""),
               session_id=session_id, task_id=task_id,
               instance=str((row.get("instance") if hasattr(row, "get") else "") or ""),
               recovered_by=_exec.INSTANCE, attempts=attempt,
               interrupted_at=interrupted_at, started_at=_now())

        assessment = _cls.assess(kind, row)
        alog.execution_interrupted(
            kind, ref_id, session_id=assessment.session_id or session_id,
            task_id=assessment.task_id or task_id, operation=assessment.operation,
            interrupted_at=interrupted_at)

        if attempt > cap:
            return _finish(kind, ref_id, assessment, A_REVIEW,
                           f"recovery attempted {attempt - 1} times already", out,
                           attempt=attempt)

        # ── RECOVERY_PENDING ───────────────────────────────────────────────────
        _write(kind, ref_id, status=S_PENDING,
               classification=assessment.classification,
               operation=assessment.operation,
               disposition=assessment.disposition,
               reason=_text(assessment.reason),
               evidence=_json(assessment.evidence))
        alog.recovery_started(kind, ref_id,
                              classification=assessment.classification,
                              attempt=attempt, session_id=assessment.session_id,
                              task_id=assessment.task_id)
        out["classification"] = assessment.classification

        # §25.5 — a command whose process may still be running is never touched.
        if kind == _cls.K_COMMAND:
            live = pid_alive(row.get("process_id") if hasattr(row, "get") else None)
            if live is True:
                return _finish(kind, ref_id, assessment, A_REVIEW,
                               "the command's process is still alive", out,
                               attempt=attempt)

        # ── VERIFYING ──────────────────────────────────────────────────────────
        if assessment.needs_verification:
            _write(kind, ref_id, status=S_VERIFYING)
            assessment = _cls.verify(assessment, row)
            out["verdict"] = assessment.verdict
            _write(kind, ref_id, verdict=assessment.verdict,
                   reason=_text(assessment.reason),
                   evidence=_json(assessment.evidence))

        decision, reason = _decide(assessment)
        if assessment.needs_verification:
            alog.recovery_verified(kind, ref_id, verdict=assessment.verdict,
                                   decision=decision,
                                   session_id=assessment.session_id)
        return _finish(kind, ref_id, assessment, decision, reason, out,
                       attempt=attempt, row=row)
    except Exception as exc:  # a fault in one unit may not end the scan; see docstring
        _oops("advance", exc)
        _write(kind, ref_id, status=S_FAILED, decision=A_NONE,
               reason=_text(f"recovery failed: {exc}"), completed_at=_now())
        alog.recovery_failed(kind, ref_id, error=str(exc)[:200], attempt=attempt)
        with _lock:
            _counters["failed"] += 1
        out.update(state=S_FAILED, decision=A_NONE,
                   reason=_text(f"recovery failed: {exc}"))
        return out
    finally:
        _slot_free()


def _finish(kind: str, ref_id: str, assessment: _cls.Assessment, decision: str,
            reason: str, out: dict, *, attempt: int = 1, row=None) -> dict:
    """Apply one decision, record it, log it. The ONLY place a decision is acted on."""
    state = S_REVIEW if decision == A_REVIEW else S_RECOVERED
    reason = _text(reason)

    if decision in (A_MARK_COMPLETE, A_RETRY):
        _write(kind, ref_id, status=S_RECOVERING, decision=decision, reason=reason)
        target = (_SETTLE_AS.get(decision) or {}).get(kind, "")
        table = _cls.TABLE_FOR.get(kind, "")
        if target and table:
            _exec.resolve_interrupted(table, ref_id, target)

    _write(kind, ref_id, status=state, decision=decision,
           classification=assessment.classification,
           operation=assessment.operation, disposition=assessment.disposition,
           verdict=assessment.verdict, reason=reason,
           evidence=_json(assessment.evidence), attempts=attempt,
           completed_at=_now())

    with _lock:
        _counters["attempts"] += 1
        if decision == A_REVIEW:
            _counters["review"] += 1
        else:
            _counters["recovered"] += 1
            if decision == A_RETRY:
                _counters["retried"] += 1
            elif decision == A_RESUME:
                _counters["resumed"] += 1

    if decision == A_REVIEW:
        alog.recovery_needs_review(kind, ref_id, reason=reason,
                                   operation=assessment.operation,
                                   verdict=assessment.verdict,
                                   session_id=assessment.session_id,
                                   task_id=assessment.task_id)
    else:
        if decision == A_RETRY:
            alog.recovery_retried(kind, ref_id, attempt=attempt,
                                  operation=assessment.operation,
                                  session_id=assessment.session_id)
        elif decision == A_RESUME:
            at = str((assessment.evidence or {}).get("step_index") or "")
            alog.recovery_resumed(kind, ref_id, session_id=assessment.session_id,
                                  task_id=assessment.task_id, at=at)
        alog.recovery_completed(kind, ref_id, decision=decision,
                                session_id=assessment.session_id,
                                task_id=assessment.task_id)

    out.update(state=state, decision=decision, reason=reason,
               classification=assessment.classification,
               verdict=assessment.verdict, attempt=attempt)
    return out


# ── Worker recovery (Task 25 §6) ──────────────────────────────────────────────

def recover_workers(*, cwd: str = "", older_than: float | None = None,
                    limit: int = 50) -> list[dict]:
    """Tasks a worker is still holding that no live worker is working on.

    §25.6 in one line: *"do not leave tasks permanently stuck in RUNNING."* The
    action is `tasks.interrupt()`, which already exists and already does the right
    thing — it marks in-flight sub-steps interrupted, saves the `CP_STOPPED`
    checkpoint and parks the task as PAUSED. From there the Task 3 recovery
    offer finds it through `unfinished_sessions()` and puts it in front of the user.

    ⚠️ **`interrupt()` IS CALLED ONCE PER SESSION, NOT ONCE PER TASK.** It operates
    on every held task in a session, so calling it per task would write the same
    checkpoint N times and log N identical events for one crash.

    ⚠️ **THE SEED IS `tasks.HELD` — RUNNING *AND* QUEUED — AND PAUSED IS NEVER
    SWEPT.** `stale_running()` owns that set and this module may not re-derive it;
    QUEUED is in it because `dag.store.claim()` writes the status *as* the lock, so a
    process that dies between the claim and the RUNNING mark leaves a row no
    heartbeat ever touches and no scheduler can re-take — see `stale_running()`'s
    docstring for the four paths that could not see it. PAUSED stays out: sweeping it
    would hand recovery every chat the user paused on purpose, which is exactly the
    repurposing of `/pause` this phase is forbidden to do.
    """
    out: list[dict] = []
    if not enabled():
        return out
    try:
        stale = float(older_than if older_than is not None else _cfg.EXEC_STALE_SEC)
    except Exception:
        stale = 90.0
    try:
        rows = _tasks.stale_running(cwd=cwd, older_than=stale, limit=limit)
    except Exception as exc:
        _oops("workers", exc)
        return out

    seen: set = set()
    for task in rows:
        ref_id = str(getattr(task, "id", "") or "")
        session_id = str(getattr(task, "session_id", "") or "")
        assessment = _cls.assess(_cls.K_TASK, task)
        try:
            alog.execution_interrupted(
                _cls.K_TASK, ref_id, session_id=session_id, task_id=ref_id,
                operation=assessment.operation,
                interrupted_at=str(getattr(task, "updated_at", "") or ""))
        except Exception:  # logging is best-effort by contract; see this file's allowance
            pass
        _write(_cls.K_TASK, ref_id, status=S_PENDING, session_id=session_id,
               task_id=ref_id, recovered_by=_exec.INSTANCE,
               classification=assessment.classification,
               operation=assessment.operation, disposition=assessment.disposition,
               reason=_text(assessment.reason), evidence=_json(assessment.evidence),
               interrupted_at=str(getattr(task, "updated_at", "") or ""),
               started_at=_now(), attempts=1)
        if assessment.needs_verification:
            _write(_cls.K_TASK, ref_id, status=S_VERIFYING)
            assessment = _cls.verify(assessment, task)
            _write(_cls.K_TASK, ref_id, verdict=assessment.verdict,
                   reason=_text(assessment.reason))
        decision, reason = _decide(assessment)
        if session_id and session_id not in seen:
            seen.add(session_id)
            try:
                _tasks.interrupt(session_id, "the worker did not survive the restart")
            except Exception as exc:
                _oops("interrupt", exc)
        entry = _finish(_cls.K_TASK, ref_id, assessment, decision, reason,
                        {"kind": _cls.K_TASK, "ref_id": ref_id, "state": "",
                         "decision": "", "classification": "", "verdict": "",
                         "reason": "", "attempt": 1, "skipped": ""}, attempt=1)
        out.append(entry)
        with _lock:
            _counters["workers"] += 1
    return out


# ── The startup scan (Task 25 §1) ─────────────────────────────────────────────

def _task_scope(project) -> str:
    """Translate the ledger's project sentinel into the task model's.

    ⚠️ **THE TWO READERS SPELL "EVERY PROJECT" DIFFERENTLY, AND THAT IS WHY THIS
    FUNCTION EXISTS RATHER THAN A `str(project or "")` AT THE CALL SITE.**
    `execstate.interrupted()` takes `ANY_PROJECT` — the literal `"*"` — to lift its
    filter, while `tasks.stale_running()` lifts its own on an EMPTY `cwd`, because
    `""` there means "do not join task_sessions at all". Passing `"*"` straight
    through therefore does not widen the task query, it narrows it to a project
    literally named `*`: worker recovery silently finds nothing, the scan reports
    `workers: 0`, and every stuck task stays held forever — the one outcome §25.6
    forbids, produced by a sentinel mismatch that raises nothing.

    `None` (the default, meaning "this project") passes through as `""` too, which
    is correct only because `stale_running("")` scopes by nothing and the scan's own
    default is the current project either way — the ledger half already applied the
    project filter, and a task has no `project` column to filter on.
    """
    text = str(project or "")
    return "" if text == _exec.ANY_PROJECT else text


def scan(*, project=None, stale_after: float | None = None,
         limit: int | None = None, budget: float | None = None,
         workers: bool = True, force: bool = False) -> dict:
    """Find interrupted work, decide what to do with each unit, report.

    The whole of §25.1's ordering, in one call: sweep the ledger for rows whose
    owner is gone, run each through the state machine, then look for tasks a dead
    worker is still holding. Bounded by `limit` units and `budget` seconds — both
    default from `config` — and the report says `truncated` when either ceiling
    stopped it early.
    ⚠️ That report is why the reader is asked for `cap + 1` rows: see the comment at
    the call, and `test_the_scan_is_bounded`.

    ⚠️ **TOTAL, AND CALLED ON THE STARTUP PATH.** Every failure mode returns a
    report; nothing propagates. A recovery scan that could raise would make a
    crashed *previous* run into a launch failure for the current one, which is the
    one outcome worse than not recovering.
    """
    started = time.monotonic()
    report = {
        "enabled": enabled(), "instance": _exec.INSTANCE, "found": 0,
        "processed": 0, "recovered": 0, "retried": 0, "resumed": 0, "review": 0,
        "failed": 0, "skipped": 0, "live_owner": 0, "workers": 0,
        "truncated": False, "elapsed_ms": 0, "swept": {}, "units": [],
        "at": _now(),
    }
    if not report["enabled"]:
        report["reason"] = "recovery is disabled (AGENT2_RECOVERY=0)"
        return report

    try:
        cap = max(1, int(limit if limit is not None else _cfg.RECOVERY_SCAN_LIMIT))
    except Exception:
        cap = 200
    try:
        deadline = started + max(
            0.5, float(budget if budget is not None
                       else _cfg.RECOVERY_SCAN_BUDGET_SEC))
    except Exception:
        deadline = started + 5.0
    try:
        stale = float(stale_after if stale_after is not None else _cfg.EXEC_STALE_SEC)
    except Exception:
        stale = 90.0

    try:
        alog.recovery_scan_started(instance=_exec.INSTANCE,
                                  project=str(project or ""), stale_sec=stale)
    except Exception:  # logging is best-effort by contract; see this file's allowance
        pass

    try:
        report["swept"] = _exec.sweep(project=project, stale_after=stale)
        # ⚠️ ONE ROW MORE THAN THE CEILING, ON PURPOSE. The loop below reports
        # `truncated` by noticing it has reached `cap` with a row still in hand, so
        # asking the reader for exactly `cap` makes that condition unreachable: a
        # scan that stopped early would report `truncated: False`, which an operator
        # reads as "recovery looked at everything". A silent cap on the one path
        # that decides whether interrupted work is examined at all is the failure
        # this whole module exists to prevent — so the probe row is what turns the
        # ceiling into a *reported* ceiling. With `truncated` set, `found` is a
        # floor, not a total.
        found = _exec.interrupted(project=project, stale_after=stale, limit=cap + 1)
    except Exception as exc:
        _oops("scan", exc)
        report["error"] = str(exc)[:200]
        report["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        return report

    report["found"] = int(found.get("total") or 0)
    report["cutoff"] = str(found.get("cutoff") or "")

    for kind, bucket in _LEDGER_KINDS:
        for row in found.get(bucket) or []:
            if len(report["units"]) >= cap or time.monotonic() >= deadline:
                report["truncated"] = True
                break
            entry = _advance(kind, row, force=force)
            report["units"].append(entry)
            if entry.get("skipped"):
                report["skipped"] += 1
                if "owner" in entry["skipped"]:
                    report["live_owner"] += 1
                continue
            report["processed"] += 1
            decision = entry.get("decision")
            state = entry.get("state")
            if state == S_FAILED:
                report["failed"] += 1
            elif decision == A_REVIEW:
                report["review"] += 1
            else:
                report["recovered"] += 1
                if decision == A_RETRY:
                    report["retried"] += 1
                elif decision == A_RESUME:
                    report["resumed"] += 1
        if report["truncated"]:
            break

    if workers and not report["truncated"]:
        try:
            for entry in recover_workers(
                    cwd=_task_scope(project),
                    older_than=stale,
                    limit=max(1, cap - len(report["units"]))):
                report["units"].append(entry)
                report["workers"] += 1
                report["processed"] += 1
                if entry.get("decision") == A_REVIEW:
                    report["review"] += 1
                else:
                    report["recovered"] += 1
                    if entry.get("decision") == A_RESUME:
                        report["resumed"] += 1
        except Exception as exc:
            _oops("scan.workers", exc)

    report["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    with _lock:
        _counters["scans"] += 1
        _counters["skipped"] += report["skipped"]
        _counters["live_owner"] += report["live_owner"]
        _last.clear()
        _last.update({k: v for k, v in report.items() if k != "units"})
        _last["units"] = len(report["units"])
    try:
        alog.recovery_scan_finished(
            found=report["found"], recovered=report["recovered"],
            retried=report["retried"], review=report["review"],
            failed=report["failed"], elapsed_ms=report["elapsed_ms"],
            truncated=report["truncated"])
    except Exception:  # logging is best-effort by contract; see this file's allowance
        pass
    return report


def scan_on_start(*, project=None, background: bool | None = None) -> dict:
    """The launch-path entry point every surface calls. Never raises, never blocks
    for long.

    ⚠️ **THE DECISION TO GO TO A THREAD IS MADE HERE, NOT AT THE THREE CALL SITES.**
    `agent2cli.py`, `agent2web.py` and `agent2dual.py` each call this one function,
    so "how long may recovery delay a launch" has one answer instead of three that
    drift. `background=None` means "decide from the ledger": a scan with nothing to
    do costs one indexed query and is done inline, and only a real backlog is handed
    to a daemon thread — §25.1's *"controlled background recovery if the amount of
    interrupted work is large"*.
    """
    if not enabled():
        return {"enabled": False, "started": False}
    try:
        if not _cfg.RECOVERY_ON_START:
            return {"enabled": True, "started": False,
                    "reason": "AGENT2_RECOVERY_ON_START=0"}
    except Exception:
        pass

    if background is None:
        try:
            counts = _exec.stats() or {}
            pending = sum(int((counts.get(t) or {}).get("interrupted") or 0)
                          for t in _exec.TABLES)
            background = pending > 25
        except Exception:
            background = False

    if not background:
        return scan(project=project)

    def _run() -> None:
        try:
            scan(project=project)
        except Exception as exc:  # a background scan may not kill the process
            _oops("scan_on_start", exc)

    thread = threading.Thread(target=_run, name="agent2-recovery", daemon=True)
    thread.start()
    return {"enabled": True, "started": True, "background": True}


# ── Explicit, human-authorized actions ────────────────────────────────────────

def terminate(kind: str, ref_id: str) -> dict:
    """Kill the orphaned process tree behind an interrupted command — §25.5's
    *"terminate safely if required"*.

    ⚠️ **NOT REACHABLE FROM `scan()`, AND THAT IS THE POINT.** A live pid means we
    cannot prove the process is ours (pids are recycled), so killing one during an
    automatic scan risks killing something a user started. This is the explicit path
    an operator takes after reading the review entry — the CLI panel's action and
    `POST /api/recovery/<id>` — and it goes through `procio.terminate_pid()` rather
    than growing a second killer.

    ⚠️ **THE `exec` CAPABILITY IS CHECKED HERE, LIVE.** Ending a process tree is a
    privileged act whether or not the process that started it was allowed to; an
    operator who has taken `exec` away from this process does not get it back through
    the recovery surface.
    """
    if kind != _cls.K_COMMAND:
        return {"ok": False, "error": "only a command has a process tree"}
    if not _safety.permitted(_safety.SHELL):
        return {"ok": False, "error": "the 'exec' capability is not held"}
    row = _exec.command(ref_id) or {}
    pid = row.get("process_id")
    if not pid:
        return {"ok": False, "error": "no process was recorded"}
    if pid_alive(pid) is not True:
        return {"ok": False, "error": "no live process with that id"}
    try:
        gone = bool(_procio.terminate_pid(pid))
    except Exception as exc:
        _oops("terminate", exc)
        return {"ok": False, "error": str(exc)[:200]}
    if not gone:
        return {"ok": False, "pid": int(pid),
                "error": "the process did not exit"}
    _write(kind, ref_id, status=S_RECOVERED, decision=A_MARK_COMPLETE,
           reason="the orphaned process tree was terminated by an operator",
           completed_at=_now())
    _exec.resolve_interrupted("exec_commands", ref_id,
                              _cmds.CommandStatus.KILLED)
    return {"ok": True, "pid": int(pid)}


def acknowledge(kind: str, ref_id: str, note: str = "") -> dict:
    """A human looked at a review entry and is done with it.

    Moves `NEEDS_REVIEW` → `RECOVERED` with the decision `A_NONE`: reviewed, nothing
    to do. It does NOT settle the unit's ledger row, because a human deciding not to
    act does not change what the crashed process did or did not do.
    """
    row = _row(kind, ref_id)
    if not row:
        return {"ok": False, "error": "no recovery record"}
    _write(kind, ref_id, status=S_RECOVERED, decision=A_NONE,
           reason=_text(note or "acknowledged by an operator"),
           completed_at=_now())
    try:
        alog.recovery_completed(kind, ref_id, decision=A_NONE,
                                session_id=str(row.get("session_id") or ""),
                                task_id=str(row.get("task_id") or ""))
    except Exception:  # logging is best-effort by contract; see this file's allowance
        pass
    return {"ok": True, "state": S_RECOVERED}


def retry(kind: str, ref_id: str) -> dict:
    """An operator authorizes a repeat of a unit that recovery would not repeat.

    ⚠️ **THE CAPABILITY IS STILL CHECKED.** An explicit human decision overrules the
    *verdict* — the thing recovery could not establish — and never the *permission*
    gate, which describes what this process may do at all. Otherwise "retry" would
    be a way to launder `AGENT2_DENY_CAPS` away, and the security requirement that
    recovery never bypass authorization would hold only until someone clicked a
    button.
    """
    row = _row(kind, ref_id)
    if not row:
        return {"ok": False, "error": "no recovery record"}
    operation = str(row.get("operation") or _safety.UNKNOWN)
    if not _safety.permitted(operation):
        return {"ok": False,
                "error": (f"the '{_safety.capability_for(operation)}' capability is "
                          f"not held by this process")}
    table = _cls.TABLE_FOR.get(kind, "")
    if table:
        _exec.resolve_interrupted(
            table, ref_id, (_SETTLE_AS[A_RETRY]).get(kind, _tasks.STEP_FAILED))
    _write(kind, ref_id, status=S_RECOVERED, decision=A_RETRY,
           reason="a repeat was authorized by an operator", completed_at=_now())
    try:
        alog.recovery_retried(kind, ref_id,
                              attempt=int(row.get("attempts") or 1),
                              operation=operation,
                              session_id=str(row.get("session_id") or ""))
    except Exception:  # logging is best-effort by contract; see this file's allowance
        pass
    with _lock:
        _counters["retried"] += 1
    return {"ok": True, "state": S_RECOVERED, "decision": A_RETRY}


# ── Reporting (Task 25 §9, and the health section) ────────────────────────────

def rows(*, project=None, status: str = "", limit: int = 50) -> list[dict]:
    """Recovery records, newest first. Read-only."""
    where: list[str] = []
    params: list = []
    if project not in (None, "", _exec.ANY_PROJECT):
        where.append("project=?")
        params.append(str(project))
    if status:
        where.append("status=?")
        params.append(str(status))
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    try:
        num = max(1, min(500, int(limit)))
    except Exception:
        num = 50
    params.append(num)
    try:
        from agent2.database import qall
        out = qall(f"SELECT * FROM exec_recovery{clause}"
                   f" ORDER BY updated_at DESC, rowid DESC LIMIT ?", tuple(params))
    except Exception as exc:
        _oops("rows", exc)
        return []
    return [_payload(r) for r in out]


def _payload(row) -> dict:
    """One recovery row as a surface-safe payload.

    ⚠️ **NO COMMAND LINE, NO ARGUMENTS, NO FILE CONTENT — EVER.** `evidence` holds
    digests, counters and step indices because that is all `execstate` ever recorded;
    the unit's own `command` column is deliberately not joined in. The security
    requirement is explicit — *"do not expose recovery data containing secrets"* —
    and `run_command` argv routinely carries a token.
    """
    return {
        "id": str(row.get("id") or ""),
        "kind": str(row.get("kind") or ""),
        "ref_id": str(row.get("ref_id") or ""),
        "state": str(row.get("status") or ""),
        "classification": str(row.get("classification") or ""),
        "operation": str(row.get("operation") or ""),
        "disposition": str(row.get("disposition") or ""),
        "decision": str(row.get("decision") or ""),
        "verdict": str(row.get("verdict") or ""),
        "reason": str(row.get("reason") or ""),
        "evidence": _loads(row.get("evidence")),
        "attempts": int(row.get("attempts") or 0),
        "session_id": str(row.get("session_id") or ""),
        "task_id": str(row.get("task_id") or ""),
        "interrupted_at": str(row.get("interrupted_at") or ""),
        "updated_at": str(row.get("updated_at") or ""),
    }


def review_queue(*, project=None, limit: int = 20) -> list[dict]:
    """Everything waiting for a human. What the CLI panel prints."""
    return rows(project=project, status=S_REVIEW, limit=limit)


def report(*, project=None, limit: int = 20) -> dict:
    """The whole recovery picture for one project — CLI panel and `/api/recovery`."""
    open_rows = rows(project=project, limit=limit)
    review = [r for r in open_rows if r["state"] == S_REVIEW]
    return {
        "enabled": enabled(),
        "instance": _exec.INSTANCE,
        "last_scan": dict(_last),
        "counters": counters(),
        "needs_review": len(review),
        "review": review,
        "recent": open_rows,
    }


def counters() -> dict:
    """Process-lifetime counters. Copy, so a caller cannot mutate them."""
    with _lock:
        return dict(_counters)


def stats() -> dict:
    """The `recovery` section of `GET /api/health`.

    ⚠️ **NO RECOVERY *STATE* IS A HEALTH PROBLEM — only a broken scan is.** A row
    sitting in `NEEDS_REVIEW` is recovery working exactly as designed, and it can sit
    there for weeks; reporting it as a problem would pin `/api/health` at 503 until
    someone tidied a queue, and an endpoint that cries wolf gets ignored. The same
    call the MCP section makes when it refuses to treat a deliberately-disabled
    server as failing. Counters are reported so an operator can *see* the queue; only
    `errors` — this module failing at its own job — raises a problem.
    """
    by_state: dict = {}
    stale = 0
    try:
        from agent2.database import qall
        for row in qall("SELECT status, COUNT(*) AS n FROM exec_recovery"
                        " GROUP BY status"):
            by_state[str(row.get("status") or "")] = int(row.get("n") or 0)
    except Exception as exc:
        _oops("stats", exc)
    try:
        ledger = _exec.stats() or {}
        stale = sum(int((ledger.get(t) or {}).get("interrupted") or 0)
                    for t in _exec.TABLES)
    except Exception as exc:
        _oops("stats.ledger", exc)

    counts = counters()
    out = {
        "enabled": enabled(),
        "on_start": bool(getattr(_cfg, "RECOVERY_ON_START", True)),
        "max_attempts": int(getattr(_cfg, "RECOVERY_MAX_ATTEMPTS", 3)),
        "scan_limit": int(getattr(_cfg, "RECOVERY_SCAN_LIMIT", 200)),
        "max_parallel": int(getattr(_cfg, "RECOVERY_MAX_PARALLEL", 2)),
        "by_state": by_state,
        "scans": counts["scans"],
        "attempts": counts["attempts"],
        "successes": counts["recovered"],
        "failures": counts["failed"],
        "tasks_recovered": counts["recovered"],
        "tasks_retried": counts["retried"],
        "tasks_resumed": counts["resumed"],
        "needs_review": by_state.get(S_REVIEW, 0),
        "stale_executions": stale,
        "worker_failures": counts["workers"],
        "errors": counts["errors"],
        "last_scan_ms": int((_last or {}).get("elapsed_ms") or 0),
        "last_scan_at": str((_last or {}).get("at") or ""),
        "truncated": bool((_last or {}).get("truncated")),
        "problems": [],
    }
    if counts["errors"]:
        out["problems"].append(f"recovery hit {counts['errors']} internal error(s)")
    return out


def describe(entry: dict) -> str:
    """One recovery row as a human line, for the CLI panel."""
    try:
        kind = str(entry.get("kind") or "?")
        bits = [f"{kind} {str(entry.get('ref_id') or '')[:12]}"]
        operation = str(entry.get("operation") or "")
        if operation and operation != _safety.UNKNOWN:
            bits.append(f"[{operation}]")
        decision = str(entry.get("decision") or "")
        if decision and decision != A_NONE:
            bits.append(f"→ {decision.replace('_', ' ')}")
        reason = str(entry.get("reason") or "")
        if reason:
            bits.append(f"— {reason}")
        return " ".join(bits)
    except Exception:
        return "?"


def reset() -> None:
    """Drop the in-process counters and the last-scan memo. Tests only.

    Deliberately does NOT delete `exec_recovery` rows — they are the durable record
    of decisions already taken, and a "reset" that erased them would make a second
    scan re-decide work a first scan already resolved.
    """
    global _running
    with _lock:
        _running = 0
        _last.clear()
        for key in _counters:
            _counters[key] = 0
