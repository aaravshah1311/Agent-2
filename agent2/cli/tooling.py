# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/cli/tooling.py
─────────────────────
What the CLI tells the model it can do, and what actually happens when it asks.

⚠️ ONE BACKEND, AND IT IS AN ABSENCE RATHER THAN A CONVENTION.
Every filesystem- or sandbox-sensitive tool is routed to `agent2.tools`, the
shared workspace-confined registry the Web UI uses — `_SHARED_TOOLS` is that
list. Only genuinely presentation-layer tools stay local: `web_search`,
`save_memory` and `emit_plan` print into *this* terminal, so delegating them
would change their behaviour. This module therefore performs **no filesystem
operation at all** — no `open`, no `read_text`/`write_text`, no `os.walk`, no
`mkdir` — and that is asserted structurally, not promised, because the claim was
false for a whole phase: `_impl_read`, `_impl_write`, `_impl_scan_project` and
`_impl_multi_edit` sat right here, resolving `Path(args["path"]).expanduser()`
and calling bare `open()`/`write_text()`. Three were unreachable (`dispatch_tool`
tests `_SHARED_TOOLS` first), but `_impl_read` was live behind the `/read` slash
command — a human-facing filesystem read with no `_ws.validate_path` confinement,
no capability gate (`read_file` is `CAP_READ`, which `AGENT2_DENY_CAPS` subtracts
process-wide), no `alog.tool_exec` audit line, and its own private 64 000-char cap
where `agent2.tools` and the docs both say 100 000. A second sandbox with its own
bugs is exactly what that was.

⚠️ A HUMAN-FACING COMMAND THAT PERFORMS A GATED OPERATION STILL ASKS THE GATE.
`/read` now goes through `dispatch_tool("read_file", …)`, the same reason `/run`
carries its own exec gate rather than trusting that the operator typed it: the
capability model describes *this process*, CLI included, so a command that
bypasses it makes `AGENT2_DENY_CAPS` a statement about the browser alone.

⚠️ `_build_tools()` IS WHAT THE MODEL IS TOLD EXISTS, AND `dispatch_tool()` IS
WHAT CAN BE ROUTED. If a name is advertised but not dispatchable, the model calls
it and gets "not registered" — which costs the turn. The two are pinned against
each other by `test_every_advertised_tool_is_dispatchable`, which walks
`_build_tools()` rather than `_SHARED_TOOLS`; comparing the frozenset to itself
was tautological and sabotage proved it (dropping "list_dir" left it green).

⚠️ `dispatch_tool` resolves `_impl_*` through THIS module's globals. A test that
swaps one must patch `agent2.cli.tooling`, not the `agent2cli` re-export — the
alias is a separate binding, same shape as the palette snapshot bug.

