# agent2.core — the centralized runtime

`agent2/core/` is the single source of truth for everything that would otherwise
be duplicated between the Web UI (`agent2web.py`) and the CLI (`agent2cli.py`):
the workspace sandbox, per-chat execution isolation, context assembly, command
execution, cancellation, authorization, secrets, structured logging, the shared
memory/rules/context backends — and the whole autonomy family: the one generalized
DAG, its scheduler, workflows, the goal-sentence planner, the UltraCode loop and the
verifier they all share. Both interfaces import these modules — no interface
re-implements this logic locally.

> **Why "core" and not "shared".** These are not utilities. Each module here owns
> a **decision** that must have exactly one answer, because a second answer drifts
> **silently**: each surface still looks right on its own, and neither can see the
> disagreement. Every `⚠️` below names the concrete bug that shape prevents; the
> full rationale is in the module's own docstring, and a named test pins it.

```
agent2/core/
├── workspace/     the WorkspaceManager singleton + the path sandbox
├── session/       per-(sid, chat_id) Session + Task registry
├── memory/        memories table CRUD + system-prompt block
├── rules/         rules table CRUD + system-prompt block
├── context/       project-scoped (cwd-keyed) chat lifecycle + project_key()
├── broker/        THE answer to "what does the model see this turn"
│   ├── __init__.py    ten sources, one ORDER, one guard per collector
│   ├── sources.py     priority · TTL · the four measures
│   ├── budget.py      what fits inside the model's token window
│   └── isolation.py   may this project see that row
├── skills/        which instruction files reach THIS prompt, and why the rest did not
│   ├── discovery.py   one bounded walk of .agent2/skills/, TTL-cached
│   ├── normalize.py   what a foreign vendor's header MEANS here (the only namer)
│   ├── select.py      five tiers; index IS rank
│   └── state.py       on/off/automatic — a row, never a file edit
├── dag/           THE one generalized graph engine — every consumer's, no feature name
│   ├── model.py       what a Graph, a Node and an Edge ARE
│   ├── validate.py    whether one can ever finish (a cycle produces no error)
│   ├── store.py       nine node states, two derived on every read
│   └── schedule.py    which READY nodes actually run NOW, and what held the rest
├── workflow/      the first DAG consumer — a plan, not a second engine
│   ├── graph.py       WorkflowDef IS dag.Graph (aliased, so nothing can drift)
│   ├── loader.py      .agent2/workflows/*.yaml — a declared stdlib parser subset
│   ├── runner.py      rows in, progress re-derived out
│   └── dynamic.py     a goal sentence becomes a graph (plan is the default)
├── ultracode/     the autonomous loop — the fourth consumer, two doors only
│   ├── stages.py      the eleven stage words, DERIVED from the rows
│   ├── plan.py        evidence in, graph out (names and counts, never prose)
│   └── engine.py      who drives a run, and every way it may refuse
├── verify.py      "Done" is not verification — five verdicts, from the ledgers
├── health.py      one assembly answers "is it working" — 14 sections
├── metrics.py     thirteen declared signals; three deliberately BORROWED
├── projectscan.py what this project IS, and what its own comments say about it
├── projectdoc.py  .agent2/agent2.md — and which half of it a human owns
├── pil/           the offline Personal Intelligence Layer
├── commands.py    execution STATE — the lifecycle and the watchdog verdict
├── procio.py      pipe I/O and the process-tree kill; records nothing
├── scheduler.py   bounded worker pool for agent TURNS (never for graph nodes)
├── tasks.py       persistent tasks + checkpoints — and every DAG node is one
├── recovery/      picking interrupted work back up; what a killed process left
├── execstate.py   whether a run is still alive (pid + heartbeat, one owner)
├── sync.py        THE synchronization layer (locks, bus, caches, poller)
├── diffs.py       diff computation + preview_window()
├── highlight.py   the one tokenizer (ships spans, never a second lexer)
├── progress.py    turn-progress events pushed to a surface
├── gitstate.py    THE git reader — total, timed out, TTL-cached
├── permissions.py THE capability model (routes · socket events · tools)
├── secrets.py     how a credential is persisted (references, never values)
└── logging.py     one structured, rotating audit trail
```

Subpackages are directories with an `__init__.py` — import the submodule, e.g.
`from agent2.core import workspace as ws`.

---

## workspace — the security boundary

`manager` (a `WorkspaceManager`) owns "the project root" for the whole process.
Nobody else stores a copy of that path.

- **Discovery** (`_discover_root`) resolves a launch dir to a project root in a
  fixed order: persisted config → git root → `agent.md` → `package.json` →
  `pyproject.toml` → `Cargo.toml` → `go.mod` → `composer.json` → `.workspace` →
  the launch dir itself. `_repair_root` guarantees the result is non-null,
  absolute, and an existing directory.
