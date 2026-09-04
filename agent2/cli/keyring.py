# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/cli/keyring.py
─────────────────────
The CLI's binding to THE shared key rotator (`agent2/llm/keys.py`).

⚠️ DO NOT REINTRODUCE A CLI-LOCAL KeyRotator.
`agent2cli.py` carried its own ~90-line copy over the same `api_keys` table, and
the three ways it silently diverged are exactly why:

  1. it never rotated — `get()` returned `active[0]` on every call, so key #1 was
     burned to quota while every other key sat idle. The shared one is
     round-robin over the active keys.
  2. it never recorded usage, so `/keys` and the Web key panel reported different
     numbers for the same keys.
  3. its pin went to a `cli_pinned_key` setting nothing else read, so a pin never
     crossed to the browser — and dual mode runs both surfaces at once.

It also built a fresh `genai.Client` — a fresh HTTP pool and TLS handshake — on
every single `get()`; the shared rotator caches one client per key. Pinned by
`test_cli_uses_the_shared_rotator`, which asserts *identity*.
"""

from typing import ClassVar


# ⚠️ FAILSAFE: `rotator` must stay CALLABLE, never None.
#
# There are ten `rotator.<method>()` call sites (get / fail / status / pin /
# reload / add / remove / reset_key / record_usage / flush_usage). Leaving None
# on a failed import would turn a broken install from "the CLI starts and says it
# has no keys" into an AttributeError from inside the agent loop, and guarding
# all ten with `if _ROTATOR_OK` is a line someone forgets.
#
# This null object answers every one of them with the same "no keys" shape
# `get()` already returns when google-genai is missing, so the no-key path the
# CLI ALREADY handles is what runs.
#
# Defined at MODULE level, not inside the `except` — a failsafe that only exists
# on a broken machine is a failsafe nobody has ever run. The `except` branch is
# unreachable on a healthy install, so it is pinned by an AST assertion on the
# handler itself; an object-level assertion inspects the successfully-imported
# singleton and cannot see the regression (proved by sabotage).
class _NullRotator:
    # ClassVar, and deliberately never mutated: the null object's whole contract
    # is "no keys". A per-instance list would suggest something could fill it.
    entries: ClassVar[list[dict]] = []

    def get(self):            return None, None, None
    def status(self):         return []
    def reload(self):         pass
    def fail(self, *a, **k):  pass
    def pin(self, *a, **k):   pass
    def record_usage(self, *a, **k): pass
    def flush_usage(self):    pass
    def add(self, *a, **k):   return False, "database unavailable"
    def remove(self, *a, **k): pass
    def reset_key(self, *a, **k): pass


try:
    from agent2.llm.keys import KeyRotator, rotator as _rotator
    _ROTATOR_OK = True
    _ROTATOR_IMPORT_ERROR = None
except Exception as _rex:
    # agent2/llm/keys.py has its own never-raises fallback singleton, so this
    # only triggers if the module cannot be imported at all.
    KeyRotator = _NullRotator
    _rotator = _NullRotator()
    _ROTATOR_OK = False
    _ROTATOR_IMPORT_ERROR = _rex


def load_keys() -> list[dict]:
    """Keys currently known to the shared rotator, in its own order.

    Kept as a CLI-local helper because several call sites only ask "are there any
    keys, and how many" — but it READS the rotator instead of re-querying the DB,
    so the CLI can never disagree with the thing that actually picks a key.
    """
    try:
        return [{"key": e["key"], "label": e["label"],
                 "active": e["active"], "errs": e["errs"]}
                for e in _rotator.entries]
    except Exception:
        return []


def save_key_to_env(new_key: str) -> tuple[bool, str]:
    """Add a new key. Returns (success, label_or_reason).

    Routes through the rotator's `add()` so the in-memory entries are refreshed
    in the same call — a raw INSERT would leave this process picking from a key
    set that does not include the key the user just added.

    The name is legacy: there has been no .env since keys moved into agent2.db.
    """
    reason_map = {"too_short": "key too short", "already_exists": "already exists"}
    try:
        ok, info = _rotator.add(new_key)
    except Exception as ex:
        return False, str(ex)
    return (ok, info if ok else reason_map.get(info, info))
