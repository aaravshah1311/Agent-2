# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
Tests for **continuity** — "carry on from where you stopped" — and for the two
browser features that ship with it: the mid-turn prompt queue and the pinned run
indicator.

Run from the repo root:  python -m pytest .github/tests/test_continuity.py -v

Coverage
  Selection (`agent2/core/context/__init__.py`)
    - `last_session()` is ONE rule, and all three of its filters are load-bearing:
      active-only, must-have-messages, this-project-only
    - only `user`/`assistant` rows count as messages, and the counting EXPRESSION
      has one home (`_MSG_COUNT_EXPR`), shared with `list_chats_for_cwd`
    - it is total: a broken DB reads as "nothing to resume", never as an exception
    - ⚠️ continuing unasked is OPT-IN: `resume_mode()` defaults to `off`, anything
      unrecognised falls back to that default, and `AGENT2_RESUME=last` is the way
      back to the behaviour this shipped with
    - policy is not selection — the default may not disable `/load`, and
      `load_last_conversation()` is asserted (structurally) never to read it
    - `resumable()` carries identity and size, never message text

  The history window (`agent2/cli/store.py`)
    - ⚠️ the load window IS the save window. `save_history()` DELETEs before it
      re-INSERTs, so a loader that reads less than the writer writes truncates the
      stored conversation on every round trip — and continuity makes that round
      trip happen at every launch. Sabotage-verified: putting a literal back in
      either half turns `test_a_resume_round_trip_does_not_shrink_the_conversation`
      red.
    - a load that comes back empty binds nothing (binding first is how a failed
      load erased the conversation it failed to load)

  The order a conversation comes back in (`core.context.MSG_ORDER`)
    - ⚠️ found by LIVE verification, not by reading: a 152-message resume came
      back newest-first. `created_at` is second-granular, `save_history()` stamps
      a whole rewritten window with one second, and the index serving the read is
      `(chat_id, created_at DESC)` — so a bare `ORDER BY created_at` walks it
      backwards and reverses the conversation end to end
    - one clause, five readers, no local spelling
    - the same tie is why `edit_message` could delete the past, so its truncation
      predicate carries the `rowid` half too

  The CLI (`agent2cli.py`, read as source)
    - both loops bound context by `config.MAX_CTX_MESSAGES`, not a literal
    - continuity runs BEFORE crash recovery, so a recovery plan's chat wins
    - a launch starts CLEAN; `--continue` / `--load` takes the explicit loader and
      `--clear` still wins over both

  The web surface
    - `GET /api/chats/resume` resolves to its OWN view and is not swallowed by
      `/api/chats/<cid>` (a Werkzeug ranking rule this route depends on)
    - it reports `auto` and `resume` separately, and carries no message text

  The browser (`public/script.js` / `style.css` / `server/ui.py`, read as text)
    - the run bar is pinned OUTSIDE the scroll container
    - the composer is never disabled mid-turn; a message typed then is queued
    - the queue drains on `d.done` only, is dropped on stop and on chat switch
    - ONE declaration of "a turn starts here" (`_startTurn`)
    - the first paint asks the SERVER which conversation to continue, and treats
      any non-payload answer as "none"
    - it opens a FRESH chat unless the policy says otherwise, and `continueLast()`
      is the browser's `/load` — an explicit ask that ignores the policy

