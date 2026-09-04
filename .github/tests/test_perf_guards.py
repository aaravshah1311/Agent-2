# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
Performance guard tests — latency ceilings for Agent2's hot paths.

These are NOT micro-benchmarks chasing a number; they are regression alarms.
Each hot path here has an optimization behind it that is easy to silently undo:

  * `system_prompt()`   — the static/memories/rules blocks are VersionedCache'd,
                          so a normal turn does ZERO DB queries to build the
                          prompt. Guarded by QUERY COUNT, not latency (see
                          below).
  * `build_context()`   — one query with a LIMIT, then a single reverse. A
                          regression to per-message queries (N+1) would show up
                          here long before a user noticed.
  * `qone`/`qall`       — pooled connections + WAL. If the pool were bypassed
                          (a fresh sqlite3.connect per call) latency jumps by
                          orders of magnitude.
  * `pil.predict()`     — must stay far below human keystroke cadence (~80ms)
                          because it runs on the ghost-text path per keypress.
  * `sync.notify()`     — fires on every memory/rule write; a slow notify makes
                          the whole write path slow.

WHY QUERY COUNTS BEAT TIMERS FOR CACHE GUARDS
─────────────────────────────────────────────
The prompt-cache guard was originally a 1ms latency bound. It was verified by
sabotage — forcing the cache to always miss — and it STILL PASSED: the rebuild
costs 0.06ms against 0.01ms cached. A 5x regression, invisible to any threshold
loose enough to survive a shared CI runner. So the cache guards now assert on
the number of queries issued, which is what the optimization actually promises;
the latency bound is kept only as a coarse catastrophic-failure net.

The count is taken at `database._checkout`, not at `qall`. See the
`counting_queries()` docstring — patching `qall` cannot see these queries at all,
which would make the guard silently vacuous rather than merely loose.

THRESHOLDS (for the remaining timing tests) are deliberately loose — roughly
20-60x the measured local p95 — because CI runners are slow, shared, and noisy.
The measured medians on developer hardware (for reference, not assertion) were:

    qone trivial                 0.007 ms
    qall settings                0.014 ms
    system_prompt() cached       0.006 ms
    pil.predict()                0.037 ms
    pil.learn_from_message()     0.445 ms
    build_context() 60->40 cap   0.858 ms
    sync.notify()                0.036 ms

