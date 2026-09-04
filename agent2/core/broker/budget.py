# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/core/broker/budget.py — WHAT FITS, AND WHAT IS LEFT OUT (Task 22)
───────────────────────────────────────────────────────────────────────
Task 20 gave the broker every source. Task 21 gave every item a priority, a
relevance, a freshness and a token cost. This module is the decision those two
were for: **the prompt has a ceiling, so not everything collected is sent.**

Tasks 20 and 21 were deliberately allowed to inject everything, because with ten
sources and a million-token window nothing overflowed. That is not a policy, it
is a coincidence of the current model table — a custom provider with a 32k window,
a long session, a project doc at its 6 000-character bound and a task list eight
lines deep is a prompt that a vendor rejects mid-turn. "Do not blindly inject
everything into every prompt" is the requirement; this file is where the *not*
lives.

⚠️ PINNED ITEMS ARE NEVER DROPPED, AND OVER-BUDGET IS REPORTED RATHER THAN FIXED.
Memories, rules and the conversation are pinned. If they alone exceed the ceiling
the plan keeps them, sets `over`, and says by how much. The alternative — trimming
until the numbers look right — means the user's own standing rules are what a
budget silently discards, on a turn where nothing tells anyone it happened. A
prompt that is honestly too large fails loudly at the vendor with a message an
operator can act on; a prompt that quietly lost its rules produces confident
wrong behaviour that nobody can trace. Only the second failure is unrecoverable.

