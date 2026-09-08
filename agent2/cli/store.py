# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/cli/store.py
───────────────────
Everything the CLI reads back from, or writes to, `agent2.db`: memories, rules,
conversation history, the workspace root, and the handful of remembered settings
(`cli_last_model`, `cli_last_gemini_model`).

⚠️ NOTHING HERE OWNS ANY LOGIC.
Memories go through `agent2.core.memory`, rules through `agent2.core.rules`,
history through `agent2.core.context`, the workspace through
`agent2.core.workspace` — the SAME backends the Web UI uses. These are adapters
into the CLI's legacy in-memory shapes and nothing more. A raw `INSERT` into
`memories` here would skip the dedup and the `sync.notify("memories")` that
invalidates the cached system prompt, and the agent would run the rest of the
process on a prompt missing the memory it just saved.

Every optional backend degrades to a flag (`_CTX_OK`, `_WS_OK`, `_PIL_OK`), so
the CLI still starts — with history and workspace switching disabled — on a
machine where the DB cannot be opened.

Layer: env / models / state → store.
"""

from pathlib import Path

from agent2.cli.env import _CORE_OK, _DB_OK, _core_memory, _core_rules, _db_exe, _db_qall
from agent2.cli.models import DEFAULT_MODE, DEFAULT_MODEL, MODELS
from agent2.cli.state import S

# ── History (project-isolated, DB-backed via agent2.core.context) ──────────────
# History is stored per-conversation in agent2.db, tagged with the working
# directory. Starting Agent2 in a directory loads only that project's active
# chat. Cross-project chats are reachable only through /resume.
try:
    from agent2.core import context as _core_ctx
    _CTX_OK = _DB_OK
except Exception:
    _core_ctx = None
    _CTX_OK = False

# ── Workspace (centralized — agent2.core.workspace, the single source of truth) ─
# Switching the workspace here goes through the SAME manager the Web UI and the
# tool sandbox use, so a /workspace switch cancels running tasks and re-confines
# every filesystem tool to the new root.
try:
    from agent2.core import workspace as _core_ws
    from agent2.core.workspace import WorkspaceViolation as _WSViolation
    _WS_OK = _DB_OK
except Exception:
    _core_ws = None
    _WSViolation = Exception
    _WS_OK = False

# ── Personal Intelligence Layer (offline prompt enhancement) ───────────────────
# The same PIL the Web UI uses. We learn from the ORIGINAL user text, then send an
# optionally grammar-corrected / preference-enriched copy to the model while the
# CLI display + history keep the untouched original. All errors degrade to "do
# nothing", so a broken PIL can never break a chat turn.
try:
    from agent2.core import pil as _pil
    _PIL_OK = True
except Exception:
    _pil = None
    _PIL_OK = False


# ── Memories & rules ───────────────────────────────────────────────────────────

def load_mems() -> list:
    """Centralized memory list, mapped to the CLI's legacy shape.

    Scoped to this workspace by `core.memory` itself (Task 23), so `shared` is
    carried through rather than derived here — one project's `/memory` listing
    must be able to say which of its entries are global.
    """
    if _CORE_OK:
        try:
            return [{"id": m["id"], "content": m["content"],
                     "importance": m.get("importance", 5) or 5,
                     "tags": [t for t in (m.get("tags") or "").split(",") if t],
                     "shared": not str(m.get("project") or ""),
                     "created": m.get("created_at", "")}
                    for m in _core_memory.list_memories()]
        except Exception:
            pass
    return []


def save_mems(mems: list):
    pass  # DB-only; no JSON fallback


def add_mem(content: str, importance: int = 5, tags: list | None = None,
            *, shared: bool = False):
    content = (content or "").strip()
    if not content:
        return
    if _CORE_OK:
        try:
            # importance/tags are persisted now — they used to be accepted here
            # and silently dropped one call later.
            _core_memory.add_memory(content, importance, tags, shared=shared)
        except Exception:
            pass


def load_rules() -> list:
    """Active custom rules via the centralized backend (empty if unavailable)."""
    if _CORE_OK:
        try:
            return _core_rules.list_rules(active_only=True)
        except Exception:
            pass
    return []


# ── Conversation history ───────────────────────────────────────────────────────

def _msgs_to_history(rows: list) -> list:
    """Convert stored message rows into the CLI's in-memory history shape."""
    hist = []
    for r in rows:
        role = r.get("role")
        if role in ("user", "assistant"):
            hist.append({"role": role, "content": r.get("content", ""),
                         "ts": r.get("created_at", "")})
    return hist


