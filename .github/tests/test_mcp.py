# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
Tests for the MCP layer: the shared bridge, the OWASP ZAP server added beside
Burp, the registry that lists both, and the `/mcp` menu that toggles them.

Run from the repo root:  python -m pytest .github/tests/test_mcp.py -v

Coverage (Task 8)
  - `McpBridge` — the prefix invariant, transport probing, the two disagreeing
    schema converters, `prompt_block()`, and `status()` doing no I/O
  - `ZapMCP` — its Authorization header, and that the key never leaves it
  - `registry` — the table's shape and order, totality, and `resolve()`
  - both agent loops — the new `mcp` meta key round-tripping through
    `_tool_name` / `_tool_args` / `build_context`
  - `cli.tooling.dispatch_tool` — MCP routing added WITHOUT disturbing Burp or
    the unregistered-tool message
  - the `cancellable` toggle-menu contract, in the prompt_toolkit path and in
    the numbered-loop fallback

⚠️ The load-bearing tests here are
``test_no_mcp_tool_name_can_shadow_a_local_tool`` (the single reason the two
agent loops are allowed to check local tools and bridges in OPPOSITE orders) and
``test_a_cancelled_menu_is_not_an_empty_menu`` (Esc on `/mcp` must not read as
"turn everything off", which would disconnect a live pentest session).

Nothing here touches the network: a "connected" bridge is faked by installing a
sentinel session + loop, which is exactly what `is_connected()` reads.
"""

import json
import sys
import uuid

import pytest

from agent2 import config
from agent2.database import exe, init_db, qall
from agent2.integrations import registry as R
from agent2.integrations.burp_mcp import burp
from agent2.integrations.mcp_base import McpBridge
from agent2.integrations.zap_mcp import zap


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fake_connect(bridge: McpBridge, tools: tuple[str, ...] = ("scan", "alerts_list")):
    """Put *bridge* into the state `is_connected()` reports as connected.

    `is_connected()` is `self._session is not None and self._loop is not None`,
    so two sentinels are enough — and no socket is opened, which is the point.
    Returns the sanitized (prefixed) tool names.
    """
    bridge._session = object()
    bridge._loop = object()
    bridge._tools = [
        {"real_name": t, "name": bridge.sanitize_name(t),
         "description": f"{t} description", "schema": {}}
        for t in tools
    ]
    bridge._tool_names = {t["name"] for t in bridge._tools}
    bridge._name_map = {t["name"]: t["real_name"] for t in bridge._tools}
    bridge._connected_url = bridge.url
    return [t["name"] for t in bridge._tools]


def _reset(bridge: McpBridge):
    bridge._session = None
    bridge._loop = None
    bridge._tools = []
    bridge._tool_names = set()
    bridge._name_map = {}
    bridge._connected_url = ""
    bridge._last_error = ""


@pytest.fixture(autouse=True)
def _clean_bridges():
    """The bridges are process-global singletons — restore them around each test.

    ⚠️ NOTHING HERE IS RESTORED BY ASSIGNING TO A BRIDGE ATTRIBUTE ANY MORE.
    `enabled` (Task 9), `url` and `zap.key` (the config redesign) are all
    read-through properties whose setters WRITE TO THE DATABASE, so `b.url = url`
    would not restore state — it would manufacture a row and turn "unconfigured"
    into "explicitly configured" for every later test. Both tables are
    snapshotted and put back verbatim instead, and the cache is dropped either
    way so the next read cannot be served a value from the test that just ran.

    ⚠️ `mcp_config` MUST be cleaned as well as `mcp_state`, and the reason is a
    credential: a test that stores a fake ZAP key now leaves it in the DB, where
    the next test's `auth_headers()` would pick it up and quietly pass.
    """
    from agent2.integrations import state as S
    try:
        rows = qall("SELECT project, server, enabled FROM mcp_state", ())
    except Exception:
        rows = []
    try:
        cfg = qall("SELECT server, url, security_key FROM mcp_config", ())
    except Exception:
        cfg = []
    yield
    for b in R.bridges():
        _reset(b)
    try:
        exe("DELETE FROM mcp_state")
        for r in rows:
            exe("INSERT INTO mcp_state(project, server, enabled) VALUES(?,?,?)",
                (r["project"], r["server"], r["enabled"]))
    except Exception:
        pass
    try:
        exe("DELETE FROM mcp_config")
        for r in cfg:
            exe("INSERT INTO mcp_config(server, url, security_key) VALUES(?,?,?)",
                (r["server"], r["url"], r["security_key"]))
    except Exception:
        pass
    S.invalidate()


# ── The registry is a table, not a plugin loader ───────────────────────────────

def test_the_registry_lists_burp_first_then_zap():
    """⚠️ Order is the UI order: `/mcp`, health and the toast sequence read it."""
    keys = [b.SERVER_KEY for b in R.bridges()]
    assert keys[0] == "burp", "Burp must stay first — it is the one that existed"
    assert "zap" in keys


def test_adding_zap_did_not_remove_burp():
    """The spec's first constraint: do not remove or break Burp."""
    assert R.get("burp") is burp
    assert R.get("zap") is zap


def test_extra_bridges_excludes_burp_only():
    extra = R.extra_bridges()
    assert burp not in extra
    assert zap in extra
    assert len(extra) == len(R.bridges()) - 1


def test_get_is_case_insensitive_and_total():
    assert R.get("ZAP") is zap
    assert R.get("  burp  ") is burp
    assert R.get("nope") is None
    assert R.get("") is None


def test_the_server_table_is_hardcoded_not_discovered():
    """⚠️ A plugin architecture was explicitly ruled out. This pins that."""
    import inspect
    src = inspect.getsource(R)
    for banned in ("iterdir", "glob(", "entry_points", "pkgutil", "importlib.metadata"):
        assert banned not in src, f"registry started discovering servers: {banned!r}"


# ── The prefix invariant (the reason the base is shared, not copied) ───────────

def test_sanitize_name_forces_the_server_prefix():
    assert burp.sanitize_name("proxy_history") == "burp_proxy_history"
    assert zap.sanitize_name("alerts") == "zap_alerts"


def test_a_name_that_already_carries_the_prefix_is_not_doubled():
    assert zap.sanitize_name("zap_alerts") == "zap_alerts"


def test_sanitize_name_maps_illegal_chars_and_caps_length():
    """'.' is legal on Gemini but rejected by OpenAI/Anthropic — 63 is the floor."""
    assert zap.sanitize_name("core.view.alerts") == "zap_core_view_alerts"
    assert len(zap.sanitize_name("x" * 200)) == 63


def test_no_mcp_tool_name_can_shadow_a_local_tool():
    """⚠️ THE INVARIANT THE WHOLE DISPATCH CHAIN RESTS ON.

    `agent.py` checks `_LOCAL_TOOLS` BEFORE the bridges; `provider_agent.py`
    checks the bridges BEFORE `_LOCAL_TOOLS`. Opposite orders are harmless only
    because a prefixed MCP name can never equal a local one. If this fails, a
    server exposing `read_file` silently hijacks the local tool in one loop and
    not the other — and the model reports no error either way.
    """
    from agent2.agent import _LOCAL_TOOLS
    for local in _LOCAL_TOOLS:
        for bridge in R.bridges():
            assert bridge.sanitize_name(local) != local
            assert bridge.sanitize_name(local) not in _LOCAL_TOOLS


def test_two_servers_exposing_the_same_tool_do_not_collide():
    assert burp.sanitize_name("scan") != zap.sanitize_name("scan")


def test_every_bridge_has_a_distinct_key_prefix_and_thread_name():
    bs = R.bridges()
    for attr in ("SERVER_KEY", "PREFIX", "THREAD_NAME"):
        vals = [getattr(b, attr) for b in bs]
        assert len(set(vals)) == len(vals), f"duplicate {attr}: {vals}"
    for b in bs:
        assert b.PREFIX.endswith("_"), f"{b.SERVER_KEY} prefix must end with _"


# ── Tool ownership ────────────────────────────────────────────────────────────

def test_a_disconnected_bridge_claims_no_tools():
    """⚠️ `is_tool` is membership, never a prefix test."""
    assert zap.is_tool("zap_alerts") is False
    assert R.resolve("zap_alerts") is None


def test_resolve_finds_the_owning_bridge_when_connected():
    names = _fake_connect(zap)
    assert R.resolve(names[0]) is zap
    assert zap.is_tool(names[0]) is True


def test_resolve_stops_claiming_after_a_disconnect():
    """A tool call that arrives late must fall through, not enter a dead session."""
    names = _fake_connect(zap)
    assert R.resolve(names[0]) is zap
    _reset(zap)
    assert R.resolve(names[0]) is None


def test_resolve_is_total_and_never_raises():
    assert R.resolve("") is None
    assert R.resolve(None) is None
    assert R.resolve("no_such_tool") is None


def test_call_tool_on_a_disconnected_bridge_is_an_error_not_an_exception():
    out = zap.call_tool("zap_alerts", {})
    assert "error" in out
    assert "OWASP ZAP" in out["error"]


# ── Transport probing ─────────────────────────────────────────────────────────

def test_burp_probes_sse_only_and_appends_the_conventional_path():
    burp.url = "http://127.0.0.1:9876"
    eps = burp.endpoints()
    assert [t for t, _u in eps] == ["sse", "sse"]
    assert eps[0][1] == "http://127.0.0.1:9876"
    assert eps[1][1] == "http://127.0.0.1:9876/sse"


def test_zap_probes_streamable_http_before_sse():
    """⚠️ ZAP's add-on documents no path and names no transport, so it probes."""
    zap.url = "http://127.0.0.1:8282"
    order = [t for t, _u in zap.endpoints()]
    assert order.index("streamable_http") < order.index("sse")
    urls = [u for _t, u in zap.endpoints()]
    assert "http://127.0.0.1:8282/mcp" in urls
    assert "http://127.0.0.1:8282/sse" in urls


def test_a_pasted_full_endpoint_is_tried_first_and_never_second_guessed():
    zap.url = "http://127.0.0.1:8282/mcp"
    assert zap.endpoints()[0][1] == "http://127.0.0.1:8282/mcp"


def test_walk_deduplicates_and_tolerates_an_empty_url():
    assert McpBridge._walk("", ("sse",), {"sse": "/sse"}) == []
    pairs = McpBridge._walk("http://h/sse", ("sse",), {"sse": "/sse"})
    assert pairs == [("sse", "http://h/sse")]


def test_a_url_that_already_ends_in_the_suffix_gets_no_doubled_candidate():
    """⚠️ `…/sse` must not also probe `…/sse/sse` — one more doomed handshake.

    The original `_candidate_urls` guarded this by name (`if not
    base.endswith("/sse")`); generalising it to N transports is exactly where
    that guard goes missing without anyone noticing, because the extra attempt
    only ever costs latency.
    """
    burp.url = "http://127.0.0.1:9876/sse"
    assert burp.endpoints() == [("sse", "http://127.0.0.1:9876/sse")]
    zap.url = "http://127.0.0.1:8282/mcp"
    assert "http://127.0.0.1:8282/mcp/mcp" not in [u for _t, u in zap.endpoints()]


def test_candidate_urls_shim_still_answers_for_burp():
    """`burp_mcp._candidate_urls` is a documented back-compat shim."""
    from agent2.integrations.burp_mcp import _candidate_urls, _sanitize_name
    assert _candidate_urls("http://h") == ["http://h", "http://h/sse"]
    assert _sanitize_name("scan") == "burp_scan"


# ── The ZAP security key ──────────────────────────────────────────────────────

def test_zap_sends_the_key_as_the_authorization_value_verbatim():
    """⚠️ ZAP prepends no `Bearer`, so neither do we."""
    zap.set_key("s3cret-key")
    assert zap.auth_headers() == {"Authorization": "s3cret-key"}


def test_no_key_means_no_auth_header_at_all():
    zap.set_key("")
    assert zap.auth_headers() == {}


def test_the_key_never_reaches_status_or_statuses():
    """⚠️ `/api/health` and the CLI both render these. A credential may not ride along."""
    zap.set_key("s3cret-key")
    blob = json.dumps(zap.status()) + json.dumps(R.statuses())
    assert "s3cret-key" not in blob