conftest.py redirects AGENT2_DB to a throwaway temp DB, so these tests never
touch the developer's real agent2.db.
"""

import inspect
import json
import os
import uuid
from pathlib import Path

import pytest

from agent2 import database as db
from agent2.cli import store as ST
from agent2.cli.state import S
from agent2.config import ROOT
from agent2.core import context as C

PROJ_A = C.project_key("/proj/continuity-a")
PROJ_B = C.project_key("/proj/continuity-b")


@pytest.fixture(autouse=True)
def _clean_slate():
    """Fresh chats/messages and a default resume policy for every test."""
    db.init_db()
    db.exe("DELETE FROM messages")
    db.exe("DELETE FROM chats")
    S.chat = None
    os.environ.pop("AGENT2_RESUME", None)
    yield
    S.chat = None
    os.environ.pop("AGENT2_RESUME", None)


def mkchat(cwd: str, *, title: str = "Chat", status: str = "active",
           updated: str = "2026-01-01 00:00:00") -> str:
    cid = str(uuid.uuid4())
    db.exe("INSERT INTO chats(id,title,model,mode,cwd,status) VALUES(?,?,?,?,?,?)",
           (cid, title, "2.5-flash", "pro", cwd, status))
    db.exe("UPDATE chats SET updated_at=? WHERE id=?", (updated, cid))
    return cid


def add_msgs(cid: str, pairs: list[tuple[str, str]], start: int = 0) -> None:
    for i, (role, content) in enumerate(pairs):
        db.exe("INSERT INTO messages(id,chat_id,role,content,created_at) "
               "VALUES(?,?,?,?,?)",
               (str(uuid.uuid4()), cid, role, content,
                f"2026-01-01 00:00:{(start + i) % 60:02d}.{start + i:06d}"))


def src(fn) -> str:
    return inspect.getsource(fn)


def _no_comments(text: str, marker: str) -> str:
    """`text` with its whole-line comments removed.

    These files DOCUMENT the bugs they fixed — "it used to be `ci.disabled=true`",
    "it hard-coded `history[-20:]`" — so a bare substring search finds the prose
    and passes (or fails) for the wrong reason. Stripping full-line comments keeps
    every assertion below about the CODE. Trailing comments are left alone: none of
    the patterns asserted here appears in one, and a naive strip would eat a `//`
    inside a URL string.
    """
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith(marker))


def js() -> str:
    return Path(ROOT, "public", "script.js").read_text(encoding="utf-8")


def fn_code(fn) -> str:
    """`src(fn)` with its own docstring and comments removed.

    `_code_only`'s reason, one scope down: `load_last_conversation()`'s docstring
    *names* `core.context.auto_resume()` in order to say it deliberately does not
    call it, so a bare substring search finds the prose and fails for the very
    invariant it is asserting. The docstring is located through `ast`, so nothing
    here depends on how it is quoted or indented.
    """
    import ast
    import textwrap
    text = src(fn)
    doc = ast.get_docstring(ast.parse(textwrap.dedent(text)).body[0], clean=False)
    if doc:
        text = text.replace(doc, "", 1)
    return _no_comments(text, "#")


def _code_only(path: Path) -> str:
    """A Python file with its docstrings AND comments removed.

    `_no_comments` is not enough for the ordering assertions: `agent.py`'s module
    docstring *quotes* the SQL clause it documents, so a bare substring search
    found the prose and failed for the wrong reason — the same trap one level up.
    Docstrings are located through `ast`, so nothing here depends on how they are
    quoted or indented.
    """
    import ast
    text = path.read_text(encoding="utf-8")
    drop: set[int] = set()
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)) and ast.get_docstring(node, clean=False):
            first = node.body[0]
            drop.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    kept = ["" if i + 1 in drop else line
            for i, line in enumerate(text.splitlines())]
    return _no_comments("\n".join(kept), "#")


def js_code() -> str:
    return _no_comments(js(), "//")


# ══ Selection — core.context.last_session() ═══════════════════════════════════

def test_last_session_returns_the_conversation_with_messages():
    cid = mkchat(PROJ_A, title="Recon")
    add_msgs(cid, [("user", "scan it"), ("assistant", "done")])
    row = C.last_session(PROJ_A)
    assert row and row["id"] == cid
    assert row["msg_count"] == 2


def test_nothing_to_resume_reads_as_none_not_as_an_empty_row():
    assert C.last_session(PROJ_A) is None
    assert C.resumable(PROJ_A) is None


def test_a_paused_chat_is_never_resumed():
    """⚠️ `/pause` parks a conversation on purpose and tells the user to type
    `/resume`. Reviving it at the next launch makes `/pause` a no-op nobody can
    see — the chat comes back and the command appears broken."""
    cid = mkchat(PROJ_A, title="Parked")
    add_msgs(cid, [("user", "hold on"), ("assistant", "ok")])
    assert C.last_session(PROJ_A)["id"] == cid          # visible while active
    C.pause_chat(cid)
    assert C.last_session(PROJ_A) is None


def test_an_empty_chat_is_never_resumed_even_when_it_is_the_newest():
    """⚠️ The Web UI creates its chat row UP FRONT, so the newest row for a
    project is routinely empty. Opening that is not continuity — it is a no-op
    that looks like one, and it costs the real transcript by hiding it."""
    old = mkchat(PROJ_A, title="Real work", updated="2026-01-01 00:00:00")
    add_msgs(old, [("user", "the actual conversation"), ("assistant", "yes")])
    mkchat(PROJ_A, title="New Chat", updated="2026-06-01 00:00:00")   # newest, empty
    row = C.last_session(PROJ_A)
    assert row and row["id"] == old, "an empty chat outranked a real conversation"


def test_only_user_and_assistant_rows_count_as_messages():
    """A chat whose only rows are tool traffic has nothing to show a human."""
    cid = mkchat(PROJ_A)
    add_msgs(cid, [("tool_call", "run_command"), ("tool_result", "ok")])
    assert C.last_session(PROJ_A) is None


def test_the_message_count_has_one_declaration():
    """⚠️ `list_chats_for_cwd` SELECTs the count and `last_session` FILTERS on it.
    Two spellings means the browser's chat list shows a count for a conversation
    continuity refuses to open."""
    assert C._MSG_COUNT.startswith(C._MSG_COUNT_EXPR)
    # Every reader interpolates the shared expression instead of re-spelling it.
    for fn in (C.last_session, C.list_chats_for_cwd, C.list_all_chats):
        body = src(fn)
        assert "_MSG_COUNT" in body, f"{fn.__name__} does not read the shared count"
        assert "COUNT(*) FROM messages" not in body, \
            f"{fn.__name__} re-spells the counting rule — that is the second declaration"


def test_continuity_never_crosses_a_project():
    a = mkchat(PROJ_A, title="A")
    add_msgs(a, [("user", "in A"), ("assistant", "yep")])
    b = mkchat(PROJ_B, title="B", updated="2026-06-01 00:00:00")
    add_msgs(b, [("user", "in B"), ("assistant", "yep")])
    assert C.last_session(PROJ_A)["id"] == a
    assert C.last_session(PROJ_B)["id"] == b


def test_last_session_is_total(monkeypatch):
    """Every caller asks on a startup path, before there is a REPL or a socket to
    report to. A DB hiccup must degrade to "nothing to resume"."""
    def boom(*a, **k):
        raise RuntimeError("db is on fire")
    monkeypatch.setattr(C, "qone", boom)
    assert C.last_session(PROJ_A) is None
    assert C.resumable(PROJ_A) is None


# ══ Policy — AGENT2_RESUME ════════════════════════════════════════════════════

@pytest.mark.parametrize("raw,expect", [
    ("", "off"), ("   ", "off"), ("off", "off"), ("OFF", "off"),
    ("last", "last"), ("  LAST  ", "last"),
    ("no", "off"), ("0", "off"), ("true", "off"), ("nonsense", "off"),
])
def test_resume_mode_falls_back_to_the_default_never_past_it(monkeypatch, raw, expect):
    """⚠️ Same direction `AGENT2_WEB_ROLE` falls to `viewer` and
    `AGENT2_CONTEXT_ISOLATION` to `project`: an unrecognised value means nobody
    expressed a preference, so the answer is the one that surprises nobody.

    ⚠️ It is the DEFAULT that moved, not the principle. While continuing happened
    unasked the safe end was `last` (a typo must not disable a feature somebody
    relies on); now that continuing is opt-in the safe end is `off` (a typo must
    not silently reopen a conversation nobody asked for)."""
    monkeypatch.setenv(C.RESUME_ENV, raw)
    assert C.resume_mode() == expect
    assert C.auto_resume() is (expect == "last")


def test_a_launch_does_not_continue_unless_it_is_asked_to(monkeypatch):
    """⚠️ THE default, and the whole point of the flip: an unasked resume is not
    free. `save_history()` DELETEs a chat's rows and re-INSERTs the window, so the
    first turn of a session that continued by accident rewrites a transcript the
    user never meant to open. Starting clean costs a keystroke."""
    monkeypatch.delenv(C.RESUME_ENV, raising=False)
    assert C.resume_mode() == C.RESUME_DEFAULT == "off"
    assert C.auto_resume() is False
    cid = mkchat(C.current_cwd(), title="Yesterday")
    add_msgs(cid, [("user", "old"), ("assistant", "news")])
    assert C.last_session() is not None, "selection must not read the policy flag"
    assert ST.resume_last_conversation() is None
    assert S.chat is None, "a launch nobody asked to continue bound a conversation"


def test_the_opt_in_still_continues(monkeypatch):
    """`AGENT2_RESUME=last` restores the behaviour this shipped with. The flip
    changed a default; it did not delete a feature, and anyone who wants a launch
    to carry on can still say so once."""
    monkeypatch.setenv(C.RESUME_ENV, "last")
    cid = mkchat(C.current_cwd(), title="Carry on")
    add_msgs(cid, [("user", "where were we"), ("assistant", "here")])
    assert C.auto_resume() is True
    assert ST.resume_last_conversation(), "AGENT2_RESUME=last must still resume"
    assert (S.chat or {}).get("id") == cid


def test_policy_is_not_selection(monkeypatch):
    """⚠️ `AGENT2_RESUME` governs the AUTOMATIC resume and nothing else. `/load`,
    `--continue` and the `/resume` picker are the user asking out loud, and an
    explicit ask is never governed by a default — reading one flag as "may anything
    reopen a conversation" would quietly delete three commands.

    ⚠️ That matters MORE now that `off` is the default: if the policy governed the
    explicit paths too, continuing would be unreachable rather than opt-in."""
    cid = mkchat(C.current_cwd(), title="Explicit")
    add_msgs(cid, [("user", "still here"), ("assistant", "yes")])
    monkeypatch.setenv(C.RESUME_ENV, "off")
    assert C.auto_resume() is False
    assert C.last_session() is not None, "selection must not read the policy flag"
    assert ST.resume_last_conversation() is None            # policy said no
    assert S.chat is None
    assert ST.load_last_conversation(), "/load must still work with resume off"
    assert (S.chat or {}).get("id") == cid


def test_the_explicit_loader_never_consults_the_policy():
    """⚠️ Structural, because the behavioural test above can only prove one
    direction. `load_last_conversation()` is the path `/load` and `--continue`
    take; a policy check inside it would make both flags no-ops in exactly the
    configuration where they are the only way in — and the shipped behaviour would
    still look right under `AGENT2_RESUME=last`."""
    body = fn_code(ST.load_last_conversation)
    assert "auto_resume" not in body and "resume_mode" not in body
    assert "auto_resume()" in fn_code(ST.resume_last_conversation)


# ══ resumable() — the payload ═════════════════════════════════════════════════

def test_resumable_carries_identity_and_size_but_never_message_text():
    """⚠️ `GET /api/chats/<cid>` is the ONE reader of a transcript. A second
    reader shaped like a status probe is how message content ends up somewhere
    nobody audits."""
    secret = "the-password-is-hunter2"
    cid = mkchat(PROJ_A, title="Notes")
    add_msgs(cid, [("user", secret), ("assistant", "noted")])
    pay = C.resumable(PROJ_A)
    assert set(pay) == {"id", "title", "model", "mode", "messages", "updated_at"}
    assert pay["id"] == cid and pay["title"] == "Notes" and pay["messages"] == 2
    assert secret not in json.dumps(pay)


def test_resumable_states_what_not_whether():
    """The policy boolean lives in `auto_resume()`. The same fact twice in one
    payload is a client resuming against a policy that says not to."""
    cid = mkchat(PROJ_A)
    add_msgs(cid, [("user", "hi"), ("assistant", "hi")])
    assert "auto" not in (C.resumable(PROJ_A) or {})


# ══ The history window — cli/store.py ═════════════════════════════════════════

def test_load_window_and_save_window_are_the_same_declaration():
    """⚠️ ONE number. It used to be `[-60:]` in three loaders against `h[-100:]`
    in the writer."""
    assert isinstance(ST.HISTORY_WINDOW, int) and ST.HISTORY_WINDOW > 0
    assert "[-HISTORY_WINDOW:]" in src(ST._history_for)
    assert "[-HISTORY_WINDOW:]" in src(ST.save_history)
    # No literal slice may survive in either half.
    for fn in (ST._history_for, ST.save_history):
        body = src(fn)
        assert "[-60:]" not in body and "[-100:]" not in body


def test_no_literal_history_slice_survives_in_the_cli():
    """The `/resume` picker and the recovery loader are loaders too — they read
    the same rows `save_history` will later rewrite."""
    code = cli_code()
    assert "_msgs_to_history(rows)[-60:]" not in code
    assert "_msgs_to_history(row)[-60:]" not in code
    assert code.count("[-HISTORY_WINDOW:]") >= 2


def test_a_resume_round_trip_does_not_shrink_the_conversation():
    """⚠️ THE load-bearing test in this file. `save_history()` DELETEs the chat's
    rows before re-INSERTing the window it was handed, so whatever the loader
    declined to read is what the next save deletes. Harmless while loading was an
    opt-in `/load`; continuity makes the round trip happen at EVERY launch, which
    is how "resume where you stopped" would have become "and forget the rest".

    Sabotage: change either slice to a literal smaller than the seeded size and
    this goes red.
    """
    size = 150
    assert size < ST.HISTORY_WINDOW, "seed must fit the window, or nothing is proved"
    cid = mkchat(C.current_cwd(), title="Long one")
    add_msgs(cid, [(("user" if i % 2 == 0 else "assistant"), f"msg {i}")
                   for i in range(size)])
    assert db.qone("SELECT COUNT(*) c FROM messages WHERE chat_id=?", (cid,))["c"] == size

    history = ST.load_last_conversation()
    assert history is not None and len(history) == size
    assert (S.chat or {}).get("id") == cid

    ST.save_history(history, "2.5-flash", "pro")            # one turn later
    kept = db.qone("SELECT COUNT(*) c FROM messages WHERE chat_id=?", (cid,))["c"]
    assert kept == size, f"a load/save round trip truncated {size} messages to {kept}"


def test_load_binds_nothing_when_the_read_comes_back_empty(monkeypatch):
    """⚠️ `S.chat` was being bound BEFORE the read was inspected, so an empty or
    unreadable conversation left the session bound to it with no history — and the
    next `save_history()` then erased the very chat the load had failed to read."""
    cid = mkchat(C.current_cwd(), title="Do not destroy me")
    add_msgs(cid, [("user", "precious"), ("assistant", "safe")])

    def dead(*a, **k):
        raise RuntimeError("read failed")
    monkeypatch.setattr(ST, "_db_qall", dead)

    assert ST.load_last_conversation() is None
    assert S.chat is None, "a failed load bound the session to the chat anyway"
    # And the follow-up save cannot reach a chat it was never bound to.
    ST.save_history([], "2.5-flash", "pro")
    monkeypatch.undo()
    assert db.qone("SELECT COUNT(*) c FROM messages WHERE chat_id=?", (cid,))["c"] == 2


def test_the_cli_probe_and_the_cli_loader_select_the_same_conversation():
    """The startup line PROMISES a conversation; the loader opens one. Advertising
    one and opening another is a lie only a transcript reader could catch."""
    cid = mkchat(C.current_cwd(), title="Same one")
    add_msgs(cid, [("user", "a"), ("assistant", "b")])
    assert ST.last_chat_for_cwd()["id"] == cid
    ST.load_last_conversation()
    assert (S.chat or {}).get("id") == cid


# ══ The order a conversation comes back in ════════════════════════════════════
# ⚠️ Found by live verification of continuity, not by reading: a 152-message
# resume came back NEWEST FIRST. `save_history()` rewrites the whole window in one
# batch, `created_at` is second-granular, and `idx_messages_chat_created` is
# `(chat_id, created_at DESC)` — so a bare `ORDER BY created_at` is served by
# walking that index backwards and a fully-tied conversation is reversed end to
# end. `agent.build_context()` documented and fixed exactly this; four other
# readers each carried their own clause and each still had the bug.

def test_a_resumed_conversation_comes_back_in_the_order_it_was_written():
    """⚠️ THE second load-bearing test here. Sabotage: drop `, rowid` from
    `core.context.MSG_ORDER` and this goes red — reversed, not merely shuffled."""
    said = [f"turn {i}" for i in range(12)]
    cid = mkchat(C.current_cwd(), title="Ordered")
    add_msgs(cid, [(("user" if i % 2 == 0 else "assistant"), t)
                   for i, t in enumerate(said)])

    # Go through save_history() first: THAT is what ties every row to one second.
    ST.save_history(ST.load_last_conversation(), "2.5-flash", "pro")
    stamps = db.qall("SELECT DISTINCT created_at FROM messages WHERE chat_id=?", (cid,))
    assert len(stamps) == 1, "the premise of this test is that the rows tie"

    S.chat = None
    back = ST.load_last_conversation()
    assert [m["content"] for m in back] == said, "the conversation came back out of order"
    assert back[0]["role"] == "user" and back[0]["content"] == "turn 0"


def test_message_order_has_one_declaration_and_every_reader_uses_it():
    """⚠️ Five readers, one clause. A local `ORDER BY created_at` is the bug this
    constant exists to end, so no reader of `messages` may spell its own."""
    assert C.MSG_ORDER == "ORDER BY created_at, rowid"
    assert C.MSG_ORDER_DESC == "ORDER BY created_at DESC, rowid DESC"

    # The literal text of the clause exists in exactly one file: this one.
    owner = _code_only(Path(ROOT, "agent2", "core", "context", "__init__.py"))
    assert owner.count("ORDER BY created_at") == 2, "the two constants are the only home"

    readers = ("agent2/agent.py", "agent2/cli/store.py", "agent2/server/routes.py",
               "agent2/llm/provider_agent.py", "agent2cli.py")
    for rel in readers:
        code = _code_only(Path(ROOT, rel))
        assert "FROM messages" in code, f"{rel} stopped reading messages"
        assert "MSG_ORDER" in code, f"{rel} does not use the shared clause"
        assert "ORDER BY created_at" not in code, \
            f"{rel} re-spells the ordering rule instead of interpolating MSG_ORDER"


def test_editing_a_message_cannot_delete_the_conversation_before_it():
    """⚠️ Same root cause, worse consequence. `edit_message` truncated with
    `created_at >= ?`, and every row of a resumed CLI conversation carries ONE
    timestamp — so editing the newest message deleted the whole chat. Sabotage:
    drop the `rowid` half of the predicate and this goes red."""
    cid = mkchat(C.current_cwd(), title="Editable")
    add_msgs(cid, [("user", "one"), ("assistant", "two"),
                   ("user", "three"), ("assistant", "four")])
    ST.save_history(ST.load_last_conversation(), "2.5-flash", "pro")   # ties them

    rows = db.qall(f"SELECT id, content, rowid AS rid FROM messages "
                   f"WHERE chat_id=? {C.MSG_ORDER}", (cid,))
    assert len({r["rid"] for r in rows}) == 4
    target = rows[2]                                    # "three" — edit the 3rd

    row = db.qone("SELECT created_at, rowid AS rid FROM messages WHERE id=?",
                  (target["id"],))
    db.exe("DELETE FROM messages WHERE chat_id=? AND (created_at, rowid) >= (?, ?)",
           (cid, row["created_at"], row["rid"]))

    left = [r["content"] for r in db.qall(
        f"SELECT content FROM messages WHERE chat_id=? {C.MSG_ORDER}", (cid,))]
    assert left == ["one", "two"], f"truncation ate the past: {left}"


def test_the_socket_truncation_uses_the_rowid_tie_break():
    """The live predicate above must be the one `sockets.py` actually runs."""
    code = _no_comments(
        Path(ROOT, "agent2", "server", "sockets.py").read_text(encoding="utf-8"), "#")
    assert "(created_at, rowid) >= (?, ?)" in code
    assert "AND created_at >= ?" not in code, "the timestamp-only predicate is back"


# ══ The CLI wiring — read as source ═══════════════════════════════════════════

def cli_src() -> str:
    return Path(ROOT, "agent2cli.py").read_text(encoding="utf-8")


def cli_code() -> str:
    return _no_comments(cli_src(), "#")


def test_both_cli_loops_bound_context_by_the_config_value():
    """⚠️ The CLI hard-coded `history[-20:]` while `agent.build_context()` sent
    `MAX_CTX_MESSAGES` (40), so the same conversation reached the model at two
    different lengths depending on which surface the user typed into."""
    code = cli_code()
    assert code.count("history[-_cfg.MAX_CTX_MESSAGES:]") == 2
    assert "history[-20:]" not in code
    # The MODULE is imported, not the name — a bound constant cannot be corrected.
    assert "from agent2 import config as _cfg" in code


def test_continuity_is_offered_before_crash_recovery():
    """⚠️ ORDER. Recovery binds the chat its plan belongs to, so it must be able
    to overrule the conversation continuity picked — otherwise a resumed plan is
    delivered into a chat the model cannot see."""
    cli = cli_src()
    i_cont = cli.index("_resumed = _load()")
    i_scan = cli.index("_crash.scan_on_start()")
    # ⚠️ The CALL, not the def: `_offer_recovery` is defined earlier in the file,
    # so an index on the bare name would compare against the wrong occurrence and
    # pass whatever the order actually is.
    i_offer = cli.index("history = _offer_recovery(history)")
    assert i_cont < i_scan < i_offer


def test_the_launch_starts_clean_and_says_how_to_come_back():
    """⚠️ TWO flags, and the explicit one must reach the explicit loader.

    `--clear` is the belt-and-braces opt-out that wins even over
    `AGENT2_RESUME=last`; `--continue` (aliased `--load`) is `/load` at launch, so
    it takes `load_last_conversation` — the path that does NOT consult the policy.
    Routing it through `resume_last_conversation` instead would make the flag a
    silent no-op in exactly the default configuration it exists for.

    ⚠️ `dest="cont"` is mandatory, not style: `continue` is a Python keyword, so
    the derived attribute would only be reachable through `getattr`, and
    `args.continue` is a SyntaxError rather than a run-time bug.
    """
    code = cli_code()
    assert '"--clear"' in code
    assert "if not args.clear:" in code
    assert '"--continue", "--load"' in code and 'dest="cont"' in code
    assert "_load = load_last_conversation if args.cont else resume_last_conversation" in code
    # The per-launch flags name the per-install one, so the opt-in is discoverable.
    assert "AGENT2_RESUME" in cli_src()


def test_the_cli_says_what_it_continued_or_what_it_could():
    """A resume the user is not told about looks like leaked state; a hint for a
    conversation already on screen is noise. Exactly one line, either way."""
    cli = cli_src()
    assert "Continuing: " in cli
    assert "Last conversation here: " in cli
    assert "/load to continue it." in cli


# ══ The web route ═════════════════════════════════════════════════════════════

@pytest.fixture
def web():
    """A Flask test client on the migrated DB (same shape as test_tasks.py)."""
    from flask import Flask

    from agent2.server.routes import register_routes
    db.init_db()
    app = Flask(__name__)
    register_routes(app)
    with app.test_client() as c:
        yield c


def test_resume_route_is_not_swallowed_by_the_chat_id_route(web):
    """⚠️ Werkzeug ranks a literal segment above a converter, which is the ONLY
    reason `/api/chats/resume` is reachable next to `/api/chats/<cid>`. This
    endpoint depends on that rather than merely benefiting from it, so it is
    asserted instead of assumed."""
    adapter = web.application.url_map.bind("localhost")
    endpoint, args = adapter.match("/api/chats/resume", method="GET")
    assert endpoint == "api_resume_target"
    assert args == {}
    # The id route still works, and still gets the id.
    endpoint2, args2 = adapter.match("/api/chats/abc-123", method="GET")
    assert endpoint2 != "api_resume_target" and args2.get("cid") == "abc-123"


def test_resume_endpoint_reports_the_conversation_and_the_policy(web, monkeypatch):
    """⚠️ TWO facts in one payload, and the flip is why they must stay two: the
    conversation is reported whatever the policy says, so a browser can offer
    "continue?" as a button — or, as it does, print the hint and open a fresh chat."""
    cid = mkchat(C.current_cwd(), title="Web continuity")
    add_msgs(cid, [("user", "hello"), ("assistant", "hi")])
    d = web.get("/api/chats/resume").get_json()
    assert d["auto"] is False, "a fresh tab must not continue unasked"
    # ⚠️ Still reported: policy and selection are separate answers, and the browser
    # names the conversation it declined to open.
    assert d["resume"] and d["resume"]["id"] == cid
    assert d["resume"]["messages"] == 2

    monkeypatch.setenv(C.RESUME_ENV, "last")
    d2 = web.get("/api/chats/resume").get_json()
    assert d2["auto"] is True
    assert d2["resume"] and d2["resume"]["id"] == cid


def test_resume_endpoint_carries_no_message_text(web):
    secret = "sk-live-do-not-leak-me"
    cid = mkchat(C.current_cwd(), title="Keys")
    add_msgs(cid, [("user", secret), ("assistant", "stored")])
    body = web.get("/api/chats/resume").get_data(as_text=True)
    assert secret not in body


def test_resume_endpoint_is_read_only(web):
    """A probe the first paint makes may not create or mutate anything."""
    before = db.qone("SELECT COUNT(*) c FROM chats")["c"]
    assert web.get("/api/chats/resume").status_code == 200
    assert web.post("/api/chats/resume").status_code == 405
    assert db.qone("SELECT COUNT(*) c FROM chats")["c"] == before


def test_resume_endpoint_answers_when_there_is_nothing(web):
    d = web.get("/api/chats/resume").get_json()
    assert d["resume"] is None and d["auto"] is False


# ══ The browser — the pinned run bar ══════════════════════════════════════════

def test_the_run_bar_lives_outside_the_scroll_container():
    """⚠️ The indicator used to be a `.mrow` inside #msgs, which IS the scroll
    container: reading back a few screens took "is it still running" off screen
    entirely. #runbar sits between #msgs and #ia."""
    ui = Path(ROOT, "agent2", "server", "ui.py").read_text(encoding="utf-8")
    i_msgs, i_bar, i_ia = ui.index('id="msgs"'), ui.index('id="runbar"'), ui.index('id="ia"')
    assert i_msgs < i_bar < i_ia
    # #typing is now permanent inside it — `renderStage` appends into that id.
    assert ui.index('id="runbar"') < ui.index('id="typing"') < i_ia
    css = Path(ROOT, "public", "style.css").read_text(encoding="utf-8")
    for sel in ("#runbar{", "#runbar.live{", "#queued{", ".rb-q{"):
        assert sel in css, f"{sel} has no styling — the bar would render unreadable"