⚠️ THE CEILING IS DERIVED, NEVER DECLARED. `capabilities.context_window()` for the
window, `config.MODES[mode]["max_tokens"]` for the output the model must still have
room to write, `RESERVE_TOKENS` for what the broker does not own (the static prompt
body, the tool schemas, each connected bridge's own block). All three are read
live: a literal `1_048_576` here, or a copy of the mode's `max_tokens`, would be a
second declaration of a number this repo already owns — and it drifts in the
direction that overflows, because nobody edits the copy.

⚠️ AN UNKNOWN WINDOW ASSUMES A SMALL ONE, AND SAYS SO (`basis == "assumed"`).
`capabilities.context_window()` returns 0 for "we do not know" — see its docstring;
0 is never "small". A budget still needs a number, so an unknown window is planned
against `ASSUMED_WINDOW` and the plan reports that the ceiling was assumed rather
than read. The asymmetry is deliberate: assuming too *large* costs the turn (a
vendor error), assuming too *small* costs the least relevant situational source.
And the assumption is fixable through a surface that already exists —
`/model caps <model> context_window=200000`, or `AGENT2_CONTEXT_BUDGET` for the
whole process.

⚠️ THE ORDER ITEMS SURVIVE IN IS `sources.rank()` AND IS NOT RE-DERIVED HERE.
One ordering for "what do we keep", shared by the trim, by `ContextBundle.ranked()`
and by every report — because a second sort makes the report describe a different
trim than the one that happened, and both halves look right alone.

⚠️ THE CONVERSATION IS ACCOUNTED FOR AND NEVER TRIMMED. History is bounded by
`config.MAX_CTX_MESSAGES` inside `agent.build_context()`, which is also the only
reader of the `messages` table. A second trimmer here would fight that one: two
components dropping turns from the same history, neither able to see what the
other removed. So its size counts against the ceiling (that is the whole reason
`ContextRequest.conversation_tokens` exists) and its content is untouched.

⚠️ AN ITEM THAT DOES NOT FIT IS SKIPPED, NOT A STOP SIGNAL. The fill continues
down the ranking after a drop, so a cheap low-priority source can still get in
behind an expensive high-priority one. Stopping at the first item that did not fit
would let one oversized source strip *everything* below it — the MCP warning, the
git state — while leaving most of the budget unspent, and the size of one block
would silently decide the fate of unrelated ones.

The plan is also the report: `kept`, `dropped`, `limit`, `used`, `over` and the
three numbers the ceiling came from. Nothing here raises — a budget that cannot
be computed degrades to "keep everything", which is exactly the Task 20/21
behaviour, because a broken bound must not be able to end a turn.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from agent2.core.broker import sources as _sources

# ── The knobs ──────────────────────────────────────────────────────────────────

#: Window assumed for a model whose real one nobody recorded. Small on purpose —
#: see the module note on the asymmetry. Reported as `basis == "assumed"` so a
#: surface can say "assumed", never "known".
ASSUMED_WINDOW = 32_000

#: Room held back for what is in the request but not in the broker's tail: the
#: static prompt body (~1 200 tokens today), the tool declarations for 17 local
#: tools plus every MCP tool, each connected bridge's `prompt_block()`, and the
#: wire overhead. Deliberately generous — under-reserving is the direction that
#: overflows, and the cost of over-reserving is one less situational source.
RESERVE_TOKENS = 6_000

#: Floor for a *derived* ceiling, so a pathological window/mode combination (a
#: mode whose output allowance exceeds the whole window) still leaves room for
#: something rather than dropping every droppable source on every turn. It does
#: NOT apply to `AGENT2_CONTEXT_BUDGET`: an operator who wrote a number down made
#: a decision, and quietly raising it would be this module lying about its ceiling.
MIN_LIMIT = 512

ENV_BUDGET = "AGENT2_CONTEXT_BUDGET"
ENV_RESERVE = "AGENT2_CONTEXT_RESERVE"

#: Where the ceiling came from. Part of the report because "10 000 tokens" means
#: something different when it was read from a model record than when it was
#: assumed for a model nobody has described.
BASIS_ENV = "env"
BASIS_MODEL = "model"
BASIS_ASSUMED = "assumed"
BASIS_CALLER = "caller"       # `plan(limit=…)` — a test, or a dry-run report


def _env_int(name: str, default: int) -> int:
    """An env knob, read at CALL time and never at import.

    Import-time reads would make the two overrides untestable without reimporting
    the module, and would freeze a value that `/api/context` is expected to report
    honestly. The read happens once per turn, not once per agent iteration.
    """
    try:
        raw = (os.environ.get(name) or "").strip()
        return int(raw) if raw else default
    except Exception:
        return default


def output_reservation(mode_key: str = "") -> int:
    """Tokens the model must still have room to WRITE, from `config.MODES`.

    ⚠️ Read live from the mode table, never copied. `fast`/`pro`/`thinking` declare
    2 048 / 8 192 / 16 384 in exactly one place, and a copy here would keep working
    while reserving the wrong amount for whichever mode was edited.

    An unknown or empty mode reserves the LARGEST declared allowance. That is the
    conservative direction: the caller may still be in `thinking` mode and simply
    not have said so, and reserving too little is what overflows.
    """
    try:
        from agent2 import config
        modes = getattr(config, "MODES", {}) or {}
        row = modes.get(str(mode_key or ""))
        if isinstance(row, dict) and int(row.get("max_tokens") or 0) > 0:
            return int(row["max_tokens"])
        return max([int((r or {}).get("max_tokens") or 0)
                    for r in modes.values() if isinstance(r, dict)] or [0])
    except Exception:
        return 0


def window_for(model_key: str = "") -> int:
    """This model's input window in tokens, or 0 for unknown — `capabilities`, always.

    0 is passed through as 0 rather than turned into a guess here: `limits()` is the
    one place that decides what to do about an unknown window, and it records that
    the number was assumed.

    An EMPTY key resolves to `config.DEFAULT_MODEL`'s window, because that is what
    `capabilities.get()` documents an empty key to mean and it is also what actually
    answers such a turn. Treating "" as unknown here would budget a default-model
    turn against `ASSUMED_WINDOW` — a 32k ceiling on a million-token model, on every
    turn a surface did not name its model.
    """
    try:
        from agent2.llm import capabilities as _caps
        return max(0, int(_caps.context_window(str(model_key or ""))))
    except Exception:
        return 0


def limits(model_key: str = "", mode_key: str = "") -> dict:
    """THE ceiling for a turn's context, and the three numbers it came from.

    `limit` bounds the conversation plus every context item — everything in the
    request except what `reserve` covers. Total: any failure falls back to the
    assumed window rather than raising, because a turn must not end over a bound.
    """
    reserve_want = max(0, _env_int(ENV_RESERVE, RESERVE_TOKENS))

    window = window_for(model_key)
    known = window > 0

    forced = max(0, _env_int(ENV_BUDGET, 0))
    if forced > 0:
        # An explicit ceiling is used verbatim — no floor, no reserve arithmetic.
        # The operator's number IS the answer; adjusting it would mean reporting a
        # limit this module does not actually apply.
        return {"limit": forced, "basis": BASIS_ENV, "window": window,
                "window_known": known, "output": 0, "reserve": 0}


    if not known:
        window = ASSUMED_WINDOW

    # Neither reservation may eat the window: a mode allowance larger than the
    # whole window is an incoherent pairing (an 8k model in `thinking` mode), and
    # honouring it literally would leave a ceiling of zero — every situational
    # source dropped on every turn, for a reason no surface would show.
    output = min(max(0, output_reservation(mode_key)), max(0, window // 2))
    reserve = min(reserve_want, max(0, window - output) // 2)
    return {
        "limit": max(MIN_LIMIT, window - output - reserve),
        "basis": BASIS_MODEL if known else BASIS_ASSUMED,
        "window": window,
        "window_known": known,
        "output": output,
        "reserve": reserve,
    }


# ── The plan ───────────────────────────────────────────────────────────────────

@dataclass
class BudgetPlan:
    """What fits, what did not, and the ceiling both were measured against.

    `kept` and `dropped` hold the items themselves, in `sources.rank()` order, so a
    caller can render the first set and *report* the second. A plan that recorded
    only counts would make "the git block was dropped" indistinguishable from "git
    had nothing to say" — the same distinction `ContextBundle.errors` exists for.
    """

    limit: int = 0
    basis: str = BASIS_ASSUMED
    window: int = 0
    window_known: bool = False
    output: int = 0
    reserve: int = 0
    conversation: int = 0
    pinned_tokens: int = 0
    used: int = 0
    kept: list = field(default_factory=list)
    dropped: list = field(default_factory=list)
    over: bool = False

    @property
    def overflow(self) -> int:
        """Tokens the pinned floor is over the ceiling by. 0 unless `over`."""
        return max(0, self.used - self.limit) if self.over else 0

    def kept_sources(self) -> list[str]:
        """Sources with at least one surviving item, in render order.

        "Kept" is not "rendered": an accounting-only item (the conversation, a quiet
        MCP row) survives the budget while contributing no text.
        `ContextBundle.sources_used()` is the rendered set.
        """
        return [s for s in _sources.ORDER
                if any(getattr(i, "source", "") == s for i in self.kept)]

    def dropped_sources(self) -> list[str]:
        return [s for s in _sources.ORDER
                if any(getattr(i, "source", "") == s for i in self.dropped)]

    def to_payload(self) -> dict:
        """Counters and source names — never any item's text."""
        return {
            "limit": self.limit,
            "used": self.used,
            "basis": self.basis,
            "window": self.window,
            "window_known": self.window_known,
            "output_reserved": self.output,
            "static_reserved": self.reserve,
            "conversation": self.conversation,
            "pinned": self.pinned_tokens,
            "over": self.over,
            "overflow": self.overflow,
            "kept": self.kept_sources(),
            "dropped": self.dropped_sources(),
            # `_cost`, not `i.tokens()`: this is a REPORT, and a measure that raises
            # must not be able to break the reporting of a trim it caused.
            "dropped_items": [{"source": getattr(i, "source", ""),
                               "label": getattr(i, "label", ""),
                               "tokens": _cost(i)} for i in self.dropped],
        }


def _cost(item) -> int:
    """One item's token cost, or 0 if it cannot say. Total — see the module note."""
    try:
        return max(0, int(item.tokens()))
    except Exception:
        return 0


def plan(items, *, model_key: str = "", mode_key: str = "",
         conversation_tokens: int = 0, limit: int | None = None) -> BudgetPlan:
    """Decide what reaches the model. THE selection.

    Pinned items and the conversation form a floor that is kept whatever it costs
    (see the module note). Everything else is offered the remaining room in
    `sources.rank()` order, and an item that does not fit is skipped rather than
    ending the fill.

    An item that costs nothing is always kept, even over budget: dropping it saves
    nothing and would report a trim that did not happen — and the conversation item
    is exactly that, a pinned accounting row whose text is empty.
    """
    try:
        lim = limits(model_key, mode_key)
        ceiling = int(lim["limit"] if limit is None else max(0, int(limit)))
        out = BudgetPlan(
            limit=ceiling,
            basis=str(lim["basis"]) if limit is None else BASIS_CALLER,
            window=int(lim["window"]), window_known=bool(lim["window_known"]),
            output=int(lim["output"]), reserve=int(lim["reserve"]),
            conversation=max(0, int(conversation_tokens or 0)),
        )

        ranked = _sources.rank(items)
        pinned = [i for i in ranked if bool(getattr(i, "pinned", False))]
        loose = [i for i in ranked if not bool(getattr(i, "pinned", False))]

        out.pinned_tokens = sum(_cost(i) for i in pinned)
        out.kept = list(pinned)
        out.used = out.pinned_tokens + out.conversation
        out.over = out.used > ceiling

        for item in loose:
            cost = _cost(item)
            if cost == 0 or out.used + cost <= ceiling:
                out.kept.append(item)
                out.used += cost
            else:
                out.dropped.append(item)
        return out
    except Exception:
        # Total by design: a budget that cannot be computed keeps everything, which
        # is precisely how the broker behaved before this module existed. Failing
        # closed here would mean a bug in an *accounting* helper could strip the
        # user's rules — the one outcome this file is written to prevent.
        every = list(items or [])
        return BudgetPlan(limit=0, kept=every, dropped=[],
                          used=sum(_cost(i) for i in every))


def apply(bundle) -> BudgetPlan:
    """Plan *bundle* against its own request and attach the result. Returns the plan.

    ⚠️ CALLED FROM `assemble()`, NOT FROM THE SURFACES. Budgeting a prompt is not
    something a caller can be trusted to remember: `agent.py` and
    `llm/provider_agent.py` each assemble a bundle today, Phases 11–13 add more
    callers, and the failure mode of a forgotten call is invisible — the prompt is
    simply larger than the window on the one turn that overflows. `collect()` stays
    pure gathering, so a report can still show what was collected before the trim.
    """
    req = getattr(bundle, "request", None)
    got = plan(
        list(getattr(bundle, "items", []) or []),
        model_key=str(getattr(req, "model_key", "") or ""),
        mode_key=str(getattr(req, "mode_key", "") or ""),
        conversation_tokens=int(getattr(req, "conversation_tokens", 0) or 0),
    )
    try:
        bundle.plan = got
    except Exception:
        pass
    # ⚠️ THE AUDIT LINE IS EMITTED HERE, FOR THE SAME REASON THE TRIM IS. Both
    # agent loops already duplicate the per-source failure logging, and a trim
    # logged per surface would eventually be logged by only one of them — the half
    # nobody instrumented then drops context with no record anywhere. Only a real
    # event is logged: a plan that kept everything says nothing.
    if got.dropped or got.over:
        try:
            from agent2.core import logging as _alog
            _alog.context_trimmed(",".join(got.dropped_sources()),
                                  got.used, got.limit, got.basis, got.over)
        except Exception:
            pass
    return got



def notice(p: BudgetPlan | None) -> str:
    """The one line the PROMPT gets about what was left out. "" when nothing was.

    ⚠️ THE MODEL IS TOLD, AND THAT IS THE SAME REASONING `_collect_mcp` USES.
    A missing block is not read as "this was omitted", it is read as "there is
    nothing there" — an agent that cannot see the files it changed concludes it
    changed none, exactly as one that cannot see ZAP's tools concludes it called
    them wrongly. Naming the omitted sources costs ~30 tokens out of `reserve` and
    turns a silent gap into something the model can close with a tool call.

    It is NOT a source and deliberately has no slot in `ORDER`: it describes the
    tail rather than contributing to it, so it renders after everything and is
    rendered by `prompt_tail()` alone. Adding a fake source instead would put a
    row in every report for something no collector produced.
    """
    try:
        if p is None or not p.dropped:
            return ""
        names = ", ".join(p.dropped_sources())
        return ("\n\n## CONTEXT OMITTED\n"
                f"- Left out of this prompt to stay inside this model's context "
                f"window: {names}.\n"
                "- Absent here does not mean empty. Read what you need with a tool "
                "(for example `run_command` with `git status`, or `read_file`).")
    except Exception:
        return ""


def describe() -> dict:
    """The policy itself, for `/api/health` and `/api/context`. Knobs, not a turn."""
    return {
        "assumed_window": ASSUMED_WINDOW,
        "reserve": max(0, _env_int(ENV_RESERVE, RESERVE_TOKENS)),
        "min_limit": MIN_LIMIT,
        "forced": max(0, _env_int(ENV_BUDGET, 0)),
        "env": {"budget": ENV_BUDGET, "reserve": ENV_RESERVE},
    }
