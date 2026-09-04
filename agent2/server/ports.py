# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/ports.py
───────────────
Safe TCP port selection for the Web UI.

Agent2 prefers 1311. If that port is already taken (a second Agent2 instance,
or any other process), we must not crash — we pick the next FREE port that is
also SAFE to use. "Safe" deliberately excludes:

  • everything below 1024        — privileged / system ports (SSH, DNS, SMTP …)
  • IANA-registered service ports commonly live on a dev box (MySQL, Postgres,
    Redis, Mongo, RDP, VNC, Docker API, common dev servers …)
  • 9876                         — Burp Suite's MCP bridge, which Agent2 itself
                                   talks to; stealing it would break the agent
  • the ephemeral/dynamic range  — the OS hands these out to outbound sockets,
    so binding there invites random collisions later

It also owns the small amount of "where can this server be reached, and how do
we show it to the user" logic that goes with picking a port: `lan_ips()`,
`urls_for()` and `open_browser()`.

Only the standard library is used, so this imports safely anywhere (run.py runs
on the system Python before the venv exists).
"""

import os
import socket
import threading

# Agent2's canonical port.
DEFAULT_PORT = 1311

# Never bind at or below this — privileged on Unix, system-reserved everywhere.
MIN_SAFE_PORT = 1024

# Upper bound for our search. Staying below the ephemeral range (typically
# 32768–60999 on Linux, 49152–65535 on Windows) avoids fighting the OS for
# outbound socket ports.
MAX_SAFE_PORT = 32767

# Well-known services that frequently run on a developer machine. Binding one
# of these would either fail later or shadow a real service, so they're skipped
# even when momentarily free.
RESERVED_PORTS = frozenset({
    1433, 1434,              # MSSQL
    1521,                    # Oracle
    2049,                    # NFS
    2375, 2376, 2377,        # Docker daemon / swarm
    3000,                    # common Node/React dev server
    3128,                    # Squid proxy
    3306, 33060,             # MySQL / MySQL X
    3389,                    # RDP
    4200,                    # Angular dev server
    4444,                    # Metasploit / Selenium
    5000, 5001,              # Flask default / macOS AirPlay Receiver
    5432,                    # PostgreSQL
    5672, 15672,             # RabbitMQ
    5900, 5901,              # VNC
    5984,                    # CouchDB
    6379,                    # Redis
    7860,                    # Gradio
    8000, 8008, 8080, 8081,  # ubiquitous HTTP dev/proxy ports
    8443,                    # HTTPS alt
    8888,                    # Jupyter
    9000, 9001,              # PHP-FPM / MinIO
    9090,                    # Prometheus
    9200, 9300,              # Elasticsearch
    9876,                    # Burp Suite MCP bridge — Agent2 depends on this
    11211,                   # Memcached
    27017, 27018, 27019,     # MongoDB
})


def is_safe_port(port: int) -> bool:
    """True if *port* is in the range we're willing to bind and not reserved."""
    return (
        isinstance(port, int)
        and MIN_SAFE_PORT <= port <= MAX_SAFE_PORT
        and port not in RESERVED_PORTS
    )


def is_free(port: int, host: str = "0.0.0.0") -> bool:
    """True if *port* can actually be bound on *host* right now.

    SO_REUSEADDR is deliberately NOT set: on Windows it would let us bind a port
    another process already holds, which would make this check useless.
    """
    if not (0 < port < 65536):
        return False
    families = [(socket.AF_INET, host)]
    # An empty/all-interfaces host should also be checked on IPv6 where present,
    # since a listener there can still collide.
    if host in ("", "0.0.0.0") and socket.has_ipv6:
        families.append((socket.AF_INET6, "::"))

    for family, bind_host in families:
        s = socket.socket(family, socket.SOCK_STREAM)
        try:
            s.bind((bind_host, port))
        except Exception:
            # OSError is the expected "already bound"; anything else (a bad host
            # string, an address family the OS refuses) equally means "cannot
            # use this port", so both answer False rather than crashing startup.
            return False
        finally:
            s.close()
    return True


