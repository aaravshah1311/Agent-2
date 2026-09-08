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


def exec_interrupted(commands: int = 0, tool_calls: int = 0, workflows: int = 0,
                     cutoff: str = "") -> None:
    """Durable execution rows whose owning process is gone (Task 24).

    A WARNING, and the only place a crash is announced in the log: by the time
    `execstate.sweep()` runs, the process that was doing this work is already dead
    and took its own logs' final lines with it. Counters and the staleness cutoff
    only — never a command line, never a path, because this line is written on a
    path that has just proven the machine is in an unknown state.
    """
    event("exec.interrupted", level=logging.WARNING, commands=commands,
          tool_calls=tool_calls, workflows=workflows, cutoff=cutoff or "-")


def exec_persist_failed(op: str, error: str) -> None:
    """The durable execution ledger could not be written (Task 24).

    Logged **once per process** by `execstate._fail()`, not once per failure.
    `commands.heartbeat()` fires per output LINE, so a `make -j8` against a broken
    DB would otherwise write thousands of identical warnings and bury the rest of
    the file. It is logged at all because the degradation is silent by design —
    the turn continues perfectly, and only the *next* launch discovers it has no
    idea what was in flight.
    """
    event("exec.persist", level=logging.WARNING, op=op, error=error)


def project_scanned(root: str, files: int = 0, languages: int = 0, managers: int = 0,
                    tests: int = 0, truncated: bool = False, errors: int = 0,
                    ms: float = 0.0) -> None:
    """A `/init` project scan completed (Task 29).

    INFO — it is a read-only analysis of the workspace, so the interesting cases
    are the two counters at the end. `truncated` means a ceiling in
    `config.INIT_*` engaged, and `errors` means an analysis step failed and its
    part of the answer is simply absent; Tasks 30 and 31 write this scan into
    `.agent2/agent2.md`, so both are the difference between "this project has no
    tests" and "I never got as far as looking".

    `root` is a path and that is deliberate — the same fact `path_rejected` and
    `workspace_change` already record, and "which directory did it describe" is
    the first question anyone reading this line has.
    """
    event("project.scan", root=root, files=files, langs=languages,
          managers=managers, tests=tests,
          truncated="yes" if truncated else "no", errors=errors, ms=round(ms, 1))


def project_doc_written(doc: str, created: bool = False, changed: bool = False,
                        added: int = 0, updated: int = 0, preserved: int = 0) -> None:
    """`/init` created or refreshed `.agent2/agent2.md` (Tasks 30–31).

    The one line that records a WRITE where `project_scanned` records a read, and
    the counters are the interesting half. `preserved` is the number of sections
    a human owns and `/init` left alone — the count that would drop to zero if the
    marker logic ever regressed, which is the failure this event exists to make
    visible after the fact. `changed=no` is a real outcome, not a no-op worth
    hiding: a second `/init` that rewrites nothing is the design working.
    """
    event("project.doc", doc=doc,
          created="yes" if created else "no", changed="yes" if changed else "no",
          added=added, updated=updated, preserved=preserved)


def skills_applied(applied: str, reasons: str = "", considered: int = 0,
                   omitted: int = 0, chars: int = 0, truncated: bool = False) -> None:
    """Which skills reached this turn's prompt, and why (Task 36).

    INFO. This is the durable half of Task 36's *"report which skills applied"*:
    `/skills` and `GET /api/skills` show the live selection, but they can only ever
    describe the *current* state, and the question a user actually asks is about a
    turn that has already happened — *why did it ignore my skill*.

    `applied` is the ordered id list and `reasons` the matching tier for each, so
    one line answers both halves; `omitted` is a count because the reasons are per
    skill and this is a log line, not the report. ⚠️ **Ids and counters only, never
    a skill's text or its absolute path** — a skill is prose a human wrote in their
    own checkout, and the audit log is the one file most likely to be pasted into
    an issue. `truncated` says a ceiling in `config.SKILLS_*` engaged, which is the
    difference between "this project has no such skill" and "I stopped reading".
    """
    event("skills.applied", applied=applied or "-", reasons=reasons or "-",
          considered=considered, omitted=omitted, chars=chars,
          truncated="yes" if truncated else "no")


