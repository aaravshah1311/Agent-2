# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/llm/router.py
────────────────────
WHICH MODEL RUNS THIS TURN, AND WHAT RUNS INSTEAD WHEN IT FAILS (Tasks 18, 19).

Selection and fallback live in one module on purpose. Both have to rank the same
candidates by the same capability rules, and two copies of that ranking would
diverge in the worst possible way: the router would pick a model the fallback
chain considered unusable, or vice versa, and each half would look correct in
isolation. `_rank()` is the one ordering; `choose()` and `next_model()` are two
questions asked of it.

⚠️ EXPLICIT USER SELECTION WINS. THAT IS THE FIRST RULE, NOT A COURTESY.
Both surfaces send a model key on every turn — the default one when the user has
never picked. A router that ran unasked could not tell "the user chose 2.5-flash"
from "nobody chose anything", so routing is opt-in through `config.MODEL_ROUTING`:

    off (default)  — route only when the user selected the `auto` pseudo-model
    default_only   — also route when the turn arrived on DEFAULT_MODEL
    always         — route even over an explicit pick (the "configured otherwise")

`AUTO` is a real, user-selectable key (`/model auto`) precisely so that asking for
automatic routing is itself an explicit choice.

⚠️ `choose()` CAN NEVER RETURN NOTHING.
Every filter is applied and then checked: if a hard requirement empties the
candidate list, the requested model comes back unchanged with the reason recorded.
A router that returns `""` on an unusual turn is a router that breaks the app, and
the failure would land as "no model configured" — an error about the wrong thing
entirely.

⚠️ AN UNKNOWN CAPABILITY IS NOT A "NO", EXCEPT WHERE IT MUST BE.
`capabilities.get()` answers `None` for "we do not know" (see its docstring). A
HARD requirement reads unknown as unusable — an image cannot be sent hopefully. A
PREFERENCE only deranks — refusing a model because nobody filled in its record
would make every custom provider permanently unroutable.

FALLBACK (Task 19)
──────────────────
⚠️ WHICH FAILURES DESERVE A DIFFERENT MODEL IS A JUDGEMENT, AND IT IS MADE HERE.
`auth` never falls back: a rejected credential is not the model's fault, and trying
four models against a bad key turns one clear error into four confusing ones — the
key rotator is what handles that. `quota` does fall back, because Gemini's per-model
limits are separate buckets, so a flash model can answer while pro is walled.
`invalid_model` obviously does. `transient` does, but only after
`resilience.call_with_retry` has already exhausted its in-place retries — the model
is then plausibly down rather than briefly busy.

⚠️ THE HOP BUDGET IS PER TURN, NOT PER MODEL. Counting per model would let a third
model reset the ceiling, and "do not endlessly retry failed providers" would hold
for each provider while failing for the turn.

⚠️ SESSION AND TASK STATE ARE PRESERVED BY *NOT TOUCHING THEM*. A fallback is a
`continue` in the existing agent loop with a new model id — the same shape the
quota key-rotation path already uses. The context list, token tally, `ToolContext`,
checkpoint trail and cancel token all survive because nothing in this module knows
they exist. Anything that restarted the turn would replay completed tool calls,
which is exactly what rule 20 forbids.

