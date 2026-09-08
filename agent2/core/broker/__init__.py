# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/core/broker/ — THE Context Broker (Task 20)
──────────────────────────────────────────────────
One place decides what a model is told about the world before it answers.

Agent2 already had context *handling*: `agent.system_prompt()` glued a cached
static body to a memories block and a rules block, `agent.build_context()` turned
message rows into conversation turns, and `core/recovery.py` prepended a one-shot
briefing. Each was correct on its own and none of them knew about the others —
so "what does the model actually know right now" had no answer you could read,
test, or bound. The broker is that answer.

⚠️ IT SITS *ABOVE* THE EXISTING SYSTEMS AND REWRITES NONE OF THEM.
Every collector below delegates: memories come from `core.memory`, rules from
`core.rules`, the plan from `core.tasks`, the repository from `core.gitstate`, MCP
verdicts from `integrations.registry.health()`, changed files from
`core.diffs.store`. The broker owns *composition* — which sources exist, in what
order they render, and (Tasks 21–23) what each one costs, how relevant it is and
whether this project is allowed to see it. It owns no facts of its own, so there
is nothing here to drift out of step with the modules that do.

⚠️ THE CONVERSATION IS AN INPUT, NOT A SOURCE THIS MODULE READS.
`agent.build_context()` is still THE reader of the `messages` table and still the
only converter into provider-specific turn objects; a second query of those rows
here would be a second declaration of "what history means", and the two would
disagree about the tie-break that `build_context()`'s docstring exists to explain.
The caller hands its size in on `ContextRequest`, and the conversation appears in
the bundle as a **pinned** item — accounted for, never dropped, never re-read.

⚠️ NOTHING HERE MAY RAISE INTO A TURN.
`collect()` runs every collector inside its own guard and records the failure on
`bundle.errors`. A broken source degrades to *absent*, exactly like the PIL
degrades to "do nothing" — because the alternative is that a `git` binary missing
from PATH, or an MCP server returning nonsense, ends a user's turn. The two
always-on sources (memory, rules) are the ones whose absence is visible, and they
are also the two that cannot fail: they come from `VersionedCache`es that already
swallow their own builders' errors.

⚠️ MEMORY RENDERS BEFORE RULES, AND EXACTLY ONE PLACE SAYS SO — `ORDER`.
`test_system_prompt_injects_memories_rules_and_platform` pins that ordering, and
both the bundle-less tail (`base_tail()`) and the full tail (`prompt_tail()`) sort
through `ORDER`, so there is no second ordering to keep in step. Situational
sources render first and the two standing instruction blocks render last, so the
prompt ends on the user's own standing orders.

⚠️ NOT EVERYTHING COLLECTED IS SENT (Task 22).
`assemble()` plans a budget and `prompt_tail()` renders what fit, so a bundle has
two populations that must not be confused: `items` is what was gathered and
`sent()` is what reached the model. Every reader of "what is in the prompt" —
`prompt_tail()`, `sources_used()` — goes through `sent()`, because a renderer
reading `items` while a report read the plan would describe a prompt nobody sent.
`core/broker/budget.py` owns the ceiling and the reason pinned items are exempt.

⚠️ `base_tail()` IS WHY `system_prompt()` STILL COSTS ZERO QUERIES.
`test_system_prompt_does_zero_queries_when_cached` is a real guard: the prompt is
rebuilt once per turn but read on every one of up to `MAX_AGENT_ITERS`
iterations. So the *situational* sources — project docs, git, tasks, files — are
assembled once per turn by the caller and passed in, while a bare
`system_prompt()` (the provider path, `/api/platform`, every test) still resolves
to the same two cached blocks it always did. Anything that made those blocks
turn-dependent would put two DB queries back on every agent iteration.

Task map for this package:
  * Task 20 (here)          — the broker, the source table, the collectors,
                              `assemble()`, and the prompt tail both agent loops use.
  * Task 21 `sources.py`    — what each source *tracks*: priority, relevance,
                              token_cost, freshness. ⚠️ The source NAMES, `ORDER`
                              and `ALWAYS` live there and are re-exported here, so
                              `broker.SOURCE_MEMORY` is `sources.SOURCE_MEMORY` —
                              one spelling, one order, one priority table.
  * Task 22 `budget.py`     — selection inside the model's token limit. ⚠️ Applied
                              by `assemble()`, so a surface cannot forget it;
                              `prompt_tail()` renders what FIT, `items` keeps what
                              was collected, and the plan reports the difference.
  * Task 23 `isolation.py`  — workspace scoping: Project A never reads Project B.
                              ⚠️ `core.memory` and `core.rules` import it at module
                              level, so THIS module's imports of them must stay
                              lazy (they are, inside the two builders) or the
                              package cycles on import.

Skills (Phase 11) and workflow state (Phase 12) are declared here as real sources
with empty collectors, and `register()` is how those phases fill them in. That is
deliberate: a source that does not exist yet still has a name, an order and a slot
in the report, so adding it later is a registration rather than a change to this
module.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path

from agent2.core import metrics as _metrics
from agent2.core import sync as _sync
from agent2.core.broker import budget as _budget
from agent2.core.broker import isolation as _isolation
from agent2.core.broker import sources as _sources

