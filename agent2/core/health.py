# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/core/health.py
─────────────────────
THE health report. One assembly, one verdict per subsystem, read by
`GET /api/health` **and** by the CLI's `/health`.

⚠️ THIS MODULE EXISTS BECAUSE TASK 28 MADE THE REPORT A TWO-SURFACE FACT.
The fourteen sections and the 200/503 split were born inside
`register_routes()` as nested closures, which was correct while only Flask could
ask for them. Task 28 asks for a **rendered verdict list**

    ✓ Database   ✓ Scheduler   ✓ Command Executor   ⚠ Gemini Provider

and the CLI is a surface with no Flask app to call — so the answer either moves
here once, or a second, hand-rolled `if` ladder appears in `agent2cli.py` and
starts disagreeing with the endpoint about what "healthy" means. That is the one
failure the one-declaration rule exists to prevent, and it is worse here than
almost anywhere else: both halves keep working, each looks right on its own, and
the disagreement is only visible to somebody who reads the JSON and the terminal
side by side. `agent2/server/routes.py` is now a `jsonify()` and a status code.

⚠️ NOT ONE SECTION COMPUTES A NUMBER OF ITS OWN.
Every builder is a *projection* of the reader that already owns the fact —
`tasks.stats()`, `commands.snapshot()`, `broker.stats()`, `router.stats()`,
`permissions.describe()`, `memory.count_memories()`, `registry.health_report()`,
`crash.stats()`. A health report that re-derived "is this stuck", "how many are
queued" or "which context survived" would be a second opinion on a question that
already has an owner.