def test_the_key_lands_in_mcp_config_and_nowhere_else():
    """⚠️ THIS TEST HAS INVERTED TWICE, AND SAYING SO IS THE POINT.

    v1 asserted the key was never written at all — correct while the only way to
    supply one was `ZAP_MCP_KEY`, and wrong the moment `/mcp zap config` and the web
    form had to make it durable.

    v2 asserted WHERE: exactly one column, `mcp_config.security_key`. Not
    `settings`, whose rows are dumped wholesale by several surfaces and were the
    original leak this test was written to block. It also asserted the *plaintext*
    sat in that column, and flagged that as a known, bounded compromise pending
    Task 16.

    v3 (now) is Task 16 landing. The location guarantee is unchanged and still
    pinned; what changed is that the column holds a `a2s:` REFERENCE, so the
    plaintext appears nowhere in the database at all. Both halves matter and each
    is useless alone: that the credential is not readable from the DB, and that it
    still survives a restart — a "secure" store that forgot the key would just be
    the v1 bug wearing a better name.
    """
    init_db()
    zap.set_key("db-leak-canary")

    rows = qall("SELECT key, value FROM settings")
    assert not any("db-leak-canary" in str(r["value"]) for r in rows), \
        "the key leaked into `settings`, which surfaces dump wholesale"

    stored = qall("SELECT server, security_key FROM mcp_config WHERE server='zap'", ())
    assert len(stored) == 1, "the key must survive a restart, or /mcp zap config " \
                             "forgets it every launch"
    held = str(stored[0]["security_key"])
    assert "db-leak-canary" not in held, \
        "Task 16: the plaintext key is still sitting in mcp_config"
    assert held.startswith("a2s:"), f"expected a SecretStore reference, got {held[:16]!r}"

    # …and the transport still gets the real thing back.
    assert zap.key == "db-leak-canary"
    assert zap.auth_headers().get("Authorization") == "db-leak-canary"


def test_no_plaintext_credential_is_left_anywhere_in_the_database():
    """Task 16, stated as the property a user actually cares about.

    The threat is not a specific column — it is `sqlite3 agent2.db .dump` in a bug
    report, a copied `/data` volume, a backup on a shared drive. So this sweeps
    EVERY table for the canaries rather than trusting a list of the three columns we
    happen to remember, which is what would go stale the moment a fourth credential
    is added.
    """
    from agent2.database import add_api_key
    from agent2.llm import providers as P

    init_db()
    P.init_providers_table()
    zap.set_key("canary-zap-9c1f")
    add_api_key("AIzaCANARY-gemini-key-0001")
    P.add_provider("t", "https://example.invalid", "sk-canary-provider-0002",
                   "m", "openai")

    canaries = ("canary-zap-9c1f", "AIzaCANARY-gemini-key-0001",
                "sk-canary-provider-0002")
    tables = [r["name"] for r in
              qall("SELECT name FROM sqlite_master WHERE type='table'")]
    for table in tables:
        blob = json.dumps([dict(r) for r in qall(f"SELECT * FROM {table}")],  # noqa: S608
                          default=str)
        for canary in canaries:
            assert canary not in blob, f"{canary} is readable in `{table}`"


# ── status() is the health probe's input ──────────────────────────────────────

def test_status_carries_the_seven_keys_the_browser_reads():
    expected = {"enabled", "connected", "url", "tool_count",
                "tools", "mcp_installed", "last_error"}
    assert expected <= set(burp.status())
    assert expected <= set(zap.status())


def test_statuses_adds_the_key_and_label_and_nothing_else():
    rows = R.statuses()
    assert [r["key"] for r in rows][0] == "burp"
    for r in rows:
        assert r["label"]
        assert set(r) == set(burp.status()) | {"key", "label"}


def test_status_does_no_io(monkeypatch):
    """⚠️ Pure attribute reads. A probe that dials a socket blocks /api/health."""
    import socket

    def _boom(*a, **k):
        raise AssertionError("status() opened a socket")

    monkeypatch.setattr(socket, "create_connection", _boom)
    monkeypatch.setattr(socket.socket, "connect", _boom)
    zap.status()
    burp.status()
    R.statuses()


def test_status_reports_the_url_that_actually_connected():
    zap.url = "http://127.0.0.1:8282"
    _fake_connect(zap)
    zap._connected_url = "http://127.0.0.1:8282/mcp"
    assert zap.status()["url"] == "http://127.0.0.1:8282/mcp"


# ── The two schema converters disagree on purpose ─────────────────────────────

def test_gemini_gets_none_for_an_empty_object_schema():
    """Gemini rejects an OBJECT with no properties; the provider wire needs it."""
    _fake_connect(zap, ("alerts",))
    decls = zap.gemini_declarations()
    assert len(decls) == 1
    assert decls[0].name == "zap_alerts"
    assert decls[0].parameters is None


def test_providers_get_a_real_object_for_the_same_empty_schema():
    _fake_connect(zap, ("alerts",))
    schemas = zap.provider_tool_schemas()
    assert schemas[0]["parameters"] == {"type": "object", "properties": {}}
    assert schemas[0]["name"] == "zap_alerts"


def test_both_converters_label_the_tool_with_its_server():
    _fake_connect(zap, ("alerts",))
    assert "[OWASP ZAP]" in zap.gemini_declarations()[0].description
    assert "[OWASP ZAP]" in zap.provider_tool_schemas()[0]["description"]


def test_a_tool_with_no_input_schema_is_still_offered_to_gemini():
    """⚠️ A missing `inputSchema` must not delete the tool from the toolbox.

    MCP lets a server omit the schema for a no-argument tool, and the bridge
    caches that as `{}`. Fed straight to the converter, `{}` becomes
    Schema(type=STRING) — not an OBJECT, so the empty-object stub never fires and
    Gemini rejects the declaration for non-object parameters. The tool then
    vanishes silently: no exception, no log line, the model simply never sees it.
    """
    _fake_connect(zap, ("alerts",))
    for bogus in ({}, None, {"type": "string"}, "not a dict"):
        zap._tools[0]["schema"] = bogus
        decls = zap.gemini_declarations()
        assert len(decls) == 1, f"tool dropped for schema {bogus!r}"
        assert decls[0].parameters is None, f"non-None parameters for {bogus!r}"


def test_the_two_converters_agree_on_which_tools_exist():
    """They differ on schema SHAPE, never on membership — a tool in one is in both."""
    _fake_connect(zap, ("alerts", "spider", "active_scan"))
    zap._tools[1]["schema"] = {}
    zap._tools[2]["schema"] = {"type": "object",
                               "properties": {"url": {"type": "string"}},
                               "required": ["url"]}
    assert ([d.name for d in zap.gemini_declarations()]
            == [s["name"] for s in zap.provider_tool_schemas()])


def test_a_real_object_schema_survives_conversion_to_both_wires():
    _fake_connect(zap, ("spider",))
    zap._tools[0]["schema"] = {"type": "object",
                               "properties": {"url": {"type": "string"}},
                               "required": ["url"]}
    params = zap.gemini_declarations()[0].parameters
    assert params is not None
    assert "url" in params.properties
    assert params.required == ["url"]
    assert zap.provider_tool_schemas()[0]["parameters"]["required"] == ["url"]


def test_the_registry_offers_tools_only_for_connected_bridges():
    assert R.gemini_declarations_for(R.extra_bridges()) == []
    assert R.provider_schemas_for(R.extra_bridges()) == []
    _fake_connect(zap, ("alerts",))
    assert len(R.gemini_declarations_for(R.extra_bridges())) == 1
    assert len(R.provider_schemas_for(R.extra_bridges())) == 1


# ── Auto-connect: never dial a security tool the user did not enable ──────────

def test_ensure_connected_does_nothing_for_a_disabled_bridge():
    zap.enabled = False
    assert R.ensure_connected(zap) is None


def test_ensure_connected_does_nothing_when_already_connected():
    zap.enabled = True
    _fake_connect(zap)
    assert R.ensure_connected(zap) is None


def test_ensure_connected_attempts_only_when_enabled_and_down(monkeypatch):
    calls = []
    monkeypatch.setattr(type(zap), "connect",
                        lambda self, timeout=8.0: (calls.append(timeout), (True, "up"))[1])
    zap.enabled = True
    assert R.ensure_connected(zap, timeout=3.0) == (True, "up")
    assert calls == [3.0]


def test_ensure_connected_reports_a_raising_bridge_instead_of_raising(monkeypatch):
    """⚠️ Total: a broken bridge may not cost the user their turn."""
    def _raise(self, timeout=8.0):
        raise RuntimeError("boom")

    monkeypatch.setattr(type(zap), "connect", _raise)
    zap.enabled = True
    ok, msg = R.ensure_connected(zap)
    assert ok is False
    assert "boom" in msg


# ── Persisted auto-connect (shared by CLI and Web) ────────────────────────────
# Task 9 moved the WRITE to a per-project `mcp_state` row. The two legacy global
# settings stay READABLE as the fallback — see `integrations/state.py`.

def test_zap_auto_connect_persists_per_project():
    from agent2.integrations import state as S
    init_db()
    zap.set_auto_connect(True)
    assert S.states_for_project()["zap"] is True
    assert zap.enabled is True
    zap.set_auto_connect(False)
    assert S.states_for_project()["zap"] is False
    assert zap.enabled is False


def test_burp_auto_connect_persists_per_project_and_load_enabled_still_answers():
    """`_load_enabled` is back-compat: it must AGREE with the property, not lag."""
    from agent2.integrations import state as S
    init_db()
    burp.set_auto_connect(True)
    assert S.states_for_project()["burp"] is True
    assert burp._load_enabled() is True
    assert burp._load_auto_connect() is True
    burp.set_auto_connect(False)
    assert burp._load_enabled() is False


def test_a_toggle_never_writes_the_legacy_global_setting():
    """⚠️ Rule 30: the global is the fallback for every UNCONFIGURED project.

    Writing it on a toggle would make "ZAP off in this checkout" also mean "ZAP
    off in every project that has never been configured" — the cross-project
    bleed this table exists to remove. Sabotage: add a `set_setting` beside the
    row write and this fails.
    """
    from agent2.database import get_setting
    init_db()
    exe("DELETE FROM settings WHERE key IN ('burp_auto_connect','zap_auto_connect')")
    burp.set_auto_connect(True)
    zap.set_auto_connect(True)
    assert get_setting("burp_auto_connect") is None
    assert get_setting("zap_auto_connect") is None


def test_every_bridge_implements_set_auto_connect():
    """⚠️ The base defines it so no caller needs a `hasattr` check.

    A `hasattr` guard would silently do nothing on the one subclass that forgot
    it — leaving `/mcp` showing ON and the server off.
    """
    for b in R.bridges():
        assert callable(b.set_auto_connect)


# ── The system-prompt block ───────────────────────────────────────────────────

def test_prompt_block_is_empty_when_the_server_is_not_connected():
    assert zap.prompt_block() == ""


def test_prompt_block_names_the_prefix_when_connected():
    """⚠️ Telling the model the prefix is the point — otherwise it shells out."""
    _fake_connect(zap, ("active_scan",))
    block = zap.prompt_block()
    assert "OWASP ZAP" in block
    assert "`zap_`" in block
    assert "run_command" in block


def test_prompt_block_is_empty_when_connected_but_toolless():
    _fake_connect(zap, ())
    assert zap.prompt_block() == ""


def test_the_burp_block_is_still_the_hand_written_one(monkeypatch):
    """The pinned off-branch: no Burp text unless Burp says it is connected."""
    from agent2.agent import system_prompt
    assert "BURP SUITE" not in system_prompt(burp_connected=False)
    assert "BURP SUITE" in system_prompt(burp_connected=True, burp_tool_count=3)


def test_mcp_blocks_are_appended_before_memories_and_rules():
    from agent2.agent import system_prompt
    sp = system_prompt(mcp_blocks=["\n\n## ZAPBLOCK\n"])
    assert "ZAPBLOCK" in sp
    assert "BURP SUITE" not in sp


