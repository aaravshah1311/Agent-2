# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2.core.pil.learning
─────────────────────────
THE single shared learning engine that powers all three PIL modules.

It never asks the user whether a prediction was correct. It learns *passively*
from natural behaviour and writes everything into the ONE centralized memory
(agent2.core.pil.memory). Because every module feeds the same engine, a word
learned while typing is instantly available to grammar and prompt-improvement.

Passive signals (see `feedback` / `observe`):

    accept   — user took a suggestion (Tab / Right-Arrow)  → confidence up
    ignore   — user kept typing past a suggestion          → confidence down (small)
    delete   — user accepted then removed the text         → confidence down (large)
    use      — user naturally wrote a word/phrase           → frequency + rank up

Confidence deltas are intentionally asymmetric: acceptance is rewarded modestly,
deletion is punished hard, so a suggestion that gets accepted-then-deleted nets
out negative and stops being offered.
"""

import re

from agent2.core.pil import memory as mem
from agent2.core.pil.memory import tokenize, CONF_MIN, CONF_MAX

# ── Passive-signal confidence deltas ──────────────────────────────────────────
DELTA_ACCEPT = +0.08     # Tab / Right-Arrow acceptance
DELTA_IGNORE = -0.03     # user typed past the suggestion
DELTA_DELETE = -0.20     # accepted then deleted → strong negative

# Words too common to be worth learning as personal vocabulary. Kept tiny — the
# engine leans on frequency/confidence rather than a big stopword list.
_STOP = {
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "of", "for",
    "is", "are", "was", "were", "be", "with", "as", "by", "it", "this", "that",
    "i", "you", "we", "they", "me", "my", "your", "our",
}

# Signals that a token is a technical term / identifier worth tagging as "tech":
# camelCase, snake_case, dotted or hyphenated names, or an internal digit.
_TECH_RE = re.compile(r"[a-z][A-Z]|_|\.|[A-Za-z]-[A-Za-z]|[A-Za-z]\d")


def _kind_of(token: str) -> str:
    """Best-effort classification of a raw token for pil_vocab.kind."""
    if _TECH_RE.search(token):
        return "tech"
    return "word"


def _clamp(v: float) -> float:
    return max(CONF_MIN, min(CONF_MAX, v))


# ── Passive learning from natural text ────────────────────────────────────────

def learn_text(text: str, *, project: str = "", lang: str = "") -> None:
    """Learn from a full piece of natural user text (e.g. a sent prompt).

    Updates vocabulary frequency, n-gram transitions, and — when the text looks
    like a reusable instruction — the phrase store. This is the primary way the
    centralized memory grows: every prompt the user sends flows through here.
    """
    text = (text or "").strip()
    if not text:
        return

    toks = tokenize(text)
    vocab_rows: list[tuple] = []
    for t in toks:
        low = t.lower()
        if len(low) < 2 or low in _STOP:
            # Still let short tech tokens through (e.g. "go", "js") only if they
            # look technical; otherwise skip common filler.
            if not _TECH_RE.search(t) and low not in ("c", "r", "go", "js"):
                continue
        vocab_rows.append((t, _kind_of(t), lang, project, 1))

    # N-grams: unigram and bigram context → next token. Powers next-word ranking.
    lowered = [t.lower() for t in toks]
    ngram_rows: list[tuple] = []
    for i in range(len(toks) - 1):
        ngram_rows.append((lowered[i], toks[i + 1], 1))
        if i + 2 <= len(toks) - 1:
            ngram_rows.append((f"{lowered[i]} {lowered[i + 1]}", toks[i + 2], 1))

    # Phrase learning: sliding windows of 3–6 tokens become candidate phrases so
    # frequently-repeated instructions ("create a responsive login page") get
    # promoted over time. Only the leading window is stored per length to keep
    # writes bounded; background optimization merges/prunes the rest.
    phrase_rows: list[tuple] = [
        (" ".join(toks[:n]), project, 1)
        for n in (6, 5, 4, 3)
        if len(toks) >= n
    ]

    # Everything above used to run as one upsert per row — up to ~90 separate
    # commits for a long prompt. The batch variants below do the identical writes
    # (same SQL, same confidence math) in a handful of transactions.
    mem.bump_vocab_many(vocab_rows)
    mem.bump_ngram_many(ngram_rows)
    mem.bump_phrase_many(phrase_rows)


def learn_prompt(text: str, *, project: str = "") -> None:
    """Learn from a sent prompt and, additionally, mine lightweight preference
    signals for Module 3 (prompt improvement). Non-destructive: only records
    preferences the user demonstrably expressed."""
    learn_text(text, project=project)
    _mine_preferences(text)


# Keyword → (category, canonical value) map for cheap, deterministic preference
# mining. This is NOT hallucination: a preference is only recorded when the user
# literally wrote the keyword. The learning engine then ranks by proven weight.
_PREF_KEYWORDS: dict[str, tuple[str, str]] = {
    # frameworks
    "react": ("framework", "React"), "next.js": ("framework", "Next.js"),
    "nextjs": ("framework", "Next.js"), "vue": ("framework", "Vue"),
    "angular": ("framework", "Angular"), "svelte": ("framework", "Svelte"),
    "django": ("framework", "Django"), "flask": ("framework", "Flask"),
    "fastapi": ("framework", "FastAPI"), "express": ("framework", "Express"),
    "spring": ("framework", "Spring"),
    # libraries / styling
    "tailwind": ("library", "Tailwind CSS"), "bootstrap": ("library", "Bootstrap"),
    "typescript": ("language", "TypeScript"), "python": ("language", "Python"),
    "rust": ("language", "Rust"), "golang": ("language", "Go"),
    # styles
    "responsive": ("style", "responsive design"),
    "dark": ("style", "dark mode"), "accessible": ("style", "accessibility support"),
    "accessibility": ("style", "accessibility support"),
    "mobile-first": ("style", "mobile-first design"),
    "minimal": ("style", "minimal design"), "modern": ("style", "modern design"),
    # response length
    "concise": ("length", "concise"), "detailed": ("length", "detailed"),
    "brief": ("length", "concise"), "thorough": ("length", "detailed"),
}


def _mine_preferences(text: str) -> None:
    low = f" {text.lower()} "
    seen: set[tuple[str, str]] = set()
    for kw, (cat, val) in _PREF_KEYWORDS.items():
        if (cat, val) in seen:
            continue
        if re.search(rf"(?<![\w-]){re.escape(kw)}(?![\w-])", low):
            mem.bump_pref(cat, val)
            seen.add((cat, val))


# ── Passive feedback from suggestion interactions ─────────────────────────────

def feedback(kind: str, suggestion: str, *, context: str = "",
             project: str = "") -> None:
    """Apply a passive-learning signal for a shown suggestion.

    *kind* is one of "accept" | "ignore" | "delete". *suggestion* is the text
    that was offered (a word, phrase tail, or whole phrase). This is the ONLY
    place suggestion confidence moves from user interaction — silently, with no
    prompts. Unknown *kind* is a no-op.
    """
    delta = {"accept": DELTA_ACCEPT, "ignore": DELTA_IGNORE,
             "delete": DELTA_DELETE}.get(kind)
    if delta is None:
        return
    suggestion = (suggestion or "").strip()
    if not suggestion:
        return

    # A multi-word suggestion is a phrase; a single token is vocab. On accept we
    # also strengthen frequency so proven suggestions rank up over time.
    if " " in suggestion:
        row = mem.get_phrase(suggestion)
        cur = row["confidence"] if row else 0.5
        mem.set_phrase_confidence(suggestion, _clamp(cur + delta))
        if kind == "accept":
            mem.bump_phrase(suggestion, project=project)
    else:
        row = mem.get_vocab(suggestion)
        cur = row["confidence"] if row else 0.5
        mem.set_vocab_confidence(suggestion, _clamp(cur + delta))
        if kind == "accept":
            mem.bump_vocab(suggestion, kind=_kind_of(suggestion), project=project)
        # Reinforce the n-gram edge that produced this word.
        if context:
            ctx_tail = tokenize(context)
            if ctx_tail:
                prev = ctx_tail[-1].lower()
                nrow = next((n for n in mem.ngram_next(prev)
                             if n["next"] == suggestion), None)
                ncur = nrow["confidence"] if nrow else 0.5
                mem.set_ngram_confidence(prev, suggestion, _clamp(ncur + delta))


def observe_accept(suggestion: str, *, context: str = "", project: str = "") -> None:
    """Convenience wrapper — user accepted a suggestion (Tab / Right-Arrow)."""
    feedback("accept", suggestion, context=context, project=project)


def observe_ignore(suggestion: str, *, context: str = "", project: str = "") -> None:
    """Convenience wrapper — user typed past a suggestion."""
    feedback("ignore", suggestion, context=context, project=project)


def observe_delete(suggestion: str, *, context: str = "", project: str = "") -> None:
    """Convenience wrapper — user accepted then deleted the suggested text."""
    feedback("delete", suggestion, context=context, project=project)
