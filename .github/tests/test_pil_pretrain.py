# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
Tests for cold-start pre-training (``agent2/core/pil/pretraining.py``).

Verifies that pre-training the centralized memory from the bundled ``seed_data/``
CSVs actually warms up all three PIL modules WITHOUT any network/model call:

  - the shared memory grows across every table (vocab/phrases/ngrams/prefs);
  - prediction produces ghost text out of the box (mid-word, phrase, n-gram);
  - grammar spares a seeded tech token from "correction";
  - improve stays dormant (seeds are below the injection threshold — the
    "never invents requirements" guarantee is preserved until the user proves
    a preference);
  - the run-once guard + wipe/reseed cycle behave.

conftest.py redirects AGENT2_DB to a throwaway temp DB.
"""

import pytest

from agent2 import database as db
from agent2.database import get_setting, set_setting
from agent2.core.pil import memory as mem
from agent2.core.pil import prediction as pred
from agent2.core.pil import grammar as gram
from agent2.core.pil import improve as imp
from agent2.core.pil import pretraining as pretrain
from agent2.core import pil


@pytest.fixture(autouse=True, scope="session")
def _schema():
    db.init_db()


@pytest.fixture(autouse=True)
def _clean_pil():
    for table in ("pil_vocab", "pil_phrases", "pil_ngrams", "pil_prefs", "settings"):
        try:
            db.exe(f"DELETE FROM {table}")
        except Exception:
            pass
    yield


# ── data files present ────────────────────────────────────────────────────────

def test_seed_data_files_present():
    assert pretrain.has_data()


# ── pretrain.run grows every table ────────────────────────────────────────────

def test_run_populates_all_tables():
    counts = pretrain.run()
    assert counts["vocab"] > 0
    assert counts["phrases"] > 0
    assert counts["corpus"] > 0
    assert counts["prefs"] > 0
    s = mem.stats()
    assert s["vocab"] > 50
    assert s["phrases"] > 10
    assert s["ngrams"] > 50
    assert s["prefs"] > 5


def test_seeds_are_damped_below_real_use_baseline():
    pretrain.run()
    # Every seeded row is capped at SEED_CONF so a single real use (base 0.5)
    # immediately outranks it.
    hi = db.qone("SELECT MAX(confidence) AS m FROM pil_vocab")
    assert hi["m"] <= pretrain.SEED_CONF + 1e-9
    # Preferences stay below the improvement-injection threshold.
    hi_pref = db.qone("SELECT MAX(weight) AS m FROM pil_prefs")
    assert hi_pref["m"] <= pretrain.SEED_PREF_CAP + 1e-9
    assert pretrain.SEED_PREF_CAP < imp.MIN_PREF_WEIGHT


# ── the three modules work out of the box ─────────────────────────────────────

def test_prediction_works_after_pretrain():
    pretrain.run()
    # mid-word completion from seeded vocab
    out = pred.predict("build with Fast")
    assert out is not None and out["full"].lower().startswith("fast")
    # phrase completion for a seeded template
    ph = pred.predict("create a responsive")
    assert ph is not None
    # n-gram next-word prediction
    ng = pred.predict("write a ")
    assert ng is not None


def test_grammar_spares_seeded_tech_token():
    pretrain.run()
    # "nmap" is a seeded tech token; grammar must not "correct" it away.
    out, _ = gram.enhance("run nmap now")
    assert "nmap" in out


def test_improve_stays_dormant_on_seeds_alone():
    pretrain.run()
    # Seeds are deliberately below MIN_PREF_WEIGHT, so improve invents nothing.
    text, added = imp.improve("build a dashboard")
    assert text == "build a dashboard"
    assert added == []


def test_improve_activates_after_user_reinforces_a_seed():
    pretrain.run()
    # A seeded preference plus real user usage crosses the threshold and fires.
    for _ in range(10):
        mem.bump_pref("framework", "React")
    text, added = imp.improve("build a dashboard")
    assert text != "build a dashboard"
    assert any("React" in a for a in added)


# ── facade guard + reseed ─────────────────────────────────────────────────────

def test_facade_pretrain_run_once_guard():
    first = pil.pretrain()
    assert first.get("corpus", 0) > 0
    assert (get_setting("pil.seeded", "0") or "0") == "1"
    # Second call is guarded (already seeded) unless forced.
    assert pil.pretrain().get("skipped") is True
    assert "skipped" not in pil.pretrain(force=True)


def test_wipe_allows_reseed():
    pil.pretrain()
    pil.wipe()
    assert (get_setting("pil.seeded", "0") or "0") == "0"
    assert mem.stats()["vocab"] == 0
    # Reseeding repopulates.
    pil.pretrain()
    assert mem.stats()["vocab"] > 0
