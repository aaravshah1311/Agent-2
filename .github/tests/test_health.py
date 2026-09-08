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
   counters only â€” no key material, no chat/memory text, not even the DB path.
"""

import json

import pytest
from flask import Flask

from agent2 import config
from agent2 import database as db
from agent2.core import permissions as perms
from agent2.core import scheduler
from agent2.core import tasks as core_tasks
from agent2.core.session import sessions as core_sessions
from agent2.llm.keys import rotator
from agent2.server.routes import register_routes


# Every section the aggregate reports. âš ï¸ Task 28 names ELEVEN subsystems and this
# names fourteen, because three of them ("db", "pool", "wal") are the one
# subsystem the task calls "Database" reported at the three granularities the
# readers that own them already expose. The list is spelled out here rather than
# imported so that a section silently *removed* from the route fails this file.
_SECTIONS = ("agent", "db", "pool", "wal", "scheduler", "tasks", "commands",
             "sync", "recovery", "mcp", "memory", "context", "providers",
             "permissions")


@pytest.fixture
def client():
    """A Flask test client on a fully-migrated DB."""
    db.init_db()
    app = Flask(__name__)
    register_routes(app)
    with app.test_client() as c:
        yield c


@pytest.fixture
def session():
    """An empty task session, removed afterwards.

    The suite shares one database, so every task assertion below is a DELTA
    against a baseline read â€” an absolute count would pass alone and fail the
    moment another test file left a row behind.
    """
    sid = core_tasks.open_session(goal="health-probe")
    yield sid
    core_tasks.delete_session_tasks(sid)
    db.exe("DELETE FROM task_sessions WHERE id=?", (sid,))


def _health(client):
    resp = client.get("/api/health")
    return resp.status_code, resp.get_json()


# â”€â”€ The healthy path â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def test_idle_server_is_healthy(client):
    """No turns submitted yet is the NORMAL state, not a fault.

    âš ï¸ Regression pin. The first version of this check flagged
    "enabled but no live workers", which is true of every server that has not
    yet handled a turn â€” scheduler.submit() starts the workers lazily. It
    returned 503 on a completely healthy app.
    """
    status, body = _health(client)

    assert status == 200
    assert body["ok"] is True
    assert body["problems"] == []

    sch = body["scheduler"]
    assert sch["enabled"] is True
    assert sch["workers"] == 0          # lazily started â€” the state under test
    assert sch["worker_starts"] == 0


def test_every_section_is_present(client):
    """All fourteen sections report, and none of them reports an error.

    âš ï¸ The `error` assertion is the load-bearing half. `_section()` degrades a
    raising counter to `{"error": â€¦}` instead of 500ing the request, which is
    exactly right for production and exactly wrong for a test: without this line a
    section could raise on every single call and the only visible symptom would be
    a 503 that several tests in this file already expect for other reasons.
    """
    _, body = _health(client)
    for section in _SECTIONS:
        assert section in body, f"missing health section: {section}"
        assert isinstance(body[section], dict), section
        assert "error" not in body[section], \
            f"{section}: {body[section].get('error')}"
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


# â”€â”€ Task 10: the MCP section â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@pytest.fixture
def zap():
    """The ZAP bridge, restored afterwards â€” it is a process-global singleton.

    âš ï¸ `enabled`, `url` and `key` are read-through PROPERTIES whose setters write
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
    """The state `is_connected()` reports as connected â€” two sentinels, no socket."""
    bridge._session = object()
    bridge._loop = object()
    bridge._tools = [{"real_name": t, "name": f"zap_{t}"} for t in tools]
    bridge._connected_url = bridge.url


