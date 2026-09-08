# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2.core.workflow
────────────────────
THE workflow subsystem — Tasks 37-39. Four modules, one job each:

  * `graph.py`     — what a workflow IS, and whether it can run at all (Task 37)
  * `runner.py`    — where a validated graph becomes task rows (Task 37)
  * `loader.py`    — `.agent2/workflows/*.yaml`, read and version-tolerant (Task 38)
  * `authoring.py` — the ONE writer of a workflow file: new · edit · delete (Task 39)

This package is deliberately **not** an execution engine. Task 37's bar is
*"nodes are tasks, with the existing checkpoints and recovery"*, so a run is one
`task_sessions` row, one `agent_tasks` row per node and one `exec_workflows` row —
tables, writers and indexes that all existed before Phase 12. What this package
adds is the part that was genuinely missing: a **definition**, a **validation**
that refuses a graph `tasks.ready()` would deadlock on forever, and a
**topological order** for the bounded parallelism Task 41 needs.

⚠️ **`for_turn()` IS WHAT THE BROKER RENDERS, AND IT IS THE WHOLE CONTEXT STORY.**
`broker._collect_workflow` is a renderer over this function — the same split Task
34 pinned for skills, for the same reason: the broker may hold no workflow fact it
could drift on. And the block it renders is small on purpose. A turn needs to know
*which* workflow is running and *which node it is doing*; the other nodes are
**named, never inlined**, because inlining a whole plan every turn is how a
worker ends up carrying the entire parent conversation — the one thing the user's
standing context bar rules out, and Task 41's isolation clause in advance.

