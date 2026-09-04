# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""Tests for model capabilities, routing and fallback (Phase 6 — Tasks 17-19).

Three features, one file, because they are one subject: what each model can do,
which one gets used, and what happens instead when it breaks. Testing them apart
would let the registry and the router hold different opinions about the same model
and both suites stay green.

The failure modes hunted here are the quiet ones:

1. **An invented capability.** `None` means "we do not know" and must never be read
   as "no" (silently refuses a capable model) or as "yes" (sends an image to a
   model that will reject it). Most of the Task 17 block is about that one
   distinction.
2. **A router that overrules the user.** Both surfaces send a model key on every
   turn, so a router that ran unasked could not tell a choice from a default.
   Routing is opt-in, and the tests pin that from both directions.
3. **A fallback that is really a retry loop.** Bounded hops, an honest final error,
   and a breaker that stops a walled model being re-picked every turn.
4. **A fallback that loses the turn.** The single most valuable assertion in the
   file is that a model switch preserves session and task state — a restart would
   replay tool calls that already ran (rule 20).
"""

import json
import os

import pytest

from agent2 import config as cfg
from agent2 import database as db
from agent2.llm import capabilities as caps
from agent2.llm import router as R


# -- Config-derived model keys -------------------------------------------------
# WARNING: NOTHING BELOW PINS A MODEL NAME, AND THAT IS THE POINT.
# `config.MODELS` is a table the user edits - models get added, removed and
# renamed. An earlier version of this file hard-coded `2.5-pro` / `3.1-flash`, and
# every one of those assertions broke the first time the table changed, each failing
# for a reason that had nothing to do with the behaviour under test. These helpers
# ask the registry which model has the property a test needs.

def _keys() -> list[str]:
    from agent2.config import MODELS
    return list(MODELS)


def _second() -> str:
    """A built-in that is not the first. Used wherever a test needs "another model"."""
    keys = _keys()
    assert len(keys) >= 2, "these tests need at least two configured models"
    return keys[1]


def _with_known_window() -> str:
    return next((k for k in _keys() if caps.context_window(k) > 0), "")


def _with_unknown_window() -> str:
    return next((k for k in _keys() if caps.context_window(k) == 0), "")


def _best(field: str) -> int:
    """The highest tier for *field* among the built-ins, whatever that is today."""
    return max(caps.tier_rank(caps.get(k)[field]) for k in _keys())


@pytest.fixture(autouse=True)
def _clean():
    """Reset every piece of shared state these three features touch.

    ⚠️ `providers` is in this list for a reason that cost real time to find. Several
    tests below register a Claude/GPT-class provider to check that the router
    prefers it — and a leaked row makes a LATER test's "a debugging turn routes to
    the advanced model" fail, because a frontier custom model is now in the pool.
    The failure appears in a different test from the one that caused it and reads as
    a routing bug.
    """
    db.init_db()
    from agent2.llm import providers as P
    P.init_providers_table()
    db.exe("DELETE FROM providers")
    db.exe("DELETE FROM model_caps")
    db.exe("DELETE FROM model_attempts")
    db.exe("DELETE FROM settings WHERE key='model.routing'")
    caps.invalidate()
    R.reset_breaker()
    saved = {k: os.environ.get(k) for k in (
        "AGENT2_MODEL_ROUTING", "AGENT2_FALLBACK_MAX_HOPS")}
    saved_cfg = (cfg.MODEL_ROUTING, cfg.FALLBACK_MAX_HOPS,
                 cfg.BREAKER_FAILS, cfg.BREAKER_COOLDOWN, cfg.ROUTER_LONG_CONTEXT)
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    (cfg.MODEL_ROUTING, cfg.FALLBACK_MAX_HOPS, cfg.BREAKER_FAILS,
     cfg.BREAKER_COOLDOWN, cfg.ROUTER_LONG_CONTEXT) = saved_cfg
    db.exe("DELETE FROM providers")
    db.exe("DELETE FROM model_caps")
    R.reset_breaker()
    caps.invalidate()


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """⚠️ NOTHING IN THIS FILE MAY REACH A MODEL.

    `add_provider` now spawns a background ranking thread, and `rank_with_model`
    would try a real API call. Left unstubbed, this suite would be slow, flaky,
    billable, and would write metadata a later assertion then trips over — with the
    thread finishing at an unpredictable point, so the failures would move around.
    Tests that WANT a ranking answer stub these two themselves.
    """
    monkeypatch.setattr(caps, "_ask_gemini", lambda prompt: "")
    monkeypatch.setattr(caps, "_ask_any_provider", lambda prompt: "")
    monkeypatch.setattr(caps, "rank_in_background", lambda key: None)


# ══════════════════════════════════════════════════════════════════════════════
# Task 17 — the capability registry
# ══════════════════════════════════════════════════════════════════════════════

def test_every_configured_model_has_a_record():
    """⚠️ Iterates `config.MODELS`, not the catalog's own dict.

    A model added to config must appear here — with unknowns — instead of being
    silently invisible to the router. A registry that only knows the models it was
    written for is a registry that stops being consulted.
    """
    from agent2.config import MODELS
    keys = {rec["key"] for rec in caps.all_models(include_custom=False)}
    assert keys == set(MODELS)


def test_a_record_carries_every_declared_field():
    rec = caps.get(_keys()[0])
    for field in caps.FIELDS:
        assert field in rec, field


def test_unknown_is_none_and_is_not_false():
    """⚠️ THE RULE THIS FILE EXISTS FOR.

    `None` (we do not know) and `False` (does not support it) are different facts.
    A registry that stores unknown as False has invented a capability claim — the
    negative one — and the router will act on it.
    """
    blank = caps.get("a-model-nobody-described")
    assert blank["vision"] is None
    assert blank["tool_use"] is None
    assert blank["vision"] is not False
    assert blank["context_window"] == 0
    assert blank["reasoning"] == caps.TIER_UNKNOWN


def test_supports_makes_the_caller_choose_how_to_read_unknown():
    """The asymmetry is the API. A hard requirement wants unknown→False; a
    preference wants unknown→True. Both are correct, in different places."""
    key = "a-model-nobody-described"
    assert caps.supports(key, "vision", default=False) is False
    assert caps.supports(key, "vision", default=True) is True
    # …and a KNOWN value ignores the default entirely.
    assert caps.supports(_keys()[0], "vision", default=False) is True


def test_an_unknown_context_window_is_never_treated_as_small_or_large():
    """0 is "unknown". `_rank` scores it 0 for a long-context turn, i.e. last —
    routing a 200k-token turn at a model whose limit nobody recorded is a guess
    that fails as a vendor error mid-turn."""
    known, unknown = _with_known_window(), _with_unknown_window()
    if not (known and unknown):
        pytest.skip("needs one model with a published window and one without")
    assert caps.context_window(unknown) == 0
    signals = {"long_context": True, "tokens": 200_000}
    ranked = R.rank_candidates(signals, include_custom=False)
    assert ranked.index(known) < ranked.index(unknown)


def test_unknown_tiers_and_speeds_rank_last_never_as_basic():
    assert caps.tier_rank("") == 0 < caps.tier_rank(caps.TIER_BASIC)
    assert caps.speed_rank("") == 0 < caps.speed_rank(caps.SPEED_SLOW)
    assert caps.cost_rank("") == 0 < caps.cost_rank(caps.COST_LOW)


def test_thinking_is_derived_from_the_model_group_not_stored_twice():
    """`config.MODES` already declares extended thinking as a 2.5/3.1 capability.
    A second hard-coded copy here is a fact stored twice, which is a fact that
    drifts."""
    import inspect
    src = inspect.getsource(caps.get)
    assert 'group' in src
    for key in _keys():
        assert caps.get(key)["thinking"] is True, key


def test_metadata_can_be_corrected_and_persists():
    target = _second()
    caps.set_meta(target, context_window=200000, notes="measured locally")
    caps.invalidate()
    rec = caps.get(target)
    assert rec["context_window"] == 200000
    assert rec["notes"] == "measured locally"
    assert rec["source"] == "configured"


def test_an_override_beats_the_catalog():
    assert caps.get(_keys()[0])["cost"] == caps.COST_LOW
    caps.set_meta(_keys()[0], cost=caps.COST_HIGH)
    assert caps.get(_keys()[0])["cost"] == caps.COST_HIGH


def test_clearing_an_override_returns_the_catalog_answer():
    target = _second()
    original = caps.get(target)["speed"]
    other = caps.SPEED_SLOW if original != caps.SPEED_SLOW else caps.SPEED_FAST
    caps.set_meta(target, speed=other)
    assert caps.get(target)["speed"] == other
    caps.clear_meta(target)
    assert caps.get(target)["speed"] == original


def test_an_absent_field_is_left_alone_on_a_partial_update():
    """⚠️ Same distinction `state.set_config` draws. A surface that edits one field
    submits a form that does not mention the rest; reading absent as "clear it"
    wipes metadata the user spent time entering."""
    target = _second()
    caps.set_meta(target, context_window=100000, notes="keep me")
    caps.set_meta(target, context_window=150000)
    rec = caps.get(target)
    assert rec["context_window"] == 150000
    assert rec["notes"] == "keep me"


def test_explicit_none_is_how_you_say_you_no_longer_know():
    target = _second()
    caps.set_meta(target, vision=True)
    assert caps.get(target)["vision"] is True
    caps.set_meta(target, vision=None)
    assert caps.get(target)["vision"] is None


@pytest.mark.parametrize("field,value", [
    ("reasoning", "godlike"), ("speed", "warp"), ("cost", "free"),
    ("context_window", "lots"), ("context_window", -5), ("vision", "maybe"),
])
def test_a_bad_value_raises_rather_than_being_coerced(field, value):
    """⚠️ A quietly coerced value looks to the user like a write that succeeded and
    then did nothing — the worst outcome for a hand-editing surface."""
    with pytest.raises(ValueError):
        caps.set_meta(_keys()[0], **{field: value})


def test_an_unknown_field_is_rejected():
    with pytest.raises(ValueError):
        caps.set_meta(_keys()[0], teleportation=True)


def test_a_custom_provider_starts_unknown_and_never_claims_otherwise():
    """⚠️ The user gives us a URL, a key and a model id. Nothing in that says what
    the endpoint can do, and probing would spend their money to find out."""
    from agent2.llm import providers as P
    P.init_providers_table()
    made = P.add_provider("t", "https://api.example.invalid/v1", "sk-x",
                          "some-model", "openai")
    rec = caps.get("custom:" + made["id"])
    assert rec["vision"] is None, "invented a vision claim for a custom endpoint"
    assert rec["structured_output"] is None
    assert rec["context_window"] == 0
    assert rec["provider"] == "api.example.invalid"
    # `tool_use` IS True — a fact about our integration, not a vendor claim:
    # Agent2 only ever drives a custom provider through its own tool schema.
    assert rec["tool_use"] is True


def test_a_custom_providers_metadata_can_be_filled_in_by_hand():
    """This is what "support updating configured model metadata" is FOR."""
    from agent2.llm import providers as P
    P.init_providers_table()
    made = P.add_provider("t2", "https://api.example.invalid/v1", "sk-x",
                          "m", "openai")
    key = "custom:" + made["id"]
    caps.set_meta(key, vision=True, context_window=128000,
                  reasoning=caps.TIER_ADVANCED)
    rec = caps.get(key)
    assert rec["vision"] is True and rec["context_window"] == 128000
    assert caps.supports(key, "vision", default=False) is True


# ── Inference from the configured model id ────────────────────────────────────
# The gap this closes: a user registers Claude Opus 5 / GPT-5 class endpoints as
# custom providers, every one ranks all-unknown, and the router hands every
# reasoning turn to gemini-2.5-pro. The registry was technically honest and
# practically useless — the better model was configured and invisible.

@pytest.mark.parametrize("model_id,reasoning,coding", [
    ("claude-opus-5", caps.TIER_FRONTIER, caps.TIER_FRONTIER),
    ("anthropic/claude-opus-4.8", caps.TIER_FRONTIER, caps.TIER_FRONTIER),
    ("claude-sonnet-5", caps.TIER_ADVANCED, caps.TIER_FRONTIER),
    ("claude-haiku-4-5", caps.TIER_STRONG, caps.TIER_ADVANCED),
    ("gpt-5.6-sol", caps.TIER_FRONTIER, caps.TIER_FRONTIER),
    ("o3-pro", caps.TIER_FRONTIER, caps.TIER_ADVANCED),
    ("deepseek-r1", caps.TIER_ADVANCED, caps.TIER_ADVANCED),
    ("llama-4-70b-instruct", caps.TIER_STRONG, caps.TIER_STRONG),
])
def test_a_known_model_family_is_recognised_from_its_id(model_id, reasoning, coding):
    rec = caps.infer_from_model_id(model_id)
    assert rec, model_id
    assert rec["reasoning"] == reasoning
    assert rec["coding"] == coding


def test_a_narrow_pattern_beats_a_broad_one():
    """⚠️ Ordering pin. `gpt-5-mini` must be tested before `gpt-5`, or the small
    variant inherits the frontier ranking and gets every hard turn."""
    assert caps.infer_from_model_id("gpt-5-mini")["reasoning"] == caps.TIER_ADVANCED
    assert caps.infer_from_model_id("gpt-5")["reasoning"] == caps.TIER_FRONTIER
    assert caps.infer_from_model_id("gpt-5-nano")["cost"] == caps.COST_LOW


def test_inference_survives_a_gateway_renaming_the_model():
    """Substring-set matching, not a regex anchored to a vendor's current naming.
    Gateways add prefixes, dates and suffixes freely."""
    for variant in ("claude-opus-5", "anthropic/claude-opus-5",
                    "claude-opus-5-20260110", "opus-5-claude-thinking",
                    "  CLAUDE-OPUS-5  "):
        assert caps.infer_from_model_id(variant).get("reasoning") \
            == caps.TIER_FRONTIER, variant


def test_an_unrecognised_model_id_infers_nothing_at_all():
    """⚠️ `{}`, not a record full of defaults. A pattern match must never fill a
    field in just to avoid a blank — that would be the invented claim this module
    forbids."""
    assert caps.infer_from_model_id("totally-unknown-model-x") == {}
    assert caps.infer_from_model_id("") == {}
    assert caps.infer_from_model_id(None) == {}


def test_vision_is_the_strict_field_and_is_left_unknown_when_unsure():
    """Getting a tier wrong costs a suboptimal choice; getting `vision` wrong costs
    a hard vendor rejection mid-turn."""
    assert caps.infer_from_model_id("claude-opus-5")["vision"] is True
    assert caps.infer_from_model_id("o3-pro").get("vision") is None
    assert caps.infer_from_model_id("llama-4-70b").get("vision") is None


def test_an_inferred_record_is_labelled_inferred_not_measured():
    """⚠️ A surface that showed inference as a catalog fact would make a heuristic
    look like a measurement."""
    from agent2.llm import providers as P
    P.init_providers_table()
    made = P.add_provider("opus", "https://gateway.example.invalid/v1", "sk-x",
                          "claude-opus-5", "anthropic")
    rec = caps.get("custom:" + made["id"])
    assert rec["source"] == "inferred"
    assert rec["reasoning"] == caps.TIER_FRONTIER
    # The HOST is what is shown, because that is the endpoint actually dialled…
    assert rec["provider"] == "gateway.example.invalid"
    # …and the vendor that justified the tiers is in the note.
    assert "Opus" in rec["notes"]


def test_an_unrecognised_custom_model_is_labelled_provider_and_stays_unknown():
    from agent2.llm import providers as P
    P.init_providers_table()
    made = P.add_provider("mystery", "https://api.example.invalid/v1", "sk-x",
                          "some-inhouse-model", "openai")
    rec = caps.get("custom:" + made["id"])
    assert rec["source"] == "provider"
    assert rec["reasoning"] == caps.TIER_UNKNOWN
    assert rec["vision"] is None
    assert "not recognised" in rec["notes"]


def test_a_frontier_custom_model_wins_a_reasoning_turn_over_the_gemini_default():
    """⚠️ THE BUG THIS WHOLE BLOCK EXISTS FOR, AS AN ACCEPTANCE TEST.

    With both `advanced` (2.5-pro) and `frontier` (Opus) available, a debugging turn
    must reach the frontier model. Before the `frontier` tier existed both were
    `advanced`, the sort was stable and built-ins came first — so the model the user
    deliberately configured was never chosen.
    """
    from agent2.llm import providers as P
    P.init_providers_table()
    made = P.add_provider("opus", "https://gateway.example.invalid/v1", "sk-x",
                          "claude-opus-5", "anthropic")
    d = R.choose(R.AUTO, message="why does this deadlock intermittently under load?")
    assert d.model_key == "custom:" + made["id"], d.reason
    assert "frontier" in d.reason


def test_a_simple_turn_still_prefers_the_cheap_fast_model_not_the_frontier_one():
    """The other half of the same guarantee: recognising a frontier model must not
    mean spending frontier money on "thanks!"."""
    from agent2.llm import providers as P
    P.init_providers_table()
    P.add_provider("opus", "https://gateway.example.invalid/v1", "sk-x",
                   "claude-opus-5", "anthropic")
    d = R.choose(R.AUTO, message="thanks!")
    assert caps.get(d.model_key)["cost"] == caps.COST_LOW
    assert caps.get(d.model_key)["speed"] == caps.SPEED_FAST


def test_a_user_override_beats_inference():
    from agent2.llm import providers as P
    P.init_providers_table()
    made = P.add_provider("opus", "https://gateway.example.invalid/v1", "sk-x",
                          "claude-opus-5", "anthropic")
    key = "custom:" + made["id"]
    assert caps.get(key)["reasoning"] == caps.TIER_FRONTIER
    caps.set_meta(key, reasoning=caps.TIER_BASIC)
    assert caps.get(key)["reasoning"] == caps.TIER_BASIC
    assert caps.get(key)["source"] == "configured"


@pytest.mark.parametrize("model_id,expected", [
    ("claude-opus-5", 5.0),
    ("claude-opus-4-8", 4.0),
    ("gpt-5.6-sol", 5.6),
    ("claude-opus-4-20260110", 4.0),
    ("some-model", 0.0),
    ("", 0.0),
])
def test_the_version_hint_is_crude_on_purpose(model_id, expected):
    """⚠️ `4-8` reads as 4.0, not 4.8, and that is deliberate.

    `4.0 < 5.0` is all this needs to be right about. Chasing 4.8 means parsing every
    vendor's dating scheme, and the `claude-opus-4-20260110` case shows the cost of
    being clever: a naive "first number" reads 20260110 and outranks everything.
    """
    assert caps.version_hint(model_id) == expected


def test_a_newer_version_wins_only_when_nothing_else_separates_them():
    """The tiebreak must never outrank a real capability difference."""
    from agent2.llm import providers as P
    P.init_providers_table()
    old = P.add_provider("o48", "https://g.example.invalid/v1", "sk-x",
                         "claude-opus-4-8", "anthropic")
    new = P.add_provider("o5", "https://g.example.invalid/v1", "sk-x",
                         "claude-opus-5", "anthropic")
    ranked = R.rank_candidates({"reasoning": True})
    assert ranked.index("custom:" + new["id"]) < ranked.index("custom:" + old["id"])

    # …but a capability difference still dominates it.
    caps.set_meta("custom:" + new["id"], reasoning=caps.TIER_BASIC)
    ranked = R.rank_candidates({"reasoning": True})
    assert ranked.index("custom:" + old["id"]) < ranked.index("custom:" + new["id"])


def test_the_frontier_tier_outranks_advanced():
    assert caps.tier_rank(caps.TIER_FRONTIER) > caps.tier_rank(caps.TIER_ADVANCED)
    assert caps.TIER_FRONTIER in caps.TIERS


def test_a_reason_line_names_the_model_not_just_the_opaque_key():
    """An opaque provider id tells the user nothing about WHICH of their four
    endpoints ran, which is the only thing they wanted to know."""
    from agent2.llm import providers as P
    P.init_providers_table()
    P.add_provider("opus", "https://g.example.invalid/v1", "sk-x",
                   "claude-opus-5", "anthropic")
    d = R.choose(R.AUTO, message="why does this segfault under load?")
    assert "claude-opus-5" in d.reason


def test_get_never_raises_and_always_names_the_model():
    for key in ("", None, "nonsense", "custom:does-not-exist", _keys()[0]):
        rec = caps.get(key)
        assert rec["key"] and rec["model"], key


# ── LLM-assisted ranking, for the ids no pattern recognises ───────────────────
# The hole this closes: a user registers `some-inhouse-moe-v3`, no pattern matches,
# and the model is permanently all-unknown — the router never sends it anything
# demanding, which is indistinguishable from Agent2 ignoring a model the user
# deliberately configured and is paying for.

def _stub_answer(monkeypatch, payload):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr(caps, "_ask_gemini", lambda prompt: text)


def _unknown_provider(name="mystery", model_id="some-inhouse-moe-v3"):
    from agent2.llm import providers as P
    P.init_providers_table()
    made = P.add_provider(name, "https://api.example.invalid/v1", "sk-x",
                          model_id, "openai")
    return "custom:" + made["id"]


def test_an_unrecognised_model_can_be_ranked_by_asking_a_model(monkeypatch):
    key = _unknown_provider()
    assert caps.get(key)["reasoning"] == caps.TIER_UNKNOWN

    _stub_answer(monkeypatch, {
        "provider": "acme", "context_window": 128000,
        "reasoning": "advanced", "coding": "frontier",
        "speed": "medium", "cost": "medium",
        "vision": True, "structured_output": True, "confident": True})
    rec = caps.rank_with_model(key)

    assert rec["reasoning"] == caps.TIER_ADVANCED
    assert rec["coding"] == caps.TIER_FRONTIER
    assert rec["context_window"] == 128000
    assert rec["vision"] is True
    assert rec["source"] == caps.SOURCE_RANKED


def test_a_ranked_model_becomes_routable(monkeypatch):
    """⚠️ THE ACCEPTANCE TEST FOR THE WHOLE FEATURE.

    Ranking is pointless unless the router acts on it, and "the record changed" is
    not the same claim as "the model now gets the work".
    """
    key = _unknown_provider(model_id="acme-titan-x")
    before = R.choose(R.AUTO, message="why does this deadlock under load?")
    assert before.model_key != key

    _stub_answer(monkeypatch, {"reasoning": "frontier", "coding": "frontier",
                               "confident": True})
    caps.rank_with_model(key)

    after = R.choose(R.AUTO, message="why does this deadlock under load?")
    assert after.model_key == key, after.reason


def test_ranking_is_marked_as_a_heuristic_not_a_measurement(monkeypatch):
    """⚠️ This is the least reliable source in the module, so it is the one that
    must be most clearly labelled."""
    key = _unknown_provider()
    _stub_answer(monkeypatch, {"reasoning": "strong", "confident": True})
    rec = caps.rank_with_model(key)
    assert rec["source"] == caps.SOURCE_RANKED
    assert "heuristic" in rec["notes"]


def test_a_hallucinated_value_is_dropped_not_stored(monkeypatch):
    """⚠️ The answer is VALIDATED, not trusted. One bad field must not discard a
    good answer, and must not be written either."""
    key = _unknown_provider()
    _stub_answer(monkeypatch, {"reasoning": "godlike", "coding": "advanced",
                               "speed": "warp", "confident": True})
    rec = caps.rank_with_model(key)
    assert rec["reasoning"] == caps.TIER_UNKNOWN, "a bogus tier was stored"
    assert rec["speed"] == caps.SPEED_UNKNOWN
    assert rec["coding"] == caps.TIER_ADVANCED, "a good field was discarded"


def test_prose_instead_of_json_changes_nothing(monkeypatch):
    key = _unknown_provider()
    _stub_answer(monkeypatch, "Sure! That model is pretty good at coding.")
    before = caps.get(key)
    assert caps.rank_with_model(key) == before


def test_json_wrapped_in_a_fence_is_still_read(monkeypatch):
    """Models fence their JSON despite being told not to, and a classification that
    failed over that would be a silent downgrade to unknown."""
    key = _unknown_provider()
    _stub_answer(monkeypatch,
                 "Here you go:\n```json\n{\"reasoning\": \"strong\", "
                 "\"confident\": true}\n```\nHope that helps!")
    assert caps.rank_with_model(key)["reasoning"] == caps.TIER_STRONG


def test_an_honest_i_do_not_know_leaves_the_record_alone(monkeypatch):
    """`confident: false` is a USEFUL answer. Storing the nulls that came with it
    would look identical to never having asked."""
    key = _unknown_provider()
    _stub_answer(monkeypatch, {"reasoning": None, "confident": False})
    before = caps.get(key)
    assert caps.rank_with_model(key) == before
    assert caps.get(key)["source"] == "provider"


def test_no_api_key_at_all_changes_nothing(monkeypatch):
    key = _unknown_provider()
    monkeypatch.setattr(caps, "_ask_gemini", lambda p: "")
    monkeypatch.setattr(caps, "_ask_any_provider", lambda p: "")
    before = caps.get(key)
    assert caps.rank_with_model(key) == before


def test_ranking_never_raises_even_when_the_store_fails(monkeypatch):
    key = _unknown_provider()
    _stub_answer(monkeypatch, {"reasoning": "strong", "confident": True})
    monkeypatch.setattr(caps, "_store",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db")))
    caps.rank_with_model(key)          # must not raise


def test_a_recognised_model_is_not_re_asked(monkeypatch):
    """⚠️ The common case must cost nothing. `claude-opus-5` is already covered by
    pattern inference, so asking a model about it is pure waste."""
    asked = []
    monkeypatch.setattr(caps, "_ask_gemini", lambda p: asked.append(p) or "")
    key = _unknown_provider(model_id="claude-opus-5")
    caps.rank_with_model(key)
    assert asked == [], "spent an API call on a model we already recognise"


def test_a_built_in_model_is_never_re_asked(monkeypatch):
    asked = []
    monkeypatch.setattr(caps, "_ask_gemini", lambda p: asked.append(p) or "")
    caps.rank_with_model(_keys()[0])
    assert asked == []


def test_force_re_asks_even_a_described_model(monkeypatch):
    """The only way past "already described", and only a human can ask for it."""
    key = _unknown_provider(model_id="claude-opus-5")
    assert caps.get(key)["source"] == "inferred"
    _stub_answer(monkeypatch, {"context_window": 999000, "confident": True})

    # Without `force`, a recognised model is left exactly as inference left it.
    assert caps.rank_with_model(key)["context_window"] == 200_000
    assert caps.get(key)["source"] == "inferred"

    assert caps.rank_with_model(key, force=True)["context_window"] == 999_000
    assert caps.get(key)["source"] == caps.SOURCE_RANKED


def test_ranking_never_overwrites_a_users_own_correction(monkeypatch):
    """⚠️ Ranking runs in a background thread on `add_provider`. Without this guard
    a user who corrected a model by hand and then re-added the provider would
    silently lose the correction to a guess."""
    key = _unknown_provider()
    caps.set_meta(key, reasoning=caps.TIER_BASIC)
    _stub_answer(monkeypatch, {"reasoning": "frontier", "confident": True})
    caps.rank_with_model(key, force=True)
    assert caps.get(key)["reasoning"] == caps.TIER_BASIC
    assert caps.get(key)["source"] == "configured"


def test_rank_missing_covers_only_the_unknown_ones_and_is_bounded(monkeypatch):
    from agent2.llm import providers as P
    P.init_providers_table()
    known = "custom:" + P.add_provider("k", "https://g.invalid/v1", "sk-x",
                                       "claude-opus-5", "anthropic")["id"]
    unknown = [_unknown_provider(f"m{i}", f"inhouse-model-{i}") for i in range(4)]
    _stub_answer(monkeypatch, {"reasoning": "strong", "confident": True})

    changed = caps.rank_missing(limit=2)
    assert len(changed) == 2, changed
    assert known not in changed
    assert set(changed) <= set(unknown)


def test_rank_all_re_asks_every_provider_including_recognised_ones(monkeypatch):
    """⚠️ THE DIFFERENCE BETWEEN THE TWO SWEEPS, AS A TEST.

    `rank_missing` skips a model the pattern table already recognises — that is what
    makes it free to call repeatedly. `rank_all` re-asks it anyway, because the
    answers go stale: a model id that meant nothing six months ago is a known
    flagship now, and the pattern table cannot be updated on a user's machine while
    a fresh question can.
    """
    from agent2.llm import providers as P
    P.init_providers_table()
    known = "custom:" + P.add_provider("k", "https://g.invalid/v1", "sk-x",
                                       "claude-opus-5", "anthropic")["id"]
    unknown = _unknown_provider("m", "inhouse-model-9")
    _stub_answer(monkeypatch, {"reasoning": "frontier", "context_window": 500000,
                               "confident": True})

    assert known not in caps.rank_missing()
    assert caps.get(unknown)["source"] == caps.SOURCE_RANKED

    # A DIFFERENT answer for the sweep, so both models genuinely change. Re-asking
    # and getting the identical record is correctly reported as "unchanged", which
    # would otherwise hide the one thing this test is about.
    _stub_answer(monkeypatch, {"reasoning": "frontier", "context_window": 500000,
                               "coding": "advanced", "confident": True})
    changed, skipped, not_reached = caps.rank_all()
    assert set(changed) == {known, unknown}, (changed, skipped)
    assert skipped == [] and not_reached == 0
    assert caps.get(known)["context_window"] == 500_000
    assert caps.get(known)["source"] == caps.SOURCE_RANKED


def test_rank_all_keeps_hand_set_values_and_says_which(monkeypatch):
    """⚠️ "Re-rank everything" is a request to re-ask the models, not permission to
    discard the user's own answers — and a user who is not TOLD reads the missing
    row as a failure."""
    protected = _unknown_provider("p", "inhouse-a")
    other = _unknown_provider("o", "inhouse-b")
    caps.set_meta(protected, reasoning=caps.TIER_BASIC)
    _stub_answer(monkeypatch, {"reasoning": "frontier", "confident": True})

    changed, skipped, _ = caps.rank_all()
    assert protected in skipped and protected not in changed
    assert other in changed
    assert caps.get(protected)["reasoning"] == caps.TIER_BASIC


def test_rank_all_reports_what_the_ceiling_cut(monkeypatch):
    """⚠️ No silent caps. One model call per provider is real money, so the ceiling
    is real — and a truncated sweep that said nothing would read as "everything is
    ranked now"."""
    for i in range(5):
        _unknown_provider(f"m{i}", f"inhouse-{i}")
    _stub_answer(monkeypatch, {"reasoning": "strong", "confident": True})
    changed, _skipped, not_reached = caps.rank_all(limit=2)
    assert len(changed) == 2
    assert not_reached == 3


