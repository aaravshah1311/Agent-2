# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
UltraCode — the adaptive autonomous engineering loop (Phase D5).

Run from the repo root:  python -m pytest .github/tests/test_ultracode.py -v

──────────────────────────────────────────────────────────────────────────────
What this file pins, and why each one is here rather than trusted:

* **The two vocabulary collisions are the only two.** `stages.py`'s docstring
  says `test_ultracode.py` "asserts the vocabularies pair-by-pair with exactly
  these two exceptions named". Before this file existed that paragraph claimed
  `(asserted)` and nothing asserted it — which is how it came to be wrong about
  `verifying` as well. Measured over every module-level string constant and
  container-of-strings the sibling modules declare, so a *third* collision
  cannot arrive in silence — `test_the_stage_words_collide_with_exactly_two`,
  `test_no_kind_refusal_or_worker_word_collides_with_a_sibling`.

* **`STAGES` is the one declaration of the loop's order.** Three files print the
  arrow chain in prose and `config.py` prints it a fourth time; the constant is
  what `rank()` derives from. All four are pinned equal to it, and the inverted
  spelling is pinned *absent*, because that inversion had already shipped —
  `test_stages_is_the_one_declaration_of_the_order`,
  `test_every_file_that_prints_the_loop_puts_discover_before_plan`.

* **A stage is derived from the rows, never stored** — and `stage_of()` only ever
  answers with a stage a `GraphState` can prove, so the four transient stages are
  unreachable by construction rather than by convention:
  `test_stage_of_only_ever_returns_a_derived_stage`,
  `test_a_settled_graph_reads_finalizing_never_finished`.

* **A gate is a paused row with no stop checkpoint.** That is the only thing
  separating *a human is holding this* from *a dead process abandoned it*, so it
  is pinned from both ends: recovery cannot release a gate, and `approve()`
  releases the gate row alone — never `unpause_run()`, which would also release
  work a person paused on purpose. `test_start_parks_the_gate_out_of_recovery_reach`,
  `test_approve_releases_the_gate_row_and_nothing_else`.

* **The check node asks `core/verify.py`, never a model.** *"'Done' is not
  verification"*: a node that graded its own run would be the exact false
  *confirmed* that module exists to prevent —
  `test_the_check_node_asks_core_verify_and_never_the_turn`.

* **Every entry point refuses by returning.** Thirteen refusals, four
  dataclasses, and nothing on this path may raise into a turn: a broken brief, an
  unstampable definition, an unparkable gate and a pump that raises each cost one
  field and never the run — `test_start_refuses_in_ladder_order`,
  `test_a_broken_brief_costs_the_evidence_and_never_the_run`,
  `test_a_pump_that_raises_is_reported_not_propagated`.

* **No instruction ever reaches a payload.** A step's text is somebody's prompt
  about their own checkout; all four payloads plus `state()` are swept for a
  canary — `test_no_payload_here_carries_a_step_instruction`.

* **This package holds no fact of its own** — no file writer, no database import,
  and not one call to a DAG predicate it would then be a second opinion about.
  Asserted over the `ast`, never over the text, because these modules name those
  functions in prose deliberately and `stages.py` legitimately *reads*
  `v.dispatchable` — `test_nothing_here_writes_a_file_or_opens_the_database`,
  `test_the_package_calls_no_dag_predicate_of_its_own`.

`conftest.py` redirects `AGENT2_DB` to a per-process temporary file, so every
test here writes to a throwaway database.
"""

from __future__ import annotations

import ast
import json
import re
import threading
from pathlib import Path

import pytest

from agent2 import config as cfg
from agent2 import database as db
from agent2.core import execstate as X
from agent2.core import tasks as T
from agent2.core import ultracode as UC
from agent2.core import verify as V
from agent2.core.dag import model as M
from agent2.core.dag import schedule as SC
from agent2.core.dag import store as S
from agent2.core.recovery import classify as CL
from agent2.core.recovery import crash as CR
from agent2.core.recovery import safety as SF
from agent2.core.skills import select as SEL
from agent2.core.ultracode import engine as E
from agent2.core.ultracode import plan as P
from agent2.core.ultracode import stages as ST
from agent2.core.workflow import dynamic as DYN
from agent2.core.workflow import runner as RUN

#: Planted in every generated step's instruction. A step's text is a prompt about
#: somebody's own checkout, so it may reach a node row and never a payload.
SECRET = "INSTRUCTION-DO-NOT-LEAK-7c02"

#: The genuine `plan.brief()`, captured before the autouse stub replaces it.
_REAL_BRIEF = P.brief

_TABLES = ("exec_workflows", "exec_tool_calls", "exec_commands",
           "agent_tasks", "task_sessions")


@pytest.fixture(autouse=True)
def _clean():
    """A fresh graph store per test — every table an UltraCode run touches.

    `store.live()` decides `U_LIVE` off `exec_workflows`, and the check node reads
    both `exec_*` ledgers, so a leftover row from the previous test is a refusal
    or a verdict nobody asked for.
    """
    db.init_db()
    X.reset()
    for table in _TABLES:
        db.exe(f"DELETE FROM {table}")
    if hasattr(X, "_failure_reported"):
        X._failure_reported = False
    yield
    for table in _TABLES:
        db.exe(f"DELETE FROM {table}")


@pytest.fixture(autouse=True)
def _cheap_brief(monkeypatch):
    """`plan.brief()` walks the whole project; nothing but its own tests want that.

    Those tests reach the real one through `_REAL_BRIEF`.
    """
    monkeypatch.setattr(P, "brief", lambda goal, **kw: P.Brief(goal=str(goal or "")))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _node(spec, seq=0, **kw):
    """One `dag.Node` from `"id"` or `"id:dep,dep"`."""
    nid, _, needs = str(spec).partition(":")
    return M.Node(id=nid, title=nid.replace("-", " ").title(),
                  instruction=f"{SECRET} do {nid}", seq=seq,
                  needs=tuple(n for n in needs.split(",") if n), **kw)


def _defn(*specs, name="uc-run"):
    """A `dag.Graph` — which is what a `WorkflowDef` IS — from those specs."""
    return M.Graph(name=name, source=DYN.SRC_GOAL,
                   nodes=tuple(_node(s, i) for i, s in enumerate(specs)))


def _draft(defn, **kw):
    """A planner answer carrying *defn*. `validation=None` is a real shape here."""
    fields = {"ok": defn is not None, "mode": DYN.MODE_AUTO, "goal": "g",
              "defn": defn, "source": DYN.SRC_GOAL,
              "added": tuple(n.id for n in (getattr(defn, "nodes", ()) or ()))}
    fields.update(kw)
    return DYN.Draft(**fields)


def _plant(monkeypatch, *specs, draft=None):
    """Install a deterministic planner. Returns the list its calls are recorded in."""
    answer = draft if draft is not None else _draft(_defn(*specs))
    calls = []

    def _fake(goal, **kw):
        calls.append(dict(kw, goal=goal))
        return answer

    monkeypatch.setattr(DYN, "draft", _fake)
    return calls


def _views(run_id):
    """`{node id: NodeView}` for a run."""
    st = S.load(str(run_id))
    return {v.node: v for v in (st.nodes or ())}


def _inline():
    """Deterministic execution: no pool, one node at a time, on this thread."""
    return SC.limits(max_workers=0)


def _growing_replan():
    """A stubbed `replan` that really grows the graph, because `drive()` checks.

    ⚠️ AN OK `Draft` IS NOT ENOUGH, AND THAT IS THE POINT OF THE CHECK. `drive()`
    reads `st.total` before and after each re-plan and breaks with `U_PLANNER`
    ("the re-plan added no node") when it did not rise — a planner that *claims* a
    plan and writes nothing would otherwise spin the loop forever. So a stub that
    only returns a `Draft` can never reach `U_CYCLES` or `U_BUDGET`: it is refused
    one line earlier, and the test would pin the wrong refusal.

    Growth is `store.extend()`, the one mutation path — never a hand-written row.
    """
    seen = {"n": 0}

    def _replan(run_id, **_kw):
        seen["n"] += 1
        node = _node(f"fix-{seen['n']}", kind=ST.K_FIX)
        S.extend(str(run_id), (node,), label=P.LABEL)
        return _draft(_defn(node.id), goal="g")

    return _replan


def _ok(_ctx):
    return True


def _tree(path):
    return ast.parse(Path(path).read_text(encoding="utf-8"))


def _called(tree):
    """Every name this module CALLS — attribute calls by their attribute name.

    ⚠️ `ast`, never a regex over the file: these modules name the functions they
    are forbidden to call in their own prose, and `stages.py` legitimately reads
    the `dispatchable` attribute, so a text sweep would fail on the very
    documentation of the rule it is checking.
    """
    out = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute):
            out.add(node.func.attr)
        elif isinstance(node.func, ast.Name):
            out.add(node.func.id)
    return out


_PKG = Path(UC.__file__).resolve().parent
_MODULES = {"stages.py": _PKG / "stages.py",
            "plan.py": _PKG / "plan.py",
            "engine.py": _PKG / "engine.py",
            "__init__.py": _PKG / "__init__.py"}


# ═══════════════════════════════════════════════════════════════════════════════
# 1 · The words — one vocabulary, and the two collisions it admits to
# ═══════════════════════════════════════════════════════════════════════════════

_CONST = re.compile(r"^[A-Z][A-Z0-9_]*$")

#: Every sibling module that declares a closed vocabulary UltraCode sits between.
_SIBLINGS = (("model", M), ("schedule", SC), ("runner", RUN), ("dynamic", DYN),
             ("verify", V), ("crash", CR), ("classify", CL), ("safety", SF),
             ("select", SEL), ("tasks", T))


def _sibling_words():
    """`{value: "module.NAME"}` over every module-level string those modules declare.

    Bare constants and the members of every container of strings, so the census is
    total by construction rather than by a hand-kept list somebody must remember
    to extend. Dict *values* are excluded — `STAGE_LABEL`'s "VERIFY" is
    presentation, and measuring it would report a collision between a word and its
    own label.
    """
    out = {}
    for label, mod in _SIBLINGS:
        for name, val in sorted(vars(mod).items()):
            if not _CONST.match(name):
                continue
            if isinstance(val, str):
                out.setdefault(val, f"{label}.{name}")
            elif isinstance(val, (tuple, list, frozenset, set)):
                members = tuple(val)
                if members and all(isinstance(v, str) for v in members):
                    for value in sorted(members):
                        out.setdefault(value, f"{label}.{name}")
    # ⚠️ `TaskStatus` is a plain constant namespace, not an `Enum` — iterating the
    # class itself raises, and a census that quietly skipped it would under-report.
    for name, val in sorted(vars(T.TaskStatus).items()):
        if _CONST.match(name) and isinstance(val, str):
            out.setdefault(val, f"tasks.TaskStatus.{name}")
    return out


def test_the_stage_words_collide_with_exactly_two():
    """`stages.py`'s two named exceptions are the only two — nine of eleven are free.

    The paragraph this pins used to say `(asserted)` while nothing asserted it, and
    it was wrong about `verifying`. A third collision now fails here instead of
    being discovered in a payload where two facts share one word.
    """
    foreign = _sibling_words()
    hits = {(word, foreign[word]) for word in ST.STAGES if word in foreign}
    # ⚠️ The provenance label is the FIRST declaration carrying the word in sorted
    # order, so a module that both names a word and collects it reads as the
    # container — `crash.STATES` holds `S_VERIFYING`, `schedule.REASONS` holds
    # `R_FINISHED`. WHICH MODULE owns the collision is the fact worth pinning here;
    # the two constants themselves are spelled out below, so a rename that moved the
    # word out of its container would still fail one half of this test.
    assert hits == {("verifying", "crash.STATES"),
                    ("finished", "schedule.REASONS")}, hits
    assert len(ST.STAGES) == 11
    assert len(set(ST.STAGES)) == 11
    # And the two that DO collide are the two the docstring names, spelled out.
    assert ST.S_VERIFY == CR.S_VERIFYING and CR.S_VERIFYING in CR.STATES
    assert ST.S_FINISHED == SC.R_FINISHED and SC.R_FINISHED in SC.REASONS


def test_no_kind_refusal_or_worker_word_collides_with_a_sibling():
    """The other three UltraCode vocabularies are wholly free of the siblings'."""
    foreign = _sibling_words()
    for label, words in (("KINDS", ST.KINDS), ("REFUSALS", E.REFUSALS),
                         ("WORKER", (E.WORKER,))):
        hits = {(w, foreign[w]) for w in words if w in foreign}
        assert not hits, f"{label} collides: {hits}"
    assert E.WORKER != SC.WORKER
    assert len(set(E.REFUSALS)) == len(E.REFUSALS) == 13
    assert len(set(ST.KINDS)) == len(ST.KINDS) == 4


