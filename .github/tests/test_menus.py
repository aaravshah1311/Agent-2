# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""Tests for the ONE ephemeral menu system — `agent2/cli/palette.py` (Task 13).

⚠️ WHY THIS FILE EXISTS.
The reported bug was "the user selects an option but the menu stays printed".
That is not a rendering bug, it is a *duplication* bug: `interactive.py` carried a
second prompt_toolkit menu that was palette's renderer minus `erase_when_done=True`,
and `ephemeral_picker` falls back to it on ANY exception. So the failure mode was a
picker that worked perfectly, then one raised exception later stopped erasing — no
traceback, no clue, exactly the reported symptom. Two renderers for one widget is
the defect; the tests below pin both halves of the fix:

  * the **shape** — every overlay in `palette.py` is built by `_menu_app`, and that
    one function sets `erase_when_done=True`, so a menu added tomorrow is ephemeral
    by construction rather than by remembering (`test_*_erase*`, `test_*_menu_app*`);
  * the **absence** — `interactive.py`'s two functions are plain `print()`/`input()`
    and may never grow a `prompt_toolkit.Application` again. That is checked by
    walking the AST, because a *near*-copy is what drifts, and a near-copy passes
    every behavioural test written against the original (`test_the_fallback_*`).

The geometry tests are a second, independent way for a menu to "stay on screen":
prompt_toolkit clips an inline app to `size.rows`, and `erase_when_done` can only
walk back over the region it actually drew. A body taller than the terminal is
therefore both unnavigable AND un-erasable — see `_menu_window`.

The harness drives the REAL render closures and the REAL `KeyBindings` objects; only
prompt_toolkit's `Application`/`Window` are faked, so nothing here needs a terminal.
"""

import ast
from pathlib import Path

import pytest

from agent2.cli import interactive, palette

_SRC = Path(__file__).resolve().parent.parent.parent / "agent2" / "cli"
_INTERACTIVE = _SRC / "interactive.py"
_PALETTE = _SRC / "palette.py"

# The three fragments that, together, ARE a prompt_toolkit menu. Any one of them
# inside `interactive.py`'s fallbacks means the deleted copy has come back.
_PTK_WIDGETS = {"Application", "KeyBindings", "FormattedTextControl", "Layout"}

# Every overlay in `palette.py`. Each must route through `_menu_app`.
_OVERLAYS = ("_run_ephemeral_picker", "_run_palette", "_run_toggle_menu")


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

    Records the kwargs of every `Application` built and every `Window` built, which
    is how the erase-and-geometry contracts are checked on the REAL overlays rather
    than on a re-implementation of them.
    """
    script: list[str] = []
    frames: list[list[tuple]] = []      # raw fragment lists, one per paint
    app_kwargs: list[dict] = []
    win_kwargs: list[dict] = []

    def __init__(self, **kw):
        _FakeApp.app_kwargs.append(kw)
        self.kb = kw["key_bindings"]
        self.render = _Ctl.last
        self._done = False

    def exit(self, *a, **kw):
        self._done = True

    def _draw(self):
        try:
            out = self.render()
        except Exception as exc:                       # pragma: no cover
            pytest.fail(f"render() raised: {exc!r}")
        _FakeApp.frames.append(list(out))
        return out

    def run(self, *a, **kw):
        self._draw()                                   # the first paint
        for key in _FakeApp.script:
            if self._done:
                break
            matches = self.kb.get_bindings_for_keys((_parse(key),))
            if not matches:
                continue
            matches[-1].handler(_Ev(self, key))
            self._draw()
        return None


@pytest.fixture
def menus(monkeypatch):
    """Install the fakes; return a `play(fn, keys)` driver over the real overlays."""
    def _win(*a, **kw):
        _FakeApp.win_kwargs.append(kw)
        return None

    monkeypatch.setattr(palette, "Application", _FakeApp)
    monkeypatch.setattr(palette, "FormattedTextControl", _Ctl)
    monkeypatch.setattr(palette, "Window", _win)
    monkeypatch.setattr(palette, "HSplit", lambda *a, **k: None)
    monkeypatch.setattr(palette, "Layout", lambda *a, **k: None)
    monkeypatch.setattr(palette, "_PTK", True)

    def play(fn, keys=(), **kw):
        _FakeApp.script = list(keys)
        _FakeApp.frames = []
        _FakeApp.app_kwargs = []
        _FakeApp.win_kwargs = []
        result = fn(**kw)
        return result, _FakeApp.frames

    return play