⚠️ AND EVERY SECTION IS A PROJECTION, NOT A PASS-THROUGH.
`commands.snapshot()` carries command lines and `rotator.status()` carries a key
preview; both are legitimate on their own authenticated routes and neither may
appear here. `/api/health` is reachable from loopback **without a credential**
(`auth.LOOPBACK_PATHS`, so Docker's healthcheck keeps working), so anything that
enters this payload is readable by anything that can reach 127.0.0.1 on this box.
Counters, limits, model names, state words. No key material, no chat or memory
text, no command lines, no file paths, not even the DB path.

⚠️ EVERY SECTION IS ISOLATED BEHIND `_section()`.
A health report that raises because one counter broke reports the whole app down
when the only broken thing is the reporting. Each section degrades to its own
error string and the rest of the report still renders.

⚠️ `ok` AND THE 503 ARE DECIDED BY `problems` ALONE.
`warnings` may never influence either, and no line may be promoted between the
two for emphasis. The whole value of the 503 is that it means "a restart or an
operator can fix this"; every supported configuration that trips it spends that
meaning, and an alert that fires on a supported configuration is an alert people
learn to ignore — and then the real one is ignored too.

⚠️ A DISABLED OPTIMIZATION IS NOT UNHEALTHY, AND `off` IS WHY.
The WAL checkpointer and the turn scheduler are optimizations layered over
working defaults and both can be switched off on purpose
(`AGENT2_WAL_CHECKPOINT_SEC=0`, `AGENT2_MAX_CONCURRENT_TURNS=0`); both MCP
bridges ship auto-connect OFF, so on a fresh install every server is
disconnected. The verdict vocabulary therefore has four words, not two —
`ok` · `warn` · `fail` · `off` — the same four `integrations.registry.health()`
uses, so a row from either reads identically. `off` is not a lesser `warn`: it
is "this is switched off, which is a decision, not a fault".

⚠️ A ROW MAY NEVER SAY `fail` WHILE THE REPORT SAYS `ok`.
`_rows()` derives its states from the very `problems` / `warnings` lines the
aggregate was built from, rather than re-testing the data — so the list a human
reads and the status code a monitor reads cannot drift. `test_health.py` pins
that equivalence in both directions.

Owner of: the section table, the fault/warning rules, the per-subsystem verdict.
Owner of nothing else — every number here belongs to somebody named above.
"""

from __future__ import annotations

# ── The verdict vocabulary ────────────────────────────────────────────────────
# Deliberately the same four words `integrations.registry.health()` reports, so
# an MCP row and a Database row can sit in one list and mean the same thing by
# the same names. Anything that renders these must handle all four: a renderer
# that knows only ✓/✗ prints a cross at a user who disabled something on purpose.
OK = "ok"
WARN = "warn"
FAIL = "fail"
OFF = "off"

# How far back the command-executor projection counts. A health read may not turn
# into an unbounded walk of the execution registry, so the window is reported
# next to the counts it produced — "12 of the last 200" is a fact, "12" alone is
# not.
CMD_WINDOW = 200


def _section(fn):
    """Collect one section without letting it fail the whole report.

    ⚠️ A health endpoint that 500s because one counter raised is worse than no
    health endpoint: the monitor now reports the app down when the only broken
    thing is the reporting. Each section degrades to its own error string.
    """
    try:
        return fn(), None
    except Exception as ex:                                       # noqa: BLE001
        return None, f"{type(ex).__name__}: {ex}"


# ── The sections ─────────────────────────────────────────────────────────────
# ⚠️ EVERY IMPORT BELOW IS LOCAL TO ITS BUILDER, AND THAT IS DELIBERATE TWICE
# OVER. It keeps this module importable from anywhere (the CLI imports it at
# startup; a module-level `agent2.llm.providers` would drag the provider stack
# into that path), and it puts every import *inside* a `_section()` guard — so a
# build missing an optional dependency reports one section's error instead of
# failing the whole report at import time. It also means every call resolves the
# attribute live, which is what lets a test patch `db.qone` or `scheduler.stats`
# and be seen from here.

def agent_section() -> dict:
    """Can a turn run, and under what ceilings.

    ⚠️ Deliberately does NOT import `agent2.agent` to count tools. That module
    pulls in the Gemini SDK, so on a build without it the import fails, the
    section reports an error and the endpoint 503s — announcing a fault in a
    supported configuration, which is the one thing this report may not do.
    """
    from agent2 import config as _cfg
    from agent2.core.session import sessions
    return {
        "default_model":    _cfg.DEFAULT_MODEL,
        "default_mode":     _cfg.DEFAULT_MODE,
        "models":           len(_cfg.MODELS),
        "modes":            len(_cfg.MODES),
        "max_iters":        _cfg.MAX_AGENT_ITERS,
        "max_ctx_messages": _cfg.MAX_CTX_MESSAGES,
        "max_tool_output":  _cfg.MAX_TOOL_OUTPUT,
        "active_turns":     len(sessions.active_tasks()),
    }


def db_section() -> dict:
    """The one hard dependency: does a round trip work, and is the schema right?

    Everything else in this report is a counter *about* the database, so a
    failing round trip is the only thing that makes this app "down".
    """
    import agent2.database as db
    row = db.qone("SELECT 1 AS ok")
    return {"reachable": bool(row and row.get("ok") == 1),
            "schema_version": db.schema_version(),
            "schema_expected": db.SCHEMA_VERSION}


def pool_section() -> dict:
    """Connection-pool counters — `database.pool_stats()` verbatim."""
    import agent2.database as db
    return db.pool_stats()


def wal_section() -> dict:
    """WAL checkpointer counters — `database.wal_stats()` verbatim."""
    import agent2.database as db
    return db.wal_stats()


def scheduler_section() -> dict:
    """Turn scheduler — `scheduler.stats()` verbatim."""
    from agent2.core import scheduler
    return scheduler.stats()


def tasks_section() -> dict:
    """The task queue — `core.tasks.stats()` verbatim, counters only."""
    from agent2.core import tasks as core_tasks
    return core_tasks.stats()


def commands_section() -> dict:
    """Command-executor counters: `counts` and `limits`, never a command line.

    ⚠️ `snapshot()` is THE declaration of what "stuck" means (it computes it from
    `watch()` on read, because a command becomes stuck by the passage of time and
    a stored flag would be stale exactly when it matters). This asks it and throws
    the transcript away.
    """
    from agent2.core import commands as core_commands
    snap = core_commands.snapshot(limit=CMD_WINDOW)
    return {
        "counts": dict(snap.get("counts") or {}),
        "limits": dict(snap.get("limits") or {}),
        "window": CMD_WINDOW,
    }


def sync_section() -> dict:
    """Cross-process change propagation — `sync.stats()` verbatim.

    The resource names are label enums (one is literally `api_keys`) and never
    values, which is what makes them safe on an unauthenticated route.
    """
    from agent2.core import sync
    return sync.stats()


def recovery_section() -> dict:
    """Interrupted-work recovery — `crash.stats()` verbatim.

    ⚠️ It decides what counts as a problem, and it deliberately refuses to call a
    NEEDS_REVIEW record one — that is recovery *working*, and it may sit there for
    weeks. See `_faults()`.
    """
    from agent2.core.recovery import crash
    return crash.stats()


def mcp_section() -> dict:
    """The MCP servers (Task 10).

    ⚠️ IT ONLY FETCHES. Every judgement — which states count as a fault, what
    each one is called — belongs to `registry.health_report()`, because the CLI,
    `/mcp health` and the settings panel render the same verdict and a second copy
    here would be the fourth. An import failure is not a fault either: a build
    without the `mcp` package has no servers to be unhealthy about.
    """
    try:
        from agent2.integrations import registry as mcp_registry
    except Exception:                                             # noqa: BLE001
        return {"servers": [], "total": 0, "connected": 0,
                "enabled": 0, "failing": 0, "problems": []}
    return mcp_registry.health_report()


def memory_section() -> dict:
    """What the model can currently be told to remember.

    ⚠️ PROJECT-SCOPED, because `count_memories()` is — see its docstring. The
    honest reading of this section is "what this workspace sees", and the
    `context.isolation` field says whether scoping is on at all.
    """
    from agent2.core import memory as core_memory
    from agent2.core import rules as core_rules
    rules = core_rules.list_rules()
    return {
        "memories":     core_memory.count_memories(),
        "rules":        len(rules),
        "rules_active": sum(1 for r in rules if r.get("active")),
    }


def context_section() -> dict:
    """The Context Broker — `broker.stats()`, which was written for this.

    Sources, the order, the budget basis and the two prompt caches' hit/miss
    counts. No item text: `stats()` reports the table, never the payload.
    """
    from agent2.core import broker
    return broker.stats()


def providers_section() -> dict:
    """Whether there is a credential to answer a turn with — counts only.

    ⚠️ NEITHER `rotator.status()` NOR `list_providers()` IS FORWARDED. The first
    carries a 14-character key preview and the second a base URL; both belong to
    their own routes. This counts them.

    ⚠️ AND THE CUSTOM-PROVIDER COUNT COMES FROM `count_providers()`, NOT FROM
    `len(list_providers())`. The `providers` table is created by
    `init_providers_table()`, not by `init_db()`, so the list form raises
    `no such table: providers` on any process that has not run an entry point —
    which this report then called a **fault**, 503-ing a database whose only
    peculiarity was having no custom providers. `count_providers()` reads absent
    as zero because that is what it is.
    """
    from agent2.llm import providers
    from agent2.llm import router as core_router
    from agent2.llm.keys import rotator
    keys = rotator.status()
    return {
        "gemini_keys":         len(keys),
        "gemini_keys_active":  sum(1 for k in keys if k.get("active")),
        "gemini_keys_failing": sum(1 for k in keys if int(k.get("errs") or 0)),
        "gemini_key_pinned":   any(k.get("pinned") for k in keys),
        "custom_providers":    providers.count_providers(),
        "routing":             (core_router.describe() or {}).get("routing", ""),
        "attempts":            core_router.stats(),
    }


def permissions_section() -> dict:
    """The capability posture plus the denial/allow tallies.

    ⚠️ `permissions.describe()` — whose own docstring names this report — and not
    a hand-picked subset, because the *posture* is what makes a tally readable:
    "denied: 41" is alarming until you see `AGENT2_DENY_CAPS` took `exec` away on
    purpose, which is a configuration and not a fault. It reports which
    capabilities exist, never *what* was denied; the payload of a refused request
    is precisely what an unauthenticated reader may not learn, and `counters()` is
    where that line is drawn.
    """
    from agent2.core import permissions as core_perms
    return core_perms.describe()


# ── The section table ────────────────────────────────────────────────────────
# ⚠️ ONE DECLARATION OF WHICH SUBSYSTEMS A HEALTH READ COVERS, IN WHICH ORDER,
# UNDER WHICH HUMAN NAME. A surface that hard-coded its own list would silently
# stop reporting the section a later phase adds — it keeps working, and simply
# never mentions the new subsystem. `label` lives here rather than in a renderer
# for the same reason: "Command Executor" spelled two ways in two surfaces is two
# declarations of one name.
SECTIONS: tuple[tuple[str, str, object], ...] = (
    ("agent",       "Agent",             agent_section),
    ("db",          "Database",          db_section),
    ("pool",        "Connection Pool",   pool_section),
    ("wal",         "WAL Checkpointer",  wal_section),
    ("scheduler",   "Scheduler",         scheduler_section),
    ("tasks",       "Task Queue",        tasks_section),
    ("commands",    "Command Executor",  commands_section),
    ("sync",        "Sync Layer",        sync_section),
    ("recovery",    "Crash Recovery",    recovery_section),
    ("mcp",         "MCP",               mcp_section),
    ("memory",      "Memory",            memory_section),
    ("context",     "Context Broker",    context_section),
    ("providers",   "Model Providers",   providers_section),
    ("permissions", "Permissions",       permissions_section),
)

LABELS: dict[str, str] = {key: label for key, label, _ in SECTIONS}


def collect() -> tuple[dict, list[tuple[str, str]]]:
    """Run every section builder. Returns `(payload, [(section, error), …])`.

    The pure gathering step — no verdict, no fault rules — so a caller that only
    wants the numbers (a dry run, a test) can have them without the judgement.
    """
    out: dict = {}
    errors: list[tuple[str, str]] = []
    for name, _label, fn in SECTIONS:
        data, err = _section(fn)
        out[name] = data if err is None else {"error": err}
        if err is not None:
            errors.append((name, err))
    return out, errors


def _faults(out: dict) -> list[tuple[str, str]]:
    """The lines that make this install unhealthy, tagged with their section.

    ⚠️ THE TEST FOR MEMBERSHIP IS "WOULD A RESTART OR AN OPERATOR FIX IT". A
    supported configuration may never appear here — see `_cautions()` for the
    four states that look like faults and are not.
    """
    bad: list[tuple[str, str]] = []

    dbi = out.get("db") or {}
    if not dbi.get("reachable"):
        bad.append(("db", "database unreachable"))
    elif dbi.get("schema_version") != dbi.get("schema_expected"):
        # A wrong schema resurfaces later as a baffling error in an unrelated
        # feature, which is exactly why migrations re-raise rather than degrade.
        # Surface it here too.
        bad.append(("db", f"schema at v{dbi.get('schema_version')}, "
                          f"expected v{dbi.get('schema_expected')}"))

    sch = out.get("scheduler") or {}
    # ⚠️ NOT "enabled but workers == 0" — the pool starts its workers lazily on
    # the first submit(), so that is the normal state of an idle server and the
    # check fired on a perfectly healthy app. The real fault is workers that
    # started and then went away: capacity lost, so submitted turns would sit in
    # the queue forever.
    if sch.get("worker_starts") and not sch.get("workers"):
        bad.append(("scheduler", "scheduler workers died — queued turns cannot run"))
    if sch.get("queued") and sch.get("queued") >= sch.get("max_queue", 0):
        bad.append(("scheduler", "turn queue full — new turns are being rejected"))

    # Task 10: the section already decided which servers are faults and worded
    # each line; this only lifts them into the aggregate so `ok`/503 covers MCP
    # too. ⚠️ It reads `problems`, NOT `failing` — re-deriving the sentence here
    # would be the second place that describes a broken bridge.
    bad.extend(("mcp", line)
               for line in (out.get("mcp") or {}).get("problems", []))

    # Phase 8, same treatment and for the same reason. ⚠️ A recovery record in
    # NEEDS_REVIEW is NOT a health problem — it is recovery working, and it may
    # sit there for weeks, so lifting `needs_review` here would pin this report at
    # 503 until somebody tidied a queue. `crash.stats()` decides what counts (only
    # its own internal errors do) and words the line; this lifts it.
    bad.extend(("recovery", line)
               for line in (out.get("recovery") or {}).get("problems", []))

    return bad


def _cautions(out: dict) -> list[tuple[str, str]]:
    """True, actionable, and NOT a fault — tagged with their section.

    ⚠️ THIS LIST EXISTS BECAUSE THE ALTERNATIVE WAS TO CRY WOLF. "No Gemini key
    and no custom provider" is the single most common reason a fresh install
    appears to do nothing, so the report must SAY it — but a half-onboarded
    install is a supported state, and 503-ing it would page somebody about a
    machine whose user simply has not run `/addapi` yet. Restarting nothing fixes
    it, which is the test for whether a line belongs in `_faults()` instead.

    A reader that wants "is it serving" reads the status code; a reader that wants
    "what should I do about it" reads both lists. ⚠️ Never promote a line from
    here to there for emphasis.
    """
    soft: list[tuple[str, str]] = []

    prov = out.get("providers") or {}
    if not prov.get("error") and not prov.get("gemini_keys") \
            and not prov.get("custom_providers"):
        soft.append(("providers", "no model credentials — add a Gemini key"
                                  " (/addapi) or a custom provider"))

    # A stuck command is reported, never killed (see `commands.watch()`), and it
    # may be a `sleep 600` the user typed on purpose. Worth a line; not a fault,
    # and lifting it would 503 the app for as long as it ran.
    stuck = ((out.get("commands") or {}).get("counts") or {}).get("stuck") or 0
    if stuck:
        soft.append(("commands", f"{stuck} command(s) producing no output"))

    # Same treatment for the two recovery-adjacent counts. A stale RUNNING task is
    # what worker recovery exists to collect and a needs-review row is recovery
    # *working* — `crash.stats()` already refuses to call either a problem, and
    # this is where they become visible without becoming an outage.
    stale = (out.get("tasks") or {}).get("stale") or 0
    if stale:
        soft.append(("tasks", f"{stale} task(s) running with no recent heartbeat"))
    review = (out.get("recovery") or {}).get("needs_review") or 0
    if review:
        soft.append(("recovery", f"{review} recovery record(s) awaiting review"))

    return soft


# ── Per-subsystem verdicts (Task 28's rendered list) ─────────────────────────

def _row(key: str, label: str, state: str, text: str) -> dict:
    """One verdict row. `ok` is `state != FAIL` — `off` and `warn` are both fine.

    ⚠️ `ok` IS NOT `state == OK`, for exactly the reason
    `registry.health()` documents: a server that is **off** is fine, and a second
    opinion is what prints a cross at a user who disabled it on purpose.
    """
    return {"key": key, "label": label, "state": state,
            "text": text, "ok": state != FAIL}


def _summary(key: str, data: dict) -> tuple[str, str]:
    """The `ok`/`off` word and one short phrase, for a section with no line.

    Only reached when the section produced neither a fault nor a caution, so this
    never has to describe a problem — that text is the fault's own, lifted by
    `_rows()` so the list and the aggregate cannot word one thing two ways.
    """
    if key == "db":
        return OK, f"reachable, schema v{data.get('schema_version')}"
    if key == "pool":
        return OK, (f"{data.get('idle', 0)} idle of max {data.get('max', 0)}")
    if key == "wal":
        # ⚠️ `interval_sec == 0` is the documented OFF switch, not a fault.
        if not data.get("interval_sec"):
            return OFF, "checkpointer disabled (AGENT2_WAL_CHECKPOINT_SEC=0)"
        return OK, (f"{int(data.get('wal_bytes') or 0) // 1024} KiB wal,"
                    f" {data.get('runs', 0)} checkpoint(s)")
    if key == "scheduler":
        # ⚠️ Same rule: a pool switched off runs turns on their own thread, which
        # is a supported configuration and not a degradation.
        if not data.get("enabled"):
            return OFF, "pool disabled — turns run on their own thread"
        return OK, (f"{data.get('workers', 0)}/{data.get('max_workers', 0)} worker(s),"
                    f" {data.get('queued', 0)} queued")
    if key == "tasks":
        return OK, (f"{data.get('running', 0)} running, {data.get('open', 0)} open")
    if key == "commands":
        counts = data.get("counts") or {}
        return OK, (f"{counts.get('active', 0)} active"
                    f" of last {data.get('window', 0)}")
    if key == "sync":
        return OK, ("poller live" if data.get("poller_running") else "poller idle")
    if key == "recovery":
        return OK, (f"{data.get('scans', 0)} scan(s),"
                    f" {data.get('tasks_recovered', 0)} recovered")
    if key == "memory":
        return OK, (f"{data.get('memories', 0)} memor(y/ies),"
                    f" {data.get('rules_active', 0)}/{data.get('rules', 0)} rule(s) active")
    if key == "context":
        return OK, f"{len(data.get('sources') or ())} source(s)"
    if key == "agent":
        return OK, (f"{data.get('default_model', '?')} · {data.get('default_mode', '?')}"
                    f" · {data.get('active_turns', 0)} live turn(s)")
    if key == "permissions":
        return OK, f"role {data.get('role', '?')}"
    return OK, ""


def _mcp_rows(data: dict, failed: str) -> list[dict]:
    """One row per MCP server — forwarded, never re-judged.

    ⚠️ `state` AND `text` COME STRAIGHT OFF `registry.health()`. That function is
    the shared verdict `/mcp health`, `/api/mcp` and the settings panel already
    render; deriving a fifth opinion from `connected` here is what prints `✗` at a
    user who switched a server off. The rows are expanded rather than folded into
    one "MCP" line because Task 28 asks for `✓ Burp MCP` and `✓ OWASP ZAP MCP`
    separately, and a folded line cannot say which of the two is down.
    """
    if failed:
        return [_row("mcp", LABELS["mcp"], FAIL, failed)]
    rows: list[dict] = []
    for srv in data.get("servers") or ():
        state = str(srv.get("state") or "")
        if state not in (OK, WARN, FAIL, OFF):
            # An unknown state word from a newer registry is reported as a
            # caution, never silently as healthy.
            state = WARN
        rows.append(_row(f"mcp.{srv.get('key') or '?'}",
                         f"{srv.get('label') or srv.get('key') or '?'} MCP",
                         state, str(srv.get("text") or "")))
    return rows


def _provider_rows(data: dict, failed: str, caution: str) -> list[dict]:
    """`Gemini Provider` and `Custom Providers` as two rows.

    ⚠️ AN UNCONFIGURED CREDENTIAL IS `off`, NOT `warn` — UNLESS THERE IS NO OTHER.
    A user who works entirely through a custom provider has no Gemini key on
    purpose, and printing `⚠ Gemini Provider` at them every time is how a warning
    column becomes wallpaper. The one state that genuinely needs saying is "no
    credential of any kind", and that judgement is not made here: it is
    `_cautions()`' line, passed in, so the row and the `warnings` list cannot
    disagree about whether this install can answer a turn.
    """
    if failed:
        return [_row("providers", LABELS["providers"], FAIL, failed)]
    keys = int(data.get("gemini_keys") or 0)
    live = int(data.get("gemini_keys_active") or 0)
    custom = int(data.get("custom_providers") or 0)

    if caution:
        gem = (WARN, caution)
    elif not keys:
        gem = (OFF, "no key configured")
    elif not live:
        gem = (WARN, f"{keys} key(s), none active")
    else:
        gem = (OK, f"{live}/{keys} key(s) active")

    return [
        _row("providers.gemini", "Gemini Provider", gem[0], gem[1]),
        _row("providers.custom", "Custom Providers",
             OK if custom else OFF,
             f"{custom} configured" if custom else "none configured"),
    ]


def _rows(out: dict, faults: list[tuple[str, str]],
          cautions: list[tuple[str, str]]) -> list[dict]:
    """The verdict list, derived from the lines the aggregate was built from.

    ⚠️ IT READS `faults` / `cautions` RATHER THAN RE-TESTING `out`. Re-testing
    would be a second implementation of every rule in `_faults()`, and the two
    would drift in the worst possible direction: a green ✓ beside a 503, or a red
    ✗ on a 200. Because the states are projected from the same lines, "some row
    says FAIL" and "the report is not ok" are the same fact by construction.
    """
    bad: dict[str, str] = {}
    soft: dict[str, str] = {}
    for key, text in faults:
        bad.setdefault(key, text)
    for key, text in cautions:
        soft.setdefault(key, text)

    rows: list[dict] = []
    for key, label, _fn in SECTIONS:
        data = out.get(key) or {}
        if key == "mcp":
            rows.extend(_mcp_rows(data, bad.get(key, "")))
            continue
        if key == "providers":
            rows.extend(_provider_rows(data, bad.get(key, ""), soft.get(key, "")))
            continue
        if key in bad:
            rows.append(_row(key, label, FAIL, bad[key]))
            continue
        if key in soft:
            rows.append(_row(key, label, WARN, soft[key]))
            continue
        state, text = _summary(key, data)
        rows.append(_row(key, label, state, text))
    return rows


def report() -> dict:
    """THE health payload. `GET /api/health` jsonifies it; `/health` renders it.

    Keys: one entry per section name, plus `sections` (the verdict list), `ok`,
    `problems` and `warnings`.

    ⚠️ `ok` IS `not problems`. A caution never changes it, and the caller turns
    `ok` into 200/503 without consulting anything else.
    """
    out, errors = collect()

    faults = [(k, f"{k}: {e}") for k, e in errors] + _faults(out)
    cautions = _cautions(out)

    out["sections"] = _rows(out, faults, cautions)
    out["ok"] = not faults
    out["problems"] = [text for _k, text in faults]
    out["warnings"] = [text for _k, text in cautions]
    return out


def verdict_lines(payload: dict | None = None) -> list[str]:
    """`✓ Database` … as plain text — the fallback body of every renderer.

    Kept here so a surface with no Rich, no colour and no table still prints the
    same words in the same order as one that has all three.
    """
    rows = (payload or report()).get("sections") or []
    marks = {OK: "✓", WARN: "⚠", FAIL: "✗", OFF: "○"}
    return [f"{marks.get(r.get('state'), '?')} {r.get('label', '?')}" for r in rows]
