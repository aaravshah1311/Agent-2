# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2.core.pil
───────────────
The Personal Intelligence Layer (PIL).

A fully offline, privacy-first layer that sits *between the user and the selected
AI model*. It does NOT replace the model — it enhances what the user types,
learns from their natural behaviour, and personalizes over time. Nothing here
calls a network or a foundation model; all intelligence is local, deterministic
statistics over the user's own history.

Three modules, ONE shared brain
────────────────────────────────
All three modules read and write a single centralized memory (``pil.memory``)
through a single shared learning engine (``pil.learning``). A word learned while
typing is instantly usable by grammar and prompt-improvement — there are no
per-module stores.

    1. Smart Word Prediction   — real-time ghost-text autocomplete (never auto-
                                 inserts; user accepts with Tab / Right-Arrow)
    2. Grammar Enhancement     — opt-in; fixes spelling/grammar/spacing WITHOUT
                                 touching code, commands, paths, URLs, API names
    3. Prompt Improvement      — opt-in; enriches a request with the user's OWN
                                 proven preferences, never inventing requirements

This package is the public facade the rest of Agent2 uses. The agent loops call
``learn_from_message`` (passive learning) and ``process_outgoing_prompt``
(optional grammar + improvement) around every user turn; the web/CLI front-ends
call ``predict`` and ``observe_*`` for live autocomplete; the settings UI calls
``get_settings`` / ``update_settings`` / ``optimize`` / ``stats`` / ``wipe``.

