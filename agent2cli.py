#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2cli.py  —  Agent 2 CLI
────────────────────────────
Run via:  python run.py --cli
      or: venv/bin/python agent2cli.py   (after run.py setup)

Keys are stored in agent2.db (shared with the Web UI). No .env is used.
Key rotation: if one key hits quota, the next is tried automatically.

Commands:
  /help          show all commands
  /addapi        add an API key to the database
  /model [name]  switch model
  /mode  [name]  switch mode (fast | pro | thinking)
  /clear         clear conversation (start fresh)
  /shrink        summarize & shrink history manually
  /scan <path>   scan and analyze entire project
  /history       show recent messages
  /load          load the last conversation from this directory (CLI only)
  /pause         pause the current conversation (resume later)
  /resume        resume a previous conversation (↑/↓ picker)
  /clearhistory  clear message history
  /memory        list saved memories (this project + shared)
  /addmem <txt>  add a memory for this project (--shared for every project)
  /run <cmd>     run a shell command directly
  /read <file>   read a file
  /search <q>    web search
  /exit          quit

────────────────────────────────────────────────────────────────────────────
Once 3,398 lines, now a ~1,720-line entry point over the `agent2/cli/` package.
The agent loop, the `cmd_*` handlers, `process_turn` and `main` stay HERE.

This file is launched BY PATH as a subprocess and is imported by nothing — so
until test_cli.py it had ZERO coverage. The characterization net went down FIRST
(commit 5a5e4d8), so the package split is provably behaviour-preserving.

⚠️ THEME COLOURS LIVE ON ONE SHARED OBJECT, `P` — NEVER AS MODULE GLOBALS.
`/theme` and `/color` repaint at runtime and helpers read lazily. In a package,
`from agent2.cli.theme import PU` binds a COPY: that module then renders the
stale colour forever, with no exception and no traceback. `P.PU` cannot have that
bug, and `_Palette.__slots__` makes `P.PUU = …` an AttributeError rather than a
dead attribute. The indirection is the failsafe, not style. (A guard test asserts
that no CLI function rebinds the name `P`.)

`GR`/`YW`/`RD`/`WH` ARE DELIBERATELY NOT ON THE PALETTE — they are semantic
(success/warning/error/bright) and must stay fixed across themes so a status cue
never changes meaning. An accent override must leave them alone.

⚠️ PALETTE TESTS ASSERT THROUGH THE RENDERING HELPERS (`pu()`, `cy()`), NEVER BY
READING A VALUE. Reading directly still passes after the snapshot bug — the one
break that actually matters.

⚠️ TWO TAUTOLOGY TRAPS WERE FOUND HERE AND MUST NOT COME BACK.
  1. A test iterating `_SHARED_TOOLS` and comparing it to itself stayed green
     while names were dropped. It is now pinned against `_build_tools()` — the
     list the model is actually TOLD exists.
  2. A three-channel palette assertion compared the channels as ONE tuple, which
     hid a stopped `CY` repaint behind a moving `ACCENT`. Each channel is
     asserted separately now.

⚠️ THIS FILE MUST NEVER GROW ITS OWN KeyRotator AGAIN.
Its ~90-line copy (1) always returned the first active key, burning key 1 to
quota, (2) never recorded usage, so `/keys` and the web panel disagreed, and
(3) persisted its pin to a `cli_pinned_key` setting nothing else read.
`test_cli_uses_the_shared_rotator` asserts IDENTITY, so constructing a second
`KeyRotator()` fails it.

⚠️ `config.MODELS`/`config.MODES` ARE THE ONLY DECLARATION, INCLUDING FOR THE CLI.
This file used to carry literal twins plus DEFAULT_MODEL/DEFAULT_MODE, so adding
a model reached the Web UI and was invisible here. It now imports them and
flattens {"api","label","group"} → the api string at ONE place.
`detect_shell`/`shell_argv`/`SHELL_*` were a byte-identical second copy, imported
the same way.
  * The `except` branch is a FALLBACK COPY, and it IS TESTED. The CLI must stay
    importable enough for `--help` on a broken install, but an untested copy
    would just move the drift somewhere nothing looks.
  * ⚠️ `test_cli_defaults_match_config` is ONE-DIRECTIONAL BY DESIGN.
    `cli.DEFAULT_MODEL` *is* `config.DEFAULT_MODEL`, so a config-side change
    moves both and that assertion cannot see it — the fallback test owns that
    direction. This one owns a re-declared local default. Neither is redundant.

THE CLI CREATES NO CHAT ROW UNTIL THERE IS SOMETHING TO SAVE. `_CLI_CHAT` starts
as None; anything reading it must handle that — use `_bind_chat(id)`, never
`_CLI_CHAT.update(...)`. The Web UI is the opposite: it creates up front.

