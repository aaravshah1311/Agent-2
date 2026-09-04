# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""Tests for `agent2/cli/diffview.py`'s full-screen viewer (Ctrl+B, Task 12).

⚠️ WHY THIS FILE EXISTS SEPARATELY FROM `test_diffs.py`.
`test_diffs.py` covers the ENGINE — what a diff is, what it totals to, which rows
it emits. Nothing there ever entered the viewer, and the viewer is where the only
DESTRUCTIVE key in the whole diff subsystem lives (`r` → `revert_change` → a real
`write_text`). The invariant this file exists for is the one a reader cannot check
by looking: **opening a review surface must never change anything.**

The harness drives the real key handlers. `_run_viewer` builds them as closures and
then blocks in `Application.run()`, so the fake `Application` below plays a scripted
key sequence through the actual `KeyBindings` object and captures what `render()`
would have drawn. Nothing here needs a terminal: `_visible_rows`/`_width` fall back
to fixed sizes when `get_app()` raises, which is exactly what happens off-tty.
"""

import ast
import re
from pathlib import Path

import pytest

from agent2.cli import diffview
from agent2.core import diffs
from agent2.core.diffs import compute_change

_DIFFVIEW = Path(__file__).resolve().parent.parent.parent / "agent2" / "cli" / "diffview.py"


# ── Harness ───────────────────────────────────────────────────────────────────

def _parse(key: str):
    """Translate a key name the way prompt_toolkit does, so lookups match."""
    from prompt_toolkit.key_binding.key_bindings import _parse_key
    return _parse_key(key)


class _Ctl:
    """Stands in for `FormattedTextControl` — keeps the `render` callable."""
    last = None

    def __init__(self, fn, *a, **kw):
        _Ctl.last = fn


class _Ev:
    def __init__(self, app, data):
        self.app = app
        self.data = data


class _FakeApp:
    """Plays one scripted key sequence, then returns from `run()`.

    `scripts` is a list of sequences — one per `Application` the code builds — so an
    `E` round trip (which exits, edits, and re-enters) can be scripted end to end.
    """
    scripts: list[list[str]] = []
    frames: list[str] = []          # flattened render output after every key
    built: int = 0

    def __init__(self, **kw):
        self.kb = kw["key_bindings"]
        self.render = _Ctl.last
        self._done = False
        idx = _FakeApp.built
        _FakeApp.built += 1
        self.script = list(_FakeApp.scripts[idx]) if idx < len(_FakeApp.scripts) else []

    def exit(self, *a, **kw):
        self._done = True

    def _draw(self):
        try:
            out = self.render()
        except Exception as exc:                       # pragma: no cover
            pytest.fail(f"render() raised: {exc!r}")
        text = "".join(t for _style, t in out)
        _FakeApp.frames.append(text)
        return text

    def run(self, *a, **kw):
        self._draw()                                   # the first paint
        for key in self.script:
            if self._done:
                break
            matches = self.kb.get_bindings_for_keys((_parse(key),))
            if not matches:
                continue
            # prompt_toolkit dispatches the LAST match; a literal key sorts after
            # `<any>`, which is why `E` needs its `_typing` guard.
            matches[-1].handler(_Ev(self, key))
            self._draw()
        return None


@pytest.fixture
def viewer(monkeypatch):
    """Install the fake app/control and return a `play(changes, *scripts)` driver."""
    monkeypatch.setattr(diffview, "Application", _FakeApp)
    monkeypatch.setattr(diffview, "FormattedTextControl", _Ctl)
    monkeypatch.setattr(diffview, "Window", lambda *a, **k: None)
    monkeypatch.setattr(diffview, "HSplit", lambda *a, **k: None)
    monkeypatch.setattr(diffview, "Layout", lambda *a, **k: None)
    monkeypatch.setattr(diffview, "_PTK", True)
    monkeypatch.setattr(diffview, "_RICH", False)
    from agent2.cli import render as _render
    monkeypatch.setattr(_render, "_RICH", False)

    def play(changes, *scripts, **kw):
        _FakeApp.scripts = [list(s) for s in scripts] or [[]]
        _FakeApp.frames = []
        _FakeApp.built = 0
        diffview.open_viewer(changes, **kw)
        return _FakeApp.frames

    return play


def _tmp_change(tmp_path, name="a.py"):
    """A real file on disk plus the change that produced it."""
    f = tmp_path / name
    before = "line 1\nline 2\nline 3\nline 4\nline 5\n"
    after = before.replace("line 3", "CHANGED")
    f.write_text(before, encoding="utf-8")
    ch = compute_change(str(f), before, after)
    f.write_text(after, encoding="utf-8")              # the tool already ran
    return f, ch, before, after


def _no_writes(monkeypatch):
    """Make any filesystem mutation an immediate test failure."""
    def _boom(self, *a, **kw):
        pytest.fail(f"the viewer wrote to disk: {self}")
    monkeypatch.setattr(Path, "write_text", _boom)
    monkeypatch.setattr(Path, "write_bytes", _boom)
    monkeypatch.setattr(Path, "unlink", _boom)


# ── The invariant the whole file exists for ───────────────────────────────────

def test_opening_the_viewer_never_writes_to_disk(tmp_path, viewer, monkeypatch):
    """⚠️ THERE IS NO APPROVAL GATE, so a viewer that can write is a gate nobody
    designed and nobody can see.

    Every path from `open_viewer` to the first paint must be a read. Driven with
    the whole navigation vocabulary — modes, scroll, search, copy — because the
    hazard is not `r` (which is deliberate) but some *other* key quietly reaching
    `revert_change` or `save_patch`.
    """
    f, ch, before, after = _tmp_change(tmp_path)
    monkeypatch.setattr(diffview, "copy_to_clipboard", lambda _t: True)
    _no_writes(monkeypatch)
    viewer([ch], ["f", "d", "s", "u", "c", "e", "e", "down", "up", "pagedown",
                  "home", "end", "left", "right", "/", "a", "n", "p", "escape",
                  "x", "y", "q"])
    assert f.read_text(encoding="utf-8") == after


def test_the_fallback_dump_writes_nothing_either(tmp_path, monkeypatch, capsys):
    """The no-prompt_toolkit path is a different function; it gets the same rule."""
    f, ch, _before, after = _tmp_change(tmp_path)
    monkeypatch.setattr(diffview, "_PTK", False)
    monkeypatch.setattr(diffview, "_RICH", False)
    _no_writes(monkeypatch)
    diffview.open_viewer([ch])
    assert f.read_text(encoding="utf-8") == after
    assert "a.py" in capsys.readouterr().out


# ── [A] Accept / [R] Reject ───────────────────────────────────────────────────

def test_accept_writes_nothing_and_says_so(tmp_path, viewer, monkeypatch, capsys):
    """"Approved" alone implies a gate. The toast has to deny one."""
    f, ch, _b, after = _tmp_change(tmp_path)
    _no_writes(monkeypatch)
    frames = viewer([ch], ["a", "q"])
    assert "nothing written" in frames[1]
    assert "approved" in frames[1]                 # the header badge
    assert f.read_text(encoding="utf-8") == after
    # …and the scrollback trace refuses to read like a decision. Normalised,
    # because Rich soft-wraps and the line is longer than a narrow terminal.
    out = re.sub(r"\s+", " ", capsys.readouterr().out)
    assert "Review marks" in out and "the review wrote nothing" in out


def test_reject_alone_does_not_revert_the_file(tmp_path, viewer, monkeypatch):
    """`x` is the mark-only alias that shipped; it must stay mark-only."""
    f, ch, _b, after = _tmp_change(tmp_path)
    _no_writes(monkeypatch)
    frames = viewer([ch], ["x", "q"])
    assert "rejected" in frames[1]
    assert "nothing written" in frames[1]
    assert f.read_text(encoding="utf-8") == after


def test_reject_requires_a_second_key_before_it_rewrites_the_file(tmp_path, viewer):
    """⚠️ `r` USED TO REVERT ON THE FIRST PRESS while the spec's legend called it
    "Reject" — so a user following the legend rewrote their file expecting a label.

    One press marks and arms; the second one acts. Sabotage recipe: revert on the
    first press. Every other test in this file still passes.
    """
    f, ch, before, after = _tmp_change(tmp_path)

    frames = viewer([ch], ["r", "q"])
    assert f.read_text(encoding="utf-8") == after, "one press must not touch disk"
    assert "press r again" in frames[1]
    assert "rejected" in frames[1]

    frames = viewer([ch], ["r", "r", "q"])
    assert f.read_text(encoding="utf-8") == before, "the second press is the action"
    assert "reverted on disk" in frames[2]


def test_any_other_key_cancels_the_armed_revert(tmp_path, viewer):
    """"any other key cancels" is a promise the footer makes; `_typing` keeps it.

    It is kept in ONE place on purpose — every single-letter binding funnels
    through `_typing`, so a new key cannot forget to disarm.

    ⚠️ THE FOURTH PRESS IS THE WHOLE TEST. Stopping after `r d r` proves nothing:
    a `_typing` that never disarms ALSO leaves the file untouched and ALSO prints
    "press r again", because `_was_armed` stays `None` and every press just re-arms
    forever. Cancelled and permanently-dead look identical from there. The extra
    `r` is what separates them — it must fire.
    """
    f, ch, before, after = _tmp_change(tmp_path)

    frames = viewer([ch], ["r", "d", "r", "q"])
    assert f.read_text(encoding="utf-8") == after, "a key in between must disarm"
    assert "press r again" in frames[3], "the third press re-arms rather than acting"

    frames = viewer([ch], ["r", "d", "r", "r", "q"])
    assert f.read_text(encoding="utf-8") == before, "the re-armed pair still fires"
    assert "reverted on disk" in frames[4]


def test_arming_one_file_cannot_revert_another(tmp_path, viewer):
    """⚠️ The arm holds a FILE INDEX, not a bare flag.

    With a flag, `r` on one file then `→` then `r` on the next would revert the
    NEXT one on what the user read as a first press.

    ⚠️ BOTH DIRECTIONS ARE LOAD-BEARING, because the two ways of getting this
    wrong fail on opposite files. `armed = True` betrays itself going 0 → 1
    (`not True` is False, so the second press fires). An index tested for
    truthiness instead of equality — `if not armed` — survives that, because file
    0 is a FALSY index and `not 0` still reads as "nothing armed"; it only betrays
    itself coming back the other way, 1 → 0.
    """
    f1, ch1, _before1, after1 = _tmp_change(tmp_path, "one.py")
    f2, ch2, before2, after2 = _tmp_change(tmp_path, "two.py")

    frames = viewer([ch1, ch2], ["r", "right", "r", "q"])
    assert f1.read_text(encoding="utf-8") == after1
    assert f2.read_text(encoding="utf-8") == after2
    assert "press r again" in frames[3]

    frames = viewer([ch1, ch2], ["right", "r", "left", "r", "q"])
    assert f1.read_text(encoding="utf-8") == after1
    assert f2.read_text(encoding="utf-8") == after2
    assert "press r again" in frames[4]

    # …and arming on a non-zero index still FIRES, so an `r` that can never act
    # cannot pass the two checks above by simply doing nothing.
    viewer([ch1, ch2], ["right", "r", "r", "q"])
    assert f2.read_text(encoding="utf-8") == before2
    assert f1.read_text(encoding="utf-8") == after1, "only the armed file"


def test_reject_refuses_on_a_truncated_diff(tmp_path, viewer, monkeypatch):
    """A truncated diff's before-side is incomplete; writing it would destroy the
    part that was never captured. `revert_change` refuses — the viewer must say
    WHY rather than presenting a silent no-op."""
    f, ch, _b, after = _tmp_change(tmp_path)
    ch.truncated = True
    _no_writes(monkeypatch)
    frames = viewer([ch], ["r", "r", "q"])
    assert f.read_text(encoding="utf-8") == after
    assert "truncated" in frames[1]
    assert "rejected" in frames[1], "refusing the undo must not refuse the mark"


def test_reverting_a_created_file_deletes_it_and_still_takes_two_presses(tmp_path,
                                                                        viewer):
    """⚠️ UNDOING A `create` IS A `unlink`, NOT A REWRITE — the most destructive
    branch in the whole diff subsystem.

    `test_diffs.py` covers the modify round trip and nothing covered this one, yet
    it is reached by the same two keys. Rule 21 applies hardest here: the file the
    agent made is the file a mistaken keypress destroys.
    """
    f = tmp_path / "new.py"
    ch = compute_change(str(f), None, "fresh\n")
    f.write_text("fresh\n", encoding="utf-8")           # the tool already ran
    assert ch.kind == "create"

    viewer([ch], ["r", "q"])
    assert f.exists(), "one press must not delete the file"

    frames = viewer([ch], ["r", "r", "q"])
    assert not f.exists()
    assert "reverted on disk" in frames[2]


def test_copy_and_patch_export_report_whether_they_worked(tmp_path, viewer,
                                                          monkeypatch):
    """The spec lists copy as a viewer capability. Neither `y` nor `w` leaves any
    trace on screen, so a silent failure is indistinguishable from success —
    a headless box has no clipboard and that has to be said, not swallowed.
    """
    _f, ch, _b, _after = _tmp_change(tmp_path)
    grabbed = {}

    def _copy(text):
        grabbed["text"] = text
        return True

    monkeypatch.setattr(diffview, "copy_to_clipboard", _copy)
    frames = viewer([ch], ["y", "q"])
    assert "copied to clipboard" in frames[1]
    assert grabbed["text"].startswith("diff --git ")
    assert "--- a/" in grabbed["text"] and "+++ b/" in grabbed["text"]

    monkeypatch.setattr(diffview, "copy_to_clipboard", lambda _t: False)
    frames = viewer([ch], ["y", "q"])
    assert "no clipboard available" in frames[1]

    # `w` exports EVERY change, not just the current file — a patch of one file
    # out of six is almost never what "save the patch" means.
    monkeypatch.chdir(tmp_path)
    _f2, ch2, _b2, _a2 = _tmp_change(tmp_path, "two.py")
    frames = viewer([ch, ch2], ["w", "q"])
    assert "patch saved" in frames[1]
    written = sorted(tmp_path.glob("agent2-*.patch"))
    assert len(written) == 1
    body = written[0].read_text(encoding="utf-8")
    assert body.count("diff --git ") == 2

    # ⚠️ And the failure half. Asserting only the happy path leaves "patch saved"
    # printable over a write that never happened — a user then goes looking for a
    # file that does not exist, which is worse than being told it failed.
    monkeypatch.setattr(diffview, "save_patch", lambda _c: None)
    frames = viewer([ch, ch2], ["w", "q"])
    assert "could not write the patch" in frames[1]
    assert len(sorted(tmp_path.glob("agent2-*.patch"))) == 1, "no second file"


# ── [E] Edit ──────────────────────────────────────────────────────────────────

def test_edit_does_not_record_the_humans_edit_as_an_agent_change(tmp_path, viewer,
                                                                monkeypatch):
    """⚠️ `store` IS "WHAT THE AGENT CHANGED".

    Capturing the user's own edit there would make `revert_change`'s reconstructed
    before-side wrong — an undo would then restore a state that never existed. The
    edit is still visible (`f` re-reads disk); it just is not claimed as the
    agent's.
    """
    f, ch, _b, _after = _tmp_change(tmp_path)
    store = diffs.DiffStore()
    store.add(ch)
    monkeypatch.setattr(diffview, "store", store)

    def _edit(path):
        Path(path).write_text("the human's own version\n", encoding="utf-8")
        return True

    monkeypatch.setattr(diffview, "open_in_editor", _edit)
    frames = viewer([ch], ["E"], ["q"])
    assert len(store.all()) == 1, "a human edit must not become an agent change"
    assert f.read_text(encoding="utf-8") == "the human's own version\n"
    # The re-entered viewer says so, so the diff on screen is not misread as ours.
    assert "edited in your editor" in frames[2]     # the re-entry toast
    assert "[edited here]" in frames[-1]            # the header, which outlives it


def test_review_marks_survive_an_editor_round_trip(tmp_path, viewer, monkeypatch):
    """The round trip rebuilds viewer state; losing the ✓ badges would read as
    "my marks were thrown away"."""
    _f, ch, _b, _after = _tmp_change(tmp_path)
    monkeypatch.setattr(diffview, "open_in_editor", lambda _p: True)
    frames = viewer([ch], ["a", "E"], ["q"])
    assert "approved" in frames[-1]


def test_a_missing_editor_is_reported_and_the_review_continues(tmp_path, viewer,
                                                              monkeypatch):
    """No `$EDITOR` is a normal state on a fresh box. It must not close the
    review the user was in the middle of."""
    _f, ch, _b, _after = _tmp_change(tmp_path)
    monkeypatch.setattr(diffview, "open_in_editor", lambda _p: False)
    # Second script is empty on purpose: the frame under test is the re-entry's
    # first paint, and any keypress clears the toast (that is `_typing`'s job).
    frames = viewer([ch], ["E"], [])
    assert "set $EDITOR" in frames[-1]
    assert "[edited here]" not in frames[-1], "a failed launch is not an edit"


def test_the_editor_is_never_launched_from_inside_a_key_binding():
    """⚠️ THE EDITOR NEEDS THE TERMINAL THE APP IS HOLDING.

    prompt_toolkit only leaves the alternate screen and restores the cursor inside
    `renderer.reset()` on the way out of `run()`. A child spawned from a key
    handler paints into the viewer's alternate screen and fights it for the
    raw-mode console. So `E` may only set an action; `open_viewer` launches.
    """
    tree = ast.parse(_DIFFVIEW.read_text(encoding="utf-8"))
    inner = next(n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == "_run_viewer")
    calls = [n.func.id for n in ast.walk(inner)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    assert "open_in_editor" not in calls
    outer = next(n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == "open_viewer")
    outer_calls = [n.func.id for n in ast.walk(outer)
                   if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    assert "open_in_editor" in outer_calls


def test_the_editor_choice_is_visual_then_editor_then_platform_default(monkeypatch):
    """THE one declaration of "which editor a human gets".

    A second copy of this precedence elsewhere would disagree the moment a user
    sets only one of the two variables.
    """
    seen = {}

    def _call(argv, *a, **kw):
        seen["argv"] = list(argv)
        return 0

    monkeypatch.setattr(diffview.subprocess, "call", _call)

    monkeypatch.setenv("VISUAL", "myvis")
    monkeypatch.setenv("EDITOR", "myed")
    assert diffview.open_in_editor("f.py") is True
    assert seen["argv"] == ["myvis", "f.py"]

    seen.clear()
    monkeypatch.delenv("VISUAL")
    assert diffview.open_in_editor("f.py") is True
    assert seen["argv"] == ["myed", "f.py"]

    # Flags in the variable are normal (`code -w`) and must not become one argv0.
    seen.clear()
    monkeypatch.setenv("EDITOR", "code -w")
    diffview.open_in_editor("f.py")
    assert seen["argv"] == ["code", "-w", "f.py"]

    seen.clear()
    monkeypatch.delenv("EDITOR")
    diffview.open_in_editor("f.py")
    assert seen["argv"][0] in ("notepad", "vi")
    assert seen["argv"][-1] == "f.py"


def test_a_broken_editor_command_is_false_not_an_exception(monkeypatch):
    """Nothing in this module may raise into a turn — the module header's rule."""
    def _boom(*a, **kw):
        raise OSError("no such file")
    monkeypatch.setattr(diffview.subprocess, "call", _boom)
    monkeypatch.setenv("EDITOR", "does-not-exist")
    assert diffview.open_in_editor("f.py") is False
    assert diffview.open_in_editor("") is False


