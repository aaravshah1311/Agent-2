# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/cli/env.py
─────────────────
Everything the CLI needs to know about the machine it landed on, decided ONCE at
import time: paths, platform flags, and which optional dependencies are present.

⚠️ NOTHING HERE MAY RAISE AT IMPORT.
`python agent2cli.py --help` has to work on a half-installed machine, and
`run.py` launches this module by path before it has finished installing
dependencies. So every optional import degrades to a flag (`_RICH`, `_PTK`,
`_GENAI`, `_DB_OK`, …) and the callers branch on the flag. That posture predates
this split and is not negotiable — it is why a broken install reports "no keys"
instead of a traceback.

Every name here is settled at import and never rebound, so `from .env import
_RICH` is safe. Contrast `theme.P`, which IS mutated and must be reached through
the object. See the package docstring.
"""

import os
import platform
import shutil
import sys
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path.home() / ".agent2"
HST_FILE = DATA_DIR / "history.json"    # kept for legacy migration only
MEM_FILE = DATA_DIR / "memories.json"   # kept for legacy migration only
PT_HISTORY = DATA_DIR / "cli_history.txt"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ── Platform ───────────────────────────────────────────────────────────────────
OS_NAME = platform.system()
IS_WIN = OS_NAME == "Windows"
IS_MAC = OS_NAME == "Darwin"


def fix_windows_console() -> None:
    """Enable ANSI + UTF-8 on a Windows console.

    ⚠️ The `reconfigure` half is load-bearing: cp1252 cannot encode the
    box-drawing glyphs in the banner, and with stdout redirected to a file or a
    pipe that raises UnicodeEncodeError and kills the process before it prints
    anything. Called from the entry point, not at import, so importing this
    module never touches a terminal.
    """
    if not IS_WIN:
        return
    os.system("chcp 65001 >nul 2>&1")
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleMode(
            ctypes.windll.kernel32.GetStdHandle(-11), 7)
    except Exception:
        pass
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# ── Rich (installed by run.py) ─────────────────────────────────────────────────
try:
    from rich.console import Console
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.syntax import Syntax
    from rich.table import Table
    from rich.text import Text
    from rich.progress import Progress, SpinnerColumn, TextColumn
    from rich import box as rbox
    _RICH = True
    _con = Console(highlight=False)
except ImportError:
    Console = Markdown = Panel = Syntax = Table = Text = None
    Progress = SpinnerColumn = TextColumn = rbox = None
    _RICH = False
    _con = None

# ── prompt_toolkit ─────────────────────────────────────────────────────────────
# Optional so `--help` and `/addapi` still work on a fresh machine before the
# interactive dependencies are installed.
try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.formatted_text import HTML
    from prompt_toolkit.styles import Style
    from prompt_toolkit.application import Application
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.layout.containers import HSplit, Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.layout.dimension import Dimension
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
    _PTK = True
    _PTK_IMPORT_ERROR = None
except ImportError as ex:
    PromptSession = FileHistory = HTML = Style = None
    Application = KeyBindings = Layout = HSplit = Window = FormattedTextControl = None
    Dimension = None
    Completer = Completion = AutoSuggestFromHistory = None
    _PTK = False
    _PTK_IMPORT_ERROR = ex

# ── Gemini ─────────────────────────────────────────────────────────────────────
# Never exit at import time; the agent call validates this later.
try:
    from google import genai
    from google.genai import types as gtypes
    _GENAI = True
    _GENAI_IMPORT_ERROR = None
except ImportError as ex:
    genai = None
    gtypes = None
    _GENAI = False
    _GENAI_IMPORT_ERROR = ex

# ── Burp Suite MCP bridge (optional) ───────────────────────────────────────────
try:
    from agent2.integrations.burp_mcp import burp as _burp
    _BURP_OK = True
    _BURP_IMPORT_ERROR = None
except Exception as _bex:
    _burp = None
    _BURP_OK = False
    _BURP_IMPORT_ERROR = _bex

# ── The MCP server registry (Task 8) ───────────────────────────────────────────
# Burp keeps its own handle above: it predates the registry, `/burp` still drives
# it directly, and `_BURP_OK` gates prose the registry knows nothing about. This
# is how the CLI reaches EVERY server, Burp included — `_mcp_registry.bridges()`.
try:
    from agent2.integrations import registry as _mcp_registry
    _MCP_OK = True
    _MCP_IMPORT_ERROR = None
except Exception as _mex:
    _mcp_registry = None
    _MCP_OK = False
    _MCP_IMPORT_ERROR = _mex

# ── Shared SQLite store ────────────────────────────────────────────────────────
# The CLI and the Web UI use the SAME agent2.db, so memories/rules/keys stay in
# sync across both surfaces.
try:
    from agent2.database import qall as _db_qall, exe as _db_exe, init_db as _db_init
    from agent2.database import (
        list_api_keys as _db_list_keys,
        add_api_key as _db_add_key,
        remove_api_key as _db_remove_key,
    )
    _db_init()
    _DB_OK = True
    _DB_IMPORT_ERROR = None
except Exception as _dbex:
    _db_qall = _db_exe = _db_init = None
    _db_list_keys = _db_add_key = _db_remove_key = None
    _DB_OK = False
    _DB_IMPORT_ERROR = _dbex

# Shared agent iteration cap (keeps the CLI provider loop consistent with Web).
try:
    from agent2.config import MAX_AGENT_ITERS
except Exception:
    MAX_AGENT_ITERS = 40

# ── Network resilience ─────────────────────────────────────────────────────────
# Extended-timeout client + transient-retry: what stops a mid-task "read
# operation timed out" from killing the run.
try:
    from agent2.config import MAX_RETRIES as _MAX_RETRIES
except Exception:
    _MAX_RETRIES = 5

try:
    from agent2.llm.resilience import (
        make_client as _make_client,
        call_with_retry as _call_with_retry,
        classify_error as _classify_net_error,
        is_blank_reply as _is_blank_reply,
        blank_reply_notice as _blank_reply_notice,
    )
    _RESILIENCE_OK = True
except Exception:
    _RESILIENCE_OK = False

    def _make_client(key):
        return genai.Client(api_key=key) if _GENAI and key else None

    def _call_with_retry(fn, **_kw):
        return fn()

    def _classify_net_error(exc):
        return ""

    def _is_blank_reply(text):
        return not (text or "").strip()

    def _blank_reply_notice(finish_reason=None):
        return "I didn't produce a reply that time. Ask again and I'll pick it up."


# ── Memories & Rules backend (centralized) ─────────────────────────────────────
# Thin adapters over the SAME backend the Web UI uses; no memory/rules logic is
# duplicated in the CLI.
try:
    from agent2.core import memory as _core_memory
    from agent2.core import rules as _core_rules
    _CORE_OK = _DB_OK
except Exception:
    _core_memory = _core_rules = None
    _CORE_OK = False

# `shutil` is re-exported because models.py's offline shell fallback needs it and
# importing it there too would be a second probe of the same thing.
__all__ = [
    "DATA_DIR",
    "HST_FILE",
    "IS_MAC",
    "IS_WIN",
    "MAX_AGENT_ITERS",
    "MEM_FILE",
    "OS_NAME",
    "PT_HISTORY",
    "ROOT",
    "_BURP_OK",
    "_CORE_OK",
    "_DB_OK",
    "_GENAI",
    "_PTK",
    "_RESILIENCE_OK",
    "_RICH",
    "_con",
    "fix_windows_console",
    "shutil",
]
