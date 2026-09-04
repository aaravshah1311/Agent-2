# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

import json
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path
from google.genai import types
from agent2.config import OS_NAME, SHELL_LABEL
from agent2.core import workspace as _ws
from agent2.core import logging as alog
from agent2.core import permissions as _perms
from agent2.core.workspace import WorkspaceViolation, BLOCKED_MSG

MAX_FILE = 100_000   # max chars returned by read_file

def status_line(msg, typ=None):
    pass


# ── Sandbox helper (sections 2, 13) ──────────────────────────────────────────────
# Every filesystem tool resolves its path through the central WorkspaceManager
# BEFORE touching disk. On escape we return the EXACT required error string so
# the model (and the UI) always see the same message. `require_exists=False` is
# used by write/mkdir paths that legitimately create new files.

def _safe_path(raw: str, tool: str, *, require_exists: bool = False):
    """Return (resolved_Path, None) or (None, error_dict). Never raises."""
    try:
        p = _ws.validate_path(raw, tool=tool)
    except WorkspaceViolation:
        return None, {"error": BLOCKED_MSG, "code": "outside_workspace"}
    except Exception as ex:
        alog.tool_failure(tool, str(ex), path=raw)
        return None, {"error": str(ex)}
    if require_exists and not p.exists():
        return None, {"error": f"Not found: {p}"}
    return p, None


def add_mem(content: str, importance: int = 5, tags=None) -> None:
    """Persist a memory through the centralized backend.

    ⚠️ This MUST go through `core.memory.add_memory`, not raw SQL. It used to
    INSERT directly, which skipped three things that live in that function:
    `sync.notify("memories")` (so a memory the agent saved was absent from its
    own system prompt for the rest of the process — the cached block was never
    invalidated), the dedup, and `importance`/`tags`, which this signature and the
    tool schema both advertise and the INSERT silently dropped.
    """
    try:
        from agent2.core import memory as _memory
        _memory.add_memory(content, importance, tags)
    except Exception:
        pass


def print_plan(title, steps) -> None:
    """No-op in the web context; the plan is surfaced via the tool result."""

def _impl_read(args: dict) -> dict:
    p, err = _safe_path(args.get("path", ""), "read_file", require_exists=True)
    if err:
        return err
    s = args.get("start_line"); e = args.get("end_line")
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        total = len(lines)
        sl, el = (s - 1 if s else 0), (e if e else total)
        content = "".join(lines[sl:el])
        if len(content) > MAX_FILE:
            content = content[:MAX_FILE] + "\n…[truncated]"
        alog.tool_exec("read_file", ok=True, path=str(p))
        return {"content": content, "total_lines": total, "path": str(p)}
    except Exception as ex: return {"error": str(ex)}

def _impl_write(args: dict) -> dict:
    content = args.get("content", "")
    if not content: return {"error": "content is required"}
    p, err = _safe_path(args.get("path", ""), "write_file")
    if err:
        return err
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        lines = content.count("\n") + 1
        alog.tool_exec("write_file", ok=True, path=str(p), lines=lines)
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
        results.extend(
            {"title": t["Text"][:80], "snippet": t["Text"][:300]}
            for t in data.get("RelatedTopics", [])[:4]
            if isinstance(t, dict) and t.get("Text")
        )
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
    try:    steps = json.loads(args.get("steps", "[]"))
    except Exception: steps = [args.get("steps", "")]
    print_plan(title, steps)
    return {"plan_emitted": True}


def _impl_scan_project(args: dict) -> dict:
    p, err = _safe_path(args.get("path", "."), "scan_project", require_exists=True)
    if err:
        return err
    if not p.is_dir(): return {"error": f"Invalid directory: {p}"}

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
        p, err = _safe_path(edit.get("path", ""), "multi_edit_files", require_exists=True)
        if err:
            results.append(f"{edit.get('path','')}: {err['error']}")
            continue
        old_text = edit.get("old_text", "")
        new_text = edit.get("new_text", "")
        try:
            c = p.read_text(encoding="utf-8")
            if old_text not in c:
                results.append(f"{p}: old_text not found")
            else:
                p.write_text(c.replace(old_text, new_text), encoding="utf-8")
                results.append(f"{p}: Successfully edited")
                alog.tool_exec("multi_edit_files", ok=True, path=str(p))
        except Exception as e:
            results.append(f"{p}: Error {e}")
    return {"results": "\n".join(results)}

