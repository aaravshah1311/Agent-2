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

# ── Durable execution state (Task 24) ─────────────────────────────────────────
# `core/execstate.py` writes what a *crash* would otherwise take with it: which
# commands, tool calls and workflow runs were in flight. The in-memory registries
# (`core/commands.py`, `core/session/`) stay the authority while a process lives;
# these knobs govern only the durable shadow of them.
#
# ⚠️ ON BY DEFAULT, unlike the command ceilings above — and for the opposite
# reason. A ceiling that fires kills the user's work, so it must be opted into; a
# ledger row costs one small UPSERT and its absence is only ever discovered after
# the crash it was supposed to explain. `0` turns it off for someone running on a
# read-only or throwaway database.
EXEC_PERSIST = (os.environ.get("AGENT2_EXEC_PERSIST", "1") or "1").strip().lower() \
    not in ("0", "false", "no", "off")

# How often a *silent* running command is re-stamped. ⚠️ This is a throttle, not a
# poll: `commands.heartbeat()` runs once per output LINE, and a `make -j8` would
# otherwise turn a per-line dict write into thousands of UPSERTs. The lifecycle
# edges (create · start · settle) are ALWAYS written, whatever this is set to —
# throttling those would be throttling the facts recovery actually reads.
EXEC_BEAT_SEC = max(0.0, float(os.environ.get("AGENT2_EXEC_BEAT_SEC", "5.0") or 0))

# How long an ACTIVE row from ANOTHER process may go unstamped before `sweep()`
# reads it as interrupted. Must be comfortably above EXEC_BEAT_SEC: the gap is
# what separates "that process died mid-command" from "that process is alive and
# its command has simply been quiet". Too low and a live dual-mode sibling is
# libelled as crashed; too high and a real crash stays invisible for that long.
EXEC_STALE_SEC = max(5.0, float(os.environ.get("AGENT2_EXEC_STALE_SEC", "90") or 90))

# Ledger cap per exec_* table, trimmed by the writer — same discipline as
# ROUTER_LEDGER_MAX, and for the same reason. ⚠️ An ACTIVE row is never trimmed,
# whatever the count: that is the one row a recovery needs.
EXEC_LEDGER_MAX = max(50, int(os.environ.get("AGENT2_EXEC_LEDGER_MAX", "500") or 500))

# ── Crash recovery (Task 25) ──────────────────────────────────────────────────
# `core/recovery/crash.py` reads what Task 24 wrote and decides what to do about
# it. These knobs bound that decision; `core/recovery/safety.py` decides what may
# be decided at all, and no knob here can widen it.

# The master switch. OFF means the ledger is still written (that is EXEC_PERSIST's
# business) and simply never acted upon — `crash.scan()` returns an empty report,
# `/api/health` reports `enabled: false`, and nothing is classified or retried.
RECOVERY_ENABLED = (os.environ.get("AGENT2_RECOVERY", "1") or "1").strip().lower() \
    not in ("0", "false", "no", "off")

# Whether a launch scans on its own initiative. Separate from RECOVERY_ENABLED
# because they answer different questions: this one is "may a *startup* spend time
# on this", and a scripted or containerised run may want the machinery available
# to `/api/recovery` while refusing to delay its own boot.
RECOVERY_ON_START = (os.environ.get("AGENT2_RECOVERY_ON_START", "1") or "1") \
    .strip().lower() not in ("0", "false", "no", "off")

# Bounded attempts per interrupted unit. ⚠️ THE CEILING IS THE POINT, not a
# tuning parameter: the failure mode Task 25 names is a recovery that fails, is
# retried, fails again and loops forever — and a loop that re-runs a half-finished
# write is worse than the interruption it is trying to repair. Past this the unit
# is parked at NEEDS_REVIEW for a human, which is a terminal state.
RECOVERY_MAX_ATTEMPTS = max(1, int(os.environ.get("AGENT2_RECOVERY_MAX_ATTEMPTS", "3") or 3))

# How many interrupted units one scan may look at. A machine that crashed during a
# large fan-out can leave thousands of rows; a scan that walked all of them would
# be the thing that makes the next launch look broken too.
RECOVERY_SCAN_LIMIT = max(1, int(os.environ.get("AGENT2_RECOVERY_SCAN_LIMIT", "200") or 200))

