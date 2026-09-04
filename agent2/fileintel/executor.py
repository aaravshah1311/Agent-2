# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/fileintel/executor.py
────────────────────────────
The Tool Executor — the single entry point the agent tools call.

Pipeline:  security preflight  →  detect  →  route to backend  →  run  →  log.

It collects progress steps into the result (so the UI/agent sees "Reading…,
Converting…, Completed"), records an op-log row, and converts every typed error
into a clean structured dict — the agent never receives a raw traceback.

Constructed with a registry + router (dependency injection). A default wired
instance is exported from agent2/fileintel/__init__.py for the tool layer.
"""

from __future__ import annotations

import time

from agent2.config import FILEINTEL_OUTPUT_DIR
from agent2.fileintel.base import PluginContext
from agent2.fileintel.detector import detect
from agent2.fileintel.errors import FileIntelError
from agent2.fileintel.registry import CapabilityRegistry
from agent2.fileintel.router import OperationRouter
from agent2.fileintel import oplog, security


class ToolExecutor:
    def __init__(self, registry: CapabilityRegistry, router: OperationRouter | None = None):
        self.registry = registry
        self.router = router or OperationRouter(registry)

    def run_operation(self, path: str, operation: str,
                      options: dict | None = None,
                      workspace_root: str | None = None,
                      on_progress=None,
                      tool: str = "run_file_op") -> dict:
        """Execute one file operation end-to-end. Never raises.

        Returns a result dict: on success the backend's payload plus
        `progress`/`_backend`; on failure `{error, code, hint?, progress}`.
        """
        options = dict(options or {})
        steps: list[str] = []

        def _progress(msg: str):
            steps.append(msg)
            if on_progress:
                try:
                    on_progress(msg)
                except Exception:
                    pass

        allow_exec = bool(options.get("allow_executable"))
        started = time.monotonic()
        out_paths: list[str] = []
        try:
            _progress(f"Detecting file type for {path}…")
            resolved = security.preflight(
                path, workspace_root=workspace_root, allow_executable=allow_exec,
            )
            det = detect(str(resolved))
            fmt = options.get("format") or det["format"]
            _progress(f"Detected: {fmt or 'unknown'} ({det['category'] or 'uncategorised'})")

            ctx = PluginContext(
                workspace_root=workspace_root,
                on_progress=_progress,
                logger=None,
                output_dir=options.get("output_dir") or FILEINTEL_OUTPUT_DIR or None,
            )

            _progress(f"Running '{operation}'…")
            result = self.router.run(fmt, operation, str(resolved), options, ctx)
            _progress("Completed.")

            out_paths = _collect_outputs(result)
            dur = int((time.monotonic() - started) * 1000)
            oplog.log_op(tool, operation, str(resolved), out_paths, dur, ok=True)

            result["progress"] = steps
            result["duration_ms"] = dur
            return result

        except FileIntelError as exc:
            dur = int((time.monotonic() - started) * 1000)
            oplog.log_op(tool, operation, path, out_paths, dur, ok=False, error=exc.message)
            d = exc.as_dict()
            d["progress"] = steps
            return d
        except Exception as exc:  # last-resort guard
            dur = int((time.monotonic() - started) * 1000)
            oplog.log_op(tool, operation, path, out_paths, dur, ok=False, error=str(exc))
            return {"error": f"{type(exc).__name__}: {exc}",
                    "code": "unexpected_error", "progress": steps}


def _collect_outputs(result: dict) -> list[str]:
    """Pull any produced-file paths out of a result for logging."""
    paths: list[str] = []
    if not isinstance(result, dict):
        return paths
    for key in ("output_path", "output", "created", "outputs"):
        v = result.get(key)
        if isinstance(v, str):
            paths.append(v)
        elif isinstance(v, list):
            paths.extend(str(x) for x in v)
    return paths
