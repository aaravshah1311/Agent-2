# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/fileintel/errors.py
──────────────────────────
Typed exceptions for the File Intelligence System.

Every failure mode the file pipeline can hit is one of these. The executor
catches them and turns them into a clean, structured result dict (never a raw
stack trace) so the agent — and the user — always get a meaningful message with
an actionable hint.
"""

from __future__ import annotations


class FileIntelError(Exception):
    """Base class for every File Intelligence error.

    Carries an optional `hint` (what the user should do) and a `code` so the
    result formatter can render a consistent, machine-readable error dict.
    """

    code = "file_error"

    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        self.message = message
        self.hint = hint

    def as_dict(self) -> dict:
        d = {"error": self.message, "code": self.code}
        if self.hint:
            d["hint"] = self.hint
        return d


class MissingDependency(FileIntelError):
    """A required third-party library or system binary is not installed.

    The router treats this as *recoverable*: it will try the next backend for
    the same operation before giving up. If every backend is missing, the user
    sees a single message listing what to install.
    """

    code = "missing_dependency"

    def __init__(self, dependency: str, hint: str | None = None, kind: str = "package"):
        self.dependency = dependency
        self.kind = kind  # "package" (pip) | "binary" (system) | "backend"
        default_hint = (
            f"pip install {dependency}" if kind == "package"
            else f"Install the '{dependency}' system tool and ensure it is on PATH."
        )
        super().__init__(
            f"Missing {kind}: '{dependency}' is required for this operation.",
            hint or default_hint,
        )


class UnsupportedOperation(FileIntelError):
    """The requested operation is not registered for this file's format."""

    code = "unsupported_operation"


class UnsupportedFormat(FileIntelError):
    """No plugin handles this file's format at all."""

    code = "unsupported_format"


class FileTooLarge(FileIntelError):
    """File exceeds the configured maximum processing size."""

    code = "file_too_large"


class UnsafePath(FileIntelError):
    """Path traversal / confinement / blocked-extension violation."""

    code = "unsafe_path"


class BackendError(FileIntelError):
    """A backend was available but failed while processing the file.

    Also recoverable at the router level — the next backend is attempted.
    """

    code = "backend_error"
