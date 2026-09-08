# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
Tests for the core agent loop (agent2/agent.py).

Run from the repo root:  python -m pytest .github/tests/test_agent_loop.py -v

Why this exists
───────────────
`run_agent()` is the single most important function in Agent2 — every turn on
every surface goes through it — and it had ZERO direct tests. Its behaviour was
only ever verified by hand against a live Gemini key, which means the failure
modes that matter (a tool call that errors, a quota rotation, a blank reply, the
iteration cap) were never exercised at all.

These tests replace the network with a scripted fake client, so the loop's real
control flow runs end to end against the real SQLite layer:

  - system_prompt()  : static body + memories + rules + Burp block, and the
                       caching that makes it cheap on every turn.
  - build_context()  : DB rows → Gemini Content, including the tool_call /
                       tool_result pairing rule.
  - run_agent()      : text turn, run_command turn, local-tool turn, Burp turn,
                       stop-token handling, quota rotation, blank-reply recovery,
                       MAX_AGENT_ITERS bound, and the no-keys path.

FAILSAFE focus: every test that ends a turn asserts a `done: True` frame was
emitted and the session was closed. A turn that dies without either is the bug
that leaves the UI spinning forever, so it is checked explicitly rather than
implied.
"""

from __future__ import annotations

import json
import threading
import uuid

import pytest

from agent2 import agent as A
from agent2.database import exe, init_db, qall


# ── Test doubles ──────────────────────────────────────────────────────────────

class FakeSocket:
    """Records every emit instead of sending it. Mirrors socketio.emit's shape."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self._lock = threading.Lock()

    def emit(self, event: str, data=None, room=None, **kw) -> None:
        with self._lock:
            self.events.append((event, data if isinstance(data, dict) else {}))

    # — query helpers —
    def names(self) -> list[str]:
        return [e for e, _ in self.events]

    def of(self, event: str) -> list[dict]:
        return [d for e, d in self.events if e == event]

    def first(self, event: str) -> dict | None:
        got = self.of(event)
        return got[0] if got else None

    def final(self) -> dict | None:
        """The terminal chat_response frame (done=True)."""
        for _e, d in reversed(self.events):
            if _e == "chat_response" and d.get("done"):
                return d
        return None


class FakePart:
    """A single response part: text, or a function call, never both."""

    def __init__(self, text: str | None = None, function_call=None,
                 thought: bool = False):
        self.text = text
        self.function_call = function_call
        self.thought = thought


class FakeCall:
    def __init__(self, name: str, args: dict | None = None):
        self.name = name
        self.args = args or {}


class FakeContent:
    def __init__(self, parts):
        self.parts = parts


class FakeCandidate:
    def __init__(self, parts, finish_reason: str = "STOP"):
        self.content = FakeContent(parts) if parts is not None else None
        self.finish_reason = finish_reason


class FakeUsage:
    def __init__(self, total: int = 10):
        self.total_token_count = total


class FakeResponse:
    def __init__(self, parts, finish_reason: str = "STOP", tokens: int = 10):
        self.candidates = [FakeCandidate(parts, finish_reason)]
        self.usage_metadata = FakeUsage(tokens)


def text_reply(msg: str, tokens: int = 10) -> FakeResponse:
    return FakeResponse([FakePart(text=msg)], tokens=tokens)


def call_reply(name: str, args: dict | None = None) -> FakeResponse:
    return FakeResponse([FakePart(function_call=FakeCall(name, args))])


