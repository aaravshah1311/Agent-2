# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/cli/interactive.py
─────────────────────────
How the user is asked things: the input line and its style, slash-command
autocomplete, the TEXT forms of the two menu shapes, and the option lists those
are fed.

⚠️ THE TWO MENUS HERE ARE THE NO-PROMPT_TOOLKIT FALLBACKS AND NOTHING ELSE.
`select_from_list` is a numbered prompt and `toggle_menu` a numbered loop —
plain `print()` + `input()`, by design. That is what runs on a half-installed
machine and under a redirected stdin, the exact situation where someone needs
`/addapi` to work. `SlashCompleter` degrades to `None` (not to a stub class)
because the caller already has to branch on prompt_toolkit anyway.

⚠️ NEITHER OF THEM MAY GROW A `prompt_toolkit.Application` AGAIN — see
`agent2/cli/palette.py`, which owns "an ephemeral CLI menu". Both functions used
to carry a full prompt_toolkit branch that was a near-copy of palette's, minus
`erase_when_done=True`. Two renderers for one widget drift silently, and this
pair *had already drifted into the bug Task 13 was filed for*: `ephemeral_picker`
falls back to `select_from_list` on any exception, so a picker that worked
perfectly and then stayed on screen forever was one raised exception away — no
error, no clue, and the reported symptom exactly. `test_menus.py` walks the AST
of this file to keep the copy from coming back.

⚠️ `providers` IS IMPORTED AS `_prov`, NEVER AS `P`.
It used to be `from agent2.llm import providers as P` inside two functions,
shadowing the palette for the length of the body. Harmless while everything was
one file and `P` was only read for colour elsewhere; a live trap the moment a
colour lookup is added to one of those functions, and invisible when it happens.

Layer: env / theme / models / render → interactive.
"""

from agent2.cli.env import (
    Completer,
    Completion,
    Style,
    _con,
    _PTK,
    _RICH,
)
from agent2.cli.models import MODELS, MODES
from agent2.cli.render import SLASH_COMMANDS
from agent2.cli.theme import P

# ── Read multi-line input helper ───────────────────────────────────────────────
def read_input(prompt_str: str) -> str:
    """Read one line, stripping leading/trailing whitespace."""
    try:
        if _RICH:
            return _con.input(prompt_str)
        else:
            return input(prompt_str)
    except (EOFError, KeyboardInterrupt):
        # `from None`: EOF on a piped stdin and a real Ctrl+C are the same event
        # to the caller — "the user is done". Chaining the EOFError would print a
        # confusing "during handling of the above exception" at the top level.
        raise KeyboardInterrupt from None


def get_prompt_style(mode: str):
    # Mode tints the prompt, but the active theme accent is the base colour so
    # /theme and /color visibly change the input line too.
    mode_color = {
        "fast": "#00ff9c",     # neon green
        "pro": P.ACCENT,         # theme accent
        "thinking": "#ff9f43"  # orange
    }.get(mode, P.ACCENT)

    return Style.from_dict({
        "user": f"{mode_color} bold",
        "meta": "#888888",
        "cwd":  "#6ba9ff bold",      # current directory — the thing you scan for
        "arrow": f"{mode_color} bold",
        # Autocomplete dropdown styling.
        "completion-menu.completion":         "bg:#1e1e30 #c4c4dc",
        "completion-menu.completion.current": f"bg:{mode_color} #10101a bold",
        "completion-menu.meta.completion":         "bg:#15151f #8888aa",
        "completion-menu.meta.completion.current": f"bg:{mode_color} #10101a",
    })


# ── Slash-command autocomplete (type "/" → suggestions; "/m" → filtered) ───────
if _PTK and Completer is not None:
    class SlashCompleter(Completer):
        """Suggests slash commands. Only fires when the buffer starts with '/'
        and is on the first token, so normal chat isn't interrupted."""
        def get_completions(self, document, complete_event):
            text = document.text_before_cursor.lstrip()
            if not text.startswith("/") or " " in text:
                return
            word = text                      # includes leading '/'
            for _tok, base, desc in SLASH_COMMANDS:
                if base.startswith(word.lower()):
                    yield Completion(
                        base,
                        start_position=-len(word),
                        display=base,
                        display_meta=desc,
                    )
else:
    SlashCompleter = None