def _candidates(preferred: int):
    """Yield ports to try, in order: the preferred one, then a widening walk.

    The walk starts just above the preferred port (so a second instance lands on
    1312, 1313 … — predictable and easy to remember), then falls back to a few
    spaced-out blocks in case that whole neighbourhood is congested.
    """
    yield preferred
    for p in range(preferred + 1, preferred + 100):
        yield p
    for block in (1400, 1500, 2000, 4000, 7000, 12000, 18000, 24000):
        for p in range(block, block + 50):
            yield p


def find_free_port(preferred: int = DEFAULT_PORT,
                   host: str = "0.0.0.0") -> tuple[int, bool]:
    """Return `(port, is_preferred)` for the first safe, free port.

    `is_preferred` is False when we had to fall back, so the caller can tell the
    user which port was actually used.

    Raises OSError only if no safe port at all could be found, which in practice
    means the machine has no free TCP ports in a 400+ port search space.
    """
    if not is_safe_port(preferred):
        preferred = DEFAULT_PORT

    for port in _candidates(preferred):
        if not is_safe_port(port):
            continue
        if is_free(port, host):
            return port, port == preferred

    raise OSError("No safe, free TCP port available for the Agent2 Web UI.")


def describe_port_choice(port: int, preferred: int) -> str:
    """Human-readable note about the port we settled on."""
    if port == preferred:
        return f"port {port}"
    return f"port {port} (preferred {preferred} was busy)"


def lan_ips() -> list[str]:
    """Every non-loopback IPv4 address this machine can be reached on.

    Used to print the "on your network" URLs, so a phone or another laptop can
    open the Web UI without the user hunting for `ipconfig`. Ordered with the
    address of the default-route interface first, since on a box with Wi-Fi plus
    a VPN or Docker bridge that is the one the user actually wants.

    Never raises — an empty list just means we print localhost only.
    """
    found: list[str] = []

    def _add(ip: str) -> None:
        # 169.254.* is APIPA link-local: the address a NIC self-assigns when DHCP
        # fails. Printing it would offer a URL that nothing on the LAN can reach.
        if (ip and not ip.startswith("127.") and not ip.startswith("169.254.")
                and ip != "0.0.0.0" and ip not in found):
            found.append(ip)

    # The default-route address. Connecting a UDP socket sends no packets, it
    # only asks the routing table which local address would be used.
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        _add(s.getsockname()[0])
    except Exception:
        pass
    finally:
        s.close()

    # Then anything else resolvable for this host, to catch a second NIC.
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None,
                                       socket.AF_INET):
            addr = info[4][0]
            if isinstance(addr, str):
                _add(addr)
    except Exception:
        pass

    return found


def urls_for(port: int, host: str = "0.0.0.0") -> tuple[list[str], list[str]]:
    """Return `(local_urls, network_urls)` to advertise for a bound server.

    Both loopback forms are listed: `localhost` is what you click, but a raw
    `127.0.0.1` is what you need when a proxy, hosts-file entry or IPv6-only
    `localhost` resolution gets in the way.

    `network_urls` is empty when the server is bound to loopback only, because
    in that case the LAN addresses genuinely will not answer — printing them
    would send the user chasing a connection that cannot work.
    """
    local = [f"http://localhost:{port}", f"http://127.0.0.1:{port}"]
    if host in ("127.0.0.1", "localhost", "::1"):
        return local, []
    return local, [f"http://{ip}:{port}" for ip in lan_ips()]


def open_browser(port: int, *, delay: float = 1.2) -> None:
    """Open the Web UI in the default browser, off-thread and best-effort.

    Runs on a daemon timer for two reasons: the server usually is not accepting
    connections yet at the moment we are called, and on Linux `webbrowser.open`
    can block for seconds shelling out to xdg-open — which in dual mode would
    stall the CLI handover.

    Set AGENT2_NO_BROWSER=1 to suppress (headless boxes, remote sessions, or
    anyone who simply does not want a tab).
    """
    if (os.environ.get("AGENT2_NO_BROWSER") or "").strip().lower() in (
            "1", "yes", "true", "on"):
        return

    def _go():
        try:
            import webbrowser
            webbrowser.open(f"http://localhost:{port}")
        except Exception:
            pass

    try:
        t = threading.Timer(delay, _go)
        t.daemon = True
        t.start()
    except Exception:
        pass
