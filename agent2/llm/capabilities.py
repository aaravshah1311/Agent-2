# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/llm/capabilities.py
──────────────────────────
THE model capability registry (Task 17). One table of what each selectable model
can do, so the router (Task 18) and the fallback chain (Task 19) never guess.

⚠️ THE HARDEST RULE IN THIS FILE: DO NOT INVENT A CAPABILITY.
Three values are therefore distinct and must stay distinct:

    True   — the model supports it
    False  — the model does not support it
    None   — WE DO NOT KNOW

`None` is not a nicer spelling of `False`. A router that reads unknown as "no"
silently refuses to send an image to a model that handles images perfectly well; a
router that reads unknown as "yes" sends one to a model that will reject the
request. So `supports()` takes an explicit `default` and every caller states which
way it wants to be wrong — see `router.py`, where a *hard requirement* (vision)
treats unknown as unusable while a *preference* (speed) merely deranks it.

The same rule applies to the graded fields. `reasoning`/`coding` are tiers from a
CLOSED vocabulary (`""` · `basic` · `strong` · `advanced` · `frontier`) and they are
explicitly **relative to this catalog** — configuration, not a vendor benchmark
claim. A numeric score would read as measured; a tier reads as an ordering, which is
what it is and all the router needs.

⚠️ `config.MODELS` REMAINS THE ONLY LIST OF MODELS.
This module adds *metadata about* those keys and never a key of its own — a second
model list is exactly the drift `config.MODELS` exists to prevent. `all_models()`
iterates `config.MODELS`, so a model added there appears here automatically, with
unknowns, rather than being invisible to the router.

FOUR SOURCES, IN PRECEDENCE ORDER
─────────────────────────────────
`source` is on every record, and it is not decoration: it is the difference between
a fact and a guess, and a surface that flattened them would make a heuristic look
like a measurement.

    configured    the user said so                    — always wins
    model-ranked  an LLM was asked about the model id  — `rank_with_model()`
    inferred      the model id matched a known family  — `infer_from_model_id()`
    catalog       a built-in, derived from its own row — `_builtin_record()`
    provider      a custom endpoint, nothing known     — genuinely unknown

⚠️ A CUSTOM PROVIDER'S CAPABILITIES ARE NOT DISCOVERABLE, ONLY INFERABLE.
The user supplies a base URL, a key and a model id, and probing the endpoint would
spend their money to learn what its name usually already says. So the middle two
sources exist: pattern matching on the id (free, instant, covers the common
families) and, when that finds nothing, asking a cheap model once (billable, so
never on the hot path). Both are labelled, both are overridable, and where neither
knows anything the fields stay UNKNOWN rather than being filled in to look tidy.