def _opts(n=3):
    return [{"value": f"v{i}", "label": f"Option {i}", "hint": f"hint {i}"}
            for i in range(n)]


def _items(n=3):
    return [{"key": f"k{i}", "label": f"Server {i}", "on": False} for i in range(n)]


def _text(frag_list):
    return "".join(t for _s, t in frag_list)


def _cursor_rows(frag_list):
    """Indices of the fragments that carry the scroll marker."""
    return [i for i, (style, _t) in enumerate(frag_list) if style == palette.CURSOR]


def _fn(src_path: Path, name: str) -> ast.FunctionDef:
    tree = ast.parse(src_path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    pytest.fail(f"{name}() not found in {src_path.name}")


def _called_names(node: ast.AST) -> set[str]:
    """Every plain-name callee inside *node* (nested functions included)."""
    out = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
            out.add(sub.func.id)
    return out


# ── The one-renderer rule ─────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["select_from_list", "toggle_menu"])
def test_the_fallback_menus_contain_no_prompt_toolkit_application(name):
    """⚠️ THE REGRESSION GUARD. `interactive.py`'s two menus are text, forever.

    They are the fallback `palette` degrades TO — including on the exception path —
    so a prompt_toolkit menu here is a menu nobody thinks about, on exactly the
    installs nobody tests. It shipped once without `erase_when_done` and that is
    the Task 13 bug.
    """
    found = _called_names(_fn(_INTERACTIVE, name)) & _PTK_WIDGETS
    assert not found, (
        f"interactive.{name}() builds prompt_toolkit widgets {sorted(found)} — "
        "the graphical form belongs to agent2/cli/palette.py and nowhere else"
    )


@pytest.mark.parametrize("name", ["select_from_list", "toggle_menu"])
def test_the_fallback_menus_really_are_print_and_input(name):
    """Positive half of the rule above: they must still ASK, not just not-render.

    A guard that only forbids `Application` is satisfied by a function that was
    gutted to `return None`, which would make `/addapi` unusable on a
    half-installed machine — the reason the fallback exists at all.
    """
    called = _called_names(_fn(_INTERACTIVE, name))
    assert "input" in called
    assert "print" in called


def test_palette_builds_exactly_one_application():
    """⚠️ ONE `Application` CONSTRUCTION IN THE WHOLE MODULE, inside `_menu_app`.

    The count is the test. Three overlays with three constructors all look right in
    review and drift one kwarg at a time; `erase_when_done` is precisely the kwarg
    that went missing last time.
    """
    tree = ast.parse(_PALETTE.read_text(encoding="utf-8"))
    sites = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "Application"
    ]
    assert len(sites) == 1, f"expected 1 Application(...) in palette.py, found {len(sites)}"

    inside = _called_names(_fn(_PALETTE, "_menu_app"))
    assert "Application" in inside, "the one Application(...) must live in _menu_app()"


def test_palette_builds_exactly_one_window():
    """Same argument for geometry: one `Window`, inside `_menu_window`."""
    tree = ast.parse(_PALETTE.read_text(encoding="utf-8"))
    sites = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "Window"
    ]
    assert len(sites) == 1, f"expected 1 Window(...) in palette.py, found {len(sites)}"
    assert "Window" in _called_names(_fn(_PALETTE, "_menu_window"))


@pytest.mark.parametrize("overlay", _OVERLAYS)
def test_every_overlay_goes_through_menu_app(overlay):
    """Each overlay asks `_menu_app` for its application and builds none itself."""
    called = _called_names(_fn(_PALETTE, overlay))
    assert "_menu_app" in called, f"{overlay}() must build its app via _menu_app()"
    assert "Application" not in called
    assert "Window" not in called


# ── Erase-when-done: the actual Task 13 fix ───────────────────────────────────

def test_menu_app_erases_itself_and_stays_inline(menus):
    """`erase_when_done=True` IS the feature; `full_screen=False` keeps it inline."""
    from prompt_toolkit.key_binding import KeyBindings
    palette._menu_app(lambda: [("", "x")], KeyBindings(), None)
    kw = _FakeApp.app_kwargs[-1]
    assert kw["erase_when_done"] is True
    assert kw["full_screen"] is False
    assert kw["mouse_support"] is False


