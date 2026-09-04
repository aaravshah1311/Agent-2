# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/fileintel/plugins — auto-loading plugin package.

`load_plugins(registry)` imports every sibling module in this package and calls
its module-level `register(registry)` hook. Dropping a new `*.py` file here that
defines `register(reg)` extends the system — no core edits, ever.

A plugin that fails to import (e.g. a syntax error in a third-party-only module)
is skipped with a warning rather than taking down the whole subsystem.
"""

from __future__ import annotations

import importlib
import pkgutil


def load_plugins(registry) -> list[str]:
    """Import and register every plugin module. Returns loaded module names."""
    loaded: list[str] = []
    for mod in pkgutil.iter_modules(__path__):
        name = mod.name
        if name.startswith("_"):
            continue
        try:
            module = importlib.import_module(f"{__name__}.{name}")
        except Exception as exc:
            # Never let one bad plugin break the rest.
            import sys
            print(f"[fileintel] plugin '{name}' failed to import: {exc}", file=sys.stderr)
            continue
        register = getattr(module, "register", None)
        if callable(register):
            try:
                register(registry)
                loaded.append(name)
            except Exception as exc:
                import sys
                print(f"[fileintel] plugin '{name}' register() failed: {exc}", file=sys.stderr)
    return loaded