Layer: config → capabilities → router · fallback · surfaces.
"""

from __future__ import annotations

import json
import re
import threading

from agent2.config import DEFAULT_MODEL, MODELS, supports_thinking
from agent2.core import logging as audit

# ── The graded vocabulary ─────────────────────────────────────────────────────
TIER_UNKNOWN = ""
TIER_BASIC = "basic"
TIER_STRONG = "strong"
TIER_ADVANCED = "advanced"
#: ⚠️ ADDED FOR A CONCRETE REASON: a user with a Claude Opus / GPT-5 class endpoint
#: registered as a custom provider was losing every reasoning turn to
#: `gemini-2.5-pro`. Both were `advanced`, the sort is stable, and built-ins come
#: first — so the frontier model was never picked. Capping the vocabulary at
#: `advanced` did not make Gemini better, it just made the ordering unable to
#: express the difference. `2.5-pro` stays `advanced`, which is honest; `frontier`
#: is for the models that genuinely sit above it.
TIER_FRONTIER = "frontier"
TIERS: tuple[str, ...] = (TIER_UNKNOWN, TIER_BASIC, TIER_STRONG, TIER_ADVANCED,
                          TIER_FRONTIER)

SPEED_UNKNOWN = ""
SPEED_FAST = "fast"
SPEED_MEDIUM = "medium"
SPEED_SLOW = "slow"
SPEEDS: tuple[str, ...] = (SPEED_UNKNOWN, SPEED_FAST, SPEED_MEDIUM, SPEED_SLOW)

COST_UNKNOWN = ""
COST_LOW = "low"
COST_MEDIUM = "medium"
COST_HIGH = "high"
COSTS: tuple[str, ...] = (COST_UNKNOWN, COST_LOW, COST_MEDIUM, COST_HIGH)

#: Every field a record carries, with the type each accepts. The router reads
#: these names, `set_meta` validates against them, and `/api/models` ships them —
#: so this tuple is the schema, declared once.
FIELDS: tuple[str, ...] = (
    "model", "provider", "context_window", "reasoning", "coding",
    "speed", "cost", "vision", "tool_use", "structured_output",
    "thinking", "notes", "source",
)

_TIER_RANK = {TIER_UNKNOWN: 0, TIER_BASIC: 1, TIER_STRONG: 2, TIER_ADVANCED: 3,
              TIER_FRONTIER: 4}
_SPEED_RANK = {SPEED_FAST: 3, SPEED_MEDIUM: 2, SPEED_SLOW: 1, SPEED_UNKNOWN: 0}
_COST_RANK = {COST_LOW: 1, COST_MEDIUM: 2, COST_HIGH: 3, COST_UNKNOWN: 0}


def tier_rank(value: str) -> int:
    """0 for unknown, 1–3 for the tiers. Unknown sorts LAST, never as `basic`."""
    return _TIER_RANK.get(str(value or ""), 0)


def speed_rank(value: str) -> int:
    """Higher is faster. Unknown is 0, so it never wins a speed comparison."""
    return _SPEED_RANK.get(str(value or ""), 0)


def cost_rank(value: str) -> int:
    """Higher is more expensive. Unknown is 0 — it never wins a cost comparison
    either, in EITHER direction, which is the honest answer for "no idea"."""
    return _COST_RANK.get(str(value or ""), 0)


# ── What is known about the built-in models ────────────────────────────────────
# ⚠️ DERIVED FROM `config.MODELS`, NOT A SECOND HARD-CODED LIST OF MODELS.
# This started as a dict keyed by model key — `2.5-flash`, `2.5-pro`, `3.1-flash`,
# `3.1-pro` — and went stale the first time the model table was edited: the removed
# keys became dead entries and the ADDED ones (`3.5-flash`, `3.7-flash`) had no
# metadata at all, so the router saw them as all-unknown and refused to rank them.
# A capability table that has to be edited in lockstep with `config.MODELS` is a
# second declaration of the model list, which is exactly what that dict exists to
# prevent.
#
# So the tier comes from the model's own NAME, which is the vendor's own statement
# about it: `flash-lite` is the cheapest tier, `flash` the fast one, `pro` the
# capable one. Any row added to `config.MODELS` is described automatically.
#
# ⚠️ EVERY VALUE HERE IS EITHER THE NAME'S OWN CLAIM OR A DOCUMENTED FACT.
#   * vision / tool_use / structured_output: documented Gemini behaviour across the
#     supported families — images accepted, function calling and response schemas
#     supported. True for every Gemini model this app can call.
#   * `thinking` reads `config.THINKING_GROUPS`, the one declaration of that fact.
#   * `context_window` is 1M only for the family it is PUBLISHED for. Everything
#     else is `0` = UNKNOWN, never a guess — see `_WINDOWS` below.
#   * speed / cost / tiers are relative to each other and labelled `source:
#     "catalog"` so nothing reads them as benchmark claims.
_GEMINI_COMMON = {
    "provider": "google",
    "tool_use": True,
    "structured_output": True,
    "vision": True,
    "source": "catalog",
}

#: Published input windows, by model GROUP. ⚠️ Absent means UNKNOWN (`0`), and
#: `_rank` scores an unknown window 0 for a long-context turn — so a 200k-token turn
#: is never routed at a model whose limit nobody recorded. Pretending to 1M for an
#: unmeasured family is exactly the invention this module forbids; add a group here
#: only when the number is actually published.
_WINDOWS: dict[str, int] = {
    "2.5": 1_048_576,
}

#: Name fragment → (reasoning, coding, speed, cost). Ordered, first match wins, so
#: `flash-lite` MUST precede `flash` — otherwise the lite tier is ranked as a full
#: flash model and gets work it is the wrong choice for.
_NAME_TIERS: tuple[tuple[str, tuple[str, str, str, str]], ...] = (
    ("flash-lite", (TIER_BASIC, TIER_BASIC, SPEED_FAST, COST_LOW)),
    ("flash",      (TIER_STRONG, TIER_STRONG, SPEED_FAST, COST_LOW)),
    ("pro",        (TIER_ADVANCED, TIER_ADVANCED, SPEED_SLOW, COST_HIGH)),
    ("ultra",      (TIER_FRONTIER, TIER_FRONTIER, SPEED_SLOW, COST_HIGH)),
)


def _builtin_record(key: str, cfg: dict) -> dict:
    """The catalog record for a `config.MODELS` entry, derived from its own row.

    Total by construction: a model whose name matches no fragment still gets the
    documented Gemini booleans and its `thinking` flag, with the graded fields left
    UNKNOWN. That is the honest answer for a name we cannot read, and it is still
    strictly more than the nothing an absent hard-coded entry gave.
    """
    api = str(cfg.get("api") or key).lower()
    rec = dict(_GEMINI_COMMON)
    rec["context_window"] = _WINDOWS.get(str(cfg.get("group") or ""), 0)
    for fragment, (reasoning, coding, speed, cost) in _NAME_TIERS:
        if fragment in api:
            rec.update({"reasoning": reasoning, "coding": coding,
                        "speed": speed, "cost": cost})
            break
    else:
        rec["notes"] = ("Tier not derivable from the model name — set it with "
                        "`/model caps` if you know it.")
    if not rec.get("notes"):
        window = rec["context_window"]
        rec["notes"] = (f"{cfg.get('label') or key}"
                        + (f" · {window:,}-token window" if window
                           else " · window not published, treated as unknown"))
    return rec


# ── Inference from a custom provider's model id ────────────────────────────────
# ⚠️ THIS IS INFERENCE, NOT MEASUREMENT, AND THE RECORD SAYS SO (`source:
# "inferred"`). It is not the "invented capability" this module forbids, and the
# difference is worth stating precisely:
#
#   * The signal is the model id THE USER TYPED IN. `claude-opus-5` is not a guess
#     about an opaque endpoint — it is the user telling us which model is behind it.
#   * It is visible. `source` distinguishes it from `catalog` (built-in facts) and
#     `configured` (the user's own correction), and `/model caps` prints it.
#   * It is overridable, and an override always wins.
#   * Where the name says nothing, the field stays UNKNOWN. A pattern match must
#     never fill a field in just to avoid a blank.
#
# Why it has to exist: without it every custom provider ranked all-unknown, so a
# registered Claude Opus / GPT-5 class endpoint lost every reasoning turn to
# `gemini-2.5-pro`. The registry was technically honest and practically useless —
# the user had configured a better model and the router could not see it.
#
# ⚠️ CONFIDENCE IS PER FIELD, AND `vision` IS THE STRICT ONE. Getting a tier wrong
# costs a suboptimal choice; getting `vision` wrong costs a hard vendor rejection
# mid-turn, so it is asserted only for families that document image input and is
# left `None` for everything else.
#
# ⚠️ ORDER MATTERS — FIRST MATCH WINS, SO NARROW BEFORE BROAD. `gpt-5-mini` must be
# tested before `gpt-5`, or the mini is ranked as a frontier model. Same for
# `opus` vs the bare `claude` fallback.
_ANTHROPIC_LIKE = {
    "provider": "anthropic", "vision": True, "tool_use": True,
    "structured_output": True, "context_window": 200_000,
}
_OPENAI_LIKE = {
    "provider": "openai", "vision": True, "tool_use": True,
    "structured_output": True,
}

_MODEL_PATTERNS: tuple[tuple[tuple[str, ...], dict], ...] = (
    # ── Anthropic ─────────────────────────────────────────────────────────────
    (("claude", "opus"), {**_ANTHROPIC_LIKE, "reasoning": TIER_FRONTIER,
                          "coding": TIER_FRONTIER, "speed": SPEED_MEDIUM,
                          "cost": COST_HIGH,
                          "notes": "Inferred from the model id: Claude Opus class."}),
    (("claude", "sonnet"), {**_ANTHROPIC_LIKE, "reasoning": TIER_ADVANCED,
                            "coding": TIER_FRONTIER, "speed": SPEED_FAST,
                            "cost": COST_MEDIUM,
                            "notes": "Inferred from the model id: Claude Sonnet class."}),
    (("claude", "haiku"), {**_ANTHROPIC_LIKE, "reasoning": TIER_STRONG,
                           "coding": TIER_ADVANCED, "speed": SPEED_FAST,
                           "cost": COST_LOW,
                           "notes": "Inferred from the model id: Claude Haiku class."}),
    (("claude", "fable"), {**_ANTHROPIC_LIKE, "reasoning": TIER_ADVANCED,
                           "coding": TIER_ADVANCED, "speed": SPEED_FAST,
                           "cost": COST_MEDIUM,
                           "notes": "Inferred from the model id: Claude Fable class."}),
    # A Claude model whose family word we do not recognise still tells us the
    # vendor, the wire format and that it takes images — which is most of what
    # routing needs. The tiers stay unknown rather than being guessed.
    (("claude",), {**_ANTHROPIC_LIKE,
                   "notes": "Inferred from the model id: Anthropic, family not "
                            "recognised — set tiers with /model caps."}),

    # ── OpenAI / GPT ──────────────────────────────────────────────────────────
    # Narrow first: the small variants must not inherit the frontier ranking.
    (("gpt-5", "mini"), {**_OPENAI_LIKE, "reasoning": TIER_ADVANCED,
                         "coding": TIER_ADVANCED, "speed": SPEED_FAST,
                         "cost": COST_LOW,
                         "notes": "Inferred from the model id: GPT-5 mini class."}),
    (("gpt-5", "nano"), {**_OPENAI_LIKE, "reasoning": TIER_STRONG,
                         "coding": TIER_STRONG, "speed": SPEED_FAST,
                         "cost": COST_LOW,
                         "notes": "Inferred from the model id: GPT-5 nano class."}),
    (("gpt-5",), {**_OPENAI_LIKE, "reasoning": TIER_FRONTIER,
                  "coding": TIER_FRONTIER, "speed": SPEED_MEDIUM,
                  "cost": COST_HIGH,
                  "notes": "Inferred from the model id: GPT-5 class."}),
    (("gpt-4.1",), {**_OPENAI_LIKE, "reasoning": TIER_ADVANCED,
                    "coding": TIER_ADVANCED, "speed": SPEED_FAST,
                    "cost": COST_MEDIUM, "context_window": 1_000_000,
                    "notes": "Inferred from the model id: GPT-4.1 class."}),
    (("gpt-4o",), {**_OPENAI_LIKE, "reasoning": TIER_STRONG,
                   "coding": TIER_ADVANCED, "speed": SPEED_FAST,
                   "cost": COST_MEDIUM,
                   "notes": "Inferred from the model id: GPT-4o class."}),
    # o-series reasoning models. Deliberately no `vision` claim: the family's
    # image support has varied by member, and this is the field where being wrong
    # is a hard mid-turn rejection.
    (("o3",), {"provider": "openai", "tool_use": True, "structured_output": True,
               "reasoning": TIER_FRONTIER, "coding": TIER_ADVANCED,
               "speed": SPEED_SLOW, "cost": COST_HIGH,
               "notes": "Inferred from the model id: o-series reasoning model."}),
    (("o4",), {"provider": "openai", "tool_use": True, "structured_output": True,
               "reasoning": TIER_FRONTIER, "coding": TIER_ADVANCED,
               "speed": SPEED_SLOW, "cost": COST_HIGH,
               "notes": "Inferred from the model id: o-series reasoning model."}),

    # ── Others commonly reached through a gateway ─────────────────────────────
    (("gemini", "pro"), {"provider": "google", "vision": True, "tool_use": True,
                         "structured_output": True, "reasoning": TIER_ADVANCED,
                         "coding": TIER_ADVANCED, "speed": SPEED_MEDIUM,
                         "cost": COST_HIGH,
                         "notes": "Inferred from the model id: Gemini Pro class."}),
    (("gemini", "flash"), {"provider": "google", "vision": True, "tool_use": True,
                           "structured_output": True, "reasoning": TIER_STRONG,
                           "coding": TIER_STRONG, "speed": SPEED_FAST,
                           "cost": COST_LOW,
                           "notes": "Inferred from the model id: Gemini Flash class."}),
    (("grok",), {"provider": "xai", "tool_use": True, "reasoning": TIER_ADVANCED,
                 "coding": TIER_ADVANCED, "speed": SPEED_MEDIUM,
                 "cost": COST_MEDIUM,
                 "notes": "Inferred from the model id: Grok class."}),
    (("deepseek", "r1"), {"provider": "deepseek", "tool_use": True,
                          "reasoning": TIER_ADVANCED, "coding": TIER_ADVANCED,
                          "speed": SPEED_SLOW, "cost": COST_LOW,
                          "notes": "Inferred from the model id: DeepSeek R1 "
                                   "(reasoning) class."}),
    (("deepseek",), {"provider": "deepseek", "tool_use": True,
                     "reasoning": TIER_STRONG, "coding": TIER_ADVANCED,
                     "speed": SPEED_FAST, "cost": COST_LOW,
                     "notes": "Inferred from the model id: DeepSeek class."}),
    (("qwen",), {"provider": "qwen", "tool_use": True, "reasoning": TIER_STRONG,
                 "coding": TIER_ADVANCED, "speed": SPEED_FAST, "cost": COST_LOW,
                 "notes": "Inferred from the model id: Qwen class."}),
    (("llama",), {"provider": "meta", "tool_use": True, "reasoning": TIER_STRONG,
                  "coding": TIER_STRONG, "speed": SPEED_FAST, "cost": COST_LOW,
                  "notes": "Inferred from the model id: Llama class."}),
    (("mistral",), {"provider": "mistral", "tool_use": True,
                    "reasoning": TIER_STRONG, "coding": TIER_STRONG,
                    "speed": SPEED_FAST, "cost": COST_LOW,
                    "notes": "Inferred from the model id: Mistral class."}),
    (("kimi",), {"provider": "moonshot", "tool_use": True,
                 "reasoning": TIER_ADVANCED, "coding": TIER_ADVANCED,
                 "speed": SPEED_MEDIUM, "cost": COST_LOW,
                 "notes": "Inferred from the model id: Kimi class."}),
    (("glm",), {"provider": "zhipu", "tool_use": True, "reasoning": TIER_STRONG,
                "coding": TIER_ADVANCED, "speed": SPEED_FAST, "cost": COST_LOW,
                "notes": "Inferred from the model id: GLM class."}),
)


def infer_from_model_id(model_id: str) -> dict:
    """What the configured model id itself tells us. `{}` when it says nothing.

    ⚠️ ALL WORDS OF A PATTERN MUST APPEAR, AND ORDER IN THE ID IS IRRELEVANT.
    Gateways rename things freely — `anthropic/claude-opus-5`,
    `claude-opus-5-20260110`, `opus-5-claude-thinking` and `gpt-5.6-sol` are all
    the same handful of words in different arrangements. Substring-set matching
    survives that; a regex anchored to a vendor's current naming does not, and
    would silently stop matching the day a gateway added a suffix.

    ⚠️ RETURNS `{}` RATHER THAN A BLANK RECORD. The caller merges it, so `{}` means
    "leave every field unknown" — the honest answer for a name we do not recognise,
    and structurally different from a record full of defaults.
    """
    text = str(model_id or "").strip().lower()
    if not text:
        return {}
    for words, record in _MODEL_PATTERNS:
        if all(word in text for word in words):
            return dict(record)
    return {}


def version_hint(model_id: str) -> float:
    """The first version-looking number in a model id, or 0.0.

    ⚠️ A TIEBREAK ONLY, AND THE LAST ONE. Its whole job is that a user who has
    registered both `claude-opus-5` and `claude-opus-4-8` gets the newer one on a
    turn where the registry rates them identically — which it does, because both are
    `frontier` and no capability field distinguishes them. Without this the winner is
    whichever was registered first, which is not a preference anybody expressed.

    ⚠️ It is deliberately CRUDE and must stay that way. `claude-opus-4-8` reads as
    `4.0`, not `4.8`, because `4-8` is only a version separator by convention and
    could as easily be a date fragment. `4.0 < 5.0` is all this needs to be right
    about; chasing `4.8` would mean parsing every vendor's dating scheme, and a
    parser that is wrong about a *capability* is worse than a tiebreak that is
    merely imprecise. Anything version-sensitive belongs in an explicit
    `/model caps` override, not here.

    The leading `[a-z]` guard skips a bare year — `20260110` in
    `claude-opus-4-20260110` must not read as version 20260110 and outrank
    everything, which is exactly what a naive "first number" would do.
    """
    text = str(model_id or "").strip().lower()
    if not text:
        return 0.0
    for match in re.finditer(r"(?<![\d.])(\d{1,2})(?:\.(\d{1,2}))?(?![\d])", text):
        whole, frac = match.group(1), match.group(2)
        try:
            return float(f"{whole}.{frac}") if frac else float(whole)
        except ValueError:
            return 0.0
    return 0.0



# ── Asking a model to rank a model ────────────────────────────────────────────
# The last resort, and the only source here that costs an API call: when neither
# the catalog nor `infer_from_model_id` recognises a custom provider's model id,
# ask an LLM what it knows about it. A user who registers `some-inhouse-moe-v3`
# would otherwise own a permanently unrankable model — the router sees all-unknown
# and never routes anything demanding to it, which is indistinguishable from
# Agent2 ignoring a model the user deliberately configured and paid for.
#
# ⚠️ NEVER ON THE HOT PATH. This runs when a provider is ADDED, or when a human
# asks (`/model rank`, `POST /api/models/<key>/rank`) — never inside `get()` and
# never inside a routing decision. `get()` is called several times per decision, so
# a network call in there would put a multi-second billable round trip in front of
# every turn.
#
# ⚠️ THE ANSWER IS VALIDATED, NOT TRUSTED. Every field goes through `_coerce`, so a
# hallucinated `speed: "warp"` is dropped rather than stored, and prose instead of
# JSON yields nothing at all. One bad field does not discard a good answer.
#
# ⚠️ STORED AS `model-ranked`, AND IT NEVER OVERWRITES `configured`. This is the
# least reliable source in the module, so it is the one that must be most clearly
# labelled: `/model caps` prints the source, and the user's own correction always
# wins. `force=True` is the only way past that, and only a human can ask for it.
SOURCE_RANKED = "model-ranked"

_RANK_PROMPT = """You are classifying an LLM's capabilities for a model router.

