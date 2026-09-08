# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/server/auth.py
─────────────────────
THE ONE PLACE THAT DECIDES WHETHER A WEB REQUEST MAY PROCEED (Task 14).

What this closes
────────────────
`AGENT2_HOST` defaults to `0.0.0.0`, so the server binds every interface, and
until this module existed there was no authentication on any route or any socket
event. The `run_raw_command` socket event runs an arbitrary shell command; the
agent's own `run_command` tool runs more. Anything that could route to the box —
a laptop on the same café Wi-Fi, another container on the same bridge — had
unauthenticated remote code execution. `routes.py`'s docstring said so out loud
("⚠️ COUNTERS ONLY — THERE IS NO AUTH ON ANY ROUTE IN THIS FILE") and the startup
banner printed a one-line warning; neither is a control.

⚠️ ONE DECISION FUNCTION, AND EVERY SURFACE ASKS IT
`decide()` is the only thing that answers "may this proceed". The HTTP guard
(`install()`'s `before_request`) and the Socket.IO gate (`socket_allowed()`) are
both thin callers of it. A second opinion is how a surface ends up permissive:
the socket handshake does **not** pass through Flask's `before_request` at all —
engineio's middleware wraps the WSGI app *outside* Flask — so a guard installed
only on the HTTP side leaves `run_raw_command` wide open while every `/api/*`
route looks locked. That asymmetry is invisible from either half.

The trust model
───────────────
Three modes, from `AGENT2_WEB_AUTH`:

* `auto` *(default)* — loopback is trusted, everything else needs the token.
  This is the whole "keep local development convenient without exposing
  dangerous APIs anonymously" requirement in one rule: `python run.py --web`
  keeps working with zero setup, and the LAN address in the banner stops being
  an open shell.
* `always` — even loopback needs the token. For a shared machine, or a box
  where another local user is not trusted.
* `off` — no checks. An explicit, loudly-announced downgrade for a deployment
  that already has auth in front of it (a reverse proxy, an SSH tunnel). It is
  an env var and *not* a DB setting on purpose: a stored "off" would be a
  persistent silent downgrade that a compromised DB could set for you, and
  nothing in the UI can turn the guard off.

⚠️ THE ACCESS TOKEN IS NEVER WRITTEN TO DISK.
It comes from `AGENT2_WEB_TOKEN`, or is generated once per process and printed in
the startup banner. `web_sessions` stores *hashes* of session and CSRF cookies,
never the token and never a cookie. So a copied `agent2.db` — a backup, a mounted
Docker volume, a shared screen — grants nothing. This is also why rotation is
cheap: `rotate_token()` swaps the in-memory value and revokes every session.

⚠️ ORIGIN IS CHECKED EVEN WHEN THE REQUEST IS TRUSTED.
Loopback trust on its own is the classic localhost-CSRF hole: any web page the
user visits can `fetch('http://127.0.0.1:1311/api/…', {mode:'no-cors'})` and the
request genuinely arrives from 127.0.0.1. So an unsafe method is refused when
`Origin` disagrees with `Host`, or when `Sec-Fetch-Site` says `cross-site` —
before any question of who the caller is. Browsers always send at least one of
those; `curl` sends neither, which is exactly the distinction that lets a local
script keep working while a hostile page cannot.

⚠️ AND `Host` IS CHECKED BEFORE `Origin`, BECAUSE `Origin` IS COMPARED *AGAINST*
`Host` AND `Host` IS THE CLIENT'S.
Origin-vs-Host refuses the ordinary hostile page — the browser puts the real
target in `Host`, so the two disagree — and cannot refuse **DNS rebinding**, where
they agree because the attacker owns the name on both sides. A page served from
`evil.example` on a short TTL, whose name is then re-pointed at `127.0.0.1`, is
*same-origin to the browser*: `Origin: http://evil.example`,
`Sec-Fetch-Site: same-origin`, `Host: evil.example`, and `remote_addr` really is
`127.0.0.1`. Measured before the fix: a `POST /api/memories` wrote a row (200) and
`socket_allowed()` returned **True** — so merely visiting a page bought the API
*and* `run_raw_command`. `host_ok()` closes it by refusing an unknown DNS **name**,
and an **IP-literal `Host` is always allowed** because there is nothing to rebind
about an address: loopback, the LAN address in the banner, a container IP and a
Tailscale IP all keep working, and only the vector is refused. It is asked by
`decide()` *above* `PUBLIC_PATHS` (nothing at all is served under a name this
server does not answer to — least of all the login page) and again inside
`socket_allowed()`, since the handshake never reaches `before_request`.

⚠️ EVERY CREDENTIAL COMPARISON GOES THROUGH `_cmp_bytes`.
`hmac.compare_digest` **raises** `TypeError` on a non-ASCII `str`, Werkzeug
latin-1-decodes request headers, and `decide()` has no outer `try` — so one `0xFF`
byte in `Authorization` or `X-A2-CSRF` turned the gate into a **500**. A gate that
answers 500 has stopped deciding, which is the one thing this module may never do.

⚠️ CSRF IS ENFORCED ON COOKIE-AUTHENTICATED REQUESTS, NOT ON BEARER ONES.
The double-submit pair (`a2_csrf` cookie + `X-A2-CSRF` header) only defends
against *ambient* credentials. A caller that presents `Authorization: Bearer …`
attached that credential deliberately, so requiring a second token buys nothing
and would break every script. Conversely a browser session must always carry the
header, which is why the front end wraps `window.fetch` in ONE place rather than
touching forty call sites (see `public/script.js`).

Sessions
────────
A session is an opaque 256-bit value in an `HttpOnly` cookie; the row is keyed by
its SHA-256. That is what makes expiry, rotation and logout real — a
self-contained signed cookie cannot be revoked, and `SECRET_KEY` defaults to
`os.urandom(32)`, so a signed cookie would also die on every restart. `expires_at`
is an *idle* deadline pushed forward by each authenticated request; a session
older than `ROTATE_AFTER` is re-issued under a new id (old row deleted) so a
long-lived tab does not keep one cookie value forever.

Layer: config / database → auth → routes · sockets · weblog.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import secrets
import socket
import threading
import time
from collections import deque
from datetime import UTC, datetime, timedelta

from agent2.core import logging as audit
from agent2.core import permissions as perms
from agent2.database import exe, qone

# ── Names the rest of the app shares ──────────────────────────────────────────
SESSION_COOKIE = "a2_session"
CSRF_COOKIE = "a2_csrf"
CSRF_HEADER = "X-A2-CSRF"
TOKEN_HEADER = "Authorization"
TOKEN_PARAM = "token"

MODE_AUTO = "auto"
MODE_ALWAYS = "always"
MODE_OFF = "off"
_MODES = (MODE_AUTO, MODE_ALWAYS, MODE_OFF)

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

#: Served to anyone: the shell's own static assets carry no state, and the login
#: endpoints are what an unauthenticated caller is supposed to reach.
PUBLIC_PATHS = frozenset({
    "/style.css", "/script.js", "/favicon.ico",
    "/api/auth/status", "/api/auth/login",
})

#: Reachable without a session, but only from loopback. `/api/health` is counters
#: only (its own tests pin that) and Docker's healthcheck runs inside the
#: container, i.e. from 127.0.0.1 — so this keeps container health working
#: without publishing internal state to the LAN.
LOOPBACK_PATHS = frozenset({"/api/health"})

# ── Tunables ──────────────────────────────────────────────────────────────────
DEFAULT_SESSION_TTL = 12 * 3600      # idle deadline, seconds
DEFAULT_ROTATE_AFTER = 3600          # re-issue the cookie value this often
DEFAULT_RATE_LIMIT = 600             # requests per minute per IP (0 = off)
DEFAULT_LOGIN_LIMIT = 10             # login attempts per LOGIN_WINDOW per IP
LOGIN_WINDOW = 300
TOKEN_BYTES = 24                     # 192 bits, urlsafe → 32 chars
SESSION_BYTES = 32                   # 256 bits
_RATE_KEYS_MAX = 4096                # bound the in-memory limiter


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _env_int(name: str, default: int, *, floor: int = 0) -> int:
    try:
        return max(floor, int(_env(name) or default))
    except (TypeError, ValueError):
        return default


def mode() -> str:
    """The active trust mode. Unknown values fall back to `auto`, not to `off`.

    Read on every call rather than captured at import: a test (and a future
    `/settings` surface) must be able to change it without re-importing, and a
    module-level snapshot of an authorization decision is the kind of frozen copy
    the one-declaration rule exists to prevent.
    """
    raw = _env("AGENT2_WEB_AUTH", MODE_AUTO).lower()
    if raw in ("1", "on", "true", "yes", "require", "required"):
        return MODE_ALWAYS
    if raw in ("0", "no", "false", "none", "disabled"):
        return MODE_OFF
    return raw if raw in _MODES else MODE_AUTO


def session_ttl() -> int:
    return _env_int("AGENT2_WEB_SESSION_TTL", DEFAULT_SESSION_TTL, floor=60)


def rotate_after() -> int:
    return _env_int("AGENT2_WEB_SESSION_ROTATE", DEFAULT_ROTATE_AFTER, floor=60)


def rate_limit() -> int:
    return _env_int("AGENT2_WEB_RATE_LIMIT", DEFAULT_RATE_LIMIT)


def login_limit() -> int:
    return _env_int("AGENT2_WEB_LOGIN_LIMIT", DEFAULT_LOGIN_LIMIT)


def extra_origins() -> tuple[str, ...]:
    """Origins allowed in addition to the request's own host.

    The escape hatch for a reverse proxy that terminates on a different name.
    Comma-separated, compared case-insensitively, no wildcards — a wildcard here
    would re-open exactly the cross-origin hole the check closes.

    ⚠️ `*` is DROPPED, not merely inert. `origin_ok()` does an exact set-membership
    test, so a `*` entry already matches nothing; discarding it here is what stops
    a later reader from "fixing" the apparently-broken wildcard by teaching the
    comparison to glob. An operator who writes `*` is asking for `off`, and the
    honest answer is to make them say so (`AGENT2_WEB_AUTH=off`) rather than to
    quietly grant every origin from a list that reads like an allowlist.
    """
    raw = _env("AGENT2_WEB_ORIGINS")
    return tuple(o.strip().rstrip("/").lower() for o in raw.split(",")
                 if o.strip() and "*" not in o)


# ── The access token ──────────────────────────────────────────────────────────
_TOKEN_LOCK = threading.Lock()
_TOKEN: str | None = None
_TOKEN_FROM_ENV = False


def access_token() -> str:
    """The token that buys a session. Generated once per process if unset.

    ⚠️ Deliberately memory-only. Persisting it would put a permanent key to the
    agent's shell in every copy of `agent2.db`; regenerating it per process means
    the worst case of a leaked banner is "until the next restart".
    """
    global _TOKEN, _TOKEN_FROM_ENV
    with _TOKEN_LOCK:
        env = _env("AGENT2_WEB_TOKEN")
        if env:
            if env != _TOKEN:
                _TOKEN, _TOKEN_FROM_ENV = env, True
            return _TOKEN
        if _TOKEN is None or _TOKEN_FROM_ENV:
            _TOKEN, _TOKEN_FROM_ENV = secrets.token_urlsafe(TOKEN_BYTES), False
        return _TOKEN


def token_is_from_env() -> bool:
    access_token()
    return _TOKEN_FROM_ENV


def rotate_token() -> str:
    """Mint a new access token and revoke every live session.

    Revocation is not optional: a rotation that left existing sessions alive
    would rotate the *door key* while the people already inside stay inside,
    which is the opposite of what anyone asks rotation for.

    ⚠️ Revoke FIRST, then swap. `revoke_all()` raises on a DB fault (see its
    docstring), and the caller reports that as a failed rotation — but only the
    order below makes that report true. Swapping first and then failing to revoke
    leaves the worst of both: the token the user was told to stop using is gone,
    and every session it already minted is still valid. Doing it this way, a
    failure changes nothing at all.
    """
    global _TOKEN, _TOKEN_FROM_ENV
    revoke_all()
    with _TOKEN_LOCK:
        _TOKEN, _TOKEN_FROM_ENV = secrets.token_urlsafe(TOKEN_BYTES), False
        fresh = _TOKEN
    audit.event("auth.token.rotate")
    return fresh


def _cmp_bytes(value: str) -> bytes:
    """Encode a credential for `hmac.compare_digest`, which cannot take one raw.

    ⚠️ `compare_digest` **RAISES** `TypeError: comparing strings with non-ASCII
    characters is not supported` when either `str` argument is non-ASCII, and every
    caller below is fed request data. Werkzeug latin-1-decodes HTTP headers, so a
    single `0xFF` byte in `Authorization`, `X-A2-Token`, `?token=` or `X-A2-CSRF`
    arrives as `'ÿ'` — and `decide()` has no outer `try`, so that `TypeError`
    escaped `before_request` and Flask rendered a **500**. A gate that answers "500"
    has not refused anything: it has stopped deciding, which is the one thing this
    module may never do. (`socket_allowed()` was unaffected only because its blanket
    `except` already fails closed.)

    ⚠️ Encoded, never rejected-if-non-ASCII: `AGENT2_WEB_TOKEN` is an operator's own
    string and `os.environ` on POSIX can legitimately hand back undecodable bytes as
    surrogates, so refusing non-ASCII outright would lock such an operator out of
    their own server. ⚠️ And `surrogatepass`, never `replace`: `replace` maps every
    unencodable character onto one replacement byte, so two *different* candidates
    could compare equal — a comparison hardened against timing that had been
    silently weakened against content. `surrogatepass` encodes any `str` at all, and
    injectively, which is what a credential comparison needs.
    """
    return str(value).encode("utf-8", "surrogatepass")


def token_matches(candidate: str) -> bool:
    """Constant-time comparison against the access token."""
    if not candidate:
        return False
    return hmac.compare_digest(_cmp_bytes(candidate), _cmp_bytes(access_token()))


# ── Hashing / time helpers ────────────────────────────────────────────────────
def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0, tzinfo=None)