def prompt_dir_name() -> str:
    """Short label for the directory the prompt is currently pointed at.

    Reads the workspace manager rather than Path.cwd() so `/workspace` switches
    are reflected immediately — the workspace root, not the process cwd, is what
    the tools are actually confined to.
    """
    path = None
    if _WS_OK:
        try:
            path = _core_ws.current().as_dict().get("path")
        except Exception:
            path = None
    try:
        p = Path(path) if path else Path.cwd()
        # A drive/filesystem root has an empty .name ("C:\\" → ""); show the root.
        return p.name or str(p)
    except Exception:
        return "?"


def last_chat_for_cwd() -> dict | None:
    """The conversation this project would continue, or None.

    A read-only probe: it never creates a row, and it is the SAME selection the
    loader uses, because it decides what the startup line *promises*. Advertising
    one conversation and then opening another is a lie the user can only catch by
    reading the transcript — so the rule lives once, in
    `core.context.last_session()`, and this is the CLI's adapter onto it.
    """
    if not _CTX_OK:
        return None
    return _core_ctx.last_session()


# ⚠️ ONE window, read by the loader AND the writer, and that is the whole point.
# It used to be two numbers: every loader took `[-60:]` while `save_history()`
# wrote `h[-100:]`, so loading a 100-message conversation and saying one word
# rewrote it as 61 — `save_history` DELETEs the chat's rows before re-INSERTing
# the window it was handed, so the 40 turns the loader never asked for were gone
# from the database, silently, with no error and nothing on screen.
#
# Harmless while loading was an opt-in `/load`. Continuity makes it happen on
# EVERY launch, which is how a "resume where you stopped" feature would have
# quietly become a "and forget the rest" feature. Load window == save window is
# pinned in both directions by test_continuity.py.
HISTORY_WINDOW = 200


def _history_for(chat: dict) -> list:
    """The stored message window of one chat, in the CLI's in-memory shape.

    ⚠️ The order comes from `core.context.MSG_ORDER`, never a local clause: a
    bare `ORDER BY created_at` returned this conversation **reversed**, because
    `save_history()` stamps the whole rewritten window with one second and the
    index it is served from is `DESC`. Reached only through
    `load_last_conversation()`, i.e. only after `_CTX_OK`.
    """
    try:
        rows = _db_qall(
            "SELECT role, content, created_at FROM messages "
            f"WHERE chat_id=? {_core_ctx.MSG_ORDER}", (chat["id"],))
    except Exception:
        return []
    return _msgs_to_history(rows)[-HISTORY_WINDOW:]


def load_last_conversation() -> list | None:
    """Pull the conversation this project stopped in back into the session.

    THE explicit path: `/load`, and `--continue` at launch. It never consults
    `core.context.auto_resume()` — an explicit ask is not governed by a default,
    which is why this function and `resume_last_conversation()` are two functions
    over one selection rule rather than one function with a flag. Binds `S.chat`
    so later turns append to that conversation instead of forking a new one.

    ⚠️ IT BINDS ONLY AFTER A NON-EMPTY READ. `S.chat` was being set before the
    read was inspected, so an empty or unreadable conversation left the session
    *bound to it* with no history — and the next `save_history()` DELETEs that
    chat's rows before writing the window it was given, i.e. an empty one. A
    failed load therefore erased the very conversation it failed to load. The
    honest answer to "nothing came back" is None, and a clean session.
    """
    chat = last_chat_for_cwd()
    if not chat:
        return None
    history = _history_for(chat)
    if not history:
        return None
    S.chat = dict(chat)
    return history


def resume_last_conversation() -> list | None:
    """Continuity at launch — the history to carry on with, or None to start clean.

    ⚠️ THE POLICY QUESTION IS ASKED HERE AND NOWHERE ELSE. `/load` and
    `--continue` are a human asking out loud and work whatever `AGENT2_RESUME`
    says; this is Agent2 deciding on their behalf, so it is the one path the
    policy governs — and since the default is `off`, the one path that normally
    returns None. Selection still comes from `core.context.last_session()`, so
    the policy can never change *which* conversation `/load` opens, only whether
    a launch opens one unasked.

    On success `S.chat` is bound and the caller may read its title for the line
    it prints; on None nothing was touched and the session is clean.
    """
    if not _CTX_OK:
        return None
    try:
        if not _core_ctx.auto_resume():
            return None
    except Exception:
        return None
    return load_last_conversation()


