# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2.core.dag.model
─────────────────────
THE graph — Phase D1, and the one declaration of what a node, an edge and a node
state ARE for every feature in this build.

⚠️ **THIS MODULE KNOWS NOTHING ABOUT WORKFLOWS, ULTRACODE, SECURITY, ZAP, BURP OR
SKILLS, AND THAT IS THE ACCEPTANCE BAR — NOT A STYLE PREFERENCE.** The spec's rule
is literal: *"The DAG engine must NOT contain business-specific logic"*, and the
forbidden shapes are named — `if workflow:`, `if ultracode:`, `if security:`,
`if zap:`, `if skill:`. `test_dag.py` asserts it structurally rather than trusting
this paragraph: it parses the package with `ast` and fails if any branch condition
or defined name in it mentions a feature, and if the package imports one.

The reason is the one the rest of this codebase is shaped around. A graph engine
that grew one `if workflow:` would grow a second for UltraCode, and then "what
depends on what" would have three answers that each look right from inside their
own feature. Domain vocabulary therefore travels as **data the core carries and
never reads**: `Node.kind` is a free label (a consumer may call a node `tool`,
`llm`, `approval`, `mcp`, `loop`; nothing here branches on any of them) and
`Node.meta` is a dict this module stores, reports and copies without inspecting.