def test_one_renderer_owns_what_the_run_bar_shows():
    """Two writers leave it visible with nothing in it, or hidden with a queue
    nobody can see."""
    s = js()
    assert "function renderRunbar()" in s
    assert "bar.style.display=(live||n)?'flex':'none'" in s
    # showTyping/removeTyping became state changes that re-render, not DOM surgery.
    assert "function showTyping(){ hideWel(); S.live=true; renderRunbar(); }" in s
    assert "S.live=false" in s and ".stage-lbl'); if(lbl) lbl.remove()" in s


def test_live_is_not_busy():
    s = js()
    assert "queue:[], live:false," in s


# ══ The browser — the mid-turn queue ══════════════════════════════════════════

def test_the_composer_is_never_disabled_during_a_turn():
    """⚠️ THE feature. It used to be `ci.disabled=true` with the send button
    hidden, so a thought you had mid-turn had to be held in your head until the
    agent finished — while the CLI's `InputController` had always read keys during
    a turn and drained the queue after it."""
    s = js_code()
    body = s[s.index("function setBusy(v){"):s.index("function toast(")]
    assert "ci.disabled=false" in body
    assert "ci.disabled=true" not in body
    assert "sb.style.display='flex'; sb.disabled=false;" in body
    assert "Queue this message" in body


