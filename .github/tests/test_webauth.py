# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""Tests for the web authorization guard (agent2/server/auth.py — Task 14).

What makes this suite worth reading rather than skimming: almost every assertion
here is about a *refusal*, and a refusal is the one behaviour that looks identical
whether the guard is working or absent-but-lucky. So each test states the request
an attacker actually sends, not merely the flag it sets.

Four properties carry the whole feature, and each is pinned in both directions —
the thing that must be refused, and the thing that must keep working, because a
guard that breaks local development is a guard that gets turned off:

1. **Remote clients need the token.** `AGENT2_HOST` defaults to `0.0.0.0` and
   `run_raw_command` runs a shell, so "anything that can route to the box" was
   remote code execution.
2. **Loopback stays convenient** in the default `auto` mode — and *only* in
   `auto`, since `always` exists for shared machines.
3. **The socket handshake is gated separately**, because it never reaches Flask's
   `before_request` at all. That asymmetry is invisible from either half, which
   is exactly why it gets its own tests.
4. **Nothing about the credential reaches disk.** `web_sessions` holds digests;
   the access token is never written anywhere.
"""

import os
import time

import pytest
from flask import Flask

from agent2 import database as db
from agent2.server import auth
from agent2.server.routes import register_routes

REMOTE = "203.0.113.9"          # TEST-NET-3, guaranteed non-loopback


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean_auth_state():
    """Reset every piece of process-global auth state around each test.

    The token, the rate limiter and the session table all outlive a request by
    design, so without this a test would inherit the previous one's sessions and
    the rate limiter would leak hits across the file. Restoring the env is what
    lets one test flip `AGENT2_WEB_AUTH=always` without infecting the rest.
    """
    saved = {k: os.environ.get(k) for k in (
        "AGENT2_WEB_AUTH", "AGENT2_WEB_TOKEN", "AGENT2_WEB_ORIGINS",
        "AGENT2_WEB_RATE_LIMIT", "AGENT2_WEB_LOGIN_LIMIT",
        "AGENT2_WEB_SESSION_TTL", "AGENT2_WEB_SESSION_ROTATE")}
    for k in saved:
        os.environ.pop(k, None)
    db.init_db()
    auth.rate_reset()
    auth.revoke_all()
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    auth.rate_reset()
    try:
        auth.revoke_all()
    except Exception:
        pass


@pytest.fixture
def app():
    app = Flask(__name__)
    register_routes(app)

    @app.route("/")
    def _index():
        # Stands in for agent2web.py's shell: same mimetype, same path, so the
        # cookie-issuing and login-page paths are exercised as they ship.
        from flask import Response
        return Response("<html>app</html>", mimetype="text/html")

    return app


@pytest.fixture
def client(app):
    with app.test_client() as c:
        yield c


def _remote(client, method, path, **kw):
    """Issue a request that arrives from off-box.

    Werkzeug's test client lets `environ_base` set REMOTE_ADDR, which is the one
    input the whole trust model turns on.
    """
    kw.setdefault("environ_base", {"REMOTE_ADDR": REMOTE})
    return getattr(client, method)(path, **kw)


# ══════════════════════════════════════════════════════════════════════════════
# Authentication — who gets in at all
# ══════════════════════════════════════════════════════════════════════════════

def test_a_remote_client_cannot_read_the_api_without_a_token(client):
    """The headline hole: before Task 14 this returned 200 to the whole LAN."""
    resp = _remote(client, "get", "/api/chats")
    assert resp.status_code == 401
    assert resp.get_json()["code"] == "no_session"


def test_a_remote_client_cannot_run_the_agent_without_a_token(client):
    """A POST is the dangerous half, and it is refused before it reaches a route."""
    resp = _remote(client, "post", "/api/memories", json={"content": "x"})
    assert resp.status_code in (401, 403)
    assert db.qall("SELECT * FROM memories WHERE content='x'") == []


def test_loopback_keeps_working_with_no_setup_at_all(client):
    """Rule: local development must stay convenient or the guard gets disabled."""
    assert client.get("/api/chats").status_code == 200
    assert client.get("/").status_code == 200


def test_always_mode_locks_loopback_too(client):
    """A shared machine has other local users; `auto` cannot help there."""
    os.environ["AGENT2_WEB_AUTH"] = "always"
    assert client.get("/api/chats").status_code == 401


def test_off_mode_is_the_only_way_to_disable_the_guard_and_it_is_env_only(client):
    """⚠️ There is no DB setting for this, and that is deliberate.

    A stored "off" is a persistent silent downgrade — something a compromised DB
    (or a stray UI toggle) could set for you and nothing would ever announce. The
    test pins the negative half too: no `settings` row can turn the guard off.
    """
    os.environ["AGENT2_WEB_AUTH"] = "off"
    assert _remote(client, "get", "/api/chats").status_code == 200

    os.environ["AGENT2_WEB_AUTH"] = "auto"
    db.exe("INSERT OR REPLACE INTO settings (key, value) VALUES ('web_auth','off')")
    assert _remote(client, "get", "/api/chats").status_code == 401


def test_an_unknown_auth_mode_falls_back_to_auto_not_to_off(client):
    """A typo in an env var must never open the server."""
    os.environ["AGENT2_WEB_AUTH"] = "yolo"
    assert auth.mode() == auth.MODE_AUTO
    assert _remote(client, "get", "/api/chats").status_code == 401


def test_the_bearer_token_admits_a_remote_client(client):
    os.environ["AGENT2_WEB_TOKEN"] = "tok-abc-123"
    resp = _remote(client, "get", "/api/chats",
                   headers={"Authorization": "Bearer tok-abc-123"})
    assert resp.status_code == 200


def test_a_wrong_token_is_401_and_is_audited_not_merely_ignored(client, caplog):
    os.environ["AGENT2_WEB_TOKEN"] = "tok-abc-123"
    with caplog.at_level("INFO", logger="agent2"):
        resp = _remote(client, "get", "/api/chats",
                       headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401
    assert resp.get_json()["code"] == "bad_token"
    assert any("auth.deny" in r.getMessage() for r in caplog.records)


def test_the_token_is_compared_in_constant_time(client):
    """`token_matches` must use hmac.compare_digest, not `==`.

    Sabotage note: swapping in `==` keeps every other test in this file green.
    Reading the source is the only way to catch it, so the test reads the source.
    """
    import inspect
    src = inspect.getsource(auth.token_matches)
    assert "compare_digest" in src
    assert auth.token_matches("") is False


def test_login_exchanges_the_token_for_a_session_cookie(client):
    os.environ["AGENT2_WEB_TOKEN"] = "tok-abc-123"
    resp = _remote(client, "post", "/api/auth/login", json={"token": "tok-abc-123"})
    assert resp.status_code == 200 and resp.get_json()["ok"] is True
    assert client.get_cookie(auth.SESSION_COOKIE) is not None
    # …and the session is what admits the next request, with no token in hand.
    assert _remote(client, "get", "/api/chats").status_code == 200


def test_login_with_a_bad_token_leaves_no_session_behind(client):
    os.environ["AGENT2_WEB_TOKEN"] = "tok-abc-123"
    before = auth.session_count()
    resp = _remote(client, "post", "/api/auth/login", json={"token": "nope"})
    assert resp.status_code == 401
    assert auth.session_count() == before


def test_login_rotates_a_supplied_session_so_fixation_cannot_work(client):
    """⚠️ The attack: plant a cookie value, get the victim to sign in, reuse it.

    A login that keeps the presented session id hands the attacker a now-authenticated
    session. So login always mints a new one and revokes what arrived.
    """
    os.environ["AGENT2_WEB_TOKEN"] = "tok-abc-123"
    planted, _csrf, _exp = auth.issue_session(ip=REMOTE)
    client.set_cookie(auth.SESSION_COOKIE, planted, domain="localhost")

    _remote(client, "post", "/api/auth/login", json={"token": "tok-abc-123"})

    assert auth.lookup_session(planted) is None, "the planted session survived login"
    assert client.get_cookie(auth.SESSION_COOKIE).value != planted


# ══════════════════════════════════════════════════════════════════════════════
# The access token itself
# ══════════════════════════════════════════════════════════════════════════════

def test_the_access_token_is_never_written_to_the_database():
    """⚠️ The property that makes a copied agent2.db worthless.

    A backup, a mounted Docker volume and a screen-shared `sqlite3` session are
    all "someone read the DB". None of them may yield a credential.
    """
    tok = auth.access_token()
    assert tok
    auth.issue_session(ip="127.0.0.1")
    for (table,) in [(r["name"],) for r in db.qall(
            "SELECT name FROM sqlite_master WHERE type='table'")]:
        for row in db.qall(f"SELECT * FROM {table}"):        # noqa: S608 - test only
            assert tok not in " ".join(str(v) for v in dict(row).values()), table


def test_web_sessions_stores_digests_not_cookie_values():
    sid, csrf, _exp = auth.issue_session(ip="127.0.0.1")
    rows = db.qall("SELECT * FROM web_sessions")
    blob = " ".join(str(v) for r in rows for v in dict(r).values())
    assert sid not in blob and csrf not in blob
    assert auth.lookup_session(sid) is not None, "the digest lookup must still work"


def test_rotating_the_token_revokes_every_live_session():
    """Rotation that left sessions alive would change the door key and leave the
    people already inside — the opposite of what rotation is asked for."""
    auth.issue_session(ip="127.0.0.1")
    auth.issue_session(ip=REMOTE)
    assert auth.session_count() == 2

    old = auth.access_token()
    fresh = auth.rotate_token()

    assert fresh != old
    assert auth.session_count() == 0
    assert auth.token_matches(old) is False
    assert auth.token_matches(fresh) is True


def test_a_failed_revoke_leaves_the_token_unchanged(monkeypatch):
    """⚠️ Order pin: revoke first, then swap.

    Swapping first and then failing to revoke is the worst of both — the token the
    user was told to stop using is gone, and every session it minted is still
    valid. Done in this order, a failure changes nothing at all.
    """
    before = auth.access_token()
    monkeypatch.setattr(auth, "revoke_all",
                        lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    with pytest.raises(RuntimeError):
        auth.rotate_token()
    assert auth.access_token() == before


def test_the_rotate_endpoint_reports_a_store_failure_instead_of_lying(client, monkeypatch):
    monkeypatch.setattr(auth, "revoke_all",
                        lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    resp = client.post("/api/auth/rotate")
    assert resp.status_code == 503
    assert resp.get_json()["ok"] is False


# ══════════════════════════════════════════════════════════════════════════════
# CSRF + origin
# ══════════════════════════════════════════════════════════════════════════════

def test_a_cookie_authenticated_post_without_the_csrf_header_is_refused(client):
    os.environ["AGENT2_WEB_TOKEN"] = "tok-abc-123"
    _remote(client, "post", "/api/auth/login", json={"token": "tok-abc-123"})

    resp = _remote(client, "post", "/api/memories", json={"content": "csrf-probe"})
    assert resp.status_code == 403
    assert resp.get_json()["code"] == "csrf"


def test_the_same_post_succeeds_once_the_double_submit_pair_agrees(client):
    os.environ["AGENT2_WEB_TOKEN"] = "tok-abc-123"
    _remote(client, "post", "/api/auth/login", json={"token": "tok-abc-123"})
    csrf = client.get_cookie(auth.CSRF_COOKIE).value

    resp = _remote(client, "post", "/api/memories", json={"content": "csrf-ok"},
                   headers={auth.CSRF_HEADER: csrf})
    assert resp.status_code == 200


def test_a_forged_csrf_header_does_not_pass(client):
    """The header must match the cookie AND the stored digest — not just exist."""
    os.environ["AGENT2_WEB_TOKEN"] = "tok-abc-123"
    _remote(client, "post", "/api/auth/login", json={"token": "tok-abc-123"})
    resp = _remote(client, "post", "/api/memories", json={"content": "x"},
                   headers={auth.CSRF_HEADER: "made-up"})
    assert resp.status_code == 403


def test_csrf_is_not_demanded_of_a_bearer_caller(client):
    """A deliberate credential needs no second one, and demanding it breaks scripts."""
    os.environ["AGENT2_WEB_TOKEN"] = "tok-abc-123"
    resp = _remote(client, "post", "/api/memories", json={"content": "script"},
                   headers={"Authorization": "Bearer tok-abc-123"})
    assert resp.status_code == 200


def test_a_hostile_page_cannot_drive_the_local_server(client):
    """⚠️ The localhost-CSRF hole that loopback trust cannot see.

    Any page the user visits can `fetch('http://127.0.0.1:1311/api/…')` and the
    request genuinely arrives from 127.0.0.1 — trusted by IP, caused by evil.com.
    Origin is therefore checked *before* the question of who the caller is.
    """
    resp = client.post("/api/memories", json={"content": "drive-by"},
                       headers={"Origin": "http://evil.example",
                                "Sec-Fetch-Site": "cross-site"})
    assert resp.status_code == 403
    assert resp.get_json()["code"] == "bad_origin"
    assert db.qall("SELECT * FROM memories WHERE content='drive-by'") == []


def test_sec_fetch_site_alone_is_enough_to_refuse(client):
    """A `no-cors` form post can omit Origin; browsers still send Sec-Fetch-Site."""
    resp = client.post("/api/memories", json={"content": "x"},
                       headers={"Sec-Fetch-Site": "cross-site"})
    assert resp.status_code == 403


def test_a_null_origin_is_refused(client):
    """`Origin: null` is what a sandboxed iframe or a data: URL sends."""
    resp = client.post("/api/memories", json={"content": "x"},
                       headers={"Origin": "null"})
    assert resp.status_code == 403


def test_a_headerless_client_still_works(client):
    """curl and every existing test client send neither header — that distinction
    is the whole reason origin checking can be this strict."""
    assert client.post("/api/memories", json={"content": "curl"}).status_code == 200


def test_an_extra_origin_can_be_allowed_but_a_wildcard_cannot(client):
    """⚠️ `*` is dropped rather than merely inert.

    `origin_ok` does exact set membership, so a `*` entry already matches nothing.
    Discarding it at parse time is what stops a later reader from "fixing" the
    apparently-broken wildcard by teaching the comparison to glob.
    """
    os.environ["AGENT2_WEB_ORIGINS"] = "https://proxy.example"
    assert client.post("/api/memories", json={"content": "proxied"},
                       headers={"Origin": "https://proxy.example"}).status_code == 200

    os.environ["AGENT2_WEB_ORIGINS"] = "*"
    assert auth.extra_origins() == ()
    assert client.post("/api/memories", json={"content": "x"},
                       headers={"Origin": "http://evil.example"}).status_code == 403


def test_a_safe_method_is_not_origin_checked(client):
    """GET has no side effects and blocking it cross-origin buys nothing while
    breaking embeds; the browser's own SOP already hides the response body."""
    assert client.get("/api/chats",
                      headers={"Origin": "http://evil.example"}).status_code == 200