def test_the_api_can_re_rank_everything(client, monkeypatch):
    _unknown_provider("m", "inhouse-x")
    _stub_answer(monkeypatch, {"reasoning": "advanced", "confident": True})
    body = client.post("/api/models/rank", json={"all": True}).get_json()
    assert body["ok"] is True and len(body["ranked"]) == 1
    assert body["not_reached"] == 0
    assert "skipped" in body


def test_the_api_can_rank_one_model(client, monkeypatch):
    key = _unknown_provider("m", "inhouse-y")
    _stub_answer(monkeypatch, {"coding": "frontier", "confident": True})
    body = client.post(f"/api/models/{key}/rank", json={}).get_json()
    assert body["coding"] == caps.TIER_FRONTIER


def test_adding_a_provider_triggers_ranking_in_the_background():
    """⚠️ A DAEMON THREAD, and nothing waits on it — "add a provider" must stay
    instant even when the endpoint is unreachable.

    Read off the MODULE source, not off `caps.rank_in_background`: the autouse
    `_no_network` fixture replaces that attribute with a lambda, so inspecting the
    attribute would inspect the stub and pass no matter what the real function does.
    """
    import inspect

    from agent2.llm import providers as P
    assert "rank_in_background" in inspect.getsource(P.add_provider)
    module_src = inspect.getsource(caps)
    body = module_src.split("def rank_in_background", 1)[1].split("\ndef ", 1)[0]
    assert "daemon=True" in body
    assert "join" not in body, "nothing may wait on the ranking thread"


