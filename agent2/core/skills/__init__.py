# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2.core.skills — the Skills subsystem (Phase 11, Tasks 32–36)
─────────────────────────────────────────────────────────────────
THE facade. Four modules, one entry point per surface, and no second path to any
of them.

    discovery.py   what is on disk        (Task 32 — no cross-project leakage)
    normalize.py   what the file MEANS    (Task 33 — Claude / Codex / Antigravity)
    select.py      what this turn gets    (Tasks 34 + 36 — order, conflicts, report)
    state.py       what the user chose    (Task 35 — per-project, never a file edit)

⚠️ **SKILLS ARE THE EXTENSIBILITY MECHANISM, AND THERE IS NO PLUGIN ARCHITECTURE
HERE.** That is the user's own constraint, stated twice: *do not build a universal
plugin architecture — create a skill loading/normalization layer instead.* So
nothing in this package imports a third-party module, executes a discovered file,
evaluates a discovered expression, or registers a hook a skill file could name. A
skill is **text that reaches a prompt**. The most a malicious `SKILL.md` can do is
say something; the most a malformed one can do is be skipped and reported. Every
later phase that wants "extensible" wants this, not an import path.

⚠️ **`for_turn()` IS THE ONE TURN-PATH ENTRY POINT.** Four agent loops assemble a
prompt (`agent.py`, `llm/provider_agent.py`, and both CLI loops), and each reaches
skills through the Context Broker's `skills` collector, which calls this function
and nothing else. A surface that called `discover()` + `choose()` itself would be a
second selection: same inputs, drifting caps, and a `/skills` report describing a
prompt that surface never sent. That is the failure `broker.ORDER` and
`ContextBundle.sent()` are both shaped to prevent, one layer up.

⚠️ **NOTHING HERE MAY RAISE INTO A TURN.** Skills are an *addition* to the prompt,
so every degradation is "the prompt is what it was before Phase 11": no folder, an
unreadable file, a locked database and a broken normalizer all end as an empty
selection. Each of the four modules carries its own written BLE001/S110 exemption
saying what it degrades to.

