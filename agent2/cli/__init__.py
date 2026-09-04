# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2.cli — the CLI surface, split by function.

`agent2cli.py` was one 3,400-line module. It is now a thin launcher over this
package, in the same shape as `agent2/server/` (the Web surface) and
`agent2/llm/` (model access).

⚠️ THE RULE THAT MAKES THIS SPLIT SAFE
──────────────────────────────────────
Anything MUTATED at runtime is reached through an object or through its owning
module — never bound by name across a module boundary.

    from .theme import P        # ✅ a reference to the one shared object
    from .theme import PU       # ❌ a COPY; this module renders the stale
                                #    colour forever, with no traceback

That failure mode is silent, which is why it is a structural rule rather than a
review note. `theme.P` and `state.S` exist to make the correct spelling the
only spelling: both are `__slots__` objects, so a typo is an AttributeError
instead of a dead attribute every reader ignores.

Read-only flags settled at import time (`_RICH`, `_DB_OK`, `SHELL_BIN`, …) are
never rebound, so importing those by name is safe and is what `env` is for.

Layering — each module may import only from the ones above it:

    env         optional imports, paths, platform flags
    state       the mutable session state that used to be `global`
    keyring     the shared KeyRotator + its null-object failsafe
    theme       the shared palette + colour helpers
    models      MODELS / MODES / shell, derived from agent2.config
    store       memories, rules, history + chat persistence
    render      banner, panels, status line, slash-command table, plan printing
    prompt      build_sys_prompt — the CLI system-prompt builder
    tooling     tool schemas + dispatch, the _impl_* bodies, save_memory, burp
    runtime     InputController, Spinner, live command streaming
    interactive prompt_toolkit session, completer, key bindings, pickers

`agent2cli.py` itself stays the entry point — the agent loop, the `cmd_*`
handlers, `process_turn` and `main` live there, importing everything above.
"""

__all__ = [
    "env", "interactive", "keyring", "models", "prompt",
    "render", "runtime", "state", "store", "theme", "tooling",
]