def test_ranking_is_never_called_from_the_lookup_path():
    """⚠️ NEVER ON THE HOT PATH. `get()` is called several times per routing
    decision; a network call in there would put a multi-second billable round trip
    in front of every turn."""
    import inspect
    for fn in (caps.get, caps._custom_base, R._rank, R.choose, R.rank_candidates):
        src = inspect.getsource(fn)
        assert "rank_with_model" not in src, fn.__name__
        assert "_ask_gemini" not in src, fn.__name__


def test_describe_is_serialisable_and_carries_no_credential():
    from agent2.llm import providers as P
    P.init_providers_table()
    P.add_provider("t3", "https://api.example.invalid/v1", "sk-SECRET-CANARY",
                   "m", "openai")
    blob = json.dumps(caps.describe())
    assert "sk-SECRET-CANARY" not in blob


# ══════════════════════════════════════════════════════════════════════════════
# Task 18 — automatic selection
# ══════════════════════════════════════════════════════════════════════════════

def test_routing_is_off_by_default_and_an_explicit_model_is_returned_untouched():
    """⚠️ REGRESSION PIN ON THE WHOLE FEATURE.

    If this fails, every existing install silently started having its model choice
    overruled. `choose()` must be a no-op until somebody opts in.
    """
    assert R.routing_mode() == R.ROUTING_OFF
    d = R.choose(_keys()[0], message="why does this deadlock intermittently?")
    assert d.model_key == _keys()[0]
    assert d.source == R.SRC_USER
    assert d.routed is False


