# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/resilience.py
────────────────────
Network resilience helpers shared by every agent loop (Gemini web, Gemini CLI,
and custom providers).

Why this exists
───────────────
Agent2's most common "crash" was a socket **read operation timed out** partway
through a long generation. Two bugs combined to make it fatal:

  1. The google-genai client was built with no HTTP timeout, so the SDK's short
     default read timeout fired during big edits / thinking-mode replies.
  2. The agent loops classified the resulting error as neither quota, auth, nor
     invalid-model, so it fell through to a generic handler that ENDED the turn
     and dropped the user back to the terminal — no retry.

This module centralises:
  - `build_http_options()` — a generous per-request timeout for the SDK client.
  - `make_client()`        — construct a genai.Client with that timeout applied.
  - `classify_error()`     — map an exception/string to a category, crucially
                             recognising *transient* (retryable) failures.
  - `call_with_retry()`    — run a callable with exponential-backoff retries on
                             transient failures, so a single timeout no longer
                             kills a multi-step task.
"""

from __future__ import annotations

import time
import random

from agent2.config import (
    HTTP_TIMEOUT, MAX_RETRIES, RETRY_BASE_DELAY, RETRY_MAX_DELAY,
)

try:
    from google import genai
    from google.genai import types as _gtypes
except Exception:  # google-genai not installed yet
    genai = None
    _gtypes = None


# ── Error classification ─────────────────────────────────────────────────────────
# Categories: "transient" (retry), "quota" (rotate key), "auth" (rotate key),
# "invalid_model" (fall back to another model), or "" (unknown → non-retryable).

# Substrings that indicate a temporary failure worth retrying. Read timeouts,
# server-side 5xx, "overloaded", and connection resets all belong here — they are
# NOT the user's fault and usually succeed on a second attempt.
_TRANSIENT_MARKERS = (
    "read operation timed out", "timed out", "timeout", "deadline exceeded",
    "503", "502", "500", "504", "unavailable", "overloaded",
    "connection reset", "connection aborted", "connection error",
    "broken pipe", "temporarily unavailable", "try again",
    "econnreset", "remotedisconnected", "incompleteread",
    "internal error", "internal server error", "service unavailable",
    "the model is overloaded",
)

_QUOTA_MARKERS = (
    "429", "quota", "exhausted", "resource_exhausted", "resource exhausted",
    "rate limit", "rate-limit", "ratelimit",
)

_AUTH_MARKERS = (
    "401", "403", "unauthenticated", "unauthorized", "unauthorized_client",
    "invalid api key", "api key not valid", "permission denied", "permission",
)

_INVALID_MODEL_MARKERS = (
    "not found", "does not exist", "no such model", "unsupported",
    "model_not_found", "invalid model", "is not supported",
)


def classify_error(exc) -> str:
    """Return one of: 'transient', 'quota', 'auth', 'invalid_model', or ''.

    Order matters: quota (429) also contains no transient marker, but an
    'overloaded'/503 must be treated as transient even though some providers
    phrase it near rate-limit language. We check transient first so a
    momentarily overloaded model is retried rather than burning a key rotation.
    """
    s = str(exc).lower()

    # Some SDK exceptions carry a numeric .code / .status_code — prefer that.
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    try:
        code = int(code) if code is not None else None
    except (TypeError, ValueError):
        code = None
    if code in (500, 502, 503, 504):
        return "transient"
    if code == 429:
        # 429 with an explicit "overloaded" is transient; otherwise it's quota.
        return "transient" if "overloaded" in s else "quota"
    if code in (401, 403):
        return "auth"

    if any(m in s for m in _TRANSIENT_MARKERS):
        return "transient"
    if any(m in s for m in _QUOTA_MARKERS):
        return "quota"
    if any(m in s for m in _AUTH_MARKERS):
        return "auth"
    if any(m in s for m in _INVALID_MODEL_MARKERS):
        return "invalid_model"
    return ""


def is_transient(exc) -> bool:
    return classify_error(exc) == "transient"


# ── Client construction with a real timeout ──────────────────────────────────────
def build_http_options():
    """Return google.genai HttpOptions carrying our extended request timeout.

    The SDK expects the timeout in MILLISECONDS. Returns None if the SDK isn't
    importable or doesn't support http_options (older versions) so callers can
    fall back to a plain client.
    """
    if _gtypes is None:
        return None
    try:
        return _gtypes.HttpOptions(timeout=HTTP_TIMEOUT * 1000)
    except Exception:
        return None


def make_client(key: str):
    """Construct a genai.Client for `key` with our extended timeout applied.

    Falls back to a plain client if http_options is unsupported, and returns
    None only if the SDK is missing or the key is unusable — matching the old
    behaviour so callers don't need to change their None-handling.
    """
    if genai is None or not key:
        return None
    opts = build_http_options()
    try:
        if opts is not None:
            return genai.Client(api_key=key, http_options=opts)
        return genai.Client(api_key=key)
    except TypeError:
        # Older SDK: http_options kwarg not accepted.
        try:
            return genai.Client(api_key=key)
        except Exception:
            return None
    except Exception:
        return None


# ── Retry with exponential backoff ────────────────────────────────────────────────
def backoff_delay(attempt: int) -> float:
    """Exponential backoff with jitter, capped at RETRY_MAX_DELAY.

    attempt is 0-indexed: 0 → ~base, 1 → ~2*base, 2 → ~4*base, …
    """
    raw = RETRY_BASE_DELAY * (2 ** attempt)
    raw = min(raw, RETRY_MAX_DELAY)
    # Full jitter so many keys/sessions don't retry in lockstep.
    return random.uniform(0, raw)


def call_with_retry(fn, *, on_retry=None, should_stop=None, max_retries: int | None = None):
    """Call `fn()` and retry on TRANSIENT failures with exponential backoff.

    - `fn`           : zero-arg callable performing the network request.
    - `on_retry`     : optional callback(attempt:int, delay:float, exc) invoked
                       before each backoff sleep (for UI/log messages).
    - `should_stop`  : optional zero-arg predicate; if it returns True during a
                       backoff, we abort the retries and re-raise the last error.
    - `max_retries`  : override the default retry count for this call.

    Non-transient errors (quota, auth, invalid_model, unknown) are raised
    immediately so the caller's existing key-rotation / fallback logic runs.
    Returns whatever `fn()` returns.
    """
    tries = MAX_RETRIES if max_retries is None else max_retries
    last_exc = None
    for attempt in range(tries + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — we re-raise below
            last_exc = exc
            if classify_error(exc) != "transient" or attempt >= tries:
                raise
            delay = backoff_delay(attempt)
            if on_retry:
                try:
                    on_retry(attempt + 1, delay, exc)
                except Exception:
                    pass
            # Sleep in small slices so a stop request is honoured promptly.
            waited = 0.0
            while waited < delay:
                if should_stop and should_stop():
                    raise
                step = min(0.25, delay - waited)
                time.sleep(step)
                waited += step
    # Unreachable, but keep type-checkers happy.
    if last_exc:
        raise last_exc


# ── Empty-reply recovery ──────────────────────────────────────────────────────────
# Every agent loop used to end a turn with `"\n".join(texts) or "Done."`. That
# fallback fires whenever the model returns no *text* part, which happens most
# often on short conversational turns ("hi", "thanks"): thinking-capable models
# can spend the whole output budget before emitting a visible token, and a
# finish_reason of MAX_TOKENS leaves `texts` empty. The user then sees a bare
# "Done." in reply to "hello", which reads like the agent is broken.
#
# `is_blank_reply()` detects that case (including the degenerate one-word replies
# the models sometimes do produce), and `blank_reply_notice()` supplies an honest
# message for when a no-tool retry still comes back empty. Callers should prefer
# retrying once with tools disabled — see `retry_text_only` usage in the loops.

# Replies that carry no information. Compared case-insensitively after stripping
# punctuation/whitespace, so "Done.", "done", "OK!" all match.
_BLANK_REPLIES = {
    "", "done", "ok", "okay", "sure", "continue", "yes", "no", "n/a",
    "understood", "acknowledged", "got it", "noted", "alright", "k",
}


def is_blank_reply(text: str | None) -> bool:
    """True when a model reply carries no usable content for the user.

    Catches empty/whitespace-only text as well as the bare filler tokens
    ("Done.", "ok", "sure") that are never a real answer to a real message.
    """
    if not text:
        return True
    cleaned = text.strip().strip(".!,;:—-–*_`\"' \t\n").lower()
    return cleaned in _BLANK_REPLIES


def blank_reply_notice(finish_reason=None) -> str:
    """User-facing text for a turn that produced no content even after a retry.

    Honest about what happened instead of pretending work was completed. When the
    model hit its output cap we say so, because switching mode/model is the fix.
    """
    fr = str(finish_reason or "").upper()
    if "MAX_TOKEN" in fr:
        return (
            "I ran out of output budget before writing a reply. Try **Fast** or "
            "**Pro** mode for short messages, or ask again with a bit more detail."
        )
    return (
        "I didn't produce a reply that time — that's on me, not you. "
        "Ask again and I'll pick it up."
    )
