# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2.core.metrics
───────────────────
THE measurement surface (Task 27) — "how long did that take, how often does it
fail, how big was the prompt". One registry, thirteen declared signals, read by
`GET /api/metrics`, `/metrics` in the CLI and `agent2 status`.

What this is NOT
  Not a second copy of anything already recorded. Agent2 already owns three
  durable measurements and this module **borrows** them rather than re-deriving
  them, because a metric that disagrees with its own source is worse than no
  metric — both numbers look right and nothing says which window each covers:

    * LLM latency / calls / failures  → `llm.router.stats()` (`model_attempts`)
    * permission denials              → `core.permissions.counters()`
    * command / task / MCP *state*    → `commands.snapshot()`, `tasks`, `registry`

  `report()` is the one place that composition happens. Everything under
  `series()` and `counters()` is a fact **nobody else measures**.

⚠️ IN-PROCESS AND IN-MEMORY, ON PURPOSE — AND THAT IS A DOCUMENTED LIMIT, NOT AN
OVERSIGHT. Task 27 says *avoid excessive runtime overhead*, and the signals here
fire at output-line and per-tool-call rate: a durable row per observation is
precisely the "diagnostic that causes the outage" this repo refuses to ship
(`router.record_attempt`'s docstring says the same about a per-call INSERT, and
it writes one row per *model* call, which is thousands of times rarer). So an
observation costs one dict lookup, four float writes and a bounded `deque.append`
under an uncontended lock — no allocation beyond the sample, no I/O, no notify.

The consequence is the same one `core/diffs.DiffStore` documents: dual mode is two
processes over one DB, so the CLI half and the web half hold **two disjoint
registries** and "since when" means "since this process started". `report()` says
so in `scope`, and the three borrowed sections above are exactly the ones that
*are* process-wide, because their owners persist them. A reader who needs
cross-process totals reads those.

⚠️ NOTHING HERE MAY RAISE INTO A TURN. Every public function is total: a metric
that cannot be recorded is dropped and counted in `dropped`, and a report that
cannot be built comes back with an `error` field rather than propagating. That is
why this file carries a written BLE001/S110 exemption — an *accounting* helper
must never be the reason a tool call fails.

⚠️ CARDINALITY IS CAPPED (`MAX_SERIES`). Labels are enum-ish by contract — a tool
name, a model key, a server key, a capability, a task status — never a path, a
command line, an argument or a chat id. The cap is the backstop for the day a
caller forgets: past `MAX_SERIES` distinct series a new label folds into
`~other` and `folded` counts it, so the registry cannot grow without bound and
the report says that it stopped distinguishing. ⚠️ The fold is per *name*, not
global, so one chatty signal cannot starve the other twelve of their own labels.

⚠️ NO CONTENT, EVER. A series holds numbers and a label; there is no field a
command line, a file's bytes or a prompt could be written to. That is not
tidiness — `/api/metrics` sits behind the same guard as the rest of `/api/*`, but
`/api/health` (which links to it) is loopback-anonymous, and the moment a metric
carries an argument the two endpoints' data classifications disagree.

Overhead, measured
  `test_metrics.py::test_recording_overhead_is_bounded` runs 20 000 observations
  and asserts the mean cost of one is under 25 µs — roughly two orders of
  magnitude below the cheapest thing being measured (a `read_file` tool call).
  Recording can also be switched off entirely with `AGENT2_METRICS=0`, which
  turns every entry point into a single boolean test, because "an optimization
  layered over a working default" cuts both ways: the app must run identically
  with the measurement gone.

Usage
    from agent2.core import metrics
    with metrics.timer(metrics.TOOL_LATENCY, "read_file"):
        ...
    metrics.incr(metrics.TOOL_FAILURES, label="read_file")
    metrics.observe(metrics.CONTEXT_SIZE, 4200)
    metrics.report()          # what the surfaces render
