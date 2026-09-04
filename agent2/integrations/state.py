# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/integrations/state.py
───────────────────────────
THE persisted MCP facts: which servers auto-connect **per project** (Task 9), and
where each server is plus what credential reaches it (`mcp_config`).

Two tables, two deliberately different scopes, one module — because they share a
cache, a `notify("mcp")` topic and one set of invalidation hooks, and splitting
them would mean two of each that must be kept in step by hand.

⚠️ ENABLEMENT IS PER PROJECT; THE ENDPOINT AND KEY ARE PER MACHINE.
"Should ZAP arm itself in this checkout" is a per-target decision, so it is keyed
by project. "Which ZAP, on which port, with which security key" describes the one
ZAP running on this machine; keying THAT per project would make the user retype
the same endpoint in every checkout — the friction Task 9 removed, reintroduced.

⚠️ ONE DECLARATION OF "IS THIS SERVER ENABLED HERE?"
`McpBridge.enabled` is a property that reads through to `is_enabled()` on every
access, and its setter writes through to `set_enabled()`. That is deliberate and
is the whole design: the alternative — an attribute loaded once in `__init__`
plus a `reload()` that some future workspace-switch path forgets to call — is a
second copy of the answer that drifts silently. It reads right in the menu and
dials the wrong server. Reading through means a `/workspace` switch, a web folder
pick, or another process's toggle changes the answer with no reload call
anywhere, because there is nothing to reload.

⚠️ `McpBridge.url` AND `ZapMCP.key` READ THROUGH FOR THE SAME REASON, AND THE
FAILURE THEY PREVENT IS DUAL MODE.
`agent2dual.py` runs the web server and the CLI as two processes over one DB. Set
ZAP's port with `/mcp zap config` in the terminal and, with a cached URL, the
browser half would still dial the old port — a connection failure whose cause is
invisible on both surfaces because each one shows the value *it* believes. A
read-through property plus the `mcp` sync topic means the other process is right
on its next access, with nothing to reload.

⚠️ PRECEDENCE IS ROW → LEGACY GLOBAL SETTING → ENV DEFAULT, AND THE MIDDLE STEP
IS NOT OPTIONAL.
Before this module there were two GLOBAL `settings` rows, `burp_auto_connect` and
`zap_auto_connect`, and existing installs have them. Reading only `mcp_state`
would leave every one of those users with their bridges silently off after an
upgrade, which is precisely the "do not require users to reconfigure" clause this
task exists to satisfy (and rule 22 — preserve existing data). So a project with
no row of its own inherits the legacy global value, and only then the env default.
`mcp_config` follows the same shape with one step fewer: stored row → env
(`BURP_MCP_URL`, `ZAP_MCP_URL`, `ZAP_MCP_KEY`), so an install that configures
Agent2 through the environment keeps working until someone edits it in the UI.

⚠️ THE LEGACY SETTING IS READ FOREVER AND WRITTEN NEVER.
Keeping the global row in sync on every toggle looks harmless and is the bug this
whole table replaces: the global is the fallback for every project that has NOT
been configured, so writing it would make "turn ZAP off in this checkout" quietly
turn ZAP off in each of those, which is the cross-project bleed rule 30 forbids.
A toggle writes exactly one row: this project's.

⚠️ THE PROJECT KEY IS THE WORKSPACE ROOT THROUGH `context.project_key()`.
Not `os.getcwd()`: a shell that wandered into `src/` is the same project, and the
workspace root is the boundary `/workspace` switches. Not the raw path either —
`workspace.root()` is not normcased, so on Windows a row written from `C:\\Users\\…`
would never be found again by a read for `c:\\users\\…`. That exact mismatch has
already cost this repo a silent bug once (see migration 10), so the same one
canonical form is used here.

⚠️ SWITCHING PROJECTS DOES NOT DISCONNECT A LIVE BRIDGE.
`enabled` means "auto-connect on each agent turn", so a project where it is off
simply stops dialling. Tearing down an established session because the user
changed directory would kill an in-flight scan to enforce a preference about
*starting* one — a destructive act nobody asked for (rule 21). The state is what
is project-scoped; the socket is process-wide and stays under the user's control
via `/mcp` and `/api/mcp/<key>/disconnect`.