# Wall-clock ceiling on the startup scan. ⚠️ "Recovery must not block startup
# indefinitely" is a requirement, so the scan checks this between units and stops
# cleanly with `truncated: True` rather than finishing at any cost. What it did not
# reach is still in the ledger and still interrupted — the next scan continues.
RECOVERY_SCAN_BUDGET_SEC = max(0.5, float(
    os.environ.get("AGENT2_RECOVERY_SCAN_BUDGET_SEC", "5.0") or 5.0))

# Ceilings on one unit's recovery, and on one verification. Verification reads the
# filesystem and shells out to `git`, so it is the half that can hang.
RECOVERY_TIMEOUT = max(1.0, float(os.environ.get("AGENT2_RECOVERY_TIMEOUT", "60") or 60))
RECOVERY_VERIFY_TIMEOUT = max(0.5, float(
    os.environ.get("AGENT2_RECOVERY_VERIFY_TIMEOUT", "10") or 10))

# How many units may be recovered concurrently. ⚠️ Deliberately small. Recovery
# runs at startup, alongside everything else a launch does, and "never spawn
# unbounded workers" is a Task 26 requirement — the pool that repairs the damage
# must not be able to become the next outage.
RECOVERY_MAX_PARALLEL = max(1, int(os.environ.get("AGENT2_RECOVERY_MAX_PARALLEL", "2") or 2))

# ── Agent metrics (Task 27) ───────────────────────────────────────────────────
# `core/metrics.py` measures thirteen declared signals in memory. These knobs
# bound what that costs; the module's docstring says what each signal means and
# which three are *borrowed* from their durable owner rather than re-measured.

# The master switch. ⚠️ OFF must be indistinguishable from "this module was never
# written" — every entry point becomes a single boolean test, no series are
# allocated and nothing is timed. Task 27 asks for bounded overhead, and the only
# bound nobody can argue with is zero, so it stays reachable.
METRICS_ENABLED = (os.environ.get("AGENT2_METRICS", "1") or "1").strip().lower() \
    not in ("0", "false", "no", "off")

# Samples retained per series, for percentiles. A ring buffer: memory is
# O(series × this) and the hot path only appends — the sort happens at read time,
# in the report. ⚠️ Once a series' count exceeds this, p50/p95 describe the most
# recent window and `samples` in the payload says so; a percentile over "all of
# history" would need unbounded memory to answer a question about the last hour.
METRICS_SAMPLES = max(8, int(os.environ.get("AGENT2_METRICS_SAMPLES", "128") or 128))

# Distinct labels one signal may keep before new ones fold into `~other`.
# ⚠️ The cap is the backstop for a caller that labels with something unbounded.
# Labels are enum-ish by contract — a tool name, a model key, a server key — but
# the day one is a path or a command line, this is what stops the registry from
# growing with the workload, and `folded` in the report says it happened.
METRICS_MAX_SERIES = max(4, int(os.environ.get("AGENT2_METRICS_MAX_SERIES", "64") or 64))

# ── Project scan / `/init` (Task 29) ──────────────────────────────────────────
# `core/projectscan.py` walks the workspace once to answer "what IS this project"
# — languages, package managers, frameworks, entry points, tests, commands,
# conventions. These bound that walk.
#
# ⚠️ EVERY CEILING HERE IS REPORTED WHEN IT ENGAGES (`truncated` + `truncated_by`
# in the scan result, and a line on both surfaces). That is the difference between
# these and `tools.scan_project`'s silent 300 000-character cut: Task 31 writes the
# scan into `.agent2/agent2.md`, and every later turn reads that file as fact — so
# a walk that quietly stopped early does not produce a slightly smaller answer, it
# produces a confident description of a project that is not there.

# Files visited before the walk stops. A monorepo, a `node_modules` no rule
# matched, or a network mount is what this exists for. Floor 200: below that the
# census is noise, and a cap so low the answer is meaningless is worse than a slow
# scan, because it still looks like an answer.
INIT_MAX_FILES = max(200, int(os.environ.get("AGENT2_INIT_MAX_FILES", "20000") or 20000))

# Directory levels descended from the workspace root. Deep vendored trees are the
# reason; 12 clears any layout a human arranged on purpose.
INIT_MAX_DEPTH = max(2, int(os.environ.get("AGENT2_INIT_MAX_DEPTH", "12") or 12))