# ── Source kinds ───────────────────────────────────────────────────────────────
# ⚠️ RE-EXPORTED FROM `sources.py`, NOT DECLARED HERE. Task 21 moved the names,
# the render `ORDER` and `ALWAYS` into the module that also owns each source's
# priority, TTL and measures, so "which sources exist" and "what each one is
# worth" cannot drift apart. These bindings are the same objects, so existing
# callers (`broker.SOURCE_MEMORY`, `broker.ORDER`) keep working unchanged — the
# same re-export-shim discipline the flat `agent2.keys` paths use.
SOURCE_CONVERSATION = _sources.SOURCE_CONVERSATION
SOURCE_PROJECT      = _sources.SOURCE_PROJECT
SOURCE_GIT          = _sources.SOURCE_GIT
SOURCE_SKILLS       = _sources.SOURCE_SKILLS
SOURCE_TASKS        = _sources.SOURCE_TASKS
SOURCE_WORKFLOW     = _sources.SOURCE_WORKFLOW
SOURCE_MCP          = _sources.SOURCE_MCP
SOURCE_FILES        = _sources.SOURCE_FILES
SOURCE_MEMORY       = _sources.SOURCE_MEMORY
SOURCE_RULES        = _sources.SOURCE_RULES

ORDER: tuple[str, ...] = _sources.ORDER
ALWAYS: frozenset[str] = _sources.ALWAYS

# How much of `.agent2/agent2.md` reaches the prompt. Bounded because it is a file
# a user (or `/init`) writes and nothing stops it growing: unbounded, one project
# file would crowd out every other source on every turn.
# ⚠️ THIS IS THE BUDGET, NOT THE CLIP. *What* survives it is
# `projectdoc.for_prompt()`'s decision, because it turns on which sections a human
# owns — a fact of that module and of nowhere else. Slicing here instead cut the
# document head-first, and a doc ends with the sections a human took over.
PROJECT_DOC_CHARS = 6_000

# Bounds on the situational lists. Each exists so a long-running session cannot
# turn one source into the whole prompt.
MAX_DOC_FILES  = 6
MAX_TASK_LINES = 8
MAX_FILE_LINES = 12

# Candidate project-documentation files, in priority order. `.agent2/agent2.md` is
# first because it is written *for* the agent (Phase 10 generates it); a plain
# README is written for humans and is listed rather than inlined — see
# `_collect_project`.
PROJECT_DOCS: tuple[str, ...] = (
    ".agent2/agent2.md",
    "agent.md",
    "AGENTS.md",
    "CLAUDE.md",
    "README.md",
    "readme.md",
    "README.rst",
    "CONTRIBUTING.md",
)

# The one file whose *content* is inlined rather than merely named.
PRIMARY_DOC = ".agent2/agent2.md"


# ── Records ────────────────────────────────────────────────────────────────────

@dataclass
class ContextItem:
    """One piece of context, from one source, with what Task 21 tracks about it.

    `text` is the rendered prompt section including its own leading blank lines,
    or "" for an accounting-only item such as the conversation. `pinned` marks an
    item that budgeting may never drop.

    ⚠️ THE FOUR MEASURES DEFAULT TO `None`, WHICH MEANS "NOT STATED" — not zero.
    `sources.measure()` fills exactly the ones a collector left unset, so a source
    that genuinely knows its own relevance can say so and a generic word-overlap
    pass will not overwrite it. Zero as the default would be indistinguishable from
    a real measurement of zero, and a zero-relevance item is the first thing a
    budget discards, so the two must not look alike. After `collect()` every item
    on a bundle has all four set.

    `stamp` is when the underlying data was READ (wall clock, `0.0` = now) and it
    is the only input to freshness — see `sources.freshness_of()`. Only a source
    that can serve something it read earlier needs to set it; today that is git.
    """

    source: str
    text: str = ""
    label: str = ""
    pinned: bool = False
    meta: dict = field(default_factory=dict)
    stamp: float = 0.0
    priority: int | None = None
    relevance: float | None = None
    token_cost: int | None = None
    freshness: float | None = None

    @property
    def rendered(self) -> bool:
        return bool(self.text)

    def tokens(self) -> int:
        """Measured cost if it has one, else measure it now. Never `None`."""
        if self.token_cost is None:
            return _sources.token_cost(self.text)
        return int(self.token_cost)

    def to_payload(self) -> dict:
        return {
            "source": self.source,
            "label": self.label,
            "pinned": self.pinned,
            "chars": len(self.text),
            "rendered": self.rendered,
            "priority": self.priority,
            "relevance": self.relevance,
            "tokens": self.token_cost,
            "freshness": self.freshness,
            "meta": self.meta,
        }


@dataclass
class ContextRequest:
    """What the caller knows about this turn.

    Every field is optional, because the broker must be callable from a surface
    that knows very little (a health probe, `/api/platform`) as well as from the
    middle of an agent turn. `project` defaults to the current workspace's
    canonical key — `core.context.project_key()`, never a raw path, for the reason
    migration 10 exists.
    """

    chat_id: str = ""
    message: str = ""
    project: str = ""
    session_id: str = ""
    model_key: str = ""
    mode_key: str = ""
    surface: str = ""
    conversation_tokens: int = 0
    conversation_messages: int = 0
    include: frozenset[str] | None = None       # None = every registered source
    exclude: frozenset[str] = frozenset()

    def wants(self, source: str) -> bool:
        if source in self.exclude:
            return False
        return self.include is None or source in self.include


