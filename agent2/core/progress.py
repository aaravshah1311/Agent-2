# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/core/progress.py
───────────────────────
Turn progress reporting — the stage trail, the task queue, and the activity
feed — shared by BOTH web agent loops.

Why this lives in `core/` rather than in each loop:

  `agent.py` (Gemini) and `llm/provider_agent.py` (custom providers) are two
  copies of the same turn structure, and the repo has already been bitten by
  echo paths drifting apart. Building the `agent_stage` / `task_queue` /
  `activity` payloads inline would make that four copies (the CLI has two more).
  One reporter, two callers, and the wire format cannot disagree.

Why it takes an `emit` callable instead of importing socketio:

  `core/` is the shared engine and stays stdlib-only — no Flask, no Rich, no
  prompt_toolkit — so the CLI can import it too. The caller passes a function;
  this module never knows what a room is.

⚠️ **Every public method swallows its own errors.** Progress is presentation.
A turn that fails because its *progress bar* raised is strictly worse than a
turn with no progress bar — same "degrades to doing nothing" contract PIL runs
under. Nothing here is on a path a result depends on.
"""

import threading
import time

# The stage trail, in order. THE single declaration — `cli/ux.py:ProgressStages`
# imports this rather than keeping a literal twin, so a stage added here appears
# in the terminal and the browser at once instead of only where it was typed.
STAGES: tuple[str, ...] = (
    "Planning",
    "Reading Files",
    "Searching Memory",
    "Executing Tools",
    "Calling Model",
    "Editing Files",
    "Done",
)

# Terminal task states. A task in one of these is finished and stops counting as
# pending — mirrors `cli/ux.py:CommandQueue.pending_count`.
_TERMINAL = ("completed", "failed")

# Which stage a tool call belongs to. Names not listed fall through to
# "Executing Tools", which is the honest generic answer — inventing a stage per
# tool would make the trail a tool log instead of a progress indicator.
_TOOL_STAGES = {
    "read_file": "Reading Files",
    "list_dir": "Reading Files",
    "grep_search": "Reading Files",
    "scan_project": "Reading Files",
    "detect_file": "Reading Files",
    "file_capabilities": "Reading Files",
    "search_workspace": "Reading Files",
    "save_memory": "Searching Memory",
    "write_file": "Editing Files",
    "multi_edit_files": "Editing Files",
    "delete_file": "Editing Files",
    "convert_file": "Editing Files",
}


def stage_for_tool(tool_name: str) -> str:
    """Map a tool name onto a stage in `STAGES`.

    Shared so the two web loops classify identically — `provider_agent.py`
    mirrors `agent.py` exactly, and a second copy of this table is precisely how
    the surfaces start disagreeing about what the agent is doing.
    """
    return _TOOL_STAGES.get(tool_name, "Executing Tools")


class TurnProgress:
    """Per-turn progress reporter for one browser session.

    Deliberately NOT a module-level singleton, unlike the CLI's `ux.activity_feed`
    — the CLI has exactly one user at one terminal, the web server has N sockets
    in one process. A shared instance would show tab A's stages inside tab B.
    """

    def __init__(self, emit, room: str, chat_id: str = ""):
        """*emit* is called as ``emit(event_name, payload, room)``."""
        self._emit = emit
        self._room = room
        self._chat_id = chat_id
        self._start = time.time()
        self._stage_start = self._start
        self._stage = ""
        self._tasks: list[tuple[str, str]] = []   # (status, name)
        # THIS turn's file changes, for the item-10 recap. Deliberately not
        # `diffs.store`, which is the whole SESSION (what the Ctrl+B viewer and
        # the web diff panel browse) — summarizing that would re-report every
        # file touched since launch on every single turn.
        self._changes: list = []
        self._lock = threading.Lock()

    # ── internals ─────────────────────────────────────────────────────────────

    def _send(self, event: str, payload: dict) -> None:
        try:
            payload["chat_id"] = self._chat_id
            self._emit(event, payload, self._room)
        except Exception:
            pass

    # ── item 8: the stage trail ───────────────────────────────────────────────

    def stage(self, name: str, detail: str = "") -> None:
        """Move to *name*, optionally showing the current operation beside it.

        `elapsed` is time in THIS stage, not the whole turn — the browser shows
        it next to the stage name, where a total would read as though a single
        step had been running for the entire turn.
        """
        try:
            with self._lock:
                if name != self._stage:
                    self._stage_start = time.time()
                self._stage = name
                elapsed = time.time() - self._stage_start
        except Exception:
            return
        self._send("agent_stage", {"stage": name, "detail": detail,
                                   "elapsed": round(elapsed, 2)})

    # ── item 13: the activity feed ────────────────────────────────────────────

    def activity(self, message: str, kind: str = "info") -> None:
        """Append one timestamped line to the feed.

        ⚠️ No timestamp is sent. The browser stamps it with `ts()` on arrival,
        which is right: the UI is reachable over the LAN, so the server's clock
        may not be the clock the person reading the line is sitting next to.
        """
        self._send("activity", {"message": message, "kind": kind})

    # ── item 7: the task queue ────────────────────────────────────────────────

    def task(self, name: str, status: str) -> None:
        """Add or move a task. Re-adding an existing name updates it in place."""
        try:
            with self._lock:
                self._tasks = [(s, n) for s, n in self._tasks if n != name]
                self._tasks.append((status, name))
                snapshot = list(self._tasks)
        except Exception:
            return
        self._send("task_queue",
                   {"tasks": [{"status": s, "name": n} for s, n in snapshot]})

    def clear_finished(self) -> None:
        """Drop terminal entries and re-publish. Called at end of turn.

        The queue is transient state pinned in the topbar, so leaving completed
        rows there would grow a permanent strip of stale work.
        """
        try:
            with self._lock:
                self._tasks = [(s, n) for s, n in self._tasks if s not in _TERMINAL]
                snapshot = list(self._tasks)
        except Exception:
            return
        self._send("task_queue",
                   {"tasks": [{"status": s, "name": n} for s, n in snapshot]})

    # ── item 10: the end-of-turn recap ────────────────────────────────────────

    def add_changes(self, changes: list) -> None:
        """Accumulate this turn's file changes for the recap."""
        try:
            with self._lock:
                self._changes.extend(changes or [])
        except Exception:
            pass

    def file_summary(self, changes: list | None = None) -> None:
        """Emit the file-change recap — but only when files actually changed.

        ⚠️ Gated on `files > 0` for the same reason the CLI's recap is: printing
        "0 files changed" after a plain question is the scrollback flooding item
        20 rules out. The summary itself comes from `core.diffs.summarize`, the
        one implementation both surfaces count with, so the browser and the
        terminal can never report different numbers for the same turn.
        """
        try:
            if changes is None:
                with self._lock:
                    changes = list(self._changes)
            from agent2.core import diffs
            summary = diffs.summarize(changes)
            if not summary.get("files"):
                return
        except Exception:
            return
        self._send("chat_file_summary", summary)

    def total_elapsed(self) -> float:
        return time.time() - self._start
