# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/core/tasks.py
────────────────────
THE persistent task engine. One task list, in `agent2.db`, shared by the CLI,
the Web UI, and (later) the workflow runner.

⚠️ THE BUG THIS FIXES
──────────────────────
The live checklist used to be a Python list — `Session.todos`, written by the
`update_todo` tool. Three consequences, all of which users saw:

  1. `update_todo` REPLACED the list on every call (`store.clear()`), so a task
     the model had already marked `completed` went back to `pending` the moment
     the model re-sent the list with a stale status. Progress went backwards.
  2. Nothing was persisted. Ctrl+C, a crash, or just a new process started the
     plan from task 1 with every completed step forgotten.
  3. On success the tool's result was rendered NOWHERE in the CLI — there was no
     branch for it in either agent loop — so the user could not see the list at
     all, only "✅ Updating task list".

This module owns the durable half. Three invariants are load-bearing:

  ⚠️ `sync_list()` NEVER regresses a terminal task. The model re-sends the whole
     checklist on each call, frequently with statuses that lag reality. A task
     that reached COMPLETED/FAILED/CANCELLED/SKIPPED can only be moved by an
     explicit `reopen()`. This is what makes "resume does not repeat completed
     work" true, and it is pinned by
     test_tasks.py::test_sync_list_never_regresses_a_completed_task.

  ⚠️ A task the model DROPS from the list is deleted only when it is still
     open. Terminal tasks are kept, because a revised plan must not erase the
     record of work already done ("do not lose completed work"). Kept tasks are
     renumbered to the FRONT, in their previous relative order, so the list
     still reads "done above, remaining below" — see `_renumber`.

  ⚠️ Every write is one `db.batch()` and exactly ONE `sync.notify("tasks", …)`.
     A per-row notify would make the CLI panel redraw N times for one merge,
     which is the duplicate-list printing the task brief calls out.

  ⚠️ A checkpoint sub-step is written WHEN IT HAPPENS, never on the way out.
     `SIGKILL`, a power cut and an OOM kill run no exit handler, so a design that
     flushes progress at shutdown loses exactly the crash it exists for. See the
     Checkpoints section below; `interrupt()` adds only the stop *reason*, and
     the position is already on disk without it.

Presentation lives elsewhere: `cli/taskview.py` draws the terminal panel and
`server/sockets.py` emits `chat_tasks`. Nothing here prints or emits.

Ordering note: `list_tasks()` orders by `(seq, created_at, id)`. `seq` alone is
not a total order — two tasks can share a seq for the instant between an INSERT
and `_renumber` — and an unstable sort would make the panel flicker.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field

from agent2 import database as db
from agent2.core import metrics as _metrics
from agent2.core import sync

# ── States ────────────────────────────────────────────────────────────────────


class TaskStatus:
    """The eight supported states. Strings, not an Enum: they round-trip through
    SQLite text columns, JSON payloads and Socket.IO without conversion."""

    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


ALL_STATUSES = (
    TaskStatus.PENDING, TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.PAUSED,
    TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED,
    TaskStatus.SKIPPED,
)

# Reached the end of its life: no further automatic transition may move it.
TERMINAL = frozenset((TaskStatus.COMPLETED, TaskStatus.FAILED,
                      TaskStatus.CANCELLED, TaskStatus.SKIPPED))

# Counts toward "still to do".
OPEN = frozenset((TaskStatus.PENDING, TaskStatus.QUEUED, TaskStatus.RUNNING,
                  TaskStatus.PAUSED))

# "Some worker is holding this row" — the two statuses worker recovery sweeps, and
# the ones `interrupt()` parks. A tuple, because it is spliced into SQL and the
# parameter order has to be deterministic.
#
# ⚠️ QUEUED BELONGS HERE AND PAUSED DOES NOT, AND THAT IS NOT A SPECTRUM. QUEUED
# has exactly one writer — `core/dag/store.claim()`, where the status write *is*
# the lock — so it means "a scheduler took this and is about to mark it RUNNING",
# never "a human decided something". PAUSED is where `interrupt()` parks a task and
# where `/pause` puts a hold somebody chose, so sweeping it would hand recovery
# every chat the user paused on purpose. See `stale_running()` for what a claimed
# row that never reached RUNNING costs.
HELD = (TaskStatus.RUNNING, TaskStatus.QUEUED)

# What the model is allowed to say, mapped onto our vocabulary. The tool schema
# has always advertised pending|in_progress|completed, so those three must keep
# working exactly as before; the rest are accepted because the same merge path
# is used by the workflow runner, which speaks the full set.
_MODEL_STATUS = {
    "pending": TaskStatus.PENDING,
    "todo": TaskStatus.PENDING,
    "queued": TaskStatus.QUEUED,
    "in_progress": TaskStatus.RUNNING,
    "in-progress": TaskStatus.RUNNING,
    "active": TaskStatus.RUNNING,
    "running": TaskStatus.RUNNING,
    "paused": TaskStatus.PAUSED,
    "blocked": TaskStatus.PAUSED,
    "completed": TaskStatus.COMPLETED,
    "complete": TaskStatus.COMPLETED,
    "done": TaskStatus.COMPLETED,
    "failed": TaskStatus.FAILED,
    "error": TaskStatus.FAILED,
    "cancelled": TaskStatus.CANCELLED,
    "canceled": TaskStatus.CANCELLED,
    "skipped": TaskStatus.SKIPPED,
    "skip": TaskStatus.SKIPPED,
}


def normalize_status(value: str | None) -> str:
    """Map anything a model or API caller sends onto a real status.

    Unknown values become PENDING rather than raising: a typo in one checklist
    item must not fail the tool call and lose the whole update.
    """
    return _MODEL_STATUS.get(str(value or "").strip().lower(), TaskStatus.PENDING)


# The glyph each state renders as. Shared by the CLI panel and the Web UI so the
# two surfaces cannot drift (CLAUDE.md's one-declaration rule).
GLYPHS = {
    TaskStatus.PENDING: "○",
    TaskStatus.QUEUED: "◔",
    TaskStatus.RUNNING: "●",
    TaskStatus.PAUSED: "⏸",
    TaskStatus.COMPLETED: "✓",
    TaskStatus.FAILED: "✗",
    TaskStatus.CANCELLED: "⊘",
    TaskStatus.SKIPPED: "⊙",
}

MAX_TITLE = 200
MAX_TEXT = 4000
DEFAULT_PRIORITY = 5

# How long a `HELD` task (RUNNING or QUEUED) may go untouched before
# `stale_running()` calls it stuck. ⚠️ ONE declaration: it is `stale_running`'s
# default *and* the number `stats()` reports, because a health payload that names a
# threshold different from the one the sweep applied describes a sweep that never
# ran.
STALE_AFTER = 90.0

# Ceiling on the stale sweep a `stats()` read performs. A health endpoint may not
# turn into an unbounded scan of every held row, so the count is reported
# alongside the ceiling that produced it — see `stats()`.
STALE_SAMPLE = 200


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


def _uid() -> str:
    return uuid.uuid4().hex[:12]


def _jload(raw, fallback):
    """Parse a JSON column, degrading to *fallback* on anything unexpected.

    Narrow on purpose: only a malformed/absent value is tolerated. A programming
    error (a non-serialisable object reaching the writer) still raises there.
    """
    if raw in (None, ""):
        return fallback
    try:
        out = json.loads(raw)
    except (TypeError, ValueError):
        return fallback
    return out if isinstance(out, type(fallback)) else fallback


def _jdump(value) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return json.dumps(str(value))


