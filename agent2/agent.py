# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/agent.py
───────────────
Core agent logic:
  - system_prompt(): builds the full system instruction (platform rules +
    memories + user rules)
  - build_context(): assembles the last N messages into Gemini Content objects
  - run_agent(): the main agentic loop (LLM → tool call → stream → loop)

SYSTEM PROMPT CACHING
─────────────────────
Built at call time in three parts: static text (built once per process) plus
memories and rules, each behind a VersionedCache. A turn normally costs ZERO DB
queries for the prompt — an unchanged store is a flag check, not a query.

⚠️ Task 20: THE MEMORIES/RULES TAIL IS ASSEMBLED BY `core.broker`, AND
`_MEM_CACHE` / `_RULES_CACHE` HERE ARE NAMES BOUND TO **THAT** MODULE'S CACHES —
not second copies. The Context Broker composes every context source (project, git,
plan, MCP, files, skills, workflow, memory, rules) and both agent loops read the
tail from it, so there is one composition and one pair of caches. A second
`VersionedCache("memories", …)` would be invisible drift of the worst kind: the
surface that invalidated would keep serving its own stale copy while insisting it
had refreshed. The names stay because the existing tests invalidate through them.

⚠️ A BARE `system_prompt()` STILL COSTS ZERO QUERIES, AND THAT IS WHY THE BROKER
BUNDLE IS A PARAMETER RATHER THAN AN INTERNAL CALL. The prompt is built once per
turn but read on every one of up to MAX_AGENT_ITERS iterations, and the
situational sources (project docs, git, task state) are turn-dependent by nature.
So `run_agent` assembles a bundle once and passes it in; every other caller —
`provider_agent`'s fallback, `/api/platform`, the prompt tests — gets
`broker.base_tail()`, the same two cached blocks the prompt has always ended with.

`build_context()` has NO cache on purpose: history changes every turn. It stays
one LIMITed query and one pass, and `meta` is JSON-parsed only for the rows that
actually read it — because it is rebuilt on EVERY iteration, so its cost is
multiplied by MAX_AGENT_ITERS.

CONTEXT ASSEMBLY — the invariants, all of them regression-pinned
───────────────────────────────────────────────────────────────
⚠️ `ORDER BY created_at DESC, rowid DESC` — THE rowid TIE-BREAK IS LOAD-BEARING.
`created_at` is second-granular and one turn writes all of its rows inside the
same second, so ordering by timestamp alone left the whole turn tied. SQLite
breaks ties by rowid, and under DESC that is *reversed* — the model was handed
each turn backwards, with `tool_result` ahead of its own `tool_call`.

⚠️ `_tool_name()` RESOLVES THE NAME FOR BOTH ROW KINDS, from one function.
Gemini rejects a `function_response` whose name does not match its
`function_call` and fails the ENTIRE turn, so the two can never be allowed to
disagree. One function decides; both call sites use it. (Its `burp`/`mcp`/`local`
precedence is documented but deliberately not asserted — no writer can produce a
row where more than one applies, and asserting an unreachable state is noise.)

⚠️ `_tool_args()` FALLBACKS DIFFER BY KIND ON PURPOSE.
A named tool missing args gets `{}`; only a shell row falls back to the
`run_command` shape. Handing that shape to a named tool would invent parameters
its schema never declared.

⚠️ AN UNPAIRED `tool_call` IS DROPPED. Otherwise it reaches Gemini without its
response and costs the turn.

⚠️ `returncode` IS SHELL-ONLY. A local tool has no exit status, so reporting one
would tell the model a `read_file` "exited 0".

(test_agent_loop.py — sabotage-verified)

