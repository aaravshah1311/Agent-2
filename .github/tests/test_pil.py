# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
Tests for the Personal Intelligence Layer (``agent2/core/pil/``).

Run from the repo root:  python -m pytest .github/tests/test_pil.py -v

The PIL is a fully offline, privacy-first layer between the user and the model.
These tests exercise it end to end WITHOUT any network or model call:

  - memory      : upsert/bump semantics + confidence nudge, prefix lookups,
                  n-gram/pref ranking, stats + wipe.
  - learning    : learn_text / learn_prompt growing vocab + n-grams + phrases,
                  preference mining (no hallucination — only literally-present
                  keywords), and the asymmetric accept/ignore/delete deltas.
  - prediction  : mid-word, phrase, and n-gram completions honouring the show
                  threshold; suggestions never auto-insert (pure data return).
  - grammar     : safe typo/spacing fixes that NEVER touch code, URLs, paths,
                  identifiers, flags, or digits.
  - improve     : enrichment gated on build-intent + strong prefs, never
                  inventing requirements.
  - facade      : toggle gating of process_outgoing_prompt and the
                  "learn from the original, send the enhanced copy" invariant.

conftest.py redirects AGENT2_DB to a throwaway temp DB, so these tests never
touch the developer's real agent2.db.
"""

import pytest

from agent2 import database as db
from agent2.database import get_setting, set_setting
from agent2.core.pil import memory as mem
from agent2.core.pil import learning as learn
from agent2.core.pil import prediction as pred
from agent2.core.pil import grammar as gram
from agent2.core.pil import improve as imp
from agent2.core import pil


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True, scope="session")
def _schema():
    """Create the schema once in the throwaway DB before any test runs."""
    db.init_db()


@pytest.fixture(autouse=True)
def _clean_pil():
    """Wipe the PIL tables (and settings) between tests so each is isolated."""
    for table in ("pil_vocab", "pil_phrases", "pil_ngrams", "pil_prefs", "settings"):
        try:
            db.exe(f"DELETE FROM {table}")
        except Exception:
            pass
    # These are raw DELETEs, so no revision counter moves and the in-memory
    # prediction index would carry the previous test's rows into this one.
    pred.invalidate_index()
    yield


# ── memory.py ─────────────────────────────────────────────────────────────────

def test_tokenize_never_raises_and_splits_technical():
    assert mem.tokenize("") == []
    toks = mem.tokenize("call get_user() then user.name v2")
    # underscores, dots, digits stay glued to the identifier
    assert "get_user" in toks
    assert "user.name" in toks
    assert "v2" in toks


def test_bump_vocab_upsert_and_confidence_nudge():
    mem.bump_vocab("Django", kind="tech")
    row = mem.get_vocab("django")           # lookup is lowercased
    assert row is not None
    assert row["frequency"] == 1
    assert row["confidence"] == pytest.approx(0.5)
    assert row["display"] == "Django"       # display preserves casing

    mem.bump_vocab("django")
    row = mem.get_vocab("DJANGO")
    assert row["frequency"] == 2
    assert row["confidence"] == pytest.approx(0.52)  # +0.02 nudge


def test_vocab_confidence_clamped():
    mem.bump_vocab("clampme")
    mem.set_vocab_confidence("clampme", 9.0)
    assert mem.get_vocab("clampme")["confidence"] == pytest.approx(mem.CONF_MAX)
    mem.set_vocab_confidence("clampme", -9.0)
    assert mem.get_vocab("clampme")["confidence"] == pytest.approx(mem.CONF_MIN)


def test_vocab_prefix_ranks_by_confidence_times_frequency():
    mem.bump_vocab("responsive")
    mem.bump_vocab("responsible")
    for _ in range(5):
        mem.bump_vocab("responsive")       # far more frequent → ranks first
    rows = mem.vocab_prefix("respon", limit=5)
    assert rows
    assert rows[0]["token"] == "responsive"


def test_bump_phrase_requires_multiword_and_length():
    mem.bump_phrase("hi")                   # single token → rejected
    mem.bump_phrase("a")                    # too short → rejected
    assert mem.get_phrase("hi") is None
    mem.bump_phrase("create a login page")
    row = mem.get_phrase("create a login page")
    assert row is not None
    assert row["frequency"] == 1


def test_phrase_prefix_only_returns_longer_bodies():
    mem.bump_phrase("write a function that")
    # prefix equal to the whole body must NOT be returned (nothing to complete)
    assert mem.phrase_prefix("write a function that") == []
    rows = mem.phrase_prefix("write a")
    assert any(r["body"] == "write a function that" for r in rows)


def test_bump_ngram_and_next_ranking():
    mem.bump_ngram("write", "a")
    mem.bump_ngram("write", "an")
    for _ in range(4):
        mem.bump_ngram("write", "a")
    rows = mem.ngram_next("write", limit=5)
    assert rows[0]["next"] == "a"


def test_bump_pref_and_top_prefs_threshold():
    mem.bump_pref("framework", "React")
    # single bump → weight 0.5, below the default min_weight 0.5? (>= passes)
    strong = mem.top_prefs("framework", min_weight=0.5)
    assert any(p["value"] == "React" for p in strong)
    # a weak pref filtered out by a higher threshold
    assert mem.top_prefs("framework", min_weight=0.9) == []


def test_stats_and_wipe():
    mem.bump_vocab("alpha")
    mem.bump_phrase("alpha beta gamma")
    mem.bump_ngram("alpha", "beta")
    mem.bump_pref("language", "Python")
    s = mem.stats()
    assert s["vocab"] >= 1 and s["phrases"] >= 1
    assert s["ngrams"] >= 1 and s["prefs"] >= 1
    mem.wipe()
    s = mem.stats()
    assert s == {"vocab": 0, "phrases": 0, "ngrams": 0, "prefs": 0}


# ── learning.py ───────────────────────────────────────────────────────────────

def test_learn_text_grows_vocab_ngrams_and_phrases():
    learn.learn_text("create a responsive dashboard component")
    # content words become vocabulary; stopwords ("a") are skipped
    assert mem.get_vocab("responsive") is not None
    assert mem.get_vocab("dashboard") is not None
    assert mem.get_vocab("a") is None
    # adjacency edges are recorded
    nxt = [r["next"] for r in mem.ngram_next("create")]
    assert "a" in nxt
    # a multi-word phrase prefix is learned
    assert mem.phrase_prefix("create a responsive")


def test_learn_text_keeps_short_technical_and_language_tokens():
    learn.learn_text("use go and js with a c compiler")
    # "go", "js", "c" are single/short but whitelisted language tokens
    assert mem.get_vocab("go") is not None
    assert mem.get_vocab("js") is not None
    assert mem.get_vocab("c") is not None


def test_kind_of_detects_technical_tokens():
    assert learn._kind_of("get_user") == "tech"
    assert learn._kind_of("camelCase") == "tech"
    assert learn._kind_of("user.name") == "tech"
    assert learn._kind_of("v2") == "tech"
    assert learn._kind_of("hello") == "word"


def test_learn_prompt_mines_only_literal_keywords():
    learn.learn_prompt("Build a React app with a dark mode toggle")
    frameworks = [p["value"] for p in mem.top_prefs("framework", min_weight=0.4)]
    styles = [p["value"] for p in mem.top_prefs("style", min_weight=0.4)]
    assert "React" in frameworks
    assert "dark mode" in styles
    # No keyword for Flask/TypeScript was present → nothing invented
    assert "Flask" not in frameworks
    langs = [p["value"] for p in mem.top_prefs("language", min_weight=0.4)]
    assert "TypeScript" not in langs


def test_learn_prompt_no_substring_false_positive():
    # "concise" keyword must not fire on an unrelated word that contains it.
    learn.learn_prompt("please reconciles the ledger entries")
    lengths = [p["value"] for p in mem.top_prefs("length", min_weight=0.4)]
    assert "concise" not in lengths


def test_feedback_asymmetric_deltas_on_single_token():
    mem.bump_vocab("responsive")            # confidence starts at 0.5
    learn.feedback("ignore", "responsive")
    assert mem.get_vocab("responsive")["confidence"] == pytest.approx(0.5 - 0.03)

    mem.set_vocab_confidence("responsive", 0.5)
    learn.feedback("delete", "responsive")
    assert mem.get_vocab("responsive")["confidence"] == pytest.approx(0.5 - 0.20)

    mem.set_vocab_confidence("responsive", 0.5)
    learn.feedback("accept", "responsive")
    # accept both bumps (+0.02 via bump_vocab) then applies +0.08 on top
    assert mem.get_vocab("responsive")["confidence"] > 0.5


def test_feedback_unknown_kind_is_noop():
    mem.bump_vocab("stable")
    before = mem.get_vocab("stable")["confidence"]
    learn.feedback("bogus", "stable")
    assert mem.get_vocab("stable")["confidence"] == pytest.approx(before)


def test_feedback_multiword_targets_phrase_confidence():
    mem.bump_phrase("write a function")     # confidence 0.5
    learn.feedback("delete", "write a function")
    assert mem.get_phrase("write a function")["confidence"] == pytest.approx(0.5 - 0.20)


# ── prediction.py ─────────────────────────────────────────────────────────────

def test_predict_midword_completes_from_vocab():
    mem.bump_vocab("responsive")
    mem.set_vocab_confidence("responsive", 0.9)
    out = pred.predict("make it respon")
    assert out is not None
    assert out["full"].lower().endswith("responsive")
    assert out["suggestion"] == "sive"     # only the unt­yped tail
    assert out["kind"] == "word"


def test_predict_midword_skips_below_threshold():
    mem.bump_vocab("responsive")
    mem.set_vocab_confidence("responsive", pred.MIN_SHOW_CONFIDENCE - 0.05)
    assert pred.predict("make it respon") is None


def test_predict_boundary_uses_ngram():
    for _ in range(3):
        mem.bump_ngram("write", "a")
    mem.set_ngram_confidence("write", "a", 0.9)
    out = pred.predict("write ")
    assert out is not None
    assert out["suggestion"].strip() == "a"


def test_predict_phrase_completion_at_boundary():
    mem.bump_phrase("create a responsive login page")
    mem.set_phrase_confidence("create a responsive login page", 0.9)
    out = pred.predict("create a responsive")
    assert out is not None
    # the tail continues the learned phrase
    assert "login" in out["full"].lower()


def test_predict_empty_returns_none():
    assert pred.predict("") is None
    assert pred.predict("   ") is None


# ── prediction.py: per-kind index invalidation ────────────────────────────────
# The index mirrors three tables. Each has its own revision counter so a write
# rebuilds only what it changed — and a preference write, which changes a table
# the index does not read at all, rebuilds nothing. These tests pin WHICH kinds
# rebuild, because the failure mode of getting it wrong splits two ways: too few
# rebuilds serves stale suggestions, too many silently restores the cost the
# per-kind counters exist to remove.

def _watch_rebuilds(monkeypatch):
    """Return a list that records which kinds get rebuilt from now on.

    The index is refreshed first so the watcher sees only rebuilds caused by the
    test's own writes, not the initial cold build.
    """
    idx = pred._index()
    idx._refresh()
    hits: list[str] = []
    for kind in pred._INDEX_KINDS:
        real = idx._rebuilders[kind]

        def probe(_k=kind, _r=real):
            hits.append(_k)
            _r()

        monkeypatch.setitem(idx._rebuilders, kind, probe)
    return hits


def test_pref_write_rebuilds_nothing(monkeypatch):
    """The headline win: preferences feed improve.py, never a prediction."""
    hits = _watch_rebuilds(monkeypatch)
    mem.bump_pref("framework", "React")
    mem.set_pref_weight("framework", "React", 0.9)
    pred.predict("some tex")
    assert hits == []


def test_each_write_rebuilds_only_its_own_kind(monkeypatch):
    for write, expected in (
        (lambda: mem.bump_vocab("alpha"), "vocab"),
        (lambda: mem.bump_phrase("build a thing"), "phrases"),
        (lambda: mem.bump_ngram("build", "thing"), "ngrams"),
    ):
        hits = _watch_rebuilds(monkeypatch)
        write()
        pred.predict("alp")
        assert hits == [expected], f"{expected} write rebuilt {hits}"


def test_confidence_updates_rebuild_only_their_own_kind(monkeypatch):
    """`observe_accept`/`observe_ignore` land here — one table, one rebuild."""
    mem.bump_vocab("alpha")
    mem.bump_phrase("build a thing")
    hits = _watch_rebuilds(monkeypatch)
    mem.set_phrase_confidence("build a thing", 0.9)
    pred.predict("alph")        # any lookup refreshes every stale kind
    assert hits == ["phrases"]


def test_learn_text_rebuilds_all_three_kinds(monkeypatch):
    """A real message legitimately writes every mirrored table, so it must still
    rebuild all three. Per-kind counters are not meant to make this cheaper."""
    hits = _watch_rebuilds(monkeypatch)
    learn.learn_text("build a responsive dashboard with charts", project="demo")
    pred.predict("respon")
    assert sorted(hits) == ["ngrams", "phrases", "vocab"]


def test_one_refresh_per_change_not_per_lookup(monkeypatch):
    """Three lookups after one write cost one rebuild, not three."""
    hits = _watch_rebuilds(monkeypatch)
    mem.bump_vocab("alpha")
    for _ in range(3):
        pred.predict("alp")
    assert hits == ["vocab"]


def test_index_sees_a_word_learned_after_it_was_built():
    """Freshness is the guarantee the counters exist to keep. A word learned
    after the index was built must be predictable on the very next keystroke."""
    pred.predict("zz")                      # force a build
    mem.bump_vocab("zebrafish")
    mem.set_vocab_confidence("zebrafish", 0.9)
    out = pred.predict("zebraf")
    assert out is not None
    assert out["full"].lower() == "zebrafish"


def test_wipe_makes_the_index_forget():
    """'Forget me' must reach the in-memory mirror, not just the tables. These
    are raw DELETEs, so only memory.wipe()'s own touch invalidates the index."""
    mem.bump_vocab("secretproject")
    mem.set_vocab_confidence("secretproject", 0.9)
    assert pred.predict("secretpro") is not None
    mem.wipe()
    assert pred.predict("secretpro") is None