@dataclass
class ContextBundle:
    """The broker's output: the selected items plus what went wrong collecting them.

    `errors` is not decoration. A source that failed is *absent from the prompt*,
    and a bundle that reported only what it managed to gather would make that
    indistinguishable from a source that had nothing to say — which is precisely
    the difference between "ZAP is quiet" and "we could not ask ZAP".
    """

    request: ContextRequest
    items: list[ContextItem] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    # Task 22. `None` until a budget has been planned — `assemble()` always plans
    # one, a hand-built bundle has not. It is NOT a default-constructed plan,
    # because "nothing was dropped" and "nobody has decided yet" are different
    # facts and a report must not turn the second into the first.
    plan: _budget.BudgetPlan | None = None

    def by_source(self) -> dict[str, list[ContextItem]]:
        out: dict[str, list[ContextItem]] = {}
        for it in self.items:
            out.setdefault(it.source, []).append(it)
        return out

    def get(self, source: str) -> list[ContextItem]:
        return [it for it in self.items if it.source == source]

    def sent(self) -> list[ContextItem]:
        """The items that actually reach the model — THE one answer (Task 22).

        `items` is what was *collected*; this is what survived the budget. Both
        `prompt_tail()` and `sources_used()` read it, so "what is in the prompt" is
        decided once: a renderer that consulted `items` while the report consulted
        the plan would describe a prompt that was never sent.
        """
        if self.plan is None:
            return list(self.items)
        return list(self.plan.kept)

    def dropped(self) -> list[ContextItem]:
        """Items the budget left out. Empty when nothing was planned or nothing cut."""
        return list(self.plan.dropped) if self.plan is not None else []

    def sources_used(self) -> list[str]:
        """Sources that contributed rendered text TO THE PROMPT, in render order."""
        got = {it.source for it in self.sent() if it.rendered}
        return [s for s in ORDER if s in got]

    def ranked(self) -> list[ContextItem]:
        """Items in SURVIVAL order — `sources.rank()`, never a second sort.

        Pinned first, then priority, then relevance, then freshness. This is what
        a budget consumes (Task 22) and what a report shows; deriving it twice is
        how a report comes to disagree with the trim it is describing.
        """
        return _sources.rank(self.items)

    def total_tokens(self) -> int:
        """Estimated prompt tokens of everything COLLECTED, plus the conversation.

        The pre-trim cost, deliberately: `plan.used` is what is actually sent, and
        keeping both means a report can say "we gathered 40k and sent 9k" instead of
        making the trim invisible in its own accounting.

        The conversation contributes the caller's own count and no text, which is
        exactly why it is a pinned accounting item: the number belongs in the total
        even though this module never reads the rows.
        """
        return sum(it.tokens() for it in self.items) + \
            max(0, int(self.request.conversation_tokens or 0))

    def prompt_tail(self) -> str:
        """Every item that FIT, in `ORDER`, plus the omission notice if any were cut.

        THE composition — see the module note. The notice is `budget.notice()`'s and
        renders after the items because it describes them; it is not a source and has
        no `ORDER` slot for that reason.
        """
        return _render(self.sent()) + _budget.notice(self.plan)

    def to_payload(self) -> dict:
        return {
            "project": self.request.project,
            "chat_id": self.request.chat_id,
            "session_id": self.request.session_id,
            "surface": self.request.surface,
            "sources": self.sources_used(),
            "items": [it.to_payload() for it in self.items],
            "ranked": [it.source for it in self.ranked()],
            "chars": len(self.prompt_tail()),
            "tokens": self.total_tokens(),
            # Task 22. `None` says "no budget was planned for this bundle", which is
            # not the same as an empty trim — see the `plan` field.
            "budget": self.plan.to_payload() if self.plan is not None else None,
            "conversation": {
                "messages": self.request.conversation_messages,
                "tokens": self.request.conversation_tokens,
            },
            "errors": dict(self.errors),
        }


# ── Cached always-on blocks ────────────────────────────────────────────────────
# ⚠️ THESE TWO CACHES ARE THE ONES `agent.py` USED TO OWN, MOVED — NOT COPIED.
# `agent._MEM_CACHE` / `agent._RULES_CACHE` are now names bound to these objects,
# so `A._MEM_CACHE.invalidate()` in the existing tests still invalidates the cache
# the prompt actually reads. A second `VersionedCache("memories", …)` would look
# identical and be wrong in the worst way: the surface that invalidated would keep
# serving its own stale copy while insisting it had refreshed.

def _build_memory_block() -> str:
    # Delegates so the bound (PROMPT_LIMIT) and the ranking live in ONE place.
    from agent2.core import memory as _memory
    return _memory.memory_prompt_block()


def _build_rules_block() -> str:
    from agent2.core import rules as _rules
    return _rules.rules_prompt_block()


MEM_CACHE = _sync.VersionedCache("memories", _build_memory_block)
RULES_CACHE = _sync.VersionedCache("rules", _build_rules_block)


# ⚠️ AND THAT IS EXACTLY WHY A WORKSPACE SWITCH MUST INVALIDATE BOTH (Task 23).
# These two caches are process-global and keyed on the `memories` / `rules`
# resources. Nothing about switching workspace bumps either resource — `/workspace`
# writes a setting and returns — so once the blocks became project-scoped, a switch
# inside one process would keep serving project A's memories and project A's RULES
# to project B: the precise leak `isolation.py` exists to prevent, arriving through
# the cache rather than through a query. `isolation.invalidate()` is called on
# `manager.on_switch` and on a `workspace` sync event; this is the listener that
# turns that into a rebuild.
def _on_project_change() -> None:
    MEM_CACHE.invalidate()
    RULES_CACHE.invalidate()