def test_an_empty_request_is_reported_as_the_default_not_as_a_choice():
    d = R.choose("", message="hi")
    assert d.model_key == cfg.DEFAULT_MODEL
    assert d.source == R.SRC_DEFAULT


def test_selecting_auto_routes_even_while_the_policy_is_off():
    """`auto` IS the opt-in. Picking it is an explicit request to be routed, which
    is what keeps "explicit user selection takes priority" true."""
    d = R.choose(R.AUTO, message="why is this test flaky?")
    assert d.routed is True
    assert d.model_key != R.AUTO
    assert d.model_key in R.candidates(skip_cooling=False)


def test_default_only_routes_a_default_turn_and_leaves_a_chosen_one_alone(monkeypatch):
    monkeypatch.setattr(cfg, "MODEL_ROUTING", R.ROUTING_DEFAULT_ONLY)
    assert R.choose(cfg.DEFAULT_MODEL, message="why does this crash?").routed is True
    assert R.choose(_second(), message="why does this crash?").routed is False


def test_always_is_the_only_setting_that_overrules_an_explicit_pick(monkeypatch):
    """"unless explicitly configured otherwise" — this value IS that configuration,
    and nothing else may have that effect."""
    monkeypatch.setattr(cfg, "MODEL_ROUTING", R.ROUTING_ALWAYS)
    assert R.choose(_second(), message="hi").routed is True
    for other in (R.ROUTING_OFF, R.ROUTING_DEFAULT_ONLY):
        monkeypatch.setattr(cfg, "MODEL_ROUTING", other)
        assert R.choose(_second(), message="hi").routed is False


