# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2.core.pil.prediction
───────────────────────────
Module 1 — Smart Word Prediction.

Real-time autocomplete while the user types. It predicts the next words from the
current sentence, the shared centralized memory (vocabulary, phrases, n-grams),
the current project, and the active language — WITHOUT calling a large language
model on every keystroke. The techniques are all cheap and local:

    • trie/prefix lookup over learned vocabulary   (completing the current word)
    • n-gram statistics                             (predicting the next word)
    • phrase-prefix matching                        (completing a whole template)
    • confidence-weighted ranking                   (best-proven suggestion wins)

The suggestion is ONLY ever returned as a hint — this module never inserts it.
Acceptance (Tab / Right-Arrow), ignore, and delete are reported back through the
shared learning engine so the same memory that produced the suggestion improves.

`predict()` returns a dict the UI renders as gray ghost text:

    {"suggestion": "HTML, CSS, JavaScript", "kind": "phrase", "source": "phrase"}

or None when there is nothing confident enough to show.

Why an in-memory index?
  `predict()` fires on EVERY keystroke. Its old implementation ran 1–3 SQLite
  queries per keypress (each one a connection checkout + LIKE scan). The index
  below mirrors the four PIL tables in memory — a trie for vocabulary prefix
  lookup, plus sorted lists for phrases and n-grams — so a keystroke is a few
  dictionary lookups, not a database round-trip. The index is versioned against
  the PER-KIND revision counters owned by agent2.core.pil.memory (see
  agent2.core.sync) and rebuilds itself lazily when learning bumps one, so it can
  never go stale.

INDEX INVALIDATION — per-kind, NOT global
─────────────────────────────────────────
This module mirrors three tables, so `memory.py` keeps a revision counter PER
KIND and `_refresh()` rebuilds only the kinds that actually moved.

⚠️ `prefs` IS DELIBERATELY NOT IN `_INDEX_KINDS`.
It feeds `improve`, never a prediction. `_mine_preferences` fires on EVERY
prompt, so under one global counter every message invalidated an index containing
none of what changed: **10.7 ms added to the next keystroke against a 0.05 ms
baseline.** There is no "everything" value — `_touch(kind)` takes one kind.

⚠️ A REBUILT KIND RECORDS ITS COUNTER EVEN WHEN THE REBUILD FAILED and degraded
to empty. Staying stale would re-query on every keystroke, and with
`busy_timeout` at 10 s a locked DB would stall each one.

`revisions()` IS READ ONCE PER REFRESH, under the lock, so the kinds are compared
against a consistent set rather than four raced reads.

⚠️ RAW-SQL WRITERS MUST SIGNAL EXPLICITLY. `memory.wipe()` touches each cleared
kind — without it, "forget me" empties the tables while this mirror keeps
suggesting deleted rows — and `optimize._signal()` touches the three mirrored
kinds. `invalidate_index()` is the manual escape hatch.