Model identifier: {model_id}
Endpoint host: {host}
API format: {fmt}

Reply with ONLY a JSON object, no prose and no code fence, using exactly these
keys. Use null for anything you are not confident about - a wrong value is far
worse than null, because the router acts on it.

{{
  "provider": "<vendor name, or null>",
  "context_window": <input token limit as an integer, or null>,
  "reasoning": "basic" | "strong" | "advanced" | "frontier" | null,
  "coding":    "basic" | "strong" | "advanced" | "frontier" | null,
  "speed":     "fast" | "medium" | "slow" | null,
  "cost":      "low" | "medium" | "high" | null,
  "vision": true | false | null,
  "structured_output": true | false | null,
  "confident": true | false
}}

Tier guidance, relative to each other:
  frontier = current top-end flagship (Claude Opus class, GPT-5 class)
  advanced = strong flagship one step below frontier (Gemini 2.5 Pro class)
  strong   = capable mid-tier or fast variant (Flash / mini / Haiku class)
  basic    = small or older model

Set "confident": false if you do not recognise this identifier at all. Do not
guess a context window you do not know - null is the correct answer there.
"""


def _extract_json(text: str) -> dict:
    """Pull the first JSON object out of a model reply.

    Models wrap JSON in fences and prose despite being told not to, and a
    classification that failed over a stray "Here you go:" would be a silent
    downgrade to unknown. Slicing between the outermost braces handles every form
    of that without needing a parser for the wrapper.
    """
    raw = str(text or "").strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(raw[start:end + 1])
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _ask_gemini(prompt: str) -> str:
    """Ask the cheapest built-in model. `""` on any failure.

    ⚠️ DEFAULT_MODEL, not the user's current model. Classification is a one-line
    lookup the cheap fast tier does well, and the user's current model may be an
    expensive frontier endpoint — billing them at frontier rates to label a model is
    a cost they never agreed to. It is also quite likely to BE the thing being
    classified, and asking a misconfigured endpoint to describe itself fails at
    exactly the moment this feature is needed.
    """
    try:
        from agent2.config import MODELS as _M
        from agent2.llm.keys import rotator
        client, _key, _label = rotator.get()
        if client is None:
            return ""
        api_model = (_M.get(DEFAULT_MODEL) or {}).get("api") or "gemini-2.5-flash"
        resp = client.models.generate_content(model=api_model, contents=prompt)
        return str(getattr(resp, "text", "") or "")
    except Exception:
        return ""


def _ask_any_provider(prompt: str) -> str:
    """Fallback for an install with no Gemini key: ask a configured provider.

    Only reached when `_ask_gemini` found no client at all. Stops at the first
    answer — this is a best-effort label, not something worth walking every endpoint
    and every failure for.
    """
    try:
        from agent2.llm import providers as _providers
        for row in _providers.list_providers(safe=False):
            try:
                out = _providers.chat(row, [{"role": "user", "content": prompt}],
                                      "Reply with JSON only.", use_tools=False)
                text = str((out or {}).get("text") or "")
                if text.strip():
                    return text
            except Exception:
                continue
    except Exception:
        return ""
    return ""


def _provider_format(model_key: str) -> str:
    """The wire format of a custom provider, as a hint for the classifier."""
    if not str(model_key or "").startswith("custom:"):
        return ""
    pid = model_key.split(":", 1)[1]
    try:
        from agent2.llm import providers as _providers
        for row in _providers.list_providers(safe=True):
            if str(row.get("id")) == pid:
                return str(row.get("format") or "")
    except Exception:
        pass
    return ""


def rank_with_model(model_key: str, *, force: bool = False) -> dict:
    """Ask an LLM to fill in an unrecognised model's capabilities. Returns the record.

    Returns the CURRENT record unchanged when there is nothing to do — the model is
    already described by the catalog, by pattern inference, or by the user. That is
    what makes this safe to call speculatively, including from `add_provider`, where
    the common case is a recognised name and the correct behaviour is to spend
    nothing at all.

    ⚠️ NEVER RAISES AND NEVER BLOCKS FOREVER. Every failure path — no key, no
    network, prose instead of JSON, a hallucinated vocabulary, a DB fault — ends as
    "the record is unchanged", which is the state the caller was already in.
    """
    key = str(model_key or "").strip()
    if not key:
        return {}
    current = get(key)
    if not force and current.get("source") in ("catalog", "inferred",
                                               "configured", SOURCE_RANKED):
        return current

    prompt = _RANK_PROMPT.format(
        model_id=str(current.get("model") or key),
        host=str(current.get("provider") or "unknown"),
        fmt=_provider_format(key) or "unknown")

    answer = _ask_gemini(prompt) or _ask_any_provider(prompt)
    data = _extract_json(answer)
    if not data:
        audit.event("caps.rank.no_answer", model=key)
        return current
    if data.get("confident") is False:
        # ⚠️ An explicit "I do not recognise this" is a USEFUL answer and is
        # honoured. Storing the nulls that came with it would look identical to
        # never having asked, so the record is left alone and the attempt is logged
        # — otherwise a human keeps re-running `/model rank` against a model nothing
        # recognises, with no way to tell it had already been tried.
        audit.event("caps.rank.not_recognised", model=key)
        return current

    fields: dict = {}
    for field in ("provider", "context_window", "reasoning", "coding", "speed",
                  "cost", "vision", "structured_output"):
        if field not in data or data[field] is None:
            continue
        try:
            fields[field] = _coerce(field, data[field])
        except ValueError:
            continue          # one bad field must not discard a good answer
    fields = {k: v for k, v in fields.items() if v not in ("", 0)}
    if not fields:
        audit.event("caps.rank.empty", model=key)
        return current

    fields["notes"] = ("Ranked by asking a model about this identifier - a "
                       "heuristic, not a measurement. Correct it with "
                       "`/model caps`.")
    try:
        _store(key, fields, SOURCE_RANKED)
    except Exception:
        return current
    audit.event("caps.rank.ok", model=key, fields=",".join(sorted(fields)))
    return get(key)


def rank_missing(*, limit: int = 8) -> list[str]:
    """Rank only the custom providers nothing recognises. Returns the keys changed.

    The cheap sweep: it skips anything the catalog, pattern inference, the user or a
    previous ranking already describes, so calling it repeatedly costs nothing. This
    is what an automatic path should use — `rank_all` is the deliberate, expensive
    one a human asks for.

    `limit` is a real ceiling, not a paging hint: this is the "I just imported
    twenty gateway models" path, and an unbounded loop means twenty billable round
    trips from one command.
    """
    return _rank_batch(force=False, limit=limit)[0]


def rank_all(*, limit: int = 32, force: bool = True) -> tuple[list[str], list[str], int]:
    """Re-rank EVERY custom provider. Returns `(changed, skipped, not_reached)`.

    What `/model rank all` runs. Unlike `rank_missing` it re-asks about models it
    already has an answer for, because the answers go stale: a model id that meant
    nothing six months ago is a known flagship now, and the pattern table cannot be
    updated on a user's machine while a fresh question can.

    ⚠️ IT STILL DOES NOT OVERWRITE A HAND CORRECTION. `_store` refuses a
    `model-ranked` write over a `configured` one, and those models come back in
    `skipped` rather than being silently left out. "Re-rank everything" is a request
    to re-ask the models, not permission to discard the user's own answers — those
    are changed with `/model caps`.

    ⚠️ `not_reached` IS REPORTED, NOT SWALLOWED. One model call per provider is real
    money, so the ceiling is real; a truncated sweep that said nothing would read as
    "everything is now ranked" when it is not.
    """
    changed, skipped, remaining = _rank_batch(force=force, limit=limit)
    return changed, skipped, remaining


def _rank_batch(*, force: bool, limit: int) -> tuple[list[str], list[str], int]:
    """Shared body of `rank_missing` / `rank_all`. THE one batch loop.

    Two near-identical loops would drift on the parts that matter least and are
    hardest to notice — the ceiling, the skip rule, the "did we finish" count.
    """
    changed: list[str] = []
    skipped: list[str] = []
    ceiling = max(1, int(limit))
    try:
        from agent2.llm import providers as _providers
        rows = _providers.list_providers(safe=True)
    except Exception:
        return changed, skipped, 0

    queue = []
    for row in rows:
        key = "custom:" + str(row.get("id") or "")
        rec = get(key)
        if rec.get("source") == "configured":
            # Protected by `_store` anyway; listed here so the caller can SAY so
            # instead of the user wondering why their model was left out.
            skipped.append(key)
            continue
        if not force and rec.get("source") != "provider":
            continue
        queue.append(key)

    for key in queue[:ceiling]:
        before = get(key)
        if rank_with_model(key, force=force) != before:
            changed.append(key)
    return changed, skipped, max(0, len(queue) - ceiling)


def rank_in_background(model_key: str) -> None:
    """Fire-and-forget ranking, for `add_provider`.

    ⚠️ A DAEMON THREAD, AND NOTHING WAITS ON IT. Adding a provider must stay
    instant: the user is sitting at a prompt (or a form), and blocking that on a
    model call to a possibly-unreachable endpoint would make "add a provider" feel
    broken. If the process exits first the model simply stays unranked, and
    `/model rank` still works.
    """
    try:
        threading.Thread(target=rank_with_model, args=(model_key,),
                         daemon=True, name="a2-rank-model").start()
    except Exception:
        pass


#: A record with nothing claimed. The starting point for a custom provider and the
#: answer for a model key nobody has described — never a KeyError, so every caller
#: is total.
_BLANK: dict = {
    "model": "",
    "provider": "",
    "context_window": 0,
    "reasoning": TIER_UNKNOWN,
    "coding": TIER_UNKNOWN,
    "speed": SPEED_UNKNOWN,
    "cost": COST_UNKNOWN,
    "vision": None,
    "tool_use": None,
    "structured_output": None,
    "thinking": None,
    "notes": "",
    "source": "unknown",
}

_lock = threading.RLock()
_override_cache: dict[str, dict] | None = None


# ── Stored overrides ──────────────────────────────────────────────────────────
def _load_overrides() -> dict[str, dict]:
    """Every hand-set record, keyed by model key. Cached; total on a DB fault.

    A DB fault yields `{}` — i.e. "no overrides" — which degrades to the catalog
    rather than to an exception. The registry is metadata; nothing here is worth
    failing a turn over.
    """
    global _override_cache
    with _lock:
        if _override_cache is not None:
            return _override_cache
    out: dict[str, dict] = {}
    try:
        from agent2.database import qall
        for row in qall("SELECT model_key, meta FROM model_caps"):
            try:
                data = json.loads(row["meta"] or "{}")
            except Exception:
                continue
            if isinstance(data, dict):
                out[str(row["model_key"])] = data
    except Exception:
        out = {}
    with _lock:
        _override_cache = out
    return out


def invalidate() -> None:
    """Drop the override cache. Called by the `settings` sync subscriber below."""
    global _override_cache
    with _lock:
        _override_cache = None


def _coerce(field: str, value):
    """Validate one field, or raise ValueError.

    ⚠️ Raises rather than silently correcting. `set_meta` is reached from an API
    route and from the CLI; a bad value that was quietly coerced to `""` would look
    to the user like the write had succeeded and simply not worked.
    """
    if field in ("reasoning", "coding"):
        text = str(value or "").strip().lower()
        if text not in TIERS:
            raise ValueError(f"{field} must be one of {TIERS!r}")
        return text
    if field == "speed":
        text = str(value or "").strip().lower()
        if text not in SPEEDS:
            raise ValueError(f"speed must be one of {SPEEDS!r}")
        return text
    if field == "cost":
        text = str(value or "").strip().lower()
        if text not in COSTS:
            raise ValueError(f"cost must be one of {COSTS!r}")
        return text
    if field == "context_window":
        try:
            n = int(value or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("context_window must be an integer") from exc
        if n < 0:
            raise ValueError("context_window cannot be negative")
        return n
    if field in ("vision", "tool_use", "structured_output", "thinking"):
        if value is None or (isinstance(value, str) and not value.strip()):
            return None                      # explicitly "unknown"
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off"):
            return False
        if text in ("unknown", "none", "null", "?"):
            return None
        raise ValueError(f"{field} must be true, false or unknown")
    if field in ("provider", "notes", "model"):
        return str(value or "").strip()[:200]
    raise ValueError(f"unknown field {field!r}")


def _store(model_key: str, fields: dict, source: str) -> None:
    """Write a record to `model_caps` under *source*. THE one writer.

    Both `set_meta` (the user) and `rank_with_model` (an LLM) land here, because
    two writers to one table means two chances to forget the merge, the cache
    invalidation or the cross-process notify — and the symptom of forgetting the
    last one is that the CLI keeps serving stale metadata after the browser edits
    it, which nobody would attribute to a missing `sync.notify`.

    ⚠️ MERGES ONTO THE EXISTING ROW. An absent field is left alone, which is what
    makes a partial edit safe (see `set_meta`'s docstring).

    ⚠️ A `model-ranked` WRITE NEVER OVERWRITES A `configured` ONE. Ranking runs in a
    background thread on `add_provider`; without this guard a user who corrected a
    model by hand and then re-added the provider would silently lose the correction
    to a guess. The user's answer is the more reliable one and must be the sticky
    one.
    """
    key = str(model_key or "").strip()
    if not key:
        raise ValueError("model_key is required")
    current = dict(_load_overrides().get(key, {}))
    if source == SOURCE_RANKED and current.get("source") == "configured":
        return
    for field, value in fields.items():
        if field in ("model", "source"):
            continue                          # derived, never stored
        current[field] = value
    current["source"] = source
    try:
        from agent2.database import exe
        exe("INSERT INTO model_caps(model_key, meta, updated_at)"
            " VALUES(?,?,datetime('now'))"
            " ON CONFLICT(model_key) DO UPDATE SET"
            " meta=excluded.meta, updated_at=excluded.updated_at",
            (key, json.dumps(current, sort_keys=True)))
    except Exception as exc:
        raise RuntimeError(f"could not store model metadata: {str(exc)[:120]}") from exc
    invalidate()
    _notify()


def set_meta(model_key: str, **fields) -> dict:
    """Persist a metadata override for *model_key* and return the merged record.

    ⚠️ AN ABSENT FIELD IS LEFT ALONE; `None` ON A TRI-STATE MEANS "UNKNOWN".
    Same distinction `integrations.state.set_config` draws, and the same reason: a
    surface that edits one field submits a form that does not mention the rest, and
    reading absent as "clear it" would wipe metadata the user spent time entering.
    Passing `vision=None` explicitly *is* how you say "I no longer claim to know".
    """
    key = str(model_key or "").strip()
    if not key:
        raise ValueError("model_key is required")
    unknown = [f for f in fields if f not in FIELDS]
    if unknown:
        raise ValueError(f"unknown field(s): {sorted(unknown)}")

    coerced = {f: _coerce(f, v) for f, v in fields.items()
               if f not in ("model", "source")}
    _store(key, coerced, "configured")
    return get(key)


def clear_meta(model_key: str) -> dict:
    """Forget the override, returning the model to the catalog's own answer."""
    key = str(model_key or "").strip()
    if not key:
        return dict(_BLANK)
    try:
        from agent2.database import exe
        exe("DELETE FROM model_caps WHERE model_key=?", (key,))
    except Exception:
        pass
    invalidate()
    _notify()
    return get(key)


def _notify() -> None:
    try:
        from agent2.core import sync
        sync.notify("settings", model_caps=True)
    except Exception:
        pass


# ── Lookup ────────────────────────────────────────────────────────────────────
def _custom_base(model_key: str) -> dict:
    """Everything derivable about `custom:<id>` WITHOUT calling the endpoint.

    Two sources, both local: the provider row (host, wire format, model id) and
    `infer_from_model_id()` on the model id the user typed. Nothing here touches
    the network — probing an endpoint to discover its capabilities would spend the
    user's money to learn what its name already says.

    ⚠️ `tool_use` IS TRUE EVEN WHEN NOTHING WAS INFERRED, and that is a fact about
    OUR integration rather than a claim about the vendor: Agent2 only ever drives a
    custom provider through its own tool schema, so an endpoint that could not do
    tool calls would not function here at all.

    ⚠️ `source` DISTINGUISHES THE TWO OUTCOMES. `inferred` means the model id was
    recognised and the tiers below are a documented heuristic; `provider` means it
    was not, and every graded field is genuinely unknown. A surface that showed
    both as "provider" would make a heuristic look like a measurement.
    """
    rec = dict(_BLANK)
    pid = model_key.split(":", 1)[1] if ":" in model_key else ""
    rec["source"] = "provider"
    try:
        from agent2.llm import providers as _providers
        for row in _providers.list_providers(safe=True):
            if str(row.get("id")) != pid:
                continue
            model_id = str(row.get("model_id") or "")
            rec["model"] = model_id
            base = str(row.get("base_url") or "")
            host = base.split("://", 1)[-1].split("/", 1)[0]
            rec["provider"] = host or str(row.get("format") or "custom")
            rec["tool_use"] = True

            inferred = infer_from_model_id(model_id)
            if inferred:
                # ⚠️ The inferred `provider` is the VENDOR (anthropic, openai…) and
                # the row's is the HOST actually being dialled — a gateway, often.
                # The host is the more useful of the two to show a user debugging
                # "which endpoint answered", so it wins, and the vendor goes in the
                # note where it explains the tiers.
                vendor = inferred.pop("provider", "")
                rec.update(inferred)
                rec["provider"] = host or vendor or rec["provider"]
                rec["source"] = "inferred"
                if vendor and host and vendor not in host:
                    rec["notes"] = f"{rec.get('notes', '')} (via {host})".strip()
            else:
                rec["notes"] = ("Custom endpoint — the model id was not recognised, "
                                "so capabilities are unknown. Set them with "
                                "`/model caps` if you know them.")
            break
    except Exception:
        pass
    return rec


def get(model_key: str) -> dict:
    """The full record for *model_key*. Never raises, never a KeyError.

    Precedence: stored override → catalog (or provider-derived) → blank. So a user
    who corrects a value wins over the catalog, and the catalog wins over "unknown"
    — which is the only ordering that makes `set_meta` worth having.
    """
    key = str(model_key or "").strip() or DEFAULT_MODEL
    rec = dict(_BLANK)

    if key.startswith("custom:"):
        rec.update(_custom_base(key))
    elif key in MODELS:
        cfg = MODELS.get(key) or {}
        rec.update(_builtin_record(key, cfg))
        rec["model"] = str(cfg.get("api") or key)
        # `thinking` is DERIVED, not restated. `config.supports_thinking()` is the
        # one predicate; a literal group tuple here would be the same fact stored
        # twice, and it drifted exactly that way once — a model group added to
        # `config.MODELS` was absent from both copies, so thinking mode silently
        # attached no budget on the newest model.
        rec["thinking"] = supports_thinking(str(cfg.get("group") or ""))

    rec.update({k: v for k, v in _load_overrides().get(key, {}).items()
                if k in FIELDS})
    rec["key"] = key
    # `or key`, not `setdefault`: the blank record already HAS a `model` field (an
    # empty string), so setdefault would never fire and an unknown key would come
    # back nameless — which reads downstream as "a model with no id".
    rec["model"] = rec.get("model") or key
    return rec


def supports(model_key: str, capability: str, *, default: bool = False) -> bool:
    """Whether *model_key* has a boolean capability.

    ⚠️ `default` IS REQUIRED READING, NOT A CONVENIENCE. It is what the caller
    answers when the registry says `None`, and the two answers are both correct in
    different places: a hard requirement (the turn carries an image) must treat
    unknown as unusable, while a preference must not eliminate a model merely
    because nobody has filled its record in. Making it a keyword with a
    conservative default means the risky reading has to be written out loud.
    """
    value = get(model_key).get(capability)
    return default if value is None else bool(value)


def context_window(model_key: str) -> int:
    """Input window in tokens, or 0 for unknown. 0 is never "small"."""
    try:
        return int(get(model_key).get("context_window") or 0)
    except (TypeError, ValueError):
        return 0


def all_models(*, include_custom: bool = True) -> list[dict]:
    """Every selectable model with its record, built-ins first, in config order.

    ⚠️ Iterates `config.MODELS`, so a model added to config
    shows up here — with unknowns — instead of being silently absent from the
    router's candidate list.
    """
    out = [get(key) for key in MODELS]
    if include_custom:
        try:
            from agent2.llm import providers as _providers
            out += [get("custom:" + str(row["id"]))
                    for row in _providers.list_providers(safe=True)]
        except Exception:
            pass
    return out


def describe() -> dict:
    """Serialisable snapshot for `/api/models` and `/api/health`."""
    return {
        "fields": list(FIELDS),
        "tiers": list(TIERS),
        "speeds": list(SPEEDS),
        "costs": list(COSTS),
        "models": all_models(),
    }


# Metadata is shared state: an override set in the web half must invalidate the
# CLI's cache. `settings` is the resource `set_meta` bumps, and `core.sync`'s
# poller republishes another process's bump as a local event.
try:
    from agent2.core import sync as _sync
    _sync.subscribe("settings", lambda **_kw: invalidate())
except Exception:
    pass
