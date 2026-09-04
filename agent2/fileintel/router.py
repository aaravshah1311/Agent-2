# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/fileintel/router.py
──────────────────────────
The Operation Router — picks the backend and owns error recovery.

Given a (format, operation), it asks the registry for the ordered backend list
and tries each one until one succeeds. A backend that raises MissingDependency
or BackendError is *recoverable*: the router moves to the next backend. Only
when every backend is exhausted does it surface a single, clean error that lists
what went wrong / what to install.

The router is constructed with a registry (dependency injection); it holds no
global state.
"""

from __future__ import annotations

from agent2.fileintel.base import Capability, PluginContext
from agent2.fileintel.errors import (
    FileIntelError, MissingDependency, BackendError,
    UnsupportedFormat, UnsupportedOperation,
)
from agent2.fileintel.registry import CapabilityRegistry


class OperationRouter:
    def __init__(self, registry: CapabilityRegistry):
        self.registry = registry

    def run(self, fmt: str, operation: str, path: str,
            options: dict, ctx: PluginContext) -> dict:
        """Resolve and execute (format, operation), trying backends in order.

        Returns the winning backend's result dict, annotated with `_backend`.
        Raises a typed FileIntelError only when nothing could run.
        """
        if not fmt:
            raise UnsupportedFormat("Could not determine the file's format.")

        backends = self.registry.resolve(fmt, operation)
        if not backends:
            available = self.registry.operations_for(fmt)
            if not self.registry.known_formats().__contains__(fmt) and not available:
                raise UnsupportedFormat(
                    f"No plugin handles '.{fmt}' files.",
                    hint="Supported: " + ", ".join(self.registry.known_formats()[:40]),
                )
            raise UnsupportedOperation(
                f"Operation '{operation}' is not available for '.{fmt}'.",
                hint=f"Available for .{fmt}: {', '.join(available) or '(none)'}",
            )

        errors: list[str] = []
        for cap in backends:
            try:
                ctx.progress(f"Using backend: {cap.backend}")
                result = self._invoke(cap, path, options, ctx)
                if isinstance(result, dict):
                    result.setdefault("_backend", cap.backend)
                    result.setdefault("operation", operation)
                return result
            except MissingDependency as exc:
                errors.append(f"{cap.backend}: {exc.message} ({exc.hint})")
                ctx.progress(f"Backend '{cap.backend}' unavailable — trying next…")
                continue
            except BackendError as exc:
                errors.append(f"{cap.backend}: {exc.message}")
                ctx.progress(f"Backend '{cap.backend}' failed — trying next…")
                continue
            except FileIntelError:
                raise  # UnsafePath / FileTooLarge etc. are not recoverable
            except Exception as exc:  # never let a raw lib exception escape
                errors.append(f"{cap.backend}: {type(exc).__name__}: {exc}")
                continue

        # Every backend exhausted → one clean, actionable error.
        hints = "; ".join(errors) if errors else "no working backend"
        raise BackendError(
            f"'{operation}' on '.{fmt}' failed — all backends were unavailable or errored.",
            hint=hints,
        )

    @staticmethod
    def _invoke(cap: Capability, path: str, options: dict, ctx: PluginContext) -> dict:
        return cap.fn(path, options, ctx)