# ══════════════════════════════════════════════════════════════════════════════
# Sessions — expiry, rotation, logout
# ══════════════════════════════════════════════════════════════════════════════

def test_an_expired_session_is_refused_and_swept(client):
    os.environ["AGENT2_WEB_SESSION_TTL"] = "60"
    sid, _csrf, _exp = auth.issue_session(ip=REMOTE)
    db.exe("UPDATE web_sessions SET expires_at='2000-01-01 00:00:00'")

    assert auth.lookup_session(sid) is None
    assert db.qall("SELECT * FROM web_sessions") == [], "the dead row was not swept"

    client.set_cookie(auth.SESSION_COOKIE, sid, domain="localhost")
    resp = _remote(client, "get", "/api/chats")
    assert resp.status_code == 401
    assert resp.get_json()["code"] in ("expired", "no_session")


def test_an_authenticated_request_pushes_the_idle_deadline_forward(client):
    os.environ["AGENT2_WEB_TOKEN"] = "tok-abc-123"
    _remote(client, "post", "/api/auth/login", json={"token": "tok-abc-123"})
    sid = client.get_cookie(auth.SESSION_COOKIE).value

    db.exe("UPDATE web_sessions SET expires_at='2000-01-01 00:00:00' "
           "WHERE id=?", (auth._digest(sid),))
    # Re-issue a live deadline the way a request would, then confirm it moved.
    auth.touch_session(sid)
    row = db.qone("SELECT expires_at FROM web_sessions WHERE id=?", (auth._digest(sid),))
    assert row["expires_at"] > "2001-01-01 00:00:00"


