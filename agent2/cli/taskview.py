# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/cli/taskview.py
──────────────────────
The terminal half of the persistent task list. `core/tasks.py` owns the data;
this module only draws it.

⚠️ THE BUG THIS FIXES
──────────────────────
`update_todo` had NO render branch in either CLI loop. The user saw the tool
banner ("✅ Updating task list") and nothing else — the checklist the model was
maintaining was invisible, which is why "task list is not displayed reliably"
was the first symptom reported.

⚠️ AND THE ONE IT MUST NOT CAUSE
─────────────────────────────────
Printing the panel on every `update_todo` is worse than not printing it: the
model re-sends the whole list on each call, so a 12-step plan would scroll the
terminal with 12 near-identical copies. `render(...)` therefore prints ONLY when
the visible state changed, tracked by `_LAST` (a fingerprint of what was last
drawn, per task session). A no-op sync draws nothing.

Force a redraw with `render(..., force=True)` — that is what `/tasks` uses, since
an explicit request must always produce output.

Colour comes from the shared `P` object, never a `from .theme import PU`
snapshot: that binds one colour at import and freezes it through `/theme`.

⚠️ AND `P.ACCENT` IN RICH MARKUP, NEVER `P.PU`
───────────────────────────────────────────────
`P.PU` is a raw ANSI escape (`\033[38;5;135m`) — correct for `print()`, and a
*markup error* inside a rich style string. Because `render()` swallows exceptions
(a panel may never kill a turn), `[bold {P.PU}]` did not raise anywhere visible:
it simply made the whole panel print NOTHING on every machine that has rich
installed, which is nearly all of them, while the plain fallback looked perfect.
Rich styles take the hex accent, `P.ACCENT`.

