# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/core/diffs.py
────────────────────
THE diff engine. Shared by the CLI and the Web UI — computation only, no
rendering.

⚠️ THE DIFF IS COMPUTED BEFORE THE WRITE, NOT AFTER.
`write_file` overwrites; once it has run the old content is gone and no diff can
be recovered. `capture_write()` / `capture_edits()` / `capture_delete()` are
therefore called by the dispatch layer *ahead* of the tool, and they read the
file at that moment. A caller that inverts that order silently renders every
write as a pure addition — no error, just a wrong diff, which is the worst kind.

⚠️ THIS MODULE IS PRESENTATION DATA AND MAY NEVER RAISE INTO A TURN.
An unreadable file, a binary blob, a permission error, or a decode failure
degrades to "no diff" — never to a failed tool call. Every public entry point is
total.

⚠️ WHY THIS IS IN `core/` AND NOT `cli/`.
Both surfaces show diffs and the counts must agree: the CLI prints them inline
while the Web UI emits `chat_file_diff` over Socket.IO. Two implementations
would drift, and the drift would be invisible — each surface looks right on its
own. So the engine lives here (no Rich, no prompt_toolkit, no Flask) and each
surface owns only its renderer. Same rule as `config.MODELS` being the one
declaration of models for both halves.

Layer: (nothing) → core.diffs. Imports stdlib only.

⚠️ `preview_window()` IS THE WINDOWING RULE FOR BOTH SURFACES.
It returns `(start, count, hidden)`. The CLI slices with it; the browser ships it
in the payload and hides the rest behind a CSS class, so "Click to expand"
reveals exactly what Ctrl+B does. 3 lines in the terminal and 10 in the browser
is two claims about one change — and each surface looks correct on its own, so
nothing would flag it. Never re-derive the window in a renderer.

⚠️ ONLY CONTENT ROWS COUNT TOWARD THE LIMIT.
A `@@` marker is metadata, so letting one consume a slot makes "head of 3" render
two lines of code — and on a create, where the header is row 0, it always would.
`hidden` counts the same way, so the footer promises lines of the FILE, not rows
of the diff.

CREATE AND EDIT GET DIFFERENT WINDOWS. A new file has no "where did it change",
so its top identifies it; an edit opens PREVIEW_LEAD rows before the first +/-
and runs longer. Header counts are NEVER recomputed from the window — a preview
that shrank its own totals would under-report the change.

⚠️ `hidden` ≠ `truncated`.
`hidden` means "one keypress away". `truncated` means the engine stopped at
MAX_DIFF_LINES and even the full viewer has no more. Printing the first when the
second is true promises content that does not exist.

GUTTER NUMBERS ARE DERIVED FROM `@@` HEADERS, NEVER STORED — a parallel list
desyncs on every slice. A malformed header BLANKS the gutter: blank reads as
"unknown", while off-by-N sends someone editing the wrong line.

⚠️ `spans` EXIST BECAUSE THE BROWSER CANNOT CALL `core.highlight.tokenize`.
The only alternative is a second tokenizer in JS, which would drift silently.
They must reassemble the line BYTE-FOR-BYTE: the tokenizer may re-*colour* a
line, never re-*write* it. `del`/`hunk` rows carry no spans; both surfaces render
them flat.

DISPLAY ONLY — THERE IS NO APPROVAL GATE HERE.