- **The sandbox** (`validate_path`) is the hard boundary. Every filesystem tool
  resolves its path through it *before* touching disk. Any path that escapes the
  root — `..`, an absolute path outside, a symlink/junction escape, a mounted
  drive, or a UNC/network path (`\\server`, `//server`) — is rejected with the
  exact string:

  > `Operation blocked: Path is outside current workspace.`

  (`BLOCKED_MSG`, raised as `WorkspaceViolation` whose `str()` is that message.)
  ⚠️ Relative paths bind to the **workspace root**, not the process cwd, so a cwd
  change cannot be used to trick a tool.
- **Switching** (`set_workspace`) persists the choice to the `settings` table
  under `active_workspace` (CLI ⇄ Web parity), no-ops if the root is unchanged,
  and fires `on_switch` callbacks. The session manager registers one that cancels
  every running task.

Free functions mirror the other core modules: `current()`, `root()`,
`validate_path()`, `set_workspace()`.

⚠️ **A project directory used as a lookup key goes through
`core.context.project_key()`, never a raw `workspace.root()`.** A raw path is not
normcased, so a row written from `C:\…` is never found again by a read for
`c:\…` — the feature simply stops working, with nothing in any log. Migration 10
exists to repair rows written before that rule.

---

## session — per-chat execution isolation

The bug this fixes: cancellation and the live TODO list used to be keyed by the
Socket.IO `sid` alone, so running Chat B on the same socket inherited Chat A's
cancel token and checklist. Now every runtime concern is owned by a `Session`
keyed by **(sid, chat_id)**:

- `cancel` — a `threading.Event`; a **fresh token is minted on every `open()`**, so
  a new turn never starts already-cancelled and never inherits a prior stop.
- `task` — a `Task` record (id, status, progress, logs, timings).
- `todos` — the live TODO list (was a `tools.py` global).

`sessions.cancel(sid)` with no `chat_id` cancels *all* chats on that socket — the
Stop button only carries a sid; with a `chat_id` it cancels just that chat.
`owns_stream(sid, chat_id, task_id)` drops any streamed token whose task is not
the session's current one, so a stale or foreign turn cannot write into the wrong
chat. `cleanup_sid` cancels and drops everything on disconnect.

---

## broker — what the model sees this turn

`core/broker/` sits **above** the existing context machinery and rewrites none of
it. Memories come from `core.memory`, rules from `core.rules`, the plan from
`core.tasks`, the repository from `core.gitstate`, MCP verdicts from
`integrations.registry.health()`, changed files from `core.diffs.store`. The
broker owns *composition* only, so it holds no fact that could drift out of step
with the module that owns it.

**`ORDER`** — `conversation` · `project` · `git_state` · `skills` ·
`task_results` · `workflow_state` · `mcp_results` · `files` · `memory` · `rules`.
`ALWAYS = frozenset((memory, rules))`, rendered last so the prompt ends on the
user's own standing orders.

| Rule | The bug it prevents |
|---|---|
| ⚠️ **One guard per collector**, never one around the loop | A single outer `try` would let a missing `git` binary cost the user their memories and rules — the one thing this module exists to prevent. A failure lands on `bundle.errors` and logs as `context_source_failed` |
| ⚠️ The bundle is a **parameter** of `system_prompt()` | The prompt is built once per turn and read on every one of up to `MAX_AGENT_ITERS` iterations. A `system_prompt` that assembled its own would pay the file reads, the `git` calls and the task query *per iteration* — and could hand the model a different prompt halfway through one turn |
| ⚠️ The conversation is an **input**, not a source this module reads | `agent.build_context()` remains THE reader of `messages`. A second query here would be a second declaration of what history means, and the two would disagree about the tie-break |
| ⚠️ `ORDER` is where a source **prints**; `PRIORITY` is what **survives** | They deliberately disagree. Deriving one from the other forces a choice between a prompt that ends on git statistics and a budget that discards the user's rules |
| ⚠️ Measures are assigned in **one** place (`measure()`) | Ten collectors are ten ways to count, and a source registered by a later phase would arrive unmeasured and rank last by accident. `None` means *not stated*, never zero — a zero-relevance item is the first thing a budget discards |
| ⚠️ The `memories`/`rules` caches were **moved** here, not copied | `agent._MEM_CACHE` and `_RULES_CACHE` are **aliases**. `notify()` invalidates the object you hold, so a second `VersionedCache` for the same resource keeps serving a deleted memory — silently, and only in one of the two agent loops |

