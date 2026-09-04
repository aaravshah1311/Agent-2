# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2.core.workspace
──────────────────────
THE single, centralized Workspace Manager (sections 1, 2, 3, 12).

Every component asks THIS manager for workspace information — no other module
stores its own copy of "the project path". The manager owns:

  - the current workspace (id, absolute path == project root, allowed dirs)
  - discovery of the project root from a launch directory
  - path validation / sandboxing (the security boundary)
  - persistence of the active workspace across CLI ⇄ Web (via settings table)
  - workspace switching (which cancels tasks + clears caches)

Security boundary (section 2)
  The project root is a hard sandbox. Every filesystem tool must call
  `manager.validate_path(p)` before touching the filesystem. Any path that
  resolves outside the root — via `..`, an absolute path, a symlink/junction,
  a mounted drive, or a UNC/network path — is rejected with the EXACT message:

      Operation blocked: Path is outside current workspace.

Cross-platform
  Uses `pathlib` + `os.path.normcase`; `Path.resolve()` collapses `..` and
  resolves symlinks/junctions so an escape cannot survive resolution. Works on
  Windows (drive letters, UNC, junctions), macOS and Linux.
"""

from __future__ import annotations

import os
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from agent2.core import logging as alog

# The exact, user-facing message required on any sandbox violation (section 2).
BLOCKED_MSG = "Operation blocked: Path is outside current workspace."


class WorkspaceViolation(Exception):
    """Raised when a requested path escapes the active workspace.

    Its string form is exactly BLOCKED_MSG so callers can surface it verbatim.
    """

    def __init__(self, requested: str | None = None):
        super().__init__(BLOCKED_MSG)
        self.requested = requested


# Detection order (section 3). Each entry is (kind, filename-or-None). `git` and
# `explicit`/`config` are handled specially; the rest are "marker file exists in
# this directory" checks, walked upward from the launch dir.
_MARKER_FILES = [
    ("agent.md",       "agent.md"),
    ("package.json",   "package.json"),
    ("pyproject.toml", "pyproject.toml"),
    ("cargo.toml",     "Cargo.toml"),
    ("go.mod",         "go.mod"),
    ("composer.json",  "composer.json"),
    ("workspace",      ".workspace"),
]

# Directories that are never part of a project's editable surface. Kept here so
# tools and search share one definition.
SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
    ".next", "target", ".idea", ".vscode", "coverage", ".cache", ".DS_Store",
}

_SETTING_KEY = "active_workspace"


def _norm(p: Path) -> str:
    """Canonical string form for comparisons (case-normalised, absolute)."""
    return os.path.normcase(os.path.abspath(str(p)))


@dataclass
class Workspace:
    """Immutable-ish snapshot handed to callers. Never contains a null root."""
    id: str
    root: Path                      # project root == the security boundary
    allowed_dirs: list[Path] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    @property
    def path(self) -> str:
        return str(self.root)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "path": str(self.root),
            "allowed_dirs": [str(d) for d in self.allowed_dirs],
            "metadata": self.metadata,
        }


class WorkspaceManager:
    """Process-wide singleton. Access via the module-level `manager`."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._current: Workspace | None = None
        # Callbacks fired when the workspace switches (section 12): the session
        # manager registers here to cancel tasks / clear caches.
        self._on_switch: list = []

    # ── Discovery (section 3) ────────────────────────────────────────────────

    def _discover_root(self, start: Path) -> Path:
        """Find the project root from *start*, in the exact specified order:

        git root → agent.md → package.json → pyproject.toml → cargo.toml →
        go.mod → composer.json → .workspace → persisted workspace config →
        *start* itself.

        The directory Agent2 is launched from wins: whenever the launch dir
        (or an ancestor) looks like a real project, OR is simply a folder the
        user is working in, that folder is used. The persisted
        ``active_workspace`` (an explicit in-app "open folder" or a CLI
        ``/workspace`` switch) is consulted ONLY as a fallback, and ONLY when
        Agent2 was launched from a neutral spot (its own install dir or the
        user's home directory). This keeps the Web UI's "remember my last
        folder" behavior for neutral launch points while letting `agent2` from
        inside any folder target that folder.
        """
        start = start.resolve()

        # The home directory is a hard CEILING for upward auto-discovery. A stray
        # `.git`/marker file living in the user's home (e.g. a leftover
        # `package.json` in C:\Users\<name>) must NOT hijack a launch from a
        # subfolder — otherwise `agent2` from any project under home would climb
        # up and wrongly adopt the home directory as the project root. We stop the
        # ascent the moment we reach home, so home (and anything above it) is
        # never returned as a discovered root. Launching literally FROM home is a
        # neutral launch and is handled by the fallback below.
        try:
            ceiling = Path.home().resolve()
        except Exception:
            ceiling = None

        def _at_ceiling(node: Path) -> bool:
            return ceiling is not None and _norm(node) == _norm(ceiling)

        # 1. git root — nearest ancestor containing a `.git` dir/file (below home).
        node = start
        for _ in range(64):
            if _at_ceiling(node):
                break
            if (node / ".git").exists():
                return node
            if node.parent == node:
                break
            node = node.parent

        # 2-8. Marker files, nearest ancestor wins (closest to `start`, below home).
        for _name, marker in _MARKER_FILES:
            node = start
            for _ in range(64):
                if _at_ceiling(node):
                    break
                if (node / marker).exists():
                    return node
                if node.parent == node:
                    break
                node = node.parent

        # 9. Nothing project-like under the launch dir. Restore the last
        #    explicitly opened/persisted workspace (Web UI folder picker / CLI
        #    /workspace switch) ONLY when Agent2 was launched from a genuinely
        #    neutral spot — its own install dir or the user's home directory.
        #    Launching from any other folder means "work here", so that folder
        #    wins even without git/marker files.
        if self._is_neutral_launch(start):
            try:
                from agent2.database import get_setting
                saved = get_setting(_SETTING_KEY)
                if saved:
                    sp = Path(saved)
                    if sp.is_dir():
                        return sp.resolve()
            except Exception:
                pass

        # 10. Fallback: the launch directory itself is a valid, non-null root.
        return start

    @staticmethod
    def _is_neutral_launch(start: Path) -> bool:
        """True if *start* is a spot that carries no "work here" intent — the
        Agent2 install dir (where a bare `python run.py` / global `agent2` with
        no cwd change lands) or the user's home directory."""
        try:
            from agent2.config import ROOT
            neutral = {_norm(Path(ROOT)), _norm(Path.home())}
            return _norm(start) in neutral
        except Exception:
            return False

    # ── Current workspace (section 1) ────────────────────────────────────────

    def get_current_workspace(self) -> Workspace:
        """Return the active workspace, discovering + repairing if needed.

        Guarantees a non-null, absolute, existing directory root (section 1).
        """
        with self._lock:
            if self._current is not None and self._is_valid_root(self._current.root):
                return self._current
            # (Re)discover and repair.
            start = Path(os.getcwd())
            root = self._discover_root(start)
            root = self._repair_root(root, start)
            self._current = Workspace(
                id=str(uuid.uuid4())[:8],
                root=root,
                allowed_dirs=[root],
                metadata=self._project_metadata(root),
            )
            alog.workspace_change(old=None, new=str(root), wid=self._current.id)
            return self._current

    @staticmethod
    def _is_valid_root(root: Path | None) -> bool:
        try:
            # root is Path | None; short-circuit the None before dereferencing
            # so mypy narrows, then catch the actual failure modes (a path that
            # raises on is_absolute/is_dir, e.g. ENAMETOOLONG).
            if root is None:
                return False
            return root.is_absolute() and root.is_dir()
        except Exception:
            return False

    def _repair_root(self, root: Path, start: Path) -> Path:
        """Never allow a null/relative/nonexistent root (section 1)."""
        try:
            if not root or not str(root).strip():
                root = start
            root = Path(root)
            if not root.is_absolute():
                root = (start / root).resolve()
            root = root.resolve()
            if not root.is_dir():
                root = start.resolve()
        except Exception:
            root = Path(os.getcwd()).resolve()
        return root

    @staticmethod
    def _project_metadata(root: Path) -> dict:
        meta: dict = {"name": root.name}
        try:
            if (root / ".git").exists():
                meta["vcs"] = "git"
            for _name, marker in _MARKER_FILES:
                if (root / marker).exists():
                    meta["marker"] = marker
                    break
        except Exception:
            pass
        return meta

    # ── Switching (section 12) ───────────────────────────────────────────────

    def set_workspace(self, path: str) -> Workspace:
        """Switch the active workspace. Fires on-switch callbacks (which cancel
        tasks / clear caches) and persists the choice for CLI⇄Web parity."""
        with self._lock:
            start = Path(os.getcwd())
            new_root = self._repair_root(Path(path).expanduser(), start)
            old = self._current
            old_path = str(old.root) if old else None
            if old and _norm(old.root) == _norm(new_root):
                return old  # no-op switch

            new_ws = Workspace(
                id=str(uuid.uuid4())[:8],
                root=new_root,
                allowed_dirs=[new_root],
                metadata=self._project_metadata(new_root),
            )
            self._current = new_ws
            try:
                from agent2.database import set_setting
                set_setting(_SETTING_KEY, str(new_root))
            except Exception:
                pass
            alog.workspace_change(old=old_path, new=str(new_root), wid=new_ws.id)

        # Fire callbacks OUTSIDE the lock so a callback can call back in.
        for cb in list(self._on_switch):
            try:
                cb(old_path, new_ws)
            except Exception:
                alog.exception("workspace switch callback failed")
        return new_ws

    def on_switch(self, callback) -> None:
        """Register callback(old_path:str|None, new_workspace:Workspace)."""
        self._on_switch.append(callback)

    # ── Validation / sandbox (sections 2, 13) ────────────────────────────────

    def is_within(self, resolved: Path, root: Path) -> bool:
        """True iff *resolved* is *root* or a descendant of it."""
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            return False

    def validate_path(self, requested: str, *, tool: str = "?") -> Path:
        """Resolve *requested* and confine it to the active workspace root.

        Returns the resolved absolute Path on success. Raises WorkspaceViolation
        (whose message is exactly BLOCKED_MSG) on any escape: `..`, absolute
        path outside root, symlink/junction escape, mounted-drive escape, or a
        UNC/network path.
        """
        ws = self.get_current_workspace()
        root = ws.root

        if requested is None or str(requested).strip() == "":
            alog.path_rejected(tool, requested="<empty>", root=str(root))
            raise WorkspaceViolation(requested)

        raw = str(requested)

        # Reject UNC / network paths outright (\\server\share, //server/share).
        if raw.startswith(("\\\\", "//")):
            alog.path_rejected(tool, requested=raw, root=str(root))
            raise WorkspaceViolation(raw)

        p = Path(raw).expanduser()
        # Relative paths are interpreted against the workspace root, NOT cwd, so
        # a tool can't be tricked by a process-cwd change (section 13).
        if not p.is_absolute():
            p = root / p

        try:
            resolved = p.resolve()
        except Exception as exc:
            alog.path_rejected(tool, requested=raw, root=str(root))
            # Chain the cause: the caller only ever sees WorkspaceViolation's
            # message, but a resolve() failure (bad symlink, ENAMETOOLONG) is
            # worth keeping in the traceback for the audit log.
            raise WorkspaceViolation(raw) from exc

        if not self.is_within(resolved, root):
            alog.path_rejected(tool, requested=str(resolved), root=str(root))
            raise WorkspaceViolation(raw)

        return resolved


# ── Module-level singleton ────────────────────────────────────────────────────
manager = WorkspaceManager()


# Convenience free functions (mirror the core.context / core.memory style so
# both interfaces import the same helpers — section 10).

def current() -> Workspace:
    return manager.get_current_workspace()


def root() -> Path:
    return manager.get_current_workspace().root


def validate_path(requested: str, *, tool: str = "?") -> Path:
    return manager.validate_path(requested, tool=tool)


def set_workspace(path: str) -> Workspace:
    return manager.set_workspace(path)