Everything is defensive — a failure anywhere degrades to "do nothing" so the
agent can never be broken by the PIL.
"""

from agent2.database import get_setting, set_setting

from agent2.core.pil import memory as _memory
from agent2.core.pil import learning as _learning
from agent2.core.pil import prediction as _prediction
from agent2.core.pil import grammar as _grammar
from agent2.core.pil import improve as _improve
from agent2.core.pil import optimize as _optimize
from agent2.core.pil import pretraining as _pretrain

# ── Settings ──────────────────────────────────────────────────────────────────
# Persisted in the shared `settings` table so Web UI and CLI agree. The layer is
# ON by default for prediction + learning (strong out-of-box behaviour); the two
# prompt-mutating modules are OFF by default because they change the sent text
# and must be an explicit, opt-in choice.
_SETTINGS_DEFAULTS = {
    "pil.enabled": "1",       # master switch for the whole layer
    "pil.prediction": "1",    # Module 1 — live autocomplete
    "pil.learning": "1",      # shared passive learning engine
    "pil.grammar": "0",       # Module 2 — grammar enhancement (opt-in)
    "pil.improve": "0",       # Module 3 — prompt improvement (opt-in)
    "pil.autooptimize": "1",  # background optimization when idle
    "pil.seeded": "0",        # internal: cold-start seed already applied
}


def _flag(key: str) -> bool:
    return (get_setting(key, _SETTINGS_DEFAULTS.get(key, "0")) or "0") == "1"


def get_settings() -> dict:
    """Current PIL settings as booleans, plus live memory stats for the UI."""
    out = {k: _flag(k) for k in _SETTINGS_DEFAULTS if k != "pil.seeded"}
    out["stats"] = _memory.stats()
    return out


def update_settings(changes: dict) -> dict:
    """Apply a partial settings update (only recognised keys). Returns the new
    settings. Values are coerced to "1"/"0"."""
    for key, val in (changes or {}).items():
        if key not in _SETTINGS_DEFAULTS or key == "pil.seeded":
            continue
        set_setting(key, "1" if val in (True, 1, "1", "true", "True", "on") else "0")
    return get_settings()


def enabled() -> bool:
    """True when the master switch is on."""
    return _flag("pil.enabled")


# ── Passive learning (called by the agent loops on every user turn) ───────────

def learn_from_message(text: str, *, project: str = "") -> None:
    """Learn from a natural user message — the primary way the shared memory
    grows. Learns from the ORIGINAL text the user wrote (before any grammar /
    improvement), so personalization tracks real behaviour. Silent + best-effort.
    """
    if not enabled() or not _flag("pil.learning"):
        return
    try:
        _ensure_seeded()
        _learning.learn_prompt(text or "", project=project)
    except Exception:
        pass


# ── Live prediction (called by the front-ends per keystroke / debounce) ───────

def predict(text: str, *, project: str = "", lang: str = "") -> dict | None:
    """Return the best ghost-text suggestion for *text*, or None. Cheap, offline,
    and never mutates anything. Respects the master + prediction toggles."""
    if not enabled() or not _flag("pil.prediction"):
        return None
    try:
        _ensure_seeded()
        return _prediction.predict(text or "", project=project, lang=lang)
    except Exception:
        return None


def observe_accept(suggestion: str, *, context: str = "", project: str = "") -> None:
    """User accepted a suggestion (Tab / Right-Arrow) — passive positive signal."""
    if not enabled() or not _flag("pil.learning"):
        return
    try:
        _prediction.accept(suggestion, context=context, project=project)
    except Exception:
        pass


def observe_ignore(suggestion: str, *, context: str = "", project: str = "") -> None:
    """User typed past a suggestion — small passive negative signal."""
    if not enabled() or not _flag("pil.learning"):
        return
    try:
        _prediction.ignore(suggestion, context=context, project=project)
    except Exception:
        pass


def observe_delete(suggestion: str, *, context: str = "", project: str = "") -> None:
    """User accepted then deleted the text — strong passive negative signal."""
    if not enabled() or not _flag("pil.learning"):
        return
    try:
        _prediction.reject(suggestion, context=context, project=project)
    except Exception:
        pass


# ── Outgoing-prompt processing (grammar + improvement, both opt-in) ───────────

def process_outgoing_prompt(text: str, *, project: str = "") -> tuple[str, dict]:
    """Optionally transform a prompt just before it is sent to the AI model.

    Applies, in order and only when its toggle is on:
        1. Grammar Enhancement — safe spelling/grammar/spacing fixes
        2. Prompt Improvement  — enrichment with the user's proven preferences

    Returns ``(final_text, report)``. *report* describes exactly what changed so
    the UI can show it and the caller can keep the untouched original for
    display/history. Never raises — on any error the original text is returned
    unchanged. IMPORTANT: callers should learn from the ORIGINAL text, not this
    result, so learning reflects what the user actually wrote.
    """
    original = text or ""
    report = {
        "changed": False,
        "grammar_applied": False,
        "grammar_edits": [],
        "improve_applied": False,
        "improve_added": [],
        "original": original,
    }
    if not enabled() or not original.strip():
        report["final"] = original
        return original, report

    out = original
    try:
        if _flag("pil.grammar"):
            corrected, edits = _grammar.enhance(
                out, vocab_lookup=_vocab_known)
            if edits and corrected != out:
                out = corrected
                report["grammar_applied"] = True
                report["grammar_edits"] = edits
    except Exception:
        pass

    try:
        if _flag("pil.improve"):
            enriched, added = _improve.improve(out, project=project)
            if added and enriched != out:
                out = enriched
                report["improve_applied"] = True
                report["improve_added"] = added
    except Exception:
        pass

    report["changed"] = out != original
    report["final"] = out
    return out, report


def _vocab_known(token: str) -> bool:
    """True if *token* is in the user's learned vocabulary — used by grammar to
    spare personal jargon / project names from 'correction'."""
    try:
        return _memory.get_vocab(token) is not None
    except Exception:
        return False


# ── Background optimization (idle-time housekeeping) ──────────────────────────

def optimize(*, aggressive: bool = False, force: bool = False) -> dict:
    """Run the background optimization pass over the centralized memory. Respects
    the auto-optimize toggle unless *force* is set (manual 'Optimize now')."""
    if not force and (not enabled() or not _flag("pil.autooptimize")):
        return {"skipped": True}
    try:
        return _optimize.optimize(aggressive=aggressive)
    except Exception:
        return {"skipped": True, "error": True}


# ── Stats / privacy controls ──────────────────────────────────────────────────

def stats() -> dict:
    """Row counts for each centralized table."""
    try:
        return _memory.stats()
    except Exception:
        return {"vocab": 0, "phrases": 0, "ngrams": 0, "prefs": 0}


def wipe() -> None:
    """Erase the entire centralized memory ('forget me' privacy control) and allow
    the cold-start seed to be re-applied on next use."""
    try:
        _memory.wipe()
        set_setting("pil.seeded", "0")
    except Exception:
        pass


# ── Cold-start seed / pre-training ────────────────────────────────────────────
# So the layer is useful the moment it's installed — strong out-of-box behaviour
# that then personalizes from real usage. On first use the centralized memory is
# pre-trained from the bundled data files in ``seed_data/`` (see pretrain.py) via
# the SAME learning engine real usage uses. The inline lists below are a compact
# FALLBACK for installs that ship without the data files. Both add entries at LOW
# confidence / weight so the user's own proven data quickly outranks them.

_SEED_VOCAB = [
    # languages
    ("Python", "tech"), ("JavaScript", "tech"), ("TypeScript", "tech"),
    ("Java", "tech"), ("Rust", "tech"), ("Golang", "tech"), ("Kotlin", "tech"),
    ("Swift", "tech"), ("Ruby", "tech"), ("PHP", "tech"), ("HTML", "tech"),
    ("CSS", "tech"), ("SQL", "tech"), ("Bash", "tech"),
    # frameworks / libraries
    ("React", "tech"), ("Next.js", "tech"), ("Vue", "tech"), ("Angular", "tech"),
    ("Svelte", "tech"), ("Django", "tech"), ("Flask", "tech"), ("FastAPI", "tech"),
    ("Express", "tech"), ("Spring", "tech"), ("Tailwind", "tech"),
    ("Bootstrap", "tech"), ("PostgreSQL", "tech"), ("SQLite", "tech"),
    ("MongoDB", "tech"), ("Redis", "tech"), ("Docker", "tech"),
    ("Kubernetes", "tech"), ("Nginx", "tech"),
    # common instruction words
    ("Create", "word"), ("Implement", "word"), ("Generate", "word"),
    ("Refactor", "word"), ("Optimize", "word"), ("Analyze", "word"),
    ("Explain", "word"), ("Debug", "word"), ("Function", "word"),
    ("Component", "word"), ("Endpoint", "word"), ("Database", "word"),
    ("Responsive", "word"), ("Authentication", "word"),
]

_SEED_PHRASES = [
    "create a responsive login page",
    "write a function that",
    "implement a rest api",
    "add error handling to",
    "fix the bug in",
    "explain how this works",
    "optimize this code for performance",
    "write unit tests for",
    "refactor this to be more readable",
    "add dark mode support",
]

_SEED_NGRAMS = [
    ("create", "a"), ("create a", "responsive"), ("write", "a"),
    ("write a", "function"), ("a", "function"), ("function", "that"),
    ("implement", "a"), ("add", "error"), ("error", "handling"),
    ("fix", "the"), ("the", "bug"), ("unit", "tests"), ("dark", "mode"),
]


def _ensure_seeded() -> None:
    """Apply the cold-start pre-training exactly once (idempotent, guarded by a
    setting). Prefers the rich CSV pre-training in ``seed_data/``; falls back to
    the compact inline seed if those data files aren't present."""
    if _flag("pil.seeded"):
        return
    try:
        if _pretrain.has_data():
            _pretrain.run()
        else:
            _apply_inline_seed()
        set_setting("pil.seeded", "1")
    except Exception:
        # Leave the flag unset so a later call can retry the seed.
        pass