def test_optimize_invalidates_the_index():
    """optimize() prunes through raw SQL that bumps no counter, so it signals
    explicitly. Without that the index keeps serving pruned rows."""
    # NB: `agent2.core.pil.optimize` is shadowed by the re-exported function of
    # the same name, so the module has to be imported by its full path.
    from agent2.core.pil.optimize import optimize as run_optimize

    mem.bump_vocab("throwaway")
    mem.set_vocab_confidence("throwaway", 0.9)
    assert pred.predict("throwaw") is not None

    # Age the row past the prune window and drop it below the value floor.
    db.exe("UPDATE pil_vocab SET updated_at = datetime('now','-30 days'), "
           "frequency = 1, confidence = 0.1 WHERE token = 'throwaway'")
    report = run_optimize()
    assert report["pruned_vocab"] >= 1
    assert pred.predict("throwaw") is None


def test_optimize_that_changed_nothing_rebuilds_nothing(monkeypatch):
    from agent2.core.pil.optimize import optimize as run_optimize

    mem.bump_vocab("keepme")                # fresh, so nothing is prunable
    hits = _watch_rebuilds(monkeypatch)
    run_optimize()
    pred.predict("keep")
    assert hits == []


# ── optimize.py: phrase merging ───────────────────────────────────────────────
# `_merge_phrases` folds a phrase into a longer one that starts with it. It used
# to compare every phrase against every other (O(n²), 3.2 s at 4000 phrases) and
# now generates the at-most-`words` prefix candidates and looks them up. These
# tests pin the SEMANTICS the rewrite had to preserve, not the speed: the pass
# rewrites the user's learned data, so a wrong fold is silent data loss.