def test_a_long_lived_session_is_re_issued_under_a_new_id(client):
    """Rotation keeps one browser tab from holding a single cookie value forever."""
    os.environ["AGENT2_WEB_TOKEN"] = "tok-abc-123"
    os.environ["AGENT2_WEB_SESSION_ROTATE"] = "60"
    _remote(client, "post", "/api/auth/login", json={"token": "tok-abc-123"})
    first = client.get_cookie(auth.SESSION_COOKIE).value

    db.exe("UPDATE web_sessions SET created_at='2000-01-01 00:00:00'")
    _remote(client, "get", "/api/chats")

    second = client.get_cookie(auth.SESSION_COOKIE).value
    assert second != first, "the session was not rotated"
    assert auth.lookup_session(first) is None, "the old session id still works"
    assert auth.lookup_session(second) is not None


def test_logout_kills_the_row_and_the_cookies(client):
    """Both halves matter: clearing cookies alone leaves a replayable row."""
    os.environ["AGENT2_WEB_TOKEN"] = "tok-abc-123"
    _remote(client, "post", "/api/auth/login", json={"token": "tok-abc-123"})
    sid = client.get_cookie(auth.SESSION_COOKIE).value
    csrf = client.get_cookie(auth.CSRF_COOKIE).value

    resp = _remote(client, "post", "/api/auth/logout",
                   headers={auth.CSRF_HEADER: csrf})
    assert resp.status_code == 200
    assert auth.lookup_session(sid) is None
    left = client.get_cookie(auth.SESSION_COOKIE)
    assert left is None or not left.value, "the browser still holds a session cookie"


