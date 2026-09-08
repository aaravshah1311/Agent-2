# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
Durable execution state — the crash-recoverable shadow of a live process
(Task 24).

Agent2 already knew, in memory, exactly what it was doing: `core/commands.py`
holds a command's lifecycle, `core/procio.py` owns its pipes, `core/tasks.py`
persists the plan and its checkpoints, `ToolContext.note_tool` records a
sub-step. Every one of those is correct, and none of them survives `kill -9`.
`core/commands.py`'s own docstring says so: *"the registry is in-memory. Durable
execution state across a restart is Phase 8 (Task 24)."* This module is that
phase. It answers one question, after the fact and from another process:
**what was in flight when this stopped, and did it finish?**

⚠️ **A DURABLE SHADOW, NOT A SECOND SOURCE OF TRUTH.**
While a process lives, `core/commands.py` is the authority for a command and
`core/tasks.py` is the authority for a task — this module only mirrors them. The
rows exist for the *next* process, which has no registry to consult. So nothing
here refuses a write, nothing here reconciles, and the last write wins on
purpose: `commands._mutate` already owns *"whichever verdict lands first wins"*,
every lifecycle edge is always written, and a wrong guess is corrected by the
owning process's very next edge. A ledger that argued with the registry would
give two answers to one question, which is the drift the one-declaration rule
exists to prevent.

⚠️ **`instance` IS WHAT MAKES A CRASH DETECTABLE.**
`INSTANCE` is unique per process. A row that is unsettled *and* was written by
some other instance *and* has not been touched for `EXEC_STALE_SEC` is work
whose owner is gone. **Both halves of that predicate are load-bearing.** Drop
the instance test and dual mode (two processes, one `agent2.db`) reports the
*other* live surface's running command as a crash. Drop the staleness test and
a command that simply has not printed anything yet — a `sleep 300`, a long
`nmap` — reads as interrupted while it is still running. Before this column
there was no staleness rule anywhere in Agent2: `tasks.unfinished_sessions()`
offers a session interrupted a year ago exactly as eagerly as one lost a second
ago, because nothing recorded whether the process holding it was still alive.

⚠️ **NO OUTPUT, NO CONTENT, NO SECRETS EVER REACH A ROW.**
There is deliberately no stdout/stderr column and no `content` column
(`database.py:922-928`). Persisting command output would write tokens,
passwords and `env` dumps straight into the file `core/secrets.py` exists to
protect. `error` holds *our own* short reason string, one line, truncated — and
`detail` holds **digests** of the files a tool touched, never their bytes. That
is what lets Task 26 answer "did that write already happen?" decisively while
storing zero bytes of the user's files. A file too large to hash records
`size:…,mtime:…` instead, degrading to the same heuristic `core/recovery.py`
uses today.

⚠️ **TWO CHOKEPOINTS, NOT FOUR RUNNERS.**
`core/commands.create()` + `commands._mutate()` cover every shell command,
because `terminal.stream_command` and `cli/runtime.run_cmd_stream` both already
create a `commands.py` record; `tools.dispatch_tool` covers all sixteen
registered tools. Wrapping the four call sites individually is precisely the
drift the one-declaration rule forbids — and a runner that ever bypasses both
leaves a *missing hint*, never a wrong answer.

⚠️ **THE LEDGER WRITE IS OFF THE HOT LOCK, AND EDGES OUTRANK THE THROTTLE.**
`commands.heartbeat()` fires once per output LINE and its docstring promises
*"nothing here allocates, logs, notifies or touches the DB"* — so `_record` is
called **outside** `with _lock:` and a status-unchanged write is throttled to
`EXEC_BEAT_SEC`. A status *change* is never throttled, whatever the knob says,
because that is the contract `config.py:344-345` states: *"The lifecycle edges
(create · start · settle) are ALWAYS written."* Throttling those would be
throttling the only facts recovery reads. There is no `sync.notify()` here and
no `exec_*` entry in `sync.RESOURCES`, for the same reason `commands.py` has
none (`:265-268`): readers poll.

⚠️ **`target_paths()` IS NOT A SECOND `diffs.capture_for()`.**
`capture_for` answers *"what should the diff viewer show"* and needs file
**content** for the before-side, which is why its docstring names `run_command`
and the two `fileintel` writers as deliberate blind spots. `target_paths`
answers *"which paths will exist or vanish if this runs"* and needs only
**paths** — so it covers all six of `tasks.DESTRUCTIVE_TOOLS`, including
`convert_file`'s `options.output_path`. Different question, different cost,
different coverage; `core/diffs.py` is not modified.

Everything public here is **total**: a writer that cannot write returns quietly,
a reader that cannot read returns an empty list or a dict with its documented
keys, and nothing raises into a turn. Bookkeeping may never be the reason a
command fails — hence this module's reviewed BLE001/S110 exemption in
`pyproject.toml`. The first swallowed failure per process is logged through
`alog.exec_persist_failed()` so a permanently broken ledger is not silent; the
rest stay quiet, because a failing DB on a `make -j8` would otherwise write one
log line per output line.

`exec_workflows` is written by **nobody** until Phase 12 (Tasks 37-39). Its
writers live here, declared and tested, for the same reason the Context Broker
declares `skills` and `workflow_state` with empty collectors: a thing that does
not exist yet still has a name, a shape and a slot in the report, so the phase
that adds it *calls* rather than *edits*.