def _stamp(dt: datetime) -> str:
    """Format a datetime the way the rest of the schema does (`datetime('now')`).

    Same shape means a lexicographic comparison in SQL is also a chronological
    one, which is what makes `expires_at < datetime('now')` a correct purge.
    """
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _parse(text: str) -> datetime | None:
    """Read one of our own `_stamp()` strings back.

    The `+0000` / `%z` round trip is not decoration: SQLite's `datetime('now')`
    and `_stamp()` both write **UTC with no offset in the text**, so the zone has
    to be supplied here or the value is merely "some naive string" and a reader
    is free to assume local time. It is stripped again immediately because every
    comparison in this module is against `_now()`, which is naive-UTC — mixing
    the two raises `TypeError` mid-request, and a guard that raises is a guard
    that 500s instead of deciding.
    """
    try:
        aware = datetime.strptime(f"{str(text)[:19]}+0000", "%Y-%m-%d %H:%M:%S%z")
    except (TypeError, ValueError):
        return None
    return aware.replace(tzinfo=None)


def is_loopback(addr: str | None) -> bool:
    """True for 127.0.0.0/8, ::1 and IPv4-mapped forms of them."""
    if not addr:
        return False
    raw = str(addr).strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    raw = raw.split("%", 1)[0]
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return raw.lower() == "localhost"
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return bool(ip.is_loopback)


