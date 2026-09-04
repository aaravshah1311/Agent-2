"""Back-compat shim: this module moved to `agent2.server.routes`.

Re-exported so existing imports (and any user scripts) keep working. Prefer the
new path in new code. Module-level singletons live in the real module, so
`agent2.routes.X` and `agent2.server.routes.X` are the SAME object.
"""

from agent2.server.routes import *          # noqa: F401,F403