_isolation.on_invalidate(_on_project_change)


def memory_block() -> str:
    return MEM_CACHE.get()


def rules_block() -> str:
    return RULES_CACHE.get()


# ── Collector registry ─────────────────────────────────────────────────────────
# A table rather than a chain of calls, so Phase 11 (skills) and Phase 12
# (workflows) arrive by `register()` instead of by editing `collect()`.

_COLLECTORS: dict[str, object] = {}
_REG_LOCK = threading.Lock()


def register(source: str, fn) -> None:
    """Bind a collector to *source*. Replacing one is allowed and intentional.

    Later phases own their own sources: `register(SOURCE_SKILLS, …)` from the
    skills loader, `register(SOURCE_WORKFLOW, …)` from the workflow engine. A
    collector takes a `ContextRequest` and returns a list of `ContextItem`.
    """
    with _REG_LOCK:
        _COLLECTORS[source] = fn


def collector(source: str):
    """Decorator form of `register()`."""
    def _wrap(fn):
        register(source, fn)
        return fn
    return _wrap


def registered() -> list[str]:
    """Sources with a collector, in render order."""
    with _REG_LOCK:
        have = set(_COLLECTORS)
    return [s for s in ORDER if s in have]


# ── Collectors ─────────────────────────────────────────────────────────────────

@collector(SOURCE_CONVERSATION)
def _collect_conversation(req: ContextRequest) -> list[ContextItem]:
    """Accounting only — the caller owns the turn history.

    Renders nothing by design. `agent.build_context()` is THE reader of the
    `messages` table; this item exists so the conversation's size is visible to
    Task 22's budget and to `/api` reporting, and so a reader of the bundle can
    see that history was part of the context rather than guess it.
    """
    return [ContextItem(
        source=SOURCE_CONVERSATION,
        text="",
        label="Conversation",
        pinned=True,
        meta={"messages": req.conversation_messages,
              "tokens": req.conversation_tokens},
    )]


@collector(SOURCE_PROJECT)
def _collect_project(req: ContextRequest) -> list[ContextItem]:
    """The workspace and its documentation.

    ⚠️ ONE FILE IS INLINED AND THE REST ARE NAMED, WHICH IS NOT A SHORTCUT.
    `.agent2/agent2.md` is written for the agent — it is instructions, and
    instructions have to be in the prompt to be followed. A README is written for
    humans, is frequently enormous, and is exactly the kind of thing the agent can
    `read_file` when it turns out to matter. Inlining both would spend a large,
    fixed share of every prompt on prose the model mostly does not need, on every
    turn, forever.
    """
    from agent2.core import workspace as _ws

    ws = _ws.current()
    root = ws.root
    lines = [f"- Root: {root}"]
    name = ""
    try:
        name = (ws.metadata or {}).get("name", "") or Path(root).name
    except Exception:
        name = Path(root).name if root else ""
    if name:
        lines.insert(0, f"- Project: {name}")

    found: list[str] = []
    primary = ""
    base = Path(root) if root else None
    # ⚠️ Case-insensitively deduped, because `PROJECT_DOCS` deliberately lists both
    # `README.md` and `readme.md` for case-sensitive filesystems — and on Windows
    # and macOS BOTH names hit the same file, so the naive list told the model the
    # project had two documentation files where it has one.
    seen: set[str] = set()
    if base is not None:
        for rel in PROJECT_DOCS:
            try:
                p = base / rel
                if not p.is_file():
                    continue
                real = os.path.normcase(os.path.abspath(str(p)))
                if real in seen:
                    continue
                seen.add(real)
                found.append(rel)
                if rel == PRIMARY_DOC and not primary:
                    primary = p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            if len(found) >= MAX_DOC_FILES:
                break
    if found:
        lines.append("- Documentation present: " + ", ".join(found))

    text = "\n\n## PROJECT CONTEXT\n" + "\n".join(lines)
    clip: dict = {}
    if primary.strip():
        # ⚠️ THE CLIP IS OWNERSHIP-AWARE, AND THAT DECISION IS NOT THIS MODULE'S.
        # `PROJECT_DOC_CHARS` is how much of the doc a prompt may spend — a budget,
        # which is a broker fact. *Which part survives* depends on which sections a
        # human took over, which only `projectdoc` knows, so it decides and this asks.
        # The `body[:PROJECT_DOC_CHARS]` slice that used to live here was head-first,
        # and a doc ends with the sections a human owns: `## Agent2 Instructions` was
        # the first thing dropped, so the standing orders the marker design exists to
        # protect never reached the model — silently, and only once the doc grew.
        from agent2.core import projectdoc as _pdoc
        body, clip = _pdoc.for_prompt(primary, PROJECT_DOC_CHARS)
        if clip.get("clipped"):
            gone = clip.get("dropped") or []
            if gone:
                # ⚠️ NAMED, NEVER COUNTED. "3 sections omitted" tells the model that
                # something is missing; naming them tells it what to go and read, and
                # `read_file` is a tool it already has.
                body += ("\n\n_(truncated for this prompt — generated section(s) not "
                         "shown: " + ", ".join(gone) + ". Read `" + PRIMARY_DOC +
                         "` for them.)_")
            else:
                body += ("\n…(project instructions truncated — read " + PRIMARY_DOC +
                         " for the rest)")
        text += (f"\n\n## PROJECT INSTRUCTIONS ({PRIMARY_DOC})\n"
                 "These are this project's own instructions. Follow them over your "
                 "general defaults.\n\n" + body)

    return [ContextItem(
        source=SOURCE_PROJECT,
        text=text,
        label="Project",
        meta={"root": str(root), "name": name, "docs": found,
              "primary": PRIMARY_DOC if primary else "",
              "primary_chars": len(primary),
              # What the prompt did NOT get. ⚠️ Reported rather than merely done:
              # a doc section missing from a prompt with nothing said about it is
              # indistinguishable from a doc that never had the section.
              "primary_clipped": bool(clip.get("clipped")),
              "primary_dropped": list(clip.get("dropped") or []),
              "primary_over": bool(clip.get("over"))},
    )]


