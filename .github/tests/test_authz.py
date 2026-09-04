# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""Tests for the capability model (agent2/core/permissions.py — Task 15).

Task 14 answered *who is this*. This suite is about the second, different
question: *may this identity do this particular thing*. The two are easy to
conflate, and conflating them is what produces a server where anyone who can log
in can also rotate the API keys and shell out.

Three things carry the feature, and each is pinned:

1. **The default install is unchanged.** A capability model that quietly took
   something away from the single-user local case would be reverted by its own
   user, so `owner` holds everything and every existing test must stay green.
2. **The table is total.** Every route, every socket event and every mutating
   tool maps to a capability, and an *unmapped* mutating route falls to the
   narrowest capability rather than the widest — so the next endpoint someone adds
   is guarded before they think about it.
3. **The gate is reached from all three surfaces.** A policy enforced only on the
   HTTP edge is bypassed by the very next thing the model does on a turn that edge
   already admitted, which is why `dispatch_tool` and both command runners ask
   too.
"""

import os

import pytest
from flask import Flask

from agent2 import database as db
from agent2.core import permissions as perms
from agent2.server import auth
from agent2.server.routes import register_routes

REMOTE = "203.0.113.9"


@pytest.fixture(autouse=True)
def _clean_env():
    """Restore the env AND the active workspace around every test.

    The workspace half is not decoration: `set_workspace` is global, persisted
    state shared with every other suite, so a test that points it at its own
    `tmp_path` and walks away makes an unrelated file (`test_tasks.py`'s
    project-key assertions) fail later, in a different file, for no visible
    reason. Ask any suite that has debugged that once.
    """
    from agent2.core import workspace as ws

    saved = {k: os.environ.get(k) for k in (
        "AGENT2_WEB_ROLE", "AGENT2_DENY_CAPS", "AGENT2_WEB_AUTH",
        "AGENT2_WEB_TOKEN", "AGENT2_WEB_RATE_LIMIT")}
    for k in saved:
        os.environ.pop(k, None)
    db.init_db()
    try:
        original_root = str(ws.root())
    except Exception:
        original_root = ""
    auth.rate_reset()
    perms.reset_counters()
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    if original_root:
        try:
            ws.set_workspace(original_root)
        except Exception:
            pass
    auth.rate_reset()


@pytest.fixture
def client():
    app = Flask(__name__)
    register_routes(app)
    with app.test_client() as c:
        yield c


# ══════════════════════════════════════════════════════════════════════════════
# The policy itself
# ══════════════════════════════════════════════════════════════════════════════

def test_the_default_role_holds_every_capability():
    """⚠️ Regression pin on the whole design.

    If this ever fails, every existing behaviour of a normal install has silently
    changed. The capability model is for *hardening a deployment*, not for making
    the default one poorer.
    """
    assert perms.role_name() == perms.ROLE_OWNER
    assert perms.caps_for() == frozenset(perms.ALL_CAPS)
    assert all(perms.allowed(c) for c in perms.ALL_CAPS)


def test_viewer_may_only_read():
    os.environ["AGENT2_WEB_ROLE"] = "viewer"
    assert perms.caps_for() == frozenset({perms.CAP_READ})
    assert perms.allowed(perms.CAP_READ) is True
    assert perms.allowed(perms.CAP_CHAT) is False
    assert perms.allowed(perms.CAP_EXEC) is False


def test_operator_may_drive_the_agent_but_not_touch_credentials():
    """The split exists because "trusted with this repo" and "trusted with my API
    keys" are different people, and there was previously no way to say so."""
    os.environ["AGENT2_WEB_ROLE"] = "operator"
    assert perms.allowed(perms.CAP_CHAT) is True
    assert perms.allowed(perms.CAP_EXEC) is True
    assert perms.allowed(perms.CAP_FS_WRITE) is True
    assert perms.allowed(perms.CAP_SECRETS) is False
    assert perms.allowed(perms.CAP_MCP_CONFIG) is False
    assert perms.allowed(perms.CAP_DESTRUCTIVE) is False


