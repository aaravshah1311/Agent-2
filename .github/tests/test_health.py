# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""Tests for GET /api/health (agent2/server/routes.py).

The endpoint exists to be *monitorable*, which puts three properties under test
that are easy to get wrong and impossible to notice in normal use:

1. **It must not cry wolf.** A disabled optimization and an idle-but-never-used
   worker pool are both supported configurations. An alert that fires on those
   is one people learn to ignore, so `ok` must stay true for them.
2. **It must not fail as a unit.** One raising counter degrades to its own error
   string; the other sections still report.
3. **It must leak nothing.** There is no auth on any route, so the payload is
   counters only — no key material, no chat/memory text, not even the DB path.
"""

import json

import pytest
from flask import Flask

from agent2 import database as db
from agent2.core import scheduler
from agent2.server.routes import register_routes


@pytest.fixture
def client():
    """A Flask test client on a fully-migrated DB."""
    db.init_db()
    app = Flask(__name__)
    register_routes(app)
    with app.test_client() as c:
        yield c


def _health(client):
    resp = client.get("/api/health")
    return resp.status_code, resp.get_json()


# ── The healthy path ─────────────────────────────────────────────────────────

def test_idle_server_is_healthy(client):
    """No turns submitted yet is the NORMAL state, not a fault.

    ⚠️ Regression pin. The first version of this check flagged
    "enabled but no live workers", which is true of every server that has not
    yet handled a turn — scheduler.submit() starts the workers lazily. It
    returned 503 on a completely healthy app.
    """
    status, body = _health(client)

    assert status == 200
    assert body["ok"] is True
    assert body["problems"] == []

    sch = body["scheduler"]
    assert sch["enabled"] is True
    assert sch["workers"] == 0          # lazily started — the state under test
    assert sch["worker_starts"] == 0


def test_every_section_is_present(client):
    _, body = _health(client)
    for section in ("db", "pool", "wal", "scheduler", "sync", "mcp"):
        assert section in body, f"missing health section: {section}"
    assert body["db"]["reachable"] is True
    assert body["db"]["schema_version"] == body["db"]["schema_expected"]


def test_disabled_wal_checkpointer_is_not_unhealthy(client, monkeypatch):
    """`AGENT2_WAL_CHECKPOINT_SEC=0` is a supported configuration."""
    monkeypatch.setattr(db, "wal_stats",
                        lambda: {"running": False, "interval_sec": 0.0,
                                 "runs": 0, "errors": 0, "wal_bytes": 0})
    status, body = _health(client)

    assert status == 200
    assert body["ok"] is True
    assert body["wal"]["running"] is False


def test_disabled_scheduler_is_not_unhealthy(client, monkeypatch):
    """`AGENT2_MAX_CONCURRENT_TURNS=0` falls back to a direct thread, by design."""
    monkeypatch.setattr(scheduler, "stats",
                        lambda: {"enabled": False, "workers": 0,
                                 "worker_starts": 0, "queued": 0,
                                 "max_queue": 64, "submitted": 0})
    status, body = _health(client)

    assert status == 200
    assert body["ok"] is True


# ── Task 10: the MCP section ──────────────────────────────────────────────────

@pytest.fixture
def zap():
    """The ZAP bridge, restored afterwards — it is a process-global singleton.

    ⚠️ `enabled`, `url` and `key` are read-through PROPERTIES whose setters write
    to the database, so nothing here is restored by assigning to the bridge: the
    two tables are put back and the state cache dropped, the same contract
    `test_mcp.py::_clean_bridges` documents at length.
    """
    from agent2.integrations import state as S
    from agent2.integrations.zap_mcp import zap as bridge

    rows = db.qall("SELECT project, server, enabled FROM mcp_state", ())
    cfg = db.qall("SELECT server, url, security_key FROM mcp_config", ())
    yield bridge
    bridge._session = None
    bridge._loop = None
    bridge._tools = []
    bridge._connected_url = ""
    bridge._last_error = ""
    db.exe("DELETE FROM mcp_state")
    for r in rows:
        db.exe("INSERT INTO mcp_state(project, server, enabled) VALUES(?,?,?)",
               (r["project"], r["server"], r["enabled"]))
    db.exe("DELETE FROM mcp_config")
    for r in cfg:
        db.exe("INSERT INTO mcp_config(server, url, security_key) VALUES(?,?,?)",
               (r["server"], r["url"], r["security_key"]))
    S.invalidate()


def _connect(bridge, tools=("scan",)):
    """The state `is_connected()` reports as connected — two sentinels, no socket."""
    bridge._session = object()
    bridge._loop = object()
    bridge._tools = [{"real_name": t, "name": f"zap_{t}"} for t in tools]
    bridge._connected_url = bridge.url


def test_a_disconnected_never_enabled_mcp_server_is_not_a_fault(client):
    """⚠️ THE default install: both bridges ship auto-connect OFF.

    A fresh checkout has two disconnected servers, and reporting that as 503
    would be an alert that fires on the default configuration — the exact cry
    wolf the endpoint exists not to raise. `/api/health` must stay 200 until a
    server the user ENABLED is known to be broken.
    """
    status, body = _health(client)

    assert status == 200
    assert body["ok"] is True
    mcp = body["mcp"]
    assert mcp["total"] >= 1
    assert mcp["failing"] == 0
    assert all(s["ok"] is True for s in mcp["servers"])


def test_an_enabled_server_that_never_connected_is_idle_not_a_fault(client, zap):
    """DUAL MODE: the CLI child may hold the live session while the web process —
    the one serving /api/health — has never dialled. "Enabled, no session, no
    error recorded" means *we have not tried here yet*, and the next agent turn
    resolves it into connected or failed. Reporting a fault for it would make
    every dual-mode install permanently unhealthy.
    """
    if not zap.status().get("mcp_installed"):
        pytest.skip("the mcp package is not installed in this environment")
    zap.set_auto_connect(True)               # enabled … but nobody has dialled
    zap._last_error = ""
    status, body = _health(client)

    assert status == 200
    assert body["ok"] is True
    row = next(s for s in body["mcp"]["servers"] if s["key"] == "zap")
    assert row["state"] == "idle"
    assert row["enabled"] is True
    assert row["ok"] is True


def test_a_disabled_server_with_a_lingering_error_is_not_a_fault(client, zap):
    """⚠️ Rule 21, health-shaped: `last_error` records the MOST RECENT attempt,
    because `connect()` clears it first — but a server the user did not enable
    has nothing scheduled to happen. That error is history, not a condition a
    monitor should page someone about at 3am.
    """
    zap.set_auto_connect(False)
    zap._last_error = "connection refused by 127.0.0.1:9999"

    status, body = _health(client)

    assert status == 200
    assert body["ok"] is True
    row = next(s for s in body["mcp"]["servers"] if s["key"] == "zap")
    assert row["state"] == "failed"          # what we observe …
    assert row["ok"] is True                 # … is not what we alarm on


def test_an_enabled_failed_server_is_a_fault(client, zap):
    """The Task 10 spec's failure case: a server the user switched on that we
    know is not working is `✗ Connection failed`, and THAT is what turns
    /api/health 503. `ok` is not `connected` — it is "nothing the user asked for
    is broken".
    """
    if not zap.status().get("mcp_installed"):
        pytest.skip("the mcp package is not installed in this environment")
    zap.set_auto_connect(True)
    zap._last_error = "connection refused by 127.0.0.1:9999"

    status, body = _health(client)

    assert status == 503
    assert body["ok"] is False
    row = next(s for s in body["mcp"]["servers"] if s["key"] == "zap")
    assert row["state"] == "failed"
    assert row["ok"] is False
    assert body["mcp"]["failing"] == 1
    assert any("zap" in p.lower() and "connection failed" in p.lower()
               for p in body["problems"]), body["problems"]


def test_a_connected_server_is_not_a_fault(client, zap):
    """The spec's happy case — `✓ Connected`, with its tool count."""
    _connect(zap, ("scan", "spider"))

    status, body = _health(client)

    assert status == 200
    row = next(s for s in body["mcp"]["servers"] if s["key"] == "zap")
    assert row["state"] == "connected"
    assert row["text"] == "Connected"
    assert row["ok"] is True
    assert row["tool_count"] == 2
    assert body["mcp"]["connected"] == 1


