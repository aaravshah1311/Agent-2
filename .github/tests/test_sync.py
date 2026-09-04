# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
Tests for agent2.core.sync — the centralized synchronization layer.

These are concurrency primitives, so the tests deliberately run real threads and
assert on observed interleavings (mutual exclusion, no starvation, no lost
updates) rather than just calling each method once. The failsafe expectations are
asserted too: a broken subscriber must never break the publisher.
"""

import threading
import time

import pytest

from agent2 import database as db
from agent2.core import sync


@pytest.fixture(autouse=True)
def _schema():
    db.init_db()
    yield


# ── RWLock ─────────────────────────────────────────────────────────────────────

def test_rwlock_allows_concurrent_readers():
    lock = sync.RWLock()
    inside = []
    peak = [0]
    guard = threading.Lock()
    release = threading.Event()

    def reader():
        with lock.read():
            with guard:
                inside.append(1)
                peak[0] = max(peak[0], len(inside))
            release.wait(2.0)
            with guard:
                inside.pop()

    threads = [threading.Thread(target=reader) for _ in range(5)]
    for t in threads:
        t.start()
    # Give them time to all get inside the read section simultaneously.
    deadline = time.time() + 2.0
    while peak[0] < 5 and time.time() < deadline:
        time.sleep(0.01)
    release.set()
    for t in threads:
        t.join()

    assert peak[0] == 5, "readers were serialized; they should run concurrently"


def test_rwlock_writer_is_exclusive():
    lock = sync.RWLock()
    concurrent = [0]
    peak = [0]
    guard = threading.Lock()

    def writer():
        for _ in range(50):
            with lock.write():
                with guard:
                    concurrent[0] += 1
                    peak[0] = max(peak[0], concurrent[0])
                time.sleep(0.0005)
                with guard:
                    concurrent[0] -= 1

    threads = [threading.Thread(target=writer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert peak[0] == 1, "two writers held the lock at once"


def test_rwlock_no_lost_updates():
    lock = sync.RWLock()
    state = {"n": 0}

    def bump():
        for _ in range(500):
            with lock.write():
                state["n"] += 1

    threads = [threading.Thread(target=bump) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert state["n"] == 2000


def test_rwlock_writer_not_starved_by_readers():
    """A steady stream of readers must not block a pending writer forever."""
    lock = sync.RWLock()
    stop = threading.Event()
    wrote = threading.Event()

    def reader():
        while not stop.is_set():
            with lock.read():
                time.sleep(0.001)

    def writer():
        with lock.write():
            wrote.set()

    readers = [threading.Thread(target=reader) for _ in range(6)]
    for t in readers:
        t.start()
    time.sleep(0.05)          # let the readers get going
    w = threading.Thread(target=writer)
    w.start()
    got_in = wrote.wait(5.0)
    stop.set()
    w.join()
    for t in readers:
        t.join()

    assert got_in, "writer starved by continuous readers"


def test_rwlock_releases_on_exception():
    lock = sync.RWLock()
    with pytest.raises(ValueError):
        with lock.write():
            raise ValueError("boom")
    # If the lock leaked, this would deadlock rather than return.
    with lock.write():
        pass
    with lock.read():
        pass


# ── KeyedLock ──────────────────────────────────────────────────────────────────

def test_keyedlock_same_key_serializes():
    kl = sync.KeyedLock()
    peak = [0]
    cur = [0]
    guard = threading.Lock()

    def worker():
        for _ in range(30):
            with kl.hold("chat-1"):
                with guard:
                    cur[0] += 1
                    peak[0] = max(peak[0], cur[0])
                time.sleep(0.0005)
                with guard:
                    cur[0] -= 1

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak[0] == 1


def test_keyedlock_different_keys_do_not_block():
    kl = sync.KeyedLock()
    both_inside = threading.Barrier(2, timeout=3.0)
    errors = []

    def worker(key):
        try:
            with kl.hold(key):
                both_inside.wait()      # only passes if they run concurrently
        except Exception as exc:
            errors.append(exc)

    a = threading.Thread(target=worker, args=("chat-a",))
    b = threading.Thread(target=worker, args=("chat-b",))
    a.start()
    b.start()
    a.join()
    b.join()
    assert not errors, f"different keys serialized: {errors}"


def test_keyedlock_reclaims_idle_locks():
    kl = sync.KeyedLock()
    for i in range(200):
        with kl.hold(f"chat-{i}"):
            pass
    assert kl.active() == 0, "idle locks leaked"


def test_keyedlock_releases_on_exception():
    kl = sync.KeyedLock()
    with pytest.raises(ValueError):
        with kl.hold("k"):
            raise ValueError("boom")
    with kl.hold("k"):
        pass
    assert kl.active() == 0


# ── AtomicCounter ──────────────────────────────────────────────────────────────

def test_atomic_counter_under_contention():
    ctr = sync.AtomicCounter()

    def bump():
        for _ in range(1000):
            ctr.increment()

    threads = [threading.Thread(target=bump) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert ctr.value == 4000


# ── EventBus ───────────────────────────────────────────────────────────────────

def test_eventbus_delivers_and_unsubscribes():
    bus = sync.EventBus()
    got = []
    fn = lambda topic, payload: got.append((topic, payload.get("n")))

    bus.subscribe("thing", fn)
    assert bus.publish("thing", {"n": 1}) == 1
    bus.unsubscribe("thing", fn)
    bus.publish("thing", {"n": 2})
    assert got == [("thing", 1)]


def test_a_fully_unsubscribed_topic_leaves_no_phantom_behind():
    """`topics()` is a REPORT of what has listeners — /api/sync and /api/health
    both print it. An emptied list left in place names a topic nothing will ever be
    delivered on, and a reader cannot tell that from a live subscription."""
    bus = sync.EventBus()
    fn = lambda topic, payload: None

    bus.subscribe("gone", fn)
    assert "gone" in bus.topics()
    bus.unsubscribe("gone", fn)
    assert bus.topics() == [], "an unsubscribed topic is still reported as a topic"
    assert bus.publish("gone") == 0, "unsubscribing must still actually unsubscribe"


def test_eventbus_wildcard_listener():
    bus = sync.EventBus()
    seen = []
    bus.subscribe("*", lambda t, p: seen.append(t))
    bus.publish("memories")
    bus.publish("rules")
    assert seen == ["memories", "rules"]


def test_eventbus_broken_subscriber_does_not_break_publisher():
    """FAILSAFE: one bad listener must not stop the others or raise."""
    bus = sync.EventBus()
    delivered = []

    def bad(_t, _p):
        raise RuntimeError("subscriber exploded")

    bus.subscribe("t", bad)
    bus.subscribe("t", lambda _t, _p: delivered.append(1))

    assert bus.publish("t") == 2        # does not raise
    assert delivered == [1], "a later subscriber was skipped after an error"
    assert bus.errors.value == 1


def test_eventbus_payload_carries_resource():
    bus = sync.EventBus()
    seen = {}
    bus.subscribe("memories", lambda t, p: seen.update(p))
    bus.publish("memories")
    assert seen["resource"] == "memories"


def test_eventbus_concurrent_publish_is_safe():
    bus = sync.EventBus()
    count = sync.AtomicCounter()
    bus.subscribe("x", lambda _t, _p: count.increment())

    def pub():
        for _ in range(200):
            bus.publish("x")

    threads = [threading.Thread(target=pub) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert count.value == 800


# ── ChangeTracker / version counters ───────────────────────────────────────────

def test_change_tracker_primes_silently():
    db.bump_version("memories")
    tr = sync.ChangeTracker()
    assert tr.check() == [], "first check should prime, not report everything"


def test_change_tracker_reports_only_changes():
    tr = sync.ChangeTracker()
    tr.prime()
    assert tr.check() == []
    db.bump_version("rules")
    assert tr.check() == ["rules"]
    assert tr.check() == [], "a change was reported twice"


def test_change_tracker_check_versions_single_read():
    tr = sync.ChangeTracker()
    tr.prime()
    db.bump_version("chats")
    changed, versions = tr.check_versions()
    assert changed == ["chats"]
    assert versions["chats"] == db.get_version("chats")


def test_notify_bumps_version_and_publishes():
    seen = []
    fn = lambda t, p: seen.append(p.get("version"))
    sync.subscribe("workspace", fn)
    try:
        before = db.get_version("workspace")
        version = sync.notify("workspace", action="test")
        assert version == before + 1
        assert seen == [version]
    finally:
        sync.unsubscribe("workspace", fn)


def test_notify_blank_resource_is_noop():
    assert sync.notify("") == 0
    assert sync.notify("   ") == 0


def test_poll_changes_skips_own_writes():
    """A local notify() already published; the poller must not double-fire it."""
    sync.tracker.prime()
    fired = []
    fn = lambda t, p: fired.append(p.get("remote"))
    sync.subscribe("providers", fn)
    try:
        sync.notify("providers", action="local")
        assert fired == [None], "local notify should publish exactly once"
        assert "providers" not in sync.poll_changes()
        assert fired == [None], "poller re-published a local write"
    finally:
        sync.unsubscribe("providers", fn)


def test_poll_changes_fires_for_external_writes():
    """A bump that did NOT come through notify() looks like another process."""
    sync.tracker.prime()
    fired = []
    fn = lambda t, p: fired.append(p.get("remote"))
    sync.subscribe("settings", fn)
    try:
        db.bump_version("settings")          # simulate the other process
        assert "settings" in sync.poll_changes()
        assert fired == [True], "remote change not published with remote=True"
    finally:
        sync.unsubscribe("settings", fn)


# ── VersionedCache ─────────────────────────────────────────────────────────────

def test_versioned_cache_builds_once_then_serves_cached():
    calls = sync.AtomicCounter()

    def build():
        calls.increment()
        return f"value-{calls.value}"

    cache = sync.VersionedCache("memories", build)
    first = cache.get()
    for _ in range(50):
        assert cache.get() == first
    assert calls.value == 1
    assert cache.hits.value >= 49


def test_versioned_cache_rebuilds_after_notify():
    calls = sync.AtomicCounter()
    cache = sync.VersionedCache("chats", lambda: calls.increment())

    first = cache.get()
    sync.notify("chats", action="test")
    second = cache.get()
    assert second != first, "cache served stale data after its resource changed"


def test_versioned_cache_invalidate_forces_rebuild():
    calls = sync.AtomicCounter()
    cache = sync.VersionedCache("pil", lambda: calls.increment())
    cache.get()
    cache.invalidate()
    cache.get()
    assert calls.value == 2


def test_versioned_cache_ttl_expiry():
    calls = sync.AtomicCounter()
    cache = sync.VersionedCache("api_keys", lambda: calls.increment(), ttl=0.05)
    cache.get()
    cache.get()
    assert calls.value == 1
    time.sleep(0.08)
    cache.get()
    assert calls.value == 2


def test_versioned_cache_concurrent_get_is_consistent():
    """Many threads racing on a cold cache must all see a valid value."""
    def build():
        time.sleep(0.01)
        return "built"

    cache = sync.VersionedCache("workspace", build)
    results = []
    guard = threading.Lock()

    def worker():
        v = cache.get()
        with guard:
            results.append(v)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == ["built"] * 10


# ── SyncPoller ─────────────────────────────────────────────────────────────────

def test_poller_start_stop_is_idempotent():
    p = sync.SyncPoller(interval=0.25)
    p.start()
    p.start()                     # second start must not spawn a second thread
    assert p.running
    p.stop()
    assert not p.running
    p.stop()                      # stopping twice is safe


def test_poller_detects_external_change():
    p = sync.SyncPoller(interval=0.25)
    fired = threading.Event()
    fn = lambda t, payload: fired.set() if payload.get("remote") else None
    sync.subscribe("memories", fn)
    p.start()
    try:
        db.bump_version("memories")     # as if another process wrote
        assert fired.wait(4.0), "poller did not pick up an external change"
        assert p.polls.value >= 1
    finally:
        p.stop()
        sync.unsubscribe("memories", fn)


def test_poller_thread_is_daemon():
    """FAILSAFE: the poller must never keep the process alive at shutdown."""
    p = sync.SyncPoller(interval=0.25)
    p.start()
    try:
        assert p._thread is not None and p._thread.daemon
    finally:
        p.stop()


def test_stats_shape():
    s = sync.stats()
    for key in ("resources", "versions", "topics", "listener_errors",
                "poller_running", "polls"):
        assert key in s
    assert set(s["resources"]) == set(sync.RESOURCES)