def bind_chat(chat_id: str) -> dict | None:
    """Point this session at an existing chat row (used by /resume).

    A setter rather than `S.chat.update(...)` at the call site, because S.chat is
    None until something is actually saved — mutating it in place would raise on
    a fresh session.
    """
    try:
        import agent2.database as _adb
        row = _adb.qone("SELECT * FROM chats WHERE id=?", (chat_id,))
    except Exception:
        row = None
    if row:
        S.chat = dict(row)
    return S.chat


def ensure_chat(model: str, mode: str) -> dict | None:
    """Bind (creating if needed) the DB conversation this session writes to.

    The chat row is created LAZILY on the first save rather than at startup, so
    launching the CLI and quitting without saying anything leaves no empty chat
    behind. The Web UI creates its session up front; the CLI does not.
    """
    if S.chat or not _CTX_OK:
        return S.chat
    try:
        S.chat = _core_ctx.new_chat(model, mode)
    except Exception:
        S.chat = None
    return S.chat


def save_history(h: list, model: str = "", mode: str = ""):
    """Persist history to the current DB conversation (agent2.db only).

    ⚠️ The whole rewrite commits as ONE transaction. This runs after every turn
    and replaces the chat's message window, so the per-statement form did up to
    102 round trips — but the reason it is a `batch()` is CORRECTNESS, not speed:
    the DELETE committed on its own, so an INSERT that failed midway left the
    conversation permanently truncated to whatever had landed. Now the rewrite
    applies whole or not at all, and the previous window survives a failure.
    Same call as `pil.optimize._merge_phrases`.

    ⚠️ It writes `HISTORY_WINDOW`, the SAME number the loader reads. A larger
    write window is not generosity: this DELETEs first, so whatever the loader
    declined to read is what this deletes, and the two numbers disagreeing is a
    silent truncation on every resume.
    """
    if not _CTX_OK:
        return
    # Nothing said and nothing bound yet → don't create an empty chat row. This is
    # what keeps `agent2 --cli` + immediate /exit from littering the chat list.
    # An empty history WITH a bound chat still writes: that's /clearhistory.
    if not h and S.chat is None:
        return
    if not ensure_chat(model or DEFAULT_MODEL, mode or DEFAULT_MODE):
        return
    try:
        import uuid as _uuid

        import agent2.database as _adb
        cid = S.chat["id"]
        rows = [(str(_uuid.uuid4()), cid, m["role"], m.get("content", ""))
                for m in h[-HISTORY_WINDOW:] if m.get("role") in ("user", "assistant")]
        # Auto-title from first user message. Computed BEFORE the batch so the
        # title write joins the same transaction as the messages it describes.
        title = None
        if S.chat.get("title", "New Chat") == "New Chat":
            first_user = next((m["content"] for m in h if m.get("role") == "user"), "")
            if first_user:
                title = first_user.strip().replace("\n", " ")[:50]
        with _adb.batch():
            # Replace this chat's messages with the current window (simple + safe).
            _db_exe("DELETE FROM messages WHERE chat_id=?", (cid,))
            if rows:
                _adb.exemany(
                    "INSERT INTO messages(id, chat_id, role, content) VALUES(?,?,?,?)",
                    rows)
            _db_exe("UPDATE chats SET updated_at=datetime('now') WHERE id=?", (cid,))
            if title:
                _db_exe("UPDATE chats SET title=? WHERE id=?", (title, cid))
        # Only mirrored into the session object once the write actually committed.
        if title:
            S.chat["title"] = title
    except Exception:
        pass


# ── Remembered settings ────────────────────────────────────────────────────────

def load_last_model() -> str | None:
    """The model the user last used (persisted, shared settings table)."""
    if _DB_OK:
        try:
            from agent2.database import get_setting
            return get_setting("cli_last_model")
        except Exception:
            pass
    return None


def save_last_model(model_key: str) -> None:
    if _DB_OK and model_key:
        try:
            from agent2.database import set_setting
            set_setting("cli_last_model", model_key)
            # Remember the last built-in Gemini model separately so /keys can
            # restore it when un-pinning away from a custom provider.
            if model_key in MODELS:
                set_setting("cli_last_gemini_model", model_key)
        except Exception:
            pass


def load_last_gemini_model() -> str | None:
    """Best-effort recall of the last built-in Gemini model the user was on."""
    if _DB_OK:
        try:
            from agent2.database import get_setting
            m = (get_setting("cli_last_gemini_model") or "").strip()
            if m in MODELS:
                return m
        except Exception:
            pass
    return None
