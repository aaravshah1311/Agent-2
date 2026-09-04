# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/integrations/registry.py
───────────────────────────────
The list of MCP servers Agent2 ships with, and the four questions the rest of the
codebase asks about them (Task 8).

⚠️ THIS IS A TABLE, NOT A PLUGIN ARCHITECTURE — AND THAT IS THE REQUIREMENT
`_SERVERS` is a hardcoded two-entry tuple. There is no discovery, no entry-point
scan, no `load_plugin()`, and adding a third server means editing this line and
writing a subclass. That is deliberate and non-negotiable: a plugin architecture
was explicitly ruled out for this project, and skills — not plugins — are the
extension mechanism. Anything here that starts iterating a directory has drifted.

⚠️ EVERY FUNCTION IS TOTAL
`agent2/agent.py` imports this at module scope with no guard, and it is consulted
inside the agent loop for every tool call. So a missing `mcp` package, a broken
subclass, or a bridge whose singleton failed to construct must degrade to "that
server isn't there" — never raise into a turn. `_SERVERS` is built with a guarded
import per server, so ZAP failing to import cannot cost the user Burp.

⚠️ BURP IS FIRST, AND `extra_bridges()` EXISTS BECAUSE OF IT
Burp's dispatch branch in both agent loops predates this module and is pinned by
`test_agent_loop.py`, which patches `agent2.agent.burp` directly. Rather than
rewrite that branch to go through the registry — which would have moved a tested
path for no behavioural gain — the generic branch handles everything EXCEPT Burp,
and sits after it. `resolve()` still answers for all servers, because the code
that asks "who owns this tool name?" has no reason to care about that history.
"""

from __future__ import annotations

from agent2.integrations import state as mcp_state
from agent2.integrations.mcp_base import McpBridge


def _load(module: str, attr: str) -> McpBridge | None:
    """Import one bridge singleton, or None if that server is unavailable."""
    try:
        mod = __import__(module, fromlist=[attr])
        bridge = getattr(mod, attr, None)
        return bridge if isinstance(bridge, McpBridge) else None
    except Exception:
        return None


# ⚠️ ORDER IS THE UI ORDER. `/mcp`, the health section and the toast sequence all
# read this, so Burp stays first — it is the one that already existed.
_SERVERS: tuple[McpBridge, ...] = tuple(
    b for b in (
        _load("agent2.integrations.burp_mcp", "burp"),
        _load("agent2.integrations.zap_mcp", "zap"),
    ) if b is not None
)

# Task 9: forget the cached per-project answers when the workspace switches or
# another process toggles a server. Installed HERE rather than in `state` itself
# because this module is the one every surface already imports — a hook that only
# arms when someone happens to touch `state` first would arm inconsistently.
mcp_state.install_hooks()


def bridges() -> list[McpBridge]:
    """Every configured MCP bridge, in display order."""
    return list(_SERVERS)


def extra_bridges() -> list[McpBridge]:
    """Every bridge except Burp — see this module's docstring for why.

    Burp keeps its own, older, separately-tested branch in both agent loops; this
    is what the generic branch added beside it iterates.
    """
    return [b for b in _SERVERS if b.SERVER_KEY != "burp"]


def get(key: str) -> McpBridge | None:
    """The bridge with this `SERVER_KEY`, or None."""
    key = (key or "").strip().lower()
    return next((b for b in _SERVERS if key == b.SERVER_KEY), None)


def resolve(name: str) -> McpBridge | None:
    """The bridge that is currently serving tool *name*, or None.

    ⚠️ Asks each bridge `is_tool()` — membership in what it actually served — and
    never tests the prefix itself. A disconnected bridge answers False for every
    name, so a tool call that arrives after a disconnect falls through to the
    "unknown tool" path instead of being routed into a dead session.
    """
    if not name:
        return None
    for b in _SERVERS:
        try:
            if b.is_tool(name):
                return b
        except Exception:
            continue
    return None


def ensure_connected(bridge: McpBridge, timeout: float = 8.0) -> tuple[bool, str] | None:
    """Auto-connect *bridge* if it is enabled and not already up.

    Returns the `(ok, message)` of the attempt, or None when none was made —
    which is what lets a caller announce only real attempts. ⚠️ It never connects
    a bridge the user did not enable: a pentest tool that dials out on its own the
    first time the agent runs is a surprise, and for ZAP it would be a surprise
    against a target the user may not have chosen yet.
    """
    try:
        if bridge.enabled and not bridge.is_connected():
            return bridge.connect(timeout=timeout)
    except Exception as exc:
        return False, f"{bridge.LABEL} MCP: {exc}"
    return None


def gemini_declarations_for(bs: list[McpBridge]) -> list:
    """Flattened Gemini FunctionDeclarations for every connected bridge in *bs*."""
    out: list = []
    for b in bs:
        try:
            if b.is_connected():
                out.extend(b.gemini_declarations())
        except Exception:
            continue
    return out


def provider_schemas_for(bs: list[McpBridge]) -> list[dict]:
    """The same tools as `gemini_declarations_for`, in OpenAI/Anthropic shape."""
    out: list[dict] = []
    for b in bs:
        try:
            if b.is_connected():
                out.extend(b.provider_tool_schemas())
        except Exception:
            continue
    return out


def statuses() -> list[dict]:
    """One `status()` per bridge, plus its key and label, for menus and settings.

    ⚠️ Carries whatever `status()` carries and adds nothing — in particular no
    credential. `ZapMCP.auth_headers()` is the only holder of ZAP's security key
    and it is deliberately not reachable from here.
    """
    out: list[dict] = []
    for b in _SERVERS:
        try:
            row = dict(b.status())
        except Exception:
            row = {"enabled": False, "connected": False, "url": "",
                   "tool_count": 0, "tools": [], "mcp_installed": False,
                   "last_error": "status unavailable"}
        row["key"] = b.SERVER_KEY
        row["label"] = b.LABEL
        out.append(row)
    return out


# ── Health (Task 10) ───────────────────────────────────────────────────────────
# ⚠️ THE VERDICT "IS THIS SERVER HEALTHY?" IS DECLARED HERE AND NOWHERE ELSE.
# Three surfaces render it — `/mcp health`, the web MCP panel, and the `mcp`
# section of `/api/health` — and each one could trivially derive it for itself
# from `connected` and `last_error`. That is exactly the failure this repo keeps
# hitting: three copies that each look right alone and disagree the first time one
# of them learns about a new state. They read `health()`; they never re-derive.

#: What we OBSERVE. Distinct from `ok`, which is what a monitor should ALARM on.
_HEALTH_STATES = ("connected", "failed", "unavailable", "idle", "off")

#: The user-facing line for each state. `connected` / `failed` are the two the
#: Task 10 spec mandates verbatim; the other three exist because reporting
#: "connection failed" for a server nobody switched on would be a lie.
HEALTH_TEXT = {
    "connected":   "Connected",
    "failed":      "Connection failed",
    "unavailable": "mcp package not installed",
    "idle":        "Not connected",
    "off":         "Off",
}


def _health_row(b: McpBridge) -> dict:
    """One server's verdict. Total: a bridge that cannot even report degrades.

    ⚠️ `ok` IS NOT `connected`. A fault is *an enabled server we know is not
    working* — nothing else. Both bridges ship auto-connect OFF, so a plain
    install has two disconnected servers and is perfectly healthy; making that
    503 would be an alert that fires on the default configuration, which is an
    alert people learn to ignore (the same rule `/api/health` already applies to
    the WAL checkpointer and the turn scheduler).

    ⚠️ AND `idle` IS NOT A FAULT EITHER, BECAUSE OF DUAL MODE. Bridges are
    per-PROCESS singletons while enablement is in the DB, so in `agent2dual.py`
    the CLI child can hold a live ZAP session while the web process — the one
    serving `/api/health` — has never dialled it. "Enabled, no session, no error
    recorded" therefore means *we have not tried here yet*, and the next agent
    turn will resolve it into `connected` or `failed`. Reporting a fault for it
    would make every dual-mode install permanently unhealthy.
    """
    try:
        s = dict(b.status())
    except Exception:
        s = {}
    installed = bool(s.get("mcp_installed"))
    connected = bool(s.get("connected"))
    try:
        enabled = bool(b.enabled)
    except Exception:
        enabled = False
    # `connect()` clears `_last_error` before every attempt, so a non-empty one
    # means the MOST RECENT attempt failed — not that something failed once.
    detail = str(s.get("last_error") or "")

    if connected:
        state = "connected"
    elif not installed:
        state = "unavailable"
    elif detail:
        state = "failed"
    elif enabled:
        state = "idle"
    else:
        state = "off"

    return {
        "key":           b.SERVER_KEY,
        "label":         b.LABEL,
        "state":         state,
        "text":          HEALTH_TEXT.get(state, state),
        "ok":            not (enabled and state in ("failed", "unavailable")),
        "enabled":       enabled,
        "connected":     connected,
        "mcp_installed": installed,
        "tool_count":    int(s.get("tool_count") or 0),
        # ⚠️ FREE TEXT FROM A REMOTE SERVER. Fine on the terminal and in the
        # settings panel, and deliberately dropped by `health_report()` — see
        # there for why a monitoring endpoint must not carry it.
        "detail":        detail[:200],
    }


def health() -> list[dict]:
    """Every server's verdict, in display order. Never raises, never does I/O.

    `status()` is pure attribute reads and `enabled` is a cached DB read, so this
    is safe to call from `/api/health` — a probe that dialled a socket would make
    the whole endpoint block for as long as the unreachable server takes to time
    out, which is precisely when someone is looking at it.
    """
    return [_health_row(b) for b in _SERVERS]


def health_report() -> dict:
    """The monitor-safe projection of `health()` — counters and state words only.

    ⚠️ NO URL AND NO ERROR TEXT MAY ENTER THIS PAYLOAD, and neither omission is
    tidiness. `/api/health` has no auth (the documented gap) and is the endpoint
    people point a public monitor at: a URL can carry userinfo credentials
    (`http://user:pass@host`) and `last_error` is a string a *remote server we do
    not control* chose, which has already echoed request headers back at us in the
    wild. Both are available to an operator via `/api/mcp` and `/mcp health`,
    which report on the same one verdict.

    `problems` names the server and the state; a monitor learns *that* ZAP is
    down, and a human reads *why* on either of the other two surfaces.
    """
    rows = health()
    problems: list[str] = []
    for r in rows:
        if r["ok"]:
            continue
        problems.append(f"{r['label']} MCP: {r['text'].lower()}")
    return {
        "servers": [{k: r[k] for k in
                     ("key", "label", "state", "text", "ok",
                      "enabled", "connected", "mcp_installed", "tool_count")}
                    for r in rows],
        "total":     len(rows),
        "connected": sum(1 for r in rows if r["connected"]),
        "enabled":   sum(1 for r in rows if r["enabled"]),
        "failing":   sum(1 for r in rows if not r["ok"]),
        "problems":  problems,
    }