Layer: config · capabilities → router → agent.py · provider_agent.py · surfaces.
"""

from __future__ import annotations

import threading
import time

from agent2 import config as _cfg
from agent2.config import DEFAULT_MODEL, MODELS
from agent2.core import logging as audit
from agent2.llm import capabilities as caps

#: The user-selectable pseudo-model meaning "decide per turn".
#:
#: ⚠️ Deliberately NOT a member of `config.MODELS`. That dict is the list of things
#: that can be *called*, and `auto` cannot be called — an entry there would reach
#: `MODELS[key]["api"]` and be sent to the vendor as a model id. Every surface that
#: offers it treats it as a sibling of that list, never a member.
AUTO = "auto"

ROUTING_OFF = "off"
ROUTING_DEFAULT_ONLY = "default_only"
ROUTING_ALWAYS = "always"
ROUTING_MODES: tuple[str, ...] = (ROUTING_OFF, ROUTING_DEFAULT_ONLY, ROUTING_ALWAYS)

#: Where a decision came from. Recorded on every Decision so a surface can say
#: *why* a model was used — "you chose it" and "we chose it" must never look alike.
SRC_USER = "user"
SRC_AUTO = "auto"
SRC_DEFAULT = "default"
SRC_FALLBACK = "fallback"

#: Failure kinds (from `resilience.classify_error`) that justify another model.
#:
#: ⚠️ TWO DELIBERATE ABSENCES, AND BOTH ARE THE POINT OF THE SET.
#: `auth` is absent because a rejected credential is not the model's fault — see
#: the module docstring. The empty string (an *unclassified* error) is absent
#: because "we could not tell what went wrong" is not evidence that the model is at
#: fault either: a malformed request, a serialisation bug or a schema the vendor
#: rejected will fail identically on every model, and falling back would spend the
#: user's quota twice to reach the same error with a longer message. Adding `""`
#: here is a one-character change that makes every unclassified failure cost three
#: API calls instead of one.
FALLBACK_KINDS: frozenset = frozenset({"quota", "invalid_model", "transient"})


def routing_mode() -> str:
    """The active routing policy. Unknown values fall back to `off`.

    Read on every call, never captured: a `/settings` toggle has to take effect on
    the next turn, and the CLI and web halves are separate processes over one DB.
    Precedence is env → stored setting → off, matching how every other operator
    switch in this repo resolves.
    """
    raw = (_cfg.MODEL_ROUTING or "").strip().lower()
    if raw not in ROUTING_MODES:
        raw = ""
    if not raw:
        try:
            from agent2.database import qone
            row = qone("SELECT value FROM settings WHERE key='model.routing'")
            stored = str((row or {}).get("value") or "").strip().lower()
            if stored in ROUTING_MODES:
                return stored
        except Exception:
            pass
    return raw or ROUTING_OFF


def set_routing_mode(mode: str) -> str:
    """Persist the routing policy. Returns what is now in force."""
    text = str(mode or "").strip().lower()
    if text not in ROUTING_MODES:
        raise ValueError(f"routing must be one of {ROUTING_MODES!r}")
    try:
        from agent2.database import exe
        exe("INSERT OR REPLACE INTO settings(key, value) VALUES('model.routing', ?)",
            (text,))
    except Exception as exc:
        raise RuntimeError(f"could not store routing mode: {str(exc)[:120]}") from exc
    try:
        from agent2.core import sync
        sync.notify("settings", routing=text)
    except Exception:
        pass
    return routing_mode()


# ── Signals ───────────────────────────────────────────────────────────────────
_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff",
                   ".heic", ".heif", ".avif")

#: Words that mark a turn as *reasoning-heavy*. Kept small and specific: a long
#: keyword list looks thorough and mostly adds false positives, and a false
#: positive here costs the user real money on a pro model.
_REASONING_MARKERS = (
    "why does", "why is", "root cause", "race condition", "deadlock",
    "traceback", "stack trace", "segfault", "memory leak", "debug",
    "architecture", "trade-off", "tradeoff", "refactor", "design a",
    "prove", "explain how", "step by step", "regression", "intermittent",
    "flaky", "reason about", "edge case",
)

_CODING_MARKERS = (
    "```", "def ", "class ", "function ", "import ", "#include", "=>",
    "async ", "await ", "select ", "npm ", "pip ", "git ", "docker ",
    "implement", "write a test", "unit test", "compile", "typescript",
    "python", "javascript", "rust", "golang", ".py", ".js", ".ts", ".go",
    ".rs", ".java", ".c", ".cpp", ".sql", "patch", "diff",
)

#: A turn is "simple" only when it is short AND shows none of the above. Length
#: alone is a bad signal — "why is this flaky?" is five words of hard work.
_SIMPLE_MAX_CHARS = 180


def estimate_tokens(text: str) -> int:
    """Rough prompt size in tokens: characters ÷ 4.

    ⚠️ Deliberately an estimate and never a vendor tokenizer call. Counting exactly
    would mean a network round trip (or a model-specific tokenizer) before we know
    which model to use — a circular dependency for a number whose only job is to
    clear a threshold by a wide margin.
    """
    return max(0, len(str(text or "")) // 4)


def analyze(message: str = "", *, attachments=None, context_tokens: int = 0) -> dict:
    """The signals a routing decision is made from. Pure, cheap, never raises."""
    text = str(message or "")
    low = text.lower()
    atts = list(attachments or [])

    has_image = False
    for att in atts:
        name = ""
        if isinstance(att, dict):
            name = str(att.get("name") or att.get("path") or "")
            mime = str(att.get("mime") or att.get("mime_type") or "").lower()
            if mime.startswith("image/"):
                has_image = True
                break
        else:
            name = str(att or "")
        if name.lower().endswith(_IMAGE_SUFFIXES):
            has_image = True
            break

    tokens = max(int(context_tokens or 0), estimate_tokens(text))
    coding = any(m in low for m in _CODING_MARKERS)
    reasoning = any(m in low for m in _REASONING_MARKERS)
    return {
        "tokens": tokens,
        "vision": has_image,
        "coding": coding,
        "reasoning": reasoning,
        "long_context": tokens >= int(_cfg.ROUTER_LONG_CONTEXT),
        "simple": (len(text) <= _SIMPLE_MAX_CHARS and not coding
                   and not reasoning and not has_image),
        "attachments": len(atts),
    }


# ── Candidate ordering ────────────────────────────────────────────────────────
def candidates(*, include_custom: bool = True, skip_cooling: bool = True) -> list[str]:
    """Model keys eligible right now, in config order.

    ⚠️ `skip_cooling` is what makes the breaker real. A cooling model that stayed in
    this list would be re-picked on the very next turn, and "do not endlessly retry
    failed providers" would be a comment rather than a behaviour.

    ⚠️ It can still return a cooling model — as the LAST resort, when every model is
    cooling. An empty candidate list is not a safer state than a bruised one.
    """
    keys = list(MODELS)
    if include_custom:
        try:
            from agent2.llm import providers as _providers
            keys += ["custom:" + str(r["id"]) for r in _providers.list_providers(safe=True)]
        except Exception:
            pass
    if not skip_cooling:
        return keys
    warm = [k for k in keys if not cooling(k)]
    return warm or keys


def _rank(key: str, signals: dict) -> tuple:
    """The sort key for one candidate. Higher tuples win.

    THE ONE ORDERING. `choose()` and `next_model()` both use it, which is why a
    model the router likes is never one the fallback chain considers unusable.

    Read top to bottom as "what matters most on this turn":
      1. context window, but only when the turn is actually large
      2. reasoning tier, when the turn looks like reasoning
      3. coding tier, when the turn looks like code
      4. speed then cheapness, when the turn looks simple
      5. capability breadth, as a mild tiebreak so a described model beats a blank
      6. version, as the LAST tiebreak — see `capabilities.version_hint`

    ⚠️ Step 6 is last for a reason. Two models the registry rates identically (a
    user with both `claude-opus-5` and `claude-opus-4-8` registered) would otherwise
    be separated by registration order, which is not a preference anybody expressed.
    It must never outrank a real capability difference, which is why it sits below
    every graded field rather than beside them.
    """
    rec = caps.get(key)
    window = int(rec.get("context_window") or 0)

    fit_context = 0
    if signals.get("long_context"):
        need = int(signals.get("tokens") or 0)
        # ⚠️ An unknown window (0) scores 0 here, never "big enough". Routing a
        # 200k-token turn at a model whose limit nobody recorded is a guess that
        # fails as a vendor error mid-turn.
        fit_context = 2 if window >= need * 2 else (1 if window >= need else 0)

    reasoning = caps.tier_rank(rec.get("reasoning")) if signals.get("reasoning") else 0
    coding = caps.tier_rank(rec.get("coding")) if signals.get("coding") else 0
    speed = caps.speed_rank(rec.get("speed")) if signals.get("simple") else 0
    cheap = (4 - caps.cost_rank(rec.get("cost"))) if signals.get("simple") else 0
    breadth = sum(1 for f in ("vision", "tool_use", "structured_output")
                  if rec.get(f) is True)
    version = caps.version_hint(rec.get("model") or key)
    return (fit_context, reasoning, coding, speed, cheap, breadth, version)


def rank_candidates(signals: dict | None = None, *, exclude=(),
                    include_custom: bool = True,
                    skip_cooling: bool = True) -> list[str]:
    """Every eligible model, best fit first. THE public ordering.

    Exposed rather than kept private because three callers need it and two of them
    are not `next_model`: `choose()` takes the head, `next_model()` takes the head
    after exclusions, and the CLI's `_fallback_order` needs the whole list (it has
    always tried every model before giving up, and that is not a hop-budget path).
    A private `_rank` with three call sites reimplementing the sort is how the web
    loop and the CLI would come to disagree about which model to fall back to.

    `sorted` is stable, so config order breaks ties — an unmotivated turn lands on
    the first configured model rather than an arbitrary one.
    """
    skip = {str(x) for x in (exclude or ()) if x}
    pool = [k for k in candidates(include_custom=include_custom,
                                  skip_cooling=skip_cooling) if k not in skip]
    return sorted(pool, key=lambda k: _rank(k, signals or {}), reverse=True)


class Decision:
    """What `choose()` answers: a model key plus why.

    An object rather than a bare string because three callers need three different
    parts of it — the agent loop wants `model_key`, the surfaces want `reason` to
    show the user, and the ledger wants `signals` to make a past decision
    reconstructible.
    """

    __slots__ = ("model_key", "reason", "requested", "signals", "source")

    def __init__(self, model_key: str, *, source: str, reason: str = "",
                 requested: str = "", signals: dict | None = None):
        self.model_key = model_key
        self.source = source
        self.reason = reason
        self.requested = requested
        self.signals = signals or {}

    @property
    def routed(self) -> bool:
        """True when Agent2 chose, rather than the user."""
        return self.source == SRC_AUTO

    def as_dict(self) -> dict:
        return {"model": self.model_key, "source": self.source,
                "reason": self.reason, "requested": self.requested,
                "signals": dict(self.signals), "routed": self.routed}

    def __repr__(self) -> str:                        # pragma: no cover - debug
        return f"<Decision {self.model_key} via {self.source}: {self.reason}>"


def _label(key: str) -> str:
    """A name a human recognises. `custom:6c7c53a4` is not one.

    An opaque provider id in a reason line ("routed to custom:6c7c53a4") tells the
    user nothing about *which* of their four registered endpoints ran — which is the
    only thing they wanted to know. The model id they typed is the name they think
    in, so it goes in brackets after the key.
    """
    if not key.startswith("custom:"):
        return key
    model = str(caps.get(key).get("model") or "")
    return f"{key} ({model})" if model and model != key else key


def _explain(key: str, signals: dict) -> str:
    """A one-line human reason. Shown in the CLI and the web topbar.

    Says the SIGNAL, not the score: "large context (187k tokens)" is actionable and
    "rank (2,3,0,0,0,3)" is not, and a reason nobody can act on is a reason nobody
    reads.
    """
    rec = caps.get(key)
    name = _label(key)
    if signals.get("vision"):
        return f"image attached → {name} handles vision"
    if signals.get("long_context"):
        window = int(rec.get("context_window") or 0)
        return (f"large context ({signals.get('tokens', 0):,} tokens est.) → {name}"
                + (f" ({window:,}-token window)" if window else ""))
    if signals.get("reasoning"):
        return (f"looks like debugging/analysis → {name} "
                f"({rec.get('reasoning') or 'unrated'} reasoning)")
    if signals.get("coding"):
        return (f"looks like coding → {name} "
                f"({rec.get('coding') or 'unrated'} coding)")
    if signals.get("simple"):
        return f"short question → {name} (fastest, cheapest)"
    return f"no strong signal → {name}"


def choose(requested: str = "", *, message: str = "", attachments=None,
           context_tokens: int = 0, allow_custom: bool = True) -> Decision:
    """Decide the model for this turn.

    ⚠️ TOTAL. Never returns an empty model key, never raises, and never picks
    something the caller cannot call. When routing is off, or when no candidate
    beats the request, the requested key comes straight back.
    """
    asked = str(requested or "").strip()
    signals = analyze(message, attachments=attachments, context_tokens=context_tokens)
    mode = routing_mode()

    # Should we route at all? Three ways in, and every one of them is an explicit
    # consent — the user picked `auto`, or an operator set the policy. Written as a
    # single predicate so the "do not route" branch is the one that returns, which
    # is the safer shape: a future condition added in the wrong place cannot make
    # routing the fall-through default.
    may_route = (
        asked == AUTO
        or mode == ROUTING_ALWAYS
        or (mode == ROUTING_DEFAULT_ONLY and asked in ("", DEFAULT_MODEL))
    )
    if not may_route:
        key = asked or DEFAULT_MODEL
        return Decision(key, requested=asked, signals=signals,
                        source=SRC_USER if asked else SRC_DEFAULT,
                        reason=("selected by you" if asked else "default model"))

    pool = candidates(include_custom=allow_custom)

    # Hard requirements. Applied, then CHECKED — a filter that empties the pool is
    # discarded rather than obeyed, because "no model at all" is never the better
    # answer. The `_reason` records that we could not honour it, so a turn that
    # needed vision and got a model without it is explainable afterwards.
    unmet = ""
    if signals.get("vision"):
        with_vision = [k for k in pool if caps.supports(k, "vision", default=False)]
        if with_vision:
            pool = with_vision
        else:
            unmet = "no candidate is known to accept images"
    with_tools = [k for k in pool if caps.supports(k, "tool_use", default=False)]
    if with_tools:
        pool = with_tools
    elif not unmet:
        unmet = "no candidate is known to support tool calls"

    if not pool:
        key = asked if asked and asked != AUTO else DEFAULT_MODEL
        return Decision(key, source=SRC_DEFAULT, requested=asked, signals=signals,
                        reason="no candidate models available")

    # Stable: `sorted` keeps config order among equals, so an unmotivated turn
    # lands on the first configured model rather than an arbitrary one.
    best = rank_candidates(signals, include_custom=allow_custom,
                           skip_cooling=True)
    best = [k for k in best if k in pool] or pool
    best = best[0]
    reason = _explain(best, signals)
    if unmet:
        reason = f"{reason} — note: {unmet}"
    audit.event("router.choose", model=best, requested=asked or "-",
                mode=mode, tokens=signals.get("tokens", 0),
                vision=signals.get("vision"), coding=signals.get("coding"),
                reasoning=signals.get("reasoning"))
    return Decision(best, source=SRC_AUTO, requested=asked, signals=signals,
                    reason=reason)


def resolve(model_key: str) -> str:
    """Turn a possibly-`auto` key into something callable.

    The narrow guard for call sites that are NOT starting a turn — a chat row that
    stored `auto`, a `/model` echo, a health payload. `choose()` is what a turn
    uses; this exists so nothing accidentally sends `"auto"` to a vendor as a model
    id, which is the one failure mode `AUTO` introduces.
    """
    key = str(model_key or "").strip()
    if not key or key == AUTO:
        return DEFAULT_MODEL
    return key


# ══════════════════════════════════════════════════════════════════════════════
# Task 19 — fallback, the breaker, and the ledger
# ══════════════════════════════════════════════════════════════════════════════
_breaker_lock = threading.Lock()
#: model key → list of monotonic failure timestamps inside the window.
_failures: dict[str, list[float]] = {}
#: model key → monotonic time at which the cooldown ends.
_cooling_until: dict[str, float] = {}


def note_failure(model_key: str, kind: str = "") -> bool:
    """Record a failure. Returns True if this tripped the breaker.

    ⚠️ `auth` failures are counted but never trip the breaker. A bad or exhausted
    credential fails every model identically, so letting it cool them one by one
    would take the whole app offline over a problem in one API key — and the key
    rotator is the thing that actually fixes it.
    """
    key = str(model_key or "")
    if not key:
        return False
    now = time.monotonic()
    window = float(_cfg.BREAKER_WINDOW)
    with _breaker_lock:
        hits = [t for t in _failures.get(key, []) if now - t <= window]
        hits.append(now)
        _failures[key] = hits
        if kind == "auth":
            return False
        if len(hits) >= int(_cfg.BREAKER_FAILS):
            _cooling_until[key] = now + float(_cfg.BREAKER_COOLDOWN)
            _failures[key] = []
            # `failure=`, not `kind=`: `audit.event`'s first positional parameter
            # IS `kind`, so passing it as a keyword raises TypeError from inside a
            # failure handler — turning a recoverable model error into a crash.
            audit.event("router.breaker.open", model=key, failure=kind or "-",
                        cooldown=int(_cfg.BREAKER_COOLDOWN))
            return True
    return False


def note_success(model_key: str) -> None:
    """Clear a model's failure history.

    Immediate and unconditional: the breaker exists to stop a hammering loop, not
    to punish a model for one bad minute. A model that just answered is working, and
    any other reading is a policy the user did not ask for.
    """
    key = str(model_key or "")
    if not key:
        return
    with _breaker_lock:
        _failures.pop(key, None)
        if _cooling_until.pop(key, None) is not None:
            audit.event("router.breaker.close", model=key)


def cooling(model_key: str) -> bool:
    """Whether *model_key* is inside its cooldown."""
    key = str(model_key or "")
    with _breaker_lock:
        until = _cooling_until.get(key)
        if until is None:
            return False
        if time.monotonic() >= until:
            _cooling_until.pop(key, None)
            return False
        return True


def breaker_state() -> dict:
    """Diagnostic snapshot for `/api/health` and the metrics surface."""
    now = time.monotonic()
    with _breaker_lock:
        return {
            "cooling": {k: round(max(0.0, v - now), 1)
                        for k, v in _cooling_until.items()},
            "recent_failures": {k: len(v) for k, v in _failures.items() if v},
        }


def reset_breaker() -> None:
    with _breaker_lock:
        _failures.clear()
        _cooling_until.clear()


def next_model(current: str, *, kind: str = "", tried=None,
               signals: dict | None = None, allow_custom: bool = True) -> str:
    """The model to try after *current* failed, or `""` to give up.

    ⚠️ `""` IS A REAL ANSWER AND CALLERS MUST HONOUR IT. It means "no further model
    is worth trying", and a caller that treated it as "keep going" would be the
    endless retry Task 19 forbids. Four ways to get it: the failure kind does not
    justify a switch (`auth`), the hop budget is spent, every alternative has been
    tried, or there is genuinely nothing else configured.
    """
    if str(kind or "") not in FALLBACK_KINDS:
        return ""
    seen = {str(t) for t in (tried or ()) if t}
    seen.add(str(current or ""))
    if len(seen) - 1 >= int(_cfg.FALLBACK_MAX_HOPS):
        return ""

    pool = [k for k in candidates(include_custom=allow_custom) if k not in seen]
    if not pool:
        return ""
    ranked = rank_candidates(signals, exclude=seen, include_custom=allow_custom)
    return ranked[0] if ranked else ""


def record_attempt(*, model: str, ok: bool, latency_ms: int = 0,
                   primary_model: str = "", fallback_of: str = "",
                   failure_kind: str = "", failure_reason: str = "",
                   chat_id: str = "", session_id: str = "") -> None:
    """Append one model-call outcome to the ledger, then trim it.

    ⚠️ Never raises and never blocks a turn. This is a record of what happened; a
    turn that failed to write its own history is still a turn that worked, and a
    ledger write that could abort a reply would be a diagnostic causing outages.

    ⚠️ The trim happens HERE, on the writer, for the reason the DDL states: an
    unbounded per-call log on a busy dual-mode install grows the database forever.
    """
    try:
        from agent2.database import exe
        exe("INSERT INTO model_attempts(chat_id, session_id, primary_model, model,"
            " fallback_of, ok, failure_kind, failure_reason, latency_ms)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (str(chat_id or "")[:64], str(session_id or "")[:64],
             str(primary_model or model or "")[:64], str(model or "")[:64],
             str(fallback_of or "")[:64], 1 if ok else 0,
             str(failure_kind or "")[:32], str(failure_reason or "")[:300],
             max(0, int(latency_ms or 0))))
        exe("DELETE FROM model_attempts WHERE id NOT IN ("
            " SELECT id FROM model_attempts ORDER BY id DESC LIMIT ?)",
            (int(_cfg.ROUTER_LEDGER_MAX),))
    except Exception:
        pass


def attempts(limit: int = 50, *, chat_id: str = "") -> list[dict]:
    """The most recent ledger rows, newest first."""
    try:
        from agent2.database import qall
        if chat_id:
            return qall("SELECT * FROM model_attempts WHERE chat_id=?"
                        " ORDER BY id DESC LIMIT ?",
                        (str(chat_id), max(1, int(limit))))
        return qall("SELECT * FROM model_attempts ORDER BY id DESC LIMIT ?",
                    (max(1, int(limit)),))
    except Exception:
        return []


def stats() -> dict:
    """Aggregate model-call health. Counters only — no prompts, no keys."""
    out = {"total": 0, "ok": 0, "failed": 0, "fallbacks": 0,
           "avg_latency_ms": 0, "by_model": {}}
    try:
        from agent2.database import qall
        row = qall("SELECT COUNT(*) AS n, SUM(ok) AS good,"
                   " SUM(CASE WHEN fallback_of<>'' THEN 1 ELSE 0 END) AS fb,"
                   " AVG(latency_ms) AS lat FROM model_attempts")
        if row:
            r = row[0]
            out["total"] = int(r.get("n") or 0)
            out["ok"] = int(r.get("good") or 0)
            out["failed"] = out["total"] - out["ok"]
            out["fallbacks"] = int(r.get("fb") or 0)
            out["avg_latency_ms"] = int(r.get("lat") or 0)
        for r in qall("SELECT model, COUNT(*) AS n, SUM(ok) AS good"
                      " FROM model_attempts GROUP BY model"):
            out["by_model"][str(r["model"])] = {
                "calls": int(r.get("n") or 0), "ok": int(r.get("good") or 0)}
    except Exception:
        pass
    out["breaker"] = breaker_state()
    return out


def describe() -> dict:
    """Serialisable snapshot for `/api/models` and `/api/health`."""
    return {
        "routing": routing_mode(),
        "routing_modes": list(ROUTING_MODES),
        "auto_key": AUTO,
        "max_hops": int(_cfg.FALLBACK_MAX_HOPS),
        "long_context_threshold": int(_cfg.ROUTER_LONG_CONTEXT),
        "candidates": candidates(skip_cooling=False),
        "breaker": breaker_state(),
    }
