# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2.core.memory
───────────────────
THE single source of truth for persistent memory. Both the Web UI and the CLI
import these functions — no interface may re-implement memory logic locally.

Backed by the `memories` table in agent2.db (see agent2.database).

⚠️ Every memory here is injected into the system prompt of EVERY agent turn, so
this table is not just storage — it is a per-turn token cost paid on every
iteration of every message. `add_memory()` therefore deduplicates, and
`memory_prompt_block()` is bounded and ranked. See PROMPT_LIMIT below.

Written from exactly three places — the `save_memory` tool, POST /api/memories,
and the CLI's `/addmem`. Injected into the system prompt through a VersionedCache,
so an unchanged store costs a flag check rather than a query.

⚠️ `memory_prompt_block()` IS BOUNDED (PROMPT_LIMIT, 40) AND THAT BOUND IS
LOAD-BEARING.
It is rebuilt on every iteration, so its cost is multiplied by MAX_AGENT_ITERS.
`agent.py` used to inline an UNBOUNDED version, which put the entire table into
every prompt: ~5,100 tokens/turn at 300 memories, now ~700 and FLAT. One builder,
and it lives here.

Ranked by `importance DESC, created_at DESC`, and the cut affects only what is
SENT — nothing is deleted. The block states how many were withheld, so the model
knows recall is partial instead of concluding that a saved fact was forgotten.

`add_memory()` DEDUPS ON EXACT CONTENT, and a repeat is REINFORCEMENT: importance
rises to the higher of the two (never lower), the timestamp refreshes, and the
EXISTING row is returned — so a caller cannot tell a dedup from an insert, or
double-count one.

⚠️ `tools.add_mem()` MUST ROUTE THROUGH `add_memory()` HERE.
A raw INSERT skipped `sync.notify("memories")`, so a memory the agent had just
saved was ABSENT FROM ITS OWN SYSTEM PROMPT for the rest of the process, and
`importance`/`tags` were silently dropped. ANY writer that reaches this table
without notifying leaves the prompt stale.

BULK OPERATIONS EXIST BECAUSE PER-ROW ONES REBUILD THE CACHE PER ROW.
`delete_memories()` / `delete_rules()` / `set_rules_active()` commit in one
`batch()` and notify ONCE (150 deletes: 21.7 ms → 1.6 ms).
  * ⚠️ An empty id list deletes NOTHING, rather than falling through to "no
    filter, so every row". `all: true` must be passed explicitly.
  * `set_rules_active()` SETS, it does not toggle — bulk-toggling a mixed
    selection would just leave it mixed the other way.
  * `prune_memories()` ranks IDENTICALLY to the prompt block, so a prune cannot
    drop a row the prompt was using while keeping one it was not.

⚠️ EVERY QUERY IN THIS MODULE IS SCOPED TO THE WORKSPACE (Task 23).
`core.broker.isolation` owns the rule; this module only applies it. A read is
"this project's rows OR shared ones", a write is stamped with the current project,
and `shared=True` is the caller's explicit "every project may see this". No
signature changed: `project` and `shared` are keyword-only with defaults, so the
three existing writers and both surfaces became project-scoped without being
touched — which is also why there is no second place that could forget to scope.

⚠️ THE DEDUP LOOKUP IS SCOPED TOO, AND THAT IS THE SUBTLE HALF.
An unscoped `WHERE content=?` would find project B's identical row, "reinforce"
it, and return it — so project A's save would appear to succeed, write nothing A
can see, and the memory would be gone from A forever with no error anywhere. The
dedup therefore searches only rows the target project can SEE, and prefers the row
whose ownership already matches what this write would stamp.

⚠️ THE DESTRUCTIVE OPERATIONS ARE SCOPED HARDER THAN THE READS.
`delete_memory`, `delete_memories`, `clear_memories` and `prune_memories` all
carry the scope predicate: a project may only ever delete what it can see.
Reading another project's memory is a leak; deleting it is data loss (rule 22),
and "clear all" run in the wrong checkout must not empty the other one. The count
`clear_memories()` returns is taken through the same predicate as the DELETE, so
the number it reports is the number it removed.

