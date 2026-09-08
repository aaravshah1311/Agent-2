# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2.core.verify
──────────────────
THE shared verification system — Phase D3 (D3.25), and PART 10's *"ONE SHARED
VERIFICATION SYSTEM"*. It answers one question, for anything that runs:

    Something said it finished. Does the durable record agree?

⚠️ **THIS EXISTS BECAUSE "DONE" IS NOT VERIFICATION.** The spec states it twice —
*"'Done' is not verification"* and *"Never trust: Worker says Done → Agent2 says
Done"* — and both sentences describe the same failure: `agent_tasks.status` is
written by the thing being judged. A node marked COMPLETED whose only write failed,
whose command exited 1, or whose tool call never returned is *indistinguishable*
from a node that genuinely worked, and every surface downstream repeats the claim
to a human as fact. Nothing here re-runs, repairs or hides anything; it reads what
was already recorded and says whether the two stories match.

⚠️ **IT VERIFIES A TASK ROW, WHICH IS WHY IT KNOWS NOTHING ABOUT WORKFLOWS.**
`core/dag/store.py` made every graph node an `agent_tasks` row, so a workflow node,
a dynamic-workflow node and an UltraCode node are one shape here — and this module
imports neither `core.dag` nor `core.workflow`, which is what stops the spec's
forbidden `if workflow:` from ever being needed. The domain-shaped entry point is
`workflow.runner.verify()`, a wrapper, exactly as `workflow.graph.validate` wraps
`dag.validate`.

⚠️ **READ-ONLY, AND IT DOES NOT RE-STAT DISK.** The question is *"did it happen"*,
not *"is it still there"*: a file a later step legitimately replaced or removed
would read as a contradiction, which is a wrong answer, and re-measuring N paths
per node would put filesystem I/O inside a verification loop. `execstate` already
recorded a `pre`/`post` digest pair **at the moment of the call** — the one
measurement taken while the truth was still knowable — so this module compares
what was recorded and never takes a second reading. `execstate.digests()` exists
for a caller that genuinely wants today's disk; that is a different question and it
belongs to `recovery.classify`, not here.

⚠️ **PROBLEMS ALONE DECIDE A VERDICT; WARNINGS NEVER DO.** `core/health.py`'s rule,
and for its reason: every configuration that trips a bad verdict spends the meaning
of that verdict. So an *unconfirmed* unit — one that claims success having recorded
nothing either way — is a **warning**, never a contradiction. Promoting it would
make a node whose whole job was to read and reason indistinguishable from one that
failed, and that false negative would fire on every single run.

⚠️ **AND THE VERDICT IS DERIVED FROM THE SAME LINES THE CHECKS PRODUCED**, never
re-tested from the data — `health._rows()`'s discipline — so a `Check` reporting a
problem while its `Finding` reads `confirmed` is unrepresentable rather than
unlikely.

Five verdicts, and the third and fourth are the point:

| Verdict | Meaning |
|---|---|
| `V_OPEN` | not settled — nothing has been claimed yet, so there is nothing to check |
| `V_UNSUCCESSFUL` | settled, and it says so (failed · cancelled · skipped) — nothing to contradict |
| `V_CONFIRMED` | claims success, and the record agrees |
| `V_CONTRADICTED` | claims success, and the record **disagrees** |
| `V_UNCONFIRMED` | claims success, and nothing was recorded either way |

`TRUSTED` holds exactly one of them. A caller asking "may I print the word
Complete" asks `Report.verified`; a caller asking "is any claim in this run wrong"
asks `Report.ok`. They are two questions and they stay two, because a run of pure
reasoning nodes is legitimately unverifiable and must still be allowed to finish.

Five checks, each answering off a different record:

| Check | Reads |
|---|---|
| `C_STATUS` | the unit's own claim — `agent_tasks.status` / `.error` |
| `C_STEPS` | its own checkpoint sub-steps (`tasks.checkpoint_view()`) |
| `C_COMMANDS` | `exec_commands` rows — exit codes, and whether each one settled |
| `C_TOOLS` | `exec_tool_calls` rows — failures, and `post is null` (the crash signal) |
| `C_WRITES` | the `pre`/`post` digest pairs *inside* those tool rows |