def _int(value, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _float(value, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _clamp01(value: float) -> float:
    return 0.0 if value < 0 else (1.0 if value > 1 else value)


def _text(value, limit: int = MAX_TEXT) -> str:
    return str(value or "")[:limit]


def _title_key(title: str) -> str:
    """Match key for `sync_list`. The model retypes titles between calls, so a
    changed capitalisation or a doubled space must not fork a task in two."""
    return " ".join(str(title or "").split()).lower()


def _project_key(cwd) -> str:
    """Canonicalise a project directory before it is stored or queried.

    ⚠️ Delegates to `core.context.project_key` — the ONE declaration. A task
    session is found by `(cwd, status)`, and `ToolContext.task_session()` passes
    `workspace.root()`, which is NOT normcased, while chats store the normcased
    form. Storing the raw string made `unfinished_sessions()` return nothing on
    Windows while every single-row read still looked correct — the whole recovery
    path went quietly dead.
    """
    raw = _text(cwd)
    if not raw:
        return ""
    from agent2.core.context import project_key
    return project_key(raw)


# ── The record ────────────────────────────────────────────────────────────────

@dataclass
class Task:
    """One row of `agent_tasks`, with its JSON columns already decoded."""

    id: str
    session_id: str
    title: str
    parent_task_id: str = ""
    seq: int = 0
    description: str = ""
    status: str = TaskStatus.PENDING
    priority: int = DEFAULT_PRIORITY
    dependencies: list = field(default_factory=list)
    created_at: str = ""
    started_at: str = ""
    completed_at: str = ""
    attempt_count: int = 0
    progress: float = 0.0
    result: str = ""
    error: str = ""
    checkpoint: dict = field(default_factory=dict)
    # Task 24 §2's durable recovery fields (migrations 23–26). ⚠️ `updated_at` is
    # the ONE of the four that every writer maintains, because it is the one worker
    # recovery asks about: a task whose row has not changed since the cutoff, while
    # still marked RUNNING, is a task whose worker did not survive. The other three
    # are stamped by whoever claims the task and are informational — a recovery
    # report can name the process and the operation instead of only the task.
    updated_at: str = ""
    last_heartbeat: str = ""
    worker_id: str = ""
    execution_id: str = ""

    @classmethod
    def from_row(cls, row: dict) -> Task:
        return cls(
            id=str(row.get("id") or ""),
            session_id=str(row.get("session_id") or ""),
            title=str(row.get("title") or ""),
            parent_task_id=str(row.get("parent_task_id") or ""),
            seq=_int(row.get("seq"), 0),
            description=str(row.get("description") or ""),
            status=str(row.get("status") or TaskStatus.PENDING),
            priority=_int(row.get("priority"), DEFAULT_PRIORITY),
            dependencies=_jload(row.get("dependencies"), []),
            created_at=str(row.get("created_at") or ""),
            started_at=str(row.get("started_at") or ""),
            completed_at=str(row.get("completed_at") or ""),
            attempt_count=_int(row.get("attempt_count"), 0),
            progress=_float(row.get("progress"), 0.0),
            result=str(row.get("result") or ""),
            error=str(row.get("error") or ""),
            checkpoint=_jload(row.get("checkpoint"), {}),
            updated_at=str(row.get("updated_at") or ""),
            last_heartbeat=str(row.get("last_heartbeat") or ""),
            worker_id=str(row.get("worker_id") or ""),
            execution_id=str(row.get("execution_id") or ""),
        )

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL

    @property
    def is_open(self) -> bool:
        return self.status in OPEN

    @property
    def glyph(self) -> str:
        return GLYPHS.get(self.status, GLYPHS[TaskStatus.PENDING])

    def to_payload(self) -> dict:
        """The wire shape. One declaration, used by both surfaces — the CLI panel
        and `chat_tasks` must never disagree about what a task looks like."""
        return {
            "id": self.id,
            "session_id": self.session_id,
            "parent_task_id": self.parent_task_id,
            "seq": self.seq,
            "title": self.title,
            "description": self.description,
            "status": self.status,
            "glyph": self.glyph,
            "priority": self.priority,
            "dependencies": list(self.dependencies),
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "attempt_count": self.attempt_count,
            "progress": self.progress,
            "result": self.result,
            "error": self.error,
            "checkpoint": dict(self.checkpoint),
            "updated_at": self.updated_at,
            "last_heartbeat": self.last_heartbeat,
            "worker_id": self.worker_id,
            "execution_id": self.execution_id,
        }


# ── Sessions ──────────────────────────────────────────────────────────────────
# A "task session" is one plan: the checklist for a chat in a directory. It is
# NOT `core.session.Session` (a live in-process turn) — this one outlives the
# process, which is the entire point.

SESSION_ACTIVE = "active"
SESSION_DONE = "done"
SESSION_ABANDONED = "abandoned"


def open_session(*, chat_id: str = "", cwd: str = "", workspace_id: str = "",
                 model: str = "", mode: str = "", goal: str = "",
                 surface: str = "") -> str:
    """Create a task session and return its id."""
    sid = _uid()
    stamp = _now()
    db.exe(
        "INSERT INTO task_sessions"
        " (id, chat_id, cwd, workspace_id, model, mode, goal, status, surface,"
        "  created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (sid, _text(chat_id, MAX_TITLE), _project_key(cwd), _text(workspace_id, MAX_TITLE),
         _text(model, MAX_TITLE), _text(mode, MAX_TITLE), _text(goal),
         SESSION_ACTIVE, _text(surface, MAX_TITLE), stamp, stamp),
    )
    sync.notify("tasks", session_id=sid, event="session_open")
    return sid


def get_session(session_id: str) -> dict | None:
    if not session_id:
        return None
    return db.qone("SELECT * FROM task_sessions WHERE id=?", (str(session_id),))


def touch_session(session_id: str, **fields) -> None:
    """Bump `updated_at` (and optionally model/mode/goal/status)."""
    if not session_id:
        return
    allowed = ("chat_id", "cwd", "workspace_id", "model", "mode", "goal",
               "status", "surface")
    sets, params = ["updated_at=?"], [_now()]
    for key in allowed:
        if key in fields:
            sets.append(f"{key}=?")
            params.append(_project_key(fields[key]) if key == "cwd"
                          else _text(fields[key]))
    params.append(str(session_id))
    db.exe(f"UPDATE task_sessions SET {', '.join(sets)} WHERE id=?", tuple(params))


def close_session(session_id: str, status: str = SESSION_DONE) -> None:
    if not session_id:
        return
    touch_session(session_id, status=status)
    sync.notify("tasks", session_id=session_id, event="session_close")


def latest_session_for_chat(chat_id: str) -> dict | None:
    """The newest task session bound to *chat_id*, whatever its status."""
    if not chat_id:
        return None
    return db.qone(
        "SELECT * FROM task_sessions WHERE chat_id=?"
        " ORDER BY updated_at DESC, rowid DESC LIMIT 1",
        (str(chat_id),),
    )


def active_session_for_chat(chat_id: str) -> dict | None:
    """The ACTIVE task session for *chat_id*, or None. Read-only — never creates.

    Split out from `session_for_chat` because the two callers want opposite
    things: the `update_todo` path wants get-or-create, and the checkpoint path
    must never be the reason a session row exists (see
    `ToolContext.existing_task_session`).
    """
    if not chat_id:
        return None
    return db.qone(
        "SELECT * FROM task_sessions WHERE chat_id=? AND status=?"
        " ORDER BY updated_at DESC, rowid DESC LIMIT 1",
        (str(chat_id), SESSION_ACTIVE),
    )


def session_for_chat(chat_id: str, **create_kwargs) -> str:
    """Get-or-create the ACTIVE task session for *chat_id*.

    The CLI creates no chat row until there is something to save (CLAUDE.md), so
    `chat_id` may legitimately be empty on the first turn; in that case a fresh
    unbound session is created and can be re-bound later with `touch_session`.
    """
    row = active_session_for_chat(chat_id)
    if row:
        return str(row["id"])
    create_kwargs.setdefault("chat_id", chat_id)
    return open_session(**create_kwargs)


def unfinished_sessions(cwd: str = "", limit: int = 20) -> list[dict]:
    """Active sessions that still hold open tasks — the recovery candidates.

    Task 3 consumes this; it lives here because the "what counts as unfinished"
    rule is a property of the task model, not of the recovery UI.

    ⚠️ `rowid DESC` is a required tiebreaker, not tidiness. `updated_at` has
    one-SECOND resolution, so a machine that opened several sessions in the same
    second leaves `ORDER BY updated_at DESC` fully tied, and a tie plus `LIMIT`
    can drop the newest session while returning older ones. Recovery offers "the
    session you just lost", so the row that must never fall out of the window is
    exactly the one an untied sort is free to discard. Matches the tiebreaker the
    single-session lookups above already use.
    """
    # ⚠️ Bind order follows the SQL, not the logic: the `status IN (…)` marks sit
    # in the JOIN, which is bound BEFORE the WHERE clause. Appending them after
    # the status/cwd values made this return nothing at all.
    marks = ",".join("?" * len(OPEN))
    params: list = [*sorted(OPEN), SESSION_ACTIVE]
    where = "s.status=?"
    if cwd:
        where += " AND s.cwd=?"
        params.append(_project_key(cwd))
    params.append(_int(limit, 20))
    return db.qall(
        f"SELECT s.*, COUNT(t.id) AS open_tasks FROM task_sessions s"
        f" JOIN agent_tasks t ON t.session_id = s.id AND t.status IN ({marks})"
        f" WHERE {where}"
        f" GROUP BY s.id ORDER BY s.updated_at DESC, s.rowid DESC LIMIT ?",
        tuple(params),
    )


# ── Reads ─────────────────────────────────────────────────────────────────────

def list_tasks(session_id: str) -> list[Task]:
    """Every task of a session in display order (see the ordering note above)."""
    if not session_id:
        return []
    rows = db.qall(
        "SELECT * FROM agent_tasks WHERE session_id=?"
        " ORDER BY seq ASC, created_at ASC, id ASC",
        (str(session_id),),
    )
    return [Task.from_row(r) for r in rows]


def get(task_id: str) -> Task | None:
    if not task_id:
        return None
    row = db.qone("SELECT * FROM agent_tasks WHERE id=?", (str(task_id),))
    return Task.from_row(row) if row else None


def current(session_id: str) -> Task | None:
    """The task a reader should point at: the running one, else the next open."""
    tasks = list_tasks(session_id)
    for t in tasks:
        if t.status == TaskStatus.RUNNING:
            return t
    for t in tasks:
        if t.is_open:
            return t
    return None


def summary(session_id: str, tasks: list[Task] | None = None) -> dict:
    """Counts + the "5/5 completed" line the CLI prints under the panel."""
    items = list_tasks(session_id) if tasks is None else tasks
    counts = dict.fromkeys(ALL_STATUSES, 0)
    for t in items:
        counts[t.status] = counts.get(t.status, 0) + 1
    total = len(items)
    done = counts[TaskStatus.COMPLETED]
    # "Settled" is what the progress fraction is really about: a skipped or
    # cancelled task is finished with, so a plan that ends 3 done / 2 skipped is
    # complete, not stuck at 3/5 forever.
    settled = sum(counts[s] for s in TERMINAL)
    return {
        "session_id": session_id,
        "total": total,
        "completed": done,
        "settled": settled,
        "open": total - settled,
        "counts": counts,
        "done": total > 0 and settled == total,
        "progress": f"{done}/{total}" if total else "0/0",
        "percent": round(100.0 * settled / total, 1) if total else 0.0,
    }


def payload(session_id: str) -> dict:
    """The single snapshot both surfaces render. One declaration.

    `checkpoints` maps task id → `checkpoint_view()` for every task that has
    sub-steps. Only those: attaching an empty view to each of a dozen tasks would
    triple the payload to say nothing.
    """
    tasks = list_tasks(session_id)
    views = {}
    for t in tasks:
        if _steps_of(t.checkpoint) or t.checkpoint.get(CP_STOPPED) \
                or t.checkpoint.get(CP_RECOVERED):
            views[t.id] = checkpoint_view(t)
    return {
        "session_id": session_id,
        "tasks": [t.to_payload() for t in tasks],
        "summary": summary(session_id, tasks),
        "checkpoints": views,
    }


def ready(session_id: str, tasks: list[Task] | None = None) -> list[Task]:
    """Open tasks whose dependencies have all settled, best-priority first.

    Unresolvable dependencies (an id that no longer exists) are ignored rather
    than treated as blocking — a dangling reference must not deadlock a plan.

    A caller that already holds the session's rows may pass them as *tasks*, the
    same way `blockers()` accepts a pool: `core.workflow.runner.state_for()` needs
    both the full list and the runnable subset on the turn path, and re-reading the
    same rows to answer the second question is a query nobody needs. ⚠️ It is a
    pool, never a *predicate*: this function stays the one declaration of what
    "ready" means, because a second copy in a caller would release a blocked node
    to run out of order with a plausible result and no error anywhere.
    """
    tasks = list_tasks(session_id) if tasks is None else tasks
    by_id = {t.id: t for t in tasks}
    out = []
    for t in tasks:
        if not t.is_open:
            continue
        blocked = any(
            dep in by_id and not by_id[dep].is_terminal for dep in t.dependencies
        )
        if not blocked:
            out.append(t)
    out.sort(key=lambda t: (t.priority, t.seq))
    return out


def blockers(task: Task, tasks: list[Task] | None = None) -> list[Task]:
    """Which of *task*'s dependencies are still unsettled."""
    pool = list_tasks(task.session_id) if tasks is None else tasks
    by_id = {t.id: t for t in pool}
    return [by_id[d] for d in task.dependencies
            if d in by_id and not by_id[d].is_terminal]


# ── Writes ────────────────────────────────────────────────────────────────────

def create(session_id: str, title: str, *, description: str = "",
           status: str = TaskStatus.PENDING, priority: int = DEFAULT_PRIORITY,
           dependencies=(), parent_task_id: str = "", seq: int | None = None,
           checkpoint: dict | None = None, notify: bool = True) -> Task:
    """Insert one task. `seq` defaults to the end of the session's list."""
    if not session_id:
        raise ValueError("create() needs a session_id")
    title = _text(title, MAX_TITLE).strip()
    if not title:
        raise ValueError("create() needs a title")
    if seq is None:
        row = db.qone("SELECT MAX(seq) AS m FROM agent_tasks WHERE session_id=?",
                      (str(session_id),))
        seq = _int((row or {}).get("m"), -1) + 1
    tid = _uid()
    status = status if status in ALL_STATUSES else TaskStatus.PENDING
    now = _now()
    db.exe(
        "INSERT INTO agent_tasks"
        " (id, session_id, parent_task_id, seq, title, description, status,"
        "  priority, dependencies, created_at, started_at, completed_at,"
        "  attempt_count, progress, result, error, checkpoint, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (tid, str(session_id), _text(parent_task_id, MAX_TITLE), int(seq), title,
         _text(description), status, _int(priority, DEFAULT_PRIORITY),
         _jdump([str(d) for d in (dependencies or [])]), now,
         now if status == TaskStatus.RUNNING else "",
         now if status in TERMINAL else "",
         0, 0.0, "", "", _jdump(checkpoint or {}), now),
    )
    if notify:
        touch_session(session_id)
        sync.notify("tasks", session_id=session_id, task_id=tid, event="created")
    return get(tid)  # re-read so callers see the stored (normalised) row