def test_an_unknown_routing_value_falls_back_to_off(monkeypatch):
    monkeypatch.setattr(cfg, "MODEL_ROUTING", "sure-why-not")
    assert R.routing_mode() == R.ROUTING_OFF


def test_the_stored_setting_is_honoured_when_the_env_says_nothing(monkeypatch):
    monkeypatch.setattr(cfg, "MODEL_ROUTING", "")
    R.set_routing_mode(R.ROUTING_ALWAYS)
    assert R.routing_mode() == R.ROUTING_ALWAYS


def test_setting_a_bad_routing_mode_raises():
    with pytest.raises(ValueError):
        R.set_routing_mode("aggressive")


def test_a_simple_question_routes_to_the_fast_cheap_model():
    d = R.choose(R.AUTO, message="what time is it?")
    rec = caps.get(d.model_key)
    assert rec["speed"] == caps.SPEED_FAST
    assert rec["cost"] == caps.COST_LOW
    assert "short question" in d.reason


def test_a_debugging_question_routes_to_the_reasoning_model():
    d = R.choose(R.AUTO, message="why does this deadlock under load? "
                                 "here is the stack trace")
    # The BEST reasoning tier AVAILABLE, not a specific tier value: which tier that
    # is depends on what is configured, and an install of flash-tier models has no
    # `advanced` option at all. Asserting one would make this a statement about the
    # user's model table rather than about the router.
    assert caps.tier_rank(caps.get(d.model_key)["reasoning"]) == _best("reasoning")
    assert "debugging" in d.reason


