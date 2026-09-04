# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""Pytest configuration for the AP Bot test suite.

The ``ap_bot`` package lives under ``.github/`` alongside this test
directory, which is not on the default import path.  This adds that
directory to ``sys.path`` so ``import ap_bot`` works from the test modules.
"""

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
# on the real database. We point it at a per-run temp file that is left to the OS
# to reap; every DB-backed test gets a clean, throwaway store.
if not os.environ.get("AGENT2_DB"):
    _tmp_db = Path(tempfile.gettempdir()) / "agent2_pytest.db"
    try:
        if _tmp_db.exists():
            _tmp_db.unlink()
    except Exception:
        pass
    os.environ["AGENT2_DB"] = str(_tmp_db)