# ── Recovery (Task 25 §11) ────────────────────────────────────────────────────
# The nine events Task 25 enumerates, and nothing that is not one of them.
#
# ⚠️ IDENTIFIERS AND VERDICTS ONLY — NEVER THE PAYLOAD. A recovery line may carry
# `session_id`, `task_id`, `execution_id`, `workflow_id`, the operation kind and
# the decision; it may never carry the command line, a file path's contents, a
# provider response or an argument dict. That is not tidiness: `run_command`
# argv routinely holds a token (`curl -H "Authorization: …"`), and the audit log
# is the one file in the install that is *meant* to be read by whoever is
# debugging — which is exactly the reader Task 25 says must not learn a secret.
# `crash.py` passes `command=` nowhere, and a test pins that.
#
# ⚠️ THE UNIT'S KIND IS LOGGED AS `unit=`, NEVER `kind=`. `event()`'s own first
# parameter is called `kind` — it is the event NAME (`recovery.interrupted`) — so a
# wrapper forwarding `kind=kind` raises `TypeError: got multiple values for argument
# 'kind'` on every single call. That is not a style choice: it is a real collision
# that shipped once and was found only when the first recovery scan ran, because
# these wrappers swallow nothing and the exception surfaced inside the caller's
# `except`. `test_crashrecovery` calls all nine.

def recovery_scan_started(instance: str = "", project: str = "",
                          stale_sec: float = 0.0) -> None:
    """A startup (or on-demand) recovery scan began — RECOVERY_SCAN_STARTED."""
    event("recovery.scan", instance=instance, project=project or "-",
          stale_sec=stale_sec)


def recovery_scan_finished(found: int = 0, recovered: int = 0, retried: int = 0,
                           review: int = 0, failed: int = 0,
                           elapsed_ms: int = 0, truncated: bool = False) -> None:
    """The scan's own summary. INFO even when it found things: a clean report of
    interrupted work is recovery working, not an incident. `truncated` is the one
    field that matters afterwards — it means the scan hit its budget and there is
    more in the ledger than this line accounts for."""
    event("recovery.scan.done", found=found, recovered=recovered, retried=retried,
          review=review, failed=failed, ms=elapsed_ms,
          truncated="yes" if truncated else "no")


def execution_interrupted(kind: str, ref_id: str, *, session_id: str = "",
                          task_id: str = "", operation: str = "",
                          interrupted_at: str = "") -> None:
    """One unit of work is known to have died mid-flight — EXECUTION_INTERRUPTED.

    A WARNING and one line per unit, where `exec_interrupted()` above is one line
    per sweep. Both exist because they answer different questions: the sweep line
    says "this launch found a crash", this one says "and *that* is what was
    running". `operation` is the safety kind, which is the field an operator
    actually needs — "a git_commit was in flight" is the whole story.
    """
    event("recovery.interrupted", level=logging.WARNING, unit=kind,
          execution_id=ref_id, session_id=session_id or "-", task_id=task_id or "-",
          operation=operation or "-", at=interrupted_at or "-")


def recovery_started(kind: str, ref_id: str, *, classification: str = "",
                     attempt: int = 1, session_id: str = "",
                     task_id: str = "") -> None:
    """TASK_RECOVERY_STARTED — a unit entered the recovery state machine."""
    event("recovery.start", unit=kind, execution_id=ref_id,
          classification=classification or "-", attempt=attempt,
          session_id=session_id or "-", task_id=task_id or "-")


def recovery_verified(kind: str, ref_id: str, *, verdict: str = "",
                      decision: str = "", session_id: str = "") -> None:
    """TASK_RECOVERY_VERIFIED — external state was consulted and answered.

    The verdict is the load-bearing field: `not_applied` is the only one that
    licenses an unattended repeat, so this line is the record of *why* the next
    one says retried rather than needs-review.
    """
    event("recovery.verified", unit=kind, execution_id=ref_id,
          verdict=verdict or "-", decision=decision or "-",
          session_id=session_id or "-")


