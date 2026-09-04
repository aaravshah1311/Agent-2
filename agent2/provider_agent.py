"""Back-compat shim: this module moved to `agent2.llm.provider_agent`.

Re-exported so existing imports (and any user scripts) keep working. Prefer the
new path in new code. Module-level singletons live in the real module, so
`agent2.provider_agent.X` and `agent2.llm.provider_agent.X` are the SAME object.
"""

from agent2.llm.provider_agent import *          # noqa: F401,F403

# Underscored names `import *` skips, kept for callers that
# reference them directly (tests, sibling modules).
from agent2.llm.provider_agent import (  # noqa: F401
    _exec_tool,
    _retry_text_only,
    _tool_desc,
    _tool_meta,
)
