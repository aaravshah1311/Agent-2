# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2.core.sync
─────────────────
THE centralized synchronization layer.

Agent2 is concurrent in three different ways at once, and before this module
each one was handled ad hoc (or not at all):

  1. Threads in one process — the Web UI runs every turn in its own daemon
     thread, PIL optimization spawns background threads, and Flask serves
     requests on yet more threads. They all touch the same caches and the same
     SQLite file.
  2. Two processes over one DB — dual mode runs the web server and the CLI as
     separate processes against a single agent2.db. Neither can see the other's
     in-memory state, so a memory added in the browser was invisible to the CLI
     until restart.
  3. Two surfaces over one user — the browser and the terminal must agree on
     memories, rules, keys, providers, workspace and settings.

What lives here
  • RWLock       — many readers OR one writer; for read-mostly caches.
  • KeyedLock    — one lock per key, so work on different chats never contends.
  • AtomicCounter/Latch — small primitives used by the caches and pollers.
  • EventBus     — in-process publish/subscribe, so a change in one module
                   notifies every listener synchronously and without polling.
  • ChangeTracker + poll_changes() — the cross-PROCESS half: reads the
                   `sync_state` version counters (see agent2.database) and fires
                   the same EventBus topics when another process bumped one.
  • notify()/subscribe() — the public API the rest of the app uses. `notify`
                   bumps the DB version AND publishes in-process, so local
                   listeners react instantly and remote ones on their next poll.

Design rules
  • Every public function swallows listener errors: one bad subscriber must
    never break the publisher or the agent turn that triggered it.
  • Locks are always released via `finally` / context managers.
  • Nothing here blocks on the network or holds a lock across I/O.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager

from agent2 import database as db

# ── Known resources ────────────────────────────────────────────────────────────
# The shared things both surfaces render. Kept as a tuple so pollers can iterate
# a stable set, and typos in a topic name are easy to spot in review.
RESOURCES = (
    "memories",
    "rules",
    "api_keys",
    "providers",
    "settings",
    "workspace",
    "chats",
    "pil",
    # The persistent checklist (core/tasks.py). Listed here so a task list a
    # CLI process merges is republished to a web process on its next poll —
    # without it, the two surfaces would show different progress for one plan.
    "tasks",
    # Per-project MCP auto-connect (integrations/state.py). Listed for the same
    # reason: toggling ZAP in the CLI must reach a web process's `/mcp` view,
    # and the poller only republishes resources it iterates.
    "mcp",
)


# ── Primitives ─────────────────────────────────────────────────────────────────