def test_sendmsg_queues_instead_of_refusing():
    s = js()
    body = s[s.index("function sendMsg(){"):s.index("// Chat-box key/input handling")]
    # The early return no longer consults S.busy — that WAS the refusal.
    guard = body[body.index("const inp="):body.index("if(typeof closeSlash")]
    assert "S.busy" not in guard
    assert "S.queue.push({text,attachments:S.attachments.slice(),chatId:S.chatId})" in body
    assert "Queued — sends when this turn ends" in body
    # ⚠️ Editing is REFUSED mid-turn, not queued: `edit_message` truncates history
    # at that message and re-runs, and the live turn is still appending to it.
    assert "if(S.busy&&S.editingMsgId){" in body


def test_the_queue_drains_only_when_a_turn_ends():
    """⚠️ `d.done` is the one moment the browser learns a turn is over. A queue
    drained from a timer, the composer or `removeTyping` would emit a second
    `chat_message` into a turn that is still running."""
    s = js_code()
    line = next(l for l in s.splitlines() if "socket.on('chat_response'" in l)
    assert "drainQueue()" in line
    assert line.index("setBusy(false)") < line.index("drainQueue()"), \
        "drainQueue refuses while S.busy — the order is what makes the message go"
    assert s.count("drainQueue()") == 2, "one definition, one call site"
    dq = s[s.index("function drainQueue(){"):s.index("const _origSendMsg")]
    assert "if(S.busy||!S.queue.length||!S.chatId) return;" in dq
    assert "S.queue.shift()" in dq and "S.queue=[]" not in dq   # one per turn


