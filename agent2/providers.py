"""Back-compat shim: this module moved to `agent2.llm.providers`.

Re-exported so existing imports (and any user scripts) keep working. Prefer the
new path in new code. Module-level singletons live in the real module, so
`agent2.providers.X` and `agent2.llm.providers.X` are the SAME object.
"""

from agent2.llm.providers import *          # noqa: F401,F403

# Underscored names `import *` skips, kept for callers that
# reference them directly (tests, sibling modules).
from agent2.llm.providers import (  # noqa: F401
    _anthropic_messages_url,
    _anthropic_tools,
    _burp_tool_schemas,
    _http_post,
    _notify,
    _openai_chat_url,
    _openai_tools,
)
