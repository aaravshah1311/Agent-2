# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2.core.rules
──────────────────
THE single source of truth for custom rules. Both the Web UI and the CLI
import these functions — no interface may re-implement rules logic locally.

Backed by the `rules` table in agent2.db (see agent2.database).

⚠️ EVERY QUERY HERE IS SCOPED TO THE WORKSPACE (Task 23).
`core.broker.isolation` owns what the scope MEANS; this module only applies it,
exactly as `core.memory` does — a read is "this project's rules OR shared ones", a
write is stamped with the current project, and `shared=True` is the caller's
explicit "every project follows this". `project` and `shared` are keyword-only
with defaults, so `/rules`, the three REST handlers and both prompt builders
became project-scoped without a single call site changing.

⚠️ A RULE IS AN INSTRUCTION THE MODEL OBEYS, WHICH MAKES LEAKING ONE WORSE THAN
LEAKING A MEMORY. A memory from another checkout is a wrong fact the model may
mention; a rule from another checkout silently changes how it behaves here —
"always run the deploy script after editing" belonging to a different project is
an instruction nobody in this one ever wrote. So the by-id writes (`toggle_rule`,
`delete_rule`, `delete_rules`, `set_rules_active`) carry the predicate too: a
project may only ever touch a rule it can see.

⚠️ EVERY PRE-TASK-23 RULE IS `''` — SHARED — AND STAYS ACTIVE EVERYWHERE.
The column default does that (migration 18). The alternative, assigning existing
rules to whichever project was open during the upgrade, would have quietly
stopped enforcing a user's standing instructions in every other checkout, and a
rule that silently stops applying is indistinguishable from a model that ignored
it (rules 8 and 22).

(test_core.py + test_isolation.py — sabotage-verified)
"""

import uuid

from agent2.core.broker import isolation as _iso
from agent2.database import qall, qone, exe, exemany, batch


def _scope(project=None) -> tuple[str, tuple]:
    """The visibility predicate for a read/write — see `isolation.scope_sql()`.

    Degrades to `1=1` (pre-Task-23 global behaviour) if the isolation module
    cannot be imported at all. For rules the failure direction is the same as for
    memories: keep obeying the user's standing instructions rather than silently
    dropping every one of them.
    """
    try:
        return _iso.scope_sql(project)
    except Exception:
        return ("1=1", ())


def _notify(action: str, rid: str = "") -> None:
    """Announce a rule change to the other surfaces (see agent2.core.sync).

    Rules feed the system prompt, so this bump is also what invalidates the
    cached prompt in agent.py — a rule toggled in the browser takes effect on
    the CLI's very next turn.
    """
    try:
        from agent2.core import sync
        sync.notify("rules", action=action, id=rid)
    except Exception:
        pass


def list_rules(active_only: bool = False, *, project=None) -> list[dict]:
    """Rules visible to *project*, newest first. active_only=True for the prompt."""
    where, params = _scope(project)
    try:
        if active_only:
            return qall("SELECT id, content, active, project, created_at FROM rules "
                        f"WHERE active=1 AND {where} ORDER BY created_at", params)
        return qall("SELECT id, content, active, project, created_at FROM rules "
                    f"WHERE {where} ORDER BY created_at DESC", params)
    except Exception:
        return []


def add_rule(content: str, *, project=None, shared: bool = False) -> dict | None:
    """Store a rule, owned by the current project unless *shared*."""
    content = (content or "").strip()
    if not content:
        return None
    try:
        owner = _iso.stamp(shared=shared, project=project)
    except Exception:
        owner = ""
    try:
        rid = str(uuid.uuid4())
        exe("INSERT INTO rules(id, content, project) VALUES(?, ?, ?)",
            (rid, content, owner))
        row = qone("SELECT id, content, active, project, created_at "
                   "FROM rules WHERE id=?", (rid,))
        _notify("add", rid)
        return row
    except Exception:
        return None


def share_rule(rid: str, shared: bool = True, *, project=None) -> dict | None:
    """Make one rule global (`shared=True`) or local to *project*.

    The explicit half of "unless explicitly shared", and scoped like every other
    by-id write — a project can only re-scope a rule it can already see.
    """
    rid = str(rid or "").strip()
    if not rid:
        return None
    where, params = _scope(project)
    try:
        owner = "" if shared else _iso.stamp(project=project)
    except Exception:
        owner = ""
    try:
        exe(f"UPDATE rules SET project=? WHERE id=? AND {where}",
            (owner, rid, *params))
        _notify("share", rid)
        return qone("SELECT id, content, active, project, created_at "
                    "FROM rules WHERE id=?", (rid,))
    except Exception:
        return None


def toggle_rule(rid: str, *, project=None) -> dict | None:
    """Flip one rule's active flag and return the row it left behind.

    Returning the row is what lets `PUT /api/rules/<rid>` answer without a raw
    `SELECT * FROM rules` of its own: that query was unscoped, so a toggle this
    module correctly refused still handed the caller the other project's rule.
    Returns None when the rule is not visible here.
    """
    where, params = _scope(project)
    try:
        exe(f"UPDATE rules SET active = 1 - active WHERE id=? AND {where}",
            (rid, *params))
        _notify("toggle", rid)
        return qone("SELECT id, content, active, project, created_at "
                    f"FROM rules WHERE id=? AND {where}", (rid, *params))
    except Exception:
        return None


def delete_rule(rid: str, *, project=None) -> None:
    where, params = _scope(project)
    try:
        exe(f"DELETE FROM rules WHERE id=? AND {where}", (rid, *params))
        _notify("delete", rid)
    except Exception:
        pass


def delete_rules(ids, *, project=None) -> int:
    """Delete many rules in ONE transaction, with ONE cache invalidation.

    Rules feed the system prompt, so deleting them one at a time rebuilt that
    cached block once per rule.
    """
    ids = [str(i) for i in (ids or []) if str(i).strip()]
    if not ids:
        return 0
    where, params = _scope(project)
    try:
        with batch():
            exemany(f"DELETE FROM rules WHERE id=? AND {where}",
                    [(i, *params) for i in ids])
    except Exception:
        return 0                # nothing applied; the caller can retry
    _notify("delete_bulk")
    return len(ids)


def set_rules_active(ids, active: bool, *, project=None) -> int:
    """Activate/deactivate many rules at once.

    `toggle_rule` flips each rule individually, so "turn all of these off" over N
    rules was N transactions AND N prompt rebuilds — and flipping is the wrong
    primitive for it anyway: a mixed selection ends up mixed the other way rather
    than uniformly off.
    """
    ids = [str(i) for i in (ids or []) if str(i).strip()]
    if not ids:
        return 0
    flag = 1 if active else 0
    where, params = _scope(project)
    try:
        with batch():
            exemany(f"UPDATE rules SET active=? WHERE id=? AND {where}",
                    [(flag, i, *params) for i in ids])
    except Exception:
        return 0
    _notify("set_active")
    return len(ids)


def rules_prompt_block(*, project=None) -> str:
    """Render active rules as a system-prompt section, or '' when there are none.

    ⚠️ `project` is forwarded only when set, for the same reason
    `memory.memory_prompt_block` does it: `None` already means "here", and the
    unconditional keyword would otherwise become a requirement on every stub the
    prompt tests substitute for `list_rules`.
    """
    rules = list_rules(active_only=True, **({} if project is None else {"project": project}))
    if not rules:
        return ""
    body = "\n".join(f"- {r['content']}" for r in rules)
    return "\n\n## CUSTOM RULES (follow strictly):\n" + body