def test_the_mcp_section_drops_the_url_and_the_error_text(client, zap):
    """⚠️ `/api/health` HAS NO AUTH and is the endpoint people point a public
    monitor at. A URL can carry userinfo credentials (`http://user:pass@host`)
    and `last_error` is a string a remote server WE DO NOT CONTROL chose — which
    has echoed request headers back in the wild. `health_report()` drops both;
    an operator reads them on `/api/mcp` or `/mcp health` instead.
    """
    zap.set_url("http://alice:supersecret@127.0.0.1:8484")
    zap.set_auto_connect(True)
    zap._last_error = "rejected: Authorization=hunter2"

    _, body = _health(client)

    for row in body["mcp"]["servers"]:
        assert "url" not in row
        assert "detail" not in row
    blob = json.dumps(body).lower()
    assert "supersecret" not in blob
    assert "hunter2" not in blob
    assert "authorization" not in blob


def test_a_broken_mcp_section_never_500s(client, monkeypatch):
    """A registry that cannot report degrades like every other section.

    ⚠️ The isolation lives in `_section()`, so this asserts the same guarantee
    `test_a_raising_section_never_500s` does — and that a raising MCP registry
    cannot take the whole endpoint down with it.
    """
    from agent2.integrations import registry as R

    monkeypatch.setattr(R, "health_report",
                        lambda: (_ for _ in ()).throw(RuntimeError("registry down")))
    status, body = _health(client)

    assert status in (200, 503)
    assert "mcp" in body
    assert isinstance(body["mcp"], dict)