# Wall-clock ceiling for the walk. ⚠️ Same discipline as
# `RECOVERY_SCAN_BUDGET_SEC`: when it expires the scan stops *cleanly* and says
# `truncated_by: "budget"`, rather than finishing at any cost — `/init` is
# synchronous on a human's terminal, and a cold network mount must not hold it.
INIT_SCAN_BUDGET_SEC = max(0.5, float(os.environ.get("AGENT2_INIT_BUDGET_SEC", "10.0") or 10.0))

# Bytes read from any one manifest (`package.json`, `pyproject.toml`, `Makefile`).
# The scan opens only the declared manifest list, and only this far into each: a
# generated 40 MB lock-adjacent manifest is not a reason to hold a terminal.
INIT_MANIFEST_BYTES = max(1024, int(os.environ.get("AGENT2_INIT_MANIFEST_BYTES",
                                                   "262144") or 262144))

# How many of the project's own source files `/init` opens to read what their
# authors wrote *about* them — the module docstring or the leading comment block
# ("comments in a file give an idea about the project"). Those notes are how the
# scan stops guessing from file names.
#
# ⚠️ THIS IS THE ONE `/init` CEILING WHOSE `0` IS A FEATURE, AND IT HAS NO FLOOR.
# Everything else the scan collects is a count, a name or a path; a note is *prose
# a human wrote*, and it is the only part of the scan that reaches the narration
# call and therefore leaves the machine. So "do not read my comments" has to be
# expressible as a number rather than as trust in a description — `0` skips the
# step entirely and `file_notes` stays empty, exactly as `AGENT2_SKILLS=0` and
# `AGENT2_METRICS=0` make their subsystems indistinguishable from never written.
INIT_NOTE_FILES = max(0, int(os.environ.get("AGENT2_INIT_NOTE_FILES", "12") or 12))

# Characters kept from ONE file's note. Floor 120: below that a docstring is cut
# mid-sentence, and half a sentence about a module is worse than none, because it
# still reads like a conclusion. When the cap engages the item says so
# (`truncated`), the same discipline the walk's three ceilings follow.
INIT_NOTE_CHARS = max(120, int(os.environ.get("AGENT2_INIT_NOTE_CHARS", "700") or 700))

# ── Skills (Phase 11, Tasks 32–36) ────────────────────────────────────────────
# `core/skills/` reads `.agent2/skills/` — files a human wrote, possibly for
# another agent — and puts a *selected* few of them into the prompt. These bound
# the read and the selection.
#
# ⚠️ SAME DISCIPLINE AS THE `INIT_*` BLOCK ABOVE: every ceiling here is REPORTED
# when it engages (`truncated` + `truncated_by` on the catalog, `omitted` on the
# selection, a line on both surfaces). A skill silently missing from a prompt is
# indistinguishable from a skill the user never wrote, and the user's next move is
# to rewrite the file that was working.

# The master switch. Off ⇒ discovery returns an empty catalog and the broker's
# skills source collects nothing — the same "indistinguishable from never written"
# bar `METRICS_ENABLED` sets.
SKILLS_ENABLED = (os.environ.get("AGENT2_SKILLS", "1") or "1").strip().lower() \
    not in ("0", "false", "no", "off")

# Skills discovered before the walk stops. Floor 4 rather than 1: a cap that can
# hide most of a small collection is worse than a slow scan, because the answer
# still looks complete.
SKILLS_MAX = max(4, int(os.environ.get("AGENT2_SKILLS_MAX", "64") or 64))

# Directory levels descended below `.agent2/skills/`. Deeper than any layout a
# human arranges by hand (`web/xss/SKILL.md` is depth 2), shallow enough that a
# symlinked or vendored tree cannot turn discovery into a project walk.
SKILLS_MAX_DEPTH = max(1, int(os.environ.get("AGENT2_SKILLS_MAX_DEPTH", "6") or 6))

# Bytes read from any one skill file. ⚠️ A skill is prompt text, so this is also a
# *cost* ceiling, not only a safety one — and when it engages the skill is still
# used, with `body_truncated` set, because half a skill a human wrote is worth more
# than a refusal they cannot see.
SKILLS_MAX_BYTES = max(1024, int(os.environ.get("AGENT2_SKILLS_MAX_BYTES",
                                                "65536") or 65536))

# Wall-clock ceiling on one discovery pass. Small: this runs on the turn path,
# behind a 5-second cache, over a folder that holds a handful of markdown files.
SKILLS_SCAN_BUDGET_SEC = max(0.25, float(os.environ.get("AGENT2_SKILLS_BUDGET_SEC",
                                                        "2.0") or 2.0))

