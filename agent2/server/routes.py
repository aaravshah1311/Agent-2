# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/routes.py
────────────────
All REST API endpoints registered on the Flask app.
Call register_routes(app) from main.py after creating the app instance.

HEALTH REPORTING (`GET /api/health`) — 200 healthy / 503 + `problems`
────────────────────────────────────────────────────────────────────
Reports DB reachability + schema version, pool, WAL, scheduler and sync poller.

⚠️ A DISABLED OPTIMIZATION IS NOT UNHEALTHY.
The WAL checkpointer and the turn scheduler can both be switched off on purpose.
Reporting "degraded" for a supported configuration produces an alert people
learn to ignore, which is worse than no alert.

⚠️ THE SCHEDULER CHECK IS `worker_starts and not workers`, NOT
`enabled and not workers`.
`submit()` starts workers lazily, so an enabled pool with zero workers is the
NORMAL state of a server that has not handled a turn yet — the naive form
returned 503 on a healthy app. The real fault being detected is workers that
started and then went away. (test_idle_server_is_healthy)

⚠️ EVERY SECTION IS ISOLATED BEHIND `_section()`.
A health endpoint that 500s because one counter raised reports the whole app
down when only the reporting broke.

⚠️ THE DB IS THE ONLY HARD DEPENDENCY.

⚠️ `/api/health` CARRIES COUNTERS ONLY, AND IT IS THE ONE ROUTE HERE THAT AN
UNAUTHENTICATED CALLER CAN REACH — from loopback, so Docker's healthcheck keeps
working (`auth.LOOPBACK_PATHS`). The payload carries no key material, no chat or
memory text, not even the DB path. `sync.resources` are label enums (one is
literally `api_keys`) and never values. Anything added here is readable by
anything that can reach 127.0.0.1 on this box, by definition.

AUTHORIZATION (Task 14)
───────────────────────
⚠️ THIS FILE NO LONGER DECIDES WHO MAY CALL IT — `agent2/server/auth.py` DOES.
`register_routes()` calls `auth.install(app)` as its first act, which attaches a
`before_request` guard covering every route on the app, the `/api/auth/*`
endpoints, and the security headers. It is done HERE rather than in `agent2web.py`
so the guard cannot be forgotten by a second entry point: an app that has this API
has the guard, in one step, with nothing to keep in sync.

Do not add a per-route `@requires_auth` decorator. A decorator is opt-in, so the
failure mode is a new route that silently has no guard — and that is precisely the
class of bug the single `before_request` makes impossible. A route that must be
reachable anonymously belongs in `auth.PUBLIC_PATHS` or `auth.LOOPBACK_PATHS`,
where the exception is visible in one list instead of absent from one decorator.