def test_the_cli_prompt_builder_takes_the_same_blocks():
    from agent2.cli.prompt import build_sys_prompt
    sp = build_sys_prompt(mcp_blocks=["\n\n## ZAPBLOCK\n"])
    assert "ZAPBLOCK" in sp


# ── The `mcp` meta key round-trips through both agent loops ───────────────────

def test_tool_name_reads_the_mcp_meta_key():
    from agent2.agent import _tool_name
    assert _tool_name({"mcp": "zap_alerts"}) == "zap_alerts"
    assert _tool_name({"burp": "burp_scan"}) == "burp_scan"
    assert _tool_name({"local": "read_file"}) == "read_file"
    assert _tool_name({"cmd": "ls"}) == "run_command"


def test_tool_args_returns_empty_for_an_mcp_call():
    from agent2.agent import _tool_args
    assert _tool_args({"mcp": "zap_alerts"}, "") == {}


def test_provider_meta_tags_an_mcp_call_with_exactly_one_key():
    """⚠️ Exactly one of cmd / burp / mcp / local, or the call and its result disagree."""
    from agent2.llm.provider_agent import _tool_meta
    names = _fake_connect(zap)
    meta = _tool_meta(names[0], {"a": 1})
    assert meta["mcp"] == names[0]
    assert meta["server"] == "zap"
    assert "local" not in meta and "burp" not in meta and "cmd" not in meta


def test_provider_meta_falls_back_to_local_for_an_unknown_name():
    from agent2.llm.provider_agent import _tool_meta
    meta = _tool_meta("read_file", {})
    assert meta["local"] == "read_file"
    assert "mcp" not in meta


def test_build_context_grades_an_mcp_result_by_its_rc():
    """⚠️ An MCP result carries a VERDICT, never an exit code.

    `rc` is only the storage slot for that verdict. The distinguishing assertion
    is the absent `returncode`: the shell fallback branch computes the same
    `success`, so a missing `mcp` branch would look identical here while quietly
    telling the model "returncode: 0" about a scanner call that never had one —
    a number it will then reason about.
    """
    from agent2.agent import build_context
    init_db()
    cid = str(uuid.uuid4())
    exe("INSERT INTO chats(id, title) VALUES(?, 'mcp')", (cid,))
    try:
        exe("INSERT INTO messages(chat_id, role, content, meta) VALUES(?,?,?,?)",
            (cid, "tool_call", "OWASP ZAP: zap_alerts",
             json.dumps({"args": {}, "mcp": "zap_alerts", "server": "zap"})))
        exe("INSERT INTO messages(chat_id, role, content, meta) VALUES(?,?,?,?)",
            (cid, "tool_result", "2 alerts",
             json.dumps({"rc": 0, "mcp": "zap_alerts", "server": "zap"})))
        ctx = build_context(cid)
        parts = [p for c in ctx for p in (c.parts or [])]
        responses = [p.function_response for p in parts if p.function_response]
        assert responses, "the MCP tool result never became a function_response"
        assert responses[-1].name == "zap_alerts"
        assert responses[-1].response["success"] is True
        assert "returncode" not in responses[-1].response, (
            "an MCP result was graded through the SHELL branch — the model is "
            "being told an exit code that does not exist"
        )
    finally:
        exe("DELETE FROM messages WHERE chat_id=?", (cid,))
        exe("DELETE FROM chats WHERE id=?", (cid,))


def test_build_context_marks_a_failed_mcp_result_unsuccessful():
    from agent2.agent import build_context
    init_db()
    cid = str(uuid.uuid4())
    exe("INSERT INTO chats(id, title) VALUES(?, 'mcp')", (cid,))
    try:
        exe("INSERT INTO messages(chat_id, role, content, meta) VALUES(?,?,?,?)",
            (cid, "tool_call", "OWASP ZAP: zap_alerts",
             json.dumps({"args": {}, "mcp": "zap_alerts"})))
        exe("INSERT INTO messages(chat_id, role, content, meta) VALUES(?,?,?,?)",
            (cid, "tool_result", "refused", json.dumps({"rc": 1, "mcp": "zap_alerts"})))
        ctx = build_context(cid)
        parts = [p for c in ctx for p in (c.parts or [])]
        responses = [p.function_response for p in parts if p.function_response]
        assert responses[-1].response["success"] is False
        assert "returncode" not in responses[-1].response
    finally:
        exe("DELETE FROM messages WHERE chat_id=?", (cid,))
        exe("DELETE FROM chats WHERE id=?", (cid,))


# ── CLI tool dispatch ─────────────────────────────────────────────────────────

def test_dispatch_tool_routes_an_mcp_tool_to_its_bridge(monkeypatch):
    from agent2.cli import tooling
    names = _fake_connect(zap)
    monkeypatch.setattr(type(zap), "call_tool",
                        lambda self, n, a, **k: {"output": f"ran {n}", "success": True})
    out = tooling.dispatch_tool(names[0], {})
    assert out["output"] == f"ran {names[0]}"


def test_dispatch_tool_keeps_the_exact_unregistered_message():
    """Section 4 of the tool contract — the model is trained on this wording."""
    from agent2.cli import tooling
    assert tooling.dispatch_tool("nope", {}) == {"error": 'Tool "nope" is not registered.'}


def test_burp_is_still_checked_before_the_registry(monkeypatch):
    """⚠️ `resolve()` answers for Burp too; Burp's line is the one with callers."""
    from agent2.cli import tooling
    names = _fake_connect(burp, ("scan",))
    seen = []
    monkeypatch.setattr(type(burp), "call_tool",
                        lambda self, n, a, **k: (seen.append("burp"), {"output": "b"})[1])
    monkeypatch.setattr(R, "resolve",
                        lambda n: (_ for _ in ()).throw(AssertionError("registry ran first")))
    assert tooling.dispatch_tool(names[0], {})["output"] == "b"
    assert seen == ["burp"]


# ── The `/mcp` command surface ────────────────────────────────────────────────

def test_slash_mcp_is_registered_for_help_and_autocomplete():
    from agent2.cli.render import SLASH_COMMANDS
    bases = [base for _tok, base, _desc in SLASH_COMMANDS]
    assert "/mcp" in bases


def test_slash_burp_is_retired_from_help_but_still_runs(monkeypatch):
    """⚠️ RETIRED IS NOT DELETED, and the two halves pull opposite ways.

    The user asked for one MCP command, so `/burp` is gone from `/help` and from
    the completer — a deprecation that keeps advertising itself never finishes.
    But rule 28 forbids silently removing a feature, and `/burp connect` is in
    every older doc and in muscle memory, so typing it must still work rather
    than print "unknown command". This pins both halves at once: absent from the
    table, and forwarding to the `/mcp` implementation when typed.
    """
    from agent2.cli.render import SLASH_COMMANDS
    bases = [base for _tok, base, _desc in SLASH_COMMANDS]
    assert "/burp" not in bases, "/burp should no longer be advertised"

    import agent2cli
    seen = []
    monkeypatch.setattr(agent2cli, "cmd_mcp", lambda s="/mcp": seen.append(s))
    agent2cli.cmd_burp("/burp connect")
    agent2cli.cmd_burp("/burp")
    assert seen == ["/mcp burp connect", "/mcp burp"]


def test_the_slash_mcp_description_teaches_the_spec_controls():
    """The Task 8 spec prints the menu's controls; `/help` is where they live."""
    from agent2.cli.render import SLASH_COMMANDS
    desc = next(d for _t, b, d in SLASH_COMMANDS if b == "/mcp")
    for control in ("Space", "Enter", "Esc"):
        assert control in desc


def test_the_slash_mcp_description_teaches_the_subcommands():
    """Bare `/mcp` is a menu, so the grammar has nowhere else to be discovered."""
    from agent2.cli.render import SLASH_COMMANDS
    desc = next(d for _t, b, d in SLASH_COMMANDS if b == "/mcp").lower()
    for word in ("connect", "config"):
        assert word in desc


def test_cmd_mcp_exists_and_is_dispatched():
    import inspect

    import agent2cli
    assert callable(agent2cli.cmd_mcp)
    src = inspect.getsource(agent2cli)
    assert 'low.startswith("/mcp")' in src
    assert 'low.startswith("/burp")' in src


def test_mcp_apply_connects_and_disconnects_the_real_session(monkeypatch):
    """⚠️ The toggle IS the connection, not just a saved preference."""
    import agent2cli
    events = []
    monkeypatch.setattr(type(zap), "connect",
                        lambda self, timeout=12.0: (events.append("connect"), (True, "up"))[1])
    monkeypatch.setattr(type(zap), "disconnect", lambda self: events.append("disconnect"))
    monkeypatch.setattr(type(zap), "set_auto_connect",
                        lambda self, on: events.append(f"auto={on}"))

    agent2cli._mcp_apply(zap, True)
    assert events == ["auto=True", "connect"]

    events.clear()
    agent2cli._mcp_apply(zap, False)
    assert events == ["auto=False", "disconnect"]


def test_mcp_apply_does_not_reconnect_an_already_connected_bridge(monkeypatch):
    import agent2cli
    _fake_connect(zap)
    monkeypatch.setattr(type(zap), "connect",
                        lambda self, timeout=12.0: pytest.fail("reconnected a live session"))
    ok, msg = agent2cli._mcp_apply(zap, True)
    assert ok is True
    assert "already connected" in msg


# ── The cancellable toggle menu (Enter applies · Esc discards) ────────────────

def test_a_cancelled_menu_is_not_an_empty_menu():
    """⚠️ THE DISTINCTION `/mcp` DEPENDS ON.

    Esc must be expressible as "the user backed out" — `{}` already means
    "nothing is configured", and reading a cancel as an all-off dict would
    disconnect every live MCP session the moment someone pressed Esc.
    """
    from agent2.cli.palette import ephemeral_toggle_menu
    assert ephemeral_toggle_menu("t", [], cancellable=True) is None
    assert ephemeral_toggle_menu("t", []) == {}


def test_the_fallback_menu_honours_cancel_the_same_way(monkeypatch):
    """The least-tested install must not APPLY what a cancel discarded."""
    from agent2.cli import interactive

    monkeypatch.setattr(interactive, "_PTK", False, raising=False)
    monkeypatch.setattr(interactive, "Application", None, raising=False)
    items = [{"key": "zap", "label": "OWASP ZAP", "on": False}]

    monkeypatch.setattr("builtins.input", lambda *a: "q")
    assert interactive.toggle_menu("MCP", items, cancellable=True) is None

    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(KeyboardInterrupt))
    assert interactive.toggle_menu("MCP", items, cancellable=True) is None


def test_the_fallback_menu_still_applies_on_enter(monkeypatch):
    from agent2.cli import interactive

    monkeypatch.setattr(interactive, "_PTK", False, raising=False)
    monkeypatch.setattr(interactive, "Application", None, raising=False)
    items = [{"key": "zap", "label": "OWASP ZAP", "on": True}]
    monkeypatch.setattr("builtins.input", lambda *a: "")
    assert interactive.toggle_menu("MCP", items, cancellable=True) == {"zap": True}


def test_a_non_cancellable_menu_keeps_its_old_esc_means_done_contract(monkeypatch):
    """`/offline` and `/keys` were written against Esc == Enter. Do not change it."""
    from agent2.cli import interactive

    monkeypatch.setattr(interactive, "_PTK", False, raising=False)
    monkeypatch.setattr(interactive, "Application", None, raising=False)
    items = [{"key": "pred", "label": "Prediction", "on": True}]
    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(EOFError))
    assert interactive.toggle_menu("PIL", items) == {"pred": True}


def test_both_menus_advertise_the_controls_the_spec_names():
    """↑↓ Navigate · Space Toggle · Enter Apply · Esc Cancel."""
    import inspect

    from agent2.cli import interactive, palette
    for mod in (palette._run_toggle_menu, interactive.toggle_menu):
        src = inspect.getsource(mod)
        assert "Space toggle · Enter apply · Esc cancel" in src
        assert '@kb.add("space")' in src or "Number to toggle" in src


# ── Config defaults mirror the ZAP add-on's own defaults ──────────────────────

def test_zap_config_defaults_match_the_addon():
    """Port 8282 (not 8080 — that is the proxy), and the server ships disabled."""
    assert "8282" in config.ZAP_MCP_URL
    assert config.ZAP_MCP_ENABLED is False


