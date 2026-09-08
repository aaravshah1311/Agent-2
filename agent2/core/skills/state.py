# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2.core.skills.state
────────────────────────
THE per-project record of which skills are switched on — Task 35's *"state is
workspace-aware"*, and the half of `/skills` that touches a database instead of a
file.

⚠️ **ENABLEMENT IS A DATABASE ROW, NEVER AN EDIT TO A SKILL FILE.** That is the
task's own bar — *enable/disable never touches a skill file* — and it is also the
only design that could work: a skill is frequently somebody else's file, checked
into somebody else's repository, written for another agent. Toggling it by adding
`enabled: false` to their frontmatter would dirty their working tree, lose the
setting on their next `git pull`, and make "on in this checkout" a statement about
every checkout that shares the file. So there is no write path to disk anywhere in
this package.

⚠️ **THREE STATES, AND THE THIRD IS THE POINT.**

    True  — the user turned it on here
    False — the user turned it off here
    None  — never chosen (no row)

`None` is not `False`. Task 36 ranks *auto-relevant* below *enabled* and above
*general*, so a skill nobody has ever toggled must still be allowed to apply when
the request clearly asks for it, while a skill the user explicitly switched **off**
must not — even when the request mentions it by name. Collapsing absence into
`False` would mean every newly written skill starts disabled and the user has to
turn on each one by hand; collapsing it into `True` would put every file in the
folder into the prompt, which is exactly what Task 34 forbids. Both collapses read
as "skills are broken".

⚠️ **SCOPING GOES THROUGH `broker.isolation`, NOT THROUGH A LOCAL PREDICATE.**
`scope_sql()` builds the read, `stamp()` builds the write, and `skill_state` is
named in `isolation.SCOPED_TABLES`. A `WHERE project=?` written here would be the
fourth copy of a rule that already has one home, and the copies drift in the
direction nobody can see: one surface hides a toggle another surface shows.

⚠️ **`sync.notify("skills")` IS MANDATORY, NOT POLITE.** Dual mode is two
processes over one `agent2.db`, and discovery caches for `SKILLS_TTL`. Without the
topic — which had to be added to `sync.RESOURCES`, because the poller republishes
only what it iterates — a `/skills` toggle in the terminal would leave the
browser's next prompt on the previous selection until its process restarted, with
both halves showing the value each believed and neither showing the disagreement.
That is the bug `integrations/state.py`'s docstring describes, in a second place.

