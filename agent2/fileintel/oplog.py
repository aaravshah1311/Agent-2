# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/fileintel/oplog.py
─────────────────────────
History logger for file operations.

Every operation the executor runs is recorded in the `file_ops` table (created
in agent2/database.py:init_db). Logging is best-effort — a logging failure must
never break the actual operation.
"""

from __future__ import annotations

import json
import uuid


def log_op(tool: str, operation: str, path: str,
           output_paths: list[str] | None = None,
           duration_ms: int = 0, ok: bool = True,
           error: str | None = None) -> None:
    """Insert one row into file_ops. Swallows all errors."""
    try:
        from agent2.database import exe
        exe(
            "INSERT INTO file_ops(id, tool, operation, path, output_paths, "
            "duration_ms, ok, error) VALUES(?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), tool, operation, path,
             json.dumps(output_paths or []), int(duration_ms),
             1 if ok else 0, (error or "")[:1000]),
        )
    except Exception:
        pass


def recent(limit: int = 50) -> list[dict]:
    try:
        from agent2.database import qall
        return qall("SELECT * FROM file_ops ORDER BY created_at DESC LIMIT ?", (limit,))
    except Exception:
        return []