@collector(SOURCE_GIT)
def _collect_git(req: ContextRequest) -> list[ContextItem]:
    """Repository state, from `core.gitstate` — the one git reader.

    Silent outside a repository: a "not a git repo" line is a fact the model
    cannot act on, and every prompt paying for it is worse than nothing.
    """
    from agent2.core import gitstate as _git
    from agent2.core import workspace as _ws

    snap = _git.snapshot(_ws.root())
    if not snap.get("repo"):
        return []

    lines = [f"- Branch: {_git.describe(_ws.root())}"]
    if snap.get("upstream"):
        lines.append(f"- Upstream: {snap['upstream']}")
    lines.extend(f"- {c['hash']} {c['subject']} ({c['when']})"
                 for c in (snap.get("commits") or [])[:_git.LOG_COUNT])

    return [ContextItem(
        source=SOURCE_GIT,
        text="\n\n## GIT STATE\n" + "\n".join(lines),
        label="Git",
        # ⚠️ `stamp` is when `gitstate` actually ran `git`, not when this collector
        # ran. It is the only source that can serve something it read up to
        # `GIT_TTL` ago, so reporting "now" here is what would make a stale
        # snapshot indistinguishable from a fresh one in the freshness measure.
        stamp=float(snap.get("at") or 0.0),
        meta={k: snap.get(k) for k in
              ("branch", "head", "dirty", "staged", "unstaged",
               "untracked", "ahead", "behind", "upstream")},
    )]


def _session_visible(sid: str, req: ContextRequest, _tasks) -> bool:
    """May *req*'s project see task session *sid*? Total; never raises.

    Split out of `_collect_tasks` so the same check is available to Phase 8's
    recovery path, which resolves sessions the same way and has the same problem.
    """
    try:
        row = _tasks.get_session(sid) or {}
        return _isolation.allows(row.get("cwd"), req.project or None)
    except Exception:
        return True             # cannot place it ⇒ treat as shared, see below


@collector(SOURCE_TASKS)
def _collect_tasks(req: ContextRequest) -> list[ContextItem]:
    """The persistent plan and its results, from `core.tasks`.

    ⚠️ THIS IS NOT THE RECOVERY BRIEF AND MUST NOT BECOME ONE.
    `core/recovery.py` owns the one-shot "here is what was already done, do NOT
    run it again" message, it is consumed exactly once, and it is inserted as its
    own turn. This block is the *standing* picture of the plan — it reappears
    every turn while a session is open, which is why it names results rather than
    instructing anything. Two sources of "do not redo this" would eventually
    disagree, and the one that says "go ahead" wins by accident.

    ⚠️ A SESSION FROM ANOTHER WORKSPACE IS REFUSED (Task 23). Task *results* are
    the fourth thing the isolation rule names, and this collector is the only path
    by which they reach a prompt. A chat id is not project-scoped — the same chat
    can be reopened from anywhere, and `latest_session_for_chat` happily returns
    the session that chat ran in a different checkout — so the plan and the
    completed-step summaries of project B would be injected here as this project's
    standing state, with the model then reporting work that was never done here.
    The check is `isolation.allows`, which is fail-open for a session whose `cwd`
    we cannot place: an unknown owner is pre-Task-23 data or a hand-made row, and
    only a KNOWN different project is rejected.
    """
    from agent2.core import tasks as _tasks

    sid = req.session_id
    if not sid and req.chat_id:
        row = _tasks.active_session_for_chat(req.chat_id) \
            or _tasks.latest_session_for_chat(req.chat_id)
        sid = (row or {}).get("id", "") if row else ""
    if not sid:
        return []
    if not _session_visible(sid, req, _tasks):
        return []

    items = _tasks.list_tasks(sid)
    if not items:
        return []
    summ = _tasks.summary(sid, items)

    lines = [f"- Plan: {summ['progress']} completed, {summ['open']} open"]
    cur = _tasks.current(sid)
    if cur is not None:
        lines.append(f"- In progress: {cur.title}")

    done = [t for t in items if t.status == _tasks.TaskStatus.COMPLETED]
    for t in done[:MAX_TASK_LINES]:
        res = (t.result or "").strip().replace("\n", " ")
        lines.append(f"- Done: {t.title}" + (f" → {res[:120]}" if res else ""))
    if len(done) > MAX_TASK_LINES:
        lines.append(f"- …and {len(done) - MAX_TASK_LINES} more completed steps")

    failed = [t for t in items if t.status == _tasks.TaskStatus.FAILED]
    for t in failed[:MAX_TASK_LINES]:
        err = (t.error or "").strip().replace("\n", " ")
        lines.append(f"- Failed: {t.title}" + (f" → {err[:120]}" if err else ""))

    return [ContextItem(
        source=SOURCE_TASKS,
        text="\n\n## TASK STATE (persistent plan)\n" + "\n".join(lines),
        label="Tasks",
        meta={"session_id": sid, "total": summ["total"],
              "completed": summ["completed"], "open": summ["open"],
              "failed": len(failed)},
    )]