Kept stable for `llm/provider_agent.py`, which imports `system_prompt`,
`save_msg`, `_LOCAL_TOOLS`, `_tool_label` and `_tool_result_summary` from here.
"""

import base64
import json
import os
import random
import threading
import time
import uuid
from pathlib import Path
from collections.abc import Callable

from google.genai import types

from agent2.config import (
    OS_NAME, SHELL_LABEL,
    MODELS, MODES, DEFAULT_MODEL, DEFAULT_MODE,
    MAX_CTX_MESSAGES, MAX_TOOL_OUTPUT, MAX_AGENT_ITERS, MAX_RETRIES,
    supports_thinking,
)
from agent2.database import qall, qone, exe
from agent2.llm.keys import rotator
from agent2.llm import router
from agent2.llm.resilience import (
    call_with_retry, classify_error, is_blank_reply, blank_reply_notice,
)
from agent2.terminal import stream_command
from agent2.integrations.burp_mcp import burp
from agent2.integrations import registry as mcp_registry
from agent2.tools import dispatch_tool, _build_tools, ToolContext
from agent2.core import logging as alog
from agent2.core.session import sessions
from agent2.core import workspace as _workspace
from agent2.core import diffs as _diffs
from agent2.core import recovery as _recovery
from agent2.core import broker as _broker
from agent2.core.progress import TurnProgress, stage_for_tool

# Local (non-shell, non-Burp) tools dispatched via agent2.tools.dispatch_tool
_LOCAL_TOOLS = {
    "read_file", "write_file", "web_search", "save_memory", "emit_plan",
    "scan_project", "multi_edit_files", "list_dir", "delete_file",
    "grep_search", "update_todo",
    # File Intelligence System
    "detect_file", "file_capabilities", "run_file_op", "convert_file",
    "search_workspace",
}


def _short_path(p: str) -> str:
    """Show a path relative to the CWD when possible (e.g. agent2/agent.py)."""
    if not p or p == "?":
        return p or "?"
    try:
        rel = os.path.relpath(str(Path(p).expanduser()), os.getcwd())
        if not rel.startswith(".." + os.sep) and rel != "..":
            return rel.replace(os.sep, "/")
        return Path(p).name
    except Exception:
        return Path(p).name


def _edit_count(args: dict) -> str:
    edits = args.get("edits", [])
    return str(len(edits)) if isinstance(edits, list) else "?"


# Tool → label formatter. A dict dispatch (one hash lookup) replaces what was a
# 16-branch if-chain evaluated on every single tool call; the tools declared last
# used to pay for every comparison ahead of them.
_TOOL_LABELS: dict[str, Callable[[dict], str]] = {
    "read_file":        lambda a: f"📖 Reading: {_short_path(a.get('path','?'))}",
    "write_file":       lambda a: f"✍ Writing: {_short_path(a.get('path','?'))}",
    "scan_project":     lambda a: f"Scanning {_short_path(a.get('path','?'))}",
    "list_dir":         lambda a: f"Listing {_short_path(a.get('path','?'))}",
    "delete_file":      lambda a: f"Deleting {_short_path(a.get('path','?'))}",
    "grep_search":      lambda a: f"Searching /{a.get('pattern','?')}/",
    "web_search":       lambda a: f"Web search: {a.get('query','?')}",
    "save_memory":      lambda a: "Saving memory",
    "emit_plan":        lambda a: f"Planning: {a.get('title','?')}",
    "update_todo":      lambda a: "Updating task list",
    "detect_file":      lambda a: f"🔍 Detecting: {_short_path(a.get('path','?'))}",
    "multi_edit_files": lambda a: f"✍ Editing {_edit_count(a)} file(s)",
    "file_capabilities": lambda a: (
        f"Capabilities: {_short_path(a.get('path') or a.get('category','?'))}"),
    "run_file_op":      lambda a: (
        f"⚙ {a.get('operation','?')}: {_short_path(a.get('path','?'))}"),
    "convert_file":     lambda a: (
        f"🔄 Converting {_short_path(a.get('path','?'))} → {a.get('to_format','?')}"),
    "search_workspace": lambda a: (
        f"🔎 Workspace search ({a.get('kind','content')}): {a.get('query','?')}"),
}


def _tool_label(name: str, args: dict) -> str:
    fmt = _TOOL_LABELS.get(name)
    if fmt is None:
        return name
    try:
        return fmt(args)
    except Exception:
        # A label is cosmetic — never let formatting kill a tool call.
        return name


def _todo_summary(r: dict) -> str | None:
    rows = "\n".join(f"[{'x' if t['status']=='completed' else ' '}] {t['task']}"
                     for t in r.get("todos", []))
    return f"Progress {r.get('progress','')}\n{rows}"


# Tool → result-summary formatter (one hash lookup instead of a 10-branch chain).
# A formatter returns a string, or None to fall through to the generic summary —
# e.g. write_file when `success` is falsy, or when malformed payloads raise.
_TOOL_RESULT_SUMMARIES: dict[str, Callable[[dict], str | None]] = {
    "write_file": lambda r: (f"Wrote {r.get('path','?')} ({r.get('lines',0)} lines)"
                             if r.get("success") else None),
    "read_file":   lambda r: r.get("content", ""),
    "list_dir":    lambda r: f"{r.get('count',0)} entries:\n" + "\n".join(r.get("entries", [])),
    "grep_search": lambda r: (f"{r.get('match_count',0)} matches:\n"
                              + "\n".join(r.get("matches", []))),
    "scan_project": lambda r: f"Scanned {r.get('file_count',0)} files\n{r.get('file_tree','')}",
    "update_todo": _todo_summary,
    "multi_edit_files": lambda r: r.get("results", "done"),
    "delete_file": lambda r: f"Deleted {r.get('deleted','?')}",
}

_FILEINTEL_TOOLS = frozenset(
    ("detect_file", "file_capabilities", "run_file_op", "convert_file", "search_workspace"),
)


def _tool_result_summary(name: str, result: dict) -> str:
    if "error" in result:
        return f"Error: {result['error']}"
    fmt = _TOOL_RESULT_SUMMARIES.get(name)
    if fmt is not None:
        try:
            out = fmt(result)
        except Exception:
            out = None
        if out is not None:
            return out
    if name in _FILEINTEL_TOOLS:
        return _fileintel_summary(name, result)
    return str(result)[:2000]


def _fileintel_summary(name: str, result: dict) -> str:
    """Compact, human-readable summary for the File Intelligence tools."""
    steps = result.get("progress") or []
    prog = ("\n" + "\n".join(f"  · {s}" for s in steps)) if steps else ""

    if name == "detect_file":
        head = (f"{result.get('filename','?')} — {result.get('format','?')} "
                f"({result.get('category','?')}), {result.get('size_human','?')}")
        ops = ", ".join(result.get("operations", []))
        extra = [
            f"{k}={result[k]}"
            for k in ("dimensions", "page_count", "duration_sec", "rows", "sha256")
            if result.get(k) is not None
        ]
        return f"{head}\n{'; '.join(extra)}\nOperations: {ops}"

    if name == "file_capabilities":
        return json.dumps({k: v for k, v in result.items()
                           if k in ("category", "format", "operations", "formats")},
                          indent=1)[:2000]

    if "error" in result:
        hint = f"\nHint: {result['hint']}" if result.get("hint") else ""
        return f"Error: {result['error']}{hint}{prog}"

    # Successful run_file_op / convert_file / search_workspace
    payload = {k: v for k, v in result.items()
               if k not in ("progress", "text", "diff", "preview")}
    # Include a text preview when the op returned extracted content.
    body = json.dumps(payload, default=str)[:1500]
    text = result.get("text")
    if text:
        body += f"\n--- text ({result.get('chars', len(text))} chars) ---\n{text[:3000]}"
    if result.get("ai_task"):
        body = (f"[AI task: {result['ai_task']}] {result.get('instruction','')}\n"
                f"--- content ---\n{(result.get('text') or '')[:4000]}")
    return f"{body}{prog}"

# ── Tool declaration ───────────────────────────────────────────────────────────

_TOOL = types.Tool(function_declarations=[
    types.FunctionDeclaration(
        name="run_command",
        description=(
            f"Execute a shell command on the user's {OS_NAME} machine ({SHELL_LABEL}). "
            "Use for: running scripts, port scanning, network recon, file operations, "
            "installing packages, building/compiling, testing, launching programs. "
            f"Triggers: run, execute, scan, install, build, test, compile, launch, save, start."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "command": types.Schema(
                    type=types.Type.STRING,
                    description=f"Exact {SHELL_LABEL} command for {OS_NAME}",
                ),
                "description": types.Schema(
                    type=types.Type.STRING,
                    description="One-line human-readable description of what this command does",
                ),
            },
            required=["command", "description"],
        ),
    )
])


# ── System prompt ──────────────────────────────────────────────────────────────

def _platform_rules() -> str:
    if OS_NAME == "Windows":
        return (
            "PLATFORM: Windows.\n"
            "- Shell: CMD / PowerShell. Use Windows syntax only.\n"
            "- ipconfig (not ifconfig) | dir (not ls) | type (not cat)\n"
            "- python (not python3) | pip (not pip3)\n"
            "- ping -n 4 (not ping -c 4) | Paths use backslash: C:\\Users\\\n"
            "- nmap.exe if installed; winget or choco for packages"
        )
    if OS_NAME == "Darwin":
        return (
            "PLATFORM: macOS / zsh.\n"
            "- ifconfig for network | brew for packages\n"
            "- python3, pip3 | open <file> to launch"
        )
    return (
        "PLATFORM: Linux / bash.\n"
        "- ip addr or ifconfig | apt/dnf/pacman for packages\n"
        "- python3, pip3"
    )


def system_prompt(burp_connected: bool = False, burp_tool_count: int = 0,
                  mcp_blocks: list[str] | None = None,
                  context: "_broker.ContextBundle | None" = None) -> str:
    """Build the full system instruction including platform rules and context.

    The large static body is assembled once and cached (see _static_prompt); the
    memories/rules tails are cached too and rebuilt only when those resources
    actually change. Previously this rebuilt a ~7 KB f-string and ran two DB
    queries on every single agent turn.

    ⚠️ Task 8: BURP KEEPS ITS OWN HAND-WRITTEN BLOCK. Every MCP server added
    since gets the generic one from `McpBridge.prompt_block()` and arrives here
    pre-rendered in `mcp_blocks`. Folding Burp into the generic form would have
    rewritten prose that
    `test_system_prompt_burp_block_only_when_connected` pins, for no gain.

    ⚠️ Task 20: `context` IS OPTIONAL AND ITS ABSENCE IS NOT A DEGRADED MODE.
    With a broker bundle the tail is every collected source; without one it is
    `broker.base_tail()` — memories then rules, from the same two caches, in the
    same order. Both paths compose through `broker.ORDER`, so there is exactly one
    answer to "what order do context sections come in", and a caller that has no
    turn to describe (a health probe, a test) is not forced to invent one.
    """
    sp = _static_prompt()

    if burp_connected:
        sp += (
            "\n\n## BURP SUITE (live — via MCP)\n"
            f"You are connected to a running Burp Suite instance and have {burp_tool_count} "
            "Burp tools available, all prefixed `burp_` (e.g. proxy history, Repeater, "
            "Intruder, active/passive Scanner, site map, send raw HTTP request, issues).\n"
            "- When the user asks anything about intercepted traffic, requests/responses, "
            "scanning a target, replaying/modifying a request, or their Burp session, CALL the "
            "relevant `burp_*` tool instead of run_command or guessing.\n"
            "- Prefer Burp tools over shell tools for HTTP interception, request replay and web "
            "vulnerability scanning; use run_command for OS-level tools (nmap, sqlmap, etc.).\n"
            "- After a Burp tool returns, summarise findings clearly (endpoints, params, issues)."
        )
    for block in (mcp_blocks or []):
        sp += block
    sp += context.prompt_tail() if context is not None else _broker.base_tail()
    return sp


def _build_static_prompt() -> str:
    """The platform-dependent but otherwise constant body of the system prompt."""
    return f"""You are Agent2 — an elite AUTONOMOUS AI software engineer and security agent, on par with Claude Code. You do not just advise — you BUILD, EDIT, RUN, and VERIFY, using tools, until the task is fully done.