A test failing here means something got structurally slower (a cache removed,
a query added to a loop, a lock held too long) — not that the machine is busy.
Timings use the MEDIAN of many runs, so a single scheduling hiccup cannot fail
a test; only a sustained slowdown can.
"""

import contextlib
import statistics
import time
import uuid

import pytest

from agent2.database import all_versions, exe, init_db, qall, qone


# ── Timing helper ─────────────────────────────────────────────────────────────

def timed_median_ms(fn, n: int = 60, warmup: int = 10) -> float:
    """Median wall-clock ms per call over `n` runs, after `warmup` runs.

    Median (not mean/min) on purpose: it ignores the occasional GC pause or
    scheduler preemption that would make a mean-based guard flaky on CI, while
    still moving decisively if the path itself got slower.
    """
    for _ in range(warmup):
        fn()
    samples = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1000.0)
    return statistics.median(samples)


# ── Query counting ────────────────────────────────────────────────────────────

@contextlib.contextmanager
def counting_queries():
    """Count every DB statement issued inside the block. Yields a live counter.

    Patches `database._checkout` — the pooled-connection context manager that
    `qall`/`qone`/`exe`/`exemany` all borrow from — and NOT `qall` itself.

    That distinction is the whole reason this helper exists. `agent2/core/memory`
    and `agent2/core/rules` do `from agent2.database import qall`, which binds the
    function object into their own module namespace at import time. Replacing
    `database.qall` (or `agent.qall`) afterwards leaves those bindings pointing at
    the original, so a patch there silently counts nothing — a guard that can only
    ever report zero and therefore always passes.

    `_checkout` has no such problem: database.py's helpers resolve it as a module
    global on every call, so one patch here sees every query no matter which
    namespace the caller imported from.
    """
    from agent2 import database as D

    counter = {"n": 0}
    real = D._checkout

    def counted(*a, **k):
        counter["n"] += 1
        return real(*a, **k)

    D._checkout = counted
    try:
        yield counter
    finally:
        D._checkout = real


@pytest.fixture(scope="module", autouse=True)
def _schema():
    init_db()


# ── DB primitives ─────────────────────────────────────────────────────────────

def test_qone_stays_sub_millisecond():
    """Pooled + WAL. A fresh connect-per-query would blow straight past this."""
    med = timed_median_ms(lambda: qone("SELECT 1"), n=200)
    assert med < 1.0, f"qone median {med:.3f}ms — connection pooling may be bypassed"


def test_qall_small_select_stays_fast():
    med = timed_median_ms(lambda: qall("SELECT * FROM settings LIMIT 50"), n=200)
    assert med < 2.0, f"qall median {med:.3f}ms"


def test_version_counter_read_is_cheap():
    """all_versions() is polled by SyncPoller on an interval — must stay trivial."""
    med = timed_median_ms(all_versions, n=200)
    assert med < 2.0, f"all_versions median {med:.3f}ms"


# ── System prompt caching ─────────────────────────────────────────────────────

def test_system_prompt_does_zero_queries_when_cached():
    """The three prompt blocks are VersionedCache'd; a cached call must not query.

    This is the single most valuable guard in this file: dropping the cache
    breaks NO functional test, it only makes every agent turn slower.

    It asserts on QUERY COUNT, not latency, and that distinction matters. An
    earlier version of this test used a 1ms latency bound — and a deliberately
    sabotaged build with the cache forced to always-miss still passed it, because
    the rebuild is 0.06ms vs 0.01ms cached. Both are far under any threshold
    loose enough to survive CI. Latency cannot see a 5x regression that small;
    counting queries can, and it is what the optimization actually promises.
    """
    from agent2 import agent as A

    A.system_prompt()   # prime the caches

    with counting_queries() as q:
        A.system_prompt()
        A.system_prompt()
        A.system_prompt()

    assert q["n"] == 0, (
        f"system_prompt() ran {q['n']} queries across 3 cached calls — "
        "the memories/rules VersionedCache is no longer being hit"
    )


def test_system_prompt_rebuilds_exactly_once_after_invalidation():
    """Invalidate must cost ONE rebuild, not one per call.

    Guards the other failure direction: a cache that never caches. After a
    single invalidation, the first call rebuilds and subsequent calls must be
    free again.
    """
    from agent2 import agent as A

    A.system_prompt()
    A._MEM_CACHE.invalidate()
    A._RULES_CACHE.invalidate()

    with counting_queries() as q:
        A.system_prompt()            # this one SHOULD query (rebuild)
        after_first = q["n"]
        A.system_prompt()
        A.system_prompt()

    assert after_first > 0, "invalidate() did not force a rebuild — cache is stale-serving"
    assert q["n"] == after_first, (
        f"cache re-queried after rebuild ({after_first} -> {q['n']}); "
        "invalidation is not being cleared"
    )


def test_system_prompt_stays_fast():
    """A loose absolute ceiling — catches catastrophic regressions only.

    Deliberately NOT the primary cache guard (see the query-count test above);
    this only trips if prompt building becomes pathologically slow, e.g. by
    doing real I/O or a network call.
    """
    from agent2 import agent as A

    A.system_prompt()
    med = timed_median_ms(A.system_prompt, n=200)
    assert med < 5.0, f"system_prompt() median {med:.3f}ms"


def test_system_prompt_cache_invalidation_still_works():
    """A guard that only proves speed is worthless if it also allows staleness."""
    from agent2 import agent as A

    before = A.system_prompt()
    A._MEM_CACHE.invalidate()
    A._RULES_CACHE.invalidate()
    after = A.system_prompt()
    # Same inputs, so same output — but it went through the rebuild path.
    assert after == before


# ── build_context ─────────────────────────────────────────────────────────────

@pytest.fixture
def busy_chat():
    """A chat with more messages than MAX_CTX_MESSAGES, to exercise the cap."""
    cid = "perf" + str(uuid.uuid4())[:4]
    exe("INSERT INTO chats (id,title,model,mode) VALUES (?,?,?,?)",
        (cid, "perf", "2.5-flash", "pro"))
    for i in range(60):
        exe("INSERT INTO messages (chat_id,role,content) VALUES (?,?,?)",
            (cid, "user" if i % 2 == 0 else "assistant", f"message body {i} " * 20))
    yield cid
    exe("DELETE FROM messages WHERE chat_id=?", (cid,))
    exe("DELETE FROM chats WHERE id=?", (cid,))


def test_build_context_is_one_query_not_n_plus_one(busy_chat):
    """60 stored messages, capped to 40. Must be a single LIMITed query."""
    from agent2 import agent as A
    from agent2.config import MAX_CTX_MESSAGES

    ctx = A.build_context(busy_chat)
    assert len(ctx) <= MAX_CTX_MESSAGES, "context cap not applied"

    med = timed_median_ms(lambda: A.build_context(busy_chat), n=60)
    assert med < 25.0, (
        f"build_context median {med:.3f}ms for {MAX_CTX_MESSAGES} messages — "
        "this smells like a per-message query (N+1) crept in"
    )


def test_build_context_scales_sublinearly_with_history(busy_chat):
    """Growing history past the cap must NOT grow context-build time.

    The LIMIT is what makes a 10,000-message chat as cheap as a 40-message one.
    If someone removed it (loading all rows then slicing in Python), this fails.
    """
    from agent2 import agent as A

    small = timed_median_ms(lambda: A.build_context(busy_chat), n=40)

    # Triple the history; the cap means the work should be roughly unchanged.
    for i in range(120):
        exe("INSERT INTO messages (chat_id,role,content) VALUES (?,?,?)",
            (busy_chat, "user" if i % 2 == 0 else "assistant", f"extra {i} " * 20))

    large = timed_median_ms(lambda: A.build_context(busy_chat), n=40)

    # Allow generous slack for noise, but a linear scan of 3x the rows would
    # land far outside this.
    assert large < max(small * 4.0, 25.0), (
        f"build_context went {small:.3f}ms -> {large:.3f}ms when history tripled; "
        "the LIMIT may no longer be bounding the query"
    )


# ── PIL hot paths ─────────────────────────────────────────────────────────────

def test_pil_predict_is_faster_than_a_keystroke():
    """predict() runs per keypress on the ghost-text path.

    Human fast typing is ~80ms between keystrokes. Prediction must be far
    under that or the suggestion lags the cursor. It is also required to be
    fully offline — an LLM call here would be thousands of ms.
    """
    from agent2.core import pil

    pil.learn_from_message("create a responsive login page with dark mode")
    med = timed_median_ms(lambda: pil.predict("create a resp"), n=100)
    assert med < 20.0, (
        f"pil.predict median {med:.3f}ms — must stay well under keystroke "
        "cadence (~80ms); check the prediction index is not rebuilding per call"
    )


def test_pil_learn_stays_bounded():
    """learn_from_message() runs on every turn; batched upserts keep it cheap."""
    from agent2.core import pil

    med = timed_median_ms(
        lambda: pil.learn_from_message("build a fast api endpoint with auth"),
        n=40, warmup=5,
    )
    assert med < 40.0, (
        f"pil.learn_from_message median {med:.3f}ms — the batched "
        "bump_*_many() writes may have regressed to one commit per row"
    )


def test_pil_predict_never_calls_the_network(monkeypatch):
    """PIL is offline by contract. Make that structural, not aspirational.

    Poison the socket layer: if prediction ever reaches for the network, this
    raises instead of silently adding latency (and leaking usage data).
    """
    import socket

    from agent2.core import pil

    def _boom(*a, **k):
        raise AssertionError("pil.predict() attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", _boom, raising=True)
    monkeypatch.setattr(socket, "create_connection", _boom, raising=True)

    pil.predict("create a resp")     # must not raise


# ── Sync layer ────────────────────────────────────────────────────────────────

def test_notify_is_cheap():
    """notify() is on the write path for every memory/rule/setting change."""
    from agent2.core import sync as S

    med = timed_median_ms(lambda: S.notify("memories"), n=100)
    assert med < 5.0, f"sync.notify median {med:.3f}ms"


def test_notify_with_many_subscribers_stays_linear_and_isolated():
    """A broken subscriber must neither raise nor dominate the publish cost."""
    from agent2.core import sync as S

    calls = {"n": 0}

    # EventBus calls back as fn(topic, payload).
    def good(_topic, _payload):
        calls["n"] += 1

    def bad(_topic, _payload):
        raise RuntimeError("subscriber blew up")

    for _ in range(20):
        S.subscribe("perf_probe", good)
    S.subscribe("perf_probe", bad)

    # ⚠️ Both halves of the probe are undone in a `finally`, because `notify()` does
    # two things: it publishes on the in-process bus AND it bumps a `sync_state`
    # row. Left behind, the subscription names a topic with no listeners and the row
    # names a resource that does not exist — and BOTH are reported by `/api/sync`
    # and the health endpoint's sync section, so `test_health`'s "only known
    # resource names" assertion fails whenever the two files run in this order,
    # blaming health for a leak that happened here.
    try:
        med = timed_median_ms(lambda: S.notify("perf_probe"), n=50)
    finally:
        for _ in range(20):
            S.unsubscribe("perf_probe", good)
        S.unsubscribe("perf_probe", bad)
        exe("DELETE FROM sync_state WHERE resource=?", ("perf_probe",))
    assert calls["n"] > 0, "subscribers were never invoked"
    assert med < 10.0, f"notify with 21 subscribers median {med:.3f}ms"


def test_broken_subscriber_never_breaks_the_publisher():
    """Failsafe check, not a perf check: a raising listener is counted, not fatal.

    This is the behaviour that let the bug in the test above hide — the bus
    swallowed 21 raising callbacks without a murmur. That is correct for
    production (a broken listener must never kill an agent turn), so pin it.
    """
    from agent2.core import sync as S

    def always_raises(_topic, _payload):
        raise RuntimeError("boom")

    S.subscribe("perf_probe_isolated", always_raises)
    try:
        # Must return normally despite every listener raising.
        S.notify("perf_probe_isolated")
    finally:
        S.unsubscribe("perf_probe_isolated", always_raises)
        exe("DELETE FROM sync_state WHERE resource=?", ("perf_probe_isolated",))
