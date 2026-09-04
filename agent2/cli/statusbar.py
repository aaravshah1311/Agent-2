# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/cli/statusbar.py
───────────────────────
The persistent bottom status bar and the global keyboard shortcuts, both bound
onto the one `PromptSession` the REPL runs on.

⚠️ THIS IS THE `bottom_toolbar`, NOT A FULL-SCREEN LAYOUT.
The chosen architecture is "inline + bottom toolbar": scrollback still receives
ordinary prints, so piping, redirecting and the no-prompt_toolkit fallback keep
working. A full-screen TUI would own the whole terminal and take all three away.
prompt_toolkit redraws this line itself; nothing here schedules a repaint.

⚠️ THE TOOLBAR IS ON THE RENDER PATH AND MUST BE CHEAP.
It is rebuilt on every keystroke. Anything that shells out or queries SQLite has
to be cached with a TTL, or typing goes visibly laggy — `git branch` is the
expensive one and is cached for `_GIT_TTL` seconds. A cache miss returns the
stale value and refreshes in the background rather than blocking the keystroke.

⚠️ IT MAY NEVER RAISE.
An exception in a `bottom_toolbar` callable propagates into the prompt and takes
the REPL down. Every segment is computed inside its own try/except and a failing
one is simply omitted; a total failure renders an empty bar.

⚠️ COLOURS READ THROUGH `P` ON EVERY CALL — see `agent2/cli/theme.py`.