(test_cli.py — 47 tests, sabotage-verified)
"""

import os, sys, re, json, shutil, threading, time, platform
import subprocess, urllib.request, urllib.parse
from pathlib import Path
from datetime import datetime

# ── The cli package ────────────────────────────────────────────────────────────
# This module is the ENTRY POINT; the implementation lives in `agent2/cli/`,
# alongside `agent2/server/` (the Web surface) and `agent2/llm/` (model access).
#
# ⚠️ The one rule that makes the split safe: anything MUTATED at runtime is
# reached through an object (`P.PU`, `S.chat`), never bound by name across a
# module boundary. `from .theme import PU` would bind a COPY and this module
# would render the stale colour forever, with no exception and no traceback.
# Read-only flags settled at import (`_RICH`, `_DB_OK`, `SHELL_BIN`, …) are never
# rebound, so importing those by name is safe — that is what `env` is for.
from agent2.cli.env import (
    ROOT, DATA_DIR, HST_FILE, MEM_FILE, PT_HISTORY,
    OS_NAME, IS_WIN, IS_MAC, fix_windows_console,
    Console, Markdown, Panel, Syntax, Table, Text,
    Progress, SpinnerColumn, TextColumn, rbox, _RICH, _con,
    PromptSession, FileHistory, HTML, Style, Application, KeyBindings,
    Layout, HSplit, Window, FormattedTextControl, Completer, Completion,
    AutoSuggestFromHistory, _PTK, _PTK_IMPORT_ERROR,
    genai, gtypes, _GENAI, _GENAI_IMPORT_ERROR,
    _burp, _BURP_OK,
    _mcp_registry, _MCP_OK,
    _db_qall, _db_exe, _db_init, _db_list_keys, _db_add_key, _db_remove_key,
    _DB_OK, MAX_AGENT_ITERS, _MAX_RETRIES,
    _make_client, _call_with_retry, _classify_net_error,
    _is_blank_reply, _blank_reply_notice, _RESILIENCE_OK,
    _core_memory, _core_rules, _CORE_OK,
)
from agent2.cli.theme import (
    R, B, D, GR, YW, RD, WH, _Palette, P,
    THEMES, DEFAULT_THEME, ACCENT_CHOICES,
    apply_theme, apply_accent, load_theme_from_settings,
    _p, ok, warn, err, dim, pu, cy,
)
from agent2.cli.models import (
    MODELS, MODES, DEFAULT_MODEL, DEFAULT_MODE,
    _FALLBACK_MODELS, _FALLBACK_MODES,
    _FALLBACK_DEFAULT_MODEL, _FALLBACK_DEFAULT_MODE,
    detect_shell, shell_argv, SHELL_BIN, SHELL_LABEL, SHELL_FLAG,
)
from agent2.cli.keyring import (
    KeyRotator, _NullRotator, _rotator, _ROTATOR_OK,
    load_keys, save_key_to_env,
)
from agent2.cli.state import (
    S, cancel_event as _cancel_event, exit_event as _exit_event,
    proc_lock as _proc_lock, DOUBLE_TAP_WINDOW,
    register_proc as _register_proc, clear_proc as _clear_proc,
    kill_active_proc as _kill_active_proc,
    cancelled, trigger_cancel as _trigger_cancel,
)
from agent2.cli.store import (
    _CTX_OK, _PIL_OK, _WS_OK, _WSViolation, _core_ctx, _core_ws, _pil,
    _msgs_to_history, add_mem, load_mems, load_rules, save_mems,
    bind_chat as _bind_chat, ensure_chat as _ensure_chat,
    last_chat_for_cwd, load_last_conversation, prompt_dir_name, save_history,
    load_last_gemini_model, load_last_model, save_last_model,
)
from agent2.cli.render import (
    SLASH_COMMANDS, _render_markdown_plain, _short_path, hr, print_agent_reply,
    print_banner, print_help, print_plan, print_tool_call, status_line, tw,
)
from agent2.cli.runtime import (
    InputController, Spinner, run_cmd_stream,
    interrupt as _interrupt,
)
from agent2.cli.prompt import build_sys_prompt
# Task 18/19. Imported at module scope, not inside the `/model` branch: the picker
# hint, the `/model routing` subcommand and the turn's own `choose()` call all read
# it, and a local import in three places is three chances for one of them to be the
# one that quietly falls back to no routing.
from agent2.llm import router as _router
from agent2.cli.tooling import (
    MAX_FILE, _SHARED_TOOLS, _build_tools, _impl_multi_edit, _impl_plan,
    _impl_read, _impl_save_mem, _impl_scan_project, _impl_search, _impl_write,
    dispatch_tool,
)
from agent2.cli.interactive import (
    SlashCompleter, build_mode_choices, build_model_choices, get_prompt_style,
    model_label, read_input,
)
from agent2.cli.palette import ephemeral_picker, ephemeral_toggle_menu
from agent2.cli import diffview
from agent2.cli import taskview
from agent2.cli import tooling
from agent2.core import tasks as _tasks_mod
from agent2.core import recovery as _recovery
from agent2.cli import ux
from agent2.cli import palette
from agent2.cli import statusbar

fix_windows_console()


# ── Shrink History ─────────────────────────────────────────────────────────────
def shrink_history_agent(history: list, model_key: str, keep: int = 10, manual: bool = False) -> list:
    if not manual and len(history) < 100:
        return history
    
    if manual:
        status_line("Manually shrinking history...", "info")
    else:
        status_line("History reached 100 messages. Summarizing to save tokens...", "info")
        
    client, key, label = _rotator.get()
    if not client: return history[-50:] # fallback
    
    api_model = MODELS.get(model_key, MODELS[DEFAULT_MODEL])
    
    text_to_summarize = ""
    for h in history[:-keep]:
        role = "User" if h["role"] == "user" else "Agent"
        text_to_summarize += f"{role}: {h['content']}\n\n"
        
    prompt = f"Please provide a concise but comprehensive summary of the following conversation history. Retain key facts, decisions, and context.\n\n{text_to_summarize}"
    
    try:
        resp = client.models.generate_content(model=api_model, contents=prompt)
        summary = resp.text
        
        new_history = [{"role": "assistant", "content": f"**[System: History Summary]**\n{summary}", "ts": datetime.now().isoformat()}]
        new_history.extend(history[-keep:])
        return new_history
    except Exception as ex:
        status_line(f"Failed to shrink history: {ex}", "warning")
        return history[-50:] # fallback

# ── Model fallback + last-model persistence ────────────────────────────────────
class ModelUnavailable(Exception):
    """Raised by an agent loop when a model can't serve the turn and the caller
    should fall back to another model. `reason` ∈ {unauthenticated, exhausted,
    invalid_model}."""
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


def _classify_model_error(msg: str) -> str | None:
    """Map an error message to a fallback reason, or None if not fallback-worthy."""
    m = (msg or "").lower()
    if any(k in m for k in ("401", "403", "unauthenticated", "unauthorized",
                            "invalid api key", "api key not valid", "permission",
                            "unauthorized_client")):
        return "unauthenticated"
    if any(k in m for k in ("429", "quota", "exhausted", "resource_exhausted",
                            "rate limit", "rate-limit", "overloaded")):
        return "exhausted"
    if any(k in m for k in ("not found", "does not exist", "no such model",
                            "unsupported", "model_not_found", "invalid model")):
        return "invalid_model"
    return None


# ── Blank-reply recovery ───────────────────────────────────────────────────────
def _retry_text_only_cli(client, api_model, context, cfg_kw: dict) -> str:
    """Re-ask Gemini for TEXT ONLY after it returned a blank/filler reply.

    Mirrors agent2/agent.py's `_retry_without_tools`. On a short conversational
    turn ("hi", "thanks") a thinking-capable model can spend the whole
    `max_output_tokens` budget thinking and return no text part — the loop's old
    fallback printed a literal "Done." at the user. Dropping the tool schemas and
    the thinking budget gives the model nothing to call and room to just answer.

    Returns the recovered text, or "" if this attempt is unusable too. Never
    raises — recovery is best-effort.
    """
    retry_kw = {k: v for k, v in cfg_kw.items()
                if k not in ("tools", "tool_config", "thinking_config",
                             "max_output_tokens")}
    retry_kw["max_output_tokens"] = 1024

    def _ask(kwargs: dict) -> str:
        resp = _call_with_retry(lambda: client.models.generate_content(
            model=api_model, contents=context,
            config=gtypes.GenerateContentConfig(**kwargs)))
        cand = resp.candidates[0] if resp.candidates else None
        if not cand or not cand.content:
            return ""
        out = []
        for p in (cand.content.parts or []):
            try:
                if p.text and not getattr(p, "thought", False):
                    out.append(p.text)
            except Exception:
                pass
        return "\n".join(out).strip()

    # Prefer thinking fully off (2.5 Flash honours budget=0); some models reject a
    # zero budget, so fall back to letting the model decide.
    try:
        kw = dict(retry_kw)
        kw["thinking_config"] = gtypes.ThinkingConfig(thinking_budget=0)
        text = _ask(kw)
        if not _is_blank_reply(text):
            return text
    except Exception:
        pass
    try:
        text = _ask(retry_kw)
        return "" if _is_blank_reply(text) else text
    except Exception:
        return ""


def _spinner_label(base: str):
    """Wrap a static spinner line so it follows the turn's stages — item 8.

    Returns a CALLABLE, which is what `runtime.Spinner` invokes per frame. That is
    how "Thinking…" becomes `Executing Tools · pytest · 3.4s` without the spinner
    knowing anything about stages.

    ⚠️ `base` is the fallback, not decoration. Before the turn sets its first stage
    — and on any failure inside the tracker — the callable returns `base`, so the
    line is exactly what it was before item 8 rather than going blank.

    ⚠️ IT MUST DO NO I/O. It is called ~12×/s on the render thread;
    `ProgressStages.label()` is pure string formatting for this reason.
    """
    def _label() -> str:
        try:
            stage_line = ux.progress_stages.label()
            return stage_line if stage_line else base
        except Exception:
            return base
    return _label


def _print_turn_recap(history: list, mark: int | None = None):
    """End-of-turn recap — items 8 and 10.

    Shows the stage trail and a file-change summary when the turn actually did
    something. A plain question with no tools gets no recap — printing a stage
    list after "what is 2+2" is exactly the noise item 20 rules out.

    ⚠️ *mark* SCOPES THIS TO THE TURN, AND WITHOUT IT THE RECAP IS CUMULATIVE.
    `diffs.store` is session-long and nothing in the repo clears it, so reading
    `.all()` here made turn 5 report every file touched since launch: "4 files
    changed" after a turn that changed one. The Web UI never had the bug because
    `core/progress.TurnProgress` keeps a per-turn list — same turn, two different
    totals, each surface looking right on its own. `store.mark()` is taken in
    `process_turn` before the worker starts and `store.since()` reads the tail, so
    none of the six `store.add` call sites had to learn about turns.

    ⚠️ The summary goes through `diffview.render_summary`, which is the CLI's ONE
    renderer for this row. `ux.print_file_summary` used to be a second one, called
    only from here; it was removed rather than left as a fork.
    """
    try:
        from agent2.core import diffs
        changes = diffs.store.all() if mark is None else diffs.store.since(mark)
        summary = diffs.summarize(changes)

        # Print the recap only if files changed OR commands ran. A turn with
        # neither is just text Q&A and needs no recap.
        if summary["files"] > 0:
            print()
            ux.progress_stages.print_trail()
            # No `print()` here: `render_summary` opens with its own blank line,
            # and two would push the trail away from what it is summarising.
            diffview.render_summary(changes)
    except Exception:
        pass


# ── Agent loop ─────────────────────────────────────────────────────────────────
def run_agent(
    user_msg:  str,
    history:   list,
    model_key: str,
    mode_key:  str,
    send_msg:  str | None = None,
) -> list:
    """One full agentic turn. Returns updated history.

    *user_msg* is the ORIGINAL text the user typed — it is what gets stored in
    history/display. *send_msg* (when given) is the offline-PIL-enhanced copy
    that is actually sent to the model; when None the original is sent as-is.
    """

    if not _GENAI:
        status_line("google-genai is not installed. Run: pip install google-genai", "error")
        return history

    client, key, label = _rotator.get()
    if not client:
        status_line("No API keys found. Run:  python run.py --addapi  or type /addapi", "error")
        return history

    api_model = MODELS.get(model_key, MODELS[DEFAULT_MODEL])
    mode_cfg  = MODES.get(mode_key,  MODES[DEFAULT_MODE])

    # Burp tools are only offered when the user has explicitly connected with
    # `/burp connect`. We NEVER auto-connect here — connecting on every turn was
    # slow and surprising. If a live session exists, expose its tools.
    burp_decls = []
    if _BURP_OK and _burp and _burp.is_connected():
        burp_decls = _burp.gemini_declarations()

    # Task 8: the same rule for every other MCP server — offered when live, never
    # dialled from here. `/mcp` is the CLI's connect surface, exactly as `/burp`
    # is Burp's.
    mcp_bridges = _mcp_registry.extra_bridges() if (_MCP_OK and _mcp_registry) else []
    mcp_decls = _mcp_registry.gemini_declarations_for(mcp_bridges) if mcp_bridges else []

    agent_tools = [_build_tools()]
    if burp_decls:
        agent_tools.append(gtypes.Tool(function_declarations=burp_decls))
    if mcp_decls:
        agent_tools.append(gtypes.Tool(function_declarations=mcp_decls))

    # Generation config
    cfg_kw: dict = dict(
        system_instruction=build_sys_prompt(
            burp_tool_count=len(burp_decls),
            mcp_blocks=[b.prompt_block() for b in mcp_bridges],
        ),
        tools=agent_tools,
        tool_config=gtypes.ToolConfig(
            function_calling_config=gtypes.FunctionCallingConfig(mode="AUTO")
        ),
        max_output_tokens=mode_cfg["max_tokens"],
    )
    if mode_cfg.get("thinking") and model_key in ("2.5-pro", "3.1-flash", "3.1-pro", "2.5-flash"):
        try:
            cfg_kw["thinking_config"] = gtypes.ThinkingConfig(
                thinking_budget=mode_cfg.get("thinking_budget", 8000))
        except Exception: pass

    gen_cfg = gtypes.GenerateContentConfig(**cfg_kw)

    # Build context from history (last 20 turns)
    context = []
    for h in history[-20:]:
        if   h["role"] == "user":      context.append(gtypes.Content(role="user",  parts=[gtypes.Part(text=h["content"])]))
        elif h["role"] == "assistant": context.append(gtypes.Content(role="model", parts=[gtypes.Part(text=h["content"])]))

    # Recovery briefing (Task 3), ahead of the new message and never merged into
    # it: it is not something the user typed, and it must not land in `history`
    # (which is what gets saved and replayed). Consumed once — see core/recovery.
    _brief = _recovery.brief_for_chat((S.chat or {}).get("id", "") or "")
    if _brief:
        context.append(gtypes.Content(role="user", parts=[gtypes.Part(text=_brief)]))

    # Send the enhanced copy to the model (if any) but keep the original in
    # history so the display and future context show what the user actually typed.
    context.append(gtypes.Content(role="user", parts=[gtypes.Part(text=send_msg or user_msg)]))
    history.append({"role": "user", "content": user_msg, "ts": datetime.now().isoformat()})

    total_tokens = 0

    for _iteration in range(MAX_AGENT_ITERS):
        if cancelled():
            status_line("Cancelled.", "warning")
            return history
        mode_icon = mode_cfg["icon"]
        # Item 8: a CALLABLE, not a string — the line follows the turn's stages,
        # falling back to this static text before the first stage is set.
        spin = Spinner(_spinner_label(
            f"Agent 2  [{model_key} / {mode_key} {mode_icon}]  key #{label}"))
        spin.start()
        ux.progress_stages.set_stage("Calling Model")

        try:
            def _on_net_retry(attempt, delay, exc):
                status_line(
                    f"Network hiccup — retrying ({attempt}/{_MAX_RETRIES}) in {delay:.1f}s…",
                    "warning",
                )
            resp = _call_with_retry(
                lambda: client.models.generate_content(model=api_model, contents=context, config=gen_cfg),
                on_retry=_on_net_retry,
                should_stop=cancelled,
            )
        except KeyboardInterrupt:
            spin.stop()
            print()
            status_line("Interrupted.", "warning")
            return history
        except Exception as exc:
            spin.stop()
            if cancelled():
                status_line("Cancelled.", "warning")
                return history
            es = str(exc)
            reason    = _classify_model_error(es)
            is_quota  = reason == "exhausted"
            is_auth   = reason == "unauthenticated"
            _rotator.fail(key, quota=is_quota)

            if is_quota or is_auth:
                # Try the next API key on the SAME model first.
                # fail() above already deactivated this key on a quota error, so
                # get() cannot hand it straight back — but a non-quota auth error
                # only increments the strike count, so the `k2 != key` guard is
                # what actually prevents retrying the identical dead key. This is
                # the same idiom agent.py uses; the CLI used to call a local
                # next_active() helper, which meant two rotation policies.
                c2, k2, l2 = _rotator.get()
                if c2 and k2 != key:
                    tag = "Quota" if is_quota else "Auth"
                    status_line(f"{tag} issue on key #{label} — switching to key #{l2}", "warning")
                    client, key, label = c2, k2, l2
                    continue
                # No keys left → let the caller fall back to another model.
                raise ModelUnavailable(reason, es)
            if reason == "invalid_model":
                raise ModelUnavailable("invalid_model", es)
            hint = ("  (temporary network/server issue — retried automatically)"
                    if _classify_net_error(exc) == "transient" else "")
            status_line(f"API Error ({model_key}): {es}{hint}", "error")
            return history
        finally:
            spin.stop()

        # The network call is blocking and can't be aborted mid-flight, so the
        # moment it returns we check for a cancel and bail before parsing,
        # printing, or running any tools. This stops the daemon worker thread
        # from "thinking and processing" after the user already interrupted.
        if cancelled():
            status_line("Cancelled.", "warning")
            return history

        # Parse response
        try:
            candidate = resp.candidates[0] if resp.candidates else None
            if not candidate or not candidate.content:
                fr = getattr(candidate, "finish_reason", "?") if candidate else "none"
                status_line(f"Empty response (finish_reason={fr}). Try /model 2.5-flash", "warning")
                return history
            parts = candidate.content.parts or []
        except Exception as ex:
            status_line(f"Parse error: {ex}", "error")
            return history

        func_calls: list = []
        texts:      list = []
        for p in parts:
            try:
                if p.function_call and p.function_call.name: func_calls.append(p.function_call)
                elif p.text and not getattr(p, "thought", False): texts.append(p.text)
            except Exception: pass

        # Tokens
        try:    tok = getattr(resp.usage_metadata, "total_token_count", 0) or 0
        except: tok = 0
        total_tokens += tok
        if tok:
            # Attribute the spend to the key that paid it. The CLI used to only
            # PRINT this number and throw it away, so `/keys` and the Web key
            # panel reported different usage for the same keys and a CLI-only
            # user saw zeros forever. record_usage batches the SQLite write.
            if label:
                try:
                    _rotator.record_usage(label, tok)
                except Exception:
                    pass
            if _RICH: _con.print(f"  [dim]tokens: {total_tokens:,}[/]")
            else:     print(f"  {D}tokens: {total_tokens:,}{R}")

        # Interim text (before tool calls)
        if texts and func_calls:
            print()
            for t in texts: print(f"  {D}{t[:200]}{R}")

        # Tool calls
        if func_calls:
            context.append(gtypes.Content(role="model",
                parts=[gtypes.Part(function_call=fc) for fc in func_calls]))
            tool_result_parts = []

            for fc in func_calls:
                if cancelled():
                    status_line("Cancelled.", "warning")
                    return history
                name = fc.name
                args = dict(fc.args)
                print()

                if name == "run_command":
                    cmd  = args.get("command", "")
                    desc = args.get("description", "Running…")
                    cwd  = args.get("cwd", None)
                    print_tool_call(name, desc, cmd)
                    ux.progress_stages.set_stage("Executing Tools")
                    ux.progress_stages.set_detail(cmd[:40])
                    ux.command_queue.add(cmd[:30], "running")
                    ux.activity_feed.log(f"Running {cmd[:50]}")
                    # Task 4: the execution is attributed to the running task, so
                    # the command registry can answer "what is this task doing?"
                    _cctx = tooling.tool_ctx()
                    out, err, rc, dur = run_cmd_stream(
                        cmd, cwd, task_id=_cctx.current_task_id(),
                        session_id=_cctx.existing_task_session())
                    ux.command_queue.add(cmd[:30], "completed" if rc == 0 else "failed")
                    # The result card reports stdout AND stderr separately with the
                    # exit code and wall-clock duration — item 6.
                    ux.print_command_result(cmd, out, err, rc, dur)
                    # The model still needs stderr: a command that failed with a
                    # message only on stderr would otherwise look like silent failure.
                    result  = {"output": out[:3000], "stderr": err[:1500],
                               "returncode": rc, "success": rc == 0,
                               "duration": round(dur, 2)}
                    # run_command bypasses `tools.dispatch_tool` (the shared
                    # checkpoint chokepoint), so it records its own sub-step.
                    tooling.tool_ctx().note_tool("run_command", ok=rc == 0,
                                                 detail=cmd)
                else:
                    labels = {
                        "read_file":        f"📖 Reading: {_short_path(args.get('path','?'))}",
                        "write_file":       f"✍ Writing: {_short_path(args.get('path','?'))}",
                        "scan_project":     f"🔍 Scanning: {_short_path(args.get('path','?'))}",
                        "list_dir":         f"📂 Listing: {_short_path(args.get('path','?'))}",
                        "delete_file":      f"🗑 Deleting: {_short_path(args.get('path','?'))}",
                        "grep_search":      f"🔎 Searching: /{args.get('pattern','?')}/",
                        "multi_edit_files": f"✍ Editing {len(args.get('edits',[])) if isinstance(args.get('edits'), list) else '?'} file(s)",
                        "web_search":       f"🌐 Web search: {args.get('query','?')}",
                        "save_memory":      "🧠 Saving memory",
                        "emit_plan":        f"📋 Planning: {args.get('title','?')}",
                        "update_todo":      "✅ Updating task list",
                    }
                    print_tool_call(name, labels.get(name, name))

                    # Snapshot BEFORE dispatch — write_file overwrites, so after
                    # this call the old content is gone and no diff is possible.
                    _changes = diffview.capture_for(name, args)

                    result = dispatch_tool(name, args)

                    # Pretty display for Burp MCP tools
                    if name.startswith("burp_"):
                        if result.get("success"):
                            preview = str(result.get("output", ""))[:1000]
                            status_line(f"Burp → {name}", "success")
                            if _RICH: _con.print(f"[dim]{preview}[/]")
                            else:     print(f"{D}{preview}{R}")
                        elif "error" in result:
                            status_line(f"Burp error: {result['error']}", "error")
                    elif name == "read_file" and "content" in result:
                        preview = result["content"][:600]
                        lang    = Path(args.get("path","")).suffix.lstrip(".")
                        if _RICH:
                            try:   _con.print(Syntax(preview, lang or "text", theme="monokai", line_numbers=True))
                            except: _con.print(f"[dim]{preview}[/]")
                        else: print(f"{YW}{preview}{R}")
                    elif name == "write_file" and result.get("success"):
                        # The diff IS the confirmation \u2014 it shows what changed, not
                        # just that something did. Falls back to the old status line
                        # when no diff could be computed (binary, unreadable, etc).
                        #
                        # preview=True keeps the echo short (3 rows for a new file,
                        # 10 around the first change for an edit); Ctrl+B opens the
                        # whole file. Never flood the terminal is spec item 20.
                        if _changes:
                            for _ch in _changes:
                                diffview.render_change(_ch, preview=True)
                                diffview.store.add(_ch)
                            diffview.render_summary(_changes)
                        else:
                            status_line(f"Written \u2192 {result.get('path','?')}  ({result.get('lines',0)} lines)", "success")
                    elif name == "update_todo":
                        # ⚠️ There was NO branch here, which is why the checklist
                        # was invisible in the CLI: the user saw the tool banner
                        # and nothing else. taskview prints only when the VISIBLE
                        # state changed, so the model re-sending an identical list
                        # does not stack duplicate panels down the terminal.
                        taskview.render_for_result(result)
                    elif name == "scan_project" and "file_tree" in result:
                        tree = result["file_tree"][:2000]
                        cnt  = result.get("file_count", 0)
                        status_line(f"Scanned {cnt} files", "success")
                        if _RICH: _con.print(f"[dim]{tree}[/]")
                        else:     print(f"{D}{tree}{R}")
                    elif name == "multi_edit_files" and "results" in result:
                        # Same as write_file: the diff IS the confirmation, and the
                        # same windowing applies. A multi-edit touches several files
                        # at once, so this is where an unwindowed echo would flood
                        # the terminal hardest.
                        if _changes:
                            for _ch in _changes:
                                diffview.render_change(_ch, preview=True)
                                diffview.store.add(_ch)
                            diffview.render_summary(_changes)
                        else:
                            # Fallback when diff capture failed — parse the tool's own summary.
                            for line in result["results"].split("\n"):
                                if "Successfully" in line:
                                    status_line(line, "success")
                                elif "not found" in line or "Error" in line:
                                    status_line(line, "error")
                                else:
                                    status_line(line, "info")
                    elif name == "delete_file" and result.get("success"):
                        # A delete is the one change you most want a record of —
                        # and until Task 11 it was the one change the CLI recorded
                        # WITHOUT showing. The browser has no such gate
                        # (`agent.py`'s emit is tool-agnostic), so the same delete
                        # printed a full diff block in the web UI and a single grey
                        # line here: one change, two claims about it.
                        #
                        # A deleted file's body is all `del` rows, and
                        # `preview_window()` caps a non-create at PREVIEW_EDIT, so
                        # deleting a 4,000-line file still prints 10 rows plus a
                        # "… N more lines" footer. It cannot flood.
                        for _ch in _changes:
                            diffview.render_change(_ch, preview=True)
                            diffview.store.add(_ch)
                        status_line(f"Deleted → {_short_path(args.get('path','?'))}", "warning")
                    elif name == "web_search" and "results" in result:
                        for res in result["results"][:3]:
                            if _RICH: _con.print(f"  [bold #60b8ff]{res.get('title','')[:70]}[/]\n  [dim]{res.get('snippet','')[:220]}[/]\n")
                            else:     print(f"  {P.CY}{res.get('title','')[:70]}{R}\n  {D}{res.get('snippet','')[:220]}{R}\n")
                    elif name == "save_memory" and result.get("saved"):
                        status_line("Memory saved", "success")
                    elif "error" in result:
                        status_line(f"Tool error: {result['error']}", "error")

                # Cap large string values so a big scan can't blow up context
                # (mirrors the Web UI's MAX_TOOL_OUTPUT truncation).
                safe_result = {
                    k: (v[:6000] + "\n…[truncated]" if isinstance(v, str) and len(v) > 6000 else v)
                    for k, v in result.items()
                }
                tool_result_parts.append(gtypes.Part(function_response=gtypes.FunctionResponse(
                    name=name, response=safe_result)))

            context.append(gtypes.Content(role="user", parts=tool_result_parts))

        else:
            # Final text response
            final = "\n".join(texts).strip()

            # A blank/filler reply is what made short conversational turns ("hi")
            # print a bare "Done.". Retry once with tools off and thinking
            # disabled so the whole budget goes to visible text.
            if _is_blank_reply(final):
                final = _retry_text_only_cli(client, api_model, context, cfg_kw) \
                    or _blank_reply_notice(getattr(candidate, "finish_reason", None))

            print_agent_reply(final)
            history.append({"role": "assistant", "content": final,
                            "ts": datetime.now().isoformat()})
            return history

    status_line(f"Reached max iterations ({MAX_AGENT_ITERS}).", "warning")
    return history

# ── /burp → /mcp burp (retired alias) ──────────────────────────────────────────
def cmd_burp(user_input: str):
    """⚠️ RETIRED, NOT DELETED — `/burp …` now forwards to `/mcp burp …`.

    `/burp` was the only MCP command for as long as Burp was the only MCP server,
    so it is in users' muscle memory, in the README and in this repo's docs trail.
    Deleting it would meet a typed `/burp connect` with "unknown command" and no
    hint that the capability still exists two words away (rule 28 — do not
    silently remove a feature). Forwarding keeps every old spelling working,
    prints where it went, and leaves exactly one implementation to maintain.
    """
    parts = user_input.split(maxsplit=1)
    rest = parts[1].strip() if len(parts) > 1 else ""
    status_line(f"/burp is now /mcp burp — running: /mcp burp {rest or 'status'}", "info")
    cmd_mcp(f"/mcp burp {rest}".rstrip())


# ── /mcp command (manage every MCP server) ─────────────────────────────────────
def _mcp_apply(bridge, want_on: bool) -> tuple[bool, str]:
    """Bring one bridge to *want_on*. Returns (ok, one-line result).

    ⚠️ THE TOGGLE IS THE CONNECTION, not just a saved preference. `/mcp` showing
    "Burp: ON" while nothing is connected would be a lie the user can only catch
    by asking the agent for a Burp tool and watching it fail, so turning a server
    on here dials it, and the returned line reports what actually happened.
    """
    if want_on:
        bridge.set_auto_connect(True)
        if bridge.is_connected():
            return True, f"{bridge.LABEL}: already connected"
        okc, msg = bridge.connect()
        return okc, msg
    bridge.set_auto_connect(False)
    bridge.disconnect()
    return True, f"Disconnected from {bridge.LABEL} MCP."


def _mcp_print_status() -> None:
    """The plain (non-menu) status table — also what `/mcp status` prints."""
    for s in _mcp_registry.statuses():
        state = ok("connected") if s["connected"] else err("disconnected")
        auto = "auto-connect on" if s["enabled"] else "auto-connect off"
        print(f"  {s['label']}: {state}  {D}({s['url']} · {auto}){R}")
        print(f"    Tools: {s['tool_count']}   mcp installed: {s['mcp_installed']}")
        if s["last_error"]:
            print(f"    {D}last error: {s['last_error'][:120]}{R}")


def _mcp_list_tools(bridges) -> None:
    """Print every tool exposed by *bridges*, or say why there are none."""
    any_tools = False
    for b in bridges:
        tools = b.list_tools()
        if not tools:
            continue
        any_tools = True
        status_line(f"{len(tools)} {b.LABEL} tools available:", "success")
        for t in tools:
            print(f"  {P.CY}{t['name']}{R}  {D}{(t['description'] or '')[:70]}{R}")
    if not any_tools:
        if len(bridges) == 1:
            status_line(f"No {bridges[0].LABEL} tools — not connected. "
                        f"Try: /mcp {bridges[0].SERVER_KEY} connect", "warning")
        else:
            status_line("No MCP tools — nothing is connected. Open /mcp to turn a server on.",
                        "warning")


def _mcp_config(bridge) -> None:
    """`/mcp <server> config` — edit URL, port and security key for one server.

    ⚠️ EVERY PROMPT IS "ENTER KEEPS THE CURRENT VALUE", INCLUDING THE KEY, and the
    key prompt is the reason the whole editor is shaped this way. It shows bullets
    from `state.mask_secret()`, never the stored value, so someone changing a port
    over a shared screen does not read their ZAP key out loud — and because blank
    means "unchanged", that same person cannot wipe the credential by tabbing
    through. Clearing stays available, spelled out loud as the literal word
    `clear`, because destroying a credential should take an explicit sentence
    (rule 21).
    """
    from agent2.integrations import state as _mcp_state
    cfg = _mcp_state.config_for(bridge.SERVER_KEY,
                                url_default=bridge.DEFAULT_URL,
                                key_default=bridge.ENV_KEY)
    takes_key = bridge.SERVER_KEY == "zap"

    print()
    print(f"  {P.PU}{B}{bridge.LABEL} — MCP configuration{R}")
    print(f"  {D}Enter keeps the current value. Type 'clear' to reset a field.{R}")
    print()
    print(f"  URL        {P.CY}{cfg['url']}{R}  {D}({cfg['url_source']}){R}")
    print(f"  Port       {P.CY}{bridge.port or '-'}{R}")
    if takes_key:
        shown = cfg["key_masked"] or f"{D}not set{R}"
        print(f"  Key        {shown}  {D}({cfg['key_source']}){R}")
    if bridge.SETUP_HINT:
        print(f"  {D}{bridge.SETUP_HINT}{R}")
    print()

    try:
        new_url = input(f"  URL [{cfg['url']}]: ").strip()
        new_port = input(f"  Port [{bridge.port or '-'}]: ").strip()
        new_key = input("  Security key [unchanged]: ").strip() if takes_key else ""
    except (EOFError, KeyboardInterrupt):
        print()
        status_line("Configuration unchanged.", "info")
        return

    changes: list[str] = []
    if new_url:
        if new_url.lower() == "clear":
            bridge.set_url("")
            changes.append(f"URL → {bridge.url} (default)")
        else:
            bridge.set_url(new_url)
            changes.append(f"URL → {bridge.url}")
    if new_port:
        if new_port.lower() == "clear":
            status_line("Port follows the URL — clear the URL instead.", "warning")
        elif bridge.set_port(new_port):
            changes.append(f"Port → {bridge.port}  ({bridge.url})")
        else:
            status_line(f"Not a usable port: {new_port}", "error")
    if takes_key and new_key:
        if new_key.lower() == "clear":
            bridge.set_security_key("")
            changes.append("Security key cleared")
        else:
            bridge.set_security_key(new_key)
            changes.append("Security key updated")

    if not changes:
        status_line("Configuration unchanged.", "info")
        return

    print()
    status_line(f"{bridge.LABEL} configuration updated", "success")
    for line in changes:
        print(f"  {line}")

    # ⚠️ A LIVE SESSION IS NOT SILENTLY RECONNECTED. The new endpoint or key only
    # reaches the wire on the next connect, and reconnecting here would tear down
    # a session the user is mid-scan on to apply a setting they may still be
    # editing (rule 21). Saying so is the difference between a stale connection
    # and a confusing one.
    if bridge.is_connected():
        print(f"  {D}Still connected with the previous settings — "
              f"/mcp {bridge.SERVER_KEY} reconnect to apply.{R}")


def _mcp_health(scope) -> None:
    """`/mcp health` — the Task 10 report, plus [R]etry / [S]ettings on a failure.

        Burp
        ✓ Connected

        OWASP ZAP
        ✗ Connection failed

        [R] Retry
        [S] Settings

    ⚠️ THE VERDICT IS `registry.health()`'S, NOT THIS FUNCTION'S. `/api/health`
    and the web panel render the same three fields; deciding here that "no session"
    means "failed" would be a second opinion that disagrees with the endpoint the
    moment either learns a new state.

    ⚠️ THE OFFER IS ONLY PRINTED WHEN IT CAN BE ACCEPTED AND WOULD DO SOMETHING.
    No failing server means nothing to retry, and offering keys where no key can
    arrive (a piped stdin) is worse than offering nothing — the same rule
    `cli/runtime._StuckPrompt` follows. Unlike that prompt this one MAY block on
    `input()`: `/mcp health` is typed at the REPL with no command draining pipes
    behind it, so there is nothing here for a blocking read to starve.

    ⚠️ [R] RETRIES A CONNECTION, WHICH IS WHY IT IS SAFE TO OFFER AT ALL. Rule 21
    forbids blind retries of operations whose effects we cannot see; dialling an
    MCP server is idempotent and read-only — it opens a session and lists tools.
    It is deliberately NOT offered for anything that ran a scan.
    """
    rows = [r for r in _mcp_registry.health()
            if any(r["key"] == b.SERVER_KEY for b in scope)]
    if not rows:
        status_line("No MCP servers are configured in this build.", "warning")
        return

    print()
    for r in rows:
        mark = ok("✓") if r["state"] == "connected" else (
            err("✗") if not r["ok"] else f"{D}•{R}")
        print(f"  {B}{r['label']}{R}")
        print(f"  {mark} {r['text']}")
        if r["state"] == "connected" and r["tool_count"]:
            print(f"    {D}{r['tool_count']} tool(s){R}")
        elif r["detail"]:
            print(f"    {D}{r['detail'][:120]}{R}")
        print()

    failing = [r for r in rows if not r["ok"]]
    if not failing:
        return

    try:
        interactive = sys.stdin.isatty()
    except Exception:
        interactive = False
    if not interactive:
        names = ", ".join(f"/mcp {r['key']} reconnect" for r in failing)
        print(f"  {D}Retry with: {names}{R}")
        return

    print(f"  {D}[R] Retry   [S] Settings   [Enter] Close{R}")
    try:
        choice = input("  > ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return
    except OSError:                     # /dev/null or a closed handle — no key
        print()
        return

    if choice == "r":
        for r in failing:
            bridge = _mcp_registry.get(r["key"])
            if bridge is None:
                continue
            bridge.disconnect()
            okc, msg = _mcp_apply(bridge, True)
            status_line(msg, "success" if okc else "error")
        return
    if choice == "s":
        # One failure edits that server; several would mean picking one for the
        # user, and `_mcp_config` is deliberately per-server (URL, port and key
        # are per-server facts).
        if len(failing) == 1:
            bridge = _mcp_registry.get(failing[0]["key"])
            if bridge is not None:
                _mcp_config(bridge)
            return
        names = " · ".join(f"/mcp {r['key']} config" for r in failing)
        status_line(f"Which server? {names}", "warning")


def _mcp_usage() -> None:
    print(f"  {D}Usage: /mcp                        interactive on/off menu{R}")
    print(f"  {D}       /mcp status | list          every server{R}")
    print(f"  {D}       /mcp health                 health, with [R]etry{R}")
    print(f"  {D}       /mcp connect | disconnect   every server{R}")
    print(f"  {D}       /mcp <server> connect | disconnect | reconnect{R}")
    print(f"  {D}       /mcp <server> status | list | config | health{R}")
    keys = " · ".join(b.SERVER_KEY for b in _mcp_registry.bridges())
    if keys:
        print(f"  {D}       servers: {keys}{R}")


def cmd_mcp(user_input: str = "/mcp"):
    """/mcp — the ONE command for every MCP server (Burp, OWASP ZAP, …).

    Bare `/mcp` opens the interactive on/off menu. Everything else is
    `/mcp [server] <action>`, where omitting the server means "all of them":

        /mcp connect                 dial every configured server
        /mcp zap connect             dial one
        /mcp zap list                its tools
        /mcp zap config              edit URL / port / security key

    ⚠️ THE SERVER NAME IS RESOLVED THROUGH THE REGISTRY, NOT A LITERAL LIST. A
    hard-coded `("burp","zap")` here is the second declaration of the server table
    that `integrations/registry.py` exists to be: adding a third server would keep
    working everywhere except this parser, which would report it as an unknown
    action and print the usage line.
    """
    if not _MCP_OK or _mcp_registry is None:
        status_line("MCP bridges unavailable (mcp package missing). Run: pip install mcp",
                    "error")
        return

    bridges = _mcp_registry.bridges()
    if not bridges:
        status_line("No MCP servers are configured in this build.", "warning")
        return

    words = user_input.split()[1:]                       # drop "/mcp"
    target = None
    if words:
        target = _mcp_registry.get(words[0].strip().lower())
        if target is not None:
            words = words[1:]                            # it named a server
    action = (words[0].strip().lower() if words else "")
    scope = [target] if target is not None else list(bridges)

    # ── actions that apply to a named server or to all of them ──────────────────
    if action in ("connect", "on"):
        for b in scope:
            okc, msg = _mcp_apply(b, True)
            status_line(msg, "success" if okc else "error")
        return

    if action in ("disconnect", "off"):
        # ⚠️ DISCONNECT ALSO TURNS AUTO-CONNECT OFF, and that is not overreach.
        # `ensure_connected()` redials an enabled bridge at the start of the next
        # agent turn, so a disconnect that left the flag on would look like it did
        # nothing — the user drops the session and the next message silently
        # rebuilds it. `/mcp` and `/mcp <server> connect` turn it back on.
        for b in scope:
            _mcp_apply(b, False)
            status_line(f"Disconnected from {b.LABEL} MCP.", "info")
        return

    if action in ("reconnect", "restart"):
        for b in scope:
            b.disconnect()
            okc, msg = _mcp_apply(b, True)
            status_line(msg, "success" if okc else "error")
        return

    if action in ("list", "tools"):
        _mcp_list_tools(scope)
        return

    if action in ("health", "check", "doctor"):
        _mcp_health(scope)
        return

    if action in ("status", "st"):
        if target is None:
            _mcp_print_status()
        else:
            _mcp_print_one(target)
        _mcp_usage()
        return

    if action in ("config", "settings", "cfg"):
        # ⚠️ Config edits ONE server: URL, port and key are per-server facts, and
        # a bulk editor would ask for a single port to hand to every bridge.
        if target is None:
            status_line("Which server? e.g. /mcp zap config", "warning")
            _mcp_usage()
            return
        _mcp_config(target)
        return

    if action:
        status_line(f"Unknown: /mcp {' '.join(words)}", "error")
        _mcp_usage()
        return

    if target is not None:
        # `/mcp zap` with no action — show that server rather than guessing.
        _mcp_print_one(target)
        _mcp_usage()
        return

    _mcp_menu(bridges)


def _mcp_print_one(bridge) -> None:
    """One server's status block, including where its config came from."""
    from agent2.integrations import state as _mcp_state
    s = bridge.status()
    cfg = _mcp_state.config_for(bridge.SERVER_KEY, url_default=bridge.DEFAULT_URL,
                                key_default=bridge.ENV_KEY)
    state = ok("connected") if s["connected"] else err("disconnected")
    auto = "auto-connect on" if s["enabled"] else "auto-connect off"
    print(f"  {bridge.LABEL}: {state}  {D}({s['url']} · {auto}){R}")
    print(f"    Tools: {s['tool_count']}   mcp installed: {s['mcp_installed']}")
    if cfg["key_set"]:
        print(f"    Security key: {cfg['key_masked']}  {D}({cfg['key_source']}){R}")
    if s["last_error"]:
        print(f"    {D}last error: {s['last_error'][:120]}{R}")


