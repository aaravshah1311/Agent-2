# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/cli/diffview.py
──────────────────────
Claude-Code-style file diffs for the CLI: inline rendering and the full-screen
viewer bound to Ctrl+B.

⚠️ THE ENGINE IS NOT HERE — IT IS `agent2.core.diffs`, AND THE HIGHLIGHTER IS
`agent2.core.highlight`.
This module is the CLI's *renderer*. Computation, the capture helpers, the
session store and the tokenizer are shared with the Web UI so the two surfaces
can never disagree about what changed, how many blocks changed, or what counts
as a keyword. A second implementation here would drift invisibly — each surface
looks correct on its own. The engine names are re-exported below so existing CLI
call sites keep working against one engine.

⚠️ THE DIFF IS COMPUTED BEFORE THE WRITE, NOT AFTER.
`write_file` overwrites; once it has run the old content is gone. The capture
helpers are called by the dispatch layer *ahead* of the tool. A caller that
inverts that order silently renders every write as a pure addition — see the
invariant in `core/diffs.py`.

⚠️ NOTHING HERE MAY RAISE INTO A TURN.
A diff is presentation. An unreadable file, a binary blob, a permission error, or
a decode failure degrades to "no diff shown" — never to a failed tool call. Every
public entry point is total.

⚠️ COLOURS READ THROUGH `P` ON EVERY CALL, and both a Rich and a plain-ANSI body
exist for every printer. Same rule as `render.py`; see `agent2/cli/theme.py`.

⚠️ APPROVE/REJECT IN THE VIEWER IS A *RECORD*, NOT A GATE.
The write already happened — the agent is autonomous and nothing blocks on this
review. `[A] Accept` therefore means "marked reviewed" and its own toast says
`nothing written`; the only action that touches disk is `r`'s undo, and it takes a
SECOND deliberate `r` to fire. Anything that made this a gate would put a human in
the critical path of every turn.

⚠️ OPENING THE VIEWER WRITES NOTHING, AND `[E] Edit` DOES NOT FEED THE STORE.
`store` is "what the agent changed"; recording a human's own edit there would make
`revert_change`'s reconstructed before-side wrong, so an `E` round trip is
deliberately invisible to it. The whole-file view (`f`) re-reads disk, so the user
still sees their edit — the unified diff keeps showing the agent's change, which is
the thing the viewer exists to show.

