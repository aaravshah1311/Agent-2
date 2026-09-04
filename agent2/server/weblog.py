# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/weblog.py
────────────────
Structured, colourised logging for Web mode.

Werkzeug's default one-line-per-request log is noisy and unhelpful (it prints
static-asset polls and socket.io long-poll frames at the same volume as real
API calls). This module replaces it with:

  • a timestamped, level-tagged, colour-coded formatter
  • per-request access logs that carry method / path / status / duration
  • noise filtering (socket.io transport frames + static assets are dropped
    unless AGENT2_LOG_LEVEL=debug)
  • socket lifecycle logs (connect / disconnect with live client count)
  • a startup summary block (keys, DB path, URLs, burp status)

Everything is opt-out safe: if anything here fails the app still runs, it just
logs less. Verbosity is controlled by AGENT2_LOG_LEVEL (debug|info|warn|error),
default "info", and colour by AGENT2_LOG_COLOR=0 to disable.

QUIET MODE (AGENT2_WEB_QUIET=1)
──────────────────────────────
In dual mode the CLI owns the terminal, so a web request log painting over the
prompt makes the CLI unusable. With AGENT2_WEB_QUIET=1 nothing here writes to
stdout: every line is redirected to `logs/agent2-web.log` instead,
and the startup banner is suppressed. Logs are not lost — they just move off the
shared console. agent2dual.py sets this by default.
"""

import logging
import os
import sys
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler

from agent2.config import log_path

# ── Level / colour configuration ──────────────────────────────────────────────

_LEVELS = {
    "debug": logging.DEBUG,
    "info":  logging.INFO,
    "warn":  logging.WARNING,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

LEVEL_NAME = (os.environ.get("AGENT2_LOG_LEVEL") or "info").strip().lower()
LEVEL      = _LEVELS.get(LEVEL_NAME, logging.INFO)
IS_DEBUG   = LEVEL <= logging.DEBUG

# Quiet mode: send every web log line to a file instead of the shared console.
# Used by dual mode, where the CLI owns the terminal. An explicit
# AGENT2_LOG_LEVEL=debug wins, so debugging the web half is still possible.
QUIET = (os.environ.get("AGENT2_WEB_QUIET") or "").strip().lower() in (
    "1", "yes", "true", "on") and not IS_DEBUG

# Colour is on for a TTY unless explicitly disabled. Docker logs are not a TTY,
# so this keeps `docker logs` output clean of escape codes by default. A file
# handler must never receive escape codes either.
_COLOR_ENV = (os.environ.get("AGENT2_LOG_COLOR") or "").strip()
if QUIET or _COLOR_ENV in ("0", "no", "false", "off"):
    COLOR = False
elif _COLOR_ENV in ("1", "yes", "true", "on"):
    COLOR = True
else:
    COLOR = bool(getattr(sys.stdout, "isatty", lambda: False)())


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if COLOR else text


def _dim(t):   return _c("2", t)
def _grey(t):  return _c("90", t)
def _green(t): return _c("32", t)
def _yellow(t):return _c("33", t)
def _red(t):   return _c("31", t)
def _cyan(t):  return _c("36", t)
def _mag(t):   return _c("35", t)
def _bold(t):  return _c("1", t)


# Public alias — callers (e.g. sockets.py) use this to de-emphasise detail in a
# log line without reaching into the private helpers above.
def dim(t):    return _dim(t)


# ── Formatter ─────────────────────────────────────────────────────────────────

_TAGS = {
    logging.DEBUG:    ("DBG", _grey),
    logging.INFO:     ("INF", _cyan),
    logging.WARNING:  ("WRN", _yellow),
    logging.ERROR:    ("ERR", _red),
    logging.CRITICAL: ("CRT", _red),
}


class Agent2Formatter(logging.Formatter):
    """`HH:MM:SS  LVL  scope  message` — aligned, coloured, single line."""

    def format(self, record: logging.LogRecord) -> str:
        tag, paint = _TAGS.get(record.levelno, ("INF", _cyan))
        # Local wall-clock on purpose: this is the console timestamp a human
        # reads next to their own clock. UTC here would be actively confusing.
        ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        scope = getattr(record, "scope", None) or record.name.replace("a2web", "web")
        msg = record.getMessage()

        line = (f"{_grey(ts)}  {paint(tag)}  "
                f"{_mag(scope[:9].ljust(9))}  {msg}")

        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


# ── Noise filter ──────────────────────────────────────────────────────────────

# Paths that produce constant chatter and carry no diagnostic value at INFO.
_NOISY_PREFIXES = ("/socket.io", "/favicon.ico", "/style.css", "/script.js")


def _is_noise(path: str) -> bool:
    return any(path.startswith(p) for p in _NOISY_PREFIXES)


class _WerkzeugFilter(logging.Filter):
    """Drop werkzeug's own access lines — we emit richer ones ourselves."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        # Werkzeug access lines look like: 127.0.0.1 - - [..] "GET / HTTP/1.1" 200 -
        if '"' in msg and ("HTTP/1." in msg or "HTTP/2" in msg):
            return False
        return True