(.github/tests/test_diffs.py — 42 tests, sabotage-verified with 15 breaks.)
⚠️ The `hidden` test is PARAMETRIZED because the obvious fixture cannot see the
bug: on a create the single hunk header falls INSIDE the window, cancelling out
of the wrong formula, so the sabotage passed until multi-hunk shapes existed.
"""

import difflib
import threading
from dataclasses import dataclass, field
from pathlib import Path

# A file bigger than this is summarised by counts only. Rendering a 50k-line
# unified diff into scrollback — or shipping it down a websocket — is not review,
# it is a denial of service on the client.
MAX_DIFF_LINES = 400
_BINARY_SNIFF = 8000

# Tags used by `FileChange.lines`. Renderers switch on these, so they are part of
# the contract: hunk = `@@` header, add = inserted, del = removed, ctx = unchanged.
TAGS = ("hunk", "add", "del", "ctx")

# ── Preview windows ────────────────────────────────────────────────────────────
# How much of a change is shown BEFORE the user asks for the rest — Ctrl+B in the
# CLI, "Click to expand" in the browser.
#
# ⚠️ ONE declaration for BOTH surfaces, for the same reason the counts are: a diff
# that shows 3 lines in the terminal and 10 in the browser is two different claims
# about the same change, and nothing would flag the disagreement.
#
# A write and an edit get different windows because what you need to verify
# differs. A new file has no "where did it change" — the top of the file is what
# confirms it is the right file. An edit does, so the window opens one row before
# the first +/- and runs longer, which shows the change in its own context rather
# than as a naked line.
PREVIEW_CREATE = 3
PREVIEW_EDIT = 10
PREVIEW_LEAD = 1        # rows of lead-in before the first +/- on an edit


@dataclass
class FileChange:
    """One file's before/after, plus the derived diff.

    `kind` is one of create | modify | delete. `lines` holds unified-diff rows
    already classified as (tag, text), so no renderer ever re-parses diff text.
    """

    path: str
    kind: str = "modify"
    added: int = 0
    removed: int = 0
    blocks: int = 0
    lines: list[tuple[str, str]] = field(default_factory=list)
    truncated: bool = False
    binary: bool = False
    lang: str = "generic"

    @property
    def modified(self) -> int:
        """Lines that are a replacement rather than a pure insert/delete.

        A unified diff has no 'modified' tag — a changed line is a `-` adjacent
        to a `+`. The paired count is what the summary calls "modified blocks",
        so this is `min(added, removed)` per file, not a separate tally.
        """
        return min(self.added, self.removed)

    def to_payload(self, include_lines: bool = True) -> dict:
        """JSON-safe dict for the Socket.IO `chat_file_diff` event.

        The Web UI cannot receive a dataclass, and hand-building this dict at the
        emit site is how the two surfaces would start disagreeing about what a
        diff is. `include_lines=False` gives the counts alone for a summary row.

        ⚠️ THE COUNTS ROW IS A SHARED CONTRACT, NOT A PER-RENDERER CHOICE.
        The `└ Added N lines, removed N lines, ~N modified` child row under a
        file's header shows a side ONLY when that side is non-zero, pluralizes on
        1, and prints `~modified` in the hunk yellow. "removed 0 lines" on a pure
        addition is noise, and "Added 1 lines" is a typo the user reads as a bug
        in the diff itself. Formatted strings are deliberately NOT shipped in this
        payload — colour and layout are each surface's own — so the RULE lives
        here, both renderers implement it (`cli/diffview._render_change`,
        `public/script.js:renderFileDiff`), and `test_diffs.py` pins that the two
        still agree. That is the same argument `preview` makes one field below:
        the decision is made once, the drawing twice.
        """
        out = {
            "path": self.path,
            "kind": self.kind,
            "added": self.added,
            "removed": self.removed,
            "modified": self.modified,
            "blocks": self.blocks,
            "truncated": self.truncated,
            "binary": self.binary,
            # The browser highlights from this rather than sniffing the extension
            # itself — one language decision, made in `core.highlight`.
            "lang": self.lang,
        }
        if include_lines:
            # The rows the browser shows COLLAPSED, and how many lines that hides.
            # Shipped rather than recomputed in JS for the same reason `num` and
            # `spans` are: a second implementation of the windowing rule would
            # drift from the terminal's, and "Click to expand" would reveal a
            # different amount than Ctrl+B for the same change.
            start, count, hidden = self.preview_window()
            out["preview"] = {"start": start, "count": count, "hidden": hidden}
            # `num` is the gutter line number, derived here (see numbered_lines)
            # so the browser never recomputes it and cannot disagree with the
            # terminal about which line a change landed on.
            #
            # `spans` is the same argument applied to syntax highlighting: the
            # browser CANNOT call `core.highlight.tokenize`, so the only way to
            # colour a row there is to ship the token spans or re-implement the
            # tokenizer in JavaScript. A second tokenizer would drift silently —
            # each surface looks right on its own — which is the exact failure
            # this module's header rules out. So the decision is made once, in
            # Python, and the browser maps kinds to CSS classes.
            #
            # ⚠️ `del` and `hunk` rows carry NO spans, deliberately. A removed
            # line renders flat red on both surfaces (see `_rich_line`), so
            # tokenizing them would be bytes on the wire for output nobody uses.
            out["lines"] = [
                {"tag": t, "text": x, "num": n,
                 **({} if t in ("del", "hunk") else {"spans": _spans(x, self.lang)})}
                for t, x, n in self.numbered_lines()
            ]
        return out

    def preview_window(self) -> tuple[int, int, int]:
        """Which rows to show before the user expands: `(start, count, hidden)`.

        Indexes into `numbered_lines()`. THE windowing decision for both surfaces —
        the CLI slices its rows with it, the browser ships it in the payload and
        collapses to the same rows, so "Click to expand" and Ctrl+B reveal the same
        thing.

        ⚠️ Only CONTENT rows count toward the limit. A `@@` marker is metadata about
        where we are in the file, not a line of it, so letting one consume a slot
        makes "head of 3" quietly render two lines of code — and on a create, where
        the header is always the first row, it always would. `hidden` is counted the
        same way, so it means lines of the file rather than rows of the diff.
        """
        rows = self.numbered_lines()
        content = sum(1 for t, _x, _n in rows if t != "hunk")
        if not rows:
            return 0, 0, 0

        if self.kind == "create":
            keep, start = PREVIEW_CREATE, 0
        else:
            keep = PREVIEW_EDIT
            first = next((i for i, (t, _x, _n) in enumerate(rows)
                          if t in ("add", "del")), 0)
            start = max(0, first - PREVIEW_LEAD)

        count = 0
        shown = 0
        for t, _x, _n in rows[start:]:
            if shown >= keep:
                break
            count += 1
            if t != "hunk":
                shown += 1
        return start, count, max(0, content - shown)

    def numbered_lines(self) -> list[tuple[str, str, int | None]]:
        """`lines` with a gutter line number attached to each row.

        ⚠️ DERIVED from the `@@` headers, never stored. A parallel `numbers` list
        beside `lines` could desync from it — every truncation, filter or slice
        would have to remember to cut both — whereas a number computed from the
        row stream is correct by construction.

        Which number a row gets:
          • `del` → its line number in the file BEFORE the change
          • `add` → its line number in the file AFTER
          • `ctx` → the AFTER number, because that is the file the user now has
          • `hunk` → None; the `@@` header is not a line of the file

        A malformed or absent `@@` header degrades to `None` for the whole run
        rather than to a wrong number: a blank gutter reads as "unknown", while
        an off-by-N gutter would send someone editing the wrong line.
        """
        out: list[tuple[str, str, int | None]] = []
        old_n: int | None = None
        new_n: int | None = None
        for tag, text in self.lines:
            if tag == "hunk":
                old_n, new_n = _parse_hunk(text)
                out.append((tag, text, None))
            elif tag == "del":
                out.append((tag, text, old_n))
                if old_n is not None:
                    old_n += 1
            elif tag == "add":
                out.append((tag, text, new_n))
                if new_n is not None:
                    new_n += 1
            else:
                out.append((tag, text, new_n))
                if old_n is not None:
                    old_n += 1
                if new_n is not None:
                    new_n += 1
        return out


def _spans(text: str, lang: str) -> list[dict]:
    """Token spans for one row, as JSON for the browser's renderer.

    Degrades to a single `text` span — i.e. the unhighlighted line, which is what
    a terminal without colour shows anyway — rather than raising into an emit.
    """
    try:
        from agent2.core.highlight import tokenize
        return [{"k": k, "t": c} for k, c in tokenize(text, lang)]
    except Exception:
        return [{"k": "text", "t": text}]


def _parse_hunk(header: str) -> tuple[int | None, int | None]:
    """Extract the (old_start, new_start) line numbers from an `@@` header.

    `@@ -12,7 +12,9 @@` → (12, 12). Returns (None, None) on anything it does not
    recognise — see `numbered_lines` for why that beats guessing.
    """
    try:
        body = header.split("@@")[1].strip()
        old_part, new_part = body.split(" ")[0], body.split(" ")[1]
        old = int(old_part.lstrip("-").split(",")[0])
        new = int(new_part.lstrip("+").split(",")[0])
        return old, new
    except Exception:
        return None, None


def _is_binary(text: str) -> bool:
    return "\x00" in text[:_BINARY_SNIFF]


def _read(path) -> str | None:
    """Current content of *path*, or None when it does not exist / can't be read."""
    try:
        p = Path(path).expanduser()
        if not p.exists() or not p.is_file():
            return None
        return p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None


