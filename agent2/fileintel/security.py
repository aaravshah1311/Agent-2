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
