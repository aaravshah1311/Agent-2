# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/integrations/burp_mcp.py
───────────────────────────────
Burp Suite MCP bridge.

Connects Agent2 to the Burp Suite **MCP server** (the official PortSwigger
"MCP Server" BApp, which exposes an SSE endpoint — default
http://127.0.0.1:9876/sse). Once connected, EVERY tool Burp exposes
(proxy history, Repeater, Intruder, Scanner, site map, active-scan issues,
send raw HTTP request, etc.) becomes available to the Gemini agent as a
normal function call — so the user can drive all of Burp from Agent2.

⚠️ THE MACHINERY MOVED; THE PUBLIC SURFACE DID NOT (Task 8)
────────────────────────────────────────────────────────────
Everything generic — the asyncio-loop-on-a-daemon-thread design, the SSE
lifecycle, both schema converters, `call_tool`, `status()` — now lives in
`mcp_base.McpBridge`, so OWASP ZAP is a sibling rather than a copy. Read that
module's docstring for the design and for why the tool-name prefix is
load-bearing.

What did NOT change, and must not: every name on the `burp` singleton. It is
imported by `agent2/agent.py`, `llm/provider_agent.py`, `llm/providers.py`,
`cli/env.py`, the `/api/mcp/burp/*` routes and `agent2cli.py`; `script.js` reads
all seven keys of `status()`; and `test_agent_loop.py` patches
`agent2.agent.burp` with `raising=True`, so even the module attribute name is
part of the contract. `is_burp_tool` is kept as an explicit alias of the base's
`is_tool` for exactly that reason.

Public surface (all synchronous, safe to call from any thread):
    burp.enabled                      -> bool   (auto-connect toggle)
    burp.connect(timeout=)            -> (ok, message)
    burp.disconnect()                 -> None
    burp.is_connected()               -> bool
    burp.list_tools()                 -> list[dict]   (cached MCP tool schemas)
    burp.gemini_declarations()        -> list[FunctionDeclaration]
    burp.provider_tool_schemas()      -> list[dict]
    burp.is_burp_tool(name)           -> bool
    burp.call_tool(name, args)        -> dict   (normalised result)
    burp.status()                     -> dict   (for UI / /burp command)
    burp.set_auto_connect(on)         -> None   (persisted, shared web + CLI)
"""

from __future__ import annotations

from agent2.config import BURP_MCP_URL, BURP_MCP_ENABLED
from agent2.integrations.mcp_base import (  # noqa: F401  (re-exported: see below)
    _MCP_AVAILABLE,
    _MCP_IMPORT_ERROR,
    McpBridge,
    _json_schema_to_gemini,
    _root_cause,
)

# ⚠️ `_MCP_AVAILABLE` is re-exported, not re-derived. It is surfaced to the
# browser as `status()["mcp_installed"]` and read by `agent2cli.py`; a second
# copy computed here could disagree with the one the bridge actually used.


class BurpMCP(McpBridge):
    """Singleton bridge to the Burp Suite MCP server (thread-safe, sync API)."""

    SERVER_KEY = "burp"
    LABEL = "Burp Suite"
    PREFIX = "burp_"
    THREAD_NAME = "burp-mcp"
    DEFAULT_URL = BURP_MCP_URL
    ENV_DEFAULT = BURP_MCP_ENABLED
    SETUP_HINT = "Open Burp → Extensions → MCP tab → tick 'Enabled', then retry."
    # ⚠️ THE LEGACY `burp_auto_connect` SETTING STAYS HONOURED. Existing installs
    # have it, and an upgrade that stopped reading it would silently turn
    # auto-connect off for every current user (rule 22). Task 9 moved the WRITE
    # to a per-project `mcp_state` row; this global stays the read-fallback for a
    # project that has never been configured. `integrations/state.py` says why it
    # is never written again.
    LEGACY_SETTING = "burp_auto_connect"

    # ── enablement ──────────────────────────────────────────────────────────────
    # Both `_load_enabled` and `set_auto_connect` used to live here, one copy per
    # server. They are now the base class's, reading through `integrations/state`,
    # because per-project state has to behave identically for every bridge.

    def _load_enabled(self) -> bool:
        """⚠️ Back-compat only — the live answer is the `enabled` PROPERTY.

        Kept because the name appears in the Burp docs trail and in older
        installs' muscle memory, and because returning something stale would be
        worse than not existing. It defers to the same read-through path, so it
        cannot disagree with `enabled`.
        """
        return self.enabled

    _load_auto_connect = _load_enabled

    # ── tool discovery ──────────────────────────────────────────────────────────

    def is_burp_tool(self, name: str) -> bool:
        """⚠️ The historical name for `is_tool`, and it must keep working.

        Three dispatch sites call it (`agent.py`, `llm/provider_agent.py`,
        `cli/tooling.py`) and `test_agent_loop.py`'s `FakeBurp` implements it, so
        removing it in favour of the base method would break the fake's
        conformance as well as the real callers.
        """
        return self.is_tool(name)

    def _timeout_message(self) -> str:
        return (f"Timed out connecting to Burp MCP at {self.url}. "
                "Is Burp running with the MCP server BApp enabled?")

    def _failure_message(self, err: str) -> str:
        return (f"Could not connect to Burp MCP at {self.url}: {err}\n"
                f"{self.SETUP_HINT}")


def _sanitize_name(name: str) -> str:
    """Back-compat shim for the old module-level sanitizer (now `PREFIX`-driven)."""
    return burp.sanitize_name(name)


def _candidate_urls(url: str) -> list[str]:
    """Back-compat shim for the old module-level URL walker."""
    return McpBridge.candidate_urls(url)


# Module-level singleton — import this everywhere.
burp = BurpMCP()
