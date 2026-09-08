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

⚠️ THIS MODULE ALSO OWNS *CONTINUITY* — "carry on from where you stopped".
`last_session()` is the ONE declaration of which conversation that is, and
`resume_mode()` the ONE declaration of whether it happens by itself. Three
surfaces ask (the CLI at launch, the browser on first load, and `/load`), and a
second selection rule is the drift this module exists to prevent: the terminal
would carry on in one conversation while the browser opened another, each half
looking perfectly correct on its own — and in dual mode that is one process pair
over one database, so the two would then take turns overwriting each other's
`messages` window.

Three properties of that rule are load-bearing, and each is pinned:
  * **`status='active'` only.** `/pause` parks a conversation deliberately and
    tells the user to type `/resume`; resurrecting it at the next launch would
    make `/pause` mean nothing.
  * **It must have messages.** The Web UI creates its chat row up front, so the
    newest row for a project is routinely an empty one. Resuming that is not
    continuity, it is a no-op that *looks* like continuity — and it costs the
    real transcript, because it hides the conversation that does have content.
  * **Nothing here raises.** All three callers ask on a startup path, before
    there is a REPL or a bound socket to report an error to, so a DB hiccup
    degrades to "nothing to resume" and the surface starts clean.

⚠️ **CONTINUING IS OPT-IN — `resume_mode()` DEFAULTS TO `off`.** A launch starts
clean on both surfaces; `/load` and `--continue` (CLI) and the `load` palette
command (browser) are how a human asks for the last conversation back, and
`AGENT2_RESUME=last` is the opt-in for anyone who wants it on every launch.
Selection and policy stay two separate functions precisely because of that: the
explicit paths call `last_session()` **without** consulting `auto_resume()`, so
turning the automatic half off can never make the deliberate half unreachable.
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


# ── The order a conversation is in ─────────────────────────────────────────────
# ⚠️ ONE declaration, and the `rowid` tie-break is the entire reason it exists.
#
# `created_at` is SECOND-granular; `cli/store.save_history()` rewrites a whole
# window inside one batch, so every row of a resumed CLI conversation carries the
# SAME timestamp. And `idx_messages_chat_created` is `(chat_id, created_at DESC)`
# — so a bare `ORDER BY created_at` is served by walking that index BACKWARDS,
# and a fully-tied conversation comes back **reversed, end to end**, with no
# error and nothing on screen to point at. Measured, not theorised: a 152-message
# resume returned newest-first.
#
# `agent.build_context()` was fixed for exactly this and its docstring explains
# it — and then five more readers each spelled their own clause, four of which
# still had the bug. That is what a second declaration costs here, so there is
# now one: every reader of `messages` interpolates one of these two names.
MSG_ORDER = "ORDER BY created_at, rowid"                  # oldest first
MSG_ORDER_DESC = "ORDER BY created_at DESC, rowid DESC"   # newest first


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
#
# ⚠️ The EXPRESSION is the one declaration, not the projection: `list_chats_for_cwd`
# selects it as a column and `last_session()` filters on it, and those two must
# agree about what "has messages" means or the browser's chat list would show a
# count for a conversation continuity refuses to open.
_MSG_COUNT_EXPR = ("(SELECT COUNT(*) FROM messages m WHERE m.chat_id = chats.id "
                   "AND m.role IN ('user','assistant'))")
_MSG_COUNT = f"{_MSG_COUNT_EXPR} AS msg_count"


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


# ── Continuity — "carry on from where you stopped" ─────────────────────────────

RESUME_ENV = "AGENT2_RESUME"
RESUME_MODES = ("last", "off")
RESUME_DEFAULT = "off"