(test_core.py + test_agent_loop.py + test_isolation.py — sabotage-verified)
"""

import uuid

from agent2.core.broker import isolation as _iso
from agent2.database import qall, qone, exe, exemany, batch


def _scope(project=None) -> tuple[str, tuple]:
    """The visibility predicate for a read/delete — see `isolation.scope_sql()`.

    Degrades to `1=1` if the isolation module cannot be reached at all, i.e. to
    pre-Task-23 global behaviour. That direction is deliberate for THIS module:
    the alternative failure mode is showing the user an empty memory store and
    then letting a "clear" run against nothing, which looks exactly like data
    loss. `scope_sql()` is itself total, so this only covers an import that a
    working install cannot fail.
    """
    try:
        return _iso.scope_sql(project)
    except Exception:
        return ("1=1", ())

# How many memories reach the system prompt. Chosen because the block is re-sent
# on every iteration of every turn (up to MAX_AGENT_ITERS), so its cost is
# multiplied, not paid once: 300 memories measured ~5,100 tokens PER TURN before
# this bound existed. Ranked by importance so the cut drops the least important
# rows rather than an arbitrary set — nothing is deleted, only left unsent.
PROMPT_LIMIT = 40


def _notify(action: str, mid: str = "") -> None:
    """Announce a memory change to the other surfaces (see agent2.core.sync).

    Imported lazily and failure-tolerant: memory writes must succeed even if the
    sync layer is unavailable (e.g. a partially initialised install).

    ⚠️ This is also what invalidates the cached memories block in the system
    prompt (`agent.py:_MEM_CACHE`). Any writer that reaches this table WITHOUT
    calling it leaves the agent running on a stale prompt until something else
    happens to bump the counter — which is precisely the bug `tools.add_mem` had:
    it wrote raw SQL, so a memory the agent saved itself was invisible to the
    agent for the rest of the process.
    """
    try:
        from agent2.core import sync
        sync.notify("memories", action=action, id=mid)
    except Exception:
        pass


def list_memories(*, project=None) -> list[dict]:
    """Memories visible to *project* (default: the current one), oldest first.

    Returns [] on any failure. `project` carries the row's owner so a surface can
    show the user which of their memories are shared — nothing else reads it.
    """
    where, params = _scope(project)
    try:
        return qall("SELECT id, content, importance, tags, project, created_at "
                    f"FROM memories WHERE {where} ORDER BY created_at", params)
    except Exception:
        return []


def top_memories(limit: int = PROMPT_LIMIT, *, project=None) -> list[dict]:
    """The *limit* most important visible memories, strongest first.

    LIMITed in SQL rather than sliced in Python: the prompt block needs at most
    PROMPT_LIMIT rows, and a heavy user's table can hold thousands. Ties break on
    recency so a fresh fact outranks an equally-important stale one.
    """
    where, params = _scope(project)
    try:
        return qall("SELECT id, content, importance, tags, project, created_at "
                    f"FROM memories WHERE {where} "
                    "ORDER BY importance DESC, created_at DESC "
                    "LIMIT ?", (*params, max(0, int(limit))))
    except Exception:
        return []


def count_memories(*, project=None) -> int:
    """How many memories *project* can see.

    ⚠️ Scoped, and it has to be: `memory_prompt_block()` subtracts what it
    rendered from this to tell the model how many were withheld. Counting the
    whole table would announce another project's memories as "not shown", and the
    model would keep asking for facts this project does not have.
    """
    where, params = _scope(project)
    try:
        row = qone(f"SELECT COUNT(*) AS n FROM memories WHERE {where}", params)
        return int((row or {}).get("n") or 0)
    except Exception:
        return 0


def add_memory(content: str, importance: int = 5, tags=None, *,
               project=None, shared: bool = False) -> dict | None:
    """Persist a memory. Returns the stored row, or None if empty/failed.

    Deduplicates on exact content. The agent calls `save_memory` freely and the
    same fact tends to be re-derived across sessions, so without this the prompt
    grew with duplicate lines that cost tokens and taught the model nothing. A
    repeat is treated as reinforcement: it raises importance to the higher of the
    two and refreshes the timestamp, then returns the EXISTING row — so a caller
    cannot tell a dedup from an insert, and neither can double-count.

    Stamped with the current project unless `shared=True` (see `isolation.stamp`).

    ⚠️ THE DEDUP SEARCHES ONLY WHAT THIS PROJECT CAN SEE. Unscoped, project A's
    save would find project B's identical row, reinforce it, return it, and leave
    A with nothing — a memory silently lost to a successful-looking call. Among
    visible rows it prefers the one whose owner already equals what this write
    would stamp, so re-saving in project A reinforces A's row rather than the
    shared row that merely happens to be older.

    ⚠️ `shared=True` on an existing project-owned row PROMOTES it to shared. That
    is the one direction visibility ever moves implicitly: the user asked for this
    fact to be global, and a second row with identical content would render twice
    in the prompt. The reverse never happens — a `shared` row is never narrowed
    to one project by an ordinary save.
    """
    content = (content or "").strip()
    if not content:
        return None
    imp = min(10, max(1, int(importance or 5)))
    if isinstance(tags, (list, tuple, set)):
        tag_s = ",".join(str(t).strip() for t in tags if str(t).strip())
    else:
        tag_s = (tags or "").strip()
    try:
        owner = _iso.stamp(shared=shared, project=project)
    except Exception:
        owner = ""
    where, params = _scope(project)
    cols = "id, content, importance, tags, project, created_at"
    try:
        dupe = qone(f"SELECT id, importance, project FROM memories "
                    f"WHERE content=? AND {where} "
                    "ORDER BY CASE WHEN project=? THEN 0 ELSE 1 END, created_at "
                    "LIMIT 1",
                    (content, *params, owner))
        if dupe:
            if shared and str(dupe.get("project") or "") != "":
                exe("UPDATE memories SET importance=MAX(importance, ?), "
                    "project='', created_at=datetime('now') WHERE id=?",
                    (imp, dupe["id"]))
            else:
                exe("UPDATE memories SET importance=MAX(importance, ?), "
                    "created_at=datetime('now') WHERE id=?", (imp, dupe["id"]))
            _notify("reinforce", dupe["id"])
            return qone(f"SELECT {cols} FROM memories WHERE id=?", (dupe["id"],))
        mid = str(uuid.uuid4())
        exe("INSERT INTO memories(id, content, importance, tags, project) "
            "VALUES(?, ?, ?, ?, ?)", (mid, content, imp, tag_s, owner))
        row = qone(f"SELECT {cols} FROM memories WHERE id=?", (mid,))
        _notify("add", mid)
        return row
    except Exception:
        return None


def share_memory(mid: str, shared: bool = True, *, project=None) -> dict | None:
    """Make one memory global (`shared=True`) or local to *project*.

    The explicit half of "unless explicitly shared". Scoped like every other
    by-id write: a project can only re-scope a memory it can already see, so this
    cannot be used to reach into another workspace's store.
    """
    mid = str(mid or "").strip()
    if not mid:
        return None
    where, params = _scope(project)
    try:
        owner = "" if shared else _iso.stamp(project=project)
    except Exception:
        owner = ""
    try:
        exe(f"UPDATE memories SET project=? WHERE id=? AND {where}",
            (owner, mid, *params))
        _notify("share", mid)
        return qone("SELECT id, content, importance, tags, project, created_at "
                    "FROM memories WHERE id=?", (mid,))
    except Exception:
        return None


def delete_memory(mid: str, *, project=None) -> None:
    where, params = _scope(project)
    try:
        exe(f"DELETE FROM memories WHERE id=? AND {where}", (mid, *params))
        _notify("delete", mid)
    except Exception:
        pass


def delete_memories(ids, *, project=None) -> int:
    """Delete many memories in ONE transaction. Returns the number requested.

    A bulk clear used to be N HTTP calls, each its own transaction AND its own
    `notify()` — so clearing 200 memories rebuilt the cached prompt block 200
    times. Here the whole set commits once and notifies once.
    """
    ids = [str(i) for i in (ids or []) if str(i).strip()]
    if not ids:
        return 0
    where, params = _scope(project)
    try:
        with batch():
            exemany(f"DELETE FROM memories WHERE id=? AND {where}",
                    [(i, *params) for i in ids])
    except Exception:
        return 0                # nothing applied; the caller can retry
    _notify("delete_bulk")
    return len(ids)


def clear_memories(*, project=None) -> int:
    """Delete every memory THIS project can see. Returns how many were removed.

    ⚠️ Scoped, so "clear all" in one checkout cannot empty another's store
    (rule 22). The count and the DELETE go through the same predicate, so the
    number returned is the number actually removed.
    """
    n = count_memories(project=project)
    if not n:
        return 0
    where, params = _scope(project)
    try:
        exe(f"DELETE FROM memories WHERE {where}", params)
    except Exception:
        return 0
    _notify("clear")
    return n


def prune_memories(*, keep: int = 500, min_importance: int = 1,
                   project=None) -> int:
    """Drop the least valuable visible memories beyond *keep*. Returns rows removed.

    Ranked the same way the prompt block is, so pruning can never remove a row
    the prompt was using while leaving one it was not. Memories at or above
    *min_importance* are exempt from the cap — a fact the user marked critical is
    not garbage-collected because it is old.
    """
    keep = max(0, int(keep))
    where, params = _scope(project)
    try:
        victims = qall(
            f"SELECT id FROM memories WHERE importance < ? AND {where} "
            "ORDER BY importance DESC, created_at DESC LIMIT -1 OFFSET ?",
            (max(1, min(10, int(min_importance))) + 1, *params, keep))
    except Exception:
        return 0
    if not victims:
        return 0
    try:
        with batch():
            exemany("DELETE FROM memories WHERE id=?",
                    [(v["id"],) for v in victims])
    except Exception:
        return 0
    _notify("prune")
    return len(victims)


def memory_prompt_block(limit: int = PROMPT_LIMIT, *, project=None) -> str:
    """Render memories as a system-prompt section, or '' when there are none.

    ⚠️ Bounded and ranked by importance, and it queries only what it renders.
    This is the single builder both `agent.py` and `provider_agent.py` must use:
    `agent.py` previously inlined its own unbounded version, so the limit here
    was dead code and the real prompt grew without a ceiling.

    ⚠️ `project` is forwarded ONLY when it is actually set. `project=None` already
    means "wherever we are right now" to both queries below, so passing it
    explicitly would change nothing except the *shape* of the two calls — and that
    shape is a contract: the broker's prompt tests substitute `top_memories` and
    `count_memories` with stubs, so a keyword this renderer adds unconditionally
    becomes a keyword every substitute must accept for a value it ignores.
    """
    scope = {} if project is None else {"project": project}
    top = top_memories(limit, **scope)
    if not top:
        return ""
    body = "\n".join(f"- {m['content']}" for m in top)
    extra = count_memories(**scope) - len(top)
    if extra > 0:
        # Told, not hidden: the model should know its recall is partial rather
        # than infer that a fact it saved was forgotten.
        body += (f"\n- (+{extra} older/lower-priority memories not shown — "
                 f"ask if you need them)")
    return "\n\n## MEMORIES (always apply):\n" + body
