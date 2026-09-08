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
⚠️ THIS FILE NO LONGER BUILDS THE REPORT — `agent2/core/health.py` DOES.
The fourteen sections, the fault rules, the `warnings` list and the per-subsystem
`✓/⚠/✗/○` verdict all live there, because Task 28 asks for that verdict list on
the CLI as well and the CLI has no Flask app to call. Read that module's docstring
for every invariant that used to be stated here; this route is a `jsonify()` and a
status code, and `ok` decides which.

Do not re-derive a section, a fault or a verdict here. A second copy would agree
on the day it was written and drift silently afterwards, with the JSON and the
terminal each looking perfectly correct on its own.

⚠️ `/api/health` CARRIES COUNTERS ONLY, AND IT IS THE ONE ROUTE HERE THAT AN
UNAUTHENTICATED CALLER CAN REACH — from loopback, so Docker's healthcheck keeps
working (`auth.LOOPBACK_PATHS`). The payload carries no key material, no chat or
memory text, not even the DB path. Anything added to a section is readable by
anything that can reach 127.0.0.1 on this box, by definition.

AGENT METRICS (`GET /api/metrics`) — Task 27
───────────────────────────────────────────
A separate endpoint on purpose: health answers "is it working" and is what a
monitor pages on; metrics answer "how fast, how often, how big". `metrics.report()`
owns the arithmetic — see `agent2/core/metrics.py`.

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

