# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2.core.workflow.graph
──────────────────────────
THE workflow definition — and, since Phase D1, a **thin domain wrapper over the
generalized DAG core** rather than a second graph engine.

A workflow is a directed acyclic graph of nodes, and a node is an `agent_tasks`
row. That was Task 37's whole acceptance bar and it has not changed; what changed
is *where the graph lives*. The spec's final architectural rule is **ONE
GENERALIZED DAG**, and it permits exactly one shape of consumer:

> *"No `WorkflowDAG` / `UltraCodeDAG` / `SecurityDAG` / `DynamicWorkflowDAG`
> **unless these are merely thin domain-level wrappers around the same core DAG
> API**."*

This module is that wrapper. `agent2.core.dag` owns the model, the validator and
the store; this file owns **workflow vocabulary** and nothing else.

⚠️ **`WorkflowDef` IS `dag.Graph` — THE SAME CLASS UNDER ITS OWN NAME, NOT A
SUBCLASS AND NOT A COPY.** An alias is what makes the wrapper honest: a second
dataclass with the same six fields would be a second declaration of what a
declared graph *is*, and the two would drift the first time the core grew a field
(`kind`, `meta` and `Edge` all arrived after Task 37). Because it is an alias,
`isinstance(defn, dag.Graph)` is true of every workflow definition ever built,
so a workflow can be handed to the core's store, its mutation validator and every
Phase D2+ scheduler with no conversion step and no adapter to keep in sync.

⚠️ **THE ONLY THING THIS MODULE ADDS IS VOCABULARY, AND IT IS INJECTED, NEVER
BRANCHED ON.** `dag.validate()` takes `max_nodes`, the `knob` its message should
name and the `label` its prose should use, so a 4-node workflow is still refused
with *"…the ceiling is 3 (AGENT2_WORKFLOW_MAX_NODES)"* and an unknown dependency
still reads *"…is not a node of this workflow"* — byte-identical to the sentences
that shipped in Task 37 — while the engine contains no `if workflow:`. That
injection is the entire reason the core can stay feature-agnostic and still
produce messages a workflow author recognises.

⚠️ **`config.WORKFLOW_MAX_NODES` IS READ AT CALL TIME, NEVER BOUND AT IMPORT.**
`validate()` and `describe()` both look it up per call. A module-level
`MAX_NODES = config.WORKFLOW_MAX_NODES` would freeze whatever the value was when
the first surface imported this file, so an operator's `AGENT2_WORKFLOW_MAX_NODES`
would apply in one process and not another — and it would silently break the two
tests that `monkeypatch.setattr(cfg, "WORKFLOW_MAX_NODES", …)` to prove the
ceiling refuses rather than truncates.

⚠️ **EVERY NAME THIS MODULE EVER EXPORTED IS STILL EXPORTED.** *"Preserve
backward compatibility wherever practical."* `loader.py`, `authoring.py`,
`runner.py` and `workflow/__init__.py` reach for `graph.<name>` in nineteen
places, and three test files reach for fourteen more; a wrapper that quietly
dropped `MAX_TEXT` or `NAME_RE` would be a refactor that broke the callers it was
supposed to protect. The re-exports below are assignments rather than
`from … import *` so that every one of them is visible, greppable and attributable
to the module that owns it.

Where the invariants moved to — **read these before changing behaviour**:

| Fact | Now owned by |
|---|---|
| what a graph, a node and an edge ARE | `core/dag/model.py` |
| a cycle is the one error that produces no error | `core/dag/validate.py` |
| an unknown dependency is fatal here and ignored by `tasks.ready()` | `core/dag/validate.py` |
| `_find_cycles` is iterative, never recursive | `core/dag/validate.py` (`find_cycles`) |
| levels order within a level by `(priority, seq)`, as `tasks.ready()` does | `core/dag/validate.py` (`levels_for`) |
| a ceiling refuses, it never truncates | `core/dag/validate.py` |

