# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2.core.pil.improve
─────────────────────────
Module 3 — Prompt Improvement (offline, opt-in).

Runs ONLY when the user enables it in settings. Its job is NOT to rewrite the
user's request into something different — it enriches the prompt while strictly
preserving the original intent, using ONLY preferences the user has actually,
repeatedly demonstrated (stored in the shared centralized memory's pil_prefs).

    Original:  Create a login page
    Improved:  Create a login page
               [Apply my usual preferences: React, Tailwind CSS; dark mode,
                responsive design, accessibility support. Keep the response
                concise.]

Key guarantees:
  • Never hallucinates new requirements — every added preference came from the
    user's own proven history via the learning engine.
  • Never removes or contradicts the original text; enrichment is appended as an
    explicit, clearly-labelled guidance block the model can honour.
  • Does nothing (returns the input unchanged) when there are no strong-enough
    preferences, or when the prompt clearly isn't a build/creation request.

Because personalization comes from updating the centralized memory rather than
retraining any model, this improves automatically as the user works.
"""

import re

from agent2.core.pil import memory as mem

# Weight below which a preference isn't trusted enough to inject. The learning
# engine raises weight only from proven, repeated use, so this gates out one-off
# mentions and prevents hallucinated requirements.
MIN_PREF_WEIGHT = 0.62

# Only enrich prompts that look like build/creation/authoring requests — the
# kind where stack + style preferences are relevant. A question or a shell
# command is left completely alone.
_BUILD_INTENT = re.compile(
    r"\b(create|build|make|generate|implement|write|add|design|scaffold|"
    r"develop|set\s?up|refactor)\b", re.IGNORECASE)

# Categories injected as an enrichment block, in render order.
_STACK_CATS = ("framework", "library", "language")
_STYLE_CATS = ("style",)


def improve(text: str, *, project: str = "") -> tuple[str, list[str]]:
    """Enrich *text* with proven user preferences. Returns ``(text, added)``
    where *added* lists the preference values that were appended (empty when the
    prompt was left unchanged)."""
    if not text or not text.strip():
        return text, []

    # Respect intent: only enrich build/creation requests.
    if not _BUILD_INTENT.search(text):
        return text, []

    low = text.lower()
    added: list[str] = []

    # Gather proven preferences from the ONE centralized memory.
    stack: list[str] = []
    for cat in _STACK_CATS:
        for p in mem.top_prefs(cat, limit=3, min_weight=MIN_PREF_WEIGHT):
            val = p["value"]
            # Skip anything the user already named in this very prompt — no point
            # restating, and it avoids fighting an explicit override.
            if val.lower() in low or val in stack:
                continue
            stack.append(val)

    styles: list[str] = []
    for cat in _STYLE_CATS:
        for p in mem.top_prefs(cat, limit=4, min_weight=MIN_PREF_WEIGHT):
            val = p["value"]
            if val.lower() in low or val in styles:
                continue
            styles.append(val)

    length_prefs = mem.top_prefs("length", limit=1, min_weight=MIN_PREF_WEIGHT)

    # Nothing proven → leave the prompt exactly as written.
    if not (stack or styles or length_prefs):
        return text, []

    # Build a compact, explicit guidance block. It is additive and clearly framed
    # as *the user's usual preferences*, so intent is preserved and the model can
    # still defer to anything explicit in the original request.
    clauses: list[str] = []
    if stack:
        clauses.append("using " + ", ".join(stack))
        added += stack
    if styles:
        clauses.append("with " + ", ".join(styles))
        added += styles
    if length_prefs:
        lp = length_prefs[0]["value"]
        clauses.append(f"keep the response {lp}")
        added.append(lp)

    guidance = "; ".join(clauses)
    enriched = (
        f"{text.rstrip()}\n\n"
        f"[Preferences learned from my past prompts — apply where they fit the "
        f"request above, without changing what I actually asked for: {guidance}.]"
    )
    return enriched, added