def test_the_picker_erases_itself(menus):
    """`/model`, `/mode`, `/keys`, `/theme`, `/color`, `/resume`, recovery."""
    _r, _f = menus(palette._run_ephemeral_picker, ["escape"],
                   title="Select a model", options=_opts(),
                   current_value=None, allow_empty=False)
    assert _FakeApp.app_kwargs and all(k["erase_when_done"] is True
                                      for k in _FakeApp.app_kwargs)


def test_the_toggle_menu_erases_itself(menus):
    """`/mcp`, `/offline`."""
    _r, _f = menus(palette._run_toggle_menu, ["escape"],
                   title="MCP Servers", items=_items(), subtitle="", cancellable=True)
    assert _FakeApp.app_kwargs and all(k["erase_when_done"] is True
                                       for k in _FakeApp.app_kwargs)


def test_the_command_palette_erases_itself(menus):
    """Ctrl+P."""
    palette.registry.register("/menu-test", "a test command", "Test", lambda: None)
    try:
        _r, _f = menus(palette._run_palette, ["escape"], initial_query="")
        assert _FakeApp.app_kwargs and all(k["erase_when_done"] is True
                                          for k in _FakeApp.app_kwargs)
    finally:
        palette.registry._commands = [
            c for c in palette.registry._commands if c.name != "/menu-test"]


# ── Selecting actually returns the selection ──────────────────────────────────

def test_arrowing_down_and_pressing_enter_returns_that_option(menus):
    """↓↓Enter picks the third option — what `/model` turns into a switch."""
    chosen, _f = menus(palette._run_ephemeral_picker, ["down", "down", "enter"],
                       title="Select a model", options=_opts(),
                       current_value=None, allow_empty=False)
    assert chosen == "v2"


def test_escape_cancels_the_picker(menus):
    chosen, _f = menus(palette._run_ephemeral_picker, ["escape"],
                       title="Select a model", options=_opts(),
                       current_value="v1", allow_empty=False)
    assert chosen is None


def test_allow_empty_escape_keeps_the_current_value(menus):
    """`allow_empty` means "Esc = leave it as it was", not "Esc = None"."""
    chosen, _f = menus(palette._run_ephemeral_picker, ["escape"],
                       title="Select a model", options=_opts(),
                       current_value="v1", allow_empty=True)
    assert chosen == "v1"


def test_space_toggles_and_enter_applies(menus):
    """The controls the spec names: Space toggle · Enter apply."""
    result, _f = menus(palette._run_toggle_menu, ["space", "down", "space", "enter"],
                       title="MCP Servers", items=_items(), subtitle="",
                       cancellable=True)
    assert result == {"k0": True, "k1": True, "k2": False}


def test_escape_discards_a_cancellable_toggle_menu(menus):
    """⚠️ Esc on `/mcp` must be None — see test_mcp.py for why `{}` is not it."""
    result, _f = menus(palette._run_toggle_menu, ["space", "escape"],
                       title="MCP Servers", items=_items(), subtitle="",
                       cancellable=True)
    assert result is None


def test_escape_applies_a_non_cancellable_toggle_menu(menus):
    """`/offline` was written against Esc == Enter == done. Do not change it."""
    result, _f = menus(palette._run_toggle_menu, ["space", "escape"],
                       title="Offline PIL Models", items=_items(), subtitle="",
                       cancellable=False)
    assert result == {"k0": True, "k1": False, "k2": False}


# ── Geometry: no menu may be taller than the terminal ─────────────────────────

def test_menu_body_rows_leaves_room_for_the_prompt(monkeypatch):
    import shutil as _sh
    monkeypatch.setattr(_sh, "get_terminal_size", lambda fallback=(80, 24): _Size(80, 40))
    assert palette.menu_body_rows() == 40 - palette.MENU_CHROME_ROWS


def test_menu_body_rows_floors_on_a_tiny_terminal(monkeypatch):
    """⚠️ A `Dimension(max=0)` renders as NOTHING AT ALL — hence the floor."""
    import shutil as _sh
    monkeypatch.setattr(_sh, "get_terminal_size", lambda fallback=(80, 24): _Size(80, 2))
    assert palette.menu_body_rows() == palette.MENU_MIN_ROWS
    assert palette.menu_body_rows() > 0