@collector(SOURCE_MCP)
def _collect_mcp(req: ContextRequest) -> list[ContextItem]:
    """MCP availability — and deliberately NOT the tool descriptions.

    ⚠️ NO DUPLICATION WITH `McpBridge.prompt_block()`. A connected bridge already
    contributes its own hand-written section through `system_prompt(mcp_blocks=…)`,
    and repeating it here would spend the tokens twice and create a second place
    where a server describes itself. What is missing from that path is the
    negative case, which is the one the model gets wrong: a server the user
    *enabled* but that is not answering still has no tools registered, so the
    model invents a `zap_*` call and reads "not registered" as its own mistake.
    So this renders the gap, and keeps the counters in `meta` for reporting.

    `registry.health()` never dials a socket — see its docstring — so this is safe
    on the turn path.
    """
    from agent2.integrations import registry as _reg

    rows = _reg.health()
    missing = [r for r in rows if r.get("enabled") and not r.get("connected")]
    counts = {
        "total": len(rows),
        "enabled": sum(1 for r in rows if r.get("enabled")),
        "connected": sum(1 for r in rows if r.get("connected")),
    }
    if not missing:
        return [ContextItem(source=SOURCE_MCP, text="", label="MCP", meta=counts)]

    lines = [f"- {r['label']}: enabled but not connected ({r.get('text', '')}) — "
             f"its tools are unavailable this turn; do not call them."
             for r in missing]
    return [ContextItem(
        source=SOURCE_MCP,
        text="\n\n## MCP SERVERS\n" + "\n".join(lines),
        label="MCP",
        meta=dict(counts, unavailable=[r["key"] for r in missing]),
    )]


@collector(SOURCE_FILES)
def _collect_files(req: ContextRequest) -> list[ContextItem]:
    """Files this process has already changed, from `core.diffs.store`.

    In-memory and per-process by design (see `DiffStore`), so in dual mode this
    means "changed by *this* surface". That is the honest scope and the block says
    "this session" rather than "this project" for exactly that reason.
    """
    from agent2.core import diffs as _diffs

    changes = _diffs.store.all()
    if not changes:
        return []
    shown = changes[-MAX_FILE_LINES:]
    lines = [f"- {c.path} ({c.kind}, +{c.added} −{c.removed})" for c in shown]
    if len(changes) > len(shown):
        lines.insert(0, f"- …{len(changes) - len(shown)} earlier changes not listed")

    summ = _diffs.summarize(changes)
    return [ContextItem(
        source=SOURCE_FILES,
        text="\n\n## FILES YOU CHANGED THIS SESSION\n" + "\n".join(lines),
        label="Files",
        meta=summ,
    )]


@collector(SOURCE_SKILLS)
def _collect_skills(req: ContextRequest) -> list[ContextItem]:
    """This turn's selected skills, from `core.skills` (Phase 11, Tasks 32–36).

    ⚠️ **THE SELECTION IS NOT MADE HERE.** `skills.for_turn()` owns it — discovery,
    the user's per-project choices, the deterministic order and the two caps — and
    this collector renders what it returns. That is why the slot was declared empty
    in Task 20: a broker that decided *which* skills apply would be a second
    ordering beside `select.REASONS`, and the prompt would then contain one
    selection while `/skills` and `GET /api/skills` described another.

    ⚠️ **`relevance` IS PRE-STATED, DELIBERATELY.** `sources.measure()` fills only
    what a collector left unset, and its generic pass scores an item by word overlap
    with the whole rendered text — which for skills is backwards: the longest file in
    the folder would out-score the one the user named. Selection already computed a
    relevance from what each skill *declares*, so it is carried here and the generic
    pass leaves it alone (see `ContextItem`'s "not stated is not zero").

    A skill that reached the prompt is reported in `meta` by id and reason only —
    never its text. `ContextBundle.to_payload()` is served to a browser.
    """
    from agent2.core import skills as _skills

    sel = _skills.for_turn(req.message)
    text = _skills.prompt_block(sel)
    if not text:
        return []
    # Selection's integer score against its own strongest possible signal, mapped
    # into the 0–1 measure the rest of Task 21 speaks. `request` is 1.0 by
    # definition: the user named it, and no lexical score outranks that.
    top = max((a.score for a in sel.applied), default=0) or 1
    best = max((_skills.REASONS.index(a.reason) for a in sel.applied), default=0)
    rel = 1.0 if best == 0 else min(1.0, max(0.2, top / (top + 3.0)))
    return [ContextItem(
        source=SOURCE_SKILLS,
        text="\n\n" + text,
        label="Skills",
        relevance=rel,
        stamp=sel.at,
        meta={
            "applied": [{"id": a.skill.id, "reason": a.reason} for a in sel.applied],
            "count": len(sel.applied),
            "considered": sel.considered,
            "omitted": len(sel.omitted),
            "chars": sel.chars,
            "truncated": sel.catalog_truncated,
        },
    )]