def silence_flask_banner() -> None:
    """Suppress Flask's ' * Serving Flask app ...' / ' * Debug mode: off' block.

    Those lines are printed straight to stderr by werkzeug (not via logging), so
    a log filter can't catch them. Flask honours this env var to stay quiet.
    """
    os.environ.setdefault("FLASK_RUN_FROM_CLI", "false")
    try:
        import flask.cli
        flask.cli.show_server_banner = lambda *a, **k: None
    except Exception:
        pass


# ── Logger wiring ─────────────────────────────────────────────────────────────

# Deliberately NOT under the "agent2" namespace. agent2/core/logging.py owns
# that logger and attaches a rotating file handler (agent2.log) to it — taking
# it over here would silently destroy the structured audit trail. We keep our
# own console-facing logger and leave core.logging entirely alone.
_LOG = logging.getLogger("a2web")
_CONFIGURED = False

# Where quiet-mode logs go: the shared logs/ folder (see config.LOG_DIR), so all
# log files live in one place instead of scattered at the project root. Resolved
# through config.log_path() which creates the folder and never raises — an
# unwritable path still degrades to a NullHandler rather than crashing startup.
WEB_LOG_FILE = log_path("agent2-web.log")


def _make_handler() -> logging.Handler:
    """A stdout handler normally; a rotating file handler in quiet mode.

    Quiet mode must never write to the shared console — in dual mode that console
    belongs to the CLI prompt. If the log file can't be opened we fall back to a
    NullHandler (drop the lines) rather than back to stdout, because polluting
    the CLI is worse than losing web access logs.
    """
    if not QUIET:
        return logging.StreamHandler(sys.stdout)
    try:
        return RotatingFileHandler(
            str(WEB_LOG_FILE), maxBytes=1_000_000, backupCount=2,
            encoding="utf-8", delay=True)
    except Exception:
        return logging.NullHandler()