Layer: env / theme / state / render / diffview / palette / ux → statusbar.
"""

import os
import threading
import time
from pathlib import Path

from agent2.cli.env import _PTK, KeyBindings
from agent2.cli.theme import P

# ── Git branch cache ───────────────────────────────────────────────────────────
_GIT_TTL = 8.0            # seconds; a branch changes far more slowly than a keystroke
_git_cache = {"cwd": None, "branch": "", "at": 0.0}
_git_lock = threading.Lock()
_git_busy = False


def _git_branch_now() -> str:
    """Read the branch. Called OFF the render path only.

    ⚠️ Task 20: THE `git` CALL ITSELF LIVES IN `core/gitstate.py`, NOT HERE.
    That module is THE repository reader — the Context Broker's Git source and
    `/init` read through it too — and a second `subprocess.run(["git", …])` in this
    file is exactly the drift the one-declaration rule exists to stop: the day the
    timeout, the Windows no-console flag or the "not a repo" handling changes in one
    of them, the bar and the prompt disagree about which branch you are on and
    neither can see it. The *cache* stays here on purpose: this is the only caller
    that must never block a keystroke, so it keeps its own serve-stale policy and
    calls the single-subprocess `branch_now()` rather than the three-call snapshot.
    """
    try:
        from agent2.core import gitstate
        return gitstate.branch_now()
    except Exception:
        return ""


def _refresh_git_async(cwd: str) -> None:
    """Fetch the branch on a daemon thread and store it. Never raises."""
    global _git_busy

    def _work():
        global _git_busy
        try:
            b = _git_branch_now()
            with _git_lock:
                _git_cache.update({"cwd": cwd, "branch": b, "at": time.monotonic()})
        except Exception:
            pass
        finally:
            with _git_lock:
                _git_busy = False

    with _git_lock:
        if _git_busy:
            return
        _git_busy = True
    try:
        threading.Thread(target=_work, name="a2-git-branch", daemon=True).start()
    except Exception:
        with _git_lock:
            _git_busy = False


def git_branch() -> str:
    """Current branch, cached. Returns "" outside a repo.

    ⚠️ Never blocks. On a stale entry it serves the old value and refreshes
    behind the scenes — a `git` call on a cold NFS mount can take seconds, and
    paying that per keystroke is the difference between a status bar and a
    liability.
    """
    try:
        cwd = os.getcwd()
        with _git_lock:
            fresh = (_git_cache["cwd"] == cwd
                     and (time.monotonic() - _git_cache["at"]) < _GIT_TTL)
            cached = _git_cache["branch"] if _git_cache["cwd"] == cwd else ""
        if not fresh:
            _refresh_git_async(cwd)
        return cached
    except Exception:
        return ""


def invalidate_git() -> None:
    """Drop the cached branch — call after a workspace switch.

    Clears BOTH caches: this module's serve-stale entry and `core.gitstate`'s
    snapshot for the directory. Dropping only one of them is how a workspace
    switch shows the new branch in the bar and the old one in the prompt.
    """
    with _git_lock:
        _git_cache.update({"cwd": None, "branch": "", "at": 0.0})
    try:
        from agent2.core import gitstate
        gitstate.invalidate()
    except Exception:
        pass


# ── Session context the bar reports ────────────────────────────────────────────
class _Bar:
    """What the status bar shows, updated by the REPL as it changes.

    ⚠️ ATTRIBUTES on one shared object, never module globals — the same rule as
    `theme.P` and `state.S`. A module doing `from .statusbar import model` would
    bind a snapshot and report the startup model forever, with no traceback.
    `__slots__` turns a typo into an AttributeError instead of a dead attribute.
    """

    __slots__ = ("context_msgs", "mode", "model", "provider", "tokens")

    def __init__(self):
        self.model = ""
        self.mode = ""
        self.provider = ""
        self.context_msgs = 0
        self.tokens = 0


BAR = _Bar()


def update(model: str | None = None, mode: str | None = None, provider: str | None = None,
           context_msgs: int | None = None, tokens: int | None = None) -> None:
    """Set whichever fields were passed. Never raises."""
    try:
        if model is not None:
            BAR.model = model
        if mode is not None:
            BAR.mode = mode
        if provider is not None:
            BAR.provider = provider
        if context_msgs is not None:
            BAR.context_msgs = context_msgs
        if tokens is not None:
            BAR.tokens = tokens
    except Exception:
        pass


def _pending_tasks() -> int:
    try:
        from agent2.cli import ux
        return ux.command_queue.pending_count()
    except Exception:
        return 0


def _pending_diffs() -> int:
    try:
        from agent2.core.diffs import store
        return len(store.all())
    except Exception:
        return 0


def _memory_count() -> int:
    try:
        from agent2.core import memory as _mem
        return int(_mem.count_memories())
    except Exception:
        return 0


def _token_total() -> int:
    """Tokens this session, from the shared rotator — never a second counter.

    ⚠️ `rotator.status()` returns a LIST of per-key dicts, not a wrapper object.
    Summing it here is what keeps this bar reporting the same numbers `/keys` and
    the Web key panel do; a local tally would be a fifth counter that drifts.
    """
    try:
        from agent2.llm.keys import rotator
        return sum(int(k.get("tokens") or 0) for k in (rotator.status() or []))
    except Exception:
        return int(BAR.tokens or 0)


def _human(n: int) -> str:
    try:
        n = int(n)
        if n >= 1_000_000:
            return f"{n / 1_000_000:.1f}M"
        if n >= 1_000:
            return f"{n / 1_000:.1f}k"
        return str(n)
    except Exception:
        return "0"


def _seg(out: list, label: str, value: str, style: str = "class:sb.val") -> None:
    """Append one `label value` segment, skipping empties."""
    if not value:
        return
    if out:
        out.append(("class:sb.sep", " │ "))
    out.append(("class:sb.key", f"{label} "))
    out.append((style, value))


def bottom_toolbar():
    """The persistent bar. Returns prompt_toolkit formatted text.

    Workspace · Model · Provider · Git branch · Profile · Plugins · Memory ·
    Pending tasks · Context size · Tokens — each omitted when it has nothing to
    say, so a bare setup shows a short bar instead of a row of zeros.
    """
    out: list[tuple[str, str]] = []
    try:
        try:
            ws = Path(os.getcwd()).name or os.getcwd()
        except Exception:
            ws = "?"
        _seg(out, "", ws, "class:sb.ws")

        _seg(out, "", BAR.model or "", "class:sb.model")
        _seg(out, "", BAR.mode or "", "class:sb.mode")

        if BAR.provider:
            _seg(out, "", BAR.provider, "class:sb.val")

        br = git_branch()
        if br:
            _seg(out, "⎇", br, "class:sb.git")

        # Permission profile: Agent2 has no permission system, so this reports
        # the sandbox root honestly rather than inventing a profile name.
        _seg(out, "", "sandbox", "class:sb.dim")

        mem = _memory_count()
        if mem:
            _seg(out, "mem", str(mem), "class:sb.val")

        tasks = _pending_tasks()
        if tasks:
            _seg(out, "tasks", str(tasks), "class:sb.warn")

        diffs = _pending_diffs()
        if diffs:
            _seg(out, "diffs", f"{diffs} ^B", "class:sb.ok")

        if BAR.context_msgs:
            _seg(out, "ctx", str(BAR.context_msgs), "class:sb.val")

        tok = _token_total()
        if tok:
            _seg(out, "tok", _human(tok), "class:sb.val")
    except Exception:
        return []
    return out


# ── Style for the bar + its completion menu ────────────────────────────────────
def style_dict() -> dict:
    """Style entries the prompt style must merge in. Read `P` at call time."""
    return {
        "bottom-toolbar":       "bg:#12121c #666677 noreverse",
        "bottom-toolbar.text":  "bg:#12121c #666677",
        "sb.ws":                f"bg:#12121c {P.ACCENT} bold",
        "sb.model":             "bg:#12121c #c4c4dc",
        "sb.mode":              f"bg:#12121c {P.ACCENT2}",
        "sb.git":               "bg:#12121c #f0c060",
        "sb.key":               "bg:#12121c #55556a",
        "sb.val":               "bg:#12121c #9a9ab0",
        "sb.sep":               "bg:#12121c #2a2a40",
        "sb.ok":                "bg:#12121c #3ddc84",
        "sb.warn":              "bg:#12121c #f0c060",
        "sb.dim":               "bg:#12121c #55556a",
    }


# ── Keyboard shortcuts ─────────────────────────────────────────────────────────
# ⚠️ THESE BIND ON THE PROMPT, NOT DURING A TURN.
# While the agent is working, `runtime.InputController` owns the keyboard: any
# keystroke queues a message and ESC cancels. Binding these there too would mean
# a Ctrl+P mid-turn either opens an overlay on top of streaming output or gets
# swallowed as message text — so the prompt-level bindings are the ones that
# exist, and the toolbar advertises exactly those.
def build_key_bindings(handlers: dict) -> "KeyBindings | None":
    """Global prompt shortcuts. `handlers` maps a name → a zero-arg callable.

    Recognised names: diff, palette, clear, history, tasks, cancel, actions,
    help. A name that is absent is simply not bound — the caller decides what it
    supports rather than this module assuming.

    ⚠️ Every handler runs inside `run_in_terminal`, which suspends the prompt,
    lets the callable own the terminal, and then redraws the prompt line exactly
    as it was. Calling an overlay directly from a key binding instead paints it
    over the live prompt and corrupts both.
    """
    if not _PTK or KeyBindings is None:
        return None
    try:
        from prompt_toolkit.application import run_in_terminal
    except Exception:
        return None

    kb = KeyBindings()

    def _wrap(name):
        fn = handlers.get(name)
        if not fn:
            return None

        def _run(event):
            def _call():
                try:
                    fn()
                except Exception:
                    pass
            try:
                run_in_terminal(_call)
            except Exception:
                pass
        return _run

    _MAP = {
        "c-b": "diff",        # Ctrl+B  diff viewer
        "c-p": "palette",     # Ctrl+P  command palette
        "c-t": "tasks",       # Ctrl+T  running tasks
        "f1":  "help",        # F1      help
    }
    for key, name in _MAP.items():
        fn = _wrap(name)
        if fn is not None:
            kb.add(key)(fn)

    # Ctrl+L clears the screen and REDRAWS the prompt — prompt_toolkit's own
    # binding erases the prompt line with it, which looks like a hang.
    clear = handlers.get("clear")
    if clear:
        @kb.add("c-l")
        def _(event):
            try:
                clear()
            except Exception:
                pass
            try:
                event.app.renderer.clear()
            except Exception:
                pass

    # Ctrl+R is prompt_toolkit's own reverse history search and is strictly
    # better than anything reimplemented here, so it is deliberately NOT
    # rebound — only registered as a shortcut the help screen lists.
    return kb


# What F1 / the help screen advertises. One list so the help text and the real
# bindings above cannot drift.
SHORTCUTS: list[tuple[str, str]] = [
    ("Ctrl+B", "Diff viewer for this session's file changes (at the prompt)"),
    ("Ctrl+P", "Command palette (fuzzy search every command)"),
    ("Ctrl+L", "Clear the screen (history is kept)"),
    ("Ctrl+R", "Reverse-search the input history"),
    ("Ctrl+T", "Show running and queued tasks"),
    ("Ctrl+K", "Cancel the current task (during a turn)"),
    ("Esc",    "Close an overlay · cancel the running turn"),
    ("Tab",    "Accept / cycle the autocomplete suggestion"),
    ("F1",     "This help"),
]