def _apply(task: Task, status: str, *, result=None, error=None,
           progress=None, checkpoint=None, bump_attempt: bool = False,
           worker_id=None, execution_id=None, beat: bool = False) -> Task:
    """The ONE status writer. Every transition helper funnels through here so the
    timestamp/attempt bookkeeping cannot drift between them.

    ⚠️ `updated_at` IS STAMPED HERE, UNCONDITIONALLY, AND THAT IS WHY THIS HAS TO
    STAY THE ONLY WRITER. It is the sole input to `stale_running()`, so a second
    UPDATE that forgot it would leave a live task looking untouched — and worker
    recovery would report a task some worker is happily working on as interrupted.
    That failure is silent in the worst direction: the row is real, the timestamps
    look plausible, and only the *conclusion* is wrong.

    ⚠️ IT IS ALSO WHERE `task.duration` IS MEASURED (Task 27), for that same
    single-writer reason, and it is measured from the row's own two stamps —
    `spanned()`, not a monotonic clock. A task is durable and may outlive the
    process that started it, so `time.monotonic()` at start would be unreadable
    after a restart, and a `started_mono` column would be a second declaration of
    when the task began. The label is the terminal status, so "how long does a
    task take" and "how long does a task take *to fail*" are separate answers.
    """
    sets = {"status": status}
    now = _now()
    sets["updated_at"] = now
    if status == TaskStatus.RUNNING:
        if not task.started_at:
            sets["started_at"] = now
        sets["completed_at"] = ""
    if status in TERMINAL:
        sets["completed_at"] = now
        if status == TaskStatus.COMPLETED and progress is None:
            sets["progress"] = 1.0
    if bump_attempt:
        sets["attempt_count"] = task.attempt_count + 1
    if result is not None:
        sets["result"] = _text(result)
    if error is not None:
        sets["error"] = _text(error)
    if progress is not None:
        sets["progress"] = _clamp01(_float(progress, task.progress))
    if checkpoint is not None:
        sets["checkpoint"] = _jdump(checkpoint)
    if worker_id is not None:
        sets["worker_id"] = _text(worker_id, MAX_TITLE)
    if execution_id is not None:
        sets["execution_id"] = _text(execution_id, MAX_TITLE)
    if beat:
        sets["last_heartbeat"] = now
    cols = ", ".join(f"{k}=?" for k in sets)
    db.exe(f"UPDATE agent_tasks SET {cols} WHERE id=?",
           (*sets.values(), task.id))
    if status in TERMINAL and task.started_at:
        _metrics.spanned(_metrics.TASK_DURATION, task.started_at, now, status)
    return get(task.id)