(test_health.py — 14 tests, sabotage-verified · test_webauth.py)
"""

from flask import request, jsonify

from agent2.config import (
    OS_NAME, SHELL_BIN, SHELL_LABEL,
    MODELS, MODES, DEFAULT_MODEL, DEFAULT_MODE,
)
from agent2.database import qall, qone, exe
from agent2.llm.keys import rotator
from agent2.llm import providers
from agent2.core import memory as core_memory
from agent2.core import rules as core_rules
from agent2.core import context as core_context
from agent2.core import workspace as core_workspace
from agent2.core.session import sessions as core_sessions
from agent2.core import tasks as core_tasks
from agent2.core import recovery as core_recovery
from agent2.core import permissions as core_perms
from agent2.llm import capabilities as core_caps
from agent2.llm import router as core_router
from agent2.server import auth


def _port_fits(bridge, url, port) -> bool:
    """Would `set_port(port)` succeed against *url* (or the stored one)?

    A dry run for `/api/mcp/<key>/config`, which must answer "is this request
    acceptable?" BEFORE it persists the URL — see the comment at that call site.
    ⚠️ It duplicates no rule: the range and the host requirement are still
    `set_port`'s to state, and this asks the same question of a candidate URL
    without writing. The URL matters because `set_port` needs a host to edit, so
    a request that supplies both must be judged against the incoming URL rather
    than the one on disk.
    """
    from urllib.parse import urlsplit
    try:
        num = int(str(port).strip())
    except Exception:
        return False
    if not (1 <= num <= 65535):
        return False
    candidate = str(url).strip() if url not in (None, "") else (bridge.url or bridge.DEFAULT_URL)
    try:
        return bool(urlsplit(candidate).hostname)
    except Exception:
        return False


def register_routes(app) -> None:
    """Attach all /api/* routes to *app*.

    ⚠️ The auth guard goes on FIRST and unconditionally. Registering the API
    without it is not a supported state — see this module's docstring.
    """

    auth.install(app)

    # ── Chats ──────────────────────────────────────────────────────────────────

    @app.route("/api/chats", methods=["GET"])
    def api_list_chats():
        # Project isolation: by default only chats for the server's working
        # directory (the current project). Pass ?all=1 to list every project's
        # chats (used by the "resume other project" picker).
        if (request.args.get("all") or "").strip() in ("1", "true", "yes"):
            return jsonify(core_context.list_all_chats())
        return jsonify(core_context.list_chats_for_cwd())

    @app.route("/api/chats", methods=["POST"])
    def api_new_chat():
        d = request.json or {}
        chat = core_context.new_chat(
            d.get("model", DEFAULT_MODEL), d.get("mode", DEFAULT_MODE))
        return jsonify(chat)

    @app.route("/api/chats/<cid>/pause", methods=["POST"])
    def api_pause_chat(cid):
        core_context.pause_chat(cid)
        return jsonify(qone("SELECT * FROM chats WHERE id=?", (cid,)))

    @app.route("/api/chats/<cid>/resume", methods=["POST"])
    def api_resume_chat(cid):
        core_context.resume_chat(cid)
        return jsonify(qone("SELECT * FROM chats WHERE id=?", (cid,)))

    @app.route("/api/chats/<cid>", methods=["GET"])
    def api_get_chat(cid):
        chat = qone("SELECT * FROM chats WHERE id=?", (cid,))
        if not chat:
            return jsonify({"error": "not found"}), 404
        chat["messages"] = qall(
            "SELECT * FROM messages WHERE chat_id=? ORDER BY created_at", (cid,)
        )
        return jsonify(chat)

    @app.route("/api/chats/<cid>/tasks", methods=["GET"])
    def api_get_chat_tasks(cid):
        """The PERSISTENT checklist for a chat (`agent_tasks`).

        ⚠️ Not `/api/tasks` — that one lists in-flight agent turns from
        `core.session` and dies with the process. This one is the durable plan,
        and it is the reason a browser reload does not lose the checklist: the
        live `chat_tasks` socket event only reaches a page that was already open.

        A chat with no session yet is not an error — it is a chat where the model
        has not planned anything, so the empty payload is the honest answer.
        """
        row = core_tasks.latest_session_for_chat(cid)
        if not row:
            return jsonify({"session_id": "", "tasks": [], "summary": {}})
        return jsonify(core_tasks.payload(str(row["id"])))

    # ── Recovery (Task 3) ──────────────────────────────────────────────────────
    #
    # ⚠️ THE BROWSER HALF OF RECOVERY IS EXPLICIT, LIKE THE CLI'S.
    # `GET` only looks; resuming or discarding needs a POST naming the session.
    # Nothing here is reachable from chat pause/resume, and neither verb reopens a
    # completed task — `core/recovery` owns that guarantee and this is a thin skin
    # over it, so the two surfaces cannot drift into different recovery rules.

    @app.route("/api/recovery", methods=["GET"])
    def api_recovery():
        """Interrupted task sessions for this project, newest first."""
        try:
            cwd = str(core_workspace.root())
        except Exception:
            cwd = ""
        return jsonify({"candidates": [c.to_dict()
                                       for c in core_recovery.candidates(cwd)]})

    @app.route("/api/recovery/<sid>", methods=["GET"])
    def api_recovery_plan(sid):
        """The full plan for one session: done / resume / waiting + verdicts."""
        return jsonify(core_recovery.plan(sid).to_dict())

    @app.route("/api/recovery/<sid>", methods=["POST"])
    def api_recovery_act(sid):
        """`{"action": "resume"|"discard"}` — adopt or decline one session.

        A `resume` returns the plan the caller should show, including
        `needs_verification`: the browser must be able to warn about an in-flight
        destructive step for the same reason the terminal panel does.
        """
        action = str((request.json or {}).get("action") or "").strip().lower()
        if action == "discard":
            n = core_recovery.abandon(sid, reason="declined in web UI")
            return jsonify({"ok": True, "action": "discard", "cancelled": n})
        if action != "resume":
            return jsonify({"error": "action must be 'resume' or 'discard'"}), 400
        rp = core_recovery.recover(sid, reason="resumed in web UI")
        if rp.is_empty():
            return jsonify({"error": "nothing to recover"}), 404
        return jsonify({"ok": True, "action": "resume", "plan": rp.to_dict()})

    @app.route("/api/chats/<cid>", methods=["PUT"])
    def api_update_chat(cid):
        d = request.json or {}
        for col in ("title", "model", "mode"):
            v = d.get(col, "").strip()
            if v:
                exe(f"UPDATE chats SET {col}=? WHERE id=?", (v, cid))
        return jsonify(qone("SELECT * FROM chats WHERE id=?", (cid,)))

    @app.route("/api/chats/<cid>", methods=["DELETE"])
    def api_del_chat(cid):
        exe("DELETE FROM chats WHERE id=?", (cid,))
        return jsonify({"ok": True})

    # ── Memories ───────────────────────────────────────────────────────────────

    @app.route("/api/memories", methods=["GET"])
    def api_get_mems():
        # Centralized backend (agent2.core.memory) — shared with the CLI.
        return jsonify(list(reversed(core_memory.list_memories())))

    @app.route("/api/memories", methods=["POST"])
    def api_add_mem():
        """Add a memory. `shared: true` makes it visible in EVERY project.

        Task 23: without the flag a memory belongs to the workspace that saved it.
        The flag is the explicit half of "unless explicitly shared" — it is never
        inferred, and it is the only way a write leaves this project.
        """
        d = request.json or {}
        content = (d.get("content") or "").strip()
        row = core_memory.add_memory(content, shared=d.get("shared") is True)
        if not row:
            return jsonify({"error": "empty"}), 400
        return jsonify(row)

    @app.route("/api/memories/<mid>", methods=["DELETE"])
    def api_del_mem(mid):
        core_memory.delete_memory(mid)
        return jsonify({"ok": True})

    @app.route("/api/memories", methods=["DELETE"])
    def api_del_mems():
        """Bulk delete: {"ids": [...]} or {"all": true}.

        One transaction and ONE cache invalidation for the whole set. Clearing
        200 memories one-by-one was 200 requests, 200 transactions and 200
        rebuilds of the cached system-prompt block.

        ⚠️ `all` is required to be explicit. An empty/absent `ids` list deletes
        NOTHING rather than falling through to "no filter, so everything" — the
        classic bulk-delete footgun.
        """
        d = request.json or {}
        if d.get("all") is True:
            return jsonify({"ok": True, "deleted": core_memory.clear_memories()})
        ids = d.get("ids")
        if not isinstance(ids, list) or not ids:
            return jsonify({"error": "ids must be a non-empty list, or pass all=true"}), 400
        return jsonify({"ok": True, "deleted": core_memory.delete_memories(ids)})

    @app.route("/api/memories/prune", methods=["POST"])
    def api_prune_mems():
        """Drop the least valuable memories beyond `keep`, sparing importance
        >= `min_importance`. Ranked exactly as the prompt block is."""
        d = request.json or {}
        try:
            keep = int(d.get("keep", 500))
            floor = int(d.get("min_importance", 10))
        except (TypeError, ValueError):
            return jsonify({"error": "keep and min_importance must be integers"}), 400
        removed = core_memory.prune_memories(keep=keep, min_importance=floor)
        return jsonify({"ok": True, "pruned": removed,
                        "remaining": core_memory.count_memories()})

    # ── Rules ──────────────────────────────────────────────────────────────────

    @app.route("/api/rules", methods=["GET"])
    def api_get_rules():
        # Centralized backend (agent2.core.rules) — shared with the CLI.
        return jsonify(core_rules.list_rules())

    @app.route("/api/rules", methods=["POST"])
    def api_add_rule():
        """Add a rule. `shared: true` makes it apply in EVERY project (Task 23)."""
        d = request.json or {}
        content = (d.get("content") or "").strip()
        row = core_rules.add_rule(content, shared=d.get("shared") is True)
        if not row:
            return jsonify({"error": "empty"}), 400
        return jsonify(row)

    @app.route("/api/rules/<rid>", methods=["PUT"])
    def api_toggle_rule(rid):
        # ⚠️ The row comes back from `toggle_rule`, not from a `SELECT *` here: a
        # raw read by id is unscoped, so a toggle refused as another project's rule
        # would still have returned that rule's contents to this caller.
        row = core_rules.toggle_rule(rid)
        if row is None:
            return jsonify({"error": "not found"}), 404
        return jsonify(row)

    @app.route("/api/rules/<rid>", methods=["DELETE"])
    def api_del_rule(rid):
        core_rules.delete_rule(rid)
        return jsonify({"ok": True})

    @app.route("/api/rules", methods=["DELETE"])
    def api_del_rules():
        """Bulk delete: {"ids": [...]}. See api_del_mems on why `all` is explicit."""
        d = request.json or {}
        if d.get("all") is True:
            rows = core_rules.list_rules()
            n = core_rules.delete_rules([r["id"] for r in rows])
            return jsonify({"ok": True, "deleted": n})
        ids = d.get("ids")
        if not isinstance(ids, list) or not ids:
            return jsonify({"error": "ids must be a non-empty list, or pass all=true"}), 400
        return jsonify({"ok": True, "deleted": core_rules.delete_rules(ids)})

    @app.route("/api/rules/active", methods=["PUT"])
    def api_set_rules_active():
        """Set many rules active/inactive at once: {"ids": [...], "active": bool}.

        Distinct from PUT /api/rules/<rid>, which TOGGLES. A bulk toggle would
        leave a mixed selection mixed; this sets them uniformly.
        """
        d = request.json or {}
        ids = d.get("ids")
        if not isinstance(ids, list) or not ids:
            return jsonify({"error": "ids must be a non-empty list"}), 400
        if not isinstance(d.get("active"), bool):
            return jsonify({"error": "active must be a boolean"}), 400
        n = core_rules.set_rules_active(ids, d["active"])
        return jsonify({"ok": True, "updated": n})

    # ── API Keys ───────────────────────────────────────────────────────────────

    @app.route("/api/keys", methods=["GET"])
    def api_get_keys():
        return jsonify(rotator.status())

    @app.route("/api/keys", methods=["POST"])
    def api_add_key():
        d    = request.json or {}
        key  = d.get("key", "").strip().replace(" ", "").replace("\n", "")
        name = d.get("name", "").strip()
        if not key or len(key) < 15:
            return jsonify({"error": "invalid key"}), 400
        if any(e["key"] == key for e in rotator.entries):
            return jsonify({"ok": False, "error": "already_exists"}), 409
        ok, label = rotator.add(key, name or None)
        return jsonify({"ok": ok, "label": label, "keys": rotator.status()})

    @app.route("/api/keys/<label>", methods=["PUT"])
    def api_update_key(label):
        d = request.json or {}
        if "name" in d:
            rotator.set_name(label, d["name"])
        return jsonify({"ok": True, "keys": rotator.status()})

    @app.route("/api/keys/<label>", methods=["DELETE"])
    def api_del_key(label):
        rotator.remove(label)
        return jsonify({"ok": True, "keys": rotator.status()})

    @app.route("/api/keys/<label>/reset", methods=["POST"])
    def api_reset_key(label):
        rotator.reset_key(label)
        return jsonify({"ok": True, "keys": rotator.status()})

    @app.route("/api/keys/<label>/pin", methods=["POST"])
    def api_pin_key(label):
        d = request.json or {}
        rotator.pin(label if d.get("pin") else None)
        return jsonify({"ok": True, "keys": rotator.status()})

    # ── Custom providers (bring your own API) ──────────────────────────────────

    @app.route("/api/providers", methods=["GET"])
    def api_get_providers():
        return jsonify(providers.list_providers(safe=True))

    @app.route("/api/providers", methods=["POST"])
    def api_add_provider():
        d = request.json or {}
        base_url = (d.get("base_url") or "").strip()
        api_key  = (d.get("api_key")  or "").strip()
        model_id = (d.get("model_id") or "").strip()
        name     = (d.get("name")     or "").strip()
        fmt      = (d.get("format")   or "openai").strip().lower()
        user_agent = (d.get("user_agent") or "").strip()
        if not base_url or not api_key or not model_id:
            return jsonify({"error": "base_url, api_key and model_id are required"}), 400
        if not base_url.startswith("http"):
            return jsonify({"error": "base_url must start with http(s)://"}), 400
        p = providers.add_provider(name, base_url, api_key, model_id, fmt, user_agent)
        return jsonify({"ok": True, "provider": p,
                        "providers": providers.list_providers(safe=True)})

    @app.route("/api/providers/<pid>", methods=["PUT"])
    def api_update_provider(pid):
        d = request.json or {}
        base_url = (d.get("base_url") or "").strip()
        if base_url and not base_url.startswith("http"):
            return jsonify({"error": "base_url must start with http(s)://"}), 400
        p = providers.update_provider(
            pid,
            name=d.get("name"),
            base_url=d.get("base_url"),
            model_id=d.get("model_id"),
            format=d.get("format"),
            api_key=d.get("api_key"),
            user_agent=d.get("user_agent"),
        )
        if not p:
            return jsonify({"error": "provider not found"}), 404
        return jsonify({"ok": True,
                        "providers": providers.list_providers(safe=True)})

    @app.route("/api/providers/<pid>", methods=["DELETE"])
    def api_del_provider(pid):
        providers.remove_provider(pid)
        return jsonify({"ok": True, "providers": providers.list_providers(safe=True)})

    @app.route("/api/providers/<pid>/test", methods=["POST"])
    def api_test_provider(pid):
        prov = providers.get_provider(pid)
        if not prov:
            return jsonify({"ok": False, "error": "not found"}), 404
        try:
            r = providers.chat(prov, [{"role": "user", "content": "Reply with the single word: OK"}],
                               "You are a connection test. Reply concisely.")
            return jsonify({"ok": True, "text": (r.get("text") or "")[:200],
                            "tokens": r.get("tokens", 0)})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)[:400]}), 200

    # ── MCP bridges (registry-driven; Burp + every other server) ───────────────
    # ⚠️ THERE IS NO `/api/burp*` ANY MORE, AND ITS ABSENCE IS DELIBERATE.
    # Four bespoke Burp routes lived here (`GET /api/burp`, `POST /api/burp/
    # connect|disconnect|auto`) from before the registry existed. They were
    # removed on explicit instruction once this generic surface reached parity —
    # rule 28 forbids removing a feature *silently*, not removing one that has
    # been superseded and asked for. The replacement is exact and total:
    #
    #     GET  /api/burp             ->  GET  /api/mcp        (every server)
    #     POST /api/burp/connect     ->  POST /api/mcp/burp/connect
    #     POST /api/burp/disconnect  ->  POST /api/mcp/burp/disconnect
    #     POST /api/burp/auto        ->  POST /api/mcp/burp/auto
    #
    # Do NOT reintroduce a per-server route set. That is the shape this endpoint
    # exists to end: a second list of servers in a surface keeps working and
    # simply never shows the next bridge anyone registers.

    @app.route("/api/mcp", methods=["GET"])
    def api_mcp_status():
        """Every server's live status plus its editable config.

        ⚠️ NO CREDENTIAL IS IN THIS PAYLOAD AND NONE MAY EVER BE. `status()`
        carries no key by construction and `state.config_for()` returns `key_set`
        plus fixed-width bullets — never the value, never its length.
        `test_api_mcp_never_ships_the_zap_key` is what keeps that true.
        """
        from agent2.integrations import registry as mcp_registry
        from agent2.integrations import state as mcp_state
        # Task 10: the panel renders `health` rather than deciding for itself what
        # "connected + an error string" adds up to. ⚠️ That is the whole point of
        # `registry.health()` — this endpoint, `/mcp health` and the `mcp` section
        # of `/api/health` are three renderers of ONE verdict, and the browser
        # deriving its own is how the dot goes green while the endpoint says 503.
        verdicts = {r["key"]: r for r in mcp_registry.health()}
        out = []
        for s in mcp_registry.statuses():
            # `statuses()` names the server id "key" — it predates ZAP having a
            # credential, where "key" means something else entirely. Renaming it
            # would break `script.js`, so the ambiguity is documented, not fixed.
            bridge = mcp_registry.get(s.get("key", ""))
            if bridge is not None:
                s = dict(s)
                s["config"] = mcp_state.config_for(bridge.SERVER_KEY,
                                                   url_default=bridge.DEFAULT_URL,
                                                   key_default=bridge.ENV_KEY)
                s["port"] = bridge.port
                s["takes_key"] = bool(bridge.SERVER_KEY == "zap")
                s["setup_hint"] = bridge.SETUP_HINT
                s["health"] = verdicts.get(bridge.SERVER_KEY, {})
            out.append(s)
        return jsonify({"servers": out})

    @app.route("/api/mcp/<key>/config", methods=["POST"])
    def api_mcp_config(key):
        """Persist url / port / security_key for one server (CLI parity).

        ⚠️ AN ABSENT FIELD MEANS "LEAVE IT ALONE", AN EMPTY ONE MEANS "CLEAR IT",
        and the difference is what stops the browser wiping a working ZAP key.
        The config form pre-fills the key box with bullets, so a user editing only
        the port submits with the key field untouched; the client omits `key`
        entirely in that case, and `state.set_config` skips the column. Sending
        `""` is a deliberate clear and stays supported.
        """
        from agent2.integrations import registry as mcp_registry
        from agent2.integrations import state as mcp_state
        bridge = mcp_registry.get(key)
        if bridge is None:
            return jsonify({"ok": False, "error": f"unknown MCP server '{key}'"}), 404
        d = request.json if isinstance(request.json, dict) else {}
        changed = []

        # ⚠️ VALIDATE BEFORE WRITING ANYTHING. `set_url` persists immediately, so
        # applying it first and then rejecting the port returned 400 — "nothing was
        # saved" as far as the client is concerned — over an endpoint that had
        # already moved. The browser's error path stops before `loadMcp()`, so the
        # panel kept showing the old URL too: a half-applied save that neither side
        # reports. A port is only checkable against the URL it will be applied to,
        # which is why this recomputes the candidate rather than trusting the
        # stored one.
        port = d.get("port")
        if port not in (None, ""):
            if not _port_fits(bridge, d.get("url"), port):
                return jsonify({"ok": False, "error": f"not a usable port: {port}",
                                "status": bridge.status()}), 400

        if "url" in d:
            bridge.set_url(str(d.get("url") or "").strip())
            changed.append("url")
        if port not in (None, ""):
            if not bridge.set_port(port):
                return jsonify({"ok": False, "error": f"not a usable port: {port}",
                                "status": bridge.status()}), 400
            changed.append("port")
        if "security_key" in d:
            bridge.set_security_key(str(d.get("security_key") or "").strip())
            changed.append("security_key")

        return jsonify({
            "ok": True,
            "changed": changed,
            # The bridge keeps whatever session it already had: a config edit is
            # not a reconnect (same rule the CLI editor states).
            "reconnect_required": bool(changed and bridge.is_connected()),
            "status": bridge.status(),
            "config": mcp_state.config_for(bridge.SERVER_KEY,
                                           url_default=bridge.DEFAULT_URL,
                                           key_default=bridge.ENV_KEY),
            "port": bridge.port,
        })

    @app.route("/api/mcp/connect", methods=["POST"])
    def api_mcp_connect_all():
        """Dial every configured server — the web twin of a bare `/mcp connect`."""
        from agent2.integrations import registry as mcp_registry
        results = []
        for bridge in mcp_registry.bridges():
            bridge.set_auto_connect(True)
            if bridge.is_connected():
                results.append({"server": bridge.SERVER_KEY, "ok": True,
                                "message": f"{bridge.LABEL}: already connected"})
                continue
            ok_, msg = bridge.connect()
            results.append({"server": bridge.SERVER_KEY, "ok": ok_, "message": msg})
        return jsonify({"ok": all(r["ok"] for r in results) if results else False,
                        "results": results, "servers": mcp_registry.statuses()})

    @app.route("/api/mcp/disconnect", methods=["POST"])
    def api_mcp_disconnect_all():
        """Drop every server. Also clears auto-connect, for the CLI's reason:
        an enabled bridge is redialled at the next turn, so leaving the flag on
        would make this look like it did nothing."""
        from agent2.integrations import registry as mcp_registry
        for bridge in mcp_registry.bridges():
            bridge.set_auto_connect(False)
            bridge.disconnect()
        return jsonify({"ok": True, "servers": mcp_registry.statuses()})

    @app.route("/api/mcp/<key>/connect", methods=["POST"])
    def api_mcp_connect(key):
        from agent2.integrations import registry as mcp_registry
        bridge = mcp_registry.get(key)
        if bridge is None:
            return jsonify({"ok": False, "error": f"unknown MCP server '{key}'"}), 404
        d = request.json if isinstance(request.json, dict) else {}
        url = (d.get("url") or "").strip()
        # ⚠️ A CONNECT IS NOT A SAVE. `bridge.url = url` writes `mcp_config`, so
        # dialling a one-off address used to repoint the server permanently — and
        # the panel sends the URL box on every Connect click, which means a failed
        # attempt at a typo'd address replaced a working stored endpoint with the
        # typo. `/api/mcp/<key>/config` is the endpoint that persists; this one
        # restores what it found unless the dial actually succeeded.
        previous = bridge.url
        if url and url != previous:
            bridge.set_url(url)
        ok, msg = bridge.connect()
        if url and url != previous and not ok:
            bridge.set_url(previous)
        return jsonify({"ok": ok, "message": msg, "status": bridge.status()})

    @app.route("/api/mcp/<key>/disconnect", methods=["POST"])
    def api_mcp_disconnect(key):
        from agent2.integrations import registry as mcp_registry
        bridge = mcp_registry.get(key)
        if bridge is None:
            return jsonify({"ok": False, "error": f"unknown MCP server '{key}'"}), 404
        bridge.disconnect()
        return jsonify({"ok": True, "message": "Disconnected.", "status": bridge.status()})

    @app.route("/api/mcp/<key>/auto", methods=["POST"])
    def api_mcp_auto(key):
        """Toggle auto-connect (connect on every agent turn) for one server."""
        from agent2.integrations import registry as mcp_registry
        bridge = mcp_registry.get(key)
        if bridge is None:
            return jsonify({"ok": False, "error": f"unknown MCP server '{key}'"}), 404
        # A JSON body that parses but is not an object (`[]`, `"x"`, `3`) has no
        # `.get`, and `or {}` only catches the falsy ones — `[1]` reaches this line
        # and 500s. The three MCP routes that read a body all guard the same way.
        d = request.json if isinstance(request.json, dict) else {}
        bridge.set_auto_connect(bool(d.get("enabled")))
        return jsonify({"ok": True, "status": bridge.status()})

    # ── Personal Intelligence Layer (offline personalization) ──────────────────
    # Shared, offline layer between the user and the model. All endpoints are
    # best-effort: the PIL facade never raises, so failures degrade to "do nothing".

    @app.route("/api/pil/settings", methods=["GET"])
    def api_pil_settings():
        from agent2.core import pil
        return jsonify(pil.get_settings())

    @app.route("/api/pil/settings", methods=["PUT"])
    def api_pil_update_settings():
        from agent2.core import pil
        return jsonify(pil.update_settings(request.json or {}))

    @app.route("/api/pil/predict", methods=["POST"])
    def api_pil_predict():
        """Offline ghost-text suggestion for the current input. Never mutates."""
        from agent2.core import pil
        d = request.json or {}
        text = d.get("text", "") or ""
        suggestion = pil.predict(
            text,
            project=(d.get("project") or "").strip(),
            lang=(d.get("lang") or "").strip(),
        )
        return jsonify({"suggestion": suggestion})

    @app.route("/api/pil/feedback", methods=["POST"])
    def api_pil_feedback():
        """Passive learning signal from the front-end: accept / ignore / delete.
        Never asks the user anything — this is a silent behavioural signal."""
        from agent2.core import pil
        d = request.json or {}
        action = (d.get("action") or "").strip().lower()
        suggestion = d.get("suggestion", "") or ""
        context = d.get("context", "") or ""
        project = (d.get("project") or "").strip()
        if action == "accept":
            pil.observe_accept(suggestion, context=context, project=project)
        elif action == "ignore":
            pil.observe_ignore(suggestion, context=context, project=project)
        elif action == "delete":
            pil.observe_delete(suggestion, context=context, project=project)
        else:
            return jsonify({"ok": False, "error": "unknown action"}), 400
        return jsonify({"ok": True})

    @app.route("/api/pil/learn", methods=["POST"])
    def api_pil_learn():
        """Manually feed text into the shared memory (e.g. importing history)."""
        from agent2.core import pil
        d = request.json or {}
        pil.learn_from_message(d.get("text", "") or "",
                               project=(d.get("project") or "").strip())
        return jsonify({"ok": True, "stats": pil.stats()})

    @app.route("/api/pil/optimize", methods=["POST"])
    def api_pil_optimize():
        """Manual 'Optimize now' — forces a background-optimization pass."""
        from agent2.core import pil
        d = request.json or {}
        report = pil.optimize(aggressive=bool(d.get("aggressive")), force=True)
        return jsonify({"ok": True, "report": report})

    @app.route("/api/pil/stats", methods=["GET"])
    def api_pil_stats():
        from agent2.core import pil
        return jsonify(pil.stats())

    @app.route("/api/pil/wipe", methods=["POST"])
    def api_pil_wipe():
        """'Forget me' privacy control — erase the entire centralized memory."""
        from agent2.core import pil
        pil.wipe()
        return jsonify({"ok": True, "stats": pil.stats()})

    # ── Sync layer ─────────────────────────────────────────────────────────────

    @app.route("/api/sync", methods=["GET"])
    def api_sync():
        """Sync-layer snapshot: resource versions, poller health, cache stats.

        Lets a client (or a human debugging dual mode) see whether cross-process
        change propagation is actually live.
        """
        from agent2.core import sync
        return jsonify(sync.stats())

    @app.route("/api/sync/poll", methods=["POST"])
    def api_sync_poll():
        """Force an immediate change check instead of waiting for the next tick."""
        from agent2.core import sync
        return jsonify({"ok": True, "changed": sync.poll_changes()})

    # ── Health ─────────────────────────────────────────────────────────────────

    def _section(fn):
        """Collect one health section without letting it fail the whole report.

        ⚠️ A health endpoint that 500s because one counter raised is worse than no
        health endpoint: the monitor now reports the app down when the only broken
        thing is the reporting. Each section degrades to its own error string.
        """
        try:
            return fn(), None
        except Exception as ex:
            return None, f"{type(ex).__name__}: {ex}"

    def mcp_health_report():
        """The `mcp` health section (Task 10).

        ⚠️ IT ONLY FETCHES. Every judgement — which states count as a fault, what
        each one is called — belongs to `registry.health_report()`, because the CLI
        and the settings panel render the same verdict and a second copy here would
        be the third. An import failure is not a fault either: a build without the
        `mcp` package has no servers to be unhealthy about.
        """
        try:
            from agent2.integrations import registry as mcp_registry
        except Exception:
            return {"servers": [], "total": 0, "connected": 0,
                    "enabled": 0, "failing": 0, "problems": []}
        return mcp_registry.health_report()

    @app.route("/api/health", methods=["GET"])
    def api_health():
        """Aggregate health snapshot: DB, pool, WAL, scheduler, sync poller, MCP.

        Built for monitoring, `agent2 status`, and the settings UI. There is no
        auth on this or any other route (the documented gap), so it reports
        COUNTERS ONLY — no keys, no chat content, no memory text, not even the DB
        path.

        ⚠️ A disabled optimization is NOT unhealthy. The WAL checkpointer and the
        turn scheduler are both documented as optimizations layered over working
        defaults, and both can be switched off on purpose
        (`AGENT2_WAL_CHECKPOINT_SEC=0`, `AGENT2_MAX_CONCURRENT_TURNS=0`). Reporting
        "degraded" for those would make the endpoint cry wolf, and an alert that
        fires on a supported configuration is an alert people learn to ignore.
        Only genuine faults set `ok: false`.

        ⚠️ Task 10 applies that same rule to MCP, where it bites hardest: BOTH
        bridges ship auto-connect OFF, so on a fresh install every server is
        disconnected and the endpoint must still return 200. Only a server the
        user ENABLED and that we know is not working is a fault — the verdict is
        `registry.health()`'s to make, not this route's (see its docstring).
        """
        import agent2.database as db
        from agent2.core import scheduler, sync

        out, problems = {}, []

        # The DB is the one hard dependency: everything else is a counter about
        # it. A failing round-trip is the only thing that makes this app "down".
        def _db_probe():
            row = db.qone("SELECT 1 AS ok")
            return {"reachable": bool(row and row.get("ok") == 1),
                    "schema_version": db.schema_version(),
                    "schema_expected": db.SCHEMA_VERSION}

        for name, fn in (("db", _db_probe),
                         ("pool", db.pool_stats),
                         ("wal", db.wal_stats),
                         ("scheduler", scheduler.stats),
                         ("sync", sync.stats),
                         ("mcp", mcp_health_report)):
            data, err = _section(fn)
            out[name] = data if err is None else {"error": err}
            if err is not None:
                problems.append(f"{name}: {err}")

        dbi = out.get("db") or {}
        if not dbi.get("reachable"):
            problems.append("database unreachable")
        elif dbi.get("schema_version") != dbi.get("schema_expected"):
            # A wrong schema resurfaces later as a baffling error in an unrelated
            # feature, which is exactly why migrations re-raise rather than
            # degrade. Surface it here too.
            problems.append(
                f"schema at v{dbi.get('schema_version')}, "
                f"expected v{dbi.get('schema_expected')}")

        sch = out.get("scheduler") or {}
        # ⚠️ NOT "enabled but workers == 0" — the pool starts its workers lazily on
        # the first submit(), so that is the normal state of an idle server and the
        # check fired on a perfectly healthy app. The real fault is workers that
        # started and then went away: capacity lost, so submitted turns would sit
        # in the queue forever.
        if sch.get("worker_starts") and not sch.get("workers"):
            problems.append("scheduler workers died — queued turns cannot run")
        if sch.get("queued") and sch.get("queued") >= sch.get("max_queue", 0):
            problems.append("turn queue full — new turns are being rejected")

        # Task 10: the section already decided which servers are faults and worded
        # each line; this only lifts them into the aggregate so `ok`/503 covers MCP
        # too. ⚠️ It reads `problems`, NOT `failing` — re-deriving the sentence here
        # would be the second place that describes a broken bridge.
        problems.extend((out.get("mcp") or {}).get("problems", []))

        out["ok"] = not problems
        out["problems"] = problems
        # 503 so a monitor can act on the status line alone, without parsing the
        # body. Still returns the full body, so whatever is wrong is visible.
        return jsonify(out), (200 if out["ok"] else 503)

    # ── Model capability registry + routing + fallback (Tasks 17, 18, 19) ──────
    # ⚠️ ONE ROUTE SET FOR ALL THREE, because they are one subject from a
    # surface's point of view: "what can these models do, which one will be used,
    # and what happened last time". Splitting them would put the routing mode on a
    # different endpoint from the capability list that justifies it, and a UI would
    # have to poll two things to render one panel.

    @app.route("/api/models", methods=["GET"])
    def api_models():
        """Every selectable model with its metadata, plus the routing policy.

        ⚠️ Carries no credential. A custom provider contributes its *host* and
        model id (already in `/api/platform`), never its key — `capabilities`
        reads `list_providers(safe=True)` for exactly that reason.
        """
        body = core_caps.describe()
        body["router"] = core_router.describe()
        body["attempts"] = core_router.attempts(limit=25)
        body["stats"] = core_router.stats()
        return jsonify(body)

    @app.route("/api/models/<path:model_key>", methods=["PUT"])
    def api_update_model_meta(model_key):
        """Correct what Agent2 believes about a model (Task 17).

        ⚠️ `<path:…>` and not `<model_key>`: a custom provider's key is
        `custom:ab12`, and Flask's default converter stops at `/` but a future
        provider id containing one would 404 silently. An absent field is left
        alone — see `capabilities.set_meta`.
        """
        data = request.json or {}
        try:
            return jsonify(core_caps.set_meta(model_key, **data))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503

    @app.route("/api/models/<path:model_key>", methods=["DELETE"])
    def api_clear_model_meta(model_key):
        """Forget an override, returning the model to the catalog's own answer."""
        return jsonify(core_caps.clear_meta(model_key))

    @app.route("/api/models/routing", methods=["PUT"])
    def api_set_routing():
        """Set the routing policy: `off` | `default_only` | `always`."""
        data = request.json or {}
        try:
            return jsonify({"routing": core_router.set_routing_mode(
                str(data.get("routing") or ""))})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503

    @app.route("/api/models/rank", methods=["POST"])
    def api_rank_models():
        """Ask a model to rank custom providers (Task 17).

        `{"all": true}` re-ranks every custom provider — the `/model rank all` path.
        Without it, only the ones nothing recognises are touched, which costs
        nothing on an install where every id is already known.

        ⚠️ SYNCHRONOUS AND BOUNDED. It costs one model call per provider, so `limit`
        is a real ceiling rather than paging, and `not_reached` reports what the
        ceiling cut — a truncated sweep that said nothing would read as "everything
        is ranked now" when it is not. It is a POST because it spends money and
        writes rows; a GET that did that would be fetched by a prefetching browser.

        ⚠️ `skipped` are the models the user corrected by hand. They are reported,
        not silently omitted: "re-rank everything" is a request to re-ask the
        models, not permission to discard the user's own answers.
        """
        data = request.json or {}
        if data.get("all"):
            changed, skipped, not_reached = core_caps.rank_all(
                limit=int(data.get("limit") or 32))
        else:
            changed = core_caps.rank_missing(limit=int(data.get("limit") or 8))
            skipped, not_reached = [], 0
        return jsonify({"ok": True, "ranked": changed, "skipped": skipped,
                        "not_reached": not_reached,
                        "models": [core_caps.get(k) for k in changed]})

    @app.route("/api/models/<path:model_key>/rank", methods=["POST"])
    def api_rank_one_model(model_key):
        """Rank ONE model. `force` re-asks even for an already-described model.

        ⚠️ `force` is the only way past "a user's own correction always wins", and
        only a human can ask for it — nothing in the app sets it.
        """
        data = request.json or {}
        return jsonify(core_caps.rank_with_model(
            model_key, force=bool(data.get("force"))))

    @app.route("/api/models/route", methods=["POST"])
    def api_preview_route():
        """What WOULD be chosen for this message. Never runs a turn.

        The debugging surface for Task 18: routing is invisible by design (it just
        picks a model), so without a dry run the only way to see why a decision was
        made is to spend a turn on it.
        """
        data = request.json or {}
        decision = core_router.choose(
            str(data.get("model") or ""),
            message=str(data.get("message") or ""),
            attachments=data.get("attachments") or [],
            context_tokens=int(data.get("context_tokens") or 0))
        return jsonify(decision.as_dict())

    # ── Platform info ──────────────────────────────────────────────────────────

    @app.route("/api/platform", methods=["GET"])
    def api_platform():
        return jsonify({
            "os":            OS_NAME,
            "shell":         SHELL_LABEL,
            "shell_bin":     SHELL_BIN,
            "models":        MODELS,
            "modes":         MODES,
            "providers":     providers.list_providers(safe=True),
            "default_model": DEFAULT_MODEL,
            "default_mode":  DEFAULT_MODE,
            "workspace":     core_workspace.current().as_dict(),
            # Task 15: the deployment's capability posture. Behind the guard, so
            # only an authenticated caller sees it; the *client's own* role also
            # comes back from `/api/auth/status`, which is the one a UI polls.
            "permissions":   core_perms.describe(),
        })

    # ── Workspace (sections 1, 12) ─────────────────────────────────────────────
    # The centralized WorkspaceManager is the single source of truth; both the
    # Web UI and CLI read/switch the active workspace through it.

    @app.route("/api/workspace", methods=["GET"])
    def api_get_workspace():
        return jsonify(core_workspace.current().as_dict())

    @app.route("/api/workspace", methods=["POST"])
    def api_set_workspace():
        d = request.json or {}
        path = (d.get("path") or "").strip()
        if not path:
            return jsonify({"error": "path is required"}), 400
        ws = core_workspace.set_workspace(path)
        return jsonify(ws.as_dict())

    # ── Background tasks (section 7) ───────────────────────────────────────────

    @app.route("/api/tasks", methods=["GET"])
    def api_list_tasks():
        return jsonify([
            {"id": t.id, "sid": t.sid, "chat_id": t.chat_id,
             "workspace_id": t.workspace_id, "status": t.status,
             "progress": t.progress, "started_at": t.started_at,
             "ended_at": t.ended_at}
            for t in core_sessions.active_tasks()
        ])

    # ── Command executions (Task 4) ────────────────────────────────────────────

    @app.route("/api/commands", methods=["GET"])
    def api_list_commands():
        """Every tracked command execution, live ones separated out.

        ⚠️ This is the answer to "Agent2 is stuck — on what?". `elapsed` alone
        cannot distinguish a slow build from a deadlocked one, so the field that
        matters here is `idle`: seconds since the process last produced output.
        Task 6 adds `stuck` (the live executions past the reporting threshold,
        each with its rendered `state` line) and `limits`, the ceilings in force.

        In-memory and per-process by design (see `core/commands.py`) — durable
        execution state is Phase 8. `?session=` / `?task=` narrow it.
        """
        from agent2.core import commands as core_commands
        try:
            limit = max(1, min(500, int(request.args.get("limit", 50))))
        except (TypeError, ValueError):
            limit = 50
        return jsonify(core_commands.snapshot(
            limit=limit,
            session_id=(request.args.get("session") or "").strip(),
            task_id=(request.args.get("task") or "").strip(),
        ))

