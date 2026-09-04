# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2.core.context
────────────────────
Project-based conversation isolation.

A "project" is identified by the working directory (cwd) from which Agent2
was launched. Chats are tagged with their cwd at creation time. Loading
history for a session only returns chats belonging to that cwd.

Cross-project access is intentional only (e.g. /resume picker).
"""

import os
import uuid

from agent2.database import qall, qone, exe


def project_key(path: str | None = None) -> str:
    """Canonical form of a project directory — THE one declaration of that rule.

    ⚠️ Chats AND task sessions are looked up by this string, so two callers that
    normalise differently produce rows neither can find. That is not theoretical:
    task sessions first stored `workspace.root()` verbatim (`C:\\Users\\…`) while
    chats stored the normcased form (`c:\\users\\…`), so on Windows a recovery
    lookup by project matched nothing at all while every individual read looked
    perfectly healthy.

    Anything that stores or queries a `cwd` column goes through here.
    """
    return os.path.normcase(os.path.abspath(path or os.getcwd()))


def current_cwd() -> str:
    """Canonical cwd string used as the project key."""
    return project_key()


# ── Chat lifecycle ─────────────────────────────────────────────────────────────

def new_chat(model: str, mode: str, title: str = "New Chat") -> dict:
    """Create a new chat tagged with the current cwd. Returns the row."""
    cid = str(uuid.uuid4())
    exe(
        "INSERT INTO chats(id, title, model, mode, cwd, status) VALUES(?,?,?,?,?,?)",
        (cid, title, model, mode, current_cwd(), "active"),
    )
    return qone("SELECT * FROM chats WHERE id=?", (cid,))


def get_active_chat(cwd: str | None = None) -> dict | None:
    """Return the most-recently-updated active chat for this cwd, or None."""
    cwd = cwd or current_cwd()
    return qone(
        "SELECT * FROM chats WHERE cwd=? AND status='active' ORDER BY updated_at DESC LIMIT 1",
        (cwd,),
    )


def pause_chat(chat_id: str) -> None:
    """Mark a chat as paused (stops processing, preserves state).

    ⚠️ This is a CHAT-SESSION control and stays one. Pausing a chat does not
    resume, retry or recover anything — task recovery is driven entirely by the
    persisted task state in `core/tasks.py`, never by pause/resume.

    The one thing it does do is stamp the checkpoint of any task that was mid-
    flight, so "preserves state" is true of the task list too and not just of the
    message history. Best-effort: the pause itself must always succeed.

    ⚠️ The `noqa` is deliberate and scoped to that one line, NOT a file-wide
    failsafe allowance: the chat lifecycle is not a degrade-never-raise module,
    and a `chats` write that fails must still raise. Only the breadcrumb is
    optional — the sub-step trail is already durable (`core/tasks.py`), so losing
    this costs the *reason* for the stop, never the position.
    """
    exe("UPDATE chats SET status='paused' WHERE id=?", (chat_id,))
    try:
        from agent2.core import tasks as _tasks
        row = _tasks.active_session_for_chat(str(chat_id or ""))
        if row:
            _tasks.interrupt(str(row["id"]), "chat paused")
    except Exception:  # noqa: BLE001, S110 — see above
        pass


def resume_chat(chat_id: str) -> None:
    """Mark a chat as active again."""
    exe("UPDATE chats SET status='active', updated_at=datetime('now') WHERE id=?", (chat_id,))


# Chat rows carry a msg_count so callers can tell an empty chat from a used one
# without a second query per chat. Only user/assistant turns count: a chat whose
# only rows are tool_call/tool_result has nothing to show and reads as empty.
_MSG_COUNT = ("(SELECT COUNT(*) FROM messages m WHERE m.chat_id = chats.id "
              "AND m.role IN ('user','assistant')) AS msg_count")


def list_chats_for_cwd(cwd: str | None = None, limit: int | None = None) -> list[dict]:
    """All chats for this project, newest first. Used for project-scoped history."""
    cwd = cwd or current_cwd()
    sql = f"SELECT *, {_MSG_COUNT} FROM chats WHERE cwd=? ORDER BY updated_at DESC"
    params: tuple = (cwd,)
    if limit:
        sql += f" LIMIT {int(limit)}"
    return qall(sql, params)


def list_all_chats(limit: int | None = None) -> list[dict]:
    """All chats across all projects, newest first. Used by /resume cross-project."""
    sql = f"SELECT *, {_MSG_COUNT} FROM chats ORDER BY updated_at DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return qall(sql, ())


def get_or_create_chat(model: str, mode: str) -> dict:
    """Return the current project's active chat, creating one if none exists."""
    chat = get_active_chat()
    if chat:
        return chat
    return new_chat(model, mode)