Layer: env / models / render / store → tooling.
"""

import json
import os
import urllib.parse
import urllib.request

from agent2.cli.env import OS_NAME, _burp, _BURP_OK, _mcp_registry, _MCP_OK, gtypes
from agent2.cli.models import SHELL_LABEL
from agent2.cli.render import print_plan
from agent2.cli.store import add_mem

# ── The three CLI-local tool implementations ──────────────────────────────────
# There is no filesystem code in this module, and that absence is the invariant —
# see the module docstring. Anything that touches a path goes to `agent2.tools`.

def _impl_search(args: dict) -> dict:
    q = args.get("query", "")
    try:
        url = "https://api.duckduckgo.com/?" + urllib.parse.urlencode(
            {"q": q, "format": "json", "no_html": "1", "skip_disambig": "1"})
        req = urllib.request.Request(url, headers={"User-Agent": "Agent 2CLI/2.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode())
        results = []
        if data.get("AbstractText"):
            results.append({"title": data.get("Heading",""), "snippet": data["AbstractText"][:400]})
        for t in data.get("RelatedTopics", [])[:4]:
            if isinstance(t, dict) and t.get("Text"):
                results.append({"title": t["Text"][:80], "snippet": t["Text"][:300]})  # noqa: PERF401
        return {"query": q, "results": results[:5]} if results else {"query": q, "results": [], "note": "No results"}
    except Exception as ex:
        return {"error": str(ex), "query": q}

def _impl_save_mem(args: dict) -> dict:
    c = args.get("content", "").strip()
    if not c: return {"error": "content required"}
    imp  = min(10, max(1, int(args.get("importance", 5))))
    tags = [t.strip() for t in args.get("tags", "").split(",") if t.strip()]
    add_mem(c, imp, tags)
    return {"saved": True}

def _impl_plan(args: dict) -> dict:
    title = args.get("title", "Plan")
    # The model sends `steps` as a JSON array string; anything else is shown as a
    # single step rather than failing the call. Narrowed from a bare `except:`,
    # which also swallowed KeyboardInterrupt — Ctrl+C during a plan emit was
    # silently turned into a one-step plan.
    try:
        steps = json.loads(args.get("steps", "[]"))
    except Exception:
        steps = [args.get("steps", "")]
    print_plan(title, steps)
    return {"plan_emitted": True}


# ⚠️ ONE BACKEND. Every filesystem- or sandbox-sensitive tool — and every File
# Intelligence tool — is routed to the shared, workspace-confined registry in
# `agent2.tools`; only the three CLI-presentation tools above stay local.
# ⚠️ A name dropped from this frozenset does not fall back to anything: it falls
# through to "not registered", which is loud. That is deliberate. The CLI used to
# carry its own `_impl_read`/`_impl_write`/`_impl_scan_project`/`_impl_multi_edit`
# beside this list, so a dropped name silently landed on an unconfined copy with
# no `_safe_path`, no capability gate and no audit line — a second sandbox with
# its own bugs, exactly as the docstring warns. They are gone; keep them gone.
# Module-level and frozen: this used to be rebuilt on every single tool call.
_SHARED_TOOLS = frozenset((
    "read_file", "write_file", "scan_project", "multi_edit_files",
    "list_dir", "delete_file", "grep_search", "update_todo",
    "update_project_doc",
    "detect_file", "file_capabilities", "run_file_op", "convert_file",
    "search_workspace",
))


# ── The CLI's tool context ────────────────────────────────────────────────────
# ⚠️ `update_todo` is chat-scoped durable state, so the CLI needs a real
# ToolContext for it. Dispatching with `ctx=None` sent the checklist to
# `tools._TODO_FALLBACK`, a module-level list — which is why the CLI's task list
# never survived a restart. The context is cached per (chat, cwd) so every tool
# call in a session merges into ONE task session instead of opening a new one on
# every call.
_TOOL_CTX: dict = {"key": None, "ctx": None}


def tool_ctx():
    """The shared ToolContext for this CLI session (created on first use).

    Keyed on (chat id, cwd): a `/load` that re-binds the chat, or a directory
    switch, must not keep writing into the previous session's checklist.
    """
    from agent2.cli.state import S
    from agent2.tools import ToolContext
    chat_id = (S.chat or {}).get("id", "") or ""
    key = (chat_id, os.getcwd())
    if _TOOL_CTX["key"] != key or _TOOL_CTX["ctx"] is None:
        _TOOL_CTX["key"] = key
        _TOOL_CTX["ctx"] = ToolContext(sid="cli", chat_id=chat_id)
    return _TOOL_CTX["ctx"]


def reset_tool_ctx() -> None:
    """Drop the cached context (used by `/clearhistory`)."""
    _TOOL_CTX["key"] = None
    _TOOL_CTX["ctx"] = None


def dispatch_tool(name: str, args: dict) -> dict:
    if name in _SHARED_TOOLS:
        from agent2.tools import dispatch_tool as _shared_dispatch
        return _shared_dispatch(name, args, tool_ctx())
    if name == "web_search":       return _impl_search(args)
    if name == "save_memory":      return _impl_save_mem(args)
    if name == "emit_plan":        return _impl_plan(args)
    if _BURP_OK and _burp and _burp.is_burp_tool(name):
        return _burp.call_tool(name, args)
    # Task 8: every other MCP server, after Burp for the same reason as in both
    # agent loops — `resolve()` answers for Burp too, and Burp's line is the one
    # that already had callers.
    if _MCP_OK and _mcp_registry:
        _bridge = _mcp_registry.resolve(name)
        if _bridge is not None:
            return _bridge.call_tool(name, args)
    # Unknown tool → EXACT registry message (section 4).
    return {"error": f'Tool "{name}" is not registered.'}

# ── Gemini tool declarations ────────────────────────────────────────────────────
def _build_tools():
    S = gtypes.Schema; T = gtypes.Type
    return gtypes.Tool(function_declarations=[
        gtypes.FunctionDeclaration(name="run_command",
            description=f"Execute a shell command on {OS_NAME} ({SHELL_LABEL}). Use for running scripts, installs, scans, builds.",
            parameters=S(type=T.OBJECT, properties={
                "command":     S(type=T.STRING),
                "description": S(type=T.STRING),
                "cwd":         S(type=T.STRING),
            }, required=["command","description"])),
        gtypes.FunctionDeclaration(name="read_file",
            description="Read a file's contents. Always read before editing.",
            parameters=S(type=T.OBJECT, properties={
                "path":       S(type=T.STRING),
                "start_line": S(type=T.INTEGER),
                "end_line":   S(type=T.INTEGER),
            }, required=["path"])),
        gtypes.FunctionDeclaration(name="write_file",
            description="Create or overwrite a file with the given content. Use for creating new files. Parent directories are created automatically.",
            parameters=S(type=T.OBJECT, properties={
                "path":    S(type=T.STRING),
                "content": S(type=T.STRING),
            }, required=["path","content"])),
        gtypes.FunctionDeclaration(name="web_search",
            description="Search the web for CVEs, docs, error messages, latest info.",
            parameters=S(type=T.OBJECT, properties={
                "query":       S(type=T.STRING),
                "max_results": S(type=T.INTEGER),
            }, required=["query"])),
        gtypes.FunctionDeclaration(name="save_memory",
            description="Save an important fact to long-term memory (persists across sessions).",
            parameters=S(type=T.OBJECT, properties={
                "content":    S(type=T.STRING),
                "importance": S(type=T.INTEGER),
                "tags":       S(type=T.STRING),
            }, required=["content"])),
        gtypes.FunctionDeclaration(name="emit_plan",
            description="Show a step-by-step plan before a complex multi-step task.",
            parameters=S(type=T.OBJECT, properties={
                "title": S(type=T.STRING),
                "steps": S(type=T.STRING),
            }, required=["title","steps"])),
        gtypes.FunctionDeclaration(name="scan_project",
            description="Recursively scan a project directory and return a file tree + content of ALL code/config files. Use this AUTOMATICALLY whenever the user mentions a project, asks to check code, add features, or fix bugs. Pass the project path.",
            parameters=S(type=T.OBJECT, properties={
                "path": S(type=T.STRING),
            }, required=["path"])),
        gtypes.FunctionDeclaration(name="multi_edit_files",
            description="Edit multiple files at once by replacing exact text snippets. Each edit has path, old_text (exact match), new_text (replacement). Use for renaming, refactoring, or patching across files.",
            parameters=S(type=T.OBJECT, properties={
                "edits": S(type=T.ARRAY, items=S(type=T.OBJECT, properties={
                    "path": S(type=T.STRING),
                    "old_text": S(type=T.STRING),
                    "new_text": S(type=T.STRING)
                }))
            }, required=["edits"])),
        gtypes.FunctionDeclaration(name="list_dir",
            description="List a directory's files and subfolders. Use to explore project structure before reading/editing.",
            parameters=S(type=T.OBJECT, properties={
                "path": S(type=T.STRING),
            }, required=["path"])),
        gtypes.FunctionDeclaration(name="delete_file",
            description="Delete a file or directory (recursively). Use when refactoring or removing artifacts.",
            parameters=S(type=T.OBJECT, properties={
                "path": S(type=T.STRING),
            }, required=["path"])),
        gtypes.FunctionDeclaration(name="grep_search",
            description="Regex-search file contents across a directory tree. Returns file:line: matches. Use to locate symbols/usages.",
            parameters=S(type=T.OBJECT, properties={
                "pattern": S(type=T.STRING),
                "path":    S(type=T.STRING),
                "glob":    S(type=T.STRING),
            }, required=["pattern"])),
        gtypes.FunctionDeclaration(name="update_todo",
            description="Create/update a live TODO checklist for a multi-step build. Pass the full list each time with each item's status (pending|in_progress|completed). Call FIRST for big tasks, then update as you finish steps.",
            parameters=S(type=T.OBJECT, properties={
                "todos": S(type=T.ARRAY, items=S(type=T.OBJECT, properties={
                    "task":   S(type=T.STRING),
                    "status": S(type=T.STRING),
                }))
            }, required=["todos"])),
        gtypes.FunctionDeclaration(name="update_project_doc",
            description="Re-scan this project and refresh its .agent2/agent2.md brief "
                        "(purpose, features, architecture, commands, layout). Call AFTER you "
                        "finish work that CHANGED what the project contains — a new feature, "
                        "module, entry point, dependency, command, test suite or a restructure "
                        "— so the doc the next turn reads is still true. Anything a human wrote "
                        "in it is preserved. Set describe=true ONLY when you changed what the "
                        "project is FOR (a new purpose or a headline feature). Do NOT call it "
                        "after read-only work, a one-line fix, or a question. No arguments "
                        "required.",
            parameters=S(type=T.OBJECT, properties={
                "describe": S(type=T.BOOLEAN),
                "hint":     S(type=T.STRING),
            })),
        # ── File Intelligence System ────────────────────────────────────────────
        # ⚠️ These five are in `_SHARED_TOOLS`, so `dispatch_tool` has always been
        # able to route them — they were simply never DECLARED here, and a tool the
        # model is not told about is a tool that is never called. The CLI is the
        # default surface, so the whole subsystem was unreachable from it while the
        # browser used it freely, with no error on either side. Descriptions are
        # each surface's own prose (five of the thirteen above already differ from
        # `tools.py`); the NAMES are the invariant, pinned in both directions by
        # `test_cli.py::test_advertised_and_dispatchable_tools_agree_both_ways`.
        gtypes.FunctionDeclaration(name="detect_file",
            description="Auto-detect a file's type, rich metadata (size, dates, checksum, page count, dimensions, duration), and the list of operations available for it. Call this FIRST for any file the user references before deciding what to do.",
            parameters=S(type=T.OBJECT, properties={
                "path": S(type=T.STRING),
            }, required=["path"])),
        gtypes.FunctionDeclaration(name="file_capabilities",
            description="List the operations available for a file path OR a whole category (documents, spreadsheets, presentations, images, audio, video, archives, code). Use to discover what you can do before calling run_file_op.",
            parameters=S(type=T.OBJECT, properties={
                "path":     S(type=T.STRING),
                "category": S(type=T.STRING),
            })),
        gtypes.FunctionDeclaration(name="run_file_op",
            description=(
                "Universal file operation. Auto-detects the file type and routes to the "
                "right backend. Operations by category: documents (read, extract_text, "
                "summarize, translate, rewrite, grammar, compare, merge, split, "
                "extract_images, ocr, convert); spreadsheets (read, analyze, formula_audit, "
                "clean, export_csv, convert); presentations (read, speaker_notes, translate, "
                "convert); images (metadata, convert, compress, resize, ocr); archives "
                "(list, extract, inspect, create); audio/video (metadata, convert, "
                "extract_audio, transcribe); code (read, analyze). For AI ops "
                "(summarize/translate/rewrite/grammar) the tool returns the extracted text "
                "with an instruction — YOU then produce the transformed text and, if asked, "
                "save it with write_file. Pass batch inputs (e.g. multiple PDFs to merge) in "
                "options.paths. Common options: to_format, output_path, other (for compare), "
                "quality/width/height (images), target_language, paths."
            ),
            parameters=S(type=T.OBJECT, properties={
                "path":      S(type=T.STRING),
                "operation": S(type=T.STRING),
                "options":   S(type=T.OBJECT),
            }, required=["path", "operation"])),
        gtypes.FunctionDeclaration(name="convert_file",
            description="Convert a file to another format. The best backend is chosen automatically (e.g. DOCX/PPTX/XLSX/MD/HTML/image → PDF, image ↔ image, CSV ↔ XLSX, media transcode). Falls back gracefully if a backend is missing.",
            parameters=S(type=T.OBJECT, properties={
                "path":        S(type=T.STRING),
                "to_format":   S(type=T.STRING),
                "output_path": S(type=T.STRING),
            }, required=["path", "to_format"])),
        gtypes.FunctionDeclaration(name="search_workspace",
            description=(
                "Search across many files at once. kind='content' (regex/keyword over code, "
                "text AND document text like PDF/DOCX), 'filename', 'recent' (query = number "
                "of days), 'secrets' (find API keys/credentials), 'duplicates' (find identical "
                "files). Natural-language presets work too: 'invoices', 'TODOs', 'API keys', "
                "'duplicates'."
            ),
            parameters=S(type=T.OBJECT, properties={
                "query": S(type=T.STRING),
                "path":  S(type=T.STRING),
                "kind":  S(type=T.STRING),
            }, required=["query"])),
    ])