def test_burp_config_defaults_are_untouched():
    assert "9876" in config.BURP_MCP_URL


def test_the_bridges_take_their_urls_from_config():
    assert zap.DEFAULT_URL == config.ZAP_MCP_URL
    assert burp.DEFAULT_URL == config.BURP_MCP_URL


def test_every_bridge_offers_a_setup_hint_for_the_settings_prompt():
    for b in R.bridges():
        assert b.SETUP_HINT, f"{b.SERVER_KEY} has no setup hint for [S]ettings"


# ── The web surface sees every server, not just Burp ──────────────────────────
# Rules 9/10: both surfaces share one backend. The CLI got `/mcp`; without these
# routes the browser could see and toggle Burp and would be blind to ZAP.

@pytest.fixture
def client():
    """A Flask test client on a fully-migrated DB (same shape as test_health)."""
    from flask import Flask

    from agent2.server.routes import register_routes
    init_db()
    app = Flask(__name__)
    register_routes(app)
    with app.test_client() as c:
        yield c


def test_api_mcp_lists_every_bridge_in_registry_order(client):
    body = client.get("/api/mcp").get_json()
    keys = [s["key"] for s in body["servers"]]
    assert keys == [b.SERVER_KEY for b in R.bridges()]
    assert "zap" in keys, "the web surface cannot see the server the CLI can toggle"


def test_api_mcp_never_ships_the_zap_key(client):
    """⚠️ Same guarantee as `status()`, restated at the boundary that publishes it.

    `/api/health` and this endpoint are unauthenticated (see routes.py), so a key
    that reaches the payload is world-readable. Asserting on `statuses()` alone
    would not catch a route that helpfully merged `auth_headers()` in.
    """
    zap.set_key("SEKRIT-ZAP-KEY-9c1f")
    raw = client.get("/api/mcp").get_data(as_text=True)
    assert "SEKRIT-ZAP-KEY-9c1f" not in raw
    assert "Authorization" not in raw


def test_an_unknown_mcp_server_is_a_404_not_a_500(client):
    for path in ("connect", "disconnect", "auto"):
        resp = client.post(f"/api/mcp/nope/{path}", json={})
        assert resp.status_code == 404, path
        assert "unknown MCP server" in resp.get_json()["error"]


def test_api_mcp_auto_toggles_the_same_state_the_cli_menu_writes(client):
    """One backend: the route must move `enabled` and persist it, not shadow it.

    Task 9: "persist" now means the per-project `mcp_state` row, so this asserts
    the row the CLI menu writes — not the legacy global, which is read-only now.
    """
    from agent2.integrations import state as S

    client.post("/api/mcp/zap/auto", json={"enabled": True})
    assert zap.enabled is True
    assert S.states_for_project()["zap"] is True

    body = client.post("/api/mcp/zap/auto", json={"enabled": False}).get_json()
    assert body["status"]["enabled"] is False
    assert S.states_for_project()["zap"] is False


def test_api_mcp_disconnect_is_idempotent_on_a_dead_bridge(client):
    body = client.post("/api/mcp/zap/disconnect", json={}).get_json()
    assert body["ok"] is True
    assert body["status"]["connected"] is False


def test_the_bespoke_burp_routes_are_gone_and_the_generic_ones_cover_them(client):
    """⚠️ REMOVED ON PURPOSE, AND THIS TEST IS WHERE THAT IS RECORDED.

    `/api/burp*` predated the registry and was kept alive through Task 8 under
    rule 28 (never remove a feature silently). It was then removed on explicit
    instruction, because `/api/mcp` reached exact parity — and rule 28 is about
    *silence*, not about superseded surfaces.

    The test asserts BOTH halves, because either one alone is misleading: that
    the old paths really are gone (a stale route left behind is a second list of
    servers, which is the drift `/api/mcp` exists to end), and that every action
    they offered still has a live home. Deleting a route without proving the
    replacement answers is how a "cleanup" becomes a regression.
    """
    for path in ("/api/burp", "/api/burp/connect", "/api/burp/disconnect", "/api/burp/auto"):
        for call in (client.get, client.post):
            assert call(path).status_code == 404, f"{path} still answers"

    # …and the generic surface covers every one of them.
    body = client.get("/api/mcp").get_json()
    assert "burp" in [s["key"] for s in body["servers"]]
    assert set(client.get("/api/mcp").get_json()["servers"][0]) >= {"key", "connected", "enabled"}
    auto = client.post("/api/mcp/burp/auto", json={"enabled": False}).get_json()
    assert auto["ok"] is True and auto["status"]["enabled"] is False
    off = client.post("/api/mcp/burp/disconnect", json={}).get_json()
    assert off["ok"] is True


# ══════════════════════════════════════════════════════════════════════════════
# Task 9 — per-project MCP state (agent2/integrations/state.py, migration 11)
# ══════════════════════════════════════════════════════════════════════════════
# The spec's own example is the acceptance test: Project A has Burp ✓ ZAP ✓ while
# Project B has Burp ✓ ZAP ✗, and neither is reconfigured on the next session.

@pytest.fixture
def two_projects(tmp_path, monkeypatch):
    """Two project keys, switchable, with the state cache honest about the switch.

    Patches `state.project` rather than the workspace manager: this exercises the
    read-through path itself, which is the thing under test, without depending on
    how a workspace switch is triggered.
    """
    from agent2.core.context import project_key
    from agent2.integrations import state as S

    init_db()
    a = project_key(str(tmp_path / "project-a"))
    b = project_key(str(tmp_path / "project-b"))
    current = {"key": a}
    monkeypatch.setattr(S, "project", lambda: current["key"])

    def switch_to(key: str) -> None:
        current["key"] = key
        S.invalidate()

    S.invalidate()
    yield a, b, switch_to
    S.invalidate()


def test_two_projects_hold_different_mcp_state(two_projects):
    """⚠️ Rule 30 verbatim from the spec: A=(Burp ✓ ZAP ✓), B=(Burp ✓ ZAP ✗)."""
    from agent2.integrations import state as S
    a, b, switch_to = two_projects

    burp.set_auto_connect(True)
    zap.set_auto_connect(True)
    assert (burp.enabled, zap.enabled) == (True, True)

    switch_to(b)
    burp.set_auto_connect(True)
    zap.set_auto_connect(False)
    assert (burp.enabled, zap.enabled) == (True, False)

    switch_to(a)
    assert (burp.enabled, zap.enabled) == (True, True), \
        "project B's toggle leaked into project A"
    assert S.states_for_project(a) == {"burp": True, "zap": True}
    assert S.states_for_project(b) == {"burp": True, "zap": False}


def test_state_survives_a_new_session(two_projects):
    """"Do not require users to reconfigure MCP every session."

    A fresh bridge object is what a new process has; the answer must come from
    the row, not from anything the old instance remembered.
    """
    from agent2.integrations.zap_mcp import ZapMCP
    a, _b, _switch = two_projects

    zap.set_auto_connect(True)
    assert ZapMCP().enabled is True, "a new session lost the persisted toggle"


def test_a_project_with_no_row_inherits_the_legacy_global(two_projects):
    """⚠️ Rule 22: an existing install must not wake up with its bridges off."""
    from agent2.database import set_setting
    from agent2.integrations import state as S
    _a, b, switch_to = two_projects

    switch_to(b)
    exe("DELETE FROM mcp_state WHERE project=?", (b,))
    set_setting("zap_auto_connect", "1")
    S.invalidate()
    assert zap.enabled is True, "the legacy global was ignored — upgrades lose state"

    # …and this project's own row, once written, outranks that global.
    zap.set_auto_connect(False)
    assert zap.enabled is False
    exe("DELETE FROM settings WHERE key='zap_auto_connect'")


def test_precedence_falls_through_to_the_env_default(two_projects):
    """No row and no legacy key: the answer is the bridge's ENV_DEFAULT."""
    from agent2.integrations import state as S
    _a, b, switch_to = two_projects

    switch_to(b)
    exe("DELETE FROM mcp_state WHERE project=?", (b,))
    exe("DELETE FROM settings WHERE key IN ('burp_auto_connect','zap_auto_connect')")
    S.invalidate()
    assert zap.enabled is bool(zap.ENV_DEFAULT)
    assert burp.enabled is bool(burp.ENV_DEFAULT)


def test_enabled_is_read_through_with_no_stored_copy():
    """⚠️ The one-declaration rule, asserted structurally.

    A future edit that memoizes the answer on the instance (plus a `reload()`
    someone forgets to call on a workspace switch) is the drift this property
    exists to prevent. Asserting only that no attribute is literally NAMED
    `enabled` is too weak — a `self._cached` under the property is the same bug —
    so this pins the real property: READING must not store anything.
    """
    assert isinstance(McpBridge.__dict__.get("enabled"), property), \
        "`enabled` stopped being a read-through property"
    for b in R.bridges():
        before = set(vars(b))
        _ = b.enabled
        _ = b.enabled
        assert set(vars(b)) == before, \
            f"{b.SERVER_KEY} memoized `enabled` on the instance: " \
            f"{set(vars(b)) - before}"


def test_assignment_to_enabled_persists(two_projects):
    """`bridge.enabled = True` IS `set_auto_connect(True)` — the setter writes."""
    from agent2.integrations import state as S
    a, _b, _switch = two_projects

    zap.enabled = True
    assert S.states_for_project(a).get("zap") is True
    zap.enabled = False
    assert S.states_for_project(a).get("zap") is False


def test_a_toggle_publishes_on_the_sync_bus(two_projects):
    """Rule 10: a CLI toggle has to reach a web process. `mcp` is a sync resource."""
    from agent2.core import sync
    seen = []

    def listener(_topic, payload):
        seen.append(payload)

    sync.subscribe("mcp", listener)
    try:
        zap.set_auto_connect(True)
    finally:
        sync.unsubscribe("mcp", listener)
    assert seen and seen[-1].get("server") == "zap"
    assert seen[-1].get("enabled") is True


def test_mcp_is_a_known_sync_resource():
    from agent2.core import sync
    assert "mcp" in sync.RESOURCES, \
        "the poller only republishes resources it iterates — a toggle would not travel"


def test_a_write_leaves_the_cache_agreeing_with_the_row(two_projects):
    """⚠️ The notify-before-cache ordering, pinned.

    `notify()` publishes synchronously and this module subscribes to its own
    resource, so caching before notifying hands the inline listener the fresh
    value to clear. Reversing the two lines in `set_enabled` fails this.
    """
    from agent2.integrations import state as S
    a, _b, _switch = two_projects

    zap.set_auto_connect(True)
    assert S._cache.get((a, "zap")) is True, "the write was cleared by its own notify"
    assert zap.enabled is True


def test_migration_11_creates_mcp_state():
    """Rule 23: schema changes ship as migrations, and the table is keyed by pair."""
    from agent2.database import SCHEMA_VERSION, _MIGRATIONS
    versions = [v for v, _n, _f in _MIGRATIONS]
    assert versions == list(range(1, len(versions) + 1)), "migrations are not contiguous"
    assert SCHEMA_VERSION >= 11
    assert any(n == "mcp_state" for _v, n, _f in _MIGRATIONS)

    init_db()
    cols = {r["name"] for r in qall("PRAGMA table_info(mcp_state)", ())}
    assert {"project", "server", "enabled", "updated_at"} <= cols
    pk = {r["name"] for r in qall("PRAGMA table_info(mcp_state)", ()) if r["pk"]}
    assert pk == {"project", "server"}


def test_states_for_project_reports_only_configured_servers(two_projects):
    """An absent server is on its fallback, which is not the same as being off."""
    from agent2.integrations import state as S
    a, _b, _switch = two_projects

    exe("DELETE FROM mcp_state WHERE project=?", (a,))
    S.invalidate()
    assert S.states_for_project(a) == {}
    zap.set_auto_connect(False)
    assert S.states_for_project(a) == {"zap": False}, \
        "an explicit OFF must be a row, or it reads as unconfigured"