def set_status(task_id: str, status: str, *, result=None, error=None,
               progress=None, force: bool = False,
               notify: bool = True) -> Task | None:
    """Move a task to *status*.

    ⚠️ A terminal task is NOT moved unless `force=True` (that is `reopen()`).
    This is the invariant that keeps completed work completed across a resume.
    """
    task = get(task_id)
    if task is None:
        return None
    status = status if status in ALL_STATUSES else TaskStatus.PENDING
    if task.is_terminal and not force:
        return task
    bump = status == TaskStatus.RUNNING and task.status != TaskStatus.RUNNING
    out = _apply(task, status, result=result, error=error, progress=progress,
                 bump_attempt=bump)
    if notify:
        touch_session(task.session_id)
        sync.notify("tasks", session_id=task.session_id, task_id=task.id,
                    event=status)
    return out


def start(task_id: str, **kw) -> Task | None:
    return set_status(task_id, TaskStatus.RUNNING, **kw)


def queue(task_id: str, **kw) -> Task | None:
    return set_status(task_id, TaskStatus.QUEUED, **kw)


def pause(task_id: str, **kw) -> Task | None:
    return set_status(task_id, TaskStatus.PAUSED, **kw)


def complete(task_id: str, result: str = "", **kw) -> Task | None:
    return set_status(task_id, TaskStatus.COMPLETED, result=result, **kw)


def fail(task_id: str, error: str = "", **kw) -> Task | None:
    return set_status(task_id, TaskStatus.FAILED, error=error, **kw)


def cancel(task_id: str, **kw) -> Task | None:
    return set_status(task_id, TaskStatus.CANCELLED, **kw)


def skip(task_id: str, reason: str = "", **kw) -> Task | None:
    return set_status(task_id, TaskStatus.SKIPPED, result=reason, **kw)


def reopen(task_id: str, status: str = TaskStatus.PENDING) -> Task | None:
    """The ONLY way out of a terminal state. Explicit, never automatic."""
    if status in TERMINAL:
        status = TaskStatus.PENDING
    return set_status(task_id, status, force=True)


def set_progress(task_id: str, progress: float, notify: bool = True) -> Task | None:
    task = get(task_id)
    if task is None:
        return None
    out = _apply(task, task.status, progress=progress)
    if notify:
        sync.notify("tasks", session_id=task.session_id, task_id=task.id,
                    event="progress")
    return out


def set_dependencies(task_id: str, dependencies, *, task: Task | None = None,
                     notify: bool = True) -> Task | None:
    """Rewrite one task's `dependencies`. THE writer for that column.

    Dependencies are *structure*, not state, so this is deliberately **not** routed
    through `_apply()` and carries no terminal guard: `_apply`'s refusal protects a
    settled row's *status*, and a caller that must protect a settled row's *edges*
    owns a different rule (`core.dag.validate.validate_mutation` is one, and it
    refuses before reaching this function). Ordering a plan is not the same act as
    claiming to have run it.

    ⚠️ `task=` exists so a caller holding the row pays no extra read — the pattern
    `ready(session_id, tasks=None)` and `blockers(task, tasks=None)` already use in
    this module, and the reason `sync_list()` can delegate here inside its own
    `db.batch()` without adding a query per item. A self-dependency is dropped
    rather than refused, exactly as `_resolve_deps` drops one: a node that waits for
    itself never becomes ready, and `ready()` would report that as *blocked* forever
    with nothing to point at.
    """
    if not task_id:
        return None
    row = task if task is not None else get(task_id)
    if row is None:
        return None
    want = [str(d) for d in (dependencies or []) if str(d) and str(d) != row.id]
    if want == list(row.dependencies):
        return row
    db.exe("UPDATE agent_tasks SET dependencies=?, updated_at=? WHERE id=?",
           (_jdump(want), _now(), row.id))
    row.dependencies = want
    if notify:
        sync.notify("tasks", session_id=row.session_id, task_id=row.id,
                    event="dependencies")
    return row


def cancel_open(session_id: str, reason: str = "") -> int:
    """Cancel every still-open task of a session (Ctrl+C / stop_agent).

    Terminal tasks are untouched by the WHERE clause, so this cannot undo work.
    """
    if not session_id:
        return 0
    marks = ",".join("?" * len(OPEN))
    rows = db.qall(
        f"SELECT id FROM agent_tasks WHERE session_id=? AND status IN ({marks})",
        (str(session_id), *sorted(OPEN)),
    )
    if not rows:
        return 0
    with db.batch():
        db.exe(
            f"UPDATE agent_tasks SET status=?, completed_at=?, error=?,"
            f" updated_at=?"
            f" WHERE session_id=? AND status IN ({marks})",
            (TaskStatus.CANCELLED, _now(), _text(reason), _now(), str(session_id),
             *sorted(OPEN)),
        )
    sync.notify("tasks", session_id=session_id, event="cancelled")
    return len(rows)


def delete_session_tasks(session_id: str) -> int:
    """Wipe a session's checklist (`/clearhistory`, an explicit reset)."""
    if not session_id:
        return 0
    rows = db.qall("SELECT id FROM agent_tasks WHERE session_id=?",
                   (str(session_id),))
    if rows:
        db.exe("DELETE FROM agent_tasks WHERE session_id=?", (str(session_id),))
        sync.notify("tasks", session_id=session_id, event="cleared")
    return len(rows)