⚠️ **`last_applied()` IS PER PROCESS, AND SAYS SO.** Dual mode is two processes over
one `agent2.db`, so "the last selection" is this process's last selection — the same
honesty `DiffStore` and `core.metrics` are documented with. The durable half is
`alog.skills_applied()`, which is written once per turn on whichever surface ran it.
"""

import threading

from agent2 import config
from agent2.core.skills.discovery import (
    SKILLS_TTL,
    Catalog,
    Skill,
    discover,
    invalidate,
    skills_root,
)
from agent2.core.skills.normalize import (
    ALIASES,
    CANONICAL,
    MANIFESTS,
    ORIGINS,
    normalize,
    split_frontmatter,
)
from agent2.core.skills.select import (
    HEADING,
    REASON_LABEL,
    REASONS,
    Applied,
    Selection,
    choose,
    prompt_block,
    relevance,
)
from agent2.core.skills import state

__all__ = [
    "ALIASES",
    "CANONICAL",
    "HEADING",
    "MANIFESTS",
    "ORIGINS",
    "REASONS",
    "REASON_LABEL",
    "SKILLS_TTL",
    "Applied",
    "Catalog",
    "Selection",
    "Skill",
    "available",
    "choose",
    "describe",
    "discover",
    "for_turn",
    "invalidate",
    "last_applied",
    "normalize",
    "prompt_block",
    "relevance",
    "skills_root",
    "split_frontmatter",
    "state",
    "stats",
    "toggle",
]


_last_lock = threading.RLock()
_last: dict = {}
_turns = 0


def enabled() -> bool:
    """Whether the subsystem does anything at all (`AGENT2_SKILLS`).

    Read live from `config`, not latched, so a test — and an operator — sees the
    value in force rather than the one set when this module was first imported.
    """
    return bool(config.SKILLS_ENABLED)


def available(*, force: bool = False) -> Catalog:
    """Every skill in this project, for a surface that wants to LIST them.

    Deliberately named apart from `for_turn`: this is the read `/skills` and
    `GET /api/skills` make, and it applies no selection at all — a report must be
    able to show a skill that this turn's message did not select, which is the
    question a user asks when their skill did not fire.
    """
    return discover(force=force)


def for_turn(message: str = "", *, force: bool = False) -> Selection:
    """THE selection for one turn: discover → read the user's choices → choose.

    Total. `force` bypasses the discovery TTL and exists for `/skills` after a
    toggle, never for the turn path — paying a tree walk per agent iteration is
    the cost `broker.base_tail()`'s docstring exists to prevent.

    ⚠️ The audit line is written HERE and only here, for the same reason
    `budget.apply()` owns its trim log: one turn produces one record, and a caller
    that had to remember to log is a caller that eventually does not.
    """
    global _turns
    if not enabled():
        return Selection()
    try:
        cat = discover(force=force)
    except Exception:  # noqa: BLE001 — degrades to no skills; see the module docstring
        return Selection()
    try:
        chosen = state.states()
    except Exception:  # noqa: BLE001 — an unreadable choice reads as "never chosen"
        chosen = {}
    try:
        sel = choose(cat, message, chosen)
    except Exception:  # noqa: BLE001 — a selection that cannot be computed is empty
        return Selection()
    with _last_lock:
        _turns += 1
        _last.clear()
        _last.update(sel.to_payload())
    try:
        from agent2.core import logging as alog
        alog.skills_applied(
            applied=",".join(sel.ids) or "-",
            reasons=",".join(a.reason for a in sel.applied) or "-",
            considered=sel.considered, omitted=len(sel.omitted),
            chars=sel.chars, truncated=sel.catalog_truncated,
        )
    except Exception:  # noqa: BLE001, S110 — the selection stands even unlogged
        pass
    return sel


def block(message: str = "") -> str:
    """The `## PROJECT SKILLS` prompt text for this turn, or `""`.

    The convenience the broker's collector uses. It is one call rather than two so
    that "assemble a selection" and "render it" can never be done by different
    callers with different arguments.
    """
    return prompt_block(for_turn(message))


def toggle(word: str, enabled_: bool | None) -> tuple[bool, str]:
    """Turn the skill named *word* on / off / back to never-chosen.

    Returns `(ok, message)` — never raises, and never writes to a skill file.
    Resolution is `Catalog.find()`, so an ambiguous prefix is refused with the same
    words on both surfaces rather than resolved differently by each.
    """
    cat = discover()
    sk = cat.find(word)
    if sk is None:
        if not cat.exists:
            return (False, f"no skills folder yet — run /init to create {cat.root or '.agent2/skills'}")
        return (False, f"no single skill matches {word!r} "
                       f"({len(cat.skills)} known; try /skills list)")
    ok = state.set_enabled(sk.id, enabled_)
    word_for = {True: "enabled", False: "disabled", None: "reset"}[enabled_]
    if not ok:
        return (False, f"could not record {word_for} for {sk.name}")
    return (True, f"{sk.name} {word_for} for this project")


def last_applied() -> dict:
    """The most recent selection THIS PROCESS made. `{}` before the first turn.

    ⚠️ Per process by design — see the module docstring. An empty dict means "no
    turn has been assembled here", which is a different fact from "no skills
    applied" (that is a populated dict with an empty `applied`).
    """
    with _last_lock:
        return dict(_last)


def stats() -> dict:
    """Counters for `/api/health`, `/api/skills` and `describe()`. No skill text."""
    cat = discover()
    with _last_lock:
        turns, last = _turns, dict(_last)
    from agent2.core.skills import discovery as _disc
    return {
        "enabled": enabled(),
        "root": cat.root,
        "exists": cat.exists,
        "count": len(cat.skills),
        "truncated": cat.truncated,
        "truncated_by": cat.truncated_by,
        "errors": len(cat.errors),
        "skipped": cat.skipped,
        "turns": turns,
        "last_applied": len(last.get("applied") or []),
        "scope": "this process",
        "cache": _disc.stats(),
        "state": state.describe(),
    }


def describe() -> dict:
    """The policy and the vocabulary — what a surface needs to explain itself.

    Reasons, origins and ceilings, so `/skills` and the web panel print one set of
    words. ⚠️ Never a skill body and never a project path beyond the root the user
    already typed `/init` in.
    """
    return {
        "enabled": enabled(),
        "reasons": list(REASONS),
        "labels": dict(REASON_LABEL),
        "origins": [name for name, _ in ORIGINS],
        "manifests": list(MANIFESTS),
        "fields": list(CANONICAL),
        "limits": {
            "max": config.SKILLS_MAX,
            "max_depth": config.SKILLS_MAX_DEPTH,
            "max_bytes": config.SKILLS_MAX_BYTES,
            "budget_sec": config.SKILLS_SCAN_BUDGET_SEC,
            "in_prompt": config.SKILLS_IN_PROMPT,
            "max_chars": config.SKILLS_MAX_CHARS,
        },
        "state": state.describe(),
    }
