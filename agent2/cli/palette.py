# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/cli/palette.py
─────────────────────
Command palette and ephemeral menus: Ctrl+P fuzzy search, `/` inline suggest,
and the overlays that ERASE THEMSELVES so terminal history stays clean.

⚠️ EVERY PICKER ERASES ITSELF ON EXIT.
`select_from_list` / `toggle_menu` in `interactive.py` are the TEXT forms — they
print, and what they print stays. This module's overlays run in their own
application context and clear the moment they close, so `/model` opens, you pick,
and the scrollback shows only `✓ Model switched — 2.5-pro`, never the selector.
Those two used to carry a prompt_toolkit branch of their own that was this
module's renderer minus `erase_when_done=True`, reachable through
`ephemeral_picker`'s own `except` clause; that copy is deleted, so exactly one
prompt_toolkit picker and one prompt_toolkit toggle menu exist in the codebase and
both erase. `test_menus.py` walks the AST of both files to keep it that way.

⚠️ NOTHING HERE MAY RAISE INTO A TURN.
A broken picker degrades to the numbered-list fallback `interactive.py` already
has, never to a failed command. Every entry point is total.

⚠️ NO MENU MAY BE TALLER THAN THE TERMINAL — see `_menu_window`.
prompt_toolkit clips an inline app's canvas to `size.rows` (`renderer.py`:
`height = min(height, size.rows)`), and a `FormattedTextControl` with no cursor
marker never scrolls, so an over-long option list is drawn from the top and the
rest simply does not exist: you arrow down onto a row nobody can see, and the
footer is gone with it. `_menu_window` caps the body and lets prompt_toolkit
scroll inside it. It is also what keeps the erase honest — `erase_when_done`
walks the cursor back over the region it drew, so a region that fits is a region
that clears.

Layer: env / theme / models / render / interactive → palette.
"""

import shutil

from dataclasses import dataclass
from collections.abc import Callable

from agent2.cli.env import (
    Application,
    Completion,
    Completer,
    Dimension,
    FormattedTextControl,
    HSplit,
    KeyBindings,
    Layout,
    Style,
    Window,
    _PTK,
)
from agent2.cli.interactive import (
    select_from_list,
)
from agent2.cli.render import SLASH_COMMANDS, status_line
from agent2.cli.theme import P


# ── Fuzzy matching ─────────────────────────────────────────────────────────────
def fuzzy_score(query: str, text: str) -> int:
    """Rank how well *query* matches *text*. Higher is better; 0 means no match.

    Consecutive chars score higher than scattered ones, case-insensitive. The
    order matters: "mp" matches "model picker" but "pm" doesn't.
    """
    if not query:
        return 1000   # empty query matches everything
    q = query.lower()
    t = text.lower()
    score = 0
    pos = 0
    consecutive = 0
    for ch in q:
        idx = t.find(ch, pos)
        if idx == -1:
            return 0    # char not found → no match
        if idx == pos:
            consecutive += 1
        else:
            consecutive = 0
        score += 10 + consecutive * 5   # bonus for runs
        pos = idx + 1
    return score


# ── Command registry ───────────────────────────────────────────────────────────
@dataclass
class Command:
    """One palette entry: name, description, category, and the handler to call."""
    name: str
    desc: str
    category: str
    handler: Callable[[], None]
    keywords: list[str] = None  # Extra search terms

    def matches(self, query: str) -> int:
        """Fuzzy score against name, desc, and keywords."""
        best = max(
            fuzzy_score(query, self.name),
            fuzzy_score(query, self.desc),
            max((fuzzy_score(query, kw) for kw in (self.keywords or [])), default=0),
        )
        return best

    def __post_init__(self):
        if self.keywords is None:
            self.keywords = []


class CommandRegistry:
    """The palette's backing store. Commands are registered by category."""

    def __init__(self):
        self._commands: list[Command] = []
        self._recents: list[str] = []   # command names, most recent first
        self._max_recents = 10

    def register(self, name: str, desc: str, category: str, handler: Callable,
                 keywords: list[str] | None = None):
        """Add a command. Overwrites an existing one with the same name."""
        self._commands = [c for c in self._commands if c.name != name]
        self._commands.append(Command(name, desc, category, handler, keywords or []))

    def mark_used(self, name: str):
        """Move *name* to the front of the recents list."""
        self._recents = [name] + [n for n in self._recents if n != name]
        self._recents = self._recents[: self._max_recents]

    def search(self, query: str, limit: int = 15) -> list[Command]:
        """Fuzzy-ranked results, recents first when query is empty."""
        if not query.strip():
            # No query → show recents + top commands
            recent_cmds = [c for c in self._commands if c.name in self._recents]
            # Sort by recency
            recent_cmds.sort(key=lambda c: self._recents.index(c.name))
            other_cmds = [c for c in self._commands if c.name not in self._recents]
            return (recent_cmds + other_cmds)[: limit]

        scored = [(c, c.matches(query)) for c in self._commands]
        scored = [(c, s) for c, s in scored if s > 0]
        scored.sort(key=lambda x: -x[1])
        return [c for c, _s in scored[: limit]]

    def all_categories(self) -> list[str]:
        """Unique categories in registration order."""
        seen = set()
        out = []
        for c in self._commands:
            if c.category not in seen:
                seen.add(c.category)
                out.append(c.category)
        return out


