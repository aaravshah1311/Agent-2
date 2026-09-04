# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/provider_agent.py
────────────────────────
Autonomous agent loop for CUSTOM providers (OpenAI- or Anthropic-compatible).

Mirrors agent2/agent.py's run_agent() behaviour (same tools, same DB, same
Socket.IO events) but talks to a user-registered endpoint via agent2.llm.providers
instead of the Gemini SDK. Tool execution is shared (agent2.tools.dispatch_tool
for local tools, terminal.stream_command for shell, `burp` and the
`integrations.registry` bridges for MCP tools), so a custom model gets the exact
same capabilities as Gemini.

⚠️ THE DISPATCH ORDER HERE IS THE OPPOSITE OF `agent.py`'s — shell, then the MCP
bridges, then `_LOCAL_TOOLS`, where `agent.py` checks `_LOCAL_TOOLS` first. That
is harmless for exactly one reason: `McpBridge.sanitize_name` force-prefixes every
MCP tool, so no MCP name can shadow a local one. Read
`integrations/mcp_base.py`'s docstring before relaxing anything about that prefix.
"""

from __future__ import annotations

import json
import random
import threading
import time

from agent2.config import SHELL_LABEL, MAX_AGENT_ITERS, MAX_TOOL_OUTPUT, MAX_RETRIES
from agent2.database import qall, qone, exe
from agent2.llm import providers
from agent2.llm import router
from agent2.tools import dispatch_tool, ToolContext
from agent2.terminal import stream_command
from agent2.integrations.burp_mcp import burp
from agent2.integrations import registry as mcp_registry
from agent2.llm.resilience import (
    call_with_retry, classify_error, is_blank_reply, blank_reply_notice,
)
from agent2.core.session import sessions
from agent2.core import workspace as _workspace
from agent2.core import diffs as _diffs
from agent2.core import recovery as _recovery
from agent2.core import broker as _broker
from agent2.core import logging as alog
from agent2.core.progress import TurnProgress, stage_for_tool
from agent2.agent import (
    system_prompt, save_msg, _LOCAL_TOOLS, _tool_label, _tool_result_summary,
)

SHELL_TOOL = "run_command"


def _retry_text_only(prov: dict, messages: list[dict], system: str, stop) -> str:
    """Re-ask a custom provider for plain text after a blank/filler reply.

    Mirrors agent.py's `_retry_without_tools`: dropping the tool schemas removes
    the tool-preamble pressure that makes some models answer a bare "hi" with an
    empty content block. Best-effort — returns "" if it doesn't help.
    """
    if stop.is_set():
        return ""
    try:
        result = call_with_retry(
            lambda: providers.chat(prov, messages, system, use_tools=False),
            should_stop=stop.is_set,
        )
    except Exception:
        return ""
    text = (result.get("text") or "").strip()
    return "" if is_blank_reply(text) else text


def _tool_meta(name: str, args: dict) -> dict:
    """Metadata for a saved tool_call/result row so the Web UI renders it right.

    ⚠️ EXACTLY ONE OF `cmd` / `burp` / `mcp` / `local` — `agent._tool_name` reads
    these back to rebuild the `function_call` name, and a row carrying two of them
    would let the call and its result disagree, which costs the whole turn.
    """
    if name == SHELL_TOOL:
        return {"args": args, "cmd": args.get("command", "")}
    if burp.is_burp_tool(name):
        return {"args": args, "burp": name}
    bridge = mcp_registry.resolve(name)
    if bridge is not None:
        return {"args": args, "mcp": name, "server": bridge.SERVER_KEY}
    return {"args": args, "local": name}


def _tool_desc(name: str, args: dict) -> str:
    """Human-readable description for a saved tool_call row."""
    if name == SHELL_TOOL:
        return args.get("description", "Running…")
    return _tool_label(name, args)


def _exec_tool(name: str, args: dict, sid: str, term_id: str, socketio,
               ctx: ToolContext | None = None, prog=None) -> tuple[str, bool]:
    """Run any tool by name; return (result_text, ok).

    *prog* is an optional `core.progress.TurnProgress`. It is threaded through
    here — the ONE dispatch point both wire formats (OpenAI and Anthropic) share
    — rather than at the two call sites, so the stage/queue/feed reporting cannot
    end up on only one of them.
    """
    def _report(label: str, stage: str) -> None:
        if prog:
            prog.stage(stage, label[:60])
            prog.task(label[:60], "running")

    def _done(label: str, ok: bool) -> None:
        if prog:
            prog.task(label[:60], "completed" if ok else "failed")
            prog.activity(label[:80], "success" if ok else "error")

    if name == SHELL_TOOL:
        cmd = args.get("command", "")
        desc = args.get("description", "Running…")
        socketio.emit("chat_tool_call",
                      {"command": cmd, "description": desc,
                       "shell": SHELL_LABEL, "tool": "run_command"}, room=sid)
        _report(cmd, "Executing Tools")
        if prog:
            prog.activity(f"$ {cmd[:60]}")
        # Mirrors agent.py: run_command is the one tool that does not pass
        # through `dispatch_tool`, so it records BOTH halves of its own
        # checkpoint sub-step here — in flight before, resolved after. Recovery
        # reads the in-flight half (rule 21).
        if ctx is not None:
            ctx.note_tool("run_command", detail=cmd)
        # Task 4: same attribution as `agent.py` — parity IS this module's
        # contract, and a command tracked on only one loop would make the
        # registry lie about which surface was busy.
        out, rc = stream_command(
            cmd, sid, term_id, socketio,
            task_id=ctx.current_task_id() if ctx is not None else "",
            session_id=ctx.existing_task_session() if ctx is not None else "",
        )
        if ctx is not None:
            ctx.note_tool("run_command", ok=rc == 0, detail=cmd)
        # Shell output already streams into the terminal pane; don't duplicate it
        # as a chat result block. The command-result card (item 6) comes from
        # `stream_command` itself, so this loop gets it without its own emit.
        if prog:
            prog.task(cmd[:60], "completed" if rc == 0 else "failed")
            prog.activity(f"exit {rc}  ·  {cmd[:60]}",
                          "success" if rc == 0 else "error")
        return out[:MAX_TOOL_OUTPUT], rc == 0

    if burp.is_burp_tool(name):
        socketio.emit("chat_tool_call",
                      {"command": f"{name}(…)", "description": f"Burp: {name}",
                       "shell": "Burp MCP", "tool": name}, room=sid)
        _report(f"Burp: {name}", "Executing Tools")
        r = burp.call_tool(name, args)
        out = (r.get("output") or r.get("error") or "")
        ok = "error" not in r
        socketio.emit("chat_tool_result",
                      {"tool": name, "summary": str(out)[:2000], "ok": ok}, room=sid)
        _done(f"Burp: {name}", ok)
        return str(out)[:MAX_TOOL_OUTPUT], ok

    # Task 8: every other MCP server (OWASP ZAP, …). Placed AFTER Burp for the
    # same reason as in `agent.py` — `resolve()` answers for Burp too, and Burp's
    # branch is the tested one.
    mcp_bridge = mcp_registry.resolve(name)
    if mcp_bridge is not None:
        label = f"{mcp_bridge.LABEL}: {name}"
        socketio.emit("chat_tool_call",
                      {"command": f"{name}(…)", "description": label,
                       "shell": f"{mcp_bridge.LABEL} MCP", "tool": name}, room=sid)
        _report(label, "Executing Tools")
        r = mcp_bridge.call_tool(name, args)
        out = (r.get("output") or r.get("error") or "")
        ok = "error" not in r
        socketio.emit("chat_tool_result",
                      {"tool": name, "summary": str(out)[:2000], "ok": ok}, room=sid)
        _done(label, ok)
        return str(out)[:MAX_TOOL_OUTPUT], ok

    if name in _LOCAL_TOOLS:
        label = _tool_label(name, args)
        socketio.emit("chat_tool_call",
                      {"command": f"{name}(…)", "description": label,
                       "shell": "tool", "tool": name}, room=sid)
        _report(label, stage_for_tool(name))
        # Same ordering invariant as agent.py: snapshot BEFORE the write, because
        # write_file overwrites and the old content is unrecoverable after.
        changes = _diffs.capture_for(name, args)
        r = dispatch_tool(name, args, ctx)
        summary = _tool_result_summary(name, r)
        ok = "error" not in r
        socketio.emit("chat_tool_result",
                      {"tool": name, "summary": summary[:2000], "ok": ok}, room=sid)
        # Mirrors agent.py exactly (that parity IS this module's contract): the
        # browser gets the STORED checklist, not the model's arguments.
        if name == "update_todo" and r.get("persisted"):
            from agent2.core import tasks as _tasks
            socketio.emit("chat_tasks",
                          _tasks.payload(str(r.get("session_id") or "")), room=sid)
        if changes and ok:
            for ch in changes:
                socketio.emit("chat_file_diff", ch.to_payload(), room=sid)
                _diffs.store.add(ch)
            if prog:
                prog.add_changes(changes)
        _done(label, ok)
        return summary[:MAX_TOOL_OUTPUT], ok

    return f"Unknown tool: {name}", False


def run_provider_agent(chat_id, user_message, sid, term_id, pid, socketio,
                       attachments=None) -> None:
    """Main loop for a custom provider. `pid` is the provider id (after 'custom:')."""
    ws = _workspace.current()
    session = sessions.open(sid, chat_id, workspace_id=ws.id)
    stop = session.cancel
    ctx = ToolContext(session=session, sid=sid, chat_id=chat_id)

    # Web parity with the CLI's rich UX (items 7, 8, 10, 13) — identical wiring
    # to `agent.py`, which is the contract this module is written to mirror.
    prog = TurnProgress(
        lambda ev, payload, room: socketio.emit(ev, payload, room=room),
        room=sid, chat_id=chat_id)
    prog.stage("Planning")

    def _finish() -> None:
        # Clear the transient queue BEFORE closing: those rows are pinned in the
        # topbar, so a turn ending with "Running" still there would strand it.
        prog.clear_finished()
        sessions.close(sid, chat_id)

    save_msg(chat_id, "user", user_message,
             {"attachments": [a["name"] for a in (attachments or [])]})

    # ── Personal Intelligence Layer ───────────────────────────────────────────
    # Learn from the ORIGINAL text the user wrote, then optionally produce an
    # enhanced copy (grammar / improvement) to send to the model. History/display
    # keep the original; only the model sees `sent_message`. All best-effort.
    project_name = (ws.metadata or {}).get("name", "")
    sent_message = user_message
    try:
        from agent2.core import pil
        pil.learn_from_message(user_message, project=project_name)
        sent_message, pil_report = pil.process_outgoing_prompt(
            user_message, project=project_name)
        if pil_report.get("changed"):
            socketio.emit("pil_enhanced", {
                "chat_id": chat_id,
                "grammar_applied": pil_report.get("grammar_applied", False),
                "grammar_edits": pil_report.get("grammar_edits", []),
                "improve_applied": pil_report.get("improve_applied", False),
                "improve_added": pil_report.get("improve_added", []),
                "final": pil_report.get("final", sent_message),
            }, room=sid)
        if random.random() < 0.10:
            threading.Thread(target=pil.optimize, daemon=True).start()
    except Exception:
        sent_message = user_message

    chat = qone("SELECT title FROM chats WHERE id=?", (chat_id,))
    if chat and chat["title"] == "New Chat":
        title = user_message.strip().replace("\n", " ")[:50]
        exe("UPDATE chats SET title=? WHERE id=?", (title, chat_id))
        socketio.emit("chat_titled", {"chat_id": chat_id, "title": title}, room=sid)

    prov = providers.get_provider(pid)
    if not prov:
        msg = "**Custom provider not found.** It may have been removed."
        save_msg(chat_id, "assistant", msg)
        socketio.emit("chat_response", {"text": msg, "done": True, "tokens": 0}, room=sid)
        _finish()
        return

    fmt = prov.get("format", "openai")

    # Lazily bring up the Burp MCP bridge so its tools are offered to the
    # custom provider too (parity with the Gemini loop).
    if burp.enabled and not burp.is_connected():
        ok, bmsg = burp.connect(timeout=8.0)
        socketio.emit("toast", {"msg": bmsg, "type": "success" if ok else "warning"}, room=sid)

    # Task 8: and every other MCP server, on the same terms. Parity with
    # `agent.py` IS this module's contract — a bridge the Gemini loop offers and
    # this one does not is a capability the user loses by switching model.
    _mcp_bridges = mcp_registry.extra_bridges()
    for _b in _mcp_bridges:
        _attempt = mcp_registry.ensure_connected(_b, timeout=8.0)
        if _attempt is not None:
            _ok, _msg = _attempt
            socketio.emit("toast",
                          {"msg": _msg, "type": "success" if _ok else "warning"}, room=sid)

    # Build message history from DB (plain user/assistant text is enough to seed;
    # tool round-trips within THIS turn are kept in the provider-native format).
    history = qall(
        "SELECT role, content FROM messages WHERE chat_id=? "
        "AND role IN ('user','assistant') ORDER BY created_at DESC LIMIT 20",
        (chat_id,))
    history.reverse()
    messages: list[dict] = [{"role": h["role"] if h["role"] == "user" else "assistant",
                             "content": h["content"]} for h in history]

    # Feed the PIL-processed copy to the model (history keeps the original). The
    # last row is the current user turn we just saved.
    if sent_message != user_message and messages and messages[-1]["role"] == "user":
        messages[-1]["content"] = sent_message

    # Recovery briefing (Task 3) — its own turn ahead of what the user typed, for
    # the same reasons as `agent.py`. Consumed once; "" when nothing is owed.
    _brief = _recovery.brief_for_chat(chat_id)
    if _brief:
        messages.insert(max(0, len(messages) - 1),
                        {"role": "user", "content": _brief})

    # ⚠️ Task 20: THE SYSTEM PROMPT IS BUILT *AFTER* THE HISTORY, not before it.
    # The Context Broker's bundle is assembled once per turn and needs the size of
    # the conversation it is being weighed against (Task 22 budgets against it), so
    # the prompt cannot be built until `messages` exists. Parity with `agent.py` is
    # this module's contract — a source the Gemini loop injects and this one does
    # not is a capability the user loses by switching model — so the bundle is
    # assembled here on the same terms and passed in the same way.
    _bundle = _broker.assemble(
        chat_id=chat_id, message=sent_message, surface="web",
        model_key="custom:" + str(prov.get("id") or ""),
        conversation_messages=len(messages),
        conversation_tokens=router.estimate_tokens(
            "".join(str(m.get("content") or "") for m in messages)),
    )
    for _src, _err in _bundle.errors.items():
        alog.context_source_failed(_src, _err)

    system = system_prompt(burp_connected=burp.is_connected(),
                           burp_tool_count=len(burp.list_tools()),
                           mcp_blocks=[b.prompt_block() for b in _mcp_bridges],
                           context=_bundle)

    total_tokens = 0
    for _ in range(MAX_AGENT_ITERS):
        if stop.is_set():
            socketio.emit("chat_response", {"text": "_Stopped by user._", "done": True,
                                            "tokens": total_tokens}, room=sid)
            _finish()
            return
        prog.stage("Calling Model", str(prov.get("model_id") or "")[:40])
        try:
            def _on_retry(attempt, delay, exc):
                socketio.emit(
                    "toast",
                    {"msg": f"Network hiccup — retrying ({attempt}/{MAX_RETRIES}) in {delay:.1f}s…",
                     "type": "warning"},
                    room=sid,
                )
            _started = time.time()
            result = call_with_retry(
                lambda: providers.chat(prov, messages, system),
                on_retry=_on_retry,
                should_stop=stop.is_set,
            )
        except Exception as exc:
            # Task 19: record the outcome and feed the breaker, exactly as the
            # Gemini loop does. ⚠️ This loop deliberately does NOT switch models on
            # failure. A custom provider is a user-configured endpoint with its own
            # key, base URL and wire format; "falling back" from it could only mean
            # sending the user's prompt to a DIFFERENT vendor they did not choose for
            # this turn — a data-egress decision no error justifies making silently.
            # So the ledger and the breaker apply (they are what `/api/models` and
            # the health surface read), and the model choice does not.
            _model_key = "custom:" + str(prov.get("id") or "")
            router.record_attempt(
                model=_model_key, ok=False,
                latency_ms=int((time.time() - _started) * 1000),
                primary_model=_model_key, failure_kind=classify_error(exc) or "unknown",
                failure_reason=str(exc), chat_id=chat_id, session_id=sid)
            router.note_failure(_model_key, classify_error(exc))
            if stop.is_set():
                socketio.emit("chat_response", {"text": "_Stopped by user._", "done": True,
                                                "tokens": total_tokens}, room=sid)
                _finish()
                return
            extra = ("\n\n> Temporary network/server issue — retried automatically. Please try again."
                     if classify_error(exc) == "transient" else "")
            msg = f"**Provider error ({prov.get('model_id')}):** {exc}{extra}"
            save_msg(chat_id, "assistant", msg)
            socketio.emit("chat_response", {"text": msg, "done": True, "tokens": total_tokens}, room=sid)
            _finish()
            return

        _pid_key = "custom:" + str(prov.get("id") or "")
        router.record_attempt(model=_pid_key, ok=True,
                              latency_ms=int((time.time() - _started) * 1000),
                              primary_model=_pid_key, chat_id=chat_id, session_id=sid)
        router.note_success(_pid_key)

        total_tokens += result.get("tokens", 0) or 0
        socketio.emit("token_update", {"chat_id": chat_id, "tokens": total_tokens}, room=sid)
        tool_calls = result.get("tool_calls") or []

        if not tool_calls:
            final = (result.get("text") or "").strip()

            # Same blank-reply guard as the Gemini loop: retry once with no tool
            # schemas attached before falling back to an honest notice, so a
            # simple "hi" can never come back as a bare "Done.".
            if is_blank_reply(final):
                final = _retry_text_only(prov, messages, system, stop) \
                    or blank_reply_notice()

            save_msg(chat_id, "assistant", final)
            socketio.emit("chat_response", {"text": final, "done": True, "tokens": total_tokens}, room=sid)
            # Items 8 + 10, same as the Gemini loop: close the trail, then report
            # what the turn did. `file_summary` self-gates on files > 0.
            prog.stage("Done")
            prog.file_summary()
            _finish()
            return

        # Append the assistant turn (native format) then execute each tool.
        if fmt == "anthropic":
            messages.append({"role": "assistant", "content": result.get("raw_content", [])})
            tool_results = []
            for tc in tool_calls:
                out, ok = _exec_tool(tc["name"], tc["args"], sid, term_id, socketio, ctx, prog)
                save_msg(chat_id, "tool_call", _tool_desc(tc["name"], tc["args"]),
                         _tool_meta(tc["name"], tc["args"]))
                save_msg(chat_id, "tool_result", out[:10_000],
                         {**_tool_meta(tc["name"], tc["args"]), "ok": ok})
                tool_results.append({"type": "tool_result", "tool_use_id": tc["id"],
                                     "content": out or "(no output)"})
            messages.append({"role": "user", "content": tool_results})
        else:  # openai
            messages.append(result.get("raw_assistant") or
                            {"role": "assistant", "content": result.get("text", ""),
                             "tool_calls": [
                                 {"id": tc["id"], "type": "function",
                                  "function": {"name": tc["name"],
                                               "arguments": json.dumps(tc["args"])}}
                                 for tc in tool_calls]})
            for tc in tool_calls:
                out, ok = _exec_tool(tc["name"], tc["args"], sid, term_id, socketio, ctx, prog)
                save_msg(chat_id, "tool_call", _tool_desc(tc["name"], tc["args"]),
                         _tool_meta(tc["name"], tc["args"]))
                save_msg(chat_id, "tool_result", out[:10_000],
                         {**_tool_meta(tc["name"], tc["args"]), "ok": ok})
                messages.append({"role": "tool", "tool_call_id": tc["id"],
                                 "content": out or "(no output)"})

    msg = f"Agent reached max iterations ({MAX_AGENT_ITERS})."
    save_msg(chat_id, "assistant", msg)
    socketio.emit("chat_response", {"text": msg, "done": True, "tokens": total_tokens}, room=sid)
    _finish()
