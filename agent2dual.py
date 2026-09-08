#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2dual.py — Agent2 Dual Mode (Web + CLI together)
─────────────────────────────────────────────────────
Runs both interfaces in one command:

  • the Web UI serves in a background thread (http://localhost:1311)
  • the CLI runs in the foreground, so you keep a normal interactive terminal

Both halves share the same agent2.db, so memories, rules, keys, providers and
chats are identical whichever surface you use.

Run via:  python run.py --dual      (or just `python run.py` — it's the default)
      or: .venv/bin/python agent2dual.py

The CLI owns this terminal. The web half therefore runs QUIET by default: its
request/socket logs go to `logs/agent2-web.log` instead of stdout, so
nothing paints over the CLI prompt. Set AGENT2_WEB_QUIET=0 to put them back on
the console, or AGENT2_LOG_LEVEL=debug for verbose web logs (which also
re-enables console output).
"""

import os
import sys
import threading
import time
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# 🤫 WEB-LOG QUIETING (MUST BE SET BEFORE agent2.server.weblog IS IMPORTED)
# ─────────────────────────────────────────────────────────────────────────────
# weblog reads this at import time to pick its handler. Dual mode shares ONE
# terminal with the CLI, so the web half must not log to stdout unless the user
# explicitly asks for it. setdefault → an explicit AGENT2_WEB_QUIET=0 still wins.
os.environ.setdefault("AGENT2_WEB_QUIET", "1")

# Same problem, different logger. agent2/core/logging.py mirrors WARNING+ to
# stderr, and ordinary web events raise warnings — closing a browser tab fires
# `session.cancel`, which would print over the CLI prompt from a background
# thread mid-answer. The rotating agent2.log file handler is unaffected, so the
# audit trail stays complete; only the console mirror is off. Inherited by the
# CLI child through its copied env, which is what we want: the CLI's own
# warnings surface through status_line(), not through a second raw stderr feed.
os.environ.setdefault("AGENT2_LOG_CONSOLE", "0")

# ─────────────────────────────────────────────────────────────────────────────
# 🔒 VENV ENFORCEMENT (MUST RUN FIRST)
# ─────────────────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent
VENV_DIR = BASE_DIR / ".venv"
IS_WIN = os.name == "nt"


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
        os.execv(str(venv_python), [str(venv_python)] + sys.argv)
    except Exception as e:
        print(f"\n  [ERR] Failed to switch to venv: {e}")
        print("\n  Suggested fix:")
        print("     python run.py")
        print("     python run.py --reset\n")
        sys.exit(1)


# 🔥 Enforce venv BEFORE any imports that rely on dependencies
ensure_venv()

# Windows console: match agent2cli.py so box-drawing glyphs render.
if IS_WIN:
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


# ─────────────────────────────────────────────────────────────────────────────
# 📦 Imports (safe after venv enforcement)
# ─────────────────────────────────────────────────────────────────────────────

import subprocess

from flask import Flask, Response, send_from_directory
from flask_socketio import SocketIO

from agent2.database import init_db
from agent2.llm.providers import init_providers_table
from agent2.server.routes import register_routes
from agent2.server.sockets import register_sockets
from agent2.server.ui import get_html
from agent2.server import weblog, ports
from agent2.core import sync as _sync


# ─────────────────────────────────────────────────────────────────────────────
# 🌐 Web half — same wiring as agent2web.py, in a thread
# ─────────────────────────────────────────────────────────────────────────────

def build_web_app():
    """Create the Flask app + SocketIO exactly as agent2web.py does."""
    app = Flask(__name__, root_path=str(BASE_DIR))
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or os.urandom(32)

    weblog.setup(app)

    socketio = SocketIO(
        app,
        cors_allowed_origins="*",
        async_mode="threading",
        logger=weblog.IS_DEBUG,
        engineio_logger=weblog.IS_DEBUG,
    )

    register_routes(app)
    register_sockets(socketio)

    # Cross-process sync matters MOST here: dual mode runs the web half on a
    # daemon thread while the CLI runs as a separate child process, both against
    # one agent2.db. Without the poller, a memory or rule added in the CLI would
    # not invalidate the web half's cached system prompt. Daemon thread; a failed
    # poll is retried on the next tick. See agent2/core/sync.py.
    # ⚠️ This covers ONE DIRECTION ONLY — it is why `agent2cli.py:main()` starts a
    # poller of its own. Each process republishes what the others wrote, so a
    # surface with no poller is deaf no matter how many the other half runs.
    _sync.poller.start()

    @app.route("/")
    def index():
        return Response(get_html(), mimetype="text/html")

    @app.route("/favicon.ico")
    def favicon():
        return send_from_directory(
            os.path.join(app.root_path, "public"),
            "favicon.ico", mimetype="image/vnd.microsoft.icon")

    @app.route("/style.css")
    def style():
        return send_from_directory(
            os.path.join(app.root_path, "public"),
            "style.css", mimetype="text/css")

    @app.route("/script.js")
    def script():
        return send_from_directory(
            os.path.join(app.root_path, "public"),
            "script.js", mimetype="application/javascript")

    return app, socketio


def serve_web(app, socketio, host: str, port: int):
    """Blocking serve — run on a daemon thread. Never kills the CLI on error."""
    try:
        socketio.run(
            app,
            host=host,
            port=port,
            debug=False,
            allow_unsafe_werkzeug=True,
            use_reloader=False,          # a reloader in a thread would re-exec
            log_output=weblog.IS_DEBUG,
        )
    except Exception as e:
        weblog.error("server", f"web half stopped: {str(e)[:200]}")
        weblog.warn("server", "the CLI below is unaffected — continue as normal.")


# ─────────────────────────────────────────────────────────────────────────────
# 🖥️ CLI half — foreground child process
# ─────────────────────────────────────────────────────────────────────────────

def run_cli() -> int:
    """Run agent2cli.py in the foreground, inheriting this terminal.

    A child process (rather than an import) keeps prompt_toolkit's full-screen
    key handling on a real TTY and stops a CLI crash from taking the web server
    with it. We pass the CURRENT interpreter, which the venv guard above has
    already guaranteed is the venv python.
    """
    cli_script = BASE_DIR / "agent2cli.py"
    if not cli_script.exists():
        weblog.error("cli", f"agent2cli.py not found at {cli_script}")
        return 1

    env = os.environ.copy()
    env["AGENT2_DUAL"] = "1"          # lets the CLI know the web half is live
    try:
        return subprocess.run([sys.executable, str(cli_script)], env=env).returncode
    except KeyboardInterrupt:
        return 130


# ─────────────────────────────────────────────────────────────────────────────
# 🧾 Dual-mode summary (the ONLY thing the web half prints to the CLI terminal)
# ─────────────────────────────────────────────────────────────────────────────

def _print_dual_summary(port: int, wanted: int, exact: bool, bound: bool,
                        host: str) -> None:
    """Print a short, static block telling the user where the web half lives.

    In quiet mode this replaces the web half's whole startup banner + log stream,
    so the CLI banner that follows stays readable. Nothing else is printed to this
    terminal by the web server for the rest of the session.
    """
    tty = bool(getattr(sys.stdout, "isatty", lambda: False)())
    def _c(code: str, t: str) -> str:
        return f"\033[{code}m{t}\033[0m" if tty else t

    cyan, dim, green, yellow = (lambda t: _c("36", t)), (lambda t: _c("2", t)), \
                               (lambda t: _c("32", t)), (lambda t: _c("33", t))

    local, network = ports.urls_for(port, host)
    state = green("running") if bound else yellow("not responding yet")
    print()
    print(f"  {cyan('─' * 52)}")
    print(f"  {_c('1;35', 'Agent2')}  {dim('dual mode')}")
    print(f"  {cyan('─' * 52)}")
    for i, u in enumerate(local):
        head = "Web UI  " if i == 0 else "        "
        tail = f"  {dim('(' + state + ')')}" if i == 0 else ""
        print(f"  {head} {_c('1', u)}{tail}")
    for i, u in enumerate(network):
        print(f"  {'Network ' if i == 0 else '        '} {_c('1', u)}")
    if network:
        _note = dim("no auth; AGENT2_HOST=127.0.0.1 for local-only")
        print(f"  {yellow('[!]')}      open to your network — {_note}")
    if not exact:
        print(f"  {yellow('[!]')}      port {wanted} was busy — using {port} instead")
    print(f"  CLI      {dim('this terminal (below)')}")
    if weblog.QUIET:
        print(f"  Web logs {dim(str(weblog.WEB_LOG_FILE))}")
        print(f"           {dim('kept off this console so the CLI stays readable')}")
        print(f"           {dim('AGENT2_WEB_QUIET=0 to show them here')}")
    print(f"  {dim('/exit in the CLI shuts down both halves.')}")
    print(f"  {cyan('─' * 52)}")
    print()
    time.sleep(0.4)


# ─────────────────────────────────────────────────────────────────────────────
# 🚀 Main Runner
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    init_db()
    init_providers_table()

    # Crash recovery (Task 25 §1) — once, here, before either half is live.
    # ⚠️ THE CLI CHILD SCANS TOO, AND THAT IS NOT A DUPLICATE BUG. Dual mode is two
    # processes over one DB, so each has to be able to recover on its own when run
    # alone; running this first simply means the child finds the work already
    # resolved and skips it (`_advance` compare-and-swaps through
    # `resolve_interrupted`, and a settled recovery row is in the skip set). It still
    # PRINTS the panel, because that reads the durable rows rather than its own scan.
    from agent2.core.recovery import crash as _crash
    _crash.scan_on_start()

    host = os.environ.get("AGENT2_HOST", "0.0.0.0")
    try:
        wanted = int(os.environ.get("AGENT2_PORT", ports.DEFAULT_PORT))
    except ValueError:
        wanted = ports.DEFAULT_PORT

    # Pick a safe, free port so a second instance (or a busy 1311) still works.
    try:
        port, exact = ports.find_free_port(wanted, host)
    except OSError as e:
        # Genuine startup failure: print it, don't just log it. In quiet mode the
        # log goes to a file the user isn't watching, and silently dropping to
        # CLI-only would look like `--dual` simply ignored the web half.
        weblog.setup()
        weblog.error("server", str(e))
        print(f"\n  [!] Could not start the web half: {str(e)[:200]}")
        print("      Continuing with the CLI only.\n")
        return run_cli()

    app, socketio = build_web_app()

    weblog.startup_banner(host, port, mode="dual (web + cli)")   # no-op when quiet
    weblog.info("server", "web half starting in the background ...")

    threading.Thread(
        target=serve_web, args=(app, socketio, host, port), daemon=True
    ).start()

    # Give the server a beat to bind before we hand the terminal over.
    deadline = time.time() + 5.0
    while time.time() < deadline and ports.is_free(port, host):
        time.sleep(0.1)

    bound = not ports.is_free(port, host)
    if bound:
        weblog.info("server", f"web UI ready  ->  http://localhost:{port}")
        # Only worth a browser tab if something is actually listening.
        ports.open_browser(port, delay=0.2)
    else:
        weblog.warn("server", "web half did not bind in time — continuing with the CLI.")

    _print_dual_summary(port, wanted, exact, bound, host)

    code = run_cli()

    weblog.info("cli", "CLI session ended.")
    weblog.info("server", "stopping the web half — bye.")
    if weblog.QUIET:
        print(f"\n  Web half stopped. Bye.\n")
    return code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n\n  [INFO] Shutting down Agent2 dual mode ...")
        print("  [OK] Cleanup complete. Bye.\n")
        sys.exit(130)
    except Exception as e:
        print("\n  [FATAL] Runtime error occurred:")
        print(f"          {str(e)[:300]}\n")
        print("  Suggested fix:")
        print("     python run.py")
        print("     python run.py --reset\n")
        sys.exit(1)