# ── Numbered fallback for the arrow-key picker ─────────────────────────────────
def select_from_list(title: str, options: list[dict], current_value=None):
    """The TEXT form of `palette.ephemeral_picker` — a numbered prompt.

    Each option is {"value","label","hint"}. Returns the chosen option's "value",
    or None if cancelled (an empty answer, a number out of range, EOF or Ctrl+C
    are all "cancelled").

    ⚠️ THIS IS DELIBERATELY NOT A MENU. It prints, and what it prints STAYS in
    scrollback — which is correct here, because with no prompt_toolkit there is no
    cursor addressing to erase it with. `palette.ephemeral_picker` degrades to
    this both when prompt_toolkit is missing and when its own renderer raises, and
    degrading to plain text is the whole point: the previous version of this
    function was a second prompt_toolkit menu that forgot `erase_when_done`, so
    the failure mode was a menu that worked and never cleaned up.
    """
    if not options:
        return None

    if title:
        print(f"\n  {title}")
    for i, o in enumerate(options, 1):
        mark = "  ←" if o["value"] == current_value else ""
        print(f"  {i}. {o['label']}  {o.get('hint','')}{mark}")
    try:
        raw = input("  Pick number (Enter to cancel): ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if raw.isdigit() and 1 <= int(raw) <= len(options):
        return options[int(raw) - 1]["value"]
    return None


# ── Numbered fallback for the ON/OFF toggle menu ───────────────────────────────
def toggle_menu(title: str, items: list[dict], subtitle: str = "",
                cancellable: bool = False):
    """The TEXT form of `palette.ephemeral_toggle_menu` — a numbered loop.

    Each item is {"key","label","hint","on"(bool)}. Returns a dict mapping each
    item's "key" → final bool state, or None when a `cancellable` menu was backed
    out of. The loop STAYS OPEN while toggling so several switches can be flipped
    in one visit, which is the text equivalent of the real menu's
    `↑↓ navigate · Space toggle · Enter apply · Esc cancel`: a number toggles one
    switch, a bare Enter applies, and `q` cancels.

    ⚠️ THIS IS DELIBERATELY NOT A MENU — see `select_from_list` above and
    `agent2/cli/palette.py`, which owns the graphical form. It prints, and what it
    prints stays; with no prompt_toolkit there is nothing to erase it with.

    ⚠️ `cancellable` MUST BEHAVE THE SAME HERE AS IN `palette.ephemeral_toggle_menu`
    — Esc/`q` returns None, meaning "discard". This is the fallback that path falls
    back TO, on exactly the installs least likely to be tested: no prompt_toolkit,
    or a prompt_toolkit that raised. A fallback that silently APPLIED what a
    cancellable menu was told to discard would connect a security tool the user
    just backed out of, on those installs only.
    """
    if not items:
        return None if cancellable else {}

    state = {it["key"]: bool(it.get("on")) for it in items}
    finish = "Enter to apply, q to cancel" if cancellable else "Enter to finish"
    while True:
        print(f"\n  {title}")
        if subtitle:
            print(f"  {subtitle}")
        for i, it in enumerate(items, 1):
            mark = "ON " if state[it["key"]] else "OFF"
            print(f"  {i}. [{mark}] {it['label']}   {it.get('hint','')}")
        try:
            raw = input(f"  Number to toggle ({finish}): ").strip()
        except (EOFError, KeyboardInterrupt):
            return None if cancellable else state
        if not raw:
            return state
        if cancellable and raw.lower() in ("q", "esc", "cancel"):
            return None
        if raw.isdigit() and 1 <= int(raw) <= len(items):
            k = items[int(raw) - 1]["key"]
            state[k] = not state[k]


def model_label(model_key: str) -> str:
    """Short, friendly name for the prompt line (custom providers → their name)."""
    if isinstance(model_key, str) and model_key.startswith("custom:"):
        try:
            from agent2.llm import providers as _prov
            prov = _prov.get_provider(model_key.split(":", 1)[1])
            if prov:
                return prov.get("name") or prov.get("model_id") or "custom"
        except Exception:
            pass
        return "custom"
    return model_key


def build_model_choices() -> list[dict]:
    """Built-in Gemini models + custom providers, as selector options.

    ⚠️ Task 18: `auto` is offered FIRST and is not a member of `config.MODELS`.
    Selecting it is how a user asks for automatic routing, which is what keeps
    "explicit user selection takes priority" true — Agent2 only routes because the
    user picked the thing that means "you decide". It is listed first because a
    picker's first row is where the eye lands, and "let Agent2 choose" is the right
    default suggestion for someone who opened this menu unsure.

    The hint states the current policy, so a user who selects `auto` while routing
    is `off` can see why nothing changed instead of concluding it is broken.
    """
    opts: list[dict] = []
    try:
        from agent2.llm import router as _router
        mode = _router.routing_mode()
        opts.append({
            "value": _router.AUTO,
            "label": "✨ auto",
            "hint": ("Agent2 picks per turn — vision, context size, "
                     f"complexity  ·  routing: {mode}"),
        })
    except Exception:
        pass
    for k, api in MODELS.items():
        opts.append({"value": k, "label": f"⚡ {k}",
                     "hint": f"Gemini · {api}"})
    try:
        from agent2.llm import providers as _prov
        _prov.init_providers_table()
        for p in _prov.list_providers(safe=True):
            opts.append({"value": p["key"],  # noqa: PERF401
                         "label": f"🔌 {p['name']}",
                         "hint": f"{p['format']} · {p['model_id']}"})
    except Exception:
        pass
    return opts


def build_mode_choices() -> list[dict]:
    """Modes (fast / pro / thinking) as arrow-selector options."""
    hints = {
        "fast":     "Fastest replies, lowest token use",
        "pro":      "Balanced — recommended for most tasks",
        "thinking": "Deep reasoning via extended thinking",
    }
    return [{"value": k, "label": f"{v['icon']}  {k}",
             "hint": f"{v['max_tokens']} tokens · {hints.get(k, '')}"}
            for k, v in MODES.items()]