Everything here is total. A workflow is a convenience: a broken run row, a
missing table or a file nobody can parse costs the workflow block and nothing
else, so no function in this package raises into a turn.
"""

from __future__ import annotations

from agent2 import config
from agent2.core.workflow import authoring, graph, loader, runner
from agent2.core.workflow.authoring import Authored
from agent2.core.workflow.graph import (
    MIN_SCHEMA,
    SCHEMA_VERSION,
    Node,
    Validation,
    WorkflowDef,
    fold_id,
    levels_for,
    make_def,
    make_node,
    validate,
)
from agent2.core.workflow.loader import (
    Catalog,
    WorkflowFile,
    build,
    discover,
    load,
    parse_yaml,
    path_for,
    workflows_root,
)
from agent2.core.workflow.runner import (
    Run,
    NodeState,
    RunState,
    advance,
    instantiate,
    live,
    recover,
    resume,
    runs,
    settle,
    state_for,
    verify,
)

__all__ = [
    "MIN_SCHEMA", "SCHEMA_VERSION", "Authored", "Catalog", "Node", "NodeState",
    "Run", "RunState", "Validation", "WorkflowDef", "WorkflowFile", "advance",
    "authoring", "build", "describe", "discover", "fold_id", "for_turn", "graph",
    "instantiate", "levels_for", "live", "load", "loader", "make_def", "make_node",
    "parse_yaml", "path_for", "recover", "resume", "runner", "runs", "settle",
    "state_for", "stats", "validate", "verify", "workflows_root",
]

#: How many other nodes `for_turn()` will name before it stops naming them. The
#: point of the list is orientation, not a table of contents: past a handful a
#: human reads "and 40 more" exactly as usefully as forty titles, and the block
#: has a character ceiling it would otherwise spend here.
NAMED_NODES = 6


def _clip(text: str, limit: int) -> tuple[str, bool]:
    """Trim to *limit*, reporting whether it engaged. One place, so the ellipsis
    and the flag cannot disagree."""
    body = text or ""
    if len(body) <= limit:
        return body, False
    return body[: max(0, limit - 1)].rstrip() + "…", True


def for_turn(*, chat_id: str = "", project=None) -> dict:
    """What this turn should be told about a running workflow. Total and bounded.

    Returns a payload the broker renders and `/workflow` prints:

        active     — is a run live at all (False ⇒ `text` is empty)
        text       — the block, already inside `config.WORKFLOW_STATE_CHARS`
        truncated  — the cap engaged, and it says so rather than clipping silently
        run        — `RunState.to_payload()`, or `{}`
        omitted    — how many sibling nodes were named as a count instead of a title

    ⚠️ The **current node's instruction** is the one piece of prose here. Every
    other node contributes its title, and past `NAMED_NODES` only a count. That
    ordering is the context bar: a turn is doing one node, so one node's detail is
    what it needs, and the rest is orientation.
    """
    out = {"active": False, "text": "", "truncated": False, "run": {}, "omitted": 0}
    if not config.WORKFLOW_ENABLED:
        return out
    try:
        state = runner.live(project=project, chat_id=chat_id)
    except Exception:
        return out
    if state is None or not state.nodes:
        return out

    out["active"] = True
    out["run"] = state.to_payload()
    cur = state.current()
    lines = [f"Workflow `{state.name}` is running — "
             f"{state.done}/{state.total} nodes settled."]
    if cur is not None:
        lines.append(f"Current node: `{cur.node}` — {cur.title}")
        note = (cur.instruction or "").strip()
        if note:
            lines.append(f"What it asks for: {note}")
        if cur.upstream_failed:
            # ⚠️ Said out loud, because `tasks.ready()` released this node on
            # *settled*, not on *succeeded*. A turn that is about to build on a step
            # which failed needs to know that; silently proceeding is how a workflow
            # produces a confident answer from a broken input.
            lines.append("⚠ Upstream node(s) did not succeed: "
                         + ", ".join(f"`{n}`" for n in cur.upstream_failed))
    remaining = [n for n in state.nodes
                 if n.phase not in (runner.PHASE_DONE, runner.PHASE_FAILED)
                 and (cur is None or n.node != cur.node)]
    if remaining:
        named = remaining[:NAMED_NODES]
        out["omitted"] = len(remaining) - len(named)
        tail = ", ".join(f"`{n.node}`" for n in named)
        if out["omitted"]:
            tail += f", and {out['omitted']} more"
        lines.append(f"Still to do: {tail}")
    if state.failed:
        lines.append("Failed: " + ", ".join(f"`{n.node}`" for n in state.failed))
    lines.append("Do the current node only. Do not start a node that is still "
                 "blocked, and do not redo one that is already settled.")

    body, cut = _clip("\n".join(lines), config.WORKFLOW_STATE_CHARS)
    out["text"] = body
    out["truncated"] = cut
    return out


def describe() -> dict:
    """The subsystem's own posture — read by `/workflow` and `GET /api/workflows`."""
    out = {
        "enabled": config.WORKFLOW_ENABLED,
        "schema": SCHEMA_VERSION,
        "min_schema": MIN_SCHEMA,
        "state_chars": config.WORKFLOW_STATE_CHARS,
        "named_nodes": NAMED_NODES,
    }
    try:
        out.update(graph.describe())
    except Exception:
        pass
    try:
        # ⚠️ AFTER `graph.describe()`, deliberately: `loader.describe()` *wraps* it
        # and its `yaml`/`yaml_subset` pair is the more specific answer to "what
        # shape does a workflow file take". Both orders "work"; only this one tells
        # a user whose file did not load which YAML this build actually reads.
        out.update(loader.describe())
    except Exception:
        pass
    try:
        out["runs"] = runner.stats()
    except Exception:
        out["runs"] = {}
    try:
        out["files"] = loader.stats()
    except Exception:
        out["files"] = {}
    try:
        out["authoring"] = authoring.describe()
    except Exception:
        out["authoring"] = {}
    return out


def stats() -> dict:
    """Counters only, for `core.health`. Never a node title or an instruction."""
    out = runner.stats()
    try:
        out["files"] = loader.stats()
    except Exception:
        out["files"] = {}
    return out