def test_an_unknown_role_falls_back_to_viewer_not_to_owner():
    """⚠️ Fallback direction. A typo in a security setting must never grant.

    `operater` gets a visibly broken UI and is fixed in a minute. The opposite
    mistake is silent and permanent.
    """
    os.environ["AGENT2_WEB_ROLE"] = "operater"
    assert perms.role_name() == perms.ROLE_VIEWER


@pytest.mark.parametrize("spelling", ["readonly", "read-only", "RO", "  Viewer  "])
def test_common_spellings_of_read_only_are_accepted(spelling):
    os.environ["AGENT2_WEB_ROLE"] = spelling
    assert perms.role_name() == perms.ROLE_VIEWER


def test_deny_caps_subtracts_from_every_role_including_owner():
    os.environ["AGENT2_DENY_CAPS"] = "exec, fs.delete"
    assert perms.role_name() == perms.ROLE_OWNER
    assert perms.allowed(perms.CAP_EXEC) is False
    assert perms.allowed(perms.CAP_FS_DELETE) is False
    assert perms.allowed(perms.CAP_FS_WRITE) is True


def test_deny_all_keeps_read_so_the_ui_still_functions():
    """A deployment that can show nothing is indistinguishable from a broken one."""
    os.environ["AGENT2_DENY_CAPS"] = "all"
    assert perms.process_caps() == frozenset({perms.CAP_READ})


def test_a_typo_in_deny_caps_is_dropped_and_logged_not_silently_accepted(caplog):
    """⚠️ The worst outcome for a security control is looking like it works.

    `AGENT2_DENY_CAPS=exe` denies nothing. To the operator who set it, that is
    indistinguishable from a working restriction — so it is announced.
    """
    perms._WARNED.clear()
    os.environ["AGENT2_DENY_CAPS"] = "exe"
    with caplog.at_level("INFO", logger="agent2"):
        assert perms.denied_caps() == frozenset()
    assert any("authz.config.bad" in r.getMessage() for r in caplog.records)


def test_the_deny_warning_is_logged_once_not_per_request(caplog):
    """A per-request warning about a static misconfiguration is a log flood."""
    perms._WARNED.clear()
    os.environ["AGENT2_DENY_CAPS"] = "nonsense"
    with caplog.at_level("INFO", logger="agent2"):
        for _ in range(5):
            perms.denied_caps()
    hits = [r for r in caplog.records if "authz.config.bad" in r.getMessage()]
    assert len(hits) == 1


def test_the_policy_is_read_at_call_time_not_captured_at_import():
    """A snapshot of an authorization decision is the frozen copy the
    one-declaration rule exists to prevent — and here it would mean a change to
    the policy took effect only after a restart."""
    assert perms.allowed(perms.CAP_EXEC) is True
    os.environ["AGENT2_DENY_CAPS"] = "exec"
    assert perms.allowed(perms.CAP_EXEC) is False
    os.environ.pop("AGENT2_DENY_CAPS")
    assert perms.allowed(perms.CAP_EXEC) is True