That is why the tests exercise BOTH renderers. A rendering test that only forces
`_RICH = False` proves the fallback works and says nothing about what a user
actually sees.
"""

from __future__ import annotations

from agent2.cli.env import _RICH, _con
from agent2.cli.theme import B, D, GR, P, R, RD, WH, YW
from agent2.core import tasks as _tasks

# Fingerprint of the last panel drawn, keyed by task-session id. Not an LRU: a
# CLI process sees a handful of sessions at most, and an entry is two short
# strings.
_LAST: dict[str, str] = {}

# status → (rich style, ANSI colour). One table, both renderers, so a themed
# terminal and a plain one cannot disagree about which task is running.
_STYLES = {
    _tasks.TaskStatus.PENDING:   ("#6a6a80", D),
    _tasks.TaskStatus.QUEUED:    ("#8a8ab0", D),
    _tasks.TaskStatus.RUNNING:   ("bold #f0c060", YW),
    _tasks.TaskStatus.PAUSED:    ("#c0a060", YW),
    _tasks.TaskStatus.COMPLETED: ("#5fd7a0", GR),
    _tasks.TaskStatus.FAILED:    ("bold #ff6b6b", RD),
    _tasks.TaskStatus.CANCELLED: ("#8a6a6a", D),
    _tasks.TaskStatus.SKIPPED:   ("#7a7a90", D),
}


def _fingerprint(tasks: list) -> str:
    """What the user can actually SEE. Deliberately excludes timestamps,
    attempt counts and checkpoints — a task whose checkpoint advanced but whose
    row looks identical must not trigger a redraw."""
    return "|".join(f"{t.seq}:{t.status}:{t.title}" for t in tasks)


def _line(index: int, task) -> tuple[str, str]:
    """(glyph+number prefix, title) for one row."""
    return f"{task.glyph} {index}.", task.title


def render(session_id: str, *, force: bool = False, title: str = "Tasks",
           detail: bool = False) -> bool:
    """Draw the checklist for *session_id*. Returns True if anything printed.

    `detail=True` adds each unfinished task's checkpoint — the sub-steps already
    done and the one that was in flight.

    ⚠️ It is OFF for the live panel on purpose. Sub-steps advance on every single
    tool call, so a panel that showed them would have to be redrawn every tool
    call to stay honest, and that is the duplicate-list scrolling this module
    exists to prevent. `/tasks` and the recovery prompt ask for it explicitly,
    where one deliberate print is exactly what the user wanted.

    Never raises: a panel is presentation, and a turn that died because its own
    progress display failed is strictly worse than one with no display.
    """
    try:
        tasks = _tasks.list_tasks(session_id)
    except Exception:
        return False
    if not tasks:
        return False

    stamp = _fingerprint(tasks)
    if not force and _LAST.get(session_id) == stamp:
        return False
    _LAST[session_id] = stamp

    try:
        summary = _tasks.summary(session_id, tasks)
        if _RICH:
            _render_rich(tasks, summary, title, detail)
        else:
            _render_plain(tasks, summary, title, detail)
    except Exception:
        return False
    return True


def _checkpoint_lines(task) -> list[tuple[str, str]]:
    """(kind, text) rows describing where a task stopped. Empty when unknown."""
    if task.status not in (_tasks.TaskStatus.RUNNING, _tasks.TaskStatus.PAUSED,
                           _tasks.TaskStatus.FAILED):
        return []
    try:
        view = _tasks.checkpoint_view(task)
    except Exception:
        return []
    if not view.get("total"):
        return []
    out: list[tuple[str, str]] = []
    done = view["completed"]
    if done:
        shown = ", ".join(done[-3:])
        more = f" (+{len(done) - 3} earlier)" if len(done) > 3 else ""
        out.append(("done", f"done: {shown}{more}"))
    if view.get("current"):
        # ⚠️ "was …", not "doing …": this step was in flight when we stopped and
        # may or may not have landed. Recovery must not assume either way.
        out.append(("cur", f"was: {view['current']}"))
    if view.get("remaining"):
        out.append(("left", f"left: {', '.join(view['remaining'][:3])}"))
    stopped = view.get("stopped") or {}
    if stopped.get("reason"):
        out.append(("stop", f"stopped: {stopped['reason']}"))
    return out


_CP_STYLE = {"done": ("#5fd7a0", GR), "cur": ("bold #f0c060", YW),
             "left": ("#6a6a80", D), "stop": ("#8a8ab0", D)}


def _render_rich(tasks: list, summary: dict, title: str,
                 detail: bool = False) -> None:
    from rich.panel import Panel
    from rich.text import Text

    body = Text()
    for i, task in enumerate(tasks, 1):
        style, _ = _STYLES.get(task.status, _STYLES[_tasks.TaskStatus.PENDING])
        prefix, label = _line(i, task)
        body.append(f"  {prefix} ", style=style)
        # A finished item is struck through so the eye lands on what is left.
        body.append(f"{label}\n",
                    style=f"{style} strike" if task.status ==
                    _tasks.TaskStatus.COMPLETED else style)
        if task.status == _tasks.TaskStatus.FAILED and task.error:
            body.append(f"      ↳ {task.error[:120]}\n", style="#ff6b6b")
        if detail:
            for kind, text in _checkpoint_lines(task):
                body.append(f"      · {text}\n", style=_CP_STYLE[kind][0])

    body.append(f"\n  {summary['completed']}/{summary['total']} completed",
                style="bold #5fd7a0" if summary["done"] else "#8a8ab0")
    # ⚠️ `P.ACCENT` (hex), NOT `P.PU` (a raw ANSI escape). Rich parses a style
    # string, so an escape sequence there is not a colour — it is a markup error,
    # and this module swallows those, so the whole panel printed NOTHING in the
    # normal (rich) terminal while the plain fallback looked perfect. Both new
    # `_rich_render` tests exist to keep that path exercised.
    _con.print(Panel(body, title=Text(title, style=f"bold {P.ACCENT}"),
                     border_style="#2a2a40", padding=(0, 1)))


def _render_plain(tasks: list, summary: dict, title: str,
                  detail: bool = False) -> None:
    print(f"\n  {P.PU}{B}{title}{R}")
    for i, task in enumerate(tasks, 1):
        _, colour = _STYLES.get(task.status, _STYLES[_tasks.TaskStatus.PENDING])
        prefix, label = _line(i, task)
        print(f"  {colour}{prefix}{R} {WH}{label}{R}")
        if task.status == _tasks.TaskStatus.FAILED and task.error:
            print(f"      {RD}↳ {task.error[:120]}{R}")
        if detail:
            for kind, text in _checkpoint_lines(task):
                print(f"      {_CP_STYLE[kind][1]}· {text}{R}")
    tone = GR if summary["done"] else D
    print(f"\n  {tone}{summary['completed']}/{summary['total']} completed{R}\n")


def render_for_result(result: dict, *, force: bool = False) -> bool:
    """Draw the panel straight from an `update_todo` tool result.

    The tool returns the session id when (and only when) the merge was actually
    persisted, so a degraded in-memory run draws nothing rather than drawing a
    stale panel from some other session.
    """
    if not isinstance(result, dict) or not result.get("persisted"):
        return False
    return render(str(result.get("session_id") or ""), force=force)


def forget(session_id: str = "") -> None:
    """Drop the redraw fingerprint so the next render prints unconditionally."""
    if session_id:
        _LAST.pop(session_id, None)
    else:
        _LAST.clear()


# ── Recovery (Task 3) ─────────────────────────────────────────────────────────
#
# ⚠️ THIS PANEL IS NOT THE TASK PANEL, AND IT DELIBERATELY REPEATS ITSELF.
# `render()` suppresses an unchanged redraw because a live checklist reprints on
# every tool call. A recovery briefing is the opposite kind of event: it happens
# once per launch, the user has not seen it before, and it is the thing they must
# read before answering a question. So it never consults `_LAST`, and the
# fingerprint is not written either — the first live render after a recovery must
# still print.

_VERDICT_STYLE = {
    # `not_applied` is the only safe-to-repeat verdict, so it is the only green
    # one. Everything else is a question the user has to answer.
    "not_applied":    ("#5fd7a0", GR),
    "likely_applied": ("bold #ff6b6b", RD),
    "uncertain":      ("bold #f0c060", YW),
    "unverifiable":   ("bold #f0c060", YW),
}

_VERDICT_LABEL = {
    "not_applied":    "did NOT happen — safe to repeat",
    "likely_applied": "PROBABLY ALREADY HAPPENED — do not repeat blindly",
    "uncertain":      "UNCERTAIN — verify before repeating",
    "unverifiable":   "CANNOT BE VERIFIED — check the system state yourself",
}


def _recovery_rows(rp) -> list[tuple[str, str]]:
    """(kind, text) rows for a recovery plan. Pure — no printing, no reads."""
    rows: list[tuple[str, str]] = [
        ("done", f"✓ {t.title}  (already completed)") for t in rp.done]
    if rp.resume is not None:
        rows.append(("cur", f"↻ {rp.resume.title}  (resuming here)"))
        view = rp.view or {}
        if view.get("completed"):
            rows.append(("sub", "    done: " + ", ".join(view["completed"][-3:])))
        if view.get("current"):
            rows.append(("sub", f"    was: {view['current']}"))
        reason = (view.get("stopped") or {}).get("reason", "")
        if reason:
            rows.append(("sub", f"    stopped: {reason}"))
    rows.extend(("left", f"○ {t.title}  (waiting)") for t in rp.waiting)
    for check in rp.checks:
        label = _VERDICT_LABEL.get(check.get("verdict", ""), check.get("verdict", ""))
        target = f" {check['target']}" if check.get("target") else ""
        rows.append((check.get("verdict", "uncertain"),
                     f"⚠ {check.get('tool', '?')}{target} — {label}"))
    return rows


def render_recovery(rp, *, title: str = "Recovered session") -> bool:
    """Draw a `core.recovery.RecoveryPlan`. Returns True if anything printed.

    Never raises, for the same reason `render()` does not: this runs at launch,
    and a display bug in a recovery hint must not be able to stop the CLI from
    starting.
    """
    try:
        if rp is None or rp.is_empty():
            return False
        rows = _recovery_rows(rp)
        if _RICH:
            _render_recovery_rich(rp, rows, title)
        else:
            _render_recovery_plain(rp, rows, title)
    except Exception:
        return False
    return True


def _row_styles(kind: str) -> tuple[str, str]:
    if kind in _VERDICT_STYLE:
        return _VERDICT_STYLE[kind]
    if kind == "sub":
        return ("#6a6a80", D)
    return _CP_STYLE.get(kind, ("#8a8ab0", D))


def _recovery_footer(rp) -> str:
    return (f"  {len(rp.done)} already done · resuming from "
            f"{rp.resume.title if rp.resume is not None else '—'}"
            f" · {len(rp.waiting)} waiting")


def _render_recovery_rich(rp, rows: list, title: str) -> None:
    from rich.panel import Panel
    from rich.text import Text

    body = Text()
    if rp.goal:
        body.append(f"  {rp.goal}\n\n", style="#8a8ab0")
    for kind, text in rows:
        body.append(f"  {text}\n", style=_row_styles(kind)[0])
    body.append("\n" + _recovery_footer(rp),
                style="bold #ff6b6b" if rp.needs_verification else "#8a8ab0")
    # `P.ACCENT` (hex) — see the docstring; `P.PU` here prints nothing at all.
    _con.print(Panel(body, title=Text(title, style=f"bold {P.ACCENT}"),
                     border_style="#2a2a40", padding=(0, 1)))


def _render_recovery_plain(rp, rows: list, title: str) -> None:
    print(f"\n  {P.PU}{B}{title}{R}")
    if rp.goal:
        print(f"  {D}{rp.goal}{R}")
    for kind, text in rows:
        print(f"  {_row_styles(kind)[1]}{text}{R}")
    tone = RD if rp.needs_verification else D
    print(f"\n{tone}{_recovery_footer(rp)}{R}\n")


# ── Crash recovery (Task 25 §9) ───────────────────────────────────────────────
#
# ⚠️ THIS IS A REPORT, NOT A PROMPT. Task 3's panel above asks a question and the
# launch waits for the answer; this one states what the startup scan already
# decided and returns. The distinction is the whole reason it is a second function
# rather than a branch in `render_recovery`: a scan runs on EVERY launch, and a
# launch that stopped for a confirmation whenever a previous run had crashed would
# make an unattended start (a container, `agent2 dual`, a cron job) hang forever.
#
# ⚠️ AND IT PRINTS NOTHING WHEN THERE IS NOTHING TO SAY. A "recovery: 0 units"
# line on every launch is noise, and noise on the launch path is what teaches a
# user to skip past the line that matters. The one thing it always prints when
# present is the review queue — those entries do not resolve on their own.
#
# ⚠️ NO COMMAND LINES, NO ARGUMENTS, NO PATHS. It renders `crash._payload()`
# fields only, which is why the reason strings are our own prose. The panel is on
# a user's screen and in their scrollback, and `run_command` argv routinely holds
# a bearer token — the same rule the recovery log lines follow.

_STATE_STYLE = {
    "needs_review":    ("bold #f0c060", YW),
    "recovery_failed": ("bold #ff6b6b", RD),
    "recovered":       ("#5fd7a0", GR),
    "recovering":      ("#c0a060", YW),
    "verifying":       ("#8a8ab0", D),
    "interrupted":     ("#8a8ab0", D),
}

#: What each decision means in one phrase. The decision word alone is ambiguous
#: at a glance — "retry" reads as "it will run again", which is true only for the
#: operations whose safety kind permits an unattended repeat.
_DECISION_LABEL = {
    "retry":         "queued to run again (safe to repeat)",
    "resume":        "will continue from its checkpoint",
    "mark_complete": "marked finished — the work was already done",
    "review":        "WAITING FOR YOU — recovery would not guess",
    "none":          "nothing to do",
}


def _crash_rows(report: dict) -> list[tuple[str, str]]:
    """(style-key, text) rows for a `crash.report()`. Pure — no printing."""
    from agent2.core.recovery import crash as _crash

    rows: list[tuple[str, str]] = []
    scan = report.get("last_scan") or {}
    for entry in report.get("review") or []:
        rows.append((entry.get("state") or "needs_review",
                     f"⚠ {_crash.describe(entry)}"))
        label = _DECISION_LABEL.get(entry.get("decision") or "", "")
        if label:
            rows.append(("sub", f"    {label}"))
    if scan.get("recovered"):
        bits = []
        if scan.get("resumed"):
            bits.append(f"{scan['resumed']} resumed")
        if scan.get("retried"):
            bits.append(f"{scan['retried']} to retry")
        rows.append(("recovered",
                     f"✓ {scan['recovered']} unit(s) recovered"
                     + (f"  ({', '.join(bits)})" if bits else "")))
    if scan.get("live_owner"):
        rows.append(("sub", f"    {scan['live_owner']} left alone — the process "
                            f"that owns it is still running"))
    if scan.get("truncated"):
        # ⚠️ Said out loud, always. A truncated scan looks exactly like a clean one
        # from the counts, and "recovery found nothing" is the wrong conclusion to
        # let a user draw from a scan that simply ran out of budget.
        rows.append(("needs_review",
                     "⚠ the scan hit its limit — more interrupted work remains"))
    return rows


def render_crash_recovery(report: dict, *, title: str = "Recovery") -> bool:
    """Draw a `crash.report()`. Returns True if anything printed.

    Never raises and never blocks: it runs on the launch path, so a display bug
    here must not be able to stop the CLI from starting — the same contract
    `render()` and `render_recovery()` hold.
    """
    try:
        if not report or not report.get("enabled"):
            return False
        rows = _crash_rows(report)
        if not rows:
            return False
        footer = _crash_footer(report)
        if _RICH:
            _render_crash_rich(rows, title, footer,
                              bool(report.get("needs_review")))
        else:
            _render_crash_plain(rows, title, footer,
                                bool(report.get("needs_review")))
    except Exception:
        return False
    return True


def _crash_footer(report: dict) -> str:
    scan = report.get("last_scan") or {}
    review = int(report.get("needs_review") or 0)
    parts = [f"{int(scan.get('processed') or 0)} handled"]
    if review:
        parts.append(f"{review} awaiting review — /recovery")
    if scan.get("failed"):
        parts.append(f"{scan['failed']} recovery failure(s)")
    return "  " + " · ".join(parts)


def _render_crash_rich(rows: list, title: str, footer: str, urgent: bool) -> None:
    from rich.panel import Panel
    from rich.text import Text

    body = Text()
    for kind, text in rows:
        body.append(f"  {text}\n", style=_crash_style(kind)[0])
    body.append("\n" + footer, style="bold #f0c060" if urgent else "#8a8ab0")
    # `P.ACCENT` (hex), never `P.PU` — see this module's docstring.
    _con.print(Panel(body, title=Text(title, style=f"bold {P.ACCENT}"),
                     border_style="#2a2a40", padding=(0, 1)))


def _render_crash_plain(rows: list, title: str, footer: str, urgent: bool) -> None:
    print(f"\n  {P.PU}{B}{title}{R}")
    for kind, text in rows:
        print(f"  {_crash_style(kind)[1]}{text}{R}")
    print(f"\n{YW if urgent else D}{footer}{R}\n")


def _crash_style(kind: str) -> tuple[str, str]:
    if kind == "sub":
        return ("#6a6a80", D)
    return _STATE_STYLE.get(kind, ("#8a8ab0", D))
