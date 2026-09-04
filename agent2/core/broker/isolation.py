# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/core/broker/isolation.py — workspace scoping (Task 23)
─────────────────────────────────────────────────────────────
THE one declaration of "may this project see that row".

Workspace A must not be told about workspace B's memories, rules, skills or task
results unless the user explicitly shared them. Four different subsystems own
those four things, so the *rule* lives here and each of them asks — rather than
each of them growing its own idea of what a project is and what sharing means.
Four copies of a scoping predicate is the drift that produces no error at all:
one surface hides a row, another shows it, and neither can see the disagreement.

HOW A ROW IS SCOPED
  Every governed table has a `project` column (migrations 17 and 18) holding a
  canonical project key, or `''`.
    * `''` (`SHARED`) means EVERY project sees it.
    * anything else means only that project sees it.
  A read is "mine OR shared" (`scope_sql()`), a write is stamped with the current
  project (`stamp()`), and `stamp(shared=True)` is what "unless explicitly
  shared" means in code.

⚠️ `''` IS THE COLUMN DEFAULT, AND THAT IS LOAD-BEARING.
Every row that existed before Task 23 therefore reads as shared and stays visible
in every project, exactly as it was (rule 22 — preserve existing data; rule 8 —
preserve existing behaviour). An upgrade that had defaulted to "belongs to
whichever project happened to be open during the migration" would have made a
user's entire memory store vanish from every other checkout, with no error and
nothing to point at. The same default protects any writer that reaches the table
without going through `core.memory` / `core.rules`: an unstamped row is visible,
never invisible.

⚠️ THE KEY IS `core.context.project_key()`, NEVER A RAW PATH.
`canonical()` delegates to it. Migration 10 exists because a raw
`workspace.root()` was stored once: a row written from `C:\\…` was never found
again by a read for `c:\\…`, and the feature simply stopped working with nothing
in any log.

⚠️ THE PROMPT CACHES GO STALE ON A WORKSPACE SWITCH — THAT IS WHY HOOKS EXIST.
`broker.MEM_CACHE` / `broker.RULES_CACHE` are process-global `VersionedCache`es
keyed on the `memories` / `rules` resources, and nothing about a workspace switch
bumps either resource. Once their *content* depends on the current project, a
switch inside one process would keep serving project A's memories block to
project B — a silent, invisible leak of exactly what this module exists to
prevent. So `invalidate()` clears the project key AND fans out to whatever
registered through `on_invalidate()` (the broker registers its two caches), and
`_ensure_hooks()` wires that to `workspace.manager.on_switch` plus the `workspace`
sync topic. Installed lazily on first use, idempotently, and every failure is
swallowed — the same shape as `integrations/state.install_hooks()`, for the same
reason: a scoping rule that raises would take the turn down with it.

⚠️ `allows()` IS TOTAL, AND ITS TWO FALLBACKS POINT IN OPPOSITE DIRECTIONS ON
PURPOSE. An unknown row owner (`''`, NULL, unparseable) is ALLOWED, because that
is what pre-Task-23 data looks like and hiding a user's own rows is worse than
showing them. A row owned by a *known different* project is REFUSED. Only the
second case is a leak; the first is history.

⚠️ THE MODE IS ENV-ONLY (`AGENT2_CONTEXT_ISOLATION`), like `AGENT2_WEB_AUTH`.
`off` restores the pre-Task-23 global behaviour for someone who wants one shared
brain across every checkout (rule 8 — nothing is taken away). It is deliberately
NOT a `settings` row: a stored "off" is a persistent silent downgrade that
survives every restart with nothing on screen to say so. Read at call time, not
at import, so a test (and `/api/context`) sees the value actually in force rather
than the one that happened to be set when the module loaded.

⚠️ WITH ISOLATION OFF, A NEW ROW IS STAMPED `SHARED` — NOT WITH THE CURRENT
PROJECT. Writing the project key while the rule is off would mean that turning
isolation back on later hid rows the user created under global semantics. Absent
scoping must not quietly become scoping-after-the-fact.