⚠️ **NINE NODE STATES, AND TWO OF THEM ARE DERIVED ON EVERY READ.** The spec names
PENDING · READY · RUNNING · COMPLETED · FAILED · BLOCKED · PAUSED · CANCELLED ·
SKIPPED. Seven of the nine names are also `tasks.TaskStatus` values, but only **six**
are ever *projected* from a stored status (the four terminal ones, RUNNING and
PAUSED): **READY and BLOCKED are not stored anywhere and must not be**, and a stored
`pending` row reads as one of those two rather than as PENDING — which `store.plan()`
reserves for a graph with no runnable order at all.
Readiness is `tasks.ready()`'s answer — one declaration, asked over the rows —
and a `state` column holding "ready" would be a second answer that goes stale the
instant an upstream node settles, with nothing on screen to say which one the
dispatcher believed. `store.py` derives both; see its docstring for the full map
and for what `QUEUED` (stored, not in the spec's nine) projects to.

⚠️ **`needs` IS THE ONE DECLARATION OF STRUCTURE; AN `Edge` IS A VIEW OF IT.** The
spec asks for edge creation and deletion, and it would have been natural to store
an `edges` list beside the nodes — which is two records of one fact, drifting the
first time a caller updates one of them. So `Graph.edges()` *derives* the edge set
from the nodes' `needs`, and `with_edge()` / `without_edge()` return a NEW graph
with the `needs` tuples rewritten. Every mutation here is non-destructive for the
same reason: a runtime change has to be **validated before it is accepted**
(`validate.validate_mutation`), and you cannot validate a graph you already
overwrote.

Nothing in this module touches the database, the filesystem, the clock or the
network, and nothing raises: `make_node()` / `make_graph()` coerce whatever they
are handed, and a graph too broken to be coerced comes back empty for
`validate()` to describe. That is what makes the model deterministic on every OS —
ids are folded to lower case so one definition resolves to one graph in a Windows
and a Linux checkout of the same repository.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Schema version of the *graph model*. Consumers that read graphs off disk
#: (`workflow.loader`) upgrade an older document rather than refusing it, so this
#: is bumped only when a field changes MEANING — adding an optional field does
#: not need a bump, which is what keeps "an old file still runs" cheap to honour.
SCHEMA_VERSION = 1

#: The lowest schema this build will still read. Kept separate from
#: `SCHEMA_VERSION` so "we understand version 1" and "we still accept version 1"
#: stay two facts rather than one.
MIN_SCHEMA = 1

# A node id is a name a human types (`needs: [build]`, `/workflow run audit`) and a
# key two independent tables agree on, so it is deliberately narrow: lower-case
# alphanumerics plus `.`, `-`, `_`. No spaces (they would need quoting on every
# surface), no slashes (they read as a path and invite one), no leading punctuation.
NODE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

#: Same shape for a graph's own name — it is a filename stem for one consumer and a
#: word on the command line for another, so it may not contain anything a shell or
#: a filesystem would argue about.
NAME_RE = NODE_ID_RE

MAX_TITLE = 200
MAX_TEXT = 4000

#: Characters kept from one `kind` / `resource` label. Both are keys, not prose.
MAX_LABEL = 64

# ── The nine node states ──────────────────────────────────────────────────────
# ⚠️ These strings are `tasks.TaskStatus`' strings on purpose, for the seven that
# overlap. A node's state and its task row's status are asked about the same node
# by different callers, and a second vocabulary (`"done"` for `"completed"`) is a
# translation table somebody has to keep — `runner.py` keeps exactly one, for its
# own historical `PHASE_*` payload, and its cost is visible there.

#: Declared, but no row exists yet — what `plan()` reports before `store.create()`.
#: ⚠️ NOT the projection of a stored `pending` row: that row is READY or BLOCKED
#: depending on its dependencies, and calling it PENDING would hide which.
PENDING = "pending"
#: Every dependency has settled: this node may be dispatched now. Derived, never
#: stored — see the module docstring.
READY = "ready"
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
#: At least one dependency has not settled. Derived, never stored.
BLOCKED = "blocked"
PAUSED = "paused"
CANCELLED = "cancelled"
SKIPPED = "skipped"

#: Every state, in the order a human reads a lifecycle. A surface can enumerate
#: what it may be shown without waiting to be shown one.
STATES: tuple[str, ...] = (
    PENDING, BLOCKED, READY, RUNNING, PAUSED, COMPLETED, FAILED, CANCELLED,
    SKIPPED,
)

#: Settled: this node will not run again. ⚠️ Mirrors `tasks.TERMINAL`, because
#: `tasks.set_status()` refuses to move a terminal row without `force=True` — that
#: refusal, not this frozenset, is what makes "recovery never re-runs completed
#: work" true.
TERMINAL: frozenset[str] = frozenset((COMPLETED, FAILED, CANCELLED, SKIPPED))

#: Not settled — still part of the remaining plan.
OPEN: frozenset[str] = frozenset((PENDING, BLOCKED, READY, RUNNING, PAUSED))

#: Settled, but NOT successfully. Consumers decide what that means for downstream
#: work (skip the branch, diagnose it, ask a human); the core only names the set.
UNSUCCESSFUL: frozenset[str] = frozenset((FAILED, CANCELLED, SKIPPED))

# ── Problem codes ─────────────────────────────────────────────────────────────
# Reported as data, never as prose a caller has to parse: each surface renders its
# own sentence from the same code, exactly as `skills.select.REASONS` is a table
# rather than a set of strings.
P_NO_NAME = "no_name"
P_BAD_NAME = "bad_name"
P_NO_NODES = "no_nodes"
P_TOO_MANY = "too_many_nodes"
P_BAD_ID = "bad_node_id"
P_DUPLICATE = "duplicate_node_id"
P_UNKNOWN_DEP = "unknown_dependency"
P_SELF_DEP = "self_dependency"
P_CYCLE = "cycle"
P_SCHEMA = "unsupported_schema"
#: Mutation-only: a settled node may not be removed, re-declared or re-wired.
P_LOST_NODE = "settled_node_removed"
P_REWIRED = "settled_node_rewired"
P_NO_GROWTH = "growth_exhausted"

#: Every fatal code.
PROBLEM_CODES: tuple[str, ...] = (
    P_NO_NAME, P_BAD_NAME, P_NO_NODES, P_TOO_MANY, P_BAD_ID, P_DUPLICATE,
    P_UNKNOWN_DEP, P_SELF_DEP, P_CYCLE, P_SCHEMA, P_LOST_NODE, P_REWIRED,
    P_NO_GROWTH,
)

# Warning codes — the graph still runs.
W_NO_TITLE = "no_title"
W_DUP_DEP = "duplicate_dependency"
W_NO_INSTRUCTION = "no_instruction"
W_NEW_SCHEMA = "newer_schema"

WARNING_CODES: tuple[str, ...] = (W_NO_TITLE, W_DUP_DEP, W_NO_INSTRUCTION,
                                  W_NEW_SCHEMA)


def fold_id(raw) -> str:
    """`  Build-Step ` → `build-step`. THE one folding of a node id or a name.

    ⚠️ Lower-cased, and that is a cross-platform requirement rather than a style
    choice: one consumer derives a graph's name from a **filename**, and
    `Audit.yaml` and `audit.yaml` are the same file on Windows and macOS and two
    files on Linux. Folding here means one declaration resolves to one name on
    every OS — `skills.discovery._id_for()` folds for exactly this reason.
    """
    return " ".join(str(raw or "").split()).strip().lower()


def _clip(raw, limit: int) -> str:
    return " ".join(str(raw or "").split())[:limit] if limit else ""


def _text(raw, limit: int = MAX_TEXT) -> str:
    """Free prose, trimmed but not collapsed — an instruction may be multi-line."""
    return str(raw or "").strip()[:limit]


def problem(code: str, message: str, **extra) -> dict:
    """One problem or warning entry. The shape every surface renders from."""
    return {"code": code, "message": message, **extra}


@dataclass(frozen=True)
class Edge:
    """`src` must settle before `dst` may run. A **view**, not a stored record.

    ⚠️ Derived from `dst`'s `needs` by `Graph.edges()` and never held beside the
    nodes, because two records of one structure drift the first time a caller
    rewrites one of them. `Graph.with_edge()` / `without_edge()` therefore rewrite
    `needs` and hand back a new graph — the edge API is real, its storage is not.
    """

    src: str
    dst: str

    def to_payload(self) -> dict:
        return {"src": self.src, "dst": self.dst}


@dataclass
class Node:
    """One node of a graph, and — once a consumer persists it — one task row.

    `instruction` is what the node tells a worker to do; `title` is what a human
    reads in `/tasks`.

    ⚠️ `kind` and `meta` are the **extensible node model**, and the core's promise
    about them is that it never looks inside. The spec allows node types (task,
    tool, LLM, skill-assisted, human approval, conditional, loop, sub-agent, MCP)
    to be built *on top of* the generalized graph while the graph stays unaware of
    them, so there are no `kind` constants declared here: the consumer that
    invents a type owns its spelling, and this module carries the label through
    validation, persistence and reporting untouched. A `KIND_APPROVAL` in this
    file would be the first business fact in a feature-agnostic engine.

    ⚠️ `resource` is a mutual-exclusion token: two nodes naming one resource may
    never run concurrently. It is declared in the model rather than in the
    scheduler because a definition written today has to still validate — and mean
    the same thing — on the build where the scheduler honours it.
    """

    id: str
    title: str = ""
    instruction: str = ""
    needs: tuple[str, ...] = ()
    priority: int = 5
    resource: str = ""          # scheduler-honoured mutual exclusion
    seq: int = 0                # declaration order — the tie-break, never re-derived
    #: A consumer's own type label. ⚠️ Never branched on in this package.
    kind: str = ""
    #: A consumer's own opaque payload. ⚠️ Never read in this package.
    meta: dict = field(default_factory=dict)

    def to_payload(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "instruction": self.instruction,
            "needs": list(self.needs),
            "priority": self.priority,
            "resource": self.resource,
            "seq": self.seq,
            "kind": self.kind,
            "meta": dict(self.meta),
        }


@dataclass
class Graph:
    """A whole graph, as declared. Data only — nothing here acts.

    ⚠️ `source` travels WITH the graph for the reason `skills.Catalog` carries its
    own `project` and `root`: a graph that did not say where it came from could be
    cached, passed around and reported elsewhere, and every claim about which file
    or which planner produced a run would become a claim about the caller's
    discipline instead of about the data.
    """

    name: str
    description: str = ""
    version: int = SCHEMA_VERSION
    nodes: tuple[Node, ...] = ()
    source: str = "inline"
    #: Fields a newer schema wrote that this build does not map. Kept, never
    #: guessed at — `skills.normalize`'s `extra`, for its reason.
    extra: dict = field(default_factory=dict)

    # ── Reading ───────────────────────────────────────────────────────────────

    def node(self, node_id: str) -> Node | None:
        want = fold_id(node_id)
        for n in self.nodes:
            if n.id == want:
                return n
        return None

    def ids(self) -> tuple[str, ...]:
        return tuple(n.id for n in self.nodes)

    def edge_map(self) -> dict[str, list[str]]:
        """`node id → the ids it needs`, restricted to nodes of THIS graph.

        The adjacency the cycle finder and the level builder both consume. Refs to
        anything outside the pool are dropped here so those two never disagree
        about whether a dangling `needs:` is a ring — it is its own problem, and
        `validate()` reports it as one.
        """
        known = {n.id for n in self.nodes if n.id}
        return {n.id: [d for d in n.needs if d in known and d != n.id]
                for n in self.nodes if n.id}

    def edges(self) -> tuple[Edge, ...]:
        """Every dependency as an `Edge`, in declaration order. Derived."""
        out: list[Edge] = []
        for n in self.nodes:
            if not n.id:
                continue
            out.extend(Edge(src=d, dst=n.id) for d in n.needs)
        return tuple(out)

    def dependents(self) -> dict[str, list[str]]:
        """The reverse adjacency: `node id → the ids that need it`.

        Consumed by whoever has to answer "what does this failure block" without
        walking the whole graph per node.
        """
        out: dict[str, list[str]] = {n.id: [] for n in self.nodes if n.id}
        for n in self.nodes:
            for d in n.needs:
                if d in out and n.id and n.id not in out[d]:
                    out[d].append(n.id)
        return out

    # ── Mutation — every one of these returns a NEW graph ─────────────────────
    # ⚠️ Non-destructive on purpose. The spec requires a runtime change to be
    # checked before it is accepted ("before accepting changes detect cycles,
    # validate node references, dependencies, edge references, invalid
    # mutations"), and a caller cannot validate a graph it has already
    # overwritten. So a mutation produces a candidate, `validate_mutation()`
    # judges it against the state on disk, and only then does a store write it.

    def with_nodes(self, nodes) -> Graph:
        """This graph plus *nodes*, re-sequenced onto the end. Replaces by id.

        A node whose id already exists **replaces** the existing one in place,
        keeping its `seq`: that is how a re-plan revises an instruction it has not
        run yet. `validate_mutation()` is what refuses to do it to a node that has
        already settled — the model allows the edit, the validator owns the rule,
        because "has this run" is a fact about rows and not about the graph.
        """
        merged = list(self.nodes)
        at = {n.id: i for i, n in enumerate(merged) if n.id}
        nxt = max((n.seq for n in merged), default=-1) + 1
        for raw in list(nodes or ()):
            node = raw if isinstance(raw, Node) else make_node(raw)
            if not node.id:
                continue
            if node.id in at:
                keep = merged[at[node.id]].seq
                merged[at[node.id]] = replace_seq(node, keep)
                continue
            at[node.id] = len(merged)
            merged.append(replace_seq(node, nxt))
            nxt += 1
        return Graph(name=self.name, description=self.description,
                     version=self.version, nodes=tuple(merged), source=self.source,
                     extra=dict(self.extra))

    def without_nodes(self, ids) -> Graph:
        """This graph minus *ids*, with every `needs:` reference to them dropped.

        ⚠️ The references go too. Leaving them would turn one deletion into a
        fistful of unknown-dependency problems pointing at nodes the author did
        write, which is the same reason a truncated declaration is refused rather
        than read as far as it got.
        """
        drop = {fold_id(i) for i in (ids or ()) if fold_id(i)}
        if not drop:
            return self
        kept = [n for n in self.nodes if n.id not in drop]
        out = [Node(id=n.id, title=n.title, instruction=n.instruction,
                    needs=tuple(d for d in n.needs if d not in drop),
                    priority=n.priority, resource=n.resource, seq=n.seq,
                    kind=n.kind, meta=dict(n.meta)) for n in kept]
        return Graph(name=self.name, description=self.description,
                     version=self.version, nodes=tuple(out), source=self.source,
                     extra=dict(self.extra))

    def with_edge(self, src, dst) -> Graph:
        """`dst` now needs `src`. Idempotent; a duplicate is not a second edge."""
        return self._rewire(dst, src, add=True)

    def without_edge(self, src, dst) -> Graph:
        """`dst` no longer needs `src`. Absent is not an error."""
        return self._rewire(dst, src, add=False)

    def _rewire(self, dst, src, *, add: bool) -> Graph:
        want, dep = fold_id(dst), fold_id(src)
        if not want or not dep:
            return self
        out: list[Node] = []
        for n in self.nodes:
            needs = n.needs
            if n.id == want:
                cur = list(needs)
                if add and dep not in cur:
                    cur.append(dep)
                elif not add:
                    cur = [d for d in cur if d != dep]
                needs = tuple(cur)
            out.append(Node(id=n.id, title=n.title, instruction=n.instruction,
                            needs=needs, priority=n.priority, resource=n.resource,
                            seq=n.seq, kind=n.kind, meta=dict(n.meta)))
        return Graph(name=self.name, description=self.description,
                     version=self.version, nodes=tuple(out), source=self.source,
                     extra=dict(self.extra))

    def to_payload(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "source": self.source,
            "count": len(self.nodes),
            "nodes": [n.to_payload() for n in self.nodes],
            "edges": [e.to_payload() for e in self.edges()],
            "extra": dict(self.extra),
        }


def replace_seq(node: Node, seq: int) -> Node:
    """*node* with a different `seq`. A copy — `Node` is mutable and shared."""
    return Node(id=node.id, title=node.title, instruction=node.instruction,
                needs=tuple(node.needs), priority=node.priority,
                resource=node.resource, seq=seq, kind=node.kind,
                meta=dict(node.meta))


@dataclass
class Validation:
    """Whether a graph may run, and everything wrong with it. Never an exception.

    ⚠️ `problems` and `warnings` are two lists and `ok` is decided by `problems`
    **alone** — `core/health.py`'s rule, for its reason: every configuration that
    can legitimately occur and still run must be expressible without tripping the
    refusal, or the refusal stops being read. A node with no title is a cosmetic
    gap; a cycle is a graph that can never advance.
    """

    ok: bool = True
    problems: list[dict] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)
    #: Topological levels: `levels[0]` may start at once, `levels[1]` once every
    #: node in `levels[0]` has settled, and so on. Empty when `ok` is False,
    #: because an order derived from an unrunnable graph would be fiction.
    levels: tuple[tuple[str, ...], ...] = ()
    #: The node ids caught in a dependency ring, one tuple per ring found.
    cycles: tuple[tuple[str, ...], ...] = ()

    def summary(self) -> str:
        """One line a human reads. Empty when the graph is clean."""
        if self.problems:
            first = self.problems[0]
            more = f" (+{len(self.problems) - 1} more)" if len(self.problems) > 1 else ""
            return f"{first.get('message') or first.get('code')}{more}"
        if self.warnings:
            first = self.warnings[0]
            more = f" (+{len(self.warnings) - 1} more)" if len(self.warnings) > 1 else ""
            return f"{first.get('message') or first.get('code')}{more}"
        return ""

    def codes(self) -> tuple[str, ...]:
        """Every problem code, in order. For a caller deciding what to do next."""
        return tuple(str(p.get("code") or "") for p in self.problems)

    def to_payload(self) -> dict:
        return {
            "ok": self.ok,
            "problems": [dict(p) for p in self.problems],
            "warnings": [dict(w) for w in self.warnings],
            "levels": [list(lv) for lv in self.levels],
            "cycles": [list(c) for c in self.cycles],
            "summary": self.summary(),
        }