class FakeModels:
    """Serves a scripted list of responses; records every request it was given."""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[dict] = []

    def generate_content(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        if not self.script:
            # Ran off the end of the script: a bare text reply ends the turn
            # cleanly rather than hanging the test.
            return text_reply("script exhausted — ending turn")
        nxt = self.script.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        if callable(nxt):
            return nxt()
        return nxt


class FakeClient:
    def __init__(self, script, label: str = "1"):
        self.models = FakeModels(script)
        self.label = label


class FakeRotator:
    """Stands in for llm.keys.rotator: hands out fake clients, tracks failures."""

    def __init__(self, clients):
        self.clients = list(clients)
        self.idx = 0
        self.failures: list[tuple[str, bool]] = []
        self.usage: list[tuple[str, int]] = []

    def get(self):
        if self.idx >= len(self.clients):
            return None, None, None
        c = self.clients[self.idx]
        return c, f"key-{c.label}", c.label

    def fail(self, key, quota: bool = False):
        self.failures.append((key, quota))
        if quota:
            self.idx += 1        # rotate to the next key, like the real rotator

    def record_usage(self, label, tokens):
        self.usage.append((label, tokens))

    def status(self):
        return [{"label": c.label, "active": True} for c in self.clients]


class FakeBurp:
    """Burp bridge stub. Disconnected by default so the loop skips Burp entirely."""

    def __init__(self, connected: bool = False, tools: tuple[str, ...] = (),
                 result: dict | None = None):
        self.enabled = False
        self._connected = connected
        self._tools = tuple(tools)
        self.result = result or {"success": True, "output": "burp ok"}
        self.calls: list[tuple[str, dict]] = []

    def is_connected(self) -> bool:
        return self._connected

    def connect(self, timeout: float = 8.0):
        self._connected = True
        return True, "connected"

    def gemini_declarations(self) -> list:
        from google.genai import types
        return [types.FunctionDeclaration(name=t, description=t) for t in self._tools]

    def is_burp_tool(self, name: str) -> bool:
        return name in self._tools

    def call_tool(self, name: str, args: dict) -> dict:
        self.calls.append((name, dict(args)))
        return self.result


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module", autouse=True)
def _schema():
    init_db()


@pytest.fixture
def chat():
    """A throwaway chat row. Returns its id."""
    cid = str(uuid.uuid4())
    exe("INSERT INTO chats(id, title) VALUES(?, 'New Chat')", (cid,))
    yield cid
    exe("DELETE FROM messages WHERE chat_id=?", (cid,))
    exe("DELETE FROM chats WHERE id=?", (cid,))


@pytest.fixture
def sock():
    return FakeSocket()


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Cut every external dependency the loop reaches for.

    PIL is stubbed out (it is separately tested and its background optimize
    thread would race the assertions), Burp is disconnected, and the shell is
    replaced — a real run_command in a unit test would execute on the dev box.
    """
    monkeypatch.setattr(A, "burp", FakeBurp(), raising=True)

    import agent2.core.pil as pil
    monkeypatch.setattr(pil, "learn_from_message", lambda *a, **k: None)
    monkeypatch.setattr(pil, "process_outgoing_prompt",
                        lambda text, **k: (text, {"changed": False}))
    monkeypatch.setattr(pil, "optimize", lambda *a, **k: None)

    # ⚠️ Task 19: the model circuit breaker is PROCESS-GLOBAL in-memory state, by
    # design — it exists to stop a whole process hammering a model that keeps
    # failing. That makes it shared between tests: three quota errors on
    # `2.5-flash` inside `BREAKER_WINDOW` leaves it cooling, and the NEXT test
    # silently gets a different fallback target. Every failure in this file after
    # that reads as "the loop picked the wrong model" rather than "the previous test
    # left state behind", which is a genuinely miserable hour. Reset both ends.
    from agent2.llm import router as _router
    _router.reset_breaker()
    yield
    _router.reset_breaker()


def rows(chat_id: str) -> list[dict]:
    """Messages for a chat in insertion order, with `meta` already decoded."""
    out = qall(
        "SELECT role, content, meta FROM messages WHERE chat_id=? "
        "ORDER BY created_at, rowid",
        (chat_id,),
    )
    for r in out:
        try:
            r["meta"] = json.loads(r["meta"] or "{}")
        except Exception:
            r["meta"] = {}
    return out


def roles(chat_id: str) -> list[str]:
    return [r["role"] for r in rows(chat_id)]


def run(chat_id, sock, script, *, monkeypatch=None, rotator=None,
        model="2.5-flash", mode="pro", message="build me a thing",
        attachments=None, sid="sid-1"):
    """Drive run_agent() against a scripted client. Returns the FakeRotator."""
    rot = rotator or FakeRotator([FakeClient(script)])
    if monkeypatch is not None:
        monkeypatch.setattr(A, "rotator", rot, raising=True)
    A.run_agent(chat_id, message, sid, "term-1", model, mode, sock,
                attachments=attachments)
    return rot


def sent_contents(rot, call_index: int = 0):
    """The `contents` list handed to the model on a given generate_content call."""
    return rot.clients[0].models.calls[call_index]["contents"]


# ── system_prompt() ───────────────────────────────────────────────────────────

def test_system_prompt_injects_memories_rules_and_platform(monkeypatch):
    """The prompt is static body + memories + rules, all present and ordered."""
    from agent2.core import memory as M
    from agent2.core import rules as R

    monkeypatch.setattr(M, "top_memories",
                        lambda limit=M.PROMPT_LIMIT: [{"content": "user prefers python over go"}])
    monkeypatch.setattr(M, "count_memories", lambda: 1)
    monkeypatch.setattr(R, "list_rules",
                        lambda active_only=False: [{"content": "always use async"}])
    # ⚠️ Both blocks are VersionedCaches: they rebuild only when their resource is
    # notified. Patching the builder's source is invisible to a cache that is
    # already warm, so an earlier test file that touched memories makes this pass
    # or fail by run ORDER. Its siblings below invalidate for the same reason.
    A._MEM_CACHE.invalidate()
    A._RULES_CACHE.invalidate()

    sp = A.system_prompt()
    assert "Agent2" in sp
    assert "user prefers python over go" in sp
    assert "always use async" in sp
    # Memories section comes before the rules section.
    assert sp.index("MEMORIES") < sp.index("CUSTOM RULES")
    # Platform rules are present (whatever OS the suite runs on).
    assert "PLATFORM:" in sp


def test_system_prompt_memory_block_is_bounded(monkeypatch):
    """⚠️ The prompt block must come from core.memory, not be re-inlined here.

    This block is rebuilt into the system prompt of EVERY iteration of EVERY
    turn, so an unbounded one multiplies its token cost by MAX_AGENT_ITERS —
    measured ~5,100 tokens per turn at 300 memories. agent.py used to build its
    own unbounded version, which made the limit in core.memory dead code, so
    testing core.memory alone does NOT pin this.
    """
    from agent2.core import memory as M

    # This file has no memories fixture, so clean up either side of the write.
    exe("DELETE FROM memories")
    try:
        for i in range(M.PROMPT_LIMIT + 50):
            M.add_memory(f"fact number {i}", importance=3)
        A._MEM_CACHE.invalidate()

        sp = A.system_prompt()
        bullets = [ln for ln in sp.splitlines()
                   if ln.startswith("- fact number ")]
        assert len(bullets) == M.PROMPT_LIMIT
        assert "not shown" in sp      # partial recall is disclosed, not hidden
    finally:
        exe("DELETE FROM memories")
        A._MEM_CACHE.invalidate()


def test_system_prompt_burp_block_only_when_connected(monkeypatch):
    from agent2.core import memory as M
    from agent2.core import rules as R

    monkeypatch.setattr(M, "top_memories", lambda limit=M.PROMPT_LIMIT: [])
    monkeypatch.setattr(R, "list_rules", lambda active_only=False: [])

    off = A.system_prompt()
    assert "BURP SUITE" not in off

    on = A.system_prompt(burp_connected=True, burp_tool_count=7)
    assert "BURP SUITE" in on
    assert "7 " in on


def test_static_prompt_built_once():
    """The constant body is cached — the identical object comes back."""
    assert A._static_prompt() is A._static_prompt()
    assert len(A._static_prompt()) > 1000


def test_memories_block_rebuilds_only_after_invalidate(monkeypatch):
    """VersionedCache: repeated reads reuse one build until the resource changes.

    This is what makes system_prompt() free on a normal turn — a regression here
    puts two DB queries back on every single agent iteration.
    """
    from agent2.core import memory as M

    calls = {"n": 0}

    def _mem(limit=M.PROMPT_LIMIT):
        calls["n"] += 1
        return [{"content": f"fact {calls['n']}"}]

    monkeypatch.setattr(M, "top_memories", _mem)
    monkeypatch.setattr(M, "count_memories", lambda: 1)
    cache = A._MEM_CACHE
    cache.invalidate()

    first = cache.get()
    again = cache.get()
    assert first == again and calls["n"] == 1        # one build, two reads

    cache.invalidate()                                # memories changed
    third = cache.get()
    assert third != first and calls["n"] == 2         # rebuilt exactly once
    cache.invalidate()


# ── build_context() ───────────────────────────────────────────────────────────

def test_build_context_orders_and_maps_roles(chat):
    A.save_msg(chat, "user", "hello")
    A.save_msg(chat, "assistant", "hi back")
    ctx = A.build_context(chat)

    assert [c.role for c in ctx] == ["user", "model"]
    assert ctx[0].parts[0].text == "hello"
    assert ctx[1].parts[0].text == "hi back"


def test_build_context_is_chronological_within_one_second(chat):
    """Regression: `created_at` is second-granular, so a whole turn ties on it.

    Ordering by that column alone let SQLite break the tie by rowid — which under
    DESC is REVERSED — and the model received the turn backwards. The rowid
    tie-break in build_context() is what keeps this honest.
    """
    for i in range(8):
        A.save_msg(chat, "user", f"m{i}")

    ctx = A.build_context(chat)
    assert [c.parts[0].text for c in ctx] == [f"m{i}" for i in range(8)]


def test_build_context_pairs_tool_call_with_result(chat):
    A.save_msg(chat, "user", "read me a file")
    A.save_msg(chat, "tool_call", "Reading: x.py",
               {"local": "read_file", "args": {"path": "x.py"}})
    A.save_msg(chat, "tool_result", "contents", {"ok": True, "local": "read_file"})

    ctx = A.build_context(chat)
    assert [c.role for c in ctx] == ["user", "model", "user"]

    fc = ctx[1].parts[0].function_call
    fr = ctx[2].parts[0].function_response
    assert fc.name == "read_file"
    assert fc.args == {"path": "x.py"}
    assert fr.name == "read_file"
    assert fr.response["output"] == "contents"
    assert fr.response["success"] is True


def test_build_context_skips_orphan_tool_call(chat):
    """A tool_call with no following tool_result must be dropped.

    Gemini rejects a function_call that has no matching function_response, so a
    dangling pair would fail the whole turn, not just lose one message.
    """
    A.save_msg(chat, "user", "hello")
    A.save_msg(chat, "tool_call", "orphan", {"local": "read_file", "args": {}})
    ctx = A.build_context(chat)
    assert [c.role for c in ctx] == ["user"]


def test_build_context_skips_orphan_tool_result(chat):
    """The exact mirror of the test above, and it is NOT redundant.

    Gemini rejects a function_response with no matching function_call just as it
    rejects the reverse. Only the forward half of the pairing rule was ever
    written, which was self-consistent and half a rule.

    Sabotage check: delete the `elif role == "tool_result"` guard in
    build_context() and this fails — ctx opens with a bare function_response.
    """
    A.save_msg(chat, "tool_result", "orphan", {"local": "read_file", "ok": True})
    A.save_msg(chat, "user", "hello")

    ctx = A.build_context(chat)
    assert [c.role for c in ctx] == ["user"]
    assert not any(p.function_response for c in ctx for p in c.parts)


def test_build_context_never_opens_with_an_orphan_function_response(chat):
    """The window boundary is how the orphan actually happens in production.

    The window is a `LIMIT MAX_CTX_MESSAGES` off the NEWEST end, so its oldest
    row is wherever the count landed. Land it on a tool_result and the tool_call
    that pairs with it is the row that fell off the front — so the FIRST Content
    of a long tool-using conversation was an orphan response and the whole turn
    died at the vendor, with no error on this side.

    Sabotage check: delete the tool_result guard and this fails with one
    unmatched function_response, ctx[0] carrying it.
    """
    A.save_msg(chat, "user", "read me a file")
    A.save_msg(chat, "tool_call", "Reading: x.py",
               {"local": "read_file", "args": {"path": "x.py"}})
    A.save_msg(chat, "tool_result", "contents", {"ok": True, "local": "read_file"})
    # Push the pair to the front edge: the window then starts ON the result.
    for i in range(A.MAX_CTX_MESSAGES - 1):
        A.save_msg(chat, "assistant", f"a{i}")

    ctx = A.build_context(chat)
    calls = [p.function_call for c in ctx for p in c.parts if p.function_call]
    responses = [p.function_response for c in ctx for p in c.parts if p.function_response]
    assert len(calls) == len(responses), f"unpaired: {len(calls)} calls, {len(responses)}"
    # Both halves are gone, not one of them: the call was evicted by the LIMIT.
    assert (calls, responses) == ([], [])
    assert ctx[0].parts[0].text == "a0"


def test_build_context_keeps_a_pair_whose_call_is_the_oldest_row(chat):
    """The tool_result guard may not be over-eager.

    A tool_call at index 0 whose result sits at index 1 is a COMPLETE pair, and
    dropping it would silently shrink every conversation whose window happens to
    start on a tool call — the opposite failure, equally invisible.
    """
    A.save_msg(chat, "tool_call", "Reading: x.py",
               {"local": "read_file", "args": {"path": "x.py"}})
    A.save_msg(chat, "tool_result", "contents", {"ok": True, "local": "read_file"})

    ctx = A.build_context(chat)
    assert [c.role for c in ctx] == ["model", "user"]
    assert ctx[0].parts[0].function_call.name == "read_file"
    assert ctx[1].parts[0].function_response.name == "read_file"


def test_build_context_caps_at_max_messages(chat):
    pairs = A.MAX_CTX_MESSAGES + 20
    for i in range(pairs):
        A.save_msg(chat, "user", f"m{i}")
        A.save_msg(chat, "assistant", f"a{i}")

    ctx = A.build_context(chat)
    assert len(ctx) == A.MAX_CTX_MESSAGES
    # Oldest evicted, newest kept, chronological order intact.
    first_kept = pairs - A.MAX_CTX_MESSAGES // 2
    assert ctx[0].parts[0].text == f"m{first_kept}"
    assert ctx[-1].parts[0].text == f"a{pairs - 1}"


def test_build_context_falls_back_for_run_command(chat):
    """Legacy rows carry no `local`/`burp` meta — they are shell calls."""
    A.save_msg(chat, "user", "run a scan")
    A.save_msg(chat, "tool_call", "scanning", {"args": {"command": "nmap -sV"}})
    A.save_msg(chat, "tool_result", "output", {"rc": 0})

    ctx = A.build_context(chat)
    fc = ctx[1].parts[0].function_call
    assert fc.name == "run_command"
    assert fc.args["command"] == "nmap -sV"
    assert ctx[2].parts[0].function_response.response["returncode"] == 0


def test_build_context_truncates_tool_output(chat):
    """A huge tool result is clipped to MAX_TOOL_OUTPUT before it re-enters context."""
    A.save_msg(chat, "user", "cat a big file")
    A.save_msg(chat, "tool_call", "reading", {"args": {"command": "cat big"}})
    A.save_msg(chat, "tool_result", "x" * (A.MAX_TOOL_OUTPUT * 3), {"rc": 0})

    ctx = A.build_context(chat)
    out = ctx[2].parts[0].function_response.response["output"]
    assert len(out) == A.MAX_TOOL_OUTPUT


def test_build_context_round_trips_a_burp_tool(chat):
    """Burp rows take `rc` for success, unlike local tools which take `ok`."""
    A.save_msg(chat, "user", "check the proxy history")
    A.save_msg(chat, "tool_call", "querying burp",
               {"burp": "burp_proxy_history", "args": {"count": 5}})
    A.save_msg(chat, "tool_result", "3 requests", {"burp": "burp_proxy_history", "rc": 0})

    ctx = A.build_context(chat)
    fc = ctx[1].parts[0].function_call
    fr = ctx[2].parts[0].function_response
    assert fc.name == "burp_proxy_history"
    assert fc.args == {"count": 5}
    assert fr.name == "burp_proxy_history"
    assert fr.response["success"] is True

    A.save_msg(chat, "tool_call", "again", {"burp": "burp_scanner", "args": {}})
    A.save_msg(chat, "tool_result", "failed", {"burp": "burp_scanner", "rc": 1})
    assert A.build_context(chat)[-1].parts[0].function_response.response["success"] is False


def test_build_context_names_the_call_and_result_identically(chat):
    """Gemini rejects a function_response whose name doesn't match its call.

    A mismatch fails the entire turn rather than losing one message, so the name
    is resolved from `meta` in ONE place for both row kinds. This is the test
    that would catch the two resolutions drifting apart.
    """
    for meta in ({"local": "read_file"}, {"burp": "burp_repeater"}, {}):
        A.save_msg(chat, "tool_call", "call", {**meta, "args": {}})
        A.save_msg(chat, "tool_result", "result", meta)

    ctx = A.build_context(chat)
    calls = [p.parts[0].function_call.name for p in ctx if p.parts[0].function_call]
    results = [p.parts[0].function_response.name
               for p in ctx if p.parts[0].function_response]
    assert calls == results == ["read_file", "burp_repeater", "run_command"]


def test_build_context_does_not_invent_args_for_a_named_tool(chat):
    """A named tool with no stored args gets `{}` — never the shell shape.

    `run_command` rows fall back to `{"command": ..., "description": ...}`
    rebuilt from legacy meta. Handing that to `read_file` would pass parameters
    its schema does not declare.
    """
    A.save_msg(chat, "tool_call", "some description", {"local": "read_file"})
    A.save_msg(chat, "tool_result", "ok", {"local": "read_file", "ok": True})

    fc = A.build_context(chat)[0].parts[0].function_call
    assert fc.name == "read_file"
    assert fc.args == {}, f"invented args for a named tool: {fc.args}"


def test_build_context_reports_returncode_only_for_shell(chat):
    """`returncode` is a shell concept; a local tool has no exit status.

    Leaking it would tell the model a `read_file` call "exited 0", which is a
    fact about a process that never existed.
    """
    A.save_msg(chat, "tool_call", "read", {"local": "read_file", "args": {}})
    A.save_msg(chat, "tool_result", "body", {"local": "read_file", "ok": False})
    A.save_msg(chat, "tool_call", "shell", {"args": {"command": "ls"}})
    A.save_msg(chat, "tool_result", "listing", {"rc": 2})

    ctx = A.build_context(chat)
    local, shell = ctx[1].parts[0].function_response, ctx[3].parts[0].function_response
    assert "returncode" not in local.response
    assert local.response["success"] is False       # from `ok`, not from `rc`
    assert shell.response["returncode"] == 2
    assert shell.response["success"] is False


# ── run_agent(): text turns ───────────────────────────────────────────────────

def test_text_turn_saves_and_emits(chat, sock, monkeypatch):
    """One text reply → user msg saved, reply saved, done frame, session closed."""
    run(chat, sock, [text_reply("Hello! What should we build?")],
        monkeypatch=monkeypatch)

    final = sock.final()
    assert final is not None and final["text"] == "Hello! What should we build?"
    assert roles(chat) == ["user", "assistant"]
    assert rows(chat)[-1]["content"] == "Hello! What should we build?"
    # The user's message is persisted under its ORIGINAL text.
    assert rows(chat)[0]["content"] == "build me a thing"
    assert "pil_enhanced" not in sock.names()
    assert A.sessions.get("sid-1", chat).task.status == "done"


def test_tokens_are_accounted_and_emitted(chat, sock, monkeypatch):
    rot = run(chat, sock, [text_reply("hi", tokens=42)], monkeypatch=monkeypatch)

    assert rot.usage == [("1", 42)]
    assert sock.final()["tokens"] == 42
    assert sock.first("token_update")["tokens"] == 42


# ── run_agent(): tool turns ───────────────────────────────────────────────────

def test_local_tool_then_final_text(chat, sock, monkeypatch):
    """Local tool call → result saved → the next turn produces the final text."""
    run(chat, sock, [
        call_reply("update_todo", {"todos": [{"task": "scan", "status": "completed"},
                                             {"task": "report", "status": "pending"}]}),
        text_reply("Scanned; report still open."),
    ], monkeypatch=monkeypatch)

    assert roles(chat) == ["user", "tool_call", "tool_result", "assistant"]
    tr = [r for r in rows(chat) if r["role"] == "tool_result"][0]
    assert tr["meta"] == {"ok": True, "local": "update_todo"}
    assert "1/2" in tr["content"]                      # _todo_summary progress line

    result = sock.first("chat_tool_result")
    assert result["tool"] == "update_todo" and result["ok"] is True
    assert sock.final()["text"] == "Scanned; report still open."


def test_local_tool_error_is_reported_not_raised(chat, sock, monkeypatch):
    """A failing tool must feed its error back to the model, not end the turn.

    FAILSAFE: the loop keeps going so the model can correct itself — a tool error
    that killed the turn is exactly the 'agent gives up halfway' bug.
    """
    run(chat, sock, [
        call_reply("read_file", {"path": "definitely/not/here.txt"}),
        text_reply("That file doesn't exist — want me to create it?"),
    ], monkeypatch=monkeypatch)

    assert roles(chat) == ["user", "tool_call", "tool_result", "assistant"]
    tr = [r for r in rows(chat) if r["role"] == "tool_result"][0]
    assert tr["content"].startswith("Error:")
    assert tr["meta"]["ok"] is False
    assert sock.first("chat_tool_result")["ok"] is False
    # The turn still completed with a real answer.
    assert "doesn't exist" in sock.final()["text"]


def test_unregistered_tool_does_not_crash_the_turn(chat, sock, monkeypatch):
    """An unknown local-looking tool falls through to the final-text branch."""
    run(chat, sock, [
        call_reply("no_such_tool", {"x": 1}),
        text_reply("unreachable"),
    ], monkeypatch=monkeypatch)

    # Not in _LOCAL_TOOLS and not a Burp tool → treated as a final (empty) reply,
    # which the blank-reply path then turns into an honest notice.
    final = sock.final()
    assert final is not None and final["done"] is True
    assert roles(chat) == ["user", "assistant"]


def test_run_command_turn_uses_shell_not_the_registry(chat, sock, monkeypatch):
    """run_command streams the shell; it must never reach dispatch_tool."""
    captured = {}

    # **kw absorbs the Task 4 attribution (task_id / session_id). The doubles
    # here assert on the shell being reached at all, not on the bookkeeping —
    # `test_commands.py` owns that — so they must not re-pin the signature.
    def fake_stream(cmd, sid, term_id, socket, **kw):
        captured["cmd"] = cmd
        return "scan complete", 0

    monkeypatch.setattr(A, "stream_command", fake_stream)
    monkeypatch.setattr(A, "dispatch_tool",
                        lambda *a, **k: pytest.fail("run_command hit dispatch_tool"))

    run(chat, sock, [
        call_reply("run_command", {"command": "nmap -sV localhost",
                                   "description": "port scan"}),
        text_reply("Port 80 is open."),
    ], monkeypatch=monkeypatch)

    assert captured["cmd"] == "nmap -sV localhost"
    assert roles(chat) == ["user", "tool_call", "tool_result", "assistant"]
    tr = [r for r in rows(chat) if r["role"] == "tool_result"][0]
    assert tr["meta"]["rc"] == 0
    assert tr["meta"]["cmd"] == "nmap -sV localhost"
    # Shell output streams into the terminal pane — no duplicate chat result.
    assert sock.first("chat_tool_result") is None


def test_failed_command_keeps_going(chat, sock, monkeypatch):
    """A non-zero exit is data for the model, not the end of the turn."""
    monkeypatch.setattr(A, "stream_command",
                        lambda cmd, sid, tid, s, **kw: ("command not found", 127))

    run(chat, sock, [
        call_reply("run_command", {"command": "nmapp", "description": "typo"}),
        text_reply("Typo — the binary is `nmap`. Retrying."),
    ], monkeypatch=monkeypatch)

    tr = [r for r in rows(chat) if r["role"] == "tool_result"][0]
    assert tr["meta"]["rc"] == 127
    assert "Typo" in sock.final()["text"]


def test_burp_tool_dispatched_to_bridge(chat, sock, monkeypatch):
    """A connected Burp session routes burp_* to the bridge, not the registry."""
    fake = FakeBurp(connected=True, tools=("burp_scan",),
                    result={"success": True, "output": "2 issues found"})
    monkeypatch.setattr(A, "burp", fake)
    monkeypatch.setattr(A, "dispatch_tool",
                        lambda *a, **k: pytest.fail("burp tool hit dispatch_tool"))

    run(chat, sock, [
        call_reply("burp_scan", {"url": "http://x"}),
        text_reply("Scan done — 2 issues."),
    ], monkeypatch=monkeypatch)

    assert fake.calls == [("burp_scan", {"url": "http://x"})]
    assert roles(chat) == ["user", "tool_call", "tool_result", "assistant"]
    tr = [r for r in rows(chat) if r["role"] == "tool_result"][0]
    assert tr["content"] == "2 issues found"
    assert tr["meta"]["burp"] == "burp_scan"
    assert "Scan done" in sock.final()["text"]


def test_memory_tool_writes_through_the_registry(chat, sock, monkeypatch):
    """save_memory reaches the real implementation and persists to SQLite."""
    from agent2.core import memory as M
    from agent2.database import exe as _exe

    marker = f"cargo-{uuid.uuid4().hex[:8]}"
    try:
        run(chat, sock, [
            call_reply("save_memory", {"content": f"user builds with {marker}"}),
            text_reply("Saved."),
        ], monkeypatch=monkeypatch)

        assert any(marker in m["content"] for m in M.list_memories())
        assert sock.first("chat_tool_result")["ok"] is True
    finally:
        _exe("DELETE FROM memories WHERE content LIKE ?", (f"%{marker}%",))


# ── run_agent(): several function_calls in one response ───────────────────────

def _two_calls() -> FakeResponse:
    """One response carrying two function_call parts, in the model's own order."""
    return FakeResponse([
        FakePart(function_call=FakeCall("write_file", {"path": "a.txt", "content": "x"})),
        FakePart(function_call=FakeCall("read_file", {"path": "a.txt"})),
    ])


def test_the_first_of_several_function_calls_is_the_one_that_runs(chat, sock, monkeypatch):
    """Gemini may return several calls at once; this loop runs ONE per iteration.

    Taking the LAST part INVERTS the order the model asked for — `write_file`
    then `read_file` executed as `read_file` then `write_file` — which is a wrong
    answer with no error anywhere. Running only the first is a degradation the
    model recovers from on the next round trip; running the wrong one is not.

    Sabotage check: drop the `if func_call is None:` in the extraction loop so it
    keeps the last call, and `seen` becomes ["read_file"].
    """
    seen: list[str] = []

    def fake_dispatch(name, args, ctx):
        seen.append(name)
        return {"output": "ok"}

    monkeypatch.setattr(A, "dispatch_tool", fake_dispatch)
    run(chat, sock, [_two_calls(), text_reply("done")], monkeypatch=monkeypatch)

    assert seen == ["write_file"], f"ran the wrong call of the batch: {seen}"
    assert roles(chat) == ["user", "tool_call", "tool_result", "assistant"]


def test_deferred_parallel_calls_are_logged_not_dropped_in_silence(chat, sock, monkeypatch):
    """A call this iteration declined to run is an audit line, not silence.

    A model whose second and third calls never ran, with nothing recorded, is
    indistinguishable from a model that only asked for one — which is the state
    that makes the ordering bug above unfindable from the logs.
    """
    seen: list[tuple[str, dict]] = []
    monkeypatch.setattr(A.alog, "event", lambda kind, **f: seen.append((kind, f)))
    monkeypatch.setattr(A, "dispatch_tool", lambda name, args, ctx: {"output": "ok"})

    run(chat, sock, [_two_calls(), text_reply("done")], monkeypatch=monkeypatch)

    fields = next((f for k, f in seen if k == "agent.parallel_calls_deferred"), None)
    assert fields is not None, f"no deferral recorded: {[k for k, _ in seen]}"
    assert fields["kept"] == "write_file"
    assert fields["deferred"] == 1


def test_a_single_function_call_reports_no_deferral(chat, sock, monkeypatch):
    """The audit line fires only when there was actually something to defer.

    An event on every ordinary tool turn would make the signal worthless.
    """
    seen: list[str] = []
    monkeypatch.setattr(A.alog, "event", lambda kind, **f: seen.append(kind))

    run(chat, sock, [call_reply("update_todo", {"todos": []}), text_reply("done")],
        monkeypatch=monkeypatch)

    assert "agent.parallel_calls_deferred" not in seen


# ── run_agent(): error paths and bounds ───────────────────────────────────────

def test_no_keys_is_honest(chat, sock, monkeypatch):
    """Zero configured keys → a clear message, and the turn still closes."""
    run(chat, sock, [], monkeypatch=monkeypatch, rotator=FakeRotator([]))

    final = sock.final()
    assert final is not None
    assert "No API keys configured" in final["text"]
    assert roles(chat) == ["user", "assistant"]
    assert A.sessions.get("sid-1", chat).task.status == "done"


def test_api_error_ends_turn_with_a_message(chat, sock, monkeypatch):
    """A non-quota API error is saved and the UI is told the turn is done."""
    run(chat, sock, [Exception("RPC failed")], monkeypatch=monkeypatch)

    final = sock.final()
    assert final is not None and "API Error" in final["text"]
    assert rows(chat)[-1]["role"] == "assistant"
    assert A.sessions.get("sid-1", chat).task.status == "done"


def test_invalid_model_error_falls_back_to_another_model(chat, sock, monkeypatch):
    """⚠️ THIS TEST INVERTED IN TASK 19, AND SAYING SO IS THE POINT.

    It used to assert the turn ENDED with a hint telling the user to switch to 2.5
    Flash by hand. That was the honest behaviour when nothing could switch for them.
    Task 19 makes the switch automatic, so asserting the old hint would now be
    pinning a worse product: the user reads "try 2.5 Flash" while Agent2 sits there
    able to do exactly that.

    What is still guaranteed, and what this now pins: the fallback is a `continue`
    in the SAME turn — same session, same context, no replayed tool calls (rule 20)
    — and the user is told it happened rather than silently served a different
    model's answer.
    """
    run(chat, sock, [Exception("model not found"), text_reply("second model ok")],
        monkeypatch=monkeypatch)

    fb = sock.first("model_fallback")
    assert fb is not None, "an invalid-model error did not trigger a fallback"
    assert fb["from"] == "2.5-flash" and fb["to"] != "2.5-flash"
    assert fb["reason"] == "invalid_model"
    assert sock.final()["text"] == "second model ok"
    # One user + one assistant: a fallback must not leave a partial turn behind.
    assert roles(chat) == ["user", "assistant"]


def test_a_fallback_is_bounded_and_ends_with_an_honest_error(chat, sock, monkeypatch):
    """⚠️ "Do not endlessly retry failed providers", stated as a test.

    Every model fails, so the turn must stop after `FALLBACK_MAX_HOPS` switches and
    report what it tried — not loop, and not claim success. The message naming the
    other models is what stops the user re-running an identical request.
    """
    monkeypatch.setattr(A.router._cfg, "FALLBACK_MAX_HOPS", 2, raising=False)
    boom = [Exception("model not found")] * 6
    run(chat, sock, boom, monkeypatch=monkeypatch)

    final = sock.final()
    assert "API Error" in final["text"]
    assert "Also tried" in final["text"], "the turn did not say which models it tried"
    assert len(sock.of("model_fallback")) == 2, "hop budget was not honoured"
    assert A.sessions.get("sid-1", chat).task.status == "done"


def test_an_unclassified_error_does_not_burn_quota_on_a_fallback(chat, sock, monkeypatch):
    """⚠️ `""` is deliberately absent from `router.FALLBACK_KINDS`.

    A malformed request or a serialisation bug fails identically on every model, so
    a fallback would spend the user's quota twice to reach the same error with a
    longer message. This is the test that would fail if someone "helpfully" added
    the empty string to that set.
    """
    rot = run(chat, sock, [Exception("RPC failed")], monkeypatch=monkeypatch)

    assert sock.of("model_fallback") == []
    assert len(rot.clients[0].models.calls) == 1, "the model was called twice"
    assert "API Error" in sock.final()["text"]


def test_quota_rotates_to_the_next_key_and_continues(chat, sock, monkeypatch):
    """A quota error on key 1 switches to key 2 and finishes the turn."""
    rot = FakeRotator([
        FakeClient([Exception("429 quota exhausted")], label="1"),
        FakeClient([text_reply("back on key 2")], label="2"),
    ])
    run(chat, sock, [], monkeypatch=monkeypatch, rotator=rot)

    assert rot.failures == [("key-1", True)]
    assert sock.final()["text"] == "back on key 2"
    assert roles(chat) == ["user", "assistant"]
    toast = sock.first("toast")
    assert toast is not None and "switched to key" in toast["msg"]


def test_quota_with_no_spare_key_falls_back_to_another_model(chat, sock, monkeypatch):
    """⚠️ ALSO INVERTED IN TASK 19, AND FOR A REASON WORTH KNOWING.

    One key, quota exhausted. There is still no phantom key rotation — that half is
    unchanged and still pinned. What changed is what happens next: Gemini's rate
    limits are PER MODEL, so a walled `2.5-flash` says nothing about whether
    `3.1-flash` will answer, and trying it is the whole point of a fallback chain.
    The old assertion ("API Error") pinned giving up while an answer was one call
    away.
    """
    rot = FakeRotator([FakeClient([Exception("429 quota exhausted"),
                                   text_reply("answered on the other model")],
                                  label="1")])
    run(chat, sock, [], monkeypatch=monkeypatch, rotator=rot)

    assert rot.failures == [("key-1", True)]
    assert sock.first("model_fallback") is not None
    assert sock.final()["text"] == "answered on the other model"


def test_a_key_rotation_is_tried_before_a_model_fallback(chat, sock, monkeypatch):
    """Ordering pin. A spare key is cheaper than a different model and keeps the
    user on the model they asked for, so quota must rotate the key FIRST and only
    fall back when there is no spare."""
    rot = FakeRotator([
        FakeClient([Exception("429 quota exhausted")], label="1"),
        FakeClient([text_reply("back on key 2")], label="2"),
    ])
    run(chat, sock, [], monkeypatch=monkeypatch, rotator=rot)

    assert sock.of("model_fallback") == [], "switched model while a spare key existed"
    assert sock.final()["text"] == "back on key 2"


def test_empty_candidate_is_reported(chat, sock, monkeypatch):
    """A candidate with no content must not raise — it ends the turn cleanly."""
    run(chat, sock, [FakeResponse(None, finish_reason="SAFETY")],
        monkeypatch=monkeypatch)

    final = sock.final()
    assert final is not None
    assert "Empty response" in final["text"] and "SAFETY" in final["text"]
    assert A.sessions.get("sid-1", chat).task.status == "done"


def test_iteration_cap_ends_the_turn(chat, sock, monkeypatch):
    """MAX_AGENT_ITERS tool calls → a clean message, never an endless loop."""
    monkeypatch.setattr(A, "MAX_AGENT_ITERS", 4)
    monkeypatch.setattr(A, "stream_command", lambda cmd, sid, tid, s, **kw: ("x", 0))

    run(chat, sock, [call_reply("run_command", {"command": "echo x"})] * 10,
        monkeypatch=monkeypatch)

    final = sock.final()
    assert final is not None and "max iterations (4)" in final["text"]
    assert roles(chat).count("tool_call") == 4
    assert A.sessions.get("sid-1", chat).task.status == "done"


def test_stop_token_aborts_before_the_next_iteration(chat, sock, monkeypatch):
    """Cancelling during a tool run ends the turn at the next loop checkpoint."""
    def cancel_during_command(cmd, sid, tid, s, **kw):
        A.sessions.cancel("sid-1", chat)      # user hits Stop mid-command
        return "partial output", 0

    monkeypatch.setattr(A, "stream_command", cancel_during_command)

    rot = run(chat, sock, [
        call_reply("run_command", {"command": "sleep 60"}),
        text_reply("should never be reached"),
    ], monkeypatch=monkeypatch)

    final = sock.final()
    assert final is not None and "Stopped by user" in final["text"]
    # The loop never asked the model again after the cancel.
    assert len(rot.clients[0].models.calls) == 1
    assert "should never be reached" not in json.dumps(sock.events, default=str)
    # The tool's own output was still recorded before the abort.
    assert roles(chat) == ["user", "tool_call", "tool_result"]


def test_stop_before_a_tool_dispatch_skips_the_tool(chat, sock, monkeypatch):
    """A cancel between the model reply and the tool run must not run the tool."""
    monkeypatch.setattr(A, "stream_command",
                        lambda *a: pytest.fail("command ran after cancel"))

    def reply_then_cancel():
        A.sessions.cancel("sid-1", chat)
        return call_reply("run_command", {"command": "rm -rf /"})

    run(chat, sock, [reply_then_cancel], monkeypatch=monkeypatch)

    assert "Stopped by user" in sock.final()["text"]
    assert roles(chat) == ["user"]            # nothing was executed or recorded


def test_a_completed_final_answer_is_still_delivered(chat, sock, monkeypatch):
    """Documents a deliberate gap: the final-text branch has no stop check.

    By the time the model has produced the answer the work is already paid for,
    so a cancel that lands during that last call does not discard it. This is
    pinned because it is a real, visible behaviour — Stop looks like it did
    nothing on the last turn — and any change to it should be a deliberate one.
    """
    def cancel_then_answer():
        A.sessions.cancel("sid-1", chat)
        return text_reply("here is your answer")

    run(chat, sock, [cancel_then_answer], monkeypatch=monkeypatch)

    assert sock.final()["text"] == "here is your answer"
    assert roles(chat) == ["user", "assistant"]


# ── run_agent(): blank-reply recovery ─────────────────────────────────────────

def test_blank_reply_retries_without_tools(chat, sock, monkeypatch):
    """A filler reply triggers the no-tools retry rather than shipping 'Done.'."""
    rot = run(chat, sock, [
        text_reply("Done.", tokens=0),
        text_reply("Hey! I'm Agent2 — what are we building?", tokens=5),
    ], monkeypatch=monkeypatch, message="hi")

    final = sock.final()
    assert final["text"] == "Hey! I'm Agent2 — what are we building?"
    # Exactly one chat_response: the recovered text, never the "Done." draft.
    assert len(sock.of("chat_response")) == 1
    assert rows(chat)[-1]["content"] == final["text"]

    # The retry dropped tools so there is nothing to call and no preamble spend.
    calls = rot.clients[0].models.calls
    assert len(calls) == 2
    assert calls[0]["config"].tools                 # first call offered tools
    assert not calls[1]["config"].tools             # retry did not


def test_blank_reply_falls_back_to_an_honest_notice(chat, sock, monkeypatch):
    """When the retry is also blank, say so — never fake a completed task."""
    run(chat, sock, [text_reply("ok"), text_reply(""), text_reply("")],
        monkeypatch=monkeypatch, message="hi")

    final = sock.final()
    assert final is not None
    assert "didn't produce a reply" in final["text"]
    assert roles(chat) == ["user", "assistant"]


def test_blank_reply_notice_mentions_the_cap_on_max_tokens(chat, sock, monkeypatch):
    run(chat, sock, [
        FakeResponse([FakePart(text="")], finish_reason="MAX_TOKENS"),
        text_reply(""), text_reply(""),
    ], monkeypatch=monkeypatch, message="hi")

    assert "output budget" in sock.final()["text"]


# ── run_agent(): request shaping ──────────────────────────────────────────────

def test_auto_title_from_the_first_message(chat, sock, monkeypatch):
    from agent2.database import qone

    run(chat, sock, [text_reply("hi")], monkeypatch=monkeypatch,
        message="let's build a todo app")

    assert qone("SELECT title FROM chats WHERE id=?", (chat,))["title"] == \
        "let's build a todo app"
    assert sock.first("chat_titled")["title"] == "let's build a todo app"


def test_long_first_message_is_truncated_for_the_title(chat, sock, monkeypatch):
    from agent2.database import qone

    run(chat, sock, [text_reply("ack")], monkeypatch=monkeypatch, message="z" * 120)

    title = qone("SELECT title FROM chats WHERE id=?", (chat,))["title"]
    assert title.endswith("…") and len(title) == 51


def test_text_attachment_is_inlined_into_the_user_turn(chat, sock, monkeypatch):
    import base64

    att = [{"name": "notes.txt",
            "data": base64.b64encode(b"hello world").decode(),
            "mime_type": "text/plain"}]
    rot = run(chat, sock, [text_reply("Read it.")], monkeypatch=monkeypatch,
              attachments=att)

    last_user = sent_contents(rot)[-1]
    assert last_user.role == "user"
    blob = "".join(p.text or "" for p in last_user.parts)
    assert "[Attached: notes.txt]" in blob
    assert "hello world" in blob
    # The attachment name is recorded on the stored message.
    assert rows(chat)[0]["meta"]["attachments"] == ["notes.txt"]


def test_binary_attachment_becomes_inline_data(chat, sock, monkeypatch):
    import base64

    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    att = [{"name": "shot.png",
            "data": base64.b64encode(png).decode(),
            "mime_type": "image/png"}]
    rot = run(chat, sock, [text_reply("Nice screenshot.")], monkeypatch=monkeypatch,
              attachments=att)

    parts = sent_contents(rot)[-1].parts
    blobs = [p for p in parts if getattr(p, "inline_data", None) is not None]
    assert len(blobs) == 1
    assert blobs[0].inline_data.mime_type == "image/png"
    assert blobs[0].inline_data.data == png


def test_corrupt_attachment_degrades_to_a_note(chat, sock, monkeypatch):
    """FAILSAFE: an undecodable attachment must not kill the turn."""
    att = [{"name": "bad.bin", "data": "!!!not-base64!!!", "mime_type": "text/plain"}]
    rot = run(chat, sock, [text_reply("Couldn't read that file.")],
              monkeypatch=monkeypatch, attachments=att)

    blob = "".join(p.text or "" for p in sent_contents(rot)[-1].parts)
    assert "[Attachment error: bad.bin" in blob
    assert sock.final()["text"] == "Couldn't read that file."


def test_mode_sets_the_token_ceiling(chat, sock, monkeypatch):
    rot = run(chat, sock, [text_reply("ok")], monkeypatch=monkeypatch, mode="fast")
    assert rot.clients[0].models.calls[0]["config"].max_output_tokens == 2048


def test_thinking_mode_sets_a_budget(chat, sock, monkeypatch):
    rot = run(chat, sock, [text_reply("ok")], monkeypatch=monkeypatch, mode="thinking")
    cfg = rot.clients[0].models.calls[0]["config"]
    assert cfg.max_output_tokens == 16384
    assert cfg.thinking_config.thinking_budget == 8000


def test_unknown_model_falls_back_to_the_default(chat, sock, monkeypatch):
    rot = run(chat, sock, [text_reply("ok")], monkeypatch=monkeypatch,
              model="not-a-real-model")
    assert rot.clients[0].models.calls[0]["model"] == \
        A.MODELS[A.DEFAULT_MODEL]["api"]


def test_chat_records_the_model_and_mode(chat, sock, monkeypatch):
    from agent2.database import qone

    run(chat, sock, [text_reply("ok")], monkeypatch=monkeypatch,
        model="2.5-pro", mode="fast")

    row = qone("SELECT model, mode FROM chats WHERE id=?", (chat,))
    assert row["model"] == "2.5-pro" and row["mode"] == "fast"


# ── run_agent(): PIL wiring ───────────────────────────────────────────────────

def test_pil_enhanced_copy_is_sent_but_not_stored(chat, sock, monkeypatch):
    """History keeps the user's words; only the copy sent to the model changes."""
    import agent2.core.pil as pil

    monkeypatch.setattr(pil, "process_outgoing_prompt", lambda text, **k: (
        "Build a REST API in Python.",
        {"changed": True, "grammar_applied": True, "grammar_edits": ["bild→Build"],
         "improve_applied": False, "improve_added": [],
         "final": "Build a REST API in Python."},
    ))

    rot = run(chat, sock, [text_reply("On it.")], monkeypatch=monkeypatch,
              message="bild a rest api in python")

    # The model received the enhanced copy…
    assert sent_contents(rot)[-1].parts[0].text == "Build a REST API in Python."
    # …while the stored history kept the original.
    assert rows(chat)[0]["content"] == "bild a rest api in python"
    ev = sock.first("pil_enhanced")
    assert ev is not None and ev["grammar_applied"] is True


def test_pil_failure_never_breaks_a_turn(chat, sock, monkeypatch):
    """FAILSAFE: PIL is best-effort — an exception falls back to the raw message."""
    import agent2.core.pil as pil

    monkeypatch.setattr(pil, "process_outgoing_prompt",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("pil blew up")))

    rot = run(chat, sock, [text_reply("Still fine.")], monkeypatch=monkeypatch,
              message="hello there")

    assert sent_contents(rot)[-1].parts[0].text == "hello there"
    assert sock.final()["text"] == "Still fine."
    assert "pil_enhanced" not in sock.names()