def test_the_ultracode_vocabularies_share_no_word_with_each_other():
    """Four closed vocabularies here too — a word carrying two meanings is the bug."""
    groups = {"stages": set(ST.STAGES), "kinds": set(ST.KINDS),
              "refusals": set(E.REFUSALS), "worker": {E.WORKER}}
    for a, words_a in groups.items():
        for b, words_b in groups.items():
            if a < b:
                assert not (words_a & words_b), f"{a} ∩ {b} = {words_a & words_b}"


# ═══════════════════════════════════════════════════════════════════════════════
# 2 · The order — DISCOVER SKILLS before PLAN, declared once
# ═══════════════════════════════════════════════════════════════════════════════

_ARROW = "INSPECT → DISCOVER SKILLS → PLAN"
_INVERTED = ("PLAN → DISCOVER", "PLAN -> DISCOVER",
             "INSPECT → PLAN", "INSPECT -> PLAN")


def test_stages_is_the_one_declaration_of_the_order():
    """`rank()` derives from `STAGES`; nothing re-states the order as data."""
    assert ST.STAGES == (
        "understanding", "inspecting", "discovering", "planning", "executing",
        "observing", "analyzing", "verifying", "replanning", "finalizing",
        "finished")
    assert [ST.rank(s) for s in ST.STAGES] == list(range(11))
    assert ST.rank(ST.S_DISCOVER) < ST.rank(ST.S_PLAN)
    assert ST.rank("not-a-stage") == -1
    assert ST.rank("") == -1


def test_every_file_that_prints_the_loop_puts_discover_before_plan():
    """Four files print the chain in prose. Prose that inverts it had shipped once.

    The negative half is the load-bearing one: the arrow being *present* somewhere
    in a file would still pass with an inverted copy three paragraphs below it.
    """
    files = dict(_MODULES)
    files["config.py"] = Path(cfg.__file__).resolve()
    files.pop("plan.py")            # plan.py describes the brief, not the chain
    for name, path in sorted(files.items()):
        text = Path(path).read_text(encoding="utf-8")
        assert _ARROW in text, f"{name} does not print the loop's order"
        for bad in _INVERTED:
            assert bad not in text, f"{name} still spells {bad!r}"


def test_the_three_stage_sets_partition_the_eleven_stages():
    """Derived · transient · terminal — disjoint, and together the whole vocabulary."""
    assert ST.DERIVED | ST.TRANSIENT | ST.TERMINAL_STAGES == set(ST.STAGES)
    assert not ST.DERIVED & ST.TRANSIENT
    assert not ST.DERIVED & ST.TERMINAL_STAGES
    assert not ST.TRANSIENT & ST.TERMINAL_STAGES
    # The four transient ones are exactly the stages no row can prove.
    assert ST.TRANSIENT == {ST.S_UNDERSTAND, ST.S_INSPECT, ST.S_DISCOVER,
                            ST.S_REPLAN}
    assert ST.TERMINAL_STAGES == {ST.S_FINISHED}
    assert set(ST.STAGE_LABEL) == set(ST.STAGES)
    assert ST.STAGE_LABEL[ST.S_DISCOVER] == "DISCOVER SKILLS"
    assert ST.STAGE_LABEL[ST.S_FINISHED] == "COMPLETE"


# ═══════════════════════════════════════════════════════════════════════════════
# 3 · stages.py — the stage a run's rows prove, and the gate they hold
# ═══════════════════════════════════════════════════════════════════════════════

class _V:
    """A stand-in `NodeView`: the five fields `stage_of`/`awaiting_approval` read."""

    def __init__(self, node, state, kind=ST.K_BUILD, dispatchable=False,
                 interrupted=False, priority=0, seq=0):
        self.node, self.state, self.kind = node, state, kind
        self.dispatchable, self.interrupted = dispatchable, interrupted
        self.priority, self.seq = priority, seq


class _St:
    def __init__(self, nodes, exists=True, finished=False):
        self.nodes, self.exists, self.finished = list(nodes), exists, finished


def test_stage_of_is_total_and_reads_a_shapeless_state_as_planning():
    for junk in (None, _St([], exists=False), _St([]), object()):
        assert ST.stage_of(junk) == ST.S_PLAN