def test_state_functions_are_total_on_a_broken_database(monkeypatch):
    """The dispatch path may not raise: a DB fault degrades to the env default."""
    from agent2.integrations import state as S

    S.invalidate()
    monkeypatch.setattr("agent2.database.qone",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("locked")))
    assert S.is_enabled("zap", env_default=True) is True
    assert S.is_enabled("zap", env_default=False) is False
    assert zap.enabled is bool(zap.ENV_DEFAULT)


def test_switching_projects_never_disconnects_a_live_bridge(two_projects):
    """⚠️ Rule 21: `enabled` gates DIALLING, so a switch must not tear down a scan."""
    from agent2.integrations import state as S
    a, b, switch_to = two_projects

    zap.set_auto_connect(True)
    _fake_connect(zap)
    try:
        switch_to(b)
        exe("DELETE FROM mcp_state WHERE project=?", (b,))
        S.invalidate()
        assert zap.enabled is bool(zap.ENV_DEFAULT)
        assert zap.is_connected() is True, \
            "a workspace switch killed an established session"
    finally:
        zap._session = None
        zap._loop = None
        switch_to(a)


# ══════════════════════════════════════════════════════════════════════════════
# MCP endpoint + credential configuration (mcp_config, migration 12)
# ══════════════════════════════════════════════════════════════════════════════
# The user's redesign: one `/mcp` command with `<server> config` for URL, port and
# ZAP's Security Key, the same three editable from the browser, and `/mcp connect`
# meaning "every server". Storage had to become durable for any of it to survive a
# restart — which is what makes the masking rules below load-bearing rather than
# cosmetic.


def test_migration_12_creates_mcp_config_keyed_by_server_alone():
    """Rule 23, and the key is the whole design decision.

    ⚠️ `mcp_state` is keyed by (project, server) and `mcp_config` by server ALONE,
    on purpose: "should ZAP arm itself in this checkout" is per target, but "which
    ZAP, on which port, with which key" describes the one ZAP on this machine.
    Keying config per project would make the user retype the endpoint in every
    checkout — exactly the friction Task 9 removed.
    """
    from agent2.database import SCHEMA_VERSION, _MIGRATIONS
    versions = [v for v, _n, _f in _MIGRATIONS]
    assert versions == list(range(1, len(versions) + 1)), "migrations are not contiguous"
    assert SCHEMA_VERSION >= 12
    assert any(n == "mcp_config" for _v, n, _f in _MIGRATIONS)

    init_db()
    info = qall("PRAGMA table_info(mcp_config)", ())
    cols = {r["name"] for r in info}
    assert {"server", "url", "security_key", "updated_at"} <= cols
    assert {r["name"] for r in info if r["pk"]} == {"server"}


def test_set_config_leaves_the_other_field_alone():
    """⚠️ THE BUG THIS PINS SHIPPED ONCE IN THIS FILE'S OWN HISTORY.

    `None` means "leave it alone" and `""` means "clear it". The first draft folded
    NULL to '' inside the upsert's VALUES clause, which made `excluded.url` an empty
    STRING that the UPDATE's COALESCE then happily kept — so saving a key wiped the
    URL, with a successful write and no error anywhere. A user editing only the port
    in the web form submits with the key box untouched; that must not cost them the
    credential.
    """
    from agent2.integrations import state as S
    init_db()

    def stored(fn, server="zap"):
        """Read past `_config_cache` — the DB is the assertion, not the cache.

        ⚠️ WITHOUT THIS DROP THIS TEST CANNOT SEE THE BUG IT EXISTS FOR. Sabotage
        verification proved it: restoring `COALESCE(?, '')` in the upsert's VALUES
        clause clobbers the OTHER COLUMN IN THE TABLE, and this test still passed,
        because `set_config` seeds the cache with the correctly merged row and
        `get_*` answered from there. The process that made the edit is the one
        process that cannot notice — and dual mode is two processes over one DB, so
        the browser half would read the clobbered row while the CLI half kept
        insisting the value was fine.
        """
        S.invalidate()
        return fn(server)

    S.set_config("zap", url="http://127.0.0.1:9999", key="keep-me")
    S.set_config("zap", url="http://127.0.0.1:7777")          # port-only edit
    assert S.get_key("zap") == "keep-me", "a URL edit destroyed the credential"
    assert stored(S.get_key) == "keep-me", "the credential is gone from the TABLE"

    S.set_config("zap", key="new-key")                        # key-only edit
    assert S.get_url("zap") == "http://127.0.0.1:7777", "a key edit destroyed the URL"
    assert stored(S.get_url) == "http://127.0.0.1:7777", "the URL is gone from the TABLE"

    S.set_config("zap", key="")                               # deliberate clear
    assert S.get_key("zap") == ""
    assert S.get_url("zap") == "http://127.0.0.1:7777", "clearing the key cleared the URL"
    assert stored(S.get_url) == "http://127.0.0.1:7777"
    assert stored(S.get_key) == "", "a cleared key came back from the TABLE"


def test_set_config_survives_the_cache_being_dropped():
    """The value has to be in the DB, not only in `_config_cache`.

    Without this the previous test would pass on a pure in-memory write, and dual
    mode — two processes, one DB — would show each surface its own answer.
    """
    from agent2.integrations import state as S
    init_db()
    S.set_config("zap", url="http://127.0.0.1:4242", key="durable")
    S.invalidate()
    assert S.get_url("zap") == "http://127.0.0.1:4242"
    assert S.get_key("zap") == "durable"


def test_clear_config_falls_back_to_the_env_default():
    from agent2.integrations import state as S
    init_db()
    S.set_config("zap", url="http://127.0.0.1:4242", key="x")
    S.clear_config("zap")
    assert S.get_url("zap", env_default="http://env:1") == "http://env:1"
    assert S.get_key("zap", env_default="") == ""
    assert zap.url == config.ZAP_MCP_URL


def test_a_stored_row_beats_the_env_default_but_an_empty_one_does_not():
    """Precedence is row → env, and an empty column is not a configured value."""
    from agent2.integrations import state as S
    init_db()
    S.set_config("zap", url="", key="")
    assert S.get_url("zap", env_default="http://env:1") == "http://env:1"
    S.set_config("zap", url="http://stored:2")
    assert S.get_url("zap", env_default="http://env:1") == "http://stored:2"


def test_mask_secret_leaks_neither_content_nor_length():
    """⚠️ NOT `k[:6]…k[-4:]` — that pattern prints most of a ten-character ZAP key.

    `providers.list_providers(safe=True)` uses it, which is defensible for a 40-char
    vendor token. A ZAP Security Key is short enough that a prefix, a suffix, or even
    a faithful bullet COUNT meaningfully narrows a guess, so nothing about the return
    value may vary with the input except whether it is empty.
    """
    from agent2.integrations import state as S
    short, long = "1234567890", "x" * 64
    assert S.mask_secret(short) == S.mask_secret(long), "the mask leaks the length"
    for secret in (short, long):
        masked = S.mask_secret(secret)
        assert secret[:3] not in masked and secret[-3:] not in masked
        assert set(masked) == {"\u2022"}
    assert S.mask_secret("") == ""
    assert S.mask_secret("   ") == "", "whitespace is not a credential"


def test_config_for_is_the_only_shape_a_surface_may_render():
    from agent2.integrations import state as S
    init_db()
    S.set_config("zap", url="http://127.0.0.1:8383", key="surface-canary")
    view = S.config_for("zap", url_default=config.ZAP_MCP_URL)

    assert "surface-canary" not in json.dumps(view)
    assert view["key_set"] is True
    assert view["key_masked"] and "surface" not in view["key_masked"]
    assert view["url"] == "http://127.0.0.1:8383"
    assert view["url_source"] == "config" and view["key_source"] == "config"

    S.clear_config("zap")
    view = S.config_for("zap", url_default=config.ZAP_MCP_URL)
    assert view["key_set"] is False and view["key_masked"] == ""
    assert view["url_source"] == "env" and view["key_source"] == "none"


def test_config_reads_are_total_on_a_broken_database(monkeypatch):
    """Same contract as the state half: the hot path degrades, it does not raise."""
    from agent2.integrations import state as S
    S.invalidate()
    monkeypatch.setattr("agent2.database.qone",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("locked")))
    assert S.get_url("zap", env_default="http://fallback:1") == "http://fallback:1"
    assert S.get_key("zap", env_default="") == ""
    assert zap.url == config.ZAP_MCP_URL
    assert zap.auth_headers() in ({}, {"Authorization": config.ZAP_MCP_KEY})


def test_a_failed_config_read_is_not_cached(monkeypatch):
    """⚠️ Caching a fault would outlive it — one locked read must not pin "unset"
    for the rest of the process, which is how a working key stops being sent."""
    from agent2.integrations import state as S
    init_db()
    S.set_config("zap", key="present")
    S.invalidate()

    boom = {"on": True}
    real = __import__("agent2.database", fromlist=["qone"]).qone

    def flaky(*a, **k):
        if boom["on"]:
            raise RuntimeError("locked")
        return real(*a, **k)

    monkeypatch.setattr("agent2.database.qone", flaky)
    assert S.get_key("zap") == ""
    boom["on"] = False
    assert S.get_key("zap") == "present", "a transient DB fault was cached forever"


# ── url / port / key are properties, not stored attributes ────────────────────

def test_the_bridge_url_reads_through_with_no_instance_copy():
    """⚠️ DUAL MODE IS THE FAILURE THIS PREVENTS.

    `agent2dual.py` is two processes over one DB. With a cached `self.url`, editing
    ZAP's port in the terminal leaves the browser half dialling the old one — a
    connection failure invisible on both surfaces, because each shows the value IT
    believes and neither shows the disagreement. Writing the row from underneath the
    object is exactly what another process does.
    """
    from agent2.integrations import state as S
    init_db()
    assert "url" not in vars(zap), "a stored copy defeats the read-through"
    S.set_config("zap", url="http://elsewhere:1234")
    assert zap.url == "http://elsewhere:1234"

    exe("UPDATE mcp_config SET url=? WHERE server=?", ("http://other-process:5555", "zap"))
    S.invalidate()                                   # what the `mcp` sync topic does
    assert zap.url == "http://other-process:5555"
    assert "url" not in vars(zap)


def test_the_zap_key_reads_through_the_same_way():
    from agent2.integrations import state as S
    init_db()
    zap.set_key("first")
    assert zap.key == "first" and "key" not in vars(zap)
    exe("UPDATE mcp_config SET security_key=? WHERE server=?", ("second", "zap"))
    S.invalidate()
    assert zap.key == "second"
    assert zap.auth_headers() == {"Authorization": "second"}


def test_port_is_derived_from_the_url_and_never_stored():
    """⚠️ ONE DECLARATION OF THE ENDPOINT. A `port` column could disagree with the
    port inside `url`, and the loser would be whichever the connect path skipped."""
    init_db()
    assert {r["name"] for r in qall("PRAGMA table_info(mcp_config)", ())} \
        .isdisjoint({"port", "host"})
    zap.set_url("http://127.0.0.1:8282")
    assert zap.port == 8282
    assert zap.set_port(8383) is True
    assert zap.url == "http://127.0.0.1:8383" and zap.port == 8383


def test_set_port_refuses_a_port_that_can_never_connect():
    """Rule 21-adjacent: reject the edit rather than store a dead endpoint."""
    init_db()
    zap.set_url("http://127.0.0.1:8282")
    for bad in (0, 70000, -1, "x", "", None, "80 80"):
        assert zap.set_port(bad) is False, f"accepted {bad!r}"
    assert zap.url == "http://127.0.0.1:8282", "a rejected port still rewrote the URL"


def test_set_port_preserves_scheme_path_and_brackets_ipv6():
    init_db()
    zap.set_url("https://[::1]:8282/mcp?x=1")
    assert zap.set_port(9999) is True
    assert zap.url == "https://[::1]:9999/mcp?x=1"


def test_burp_takes_no_key_and_saying_so_is_a_no_op():
    """A stored key nothing sends would show "key set" and change nothing on the
    wire — a lie with security consequences, so the base class refuses it."""
    from agent2.integrations import state as S
    init_db()
    burp.set_security_key("should-not-stick")
    assert burp.security_key() == ""
    assert S.get_key("burp") == ""
    assert "should-not-stick" not in json.dumps(R.statuses())


