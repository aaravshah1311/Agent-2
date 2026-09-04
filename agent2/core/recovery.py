# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/core/recovery.py
───────────────────────
Task 3 — automatic recovery of work that was interrupted mid-flight.
`core/tasks.py` owns the durable state; this module answers the three questions a
restart has to ask of it: *what was running*, *did it actually land*, and *what
must never run again*.

⚠️ NOT `/pause`, AND NOT `/resume`
───────────────────────────────────
Those two are CHAT controls and stay that way (`core/context.pause_chat`).
Nothing here is reachable from either, and a test pins that. Recovery is driven
by persistent task state alone — which is what lets it handle the case `/resume`
structurally cannot: a process that died without running a single handler.

⚠️ COMPLETED WORK IS NEVER HANDED BACK AS WORK
───────────────────────────────────────────────
`plan()` splits the checklist into `done` / `resume` / `waiting`, and `adopt()`
touches ONLY the interrupted task. A terminal task is not reopened, not requeued,
and reaches the model exactly once — as "already completed, do NOT repeat".
`tasks.sync_list()` refuses to regress it even if the model asks; this layer says
the same thing in the words the model reads first, so it never asks.

⚠️ AND A DESTRUCTIVE STEP CAUGHT IN FLIGHT IS VERIFIED, NEVER REPLAYED
───────────────────────────────────────────────────────────────────────
`checkpoint_view()["destructive_pending"]` means a write / delete / shell command
was in flight when the process died and NOBODY knows whether it landed.
`verify_step()` goes and looks instead of guessing, and returns one of:

  not_applied     the target is absent — the write never happened → safe to repeat
  likely_applied  the target exists and changed at or after the step started
  uncertain       the target exists, but it may have existed all along
  unverifiable    a shell command; its effects are not knowable from here

Only `not_applied` is safe to repeat unattended. The other three surface to the
user AND to the model as an explicit warning, because "retry and hope" on a
half-finished `delete_file` or a `git commit` is the exact damage rule 21 forbids.