def test_stopping_discards_the_queue_and_says_so():
    s = js()
    stop = s[s.index("function stopAgent(){"):s.index("function editMsg(")]
    assert "clearQueue()" in stop
    assert "queued message(s) discarded" in stop
    # The socket event clears too — a server-side cancel is a stop nobody pressed,
    # so it clears SILENTLY rather than toasting a decision the user did not make.
    ev = next(l for l in s.splitlines() if "socket.on('agent_stopped'" in l)
    assert "clearQueue()" in ev and "toast(" not in ev


def test_switching_chats_drops_the_queue():
    """A queued message is a PENDING message, never a stored one: `drainQueue`'s
    chatId re-check only protects the head of the queue."""
    s = js()
    body = s[s.index("async function switchChat(id){"):s.index("async function loadTaskPanel(")]
    assert "clearQueue();" in body
    assert body.index("clearQueue();") < body.index("S.chatId=id")
    # The pinned bar outlives the transcript `renderMsgs` replaces.
    assert "removeTyping();" in body


def test_a_queued_message_can_be_taken_back():
    s = js()
    assert "function unqueue(i)" in s
    assert 'onclick="unqueue(' in s
    un = s[s.index("function unqueue(i){"):s.index("function _startTurn(")]
    assert "inp.value=m.text" in un, "removing a queued message destroyed its text"


