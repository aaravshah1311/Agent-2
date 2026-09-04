# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/fileintel/base.py
────────────────────────
Plugin contract + the data structures the registry/router speak in.

A *plugin* declares which formats it handles and, for each, which operations it
can perform. Each operation is a plain callable with the signature:

    def op(path: str, options: dict, ctx: PluginContext) -> dict

Operations return a JSON-serialisable dict on success. On failure they raise one
of the typed errors in `errors.py` (MissingDependency / BackendError / …); they
must never let a raw third-party exception escape — wrap it in BackendError.

Dependency injection: nothing here reaches for globals. The registry is passed
into the router/executor, and every operation receives a `PluginContext` carrying
the workspace root, a progress callback, and a logger. Tests build their own
context and registry with fakes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol
from collections.abc import Callable

# An operation callable: (path, options, ctx) -> result dict
OpFn = Callable[[str, dict, "PluginContext"], dict]


@dataclass
class Capability:
    """One operation a plugin can perform on a format.

    `backend` is a short label ("pypdf", "reportlab", "stdlib") used for logging
    and for the router's fallback ordering — lower `priority` runs first.
    `batch` marks operations that accept an `options["paths"]` list (merge, etc.).
    """

    operation: str
    fn: OpFn
    backend: str = "default"
    priority: int = 100
    batch: bool = False
    description: str = ""


@dataclass
class PluginContext:
    """Carrier for everything an operation needs from the outside world (DI).

    - workspace_root: confinement root for path safety (None = unconfined).
    - on_progress: called with human-readable step strings; also collected into
      the result's `progress` list by the executor.
    - logger: callable(dict) that records an op-log row (see oplog.py).
    - output_dir: where generated files should go (None = beside the source).
    """

    workspace_root: str | None = None
    on_progress: Callable[[str], None] | None = None
    logger: Callable[[dict], None] | None = None
    output_dir: str | None = None
    extras: dict = field(default_factory=dict)

    def progress(self, msg: str) -> None:
        if self.on_progress:
            try:
                self.on_progress(msg)
            except Exception:
                pass


class Plugin(Protocol):
    """Structural contract for a plugin module's exported object.

    In practice plugins are plain modules that expose a module-level
    `register(registry)` function (see plugins/__init__.py auto-loader). This
    Protocol documents the shape a class-based plugin would take if one is ever
    written; the registry only needs `name`, `formats`, and `capabilities`.
    """

    name: str
    formats: list[str]

    def capabilities(self) -> list[Capability]:
        ...


def require(module_name: str, pip_name: str | None = None):
    """Import an optional dependency or raise MissingDependency.

    Usage inside an operation:
        pypdf = require("pypdf")
    Keeps the top of every plugin free of hard imports so the package loads with
    zero optional libraries installed.
    """
    from agent2.fileintel.errors import MissingDependency
    import importlib
    try:
        return importlib.import_module(module_name)
    except Exception as exc:  # ImportError and anything a broken install throws
        raise MissingDependency(pip_name or module_name,
                                 hint=f"pip install {pip_name or module_name}") from exc


def require_binary(binary: str, hint: str | None = None) -> str:
    """Return the resolved path to a system binary or raise MissingDependency."""
    import shutil
    from agent2.fileintel.errors import MissingDependency
    found = shutil.which(binary)
    if not found:
        raise MissingDependency(binary, hint=hint, kind="binary")
    return found
