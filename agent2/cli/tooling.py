# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/cli/tooling.py
─────────────────────
What the CLI tells the model it can do, and what actually happens when it asks.

⚠️ ONE BACKEND, AND THE SPLIT BETWEEN THEM IS DELIBERATE.
Every filesystem- or sandbox-sensitive tool is routed to `agent2.tools`, the
shared workspace-confined registry the Web UI uses — `_SHARED_TOOLS` is that
list. Only genuinely presentation-layer tools stay local: `web_search`,
`save_memory` and `emit_plan` print into *this* terminal, so delegating them
would change their behaviour. Re-implementing a shared tool here would give the
CLI a second sandbox with its own bugs.

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
from pathlib import Path

from agent2.cli.env import OS_NAME, _burp, _BURP_OK, _mcp_registry, _MCP_OK, gtypes
from agent2.cli.models import SHELL_LABEL
from agent2.cli.render import print_plan
from agent2.cli.store import add_mem

# ── Tool implementations (same logic as web app) ───────────────────────────────
MAX_FILE = 64_000
_SKIP    = {"__pycache__", ".git", "node_modules", ".venv", "venv", "env",
            "dist", "build", ".next", "target", ".DS_Store"}

def _impl_read(args: dict) -> dict:
    p = Path(args["path"]).expanduser()
    s = args.get("start_line"); e = args.get("end_line")
    try:
        if not p.exists(): return {"error": f"Not found: {p}"}
        with open(p, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        total = len(lines)
        sl, el = (s - 1 if s else 0), (e if e else total)
        content = "".join(lines[sl:el])
        if len(content) > MAX_FILE:
            content = content[:MAX_FILE] + "\n…[truncated]"
        return {"content": content, "total_lines": total, "path": str(p)}
    except Exception as ex: return {"error": str(ex)}

def _impl_write(args: dict) -> dict:
    p = Path(args.get("path", "")).expanduser()
    content = args.get("content", "")
    if not content: return {"error": "content is required"}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        lines = content.count("\n") + 1
        return {"success": True, "path": str(p), "lines": lines}
    except Exception as ex: return {"error": str(ex)}



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


def _impl_scan_project(args: dict) -> dict:
    raw = args.get("path", ".")
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = Path(os.getcwd()) / p
    p = p.resolve()
    if not p.exists() or not p.is_dir(): return {"error": f"Invalid directory: {p}"}

    important_exts = {".py", ".js", ".html", ".css", ".json", ".md", ".txt", ".ts", ".tsx",
                      ".jsx", ".java", ".c", ".cpp", ".h", ".hpp", ".go", ".rs", ".rb",
                      ".php", ".yaml", ".yml", ".toml", ".cfg", ".ini", ".env", ".sql",
                      ".sh", ".bat", ".ps1", ".xml", ".svg", ".lock"}
    skip_dirs = {".git", "node_modules", "venv", ".venv", "__pycache__", "dist", "build",
                 ".next", "target", ".DS_Store", ".idea", ".vscode", "coverage", ".cache"}

    # Build a file tree first
    tree_lines = [f"Project root: {p}"]
    contents = []
    total_size = 0

    import os as _os
    for root, dirs, files in _os.walk(p):
        dirs[:] = sorted([d for d in dirs if d not in skip_dirs])
        level = len(Path(root).relative_to(p).parts)
        indent = "  " * level
        tree_lines.append(f"{indent}{Path(root).name}/")
        for fname in sorted(files):
            fp = Path(root) / fname
            if fp.suffix in important_exts:
                tree_lines.append(f"{indent}  {fname}  ({fp.stat().st_size} bytes)")
                try:
                    text = fp.read_text(encoding="utf-8", errors="replace")
                    if len(text) > 50000: text = text[:50000] + "\n...[truncated]"
                    contents.append(f"\n{'='*60}\n FILE: {fp.relative_to(p)}\n{'='*60}\n{text}")
                    total_size += len(text)
                    if total_size > 300000:
                        contents.append("\n--- [TRUNCATED: Project too large, remaining files skipped] ---")
                        break
                except Exception:
                    pass
        if total_size > 300000:
            break

    file_tree = "\n".join(tree_lines)
    file_contents = "\n".join(contents) if contents else "No important text files found."
    return {"file_tree": file_tree, "file_count": len(contents), "project_contents": file_contents}

def _impl_multi_edit(args: dict) -> dict:
    edits = args.get("edits", [])
    results = []
    for edit in edits:
        p = Path(edit.get("path", "")).expanduser()
        old_text = edit.get("old_text", "")
        new_text = edit.get("new_text", "")
        if not p.exists():
            results.append(f"{p}: File not found")
            continue
        try:
            c = p.read_text(encoding="utf-8")
            if old_text not in c:
                results.append(f"{p}: old_text not found")
            else:
                p.write_text(c.replace(old_text, new_text), encoding="utf-8")
                results.append(f"{p}: Successfully edited")
        except Exception as e:
            results.append(f"{p}: Error {e}")
    return {"results": "\n".join(results)}

# Section 10: ONE backend. All filesystem/sandbox-sensitive tools and the File
# Intelligence tools go through the shared, workspace-sandboxed registry in
# agent2.tools — the CLI no longer re-implements them. Only genuinely
# CLI-presentation tools (web_search/save_memory/emit_plan) stay local.
# Module-level and frozen: this used to be rebuilt on every single tool call.
_SHARED_TOOLS = frozenset((
    "read_file", "write_file", "scan_project", "multi_edit_files",
    "list_dir", "delete_file", "grep_search", "update_todo",
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
    ])