def compute_change(path: str, before: str | None, after: str | None) -> FileChange:
    """Build a FileChange from two snapshots. Never raises.

    `before is None` → create. `after is None` → delete.
    """
    kind = "modify"
    if before is None:
        kind = "create"
        before = ""
    if after is None:
        kind = "delete"
        after = ""

    ch = FileChange(path=str(path), kind=kind)
    try:
        from agent2.core.highlight import lang_for
        ch.lang = lang_for(str(path))
    except Exception:
        ch.lang = "generic"

    try:
        if _is_binary(before) or _is_binary(after):
            ch.binary = True
            return ch

        diff = list(difflib.unified_diff(
            before.splitlines(keepends=False),
            after.splitlines(keepends=False),
            lineterm="", n=3,
        ))
        # Drop the ---/+++ header rows; the path is rendered separately.
        diff = [d for d in diff if not d.startswith(("---", "+++"))]

        for line in diff:
            if line.startswith("@@"):
                ch.blocks += 1
                ch.lines.append(("hunk", line))
            elif line.startswith("+"):
                ch.added += 1
                ch.lines.append(("add", line[1:]))
            elif line.startswith("-"):
                ch.removed += 1
                ch.lines.append(("del", line[1:]))
            else:
                ch.lines.append(("ctx", line[1:] if line.startswith(" ") else line))

        # Truncate the BODY only. The counts above are already complete, so a
        # capped diff still reports "+312 -40" honestly and only the rendered
        # rows are cut — reporting truncated counts would understate the change.
        if len(ch.lines) > MAX_DIFF_LINES:
            ch.lines = ch.lines[:MAX_DIFF_LINES]
            ch.truncated = True
    except Exception:
        # Presentation only — a failed diff must not fail the write.
        ch.lines = []

    return ch


