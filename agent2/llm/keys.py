# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/keys.py
──────────────
KeyRotator: manages multiple Gemini API keys with:
  - auto-rotation on quota exhaustion (round-robin across active keys)
  - manual pinning (always use a specific key), persisted and shared by BOTH
    the CLI and the Web UI
  - per-key usage tracking (tokens + requests) persisted to SQLite
  - label / friendly-name support
  - thread-safe access

This is the ONE rotator. `agent2cli.py` used to carry its own ~90-line copy that
never round-robined (it always handed back the first active key, so key 1 burned
to quota while the rest idled), never recorded usage (so `/keys` and the Web key
panel reported different numbers for the same keys), and persisted its pin to a
setting nothing else read. Both surfaces now share this module's singleton.

Keys are stored ENTIRELY in agent2.db (table `api_keys`) — there is no .env.
Every DB access is wrapped so a locked/corrupt DB degrades gracefully instead
of crashing the agent.

THE OPERATIONAL INVARIANTS
──────────────────────────
  * `get()` ROUND-ROBINS. Always returning the first key burns key 1 to a 429
    while the rest sit at zero.
  * EXHAUSTING EVERY KEY REVIVES THEM ALL ONCE AND RETRIES, so a transient quota
    wall is not reported as a permanent "no keys configured".
  * ⚠️ THE PIN IS PERSISTED AND SHARED. `pin(label)` writes the `pinned_key`
    setting and notifies `api_keys`. `_load_pin()` falls back to the legacy
    `cli_pinned_key` so an existing CLI pin is ADOPTED rather than ignored.
    Persistence is best-effort: the in-memory pin is set FIRST and OUTSIDE the
    `try`, so an unwritable DB costs durability, never the pin.
  * PINNING REVIVES AN EXHAUSTED KEY — otherwise the pin silently does nothing.
  * `remove()` CLEARS A PIN TO THE REMOVED KEY *AND PERSISTS THAT*, or the next
    start restores a pin to a key that no longer exists.
  * `record_usage()` updates memory immediately and batches the write; a failed
    flush puts the deltas BACK.
  * THE PLACEHOLDER KEY (`your_gemini_api_key_here`) IS FILTERED IN `reload()`.
    This used to live only in the CLI's loader, which made one surface stricter
    than the other over one shared table.
  * ⚠️ `status()` REDACTS TO `key[:14] + "…"`. There is no auth on any route.