# ── Genuine faults ───────────────────────────────────────────────────────────

def test_unreachable_database_is_a_fault(client, monkeypatch):
    """The DB is the one hard dependency — a failing round-trip means down."""
    def _boom(*a, **kw):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(db, "qone", _boom)
    status, body = _health(client)

    assert status == 503
    assert body["ok"] is False
    assert any("db" in p for p in body["problems"])


def test_database_answering_wrongly_is_also_unreachable(client, monkeypatch):
    """A probe that RETURNS but does not answer correctly is still a fault.

    ⚠️ Distinct from the test above, which patches qone to raise — that trips
    the per-section error path and never reaches the `reachable` branch. A DB
    that responds without erroring but fails the round-trip is the state this
    branch actually guards, and nothing else covers it.
    """
    monkeypatch.setattr(db, "qone", lambda *a, **kw: None)
    status, body = _health(client)

    assert status == 503
    assert body["ok"] is False
    assert body["db"]["reachable"] is False
    assert any("database unreachable" in p for p in body["problems"]), body["problems"]


def test_schema_mismatch_is_a_fault(client, monkeypatch):
    """A wrong schema resurfaces later as a baffling unrelated error."""
    monkeypatch.setattr(db, "schema_version", lambda: 3)
    status, body = _health(client)

    assert status == 503
    assert body["ok"] is False
    assert any("schema at v3" in p and "expected v" in p
               for p in body["problems"]), body["problems"]


def test_dead_workers_are_a_fault(client, monkeypatch):
    """Workers that STARTED and then went away is lost capacity.

    This is the real fault the naive "workers == 0" check was reaching for:
    turns were submitted, the pool started threads, and none survive — so
    queued turns can never run.
    """
    monkeypatch.setattr(scheduler, "stats",
                        lambda: {"enabled": True, "workers": 0,
                                 "worker_starts": 8, "queued": 2,
                                 "max_queue": 64, "submitted": 10})
    status, body = _health(client)

    assert status == 503
    assert body["ok"] is False
    assert any("workers died" in p for p in body["problems"]), body["problems"]


def test_full_queue_is_a_fault(client, monkeypatch):
    """A full backlog means turns are actively being rejected."""
    monkeypatch.setattr(scheduler, "stats",
                        lambda: {"enabled": True, "workers": 8,
                                 "worker_starts": 8, "queued": 64,
                                 "max_queue": 64, "submitted": 100})
    status, body = _health(client)

    assert status == 503
    assert any("queue full" in p for p in body["problems"]), body["problems"]