⚠️ **EVIDENCE WITH NO `task_id` IS SESSION-LEVEL AND IS NEVER ATTRIBUTED TO A
NODE.** `tools.py:_exec_started` deliberately does not resolve a task id — that
would be a `tasks.current()` query on the hot path of every tool call, which its own
docstring refuses — so `exec_tool_calls.task_id` is empty in practice today.
Charging those rows to every node would make one failed write contradict all of
them; charging them to none would throw the evidence away. So `Report.session` is a
first-class `Check` of its own: a run-level contradiction, reported beside the
per-node findings rather than smeared across them. The day the tool layer starts
recording a task id, attribution sharpens with no change here.

⚠️ **TWO QUERIES PER REPORT, AT ANY NODE COUNT.** `evidence_for()` reads both
ledgers once and groups by `task_id` in memory, so verifying a 3-node graph and a
64-node graph cost the same — `workflow.for_turn()`'s flatness rule, applied to the
other end of a run. A per-node read would be invisible at the size a developer
tests with and 128 round trips at the size a user writes.

⚠️ **THERE IS NO OFF SWITCH, AND THAT IS DELIBERATE.** Every other subsystem has
one because the app must be able to behave as though the feature was never written.
Verification off does not make Agent2 quieter — it makes it credulous: every claim
would read as confirmed, which is the single state the spec forbids. The only knob
is `AGENT2_VERIFY_MAX_ROWS`, a read ceiling, and when it engages the report says
`truncated` rather than quietly confirming what it did not read.

⚠️ **TOTAL, AND IT ERRS TOWARD REPORTING.** Nothing here may raise into a turn or a
render path (hence the written BLE001/S110 exemption): an unreadable row, a legacy
`detail` column or a missing table folds to `V_UNCONFIRMED` and never to
`V_CONFIRMED`. The asymmetry is the whole design — the cost of a false *confirmed*
is a wrong claim a human acts on, and the cost of a false *contradicted* is a line
a human reads.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ⚠️ THE MODULE, NOT THE VALUES — a `from agent2.config import VERIFY_MAX_ROWS`
# binds a snapshot at import and freezes it, so every test that monkeypatches the
# knob would silently test nothing. `core/execstate.py:108` states the same rule.
from agent2 import config as _cfg
from agent2.core import commands as _cmds
from agent2.core import execstate as _xs
from agent2.core import logging as alog
from agent2.core import tasks as _tasks
from agent2.core.recovery import safety as _safety

# ── Verdicts ──────────────────────────────────────────────────────────────────

V_OPEN = "open"
V_UNSUCCESSFUL = "unsuccessful"
V_CONFIRMED = "confirmed"
V_CONTRADICTED = "contradicted"
V_UNCONFIRMED = "unconfirmed"

# Declared worst-first, because that is the order a human wants them summarised in
# and the order a renderer should walk. It is NOT a severity ladder a caller may
# compare with `<`: `V_OPEN` is not "better" than `V_UNSUCCESSFUL`, it is a
# different question, and an index comparison would invent a ranking nobody meant.
VERDICTS = (V_CONTRADICTED, V_UNCONFIRMED, V_UNSUCCESSFUL, V_OPEN, V_CONFIRMED)

# ⚠️ EXACTLY ONE. A caller that needs "was this really done" gets one word to
# accept, so a verdict added by a later phase is untrusted until somebody decides
# otherwise — the direction `permissions` and `auth` both fall in.
TRUSTED = frozenset((V_CONFIRMED,))

# Settled, and the unit itself says it did not succeed.
UNSUCCESSFUL_STATUS = frozenset((
    _tasks.TaskStatus.FAILED, _tasks.TaskStatus.CANCELLED, _tasks.TaskStatus.SKIPPED,
))


def _settled_tool_statuses() -> frozenset:
    """What "this tool call is over" means — asked of the module that decides it.

    ⚠️ Read from `execstate` rather than re-declared, because `INTERRUPTED` is a word
    that module *"adds here and only here"* and a local copy would stop recognising a
    status the ledger later starts writing — a crash-parked row would then read as
    *still running*, which is a contradiction rather than a park. The public fallback
    is the same three words this build knows, so a rename there costs a slightly
    coarser answer and never an exception at import.
    """
    try:
        return frozenset(_xs._SETTLED["exec_tool_calls"])
    except Exception:
        return frozenset((_tasks.STEP_COMPLETED, _tasks.STEP_FAILED, _xs.INTERRUPTED))


_SETTLED_TOOLS = _settled_tool_statuses()

# ── Checks ────────────────────────────────────────────────────────────────────