Presentation lives elsewhere, as always: `cli/taskview.render_recovery()` draws
the terminal panel and the browser reads `checkpoints[…].recovered` out of
`tasks.payload()`. Nothing here prints.
"""

from __future__ import annotations

import calendar
import os
import time
from dataclasses import dataclass, field

from agent2.core import sync
from agent2.core import tasks as _tasks

# Timestamps come from `core/tasks` rather than a second `strftime` here: the two
# sit side by side in one checkpoint dict, and a recovery stamp in a different
# format (or a different timezone) would make the trail unreadable.
_now = _tasks._now

# What `verify_step` can conclude. `not_applied` is the ONLY one a caller may act
# on without asking — see the module docstring.
V_NOT_APPLIED = "not_applied"
V_LIKELY_APPLIED = "likely_applied"
V_UNCERTAIN = "uncertain"
V_UNVERIFIABLE = "unverifiable"

SAFE_TO_REPEAT = frozenset((V_NOT_APPLIED,))

# Tools whose effect is a file at a known path, so the filesystem can be asked.
_WRITE_TOOLS = frozenset(("write_file", "multi_edit_files", "convert_file",
                          "run_file_op"))
_DELETE_TOOLS = frozenset(("delete_file",))


@dataclass
class Candidate:
    """One recoverable session, summarised for a chooser."""

    session_id: str
    chat_id: str = ""
    cwd: str = ""
    goal: str = ""
    updated_at: str = ""
    total: int = 0
    completed: int = 0
    open: int = 0

    def to_dict(self) -> dict:
        return {"session_id": self.session_id, "chat_id": self.chat_id,
                "cwd": self.cwd, "goal": self.goal,
                "updated_at": self.updated_at, "total": self.total,
                "completed": self.completed, "open": self.open}


@dataclass
class RecoveryPlan:
    """What a restart may do with one interrupted session.

    `done` is deliberately a separate list from `waiting` rather than a flag on a
    single list: the two get *opposite* instructions, and a single collection is
    how a caller ends up looping over "the tasks" and re-running finished work.
    """

    session_id: str = ""
    chat_id: str = ""
    cwd: str = ""
    goal: str = ""
    done: list = field(default_factory=list)      # terminal — never re-run
    resume: object | None = None                  # the interrupted task, if any
    waiting: list = field(default_factory=list)   # never started
    view: dict = field(default_factory=dict)      # checkpoint_view(resume)
    checks: list = field(default_factory=list)    # verify_step() per risky step
    attempt: int = 0

    @property
    def needs_verification(self) -> bool:
        """True when at least one in-flight destructive step is not provably safe
        to repeat. The gate rule 21 asks for."""
        return any(c.get("verdict") not in SAFE_TO_REPEAT for c in self.checks)

    def is_empty(self) -> bool:
        return self.resume is None and not self.waiting

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id, "chat_id": self.chat_id,
            "cwd": self.cwd, "goal": self.goal, "attempt": self.attempt,
            "done": [t.title for t in self.done],
            "resume": self.resume.title if self.resume is not None else "",
            "waiting": [t.title for t in self.waiting],
            "view": self.view, "checks": self.checks,
            "needs_verification": self.needs_verification,
        }


# ── Detect ────────────────────────────────────────────────────────────────────

def candidates(cwd: str = "", limit: int = 5,
               exclude: str = "") -> list[Candidate]:
    """Sessions in *cwd* that still hold open tasks — what a restart found.

    *exclude* drops the session this process is already using, so a live CLI does
    not offer to recover the plan it is in the middle of running.

    Never raises: startup detection is a courtesy, and a launcher that died
    because it could not read a recovery hint would be strictly worse than one
    that simply showed nothing.
    """
    try:
        rows = _tasks.unfinished_sessions(cwd or os.getcwd(), limit=max(1, limit) + 1)
    except Exception:
        return []
    out: list[Candidate] = []
    for row in rows:
        sid = str(row.get("id") or "")
        if not sid or sid == str(exclude or ""):
            continue
        try:
            summary = _tasks.summary(sid)
        except Exception:
            continue
        if not summary.get("open"):
            continue
        out.append(Candidate(
            session_id=sid, chat_id=str(row.get("chat_id") or ""),
            cwd=str(row.get("cwd") or ""), goal=str(row.get("goal") or ""),
            updated_at=str(row.get("updated_at") or ""),
            total=int(summary.get("total") or 0),
            completed=int(summary.get("completed") or 0),
            open=int(summary.get("open") or 0)))
        if len(out) >= max(1, limit):
            break
    return out


def latest(cwd: str = "", exclude: str = "") -> Candidate | None:
    """The single most recent recovery candidate, or None."""
    found = candidates(cwd, limit=1, exclude=exclude)
    return found[0] if found else None


# ── Verify ────────────────────────────────────────────────────────────────────

def _epoch(stamp: str) -> float:
    """`core/tasks._now()` writes UTC, so the parse must too — `mktime` would
    read it as local time and shift the comparison by the whole UTC offset."""
    try:
        return float(calendar.timegm(time.strptime(str(stamp), "%Y-%m-%d %H:%M:%S")))
    except (TypeError, ValueError):
        return 0.0


def verify_step(step: dict) -> dict:
    """Ask the filesystem what really happened to one in-flight step.

    ⚠️ This is the "verify the last known state" half of recovery, and it reports
    EVIDENCE rather than a decision. The tolerance below is deliberately biased
    toward `likely_applied`: mistaking a finished write for an unfinished one
    would repeat it, and repeating is the outcome rule 21 forbids. Being wrong the
    other way only costs a question.
    """
    tool = str(step.get("name") or "")
    target = str(step.get("detail") or "")
    out = {"tool": tool, "target": target, "at": str(step.get("at") or ""),
           "verdict": V_UNCERTAIN, "evidence": ""}

    if tool == "run_command":
        out["verdict"] = V_UNVERIFIABLE
        out["evidence"] = "a shell command's effects cannot be read back from here"
        return out
    if not target or (tool not in _WRITE_TOOLS and tool not in _DELETE_TOOLS):
        out["evidence"] = "no inspectable target recorded for this step"
        return out

    try:
        exists = os.path.exists(target)
        mtime = os.path.getmtime(target) if exists else 0.0
    except OSError as ex:
        out["evidence"] = f"could not stat {target}: {ex}"
        return out

    if tool in _DELETE_TOOLS:
        if exists:
            out["verdict"] = V_NOT_APPLIED
            out["evidence"] = f"{target} is still present — the delete did not happen"
        else:
            out["verdict"] = V_LIKELY_APPLIED
            out["evidence"] = f"{target} is gone — the delete appears to have happened"
        return out

    if not exists:
        out["verdict"] = V_NOT_APPLIED
        out["evidence"] = f"{target} does not exist — nothing was written"
    elif mtime + 1.0 >= _epoch(step.get("at") or ""):
        out["verdict"] = V_LIKELY_APPLIED
        out["evidence"] = f"{target} was modified at or after the step started"
    else:
        out["verdict"] = V_UNCERTAIN
        out["evidence"] = f"{target} exists but predates the step — it may be the old content"
    return out


def verify(task) -> list[dict]:
    """`verify_step()` for every destructive step of *task* caught in flight."""
    try:
        view = _tasks.checkpoint_view(task)
    except Exception:
        return []
    return [verify_step(s) for s in view.get("steps", [])
            if s.get("status") == _tasks.STEP_RUNNING and s.get("destructive")]


# ── Plan ──────────────────────────────────────────────────────────────────────

def plan(session_id: str) -> RecoveryPlan:
    """Split an interrupted session into done / resume / waiting.

    ⚠️ `resume` is at most ONE task. A checklist is worked top to bottom, so the
    first unfinished item is where the previous process was; handing back several
    "in progress" tasks would invite a caller to restart work that a later task
    already depended on. Everything after it is `waiting` — untouched, in order.
    """
    out = RecoveryPlan(session_id=str(session_id or ""))
    row = _tasks.get_session(out.session_id) if out.session_id else None
    if row:
        out.chat_id = str(row.get("chat_id") or "")
        out.cwd = str(row.get("cwd") or "")
        out.goal = str(row.get("goal") or "")
    for task in _tasks.list_tasks(out.session_id):
        if task.is_terminal:
            out.done.append(task)
        elif out.resume is None and task.status in (
                _tasks.TaskStatus.RUNNING, _tasks.TaskStatus.PAUSED,
                _tasks.TaskStatus.QUEUED):
            out.resume = task
        else:
            out.waiting.append(task)

    # A plan whose only open items never started still recovers — the first
    # pending task becomes the resume point. Nothing was in flight, so there is
    # nothing to verify, which is exactly what the empty `checks` list says.
    if out.resume is None and out.waiting:
        out.resume = out.waiting.pop(0)

    if out.resume is not None:
        out.view = _tasks.checkpoint_view(out.resume)
        out.attempt = int(getattr(out.resume, "attempt_count", 0) or 0)
        if out.view.get("destructive_pending"):
            out.checks = verify(out.resume)
    return out


def describe(rp: RecoveryPlan) -> str:
    """The recovery briefing that goes to the MODEL, as plain text.

    ⚠️ Completed tasks are listed with an explicit "do NOT run these again".
    `sync_list()` already refuses to regress them, but that is a guard against a
    request the model should never make — this is what stops it making the
    request, and it is also what stops the model *redoing* the work outside the
    checklist entirely, which no database constraint can catch.
    """
    if rp.is_empty():
        return ""
    lines = ["## RECOVERED SESSION — work was interrupted and is being continued"]
    if rp.goal:
        lines.append(f"Goal: {rp.goal}")
    if rp.done:
        lines.append("")
        lines.append("ALREADY COMPLETED — do NOT run these again, and do not "
                     "re-verify them unless the user asks:")
        lines += [f"  ✓ {t.title}" for t in rp.done]
    if rp.resume is not None:
        lines.append("")
        lines.append(f"INTERRUPTED — resume here: {rp.resume.title}")
        stopped = (rp.view.get("stopped") or {}).get("reason", "")
        if stopped:
            lines.append(f"  stopped because: {stopped}")
        if rp.view.get("completed"):
            lines.append("  sub-steps already done: "
                         + ", ".join(rp.view["completed"]))
        if rp.view.get("current"):
            lines.append(f"  in flight when it stopped: {rp.view['current']}")
    if rp.waiting:
        lines.append("")
        lines.append("NOT STARTED:")
        lines += [f"  ○ {t.title}" for t in rp.waiting]
    if rp.checks:
        lines.append("")
        lines.append("⚠ UNCERTAIN DESTRUCTIVE OPERATIONS — these were in flight "
                     "when the process died and MAY ALREADY HAVE HAPPENED. Check "
                     "the current state (read the file, run `git status`) BEFORE "
                     "repeating any of them:")
        for c in rp.checks:
            lines.append(f"  - {c['tool']} {c['target']}: {c['evidence']}")
        lines.append("  A step marked not_applied is safe to repeat. Anything "
                     "else must be verified first — never blindly retry it.")
    return "\n".join(lines)


# ── Adopt ─────────────────────────────────────────────────────────────────────

def adopt(rp: RecoveryPlan, *, reason: str = "recovered after restart") -> bool:
    """Mark the interrupted task as picked back up. Returns True if anything moved.

    ⚠️ Touches the resume task ONLY. Completed tasks are not reopened, not
    requeued and not renumbered — `plan()` put them in `done` precisely so that
    no code path here can reach them. `waiting` tasks are already PENDING and the
    normal loop will reach them in order.

    ⚠️ And the task is moved to PENDING, not RUNNING. Nothing is executing yet at
    the moment recovery is adopted; marking it RUNNING would make the *next*
    crash think a step was in flight when none was, which is how a recovery
    system starts inventing its own uncertain destructive operations.
    """
    if rp.resume is None:
        return False
    stamp = {"at": _now(), "reason": str(reason or "")[:200],
             "attempt": rp.attempt + 1,
             "checks": [dict(c) for c in rp.checks]}
    _tasks.save_checkpoint(rp.resume.id, {_tasks.CP_RECOVERED: stamp},
                           notify=False)
    if rp.resume.status != _tasks.TaskStatus.PENDING:
        _tasks.set_status(rp.resume.id, _tasks.TaskStatus.PENDING, notify=False)
    _tasks.touch_session(rp.session_id)
    sync.notify("tasks", session_id=rp.session_id, event="recovered",
                task_id=rp.resume.id)
    return True


def recover(session_id: str, *, reason: str = "recovered after restart"
            ) -> RecoveryPlan:
    """`plan()` + `adopt()` — the one call a surface needs to resume a session."""
    rp = plan(session_id)
    if not rp.is_empty():
        adopt(rp, reason=reason)
        rp.resume = _tasks.get(rp.resume.id) if rp.resume is not None else None
    return rp


def abandon(session_id: str, *, reason: str = "not resumed") -> int:
    """Decline a recovery: cancel the open tasks and close the session.

    The other half of the choice. Without it a declined session stays "unfinished"
    forever and is offered again at every single launch, which trains the user to
    ignore the prompt — and the one time it mattered they would ignore that too.
    """
    if not session_id:
        return 0
    count = _tasks.cancel_open(session_id, reason=reason)
    _tasks.close_session(session_id, _tasks.SESSION_ABANDONED)
    return count


# ── Hand-off to the model ─────────────────────────────────────────────────────
#
# ⚠️ THE BRIEF IS DURABLE AND CONSUMED EXACTLY ONCE.
# Adopting a recovery and telling the model about it are two different moments —
# the user may adopt at launch and type nothing for an hour, or close the terminal
# again first. An in-process flag would lose the brief in exactly the second case,
# which is the one recovery exists for, so the "still owed" bit lives in the
# checkpoint next to the rest of the trail.
#
# And it is cleared BEFORE the turn runs rather than after: a brief re-injected
# every turn would keep telling the model to resume a task it has since finished,
# and the failure mode of losing one brief (the user repeats themselves) is far
# cheaper than the failure mode of repeating it forever.

def pending_brief(session_id: str) -> str:
    """The recovery briefing still owed to the model for *session_id*, or ""."""
    rp = plan(session_id)
    if rp.resume is None:
        return ""
    stamp = _tasks.checkpoint_view(rp.resume).get("recovered") or {}
    if not stamp or stamp.get("briefed"):
        return ""
    return describe(rp)


def consume_brief(session_id: str) -> str:
    """`pending_brief()`, and mark it delivered. Returns "" when none is owed."""
    text = pending_brief(session_id)
    if not text:
        return ""
    rp = plan(session_id)
    if rp.resume is None:
        return ""
    stamp = dict(_tasks.checkpoint_view(rp.resume).get("recovered") or {})
    stamp["briefed"] = True
    _tasks.save_checkpoint(rp.resume.id, {_tasks.CP_RECOVERED: stamp},
                           notify=False)
    return text


def brief_for_chat(chat_id: str) -> str:
    """`consume_brief()` for whichever session belongs to *chat_id*.

    The agent loops call this on the way into a turn, so it must never raise and
    never create anything: a chat with no plan has no recovery, and a turn that
    died looking for one would be a recovery system causing the outage.
    """
    try:
        if not chat_id:
            return ""
        row = _tasks.active_session_for_chat(str(chat_id))
        return consume_brief(str(row["id"])) if row else ""
    except Exception:
        return ""