def test_stage_of_only_ever_returns_a_derived_stage():
    """Every branch of it, and never one of the four stages no row can prove."""
    cases = [
        _St([]),
        _St([_V("a", M.COMPLETED)], finished=True),
        _St([_V("a", M.RUNNING)]),
        _St([_V("v", M.RUNNING, ST.K_CHECK)]),
        _St([_V("a", M.PENDING, dispatchable=True)]),
        _St([_V("v", M.PENDING, ST.K_CHECK, dispatchable=True)]),
        _St([_V("a", M.FAILED)]),
        _St([_V("a", M.PAUSED, ST.K_APPROVAL)]),
    ]
    for st in cases:
        assert ST.stage_of(st) in ST.DERIVED
        assert ST.stage_of(st) not in ST.TRANSIENT
        assert ST.stage_of(st) not in ST.TERMINAL_STAGES


def test_a_settled_graph_reads_finalizing_never_finished():
    """`finished` means *settled and judged*, and only `finalize()` may say it."""
    st = _St([_V("a", M.COMPLETED)], finished=True)
    assert ST.stage_of(st) == ST.S_FINALIZE
    # Tested before the nodes, or a settled graph would fall through to observing.
    st_failed = _St([_V("a", M.FAILED)], finished=True)
    assert ST.stage_of(st_failed) == ST.S_FINALIZE


def test_only_verification_left_reads_verifying_and_a_mix_reads_executing():
    only = _St([_V("a", M.COMPLETED), _V("v", M.RUNNING, ST.K_CHECK)])
    assert ST.stage_of(only) == ST.S_VERIFY
    mixed = _St([_V("a", M.RUNNING), _V("v", M.RUNNING, ST.K_CHECK)])
    assert ST.stage_of(mixed) == ST.S_EXECUTE
    # A kindless row counts as work, never as verification.
    assert ST.stage_of(_St([_V("a", M.RUNNING, "")])) == ST.S_EXECUTE


def test_a_failure_reads_analyzing_only_when_nothing_can_proceed():
    """`ready()` releases on *settled*, so one failed branch beside live work is
    still executing — `analyzing` is the diagnosis condition, not the presence of
    a failure."""
    live = _St([_V("a", M.FAILED), _V("b", M.PENDING, dispatchable=True)])
    assert ST.stage_of(live) == ST.S_EXECUTE
    stuck = _St([_V("a", M.FAILED), _V("b", M.BLOCKED)])
    assert ST.stage_of(stuck) == ST.S_ANALYZE
    held = _St([_V("a", M.PAUSED, ST.K_APPROVAL), _V("b", M.BLOCKED)])
    assert ST.stage_of(held) == ST.S_OBSERVE


def test_awaiting_approval_skips_a_crash_park_and_sorts_by_priority_seq_node():
    """A gate carries no stop checkpoint; a paused row that does is recovery's."""
    nodes = [_V("z-gate", M.PAUSED, ST.K_APPROVAL, priority=1, seq=9),
             _V("a-gate", M.PAUSED, ST.K_APPROVAL, priority=1, seq=9),
             _V("first", M.PAUSED, ST.K_APPROVAL, priority=0, seq=3),
             _V("parked", M.PAUSED, ST.K_APPROVAL, interrupted=True),
             _V("work", M.PAUSED, ST.K_BUILD),
             _V("running", M.RUNNING, ST.K_APPROVAL)]
    assert [v.node for v in ST.awaiting_approval(_St(nodes))] == [
        "first", "a-gate", "z-gate"]
    assert ST.awaiting_approval(None) == []
    assert ST.awaiting_approval(_St([])) == []


def test_the_kinds_route_to_one_stage_each_and_a_check_is_labelled_verify():
    assert set(ST.KIND_STAGE) == set(ST.KINDS)
    assert set(ST.KIND_LABEL) == set(ST.KINDS)
    assert set(ST.KIND_STAGE.values()) <= ST.DERIVED
    assert ST.KIND_STAGE[ST.K_CHECK] == ST.S_VERIFY
    assert ST.KIND_STAGE[ST.K_APPROVAL] == ST.S_OBSERVE
    assert ST.KIND_STAGE[ST.K_FIX] == ST.KIND_STAGE[ST.K_BUILD] == ST.S_EXECUTE
    # The spec's word for a check node, which is NOT the stage's word.
    assert ST.KIND_LABEL[ST.K_CHECK] == "verify"
    assert ST.KIND_LABEL[ST.K_CHECK] != ST.S_VERIFY
    # The three kind sets partition the four kinds.
    assert ST.WORK_KINDS | ST.VERIFY_KINDS | ST.GATE_KINDS == set(ST.KINDS)
    assert not ST.WORK_KINDS & ST.VERIFY_KINDS
    assert not ST.WORK_KINDS & ST.GATE_KINDS
    assert not ST.VERIFY_KINDS & ST.GATE_KINDS


def test_stages_describe_carries_ten_keys_and_no_terminal_set():
    got = ST.describe()
    assert set(got) == {"stages", "labels", "derived", "transient", "kinds",
                        "kind_labels", "kind_stage", "work_kinds",
                        "verify_kinds", "gate_kinds"}
    assert got["stages"] == list(ST.STAGES)
    assert got["labels"] == dict(ST.STAGE_LABEL)
    json.dumps(got)


# ═══════════════════════════════════════════════════════════════════════════════
# 4 · plan.py — what the run is about, and the shape the plan is given
# ═══════════════════════════════════════════════════════════════════════════════

def test_a_brief_degrades_one_line_per_broken_source(monkeypatch):
    """⚠️ ONE GUARD PER SOURCE. Three of the four broken must still leave a brief.

    And every note carries the exception's *type*, never its message: a brief is
    printed, sent to a browser and turned into a node instruction, so an
    exception's text (which routinely holds a path) has no business in one.
    """
    import agent2.core.projectdoc as PD
    import agent2.core.projectscan as PS
    import agent2.core.skills as SK

    boom = "SECRET-PATH-c:/somebody/private"

    def _raise(*a, **kw):
        raise RuntimeError(boom)

    monkeypatch.setattr(PS, "scan", _raise)
    monkeypatch.setattr(PD, "read", _raise, raising=False)
    monkeypatch.setattr(PD, "parse", _raise, raising=False)
    monkeypatch.setattr(SK, "for_turn", _raise, raising=False)

    got = _REAL_BRIEF("ship the thing")
    assert isinstance(got, P.Brief)
    assert got.goal == "ship the thing"
    assert got.notes, "a broken source must be reported, never swallowed"
    joined = " ".join(got.notes)
    assert "RuntimeError" in joined
    assert boom not in joined
    assert boom not in json.dumps(got.to_payload())


def test_a_brief_never_guesses_a_test_command():
    """A guessed command would run nothing, exit 0 and read as verification."""
    got = P.Brief(goal="g")
    assert got.test_command == ""
    assert got.verifiable is False
    proved = P.Brief(goal="g", test_command="python -m pytest", test_from="pytest.ini")
    assert proved.verifiable is True
    assert proved.test_from == "pytest.ini"


def test_a_brief_payload_is_names_counts_and_notes_only():
    """No file content, no docstring text, no skill body — it leaves the machine."""
    got = _REAL_BRIEF("describe this repo").to_payload()
    assert set(got) >= {"goal", "project", "language", "managers", "tests",
                        "runners", "test_command", "doc", "doc_chars", "skills",
                        "notes", "verifiable"}
    assert isinstance(got["doc"], bool), "the doc is a fact about the file, not the file"
    assert isinstance(got["doc_chars"], int)
    body = json.dumps(got)
    assert "def " not in body
    assert '"""' not in body
    json.dumps(got)


def test_approval_needed_is_the_setting_and_an_explicit_ask(monkeypatch):
    monkeypatch.setattr(cfg, "ULTRACODE_APPROVAL", True)
    assert P.approval_needed() is True
    assert P.approval_needed(override=False) is False
    monkeypatch.setattr(cfg, "ULTRACODE_APPROVAL", False)
    assert P.approval_needed() is False
    assert P.approval_needed(override=True) is True
    assert P.approval_needed(override=None) is False