def test_menu_body_rows_survives_a_broken_terminal(monkeypatch):
    """Nothing here may raise into a turn — not even querying the window size."""
    import shutil as _sh

    def _boom(fallback=(80, 24)):
        raise OSError("no tty")

    monkeypatch.setattr(_sh, "get_terminal_size", _boom)
    assert palette.menu_body_rows() == 24 - palette.MENU_CHROME_ROWS


def test_the_menu_window_caps_its_height(menus, monkeypatch):
    """The cap is what stops prompt_toolkit having to clip the canvas."""
    import shutil as _sh
    monkeypatch.setattr(_sh, "get_terminal_size", lambda fallback=(80, 24): _Size(80, 24))
    palette._menu_window(lambda: [("", "x")])
    kw = _FakeApp.win_kwargs[-1]
    assert kw["height"].max == 24 - palette.MENU_CHROME_ROWS


def test_a_long_option_list_does_not_grow_the_window(menus, monkeypatch):
    """⚠️ THE CLIPPING BUG. 60 options on a 24-row terminal still asks for 20 rows.

    Without the cap prompt_toolkit draws from the top and truncates at `size.rows`:
    the footer is gone, ↓ walks onto rows that are not on screen, and
    `erase_when_done` can only clear the region it drew — so the overflow stays in
    scrollback, which is the very symptom Task 13 is about.
    """
    import shutil as _sh
    monkeypatch.setattr(_sh, "get_terminal_size", lambda fallback=(80, 24): _Size(80, 24))
    _r, frames = menus(palette._run_ephemeral_picker, ["escape"],
                       title="Select a model", options=_opts(60),
                       current_value=None, allow_empty=False)
    assert _FakeApp.win_kwargs[-1]["height"].max == 24 - palette.MENU_CHROME_ROWS
    # ...and the render really did produce more rows than fit, so the cap matters.
    assert _text(frames[0]).count("\n") > 24


class _Size:
    def __init__(self, columns, lines):
        self.columns = columns
        self.lines = lines


# ── The scroll marker ─────────────────────────────────────────────────────────

def test_the_picker_marks_the_highlighted_row_for_scrolling(menus):
    """⚠️ `Window` follows the cursor position and NOTHING else.

    Without this marker `vertical_scroll` stays 0 forever, so on a capped body the
    pointer walks off the visible region and the user is arrowing blind.
    """
    _r, frames = menus(palette._run_ephemeral_picker, ["down"],
                       title="t", options=_opts(40),
                       current_value=None, allow_empty=False)
    first, second = frames[0], frames[1]
    assert len(_cursor_rows(first)) == 1, "exactly one scroll target per paint"
    assert len(_cursor_rows(second)) == 1
    assert _cursor_rows(second)[0] > _cursor_rows(first)[0], "the marker must MOVE"


def test_the_toggle_menu_marks_the_highlighted_row_for_scrolling(menus):
    _r, frames = menus(palette._run_toggle_menu, ["down"],
                       title="t", items=_items(40), subtitle="", cancellable=True)
    assert len(_cursor_rows(frames[0])) == 1
    assert _cursor_rows(frames[1])[0] > _cursor_rows(frames[0])[0]


def test_the_palette_marks_the_highlighted_row_for_scrolling(menus):
    names = [f"/menu-test-{i}" for i in range(6)]
    for n in names:
        palette.registry.register(n, "a test command", "Test", lambda: None)
    try:
        _r, frames = menus(palette._run_palette, ["down"], initial_query="menu-test")
        assert len(_cursor_rows(frames[0])) == 1
        assert _cursor_rows(frames[1])[0] > _cursor_rows(frames[0])[0]
    finally:
        palette.registry._commands = [
            c for c in palette.registry._commands if c.name not in names]


def test_the_marker_carries_no_text(menus):
    """It is free when the list fits — it must never add a visible row."""
    _r, frames = menus(palette._run_ephemeral_picker, [],
                       title="t", options=_opts(3),
                       current_value=None, allow_empty=False)
    for style, text in frames[0]:
        if style == palette.CURSOR:
            assert text == ""


# ── Totality: a broken menu degrades, it never raises into a turn ─────────────