⚠️ A STORED SECURITY KEY NEVER LEAVES THIS MODULE IN ANY READABLE FORM EXCEPT TO
THE TRANSPORT.
`mask_secret()` returns bullets whose COUNT is capped and whose CONTENT is not
derived from the key — not a prefix, not a suffix, not a length past the cap. The
`providers` table redacts with `k[:6]…k[-4:]`, which is defensible for a 40-char
vendor API key and is not for a ten-character ZAP key, where that pattern prints
almost the whole secret. Surfaces get `key_set: bool` plus bullets; the only code
that sees the real value is `ZapMCP.auth_headers()`.

Every function here is total: this is read on the agent's hot path, from both
agent loops and three CLI surfaces, so a DB fault degrades to the env default
rather than costing a turn.
"""

from __future__ import annotations

import threading

# Task 16: the ZAP security key is stored as a `a2s:` reference. `core.secrets`
# depends only on `config` and `core.logging`, so unlike `database` and `sync`
# (imported inside functions here to stay import-cycle-free) it is safe at module
# scope.
from agent2.core import secrets as _secrets

# Cache of the last read, keyed by (project, server). MCP state changes at human
# speed and is read once per turn per bridge, so this only exists to keep an
# `enabled` property honest about being cheap. It is dropped wholesale on any
# write and on any `mcp` sync event, never selectively expired.
_cache: dict[tuple[str, str], bool] = {}
# The same for `mcp_config`, keyed by server alone because that table is
# machine-scoped. Deliberately a SECOND dict rather than a sentinel key in the
# first: the values are dicts, not bools, and one map holding both would need a
# type check at every read to tell "not configured" from "configured off".
_config_cache: dict[str, dict] = {}
_lock = threading.RLock()
_hooks_installed = False


def _truthy(val) -> bool:
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def project() -> str:
    """The canonical key for the project MCP state is scoped to.

    Falls back to the process cwd if the workspace manager is unavailable — an
    answer keyed on the wrong project is still better than a raised exception on
    the tool-dispatch path.
    """
    try:
        from agent2.core.context import project_key
        from agent2.core.workspace import root
        return project_key(str(root()))
    except Exception:
        try:
            from agent2.core.context import project_key
            return project_key()
        except Exception:
            return ""


def invalidate() -> None:
    """Drop every cached answer (a write here, or a change in another process)."""
    with _lock:
        _cache.clear()
        _config_cache.clear()


def is_enabled(server: str, *, legacy_key: str = "", env_default: bool = False) -> bool:
    """Auto-connect for *server* in the current project.

    Precedence: this project's row → *legacy_key* in `settings` → *env_default*.
    See the module docstring for why the middle step is load-bearing.
    """
    server = (server or "").strip().lower()
    if not server:
        return bool(env_default)
    proj = project()
    ck = (proj, server)
    with _lock:
        if ck in _cache:
            return _cache[ck]

    value = bool(env_default)
    try:
        from agent2.database import get_setting, qone
        row = qone("SELECT enabled FROM mcp_state WHERE project=? AND server=?", (proj, server))
        if row is not None:
            value = bool(row["enabled"])
        elif legacy_key:
            legacy = get_setting(legacy_key)
            if legacy is not None:
                value = _truthy(legacy)
    except Exception:
        # No table yet (a DB from before migration 11 that has not been opened
        # through init_db), a locked file, a missing column — all mean "we do not
        # know", and the env default is the documented answer for that.
        return bool(env_default)

    with _lock:
        _cache[ck] = value
    return value


def set_enabled(server: str, on: bool) -> None:
    """Persist auto-connect for *server* in the current project, and announce it.

    Writes exactly one row — never the legacy global key (module docstring).
    """
    server = (server or "").strip().lower()
    if not server:
        return
    proj = project()
    try:
        from agent2.database import exe
        exe(
            "INSERT INTO mcp_state(project, server, enabled, updated_at)"
            " VALUES(?,?,?,datetime('now'))"
            " ON CONFLICT(project, server) DO UPDATE SET"
            " enabled=excluded.enabled, updated_at=excluded.updated_at",
            (proj, server, 1 if on else 0),
        )
    except Exception:
        # The toggle still has to take effect for this process, so the cache is
        # written below regardless: the user asked, and the surface that asked
        # reports a failed write — silently ignoring it would not.
        pass
    # ⚠️ NOTIFY BEFORE CACHING, NOT AFTER. `notify()` publishes synchronously and
    # this module subscribes to its own resource to catch OTHER processes, so the
    # listener runs inline and clears the cache. Caching first would hand that
    # listener the fresh value to throw away, and the next read would go back to
    # the DB — right by luck when the write landed, and wrong when it did not.
    try:
        from agent2.core import sync
        sync.notify("mcp", server=server, enabled=bool(on))
    except Exception:
        pass
    with _lock:
        _cache[(proj, server)] = bool(on)


def states_for_project(proj: str | None = None) -> dict[str, bool]:
    """Every explicitly configured server for *proj* (default: current project).

    Only rows that exist — a server absent from the result is on its fallback,
    which is not the same as being off.
    """
    key = proj if proj is not None else project()
    try:
        from agent2.database import qall
        rows = qall("SELECT server, enabled FROM mcp_state WHERE project=?", (key,))
        return {str(r["server"]): bool(r["enabled"]) for r in rows}
    except Exception:
        return {}


def install_hooks() -> None:
    """Invalidate the cache when the workspace switches or another process writes.

    Idempotent, and called from `integrations/registry.py` at import. Note what
    it does NOT do: it never pushes a value into a bridge, because there is no
    stored copy to push into — `McpBridge.enabled`, `.url` and `ZapMCP.key` all
    read through. All that is needed on a switch is to forget what was cached.
    """
    global _hooks_installed
    with _lock:
        if _hooks_installed:
            return
        _hooks_installed = True
    try:
        from agent2.core.workspace import manager
        manager.on_switch(lambda _old, _new: invalidate())
    except Exception:
        pass
    try:
        from agent2.core import sync
        sync.subscribe("mcp", lambda _topic, _payload: invalidate())
    except Exception:
        pass


# ── Endpoint + credential config (migration 12) ────────────────────────────────
# Machine-scoped, NOT project-scoped — see the module docstring for why the two
# tables are keyed differently on purpose.

def _read_config_row(server: str) -> tuple[dict, bool]:
    """`(row, known)` for *server* — cached, and total on any DB fault.

    ⚠️ THE SECOND ELEMENT IS THE WHOLE POINT. `{}` is returned both for "this
    server has no stored row" and for "the read failed", and those two are
    opposite instructions to a writer: the first says *there is nothing to
    preserve*, the second says *we do not know what we would be overwriting*.
    `set_config` seeds the cache from this read, so collapsing them lets a
    transient DB fault turn a port edit into a credential wipe — the row keeps
    the key (the SQL COALESCE sees to that) while this process serves a cached
    row that has no `security_key` at all, and `auth_headers()` then sends
    nothing. `known` is what lets the writer decline to seed a row it cannot
    vouch for.
    """
    server = (server or "").strip().lower()
    if not server:
        return {}, True
    with _lock:
        hit = _config_cache.get(server)
        if hit is not None:
            return hit, True
    row: dict = {}
    try:
        from agent2.database import qone
        found = qone("SELECT url, security_key FROM mcp_config WHERE server=?", (server,))
        if found is not None:
            row = {"url": str(found["url"] or ""),
                   "security_key": str(found["security_key"] or "")}
    except Exception:
        # Pre-migration DB, locked file, missing column: "we do not know", and the
        # caller's env default is the documented answer for that. NOT cached —
        # caching a failure would outlive the fault that caused it.
        return {}, False
    with _lock:
        _config_cache[server] = row
    return row, True


def _config_row(server: str) -> dict:
    """The stored row for *server*, or {} — cached, and total on any DB fault."""
    return _read_config_row(server)[0]


def get_url(server: str, *, env_default: str = "") -> str:
    """Where *server* lives: stored row → *env_default*."""
    return _config_row(server).get("url") or env_default


def get_key(server: str, *, env_default: str = "") -> str:
    """The security key for *server*: stored row → *env_default*.

    ⚠️ THE ONLY CALLER THAT MAY SHOW THIS IS THE TRANSPORT. Every surface path
    goes through `mask_secret()` / `config_for()` instead.

    ⚠️ Task 16: the stored value is a `a2s:` reference, so it is RESOLVED here.
    This is the one read that hands over the usable credential, which makes it the
    one place resolution belongs — the cache and `config_for()` deliberately keep
    the reference, so a surface that forgets to mask leaks ciphertext rather than a
    key. `resolve()` passes a legacy plaintext row through unchanged.
    """
    stored = _config_row(server).get("security_key") or ""
    if stored:
        return _secrets.resolve(stored) or env_default
    return env_default


def set_config(server: str, *, url: str | None = None, key: str | None = None) -> None:
    """Persist endpoint and/or credential for *server*, and announce the change.

    ⚠️ `None` MEANS "LEAVE IT ALONE" AND `""` MEANS "CLEAR IT" — the distinction
    is the whole reason both parameters are optional. `/mcp zap config` and the
    web form both offer a key field pre-filled with bullets, and a user who edits
    only the port submits that form with an untouched key field. Treating the
    absent value as an empty string would wipe a working credential on an edit
    that never mentioned it — the same guard `llm/providers.update_provider()`
    carries, for the same reason. Clearing stays possible, but only by asking.
    """
    server = (server or "").strip().lower()
    if not server:
        return
    if url is None and key is None:
        return
    # ⚠️ THE WHOLE ROW IS READ BEFORE THE WRITE, AND THAT IS NOT AN OPTIMISATION.
    # The cache is seeded at the end so a failed DB write still takes effect for
    # this process — but seeding it from an EMPTY dict builds a partial row, and a
    # partial row is served as authoritative. `notify()` publishes synchronously
    # and this module's own listener clears the cache inline, so by the time the
    # seed runs there is nothing left to merge with: `{"url": new}` would be
    # cached with no `security_key`, and `get_key()` would report an unset
    # credential that is sitting in the table untouched. Same silent loss the SQL
    # comment below describes, one layer up, and it shipped here once.
    current, known = _read_config_row(server)
    current = dict(current)
    # ⚠️ Task 16: seal ONCE, here, and use the same reference for the write and for
    # the cache seed. Sealing separately in two places would put two different
    # ciphertexts in play (AES-GCM uses a fresh nonce per call), and the cached one
    # would silently win for the rest of the process — so a `get_key()` after an
    # edit could resolve a ref the table has never seen. `""` still means "clear
    # it": `seal("")` returns `""`, so the empty case passes through untouched and
    # the contract above is preserved exactly.
    sealed_key = (None if key is None
                  else _secrets.seal(str(key).strip(), namespace="mcp_config",
                                     name=server))
    stored = False
    try:
        from agent2.database import exe
        # ⚠️ NO `COALESCE` IN THE `VALUES` CLAUSE. Folding NULL to '' there makes
        # `excluded.url` an empty STRING, which the UPDATE's COALESCE then happily
        # keeps — silently turning "leave it alone" into "clear it". That is the
        # exact bug this function's contract forbids, and it is invisible: the
        # write succeeds and the other field is simply gone. NULL has to survive
        # all the way to `excluded` for the UPDATE to be able to skip it. A fresh
        # INSERT may therefore store NULL, which every read normalises with `or ""`.
        exe(
            "INSERT INTO mcp_config(server, url, security_key, updated_at)"
            " VALUES(?,?,?,datetime('now'))"
            " ON CONFLICT(server) DO UPDATE SET"
            " url=COALESCE(excluded.url, mcp_config.url),"
            " security_key=COALESCE(excluded.security_key, mcp_config.security_key),"
            " updated_at=excluded.updated_at",
            (server,
             None if url is None else str(url).strip(),
             sealed_key),
        )
        stored = True
    except Exception:
        # As with `set_enabled`: the change still takes effect for this process
        # via the cache below, and the surface that asked reports the failure.
        pass
    # Same ordering rule as `set_enabled` — notify first so the inline listener
    # clears the cache, then seed it with what was just written.
    try:
        from agent2.core import sync
        sync.notify("mcp", server=server, config=True)
    except Exception:
        pass
    if stored and not known:
        # ⚠️ THE WRITE LANDED BUT THE PRE-WRITE READ DID NOT, SO `current` IS A
        # GUESS AND THE TABLE IS NOW THE ONLY THING THAT KNOWS THE TRUTH. Seeding
        # from a guess here is what silently drops the ZAP key: the row still has
        # it (the SQL COALESCE saw to that), but every `get_key()` in this process
        # would serve a dict that never mentions it, and `auth_headers()` would
        # send nothing against a server that requires it. Drop the entry instead
        # — the next read re-queries and gets the row we just wrote.
        #
        # Only when the write ALSO failed do we fall through to the seed below:
        # nothing was persisted, so there is no truth to contradict, and the
        # documented degradation (the edit still takes effect for this process,
        # unset fields answering from the env default) is the honest answer.
        with _lock:
            _config_cache.pop(server, None)
        return
    with _lock:
        # `current`, not `_config_cache.get(server)`: the notify above already
        # cleared that entry (see the read comment at the top of this function).
        row = dict(current)
        if url is not None:
            row["url"] = str(url).strip()
        if key is not None:
            row["security_key"] = sealed_key
        _config_cache[server] = row


def clear_config(server: str) -> None:
    """Forget the stored endpoint and key for *server* (back to env defaults).

    Task 16: the backing material goes too, and it goes FIRST — while the row that
    points at it can still be read. An encrypted reference carries its own
    ciphertext so the DELETE is sufficient, but a keyring reference names an entry
    in the user's OS credential store that would otherwise sit there forever with
    nothing left to explain it.
    """
    server = (server or "").strip().lower()
    if not server:
        return
    try:
        from agent2.database import qone
        row = qone("SELECT security_key FROM mcp_config WHERE server=?", (server,))
        if row:
            _secrets.forget(str(row.get("security_key") or ""))
    except Exception:
        pass
    try:
        from agent2.database import exe
        exe("DELETE FROM mcp_config WHERE server=?", (server,))
    except Exception:
        pass
    try:
        from agent2.core import sync
        sync.notify("mcp", server=server, config=True)
    except Exception:
        pass
    with _lock:
        _config_cache.pop(server, None)


def mask_secret(value: str) -> str:
    """A fixed-width stand-in for a stored secret, safe to print anywhere.

    ⚠️ NOTHING ABOUT THE RETURN VALUE IS DERIVED FROM *value* EXCEPT WHETHER IT IS
    EMPTY. No prefix, no suffix, and a constant bullet count so the length does
    not leak either — a masked ten-character ZAP key must not narrow a brute force
    to ten characters. `providers.list_providers(safe=True)` shows `k[:6]…k[-4:]`,
    which is a reasonable trade for a 40-char vendor token and a bad one here.
    """
    return "••••••••" if (value or "").strip() else ""


def config_for(server: str, *, url_default: str = "", key_default: str = "") -> dict:
    """What a SURFACE may know about *server*'s config.

    ⚠️ THE RETURN VALUE IS SAFE TO SEND TO A BROWSER AND TO PRINT IN A TERMINAL,
    and that is its only job: `key_set` and `key_masked` instead of the key.
    Anything that needs the real credential calls `get_key()` — there is exactly
    one such caller, `ZapMCP.auth_headers()`.
    """
    row = _config_row(server)
    key = row.get("security_key") or key_default
    return {
        "server":     (server or "").strip().lower(),
        "url":        row.get("url") or url_default,
        "url_source": "config" if row.get("url") else ("env" if url_default else "none"),
        "key_set":    bool(key),
        "key_source": "config" if row.get("security_key") else ("env" if key_default else "none"),
        "key_masked": mask_secret(key),
    }