# ⚠️ HOW MANY SKILLS MAY REACH ONE PROMPT — the number Task 34's acceptance bar is
# about ("not every skill in every prompt"). A collection of forty skills is a
# feature; forty skills in one prompt is the context stuffing the Context Broker
# exists to end. Floor 1, because zero would make the whole subsystem a no-op
# through a knob that reads like a tuning parameter.
SKILLS_IN_PROMPT = max(1, int(os.environ.get("AGENT2_SKILLS_IN_PROMPT", "4") or 4))

# Characters of skill text one prompt may carry, across all selected skills. The
# budget in `core/broker/budget.py` is the ceiling for the whole prompt; this is
# the share skills may ask for before it gets there, so one enormous skill cannot
# arrive at the budget already having displaced the conversation.
SKILLS_MAX_CHARS = max(500, int(os.environ.get("AGENT2_SKILLS_MAX_CHARS",
                                               "6000") or 6000))

# ── The DAG core (Phase D1) ───────────────────────────────────────────────────
# ONE generalized graph engine, shared by Workflow, Dynamic Workflow and
# UltraCode. These two ceilings bound a *graph* — whatever declared it — and they
# are deliberately NOT the `WORKFLOW_*` pair below. A workflow file is something a
# human types, and 64 nodes is already more than anybody writes by hand; a plan a
# planner generates and then grows while it runs is bounded by what the machine
# can afford to schedule and recover, which is a different number for a different
# reason. Sharing one knob would mean raising the ceiling for a generated plan
# also raised it for hand-written files nobody re-read.

# Nodes one graph may hold, including nodes added at runtime. ⚠️ Floor 2 for
# `WORKFLOW_MAX_NODES`' reason: a one-node graph has no dependency to get wrong,
# so a ceiling of 1 would make the engine untestable through a knob that reads
# like tuning. Refused, never truncated — a graph missing part of itself is a
# graph whose dependencies no longer close, and that deadlocks in silence.
DAG_MAX_NODES = max(2, int(os.environ.get("AGENT2_DAG_MAX_NODES", "512") or 512))

# How many **nodes one graph may gain after it was declared**. Dynamic Workflow and
# UltraCode add nodes in response to what execution found (a failed test earns a
# diagnosis node), and the failure mode of that loop is not a crash: it is a plan
# that keeps growing a node at a time and never finishes.
# ⚠️ **NODES ADDED, NOT CALLS MADE**, and the difference is the whole bound:
# `store.load()` derives it as `total - declared` from the rows themselves, so it
# needs no counter to keep — and one `extend()` adding fifty nodes is spending the
# budget fifty times over, which is exactly the runaway this ceiling is for. A
# per-call count would be a second piece of state, and it would let the same loop
# escape by batching.
# ⚠️ It is NOT a lesser `DAG_MAX_NODES`: that one bounds how big a plan may get, this
# one bounds how far it may drift from the plan somebody approved. A 3-node graph
# grown to 500 is within the node ceiling and has re-planned itself 497 nodes past
# what was shown.
# ⚠️ Floor 0, and 0 is a supported answer rather than a broken one —
# `AGENT2_INIT_NOTE_FILES`' rule: it means "this install runs the plan it was given
# and never invents more work", which is a decision an operator is entitled to
# express as a number.
DAG_MAX_MUTATIONS = max(0, int(os.environ.get("AGENT2_DAG_MAX_MUTATIONS", "64") or 64))

# ── The DAG scheduler (Phase D2) ──────────────────────────────────────────────
# ONE scheduler for every consumer, and *"never blindly run every READY node"* is
# the whole of its job — so every ceiling it honours is declared here, live, and
# none of them is unlimited. `core/dag/schedule.py` reads them through `limits()`,
# which a consumer may override per run; these are the defaults an install runs
# under.
#
# ⚠️ **THE FIVE SPEC CEILINGS ARE TWO SHAPES, NOT FIVE FIELDS.** `max_workers` and
# `max_concurrent_tasks` bound a *run*; `max_concurrent_commands` and
# `max_concurrent_mcp_calls` bound one **kind** of node at a time; `max_model_calls`
# bounds what a run may **spend** on one kind over its whole life (the spec is
# careful not to call that one "concurrent"). Two label tables express all three, so
# a consumer that invents a sixth node type bounds it by adding a row rather than by
# teaching the engine a new field — and the engine still never reads a `kind`, it
# looks one up. A field per node type would be the `if workflow:` of concurrency.

