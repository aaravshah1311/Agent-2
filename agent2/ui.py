"""Back-compat shim: this module moved to `agent2.server.ui`.

Re-exported so existing imports (and any user scripts) keep working. Prefer the
new path in new code. Module-level singletons live in the real module, so
`agent2.ui.X` and `agent2.server.ui.X` are the SAME object.
"""

from agent2.server.ui import *          # noqa: F401,F403