def _merge():
    from agent2.core.pil.optimize import _merge_phrases
    return _merge_phrases()


def _phrase_rows():
    return {r["body"]: (r["frequency"], r["confidence"])
            for r in db.qall("SELECT body, frequency, confidence FROM pil_phrases")}


def test_merge_folds_a_word_boundary_prefix():
    mem.bump_phrase("build a thing", freq=3)
    mem.bump_phrase("build a thing now", freq=2)
    assert _merge() == 1
    rows = _phrase_rows()
    assert "build a thing" not in rows          # absorbed
    assert rows["build a thing now"][0] == 5    # frequency carried over


def test_merge_ignores_a_prefix_that_is_not_a_word_boundary():
    """'build a th' is a character prefix of 'build a thing' but not a word one —
    folding it would glue unrelated text together."""
    mem.bump_phrase("build a th", freq=4)
    mem.bump_phrase("build a thing", freq=2)
    assert _merge() == 0
    assert set(_phrase_rows()) == {"build a th", "build a thing"}


def test_merge_is_case_insensitive_but_keeps_stored_casing():
    mem.bump_phrase("Build A Thing", freq=3)
    mem.bump_phrase("build a thing now", freq=1)
    assert _merge() == 1
    rows = _phrase_rows()
    assert "Build A Thing" not in rows
    assert rows["build a thing now"][0] == 4