def test_logout_reports_a_failed_revoke_instead_of_claiming_success(client, monkeypatch):
    """⚠️ "Signed out" is a promise about the server, not about this browser."""
    os.environ["AGENT2_WEB_TOKEN"] = "tok-abc-123"
    _remote(client, "post", "/api/auth/login", json={"token": "tok-abc-123"})
    csrf = client.get_cookie(auth.CSRF_COOKIE).value
    monkeypatch.setattr(auth, "revoke_session",
                        lambda v: (_ for _ in ()).throw(RuntimeError("db down")))
    resp = _remote(client, "post", "/api/auth/logout",
                   headers={auth.CSRF_HEADER: csrf})
    assert resp.status_code == 503
    assert resp.get_json()["ok"] is False


def test_the_session_cookie_is_httponly_and_the_csrf_cookie_is_not(client):
    """That asymmetry IS the double-submit pattern.

    A readable session cookie turns XSS into session theft; an unreadable CSRF
    cookie could never be echoed back in a header.
    """
    os.environ["AGENT2_WEB_TOKEN"] = "tok-abc-123"
    resp = _remote(client, "post", "/api/auth/login", json={"token": "tok-abc-123"})
    setc = " | ".join(resp.headers.getlist("Set-Cookie"))
    session_line = next(c for c in resp.headers.getlist("Set-Cookie")
                        if c.startswith(auth.SESSION_COOKIE))
    csrf_line = next(c for c in resp.headers.getlist("Set-Cookie")
                     if c.startswith(auth.CSRF_COOKIE))
    assert "HttpOnly" in session_line, setc
    assert "HttpOnly" not in csrf_line, setc
    assert "SameSite=Lax" in session_line