No route, no socket event and no CLI command reads this ledger yet — that is
Task 25's surface.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid

# ⚠️ THE MODULE, NOT THE VALUES — `from agent2.config import EXEC_BEAT_SEC`
# would bind a snapshot at import and freeze it, so every test that monkeypatches
# a knob would silently test nothing. Same rule `core/commands.py:77-81` states.
from agent2 import config as _cfg
from agent2.core import commands as _cmds
from agent2.core import logging as alog
from agent2.core import metrics as _metrics
from agent2.core import tasks as _tasks

# ── Identity ──────────────────────────────────────────────────────────────────
# pid alone is not enough: pids are recycled, and a container that restarts often
# reuses pid 1. The random suffix is what guarantees two processes never claim the
# same instance, which is the whole basis of crash detection.
INSTANCE = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"

# ── Vocabulary ────────────────────────────────────────────────────────────────
# Statuses are REUSED, not re-declared: commands speak `CommandStatus`, tool calls
# and workflows speak `tasks.STEP_*`. One new word is added here and only here.
INTERRUPTED = "interrupted"

# Genuinely finished — set by the process that owned the row.
_TERMINAL: dict[str, frozenset] = {
    "exec_commands": frozenset(_cmds.TERMINAL),
    "exec_tool_calls": frozenset((_tasks.STEP_COMPLETED, _tasks.STEP_FAILED)),
    "exec_workflows": frozenset((
        _tasks.STEP_COMPLETED, _tasks.STEP_FAILED, _cmds.CommandStatus.CANCELLED,
    )),
}

# Finished OR swept. ⚠️ `INTERRUPTED` is settled for the TRIM and unsettled for
# `interrupted()`, which is what makes that reader idempotent across a `sweep()`:
# it finds the row before the sweep by "not terminal", and after the sweep by the
# same test, because a swept row is still not terminal. A single set would make a
# swept crash invisible to the very reader that reported it.
_SETTLED: dict[str, frozenset] = {t: v | {INTERRUPTED} for t, v in _TERMINAL.items()}

# Which scoping columns each table actually has. `exec_commands` has no `chat_id`
# and `exec_workflows` has no `task_id`; asking for one is a hard SQLite error, so
# the generic reader consults this rather than assuming a uniform shape.
_FILTERS: dict[str, tuple[str, ...]] = {
    "exec_commands": ("session_id", "task_id"),
    "exec_tool_calls": ("session_id", "task_id", "chat_id"),
    "exec_workflows": ("session_id", "chat_id"),
}

TABLES = tuple(_TERMINAL)

# `project=ANY_PROJECT` lifts the project filter. The DEFAULT is the current
# project, i.e. the strict direction — the same way an unknown `AGENT2_WEB_ROLE`
# falls to `viewer` and an unrecognised `AGENT2_CONTEXT_ISOLATION` falls to
# `project`. A reader that pooled every checkout by default would quietly break
# rule 30 for every caller that forgot to pass one.
ANY_PROJECT = "*"

# Caps. `detail` is a TEXT column, so these are what keep one pathological call
# from writing a megabyte of JSON per tool invocation.
MAX_DIGEST_BYTES = 2_000_000
MAX_DETAIL_PATHS = 12
MAX_PATH_CHARS = 260
MAX_STATE_CHARS = 4_000
MAX_ERROR_CHARS = 300
# Ceiling on the in-memory throttle/pending maps. They are pruned as rows settle;
# this is the backstop for a surface that creates rows and never finishes them.
MAX_TRACKED = 4_096

# Timestamps come from `core/tasks` — UTC, no timezone marker, second resolution.
# ⚠️ `core/recovery._epoch()` parses these with `calendar.timegm` precisely because
# of that shape. A second `strftime` here that used localtime would shift every
# comparison by the whole UTC offset, silently.
_now = _tasks._now


# ── Internals ─────────────────────────────────────────────────────────────────

_beats: dict[str, tuple[str, float]] = {}
_pending: dict[str, tuple[str, list[str], dict]] = {}
_lock = threading.Lock()
_failure_reported = False


def _fail(op: str, exc: BaseException) -> None:
    """Report the FIRST swallowed ledger failure of this process, then stay quiet.

    A broken ledger must not be silent — but it must not be loud either. Logging
    every failure would put one WARNING per output line on the heartbeat path,
    which is the noise that gets a whole log ignored.
    """
    global _failure_reported
    if _failure_reported:
        return
    _failure_reported = True
    try:
        alog.exec_persist_failed(op, str(exc)[:200])
    except Exception:  # logging is best-effort by contract; see this file's lint allowance
        pass


def _text(val, cap: int) -> str:
    try:
        return str(val or "")[:cap]
    except Exception:
        return ""


def _line(val) -> str:
    """One line, truncated — the only shape `error` is ever written in.

    A tool's error message is our own prose, but a multi-line one could carry a
    traceback or a shell's stderr, and neither belongs in the database (DDL
    invariant 3). First line, hard cap, nothing else.
    """
    try:
        raw = str(val or "").strip()
    except Exception:
        return ""
    if not raw:
        return ""
    return raw.splitlines()[0][:MAX_ERROR_CHARS]