def _impl_list_dir(args: dict) -> dict:
    p, err = _safe_path(args.get("path", "."), "list_dir", require_exists=True)
    if err:
        return err
    if p.is_file():
        return {"path": str(p), "is_file": True, "size": p.stat().st_size}
    skip = _ws.SKIP_DIRS
    entries = []
    try:
        for item in sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
            if item.name in skip:
                continue
            if item.is_dir():
                entries.append(f"{item.name}/")
            else:
                entries.append(f"{item.name}  ({item.stat().st_size} B)")
        return {"path": str(p), "count": len(entries), "entries": entries}
    except Exception as ex:
        return {"error": str(ex)}


def _impl_delete_file(args: dict) -> dict:
    import shutil as _sh
    p, err = _safe_path(args.get("path", ""), "delete_file", require_exists=True)
    if err:
        return err
    try:
        if p.is_dir():
            _sh.rmtree(p)
            alog.tool_exec("delete_file", ok=True, path=str(p), type="dir")
            return {"success": True, "deleted": str(p), "type": "dir"}
        p.unlink()
        alog.tool_exec("delete_file", ok=True, path=str(p), type="file")
        return {"success": True, "deleted": str(p), "type": "file"}
    except Exception as ex:
        return {"error": str(ex)}


def _impl_grep(args: dict) -> dict:
    import re as _re
    pattern = args.get("pattern", "")
    if not pattern:
        return {"error": "pattern required"}
    root, err = _safe_path(args.get("path", "."), "grep_search", require_exists=True)
    if err:
        return err
    glob = args.get("glob", "")
    try:
        rx = _re.compile(pattern)
    except Exception as ex:
        return {"error": f"bad regex: {ex}"}
    skip = _ws.SKIP_DIRS
    hits, scanned = [], 0
    targets = [root] if root.is_file() else root.rglob(glob or "*")
    for fp in targets:
        if not fp.is_file():
            continue
        if any(part in skip for part in fp.parts):
            continue
        try:
            scanned += 1
            for i, line in enumerate(fp.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{fp}:{i}: {line.strip()[:200]}")
                    if len(hits) >= 200:
                        return {"pattern": pattern, "matches": hits, "truncated": True, "files_scanned": scanned}
        except Exception:
            pass
    return {"pattern": pattern, "matches": hits, "match_count": len(hits), "files_scanned": scanned}


# Fallback TODO store used only when a call has no session context (e.g. a raw
# unit-test call). Real runs pass a per-chat list via the ctx, so two concurrent
# chats never share a checklist (section 6).
_TODO_FALLBACK: list[dict] = []

def _impl_update_todo(args: dict, ctx: "ToolContext | None" = None) -> dict:
    """Maintain a live task list so the agent can plan and track a big build.

    ⚠️ THE DURABLE STORE IS `core/tasks.py`, NOT `ctx.todos`.

    This used to be `store.clear()` + re-append, which had two consequences the
    user saw: a task the model had already finished went back to `pending` the
    moment the model re-sent a stale status, and nothing survived Ctrl+C. Both
    are fixed by merging into `agent_tasks` (`tasks.sync_list`), which refuses
    to regress a terminal task and outlives the process.

    `ctx.todos` is still written, as a mirror, for two reasons: it is the shape
    both agent loops already read, and it is what the tool falls back to when
    there is no DB at all (a raw unit-test call with `ctx=None`). The mirror is
    rebuilt FROM the merge result, so what the model is told matches what was
    actually stored — reporting the request back would re-introduce the
    regression this fixes.
    """
    from agent2.core import tasks as _tasks

    store = ctx.todos if ctx is not None else _TODO_FALLBACK
    todos = args.get("todos")
    if not isinstance(todos, list):
        todos = None

    merged = None
    if ctx is not None and todos is not None:
        try:
            merged = _tasks.sync_list(ctx.task_session(), todos)
        except Exception as e:
            # FAILSAFE: an unreachable DB degrades to the old in-memory list —
            # the checklist stops surviving a restart, but the turn continues.
            alog.tool_failure("update_todo", str(e), stage="persist")
            merged = None

    if merged is not None:
        store.clear()
        store.extend({"task": t.title, "status": t.status, "id": t.id}
                     for t in merged)
        summary = _tasks.summary(ctx.task_session(), merged)
        return {
            "todos": list(store),
            "total": summary["total"],
            "completed": summary["completed"],
            "progress": summary["progress"],
            "session_id": ctx.task_session(),
            "persisted": True,
        }

    if todos is not None:
        store.clear()
        for t in todos:
            if isinstance(t, dict):
                store.append({
                    "task":   str(t.get("task", ""))[:200],
                    "status": t.get("status", "pending"),
                })
            else:
                store.append({"task": str(t)[:200], "status": "pending"})
    done = sum(1 for t in store if t["status"] == "completed")
    return {
        "todos": list(store),
        "total": len(store),
        "completed": done,
        "progress": f"{done}/{len(store)}",
        "persisted": False,
    }


# ── File Intelligence tool shims ─────────────────────────────────────────────────
# Thin wrappers over agent2.fileintel. They keep dispatch_tool's clean
# (name, args) -> dict signature, so both the Gemini loop (agent.py) and the
# custom-provider loop (provider_agent.py) get these tools with no extra wiring.

def _impl_detect_file(args: dict) -> dict:
    from agent2 import fileintel
    if not args.get("path"):
        return {"error": "path is required"}
    p, err = _safe_path(args["path"], "detect_file", require_exists=True)
    if err:
        return err
    return fileintel.detect_file(str(p))


def _impl_file_capabilities(args: dict) -> dict:
    from agent2 import fileintel
    # A bare category (e.g. "images") is not a path — pass it straight through.
    if args.get("path"):
        p, err = _safe_path(args["path"], "file_capabilities")
        if err:
            return err
        return fileintel.file_capabilities(str(p))
    category = args.get("category") or ""
    if not category:
        return {"error": "path or category is required"}
    return fileintel.file_capabilities(str(category))


def _impl_run_file_op(args: dict) -> dict:
    from agent2 import fileintel
    p = args.get("path")
    op = args.get("operation")
    if not p or not op:
        return {"error": "path and operation are required"}
    sp, err = _safe_path(p, "run_file_op", require_exists=True)
    if err:
        return err
    options = args.get("options") or {}
    if isinstance(options, str):
        try:
            options = json.loads(options)
        except Exception:
            options = {}
    return fileintel.run_op(str(sp), str(op), options)


def _impl_convert_file(args: dict) -> dict:
    from agent2 import fileintel
    p = args.get("path")
    to = args.get("to_format")
    if not p or not to:
        return {"error": "path and to_format are required"}
    sp, err = _safe_path(p, "convert_file", require_exists=True)
    if err:
        return err
    out = args.get("output_path")
    if out:
        # An explicit output path must also stay inside the workspace.
        op, oerr = _safe_path(out, "convert_file")
        if oerr:
            return oerr
        out = str(op)
    return fileintel.convert_file(str(sp), str(to), output_path=out)


def _impl_search_workspace(args: dict) -> dict:
    from agent2 import fileintel
    q = args.get("query", "")
    # Confine the search to the active workspace (section 5: never scan the
    # whole disk). If no path is given, search the workspace root.
    root = str(_ws.root())
    raw_path = args.get("path") or root
    sp, err = _safe_path(raw_path, "search_workspace", require_exists=False)
    if err:
        return err
    return fileintel.search_workspace(
        q, path=str(sp), kind=args.get("kind", "content"),
        workspace_root=root)


# ── Tool Registry (section 4) ─────────────────────────────────────────────────
# Every local tool is registered ONCE here at import time, validated, and made
# discoverable. dispatch_tool goes through the registry; an unknown tool returns
# the EXACT string `Tool "<name>" is not registered.` instead of crashing.

class ToolContext:
    """Per-call context threaded into tools that need session state.

    Currently carries the per-chat TODO list (so concurrent chats don't share a
    checklist) and identifiers for logging. Optional everywhere — a None ctx
    means "no live session" (unit tests, background utilities).

    `todos` is now a MIRROR, not the store: `core/tasks.py` owns the durable
    list. It is kept because it is the in-process shape both agent loops already
    read, and because it is the fallback when the DB is unreachable.
    """

    def __init__(self, session=None, sid: str | None = None,
                 chat_id: str | None = None, task_session_id: str | None = None):
        self.session = session
        self.sid = sid
        self.chat_id = chat_id
        self.todos = session.todos if session is not None else []
        self._task_session_id = task_session_id

    def task_session(self) -> str:
        """The durable task-session id for this chat, created on first use.

        Lazy on purpose: a turn that never plans anything should not leave an
        empty session row behind, and resolving it eagerly in `__init__` would
        put a DB write on the hot path of every single tool call.
        """
        if not self._task_session_id:
            from agent2.core import tasks as _tasks
            from agent2.core import workspace as _wsmod
            try:
                root = str(_wsmod.root())
            except Exception:
                root = ""
            self._task_session_id = _tasks.session_for_chat(
                self.chat_id or "", cwd=root,
                workspace_id=getattr(self.session, "workspace_id", "") or "",
                surface="agent")
        return self._task_session_id

    def existing_task_session(self) -> str:
        """The task session id ONLY if one already exists. Never creates.

        ⚠️ Checkpoint recording runs on EVERY tool call, so it must not be the
        thing that brings a session into being — `task_session()` would leave an
        empty session row behind for every turn that never planned anything, and
        those rows are exactly what `unfinished_sessions()` offers as recovery
        candidates. A chat with no plan has nothing to checkpoint.
        """
        if self._task_session_id:
            return self._task_session_id
        if not self.chat_id:
            return ""
        from agent2.core import tasks as _tasks
        row = _tasks.active_session_for_chat(self.chat_id)
        if row:
            self._task_session_id = str(row["id"])
        return self._task_session_id or ""

    def current_task_id(self) -> str:
        """The id of the task this turn is executing, or "".

        Task 4 links a command execution to the task that spawned it. Read
        through `existing_task_session` for the same reason `note_tool` is:
        asking "which task is running?" must never be the thing that CREATES a
        task session, or every shell command in a chat with no plan would leave
        an empty session row behind for recovery to offer.

        Best-effort — a command with no task_id is still a fully tracked
        execution, just an unattributed one.
        """
        try:
            sess = self.existing_task_session()
            if not sess:
                return ""
            from agent2.core import tasks as _tasks
            task = _tasks.current(sess)
            return task.id if task is not None else ""
        except Exception:
            return ""

    def note_tool(self, tool: str, *, ok: bool | None = None,
                  detail: str = "") -> None:
        """Record *tool* as a checkpoint sub-step of the running task.

        Best-effort and silent: a checkpoint is a recovery aid, and a turn that
        died because it could not write its own breadcrumb would be strictly
        worse off than one with no breadcrumbs.
        """
        try:
            sess = self.existing_task_session()
            if sess:
                from agent2.core import tasks as _tasks
                _tasks.note_tool(sess, tool, ok=ok, detail=detail)
        except Exception:
            pass


class ToolRegistry:
    """Name → implementation map with validation + discovery (section 4)."""

    def __init__(self) -> None:
        self._tools: dict[str, dict] = {}

    def register(self, name: str, impl, *, wants_ctx: bool = False) -> None:
        if not name or not callable(impl):
            alog.registry_reject("invalid", name or "<empty>")
            raise ValueError(f"Cannot register tool: {name!r}")
        if name in self._tools:
            alog.registry_reject("duplicate", name)
            raise ValueError(f"Duplicate tool registration: {name}")
        self._tools[name] = {"impl": impl, "wants_ctx": wants_ctx}

    def has(self, name: str) -> bool:
        return name in self._tools

    def get(self, name: str):
        entry = self._tools.get(name)
        return entry["impl"] if entry else None

    def list(self) -> list[str]:
        return sorted(self._tools)

    def call(self, name: str, args: dict, ctx: "ToolContext | None" = None) -> dict:
        entry = self._tools.get(name)
        if entry is None:
            # EXACT required message (section 4).
            return {"error": f'Tool "{name}" is not registered.',
                    "code": "unregistered_tool"}
        try:
            if entry["wants_ctx"]:
                return entry["impl"](args, ctx)
            return entry["impl"](args)
        except WorkspaceViolation:
            return {"error": BLOCKED_MSG, "code": "outside_workspace"}
        except Exception as ex:
            alog.exception("tool crashed", tool=name)
            return {"error": f"{type(ex).__name__}: {ex}"}


REGISTRY = ToolRegistry()
REGISTRY.register("read_file",        _impl_read)
REGISTRY.register("write_file",       _impl_write)
REGISTRY.register("web_search",       _impl_search)
REGISTRY.register("save_memory",      _impl_save_mem)
REGISTRY.register("emit_plan",        _impl_plan)
REGISTRY.register("scan_project",     _impl_scan_project)
REGISTRY.register("multi_edit_files", _impl_multi_edit)
REGISTRY.register("list_dir",         _impl_list_dir)
REGISTRY.register("delete_file",      _impl_delete_file)
REGISTRY.register("grep_search",      _impl_grep)
REGISTRY.register("update_todo",      _impl_update_todo, wants_ctx=True)
REGISTRY.register("detect_file",       _impl_detect_file)
REGISTRY.register("file_capabilities", _impl_file_capabilities)
REGISTRY.register("run_file_op",       _impl_run_file_op)
REGISTRY.register("convert_file",      _impl_convert_file)
REGISTRY.register("search_workspace",  _impl_search_workspace)
alog.registry_loaded(len(REGISTRY.list()), ", ".join(REGISTRY.list()))


def dispatch_tool(name: str, args: dict, ctx: "ToolContext | None" = None) -> dict:
    """Dispatch a local tool through the registry (section 4).

    ⚠️ This is also THE checkpoint chokepoint. Recording the sub-step here rather
    than at each loop's call site is what keeps the Gemini loop, the custom
    provider loop and the CLI from drifting into checkpointing different things —
    the same reason `_exec_tool` threads `prog` through one place.

    `update_todo` is excluded: it is the tool that *defines* the checklist, so
    logging it as a step inside the checklist is noise, and it would show up as
    the "current sub-step" every time the model re-planned.

    ⚠️ A DESTRUCTIVE TOOL IS RECORDED TWICE — BEFORE AND AFTER
    ─────────────────────────────────────────────────────────
    Recording only on return is enough for a progress trail but useless for
    recovery: a process killed *during* `write_file`/`delete_file` never reaches
    the second call, so the step would not exist at all and `checkpoint_view()`
    would report `destructive_pending: False` — recovery would then declare a
    half-finished deletion "nothing in flight" and let the model repeat it, which
    is precisely the blind retry rule 21 forbids. The pre-call note is what makes
    the step readable as RUNNING in the next process.

    Only destructive tools pay for it. A read-only tool cannot leave the
    workspace in an unknown state, so paying a second checkpoint write per
    `read_file` would buy nothing.

    ⚠️ IT IS ALSO THE CAPABILITY CHOKEPOINT (Task 15)
    ────────────────────────────────────────────────
    `write_file` and `delete_file` are two of the sensitive operations Task 15
    names, and neither is a route — they are reachable through *any* surface that
    can start a turn. Checking here rather than at the API boundary is what makes
    `AGENT2_DENY_CAPS=fs.delete` mean what it says: a policy enforced only on the
    HTTP edge would be bypassed by the very next thing the model decided to do on
    a turn that edge had already admitted.

    The refusal is returned as a normal tool `error`, not raised. The model reads
    tool errors and adapts; an exception would abort the turn and be reported to
    the user as a crash, which is a worse description of "you asked for something
    this deployment does not allow". The check is a no-op unless an operator set
    `AGENT2_DENY_CAPS`, so the default install is untouched.
    """
    cap = _perms.capability_for_tool(name)
    if not _perms.process_allows(cap):
        _perms.audit_use(cap, ok=False, what=f"tool:{name}",
                         detail=_step_detail(name, args))
        return {"error": _perms.refusal(cap, what=f"the {name} tool")}
    if cap in _perms.SENSITIVE:
        _perms.audit_use(cap, ok=True, what=f"tool:{name}",
                         detail=_step_detail(name, args))
    if ctx is not None and name in _destructive_tools():
        ctx.note_tool(name, detail=_step_detail(name, args))
    result = REGISTRY.call(name, args, ctx)
    if ctx is not None and name != "update_todo":
        ctx.note_tool(name, ok="error" not in result,
                      detail=_step_detail(name, args))
    return result


def _destructive_tools() -> frozenset:
    """`tasks.DESTRUCTIVE_TOOLS`, resolved late and never raising.

    Imported inside the call for the same reason `ToolContext.note_tool` does it:
    `core.tasks` is not a dependency of the tool layer, and a dispatch must not
    start failing because a checkpoint import did.
    """
    try:
        from agent2.core import tasks as _tasks
        return _tasks.DESTRUCTIVE_TOOLS
    except Exception:
        return frozenset()


def _step_detail(name: str, args: dict) -> str:
    """One human-readable field per tool, for the resume summary.

    Deliberately the target and not the payload: a resume prompt says "was
    writing config.py", never the file's contents.
    """
    if not isinstance(args, dict):
        return ""
    for key in ("path", "file", "command", "query", "directory", "pattern"):
        val = args.get(key)
        if val:
            return str(val)[:120]
    return ""

# ── Gemini tool declarations ────────────────────────────────────────────────────
def _build_tools() -> types.Tool:
    S = types.Schema; T = types.Type
    return types.Tool(function_declarations=[
        types.FunctionDeclaration(name="run_command",
            description=f"Execute a shell command on {OS_NAME} ({SHELL_LABEL}). Use for running scripts, installs, scans, builds.",
            parameters=S(type=T.OBJECT, properties={
                "command":     S(type=T.STRING),
                "description": S(type=T.STRING),
                "cwd":         S(type=T.STRING),
            }, required=["command","description"])),
        types.FunctionDeclaration(name="read_file",
            description="Read a file's contents. Always read before editing.",
            parameters=S(type=T.OBJECT, properties={
                "path":       S(type=T.STRING),
                "start_line": S(type=T.INTEGER),
                "end_line":   S(type=T.INTEGER),
            }, required=["path"])),
        types.FunctionDeclaration(name="write_file",
            description="Create or overwrite a file with the given content. Use for creating new files. Parent directories are created automatically.",
            parameters=S(type=T.OBJECT, properties={
                "path":    S(type=T.STRING),
                "content": S(type=T.STRING),
            }, required=["path","content"])),
        types.FunctionDeclaration(name="web_search",
            description="Search the web for CVEs, docs, error messages, latest info.",
            parameters=S(type=T.OBJECT, properties={
                "query":       S(type=T.STRING),
                "max_results": S(type=T.INTEGER),
            }, required=["query"])),
        types.FunctionDeclaration(name="save_memory",
            description="Save an important fact to long-term memory (persists across sessions).",
            parameters=S(type=T.OBJECT, properties={
                "content":    S(type=T.STRING),
                "importance": S(type=T.INTEGER),
                "tags":       S(type=T.STRING),
            }, required=["content"])),
        types.FunctionDeclaration(name="emit_plan",
            description="Show a step-by-step plan before a complex multi-step task.",
            parameters=S(type=T.OBJECT, properties={
                "title": S(type=T.STRING),
                "steps": S(type=T.STRING),
            }, required=["title","steps"])),
        types.FunctionDeclaration(name="scan_project",
            description="Recursively scan a project directory and return a file tree + content of ALL code/config files. Use this AUTOMATICALLY whenever the user mentions a project, asks to check code, add features, or fix bugs. Pass the project path.",
            parameters=S(type=T.OBJECT, properties={
                "path": S(type=T.STRING),
            }, required=["path"])),
        types.FunctionDeclaration(name="multi_edit_files",
            description="Edit multiple files at once by replacing exact text snippets. Each edit has path, old_text (exact match), new_text (replacement). Use for renaming, refactoring, or patching across files.",
            parameters=S(type=T.OBJECT, properties={
                "edits": S(type=T.ARRAY, items=S(type=T.OBJECT, properties={
                    "path": S(type=T.STRING),
                    "old_text": S(type=T.STRING),
                    "new_text": S(type=T.STRING)
                }))
            }, required=["edits"])),
        types.FunctionDeclaration(name="list_dir",
            description="List the contents of a directory (files + subfolders). Use to explore a project's structure before reading or editing.",
            parameters=S(type=T.OBJECT, properties={
                "path": S(type=T.STRING),
            }, required=["path"])),
        types.FunctionDeclaration(name="delete_file",
            description="Delete a file or directory (recursively). Use when refactoring or removing generated artifacts.",
            parameters=S(type=T.OBJECT, properties={
                "path": S(type=T.STRING),
            }, required=["path"])),
        types.FunctionDeclaration(name="grep_search",
            description="Search file contents by regex across a directory tree. Returns file:line: matches. Use to locate symbols, functions, or usages in a codebase.",
            parameters=S(type=T.OBJECT, properties={
                "pattern": S(type=T.STRING),
                "path":    S(type=T.STRING),
                "glob":    S(type=T.STRING),
            }, required=["pattern"])),
        types.FunctionDeclaration(name="update_todo",
            description="Create/update a live TODO checklist for a multi-step build. Pass the full list each time with each item's status (pending|in_progress|completed). Call this FIRST for any big task, then update statuses as you finish steps so the user sees progress.",
            parameters=S(type=T.OBJECT, properties={
                "todos": S(type=T.ARRAY, items=S(type=T.OBJECT, properties={
                    "task":   S(type=T.STRING),
                    "status": S(type=T.STRING),
                }))
            }, required=["todos"])),
        types.FunctionDeclaration(name="detect_file",
            description="Auto-detect a file's type, rich metadata (size, dates, checksum, page count, dimensions, duration), and the list of operations available for it. Call this FIRST for any file the user references before deciding what to do.",
            parameters=S(type=T.OBJECT, properties={
                "path": S(type=T.STRING),
            }, required=["path"])),
        types.FunctionDeclaration(name="file_capabilities",
            description="List the operations available for a file path OR a whole category (documents, spreadsheets, presentations, images, audio, video, archives, code). Use to discover what you can do before calling run_file_op.",
            parameters=S(type=T.OBJECT, properties={
                "path":     S(type=T.STRING),
                "category": S(type=T.STRING),
            })),
        types.FunctionDeclaration(name="run_file_op",
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
        types.FunctionDeclaration(name="convert_file",
            description="Convert a file to another format. The best backend is chosen automatically (e.g. DOCX/PPTX/XLSX/MD/HTML/image → PDF, image ↔ image, CSV ↔ XLSX, media transcode). Falls back gracefully if a backend is missing.",
            parameters=S(type=T.OBJECT, properties={
                "path":        S(type=T.STRING),
                "to_format":   S(type=T.STRING),
                "output_path": S(type=T.STRING),
            }, required=["path", "to_format"])),
        types.FunctionDeclaration(name="search_workspace",
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

