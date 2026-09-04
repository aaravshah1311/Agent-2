"""
agent2.integrations
────────────────────
Bridges to external tools that Agent2 drives but does not own.

    mcp_base — THE MCP bridge. One asyncio loop on a daemon thread, one name
               sanitizer, both schema converters. Subclassed per server; read its
               docstring before touching either subclass.
    burp_mcp — Burp Suite MCP bridge (SSE, default :9876). Auto-connect off.
    zap_mcp  — OWASP ZAP MCP bridge (default :8282). Auto-connect off.
    registry — the hardcoded list of the two servers above, and the questions the
               agent loops ask about them. ⚠️ A table, NOT a plugin architecture.
"""