def _project() -> str:
    """The current project directory, canonicalised.

    ⚠️ `context.project_key()`, never `workspace.root()` verbatim: migration 10
    exists because task sessions stored `C:\\Users\\…` while chats stored
    `c:\\users\\…`, so a recovery lookup by project matched nothing while every
    individual read looked perfectly healthy.
    """
    try:
        from agent2.core import context as _ctx
        from agent2.core import workspace as _ws
        return _ctx.project_key(str(_ws.root()))
    except Exception:
        return ""


def _cutoff(stale_after: float | None = None) -> str:
    """The `updated_at` below which an unsettled row counts as abandoned."""
    try:
        secs = float(stale_after) if stale_after is not None else float(_cfg.EXEC_STALE_SEC)
    except Exception:
        secs = 90.0
    if secs < 0:
        secs = 0.0
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - secs))


def _due(row_id: str, status: str, table: str) -> bool:
    """Should this write reach the DB?

    ⚠️ A status CHANGE is always due, whatever `EXEC_BEAT_SEC` says — that is the
    contract at `config.py:344-345`. The knob throttles only repeats, which in
    practice means `heartbeat()`: same status, thousands of times, once per line
    of a `make -j8`.
    """
    now = time.monotonic()
    with _lock:
        prev = _beats.get(row_id)
        if prev is None or prev[0] != status:
            if status in _SETTLED.get(table, frozenset()):
                _beats.pop(row_id, None)
            else:
                _beats[row_id] = (status, now)
                _prune_locked(_beats)
            return True
        try:
            beat = float(_cfg.EXEC_BEAT_SEC or 0)
        except Exception:
            beat = 0.0
        if beat > 0 and (now - prev[1]) >= beat:
            _beats[row_id] = (status, now)
            return True
    return False


def _prune_locked(book: dict) -> None:
    """Backstop for a surface that opens rows and never settles them."""
    if len(book) <= MAX_TRACKED:
        return
    for key in list(book)[: len(book) - MAX_TRACKED]:
        book.pop(key, None)


# ── Digests ───────────────────────────────────────────────────────────────────