def test_graph_for_hangs_the_check_off_every_terminal():
    """One check node, fanning in over the nodes nothing depends on."""
    defn = _defn("a", "b:a", "c:a")
    graph, report = P.graph_for(defn, approval=False, check=True)
    ids = [n.id for n in graph.nodes]
    assert ids[:3] == ["a", "b", "c"]
    assert P.CHECK_ID in ids
    check = {n.id: n for n in graph.nodes}[P.CHECK_ID]
    assert set(check.needs) == {"b", "c"}
    assert check.kind == ST.K_CHECK
    assert check.priority == P.CHECK_PRIORITY
    assert set(report) == {"approval", "check", "roots", "terminals", "kinds", "note"}
    # ⚠️ `approval`/`check` hold the node's **id**, never a bool — `Launch.gate` and
    # `Launch.check` are properties straight off this report, and a surface needs the
    # id to name the gate a human must release. `""` is how "no such node" is said.
    assert report["check"] == P.CHECK_ID
    assert report["approval"] == ""
    assert sorted(report["terminals"]) == ["b", "c"]
    assert report["roots"] == ["a"]


def test_graph_for_omits_the_check_when_no_node_is_terminal():
    """A cycle has no terminal, and a check needing nothing would verify nothing."""
    graph, report = P.graph_for(_defn("a:b", "b:a"), approval=False, check=True)
    assert [n.id for n in graph.nodes] == ["a", "b"]
    assert report["check"] == ""
    assert report["terminals"] == []


def test_graph_for_puts_the_gate_under_every_root():
    """Nothing at all may start until a person releases it."""
    graph, report = P.graph_for(_defn("a", "b", "c:a"), approval=True, check=False)
    nodes = {n.id: n for n in graph.nodes}
    assert P.APPROVAL_ID in nodes
    gate = nodes[P.APPROVAL_ID]
    assert gate.needs == ()
    assert gate.kind == ST.K_APPROVAL
    assert gate.priority == P.APPROVAL_PRIORITY
    assert report["approval"] == P.APPROVAL_ID
    for root in ("a", "b"):
        assert P.APPROVAL_ID in nodes[root].needs, f"{root} does not wait on the gate"
    assert "c" not in report["roots"]
    assert P.APPROVAL_ID not in nodes["c"].needs


def test_the_gate_never_reaches_past_the_check_node():
    """The check depends on the work, so the gate holds the work — not the check."""
    graph, _ = P.graph_for(_defn("a", "b:a"), approval=True, check=True)
    nodes = {n.id: n for n in graph.nodes}
    assert P.APPROVAL_ID in nodes["a"].needs
    assert P.APPROVAL_ID not in nodes[P.CHECK_ID].needs
    assert nodes[P.CHECK_ID].needs == ("b",)


def test_graph_for_never_overwrites_a_planned_node_that_took_the_id():
    """A plan that already has an `approve` step keeps it; the gate renames."""
    graph, report = P.graph_for(_defn(P.APPROVAL_ID, "b:" + P.APPROVAL_ID),
                                approval=True, check=False)
    ids = [n.id for n in graph.nodes]
    assert ids.count(P.APPROVAL_ID) == 1
    assert f"{P.APPROVAL_ID}-2" in ids
    planted = {n.id: n for n in graph.nodes}[P.APPROVAL_ID]
    assert planted.kind != ST.K_APPROVAL, "the planner's own step was replaced"
    # ⚠️ The report names the gate that was actually BUILT, not the id it wanted —
    # a surface reads this to tell a human which node to release, so a rename that
    # was not carried through here would point at the planner's own step.
    assert report["approval"] == f"{P.APPROVAL_ID}-2"


def test_graph_for_reports_an_empty_plan_and_an_edgeless_gate():
    graph, report = P.graph_for(None, approval=True, check=True)
    assert report["note"] == "no plan to shape"
    graph, report = P.graph_for(_defn(), approval=True, check=True)
    assert report["note"] == "no plan to shape"
    # A graph with nodes but no root: the gate has nothing to hold, and says so.
    graph, report = P.graph_for(_defn("a:b", "b:a"), approval=True, check=False)
    assert report["roots"] == []
    assert "no root node" in report["note"]


def test_the_check_instruction_forbids_the_node_from_judging_the_run():
    """A model grading its own output is the failure `core/verify.py` names."""
    graph, _ = P.graph_for(_defn("a"), approval=False, check=True)
    text = {n.id: n for n in graph.nodes}[P.CHECK_ID].instruction
    assert "Do not judge whether the run succeeded" in text
    assert "not from your summary" in text
    # And with a proved command, that command is what the node is told to run.
    graph, _ = P.graph_for(_defn("a"), brief_=P.Brief(goal="g", test_command="pytest -q"),
                           approval=False, check=True)
    proved = {n.id: n for n in graph.nodes}[P.CHECK_ID].instruction
    assert "pytest -q" in proved
    assert "Do not judge whether the run succeeded" in proved


def test_plan_describe_and_its_exports():
    got = P.describe()
    assert set(got) == {"label", "knob", "surface", "source", "event", "max_nodes",
                        "approval_default", "approval_id", "check_id"}
    assert got["max_nodes"] == int(cfg.WORKFLOW_MAX_NODES) == P.max_nodes()
    assert got["knob"] == P.KNOB
    assert got["event"] == "ultracode_start" != "workflow_start"
    assert P.DEF_SOURCE == P.SURFACE == P.LABEL == "ultracode"
    assert len(P.__all__) == 15
    for name in P.__all__:
        assert hasattr(P, name), name
    json.dumps(got)


# ═══════════════════════════════════════════════════════════════════════════════
# 5 · start() — the ladder, the walk, and the five ways it degrades
# ═══════════════════════════════════════════════════════════════════════════════

def test_start_refuses_off_and_aimless_before_it_does_anything(monkeypatch):
    monkeypatch.setattr(cfg, "ULTRACODE_ENABLED", False)
    off = UC.start("build me a thing")
    assert (off.ok, off.reason) == (False, UC.U_OFF)
    assert off.stage == ST.S_UNDERSTAND, "a refusal before INSPECT is still UNDERSTAND"
    assert off.run_id == ""

    monkeypatch.setattr(cfg, "ULTRACODE_ENABLED", True)
    for aim in ("", "   ", None):
        blank = UC.start(aim)
        assert (blank.ok, blank.reason) == (False, UC.U_NO_AIM)
        assert blank.stage == ST.S_UNDERSTAND
    assert db.qall("SELECT id FROM exec_workflows") == []


def test_start_reads_the_project_before_it_asks_for_a_plan(monkeypatch):
    """DISCOVER SKILLS before PLAN, as a call order rather than as a comment."""
    order = []
    monkeypatch.setattr(P, "brief",
                        lambda goal, **kw: order.append("brief") or P.Brief(goal=goal))
    monkeypatch.setattr(DYN, "draft",
                        lambda goal, **kw: order.append("draft") or _draft(None,
                                                                          reason="nope"))
    out = UC.start("do the thing")
    assert order == ["brief", "draft"]
    assert (out.ok, out.reason) == (False, UC.U_PLANNER)
    assert out.stage == ST.S_PLAN
    assert out.note == "planner: nope"


def test_start_clips_a_very_long_goal():
    huge = "x" * 5000
    out = UC.start(huge)
    assert len(out.goal) == 2000
    out2 = UC.start("  keep   the words  ")
    assert out2.goal == "keep the words"


def test_start_refuses_an_unshaped_plan(monkeypatch):
    _plant(monkeypatch, draft=_draft(_defn()))
    out = UC.start("a goal")
    assert (out.ok, out.reason) == (False, UC.U_UNSHAPED)
    assert out.note == "no plan to shape"
    assert db.qall("SELECT id FROM exec_workflows") == []


def test_start_carries_the_validators_own_sentence(monkeypatch):
    """`U_ENGINE` never re-derives a refusal — the DAG's words reach the user.

    ⚠️ The sentence, not the code. `model.P_CYCLE` is the string `"cycle"` and it
    never appears in the prose: `validate.py` renders `"circular dependency: a → b →
    a"`, which *names the ring*. A test matching on the code would pass against an
    engine that had invented its own wording, which is the one thing this pins.
    """
    _plant(monkeypatch, "a:b", "b:a")
    out = UC.start("a goal", approval=False)
    assert (out.ok, out.reason) == (False, UC.U_ENGINE)
    assert "circular dependency" in out.note, out.note
    assert "a → b → a" in out.note, out.note
    assert out.run_id == ""
    assert db.qall("SELECT id FROM exec_workflows") == []