# ── Session store ─────────────────────────────────────────────────────────────
def purge_expired() -> int:
    """Drop sessions past their idle deadline. Best-effort, never raises."""
    try:
        exe("DELETE FROM web_sessions WHERE expires_at < ?", (_stamp(_now()),))
    except Exception:
        return 0
    return 1


def issue_session(ip: str = "", user_agent: str = "",
                  rotated_from: str = "") -> tuple[str, str, datetime]:
    """Create a session. Returns `(session_value, csrf_value, expires_at)`.

    The two returned values are the ONLY time either exists in plaintext outside
    a cookie — the row holds their digests.
    """
    sid = secrets.token_urlsafe(SESSION_BYTES)
    csrf = secrets.token_urlsafe(SESSION_BYTES)
    expires = _now() + timedelta(seconds=session_ttl())
    exe(
        "INSERT OR REPLACE INTO web_sessions "
        "(id, csrf_hash, ip, user_agent, created_at, last_seen_at, expires_at, "
        " rotated_from) VALUES (?,?,?,?,?,?,?,?)",
        (_digest(sid), _digest(csrf), str(ip or "")[:64],
         str(user_agent or "")[:200], _stamp(_now()), _stamp(_now()),
         _stamp(expires), str(rotated_from or "")),
    )
    return sid, csrf, expires


def lookup_session(value: str) -> dict | None:
    """Return the row for a cookie value, or None if absent/expired.

    An expired row is deleted on the way out rather than merely ignored, so a
    long-idle browser cannot keep it alive by presenting it, and the table does
    not need a sweeper thread to stay bounded.
    """
    if not value:
        return None
    try:
        row = qone("SELECT * FROM web_sessions WHERE id=?", (_digest(value),))
    except Exception:
        return None
    if not row:
        return None
    expires = _parse(row.get("expires_at"))
    if expires is None or expires <= _now():
        try:
            revoke_session(value)
        except Exception:
            # Housekeeping only. We are returning None either way, so the caller
            # is already denied; the row will be swept by the next `purge_expired`.
            pass
        return None
    return dict(row)


def touch_session(value: str) -> None:
    """Push the idle deadline forward. Called once per authenticated request."""
    try:
        exe("UPDATE web_sessions SET last_seen_at=?, expires_at=? WHERE id=?",
            (_stamp(_now()),
             _stamp(_now() + timedelta(seconds=session_ttl())),
             _digest(value)))
    except Exception:
        pass


def revoke_session(value: str) -> None:
    """Delete one session row.

    ⚠️ Deliberately NOT wrapped in a swallow, unlike every other write in this
    module. Every other failure here fails CLOSED — a DB fault degrades to "no
    session", i.e. deny. This one is the exception: a revoke that quietly failed
    would report a successful logout while leaving the cookie live, which is the
    only error on this path that hands access *back*. Callers that genuinely must
    not raise swallow it locally, where "already denying" makes the loss harmless
    (see `lookup_session`).
    """
    exe("DELETE FROM web_sessions WHERE id=?", (_digest(value),))


def revoke_all() -> None:
    """Delete every session row. Raises, for the reason `revoke_session` states."""
    exe("DELETE FROM web_sessions")


def session_count() -> int:
    try:
        row = qone("SELECT COUNT(*) AS n FROM web_sessions")
        return int((row or {}).get("n") or 0)
    except Exception:
        return 0


def needs_rotation(row: dict) -> bool:
    created = _parse((row or {}).get("created_at") or "")
    if created is None:
        return False
    return (_now() - created).total_seconds() >= rotate_after()


# ── Rate limiting ─────────────────────────────────────────────────────────────
_RATE_LOCK = threading.Lock()
_RATE: dict[tuple[str, str], deque] = {}


def rate_check(bucket: str, key: str, limit: int, window: int) -> int:
    """Consume one slot. Returns 0 when allowed, else seconds to wait.

    In-process and in-memory by design: the limiter exists to blunt brute force
    and runaway clients on one server, and a DB round-trip per request would cost
    more than the attack. A restart forgiving the counters is acceptable — a
    restart also mints a new access token.
    """
    if limit <= 0:
        return 0
    now = time.monotonic()
    slot = (bucket, str(key or "?"))
    with _RATE_LOCK:
        if len(_RATE) > _RATE_KEYS_MAX:
            _RATE.clear()
        hits = _RATE.setdefault(slot, deque())
        while hits and now - hits[0] > window:
            hits.popleft()
        if len(hits) >= limit:
            return max(1, int(window - (now - hits[0])) + 1)
        hits.append(now)
        return 0