`base_tail()` is why `system_prompt()` still costs **zero queries** on a warm
cache: situational sources are assembled once by the caller, while a bare
`system_prompt()` resolves to the same two cached blocks.

`skills` and `workflow_state` are declared with **empty collectors on purpose** — a
source that does not exist yet still has a name, an order and a slot in the
report, so a later phase adds one by *registering* rather than by editing this
module.

**`budget.py`** decides what fits: the window comes from `AGENT2_CONTEXT_BUDGET`,
then the model's own `context_window` capability, then `ASSUMED_WINDOW = 32 000`;
`RESERVE_TOKENS = 6 000` is held back for the reply; `MIN_LIMIT = 512` applies to
every basis **except** an explicit env budget, because a number an operator typed
on purpose is not second-guessed. The reported `basis` (`env` · `model` ·
`assumed` · `caller`) says *why* that limit was used. ⚠️ Token counting delegates
to `llm.router.estimate_tokens()` — the broker had a second `len // 4` for a day,
and the trim then fit a budget measured one way while the model was chosen against
another, each half self-consistent.

**`isolation.py`** is the one declaration of "may this project see that row".
Governed tables carry a `project` column (migrations 17 and 18): a read is *mine
OR shared* (`scope_sql()`), a write is stamped (`stamp()`), and
`stamp(shared=True)` is what explicit sharing means in code. Mode comes from
`AGENT2_CONTEXT_ISOLATION` (`project` default, `off` for one global store), and an
unrecognised value falls back to `project` — never `off`.

⚠️ **`''` is the column default, and that is load-bearing.** Every row that
existed before isolation reads as shared and stays visible in every project,
exactly as it was. An upgrade that had defaulted to "belongs to whichever project
happened to be open during the migration" would have made a user's entire memory
store vanish from every other checkout, with no error and nothing to point at. The
same default protects any writer that reaches the table without going through
`core.memory` or `core.rules`: an unstamped row is visible, never invisible.

---

## memory / rules / context — shared DB backends

Thin, failure-swallowing CRUD over `agent2.db`, imported identically by both
interfaces:

- **memory** — `list/add/delete_memory` + `memory_prompt_block()`.
- **rules** — `list/add/toggle/delete_rule` + `rules_prompt_block()`
  (`active_only=True` for injection), plus `set_rules_active` — **two uniform bulk
  writes, never N toggles**.
- **context** — chats are tagged with their launch `cwd`; history for a session
  only returns chats for that cwd (`list_chats_for_cwd`). Cross-project access is
  intentional only (`list_all_chats`, used by the `/resume` picker).
  `project_key()` lives here: the canonical, normcased form of a directory.

⚠️ **Never a raw `INSERT` into `memories`.** It skips the dedup *and* the
`sync.notify()` that invalidates the prompt, so the model keeps reading the old
block with nothing to indicate it.

---

## commands / procio — execution state and pipe I/O

Three registries that look redundant and are not: `cli/state.py` and
`terminal._procs` hold process **handles**, `core/commands.py` holds execution
**state** (`command_id`, pid, `task_id`, `started_at`, `last_output_at`, timeout,
status, exit code, stdout, stderr), and `core/procio.py` owns pipe **I/O** and the
tree kill while recording nothing.

Lifecycle: `CREATED → STARTING → RUNNING → STREAMING → COMPLETED`, with `FAILED`,
`TIMEOUT`, `CANCELLED` and `KILLED` as the failure set.

⚠️ **Whichever verdict lands first wins** — `_mutate` refuses to move a settled
execution — so every runner **records, then kills**. `terminate_tree` blocks, and
the moment the tree dies the pipes close and the drain would otherwise settle the
row with the *shell's* exit code: the transcript would read `FAILED / exit 1`, and
that fabricated failure is what reaches the model.

`procio.drain` reads both pipes concurrently (one thread per pipe, one queue) —
reading them in sequence deadlocks the moment one fills — and yields a
`("tick","")` even in total silence. The watchdog rides those ticks, so there is
no timer thread and no second opinion about whether the process is alive.

