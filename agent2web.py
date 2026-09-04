#!/usr/bin/env python3
# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2web.py — Agent2 entry point
────────────────────────────
Wires together the agent2 package and starts the Flask-SocketIO server.

Project layout
──────────────
  agent2web.py         ← you are here  (python run.py  or  python agent2web.py)
  agent2cli.py         ← agent2 CLI entry point (python agent2cli.py)
  run.py               ← cross-platform setup launcher (creates venv, installs deps)
  agent2.db            ← SQLite database — API keys, memories, rules, providers
  agent2/
    __init__.py
    config.py          ← platform detection, models, modes, constants, root paths
    database.py        ← SQLite helpers (qall / qone / exe) + schema + migrations
    keys.py            ← KeyRotator: multi-key, rotation, pinning, usage tracking
    terminal.py        ← stream_command, stdin injection, kill, stop events
    agent.py           ← system_prompt, build_context, run_agent agentic loop
    routes.py          ← all /api/* REST endpoints
    sockets.py         ← all Socket.IO event handlers
    ui.py              ← full HTML / CSS / JS single-page frontend
"""

#!/usr/bin/env python3
"""
agent2web.py — Agent2 entry point
"""

# ─────────────────────────────────────────────────────────────────────────────
# 🔒 VENV ENFORCEMENT (MUST RUN FIRST)
# ─────────────────────────────────────────────────────────────────────────────

import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
VENV_DIR = BASE_DIR / ".venv"
IS_WIN = os.name == "nt"

# The startup banner draws box characters. On Windows the default stdout codec
# is cp1252, which cannot encode them — and when stdout is redirected to a file
# or a pipe (`agent2web.py > log.txt`, a service wrapper, nohup) that raises
# UnicodeEncodeError and takes the whole server down before it ever binds.
# agent2cli.py and agent2dual.py already do this; the web entry point was the
# one that didn't. errors="replace" keeps a legacy console readable instead of
# fatal.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def in_venv():
    """Detect if currently inside a virtual environment."""
    return (
        hasattr(sys, "real_prefix") or
        (hasattr(sys, "base_prefix") and sys.base_prefix != sys.prefix)
    )


def get_venv_python():
    """Return platform-specific venv python path."""
    if IS_WIN:
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def ensure_venv():
    """Ensure execution inside venv, else restart using it."""
    if in_venv():
        return

    venv_python = get_venv_python()

    if not venv_python.exists():
        print("\n  [ERR] Virtual environment not found or not initialized.")
        print("       Please run one of the following:\n")
        print("         python run.py")
        print("         python run.py --reset\n")
        sys.exit(1)

    print("\n  [INFO] Switching to virtual environment...\n")

    try:
        os.execv(
            str(venv_python),
            [str(venv_python)] + sys.argv
        )
    except Exception as e:
        print(f"\n  [ERR] Failed to switch to venv: {e}")
        print("\n  Suggested fix:")
        print("     python run.py")
        print("     python run.py --reset\n")
        sys.exit(1)


# 🔥 Enforce venv BEFORE any imports that rely on dependencies
ensure_venv()


# ─────────────────────────────────────────────────────────────────────────────
# 📦 Imports (safe after venv enforcement)
# ─────────────────────────────────────────────────────────────────────────────

from flask import Flask, Response
from flask_socketio import SocketIO
from flask import send_from_directory

# All API keys (Gemini + custom providers) live in agent2.db — no .env.
from agent2.config   import OS_NAME, SHELL_LABEL
from agent2.database import init_db
init_db()
from agent2.llm.providers import init_providers_table
init_providers_table()
from agent2.llm.keys     import rotator
from agent2.server.routes   import register_routes
from agent2.server.sockets  import register_sockets
from agent2.server.ui       import get_html
from agent2.server         import weblog, ports


# ─────────────────────────────────────────────────────────────────────────────
# 🌐 App Setup
# ─────────────────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or os.urandom(32)

# Structured logging: replaces werkzeug's raw access log with timestamped,
# level-tagged lines and filters socket.io/static noise. See agent2/weblog.py.
weblog.setup(app)

socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="threading",
    logger=weblog.IS_DEBUG,
    engineio_logger=weblog.IS_DEBUG,
)


# ─────────────────────────────────────────────────────────────────────────────
# 🔌 Register Components
# ─────────────────────────────────────────────────────────────────────────────

register_routes(app)
register_sockets(socketio)

# Cross-process sync: the CLI (a separate process in dual mode) writes to the same
# agent2.db. Version counters in `sync_state` tell us when it did, and this daemon
# poller turns that into in-process events so cached state (system prompt,
# memories/rules blocks, PIL prediction index) is invalidated instead of going
# stale. Daemon thread — it can never hold the process open on shutdown, and a
# failed poll is retried on the next tick rather than propagating.
from agent2.core import sync as _sync
_sync.poller.start()


# ─────────────────────────────────────────────────────────────────────────────
# 🖥️ Frontend
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return Response(get_html(), mimetype="text/html")

@app.route('/favicon.ico')
def favicon():
    return send_from_directory(
        os.path.join(app.root_path, 'public'), 
        'favicon.ico', 
        mimetype='image/vnd.microsoft.icon'
    )
@app.route('/style.css')
def style():
    return send_from_directory(
        os.path.join(app.root_path, 'public'), 
        'style.css', 
        mimetype='text/css'
    )

@app.route('/script.js')
def script():
    return send_from_directory(
        os.path.join(app.root_path, 'public'), 
        'script.js', 
        mimetype='application/javascript'
    )

# ─────────────────────────────────────────────────────────────────────────────
# 🚀 Main Runner
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    try:
        init_db()

        HOST = os.environ.get("AGENT2_HOST", "0.0.0.0")
        try:
            WANTED = int(os.environ.get("AGENT2_PORT", ports.DEFAULT_PORT))
        except ValueError:
            WANTED = ports.DEFAULT_PORT

        # If 1311 is taken (another Agent2, or anything else), fall back to the
        # next FREE and SAFE port — never a privileged or well-known service port.
        PORT, exact = ports.find_free_port(WANTED, HOST)
        if not exact:
            weblog.warn("server",
                        f"port {WANTED} is in use — using {PORT} instead")

        weblog.startup_banner(HOST, PORT, mode="web")

        # Open the UI once the server is up (daemon timer; AGENT2_NO_BROWSER=1
        # to skip on headless/remote boxes).
        ports.open_browser(PORT)

        socketio.run(
            app,
            host=HOST,
            port=PORT,
            debug=False,
            allow_unsafe_werkzeug=True,
            log_output=weblog.IS_DEBUG,
        )

    # ─────────────────────────────────────────────────────────────────────────
    # 🛑 Graceful Shutdown
    # ─────────────────────────────────────────────────────────────────────────
    except KeyboardInterrupt:
        weblog.info("server", "shutting down (Ctrl+C) — bye.")
        sys.exit(0)

    # ─────────────────────────────────────────────────────────────────────────
    # 💥 Fail-Safe Error Handler
    # ─────────────────────────────────────────────────────────────────────────
    except Exception as e:
        weblog.error("server", f"fatal: {str(e)[:300]}")
        print("\n  Suggested fix:")
        print("     python run.py")
        print("     python run.py --reset\n")

        sys.exit(1)