def _mcp_menu(bridges) -> None:
    """The ephemeral on/off menu — bare `/mcp`."""
    items = []
    for b in bridges:
        s = b.status()
        hint = f"{s['url']}"
        if s["connected"]:
            hint += f" · connected, {s['tool_count']} tools"
        elif s["last_error"]:
            hint += f" · last error: {s['last_error'][:60]}"
        items.append({"key": b.SERVER_KEY, "label": b.LABEL, "hint": hint,
                      "on": bool(s["connected"] or b.enabled)})

    result = ephemeral_toggle_menu(
        "MCP Servers", items,
        subtitle="Turning a server ON connects to it now and on future sessions.",
        cancellable=True,
    )
    # ⚠️ None means Esc — the user backed out, so nothing is connected, nothing
    # is disconnected and nothing is written. `{}` would be a different answer.
    if result is None:
        status_line("MCP configuration unchanged.", "info")
        return

    before = {it["key"]: it["on"] for it in items}
    changed = {k: v for k, v in result.items() if before.get(k) != v}
    if not changed:
        status_line("MCP configuration unchanged.", "info")
        return

    lines: list[tuple[bool, str]] = []
    for key, want in changed.items():
        bridge = _mcp_registry.get(key)
        if bridge is None:
            continue
        lines.append(_mcp_apply(bridge, want))

    print()
    status_line("MCP configuration updated", "success")
    print()
    for s in _mcp_registry.statuses():
        on = s["connected"] or s["enabled"]
        print(f"  {s['label']}: {ok('ON') if on else err('OFF')}")
    for okc, msg in lines:
        if not okc:
            print(f"  {D}{msg}{R}")


