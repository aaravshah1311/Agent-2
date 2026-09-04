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
