# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/core/permissions.py
──────────────────────────
THE capability model (Task 15). Authentication asked *who is this*; this module
answers *may they do this particular thing*.

Why it lives in `core/` and not in `server/`
───────────────────────────────────────────
Because the sensitive operations the task names do not all live behind a route.
"Terminal execution" is a socket event, "file writing" and "file deletion" are
agent **tools**, and "MCP configuration" is an HTTP endpoint. Three surfaces, one
question. A capability table inside `server/` would have to be duplicated to reach
the tool layer, and a duplicated authorization table is the drift that leaves one
copy permissive while the other looks locked — see the one-declaration rule.

So: capabilities, the policy, and the three lookup tables (route · socket event ·
tool) are declared here exactly once. `server/auth.py` asks about a request,
`server/sockets.py` asks about an event, `tools.dispatch_tool` and
`terminal.stream_command` ask about a tool. None of them decides anything.

⚠️ DEFAULT-OPEN FOR THE OWNER, AND THAT IS DELIBERATE
The default role is `owner` with every capability, so a normal install behaves
exactly as it did before this module existed. That is not laziness about security:
the *authentication* layer (Task 14) is what stops a stranger reaching any of
this, and a tool that annoys its only user gets switched off wholesale. What this
module adds is the ability to run a *less* trusted deployment — a demo box, a
shared machine, a container someone else can reach — without editing code:

    AGENT2_WEB_ROLE=viewer         # the browser may look, not act
    AGENT2_DENY_CAPS=exec,fs.write # nothing may shell out or write, either surface

⚠️ THE TWO KNOBS ARE SCOPED DIFFERENTLY, ON PURPOSE
`AGENT2_WEB_ROLE` describes *web clients*. `AGENT2_DENY_CAPS` describes *this
process* — it subtracts from every surface, the CLI included. A single knob could
not express "let me work locally but let the browser only watch", and a
web-only knob would be a lie the moment the agent wrote a file on a turn that a
browser had started. Naming carries the distinction: anything `AGENT2_WEB_*` stops
at the browser, anything `AGENT2_*` does not.

⚠️ AN UNKNOWN CAPABILITY NAME IN `AGENT2_DENY_CAPS` IS AN ERROR THAT FAILS LOUD-ISH
It is dropped and logged rather than silently accepted. A typo'd `AGENT2_DENY_CAPS=exe`
that quietly denied nothing would read, to the operator who set it, exactly like a
working restriction — the single worst outcome for a security control.

⚠️ A DENIAL IS ALWAYS AUDITED; AN ALLOW IS AUDITED ONLY WHEN IT IS SENSITIVE.
`audit()` writes through `core.logging`, so the trail lands in the same
`logs/agent2.log` as every other event. Logging *every* allow would bury the
interesting lines under one entry per `read_file`, which is how an audit log stops
being read.