@collector(SOURCE_WORKFLOW)
def _collect_workflow(req: ContextRequest) -> list[ContextItem]:
    """The workflow this turn is a node of, from `core.workflow` (Task 37).

    ⚠️ **THE STATE IS NOT DERIVED HERE.** `workflow.for_turn()` owns it — which run
    is live, which node is current, what is still blocked and the character ceiling
    the block may spend — and this collector renders what it returns. Exactly the
    split `_collect_skills` documents, for the same reason: the slot was declared
    empty in Task 20 so that Phase 12 could fill it *without* the broker learning a
    workflow fact of its own. `runner.state_for()` is the one progress answer, and a
    second one here would let the prompt describe a run the `/workflow` panel does
    not recognise.

    ⚠️ **`relevance` IS PRE-STATED AT 1.0.** A live workflow is not lexically
    related to the message — the message is usually the node's own work — and
    `sources.measure()`'s generic word-overlap pass would score it near the floor
    and make it the first thing a tight budget discards. That is backwards: the one
    block that says *do not redo a finished node* is the one a truncated turn most
    needs. It is scored, not pinned: `ALWAYS` stays the two standing instruction
    blocks, so an over-budget turn can still drop this ahead of the user's rules.

    A turn with no live run pays one indexed `exec_workflows` read and returns
    nothing. `meta` carries counts and node **ids** only — never an instruction.
    """
    from agent2.core import workflow as _wf

    view = _wf.for_turn(chat_id=req.chat_id, project=(req.project or None))
    text = view.get("text") or ""
    if not view.get("active") or not text:
        return []
    run = view.get("run") or {}
    return [ContextItem(
        source=SOURCE_WORKFLOW,
        text="\n\n## WORKFLOW IN PROGRESS\n" + text,
        label="Workflow",
        relevance=1.0,
        meta={
            "run_id": run.get("run_id", ""),
            "name": run.get("name", ""),
            "current": run.get("current", ""),
            "done": run.get("done", 0),
            "total": run.get("total", 0),
            "failed": run.get("failed", 0),
            "omitted": view.get("omitted", 0),
            "truncated": bool(view.get("truncated")),
        },
    )]


@collector(SOURCE_MEMORY)
def _collect_memory(req: ContextRequest) -> list[ContextItem]:
    """The bounded, ranked memories block — from the cache the prompt already used."""
    block = memory_block()
    return [ContextItem(source=SOURCE_MEMORY, text=block, label="Memory",
                        pinned=True, meta={"chars": len(block)})]


@collector(SOURCE_RULES)
def _collect_rules(req: ContextRequest) -> list[ContextItem]:
    """Active custom rules — the user's standing orders, so pinned."""
    block = rules_block()
    return [ContextItem(source=SOURCE_RULES, text=block, label="Rules",
                        pinned=True, meta={"chars": len(block)})]


# ── Assembly ───────────────────────────────────────────────────────────────────

def _render(items: list[ContextItem]) -> str:
    """Concatenate rendered items in `ORDER`. The ONE composition.

    Stable within a source (registration order is preserved for items a single
    collector returned) and total: an item whose text is empty contributes
    nothing, so an accounting-only source costs no prompt space.
    """
    rank = {s: i for i, s in enumerate(ORDER)}
    ordered = sorted(
        enumerate(items),
        key=lambda pair: (rank.get(pair[1].source, len(ORDER)), pair[0]),
    )
    return "".join(it.text for _, it in ordered if it.text)


def _default_project() -> str:
    """This workspace's project key — `isolation.current()`, not a second copy.

    ⚠️ It used to compute `context.project_key(workspace.root())` here. That is
    now exactly what `isolation.current()` does, and two copies of "which project
    am I" is the drift that makes a scoped read and a scoped write disagree: the
    bundle would be built for one key while `core.memory` filtered on the other,
    and the visible symptom is an empty memories block, not an error.
    """
    return _isolation.current()


def request(**kwargs) -> ContextRequest:
    """Build a `ContextRequest`, defaulting `project` to this workspace's key."""
    req = ContextRequest(**kwargs)
    if not req.project:
        req.project = _default_project()
    return req