# Threads the pump may run nodes on. ⚠️ Floor 0, and `0` is a supported answer
# rather than a broken one: it is `MAX_CONCURRENT_TURNS`' posture — no pool at all,
# nodes run inline on the calling thread, one at a time — so an install that wants
# the graph engine strictly sequential says so with a number, and the app must
# behave identically with the threads gone.
DAG_MAX_WORKERS = max(0, int(os.environ.get("AGENT2_DAG_MAX_WORKERS", "4") or 4))

# `max_concurrent_tasks` — nodes in flight at once, whatever kind they are. ⚠️ It is
# deliberately a *second* number and not `DAG_MAX_WORKERS`: in-flight is derived from
# the rows, so it counts another process's claims too (dual mode is two processes over
# one `agent2.db`), while workers only ever counts this pump's threads. Floor 1 — a
# ceiling of 0 is a graph that can never start, which is a hang, not a setting.
DAG_MAX_RUNNING = max(1, int(os.environ.get("AGENT2_DAG_MAX_RUNNING", "8") or 8))

# `max_concurrent_commands` — how many shell nodes may be in flight together. Small
# on purpose: a command is a process tree with pipes and a watchdog, and eight of
# them racing for one terminal is how output becomes unreadable and a machine
# becomes unusable.
DAG_MAX_COMMANDS = max(1, int(os.environ.get("AGENT2_DAG_MAX_COMMANDS", "2") or 2))

# `max_concurrent_mcp_calls` — Burp and ZAP are single instances of somebody else's
# program, reached over one HTTP session each. Parallelism past a couple of calls
# does not make a scan faster, it makes the bridge the failure.
DAG_MAX_MCP_CALLS = max(1, int(os.environ.get("AGENT2_DAG_MAX_MCP_CALLS", "2") or 2))

# `max_model_calls` — the **lifetime** spend of one run on model-backed nodes, not a
# concurrency limit. This is the ceiling that stops a self-extending plan from
# spending an API budget while nobody is watching; `store.load()` derives the spend
# from `attempt_count` on the rows, so a retry is charged and a crash-resumed run
# does not get a fresh allowance. Floor 1, not 0: a spend budget of 0 would mean a
# graph that can never run its own nodes, which reads as a deadlock rather than as a
# decision.
DAG_MAX_MODEL_CALLS = max(1, int(os.environ.get("AGENT2_DAG_MAX_MODEL_CALLS",
                                                "200") or 200))

# The two tables the scheduler actually reads. A `Node.kind` this build has never
# heard of is bounded by `DAG_MAX_RUNNING` alone, which is the honest answer: an
# unknown kind gets the general ceiling, never no ceiling.
DAG_KIND_LIMITS: dict[str, int] = {
    "command": DAG_MAX_COMMANDS,
    "mcp": DAG_MAX_MCP_CALLS,
}
DAG_KIND_BUDGETS: dict[str, int] = {
    "llm": DAG_MAX_MODEL_CALLS,
}

# How many times one node may be **started**, retries included. ⚠️ Bounded AND
# classified: `core/recovery/classify.py` decides whether repeating this particular
# operation is safe at all, and this number decides how many times something already
# judged safe may be tried. Either one alone is the "blind retry" the spec forbids —
# a count with no classification re-runs a `git push`, a classification with no count
# re-runs a safe step forever. Floor 1 = one attempt and no retry.
DAG_MAX_ATTEMPTS = max(1, int(os.environ.get("AGENT2_DAG_MAX_ATTEMPTS", "2") or 2))

# Wall-clock ceiling on ONE node, seconds. ⚠️ `0` is off and that is the default —
# `CMD_TIMEOUT`'s posture, for its reason: a node may legitimately be a 40-minute
# build, and a default deadline would kill correct work on a slow machine. When it is
# set the verdict is **recorded before the worker is signalled**, because a Python
# thread cannot be killed and the last writer would otherwise win.
DAG_NODE_TIMEOUT = max(0.0, float(os.environ.get("AGENT2_DAG_NODE_TIMEOUT", "0") or 0))