(test_health.py — 58 tests, sabotage-verified · test_metrics.py — 59 tests
 · test_webauth.py)
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
from agent2.core.recovery import crash as _crash
from agent2.core import health as core_health
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

    @app.route("/api/chats/resume", methods=["GET"])
    def api_resume_target():
        """What a fresh tab would continue, and whether it will do so by itself.

        ⚠️ Registered ABOVE `/api/chats/<cid>` and the static segment is what makes
        it reachable — Werkzeug ranks a literal part over a converter, so
        `/api/chats/resume` cannot be swallowed by the id route. That is behaviour
        this endpoint *depends* on rather than merely benefits from, so it is
        asserted (`test_continuity.py`) instead of assumed, and the browser treats
        any non-payload answer as "nothing to continue" — losing the race would
        cost the feature, never the page.

        ⚠️ Identity and size only, never message text: `GET /api/chats/<cid>` is
        the one reader of a transcript, and this is a probe the first paint makes
        before anybody has chosen anything.

        `resume` and `auto` are two different facts — *which* conversation, and
        *whether* Agent2 opens it unasked (`AGENT2_RESUME`). A client that wants
        the second question answered can read `auto` even when there is nothing
        to continue.
        """
        return jsonify({
            "auto":   core_context.auto_resume(),
            "resume": core_context.resumable(),
        })

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
            f"SELECT * FROM messages WHERE chat_id=? {core_context.MSG_ORDER}", (cid,)
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
        """Both halves of recovery in one payload.

        `candidates` is Task 3's interrupted **task sessions** — work a human may
        choose to resume. `crash` is Phase 8's automatic recovery: what the startup
        scan found, what it decided, and what is waiting for review.

        ⚠️ **ONE ENDPOINT, TWO HALVES — NOT TWO ENDPOINTS.** They answer the same
        question ("is there unfinished work from a previous run") and a panel that had
        to poll two paths to render one section is how the browser ends up showing a
        session as resumable while the crash ledger has already marked its command
        `needs_review`. `candidates` keeps its exact old shape and position, because
        `public/script.js` reads it.
        """
        try:
            cwd = str(core_workspace.root())
        except Exception:
            cwd = ""
        out = {"candidates": [c.to_dict() for c in core_recovery.candidates(cwd)]}
        try:
            out["crash"] = _crash.report()
        except Exception as ex:   # a reporting failure may not cost the caller Task 3's half
            out["crash"] = {"error": f"{type(ex).__name__}: {ex}"}
        return jsonify(out)

    # ⚠️ THESE TWO STATIC PATHS SIT IN FRONT OF `/api/recovery/<sid>` ON PURPOSE, and
    # Werkzeug's rule sorting — static segments before converters — is what makes that
    # safe. A session id is 12 hex characters, so `scan`/`units` can never *be* one;
    # the risk was only ever the routing, and `test_crashrecovery` pins it by asking
    # for `/api/recovery/scan` and asserting it did not land in the plan handler.

    @app.route("/api/recovery/scan", methods=["POST"])
    def api_recovery_scan():
        """Run a recovery scan now. `{"project": "*"}` widens it past this project.

        POST because it writes: it settles ledger rows and moves recovery records.
        Bounded by the same `limit`/`budget` ceilings the startup scan uses, so a
        caller cannot turn this into an unbounded table walk.
        """
        body = request.json or {}
        kw: dict = {}
        if body.get("project"):
            kw["project"] = str(body["project"])
        for name, cast in (("stale_after", float), ("limit", int), ("budget", float)):
            if body.get(name) is not None:
                try:
                    kw[name] = cast(body[name])
                except (TypeError, ValueError):
                    return jsonify({"error": f"{name} must be a number"}), 400
        return jsonify(_crash.scan(**kw))

    @app.route("/api/recovery/units", methods=["GET"])
    def api_recovery_units():
        """Recovery records — `?state=needs_review` for just the review queue.

        ⚠️ Never carries a command line, an argument dict or file content: the payload
        is `crash._payload()`'s, which joins none of the unit's own text in. See its
        docstring — `run_command` argv routinely holds a bearer token.
        """
        state = str(request.args.get("state") or "").strip()
        project = request.args.get("project") or None
        try:
            limit = int(request.args.get("limit") or 50)
        except ValueError:
            return jsonify({"error": "limit must be a number"}), 400
        return jsonify({"units": _crash.rows(project=project, status=state,
                                             limit=limit),
                        "counters": _crash.counters()})

    @app.route("/api/recovery/units/<kind>/<ref_id>", methods=["POST"])
    def api_recovery_unit_act(kind, ref_id):
        """`{"action": "acknowledge"|"retry"|"terminate"}` on one recovery record.

        ⚠️ **`retry` AND `terminate` STILL ASK `core.permissions`** — inside
        `crash.retry()` / `crash.terminate()`, live, not here. An operator overrules
        the *verdict* recovery could not establish, never the capability gate; the
        alternative is a button that launders `AGENT2_DENY_CAPS` away.
        """
        action = str((request.json or {}).get("action") or "").strip().lower()
        note = str((request.json or {}).get("note") or "")
        if action == "acknowledge":
            res = _crash.acknowledge(kind, ref_id, note)
        elif action == "retry":
            res = _crash.retry(kind, ref_id)
        elif action == "terminate":
            res = _crash.terminate(kind, ref_id)
        else:
            return jsonify({"error": "action must be 'acknowledge', 'retry' or "
                                     "'terminate'"}), 400
        return jsonify(res), (200 if res.get("ok") else 400)

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

    @app.route("/api/health", methods=["GET"])
    def api_health():
        """Aggregate health snapshot — Task 28's fourteen subsystems in one payload.

        `agent` · `db` · `pool` · `wal` · `scheduler` · `tasks` · `commands` ·
        `sync` · `recovery` · `mcp` · `memory` · `context` · `providers` ·
        `permissions`, plus `sections` (the per-subsystem `✓/⚠/✗/○` verdict list),
        `ok`, `problems` and `warnings`.

        ⚠️ THE REPORT ITSELF LIVES IN `core/health.py`, AND THIS IS THE WHOLE
        ROUTE. Task 28 asks for the verdict list on the CLI too, and the CLI has no
        Flask app to call — so the sections, the fault rules and the ✓/⚠ mapping
        moved to `core/` (where `integrations.registry.health()` already lives, for
        the same reason). Leaving them here would have meant a second `if` ladder in
        `agent2cli.py` that agreed with this endpoint on the day it was written and
        drifted silently afterwards, each half looking correct alone.

        ⚠️ Task 28's instruction was *"reuse the existing endpoint rather than
        creating a duplicate"*, and that is why every subsystem lands here rather
        than behind a second `/api/status`: two aggregate endpoints means one of
        them is the stale one, and nothing in the payload says which.

        ⚠️ `ok` AND THE STATUS CODE ARE DECIDED BY `problems` ALONE — see
        `core.health.report()`, which states why `warnings` may never influence
        either. 503 so a monitor can act on the status line without parsing the
        body; the full body is still returned, so whatever is wrong is visible.
        """
        body = core_health.report()
        return jsonify(body), (200 if body["ok"] else 503)

    # ── Agent metrics (Task 27) ────────────────────────────────────────────────

    @app.route("/api/metrics", methods=["GET"])
    def api_metrics():
        """Everything Task 27 measures: the thirteen signals, in one payload.

        ⚠️ THIS IS NOT A SECOND `/api/health`. Health answers "is it working" and
        is what a monitor pages on; this answers "how fast, how often, how big" and
        is what a human reads while tuning. Folding the series into the health
        payload would have made the endpoint an unauthenticated performance dump
        and — worse — would have put latency numbers next to a 503, inviting a
        monitor to alert on a percentile.

        ⚠️ IT REPORTS, IT DOES NOT MEASURE. `metrics.report()` owns the arithmetic,
        and three of the thirteen signals are *borrowed* from the readers that
        already own them (`router.stats()` for LLM latency and errors,
        `permissions.counters()` for denials) rather than re-observed here — two
        windows over one fact disagree numerically and both look right.

        No content ever enters this payload: a label is a tool name, a model key, a
        status word or a source name, never an argument, a command line or a prompt.
        """
        from agent2.core import metrics
        return jsonify(metrics.report())

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

    # ── The project doc — `/init` for the browser (Tasks 29–31) ────────────────
    # ⚠️ TWO ROUTES, ONE ENGINE. `core.projectscan` decides what is true about this
    # project and `core.projectdoc` writes it; both surfaces call exactly those two,
    # so the terminal's `/init` and the browser's button cannot describe the same
    # tree differently. A "web version" that re-derived any of it would be a second
    # declaration of what a project IS — and its drift would be *committed to a
    # file* that every later turn then reads as fact.

    @app.route("/api/project", methods=["GET"])
    def api_get_project():
        """What `/init` would find, and what it has already written.

        Read-only: it scans, it never creates `.agent2/`. `doc` reports the file's
        own state (`exists`, `bytes`, `sections`, `generated`, `hint`) so a panel
        can say "3 sections are yours" before offering to refresh anything.

        ⚠️ It reports section HEADINGS and who owns them, never their text. The
        user's own `## Agent2 Instructions` is theirs, and an endpoint that returns
        it turns a project doc into one more place a stray reader finds prose the
        author expected to live only in their checkout.
        """
        from agent2.core import broker as _broker
        from agent2.core import projectdoc, projectscan
        rep = projectscan.scan()
        root = rep.get("root") or ""
        doc = {"exists": False, "path": _broker.PRIMARY_DOC, "bytes": 0,
               "sections": [], "generated": [], "preserved": [], "hint": ""}
        if root:
            existing = projectdoc.read_existing(root)
            if existing:
                _pre, blocks = projectdoc.parse(existing)
                doc.update({
                    "exists": True,
                    "bytes": len(existing.encode("utf-8", "replace")),
                    "sections": [b.heading for b in blocks],
                    "generated": [b.heading for b in blocks if b.generated],
                    "preserved": [b.heading for b in blocks if not b.generated],
                    "hint": projectdoc.prior_hint(existing),
                })
        return jsonify({"scan": rep, "doc": doc})

    @app.route("/api/project/init", methods=["POST"])
    def api_project_init():
        """Run `/init`: scan the workspace, then create or update `.agent2/agent2.md`.

        Body: `hint` (the user's own one-line description — the thing a scan can
        never learn), `describe` (default true; false skips the model call), and
        `write` (default true; false is a DRY RUN that performs the whole merge and
        reports what *would* change, touching nothing).

        ⚠️ POST because it writes, and the write is `projectdoc.apply()`'s alone —
        it re-asks `core.permissions` for `fs.write`, refuses a `.agent2` that would
        be the per-machine key folder, and preserves every section a human owns.
        A refusal is a `reason` in a 200 body with `ok: false`, not a 500: the
        caller asked a legitimate question and the answer is "not here".
        """
        from agent2.core import projectdoc, projectscan
        d = request.json or {}
        rep = projectscan.scan()
        res = projectdoc.apply(
            rep,
            hint=str(d.get("hint") or "").strip(),
            describe=bool(d.get("describe", True)),
            write=bool(d.get("write", True)),
        )
        return jsonify({"scan": rep, "result": res})

    # ── Skills (Phase 11, Tasks 32–36) ─────────────────────────────────────────
    # ⚠️ THE BROWSER GETS THE SAME THREE READS AND THE SAME ONE WRITE THE TERMINAL
    # GETS, out of the same functions. `/skills` in the CLI and this panel are two
    # renderers over `Catalog.to_payload()`, `Selection.to_payload()` and
    # `skills.describe()`; the write is `state.set_many()` / `skills.toggle()`. A
    # "web version" of any of it would be a second answer to *which skills does this
    # project have, and which ones fired* — and the browser's would be the one
    # nobody was watching while the terminal looked right.
    #
    # ⚠️ NO ROUTE HERE TOUCHES A SKILL FILE, ever — Task 33's constraint. Enablement
    # is a `skill_state` row keyed by project, so the same folder shared between two
    # checkouts carries no choices with it.

    @app.route("/api/skills", methods=["GET"])
    def api_get_skills():
        """This project's skills, the user's choices, and the last selection.

        `?force=1` bypasses the discovery TTL (what `/skills reload` does).
        ⚠️ Metadata only — `Skill.to_payload()` defaults `body=False` and this never
        overrides it, for the reason `GET /api/project` refuses section bodies: a
        skill is prose a human wrote in their own checkout.
        """
        from agent2.core import skills as _skills
        force = str(request.args.get("force") or "").strip().lower() in ("1", "true", "yes")
        cat = _skills.available(force=force)
        return jsonify({
            "catalog": cat.to_payload(),
            "states": _skills.state.states(),
            "last": _skills.last_applied(),
            "policy": _skills.describe(),
            "stats": _skills.stats(),
        })

    @app.route("/api/skills/<sid>", methods=["PUT"])
    def api_set_skill(sid):
        """Turn one skill on / off / back to automatic.

        Body: `{"enabled": true | false | null}`. ⚠️ `null` IS A REAL VALUE and is
        not the same as `false` — it clears the row, restoring *automatic* selection
        (the skill still applies when the request names it or its keywords match).
        A body with no `enabled` key is a `400`, because "absent" and "null" would
        otherwise be the same request with two different meanings.
        """
        from agent2.core import skills as _skills
        d = request.json or {}
        if "enabled" not in d:
            return jsonify({"error": "enabled is required (true, false or null)"}), 400
        val = d.get("enabled")
        if val is not None and not isinstance(val, bool):
            return jsonify({"error": "enabled must be true, false or null"}), 400
        ok, msg = _skills.toggle(str(sid), val)
        return jsonify({"ok": ok, "message": msg}), (200 if ok else 404)

    @app.route("/api/skills", methods=["PUT"])
    def api_set_skills():
        """Bulk set, for the panel's Apply button. Body: `{"states": {id: bool|null}}`.

        ⚠️ ONE WRITE, not N — `state.set_many()` exists for this, and the reason is
        in its docstring: N round trips is N `sync.notify()`s and, in dual mode, N
        chances for the other process to read a half-applied selection. The CLI's
        `/skills` menu goes through the same function.
        """
        from agent2.core import skills as _skills
        d = request.json or {}
        raw = d.get("states")
        if not isinstance(raw, dict):
            return jsonify({"error": "states must be an object of {skill_id: true|false|null}"}), 400
        changes: dict[str, bool | None] = {}
        for key, val in raw.items():
            if val is not None and not isinstance(val, bool):
                return jsonify({"error": f"states[{key}] must be true, false or null"}), 400
            changes[str(key)] = val
        written = _skills.state.set_many(changes)
        return jsonify({"ok": True, "written": written,
                        "states": _skills.state.states()})

    # ── Workflows (Phase 12, Tasks 37–39) ──────────────────────────────────────
    # ⚠️ THE BROWSER GETS THE SAME FOUR VERBS THE TERMINAL GETS — new · edit ·
    # run · delete — out of the same two modules. `/workflow` in the CLI and these
    # routes are two renderers over `Catalog.to_payload()`, `WorkflowFile.to_payload()`
    # and `RunState.to_payload()`; the writes are `authoring.create/delete()` and
    # `runner.instantiate()`. A "web version" of any of it would be a second answer
    # to *what workflows does this project have and which one is running*.
    #
    # ⚠️ NO ROUTE HERE VALIDATES A NAME, BUILDS A PATH OR PRE-CHECKS A CAPABILITY.
    # `authoring` asks `fs.write`/`fs.delete` live and `runner.instantiate()` asks
    # `chat` live, and each refuses by *returning* a reason — so a refusal is
    # `{"ok": false, "reason": …}` inside a 200 rather than a 500, and the gate has
    # exactly one home. A pre-check here is a second gate that drifts permissive.
    #
    # ⚠️ `edit` is deliberately NOT a route. The CLI's `[E]dit` launches
    # `$VISUAL`/`$EDITOR` on the machine running the terminal; a browser tab may be
    # on another machine entirely, so the web half edits by POSTing `body` — which
    # goes through the same `loader.build()` validation the file would get on the
    # next read. Spawning a server-side editor for a remote click is not the same
    # feature under a shared name.

    @app.route("/api/workflows", methods=["GET"])
    def api_get_workflows():
        """This project's workflow files, the live run, and the subsystem's posture.

        `?force=1` bypasses the discovery TTL (what `/workflow reload` does).
        ⚠️ The catalog lists every file INCLUDING the ones that will not run — a
        workflow that is absent from the list because it is broken is indistinguishable
        from one nobody wrote, and the second is not fixable. `problems` says why.

        `?verify=1` adds `verification` — `core.verify`'s five-verdict report on the run
        this payload already describes, which is the browser's half of the terminal's
        *Execute → Verify → Complete*. ⚠️ **ASKED FOR, NEVER VOLUNTEERED, AND NOT A
        SECOND ROUTE.** `runner.verify()` writes an audit line every time it runs and
        reads two ledgers, and this endpoint is what a panel polls — so a poll may not
        pay for it, and a verdict a nobody asked for may not fill the audit file. It is
        `?force=1`'s shape for `?force=1`'s reason: one route answers one question, and
        the caller says how much of the answer it wants.
        """
        from agent2.core import workflow as _wf
        force = str(request.args.get("force") or "").strip().lower() in ("1", "true", "yes")
        want = str(request.args.get("verify") or "").strip().lower() in ("1", "true", "yes")
        cat = _wf.discover(force=force)
        live = _wf.runner.live(chat_id=str(request.args.get("chat_id") or "").strip())
        recent = _wf.runner.runs(limit=10)
        out = {
            "catalog": cat.to_payload(),
            "live": live.to_payload() if live is not None else {},
            "runs": recent,
            "policy": _wf.describe(),
        }
        if want:
            # The live run when there is one, else the newest recorded — the same run
            # `live`/`runs[0]` already names, so a reader never has to guess which one
            # a verdict belongs to. `ref` on the report says it outright.
            rid = (live.run_id if live is not None
                   else str((recent[0] or {}).get("id") or "") if recent else "")
            out["verification"] = (_wf.verify(rid, state=live).to_payload()
                                   if rid else {})
        return jsonify(out)

    @app.route("/api/workflows/<name>", methods=["GET"])
    def api_get_workflow(name):
        """One workflow as Agent2 understood it — nodes, order, problems.

        404 names how many workflows ARE known, because "not found" and "you have
        none" send a caller to two different places.
        """
        from agent2.core import workflow as _wf
        wf = _wf.load(str(name))
        if wf is None:
            cat = _wf.discover()
            return jsonify({"error": f"no single workflow matches {name!r}",
                            "known": [f.name for f in cat.files]}), 404
        return jsonify(wf.to_payload(nodes=True))

    @app.route("/api/workflows", methods=["POST"])
    def api_create_workflow():
        """Write a new workflow file. Body: `name`, `description`, `fmt`, `body`.

        ⚠️ It never overwrites — `existed: true` comes back and the caller edits
        instead, because the file is the only copy of a plan somebody wrote.
        ⚠️ `ok` and `runnable` are TWO facts: a supplied `body` can be saved
        successfully and still not form a graph the runner would accept, and
        reporting only the write would let a panel say "created" about a file that
        cannot run. `problems` is the difference.
        """
        from agent2.core.workflow import authoring
        d = request.json or {}
        name = str(d.get("name") or "").strip()
        if not name:
            return jsonify({"error": "name is required"}), 400
        res = authoring.create(
            name,
            description=str(d.get("description") or "").strip(),
            fmt=str(d.get("fmt") or "yaml").strip().lower() or "yaml",
            body=str(d.get("body") or ""),
        )
        return jsonify(res.to_payload())

    @app.route("/api/workflows/auto", methods=["POST"])
    def api_auto_workflow():
        """Plan a graph from a goal — Task D4.31, the browser's `/workflow auto`.

        Body: `goal` (required), `mode` (`plan` default · `auto`), `chat_id`, `model`,
        `mode_key`.

        ⚠️ **`plan` IS THE DEFAULT AND `auto` MUST BE ASKED FOR BY NAME.** D4.31's bar
        is *"explicit activation; never automatic for a normal prompt"*, and a default
        of `auto` would make a panel that forgot the field create task rows. The two
        modes are `dynamic.MODES`, and an unrecognised word is refused by `dynamic`
        with `X_BAD_MODE` rather than coerced — `AGENT2_WEB_ROLE`'s direction: a typo
        falls to the side that changes nothing.
        ⚠️ **PLAN WRITES NOTHING AT ALL** — no `exec_workflows` row, no `agent_tasks`
        row — so this is the one workflow POST that is safe to call to *look*.
        ⚠️ **A refusal is `{"ok": false, "reason": …}` INSIDE A 200**, this section's
        rule: `dynamic` declines by returning a `Draft`, and the panel branches on
        `r.ok`. It pre-checks nothing — `runner.instantiate()` asks `chat` live on the
        `auto` path, and this route is covered by the one `("/api/workflows", "",
        CAP_CHAT)` permission entry by prefix.
        ⚠️ It is reachable only because Werkzeug ranks a literal segment above a
        converter; there is no `POST /api/workflows/<name>`, so nothing is shadowed.
        """
        from agent2.core.workflow import dynamic
        d = request.json or {}
        goal = str(d.get("goal") or "").strip()
        if not goal:
            return jsonify({"error": "goal is required"}), 400
        draft = dynamic.start(
            goal,
            mode=str(d.get("mode") or dynamic.MODE_PLAN).strip().lower(),
            chat_id=str(d.get("chat_id") or "").strip(),
            model=str(d.get("model") or "").strip(),
            mode_key=str(d.get("mode_key") or "").strip(),
        )
        return jsonify(draft.to_payload())

    @app.route("/api/workflows/<name>", methods=["DELETE"])
    def api_delete_workflow(name):
        """Remove one workflow file. `fs.delete`, asked live inside `authoring`.

        The confirmation belongs to the surface — the CLI asks twice — so this
        performs what it is told and reports it.
        """
        from agent2.core.workflow import authoring
        res = authoring.delete(str(name))
        return jsonify(res.to_payload()), (200 if res.ok else 400)

    @app.route("/api/workflows/<name>/run", methods=["POST"])
    def api_run_workflow(name):
        """Start a workflow: `runner.instantiate()`, which asks `chat` live.

        Body: `chat_id`, `model`, `mode`. ⚠️ It refuses while a run is still live in
        this project, for the reason `/workflow run` does: two live runs make
        `for_turn()`'s "the current node" ambiguous, and the turn would be told to
        work on one of them with no way to say which.
        """
        from agent2.core import workflow as _wf
        d = request.json or {}
        wf = _wf.load(str(name))
        if wf is None:
            return jsonify({"ok": False, "reason": f"no single workflow matches {name!r}"}), 404
        if wf.defn is None or not wf.ok:
            return jsonify({"ok": False, "reason": f"{wf.name} will not run as written",
                            "workflow": wf.to_payload()}), 400
        chat_id = str(d.get("chat_id") or "").strip()
        busy = _wf.runner.live(chat_id=chat_id)
        if busy is not None and not busy.finished:
            return jsonify({"ok": False,
                            "reason": f"{busy.name} is already running "
                                      f"({busy.done}/{busy.total} settled)",
                            "live": busy.to_payload()}), 409
        run = _wf.instantiate(wf.defn, chat_id=chat_id,
                              model=str(d.get("model") or "").strip(),
                              mode=str(d.get("mode") or "").strip())
        if not run.ok:
            return jsonify({"ok": False, "reason": run.reason,
                            "validation": run.validation.to_payload()
                            if run.validation is not None else {}}), 400
        return jsonify({"ok": True, "run": _wf.state_for(run.run_id).to_payload()})

    # ── UltraCode (Phase D5) ───────────────────────────────────────────────────
    # ⚠️ **THE SECOND OF EXACTLY TWO DOORS.** `config.py`'s UltraCode block says
    # `/ultracode` and `POST /api/ultracode` are the only ways in, *asserted
    # structurally* — so no other route may grow an UltraCode verb, and nothing on
    # the turn path may import `core.ultracode` at all. A loop that writes code is
    # entered on purpose, by a human, on one of two surfaces or on neither.
    @app.route("/api/ultracode", methods=["GET"])
    def api_ultracode_state():
        """The read half — `/ultracode state` and `/ultracode policy` in one payload.

        `?run_id=` reports that run; omitted, it reports whichever UltraCode run is
        live here. One call rather than two, because `engine.state()` already carries
        `policy` (`describe()`): the terminal prints them as two commands because a
        human reads two paragraphs, and that is presentation.

        ⚠️ **`ok` IS ONLY THE MASTER SWITCH** — it stays `true` with `run: null`, so
        "nothing is live" is `run` and nowhere else. `_uc_state()` carries the same
        warning for the same reason: conflating them prints *UltraCode is off* at a
        project that merely has nothing running.
        ⚠️ **`mine` IS THE ENGINE'S ANSWER** to *is this run UltraCode's* — it
        filters on `plan.DEF_SOURCE`, while `start()`'s liveness check does not (two
        questions, so two readers). A panel that re-derived it from `run.source`
        would be the second declaration of what an UltraCode run **is**.
        """
        from agent2.core import ultracode as _uc
        return jsonify(_uc.state(str(request.args.get("run_id") or "").strip()))

    @app.route("/api/ultracode", methods=["POST"])
    def api_ultracode():
        """Drive the adaptive loop: `action` is `start` · `approve` · `cancel`.

        `start` takes `goal` (required) plus `chat_id`, `model`, `mode_key`;
        `approve` and `cancel` take `run_id` (omitted ⇒ the live run), and `cancel`
        also takes a free-text `reason` for the record.

        ⚠️ **`run` IS DELIBERATELY NOT A VERB HERE, AND `/ultracode run` IS WHERE IT
        WENT** (rule 28 — name it, never silently omit it). Three reasons, and the
        third is why nothing is lost: `engine.drive()` needs a synchronous worker
        that owns a whole model turn, and the CLI's is `agent_turn` under
        `limits(max_workers=0)` because ONE mutable `history` list cannot be appended
        to by four threads — on this surface a turn is a Socket.IO stream owned by
        `core/scheduler.py` and addressed to a `sid`, which a request thread has not
        got. Driving the loop inside one request would then hold that request open
        for an entire autonomous run — `ULTRACODE_BUDGET_SEC` is **off** by default —
        with no stream, no heartbeat and no Stop. And it is unnecessary:
        `workflow.for_turn()` does **not** filter on source, so an UltraCode run's
        current node reaches the next browser turn's prompt exactly as a workflow
        node does after `POST /api/workflows/<name>/run`. This is `edit`'s posture in
        the section above, for a sharper reason.
        ⚠️ **`work`, `replan` AND `finalize` ARE NOT VERBS ON EITHER SURFACE** —
        `engine.drive()` owns all three and owns the ORDER they run in, which is the
        whole of D5's loop.
        ⚠️ **NEITHER DOOR PASSES `approval`, `force` OR `name`.** They are
        `start()`'s parameters and the terminal declines them too: approval is the
        operator's posture (`AGENT2_ULTRACODE_APPROVAL`), and a gate a JSON field
        could switch off is not a gate.
        ⚠️ **A refusal is `{"ok": false, "reason": …}` INSIDE A 200**, this section's
        rule — every engine entry point declines by *returning* one of `REFUSALS`,
        and it pre-checks nothing (`start()` asks the planner, the graph validator
        and the live-run test itself). The `400`s are the two things the engine was
        never asked: an action it has no verb for, and a `start` with no goal —
        `api_auto_workflow`'s split, where an empty `goal` carries `{"error": …}` and
        no `reason` at all because there is nothing to refuse yet.
        ⚠️ **A SUCCESSFUL `cancel` HAS `ok: false`** — `Finish.ok` answers *did this
        run do its job*, and a cancelled one did not. The cancel took effect when
        `reason` is `cancelled` (`schedule.R_CANCELLED`), which is the one case where
        that field is a verdict rather than a refusal.
        """
        from agent2.core import ultracode as _uc
        d = request.json or {}
        action = str(d.get("action") or "").strip().lower()

        if action == "start":
            goal = str(d.get("goal") or "").strip()
            if not goal:
                return jsonify({"error": "goal is required"}), 400
            return jsonify(_uc.start(
                goal,
                chat_id=str(d.get("chat_id") or "").strip(),
                model=str(d.get("model") or "").strip(),
                mode_key=str(d.get("mode_key") or "").strip(),
            ).to_payload())

        if action in ("approve", "cancel"):
            # ⚠️ WHICH RUN A VERB ACTS ON IS ASKED OF `state()`, the owner — the web
            # half of `_uc_run_id()`. Both surfaces resolve an omitted id to the live
            # run and both refuse one that is not OURS, because `dag.store.live()`
            # is unfiltered by source and `cancel()` settles any graph run by id: a
            # door that let a `/workflow auto` run be cancelled as UltraCode would be
            # wider than the terminal's, which is the worse direction to drift. The
            # facts (`ok`, `run`, `mine`) are the engine's; only the prose is ours.
            snap = _uc.state(str(d.get("run_id") or "").strip())
            run = snap.get("run") or {}
            if not snap.get("ok"):
                return jsonify({"ok": False, "reason": snap.get("reason") or "ultracode is off"})
            if not run:
                return jsonify({"ok": False, "reason": "no UltraCode run is live in this project"})
            if not snap.get("mine"):
                return jsonify({"ok": False,
                                "reason": f"that run came from "
                                          f"{run.get('source') or 'another surface'!r}, "
                                          f"not UltraCode",
                                "run": run})
            rid = str(run.get("run_id") or "")
            if action == "approve":
                return jsonify(_uc.approve(rid).to_payload())
            return jsonify(_uc.cancel(
                rid, reason=str(d.get("reason") or "cancelled from the web UI"),
            ).to_payload())

        return jsonify({
            "error": f"unknown action {action!r}" if action else "action is required",
            "actions": ["start", "approve", "cancel"],
            "read": "GET /api/ultracode reports the run, the stage and the policy",
            "run": "terminal only — `/ultracode run` works the nodes; on this "
                   "surface the next chat turn does, through workflow.for_turn()",
        }), 400

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