⚠️ **`watch()` returns a word and never acts.** The runner acts, because the
runner owns the handle. Both kill ceilings default **off** and only the stuck
*report* is on; a `[R]etry` is only ever a keypress. `rc 124` = timed out
(`timeout(1)`'s convention), `rc 130` = killed or cancelled.

---

## scheduler / tasks / recovery

- **scheduler** — a bounded worker pool for agent turns. `submit()` never blocks
  and returns `queued` / `disabled` / `rejected`, each with a required caller
  response, all three handled. ⚠️ Turning the pool off can never be why a message
  goes unanswered: with it disabled, a turn runs on its own thread.
- **tasks** — persistent tasks and checkpoints, surfaced at `GET /api/tasks`.
  ⚠️ **Every DAG node is one of these rows**, which is why a workflow node gets the
  checkpoints, the heartbeat, `/recovery` and the crash scan for free.
- **recovery** — picks interrupted work back up and ⚠️ **never re-runs completed
  work**; it prepends a briefing rather than replaying tool calls. `classify` answers
  two different questions — `safe_to_repeat()` for a repeat, `safe_to_continue()` for
  a scheduler — and ⚠️ asking the wrong one is how a bounded retry becomes
  structurally unable to fire even once.

⚠️ `stop_agent` and disconnect call **both** `scheduler.cancel()` and
`sessions.cancel()` — different halves of one guarantee.

⚠️ **`scheduler.py` bounds TURNS and must never bound graph nodes.** That queue
outlives any single run; `dag/schedule.py` bounds the nodes of one run and dies with
it. Two pools, because one would make a full turn backlog stall a graph that was
already mid-flight.

---

## dag — ONE generalized graph engine

The architectural rule the whole autonomy family rests on: **one DAG, one scheduler,
one task system, one recovery system, one verifier.** Workflow, Dynamic Workflow and
UltraCode are **consumers, never engines**.

⚠️ **Feature-agnosticism is a test here, not a promise.** Structural `ast` tests
assert that no `if workflow:` / `if ultracode:` and **no feature name at all** appears
in this package. The domain half is **injected** — `validate(…, knob=…, label=…)` — so
a 4-node workflow is refused with the workflow feature's own sentence, word for word,
by an engine that has never heard of workflows.

⚠️ **Nine states, two of them derived.** Six read straight off `agent_tasks.status`;
**READY and BLOCKED are computed on every read**. Storing readiness would let a crash
leave a node claiming READY behind an upstream that never finished — and deriving is
also cheaper: one `qone` + one `qall` for a graph of any size.

⚠️ **No new table and no migration.** A graph is one `exec_workflows` row plus N
`agent_tasks` rows, node id on `CP_NODE` and run id on `CP_WORKFLOW`.

⚠️ **A cycle is the one error that produces no error** — `tasks.ready()` releases a
node when its dependencies are *settled*, so a ring of three simply never becomes
ready: `ready()` returns `[]` forever, nothing raises and nothing is logged.
`find_cycles()` is iterative, because a 500-node chain must be **reported**, never
recursed into a `RecursionError`.

⚠️ **`schedule.py` never blindly runs every READY node, and every node it declines
carries a reason** (`HOLD_CODES`, with `taken | held == offered` asserted). It derives
no readiness of its own — `tasks.ready()` is still the only predicate — and the
corollary is the surprise: `ready()` releases a node whose upstream **failed**, so
declining it and marking it SKIPPED is the scheduler's job or the run holds itself open
forever. In-flight is **derived from the rows**, never counted in memory, because dual
mode is two processes over one `agent2.db` and a counter silently doubles the ceiling.

---

## workflow — the first consumer

`graph.py` says what a workflow is (⚠️ `WorkflowDef` **is** `dag.Graph`, aliased — so
there is no adapter to keep in sync), `loader.py` reads one off disk, `runner.py` turns
one into rows and reads it back, `dynamic.py` turns a **goal sentence** into one.

⚠️ **Progress is derived from the rows, never read out of the run.**
`exec_workflows.state` is a breadcrumb that `execstate.workflow_step()` *replaces* on
every node transition, so trusting it makes a killed run report the progress it had at
the moment it died.

⚠️ **A turn gets the current node, not the plan.** `for_turn()` inlines exactly one
instruction and *names* the rest — a worker gets only what it needs — and it is **flat,
not merely cheap**: a 24-node graph costs the same reads as a 3-node one.

⚠️ **PyYAML is not a dependency, so `loader.SUBSET` declares what this parser reads and
nothing outside it is guessed at.** An older schema still runs (`UPGRADES` is a ladder,
and each step must *advance* the version or the walk would hang). ⚠️ The **filename is
the name**; a disagreeing `name:` line is reported and loses. ⚠️ A **truncated
declaration is refused** — the tail of a YAML file is where the last node's `needs:`
lives.

⚠️ **In `dynamic.py`, `plan` is the default and writes nothing at all** — no
`exec_workflows` row, no `agent_tasks` row. `auto` must be asked for by name, and an
*unrecognised* mode word is refused rather than coerced. Its only structural opinion is
`_acyclic()`: every `needs` must point **backwards in declaration order**, which is what
licenses a too-long plan to be clipped instead of refused.

---

## ultracode — the autonomous loop

UNDERSTAND → INSPECT → DISCOVER SKILLS → PLAN → EXECUTE → OBSERVE → ANALYZE → VERIFY →
(pass ⇒ continue | fail ⇒ RE-PLAN → EXECUTE). The **fourth** DAG consumer, and no new
table.

⚠️ **The one new fact is the stage, and it is derived from the rows and never stored** —
`execstate.workflow_step()` replaces `exec_workflows.state` on every node transition, so
a stage written there would be gone by the next node. Deriving is also why a run killed
mid-flight reports the stage that is true **now**, with no stale copy to reconcile.

⚠️ **Verification is a node**, because a check outside the graph could not hold work
back. ⚠️ **Approval is a node too** — created and immediately PAUSED with the roots
depending on it, and `tasks.pause()` writes **no** stop checkpoint, which is the only
thing separating *a human chose to hold this* from *a crash abandoned it*.

⚠️ **One clock, checked between cycles.** The budget is never handed to
`schedule.run()`: the pump owns the node timeout, and two clocks over one node is two
answers — so an overrun is *reported* at a boundary rather than killing a worker
mid-write.

⚠️ **Entry is `/ultracode` and `POST /api/ultracode` only, asserted structurally.** This
loop writes code, so *explicit, never automatic* is enforced by the importer list rather
than promised.

---

## verify — "Done" is not verification

One question for anything that runs: *something said it finished — does the durable
record agree?* ⚠️ It exists because `agent_tasks.status` is **written by the thing being
judged**, so a node marked COMPLETED whose only write failed is indistinguishable from
one that genuinely worked.

⚠️ **It verifies a task row, which is why it knows nothing about workflows** — it
imports neither `core.dag` nor `core.workflow`, and that is exactly what stops the
forbidden `if workflow:` from ever being needed.

Five verdicts, and the middle three are the point: `open` · `unsuccessful` ·
`confirmed` · `contradicted` · `unconfirmed`. ⚠️ `verified` and `ok` are **two
questions and stay two** — a run of pure reasoning nodes is legitimately unverifiable
and must still be allowed to finish. ⚠️ **Read-only, and it does not re-stat disk**: a
file a later step legitimately replaced would read as a contradiction. ⚠️ **Two queries
per report at any node count.** ⚠️ There is deliberately **no off switch** —
verification off does not make Agent-2 quieter, it makes it *credulous*.

---

## health / metrics — the two read surfaces

- **health** — one assembly answers *is it working*, read by `/health` **and**
  `GET /api/health`. Fourteen sections, each a **projection** of the reader that already
  owns the fact. ⚠️ `ok` and the `503` are decided by `problems` **alone** — every
  supported configuration that trips the 503 spends the meaning of the 503. ⚠️ A row may
  never say `fail` while the report says `ok`, so `_rows()` derives its states from the
  lines the aggregate was built from rather than re-testing the data. ⚠️ **`off` is not a
  lesser `warn`**: the checkpointer, the scheduler and both MCP bridges can be off on
  purpose, and a cross printed at a deliberate choice is how an alert stops being read.
- **metrics** — thirteen declared signals at the existing single-writer chokepoints, so
  there is one stopwatch per fact. ⚠️ **Three are borrowed, not measured** — LLM latency
  and errors belong to `llm.router`, denials to `permissions` — and recording into one is
  a counted **no-op**, which is the guard that stops the second drifting copy appearing.
  ⚠️ **Cardinality is capped per signal**, because labels are created on first
  *observation* and admitting model-supplied text lets junk names fold the real tools
  into `~other` for the life of the process — a measurement destroyed by what it
  measures. ⚠️ No content, ever: a series holds numbers and one enum-ish label.

---

## skills — instruction files, selected per request

`.agent2/skills/` — prose a human (or another agent's toolchain) left in the project.
`discovery` walks, `normalize` translates, `select` ranks, `state` remembers.

⚠️ **Not every skill in every prompt** — that is the acceptance bar and the reason
`select.py` exists: ten discovered skills and an unrelated message put **nothing** in
the prompt. Five tiers, declared as data where **index IS rank**: request → project doc
→ enabled → auto-relevant → general. ⚠️ `priority` orders *within* a tier and may never
promote past one.

⚠️ **No write API reaches this package** — asserted structurally, because a skill is
frequently somebody else's file in somebody else's repo. So enablement is a **row**, not
a frontmatter edit. ⚠️ **Three states, and the third is the point**: never-chosen still
allows automatic selection, and *off* beats every signal including the request naming it.

⚠️ **The vendor is a label, never a dispatch** — no module outside `normalize.py` may
name one, which is the difference between a normalization layer and a per-vendor plugin.

---

## projectscan / projectdoc — what this project IS

`/init` walks the workspace and writes `.agent2/agent2.md`, which every later prompt
reads back **as fact**.

⚠️ **A partial answer may never read as a complete one** — three ceilings, and
`truncated_by` says which engaged. A scan that quietly stopped at 20 000 files and wrote
"no tests were found" into a *committed* file is the failure this is shaped around.
⚠️ **A runner is proved, never guessed.**

⚠️ **`_leading_comment()` is a syntax gate, not a heuristic** — comment syntax in,
comment text out, `""` for an undeclared extension. These notes are the one part of the
scan that reaches a model, so the boundary cannot be a promise.

⚠️ **Ownership is the `<!-- agent2:generated -->` marker on a section's first body line
— nothing else.** Delete it and the section is yours forever. ⚠️ An identical rewrite is
**not a write**. ⚠️ And the doc is bigger than the prompt slot, so `for_prompt()` decides
**which half** a turn gets: the clip it replaced was head-first, and a document ends with
the sections a human took over — so the first thing the prompt discarded was the user's
own standing orders.

---

## sync — the synchronization layer

`RWLock`, `KeyedLock`, `AtomicCounter`, `EventBus`, `ChangeTracker`, `SyncPoller`,
`VersionedCache`. API: `notify(resource)` / `subscribe(resource, fn)`.

In-process, `notify()` publishes **synchronously** so caches invalidate
immediately, and subscriber exceptions are swallowed **and counted** — a broken
listener never breaks a turn, but it is not invisible either (watch
`listener_errors` at `GET /api/sync`). Cross-process, it also bumps `sync_state`;
`SyncPoller` republishes what *another* process changed as `remote: True`,
filtering self-bumps.

Resources: `memories`, `rules`, `api_keys`, `providers`, `settings`, `workspace`,
`chats`, `pil`, `tasks`, `mcp`.

⚠️ The poller only republishes resources it iterates, so a topic missing from
`RESOURCES` reaches the other surface **never**.

⚠️ **Every surface that outlives a single command must call `poller.start()`
itself.** `notify()` publishes in-process only; the poller is the one thing that
turns another process's bump into a local event. A REPL without one subscribes to a
bus nobody posts to: a `/mcp zap config` in the web half never invalidates the
CLI's cache, and the CLI keeps dialling the old endpoint until it restarts. It
shipped that way once, and the CLI was the surface that forgot.

---

## diffs / highlight / progress — presentation data

`diffs.py` computes; `cli/diffview.py` prints inline and the browser renders
`chat_file_diff`.

⚠️ **The diff is computed *before* the write**, and presentation data may never
raise into a turn. Display only — there is no approval gate anywhere in this
pipeline.

Colour is the whole contract: green added, red removed, grey context, and **yellow
is the paired count** `~min(added, removed)` plus the `@@` markers. ⚠️ Never a
fifth `"mod"` row tag: `hunks_of()` rebuilds each hunk's two sides from exactly
these four names and **returns `None` — refuse — on any other**, and `revert_text()`
is what `diffview.revert_change` writes to disk, so a `mod` tag would leave the
viewer unable to undo a modified line at all (and `to_patch` with no sigil to emit
for it).

- `summarize()` is the one thing that says what a set of changes **totals** to. A
  local `sum()` in a renderer makes the CLI and the browser disagree about one turn.
- `preview_window()` is the one window function. Re-deriving it in a renderer is
  how two surfaces show different slices of the same change.
- `hunks_of()` / `revert_text()` are the one **undo** computation. ⚠️ An undo
  reverse-applies the hunks to the file **on disk**; it never rebuilds the file out
  of the diff rows. `ch.lines` is an `n=3` window, so `del`+`ctx` is the whole
  pre-change file only when the file happened to fit inside its own hunks — that
  rebuild shipped in `diffview.revert_change`, and on a 200-line file with one
  changed line it wrote 7 lines and reported success. `ch.truncated` cannot catch it:
  that flag means "past `MAX_DIFF_LINES` **rendered** rows", not "elided". Every hunk
  is verified against the file in hand first and one mismatch refuses the whole undo,
  so a file that moved on is never half-rewritten.
- `DiffStore.mark()` → `since(mark)` is how "this turn" is identified. ⚠️ A saved
  `len(store.all())` breaks the moment the cap engages, because `add()` trims from
  the **front** — a length is then not an index, and the recap confidently reports
  the wrong turn.
- `dropped` (the cap) is deliberately **not** folded into `clear()`: a user-driven
  reset would then be reported as silent data loss.

`highlight.py` is the one tokenizer — surfaces receive `spans`, never a second
lexer in JS. `progress.py` pushes turn-progress events (`agent_stage`, `activity`,
`task_queue`, `chat_file_summary`) rather than letting a surface poll.

---

## gitstate — the git reader

Three subprocesses per snapshot (`rev-parse --show-toplevel`,
`status --porcelain=v1 -b`, `log -3`), TTL-cached per directory for `GIT_TTL`
(15 s), each with `GIT_TIMEOUT`. API: `empty()`, `branch_now()`, `is_repo()`,
`snapshot()`, `invalidate()`, `describe()`.

⚠️ **Every function is total.** A missing binary, a non-repo directory, a timeout
and a non-zero exit all mean the same thing to every caller — "no git
information" — so `snapshot()` returns an empty dict and `describe()` returns `""`
rather than raising into a render path or a turn. A hung `git` must not hang a turn.

⚠️ **One reader.** `cli/statusbar` used to own its own `subprocess.run(["git", …])`,
so the bar and the prompt could disagree about the branch, and only one of them had
a timeout. `branch_now()` is the deliberately cheap single-call form the bar uses
off the render path.

⚠️ **Read-only by design.** Writing is `run_command`'s job, so the permission gate
and the diff viewer can both see it happen.

⚠️ A workspace switch must invalidate **both** this cache and the status bar's own
serve-stale entry — dropping only one is how a switch shows two different branches.

---

## permissions — the capability model

This lives in `core/` because the sensitive operations are spread across three
surfaces: routes (`capability_for`), socket events (`capability_for_event`) and
agent **tools** (`capability_for_tool`).

Roles: `owner` (default, everything) · `operator` (may drive the agent, may not
touch credentials or MCP endpoints) · `viewer` (read). Two knobs, scoped
differently on purpose: `AGENT2_WEB_ROLE` describes *web clients*,
`AGENT2_DENY_CAPS` subtracts from *this whole process* including the CLI — one knob
could not express "let me work locally, let the browser only watch".

⚠️ An unknown role falls back to `viewer`, and an unmapped mutating `/api/` route
falls to `destructive`. Both fallbacks point at the strict end, so a typo or a new
endpoint is refused rather than granted.

⚠️ The tool gate lives in `tools.dispatch_tool` and returns a tool **`error`**,
never an exception — the model reads errors and adapts. `run_command` is the one
tool that skips `dispatch_tool`, so `terminal.stream_command` and
`cli/runtime.run_cmd_stream` carry their own exec gate; the CLI's sits *outside*
the retry loop, because a refusal cannot change on retry.

---

## secrets — how a credential is persisted

Credentials are stored as `a2s:` **references**; three columns hold them
(`api_keys.api_key`, `providers.api_key`, `mcp_config.security_key`). Backends, in
order: OS keyring (only if a probe round-trip actually **succeeds** —
importability is not availability) → AES-256-GCM, or a stdlib BLAKE2b+HMAC
construction → announced plaintext.

⚠️ Two rules outrank encrypting anything. `resolve()` passes a **non-ref through
unchanged**, so every legacy row and every half-finished migration keeps working;
and `seal()` **reads back what it wrote** and returns the plaintext if the round
trip disagrees, so a caller cannot persist a dangling reference.

⚠️ There is **no in-memory-key fallback** — that would write ciphertext the next
process cannot decrypt, turning a security feature into silent data loss.

⚠️ The master key defaults to `~/.agent2/secret.key`, **outside** the DB
directory, because a key beside `agent2.db` travels with every copy of it.

⚠️ Masking is a **constant** `••••••••` whose length is not derived from the
secret. The old `k[:6]…k[-4:]` form printed most of a ten-character ZAP key, and
would print six characters of ciphertext once rows hold references.

Honest scope: this defends against exposure of the database *alone*, and
`describe()` says so in its own payload.

---

## pil — the Personal Intelligence Layer

Fully **offline**: `memory.py` (the four `pil_*` tables and the per-kind revision
counters), `learning.py` (the shared passive engine — accept **+0.08**, ignore
**−0.03**, accept-then-delete **−0.20**), `prediction.py` (a trie, an n-gram table
and phrase prefixes; **never** calls an LLM), `grammar.py` and `improve.py` (both
opt-in and off by default), `optimize.py` (idle-only), `pretraining.py`
(cold-start through the real learning engine, once).

Every function swallows errors and returns a safe default: **PIL failure degrades
to doing nothing.** ⚠️ `optimize.py` uses raw SQL, so it must call `_signal()`
explicitly; and `prefs` is deliberately **not** a mirrored index kind.

---

## logging — one audit trail

`agent2.core.logging` (`alog`) is the only audit surface. Every subsystem emits a
structured `kind key=value` line to a rotating `agent2.log` next to the DB, plus
stderr for warnings and above. It never raises; a logging failure cannot break the
agent.

⚠️ **The helper you call and the string the log holds are not spelled the same.**
The named helpers emit **31** dotted kinds between them, and several diverge by more
than punctuation — grep for the right-hand column, never the function name. A
**selection**, chosen because these are the ones whose spelling surprises people:

| Helper | Emitted kind |
|---|---|
| `workspace_change` | `workspace.change` |
| `workspace_validated` | `workspace.validate` |
| `path_rejected` | `path.rejected` |
| `tool_exec` · `tool_failure` | `tool.exec` · `tool.failure` |
| `registry_loaded` · `registry_reject` | `registry.loaded` · `registry.reject` |
| `session_open` · `session_close` · `session_cancel` | `session.open` · `session.close` · `session.cancel` |
| `stream_owner` · `stream_dropped` | `stream.owner` · `stream.dropped` |
| `context_source_failed` | `context.source` |
| `context_trimmed` | `context.trimmed` |

The rest of the 31 are the recovery, verification, project and skills families —
`recovery.scan`, `recovery.scan.done`, `recovery.interrupted`, `recovery.start`,
`recovery.verified`, `recovery.resumed`, `recovery.retried`, `recovery.completed`,
`recovery.failed`, `recovery.review`, `verify.reported`, `verify.contradicted`,
`exec.interrupted`, `exec.persist`, `project.scan`, `project.doc`, `skills.applied`.

A generic `event()` / `exception()` pair covers anything else; production code uses
it with **four** further dotted names (`agent.model_fallback`,
`agent.parallel_calls_deferred`, `agent.part_skipped`, `scheduler.error`), for
**35** distinct kind strings in total. ⚠️ Count them by walking the helpers' own
`event("…")` arguments plus a grep for direct `alog.event("…")` calls in production
code — a bare grep over the whole tree also picks up a `'test'` string from the
suite, which is how a 36 that looks derived gets quoted.

⚠️ Never point `server/weblog` at the `agent2` namespace. Replacing that logger's
handlers silently kills the audit file.

---

## tools integration

`tools.py` wires the sandbox and registry into the agent loop:

- `_safe_path()` routes every filesystem tool through `workspace.validate_path`
  and returns the exact `BLOCKED_MSG` on escape.
- `ToolRegistry` maps name → impl with duplicate/invalid rejection and discovery;
  an unknown tool returns `Tool "<name>" is not registered.` instead of crashing.
- `ToolContext` threads per-chat session state (the TODO list, ids) into tools that
  need it (`update_todo`), so concurrent chats keep independent checklists.

---

## Surfaces

- **REST** (`server/routes.py`): `GET/POST /api/workspace`, `GET /api/tasks`,
  `GET /api/commands`, `GET/POST /api/sync`, `GET /api/recovery`, and the workspace
  dict inside `/api/platform`.
- **CLI** (`agent2cli.py`): `/workspace [path]` and its `/cd` alias show or switch
  the active workspace; switching persists and cancels running tasks.
- **No auto-restore**: a switch is always explicit. The sandbox root never silently
  follows the process cwd — it changes only via `set_workspace`.

---

## Tests

| Module | Test |
|---|---|
| workspace · session · memory · rules · context · registry | `test_core.py` |
| broker · sources · budget · isolation · gitstate | `test_broker.py` |
| commands · watchdog | `test_commands.py`, `test_cmd_timeout.py` |
| procio | `test_procio.py` |
| scheduler | `test_scheduler.py` |
| tasks · recovery | `test_tasks.py`, `test_recovery.py` |
| execstate · crash recovery | `test_execstate.py`, `test_crashrecovery.py` |
| sync | `test_sync.py` |
| diffs | `test_diffs.py` |
| permissions | `test_authz.py` |
| secrets | `test_secrets.py` |
| skills (discovery · normalize · select · state) | `test_skills.py` |
| dag (model · validate · store · schedule) | `test_dag.py` |
| workflow (graph · runner) · workflow files | `test_workflow.py`, `test_workflowfile.py` |
| dynamic workflow | `test_dynamic.py` |
| ultracode (stages · plan · engine) | `test_ultracode.py` |
| verify | `test_verify.py` |
| health | `test_health.py` |
| metrics | `test_metrics.py` |
| projectscan · projectdoc | `test_init.py` |
| pil | `test_pil.py` |
| logging | `test_logging.py` |

`conftest.py` redirects `AGENT2_DB` to a throwaway temp DB, so the suite never
touches a real `agent2.db`.

```bash
python -m pytest .github/tests/test_core.py -v
python -m pytest .github/tests/            # the whole suite: 2670 tests
```

Many of these are **sabotage-verified** — the test was proven to fail against a
deliberate break before it was trusted to pass. Two *tautology* traps have been
found and fixed in this repository, so a test that looks trivial is suspect only
*after* you have read it.