# ══════════════════════════════════════════════════════════════════════════════
# Lookup 1 — routes
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("method,path,cap", [
    ("GET", "/api/chats", perms.CAP_READ),
    ("GET", "/api/keys", perms.CAP_READ),
    ("POST", "/api/chats", perms.CAP_CHAT),
    ("POST", "/api/memories", perms.CAP_MEMORY),
    ("DELETE", "/api/memories/7", perms.CAP_MEMORY),
    ("DELETE", "/api/memories", perms.CAP_DESTRUCTIVE),
    ("POST", "/api/memories/prune", perms.CAP_DESTRUCTIVE),
    ("POST", "/api/rules", perms.CAP_MEMORY),
    ("DELETE", "/api/rules", perms.CAP_DESTRUCTIVE),
    ("POST", "/api/keys", perms.CAP_SECRETS),
    ("DELETE", "/api/keys/k1", perms.CAP_SECRETS),
    ("POST", "/api/providers", perms.CAP_SECRETS),
    ("POST", "/api/auth/rotate", perms.CAP_SECRETS),
    ("POST", "/api/auth/logout", perms.CAP_READ),
    ("POST", "/api/mcp/zap/connect", perms.CAP_MCP),
    ("POST", "/api/mcp/zap/auto", perms.CAP_MCP),
    ("POST", "/api/mcp/zap/config", perms.CAP_MCP_CONFIG),
    ("PUT", "/api/pil/settings", perms.CAP_SETTINGS),
    ("POST", "/api/pil/wipe", perms.CAP_DESTRUCTIVE),
    ("POST", "/api/pil/predict", perms.CAP_CHAT),
    ("POST", "/api/workspace", perms.CAP_SETTINGS),
    ("POST", "/api/sync/poll", perms.CAP_READ),
])
def test_route_capability_table(method, path, cap):
    assert perms.capability_for(method, path) == cap


def test_the_mcp_config_rule_beats_the_general_mcp_rule():
    """Ordering pin. `/api/mcp/<key>/config` writes an endpoint and a credential;
    if the general `/api/mcp` rule matched first, an `operator` who may toggle a
    bridge could also repoint it at a host they control."""
    assert perms.capability_for("POST", "/api/mcp/zap/config") == perms.CAP_MCP_CONFIG
    assert perms.capability_for("POST", "/api/mcp/zap/connect") == perms.CAP_MCP


def test_an_unmapped_mutating_api_route_defaults_to_the_narrowest_capability():
    """⚠️ Default-DENY for the next endpoint someone adds.

    Defaulting to `read` (or to `chat`) would mean every future route ships
    unguarded — the exact failure mode of an opt-in decorator.
    """
    assert perms.capability_for("POST", "/api/something/brand/new") \
        == perms.CAP_DESTRUCTIVE
    os.environ["AGENT2_WEB_ROLE"] = "operator"
    assert perms.allowed(perms.capability_for("POST", "/api/brand/new")) is False


def test_safe_methods_never_need_more_than_read():
    for path in ("/api/keys", "/api/mcp/zap/config", "/api/health", "/api/anything"):
        for method in ("GET", "HEAD", "OPTIONS"):
            assert perms.capability_for(method, path) == perms.CAP_READ


# ══════════════════════════════════════════════════════════════════════════════
# Lookup 1 — enforced end to end over HTTP
# ══════════════════════════════════════════════════════════════════════════════

def test_a_viewer_may_read_but_not_write(client):
    os.environ["AGENT2_WEB_ROLE"] = "viewer"
    assert client.get("/api/chats").status_code == 200
    resp = client.post("/api/memories", json={"content": "viewer-write"})
    assert resp.status_code == 403
    assert resp.get_json()["code"] == "forbidden"
    assert db.qall("SELECT * FROM memories WHERE content='viewer-write'") == []


def test_an_operator_is_refused_the_credential_routes(client):
    os.environ["AGENT2_WEB_ROLE"] = "operator"
    assert client.post("/api/memories", json={"content": "op-write"}).status_code == 200
    assert client.post("/api/keys", json={"key": "AIzaFAKE", "name": "k"}).status_code == 403
    assert client.post("/api/mcp/zap/config", json={"port": 9999}).status_code == 403
    # …but the bridge toggle it IS trusted with still works.
    assert client.post("/api/mcp/zap/auto", json={"enabled": False}).status_code == 200