def rate_reset() -> None:
    with _RATE_LOCK:
        _RATE.clear()


# ── Host / Origin / CSRF ──────────────────────────────────────────────────────
def _bare_host(raw: str) -> str:
    """Strip brackets and a trailing port from a `host[:port]` string.

    One spelling for both readers — the request's own `Host` and the entries of
    `AGENT2_WEB_ORIGINS` — because an allowlist that keeps the port on one side and
    drops it on the other never matches, and the symptom is a 403 nobody can
    explain. A bracket-less address carrying more than one colon is a bare IPv6
    literal and is left whole; splitting it would produce a prefix.
    """
    raw = (raw or "").strip().lower().rstrip("/")
    if raw.startswith("["):
        end = raw.find("]")
        return raw[1:end] if end > 0 else raw[1:]
    if raw.count(":") == 1:
        raw = raw.split(":", 1)[0]
    return raw


def _is_ip_literal(host: str) -> bool:
    """Whether `host` is an IP address rather than a name."""
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    return True


_OWN_NAMES_LOCK = threading.Lock()
_OWN_NAMES: frozenset[str] | None = None


def own_names() -> frozenset[str]:
    """Names this machine legitimately answers to, resolved once per process.

    `socket.gethostname()` only reads the configured hostname — no DNS, no reverse
    lookup — so it is safe on the request path. ⚠️ `socket.getfqdn()` is deliberately
    NOT consulted: it can perform a reverse lookup that blocks for the resolver's
    whole timeout, and a hostname allowlist that can hang is a hostname allowlist
    that hangs every request on a box with a sick resolver. A deployment reached
    under any other name says so in `AGENT2_WEB_ORIGINS`.
    """
    global _OWN_NAMES
    with _OWN_NAMES_LOCK:
        if _OWN_NAMES is None:
            names = {"localhost", "localhost.localdomain"}
            try:
                host = (socket.gethostname() or "").strip().lower()
            except Exception:
                host = ""
            if host:
                names.add(host)
                names.add(host.split(".", 1)[0])
            _OWN_NAMES = frozenset(n for n in names if n)
        return _OWN_NAMES


def _origin_hosts() -> frozenset[str]:
    """The host halves of `AGENT2_WEB_ORIGINS` — the `Host` half of that escape hatch.

    Read live, exactly as `extra_origins()` is: one variable configures both checks,
    so an operator who names their proxy once is not asked to name it twice.
    """
    return frozenset(_bare_host(o.split("://", 1)[-1]) for o in extra_origins())


def host_ok(req) -> bool:
    """Whether `Host` names something this server may answer to. Blocks DNS rebinding.

    ⚠️ THIS EXISTS BECAUSE `_allowed_origins()` IS BUILT FROM `Host`, AND `Host` IS
    THE CLIENT'S. Comparing `Origin` against `Host` refuses the *ordinary* hostile
    page — the browser sets `Host` to the real target, so the two disagree — but it
    cannot refuse **DNS rebinding**, where they agree because the attacker owns the
    name on both sides. Serve a page from `evil.example` on a short TTL, re-point
    `evil.example` at `127.0.0.1`, and the browser then believes its own fetches are
    same-origin: it sends `Origin: http://evil.example`, `Sec-Fetch-Site:
    same-origin` and `Host: evil.example`, while `remote_addr` really is `127.0.0.1`.
    So `origin_ok()` passed, loopback trust applied in the default `auto` mode, and
    merely *visiting a page* bought the whole API plus `run_raw_command` — remote
    code execution, from a check that reads as a CSRF defence and is one.

    ⚠️ AN IP-LITERAL `Host` IS ALWAYS ACCEPTED, AND THAT ASYMMETRY IS THE WHOLE FIX.
    Rebinding needs a *name* to re-point; an address literal has nothing to rebind.
    So `http://127.0.0.1:1311`, the LAN address in the startup banner, a container
    IP, a Tailscale IP and an IPv6 literal all keep working untouched, and only an
    **unknown DNS name** is refused — which is exactly the vector and nothing else.
    That is what keeps this from being the "unnecessary security restriction that
    breaks legitimate functionality" a blanket name allowlist would have been.

    ⚠️ ASKED FOR EVERY METHOD, NOT JUST THE UNSAFE ONES. `origin_ok()` guards unsafe
    methods because a cross-site *read* is already stopped by the browser; a rebound
    origin is same-origin, so the browser hands the response back and a plain
    `GET /api/chats` is the entire conversation history. Whoever the caller turns out
    to be is a later question — hence the check sits above `PUBLIC_PATHS` too: under
    a name this server does not answer to, nothing is served at all.

    An absent `Host` (HTTP/1.0, a hand-rolled client) is allowed: there is no name to
    rebind, and no browser omits it.
    """
    host = _bare_host(getattr(req, "host", ""))
    if not host:
        return True
    if _is_ip_literal(host):
        return True
    return host in own_names() or host in _origin_hosts()


def _allowed_origins(req) -> set[str]:
    """Origins that count as "this site" for `req`.

    ⚠️ Still derived from `Host`, and that is only safe because `host_ok()` has
    already refused every name this server does not answer to. Wire one without the
    other and the allowlist is the client's to write — see `host_ok`'s docstring.
    """
    host = (getattr(req, "host", "") or "").lower().rstrip("/")
    out = set(extra_origins())
    if host:
        out.add(f"http://{host}")
        out.add(f"https://{host}")
    return out


def origin_ok(req) -> bool:
    """Reject a browser request that a *different* site caused.

    Checked for unsafe methods regardless of who the caller is, because loopback
    trust cannot distinguish "the user's own tab" from "a page the user happened
    to visit". Absent headers mean a non-browser client and are allowed — that is
    what keeps `curl` and the existing test clients working.
    """
    origin = (req.headers.get("Origin") or "").strip().rstrip("/").lower()
    if origin and origin != "null":
        if origin not in _allowed_origins(req):
            return False
    elif origin == "null":
        return False
    site = (req.headers.get("Sec-Fetch-Site") or "").strip().lower()
    if site and site not in ("same-origin", "none"):
        return False
    return True