def test_one_declaration_of_a_turn_start():
    """⚠️ The composer, the queue drain and Retry all start turns. Three copies of
    `showTyping(); setBusy(true); socket.emit('chat_message', …)` is three places
    to forget the run bar, the busy flag, or the model the turn is charged to."""
    s = js()
    assert s.count("socket.emit('chat_message'") == 1
    assert "function _startTurn(text,attachments){" in s
    assert s.count("_startTurn(") == 4          # 1 definition + 3 call sites


# ══ The browser — continuity on first paint ═══════════════════════════════════

def test_the_browser_asks_the_server_which_conversation_to_continue():
    """⚠️ NOT re-derived from `S.chats`: "the newest row" is not the rule — paused
    and empty chats are excluded — and a second rule means the tab opens one
    conversation while the terminal continues another."""
    s = js()
    assert "async function resumeTarget()" in s
    assert "fetch('/api/chats/resume')" in s
    assert "d.auto" in s and "d.resume.id" in s
    load = s[s.index("async function loadChats(){"):s.index("// Which conversation to continue")]
    assert "await resumeTarget()" in load
    assert load.index("resumeTarget()") < load.index("c=>!c.msg_count")


def test_continuity_failing_in_the_browser_costs_the_feature_not_the_page():
    """A missing route, a 4xx or junk JSON all mean "no continuity", and the caller
    falls through to exactly the blank-chat behaviour that shipped before this."""
    s = js()
    body = s[s.index("async function resumeTarget(){"):s.index("async function continueLast(){")]
    assert "try{" in body and "catch(e){ return null; }" in body
    assert "if(!r.ok) return null;" in body
    assert "if(!d||!d.resume||!d.resume.id) return null;" in body
    load = s[s.index("async function loadChats(){"):s.index("// Which conversation")]
    assert "if(blank) await switchChat(blank.id); else await newChat();" in load