def test_the_capability_check_covers_a_bearer_caller_too(client):
    """⚠️ All three identity paths funnel through one authorizer.

    A check applied to the cookie path only is the shape of bug that makes a
    policy look enforced while a scripted client walks past it.
    """
    os.environ["AGENT2_WEB_ROLE"] = "viewer"
    os.environ["AGENT2_WEB_TOKEN"] = "tok-abc-123"
    resp = client.post("/api/memories", json={"content": "x"},
                       headers={"Authorization": "Bearer tok-abc-123"},
                       environ_base={"REMOTE_ADDR": REMOTE})
    assert resp.status_code == 403


def test_the_capability_check_covers_a_trusted_loopback_caller_too(client):
    os.environ["AGENT2_WEB_ROLE"] = "viewer"
    assert client.post("/api/memories", json={"content": "x"}).status_code == 403


def test_authorization_is_answered_after_authentication_not_before(client):
    """A stranger must learn "authentication required", never "you lack 'memory'" —
    the second answer confirms the endpoint exists and is worth attacking."""
    os.environ["AGENT2_WEB_ROLE"] = "viewer"
    resp = client.post("/api/memories", json={"content": "x"},
                       environ_base={"REMOTE_ADDR": REMOTE})
    assert resp.status_code == 401
    assert resp.get_json()["code"] == "no_session"


def test_auth_off_skips_capability_checks_entirely(client):
    """`off` is documented as "no checks", and a capability refusal on a server
    whose banner says auth is disabled would be a contradiction nobody can debug."""
    os.environ["AGENT2_WEB_AUTH"] = "off"
    os.environ["AGENT2_WEB_ROLE"] = "viewer"
    assert client.post("/api/memories", json={"content": "off-write"}).status_code == 200


def test_a_refusal_names_the_capability_and_is_audited(client, caplog):
    os.environ["AGENT2_WEB_ROLE"] = "viewer"
    with caplog.at_level("INFO", logger="agent2"):
        resp = client.post("/api/memories", json={"content": "x"})
    assert "memory" in resp.get_json()["error"]
    assert any("authz.deny" in r.getMessage() for r in caplog.records)


def test_denials_are_counted_for_the_metrics_surface(client):
    os.environ["AGENT2_WEB_ROLE"] = "viewer"
    before = perms.counters()["denied"]
    client.post("/api/memories", json={"content": "x"})
    assert perms.counters()["denied"] == before + 1


def test_auth_status_tells_the_client_what_it_may_do(client):
    """So a UI can grey a control out instead of offering it and collecting a 403."""
    os.environ["AGENT2_WEB_ROLE"] = "operator"
    body = client.get("/api/auth/status").get_json()
    assert body["role"] == "operator"
    assert perms.CAP_CHAT in body["capabilities"]
    assert perms.CAP_SECRETS not in body["capabilities"]


def test_auth_status_tells_a_stranger_nothing_about_the_policy(client):
    body = client.get("/api/auth/status",
                      environ_base={"REMOTE_ADDR": REMOTE}).get_json()
    assert "capabilities" not in body and "role" not in body


# ══════════════════════════════════════════════════════════════════════════════
# Lookup 2 — socket events
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("event,cap", [
    ("chat_message", perms.CAP_CHAT),
    ("edit_message", perms.CAP_CHAT),
    ("stop_agent", perms.CAP_CHAT),
    ("run_raw_command", perms.CAP_EXEC),
    ("terminal_input", perms.CAP_EXEC),
    ("terminal_kill", perms.CAP_CHAT),
    ("pil_predict", perms.CAP_CHAT),
])
def test_socket_event_capability_table(event, cap):
    assert perms.capability_for_event(event) == cap