Layer: config → permissions → auth · sockets · tools · terminal.
"""

from __future__ import annotations

import os
import threading

from agent2.core import logging as audit

# ── Capabilities ──────────────────────────────────────────────────────────────
# One name per class of sensitive operation the task lists. Kept coarse on
# purpose: a capability nobody can describe in a sentence is one nobody sets
# correctly.
CAP_READ = "read"              # look at state that carries no secret
CAP_CHAT = "chat"              # run an agent turn / edit history / stop one
CAP_EXEC = "exec"              # run a shell command (raw terminal or run_command)
CAP_FS_WRITE = "fs.write"      # create or modify a file in the workspace
CAP_FS_DELETE = "fs.delete"    # delete a file
CAP_MEMORY = "memory"          # memories + rules + PIL learning writes
CAP_SETTINGS = "settings"      # settings, PIL toggles, workspace switch
CAP_SECRETS = "secrets"        # API keys, provider credentials, token rotation
CAP_MCP = "mcp"                # connect / disconnect / auto-connect a bridge
CAP_MCP_CONFIG = "mcp.config"  # a bridge's endpoint and its security key
CAP_DESTRUCTIVE = "destructive"  # bulk deletes, wipes, prunes — no undo

ALL_CAPS: tuple[str, ...] = (
    CAP_READ, CAP_CHAT, CAP_EXEC, CAP_FS_WRITE, CAP_FS_DELETE, CAP_MEMORY,
    CAP_SETTINGS, CAP_SECRETS, CAP_MCP, CAP_MCP_CONFIG, CAP_DESTRUCTIVE,
)

#: Capabilities whose *use* is worth a log line even when it is allowed.
#: `read` and `chat` are excluded because they are the normal traffic.
SENSITIVE: frozenset = frozenset({
    CAP_EXEC, CAP_FS_WRITE, CAP_FS_DELETE, CAP_SECRETS, CAP_MCP_CONFIG,
    CAP_DESTRUCTIVE,
})

# ── Roles ─────────────────────────────────────────────────────────────────────
ROLE_OWNER = "owner"
ROLE_OPERATOR = "operator"
ROLE_VIEWER = "viewer"

#: role → the capabilities it holds.
#:
#: `operator` is the interesting one: it may drive the agent and let it write
#: code, but it may not reach the credentials or the MCP endpoints. That split
#: exists because "someone I trust with this repo" and "someone I trust with my
#: API keys" are different people, and before this table there was no way to say
#: so.
ROLES: dict[str, frozenset] = {
    ROLE_OWNER: frozenset(ALL_CAPS),
    ROLE_OPERATOR: frozenset({
        CAP_READ, CAP_CHAT, CAP_EXEC, CAP_FS_WRITE, CAP_FS_DELETE,
        CAP_MEMORY, CAP_SETTINGS, CAP_MCP,
    }),
    ROLE_VIEWER: frozenset({CAP_READ}),
}

DEFAULT_ROLE = ROLE_OWNER

_WARNED: set[str] = set()
_WARN_LOCK = threading.Lock()


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _warn_once(key: str, **fields) -> None:
    with _WARN_LOCK:
        if key in _WARNED:
            return
        _WARNED.add(key)
    audit.event("authz.config.bad", **fields)


def role_name(raw: str | None = None) -> str:
    """Resolve a role name. Unknown values fall back to `viewer`, not to owner.

    ⚠️ The fallback direction is the whole point. `mode()` in `auth.py` falls back
    to the *strict* end for the same reason: a typo in a security setting must
    never be the thing that grants access. An operator who meant `operator` and
    typed `operater` gets a visibly broken UI and fixes it in a minute; the
    opposite mistake is silent and permanent.
    """
    text = (raw if raw is not None else _env("AGENT2_WEB_ROLE")).strip().lower()
    if not text:
        return DEFAULT_ROLE
    if text in ROLES:
        return text
    if text in ("readonly", "read-only", "read_only", "ro"):
        return ROLE_VIEWER
    if text in ("admin", "root", "full"):
        return ROLE_OWNER
    _warn_once(f"role:{text}", setting="AGENT2_WEB_ROLE", value=text[:40],
               using=ROLE_VIEWER)
    return ROLE_VIEWER


def denied_caps() -> frozenset:
    """Capabilities subtracted from every role, from `AGENT2_DENY_CAPS`.

    Process-wide: it reaches the CLI too. See the module docstring for why that
    is the correct scope rather than an oversight.
    """
    raw = _env("AGENT2_DENY_CAPS")
    if not raw:
        return frozenset()
    out, bad = set(), []
    for word in raw.replace(";", ",").split(","):
        cap = word.strip().lower()
        if not cap:
            continue
        if cap in ALL_CAPS:
            out.add(cap)
        elif cap == "all":
            out.update(c for c in ALL_CAPS if c != CAP_READ)
        else:
            bad.append(cap)
    for cap in bad:
        _warn_once(f"deny:{cap}", setting="AGENT2_DENY_CAPS", value=cap[:40],
                   using="ignored")
    return frozenset(out)


def caps_for(role: str | None = None) -> frozenset:
    """The effective capability set for *role* after the process-wide subtraction."""
    return ROLES.get(role_name(role), ROLES[ROLE_VIEWER]) - denied_caps()


def process_caps() -> frozenset:
    """What THIS process may do at all, regardless of who asked.

    Used by the tool and command gates, which have no client to attribute the
    action to — a `write_file` inside an agent turn may have been started from the
    CLI, from a browser, or by task recovery on startup.
    """
    return frozenset(ALL_CAPS) - denied_caps()


def allowed(cap: str, *, role: str | None = None) -> bool:
    """The single question. Everything else in this module is a lookup."""
    if not cap:
        return True
    return cap in caps_for(role)


def process_allows(cap: str) -> bool:
    if not cap:
        return True
    return cap in process_caps()


# ── Lookup 1: HTTP routes ─────────────────────────────────────────────────────
# Ordered, first match wins, matched on the path PREFIX plus an optional suffix so
# `/api/mcp/<key>/config` can be distinguished from `/api/mcp/<key>/connect`
# without a regex per route.
#
# ⚠️ The default for an unmatched *unsafe* method under `/api/` is
# `CAP_DESTRUCTIVE`, the narrowest capability — so a route added later without a
# table entry is refused for a viewer instead of quietly inheriting `read`.
# Default-open here would mean every future endpoint ships unguarded, which is the
# exact failure mode a `@requires_auth` decorator has and this table exists to
# avoid.
_SAFE = frozenset({"GET", "HEAD", "OPTIONS"})

_ROUTE_RULES: tuple[tuple[str, str, str], ...] = (
    # (path prefix, path suffix or "", capability)   — unsafe methods only
    ("/api/auth/rotate", "", CAP_SECRETS),
    ("/api/auth/", "", CAP_READ),          # login/logout are auth's own business
    ("/api/keys", "", CAP_SECRETS),
    ("/api/providers", "", CAP_SECRETS),
    ("/api/mcp/", "/config", CAP_MCP_CONFIG),
    ("/api/mcp", "", CAP_MCP),
    ("/api/memories/prune", "", CAP_DESTRUCTIVE),
    ("/api/memories", "", CAP_MEMORY),
    ("/api/rules", "", CAP_MEMORY),
    ("/api/pil/settings", "", CAP_SETTINGS),
    ("/api/pil/wipe", "", CAP_DESTRUCTIVE),
    ("/api/pil/learn", "", CAP_MEMORY),
    ("/api/pil/optimize", "", CAP_SETTINGS),
    ("/api/pil/", "", CAP_CHAT),           # predict + feedback: normal typing
    ("/api/workspace", "", CAP_SETTINGS),
    # Model metadata and the routing policy are configuration: they change which
    # model answers, never what it may do. Editing them is `settings`, which is why
    # an `operator` may retune routing but still cannot reach the credentials the
    # models are called with.
    ("/api/models", "", CAP_SETTINGS),
    ("/api/sync", "", CAP_READ),
    ("/api/recovery", "", CAP_CHAT),
    ("/api/chats", "", CAP_CHAT),
    ("/api/skills", "", CAP_SETTINGS),
    ("/api/workflows", "", CAP_CHAT),
    ("/api/metrics", "", CAP_READ),
)

#: Collection-level DELETEs are bulk operations with no undo, so they need
#: `destructive` even though the same path's per-item DELETE needs only its own
#: capability. Keyed by the exact path.
_BULK_DELETE = frozenset({"/api/memories", "/api/rules", "/api/chats"})


def capability_for(method: str, path: str) -> str:
    """Which capability a request needs. `CAP_READ` for anything safe."""
    m = str(method or "GET").upper()
    p = str(path or "/")
    if m in _SAFE:
        return CAP_READ
    if m == "DELETE" and p.rstrip("/") in _BULK_DELETE:
        return CAP_DESTRUCTIVE
    for prefix, suffix, cap in _ROUTE_RULES:
        if p.startswith(prefix) and (not suffix or p.endswith(suffix)):
            return cap
    if p.startswith("/api/"):
        return CAP_DESTRUCTIVE
    return CAP_CHAT


# ── Lookup 2: Socket.IO events ────────────────────────────────────────────────
# ⚠️ `run_raw_command` is why this table exists. It executes an arbitrary shell
# command, it is not an HTTP route, and so no amount of route guarding covers it.
_SOCKET_CAPS: dict[str, str] = {
    "chat_message": CAP_CHAT,
    "edit_message": CAP_CHAT,
    "stop_agent": CAP_CHAT,
    "run_raw_command": CAP_EXEC,
    "terminal_input": CAP_EXEC,
    "terminal_kill": CAP_CHAT,      # ending something is never the risky direction
    "pil_predict": CAP_CHAT,
    "pil_feedback": CAP_CHAT,
}


def capability_for_event(event: str) -> str:
    return _SOCKET_CAPS.get(str(event or ""), CAP_CHAT)


# ── Lookup 3: agent tools ─────────────────────────────────────────────────────
# Only the tools that change something outside the process appear. Everything
# absent is a read and needs `read`.
_TOOL_CAPS: dict[str, str] = {
    "run_command": CAP_EXEC,
    "write_file": CAP_FS_WRITE,
    "multi_edit_files": CAP_FS_WRITE,
    "delete_file": CAP_FS_DELETE,
    "save_memory": CAP_MEMORY,
    "convert_file": CAP_FS_WRITE,
    "run_file_op": CAP_FS_WRITE,
}


def capability_for_tool(name: str) -> str:
    return _TOOL_CAPS.get(str(name or ""), CAP_READ)


# ── Audit ─────────────────────────────────────────────────────────────────────
_COUNTS: dict[str, int] = {"allowed": 0, "denied": 0}
_COUNT_LOCK = threading.Lock()


def _bump(key: str) -> None:
    with _COUNT_LOCK:
        _COUNTS[key] = _COUNTS.get(key, 0) + 1


def counters() -> dict:
    """Denial/allow tallies for `/api/health` and the metrics surface.

    Counters, never a log of *what* — the payload of a denied request is exactly
    the thing an unauthenticated reader must not learn.
    """
    with _COUNT_LOCK:
        return dict(_COUNTS)


def reset_counters() -> None:
    with _COUNT_LOCK:
        _COUNTS.clear()
        _COUNTS.update({"allowed": 0, "denied": 0})


def audit_use(cap: str, *, ok: bool, what: str = "", **fields) -> None:
    """Record a capability decision. Never raises — a logging fault is not a
    security decision, and swallowing here cannot admit anything, because the
    caller has already decided by the time it calls this."""
    try:
        _bump("allowed" if ok else "denied")
        if ok and cap not in SENSITIVE:
            return
        audit.event("authz.allow" if ok else "authz.deny",
                    cap=cap, what=str(what)[:120], **fields)
    except Exception:
        pass


def refusal(cap: str, *, what: str = "") -> str:
    """The message a refused caller sees.

    Says which capability and which switch, because "permission denied" with no
    noun is a support ticket. It does NOT say what the caller would have needed to
    do to get it — that is the operator's business, not the client's.
    """
    return (f"Not permitted: {what or 'this operation'} requires the "
            f"'{cap}' capability, which this client does not hold.")


def describe(role: str | None = None) -> dict:
    """A serialisable snapshot for `/api/health`, `/api/platform` and tests."""
    name = role_name(role)
    return {
        "role": name,
        "capabilities": sorted(caps_for(name)),
        "denied": sorted(denied_caps()),
        "process_capabilities": sorted(process_caps()),
        "counters": counters(),
    }