def test_start_builds_a_run_and_names_what_it_shaped(monkeypatch):
    _plant(monkeypatch, "a", "b:a")
    out = UC.start("ship it", approval=False)
    assert out.ok is True
    assert out.reason == ""
    assert out.run_id and out.session_id and out.name
    assert out.nodes == 3, "two planned steps plus the check node"
    assert out.stage == ST.S_EXECUTE
    # ⚠️ The spec's uppercase spelling is presentation and lives in the payload, not
    # on the dataclass — `stage` is the key, `stage_label` is what a human reads.
    assert out.to_payload()["stage_label"] == "EXECUTE"
    assert out.check == P.CHECK_ID
    assert out.gate == ""
    views = _views(out.run_id)
    assert set(views) == {"a", "b", P.CHECK_ID}
    assert views[P.CHECK_ID].kind == ST.K_CHECK
    assert views["a"].state in (M.READY, M.PENDING)
    assert set(out.to_payload()) == {
        "ok", "reason", "note", "run_id", "session_id", "goal", "name", "stage",
        "stage_label", "nodes", "gate", "check", "awaiting", "shape", "brief",
        "draft", "state"}


def test_start_restamps_the_definition_so_state_can_tell_whose_run_it_is(monkeypatch):
    """Both a `/workflow auto` run and this one are `dynamic` definitions until then."""
    _plant(monkeypatch, "a")
    out = UC.start("mine", approval=False)
    row = S.load(out.run_id)
    assert row.source == P.DEF_SOURCE
    assert row.source != DYN.DEF_SOURCE
    assert UC.state(out.run_id)["mine"] is True


def test_start_refuses_while_a_run_is_still_open_here(monkeypatch):
    """`U_LIVE` mirrors the 409 on `POST /api/workflows/<name>/run`, for its reason."""
    _plant(monkeypatch, "a")
    first = UC.start("one", approval=False)
    assert first.ok is True
    second = UC.start("two", approval=False)
    assert (second.ok, second.reason) == (False, UC.U_LIVE)
    assert second.run_id == first.run_id
    assert second.note == f"a run is still open here ({first.run_id})"
    assert second.state is not None
    assert len(db.qall("SELECT id FROM exec_workflows")) == 1


def test_a_broken_brief_costs_the_evidence_and_never_the_run(monkeypatch):
    """A brief is context, never permission."""
    def _boom(*a, **kw):
        raise RuntimeError("scan exploded")

    monkeypatch.setattr(P, "brief", _boom)
    _plant(monkeypatch, "a")
    out = UC.start("still ship it", approval=False)
    assert out.ok is True
    assert out.brief in ({}, None) or not out.brief.get("goal")
    assert "brief unavailable" in out.note


def test_an_unstampable_definition_costs_a_note_and_never_the_run(monkeypatch):
    """The fourth degradation note. `dataclasses.replace` can refuse; a run cannot."""
    frozen = _defn("a")

    def _no_replace(obj, **kw):
        raise TypeError("cannot replace")

    _plant(monkeypatch, draft=_draft(frozen))
    monkeypatch.setattr(E.dataclasses, "replace", _no_replace)
    out = UC.start("ship it", approval=False)
    assert out.ok is True
    assert "source not stamped" in out.note
    assert S.load(out.run_id).source != P.DEF_SOURCE


def test_start_parks_the_gate_out_of_recovery_reach(monkeypatch):
    """A gate is `tasks.pause()`'d, so it carries no stop checkpoint.

    That single fact is what separates *a human is holding this* from *a dead
    process abandoned it* — and it is why `create()`'s closing `advance()` cannot
    hand the gate to the first worker that asks.
    """
    _plant(monkeypatch, "a", "b:a")
    out = UC.start("ship it", approval=True)
    assert out.ok is True
    assert out.gate == P.APPROVAL_ID
    # ⚠️ `Launch.awaiting` is a **bool** while `Cycle.awaiting` is a list of ids, and
    # the asymmetry is deliberate: at launch there is exactly one gate and `gate`
    # already names it, so a second list would be a second declaration of it.
    assert out.awaiting is True
    views = _views(out.run_id)
    gate = views[P.APPROVAL_ID]
    assert gate.state == M.PAUSED
    assert gate.interrupted is False
    assert gate.dispatchable is False
    assert views["a"].dispatchable is False, "work must wait on the gate"
    # Recovery cannot touch it: no CP_STOPPED, so nothing was abandoned.
    assert S.release_interrupted(out.run_id) == []
    assert _views(out.run_id)[P.APPROVAL_ID].state == M.PAUSED
    assert [v.node for v in ST.awaiting_approval(S.load(out.run_id))] == [
        P.APPROVAL_ID]


# ═══════════════════════════════════════════════════════════════════════════════
# 6 · approve() and work() — the gate, the turn, and the check node
# ═══════════════════════════════════════════════════════════════════════════════

def test_approve_releases_the_gate_row_and_nothing_else(monkeypatch):
    """Never `unpause_run()`: a person may have paused work on purpose."""
    _plant(monkeypatch, "a", "b:a")
    out = UC.start("ship it", approval=True)
    held = _views(out.run_id)["a"]
    T.pause(held.task_id, notify=False)          # a human holding one step

    got = UC.approve(out.run_id)
    assert got.ok is True
    assert got.released == [P.APPROVAL_ID]
    views = _views(out.run_id)
    assert views[P.APPROVAL_ID].state != M.PAUSED
    assert views["a"].state == M.PAUSED, "a hand-paused step was released too"
    assert ST.awaiting_approval(S.load(out.run_id)) == []


def test_approve_reports_both_reasons_for_having_no_gate(monkeypatch):
    monkeypatch.setattr(cfg, "ULTRACODE_ENABLED", False)
    assert UC.approve("nope").reason == UC.U_OFF
    monkeypatch.setattr(cfg, "ULTRACODE_ENABLED", True)
    assert UC.approve("nope").reason == UC.U_MISSING
    assert UC.approve("").reason == UC.U_MISSING

    _plant(monkeypatch, "a")
    out = UC.start("ship it", approval=False)
    ungated = UC.approve(out.run_id)
    assert (ungated.ok, ungated.reason) == (False, UC.U_NO_GATE)
    assert ungated.note == "no node is awaiting approval"
    assert ungated.stage in ST.DERIVED


def test_approve_refuses_a_settled_run(monkeypatch):
    _plant(monkeypatch, "a")
    out = UC.start("ship it", approval=False)
    S.cancel_run(out.run_id)
    got = UC.approve(out.run_id)
    assert (got.ok, got.reason) == (False, UC.U_SETTLED)
    assert got.state is not None


def test_work_refuses_a_missing_run_without_a_state(monkeypatch):
    """`U_MISSING` leaves `state` None; `U_SETTLED` sets it. Two different facts."""
    monkeypatch.setattr(cfg, "ULTRACODE_ENABLED", False)
    assert UC.work("x", _ok).reason == UC.U_OFF
    monkeypatch.setattr(cfg, "ULTRACODE_ENABLED", True)
    missing = UC.work("no-such-run", _ok)
    assert (missing.ok, missing.reason) == (False, UC.U_MISSING)
    assert missing.state is None

    _plant(monkeypatch, "a")
    out = UC.start("ship it", approval=False)
    S.cancel_run(out.run_id)
    settled = UC.work(out.run_id, _ok)
    assert (settled.ok, settled.reason) == (False, UC.U_SETTLED)
    assert settled.state is not None
    assert settled.stage == ST.S_FINALIZE


def test_work_will_not_start_a_node_while_the_gate_stands(monkeypatch):
    """And it runs the moment the gate is released — same driver, same graph."""
    _plant(monkeypatch, "a", "b:a")
    out = UC.start("ship it", approval=True)
    ran = []

    def _turn(ctx):
        ran.append(ctx.node)
        return True

    held = UC.work(out.run_id, _turn, caps=_inline())
    assert (held.ok, held.reason) == (False, UC.U_GATE)
    assert held.note == "approval is required before any node may start"
    assert held.awaiting == [P.APPROVAL_ID]
    assert ran == [], "a node ran with the gate standing"

    UC.approve(out.run_id)
    freed = UC.work(out.run_id, _turn, caps=_inline())
    assert freed.ok is True
    assert set(ran) == {"a", "b"}, ran
    assert S.load(out.run_id).finished is True


