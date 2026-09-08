# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""Tests for the shared diff engine (`agent2/core/diffs.py`) and the two
renderers that read it.

⚠️ WHY THIS FILE EXISTS.
`core/diffs.py` makes a claim in its own header — "both surfaces show diffs and
the counts must agree" — and until this file nothing enforced it. The engine and
both renderers had **zero** coverage across 486 tests, which is the same position
`agent2cli.py` was in before `test_cli.py`: the only argument for any change to
it was "it still renders".

The claim is not testable by looking at either surface alone. Each one looks
right on its own; the failure is that they disagree, and nobody is holding both
at once. So the central test here replays the BROWSER's windowing rule — read out
of the real `public/script.js`, not restated from memory — against the CLI's
slice, and fails if they ever reveal different rows for the same change.
"""

import ast
import inspect
import re
from pathlib import Path

import pytest

from agent2.core import diffs
from agent2.core.diffs import FileChange, compute_change

_ROOT = Path(__file__).resolve().parent.parent.parent
_SCRIPT_JS = _ROOT / "public" / "script.js"
_STYLE_CSS = _ROOT / "public" / "style.css"
_DIFFVIEW = _ROOT / "agent2" / "cli" / "diffview.py"
_AGENT2CLI = _ROOT / "agent2cli.py"
_AGENT_PY = _ROOT / "agent2" / "agent.py"
_PROVIDER_AGENT = _ROOT / "agent2" / "llm" / "provider_agent.py"


# ── Fixtures ───────────────────────────────────────────────────────────────────

def _lines(n, start=1):
    return "\n".join(f"line {i}" for i in range(start, start + n))


def _create(n=20):
    return compute_change("new.py", None, _lines(n))


def _modify(changed=(25,), n=40):
    before = _lines(n)
    after = "\n".join(("CHANGED" if i in changed else f"line {i}")
                      for i in range(1, n + 1))
    return compute_change("mod.py", before, after)


# ── The one that matters: both surfaces reveal the same rows ───────────────────

def _browser_visible(payload):
    """The rows `renderFileDiff` leaves visible while collapsed.

    A transcription of the rule in `public/script.js`; `test_browser_windowing_
    rule_is_still_the_one_transcribed_here` fails if the real file stops matching
    it, so this cannot quietly drift into testing a rule the browser abandoned.
    """
    pv = payload.get("preview")
    start = (pv.get("start") or 0) if pv else 0
    end = start + (pv.get("count") or 0) if pv else float("inf")
    hidden = (pv.get("hidden") or 0) if pv else 0
    return [(ln["tag"], ln["text"])
            for i, ln in enumerate(payload["lines"])
            if not (hidden > 0 and (i < start or i >= end))]


def _cli_visible(ch):
    """The rows `_render_change` prints under `preview=True` (diffview.py:260)."""
    start, count, _hidden = ch.preview_window()
    return [(t, x) for t, x, _n in ch.numbered_lines()[start:start + count]]


@pytest.mark.parametrize("ch", [
    _create(20),
    _create(3),
    _modify((25,)),
    _modify((25, 26)),
    _modify((1,)),                                    # change on the first line
    _modify((40,)),                                   # change on the last line
    _modify((5, 30)),                                 # a hunk outside the window
    _modify((5, 30, 60), n=80),                       # two
    compute_change("t.py", None, "a\nb"),             # shorter than the window
    compute_change("gone.py", _lines(15), None),      # delete
    compute_change("same.py", _lines(5), _lines(5)),  # no change at all
], ids=["create20", "create3", "mod1", "mod2", "mod-first", "mod-last",
        "2-hunks", "3-hunks", "tiny", "delete", "unchanged"])
def test_both_surfaces_reveal_identical_rows(ch):
    """THE claim `core/diffs.py` exists to make.

    Not a tautology: the two sides are computed by different code — the CLI
    slices `numbered_lines()`, the browser filters the serialized `lines` array
    by index — and a windowing bug that hit only one of them shows up here as a
    mismatch.
    """
    assert _browser_visible(ch.to_payload()) == _cli_visible(ch)


def test_browser_windowing_rule_is_still_the_one_transcribed_here():
    """Pins `_browser_visible` against the REAL script.js.

    Without this the transcription above is a copy that can rot: script.js could
    change how it collapses and every agreement test would keep passing against
    the old rule. Same argument as `test_run_setup.py` AST-parsing real call
    sites instead of trusting a restated list.
    """
    src = re.sub(r"\s+", "", _SCRIPT_JS.read_text(encoding="utf-8"))
    assert "constpvStart=pv?(pv.start||0):0;" in src
    assert "constpvEnd=pvStart+(pv.count||0);" in src or \
           "constpvEnd=pv?pvStart+(pv.count||0):Infinity;" in src
    assert "constpvHidden=pv?(pv.hidden||0):0;" in src
    # The hide predicate itself.
    assert "pvHidden>0&&(i<pvStart||i>=pvEnd)?'dl-hid':''" in src


def test_the_cli_does_not_keep_its_own_copy_of_the_window():
    """The CLI must CALL `preview_window()`, never re-derive it.

    A local copy is how the two surfaces start showing different amounts of the
    same change — the failure this whole module is arranged to prevent, and one
    that no rendering test can see because each surface still looks correct.
    """
    src = _DIFFVIEW.read_text(encoding="utf-8")
    assert "ch.preview_window()" in src
    assert "PREVIEW_CREATE" not in src
    assert "PREVIEW_EDIT" not in src


# ── The windowing rule itself ──────────────────────────────────────────────────

def test_a_hunk_marker_does_not_consume_a_preview_slot():
    """`@@` is metadata about position, not a line of the file.

    Letting one count toward the limit makes "head of 3" render two lines of
    code — and on a create, where the header is always row 0, it always would.
    """
    ch = _create(20)
    start, count, _hidden = ch.preview_window()
    shown = ch.numbered_lines()[start:start + count]
    content = [r for r in shown if r[0] != "hunk"]
    assert len(content) == diffs.PREVIEW_CREATE
    assert len(shown) > len(content), "fixture must contain a hunk header to be a real test"


@pytest.mark.parametrize("ch", [
    _create(20),                       # the one hunk sits INSIDE the window
    _modify((25,)),                    # the hunk sits before it
    _modify((5, 30)),                  # a second hunk falls outside it
    _modify((5, 30, 60), n=80),        # two do
], ids=["create", "hunk-before", "2-hunks", "3-hunks"])
def test_hidden_counts_lines_of_the_file_not_rows_of_the_diff(ch):
    """"… 17 more lines" is a promise about the FILE.

    Counting diff rows instead inflates it by one per `@@` header outside the
    window, so the footer promises content that expanding never produces.

    ⚠️ The fixtures are parametrized because the obvious one cannot see the bug.
    On a create the single hunk header falls *inside* the window, so it cancels
    from both sides of `len(rows) - count` and the wrong formula returns the
    right answer — this test passed against the sabotage until the hunk-before
    and multi-hunk shapes were added.
    """
    rows = ch.numbered_lines()
    start, count, hidden = ch.preview_window()
    shown = rows[start:start + count]
    # Stated independently of the implementation: what is hidden is every
    # content row that the window does not cover.
    outside = [r for i, r in enumerate(rows)
               if not (start <= i < start + count) and r[0] != "hunk"]
    assert hidden == len(outside)
    assert hidden + sum(1 for r in shown if r[0] != "hunk") == \
        sum(1 for r in rows if r[0] != "hunk")


def test_create_and_edit_get_different_windows():
    """A new file has no "where did it change" — its top is what identifies it.
    An edit does, so it gets the longer window opened around the change.
    """
    assert diffs.PREVIEW_CREATE != diffs.PREVIEW_EDIT
    c_start, c_count, _ = _create(40).preview_window()
    m_start, m_count, _ = _modify((25,)).preview_window()
    assert c_start == 0
    assert c_count < m_count


def test_an_edit_window_opens_before_the_first_change():
    """The lead-in is what makes a `+` read as a change rather than a naked line."""
    ch = _modify((25,))
    rows = ch.numbered_lines()
    start, count, _hidden = ch.preview_window()
    first = next(i for i, (t, _x, _n) in enumerate(rows) if t in ("add", "del"))
    assert start == max(0, first - diffs.PREVIEW_LEAD)
    assert start < first or first == 0
    # …and the change is actually inside the window it opened for.
    assert first < start + count


def test_a_change_smaller_than_the_window_hides_nothing():
    """`hidden == 0` is what suppresses the affordance on both surfaces.

    An always-on "Click to expand" that reveals nothing trains people to ignore
    the one that doesn't.
    """
    ch = compute_change("t.py", None, "a\nb")
    _start, count, hidden = ch.preview_window()
    assert hidden == 0
    assert count == len(ch.numbered_lines())


def test_expanding_reveals_the_whole_file_not_a_second_window():
    """Every row ships in the payload; the window only picks which are hidden."""
    ch = _create(20)
    assert len(ch.to_payload()["lines"]) == len(ch.numbered_lines())


def test_preview_window_on_an_empty_change_is_inert():
    ch = FileChange(path="x.py", kind="modify")
    assert ch.preview_window() == (0, 0, 0)


def test_the_windowed_header_still_reports_the_whole_change():
    """A preview that also shrank its totals would under-report the change —
    the one thing a review surface may never do."""
    ch = _modify(tuple(range(10, 31)))
    _start, count, hidden = ch.preview_window()
    assert hidden > 0, "fixture must actually be windowed"
    assert ch.added == 21 and ch.removed == 21
    assert ch.to_payload()["added"] == 21


# ── Gutter numbering ───────────────────────────────────────────────────────────

def test_gutter_numbers_track_the_right_side_of_the_change():
    """`del` → line before, `add`/`ctx` → line after. An off-by-N gutter sends
    someone editing the wrong line."""
    ch = _modify((25,))
    rows = ch.numbered_lines()
    dels = [(x, n) for t, x, n in rows if t == "del"]
    adds = [(x, n) for t, x, n in rows if t == "add"]
    assert dels == [("line 25", 25)]
    assert adds == [("CHANGED", 25)]
    # Context above the change is numbered from the hunk start, contiguously.
    ctx = [n for t, _x, n in rows if t == "ctx"]
    assert ctx == list(range(22, 25)) + list(range(26, 29))


def test_a_hunk_header_has_no_line_number():
    ch = _modify((25,))
    assert all(n is None for t, _x, n in ch.numbered_lines() if t == "hunk")


def test_a_malformed_hunk_header_blanks_the_gutter_rather_than_guessing():
    """A blank gutter reads as "unknown"; a wrong one reads as fact."""
    ch = FileChange(path="x.py", kind="modify",
                    lines=[("hunk", "@@ garbage @@"), ("add", "a"), ("ctx", "b")])
    assert [n for _t, _x, n in ch.numbered_lines()] == [None, None, None]


# ── Totality: presentation data may never raise into a turn ────────────────────

def test_compute_change_survives_a_binary_blob():
    ch = compute_change("x.bin", "\x00\x01\x02", "\x00\x03")
    assert ch.binary and ch.lines == []
    ch.to_payload()          # must not raise
    assert ch.preview_window() == (0, 0, 0)


@pytest.mark.parametrize("args", [{}, {"path": ""}, {"path": None},
                                  {"edits": "not-a-list"}, {"edits": [None, 3]}])
def test_capture_helpers_never_raise_on_junk(args):
    assert diffs.capture_for("write_file", args) == [] or True
    assert isinstance(diffs.capture_for("multi_edit_files", args), list)
    assert isinstance(diffs.capture_for("delete_file", args), list)
    assert diffs.capture_for("not_a_file_tool", args) == []


def test_capture_reads_the_file_before_the_write(tmp_path):
    """⚠️ The ordering the module header calls out.

    Capture after the write and every write renders as a pure addition — no
    error, just a wrong diff, which is the worst kind. This test pins the
    behaviour capture depends on: the snapshot is taken at CALL time.
    """
    f = tmp_path / "a.txt"
    f.write_text("old\n", encoding="utf-8")
    ch = diffs.capture_write({"path": str(f), "content": "new\n"})
    f.write_text("new\n", encoding="utf-8")          # the tool runs afterwards
    assert ch.kind == "modify"
    assert ch.added == 1 and ch.removed == 1


def test_several_edits_to_one_file_become_one_diff(tmp_path):
    """Not three diffs of the same file with overlapping hunks and no single
    accurate before/after."""
    f = tmp_path / "a.txt"
    f.write_text("a\nb\nc\n", encoding="utf-8")
    out = diffs.capture_edits({"edits": [
        {"path": str(f), "old_text": "a", "new_text": "A"},
        {"path": str(f), "old_text": "c", "new_text": "C"},
    ]})
    assert len(out) == 1
    assert out[0].added == 2 and out[0].removed == 2


def test_capture_delete_of_a_missing_file_is_no_diff(tmp_path):
    assert diffs.capture_delete({"path": str(tmp_path / "nope.txt")}) is None


# ── Truncation ─────────────────────────────────────────────────────────────────

def test_truncation_caps_the_body_but_never_the_counts():
    """A capped diff still reports "+N" honestly; truncated counts would
    understate the change."""
    big = diffs.MAX_DIFF_LINES + 200
    ch = compute_change("big.py", None, _lines(big))
    assert ch.truncated
    assert len(ch.lines) == diffs.MAX_DIFF_LINES
    assert ch.added == big


# ── Payload shape ──────────────────────────────────────────────────────────────

def test_removed_and_hunk_rows_ship_without_spans():
    """They render flat on both surfaces, so tokenizing them is bytes on the
    wire for output nobody uses."""
    p = _modify((25,)).to_payload()
    for ln in p["lines"]:
        if ln["tag"] in ("del", "hunk"):
            assert "spans" not in ln
        else:
            assert isinstance(ln["spans"], list)


def test_spans_reconstruct_the_line_exactly():
    """⚠️ The tokenizer may re-COLOUR a line, never re-WRITE it.

    The browser builds each row by concatenating `spans`, so if they do not
    reassemble the original text byte-for-byte it renders a line that is not the
    line the terminal shows — a diff that lies about the file, with no error
    anywhere. Checked on source with strings, comments and punctuation, since a
    tokenizer is likeliest to drop a character at a token boundary.
    """
    src = ('def health():\n'
           '    # NOT "enabled but workers == 0"\n'
           '    if sch.get("worker_starts"):\n'
           '        problems.append(f"{n} lost")\n')
    ch = compute_change("routes.py", "def health():\n    pass\n", src)
    checked = 0
    for ln in ch.to_payload()["lines"]:
        if "spans" not in ln:
            continue
        assert "".join(s["t"] for s in ln["spans"]) == ln["text"]
        checked += 1
    assert checked, "fixture produced no highlighted rows"


def test_counts_only_payload_carries_no_lines():
    p = _modify((25,)).to_payload(include_lines=False)
    assert "lines" not in p and "preview" not in p
    assert p["added"] == 1 and p["removed"] == 1


def test_modified_is_the_paired_count_not_a_separate_tally():
    ch = compute_change("x.py", _lines(5), _lines(3))
    assert ch.modified == min(ch.added, ch.removed)


def test_summarize_aggregates_and_never_raises():
    s = diffs.summarize([_create(5), _modify((25,))])
    assert s["files"] == 2
    assert diffs.summarize([]) == {"files": 0, "added": 0, "removed": 0,
                                   "modified": 0, "blocks": 0}


# ── The counts row both surfaces print (Task 11) ───────────────────────────────
# `└ Added 8 lines, removed 6 lines, ~6 modified` under a file's header. The RULE
# is stated in `FileChange.to_payload`'s docstring — suppress a zero side,
# pluralize on 1, `~modified` in the hunk yellow — and drawn twice, once per
# surface. Before Task 11 the terminal printed both sides always and never
# pluralized ("Added 1 lines, removed 0 lines") while the browser already did the
# right thing: one change, two claims about it, each side looking correct alone.


def _plain(monkeypatch, capsys, ch, **kw):
    """`_render_change`'s no-Rich output as text. Nothing else asserts on print."""
    from agent2.cli import diffview
    monkeypatch.setattr(diffview, "_RICH", False)
    diffview.render_change(ch, **kw)
    return capsys.readouterr().out