# ── Workflow (Tasks 37-39) ────────────────────────────────────────────────────
# A workflow is a *task graph*, not a second execution engine: every node is an
# `agent_tasks` row and the run is one `exec_workflows` row, so the checkpoints,
# the heartbeats and the crash recovery that already exist are the ones a workflow
# gets. These ceilings therefore bound a **declaration**, not a runtime.

# The master switch. Off ⇒ no discovery, no run, and the broker's `workflow_state`
# source collects nothing — the same "indistinguishable from never written" bar
# `SKILLS_ENABLED` and `METRICS_ENABLED` set.
WORKFLOW_ENABLED = (os.environ.get("AGENT2_WORKFLOWS", "1") or "1").strip().lower() \
    not in ("0", "false", "no", "off")

# Nodes one workflow may declare. ⚠️ Floor 2, because a one-node graph has no
# dependencies to get wrong and a ceiling of 1 would make the feature untestable
# through a knob that reads like tuning. Each node costs an `agent_tasks` row and
# a place in the topological order, so this is a cost ceiling as much as a guard —
# and when it engages the definition is REFUSED rather than silently truncated: a
# graph missing its last node is a graph whose dependencies no longer close.
WORKFLOW_MAX_NODES = max(2, int(os.environ.get("AGENT2_WORKFLOW_MAX_NODES",
                                               "64") or 64))

# Characters the `workflow_state` context block may spend. Small on purpose: what
# a turn needs to know is *which* workflow is running and which node is live, and
# the node's own instruction is already the turn's goal. The user's standing bar —
# a worker gets only what it needs, never the whole parent conversation — starts
# here, at the one source that could otherwise inline an entire plan every turn.
WORKFLOW_STATE_CHARS = max(200, int(os.environ.get("AGENT2_WORKFLOW_STATE_CHARS",
                                                   "1200") or 1200))

# ── Workflow FILES (Task 38) ──────────────────────────────────────────────────
# `.agent2/workflows/*.yaml` — a human-readable declaration, read by
# `core.workflow.loader`. These bound the *read*, exactly as the `SKILLS_*` block
# above bounds the skills walk, and for its reason: the folder belongs to the user,
# so its size is not something this build gets to assume.
#
# ⚠️ EVERY CEILING HERE IS REPORTED WHEN IT ENGAGES (`truncated` + `truncated_by`
# on the catalog, `truncated` on the file). A workflow silently missing from
# `/workflow` is indistinguishable from one the user never wrote, and their next
# move is to rewrite the file that was already working.

# Workflow files read in one pass. Floor 4, not 1 — `SKILLS_MAX`'s reason: a cap
# that can hide most of a small collection is worse than a slow scan, because the
# answer still looks complete.
WORKFLOW_MAX_FILES = max(4, int(os.environ.get("AGENT2_WORKFLOW_MAX_FILES",
                                               "64") or 64))

# Bytes read from any ONE workflow file. A declaration of at most
# `WORKFLOW_MAX_NODES` nodes, each with an instruction capped at `graph.MAX_TEXT`,
# fits inside this an order of magnitude over. ⚠️ Unlike a skill — which is still
# usable half-read — a truncated *declaration* is refused by `validate()`, because
# the tail of a YAML file is where the last node's `needs:` lives.
WORKFLOW_MAX_BYTES = max(1024, int(os.environ.get("AGENT2_WORKFLOW_MAX_BYTES",
                                                  "131072") or 131072))

# Wall-clock ceiling on one discovery pass. Small for `SKILLS_SCAN_BUDGET_SEC`'s
# reason: it runs behind a TTL cache over a folder holding a handful of files.
WORKFLOW_SCAN_BUDGET_SEC = max(0.25, float(os.environ.get(
    "AGENT2_WORKFLOW_BUDGET_SEC", "2.0") or 2.0))

# ── Verification (Phase D3) ───────────────────────────────────────────────────
# `core.verify` — *"'Done' is not verification"*, and *"never trust: worker says
# Done → Agent2 says Done"*. One number, and it is a **read** ceiling, not a
# feature switch.
#
# ⚠️ THERE IS DELIBERATELY NO `AGENT2_VERIFY=0`. Every other subsystem in this
# file has an off switch because the app must be able to behave as though the
# feature was never written — but verification off does not make Agent2 quieter,
# it makes it *credulous*: every claim would read as confirmed, which is the one
# state the spec forbids. An operator who wants fewer verdicts has `/health` and
# `/metrics`; nobody gets a knob that turns a wrong answer into a green one.