def csrf_ok(req, row: dict | None) -> bool:
    """Double-submit check: cookie value, header value and stored digest agree.

    Every comparison goes through `_cmp_bytes` — see its docstring for why a raw
    `str` here turned a non-ASCII `X-A2-CSRF` header into a 500.
    """
    if not row:
        return False
    header = (req.headers.get(CSRF_HEADER) or "").strip()
    cookie = (req.cookies.get(CSRF_COOKIE) or "").strip()
    if not header or not cookie:
        return False
    if not hmac.compare_digest(_cmp_bytes(header), _cmp_bytes(cookie)):
        return False
    stored = str(row.get("csrf_hash") or "")
    return bool(stored) and hmac.compare_digest(_cmp_bytes(_digest(header)),
                                                _cmp_bytes(stored))


# ── The verdict ───────────────────────────────────────────────────────────────
class Verdict:
    """What `decide()` answers. `ok` plus enough context to act on a refusal.

    A small object rather than a bare bool because three different callers need
    three different reactions to the same refusal: the HTTP guard renders a page
    or a JSON body, the socket gate returns False, and `after_request` decides
    whether the caller has earned a fresh cookie pair.
    """

    __slots__ = ("code", "may_issue", "ok", "retry_after", "session",
                 "session_value", "status", "text", "trusted")

    def __init__(self, ok: bool, *, status: int = 200, code: str = "ok",
                 text: str = "", session: dict | None = None,
                 session_value: str = "", may_issue: bool = False,
                 retry_after: int = 0, trusted: bool = False):
        self.ok = ok
        self.status = status
        self.code = code
        self.text = text
        self.session = session
        self.session_value = session_value
        self.may_issue = may_issue
        self.retry_after = retry_after
        self.trusted = trusted

    def __repr__(self) -> str:                       # pragma: no cover - debug
        return f"<Verdict ok={self.ok} code={self.code} status={self.status}>"


_OK_OFF = "auth disabled (AGENT2_WEB_AUTH=off)"


def client_ip(req) -> str:
    """The peer address. `X-Forwarded-For` is NOT consulted.

    ⚠️ A forwarded header is client-controlled, so trusting it would let anyone
    claim `127.0.0.1` and inherit loopback trust — the header would *become* the
    authentication bypass. A deployment behind a real proxy sets
    `AGENT2_WEB_AUTH=always` (or `off`, with the proxy authenticating) instead.
    """
    return str(getattr(req, "remote_addr", "") or "")


def decide(req) -> Verdict:
    """May this request proceed? The single authorization decision.

    Order matters and each step is a different question:
      1. rate limit   — cheapest, and applies even to public paths
      2. host         — is this server even known by that name? (DNS rebinding)
      3. origin       — did a *different site* cause this? (unsafe methods)
      4. identity     — session cookie, bearer token, or trusted loopback
      5. CSRF         — only for ambient (cookie) credentials
      6. capability   — is this identity permitted THIS operation? (Task 15)

    ⚠️ Step 6 is last for a reason. Answering "you may not do that" to a caller
    whose identity is still unknown tells a stranger which endpoints exist and
    which are guarded; answering "who are you" first means an anonymous prober
    learns nothing beyond "authentication required".

    ⚠️ Step 2 sits ABOVE `PUBLIC_PATHS` and `LOOPBACK_PATHS`, which is the only
    placement that works: under a name this server does not answer to, nothing at
    all may be served — not the stylesheet, not `/api/auth/status`, and above all
    not the login page, which under a rebound name is a credential-phishing form
    wearing this server's clothes. It is also the one step that applies to **safe**
    methods, because a rebound origin *is* same-origin to the browser, so the
    response comes back and a plain `GET /api/chats` is the whole transcript.
    See `host_ok()` for the attack and why an IP-literal `Host` is exempt.
    """
    path = str(getattr(req, "path", "") or "")
    method = str(getattr(req, "method", "GET") or "GET").upper()
    ip = client_ip(req)
    m = mode()

    # 1. Rate limit — before anything that touches the DB.
    wait = rate_check("http", ip, rate_limit(), 60)
    if wait:
        return Verdict(False, status=429, code="rate_limited", retry_after=wait,
                       text="Too many requests — slow down.")

    if m == MODE_OFF:
        return Verdict(True, code="off", text=_OK_OFF, trusted=True)

    # 2. Host — refuse a name this server does not answer to, before anything is
    #    served under it. An IP literal cannot be rebound and is always allowed.
    if not host_ok(req):
        return Verdict(False, status=403, code="bad_host",
                       text="Unrecognised Host — refused. Add the name to "
                            "AGENT2_WEB_ORIGINS if you reach this server through it.")

    loopback = is_loopback(ip)
    trusted = loopback and m == MODE_AUTO

    if path in PUBLIC_PATHS:
        return Verdict(True, code="public", may_issue=False, trusted=trusted)
    if path in LOOPBACK_PATHS and loopback:
        return Verdict(True, code="loopback", trusted=trusted)

    # 3. Origin — an unsafe method caused by another site is refused outright,
    #    whoever the caller turns out to be.
    if method not in SAFE_METHODS and not origin_ok(req):
        return Verdict(False, status=403, code="bad_origin",
                       text="Cross-origin request refused.")

    # 4. Identity.
    cookie = (req.cookies.get(SESSION_COOKIE) or "").strip()
    row = lookup_session(cookie) if cookie else None
    if row is not None:
        # 5. CSRF applies to the ambient credential only.
        if method not in SAFE_METHODS and not csrf_ok(req, row):
            return Verdict(False, status=403, code="csrf",
                           text="Missing or invalid CSRF token.")
        return _authorized(Verdict(True, code="session", session=row,
                                   session_value=cookie, trusted=trusted),
                           method, path, ip)

    bearer = bearer_token(req)
    if bearer:
        if token_matches(bearer):
            return _authorized(Verdict(True, code="token", may_issue=True,
                                       trusted=trusted), method, path, ip)
        audit.event("auth.deny", reason="bad_token", ip=ip, path=path)
        return Verdict(False, status=401, code="bad_token",
                       text="Invalid access token.")

    if trusted:
        # Loopback in `auto` mode: allowed, and allowed to be handed cookies so
        # the browser half gets a real session (and therefore CSRF cover) on its
        # very first page load.
        return _authorized(Verdict(True, code="loopback", may_issue=True,
                                   trusted=True), method, path, ip)

    if cookie:
        return Verdict(False, status=401, code="expired",
                       text="Session expired — sign in again.")
    return Verdict(False, status=401, code="no_session",
                   text="Authentication required.")