def test_no_bridge_offers_a_skip_tls_verification_switch():
    """⚠️ DELIBERATELY ABSENT, FOREVER (rule 29).

    ZAP ships "Secure Only" ON, so an HTTPS endpoint with an untrusted root CA is
    the common first failure and "just skip verification" is the tempting fix.
    Silently trusting any certificate on a security tool's CONTROL CHANNEL is the
    quiet downgrade rule 29 exists to prevent; the documented answer is to trust
    ZAP's root CA. If this test ever fails, the switch — not the test — is the bug.
    """
    import inspect

    from agent2.integrations import mcp_base, zap_mcp
    for mod in (mcp_base, zap_mcp):
        src = inspect.getsource(mod).lower()
        for smell in ("verify=false", "verify_ssl=false", "insecure=true",
                      "check_hostname = false", "_create_unverified_context"):
            assert smell not in src, f"{mod.__name__} added {smell!r}"


# ── The CLI grammar: /mcp [server] <action> ───────────────────────────────────

def test_the_mcp_parser_resolves_servers_through_the_registry(monkeypatch):
    """⚠️ NO LITERAL ("burp","zap") IN THE PARSER — that is the registry's job.

    A hard-coded pair is the second declaration of the server table: a third server
    would work everywhere except here, where it would fall through to "unknown
    action" and print usage.
    """
    import inspect

    import agent2cli
    src = inspect.getsource(agent2cli.cmd_mcp)
    assert "_mcp_registry.get(" in src
    # The docstring NAMES the banned literal in order to explain it, so search the
    # body only — otherwise this test could only pass by deleting the explanation.
    # (Split rather than `replace(getdoc(...))`: `getdoc` dedents, so it no longer
    # matches the source text it came from.)
    body = src.split('"""')[2] if src.count('"""') >= 2 else src
    assert '"zap"' not in body and "'zap'" not in body, "the parser hard-codes a server"
    assert '"burp"' not in body and "'burp'" not in body


def test_bare_mcp_connect_dials_every_server(monkeypatch):
    """The user's requirement verbatim: "when i do /mcp connect it tries to connect
    all"."""
    import agent2cli
    seen = []
    monkeypatch.setattr(agent2cli, "_mcp_apply",
                        lambda b, on: (seen.append((b.SERVER_KEY, on)), (True, "ok"))[1])
    agent2cli.cmd_mcp("/mcp connect")
    assert seen == [(b.SERVER_KEY, True) for b in R.bridges()]

    seen.clear()
    agent2cli.cmd_mcp("/mcp zap connect")
    assert seen == [("zap", True)]


def test_bare_mcp_disconnect_drops_every_server(monkeypatch):
    import agent2cli
    seen = []
    monkeypatch.setattr(agent2cli, "_mcp_apply",
                        lambda b, on: (seen.append((b.SERVER_KEY, on)), (True, "ok"))[1])
    agent2cli.cmd_mcp("/mcp disconnect")
    assert seen == [(b.SERVER_KEY, False) for b in R.bridges()]


def test_mcp_config_without_a_server_asks_instead_of_guessing(monkeypatch):
    """Rule 21: an editor that picked a server for you would edit the wrong one."""
    import agent2cli
    monkeypatch.setattr(agent2cli, "_mcp_config",
                        lambda b: pytest.fail(f"configured {b.SERVER_KEY} unasked"))
    said = []
    monkeypatch.setattr(agent2cli, "status_line", lambda m, k="info": said.append(m))
    agent2cli.cmd_mcp("/mcp config")
    assert any("Which server" in m for m in said)


def test_mcp_config_edits_only_what_was_typed(monkeypatch):
    """⚠️ ENTER KEEPS THE CURRENT VALUE — INCLUDING THE KEY.

    The key prompt shows bullets and treats blank as "unchanged", so tabbing through
    the editor to fix a port cannot wipe a working credential, and nobody reads their
    ZAP key out loud off a shared screen.
    """
    from agent2.integrations import state as S

    import agent2cli
    init_db()
    S.set_config("zap", url="http://127.0.0.1:8282", key="untouched")

    answers = iter(["", "8383", ""])              # URL unchanged, port, key unchanged
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    monkeypatch.setattr(agent2cli, "status_line", lambda *a, **k: None)
    agent2cli._mcp_config(zap)

    assert zap.url == "http://127.0.0.1:8383"
    assert zap.key == "untouched", "an untouched key prompt destroyed the credential"


def test_clearing_the_key_from_the_cli_takes_the_literal_word(monkeypatch):
    """Rule 21: destroying a credential is an explicit sentence, not a blank field."""
    from agent2.integrations import state as S

    import agent2cli
    init_db()
    S.set_config("zap", url="http://127.0.0.1:8282", key="doomed")
    answers = iter(["", "", "clear"])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    monkeypatch.setattr(agent2cli, "status_line", lambda *a, **k: None)
    agent2cli._mcp_config(zap)
    assert zap.key == ""
    assert zap.url == "http://127.0.0.1:8282", "clearing the key moved the URL"


def test_the_config_editor_never_prints_the_key(monkeypatch, capsys):
    import agent2cli
    from agent2.integrations import state as S
    init_db()
    S.set_config("zap", key="screen-share-canary")
    answers = iter(["", "", ""])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    monkeypatch.setattr(agent2cli, "status_line", lambda *a, **k: None)
    agent2cli._mcp_config(zap)
    assert "screen-share-canary" not in capsys.readouterr().out


def test_a_config_edit_does_not_reconnect_a_live_session(monkeypatch):
    """⚠️ Rule 21 again: applying a half-typed setting by tearing down a running
    scan is a destructive act nobody asked for. Say it is stale instead."""
    import agent2cli
    init_db()
    _fake_connect(zap)
    monkeypatch.setattr(type(zap), "connect",
                        lambda self, timeout=12.0: pytest.fail("reconnected mid-scan"))
    monkeypatch.setattr(type(zap), "disconnect",
                        lambda self: pytest.fail("dropped a live session"))
    answers = iter(["http://127.0.0.1:9001", "", ""])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    monkeypatch.setattr(agent2cli, "status_line", lambda *a, **k: None)
    agent2cli._mcp_config(zap)
    assert zap.is_connected() is True


# ── The same three fields, from the browser ───────────────────────────────────

def test_api_mcp_carries_the_editable_config_for_every_server(client):
    """Rules 9/10: the CLI can edit URL, port and key, so the browser must too."""
    body = client.get("/api/mcp").get_json()
    by_key = {s["key"]: s for s in body["servers"]}
    for row in by_key.values():
        assert {"config", "port", "takes_key", "setup_hint"} <= set(row)
        assert {"url", "url_source", "key_set", "key_masked"} <= set(row["config"])
    assert by_key["zap"]["takes_key"] is True
    assert by_key["burp"]["takes_key"] is False


def test_the_web_config_route_is_the_same_leave_alone_contract(client):
    """An absent field leaves the column alone; `""` clears it — as in the CLI."""
    init_db()
    client.post("/api/mcp/zap/config",
                json={"url": "http://127.0.0.1:8282", "security_key": "web-key"})

    body = client.post("/api/mcp/zap/config", json={"port": "8484"}).get_json()
    assert body["ok"] is True and body["port"] == 8484
    assert zap.key == "web-key", "a port-only POST wiped the credential"
    assert "web-key" not in json.dumps(body)

    client.post("/api/mcp/zap/config", json={"security_key": ""})
    assert zap.key == ""
    assert zap.port == 8484, "clearing the key moved the endpoint"


def test_the_web_config_route_rejects_an_unusable_port(client):
    init_db()
    client.post("/api/mcp/zap/config", json={"url": "http://127.0.0.1:8282"})
    resp = client.post("/api/mcp/zap/config", json={"port": "70000"})
    assert resp.status_code == 400
    assert "not a usable port" in resp.get_json()["error"]
    assert zap.url == "http://127.0.0.1:8282", "a rejected port still moved the URL"


def test_the_web_config_route_reports_a_stale_session_instead_of_reconnecting(client):
    init_db()
    _fake_connect(zap)
    body = client.post("/api/mcp/zap/config", json={"url": "http://127.0.0.1:9002"}).get_json()
    assert body["reconnect_required"] is True
    assert zap.is_connected() is True, "the route reconnected a live session"


def test_an_unknown_server_is_a_404_on_the_config_route_too(client):
    resp = client.post("/api/mcp/nope/config", json={"url": "http://x"})
    assert resp.status_code == 404
    assert "unknown MCP server" in resp.get_json()["error"]


def test_connect_all_and_disconnect_all_cover_every_bridge(client, monkeypatch):
    """The web twin of a bare `/mcp connect`."""
    dialled = []
    monkeypatch.setattr(McpBridge, "connect",
                        lambda self, timeout=12.0: (dialled.append(self.SERVER_KEY),
                                                    (True, f"{self.LABEL}: up"))[1])
    body = client.post("/api/mcp/connect").get_json()
    assert dialled == [b.SERVER_KEY for b in R.bridges()]
    assert body["ok"] is True
    assert [r["server"] for r in body["results"]] == dialled

    dropped = []
    monkeypatch.setattr(McpBridge, "disconnect",
                        lambda self: dropped.append(self.SERVER_KEY))
    assert client.post("/api/mcp/disconnect").get_json()["ok"] is True
    assert dropped == [b.SERVER_KEY for b in R.bridges()]
    assert all(b.enabled is False for b in R.bridges()), \
        "auto-connect stayed on, so the next turn silently redials"


def test_the_web_panel_is_built_from_the_registry_not_from_burp():
    """⚠️ ONE PANEL, NOT ONE PER SERVER. The old markup was Burp-shaped
    (`burp-url`, `burp-auto`), so ZAP would have meant a second copy of all of it
    and the two would drift the first time one gained a field."""
    from pathlib import Path

    from agent2.server.ui import get_html
    html = get_html()
    assert 'id="mcp-list"' in html
    assert "mcpConnectAll()" in html and "mcpDisconnectAll()" in html
    assert 'id="burp-url"' not in html and 'id="burp-auto"' not in html

    js = Path("public/script.js").read_text(encoding="utf-8")
    for fn in ("function renderMcp", "async function loadMcp",
               "async function mcpSaveConfig", "async function mcpConnectAll"):
        assert fn in js, f"missing {fn}"
    # Rule 28: an older cached page still calls this.
    assert "function loadBurp()" in js


def test_the_web_key_field_is_never_prefilled_with_the_key():
    """The payload has no key to prefill with, and the input must not ask for one
    in cleartext — `type="password"`, placeholder only."""
    from pathlib import Path
    js = Path("public/script.js").read_text(encoding="utf-8")
    assert 'type="password" id="mcp-key-' in js
    assert "key_masked" not in js or "value=\"${esc(cfg.key_masked" not in js
    # Only send the credential when the user actually typed one. The guard is
    # `.trim()`-based — a whitespace-only box is NOT "typed one", see
    # `test_the_panel_never_sends_a_whitespace_only_key`.
    save = js.split("async function mcpSaveConfig")[1].split("\n}")[0]
    assert "if(keyEl && keyEl.value.trim()) body.security_key=keyEl.value.trim();" in save


# ══════════════════════════════════════════════════════════════════════════════
# TASK 10 — MCP health
# ══════════════════════════════════════════════════════════════════════════════
# The spec is four lines of terminal output, but the thing worth pinning is the
# state machine underneath: which observations mean "healthy", and — because
# `/api/health` returns 503 on a fault — which ones must NOT.

def test_health_reports_every_server_in_registry_order():
    """⚠️ Same one-declaration rule as the panel: the health list is the SERVER
    TABLE's, not a hand-written pair. A third server appears here for free."""
    rows = R.health()
    assert [r["key"] for r in rows] == [b.SERVER_KEY for b in R.bridges()]
    assert [r["label"] for r in rows] == [b.LABEL for b in R.bridges()]


def test_every_state_has_wording_and_every_wording_a_state():
    """A state with no `HEALTH_TEXT` entry renders as its own identifier —
    `"idle"` on a user's screen instead of "Not connected"."""
    assert set(R.HEALTH_TEXT) == set(R._HEALTH_STATES)
    assert R.HEALTH_TEXT["connected"] == "Connected"
    assert R.HEALTH_TEXT["failed"] == "Connection failed"   # the spec, verbatim