# Durable rows one verification read may consult, per table. The verifier issues
# exactly two queries whatever the node count, so this is what bounds them.
# ⚠️ Floor 50, and when it engages the report says `truncated` — a verification
# that quietly stopped reading is exactly the false "confirmed" this module
# exists to prevent, so the ceiling has to be visible in the answer.
VERIFY_MAX_ROWS = max(50, int(os.environ.get("AGENT2_VERIFY_MAX_ROWS",
                                             "500") or 500))

# ── Dynamic workflow (Phase D4) ───────────────────────────────────────────────
# `core.workflow.dynamic` — the planner. A goal in prose becomes a graph, and the
# graph is then run by the machinery that already exists: *the planner decides
# **what**, the DAG decides **structure***. So these three numbers bound a
# **model's answer** and the loop that asks for it — nothing here bounds execution,
# which is `DAG_*`'s job and stays there.
#
# ⚠️ ENTERED EXPLICITLY, NEVER AUTOMATICALLY. There is no ceiling here that makes
# planning happen; `/workflow auto` and `POST /api/workflows/auto` are the only two
# doors, so an ordinary prompt cannot wander into a planner however these are set.

# The master switch. Off ⇒ `plan()` refuses by returning, `/workflow auto` says so
# and no run is ever instantiated from a goal — the same "indistinguishable from
# never written" bar `WORKFLOW_ENABLED`, `SKILLS_ENABLED` and `METRICS_ENABLED` set.
# ⚠️ It does NOT disable `core/workflow/` itself: a file a human wrote still runs,
# because turning the *planner* off is a statement about who may author a graph.
DYNAMIC_ENABLED = (os.environ.get("AGENT2_DYNAMIC_WORKFLOW", "1") or "1").strip().lower() \
    not in ("0", "false", "no", "off")

# Steps one plan may contain. ⚠️ **CLIPPED AND REPORTED, NOT REFUSED — and that is
# the opposite of `WORKFLOW_MAX_NODES` on purpose.** A workflow *file* is a
# declaration a human wrote, so dropping its tail would break dependencies the
# author closed by hand and the honest answer is to refuse the whole document. A
# plan is a model's answer to a prose goal, arriving in the order it means to be
# done in, and `dynamic._acyclic()` keeps only edges that point *backwards* in that
# order — so declaration order IS a topological order by construction, every
# dependency of step N precedes it, and a suffix can be dropped without leaving a
# single dangling `needs`. Refusing instead would throw away a usable plan because
# the model was wordy. Floor 1: one step is a real plan (do this one thing), and it
# is what a goal degrades to when no model is reachable at all.
DYNAMIC_MAX_STEPS = max(1, int(os.environ.get("AGENT2_DYNAMIC_MAX_STEPS",
                                              "24") or 24))

# Times ONE run may be re-planned — how often a live graph may be handed back to a
# model that then adds nodes to it. ⚠️ A SECOND CEILING FROM `DAG_MAX_MUTATIONS`,
# and neither implies the other: that one counts *nodes* a graph may gain, this one
# counts *decisions to add some*. A loop that adds a single node twenty times is
# far under a 64-node growth ceiling and is still exactly the runaway the ceiling
# exists to stop, so the tally is kept on the run (`GraphState.extensions`, durable
# because dual mode is two processes over one DB) and bounded here. ⚠️ It is NOT
# `schedule.Outcome.rounds`, which counts one pump's loop iterations — two facts, and
# this repo lets no word carry both, which is why the engine's field is `extensions`
# and only this planner-facing knob says "rounds".
# ⚠️ Floor 0, and `0` is a supported answer, not a broken one —
# `DAG_MAX_MUTATIONS`' posture: *plan once and never invent more work* is a decision
# an operator is entitled to express as a number.
DYNAMIC_MAX_ROUNDS = max(0, int(os.environ.get("AGENT2_DYNAMIC_MAX_ROUNDS",
                                               "3") or 3))