def test_the_counts_row_hides_a_side_that_is_zero_on_both_surfaces(monkeypatch, capsys):
    """A pure addition must not say "removed 0 lines" on either surface.

    Behaviour first, then the cross-source check: the two renderers are different
    code in different languages, so the only thing that can catch them drifting
    apart is holding both at once. Same argument as
    `test_browser_windowing_rule_is_still_the_one_transcribed_here`.
    """
    out = _plain(monkeypatch, capsys, _create(20))
    assert "Added 20 lines" in out
    assert "removed" not in out, "a create has nothing removed; the row must omit that side"

    out = _plain(monkeypatch, capsys, compute_change("gone.py", _lines(9), None))
    assert "removed 9 lines" in out
    assert "Added" not in out, "a delete adds nothing; the row must omit that side"

    # …and the browser's copy of the rule still reads the same way.
    js = re.sub(r"\s+", "", _SCRIPT_JS.read_text(encoding="utf-8"))
    assert "if(d.added)cnt.push" in js
    assert "if(d.removed)cnt.push" in js
    cli = re.sub(r"\s+", "", inspect.getsource(
        __import__("agent2.cli.diffview", fromlist=["x"])._render_change))
    assert "ifch.added:" in cli
    assert "ifch.removed:" in cli


def test_the_counts_row_pluralizes_on_one_on_both_surfaces(monkeypatch, capsys):
    """"Added 1 lines" reads as a bug in the diff, not a typo in the label."""
    out = _plain(monkeypatch, capsys, _modify((25,)))
    assert "Added 1 line" in out and "Added 1 lines" not in out
    assert "removed 1 line" in out and "removed 1 lines" not in out

    out = _plain(monkeypatch, capsys, _modify((25, 26)))
    assert "Added 2 lines" in out and "removed 2 lines" in out

    js = re.sub(r"\s+", "", _SCRIPT_JS.read_text(encoding="utf-8"))
    assert "d.added===1?'':'s'" in js
    assert "d.removed===1?'':'s'" in js