What this module does NOT do: it never queries, never writes, and owns no table.
`core.memory` and `core.rules` apply it to their own SQL; Phase 11's skill state
and `broker._collect_tasks` apply it to theirs. It is a predicate, a stamp, and a
cache of one string.

(test_isolation.py — sabotage-verified)
"""

from __future__ import annotations

import os
import threading

# ── The vocabulary ─────────────────────────────────────────────────────────────

#: A row visible from every project. Also the column default, see the module note.
SHARED = ""

#: The column name in every governed table. NOT a caller's choice — it is part of
#: the rule, and hard-coding it is what keeps this module free of any SQL
#: identifier it would otherwise have to interpolate and validate.
COLUMN = "project"

#: Tables this rule governs today. Phase 11's per-workspace skill state joins it.
SCOPED_TABLES = ("memories", "rules")

MODE_PROJECT = "project"        # the default: this project's rows plus shared ones
MODE_OFF = "off"               # pre-Task-23 behaviour: one global store
MODES = (MODE_PROJECT, MODE_OFF)
DEFAULT_MODE = MODE_PROJECT

ENV_MODE = "AGENT2_CONTEXT_ISOLATION"


_lock = threading.RLock()
_cached_project: str | None = None
_listeners: list = []
_hooks_installed = False


# ── Policy ─────────────────────────────────────────────────────────────────────

def mode() -> str:
    """The isolation mode in force. Unknown values fall back to the strict end.

    Read live (see the module note). An unrecognised value is `project`, never
    `off`: a typo must not silently disable scoping, which is the same direction
    `AGENT2_WEB_ROLE` falls in.
    """
    try:
        raw = (os.environ.get(ENV_MODE) or "").strip().lower()
    except Exception:
        return DEFAULT_MODE
    return raw if raw in MODES else DEFAULT_MODE


def enabled() -> bool:
    """True when rows are scoped to a project at all."""
    return mode() != MODE_OFF


# ── The current project ────────────────────────────────────────────────────────

def canonical(path) -> str:
    """A project key from *path* — `core.context.project_key()`, or `SHARED`.

    Empty/None is `SHARED` and is NEVER passed through `project_key()`, which
    reads an empty argument as "the current directory" and would silently turn
    "shared" into "wherever this process happens to be standing".
    """
    if path is None:
        return SHARED
    text = str(path).strip()
    if not text:
        return SHARED
    try:
        from agent2.core import context as _context
        return _context.project_key(text)
    except Exception:
        try:
            return os.path.normcase(os.path.abspath(text))
        except Exception:
            return text


def _discover() -> str:
    """The active workspace as a project key. Total — '' if nothing is knowable."""
    try:
        from agent2.core import workspace as _ws
        return canonical(_ws.root())
    except Exception:
        try:
            return os.path.normcase(os.path.abspath(os.getcwd()))
        except Exception:
            return SHARED


def current() -> str:
    """THE current project key, cached until a workspace switch invalidates it.

    Cached because it is asked on every scoped read and write, and resolving it
    stats the workspace root. The cache is only ever as stale as
    `WorkspaceManager._current` itself, because the same switch that changes one
    clears the other — see `_ensure_hooks()`.
    """
    global _cached_project
    _ensure_hooks()
    with _lock:
        if _cached_project is not None:
            return _cached_project
    key = _discover()                 # outside the lock: it touches the filesystem
    with _lock:
        if _cached_project is None:
            _cached_project = key
        return _cached_project


# ── The rule ───────────────────────────────────────────────────────────────────

def stamp(*, shared: bool = False, project: str | None = None) -> str:
    """The value a NEW row's `project` column takes.

    `shared=True` is the user's explicit "every project may see this". With
    isolation off, everything is stamped `SHARED` — see the module note on why
    that is not the same as stamping the current project.
    """
    if shared or not enabled():
        return SHARED
    if project is not None:
        return canonical(project)
    return current()


def allows(row_project, project: str | None = None) -> bool:
    """May *project* (default: the current one) see a row owned by *row_project*?

    Total, and asymmetric on purpose — see the module note. Used where the scope
    cannot be expressed as SQL, e.g. a task session resolved by chat id.
    """
    if not enabled():
        return True
    owner = canonical(row_project)
    if owner == SHARED:
        return True                   # shared, or written before Task 23
    key = current() if project is None else canonical(project)
    if not key:
        return True                   # we do not know where we are; do not hide data
    return owner == key


def scope_sql(project: str | None = None) -> tuple[str, tuple]:
    """A `WHERE`-ready predicate for "this project's rows OR shared ones".

    Returns `(expression, params)`. The expression is ALWAYS valid SQL — `1=1`
    when nothing is being scoped — so a caller can interpolate it
    unconditionally instead of building a `WHERE` clause two ways and getting the
    `AND`s wrong in one of them.

    `IS NULL` is included alongside `''` because the column is only NOT NULL on a
    table created after migration 17; a row an older writer left NULL is
    pre-Task-23 data and reads as shared, exactly like `''`.
    """
    if not enabled():
        return ("1=1", ())
    key = current() if project is None else canonical(project)
    if not key:
        return ("1=1", ())
    return (f"({COLUMN}=? OR {COLUMN}=? OR {COLUMN} IS NULL)", (key, SHARED))


def is_shared(row_project) -> bool:
    """True if this row is visible from every project."""
    return canonical(row_project) == SHARED


# ── Invalidation ───────────────────────────────────────────────────────────────

def on_invalidate(fn) -> None:
    """Register `fn()` to run whenever the current project may have changed.

    Deduped, so importing a registrant twice cannot double-fire it.
    """
    with _lock:
        if fn not in _listeners:
            _listeners.append(fn)


def invalidate() -> None:
    """Forget the cached project key and tell every listener. Never raises."""
    global _cached_project
    with _lock:
        _cached_project = None
        listeners = list(_listeners)
    for fn in listeners:
        try:
            fn()
        except Exception:
            pass        # a broken listener must never break a workspace switch


def _ensure_hooks() -> None:
    """Wire `invalidate()` to a workspace switch. Idempotent, lazy, silent.

    Lazy rather than at import: this module is imported by the two tables it
    governs, and an import-time reach into `core.workspace` would put an ordering
    constraint on early startup for no gain.

    The flag is set BEFORE the wiring is attempted — the same shape as
    `integrations/state.install_hooks()`. A failure here is not retried: retrying
    per call would attempt the same broken import on every scoped read.

    Two signals, deliberately:
      * `manager.on_switch` — this process switched (`/workspace`). The only one
        that actually fires today.
      * the `workspace` sync topic — another process switched, or something bumps
        the resource in future. `RESOURCES` already lists it, so the poller
        already republishes it; subscribing costs nothing and means a later
        `sync.notify("workspace")` does not need to remember this module exists.
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
        sync.subscribe("workspace", lambda _topic, _payload: invalidate())
    except Exception:
        pass


# ── Reporting ──────────────────────────────────────────────────────────────────

def describe() -> dict:
    """The policy, for `broker.stats()` / `/api/health`. Policy only.

    ⚠️ Deliberately carries no project PATH. `/api/health` is counters-and-policy,
    and the absolute path of a user's working directory is neither. A caller that
    genuinely needs the key calls `current()`.
    """
    return {
        "mode": mode(),
        "enabled": enabled(),
        "env": ENV_MODE,
        "modes": list(MODES),
        "column": COLUMN,
        "shared_marker": SHARED,
        "scoped": list(SCOPED_TABLES),
        "listeners": len(_listeners),
    }