class RWLock:
    """Readers/writer lock: unlimited concurrent readers, exclusive writers.

    For caches that are read on every keystroke or every agent turn but written
    rarely (PIL prediction index, system-prompt cache). A plain Lock would
    serialize the reads that dominate.

    Writer-preferring: a waiting writer blocks NEW readers, so a steady stream
    of reads cannot starve a pending invalidation.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition(threading.Lock())
        self._readers = 0
        self._writer = False
        self._waiting_writers = 0

    @contextmanager
    def read(self):
        self._acquire_read()
        try:
            yield
        finally:
            self._release_read()

    @contextmanager
    def write(self):
        self._acquire_write()
        try:
            yield
        finally:
            self._release_write()

    def _acquire_read(self) -> None:
        with self._cond:
            # Wait out an active writer and any queued writer (no starvation).
            while self._writer or self._waiting_writers > 0:
                self._cond.wait()
            self._readers += 1

    def _release_read(self) -> None:
        with self._cond:
            self._readers -= 1
            if self._readers == 0:
                self._cond.notify_all()

    def _acquire_write(self) -> None:
        with self._cond:
            self._waiting_writers += 1
            try:
                while self._writer or self._readers > 0:
                    self._cond.wait()
                self._writer = True
            finally:
                self._waiting_writers -= 1

    def _release_write(self) -> None:
        with self._cond:
            self._writer = False
            self._cond.notify_all()


class KeyedLock:
    """A lock per key, created on demand.

    Two turns in two different chats must not serialize against each other, but
    two turns in the SAME chat must. Keying the lock by chat id gives exactly
    that. Idle locks are reclaimed once nobody holds or waits on them, so a long
    session cannot leak one lock per chat id forever.
    """

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._users: dict[str, int] = {}

    @contextmanager
    def hold(self, key: str):
        key = str(key or "-")
        with self._guard:
            lk = self._locks.get(key)
            if lk is None:
                lk = self._locks[key] = threading.Lock()
            self._users[key] = self._users.get(key, 0) + 1
        lk.acquire()
        try:
            yield
        finally:
            lk.release()
            with self._guard:
                self._users[key] = self._users.get(key, 1) - 1
                if self._users[key] <= 0:
                    self._users.pop(key, None)
                    self._locks.pop(key, None)

    def active(self) -> int:
        with self._guard:
            return len(self._locks)


class AtomicCounter:
    """Thread-safe integer. Used for cache generations and poll bookkeeping."""

    def __init__(self, initial: int = 0) -> None:
        self._v = int(initial)
        self._lock = threading.Lock()

    def increment(self, by: int = 1) -> int:
        with self._lock:
            self._v += by
            return self._v

    @property
    def value(self) -> int:
        with self._lock:
            return self._v


# ── In-process event bus ───────────────────────────────────────────────────────

class EventBus:
    """Minimal synchronous publish/subscribe.

    Listeners are called on the publishing thread, so a subscriber must be quick
    and must not block. Exceptions from subscribers are swallowed and counted —
    a broken listener never propagates into an agent turn.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._subs: dict[str, list] = {}
        self.errors = AtomicCounter()

    def subscribe(self, topic: str, fn) -> None:
        """Register *fn* for *topic*. Called as fn(topic, payload_dict)."""
        with self._lock:
            self._subs.setdefault(str(topic), []).append(fn)

    def unsubscribe(self, topic: str, fn) -> None:
        with self._lock:
            lst = self._subs.get(str(topic))
            if lst and fn in lst:
                lst.remove(fn)
            # ⚠️ Drop the key once nobody is listening. `topics()` is a REPORT of
            # what has listeners (`/api/sync`, and the health endpoint's sync
            # section), and an emptied list left behind names a topic that will
            # never be delivered to anyone — a reader cannot tell that from a live
            # subscription, so an unsubscribed probe looks like a leak forever.
            if lst is not None and not lst:
                self._subs.pop(str(topic), None)

    def publish(self, topic: str, payload: dict | None = None) -> int:
        """Notify every subscriber of *topic* plus every '*' wildcard listener.

        Returns the number of listeners invoked. Never raises.
        """
        topic = str(topic)
        with self._lock:
            listeners = list(self._subs.get(topic, ())) + list(self._subs.get("*", ()))
        data = dict(payload or {})
        data.setdefault("resource", topic)
        for fn in listeners:
            try:
                fn(topic, data)
            except Exception:
                self.errors.increment()
        return len(listeners)

    def topics(self) -> list[str]:
        with self._lock:
            return sorted(self._subs)


bus = EventBus()


# ── Cross-process change tracking ──────────────────────────────────────────────

