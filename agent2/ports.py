"""Back-compat shim: this module moved to `agent2.server.ports`.

Re-exported so existing imports (and any user scripts) keep working. Prefer the
new path in new code. Module-level singletons live in the real module, so
`agent2.ports.X` and `agent2.server.ports.X` are the SAME object.
"""

from agent2.server.ports import *          # noqa: F401,F403

# Underscored names `import *` skips, kept for callers that
# reference them directly (tests, sibling modules).
from agent2.server.ports import (  # noqa: F401
    _candidates,
)