def _authorized(v: Verdict, method: str, path: str, ip: str) -> Verdict:
    """Apply the capability policy to an already-identified caller (Task 15).

    ⚠️ Every authenticated path funnels through here — session, bearer and trusted
    loopback alike. A capability check applied to two of the three is the shape of
    bug that makes a policy look enforced while one door stands open, and which of
    the three a given deployment uses is not something this function can know.

    The default role is `owner`, so this is a no-op on a normal install; it is the
    hardened deployments (`AGENT2_WEB_ROLE=viewer`, `AGENT2_DENY_CAPS=exec`) where
    it does the work.
    """
    cap = perms.capability_for(method, path)
    if perms.allowed(cap):
        perms.audit_use(cap, ok=True, what=f"{method} {path}", ip=ip)
        return v
    perms.audit_use(cap, ok=False, what=f"{method} {path}", ip=ip,
                    role=perms.role_name())
    return Verdict(False, status=403, code="forbidden",
                   text=perms.refusal(cap, what=f"{method} {path}"),
                   trusted=v.trusted)


def bearer_token(req) -> str:
    """Pull an access token out of the request without looking at cookies.

    Three spellings, all deliberate: `Authorization: Bearer …` for scripts, a
    bare `X-A2-Token` for clients that cannot set Authorization, and `?token=`
    for the one-click link the banner prints (a browser cannot be made to send a
    header by being clicked).
    """
    raw = (req.headers.get(TOKEN_HEADER) or "").strip()
    if raw.lower().startswith("bearer "):
        return raw[7:].strip()
    alt = (req.headers.get("X-A2-Token") or "").strip()
    if alt:
        return alt
    try:
        return (req.args.get(TOKEN_PARAM) or "").strip()
    except Exception:
        return ""


# ── Cookie plumbing ───────────────────────────────────────────────────────────
def _secure(req) -> bool:
    try:
        return bool(getattr(req, "is_secure", False))
    except Exception:
        return False


def attach_session(resp, req, sid: str, csrf: str, expires) -> None:
    """Set the cookie pair on a response.

    The session cookie is `HttpOnly` (script must not be able to read it); the
    CSRF cookie deliberately is NOT, because the front end has to read it to echo
    it back in a header. That asymmetry *is* the double-submit pattern — a
    readable session cookie would make XSS a session theft, and an unreadable
    CSRF cookie could never be echoed.

    `max_age` is derived from the row's own deadline rather than from
    `session_ttl()` again, so the browser stops presenting the cookie at the same
    moment the server stops honouring it. Two clocks computed separately drift the
    instant the TTL is changed while a session is live.
    """
    try:
        max_age = int((expires - _now()).total_seconds())
    except Exception:
        max_age = int(session_ttl())
    max_age = max(60, max_age)
    common = {"max_age": max_age, "samesite": "Lax",
              "secure": _secure(req), "path": "/"}
    resp.set_cookie(SESSION_COOKIE, sid, httponly=True, **common)
    resp.set_cookie(CSRF_COOKIE, csrf, httponly=False, **common)


def clear_session_cookies(resp) -> None:
    for name in (SESSION_COOKIE, CSRF_COOKIE):
        resp.set_cookie(name, "", max_age=0, expires=0, path="/")


# ── Socket.IO gate ────────────────────────────────────────────────────────────
def socket_allowed(req) -> bool:
    """Whether a Socket.IO handshake may connect.

    ⚠️ This exists because the handshake never reaches Flask's `before_request` —
    engineio's middleware wraps the WSGI app from outside. `run_raw_command` is a
    socket event, so a guard on `/api/*` alone protects the endpoints that read
    state and leaves the one that executes shell commands open.

    A WebSocket upgrade is also exempt from CORS by design, so `Origin` is checked
    here even though the handshake is a GET.

    ⚠️ AND SO IS `Host`, FOR THE SAME REASON — `decide()`'s step 2 is not reached
    here either. Without it a DNS-rebound page passed this gate (measured: `True`),
    which is the `run_raw_command` door: remote code execution from a visit. Both
    surfaces ask `host_ok()` or only one of them is defended, and the undefended one
    is the one that runs shell commands.
    """
    try:
        if mode() == MODE_OFF:
            return True
        if not host_ok(req):
            return False
        origin = (req.headers.get("Origin") or "").strip().rstrip("/").lower()
        if origin and origin != "null" and origin not in _allowed_origins(req):
            return False
        if origin == "null":
            return False
        ip = client_ip(req)
        cookie = (req.cookies.get(SESSION_COOKIE) or "").strip()
        if cookie and lookup_session(cookie):
            return True
        bearer = bearer_token(req)
        if bearer and token_matches(bearer):
            return True
        return is_loopback(ip) and mode() == MODE_AUTO
    except Exception:
        # A gate that crashes must fail CLOSED. Every other module in this repo
        # degrades to "do nothing"; here "do nothing" would mean "let it in".
        return False