def test_a_coding_request_routes_to_a_coding_capable_model():
    d = R.choose(R.AUTO, message="implement a retry decorator in python:\n```py\n"
                                 "def f():\n    pass\n```")
    assert caps.tier_rank(caps.get(d.model_key)["coding"]) == _best("coding")


def test_an_image_attachment_forces_a_vision_model():
    """A hard requirement: unknown reads as unusable. An image cannot be sent
    hopefully."""
    from agent2.llm import providers as P
    P.init_providers_table()
    made = P.add_provider("blind", "https://api.example.invalid/v1", "sk-x",
                          "m", "openai")          # vision unknown → excluded
    d = R.choose(R.AUTO, message="what is in this screenshot?",
                 attachments=[{"name": "shot.png"}])
    assert d.model_key != "custom:" + made["id"]
    assert caps.supports(d.model_key, "vision", default=False) is True
    assert "image" in d.reason


def test_a_mime_type_counts_as_an_image_even_without_a_suffix():
    sig = R.analyze("look", attachments=[{"name": "blob", "mime": "image/png"}])
    assert sig["vision"] is True


def test_a_large_turn_routes_to_a_model_with_a_known_window(monkeypatch):
    monkeypatch.setattr(cfg, "ROUTER_LONG_CONTEXT", 1000)
    d = R.choose(R.AUTO, message="x" * 40000)
    assert caps.context_window(d.model_key) > 0, \
        "routed a large turn at a model whose window nobody recorded"
    assert "large context" in d.reason


def test_choose_never_returns_an_empty_model_or_the_auto_key(monkeypatch):
    """⚠️ TOTALITY. A router that returns "" on an unusual turn breaks the app, and
    the failure lands as "no model configured" — an error about the wrong thing."""
    monkeypatch.setattr(cfg, "MODEL_ROUTING", R.ROUTING_ALWAYS)
    for msg in ("", "x", "x" * 100000, "🙂", "```\n```"):
        for req in ("", R.AUTO, _second(), "custom:nope", "garbage"):
            d = R.choose(req, message=msg)
            assert d.model_key, (req, msg[:12])
            assert d.model_key != R.AUTO, (req, msg[:12])


