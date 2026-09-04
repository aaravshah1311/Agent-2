# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
Tests for the consolidated log folder (config.LOG_DIR / config.log_path).

Both logging layers (core/logging.py audit trail and server/weblog.py console
presentation) must write into the SAME folder, next to the DB, so a containerised
run with AGENT2_DB on a mounted volume keeps its logs on that volume.
"""

import logging
import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _restore_config():
    """These tests reload agent2.config against a temp AGENT2_DB.

    config.DB / config.LOG_DIR are module-level constants computed at import, so
    the reload is the only way to exercise them — but leaving the reloaded module
    (and the mutated env) in place would point every later test at a temp path.
    Snapshot the env, then reload config once more on the way out so the rest of
    the suite sees the original values.
    """
    keys = ("AGENT2_DB", "AGENT2_LOG_DIR", "AGENT2_LOG_CONSOLE", "AGENT2_WEB_QUIET")
    saved = {k: os.environ.get(k) for k in keys}
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    import importlib
    from agent2 import config
    from agent2.server import weblog
    importlib.reload(config)
    importlib.reload(weblog)


def test_log_dir_sits_next_to_db(tmp_path):
    os.environ["AGENT2_DB"] = str(tmp_path / "data" / "agent2.db")
    import importlib
    from agent2 import config
    importlib.reload(config)

    assert config.DB.parent == tmp_path / "data"
    assert config.LOG_DIR == tmp_path / "data" / "logs"


def test_log_dir_env_override(tmp_path):
    os.environ["AGENT2_DB"] = str(tmp_path / "data" / "agent2.db")
    os.environ["AGENT2_LOG_DIR"] = str(tmp_path / "custom_logs")
    import importlib
    from agent2 import config
    importlib.reload(config)

    assert config.LOG_DIR == tmp_path / "custom_logs"


def test_log_path_creates_folder_and_returns_path(tmp_path):
    os.environ["AGENT2_DB"] = str(tmp_path / "data" / "agent2.db")
    os.environ.pop("AGENT2_LOG_DIR", None)
    import importlib
    from agent2 import config
    importlib.reload(config)

    p = config.log_path("agent2.log")
    assert p == config.LOG_DIR / "agent2.log"
    assert p.parent.exists(), "log_path() must create the folder"


def test_audit_log_writes_into_log_dir(tmp_path):
    os.environ["AGENT2_DB"] = str(tmp_path / "agent2.db")
    os.environ["AGENT2_LOG_CONSOLE"] = "0"
    import importlib
    from agent2 import config
    from agent2.core import logging as alog
    importlib.reload(config)

    # The audit logger is a process-wide singleton that resolves its file path on
    # FIRST use and caches it — correct in production (config is settled before
    # anything logs), but it means an earlier test in the same process may already
    # have bound it to another path. Reset it so this test measures the new one.
    for h in list(alog._LOGGER.handlers) if alog._LOGGER else []:
        try:
            h.close()
        except Exception:
            pass
        alog._LOGGER.removeHandler(h)
    alog._LOGGER = None

    alog.event("test", logging.INFO, msg="log dir probe")
    logging.shutdown()

    assert (config.LOG_DIR / "agent2.log").exists()
    # The audit log must not appear beside the DB itself.
    assert not (config.DB.parent / "agent2.log").exists()


def test_web_log_writes_into_log_dir(tmp_path):
    os.environ["AGENT2_DB"] = str(tmp_path / "agent2.db")
    os.environ["AGENT2_WEB_QUIET"] = "1"        # force the file handler
    import importlib
    from agent2 import config
    from agent2.server import weblog
    importlib.reload(config)
    importlib.reload(weblog)

    assert weblog.WEB_LOG_FILE.parent == config.LOG_DIR
    weblog.setup()
    weblog.info("test", "web log dir probe")
    logging.shutdown()

    assert (config.LOG_DIR / "agent2-web.log").exists()
    assert not (config.DB.parent / "agent2-web.log").exists()