registry = CommandRegistry()


# ── How tall an inline menu may be, and how it scrolls ─────────────────────────
# ⚠️ ONE OWNER FOR MENU GEOMETRY. All three overlays below build their body
# through `_menu_window`; a second `Window(...)` with its own idea of height is
# how one menu starts clipping while the other two look fine.

MENU_CHROME_ROWS = 4
"""Rows kept clear below the body: the shell prompt gets one, and three are
breathing room so the menu never sits flush against the bottom edge."""

MENU_MIN_ROWS = 3
"""Floor. A 4-row terminal still gets a usable pointer + one option rather than
a `Dimension(max=0)`, which prompt_toolkit renders as nothing at all."""

CURSOR = "[SetCursorPosition]"
"""prompt_toolkit's marker fragment: `FormattedTextControl` reads it out of the
fragment list and reports it as `UIContent.cursor_position`, which is the ONLY
thing `Window` scrolls to follow (`layout/controls.py` → `layout/containers.py`).
⚠️ Emit it on the highlighted row of every scrollable menu. Without it the window
sits at `vertical_scroll = 0` forever, so pressing ↓ past the cap moves a pointer
the user cannot see — the row exists in the fragment list and not on the screen.
It carries no text, so it costs nothing when the list fits."""


def menu_body_rows() -> int:
    """How many rows a menu body may occupy on this terminal, right now.

    Read at open time, not cached: a menu opened after the user resized the window
    must use the new size, and there is no resize event to invalidate a cache with.
    """
    try:
        rows = shutil.get_terminal_size(fallback=(80, 24)).lines
    except Exception:
        rows = 24
    return max(MENU_MIN_ROWS, int(rows) - MENU_CHROME_ROWS)


def _menu_window(render: Callable[[], list]):
    """The body of an inline menu: capped height, scrolls to the cursor marker.

    `Dimension(max=...)` is what stops the canvas from exceeding the screen —
    `Window.preferred_height` clamps the content height into the dimension, so a
    40-row option list on a 24-row terminal asks for 20 rows and prompt_toolkit
    never has to clip it. Combined with `CURSOR` on the highlighted row, the list
    scrolls inside that window like any other prompt_toolkit content.

    ⚠️ There is deliberately NO escalation to `full_screen=True`. The alternate
    screen would also be residue-proof, but it costs the inline feel that makes
    these menus read like part of the conversation, and it is not needed: a body
    that fits cannot scroll off, and a body that fits is exactly what this
    returns. The diff viewer is full-screen because it *is* a separate surface.
    """
    kw = {"wrap_lines": True}
    if Dimension is not None:
        kw["height"] = Dimension(max=menu_body_rows())
    return Window(FormattedTextControl(render), **kw)


def _menu_app(render: Callable[[], list], kb, style):
    """The one `Application` shape every overlay in this module uses.

    ⚠️ `erase_when_done=True` IS THE FEATURE. It makes prompt_toolkit walk the
    cursor back over the region it drew and clear it, which is why the scrollback
    keeps the confirmation line and not the menu. `full_screen=False` keeps the
    menu inline, in the flow of the conversation.
    """
    return Application(
        layout=Layout(HSplit([_menu_window(render)])),
        key_bindings=kb,
        style=style,
        full_screen=False,
        erase_when_done=True,
        mouse_support=False,
    )