def test_the_cli_inline_echo_shows_the_paired_modified_count(monkeypatch, capsys):
    """"Yellow = modifications" — the one place it was missing.

    The viewer header had `~N` and the browser had `.dst-mod`; the inline echo,
    which is what a user actually sees during a turn, had neither. It is the PAIRED
    count and it is suppressed when nothing was replaced.
    """
    ch = _modify((25, 26))
    out = _plain(monkeypatch, capsys, ch)
    assert f"~{ch.modified} modified" in out
    assert ch.modified == 2

    # A pure addition replaced nothing, so there is no `~` to print.
    assert "modified" not in _plain(monkeypatch, capsys, _create(20))

    # ⚠️ The browser must carry it in the COUNTS ROW, not only as a header badge.
    # It had the badge (`dst-mod`) and stopped there, so one change printed two
    # different sentences depending on which surface you read — and the badge is no
    # excuse, since `+N`/`-N` are duplicated between badge and row already.
    js = re.sub(r"\s+", "", _SCRIPT_JS.read_text(encoding="utf-8"))
    assert "if(d.modified)stats.push" in js
    assert "if(d.modified)cnt.push" in js
    # An unstyled span is an invisible requirement: "Yellow = modifications" is a
    # colour, so the class has to resolve to one.
    css = re.sub(r"\s+", "", _STYLE_CSS.read_text(encoding="utf-8"))
    assert ".dc-mod{color:var(--yw)}" in css