def make_node(raw, *, seq: int = 0) -> Node:
    """One node from a dict (or a bare string). Total — never raises.

    A bare string is the shorthand a human reaches for first (`nodes: [build,
    test]`) and it means *a node with this id, no dependencies*. Coercion happens
    here, once, so a file loader and an inline caller cannot disagree about what a
    node literal means.
    """
    if not isinstance(raw, dict):
        nid = fold_id(raw)
        return Node(id=nid, title=_clip(raw, MAX_TITLE), seq=seq)

    nid = fold_id(raw.get("id") or raw.get("name") or raw.get("node") or "")
    needs_raw = raw.get("needs")
    if needs_raw is None:
        needs_raw = raw.get("depends_on", raw.get("after", raw.get("requires", ())))
    if isinstance(needs_raw, str):
        parts = [p for p in re.split(r"[,\s]+", needs_raw) if p]
    elif isinstance(needs_raw, (list, tuple)):
        parts = list(needs_raw)
    else:
        parts = []
    # Deduped here, order preserved: a repeated dependency is a warning, not a
    # second edge, and letting it through would inflate every fan-in count.
    needs: list[str] = []
    for p in parts:
        folded = fold_id(p)
        if folded and folded not in needs:
            needs.append(folded)

    try:
        priority = int(raw.get("priority", 5))
    except (TypeError, ValueError):
        priority = 5
    meta = raw.get("meta")
    return Node(
        id=nid,
        title=_clip(raw.get("title") or raw.get("name") or nid, MAX_TITLE),
        instruction=_text(raw.get("instruction") or raw.get("prompt")
                          or raw.get("description") or raw.get("do") or ""),
        needs=tuple(needs),
        priority=max(0, min(99, priority)),
        resource=fold_id(raw.get("resource") or raw.get("lock") or "")[:MAX_LABEL],
        seq=seq,
        kind=fold_id(raw.get("kind") or raw.get("type") or "")[:MAX_LABEL],
        meta=dict(meta) if isinstance(meta, dict) else {},
    )


