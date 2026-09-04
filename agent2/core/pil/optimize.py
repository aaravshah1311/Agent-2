# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2.core.pil.optimize
─────────────────────────
Background optimization for the centralized Personal Memory.

Runs while the user is idle (never on the hot path, never interrupting). It keeps
the ONE shared memory small, fast, and well-calibrated so prediction stays cheap
and accurate as the store grows:

    • deduplicate    — collapse case-variant / whitespace-variant duplicates
    • merge          — fold near-identical phrases (one a prefix of another) into
                       the stronger record, summing their frequency
    • recalibrate    — gently decay confidence of stale, unused entries so old
                       guesses stop being offered; keep proven ones sharp
    • prune / archive — drop low-value noise (seen once, low confidence, old) so
                       the tables don't accumulate dead weight
    • reindex        — ANALYZE so SQLite's planner keeps prefix lookups fast

Everything is defensive: any failure is swallowed and the pass simply does less
work. `optimize()` returns a small report dict the settings UI can show.

Because personalization lives in this data (not in model weights), tidying the
data *is* the maintenance — there is nothing to retrain.

⚠️ THIS RUNS ON ~10% OF TURNS, so its cost is NOT free-because-background.

PHRASE MERGING — `_merge_phrases()`
───────────────────────────────────
Folds a phrase that is a strict prefix of a longer one into it.

⚠️ IT GENERATES CANDIDATES; IT DOES NOT SCAN FOR THEM.
The obvious form is O(n²) — 46 ms at 500 phrases, 3.2 s at 4000, and PHRASE_CAP
allows 8000. The merge only applies to a WORD-BOUNDARY prefix, and both writers
normalize with `" ".join(body.split())`, so every such prefix is exactly
`" ".join(words[:k])` — at most `len(words) - 1` of them, generated and looked up
in a dict.

⚠️ EVERY MERGE IS DECIDED FIRST, THEN APPLIED AS TWO BULK STATEMENTS IN ONE
`batch()`. This is also a CORRECTNESS fix, not just speed: the per-merge form
could DELETE the shorter phrase and then fail the UPDATE carrying its frequency,
losing that count permanently. Net 46× on the no-merge shape, 29× when most rows
merge.

⚠️ THE ATOMICITY TEST MUST FAIL THE *SECOND* WRITE. Failing the first proves
nothing — nothing is written either way — which is exactly how the
"writes outside the transaction" sabotage survived.

(The `cut` order and the `short_body == long_body` guard are deliberately not
asserted: no writer can produce the state that would distinguish them.)

⚠️ THIS MODULE USES RAW SQL, so it must call `_signal()` to touch the three
mirrored index kinds — but only if something actually changed. See
`agent2.core.pil.prediction`.