def test_a_connected_bridge_is_connected_and_ok():
    names = _fake_connect(zap, ("scan", "spider", "alerts"))
    row = next(r for r in R.health() if r["key"] == "zap")

    assert row["state"] == "connected"
    assert row["text"] == "Connected"
    assert row["ok"] is True
    assert row["connected"] is True
    assert row["tool_count"] == len(names)


def test_a_bridge_nobody_enabled_is_off_and_ok():
    """⚠️ BOTH BRIDGES SHIP AUTO-CONNECT OFF. If "not connected" were a fault,
    a fresh install would be unhealthy out of the box — an alarm that fires on
    the default configuration is one people learn to ignore."""
    zap.set_auto_connect(False)
    row = next(r for r in R.health() if r["key"] == "zap")

    assert row["state"] == "off"
    assert row["ok"] is True
    assert row["enabled"] is False


def test_an_enabled_bridge_that_has_not_been_dialled_is_idle_and_ok():
    """⚠️ DUAL MODE. Bridges are per-PROCESS singletons, enablement is in the DB:
    the CLI child can hold a live ZAP session while the web process — the one
    answering /api/health — never dialled. "Enabled, no session, no error" means
    *not tried here yet*; the next agent turn resolves it. A fault here would
    make every dual-mode install permanently unhealthy."""
    if not zap.status().get("mcp_installed"):
        pytest.skip("the mcp package is not installed in this environment")
    zap.set_auto_connect(True)
    zap._last_error = ""
    row = next(r for r in R.health() if r["key"] == "zap")

    assert row["state"] == "idle"
    assert row["text"] == "Not connected"
    assert row["ok"] is True


def test_an_enabled_bridge_whose_last_attempt_failed_is_a_fault():
    """`connect()` clears `_last_error` before every attempt, so a non-empty one
    means THE MOST RECENT attempt failed — not that something failed once. That
    plus "the user asked for this server" is the whole fault condition."""
    if not zap.status().get("mcp_installed"):
        pytest.skip("the mcp package is not installed in this environment")
    zap.set_auto_connect(True)
    zap._last_error = "connection refused"
    row = next(r for r in R.health() if r["key"] == "zap")

    assert row["state"] == "failed"
    assert row["text"] == "Connection failed"
    assert row["ok"] is False


def test_the_same_failure_on_a_disabled_bridge_is_not_a_fault():
    """⚠️ THE PAIR THAT MAKES `ok` MEAN SOMETHING. Identical observation, opposite
    verdict, and the only difference is whether the user asked for the server.
    Nothing is scheduled to dial a disabled bridge, so its last error is history.
    """
    zap.set_auto_connect(False)
    zap._last_error = "connection refused"
    row = next(r for r in R.health() if r["key"] == "zap")

    assert row["state"] == "failed"          # what we observed is unchanged …
    assert row["ok"] is True                 # … the verdict is not


def test_health_never_raises_when_a_bridge_cannot_even_report(monkeypatch):
    """Total, like every other lookup in this module: /api/health calls it."""
    monkeypatch.setattr(type(zap), "status",
                        lambda self: (_ for _ in ()).throw(RuntimeError("boom")))
    rows = R.health()
    row = next(r for r in rows if r["key"] == "zap")
    assert row["state"] in R._HEALTH_STATES
    assert isinstance(row["ok"], bool)


def test_health_does_no_io(monkeypatch):
    """⚠️ A PROBE THAT DIALS A SOCKET MAKES /api/health BLOCK — for exactly as
    long as an unreachable server takes to time out, which is precisely when
    someone is looking at it. `_section()` catches exceptions; it cannot cap
    latency. Same guarantee `test_status_does_no_io` pins one layer down."""
    import socket

    def _no(*a, **kw):
        raise AssertionError("health() opened a socket")

    monkeypatch.setattr(socket, "create_connection", _no)
    monkeypatch.setattr(socket.socket, "connect", _no)
    R.health()
    R.health_report()


def test_the_report_carries_no_url_and_no_remote_error_text():
    """⚠️ /api/health HAS NO AUTH. A URL can carry userinfo credentials
    (`http://user:pass@host`) and `last_error` is free text a REMOTE SERVER chose
    — servers have echoed request headers back in the wild. Both are available to
    an operator on `/api/mcp` and `/mcp health`, which render the same verdict."""
    zap.set_url("http://alice:supersecret@127.0.0.1:8484")
    zap.set_auto_connect(True)
    zap._last_error = "rejected: Authorization=hunter2"

    report = R.health_report()
    blob = json.dumps(report).lower()

    for row in report["servers"]:
        assert "url" not in row and "detail" not in row
    assert "supersecret" not in blob
    assert "hunter2" not in blob
    assert "authorization" not in blob


def test_the_report_counts_agree_with_the_rows():
    _fake_connect(burp)
    zap.set_auto_connect(True)
    zap._last_error = "nope"

    report = R.health_report()
    rows = R.health()

    assert report["total"] == len(rows)
    assert report["connected"] == sum(1 for r in rows if r["connected"])
    assert report["enabled"] == sum(1 for r in rows if r["enabled"])
    assert report["failing"] == sum(1 for r in rows if not r["ok"])
    assert len(report["problems"]) == report["failing"]


def test_a_problem_line_names_the_server_and_the_state():
    """A monitor learns THAT ZAP is down from the aggregate; a human reads WHY on
    either of the other two surfaces."""
    if not zap.status().get("mcp_installed"):
        pytest.skip("the mcp package is not installed in this environment")
    zap.set_auto_connect(True)
    zap._last_error = "connection refused"

    problems = R.health_report()["problems"]
    assert any(zap.LABEL.lower() in p.lower() and "connection failed" in p.lower()
               for p in problems), problems


# ── The CLI report (`/mcp health`) ────────────────────────────────────────────

def test_slash_mcp_health_is_dispatched(monkeypatch):
    """The action exists — without this branch `/mcp health` prints "Unknown"."""
    import agent2cli
    seen = []
    monkeypatch.setattr(agent2cli, "_mcp_health", lambda scope: seen.append(
        [b.SERVER_KEY for b in scope]))

    agent2cli.cmd_mcp("/mcp health")
    assert seen == [[b.SERVER_KEY for b in R.bridges()]]

    seen.clear()
    agent2cli.cmd_mcp("/mcp zap health")
    assert seen == [["zap"]], "a named server must narrow the report"


def test_the_cli_renders_the_registry_verdict_rather_than_deriving_one(monkeypatch,
                                                                      capsys):
    """⚠️ THREE SURFACES, ONE VERDICT. The CLI reading `connected` and deciding
    for itself what an error string means is the second copy; /api/health's is
    the third, and they disagree the first time one learns a new state."""
    import inspect

    import agent2cli
    src = inspect.getsource(agent2cli._mcp_health)
    body = src.split('"""')[2] if src.count('"""') >= 2 else src
    assert "_mcp_registry.health()" in body
    assert "last_error" not in body, "the CLI is re-deriving the verdict"


def test_the_cli_prints_the_spec_lines_for_connected_and_failed(monkeypatch, capsys):
    """The Task 10 output, verbatim:

        Burp
        ✓ Connected

        OWASP ZAP
        ✗ Connection failed
    """
    import agent2cli
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    monkeypatch.setattr(R, "health", lambda: [
        {"key": "burp", "label": "Burp", "state": "connected", "text": "Connected",
         "ok": True, "enabled": True, "connected": True, "mcp_installed": True,
         "tool_count": 3, "detail": ""},
        {"key": "zap", "label": "OWASP ZAP", "state": "failed",
         "text": "Connection failed", "ok": False, "enabled": True,
         "connected": False, "mcp_installed": True, "tool_count": 0,
         "detail": "connection refused"},
    ])
    agent2cli._mcp_health(list(R.bridges()))
    out = capsys.readouterr().out

    assert "Burp" in out and "OWASP ZAP" in out
    assert "✓" in out and "Connected" in out
    assert "✗" in out and "Connection failed" in out


def test_a_benign_state_does_not_get_the_failure_mark(monkeypatch, capsys):
    """⚠️ THE ONE PLACE THIS REPORT DEPARTS FROM THE SPEC'S LITERAL TEXT, and the
    reason is the same anti-cry-wolf rule `/api/health` follows: `✗ Connection
    failed` under a server nobody enabled is a false statement — no connection
    was attempted, so none failed. Benign states get a dim bullet and their own
    wording; `✗` is reserved for `ok is False`, so the mark on screen and the
    503 on the endpoint always agree."""
    import agent2cli
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    monkeypatch.setattr(R, "health", lambda: [
        {"key": "zap", "label": "OWASP ZAP", "state": "off", "text": "Off",
         "ok": True, "enabled": False, "connected": False, "mcp_installed": True,
         "tool_count": 0, "detail": "connection refused an hour ago"}])

    agent2cli._mcp_health([zap])
    out = capsys.readouterr().out
    assert "✗" not in out, "a disabled server was reported as a failure"
    assert "Off" in out


def test_the_retry_offer_only_appears_when_something_failed(monkeypatch, capsys):
    """Nothing to retry means no offer — an offer that does nothing teaches the
    user that the prompt is noise."""
    import agent2cli
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input",
                        lambda *a: pytest.fail("prompted with nothing to retry"))
    monkeypatch.setattr(R, "health", lambda: [
        {"key": "zap", "label": "OWASP ZAP", "state": "connected",
         "text": "Connected", "ok": True, "enabled": True, "connected": True,
         "mcp_installed": True, "tool_count": 2, "detail": ""}])

    agent2cli._mcp_health([zap])
    assert "[R] Retry" not in capsys.readouterr().out


def test_no_keys_are_offered_where_no_key_can_arrive(monkeypatch, capsys):
    """⚠️ THE `_StuckPrompt` RULE, RESTATED HERE (Task 6). With stdin piped there
    is no way to press R, so offering it would hang or mislead; the equivalent
    command is printed instead, which is actionable in a script."""
    import agent2cli
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    monkeypatch.setattr("builtins.input",
                        lambda *a: pytest.fail("prompted on a non-tty"))
    monkeypatch.setattr(R, "health", lambda: [
        {"key": "zap", "label": "OWASP ZAP", "state": "failed",
         "text": "Connection failed", "ok": False, "enabled": True,
         "connected": False, "mcp_installed": True, "tool_count": 0,
         "detail": "refused"}])

    agent2cli._mcp_health([zap])
    out = capsys.readouterr().out
    assert "[R] Retry" not in out
    assert "/mcp zap reconnect" in out


def test_r_redials_only_the_failing_server(monkeypatch, capsys):
    """⚠️ RULE 21 SAYS WHY THIS RETRY IS ALLOWED AT ALL: dialling an MCP server is
    idempotent and read-only — it opens a session and lists tools. A retry is
    never offered for something that ran a scan, and it is never automatic: this
    one is a keypress."""
    import agent2cli
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda *a: "r")
    monkeypatch.setattr(agent2cli, "status_line", lambda *a, **k: None)
    tried = []
    monkeypatch.setattr(agent2cli, "_mcp_apply",
                        lambda b, on: (tried.append((b.SERVER_KEY, on)), (True, "ok"))[1])
    monkeypatch.setattr(R, "health", lambda: [
        {"key": "burp", "label": "Burp", "state": "connected", "text": "Connected",
         "ok": True, "enabled": True, "connected": True, "mcp_installed": True,
         "tool_count": 1, "detail": ""},
        {"key": "zap", "label": "OWASP ZAP", "state": "failed",
         "text": "Connection failed", "ok": False, "enabled": True,
         "connected": False, "mcp_installed": True, "tool_count": 0,
         "detail": "refused"}])

    agent2cli._mcp_health(list(R.bridges()))
    assert tried == [("zap", True)], "retry touched a server that was fine"


