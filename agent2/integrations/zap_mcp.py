# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/integrations/zap_mcp.py
──────────────────────────────
OWASP ZAP MCP bridge (Task 8) — a sibling of Burp, not a replacement.

ZAP's official **MCP Integration** add-on runs an MCP server inside ZAP. Once
connected, every tool it exposes (spider, active scan, alerts, sites, request
replay, …) becomes a normal function call for the agent, exactly like `burp_*`.

⚠️ THIS FILE IS DELIBERATELY THIN — ALL MACHINERY IS IN `mcp_base.McpBridge`
The asyncio-loop-on-a-daemon-thread design, the transport probe, both schema
converters, `call_tool` and `status()` live in the base class. Read that module's
docstring first; in particular, `PREFIX` is load-bearing and not cosmetic.

⚠️ THREE WAYS ZAP DIFFERS FROM BURP, AND EACH ONE COSTS A LINE HERE
1. **The transport is not documented.** Burp's BApp is SSE at `/sse` and says so.
   ZAP's add-on documents a port and calls itself "a standard HTTP-based
   protocol" — no path, no wire format. So `TRANSPORTS` probes streamable HTTP
   first (what a current SDK-built server most likely speaks) and falls back to
   SSE, each at the given URL and at its conventional path. A wrong guess costs
   one refused request inside `_serve`'s loop, not a failed connection.
2. **It requires a key by default.** ZAP's MCP options ship with "Require security
   key in Authorization header" ticked and a generated key in the box, so a bridge
   that cannot hold one cannot talk to a stock ZAP at all. The key is sent as the
   `Authorization` header **value, verbatim** — ZAP prepends no `Bearer`, so
   neither does `auth_headers()`.
   ⚠️ IT IS PERSISTED, AND WHAT THAT COSTS IS BOUNDED ON PURPOSE. It lives in
   `mcp_config.security_key` as plaintext, exactly as `providers.api_key` already
   does, because the alternative shipped first and was wrong: memory-only meant
   re-entering ZAP's key on every launch, and an env var is not editable from
   either surface. What stays true is the part that matters — it is never returned
   by `status()`, never carried by `/api/mcp`, never logged, and never rendered:
   surfaces get `key_set` plus fixed-width bullets from `state.mask_secret()`,
   which derive nothing from the value, not even its length. Task 16's SecretStore
   owns moving this column and `providers.api_key` behind OS keyring storage
   together; its spec already calls for migrating existing credential storage, and
   one row in a known table is what makes that migration findable.
3. **"Secure Only" ships ON**, so a stock ZAP answers HTTPS and refuses plain
   HTTP. That surfaces as a connection failure, and the failure message says what
   to do about it. ⚠️ There is deliberately NO "skip TLS verification" switch:
   silently trusting any certificate on a security tool's control channel is
   exactly the kind of quiet downgrade rule 29 exists to prevent.

Public surface: identical to `burp` (see `mcp_base.McpBridge`), plus `set_key`.
"""

from __future__ import annotations

from agent2.config import ZAP_MCP_ENABLED, ZAP_MCP_KEY, ZAP_MCP_URL
from agent2.integrations.mcp_base import McpBridge

class ZapMCP(McpBridge):
    """Singleton bridge to the OWASP ZAP MCP server (thread-safe, sync API)."""

    SERVER_KEY = "zap"
    LABEL = "OWASP ZAP"
    PREFIX = "zap_"
    THREAD_NAME = "zap-mcp"
    DEFAULT_URL = ZAP_MCP_URL
    ENV_DEFAULT = ZAP_MCP_ENABLED
    ENV_KEY = ZAP_MCP_KEY
    SETUP_HINT = ("Open ZAP → Tools → Options → MCP Server → tick 'Enable MCP Server'. "
                  "If 'Secure Only' is on, use an https:// URL and trust ZAP's root CA "
                  "(Options → Network → Server Certificates → Save), or untick it.")
    PROMPT_HINT = ("These drive ZAP itself: spidering a site, active and passive scanning, "
                   "listing alerts and sites, and replaying requests. Prefer them over shell "
                   "tools for web vulnerability scanning; use run_command for OS-level tools.")
    # ⚠️ Order matters: probe the newer transport first. See point 1 above.
    TRANSPORTS = ("streamable_http", "sse")
    # The global key ZAP's toggle wrote before Task 9 made state per-project. Read
    # as a fallback, never written — `integrations/state.py` explains why.
    LEGACY_SETTING = "zap_auto_connect"

    # ── authentication ──────────────────────────────────────────────────────────

    @property
    def key(self) -> str:
        """The security key, read through: stored config → `ZAP_MCP_KEY`.

        ⚠️ NO STORED COPY — the rule `enabled` and `url` follow, and here it also
        means a key set in the browser reaches a CLI process (and the reverse) on
        its next connect rather than at the next restart. Reading this in order to
        DISPLAY it is a bug; `state.config_for()` is the surface-safe view.
        """
        try:
            from agent2.integrations import state
            return state.get_key(self.SERVER_KEY, env_default=ZAP_MCP_KEY)
        except Exception:
            return ZAP_MCP_KEY

    @key.setter
    def key(self, value: str) -> None:
        self.set_key(value)

    def set_key(self, key: str) -> None:
        """Persist the ZAP security key (empty clears it back to `ZAP_MCP_KEY`).

        See this module's docstring for why a live credential is stored at all and
        what is guaranteed about it. `""` is a real instruction here — "forget the
        stored key" — which is why `state.set_config` distinguishes it from the
        `None` that a form submitted with an untouched key field sends.
        """
        try:
            from agent2.integrations import state
            state.set_config(self.SERVER_KEY, key=(key or "").strip())
        except Exception:
            pass

    # `set_security_key`/`security_key` are the generic spelling every surface
    # uses, so a caller never needs to know which bridge takes a credential.
    set_security_key = set_key

    def security_key(self) -> str:
        return self.key

    def auth_headers(self) -> dict[str, str]:
        """⚠️ ZAP takes the key as the Authorization value VERBATIM — no `Bearer`.

        Never surfaced by `status()`; see this module's docstring.
        """
        key = self.key
        return {"Authorization": key} if key else {}

    # ── messages ────────────────────────────────────────────────────────────────

    def _timeout_message(self) -> str:
        return (f"Timed out connecting to OWASP ZAP MCP at {self.url}. "
                "Is ZAP running with the MCP Integration add-on enabled?")

    def _failure_message(self, err: str) -> str:
        return (f"Could not connect to OWASP ZAP MCP at {self.url}: {err}\n"
                f"{self.SETUP_HINT}")


# Module-level singleton — import this everywhere.
zap = ZapMCP()