def test_optimizer_thread_failure_does_not_rewrite_the_sent_prompt(chat, sock, monkeypatch):
    """The idle-optimizer spawn must not be able to undo the enhancement.

    These two once shared a try/except. Because the housekeeping spawn ran AFTER
    the `pil_enhanced` emit, a Thread.start() failure (thread exhaustion under
    load — exactly when the optimizer is most likely to fail) reset
    `sent_message` back to the original *after* the UI had already been told the
    prompt was enhanced. The user saw "grammar applied", the model got the
    unenhanced text, and nothing logged the divergence.

    Optimization is unrelated housekeeping; it now sits in `else` with its own
    guard.
    """
    import agent2.core.pil as pil

    monkeypatch.setattr(pil, "process_outgoing_prompt", lambda text, **k: (
        "Build a REST API in Python.",
        {"changed": True, "grammar_applied": True, "grammar_edits": ["bild→Build"],
         "improve_applied": False, "improve_added": [], "final": "Build a REST API in Python."},
    ))
    # Force the 10% housekeeping branch, then make the spawn fail. Both are
    # patched on the `agent` module's own namespace — `A.threading` IS the
    # global threading module, so patching through it would break every other
    # thread in the process, including pytest's.
    spawned = {"tried": False}

    def _no_threads_left(*a, **k):
        spawned["tried"] = True
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(A, "random", type("R", (), {"random": staticmethod(lambda: 0.0)}))
    monkeypatch.setattr(A, "threading",
                        type("T", (), {"Thread": staticmethod(_no_threads_left)}))

    rot = run(chat, sock, [text_reply("On it.")], monkeypatch=monkeypatch,
              message="bild a rest api in python")

    assert spawned["tried"], "test premise wrong: the optimizer spawn never ran"
    ev = sock.first("pil_enhanced")
    assert ev is not None, "test premise wrong: the enhancement never fired"
    # What the UI was promised is what the model actually received.
    assert sent_contents(rot)[-1].parts[0].text == ev["final"] == \
        "Build a REST API in Python."
    assert sock.final()["text"] == "On it."