# ── Checkpoints ───────────────────────────────────────────────────────────────
#
# A checkpoint answers ONE question: if we stopped right now, where exactly were
# we inside this task? It is a plain dict in `agent_tasks.checkpoint`, and the
# structured half uses three reserved keys:
#
#   steps  : [{"name": str, "status": "completed"|"running"|"pending",
#              "at": iso8601, "destructive": bool, "detail": str}, …]
#   note   : free text the caller wants a human to read on resume
#   stopped: {"at": iso8601, "reason": str} — set when a stop is OBSERVABLE
#
# Any other key a caller adds is preserved untouched: `save_checkpoint` merges.
#
# ⚠️ WHY STEPS ARE WRITTEN AS THEY HAPPEN, NOT AT EXIT
# ─────────────────────────────────────────────────────
# A `SIGKILL`, a pulled plug or an OOM kill runs no handler, so any design that
# flushes progress "on the way out" loses precisely the crash it was built for.
# Every `record_step()` is therefore its own committed write. `interrupt()` adds
# only the *reason*, which is a nicety — the durable fact is already on disk.
#
# The corollary is the signal Task 3 recovers from: a task still marked RUNNING
# in a fresh process was, by definition, running when the last one died.

CP_STEPS = "steps"
CP_NOTE = "note"
CP_STOPPED = "stopped"
# Written by `core/recovery.adopt()` when a task is picked back up: {"at", "attempt",
# "reason", "checks"}. Stored rather than derived because "this task was resumed
# after a crash" is a fact about history that nothing else on disk records.
CP_RECOVERED = "recovered"
# Written by `core/workflow/runner.instantiate()` (Task 37): which workflow run this
# task belongs to, and which node of that workflow it IS.
#
# ⚠️ THIS IS THE ONLY DURABLE LINK FROM A NODE ID TO A TASK ID, and it lives on the
# task rather than in `exec_workflows.state` on purpose. The graph speaks node ids
# (`build`, `test`) while `agent_tasks.dependencies` holds task ids, so a remap
# happens exactly once, at creation. Keeping the reverse map in the run row would
# put it under `execstate.MAX_STATE_CHARS`, which degrades an over-long blob to
# `"{}"` — and a workflow that lost its node names would resume by re-running work
# it had already finished, silently, which is the one thing Task 42 forbids. On the
# row it describes, the mapping cannot be truncated away and cannot outlive it.
CP_WORKFLOW = "workflow"
CP_NODE = "node"
# Written by `core/dag/store.py` (Phase D1) alongside the two above: the node's
# `kind` (an extensible type label the DAG core itself never branches on) and its
# `resource` (the mutual-exclusion token the scheduler honours).
#
# ⚠️ ON THE ROW FOR `CP_NODE`'s REASON, not for convenience. Both are properties of
# ONE node, and the alternative — a table of them in `exec_workflows.state` — sits
# under `execstate.MAX_STATE_CHARS`, which degrades an over-long blob to `"{}"`. A
# graph that lost its resource tokens would not fail: it would quietly run two
# nodes that were declared as never allowed to overlap, which is the failure mode
# with no error message. `kind` is likewise the only durable record of what a node
# IS — a title is prose a model may rewrite.
CP_KIND = "kind"
CP_RESOURCE = "resource"

STEP_PENDING = "pending"
STEP_RUNNING = "running"
STEP_COMPLETED = "completed"
STEP_FAILED = "failed"

# A step that may have changed something outside the process. Recovery must ask
# before repeating one of these (rule 21: never blindly retry an uncertain
# destructive operation) — the flag is what makes that question possible.
DESTRUCTIVE_TOOLS = frozenset((
    "write_file", "multi_edit_files", "delete_file", "run_command",
    "run_file_op", "convert_file",
))


def _steps_of(checkpoint: dict) -> list[dict]:
    raw = checkpoint.get(CP_STEPS)
    return [s for s in raw if isinstance(s, dict)] if isinstance(raw, list) else []


def _step_key(name: str) -> str:
    return " ".join(str(name or "").split()).lower()


def plan_steps(task_id: str, steps, *, notify: bool = True) -> Task | None:
    """Declare the sub-steps of a task up front, all PENDING.

    Optional — `record_step()` appends on the fly for callers that discover their
    steps as they go. Declaring them first is what makes "remaining" meaningful
    before the work starts.
    """
    rows: list[dict] = []
    seen: set[str] = set()
    for raw in steps or ():
        name = _text(raw.get("name") if isinstance(raw, dict) else raw, MAX_TITLE)
        if not name or _step_key(name) in seen:
            continue
        seen.add(_step_key(name))
        entry = {"name": name, "status": STEP_PENDING, "at": ""}
        if isinstance(raw, dict):
            if raw.get("destructive"):
                entry["destructive"] = True
            if raw.get("detail"):
                entry["detail"] = _text(raw["detail"], MAX_TITLE)
        rows.append(entry)
    return save_checkpoint(task_id, {CP_STEPS: rows}, notify=notify)


def record_step(task_id: str, name: str, status: str = STEP_COMPLETED, *,
                detail: str = "", destructive: bool | None = None,
                notify: bool = False) -> Task | None:
    """Move one sub-step of *task_id* to *status*, appending it if it is new.

    Matched on a normalised name, like `sync_list` matches titles: the caller
    that starts a step and the caller that finishes it are often two different
    code paths, and neither should have to carry an index.

    `notify` defaults to **False**. Sub-steps fire far more often than tasks do,
    and a redraw per tool call is the duplicate-panel problem all over again —
    the CLI fingerprint deliberately ignores checkpoints for the same reason.
    """
    task = get(task_id)
    if task is None or not str(name or "").strip():
        return None
    steps = _steps_of(task.checkpoint)
    key = _step_key(name)
    now = _now()
    for step in steps:
        if _step_key(step.get("name", "")) != key:
            continue
        # ⚠️ A finished sub-step is never walked backwards, for the same reason a
        # terminal task is not: the model (or a retry) re-reports stale state.
        if step.get("status") in (STEP_COMPLETED, STEP_FAILED) \
                and status not in (STEP_COMPLETED, STEP_FAILED):
            return task
        step["status"] = status
        step["at"] = now
        if detail:
            step["detail"] = _text(detail, MAX_TITLE)
        if destructive is not None:
            step["destructive"] = bool(destructive)
        break
    else:
        entry = {"name": _text(name, MAX_TITLE), "status": status, "at": now}
        if detail:
            entry["detail"] = _text(detail, MAX_TITLE)
        if destructive:
            entry["destructive"] = True
        steps.append(entry)
    return save_checkpoint(task_id, {CP_STEPS: steps}, notify=notify)


def note_tool(session_id: str, tool: str, *, detail: str = "",
              ok: bool | None = None) -> Task | None:
    """Record a tool call as a sub-step of whatever task is RUNNING.

    This is the automatic half: the agent loops call it for every tool, so the
    checkpoint fills in without the model having to describe its own progress
    (it does not reliably do so). No running task → nothing recorded, which is
    the right answer for a turn that never planned anything.
    """
    if not session_id or not tool:
        return None
    task = current(session_id)
    if task is None or task.status != TaskStatus.RUNNING:
        return None
    status = STEP_RUNNING if ok is None else (
        STEP_COMPLETED if ok else STEP_FAILED)
    return record_step(task.id, tool, status, detail=detail,
                       destructive=tool in DESTRUCTIVE_TOOLS)