def _apply_inline_seed() -> None:
    """Fallback cold-start seed used only when the bundled data files are absent."""
    for token, kind in _SEED_VOCAB:
        _memory.bump_vocab(token, kind=kind, freq=1)
        _memory.set_vocab_confidence(token, 0.4)
    for body in _SEED_PHRASES:
        _memory.bump_phrase(body, freq=1)
        _memory.set_phrase_confidence(body, 0.4)
    for prev, nxt in _SEED_NGRAMS:
        _memory.bump_ngram(prev, nxt, freq=1)
        _memory.set_ngram_confidence(prev, nxt, 0.4)


def pretrain(*, force: bool = False) -> dict:
    """Manually (re)run cold-start pre-training from the bundled data files. Useful
    after editing the CSVs in ``seed_data/`` or to warm a fresh memory on demand.
    With *force*, re-applies even if the seed flag is already set. Returns the
    per-file row counts from the loader."""
    try:
        if not force and _flag("pil.seeded"):
            return {"skipped": True}
        counts = _pretrain.run()
        set_setting("pil.seeded", "1")
        return counts
    except Exception:
        return {"skipped": True, "error": True}


__all__ = [
    "enabled",
    "get_settings",
    "learn_from_message",
    "observe_accept",
    "observe_delete",
    "observe_ignore",
    "optimize",
    "predict",
    "pretrain",
    "process_outgoing_prompt",
    "stats",
    "update_settings",
    "wipe",
]