# ── Honest completeness ───────────────────────────────────────────────────────

def test_the_viewer_declares_the_changes_the_cap_ate(tmp_path, viewer, monkeypatch):
    """"Complete current-session diff" is the claim Ctrl+B makes; `MAX_CHANGES` is
    what can quietly make it false.

    `hidden` (windowed), `truncated` (engine limit) and `dropped` (session cap) are
    three different facts, and this is the third one's only surface.
    """
    store = diffs.DiffStore()
    for i in range(store.MAX_CHANGES + 3):
        store.add(compute_change(f"f{i}.py", None, "x\n"))
    monkeypatch.setattr(diffview, "store", store)
    frames = viewer(None, ["q"])
    assert "3 earlier changes dropped" in frames[0]
    assert f"session cap {store.MAX_CHANGES}" in frames[0]


def test_the_cap_is_not_blamed_for_a_caller_supplied_list(tmp_path, viewer,
                                                          monkeypatch):
    """A caller that passed its own list is showing something else; reporting the
    store's eviction count over it would be a second, wrong claim."""
    _f, ch, _b, _after = _tmp_change(tmp_path)
    store = diffs.DiffStore()
    for i in range(store.MAX_CHANGES + 3):
        store.add(compute_change(f"f{i}.py", None, "x\n"))
    monkeypatch.setattr(diffview, "store", store)
    frames = viewer([ch], ["q"])
    assert "dropped" not in frames[0]