# ══════════════════════════════════════════════════════════════════════════════
# Rate limiting
# ══════════════════════════════════════════════════════════════════════════════

def test_login_attempts_are_throttled_per_ip(client):
    os.environ["AGENT2_WEB_TOKEN"] = "tok-abc-123"
    os.environ["AGENT2_WEB_LOGIN_LIMIT"] = "3"
    codes = [_remote(client, "post", "/api/auth/login",
                     json={"token": "guess"}).status_code for _ in range(5)]
    assert codes[:3] == [401, 401, 401]
    assert 429 in codes[3:], codes


def test_a_throttled_response_says_how_long_to_wait(client):
    os.environ["AGENT2_WEB_RATE_LIMIT"] = "2"
    for _ in range(2):
        client.get("/api/chats")
    resp = client.get("/api/chats")
    assert resp.status_code == 429
    assert int(resp.headers["Retry-After"]) >= 1


def test_the_limiter_is_bounded_and_cannot_grow_without_limit():
    """An in-memory limiter keyed by client IP is a memory-exhaustion target."""
    for i in range(auth._RATE_KEYS_MAX + 50):
        auth.rate_check("probe", f"10.0.{i // 256}.{i % 256}", 100, 60)
    assert len(auth._RATE) <= auth._RATE_KEYS_MAX + 1