def test_work_releases_what_a_dead_process_parked_and_names_it(monkeypatch):
    """Recovery is automatic; a silent release is indistinguishable from a node
    that was never stuck."""
    _plant(monkeypatch, "a", "b:a")
    out = UC.start("ship it", approval=False)
    # ⚠️ A node has to be CLAIMED before a crash could have abandoned it:
    # `tasks.interrupt()` filters on `HELD` (running · queued) and `store._insert`
    # creates every node PENDING, so an unclaimed graph parks nothing at all. The
    # claim is the status write that makes the row look taken — which is precisely
    # the state a process that then died leaves behind.
    assert S.claim(out.run_id, "a") is True
    T.interrupt(out.session_id)
    parked = [v.node for v in S.load(out.run_id).nodes if v.interrupted]
    assert parked, "the fixture did not park anything"

    got = UC.work(out.run_id, _ok, caps=_inline())
    assert got.released, "an abandoned node was released in silence"
    assert set(got.released) <= set(parked)
    assert not [v for v in S.load(out.run_id).nodes if v.interrupted]


def test_the_check_node_asks_core_verify_and_never_the_turn(monkeypatch):
    """*"'Done' is not verification"* — the node reports, `core/verify.py` judges."""
    _plant(monkeypatch, "a")
    out = UC.start("ship it", approval=False)
    seen, asked = [], []

    def _turn(ctx):
        seen.append(ctx.node)
        return True

    # ⚠️ `E._verify` IS `V` — the same module object, not a copy — so a spy that
    # called `V.verify_tasks(...)` would resolve the name at call time and find
    # *itself*. Bind the real function first; the recursion is otherwise silent
    # until the interpreter runs out of stack.
    real = V.verify_tasks
    monkeypatch.setattr(E._verify, "verify_tasks",
                        lambda ids, **kw: asked.append((tuple(ids), kw))
                        or real(ids, **kw))
    got = UC.work(out.run_id, _turn, caps=_inline())
    assert got.ok is True
    assert seen == ["a"], "the check node was handed to the model"
    assert len(asked) == 1
    ids, kwargs = asked[0]
    work_ids = {v.task_id for v in S.load(out.run_id).nodes if v.node == "a"}
    assert set(ids) == work_ids, "the check judged itself or the gate"
    assert kwargs["ref"] == out.run_id
    assert kwargs["session_id"] == out.session_id
    row = [v for v in S.load(out.run_id).nodes if v.node == P.CHECK_ID][0]
    assert row.state == M.COMPLETED


def test_the_check_node_reads_the_graph_live(monkeypatch):
    """It re-reads inside the node, so it sees work that finished after it planned."""
    _plant(monkeypatch, "a", "b:a")
    out = UC.start("ship it", approval=False)
    got = UC.work(out.run_id, _ok, caps=_inline())
    assert got.ok is True
    result = {v.node: v for v in S.load(out.run_id).nodes}[P.CHECK_ID]
    assert result.state == M.COMPLETED
    # ⚠️ The verdict text is on the TASK ROW, not on the `NodeView` — `to_payload()`
    # carries seventeen keys and `result` is not among them, because a node view is
    # what a surface renders and a result is what the worker wrote. Nothing was
    # recorded either way here, so the honest word is "unconfirmed".
    wrote = T.get(result.task_id)
    assert "unconfirmed" in (wrote.result or "")


def test_a_check_node_with_no_work_says_so(monkeypatch):
    """A gate and a check alone: there is nothing to verify, and that is not a fail."""
    _plant(monkeypatch, draft=_draft(_defn("a")))
    out = UC.start("ship it", approval=False)
    ids = E._work_ids(S.load(out.run_id))
    assert ids, "the planned step is work"
    gate_only = _St([_V(P.CHECK_ID, M.PENDING, ST.K_CHECK),
                     _V(P.APPROVAL_ID, M.PAUSED, ST.K_APPROVAL)])
    assert E._work_ids(gate_only) == []


def test_a_pump_that_raises_is_reported_not_propagated(monkeypatch):
    """Nothing on this path may raise into a turn — including the scheduler."""
    _plant(monkeypatch, "a")
    out = UC.start("ship it", approval=False)

    def _boom(*a, **kw):
        raise RuntimeError("the pump broke")

    monkeypatch.setattr(E._sched, "run", _boom)
    got = UC.work(out.run_id, _ok, caps=_inline())
    assert (got.ok, got.reason) == (False, UC.U_ENGINE)
    assert got.note == "the pump broke"
    assert set(got.to_payload()) >= {"ok", "reason", "note", "run_id", "stage",
                                     "stage_label", "released", "awaiting"}


def test_work_passes_its_own_worker_id(monkeypatch):
    """`WORKER` is UltraCode's, not the pump's — two drivers, two names."""
    _plant(monkeypatch, "a")
    out = UC.start("ship it", approval=False)
    seen = {}
    real = E._sched.run

    def _spy(run_id, worker, **kw):
        seen.update(kw)
        return real(run_id, worker, **kw)

    monkeypatch.setattr(E._sched, "run", _spy)
    UC.work(out.run_id, _ok, caps=_inline())
    assert seen.get("worker_id") == UC.WORKER == "ultracode"


# ═══════════════════════════════════════════════════════════════════════════════
# 7 · replan · finalize · drive · cancel · state
# ═══════════════════════════════════════════════════════════════════════════════

def test_replan_returns_the_planners_own_dataclass(monkeypatch):
    """A fifth engine dataclass wrapping a `Draft` would be a second spelling of it."""
    monkeypatch.setattr(cfg, "ULTRACODE_ENABLED", False)
    off = UC.replan("x", goal="fix it")
    assert isinstance(off, DYN.Draft)
    assert (off.ok, off.reason) == (False, UC.U_OFF)
    assert off.goal == "fix it"

    monkeypatch.setattr(cfg, "ULTRACODE_ENABLED", True)
    seen = {}

    def _spy(run_id, **kw):
        seen.update(dict(kw, run_id=run_id))
        return DYN.Draft(reason="spied")

    monkeypatch.setattr(DYN, "replan", _spy)
    got = UC.replan("run-7", goal="fix it", max_steps=3)
    assert isinstance(got, DYN.Draft)
    assert seen["run_id"] == "run-7"
    assert seen["kind"] == ST.K_FIX, "a remediation step must be legible as one"
    assert seen["max_steps"] == 3


def test_replan_reaches_the_real_planner_for_a_live_run(monkeypatch):
    """A run with nothing failed has nothing to react to — the planner says so."""
    _plant(monkeypatch, "a")
    out = UC.start("ship it", approval=False)
    got = UC.replan(out.run_id)
    assert isinstance(got, DYN.Draft)
    assert got.ok is False
    assert got.reason == DYN.X_NO_FAILURE


def test_finalize_refuses_to_settle_an_open_run(monkeypatch):
    _plant(monkeypatch, "a", "b:a")
    out = UC.start("ship it", approval=False)
    got = UC.finalize(out.run_id)
    assert (got.ok, got.reason) == (False, UC.U_OPEN)
    st = S.load(out.run_id)
    assert got.note == f"{st.done} of {st.total} nodes settled"
    assert got.stage == ST.S_EXECUTE, "an open run is not verifying"
    assert S.load(out.run_id).finished is False


def test_finalize_is_the_only_thing_that_reports_finished(monkeypatch):
    _plant(monkeypatch, "a")
    out = UC.start("ship it", approval=False)
    UC.work(out.run_id, _ok, caps=_inline())
    assert ST.stage_of(S.load(out.run_id)) == ST.S_FINALIZE

    got = UC.finalize(out.run_id)
    assert got.ok is True
    assert got.stage == ST.S_FINISHED
    # ⚠️ The spec's uppercase spelling is presentation and lives in the payload —
    # `stage` is the key every consumer compares, `stage_label` is what a human reads.
    payload = got.to_payload()
    assert payload["stage_label"] == "COMPLETE"
    # ⚠️ `failed`, not `settled`: a `Finish` names the nodes that did not make it,
    # because "which ones" is the only part of a settled run a human can act on.
    assert set(payload) == {"ok", "reason", "note", "run_id", "stage",
                            "stage_label", "verified", "failed", "report", "state"}


def test_finalize_ok_is_not_verified(monkeypatch):
    """`ok` and `verified` are two questions, and `core/verify.py`'s calibration is why.

    ⚠️ AN ALL-`unconfirmed` RUN IS STILL `verified`, AND THAT IS DELIBERATE.
    `Report.verified` is `ok and complete and not unsuccessful`; it does **not**
    require every unit to be `V_CONFIRMED`, because a node whose whole job is to read
    and reason records nothing measurable — requiring confirmation would refuse to
    finish every run containing one, which is the false negative that fires on every
    run. So an unconfirmed verdict is a **warning**, never a contradiction.

    What separates the two facts is therefore the path with no report at all: the
    work settled (`ok`) and nothing judged it (`verified is False`). That is
    `Report.ok is not Report.verified`, one layer up.
    """
    _plant(monkeypatch, "a")
    out = UC.start("ship it", approval=False)
    UC.work(out.run_id, _ok, caps=_inline())
    got = UC.finalize(out.run_id)
    assert got.ok is True
    assert got.verified is True, "settled and unopposed — verify.py's own calibration"
    # ⚠️ `Finish.report` is `core.verify`'s own `Report` OBJECT, not a dict — the
    # payload is where it becomes one, so a consumer reading the dataclass and a
    # consumer reading the payload cannot disagree about a verdict.
    assert isinstance(got.report, V.Report)
    counts = got.report.counts
    assert counts[V.V_CONFIRMED] == 0, "nothing was recorded, so nothing is proved"
    assert counts[V.V_UNCONFIRMED] == 1, counts
    assert not got.report.problems, "an unconfirmed unit is a warning, never a problem"
    assert got.to_payload()["report"]["counts"] == counts