def test_the_cli_does_not_keep_its_own_copy_of_the_cross_file_total():
    """`render_summary` must CALL `summarize()`, never re-derive the total.

    It already imports it, and the Web UI's summary bar already goes through it.
    A local `sum(c.added ...)` here is a second declaration of the cross-file total
    living in the module that holds the first one — the same failure
    `test_the_cli_does_not_keep_its_own_copy_of_the_window` guards for the window.
    """
    from agent2.cli import diffview
    src = inspect.getsource(diffview.render_summary)
    # The docstring NAMES the anti-pattern, so assert on the body alone — a test
    # that searched the whole source would be satisfied by the warning about it.
    body = src.split('"""')[2] if src.count('"""') >= 2 else src
    assert "summarize(" in body
    assert "sum(c.added" not in body
    assert "sum(c.removed" not in body
    assert "sum(c.blocks" not in body


def test_yellow_is_the_paired_count_and_never_a_row_tag():
    """⚠️ A `mod` TAG WOULD MAKE EVERY REVERT AND EVERY PATCH REFUSE.

    "Yellow = modifications" is tempting to implement as a fifth row tag. It must
    not be, and the reason is not aesthetic: `diffs.hunks_of` reads exactly these
    four tags to rebuild each hunk's two sides, and returns `None` — refuse — on any
    other, so `revert_change` would stop being able to undo a modified line at all.
    `to_patch` would likewise stop emitting appliable patches, since a `mod` row has
    no unified-diff sigil.
    """
    assert diffs.TAGS == ("hunk", "add", "del", "ctx")
    for ch in (_create(20), _modify((25,)), _modify((5, 30)),
               compute_change("gone.py", _lines(15), None)):
        tags = {t for t, _x in ch.lines}
        assert tags <= set(diffs.TAGS), f"unknown row tag in {ch.path}: {tags}"
        assert "mod" not in tags
        assert ch.modified == min(ch.added, ch.removed)