def test_merge_keeps_the_higher_confidence():
    mem.bump_phrase("run the tests", freq=1)
    mem.bump_phrase("run the tests now", freq=1)
    mem.set_phrase_confidence("run the tests", 0.9)
    mem.set_phrase_confidence("run the tests now", 0.4)
    _merge()
    assert _phrase_rows()["run the tests now"][1] == pytest.approx(0.9)


def test_merge_collapses_a_chain_into_the_longest():
    for body, f in (("run the", 2), ("run the tests", 3), ("run the tests now", 4)):
        mem.bump_phrase(body, freq=f)
    assert _merge() == 2
    rows = _phrase_rows()
    assert set(rows) == {"run the tests now"}
    assert rows["run the tests now"][0] == 9    # every frequency preserved


def test_merge_leaves_unrelated_phrases_alone():
    mem.bump_phrase("alpha beta", freq=1)
    mem.bump_phrase("gamma delta", freq=2)
    assert _merge() == 0
    assert len(_phrase_rows()) == 2


def test_merge_applies_atomically_on_write_failure():
    """The writes go out as one transaction. A failure must leave the data as it
    was — the old per-merge form could delete the short phrase and then fail the
    update carrying its frequency, losing that count for good.

    ⚠️ The failure must land on the SECOND statement, after the first has really
    written. Failing the first proves nothing: nothing is written either way, so
    the test passes with or without the transaction. Which statement goes first
    is deliberately not asserted — inside one transaction the order cannot change
    the committed state, so pinning it would test an implementation detail.
    """
    import sys

    # The submodule is shadowed by the re-exported function of the same name, so
    # neither `import ... as` nor `from ... import` reaches the module object.
    opt_mod = sys.modules["agent2.core.pil.optimize"]

    mem.bump_phrase("build a thing", freq=3)
    mem.bump_phrase("build a thing now", freq=2)
    before = _phrase_rows()

    real = opt_mod.exemany
    calls = []

    def fail_on_the_second(sql, params):
        calls.append(sql)
        if len(calls) > 1:
            raise RuntimeError("disk full")
        return real(sql, params)

    opt_mod.exemany = fail_on_the_second
    try:
        assert _merge() == 0                   # reported as nothing merged
    finally:
        opt_mod.exemany = real

    assert len(calls) == 2                      # the first write really ran
    assert _phrase_rows() == before             # and was rolled back