# ── Undoing a change ──────────────────────────────────────────────────────────
# ⚠️ THE BEFORE-SIDE OF A WINDOWED DIFF IS NOT THE FILE. `ch.lines` is an `n=3`
# unified diff, so `[t for tag, t in ch.lines if tag in ("del", "ctx")]` is the
# whole pre-change file only when every line of it happened to fall inside a hunk.
# That reconstruction shipped in `cli/diffview.revert_change`, and on a 200-line
# file with one changed line it wrote 7 lines to disk and returned True.
# `ch.truncated` cannot catch it: that flag means "past MAX_DIFF_LINES *rendered*
# rows", not "elided", and it is False for every ordinary diff. So an undo
# reverse-applies the hunks to the file ON DISK — the parts a windowed diff never
# captured are then the parts it never touches.
#
# The computation lives here because `core/diffs.py` is the one home for reading a
# diff (the one-declaration rule); `cli/diffview.revert_change` is the disk actor.

def hunks_of(ch: FileChange) -> list[tuple[int, int, list[str], list[str]]] | None:
    """Split `ch.lines` into `(old_start, new_start, before_rows, after_rows)`.

    `None` means the rows cannot be read as hunks: an unparseable `@@` header, a
    row before the first header, a tag this reader does not know, or no hunks at
    all. ⚠️ Every caller must treat that as *refuse*, never as *nothing to do* —
    the one consumer writes to disk.
    """
    out: list[tuple[int, int, list[str], list[str]]] = []
    cur: tuple[int, int, list[str], list[str]] | None = None
    for tag, text in ch.lines:
        if tag == "hunk":
            old, new = _parse_hunk(text)
            if old is None or new is None:
                return None
            cur = (old, new, [], [])
            out.append(cur)
            continue
        if cur is None:
            return None                     # a row before any `@@` header
        if tag == "ctx":
            cur[2].append(text)
            cur[3].append(text)
        elif tag == "del":
            cur[2].append(text)
        elif tag == "add":
            cur[3].append(text)
        else:
            return None                     # a tag this reader does not know
    return out or None