def test_a_revert_round_trips_a_modified_line(tmp_path):
    """The round-trip the tag set is load-bearing for.

    Sabotage recipe for the test above: add a `mod` tag to `TAGS` and emit it for
    paired rows, and this test is what turns red — `hunks_of` no longer recognises
    the rows, so the undo refuses and the file keeps the change.
    """
    from agent2.cli import diffview
    f = tmp_path / "a.py"
    original = "line 1\nline 2\nline 3\nline 4\nline 5\n"
    f.write_text(original, encoding="utf-8")

    after = original.replace("line 3", "CHANGED")
    ch = compute_change(str(f), original, after)
    assert ch.added == 1 and ch.removed == 1 and ch.modified == 1
    f.write_text(after, encoding="utf-8")           # the tool runs

    assert diffview.revert_change(ch) is True
    assert f.read_text(encoding="utf-8") == original


# ── Undo: the windowed before-side is not the file ─────────────────────────────

def test_an_undo_restores_a_large_file_that_never_fitted_in_its_own_diff(tmp_path):
    """⚠️ THE BUG THIS PAIR OF FUNCTIONS EXISTS FOR — 200 lines in, 7 lines out.

    `revert_change` used to rebuild the pre-change file as
    `[t for tag, t in ch.lines if tag in ("del", "ctx")]`. Those rows are an `n=3`
    unified diff, so that expression is the whole file only when the file happened
    to fit inside its own hunks. On a 200-line file with ONE changed line it wrote
    7 lines to disk and returned True: 193 lines destroyed, reported as success.

    ⚠️ `ch.truncated` cannot be the guard, and this test asserts that too. That flag
    means "past MAX_DIFF_LINES *rendered* rows" and is False here — every ordinary
    elided diff is partial without being truncated, which is exactly why the old
    refusal looked like it covered this and did not.
    """
    from agent2.cli import diffview
    f = tmp_path / "big.py"
    original = "".join(f"line {i}\n" for i in range(1, 201))
    f.write_text(original, encoding="utf-8")

    after = original.replace("line 100\n", "CHANGED\n")
    ch = compute_change(str(f), original, after)
    assert ch.truncated is False                    # the old guard never fires here
    assert len(ch.lines) < 20                       # ... and the rows are a WINDOW
    f.write_text(after, encoding="utf-8")           # the tool runs

    assert diffview.revert_change(ch) is True
    assert f.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("mutate", [
    lambda t: t.replace("CHANGED\n", "HAND EDITED\n"),    # edited inside the hunk
    lambda t: t.replace("line 28\n", ""),                 # a context line dropped
    lambda t: "",                                         # emptied entirely
])
def test_an_undo_refuses_when_the_file_no_longer_matches_the_after_side(tmp_path, mutate):
    """⚠️ AN UNDO AGAINST A FILE WE NO LONGER RECOGNISE IS A SECOND CORRUPTION.

    The hunk's after-side is what the file is supposed to look like right now. A
    mismatch means it moved on — an `[E]` round trip in the viewer, another turn's
    write, a hand edit — so the undo refuses ENTIRELY rather than reverting the
    hunks it still recognises. `revert_text` computes on a local list and returns
    None, so nothing is written: the refusal is atomic by construction.
    """
    from agent2.cli import diffview
    f = tmp_path / "moved.py"
    original = "".join(f"line {i}\n" for i in range(1, 61))
    after = original.replace("line 30\n", "CHANGED\n")
    ch = compute_change(str(f), original, after)

    moved = mutate(after)
    f.write_text(moved, encoding="utf-8")
    assert diffview.revert_change(ch) is False
    assert f.read_text(encoding="utf-8") == moved    # untouched