def _digest(path: str) -> str | None:
    """A content digest for one path, or `None` if it does not exist.

    ⚠️ `None` means **absent**, and that is a fact Task 26 needs: "the file the
    delete targeted is gone" is the difference between a completed deletion and
    one that never ran. It is deliberately not the same value as "we could not
    read it" — an unreadable path returns the `stat` form, so uncertainty never
    masquerades as absence.
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    except Exception:
        return None
    try:
        if not os.path.isfile(path):
            # A directory (or a device/socket) still EXISTS, which is the half of
            # the answer that matters. Hashing it is neither possible nor useful.
            return f"kind:{'dir' if os.path.isdir(path) else 'other'},mtime:{st.st_mtime:.0f}"
        if st.st_size > MAX_DIGEST_BYTES:
            # Degrades to exactly the heuristic `recovery.verify_step()` uses
            # today, rather than reading a gigabyte on the hot path.
            return f"size:{st.st_size},mtime:{st.st_mtime:.0f}"
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(65536)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return f"size:{st.st_size},mtime:{st.st_mtime:.0f}"


def _digests(paths) -> dict:
    out: dict[str, str | None] = {}
    for raw in list(paths or [])[:MAX_DETAIL_PATHS]:
        key = _text(raw, MAX_PATH_CHARS)
        if key and key not in out:
            out[key] = _digest(key)
    return out


def digests(paths) -> dict:
    """The public reader for `{path: digest}` — Task 25's verifier goes through here.

    It exists so `core/recovery/classify.py` can take the *same* measurement the
    ledger took before the tool ran. A second hasher in the verifier would be a
    second declaration of what "unchanged" means, and the two would disagree in
    exactly one case — the oversized file that degrades to `size:…,mtime:…` — which
    is the case where being wrong means "the user's 4 GB file looks untouched".
    """
    return _digests(paths)


# ── Which paths a tool's arguments name ───────────────────────────────────────

def _resolve(raw) -> str:
    """Best-effort absolute form of one argument path.

    ⚠️ NOT a security check. `workspace.validate_path()` is the gate, inside the
    tool; this is only so two digests taken at different times compare. A path the
    gate later rejects simply yields identical pre/post digests, which reads as
    "not applied" — the safe direction.
    """
    try:
        text = str(raw or "").strip()
        if not text:
            return ""
        if os.path.isabs(text):
            return os.path.abspath(text)
        from agent2.core import workspace as _ws
        return os.path.abspath(os.path.join(str(_ws.root()), text))
    except Exception:
        try:
            return str(raw or "").strip()
        except Exception:
            return ""


def target_paths(tool: str, args) -> list[str]:
    """THE one declaration of which filesystem paths a tool's arguments name.

    Covers all six of `tasks.DESTRUCTIVE_TOOLS`. See the module docstring for why
    this is not a second copy of `core/diffs.capture_for()`.

    ⚠️ `run_command` returns `[]` on purpose — a shell command's targets are not
    knowable from its argv, and guessing them would hand Task 26 a confident
    wrong answer where `exec_commands.status` gives it a right one.
    """
    name = str(tool or "")
    if not isinstance(args, dict) or not name:
        return []
    found: list[str] = []

    def _add(raw) -> None:
        path = _resolve(raw)
        if path and path not in found and len(found) < MAX_DETAIL_PATHS * 2:
            found.append(path[:MAX_PATH_CHARS])

    try:
        if name == "multi_edit_files":
            # ⚠️ EVERY edit's path. `tools._step_detail()` records only the first,
            # which is exactly why `recovery.verify_step()` lands on `uncertain`
            # for a multi-file edit today.
            for edit in args.get("edits") or []:
                if isinstance(edit, dict):
                    _add(edit.get("path"))
        elif name in ("write_file", "delete_file", "read_file", "list_dir"):
            _add(args.get("path"))
        elif name in ("run_file_op", "convert_file"):
            _add(args.get("path"))
            _add(args.get("output_path"))
            opts = args.get("options")
            if isinstance(opts, dict):
                _add(opts.get("output_path"))
                _add(opts.get("other"))
                for extra in opts.get("paths") or []:
                    _add(extra)
    except Exception:
        return found
    return found


# ── The `detail` payload ──────────────────────────────────────────────────────

def _detail_json(tool: str, paths, *, target: str = "",
                 pre: dict | None = None, post: dict | None = None,
                 post_seen: bool = False) -> str:
    """The structured `detail` column — a shape Task 26 parses, never prose.

    ⚠️ `post` is `null` until the call finishes, and that null IS the crash
    signal: a row whose `post` was never written is a tool that never returned.
    An empty dict would be indistinguishable from "finished, touched nothing".
    """
    every = [_text(p, MAX_PATH_CHARS) for p in (paths or [])]
    body = {
        "tool": _text(tool, 64),
        "target": _text(target, 120),
        "paths": every[:MAX_DETAIL_PATHS],
        "paths_total": len(every),
        "pre": dict(pre or {}),
        "post": dict(post) if post_seen else None,
    }
    try:
        return json.dumps(body, separators=(",", ":"))
    except Exception:
        return "{}"


def parse_detail(raw) -> dict:
    """`detail` as a dict, total. A legacy or non-JSON row reads as `{}`.

    Task 26's verifier consults this rather than `json.loads` directly, so a row
    written before this module existed degrades to "cannot verify" instead of
    raising inside a recovery path.
    """
    try:
        out = json.loads(str(raw or "") or "{}")
    except Exception:
        return {}
    return out if isinstance(out, dict) else {}


# ── Writers ───────────────────────────────────────────────────────────────────

_CMD_SQL = (
    "INSERT INTO exec_commands("
    " id, instance, project, session_id, task_id, surface, term_id, command,"
    " process_id, status, exit_code, output_lines, error,"
    " created_at, started_at, last_output_at, completed_at, updated_at)"
    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
    " ON CONFLICT(id) DO UPDATE SET"
    " instance=excluded.instance, project=excluded.project,"
    " session_id=excluded.session_id, task_id=excluded.task_id,"
    " surface=excluded.surface, term_id=excluded.term_id,"
    " command=excluded.command, process_id=excluded.process_id,"
    " status=excluded.status, exit_code=excluded.exit_code,"
    " output_lines=excluded.output_lines, error=excluded.error,"
    " created_at=excluded.created_at, started_at=excluded.started_at,"
    " last_output_at=excluded.last_output_at,"
    " completed_at=excluded.completed_at, updated_at=excluded.updated_at"
)


def record_command(cmd) -> None:
    """Mirror one `CommandExecution` snapshot into `exec_commands`.

    Called from `commands.create()` and `commands._mutate()` — the two
    chokepoints — and from **outside** `commands._lock`. Idempotent by primary
    key: the row is upserted, so a heartbeat and a settle write the same row.

    ⚠️ Only the `*_at` strings are persisted. The `*_mono` clock is
    `time.monotonic()`, which is meaningless in the next process and would make
    `elapsed` read as decades.
    """
    if cmd is None:
        return
    try:
        if not _cfg.EXEC_PERSIST:
            return
        cid = _text(getattr(cmd, "id", ""), 64)
        if not cid:
            return
        status = _text(getattr(cmd, "status", ""), 32)
        if not _due(cid, status, "exec_commands"):
            return
        pid = getattr(cmd, "process_id", None)
        code = getattr(cmd, "exit_code", None)
        from agent2.database import exe
        exe(_CMD_SQL, (
            cid, INSTANCE, _project(),
            _text(getattr(cmd, "session_id", ""), 64),
            _text(getattr(cmd, "task_id", ""), 64),
            _text(getattr(cmd, "surface", ""), 16),
            _text(getattr(cmd, "term_id", ""), 64),
            _text(getattr(cmd, "command", ""), 2000),
            int(pid) if pid else None,
            status,
            int(code) if code is not None else None,
            int(getattr(cmd, "output_lines", 0) or 0),
            _line(getattr(cmd, "error", "")),
            _text(getattr(cmd, "created_at", ""), 32),
            _text(getattr(cmd, "started_at", ""), 32),
            _text(getattr(cmd, "last_output_at", ""), 32),
            _text(getattr(cmd, "completed_at", ""), 32),
            _now(),
        ))
        _trim("exec_commands")
    except Exception as exc:
        _fail("record_command", exc)


def tool_started(tool: str, args, *, session_id: str = "", task_id: str = "",
                 chat_id: str = "", surface: str = "", destructive: bool | None = None,
                 target: str = "") -> str:
    """Open a row for a tool call and return its id (`""` when nothing was written).

    ⚠️ Written whether or not a `ToolContext` exists. That is the gap
    `database.py:956-961` names: *"`tasks.note_tool()` returns immediately unless
    a task is RUNNING, so a destructive tool call made outside a task session —
    which is most of them — left no durable trace whatsoever."*

    `target` is passed in rather than derived: `tools._step_detail()` already owns
    the one human-readable label per tool, and re-deriving it here would be a
    second declaration of the same string.
    """
    try:
        if not _cfg.EXEC_PERSIST:
            return ""
        name = _text(tool, 64)
        if not name:
            return ""
        if destructive is None:
            destructive = name in _destructive_tools()
        paths = target_paths(name, args) if destructive else []
        pre = _digests(paths)
        call_id = uuid.uuid4().hex[:16]
        stamp = _now()
        from agent2.database import exe
        exe(
            "INSERT INTO exec_tool_calls("
            " id, instance, project, session_id, task_id, chat_id, surface, tool,"
            " detail, destructive, status, ok, error, started_at, completed_at, updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (call_id, INSTANCE, _project(), _text(session_id, 64), _text(task_id, 64),
             _text(chat_id, 64), _text(surface, 16), name,
             _detail_json(name, paths, target=target, pre=pre),
             1 if destructive else 0, _tasks.STEP_RUNNING, None, "",
             stamp, "", stamp),
        )
        with _lock:
            _pending[call_id] = (name, paths, pre)
            _prune_locked(_pending)
        _trim("exec_tool_calls")
        return call_id
    except Exception as exc:
        _fail("tool_started", exc)
        return ""


def tool_finished(call_id: str, *, ok: bool | None = None, error: str = "") -> None:
    """Settle a tool-call row, recording the POST digests of every path it named.

    The pre/post pair is what makes verification decisive: disk matches `post` ⇒
    applied, disk matches `pre` ⇒ not applied, neither ⇒ genuinely uncertain.
    """
    cid = _text(call_id, 64)
    if not cid:
        return
    try:
        if not _cfg.EXEC_PERSIST:
            return
        with _lock:
            held = _pending.pop(cid, None)
        if held is not None:
            tool, paths, pre = held
        else:
            # The map was pruned, or another process opened the row. The row itself
            # still carries everything needed, so re-read rather than lose the post.
            tool, paths, pre = _reload(cid)
        post = _digests(paths)
        status = _tasks.STEP_COMPLETED if (ok is None or ok) else _tasks.STEP_FAILED
        stamp = _now()
        from agent2.database import exe
        exe(
            "UPDATE exec_tool_calls SET status=?, ok=?, error=?, detail=?,"
            " completed_at=?, updated_at=?, instance=? WHERE id=?",
            (status, None if ok is None else (1 if ok else 0), _line(error),
             _detail_json(tool, paths, target=_target_of(cid, paths),
                          pre=pre, post=post, post_seen=True),
             stamp, stamp, INSTANCE, cid),
        )
        with _lock:
            _beats.pop(cid, None)
    except Exception as exc:
        _fail("tool_finished", exc)


def _reload(call_id: str) -> tuple[str, list[str], dict]:
    """Re-derive `(tool, paths, pre)` from a row this process did not open."""
    try:
        from agent2.database import qone
        row = qone("SELECT tool, detail FROM exec_tool_calls WHERE id=?", (call_id,))
    except Exception:
        return "", [], {}
    if not row:
        return "", [], {}
    body = parse_detail(row.get("detail"))
    paths = [str(p) for p in (body.get("paths") or []) if p]
    pre = body.get("pre")
    return str(row.get("tool") or ""), paths, dict(pre) if isinstance(pre, dict) else {}


def _target_of(call_id: str, paths) -> str:
    """The human label recorded at start, so a settle does not blank it."""
    try:
        from agent2.database import qone
        row = qone("SELECT detail FROM exec_tool_calls WHERE id=?", (call_id,))
        if row:
            label = parse_detail(row.get("detail")).get("target")
            if label:
                return str(label)
    except Exception:
        pass
    every = list(paths or [])
    return str(every[0]) if every else ""


def workflow_started(name: str, *, session_id: str = "", chat_id: str = "",
                     total_steps: int = 0, state: dict | None = None) -> str:
    """Open a workflow run row. **No caller until Phase 12** — see the docstring."""
    try:
        if not _cfg.EXEC_PERSIST:
            return ""
        run_id = uuid.uuid4().hex[:16]
        stamp = _now()
        from agent2.database import exe
        exe(
            "INSERT INTO exec_workflows("
            " id, instance, project, session_id, chat_id, name, status, step,"
            " step_index, total_steps, state, error, started_at, completed_at, updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, INSTANCE, _project(), _text(session_id, 64), _text(chat_id, 64),
             _text(name, 120), _tasks.STEP_RUNNING, "", 0, max(0, int(total_steps or 0)),
             _state_json(state), "", stamp, "", stamp),
        )
        _trim("exec_workflows")
        return run_id
    except Exception as exc:
        _fail("workflow_started", exc)
        return ""


def workflow_step(run_id: str, *, step: str = "", step_index: int | None = None,
                  state: dict | None = None) -> None:
    """Advance a workflow run. Throttled like a heartbeat when nothing changed."""
    rid = _text(run_id, 64)
    if not rid:
        return
    try:
        if not _cfg.EXEC_PERSIST:
            return
        sets = ["step=?", "updated_at=?", "instance=?"]
        vals: list = [_text(step, 200), _now(), INSTANCE]
        if step_index is not None:
            sets.insert(1, "step_index=?")
            vals.insert(1, max(0, int(step_index)))
        if state is not None:
            sets.insert(1, "state=?")
            vals.insert(1, _state_json(state))
        vals.append(rid)
        from agent2.database import exe
        exe(f"UPDATE exec_workflows SET {', '.join(sets)} WHERE id=?", tuple(vals))
    except Exception as exc:
        _fail("workflow_step", exc)


def workflow_finished(run_id: str, *, ok: bool | None = None, error: str = "",
                      state: dict | None = None) -> None:
    """Settle a workflow run.

    ⚠️ IT IS ALSO WHERE `workflow.duration` IS MEASURED (Task 27). The extra
    SELECT is deliberate and affordable: it happens once per workflow *run*, not
    per step, and the alternative — caching `started_at` in memory when the run
    opened — would be wrong for the case workflows exist to survive. A run can be
    started by one process and settled by another after a crash and a recovery, so
    the row is the only place its start time is knowable. `spanned()` reads the
    same second-resolution stamps `workflow_started` wrote, which is why there is
    no monotonic clock here either.
    """
    rid = _text(run_id, 64)
    if not rid:
        return
    try:
        if not _cfg.EXEC_PERSIST:
            return
        status = _tasks.STEP_COMPLETED if (ok is None or ok) else _tasks.STEP_FAILED
        stamp = _now()
        sets = ["status=?", "error=?", "completed_at=?", "updated_at=?", "instance=?"]
        vals: list = [status, _line(error), stamp, stamp, INSTANCE]
        if state is not None:
            sets.insert(1, "state=?")
            vals.insert(1, _state_json(state))
        vals.append(rid)
        from agent2.database import exe, qone
        began = qone("SELECT started_at FROM exec_workflows WHERE id=?", (rid,))
        exe(f"UPDATE exec_workflows SET {', '.join(sets)} WHERE id=?", tuple(vals))
        with _lock:
            _beats.pop(rid, None)
        if began and began["started_at"]:
            _metrics.spanned(_metrics.WORKFLOW_DURATION, began["started_at"],
                             stamp, status)
    except Exception as exc:
        _fail("workflow_finished", exc)


def _state_json(state) -> str:
    try:
        text = json.dumps(dict(state or {}), separators=(",", ":"))
    except Exception:
        return "{}"
    return text if len(text) <= MAX_STATE_CHARS else "{}"


def _destructive_tools() -> frozenset:
    """`tasks.DESTRUCTIVE_TOOLS`, never re-declared here."""
    try:
        return _tasks.DESTRUCTIVE_TOOLS
    except Exception:
        return frozenset()


# ── The trim ──────────────────────────────────────────────────────────────────

def _trim(table: str) -> None:
    """Hold a table at `EXEC_LEDGER_MAX`, oldest SETTLED row first.

    ⚠️ **AN UNSETTLED ROW IS NEVER EVICTED, whatever the count.** `llm/router.py`
    trims a pure history and can drop the oldest row unconditionally; this table
    holds live state, and the one row a crash makes precious is exactly the one an
    unconditional trim would delete. The table is therefore allowed to exceed the
    cap when unsettled rows dominate — the same discipline, and the same reason, as
    `commands._prune_locked()`.
    """
    settled = _SETTLED.get(table)
    if not settled:
        return
    try:
        cap = max(50, int(_cfg.EXEC_LEDGER_MAX))
    except Exception:
        cap = 500
    marks = ",".join("?" * len(settled))
    try:
        from agent2.database import exe
        exe(
            f"DELETE FROM {table} WHERE id IN ("  # table name is a module literal, values parameterised:table from _SETTLED, a module literal
            f" SELECT id FROM {table} WHERE status IN ({marks})"
            f" ORDER BY updated_at ASC, rowid ASC"
            f" LIMIT MAX(0, (SELECT COUNT(*) FROM {table}) - ?))",
            (*sorted(settled), cap),
        )
    except Exception as exc:
        _fail("trim", exc)


# ── Readers ───────────────────────────────────────────────────────────────────

def _rows(table: str, *, project=None, status: str = "", unsettled_only: bool = False,
          limit: int = 50, **scope) -> list[dict]:
    """One generic, total reader. Returns `[]` for anything it cannot answer."""
    if table not in _TERMINAL:
        return []
    where: list[str] = []
    vals: list = []
    if project != ANY_PROJECT:
        where.append("project=?")
        vals.append(_project() if project is None else str(project))
    for col in _FILTERS.get(table, ()):
        val = scope.get(col)
        if val:
            where.append(f"{col}=?")
            vals.append(str(val))
    if status:
        where.append("status=?")
        vals.append(str(status))
    if unsettled_only:
        settled = sorted(_SETTLED[table])
        where.append(f"status NOT IN ({','.join('?' * len(settled))})")
        vals.extend(settled)
    try:
        cap = max(1, min(1000, int(limit)))
    except Exception:
        cap = 50
    vals.append(cap)
    clause = f" WHERE {' AND '.join(where)}" if where else ""
    try:
        from agent2.database import qall
        return qall(
            f"SELECT * FROM {table}{clause}"  # table name is a module literal, values parameterised:fixed table + parameterised values
            f" ORDER BY updated_at DESC, rowid DESC LIMIT ?",
            tuple(vals),
        )
    except Exception as exc:
        _fail("read", exc)
        return []


def command(command_id: str) -> dict | None:
    """One command row by id, across every project. `None` when absent."""
    try:
        from agent2.database import qone
        return qone("SELECT * FROM exec_commands WHERE id=?", (_text(command_id, 64),))
    except Exception as exc:
        _fail("read", exc)
        return None


def workflow(run_id: str) -> dict | None:
    """One workflow run row by id, across every project. `None` when absent.

    The counterpart of `command()`, and project-blind for the same reason: a run id
    is already unique, and a workflow started in one directory may legitimately be
    *read* from another (the recovery panel, `/workflow status <id>`). Filtering by
    project here would answer "no such run" for a run that plainly exists — the
    least debuggable of the two wrong answers.
    """
    try:
        from agent2.database import qone
        return qone("SELECT * FROM exec_workflows WHERE id=?", (_text(run_id, 64),))
    except Exception as exc:
        _fail("read", exc)
        return None


def commands(*, project=None, session_id: str = "", task_id: str = "",
             status: str = "", active_only: bool = False, limit: int = 50) -> list[dict]:
    return _rows("exec_commands", project=project, status=status,
                 unsettled_only=active_only, limit=limit,
                 session_id=session_id, task_id=task_id)


def tool_calls(*, project=None, session_id: str = "", task_id: str = "",
               chat_id: str = "", status: str = "", active_only: bool = False,
               limit: int = 50) -> list[dict]:
    return _rows("exec_tool_calls", project=project, status=status,
                 unsettled_only=active_only, limit=limit,
                 session_id=session_id, task_id=task_id, chat_id=chat_id)


def workflows(*, project=None, session_id: str = "", chat_id: str = "",
              status: str = "", active_only: bool = False,
              limit: int = 50) -> list[dict]:
    return _rows("exec_workflows", project=project, status=status,
                 unsettled_only=active_only, limit=limit,
                 session_id=session_id, chat_id=chat_id)


def interrupted(*, project=None, stale_after: float | None = None,
                limit: int = 50) -> dict:
    """Work whose owning process is gone — the crash-detection reader.

    ⚠️ **BOTH HALVES OF THE PREDICATE ARE LOAD-BEARING.** A row qualifies only if
    it is not terminal **and** `instance != INSTANCE` **and** it has been silent
    for `EXEC_STALE_SEC`. Drop the instance test and dual mode reports the other
    live surface's command as a crash; drop the staleness test and a `sleep 300`
    reads as interrupted while it is still running.

    Idempotent across `sweep()`: a swept row keeps its old `updated_at` and its
    non-terminal status, so the reader that reported it still finds it.
    """
    cutoff = _cutoff(stale_after)
    out: dict = {"instance": INSTANCE, "cutoff": cutoff,
                 "commands": [], "tool_calls": [], "workflows": []}
    try:
        cap = max(1, min(1000, int(limit)))
    except Exception:
        cap = 50
    for table, key in (("exec_commands", "commands"),
                       ("exec_tool_calls", "tool_calls"),
                       ("exec_workflows", "workflows")):
        terminal = sorted(_TERMINAL[table])
        where = [f"status NOT IN ({','.join('?' * len(terminal))})",
                 "instance<>?", "updated_at<?"]
        vals: list = [*terminal, INSTANCE, cutoff]
        if project != ANY_PROJECT:
            where.append("project=?")
            vals.append(_project() if project is None else str(project))
        vals.append(cap)
        try:
            from agent2.database import qall
            out[key] = qall(
                f"SELECT * FROM {table} WHERE {' AND '.join(where)}"  # table name is a module literal, values parameterised:fixed table
                f" ORDER BY updated_at DESC, rowid DESC LIMIT ?",
                tuple(vals),
            )
        except Exception as exc:
            _fail("interrupted", exc)
    out["total"] = len(out["commands"]) + len(out["tool_calls"]) + len(out["workflows"])
    return out


def snapshot(*, project=None, limit: int = 50) -> dict:
    """Everything one process left behind, in one call.

    ⚠️ Sessions and tasks are read **through `core.tasks`**, never by querying
    `task_sessions`/`agent_tasks` here. Those rows already have an owner, and a
    second query would be a second declaration of what "unfinished" means.
    """
    out: dict = {
        "instance": INSTANCE,
        "project": _project() if project is None else str(project),
        "persist": bool(getattr(_cfg, "EXEC_PERSIST", True)),
        "commands": commands(project=project, limit=limit),
        "tool_calls": tool_calls(project=project, limit=limit),
        "workflows": workflows(project=project, limit=limit),
        "sessions": [],
    }
    try:
        from agent2.core import workspace as _ws
        out["sessions"] = _tasks.unfinished_sessions(str(_ws.root()))
    except Exception as exc:
        _fail("snapshot", exc)
    return out


def stats() -> dict:
    """Counters only — never a command line, never a path. Safe for `/api/health`."""
    out: dict = {
        "instance": INSTANCE,
        "persist": bool(getattr(_cfg, "EXEC_PERSIST", True)),
        "beat_sec": float(getattr(_cfg, "EXEC_BEAT_SEC", 0) or 0),
        "stale_sec": float(getattr(_cfg, "EXEC_STALE_SEC", 0) or 0),
        "ledger_max": int(getattr(_cfg, "EXEC_LEDGER_MAX", 0) or 0),
        "tracked": len(_beats),
        "pending": len(_pending),
    }
    for table in TABLES:
        block = {"total": 0, "active": 0, "interrupted": 0, "by_status": {}}
        try:
            from agent2.database import qall
            for row in qall(f"SELECT status, COUNT(*) AS n FROM {table} GROUP BY status"):  # table name is a module literal, values parameterised:fixed table
                word = str(row.get("status") or "")
                num = int(row.get("n") or 0)
                block["by_status"][word] = num
                block["total"] += num
                if word == INTERRUPTED:
                    block["interrupted"] += num
                elif word not in _SETTLED[table]:
                    block["active"] += num
        except Exception as exc:
            _fail("stats", exc)
        out[table] = block
    return out


# ── The sweep ─────────────────────────────────────────────────────────────────

def sweep(*, project=None, stale_after: float | None = None) -> dict:
    """Mark abandoned rows `interrupted` and report how many, per table.

    ⚠️ **NO TERMINAL-REFUSAL GUARD, ON PURPOSE.** A wrong sweep is
    self-correcting: `commands._mutate` owns "whichever verdict lands first wins"
    in memory, every lifecycle edge is always written, and the owning process's
    next edge restores the truth. Last write wins — which is what makes the
    foreign-instance predicate safe in dual mode, where a silent long-running
    command cannot heartbeat.

    ⚠️ `updated_at` is deliberately **not** bumped. It is the forensic record of
    when the dead process last spoke, it is what Task 25 reports, and bumping it
    would make the row look fresh to `interrupted()` — hiding the very crash this
    call just recorded.
    """
    counts: dict = {"commands": 0, "tool_calls": 0, "workflows": 0,
                    "cutoff": _cutoff(stale_after), "total": 0}
    found = interrupted(project=project, stale_after=stale_after, limit=1000)
    for table, key in (("exec_commands", "commands"),
                       ("exec_tool_calls", "tool_calls"),
                       ("exec_workflows", "workflows")):
        ids = [str(r.get("id") or "") for r in found.get(key) or []
               if str(r.get("status") or "") != INTERRUPTED]
        if not ids:
            continue
        marks = ",".join("?" * len(ids))
        try:
            from agent2.database import exe
            exe(
                f"UPDATE {table} SET status=? WHERE id IN ({marks})",  # table name is a module literal, values parameterised:fixed table
                (INTERRUPTED, *ids),
            )
            counts[key] = len(ids)
            counts["total"] += len(ids)
        except Exception as exc:
            _fail("sweep", exc)
    if counts["total"]:
        try:
            alog.exec_interrupted(
                commands=counts["commands"], tool_calls=counts["tool_calls"],
                workflows=counts["workflows"], cutoff=counts["cutoff"],
            )
        except Exception:  # logging is best-effort by contract; see this file's lint allowance
            pass
    return counts


def resolve_interrupted(table: str, row_id: str, status: str) -> bool:
    """Settle ONE `interrupted` ledger row with the verdict recovery reached.

    Task 25's state machine ends in a decision, and for two of those decisions the
    ledger row itself has an honest final answer: verification said the work landed
    (`completed`) or it did not (`failed`). Writing that answer is what stops the
    next launch from finding the same row and re-deciding it forever.

    ⚠️ **A COMPARE-AND-SWAP ON `status = INTERRUPTED`, AND THAT IS THE WHOLE
    GUARD.** Only a row this process has already established is abandoned may be
    moved. Without the predicate in the `WHERE`, a recovery scan racing the
    original process's own final write — dual mode is two processes over one DB —
    could overwrite a real `completed` with a guessed `failed`, and the transcript
    the model then reads would report a failure that never happened. The same
    "whichever verdict lands first wins" discipline `commands._mutate` owns in
    memory, expressed as one statement.

    ⚠️ `updated_at` is deliberately **not** bumped, for the reason `sweep()` gives:
    it is the forensic record of when the dead process last spoke. `completed_at`
    records when recovery settled it, which is a different fact and gets its own
    column.

    Returns True only if the row actually reads as *word* afterwards. Total: a
    broken DB is `False`.

    ⚠️ The result is established by **reading the row back**, not by a rowcount:
    `database.exe()` returns nothing, and inventing a second write path here to get
    one would be a second SQLite accessor — the one thing `database.py` forbids.
    The read-back is the same discipline `secrets.seal()` uses, and it answers a
    slightly better question anyway: not "did my statement match" but "does the row
    now say what I meant it to say".
    """
    if table not in TABLES or not str(row_id or ""):
        return False
    word = _text(status, 40)
    if not word or word == INTERRUPTED:
        return False
    try:
        from agent2.database import exe, qone
        exe(
            f"UPDATE {table} SET status=?, completed_at=?"  # table name is a module literal, values parameterised:fixed table
            f" WHERE id=? AND status=?",
            (word, _now(), str(row_id), INTERRUPTED),
        )
        row = qone(
            f"SELECT status FROM {table} WHERE id=?",  # table name is a module literal, values parameterised:fixed table
            (str(row_id),),
        )
        return bool(row) and str(row.get("status") or "") == word
    except Exception as exc:
        _fail("resolve", exc)
        return False


def reset() -> None:
    """Drop the in-memory throttle/pending books. Tests and a workspace switch.

    Deliberately does NOT delete rows: the ledger's whole purpose is to outlive
    the process, so a "reset" that truncated it would delete the evidence the
    next launch reads.
    """
    with _lock:
        _beats.clear()
        _pending.clear()