def checkpoint_view(task_id_or_task) -> dict:
    """`{completed, current, remaining, …}` — the resume-point summary.

    Derived, never stored: a stored copy would be a second declaration of the
    same fact and would drift from `steps` the first time one was written
    without the other.
    """
    task = (task_id_or_task if isinstance(task_id_or_task, Task)
            else get(str(task_id_or_task or "")))
    if task is None:
        return {"completed": [], "current": "", "remaining": [], "failed": [],
                "note": "", "stopped": {}, "steps": [], "total": 0,
                "destructive_pending": False, "recovered": {}}
    steps = _steps_of(task.checkpoint)
    completed = [s["name"] for s in steps if s.get("status") == STEP_COMPLETED]
    failed = [s["name"] for s in steps if s.get("status") == STEP_FAILED]
    running = [s for s in steps if s.get("status") == STEP_RUNNING]
    remaining = [s["name"] for s in steps if s.get("status") == STEP_PENDING]
    return {
        "completed": completed,
        "current": running[0].get("name", "") if running else "",
        "remaining": remaining,
        "failed": failed,
        "note": _text(task.checkpoint.get(CP_NOTE), MAX_TEXT),
        "stopped": task.checkpoint.get(CP_STOPPED) or {},
        "recovered": task.checkpoint.get(CP_RECOVERED) or {},
        "steps": steps,
        "total": len(steps),
        # ⚠️ Task 3 / rule 21 read this: a destructive step that was RUNNING when
        # we stopped may or may not have landed. Nobody may retry it silently.
        "destructive_pending": any(s.get("destructive") for s in running),
    }


def stale_running(cwd: str = "", older_than: float = STALE_AFTER,
                  limit: int = 50) -> list[Task]:
    """Tasks a worker is still holding that nothing has touched for *older_than* seconds.

    Task 25 §6 — worker recovery — asks exactly one question of the task model:
    *"which tasks did a worker die holding?"* It lives here, beside
    `unfinished_sessions()`, for the same reason that one does: what counts as
    stuck is a property of the task model, and a `SELECT … WHERE status='running'`
    written inside `core/recovery/` would be a second declaration of it — one that
    keeps working and silently stops matching the day a ninth status is added.

    ⚠️ `HELD` — RUNNING **AND QUEUED** — NEVER PAUSED, and those are three
    different facts rather than degrees of one. QUEUED has a single writer,
    `core/dag/store.claim()`, where the status write *is* the lock: `pending` →
    `queued` comes first precisely so `dispatchable` goes False for every other
    scheduler, and `schedule._start()` marks RUNNING milliseconds later, in the
    pump's own thread. A process that dies inside that window used to leave a row
    no path could recover — `heartbeat()` writes only to a RUNNING row, so it beats
    nothing and this sweep never saw it; the status is no longer `pending`, so no
    scheduler re-takes it; and `release_interrupted()`/`unpause_run()` both key on
    PAUSED, so neither frees it. `_state_of` projects QUEUED onto READY, so the run
    sat at N-1/N forever while every surface rendered the node as healthy. Sweeping
    it is safe in the direction that matters: a legitimately-held QUEUED row is
    RUNNING within milliseconds, orders of magnitude under `STALE_AFTER`.
    PAUSED stays out for the opposite reason — `interrupt()` parks there and
    `/pause` is a hold a human chose, and handing recovery every deliberately
    paused chat is the repurposing of `/pause` Phase 8 is forbidden to do. What
    separates the two on disk is the `CP_STOPPED` checkpoint, which `interrupt()`
    writes and `pause()` never does.

    ⚠️ THE COMPARISON IS `last_heartbeat` FIRST, `updated_at` SECOND, AND THE ORDER
    MATTERS IN ONE DIRECTION ONLY. A long task that is genuinely working changes
    nothing about its row for minutes at a time — same status, same progress — so
    `updated_at` alone would call it stale and worker recovery would declare a
    healthy task interrupted. `heartbeat()` is what a live worker writes to say
    otherwise, so the freshest of the two is the honest answer. `created_at` is the
    last resort: migration 27 backfills `updated_at`, but a row written by something
    that bypassed `create()` would otherwise read as `''`, which sorts BELOW every
    cutoff and would make that row permanently, silently "stale". A QUEUED row is
    measured by `updated_at` alone in practice, since `heartbeat()` refuses to
    touch anything but RUNNING — which is the honest reading, because `queue()`
    stamped it at the moment of the claim.

    ⚠️ The comparison is on second-resolution timestamps, so `older_than=0` means
    "everything held" rather than "nothing" — a test can ask for that without
    inserting a sleep, and a caller cannot accidentally get an empty answer from a
    zero.
    """
    try:
        secs = max(0.0, float(older_than))
    except Exception:
        secs = STALE_AFTER
    cutoff = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - secs))
    seen = ("COALESCE(NULLIF(last_heartbeat,''), NULLIF(updated_at,''),"
            " NULLIF(created_at,''), '')")
    # `status IN (…)` still rides `idx_agent_tasks_live(status, updated_at)` —
    # SQLite serves an IN list as one range scan per value — so widening the seed
    # to `HELD` costs no extra scan on the startup path.
    params: list = [*HELD, cutoff]
    holds = ",".join("?" for _ in HELD)
    where = f"status IN ({holds}) AND {seen}<=?"
    if cwd:
        where += " AND session_id IN (SELECT id FROM task_sessions WHERE cwd=?)"
        params.append(_project_key(cwd))
    params.append(_int(limit, 50))
    rows = db.qall(
        f"SELECT * FROM agent_tasks WHERE {where}"
        f" ORDER BY {seen} ASC, rowid ASC LIMIT ?",
        tuple(params),
    )
    return [Task.from_row(r) for r in rows]


def heartbeat(task_id: str, *, worker_id: str = "",
              execution_id: str = "") -> bool:
    """Say "this task is still being worked on" — Task 24 §6's liveness signal.

    ⚠️ **A STALE HEARTBEAT IS EVIDENCE, NOT A VERDICT**, which is why this writes a
    timestamp and nothing else. §24.6 is explicit that recovery must not assume
    failure merely because a beat is late — a machine that was asleep, a worker
    blocked on a 10-minute `docker build`, and a process that died all produce the
    same silence here. `core/recovery/crash.py` combines it with the owning
    instance's pid before deciding anything.

    Deliberately NOT routed through `_apply()`: a heartbeat is not a transition, and
    passing one through the status writer would fire the `sync.notify("tasks")` that
    repaints both surfaces — once per beat, forever, for a task that did not change.
    It is the one write allowed to touch `updated_at` from outside `_apply`, and it
    writes the same stamp to both columns so the two can never disagree about when
    the row was last known good.

    Cheap and total: one UPDATE, no read-back, `False` on anything unexpected.
    """
    if not str(task_id or ""):
        return False
    now = _now()
    sets = {"last_heartbeat": now, "updated_at": now}
    if worker_id:
        sets["worker_id"] = _text(worker_id, MAX_TITLE)
    if execution_id:
        sets["execution_id"] = _text(execution_id, MAX_TITLE)
    try:
        cols = ", ".join(f"{k}=?" for k in sets)
        db.exe(
            f"UPDATE agent_tasks SET {cols} WHERE id=? AND status=?",
            (*sets.values(), str(task_id), TaskStatus.RUNNING),
        )
        return True
    except Exception:
        return False


def claim(task_id: str, *, worker_id: str = "", execution_id: str = "") -> Task | None:
    """Record WHICH worker and WHICH execution a running task belongs to (§24.3).

    The two identifiers a recovery report needs in order to say more than "a task
    was interrupted": the worker is what `recover_workers()` groups by, and the
    execution id is the link to the `exec_commands` / `exec_tool_calls` row that
    holds the operation itself — which is how a review panel can say *"a git_commit
    was in flight"* rather than *"task 3 stopped"*.

    Goes through `_apply()` (with the task's existing status, so it is not a
    transition) precisely so `updated_at` bookkeeping stays in one place.
    """
    task = get(task_id)
    if task is None:
        return None
    return _apply(task, task.status, worker_id=worker_id,
                  execution_id=execution_id, beat=True)