(test_pil.py — sabotage-verified)
"""

import threading

from agent2.core.pil import memory as mem
from agent2.core.pil.memory import tokenize

# Don't show a suggestion below this confidence — silence beats a bad guess.
MIN_SHOW_CONFIDENCE = 0.35
# Cap how much ghost text we emit at once (whole phrase completions can be long).
MAX_SUGGESTION_CHARS = 80

# How many candidate rows the index materializes per key. The DB stores far more
# than this, but the top ~200 per prefix are all a real user session will ever
# surface — an unbounded load would just burn memory on cold-start pretrains.
_INDEX_LIMIT = 200


def _ends_with_space(text: str) -> bool:
    return bool(text) and text[-1].isspace()


def predict(text: str, *, project: str = "", lang: str = "") -> dict | None:
    """Return the single best next-token / completion suggestion for *text*.

    Strategy, in priority order:
      1. Mid-word  → complete the current word from learned vocabulary (prefix).
      2. Word-boundary → predict the next word(s) from n-grams, then fall back to
         a phrase-template completion when the typed tail matches one.
    Ranking always uses confidence × frequency so proven, personal suggestions
    surface first. Returns None if nothing clears MIN_SHOW_CONFIDENCE.
    """
    if not text:
        return None

    idx = _index()
    toks = tokenize(text)
    at_boundary = _ends_with_space(text)

    # ── 1. Mid-word completion ────────────────────────────────────────────────
    if toks and not at_boundary:
        partial = toks[-1]
        # A single letter is too ambiguous to complete usefully.
        if len(partial) >= 2:
            for c in idx.completions(partial, limit=3, project=project):
                if c["token"].lower() == partial.lower():
                    continue  # already fully typed
                if c["confidence"] < MIN_SHOW_CONFIDENCE:
                    continue
                tail = c["display"][len(partial):]
                if tail:
                    return {
                        "suggestion": tail[:MAX_SUGGESTION_CHARS],
                        "full": c["display"],
                        "kind": c.get("kind", "word"),
                        "source": "vocab",
                        "confidence": round(c["confidence"], 3),
                    }
            # Also try completing the phrase the current line is starting.
            line_phrase = _phrase_completion(text, project=project, mid_word=True)
            if line_phrase:
                return line_phrase
        return None

    # ── 2. Next-word prediction at a word boundary ────────────────────────────
    if at_boundary and toks:
        # Prefer a phrase-template completion for the whole line — it produces the
        # richest ghost text (e.g. "Create a website in " → "HTML, CSS, JavaScript").
        phrase = _phrase_completion(text, project=project, mid_word=False)
        if phrase:
            return phrase

        # Otherwise fall back to n-gram next-word (bigram context, then unigram).
        nxt = _ngram_completion(toks, idx)
        if nxt:
            return nxt

    return None


def _phrase_completion(text: str, *, project: str, mid_word: bool) -> dict | None:
    """Complete the current line against a learned phrase whose prefix matches."""
    # Match the phrase store against the trimmed line so trailing spaces don't
    # break the prefix check.
    typed = " ".join(text.split())
    if not typed:
        return None
    rows = _index().phrase_completions(typed, limit=3, project=project)
    for r in rows:
        if r["confidence"] < MIN_SHOW_CONFIDENCE:
            continue
        body = r["body"]
        # The stored phrase is longer than the typed prefix — emit the remainder.
        # Preserve the exact remaining substring (respecting original spacing).
        if body.lower().startswith(typed.lower()) and len(body) > len(typed):
            tail = body[len(typed):]
            # At a word boundary the tail should not start with a leading space
            # we already typed; strip a single leading space for clean rendering.
            if not mid_word and tail.startswith(" "):
                tail = tail[1:]
            if tail:
                return {
                    "suggestion": tail[:MAX_SUGGESTION_CHARS],
                    "full": body,
                    "kind": "phrase",
                    "source": "phrase",
                    "confidence": round(r["confidence"], 3),
                }
    return None


def _ngram_completion(toks: list[str], idx) -> dict | None:
    """Predict the next word from bigram context, falling back to unigram."""
    contexts = []
    if len(toks) >= 2:
        contexts.append(f"{toks[-2].lower()} {toks[-1].lower()}")   # bigram
    contexts.append(toks[-1].lower())                                # unigram
    for ctx in contexts:
        for row in idx.ngram_next(ctx, limit=3):
            if row["confidence"] < MIN_SHOW_CONFIDENCE:
                continue
            return {
                "suggestion": row["next"][:MAX_SUGGESTION_CHARS],
                "full": row["next"],
                "kind": "word",
                "source": "ngram",
                "confidence": round(row["confidence"], 3),
            }
    return None


# ── In-memory prediction index ─────────────────────────────────────────────────

# The kinds this index mirrors. `prefs` is deliberately ABSENT — preferences
# feed the prompt-improver (improve.py), never a prediction, so a preference
# write must not cost a rebuild here. `_mine_preferences` runs on every sent
# prompt, so treating that as an invalidation added ~10.7 ms to the next
# keystroke to re-read three tables that had not changed. See the KINDS comment
# in memory.py.
_INDEX_KINDS = ("vocab", "phrases", "ngrams")


class _PredictionIndex:
    """In-memory mirror of the PIL store, rebuilt when the 'pil' version moves.

    Each mirrored table has its OWN revision counter, so a write rebuilds only
    what it actually changed: `observe_accept` bumps one phrase and costs one
    ranked query instead of three, and a preference write costs none. The
    per-kind builders live in `_rebuild_*`, the only methods allowed to write to
    `self`. `_refresh()` runs them under the sync write lock, so concurrent
    keystrokes block on a short lock briefly (or not at all, since refresh is
    one dict comparison) instead of every one paying for a database query.
    """

    def __init__(self) -> None:
        from agent2.core import sync
        self._lock = sync.RWLock()
        self._vocab: dict[str, list[dict]] = {}     # first char → ranked vocab rows
        self._phrases: list[dict] = []              # all phrases, pre-sorted
        self._ngrams: dict[str, list[dict]] = {}    # ctx → ranked next tokens
        self._revisions: dict[str, int] = {}        # kind → mem counter when built
        self._built = False
        self._rebuilders = {
            "vocab": self._rebuild_vocab,
            "phrases": self._rebuild_phrases,
            "ngrams": self._rebuild_ngrams,
        }
        # A remote change (another process) invalidates via the sync bus.
        sync.subscribe("pil", self._on_remote_change)

    def _on_remote_change(self, _topic, payload) -> None:
        if payload.get("remote"):
            self.invalidate()

    def _stale(self, revs: dict) -> tuple:
        """Which mirrored kinds have moved since they were built.

        A kind never built yet has no entry, so `.get()` returns None and never
        compares equal to a real counter — a fresh index reports every kind
        stale without needing a sentinel value.
        """
        return tuple(k for k in _INDEX_KINDS
                     if revs.get(k, 0) != self._revisions.get(k))

    def _refresh(self) -> None:
        """Rebuild the kinds that changed since the index was built.

        The check is one snapshot plus a few int comparisons, so the common case
        (no writes since the last keystroke) costs almost nothing. `revisions()`
        is read ONCE, under the memory lock, so the kinds are compared against a
        consistent set rather than four separately-raced reads.
        """
        revs = mem.revisions()
        with self._lock.read():
            if self._built and not self._stale(revs):
                return
        with self._lock.write():
            stale = _INDEX_KINDS if not self._built else self._stale(revs)
            for kind in stale:
                self._rebuilders[kind]()
                # Recorded even if the rebuild hit a DB error and degraded to
                # empty. Leaving it stale would retry on EVERY keystroke, and
                # with busy_timeout at 10 s a locked DB would stall each one —
                # far worse than serving no suggestions until the next write.
                self._revisions[kind] = revs.get(kind, 0)
            self._built = True

    def _rebuild_vocab(self) -> None:
        """Re-read pil_vocab into the first-character bucket map."""
        try:
            rows = mem._vocab_ranked(_INDEX_LIMIT)
        except Exception:
            rows = []
        vocab: dict[str, list[dict]] = {}
        for r in rows:
            token = (r.get("token") or "")
            if not token:
                continue
            vocab.setdefault(token[0], []).append(r)
        for lst in vocab.values():
            lst.sort(key=lambda r: r["confidence"] * (r.get("frequency", 0) + 1),
                     reverse=True)
        self._vocab = vocab

    def _rebuild_phrases(self) -> None:
        try:
            self._phrases = mem._phrases_ranked(_INDEX_LIMIT)
        except Exception:
            self._phrases = []

    def _rebuild_ngrams(self) -> None:
        ngrams: dict[str, list[dict]] = {}
        try:
            for row in mem._ngrams_ranked(_INDEX_LIMIT):
                ngrams.setdefault(row["prev"], []).append(row)
        except Exception:
            pass                # keep what was read; a partial map still helps
        self._ngrams = ngrams

    def rebuild(self) -> None:
        """Rebuild every mirrored kind.

        Called with the write lock held. `_refresh()` normally rebuilds only the
        kinds that moved; this full form is what a wipe, a remote change, or a
        first build goes through.
        """
        for kind in _INDEX_KINDS:
            self._rebuilders[kind]()

    def completions(self, prefix: str, *, limit: int, project: str = "") -> list[dict]:
        self._refresh()
        prefix = (prefix or "").strip().lower()
        if not prefix:
            return []
        with self._lock.read():
            rows = self._vocab.get(prefix[0], [])
            matched = [r for r in rows if r["token"].startswith(prefix)]
            if project:
                matched = sorted(matched,
                                 key=lambda r: (r.get("project") == project,
                                                r["confidence"] * (r.get("frequency", 0) + 1)),
                                 reverse=True)
            return matched[:limit]

    def phrase_completions(self, prefix: str, *, limit: int, project: str = "") -> list[dict]:
        self._refresh()
        prefix = " ".join((prefix or "").split()).lower()
        if not prefix:
            return []
        with self._lock.read():
            matched = [r for r in self._phrases
                       if r["body"].lower().startswith(prefix)]
            matched = [r for r in matched if len(r["body"]) > len(prefix)]
            if project:
                matched = sorted(matched,
                                 key=lambda r: (r.get("project") == project,
                                                r["confidence"] * (r.get("frequency", 0) + 1)),
                                 reverse=True)
            return matched[:limit]

    def ngram_next(self, ctx: str, *, limit: int) -> list[dict]:
        self._refresh()
        ctx = (ctx or "").strip().lower()
        if not ctx:
            return []
        with self._lock.read():
            return self._ngrams.get(ctx, [])[:limit]

    def invalidate(self) -> None:
        """Force a rebuild on the next lookup (remote change, wipe, or tests)."""
        with self._lock.write():
            self._built = False


# Built lazily on first predict() so importing this module never touches the DB.
_INDEX: "_PredictionIndex | None" = None
_INDEX_LOCK = threading.Lock()


def _index() -> "_PredictionIndex":
    global _INDEX
    if _INDEX is None:
        with _INDEX_LOCK:
            if _INDEX is None:
                _INDEX = _PredictionIndex()
    return _INDEX


def invalidate_index() -> None:
    """Public hook: drop the cached index (called by pil.wipe()/optimize)."""
    if _INDEX is not None:
        _INDEX.invalidate()


# ── Passive feedback shims (delegate to the shared learning engine) ────────────
# The UI calls these when it observes the user accepting / ignoring / deleting a
# suggestion. They live here so the module has one clean surface, but all state
# changes happen in the single shared engine + centralized memory.

def accept(suggestion: str, *, context: str = "", project: str = "") -> None:
    from agent2.core.pil import learning
    learning.observe_accept(suggestion, context=context, project=project)


def ignore(suggestion: str, *, context: str = "", project: str = "") -> None:
    from agent2.core.pil import learning
    learning.observe_ignore(suggestion, context=context, project=project)


def reject(suggestion: str, *, context: str = "", project: str = "") -> None:
    """User accepted then deleted the text — strong negative signal."""
    from agent2.core.pil import learning
    learning.observe_delete(suggestion, context=context, project=project)
