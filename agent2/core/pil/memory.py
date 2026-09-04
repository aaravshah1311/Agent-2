# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2.core.pil.memory
───────────────────────
THE single, centralized Personal Memory for the Personal Intelligence Layer.

Every PIL module (Smart Word Prediction, Grammar Enhancement, Prompt
Improvement) reads and writes THIS store — there are no per-module memories.
A word learned by one module is immediately visible to the others because they
all query the same four tables:

    pil_vocab    — learned single tokens (words, tech terms, project names …)
    pil_phrases  — learned multi-word phrases / prompt templates
    pil_ngrams   — token→token transitions for next-word prediction
    pil_prefs    — proven user preferences used to enrich prompts

This module is a thin, defensive data-access layer. All ranking / confidence
math lives in the shared learning engine (agent2.core.pil.learning); all schema
lives in agent2.database. Every function swallows errors and returns a sane
default so a corrupt or locked DB can never crash the agent.
"""

import re
import threading
import time
import uuid

from agent2.database import qall, qone, exe, exemany

# Tokenizer shared by the whole layer: words, tech identifiers, and dotted/
# hyphenated names (react-dom, node.js, my_project). Kept deliberately simple
# and fast — no NLP library, this runs on the hot path.
_TOKEN_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.+\-]*")

# ── Change signalling ──────────────────────────────────────────────────────────
# The prediction index caches these tables in memory, so it must know when they
# change. Two signals, deliberately different in cost:
#
#   • `revision` — process-local counters bumped on every write. Free (one int),
#     and it is what makes a just-learned word instantly predictable in THIS
#     process.
#   • the 'pil' sync version — a DB write, so it is throttled to at most once
#     per _SYNC_THROTTLE seconds. It only exists so a DIFFERENT process (the CLI
#     next to the web server) eventually notices; prediction freshness across
#     processes does not need to be instant.
#
# ⚠️ The counters are PER KIND, and that is a performance guarantee, not
# bookkeeping. A rebuild of one kind is a ranked LIMITed query over that table,
# so the difference between "something changed" and "vocab changed" is the
# difference between three queries and one. Two write paths make it matter:
#
#   • `bump_pref` — preferences are read by the prompt-improver, NOT by the
#     prediction index. `_mine_preferences` fires on every sent prompt and can
#     bump several, so one global counter made every message invalidate an index
#     that does not contain a single row of what changed. Measured: 10.7 ms
#     added to the next keystroke (a 200x baseline) for zero freshness gained.
#   • `observe_accept` / `observe_ignore` / `set_*_confidence` — each writes
#     exactly ONE kind, so two of the three queries were rebuilding rows that
#     had not moved.
#
# KINDS is the closed set; `_touch()` takes one of them and there is no
# "everything" value — a caller that writes two kinds calls it twice, which is
# what `learn_text` legitimately does.
KINDS = ("vocab", "phrases", "ngrams", "prefs")

_revisions = dict.fromkeys(KINDS, 0)
_rev_lock = threading.Lock()
_last_sync_bump = 0.0
_SYNC_THROTTLE = 2.0


def revision(kind: str | None = None) -> int:
    """Process-local write counter — cheap staleness check for in-memory caches.

    With *kind*, the counter for that table alone. With no argument, the sum
    across every kind: still a single int that changes whenever anything does,
    which is what a cache that mirrors the whole store wants.
    """
    with _rev_lock:
        if kind is None:
            return sum(_revisions.values())
        return _revisions.get(kind, 0)


def revisions() -> dict:
    """Snapshot of every per-kind counter. One lock acquisition, so the caller
    compares a CONSISTENT set rather than racing four separate reads."""
    with _rev_lock:
        return dict(_revisions)


def _touch(kind: str) -> None:
    """Mark one PIL table as changed. Never raises.

    *kind* must be in KINDS. An unknown kind still fires the cross-process
    notify — dropping the signal entirely would be a silent staleness bug, and
    over-notifying only costs a rebuild.
    """
    global _last_sync_bump
    with _rev_lock:
        if kind in _revisions:
            _revisions[kind] += 1
        now = time.time()
        due = (now - _last_sync_bump) >= _SYNC_THROTTLE
        if due:
            _last_sync_bump = now
    if not due:
        return
    try:
        from agent2.core import sync
        sync.notify("pil", throttled=True)
    except Exception:
        pass

# Confidence floor/ceiling the learning engine clamps to. Defined here so both
# the data layer and the engine agree on the range.
CONF_MIN = 0.05
CONF_MAX = 1.0


def tokenize(text: str) -> list[str]:
    """Split *text* into prediction tokens. Never raises."""
    if not text:
        return []
    try:
        return _TOKEN_RE.findall(text)
    except Exception:
        return []


def _clamp(v: float) -> float:
    return max(CONF_MIN, min(CONF_MAX, v))


# ── Vocabulary ──────────────────────────────────────────────────────────────

def bump_vocab(token: str, *, kind: str = "word", lang: str = "",
               project: str = "", freq: int = 1) -> None:
    """Record one usage of *token*. Upserts, incrementing frequency and nudging
    confidence upward slightly (proven-by-use). Case-insensitive key, original
    casing preserved in `display`."""
    token = (token or "").strip()
    if not token:
        return
    key = token.lower()
    try:
        exe(
            "INSERT INTO pil_vocab(token, display, kind, frequency, confidence, lang, project) "
            "VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(token) DO UPDATE SET "
            "  frequency = frequency + ?, "
            "  confidence = MIN(?, confidence + 0.02), "
            "  display = excluded.display, "
            "  kind = CASE WHEN pil_vocab.kind='word' THEN excluded.kind ELSE pil_vocab.kind END, "
            "  lang = CASE WHEN excluded.lang != '' THEN excluded.lang ELSE pil_vocab.lang END, "
            "  project = CASE WHEN excluded.project != '' THEN excluded.project ELSE pil_vocab.project END, "
            "  updated_at = datetime('now')",
            (key, token, kind, freq, 0.5, lang, project, freq, CONF_MAX),
        )
        _touch("vocab")
    except Exception:
        pass


def get_vocab(token: str) -> dict | None:
    try:
        return qone("SELECT * FROM pil_vocab WHERE token=?", ((token or "").lower(),))
    except Exception:
        return None


def vocab_prefix(prefix: str, *, limit: int = 5, project: str = "") -> list[dict]:
    """Vocabulary entries starting with *prefix*, ranked by confidence*log(freq).
    Entries matching the current *project* are boosted."""
    prefix = (prefix or "").strip().lower()
    if not prefix:
        return []
    try:
        rows = qall(
            "SELECT * FROM pil_vocab WHERE token LIKE ? "
            "ORDER BY confidence * (frequency + 1) DESC LIMIT ?",
            (prefix + "%", max(1, limit) * 4),
        )
    except Exception:
        return []
    if project:
        rows.sort(key=lambda r: (r.get("project") == project,
                                 r["confidence"] * (r["frequency"] + 1)),
                  reverse=True)
    return rows[:limit]


def set_vocab_confidence(token: str, confidence: float) -> None:
    try:
        exe("UPDATE pil_vocab SET confidence=?, updated_at=datetime('now') WHERE token=?",
            (_clamp(confidence), (token or "").lower()))
        _touch("vocab")
    except Exception:
        pass


# ── Phrases ─────────────────────────────────────────────────────────────────

def bump_phrase(body: str, *, project: str = "", freq: int = 1) -> None:
    """Record one usage of the multi-word phrase / template *body*."""
    body = " ".join((body or "").split())
    if len(body) < 3 or " " not in body:
        return
    try:
        exe(
            "INSERT INTO pil_phrases(id, body, frequency, confidence, project) "
            "VALUES(?,?,?,?,?) "
            "ON CONFLICT(body) DO UPDATE SET "
            "  frequency = frequency + ?, "
            "  confidence = MIN(?, confidence + 0.02), "
            "  project = CASE WHEN excluded.project != '' THEN excluded.project ELSE pil_phrases.project END, "
            "  updated_at = datetime('now')",
            (str(uuid.uuid4()), body, freq, 0.5, project, freq, CONF_MAX),
        )
        _touch("phrases")
    except Exception:
        pass


def phrase_prefix(prefix: str, *, limit: int = 3, project: str = "") -> list[dict]:
    """Phrases whose body begins with *prefix* (case-insensitive), best first."""
    prefix = " ".join((prefix or "").split()).lower()
    if not prefix:
        return []
    try:
        rows = qall(
            "SELECT * FROM pil_phrases WHERE LOWER(body) LIKE ? "
            "ORDER BY confidence * (frequency + 1) DESC LIMIT ?",
            (prefix + "%", max(1, limit) * 4),
        )
    except Exception:
        return []
    # Only keep phrases strictly longer than what's typed (there is a tail to add).
    rows = [r for r in rows if len(r["body"]) > len(prefix)]
    if project:
        rows.sort(key=lambda r: (r.get("project") == project,
                                 r["confidence"] * (r["frequency"] + 1)),
                  reverse=True)
    return rows[:limit]


def get_phrase(body: str) -> dict | None:
    try:
        return qone("SELECT * FROM pil_phrases WHERE body=?",
                    (" ".join((body or "").split()),))
    except Exception:
        return None


def set_phrase_confidence(body: str, confidence: float) -> None:
    try:
        exe("UPDATE pil_phrases SET confidence=?, updated_at=datetime('now') WHERE body=?",
            (_clamp(confidence), " ".join((body or "").split())))
        _touch("phrases")
    except Exception:
        pass


# ── N-grams ─────────────────────────────────────────────────────────────────

def bump_ngram(prev: str, nxt: str, freq: int = 1) -> None:
    """Record that token *nxt* followed context *prev* (1–2 tokens)."""
    prev = (prev or "").strip().lower()
    nxt = (nxt or "").strip()
    if not prev or not nxt:
        return
    try:
        exe(
            "INSERT INTO pil_ngrams(prev, next, frequency, confidence) "
            "VALUES(?,?,?,?) "
            "ON CONFLICT(prev, next) DO UPDATE SET "
            "  frequency = frequency + ?, "
            "  confidence = MIN(?, confidence + 0.02), "
            "  updated_at = datetime('now')",
            (prev, nxt, freq, 0.5, freq, CONF_MAX),
        )
        _touch("ngrams")
    except Exception:
        pass


def ngram_next(prev: str, *, limit: int = 5) -> list[dict]:
    """Most-likely next tokens for context *prev*, best first."""
    prev = (prev or "").strip().lower()
    if not prev:
        return []
    try:
        return qall(
            "SELECT * FROM pil_ngrams WHERE prev=? "
            "ORDER BY confidence * (frequency + 1) DESC LIMIT ?",
            (prev, max(1, limit)),
        )
    except Exception:
        return []


def set_ngram_confidence(prev: str, nxt: str, confidence: float) -> None:
    try:
        exe("UPDATE pil_ngrams SET confidence=?, updated_at=datetime('now') "
            "WHERE prev=? AND next=?",
            (_clamp(confidence), (prev or "").lower(), nxt))
        _touch("ngrams")
    except Exception:
        pass


# ── Preferences ───────────────────────────────────────────────────────────────

def bump_pref(category: str, value: str, *, weight_delta: float = 0.03,
              freq: int = 1) -> None:
    """Record/strengthen a proven user preference (used by prompt improvement)."""
    category = (category or "").strip().lower()
    value = (value or "").strip()
    if not category or not value:
        return
    try:
        exe(
            "INSERT INTO pil_prefs(id, category, value, weight, frequency) "
            "VALUES(?,?,?,?,?) "
            "ON CONFLICT(category, value) DO UPDATE SET "
            "  frequency = frequency + ?, "
            "  weight = MIN(?, weight + ?), "
            "  updated_at = datetime('now')",
            (str(uuid.uuid4()), category, value, 0.5, freq,
             freq, CONF_MAX, weight_delta),
        )
        _touch("prefs")
    except Exception:
        pass


def top_prefs(category: str, *, limit: int = 5, min_weight: float = 0.5) -> list[dict]:
    """Strongest preferences in *category* at or above *min_weight*."""
    category = (category or "").strip().lower()
    if not category:
        return []
    try:
        return qall(
            "SELECT * FROM pil_prefs WHERE category=? AND weight>=? "
            "ORDER BY weight * (frequency + 1) DESC LIMIT ?",
            (category, min_weight, max(1, limit)),
        )
    except Exception:
        return []


# ── Ranked read helpers (prediction index) ─────────────────────────────────────
# The in-memory prediction index (agent2.core.pil.prediction) materializes the
# top rows of each table once per version bump, then serves keystrokes from
# memory. These are the one-shot queries that build it.

def _vocab_ranked(limit: int = 200) -> list[dict]:
    try:
        return qall(
            "SELECT * FROM pil_vocab "
            "ORDER BY confidence * (frequency + 1) DESC LIMIT ?",
            (max(1, limit),),
        )
    except Exception:
        return []


def _phrases_ranked(limit: int = 200) -> list[dict]:
    try:
        return qall(
            "SELECT * FROM pil_phrases "
            "ORDER BY confidence * (frequency + 1) DESC LIMIT ?",
            (max(1, limit),),
        )
    except Exception:
        return []


def _ngrams_ranked(limit: int = 400) -> list[dict]:
    try:
        return qall(
            "SELECT * FROM pil_ngrams "
            "ORDER BY confidence * (frequency + 1) DESC LIMIT ?",
            (max(1, limit),),
        )
    except Exception:
        return []


def set_pref_weight(category: str, value: str, weight: float) -> None:
    """Set the absolute weight of a preference (used by cold-start pre-training to
    cap seeds below the improvement-injection threshold). Clamped to the range."""
    try:
        exe("UPDATE pil_prefs SET weight=?, updated_at=datetime('now') "
            "WHERE category=? AND value=?",
            (_clamp(weight), (category or "").strip().lower(), (value or "").strip()))
        _touch("prefs")
    except Exception:
        pass


def all_prefs(*, min_weight: float = 0.5) -> list[dict]:
    try:
        return qall(
            "SELECT * FROM pil_prefs WHERE weight>=? ORDER BY category, weight DESC",
            (min_weight,),
        )
    except Exception:
        return []


# ── Stats / maintenance helpers (used by optimize.py and the API) ─────────────

def stats() -> dict:
    """Row counts for each centralized table — powers the settings UI."""
    out = {"vocab": 0, "phrases": 0, "ngrams": 0, "prefs": 0}
    try:
        for name, table in (("vocab", "pil_vocab"), ("phrases", "pil_phrases"),
                            ("ngrams", "pil_ngrams"), ("prefs", "pil_prefs")):
            row = qone(f"SELECT COUNT(*) AS n FROM {table}")
            out[name] = row["n"] if row else 0
    except Exception:
        pass
    return out


def wipe() -> None:
    """Erase the entire centralized memory (privacy 'forget me' control).

    ⚠️ Each cleared table bumps its own counter, and that is what makes the
    prediction index forget too. Nothing else invalidates it on this path:
    `pil.wipe()` calls only this function, and these are raw DELETEs that no
    `bump_*` helper sees. Without the touch, "forget me" would erase the tables
    while the in-memory mirror kept suggesting the very rows it deleted.
    """
    for kind in KINDS:
        try:
            exe(f"DELETE FROM pil_{kind}")
        except Exception:
            continue
        _touch(kind)


# ── Batch variants (bulk learning) ─────────────────────────────────────────────
# learn_text() issues up to ~90 upserts per prompt; for a long prompt that is
# ~90 separate commits. The batch functions below do the same work in ONE
# transaction. They are functionally identical to the per-row helpers — same
# tables, same confidence math — and are used by the learning engine when it
# processes whole texts instead of single signals. Batching means a cold-start
# pretrain or a long paste costs a handful of writes instead of hundreds.

_VOCAB_UPSERT = (
    "INSERT INTO pil_vocab(token, display, kind, frequency, confidence, lang, project) "
    "VALUES(?,?,?,?,?,?,?) "
    "ON CONFLICT(token) DO UPDATE SET "
    "  frequency = frequency + ?, "
    "  confidence = MIN(?, confidence + 0.02), "
    "  display = excluded.display, "
    "  kind = CASE WHEN pil_vocab.kind='word' THEN excluded.kind ELSE pil_vocab.kind END, "
    "  lang = CASE WHEN excluded.lang != '' THEN excluded.lang ELSE pil_vocab.lang END, "
    "  project = CASE WHEN excluded.project != '' THEN excluded.project ELSE pil_vocab.project END, "
    "  updated_at = datetime('now')"
)

_NGRAM_UPSERT = (
    "INSERT INTO pil_ngrams(prev, next, frequency, confidence) "
    "VALUES(?,?,?,?) "
    "ON CONFLICT(prev, next) DO UPDATE SET "
    "  frequency = frequency + ?, "
    "  confidence = MIN(?, confidence + 0.02), "
    "  updated_at = datetime('now')"
)

_PHRASE_UPSERT = (
    "INSERT INTO pil_phrases(id, body, frequency, confidence, project) "
    "VALUES(?,?,?,?,?) "
    "ON CONFLICT(body) DO UPDATE SET "
    "  frequency = frequency + ?, "
    "  confidence = MIN(?, confidence + 0.02), "
    "  project = CASE WHEN excluded.project != '' THEN excluded.project ELSE pil_phrases.project END, "
    "  updated_at = datetime('now')"
)

_PREF_UPSERT = (
    "INSERT INTO pil_prefs(id, category, value, weight, frequency) "
    "VALUES(?,?,?,?,?) "
    "ON CONFLICT(category, value) DO UPDATE SET "
    "  frequency = frequency + ?, "
    "  weight = MIN(?, weight + ?), "
    "  updated_at = datetime('now')"
)


def bump_vocab_many(rows: list[tuple]) -> None:
    """Batch vocab upserts. Each row: (token, kind, lang, project, freq).

    One transaction for the whole batch instead of one commit per token.
    """
    if not rows:
        return
    try:
        params = [
            (t.lower(), t, k, f, 0.5, lg, pj, f, CONF_MAX)
            for t, k, lg, pj, f in rows
            if (t or "").strip()
        ]
        if params:
            exemany(_VOCAB_UPSERT, params)
        _touch("vocab")
    except Exception:
        pass


def bump_ngram_many(rows: list[tuple]) -> None:
    """Batch n-gram upserts. Each row: (prev, next, freq)."""
    if not rows:
        return
    try:
        params = [
            (p.lower(), n, f, 0.5, f, CONF_MAX)
            for p, n, f in rows
            if (p or "").strip() and (n or "").strip()
        ]
        if params:
            exemany(_NGRAM_UPSERT, params)
        _touch("ngrams")
    except Exception:
        pass


def bump_phrase_many(rows: list[tuple]) -> None:
    """Batch phrase upserts. Each row: (body, project, freq)."""
    if not rows:
        return
    try:
        params = []
        for body, pj, f in rows:
            body = " ".join((body or "").split())
            if len(body) >= 3 and " " in body:
                params.append((str(uuid.uuid4()), body, f, 0.5, pj, f, CONF_MAX))
        if params:
            exemany(_PHRASE_UPSERT, params)
        _touch("phrases")
    except Exception:
        pass


def bump_pref_many(rows: list[tuple]) -> None:
    """Batch preference upserts. Each row: (category, value, weight_delta, freq)."""
    if not rows:
        return
    try:
        params = [
            (str(uuid.uuid4()), (c or "").strip().lower(), (v or "").strip(),
             0.5, f, f, CONF_MAX, wd)
            for c, v, wd, f in rows
            if (c or "").strip() and (v or "").strip()
        ]
        if params:
            exemany(_PREF_UPSERT, params)
        _touch("prefs")
    except Exception:
        pass