def interrupt(session_id: str, reason: str = "interrupted", *,
              pause: bool = True) -> list[Task]:
    """Stamp every HELD task of *session_id* as stopped, and park it.

    Called on Ctrl+C, on `stop_agent`, and on a clean shutdown. Best-effort by
    design: the sub-step trail is already durable, so failing to reach this only
    costs the *reason*, never the position.

    `pause=True` moves the task to PAUSED — an honest "started, not finished"
    that neither loses it nor claims it succeeded. PAUSED is open, so
    `unfinished_sessions()` still offers it for recovery.

    ⚠️ THE FILTER IS `HELD`, NOT RUNNING, AND IT HAS TO MATCH `stale_running()`'s.
    That sweep is what seeds `crash.recover_workers()`, and this is the only writer
    of `CP_STOPPED` — the one durable mark that separates *a crash abandoned this*
    from *a human paused it*, and therefore the only thing
    `dag.store.release_interrupted()` will act on. A row the seed found and this
    filter then skipped would be reported as interrupted and parked by nothing:
    still QUEUED, still unrecoverable, and now with a recovery pass on record
    saying it was handled.
    """
    out: list[Task] = []
    stamp = {"at": _now(), "reason": _text(reason, MAX_TITLE)}
    for task in list_tasks(session_id):
        if task.status not in HELD:
            continue
        steps = _steps_of(task.checkpoint)
        for step in steps:
            # A sub-step caught mid-flight is left visible as the current one —
            # that IS where execution stopped. It is not marked completed.
            if step.get("status") == STEP_RUNNING:
                step["interrupted"] = True
        updated = save_checkpoint(task.id, {CP_STOPPED: stamp, CP_STEPS: steps},
                                  notify=False)
        if pause:
            updated = set_status(task.id, TaskStatus.PAUSED, notify=False)
        out.append(updated or task)
    if out:
        touch_session(session_id)
        sync.notify("tasks", session_id=session_id, event="interrupt",
                    count=len(out))
    return out


def save_checkpoint(task_id: str, data: dict, *, merge: bool = True,
                    notify: bool = True) -> Task | None:
    """Persist per-task execution state.

    `merge=True` (the default) shallow-merges, so a caller recording one new
    step cannot blank the rest of the checkpoint by omission.
    """
    task = get(task_id)
    if task is None:
        return None
    payload_ = dict(task.checkpoint) if merge else {}
    if isinstance(data, dict):
        payload_.update(data)
    # ⚠️ `updated_at` too: a checkpoint save IS the row changing, and it is the one
    # write a long-running task performs most often. Omitting it here would leave a
    # task that is checkpointing steadily looking untouched to `stale_running()`.
    db.exe("UPDATE agent_tasks SET checkpoint=?, updated_at=? WHERE id=?",
           (_jdump(payload_), _now(), task.id))
    if notify:
        sync.notify("tasks", session_id=task.session_id, task_id=task.id,
                    event="checkpoint")
    return get(task.id)


def load_checkpoint(task_id: str) -> dict:
    task = get(task_id)
    return dict(task.checkpoint) if task else {}


def clear_stopped(task_id: str, *, notify: bool = True) -> Task | None:
    """Drop the `CP_STOPPED` mark once the row it parked has been released.

    ⚠️ **IT LIVES HERE BECAUSE `interrupt()` IS THE ONLY WRITER OF THAT KEY.** The
    mark is the one durable difference between *a crash abandoned this* and *a human
    held this* (`dag.store._stopped_of()` is its only reader), so a releaser that
    left it behind would make the two indistinguishable **the next time round**: a
    node freed after a crash and later paused on purpose reads PAUSED *and*
    interrupted, and the following `release_interrupted()` un-pauses a hold somebody
    chose. Clearing it is therefore part of releasing, not a tidy-up.

    ⚠️ **`merge=False` IS LOAD-BEARING, AND SO IS RE-SEEDING FROM THE ROW.**
    `save_checkpoint(merge=True)` can only ever add a key, so it cannot express a
    deletion; a bare `merge=False` starts from `{}` and would blank `CP_STEPS`,
    `CP_NODE`, `CP_WORKFLOW` and `CP_KIND` — the resume position and the node's whole
    identity. So the surviving keys are read back and written whole.

    ⚠️ An absent mark writes **nothing at all**: `save_checkpoint()` stamps
    `updated_at`, which is exactly what `stale_running()` measures, so an idempotent
    call must not make a parked row look freshly touched.
    """
    task = get(task_id)
    if task is None:
        return None
    cp = dict(task.checkpoint) if isinstance(task.checkpoint, dict) else {}
    if CP_STOPPED not in cp:
        return task
    cp.pop(CP_STOPPED, None)
    return save_checkpoint(task_id, cp, merge=False, notify=notify)


# ── The merge (`update_todo`'s durable half) ──────────────────────────────────

def _normalize_items(items) -> list[dict]:
    """Accept the several shapes a model actually sends and produce one."""
    out: list[dict] = []
    for raw in items or []:
        if isinstance(raw, dict):
            title = raw.get("task") or raw.get("title") or raw.get("name") or ""
            title = " ".join(str(title).split())[:MAX_TITLE]
            if not title:
                continue
            deps = raw.get("dependencies") or raw.get("depends_on") or []
            out.append({
                "title": title,
                "status": normalize_status(raw.get("status")),
                "description": _text(raw.get("description") or raw.get("detail")),
                "priority": _int(raw.get("priority"), DEFAULT_PRIORITY),
                "deps": list(deps) if isinstance(deps, (list, tuple)) else [],
            })
        else:
            title = " ".join(str(raw).split())[:MAX_TITLE]
            if title:
                out.append({"title": title, "status": TaskStatus.PENDING,
                            "description": "", "priority": DEFAULT_PRIORITY,
                            "deps": []})
    return out


def _resolve_deps(spec, ordered: list[Task]) -> list[str]:
    """Turn what the model wrote into real task ids.

    Three accepted forms, because models pick whichever they feel like:
      * a 1-based position in the list it just sent ("depends on step 2")
      * a task title
      * an actual task id (the workflow runner uses these)
    Anything unresolvable is dropped — a bad reference must not block a plan.
    """
    by_id = {t.id: t for t in ordered}
    by_title = {_title_key(t.title): t for t in ordered}
    out: list[str] = []
    for item in spec or []:
        target = None
        if isinstance(item, bool):
            continue
        if isinstance(item, int):
            idx = item - 1
            if 0 <= idx < len(ordered):
                target = ordered[idx]
        else:
            text = str(item).strip()
            if text in by_id:
                target = by_id[text]
            elif _title_key(text) in by_title:
                target = by_title[_title_key(text)]
            elif text.isdigit():
                idx = int(text) - 1
                if 0 <= idx < len(ordered):
                    target = ordered[idx]
        if target is not None and target.id not in out:
            out.append(target.id)
    return out


def _renumber(ordered: list[Task]) -> None:
    """Write `seq` to match the merged order. Only rows whose seq actually moved
    are touched, so a no-op `update_todo` costs zero writes.

    `updated_at` moves with it, because the rule that keeps `stale_running()`
    trustworthy is "every write to this table stamps it" — an exception for one
    column is how the next writer learns there are exceptions.
    """
    now = _now()
    for i, t in enumerate(ordered):
        if t.seq != i:
            db.exe("UPDATE agent_tasks SET seq=?, updated_at=? WHERE id=?",
                   (i, now, t.id))
            t.seq = i