def test_a_fresh_tab_opens_a_fresh_chat_and_names_what_it_did_not_open():
    """⚠️ THE BROWSER HALF OF THE FLIP, and the ordering inside `resumeTarget()` is
    the point: the policy is read AFTER the row is found, so a tab that declines to
    continue can still say which conversation is there — the browser's version of
    the CLI's "Last conversation here: … — /load to continue it." line.

    Testing `d.auto` first would be simpler and would leave the tab silent about a
    feature the user has no other way to discover."""
    s = js()
    body = s[s.index("async function resumeTarget(){"):s.index("async function continueLast(){")]
    i_row, i_auto = body.index("S.chats.find(c=>c.id===d.resume.id)"), body.index("if(!d.auto){")
    assert i_row < i_auto, "the policy is read before the selection — nothing to name"
    assert "Last conversation here:" in body and "/load continues it." in body
    assert body.index("if(!d.auto){") < body.index("return row.id;")


def test_the_browser_has_a_load_of_its_own_and_it_ignores_the_policy():
    """⚠️ `continueLast()` is `/load` in the browser, and it is a SECOND function
    rather than `resumeTarget(force)` for `core.context`'s reason: one runs unasked
    and must obey the policy, the other is the user asking out loud and never may.
    A shared function with a flag is one call site away from obeying a default it
    was written to ignore."""
    s = js()
    assert "async function continueLast()" in s
    body = s[s.index("async function continueLast(){"):s.index("function renderList(")]
    assert "d.auto" not in body, "the explicit path consulted the policy"
    assert "fetch('/api/chats/resume')" in body          # same selection rule
    assert "await switchChat(d.resume.id)" in body
    # Total: nothing here throws, and every refusal says why.
    assert "catch(e){ d=null; }" in body
    assert "No previous conversation in this project." in body
    assert "That conversation is no longer here." in body
    # And it is reachable — the palette spells it the way the terminal does.
    assert "{name:'load'," in s and "act:()=>continueLast()" in s


def test_the_browser_can_still_open_a_brand_new_chat():
    """The other half of the ask: "i want to open new chat in web mode also". The
    sidebar button and the palette command are two callers of ONE `newChat()`."""
    s = js()
    assert "async function newChat()" in s
    assert "{name:'new'," in s
    ui = Path(ROOT, "agent2", "server", "ui.py").read_text(encoding="utf-8")
    assert 'onclick="newChat()"' in ui