def test_finalize_survives_a_broken_verifier(monkeypatch):
    _plant(monkeypatch, "a")
    out = UC.start("ship it", approval=False)
    UC.work(out.run_id, _ok, caps=_inline())

    def _boom(*a, **kw):
        raise RuntimeError("ledger unreadable")

    monkeypatch.setattr(E._verify, "verify_tasks", _boom)
    got = UC.finalize(out.run_id)
    assert "verification unavailable" in got.note
    assert got.stage == ST.S_FINISHED
    assert got.ok is True
    # ⚠️ THIS is where `ok` and `verified` come apart: the work settled, and nothing
    # judged it. An unreadable ledger may never read as a confirmation.
    assert got.verified is False
    assert got.report is None


def test_drive_names_which_allowance_ran_out(monkeypatch):
    """One clock, checked between cycles — never handed to the pump."""
    _plant(monkeypatch, "a")
    out = UC.start("ship it", approval=False)
    spins = []

    def _spin(run_id, turn, **kw):
        spins.append(run_id)
        return E.Cycle(ok=True, run_id=run_id, reason=SC.R_BLOCKED,
                       stage=ST.S_EXECUTE, state=S.load(run_id))

    monkeypatch.setattr(E, "work", _spin)
    monkeypatch.setattr(E, "replan", _growing_replan())
    got = UC.drive(out.run_id, _ok, cycles=2)
    assert (got.ok, got.reason) == (False, UC.U_CYCLES)
    assert got.note == "2 of 2 cycles spent"
    assert got.spent == 2 == len(spins)
    assert got.elapsed >= 0.0


def test_drive_reports_a_spent_wall_clock_budget(monkeypatch):
    _plant(monkeypatch, "a")
    out = UC.start("ship it", approval=False)
    clock = {"t": 1000.0}
    monkeypatch.setattr(E, "monotonic", lambda: clock["t"])

    def _spin(run_id, turn, **kw):
        clock["t"] += 10.0
        return E.Cycle(ok=True, run_id=run_id, reason=SC.R_BLOCKED,
                       stage=ST.S_EXECUTE, state=S.load(run_id))

    monkeypatch.setattr(E, "work", _spin)
    monkeypatch.setattr(E, "replan", _growing_replan())
    got = UC.drive(out.run_id, _ok, cycles=9, budget_sec=5)
    assert (got.ok, got.reason) == (False, UC.U_BUDGET)
    assert got.note == "5s wall-clock allowance spent"
    assert got.spent == 1


def test_drive_off_never_starts_a_clock(monkeypatch):
    monkeypatch.setattr(cfg, "ULTRACODE_ENABLED", False)
    off = UC.drive("x", _ok)
    assert (off.ok, off.reason) == (False, UC.U_OFF)
    assert off.elapsed == 0.0
    monkeypatch.setattr(cfg, "ULTRACODE_ENABLED", True)
    missing = UC.drive("no-such-run", _ok)
    assert missing.reason == UC.U_MISSING
    assert missing.elapsed >= 0.0


def test_drive_stops_on_a_cancel_token_without_inventing_a_note(monkeypatch):
    _plant(monkeypatch, "a")
    out = UC.start("ship it", approval=False)
    token = threading.Event()
    token.set()
    got = UC.drive(out.run_id, _ok, cancel_event=token)
    assert got.reason == SC.R_CANCELLED
    assert got.note == ""
    assert got.spent == 0
    assert S.load(out.run_id).finished is False, "a cancel token is not a settle"


def test_drive_reports_both_ways_a_replan_can_fail(monkeypatch):
    _plant(monkeypatch, "a")
    out = UC.start("ship it", approval=False)

    def _spin(run_id, turn, **kw):
        return E.Cycle(ok=True, run_id=run_id, reason=SC.R_EXHAUSTED,
                       stage=ST.S_EXECUTE, state=S.load(run_id))

    monkeypatch.setattr(E, "work", _spin)
    monkeypatch.setattr(E, "replan", lambda rid, **kw: DYN.Draft(reason="dry"))
    refused = UC.drive(out.run_id, _ok, cycles=3)
    assert (refused.ok, refused.reason) == (False, UC.U_PLANNER)
    assert refused.note == "re-plan: dry"

    # A plan that succeeded and added nothing is a different, quieter failure.
    monkeypatch.setattr(E, "replan",
                        lambda rid, **kw: _draft(_defn("a"), goal="g"))
    total = S.load(out.run_id).total
    empty = UC.drive(out.run_id, _ok, cycles=3)
    assert (empty.ok, empty.reason) == (False, UC.U_PLANNER)
    assert empty.note == f"the re-plan added no node ({total} before, {total} after)"


def test_drive_carries_a_cycles_refusal_out_unchanged(monkeypatch):
    _plant(monkeypatch, "a")
    out = UC.start("ship it", approval=False)
    monkeypatch.setattr(E, "work",
                        lambda rid, turn, **kw: E.Cycle(ok=False, run_id=rid,
                                                        reason=UC.U_GATE,
                                                        note="held", stage=ST.S_OBSERVE))
    got = UC.drive(out.run_id, _ok, cycles=4)
    assert (got.ok, got.reason, got.note) == (False, UC.U_GATE, "held")
    assert got.spent == 1
    assert got.finish is None


def test_drive_finishes_a_real_graph_end_to_end(monkeypatch):
    """Start → EXECUTE → VERIFY → FINALIZE, one call, no stubbed cycle."""
    _plant(monkeypatch, "a", "b:a", "c:b")
    out = UC.start("ship it", approval=False)
    got = UC.drive(out.run_id, _ok, cycles=4)
    assert got.ok is True, (got.reason, got.note)
    assert got.stage == ST.S_FINISHED
    assert got.finish is not None and got.finish.ok is True
    assert got.spent >= 1
    assert S.load(out.run_id).finished is True
    # ⚠️ `replans`, and deliberately NO `state`. A `Loop` is the whole driven run,
    # so the graph it ends on is `finish.state` — carrying a second copy at the top
    # level would be two answers to "what do the rows say now", and the shallower one
    # is the one a renderer would reach for.
    assert set(got.to_payload()) == {"ok", "reason", "note", "run_id", "stage",
                                     "stage_label", "spent", "replans", "elapsed",
                                     "cycles", "drafts", "finish", "verified"}
    assert got.to_payload()["replans"] == len(got.drafts)
    assert got.to_payload()["finish"]["state"]["finished"] is True


def test_cancel_lands_on_finalizing_and_is_never_ok(monkeypatch):
    """Cancelled rows are settled, but nobody took a verdict — so not `finished`."""
    monkeypatch.setattr(cfg, "ULTRACODE_ENABLED", False)
    assert UC.cancel("x").reason == UC.U_OFF
    monkeypatch.setattr(cfg, "ULTRACODE_ENABLED", True)
    assert UC.cancel("nope").reason == UC.U_MISSING

    _plant(monkeypatch, "a", "b:a")
    out = UC.start("ship it", approval=False)
    got = UC.cancel(out.run_id, reason="the user pressed stop")
    assert got.ok is False, "a cancel is not a success"
    assert got.reason == SC.R_CANCELLED
    assert got.note == "the user pressed stop"
    assert got.stage == ST.S_FINALIZE
    assert got.stage != ST.S_FINISHED
    assert S.load(out.run_id).finished is True
    again = UC.cancel(out.run_id)
    assert again.reason == UC.U_SETTLED


