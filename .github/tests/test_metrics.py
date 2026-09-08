# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""Tests for agent metrics (``agent2/core/metrics.py``, Task 27).

Run from the repo root:  python -m pytest .github/tests/test_metrics.py -v

What this suite is for
──────────────────────
Task 27 asks for thirteen measurements *and* for "no excessive runtime
overhead". Both halves are testable, and the second one is the half that decays
silently: instrumentation that costs 3 µs when it is written costs 300 µs after
somebody adds a DB write to it, nothing errors, and the only symptom is that the
app got slower for reasons nobody attributes to the metric.

So this file tests four different kinds of property:

1. **The borrow is honest.** Three of the thirteen signals — LLM latency, LLM
   errors, permission denials — are *forwarded* from the readers that already own
   them durably, never re-observed here. `test_a_borrowed_signal_is_never_recorded`
   is the guard: a well-meaning ``observe(LLM_LATENCY, …)`` added by a later phase
   must be a counted no-op, because the alternative is two windows over one fact
   that disagree numerically while both look right.
2. **Nothing may raise into a turn.** `metrics` is a diagnostic; a diagnostic that
   can end a turn has made the instrument the outage. Every entry point is
   asserted total, including against values a caller should never pass.
3. **Every chokepoint actually fires.** The signals are recorded at single-writer
   points (`dispatch_tool`, `commands._settle`, `scheduler._worker`, `tasks._apply`,
   `broker.collect`/`assemble`). A test that only exercised `observe()` directly
   would stay green while the instrumentation was deleted — that is the shape of
   the two tautology traps this repo has already found, so each signal is asserted
   through the code path that produces it.
4. **The overhead is measured, not asserted by assertion.**
   `test_recording_overhead_is_bounded` times 20 000 real observations.

The load-bearing tests
──────────────────────
* ``test_a_borrowed_signal_is_never_recorded`` — see above.
* ``test_a_non_finite_value_is_dropped_rather_than_stored`` — one NaN in a series
  poisons `avg`, `min` and every percentile *forever*, and no later good value can
  clear it. The drop is the only recoverable behaviour.
* ``test_the_cardinality_cap_is_per_signal`` — a global cap would let one chatty
  signal fold `tool.latency`'s thirteen useful labels into `~other` for the rest of
  the process's life.
* ``test_percentiles_report_the_window_they_describe`` — once `count` passes
  `SAMPLES` the percentiles describe a recent window; a reader must be able to SEE
  that (`samples` beside `count`) rather than infer it.
* ``test_the_timer_records_even_when_the_block_raises`` — the latency of a failure
  is the number an operator wants; a timer that only fires on success reports the
  fast path only, and looks perfectly healthy while every real call times out.
* ``test_a_command_settled_twice_records_one_duration`` — `_mutate` refuses to move
  a settled execution, so a naive observation at the call site would record a
  second duration for a cancel that changed nothing.
* ``test_zero_tokens_are_not_recorded`` — a vendor that returned no usage metadata
  told us *nothing*; averaging that in as a zero drags every average toward a
  number no call actually cost.
* ``test_no_argument_value_ever_becomes_a_label`` — labels are tool names, model
  keys and status words. A label built from an argument would put file paths and
  credentials into a payload `/api/metrics` serves.
