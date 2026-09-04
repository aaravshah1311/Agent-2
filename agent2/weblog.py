"""Back-compat shim: this module moved to `agent2.server.weblog`.

Re-exported so existing imports (and any user scripts) keep working. Prefer the
new path in new code. Module-level singletons live in the real module, so
`agent2.weblog.X` and `agent2.server.weblog.X` are the SAME object.
"""

from agent2.server.weblog import *          # noqa: F401,F403

# Underscored names `import *` skips, kept for callers that
# reference them directly (tests, sibling modules).
from agent2.server.weblog import (  # noqa: F401
    _COLOR_ENV,
    _CONFIGURED,
    _LEVELS,
    _LOG,
    _NOISY_PREFIXES,
    _TAGS,
    _WerkzeugFilter,
    _bold,
    _c,
    _clients,
    _cyan,
    _dim,
    _green,
    _grey,
    _install_request_logging,
    _is_noise,
    _mag,
    _make_handler,
    _red,
    _status_paint,
    _yellow,
)