def resume_mode() -> str:
    """Whether a fresh surface continues by itself: ``off`` (default) or ``last``.

    ⚠️ **CONTINUING UNASKED IS OPT-IN, AND THE REVERSAL IS DELIBERATE.** This
    shipped as ``last``: every launch reopened whatever this directory had last
    been discussing, and printed a line saying so. That is the wrong default, for
    a reason the selection rules above cannot express — a new session is far more
    often a new question than the next sentence of the old one, and an unasked
    resume is not free. `cli/store.save_history()` DELETEs a chat's rows and
    re-INSERTs the window, so the first turn of a session that continued by
    accident rewrites the transcript the user did not mean to open. Starting clean
    costs a keystroke; continuing by accident costs the conversation.

    ⚠️ An unrecognised value falls back to `RESUME_DEFAULT` — now ``off``. The
    rule is unchanged and so is its direction: fall back to the answer that
    surprises nobody, exactly as `AGENT2_WEB_ROLE` falls to `viewer` and
    `AGENT2_CONTEXT_ISOLATION` to `project`. While this was on by default the safe
    end was ``last``, because a typo must not silently disable a feature somebody
    was relying on; now that it is off by default the safe end is ``off``, because
    a typo must not silently reopen a conversation nobody asked for. It is the
    default that moved, not the principle.

    ⚠️ **POLICY IS NOT SELECTION.** This governs the *automatic* resume and
    nothing else — `/load`, `--continue` and the `/resume` picker are the user
    asking out loud, and an explicit ask is never governed by a default. Reading
    this one flag as "may anything reopen a conversation" would make it quietly
    delete three commands, which is the failure mode rule 28 names: a plausible
    setting doing more than it says. That cuts both ways now: `off` being the
    default is precisely why the explicit paths must stay exempt, or the feature
    would be unreachable rather than opt-in.
    """
    raw = (os.environ.get(RESUME_ENV) or "").strip().lower()
    return raw if raw in RESUME_MODES else RESUME_DEFAULT


def auto_resume() -> bool:
    """True when a launching surface should continue the last conversation itself.

    False by default — see `resume_mode()`. `AGENT2_RESUME=last` is the opt-in
    that restores the behaviour this shipped with, for anyone who wants it.
    """
    return resume_mode() == "last"


def last_session(cwd: str | None = None) -> dict | None:
    """THE conversation "carry on where you stopped" means — or None.

    ⚠️ ONE declaration, three callers: the CLI at launch, `GET /api/chats/resume`
    on the browser's first paint, and `/load`. A second rule anywhere means the
    terminal continues one conversation while the tab opens another — and in dual
    mode both halves then rewrite the same `messages` window in turn.

    Three filters, each load-bearing and each pinned:
      * `status='active'` — a **paused** chat was parked on purpose and says
        "/resume to continue"; reviving it at the next launch makes `/pause` a
        no-op the user cannot see.
      * `{_MSG_COUNT_EXPR} > 0` — the Web UI creates its row up front, so the
        newest row for a project is routinely *empty*. Opening that is not
        continuity, and it costs the real transcript by hiding it.
      * this project only — `cwd` is the isolation boundary; crossing it is the
        `/resume` picker's job, and only ever because a human chose a row.

    Total by contract: every caller asks before there is a REPL or a socket to
    report to, so any failure degrades to "nothing to resume".
    """
    try:
        return qone(
            # Interpolates module constants only — never a caller's string.
            f"SELECT *, {_MSG_COUNT} FROM chats "
            f"WHERE cwd=? AND status='active' AND {_MSG_COUNT_EXPR} > 0 "
            "ORDER BY updated_at DESC LIMIT 1",
            (cwd or current_cwd(),),
        )
    except Exception:  # noqa: BLE001 — see above; startup path, no reporting surface
        return None


def resumable(cwd: str | None = None) -> dict | None:
    """`last_session()` as a payload a surface can render, or None.

    ⚠️ Carries the chat's *identity and size*, **never its text**: the transcript
    already has one reader (`GET /api/chats/<cid>`), and a second one shaped like
    a status probe is how message content ends up somewhere nobody audits.

    ⚠️ It answers *what*, not *whether* — `auto_resume()` answers that, and the
    two stay apart for the same reason `resume_mode()`'s docstring gives. Folding
    the policy in here would put the same boolean in two places inside one
    payload, and a client reading the wrong one would resume against a policy
    that says not to.
    """
    row = last_session(cwd)
    if not row:
        return None
    return {
        "id": str(row.get("id") or ""),
        "title": str(row.get("title") or "New Chat"),
        "model": str(row.get("model") or ""),
        "mode": str(row.get("mode") or ""),
        "messages": int(row.get("msg_count") or 0),
        "updated_at": str(row.get("updated_at") or ""),
    }
