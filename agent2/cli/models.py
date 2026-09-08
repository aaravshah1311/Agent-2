# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/cli/models.py
────────────────────
Models, modes and shell detection — all DERIVED from `agent2.config`, which is
the single declaration for both surfaces.

⚠️ DO NOT RE-DECLARE THESE VALUES.
The CLI used to carry its own copies of MODELS / MODES / DEFAULT_MODEL /
DEFAULT_MODE and its own byte-identical `detect_shell`. They agreed with
`agent2.config` only by luck and nothing enforced it, so adding a model to
config.py reached the Web UI and was invisible to the CLI — while CLAUDE.md
claimed config was the single source of truth. It was not.

`config.MODELS` maps key → {"api", "label", "group"}; the CLI only ever wants the
API string, so the shape is flattened HERE rather than at each of the ~10 call
sites — and `MODEL_GROUPS` projects the one other field the CLI needs (the group,
for `config.supports_thinking()`) out of the same table. `config.MODES` is a
strict superset of what the CLI used (it adds "label" and "desc") and is used
as-is: `thinking_budget` is read via `.get(..., 8000)` behind a truthy "thinking"
gate, which the extra keys do not affect.

The `_FALLBACK_*` block is reached ONLY when `agent2/config.py` cannot be
imported at all, so that `--help` still works on a broken install — the standing
posture of every optional import in `env.py`. It is still a copy, and an
UNTESTED copy would just move the drift somewhere nothing looks, so it is pinned
field-by-field against config by
`test_the_offline_fallback_cannot_drift_from_config` even though a normal run
never reads it.
"""

import os
from pathlib import Path

from agent2.cli.env import IS_WIN, shutil

# ── Last-resort values (broken install only) ───────────────────────────────────
# ⚠️ THIS MIRRORS `config.MODELS` AND MUST BE UPDATED WITH IT.
# It is the only reason `agent2cli.py --help` still works on an install where
# `agent2/config.py` cannot be imported, and `test_the_offline_fallback_cannot_drift_
# from_config` compares it key-for-key so the copy cannot rot unnoticed. When you add
# or rename a model in config, this is the second (and last) place to touch — the
# capability registry derives its own metadata and needs no edit.
_FALLBACK_MODELS = {
    "2.5-flash":      "gemini-2.5-flash",
    "2.5-flash-lite": "gemini-2.5-flash-lite",
    "3.5-flash":      "gemini-3.5-flash",
    "3.5-flash-lite": "gemini-3.5-flash-lite",
    "3.6-flash":      "gemini-3.6-flash",
    "3.7-flash":      "gemini-3.7-flash",
}
_FALLBACK_MODES = {
    "fast":     {"icon": "⚡", "max_tokens": 2048,  "thinking": False},
    "pro":      {"icon": "★",  "max_tokens": 8192,  "thinking": False},
    "thinking": {"icon": "🧠", "max_tokens": 16384, "thinking": True,
                 "thinking_budget": 8000},
}
_FALLBACK_DEFAULT_MODEL = "2.5-flash"
_FALLBACK_DEFAULT_MODE = "pro"

try:
    from agent2.config import (
        MODELS as _CFG_MODELS,
        MODES as _CFG_MODES,
        DEFAULT_MODEL as _CFG_DEFAULT_MODEL,
        DEFAULT_MODE as _CFG_DEFAULT_MODE,
        supports_thinking,
    )
    MODELS = {k: v["api"] for k, v in _CFG_MODELS.items()}
    # ⚠️ DERIVED, exactly like `MODELS` above, and for the same reason. The CLI
    # needs a model's *group* only to ask `config.supports_thinking()` about it, so
    # the group is projected out of the one model table rather than restated. It
    # used to be restated — as a literal `("2.5-pro", "3.1-flash", "3.1-pro",
    # "2.5-flash")` tuple of MODEL KEYS at the `thinking_config` call site — and it
    # had rotted in both directions at once: three of those keys are not in
    # `config.MODELS` at all, and four keys that ARE (`3.5-flash`,
    # `3.5-flash-lite`, `3.6-flash`, `3.7-flash`) were missing from it. So
    # `thinking` mode attached no thinking budget on five of the six selectable
    # models in the CLI — the request succeeded, nothing reported it, and the Web
    # UI (which asks the predicate) behaved differently for the same selection.
    MODEL_GROUPS = {k: str(v.get("group") or "") for k, v in _CFG_MODELS.items()}
    DEFAULT_MODEL = _CFG_DEFAULT_MODEL
    MODES = _CFG_MODES
    DEFAULT_MODE = _CFG_DEFAULT_MODE
except Exception:
    MODELS = _FALLBACK_MODELS
    MODES = _FALLBACK_MODES
    DEFAULT_MODEL = _FALLBACK_DEFAULT_MODEL
    DEFAULT_MODE = _FALLBACK_DEFAULT_MODE
    MODEL_GROUPS = {}

    def supports_thinking(group: str) -> bool:
        """Broken-install fallback: mirrors `config.THINKING_GROUPS == ()`.

        Permissive for the reason config.py documents — the SDK rejects an
        unsupported `thinking_config` and the call site swallows that, so being
        wrong here costs nothing, while a restrictive guess silently drops thinking
        from a model that supports it. No turn runs down this path anyway; it exists
        so `--help` survives an install where `agent2/config.py` will not import.
        """
        return True

# ── Shell ──────────────────────────────────────────────────────────────────────
# ⚠️ IMPORTED, not re-implemented. Two implementations deciding how every command
# gets executed is a drift hazard with no upside. Pinned by
# test_cli_detect_shell_agrees_with_config.
try:
    from agent2.config import (
        detect_shell, shell_argv, SHELL_BIN, SHELL_LABEL, SHELL_FLAG,
    )
except Exception:
    def detect_shell():
        if IS_WIN:
            ps = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
            if ps:
                return ps, "PowerShell", "-Command"
            return "cmd.exe", "CMD", "/c"
        sh = os.environ.get("SHELL", "")
        for s in [sh, "/bin/bash", "/bin/zsh", "/bin/sh"]:
            if s and shutil.which(s):
                return s, Path(s).name.upper(), "-c"
        return "/bin/sh", "SH", "-c"

    SHELL_BIN, SHELL_LABEL, SHELL_FLAG = detect_shell()

    def shell_argv(cmd: str) -> list:
        if IS_WIN and SHELL_BIN.lower().endswith("cmd.exe"):
            return ["cmd.exe", "/c", cmd]
        return [SHELL_BIN, SHELL_FLAG, cmd]