def test_state_filters_on_the_source_and_start_does_not(monkeypatch):
    """Two different questions. `start()` asks *may a run begin here* — and a live
    `/workflow run` is just as much a reason to say no."""
    empty = UC.state()
    assert set(empty) == {"ok", "reason", "run", "stage", "stage_label",
                          "awaiting", "mine", "policy"}
    # ⚠️ `ok` HERE IS THE POSTURE, NOT A RUN — and `U_MISSING` is deliberately never
    # this reader's answer. "Nothing is happening" is a complete answer to *what is
    # happening*, so only the switch being off can make this call unsuccessful; the
    # four entry points that ACT are the ones that refuse a run they cannot find.
    assert (empty["ok"], empty["reason"]) == (True, "")
    assert empty["run"] is None
    assert empty["mine"] is False
    assert empty["stage"] == "" and empty["awaiting"] == []

    _plant(monkeypatch, "a")
    out = UC.start("ship it", approval=False)
    live = UC.state()
    assert live["ok"] is True
    assert live["mine"] is True
    assert live["run"]["run_id"] == out.run_id
    assert live["stage"] in ST.DERIVED
    assert live["policy"] == UC.describe()
    json.dumps(live)

    # A foreign run is still live enough to refuse a start, and is not "mine".
    db.exe("UPDATE exec_workflows SET state = REPLACE(state, ?, ?)",
           (f'"{P.DEF_SOURCE}"', '"planner"'))
    S.load.cache_clear() if hasattr(S.load, "cache_clear") else None
    foreign = UC.state()
    assert foreign["mine"] is False
    assert UC.start("another", approval=False).reason == UC.U_LIVE


def test_engine_describe_nests_the_other_two():
    got = UC.describe()
    assert set(got) == {"enabled", "max_cycles", "budget_sec", "approval",
                        "worker", "refusals", "replan_on", "plan", "stages"}
    assert got["worker"] == UC.WORKER
    assert got["refusals"] == list(E.REFUSALS)
    assert got["plan"] == P.describe()
    assert got["stages"] == ST.describe()
    assert sorted(got["replan_on"]) == sorted((SC.R_BLOCKED, SC.R_EXHAUSTED))
    assert UC.describe is E.describe
    json.dumps(got)


# ═══════════════════════════════════════════════════════════════════════════════
# 8 · Structure — this package holds no fact of its own
# ═══════════════════════════════════════════════════════════════════════════════

#: ⚠️ `replace` IS DELIBERATELY ABSENT, and leaving it in was a false positive that
#: read as a real one. `_called()` collects an attribute call by its *attribute name*
#: alone, so `dataclasses.replace(out, …)` — how every one of these dataclasses is
#: updated without mutating it — is indistinguishable from `Path.replace()` to this
#: census. Keeping the word would fail the test on the very idiom the module is
#: written in, and the filesystem verb it was aimed at is already covered by
#: `rename`/`unlink`/`write_text`.
_WRITERS = {"write_text", "write_bytes", "mkdir", "unlink", "rename",
            "rmtree", "touch", "copy", "copy2", "move", "symlink_to"}


def test_nothing_here_writes_a_file_or_opens_the_database():
    """A plan is prose somebody wrote; the rows are `core/dag/store.py`'s job."""
    for name, path in sorted(_MODULES.items()):
        source = Path(path).read_text(encoding="utf-8")
        tree = ast.parse(source)
        calls = _called(tree)
        assert not (calls & _WRITERS), f"{name} writes: {calls & _WRITERS}"
        assert "open" not in calls, f"{name} opens a file"
        for banned in ("import sqlite3", "from agent2 import database",
                       "from agent2.database import", "import agent2.database"):
            assert banned not in source, f"{name} reaches the database directly"
        for verb in ("INSERT INTO", "UPDATE ", "DELETE FROM", "CREATE TABLE"):
            code = "\n".join(l for l in source.splitlines()
                             if not l.strip().startswith("#"))
            assert verb not in _stripped(tree, code), f"{name} spells SQL: {verb}"


def _stripped(tree, code):
    """*code* with every docstring removed — prose may name what code may not do."""
    docs = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            text = ast.get_docstring(node, clean=False)
            if text:
                docs.append(text)
    for text in docs:
        code = code.replace(text, "")
    return code


_DAG_PREDICATES = {"find_cycles", "levels_for", "validate", "validate_mutation",
                   "plan_next", "ready", "blockers", "waves", "in_flight",
                   "may_repeat", "dispatchable"}


def test_the_package_calls_no_dag_predicate_of_its_own():
    """The engine decides structure; this package decides what the run is ABOUT.

    A call ban over the `ast`, never a text sweep: `stages.py` legitimately *reads*
    `v.dispatchable` and all three modules name these functions in prose on
    purpose — saying which module owns a fact is how the one-declaration rule is
    written down here.
    """
    for name, path in sorted(_MODULES.items()):
        calls = _called(_tree(path))
        assert not (calls & _DAG_PREDICATES), f"{name} calls {calls & _DAG_PREDICATES}"
    # And the read it IS allowed is still there, so the ban above is not vacuous.
    assert "dispatchable" in Path(_MODULES["stages.py"]).read_text(encoding="utf-8")


def test_the_engine_reaches_the_dag_only_through_store_and_schedule():
    """`store.{create,load,advance,settle,cancel_run,live,release_interrupted}` and
    `schedule.run` — not a second engine."""
    source = Path(_MODULES["engine.py"]).read_text(encoding="utf-8")
    tree = ast.parse(source)
    reached = {n.func.attr for n in ast.walk(tree)
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
               and isinstance(n.func.value, ast.Name)
               and n.func.value.id in {"_store", "_sched"}}
    assert reached <= {"create", "load", "advance", "settle", "cancel_run", "live",
                       "release_interrupted", "extend", "plan", "run", "limits",
                       "outcome_of", "NodeOutcome"}, reached


def test_no_payload_here_carries_a_step_instruction(monkeypatch):
    """A step's text is a prompt about somebody's own checkout."""
    _plant(monkeypatch, "a", "b:a")
    launch = UC.start("ship it", approval=True)
    assert SECRET in _views(launch.run_id)["a"].instruction, "the canary was not planted"

    payloads = [launch.to_payload(), UC.state(launch.run_id),
                UC.approve(launch.run_id).to_payload()]
    payloads.append(UC.work(launch.run_id, _ok, caps=_inline()).to_payload())
    payloads.append(UC.finalize(launch.run_id).to_payload())
    payloads.append(UC.drive(launch.run_id, _ok, cycles=1).to_payload())
    payloads.append(_REAL_BRIEF("ship it").to_payload())
    for got in payloads:
        body = json.dumps(got, default=str)
        assert SECRET not in body, got.get("reason", "")


def test_the_four_dataclasses_are_json_and_never_raise():
    """Every entry point returns one of these, so an empty one must render."""
    for maker in (E.Launch, E.Cycle, E.Finish, E.Loop):
        got = maker()
        payload = got.to_payload()
        assert payload["ok"] is False
        assert payload["stage_label"] == ST.STAGE_LABEL.get(payload["stage"],
                                                            payload["stage"])
        json.dumps(payload, default=str)
    assert E.Launch().stage == ST.S_UNDERSTAND


def test_the_facade_re_exports_the_engine_and_deliberately_not_two_describes():
    assert len(E.__all__) == 28
    assert len(ST.__all__) == 30
    assert len(UC.__all__) == 74
    for mod in (E, P, ST, UC):
        assert sorted(set(mod.__all__)) == sorted(mod.__all__), "a duplicate export"
        for name in mod.__all__:
            assert hasattr(mod, name), f"{mod.__name__}.{name}"
    # Every name all three modules export is reachable from the facade — including
    # `describe`, which all three declare.
    assert set(E.__all__) - set(UC.__all__) == set()
    assert set(P.__all__) - set(UC.__all__) == set()
    assert set(ST.__all__) - set(UC.__all__) == set()
    # ⚠️ SO THE NAME SET CANNOT PIN *WHOSE* `describe` THAT IS, AND THAT IS THE ONE
    # FACT THIS TEST EXISTS FOR. Three modules define one; the facade re-exports the
    # engine's, which nests the other two, so a surface asking "what is this
    # subsystem's posture" gets one answer rather than three that agree until
    # somebody edits one of them. Identity is what says so — a name comparison would
    # pass against a facade that had re-exported any of the three.
    assert UC.describe is E.describe
    assert UC.describe is not P.describe
    assert UC.describe is not ST.describe
    # The other two are reachable, and only, through their own module object.
    assert UC.plan.describe is P.describe
    assert UC.stages.describe is ST.describe
    # And the engine's really does nest them, so "one answer" is not merely one name.
    nested = UC.describe()
    assert nested["plan"] == P.describe() and nested["stages"] == ST.describe()