# ── /addapi command (interactive, writes to agent2.db) ─────────────────────────
def cmd_addapi():
    keys = load_keys()
    print()
    if _RICH:
        _con.print(Panel("[bold #7c6af7]Add Gemini API Key[/]\nFree: [link=https://aistudio.google.com/app/apikey]aistudio.google.com/app/apikey[/link]",
                         border_style="#3a2a70"))
    else:
        print(f"  {P.PU}{B}Add Gemini API Key{R}")
        print(f"  Free key: https://aistudio.google.com/app/apikey\n")

    status_line(f"Keys currently stored: {len(keys)}", "info")
    for k in keys:
        col = GR if k["active"] else RD
        print(f"    {col}●{R}  #{k['label']}: {D}{k['key'][:14]}…{R}")
    print()

    while True:
        try:
            raw = input(f"  {P.PU}paste key (or Enter to cancel):{R} ").strip()
        except (EOFError, KeyboardInterrupt):
            print(); return
        if not raw:
            return
        raw = raw.replace(" ", "").replace("\n", "")
        ok_save, msg = save_key_to_env(raw)
        if ok_save:
            _rotator.reload()
            status_line(f"Key saved as #{msg}", "success")
            status_line(f"Total keys stored: {len(load_keys())}", "info")
            ans = input(f"  Add another? [y/N]: ").strip().lower()
            if ans != "y":
                break
        else:
            status_line(f"Could not save key: {msg}", "error")

# ── Main interactive loop ──────────────────────────────────────────────────────
def run_provider_agent_cli(user_msg: str, history: list, pid: str,
                           send_msg: str | None = None) -> list:
    """CLI agentic loop for a custom provider (OpenAI/Anthropic compatible).

    *user_msg* is the original text (stored in history); *send_msg* is the
    offline-PIL-enhanced copy actually sent to the provider (falls back to the
    original when None).
    """
    from agent2.llm import providers as _prov
    prov = _prov.get_provider(pid)
    if not prov:
        status_line("Custom provider not found. Use /provider list", "error")
        return history

    history.append({"role": "user", "content": user_msg,
                    "ts": datetime.now().isoformat()})
    # Burp tools are only offered if the user already ran `/burp connect`.
    burp_live = _BURP_OK and _burp and _burp.is_connected()
    # Task 8: and every other MCP server the user connected with `/mcp`. Parity
    # with the Gemini CLI path — switching to a custom provider must not silently
    # cost the user their MCP tools.
    _mcp_bridges = _mcp_registry.extra_bridges() if (_MCP_OK and _mcp_registry) else []
    system = build_sys_prompt(
        burp_tool_count=len(_burp.list_tools()) if burp_live else 0,
        mcp_blocks=[b.prompt_block() for b in _mcp_bridges],
    )
    fmt = prov.get("format", "openai")

    # Seed provider messages from text history
    messages = [{"role": ("user" if h["role"] == "user" else "assistant"),
                 "content": h["content"]}
                for h in history[-20:] if h["role"] in ("user", "assistant")]
    # Swap the enhanced copy in for the just-appended original user turn so the
    # provider sees the PIL-improved prompt while history keeps the original.
    if send_msg and send_msg != user_msg and messages and messages[-1]["role"] == "user":
        messages[-1]["content"] = send_msg

    for _ in range(MAX_AGENT_ITERS):
        if cancelled():
            status_line("Cancelled.", "warning")
            return history
        spin = Spinner(_spinner_label(f"{prov.get('model_id','model')} thinking…"))
        spin.start()
        ux.progress_stages.set_stage("Calling Model")
        try:
            result = _prov.chat(prov, messages, system)
        except Exception as exc:
            spin.stop()
            reason = _classify_model_error(str(exc))
            if reason:
                # Let agent_turn fall back to another model.
                raise ModelUnavailable(reason, str(exc))
            status_line(f"Provider error: {exc}", "error")
            return history
        finally:
            spin.stop()

        # Bail the instant the (blocking, un-abortable) request returns if the
        # user interrupted while it was in flight.
        if cancelled():
            status_line("Cancelled.", "warning")
            return history

        total = result.get("tokens", 0)
        if total:
            print(f"  {D}tokens: {total:,}{R}")
        tool_calls = result.get("tool_calls") or []

        if not tool_calls:
            final = (result.get("text") or "").strip()
            # Same blank-reply guard as the Gemini loop: retry once with no tool
            # schemas attached before falling back to an honest notice, so a bare
            # "hi" can never come back as a placeholder "Done.".
            if _is_blank_reply(final):
                if cancelled():
                    status_line("Cancelled.", "warning")
                    return history
                try:
                    r2 = _prov.chat(prov, messages, system, use_tools=False)
                    t2 = (r2.get("text") or "").strip()
                    final = t2 if not _is_blank_reply(t2) else _blank_reply_notice()
                except Exception:
                    final = _blank_reply_notice()
            print_agent_reply(final)
            history.append({"role": "assistant", "content": final,
                            "ts": datetime.now().isoformat()})
            return history

        if fmt == "anthropic":
            messages.append({"role": "assistant", "content": result.get("raw_content", [])})
            tr = []
            for tc in tool_calls:
                if cancelled():
                    status_line("Cancelled.", "warning")
                    return history
                out, ok = _run_tool_cli(tc["name"], tc["args"])
                tr.append({"type": "tool_result", "tool_use_id": tc["id"],
                           "content": out or "(no output)"})
            messages.append({"role": "user", "content": tr})
        else:
            messages.append(result.get("raw_assistant") or {
                "role": "assistant", "content": result.get("text", ""),
                "tool_calls": [{"id": tc["id"], "type": "function",
                                "function": {"name": tc["name"],
                                             "arguments": json.dumps(tc["args"])}}
                               for tc in tool_calls]})
            for tc in tool_calls:
                if cancelled():
                    status_line("Cancelled.", "warning")
                    return history
                out, ok = _run_tool_cli(tc["name"], tc["args"])
                messages.append({"role": "tool", "tool_call_id": tc["id"],
                                 "content": out or "(no output)"})

    status_line("Reached max iterations.", "warning")
    return history


def _checkpoint_stop(reason: str) -> None:
    """Stamp the CLI's running task as stopped, with a reason.

    ⚠️ Only the reason. The sub-step trail is written as the work happens
    (`core/tasks.py`), because a `SIGKILL` or a power cut runs no handler — a
    design that flushed progress here would lose exactly the crash it exists for.
    Silent: a failed breadcrumb must never turn an interrupted turn into an
    error the user has to read.
    """
    try:
        sess = tooling.tool_ctx().existing_task_session()
        if sess:
            _tasks_mod.interrupt(sess, reason)
    except Exception:
        pass


def _run_tool_cli(name: str, args: dict) -> tuple[str, bool]:
    """Execute a tool for the CLI provider loop, echoing to the terminal."""
    if name == "run_command":
        cmd = args.get("command", "")
        print_tool_call(name, args.get("description", "Running…"), cmd)
        ux.progress_stages.set_stage("Executing Tools")
        ux.progress_stages.set_detail(cmd[:40])
        ux.command_queue.add(cmd[:30], "running")
        ux.activity_feed.log(f"Running {cmd[:50]}")
        # Task 4: same attribution as the Gemini CLI loop above.
        _cctx = tooling.tool_ctx()
        out, err, rc, dur = run_cmd_stream(
            cmd, args.get("cwd"), task_id=_cctx.current_task_id(),
            session_id=_cctx.existing_task_session())
        ux.command_queue.add(cmd[:30], "completed" if rc == 0 else "failed")
        ux.print_command_result(cmd, out, err, rc, dur)
        # stderr is appended rather than dropped — this loop returns ONE string to
        # the provider, so a message that only reached stderr would be invisible.
        payload = out[:6000]
        if err.strip():
            payload = f"{payload}\n\n[stderr]\n{err[:1500]}"
        # run_command never reaches `tools.dispatch_tool`, so it records its own
        # checkpoint sub-step — same exception as in both web loops.
        tooling.tool_ctx().note_tool("run_command", ok=rc == 0, detail=cmd)
        return payload, rc == 0
    _labels = {
        "read_file":        f"📖 Reading: {_short_path(args.get('path','?'))}",
        "write_file":       f"✍ Writing: {_short_path(args.get('path','?'))}",
        "scan_project":     f"🔍 Scanning: {_short_path(args.get('path','?'))}",
        "list_dir":         f"📂 Listing: {_short_path(args.get('path','?'))}",
        "delete_file":      f"🗑 Deleting: {_short_path(args.get('path','?'))}",
        "grep_search":      f"🔎 Searching: /{args.get('pattern','?')}/",
        "web_search":       f"🌐 Web search: {args.get('query','?')}",
        "multi_edit_files": f"✍ Editing {len(args.get('edits',[])) if isinstance(args.get('edits'), list) else '?'} file(s)",
        "save_memory":      "🧠 Saving memory",
        "emit_plan":        f"📋 Planning: {args.get('title','?')}",
        "update_todo":      "✅ Updating task list",
    }
    print_tool_call(name, _labels.get(name, name))
    # Same capture-before-dispatch invariant as run_agent above. This is a
    # genuinely separate echo path (custom providers), so it needs its own call.
    _changes = diffview.capture_for(name, args)
    result = dispatch_tool(name, args)
    if "error" in result:
        status_line(f"Tool error: {result['error']}", "error")
        return f"Error: {result['error']}", False
    if _changes:
        # ⚠️ A DELETE IS RENDERED HERE TOO. This branch used to skip
        # `render_change` for `delete_file` exactly as the Gemini loop did, so the
        # gate had to be lifted in BOTH places or a custom provider would still be
        # the one surface that hides a deletion. The confirmation line matches the
        # Gemini loop's wording for the same reason.
        for _ch in _changes:
            diffview.render_change(_ch, preview=True)
            diffview.store.add(_ch)
        if name != "delete_file":
            diffview.render_summary(_changes)
        else:
            status_line(f"Deleted → {_short_path(args.get('path','?'))}", "warning")
    # Same reason as the Gemini loop above: this is a genuinely separate echo
    # path, so the checklist needs its own render call or it stays invisible for
    # every custom provider.
    if name == "update_todo":
        taskview.render_for_result(result)
    txt = result.get("output") or result.get("content") or json.dumps(result)[:2000]
    return str(txt)[:6000], True


def _one_model_turn(user_input: str, history: list, model: str, mode: str,
                    send_msg: str | None = None) -> list:
    """Run a single turn on exactly one model (may raise ModelUnavailable).

    *send_msg* is the offline-PIL-enhanced copy to send to the model; history
    always keeps the original *user_input*.
    """
    if isinstance(model, str) and model.startswith("custom:"):
        return run_provider_agent_cli(user_input, history, model.split(":", 1)[1], send_msg)
    return run_agent(user_input, history, model, mode, send_msg)


def _fallback_order(current: str) -> list:
    """Ordered models to try: *current* first, then the rest by capability fit.

    ⚠️ Task 19: the ordering after `current` now comes from `router._rank` via
    `router.next_model`, NOT from raw `config.MODELS` order. A second ordering here
    is exactly the drift the one-declaration rule warns about — the web loop would
    fall back to one model and the CLI to another for the same failure, and each
    half would look correct on its own.

    ⚠️ COOLING MODELS ARE SKIPPED, BUT THE LIST IS NEVER EMPTIED. `router.candidates`
    returns `warm or keys`, so when every model is cooling we still try them — an
    empty order would mean "the CLI refuses to answer", which is worse than a
    bruised model.

    ⚠️ Every model is still tried AT MOST ONCE, and that is deliberately unchanged.
    The CLI has always exhausted the list before giving up, and `FALLBACK_MAX_HOPS`
    is not applied here: trying each configured model once is already bounded (no
    loop can form), and capping it at two would REMOVE working behaviour to satisfy
    a rule about endless retries that this path never broke.
    """
    order = [current]
    # `rank_candidates` is the SAME ordering the web loop's `next_model` uses, asked
    # for the whole list instead of the head.
    order += [k for k in _router.rank_candidates({}, exclude=order) if k not in order]
    # Anything the ranking withheld (a cooling model) still belongs at the END:
    # "tried and failed" beats "never tried" when the alternative is telling the
    # user every model is unavailable.
    for key in _router.candidates(skip_cooling=False):
        if key not in order:
            order.append(key)
    return order