#: Keys `make_graph()` maps. Anything else lands in `Graph.extra`.
#: ⚠️ Frozen deliberately: a consumer's own top-level field must keep arriving in
#: `extra` rather than being absorbed here, because absorbing it would drop it
#: from the one place a caller can still read it.
GRAPH_KEYS: frozenset[str] = frozenset((
    "name", "workflow", "id", "description", "desc", "summary", "version",
    "schema", "schema_version", "nodes", "steps", "tasks", "graph",
))


def make_graph(raw, *, source: str = "inline", name: str = "") -> Graph:
    """A `Graph` from a plain dict. Total — never raises.

    Shared by every reader of a declared graph, so "what a graph literal means"
    has one home. Unmapped keys land in `extra` rather than being dropped, because
    a document written by a newer build must survive a round trip through an older
    one intact enough to be reported.
    """
    if not isinstance(raw, dict):
        return Graph(name=fold_id(name), source=source)

    nodes_raw = raw.get("nodes")
    if nodes_raw is None:
        nodes_raw = raw.get("steps", raw.get("tasks", raw.get("graph", ())))
    if isinstance(nodes_raw, dict):
        # A mapping of id → body is the other shape people write. The key is the
        # id, so it wins over any `id:` inside the body — otherwise one node could
        # answer to two names and `needs:` would resolve to whichever was read last.
        items = []
        for key, body in nodes_raw.items():
            entry = dict(body) if isinstance(body, dict) else {"instruction": body}
            entry["id"] = key
            items.append(entry)
        nodes_raw = items
    elif not isinstance(nodes_raw, (list, tuple)):
        nodes_raw = []

    version_raw = raw.get("version", raw.get("schema_version", raw.get("schema")))
    try:
        version = int(version_raw) if version_raw not in (None, "") else SCHEMA_VERSION
    except (TypeError, ValueError):
        version = SCHEMA_VERSION

    return Graph(
        name=fold_id(raw.get("name") or raw.get("workflow") or raw.get("id") or name),
        description=_text(raw.get("description") or raw.get("desc")
                          or raw.get("summary") or "", MAX_TEXT),
        version=version,
        nodes=tuple(make_node(n, seq=i) for i, n in enumerate(nodes_raw)),
        source=source,
        extra={k: v for k, v in raw.items() if k not in GRAPH_KEYS},
    )
