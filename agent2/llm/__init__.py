"""
agent2.llm
───────────
Model access: talking to Gemini and to user-registered custom providers.

    keys           — KeyRotator: multi-key rotation, pinning, per-key usage stats
    resilience     — retry/backoff, error classification, blank-reply handling
    providers      — CRUD for custom provider records + wire-format translation
    provider_agent — the parallel agent loop for custom (OpenAI/Anthropic) endpoints

`provider_agent` mirrors `agent2.agent` exactly — same tools, same events — so the
two stay interchangeable from the caller's point of view.
"""