def test_an_undo_is_surgical_and_leaves_untouched_regions_alone(tmp_path):
    """The whole point of reverse-applying rather than rebuilding.

    A later append the diff never saw is not part of this change, so undoing the
    change must not undo it. The old rebuild could not express that — it wrote the
    hunks and nothing else, so every line outside them was lost whether or not
    anybody had touched it.
    """
    from agent2.cli import diffview
    f = tmp_path / "grown.py"
    original = "".join(f"line {i}\n" for i in range(1, 61))
    after = original.replace("line 30\n", "CHANGED\n")
    ch = compute_change(str(f), original, after)
    f.write_text(after + "appended later\n", encoding="utf-8")

    assert diffview.revert_change(ch) is True
    assert f.read_text(encoding="utf-8") == original + "appended later\n"


def test_an_undo_puts_a_deleted_file_back(tmp_path):
    """A whole-file delete is the one hunk whose header names an insertion POINT.

    difflib writes `@@ -1,5 +0,0 @@` for it, so `new_start` is 0 rather than a
    1-based line and `revert_text` must index with it directly. Off by one here and
    the restore either refuses or lands the content one line late.
    """
    from agent2.cli import diffview
    f = tmp_path / "gone.py"
    original = "line 1\nline 2\nline 3\nline 4\nline 5\n"
    f.write_text(original, encoding="utf-8")
    ch = compute_change(str(f), original, None)
    assert ch.kind == "delete"
    f.unlink()                                      # the tool runs

    assert diffview.revert_change(ch) is True
    assert f.read_text(encoding="utf-8") == original


