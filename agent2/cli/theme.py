# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/cli/theme.py
───────────────────
The CLI palette: ANSI constants, the mutable shared palette object, the theme
presets, and the colour-rendering helpers every printer calls.

⚠️ READ THIS BEFORE ADDING A COLOUR
────────────────────────────────────
`/theme` and `/color` repaint at runtime and ~118 call sites read the result. A
theme-dependent colour must live as an ATTRIBUTE on the shared `P` object, never
as a module global, because `from .theme import PU` binds a **copy** — the
importing module then renders the old colour forever, with no exception and no
traceback. `from .theme import P` binds a reference to the one instance, so
`P.PU = ...` is seen everywhere.

This module existing at all is what made that hazard real; the object is the
failsafe, not a style preference.
"""

from agent2.cli.env import _DB_OK

# ── Fixed ANSI ─────────────────────────────────────────────────────────────────
R = "\033[0m"
B = "\033[1m"
D = "\033[2m"

# Semantic colours — success / warning / error / bright. These stay FIXED across
# every theme so a status cue never changes meaning, so they are plain constants
# and binding them by name in another module is safe.
GR = "\033[38;5;83m"
YW = "\033[38;5;221m"
RD = "\033[38;5;203m"
WH = "\033[38;5;255m"


class _Palette:
    """The mutable half of the palette, held as ATTRIBUTES on one shared object.

    `__slots__` is deliberate: `P.PUU = ...` raises AttributeError rather than
    creating a dead attribute that every reader silently ignores.

    `THEME` lives here too. It used to be a `_CURRENT_THEME` module global
    mutated through `global` — exactly the shape that cannot survive a module
    split, since an importer would bind the startup value and `/theme` would
    appear to do nothing to it. Keeping it on the object means the palette
    save/restore in the tests covers it automatically.

    Pinned by test_apply_theme_repaints_through_the_helpers, which asserts
    through the pu()/cy() helpers rather than by reading the attribute — the
    snapshot bug is invisible to a test that reads the value directly.
    """

    __slots__ = ("ACCENT", "ACCENT2", "CY", "MG", "PU", "THEME")

    def __init__(self):
        self.PU = "\033[38;5;135m"
        self.CY = "\033[38;5;81m"
        self.MG = "\033[38;5;177m"
        # Hex accents used by the Rich-rendered paths and the prompt_toolkit prompt.
        self.ACCENT = "#7c6af7"
        self.ACCENT2 = "#60b8ff"
        self.THEME = DEFAULT_THEME


# ── Theme presets ──────────────────────────────────────────────────────────────
# Each preset defines the primary accent (PU), secondary (CY/MG) and matching hex
# accents for Rich + the prompt line. GR/YW/RD/WH stay semantic across themes.
THEMES: dict[str, dict] = {
    "purple": {"label": "Purple (default)", "PU": "135", "CY": "81",  "MG": "177",
               "accent": "#7c6af7", "accent2": "#60b8ff"},
    "emerald": {"label": "Emerald",         "PU": "48",  "CY": "43",  "MG": "84",
                "accent": "#2ee6a6", "accent2": "#3ddc84"},
    "ocean":  {"label": "Ocean",            "PU": "39",  "CY": "45",  "MG": "81",
               "accent": "#3aa0ff", "accent2": "#60d0ff"},
    "amber":  {"label": "Amber",            "PU": "214", "CY": "221", "MG": "215",
               "accent": "#ff9f43", "accent2": "#f0c060"},
    "rose":   {"label": "Rose",             "PU": "205", "CY": "211", "MG": "218",
               "accent": "#ff5c8a", "accent2": "#ff8fb0"},
    "mono":   {"label": "Monochrome",       "PU": "250", "CY": "244", "MG": "252",
               "accent": "#cccccc", "accent2": "#999999"},
    # ── Editor/terminal-inspired presets ──────────────────────────────────────
    # Matched to each tool's published brand colour so a user who themes their
    # whole setup one way doesn't get a clashing Agent2. They are ordinary
    # entries in this table — nothing special-cases them, which is the point:
    # `/theme` and the persistence path pick them up for free.
    "claude": {"label": "Claude Code",      "PU": "173", "CY": "180", "MG": "209",
               "accent": "#d97757", "accent2": "#e8a87c"},
    "warp":   {"label": "Warp",             "PU": "141", "CY": "75",  "MG": "177",
               "accent": "#a277ff", "accent2": "#38bdf8"},
    "vscode": {"label": "VS Code",          "PU": "32",  "CY": "39",  "MG": "75",
               "accent": "#007acc", "accent2": "#4fc1ff"},
    "github": {"label": "GitHub Dark",      "PU": "111", "CY": "75",  "MG": "141",
               "accent": "#58a6ff", "accent2": "#bc8cff"},
    "nord":   {"label": "Nord",             "PU": "110", "CY": "109", "MG": "139",
               "accent": "#88c0d0", "accent2": "#81a1c1"},
    "dracula": {"label": "Dracula",         "PU": "141", "CY": "117", "MG": "212",
                "accent": "#bd93f9", "accent2": "#8be9fd"},
}
DEFAULT_THEME = "purple"

# `/theme github-dark` and `/theme claude-code` are what people actually type.
# Resolved in `apply_theme` so the alias never becomes a second THEMES entry
# that `/theme`'s picker would list twice.
THEME_ALIASES = {
    "github-dark": "github", "githubdark": "github", "gh": "github",
    "claude-code": "claude", "claudecode": "claude", "cc": "claude",
    "vs-code": "vscode", "code": "vscode",
    "monochrome": "mono", "grey": "mono", "gray": "mono",
}

# A few named accent colours for /color (overrides just the primary accent).
ACCENT_CHOICES = {
    "purple": ("135", "#7c6af7"), "blue":   ("39",  "#3aa0ff"),
    "green":  ("48",  "#2ee6a6"), "cyan":   ("45",  "#22d3ee"),
    "amber":  ("214", "#ff9f43"), "orange": ("208", "#ff7a1a"),
    "red":    ("203", "#ff5555"), "pink":   ("205", "#ff5c8a"),
    "teal":   ("43",  "#14b8a6"), "white":  ("255", "#ffffff"),
}

P = _Palette()


def resolve_theme(name: str) -> str:
    """Canonical THEMES key for *name*, honouring the aliases. Never raises."""
    key = (name or "").strip().lower()
    return THEME_ALIASES.get(key, key)


def apply_theme(name: str, persist: bool = True) -> bool:
    """Repaint the palette from a preset. Returns True if applied.

    Accepts an alias (`github-dark`, `claude-code`) as well as a canonical key,
    and PERSISTS the canonical one so a saved setting never depends on which
    spelling the user happened to type.
    """
    name = resolve_theme(name)
    t = THEMES.get(name)
    if not t:
        return False
    P.PU = f"\033[38;5;{t['PU']}m"
    P.CY = f"\033[38;5;{t['CY']}m"
    P.MG = f"\033[38;5;{t['MG']}m"
    P.ACCENT = t["accent"]
    P.ACCENT2 = t["accent2"]
    P.THEME = name
    if persist and _DB_OK:
        try:
            from agent2.database import set_setting
            set_setting("cli_theme", name)
            set_setting("cli_accent", "")   # theme wins → clear standalone accent
        except Exception:
            pass
    return True


def apply_accent(name_or_code: str, persist: bool = True) -> bool:
    """Override just the primary accent colour (PU + hex). Returns True if applied.

    ⚠️ Deliberately leaves GR/YW/RD/WH alone. An accent that recoloured the
    error red would make a failure indistinguishable from a success at a glance.
    """
    key = (name_or_code or "").strip().lower()
    if key in ACCENT_CHOICES:
        code, hexv = ACCENT_CHOICES[key]
    elif key.isdigit() and 0 <= int(key) <= 255:
        code, hexv = key, P.ACCENT   # keep hex accent; recolour ANSI primary
    else:
        return False
    P.PU = f"\033[38;5;{code}m"
    P.MG = f"\033[38;5;{code}m"
    P.ACCENT = hexv
    if persist and _DB_OK:
        try:
            from agent2.database import set_setting
            set_setting("cli_accent", key)
        except Exception:
            pass
    return True


def load_theme_from_settings() -> None:
    """Restore the saved theme + accent override at startup (best-effort)."""
    if not _DB_OK:
        return
    try:
        from agent2.database import get_setting
        theme = resolve_theme(get_setting("cli_theme") or "")
        accent = (get_setting("cli_accent") or "").strip()
        if theme in THEMES:
            apply_theme(theme, persist=False)
        if accent:
            apply_accent(accent, persist=False)
    except Exception:
        pass


# ── Rendering helpers ──────────────────────────────────────────────────────────
# ⚠️ pu()/cy() read through `P` on every call. That laziness is what makes a
# theme change instant — resolving the colour once into a default argument or a
# module constant would freeze it at import.

def _p(col, text): return f"{col}{text}{R}"
def ok(t):   return _p(GR, t)
def warn(t): return _p(YW, t)
def err(t):  return _p(RD, t)
def dim(t):  return _p(D, t)
def pu(t):   return _p(P.PU, t)
def cy(t):   return _p(P.CY, t)