C_STATUS = "status"
C_STEPS = "steps"
C_COMMANDS = "commands"
C_TOOLS = "tools"
C_WRITES = "writes"

# Order is the order a finding reports them: the claim first, then each record that
# could contradict it, narrowest last. ⚠️ `C_STATUS` is excluded from the evidence
# count on purpose — the unit's own claim is the thing being judged, so counting it
# as support for itself is exactly the circularity this module exists to break.
CHECKS = (C_STATUS, C_STEPS, C_COMMANDS, C_TOOLS, C_WRITES)
_EVIDENCE_CHECKS = frozenset((C_STEPS, C_COMMANDS, C_TOOLS, C_WRITES))

# ── What a recorded digest pair is expected to show ───────────────────────────
# ⚠️ Keyed on the SAFETY KIND, never on a tool name: `safety._TOOL_KIND` is already
# the one declaration of what a tool does, and a second list here would silently
# stop covering a tool the day one is added to that table.
#
# ⚠️ ONLY TWO KINDS APPEAR, AND THE OMISSIONS ARE THE CAREFUL PART. `GENERATE`
# (`emit_plan`, `update_todo`) names no filesystem path at all, so there is nothing
# to expect. `SHELL`, `GIT_*`, `DATABASE_MUTATION`, `EXTERNAL_API_MUTATION` and
# `DEPLOYMENT` have real effects this module cannot measure — `target_paths()`
# returns `[]` for `run_command` on purpose, *"guessing them would hand a confident
# wrong answer where `exec_commands.status` gives a right one"* — so their evidence
# is the exit code, which `C_COMMANDS` already reads. There is no MOVE/RENAME entry
# because no tool maps to those kinds, and if one arrives it must not land here by
# accident: a move records source **and** destination in one `paths` list, so
# "every path present" and "every path absent" are both wrong for it, and the check
# would fire on every correct move.
_EXPECT_PRESENT = frozenset((_safety.WRITE,))
_EXPECT_ABSENT = frozenset((_safety.DELETE,))

# ── Small totals ──────────────────────────────────────────────────────────────

MAX_REASON = 200        # one problem line; paths are already capped at 260 upstream
MAX_LINES = 24          # problem/warning lines kept per check


def _text(val, cap: int = MAX_REASON) -> str:
    """One line, bounded. Never raises — a row's column may be anything."""
    try:
        out = " ".join(str(val if val is not None else "").split())
    except Exception:
        return ""
    return out[:cap]


def _cap() -> int:
    """The live row ceiling. Read per call so a monkeypatched knob is honoured."""
    try:
        return max(50, int(getattr(_cfg, "VERIFY_MAX_ROWS", 500)))
    except Exception:
        return 500


def _short(paths) -> str:
    """A path list as one readable fragment, bounded."""
    try:
        every = [_text(p, 120) for p in list(paths)[:3] if p]
    except Exception:
        return ""
    return ", ".join(every)