def recovery_resumed(kind: str, ref_id: str, *, session_id: str = "",
                     task_id: str = "", at: str = "") -> None:
    """TASK_RECOVERY_RESUMED — work continued from a checkpoint, not restarted."""
    event("recovery.resumed", unit=kind, execution_id=ref_id,
          session_id=session_id or "-", task_id=task_id or "-", resume_at=at or "-")


def recovery_retried(kind: str, ref_id: str, *, attempt: int = 1,
                     operation: str = "", session_id: str = "") -> None:
    """TASK_RECOVERY_RETRIED — the unit was queued to run again.

    Only ever reached for an operation whose safety kind permits an unattended
    repeat, or one whose verification returned `not_applied`. `operation` is on
    the line so that "recovery retried a delete" is impossible to miss in review.
    """
    event("recovery.retried", unit=kind, execution_id=ref_id, attempt=attempt,
          operation=operation or "-", session_id=session_id or "-")


def recovery_completed(kind: str, ref_id: str, *, decision: str = "",
                       session_id: str = "", task_id: str = "") -> None:
    """TASK_RECOVERY_COMPLETED — this unit needs nothing further."""
    event("recovery.completed", unit=kind, execution_id=ref_id,
          decision=decision or "-", session_id=session_id or "-",
          task_id=task_id or "-")


def recovery_failed(kind: str, ref_id: str, *, error: str = "", attempt: int = 0,
                    session_id: str = "") -> None:
    """TASK_RECOVERY_FAILED — the recovery attempt itself broke."""
    event("recovery.failed", level=logging.WARNING, unit=kind, execution_id=ref_id,
          error=error, attempt=attempt, session_id=session_id or "-")


def recovery_needs_review(kind: str, ref_id: str, *, reason: str = "",
                          operation: str = "", verdict: str = "",
                          session_id: str = "", task_id: str = "") -> None:
    """RECOVERY_NEEDS_REVIEW — recovery stopped and is waiting for a human.

    A WARNING, because it is the one recovery outcome that will not resolve on its
    own: nothing further happens to this unit until someone looks. `reason` is
    always our own prose (`"process still alive"`, `"attempts exhausted"`), never
    an echoed argument.
    """
    event("recovery.review", level=logging.WARNING, unit=kind, execution_id=ref_id,
          reason=reason or "-", operation=operation or "-", verdict=verdict or "-",
          session_id=session_id or "-", task_id=task_id or "-")


# ── Verification (Phase D3) ───────────────────────────────────────────────────

def verification_reported(ref_id: str, *, verdict: str = "", checks: int = 0,
                          problems: int = 0, session_id: str = "") -> None:
    """VERIFICATION_REPORTED — a completion claim was checked against the record.

    ⚠️ Logged for **every** verdict, not only the bad one. A line that appears
    only on `contradicted` cannot answer "was this run verified at all", which is
    the question *"'Done' is not verification"* actually asks — an absent line
    would be indistinguishable from a verifier nobody called.

    `ref_id` is a task id or a run id; `problems` is a **count**, because the
    problem lines name paths and commands and this file is not where those belong.
    """
    event("verify.reported", execution_id=ref_id, verdict=verdict or "-",
          checks=checks, problems=problems, session_id=session_id or "-")


def verification_contradicted(ref_id: str, *, reason: str = "",
                              check: str = "", session_id: str = "") -> None:
    """VERIFICATION_CONTRADICTED — something claimed done, and the record disagrees.

    A WARNING, and it is the one verification line that is: every other verdict
    resolves itself as the run proceeds, while this one is a claim already made to
    a human that nothing further will correct. `reason` is our own prose.
    """
    event("verify.contradicted", level=logging.WARNING, execution_id=ref_id,
          reason=reason or "-", check=check or "-", session_id=session_id or "-")