def revert_text(ch: FileChange, current: str) -> str | None:
    """The file as it was before *ch*, or None when that cannot be established.

    Reverse-applies each hunk to *current*: find the hunk's after-side, put its
    before-side back. Hunks are applied LAST-FIRST, so the earlier ones still sit
    at the offsets their own headers were written against.

    ⚠️ EVERY HUNK IS VERIFIED AGAINST *current* BEFORE ANYTHING IS RETURNED, AND
    ONE MISMATCH REFUSES THE WHOLE UNDO. The after-side is what the file is
    supposed to look like right now, so a mismatch means the file moved on — an
    `[E]` round trip in the viewer, another turn's write, a hand edit — and
    reverting against a file we no longer recognise is how an undo becomes a
    second corruption. Nothing is written here, so the refusal is atomic by
    construction: this returns a string or None and the caller owns the write.
    """
    hunks = hunks_of(ch)
    if hunks is None:
        return None
    lines = current.splitlines(keepends=False)
    for _old_start, new_start, before_rows, after_rows in reversed(hunks):
        # A hunk with no after-side is a whole-file deletion. difflib writes
        # `@@ -1,15 +0,0 @@` for it (`_format_range_unified` does `beginning -= 1`
        # when the length is 0), so the number names an insertion POINT and is
        # already the index. Every other header points at a real 1-based line.
        idx = (new_start - 1) if after_rows else new_start
        if idx < 0 or idx + len(after_rows) > len(lines):
            return None
        if lines[idx:idx + len(after_rows)] != after_rows:
            return None
        lines[idx:idx + len(after_rows)] = before_rows
    if not lines:
        return ""
    # A trailing newline is not representable in the diff (`lineterm=""` over
    # `splitlines`), so follow the file in hand — and a file that is absent or
    # empty (an undone delete) gets one, which is what every other writer here
    # produces.
    tail = "\n" if (current.endswith("\n") or not current) else ""
    return "\n".join(lines) + tail


# ── Capture helpers (called BEFORE the tool runs) ──────────────────────────────
# These are the whole reason this module exists. Each reads the file as it is
# RIGHT NOW and pairs it with what the tool is about to write.

def capture_write(args: dict) -> FileChange | None:
    """Snapshot for a pending `write_file`. Call before dispatching the tool."""
    try:
        path = args.get("path", "")
        if not path:
            return None
        return compute_change(path, _read(path), args.get("content", ""))
    except Exception:
        return None


def capture_edits(args: dict) -> list[FileChange]:
    """Snapshots for a pending `multi_edit_files`.

    Each edit is applied to an in-memory copy so several edits to ONE file
    accumulate into a single diff, matching what the tool will actually write.
    Emitting one diff per edit would show the same file three times with
    overlapping hunks and no single accurate before/after.
    """
    out: list[FileChange] = []
    try:
        edits = args.get("edits", [])
        if not isinstance(edits, list):
            return out

        originals: dict[str, str | None] = {}
        working: dict[str, str] = {}
        order: list[str] = []

        for e in edits:
            if not isinstance(e, dict):
                continue
            path = e.get("path", "")
            if not path:
                continue
            if path not in originals:
                originals[path] = _read(path)
                working[path] = originals[path] or ""
                order.append(path)
            old_text = e.get("old_text", "")
            new_text = e.get("new_text", "")
            if old_text and old_text in working[path]:
                working[path] = working[path].replace(old_text, new_text)

        # `extend` over a generator, not an append loop: if `compute_change`
        # ever did raise, the paths already computed stay in `out` exactly as
        # the append form kept them, so the failsafe below is unchanged.
        out.extend(compute_change(path, originals[path], working[path]) for path in order)
    except Exception:
        pass
    return out


def capture_delete(args: dict) -> FileChange | None:
    """Snapshot for a pending `delete_file`."""
    try:
        path = args.get("path", "")
        if not path:
            return None
        before = _read(path)
        if before is None:
            return None
        return compute_change(path, before, None)
    except Exception:
        return None


# Dispatch table so a caller does not re-implement the "which tool needs which
# capture" decision. `capture_for()` always returns a LIST — multi_edit_files
# legitimately produces several changes and a caller special-casing that at each
# site is how one of them ends up forgetting.
def capture_for(tool_name: str, args: dict) -> list[FileChange]:
    """Pre-dispatch snapshots for *tool_name*. Empty list when it touches no file.

    ⚠️ TWO KNOWN BLIND SPOTS, DOCUMENTED RATHER THAN HALF-CLOSED.
    Both are "no diff appears", which is quiet, so they are written down here so
    the next reader does not conclude the engine is broken:

      1. **A file written by a shell command is invisible.** The table above
         matches three TOOL NAMES; `run_command` with `bash -c '... > f'`, and the
         fileintel writers (`run_file_op`, `convert_file` with an
         `options.output_path`), all change files without going through one of
         them. Closing this needs either post-hoc filesystem watching or diffing
         inside `dispatch_tool` — a new subsystem either way, not a patch here.
      2. **A wholly-failed `multi_edit_files` still renders a no-op diff.**
         `_impl_multi_edit` always returns a `results` key, so both surfaces'
         success gates pass and the user sees `Added 0 lines, removed 0 lines` +
         `no textual change`. Fixing it needs a per-edit failure count in
         `tools.py`'s return contract, which is that module's decision.
    """
    try:
        if tool_name == "write_file":
            ch = capture_write(args)
            return [ch] if ch else []
        if tool_name == "multi_edit_files":
            return capture_edits(args)
        if tool_name == "delete_file":
            ch = capture_delete(args)
            return [ch] if ch else []
    except Exception:
        pass
    return []