def test_every_data_handler_is_registered_through_the_guard():
    """⚠️ The check that keeps the gate total.

    `@socketio.on` used directly for a data event reopens the hole, and the
    omission is invisible: the handler works perfectly, for everyone. Only
    `connect` and `disconnect` may bypass it, for the reasons in the module
    docstring.
    """
    import inspect
    import re

    from agent2.server import sockets
    src = inspect.getsource(sockets.register_sockets)
    direct = set(re.findall(r'@socketio\.on\("([^"]+)"\)', src))
    assert direct <= {"connect", "disconnect"}, \
        f"ungated socket handlers: {sorted(direct - {'connect', 'disconnect'})}"
    guarded = set(re.findall(r'@guarded\("([^"]+)"\)', src))
    assert {"chat_message", "run_raw_command", "terminal_input"} <= guarded


def test_the_guard_wrapper_refuses_before_the_body_runs(monkeypatch):
    """Pins the ordering inside the decorator, which is where a refusal could
    otherwise happen after the shell had already been spawned."""
    import inspect

    from agent2.server import sockets
    src = inspect.getsource(sockets.register_sockets)
    body = src.split("def wrapper", 1)[1]
    assert body.index("perms.allowed") < body.index("return fn(")


# ══════════════════════════════════════════════════════════════════════════════
# Lookup 3 — agent tools
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("tool,cap", [
    ("write_file", perms.CAP_FS_WRITE),
    ("multi_edit_files", perms.CAP_FS_WRITE),
    ("delete_file", perms.CAP_FS_DELETE),
    ("run_command", perms.CAP_EXEC),
    ("save_memory", perms.CAP_MEMORY),
    ("read_file", perms.CAP_READ),
    ("grep_search", perms.CAP_READ),
])
def test_tool_capability_table(tool, cap):
    assert perms.capability_for_tool(tool) == cap


def test_a_denied_tool_returns_an_error_the_model_can_read(tmp_path):
    """⚠️ Refused as a tool `error`, never as an exception.

    The model reads tool errors and adapts. An exception aborts the turn and is
    reported to the user as a crash, which is a poor description of "this
    deployment does not allow that".
    """
    from agent2 import tools
    os.environ["AGENT2_DENY_CAPS"] = "fs.write"
    target = tmp_path / "should-not-exist.txt"
    out = tools.dispatch_tool("write_file", {"path": str(target),
                                             "content": "nope"})
    assert "error" in out
    assert "fs.write" in out["error"]
    assert not target.exists(), "the file was written despite the refusal"


def test_a_denied_delete_leaves_the_file_alone(tmp_path):
    from agent2 import tools
    from agent2.core import workspace as ws
    ws.set_workspace(str(tmp_path))
    victim = tmp_path / "keep.txt"
    victim.write_text("keep me", encoding="utf-8")

    os.environ["AGENT2_DENY_CAPS"] = "fs.delete"
    out = tools.dispatch_tool("delete_file", {"path": str(victim)})
    assert "error" in out and "fs.delete" in out["error"]
    assert victim.exists()


def test_a_read_tool_is_untouched_by_a_write_denial(tmp_path):
    from agent2 import tools
    from agent2.core import workspace as ws
    ws.set_workspace(str(tmp_path))
    src = tmp_path / "readable.txt"
    src.write_text("hello", encoding="utf-8")

    os.environ["AGENT2_DENY_CAPS"] = "fs.write,fs.delete,exec"
    out = tools.dispatch_tool("read_file", {"path": str(src)})
    assert "error" not in out, out


def test_the_tool_gate_uses_the_process_policy_not_the_web_role(tmp_path):
    """⚠️ Scope pin, and the reason there are two knobs.

    A turn's tools run on a worker thread with no client attached — it may have
    been started from the CLI, from a browser, or by task recovery on startup.
    `AGENT2_WEB_ROLE` therefore must NOT reach here, or a `viewer` deployment
    would break the CLI running in the same process (dual mode).
    """
    from agent2 import tools
    from agent2.core import workspace as ws
    ws.set_workspace(str(tmp_path))
    os.environ["AGENT2_WEB_ROLE"] = "viewer"
    target = tmp_path / "cli-write.txt"
    out = tools.dispatch_tool("write_file", {"path": str(target),
                                             "content": "cli still works"})
    assert "error" not in out, out