# ── Section isolation ────────────────────────────────────────────────────────

def test_one_raising_section_does_not_fail_the_request(client, monkeypatch):
    """⚠️ A health endpoint that 500s because one COUNTER raised is worse than
    no health endpoint: the monitor reports the app down when the only broken
    thing is the reporting itself.
    """
    def _boom():
        raise RuntimeError("counter exploded")

    monkeypatch.setattr(db, "pool_stats", _boom)
    resp = client.get("/api/health")

    assert resp.status_code == 503          # reported, not crashed
    body = resp.get_json()
    assert "error" in body["pool"]
    assert "counter exploded" in body["pool"]["error"]
    # The other sections still reported.
    assert body["scheduler"]["enabled"] is True
    assert body["db"]["reachable"] is True


def test_a_raising_section_never_500s(client, monkeypatch):
    """Every section raising at once still yields a parseable report."""
    def _boom(*a, **kw):
        raise RuntimeError("nope")

    for name in ("pool_stats", "wal_stats"):
        monkeypatch.setattr(db, name, _boom)
    monkeypatch.setattr(scheduler, "stats", _boom)

    resp = client.get("/api/health")
    assert resp.status_code == 503
    body = resp.get_json()
    assert body["ok"] is False
    assert len(body["problems"]) >= 3


# ── The no-auth constraint ───────────────────────────────────────────────────

def test_payload_leaks_no_secrets(client):
    """There is no auth on any route, so this must be counters only.

    Pins the standing constraint: no key material, no chat or memory text, and
    not even the DB path (which discloses the filesystem layout).

    ⚠️ The scan deliberately EXCLUDES `sync.resources` / `sync.topics`. Those are
    a fixed enum of resource labels — one of which is literally `api_keys` — so a
    naive substring scan flags them. They are names of things that can change,
    never values, and the next test pins that enum so a real leak cannot hide by
    being added to it.
    """
    db.exe("INSERT INTO memories(id, content) VALUES(?, ?)",
           ("health-secret", "the moon landing was catered"))
    try:
        _, body = _health(client)
        scanned = {k: v for k, v in body.items() if k != "sync"}
        blob = json.dumps(scanned).lower()

        assert "moon landing" not in blob
        assert "catered" not in blob
        for forbidden in ("api_key", "apikey", "secret", "password", "token"):
            assert forbidden not in blob, f"health payload mentions {forbidden!r}"
        # The DB path itself is disclosure — it discloses the filesystem layout.
        assert str(db.DB).lower() not in blob
        assert ".db" not in blob
    finally:
        db.exe("DELETE FROM memories WHERE id=?", ("health-secret",))


def test_sync_section_reports_only_known_resource_names(client):
    """The sync section may report resource LABELS, never values.

    Pins the exclusion the leak test above makes: everything in `resources` and
    `topics` comes from the fixed set in core/sync.py. If something ever puts a
    real value in there, this fails.
    """
    from agent2.core import sync

    _, body = _health(client)
    known = set(sync.RESOURCES) if hasattr(sync, "RESOURCES") else {
        "memories", "rules", "api_keys", "providers",
        "settings", "workspace", "chats", "pil"}

    assert set(body["sync"].get("resources", [])) <= known
    assert set(body["sync"].get("topics", [])) <= known
    # `versions` is a resource -> counter map: keys are labels, values are ints.
    for name, ver in (body["sync"].get("versions") or {}).items():
        assert name in known, f"unknown sync resource in health payload: {name!r}"
        assert isinstance(ver, int)


def test_values_are_all_scalars_or_counters(client):
    """No section may smuggle free text through as a value.

    Every leaf is a number, bool, string label, or list of resource names — the
    shape a monitor can graph. A dict of unbounded strings would be a leak
    waiting to happen.
    """
    _, body = _health(client)

    for section in ("pool", "wal", "scheduler"):
        for key, val in body[section].items():
            assert isinstance(val, (int, float, bool)), \
                f"{section}.{key} is {type(val).__name__}, expected a counter"
