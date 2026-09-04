# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/cli/ux.py
────────────────
Modern UX components: status bar, activity feed, progress stages, execution cards,
and rich displays that make the CLI feel like Claude Code / Cursor / Warp.

All components degrade gracefully when Rich is unavailable.
"""

import threading
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar, Literal

from agent2.cli.env import Panel, Text, _con, _RICH
from agent2.cli.theme import B, D, GR, P, R, RD, WH, YW
from agent2.core.progress import STAGES as _CORE_STAGES


# ── Activity Feed ──────────────────────────────────────────────────────────────
class ActivityFeed:
    """Live timestamped activity log - shows what Agent2 is doing in real time.

    Instead of hiding operations, this makes them visible:
      09:42:10  Loaded Workspace
      09:42:11  Read README
      09:42:12  Loaded Memory
      09:42:13  Searching Docs
      09:42:15  Calling GPT
      09:42:19  Editing Files
    """

    def __init__(self, max_entries: int = 50):
        self._entries: deque = deque(maxlen=max_entries)
        self._lock = threading.Lock()

    def log(self, message: str, kind: Literal["info", "success", "warning", "error"] = "info"):
        """Add an entry to the activity feed.

        ⚠️ The stamp is LOCAL wall-clock, via `.astimezone()`. This line is read
        by the person at the terminal beside their own clock — a UTC reading
        would be the wrong time for most of them. Same deliberate choice as
        `cli/render.py` (DTZ005) and `server/weblog.py` (DTZ006).
        """
        ts = datetime.now(tz=UTC).astimezone().strftime("%H:%M:%S")
        with self._lock:
            self._entries.append((ts, message, kind))

    def render(self, limit: int = 10):
        """Render the last N entries."""
        with self._lock:
            entries = list(self._entries)[-limit:]

        if not entries:
            return

        if _RICH:
            for ts, msg, kind in entries:
                style = {
                    "info": "dim",
                    "success": "#3ddc84",
                    "warning": "#f0c060",
                    "error": "#ff5555",
                }.get(kind, "dim")
                _con.print(f"  [{style}]{ts}[/]  {msg}")
        else:
            for ts, msg, kind in entries:
                col = {"info": D, "success": GR, "warning": YW, "error": RD}.get(kind, D)
                print(f"  {col}{ts}{R}  {msg}")

    def clear(self):
        """Clear the feed."""
        with self._lock:
            self._entries.clear()


# ── Progress Stages ────────────────────────────────────────────────────────────
class ProgressStages:
    """Replace generic 'Thinking...' with meaningful stages.

    Planning → Reading Files → Searching Memory → Executing Tools →
    Calling Model → Editing Files → Done

    Each stage shows elapsed time and current operation.
    """

    # ⚠️ DERIVED from `core.progress.STAGES`, never re-declared. The web loops
    # emit stage names from that tuple and this class ticks them off in the
    # terminal; a local literal here would let the two surfaces disagree about
    # what the stages even are — the same drift CLAUDE.md pins for MODELS/MODES.
    STAGES: ClassVar[list[str]] = list(_CORE_STAGES)

    def __init__(self):
        self._current_stage: str | None = None
        self._stage_start: float = 0.0
        self._total_start: float = 0.0
        self._detail: str = ""
        self._reached: set[str] = set()
        self._lock = threading.Lock()

    def start(self):
        """Start progress tracking."""
        with self._lock:
            self._total_start = time.time()
            self._current_stage = None
            self._detail = ""
            self._reached = set()

    def set_stage(self, stage: str):
        """Move to a new stage."""
        with self._lock:
            if self._current_stage:
                self._reached.add(self._current_stage)
            self._current_stage = stage
            self._stage_start = time.time()
            self._detail = ""

    def status(self) -> tuple[str | None, float]:
        """Get current stage and elapsed time."""
        with self._lock:
            if not self._current_stage:
                return None, 0.0
            elapsed = time.time() - self._stage_start
            return self._current_stage, elapsed

    def total_elapsed(self) -> float:
        """Get total elapsed time since start."""
        with self._lock:
            if self._total_start == 0.0:
                return 0.0
            return time.time() - self._total_start

    def set_detail(self, detail: str):
        """Set the current operation shown beside the stage (e.g. a filename)."""
        with self._lock:
            self._detail = detail or ""

    def label(self) -> str:
        """One line for a spinner: `Stage · detail · 3.4s`.

        ⚠️ Built fresh on every call because a spinner reads it per frame. That
        is also why it does no I/O — see the `Spinner` contract in `runtime.py`.
        """
        with self._lock:
            stage = self._current_stage
            detail = self._detail
            started = self._stage_start
            total = self._total_start
        if not stage:
            return ""
        parts = [stage]
        if detail:
            parts.append(detail)
        base = time.time() - (started or total or time.time())
        parts.append(f"{base:.1f}s")
        return "  ·  ".join(parts)

    def print_trail(self):
        """Print the stage list with the reached ones ticked. End-of-turn recap."""
        try:
            with self._lock:
                done = list(self._reached)
                cur = self._current_stage
                total = (time.time() - self._total_start) if self._total_start else 0.0
            if not done and not cur:
                return
            if _RICH:
                for s in self.STAGES:
                    if s in done:
                        _con.print(f"  [#3ddc84]✓[/] [dim]{s}[/]")
                    elif s == cur:
                        _con.print(f"  [#f0c060]▸[/] {s}")
                _con.print(f"  [dim]total {total:.1f}s[/]")
            else:
                for s in self.STAGES:
                    if s in done:
                        print(f"  {GR}✓{R} {D}{s}{R}")
                    elif s == cur:
                        print(f"  {YW}▸{R} {s}")
                print(f"  {D}total {total:.1f}s{R}")
        except Exception:
            pass


# ── Tool Execution Card ────────────────────────────────────────────────────────
def print_tool_card(category: str, operation: str, detail: str = ""):
    """Rich tool execution card instead of generic 'Running...'.

    Filesystem
    ━━━━━━━━━━━━━━━━━━
    Reading  src/main.py

    Git
    ━━━━━━━━━━━━━━━━━━
    Generating Diff

    Playwright
    ━━━━━━━━━━━━━━━━━━
    Launching Browser
    """
    if _RICH:
        body = Text()
        body.append(f"{operation}", style="bold #60b8ff")
        if detail:
            body.append(f"\n{detail}", style="#c4c4dc")
        _con.print(Panel(
            body,
            title=f"[bold {P.ACCENT}]{category}[/]",
            border_style="#2a2a40",
            padding=(0, 1),
        ))
    else:
        print(f"\n  {P.PU}{B}{category}{R}")
        print(f"  {P.CY}{'━' * 20}{R}")
        print(f"  {P.CY}{operation}{R}")
        if detail:
            print(f"  {D}{detail}{R}")
        print()


# ── Command Execution Display ──────────────────────────────────────────────────
def print_command_result(cmd: str, stdout: str, stderr: str, exit_code: int, duration: float):
    """Rich command execution result.

    Executing
    ━━━━━━━━━━━━━━━━━━
    $ python main.py

    stdout
    ...

    stderr
    ...

    Exit Code  0
    Duration   2.1 seconds
    """
    if _RICH:
        body = Text()
        body.append(f"$ {cmd}\n\n", style="dim")

        if stdout.strip():
            body.append("stdout\n", style="bold #60b8ff")
            body.append(f"{stdout.strip()[:500]}\n\n", style="#c4c4dc")

        if stderr.strip():
            body.append("stderr\n", style="bold #ff5555")
            body.append(f"{stderr.strip()[:500]}\n\n", style="#ffb454")

        body.append("Exit Code  ", style="dim")
        exit_style = "#3ddc84" if exit_code == 0 else "#ff5555"
        body.append(f"{exit_code}\n", style=exit_style)
        body.append("Duration   ", style="dim")
        body.append(f"{duration:.1f} seconds", style="dim")

        _con.print(Panel(
            body,
            title="[bold #60b8ff]Executing[/]",
            border_style="#2a2a40",
            padding=(0, 1),
        ))
    else:
        print(f"\n  {P.CY}{B}Executing{R}")
        print(f"  {P.CY}{'━' * 20}{R}")
        print(f"  {D}$ {cmd}{R}\n")
        if stdout.strip():
            print(f"  {P.CY}stdout{R}")
            print(f"  {D}{stdout.strip()[:500]}{R}\n")
        if stderr.strip():
            print(f"  {RD}stderr{R}")
            print(f"  {YW}{stderr.strip()[:500]}{R}\n")
        col = GR if exit_code == 0 else RD
        print(f"  {D}Exit Code{R}  {col}{exit_code}{R}")
        print(f"  {D}Duration{R}   {D}{duration:.1f} seconds{R}\n")


# ── File Edit Display ──────────────────────────────────────────────────────────
# ⚠️ THE FILE-DIFF RENDERERS THAT LIVED HERE ARE GONE — ON PURPOSE, AND THE ONE
# THAT REPLACED THEM IS `agent2/cli/diffview.py`.
#
#   print_file_diff(path, added, removed, modified)
#       -> diffview.render_change(ch)      — and it prints the actual code, not
#                                            just a count of it
#   print_file_summary(files, added, removed, blocks)
#       -> diffview.render_summary(changes) — over `core.diffs.summarize()`
#
# Both were count-only panels built from numbers a caller passed in, so they could
# not show a `+`/`-` row and could not agree with the browser about what the totals
# were. `print_file_diff` had NO callers at all; `print_file_summary` had exactly
# one (`agent2cli._print_turn_recap`), which now routes through `render_summary`.
# They were internal helpers, never commands, so rule 28 does not apply — but they
# were also a THIRD declaration of "what a file's change counts look like", and
# that fact has one home now: `FileChange.to_payload`'s docstring states the rule,
# `diffview` draws it for the terminal, `script.js` draws it for the browser.
#
# Do not add a counts renderer back here. `ux.py` is chrome — status bars, feeds,
# stage trails — and a diff is not chrome.


# ── File Operations Display ────────────────────────────────────────────────────
def print_file_operations(reading: list[str] | None = None, writing: list[str] | None = None,
                          modified: list[str] | None = None, added: list[str] | None = None,
                          deleted: list[str] | None = None):
    """Show file operations cleanly.

    Reading
    ━━━━━━━━━━━━━━━━━━
    ✔ README.md
    ✔ src/main.py
    ✔ requirements.txt

    Modified
    ━━━━━━━━━━━━━━━━━━
    src/main.py

    Added
    ━━━━━━━━━━━━━━━━━━
    config.py

    Deleted
    ━━━━━━━━━━━━━━━━━━
    temp.txt
    """
    if reading:
        if _RICH:
            _con.print("\n[bold #60b8ff]Reading[/]")
            for f in reading:
                _con.print(f"  [#3ddc84]✔[/] [dim]{Path(f).name}[/]")
        else:
            print(f"\n  {P.CY}{B}Reading{R}")
            for f in reading:
                print(f"  {GR}✔{R} {D}{Path(f).name}{R}")

    if writing:
        if _RICH:
            _con.print("\n[bold #60b8ff]Writing[/]")
            for f in writing:
                _con.print(f"  [#3ddc84]✔[/] [dim]{Path(f).name}[/]")
        else:
            print(f"\n  {P.CY}{B}Writing{R}")
            for f in writing:
                print(f"  {GR}✔{R} {D}{Path(f).name}{R}")

    if modified:
        if _RICH:
            _con.print("\n[bold #f0c060]Modified[/]")
            for f in modified:
                _con.print(f"  [dim]{Path(f).name}[/]")
        else:
            print(f"\n  {YW}{B}Modified{R}")
            for f in modified:
                print(f"  {D}{Path(f).name}{R}")

    if added:
        if _RICH:
            _con.print("\n[bold #3ddc84]Added[/]")
            for f in added:
                _con.print(f"  [dim]{Path(f).name}[/]")
        else:
            print(f"\n  {GR}{B}Added{R}")
            for f in added:
                print(f"  {D}{Path(f).name}{R}")

    if deleted:
        if _RICH:
            _con.print("\n[bold #ff5555]Deleted[/]")
            for f in deleted:
                _con.print(f"  [dim]{Path(f).name}[/]")
        else:
            print(f"\n  {RD}{B}Deleted{R}")
            for f in deleted:
                print(f"  {D}{Path(f).name}{R}")


# ── Better Notifications ───────────────────────────────────────────────────────
def print_completion_notification(title: str, stats: dict[str, str | int]):
    """Rich completion notification with stats.

    ✓ Tests Passed
    ━━━━━━━━━━━━━━━━━━
    18 Tests
    91% Coverage
    4 Files Modified
    Execution Time  2.3 seconds
    """
    if _RICH:
        body = Text()
        for key, value in stats.items():
            body.append(f"{key}\n", style="dim")
            body.append(f"{value}\n", style="bold #c4c4dc")

        _con.print(Panel(
            body,
            title=f"[bold #3ddc84]✓ {title}[/]",
            border_style="#2a2a40",
            padding=(0, 1),
        ))
    else:
        print(f"\n  {GR}✓ {B}{title}{R}")
        print(f"  {P.CY}{'━' * 20}{R}")
        for key, value in stats.items():
            print(f"  {D}{key}{R}")
            print(f"  {WH}{value}{R}")
        print()


# ── Multi-command Queue ────────────────────────────────────────────────────────
class CommandQueue:
    """Visual queue showing Running/Queued/Waiting/Completed tasks.

    Running     Python Tests
    Queued      Playwright
    Waiting     Git Commit
    Completed   Formatting
    """

    def __init__(self):
        self._tasks: list[tuple[str, str]] = []  # (status, name)
        self._lock = threading.Lock()

    def add(self, name: str, status: Literal["running", "queued", "waiting", "completed", "failed"]):
        """Add or update a task."""
        with self._lock:
            # Remove existing entry for this task
            self._tasks = [(s, n) for s, n in self._tasks if n != name]
            self._tasks.append((status, name))

    def remove(self, name: str):
        """Remove a task."""
        with self._lock:
            self._tasks = [(s, n) for s, n in self._tasks if n != name]

    def pending_count(self) -> int:
        """Tasks that are not finished — what the status bar reports.

        `completed` and `failed` are both terminal, so neither counts as
        pending; a bar that kept counting finished work would only ever go up.
        """
        with self._lock:
            return sum(1 for s, _n in self._tasks
                       if s in ("running", "queued", "waiting"))

    def clear_finished(self) -> None:
        """Drop terminal entries. Called at the end of a turn."""
        with self._lock:
            self._tasks = [(s, n) for s, n in self._tasks
                           if s not in ("completed", "failed")]

    def snapshot(self) -> list[tuple[str, str]]:
        with self._lock:
            return list(self._tasks)

    def render(self):
        """Render the current queue."""
        with self._lock:
            if not self._tasks:
                return

            if _RICH:
                for status, name in self._tasks:
                    style_map = {
                        "running": "#3ddc84 bold",
                        "queued": "#f0c060",
                        "waiting": "dim",
                        "completed": "#3ddc84",
                        "failed": "#ff5555",
                    }
                    style = style_map.get(status, "dim")
                    _con.print(f"  [{style}]{status.capitalize():<12}[/] {name}")
            else:
                for status, name in self._tasks:
                    col_map = {
                        "running": GR,
                        "queued": YW,
                        "waiting": D,
                        "completed": GR,
                        "failed": RD,
                    }
                    col = col_map.get(status, D)
                    print(f"  {col}{status.capitalize():<12}{R} {name}")


# ── Approval Dialog ────────────────────────────────────────────────────────────
def approval_dialog(message: str, allow_preview: bool = False) -> Literal["yes", "no", "view"] | None:
    """Show approval dialog before destructive actions.

    Delete 17 files?
    ━━━━━━━━━━━━━━━━━━
    [Y] Yes
    [N] No
    [V] View Changes
    """
    if _RICH:
        _con.print()
        _con.print(Panel(
            message,
            title="[bold #f0c060]⚠ Confirmation Required[/]",
            border_style="#f0c060",
            padding=(0, 1),
        ))
        options = "[Y] Yes  [N] No"
        if allow_preview:
            options += "  [V] View Changes"
        _con.print(f"  [dim]{options}[/]")
    else:
        print(f"\n  {YW}⚠ Confirmation Required{R}")
        print(f"  {YW}{'━' * 20}{R}")
        print(f"  {message}")
        options = "[Y] Yes  [N] No"
        if allow_preview:
            options += "  [V] View Changes"
        print(f"  {D}{options}{R}")

    try:
        choice = input("  > ").strip().lower()
        if choice in ("y", "yes"):
            return "yes"
        elif choice in ("v", "view") and allow_preview:
            return "view"
        else:
            return "no"
    except (EOFError, KeyboardInterrupt):
        return "no"


# ── Better Error Display ───────────────────────────────────────────────────────
def print_error(category: str, error: str, cause: str = "", suggestion: str = "", allow_retry: bool = False):
    """Structured error display with cause, suggestion, and retry option.

    Filesystem
    ━━━━━━━━━━━━━━━━━━
    Permission Denied

    Cause
    Missing Permission

    Suggestion
    Grant filesystem.write

    Retry?
    """
    if _RICH:
        body = Text()
        body.append(f"{error}\n\n", style="bold #ff5555")

        if cause:
            body.append("Cause\n", style="dim")
            body.append(f"{cause}\n\n", style="#ffb454")

        if suggestion:
            body.append("Suggestion\n", style="dim")
            body.append(f"{suggestion}\n\n", style="#60b8ff")

        if allow_retry:
            body.append("Retry?", style="dim")

        _con.print(Panel(
            body,
            title=f"[bold #ff5555]{category}[/]",
            border_style="#ff5555",
            padding=(0, 1),
        ))
    else:
        print(f"\n  {RD}{B}{category}{R}")
        print(f"  {RD}{'━' * 20}{R}")
        print(f"  {RD}{error}{R}\n")
        if cause:
            print(f"  {D}Cause{R}")
            print(f"  {YW}{cause}{R}\n")
        if suggestion:
            print(f"  {D}Suggestion{R}")
            print(f"  {P.CY}{suggestion}{R}\n")
        if allow_retry:
            print(f"  {D}Retry?{R}")


# Global instances
activity_feed = ActivityFeed()
progress_stages = ProgressStages()
command_queue = CommandQueue()