def test_a_disconnected_never_enabled_mcp_server_is_not_a_fault(client):
    """âš ï¸ THE default install: both bridges ship auto-connect OFF.

    A fresh checkout has two disconnected servers, and reporting that as 503
    would be an alert that fires on the default configuration â€” the exact cry
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
    """DUAL MODE: the CLI child may hold the live session while the web process â€”
    the one serving /api/health â€” has never dialled. "Enabled, no session, no
    error recorded" means *we have not tried here yet*, and the next agent turn
    resolves it into connected or failed. Reporting a fault for it would make
    every dual-mode install permanently unhealthy.
    """
    if not zap.status().get("mcp_installed"):
        pytest.skip("the mcp package is not installed in this environment")
    zap.set_auto_connect(True)               # enabled â€¦ but nobody has dialled
    zap._last_error = ""
    status, body = _health(client)

    assert status == 200
    assert body["ok"] is True
    row = next(s for s in body["mcp"]["servers"] if s["key"] == "zap")
    assert row["state"] == "idle"
    assert row["enabled"] is True
    assert row["ok"] is True


def test_a_disabled_server_with_a_lingering_error_is_not_a_fault(client, zap):
    """âš ï¸ Rule 21, health-shaped: `last_error` records the MOST RECENT attempt,
    because `connect()` clears it first â€” but a server the user did not enable
    has nothing scheduled to happen. That error is history, not a condition a
    monitor should page someone about at 3am.
    """
    zap.set_auto_connect(False)
    zap._last_error = "connection refused by 127.0.0.1:9999"

    status, body = _health(client)

    assert status == 200
    assert body["ok"] is True
    row = next(s for s in body["mcp"]["servers"] if s["key"] == "zap")
    assert row["state"] == "failed"          # what we observe â€¦
    assert row["ok"] is True                 # â€¦ is not what we alarm on


def test_an_enabled_failed_server_is_a_fault(client, zap):
    """The Task 10 spec's failure case: a server the user switched on that we
    know is not working is `âœ— Connection failed`, and THAT is what turns
    /api/health 503. `ok` is not `connected` â€” it is "nothing the user asked for
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
    """The spec's happy case â€” `âœ“ Connected`, with its tool count."""
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
    """âš ï¸ `/api/health` HAS NO AUTH and is the endpoint people point a public
    monitor at. A URL can carry userinfo credentials (`http://user:pass@host`)
    and `last_error` is a string a remote server WE DO NOT CONTROL chose â€” which
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

    âš ï¸ The isolation lives in `_section()`, so this asserts the same guarantee
    `test_a_raising_section_never_500s` does â€” and that a raising MCP registry
    cannot take the whole endpoint down with it.
    """
    from agent2.integrations import registry as R

    monkeypatch.setattr(R, "health_report",
                        lambda: (_ for _ in ()).throw(RuntimeError("registry down")))
    status, body = _health(client)

    assert status in (200, 503)
    assert "mcp" in body
    assert isinstance(body["mcp"], dict)


# â”€â”€ Genuine faults â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def test_unreachable_database_is_a_fault(client, monkeypatch):
    """The DB is the one hard dependency â€” a failing round-trip means down."""
    def _boom(*a, **kw):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(db, "qone", _boom)
    status, body = _health(client)

    assert status == 503
    assert body["ok"] is False
    assert any("db" in p for p in body["problems"])


def test_database_answering_wrongly_is_also_unreachable(client, monkeypatch):
    """A probe that RETURNS but does not answer correctly is still a fault.

    âš ï¸ Distinct from the test above, which patches qone to raise â€” that trips
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
    turns were submitted, the pool started threads, and none survive â€” so
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


# â”€â”€ Section isolation â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def test_one_raising_section_does_not_fail_the_request(client, monkeypatch):
    """âš ï¸ A health endpoint that 500s because one COUNTER raised is worse than
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