def agent_turn(user_input: str, history: list, model: str, mode: str) -> tuple[list, str]:
    """Run a turn with automatic model fallback on auth/quota failures.

    Returns (history, model_that_succeeded). If every model fails, prints a clear
    'all exhausted/failed' error and returns the history unchanged.

    ⚠️ Task 18: routing happens ONCE, here, before the fallback loop — the same
    place and for the same reason as in the web loop. Deciding per candidate would
    let the router re-pick the model that just failed.
    """
    # Task 18. `choose()` is total: with routing off and an explicit model it hands
    # `model` straight back, so the default install behaves exactly as before.
    decision = _router.choose(model, message=user_input,
                              context_tokens=_router.estimate_tokens(user_input))
    if decision.routed and decision.model_key != model:
        status_line(f"Auto-routed → {model_label(decision.model_key)}"
                    f"  ({decision.reason})", "info")
        model = decision.model_key
    # The one guard `auto` needs: it is a policy, not a callable model id.
    model = _router.resolve(model)

    order = _fallback_order(model)
    base_len = len(history)
    failures: list[tuple[str, str]] = []    # (model, reason)

    # Offline PIL: learn from the original, print the "Prompt / improved to"
    # preview ONCE (here, before the fallback loop — not inside run_agent, which
    # runs once per candidate model), and send the enhanced copy to the model.
    send_msg = _pil_process(user_input)

    for i, m in enumerate(order):
        _started = time.time()
        try:
            new_hist = _one_model_turn(user_input, history, m, mode, send_msg)
            # Task 19: the ledger and the breaker are shared with the web half, so
            # `/api/models` reports both surfaces and a model that keeps failing in
            # the CLI is skipped by the browser's fallback too.
            _router.record_attempt(
                model=m, ok=True, latency_ms=int((time.time() - _started) * 1000),
                primary_model=model,
                fallback_of=(order[i - 1] if i else ""), session_id="cli")
            _router.note_success(m)
            if m != model:
                status_line(f"Now using {model_label(m)} (auto-switched).", "success")
            return new_hist, m
        except ModelUnavailable as mu:
            failures.append((m, mu.reason))
            _router.record_attempt(
                model=m, ok=False, latency_ms=int((time.time() - _started) * 1000),
                primary_model=model, fallback_of=(order[i - 1] if i else ""),
                failure_kind=mu.reason, failure_reason=str(mu), session_id="cli")
            _router.note_failure(m, mu.reason)
            # Roll back any partial user/assistant rows this attempt appended so
            # the next model starts clean.
            del history[base_len:]
            nice = {"unauthenticated": "unauthenticated",
                    "exhausted": "resource exhausted",
                    "invalid_model": "unavailable"}.get(mu.reason, mu.reason)
            nxt = order[i + 1] if i + 1 < len(order) else None
            if nxt:
                status_line(f"{model_label(m)} → {nice}. Trying {model_label(nxt)}…", "warning")
            else:
                status_line(f"{model_label(m)} → {nice}.", "error")
        except KeyboardInterrupt:
            print(); status_line("Interrupted.", "warning")
            return history, model
        except SystemExit:
            raise
        except BaseException as exc:  # noqa: BLE001 — never let one model kill the app
            # Any unexpected error (network, parsing, SDK bug, etc.) is recorded
            # as a failure for THIS model and we fall through to the next one,
            # rather than aborting the whole session.
            failures.append((m, f"error: {str(exc)[:120]}"))
            _router.record_attempt(
                model=m, ok=False, latency_ms=int((time.time() - _started) * 1000),
                primary_model=model, fallback_of=(order[i - 1] if i else ""),
                failure_kind="unknown", failure_reason=str(exc), session_id="cli")
            _router.note_failure(m, "unknown")
            del history[base_len:]
            nxt = order[i + 1] if i + 1 < len(order) else None
            if nxt:
                status_line(f"{model_label(m)} → error. Trying {model_label(nxt)}…", "warning")
            else:
                status_line(f"{model_label(m)} → error: {str(exc)[:120]}.", "error")

    # Every model failed.
    all_exhausted = all(r == "exhausted" for _, r in failures) and failures
    detail = ", ".join(f"{model_label(mm)}: {rr}" for mm, rr in failures)
    if all_exhausted:
        status_line(f"All resources exhausted — every model hit its quota. ({detail})", "error")
    else:
        status_line(f"All models failed. ({detail})", "error")
    status_line("Add/rotate keys with /addapi, add a provider with /provider add, "
                "or try again later.", "info")
    return history, model


# ── /model caps — the capability registry, editable (Task 17) ──────────────────
def cmd_model_caps(arg: str, current_model: str) -> None:
    """`/model caps [<model>] [field=value …]` — show or correct model metadata.

    ⚠️ This is the CLI half of "support updating configured model metadata", and it
    prints UNKNOWN as `?`, never as "no". The registry's three-way answer
    (yes / no / we-do-not-know) is the whole reason a user would come here — a
    custom endpoint that does handle images looks identical to one that does not
    until somebody says so, and a display that collapsed unknown into "no" would
    hide the very field the user came to fix.

    Writes go through `capabilities.set_meta`, which validates and raises; a bad
    value is reported rather than silently coerced, because a coerced value reads as
    a successful edit that did not work.
    """
    from agent2.llm import capabilities as _caps

    words = arg.split()[1:]          # drop the literal "caps"
    target = current_model
    assignments: list[tuple[str, str]] = []
    for word in words:
        if "=" in word:
            field, _, value = word.partition("=")
            assignments.append((field.strip(), value.strip()))
        else:
            target = word.strip()
    target = target or current_model

    if assignments:
        try:
            _caps.set_meta(target, **dict(assignments))
            status_line(f"Updated metadata for {target}.", "success")
        except (ValueError, RuntimeError) as exc:
            status_line(str(exc), "error")
            return

    def _show(value) -> str:
        if value is None or value == "":
            return "?"
        if value is True:
            return "yes"
        if value is False:
            return "no"
        return str(value)

    rec = _caps.get(target)
    status_line(f"Capabilities · {target}", "info")
    rows = [
        ("model id", _show(rec.get("model"))),
        ("provider", _show(rec.get("provider"))),
        ("context window", f"{rec['context_window']:,}" if rec.get("context_window")
                           else "?"),
        ("reasoning", _show(rec.get("reasoning"))),
        ("coding", _show(rec.get("coding"))),
        ("speed", _show(rec.get("speed"))),
        ("cost", _show(rec.get("cost"))),
        ("vision", _show(rec.get("vision"))),
        ("tool use", _show(rec.get("tool_use"))),
        ("structured output", _show(rec.get("structured_output"))),
        ("thinking", _show(rec.get("thinking"))),
        ("source", _show(rec.get("source"))),
    ]
    width = max(len(k) for k, _ in rows)
    for label, value in rows:
        print(f"  {D}{label.ljust(width)}{R}  {value}")
    if rec.get("notes"):
        print(f"  {D}{'notes'.ljust(width)}{R}  {rec['notes']}")
    print(f"  {D}edit:{R} /model caps {target} vision=yes context_window=200000")


# ── /model rank — ask a model to classify a model (Task 17) ────────────────────
def cmd_model_rank(arg: str, current_model: str) -> None:
    """`/model rank [<model>|all] [force]` — fill in an unrecognised model's ranking.

    The escape hatch for "I registered `some-inhouse-moe-v3` and Agent2 will not
    route anything demanding to it". Pattern inference covers the well-known
    families for free; this covers everything else by asking a cheap model once and
    storing the answer, so the cost is paid at most once per provider.

    ⚠️ It SAYS it is asking, and it says what it learned. A command that silently
    made a network call and silently changed how routing behaves would be the kind
    of invisible action nobody can debug — so the before/after is printed, and the
    record is labelled `model-ranked` for anyone who reads it later.
    """
    from agent2.llm import capabilities as _caps

    words = [w for w in arg.split()[1:]]      # drop the literal "rank"
    force = any(w.lower() in ("force", "--force", "-f") for w in words)
    targets = [w for w in words if w.lower() not in ("force", "--force", "-f")]

    if targets and targets[0].lower() == "all":
        status_line("Asking a model to re-rank every custom provider…", "info")
        changed, skipped, not_reached = _caps.rank_all()
        for key in changed:
            rec = _caps.get(key)
            status_line(f"{key} ({rec['model']}) → reasoning="
                        f"{rec['reasoning'] or '?'} coding={rec['coding'] or '?'}"
                        f" [{rec['source']}]", "success")
        if skipped:
            # ⚠️ Said out loud rather than quietly left out. "Re-rank everything"
            # is a request to re-ask the models, not permission to discard the
            # user's own answers — but a user who is not told will read the missing
            # rows as a failure.
            status_line(f"{len(skipped)} kept your hand-set values "
                        f"({', '.join(skipped[:4])}"
                        f"{'…' if len(skipped) > 4 else ''}) — "
                        "change those with /model caps.", "info")
        if not_reached:
            status_line(f"{not_reached} more not reached this run (one model call "
                        "each) — run /model rank all again.", "warning")
        if not changed and not skipped:
            status_line("No custom providers configured.", "info")
        elif not changed:
            status_line("Nothing changed.", "info")
        return

    target = targets[0] if targets else current_model
    before = _caps.get(target)
    status_line(f"Asking a model about {target} ({before['model']})…", "info")
    after = _caps.rank_with_model(target, force=force)
    if after == before:
        if before["source"] in ("catalog", "inferred", "configured",
                                _caps.SOURCE_RANKED) and not force:
            status_line(f"Already described ({before['source']}). "
                        f"`/model rank {target} force` to re-ask.", "info")
        else:
            status_line("No usable answer — the model is still unranked. "
                        f"Set it by hand: /model caps {target} reasoning=advanced",
                        "warning")
        return
    status_line(f"{target} → reasoning={after['reasoning'] or '?'} "
                f"coding={after['coding'] or '?'} speed={after['speed'] or '?'} "
                f"cost={after['cost'] or '?'} [{after['source']}]", "success")