Everything here remains **total**: `validate()` returns a report and never raises,
and nothing in this module touches the database, the filesystem or the clock.
"""

from agent2 import config
from agent2.core.dag import model as _m
from agent2.core.dag.validate import find_cycles as _find_cycles
from agent2.core.dag.validate import levels_for as _levels_for
from agent2.core.dag.validate import validate as _validate
from agent2.core.dag.validate import validate_mutation as _validate_mutation

# ── The vocabulary this wrapper injects ───────────────────────────────────────
#: The noun the validator's prose uses, and the environment variable its ceiling
#: message names. These two strings ARE the domain half of this module — the only
#: workflow-specific facts the shared engine is ever told.
LABEL = "workflow"
KNOB = "AGENT2_WORKFLOW_MAX_NODES"

# ── Re-exports: the model ─────────────────────────────────────────────────────
# ⚠️ Assignments, not `import *`. A star import would make "what does this module
# guarantee to its callers" a question you answer by reading another file, and the
# nineteen in-package call sites plus fourteen test call sites are the contract.
SCHEMA_VERSION = _m.SCHEMA_VERSION
MIN_SCHEMA = _m.MIN_SCHEMA
NODE_ID_RE = _m.NODE_ID_RE
NAME_RE = _m.NAME_RE
MAX_TITLE = _m.MAX_TITLE
MAX_TEXT = _m.MAX_TEXT

#: ⚠️ The alias, and the whole point of the wrapper. `WorkflowDef is dag.Graph`.
WorkflowDef = _m.Graph
Node = _m.Node
Edge = _m.Edge
Validation = _m.Validation

fold_id = _m.fold_id
make_node = _m.make_node
problem = _m.problem

# ── Re-exports: the problem and warning vocabulary ────────────────────────────
# One declaration, in `dag.model`, so a surface that renders a refusal renders the
# same code whichever consumer produced it.
P_NO_NAME = _m.P_NO_NAME
P_BAD_NAME = _m.P_BAD_NAME
P_NO_NODES = _m.P_NO_NODES
P_TOO_MANY = _m.P_TOO_MANY
P_BAD_ID = _m.P_BAD_ID
P_DUPLICATE = _m.P_DUPLICATE
P_UNKNOWN_DEP = _m.P_UNKNOWN_DEP
P_SELF_DEP = _m.P_SELF_DEP
P_CYCLE = _m.P_CYCLE
P_SCHEMA = _m.P_SCHEMA
#: The three the core added for **runtime** mutation (`validate_mutation`). A
#: plain `validate()` never emits one, and they are named here anyway because
#: `PROBLEM_CODES` lists them: a surface enumerating that tuple to build a
#: renderer must be able to find a name for every member of it.
P_LOST_NODE = _m.P_LOST_NODE
P_REWIRED = _m.P_REWIRED
P_NO_GROWTH = _m.P_NO_GROWTH

PROBLEM_CODES: tuple[str, ...] = _m.PROBLEM_CODES

W_NO_TITLE = _m.W_NO_TITLE
W_DUP_DEP = _m.W_DUP_DEP
W_NO_INSTRUCTION = _m.W_NO_INSTRUCTION
W_NEW_SCHEMA = _m.W_NEW_SCHEMA

WARNING_CODES: tuple[str, ...] = _m.WARNING_CODES

# ── Dependency resolution and cycle detection ─────────────────────────────────
# ⚠️ Assignments, for the reason stated above: `graph.levels_for` **is**
# `dag.validate.levels_for`, the identical function object, so the graph's answer to
# "what may run now" and the scheduler's cannot diverge. `find_cycles` is public
# since Phase D1; the old private `_find_cycles` had no caller outside this file
# (only docstrings named it), so nothing needs the old spelling.
#
# ⚠️ They are imported BY NAME rather than as `from agent2.core.dag import
# validate as _v`, and that is a trap worth naming: `dag/__init__.py` re-exports a
# *function* called `validate`, which shadows the submodule of the same name on the
# package object — so the module-style import binds the function and every
# attribute access on it raises `AttributeError` at import time.
levels_for = _levels_for
find_cycles = _find_cycles


def make_def(raw, *, source: str = "inline", name: str = "") -> WorkflowDef:
    """A `WorkflowDef` from a plain dict. Total — never raises.

    A one-line delegation kept as a *function* rather than an alias so the
    workflow-facing name and signature survive in tracebacks, in `help()` and in
    the four in-package callers, while the coercion rules (bare strings, `steps:`
    / `depends_on:` spellings, `extra`) live once in `dag.model.make_graph`.
    """
    return _m.make_graph(raw, source=source, name=name)


def validate(defn: WorkflowDef) -> Validation:
    """Can this workflow run — and if not, exactly why. Never raises.

    Called **before any row is written** (`runner.instantiate()` refuses on a
    problem) because every failure this catches is durable once the tasks exist: a
    cycle deadlocks the plan forever, and an unknown dependency releases a node to
    run out of order with nothing on screen to say so. Both rules, and the reason
    `tasks.ready()` deliberately does *not* enforce the second, are stated in full
    in `core/dag/validate.py`'s docstring.

    ⚠️ The ceiling is read **live** from `config`, and the knob and label are
    passed in — that is what keeps one shared validator producing this surface's
    sentences without the engine knowing this surface exists.
    """
    return _validate(defn, max_nodes=config.WORKFLOW_MAX_NODES,
                     knob=KNOB, label=LABEL)


def validate_mutation(before: WorkflowDef, after: WorkflowDef, *,
                      settled=(), growth: int | None = None,
                      max_growth: int | None = None) -> Validation:
    """Whether *after* may replace *before* mid-run. The same three extra checks
    the core makes, in this surface's vocabulary.

    Exposed here so a Phase D4 caller holding a `WorkflowDef` never has to reach
    past the wrapper into the engine and re-supply `knob`/`label` itself — the one
    way a second spelling of the ceiling message could appear.
    """
    return _validate_mutation(before, after, settled=settled,
                              max_nodes=config.WORKFLOW_MAX_NODES,
                              knob=KNOB, label=LABEL,
                              growth=growth, max_growth=max_growth)


def describe() -> dict:
    """What this build accepts — for `/workflow` and `GET /api/workflows`.

    Derived rather than documented, for `skills.normalize.describe()`'s reason:
    "what shape does a workflow file take" is the first question when one does not
    load, and a README can go stale while a payload built from the tables cannot.

    ⚠️ `max_nodes` is read per call for `validate()`'s reason — a payload that
    reported a ceiling the validator does not apply would be worse than no payload.
    """
    return {
        "schema": SCHEMA_VERSION,
        "min_schema": MIN_SCHEMA,
        "max_nodes": config.WORKFLOW_MAX_NODES,
        "problems": list(PROBLEM_CODES),
        "warnings": list(WARNING_CODES),
        "node_id": NODE_ID_RE.pattern,
        "yaml": False,
    }