{_platform_rules()}

## CONVERSATION — greetings and small talk are NOT tasks
- When the user just says hi / hello / how's it going / thanks / ok, answer like a friendly
  colleague would: one or two warm sentences in your own words, then offer what you could
  do next. Call NO tools for these — there is nothing to build or verify.
- NEVER reply with a bare "Done.", "ok", "sure", "continue" or an empty message. Those are
  not answers. Every reply must contain real content the user can read and respond to.
- Questions about you, your tools, or what you can do are answered directly from this
  prompt — don't run a command to find out.
- Match the user's energy: a casual message gets a short human reply; a work request gets
  the full autonomous treatment described below.

## AUTONOMY — finish the whole task in one go
- For any non-trivial request, FIRST call `update_todo` with the full step list, then work through it, marking each item `in_progress` → `completed` as you go. Keep the list current.
- Do NOT stop to ask permission between steps. Chain tool calls: explore → plan → create files → run → fix errors → verify. Only return your final text when the task is genuinely complete.
- Build ENTIRE projects from a single prompt: create the full directory structure and EVERY file with `write_file`, install deps and run the project with `run_command`, then confirm it works.
- If a command fails, read the error, fix the cause, and re-run — autonomously. Iterate until green.

## TOOLS (use them — never just print code and stop)
- `update_todo` — live task checklist; call first for multi-step work, update as you progress
- `scan_project` / `list_dir` / `grep_search` / `read_file` — explore before editing
- `write_file` — create/overwrite files (ACTUALLY write code to disk)
- `multi_edit_files` — precise find/replace across many files
- `delete_file` — remove files/dirs when refactoring
- `run_command` — execute shell commands: installs, builds, tests, scans, launches
- `web_search` — docs, CVEs, errors, latest info
- `save_memory` — persist important facts
- `emit_plan` — show a plan for a complex task

## FILE INTELLIGENCE — understand & process any file
Agent2 has a universal file-processing system. For ANY file the user references
(PDF, Word, Excel, PowerPoint, images, audio, video, archives, code):
- Call `detect_file(path)` FIRST to learn its type, metadata, and which operations are possible.
- Then `run_file_op(path, operation, options)` to do the work (read/summarize/analyze/
  extract/merge/split/ocr/compare/resize/compress/transcribe/…), or `convert_file` to convert.
- Infer intent automatically — "summarize this"→read+summarize, "convert this"→convert_file,
  "analyze this sheet"→run_file_op analyze, "what's in this zip"→run_file_op list.
- For AI operations (summarize/translate/rewrite/grammar) the tool returns the extracted
  text plus an instruction; YOU then produce the result and save it with `write_file` if asked.
- Batch work: call the tool per file, or pass `options.paths` to batch ops (merge, create).
- Use `search_workspace` to search many files (content, filenames, secrets, duplicates, recent).
- These tools stream progress steps and recover from a missing backend automatically — if an
  operation needs an uninstalled library/binary, relay the returned install hint to the user.

## CODE QUALITY
- Production-quality, complete, runnable code — no TODO stubs or placeholders
- Correct project structure, dependency files, and a README when building projects
- Fenced code blocks with language tags in explanations

## SECURITY WORK
- Full pentest workflows via run_command (nmap, sqlmap, nikto, gobuster, etc.)
- When Burp is connected, drive it via the `burp_*` tools

