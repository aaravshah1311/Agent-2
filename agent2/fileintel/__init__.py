# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/fileintel — Universal File Intelligence & Processing System
──────────────────────────────────────────────────────────────────
A modular, plugin-based subsystem that gives the Agent2 agent real file
understanding: detect any common file type, discover what operations are
possible, and dispatch to the right backend — reading, summarising, converting,
analysing, extracting, OCR-ing, searching, and more.

Public API (used by agent2/tools.py):
    detect_file(path)                       → type + metadata + operations
    file_capabilities(path_or_category)     → operations available
    run_op(path, operation, options)        → run an operation (router picks backend)
    convert_file(path, to_format, out?)     → conversion convenience
    search_workspace(query, path?, kind?)   → content/metadata search across files

The module builds ONE default, fully-wired instance (REGISTRY / ROUTER /
EXECUTOR) with all plugins auto-loaded. Tests construct their own registry with
fakes and inject it — nothing here is a hard global dependency.
"""

from __future__ import annotations

from agent2.fileintel.registry import CapabilityRegistry
from agent2.fileintel.router import OperationRouter
from agent2.fileintel.executor import ToolExecutor
from agent2.fileintel.detector import detect, FORMAT_CATEGORY, CATEGORY_FORMATS
from agent2.fileintel.metadata import extract_metadata


def build_registry() -> CapabilityRegistry:
    """Create a registry with every plugin auto-loaded. Safe if optional libs
    are missing — plugins register their capabilities regardless; missing libs
    only surface when an operation actually runs."""
    reg = CapabilityRegistry()
    from agent2.fileintel.plugins import load_plugins
    load_plugins(reg)
    return reg


# ── Default wired instances (the tool layer uses these) ──────────────────────────
REGISTRY = build_registry()
ROUTER = OperationRouter(REGISTRY)
EXECUTOR = ToolExecutor(REGISTRY, ROUTER)


# ── High-level API ───────────────────────────────────────────────────────────────

def detect_file(path: str) -> dict:
    """Detect type + metadata + the operations available for this file."""
    meta = extract_metadata(path)
    fmt = meta.get("format")
    ops = REGISTRY.capabilities_for(fmt) if fmt else {}
    return {
        **meta,
        "operations": sorted(ops.keys()),
        "backends": ops,
    }


def file_capabilities(path_or_category: str) -> dict:
    """Operations available for a file path OR a category name."""
    token = (path_or_category or "").strip().lower()
    if token in CATEGORY_FORMATS:
        formats = REGISTRY.formats_in_category(token)
        return {"category": token,
                "formats": {f: REGISTRY.operations_for(f) for f in formats}}
    det = detect(path_or_category)
    fmt = det["format"]
    return {"path": path_or_category, "format": fmt, "category": det["category"],
            "operations": REGISTRY.operations_for(fmt) if fmt else [],
            "backends": REGISTRY.capabilities_for(fmt) if fmt else {}}


def run_op(path: str, operation: str, options: dict | None = None,
           workspace_root: str | None = None, on_progress=None,
           tool: str = "run_file_op") -> dict:
    """Run one operation via the executor (security → detect → route → log)."""
    return EXECUTOR.run_operation(
        path, operation, options or {},
        workspace_root=workspace_root, on_progress=on_progress, tool=tool,
    )


def convert_file(path: str, to_format: str, output_path: str | None = None,
                 options: dict | None = None, workspace_root: str | None = None,
                 on_progress=None) -> dict:
    """Convenience wrapper: dispatch to the 'convert' operation with a target."""
    opts = dict(options or {})
    opts["to_format"] = (to_format or "").lower().lstrip(".")
    if output_path:
        opts["output_path"] = output_path
    return run_op(path, "convert", opts, workspace_root=workspace_root,
                  on_progress=on_progress, tool="convert_file")


def search_workspace(query: str, path: str = ".", kind: str = "content",
                     workspace_root: str | None = None) -> dict:
    """Search many files by content / filename / metadata. See plugins/search."""
    from agent2.fileintel.search import search
    return search(query, path, kind, workspace_root=workspace_root)


__all__ = [
    "CATEGORY_FORMATS",
    "EXECUTOR",
    "FORMAT_CATEGORY",
    "REGISTRY",
    "ROUTER",
    "CapabilityRegistry",
    "OperationRouter",
    "ToolExecutor",
    "build_registry",
    "convert_file",
    "detect",
    "detect_file",
    "extract_metadata",
    "file_capabilities",
    "run_op",
    "search_workspace",
]