⚠️ FAILSAFE: the singleton falls back to a hand-initialised instance so key
loading cannot crash import, and `agent2cli.py` binds a module-level
`_NullRotator()` — never `None` — if this import fails. Otherwise ten call sites
raise AttributeError on a broken install instead of reporting "no keys". That
branch never runs on a healthy machine, so it is pinned by an AST assertion; an
object-level assertion cannot see the regression (proved by sabotage).
"""

import time
import threading

try:
    from google import genai
except Exception:  # google-genai not installed yet
    genai = None

from agent2.database import (
    qall, exemany,
    list_api_keys, add_api_key, remove_api_key, set_api_key_name,
)

# Same literal database.py uses when migrating a legacy .env. Kept here too so a
# key that reaches the table by another route is still never handed to the model.
_PLACEHOLDER_KEY = "your_gemini_api_key_here"


class KeyRotator:
    # Instance-level lock (see __init__). The old class-level lock was shared by
    # every KeyRotator ever constructed, so two rotators would needlessly block
    # each other; it also made the singleton's fallback path (which bypasses
    # __init__) silently lock-free.
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.entries: list[dict] = []   # {key, label, name, active, errs, tokens, requests, last_used}
        self._by_label: dict[str, dict] = {}   # label → entry (O(1) lookup)
        self._by_key: dict[str, dict] = {}     # raw key → entry (O(1) lookup)
        self._active_label: str | None = None  # pinned label (None = auto-rotate)
        # Cache one genai.Client per key. Building a client sets up an HTTP
        # connection pool; the old code built a brand-new one on EVERY get(),
        # which meant a fresh pool (and a fresh TLS handshake on first use) for
        # every single model call.
        self._clients: dict[str, object] = {}
        self._rr = 0                    # round-robin cursor over active keys
        # Usage deltas not yet flushed to SQLite: label → (tokens, requests).
        # Flushed in one batched write instead of one UPDATE per request.
        self._pending: dict[str, list[int]] = {}
        self.reload()
        self._load_pin()

    # ── Load keys from the database ────────────────────────────────────────────

    def reload(self) -> None:
        """Re-read keys from the DB, preserving live runtime state per label."""
        seen: set[str] = set()
        new_entries: list[dict] = []
        with self._lock:
            existing = dict(self._by_label)

        # One query for all usage rows instead of one per key (was N+1).
        try:
            usage = {r["key_label"]: r for r in qall("SELECT * FROM key_usage")}
        except Exception:
            usage = {}

        for rec in list_api_keys():
            v = (rec.get("api_key") or "").strip()
            label = str(rec.get("label") or "")
            # The placeholder check is defence-in-depth: database.py already drops
            # it when migrating a legacy .env, but nothing stops a user pasting it
            # into /addapi. It was previously filtered only by the CLI's own
            # loader, which made the CLI stricter than the Web UI over one table.
            if not v or v == _PLACEHOLDER_KEY or v in seen or len(v) < 10 or not label:
                continue
            ex = existing.get(label, {})
            row = usage.get(label)
            new_entries.append({
                "key":      v,
                "label":    label,
                "name":     rec.get("name") or ex.get("name", f"Key {label}"),
                "active":   ex.get("active", bool(rec.get("active", 1))),
                "errs":     ex.get("errs", 0),
                "tokens":   row["total_tokens"]   if row else 0,
                "requests": row["total_requests"] if row else 0,
                "last_used": row["last_used"]     if row else None,
            })
            seen.add(v)

        with self._lock:
            self.entries = new_entries
            self._by_label = {e["label"]: e for e in new_entries}
            self._by_key = {e["key"]: e for e in new_entries}
            # Drop cached clients for keys that no longer exist.
            for k in list(self._clients):
                if k not in self._by_key:
                    self._clients.pop(k, None)
            if self._rr >= max(1, len(new_entries)):
                self._rr = 0

    # ── Get a usable client ────────────────────────────────────────────────────

    def get(self) -> tuple["genai.Client | None", str | None, str | None]:
        """Return (client, raw_key, label) for the next active key.

        Auto-rotate spreads load round-robin across active keys rather than
        always handing back the first one, so a multi-key setup actually uses
        every key's quota instead of burning key 1 until it 429s.
        """
        if genai is None:
            return None, None, None
        with self._lock:
            # Pinned key has priority
            if self._active_label:
                e = self._by_label.get(self._active_label)
                if e and e["active"]:
                    return self._client(e["key"]), e["key"], e["label"]

            good = [e for e in self.entries if e["active"]]
            if not good:
                # All exhausted — reset and retry once
                for e in self.entries:
                    e["active"] = True
                    e["errs"]   = 0
                good = list(self.entries)

            if not good:
                return None, None, None

            e = good[self._rr % len(good)]
            self._rr = (self._rr + 1) % len(good)
            return self._client(e["key"]), e["key"], e["label"]

    def _client(self, key: str):
        """Return a cached client for *key*, building it on first use.

        Callers hold self._lock. Building the client with an extended HTTP
        timeout keeps long generations from tripping the SDK's short default
        read timeout (see resilience.py).
        """
        c = self._clients.get(key)
        if c is not None:
            return c
        try:
            from agent2.llm.resilience import make_client
            c = make_client(key)
        except Exception:
            try:
                c = genai.Client(api_key=key)
            except Exception:
                return None
        if c is not None:
            self._clients[key] = c
        return c

    # ── Mark failures ──────────────────────────────────────────────────────────

    def fail(self, key: str, quota: bool = False) -> None:
        with self._lock:
            e = self._by_key.get(key)
            if e is not None:
                e["errs"] += 1
                if quota or e["errs"] >= 3:
                    e["active"] = False

    # ── Record successful usage ────────────────────────────────────────────────

    def record_usage(self, label: str, tokens: int, flush: bool = True) -> None:
        """Attribute *tokens* and one request to *label*.

        In-memory counters update immediately (so /keys is always current). The
        SQLite write is accumulated per label and flushed via flush_usage(), so a
        burst of calls costs one write instead of one per call. Pass flush=False
        to defer explicitly.
        """
        with self._lock:
            e = self._by_label.get(label)
            if e is not None:
                e["tokens"]   += tokens
                e["requests"] += 1
                e["last_used"] = time.strftime("%Y-%m-%d %H:%M:%S")
            slot = self._pending.setdefault(label, [0, 0])
            slot[0] += tokens
            slot[1] += 1
        if flush:
            self.flush_usage()

    def flush_usage(self) -> None:
        """Persist accumulated usage deltas in a single batched transaction."""
        with self._lock:
            pending, self._pending = self._pending, {}
        if not pending:
            return
        rows = [(label, tok, reqs, tok, reqs)
                for label, (tok, reqs) in pending.items()]
        try:
            exemany(
                """INSERT INTO key_usage(key_label, total_tokens, total_requests, last_used)
                   VALUES(?, ?, ?, datetime('now'))
                   ON CONFLICT(key_label) DO UPDATE SET
                     total_tokens   = total_tokens + ?,
                     total_requests = total_requests + ?,
                     last_used      = datetime('now')""",
                rows,
            )
        except Exception:
            # Put the deltas back so they are retried on the next flush rather
            # than silently lost.
            with self._lock:
                for label, (tok, reqs) in pending.items():
                    slot = self._pending.setdefault(label, [0, 0])
                    slot[0] += tok
                    slot[1] += reqs

    # ── Manage keys ───────────────────────────────────────────────────────────

    def reset_key(self, label: str) -> None:
        with self._lock:
            e = self._by_label.get(label)
            if e is not None:
                e["active"] = True
                e["errs"]   = 0

    def set_name(self, label: str, name: str) -> None:
        set_api_key_name(label, name)
        self.reload()

    def pin(self, label: str | None) -> None:
        """Pin to a specific key, or None to re-enable auto-rotate.

        ⚠️ The pin is PERSISTED and shared by both surfaces. It used to be
        in-memory only here while `agent2cli.py` kept its own rotator that wrote
        a `cli_pinned_key` setting nothing else read — so pinning in the browser
        was forgotten on restart, and pinning in the CLI was invisible to the
        browser. One rotator, one setting, both surfaces.

        Persistence is best-effort by design: a DB that cannot be written must
        cost the *durability* of the pin, never the pin itself, so the in-memory
        state is set first and outside the try.
        """
        with self._lock:
            self._active_label = label
            if label:
                # Pinning a key the user can see marked "exhausted" has to make
                # it usable again, or the pin silently does nothing.
                e = self._by_label.get(label)
                if e is not None:
                    e["active"] = True
                    e["errs"] = 0
        self._save_pin(label)

    # ── Pin persistence ────────────────────────────────────────────────────────

    _PIN_SETTING = "pinned_key"
    _PIN_SETTING_LEGACY = "cli_pinned_key"   # written by the old CLI-only rotator

    def _save_pin(self, label: str | None) -> None:
        try:
            from agent2.database import set_setting
            set_setting(self._PIN_SETTING, label or "")
        except Exception:
            return
        # Tell the other process. In-process this also re-runs reload(), which is
        # harmless (the pin lives outside the reloaded entry state) and rare —
        # pinning is a human action, not a hot path.
        try:
            from agent2.core import sync
            sync.notify("api_keys")
        except Exception:
            pass

    def _load_pin(self) -> None:
        """Restore a persisted pin. Falls back to the legacy CLI-only setting so
        an existing pin survives this unification rather than silently resetting."""
        lbl = ""
        try:
            from agent2.database import get_setting
            lbl = (get_setting(self._PIN_SETTING) or "").strip()
            if not lbl:
                lbl = (get_setting(self._PIN_SETTING_LEGACY) or "").strip()
        except Exception:
            return
        with self._lock:
            self._active_label = lbl or None

    def add(self, key: str, name: str | None = None) -> tuple[bool, str]:
        ok, label = add_api_key(key, name)
        if ok:
            self.reload()
        return ok, label

    def remove(self, label: str) -> None:
        remove_api_key(label)
        cleared = False
        with self._lock:
            if self._active_label == label:
                self._active_label = None
                cleared = True
        # Must persist too: leaving the setting behind would restore a pin to a
        # key that no longer exists on the next start, and get() would then fall
        # through to auto-rotate with the UI still claiming a key is pinned.
        if cleared:
            self._save_pin(None)
        self.reload()

    # ── Status snapshot (safe for JSON serialisation) ─────────────────────────

    def status(self) -> list[dict]:
        with self._lock:
            return [
                {
                    "label":    e["label"],
                    "name":     e["name"],
                    "preview":  e["key"][:14] + "…",
                    "active":   e["active"],
                    "errs":     e["errs"],
                    "tokens":   e["tokens"],
                    "requests": e["requests"],
                    "last_used": e["last_used"],
                    "pinned":   self._active_label == e["label"],
                }
                for e in self.entries
            ]


# ── Singleton ──────────────────────────────────────────────────────────────────
try:
    rotator = KeyRotator()
except Exception:
    # Never let key loading crash import of the whole app. Every attribute the
    # methods touch must be initialised here too, since __init__ never ran.
    rotator = KeyRotator.__new__(KeyRotator)
    rotator._lock = threading.RLock()
    rotator.entries = []
    rotator._by_label = {}
    rotator._by_key = {}
    rotator._active_label = None
    rotator._clients = {}
    rotator._rr = 0
    rotator._pending = {}


# ── Cross-surface sync ─────────────────────────────────────────────────────────
# A key added in the browser must become usable in the CLI (and vice versa)
# without a restart. agent2.core.sync bumps the 'api_keys' version on every
# mutation; this listener reloads the in-memory entries when that happens.
def _on_keys_changed(_topic, _payload) -> None:
    try:
        rotator.reload()
    except Exception:
        pass
    # The pin is DB state now, so a pin set in the OTHER process (dual mode)
    # arrives here too. reload() deliberately does not touch it — it rebuilds
    # entries, and clobbering a local pin from a stale read would be worse than
    # a slightly late one — so it is re-read explicitly.
    try:
        rotator._load_pin()
    except Exception:
        pass


try:
    from agent2.core import sync as _sync
    _sync.subscribe("api_keys", _on_keys_changed)
except Exception:
    pass