def test_hunks_of_refuses_rather_than_returning_an_empty_plan():
    """⚠️ `None` MEANS REFUSE, AND NEVER "NOTHING TO DO" — the consumer writes.

    A `hunks_of` that returned `[]` for rows it could not read would make
    `revert_text` return the file unchanged, which `revert_change` would then write
    back and report as a successful undo. Every unreadable shape is `None`.
    """
    unreadable = [
        FileChange(path="a.py", lines=[]),                              # no hunks
        FileChange(path="a.py", lines=[("ctx", "x")]),                  # row before @@
        FileChange(path="a.py", lines=[("hunk", "@@ nonsense @@")]),    # bad header
        FileChange(path="a.py", lines=[("hunk", "@@ -1 +1 @@"),
                                       ("mod", "x")]),                 # unknown tag
    ]
    for ch in unreadable:
        assert diffs.hunks_of(ch) is None
        assert diffs.revert_text(ch, "x\n") is None


def test_an_undo_keeps_the_file_s_own_trailing_newline_convention(tmp_path):
    """`lineterm=""` over `splitlines` cannot represent a trailing newline.

    So the undo follows the file in hand rather than inventing one. Writing
    `"\\n".join(rows) + "\\n"` unconditionally — which is what the old rebuild did —
    appends a newline to a file that never had one, and that shows up as a spurious
    one-line diff on the NEXT change to it.
    """
    from agent2.cli import diffview
    f = tmp_path / "nonl.py"
    original = "alpha\nbeta\ngamma"                 # no trailing newline
    f.write_text(original, encoding="utf-8", newline="")
    after = original.replace("beta", "BETA")
    ch = compute_change(str(f), original, after)
    f.write_text(after, encoding="utf-8", newline="")

    assert diffview.revert_change(ch) is True
    assert f.read_text(encoding="utf-8") == original


def test_the_engine_owns_the_undo_computation_and_the_renderer_only_writes():
    """One declaration: the reverse-apply is in `core/diffs.py`, not in a renderer.

    `cli/diffview.revert_change` is the disk actor — read, call, write. A second
    reconstruction living in the viewer is how the first one got there.
    """
    from agent2.cli import diffview
    body = inspect.getsource(diffview.revert_change)
    assert "revert_text(" in body
    # The exact expression that shipped the data loss, in either surface's spelling.
    assert 'tag in ("del", "ctx")' not in body
    assert "splitlines" not in body
    assert hasattr(diffs, "revert_text") and hasattr(diffs, "hunks_of")


# ── Turn scope: whose changes is the recap reporting? ──────────────────────────

def test_the_turn_recap_reports_this_turn_not_the_whole_session():
    """`store` is session-long and nothing clears it.

    Reading `.all()` at the end of a turn made turn 5 report every file touched
    since launch — "4 files changed" after a turn that changed one — while the Web
    UI's per-turn `TurnProgress` reported the truth. Two totals for one turn.
    """
    store = diffs.DiffStore()
    m0 = store.mark()
    store.add(_create(5))
    store.add(_modify((25,)))
    assert len(store.since(m0)) == 2

    m1 = store.mark()
    store.add(_create(7))
    assert len(store.since(m1)) == 1, "the second turn must not inherit the first's"
    assert len(store.since(m0)) == 3, "an older mark still covers everything after it"
    assert store.since(store.mark()) == [], "nothing has happened since 'now'"


def test_a_mark_is_not_a_length_and_survives_the_cap():
    """⚠️ WHY `mark()` EXISTS AT ALL.

    `add()` trims from the FRONT, so a length remembered before a turn is not an
    index into the list after it. A caller that saved `len(store.all())` and sliced
    from there reports the wrong turn's changes — confidently — the moment the cap
    engages. Sabotage recipe: make `mark()` return `len(self._changes)`.

    ⚠️ The overfill is the whole test. Marking at exactly `MAX_CHANGES` proves
    nothing: there the length and the sequence are the same number, so a `mark()`
    that returns the length looks right and the sabotage stays green. The mark has
    to be taken once the cap has ALREADY evicted something.
    """
    store = diffs.DiffStore()
    for _ in range(store.MAX_CHANGES + 5):          # cap already engaged
        store.add(_create(3))
    m = store.mark()
    assert len(store.all()) == store.MAX_CHANGES    # the list stopped growing…
    assert m == store.MAX_CHANGES + 5              # …the mark did not
    assert m != len(store.all())                   # a length would have been wrong here
    store.add(_modify((25,)))                       # two more, each evicting the oldest
    store.add(_modify((26,)))
    assert len(store.all()) == store.MAX_CHANGES    # length still did not move…
    assert len(store.since(m)) == 2                 # …but the mark still knows


