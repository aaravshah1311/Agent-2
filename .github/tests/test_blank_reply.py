# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
Tests for blank-reply recovery in agent2/resilience.py.

Run from the repo root:  python -m pytest .github/tests/test_blank_reply.py -v

Why this exists
───────────────
Every agent loop used to end a turn with ``"\\n".join(texts) or "Done."``. That
fallback fires whenever the model returns no *text* part — which is exactly what
a thinking-capable Gemini model does on a short conversational turn ("hi",
"thanks"), because it can spend the whole output budget thinking before emitting
a visible token. Users saw a bare "Done." in reply to "hello".

Coverage
  - is_blank_reply     : empty/whitespace, the filler set, punctuation and
                         markdown-wrapped variants, and real replies it must NOT
                         swallow (including a legitimate sentence containing the
                         word "done").
  - blank_reply_notice : always non-empty, never itself blank, and mentions the
                         output cap when finish_reason is MAX_TOKENS.
  - system_prompt      : carries explicit small-talk / no-bare-"Done." guidance.
"""

import pytest

from agent2.llm.resilience import is_blank_reply, blank_reply_notice


# ── is_blank_reply ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    None, "", "   ", "\n\n", "\t",
    "Done.", "done", "DONE", " Done ", "Done!", "done...",
    "ok", "OK", "Ok.", "okay", "k",
    "sure", "continue", "yes", "no", "n/a",
    "understood", "acknowledged", "got it", "noted", "alright",
    "**Done.**", "`ok`", "_done_", "— done —",
])
def test_blank_replies_are_detected(text):
    """Empty text and bare filler tokens carry no information for the user."""
    assert is_blank_reply(text) is True


@pytest.mark.parametrize("text", [
    "Hey! I'm Agent2 — what are we building today?",
    "Done — I wrote app.py and the tests pass.",
    "ok, here's the plan:\n1. scan the repo",
    "Done.\n\nCreated 3 files.",
    "I'm doing well, thanks for asking!",
    "No, that file doesn't exist yet — want me to create it?",
    "continue reading at line 40 of agent.py",
])
def test_real_replies_are_not_blank(text):
    """A reply with actual content must survive, even if it starts with filler."""
    assert is_blank_reply(text) is False


def test_blank_check_is_not_substring_based():
    """Guard against a regression to `"done" in text` matching real sentences."""
    assert is_blank_reply("The download is done and verified.") is False


# ── blank_reply_notice ────────────────────────────────────────────────────────

def test_notice_is_never_itself_blank():
    """The recovery message must not trip the very check that produced it."""
    for fr in (None, "", "STOP", "MAX_TOKENS", "SAFETY"):
        notice = blank_reply_notice(fr)
        assert notice.strip()
        assert is_blank_reply(notice) is False


def test_notice_explains_a_token_cap():
    """MAX_TOKENS is actionable — the user should hear about mode/model."""
    notice = blank_reply_notice("MAX_TOKENS").lower()
    assert "budget" in notice or "output" in notice
    assert "mode" in notice


def test_notice_is_honest_when_cause_is_unknown():
    """No pretending work happened when we simply got nothing back."""
    notice = blank_reply_notice(None).lower()
    assert "done" not in notice


# ── System prompt guidance ────────────────────────────────────────────────────

def test_system_prompt_covers_small_talk():
    """The prompt must tell the model that greetings are not build tasks."""
    from agent2.agent import system_prompt
    sp = system_prompt().lower()
    assert "small talk" in sp
    assert "greeting" in sp
    # And explicitly forbid the exact failure the user reported.
    assert "never reply with a bare" in sp


# ── Recovery must not mask key-level failures ─────────────────────────────────
# `_retry_without_tools` used to `return ""` for ANY exception. Since
# call_with_retry deliberately re-raises quota/auth "so the caller's existing
# key-rotation / fallback logic runs", that swallow produced two real defects:
#   1. an exhausted key was reported to the user as "I didn't produce a reply
#      that time — that's on me, not you", hiding the actual cause; and
#   2. rotator.fail() never ran, so the dead key stayed in rotation and the very
#      next turn hit it again.
# It now returns (text, fatal) and the caller rotates on `fatal`.

def _stub_client(*side_effects):
    """A MagicMock Gemini client whose generate_content follows *side_effects*."""
    from unittest.mock import MagicMock
    c = MagicMock()
    if len(side_effects) == 1:
        eff = side_effects[0]
        if isinstance(eff, BaseException):
            c.models.generate_content.side_effect = eff
        else:
            c.models.generate_content.return_value = eff
    else:
        c.models.generate_content.side_effect = list(side_effects)
    return c


def _resp(text: str | None):
    """A stand-in Gemini response carrying one text part (or none)."""
    from unittest.mock import MagicMock
    parts = [MagicMock(text=text)] if text is not None else []
    return MagicMock(candidates=[MagicMock(content=MagicMock(parts=parts))])


@pytest.mark.parametrize("message,kind", [
    ("429 RESOURCE_EXHAUSTED: Quota exceeded for quota metric", "quota"),
    ("401 API key not valid. Please pass a valid API key.", "auth"),
])
def test_recovery_returns_key_errors_instead_of_swallowing(message, kind):
    """Quota/auth must reach the caller so it can rotate keys and say why."""
    import threading

    from agent2.agent import _retry_without_tools
    from agent2.llm.resilience import classify_error

    exc = RuntimeError(message)
    assert classify_error(exc) == kind, "test premise wrong: error misclassified"

    client = _stub_client(exc)
    text, fatal = _retry_without_tools(
        client, "gemini-2.5-flash", [], {}, threading.Event())

    assert text == ""
    assert fatal is not None, (
        f"{kind} error was swallowed — the user would be told the model went "
        "quiet, and rotator.fail() would never run"
    )
    assert classify_error(fatal) == kind


def test_recovery_does_not_waste_a_second_call_on_a_dead_key():
    """Tier 2 must be skipped for key-level errors — it would reuse the same key.

    Two tiers exist because some models reject `thinking_budget=0`. That is a
    per-request rejection; an exhausted key is not. Retrying it only spends
    another call and delays the real error.
    """
    import threading

    from agent2.agent import _retry_without_tools

    client = _stub_client(RuntimeError("429 RESOURCE_EXHAUSTED: Quota exceeded"))
    _retry_without_tools(client, "gemini-2.5-flash", [], {}, threading.Event())

    assert client.models.generate_content.call_count == 1, (
        "tier 2 ran against a key already known to be exhausted"
    )


def test_recovery_still_falls_through_to_tier_two_on_a_request_rejection():
    """The documented `thinking_budget=0` rejection must still be recovered."""
    import threading

    from agent2.agent import _retry_without_tools

    client = _stub_client(
        ValueError("400 INVALID_ARGUMENT: thinking_budget is not supported"),
        _resp("Hey! What are we building today?"),
    )
    text, fatal = _retry_without_tools(
        client, "gemini-2.5-flash", [], {}, threading.Event())

    assert fatal is None, "a request-level rejection is not a key failure"
    assert text == "Hey! What are we building today?"
    assert client.models.generate_content.call_count == 2


def test_a_genuinely_blank_reply_is_still_reported_as_blank():
    """The fix must not turn every empty recovery into a false API error."""
    import threading

    from agent2.agent import _retry_without_tools

    client = _stub_client(_resp(None))
    text, fatal = _retry_without_tools(
        client, "gemini-2.5-flash", [], {}, threading.Event())

    assert text == ""
    assert fatal is None, "an empty reply was misreported as a fatal API error"


def test_recovery_honours_a_stop_request():
    """A user pressing stop must not trigger an API error path."""
    import threading

    from agent2.agent import _retry_without_tools

    stop = threading.Event()
    stop.set()
    client = _stub_client(_resp("unused"))
    text, fatal = _retry_without_tools(client, "gemini-2.5-flash", [], {}, stop)

    assert (text, fatal) == ("", None)
    assert client.models.generate_content.call_count == 0