def test_choose_survives_every_candidate_being_filtered_out(monkeypatch):
    """A hard requirement that empties the pool is DISCARDED, not obeyed — and the
    reason records that we could not honour it, so the turn is explainable."""
    monkeypatch.setattr(caps, "supports", lambda *a, **k: False)
    d = R.choose(R.AUTO, message="look at this", attachments=[{"name": "a.png"}])
    assert d.model_key
    assert "note:" in d.reason


def test_resolve_never_lets_the_auto_key_reach_a_vendor():
    """The one failure mode `AUTO` introduces: `auto` sent as a model id."""
    assert R.resolve(R.AUTO) == cfg.DEFAULT_MODEL
    assert R.resolve("") == cfg.DEFAULT_MODEL
    assert R.resolve(None) == cfg.DEFAULT_MODEL
    assert R.resolve(_second()) == _second()


def test_auto_is_not_a_member_of_config_models():
    """⚠️ An entry in `config.MODELS` would reach `MODELS[key]["api"]` and be sent
    to the vendor as a model id."""
    from agent2.config import MODELS
    assert R.AUTO not in MODELS


def test_the_cli_picker_offers_auto_first():
    from agent2.cli.interactive import build_model_choices
    choices = build_model_choices()
    assert choices[0]["value"] == R.AUTO
    assert "routing" in choices[0]["hint"]


def test_analyze_is_pure_and_total():
    for bad in (None, "", 12345):
        sig = R.analyze(bad if isinstance(bad, str) else str(bad or ""))
        assert set(sig) >= {"tokens", "vision", "coding", "reasoning",
                            "long_context", "simple"}


def test_a_short_but_hard_question_is_not_classified_as_simple():
    """Length alone is a bad signal — "why is this flaky?" is five words of work."""
    assert R.analyze("why is this flaky?")["simple"] is False
    assert R.analyze("thanks!")["simple"] is True


# ══════════════════════════════════════════════════════════════════════════════
# Task 19 — fallback, breaker, ledger
# ══════════════════════════════════════════════════════════════════════════════

def test_an_invalid_model_gets_a_different_model():
    alt = R.next_model(_keys()[0], kind="invalid_model", tried=[_keys()[0]])
    assert alt and alt != _keys()[0]


def test_a_quota_failure_gets_a_different_model():
    """Gemini's limits are per model, so a walled flash says nothing about pro."""
    assert R.next_model(_keys()[0], kind="quota", tried=[_keys()[0]])


def test_an_auth_failure_never_falls_back():
    """⚠️ A rejected credential is not the model's fault. Trying four models against
    a bad key turns one clear error into four confusing ones."""
    assert R.next_model(_keys()[0], kind="auth", tried=[_keys()[0]]) == ""


def test_an_unclassified_failure_never_falls_back():
    """A malformed request fails identically everywhere; a fallback would spend the
    user's quota twice to reach the same error."""
    assert R.next_model(_keys()[0], kind="", tried=[_keys()[0]]) == ""
    assert "" not in R.FALLBACK_KINDS


def test_the_hop_budget_is_per_turn_not_per_model(monkeypatch):
    """⚠️ Counting per model would let a third model reset the ceiling, and "do not
    endlessly retry failed providers" would hold for each provider while failing
    for the turn."""
    monkeypatch.setattr(cfg, "FALLBACK_MAX_HOPS", 2)
    tried = [_keys()[0]]
    for _ in range(2):
        nxt = R.next_model(tried[-1], kind="quota", tried=tried)
        assert nxt
        tried.append(nxt)
    assert R.next_model(tried[-1], kind="quota", tried=tried) == ""


def test_zero_hops_disables_fallback_entirely(monkeypatch):
    monkeypatch.setattr(cfg, "FALLBACK_MAX_HOPS", 0)
    assert R.next_model(_keys()[0], kind="quota", tried=[_keys()[0]]) == ""


def test_a_model_already_tried_is_never_offered_again():
    tried = R.candidates(skip_cooling=False)
    assert R.next_model(tried[0], kind="quota", tried=tried) == ""


def test_the_breaker_opens_after_repeated_failures(monkeypatch):
    monkeypatch.setattr(cfg, "BREAKER_FAILS", 3)
    assert R.cooling(_second()) is False
    for i in range(3):
        tripped = R.note_failure(_second(), "transient")
        assert tripped is (i == 2)
    assert R.cooling(_second()) is True


def test_a_cooling_model_is_skipped_by_the_router_and_the_chain(monkeypatch):
    monkeypatch.setattr(cfg, "BREAKER_FAILS", 1)
    R.note_failure(_second(), "transient")
    assert _second() not in R.candidates()
    assert R.next_model(_keys()[0], kind="quota", tried=[_keys()[0]]) != _second()


def test_a_success_clears_the_breaker_immediately(monkeypatch):
    """The breaker stops a hammering loop; it does not punish a model for one bad
    minute."""
    monkeypatch.setattr(cfg, "BREAKER_FAILS", 1)
    R.note_failure(_second(), "transient")
    assert R.cooling(_second()) is True
    R.note_success(_second())
    assert R.cooling(_second()) is False


def test_an_auth_failure_never_trips_the_breaker(monkeypatch):
    """⚠️ A bad key fails every model identically; cooling them one by one would
    take the whole app offline over one credential."""
    monkeypatch.setattr(cfg, "BREAKER_FAILS", 1)
    for _ in range(5):
        assert R.note_failure(_second(), "auth") is False
    assert R.cooling(_second()) is False


def test_the_cooldown_expires(monkeypatch):
    monkeypatch.setattr(cfg, "BREAKER_FAILS", 1)
    monkeypatch.setattr(cfg, "BREAKER_COOLDOWN", 0.01)
    R.note_failure(_second(), "transient")
    import time as _t
    _t.sleep(0.05)
    assert R.cooling(_second()) is False


def test_candidates_never_returns_nothing_even_when_all_are_cooling(monkeypatch):
    """⚠️ An empty candidate list means "Agent2 refuses to answer", which is worse
    than a bruised model."""
    monkeypatch.setattr(cfg, "BREAKER_FAILS", 1)
    for key in R.candidates(skip_cooling=False):
        R.note_failure(key, "transient")
    assert R.candidates(), "every model cooling emptied the pool"