# ── /provider command ──────────────────────────────────────────────────────────
def cmd_provider(user_input: str, current_model: str) -> str:
    """Manage custom providers. Returns a (possibly new) model key to use."""
    from agent2.llm import providers as _prov
    _prov.init_providers_table()
    parts = user_input.split(maxsplit=2)
    sub = parts[1].strip().lower() if len(parts) > 1 else "list"

    if sub == "list":
        provs = _prov.list_providers(safe=True)
        if not provs:
            status_line("No custom providers. Add one: /provider add", "info")
        else:
            for p in provs:
                mark = "  ← active" if current_model == p["key"] else ""
                print(f"  {P.CY}{p['key']}{R}  {p['name']}  {D}[{p['format']}] {p['model_id']}{mark}{R}")
        print(f"  {D}Usage: /provider add | use <custom:id> | del <id> | test <id>{R}")
        return current_model

    if sub == "add":
        print(f"  {P.PU}{B}Add custom provider{R}  (OpenAI- or Anthropic-compatible)")
        print(f"  {D}Base URL examples:{R}")
        print(f"  {D}  OpenRouter : https://openrouter.ai/api/v1{R}")
        print(f"  {D}  OpenAI     : https://api.openai.com/v1{R}")
        print(f"  {D}  Anthropic  : https://api.anthropic.com   (format: anthropic){R}")
        print(f"  {D}  Groq       : https://api.groq.com/openai/v1{R}")
        print(f"  {D}  Ollama     : http://localhost:11434/v1{R}")
        try:
            name  = input("  Name: ").strip()
            burl  = input("  Base URL (usually ends in /v1): ").strip()
            mid   = input("  Model ID (e.g. deepseek/deepseek-chat): ").strip()
            fmt   = (input("  Format [openai/anthropic] (default openai): ").strip().lower() or "openai")
            akey  = input("  API key: ").strip()
            print(f"  {D}  User-Agent: optional. Some gateways (e.g. AgentRouter) only accept an{R}")
            print(f"  {D}  allowlisted client UA like  opencode/0.4.0  — leave blank for default.{R}")
            uagent = input("  User-Agent (optional): ").strip()
        except (EOFError, KeyboardInterrupt):
            print(); return current_model
        if not (burl and mid and akey):
            status_line("base_url, model_id and api_key are all required.", "error")
            return current_model
        p = _prov.add_provider(name, burl, akey, mid, fmt, uagent)
        ua_note = f", UA {uagent}" if uagent else ""
        status_line(f"Added {p['key']} ({p['format']}{ua_note}).", "success")

        # Test the connection immediately so the user knows it works NOW.
        prov = _prov.get_provider(p["id"])
        status_line("Testing connection…", "info")
        try:
            r = _prov.chat(prov, [{"role": "user", "content": "Reply with the single word OK"}],
                       "Connection test.")
            status_line(f"Connection OK — {(r.get('text') or 'OK')[:50]}", "success")
            try:
                ans = input(f"  Switch to this model now? [Y/n]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                ans = "n"
            if ans in ("", "y", "yes"):
                status_line(f"Model → {p['key']}", "success")
                return p["key"]
        except Exception as exc:
            status_line(f"Connection test FAILED: {exc}", "error")
            status_line("Fix the Base URL/key/format, then: /provider test " + p["id"], "warning")
        return current_model

    if sub == "use" and len(parts) > 2:
        key = parts[2].strip()
        pid = key.split(":", 1)[1] if key.startswith("custom:") else key
        if _prov.get_provider(pid):
            status_line(f"Model → custom:{pid}", "success")
            return "custom:" + pid
        status_line("Provider not found. /provider list", "error")
        return current_model

    if sub == "del" and len(parts) > 2:
        pid = parts[2].strip().replace("custom:", "")
        _prov.remove_provider(pid)
        status_line(f"Removed provider {pid}", "success")
        return DEFAULT_MODEL if current_model == "custom:" + pid else current_model

    if sub == "test" and len(parts) > 2:
        pid = parts[2].strip().replace("custom:", "")
        prov = _prov.get_provider(pid)
        if not prov:
            status_line("Provider not found.", "error"); return current_model
        try:
            r = _prov.chat(prov, [{"role": "user", "content": "Reply with the single word OK"}],
                       "Connection test.")
            status_line(f"OK — {(r.get('text') or '')[:60]}", "success")
        except Exception as exc:
            status_line(f"Test failed: {exc}", "error")
        return current_model

    status_line("Usage: /provider list | add | use <custom:id> | del <id> | test <id>", "info")
    return current_model


# ── /keys command (interactive activator with ↑/↓ selector) ────────────────────
def cmd_keys(current_model: str) -> str:
    """Show Gemini keys + custom providers and let the user ACTIVATE one with
    ↑/↓. Activating a Gemini key pins it as preferred (and, if a custom provider
    was active, switches the model back to Gemini). Activating a custom provider
    switches the model to it. Returns the (possibly new) model key."""
    gem = _rotator.status()
    try:
        from agent2.llm import providers as _prov
        _prov.init_providers_table()
        provs = _prov.list_providers(safe=True)
    except Exception:
        provs = []

    if not gem and not provs:
        status_line("No keys yet. Add a Gemini key with /addapi or a provider with /provider add.", "warning")
        return current_model

    using_custom = isinstance(current_model, str) and current_model.startswith("custom:")

    options: list[dict] = []
    # Gemini keys — value gem:<label>
    for k in gem:
        state = []
        if k.get("pinned"):
            state.append("pinned")
        if not k["active"]:
            state.append("exhausted")
        hint = "Gemini · " + (", ".join(state) if state else ("active" if not using_custom else "available"))
        options.append({"value": f"gem:{k['label']}",
                        "label": f"⚡ Gemini key #{k['label']}  {k['preview']}",
                        "hint": hint})
    # Custom providers — value = their model key (custom:<id>). Show model id
    # beside the key so two providers sharing the same API key are distinct.
    for p in provs:
        options.append({"value": p["key"],
                        "label": f"🔌 {p['name']}  {p['api_key']}  ·  {p['model_id']}",
                        "hint": f"{p['format']} provider · model {p['model_id']}"})
    # Auto-rotate option (unpin) when a key is currently pinned.
    if any(k.get("pinned") for k in gem):
        options.append({"value": "gem:__auto__",
                        "label": "↻ Auto-rotate Gemini keys (unpin)",
                        "hint": "Use whichever key is available; rotate on quota"})

    # Where the cursor starts.
    if using_custom:
        current_value = current_model
    else:
        pinned = next((k["label"] for k in gem if k.get("pinned")), None)
        current_value = f"gem:{pinned}" if pinned else None

    picked = ephemeral_picker("Activate a key / provider", options, current_value=current_value)
    if picked is None:
        status_line("No change.", "info")
        return current_model

    # ── Custom provider chosen → switch the model to it ────────────────────────
    if picked.startswith("custom:"):
        status_line(f"Activated provider → {model_label(picked)}  ({picked})", "success")
        return picked

    # ── Gemini branch ──────────────────────────────────────────────────────────
    if picked == "gem:__auto__":
        _rotator.pin(None)
        status_line("Gemini keys set to auto-rotate.", "success")
    else:
        label = picked.split(":", 1)[1]
        _rotator.pin(label)
        status_line(f"Activated Gemini key #{label} (pinned).", "success")

    # If a custom provider was active, drop back to a Gemini model so the pinned
    # key is actually used.
    if using_custom:
        new_model = load_last_gemini_model() or DEFAULT_MODEL
        status_line(f"Model → {new_model}", "success")
        return new_model
    return current_model


# ── /theme and /color (UI settings) ────────────────────────────────────────────
def cmd_theme(user_input: str):
    """/theme [name] — pick a colour theme with ↑/↓ (or set directly by name)."""
    parts = user_input.split(maxsplit=1)
    if len(parts) > 1:
        name = parts[1].strip().lower()
        if apply_theme(name):
            status_line(f"Theme → {THEMES[name]['label']}", "success")
        else:
            status_line(f"Unknown theme. Options: {', '.join(THEMES)}", "error")
        return
    choices = [{"value": k, "label": f"{v['label']}",
                "hint": f"accent {v['accent']}"} for k, v in THEMES.items()]
    picked = ephemeral_picker("Select a theme", choices, current_value=P.THEME)
    if picked and apply_theme(picked):
        status_line(f"Theme → {THEMES[picked]['label']}", "success")
        # Redraw so the new palette is immediately visible.
        print_banner()
    elif picked is None:
        status_line("Theme unchanged.", "info")


def cmd_color(user_input: str):
    """/color [name] — set just the accent colour with ↑/↓ (or by name)."""
    parts = user_input.split(maxsplit=1)
    if len(parts) > 1:
        name = parts[1].strip().lower()
        if apply_accent(name):
            status_line(f"Accent → {name}", "success")
        else:
            status_line(f"Unknown colour. Options: {', '.join(ACCENT_CHOICES)} (or a 0–255 code)", "error")
        return
    choices = [{"value": k, "label": f"{k}", "hint": hexv}
               for k, (code, hexv) in ACCENT_CHOICES.items()]
    picked = ephemeral_picker("Select an accent colour", choices)
    if picked and apply_accent(picked):
        status_line(f"Accent → {picked}", "success")
        print_banner()
    elif picked is None:
        status_line("Accent unchanged.", "info")


# ── /rules — activate/deactivate the rules that feed the system prompt ─────────
def cmd_rules():
    """/rules — an ephemeral ON/OFF menu over the `rules` table (Task 13).

    Rules were manageable from the browser only (`PUT /api/rules/active`); this is
    the CLI half of the same bulk primitive, so both surfaces write through
    `core.rules.set_rules_active` and neither flips rows one at a time.

    ⚠️ `cancellable=True`, i.e. Esc DISCARDS. Every switch here changes what the
    next prompt says to the model, and the footer this menu prints promises
    `Enter apply · Esc cancel` — a menu that applied on Esc would be lying in its
    own legend, which is the class of bug rule 21 exists for.
    """
    if not _CORE_OK or _core_rules is None:
        status_line("Rules are unavailable in this build.", "warning")
        return

    try:
        rows = _core_rules.list_rules()
    except Exception as exc:
        status_line(f"Could not read rules: {str(exc)[:120]}", "error")
        return

    if not rows:
        status_line("No rules saved yet — add one from the Web UI or /addmem for memories.", "info")
        return

    # The rule text IS the label; a rule has no name. Truncate for the menu and
    # keep the full text as the hint so a long rule is still identifiable.
    items = []
    for r in rows:
        content = (r.get("content") or "").strip().replace("\n", " ")
        items.append({
            "key": str(r.get("id")),
            "label": content[:64] + ("…" if len(content) > 64 else ""),
            "hint": content[64:160].strip() if len(content) > 64 else "",
            "on": bool(r.get("active")),
        })

    was = {it["key"]: it["on"] for it in items}
    result = ephemeral_toggle_menu(
        "Rules", items,
        subtitle="Active rules are injected into every system prompt.",
        cancellable=True,
    )
    if result is None:
        status_line("Rules unchanged.", "info")
        return

    on_ids = [k for k, v in result.items() if v and not was.get(k)]
    off_ids = [k for k, v in result.items() if not v and was.get(k)]
    if not on_ids and not off_ids:
        status_line("Rules unchanged.", "info")
        return

    try:
        # Two uniform bulk writes, not N toggles — see set_rules_active's docstring.
        if on_ids:
            _core_rules.set_rules_active(on_ids, True)
        if off_ids:
            _core_rules.set_rules_active(off_ids, False)
    except Exception as exc:
        status_line(f"Could not save rules: {str(exc)[:120]}", "error")
        return

    bits = []
    if on_ids:
        bits.append(f"{len(on_ids)} activated")
    if off_ids:
        bits.append(f"{len(off_ids)} deactivated")
    status_line(f"Rules updated → {', '.join(bits)}", "success")


# ── /settings — one hub over the menus that already exist ──────────────────────
# ⚠️ THIS OWNS NO STATE. Every entry hands off to the command that already owns
# that setting, so `/settings` can never disagree with `/model`, `/theme` or
# `/mcp` about the current value — there is nothing here to drift. It exists
# because Task 13 asks for one door onto the ephemeral menus, not because the
# settings needed a second writer.
def cmd_settings(current_model: str, current_mode: str) -> tuple[str, str]:
    """/settings — pick a settings area; each opens its own ephemeral menu.

    Returns the (possibly changed) (model, mode) pair, because two of the entries
    are the model and mode pickers and those values live in the REPL's locals.
    """
    choices = [
        {"value": "model",   "label": "Model",        "hint": f"currently {model_label(current_model)}"},
        {"value": "mode",    "label": "Mode",         "hint": f"currently {current_mode}"},
        {"value": "theme",   "label": "Theme",        "hint": f"currently {P.THEME}"},
        {"value": "color",   "label": "Accent colour", "hint": "the highlight colour"},
        {"value": "rules",   "label": "Rules",        "hint": "what every prompt is told"},
        {"value": "offline", "label": "Offline models", "hint": "local prediction / grammar / prompt engineer"},
        {"value": "mcp",     "label": "MCP servers",   "hint": "Burp · OWASP ZAP — this project"},
        {"value": "keys",    "label": "Keys & providers", "hint": "activate a key or custom provider"},
    ]
    picked = ephemeral_picker("Settings", choices)
    if picked is None:
        return current_model, current_mode

    if picked == "model":
        opts = build_model_choices()
        sel = ephemeral_picker("Select a model", opts, current_value=current_model)
        if sel and sel != current_model:
            current_model = sel
            save_last_model(current_model)
            statusbar.update(model=model_label(current_model))
            status_line(f"Model → {model_label(current_model)}", "success")
        elif sel is None:
            status_line("Model unchanged.", "info")
    elif picked == "mode":
        opts = build_mode_choices()
        sel = ephemeral_picker("Select a mode", opts, current_value=current_mode)
        if sel and sel != current_mode:
            current_mode = sel
            statusbar.update(mode=current_mode)
            status_line(f"Mode → {current_mode}", "success")
        elif sel is None:
            status_line("Mode unchanged.", "info")
    elif picked == "theme":
        cmd_theme("/theme")
    elif picked == "color":
        cmd_color("/color")
    elif picked == "rules":
        cmd_rules()
    elif picked == "offline":
        cmd_offline()
    elif picked == "mcp":
        cmd_mcp("/mcp")
    elif picked == "keys":
        current_model = cmd_keys(current_model)
        save_last_model(current_model)

    return current_model, current_mode


# ── /offline — toggle the offline PIL models on/off ─────────────────────────────
# Maps the user-facing names to the PIL setting keys:
#   Auto Suggestion → pil.prediction   (ghost-text autocomplete)
#   Auto correct    → pil.grammar      (opt-in grammar/spelling fixes)
#   Prompt engineer → pil.improve      (opt-in preference enrichment)
# The master switch (pil.enabled) gates all of them.
_PIL_MENU = [
    ("pil.prediction", "Auto Suggestion", "Ghost-text autocomplete as you type"),
    ("pil.grammar",    "Auto correct",    "Fix spelling/grammar before sending (never touches code)"),
    ("pil.improve",    "Prompt engineer", "Enrich prompts with your proven preferences"),
    ("pil.enabled",    "Master switch",   "Turn the whole offline layer on or off"),
]


def cmd_offline():
    """/offline — interactive on/off switches for the offline PIL models.

    ↑/↓ move · → toggles the highlighted model on/off (menu stays open) · Esc done.
    """
    if not _PIL_OK or _pil is None:
        status_line("Offline PIL layer is unavailable in this build.", "warning")
        return

    try:
        settings = _pil.get_settings()
    except Exception as exc:
        status_line(f"Could not read PIL settings: {str(exc)[:120]}", "error")
        return

    items = [
        {"key": key, "label": label, "hint": hint, "on": bool(settings.get(key))}
        for key, label, hint in _PIL_MENU
    ]
    subtitle = "Auto correct / Prompt engineer rewrite the prompt before it is sent."
    result = ephemeral_toggle_menu("Offline PIL Models", items, subtitle=subtitle)

    # The menu returns {key: bool}. Persist only what actually changed.
    changes = {k: v for k, v in (result or {}).items() if bool(settings.get(k)) != v}
    if not changes:
        status_line("Offline models unchanged.", "info")
        return

    try:
        _pil.update_settings(changes)
    except Exception as exc:
        status_line(f"Could not save PIL settings: {str(exc)[:120]}", "error")
        return

    label_of = {k: lbl for k, lbl, _ in _PIL_MENU}
    summary = ", ".join(f"{label_of.get(k, k)} {'ON' if v else 'OFF'}"
                        for k, v in changes.items())
    status_line(f"Offline models updated → {summary}", "success")


def _pil_process(user_input: str) -> str:
    """Learn from the ORIGINAL user text, then return the copy to send to the model.

    When grammar and/or prompt-engineering are on and they change the text, prints
    a 'Prompt: … / improved to: …' preview so the user sees what is actually sent.
    History and display always keep the original — only the model sees the enhanced
    copy. Fully best-effort: any failure returns the original untouched.
    """
    if not _PIL_OK or _pil is None:
        return user_input

    project = ""
    try:
        if _WS_OK and _core_ws is not None:
            project = _core_ws.current().as_dict().get("path", "") or ""
    except Exception:
        project = ""

    # Passive learning always uses the original text.
    try:
        _pil.learn_from_message(user_input, project=project)
    except Exception:
        pass

    try:
        final, report = _pil.process_outgoing_prompt(user_input, project=project)
    except Exception:
        return user_input

    if not report.get("changed") or not final or final == user_input:
        return user_input

    # Show the original → enhanced preview.
    orig_short = user_input if len(user_input) <= 220 else user_input[:217] + "…"
    if _RICH:
        _con.print(f"\n  [dim]Prompt:[/] {orig_short}")
        _con.print(f"  [bold {P.ACCENT2}]improved to:[/] {final}")
        tags = []
        if report.get("grammar_applied"):
            tags.append("auto-correct")
        if report.get("improve_applied"):
            tags.append("prompt-engineer")
        if tags:
            _con.print(f"  [dim]({' + '.join(tags)})[/]\n")
        else:
            print()
    else:
        print(f"\n  Prompt: {orig_short}")
        print(f"  improved to: {final}\n")

    return final


def process_turn(user_input: str, history: list, model: str, mode: str):
    """Run one agent turn while letting the user interrupt (ESC / single Ctrl+C)
    or QUEUE a follow-up message by typing during execution.

    Single ESC or Ctrl+C  → cancel the running turn instantly.
    Double Ctrl+C (within DOUBLE_TAP_WINDOW seconds) → exit the whole app.

    Returns (history, model, queued_messages).
    """
    _cancel_event.clear()
    _exit_event.clear()

    # ⚠️ WHERE THIS TURN'S FILE CHANGES START. `diffs.store` is session-long and
    # never cleared, so the recap needs a bookmark or it reports the whole session
    # as this turn's work. It is a SEQUENCE number, not a length — the store trims
    # from the front at MAX_CHANGES, so a length taken here would silently point at
    # the wrong turn once the cap engages (see `DiffStore.mark`).
    #
    # A mark is correct here specifically because the CLI is serial: the loop below
    # polls `while t.is_alive()` and cannot start a second turn. The Web UI runs N
    # turns concurrently from one process, which is why it accumulates per-session
    # in `core/progress.TurnProgress` instead of taking a process-global mark.
    _diff_mark = diffview.store.mark()

    # Item 8: the turn is tracked in stages (Planning → … → Done) rather than a
    # single "Thinking…". `Spinner` reads `progress_stages.label` per frame, so
    # starting the tracker here is what makes the spinner follow the turn.
    ux.progress_stages.start()
    ux.progress_stages.set_stage("Planning")
    ux.activity_feed.log(f"Turn started: {user_input[:60]}")
    statusbar.update(context_msgs=len(history))

    result: dict = {}

    def _worker():
        try:
            h, m = agent_turn(user_input, history, model, mode)
            result["history"] = h
            result["model"]   = m
        except Exception as exc:
            result["exc"] = exc

    # Route Ctrl+C to the cancel FLAG instead of letting it raise
    # KeyboardInterrupt in the main thread. Previously (esp. on Windows) the
    # interrupt unwound the main thread back to the prompt while the worker
    # thread kept running its in-flight API call, then printed the "stopped"
    # reply anyway. With the flag set, the worker's cancelled() checks make it
    # bail — and we wait for it below so nothing resumes after we return.
    #
    # ⚠️ Task 7: the handler cancels the WORK, never the process. `_interrupt`
    # settles the command state, tears down the child's process tree and sets the
    # flag — it does not raise, exit, or unwind — so control comes back to the
    # prompt below with the conversation still loaded. Letting SIGINT raise
    # `KeyboardInterrupt` here is what used to drop the user out of the session.
    import signal
    _prev_sigint = None
    try:
        _prev_sigint = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, lambda *_a: _interrupt(is_ctrl_c=True))
    except (ValueError, OSError, TypeError):
        _prev_sigint = None

    ctrl = InputController()
    ctrl.start()

    t = threading.Thread(target=_worker, daemon=True)
    t.start()

    noted = False
    try:
        # Loop until the worker THREAD actually finishes. We never return while
        # it is still alive — that is what stops a cancelled turn from "coming
        # back" and replying after the prompt has already returned.
        while t.is_alive():
            if _exit_event.is_set():
                # Double Ctrl+C — clean up and exit the whole app.
                _cancel_event.set()
                _interrupt()
                t.join(timeout=3.0)
                print()
                status_line("Goodbye.", "info")
                save_history(history, model, mode)
                sys.exit(0)
            if _cancel_event.is_set() and not noted:
                # Acknowledge the interrupt once. The worker will stop as soon as
                # its current (un-abortable) network step returns.
                #
                # ⚠️ Task 7: `_interrupt()` again, not `_kill_active_proc()`. The
                # flag can be set by a path that never went through the key
                # listener (`/pause`, a workspace switch, the web half in dual
                # mode), and those must still clean the process tree AND the
                # command state. It is idempotent: with nothing left running it
                # prints nothing and settles nothing.
                noted = True
                _interrupt()
                status_line("Stopping the current turn…", "warning")
            t.join(timeout=0.05)
    finally:
        ctrl.stop()
        if _prev_sigint is not None:
            try:
                signal.signal(signal.SIGINT, _prev_sigint)
            except Exception:
                pass
        # ⚠️ Queue cleanup belongs in `finally`, not on the success path. A
        # cancelled or crashed turn leaves entries marked "running" forever, and
        # the status bar counts those as pending — the bar would climb and never
        # come back down. Every exit path clears them.
        try:
            ux.command_queue.clear_finished()
        except Exception:
            pass

    queued = ctrl.drain()

    if "exc" in result:
        # Never let a worker exception propagate out of the turn — that used to
        # unwind main() into the __main__ failsafe and drop the user back to the
        # terminal. Report it inline and keep the session alive with history intact.
        exc = result["exc"]
        if isinstance(exc, SystemExit):
            raise exc
        status_line(f"Turn error: {str(exc)[:300]}", "error")
        status_line("The session is still running — try again, /model to switch, or /addapi.", "info")
        try:
            save_history(history, model, mode)
        except Exception:
            pass
        return history, model, queued

    if _cancel_event.is_set():
        print()
        # ⚠️ Task 7: the TASK half of a cancel happens HERE, on the flag path.
        # Ctrl+C no longer raises `KeyboardInterrupt` — the SIGINT handler above
        # sets a flag instead, precisely so the session survives — which means the
        # `except KeyboardInterrupt` in `main()` that used to stamp this can no
        # longer fire. Without the stamp a resumed session would show a task
        # parked with no reason, and `/tasks` would still call it RUNNING.
        _checkpoint_stop("interrupted (Ctrl+C)")
        status_line("Interrupted — the session is still running, keep typing.  "
                    "(Ctrl+C again quickly to quit.)", "warning")
        # Return whatever history the worker had bailed with; do NOT persist or
        # shrink a cancelled turn.
        return result.get("history", history), result.get("model", model), queued

    new_history = result.get("history", history)
    new_model   = result.get("model",   model)

    # Item 8 / 10: the end-of-turn recap. `set_stage("Done")` closes the trail so
    # the last stage is ticked rather than left mid-flight, and the summary reports
    # what the turn actually did instead of a bare "Done".
    #
    # ⚠️ Only printed when the turn produced something worth reporting. A plain
    # question with no tools run gets no recap — flooding the scrollback with a
    # stage list after "what is 2+2" is exactly the noise item 20 rules out.
    try:
        ux.progress_stages.set_stage("Done")
        _print_turn_recap(new_history, _diff_mark)
    except Exception:
        pass

    save_last_model(new_model)
    new_history = shrink_history_agent(new_history, new_model)
    save_history(new_history, new_model, mode)

    return new_history, new_model, queued