# â”€â”€ The no-auth constraint â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def test_payload_leaks_no_secrets(client):
    """There is no auth on any route, so this must be counters only.

    Pins the standing constraint: no key material, no chat or memory text, and
    not even the DB path (which discloses the filesystem layout).

    âš ï¸ The scan deliberately EXCLUDES `sync.resources` / `sync.topics` and
    `permissions.capabilities`. Both are fixed enums of LABELS â€” one sync resource
    is literally `api_keys` and one capability is literally `secrets` â€” so a naive
    substring scan flags them. They are names of things that can change, never
    values, and the two tests below pin each enum so a real leak cannot hide by
    being added to one of them.
    """
    db.exe("INSERT INTO memories(id, content) VALUES(?, ?)",
           ("health-secret", "the moon landing was catered"))
    try:
        _, body = _health(client)
        scanned = {k: v for k, v in body.items()
                   if k not in ("sync", "permissions")}
        blob = json.dumps(scanned).lower()

        assert "moon landing" not in blob
        assert "catered" not in blob
        for forbidden in ("api_key", "apikey", "secret", "password", "token"):
            assert forbidden not in blob, f"health payload mentions {forbidden!r}"
        # The DB path itself is disclosure â€” it discloses the filesystem layout.
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

    Every leaf is a number, bool, string label, or list of resource names â€” the
    shape a monitor can graph. A dict of unbounded strings would be a leak
    waiting to happen.
    """
    _, body = _health(client)

    for section in ("pool", "wal", "scheduler"):
        for key, val in body[section].items():
            assert isinstance(val, (int, float, bool)), \
                f"{section}.{key} is {type(val).__name__}, expected a counter"


# â”€â”€ Task 28: the sections the aggregate gained â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
#
# The task's own instruction is *"reuse the existing endpoint rather than creating
# a duplicate"*, so the first test below is about a route that must NOT exist. The
# rest divide along one line that is the whole design of this endpoint: a
# **projection** never re-derives a fact (the readers that own it already did), and
# it never forwards the payload it read (command lines, key previews, task titles,
# memory text). Every section here is checked for both.


def test_there_is_no_second_aggregate_endpoint(client):
    """âš ï¸ Task 28 says *reuse the existing endpoint rather than creating a
    duplicate*, and this is the only way to test the negative: two aggregate
    endpoints means one of them is the stale one, and nothing in either payload
    says which. If a future phase adds `/api/status`, it must replace this route,
    not stand beside it.
    """
    for path in ("/api/status", "/api/healthz", "/api/health/full",
                 "/api/subsystems"):
        assert client.get(path).status_code == 404, f"a second aggregate: {path}"


def test_agent_section_reads_its_ceilings_from_config(client):
    """The `agent` section is a projection of `config.py`, not a copy of it.

    Catches the one failure that is otherwise invisible: a literal (`80`, `40`,
    `6000`) typed into the route. Both halves keep working, and they drift the day
    somebody edits `config.py` â€” the drift the one-declaration rule exists to stop.
    """
    _, body = _health(client)
    agent = body["agent"]

    assert agent["max_iters"] == config.MAX_AGENT_ITERS
    assert agent["max_ctx_messages"] == config.MAX_CTX_MESSAGES
    assert agent["max_tool_output"] == config.MAX_TOOL_OUTPUT
    assert agent["models"] == len(config.MODELS)
    assert agent["modes"] == len(config.MODES)
    assert agent["default_model"] in config.MODELS
    assert agent["default_mode"] in config.MODES


def test_agent_section_projects_the_live_turn_count(client, monkeypatch):
    """`active_turns` is asked of the session registry on every read.

    Not a counter this route keeps: a second tally of in-flight turns would be a
    second opinion about whether the agent is busy, and the disagreement would only
    ever show up under load.
    """
    monkeypatch.setattr(core_sessions, "active_tasks",
                        lambda: [object(), object(), object()])
    _, body = _health(client)

    assert body["agent"]["active_turns"] == 3


def test_a_build_without_the_gemini_sdk_still_reports_the_agent_section(client):
    """âš ï¸ REGRESSION PIN, and the reason `_agent_section` counts models from
    `config.MODELS` instead of importing `agent2.agent` to count its tool table:
    that module pulls in the Gemini SDK, so on a custom-provider-only install the
    import raises, the section degrades to an error and the endpoint 503s â€”
    announcing a fault in a supported configuration, which is the one thing this
    route may not do. Asserting the module is absent from the section's imports is
    not possible; asserting it is not needed is.
    """
    import sys

    saved = sys.modules.pop("agent2.agent", None)
    try:
        status, body = _health(client)
        assert status == 200
        assert "error" not in body["agent"]
        assert "agent2.agent" not in sys.modules, \
            "the agent section imported the Gemini-backed agent module"
    finally:
        if saved is not None:
            sys.modules["agent2.agent"] = saved


# â”€â”€ The task queue â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def test_tasks_section_counts_by_status_and_never_names_a_task(client, session):
    """Counts move with the queue; the title never appears."""
    before = _health(client)[1]["tasks"]

    core_tasks.create(session, "delete the production bucket")
    done = core_tasks.create(session, "second task")
    core_tasks.complete(done.id, "bucket-was-emptied")

    _, body = _health(client)
    now = body["tasks"]

    assert now["tasks"] == before["tasks"] + 2
    assert now["by_status"]["pending"] == before["by_status"]["pending"] + 1
    assert now["by_status"]["completed"] == before["by_status"]["completed"] + 1
    assert now["open"] == before["open"] + 1
    assert now["settled"] == before["settled"] + 1

    blob = json.dumps(body).lower()
    assert "production bucket" not in blob
    assert "bucket-was-emptied" not in blob      # the RESULT is content too


def test_task_status_table_is_zero_filled(client):
    """âš ï¸ ABSENT IS NOT ZERO. A `GROUP BY status` returns no row for a status
    nothing is in, so a monitor graphing `by_status.failed` would see the key
    appear and disappear â€” and a dashboard that cannot draw a zero reads a
    missing key as an outage of the reporting rather than as good news.
    """
    _, body = _health(client)
    for status in core_tasks.ALL_STATUSES:
        assert status in body["tasks"]["by_status"], status
        assert isinstance(body["tasks"]["by_status"][status], int)


def test_an_unknown_task_status_is_reported_under_its_own_name(client, session):
    """âš ï¸ NOT folded through `normalize_status()` into `pending`.

    `stats()` is a report, and a row written by an older or newer build is a fact
    about the database. Normalizing here would add it to `pending` â€” inventing
    queued work that does not exist and hiding the only evidence that something is
    writing a status this build does not know.
    """
    base = _health(client)[1]["tasks"]
    task = core_tasks.create(session, "probe")
    db.exe("UPDATE agent_tasks SET status='quantum' WHERE id=?", (task.id,))

    _, body = _health(client)
    now = body["tasks"]

    assert now["by_status"].get("quantum") == 1
    assert now["by_status"]["pending"] == base["by_status"]["pending"]
    assert now["tasks"] == base["tasks"] + 1
    # Unknown is not TERMINAL, so it counts as work still open.
    assert now["open"] == base["open"] + 1
    assert now["settled"] == base["settled"]


def test_tasks_section_reports_the_threshold_it_applied(client):
    """âš ï¸ `stale_after` is `stale_running()`'s OWN default, not a second number.

    A payload that named a threshold different from the one the sweep applied would
    describe a sweep that never ran â€” and the number is the only context that makes
    `stale: 3` mean anything.
    """
    _, body = _health(client)

    assert body["tasks"]["stale_after"] == core_tasks.STALE_AFTER
    assert body["tasks"]["stale_sample"] == core_tasks.STALE_SAMPLE


def test_a_stale_running_task_is_a_warning_not_a_fault(client, session):
    """âš ï¸ A task a dead worker left RUNNING is what worker recovery exists to
    collect. Raising it as a fault would 503 the app for as long as the queue held
    one, i.e. until recovery got to it â€” an alert that fires while the fix is
    already running.
    """
    task = core_tasks.create(session, "probe")
    core_tasks.start(task.id)
    db.exe("UPDATE agent_tasks SET last_heartbeat='',"
           " updated_at='2001-01-01 00:00:00', created_at='2001-01-01 00:00:00'"
           " WHERE id=?", (task.id,))

    status, body = _health(client)

    assert status == 200
    assert body["ok"] is True
    assert body["tasks"]["stale"] >= 1
    assert any("no recent heartbeat" in w for w in body["warnings"]), \
        body["warnings"]
    assert not any("heartbeat" in p for p in body["problems"]), body["problems"]


# â”€â”€ The command executor â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def test_commands_section_counts_without_the_command_line(client):
    """âš ï¸ `snapshot()` carries the command STRING, which is legitimate on
    `/api/commands` and a public leak here: a command line holds paths, hostnames
    and â€” as below â€” bearer tokens people paste into `curl`.
    """
    from agent2.core import commands as core_commands

    cmd = core_commands.create(
        "curl -H 'Authorization: Bearer hunter2' https://vault.internal/keys")
    try:
        _, body = _health(client)
        section = body["commands"]

        # âš ï¸ The forwarding assertions come FIRST, so a section that passes the
        # snapshot straight through fails on the reason rather than on a missing
        # `window` key three lines later.
        assert "commands" not in section       # the transcript is not forwarded
        assert "active" not in section
        assert "stuck" not in section
        blob = json.dumps(body).lower()
        assert "curl" not in blob
        assert "hunter2" not in blob
        assert "vault.internal" not in blob

        assert section["counts"]["total"] >= 1
        assert section["window"] > 0
        assert "stuck_after" in section["limits"]
    finally:
        core_commands.forget(cmd.id)


def test_a_stuck_command_is_a_warning_not_a_fault(client, monkeypatch):
    """âš ï¸ A stuck command is REPORTED and never killed (see `commands.watch()`) â€”
    and it may be a `sleep 600` the user typed on purpose. Worth a sentence; not a
    fault, and lifting it would 503 the app for as long as the command ran.
    """
    from agent2.core import commands as core_commands

    monkeypatch.setattr(
        core_commands, "snapshot",
        lambda **kw: {"counts": {"total": 1, "active": 1, "stuck": 1, "failed": 0},
                      "limits": {"timeout": None, "idle_timeout": None,
                                 "stuck_after": 20.0}})
    status, body = _health(client)

    assert status == 200
    assert body["ok"] is True
    assert body["commands"]["counts"]["stuck"] == 1
    assert any("producing no output" in w for w in body["warnings"]), \
        body["warnings"]
    assert body["problems"] == []


# â”€â”€ Memory, rules, and the context broker â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def test_memory_section_counts_without_the_text(client):
    """Memories and rules are the two blocks that end every system prompt, so
    "how many" is a health fact and "which" is the user's private content.
    """
    db.exe("INSERT INTO memories(id, content) VALUES(?, ?)",
           ("health-mem", "the vault code is 4815162342"))
    db.exe("INSERT INTO rules(id, content, active) VALUES(?, ?, 1)",
           ("health-rule", "never speak of the vault"))
    try:
        _, body = _health(client)
        section = body["memory"]

        assert section["memories"] >= 1
        assert section["rules"] >= 1
        assert section["rules_active"] >= 1
        assert section["rules_active"] <= section["rules"]

        blob = json.dumps(body).lower()
        assert "4815162342" not in blob
        assert "vault" not in blob
    finally:
        db.exe("DELETE FROM memories WHERE id=?", ("health-mem",))
        db.exe("DELETE FROM rules WHERE id=?", ("health-rule",))


def test_context_section_reports_the_source_TABLE_not_the_prompt(client):
    """âš ï¸ `broker.stats()` reports which sources exist and in what order â€” never
    an item's text. The broker is the one component whose payload IS the user's
    project: the project doc, the memories, the file diffs. A health endpoint that
    echoed any of it would publish the prompt.
    """
    from agent2.core import broker

    _, body = _health(client)
    ctx = body["context"]

    assert list(ctx["registered"]) == list(broker.ORDER)
    assert set(ctx["always"]) == set(broker.ALWAYS)
    # âš ï¸ Falls back to `project`, never `off` â€” see `isolation.py`.
    assert ctx["isolation"]["mode"] in ("project", "off")
    assert isinstance(ctx["budget"]["reserve"], int)


# â”€â”€ Model credentials â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def test_providers_section_drops_the_key_preview(client, monkeypatch):
    """âš ï¸ `rotator.status()` carries `preview` â€” the first fourteen characters of a
    real API key. That is deliberate on `/api/keys`, where the user is identifying
    which key is which; here it would be key material on an endpoint people point
    a public monitor at. This section counts the rows and forwards none of them.
    """
    monkeypatch.setattr(rotator, "status", lambda: [
        {"label": "one", "preview": "AIzaSyLEAKED0", "active": True,
         "errs": 0, "pinned": False},
        {"label": "two", "preview": "AIzaSyLEAKED1", "active": False,
         "errs": 3, "pinned": True},
    ])
    status, body = _health(client)
    section = body["providers"]

    assert status == 200
    assert section["gemini_keys"] == 2
    assert section["gemini_keys_active"] == 1
    assert section["gemini_keys_failing"] == 1
    assert section["gemini_key_pinned"] is True
    assert "leaked" not in json.dumps(body).lower()


def test_providers_section_counts_a_custom_provider_without_its_endpoint(client,
                                                                        monkeypatch):
    """A registered provider is counted; its base URL and key are not reported.

    A base URL can carry userinfo credentials and always names infrastructure, so
    it belongs on `/api/providers` behind the same auth as the rest of the row.
    """
    from agent2.llm import capabilities as caps
    from agent2.llm import providers as P

    # `add_provider` ranks the new model in a daemon thread. Harmless in
    # production, non-deterministic in a test â€” and it is not what is under test.
    monkeypatch.setattr(caps, "rank_in_background", lambda *a, **kw: None)
    P.init_providers_table()
    before = _health(client)[1]["providers"]["custom_providers"]
    row = P.add_provider("health probe", "https://gateway.invalid/v1",
                         "sk-do-not-print-me", "some-model")
    try:
        _, body = _health(client)
        assert body["providers"]["custom_providers"] == before + 1

        blob = json.dumps(body).lower()
        assert "gateway.invalid" not in blob
        assert "do-not-print-me" not in blob
    finally:
        P.remove_provider(row["id"])


def test_a_missing_providers_table_is_not_a_fault(client, monkeypatch):
    """âš ï¸ REGRESSION PIN â€” this route 503'd on a healthy database.

    `providers` is created by `init_providers_table()`, NOT by `init_db()` (see
    migration 5's note in `database.py`), so any process that registered the routes
    without running an entry point has no such table â€” including this file's own
    fixture. The first version of this section called `len(list_providers())`,
    which raised, which became a `problems` entry, which turned every test above
    into `assert 503 == 200`. `count_providers()` reads absent as zero, which is
    what it is: no table, no rows.
    """
    import sqlite3

    from agent2.llm import providers as P

    def _no_table(*a, **kw):
        raise sqlite3.OperationalError("no such table: providers")

    monkeypatch.setattr(P, "qone", _no_table)
    status, body = _health(client)

    assert status == 200
    assert body["ok"] is True
    assert body["providers"]["custom_providers"] == 0
    assert not any("providers" in p for p in body["problems"]), body["problems"]


# â”€â”€ Warnings: true, actionable, and NOT a fault â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def test_no_model_credentials_is_a_warning_not_a_fault(client, monkeypatch):
    """âš ï¸ THE MOST IMPORTANT NON-FAULT IN THE PAYLOAD.

    No key and no provider is why a fresh install appears to do nothing, so the
    endpoint must SAY it. It is also the state every install passes through before
    `/addapi`, and no restart fixes it â€” which is the test for whether a line
    belongs in `problems`. So it is a warning, and the status code stays 200.
    """
    from agent2.llm import providers as P

    monkeypatch.setattr(rotator, "status", list)
    monkeypatch.setattr(P, "count_providers", lambda: 0)
    status, body = _health(client)

    assert status == 200
    assert body["ok"] is True
    assert body["problems"] == []
    assert any("no model credentials" in w for w in body["warnings"]), \
        body["warnings"]


def test_a_credentialed_install_gets_no_credential_warning(client, monkeypatch):
    """The other direction â€” otherwise the warning above would be a constant.

    âš ï¸ EITHER credential clears it: a custom-provider-only install has no Gemini
    key on purpose, and warning about that would be the same cry-wolf one level
    down.
    """
    from agent2.llm import providers as P

    monkeypatch.setattr(rotator, "status", list)
    monkeypatch.setattr(P, "count_providers", lambda: 1)
    _, body = _health(client)

    assert not any("credential" in w for w in body["warnings"]), body["warnings"]


def test_a_needs_review_recovery_record_is_a_warning_not_a_fault(client,
                                                                monkeypatch):
    """âš ï¸ A recovery record awaiting review is recovery WORKING â€” `crash.stats()`
    already refuses to call it a problem. It may sit in the queue for weeks, so
    lifting it would pin this endpoint at 503 until somebody tidied up.
    """
    from agent2.core.recovery import crash as _crash

    base = dict(_crash.stats())
    monkeypatch.setattr(_crash, "stats",
                        lambda: {**base, "needs_review": 3, "problems": []})
    status, body = _health(client)

    assert status == 200
    assert body["ok"] is True
    assert any("awaiting review" in w for w in body["warnings"]), body["warnings"]


def test_warnings_never_change_ok_or_the_status_code(client, monkeypatch, session):
    """All four warning conditions at once, and the app is still healthy.

    âš ï¸ THE WHOLE VALUE OF A 503 HERE IS THAT IT MEANS *"a restart or an operator
    can fix this"*, and every supported configuration that trips it spends that
    meaning. This is the pin against a future phase promoting one of these four
    lines into `problems` "for visibility": no key, a stuck command, a stale task
    and an unreviewed recovery record are, together, still a 200.
    """
    from agent2.core import commands as core_commands
    from agent2.core.recovery import crash as _crash
    from agent2.llm import providers as P

    monkeypatch.setattr(rotator, "status", list)
    monkeypatch.setattr(P, "count_providers", lambda: 0)
    monkeypatch.setattr(
        core_commands, "snapshot",
        lambda **kw: {"counts": {"total": 2, "active": 2, "stuck": 2, "failed": 0},
                      "limits": {"timeout": None, "idle_timeout": None,
                                 "stuck_after": 20.0}})
    base = dict(_crash.stats())
    monkeypatch.setattr(_crash, "stats",
                        lambda: {**base, "needs_review": 1, "problems": []})
    task = core_tasks.create(session, "probe")
    core_tasks.start(task.id)
    db.exe("UPDATE agent_tasks SET last_heartbeat='',"
           " updated_at='2001-01-01 00:00:00', created_at='2001-01-01 00:00:00'"
           " WHERE id=?", (task.id,))

    status, body = _health(client)

    assert status == 200
    assert body["ok"] is True
    assert body["problems"] == []
    assert len(body["warnings"]) == 4, body["warnings"]


def test_a_fault_and_a_warning_are_reported_side_by_side(client, monkeypatch):
    """The 503 half of the split: a real fault still 503s while a warning stands.

    Pins that the two lists are independent â€” a warning does not soften a fault,
    and a fault does not swallow the warning that would tell the operator what
    else to fix once the app is up.
    """
    from agent2.llm import providers as P

    monkeypatch.setattr(db, "schema_version", lambda: 3)
    monkeypatch.setattr(rotator, "status", list)
    monkeypatch.setattr(P, "count_providers", lambda: 0)
    status, body = _health(client)

    assert status == 503
    assert body["ok"] is False
    assert any("schema at v3" in p for p in body["problems"]), body["problems"]
    assert any("no model credentials" in w for w in body["warnings"]), \
        body["warnings"]


def test_warnings_is_always_present_and_always_a_list(client):
    """A monitor reads `body["warnings"]` unconditionally; an absent key is a
    KeyError in somebody's dashboard, not a healthy install.
    """
    _, body = _health(client)

    assert isinstance(body["warnings"], list)
    assert isinstance(body["problems"], list)
    assert all(isinstance(w, str) for w in body["warnings"])


# â”€â”€ Capabilities â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def test_permissions_section_reports_posture_and_tallies(client):
    """The posture is what makes the tally readable.

    "denied: 41" is alarming until you see that `AGENT2_DENY_CAPS` took `exec`
    away on purpose â€” a configuration, not a fault. So the section carries both,
    and the endpoint raises no problem either way: a locked-down process is
    working exactly as its operator asked.
    """
    _, body = _health(client)
    section = body["permissions"]

    assert section["role"] in perms.ROLES
    assert isinstance(section["capabilities"], list)
    assert isinstance(section["denied"], list)
    assert section["counters"]["allowed"] >= 0
    assert section["counters"]["denied"] >= 0


def test_permissions_section_reports_only_known_capability_names(client):
    """Pins the enum the leak scan excludes â€” the same contract
    `test_sync_section_reports_only_known_resource_names` pins for `sync`.

    âš ï¸ `capabilities` is a list of NAMES, one of which is literally `secrets`, so
    the blanket substring scan cannot cover this section. If anything ever puts a
    value in there, this fails.
    """
    _, body = _health(client)
    section = body["permissions"]
    known = set(perms.ALL_CAPS)

    assert set(section["capabilities"]) <= known
    assert set(section["denied"]) <= known
    assert set(section["process_capabilities"]) <= known
    assert set(section["counters"]) == {"allowed", "denied"}


def test_a_locked_down_process_is_not_unhealthy(client, monkeypatch):
    """`AGENT2_DENY_CAPS=all` is a supported configuration â€” a read-only deployment.

    It is also the shape most likely to be mistaken for a fault, since the agent
    can do nothing. Nobody is paged for a machine configured that way on purpose.
    """
    monkeypatch.setattr(perms, "describe",
                        lambda role=None: {"role": "viewer", "capabilities": [],
                                           "denied": sorted(perms.ALL_CAPS),
                                           "process_capabilities": [],
                                           "counters": {"allowed": 0,
                                                        "denied": 97}})
    status, body = _health(client)

    assert status == 200
    assert body["ok"] is True
    assert body["permissions"]["counters"]["denied"] == 97


# ── The verdict list (Task 28) ────────────────────────────────────────────────
# Task 28's example output is a LIST OF MARKS — `✓ Database`, `⚠ Gemini Provider`
# — not a JSON tree, and `/health` in the CLI renders the same one. So `sections`
# is a payload key with its own contract, and these tests pin the one property
# that cannot be checked by reading either half alone: the marks and the aggregate
# describe the same install.


def test_every_section_has_a_verdict_row(client):
    """One row per subsystem, in `SECTIONS` order, MCP and providers expanded.

    ⚠️ The order is asserted because it is the reading order on three surfaces. A
    dict would have made it incidental; the tuple is the declaration.
    """
    _, body = _health(client)
    rows = body["sections"]

    assert isinstance(rows, list) and rows
    keys = [r["key"] for r in rows]
    # Every plain section appears exactly once, under its own key.
    for key in _SECTIONS:
        if key in ("mcp", "providers"):
            continue
        assert keys.count(key) == 1, f"{key} missing or duplicated in sections"
    # The two expanded ones appear as prefixed children, never as a bare row.
    assert "providers" not in keys
    assert {"providers.gemini", "providers.custom"} <= set(keys)
    assert all(k.startswith("mcp.") for k in keys if k.startswith("mcp"))
    # Order: the plain keys follow SECTIONS.
    plain = [k for k in keys if k in _SECTIONS]
    assert plain == [k for k in _SECTIONS if k in plain]


def test_every_row_carries_a_known_state_and_a_label(client):
    """Four words, and a renderer that only knew ✓/✗ is why `off` is one of them."""
    _, body = _health(client)

    for row in body["sections"]:
        assert row["state"] in ("ok", "warn", "fail", "off"), row
        assert isinstance(row["label"], str) and row["label"]
        assert isinstance(row["text"], str)
        assert isinstance(row["ok"], bool)


def test_a_row_is_ok_unless_it_failed(client):
    """⚠️ `row["ok"]` is `state != "fail"` — `off` and `warn` are both fine.

    The same rule `registry.health()` documents: a server that is off is working
    as configured, and a second opinion is what prints a cross at a user who
    disabled it on purpose.
    """
    _, body = _health(client)

    for row in body["sections"]:
        assert row["ok"] is (row["state"] != "fail"), row


def test_no_row_says_fail_while_the_report_says_ok(client):
    """⚠️ THE LOAD-BEARING EQUIVALENCE, in the direction a healthy install can show.

    A green aggregate beside a red row is the one output nobody can act on: the
    monitor is quiet and the dashboard is on fire. `_rows()` projects states from
    the very lines the aggregate was built from, so this holds by construction —
    this test is what notices if somebody re-derives them instead.
    """
    status, body = _health(client)

    failed = [r for r in body["sections"] if r["state"] == "fail"]
    assert bool(failed) is (not body["ok"])
    assert (status == 200) is body["ok"]


def test_a_fault_turns_its_own_row_red_and_leaves_the_others_alone(client,
                                                                  monkeypatch):
    """The other direction: a real fault must reach the list, and only its row.

    ⚠️ Scoped deliberately. A fault that marked everything red would make the list
    useless for the one question it exists to answer — *which* subsystem.
    """
    monkeypatch.setattr(db, "qone", lambda *a, **k: None)
    status, body = _health(client)

    assert status == 503 and body["ok"] is False
    rows = {r["key"]: r for r in body["sections"]}
    assert rows["db"]["state"] == "fail"
    assert rows["db"]["ok"] is False
    # The fault's text is the aggregate's line, not a second wording of it.
    assert rows["db"]["text"] in body["problems"]
    assert rows["memory"]["state"] != "fail"
    assert rows["permissions"]["state"] != "fail"


def test_a_warning_shows_as_warn_and_still_reports_ok(client, monkeypatch):
    """A caution is visible in the list and invisible to the status code."""
    monkeypatch.setattr(rotator, "status", lambda: [])
    monkeypatch.setattr(db, "qone",
                        lambda sql, *a, **k: {"n": 0} if "providers" in str(sql)
                        else {"ok": 1, "v": db.SCHEMA_VERSION})
    status, body = _health(client)

    rows = {r["key"]: r for r in body["sections"]}
    assert status == 200 and body["ok"] is True
    assert rows["providers.gemini"]["state"] == "warn"
    assert rows["providers.gemini"]["ok"] is True
    assert rows["providers.gemini"]["text"] in body["warnings"]


def test_a_disabled_wal_checkpointer_reads_off_not_warn(client, monkeypatch):
    """⚠️ `off` IS NOT A LESSER `warn`. `AGENT2_WAL_CHECKPOINT_SEC=0` is a
    documented switch, and a ⚠ next to it is a support ticket about a setting the
    operator chose.
    """
    monkeypatch.setattr(db, "wal_stats",
                        lambda: {"running": False, "interval_sec": 0.0,
                                 "runs": 0, "errors": 0, "wal_bytes": 0})
    status, body = _health(client)
    rows = {r["key"]: r for r in body["sections"]}

    assert status == 200
    assert rows["wal"]["state"] == "off"
    assert rows["wal"]["ok"] is True


def test_a_disabled_scheduler_reads_off_not_warn(client, monkeypatch):
    """Same rule: `AGENT2_MAX_CONCURRENT_TURNS=0` runs turns on their own thread."""
    monkeypatch.setattr(scheduler, "stats",
                        lambda: {"enabled": False, "workers": 0,
                                 "worker_starts": 0, "queued": 0,
                                 "max_queue": 64, "submitted": 0})
    status, body = _health(client)
    rows = {r["key"]: r for r in body["sections"]}

    assert status == 200
    assert rows["scheduler"]["state"] == "off"
    assert rows["scheduler"]["ok"] is True


def test_mcp_rows_are_one_per_server_and_forwarded_verbatim(client):
    """⚠️ Task 28 asks for `✓ Burp MCP` and `✓ OWASP ZAP MCP` SEPARATELY, and the
    state comes off `registry.health()` — a folded line cannot say which is down,
    and a re-derived state is the fifth opinion `registry.health()` exists to end.
    """
    from agent2.integrations import registry

    _, body = _health(client)
    rows = {r["key"]: r for r in body["sections"] if r["key"].startswith("mcp")}
    verdicts = {v["key"]: v for v in registry.health_report()["servers"]}

    assert len(rows) == len(verdicts) == body["mcp"]["total"]
    for key, verdict in verdicts.items():
        row = rows[f"mcp.{key}"]
        assert row["state"] == verdict["state"]
        assert row["text"] == verdict["text"]
        assert verdict["label"] in row["label"]


def test_a_broken_mcp_section_shows_one_failed_row_not_none(client, monkeypatch):
    """A section that could not be built is `✗`, never silently absent.

    An absent row reads as "no MCP in this build", which is a different fact from
    "the MCP reader raised" and the wrong one to act on.
    """
    from agent2.integrations import registry

    monkeypatch.setattr(registry, "health_report",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    status, body = _health(client)
    rows = [r for r in body["sections"] if r["key"].startswith("mcp")]

    assert status == 503
    assert len(rows) == 1
    assert rows[0]["state"] == "fail"


def test_verdict_lines_render_the_same_rows_in_the_same_order(client):
    """The plain-text fallback is a projection of `sections`, not a second pass.

    ⚠️ `verdict_lines()` is what a surface with no Rich prints. Building it from
    anything but `sections` is how the two surfaces come to disagree about which
    subsystem is down while both look internally consistent.
    """
    from agent2.core import health as core_health

    _, body = _health(client)
    lines = core_health.verdict_lines(body)
    marks = {"ok": "✓", "warn": "⚠", "fail": "✗", "off": "○"}

    assert len(lines) == len(body["sections"])
    for line, row in zip(lines, body["sections"]):
        assert line == f"{marks[row['state']]} {row['label']}"


def test_the_verdict_list_carries_no_text_a_leak_scan_would_reject(client):
    """`text` is a phrase built from counters — never a path, a key or a URL.

    The endpoint has no auth, so this is the same contract
    `test_payload_leaks_no_secrets` pins for the sections, applied to the one part
    of the payload that is prose.
    """
    _, body = _health(client)
    # `ensure_ascii=False` on purpose: escaping `·` to `·` would put a
    # backslash in the blob and make the path-separator needle a false positive.
    blob = json.dumps(body["sections"], ensure_ascii=False).lower()

    for needle in ("sk-", "api_key", "http://", "https://", "\\", ".db",
                   "authorization", "bearer"):
        assert needle not in blob, needle