# ── The login page ────────────────────────────────────────────────────────────
_LOGIN_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Agent 2 — sign in</title>
<style>
 :root{color-scheme:dark}
 *{box-sizing:border-box}
 body{margin:0;min-height:100vh;display:grid;place-items:center;
   background:#0b0b12;color:#e6e6f0;
   font:14px/1.55 ui-sans-serif,system-ui,'Segoe UI',Roboto,sans-serif}
 .card{width:min(420px,92vw);padding:26px 24px;border-radius:14px;
   background:#12121c;border:1px solid #23233a;
   box-shadow:0 18px 50px rgba(0,0,0,.45)}
 h1{margin:0 0 4px;font-size:17px;letter-spacing:.2px}
 p{margin:0 0 18px;color:#8f8fae;font-size:12.5px}
 label{display:block;margin-bottom:6px;font-size:11px;letter-spacing:.6px;
   text-transform:uppercase;color:#8f8fae}
 input{width:100%;padding:11px 12px;border-radius:9px;background:#0b0b12;
   border:1px solid #2b2b45;color:#e6e6f0;
   font:13px ui-monospace,'JetBrains Mono',Consolas,monospace}
 input:focus{outline:none;border-color:#3b82f6}
 button{width:100%;margin-top:12px;padding:11px;border:0;border-radius:9px;
   background:#3b82f6;color:#fff;font-weight:600;font-size:13px;cursor:pointer}
 button:disabled{opacity:.55;cursor:default}
 .err{margin-top:12px;padding:9px 11px;border-radius:8px;display:none;
   background:rgba(255,85,85,.12);border:1px solid rgba(255,85,85,.35);
   color:#ff9b9b;font-size:12px}
 .hint{margin-top:16px;padding-top:14px;border-top:1px solid #23233a;
   color:#6f6f8c;font-size:11.5px;line-height:1.65}
 code{background:#0b0b12;border:1px solid #23233a;border-radius:5px;
   padding:1px 5px;font-size:11px;color:#9ecbff}
</style></head><body>
<form class="card" id="f" autocomplete="off">
  <h1>Agent 2</h1>
  <p>This server is reachable beyond this machine, so it needs the access token.</p>
  <label for="t">Access token</label>
  <input id="t" name="token" type="password" spellcheck="false" autofocus
         placeholder="paste the token from the startup banner">
  <button id="b" type="submit">Sign in</button>
  <div class="err" id="e"></div>
  <div class="hint">
    The token is printed in the terminal that started Agent&nbsp;2
    (also visible with <code>docker logs</code>). It is regenerated on every
    restart unless you set <code>AGENT2_WEB_TOKEN</code>.
  </div>
</form>
<script>
const f=document.getElementById('f'),b=document.getElementById('b'),
      e=document.getElementById('e'),t=document.getElementById('t');
f.addEventListener('submit',async ev=>{
  ev.preventDefault(); e.style.display='none'; b.disabled=true;
  b.textContent='Signing in…';
  try{
    const r=await fetch('/api/auth/login',{method:'POST',
      headers:{'Content-Type':'application/json'},
      credentials:'same-origin',
      body:JSON.stringify({token:t.value.trim()})});
    const d=await r.json().catch(()=>({}));
    if(r.ok&&d.ok){ location.replace('/'); return; }
    e.textContent=d.error||('Sign-in failed ('+r.status+')');
    e.style.display='block';
  }catch(err){ e.textContent='Could not reach the server.'; e.style.display='block'; }
  b.disabled=false; b.textContent='Sign in';
});
</script></body></html>"""


def login_page() -> str:
    """The HTML served at `/` when the caller has no session."""
    return _LOGIN_HTML


# ── Banner ────────────────────────────────────────────────────────────────────
def banner_note(host: str, port: int) -> list[tuple[str, str]]:
    """`(kind, text)` lines for the startup banner. `kind` ∈ ok/warn/info.

    Built here, printed by `weblog.startup_banner`, because what the banner must
    say is a property of the auth model — the banner used to warn "no auth" as a
    hard-coded string, and a warning that cannot go stale is one that gets
    printed after the hole is closed.
    """
    m = mode()
    if m == MODE_OFF:
        return [("warn", "Web auth is OFF (AGENT2_WEB_AUTH=off) — "
                         "every route and socket event is anonymous")]
    local_only = str(host) in ("127.0.0.1", "localhost", "::1")
    lines: list[tuple[str, str]] = []
    if m == MODE_ALWAYS:
        lines.append(("ok", "Web auth: token required for every client"))
    else:
        lines.append(("ok", "Web auth: this machine is trusted, "
                            "other hosts need the token"))
    if m == MODE_ALWAYS or not local_only:
        tok = access_token()
        lines.append(("info", f"Access token  {tok}"))
        lines.append(("info", f"One-click     http://127.0.0.1:{port}/?token={tok}"))
        if not token_is_from_env():
            lines.append(("info", "New token on every restart — "
                                  "set AGENT2_WEB_TOKEN to pin it"))
    return lines


# ── Installation ──────────────────────────────────────────────────────────────
def install(app) -> None:
    """Attach the guard, the auth routes and the security headers to *app*.

    Called from `register_routes()` so the guard cannot be forgotten: an app that
    has the API has the guard, in one step, with no second call site to keep in
    sync. Idempotent — installing twice would double every hook.
    """
    if getattr(app, "_a2_auth_installed", False):
        return
    app._a2_auth_installed = True

    from flask import Response, g, jsonify, redirect, request

    def _denied(req, v: Verdict):
        """Render a refusal. HTML for a browser landing on `/`, JSON otherwise."""
        wants_html = req.path == "/" or (
            "text/html" in (req.headers.get("Accept") or "")
            and not req.path.startswith("/api/"))
        if wants_html and v.code in ("no_session", "expired", "bad_token"):
            # 200, not 401: this IS the page the caller should see, and a 401 on
            # the shell makes the browser's own auth dialog appear over it.
            return Response(login_page(), mimetype="text/html")
        body = {"ok": False, "error": v.text, "code": v.code}
        if v.code in ("no_session", "expired", "bad_token"):
            body["login"] = "/api/auth/login"
        resp = jsonify(body)
        resp.status_code = v.status
        if v.retry_after:
            resp.headers["Retry-After"] = str(v.retry_after)
        return resp

    @app.before_request
    def _a2_guard():
        v = decide(request)
        g.a2_auth = v
        if not v.ok:
            return _denied(request, v)
        # A `?token=…` link is the only way to authenticate a browser by being
        # clicked, and it leaves the token in history, in the address bar and in
        # any Referer the page later sends. So it is spent immediately: set the
        # cookies, then redirect to the same path without it.
        if v.code == "token" and request.method == "GET" and request.path == "/" \
                and (request.args.get(TOKEN_PARAM) or "").strip():
            resp = redirect("/")
            sid, csrf, exp = issue_session(
                client_ip(request), request.headers.get("User-Agent", ""))
            attach_session(resp, request, sid, csrf, exp)
            purge_expired()
            audit.event("auth.session.open", ip=client_ip(request), via="link")
            g.a2_auth = Verdict(True, code="issued")
            return resp
        return None

    @app.after_request
    def _a2_finish(resp):
        v = getattr(g, "a2_auth", None)
        try:
            _apply_cookies(resp, request, v)
        except Exception:
            pass
        # Cheap, non-negotiable headers. `setdefault` so a route that knows
        # better keeps its own value.
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        return resp

    def _apply_cookies(resp, req, v: Verdict | None) -> None:
        """Issue or rotate the cookie pair as a side effect of a served request.

        Issuing happens here rather than in a route because `/` lives in
        `agent2web.py` while the decision lives here; a route that had to
        remember to call an issuer is a route that will forget.
        """
        if v is None or not v.ok:
            return
        if v.session is not None:
            touch_session(v.session_value)
            if needs_rotation(v.session):
                revoke_session(v.session_value)
                sid, csrf, exp = issue_session(
                    client_ip(req), req.headers.get("User-Agent", ""),
                    rotated_from=str(v.session.get("id") or ""))
                attach_session(resp, req, sid, csrf, exp)
                audit.event("auth.session.rotate", ip=client_ip(req))
            return
        if not v.may_issue:
            return
        # Only the app shell earns a fresh session: an API response has no way to
        # tell the caller a login happened, and minting one per anonymous poll
        # would fill the table.
        if "text/html" not in (resp.mimetype or ""):
            return
        # ⚠️ And only a SUCCESSFUL shell. Flask renders its own 404 and 500 pages
        # as `text/html`, so without this a request for a path that does not exist
        # mints a session row — the table fills from scans and typos, and, worse,
        # the caller silently acquires an ambient credential it never asked for.
        # That last part is not theoretical: it is why `GET /api/burp` (404, HTML)
        # followed by `POST /api/burp` stopped being a 404 and became a CSRF 403,
        # since the POST now arrived cookie-authenticated. A guard that changes an
        # unrelated route's status code is a guard nobody can reason about.
        if not (200 <= int(getattr(resp, "status_code", 200) or 200) < 400):
            return
        sid, csrf, exp = issue_session(client_ip(req),
                                       req.headers.get("User-Agent", ""))
        attach_session(resp, req, sid, csrf, exp)
        purge_expired()
        audit.event("auth.session.open", ip=client_ip(req), via=v.code)

    # ── Auth endpoints ────────────────────────────────────────────────────────

    @app.route("/api/auth/status", methods=["GET"])
    def api_auth_status():
        """What the front end needs to know, and nothing more.

        ⚠️ Never carries the token, and never says whether a *guessed* token was
        close. It is also the one authenticated-state endpoint an anonymous LAN
        client can reach, so an unauthenticated caller learns only whether a token
        is needed — session counts and the token's provenance are operational
        detail and stay behind the guard.
        """
        v = getattr(g, "a2_auth", None)
        cookie = (request.cookies.get(SESSION_COOKIE) or "").strip()
        row = lookup_session(cookie) if cookie else None
        trusted = bool(v.trusted) if v else False
        authed = bool(row) or trusted
        body = {
            "mode": mode(),
            "authenticated": authed,
            "token_required": mode() != MODE_OFF and not authed,
            "csrf_header": CSRF_HEADER,
        }
        if authed:
            body.update({
                "session": bool(row),
                "trusted_client": trusted,
                "token_from_env": token_is_from_env(),
                "session_ttl": session_ttl(),
                "sessions": session_count(),
                # Task 15: what this client may DO, so the UI can grey out a
                # control instead of offering it and collecting a 403. Sent only
                # to an authenticated caller — the capability list describes the
                # deployment's posture and is not a stranger's business.
                "role": perms.role_name(),
                "capabilities": sorted(perms.caps_for()),
            })
        return jsonify(body)

    @app.route("/api/auth/login", methods=["POST"])
    def api_auth_login():
        """Exchange the access token for a session.

        Rate-limited on its own, much harder than the global limit: this is the
        one endpoint where guessing is the attack.
        """
        ip = client_ip(request)
        wait = rate_check("login", ip, login_limit(), LOGIN_WINDOW)
        if wait:
            audit.event("auth.login.throttled", ip=ip)
            resp = jsonify({"ok": False, "code": "rate_limited",
                            "error": "Too many attempts — wait a moment."})
            resp.status_code = 429
            resp.headers["Retry-After"] = str(wait)
            return resp

        if not origin_ok(request):
            return jsonify({"ok": False, "code": "bad_origin",
                            "error": "Cross-origin request refused."}), 403

        data = request.get_json(silent=True) or {}
        supplied = str(data.get("token") or "").strip() or bearer_token(request)
        if not token_matches(supplied):
            audit.event("auth.login.fail", ip=ip)
            return jsonify({"ok": False, "code": "bad_token",
                            "error": "Invalid access token."}), 401

        # A successful login always mints a NEW session and drops whatever the
        # caller presented — session fixation is exactly the bug that skipping
        # this step creates.
        #
        # ⚠️ Which is why a failed revoke fails the LOGIN. `revoke_session` raises
        # (see its docstring); minting a fresh session anyway would leave the
        # attacker-planted cookie alive alongside the new one, i.e. it would ship
        # the fixation hole this block exists to close.
        old = (request.cookies.get(SESSION_COOKIE) or "").strip()
        if old:
            try:
                revoke_session(old)
            except Exception as exc:
                audit.event("auth.login.error", ip=ip, error=str(exc)[:200])
                return jsonify({
                    "ok": False, "code": "store_error",
                    "error": "Could not retire the previous session — not signing in.",
                }), 503
        sid, csrf, exp = issue_session(ip, request.headers.get("User-Agent", ""))
        purge_expired()
        audit.event("auth.login.ok", ip=ip)
        resp = jsonify({"ok": True, "expires_at": _stamp(exp),
                        "csrf_header": CSRF_HEADER})
        attach_session(resp, request, sid, csrf, exp)
        return resp

    @app.route("/api/auth/logout", methods=["POST"])
    def api_auth_logout():
        """Revoke this session server-side, then clear the cookies.

        Both halves are required: clearing cookies alone leaves a live row that a
        copied cookie could still use, and deleting the row alone leaves the
        browser presenting a value that now looks merely expired.

        ⚠️ A failed revoke is reported as a failure — `ok: False`, 503 — and the
        cookies are cleared regardless. Clearing is a pure reduction in access, so
        it is always safe to do; claiming `ok: True` over a row that survived is
        what would be unsafe, because "signed out" is a promise about the server,
        not about this browser.
        """
        cookie = (request.cookies.get(SESSION_COOKIE) or "").strip()
        failed = ""
        if cookie:
            try:
                revoke_session(cookie)
                audit.event("auth.logout", ip=client_ip(request))
            except Exception as exc:
                failed = str(exc)[:200]
                audit.event("auth.logout.error", ip=client_ip(request), error=failed)
        if failed:
            resp = jsonify({"ok": False, "code": "store_error",
                            "error": "Cookies cleared, but the session record "
                                     "could not be deleted."})
            resp.status_code = 503
        else:
            resp = jsonify({"ok": True})
        clear_session_cookies(resp)
        return resp

    @app.route("/api/auth/rotate", methods=["POST"])
    def api_auth_rotate():
        """Mint a new access token and sign every client out.

        The new token is returned exactly once, to the caller that asked. It is
        not stored, so there is no second chance to read it — which is the same
        contract the startup banner has.

        A failure here changes nothing at all: `rotate_token` revokes before it
        swaps, so the old token still works and the caller can retry.
        """
        try:
            fresh = rotate_token()
        except Exception as exc:
            audit.event("auth.token.rotate.error", error=str(exc)[:200])
            return jsonify({
                "ok": False, "code": "store_error",
                "error": "Could not revoke live sessions — the token is unchanged.",
            }), 503
        resp = jsonify({"ok": True, "token": fresh,
                        "note": "every session was revoked"})
        clear_session_cookies(resp)
        return resp
