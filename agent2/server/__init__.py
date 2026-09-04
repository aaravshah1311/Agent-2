"""
agent2.server
──────────────
The web-serving layer: everything that exists to put Agent2 in a browser.

    routes   — all /api/* REST endpoints (Flask)
    sockets  — all Socket.IO event handlers
    ui       — renders the single-page HTML shell
    weblog   — console presentation for the web server (owns the `a2web` logger)
    ports    — safe port selection + LAN reachability display

Nothing here is imported by the CLI path, so the terminal surface never pays for
Flask. Note `weblog` is deliberately separate from `agent2.core.logging`: that one
owns the `agent2` audit logger and its rotating file, and must never be replaced.
"""
