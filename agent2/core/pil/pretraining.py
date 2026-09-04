# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2.core.pil.pretraining
────────────────────────────
Cold-start PRE-TRAINING for the Personal Intelligence Layer.

The three PIL modules (prediction, grammar, prompt-improvement) all read the ONE
centralized memory. On a brand-new install that memory is empty, so the modules
would "perform worst on first experience" — no ghost text, no spared jargon, no
enrichment. This module fixes that by pre-training the shared memory from bundled
data files, using the SAME learning engine real usage uses. It runs exactly once
(guarded by a setting), is fully offline, and never calls a model.

Data lives in ``seed_data/`` next to this file — plain CSVs anyone can extend:

    vocab.csv    token,kind,lang    — canonical tech vocabulary (exact casing)
    phrases.csv  phrase             — reusable prompt templates
    prompts.csv  prompt             — realistic prompt corpus (the richest source)
    prefs.csv    category,value,weight — dormant preference seeds for Module 3

The corpus in ``prompts.csv`` is the heart of it: every row flows through
``learning.learn_prompt`` exactly like a real sent prompt, so it grows vocabulary,
n-gram transitions, phrase templates AND mines preferences coherently — the same
statistics real behaviour would produce, just pre-warmed.

Seeds are then damped BELOW the values fresh real usage produces, so the user's
own proven vocabulary and phrasing quickly outrank the pre-training:

    vocab / phrase / n-gram confidence  →  capped at SEED_CONF (< 0.5 real-use base)
    preference weight                   →  capped at SEED_PREF_CAP (< improve's 0.62)

Everything is best-effort: any failure degrades to "do less" and never breaks the
agent. Lines beginning with ``#`` in the CSVs are treated as comments and skipped.
"""

import csv
import os

from agent2.database import exe, batch
from agent2.core.pil import memory as mem
from agent2.core.pil import learning

_DATA_DIR = os.path.join(os.path.dirname(__file__), "seed_data")

# Seed ceilings. Kept ABOVE the prediction show threshold (0.35) so suggestions
# fire immediately, but BELOW the 0.5 baseline a real first use produces, so the
# user's own vocabulary/phrasing outranks the seeds as soon as they type.
SEED_CONF = 0.45
# Preference ceiling kept BELOW improve.MIN_PREF_WEIGHT (0.62): pre-training gives
# the ranking a head start, but improve stays silent until the user's real usage
# pushes a preference over the line — never inventing requirements.
SEED_PREF_CAP = 0.6


def _read(name: str):
    """Yield non-comment, non-empty rows of ``seed_data/<name>`` as dicts. Never
    raises — a missing/locked/garbled file yields nothing."""
    path = os.path.join(_DATA_DIR, name)
    try:
        if not os.path.exists(path):
            return
        with open(path, encoding="utf-8", newline="") as fh:
            # Drop blank lines and full-line comments before the CSV parser so a
            # leading '#' never gets mistaken for data.
            lines = [ln for ln in fh
                     if ln.strip() and not ln.lstrip().startswith("#")]
        for row in csv.DictReader(lines):
            yield {(k or "").strip(): (v or "").strip()
                   for k, v in row.items() if k}
    except Exception:
        return


def _load_vocab() -> int:
    n = 0
    for row in _read("vocab.csv"):
        token = row.get("token", "")
        if not token:
            continue
        mem.bump_vocab(token, kind=row.get("kind") or "word",
                       lang=row.get("lang") or "")
        n += 1
    return n


def _load_phrases() -> int:
    n = 0
    for row in _read("phrases.csv"):
        body = row.get("phrase", "")
        if body:
            mem.bump_phrase(body)
            n += 1
    return n


def _load_corpus() -> int:
    """The main event: replay a realistic prompt corpus through the real learning
    engine, growing vocab + n-grams + phrases + mined preferences together."""
    n = 0
    for row in _read("prompts.csv"):
        prompt = row.get("prompt", "")
        if prompt:
            learning.learn_prompt(prompt)
            n += 1
    return n


def _load_prefs() -> int:
    n = 0
    for row in _read("prefs.csv"):
        cat, val = row.get("category", ""), row.get("value", "")
        if not cat or not val:
            continue
        mem.bump_pref(cat, val)
        try:
            w = min(float(row.get("weight") or SEED_PREF_CAP), SEED_PREF_CAP)
            mem.set_pref_weight(cat, val, w)
        except Exception:
            pass
        n += 1
    return n


def _damp() -> None:
    """Cap seeded confidence/weight so a single real use outranks the pre-training.
    Applied to the whole memory because pretrain runs only on cold start, when the
    memory contains nothing but seeds."""
    for sql, args in (
        ("UPDATE pil_vocab   SET confidence = MIN(confidence, ?)", (SEED_CONF,)),
        ("UPDATE pil_phrases SET confidence = MIN(confidence, ?)", (SEED_CONF,)),
        ("UPDATE pil_ngrams  SET confidence = MIN(confidence, ?)", (SEED_CONF,)),
        ("UPDATE pil_prefs   SET weight     = MIN(weight, ?)",     (SEED_PREF_CAP,)),
    ):
        try:
            exe(sql, args)
        except Exception:
            pass


def has_data() -> bool:
    """True when at least one bundled data file exists (so callers can fall back
    to an inline seed when the package was installed without seed_data/)."""
    return any(os.path.exists(os.path.join(_DATA_DIR, f))
               for f in ("vocab.csv", "phrases.csv", "prompts.csv", "prefs.csv"))


def run() -> dict:
    """Pre-train the centralized memory from the bundled data files. Idempotent to
    call (upserts), best-effort, offline. Returns per-file row counts for logging.
    Callers are responsible for the run-once guard (see pil._ensure_seeded)."""
    counts = {"vocab": 0, "phrases": 0, "corpus": 0, "prefs": 0}
    try:
        # One transaction for the whole seed — thousands of upserts become a
        # single commit, so pre-training stays fast even as the dataset grows.
        with batch():
            counts["vocab"] = _load_vocab()
            counts["phrases"] = _load_phrases()
            counts["corpus"] = _load_corpus()
            counts["prefs"] = _load_prefs()
            _damp()
    except Exception:
        pass
    return counts