def test_an_evicted_change_is_counted_not_silently_forgotten():
    """"Complete" is a claim the viewer makes; the cap must not make it a lie.

    A store that drops history without saying so renders an empty-looking viewer,
    and an empty viewer reads as "nothing changed".
    """
    store = diffs.DiffStore()
    assert store.dropped == 0
    for _ in range(store.MAX_CHANGES + 5):
        store.add(_create(3))
    assert len(store.all()) == store.MAX_CHANGES
    assert store.dropped == 5


def test_clearing_is_not_blamed_on_the_cap():
    """`clear()` is a user-driven reset, not a silent loss.

    Folding it into `dropped` would have the viewer report "5 earlier changes
    dropped — session cap" at someone who asked for the reset.
    """
    store = diffs.DiffStore()
    for _ in range(3):
        store.add(_create(3))
    store.clear()
    assert store.all() == []
    assert store.dropped == 0


def test_a_mark_this_store_never_issued_degrades_to_the_whole_session():
    """Totality: a junk mark must not make the recap silently vanish.

    The recap is gated on `files > 0`, so returning `[]` for an unrecognised mark
    would delete the whole summary with no sign anything went wrong. Returning
    everything is the answer callers had before marks existed.
    """
    store = diffs.DiffStore()
    store.add(_create(5))
    assert len(store.since(None)) == 1
    assert len(store.since("garbage")) == 1
    assert len(store.since(-7)) == 1
    assert store.since(10 ** 9) == []


# ── The call sites (structural guards) ─────────────────────────────────────────

def _funcs_calling(path: Path, needle: str) -> list[ast.FunctionDef]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for c in ast.walk(node):
                if isinstance(c, ast.Call) and needle in ast.dump(c.func):
                    out.append(node)
                    break
    return out


def _call_lines(node, needle: str) -> list[int]:
    return [c.lineno for c in ast.walk(node)
            if isinstance(c, ast.Call) and needle in ast.dump(c.func)]


@pytest.mark.parametrize("path", [_AGENT_PY, _PROVIDER_AGENT, _AGENT2CLI],
                         ids=["agent", "provider_agent", "cli"])
def test_capture_stays_above_dispatch_at_every_call_site(path):
    """⚠️ THE ORDERING GUARD THE MODULE HEADER ASKS FOR AND NOTHING ENFORCED.

    `write_file` overwrites; capture after dispatch and the "before" side is the
    file the tool just wrote, so every write renders as a pure addition. There is
    no error — just a wrong diff, on every surface, which is the worst kind. Until
    this test, `test_capture_reads_the_file_before_the_write` pinned the HELPER's
    semantics while the four call sites were free to invert. AST rather than grep
    because both calls sit inside 400-line functions where a text search proves
    nothing about their order. Same precedent as `test_run_setup.py`.
    """
    funcs = _funcs_calling(path, "capture_for")
    assert funcs, f"no function in {path.name} calls capture_for — this test lost its target"
    for fn in funcs:
        cap = min(_call_lines(fn, "capture_for"))
        for line in _call_lines(fn, "dispatch_tool"):
            assert line > cap, (
                f"{path.name}:{fn.name} dispatches at line {line} before capturing at {cap} — "
                "the diff would be computed against the file the tool already wrote")


def test_a_delete_is_rendered_and_not_only_recorded():
    """The spec says "whenever Agent2 changes a file". A delete is a change.

    The CLI recorded a delete into the store and printed one grey status line,
    while the browser — whose emit is tool-agnostic — showed the full diff block.
    Source-level because the branch lives inside a 400-line function: grepping the
    whole file for `render_change` is satisfied by the `write_file` branch.
    """
    tree = ast.parse(_AGENT2CLI.read_text(encoding="utf-8"))
    branches = [n for n in ast.walk(tree)
                if isinstance(n, ast.If) and "delete_file" in ast.dump(n.test)]
    assert branches, "no delete_file branch in agent2cli.py — this test lost its target"

    recording = [b for b in branches
                 if "store" in ast.dump(b) and "'add'" in ast.dump(b).replace('"', "'")]
    assert recording, "no delete branch records into the store any more"
    for b in recording:
        body = ast.dump(ast.Module(body=b.body, type_ignores=[]))
        assert "render_change" in body, \
            "a delete branch records the change without ever showing it"

    # ⚠️ The other half, and the shape the bug actually had: a render GUARDED on
    # "this is not a delete". Both echo paths carried it, so lifting it in one
    # place left custom providers as the surface that still hid a deletion.
    for n in branches:
        if "NotEq" not in ast.dump(n.test):
            continue
        guarded = ast.dump(ast.Module(body=n.body, type_ignores=[]))
        assert "render_change" not in guarded, \
            "a diff render is gated on the change not being a delete"
