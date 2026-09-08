# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/sockets.py
─────────────────
All Socket.IO event handlers.
Call register_sockets(socketio) from main.py.

⚠️ THE HANDSHAKE IS GATED HERE, NOT BY THE HTTP GUARD (Task 14).
engineio's middleware wraps the WSGI app from *outside* Flask, so a Socket.IO
handshake never reaches `agent2/server/auth.py`'s `before_request`. `on_connect`
therefore asks `auth.socket_allowed()` itself and returns False to refuse. This is
not belt-and-braces: `run_raw_command` executes an arbitrary shell command and is
a socket event, so a guard on `/api/*` alone would lock every route that merely
*reads* state and leave the one that runs commands anonymous.

Refusing at `connect` — rather than checking inside each handler — is what makes
that total: a new event added later inherits the gate instead of needing to
remember it.

⚠️ CAPABILITIES ARE A SECOND, DIFFERENT GATE (Task 15).
`socket_allowed` answers *may this client connect at all*. It cannot answer *may
this client run a shell command*, because those are not the same question: a
`viewer` deployment wants the browser to watch a turn and never to reach
`run_raw_command`. So every data handler is registered through the local
`guarded()` decorator, which asks `core.permissions` for the event's capability
before the handler body runs.

⚠️ IT IS A DECORATOR ON THE REGISTRATION, NOT A CHECK INSIDE EACH BODY. A check
inside the body is opt-in — the failure mode is a handler added later with no
check, which is exactly the shape of bug that a single `before_request` avoids on
the HTTP side. Registering through `guarded()` is the only way to attach a handler
here, so a new event is gated by construction. `connect` and `disconnect` are the
two deliberate exceptions: `connect` runs the *authentication* gate itself (a
capability refusal there would deny a viewer the socket entirely), and
`disconnect` must always run its cleanup — refusing to release a subprocess tree
because of a permission policy would leak processes, which is not a safer state.
"""

import functools
import threading

from flask import request
from flask_socketio import emit, join_room

from agent2.config import (
    OS_NAME, SHELL_LABEL,
    MODELS, MODES, DEFAULT_MODEL, DEFAULT_MODE,
)
from agent2.database import qone, exe
from agent2.agent import run_agent, save_msg
from agent2.llm.provider_agent import run_provider_agent
from agent2.terminal import (
    stream_command, send_stdin, kill_proc,
    stop_agent as _term_stop, cleanup_sid as _term_cleanup,
    cancel_sid as _term_cancel,
)
from agent2.core.session import sessions
from agent2.core import scheduler
from agent2.core import tasks as _tasks
from agent2.core import logging as _audit
from agent2.core import permissions as perms
from agent2.server import auth
from agent2.server import weblog


def _checkpoint_stop(chat_id, reason: str) -> None:
    """Mark the chat's running tasks as stopped, with a reason.

    Best-effort and silent by design: a cancel that failed because the checkpoint
    write failed would be a cancel that did not happen, which is far worse than a
    resume that cannot say why it stopped. The sub-step trail is written as work
    happens, so the *position* is on disk either way.
    """
    if not chat_id:
        return
    try:
        row = _tasks.active_session_for_chat(str(chat_id))
        if row:
            _tasks.interrupt(str(row["id"]), reason)
    except Exception:
        pass


def _run_turn(socketio, sid, chat_id, *args) -> None:
    """Hand a turn to the bounded worker pool, degrading to a raw thread.

    Three outcomes, and each one has to leave the browser in a sane state:

    * queued   — a worker picks it up (immediately, in the normal case).
    * disabled — the pool is off (AGENT2_MAX_CONCURRENT_TURNS=0) or its threads
                 could not start. Run on our own daemon thread: exactly the
                 pre-scheduler behaviour, so turning the pool off can never be
                 the reason a message goes unanswered.
    * rejected — the backlog is full. This is the one case the user must SEE;
                 a dropped turn with a spinner running forever is worse than an
                 explicit "try again", so the turn is closed out properly.
    """
    outcome = scheduler.submit(
        _dispatch_agent, chat_id, *args, sid=sid, chat_id=chat_id,
    )
    if outcome == scheduler.DISABLED:
        threading.Thread(
            target=_dispatch_agent, args=(chat_id, *args), daemon=True,
        ).start()
        return
    if outcome == scheduler.REJECTED:
        msg = ("**Too many turns in flight.** The queue is full, so this message "
               "was not started.\n\n> Wait for a running turn to finish and send "
               "it again.")
        weblog.warn("agent", f"queue full — rejected a turn on chat {chat_id}")
        try:
            save_msg(chat_id, "assistant", msg)
        except Exception:
            pass
        socketio.emit("chat_response",
                      {"text": msg, "done": True, "tokens": 0}, room=sid)


def _dispatch_agent(chat_id, message, sid, term_id, model_key, mode_key,
                    socketio, attachments):
    """Route to the Gemini loop or a custom-provider loop based on model_key.

    Wrapped so a crash in the agent loop never kills the background thread
    silently and leaves the browser spinner hanging forever — any escaped
    exception is reported to the client and the turn is closed cleanly.
    """
    try:
        if isinstance(model_key, str) and model_key.startswith("custom:"):
            pid = model_key.split(":", 1)[1]
            run_provider_agent(chat_id, message, sid, term_id, pid, socketio, attachments)
        else:
            run_agent(chat_id, message, sid, term_id, model_key, mode_key, socketio, attachments)
    except Exception as exc:
        msg = f"**Unexpected error:** {str(exc)[:400]}\n\n> The session is still active — please try again."
        weblog.error("agent", f"loop crashed on chat {chat_id}: {str(exc)[:200]}")
        try:
            save_msg(chat_id, "assistant", msg)
        except Exception:
            pass
        try:
            socketio.emit("chat_response", {"text": msg, "done": True, "tokens": 0}, room=sid)
        except Exception:
            pass
        try:
            sessions.close(sid, chat_id, status="error")
        except Exception:
            pass


def register_sockets(socketio) -> None:
    """Attach all socket.io event handlers to *socketio*."""

    def guarded(event: str):
        """Register a data handler behind its capability check (Task 15).

        ⚠️ THE ONLY WAY A DATA HANDLER IS REGISTERED IN THIS FILE. Using
        `@socketio.on` directly for anything but `connect`/`disconnect` reopens the
        hole this closes, because the omission is invisible — the handler works
        perfectly, for everyone.

        A refusal is *told to the client*, not dropped: a `run_raw_command` that
        vanishes silently looks like a hung terminal, and a user who cannot tell a
        refusal from a bug files the bug. `emit` is wrapped because a socket can
        die between the check and the reply.
        """
        cap = perms.capability_for_event(event)

        def deco(fn):
            @functools.wraps(fn)
            def wrapper(*args, **kwargs):
                if not perms.allowed(cap):
                    perms.audit_use(cap, ok=False, what=f"socket:{event}",
                                    ip=auth.client_ip(request),
                                    role=perms.role_name())
                    weblog.warn("socket", f"refused {event} — "
                                          f"role={perms.role_name()} lacks '{cap}'")
                    try:
                        emit("toast", {"msg": perms.refusal(cap, what=event),
                                       "type": "error"})
                    except Exception:
                        pass
                    return None
                perms.audit_use(cap, ok=True, what=f"socket:{event}")
                return fn(*args, **kwargs)
            socketio.on(event)(wrapper)
            return wrapper
        return deco

    # ── Connection lifecycle ──────────────────────────────────────────────────

    @socketio.on("connect")
    def on_connect():
        # ⚠️ Task 14: the ONE gate for every socket event. Returning False refuses
        # the handshake, so no later handler — present or future — has to
        # re-check. `socket_allowed` fails closed if it raises.
        if not auth.socket_allowed(request):
            _audit.event("auth.socket.deny", ip=auth.client_ip(request))
            weblog.warn("socket", f"refused unauthenticated handshake from "
                                  f"{auth.client_ip(request) or '?'}")
            return False
        join_room(request.sid)
        weblog.socket_connected(request.sid)
        emit("connected", {
            "sid":           request.sid,
            "os":            OS_NAME,
            "shell":         SHELL_LABEL,
            "models":        MODELS,
            "modes":         MODES,
            "default_model": DEFAULT_MODEL,
            "default_mode":  DEFAULT_MODE,
        })
        return None

    @socketio.on("disconnect")
    def on_disconnect():
        sid = request.sid
        weblog.socket_disconnected(sid)
        # Abort every running task for this socket, kill its child processes,
        # then drop the session records (sections 8, 9). Queued-but-unstarted
        # turns are dropped too — a closed browser has nowhere to stream to, so
        # running them would burn quota to write into a room nobody is in.
        #
        # ⚠️ Task 7: `_term_cancel` BEFORE `_term_cleanup`, and it is not optional.
        # `cleanup_sid` only forgets the handles — it kills nothing. Closing the tab
        # mid-`nmap` therefore left the whole tree running with the one reference to
        # it discarded, and its execution ACTIVE forever, so `/api/commands` kept
        # reporting a command nobody could see or stop. Forgetting a process is not
        # the same as ending it.
        scheduler.cancel(sid)
        sessions.cancel(sid)
        _term_cancel(sid, "browser disconnected")
        _term_cleanup(sid)
        sessions.cleanup_sid(sid)

    # ── Chat messages ─────────────────────────────────────────────────────────

    @guarded("chat_message")
    def on_chat(data):
        sid = request.sid
        msg = data.get("message", "").strip()
        model = data.get("model", DEFAULT_MODEL)
        atts = data.get("attachments", []) or []
        weblog.info("chat", f"→ {model} {weblog.dim('chat=' + str(data.get('chat_id')))}  "
                            f"{len(msg)} chars"
                            + (f" +{len(atts)} attachment(s)" if atts else ""))
        _run_turn(
            socketio, sid, data.get("chat_id"),
            msg,
            sid,
            data.get("term_id", "t1"),
            model,
            data.get("mode",  DEFAULT_MODE),
            socketio,
            atts,
        )

    # ── Stop running agent ────────────────────────────────────────────────────

    @guarded("stop_agent")
    def on_stop_agent(data):
        sid = request.sid
        chat_id = (data or {}).get("chat_id") if isinstance(data, dict) else None
        # Signal the per-chat cancel token(s) AND any legacy terminal stop event,
        # then kill running child processes so an abort is immediate (section 9).
        #
        # `scheduler.cancel` covers the case `sessions.cancel` structurally cannot:
        # a turn still sitting in the queue has not reached `sessions.open()` yet,
        # so it owns no cancel token. Without this it would start running minutes
        # later, after the user had already been told it stopped.
        scheduler.cancel(sid, chat_id)
        sessions.cancel(sid, chat_id)
        _term_stop(sid)
        # ⚠️ Task 7: the stop EVENT is read between agent iterations, so on its own
        # it cannot end a command that is already streaming — that child ran to
        # completion while the UI said it had stopped. `_term_cancel` is what
        # actually reaches the subprocess, its children and its command state, with
        # the same record-then-kill ordering `kill_proc` uses.
        stopped = _term_cancel(sid, "stopped by user")
        if stopped:
            weblog.info("chat", f"cancelled {len(stopped)} running command(s)")
        # Stamp the checkpoint so a resume can say WHY it stopped and which
        # sub-step was in flight. Best-effort: the sub-step trail itself is
        # already durable (core/tasks.py), so this only adds the reason.
        _checkpoint_stop(chat_id, "stopped by user")
        emit("agent_stopped", {"chat_id": chat_id})

    # ── Edit a past user message (truncate + re-run) ──────────────────────────

    @guarded("edit_message")
    def on_edit_message(data):
        sid       = request.sid
        msg_id    = data.get("message_id")
        new_text  = data.get("new_text", "").strip()
        chat_id   = data.get("chat_id")
        term_id   = data.get("term_id",  "t1")
        model_key = data.get("model", DEFAULT_MODEL)
        mode_key  = data.get("mode",  DEFAULT_MODE)

        if not (msg_id and new_text and chat_id):
            return

        row = qone("SELECT created_at, rowid AS rid FROM messages WHERE id=?", (msg_id,))
        if not row:
            return

        # Delete the edited message and everything that came after it.
        # ⚠️ THE rowid HALF OF THIS PREDICATE IS WHAT STOPS IT DELETING THE PAST.
        # `created_at` is second-granular and `cli/store.save_history()` stamps a
        # whole rewritten window with ONE timestamp, so `created_at >= ?` matched
        # every row of a resumed conversation — editing the newest message wiped
        # the entire chat. The order it deletes in is `core.context.MSG_ORDER`,
        # spelled as a row-value comparison so the two halves cannot disagree.
        exe(
            "DELETE FROM messages WHERE chat_id=? AND (created_at, rowid) >= (?, ?)",
            (chat_id, row["created_at"], row["rid"]),
        )

        # Tell the UI to reload messages for this chat
        emit("messages_truncated", {"chat_id": chat_id, "from_msg_id": msg_id})

        # Re-run the agent with the new text
        _run_turn(socketio, sid, chat_id, new_text, sid, term_id,
                  model_key, mode_key, socketio, [])

    # ── Terminal: run a raw command (no AI) ───────────────────────────────────

    @guarded("run_raw_command")
    def on_raw(data):
        sid     = request.sid
        cmd     = data.get("command", "").strip()
        term_id = data.get("term_id", "t1")
        if cmd:
            weblog.info("term", f"$ {cmd[:90]}  {weblog.dim(term_id)}")
            threading.Thread(
                target=stream_command,
                args=(cmd, sid, term_id, socketio),
                daemon=True,
            ).start()

    # ── Terminal: inject stdin into a running process ─────────────────────────

    @guarded("terminal_input")
    def on_stdin(data):
        sid     = request.sid
        term_id = data.get("term_id", "t1")
        text    = data.get("text", "")
        send_stdin(sid, term_id, text, socketio)

    # ── Terminal: kill running process ────────────────────────────────────────

    @guarded("terminal_kill")
    def on_kill(data):
        kill_proc(request.sid, data.get("term_id", "t1"))

    # ── Personal Intelligence Layer (real-time, offline) ──────────────────────
    # Low-latency channel for live ghost-text autocomplete and the silent passive
    # learning signals behind it. The prediction path is fully local (tries /
    # n-grams / ranking) — it NEVER calls the model, so it's safe per-keystroke.
    # Every handler is best-effort; the PIL facade never raises.

    @guarded("pil_predict")
    def on_pil_predict(data):
        """Return a ghost-text suggestion for the current input. Never mutates and
        never auto-inserts — the front-end shows gray text the user may accept with
        Tab / Right-Arrow. Echoes `req` so the client can drop stale responses."""
        sid = request.sid
        d = data or {}
        try:
            from agent2.core import pil
            suggestion = pil.predict(
                d.get("text", "") or "",
                project=(d.get("project") or "").strip(),
                lang=(d.get("lang") or "").strip(),
            )
        except Exception:
            suggestion = None
        emit("pil_suggestion", {
            "req": d.get("req"),
            "text": d.get("text", ""),
            "suggestion": suggestion,
        }, room=sid)

    @guarded("pil_feedback")
    def on_pil_feedback(data):
        """Passive behavioural signal — accept / ignore / delete. Silent: the agent
        never asks whether a prediction was right, it only observes what the user
        does. No response is emitted."""
        d = data or {}
        action = (d.get("action") or "").strip().lower()
        suggestion = d.get("suggestion", "") or ""
        context = d.get("context", "") or ""
        project = (d.get("project") or "").strip()
        try:
            from agent2.core import pil
            if action == "accept":
                pil.observe_accept(suggestion, context=context, project=project)
            elif action == "ignore":
                pil.observe_ignore(suggestion, context=context, project=project)
            elif action == "delete":
                pil.observe_delete(suggestion, context=context, project=project)
        except Exception:
            pass
