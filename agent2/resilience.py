"""Back-compat shim: this module moved to `agent2.llm.resilience`.

Re-exported so existing imports (and any user scripts) keep working. Prefer the
new path in new code. Module-level singletons live in the real module, so
`agent2.resilience.X` and `agent2.llm.resilience.X` are the SAME object.
"""

from agent2.llm.resilience import *          # noqa: F401,F403

# Underscored names `import *` skips, kept for callers that
# reference them directly (tests, sibling modules).
from agent2.llm.resilience import (  # noqa: F401
    _AUTH_MARKERS,
    _BLANK_REPLIES,
    _INVALID_MODEL_MARKERS,
    _QUOTA_MARKERS,
    _TRANSIENT_MARKERS,
)