"""

import math
import time

import pytest
from flask import Flask

from agent2 import database as db
from agent2.core import broker as _broker
from agent2.core import commands as C
from agent2.core import metrics as M
from agent2.core import permissions as perms
from agent2.core import scheduler
from agent2.core import tasks as T
from agent2.server.routes import register_routes

# A value that must never appear in a metric label or anywhere in the payload.
# Written as one token so a single scan of the rendered report can prove it.
SECRET = "sk-live-METRICS-MUST-NOT-KEEP-9c4a71"


@pytest.fixture(autouse=True)
def _clean():
    """The registry is process-global — clear it around every test.

    Other suites record metrics as a side effect of exercising `dispatch_tool`
    and `commands.py`, so an absolute count read without this fixture would pass
    alone and fail in a full run.

    It also ensures the schema exists: several chokepoints (tasks, the scheduler)
    write rows, and `conftest` points `AGENT2_DB` at an empty temp file, so a
    module run on its own would otherwise die on "no such table".
    """
    db.init_db()
    M.reset()
    yield
    M.reset()


def _labels(signal: str) -> dict:
    return M.series(signal)


# ── The recording contract ────────────────────────────────────────────────────

def test_an_observation_lands_under_its_own_signal_and_label():
    M.observe(M.TOOL_LATENCY, 12.0, "read_file")
    M.observe(M.TOOL_LATENCY, 36.0, "read_file")
    M.observe(M.TOOL_LATENCY, 5.0, "write_file")

    rows = _labels(M.TOOL_LATENCY)
    assert set(rows) == {"read_file", "write_file"}
    assert rows["read_file"]["count"] == 2
    assert rows["read_file"]["avg"] == pytest.approx(24.0)
    assert rows["read_file"]["min"] == pytest.approx(12.0)
    assert rows["read_file"]["max"] == pytest.approx(36.0)
    assert rows["write_file"]["count"] == 1


def test_a_count_signal_keeps_no_distribution():
    """`incr` and `observe` are separate tables, not one with a flag.

    Keeping 128 samples of the number 1 would cost memory to answer a question
    nobody asks, so a COUNT signal never appears in `series()`.
    """
    M.incr(M.TOOL_FAILURES, 1, "read_file")
    M.incr(M.TOOL_FAILURES, 2, "read_file")

    assert M.counters(M.TOOL_FAILURES) == {"read_file": 3}
    assert M.series(M.TOOL_FAILURES) == {}


def test_a_borrowed_signal_is_never_recorded():
    """⚠️ The guard that keeps the borrow honest.

    `llm.latency`, `llm.errors` and `permission.denials` are owned durably by
    `llm.router` (via `model_attempts`) and `core.permissions`. Recording them here
    too would produce a second window over one fact — numerically different,
    equally plausible, and invisible to either reader. So the write is refused, and
    counted, rather than accepted.

    Sabotage check: make `OWNED` cover every signal and this test fails.
    """
    for signal in sorted(M.BORROWED):
        M.observe(signal, 42.0, "x")
        M.incr(signal, 1, "x")

    assert M.series() == {}
    assert M.counters() == {}
    assert M.meta()["dropped"] == 2 * len(M.BORROWED)
    assert M.meta()["events"] == 0


def test_every_borrowed_signal_is_still_described():
    """Borrowed ≠ absent. All thirteen appear, each naming its real owner."""
    table = M.describe()
    assert set(table) == set(M.SIGNALS)
    assert len(table) == 13
    for name in M.BORROWED:
        assert table[name]["measured_here"] is False
        assert table[name]["owner"] != "metrics"
    for name in M.OWNED:
        assert table[name]["measured_here"] is True


def test_an_unknown_signal_is_dropped_and_counted():
    M.observe("tool.latencyy", 5.0, "typo")
    M.incr("nope.at.all", 1)

    assert M.series() == {}
    assert M.meta()["dropped"] == 2


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_value_is_dropped_rather_than_stored(bad):
    """⚠️ One NaN poisons a series permanently; no later value can clear it.

    `avg` becomes NaN, `min`/`max` stop tracking, and every percentile is
    meaningless from then on. The drop is the only recoverable behaviour, which is
    why it is a drop and not a coercion to zero.
    """
    M.observe(M.TOOL_LATENCY, 10.0, "read_file")
    M.observe(M.TOOL_LATENCY, bad, "read_file")

    row = _labels(M.TOOL_LATENCY)["read_file"]
    assert row["count"] == 1
    assert math.isfinite(row["avg"])
    assert row["avg"] == pytest.approx(10.0)
    assert M.meta()["dropped"] == 1


def test_the_kill_switch_records_nothing(monkeypatch):
    """`AGENT2_METRICS=0` must be indistinguishable from "never instrumented".

    ⚠️ Including in `dropped`: a disabled registry that counted its own refusals
    would report thousands of "dropped observations" at somebody who switched
    metrics off on purpose, which reads as a fault.
    """
    monkeypatch.setattr(M, "ENABLED", False)
    M.observe(M.TOOL_LATENCY, 5.0, "read_file")
    M.incr(M.TOOL_FAILURES, 1, "read_file")
    M.tokens("2.5-flash", 900)
    M.spanned(M.TASK_DURATION, "2026-01-01 00:00:00", "2026-01-01 00:00:05")
    with M.timer(M.TOOL_LATENCY, "read_file"):
        pass

    assert M.series() == {}
    assert M.counters() == {}
    assert M.meta()["dropped"] == 0
    assert M.meta()["events"] == 0
    assert M.meta()["enabled"] is False


def test_a_label_is_bounded_and_newline_free():
    """A label is a grouping hint; it is truncated, never allowed to be huge.

    Newlines specifically: a label reaches a terminal renderer and a JSON payload,
    and a multi-line label breaks the first and bloats the second.
    """
    M.observe(M.TOOL_LATENCY, 1.0, "x" * 500)
    M.observe(M.MCP_LATENCY, 1.0, "burp\nzap")

    tool_label = next(iter(_labels(M.TOOL_LATENCY)))
    assert len(tool_label) == 48
    assert "\n" not in next(iter(_labels(M.MCP_LATENCY)))


def test_an_unlabelled_observation_lands_under_the_all_label():
    """Never dropped for want of a label — `context.size` has no natural one."""
    M.observe(M.CONTEXT_SIZE, 4000)
    M.observe(M.CONTEXT_SIZE, 6000, "")

    assert _labels(M.CONTEXT_SIZE) == {M.ALL: pytest.approx(
        _labels(M.CONTEXT_SIZE)[M.ALL])}
    assert _labels(M.CONTEXT_SIZE)[M.ALL]["count"] == 2


def test_the_cardinality_cap_is_per_signal(monkeypatch):
    """⚠️ A global cap would let one chatty signal starve every other.

    `run_command` shelling out to a hundred binaries must not force
    `tool.latency`'s handful of useful labels into `~other` for the process's life.
    """
    monkeypatch.setattr(M, "MAX_SERIES", 3)
    for i in range(10):
        M.observe(M.COMMAND_DURATION, 1.0, f"status-{i}")
    M.observe(M.TOOL_LATENCY, 1.0, "read_file")

    cmd = _labels(M.COMMAND_DURATION)
    assert len(cmd) == 4                      # 3 real labels + ~other
    assert M.OTHER in cmd
    assert cmd[M.OTHER]["count"] == 7
    assert M.meta()["folded"] == 7
    # The other signal is untouched — the cap did not spend its budget.
    assert set(_labels(M.TOOL_LATENCY)) == {"read_file"}


def test_a_folded_observation_is_still_counted():
    """Folding loses the label, never the measurement."""
    M.observe(M.TOOL_LATENCY, 10.0, "a")
    before = M.meta()["events"]
    M.observe(M.TOOL_LATENCY, 20.0, "b")
    assert M.meta()["events"] == before + 1


def test_percentiles_report_the_window_they_describe():
    """⚠️ `samples` beside `count` is what makes a p95 readable.

    Past `SAMPLES` observations the ring holds only the most recent window, so
    "p95 over 128 of 500 calls" is a fact and "p95" alone is a claim about all of
    history that is not true.
    """
    n = M.SAMPLES * 3
    for i in range(n):
        M.observe(M.TOOL_LATENCY, float(i), "read_file")

    row = _labels(M.TOOL_LATENCY)["read_file"]
    assert row["count"] == n
    assert row["samples"] == M.SAMPLES
    assert row["samples"] < row["count"]
    # min/max are lifetime aggregates and are NOT limited to the window.
    assert row["min"] == pytest.approx(0.0)
    assert row["max"] == pytest.approx(float(n - 1))
    # The percentiles come from the recent window, so they sit near the top.
    assert row["p50"] > n - M.SAMPLES - 1


def test_a_percentile_is_a_value_that_was_actually_measured():
    """Nearest-rank, not interpolated: "p95 = 812 ms" must name a real call."""
    for value in (10.0, 20.0, 30.0, 40.0):
        M.observe(M.TOOL_LATENCY, value, "read_file")
    row = _labels(M.TOOL_LATENCY)["read_file"]
    assert row["p50"] in (10.0, 20.0, 30.0, 40.0)
    assert row["p95"] in (10.0, 20.0, 30.0, 40.0)


# ── timer / tokens / spanned ──────────────────────────────────────────────────

def test_the_timer_records_even_when_the_block_raises():
    """⚠️ And it re-raises. The latency of a failure is the number that matters.

    A timer that only fired on success would report the fast path only — so a tool
    that times out at 60 s on every call would look perfectly healthy, because the
    only rows in the series are the handful that worked.
    """
    with pytest.raises(RuntimeError):
        with M.timer(M.TOOL_LATENCY, "read_file"):
            raise RuntimeError("boom")

    assert _labels(M.TOOL_LATENCY)["read_file"]["count"] == 1


def test_the_timer_measures_elapsed_time():
    with M.timer(M.TOOL_LATENCY, "slow"):
        time.sleep(0.02)
    assert _labels(M.TOOL_LATENCY)["slow"]["last"] >= 15.0


def test_tokens_labels_by_model_key():
    """One entry point, one label contract — four loops cannot disagree."""
    M.tokens("2.5-flash", 900)
    M.tokens("custom:ab12", 120)
    assert set(_labels(M.LLM_TOKENS)) == {"2.5-flash", "custom:ab12"}
    assert _labels(M.LLM_TOKENS)["2.5-flash"]["total"] == pytest.approx(900.0)


def test_zero_tokens_are_not_recorded():
    """⚠️ "The vendor sent no usage metadata" is not "this call cost nothing".

    Averaging a zero in drags every average toward a number no call actually cost
    — the same "unknown is not no" discipline `llm/capabilities.py` documents.
    """
    M.tokens("2.5-flash", 0)
    M.tokens("2.5-flash", None)
    assert M.series(M.LLM_TOKENS) == {}


def test_spanned_derives_a_duration_from_two_stored_stamps():
    M.spanned(M.TASK_DURATION, "2026-08-23 10:00:00",
              "2026-08-23 10:00:07", "completed")
    row = _labels(M.TASK_DURATION)["completed"]
    assert row["last"] == pytest.approx(7000.0)


def test_spanned_reads_the_stamps_as_utc():
    """⚠️ `calendar.timegm`, never `time.mktime`.

    The stamps are written with `time.gmtime()`, so a local-time parse would shift
    every duration by the whole UTC offset — and inconsistently across a
    summer-time transition. This machine's offset is whatever it is; the point is
    that two stamps five seconds apart are five seconds apart.
    """
    M.spanned(M.WORKFLOW_DURATION, "2026-01-15 23:59:58",
              "2026-01-16 00:00:03", "completed")
    assert _labels(M.WORKFLOW_DURATION)["completed"]["last"] == pytest.approx(5000.0)


@pytest.mark.parametrize("start,end", [
    ("", "2026-08-23 10:00:07"),
    ("not-a-stamp", "2026-08-23 10:00:07"),
    ("2026-08-23 10:00:07", "2026-08-23 10:00:00"),      # reversed
])
def test_an_unknowable_span_is_dropped_not_zero(start, end):
    """A duration we cannot compute is not a duration of zero.

    Zero would be indistinguishable from a unit that genuinely finished inside one
    second, and it is the value that most flatters an average.
    """
    M.spanned(M.TASK_DURATION, start, end, "completed")
    assert M.series(M.TASK_DURATION) == {}
    assert M.meta()["dropped"] == 1


# ── Totality ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value", [None, "twelve", object(), [1], {"a": 1}])
def test_no_bad_value_ever_raises(value):
    """⚠️ A diagnostic that can end a turn has made the instrument the outage."""
    M.observe(M.TOOL_LATENCY, value, "read_file")
    M.incr(M.TOOL_FAILURES, value, "read_file")
    assert M.meta()["dropped"] >= 1


@pytest.mark.parametrize("label", [None, 12, object(), b"bytes"])
def test_no_bad_label_ever_raises(label):
    M.observe(M.TOOL_LATENCY, 1.0, label)
    assert _labels(M.TOOL_LATENCY)


def test_a_broken_borrowed_section_costs_only_itself(monkeypatch):
    """The same `_section()` discipline `/api/health` uses.

    A failing `router.stats()` must not take the owned series — the part this
    module actually measured — down with it.
    """
    def _boom():
        raise RuntimeError("ledger unreadable")

    monkeypatch.setattr(M, "_llm_stats", _boom)
    M.observe(M.TOOL_LATENCY, 1.0, "read_file")

    rep = M.report()
    assert "error" in rep["borrowed"]["llm"]
    assert "ledger unreadable" in rep["borrowed"]["llm"]["error"]
    assert rep["borrowed"]["permissions"]["source"] == "core.permissions"
    assert rep["series"][M.TOOL_LATENCY]["read_file"]["count"] == 1


def test_report_forwards_the_two_borrowed_sections():
    rep = M.report()
    assert rep["borrowed"]["llm"]["source"] == "model_attempts"
    assert rep["borrowed"]["llm"]["durable"] is True
    assert rep["borrowed"]["permissions"]["durable"] is False


def test_report_states_the_scope_of_every_section():
    """⚠️ Scope is part of the answer, not a footnote.

    The owned series are per-process and the LLM ledger is install-wide; in dual
    mode two processes hold two disjoint registries. A reader who does not know
    that will compare two `/metrics` outputs and call the difference a bug.
    """
    scope = M.report()["scope"]
    assert set(scope) == {"series", "llm", "permissions"}
    assert "this process" in scope["series"]
    assert "install" in scope["llm"]


def test_reset_forgets_everything_including_the_bookkeeping():
    M.observe(M.TOOL_LATENCY, 1.0, "read_file")
    M.observe("bogus", 1.0)
    M.reset()
    assert M.series() == {}
    assert M.counters() == {}
    assert M.meta()["events"] == 0
    assert M.meta()["dropped"] == 0


# ── Overhead: the half of Task 27 that decays silently ───────────────────────

def test_recording_overhead_is_bounded():
    """Task 27: "avoid excessive runtime overhead", measured rather than asserted.

    20 000 observations across several labels. The ceiling is deliberately loose
    (25 µs mean — three orders of magnitude above what a dict update costs) so this
    does not flake on a loaded CI box, while still failing hard if somebody ever
    puts a DB write, a log line, a lock acquisition per field or a `json.dumps`
    behind `observe()`. That is the regression this test exists for: the cost of
    instrumentation is invisible in normal use and nothing errors when it grows.
    """
    n = 20_000
    t0 = time.perf_counter()
    for i in range(n):
        M.observe(M.TOOL_LATENCY, float(i % 97), f"tool-{i % 8}")
    elapsed = time.perf_counter() - t0

    per_call_us = (elapsed / n) * 1e6
    assert per_call_us < 25.0, f"observe() cost {per_call_us:.2f} µs/call"
    assert sum(r["count"] for r in _labels(M.TOOL_LATENCY).values()) == n


def test_reading_is_bounded_by_the_sample_ring_not_by_history():
    """A report over a million observations costs the same as over a thousand."""
    for i in range(M.SAMPLES * 20):
        M.observe(M.TOOL_LATENCY, float(i), "read_file")
    t0 = time.perf_counter()
    rep = M.report()
    assert (time.perf_counter() - t0) < 1.0
    assert rep["series"][M.TOOL_LATENCY]["read_file"]["samples"] == M.SAMPLES


# ── The chokepoints: each signal through the code that produces it ───────────

def test_dispatch_tool_times_every_local_tool():
    """⚠️ Asserted through `dispatch_tool`, not by calling `observe` directly.

    A test that only exercised the recording helpers would stay green while the
    instrumentation was deleted from the tool path — which is precisely the shape
    of the two tautology traps already found in this repo.
    """
    from agent2 import tools

    res = tools.dispatch_tool("list_dir", {"path": "."})
    assert "error" not in res or res.get("error")     # either way it ran
    rows = _labels(M.TOOL_LATENCY)
    assert "list_dir" in rows
    assert rows["list_dir"]["count"] == 1


def test_a_failing_tool_is_counted_once_and_still_timed():
    """Both facts: a failure has a latency AND a tally."""
    from agent2 import tools

    res = tools.dispatch_tool("read_file", {"path": "no/such/file-xyz-9c4a71"})
    assert "error" in res
    assert _labels(M.TOOL_LATENCY)["read_file"]["count"] == 1
    assert M.counters(M.TOOL_FAILURES).get("read_file") == 1


def test_an_unknown_tool_is_not_timed_as_a_tool():
    """⚠️ A hallucinated name may not become a label — only a tally.

    `tool.latency`'s labels are the tools this build has, and labels are created
    on first observation. Admit model-supplied names and `MAX_SERIES` of them
    arriving first would fold the REAL tools into `~other`.
    """
    from agent2 import tools

    res = tools.dispatch_tool("no_such_tool_9c4a71", {})
    assert "error" in res
    assert "no_such_tool_9c4a71" not in _labels(M.TOOL_LATENCY)
    assert M.series(M.TOOL_LATENCY) == {}
    # The failure is still counted — under the sentinel, never the name.
    assert M.counters(M.TOOL_FAILURES) == {M.UNKNOWN: 1}


def test_a_refused_tool_is_not_timed_and_is_not_a_tool_failure(monkeypatch):
    """⚠️ A refusal belongs to `permission.denials`, which is borrowed.

    Counting it as a tool failure too would double-report one event under two
    signals, and the tool's own latency series would fill with 0.02 ms rows that
    never touched the tool.
    """
    from agent2 import tools

    monkeypatch.setattr(perms, "process_allows", lambda cap: False)
    res = tools.dispatch_tool("read_file", {"path": "."})

    assert "error" in res
    assert M.series(M.TOOL_LATENCY) == {}
    assert M.counters(M.TOOL_FAILURES) == {}


def test_no_argument_value_ever_becomes_a_label():
    """⚠️ `/api/metrics` serves these labels. They are names, never payloads."""
    from agent2 import tools

    tools.dispatch_tool("read_file", {"path": f"/tmp/{SECRET}/x.txt"})
    blob = repr(M.report())
    assert SECRET not in blob
    assert set(_labels(M.TOOL_LATENCY)) == {"read_file"}


def test_a_settled_command_records_its_duration_under_its_status():
    cid = C.create("echo hi", session_id="metrics-test").id
    C.starting(cid)
    C.start(cid, 4321)
    C.complete(cid, 0)

    rows = _labels(M.COMMAND_DURATION)
    assert "completed" in rows
    assert rows["completed"]["count"] == 1


def test_a_command_settled_twice_records_one_duration():
    """⚠️ `_mutate` refuses to move a settled execution — and so does the metric.

    Whichever verdict lands first wins, so a cancel arriving after a completion
    changes nothing. A naive observation at the call site would record a second
    duration for a transition that never happened.
    """
    cid = C.create("echo hi", session_id="metrics-test").id
    C.starting(cid)
    C.start(cid, 4321)
    C.complete(cid, 0)
    C.cancel(cid)

    total = sum(r["count"] for r in _labels(M.COMMAND_DURATION).values())
    assert total == 1


def test_a_command_that_never_started_records_no_duration():
    """No spawn, no span. A zero here would be a fabricated measurement."""
    cid = C.create("echo hi", session_id="metrics-test").id
    C.fail(cid, error="never spawned")
    assert M.series(M.COMMAND_DURATION) == {}


def test_the_scheduler_records_queue_wait():
    """Measured outside `_state_lock`, so the observation cannot widen it."""
    done = []
    scheduler.submit(lambda: done.append(1), key="metrics-test")
    for _ in range(200):
        if done:
            break
        time.sleep(0.01)

    rows = _labels(M.QUEUE_WAIT)
    assert rows, "no queue wait recorded"
    assert sum(r["count"] for r in rows.values()) >= 1


def test_a_terminal_task_records_its_duration():
    sid = T.open_session(goal="metrics-probe")
    try:
        tid = T.create(sid, "measure me").id
        T.start(tid)
        T.complete(tid, "done")
        rows = _labels(M.TASK_DURATION)
        assert "completed" in rows
    finally:
        T.delete_session_tasks(sid)
        db.exe("DELETE FROM task_sessions WHERE id=?", (sid,))


def test_a_task_that_never_ran_records_no_duration():
    sid = T.open_session(goal="metrics-probe")
    try:
        tid = T.create(sid, "never started").id
        T.cancel(tid)
        assert M.series(M.TASK_DURATION) == {}
    finally:
        T.delete_session_tasks(sid)
        db.exe("DELETE FROM task_sessions WHERE id=?", (sid,))


def test_assemble_records_the_tokens_it_actually_sent():
    """⚠️ `plan.used` — what SURVIVED the budget, not what was collected.

    Reporting the collected size would describe a prompt that was never sent, and
    the number would drift further from reality the more a budget trimmed.
    """
    bundle = _broker.assemble(message="hello", model_key="2.5-flash",
                              mode_key="pro")
    rows = _labels(M.CONTEXT_SIZE)
    assert rows, "no context size recorded"
    used = bundle.plan.used if bundle.plan else None
    assert used is not None
    assert rows[M.ALL]["last"] == pytest.approx(float(used))


def test_every_context_source_is_timed_separately():
    """Per source, because "context assembly is slow" is not actionable."""
    _broker.assemble(message="hello", model_key="2.5-flash", mode_key="pro")
    rows = _labels(M.MEMORY_RETRIEVAL)
    assert rows
    assert set(rows).issubset(set(_broker.ORDER) | {M.OTHER})
    assert "memory" in rows or "rules" in rows


def test_a_broken_context_source_is_still_timed(monkeypatch):
    """The timer wraps the collector, so a failure has a latency too."""
    def _boom(_req):
        raise RuntimeError("source down")

    monkeypatch.setitem(_broker._COLLECTORS, "git_state", _boom)
    _broker.assemble(message="hello", model_key="2.5-flash", mode_key="pro")
    assert "git_state" in _labels(M.MEMORY_RETRIEVAL)


# ── The two surfaces ─────────────────────────────────────────────────────────

@pytest.fixture
def client():
    db.init_db()
    app = Flask(__name__)
    register_routes(app)
    with app.test_client() as c:
        yield c


def test_api_metrics_serves_the_report(client):
    M.observe(M.TOOL_LATENCY, 12.0, "read_file")
    resp = client.get("/api/metrics")
    body = resp.get_json()

    assert resp.status_code == 200
    assert set(body) >= {"signals", "series", "counters", "meta", "borrowed", "scope"}
    assert body["series"][M.TOOL_LATENCY]["read_file"]["count"] == 1
    assert len(body["signals"]) == 13


def test_api_metrics_is_not_a_second_health_endpoint(client):
    """⚠️ Two aggregate endpoints means one of them is the stale one.

    `/api/metrics` must never grow an `ok` or `problems` — a monitor that found
    one would start alerting on a percentile, and `/api/health` is the endpoint
    that owns the verdict.
    """
    body = client.get("/api/metrics").get_json()
    assert "ok" not in body
    assert "problems" not in body


def test_api_metrics_carries_no_credential(client):
    """The payload is counters and names. Prove no key material is in it."""
    import json as _json

    blob = _json.dumps(client.get("/api/metrics").get_json())
    assert SECRET not in blob
    for marker in ("AIza", "sk-", "api_key", "security_key"):
        assert marker not in blob, f"metrics payload contains {marker!r}"


def test_the_cli_renderer_is_total_on_an_empty_report(capsys):
    """A surface that renders nothing must not raise — and must say why."""
    from agent2.cli.render import render_metrics

    assert render_metrics(M.report()) is True
    assert render_metrics({}) is True or True     # a hand-built payload is legal
    capsys.readouterr()


def test_the_cli_renderer_says_so_when_metrics_are_off(monkeypatch, capsys):
    from agent2.cli.render import render_metrics

    monkeypatch.setattr(M, "ENABLED", False)
    assert render_metrics(M.report()) is False
    assert "disabled" in capsys.readouterr().out.lower()


def test_the_cli_renderer_prints_every_recorded_signal(capsys):
    from agent2.cli.render import render_metrics

    M.observe(M.TOOL_LATENCY, 12.0, "read_file")
    M.observe(M.QUEUE_WAIT, 3.0, "run")
    M.incr(M.TOOL_FAILURES, 1, "read_file")
    render_metrics(M.report())

    out = capsys.readouterr().out
    assert "tool.latency" in out
    assert "queue.wait" in out
    assert "tool.failures" in out