## RESPONSE STYLE
- Markdown: headers, **bold**, tables, code blocks
- Concise but complete — no filler. Summarise what you built and how to run it.
- "Concise" never means one word. A reply that is only "Done." or "ok" is a bug: say what
  you did, what you verified, and what makes sense next."""


# ── System-prompt caches ───────────────────────────────────────────────────────
# system_prompt() runs on EVERY agent turn (and every provider turn). Of its
# three parts, one is constant for the process and two change only when the user
# edits memories/rules — which the sync layer already tells us about. So each
# part is cached and the whole function becomes string concatenation in the
# common case.

_STATIC_PROMPT: str | None = None
_STATIC_LOCK = threading.Lock()


def _static_prompt() -> str:
    """The constant body, built once per process."""
    global _STATIC_PROMPT
    if _STATIC_PROMPT is None:
        with _STATIC_LOCK:
            if _STATIC_PROMPT is None:
                _STATIC_PROMPT = _build_static_prompt()
    return _STATIC_PROMPT


# ⚠️ Task 20: THESE FOUR NAMES ARE ALIASES OF `core.broker`, NOT DEFINITIONS.
# The memories/rules blocks and their VersionedCaches moved into the Context
# Broker, which now owns the composition of every context source. They are still
# bound here because `test_perf_guards.py` and `test_agent_loop.py` invalidate
# through `agent._MEM_CACHE` / `agent._RULES_CACHE`, and those tests must go on
# invalidating the cache the prompt actually reads. Rebuilding a second cache
# under these names instead would leave both tests passing while the prompt served
# a stale block — the invalidation would land on an object nobody reads.
_build_memories_block = _broker._build_memory_block
_build_rules_block = _broker._build_rules_block
_MEM_CACHE = _broker.MEM_CACHE
_RULES_CACHE = _broker.RULES_CACHE
_memories_block = _broker.memory_block
_rules_block = _broker.rules_block


# ── Context builder ────────────────────────────────────────────────────────────

def _tool_name(meta: dict) -> str:
    """The tool a stored row refers to.

    One place decides this so a `tool_call` and its `tool_result` can never
    disagree about the name — Gemini rejects a `function_response` whose name
    does not match its `function_call`, which fails the whole turn rather than
    just losing one message.

    `burp`, `mcp` and `local` are mutually exclusive by construction: the
    dispatch is an elif chain (`_LOCAL_TOOLS`, then `is_burp_tool`, then the
    other MCP bridges) and `provider_agent._tool_meta` returns exactly one of the
    three keys. So the order below is arbitrary — swapping it is undetectable,
    which is why there is no test pinning it.

    Rows written before those keys existed carry none of them, and those are
    always shell calls.
    """
    return meta.get("burp") or meta.get("mcp") or meta.get("local") or "run_command"


def _tool_args(meta: dict, content: str) -> dict:
    """Args for a `function_call`. Only calls need these; results never do.

    The fallback differs by kind: a named tool missing its args gets `{}`, while
    a shell row gets the `run_command` shape rebuilt from the legacy `cmd` key.
    Handing the shell shape to a named tool would invent parameters it has no
    schema for.
    """
    args = meta.get("args")
    if args is not None:
        return args
    if meta.get("burp") or meta.get("mcp") or meta.get("local"):
        return {}
    return {"command": meta.get("cmd", ""), "description": content}


def build_context(chat_id: str) -> list[types.Content]:
    """Fetch the last MAX_CTX_MESSAGES rows and convert to Gemini Content objects.

    Ordering note: `created_at` is `datetime('now')`, which is only
    SECOND-granular. A single agent turn writes its user / tool_call /
    tool_result / assistant rows well inside one second, so ordering by that
    column alone left the whole turn tied — and SQLite broke the tie by rowid,
    which under DESC means REVERSED. The context handed to the model then had
    the turn's messages backwards (and a tool_result sitting before its
    tool_call, which the pairing rule below silently dropped). `rowid` is the
    monotonic insertion order, so it is the real tie-break.

    This runs on every agent iteration, so it stays a single LIMITed query and a
    single pass: `meta` is parsed only for the tool rows that read it (a text
    conversation parses none), and the six near-identical `types.Content`
    constructions the tool branches used to repeat collapse to one per kind,
    with `_tool_name` / `_tool_args` deciding the name and args.
    """
    rows = qall(
        "SELECT id, role, content, meta FROM messages "
        "WHERE chat_id=? ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (chat_id, MAX_CTX_MESSAGES),
    )
    rows.reverse()

    ctx: list[types.Content] = []
    for i, r in enumerate(rows):
        role, content = r["role"], r["content"]

        if role == "user":
            ctx.append(types.Content(role="user", parts=[types.Part(text=content)]))

        elif role == "assistant":
            ctx.append(types.Content(role="model", parts=[types.Part(text=content)]))

        elif role == "tool_call":
            # Only emit the call if its result is the very next row. A
            # function_call with no matching function_response is rejected by
            # Gemini, so a dangling half would cost the turn, not the message.
            if i + 1 < len(rows) and rows[i + 1]["role"] == "tool_result":
                meta = json.loads(r.get("meta") or "{}")
                ctx.append(types.Content(
                    role="model",
                    parts=[types.Part(function_call=types.FunctionCall(
                        name=_tool_name(meta),
                        args=_tool_args(meta, content)))],
                ))

        elif role == "tool_result":
            meta = json.loads(r.get("meta") or "{}")
            response = {"output": content[:MAX_TOOL_OUTPUT]}
            if meta.get("burp"):
                response["success"] = meta.get("rc", 0) == 0
            elif meta.get("mcp"):
                # Same shape as Burp — an MCP result has no exit code, only a
                # verdict, and it is stored under `rc` for exactly that reason.
                response["success"] = meta.get("rc", 0) == 0
            elif meta.get("local"):
                response["success"] = meta.get("ok", True)
            else:
                # Shell results carry the exit code as well; it is the thing the
                # model reasons about when deciding whether a command worked.
                response["returncode"] = meta.get("rc", 0)
                response["success"] = meta.get("rc", 0) == 0
            ctx.append(types.Content(
                role="user",
                parts=[types.Part(function_response=types.FunctionResponse(
                    name=_tool_name(meta), response=response))],
            ))
    return ctx


# ── DB helpers ─────────────────────────────────────────────────────────────────

def save_msg(chat_id: str, role: str, content: str, meta: dict | None = None) -> None:
    exe(
        "INSERT INTO messages(id, chat_id, role, content, meta) VALUES(?,?,?,?,?)",
        (str(uuid.uuid4()), chat_id, role, content, json.dumps(meta or {})),
    )
    exe("UPDATE chats SET updated_at=datetime('now') WHERE id=?", (chat_id,))


# ── Blank-reply recovery ───────────────────────────────────────────────────────

def _retry_without_tools(client, api_model, context, cfg_kwargs: dict,
                         stop) -> tuple[str, Exception | None]:
    """Re-ask the model for TEXT ONLY after it returned a blank/filler reply.

    Why this exists: on short conversational turns ("hi", "thanks") a
    thinking-capable Gemini model can spend its entire `max_output_tokens` budget
    on internal thinking and return a candidate with no text part. The loop's old
    fallback turned that into a literal "Done." — a nonsense answer to "hello".

    The retry removes the two things that cause it:
      - `tools`/`tool_config`, so there is nothing to call and no tool-preamble
        tokens to spend; and
      - the thinking budget, so the whole allowance goes to visible text.

    Returns `(text, fatal)`:
      - `text`  — the recovered reply, or "" if this attempt is also unusable.
      - `fatal` — a key-level exception (quota/auth) the CALLER must handle, or
        None. This second element exists because swallowing those was a real
        bug: `call_with_retry` re-raises quota/auth immediately and deliberately,
        "so the caller's existing key-rotation / fallback logic runs". Returning
        only "" made the loop print blank_reply_notice() — telling the user "I
        didn't produce a reply that time" when their key was actually exhausted,
        while `rotator.fail()` never ran, so no key rotation happened and the
        next turn hit the same dead key.

    Never raises: a failure is reported through the return value, so recovery
    stays best-effort and the caller keeps control of the turn.
    """
    if stop.is_set():
        return "", None

    retry_kwargs = {
        k: v for k, v in cfg_kwargs.items()
        if k not in ("tools", "tool_config", "thinking_config", "max_output_tokens")
    }
    # Enough room for a real conversational reply, small enough to stay cheap.
    retry_kwargs["max_output_tokens"] = 1024

    def _ask(kwargs: dict) -> str:
        resp = call_with_retry(
            lambda: client.models.generate_content(
                model=api_model, contents=context,
                config=types.GenerateContentConfig(**kwargs),
            ),
            should_stop=stop.is_set,
        )
        cand = resp.candidates[0] if resp.candidates else None
        if not cand or not cand.content:
            return ""
        out: list[str] = []
        for p in (cand.content.parts or []):
            try:
                if p.text:
                    out.append(p.text)
            except Exception:
                pass
        return "\n".join(out).strip()

    # Prefer thinking fully off (2.5 Flash honours budget=0). Some models reject
    # a zero budget, so fall back to letting the model decide.
    #
    # A key-level failure (quota/auth) is returned to the caller instead of being
    # swallowed, and short-circuits tier 2: the second attempt would reuse the
    # same exhausted key and is guaranteed to fail, so it only wasted an API call
    # and delayed the real error.
    try:
        kwargs = dict(retry_kwargs)
        kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
        text = _ask(kwargs)
        if not is_blank_reply(text):
            return text, None
    except Exception as exc:
        if classify_error(exc) in ("quota", "auth"):
            return "", exc
        # Anything else (e.g. the documented 400 rejecting thinking_budget=0)
        # is exactly what tier 2 exists to recover from.

    try:
        text = _ask(retry_kwargs)
        return ("" if is_blank_reply(text) else text), None
    except Exception as exc:
        # Only key-level errors need the caller's rotation logic; a genuinely
        # unusable reply stays a blank reply.
        return "", (exc if classify_error(exc) in ("quota", "auth") else None)


# ── Agent loop ─────────────────────────────────────────────────────────────────

def run_agent(
    chat_id:     str,
    user_message: str,
    sid:         str,
    term_id:     str,
    model_key:   str,
    mode_key:    str,
    socketio,                    # passed in to avoid circular import
    attachments: list | None = None,
) -> None:
    """
    Main agentic loop.
    1. Saves the user message.
    2. Calls Gemini with the full context.
    3. If the response contains a tool call → run the command, feed result back.
    4. If the response is text → emit to client and return.
    Supports stop events and message editing.
    """
    # Open an isolated per-chat session (fresh cancel token + task + TODO list).
    # This is what stops Chat B from "continuing" Chat A (section 6).
    ws = _workspace.current()
    session = sessions.open(sid, chat_id, workspace_id=ws.id)
    stop = session.cancel
    ctx = ToolContext(session=session, sid=sid, chat_id=chat_id)

    # Web parity with the CLI's rich UX (items 7, 8, 10, 13). Per-turn, not a
    # singleton: the web server has N sockets in one process, so a shared
    # reporter would show tab A's stages inside tab B.
    prog = TurnProgress(
        lambda ev, payload, room: socketio.emit(ev, payload, room=room),
        room=sid, chat_id=chat_id)
    prog.stage("Planning")

    def _finish() -> None:
        # Clear the transient queue BEFORE closing the session: those rows are
        # pinned in the topbar, so a turn that ended while leaving "Running"
        # there would strand it until the next turn happened to overwrite it.
        prog.clear_finished()
        sessions.close(sid, chat_id)

    # ── Persist user message ─────────────────────────────────────────────────
    # The ORIGINAL text is what we store and display — the PIL only ever changes
    # the copy that is *sent* to the model (grammar / prompt-improvement), never
    # the user's own history.
    save_msg(chat_id, "user", user_message,
             {"attachments": [a["name"] for a in (attachments or [])]})

    # ── Personal Intelligence Layer (offline, best-effort) ────────────────────
    # 1. Learn from the ORIGINAL text so personalization tracks real behaviour.
    # 2. Optionally enhance the copy that goes to the model (opt-in grammar /
    #    improvement); on any error we fall back to the untouched message.
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
    except Exception:
        sent_message = user_message
    else:
        # Idle-time housekeeping, off the hot path (respects the autooptimize
        # toggle inside the facade). Occasional so it stays cheap.
        #
        # Deliberately in `else` with its OWN guard, not inside the try above:
        # if Thread.start() raised (thread exhaustion) while sharing that
        # handler, `sent_message` would be reset to the original AFTER
        # `pil_enhanced` had already been emitted — the UI would show an
        # enhancement the model never actually received. Optimization is
        # unrelated housekeeping; its failure must not rewrite the prompt.
        if random.random() < 0.10:
            try:
                threading.Thread(target=pil.optimize, daemon=True).start()
            except Exception:
                pass

    # Auto-title on first message
    chat = qone("SELECT title FROM chats WHERE id=?", (chat_id,))
    if chat and chat["title"] == "New Chat":
        title = user_message.strip().replace("\n", " ")[:50]
        if len(user_message) > 50:
            title += "…"
        exe("UPDATE chats SET title=? WHERE id=?", (title, chat_id))
        socketio.emit("chat_titled", {"chat_id": chat_id, "title": title}, room=sid)

    exe("UPDATE chats SET model=?, mode=? WHERE id=?", (model_key, mode_key, chat_id))

    # ── Get API client ────────────────────────────────────────────────────────
    client, key, key_label = rotator.get()
    if not client:
        msg = (
            "**No API keys configured.**\n\n"
            "Open **Keys** in the sidebar to add a Gemini API key.\n"
            "Free key: https://aistudio.google.com/app/apikey"
        )
        save_msg(chat_id, "assistant", msg)
        socketio.emit("chat_response", {"text": msg, "done": True, "tokens": 0}, room=sid)
        _finish()
        return

    # ── Resolve model / mode ──────────────────────────────────────────────────
    # ⚠️ Task 18: routing happens HERE, once, before the loop — not per iteration.
    # A per-iteration decision would let the model change mid-turn between two tool
    # calls, so the second half of a turn would be answered by a model that never
    # saw the first half's reasoning. `choose()` is total and returns the requested
    # key untouched unless routing is switched on, so the default install is
    # unchanged; `routed_model` is what the rest of the turn uses.
    mode_cfg  = MODES.get(mode_key, MODES[DEFAULT_MODE])
    decision = router.choose(model_key, message=user_message,
                             attachments=attachments,
                             context_tokens=router.estimate_tokens(user_message))
    if decision.routed and decision.model_key != model_key:
        model_key = decision.model_key
        # Persist the model the turn ACTUALLY used. The row was written above with
        # what the surface asked for, and leaving it there makes the transcript
        # claim a model that never ran — which is the same class of lie as reporting
        # a fabricated exit code (see `cli.state.trigger_cancel`).
        exe("UPDATE chats SET model=? WHERE id=?", (model_key, chat_id))
        socketio.emit("model_routed", {
            "chat_id": chat_id, "model": model_key,
            "requested": decision.requested, "reason": decision.reason,
        }, room=sid)
        socketio.emit("toast", {"msg": f"Auto-routed → {model_key}: {decision.reason}",
                                "type": "info"}, room=sid)

    # `resolve()` guards the one failure mode `AUTO` introduces: a chat row (or a
    # surface) holding "auto" must never reach the vendor as a model id.
    model_key = router.resolve(model_key)
    primary_model = model_key
    tried_models: list[str] = [model_key]

    model_cfg = MODELS.get(model_key) or MODELS.get(DEFAULT_MODEL) or next(iter(MODELS.values()))
    api_model = model_cfg["api"]
    model_group = model_cfg.get("group", "")

    # ── Build generation config ───────────────────────────────────────────────
    # Lazily bring up the Burp MCP bridge so its tools can be offered to Gemini.
    # Auto-connect only when the user enabled it; but ALWAYS offer Burp tools if
    # a session is live (e.g. connected manually via the Burp settings tab).
    burp_decls: list = []
    if burp.enabled and not burp.is_connected():
        ok, bmsg = burp.connect(timeout=8.0)
        socketio.emit("toast",
                      {"msg": bmsg, "type": "success" if ok else "warning"},
                      room=sid)
    if burp.is_connected():
        burp_decls = burp.gemini_declarations()

    # ⚠️ Task 8: the SAME rule for every other MCP server, driven by the registry.
    # It is a second loop rather than one over `registry.bridges()` because Burp's
    # branch above is separately tested (`test_agent_loop.py` patches
    # `agent2.agent.burp` with raising=True) and moving it into a loop would have
    # changed a pinned path to gain nothing.
    mcp_bridges = mcp_registry.extra_bridges()
    for _b in mcp_bridges:
        attempt = mcp_registry.ensure_connected(_b, timeout=8.0)
        if attempt is not None:
            _ok, _msg = attempt
            socketio.emit("toast",
                          {"msg": _msg, "type": "success" if _ok else "warning"},
                          room=sid)
    mcp_decls = mcp_registry.gemini_declarations_for(mcp_bridges)

    agent_tools = [_build_tools()]
    if burp_decls:
        agent_tools.append(types.Tool(function_declarations=burp_decls))
    if mcp_decls:
        agent_tools.append(types.Tool(function_declarations=mcp_decls))

    # ── Build context from DB ─────────────────────────────────────────────────
    # ⚠️ Task 20: THE CONVERSATION IS ASSEMBLED BEFORE THE GENERATION CONFIG, AND
    # THE ORDER MATTERS NOW. The Context Broker is told how large the history is so
    # it can account for it (and, from Task 22, budget around it), so the config —
    # which carries the broker's tail in its `system_instruction` — cannot be built
    # until the history exists. `cfg` is only read inside the loop, so nothing
    # between here and there notices the move.
    context = build_context(chat_id)

    # ── Feed the PIL-processed copy to the model (not the stored original) ─────
    # build_context() rebuilds the last user turn from the ORIGINAL text we saved.
    # If the PIL produced a different sent copy (grammar / improvement), swap it in
    # here so the MODEL sees the enhanced text while history/display stay original.
    if sent_message != user_message and context and context[-1].role == "user":
        context[-1] = types.Content(
            role="user", parts=[types.Part(text=sent_message)])

    # ── Recovery briefing (Task 3) ────────────────────────────────────────────
    # ⚠️ PREPENDED as its own user turn, never merged into the user's message:
    # the merged form would be stored-looking text the user never wrote, and the
    # PIL swap above rewrites `context[-1]` wholesale — a brief merged there
    # would be silently discarded on exactly the turns the PIL is enabled.
    # `brief_for_chat()` consumes it, so it reaches the model once and never
    # again; a chat with nothing to recover returns "" and nothing is inserted.
    brief = _recovery.brief_for_chat(chat_id)
    if brief:
        context.insert(max(0, len(context) - 1),
                       types.Content(role="user", parts=[types.Part(text=brief)]))

    # ── Inject file attachments into the last user turn ───────────────────────
    if attachments:
        file_parts: list[types.Part] = []
        for att in attachments:
            try:
                raw = base64.b64decode(att["data"])
                mt  = att.get("mime_type", "text/plain")
                is_text = mt.startswith("text/") or mt in (
                    "application/json", "application/xml",
                    "application/javascript", "application/yaml",
                )
                if is_text:
                    file_parts.append(types.Part(
                        text=f"\n\n[Attached: {att['name']}]\n```\n"
                             f"{raw.decode('utf-8', 'replace')[:8000]}\n```"
                    ))
                else:
                    file_parts.append(types.Part(
                        inline_data=types.Blob(mime_type=mt, data=raw)
                    ))
            except Exception as exc:
                file_parts.append(types.Part(text=f"[Attachment error: {att['name']}: {exc}]"))

        if context and context[-1].role == "user":
            context[-1] = types.Content(
                role="user",
                parts=list(context[-1].parts) + file_parts,
            )
        else:
            context.append(types.Content(role="user", parts=file_parts))

    # ── Context Broker (Task 20) ──────────────────────────────────────────────
    # ONE assembly per turn, not per iteration: the situational sources read the
    # filesystem, git and the task table, and the prompt they build is reused for
    # every one of up to MAX_AGENT_ITERS calls. `assemble()` is total — a source
    # that fails is recorded on `bundle.errors` and simply absent — so there is no
    # failure mode here that can end a turn.
    bundle = _broker.assemble(
        chat_id=chat_id,
        message=sent_message,
        model_key=model_key,
        mode_key=mode_key,
        surface="web",
        conversation_messages=len(context),
        conversation_tokens=router.estimate_tokens(
            "".join(p.text or "" for c in context for p in (c.parts or []))),
    )
    if bundle.errors:
        for _src, _err in bundle.errors.items():
            alog.context_source_failed(_src, _err)

    cfg_kwargs: dict = {
        "system_instruction": system_prompt(
            burp_connected=bool(burp_decls),
            burp_tool_count=len(burp_decls),
            mcp_blocks=[b.prompt_block() for b in mcp_bridges],
            context=bundle,
        ),
        "tools": agent_tools,
        "tool_config": types.ToolConfig(
            function_calling_config=types.FunctionCallingConfig(mode="AUTO")
        ),
    }
    if mode_cfg.get("max_tokens", 0) > 0:
        cfg_kwargs["max_output_tokens"] = mode_cfg["max_tokens"]

    if mode_cfg.get("thinking") and mode_cfg.get("thinking_budget", 0) > 0:
        # ⚠️ `config.THINKING_GROUPS`, never a literal tuple. Both readers of this
        # fact used to carry their own copy, and a model group added to
        # `config.MODELS` was then absent from both — so `thinking` mode silently
        # attached no budget and nothing reported it.
        if supports_thinking(model_group):
            try:
                cfg_kwargs["thinking_config"] = types.ThinkingConfig(
                    thinking_budget=mode_cfg["thinking_budget"]
                )
            except Exception:
                pass   # model doesn't support thinking → skip silently

    cfg = types.GenerateContentConfig(**cfg_kwargs)

    total_tokens = 0

    # Hoisted out of the loop: it closes over nothing that changes per iteration
    # (socketio/sid/MAX_RETRIES are all fixed for the turn), so rebuilding the
    # function object and its closure cell on every pass was pure waste.
    def _on_retry(attempt, delay, exc):
        socketio.emit(
            "toast",
            {"msg": f"Network hiccup — retrying ({attempt}/{MAX_RETRIES}) in {delay:.1f}s…",
             "type": "warning"},
            room=sid,
        )

    # ── Main loop ─────────────────────────────────────────────────────────────
    for _iteration in range(MAX_AGENT_ITERS):

        # Check if user requested stop
        if stop.is_set():
            socketio.emit("chat_response",
                          {"text": "_Stopped by user._", "done": True, "tokens": total_tokens},
                          room=sid)
            _finish()
            return

        prog.stage("Calling Model", model_key)
        # ── Call Gemini ───────────────────────────────────────────────────────
        # Transient network failures (read timeouts, 5xx, "overloaded", reset
        # connections) are the #1 cause of Agent2 dropping a task mid-run. We
        # retry those in-place with exponential backoff instead of ending the
        # turn. Only genuinely fatal errors (quota, auth, invalid model) fall
        # through to the handler below.
        _call_started = time.time()
        try:
            resp = call_with_retry(
                # `c=client` binds the CURRENT key's client at call-construction
                # time. Quota rotation rebinds `client` in the handler below, and
                # a late-bound closure would let an in-flight retry silently jump
                # to the new key mid-backoff.
                #
                # ⚠️ `m=api_model` is bound for the SAME reason, and it became
                # load-bearing with Task 19: `api_model` used to be invariant for the
                # whole turn, and the fallback path now reassigns it. A late-bound
                # read would let a retry that started under one model finish against
                # another — the request would carry a model id the surrounding
                # bookkeeping does not know it used.
                lambda c=client, m=api_model: c.models.generate_content(
                    model=m, contents=context, config=cfg
                ),
                on_retry=_on_retry,
                should_stop=stop.is_set,
            )
        except Exception as exc:
            es = str(exc)
            kind = classify_error(exc)
            is_quota     = kind == "quota"
            is_model_err = kind == "invalid_model"
            rotator.fail(key, quota=is_quota)

            # Task 19: record the failed call before deciding anything, so a turn
            # that ends here still leaves an auditable row. Latency is measured
            # around the whole `call_with_retry`, i.e. including its backoffs —
            # which is the number that answers "why did that take 40 seconds".
            _latency = int((time.time() - _call_started) * 1000)
            router.record_attempt(
                model=model_key, ok=False, latency_ms=_latency,
                primary_model=primary_model,
                fallback_of=(tried_models[-2] if len(tried_models) > 1 else ""),
                failure_kind=kind or "unknown", failure_reason=es,
                chat_id=chat_id, session_id=sid)
            router.note_failure(model_key, kind)

            if is_quota:
                c2, k2, l2 = rotator.get()
                if c2 and k2 != key:
                    client, key, key_label = c2, k2, l2
                    socketio.emit("toast",
                                  {"msg": f"Quota hit — switched to key #{l2}", "type": "warning"},
                                  room=sid)
                    continue   # retry with new key

            # If the user cancelled during a backoff, end quietly.
            if stop.is_set():
                socketio.emit("chat_response",
                              {"text": "_Stopped by user._", "done": True, "tokens": total_tokens},
                              room=sid)
                _finish()
                return

            # ── Task 19: try a different model before giving up ───────────────
            # ⚠️ THIS IS A `continue`, NOT A RESTART. The context list, the token
            # tally, `ctx`/`prog`, the checkpoint trail and the cancel token all
            # carry over untouched — which is what "preserve task/session state"
            # means and what stops a fallback from replaying tool calls that have
            # already run (rule 20). Only `model_key` and `api_model` change.
            #
            # `next_model` returns "" when no further attempt is warranted (auth
            # failures, hop budget spent, nothing else configured), and that answer
            # is honoured rather than second-guessed — ignoring it is precisely the
            # endless retry Task 19 forbids.
            #
            # ⚠️ `allow_custom=False`: a `custom:<id>` model is served by
            # `llm/provider_agent.py`, a different loop with a different client and
            # a different wire format. Offering one here would mean sending
            # `custom:ab12` to `generate_content` as a model id — a confusing vendor
            # 404 dressed up as a fallback. Declining it is the honest answer.
            #
            # `cfg` is deliberately NOT rebuilt: it carries the system prompt, the
            # tool declarations and the thinking budget, none of which depend on
            # which model id is called, and every built-in model is in a
            # thinking-capable group. Rebuilding it would re-run `system_prompt()`
            # mid-turn and could hand the model a different prompt than the one the
            # first half of the conversation was answered under.
            alt = router.next_model(model_key, kind=kind, tried=tried_models,
                                    signals=decision.signals, allow_custom=False)
            if alt and alt in MODELS:
                previous = model_key
                model_key = alt
                tried_models.append(alt)
                api_model = MODELS[alt]["api"]
                alog.event("agent.model_fallback", frm=previous, to=alt,
                           failure=kind or "unknown", chat=chat_id)
                socketio.emit("model_fallback", {
                    "chat_id": chat_id, "from": previous, "to": alt,
                    "reason": kind or "error",
                }, room=sid)
                socketio.emit("toast", {
                    "msg": f"{previous} failed ({kind or 'error'}) — trying {alt}",
                    "type": "warning"}, room=sid)
                continue

            hint = (
                f"\n\n> Model `{api_model}` may not be available on your key tier. "
                "Try **2.5 Flash**."
                if is_model_err else (
                    "\n\n> This looks like a temporary network/server issue. "
                    "It was retried automatically — please try again."
                    if kind == "transient" else ""
                )
            )
            if len(tried_models) > 1:
                hint += ("\n\n> Also tried: "
                         + ", ".join(f"`{m}`" for m in tried_models[1:]) + ".")
            msg = f"**API Error ({api_model}):** {es}{hint}"
            save_msg(chat_id, "assistant", msg)
            socketio.emit("chat_response", {"text": msg, "done": True, "tokens": total_tokens}, room=sid)
            _finish()
            return

        # Task 19: a successful call clears the model's failure history and closes
        # its breaker. Recorded per call rather than per turn because a turn is many
        # calls, and "which call was slow" is the question the ledger answers.
        router.record_attempt(
            model=model_key, ok=True,
            latency_ms=int((time.time() - _call_started) * 1000),
            primary_model=primary_model,
            fallback_of=(tried_models[-2] if len(tried_models) > 1 else ""),
            chat_id=chat_id, session_id=sid)
        router.note_success(model_key)

        # ── Parse response safely ─────────────────────────────────────────────
        try:
            candidate = resp.candidates[0] if resp.candidates else None
            if candidate is None or candidate.content is None:
                finish = getattr(candidate, "finish_reason", "UNKNOWN") if candidate else "NO_CANDIDATE"
                msg = (
                    f"**Empty response** (finish_reason: `{finish}`).\n\n"
                    "The model returned no content. Try rephrasing your message "
                    "or switching to a different model."
                )
                save_msg(chat_id, "assistant", msg)
                socketio.emit("chat_response", {"text": msg, "done": True, "tokens": total_tokens}, room=sid)
                _finish()
                return
            parts = candidate.content.parts or []
        except (IndexError, AttributeError) as exc:
            msg = f"**Response parse error:** {exc}. Try again or switch models."
            save_msg(chat_id, "assistant", msg)
            socketio.emit("chat_response", {"text": msg, "done": True, "tokens": total_tokens}, room=sid)
            _finish()
            return

        # ── Extract text + function call ──────────────────────────────────────
        func_call: types.FunctionCall | None = None
        texts: list[str] = []
        for idx, p in enumerate(parts):
            try:
                if p.function_call and p.function_call.name:
                    func_call = p.function_call
                elif p.text and not getattr(p, "thought", False):
                    texts.append(p.text)
            except Exception as exc:
                # One malformed part must not abort the turn — but a dropped
                # part may be the one carrying the turn's function_call, i.e.
                # the model's primary action silently never runs. That gets a
                # log line rather than silence.
                #
                # Nothing is re-read off `p` here on purpose: the attribute
                # access is what raised, so touching it again could raise from
                # inside the handler and take the whole turn down with it.
                # `alog.event` is itself guaranteed non-raising.
                alog.event("agent.part_skipped", index=idx,
                           error=f"{type(exc).__name__}: {exc}")

        # ── Token accounting ──────────────────────────────────────────────────
        try:
            tok = getattr(resp.usage_metadata, "total_token_count", 0) or 0
        except Exception:
            tok = 0
        total_tokens += tok
        if tok and key_label:
            rotator.record_usage(key_label, tok)
            socketio.emit("key_usage_update",
                          {"label": key_label, "tokens": tok, "keys": rotator.status()},
                          room=sid)
        socketio.emit("token_update", {"chat_id": chat_id, "tokens": total_tokens}, room=sid)

        # ── Handle tool call ──────────────────────────────────────────────────
        if func_call and func_call.name == "run_command":
            if stop.is_set():
                socketio.emit("chat_response",
                              {"text": "_Stopped by user._", "done": True, "tokens": total_tokens},
                              room=sid)
                _finish()
                return

            args = dict(func_call.args)
            cmd  = args.get("command", "")
            desc = args.get("description", "Running…")

            save_msg(chat_id, "tool_call", desc, {"args": args, "cmd": cmd})
            socketio.emit("chat_tool_call",
                          {"command": cmd, "description": desc,
                           "shell": SHELL_LABEL, "tool": "run_command"},
                          room=sid)

            # Items 7/8/13: the shell command becomes a visible stage, a queue
            # row and a feed line. `cmd[:60]` because these render in a topbar
            # strip — a 300-char one-liner would blow the layout apart.
            short_cmd = cmd[:60]
            prog.stage("Executing Tools", short_cmd)
            prog.task(short_cmd, "running")
            prog.activity(f"$ {short_cmd}")

            # run_command is the ONE tool that bypasses `dispatch_tool` (it goes
            # through terminal.stream_command), so its checkpoint sub-step has to
            # be recorded here rather than at the shared chokepoint — BOTH halves
            # of it. The pre-call note is what lets the next process see that a
            # shell command was in flight: a command is the least reversible thing
            # the agent does and its effects cannot be read back off disk, so
            # recovery has to ask about it instead of running it twice (rule 21).
            ctx.note_tool("run_command", detail=cmd)
            # Task 4: attribute the execution to the task it belongs to, so
            # "which command is this turn stuck on?" is answerable.
            output, rc = stream_command(
                cmd, sid, term_id, socketio,
                task_id=ctx.current_task_id(),
                session_id=ctx.existing_task_session(),
            )
            save_msg(chat_id, "tool_result", output[:10_000], {"rc": rc, "cmd": cmd})
            ctx.note_tool("run_command", ok=rc == 0, detail=cmd)
            prog.task(short_cmd, "completed" if rc == 0 else "failed")
            prog.activity(f"exit {rc}  ·  {short_cmd}",
                          "success" if rc == 0 else "error")
            # Shell output already streams live into the terminal pane; no chat
            # result block needed (that would duplicate it). The command-result
            # card (item 6) is emitted by `stream_command` itself, so BOTH agent
            # loops get it from one place rather than each building its own.

            context.append(types.Content(
                role="model",
                parts=[types.Part(function_call=types.FunctionCall(name="run_command", args=args))],
            ))
            context.append(types.Content(
                role="user",
                parts=[types.Part(function_response=types.FunctionResponse(
                    name="run_command",
                    response={
                        "output":     output[:MAX_TOOL_OUTPUT],
                        "returncode": rc,
                        "success":    rc == 0,
                    },
                ))],
            ))
            # Continue loop → Gemini will now summarise the output

        # ── Handle local file / utility tools (read, write, list, grep, todo…) ─
        elif func_call and func_call.name in _LOCAL_TOOLS:
            if stop.is_set():
                socketio.emit("chat_response",
                              {"text": "_Stopped by user._", "done": True, "tokens": total_tokens},
                              room=sid)
                _finish()
                return

            tname = func_call.name
            targs = dict(func_call.args)
            desc  = _tool_label(tname, targs)

            save_msg(chat_id, "tool_call", desc, {"args": targs, "local": tname})
            socketio.emit("chat_tool_call",
                          {"command": f"{tname}(…)", "description": desc,
                           "shell": "tool", "tool": tname},
                          room=sid)

            # Items 7/8/13: classify the tool into a stage via the shared table,
            # so "Reading Files" / "Editing Files" are real rather than a generic
            # "Executing Tools" for everything the model calls.
            prog.stage(stage_for_tool(tname), desc[:60])
            prog.task(desc[:60], "running")

            # Capture the diff BEFORE the write — write_file overwrites; once it runs
            # the old content is gone. capture_for() returns [] for non-file tools.
            changes = _diffs.capture_for(tname, targs)

            result = dispatch_tool(tname, targs, ctx)
            summary = _tool_result_summary(tname, result)
            save_msg(chat_id, "tool_result", summary[:10_000],
                     {"ok": "error" not in result, "local": tname})
            socketio.emit("chat_tool_result",
                          {"tool": tname, "summary": summary[:2000],
                           "ok": "error" not in result}, room=sid)
            _ok = "error" not in result
            prog.task(desc[:60], "completed" if _ok else "failed")
            prog.activity(desc[:80], "success" if _ok else "error")

            # The checklist is durable state now, so the browser is sent the
            # STORED list rather than the arguments the model just sent. Those
            # two differ whenever the model re-sends a stale status: honouring
            # the request would show progress going backwards (core/tasks.py).
            if tname == "update_todo" and result.get("persisted"):
                from agent2.core import tasks as _tasks
                socketio.emit("chat_tasks",
                              _tasks.payload(str(result.get("session_id") or "")),
                              room=sid)

            # Emit each diff as a separate event so the Web UI can render them inline.
            # The CLI will render these from its own store; the Web UI gets them here.
            if changes and "error" not in result:
                for ch in changes:
                    socketio.emit("chat_file_diff", ch.to_payload(), room=sid)
                    _diffs.store.add(ch)  # Accumulate into the shared session store
                prog.add_changes(changes)  # …and into THIS turn's recap

            context.append(types.Content(
                role="model",
                parts=[types.Part(function_call=types.FunctionCall(name=tname, args=targs))],
            ))
            context.append(types.Content(
                role="user",
                parts=[types.Part(function_response=types.FunctionResponse(
                    name=tname,
                    response={k: (str(v)[:MAX_TOOL_OUTPUT] if isinstance(v, str) else v)
                              for k, v in result.items()},
                ))],
            ))
            # Continue loop → model uses the tool result

        # ── Handle Burp Suite MCP tool call ───────────────────────────────────
        elif func_call and burp.is_burp_tool(func_call.name):
            if stop.is_set():
                socketio.emit("chat_response",
                              {"text": "_Stopped by user._", "done": True, "tokens": total_tokens},
                              room=sid)
                _finish()
                return

            bname = func_call.name
            bargs = dict(func_call.args)
            desc  = f"Burp: {bname}"

            save_msg(chat_id, "tool_call", desc, {"args": bargs, "burp": bname})
            socketio.emit("chat_tool_call",
                          {"command": f"{bname}({', '.join(f'{k}={v}' for k, v in bargs.items())[:200]})",
                           "description": desc, "shell": "Burp MCP", "tool": bname},
                          room=sid)

            prog.stage("Executing Tools", desc[:60])
            prog.task(desc[:60], "running")

            b_result = burp.call_tool(bname, bargs)
            b_out    = b_result.get("output") or b_result.get("error") or "(no output)"
            save_msg(chat_id, "tool_result", str(b_out)[:10_000],
                     {"rc": 0 if b_result.get("success") else 1, "burp": bname})
            socketio.emit("chat_tool_result",
                          {"tool": bname, "summary": str(b_out)[:2000],
                           "ok": bool(b_result.get("success"))}, room=sid)
            _bok = bool(b_result.get("success"))
            prog.task(desc[:60], "completed" if _bok else "failed")
            prog.activity(desc[:80], "success" if _bok else "error")

            context.append(types.Content(
                role="model",
                parts=[types.Part(function_call=types.FunctionCall(name=bname, args=bargs))],
            ))
            context.append(types.Content(
                role="user",
                parts=[types.Part(function_response=types.FunctionResponse(
                    name=bname,
                    response={k: str(v)[:MAX_TOOL_OUTPUT] if isinstance(v, str) else v
                              for k, v in b_result.items()},
                ))],
            ))
            # Continue loop → Gemini will now use the Burp result

        # ── Handle any other MCP tool call (OWASP ZAP, …) ──────────────────────
        # ⚠️ AFTER Burp's branch, never before it. `resolve()` would answer for
        # Burp too, so an earlier placement would quietly take over the tested
        # path — same behaviour today, a different code path under the test.
        elif func_call and (mcp_bridge := mcp_registry.resolve(func_call.name)):
            if stop.is_set():
                socketio.emit("chat_response",
                              {"text": "_Stopped by user._", "done": True, "tokens": total_tokens},
                              room=sid)
                _finish()
                return

            mname = func_call.name
            margs = dict(func_call.args)
            label = mcp_bridge.LABEL
            desc  = f"{label}: {mname}"

            save_msg(chat_id, "tool_call", desc,
                     {"args": margs, "mcp": mname, "server": mcp_bridge.SERVER_KEY})
            socketio.emit("chat_tool_call",
                          {"command": f"{mname}({', '.join(f'{k}={v}' for k, v in margs.items())[:200]})",
                           "description": desc, "shell": f"{label} MCP", "tool": mname},
                          room=sid)

            prog.stage("Executing Tools", desc[:60])
            prog.task(desc[:60], "running")

            m_result = mcp_bridge.call_tool(mname, margs)
            m_out    = m_result.get("output") or m_result.get("error") or "(no output)"
            save_msg(chat_id, "tool_result", str(m_out)[:10_000],
                     {"rc": 0 if m_result.get("success") else 1, "mcp": mname,
                      "server": mcp_bridge.SERVER_KEY})
            socketio.emit("chat_tool_result",
                          {"tool": mname, "summary": str(m_out)[:2000],
                           "ok": bool(m_result.get("success"))}, room=sid)
            _mok = bool(m_result.get("success"))
            prog.task(desc[:60], "completed" if _mok else "failed")
            prog.activity(desc[:80], "success" if _mok else "error")

            context.append(types.Content(
                role="model",
                parts=[types.Part(function_call=types.FunctionCall(name=mname, args=margs))],
            ))
            context.append(types.Content(
                role="user",
                parts=[types.Part(function_response=types.FunctionResponse(
                    name=mname,
                    response={k: str(v)[:MAX_TOOL_OUTPUT] if isinstance(v, str) else v
                              for k, v in m_result.items()},
                ))],
            ))
            # Continue loop → Gemini will now use the MCP result

        # ── Final text response ───────────────────────────────────────────────
        else:
            final = "\n".join(texts).strip()

            # A blank/filler reply ("", "Done.", "ok") is what made short
            # conversational turns look broken. Before accepting it, retry once
            # with tools OFF and no output cap: with nothing to call and room to
            # speak, the model answers the message instead of burning the budget
            # on thinking. Only if that also comes back empty do we say so.
            if is_blank_reply(final):
                retry_text, fatal = _retry_without_tools(
                    client, api_model, context, cfg_kwargs, stop)
                if fatal is not None:
                    # A key-level failure during recovery is NOT a blank reply.
                    # Mark the key and rotate exactly as the main call does —
                    # reporting "I didn't produce a reply" for an exhausted key
                    # would hide the cause AND leave the dead key in rotation.
                    kind = classify_error(fatal)
                    rotator.fail(key, quota=(kind == "quota"))
                    if kind == "quota":
                        c2, k2, l2 = rotator.get()
                        if c2 and k2 != key:
                            client, key, key_label = c2, k2, l2
                            socketio.emit(
                                "toast",
                                {"msg": f"Quota hit — switched to key #{l2}",
                                 "type": "warning"},
                                room=sid)
                            continue        # retry the turn on the new key
                    msg = f"**API Error ({api_model}):** {str(fatal)[:400]}"
                    save_msg(chat_id, "assistant", msg)
                    socketio.emit("chat_response",
                                  {"text": msg, "done": True, "tokens": total_tokens},
                                  room=sid)
                    _finish()
                    return
                if retry_text:
                    final = retry_text
                else:
                    final = blank_reply_notice(
                        getattr(candidate, "finish_reason", None))

            save_msg(chat_id, "assistant", final)
            socketio.emit("chat_response", {"text": final, "done": True, "tokens": total_tokens}, room=sid)
            # Items 8 + 10: close the trail, then report what the turn actually
            # did. `file_summary` self-gates on files > 0, so a plain question
            # gets the "Done" stage and nothing else — no empty recap card.
            prog.stage("Done")
            prog.file_summary()
            _finish()
            return

    # Exceeded max iterations
    msg = f"Agent reached max iterations ({MAX_AGENT_ITERS})."
    save_msg(chat_id, "assistant", msg)
    socketio.emit("chat_response", {"text": msg, "done": True, "tokens": total_tokens}, room=sid)
    _finish()