def sync_list(session_id: str, items, *, notify: bool = True) -> list[Task]:
    """Merge the model's checklist into the durable one and return the result.

    The merge rules, in the order they apply:

      1. Incoming items are matched to existing tasks by normalised title (with
         duplicate titles matched by occurrence, so "run tests" twice stays two
         tasks).
      2. ⚠️ A matched task that is already TERMINAL keeps its status. The model
         re-sends stale statuses constantly; honouring them is what used to send
         progress backwards.
      3. A matched OPEN task takes the incoming status (and description/priority
         when the model supplied one).
      4. An unmatched incoming item is created.
      5. ⚠️ An existing task the model dropped is deleted only if it is OPEN.
         Terminal tasks survive a replanned list and are renumbered to the front
         in their previous order.
    """
    if not session_id:
        return []
    incoming = _normalize_items(items)
    existing = list_tasks(session_id)

    # 1. Index existing by title, preserving duplicates as a queue.
    buckets: dict[str, list[Task]] = {}
    for t in existing:
        buckets.setdefault(_title_key(t.title), []).append(t)

    matched: set[str] = set()
    plan: list[tuple[Task | None, dict]] = []
    for item in incoming:
        bucket = buckets.get(_title_key(item["title"]))
        found = None
        while bucket:
            candidate = bucket.pop(0)
            if candidate.id not in matched:
                found = candidate
                break
        if found is not None:
            matched.add(found.id)
        plan.append((found, item))

    survivors = [t for t in existing if t.id not in matched and t.is_terminal]
    doomed = [t for t in existing if t.id not in matched and not t.is_terminal]

    ordered: list[Task] = []
    with db.batch():
        for task in doomed:                                        # rule 5
            db.exe("DELETE FROM agent_tasks WHERE id=?", (task.id,))

        for task, item in plan:
            if task is None:                                       # rule 4
                ordered.append(create(
                    session_id, item["title"], description=item["description"],
                    status=item["status"], priority=item["priority"],
                    seq=len(survivors) + len(ordered), notify=False))
                continue
            if task.is_terminal:                                   # rule 2
                ordered.append(task)
                continue
            fresh = task                                           # rule 3
            if item["status"] != task.status:
                fresh = _apply(
                    task, item["status"],
                    bump_attempt=item["status"] == TaskStatus.RUNNING,
                ) or task
            updates, params = [], []
            if item["description"] and item["description"] != fresh.description:
                updates.append("description=?")
                params.append(item["description"])
            if item["priority"] != fresh.priority:
                updates.append("priority=?")
                params.append(item["priority"])
            if updates:
                updates.append("updated_at=?")
                params.append(_now())
                params.append(fresh.id)
                db.exe(f"UPDATE agent_tasks SET {', '.join(updates)} WHERE id=?",
                       tuple(params))
                fresh = get(fresh.id) or fresh
            ordered.append(fresh)

        merged = survivors + ordered
        _renumber(merged)

        # Dependencies resolve against the FINAL order, so a "step 2" reference
        # means step 2 of the list the user is about to see. `set_dependencies` is
        # the one writer of that column; `task=`/`notify=False` keep this loop at
        # zero extra queries and one notification for the whole merge.
        for (_task, item), fresh in zip(plan, ordered, strict=False):
            if not item["deps"]:
                continue
            resolved = _resolve_deps(item["deps"], merged)
            set_dependencies(fresh.id, resolved, task=fresh, notify=False)

    touch_session(session_id)
    if notify:
        sync.notify("tasks", session_id=session_id, event="synced")
    return list_tasks(session_id)


# ── Aggregate counters (Task 28) ──────────────────────────────────────────────

def stats() -> dict:
    """Task-queue counters for `/api/health` and the metrics surface.

    THE one declaration of "how is the task queue doing". `summary()` above
    answers it for ONE session and is what the CLI panel renders; this answers it
    for the whole database, which is a different question and the only one a
    health endpoint can ask. A `SELECT status, COUNT(*)` written inside
    `server/routes.py` would be a second declaration of the vocabulary — and the
    one that keeps working while silently never counting a ninth status.

    ⚠️ COUNTERS ONLY — never a title, a description, a goal, a result, an error,
    a session id or a `cwd`. `/api/health` is reachable by anything that can
    reach the port, and a task title is a sentence the user wrote about their own
    work; `/api/tasks` is the authenticated route that may show it. This is the
    same rule `permissions.counters()` and `broker.stats()` already follow.

    ⚠️ `by_status` is ZERO-FILLED over `ALL_STATUSES`. A status absent from the
    dict is indistinguishable from a status this build has never heard of, and a
    reader that renders `by_status.get("queued")` would print nothing at all for
    an empty queue rather than `0` — the same "absent is not zero" discipline
    `execstate._digest()` and `llm/capabilities` are built on.

    ⚠️ `stale` is `stale_running()`'s answer, never a second
    `WHERE status IN (…) AND …`. What counts as stuck is that function's
    declaration — the `HELD` set and the `last_heartbeat` → `updated_at` →
    `created_at` fallback its docstring exists to explain — and a health endpoint
    that re-derived it would disagree with worker recovery about which tasks are
    lost. It is a *sample*:
    `STALE_SAMPLE` bounds it, `stale_sample` reports the bound, so a reader can
    tell "200 stale tasks" from "at least 200".

    ⚠️ TOTAL. Every read is guarded and a failure answers with the empty shape,
    because a health endpoint that raises is the one endpoint you cannot use to
    diagnose the raise. `/api/health`'s own `_section()` would catch it and
    report the section as an error, which is honest but strictly less useful than
    the counters that still worked.
    """
    out: dict = {
        "tasks": 0,
        "by_status": dict.fromkeys(ALL_STATUSES, 0),
        "open": 0,
        "settled": 0,
        "running": 0,
        "checkpointed": 0,
        "stale": 0,
        "stale_after": STALE_AFTER,
        "stale_sample": STALE_SAMPLE,
        "sessions": 0,
        "sessions_by_status": dict.fromkeys(
            (SESSION_ACTIVE, SESSION_DONE, SESSION_ABANDONED), 0),
        "unfinished_sessions": 0,
    }

    # ⚠️ EVERY GUARD BELOW ANSWERS WITH A VALUE, NEVER WITH `pass`. That is not a
    # lint accommodation: `stats()` is exempted for BLE001 (a broken counter may
    # not break a health read) and deliberately NOT for S110, so each failure has
    # to name what it degrades to — an empty row set, a zero — right where it
    # happens. A bare `pass` five times over would leave the caller unable to tell
    # "no tasks" from "the count did not run", which is the one distinction a
    # health payload is read to make.
    def _rows(sql: str) -> list:
        try:
            return db.qall(sql)
        except Exception:
            return []

    def _count(sql: str) -> int:
        try:
            row = db.qone(sql)
        except Exception:
            return 0
        return int((row or {}).get("n") or 0)

    def _len(fn) -> int:
        try:
            return len(fn(limit=STALE_SAMPLE))
        except Exception:
            return 0

    for row in _rows("SELECT status, COUNT(*) AS n FROM agent_tasks"
                     " GROUP BY status"):
        # ⚠️ A status this build does not know is reported UNDER ITS OWN NAME,
        # never folded through `normalize_status()` into PENDING. That mapping
        # exists so a model's typo cannot fail a tool call; using it here would
        # make a health payload quietly relabel rows and hide the schema drift
        # this section is read to reveal.
        word = str(row["status"] or "") or "?"
        out["by_status"][word] = out["by_status"].get(word, 0) + int(row["n"])
    counts = out["by_status"]
    out["tasks"] = sum(counts.values())
    out["settled"] = sum(counts.get(s, 0) for s in TERMINAL)
    out["open"] = out["tasks"] - out["settled"]
    out["running"] = counts.get(TaskStatus.RUNNING, 0)

    out["checkpointed"] = _count("SELECT COUNT(*) AS n FROM agent_tasks"
                                 " WHERE checkpoint NOT IN ('', '{}')")
    out["stale"] = _len(stale_running)

    for row in _rows("SELECT status, COUNT(*) AS n FROM task_sessions"
                     " GROUP BY status"):
        word = str(row["status"] or "") or "?"
        table = out["sessions_by_status"]
        table[word] = table.get(word, 0) + int(row["n"])
    out["sessions"] = sum(out["sessions_by_status"].values())
    out["unfinished_sessions"] = _len(unfinished_sessions)
    return out