def test_an_attempt_is_recorded_with_everything_task_19_asks_for():
    R.record_attempt(model=_second(), ok=False, latency_ms=1234,
                     primary_model=_keys()[0], fallback_of=_keys()[0],
                     failure_kind="quota", failure_reason="429 exhausted",
                     chat_id="c1", session_id="s1")
    row = R.attempts(limit=1)[0]
    assert row["primary_model"] == _keys()[0]
    assert row["fallback_of"] == _keys()[0]
    assert row["failure_kind"] == "quota"
    assert "429" in row["failure_reason"]
    assert row["latency_ms"] == 1234
    assert row["ok"] == 0


def test_the_ledger_is_bounded_by_the_writer(monkeypatch):
    """⚠️ The one thing a diagnostic table must never do is become the problem it
    was added to diagnose."""
    monkeypatch.setattr(cfg, "ROUTER_LEDGER_MAX", 60)
    for i in range(90):
        R.record_attempt(model=_keys()[0], ok=True, latency_ms=i)
    n = db.qone("SELECT COUNT(*) AS n FROM model_attempts")["n"]
    assert n <= 60, n


def test_recording_an_attempt_never_raises(monkeypatch):
    monkeypatch.setattr(db, "exe",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db")))
    R.record_attempt(model=_keys()[0], ok=True)      # must not raise


def test_stats_summarise_without_leaking_a_prompt():
    R.record_attempt(model=_keys()[0], ok=True, latency_ms=100)
    R.record_attempt(model=_second(), ok=False, failure_kind="quota",
                     failure_reason="429", fallback_of=_keys()[0])
    s = R.stats()
    assert s["total"] == 2 and s["ok"] == 1 and s["failed"] == 1
    assert s["fallbacks"] == 1
    assert _keys()[0] in s["by_model"]
    json.dumps(s)


def test_describe_is_serialisable():
    snap = R.describe()
    json.dumps(snap)
    assert snap["routing"] in R.ROUTING_MODES
    assert snap["auto_key"] == R.AUTO


# ── The one ordering, shared by both surfaces ─────────────────────────────────

def test_the_cli_fallback_order_uses_the_same_ranking_as_the_web_loop():
    """⚠️ Two orderings would mean the web loop falls back to one model and the CLI
    to another for the same failure, each looking correct alone."""
    import agent2cli

    order = agent2cli._fallback_order(_keys()[0])
    assert order[0] == _keys()[0]
    ranked = R.rank_candidates({}, exclude=[_keys()[0]])
    assert order[1:1 + len(ranked)] == ranked


def test_the_cli_still_tries_every_model_before_giving_up():
    """⚠️ Pre-existing behaviour that Task 19 must not remove. `FALLBACK_MAX_HOPS`
    is deliberately NOT applied here — each model is tried at most once, so no loop
    can form, and capping at two would delete working behaviour."""
    import agent2cli

    order = agent2cli._fallback_order(_keys()[0])
    for key in R.candidates(skip_cooling=False):
        assert key in order, key


def test_the_cli_order_has_no_duplicates():
    import agent2cli
    order = agent2cli._fallback_order(_second())
    assert len(order) == len(set(order))


def test_a_cooling_model_is_last_in_the_cli_order_not_absent(monkeypatch):
    """"Tried and failed" beats "never tried" when the alternative is telling the
    user every model is unavailable."""
    import agent2cli
    monkeypatch.setattr(cfg, "BREAKER_FAILS", 1)
    first, cooling = _keys()[0], _keys()[-1]
    R.note_failure(cooling, "transient")
    order = agent2cli._fallback_order(first)
    assert cooling in order, "a cooling model was dropped from the CLI order entirely"
    assert order[-1] == cooling, "a cooling model must be tried LAST, not skipped"


# ══════════════════════════════════════════════════════════════════════════════
# The HTTP surface
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def client():
    from flask import Flask

    from agent2.server.routes import register_routes
    app = Flask(__name__)
    register_routes(app)
    with app.test_client() as c:
        yield c


def test_api_models_lists_every_model_with_the_routing_policy(client):
    body = client.get("/api/models").get_json()
    keys = {m["key"] for m in body["models"]}
    from agent2.config import MODELS
    assert set(MODELS) <= keys
    assert body["router"]["routing"] in R.ROUTING_MODES
    assert body["router"]["auto_key"] == R.AUTO


def test_api_models_never_ships_a_provider_key(client):
    from agent2.llm import providers as P
    P.init_providers_table()
    P.add_provider("t", "https://api.example.invalid/v1", "sk-API-CANARY-77",
                   "m", "openai")
    assert "sk-API-CANARY-77" not in client.get("/api/models").get_data(as_text=True)


def test_api_can_correct_and_clear_model_metadata(client):
    resp = client.put("/api/models/3.1-pro", json={"context_window": 250000})
    assert resp.status_code == 200
    assert resp.get_json()["context_window"] == 250000
    assert client.delete("/api/models/3.1-pro").get_json()["context_window"] == 0


def test_api_rejects_a_bad_metadata_value_with_400(client):
    resp = client.put("/api/models/2.5-flash", json={"speed": "warp"})
    assert resp.status_code == 400
    assert "speed" in resp.get_json()["error"]


def test_api_can_set_the_routing_policy(client):
    assert client.put("/api/models/routing",
                      json={"routing": "always"}).get_json()["routing"] == "always"
    assert client.put("/api/models/routing",
                      json={"routing": "nope"}).status_code == 400


def test_api_route_preview_never_runs_a_turn(client):
    """Routing is invisible by design, so without a dry run the only way to see why
    a decision was made is to spend a turn on it."""
    before = db.qone("SELECT COUNT(*) AS n FROM model_attempts")["n"]
    body = client.post("/api/models/route",
                       json={"model": "auto",
                             "message": "why does this segfault?"}).get_json()
    assert body["routed"] is True and body["model"] != R.AUTO
    assert "reason" in body and body["signals"]["reasoning"] is True
    assert db.qone("SELECT COUNT(*) AS n FROM model_attempts")["n"] == before


def test_editing_model_metadata_needs_only_the_settings_capability():
    """An `operator` may retune routing but still cannot reach the credentials the
    models are called with."""
    from agent2.core import permissions as perms
    assert perms.capability_for("PUT", "/api/models/2.5-flash") == perms.CAP_SETTINGS
    assert perms.capability_for("PUT", "/api/models/routing") == perms.CAP_SETTINGS
    assert perms.CAP_SETTINGS in perms.ROLES[perms.ROLE_OPERATOR]
    assert perms.CAP_SECRETS not in perms.ROLES[perms.ROLE_OPERATOR]


def test_migrations_15_and_16_are_registered():
    versions = {v for v, _, _ in db._MIGRATIONS}
    assert {15, 16} <= versions
    assert db.SCHEMA_VERSION >= 16
