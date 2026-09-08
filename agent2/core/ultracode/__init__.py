# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2.core.ultracode
─────────────────────
THE adaptive autonomous engineering loop — Phase D5. Three modules, one job each:

  * `stages.py` — what stage a run is IN, and what a node KIND means (the words)
  * `plan.py`   — what the run is ABOUT, and the SHAPE the plan is given
  * `engine.py` — the driver: start · approve · work · replan · finalize · drive

The spec's loop is

    UNDERSTAND → INSPECT → DISCOVER SKILLS → PLAN → BUILD DAG → EXECUTE
      → OBSERVE → ANALYZE → VERIFY → (pass ⇒ continue · fail ⇒ diagnose
      → RE-PLAN → UPDATE DAG → EXECUTE)

⚠️ DISCOVER SKILLS comes **before** PLAN — the spec's own order (D5.34 goal → D5.35
project → D5.36 skills → D5.37 planning), and `stages.STAGES` is the one declaration
of it. `plan.brief()` gathers the first three arrows in one call precisely so the
skills this project has are in hand before a model is asked for steps.

and every arrow of it is owned by a module that already existed:

  | arrow                  | who does it                                    |
  |------------------------|------------------------------------------------|
  | UNDERSTAND · INSPECT   | `plan.brief()` (`projectscan` · `gitstate`)    |
  | DISCOVER SKILLS        | `plan.brief()` (`core.skills.for_turn`)        |
  | PLAN · RE-PLAN         | `core.workflow.dynamic`                        |
  | BUILD · UPDATE DAG     | `dag.store.create()` / `.extend()`             |
  | EXECUTE · OBSERVE      | `dag.schedule.run()`                           |
  | ANALYZE                | `core.recovery.classify` (through `schedule`)  |
  | VERIFY                 | `core.verify` — as a NODE, never a post-pass   |
  | which stage we are in  | `stages.stage_of()`, derived from the rows     |

⚠️ **SO THIS PACKAGE HOLDS NO FACT OF ITS OWN.** It is the fourth consumer of the
one generalized DAG (Workflow · Dynamic Workflow · UltraCode), never a second
engine: a node is an `agent_tasks` row and a run is one `exec_workflows` row, so
the checkpoints, the heartbeat, `/recovery` and the Phase 8 crash scan an
UltraCode run gets are the ones that already existed. **No new table, no
migration.**

⚠️ **THERE IS NO `for_turn()` HERE, AND THAT IS DELIBERATE.**
`core.workflow.for_turn()` already tells a turn about the run it is part of, and an
UltraCode run *is* an `exec_workflows` row — so a second collector would be a second
declaration of what a live run tells a prompt, for the one source
(`broker.ORDER`'s `workflow_state`) that already answers it.

⚠️ **`describe()` IS THE ENGINE'S, AND IT IS THE ONLY ONE RE-EXPORTED.** All three
modules define one; `engine.describe()` nests the other two, so a surface asking
"what is this subsystem's posture" gets one answer rather than three that agree
until somebody edits one of them.

Entered explicitly, never automatically — `/ultracode` and `POST /api/ultracode`
are the only two doors. `AGENT2_ULTRACODE=0` refuses every entry point.
"""

from __future__ import annotations

from agent2.core.ultracode import engine, plan, stages
from agent2.core.ultracode.engine import (
    REFUSALS,
    U_BUDGET,
    U_CYCLES,
    U_ENGINE,
    U_GATE,
    U_LIVE,
    U_MISSING,
    U_NO_AIM,
    U_NO_GATE,
    U_OFF,
    U_OPEN,
    U_PLANNER,
    U_SETTLED,
    U_UNSHAPED,
    WORKER,
    Cycle,
    Finish,
    Launch,
    Loop,
    approve,
    cancel,
    describe,
    drive,
    finalize,
    replan,
    start,
    state,
    work,
)
from agent2.core.ultracode.plan import (
    APPROVAL_ID,
    APPROVAL_TITLE,
    CHECK_ID,
    CHECK_TITLE,
    DEF_SOURCE,
    EVENT,
    KNOB,
    LABEL,
    SURFACE,
    Brief,
    approval_needed,
    brief,
    graph_for,
    max_nodes,
)
from agent2.core.ultracode.stages import (
    DERIVED,
    GATE_KINDS,
    KIND_LABEL,
    KIND_STAGE,
    KINDS,
    K_APPROVAL,
    K_BUILD,
    K_CHECK,
    K_FIX,
    S_ANALYZE,
    S_DISCOVER,
    S_EXECUTE,
    S_FINALIZE,
    S_FINISHED,
    S_INSPECT,
    S_OBSERVE,
    S_PLAN,
    S_REPLAN,
    S_UNDERSTAND,
    S_VERIFY,
    STAGE_LABEL,
    STAGES,
    TERMINAL_STAGES,
    TRANSIENT,
    VERIFY_KINDS,
    WORK_KINDS,
    awaiting_approval,
    rank,
    stage_of,
)

__all__ = [
    "APPROVAL_ID",
    "APPROVAL_TITLE",
    "CHECK_ID",
    "CHECK_TITLE",
    "DEF_SOURCE",
    "DERIVED",
    "EVENT",
    "GATE_KINDS",
    "KINDS",
    "KIND_LABEL",
    "KIND_STAGE",
    "KNOB",
    "K_APPROVAL",
    "K_BUILD",
    "K_CHECK",
    "K_FIX",
    "LABEL",
    "REFUSALS",
    "STAGES",
    "STAGE_LABEL",
    "SURFACE",
    "S_ANALYZE",
    "S_DISCOVER",
    "S_EXECUTE",
    "S_FINALIZE",
    "S_FINISHED",
    "S_INSPECT",
    "S_OBSERVE",
    "S_PLAN",
    "S_REPLAN",
    "S_UNDERSTAND",
    "S_VERIFY",
    "TERMINAL_STAGES",
    "TRANSIENT",
    "U_BUDGET",
    "U_CYCLES",
    "U_ENGINE",
    "U_GATE",
    "U_LIVE",
    "U_MISSING",
    "U_NO_AIM",
    "U_NO_GATE",
    "U_OFF",
    "U_OPEN",
    "U_PLANNER",
    "U_SETTLED",
    "U_UNSHAPED",
    "VERIFY_KINDS",
    "WORKER",
    "WORK_KINDS",
    "Brief",
    "Cycle",
    "Finish",
    "Launch",
    "Loop",
    "approval_needed",
    "approve",
    "awaiting_approval",
    "brief",
    "cancel",
    "describe",
    "drive",
    "engine",
    "finalize",
    "graph_for",
    "max_nodes",
    "plan",
    "rank",
    "replan",
    "stage_of",
    "stages",
    "start",
    "state",
    "work",
]