def test_a_malformed_response_part_is_logged_not_silently_dropped(chat, sock, monkeypatch):
    """A skipped part may be the turn's function_call — losing it must leave a trace.

    The handler previously did `except Exception: pass`. If the dropped part
    carried the function_call, the model's primary action simply never ran and
    there was nothing anywhere to explain why.
    """
    seen: list[tuple] = []
    monkeypatch.setattr(A.alog, "event",
                        lambda kind, **f: seen.append((kind, f)))

    class ExplodingPart:
        """A part whose attribute access raises, as a malformed proto would."""
        @property
        def function_call(self):
            raise ValueError("malformed part")

    resp = text_reply("Recovered.")
    resp.candidates[0].content.parts.insert(0, ExplodingPart())

    run(chat, sock, [resp], monkeypatch=monkeypatch)

    # The turn survived the bad part…
    assert sock.final()["text"] == "Recovered."
    # …and the drop is on the record.
    kinds = [k for k, _ in seen]
    assert "agent.part_skipped" in kinds, (
        f"the malformed part was dropped silently; logged: {kinds}"
    )
    fields = next(f for k, f in seen if k == "agent.part_skipped")
    assert fields["index"] == 0
    assert "malformed part" in fields["error"]


def test_the_part_handler_cannot_itself_raise(chat, sock, monkeypatch):
    """The recovery path must not touch the attribute that just blew up.

    A handler that re-reads `p.function_call` to name the tool would raise a
    SECOND time — from inside the except — and take down the whole turn, which
    is the exact failure the guard exists to prevent.
    """
    class HostilePart:
        """Raises on EVERY attribute access, including a retry in the handler."""
        def __getattr__(self, name):
            raise RuntimeError(f"hostile: {name}")

    resp = text_reply("Survived.")
    resp.candidates[0].content.parts.insert(0, HostilePart())

    run(chat, sock, [resp], monkeypatch=monkeypatch)

    assert sock.final()["text"] == "Survived."