def test_a_raising_picker_degrades_to_the_text_form(monkeypatch):
    """The exception path is the one that used to leave a menu on screen."""
    monkeypatch.setattr(palette, "_PTK", True)
    monkeypatch.setattr(palette, "Application", object)
    monkeypatch.setattr(palette, "_run_ephemeral_picker",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr("builtins.input", lambda *a: "2")
    assert palette.ephemeral_picker("t", _opts()) == "v1"


def test_a_raising_toggle_menu_degrades_to_the_text_form(monkeypatch):
    monkeypatch.setattr(palette, "_PTK", True)
    monkeypatch.setattr(palette, "Application", object)
    monkeypatch.setattr(palette, "_run_toggle_menu",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr("builtins.input", lambda *a: "")
    assert palette.ephemeral_toggle_menu("t", _items(1)) == {"k0": False}


def test_a_raising_toggle_menu_still_honours_cancel(monkeypatch):
    """⚠️ The degraded path may not APPLY what a cancellable menu discarded."""
    monkeypatch.setattr(palette, "_PTK", True)
    monkeypatch.setattr(palette, "Application", object)
    monkeypatch.setattr(palette, "_run_toggle_menu",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr("builtins.input", lambda *a: "q")
    assert palette.ephemeral_toggle_menu("t", _items(1), cancellable=True) is None


def test_a_raising_palette_reports_instead_of_crashing(monkeypatch):
    monkeypatch.setattr(palette, "_PTK", True)
    monkeypatch.setattr(palette, "Application", object)
    monkeypatch.setattr(palette, "_run_palette",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    said = []
    monkeypatch.setattr(palette, "status_line", lambda msg, *a, **k: said.append(msg))
    palette.open_palette()                      # must not raise
    assert any("Palette error" in m for m in said)


def test_no_prompt_toolkit_at_all_still_picks(monkeypatch):
    """The half-installed machine: `/model` must still work as a numbered prompt."""
    monkeypatch.setattr(palette, "_PTK", False)
    monkeypatch.setattr(palette, "Application", None)
    monkeypatch.setattr("builtins.input", lambda *a: "3")
    assert palette.ephemeral_picker("t", _opts()) == "v2"


def test_an_empty_menu_is_not_an_error(monkeypatch):
    assert palette.ephemeral_picker("t", []) is None
    assert palette.ephemeral_toggle_menu("t", []) == {}
    assert palette.ephemeral_toggle_menu("t", [], cancellable=True) is None


# ── Every CLI menu is built on the shared system ──────────────────────────────

def test_the_cli_has_no_menu_of_its_own():
    """⚠️ `agent2cli.py` must ASK palette for a menu, never build one.

    Task 13's requirement is "avoid separate implementations for every menu". The
    check is structural because a hand-rolled menu works fine on the day it lands —
    it just never learns about the cap, the marker, or the erase.
    """
    src = Path(__file__).resolve().parent.parent.parent / "agent2cli.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    built = {
        node.func.id for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id in _PTK_WIDGETS
    }
    assert not built, f"agent2cli.py builds prompt_toolkit widgets {sorted(built)}"


def test_the_menu_helpers_are_importable_by_name():
    """The two entry points every surface uses. Renaming one is a breaking change."""
    assert callable(palette.ephemeral_picker)
    assert callable(palette.ephemeral_toggle_menu)
    assert callable(interactive.select_from_list)
    assert callable(interactive.toggle_menu)


# ── `/rules` and `/settings` — the two menus Task 13 names that were missing ───

@pytest.mark.parametrize("base", ["/rules", "/settings"])
def test_the_new_menus_are_advertised_and_dispatched(base):
    """Advertised AND wired. A command missing from `/help` is a command nobody finds."""
    import agent2cli as cli
    from agent2.cli.render import SLASH_COMMANDS

    bases = {b for _tok, b, _desc in SLASH_COMMANDS}
    assert base in bases
    body = Path(cli.__file__).read_text(encoding="utf-8")
    assert f'low == "{base}"' in body


def test_slash_rules_applies_in_two_uniform_bulk_writes(monkeypatch):
    """⚠️ `set_rules_active`, not N × `toggle_rule`.

    Rules feed the system prompt, so flipping them one at a time is N transactions
    AND N prompt rebuilds — and flipping is the wrong primitive anyway: a mixed
    selection ends up mixed the other way round instead of uniformly set.
    """
    import agent2cli as cli

    rows = [
        {"id": "r1", "content": "always run tests", "active": 1},
        {"id": "r2", "content": "never force push", "active": 0},
        {"id": "r3", "content": "prefer stdlib", "active": 1},
    ]
    calls = []

    class _Rules:
        @staticmethod
        def list_rules(active_only=False):
            return rows

        @staticmethod
        def set_rules_active(ids, active):
            calls.append((sorted(ids), active))
            return len(ids)

        @staticmethod
        def toggle_rule(rid):
            pytest.fail("toggle_rule is the wrong primitive for a bulk menu")

    monkeypatch.setattr(cli, "_core_rules", _Rules)
    monkeypatch.setattr(cli, "_CORE_OK", True)
    # r1 stays on, r2 goes on, r3 goes off.
    monkeypatch.setattr(cli, "ephemeral_toggle_menu",
                        lambda *a, **kw: {"r1": True, "r2": True, "r3": False})
    monkeypatch.setattr(cli, "status_line", lambda *a, **k: None)

    cli.cmd_rules()
    assert (["r2"], True) in calls
    assert (["r3"], False) in calls
    assert len(calls) == 2, "one write per direction, not one per rule"


def test_slash_rules_writes_nothing_when_cancelled(monkeypatch):
    """⚠️ Esc DISCARDS. The footer promises it, so the handler must honour it."""
    import agent2cli as cli

    class _Rules:
        @staticmethod
        def list_rules(active_only=False):
            return [{"id": "r1", "content": "a rule", "active": 0}]

        @staticmethod
        def set_rules_active(ids, active):
            pytest.fail("a cancelled menu wrote to the rules table")

    monkeypatch.setattr(cli, "_core_rules", _Rules)
    monkeypatch.setattr(cli, "_CORE_OK", True)
    monkeypatch.setattr(cli, "ephemeral_toggle_menu", lambda *a, **kw: None)
    said = []
    monkeypatch.setattr(cli, "status_line", lambda msg, *a, **k: said.append(msg))

    cli.cmd_rules()
    assert any("unchanged" in m.lower() for m in said)


def test_slash_rules_asks_a_cancellable_menu(monkeypatch):
    """The `cancellable=True` flag is what makes the test above reachable."""
    import agent2cli as cli

    class _Rules:
        @staticmethod
        def list_rules(active_only=False):
            return [{"id": "r1", "content": "a rule", "active": 1}]

    seen = {}
    monkeypatch.setattr(cli, "_core_rules", _Rules)
    monkeypatch.setattr(cli, "_CORE_OK", True)
    monkeypatch.setattr(cli, "ephemeral_toggle_menu",
                        lambda *a, **kw: seen.update(kw) or None)
    monkeypatch.setattr(cli, "status_line", lambda *a, **k: None)
    cli.cmd_rules()
    assert seen.get("cancellable") is True


def test_slash_settings_is_a_hub_and_owns_no_state(monkeypatch):
    """⚠️ It must DELEGATE. A second writer for a setting is a second opinion."""
    import agent2cli as cli

    monkeypatch.setattr(cli, "ephemeral_picker", lambda *a, **kw: "rules")
    monkeypatch.setattr(cli, "status_line", lambda *a, **k: None)
    hit = []
    monkeypatch.setattr(cli, "cmd_rules", lambda: hit.append("rules"))

    model, mode = cli.cmd_settings("2.5-flash", "pro")
    assert hit == ["rules"]
    assert (model, mode) == ("2.5-flash", "pro"), "a hub may not change what it delegates"


def test_slash_settings_escape_changes_nothing(monkeypatch):
    import agent2cli as cli

    monkeypatch.setattr(cli, "ephemeral_picker", lambda *a, **kw: None)
    monkeypatch.setattr(cli, "cmd_rules", lambda: pytest.fail("Esc opened a submenu"))
    assert cli.cmd_settings("2.5-pro", "thinking") == ("2.5-pro", "thinking")


def test_slash_settings_carries_the_model_choice_back(monkeypatch):
    """Model and mode live in the REPL's locals, so the hub must return them."""
    import agent2cli as cli

    answers = iter(["model", "3.1-pro"])
    monkeypatch.setattr(cli, "ephemeral_picker", lambda *a, **kw: next(answers))
    monkeypatch.setattr(cli, "build_model_choices", lambda: _opts())
    monkeypatch.setattr(cli, "save_last_model", lambda m: None)
    monkeypatch.setattr(cli, "status_line", lambda *a, **k: None)
    monkeypatch.setattr(cli.statusbar, "update", lambda **kw: None)

    model, mode = cli.cmd_settings("2.5-flash", "pro")
    assert model == "3.1-pro"
    assert mode == "pro"