def test_s_opens_the_config_editor_for_the_one_failing_server(monkeypatch):
    """[S] Settings, from the spec. Several failures would mean picking one for
    the user — `_mcp_config` is deliberately per-server, since URL, port and key
    are per-server facts."""
    import agent2cli
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda *a: "s")
    monkeypatch.setattr(agent2cli, "status_line", lambda *a, **k: None)
    edited = []
    monkeypatch.setattr(agent2cli, "_mcp_config", lambda b: edited.append(b.SERVER_KEY))
    monkeypatch.setattr(R, "health", lambda: [
        {"key": "zap", "label": "OWASP ZAP", "state": "failed",
         "text": "Connection failed", "ok": False, "enabled": True,
         "connected": False, "mcp_installed": True, "tool_count": 0,
         "detail": "refused"}])

    agent2cli._mcp_health([zap])
    assert edited == ["zap"]


def test_enter_closes_the_report_without_touching_anything(monkeypatch):
    import agent2cli
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda *a: "")
    monkeypatch.setattr(agent2cli, "_mcp_apply",
                        lambda b, on: pytest.fail("Enter reconnected something"))
    monkeypatch.setattr(agent2cli, "_mcp_config",
                        lambda b: pytest.fail("Enter opened the editor"))
    monkeypatch.setattr(R, "health", lambda: [
        {"key": "zap", "label": "OWASP ZAP", "state": "failed",
         "text": "Connection failed", "ok": False, "enabled": True,
         "connected": False, "mcp_installed": True, "tool_count": 0,
         "detail": "refused"}])

    agent2cli._mcp_health([zap])


# ── The web panel reads the same verdict ──────────────────────────────────────

def test_api_mcp_carries_the_health_verdict_for_every_server(client):
    body = client.get("/api/mcp").get_json()
    for s in body["servers"]:
        assert "health" in s, f"{s['key']} has no verdict for the panel to render"
        assert s["health"]["state"] in R._HEALTH_STATES
        assert isinstance(s["health"]["ok"], bool)


def test_the_panel_renders_the_verdict_instead_of_deriving_one():
    """⚠️ The browser deciding for itself what `connected` + `last_error` adds up
    to is how the dot goes green while /api/health returns 503."""
    from pathlib import Path
    js = Path("public/script.js").read_text(encoding="utf-8")
    dot = js.split("function mcpDot(s){")[1].split("\n}")[0]
    assert "s.health" in dot
    assert "h.text" in dot and "h.state" in dot


# ── Adversarial review fixes ──────────────────────────────────────────────────
# Each test below pins one defect found by review of the config redesign. They
# are grouped because they share a theme: the config row is CACHED and reached
# from three processes, so every path that writes it, fails to write it, or
# learns about someone else's write is load-bearing.


def test_the_cli_starts_a_sync_poller():
    """⚠️ THE CLI IS A SEPARATE PROCESS AND MUST LISTEN FOR ITS OWN INVALIDATIONS.

    `state.py` caches the config row and clears it only when the `mcp` topic
    fires; the sole thing that republishes ANOTHER process's write onto that bus
    is `SyncPoller`. Web and dual both start one — the CLI did not, so a URL or
    security key changed in the browser never reached a CLI that had already read
    it, and the next connect dialled the old endpoint with the old credential.
    Asserting on the source keeps this honest without launching the REPL: the
    call must be in `main()`, before the loop that would consume it.
    """
    from pathlib import Path
    src = Path("agent2cli.py").read_text(encoding="utf-8")
    body = src.split("def main(")[1]
    assert "poller.start()" in body, \
        "agent2cli.main() must start the sync poller — see agent2dual.py"


def test_every_surface_that_runs_a_repl_or_a_server_polls():
    """The pair to the test above: no surface may be deaf to the other two."""
    from pathlib import Path
    for entry in ("agent2cli.py", "agent2web.py", "agent2dual.py"):
        src = Path(entry).read_text(encoding="utf-8")
        assert "poller.start()" in src, f"{entry} never starts a sync poller"


def test_a_config_read_fault_never_caches_a_row_that_drops_the_key(monkeypatch):
    """⚠️ THE FAULT PATH USED TO WIPE THE ZAP CREDENTIAL — IN THIS PROCESS ONLY.

    `set_config` reads the whole row before writing so it can seed the cache with
    a COMPLETE row (the docstring there explains why a partial one is served as
    authoritative). But the read returns `{}` both for "no row yet" and for "the
    read failed", and those are opposite instructions: on a transient DB fault
    the seed became `{"url": new}` with no `security_key`, so `auth_headers()`
    sent nothing while the table still held the key. Nothing errors, nothing
    logs — ZAP just starts refusing every request until the process restarts.
    """
    from agent2.integrations import state as S
    from agent2.integrations.zap_mcp import zap

    init_db()
    zap.set_key("s3cret-zap-key")
    zap.set_url("http://127.0.0.1:8282")
    assert zap.key == "s3cret-zap-key"
    S.invalidate()                       # force the next read to hit the DB

    real = S._read_config_row
    monkeypatch.setattr(S, "_read_config_row",
                        lambda server: ({}, False))   # the fault: "we do not know"
    zap.set_url("http://127.0.0.1:9090")
    monkeypatch.setattr(S, "_read_config_row", real)

    assert zap.key == "s3cret-zap-key",         "a failed pre-write read cached a partial row and dropped the stored key"
    assert zap.url == "http://127.0.0.1:9090",  "the edit itself was lost — the fault guard must not skip the write"


def test_a_config_edit_still_takes_effect_when_the_db_is_unreachable(monkeypatch):
    """⚠️ THE OTHER HALF OF THE FAULT RULE, AND THE TWO LOOK ALIKE FROM HERE.

    `set_config` declines to seed its cache from a guess — but ONLY when the
    write landed, because then the table is the one thing that knows the truth
    and a re-read will find it. When the write failed too there is no truth to
    contradict, and refusing to seed silently downgrades every no-DB caller: a
    `/mcp zap config` on a pre-migration or locked DB would report success and
    then dial the old URL, because the assignment evaporated. The guard is
    `stored and not known` for exactly this reason, and dropping the `stored`
    half costs nothing a test can see unless one names this case.
    """
    from agent2.integrations import state as S
    from agent2.integrations.zap_mcp import zap

    def dead(*_a, **_k):
        raise RuntimeError("no such table: mcp_config")

    real = S._read_config_row
    monkeypatch.setattr(S, "_read_config_row", lambda server: ({}, False))
    monkeypatch.setattr("agent2.database.exe", dead)
    zap.set_url("http://127.0.0.1:7777")
    monkeypatch.setattr(S, "_read_config_row", real)   # read the CACHE, not the fault

    assert zap.url == "http://127.0.0.1:7777", "an edit made against an unreachable DB did not even take effect in this process"


def test_a_missing_row_still_seeds_the_cache(monkeypatch):
    """The other half of the pair: "no row" is NOT a fault and must still cache,
    or the fix above would turn every first write into a permanent cache miss."""
    from agent2.integrations import state as S
    from agent2.integrations.zap_mcp import zap

    init_db()
    S.clear_config("zap")
    S.invalidate()
    zap.set_url("http://127.0.0.1:7070")
    assert zap.url == "http://127.0.0.1:7070"
    assert S._config_cache.get("zap") is not None, "a first write left no cache entry"


def test_a_rejected_port_saves_nothing_at_all(client):
    """⚠️ A 400 MUST MEAN "NOTHING CHANGED". The route applied the URL first and
    validated the port second, so a bad port returned 400 while the endpoint had
    already moved — and the browser's error path stops before `loadMcp()`, so the
    panel kept rendering the old URL. Neither side reported the half-save.
    """
    from agent2.integrations import state as S
    from agent2.integrations.zap_mcp import zap

    init_db()
    zap.set_url("http://127.0.0.1:8282")
    before = zap.url

    resp = client.post("/api/mcp/zap/config",
                       json={"url": "http://127.0.0.1:1234", "port": "99999"})
    assert resp.status_code == 400
    S.invalidate()
    assert zap.url == before, "a rejected request still moved the endpoint"


def test_a_connect_is_not_a_save(client, monkeypatch):
    """⚠️ `/connect` took a URL and PERSISTED it before dialling, so one failed
    attempt at a typo'd address replaced a working stored endpoint. The panel
    sends the URL box on every Connect click, so this was a keystroke away.
    `/config` is the endpoint that saves.
    """
    from agent2.integrations import state as S
    from agent2.integrations.zap_mcp import zap

    init_db()
    zap.set_url("http://127.0.0.1:8282")
    monkeypatch.setattr(type(zap), "connect", lambda self: (False, "refused"))

    resp = client.post("/api/mcp/zap/connect", json={"url": "http://127.0.0.1:9999"})
    assert resp.get_json()["ok"] is False
    S.invalidate()
    assert zap.url == "http://127.0.0.1:8282", \
        "a failed connect overwrote the stored endpoint with the address it tried"


def test_a_non_object_json_body_is_not_a_500(client):
    """`or {}` only catches the FALSY non-dicts; `[1]` has no `.get` and 500s."""
    for path in ("/api/mcp/zap/connect", "/api/mcp/zap/auto", "/api/mcp/zap/config"):
        resp = client.post(path, json=[1, 2, 3])
        assert resp.status_code != 500, f"{path} 500s on a JSON array body"


def test_setting_a_port_keeps_userinfo_and_the_path(client):
    """⚠️ `parts.password` IS `None`, NOT `""`, FOR A URL LIKE `user@host`.

    `set_port` built the netloc with an f-string, so a password-less userinfo URL
    became `alice:None@host` — a password this code invented, sent as Basic auth
    on every later connect. The proxy 401s and the bridge reports "Could not
    connect" while the stored URL now contains a credential the user never typed.
    The `alice:pw@` case below never caught it: with a password present the buggy
    and correct forms agree, which is exactly why both are asserted here.
    """
    from agent2.integrations import state as S
    from agent2.integrations.zap_mcp import zap

    init_db()
    zap.set_url("http://alice:pw@127.0.0.1:8282/mcp")
    assert zap.set_port(9090) is True
    S.invalidate()
    assert zap.url == "http://alice:pw@127.0.0.1:9090/mcp", zap.url

    zap.set_url("http://alice@127.0.0.1:8282/mcp")
    assert zap.set_port(9091) is True
    S.invalidate()
    assert zap.url == "http://alice@127.0.0.1:9091/mcp", \
        f"set_port invented a password: {zap.url}"
    assert "None" not in zap.url


def test_an_env_provided_zap_key_is_reported_as_set(client):
    """⚠️ `config_for`'s `key_default` was never passed by ANY caller, so a key
    given via `ZAP_MCP_KEY` read as `key_set: False` / `key_source: "none"` on
    every surface while `auth_headers()` was sending it — the panel and `/mcp zap
    config` both invited the user to "set" a key that was already in use.
    """
    from agent2.integrations import state as S
    from agent2.integrations.zap_mcp import zap

    init_db()
    S.clear_config("zap")
    S.invalidate()
    assert zap.ENV_KEY == config.ZAP_MCP_KEY, "the bridge must expose the env key"

    view = S.config_for("zap", url_default=zap.DEFAULT_URL, key_default="env-key-xyz")
    assert view["key_set"] is True
    assert view["key_source"] == "env"
    assert "env-key-xyz" not in json.dumps(view), "the view leaked the raw key"


def test_the_panel_escapes_quotes_because_it_builds_attributes():
    """⚠️ `esc()` output goes into `value="${...}"`, so escaping only &<> lets a
    stored URL close the attribute and add an event handler."""
    from pathlib import Path
    js = Path("public/script.js").read_text(encoding="utf-8")
    lines = js.splitlines()
    i = next(n for n, ln in enumerate(lines) if ln.startswith("const esc ="))
    body = "\n".join(lines[i:i + 3])          # the declaration wraps
    assert "&quot;" in body and "&#39;" in body, \
        f"esc() does not escape quotes but is used in attributes:\n{body}"


def test_the_panel_never_sends_a_whitespace_only_key():
    """⚠️ A box holding spaces is truthy in JS: it was sent, stripped to "" on the
    server, and read as the deliberate clear — destroying a stored credential
    without the confirm() that `mcpClearKey` requires."""
    from pathlib import Path
    js = Path("public/script.js").read_text(encoding="utf-8")
    save = js.split("async function mcpSaveConfig")[1].split("\n}")[0]
    assert "keyEl.value.trim()" in save, \
        "the save path tests the raw value, so whitespace reads as 'clear the key'"
