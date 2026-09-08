# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""Pytest configuration for the AP Bot test suite.

The ``ap_bot`` package lives under ``.github/`` alongside this test
directory, which is not on the default import path.  This adds that
directory to ``sys.path`` so ``import ap_bot`` works from the test modules.
"""

import atexit
import os
import sys
import tempfile
from pathlib import Path

_GITHUB_DIR = Path(__file__).resolve().parent.parent
if str(_GITHUB_DIR) not in sys.path:
    sys.path.insert(0, str(_GITHUB_DIR))

# ── Isolate agent2 state from the developer's real agent2.db ──────────────────
# agent2.config binds `DB` from AGENT2_DB at *import* time, and agent2.database
# imports that value once. So the redirect MUST happen here in conftest — before
# pytest imports any test module that pulls in agent2 — or DB writes would land
# on the real database. Every DB-backed test gets a clean, throwaway store.
#
# ⚠️ The filename carries the PID, and that is load-bearing rather than tidy:
# this used to be one fixed `agent2_pytest.db` that was unlink()ed at import, so
# two pytest invocations running at once shared a single SQLite file and deleted
# it out from under each other mid-run. The symptom is not an error — it is a
# handful of unrelated tests going red in one process because the other process
# had just truncated the database, which reads exactly like a real regression and
# sends you looking for it in the wrong module. A per-process path makes
# concurrent runs (two terminals, an agent verifying while a suite runs, `-p
# xdist` workers) independent by construction. A child process inherits
# AGENT2_DB through the environment, so it still shares its parent's file.
if not os.environ.get("AGENT2_DB"):
    _tmp_db = Path(tempfile.gettempdir()) / f"agent2_pytest_{os.getpid()}.db"

    def _sweep(path: Path = _tmp_db) -> None:
        """Drop the store and both SQLite sidecars. Total — never raises."""
        for suffix in ("", "-wal", "-shm"):
            try:
                Path(str(path) + suffix).unlink(missing_ok=True)
            except Exception:
                pass

    _sweep()
    # A PID-named file is not reaped by the next run the way a fixed name was,
    # so this run cleans up after itself instead of leaving one behind per run.
    atexit.register(_sweep)
    os.environ["AGENT2_DB"] = str(_tmp_db)