"""

from __future__ import annotations

import calendar
import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from agent2 import config as _cfg

# ── Tunables ──────────────────────────────────────────────────────────────────
# ⚠️ DECLARED IN `config.py`, read through here. Every tunable in this app lives
# in one file; a second `os.environ.get("AGENT2_METRICS")` would be a second
# default, and the one nobody edits is the one that wins in the surface nobody
# checked.

#: Master switch. OFF makes every entry point a single boolean test — the app
#: behaves exactly as it did before this module existed, which is the property
#: every optimization in this repo has to keep.
ENABLED = _cfg.METRICS_ENABLED

#: Samples kept per series for percentiles. A ring buffer, so memory is
#: O(series × SAMPLES) and bounded; percentiles are computed at *read* time so
#: the hot path never sorts.
SAMPLES = _cfg.METRICS_SAMPLES

#: Distinct labels allowed per signal before new ones fold into `~other`.
MAX_SERIES = _cfg.METRICS_MAX_SERIES

#: The label a folded series lands under. Chosen to sort last and to be
#: obviously not a real tool/model name.
OTHER = "~other"

#: The label a *name we do not recognise* lands under — a model asking for a tool
#: this build does not have. ⚠️ DELIBERATELY NOT `OTHER`: a fold means "a real
#: label ran out of room", this means "that was never a tool", and one label for
#: both facts makes a hallucinating model look like a cardinality problem.
#: It exists so the tally can stay honest without admitting model-supplied text
#: into a label vocabulary — see `tools.dispatch_tool`.
UNKNOWN = "~unknown"

#: Label used when a caller gives none. A signal always has at least one series,
#: so a report never has to distinguish "no label" from "no data".
ALL = "*"

# ── The thirteen signals (Task 27 enumerates exactly these) ───────────────────
# ⚠️ THE TABLE IS THE DECLARATION. A signal has a name, a unit and an owner, and
# `describe()` reports all thirteen whether or not anything has been recorded —
# an absent row and a zero row are different facts, and a surface that only
# renders what it has seen makes "nothing happened yet" look like "not
# implemented".

LLM_LATENCY = "llm.latency"
LLM_TOKENS = "llm.tokens"
LLM_ERRORS = "llm.errors"
TOOL_LATENCY = "tool.latency"
TOOL_FAILURES = "tool.failures"
COMMAND_DURATION = "command.duration"
QUEUE_WAIT = "queue.wait"
TASK_DURATION = "task.duration"
WORKFLOW_DURATION = "workflow.duration"
MEMORY_RETRIEVAL = "memory.retrieval"
CONTEXT_SIZE = "context.size"
MCP_LATENCY = "mcp.latency"
PERMISSION_DENIALS = "permission.denials"

#: Units. `ms` and `tokens` are observations (a distribution); `count` is a
#: counter. The unit is what tells a renderer whether "p95" means anything.
MS = "ms"
TOKENS = "tokens"
COUNT = "count"

#: signal → (unit, owner, what one observation means)
#: `owner` is "metrics" for a fact this module measures, or the module that owns
#: it durably — the borrowed three. A renderer uses it to say where a number came
#: from, which is the whole reason the borrow is safe.
SIGNALS: dict[str, tuple[str, str, str]] = {
    LLM_LATENCY:        (MS, "llm.router", "one model call, wall clock"),
    LLM_TOKENS:         (TOKENS, "metrics", "tokens billed for one model call"),
    LLM_ERRORS:         (COUNT, "llm.router", "one failed model call, by kind"),
    TOOL_LATENCY:       (MS, "metrics", "one local tool call"),
    TOOL_FAILURES:      (COUNT, "metrics", "one tool call that returned an error"),
    COMMAND_DURATION:   (MS, "metrics", "one shell command, spawn to settle"),
    QUEUE_WAIT:         (MS, "metrics", "how long a turn sat in the scheduler queue"),
    TASK_DURATION:      (MS, "metrics", "one task, RUNNING to terminal"),
    WORKFLOW_DURATION:  (MS, "metrics", "one workflow run, start to finish"),
    MEMORY_RETRIEVAL:   (MS, "metrics", "building one context source's block"),
    CONTEXT_SIZE:       (TOKENS, "metrics", "estimated prompt tokens sent for a turn"),
    MCP_LATENCY:        (MS, "metrics", "one MCP tool call, per server"),
    PERMISSION_DENIALS: (COUNT, "core.permissions", "one refused capability"),
}

#: The signals this module measures itself. The rest are reported by `report()`
#: from their owner. ⚠️ Recording into a borrowed signal is a no-op and is
#: counted in `dropped` — that is the guard that keeps the borrow honest, because
#: a well-meaning `observe(LLM_LATENCY, …)` added by a later phase is exactly how
#: the second, drifting copy would appear.
BORROWED = frozenset((LLM_LATENCY, LLM_ERRORS, PERMISSION_DENIALS))
OWNED = frozenset(k for k in SIGNALS if k not in BORROWED)


# ── Storage ───────────────────────────────────────────────────────────────────

@dataclass
class _Series:
    """One (signal, label) pair's aggregate. Fixed size, O(1) to update."""
    count: int = 0
    total: float = 0.0
    low: float = 0.0
    high: float = 0.0
    last: float = 0.0
    samples: deque = field(default_factory=lambda: deque(maxlen=SAMPLES))

    def add(self, value: float) -> None:
        if self.count == 0:
            self.low = self.high = value
        elif value < self.low:
            self.low = value
        elif value > self.high:
            self.high = value
        self.count += 1
        self.total += value
        self.last = value
        self.samples.append(value)