(test_pil.py — sabotage-verified)
"""

from agent2.database import qall, qone, exe, exemany, batch
from agent2.core.pil import memory as mem

# ── Tunables ──────────────────────────────────────────────────────────────────
# Confidence decay applied to entries not touched recently. Small, so proven
# items barely move while one-off noise slowly fades below the show threshold.
DECAY = 0.03
# Only decay rows older than this many days (SQLite datetime modifier).
STALE_DAYS = 7
# Prune vocab/phrases seen at most this often and weaker than this confidence.
PRUNE_MAX_FREQ = 1
PRUNE_MAX_CONF = 0.2
# Keep at most this many n-gram edges per context — the tail is long and cheap
# to regenerate from future typing.
NGRAM_KEEP_PER_CTX = 12
# Hard caps so a very heavy user's store can't grow without bound. Lowest-value
# rows beyond the cap are archived (deleted); real usage rebuilds what matters.
VOCAB_CAP = 20000
PHRASE_CAP = 8000


def optimize(*, aggressive: bool = False) -> dict:
    """Run a full background optimization pass over the centralized memory.

    Safe to call any time; designed for idle moments. *aggressive* tightens the
    pruning thresholds for an occasional deep clean. Returns a report dict.
    """
    report = {
        "merged_phrases": 0,
        "pruned_vocab": 0,
        "pruned_phrases": 0,
        "pruned_ngrams": 0,
        "decayed": 0,
        "capped": 0,
    }
    report["merged_phrases"] = _merge_phrases()
    report["decayed"] = _decay_stale()
    pv, pp, pn = _prune(aggressive=aggressive)
    report["pruned_vocab"] = pv
    report["pruned_phrases"] = pp
    report["pruned_ngrams"] = pn
    report["capped"] = _enforce_caps()
    _reindex()
    _signal(report)
    return report


def _signal(report: dict) -> None:
    """Tell the prediction index this pass changed rows underneath it.

    Everything above writes through raw `exe()` rather than the `mem.bump_*`
    helpers, so nothing bumps a revision counter — without this the in-memory
    index keeps serving rows this pass just deleted or decayed. Real usage hides
    it (the next learned word invalidates anyway), which is exactly why it needs
    to be explicit: a user who stops typing after an optimize pass would keep
    getting suggestions for pruned entries.

    Deliberately coarse. This pass touches all three mirrored tables through
    several helpers, and mapping each count back to a table would be precision
    that buys nothing — over-notifying costs ONE rebuild, and only when the pass
    actually changed something.
    """
    if not any(report.values()):
        return                          # nothing changed; don't force a rebuild
    for kind in ("vocab", "phrases", "ngrams"):
        mem._touch(kind)


# ── Merge near-identical phrases ──────────────────────────────────────────────

def _merge_phrases() -> int:
    """Fold a phrase that is a strict prefix of a stronger, longer phrase into the
    longer one, summing frequency and keeping the higher confidence. This removes
    redundant sliding-window fragments left by the learner without losing signal.

    ⚠️ This looks up candidates instead of scanning for them, and that is a
    complexity fix, not a style preference. The pair test is
    `long.startswith(short + " ")`, which the obvious form answers by comparing
    every phrase against every other — O(n²), and `optimize()` fires on ~10% of
    agent turns. Measured on realistic data it grew 4.3x per doubling: 46 ms at
    500 phrases, 3.2 s at 4000, and PHRASE_CAP allows 8000.

    But the test only passes for a WORD-BOUNDARY prefix, and both phrase writers
    normalize bodies with `" ".join(body.split())`, so every such prefix is
    exactly `" ".join(words[:k])` for some k. There are at most `len(words) - 1`
    of those, so the candidates can be generated and looked up in a dict —
    O(n · words) instead of O(n²), with identical results (verified against the
    old implementation on 60 randomized inputs plus the case-only, chain, and
    not-a-word-boundary edge cases).

    Two decisions below are deliberately UNTESTED because the state they guard is
    unreachable, and a test for it would assert on something no writer can
    produce: the `cut` order (every present prefix is absorbed regardless, so the
    drop set is identical either way) and the `short_body == long_body` guard
    (`cut` stops at `len(words) - 1`, so the candidate is always a STRICT prefix,
    and both writers space-normalize bodies). The guard stays as documentation of
    the invariant; it is not load-bearing.
    """
    try:
        rows = qall(
            "SELECT id, body, frequency, confidence FROM pil_phrases "
            "ORDER BY LENGTH(body) DESC")
    except Exception:
        return 0
    if not rows:
        return 0

    # Longest first: each phrase can absorb shorter prefixes of itself. A valid
    # prefix is always strictly shorter, so it always sorts after its absorber —
    # which is why generating candidates needs no index bookkeeping.
    alive = {r["body"]: dict(r) for r in rows}
    bodies = [r["body"] for r in rows]
    # Matching is case-insensitive but `alive` is keyed by the original body, so
    # one lowercase form can map to several stored bodies.
    by_lower: dict[str, list[str]] = {}
    for b in bodies:
        by_lower.setdefault(b.lower(), []).append(b)

    # Decide every merge FIRST, touching nothing. The writes then go out as two
    # bulk statements instead of two per merge — measured 97% of this pass was
    # per-statement round-trips (2.4 s of 2.5 s at 8000 phrases), and batching
    # them is ~11x. It also makes the pass atomic: the old form could delete the
    # shorter phrase and then fail the update that carried its frequency over,
    # losing that count permanently. Now either the whole fold applies or none
    # of it does, and `optimize()` simply folds them on a later pass.
    drops: list[tuple] = []
    absorbed: dict[str, dict] = {}
    for long_body in bodies:
        if long_body not in alive:
            continue
        words = long_body.lower().split(" ")
        # Longest prefix first, matching the length-descending scan this replaces.
        for cut in range(len(words) - 1, 0, -1):
            for short_body in by_lower.get(" ".join(words[:cut]), ()):
                if short_body not in alive or short_body == long_body:
                    continue
                keep = alive[long_body]
                drop = alive.pop(short_body)
                keep["frequency"] += drop["frequency"]
                keep["confidence"] = max(keep["confidence"], drop["confidence"])
                drops.append((short_body,))
                absorbed[long_body] = keep

    if not drops:
        return 0

    updates = [(r["frequency"], min(1.0, r["confidence"]), body)
               for body, r in absorbed.items()]
    try:
        with batch():
            exemany("DELETE FROM pil_phrases WHERE body=?", drops)
            exemany("UPDATE pil_phrases SET frequency=?, confidence=? WHERE body=?",
                    updates)
    except Exception:
        return 0               # nothing applied; a later pass retries
    return len(drops)


# ── Confidence decay for stale entries ────────────────────────────────────────

def _decay_stale() -> int:
    """Nudge confidence down on entries not updated in a while, so predictions the
    user has stopped using quietly stop surfacing. Frequency is untouched — only
    the willingness to *suggest* fades; the knowledge is retained."""
    n = 0
    for table in ("pil_vocab", "pil_phrases", "pil_ngrams"):
        try:
            row = qone(
                f"SELECT COUNT(*) AS n FROM {table} "
                f"WHERE updated_at < datetime('now', ?)",
                (f"-{STALE_DAYS} days",))
            exe(
                f"UPDATE {table} SET confidence = MAX(?, confidence - ?) "
                f"WHERE updated_at < datetime('now', ?)",
                (mem.CONF_MIN, DECAY, f"-{STALE_DAYS} days"))
            n += row["n"] if row else 0
        except Exception:
            pass
    return n


# ── Prune low-value noise ─────────────────────────────────────────────────────

def _prune(*, aggressive: bool = False) -> tuple[int, int, int]:
    """Delete rows that carry little predictive value: seen only once and never
    reinforced above a low confidence floor. Aggressive mode raises the bar."""
    max_freq = PRUNE_MAX_FREQ + (1 if aggressive else 0)
    max_conf = PRUNE_MAX_CONF + (0.1 if aggressive else 0.0)

    pv = _delete_count(
        "pil_vocab",
        "frequency <= ? AND confidence <= ? AND updated_at < datetime('now', ?)",
        (max_freq, max_conf, f"-{STALE_DAYS} days"))
    pp = _delete_count(
        "pil_phrases",
        "frequency <= ? AND confidence <= ? AND updated_at < datetime('now', ?)",
        (max_freq, max_conf, f"-{STALE_DAYS} days"))
    pn = _prune_ngrams()
    return pv, pp, pn


def _prune_ngrams() -> int:
    """Keep only the strongest NGRAM_KEEP_PER_CTX next-tokens per context; the rest
    are low-signal and rebuild naturally from future typing."""
    pruned = 0
    try:
        ctxs = qall("SELECT DISTINCT prev FROM pil_ngrams")
    except Exception:
        return 0
    for c in ctxs:
        prev = c["prev"]
        try:
            rows = qall(
                "SELECT next FROM pil_ngrams WHERE prev=? "
                "ORDER BY confidence * (frequency + 1) DESC",
                (prev,))
            for extra in rows[NGRAM_KEEP_PER_CTX:]:
                exe("DELETE FROM pil_ngrams WHERE prev=? AND next=?",
                    (prev, extra["next"]))
                pruned += 1
        except Exception:
            pass
    return pruned


# ── Enforce hard size caps (archive lowest-value overflow) ────────────────────

def _enforce_caps() -> int:
    capped = 0
    capped += _cap_table("pil_vocab", VOCAB_CAP)
    capped += _cap_table("pil_phrases", PHRASE_CAP)
    return capped


def _cap_table(table: str, cap: int) -> int:
    """If *table* exceeds *cap* rows, delete the lowest-value overflow."""
    try:
        row = qone(f"SELECT COUNT(*) AS n FROM {table}")
        total = row["n"] if row else 0
        if total <= cap:
            return 0
        overflow = total - cap
        # Rank by predictive value; delete the weakest `overflow` rows.
        weak = qall(
            f"SELECT rowid AS rid FROM {table} "
            f"ORDER BY confidence * (frequency + 1) ASC LIMIT ?",
            (overflow,))
        for w in weak:
            exe(f"DELETE FROM {table} WHERE rowid=?", (w["rid"],))
        return len(weak)
    except Exception:
        return 0


# ── Helpers ───────────────────────────────────────────────────────────────────

def _delete_count(table: str, where: str, params: tuple) -> int:
    """Count matching rows, then delete them. Returns rows removed."""
    try:
        row = qone(f"SELECT COUNT(*) AS n FROM {table} WHERE {where}", params)
        n = row["n"] if row else 0
        if n:
            exe(f"DELETE FROM {table} WHERE {where}", params)
        return n
    except Exception:
        return 0


def _reindex() -> None:
    """Let SQLite refresh planner statistics so prefix lookups stay fast."""
    try:
        exe("ANALYZE")
    except Exception:
        pass