# ── Ephemeral picker ───────────────────────────────────────────────────────────
def ephemeral_picker(
    title: str,
    options: list[dict],
    current_value=None,
    allow_empty: bool = False,
) -> str | None:
    """Arrow-key selector that ERASES ITSELF on exit.

    Returns the chosen option's "value", or None if cancelled. Falls back to the
    numbered-list printer when prompt_toolkit is unavailable.
    """
    if not options:
        return None

    # Fallback: the old numbered-list printer. This stays because env.py demands
    # that `--help` work on a half-installed machine, which means every picker
    # must degrade. The ephemeral behaviour is a no-op here — plain print() has
    # no alternate screen to restore from.
    if not _PTK or Application is None:
        return select_from_list(title, options, current_value)

    try:
        return _run_ephemeral_picker(title, options, current_value, allow_empty)
    except Exception:
        # Best-effort: a broken picker falls back to the plain one rather than
        # failing the command outright.
        return select_from_list(title, options, current_value)


def _run_ephemeral_picker(title: str, options: list[dict], current_value, allow_empty: bool):
    idx = next((i for i, o in enumerate(options) if o["value"] == current_value), 0)
    state = {"idx": idx, "chosen": None}

    def render():
        lines = [("class:title", f"  {title}\n\n")]
        for i, o in enumerate(options):
            sel = i == state["idx"]
            cur = o["value"] == current_value
            pointer = "❯ " if sel else "  "
            style = "class:sel" if sel else "class:opt"
            tag = " (current)" if cur else ""
            if sel:
                lines.append((CURSOR, ""))   # scroll target — see CURSOR
            lines.append((style, f"  {pointer}{o['label']}{tag}"))
            hint = o.get("hint", "")
            if hint:
                lines.append(("class:hint", f"   {hint}"))
            lines.append(("", "\n"))
        lines.append(("class:footer", "\n  ↑/↓ move · Enter select · Esc cancel"))
        return lines

    kb = KeyBindings()

    @kb.add("up")
    @kb.add("k")
    def _up(event):
        state["idx"] = (state["idx"] - 1) % len(options)

    @kb.add("down")
    @kb.add("j")
    def _down(event):
        state["idx"] = (state["idx"] + 1) % len(options)

    @kb.add("enter")
    def _enter(event):
        state["chosen"] = options[state["idx"]]["value"]
        event.app.exit()

    @kb.add("escape")
    @kb.add("q")
    @kb.add("c-c")
    def _cancel(event):
        state["chosen"] = None if not allow_empty else current_value
        event.app.exit()

    style = Style.from_dict({
        "title":  "#7c6af7 bold",
        "sel":    "#00ff9c bold",
        "opt":    "#c4c4dc",
        "hint":   "#888888 italic",
        "footer": "#666666",
    })

    _menu_app(render, kb, style).run()
    return state["chosen"]


# ── Command palette ────────────────────────────────────────────────────────────
def open_palette(initial_query: str = ""):
    """Full command palette: fuzzy search with categories, recents, descriptions.

    Ctrl+P opens this with no query; typing "/" from the prompt line can call it
    with "/" already in the search box (optional). Falls back to a plain menu
    when prompt_toolkit is missing.
    """
    if not _PTK or Application is None:
        # Fallback: plain numbered list of all commands, no search.
        cmds = registry.search("", limit=50)
        if not cmds:
            status_line("No commands registered yet.", "info")
            return
        opts = [{"value": c.name, "label": c.name, "hint": c.desc} for c in cmds]
        picked = select_from_list("Command Palette", opts)
        if picked:
            cmd = next((c for c in cmds if c.name == picked), None)
            if cmd:
                registry.mark_used(cmd.name)
                try:
                    cmd.handler()
                except Exception as ex:
                    status_line(f"Command error: {ex}", "error")
        return

    try:
        _run_palette(initial_query)
    except Exception as ex:
        status_line(f"Palette error: {ex}", "error")