_lock = threading.Lock()
#: signal → label → _Series
_data: dict[str, dict[str, _Series]] = {}
#: signal → label → int, for COUNT signals (no distribution to keep)
_counts: dict[str, dict[str, int]] = {}
_started_mono = time.monotonic()
_started_at = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
_meta = {"events": 0, "dropped": 0, "folded": 0}


def _label(label: str | None) -> str:
    """Normalise a label. Bounded length, no newlines, never empty.

    ⚠️ Truncated rather than rejected: a label is an ordering/grouping hint and
    losing it must never cost the observation. 48 characters holds every tool
    name, model key and capability in the app with room to spare.
    """
    text = str(label if label is not None else "").strip().replace("\n", " ")
    return text[:48] or ALL


def _bucket(table: dict, signal: str, label: str):
    """Return (labels_dict, label) after applying the per-signal cardinality cap.

    ⚠️ The cap is per SIGNAL. A global cap would let one chatty signal (say a
    `run_command` that shells out to a hundred different binaries) consume the
    budget and force `tool.latency` — thirteen labels, all of them useful — into
    `~other` for the rest of the process's life.
    """
    labels = table.setdefault(signal, {})
    if label in labels or len(labels) < MAX_SERIES:
        return labels, label
    _meta["folded"] += 1
    return labels, OTHER


# ── Recording ─────────────────────────────────────────────────────────────────

def observe(signal: str, value: float, label: str | None = None) -> None:
    """Record one measurement of *signal*. Never raises.

    Dropped (and counted) when metrics are off, the signal is unknown, the signal
    is BORROWED, or the value is not a finite number. All four are silent to the
    caller on purpose: an instrumentation mistake must not change what the
    instrumented code does, and `report()["dropped"]` is where it shows up.
    """
    if not ENABLED:
        return
    try:
        if signal not in OWNED:
            with _lock:
                _meta["dropped"] += 1
            return
        num = float(value)
        if not math.isfinite(num):                               # NaN / ±inf
            with _lock:
                _meta["dropped"] += 1
            return
        name = _label(label)
        with _lock:
            labels, key = _bucket(_data, signal, name)
            series = labels.get(key)
            if series is None:
                series = labels[key] = _Series()
            series.add(num)
            _meta["events"] += 1
    except Exception:
        try:
            with _lock:
                _meta["dropped"] += 1
        except Exception:
            pass


def incr(signal: str, n: int = 1, label: str | None = None) -> None:
    """Add *n* to a COUNT signal. Never raises.

    Separate from `observe` because a count has no distribution: keeping 128
    samples of the number 1 would cost memory to answer a question nobody asks.
    """
    if not ENABLED:
        return
    try:
        if signal not in OWNED:
            with _lock:
                _meta["dropped"] += 1
            return
        step = int(n)
        name = _label(label)
        with _lock:
            labels, key = _bucket(_counts, signal, name)
            labels[key] = labels.get(key, 0) + step
            _meta["events"] += 1
    except Exception:
        try:
            with _lock:
                _meta["dropped"] += 1
        except Exception:
            pass


def tokens(model: str, count: int) -> None:
    """Record the tokens one model call was billed for. Never raises.

    ⚠️ A NAMED ENTRY POINT RATHER THAN A BARE `observe(LLM_TOKENS, …)` BECAUSE THE
    LABEL IS THE CONTRACT. Four agent loops count tokens — `agent.py`,
    `llm/provider_agent.py` and both CLI loops — and each has a differently-named
    local for the model (`model_key`, `model`, `mid`, `provider["model_id"]`). Four
    `observe` calls would be four chances to label the same signal with a chat id,
    a provider name or nothing at all, and the registry would then hold four series
    that cannot be added up. Here there is one answer: the model key, always.

    Zero is skipped, not recorded. A vendor that returned no usage metadata told us
    *nothing*, and averaging that in as a zero would drag every average toward a
    number no call actually cost — the same "unknown is not no" discipline
    `llm/capabilities.py` documents.
    """
    try:
        n = int(count or 0)
    except Exception:
        n = 0
    if n > 0:
        observe(LLM_TOKENS, n, model or ALL)