def setup(app=None, socketio=None) -> logging.Logger:
    """Install the Agent2 log formatter and quiet the noisy third-party loggers.

    Safe to call more than once. Returns the `a2web` logger.

    NOTE: this only configures console presentation for the *web server* and
    the third-party libraries it drags in. The application-level audit log
    (agent2/core/logging.py → agent2.log) is untouched by design.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return _LOG

    handler = _make_handler()
    handler.setFormatter(Agent2Formatter())

    if not IS_DEBUG:
        silence_flask_banner()

    _LOG.handlers[:] = [handler]
    _LOG.setLevel(LEVEL)
    _LOG.propagate = False

    # Werkzeug: keep real warnings/errors, drop its access log (we do our own).
    wz = logging.getLogger("werkzeug")
    wz.handlers[:] = [handler]
    wz.setLevel(logging.WARNING if not IS_DEBUG else logging.INFO)
    wz.addFilter(_WerkzeugFilter())
    wz.propagate = False

    # engineio / socketio internals are extremely chatty — debug only.
    for name in ("engineio", "engineio.server", "socketio", "socketio.server"):
        lg = logging.getLogger(name)
        lg.handlers[:] = [handler]
        lg.setLevel(logging.DEBUG if IS_DEBUG else logging.ERROR)
        lg.propagate = False

    if app is not None:
        app.logger.handlers[:] = [handler]
        app.logger.setLevel(LEVEL)
        app.logger.propagate = False
        _install_request_logging(app)

    _CONFIGURED = True
    return _LOG


def log(scope: str, msg: str, level: int = logging.INFO) -> None:
    """Emit one line under *scope* (shown in the scope column)."""
    try:
        _LOG.log(level, msg, extra={"scope": scope})
    except Exception:
        pass


def info(scope, msg):  log(scope, msg, logging.INFO)
def warn(scope, msg):  log(scope, msg, logging.WARNING)
def error(scope, msg): log(scope, msg, logging.ERROR)
def debug(scope, msg): log(scope, msg, logging.DEBUG)


# ── Per-request access logging ────────────────────────────────────────────────

def _status_paint(code: int):
    if code >= 500: return _red
    if code >= 400: return _yellow
    if code >= 300: return _grey
    return _green


def _install_request_logging(app) -> None:
    """Log every non-noise request with method, path, status and duration."""
    from flask import g, request

    @app.before_request
    def _a2_log_start():
        g._a2_t0 = time.perf_counter()

    @app.after_request
    def _a2_log_end(response):
        try:
            path = request.path or "/"
            if _is_noise(path) and not IS_DEBUG:
                return response

            dt_ms = (time.perf_counter() - getattr(g, "_a2_t0", time.perf_counter())) * 1000
            paint = _status_paint(response.status_code)

            # Slow requests are worth flagging even when they succeed.
            dur = f"{dt_ms:6.1f}ms"
            dur = _yellow(dur) if dt_ms >= 1000 else _dim(dur)

            log("http",
                f"{_bold(request.method.ljust(6))} {path:<34} "
                f"{paint(str(response.status_code))}  {dur}",
                logging.INFO)
        except Exception:
            pass
        return response

    @app.errorhandler(Exception)
    def _a2_log_error(exc):
        # Re-raise HTTP errors untouched; only log genuine 500s.
        from werkzeug.exceptions import HTTPException
        if isinstance(exc, HTTPException):
            return exc
        _LOG.error("unhandled exception on %s %s", request.method, request.path,
                   exc_info=True, extra={"scope": "http"})
        # Also record it in the structured audit trail (agent2.log).
        try:
            from agent2.core import logging as alog
            alog.exception("web.unhandled", method=request.method, path=request.path)
        except Exception:
            pass
        return ("Internal Server Error", 500)


# ── Socket lifecycle logging ──────────────────────────────────────────────────

_clients: set[str] = set()


def socket_connected(sid: str) -> int:
    _clients.add(sid)
    info("socket", f"client connected   {_dim(sid[:8])}  "
                   f"{_dim('(' + str(len(_clients)) + ' online)')}")
    return len(_clients)


def socket_disconnected(sid: str) -> int:
    _clients.discard(sid)
    info("socket", f"client disconnected {_dim(sid[:8])}  "
                   f"{_dim('(' + str(len(_clients)) + ' online)')}")
    return len(_clients)


def client_count() -> int:
    return len(_clients)


# ── Startup summary ───────────────────────────────────────────────────────────

def startup_banner(host: str, port: int, *, mode: str = "web") -> None:
    """Print the pre-serve summary: platform, keys, DB, URLs.

    No-op in quiet mode — dual mode prints its own two-line summary instead, so
    the CLI banner isn't buried under the web half's startup block.
    """
    if QUIET:
        return

    from agent2.config import OS_NAME, SHELL_LABEL, DB
    from agent2.llm.keys import rotator

    bar = _cyan("─" * 58)
    print()
    print(f"  {bar}")
    print(f"  {_bold(_mag('Agent2'))}  {_dim(mode + ' mode')}   "
          f"{_dim(OS_NAME + ' / ' + SHELL_LABEL)}")
    print(f"  {bar}")

    # Keys
    try:
        keys = rotator.status()
    except Exception:
        keys = []
    if not keys:
        print(f"  {_yellow('[!]')}  No API keys configured — "
              f"{_dim('add one with')} {_cyan('python run.py --addapi')}")
    else:
        # Count only. Key previews are not printed on every start: this banner
        # scrolls past in shared terminals, screen shares and `docker logs`, and
        # a per-key breakdown is available on demand in the UI's Keys panel.
        active = sum(1 for k in keys if k.get("active"))
        print(f"  {_green('[OK]')}  {len(keys)} API key(s)  "
              f"{_dim('(' + str(active) + ' active · •••• hidden)')}")

    # State + endpoints
    print(f"  {_green('[OK]')}  Database  {_dim(str(DB))}")
    print(f"  {_green('[OK]')}  Log level {_dim(LEVEL_NAME)}  "
          f"{_dim('(AGENT2_LOG_LEVEL=debug for verbose)')}")

    from agent2.server import ports
    local, network = ports.urls_for(port, host)
    for i, u in enumerate(local):
        print(f"  {_cyan('[>>]')}  {'Local  ' if i == 0 else '       '}   {_bold(u)}")
    for i, u in enumerate(network):
        print(f"  {_cyan('[>>]')}  {'Network' if i == 0 else '       '}   {_bold(u)}")

    # Task 14: what the auth model actually is, asked at print time.
    # ⚠️ This block used to be a hard-coded "no auth" warning. A warning that
    # cannot go stale is a warning that keeps being printed after the hole is
    # closed — and one that keeps being printed is one people stop reading. The
    # lines come from `auth.banner_note()` so the banner and the guard can never
    # describe two different servers.
    try:
        from agent2.server import auth as _auth
        notes = _auth.banner_note(host, port)
    except Exception:
        notes = []
    for kind, text in notes:
        tag = {"ok": _green("[OK]"), "warn": _yellow("[!]"),
               "info": _cyan("[>>]")}.get(kind, _cyan("[>>]"))
        print(f"  {tag}  {text}")
    if network:
        print(f"  {_cyan('[>>]')}       reachable on your network — "
              f"{_dim('set AGENT2_HOST=127.0.0.1 for local-only')}")
    print(f"  {bar}")
    print()
