# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/fileintel/security.py
────────────────────────────
Pre-flight safety checks run by the executor BEFORE any file is touched.

Guards against: path traversal / workspace escape, oversized files, and
processing of executable/dangerous types. All checks raise a typed error
(UnsafePath / FileTooLarge) that the executor turns into a clean message.

⚠️ The subject path is not the only path in a call: `options` is model-supplied
and a couple of dozen plugin sites treat one of `PATH_OPTIONS` /
`PATH_LIST_OPTIONS` as a file to write or read. `confine_options()` is the one
gate for those, so a plugin never has to remember — see its comment for why a
per-plugin check is the wrong shape.
"""

from __future__ import annotations

from pathlib import Path

from agent2.config import MAX_FILE_SIZE, BLOCKED_EXTENSIONS
from agent2.fileintel.errors import UnsafePath, FileTooLarge


def resolve_safe(path: str, workspace_root: str | None = None) -> Path:
    """Resolve a path and, when a workspace root is set, confine it there.

    Rejects symlink/`..` escapes out of the workspace. When no root is given the
    path is only resolved (the agent is trusted to operate on the whole machine,
    matching the existing tools), but traversal is still normalised.
    """
    p = Path(path).expanduser()
    try:
        resolved = p.resolve()
    except Exception as exc:
        raise UnsafePath(f"Cannot resolve path: {path}") from exc

    if workspace_root:
        root = Path(workspace_root).expanduser().resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            raise UnsafePath(
                f"Path escapes the workspace: {path}",
                hint=f"Only files under {root} may be processed.",
            )
    return resolved


# ── Path-bearing options ──────────────────────────────────────────────────────
# Option keys whose VALUE is a filesystem path. Roughly two dozen plugin sites
# read one of these straight out of the model-supplied `options` dict and then
# write to it (`output_path`, `output_dir`) or read from it (`paths`,
# `other`/`compare_to`). Declaring them here — and confining them in the
# executor — is what keeps the subject path and its options under ONE rule: a
# check inside each plugin is a check the next plugin forgets, and the failure is
# silent (a file written outside the workspace, reported as success).
PATH_OPTIONS = ("output_path", "output_dir", "other", "compare_to")
PATH_LIST_OPTIONS = ("paths",)


def confine_options(options: dict, workspace_root: str | None) -> dict:
    """Return `options` with every path-bearing key confined to the workspace.

    A no-op without a root, so direct library use keeps its whole-machine
    posture. Raises `UnsafePath` for the first value that escapes — the same
    refusal an escaping subject path earns, through the same executor branch.
    """
    if not workspace_root or not options:
        return options
    root = Path(workspace_root).expanduser()
    out = dict(options)

    def _confine(value: str) -> str:
        p = Path(str(value)).expanduser()
        # Relative resolves against the ROOT, not cwd — workspace.validate_path's
        # rule, for its reason: a process-cwd change must not move the boundary.
        return str(resolve_safe(str(p if p.is_absolute() else root / p), workspace_root))

    for key in PATH_OPTIONS:
        v = out.get(key)
        if isinstance(v, str) and v.strip():
            out[key] = _confine(v)
    for key in PATH_LIST_OPTIONS:
        v = out.get(key)
        if isinstance(v, (list, tuple)):
            out[key] = [_confine(x) for x in v if str(x).strip()]
    return out


def validate_size(path: Path) -> None:
    """Reject files larger than MAX_FILE_SIZE."""
    try:
        size = path.stat().st_size
    except Exception:
        return
    if size > MAX_FILE_SIZE:
        raise FileTooLarge(
            f"File is {size:,} bytes; limit is {MAX_FILE_SIZE:,}.",
            hint="Increase MAX_FILE_SIZE in agent2/config.py or split the file.",
        )


def check_extension(path: Path, allow_executable: bool = False) -> None:
    """Block dangerous/executable extensions unless explicitly allowed."""
    ext = path.suffix.lower().lstrip(".")
    if ext in BLOCKED_EXTENSIONS and not allow_executable:
        raise UnsafePath(
            f"Refusing to process a '{ext}' file (blocked type).",
            hint="Pass allow_executable=true in options to override.",
        )


def preflight(path: str, workspace_root: str | None = None,
              allow_executable: bool = False, require_exists: bool = True) -> Path:
    """Run all safety checks and return the resolved Path."""
    resolved = resolve_safe(path, workspace_root)
    if require_exists and not resolved.exists():
        raise UnsafePath(f"File not found: {resolved}")
    if resolved.exists() and resolved.is_file():
        check_extension(resolved, allow_executable)
        validate_size(resolved)
    return resolved