def spanned(signal: str, start: str, end: str = "",
            label: str | None = None) -> None:
    """Record a duration derived from two **stored UTC stamps**. Never raises.

    For the two signals whose span outlives the process that started it — a task
    and a workflow — where a monotonic clock is not merely unnecessary but wrong:
    `time.monotonic()` is meaningless across a restart, and these rows are
    designed to survive one. The row's own `started_at`/`completed_at` are the ONE
    declaration of when the unit ran, so the duration is derived from them rather
    than from a second stamp this module would have to store and keep in step.

    ⚠️ The inherited resolution is ONE SECOND, because that is what
    `core/tasks._now()` writes. A unit that starts and finishes inside the same
    second therefore reports 0 ms, and that is the honest answer for this signal —
    adding a millisecond clock here would be a second, disagreeing account of a
    span the row already describes. Sub-second work is what `tool.latency` and
    `command.duration` measure, and both use `perf_counter`.

    An unparseable or missing stamp means "we do not know how long that took",
    which is not zero: nothing is recorded, and `dropped` counts it.
    """
    if not ENABLED:
        return
    try:
        t0 = _epoch(start)
        t1 = _epoch(end) if end else time.time()
        if not t0 or not t1 or t1 < t0:
            with _lock:
                _meta["dropped"] += 1
            return
        observe(signal, (t1 - t0) * 1000.0, label)
    except Exception:
        try:
            with _lock:
                _meta["dropped"] += 1
        except Exception:
            pass


def _epoch(stamp: str) -> float:
    """Parse a `core/tasks._now()` stamp. 0.0 when it cannot be read.

    ⚠️ `calendar.timegm`, never `time.mktime`: the stamps are written with
    `time.gmtime()`, so a local-time parse would shift every duration by the whole
    UTC offset — and in a summer-time transition, inconsistently. (`recovery._epoch`
    carries the same note for the same format; it answers a different question — an
    absolute age against now — and lives above this module in the import graph.)
    """
    try:
        return float(calendar.timegm(time.strptime(str(stamp),
                                                   "%Y-%m-%d %H:%M:%S")))
    except (TypeError, ValueError):
        return 0.0


class timer:            # lower-case on purpose: it reads as a verb at the call site
    """Context manager timing a block in milliseconds.

        with metrics.timer(metrics.TOOL_LATENCY, "read_file"):
            ...

    ⚠️ It records on the way out **whether or not the block raised**, because the
    latency of a failure is the number an operator actually wants: a tool that
    times out at 60 s and one that errors in 2 ms are the same row otherwise. The
    exception is never swallowed — `__exit__` returns None.

    ⚠️ `time.perf_counter` and not `time.time`: a clock adjustment mid-call would
    otherwise produce a negative duration, and a negative latency poisons every
    aggregate that reads it (`low` especially, which would then never recover).
    """

    __slots__ = ("_t0", "label", "signal")

    def __init__(self, signal: str, label: str | None = None) -> None:
        self.signal = signal
        self.label = label
        self._t0 = time.perf_counter() if ENABLED else 0.0

    def __enter__(self) -> timer:
        return self

    def __exit__(self, *_exc) -> None:
        if ENABLED:
            observe(self.signal, (time.perf_counter() - self._t0) * 1000.0,
                    self.label)


# ── Reading ───────────────────────────────────────────────────────────────────

def _percentile(sorted_values: list[float], fraction: float) -> float:
    """Nearest-rank percentile over an already-sorted list.

    Nearest-rank rather than interpolating: every value reported is a value that
    was actually measured, so "p95 = 812 ms" names a real call. With at most
    `SAMPLES` points the difference from a linear interpolation is noise, and a
    synthesised number invites the question "which call was that?" — which has no
    answer.
    """
    if not sorted_values:
        return 0.0
    idx = round(fraction * (len(sorted_values) - 1))
    return sorted_values[max(0, min(idx, len(sorted_values) - 1))]


def _render(series: _Series) -> dict:
    values = sorted(series.samples)
    return {
        "count": series.count,
        "total": round(series.total, 3),
        "avg": round(series.total / series.count, 3) if series.count else 0.0,
        "min": round(series.low, 3),
        "max": round(series.high, 3),
        "last": round(series.last, 3),
        "p50": round(_percentile(values, 0.50), 3),
        "p95": round(_percentile(values, 0.95), 3),
        "samples": len(values),
    }