def test_the_web_command_runner_refuses_without_exec(monkeypatch):
    """`run_command` is the one agent tool that skips `dispatch_tool`, so the tool
    gate structurally cannot see it — this is its only chokepoint."""
    from agent2 import terminal

    class _Sock:
        def __init__(self):
            self.events = []

        def emit(self, event, payload, room=None):
            self.events.append(event)

    os.environ["AGENT2_DENY_CAPS"] = "exec"
    sock = _Sock()
    out, rc = terminal.stream_command("echo should-not-run", "sid1", "t1", sock)
    assert rc == 126
    assert "exec" in out
    # The browser terminal must be closed out or it shows a running prompt forever.
    assert "terminal_done" in sock.events


def test_a_refused_command_records_no_execution():
    """An execution that was never permitted is not a FAILED execution.

    Recording one would put a command in `/api/commands` that no process ever
    backed — and `/api/commands` is what answers "what is Agent2 doing".
    """
    from agent2.core import commands as cmds
    from agent2 import terminal

    class _Sock:
        def emit(self, *a, **k):
            pass

    before = len(cmds.list_all(limit=500))
    os.environ["AGENT2_DENY_CAPS"] = "exec"
    terminal.stream_command("echo nope", "sid2", "t1", _Sock())
    assert len(cmds.list_all(limit=500)) == before


def test_the_cli_command_runner_refuses_without_exec_and_does_not_retry(capsys):
    """⚠️ The gate is OUTSIDE the retry loop.

    Inside it, a refusal would be retried up to `CMD_MAX_RETRIES` times and print
    "↻ retrying" at a user whose deployment forbids shell execution — retrying a
    decision that cannot change.
    """
    from agent2.cli import runtime

    os.environ["AGENT2_DENY_CAPS"] = "exec"
    out, err, rc, dur = runtime.run_cmd_stream("echo should-not-run")
    assert rc == 126 and out == ""
    assert "exec" in err
    assert "retrying" not in capsys.readouterr().out


# ══════════════════════════════════════════════════════════════════════════════
# Shape / reporting
# ══════════════════════════════════════════════════════════════════════════════

def test_describe_is_serialisable_and_carries_no_secret():
    import json
    snap = perms.describe()
    assert set(snap) >= {"role", "capabilities", "denied", "counters"}
    json.dumps(snap)


def test_platform_reports_the_policy(client):
    # `providers` is owned by llm/providers.py, not by init_db() — see
    # database.py's note on migration 5. /api/platform reads it, so the table has
    # to exist before this route can answer at all.
    from agent2.llm import providers as _providers
    _providers.init_providers_table()
    body = client.get("/api/platform").get_json()
    assert body["permissions"]["role"] == "owner"


def test_every_capability_in_a_lookup_table_is_a_declared_capability():
    """⚠️ Guards against a typo'd capability string in one of the three tables.

    A rule mapping to `"fs.wrote"` would be a capability no role holds, so the
    route would be refused for *everyone* — a total outage of one endpoint that no
    other test in this file would attribute to a typo.
    """
    seen = {cap for _, _, cap in perms._ROUTE_RULES}
    seen |= set(perms._SOCKET_CAPS.values())
    seen |= set(perms._TOOL_CAPS.values())
    unknown = seen - set(perms.ALL_CAPS)
    assert not unknown, f"undeclared capabilities in a lookup table: {unknown}"


def test_every_declared_capability_is_held_by_at_least_the_owner():
    for cap in perms.ALL_CAPS:
        assert cap in perms.ROLES[perms.ROLE_OWNER], cap


def test_sensitive_is_a_subset_of_all_caps():
    assert perms.SENSITIVE <= frozenset(perms.ALL_CAPS)
    assert perms.CAP_READ not in perms.SENSITIVE, \
        "logging every read would bury the audit trail it is meant to be"
