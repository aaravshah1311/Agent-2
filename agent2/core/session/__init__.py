# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2.core.session
─────────────────────
Per-chat execution isolation + background task tracking + abort (sections
6, 7, 8, 9).

The bug this fixes
  Cancellation and the live TODO list used to be keyed by the Socket.IO `sid`
  alone (or a plain process global). So while Chat A was running, opening or
  running Chat B on the same connection shared A's cancel token and A's TODO
  state — B "continued A". Every runtime concern is now owned by a per-chat
  Session identified by (sid, chat_id):

    - cancel token   : a threading.Event, one per (sid, chat_id)
    - workspace id   : the workspace bound to this run
    - task record    : id, status, progress, logs, start/end time
    - tool state     : the live TODO list (was a tools.py global)
    - stream identity : task_id used to tag every streamed token (section 8)

  A brand-new session NEVER inherits another session's cancel token, tasks or
  TODO list (sections 6, 7, 11).

Everything here is thread-safe: the Web UI runs each turn in its own daemon
thread and multiple chats can run concurrently on one socket.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field

from agent2.core import logging as alog


@dataclass
class Task:
    """A single background run (one user turn). Section 7."""
    id: str
    session_key: str
    sid: str
    chat_id: str
    workspace_id: str | None
    status: str = "running"          # running | done | cancelled | error
    progress: str = ""
    logs: list[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None

    def log(self, line: str) -> None:
        self.logs.append(line)
        if len(self.logs) > 200:
            self.logs = self.logs[-200:]


@dataclass
class Session:
    """Everything a single chat owns at runtime. Section 6."""
    key: str
    sid: str
    chat_id: str
    workspace_id: str | None
    cancel: threading.Event = field(default_factory=threading.Event)
    task: Task | None = None
    todos: list[dict] = field(default_factory=list)   # was tools._TODO_STATE

    @property
    def stopped(self) -> bool:
        return self.cancel.is_set()

    def should_stop(self) -> bool:
        return self.cancel.is_set()


def _key(sid: str, chat_id: str) -> str:
    return f"{sid or '-'}::{chat_id or '-'}"


class SessionManager:
    """Process-wide registry of live per-chat sessions + their tasks."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sessions: dict[str, Session] = {}

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def open(self, sid: str, chat_id: str, workspace_id: str | None = None) -> Session:
        """Create (or reset) the session for (sid, chat_id) and start a Task.

        A fresh cancel token is always minted so a new turn never starts already
        cancelled and never shares a previous turn's token (section 6/9).
        """
        k = _key(sid, chat_id)
        with self._lock:
            sess = self._sessions.get(k)
            if sess is None:
                sess = Session(key=k, sid=sid, chat_id=chat_id,
                               workspace_id=workspace_id)
                self._sessions[k] = sess
            else:
                # Reuse the session record but mint a fresh cancel token so the
                # new turn is not affected by a prior stop (section 11: no
                # inherited execution state).
                sess.cancel = threading.Event()
                sess.workspace_id = workspace_id or sess.workspace_id
            task = Task(id=str(uuid.uuid4())[:8], session_key=k, sid=sid,
                        chat_id=chat_id, workspace_id=workspace_id)
            sess.task = task
        alog.session_open(sid, chat_id, workspace_id)
        alog.stream_owner(sid, chat_id, task.id)
        return sess

    def get(self, sid: str, chat_id: str) -> Session | None:
        with self._lock:
            return self._sessions.get(_key(sid, chat_id))

    def close(self, sid: str, chat_id: str, status: str = "done") -> None:
        with self._lock:
            sess = self._sessions.get(_key(sid, chat_id))
            if sess and sess.task and sess.task.status == "running":
                sess.task.status = status
                sess.task.ended_at = time.time()
        alog.session_close(sid, chat_id)

    # ── Cancellation / abort (section 9) ─────────────────────────────────────

    def cancel(self, sid: str, chat_id: str | None = None) -> int:
        """Set the cancel token for one chat, or ALL chats on this sid when
        chat_id is None (the Stop button only carries the sid). Returns the
        number of sessions signalled."""
        n = 0
        with self._lock:
            for sess in self._sessions.values():
                if sess.sid != sid:
                    continue
                if chat_id is not None and sess.chat_id != chat_id:
                    continue
                sess.cancel.set()
                if sess.task and sess.task.status == "running":
                    sess.task.status = "cancelled"
                    sess.task.ended_at = time.time()
                n += 1
        alog.session_cancel(sid, chat_id)
        return n

    def cleanup_sid(self, sid: str) -> None:
        """Drop every session for a disconnected socket (section 6/8)."""
        with self._lock:
            dead = [k for k, s in self._sessions.items() if s.sid == sid]
            for k in dead:
                s = self._sessions.pop(k, None)
                if s:
                    s.cancel.set()
        if dead:
            alog.session_cancel(sid, None)

    # ── Introspection ────────────────────────────────────────────────────────

    def active_tasks(self) -> list[Task]:
        with self._lock:
            return [s.task for s in self._sessions.values()
                    if s.task and s.task.status == "running"]

    def owns_stream(self, sid: str, chat_id: str, task_id: str) -> bool:
        """Section 8: a streamed token is only valid if its (sid, chat_id, task)
        matches the session's CURRENT task. Mismatches must be ignored."""
        sess = self.get(sid, chat_id)
        ok = bool(sess and sess.task and sess.task.id == task_id
                  and not sess.cancel.is_set())
        if not ok:
            alog.stream_dropped("stale-or-foreign", sid=sid, chat=chat_id, task=task_id)
        return ok


# ── Module-level singleton ────────────────────────────────────────────────────
sessions = SessionManager()


# Wire workspace switching → cancel every task in the affected process
# (section 12: switching cancels tasks). Registered once at import time.
def _on_workspace_switch(_old_path, _new_ws) -> None:
    with sessions._lock:
        for sess in sessions._sessions.values():
            sess.cancel.set()
            if sess.task and sess.task.status == "running":
                sess.task.status = "cancelled"
                sess.task.ended_at = time.time()


# The import is guarded (a partially initialised install may not have it yet);
# the registration deliberately is NOT.
#
# `on_switch` is a one-line `list.append`, so the only realistic way it raises is
# a refactor renaming `manager` or `on_switch`. Swallowing that would silently
# drop the section-12 guarantee — tasks would keep running against the OLD root
# after a workspace switch, with nothing logged anywhere. Better to fail loudly
# at import than to lose task cancellation without a trace.
try:
    from agent2.core import workspace as _workspace
except Exception:                       # pragma: no cover - import-order guard
    _workspace = None                   # type: ignore[assignment]

if _workspace is not None:
    _workspace.manager.on_switch(_on_workspace_switch)
