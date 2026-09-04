# agent2.core — the centralized runtime

`agent2/core/` is the single source of truth for everything that would otherwise
be duplicated between the Web UI (`agent2web.py`) and the CLI (`agent2cli.py`):
the workspace sandbox, per-chat execution isolation, context assembly, command
execution, cancellation, authorization, secrets, structured logging, and the
shared memory/rules/context backends. Both interfaces import these modules — no
interface re-implements this logic locally.

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
├── pil/           the offline Personal Intelligence Layer
├── commands.py    execution STATE — the lifecycle and the watchdog verdict
├── procio.py      pipe I/O and the process-tree kill; records nothing
├── scheduler.py   bounded worker pool for agent turns
├── tasks.py       persistent tasks + checkpoints
├── recovery.py    picking interrupted work back up
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
- **recovery** — picks interrupted work back up and ⚠️ **never re-runs completed
  work**; it prepends a briefing rather than replaying tool calls.

⚠️ `stop_agent` and disconnect call **both** `scheduler.cancel()` and
`sessions.cancel()` — different halves of one guarantee.

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
fifth `"mod"` row tag: `diffview.revert_change` rebuilds the before-side as
`[t for tag,t in ch.lines if tag in ("del","ctx")]` and **writes it to disk**, so a
`mod` tag would silently delete every modified line from the user's file.

- `summarize()` is the one thing that says what a set of changes **totals** to. A
  local `sum()` in a renderer makes the CLI and the browser disagree about one turn.
- `preview_window()` is the one window function. Re-deriving it in a renderer is
  how two surfaces show different slices of the same change.
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
Fourteen named helpers emit fourteen **dotted** kinds, and two of them diverge by
more than punctuation — grep for the right-hand column, never the function name:

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

A generic `event()` / `exception()` pair covers anything else; production code uses
it with three further dotted names (`agent.model_fallback`, `agent.part_skipped`,
`scheduler.error`), for **17** distinct kind strings in total.

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
| sync | `test_sync.py` |
| diffs | `test_diffs.py` |
| permissions | `test_authz.py` |
| secrets | `test_secrets.py` |
| pil | `test_pil.py` |
| logging | `test_logging.py` |

`conftest.py` redirects `AGENT2_DB` to a throwaway temp DB, so the suite never
touches a real `agent2.db`.

```bash
python -m pytest .github/tests/test_core.py -v
python -m pytest .github/tests/            # the whole suite: 1602 tests
```

Many of these are **sabotage-verified** — the test was proven to fail against a
deliberate break before it was trusted to pass. Two *tautology* traps have been
found and fixed in this repository, so a test that looks trivial is suspect only
*after* you have read it.