def series(signal: str = "") -> dict:
    """Rendered observation series — one signal's, or every signal's.

    Percentiles are computed here, off the hot path, from the bounded sample
    ring. `samples` is reported beside `count` on purpose: once `count` exceeds
    `SAMPLES` the percentiles describe the most recent window, not all of
    history, and a reader must be able to see that rather than infer it.
    """
    with _lock:
        table = {s: dict(labels) for s, labels in _data.items()}
    if signal:
        return {lab: _render(sv) for lab, sv in (table.get(signal) or {}).items()}
    return {s: {lab: _render(sv) for lab, sv in labels.items()}
            for s, labels in table.items()}


def counters(signal: str = "") -> dict:
    """Rendered COUNT signals — one signal's labels, or every signal's."""
    with _lock:
        table = {s: dict(labels) for s, labels in _counts.items()}
    if signal:
        return dict(table.get(signal) or {})
    return table


def describe() -> dict:
    """The signal table — name, unit, owner, meaning. Total, and data-free.

    ⚠️ Reports all thirteen signals whether or not anything has been recorded.
    "Nothing has happened yet" and "this build does not measure that" are
    different facts, and a report built only from what the registry happens to
    hold cannot tell them apart.
    """
    return {name: {"unit": unit, "owner": owner, "means": means,
                   "measured_here": name in OWNED}
            for name, (unit, owner, means) in SIGNALS.items()}


def meta() -> dict:
    """Registry bookkeeping: how many events, how many dropped, how many folded."""
    with _lock:
        out = dict(_meta)
    out.update({
        "enabled": ENABLED,
        "since": _started_at,
        "uptime_sec": round(time.monotonic() - _started_mono, 1),
        "max_series": MAX_SERIES,
        "samples_per_series": SAMPLES,
        "series_count": sum(len(v) for v in _data.values()) + sum(
            len(v) for v in _counts.values()),
    })
    return out


def snapshot() -> dict:
    """This process's own registry: signals, series, counters and bookkeeping.

    No borrowed sections — `report()` is where those are joined, so a caller that
    wants "what did *this* process measure" can ask for exactly that without
    paying three queries for it.
    """
    return {"signals": describe(), "series": series(), "counters": counters(),
            "meta": meta()}


def _borrowed() -> dict:
    """The three durable measurements this module deliberately does not keep.

    ⚠️ EACH SECTION IS ITS OWNER'S OUTPUT, FORWARDED VERBATIM — never re-derived
    and never merged into `series`. `llm.router.stats()` reads `model_attempts`,
    which is bounded and cross-process; `permissions.counters()` is this
    process's allow/deny tally and says so. Recomputing either here would produce
    a number that disagrees with the endpoint that owns it, over a window nothing
    in the payload describes.

    Each is isolated: a borrowed section that raises lands as `{"error": …}` and
    costs only itself, the same discipline `/api/health`'s `_section()` uses.
    """
    out: dict = {}
    for name, load in (("llm", _llm_stats), ("permissions", _perm_counters)):
        try:
            out[name] = load()
        except Exception as exc:
            out[name] = {"error": f"{type(exc).__name__}: {exc}"[:200]}
    return out


def _llm_stats() -> dict:
    from agent2.llm import router
    got = dict(router.stats())
    got["source"] = "model_attempts"
    got["durable"] = True
    return got


def _perm_counters() -> dict:
    from agent2.core import permissions
    got = dict(permissions.counters())
    got["source"] = "core.permissions"
    got["durable"] = False
    return got


def report() -> dict:
    """THE metrics payload — what `/api/metrics` and `/metrics` both render.

    ⚠️ ONE COMPOSITION, IN ONE PLACE. The CLI cannot call the HTTP endpoint (the
    web half may not be running, and in dual mode it is a different process), so
    both surfaces build the report from here. Two compositions would mean the
    browser and the terminal answering "how is the agent doing" with different
    sets of numbers, each self-consistent — the failure this repo's
    one-declaration rule exists to prevent.

    `scope` is part of the payload because it is part of the answer: the owned
    series cover **this process since it started**, and the borrowed `llm`
    section covers the whole install. Leaving that to the reader is how a dual
    install's two `/metrics` outputs get compared and called a bug.
    """
    out = snapshot()
    out["borrowed"] = _borrowed()
    out["scope"] = {
        "series": "this process, since it started",
        "llm": "the whole install (model_attempts is durable and bounded)",
        "permissions": "this process, since it started",
    }
    return out


def reset() -> None:
    """Forget everything. Tests and `/metrics reset`; never on the hot path."""
    with _lock:
        _data.clear()
        _counts.clear()
        _meta.update({"events": 0, "dropped": 0, "folded": 0})
        globals()["_started_mono"] = time.monotonic()
        globals()["_started_at"] = time.strftime("%Y-%m-%d %H:%M:%S",
                                                 time.gmtime())