# ── UltraCode (Phase D5) ──────────────────────────────────────────────────────
# `core.ultracode` — the adaptive autonomous engineering loop: UNDERSTAND →
# INSPECT → DISCOVER SKILLS → PLAN → BUILD DAG → EXECUTE → OBSERVE → ANALYZE →
# VERIFY → (pass ⇒ continue · fail ⇒ diagnose → RE-PLAN → UPDATE DAG → EXECUTE).
# ⚠️ `ultracode.stages.STAGES` is the one declaration of that order; this is prose
# about it, and DISCOVER SKILLS precedes PLAN because the skills a project has must
# be in hand before a model is asked for steps.
#
# ⚠️ EVERY NUMBER HERE BOUNDS THE **LOOP**, NEVER THE GRAPH. How wide a run may
# execute, how many nodes it may hold and how often it may retry one are `DAG_*`'s
# facts and stay there; how many *observe → analyze → re-plan* passes the driver may
# take is nobody else's, and it is the one thing that could otherwise spin forever
# on a graph that never stops being extendable. `DYNAMIC_MAX_STEPS` /
# `DYNAMIC_MAX_ROUNDS` still bound the planner UltraCode asks — it owns no planner
# of its own, so nothing here duplicates either of them.
#
# ⚠️ ENTERED EXPLICITLY, NEVER AUTOMATICALLY — the same posture as the block above,
# and for a sharper reason: this loop *writes code*. `/ultracode` and
# `POST /api/ultracode` are the only two doors, asserted structurally, so an ordinary
# prompt cannot become autonomous execution however these are set.

# The master switch. Off ⇒ every entry point refuses by returning, no run is ever
# instantiated and nothing is written — the "indistinguishable from never written"
# bar `DYNAMIC_ENABLED`, `WORKFLOW_ENABLED`, `SKILLS_ENABLED` and `METRICS_ENABLED`
# set. ⚠️ It does NOT disable `core/workflow/` or its planner: `/workflow run` and
# `/workflow auto` still work, because turning the autonomous *driver* off is a
# statement about who may drive a graph unattended, not about whether graphs run.
ULTRACODE_ENABLED = (os.environ.get("AGENT2_ULTRACODE", "1") or "1").strip().lower() \
    not in ("0", "false", "no", "off")

# Adaptive cycles ONE run may take — how many times the driver may come back round
# from a failure to a fresh execution pass.
# ⚠️ **"CYCLES", AND DELIBERATELY NOT "ROUNDS".** Two facts already own that word —
# `GraphState.extensions` (decisions to add nodes, bounded by `DYNAMIC_MAX_ROUNDS`)
# and `schedule.Outcome.rounds` (one pump's loop iterations) — and this repo lets no
# word carry two meanings, so the third fact gets the third name. It is also a
# genuinely different bound: a cycle that fixes something without asking a planner
# for new nodes spends a cycle and no round at all, and twenty of those are exactly
# the runaway a mutation ceiling cannot see.
# Floor 1, because a single cycle is a real run: understand → plan → execute →
# verify, with no re-planning, is what a goal degrades to when no model is reachable.
ULTRACODE_MAX_CYCLES = max(1, int(os.environ.get("AGENT2_ULTRACODE_MAX_CYCLES",
                                                 "6") or 6))

# Wall-clock ceiling on ONE driven run, seconds. ⚠️ **OFF BY DEFAULT** —
# `CMD_TIMEOUT`'s posture for its reason, one layer up: a legitimate build node may
# take forty minutes and a legitimate *run* may take hours, so a default deadline
# would abandon correct work and call it a timeout. When set it is checked between
# nodes and reported (`U_BUDGET`), never enforced by killing a worker mid-write —
# the pump already owns per-node timeouts (`DAG_NODE_TIMEOUT`), and two clocks over
# one node is two answers.
ULTRACODE_BUDGET_SEC = max(0.0, float(os.environ.get("AGENT2_ULTRACODE_BUDGET_SEC",
                                                     "0") or 0))

# Whether a run must be released by a human before any node may start.
# ⚠️ **ON BY DEFAULT, AND THE GATE IS A DAG NODE, NOT A PROMPT** — the spec's own
# rule (*"human approval should be represented as an appropriate DAG node"*). Off
# means the graph is built without that node, which is a decision an operator makes
# once for an unattended install; it does **not** widen what a node may then do,
# because every action still goes through `core.permissions` at the moment it runs.
# ⚠️ There is deliberately no "approve everything as it comes" middle setting: a gate
# that a machine can satisfy is not approval, it is a delay.
ULTRACODE_APPROVAL = (os.environ.get("AGENT2_ULTRACODE_APPROVAL", "1") or "1") \
    .strip().lower() not in ("0", "false", "no", "off")

