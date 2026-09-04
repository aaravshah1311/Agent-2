# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/config.py
────────────────
All constants: platform detection, model definitions, mode definitions,
tuneable limits, and shared root paths.
"""

import os
import shutil
import platform as _plat
from pathlib import Path

# ── Root paths ─────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent   # project root (where run.py lives)
# DB location defaults to the project root, but can be redirected via AGENT2_DB
# so containerised runs can point it at a mounted volume and share the same
# state across devices. A bare directory is accepted too (agent2.db is appended).
_DB_ENV = os.environ.get("AGENT2_DB", "").strip()
if _DB_ENV:
    _db_path = Path(_DB_ENV).expanduser()
    DB = _db_path / "agent2.db" if _db_path.is_dir() else _db_path
    DB.parent.mkdir(parents=True, exist_ok=True)
else:
    DB = ROOT / "agent2.db"
ENV  = ROOT / ".env"                   # legacy — only read once for migration

# ── Log directory ──────────────────────────────────────────────────────────────
# All log files live in ONE folder instead of being scattered at the project root.
# Anchored beside the DB rather than to ROOT so a containerised run (AGENT2_DB
# pointing at a mounted volume) keeps its logs on that same volume — otherwise
# logs would land inside the image and vanish when the container is recreated.
# Override with AGENT2_LOG_DIR. A bare relative path is resolved against ROOT.
_LOG_ENV = os.environ.get("AGENT2_LOG_DIR", "").strip()
if _LOG_ENV:
    _log_dir = Path(_LOG_ENV).expanduser()
    LOG_DIR = _log_dir if _log_dir.is_absolute() else (ROOT / _log_dir)
else:
    LOG_DIR = DB.parent / "logs"


def log_path(name: str) -> Path:
    """Absolute path for a log file inside LOG_DIR, creating the folder if needed.

    Never raises: if the directory cannot be created (read-only FS, permissions)
    the caller still gets a usable path, and the handler that opens it is already
    wrapped so an unwritable log degrades to console/NullHandler instead of
    killing startup.
    """
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return LOG_DIR / name

# NOTE: Agent2 no longer uses .env for configuration. All API keys (Gemini +
# custom providers) live in agent2.db. The ENV path above is kept solely so the
# database layer can perform a one-time import of any old .env keys, after which
# the file is renamed to .env.migrated and never read again.

# ── Platform ───────────────────────────────────────────────────────────────────
OS_NAME = _plat.system()   # "Windows" | "Darwin" | "Linux"
IS_WIN  = OS_NAME == "Windows"
IS_MAC  = OS_NAME == "Darwin"


def detect_shell() -> tuple[str, str, str]:
    """Return (shell_bin, shell_label, shell_flag)."""
    if IS_WIN:
        ps = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
        if ps:
            return ps, "PowerShell", "-Command"
        return "cmd.exe", "CMD", "/c"
    user_shell = os.environ.get("SHELL", "")
    for sh in [user_shell, "/bin/bash", "/bin/zsh", "/bin/sh"]:
        if sh and shutil.which(sh):
            return sh, Path(sh).name.upper(), "-c"
    return "/bin/sh", "SH", "-c"


SHELL_BIN, SHELL_LABEL, SHELL_FLAG = detect_shell()


def shell_argv(cmd: str) -> list[str]:
    """Build subprocess argv for the current platform."""
    if IS_WIN and SHELL_BIN.lower().endswith("cmd.exe"):
        return ["cmd.exe", "/c", cmd]
    return [SHELL_BIN, SHELL_FLAG, cmd]


# ── Models ─────────────────────────────────────────────────────────────────────
# ⚠️ KEYS MUST BE UNIQUE, AND PYTHON WILL NOT TELL YOU IF THEY ARE NOT.
# A duplicated key here is not an error — the later entry silently replaces the
# earlier one, the selector shows one row where you wrote two, and the model you
# thought you were calling is not the one that gets called. (`2.5-flash` was
# declared twice at one point, pointing the default model at `flash-lite`.)
# `llm/capabilities.py` derives each model's ranking from its `api` name and
# `group`, so a new row gets sensible metadata for free — no second table to edit.
MODELS: dict[str, dict] = {
    "2.5-flash":      {"api": "gemini-2.5-flash",      "label": "2.5 Flash",      "group": "2.5"},
    "2.5-flash-lite": {"api": "gemini-2.5-flash-lite", "label": "2.5 Flash-Lite", "group": "2.5"},
    "3.5-flash":      {"api": "gemini-3.5-flash",      "label": "3.5 Flash",      "group": "3.5"},
    "3.5-flash-lite": {"api": "gemini-3.5-flash-lite", "label": "3.5 Flash-Lite", "group": "3.5"},
    "3.6-flash":      {"api": "gemini-3.6-flash",           "label": "3.6 Flash",      "group": "3.6"},
    "3.7-flash":      {"api": "gemini-3.7-flash",      "label": "3.7 Flash",      "group": "3.7"},
}
DEFAULT_MODEL = "2.5-flash"

# ── Modes ──────────────────────────────────────────────────────────────────────
# A mode is HOW a turn should be handled; a model is WHICH endpoint answers it.
# ⚠️ AUTOMATIC MODEL SELECTION IS NOT A MODE, AND THAT IS A DELIBERATE DECISION.
# It lives as the `auto` pseudo-key on the MODEL side (`llm/router.AUTO`), because
# "which endpoint answers" is exactly what it decides. It is likewise NOT a member
# of `MODELS` — that dict is the list of things that can be *called*, and an entry
# there would reach `MODELS[key]["api"]` and be sent to a vendor as a model id.
# Every surface offers it as a sibling of that list. See `llm/router.py`.
MODES: dict[str, dict] = {
    "fast": {
        "label": "Fast", "icon": "⚡",
        "desc": "Fastest responses, lowest token usage",
        "max_tokens": 2048, "thinking": False, "thinking_budget": 0,
    },
    "pro": {
        "label": "Pro", "icon": "★",
        "desc": "Balanced — recommended for most tasks",
        "max_tokens": 8192, "thinking": False, "thinking_budget": 0,
    },
    "thinking": {
        "label": "Thinking", "icon": "🧠",
        "desc": "Deep reasoning via extended thinking (2.5/3.5 models only)",
        "max_tokens": 16384, "thinking": True, "thinking_budget": 8000,
    },
}
DEFAULT_MODE = "pro"

# ⚠️ WHICH MODEL GROUPS SUPPORT EXTENDED THINKING — ONE DECLARATION, AND IT IS A
# FUNCTION, NOT A LITERAL. `agent.py` gates `types.ThinkingConfig` on
# `supports_thinking()`, and `llm/capabilities.py` derives a model's `thinking`
# field from it. Both used to carry their own `("2.5", "3.1")` tuple — a fact stored
# twice, which drifted the moment the model table changed: a newly added group was
# absent from both lists, so `thinking` mode became a silent no-op on the newest
# model. The request succeeded, no budget was attached, and nothing said so.
#
# ⚠️ AN EMPTY TUPLE MEANS "EVERY GROUP", AND THAT IS THE DEFAULT ON PURPOSE.
# Every Gemini generation from 2.5 onward supports a thinking budget, so a
# maintained allowlist can only ever be wrong in the dangerous direction: silently
# dropping thinking from a model that supports it. Being permissive fails safely
# instead — the SDK rejects an unsupported `thinking_config` and `agent.py` already
# swallows that, skipping thinking exactly as an allowlist miss would have, but
# without being wrong about the models that do work. Populate the tuple only if a
# future family genuinely does not support it.
THINKING_GROUPS: tuple[str, ...] = ()


def supports_thinking(group: str) -> bool:
    """Whether models in *group* accept a thinking budget. THE one predicate."""
    return True if not THINKING_GROUPS else str(group or "") in THINKING_GROUPS

# ── Burp Suite MCP bridge ──────────────────────────────────────────────────────
# Agent2 connects to the official PortSwigger "MCP Server" BApp inside Burp,
# exposing every Burp tool (proxy history, Repeater, Intruder, Scanner, …) to
# the agent. Override via .env: BURP_MCP_URL / BURP_MCP_ENABLED.
BURP_MCP_URL     = os.environ.get("BURP_MCP_URL", "http://127.0.0.1:9876")
# Auto-connect is OFF by default — Burp connects only when the user explicitly
# clicks "Connect" (web) or runs `/burp connect` (CLI), or turns on the
# auto-connect toggle. Override the initial default via .env: BURP_MCP_ENABLED=1.
BURP_MCP_ENABLED = os.environ.get("BURP_MCP_ENABLED", "0").strip().lower() not in ("0", "false", "no", "off")

# ── OWASP ZAP MCP bridge (Task 8) ──────────────────────────────────────────────
# ZAP's official "MCP Integration" add-on runs an HTTP MCP server *inside* ZAP.
# Its documented defaults, which these mirror:
#   • port 8282 (NOT 8080 — that is ZAP's proxy port, a different server)
#   • "Enable MCP Server" ships DISABLED, so this is off here too
#   • "Secure Only" ships ENABLED, i.e. ZAP refuses plain HTTP until the user
#     unticks it or the client trusts ZAP's root CA
#   • an optional "Security Key" sent as the Authorization header VALUE, verbatim
#     — ZAP prepends no `Bearer`, so neither do we
# The add-on documents no endpoint path and does not name its wire transport, so
# `ZapMCP.TRANSPORTS` probes rather than assumes — see `integrations/zap_mcp.py`.
ZAP_MCP_URL     = os.environ.get("ZAP_MCP_URL", "http://127.0.0.1:8282")
ZAP_MCP_ENABLED = os.environ.get("ZAP_MCP_ENABLED", "0").strip().lower() not in ("0", "false", "no", "off")
# ⚠️ A SECRET. Never echo it into `status()`, a log line or a health payload.
ZAP_MCP_KEY     = os.environ.get("ZAP_MCP_KEY", "")

# ── Agent limits ───────────────────────────────────────────────────────────────
MAX_CTX_MESSAGES = 40       # max messages sent to the model per turn
MAX_TOOL_OUTPUT  = 6_000    # max chars from a command that go back into context
MAX_AGENT_ITERS  = 80       # max tool-call iterations per user message (big builds)

# ⚠️ Task 5: the widest SINGLE output line either surface will DRAW. Capture is
# never capped by this — only rendering is.
#
# A command that emits one enormous line (a minified bundle, a base64 blob, a
# `cat` of a build artefact) is not a deadlock, but it looks exactly like one.
# Rich re-wraps every line to the terminal width, and that cost grows faster than
# the line: ~0.1 s at 100 KB, 1.7 s at 1 MB, ~400 s at 8 MB — measured. The
# subprocess had already exited; the CLI was busy laying out one line. On the Web
# side the same line becomes one Socket.IO frame the browser must lay out.
#
# So the LINE is clipped for display, with a visible marker, while `stdout`/
# `stderr` keep every byte for the model. Truncating capture instead would hide
# real output from the agent, which is the one thing this must not do.
MAX_OUTPUT_LINE = 8_000


def clip_output_line(line: str, limit: int = MAX_OUTPUT_LINE) -> str:
    """Return *line* safe to draw, marked if it was clipped.

    One declaration, two call sites (`cli/runtime.py`, `terminal.py`) — the
    marker text must read the same on both surfaces.

    ⚠️ NO SQUARE BRACKETS IN THE MARKER. The CLI draws through Rich, which reads
    `[...]` as markup: a marker written `…[line clipped]` was parsed as a style
    tag and silently vanished, leaving a clipped line with nothing to say it had
    been clipped. Parentheses render as themselves.
    """
    if len(line) <= limit:
        return line
    return f"{line[:limit]}… (clipped at {limit} chars, {len(line)} total)"


# ── Command watchdog (Task 6) ─────────────────────────────────────────────────
# The ceilings a running command is judged against. One declaration; both runners
# (`cli/runtime.py`, `terminal.py`) read these and `core/commands.watch()` applies
# them, so a limit can never mean one thing on the CLI and another on the Web.
#
# ⚠️ BOTH CEILINGS DEFAULT TO OFF (0), AND THAT IS THE POINT.
# "Do not solve this merely by adding an arbitrary timeout" — a `docker build`, a
# `pip install torch` or an `nmap -p-` legitimately runs for 40 minutes in
# silence, so any default number would either kill real work or be too high to
# catch a hang. What is on by default is the OBSERVATION: after
# CMD_STUCK_AFTER seconds with no output the user is TOLD and asked
# ([R]etry / [K]ill / [W]ait). Asking is right where guessing is not — and a
# caller that genuinely knows the bound (a test runner, a CI step) can still pass
# `timeout=`/`idle_timeout=` per command.
#
# ⚠️ IDLE TIME, NOT ELAPSED TIME, IS WHAT DETECTS A HANG.
# Elapsed cannot tell a healthy long build from a deadlocked one; both are "still
# going after four minutes". Time since the last byte can, which is why
# `core/commands.heartbeat()` is called per output line and why a command that
# keeps printing is never reported stuck however long it runs.
CMD_TIMEOUT = float(os.environ.get("AGENT2_CMD_TIMEOUT", "0") or 0)
CMD_IDLE_TIMEOUT = float(os.environ.get("AGENT2_CMD_IDLE_TIMEOUT", "0") or 0)
# Silence after which a command is REPORTED (not killed) as apparently stuck.
CMD_STUCK_AFTER = float(os.environ.get("AGENT2_CMD_STUCK_SEC", "20") or 0)
# How often the live "Elapsed / Last output" line is refreshed while a command is
# quiet. Drawing it per drain tick (5×/s) would fight the spinner for the line.
CMD_HEARTBEAT_SEC = float(os.environ.get("AGENT2_CMD_HEARTBEAT_SEC", "1.0") or 1.0)
# Extra grace granted by [W]ait, and the ceiling on user-driven [R]etry attempts.
CMD_WAIT_GRACE = float(os.environ.get("AGENT2_CMD_WAIT_SEC", "60") or 60)
CMD_MAX_RETRIES = int(os.environ.get("AGENT2_CMD_MAX_RETRIES", "2") or 2)

# ── File Intelligence System ─────────────────────────────────────────────────────
# Limits and safety knobs for the fileintel subsystem (agent2/fileintel/). All
# overridable via environment variables for constrained/containerised runs.
MAX_FILE_SIZE = int(os.environ.get("AGENT2_MAX_FILE_SIZE", str(100 * 1024 * 1024)))  # 100 MB
# Where generated/converted files go. Empty = write beside the source file.
FILEINTEL_OUTPUT_DIR = os.environ.get("AGENT2_FILEINTEL_OUTPUT", "").strip()
# Executable / dangerous extensions the file pipeline refuses to *process*
# unless the caller passes allow_executable=true. (This does NOT restrict
# run_command — it only gates the file-intelligence operations.) Script SOURCE
# files (.sh/.bat/.ps1/.js/…) are intentionally NOT blocked: they are plain text
# the agent legitimately reads and searches. Only compiled binaries/installers
# are gated here.
BLOCKED_EXTENSIONS = {
    "exe", "dll", "so", "dylib", "bin", "com", "msi", "scr", "cpl",
    "jar", "app", "deb", "rpm", "apk", "dmg", "pkg", "sys",
}

# ── Network resilience ───────────────────────────────────────────────────────────
# The single biggest cause of Agent2 "crashing" mid-task was a socket "read
# operation timed out" during a long generation: the google-genai SDK's default
# read timeout is short, and the agent loops treated the timeout as a fatal error
# and ended the turn. We now (a) give every Gemini client a generous per-request
# timeout and (b) retry transient network failures with exponential backoff.
# Override any of these via environment variables for constrained environments.
HTTP_TIMEOUT      = int(os.environ.get("AGENT2_HTTP_TIMEOUT", "600"))   # seconds per request
MAX_RETRIES       = int(os.environ.get("AGENT2_MAX_RETRIES", "5"))      # transient-error retries per call
RETRY_BASE_DELAY  = float(os.environ.get("AGENT2_RETRY_DELAY", "2.0"))  # seconds; exponential backoff base
RETRY_MAX_DELAY   = float(os.environ.get("AGENT2_RETRY_MAX_DELAY", "30.0"))  # cap per backoff sleep

# ── Model routing + fallback (Tasks 18, 19) ────────────────────────────────────
# ⚠️ ROUTING IS OFF BY DEFAULT, AND THAT IS THE REQUIREMENT, NOT TIMIDITY.
# Task 18 says explicit user model selection takes priority "unless explicitly
# configured otherwise" — so automatic routing has to be the thing you opt into.
# Both surfaces always send a model key (the default one when the user never
# chose), which means a router that ran unasked would silently overrule a choice it
# cannot distinguish from a default. Three settings, from narrowest to widest:
#   off           — route only when the user selected the `auto` pseudo-model
#   default_only  — also route when the turn arrived on DEFAULT_MODEL, i.e. the
#                   user has never picked one
#   always        — route even over an explicit pick. This IS the "configured
#                   otherwise" case, and nothing but this value enables it.
#
# ⚠️ THE DEFAULT IS THE EMPTY STRING, NOT "off". `router.routing_mode()` resolves
# env → stored setting → off, and it can only consult the stored setting when the
# env says *nothing*. Defaulting this to "off" would be an explicit env choice that
# silently outranks the DB, so `/model routing always` would appear to work, persist
# correctly, and change nothing — a bug with no error message anywhere.
MODEL_ROUTING = (os.environ.get("AGENT2_MODEL_ROUTING", "") or "").strip().lower()

# Above this estimated prompt size a turn is treated as needing a long-context
# model. Deliberately well under any real window: the estimate is characters/4, and
# a router that waits until it is *sure* has already overflowed.
ROUTER_LONG_CONTEXT = int(os.environ.get("AGENT2_ROUTER_LONG_CONTEXT", "120000"))

# How many times ONE turn may switch model after a failure. Bounded, small, and
# counted per turn rather than per model: "do not endlessly retry failed
# providers" is only true if the ceiling cannot be reset by trying a third model.
FALLBACK_MAX_HOPS = max(0, int(os.environ.get("AGENT2_FALLBACK_MAX_HOPS", "2") or 0))

# Circuit breaker. After FAILS failures inside WINDOW seconds a model is skipped
# by the router and the fallback chain for COOLDOWN seconds. A success clears it
# immediately — the breaker exists to stop a hammering loop, not to punish a model
# for one bad minute.
BREAKER_FAILS    = max(1, int(os.environ.get("AGENT2_BREAKER_FAILS", "3") or 3))
BREAKER_WINDOW   = float(os.environ.get("AGENT2_BREAKER_WINDOW", "120") or 120)
BREAKER_COOLDOWN = float(os.environ.get("AGENT2_BREAKER_COOLDOWN", "60") or 60)

# Ledger cap. `model_attempts` is a diagnostic table, and the one thing a
# diagnostic table must never do is become the problem it was added to diagnose.
ROUTER_LEDGER_MAX = max(50, int(os.environ.get("AGENT2_ROUTER_LEDGER_MAX", "500") or 500))