# ── Records ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Check:
    """One record consulted, and what it had to say.

    `checked` is how many pieces of evidence were actually read — 0 means *nothing
    was recorded*, which is a different fact from *nothing was wrong* and is why
    `V_UNCONFIRMED` exists as its own verdict rather than as a quiet `V_CONFIRMED`.
    """

    name: str
    checked: int = 0
    problems: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """Nothing here contradicts a completion claim. ⚠️ Warnings do not count."""
        return not self.problems

    def to_payload(self) -> dict:
        return {
            "check": self.name, "ok": self.ok, "checked": self.checked,
            "problems": list(self.problems), "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class Finding:
    """One unit's verdict, and every line it was derived from.

    ⚠️ `verdict` is computed by `_verdict()` from `checks` — the same lines a reader
    sees — so a check that reported a problem cannot coexist with a `confirmed`
    finding. That is `health._rows()`'s rule, and it is pinned in both directions.
    """

    ref: str
    title: str = ""
    status: str = ""
    verdict: str = V_OPEN
    checks: tuple[Check, ...] = ()

    @property
    def problems(self) -> tuple[str, ...]:
        return tuple(line for c in self.checks for line in c.problems)

    @property
    def warnings(self) -> tuple[str, ...]:
        return tuple(line for c in self.checks for line in c.warnings)

    @property
    def evidence(self) -> int:
        """Pieces of durable evidence read — the unit's own claim excluded."""
        return sum(c.checked for c in self.checks if c.name in _EVIDENCE_CHECKS)

    @property
    def trusted(self) -> bool:
        return self.verdict in TRUSTED

    def to_payload(self) -> dict:
        return {
            "ref": self.ref, "title": self.title, "status": self.status,
            "verdict": self.verdict, "trusted": self.trusted,
            "evidence": self.evidence,
            "problems": list(self.problems), "warnings": list(self.warnings),
            "checks": [c.to_payload() for c in self.checks],
        }


@dataclass(frozen=True)
class Evidence:
    """Both durable ledgers, read once and grouped by `task_id`.

    ⚠️ The `""` bucket is not a task — it is the session-level rows, and
    `verify_tasks()` reports it as its own `Check` rather than charging it to every
    node. See the module docstring; this is the one place the shape of the ledger
    leaks into the shape of a report, and it does so honestly.
    """

    session_id: str = ""
    by_task: dict[str, dict] = field(default_factory=dict)
    session: dict = field(default_factory=lambda: {"commands": [], "tools": []})
    truncated: bool = False
    reads: int = 0

    def for_task(self, task_id: str) -> dict:
        got = self.by_task.get(str(task_id or ""))
        return got if isinstance(got, dict) else {"commands": [], "tools": []}

    def to_payload(self) -> dict:
        return {
            "session_id": self.session_id, "reads": self.reads,
            "truncated": self.truncated,
            "tasks": len(self.by_task),
            "commands": sum(len(b.get("commands") or ()) for b in self.by_task.values())
            + len(self.session.get("commands") or ()),
            "tools": sum(len(b.get("tools") or ()) for b in self.by_task.values())
            + len(self.session.get("tools") or ()),
        }


@dataclass(frozen=True)
class Report:
    """Every unit's verdict, plus the evidence that belongs to no single one.

    Three booleans, three different questions, and they are deliberately not
    collapsed:

    * `ok` — nothing in this run contradicts a claim. Decided by problems **alone**.
    * `complete` — every unit has settled.
    * `verified` — `ok and complete` and nothing failed: the one that licenses the
      word *Complete*.

    ⚠️ `verified` does **not** require every unit to be `V_CONFIRMED`, and that is a
    calibration, not an oversight: a node whose entire job is to read and reason
    records nothing measurable, so requiring confirmation would refuse to finish
    every run containing one. The count is reported instead — `counts` and
    `warnings` carry it — which is `health.py`'s "warnings are reported beside the
    verdict and never promoted into it".
    """

    ref: str = ""
    findings: tuple[Finding, ...] = ()
    session: tuple[Check, ...] = ()
    truncated: bool = False

    @property
    def counts(self) -> dict:
        """Verdict → how many units, in `VERDICTS` order. Every key present."""
        out = dict.fromkeys(VERDICTS, 0)
        for f in self.findings:
            out[f.verdict] = out.get(f.verdict, 0) + 1
        return out

    @property
    def problems(self) -> tuple[str, ...]:
        lines = [f"{f.ref}: {line}" for f in self.findings for line in f.problems]
        lines.extend(f"session: {line}" for c in self.session for line in c.problems)
        return tuple(lines)

    @property
    def warnings(self) -> tuple[str, ...]:
        lines = [f"{f.ref}: {line}" for f in self.findings for line in f.warnings]
        lines.extend(f"session: {line}" for c in self.session for line in c.warnings)
        if self.truncated:
            lines.append(
                f"session: verification read only the newest {_cap()} durable rows"
                " (AGENT2_VERIFY_MAX_ROWS)")
        return tuple(lines)

    @property
    def ok(self) -> bool:
        """Nothing contradicted. ⚠️ Problems alone — a warning may never reach here."""
        return not self.problems

    @property
    def complete(self) -> bool:
        """Every unit settled. An empty report is NOT complete, by construction."""
        return bool(self.findings) and all(f.verdict != V_OPEN for f in self.findings)

    @property
    def unsuccessful(self) -> int:
        return sum(1 for f in self.findings if f.verdict == V_UNSUCCESSFUL)

    @property
    def verified(self) -> bool:
        """May a surface print *Complete*? See the class docstring for why this is
        not `all(f.trusted …)`."""
        return self.ok and self.complete and not self.unsuccessful

    def to_payload(self) -> dict:
        return {
            "ref": self.ref, "ok": self.ok, "complete": self.complete,
            "verified": self.verified, "truncated": self.truncated,
            "counts": self.counts,
            "problems": list(self.problems), "warnings": list(self.warnings),
            "findings": [f.to_payload() for f in self.findings],
            "session": [c.to_payload() for c in self.session],
        }


# ── Reading the ledgers ───────────────────────────────────────────────────────

def evidence_for(*, session_id: str = "", task_ids=(), project=None) -> Evidence:
    """Both durable ledgers for one session, in **two queries**, grouped by task.

    ⚠️ A SESSION ID REPLACES THE PROJECT FILTER RATHER THAN ADDING TO IT. A task
    session id is already narrower than a directory and a caller only holds one
    because it read a task row, so scoping by project as well would answer "no
    evidence" for a run legitimately verified from elsewhere — `execstate.workflow()`
    is project-blind for the same reason. With **no** session id we stay in the
    current project, because an unscoped read of the whole ledger is not a
    verification of anything.

    ⚠️ AND WITH NEITHER A SESSION NOR A TASK LIST THERE IS NOTHING TO READ AT ALL.
    Unattributed rows are `session`-level evidence, and "the session" is only
    meaningful once one is named: collecting them from a bare project read would let
    a failed command from an unrelated turn this morning contradict a report about a
    run that has not started. So that case returns an empty `Evidence` with
    `reads == 0` — the honest answer, and two queries cheaper than the wrong one.

    Total: an unreadable ledger yields an empty `Evidence`, which reads as *nothing
    was recorded* and therefore as `V_UNCONFIRMED` — never as agreement.
    """
    sid = _text(session_id, 64)
    wanted = {_text(t, 64) for t in (task_ids or ()) if _text(t, 64)}
    if not sid and not wanted:
        return Evidence()
    scope = _xs.ANY_PROJECT if sid else project
    cap = _cap()
    reads = 0
    rows_c: list = []
    rows_t: list = []
    try:
        rows_c = _xs.commands(project=scope, session_id=sid, limit=cap) or []
        reads += 1
    except Exception:
        rows_c = []
    try:
        rows_t = _xs.tool_calls(project=scope, session_id=sid, limit=cap) or []
        reads += 1
    except Exception:
        rows_t = []

    by_task: dict[str, dict] = {}
    session: dict = {"commands": [], "tools": []}
    for key, rows in (("commands", rows_c), ("tools", rows_t)):
        for row in rows:
            try:
                tid = _text(row.get("task_id"), 64)
            except Exception:
                continue
            if not tid:
                if sid:
                    session[key].append(row)
                continue
            if wanted and tid not in wanted:
                continue
            by_task.setdefault(tid, {"commands": [], "tools": []})[key].append(row)
    return Evidence(
        session_id=sid, by_task=by_task, session=session,
        truncated=len(rows_c) >= cap or len(rows_t) >= cap, reads=reads,
    )


# ── The five checks ───────────────────────────────────────────────────────────

def _check_status(task) -> Check:
    """What the unit claims about itself — the thing being judged, never support.

    ⚠️ THIS CHECK CANNOT PRODUCE A PROBLEM, BY CONSTRUCTION. A claim cannot
    contradict itself; if it could, the verdict would be derivable from the claim
    alone and this module would have no reason to exist. Everything it notices is a
    warning.

    An `error` string on a row that claims success is one such warning: a node
    retried after a failure legitimately keeps the earlier message, so treating it
    as a contradiction would report every recovered node as broken.
    """
    warnings: list[str] = []
    status = _text(getattr(task, "status", ""), 32)
    err = _text(getattr(task, "error", ""), MAX_REASON)
    if status == _tasks.TaskStatus.COMPLETED and err:
        warnings.append(f"claims completed with an error still recorded: {err}")
    return Check(C_STATUS, checked=1, warnings=tuple(warnings))


def _check_steps(task) -> Check:
    """The unit's own checkpoint sub-steps — written by the loop, not the model.

    ⚠️ A FAILED sub-step under a completed claim is a **problem**: `note_tool()` is
    called by the agent loops themselves, so that status is a record of a tool
    call that came back unsuccessful, and a unit reporting success over it is
    exactly *"worker says Done"*. A step still marked RUNNING, or one never
    reached, is a **warning** — a plan may legitimately overshoot, and a
    breadcrumb nobody closed is a missing record rather than a wrong one.
    """
    problems: list[str] = []
    warnings: list[str] = []
    try:
        view = _tasks.checkpoint_view(task)
    except Exception:
        return Check(C_STEPS)
    failed = [_text(n, 80) for n in (view.get("failed") or ())][:MAX_LINES]
    remaining = [_text(n, 80) for n in (view.get("remaining") or ())][:MAX_LINES]
    current = _text(view.get("current"), 80)
    total = int(view.get("total") or 0)
    problems.extend(f"sub-step recorded as failed: {name}" for name in failed)
    if current:
        warnings.append(f"sub-step still recorded as running: {current}")
    if view.get("destructive_pending"):
        warnings.append(
            "a destructive sub-step never reported back — it may or may not have landed")
    if remaining:
        warnings.append(
            f"{len(remaining)} planned sub-step(s) never ran: {', '.join(remaining[:3])}")
    return Check(C_STEPS, checked=total, problems=tuple(problems),
                 warnings=tuple(warnings))


def _check_commands(rows) -> Check:
    """`exec_commands` — an exit code is the one thing a shell cannot fake.

    ⚠️ A row that never settled is a **problem**, not a warning. This check only
    ever runs against a unit that already claims success, so a command still marked
    `running` there is a process whose verdict nobody ever wrote: the classic
    killed-mid-turn shape, and the exact case `execstate.INTERRUPTED` exists for.
    """
    problems: list[str] = []
    warnings: list[str] = []
    checked = 0
    for row in rows or ():
        try:
            status = _text(row.get("status"), 32)
            label = _text(row.get("command"), 100) or _text(row.get("id"), 64)
            code = row.get("exit_code")
            err = _text(row.get("error"), MAX_REASON)
        except Exception:
            continue
        checked += 1
        if status in _cmds.FAILURE:
            problems.append(f"command {status}: {label}"
                            + (f" — {err}" if err else ""))
            continue
        if status == _xs.INTERRUPTED:
            problems.append(f"command was interrupted, verdict never written: {label}")
            continue
        if status not in _cmds.TERMINAL:
            problems.append(f"command never settled (still {status or '?'}): {label}")
            continue
        if code is not None:
            try:
                if int(code) != 0:
                    problems.append(f"command exited {int(code)}: {label}")
                    continue
            except Exception:
                warnings.append(f"command exit code unreadable: {label}")
        if err:
            warnings.append(f"command completed with a note: {label} — {err}")
    return Check(C_COMMANDS, checked=checked, problems=tuple(problems[:MAX_LINES]),
                 warnings=tuple(warnings[:MAX_LINES]))


def _check_tools(rows) -> Check:
    """`exec_tool_calls` — did each call return, and did it say it worked?

    ⚠️ `post is null` ON A SETTLED ROW IS THE CRASH SIGNAL, stated by
    `execstate._detail_json`'s own docstring: *"a row whose `post` was never written
    is a tool that never returned. An empty dict would be indistinguishable from
    'finished, touched nothing'."* `tool_finished()` always writes a `post`, so its
    absence under a settled status means the process died between the two.
    """
    problems: list[str] = []
    warnings: list[str] = []
    checked = 0
    for row in rows or ():
        try:
            status = _text(row.get("status"), 32)
            tool = _text(row.get("tool"), 64) or "?"
            err = _text(row.get("error"), MAX_REASON)
            ok = row.get("ok")
            detail = _xs.parse_detail(row.get("detail"))
        except Exception:
            continue
        checked += 1
        target = _text(detail.get("target"), 100)
        where = f"{tool}({target})" if target else tool
        if status == _tasks.STEP_FAILED or ok == 0:
            problems.append(f"tool call failed: {where}" + (f" — {err}" if err else ""))
            continue
        if status == _xs.INTERRUPTED:
            problems.append(f"tool call was interrupted, verdict never written: {where}")
            continue
        if status not in _SETTLED_TOOLS:
            problems.append(f"tool call never returned (still {status or '?'}): {where}")
            continue
        if err:
            problems.append(f"tool call recorded an error: {where} — {err}")
            continue
        if detail and detail.get("post", "missing") is None:
            problems.append(
                f"tool call has no recorded outcome — it never returned: {where}")
            continue
        if not detail:
            warnings.append(f"tool call predates structured recording: {where}")
    return Check(C_TOOLS, checked=checked, problems=tuple(problems[:MAX_LINES]),
                 warnings=tuple(warnings[:MAX_LINES]))


def _check_writes(rows) -> Check:
    """The recorded `pre`/`post` digest pairs — did the filesystem move?

    This is the sharpest check and the narrowest. It reads only what was measured at
    call time and compares it against what the operation's **safety kind** implies:
    a write should leave its targets present, a delete should leave them gone.

    ⚠️ IT SKIPS A ROW THAT ALREADY FAILED. `_check_tools` has reported it, and a
    refused `write_file` legitimately leaves nothing behind — a second line saying
    *"wrote X and X is not there"* would describe a failure that never started as
    one that went wrong halfway.

    ⚠️ An identical `pre`/`post` digest is a **warning**, never a contradiction:
    `projectdoc`'s rule is that an identical rewrite is not a write, and a file
    written back byte-for-byte is a no-op somebody may want to know about, not a
    lie.
    """
    problems: list[str] = []
    warnings: list[str] = []
    checked = 0
    for row in rows or ():
        try:
            status = _text(row.get("status"), 32)
            ok = row.get("ok")
            detail = _xs.parse_detail(row.get("detail"))
        except Exception:
            continue
        if status != _tasks.STEP_COMPLETED or ok == 0:
            continue
        post = detail.get("post")
        pre = detail.get("pre")
        if not isinstance(post, dict):
            continue
        tool = _text(detail.get("tool"), 64) or _text(row.get("tool"), 64)
        kind = _safety.kind_for_tool(tool)
        if kind not in _EXPECT_PRESENT and kind not in _EXPECT_ABSENT:
            continue
        pre_map = pre if isinstance(pre, dict) else {}
        missing: list[str] = []
        surviving: list[str] = []
        unchanged: list[str] = []
        for path, digest in post.items():
            checked += 1
            if kind in _EXPECT_ABSENT:
                if digest is not None:
                    surviving.append(_text(path, 120))
                continue
            if digest is None:
                missing.append(_text(path, 120))
            elif pre_map.get(path) == digest:
                unchanged.append(_text(path, 120))
        if missing:
            problems.append(
                f"{tool or 'write'} reported success and its target is not there:"
                f" {_short(missing)}")
        if surviving:
            problems.append(
                f"{tool or 'delete'} reported success and its target is still there:"
                f" {_short(surviving)}")
        if unchanged:
            warnings.append(
                f"{tool or 'write'} left {len(unchanged)} file(s) byte-for-byte"
                f" unchanged: {_short(unchanged)}")
        try:
            total = int(detail.get("paths_total") or 0)
        except Exception:
            total = 0
        if total > len(post):
            warnings.append(
                f"{tool or 'tool'} named {total} paths and only {len(post)} were"
                " measured — verification of the rest is not possible")
    return Check(C_WRITES, checked=checked, problems=tuple(problems[:MAX_LINES]),
                 warnings=tuple(warnings[:MAX_LINES]))


# ── Verdicts ──────────────────────────────────────────────────────────────────

def _verdict(status: str, checks) -> str:
    """The one derivation, from the very lines a reader sees.

    ⚠️ Never re-tested from the data. `health._rows()`'s rule: a `Check` that
    reported a problem beside a `confirmed` finding would be a report disagreeing
    with itself, and both halves would look right alone.
    """
    if status not in _tasks.TERMINAL:
        return V_OPEN
    if status in UNSUCCESSFUL_STATUS:
        return V_UNSUCCESSFUL
    if any(c.problems for c in checks):
        return V_CONTRADICTED
    seen = sum(c.checked for c in checks if c.name in _EVIDENCE_CHECKS)
    return V_CONFIRMED if seen else V_UNCONFIRMED


def verify_task(task_id_or_task, *, evidence: Evidence | None = None) -> Finding:
    """Verify ONE unit of work — a task row, whatever created it.

    Pass `evidence` when verifying several units so the ledgers are read once;
    without it this resolves the task's own session and reads them itself, which is
    the two-query single-node path.

    Total: an id that names nothing yields a `Finding` with `V_OPEN` and no checks —
    *nothing has claimed anything*, which is the honest answer and not a raise.
    """
    try:
        task = (task_id_or_task if isinstance(task_id_or_task, _tasks.Task)
                else _tasks.get(_text(task_id_or_task, 64)))
    except Exception:
        task = None
    if task is None:
        return Finding(ref=_id_of(task_id_or_task), verdict=V_OPEN)

    status = _text(getattr(task, "status", ""), 32)
    checks: list[Check] = [_check_status(task), _check_steps(task)]
    ev = evidence
    if ev is None:
        ev = evidence_for(session_id=_text(getattr(task, "session_id", ""), 64),
                          task_ids=(task.id,))
    bucket = ev.for_task(task.id)
    checks.append(_check_commands(bucket.get("commands")))
    checks.append(_check_tools(bucket.get("tools")))
    checks.append(_check_writes(bucket.get("tools")))
    frozen = tuple(checks)
    return Finding(
        ref=_text(task.id, 64), title=_text(getattr(task, "title", ""), 120),
        status=status, verdict=_verdict(status, frozen), checks=frozen,
    )


def _id_of(unit) -> str:
    """A task id, whether the caller handed us one or a whole row."""
    return _text(unit if isinstance(unit, str) else getattr(unit, "id", ""), 64)


def _session_of(unit) -> str:
    """The session a unit belongs to, resolved from a row or by one read.

    Kept as its own step because `verify_tasks` accepts ids *and* rows, and a
    conditional doing both inline is where a totality bug hides — a stale id must
    yield `""` and let the evidence read fall back to the current project, never
    raise into whatever surface asked for a verdict.
    """
    if not isinstance(unit, str):
        return _text(getattr(unit, "session_id", ""), 64)
    try:
        row = _tasks.get(_text(unit, 64))
    except Exception:
        return ""
    return _text(getattr(row, "session_id", ""), 64) if row is not None else ""


def verify_tasks(ids, *, session_id: str = "", ref: str = "",
                 evidence: Evidence | None = None) -> Report:
    """Verify a set of units as one run, and log the outcome.

    `ref` names what the report is *about* — a workflow run id, a session id — and
    is a label only; nothing here reads it. `ids` may be task ids or `Task` rows.

    ⚠️ The session-level rows land in `Report.session` as their own checks, not on a
    node. See the module docstring: attributing them to every node would let one
    failed command contradict a whole graph, and dropping them would throw real
    evidence away.
    """
    units = [u for u in (ids or ()) if u]
    sid = _text(session_id, 64) or (_session_of(units[0]) if units else "")
    ev = evidence
    if ev is None:
        ev = evidence_for(session_id=sid, task_ids=tuple(_id_of(u) for u in units))

    findings = tuple(verify_task(u, evidence=ev) for u in units)
    loose = (
        _check_commands(ev.session.get("commands")),
        _check_tools(ev.session.get("tools")),
        _check_writes(ev.session.get("tools")),
    )
    report = Report(ref=_text(ref, 64) or sid, findings=findings,
                    session=tuple(c for c in loose if c.checked or c.problems),
                    truncated=ev.truncated)
    _record(report, sid)
    return report


def _record(report: Report, session_id: str) -> None:
    """Audit the outcome. ⚠️ Every verdict, not only the bad one — see `alog`."""
    try:
        for finding in report.findings:
            alog.verification_reported(
                finding.ref, verdict=finding.verdict, checks=len(finding.checks),
                problems=len(finding.problems), session_id=session_id)
            if finding.verdict == V_CONTRADICTED and finding.problems:
                bad = next((c for c in finding.checks if c.problems), None)
                alog.verification_contradicted(
                    finding.ref, reason=finding.problems[0],
                    check=bad.name if bad else "", session_id=session_id)
        for check in report.session:
            for line in check.problems:
                alog.verification_contradicted(
                    report.ref or session_id, reason=line,
                    check=check.name, session_id=session_id)
    except Exception:
        pass


# ── Introspection ─────────────────────────────────────────────────────────────

def describe() -> dict:
    """What this build verifies, and how, as data. For `/health` and a payload.

    Built from the declarations themselves — the verdict tuple, the check tuple, the
    kind tables, the live ceiling — never a hand-written list, which is
    `dag.describe()`'s rule and `loader.SUBSET`'s reason: a document is the one form
    of this answer that can go stale while the code changes underneath it.
    """
    return {
        "verdicts": list(VERDICTS),
        "trusted": sorted(TRUSTED),
        "checks": list(CHECKS),
        "evidence_checks": sorted(_EVIDENCE_CHECKS),
        "expect_present": sorted(_EXPECT_PRESENT),
        "expect_absent": sorted(_EXPECT_ABSENT),
        "unsuccessful": sorted(UNSUCCESSFUL_STATUS),
        "max_rows": _cap(),
        "reads_per_report": 2,
        # ⚠️ Stated in the payload because it is the honest scope of every verdict:
        # this module compares what was recorded, and never re-measures disk.
        "restats_disk": False,
    }