Nothing here raises: every function is total and a failed read degrades to
*never chosen*, which is the state that lets selection fall back to relevance.
"""

import threading

from agent2.core.broker import isolation as _iso

TOPIC = "skills"

# (project, skill_id) → bool. Only rows that EXIST are cached; a miss is a read.
_cache: dict[tuple[str, str], bool] = {}
_loaded: set[str] = set()          # projects whose rows have been read in full
_lock = threading.RLock()
_hooks_installed = False


def project() -> str:
    """The project key rows are scoped by — `isolation` decides, not this module."""
    return _iso.stamp()


def invalidate() -> None:
    """Forget every cached row. Total, and safe from a hook or a signal handler."""
    with _lock:
        _cache.clear()
        _loaded.clear()


def install_hooks() -> None:
    """Wire invalidation to a workspace switch and to another process's write.

    Idempotent and lazy, the same shape as `integrations/state.install_hooks()`.
    Two signals for two different events: `isolation.on_invalidate` covers "the
    project changed" (it already owns both halves of that — `on_switch` and the
    `workspace` sync topic), and the `skills` topic covers "somebody else toggled
    one". Neither is optional in dual mode.

    ⚠️ The flag is set BEFORE the wiring is attempted, deliberately: retrying a
    broken import on every read would pay the failure per toggle rather than once.
    """
    global _hooks_installed
    with _lock:
        if _hooks_installed:
            return
        _hooks_installed = True
    try:
        _iso.on_invalidate(invalidate)
    except Exception:  # degrades to a stale cache, never a failed turn
        pass
    try:
        from agent2.core import sync
        sync.subscribe(TOPIC, lambda _topic, _payload: invalidate())
    except Exception:  # as above
        pass


def _load(proj: str) -> dict[str, bool]:
    """Every row for *proj*, cached. One query per project per invalidation.

    Read as "mine OR shared" through `isolation.scope_sql()`, so a row a writer
    left unstamped (`''`) is visible here rather than invisible — the direction
    migration 17/18 chose and this table's DDL repeats.

    ⚠️ THE MERGE BELOW IS DELIBERATE: a row already in the cache is NOT overwritten
    by what the database says. The only entries that can survive an invalidation
    are the ones this process wrote *after* it (`set_enabled` caches after
    notifying, and its own inline listener clears the cache in between), so
    preserving them is what makes a toggle take effect here even when the write
    itself failed — the user asked, and the surface that asked reports the failure.
    A remote change cannot be masked by this: the notify that carries it clears the
    whole cache first, leaving nothing to preserve.
    """
    install_hooks()
    with _lock:
        if proj in _loaded:
            return {sid: on for (p, sid), on in _cache.items() if p == proj}
    rows: list = []
    try:
        from agent2.database import qall
        pred, params = _iso.scope_sql(proj or None)
        rows = qall(f"SELECT skill_id, enabled FROM skill_state WHERE {pred}", params)
    except Exception:  # a missing table or a locked DB reads as "nothing chosen"
        rows = []
    with _lock:
        for r in rows:
            _cache.setdefault((proj, str(r["skill_id"])), bool(r["enabled"]))
        _loaded.add(proj)
        return {sid: on for (p, sid), on in _cache.items() if p == proj}


def states(proj: str | None = None) -> dict[str, bool]:
    """Every skill this project has an explicit answer for. Absent ⇒ never chosen.

    ⚠️ The result contains ONLY rows that exist. A caller must not read a missing
    key as `False` — `get()` is the tri-state accessor, and `select()` depends on
    the difference (see the module docstring).
    """
    return _load(proj if proj is not None else project())


def get(skill_id: str, proj: str | None = None) -> bool | None:
    """`True` / `False` / `None` — on, off, or never chosen here."""
    sid = str(skill_id or "").strip().lower()
    if not sid:
        return None
    return states(proj).get(sid)


def is_enabled(skill_id: str, *, default: bool = False, proj: str | None = None) -> bool:
    """The two-valued form, with the caller stating which way it wants to be wrong.

    ⚠️ `default` is required-by-convention for the reason `capabilities.supports()`
    takes one: *unknown* is a real third answer, and a helper that silently picked
    a side would make every call site's assumption invisible. A prompt asks with
    `default=False` (do not inject what nobody asked for); a report asks with the
    tri-state `get()` and prints "—".
    """
    val = get(skill_id, proj)
    return default if val is None else val


def set_enabled(skill_id: str, enabled: bool | None, proj: str | None = None) -> bool:
    """Turn a skill on (`True`), off (`False`) or back to never-chosen (`None`).

    Returns whether the write landed. ⚠️ `None` **deletes the row** rather than
    writing a third value: "never chosen" is the absence of a decision, and
    encoding it as data would give the table two spellings for one state — one of
    which no migration and no other writer knows about.
    """
    sid = str(skill_id or "").strip().lower()
    if not sid:
        return False
    key = proj if proj is not None else project()
    ok = True
    try:
        from agent2.database import exe
        if enabled is None:
            exe("DELETE FROM skill_state WHERE project=? AND skill_id=?", (key, sid))
        else:
            exe(
                "INSERT INTO skill_state(project, skill_id, enabled, updated_at)"
                " VALUES(?,?,?,datetime('now'))"
                " ON CONFLICT(project, skill_id) DO UPDATE SET"
                " enabled=excluded.enabled, updated_at=excluded.updated_at",
                (key, sid, 1 if enabled else 0),
            )
    except Exception:  # reported by return value; the caller prints
        ok = False
    _announce()
    with _lock:
        # ⚠️ AFTER the notify, for the reason `integrations/state.set_enabled()`
        # states: `notify()` publishes synchronously and this module subscribes to
        # its own topic, so the inline listener would otherwise throw away exactly
        # the value just cached and the next read would go back to the DB — right
        # by luck when the write landed and wrong when it did not.
        if enabled is None:
            _cache.pop((key, sid), None)
        else:
            _cache[(key, sid)] = bool(enabled)
    return ok


def set_many(changes: dict, proj: str | None = None) -> int:
    """Apply several toggles as ONE write and ONE announcement. Returns the count.

    ⚠️ The bulk form exists because `/skills` is a multi-select menu: the `/rules`
    menu learned this first, and its rule is written down — *two uniform bulk
    writes, never N toggles*. N round trips is N `notify()`s, N cache clears and,
    in dual mode, N chances for the other process to poll a half-applied selection
    and render a state the user never chose.
    """
    if not changes:
        return 0
    key = proj if proj is not None else project()
    on_ids = [str(k).strip().lower() for k, v in changes.items() if v is True]
    off_ids = [str(k).strip().lower() for k, v in changes.items() if v is False]
    clear_ids = [str(k).strip().lower() for k, v in changes.items() if v is None]
    on_ids = [s for s in on_ids if s]
    off_ids = [s for s in off_ids if s]
    clear_ids = [s for s in clear_ids if s]
    written = 0
    try:
        from agent2.database import batch, exemany
        with batch():
            for flag, ids in ((1, on_ids), (0, off_ids)):
                if not ids:
                    continue
                exemany(
                    "INSERT INTO skill_state(project, skill_id, enabled, updated_at)"
                    " VALUES(?,?,?,datetime('now'))"
                    " ON CONFLICT(project, skill_id) DO UPDATE SET"
                    " enabled=excluded.enabled, updated_at=excluded.updated_at",
                    [(key, sid, flag) for sid in ids],
                )
                written += len(ids)
            if clear_ids:
                exemany("DELETE FROM skill_state WHERE project=? AND skill_id=?",
                        [(key, sid) for sid in clear_ids])
                written += len(clear_ids)
    except Exception:  # the caller reports; the cache below still applies
        pass
    _announce()
    with _lock:
        for sid in on_ids:
            _cache[(key, sid)] = True
        for sid in off_ids:
            _cache[(key, sid)] = False
        for sid in clear_ids:
            _cache.pop((key, sid), None)
    return written


def clear(proj: str | None = None) -> int:
    """Forget every choice made in this project. Returns rows removed.

    The one destructive operation here, and it destroys *choices*, never files.
    """
    key = proj if proj is not None else project()
    n = 0
    try:
        from agent2.database import exe, qone
        row = qone("SELECT COUNT(*) c FROM skill_state WHERE project=?", (key,))
        n = int((row or {}).get("c") or 0)
        exe("DELETE FROM skill_state WHERE project=?", (key,))
    except Exception:  # nothing to clear, or nothing readable
        n = 0
    _announce()
    with _lock:
        for k in [k for k in _cache if k[0] == key]:
            _cache.pop(k, None)
        _loaded.discard(key)
    return n


def _announce() -> None:
    """Publish the `skills` topic. Never raises — see the module docstring."""
    try:
        from agent2.core import sync
        sync.notify(TOPIC)
    except Exception:  # degrades to a TTL-delayed refresh
        pass
    try:
        from agent2.core.skills import discovery as _disc
        _disc.invalidate()
    except Exception:  # as above
        pass


def describe() -> dict:
    """Counters for `/api/skills` and `stats()`. ⚠️ No project path, no skill text."""
    with _lock:
        cached = len(_cache)
    st = states()
    return {
        "topic": TOPIC,
        "cached": cached,
        "chosen": len(st),
        "on": sum(1 for v in st.values() if v),
        "off": sum(1 for v in st.values() if not v),
        "scoped": _iso.enabled(),
    }