def test_rebuild_failure_degrades_to_no_suggestion(monkeypatch):
    """PIL failure means 'do nothing', never an exception into the agent loop."""
    mem.bump_vocab("alpha")
    mem.set_vocab_confidence("alpha", 0.9)
    pred.invalidate_index()

    def boom(_limit=0):
        raise RuntimeError("db is gone")

    monkeypatch.setattr(mem, "_vocab_ranked", boom)
    assert pred.predict("alp") is None      # no raise, no suggestion


def test_failed_rebuild_is_not_retried_every_keystroke(monkeypatch):
    """A failing table must not be re-queried per lookup: with busy_timeout at
    10 s a locked DB would stall every keystroke instead of degrading."""
    pred.invalidate_index()
    calls = []

    def boom(_limit=0):
        calls.append(1)
        raise RuntimeError("db is locked")

    monkeypatch.setattr(mem, "_vocab_ranked", boom)
    for _ in range(5):
        pred.predict("alp")
    assert len(calls) == 1


def test_revision_counters_are_independent():
    before = mem.revisions()
    mem.bump_vocab("countercheck")
    after = mem.revisions()
    assert after["vocab"] == before["vocab"] + 1
    for kind in ("phrases", "ngrams", "prefs"):
        assert after[kind] == before[kind], f"{kind} moved on a vocab write"