class ChangeTracker:
    """Detects resource changes made by ANOTHER process via `sync_state`.

    `check()` reads every version in one query and returns the resources whose
    version moved since the last call. The first call primes the baseline and
    reports nothing, so a fresh tracker never fires a spurious "everything
    changed" storm at startup.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seen: dict[str, int] = {}
        self._primed = False

    def prime(self) -> None:
        """Adopt current versions as the baseline without reporting changes."""
        versions = db.all_versions()
        with self._lock:
            self._seen = dict(versions)
            self._primed = True

    def check(self) -> list[str]:
        """Resources whose version changed since the previous check."""
        return self.check_versions()[0]

    def check_versions(self) -> tuple[list[str], dict[str, int]]:
        """Like check(), but also returns the versions it just read.

        Callers that need both (poll_changes) would otherwise query `sync_state`
        twice per tick — once here and once to re-read the current versions.
        """
        versions = db.all_versions()
        with self._lock:
            if not self._primed:
                self._seen = dict(versions)
                self._primed = True
                return [], versions
            changed = [r for r, v in versions.items() if self._seen.get(r) != v]
            self._seen = dict(versions)
        return changed, versions


tracker = ChangeTracker()


# ── Public API ─────────────────────────────────────────────────────────────────

# Bumps this process performed itself. A poller uses this to avoid re-publishing
# its own writes when it sees the version move (it already published locally).
_own_bumps: dict[str, int] = {}
_own_lock = threading.Lock()


def notify(resource: str, **payload) -> int:
    """Announce that *resource* changed.

    Does both halves of the job:
      1. bumps the `sync_state` version so OTHER processes notice on their poll;
      2. publishes on the in-process bus so listeners here react immediately.

    Returns the new version (0 if the DB bump failed — the local publish still
    happened, so the current surface stays correct either way).
    """
    resource = str(resource or "").strip()
    if not resource:
        return 0
    version = db.bump_version(resource, origin=payload.get("origin", ""))
    if version:
        with _own_lock:
            _own_bumps[resource] = version
    data = dict(payload)
    data["version"] = version
    bus.publish(resource, data)
    return version


def subscribe(resource: str, fn) -> None:
    """Listen for changes to *resource* (or '*' for all). fn(topic, payload)."""
    bus.subscribe(resource, fn)


def unsubscribe(resource: str, fn) -> None:
    bus.unsubscribe(resource, fn)


def poll_changes() -> list[str]:
    """Publish bus events for changes made by other processes.

    Call this from a periodic timer (the web server) or before rendering a
    shared list (the CLI). Resources this process bumped itself are filtered
    out — `notify()` already published them locally, so re-publishing would
    double-fire every listener.
    """
    changed, current = tracker.check_versions()
    if not changed:
        return []
    with _own_lock:
        mine = dict(_own_bumps)
    fired: list[str] = []
    for res in changed:
        if mine.get(res) == current.get(res):
            continue                  # our own write — already published locally
        bus.publish(res, {"resource": res, "version": current.get(res, 0),
                          "remote": True})
        fired.append(res)
    return fired


class SyncPoller:
    """Background thread that calls poll_changes() on an interval.

    Used by the web server and by dual mode so a change made in the CLI shows up
    in the browser (and vice versa) without a manual refresh. Daemon thread:
    it never keeps the process alive.
    """

    def __init__(self, interval: float = 2.0) -> None:
        self.interval = max(0.25, float(interval))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.polls = AtomicCounter()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        tracker.prime()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="agent2-sync",
                                        daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                poll_changes()
                self.polls.increment()
            except Exception:
                pass    # a poll failure is never fatal; try again next tick

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=2.0)
        self._thread = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())


poller = SyncPoller()


# ── Cache invalidation helper ──────────────────────────────────────────────────

class VersionedCache:
    """A read-mostly cache that rebuilds when its resource version changes.

    Wraps the common pattern: hold a computed value, serve it under a read lock,
    and rebuild it under a write lock when either a local `notify()` or a remote
    change bumped the underlying resource. Used for the system prompt and the
    PIL prediction index.
    """

    def __init__(self, resource: str, builder, ttl: float = 0.0) -> None:
        self.resource = resource
        self._builder = builder
        self._ttl = float(ttl)
        self._lock = RWLock()
        self._value = None
        self._built_at = 0.0
        self._version = -1
        self._dirty = True
        self.hits = AtomicCounter()
        self.misses = AtomicCounter()
        subscribe(resource, self._on_change)

    def _on_change(self, _topic, _payload) -> None:
        with self._lock.write():
            self._dirty = True

    def _stale(self) -> bool:
        if self._dirty or self._value is None:
            return True
        if self._ttl and (time.time() - self._built_at) > self._ttl:
            return True
        return False

    def get(self, *args, **kwargs):
        with self._lock.read():
            if not self._stale():
                self.hits.increment()
                return self._value
        with self._lock.write():
            if self._stale():                     # re-check under the write lock
                self._value = self._builder(*args, **kwargs)
                self._built_at = time.time()
                self._version = db.get_version(self.resource)
                self._dirty = False
                self.misses.increment()
            return self._value

    def invalidate(self) -> None:
        with self._lock.write():
            self._dirty = True


def stats() -> dict:
    """Snapshot of the sync layer — surfaced by /api/sync for debugging."""
    return {
        "resources":       list(RESOURCES),
        "versions":        db.all_versions(),
        "topics":          bus.topics(),
        "listener_errors": bus.errors.value,
        "poller_running":  poller.running,
        "polls":           poller.polls.value,
    }