def test_an_empty_viewer_still_admits_what_the_cap_ate(monkeypatch, capsys):
    """⚠️ THIS IS THE CASE THE WARNING EXISTS FOR. An empty viewer reads as
    "nothing changed"; if everything was evicted, that is precisely wrong."""
    store = diffs.DiffStore()
    for i in range(store.MAX_CHANGES + 2):
        store.add(compute_change(f"f{i}.py", None, "x\n"))
    store.clear()          # ⚠️ `clear()` does not reset `dropped` — see DiffStore
    assert store.dropped == 2
    monkeypatch.setattr(diffview, "store", store)
    monkeypatch.setattr(diffview, "_RICH", False)
    diffview.open_viewer()
    out = capsys.readouterr().out
    assert "No file changes" in out and "2 earlier change(s) dropped" in out


# ── Claims the source itself makes ────────────────────────────────────────────

def test_the_viewers_dual_mode_claim_matches_what_dual_mode_actually_does():
    """⚠️ THE COMMENT USED TO BE FALSE, AND THE LIE WAS THE DANGEROUS KIND.

    It said Ctrl+B showed changes made from the browser too. It does not:
    `agent2dual.py` launches the CLI as a CHILD PROCESS, so the two halves hold
    two disjoint `DiffStore`s. A user who believed the comment would read an empty
    viewer as "nothing changed" while the browser half was mid-edit.

    Asserting the corrected comment alone would be circular, so the second half
    checks the fact it rests on — dual mode really does spawn a process.
    """
    flat = re.sub(r"\s+", " ", _DIFFVIEW.read_text(encoding="utf-8"))
    assert "IS PROCESS-LOCAL AND IS **NOT** SHARED WITH THE WEB UI" in flat
    assert "CHILD PROCESS" in flat

    dual = (_DIFFVIEW.parent.parent.parent / "agent2dual.py").read_text(encoding="utf-8")
    assert re.search(r"subprocess\.(run|Popen|call)\(\s*\[\s*sys\.executable", dual), \
        "the comment's premise is gone — recheck whether the stores are still split"