def _run_palette(initial_query: str):
    state = {
        "query": initial_query,
        "results": registry.search(initial_query),
        "idx": 0,
        "chosen": None,
    }

    def _update():
        state["results"] = registry.search(state["query"])
        state["idx"] = 0

    def render():
        lines = [("class:title", "  ⚡ Command Palette\n")]
        lines.append(("class:search", f"  > {state['query']}"))
        lines.append(("class:cursor", "│\n"))   # cursor hint
        lines.append(("class:rule", "  " + "─" * 50 + "\n"))

        if not state["results"]:
            lines.append(("class:dim", "\n  No matches.\n"))
        else:
            # Group by category
            cats = {}
            for c in state["results"]:
                cats.setdefault(c.category, []).append(c)

            i = 0
            for cat in registry.all_categories():
                if cat not in cats:
                    continue
                lines.append(("class:cat", f"\n  {cat}\n"))
                for cmd in cats[cat]:
                    sel = i == state["idx"]
                    pointer = "❯ " if sel else "  "
                    style = "class:sel" if sel else "class:opt"
                    recent = " ⭐" if cmd.name in registry._recents[:3] else ""
                    if sel:
                        lines.append((CURSOR, ""))   # scroll target — see CURSOR
                    lines.append((style, f"  {pointer}{cmd.name}{recent}\n"))
                    lines.append(("class:hint", f"     {cmd.desc}\n"))
                    i += 1

        lines.append(("class:rule", "\n  " + "─" * 50 + "\n"))
        lines.append(("class:footer", "  ↑↓ move · Enter run · Esc cancel · type to search"))
        return lines

    kb = KeyBindings()

    @kb.add("up")
    def _up(event):
        if state["results"]:
            state["idx"] = (state["idx"] - 1) % len(state["results"])

    @kb.add("down")
    def _down(event):
        if state["results"]:
            state["idx"] = (state["idx"] + 1) % len(state["results"])

    @kb.add("enter")
    def _enter(event):
        if state["results"]:
            state["chosen"] = state["results"][state["idx"]]
        event.app.exit()

    @kb.add("backspace")
    def _backspace(event):
        state["query"] = state["query"][:-1]
        _update()

    @kb.add("escape")
    @kb.add("c-c")
    def _cancel(event):
        state["chosen"] = None
        event.app.exit()

    @kb.add("<any>")
    def _type(event):
        data = getattr(event, "data", "")
        if data and data.isprintable():
            state["query"] += data
            _update()

    style = Style.from_dict({
        "title":  f"{P.ACCENT} bold",
        "search": "#ffffff bold",
        "cursor": f"{P.ACCENT}",
        "rule":   "#2a2a40",
        "cat":    "#8888aa bold",
        "sel":    "#00ff9c bold",
        "opt":    "#c4c4dc",
        "hint":   "#666677 italic",
        "dim":    "#666677",
        "footer": "#666677",
    })

    _menu_app(render, kb, style).run()

    if state["chosen"]:
        cmd = state["chosen"]
        registry.mark_used(cmd.name)
        try:
            cmd.handler()
        except Exception as ex:
            status_line(f"Command error: {ex}", "error")


# ── Slash autocompleter (enhanced) ─────────────────────────────────────────────
# This replaces `SlashCompleter` in `interactive.py` with palette-aware fuzzy
# matching. The old one only fired on exact prefix matches; this one ranks by
# fuzzy score so "/md" suggests "/model" ahead of "/addmem".

if _PTK and Completer is not None:
    class PaletteCompleter(Completer):
        """Fuzzy slash-command suggestions. Fires when buffer starts with '/'."""

        def get_completions(self, document, complete_event):
            text = document.text_before_cursor.lstrip()
            if not text.startswith("/") or " " in text:
                return
            word = text  # includes leading '/'

            # Score every slash command
            scored = []
            for _tok, base, desc in SLASH_COMMANDS:
                s = fuzzy_score(word[1:], base[1:])  # skip the '/' for scoring
                if s > 0:
                    scored.append((base, desc, s))
            scored.sort(key=lambda x: -x[2])

            for base, desc, _s in scored[:8]:  # top 8
                yield Completion(
                    base,
                    start_position=-len(word),
                    display=base,
                    display_meta=desc,
                )
else:
    PaletteCompleter = None


