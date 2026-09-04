# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/core/broker/sources.py — THE source table (Task 21)
──────────────────────────────────────────────────────────
Which context sources exist, and what each one *tracks*: `source`, `priority`,
`relevance`, `token_cost`, `freshness`.

Task 20 gave the broker composition — which sources there are and in what order
they render. That is not enough to decide what to leave out: a prompt has a token
ceiling, and "drop the last thing collected" is not a policy. So every item
carries four measures, and this module is the one place that assigns them.

⚠️ `ORDER` IS WHERE A SOURCE PRINTS. `PRIORITY` IS WHAT SURVIVES A BUDGET.
They are different facts and they deliberately disagree: memory and rules render
**last** (the prompt should end on the user's standing orders) and rank **first**
(they are the last thing that may ever be dropped). Deriving one from the other
would force a choice between a prompt that ends on git statistics and a budget
that discards the user's rules to keep them.

⚠️ THE MEASURES ARE ASSIGNED IN ONE PLACE — `measure()`, called from `collect()`.
Not in the collectors. Ten collectors each computing their own token cost is ten
chances to count differently, and a source registered by a later phase (skills,
workflows) would arrive with no measures at all and silently rank last. A
collector that genuinely knows better — a skill that computed its own relevance
from the request — may pre-set a field, and `measure()` never overwrites a value
that is already there. `None` means "not stated"; that is why the four fields
default to `None` rather than to 0.

⚠️ `token_cost` DELEGATES TO `llm.router.estimate_tokens()` AND MAY NOT RE-DERIVE.
That function is what the router already uses to decide whether a turn needs a
long-context model. A second estimator here would mean the broker trims to fit a
budget measured one way while the router picks the model using another — and the
disagreement is invisible, because each half is self-consistent. The `len // 4`
fallback exists only for the case where the import itself fails, in which case
there is no other number to disagree with.

⚠️ UNKNOWN IS NOT ZERO. With no user message there is nothing to judge topical
relevance against, so `relevance_of()` returns `UNKNOWN_RELEVANCE`, not 0.0 —
the same three-value discipline `llm/capabilities.py` documents at length. A
source scored 0.0 would be the first thing a budget discarded, on the strength of
a question nobody asked.

Everything here is total: a measure that cannot be computed falls back to the
table default rather than raising, because these functions run on the turn path
and a broken measure must cost at most a bad ordering, never a turn.
"""

from __future__ import annotations

import time

# ── Source kinds ───────────────────────────────────────────────────────────────
# ⚠️ THE NAMES LIVE HERE AND `broker/__init__.py` RE-EXPORTS THEM, so
# `broker.SOURCE_MEMORY` and `sources.SOURCE_MEMORY` are the same string object
# and there is one spelling of each name. They appear in payloads, in `/api`
# output and in tests, so a typo in a second copy would register a source nobody
# collects — and look right in both files.
SOURCE_CONVERSATION = "conversation"
SOURCE_PROJECT      = "project"
SOURCE_GIT          = "git_state"
SOURCE_SKILLS       = "skills"
SOURCE_TASKS        = "task_results"
SOURCE_WORKFLOW     = "workflow_state"
SOURCE_MCP          = "mcp_results"
SOURCE_FILES        = "files"
SOURCE_MEMORY       = "memory"
SOURCE_RULES        = "rules"

# ⚠️ RENDER ORDER — THE ONE DECLARATION. Situational context first, standing
# instructions last, and MEMORY BEFORE RULES (pinned by test_agent_loop.py).
# `conversation` is first and renders nothing: it is the turn history, which the
# caller already holds, so its slot exists for accounting only.
ORDER: tuple[str, ...] = (
    SOURCE_CONVERSATION,
    SOURCE_PROJECT,
    SOURCE_GIT,
    SOURCE_SKILLS,
    SOURCE_TASKS,
    SOURCE_WORKFLOW,
    SOURCE_MCP,
    SOURCE_FILES,
    SOURCE_MEMORY,
    SOURCE_RULES,
)

# Sources that are part of every prompt whether or not a caller assembled a
# bundle. They are the user's standing instructions, so "we ran out of budget" is
# never an acceptable reason to drop them (Task 22 pins them for that reason).
ALWAYS: frozenset[str] = frozenset((SOURCE_MEMORY, SOURCE_RULES))


# ── Priority: what survives when the budget is tight ───────────────────────────
# Higher is kept longer. The scale is relative to this table and means nothing
# outside it (the same honesty `llm/capabilities.py` applies to its tiers).
#
# ⚠️ EVERY SOURCE IN `ORDER` HAS AN ENTRY, and every `ALWAYS` source outranks
# every droppable one — both pinned by tests. The second rule is what makes
# "pinned" and "high priority" agree instead of being two contradictory opinions
# about the same item; a table that ranked `git_state` above `rules` would let a
# reader of the numbers alone conclude the wrong thing about what is safe to cut.
PRIORITY: dict[str, int] = {
    SOURCE_RULES:        100,   # the user's standing orders
    SOURCE_MEMORY:        95,   # what the user told us to remember
    SOURCE_CONVERSATION:  90,   # the turn itself; dropping it is not a trim
    SOURCE_PROJECT:       80,   # this project's own instructions
    SOURCE_TASKS:         70,   # the plan — without it completed work gets redone
    SOURCE_SKILLS:        60,   # Phase 11
    SOURCE_WORKFLOW:      50,   # Phase 12
    SOURCE_MCP:           45,   # small, and prevents invented tool calls
    SOURCE_FILES:         40,   # recoverable: the agent can re-read a diff
    SOURCE_GIT:           30,   # cheapest to re-derive with one command
}

# An unregistered source (a third-party collector) ranks below everything named
# above rather than above it: unknown provenance is not a reason to outrank the
# user's rules.
DEFAULT_PRIORITY = 10

# Relevance floors. `UNKNOWN_RELEVANCE` is returned when there is no message to
# compare against; `BASE_RELEVANCE` is the floor for a source that shares no
# vocabulary with the request but is still describing the world the agent is
# acting in — zero would mean "provably irrelevant", which lexical overlap is far
# too crude to establish.
UNKNOWN_RELEVANCE = 0.5
BASE_RELEVANCE = 0.3

# Words too common to say anything about relevance. Deliberately tiny: this is a
# stop-list, not a language model, and a long one starts making decisions of its
# own that nobody can see in the output.
_STOP: frozenset[str] = frozenset((
    "the", "and", "for", "with", "this", "that", "you", "your", "are", "was",
    "were", "have", "has", "had", "not", "but", "can", "will", "would", "should",
    "from", "into", "when", "what", "why", "how", "please", "make", "does", "did",
    "all", "any", "its", "our", "their", "then", "than", "there", "here", "just",
))

_MIN_WORD = 3


def _words(text: str) -> set[str]:
    """Lowercased significant words. Total; a non-string yields an empty set."""
    out: set[str] = set()
    try:
        cur: list[str] = []
        for ch in str(text or "").lower():
            if ch.isalnum() or ch in "_-.":
                cur.append(ch)
            elif cur:
                w = "".join(cur).strip("-._")
                if len(w) >= _MIN_WORD and w not in _STOP:
                    out.add(w)
                cur = []
        if cur:
            w = "".join(cur).strip("-._")
            if len(w) >= _MIN_WORD and w not in _STOP:
                out.add(w)
    except Exception:
        return out
    return out


def priority_of(source: str) -> int:
    """This source's base priority. Total — an unknown source gets the default."""
    try:
        return int(PRIORITY.get(str(source), DEFAULT_PRIORITY))
    except Exception:
        return DEFAULT_PRIORITY


def ttl_for(source: str) -> float:
    """Seconds this source's data stays fully fresh. `0.0` = read live every turn.

    ⚠️ GIT IS THE ONLY SOURCE THAT CAN SERVE DATA IT READ EARLIER, and its window
    is read **live** from `gitstate.GIT_TTL` rather than copied here. A literal
    `15.0` in this file would be a second declaration of that window, and the
    failure is quiet: raise the TTL in `gitstate` and the broker keeps reporting a
    two-minute-old snapshot as perfectly fresh.

    Every other collector reads its facts during `collect()`, so their data is as
    fresh as the turn. A later phase that adds a cached source either adds a branch
    here or has its collector state `freshness` itself.
    """
    try:
        if str(source) == SOURCE_GIT:
            from agent2.core import gitstate as _git
            return max(0.0, float(_git.GIT_TTL))
    except Exception:
        return 0.0
    return 0.0


def token_cost(text: str) -> int:
    """Estimated prompt tokens — through `llm.router.estimate_tokens`, always.

    See the module note: the router uses this same number to decide whether a turn
    needs a long-context model, so a second estimator would let the broker trim to
    one budget while the model was chosen against another.
    """
    try:
        from agent2.llm import router as _router
        return max(0, int(_router.estimate_tokens(text)))
    except Exception:
        # Only reachable if the import fails, and then there is no other estimate
        # in play to disagree with. Same arithmetic, stated once, on purpose.
        try:
            return max(0, len(str(text or "")) // 4)
        except Exception:
            return 0


def freshness_of(source: str, stamp: float = 0.0, *, now: float | None = None) -> float:
    """How current this item's data is, in `0.0 … 1.0`.

    `stamp` is when the underlying data was **read** (wall clock, as
    `gitstate.snapshot()["at"]` reports it). `0.0` means "read now", which is the
    honest answer for every collector that queries live — so freshness is 1.0
    unless a source explicitly says it served something older.

    Decays linearly across the source's TTL and never below 0.0. A stamp in the
    future (clock skew, a restored snapshot) reads as fresh rather than as an
    error; there is nothing useful to do with a negative age.
    """
    try:
        ttl = ttl_for(source)
        if ttl <= 0.0 or not stamp:
            return 1.0
        age = float(now if now is not None else time.time()) - float(stamp)
        if age <= 0.0:
            return 1.0
        return max(0.0, min(1.0, 1.0 - (age / ttl)))
    except Exception:
        return 1.0


def relevance_of(source: str, text: str, message: str = "", *,
                 pinned: bool = False) -> float:
    """Topical relevance of this item to *message*, in `0.0 … 1.0`.

    ⚠️ PINNED AND ALWAYS-ON SOURCES ARE 1.0 BY DEFINITION, not by measurement.
    Memories, rules and the conversation are not *about* the request — they are the
    standing frame the request is answered in. Scoring them by word overlap would
    rank the user's own rules as irrelevant to most questions, which is exactly the
    reasoning a budget must never be able to make.

    Everything else is deterministic lexical overlap: the share of the request's
    significant words that appear in the item, lifted onto `BASE_RELEVANCE … 1.0`.
    Crude on purpose — it is a tie-break for ordering, computed on the turn path,
    and anything cleverer would need a model call to decide what to put in a model
    call. With no message to compare against it returns `UNKNOWN_RELEVANCE`.
    """
    try:
        if pinned or str(source) in ALWAYS:
            return 1.0
        want = _words(message)
        if not want:
            return UNKNOWN_RELEVANCE
        body = _words(text)
        if not body:
            return BASE_RELEVANCE
        overlap = len(want & body) / float(len(want))
        return round(BASE_RELEVANCE + (1.0 - BASE_RELEVANCE) * overlap, 3)
    except Exception:
        return UNKNOWN_RELEVANCE


def measure(item, req=None):
    """Fill in whatever measure the collector did not state. Returns *item*.

    ⚠️ NEVER OVERWRITES A STATED VALUE. `None` means "not stated" and is the only
    thing this function fills, which is what lets a source that really does know
    its own relevance (a matched skill) say so without this generic pass quietly
    replacing it with a word count.

    Total by construction: every helper it calls has its own fallback, and the
    whole body is guarded, because a measure is an ordering hint and losing one
    must never cost the item — let alone the turn.
    """
    try:
        source = getattr(item, "source", "") or ""
        if getattr(item, "priority", None) is None:
            item.priority = priority_of(source)
        if getattr(item, "token_cost", None) is None:
            item.token_cost = token_cost(getattr(item, "text", "") or "")
        if getattr(item, "freshness", None) is None:
            item.freshness = freshness_of(source, float(getattr(item, "stamp", 0.0) or 0.0))
        if getattr(item, "relevance", None) is None:
            item.relevance = relevance_of(
                source, getattr(item, "text", "") or "",
                getattr(req, "message", "") or "",
                pinned=bool(getattr(item, "pinned", False)),
            )
    except Exception:
        pass
    return item


def measure_all(items, req=None) -> list:
    """`measure()` over a list, in place. The only caller is `collect()`."""
    return [measure(it, req) for it in (items or [])]


def rank(items) -> list:
    """Survival order: pinned, then priority, then relevance, then freshness.

    ⚠️ THE ONE ORDERING FOR "WHAT DO WE KEEP", shared by `ContextBundle.ranked()`
    and (Task 22) the budget. Two sorts would mean the report showed one thing and
    the trim did another, each defensible alone. It is **not** the render order —
    that is `ORDER`, and `_render()` is its only reader.

    Stable within a tie (original position breaks it), so an unchanged bundle
    always ranks the same way.
    """
    def key(pair):
        i, it = pair
        return (
            0 if bool(getattr(it, "pinned", False)) else 1,
            -int(getattr(it, "priority", None) if getattr(it, "priority", None)
                 is not None else priority_of(getattr(it, "source", ""))),
            -float(getattr(it, "relevance", None) if getattr(it, "relevance", None)
                   is not None else UNKNOWN_RELEVANCE),
            -float(getattr(it, "freshness", None) if getattr(it, "freshness", None)
                   is not None else 1.0),
            i,
        )
    try:
        return [it for _, it in sorted(enumerate(list(items or [])), key=key)]
    except Exception:
        return list(items or [])


def table() -> list[dict]:
    """The source table, for `/api` and `/context` reporting. Counters only."""
    return [{
        "source": s,
        "order": i,
        "priority": priority_of(s),
        "always": s in ALWAYS,
        "ttl": ttl_for(s),
    } for i, s in enumerate(ORDER)]