def test_every_single_letter_binding_falls_through_while_searching():
    """⚠️ `_typing` IS BOTH THE SEARCH FALL-THROUGH AND THE REVERT DISARM.

    A single-letter handler that skips it does two things wrong at once: it eats a
    character the user was typing into the search box, and it leaves `r` armed so a
    later `r` reverts a file the user thought they had cancelled. `E` is the one
    that makes this urgent — a literal binding beats `<any>` in prompt_toolkit, so
    typing "Editing" into the search box would otherwise launch an editor.
    """
    src = _DIFFVIEW.read_text(encoding="utf-8")
    tree = ast.parse(src)
    inner = next(n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == "_run_viewer")
    checked = []
    for node in ast.walk(inner):
        if not isinstance(node, ast.FunctionDef):
            continue
        keys = [d.args[0].value for d in node.decorator_list
                if isinstance(d, ast.Call)
                and isinstance(d.func, ast.Attribute) and d.func.attr == "add"
                and d.args and isinstance(d.args[0], ast.Constant)
                and isinstance(d.args[0].value, str)]
        letters = [k for k in keys if len(k) == 1 and k.isalnum()]
        if not letters:
            continue
        first = node.body[0] if not isinstance(node.body[0], ast.Expr) else (
            node.body[1] if len(node.body) > 1 else node.body[0])
        guard = ast.dump(first)
        assert "_typing" in guard, f"binding {letters} does not fall through _typing"
        checked.extend(letters)
    assert set("acdefnpqrsuwxyE") <= set(checked), f"only checked {sorted(checked)}"


def test_ctrl_b_opens_the_viewer_and_is_advertised_as_a_prompt_shortcut():
    """Mid-turn Ctrl+B is swallowed by `runtime.InputController` and deliberately
    stays that way — `statusbar.py` states why. The help text is the only place a
    user learns the split, so it must not promise the other thing.

    The binding is also checked to be DRIVEN BY `handlers`, not hardcoded: an
    unsupported surface passes no `diff` handler and must get no Ctrl+B.
    """
    from agent2.cli import statusbar
    entry = dict(statusbar.SHORTCUTS)["Ctrl+B"]
    assert "at the prompt" in entry

    wired = statusbar.build_key_bindings({"diff": lambda: None})
    assert wired.get_bindings_for_keys((_parse("c-b"),))
    bare = statusbar.build_key_bindings({})
    assert not bare.get_bindings_for_keys((_parse("c-b"),))