Layer: core.diffs / core.highlight / env / theme / render → diffview.
"""

import os
import shlex
import subprocess
import time
from pathlib import Path

from agent2.core.diffs import (          # THE shared engine — never re-implement
    MAX_DIFF_LINES,
    FileChange,
    capture_delete,
    capture_edits,
    capture_for,
    capture_write,
    compute_change,
    store,
    summarize,
)
from agent2.core.highlight import tokenize

from agent2.cli.env import (
    Application,
    FormattedTextControl,
    HSplit,
    KeyBindings,
    Layout,
    Style,
    Window,
    _con,
    _PTK,
    _RICH,
)
from agent2.cli.render import _short_path, status_line, tw
from agent2.cli.theme import B, D, GR, P, R, RD, WH, YW

# Re-exported so `from agent2.cli import diffview` gives a CLI call site the
# whole surface — engine + renderers — without it needing to know the split.
__all__ = [
    "MAX_DIFF_LINES",
    "FileChange",
    "capture_delete",
    "capture_edits",
    "capture_for",
    "capture_write",
    "compute_change",
    "open_in_editor",
    "open_viewer",
    "render_change",
    "render_summary",
    "store",
    "summarize",
]


# ── Syntax colours ─────────────────────────────────────────────────────────────
# ⚠️ These are FIXED, not themed. A theme repaints the CLI's chrome; recolouring
# a string literal per theme would make the same file look like a different
# language between sessions. Matched to the palette every editor ships with.
_SYNTAX_HEX = {
    "keyword":  "#c678dd",
    "string":   "#98c379",
    "comment":  "#5c6370",
    "number":   "#d19a66",
    "function": "#61afef",
    "type":     "#e5c07b",
    "operator": "#56b6c2",
    "text":     "",          # inherit the row's add/del/ctx colour
}
_SYNTAX_ANSI = {
    "keyword":  "\033[38;5;176m",
    "string":   "\033[38;5;114m",
    "comment":  "\033[38;5;242m",
    "number":   "\033[38;5;173m",
    "function": "\033[38;5;75m",
    "type":     "\033[38;5;180m",
    "operator": "\033[38;5;73m",
    "text":     "",
}
# Row colours: the four the spec names — Green added, Red removed, Yellow
# modified (the `@@` hunk marker), Grey context.
_ROW_HEX = {"add": "#3ddc84", "del": "#ff5555", "hunk": "#f0c060", "ctx": "#7a7a8c"}
_ROW_ANSI = {"add": GR, "del": RD, "hunk": YW, "ctx": D}


def _rich_line(tag: str, text: str, lang: str) -> str:
    """One diff row as a Rich markup string, syntax-highlighted.

    Syntax colour applies to added and context rows. A REMOVED row stays flat
    red on purpose: the eye needs `-` lines to read as one deleted mass, and
    tokenising them makes a deletion look like ordinary code sitting next to its
    replacement.
    """
    row = _ROW_HEX.get(tag, "#c4c4dc")
    if tag in ("del", "hunk"):
        return f"[{row}]{_esc(text)}[/]"
    parts = []
    for kind, chunk in tokenize(text, lang):
        hexv = _SYNTAX_HEX.get(kind) or row
        parts.append(f"[{hexv}]{_esc(chunk)}[/]")
    return "".join(parts) or f"[{row}]{_esc(text)}[/]"


def _ansi_line(tag: str, text: str, lang: str, bg: str = "") -> str:
    """One diff row as plain ANSI, syntax-highlighted. The no-Rich branch.

    ⚠️ *bg* is re-applied after EVERY token, and that is not redundant. Plain
    ANSI has no style stack: the `\\033[0m` that ends each token resets the
    background as well as the foreground, so a banded row would shatter into
    coloured fragments separated by bare terminal background — the band is the
    thing the eye actually uses to find the change. Rich nests styles properly
    and needs no equivalent, which is exactly why this branch has to be checked
    on its own rather than assumed to match.
    """
    row = _ROW_ANSI.get(tag, "")
    if tag in ("del", "hunk"):
        return f"{bg}{row}{text}{R}{bg}"
    parts = []
    for kind, chunk in tokenize(text, lang):
        col = _SYNTAX_ANSI.get(kind) or row
        parts.append(f"{bg}{col}{chunk}{R}")
    return ("".join(parts) or f"{bg}{row}{text}{R}") + bg


def _esc(s: str) -> str:
    """Rich treats [..] as markup — a diff of a list literal must not vanish."""
    return s.replace("[", "\\[")


# ── Inline rendering ───────────────────────────────────────────────────────────
# The header verb. `Update` for an existing file rather than `Editing`, matching
# the Claude Code shape the user asked for: a past-tense-ish label plus an
# add/remove count line, so the shape of the change is legible before any row.
_KIND_VERB = {"create": "Create", "modify": "Update", "delete": "Delete"}

# Full-row backgrounds. The screenshot's defining feature is that a +/- row is a
# filled BAND across the width, not just coloured text — the eye finds the change
# without reading it. 52/22/16 are dark enough that white-ish syntax colours stay
# legible on top, which flat #003300-style fills do not manage.
_BG_ADD_RICH = "on #16301f"
_BG_DEL_RICH = "on #3a1519"
_BG_ADD_ANSI = "\033[48;2;22;48;31m"
_BG_DEL_ANSI = "\033[48;2;58;21;25m"


def _gutter(num: int | None, width: int) -> str:
    """The line-number column. Blank (not '0', not '-') when the number is unknown.

    ⚠️ `numbered_lines()` returns None for a `@@` header and for any hunk whose
    header would not parse. A blank gutter reads as "unknown"; a fabricated
    number would send someone editing the wrong line.
    """
    return (str(num) if num is not None else "").rjust(width)


def render_change(ch: FileChange, context_lines: bool = True, preview: bool = False) -> None:
    """Print one file's diff, git-style. Never raises.

    *preview* limits output: for a create, show 3 lines; for modify, show 10 lines
    around the first change. Ctrl+B opens the full viewer.
    """
    try:
        _render_change(ch, context_lines, preview)
    except Exception:
        pass


def _render_change(ch: FileChange, context_lines: bool, preview: bool) -> None:
    verb = _KIND_VERB.get(ch.kind, "Editing")
    rel = _short_path(ch.path)

    if ch.binary:
        if _RICH:
            _con.print(f"\n  [bold {P.ACCENT2}]●[/] [bold]{verb}[/]"
                       f"([bold {P.ACCENT}]{_esc(rel)}[/])  [dim](binary)[/]")
        else:
            print(f"\n  {P.CY}{B}●{R} {WH}{B}{verb}{R}({P.CY}{rel}{R})  {D}(binary){R}")
        return

    # Header, Claude-Code shape:
    #
    #   ● Update(agent2\server\routes.py)
    #     └ Added 8 lines, removed 6 lines, ~6 modified
    #
    # The bullet + `Verb(path)` on one line, the counts as a tree child beneath.
    # Reads as one statement rather than a label and a separate stats row.
    #
    # ⚠️ WHICH SIDES APPEAR IS `FileChange.to_payload`'s CONTRACT, NOT THIS
    # RENDERER'S PREFERENCE. A zero side is suppressed and 1 is singular, because
    # "removed 0 lines" is noise on a pure addition and "Added 1 lines" reads as a
    # bug in the diff itself. `script.js:renderFileDiff` implements the same rule
    # for the browser and `test_diffs.py` pins that the two still agree — the rule
    # is decided once, in the payload module, and drawn twice.
    #
    # ⚠️ `~modified` IS A COUNT, NOT A ROW TAG. "Yellow = modifications" is this
    # paired total (`min(added, removed)`) plus the `@@` marker; there is no fifth
    # tag, because two WRITERS depend on the tag set being exactly `TAGS` — see
    # `revert_change` below and `to_patch`.
    #
    # Colours come from `_ROW_HEX`/`_ROW_ANSI` rather than repeated literals: green
    # means added in this module in exactly one place.
    counts: list[tuple[str, str]] = []
    if ch.added:
        counts.append(("add", f"Added {ch.added} line{'' if ch.added == 1 else 's'}"))
    if ch.removed:
        counts.append(("del", f"removed {ch.removed} line{'' if ch.removed == 1 else 's'}"))
    if ch.modified:
        counts.append(("hunk", f"~{ch.modified} modified"))

    if _RICH:
        _con.print(f"\n  [bold {P.ACCENT2}]●[/] [bold]{verb}[/]([bold {P.ACCENT}]{_esc(rel)}[/])")
        if counts:
            _con.print("    [dim]└[/] "
                       + ", ".join(f"[{_ROW_HEX[k]}]{t}[/]" for k, t in counts))
    else:
        print(f"\n  {P.CY}{B}●{R} {WH}{B}{verb}{R}({P.CY}{rel}{R})")
        if counts:
            print(f"    {D}└{R} "
                  + ", ".join(f"{_ROW_ANSI[k]}{t}{R}" for k, t in counts))

    if not ch.lines:
        if ch.kind == "create":
            note = "new file"
        elif ch.kind == "delete":
            note = "removed"
        else:
            note = "no textual change"
        if _RICH:
            _con.print(f"  [dim]{note}[/]")
        else:
            print(f"  {D}{note}{R}")
        return

    rows = ch.numbered_lines()
    lang = ch.lang

    hidden = 0
    if preview and rows:
        # ⚠️ The window is decided by the ENGINE, not here — `core.diffs` owns it
        # so the browser's "Click to expand" collapses to exactly these rows. A
        # local copy of the arithmetic is how the two surfaces would start showing
        # different amounts of the same change.
        #
        # ⚠️ The header counts are NOT recomputed from this window — they come from
        # `ch.added`/`ch.removed`, which are always the whole file. A preview that
        # also shrank its own totals would under-report the change, which is the
        # one thing a review surface must never do.
        start, count, hidden = ch.preview_window()
        rows = rows[start:start + count]

    # Gutter width from the largest number actually present, so a 12-line file
    # gets a 2-wide column instead of a fixed 5-wide one padded with air.
    nums = [n for _t, _x, n in rows if n is not None]
    gw = max(3, len(str(max(nums)))) if nums else 3

    # The band spans the terminal, minus indent + gutter + sign. `text` is padded
    # to that width so the background colour fills the whole row rather than
    # stopping at the end of the code — that filled band is the whole point.
    total = max(40, min(tw(), 160))
    body_w = max(20, total - (2 + gw + 3))

    for tag, text, num in rows:
        text = text.rstrip("\n")[:body_w]
        g = _gutter(num, gw)

        if tag == "hunk":
            # The `@@` header keeps a blank gutter and no band: it is metadata
            # about where we are in the file, not a line of the file.
            if _RICH:
                _con.print(f"  [dim]{' ' * gw}[/] [#f0c060]{_esc(text)}[/]")
            else:
                print(f"  {' ' * gw} {YW}{text}{R}")
        elif tag == "add":
            pad = text.ljust(body_w)
            if _RICH:
                _con.print(f"  [#3ddc84 {_BG_ADD_RICH}]{g} +[/]"
                           f"[{_BG_ADD_RICH}] {_rich_line('add', pad, lang)}[/]")
            else:
                print(f"  {_BG_ADD_ANSI}{GR}{g} +{R}{_BG_ADD_ANSI} "
                      f"{_ansi_line('add', pad, lang, _BG_ADD_ANSI)}{R}")
        elif tag == "del":
            pad = text.ljust(body_w)
            if _RICH:
                _con.print(f"  [#ff5555 {_BG_DEL_RICH}]{g} -[/]"
                           f"[{_BG_DEL_RICH}] {_esc(pad)}[/]")
            else:
                print(f"  {_BG_DEL_ANSI}{RD}{g} -{R}{_BG_DEL_ANSI} {pad}{R}")
        elif context_lines:
            # Context gets the gutter but no band and no sign — it is the quiet
            # background the two coloured bands are read against.
            if _RICH:
                _con.print(f"  [dim]{g}[/]   {_rich_line('ctx', text, lang)}")
            else:
                print(f"  {D}{g}{R}   {_ansi_line('ctx', text, lang)}")

    # ── Footer ────────────────────────────────────────────────────────────────
    # Two different facts, and they must not be conflated. `hidden` means "this
    # was WINDOWED for readability, the rest is one keypress away"; `truncated`
    # means "the engine stopped computing at MAX_DIFF_LINES", so even the full
    # viewer has no more to show. Printing the first when the second is true
    # would promise Ctrl+B content that does not exist.
    if hidden > 0:
        msg = f"… {hidden} more line{'s' if hidden != 1 else ''} · Ctrl+B for the full file"
        if _RICH:
            _con.print(f"  [dim]{msg}[/]")
        else:
            print(f"  {D}{msg}{R}")
    if ch.truncated:
        msg = f"… diff truncated at {MAX_DIFF_LINES} lines — Ctrl+B for the full view"
        if _RICH:
            _con.print(f"  [dim]{msg}[/]")
        else:
            print(f"  {D}{msg}{R}")


def render_summary(changes: list[FileChange]) -> None:
    """The one-line roll-up printed after a group of changes.

        ✓ 4 files changed  +89 lines  -12 lines  7 modified blocks

    ⚠️ THE TOTAL IS `core.diffs.summarize()`'s, NOT THIS FUNCTION'S.
    This module already imports it, and the Web UI's summary bar already goes
    through it (`core/progress.py`). A local `sum(c.added ...)` here is a second
    declaration of "what a set of changes totals to" inside the module that holds
    the first one — and it would drift the way every second copy in this codebase
    drifts: each surface still looks right on its own, so the disagreement is only
    visible to someone comparing two screens.

    Binary files are dropped BEFORE aggregating: they have no line counts, so
    including them would inflate `files` past the number of diffs actually shown.
    """
    try:
        real = [c for c in changes if c and not c.binary]
        if not real:
            return
        agg = summarize(real)
        files, added, removed = agg["files"], agg["added"], agg["removed"]
        blocks = agg["blocks"]

        if _RICH:
            _con.print(
                f"\n  [bold #3ddc84]✓[/] [bold]{files}[/] file{'s' if files != 1 else ''} changed  "
                f"[#3ddc84]+{added} lines[/]  [#ff5555]-{removed} lines[/]  "
                f"[#f0c060]{blocks} modified block{'s' if blocks != 1 else ''}[/]"
                f"  [dim]· Ctrl+B to review[/]"
            )
        else:
            print(
                f"\n  {GR}✓{R} {WH}{files}{R} file{'s' if files != 1 else ''} changed  "
                f"{GR}+{added} lines{R}  {RD}-{removed} lines{R}  "
                f"{YW}{blocks} modified block{'s' if blocks != 1 else ''}{R}"
                f"  {D}· Ctrl+B to review{R}"
            )
    except Exception:
        pass


# ── Patch export ───────────────────────────────────────────────────────────────
def to_patch(changes: list[FileChange]) -> str:
    """Render changes as a unified-diff patch file.

    Reconstructed from `FileChange.lines`, which is why a TRUNCATED change is
    marked in the output: a patch that silently stopped at 400 lines would apply
    cleanly and leave the file half-migrated, which is worse than not offering it.
    """
    out: list[str] = []
    for ch in changes or []:
        if not ch or ch.binary:
            out.append(f"# {ch.path if ch else '?'}: binary — no textual patch")
            continue
        a = "/dev/null" if ch.kind == "create" else f"a/{ch.path}"
        b = "/dev/null" if ch.kind == "delete" else f"b/{ch.path}"
        out.append(f"diff --git a/{ch.path} b/{ch.path}")
        out.append(f"--- {a}")
        out.append(f"+++ {b}")
        for tag, text in ch.lines:
            if tag == "hunk":
                out.append(text)
            elif tag == "add":
                out.append("+" + text)
            elif tag == "del":
                out.append("-" + text)
            else:
                out.append(" " + text)
        if ch.truncated:
            out.append(f"# ⚠ TRUNCATED at {MAX_DIFF_LINES} lines — "
                       f"this patch is INCOMPLETE and will not apply cleanly")
    return "\n".join(out) + ("\n" if out else "")


def save_patch(changes: list[FileChange], path: str | None = None) -> str | None:
    """Write a .patch next to the workspace. Returns the path, or None on failure."""
    try:
        text = to_patch(changes)
        if not text.strip():
            return None
        target = Path(path) if path else Path.cwd() / f"agent2-{int(time.time())}.patch"
        target.write_text(text, encoding="utf-8")
        return str(target)
    except Exception:
        return None


def copy_to_clipboard(text: str) -> bool:
    """Best-effort clipboard copy. False when no mechanism is available.

    Tries pyperclip, then the platform CLI. Deliberately silent on failure —
    a headless box or an SSH session has no clipboard, and that is not an error
    worth interrupting a review for.
    """
    try:
        import pyperclip
        pyperclip.copy(text)
        return True
    except Exception:
        pass
    try:
        import subprocess
        import sys
        if sys.platform == "win32":
            cmd = ["clip"]
        elif sys.platform == "darwin":
            cmd = ["pbcopy"]
        else:
            cmd = ["xclip", "-selection", "clipboard"]
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        p.communicate(text.encode("utf-8", errors="replace"))
        return p.returncode == 0
    except Exception:
        return False


def full_rows(ch: FileChange) -> list[tuple[str, str, int | None]]:
    """The file's CURRENT content as viewer rows, with changed lines still marked.

    This is what Ctrl+B is for: the inline echo shows a window of the change, this
    shows the whole file the change produced.

    ⚠️ Read from DISK, not reconstructed from the diff. `ctx + add` rebuilds the
    after-side only when the diff is complete — a diff truncated at
    `MAX_DIFF_LINES` would silently render a partial file as if it were the whole
    thing, which is the one lie a review surface cannot tell. Disk is also simply
    the truth: it is the file the user now has.

    A DELETE has no file to read, so it falls back to the before-side from the
    diff (`ctx + del`) — the only content that still exists anywhere.

    Rows come back tagged `add` for lines the change introduced and `ctx` for the
    rest, so the whole-file view still shows WHERE the change landed instead of
    becoming an undifferentiated listing.
    """
    try:
        if not ch or ch.binary:
            return []
        if ch.kind == "delete":
            return [("del", t, i + 1)
                    for i, (tag, t) in enumerate(ch.lines) if tag in ("ctx", "del")]

        added = {n for tag, _t, n in ch.numbered_lines() if tag == "add" and n is not None}
        text = Path(ch.path).read_text(encoding="utf-8", errors="replace")
        body = text.split("\n")
        # A trailing newline yields a final empty element that is not a line.
        if body and body[-1] == "":
            body.pop()
        return [("add" if i + 1 in added else "ctx", line, i + 1)
                for i, line in enumerate(body)]
    except Exception:
        return []


def revert_change(ch: FileChange) -> bool:
    """Undo one change by restoring the file's pre-change content.

    Reconstructed from the diff: context + deleted rows ARE the "before" side.
    Refuses on a truncated diff, because the reconstruction would be partial and
    writing it would DESTROY the parts of the file the diff never captured.
    """
    try:
        if not ch or ch.binary or ch.truncated:
            return False
        if ch.kind == "create":
            # It didn't exist before; undoing means removing it.
            p = Path(ch.path)
            if p.exists():
                p.unlink()
            return True
        before = [t for tag, t in ch.lines if tag in ("del", "ctx")]
        if not before and ch.kind != "delete":
            return False
        Path(ch.path).write_text("\n".join(before) + "\n", encoding="utf-8")
        return True
    except Exception:
        return False


# ── Full-screen viewer (Ctrl+B) ────────────────────────────────────────────────
# ⚠️ `store` IS PROCESS-LOCAL AND IS **NOT** SHARED WITH THE WEB UI.
# This comment used to claim the opposite — "the SAME instance the Web UI records
# into, so Ctrl+B in a dual-mode session shows changes made from the browser too".
# It was false: `agent2dual.py` runs the CLI as a CHILD PROCESS, so the two halves
# hold two disjoint `DiffStore`s and neither has ever seen the other's changes. The
# lie is the dangerous kind — a user who believes it reads an empty viewer as
# "nothing changed" while the browser half is mid-edit. "The whole session" here
# means "this process's session"; see `DiffStore`'s docstring for why durability is
# deliberately not offered.


def open_in_editor(path: str) -> bool:
    """Hand one file to the user's editor and block until they close it.

    THE one declaration of "which editor a human gets": `$VISUAL`, then `$EDITOR`,
    then the platform default (`notepad` on Windows, `vi` elsewhere). A second copy
    elsewhere would disagree the moment a user sets only one of the two variables.

    ⚠️ CALL THIS ONLY AFTER THE VIEWER'S `Application` HAS RETURNED. prompt_toolkit
    leaves the alternate screen and shows the cursor inside `renderer.reset()` on
    its way out of `run()` (verified in the vendored 3.0.53), so a child launched
    afterwards inherits a clean, normal terminal that it owns alone. Launched from
    inside a key binding it would paint into the viewer's alternate screen and
    fight the app for the raw-mode console.

    Total, like everything else in this module: any failure is `False`, never an
    exception into the viewer.
    """
    try:
        if not path:
            return False
        cmd = os.environ.get("VISUAL") or os.environ.get("EDITOR") or ""
        if cmd:
            # An editor variable may carry flags — `code -w`, `subl -n -w`.
            argv = shlex.split(cmd, posix=(os.name != "nt"))
            if os.name == "nt":
                # posix=False keeps the quotes it split on; subprocess wants the
                # bare path, and a Windows editor path routinely has spaces in it.
                argv = [a[1:-1] if len(a) > 1 and a[0] == a[-1] == '"' else a
                        for a in argv]
        else:
            argv = ["notepad"] if os.name == "nt" else ["vi"]
        if not argv:
            return False
        return subprocess.call([*argv, path]) == 0
    except Exception:
        return False


def open_viewer(changes: list[FileChange] | None = None,
                dropped: int | None = None) -> None:
    """Full-screen diff review. Degrades to an inline dump without prompt_toolkit.

    Keys: ↑↓/PgUp/PgDn scroll · ←→ file · f full · d/u unified · s side-by-side ·
          c context · e collapse · / search · n/p match · y copy · w patch ·
          a accept · x reject · r reject→revert · E edit · q close.

    ⚠️ OPENING THIS WRITES NOTHING. Every code path from here to the first redraw
    is a read: `store.all()` and `Path.read_text` in `full_rows`. There is no
    approval gate in the diff subsystem at all, so `a` is a review MARK; the only
    disk-touching action is `r`'s undo and it needs a second deliberate keypress.
    A viewer that could commit or revert merely by opening would be a gate nobody
    designed and nobody could see.
    """
    from_store = changes is None
    changes = changes if changes is not None else store.all()
    changes = [c for c in changes if c]

    if dropped is None:
        # ⚠️ Only `DiffStore` knows what the cap ate, and the number only describes
        # the store. A caller that passed its own list is showing something else,
        # and blaming this session's cap for THAT list's shape would be a second,
        # wrong claim about completeness. `hidden` / `truncated` / `dropped` are
        # three different facts — see `core/diffs.py`.
        dropped = store.dropped if from_store else 0

    if not changes:
        msg = "No file changes in this session yet."
        if dropped:
            msg += f"  ({dropped} earlier change(s) dropped — session cap)"
        if _RICH:
            _con.print(f"  [dim]{msg}[/]")
        else:
            print(f"  {D}{msg}{R}")
        return

    if not _PTK or Application is None:
        # Fallback: print every diff inline. Not a viewer, but the content is
        # what matters and this is what a minimal install gets.
        for ch in changes:
            render_change(ch)
        render_summary(changes)
        if dropped:
            status_line(f"{dropped} earlier change(s) dropped — session cap "
                        f"{store.MAX_CHANGES}", "warning")
        return

    # `E` closes the app so the editor owns the terminal, then we re-enter. The
    # loop terminates because every extra round costs the user an `E` keypress:
    # `_run_viewer` blocks in `app.run()` and every other exit path leaves the loop.
    #
    # ⚠️ `edited` and `verdict` live HERE, not in `_run_viewer`. Each re-entry
    # builds fresh viewer state, so review marks made before an `E` would silently
    # vanish while the ✓/✗ badges disappeared from the header — the user would read
    # that as "my marks were rejected" rather than "the editor round-tripped".
    edited: set[int] = set()          # files the user opened in an editor from here
    verdict: dict[int, str] = {}      # file index → "approved" | "rejected"
    note = ""
    while True:
        try:
            action = _run_viewer(changes, dropped=dropped, edited=edited,
                                 verdict=verdict, note=note)
        except Exception:
            for ch in changes:
                render_change(ch)
            render_summary(changes)
            return
        if not action or action[0] != "edit":
            break
        idx = action[1]
        if not isinstance(idx, int) or not 0 <= idx < len(changes):
            break
        if open_in_editor(changes[idx].path):
            # ⚠️ Deliberately NOT re-captured into `store`. See the module header:
            # the store is the agent's changes, and a human edit recorded there
            # would corrupt `revert_change`'s before-side. `f` re-reads disk, so
            # the edit is visible without being claimed as the agent's.
            edited.add(idx)
            note = "edited in your editor — press f to see the file on disk"
        else:
            note = "no editor available — set $EDITOR or $VISUAL"

    # Report the review outcome to scrollback — the viewer erased itself, so this
    # line is the only trace that a review happened. ⚠️ It says "marks" on purpose:
    # the verdicts are consumed by nothing, and a roll-up that read like a decision
    # would imply the rejected changes had been undone.
    approved = sum(1 for v in verdict.values() if v == "approved")
    rejected = sum(1 for v in verdict.values() if v == "rejected")
    if approved or rejected:
        status_line(f"Review marks: {approved} accepted, {rejected} rejected "
                    f"({len(changes)} change(s) total) — the review wrote nothing",
                    "info")


def _run_viewer(changes: list[FileChange], dropped: int = 0,
                edited: set[int] | None = None,
                verdict: dict[int, str] | None = None,
                note: str = "") -> tuple[str, int] | None:
    """Draw and drive the viewer. Returns an action for `open_viewer` to perform.

    The only action is `("edit", file_index)`, and it exists because an editor
    cannot be launched from inside a key binding — see `open_in_editor`. `edited`
    and `verdict` are owned by the caller so they survive that round trip.
    """
    edited = edited if edited is not None else set()
    verdict = verdict if verdict is not None else {}
    st = {
        "file": 0,
        "scroll": 0,
        # ⚠️ Opens on "full", not "unified". The inline echo already showed the
        # windowed diff — Ctrl+B exists to answer "now show me the whole file",
        # so landing on a bigger copy of what was just printed wastes the keypress.
        # u/s/d switch to the diff views.
        "mode": "full",         # full | unified | side
        "context": True,
        "collapsed": set(),     # file indexes rendered as header-only
        "search": "",
        "searching": False,
        "matches": [],
        "match_idx": 0,
        "verdict": verdict,     # file index → "approved" | "rejected" (caller-owned)
        "toast": note,          # transient one-line status inside the viewer
        # ⚠️ The revert confirmation. `armed` holds the FILE INDEX a second `r`
        # would revert, never a bare True — arming on one file and firing on
        # another is exactly the accident this guards. `_was_armed` is what
        # `_typing` just cleared, because the `r` handler is itself a `_typing`
        # caller and has to see what its own call disarmed.
        "armed": None,
        "_was_armed": None,
        # Set by `E` and returned to `open_viewer`; an editor cannot be launched
        # from inside a key binding.
        "action": None,
    }

    def _visible_rows() -> int:
        # ⚠️ The reserve tracks the header/footer chrome exactly. The `dropped`
        # warning adds a header line, so it has to be paid for here too — a body
        # one row too tall pushes the footer off screen, and the footer is the
        # only place the keys are documented.
        chrome = 7 + (1 if dropped else 0)
        try:
            from prompt_toolkit.application.current import get_app
            return max(6, get_app().output.get_size().rows - chrome)
        except Exception:
            return 24

    def _width() -> int:
        try:
            from prompt_toolkit.application.current import get_app
            return max(40, get_app().output.get_size().columns - 6)
        except Exception:
            return 76

    def _lines_for(ch: FileChange) -> list[tuple[str, str, int | None]]:
        """The rows the current mode shows: (tag, text, gutter number).

        ⚠️ ONE row source for every mode, scroll bound, and search. Giving the
        full view its own list would mean `_max_scroll`, `_recompute_matches` and
        the renderers each had to ask which mode they were in — and the one that
        forgot would scroll past the end or highlight the wrong row.

        `context: False` is a diff-mode filter only. Hiding context in the full
        view would leave the changed lines with the whole file cut out from
        between them, which is just the diff again with the numbers of a file
        that no longer matches.
        """
        if st["mode"] == "full":
            return full_rows(ch)
        rows = ch.numbered_lines()
        if st["context"]:
            return rows
        return [(t, x, n) for t, x, n in rows if t != "ctx"]

    def _recompute_matches():
        q = st["search"].lower()
        st["matches"] = []
        if not q:
            return
        for i, (_tag, text, _n) in enumerate(_lines_for(changes[st["file"]])):
            if q in text.lower():
                st["matches"].append(i)
        st["match_idx"] = 0
        if st["matches"]:
            st["scroll"] = max(0, st["matches"][0] - 3)

    def _note(msg: str):
        st["toast"] = msg

    # ── Syntax-highlighted row emitters ────────────────────────────────────────
    def _emit_tokens(out, tag, text, lang, base_style):
        """Append one row's tokens with per-token styles.

        Deleted rows are emitted flat (see `_rich_line`); the reason is the same
        here — a `-` block has to read as one removed mass.
        """
        if tag in ("del", "hunk"):
            out.append((base_style, text))
            return
        for kind, chunk in tokenize(text, lang):
            out.append((f"class:s-{kind}" if kind != "text" else base_style, chunk))

    def render():
        ch = changes[st["file"]]
        rows = _visible_rows()
        w = _width()
        out: list[tuple[str, str]] = []

        # ── Header
        verdict = st["verdict"].get(st["file"], "")
        badge = {"approved": " ✓ approved", "rejected": " ✗ rejected"}.get(verdict, "")
        out.append(("class:title", f"  {_KIND_VERB.get(ch.kind, 'Editing')}  "))
        out.append(("class:path", f"{_short_path(ch.path)}"))
        out.append(("class:dim", f"   ({st['file'] + 1}/{len(changes)})"))
        if badge:
            out.append((f"class:{verdict}", badge))
        out.append(("", "\n"))
        out.append(("class:add", f"  +{ch.added}  "))
        out.append(("class:del", f"-{ch.removed}  "))
        out.append(("class:mod", f"~{ch.modified}  "))
        out.append(("class:dim", f"{ch.blocks} block(s)   "))
        out.append(("class:dim", f"[{st['mode']}]  {ch.lang}"))
        if not st["context"] and st["mode"] != "full":
            out.append(("class:dim", "  [no-context]"))
        if st["file"] in st["collapsed"]:
            out.append(("class:dim", "  [collapsed]"))
        if st["file"] in edited:
            # The unified diff still shows the AGENT's change; say so, so the
            # user does not read their own edit into it. `f` shows disk.
            out.append(("class:warn", "  [edited here]"))
        if st["search"]:
            hits = len(st["matches"])
            out.append(("class:dim", f"   /{st['search']}  {hits} match{'es' if hits != 1 else ''}"))
        out.append(("", "\n"))
        if dropped:
            # ⚠️ "Complete session diff" is the claim Ctrl+B makes; the cap is what
            # can make it a lie. An empty-looking viewer reads as "nothing
            # changed", so eviction is stated rather than absorbed. The number's
            # owner is `DiffStore.dropped` — this only prints it.
            out.append(("class:warn",
                        f"  ⚠ {dropped} earlier change{'' if dropped == 1 else 's'} "
                        f"dropped — session cap {store.MAX_CHANGES}\n"))
        out.append(("class:rule", "  " + "─" * w + "\n"))

        lines = _lines_for(ch)
        if st["file"] in st["collapsed"]:
            out.append(("class:dim", "\n  Collapsed — press e to expand.\n"))
        elif ch.binary:
            out.append(("class:dim", "\n  Binary file — no textual diff.\n"))
        elif not lines:
            # In full mode this means the file could not be read (deleted since,
            # or a permission fault) — say which, rather than "no textual change",
            # which would claim something false about the change itself.
            msg = ("\n  File is no longer readable — press d for the diff.\n"
                   if st["mode"] == "full" else "\n  No textual change.\n")
            out.append(("class:dim", msg))
        else:
            window = lines[st["scroll"]: st["scroll"] + rows]
            hit_rows = set(st["matches"])
            if st["mode"] == "side":
                _render_side(out, window, st["scroll"], hit_rows, ch.lang, w)
            else:
                _render_unified(out, window, st["scroll"], hit_rows, ch.lang, w)
            if ch.truncated and st["mode"] != "full":
                out.append(("class:warn",
                            f"\n  ⚠ truncated at {MAX_DIFF_LINES} lines — "
                            f"patch export and revert are disabled for this file\n"))

        # ── Footer
        out.append(("class:rule", "\n  " + "─" * w + "\n"))
        if st["searching"]:
            out.append(("class:search", f"  /{st['search']}"))
            out.append(("class:dim", "   Enter confirm · Esc cancel"))
        elif st["armed"] == st["file"]:
            # The armed state is the one moment the footer must stop being a
            # legend and start being a question.
            out.append(("class:warn",
                        "  press r again to REVERT this file on disk · "
                        "any other key cancels"))
        elif st["toast"]:
            out.append(("class:ok", f"  {st['toast']}"))
        else:
            out.append(("class:footer",
                        "  ↑↓ scroll · ←→ file · f full · d diff · s side · c context · e collapse · "
                        "/ search · y copy · w patch · a accept · x reject · r reject→revert · "
                        "E edit · q close"))
        return out

    def _gw(lines) -> int:
        """Gutter width from the largest number present, floor 3. See `_gutter`."""
        nums = [n for _t, _x, n in lines if n is not None]
        return max(3, len(str(max(nums)))) if nums else 3

    def _render_unified(out, window, offset, hit_rows, lang, w):
        gw = _gw(window)
        body = max(20, w - 7 - gw)
        for i, (tag, text, num) in enumerate(window):
            row = offset + i
            marker = "▸" if row in hit_rows else " "
            text = text.rstrip("\n")[:body]
            if tag == "hunk":
                out.append(("class:hunk", f" {marker} {' ' * gw}  {text}\n"))
                continue
            # In full mode there is no "-" side on screen, so a bare "+" would
            # read as "this line was added to a file you are seeing whole" —
            # which is exactly right, and the sign is what distinguishes the
            # changed lines from the surrounding file.
            sign = {"add": "+", "del": "-"}.get(tag, " ")
            style = {"add": "class:add", "del": "class:del"}.get(tag, "class:ctx")
            out.append(("class:dim", f" {marker} {_gutter(num, gw)} "))
            out.append((style, f"{sign} "))
            _emit_tokens(out, tag, text, lang, style)
            out.append(("", "\n"))

    def _render_side(out, window, offset, hit_rows, lang, w):
        """Two columns: deletions left, additions right, context on both."""
        gw = _gw(window)
        col = max(16, (w - 9 - gw) // 2)
        for i, (tag, text, num) in enumerate(window):
            row = offset + i
            marker = "▸" if row in hit_rows else " "
            text = text.rstrip("\n")
            if tag == "hunk":
                out.append(("class:hunk", f" {marker}  {text[:w - 4]}\n"))
                continue
            out.append(("class:dim", f" {marker} {_gutter(num, gw)} "))
            if tag == "add":
                out.append(("class:ctx", " " * col))
                out.append(("class:rule", " │ "))
                _emit_tokens(out, tag, text[:col].ljust(col), lang, "class:add")
            elif tag == "del":
                out.append(("class:del", f"{text[:col]:<{col}}"))
                out.append(("class:rule", " │ "))
                out.append(("class:ctx", " " * col))
            else:
                _emit_tokens(out, tag, text[:col].ljust(col), lang, "class:ctx")
                out.append(("class:rule", " │ "))
                _emit_tokens(out, tag, text[:col].ljust(col), lang, "class:ctx")
            out.append(("", "\n"))

    kb = KeyBindings()

    def _max_scroll() -> int:
        return max(0, len(_lines_for(changes[st["file"]])) - _visible_rows())

    def _typing(event, ch: str) -> bool:
        """While the search line is open, a letter key is TEXT, not a command.

        Every single-letter binding funnels through this. Handling it per-binding
        is how one of them ends up missing it and `/` search silently loses a
        character to a mode switch. ⚠️ `E` needs it too, even though a literal key
        binding always beats `<any>` in prompt_toolkit (it sorts by how many
        `Keys.Any` it contains and the dispatcher takes the last match) — without
        the guard, typing "Editing" into the search box would launch an editor.

        ⚠️ THIS IS ALSO THE DISARM POINT for `r`'s revert confirmation, which is
        what makes "any other key cancels" true by construction rather than by
        twenty handlers each remembering to do it. The armed index is MOVED into
        `_was_armed`, not dropped, because the `r` handler is itself a caller and
        needs to see what its own call just cleared.
        """
        if st["searching"]:
            st["search"] += ch
            return True
        st["toast"] = ""
        st["_was_armed"], st["armed"] = st["armed"], None
        return False

    @kb.add("up")
    def _(event):
        if st["searching"]:
            return
        st["scroll"] = max(0, st["scroll"] - 1)

    @kb.add("down")
    def _(event):
        if st["searching"]:
            return
        st["scroll"] = min(_max_scroll(), st["scroll"] + 1)

    @kb.add("pageup")
    def _(event):
        st["scroll"] = max(0, st["scroll"] - _visible_rows())

    @kb.add("pagedown")
    def _(event):
        st["scroll"] = min(_max_scroll(), st["scroll"] + _visible_rows())

    @kb.add("home")
    def _(event):
        st["scroll"] = 0

    @kb.add("end")
    def _(event):
        st["scroll"] = _max_scroll()

    @kb.add("right")
    def _(event):
        if st["searching"]:
            return
        st["file"] = (st["file"] + 1) % len(changes)
        st["scroll"] = 0
        st["toast"] = ""
        _recompute_matches()

    @kb.add("left")
    def _(event):
        if st["searching"]:
            return
        st["file"] = (st["file"] - 1) % len(changes)
        st["scroll"] = 0
        st["toast"] = ""
        _recompute_matches()

    def _set_mode(mode: str):
        """Switch view. Scroll and matches are row indexes into `_lines_for`, whose
        length and meaning change with the mode — carrying them across would land
        the viewport at an unrelated place in the file."""
        if st["mode"] == mode:
            return
        st["mode"] = mode
        st["scroll"] = 0
        _recompute_matches()

    @kb.add("f")
    def _(event):
        if _typing(event, "f"):
            return
        _set_mode("full")

    @kb.add("d")
    def _(event):
        if _typing(event, "d"):
            return
        _set_mode("unified")

    @kb.add("u")
    def _(event):
        if _typing(event, "u"):
            return
        _set_mode("unified")

    @kb.add("s")
    def _(event):
        if _typing(event, "s"):
            return
        _set_mode("side")

    @kb.add("c")
    def _(event):
        if _typing(event, "c"):
            return
        st["context"] = not st["context"]
        st["scroll"] = 0
        _recompute_matches()

    @kb.add("e")
    def _(event):
        if _typing(event, "e"):
            return
        if st["file"] in st["collapsed"]:
            st["collapsed"].discard(st["file"])
        else:
            st["collapsed"].add(st["file"])
        st["scroll"] = 0

    @kb.add("y")
    def _(event):
        if _typing(event, "y"):
            return
        ok = copy_to_clipboard(to_patch([changes[st["file"]]]))
        _note("copied to clipboard" if ok else "no clipboard available on this machine")

    @kb.add("w")
    def _(event):
        if _typing(event, "w"):
            return
        # Save EVERY change, not just the current file — a patch of one file out
        # of six is almost never what someone means by "save the patch".
        p = save_patch(changes)
        _note(f"patch saved → {p}" if p else "could not write the patch file")

    @kb.add("a")
    def _(event):
        if _typing(event, "a"):
            return
        st["verdict"][st["file"]] = "approved"
        # ⚠️ Say what it does. The write already happened and there is no gate, so
        # a bare "approved" invites the reader to believe they just let something
        # through — which would make them believe "rejected" had stopped one.
        _note("marked reviewed — nothing written")

    @kb.add("x")
    def _(event):
        if _typing(event, "x"):
            return
        # The silent alias, kept because it is what shipped and muscle memory is a
        # feature (rule 28). Marks only; `r` is the key that can act.
        st["verdict"][st["file"]] = "rejected"
        _note("marked rejected — nothing written. r offers to revert on disk")

    @kb.add("r")
    def _(event):
        """[R] Reject — mark, then OFFER the undo behind a second keypress.

        ⚠️ `r` used to revert on the first press while `x` merely marked. The
        spec's own legend calls `[R]` "Reject", so a user following it pressed `r`
        expecting a label and rewrote the file. Rejection now means what it says
        and the destructive half is explicit: press once to reject, again to
        revert. Rule 21 — an uncertain destructive operation is never retried
        blindly, and it is never fired on one ambiguous keypress either.
        """
        if _typing(event, "r"):
            return
        ch = changes[st["file"]]
        st["verdict"][st["file"]] = "rejected"
        if ch.truncated:
            # `revert_change` refuses too; saying WHY here is the difference
            # between a no-op and an explanation.
            _note("marked rejected — cannot revert: diff truncated, "
                  "restoring it would destroy what was never captured")
            return
        if st["_was_armed"] != st["file"]:
            st["armed"] = st["file"]
            _note("marked rejected — press r again to revert this file on disk")
            return
        ok = revert_change(ch)
        _note("reverted on disk" if ok else "could not revert this change")

    @kb.add("E")
    def _(event):
        """[E] Edit — hand the file to $EDITOR.

        Uppercase deliberately: `e` is collapse/expand, which shipped, is in the
        footer and is documented (rule 28). prompt_toolkit distinguishes the two —
        `_parse_key` keeps the case and a literal binding beats `<any>`.

        The editor cannot be launched from here; it needs the terminal the app is
        holding. So this exits with an action and `open_viewer` re-enters after.
        """
        if _typing(event, "E"):
            return
        st["action"] = ("edit", st["file"])
        event.app.exit()

    @kb.add("/")
    def _(event):
        st["searching"] = True
        st["search"] = ""
        st["toast"] = ""

    @kb.add("n")
    def _(event):
        if _typing(event, "n"):
            return
        if st["matches"]:
            st["match_idx"] = (st["match_idx"] + 1) % len(st["matches"])
            st["scroll"] = max(0, st["matches"][st["match_idx"]] - 3)

    @kb.add("p")
    def _(event):
        if _typing(event, "p"):
            return
        if st["matches"]:
            st["match_idx"] = (st["match_idx"] - 1) % len(st["matches"])
            st["scroll"] = max(0, st["matches"][st["match_idx"]] - 3)

    @kb.add("backspace")
    def _(event):
        if st["searching"]:
            st["search"] = st["search"][:-1]

    @kb.add("enter")
    def _(event):
        if st["searching"]:
            st["searching"] = False
            _recompute_matches()

    @kb.add("<any>")
    def _(event):
        # Only meaningful while the search line is open; otherwise unbound keys
        # are ignored rather than closing the viewer by accident.
        if st["searching"]:
            data = getattr(event, "data", "")
            if data and data.isprintable():
                st["search"] += data

    @kb.add("escape", eager=True)
    def _(event):
        if st["searching"]:
            st["searching"] = False
            st["search"] = ""
            st["matches"] = []
            return
        event.app.exit()

    @kb.add("q", eager=True)
    def _(event):
        if _typing(event, "q"):
            return
        event.app.exit()

    @kb.add("c-c", eager=True)
    @kb.add("c-b", eager=True)
    def _(event):
        event.app.exit()

    style = Style.from_dict({
        "title":    f"{P.ACCENT} bold",
        "path":     "#ffffff bold",
        "add":      "#3ddc84",
        "del":      "#ff5555",
        "mod":      "#f0c060",
        "hunk":     "#f0c060",
        "ctx":      "#7a7a8c",
        "dim":      "#666677",
        "rule":     "#2a2a40",
        "search":   "#f0c060 bold",
        "footer":   "#666677",
        "warn":     "#ffb454",
        "ok":       "#3ddc84",
        "approved": "#3ddc84 bold",
        "rejected": "#ff5555 bold",
        # Syntax token classes — fixed, never themed. See `_SYNTAX_HEX`.
        "s-keyword":  _SYNTAX_HEX["keyword"],
        "s-string":   _SYNTAX_HEX["string"],
        "s-comment":  _SYNTAX_HEX["comment"] + " italic",
        "s-number":   _SYNTAX_HEX["number"],
        "s-function": _SYNTAX_HEX["function"],
        "s-type":     _SYNTAX_HEX["type"],
        "s-operator": _SYNTAX_HEX["operator"],
    })

    app = Application(
        layout=Layout(HSplit([Window(FormattedTextControl(render), wrap_lines=False)])),
        key_bindings=kb,
        style=style,
        full_screen=True,
        mouse_support=False,
    )
    app.run()
    return st["action"]
