"""Back-compat shim: this module moved to `agent2.llm.keys`.

Re-exported so existing imports (and any user scripts) keep working. Prefer the
new path in new code. Module-level singletons live in the real module, so
`agent2.keys.X` and `agent2.llm.keys.X` are the SAME object.
"""

from agent2.llm.keys import *          # noqa: F401,F403

# Underscored names `import *` skips, kept for callers that
# reference them directly (tests, sibling modules).
from agent2.llm.keys import (  # noqa: F401
    _on_keys_changed,
)
