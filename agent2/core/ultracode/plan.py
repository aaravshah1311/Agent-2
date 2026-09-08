"""What UltraCode knows before it plans, and the shape of the graph that comes out.

Two jobs, both of them deliberately **pure**: nothing here writes a row, starts a
run, calls a model or asks a scheduler for anything. `engine.py` is the driver;
this module is what it consults and what it shapes.

⚠️ **THE EVIDENCE IS BORROWED, NEVER RE-DERIVED.** UNDERSTAND · INSPECT · DISCOVER
are three stages of the spec's loop and every fact they need already has exactly one
owner: what this project *is* is `core.projectscan.scan()`, what its own doc says is
`core.projectdoc`, and which of its skills bear on a goal is
`core.skills.for_turn()`. So `brief()` is a **projection** over those three and holds
no rule of its own — a second project walk or a second skill selector would be the
`if workflow:` of discovery, and the copy is always the one that goes stale.

⚠️ **THE ONE FACT THE BRIEF ADDS IS A *PROVED* TEST COMMAND.** `projectscan` will not
guess a runner (`a.test.ts` admits Jest, Vitest and Mocha, so it proves none), and
that discipline is what makes the field usable here: a `check` node whose command was
guessed would run nothing, exit 0 and read as verification — the exact false
*confirmed* `core/verify.py` exists to prevent. No proved command therefore means the
check node runs **nothing** and says so, rather than inventing `npm test`.

⚠️ **THE BRIEF IS NOT THE PLANNER'S PROMPT.** `dynamic.draft()` assembles its own
context through `core.broker`, which already carries the project doc *and* a skills
selection; handing it a second copy of either would be two spellings of one prompt.
What the brief is for is the other direction — it is what a **human** is shown before
approving autonomous execution, and it is what makes INSPECT and DISCOVER stages a
run can *demonstrate* rather than claim. The skills it names are the ones that bear
on the **goal**; the ones that reach a node's prompt are selected over that node's
own instruction, by the broker, at the moment the node runs. Two questions, one
selector, and the difference is the feature.

⚠️ **THE CEILING IS BORROWED TOO, AND THAT IS THE INTERESTING ONE.**
`config.WORKFLOW_MAX_NODES` bounds an UltraCode graph, injected under
`workflow.graph.KNOB`, because `dynamic.replan()` — which is how D5.40 grows a live
graph — passes exactly that pair to `store.extend()`. Creating a graph under the DAG
core's own 512 and re-planning it under the planner's 64 produces a run that can be
started and can never be extended: a silent half-broken state that only shows up on
the first failure, which is the worst possible moment to discover a ceiling. One
number bounds this graph at creation and at every extension, or the two disagree.
Raising it is a one-line change *in `dynamic.replan()`*, not here.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from agent2 import config
from agent2.core.dag import model as _m
from agent2.core.workflow import graph as _wgraph

from . import stages as _st

__all__ = [
    "APPROVAL_ID",
    "APPROVAL_TITLE",
    "CHECK_ID",
    "CHECK_TITLE",
    "DEF_SOURCE",
    "EVENT",
    "KNOB",
    "LABEL",
    "SURFACE",
    "Brief",
    "approval_needed",
    "brief",
    "describe",
    "graph_for",
    "max_nodes",
]


# ── What this consumer is called, wherever the engine had to be told ───────────
# ⚠️ The four words below are exactly what `core/dag/` refuses to know. `label`
# spells a refusal (*"an ultracode may declare at most 64 nodes"*), `knob` names the
# variable that would raise it, `event` is the word a `sync` subscriber sees, and
# `source` travels with the graph so a run read back a week later still says which
# planner produced it. Injected, never branched on — PART 1's rule made mechanical.

#: The noun a refusal uses. `validate(…, label=LABEL)`.
LABEL = "ultracode"
#: The variable a refusal tells the author to raise. ⚠️ Deliberately the *workflow*
#: knob — see the module docstring; a knob of our own would name a number that does
#: not bound the extension path.
KNOB = _wgraph.KNOB
#: `tasks.open_session(surface=…)` — which surface owns the session's rows.
SURFACE = "ultracode"
#: `Graph.source` — provenance that travels with the graph.
DEF_SOURCE = "ultracode"
#: The `sync` event `store.create()` publishes. Not `"workflow_start"`: a listener
#: filtering for workflows must not see an UltraCode run start.
EVENT = "ultracode_start"

#: Preferred ids for the two nodes this module adds. Both go through `_free_id()`,
#: so a plan that already used the name gets `approve-2` rather than a silent
#: replacement — `Graph.with_nodes()` replaces by id, and replacing a planned step
#: with a gate would delete work nobody agreed to drop.
APPROVAL_ID = "approve"
CHECK_ID = "verify"
APPROVAL_TITLE = "Approve autonomous execution"
CHECK_TITLE = "Verify this run's work"

#: The gate runs before anything, so it sorts before anything. `plan_next()` orders
#: by `(priority, seq, node)` and a gate that lost a tie would be listed under work
#: that cannot start until it clears.
APPROVAL_PRIORITY = 1
#: The check runs last by dependency; the priority only breaks a tie against another
#: node in the same wave, and there is normally nothing else in it.
CHECK_PRIORITY = 5

#: How much of a brief may reach a payload. Every field here is a name, a count or a
#: command — never prose a human wrote — but the note lines are composed text and a
#: surface should not have to defend itself against one.
MAX_NOTE = 200
MAX_NOTES = 8
MAX_SKILLS = 12
MAX_MANAGERS = 6
MAX_RUNNERS = 6


def max_nodes() -> int:
    """The node ceiling for an UltraCode graph, read live. See the module docstring."""
    return int(config.WORKFLOW_MAX_NODES)


# ── The brief ─────────────────────────────────────────────────────────────────


@dataclass
class Brief:
    """What one `/ultracode` run was told about the world before it planned.

    Data only, and every field is a name, a count, a command or a note — no file
    content, no docstring text, no skill body. A brief is printed to a terminal, sent
    to a browser and (for `test_command`) turned into a node instruction, so prose
    somebody wrote in their own checkout has no business in it. That boundary is
    `projectscan`'s `file_notes` rule, one layer up: the notes exist, `/init` sends
    them to a narrator on purpose, and nothing here forwards them.
    """

    goal: str = ""
    root: str = ""
    #: `projectscan.summary()` — one line, e.g. `Python · pip · 1836 tests · main`.
    project: str = ""
    name: str = ""
    language: str = ""
    managers: tuple[str, ...] = ()
    #: Test **files** counted, and the runners actually proved. Two facts: files
    #: without a runner is the honest report `projectscan` is shaped to produce.
    tests: int = 0
    runners: tuple[str, ...] = ()
    #: A command `projectscan` *proved*, and what proved it. `""` means no runner was
    #: established — never a guess. See the module docstring.
    test_command: str = ""
    test_from: str = ""
    #: Whether this project has an `.agent2/agent2.md`, and how big it is. The doc
    #: itself is NOT carried: the broker already sends it into every prompt, and a
    #: copy frozen at plan time would drift from the file for the rest of the run.
    doc: bool = False
    doc_chars: int = 0
    #: Skill ids the goal selects, and why each one — `skills.select.REASONS`' own
    #: word, never a second judgement.
    skills: tuple[str, ...] = ()
    skill_reasons: tuple[dict, ...] = ()
    considered: int = 0
    #: The scan's own ceilings, forwarded. ⚠️ A partial answer may never read as a
    #: complete one — a brief that quietly stopped at 20 000 files would tell a
    #: planner this project has no tests.
    truncated: bool = False
    truncated_by: str = ""
    #: One line per source that degraded, `broker.errors`' shape. Never an exception:
    #: a missing project doc, an unreadable manifest or a broken skill walk each cost
    #: one line of the brief and never the run.
    notes: tuple[str, ...] = ()
    elapsed_ms: int = 0
    at: float = field(default_factory=time.time)

    @property
    def verifiable(self) -> bool:
        """Whether a check node has a command to run. Not whether the run can be
        verified — that is `core/verify.py`'s question, and it is answered from the
        durable record whether a test suite exists or not."""
        return bool(self.test_command)

    def to_payload(self) -> dict:
        return {
            "goal": self.goal,
            "root": self.root,
            "project": self.project,
            "name": self.name,
            "language": self.language,
            "managers": list(self.managers),
            "tests": self.tests,
            "runners": list(self.runners),
            "test_command": self.test_command,
            "test_from": self.test_from,
            "verifiable": self.verifiable,
            "doc": self.doc,
            "doc_chars": self.doc_chars,
            "skills": list(self.skills),
            "skill_reasons": [dict(r) for r in self.skill_reasons],
            "considered": self.considered,
            "truncated": self.truncated,
            "truncated_by": self.truncated_by,
            "notes": list(self.notes),
            "elapsed_ms": self.elapsed_ms,
            "at": round(self.at, 3),
        }


def _note(notes: list, text: str) -> None:
    if text and len(notes) < MAX_NOTES:
        notes.append(str(text)[:MAX_NOTE])


def _names(rows, key: str, limit: int) -> tuple[str, ...]:
    """`[{key: name}, …] → (name, …)`, deduped, bounded, and total."""
    out: list[str] = []
    for row in list(rows or ())[: limit * 4]:
        name = str((row or {}).get(key) or "").strip() if isinstance(row, dict) else ""
        if name and name not in out:
            out.append(name)
        if len(out) >= limit:
            break
    return tuple(out)


def brief(goal: str, *, root=None, force: bool = False) -> Brief:
    """Read the project and the goal's skills. Never raises; never writes.

    ⚠️ **ONE GUARD PER SOURCE, never one around the three** — `broker.collect()`'s
    rule, and for its reason: a `git` binary missing from PATH makes `projectscan`'s
    git step degrade, and a single outer `try` would then cost the run its proved test
    command and its skills as well.

    ⚠️ It costs a project walk (bounded by `AGENT2_INIT_BUDGET_SEC`) and a skills
    pass (bounded by `AGENT2_SKILLS_BUDGET_SEC`), so it is called **once per run**,
    from `engine.start()`, off the turn path. It is the same walk `/init` makes.
    """
    started = time.perf_counter()
    notes: list[str] = []
    out = Brief(goal=str(goal or "").strip())

    scan: dict = {}
    try:
        from agent2.core import projectscan as _scan
        scan = _scan.scan(root) or {}
        out.project = _scan.summary(scan)
    except Exception as exc:
        _note(notes, f"the project scan failed: {type(exc).__name__}")
        scan = {}

    if scan:
        out.root = str(scan.get("root") or "")
        out.name = str(scan.get("name") or "")
        out.language = str(scan.get("primary_language") or "")
        out.managers = _names(scan.get("package_managers"), "key", MAX_MANAGERS)
        tests = scan.get("tests") or {}
        try:
            out.tests = len(tests.get("files") or ())
            out.runners = tuple(str(r) for r in (tests.get("runners") or ())
                                )[:MAX_RUNNERS]
        except Exception:
            pass
        # ⚠️ The FIRST proved test command, and only a proved one. `projectscan`
        # orders them by how strongly they were established, so first is the answer;
        # an empty list is an answer too, and it is not `npm test`.
        for cand in (scan.get("commands") or {}).get("test") or ():
            if isinstance(cand, dict) and str(cand.get("cmd") or "").strip():
                out.test_command = str(cand["cmd"]).strip()
                out.test_from = str(cand.get("from") or "")
                break
        out.truncated = bool(scan.get("truncated"))
        out.truncated_by = str(scan.get("truncated_by") or "")
        for line in (scan.get("errors") or ())[:2]:
            _note(notes, f"scan: {line}")
    if not out.root:
        try:
            from agent2.core import workspace as _ws
            out.root = str(root or _ws.root() or "")
        except Exception:
            out.root = str(root or "")

    try:
        from agent2.core import projectdoc as _doc
        text = _doc.read_existing(out.root)
        out.doc, out.doc_chars = bool(text), len(text)
        if not text:
            # Guidance, not a refusal: the doc is what `broker`'s project source puts
            # in front of every node, so a project without one plans against a census.
            _note(notes, "this project has no .agent2/agent2.md — /init writes one, "
                         "and every node's prompt would carry it")
    except Exception as exc:
        _note(notes, f"the project doc could not be read: {type(exc).__name__}")

    try:
        from agent2.core import skills as _skills
        sel = _skills.for_turn(out.goal, force=force)
        out.skills = tuple(sel.ids)[:MAX_SKILLS]
        out.skill_reasons = tuple(
            {"id": a.skill.id, "name": a.skill.name, "reason": a.reason}
            for a in list(sel.applied)[:MAX_SKILLS])
        out.considered = int(sel.considered)
        for line in list(sel.errors)[:2]:
            _note(notes, f"skills: {line}")
    except Exception as exc:
        _note(notes, f"skill discovery failed: {type(exc).__name__}")

    out.notes = tuple(notes)
    out.elapsed_ms = int((time.perf_counter() - started) * 1000)
    return out


# ── The gate ──────────────────────────────────────────────────────────────────


def approval_needed(*, override=None) -> bool:
    """Whether this run gets a human gate. `override` of `None` means *not stated*.

    ⚠️ **THE POSTURE IS A SETTING AND AN EXPLICIT ASK — NEVER A GUESS FROM THE
    GOAL.** It is tempting to read the sentence and demand approval when it looks
    destructive, and that would be a second risk classifier: `core/recovery/safety.py`
    already owns *"how dangerous is this operation"*, `core.permissions` owns *"may
    this process do it at all"*, and both are asked per **operation**, at the moment
    it runs, with the operation in hand. A goal is not an operation, so a verdict
    drawn from its wording would be less accurate than either and would fire on the
    word "delete" in a sentence about deleting a comment.

    `None` and a boolean are two different answers on purpose, `state.get()`'s
    tri-state for its reason: a surface that did not ask gets the operator's default
    (`AGENT2_ULTRACODE_APPROVAL`), and a surface that asked for `False` gets `False`
    even where the default is on. Nothing here can turn a gate *off* by accident,
    because absence is not `False`.
    """
    if override is None:
        return bool(config.ULTRACODE_APPROVAL)
    return bool(override)


# ── Shaping the graph ─────────────────────────────────────────────────────────


def _free_id(base: str, used) -> str:
    """`base`, or `base-2`, `base-3`… — the first id *used* does not hold.

    ⚠️ Never a bare `base`. `Graph.with_nodes()` replaces by id, so colliding with a
    planned step would silently delete work the plan asked for and leave the run
    looking like it did it.
    """
    taken = {str(u) for u in (used or ())}
    root = _m.fold_id(base) or "node"
    if root not in taken:
        return root
    for n in range(2, 100):
        cand = f"{root}-{n}"
        if cand not in taken and _m.NODE_ID_RE.match(cand):
            return cand
    return _m.fold_id(f"{root}-{int(time.time()) % 100000}") or root


def _check_instruction(brief_: Brief | None) -> str:
    """What a `check` node is told to do — and what it is told **not** to do.

    ⚠️ *"UltraCode must not trust its own generated conclusion. 'Done' is not
    verification."* A node cannot be asked whether the work went well, because the
    answer would be a model grading its own output; it is asked to run the project's
    own command and report the exit code, and the **verdict** is taken afterwards
    from the durable record by `core/verify.py`. Those are two different sentences and
    both of them are in the instruction on purpose.
    """
    tail = ("Report exactly what happened, including failures. Do not judge whether "
            "the run succeeded and do not answer 'done' — this run's verdict is taken "
            "from its recorded exit codes, tool calls and file writes, not from your "
            "summary.")
    cmd = (brief_.test_command if brief_ else "") or ""
    if cmd:
        return (f"Verify the work of this run by executing the project's own test "
                f"command and reporting its exit code and output verbatim:\n\n"
                f"    {cmd}\n\n{tail}")
    return ("Verify the work of this run by re-reading the files it changed and "
            "checking them against the goal. No test command was established for this "
            f"project, so run no test suite — an invented one proves nothing. {tail}")


def graph_for(defn, *, brief_: Brief | None = None, approval: bool = False,
              check: bool = True) -> tuple[object, dict]:
    """*defn* with UltraCode's kinds on it, a gate before it and a check after it.

    Returns `(graph, report)`. The report names what was added (`approval`, `check`),
    which nodes were rewired (`roots`) and what the check depends on (`terminals`), so
    a surface can say what happened without re-deriving any of it — and so a test can
    assert the shape without parsing prose.

    ⚠️ **VERIFICATION IS A NODE, NOT A POST-PASS.** *"Every task must be verified
    before being marked complete"* is only structurally true if the check can hold
    work back, and only a node in the graph can: a pass that ran after
    `store.advance()` settled the run would be a report about work already declared
    finished. So the check fans **in** over the graph's terminal nodes, and the run
    cannot finish without it.

    ⚠️ **THE GATE IS A NODE FOR THE MIRROR REASON.** *"Human approval should be
    represented as an appropriate DAG node"* — so it is one, with no dependencies, and
    every **root** of the plan is rewired to need it. That is what makes "nothing at
    all runs until a person says so" a property of the graph rather than a promise in
    a driver: `tasks.ready()` will not release a node whose dependency is unsettled,
    whichever process asks, however many times, and however the driver is restarted.
    A prompt in `engine.start()` would be gone the moment the process died.

    ⚠️ Total: a `defn` that is None or empty comes back untouched with a note. There
    is nothing to gate, and inventing a graph out of an absent one would hand
    `store.create()` a run whose only node is a question.
    """
    report = {"approval": "", "check": "", "roots": [], "terminals": [],
              "kinds": {}, "note": ""}
    nodes = tuple(getattr(defn, "nodes", None) or ())
    if defn is None or not nodes:
        report["note"] = "no plan to shape"
        return defn, report

    # 1 ── Every planned step is a `build` unless the planner already named a kind.
    #      ⚠️ `or K_BUILD`, never an overwrite: `replan()` marks remediation steps
    #      `fix`, and flattening those would erase which work was reactive.
    kinds: dict[str, str] = {}
    shaped = []
    for n in nodes:
        kind = (n.kind or _st.K_BUILD)
        kinds[n.id] = kind
        shaped.append(_m.Node(id=n.id, title=n.title, instruction=n.instruction,
                              needs=tuple(n.needs), priority=n.priority,
                              resource=n.resource, seq=n.seq, kind=kind,
                              meta=dict(n.meta)))
    graph = _m.Graph(name=getattr(defn, "name", "") or LABEL,
                     description=getattr(defn, "description", "") or "",
                     version=getattr(defn, "version", _m.SCHEMA_VERSION),
                     nodes=tuple(shaped),
                     source=getattr(defn, "source", "") or DEF_SOURCE,
                     extra=dict(getattr(defn, "extra", None) or {}))

    # 2 ── Roots and terminals, from the graph's own adjacency. ⚠️ `edge_map()` and
    #      `dependents()`, never a hand-walk: they are the same two views the cycle
    #      finder and the level builder consume, so "root" here cannot mean something
    #      different from what the validator and the scheduler will act on.
    edge_map = graph.edge_map()
    dependents = graph.dependents()
    roots = [nid for nid, needs in edge_map.items() if not needs]
    terminals = [nid for nid, users in dependents.items() if not users]
    report["roots"] = list(roots)
    report["terminals"] = list(terminals)

    # 3 ── The check node, fanning in over everything nothing else waits on.
    if check and terminals:
        cid = _free_id(CHECK_ID, graph.ids())
        graph = graph.with_nodes([_m.Node(
            id=cid, title=CHECK_TITLE, instruction=_check_instruction(brief_),
            needs=tuple(terminals), priority=CHECK_PRIORITY, kind=_st.K_CHECK)])
        kinds[cid] = _st.K_CHECK
        report["check"] = cid

    # 4 ── The gate, and an edge from it into every root of the *plan*. The check
    #      node is deliberately not re-read as a root here: it was appended in step 3
    #      with `needs`, so it never was one, and roots were computed before it
    #      existed.
    if approval:
        aid = _free_id(APPROVAL_ID, graph.ids())
        graph = graph.with_nodes([_m.Node(
            id=aid, title=APPROVAL_TITLE,
            instruction=("A human must approve this run before any of its work "
                         "starts. Release it from /ultracode approve, or cancel the "
                         "run."),
            needs=(), priority=APPROVAL_PRIORITY, kind=_st.K_APPROVAL)])
        kinds[aid] = _st.K_APPROVAL
        for nid in roots:
            graph = graph.with_edge(aid, nid)
        report["approval"] = aid
        if not roots:
            # Every node needs another one — the validator will refuse this graph as a
            # cycle. Said out loud rather than left as a gate nothing depends on.
            report["note"] = "the plan has no root node, so the gate holds nothing"

    report["kinds"] = dict(kinds)
    return graph, report


def describe() -> dict:
    """The shaping policy itself, for a surface that renders it. No state, no query."""
    return {
        "label": LABEL,
        "knob": KNOB,
        "surface": SURFACE,
        "source": DEF_SOURCE,
        "event": EVENT,
        "max_nodes": max_nodes(),
        "approval_default": bool(config.ULTRACODE_APPROVAL),
        "approval_id": APPROVAL_ID,
        "check_id": CHECK_ID,
    }