def test_revision_sum_still_moves_on_any_write():
    """The no-argument form stays a single int that changes whenever anything
    does, so a cache mirroring the whole store can still use it."""
    for write in (lambda: mem.bump_vocab("sumcheck"),
                  lambda: mem.bump_phrase("sum check phrase"),
                  lambda: mem.bump_ngram("sum", "check"),
                  lambda: mem.bump_pref("framework", "Vue")):
        before = mem.revision()
        write()
        assert mem.revision() > before


def test_unknown_kind_still_signals_other_processes():
    """An unknown kind must not silently drop the cross-process notify — a lost
    signal is a staleness bug, while an extra one only costs a rebuild."""
    mem._last_sync_bump = 0.0               # clear the throttle
    seen = []
    from agent2.core import sync
    sync.subscribe("pil", lambda _t, _p: seen.append(1))
    mem._touch("not_a_real_kind")
    assert seen                             # notify fired anyway
    assert "not_a_real_kind" not in mem.revisions()


# ── grammar.py ────────────────────────────────────────────────────────────────

def test_grammar_fixes_typos_and_capitalizes():
    out, edits = gram.enhance("teh cat sat. it dont move")
    assert "The cat" in out                 # typo fixed + sentence capitalized
    assert "don't" in out
    assert out[0].isupper()                 # sentence capitalization
    assert edits                            # edits were reported


def test_grammar_never_touches_code_fences():
    src = "run this ```python\ndef f(): retrun 1\n``` and teh rest"
    out, _ = gram.enhance(src)
    assert "retrun 1" in out                # typo INSIDE code left untouched
    assert "the rest" in out                # typo outside code fixed


def test_grammar_preserves_urls_paths_and_identifiers():
    src = "visit https://Example.com/teh and edit src/App_Main.py using get_user"
    out, _ = gram.enhance(src)
    assert "https://Example.com/teh" in out   # URL untouched (incl. 'teh')
    assert "src/App_Main.py" in out           # path untouched
    assert "get_user" in out                  # snake_case identifier untouched