# ── Startup recovery (Task 3) ─────────────────────────────────────────────────
#
# ⚠️ THIS IS NOT `/resume`, AND IT IS NOT `/load`.
# `/resume` picks a CONVERSATION and `/load` reopens the last one in this
# directory; both are about chat history and neither knows whether any work was
# left half-done. This asks the *task* state instead, which is the only thing
# that survives a `kill -9` — and it never runs unless a task session in THIS
# project still has open tasks.
#
# ⚠️ AND IT ALWAYS ASKS.
# Adopting a recovery silently would re-point a fresh launch at old work, which
# is precisely the "launch always starts clean" rule the CLI keeps. Declining is
# a real answer with a real effect (`abandon`), so the offer cannot come back
# forever and train the user to dismiss it unread.

def _offer_recovery(history: list) -> list:
    """Offer to resume interrupted work found in this directory. Returns history.

    Never raises: this runs before the REPL exists, and a recovery hint that
    could stop the CLI from starting would be worse than no hint at all.
    """
    try:
        cand = _recovery.latest()
        if cand is None:
            return history
        rp = _recovery.plan(cand.session_id)
        if rp.is_empty():
            return history
        taskview.render_recovery(rp)
        choice = ephemeral_picker("Interrupted work found in this directory", [
            {"value": "resume", "label": "Resume it",
             "hint": f"continue from “{rp.resume.title[:40]}”"},
            {"value": "later", "label": "Not now",
             "hint": "leave it — you will be asked again next launch"},
            {"value": "discard", "label": "Discard it",
             "hint": f"cancel {len(rp.waiting) + 1} unfinished task(s)"},
        ], current_value="resume")
        if choice == "discard":
            n = _recovery.abandon(cand.session_id, reason="declined at startup")
            status_line(f"Discarded {n} unfinished task(s).", "info")
            return history
        if choice != "resume":
            return history

        _recovery.recover(cand.session_id, reason="resumed at startup")
        # Bind the chat the plan belongs to, so the model's next turn carries the
        # brief (`recovery.brief_for_chat`) and the conversation it refers to.
        # Without this the checklist would resume against an empty history and
        # the model would be told to continue work it cannot see.
        #
        # The id is re-checked rather than trusting a truthy return: `bind_chat`
        # answers with whatever is currently bound, so a deleted chat row would
        # otherwise read as success and load somebody else's history.
        bound = _bind_chat(cand.chat_id) if cand.chat_id else None
        if bound and str(bound.get("id") or "") == cand.chat_id:
            try:
                rows = _db_qall(
                    "SELECT role, content, created_at FROM messages "
                    "WHERE chat_id=? ORDER BY created_at", (cand.chat_id,))
                history = _msgs_to_history(rows)[-60:]
            except Exception:
                pass
        tooling.reset_tool_ctx()
        status_line(f"Resuming: {rp.resume.title}", "success")
        if rp.needs_verification:
            status_line("Some interrupted operations could not be confirmed — "
                        "verify before repeating them.", "warning")
    except Exception:
        return history
    return history


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Agent 2 CLI — autonomous dev agent")
    ap.add_argument("message", nargs="?", help="One-shot message (no REPL)")
    ap.add_argument("--model", default=None, choices=list(MODELS.keys()))
    ap.add_argument("--mode",  default=None, choices=list(MODES.keys()))
    ap.add_argument("--clear", action="store_true", help="Start with fresh chats")
    args = ap.parse_args()

    # ⚠️ CROSS-PROCESS SYNC IS NOT WEB-ONLY, AND THIS IS THE HALF THAT WAS MISSING.
    # `agent2web.py` and `agent2dual.py` both start this poller; the CLI never did,
    # so every cache that invalidates on a `sync` topic was one-way here. Nothing
    # in this process publishes another process's writes, and the MCP layer made
    # that visible: `integrations/state.py` caches the config row and clears it
    # only on the `mcp` topic, so a URL or ZAP security key changed in the browser
    # never reached a CLI that had already read it — the next `/mcp zap connect`
    # dialled the old endpoint with the old credential and failed with no visible
    # cause. Same silence for memories and rules, whose VersionedCaches subscribe
    # the same way. Daemon thread; a failed poll is retried on the next tick.
    try:
        from agent2.core import sync as _sync
        _sync.poller.start()
    except Exception:
        pass    # sync is an optimisation — never a reason the CLI cannot start

    # Session state — restore the last-used model unless overridden by --model.
    def _valid_model(mk: str | None) -> bool:
        if not mk:
            return False
        if mk in MODELS:
            return True
        if mk.startswith("custom:"):
            try:
                from agent2.llm import providers as _prov
                _prov.init_providers_table()
                return _prov.get_provider(mk.split(":", 1)[1]) is not None
            except Exception:
                return False
        return False

    _saved = load_last_model()
    model     = args.model or (_saved if _valid_model(_saved) else DEFAULT_MODEL)
    mode      = args.mode  or DEFAULT_MODE
    # Every CLI launch starts on a clean slate. The previous conversation is
    # still on disk — `/load` pulls it back in — but it is never resurrected
    # silently, and no chat row is written until there's something to save.
    history: list = []

    # Restore the saved UI theme / accent before anything is drawn.
    load_theme_from_settings()

    # One-shot mode (like `gemini -m flash "hello"`)
    if args.message:
        _rotator.reload()
        history, model = agent_turn(args.message, history, model, mode)
        save_last_model(model)
        save_history(history, model, mode)
        return

    # Interactive REPL
    if not _PTK:
        print("\n  [ERR]  prompt-toolkit not installed.")
        print("         Run:  pip install prompt-toolkit\n")
        return

    print_banner()
    _restored_note = "  (restored)" if (_saved and model == _saved and not args.model) else ""
    status_line(f"Model: {model_label(model)}{_restored_note}  Mode: {mode}  Shell: {SHELL_LABEL}", "info")
    status_line("Type /help for commands.  Ctrl+C or /exit to quit.", "info")
    if not _GENAI:
        status_line("google-genai is missing — install it before chatting: pip install google-genai", "warning")
    if not load_keys():
        status_line("No API keys — type /addapi to add one.", "warning")
    # Mention the previous conversation without loading it, and only when one
    # actually exists for this directory.
    if not args.clear:
        _prev = last_chat_for_cwd()
        if _prev:
            status_line(f"Last conversation here: {_prev.get('title') or 'New Chat'}"
                        f"  —  /load to continue it.", "info")

    # Interrupted work outruns the conversation hint above: a killed process left
    # task state behind that `/load` cannot see. Asked once, at launch.
    if not args.clear:
        history = _offer_recovery(history)

    # ── Status bar + keyboard shortcuts ────────────────────────────────────────
    # Item 11 (status bar) + item 16 (shortcuts). The bar reports workspace, model,
    # mode, git branch, memory, tasks, diffs, context msgs, tokens. Shortcuts bind
    # Ctrl+B (diff viewer), Ctrl+P (palette), Ctrl+L (clear), and F1 (help).

    def _open_diff_viewer():
        """Ctrl+B handler — full-screen diff viewer."""
        try:
            diffview.open_viewer()
        except Exception:
            pass

    def _open_palette():
        """Ctrl+P handler — command palette."""
        try:
            palette.open_palette()
        except Exception:
            pass

    def _clear_screen():
        """Ctrl+L handler — clear screen, keep history."""
        try:
            if _RICH: _con.clear()
            else: print("\033[2J\033[H", end="")
        except Exception:
            pass

    def _show_help():
        """F1 handler — help overlay."""
        print_help()

    def _show_tasks():
        """Ctrl+T handler — the command queue (item 7) and activity feed (item 13).

        Both are populated during a turn; this is where they become visible. Shown
        together because "what is running" and "what just happened" are the same
        question asked at two time scales, and a user reaching for Ctrl+T mid-turn
        wants both.
        """
        try:
            if _RICH:
                _con.print("\n[bold #60b8ff]Tasks[/]")
            else:
                print(f"\n  {P.CY}{B}Tasks{R}")
            snap = ux.command_queue.snapshot()
            if snap:
                ux.command_queue.render()
            elif _RICH:
                _con.print("  [dim]no tasks[/]")
            else:
                print(f"  {D}no tasks{R}")

            if _RICH:
                _con.print("\n[bold #60b8ff]Activity[/]")
            else:
                print(f"\n  {P.CY}{B}Activity{R}")
            ux.activity_feed.render(limit=15)
            print()
        except Exception:
            pass

    handlers = {
        "diff": _open_diff_viewer,
        "palette": _open_palette,
        "clear": _clear_screen,
        "help": _show_help,
        "tasks": _show_tasks,
    }

    kb = statusbar.build_key_bindings(handlers) if statusbar.build_key_bindings else None

    # Update the bar's session state so it reports the right model/mode.
    statusbar.update(model=model_label(model), mode=mode)

    completer = SlashCompleter() if SlashCompleter else None

    # ⚠️ The prompt style is rebuilt per iteration (so /mode and /theme repaint the
    # input line instantly) and passed to `session.prompt(style=…)`, which OVERRIDES
    # the session-level style entirely. The bar's `class:sb.*` rules must therefore
    # be merged into THAT style, not only into the constructor's — otherwise every
    # toolbar segment renders unstyled the moment the loop supplies its own style.
    def _prompt_style(m: str):
        base = get_prompt_style(m)
        # `Style.style_rules` is a LIST of (class, rule) pairs, not a dict, so the
        # merge goes through `dict()` first. Later keys win, which is what lets the
        # bar's rules sit alongside the prompt's without either clobbering the other.
        return Style.from_dict({
            **dict(base.style_rules),
            **statusbar.style_dict(),
        })

    session = PromptSession(
        history=FileHistory(str(PT_HISTORY)),
        completer=completer,
        complete_while_typing=True,          # suggest as you type "/…"
        auto_suggest=AutoSuggestFromHistory() if AutoSuggestFromHistory else None,
        bottom_toolbar=statusbar.bottom_toolbar,
        key_bindings=kb,
        style=_prompt_style(mode),
    )

    pending: list[str] = []   # messages queued while the agent was working

    while True:
        # If the agent queued follow-up messages, run those before prompting.
        if pending:
            user_input = pending.pop(0).strip()
            if user_input:
                status_line(f"↳ running queued message: {user_input[:70]}", "info")
        else:
            # Build prompt line
            mo_icon   = MODES[mode]["icon"]
            style = _prompt_style(mode)

            _esc = __import__("html").escape
            prompt = HTML(
                f'<user>you</user> '
                # Directory name first: it's the thing you scan for when several
                # CLIs are open. Escaped — a folder name may contain & or <.
                f'<cwd>{_esc(prompt_dir_name())}</cwd> '
                f'<meta>[{SHELL_LABEL}|{_esc(model_label(model))}|{mo_icon} ]</meta>'
                f'<arrow>></arrow> '
            )

            try:
                print()
                user_input = session.prompt(prompt, style=style).strip()
            except KeyboardInterrupt:
                now = time.monotonic()
                if (now - S.last_ctrl_c) < DOUBLE_TAP_WINDOW:
                    print()
                    status_line("Goodbye.", "info")
                    save_history(history, model, mode)
                    break
                S.last_ctrl_c = now
                status_line("Press Ctrl+C again to exit.", "info")
                continue
            except EOFError:
                print()
                status_line("Goodbye.", "info")
                save_history(history, model, mode)
                break

        if not user_input:
            continue

        low = user_input.lower()

        # ── Slash commands ─────────────────────────────────────────────────────
        if low in ("/exit", "/quit", "exit", "quit"):
            status_line("Goodbye.", "info")
            save_history(history, model, mode)
            break

        elif low == "/help":
            print_help()

        elif low == "/addapi":
            cmd_addapi()

        elif low == "/keys":
            model = cmd_keys(model)
            save_last_model(model)

        elif low.startswith("/burp"):
            cmd_burp(user_input)

        elif low.startswith("/mcp"):
            cmd_mcp(user_input)

        elif low.startswith("/provider"):
            model = cmd_provider(user_input, model)
            save_last_model(model)

        elif low.startswith("/model"):   # keep BEFORE /mode — /mode must not swallow /model
            parts = user_input.split(maxsplit=1)
            if len(parts) == 1:
                # Interactive arrow-key picker (built-ins + custom providers).
                choices = build_model_choices()
                picked = ephemeral_picker("Select a model", choices, current_value=model)
                if picked and picked != model:
                    model = picked
                    save_last_model(model)
                    statusbar.update(model=model_label(model))
                    _lbl = next((c["label"] for c in choices if c["value"] == picked), picked)
                    status_line(f"Model → {_lbl}  ({picked})", "success")
                elif picked is None:
                    status_line("Model unchanged.", "info")
            else:
                m = parts[1].strip()
                low_arg = m.lower()
                # ── Task 18/19 subcommands ────────────────────────────────────
                # ⚠️ These are checked BEFORE the model-name lookup for the same
                # reason `/model` is checked before `/mode`: `routing` and `caps`
                # are not model names, and falling through to the lookup would
                # print "Unknown model. Options: …" at a user who typed a valid
                # command. The router's own key (`auto`) IS a selectable value, so
                # it is handled as one rather than as a subcommand.
                if low_arg == _router.AUTO:
                    model = _router.AUTO
                    save_last_model(model)
                    statusbar.update(model=model_label(model))
                    _mode_now = _router.routing_mode()
                    status_line(f"Model → auto (Agent2 picks per turn)", "success")
                    if _mode_now == _router.ROUTING_OFF:
                        status_line("Routing policy is 'off' — `auto` still routes, "
                                    "but an explicit model never will. "
                                    "`/model routing default_only` to widen it.", "info")
                elif low_arg.startswith("routing"):
                    want = low_arg.split(maxsplit=1)
                    if len(want) == 1:
                        status_line(f"Routing: {_router.routing_mode()}   "
                                    f"(options: {', '.join(_router.ROUTING_MODES)})",
                                    "info")
                    else:
                        try:
                            now = _router.set_routing_mode(want[1].strip())
                            status_line(f"Routing → {now}", "success")
                        except (ValueError, RuntimeError) as exc:
                            status_line(str(exc), "error")
                elif low_arg.startswith("caps"):
                    cmd_model_caps(m, model)
                elif low_arg.startswith("rank"):
                    cmd_model_rank(m, model)
                elif m in MODELS:
                    model = m
                    save_last_model(model)
                    statusbar.update(model=model_label(model))
                    status_line(f"Model → {m}", "success")
                else:
                    from agent2.llm import providers as _prov
                    _prov.init_providers_table()
                    pid = m.split(":", 1)[1] if m.startswith("custom:") else m
                    if _prov.get_provider(pid):
                        model = "custom:" + pid
                        save_last_model(model)
                        statusbar.update(model=model_label(model))
                        status_line(f"Model → custom:{pid}", "success")
                    else:
                        opts = ", ".join([_router.AUTO, *MODELS, "custom:<id>"])
                        status_line(f"Unknown model. Options: {opts}", "error")

        elif low.startswith("/mode"):
            parts = user_input.split(maxsplit=1)
            if len(parts) == 1:
                # Interactive arrow-key picker (same UX as /model).
                choices = build_mode_choices()
                picked = ephemeral_picker("Select a mode", choices, current_value=mode)
                if picked and picked != mode:
                    mode = picked
                    statusbar.update(mode=mode)
                    status_line(f"Mode → {mode}  {MODES[mode]['icon']}", "success")
                elif picked is None:
                    status_line("Mode unchanged.", "info")
            else:
                m = parts[1].strip()
                if m in MODES:
                    mode = m
                    statusbar.update(mode=mode)
                    status_line(f"Mode → {m}  {MODES[m]['icon']}", "success")
                else:
                    status_line(f"Unknown mode. Options: {', '.join(MODES)}", "error")

        elif low.startswith("/theme"):
            cmd_theme(user_input)

        elif low.startswith("/color") or low.startswith("/colour"):
            cmd_color(user_input)

        elif low == "/clear":
            # Clear the SCREEN only — history is preserved and saved. /clearhistory
            # actually wipes conversation state; /load brings the last one back.
            os.system("cls" if IS_WIN else "clear")
            print_banner()
            status_line("Screen cleared. History kept.", "success")
        elif low == "/clearhistory":
            history = []
            save_history(history, model, mode)
            status_line("Conversation history cleared.", "success")

        elif low == "/shrink":
            history = shrink_history_agent(history, model, keep=5, manual=True)
            save_history(history, model, mode)
            status_line("History shrunk.", "success")

        elif low == "/history":
            if not history:
                status_line("No history.", "info")
            else:
                for h in history[-10:]:
                    col = P.CY if h["role"] == "user" else P.PU
                    sym = "you" if h["role"] == "user" else " a2"
                    ts  = h.get("ts","")[-8:][:5]
                    print(f"  {col}{sym}{R}  {D}{ts}{R}  {h['content'][:90]}")

        elif low == "/tasks":
            # Reads the SAME durable session the agent loop writes through
            # `ToolContext.task_session()`, so `/tasks` after a restart shows the
            # plan the previous process left behind — that is the whole point.
            # force=True because an explicit request must always print, even when
            # nothing changed since the panel was last drawn.
            sess = tooling.tool_ctx().task_session()
            if not taskview.render(sess, force=True, detail=True):
                status_line("No tasks yet — the agent will create them as it plans.", "info")

        elif low == "/memory":
            mems = load_mems()
            if not mems:
                status_line("No memories saved yet.", "info")
            else:
                if _RICH:
                    t = Table(show_header=True, header_style="bold #7c6af7", box=rbox.SIMPLE_HEAD)
                    t.add_column("#", width=3, style="dim")
                    t.add_column("Imp", width=5)
                    t.add_column("Content")
                    t.add_column("Tags", style="dim")
                    for i, m in enumerate(sorted(mems, key=lambda x: -x.get("importance", 5)), 1):
                        t.add_row(str(i), f"{m.get('importance',5)}/10",
                                  m["content"][:80],
                                  ", ".join(m.get("tags", [])))
                    _con.print(t)
                else:
                    for i, m in enumerate(sorted(mems, key=lambda x: -x.get("importance", 5)), 1):
                        print(f"  {D}{i}.{R}  {YW}[{m.get('importance',5)}/10]{R}  {m['content'][:80]}")

        elif low.startswith("/addmem"):
            parts = user_input.split(maxsplit=1)
            if len(parts) > 1:
                # `--shared` / `--global` is the explicit half of Task 23: without
                # it a memory belongs to THIS workspace. Stripped as a prefix only,
                # so "/addmem the --shared flag means X" still saves that text.
                body = parts[1].strip()
                shared = False
                for flag in ("--shared", "--global"):
                    if body.lower().startswith(flag):
                        shared, body = True, body[len(flag):].strip()
                        break
                if not body:
                    status_line("Usage: /addmem [--shared] <text>", "warning")
                else:
                    add_mem(body, shared=shared)
                    status_line(
                        "Memory saved (shared with every project)." if shared
                        else "Memory saved for this project.", "success")
            else:
                status_line("Usage: /addmem [--shared] <text>", "warning")

        elif low == "/run" or low.startswith("/run "):
            cmd = user_input[4:].strip()
            if cmd:
                _out, _err, _rc, _dur = run_cmd_stream(cmd)
                ux.print_command_result(cmd, _out, _err, _rc, _dur)
            else:
                status_line("Usage: /run <shell command>", "warning")

        elif low == "/read" or low.startswith("/read "):
            path = user_input[5:].strip()
            if not path:
                status_line("Usage: /read <file path>", "warning")
            else:
                result = _impl_read({"path": path})
                if "error" in result:
                    status_line(result["error"], "error")
                else:
                    lang = Path(path).suffix.lstrip(".")
                    if _RICH:
                        try:   _con.print(Syntax(result["content"][:3000], lang or "text", theme="monokai", line_numbers=True))
                        except: _con.print(result["content"][:3000])
                    else:
                        print(f"{YW}{result['content'][:3000]}{R}")

        elif low == "/search" or low.startswith("/search "):
            q = user_input[7:].strip()
            if not q:
                status_line("Usage: /search <query>", "warning")
            else:
                result = _impl_search({"query": q})
                for res in result.get("results", [])[:5]:
                    print()
                    if _RICH:
                        _con.print(f"  [bold {P.ACCENT2}]{res.get('title','')[:70]}[/]\n  [dim]{res.get('snippet','')[:250]}[/]\n")
                    else:
                        print(f"  {P.CY}{res.get('title','')[:70]}{R}\n  {D}{res.get('snippet','')[:250]}{R}\n")
                if not result.get("results"):
                    status_line("No results.", "info")

        elif low.startswith("/scan"):
            # Extract path and feed to agent as an explicit scan request
            scan_path = user_input[5:].strip() or "."
            scan_msg = f"Scan the project at path: {scan_path} — use the scan_project tool on that path. Show the file tree and analyze the tech stack (languages, frameworks, DB, etc)."
            try:
                history, model, _queued = process_turn(scan_msg, history, model, mode)
                pending.extend(_queued)
            except KeyboardInterrupt:
                print(); status_line("Interrupted.", "warning")
            except Exception as _e:
                status_line(f"Scan error: {str(_e)[:200]}", "error")

        elif low == "/pause":
            # Force the lazily-created chat into existence first, otherwise a
            # session that has said something but never saved has nothing to pause.
            save_history(history, model, mode)
            if _CTX_OK and S.chat:
                _core_ctx.pause_chat(S.chat["id"])
                S.chat["status"] = "paused"
                status_line(f"Conversation paused: {S.chat.get('title','Chat')}  — use /resume to continue.", "info")
            else:
                status_line("No active conversation to pause.", "warning")

        elif low == "/load":
            # CLI-only, and deliberately narrower than /resume: no picker, no
            # cross-project listing — just the last conversation from THIS
            # directory, which is what "carry on where I left off" means.
            _prev = last_chat_for_cwd()
            if not _CTX_OK:
                status_line("DB unavailable — cannot load the last conversation.", "warning")
            elif not _prev:
                status_line("No previous conversation in this directory.", "warning")
            elif S.chat and _prev["id"] == S.chat.get("id"):
                status_line("Already in that conversation.", "info")
            else:
                # Don't lose whatever this session has said before switching away.
                save_history(history, model, mode)
                loaded = load_last_conversation()
                if loaded is None:
                    status_line("Could not load that conversation.", "error")
                else:
                    history = loaded
                    status_line(f"Loaded: {S.chat.get('title','Chat')}"
                                f"  ({len(history)} message(s))", "success")

        elif low == "/resume":
            if not _CTX_OK:
                status_line("DB unavailable — cannot list conversations.", "warning")
            else:
                all_chats = _core_ctx.list_all_chats()
                if not all_chats:
                    status_line("No saved conversations found.", "warning")
                else:
                    PAGE = 10
                    page = 0
                    chosen = None
                    while True:
                        start = page * PAGE
                        page_chats = all_chats[start:start + PAGE]
                        total_pages = max(1, (len(all_chats) + PAGE - 1) // PAGE)
                        options = [
                            {"value": c["id"],
                             "label": f"{c.get('title','New Chat')[:48]}  [{c.get('status','active')}]",
                             "hint":  c.get("cwd", "")[:60]}
                            for c in page_chats
                        ]
                        if page > 0:
                            options.append({"value": "__prev__", "label": "← Previous page", "hint": ""})
                        if start + PAGE < len(all_chats):
                            options.append({"value": "__next__", "label": "Next page →", "hint": ""})
                        if _RICH:
                            _con.print(f"\n[bold #7c6af7]Resume Conversation[/]  [dim]Page {page+1}/{total_pages}[/]")
                        else:
                            print(f"\nResume Conversation  (Page {page+1}/{total_pages})")
                        sel = ephemeral_picker("Select a conversation", options)
                        if sel == "__next__":
                            page += 1
                        elif sel == "__prev__":
                            page = max(0, page - 1)
                        elif sel:
                            chosen = sel
                            break
                        else:
                            break  # Esc / cancel
                    if chosen:
                        save_history(history, model, mode)
                        _core_ctx.resume_chat(chosen)
                        row = _db_qall(
                            "SELECT role, content, created_at FROM messages "
                            "WHERE chat_id=? ORDER BY created_at", (chosen,))
                        history = _msgs_to_history(row)[-60:]
                        _bind_chat(chosen)
                        title = (S.chat or {}).get("title", "Chat")
                        status_line(f"Resumed: {title}", "success")

        elif low == "/workspace" or low == "/cd" or low.startswith("/workspace ") or low.startswith("/cd "):
            if not _WS_OK:
                status_line("Workspace manager unavailable.", "warning")
            else:
                # Argument = everything after the command word (may be quoted).
                arg = user_input.split(None, 1)[1].strip() if " " in user_input else ""
                arg = arg.strip('"').strip("'")
                if not arg:
                    # No argument → show the current workspace.
                    ws = _core_ws.current().as_dict()
                    status_line(f"Current workspace: {ws['path']}", "info")
                    meta = ws.get("metadata") or {}
                    if meta.get("marker") or meta.get("vcs"):
                        detail = meta.get("vcs") or meta.get("marker")
                        status_line(f"Detected via: {detail}", "info")
                    status_line("Use  /workspace <path>  to switch (this also cancels running tasks).", "info")
                else:
                    try:
                        # Persist the current chat before the switch so nothing is lost.
                        save_history(history, model, mode)
                        old = _core_ws.current().as_dict()["path"]
                        ws = _core_ws.set_workspace(arg)
                        if ws.as_dict()["path"] == old:
                            status_line(f"Already in workspace: {ws.path}", "info")
                        else:
                            status_line(f"Workspace switched to: {ws.path}", "success")
                            status_line("Running tasks were cancelled; all tools are now confined to this root.", "info")
                    except _WSViolation as _wv:
                        status_line(str(_wv), "error")
                    except Exception as _e:
                        status_line(f"Could not switch workspace: {str(_e)[:200]}", "error")

        elif low == "/offline":
            cmd_offline()

        elif low == "/rules":
            cmd_rules()

        elif low == "/settings" or low == "/config":
            model, mode = cmd_settings(model, mode)

        elif low.startswith("/"):
            status_line(f"Unknown command: {user_input}  →  /help", "warning")

        # ── Agent call ─────────────────────────────────────────────────────────
        else:
            try:
                history, model, _queued = process_turn(user_input, history, model, mode)
                pending.extend(_queued)
            except KeyboardInterrupt:
                print(); status_line("Interrupted.", "warning")
                _checkpoint_stop("interrupted (Ctrl+C)")
            except Exception as _e:
                status_line(f"Turn error: {str(_e)[:200]}", "error")
                status_line("The session is still running — try again or /model to switch.", "info")
                _checkpoint_stop(f"turn error: {str(_e)[:80]}")

if __name__ == "__main__":
    # Resilient entry: the user must NEVER be dumped back to the terminal by an
    # unexpected error. A clean Ctrl+C exits; anything else is reported and the
    # REPL is relaunched. A crash cap prevents a tight restart loop.
    _crashes = 0
    while True:
        try:
            main()
            break
        except KeyboardInterrupt:
            print()
            try:
                status_line("Goodbye.", "info")
            except Exception:
                pass
            break
        except SystemExit:
            raise
        except Exception as _fatal:
            _crashes += 1
            try:
                status_line(f"Unexpected error: {str(_fatal)[:300]}", "error")
            except Exception:
                print(f"\n  [ERR] {str(_fatal)[:300]}")
            if _crashes >= 5:
                try:
                    status_line("Too many restarts — exiting. Try:  python run.py --reset", "error")
                except Exception:
                    print("  Too many restarts — exiting. Try:  python run.py --reset")
                sys.exit(1)
            try:
                status_line("Recovering the session…  (Ctrl+C to quit)", "warning")
            except Exception:
                pass
            # loop back into main() — history is restored from the DB/file.
