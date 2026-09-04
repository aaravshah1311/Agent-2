# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2.core.logging
────────────────────
THE single structured-logging surface for the whole app (section 14).

Every subsystem — workspace changes, tool execution/failures, session
lifecycle, stream ownership, task cancellation, path rejections and tool
registry loading — routes through here so there is one consistent, timestamped,
grep-able audit trail shared by the Web UI and the CLI.

Design goals
  - Never raise. A logging failure must never break the agent.
  - Structured: every event is `kind` + key=value fields on one line, so it is
    both human-readable and machine-parseable.
  - Failures carry stack traces (`log.exception`) so post-mortems are possible.
  - One file (`logs/agent2.log`, beside the DB), plus stderr for warnings and above.
    The file is always written; the stderr mirror can be turned off with
    AGENT2_LOG_CONSOLE=0 (dual mode does this so the web half cannot print over
    the CLI prompt they share).

Usage
    from agent2.core import logging as alog
    alog.workspace_change(old="/a", new="/b", wid="w1")
    alog.tool_exec("read_file", ok=True, path="x.py")
    alog.path_rejected("write_file", requested="../../etc", root="/proj")
    alog.exception("tool crashed", tool="convert_file")
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import threading
from pathlib import Path

_LOCK = threading.Lock()
_LOGGER: logging.Logger | None = None
_MAX_BYTES = 5 * 1024 * 1024   # 5 MB per file
_BACKUPS = 3

# Mirror warnings+ to the console as well as the file. Dual mode sets this to "0"
# because the CLI owns that terminal: a WARNING raised by the web half (closing a
# browser tab fires session.cancel, which is a WARNING) would otherwise paint over
# the CLI prompt from a background thread. The file handler is unaffected, so the
# audit trail in agent2.log stays complete either way.
_CONSOLE = (os.environ.get("AGENT2_LOG_CONSOLE") or "1").strip().lower() not in (
    "0", "no", "false", "off")


def _log_path() -> Path:
    """Put the audit log in the shared logs/ folder (beside the DB, so a
    containerised run keeps it on the mounted volume)."""
    try:
        from agent2.config import log_path
        return log_path("agent2.log")
    except Exception:
        return Path("agent2.log")


def _get() -> logging.Logger:
    """Lazily build the singleton logger (thread-safe, idempotent)."""
    global _LOGGER
    if _LOGGER is not None:
        return _LOGGER
    with _LOCK:
        if _LOGGER is not None:
            return _LOGGER
        lg = logging.getLogger("agent2")
        lg.setLevel(logging.INFO)
        lg.propagate = False
        fmt = logging.Formatter(
            "%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S"
        )
        # File handler (rotating). Falls back silently to stderr-only if the
        # path is unwritable (read-only FS, permissions, etc.).
        try:
            fh = logging.handlers.RotatingFileHandler(
                str(_log_path()), maxBytes=_MAX_BYTES, backupCount=_BACKUPS,
                encoding="utf-8",
            )
            fh.setFormatter(fmt)
            lg.addHandler(fh)
        except Exception:
            pass
        if _CONSOLE:
            try:
                sh = logging.StreamHandler()
                sh.setFormatter(fmt)
                sh.setLevel(logging.WARNING)   # only warnings+ hit the console
                lg.addHandler(sh)
            except Exception:
                pass
        _LOGGER = lg
        return lg


def _fields(fields: dict) -> str:
    parts = []
    for k, v in fields.items():
        if v is None:
            continue
        s = str(v).replace("\n", " ")
        if len(s) > 300:
            s = s[:300] + "…"
        if " " in s:
            s = f'"{s}"'
        parts.append(f"{k}={s}")
    return " ".join(parts)


def event(kind: str, level: int = logging.INFO, **fields) -> None:
    """Emit a structured event line. Never raises."""
    try:
        _get().log(level, "%-18s %s", kind, _fields(fields))
    except Exception:
        pass


def exception(msg: str, **fields) -> None:
    """Emit an ERROR line WITH the current stack trace (section 14)."""
    try:
        _get().error("%-18s %s", msg, _fields(fields), exc_info=True)
    except Exception:
        pass


# ── Named event wrappers (section 14 enumerates exactly these) ────────────────

def workspace_change(old: str | None, new: str, wid: str) -> None:
    event("workspace.change", old=old, new=new, wid=wid)


def workspace_validated(path: str, ok: bool, reason: str | None = None) -> None:
    event("workspace.validate", path=path, ok=ok, reason=reason)


def path_rejected(tool: str, requested: str, root: str) -> None:
    event("path.rejected", level=logging.WARNING,
          tool=tool, requested=requested, root=root)


def tool_exec(tool: str, ok: bool = True, **fields) -> None:
    event("tool.exec", tool=tool, ok=ok, **fields)


def tool_failure(tool: str, error: str, **fields) -> None:
    event("tool.failure", level=logging.WARNING, tool=tool, error=error, **fields)


def registry_loaded(count: int, names: str) -> None:
    event("registry.loaded", count=count, names=names)


def registry_reject(reason: str, name: str) -> None:
    event("registry.reject", level=logging.ERROR, reason=reason, name=name)


def session_open(sid: str, chat_id: str, wid: str | None = None) -> None:
    event("session.open", sid=sid, chat=chat_id, wid=wid)


def session_close(sid: str, chat_id: str) -> None:
    event("session.close", sid=sid, chat=chat_id)


def session_cancel(sid: str, chat_id: str | None) -> None:
    event("session.cancel", level=logging.WARNING, sid=sid, chat=chat_id or "*")


def stream_owner(sid: str, chat_id: str, task_id: str) -> None:
    event("stream.owner", sid=sid, chat=chat_id, task=task_id)


def stream_dropped(reason: str, **fields) -> None:
    event("stream.dropped", level=logging.WARNING, reason=reason, **fields)


def context_source_failed(source: str, error: str) -> None:
    """A Context Broker source could not be collected (Task 20).

    A WARNING rather than an ERROR: the turn continues without that source, which
    is the designed degradation. It is logged at all because "the prompt is
    missing the project's own instructions" is otherwise completely silent — the
    model simply behaves as though the file did not exist.
    """
    event("context.source", level=logging.WARNING, source=source, error=error)


def context_trimmed(dropped: str, used: int, limit: int, basis: str,
                    over: bool = False) -> None:
    """The Context Broker left sources out to fit the model's window (Task 22).

    INFO, not a warning: a trim is the policy working — the ceiling is real and
    something has to give. It is logged because the alternative is that the two
    surfaces disagree about what the model was told and nobody can see why; with
    `basis` in the line an operator can tell "the window was assumed" from "the
    window was read", which is usually the whole explanation.

    `over` is the case that is NOT routine: the pinned floor (memories, rules and
    the conversation) alone exceeds the ceiling. Nothing was dropped to fix it — by
    design, see `core/broker/budget.py` — so this is the only notice that a prompt
    went out over budget.
    """
    event("context.trimmed", level=logging.WARNING if over else logging.INFO,
          dropped=dropped or "-", used=used, limit=limit, basis=basis,
          over="yes" if over else "no")