def test_grammar_leaves_flags_and_digits_alone():
    src = "pass --dry-run to v2 build"
    out, _ = gram.enhance(src)
    assert "--dry-run" in out
    assert "v2" in out


def test_grammar_spares_learned_vocab():
    # A personal/jargon token that looks like a typo must survive when known.
    out, _ = gram.enhance("myproj is ready", vocab_lookup=lambda w: w == "myproj")
    assert "myproj" in out


# ── improve.py ────────────────────────────────────────────────────────────────

def test_improve_noop_without_build_intent():
    for _ in range(20):
        mem.bump_pref("framework", "React")
    text, added = imp.improve("what is the capital of France")
    assert text == "what is the capital of France"
    assert added == []


def test_improve_noop_without_strong_prefs():
    # Build intent present but no pref clears MIN_PREF_WEIGHT → unchanged.
    text, added = imp.improve("build a web app")
    assert text == "build a web app"
    assert added == []


def test_improve_appends_only_proven_prefs():
    # Make a strong framework pref, then a build-intent prompt that omits it.
    for _ in range(20):
        mem.bump_pref("framework", "React")
    weight = mem.top_prefs("framework", min_weight=imp.MIN_PREF_WEIGHT)
    assert weight, "pref should exceed MIN_PREF_WEIGHT after repeated bumps"
    text, added = imp.improve("build a dashboard")
    assert text != "build a dashboard"     # enrichment appended
    assert any("React" in a for a in added)


def test_improve_skips_pref_already_present():
    for _ in range(20):
        mem.bump_pref("framework", "React")
    text, added = imp.improve("build a React dashboard")
    # React already literally present → not re-added
    assert not any(a == "React" for a in added)


# ── facade (__init__.py) ──────────────────────────────────────────────────────

def _enable(**flags):
    """Helper: set PIL settings explicitly for a test."""
    for key, val in flags.items():
        set_setting(f"pil.{key}", "1" if val else "0")


def test_process_outgoing_prompt_grammar_gated_by_toggle():
    _enable(enabled=True, grammar=False, improve=False)
    final, report = pil.process_outgoing_prompt("teh cat")
    assert final == "teh cat"               # grammar off → unchanged
    assert report["changed"] is False

    _enable(enabled=True, grammar=True, improve=False)
    final, report = pil.process_outgoing_prompt("teh cat")
    assert "The cat" in final
    assert report["grammar_applied"] is True
    assert report["changed"] is True


def test_process_outgoing_prompt_disabled_when_master_off():
    _enable(enabled=False, grammar=True, improve=True)
    final, report = pil.process_outgoing_prompt("teh cat")
    assert final == "teh cat"
    assert report["changed"] is False


def test_learn_from_original_send_enhanced_invariant():
    # The facade must learn from the ORIGINAL text while returning an enhanced
    # copy for sending — the two paths are independent.
    _enable(enabled=True, learning=True, prediction=True, grammar=True, improve=False)
    original = "teh responsive dashboard"
    pil.learn_from_message(original)        # learns from what the user wrote
    final, report = pil.process_outgoing_prompt(original)
    # Learning recorded the real (misspelled-in-context but valid) word.
    assert mem.get_vocab("responsive") is not None
    # Sent copy is grammar-corrected (typo fixed + sentence capitalized). The
    # typo "teh" is fixed even though it was learned into vocab — curated typos
    # always win, so grammar never becomes useless once a word has been typed.
    assert "The responsive" in final


def test_wipe_resets_seed_flag():
    set_setting("pil.seeded", "1")
    pil.wipe()
    assert (get_setting("pil.seeded", "0") or "0") == "0"


def test_optimize_skipped_when_toggle_off_and_not_forced():
    _enable(enabled=True, autooptimize=False)
    assert pil.optimize().get("skipped") is True
    # force overrides the toggle
    forced = pil.optimize(force=True)
    assert "skipped" not in forced or forced.get("skipped") is not True