# ── Helper: toggle menu (reused by /offline, /keys) ───────────────────────────
def ephemeral_toggle_menu(
    title: str,
    items: list[dict],
    subtitle: str = "",
    cancellable: bool = False,
) -> dict[str, bool] | None:
    """Inline menu of on/off switches.

    Each item is {"key","label","hint","on"(bool)}. Returns a dict mapping each
    item's "key" → final bool state. Unlike the old `toggle_menu`, this one
    ERASES ITSELF on exit when prompt_toolkit is available.

    ⚠️ `cancellable` IS OPT-IN, AND THAT ASYMMETRY IS THE POINT (Task 8).
    Without it Enter and Esc mean the same thing — "done" — and the caller
    persists whatever came back. `/offline` and `/keys` were written against that
    and are right to be: every toggle there is instantly reversible, so an Esc
    that silently threw the visit away would be the surprising reading.

    `/mcp` needs the other contract — Esc discards — because its toggles CONNECT
    and DISCONNECT live sessions against a security tool. So a cancellable menu
    returns None for "the user backed out", which no dict of switch states can
    express: `{}` already means "nothing configured", and the pre-existing
    states are exactly what a cancel must leave alone.
    """
    if not items:
        return {} if not cancellable else None

    # Fallback: the numbered loop from `interactive.py`.
    if not _PTK or Application is None:
        from agent2.cli.interactive import toggle_menu
        return toggle_menu(title, items, subtitle, cancellable=cancellable)

    try:
        return _run_toggle_menu(title, items, subtitle, cancellable)
    except Exception:
        from agent2.cli.interactive import toggle_menu
        return toggle_menu(title, items, subtitle, cancellable=cancellable)


def _run_toggle_menu(title: str, items: list[dict], subtitle: str,
                     cancellable: bool = False):
    state = {
        "idx": 0,
        "on": {it["key"]: bool(it.get("on")) for it in items},
        "cancelled": False,
    }

    def render():
        lines = [("class:title", f"  {title}\n")]
        if subtitle:
            lines.append(("class:sub", f"  {subtitle}\n"))
        lines.append(("", "\n"))

        for i, it in enumerate(items):
            sel = i == state["idx"]
            is_on = state["on"][it["key"]]
            pointer = "❯ " if sel else "  "
            switch = "[ ON ]" if is_on else "[ OFF]"
            sw_style = "class:on" if is_on else "class:off"
            lbl_style = "class:sel" if sel else "class:opt"
            if sel:
                lines.append((CURSOR, ""))   # scroll target — see CURSOR
            lines.append((sw_style, f"  {pointer}{switch} "))
            lines.append((lbl_style, f"{it['label']}"))
            lines.append(("", "\n"))
            hint = it.get("hint", "")
            if hint:
                lines.append(("class:hint", f"        {hint}\n"))

        footer = ("\n  ↑↓ navigate · Space toggle · Enter apply · Esc cancel"
                  if cancellable else
                  "\n  ↑↓ move · → toggle · Enter/Esc done")
        lines.append(("class:footer", footer))
        return lines

    kb = KeyBindings()

    @kb.add("up")
    @kb.add("k")
    def _up(event):
        state["idx"] = (state["idx"] - 1) % len(items)

    @kb.add("down")
    @kb.add("j")
    def _down(event):
        state["idx"] = (state["idx"] + 1) % len(items)

    @kb.add("right")
    @kb.add("l")
    @kb.add("space")
    def _toggle(event):
        k = items[state["idx"]]["key"]
        state["on"][k] = not state["on"][k]

    @kb.add("enter")
    def _apply(event):
        event.app.exit()

    @kb.add("escape")
    @kb.add("q")
    @kb.add("c-c")
    def _escape(event):
        # ⚠️ On a cancellable menu these DISCARD; on the default one they are
        # synonyms for Enter, which is what `/offline` and `/keys` expect.
        if cancellable:
            state["cancelled"] = True
        event.app.exit()

    style = Style.from_dict({
        "title":  "#7c6af7 bold",
        "sub":    "#888888 italic",
        "sel":    "#00ff9c bold",
        "opt":    "#c4c4dc",
        "on":     "#00ff9c bold",
        "off":    "#666666",
        "hint":   "#888888 italic",
        "footer": "#666666",
    })

    _menu_app(render, kb, style).run()
    return None if state["cancelled"] else state["on"]