def test_rate_limit_zero_disables_it(client):
    os.environ["AGENT2_WEB_RATE_LIMIT"] = "0"
    assert all(client.get("/api/chats").status_code == 200 for _ in range(20))


# ══════════════════════════════════════════════════════════════════════════════
# The socket gate — the half that never reaches before_request
# ══════════════════════════════════════════════════════════════════════════════

class _Req:
    """The two attributes and one mapping `socket_allowed` actually reads."""

    def __init__(self, addr=REMOTE, headers=None, cookies=None):
        self.remote_addr = addr
        self.headers = headers or {}
        self.cookies = cookies or {}
        self.args = {}
        self.host = "localhost"


def test_a_remote_handshake_without_a_credential_is_refused():
    """⚠️ `run_raw_command` is a socket event. This is the gate that covers it."""
    assert auth.socket_allowed(_Req()) is False


def test_a_remote_handshake_with_the_token_connects():
    os.environ["AGENT2_WEB_TOKEN"] = "tok-abc-123"
    assert auth.socket_allowed(
        _Req(headers={"Authorization": "Bearer tok-abc-123"})) is True


def test_a_remote_handshake_with_a_live_session_cookie_connects():
    sid, _c, _e = auth.issue_session(ip=REMOTE)
    assert auth.socket_allowed(_Req(cookies={auth.SESSION_COOKIE: sid})) is True


def test_a_loopback_handshake_connects_in_auto_and_not_in_always():
    assert auth.socket_allowed(_Req(addr="127.0.0.1")) is True
    os.environ["AGENT2_WEB_AUTH"] = "always"
    assert auth.socket_allowed(_Req(addr="127.0.0.1")) is False


def test_a_cross_origin_handshake_is_refused_even_from_loopback():
    """A WebSocket upgrade is exempt from CORS by design, so the origin check has
    to happen here or a hostile page can open a socket to the local server."""
    assert auth.socket_allowed(
        _Req(addr="127.0.0.1", headers={"Origin": "http://evil.example"})) is False


