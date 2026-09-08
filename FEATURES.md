<!--
Author: Aarav Shah
Portfolio: aaravshah1311.is-great.net
github: github.com/aaravshah1311
-->

# ✨ Agent-2 — Feature Inventory

The full inventory of **Agent-2**, a self-hosted autonomous AI agent powered
by Google Gemini. It covers **what exists**, **what is tested**, and **what is
deliberately not built** — with counted numbers, not marketing.

> **One line:** an autonomous agent — 18 tools, one generalized DAG behind
> workflows and an autonomous loop, offline personalization, universal file
> processing, Burp and ZAP over MCP, a capability model and sealed credentials —
> that runs as a CLI, a web app or both at once over a single SQLite file, backed
> by **2 670 tests**.

Every figure below was read out of the code. Where an older document and the code
disagreed, the code won. Where something is designed and not implemented, it says
so — see [What is *not* implemented](#-what-is-not-implemented).

---

## 📊 At a glance

| | Counted |
|---|---|
| Surfaces over one brain | **3** — CLI *(default)*, Web UI, Dual |
| Agent tools | **18** — 13 core + 5 File Intelligence |
| Gemini models | **6** in **3** modes, plus any OpenAI/Anthropic-compatible endpoint |
| Slash commands | **36**, plus **9** keybindings |
| HTTP endpoints | **81** in `server/routes.py`, plus **4** in `server/auth.py` |
| Socket.IO events | **25** server→client, **8** client→server, plus **2** lifecycle |
| SQLite tables | **27**, across **32** migrations |
| Environment variables | **116** |
| Named audit event kinds | **35** — **31** from the named helpers, **4** written directly |
| File Intelligence | **11** registered plugins (7 modules) · **71** formats detected, **69** with operations · **331** format/operation pairs · **26** operations |
| Graph execution | **1** DAG engine · **9** node states · **4** consumers (Workflow, Workflow files, Dynamic Workflow, UltraCode) |
| Tests | **2 670** across **51** test modules, many sabotage-verified |
| Telemetry | **0 bytes**. No account, no upload, no phone-home |

⚠️ **Endpoints are METHOD+path pairs, not decorators** — one `@app.route` carrying
`methods=["GET", "POST"]` is two endpoints. ⚠️ **Slash commands are the length of
`cli/render.SLASH_COMMANDS`**, never a grep for `/[a-z]+` (that counts aliases like
`/colour` and `/config`); keybindings are the length of `cli/statusbar.SHORTCUTS`,
never a grep for `Ctrl+` (that misses Esc, Tab and F1). ⚠️ **71 and 69 are both
right** — 71 is what the detector *recognises*, 69 is what the capability registry
holds operations for, so two formats are identified and have nothing to run.

---

## 🧠 The agent core

| Feature | Details |
|---|---|
| **Autonomous loop** | Plan → act → read the output → correct → verify. Chains tool calls until the task is done; there is no per-step approval gate |
| **Iteration ceiling** | `MAX_AGENT_ITERS = 80` tool-call rounds per turn |
| **Conversation window** | `MAX_CTX_MESSAGES = 40` messages assembled per turn |
| **Tool output cap** | `MAX_TOOL_OUTPUT = 6000` characters per result, `MAX_OUTPUT_LINE = 8000` per line |
| **Two loops, one prompt** | `agent2/agent.py` drives Gemini; `agent2/llm/provider_agent.py` mirrors it exactly for custom providers — same tools, same events, stdlib `urllib` only |
| **One prompt tail** | `core.broker.base_tail()` decides what the system prompt *ends* with. Appending a block inside either loop would make the two send different context for the same turn, each correct on its own |
| **Prompt caching** | A cached static body plus two versioned blocks, so `system_prompt()` costs **zero queries** on a warm cache — pinned by counting collector calls, not queries |
| **Plan emission** | `emit_plan` and `update_todo` let the model publish a checklist that both surfaces render live |

### Context Broker — what the model sees this turn

`agent2/core/broker/` is the one answer to "what does the model know right now".
It sits *above* the existing context machinery and replaces none of it.

| | |
|---|---|
| **Ten sources, one order** | `conversation` · `project` · `git_state` · `skills` · `task_results` · `workflow_state` · `mcp_results` · `files` · `memory` · `rules` |
| **Always on** | `memory` and `rules`, rendered **last** so the prompt ends on the user's own standing orders |
| **Per-source caps** | `PROJECT_DOC_CHARS = 6000`, `MAX_DOC_FILES = 6`, `MAX_TASK_LINES = 8`, `MAX_FILE_LINES = 12`; the primary project doc is `.agent2/agent2.md` |
| **Four measures per item** | `priority`, `relevance`, `token_cost`, `freshness` — all assigned in one place, and `None` means *not stated*, never zero |
| **Order ≠ priority** | `ORDER` is where a source **prints**; `PRIORITY` is what **survives** a budget. They deliberately disagree — memory and rules print last and rank first |
| **One guard per collector** | A failing source lands on `bundle.errors` and is simply absent. One `try` around the loop would let a missing `git` binary cost you your memories and rules |
| **Reportable** | `to_payload()` names every source **and every failure**, and never carries the item text |

> ⚠️ The bundle is a **parameter** of `system_prompt()`, not an internal call. The
> prompt is built once per turn but read on every one of up to 80 iterations — a
> prompt that assembled its own context would pay the file reads, the `git` calls
> and the task query per iteration, and could hand the model a different prompt
> halfway through one turn.

### Token budget — what fits inside the model's window

`core/broker/budget.py` decides what survives when the assembled context is larger
than the model can take.

| | |
|---|---|
| **Where the window comes from** | `AGENT2_CONTEXT_BUDGET` (an operator wrote a number down) → the model's own `context_window` capability → `ASSUMED_WINDOW = 32 000` when nothing is known |
| **Headroom** | `RESERVE_TOKENS = 6 000` held back for the reply, tunable with `AGENT2_CONTEXT_RESERVE` |
| **Floor** | `MIN_LIMIT = 512`, applied to every basis **except** an explicit `AGENT2_CONTEXT_BUDGET` — a number someone typed on purpose is not second-guessed |
| **Reported basis** | `env` · `model` · `assumed` · `caller`, so the report says *why* that limit was used |
| **One estimator** | `llm.router.estimate_tokens()`. The broker had a second `len // 4` for a day: the trim then fit a budget measured one way while the model was chosen against another, each half self-consistent |
| **One survival order** | `sources.rank()`. A second sort in a renderer would describe a trim different from the one performed |

### Context isolation — one project's rows stay one project's

`core/broker/isolation.py` is the one declaration of "may this project see that
row". Memories and rules carry a `project` column (migrations 17 and 18): a read is
*mine OR shared*, a write is stamped with the current project, and sharing is
explicit.

| | |
|---|---|
| **Mode** | `AGENT2_CONTEXT_ISOLATION` = `project` *(default)* or `off` (one global store, the pre-isolation behaviour) |
| **Unknown value** | Falls back to `project`, never `off` — the same strict direction `AGENT2_WEB_ROLE` falls in. A typo must not silently disable scoping |
| **Governed today** | `memories` and `rules` |
| **The key** | `core.context.project_key()`, never a raw path |

> ⚠️ **`''` is the column default, and that is load-bearing.** Every row that
> existed before isolation reads as *shared* and stays visible in every project,
> exactly as it was. An upgrade that had defaulted to "belongs to whichever project
> happened to be open during the migration" would have made a user's entire memory
> store vanish from every other checkout — with no error and nothing to point at.
> The same default protects any writer that reaches the table without going through
> `core.memory` or `core.rules`: an unstamped row is visible, never invisible.

---

## 🛠 The 18 agent tools

**Core (13)**

| Tool | What it does |
|---|---|
| `run_command` | Shell execution, streamed, with the watchdog attached |
| `read_file` · `write_file` | Read and write inside the workspace |
| `multi_edit_files` | Several edits across several files in one call |
| `list_dir` · `grep_search` | Navigate and search |
| `delete_file` | Remove a file (gated by the `fs.delete` capability) |
| `scan_project` | Structural survey of the tree |
| `web_search` | DuckDuckGo — no API key required |
| `update_todo` · `emit_plan` | Publish and update a live task list |
| `save_memory` | Persist a durable fact |
| `update_project_doc` | Re-scan the project and refresh `.agent2/agent2.md` — the same `scan()` + `projectdoc.apply()` `/init` calls, with `describe` defaulting to **False** so a factual refresh costs no model call |

**File Intelligence (5)** — `detect_file`, `file_capabilities`, `run_file_op`,
`convert_file`, `search_workspace`. Router-dispatched shims into
`agent2.fileintel`, so the backend is chosen internally rather than by the model.

| Invariant | The bug it prevents |
|---|---|
| A tool exists only if **four** lists agree — `agent._LOCAL_TOOLS`, `tools.REGISTRY` + `tools._build_tools()`, `cli/tooling._SHARED_TOOLS` + `_build_tools()`, and `llm.providers.agent_tool_schema()` | Each omission is a *different* silent failure: missing from `_LOCAL_TOOLS` ⇒ "not registered" mid-turn; missing from a `_build_tools()` ⇒ the model is never told the tool exists on that surface; missing from `agent_tool_schema()` ⇒ every custom provider can dispatch it and none is advertised it. **Two of those shipped** — the CLI schema declared none of the five File Intelligence tools, and the provider schema omitted `emit_plan`. The guard that let them through was per-name; the invariant is that the name **sets** agree, now pinned in both directions |
| `run_command` is the **only** tool that bypasses `dispatch_tool` | So `terminal.stream_command` and `cli/runtime.run_cmd_stream` each carry their own exec gate. The CLI's sits **outside** the retry loop — a refusal must not be able to change on retry |
| `update_project_doc` is a **tool**, not a hook inside `write_file` | A hidden refresh at the file-write chokepoint would be a whole project walk per file written, a fifth call site across four agent loops, and a write the user can see in neither the transcript nor the diff viewer |
| The capability gate returns a tool **`error`**, never an exception | The model reads errors and adapts. An exception ends the turn instead |
| MCP tool ownership is **membership**, not prefix | `registry.resolve()` asks each bridge whether it actually holds the name, so a disconnected ZAP stops claiming `zap_*` instead of swallowing the call as "not registered" |
| `sanitize_name()` **force-prefixes** every MCP tool | It is the only reason the two agent loops may check `_LOCAL_TOOLS` in opposite orders. Drop the prefix and an MCP tool can shadow a local one on one surface and not the other |

When Burp is connected, `burp_*` tools are added dynamically from the live MCP
session; ZAP's `zap_*` tools arrive the same way.

---

## 🤖 Models and modes

| Key | API id | Group |
|---|---|---|
| `2.5-flash` *(default)* | `gemini-2.5-flash` | `2.5` |
| `2.5-flash-lite` | `gemini-2.5-flash-lite` | `2.5` |
| `3.5-flash` | `gemini-3.5-flash` | `3.5` |
| `3.5-flash-lite` | `gemini-3.5-flash-lite` | `3.5` |
| `3.6-flash` | `gemini-3.6-flash` | `3.6` |
| `3.7-flash` | `gemini-3.7-flash` | `3.7` |

| Mode | Max output tokens | Thinking budget |
|---|---|---|
| `fast` | 2 048 | — |
| `pro` *(default)* | 8 192 | — |
| `thinking` | 16 384 | 8 000 |

> ⚠️ **`THINKING_GROUPS` is the empty tuple, so every group supports thinking.**
> `supports_thinking()` reads *"no group is excluded"*, not *"no group qualifies"*.
> `MODES["thinking"]["desc"]` still reads "(2.5/3.5 models only)" — **that string
> is the stale half**, and it is display copy, not the predicate. Never re-derive
> the answer from a model key: the predicate takes a *group* precisely so a new
> model inherits the answer by joining an existing one.

`config.MODELS` and `config.MODES` are the **only** declaration of either table.

### Model capabilities — three values, all distinct

`agent2/llm/capabilities.py` records what each selectable model can do as
**`True` · `False` · `None` = we do not know**.

- `supports()` takes a **required** `default`, so every caller states which way it
  wants to be wrong: a hard requirement (vision) reads unknown as unusable, while
  a preference only deranks.
- Tiers are a closed vocabulary — `basic` · `strong` · `advanced` · `frontier` —
  relative to this catalog, never a vendor claim. `frontier` exists because a
  user's Claude-Opus/GPT-5 provider was losing every reasoning turn to a built-in:
  both were `advanced`, the sort is stable, and built-ins come first.
- Four sources in precedence order: `configured` (you) → `model-ranked` (an LLM was
  asked) → `inferred` (the id matched a family) → `catalog` (built-in) → `provider`
  (nothing known).
- The catalog is **derived from `config.MODELS`**, not a second dict keyed by model
  key. That version went stale the first time the model table was edited, leaving
  added models all-unknown and unroutable.
- `/model caps` prints unknown as `?`, never as "no".

### Routing and fallback

| | |
|---|---|
| **Explicit selection wins** | Routing is opt-in. Both surfaces send a model key every turn, so a router that ran unasked could not tell a choice from a default |
| **`auto`** | A user-selectable pseudo-key, deliberately **not** in `config.MODELS` — that list is the set of things that can be *called* |
| **Policy** | `AGENT2_MODEL_ROUTING` = `off` · `default_only` · `always`. Empty by default so the stored `model.routing` setting can win |
| **One ordering** | `rank_candidates()` serves `choose()`, `next_model()` **and** the CLI's fallback list. Two sorts would make the web loop and the CLI fall back to different models for the same failure |
| **Total by design** | A hard requirement that empties the pool is discarded and the reason recorded — "no model" breaks the app |
| **A fallback is a `continue`** | Context, tokens, `ToolContext`, checkpoints and the cancel token all survive. Restarting the turn would replay completed tool calls |
| **What never hops** | `auth` failures (a bad key fails every model) and unclassified errors (they fail everywhere, so a hop just doubles the cost) |
| **Hop budget** | `AGENT2_FALLBACK_MAX_HOPS = 2` — **per turn**, not per model |
| **Circuit breaker** | 3 failures in 120 s cools a model for 60 s; a success clears it; `auth` never trips it; and cooling models are still returned as a **last resort**, because an empty pool is not safer than a bruised one |
| **Ledger** | Every call recorded in `model_attempts` (primary · fallback_of · kind · latency), trimmed at 500 rows by the writer |
| **Dry run** | `POST /api/models/route` reports the decision without running a turn |

### Custom providers

Any OpenAI- or Anthropic-compatible endpoint — base URL, key, model id, format —
stored in the `providers` table and appearing as `custom:<id>` in the selector.
`provider_agent.py` mirrors `agent.py` exactly and uses **stdlib `urllib` only**.

> ⚠️ In `update_provider()`, an **absent** key field means *leave it alone* and
> `""` means *clear it*. The edit form shows the key pre-filled with bullets, so an
> edit that only changes the model id submits an untouched key field — and reading
> absent as empty would wipe a working credential.

### API keys

Rotation, pinning, per-key error counts and usage, all through **one**
`llm.keys.rotator`. A second `KeyRotator` would mean a second cursor, disagreeing
counters and an invisible pin. Keys are redacted on every display path and stored
as `a2s:` references (see [Secrets](#-secret-storage)).

---

## ⚡ The CLI surface

The default surface: Rich + prompt_toolkit, `agent2cli.py`.

| Feature | Details |
|---|---|
| **29 slash commands** | Full list in [the docs](https://agent2.is-best.net/docs/cli-commands/); dispatcher-only aliases (`/quit`, `/colour`, `/config`, `/burp`) also resolve |
| **5 keybindings** | Ctrl+B diff viewer · Ctrl+P command palette · Ctrl+T terminal · F1 help · Ctrl+L clear |
| **Ephemeral menus** | `↑↓` navigate, `Space` toggles, `Enter` confirms, `Esc` cancels — and the menu **erases itself**, so scrollback keeps `✓ Model → 3.5-flash` and not the selector |
| **Slash autocomplete** | Completion over commands and their arguments as you type |
| **Ghost text** | Offline next-word suggestions from the Personal Intelligence Layer. No network call, no model call |
| **Live status bar** | Model, mode, branch and dirty state, served stale rather than blocking a render |
| **Mid-execution queue** | Type while the agent works; your message is queued and delivered at the next turn boundary |
| **Themes** | `/theme` for the palette, `/color` for the accent. Colours come from one shared `P` object — importing a snapshot freezes that colour forever |
| **`/load`** | CLI only. A launch always starts clean, and this is the only way back to the last conversation **from this directory** |
| **Non-blocking prompts** | The stuck-command prompt and the recovery prompt never block the REPL |

> ⚠️ **One menu system.** `ephemeral_picker` and `ephemeral_toggle_menu` are built by
> one `_menu_app`/`_menu_window` pair — one `Application`, one `Window`. The text
> fallbacks in `interactive.py` are plain `print` + `input` and may never grow a
> renderer again: they used to be a near-copy of the real one *minus*
> `erase_when_done=True`, and the picker degrades to them on **any** exception — so a
> working menu was one raised exception away from never erasing again, with no error
> and no clue.
>
> ⚠️ A menu is capped at `menu_body_rows()` and scrolls to the cursor fragment on the
> highlighted row. prompt_toolkit clips an inline app to the terminal height, and
> `erase_when_done` can only clear the region it *drew* — so an over-long body is
> both unnavigable **and** un-erasable.

### Cancellation — Ctrl+C stops the command, not the session

Nothing on this path raises, exits or unwinds the REPL.

- `trigger_cancel()` sets the cancel event, **settles the command state, then** tears
  down the process tree — in that order, which is load-bearing.
- ⚠️ Kill first and the pipes close, the runner falls out of its drain loop, asks
  "was this cancelled?", is told *not yet*, calls `proc.wait()` and settles the row
  with the **shell's** exit code. The transcript then reads `FAILED · exit 1`, and
  that fabricated failure is what reaches the model.
- ⚠️ The key listener does **not** exit on Esc. A listener that exits leaves you
  unable to press Esc, queue a message or answer the stuck prompt for the rest of
  the turn — the session would be alive in name only.
- The way out is deliberate: two Ctrl+C within the double-tap window exit, and
  presses ≤ 50 ms apart are one physical press delivered twice.
- One cancel cleans **six** things: the subprocess, its children, the pipes, the task
  checkpoint, the scheduler state and the command record.

---

## 🔍 Diffs and the Ctrl+B viewer

| | |
|---|---|
| **Computed before the write** | `capture_for()` snapshots the file *before* a writing tool runs |
| **Display only** | There is **no approval gate**. Nothing in the diff pipeline can block or alter a write |
| **The colour contract** | Green added · red removed · grey context · **yellow is the paired count** `~min(added, removed)`, plus the `@@` markers |
| **One total** | `summarize()` is the only thing that says what a set of changes totals to. A local `sum()` in a renderer would make the CLI and the browser disagree about one turn |
| **The summary row** | `└ Added N lines, removed N lines, ~N modified` — a written contract, implemented twice on purpose, and never shipped pre-formatted in the payload |
| **One window function** | `preview_window()`. Re-deriving the window in a renderer is how two surfaces show different slices of the same change |
| **Turn boundaries** | `DiffStore.mark()` → `since(mark)`. A saved `len(store.all())` breaks the moment the cap engages, because `add()` trims from the front |
| **Three different facts** | `hidden` (windowed) · `truncated` (engine limit) · `dropped` (session cap) — the viewer states all three, including when they are zero |
| **In-memory by design** | There is no `file_changes` table, and dual mode runs the CLI as a child process — so "the whole session" can only ever mean "this process's" |

**The viewer** (`Ctrl+B`): `f` whole file · `d`/`u` unified · `s` side-by-side ·
`c` context · `e` collapse · `/` search with `n`/`p` · `y` copy · `w` write patch ·
`←→` files · `q`/`Esc`/`Ctrl+B` close.

> ⚠️ **Opening it writes nothing.** Every path to the first paint is a read — a viewer
> that could act merely by opening would be an approval gate nobody designed and
> nobody can see. `[A] Accept` is a review **mark**, and its own toast says
> `nothing written`.
>
> ⚠️ **`r` is the one destructive key, and it takes two presses.** It used to fire on
> the first while the legend called it "Reject" — a user following the legend rewrote
> their own file expecting a label. The armed value is a **file index**, so it is
> compared against a sentinel and never truth-tested: file 0 is a falsy index, and
> `if not armed` looked perfect going 0→1 while reverting the wrong file coming back.
> `r` also refuses outright on a truncated diff and says why.
>
> ⚠️ `[E] Edit` exits, launches the editor and re-enters, because the terminal is
> only restored on the way out of the app — so the marks live outside the inner run
> loop or they vanish across the round trip. And a **human's** edit is deliberately
> not captured into the store: the store is the agent's changes, and recording a hand
> edit there would make the revert path's before-side wrong.

Two blind spots are documented in the code that has them: a file written by
`run_command` or by File Intelligence produces **no** diff, and a wholly-failed
`multi_edit_files` still renders an empty one.

---

## ⏱ Command execution and the watchdog

Three registries that look redundant and are not: `cli/state.py` and
`terminal._procs` hold process **handles**, `core/commands.py` holds execution
**state**, and `core/procio.py` owns pipe **I/O** and the tree kill while recording
nothing.

Lifecycle: `CREATED → STARTING → RUNNING → STREAMING → COMPLETED`, with `FAILED`,
`TIMEOUT`, `CANCELLED` and `KILLED` as the failure set.

| | |
|---|---|
| **Concurrent draining** | One thread per pipe into one queue. Reading them in sequence deadlocks the moment one fills |
| **Ticks, not timers** | `drain` yields a tick even in total silence, and the watchdog rides those ticks — so there is no timer thread and no second opinion about whether the process is alive |
| **`watch()` returns a word** | It never acts. The runner acts, because the runner owns the handle |
| **Both kill ceilings default OFF** | `AGENT2_CMD_TIMEOUT = 0`, `AGENT2_CMD_IDLE_TIMEOUT = 0`. Only the stuck **report** is on (`AGENT2_CMD_STUCK_SEC = 20`) |
| **Heartbeat** | A live "Elapsed / Last output" line at `AGENT2_CMD_HEARTBEAT_SEC = 1.0` |
| **The stuck prompt** | `[W]ait` grants 60 s more · `[R]etry` is **only ever a keypress**, never automatic, capped at 2 |
| **Exit conventions** | rc 124 = timed out (`timeout(1)`'s convention) · rc 130 = killed or cancelled |
| **Whoever settles first wins** | `_mutate` refuses to move a settled execution, which is what makes "record, then kill" safe |
| **Inspectable** | `GET /api/commands` with `?session=` `?task=` `?limit=`; the web pane is *pushed* `command_heartbeat` / `command_stuck` / `command_unstuck` and keeps no clock of its own |

---

## 🌐 The Web UI

Flask + Socket.IO at `http://localhost:1311`.

| Feature | Details |
|---|---|
| **67 HTTP endpoints** | Chats, memories, rules, keys, providers, MCP, PIL, tasks, commands, sync, models, platform, health, auth, recovery, workspace |
| **29 pushed event types** | Including streamed tool calls, per-file diffs, command heartbeats and PIL suggestions |
| **Multi-tab terminals** | Real shells per tab, with `terminal_input` / `terminal_kill` |
| **Raw commands** | `run_raw_command` runs a shell command with **no AI** in the loop |
| **Message editing** | `edit_message` truncates history at that point and re-runs the turn |
| **Attachments** | Files sent with a message |
| **Live diffs** | `chat_file_diff` per file plus a summary bar |
| **Recovery** | `GET`/`POST /api/recovery` picks up interrupted work |
| **Port honesty** | The server never hard-fails on a busy port — read the port it *reports* rather than assuming 1311 |
| **Health** | `GET /api/health` returns 200/503 with a `problems` list and counters only |

> ⚠️ A **disabled** MCP server is not a problem. Only a server that is enabled and
> cannot be reached raises `failing`, because an endpoint that cries wolf gets
> ignored.

### Dual mode

Web server on a daemon thread, CLI in the foreground as a **child process** —
prompt_toolkit needs a real TTY, and a CLI crash must not take the server down.
One process tree, one `agent2.db`, both surfaces live. The two halves hold two
**disjoint** in-memory diff stores, and dual sets `AGENT2_LOG_CONSOLE=0` so the web
half cannot paint over the CLI prompt.

---

## 🧾 Memory and rules

- **Memories** — durable facts, written from exactly three places: the `save_memory`
  tool, `POST /api/memories`, and `/addmem`. Deduplicated on exact content. ⚠️ Only
  the **tool** accepts `importance` and `tags`; the route reads `content` and
  `shared` alone, so a client that sends the other two gets a `200` and loses them.
- **Rules** — standing instructions, activated in bulk (`PUT /api/rules/active` is
  uniform, not a toggle) and injected into every system prompt.
- Bulk delete (`ids` or `all: true`) runs as one transaction with one cache rebuild;
  `POST /api/memories/prune` drops the least valuable beyond `keep` while sparing
  anything at or above `min_importance`.

> ⚠️ Never a raw `INSERT` into `memories`. It skips the dedup **and** the
> `sync.notify()` that invalidates the prompt, so the model keeps reading the old
> block.
>
> ⚠️ Both blocks reach the model through `VersionedCache` objects that live in
> `core/broker/`; the names in `agent.py` are **aliases** of them. `notify()`
> invalidates the object you hold, so a second cache for the same resource means one
> surface keeps serving a deleted memory — silently, and only in one of the two
> agent loops.

---

## 🧬 Personal Intelligence Layer

Fully **offline** and privacy-first: it sits between you and the model, enhances
prompts and learns passively, with **no network and no retraining**.

| Module | Role |
|---|---|
| `memory.py` | Data layer over the four `pil_*` tables; owns the per-kind revision counters |
| `learning.py` | The shared passive engine — accept **+0.08**, ignore **−0.03**, accept-then-delete **−0.20** |
| `prediction.py` | **Module 1** — offline ghost text from a trie, an n-gram table and phrase prefixes. Never calls an LLM; returns data only |
| `grammar.py` | **Module 2** *(opt-in, off)* — deterministic rules; masks technical spans, corrects, restores verbatim |
| `improve.py` | **Module 3** *(opt-in, off)* — adds only preferences proven above the weight floor. Never invents requirements |
| `optimize.py` | Idle-only pass: dedupe, merge, recompute, prune |
| `pretraining.py` | Cold-start through the real learning engine, once |

Tables: `pil_vocab`, `pil_phrases`, `pil_ngrams`, `pil_prefs`. Settings:
`pil.enabled` (on), `pil.prediction` (on), `pil.learning` (on), `pil.grammar`
(**off**), `pil.improve` (**off**), `pil.autooptimize` (on).

Both agent loops learn from the **original** text and send the processed copy —
history and display keep the original — emit `pil_enhanced` when the two differ, and
spawn a background optimize pass on roughly one turn in ten. Every function
swallows errors and returns a safe default: **PIL failure degrades to doing
nothing**. `POST /api/pil/wipe` is a real "forget me".

---

## 📂 File Intelligence

Detect, convert and operate on real file formats through one router.

| Counted from the live registry | |
|---|---|
| Registered plugin names | **11** (from 7 plugin modules — `documents.py` registers three, `media.py` two, `spreadsheets.py` two) |
| Formats | **69** |
| Format/operation pairs | **331** |
| Distinct operation verbs | **26** — from `read` and `extract_text` to `ocr`, `transcribe`, `formula_audit` and `speaker_notes` |
| Categories | **8** — archives, code, documents, images, audio, video, presentations, spreadsheets |

Five tool shims (`detect_file`, `file_capabilities`, `run_file_op`, `convert_file`,
`search_workspace`) reach it, so the backend is chosen internally. Detection is by
content, not by extension. A `FileIntelError` — `UnsafePath`, `FileTooLarge` — is
**never** treated as recoverable by the router, so a refusal can never be retried
into success by the next backend. Every operation is appended to the `file_ops`
table as an SQL-readable audit trail.

---

## 🔌 MCP bridges — Burp Suite and OWASP ZAP

One base class, one registry, two servers.

| | Burp | OWASP ZAP |
|---|---|---|
| Default endpoint | `http://127.0.0.1:9876` | `http://127.0.0.1:8282` |
| Transport | SSE at `/sse` | streamable HTTP probed **before** SSE — the add-on documents neither |
| Credential | — | a "Security Key", sent as the `Authorization` value **verbatim** |
| Auto-connect | **off** | **off** |

Each bridge runs its own asyncio loop on a daemon thread and exposes a synchronous,
thread-safe API. The `mcp` SDK is a **soft** dependency, so an absent one reads as
"not connected" rather than an import crash.

**One surface for every server: `/mcp`.**
`/mcp` (menu) · `/mcp status` · `/mcp <server> connect|disconnect|list|status|config` ·
`/mcp connect` / `/mcp disconnect` (every server) · `/mcp health`.
The parser resolves a server through `registry.get()` and contains **no literal
server name** — a third bridge appears in the grammar by being registered, and the
alternative (`if word in ("burp","zap")`) keeps working while silently never
matching the new one. `/burp` is retired from the help table but still **forwards**
and says where it went.

Auto-connect is **per project** and set implicitly: `connect`, `disconnect` and the
bare `/mcp` menu write it through `set_auto_connect()`. There is no `/mcp auto`
sub-command; `POST /api/mcp/<key>/auto` exists for the web surface.

| Invariant | The bug it prevents |
|---|---|
| `mcp_config` is keyed by **server alone**; `mcp_state` by **(project, server)** | "Should ZAP arm itself in this checkout" is per-target, but "which ZAP, on which port, with which key" describes the one ZAP on this machine. Keying that per project reintroduces exactly the retyping friction it removed |
| `enabled`, `url` and `key` are **read-through properties** | Dual mode is two processes over one DB. A cached URL means the browser half keeps dialling the port the CLI half already changed — each surface showing the value *it* believes, and neither showing the disagreement |
| `port` is **derived** from the URL, never stored | A second copy that disagrees with the host in its own URL string |
| `None` means *leave it alone*, `""` means *clear it* | Both editors show the key pre-filled with bullets, so an edit that only changes the port submits an untouched key field. Reading absent as empty would wipe a working credential |
| The legacy global setting is read forever, written **never** | It is the fallback for every *unconfigured* project — syncing it on a toggle would make "ZAP off in this checkout" quietly mean "ZAP off in all of them" |
| `registry.health()` is the one verdict | `/mcp health`, `/api/health` and the web panel would otherwise disagree the moment one of them learned a new state. ⚠️ And `ok` is **not** `connected`: a server that is *off* is fine, so a second opinion is what prints `✗` at a user who disabled it on purpose |
| Switching projects never **disconnects** a live bridge | `enabled` gates *starting* a session. Tearing down an in-flight scan to enforce a preference about starting one is a destructive act nobody asked for |

> ⚠️ There is deliberately **no "skip TLS verification" switch**. Quietly trusting
> any certificate on a security tool's control channel is exactly the kind of silent
> downgrade this project refuses to ship.

---

## 🎯 Security testing

Purpose-built for security research and CTF work. `run_command` drives the real
toolchain — `nmap`, `nikto`, `gobuster`, `ffuf`, `sqlmap`, `hydra`, `metasploit`,
`searchsploit`, `theharvester`, `binwalk`, `strings`, `volatility` — and the agent
reads every line of output and decides the next step from what it actually saw.
When Burp is connected, `burp_*` tools drive proxy history, Repeater, Intruder,
Scanner and the site map.

Both kill ceilings default **off** on purpose: a long scan killed at 20 seconds
returns a truncated result the model cannot recognise as truncated, which is worse
than a slow one. The stuck **report** is what is on by default.

> **Authorized use only.** Scanning or testing systems you do not own, or are not
> authorized to test, is illegal in most jurisdictions. Point it at your own lab, a
> deliberately vulnerable target, or a signed scope.

---

## 🔐 Security posture

### Who may reach the web surface

`server/auth.py` — **one** decision function, installed once from
`register_routes()`, so an app that has the API has the guard. There is no per-route
decorator to forget.

- Modes from `AGENT2_WEB_AUTH`: `auto` (loopback trusted, everything else needs the
  token) · `always` · `off`. **Env only** — a stored "off" would be a persistent
  silent downgrade.
- ⚠️ The Socket.IO handshake does **not** pass through Flask's `before_request` —
  engineio wraps the WSGI app from outside — so the connect handler asks the same
  function itself. `run_raw_command` is a socket event, so a guard on `/api/*` alone
  would lock every reader and leave the shell open.
- ⚠️ Origin is checked on unsafe methods **before** identity, because loopback trust
  cannot tell your own tab from a page you happened to visit.
- CSRF double-submit applies to **cookie** credentials only; a bearer caller attached
  its credential deliberately.
- The access token is memory-only (or `AGENT2_WEB_TOKEN`) and **never written to
  disk**. `web_sessions` stores SHA-256 **digests**, so a copied `agent2.db` grants
  nothing.
- Sliding-window rate limits per IP: 600 requests/min and 10 login attempts/5 min.
- ⚠️ Every swallow fails **closed** — except revoke, which raises, because a
  silently-failed revoke is the one error that hands access *back*.

### What an identity may do

`core/permissions.py` — one capability model enforced across HTTP routes, socket
events **and** agent tools.

- Roles: `owner` (default, everything) · `operator` (may drive the agent, may not
  touch credentials or MCP endpoints) · `viewer` (read).
- Two knobs, scoped differently on purpose: `AGENT2_WEB_ROLE` describes **web
  clients**; `AGENT2_DENY_CAPS` subtracts from **this whole process**, CLI included.
  One knob could not express "let me work locally, let the browser only watch".
- ⚠️ An unknown role falls back to `viewer`, and an unmapped mutating `/api/` route
  falls to `destructive`. Both fallbacks point at the strict end, so a typo or a new
  endpoint is refused rather than granted.

### How a credential is stored

`core/secrets.py` — credentials persist as `a2s:` **references**, never values.
Three columns hold them: `api_keys.api_key`, `providers.api_key`,
`mcp_config.security_key`.

Backends in order: OS keyring (only if a probe round-trip actually **succeeds** —
importability is not availability) → AES-256-GCM, or a stdlib BLAKE2b+HMAC
construction → announced plaintext.

| Invariant | The bug it prevents |
|---|---|
| `resolve()` passes a **non-ref through unchanged** | Every legacy row and every half-finished migration keeps working |
| `seal()` **reads back what it wrote** | A caller cannot persist a dangling reference; on a round-trip mismatch it returns the plaintext instead |
| There is **no in-memory-key fallback** | That would write ciphertext the next process cannot decrypt — a security feature turned into silent data loss |
| The master key defaults to `~/.agent2/secret.key` | **Outside** the DB directory, because a key beside `agent2.db` travels with every copy of it |
| Masking is a **constant** `••••••••` | Its length is not derived from the secret. The old `k[:6]…k[-4:]` form printed most of a ten-character ZAP key, and would print six characters of ciphertext once rows hold references |

**Honest scope:** this defends against exposure of the database *alone*, and
`describe()` says so in its own payload. It is not full-disk protection.

---

## 🔁 Tasks, recovery and scheduling

| | |
|---|---|
| **Persistent tasks** | `agent_tasks` + `task_sessions`, with progress and checkpoints, exposed at `GET /api/tasks` |
| **Recovery** | Interrupted work is picked up from its checkpoint and **never re-runs completed work** |
| **Pause / resume** | Per chat, over HTTP and from the CLI |
| **Bounded worker pool** | `submit()` never blocks and returns `queued` / `disabled` / `rejected` — each with a required caller response, all three handled |
| **Turning the pool off** | ⚠️ Can never be why a message goes unanswered: with the pool disabled a turn runs on its own thread |
| **Cancellation** | `stop_agent` and disconnect call **both** `scheduler.cancel()` and `sessions.cancel()` — different halves of one guarantee |
| **Defaults** | `AGENT2_MAX_CONCURRENT_TURNS = 8`, `AGENT2_MAX_QUEUED_TURNS = 64` |

---

## 🔄 Cross-process sync

`core/sync.py` is **the** synchronization layer: `RWLock`, `KeyedLock`,
`AtomicCounter`, `EventBus`, `ChangeTracker`, `SyncPoller`, `VersionedCache`.

- In-process, `notify()` publishes **synchronously** so caches invalidate
  immediately, and subscriber exceptions are swallowed **and counted** — a broken
  listener never breaks a turn, but it is not invisible either.
- Cross-process, it bumps `sync_state`; the poller republishes what *another* process
  changed as `remote: True`, filtering self-bumps.
- Resources: `memories`, `rules`, `api_keys`, `providers`, `settings`, `workspace`,
  `chats`, `pil`, `tasks`, `mcp`. ⚠️ A topic missing from that list reaches the other
  surface **never**.
- ⚠️ **Every long-lived surface must start its own poller.** A REPL without one
  subscribes to a bus nobody posts to: a `/mcp zap config` in the web half never
  invalidates the CLI's cache, and the CLI keeps dialling the old endpoint until it
  restarts. It shipped that way once, and the CLI was the surface that forgot.
- Inspect at `GET /api/sync`; force a check with `POST /api/sync/poll`.

---

## 🕸 One generalized DAG

`core/dag/` is the **single** graph engine. Three modules with one job each:
`model.py` says what a graph, a node and an edge *are*, `validate.py` says whether one
can ever finish, `store.py` turns one into rows and reads it back. `schedule.py` is the
one scheduler over it.

| | |
|---|---|
| **Consumers** | **4** — Workflow, workflow *files*, Dynamic Workflow, UltraCode. Every one of them is a consumer, **never** an engine |
| **Node states** | **9** — `pending` · `ready` · `running` · `completed` · `failed` · `blocked` · `paused` · `cancelled` · `skipped` |
| **Storage** | **No new table and no migration.** A graph is one `exec_workflows` row plus N `agent_tasks` rows, node id on `CP_NODE` and run id on `CP_WORKFLOW` |
| **Entry points** | `create()` · `plan()` (writes nothing) · `extend()` (the one mutation path) · `load()` · `advance()` |

> ⚠️ **Feature-agnosticism is a test here, not a promise.** Four `ast`-based structural
> tests assert that no `if workflow:` / `if ultracode:` / `if security:` / `if zap:` /
> `if skill:` — and no feature name at all — appears in the package. The domain half is
> **injected** (`validate(…, knob=…, label=…)`), so a 4-node workflow is refused with
> the workflow feature's own sentence, word for word, by an engine that has never heard
> of workflows.

> ⚠️ **Six of the nine states are read straight off `agent_tasks.status`; READY and
> BLOCKED are derived on every read.** Storing readiness would let a crash leave a node
> claiming READY behind an upstream that never finished — and deriving is *cheaper*:
> `load()` is one `qone` + one `qall` for a graph of any size.

> ⚠️ **A cycle is the one error that produces no error.** `tasks.ready()` releases a
> node when its dependencies are *settled*, so a ring of three simply never becomes
> ready: `ready()` returns `[]` forever, the run sits at 0/3, nothing raises and nothing
> is logged. `find_cycles()` is iterative rather than recursive, because a 500-node
> chain must be **reported**, never turned into a `RecursionError`.

> ⚠️ **A mutation may not rewrite the past.** `validate_mutation()` refuses to drop a
> settled node, to rewire one, or to grow past `AGENT2_DAG_MAX_MUTATIONS` — *completed
> work stays completed; only remaining execution changes*, made checkable.

### The scheduler

*Never blindly run every READY node* is the whole of `schedule.py`'s job.
`plan_next()` is pure and writes nothing (it is what the CLI shows a human); `run()` is
the bounded pump.

- **Every declined node carries a reason.** `HOLD_CODES` says why *one* node waited;
  `REASONS` says how a whole *run* ended; `EVENTS` is a third vocabulary. No word is
  shared between them, and `taken | held == offered` is asserted — a planner cannot
  drop a node without saying so.
- **It derives no readiness of its own.** `tasks.ready()` is still the only predicate,
  and `schedule.py` calling neither `ready()` nor `blockers()` is an `ast` assertion.
- The corollary: `ready()` releases a node whose upstream **failed**, so declining it
  (`H_UPSTREAM`) and marking it SKIPPED is the scheduler's job — otherwise a run holds
  itself open forever on a branch that can never be work.
- **Five ceilings, two shapes.** `max_workers` / `max_running` bound a run;
  `kind_limits` bounds one *kind* at a time (commands, MCP calls); `kind_budgets`
  bounds what a run may **spend** on a kind over its whole life (model calls). A
  consumer that invents a node kind bounds it by adding a row.
- **Retries are bounded *and* classified.** `may_repeat()` asks `recovery.classify`
  first, so a node whose failure is not repeatable becomes a FAILED row carrying *why*
  rather than a row parked for a pump that has stopped counting.

> ⚠️ **In-flight is derived from the rows, never counted in memory.** Dual mode is two
> processes over one `agent2.db`, so a counter hands each of them a full allowance and
> the ceiling silently doubles. Same reason a **budget** is charged from
> `attempt_count`: a retry really is a second call, and a crash-resumed run gets no
> fresh allowance.

> ⚠️ **`AGENT2_DAG_MAX_WORKERS=0` is a supported answer, not a broken one** — nodes run
> inline on the calling thread, one at a time. It is `AGENT2_MAX_CONCURRENT_TURNS=0`'s
> posture, node-shaped.

> ⚠️ **`core/scheduler.py` is untouched and must stay so.** That pool bounds *turns*,
> whose queue outlives any graph; this one bounds the nodes of a single run and dies
> with it.

---

## 🗂 Workflows

A multi-step plan expressed as a task graph — and **not a second execution engine**.
Every node is an `agent_tasks` row and every run is one `exec_workflows` row, so the
checkpoints, the heartbeat, `/recovery` and the crash scan a workflow gets are the ones
that already existed. It needed **no migration**.

| | |
|---|---|
| **Files** | `.agent2/workflows/*.yaml` (or `.json`), next to the skills a user already keeps there |
| **CLI** | `/workflow` · `list` · `new` · `edit` · `run` · `auto` · `delete` · `show` · `state` · `reload` |
| **HTTP** | `GET/POST /api/workflows` · `GET/DELETE /api/workflows/<name>` · `POST /api/workflows/<name>/run` · `POST /api/workflows/auto` |
| **Ceilings** | `WORKFLOW_MAX_NODES = 64` · `WORKFLOW_STATE_CHARS = 1200` · `MAX_FILES = 64` · `MAX_BYTES = 131072` · `BUDGET_SEC = 2.0` |

- **Bare `/workflow` executes nothing.** `run` is the one verb that starts anything,
  and it Validates → Builds the DAG → **shows the plan** → then runs.
- **The filename is the name.** A disagreeing `name:` line is *reported* and the
  filename wins, because two files may claim one name and then `/workflow run x` has no
  answer.
- **PyYAML is not a dependency**, so the readable subset is *declared* (`SUBSET`) and
  anything outside it — an anchor, a tag, a flow mapping — lands in `notes` as
  unsupported, counted, never approximated.
- **An older schema still runs.** `UPGRADES` holds one step per version and `upgrade()`
  walks the ladder; the result carries `upgraded_from` plus a warning, so a human can
  see their file was read as something slightly different from what they wrote.

> ⚠️ **Progress is derived from the rows, never read out of the run.**
> `exec_workflows.state` is a breadcrumb, recomputed on every read — so a killed run
> reports what is true *now* rather than what was true when it died, and a resume never
> re-runs a completed node.

> ⚠️ **`WORKFLOW_MAX_NODES` refuses, never truncates.** A graph missing its last node is
> a graph whose dependencies no longer close, and it would deadlock `tasks.ready()` in
> silence. Same for a truncated *file*: the tail of a YAML document is where the last
> node's `needs:` lives.

> ⚠️ **The turn gets the current node, not the plan.** `for_turn()` inlines exactly one
> instruction and *names* at most a few others — a worker gets only what it needs. And
> it is **flat, not merely cheap**: a 24-node graph costs the same three queries as a
> 3-node one, because a per-node read is invisible at the size a developer tests with
> and 64 round trips per turn at the size a user writes.

### Dynamic workflows — a goal sentence becomes a graph

`/workflow auto <goal>` · `POST /api/workflows/auto`. The one module in the family whose
input is somebody else's text, so most of it is boundary rather than feature.

- **PLAN is the default and PLAN writes nothing at all** — no `exec_workflows` row, no
  `agent_tasks` row. `auto` must be asked for **by name**, and an unrecognised mode word
  is *refused* rather than coerced.
- **The planner decides *what*; the DAG decides *structure*** — `ast`-asserted, as a
  **call** ban on `levels_for` / `find_cycles` / `validate` / `plan_next` / `ready` /
  `dispatchable` and the rest.
- Its whole structural opinion is that every `needs` must point **backwards in
  declaration order**, which buys three of the validator's problems at once and is what
  licenses `DYNAMIC_MAX_STEPS` to **clip a suffix** instead of refusing the plan.
- **Rounds are read off the row**, never counted in memory — dual mode is two processes
  over one DB, so a counter hands each a full allowance.

> ⚠️ **Two buttons in the browser, never one.** `Plan` writes nothing; `Start it`
> appears **only after a plan has been drawn**. A panel that shipped one button would
> create task rows for anybody who pressed Enter in a text field.

---

## 🤖 UltraCode — the adaptive loop

UNDERSTAND → INSPECT → DISCOVER SKILLS → PLAN → EXECUTE → OBSERVE → ANALYZE → VERIFY →
(pass ⇒ continue | fail ⇒ RE-PLAN → EXECUTE). The **fourth consumer** of the one DAG,
not a fifth engine — so again **no new table and no migration**.

| | |
|---|---|
| **Doors** | **Two, and only two** — `/ultracode` in the terminal and `POST /api/ultracode`. Asserted structurally, because this loop writes code |
| **CLI verbs** | `start <goal>` · `run` · `approve` · `state` · `cancel` · `policy` |
| **HTTP actions** | `start` · `approve` · `cancel` (`GET /api/ultracode` is the read half) |
| **Node kinds** | **4** — `build` · `check` · `approval` · `fix`. The DAG *stores* a kind and never branches on it |
| **Ceilings** | `ULTRACODE_MAX_CYCLES = 6` · `ULTRACODE_BUDGET_SEC = 0` (off) · approval **on** by default |

- **Bare `/ultracode` executes nothing**, and an unknown first word is **refused, never
  read as a goal**: `/ultracode fix the login bug` is indistinguishable from a verb this
  build lacks, and guessing turns a typo into a planner call and a graph of task rows.
- **Verification is a node.** A check that ran outside the graph could not hold work
  back, so a downstream node would proceed while it was still pending — and it asks
  `core/verify.py`, never a model, because a model grading its own output is the failure
  the rule names.
- **Approval is a node too** — created and immediately PAUSED, with the roots depending
  on it. `tasks.pause()` writes no stop checkpoint, which is the only thing separating
  *a human chose to hold this* from *a crash abandoned it*, so the hold survives a crash
  and recovery can never helpfully undo it.
- **Recovery happens first and unasked, and the released nodes are named** — a silent
  release is indistinguishable from a node that was never stuck.
- **Thirteen refusal words**, a fifth closed vocabulary sharing no string with the
  scheduler's hold codes, its run reasons, the skills selector's, the planner's, the
  classifier's or the verifier's.

> ⚠️ **The stage is derived from the rows and never stored.**
> `execstate.workflow_step()` *replaces* `exec_workflows.state` on every node
> transition, so a stage written there would be gone by the next node. Deriving is also
> what lets a run killed mid-flight report the stage that is true **now**, with no stale
> copy for a resume to reconcile.

> ⚠️ **"Cycles", deliberately not "rounds".** A cycle that fixes something *without*
> asking a planner for new nodes spends a cycle and **no round at all** — twenty of
> those sit far under every mutation ceiling while being exactly the runaway they exist
> to stop.

> ⚠️ **One clock, checked between cycles.** `ULTRACODE_BUDGET_SEC` is never handed to
> the pump: the pump owns `AGENT2_DAG_NODE_TIMEOUT`, and two clocks over one node is two
> answers. A long node overruns and is *reported* at the boundary rather than killed
> mid-write.

> ⚠️ **A successful `cancel` answers `ok: false`.** `ok` asks *did this run do its job*,
> and a cancelled one did not — the cancel took effect when `reason` is `cancelled`.
> Likewise `Finish.ok` is **not** `report.verified`: a run of pure reasoning nodes is
> legitimately unverifiable and must still be allowed to finish.

---

## ✅ Verification — "Done" is not verification

One question, for anything that runs: *something said it finished — does the durable
record agree?* `agent_tasks.status` is written **by the thing being judged**, so a
second opinion at a call site is not a second opinion, it is the same claim repeated.

| Verdict | Means |
|---|---|
| `open` | Not settled. Nothing has been claimed yet |
| `unsuccessful` | Settled, and says so |
| `confirmed` | Claims success, and the record agrees |
| `contradicted` | Claims success, and the record **disagrees** |
| `unconfirmed` | Claims success, and nothing was recorded either way |

Five checks, each off a different record: the claim itself, the checkpoint view,
`exec_commands` exit codes, `exec_tool_calls` failures (and `post is null`, the crash
signal), and the `pre`/`post` digest pairs inside those rows.

- **It verifies a task row**, which is why it knows nothing about workflows — it imports
  neither `core.dag` nor `core.workflow`. A workflow node, a dynamic-workflow node and
  an UltraCode node are one shape here.
- **Problems alone decide; warnings never do.** An *unconfirmed* unit is a warning:
  promoting it would make a node whose whole job was to read and reason
  indistinguishable from one that failed, and that false negative would fire on every
  run.
- **Two queries per report at any node count.** A per-node read is 128 round trips at
  sixty-four nodes.

> ⚠️ **Read-only, and it does not re-stat disk.** The question is *did it happen*, not
> *is it still there*: a file a later step legitimately replaced would read as a
> contradiction. The digests were recorded at the moment of the call; this compares what
> was recorded and never takes a second reading.

> ⚠️ **There is deliberately no off switch.** Verification off does not make Agent-2
> quieter, it makes it **credulous** — every claim would read as confirmed. The only
> knob is `AGENT2_VERIFY_MAX_ROWS`, a *read* ceiling, and when it engages the report
> says `truncated`.

---

## 🎒 Skills

Instruction files a user (or another agent's toolchain) leaves in `.agent2/skills/`,
read on the turn path and selected **per request**. Four modules: `discovery.py` walks,
`normalize.py` translates, `select.py` ranks, `state.py` remembers — feeding the
broker's existing `skills` slot, so this added a collector *body*, not a source.

| | |
|---|---|
| **Manifests read** | **10** — `SKILL.md`, `SKILL.yaml/yml`, `AGENTS.md`, `AGENT.md`, `GEMINI.md`, `ANTIGRAVITY.md`, `agent2.md`, `INSTRUCTIONS.md`, `PROMPT.md` |
| **Selection tiers** | **5**, index *is* rank — **request** (the message names it) → **project** (`agent2.md` names it) → **enabled** → **relevant** (declared keywords match) → **general** (`always: true`) |
| **Ceilings** | `SKILLS_MAX = 64` · `MAX_DEPTH = 6` · `MAX_BYTES = 65536` · `BUDGET_SEC = 2.0` · `IN_PROMPT = 4` · `MAX_CHARS = 6000` |
| **Surfaces** | `/skills` (+ `list` · `on` · `off` · `reset` · `show` · `last` · `reload`) · `GET/PUT /api/skills` · the browser's Skills panel |

- **Not every skill in every prompt** — that is the acceptance bar and the reason the
  selector exists. Ten discovered skills and an unrelated message put *nothing* in the
  prompt.
- `priority` orders **within** a tier and may never promote past one: a pinned
  house-style skill does not outrank the skill the user just asked for.
- **Every omission carries a `why`** — `disabled` · `shadowed` · `cap` · `chars` ·
  `empty`. A skill in the folder and absent from the prompt with no stated reason is
  indistinguishable from a broken walk.
- Parsing is **stdlib only**, so a header shape this reader cannot handle is *counted*
  in `unparsed` rather than guessed at, and an unrecognised field is kept in `extra`
  rather than dropped.

> ⚠️ **No write API reaches this package.** Every `open()` in all four modules is
> asserted to carry an explicit read-only mode, and `write_text` / `write_bytes` /
> `mkdir` / `shutil` / `unlink` / `rename` are absent from the code — because a skill is
> frequently somebody else's file in somebody else's repository. Enablement is therefore
> a **database row**, scoped to this project.

> ⚠️ **Three states, and the third is the point.** `None` (never chosen) still allows
> automatic selection; `False` beats every signal *including the request naming it*.
> So `/skills` is a **block list, not a force list**: ON means "not `False`", and
> switching something back on restores *automatic* rather than pinning it into every
> prompt.

> ⚠️ **The vendor is a label, never a dispatch.** `origin` is reported and never
> branched on — asserted as *no module outside `normalize.py` names a vendor*, which is
> the difference between a normalization layer and a per-vendor plugin.

---

## 🩺 Health and 📈 Metrics

Two surfaces, deliberately two commands: folding them into one would put a percentile
next to a fault.

| | Health | Metrics |
|---|---|---|
| **Route** | `GET /api/health` (`200` / `503`) | `GET /api/metrics` |
| **CLI** | `/health` | `/metrics [reset]` |
| **Shape** | **14** sections, **16** verdict rows | **13** declared signals |
| **Scope** | Install-wide counters | **Per process**, and `scope` says so |

Health's fourteen sections — Agent · Database · Connection Pool · WAL Checkpointer ·
Scheduler · Task Queue · Command Executor · Sync Layer · Crash Recovery · MCP · Memory ·
Context Broker · Model Providers · Permissions — are each a **projection** of the reader
that already owns the fact, never a re-test.

Metrics' thirteen signals: `llm.latency`, `llm.tokens`, `llm.errors`, `tool.latency`,
`tool.failures`, `command.duration`, `queue.wait`, `task.duration`,
`workflow.duration`, `memory.retrieval`, `context.size`, `mcp.latency`,
`permission.denials`.

> ⚠️ **16 rows from 14 sections is not drift.** `_rows()` expands `mcp` into one row per
> registered server and `providers` into Gemini + Custom, *inside* the `SECTIONS` loop —
> so there is still one declaration of which subsystems a health read covers, and a
> renderer that hard-coded 14 would silently drop a server.

> ⚠️ **`problems` alone decides `ok` and the 503; `warnings` may never influence
> either.** Every supported configuration that trips the 503 spends the meaning of the
> 503. And ⚠️ **`off` is not a lesser `warn`** — the WAL checkpointer, the scheduler and
> both MCP bridges can be off *on purpose*, and a cross printed at a deliberate choice
> is how an alert stops being read.

> ⚠️ **Three of the thirteen signals are borrowed, not measured.** LLM latency and
> errors belong to the router (durable and install-wide) and permission denials to
> `core.permissions`; the report *forwards* them, and recording into one is a **counted
> no-op** — the guard that stops a later phase adding a second, drifting copy.

> ⚠️ **Cardinality is capped, per signal.** Past `MAX_SERIES` a new label folds into
> `~other`; an unrecognised *name* is a different fact and gets `~unknown`. Labels are
> created on first observation, so admitting model-supplied text would let junk names
> fold the real tools into `~other` for the life of the process — a measurement
> destroyed by what it measures.

> ⚠️ **A series holds numbers and one enum-ish label — no content, ever.** Neither
> payload carries key material, chat or memory text, command lines or paths.

---

## 🎛 The graph family's knobs

Every ceiling above is an environment variable, and each one's *direction* is chosen
rather than defaulted.

| Var | Default | Effect |
|---|---|---|
| `AGENT2_DAG_MAX_NODES` | `512` | Nodes one graph may declare, any consumer. **Refused, never truncated**; a consumer's own ceiling is injected, not replaced |
| `AGENT2_DAG_MAX_MUTATIONS` | `64` | Nodes a **live** graph may gain. `0` is supported — *plan once and never invent more work* |
| `AGENT2_DAG_MAX_WORKERS` | `4` | Pump threads. `0` runs nodes inline, one at a time |
| `AGENT2_DAG_MAX_RUNNING` | `8` | Nodes in flight, any kind. A **second** number from workers: in-flight is derived from the rows, so it counts another process's claims too |
| `AGENT2_DAG_MAX_COMMANDS` | `2` | Shell nodes together. Small on purpose — eight racing for one terminal is how output becomes unreadable |
| `AGENT2_DAG_MAX_MCP_CALLS` | `2` | Burp and ZAP are single instances behind one HTTP session each |
| `AGENT2_DAG_MAX_MODEL_CALLS` | `200` | **Lifetime** spend of one run on model-backed nodes, charged from `attempt_count` |
| `AGENT2_DAG_MAX_ATTEMPTS` | `2` | Times one node may be *started*. Bounded **and** classified |
| `AGENT2_DAG_NODE_TIMEOUT` | `0` (off) | Ceiling on one node. Off by default — a node may legitimately be a 40-minute build |
| `AGENT2_WORKFLOWS` | `1` | Master switch. `0` ⇒ no run may be instantiated and the context source collects nothing |
| `AGENT2_WORKFLOW_MAX_NODES` | `64` | Nodes one workflow may declare (floor **2**) |
| `AGENT2_WORKFLOW_STATE_CHARS` | `1200` | What the `workflow_state` block may spend. The other nodes are **named, never inlined** |
| `AGENT2_WORKFLOW_MAX_FILES` | `64` | Files one discovery pass reads |
| `AGENT2_WORKFLOW_MAX_BYTES` | `131072` | Bytes from **one** file. A truncated declaration is *refused* |
| `AGENT2_WORKFLOW_BUDGET_SEC` | `2.0` | Wall-clock on one discovery pass |
| `AGENT2_DYNAMIC_WORKFLOW` | `1` | Master switch for the **planner** only. It does not disable workflow *files* |
| `AGENT2_DYNAMIC_MAX_STEPS` | `24` | Steps one generated plan may contain. **Clipped and reported**, the opposite of the file ceiling |
| `AGENT2_DYNAMIC_MAX_ROUNDS` | `3` | Times one run may be re-planned. Read off the row, never counted in memory |
| `AGENT2_ULTRACODE` | `1` | Master switch for the autonomous driver. It does not disable workflows or their planner |
| `AGENT2_ULTRACODE_MAX_CYCLES` | `6` | Execute→verify→re-plan cycles. **Cycles, not rounds** |
| `AGENT2_ULTRACODE_BUDGET_SEC` | `0` (off) | Wall-clock for one run. Checked *between* cycles and reported, never enforced mid-write |
| `AGENT2_ULTRACODE_APPROVAL` | `1` | Whether a human must release the run. **On by default**, and the gate is a DAG node |
| `AGENT2_VERIFY_MAX_ROWS` | `500` | Rows one verification read may consult, per ledger. **The only knob verification has** |
| `AGENT2_SKILLS` | `1` | Master switch for `.agent2/skills/` |
| `AGENT2_SKILLS_MAX` | `64` | Skills discovered before the walk stops (floor **4**) |
| `AGENT2_SKILLS_MAX_DEPTH` | `6` | Levels below `.agent2/skills/` |
| `AGENT2_SKILLS_MAX_BYTES` | `65536` | Bytes from one skill file. Half a skill beats a refusal nobody can see |
| `AGENT2_SKILLS_BUDGET_SEC` | `2.0` | Wall-clock on one discovery pass |
| `AGENT2_SKILLS_IN_PROMPT` | `4` | **Skills that may reach one prompt** — the number the bar is measured against |
| `AGENT2_SKILLS_MAX_CHARS` | `6000` | Characters the whole skills block may spend |
| `AGENT2_RESUME` | `off` | Whether a launch continues the last conversation **by itself**. `last` opts in |
| `AGENT2_METRICS` | `1` | `0` makes every entry point a single boolean test |
| `AGENT2_METRICS_SAMPLES` | `128` | Samples kept per series. A ring buffer — percentiles are computed at *read* time |
| `AGENT2_METRICS_MAX_SERIES` | `64` | Labels a signal may have before folding. **Per signal**, so one chatty name cannot starve the other twelve |

> ⚠️ **A master switch means *indistinguishable from never written*, not *quieter*.**
> `AGENT2_SKILLS=0`, `AGENT2_WORKFLOWS=0`, `AGENT2_DYNAMIC_WORKFLOW=0`,
> `AGENT2_ULTRACODE=0` and `AGENT2_METRICS=0` all meet that bar. There is deliberately
> no `AGENT2_VERIFY=0`, for the reason stated above.

> ⚠️ **`AGENT2_RESUME` defaults to `off`, and the reversal was deliberate.** This
> shipped as `last`, and an unasked resume is not free: saving history DELETEs a chat's
> rows and re-INSERTs the window, so the first turn of a session that continued *by
> accident* rewrites a transcript the user never meant to open. `/load`, `--continue`,
> `/resume` and the browser's `load` palette command are the four ways in.

---

## 💾 Database

**27 tables** across **32 migrations**, in one `agent2.db`. All state lives here —
never in a `.env`.

`agent_tasks` · `api_keys` · `chats` · `exec_commands` · `exec_recovery` ·
`exec_tool_calls` · `exec_workflows` · `file_ops` · `key_usage` · `mcp_config` ·
`mcp_state` · `memories` · `messages` · `model_attempts` · `model_caps` ·
`pil_ngrams` · `pil_phrases` · `pil_prefs` · `pil_vocab` · `providers` · `rules` ·
`schema_migrations` · `settings` · `skill_state` · `sync_state` · `task_sessions` ·
`web_sessions`.

⚠️ **Twenty-six of those are declared in `database.py` and one is not.** `providers`
is created by `agent2/llm/providers.py` on first use, which is why migration 5
exists — so the honest count is `database.py` **∪** `llm/providers.py`, and a count
of one file alone is short by one and looks derived.

| | |
|---|---|
| **One access layer** | `qall` / `qone` / `exe` / `exemany` / `batch()`. Nothing else may `import sqlite3` — that is the one place a query can be wrong |
| **An index on an existing table needs BOTH halves** | The table's own DDL **and** a `_MIGRATIONS` step. ⚠️ `_create_table` returns early when the table is already there, so a DDL-only index reaches **fresh databases only** — and the install with a year of ledger rows is the one that needed it. The four exec ledgers were asymmetric this way until migrations 30–32 |
| **Pooling** | `AGENT2_DB_POOL = 8` (floor 4), idle connections retired after `AGENT2_DB_POOL_IDLE = 300` s; `0` keeps every one warm |
| **WAL** | Checkpointed every `AGENT2_WAL_CHECKPOINT_SEC = 60` s, skipping a `-wal` smaller than 2 MiB; `0` disables the thread |
| **Relocatable** | `AGENT2_DB` moves the database (and, by default, the log folder with it) |
| **Project keys are normcased** | ⚠️ In **one** function. A raw path is not normcased, so a row written from `C:\…` is never found again by a read for `c:\…` — migration 10 exists because of exactly this |

---

## 📜 Logging and audit

All logs in **one folder** — `logs/`, next to the database, so a container with
`AGENT2_DB` on a volume keeps its logs there too.

- `agent2` → `logs/agent2.log` — the **audit** file, rotating 5 MB × 3.
- `a2web` → `logs/agent2-web.log` — the web console.
- ⚠️ Never point the web logger at the `agent2` namespace: replacing that logger's
  handlers silently kills the audit file.
- **31 named event kinds**, one helper each. ⚠️ The helper you *call* and the string
  the log *holds* are not spelled the same — grep for the right-hand column. This
  table is a **partial list**; the recovery, verification, exec-ledger, project and
  skills families follow the same rule:

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

  Plus a generic `event()` / `exception()` pair, used in production code with **four**
  further dotted names — `agent.model_fallback`, `agent.parallel_calls_deferred`,
  `agent.part_skipped`, `scheduler.error` — for **35** distinct kind strings in total.
  ⚠️ Count them by walking the helpers' own `event("…")` arguments plus a grep for
  direct `alog.event("…")` calls in production code. A bare grep over the whole tree
  totals **36**, because it also matches a `'test'` string from the suite — which is
  exactly how a wrong number that *looks* derived gets quoted.
- `AGENT2_LOG_LEVEL`, `AGENT2_LOG_COLOR` and `AGENT2_LOG_CONSOLE` tune the console
  half only.

---

## 🌿 Git awareness

`core/gitstate.py` is **the** `git` reader — the broker's Git source, the CLI status
bar and everything else go through it. Three subprocesses per snapshot
(`rev-parse --show-toplevel`, `status --porcelain=v1 -b`, `log -3`), TTL-cached for
**15 s**, each with a timeout.

> ⚠️ Every function is **total**: a missing binary, a non-repo directory, a timeout
> and a non-zero exit all mean the same thing to every caller — "no git information".
> A hung `git` must not hang a turn.
>
> ⚠️ The module is **read-only by design**. Writing is `run_command`'s job, so the
> permission gate and the diff viewer can both see it happen.
>
> ⚠️ A workspace switch must invalidate **both** this cache and the status bar's own
> serve-stale entry. Dropping only one is how a switch shows two different branches.

---

## 🐳 Docker

`agent2_docker.py` + `docker-entrypoint.sh`. `AGENT2_DB` redirects the database to a
mounted volume (`/data`); `docker-compose.yml` defines the `agent2-data` volume;
`BURP_MCP_URL` defaults to `http://host.docker.internal:9876`; set `SECRET_KEY` for
stable sessions.

`AGENT2_MODE` selects the entrypoint — `web` · `cli` · `dual` (`both` is a legacy
alias). The detached compose service stays `web`, because a background container has
no TTY to host a CLI; `agent2 cli` and `agent2 dual` run a one-off interactive
container, and `dual` brings the detached web half up first so both share the data
volume rather than fighting over the published port.

---

## 🧪 Tests and code quality

```bash
python -m pytest .github/tests/                 # 2670 tests
python -m pytest .github/tests/test_config.py   # one file
```

**1 602 tests** collect from **37** test modules in `.github/tests/`.

- Many are **sabotage-verified**: the test was proven to fail against a deliberate
  break before it was trusted to pass. A green suite is evidence only if failing was
  possible.
- ⚠️ Two **tautology traps** have been found and fixed here — assertions that
  compared a thing to itself and stayed green while the behaviour under them was
  deleted. That is why a test that looks trivial is suspect only *after* you read it.
- A few decisions are documented and deliberately **not** asserted, because no writer
  can produce the state that would distinguish them. Document the invariant; do not
  assert on an unreachable state.
- Every `⚠️` rule in this file is stated in full — with the bug it prevents — in the
  **module docstring** of the file that owns it, and pinned by a named test.

---

## 🚫 What is *not* implemented

Stated plainly, because a feature described in the present tense costs a reader real
time before they find out.

| | Status |
|---|---|
| **Agent-2-Pro** | A planned hosted edition. **Nothing** on the Pro page exists. Note that `pro` is also the name of the default *mode*, which is unrelated |
| **`/ultracode run` in the browser** | **Terminal-only, and the panel says so rather than hiding it.** `engine.drive()` needs a synchronous worker owning a whole model turn, and a request thread has neither the `sid` nor the stream — so the browser's *next ordinary chat turn* does that work through `workflow.for_turn()` instead |
| **`/workflow edit` in the browser** | Terminal-only for the same class of reason: `[E]dit` launches `$VISUAL`/`$EDITOR` on the machine running the terminal, and a browser tab may be on another machine. The web half edits by POSTing a `body` |
| **`work`, `replan`, `finalize`** | Verbs on **neither** surface. `engine.drive()` owns all three *and the order they run in*, which is the whole of the adaptive loop |
| **`/shrink`, `/clearhistory` over HTTP** | No route at all. Inventing one to give the browser a button would be a second declaration of what shrinking a conversation means |
| **`/mcp auto`** | Not a command. Auto-connect is set implicitly (see above) |
| **PIL ghost text on the web** | `POST /api/pil/predict` and the `pil_predict` event both work, but no shipped front end emits either — the CLI is currently the only surface with ghost text |
| **`file_ops` read-back** | Every operation is written; nothing reads the table back, so it is an SQL-only audit trail |
| **Diff blind spots** | A file written by `run_command` or by File Intelligence produces no diff, and a wholly-failed `multi_edit_files` still renders an empty one |

---

## 📚 Where the rules live

| Read this | For |
|---|---|
| [`README.md`](README.md) | The short tour and the quick start |
| [`USAGE.md`](USAGE.md) | Driving it — commands, flows, recipes |
| [`CLAUDE.md`](CLAUDE.md) | The index of every invariant and the file that owns it |
| [**agent2.is-best.net/docs**](https://agent2.is-best.net/docs/) | 45 pages, each rule with the bug it prevents |
| The module docstring | The full rationale. **Read it before editing a `⚠️` module** |

---

<div align="center">

Built by **Aarav Shah**, **Rudra Marathe** &amp; **Naitik Soni** · MIT License

</div>