def summarize(changes: list[FileChange]) -> dict:
    """Aggregate counts for a set of changes — the "✓ 4 files changed" row.

    One implementation so the CLI's inline summary and the Web UI's summary bar
    can never disagree about what "modified blocks" means.
    """
    try:
        return {
            "files": len(changes),
            "added": sum(c.added for c in changes),
            "removed": sum(c.removed for c in changes),
            "modified": sum(c.modified for c in changes),
            "blocks": sum(c.blocks for c in changes),
        }
    except Exception:
        return {"files": 0, "added": 0, "removed": 0, "modified": 0, "blocks": 0}


# ── Session store ──────────────────────────────────────────────────────────────
class DiffStore:
    """Bounded, thread-safe record of what changed this session.

    The CLI's Ctrl+B viewer and the Web UI's diff panel both read this. Bounded
    because an agent left running overnight would otherwise hold every version of
    every file it touched in memory.

    ⚠️ DELIBERATELY IN-MEMORY. There is no `file_changes` table, so nothing here
    survives the process — and in dual mode the CLI runs as a CHILD PROCESS, so
    the two halves hold two disjoint stores. Anything that claims to show "the
    whole session" must mean "this process's session".

    ⚠️ `mark()`/`since()` EXIST BECAUSE `len()` IS NOT A USABLE BOOKMARK.
    `add()` trims from the FRONT, so a length remembered before a turn is not an
    index into the list after it: once the cap engages, a caller that saved
    `len(store.all())` and sliced from there reports the WRONG turn's changes, and
    reports them confidently. `_seq` is monotonic and survives the trim, so a mark
    keeps meaning what it meant.
    """

    MAX_CHANGES = 200

    def __init__(self):
        self._changes: list[FileChange] = []
        self._lock = threading.Lock()
        self._seq = 0            # total ever recorded; never decreases
        self._dropped = 0        # how many the cap evicted (NOT how many were cleared)

    def add(self, ch: FileChange | None) -> None:
        if ch is None:
            return
        with self._lock:
            self._changes.append(ch)
            self._seq += 1
            if len(self._changes) > self.MAX_CHANGES:
                cut = len(self._changes) - self.MAX_CHANGES
                del self._changes[:cut]
                # ⚠️ COUNTED, NOT SILENTLY FORGOTTEN. The viewer calls itself the
                # complete session diff; a cap that eats history without saying so
                # turns that into a lie, and an empty-looking viewer reads as
                # "nothing changed".
                self._dropped += cut

    def extend(self, changes: list[FileChange]) -> None:
        for ch in changes or []:
            self.add(ch)

    def all(self) -> list[FileChange]:
        with self._lock:
            return list(self._changes)

    def mark(self) -> int:
        """An opaque bookmark meaning "everything recorded up to right now".

        Pair with `since()` to get one turn's changes out of a session-long store.
        Opaque on purpose: it is a sequence number, not a length, and treating it
        as one is the bug this exists to prevent (see the class docstring).
        """
        with self._lock:
            return self._seq

    def since(self, mark: int) -> list[FileChange]:
        """The changes recorded after *mark*, oldest first. Never raises.

        Returns at most what is still held: a mark older than `MAX_CHANGES`
        evictions ago cannot resurrect the evicted rows, and `dropped` is where
        that gap is reported. A mark this store never issued (or garbage) degrades
        to "the whole session" — the answer callers had before marks existed —
        rather than to an empty list, because an empty list would make an
        `if files > 0` recap vanish with no sign that anything went wrong.
        """
        try:
            m = int(mark)
        except Exception:
            m = 0
        with self._lock:
            n = self._seq - m
            if n <= 0:
                return []
            # n >= len() slices the whole list, which is exactly right: every row
            # still held was recorded after the mark.
            return list(self._changes[-n:])

    @property
    def dropped(self) -> int:
        """How many changes `MAX_CHANGES` evicted this session.

        Only the cap counts. `clear()` is a user-driven reset, not a silent loss,
        and folding it in here would make the viewer blame the cap for it.
        """
        with self._lock:
            return self._dropped

    def clear(self) -> None:
        with self._lock:
            self._changes.clear()

    def stats(self) -> dict:
        return summarize(self.all())


store = DiffStore()