def test_the_socket_gate_fails_closed_when_it_raises(monkeypatch):
    """⚠️ Every other module here degrades to "do nothing". Here "do nothing"
    would mean "let it in", so this one degrades to a refusal."""
    monkeypatch.setattr(auth, "mode",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert auth.socket_allowed(_Req(addr="127.0.0.1")) is False


def test_the_connect_handler_actually_calls_the_gate():
    """Pins the wiring, not just the helper.

    A perfect `socket_allowed` that nothing calls is the exact shape of this bug,
    and it is invisible from auth.py's own tests.
    """
    import inspect

    from agent2.server import sockets
    src = inspect.getsource(sockets.register_sockets)
    assert "socket_allowed" in src
    connect = src.split("def on_connect", 1)[1]
    gate = connect.index("socket_allowed")
    join = connect.index("join_room")
    assert gate < join, "the room is joined before the handshake is authorized"


# ══════════════════════════════════════════════════════════════════════════════
# Reachability of the public surface, and of nothing else
# ══════════════════════════════════════════════════════════════════════════════

def test_the_login_page_is_served_to_an_anonymous_browser_at_the_root(client):
    """200, not 401: this IS the page the caller should see, and a 401 makes the
    browser's own auth dialog appear over it."""
    resp = _remote(client, "get", "/", headers={"Accept": "text/html"})
    assert resp.status_code == 200
    assert "Access token" in resp.get_data(as_text=True)


def test_health_stays_reachable_from_loopback_for_the_docker_healthcheck(client):
    os.environ["AGENT2_WEB_AUTH"] = "always"
    assert client.get("/api/health").status_code in (200, 503)
    assert _remote(client, "get", "/api/health").status_code == 401


def test_auth_status_is_public_but_says_nothing_useful_to_a_stranger(client):
    body = _remote(client, "get", "/api/auth/status").get_json()
    assert body["token_required"] is True
    assert "sessions" not in body and "token_from_env" not in body
    assert auth.access_token() not in str(body)


def test_security_headers_are_on_every_response(client):
    resp = client.get("/api/chats")
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"


def test_a_404_does_not_mint_a_session(client):
    """⚠️ Flask renders 404 as text/html, so the cookie-issuing path sees it.

    Minting there fills the table from scans and typos, and hands the caller an
    ambient credential it never asked for — which then makes the *next* request to
    an unrelated route fail CSRF instead of 404ing. Regression pin: this is the
    bug that broke test_mcp's "bespoke burp routes are gone".
    """
    before = auth.session_count()
    assert client.get("/api/burp").status_code == 404
    assert auth.session_count() == before
    assert client.post("/api/burp").status_code == 404


def test_the_guard_is_installed_by_register_routes_not_by_a_decorator():
    """⚠️ A decorator is opt-in, so the failure mode is a new route with no guard.

    One `before_request` makes that impossible; this test is what keeps it one.
    """
    import inspect

    from agent2.server import routes
    src = inspect.getsource(routes.register_routes)
    assert "auth.install(app)" in src


def test_installing_twice_does_not_double_the_hooks(app):
    """`register_routes` is called once per app, but nothing enforces that, and a
    doubled `before_request` would decide (and rate-limit) every request twice."""
    before = len(app.before_request_funcs.get(None, []))
    auth.install(app)
    assert len(app.before_request_funcs.get(None, [])) == before


def test_a_forwarded_header_cannot_buy_loopback_trust(client):
    """⚠️ `X-Forwarded-For` is client-controlled. Trusting it would let anyone
    claim 127.0.0.1 — the header would *become* the authentication bypass."""
    resp = _remote(client, "get", "/api/chats",
                   headers={"X-Forwarded-For": "127.0.0.1",
                            "X-Real-IP": "127.0.0.1"})
    assert resp.status_code == 401


@pytest.mark.parametrize("addr,expected", [
    ("127.0.0.1", True), ("127.5.6.7", True), ("::1", True),
    ("[::1]", True), ("::ffff:127.0.0.1", True), ("localhost", True),
    ("0.0.0.0", False), ("10.0.0.1", False), ("", False), (None, False),
    ("127.0.0.1.evil.com", False),
])
def test_loopback_detection(addr, expected):
    """The one predicate the whole `auto` mode turns on.

    `::ffff:127.0.0.1` is what a dual-stack listener reports for a v4 loopback
    client, and `127.0.0.1.evil.com` is what a hostname-parsing shortcut accepts.
    """
    assert auth.is_loopback(addr) is expected