def collect(req: ContextRequest) -> ContextBundle:
    """Run every wanted collector, guarding each one. THE gathering step.

    ⚠️ ONE GUARD PER COLLECTOR, NOT ONE AROUND THE LOOP. A single try/except
    around the whole loop would let the first failing source discard every source
    after it — and since `ORDER` puts memory and rules last, a broken git binary
    would silently strip the user's own rules from the prompt. Per-source, a
    failure costs exactly that source.

    ⚠️ MEASURING HAPPENS HERE AND NOWHERE ELSE (Task 21). Every item that lands on
    a bundle passes through `sources.measure()`, so a collector registered by a
    later phase gets priority, relevance, token cost and freshness for free instead
    of arriving unmeasured and ranking last by accident. A collector that stated a
    value keeps it — `measure()` only fills `None`.

    ⚠️ SO DOES `memory.retrieval` (Task 27), and per SOURCE rather than for the
    memory collector alone. Task 27 asks for "memory retrieval latency"; the same
    timer around the same loop answers it for all ten sources at a bounded ten
    labels, and the useful form of the question is comparative — a `git_state`
    source that has started taking 400 ms is invisible in a number that only
    watches `memory`. A collector that raises is timed too: its failure took time,
    and a source that fails slowly is the one worth finding.
    """
    with _REG_LOCK:
        table = dict(_COLLECTORS)

    bundle = ContextBundle(request=req)
    for source in ORDER:
        fn = table.get(source)
        if fn is None or not req.wants(source):
            continue
        try:
            with _metrics.timer(_metrics.MEMORY_RETRIEVAL, source):
                got = fn(req) or []
        except Exception as exc:                      # never into a turn
            bundle.errors[source] = f"{type(exc).__name__}: {exc}"[:200]
            continue
        for it in got:
            if isinstance(it, ContextItem):
                bundle.items.append(_sources.measure(it, req))
    return bundle


def assemble(**kwargs) -> ContextBundle:
    """`request()` → `collect()` → `budget.apply()`. What a caller actually invokes.

    ⚠️ THE BUDGET IS APPLIED HERE, NOT BY THE CALLER (Task 22). Two surfaces
    assemble a bundle today and Phases 11–13 add more; "remember to budget it" is
    exactly the kind of step a new caller omits, and the omission is invisible until
    the one turn that overflows the window. `collect()` stays pure gathering so a
    report can still show what was collected *before* the trim — `bundle.items` is
    everything, `bundle.sent()` is what fits, and `bundle.plan` is the difference.

    ⚠️ THE PER-SOURCE FAILURE IS LOGGED HERE TOO, FOR THE SAME REASON THE TRIM IS.
    `agent.py` and `llm/provider_agent.py` each carried their own copy of this loop
    and `agent2cli.py` would have made a third — and a warning emitted per surface
    ends up emitted by only *some* of them: the loop nobody remembered then loses a
    source with no record anywhere, which is the one thing
    `context_source_failed` exists to make visible. It lives in `assemble()` rather
    than in `collect()` so that `collect()` stays the pure gathering step a report
    or a dry-run can call without writing to the audit log.

    ⚠️ `context.size` IS RECORDED HERE, AND IT IS `plan.used` — WHAT WAS SENT, NOT
    WHAT WAS COLLECTED (Task 27). The two differ by exactly the trim, and the
    interesting number is the one the vendor was charged for; reading `items`
    instead would report a prompt that was never sent, which is the failure
    `sent()` exists to prevent. It is recorded after `apply()` for the same reason,
    and only when a plan exists — a hand-built bundle has no `plan`, and "nobody
    planned one" is not the same fact as "nothing was dropped".
    """
    bundle = collect(request(**kwargs))
    for _src, _err in bundle.errors.items():
        try:
            from agent2.core import logging as _alog
            _alog.context_source_failed(_src, _err)
        except Exception:
            pass
    _budget.apply(bundle)
    if bundle.plan is not None:
        _metrics.observe(_metrics.CONTEXT_SIZE, bundle.plan.used)
    return bundle


def base_tail() -> str:
    """The always-on tail: memories then rules, from the warm caches.

    ⚠️ THIS IS WHAT KEEPS A BARE `system_prompt()` FREE. Both this and
    `ContextBundle.prompt_tail()` compose through `_render`/`ORDER`, so there is
    one ordering; the difference is only *which* sources are present. A caller
    with no bundle (the provider path's fallback, `/api/platform`, every prompt
    test) gets exactly the two cached blocks the prompt has always ended with, and
    pays no query for them.

    The two items here are deliberately left unmeasured (Task 21) and unbudgeted
    (Task 22): nothing trims them — they are pinned, always-on and the whole content
    of this tail, so a budget could only ever confirm that all of it stays — and
    measuring is `collect()`'s job, on the path that has a request to measure
    against.
    """
    return _render([
        ContextItem(source=SOURCE_MEMORY, text=memory_block(), pinned=True),
        ContextItem(source=SOURCE_RULES, text=rules_block(), pinned=True),
    ])


def stats() -> dict:
    """Broker posture — counters and policy only, never any item's text.

    Read by `/api/health`'s context section (Task 28). Deliberately says nothing
    about a specific turn: a ceiling depends on that turn's model and mode, and the
    project key is a user's absolute working directory.
    """
    return {
        "sources": list(ORDER),
        "registered": registered(),
        "always": sorted(ALWAYS),
        # The Task 21 table itself — order, priority, TTL and always-on flag per
        # source, read from `sources` rather than restated, so a surface reporting
        # the policy cannot describe a policy the broker is not applying.
        "table": _sources.table(),
        # The Task 22 policy — the knobs a ceiling is derived from, never a ceiling
        # (that one depends on the turn's model and mode).
        "budget": _budget.describe(),
        # The Task 23 policy. ⚠️ Policy, not the project key: `/api/health` is
        # counters-and-policy, and a user's absolute working directory is neither.
        "isolation": _isolation.describe(),
        "memory_cache": {"hits": MEM_CACHE.hits.value,
                         "misses": MEM_CACHE.misses.value},
        "rules_cache": {"hits": RULES_CACHE.hits.value,
                        "misses": RULES_CACHE.misses.value},
    }
