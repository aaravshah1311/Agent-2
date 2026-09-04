<!--
Author: Aarav Shah
Portfolio: aaravshah1311.is-great.net
github: github.com/aaravshah1311
-->

<h1 align="center">⚡ Agent-2</h1>

<h2 align="center">Stay Tuned, Massive, Powerfull Update Comming Soon</h2>

<p align="center">
  <em>A self-hosted autonomous AI agent powered by Google Gemini —<br>
  coding assistant, terminal agent, security tester and persistent memory in one interface.</em>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.9+-3776AB?style=for-the-badge&logo=python&logoColor=white" />
  <img src="https://img.shields.io/badge/Flask-Web_UI-000000?style=for-the-badge&logo=flask&logoColor=white" />
  <img src="https://img.shields.io/badge/Gemini-6_models-4285F4?style=for-the-badge&logo=google&logoColor=white" />
  <img src="https://img.shields.io/badge/SQLite-single_file_state-003B57?style=for-the-badge&logo=sqlite&logoColor=white" />
  <img src="https://img.shields.io/badge/Tests-1602-3ddc84?style=for-the-badge&logo=pytest&logoColor=white" />
</p>

<p align="center">
  <a href="#-what-it-is">What it is</a> •
  <a href="#-quick-start">Quick start</a> •
  <a href="#-three-surfaces-one-brain">Surfaces</a> •
  <a href="#-the-17-agent-tools">Tools</a> •
  <a href="#-models-and-modes">Models</a> •
  <a href="#-subsystems">Subsystems</a> •
  <a href="#-security-posture">Security</a> •
  <a href="#-configuration">Config</a> •
  <a href="#-architecture">Architecture</a> •
  <a href="#-tests">Tests</a> •
  <a href="#-documentation">Docs</a>
</p>

---

## 🚀 What it is

Agent-2 runs on your machine, talks to Google Gemini with your own API keys, and keeps
every byte of state in one `agent2.db` beside the code. No account, no service, no telemetry,
no build step.

It ships in three surfaces that are **not three applications**. They share the system prompt,
the tool dispatcher, the memory and rules tables, the diff engine, the command watchdog, the
model router and the database. Switching from the terminal to a browser tab changes what you
look at and nothing about what the agent can do.

| Surface | Entry point | What it is |
|---------|-------------|------------|
| ⚡ **CLI** *(default)* | `agent2cli.py` | Rich + prompt_toolkit terminal UI — ephemeral menus, inline diffs, a full-screen diff viewer, a live command watchdog |
| 🌐 **Web UI** | `agent2web.py` | Flask + Socket.IO at `http://localhost:1311` — streaming tool calls, multi-tab terminals, memory/rules/keys/MCP panels |
| 🔀 **Dual** | `agent2dual.py` | Web server on a background thread, CLI in the foreground. One process, one `agent2.db`, both surfaces live |

---

## ⚡ Quick start

```bash
git clone https://github.com/aaravshah1311/Agent-2.git
cd Agent-2
python run.py                # creates .venv, installs deps, prompts for a Gemini API key
```

`run.py` is the universal launcher **and the package manifest** — the dependency tables live
there and nowhere else.

```bash
python run.py --cli          # CLI (default)
python run.py --web          # web server
python run.py --dual         # both at once
python run.py --addapi       # manage Gemini API keys
python run.py --update       # pull latest (preserves agent2.db)
python run.py --reset        # wipe .venv and reinstall
python run.py --uninstall    # remove venv, DB, global command
python run.py --docker       # Docker runtime + global `agent2` command
```

After the first run a global `agent2` command exists: `agent2` (CLI), `agent2 web`,
`agent2 dual`, plus the Docker subcommands `stop | restart | logs | status | update |
uninstall`.

Direct entry, skipping the launcher: `.venv/bin/python agent2cli.py`
(Windows: `.venv\Scripts\python agent2cli.py`).

**Requires Python 3.9+.** Get a free Gemini API key at
[aistudio.google.com/apikey](https://aistudio.google.com/apikey).

---

## 🧠 Three surfaces, one brain

Everything below is shared. That is the point of the architecture, and it is enforced by a
rule the codebase applies everywhere: **each fact has exactly one home**, because a second
copy drifts silently — each surface still looks right on its own.

| Thing | Its one home |
|-------|--------------|
| Models, modes, shell detection | `config.py` |
| API key rotation | `llm.keys.rotator` |
| SQLite access | `database.py` (`qall` / `qone` / `exe` / `exemany` / `batch()`) |
| `memories` writes | `core.memory` |
| Diff computation and totals | `core/diffs.py` |
| An ephemeral CLI menu | `cli/palette.py` |
| Whether a web request may proceed | `server/auth.decide()` |
| Whether an identity may do X | `core.permissions` |
| What a model can do | `llm/capabilities.get()` |
| Which model runs a turn | `llm/router.rank_candidates()` |
| Which MCP servers exist | `integrations/registry.py` |
| What reaches the model, in what order | `core.broker.ORDER` |

---

## 🛠 The 17 agent tools

**Core (12)** — `run_command`, `read_file`, `write_file`, `multi_edit_files`, `list_dir`,
`grep_search`, `delete_file`, `scan_project`, `web_search`, `update_todo`, `save_memory`,
`emit_plan`

**File Intelligence (5)** — `detect_file`, `file_capabilities`, `run_file_op`,
`convert_file`, `search_workspace` — router-dispatched shims into `agent2.fileintel`, so
backends are chosen internally.

Sixteen of them go through `tools.dispatch_tool`. `run_command` alone goes through
`terminal.stream_command`, which is why the exec capability gate is implemented twice on
purpose — and why the CLI's copy sits *outside* the retry loop: a refusal must not become an
allow on retry.

When an MCP bridge is connected its tools are added dynamically and force-prefixed
(`burp_`, `zap_`). Ownership is **membership, not prefix**: `registry.resolve()` asks each
bridge whether it actually holds a name, so a disconnected ZAP stops claiming `zap_*` instead
of swallowing the call.

> The tool capability gate returns a tool **`error`, never an exception**. The model reads
> errors and adapts; an exception would end the turn.

---

## 🤖 Models and modes

| Key | API id | Group |
|-----|--------|-------|
| `2.5-flash` *(default)* | `gemini-2.5-flash` | `2.5` |
| `2.5-flash-lite` | `gemini-2.5-flash-lite` | `2.5` |
| `3.5-flash` | `gemini-3.5-flash` | `3.5` |
| `3.5-flash-lite` | `gemini-3.5-flash-lite` | `3.5` |
| `3.6-flash` | `gemini-3.6-flash` | `3.6` |
| `3.7-flash` | `gemini-3.7-flash` | `3.7` |

| Mode | Max output tokens | Thinking budget |
|------|------------------|-----------------|
| `fast` | 2 048 | — |
| `pro` *(default)* | 8 192 | — |
| `thinking` | 16 384 | 8 000 |

`config.MODELS` and `config.MODES` are the **only** declaration of either, and two tests
guard each direction.

⚠️ **`THINKING_GROUPS` is the empty tuple, so every group supports thinking.**
`supports_thinking()` reads `True if not THINKING_GROUPS else group in THINKING_GROUPS` —
empty means *no group is excluded*, not *no group qualifies*. The `thinking` mode description
string still names specific groups; that string is the stale half. Never re-derive the answer
from a model key: the predicate takes a **group** precisely so a new model joins an existing
group and inherits the answer for free.

**Routing is opt-in and explicit selection always wins.** Both surfaces send a model key every
turn, so a router that ran unasked could not tell a choice from a default. `auto` is a
user-selectable pseudo-key — deliberately *not* in `config.MODELS`, which is the list of things
that can be called. `AGENT2_MODEL_ROUTING` widens it to `default_only` or `always`.

A mid-turn failure is a `continue` in the existing loop, not a restart: context, tokens, tool
context, checkpoints and the cancel token all survive, because restarting would replay
completed tool calls. The hop budget is **per turn**, not per model.

### Custom providers

Any OpenAI- or Anthropic-compatible endpoint (base URL + key + model id + format) appears in
the selector as `custom:<id>`. `llm/provider_agent.py` mirrors `agent.py` exactly — same tools,
same Socket.IO events, stdlib `urllib` only.

---

## 🧩 Subsystems

| Subsystem | What it does | Owner |
|-----------|--------------|-------|
| **Context broker** | Assembles what reaches the model each turn, in one declared order, with per-source caps. Memory and rules are in `ALWAYS` and can never be squeezed out | `core/broker/` |
| **Git awareness** | Branch, dirty state and repo facts, cached 15 s. Every function is total; a timed-out `git` yields an empty snapshot, never an exception | `core/gitstate.py` |
| **Memory & rules** | Persistent facts and standing instructions, injected into every system prompt through a versioned cache | `core/memory/`, `core/rules.py` |
| **Diffs** | Computed **before** the write. Green added, red removed, grey context, and yellow is the *paired* count | `core/diffs.py` |
| **The Ctrl+B viewer** | The whole session in one full-screen surface. Opening it writes nothing; `r` is the one destructive key and takes two presses | `cli/diffview.py` |
| **Ephemeral menus** | One `Application`, one `Window`, capped to the terminal and erased when done | `cli/palette.py` |
| **Command execution** | Three registries — handles, execution state, pipe I/O — plus a watchdog that reports and never kills by default | `core/commands.py`, `core/procio.py` |
| **Cancellation** | Ctrl+C stops the **command**, not the session. Record, then kill — that order is load-bearing | `cli/state.py`, `terminal.py` |
| **Tasks & recovery** | Checkpointed long work that never re-runs what already finished | `core/tasks.py`, `core/recovery.py` |
| **Scheduling** | Bounded worker pool. `submit()` returns `queued` / `disabled` / `rejected`, each with a required response | `core/scheduler.py` |
| **Cross-process sync** | `notify()` publishes in-process synchronously; `SyncPoller` republishes what another process changed | `core/sync.py` |
| **Personal Intelligence** | Fully offline prediction, opt-in grammar, opt-in prompt enrichment. No network, no retraining | `core/pil/` |
| **File Intelligence** | Detect, convert and operate on real file formats through one router and a plugin set | `fileintel/` |
| **MCP bridges** | Burp Suite (`:9876`, SSE) and OWASP ZAP (`:8282`, streamable HTTP probed before SSE). Both auto-connect **off** | `integrations/` |

### Personal Intelligence Layer

Three modules over one memory and one passive learning engine, entirely offline:

- **Prediction** — trie/prefix + n-gram + phrase ghost-text. Never calls an LLM.
- **Grammar** *(off by default)* — deterministic rules; masks technical spans, corrects,
  restores verbatim.
- **Improve** *(off by default)* — adds only preferences proven above a weight floor. Never
  invents requirements.

Learning is weight adjustment in SQLite, not model training: accept **+0.08**, ignore
**−0.03**, accept-then-delete **−0.20**. Both agent loops learn from the **original** text and
send the processed copy; history and display keep the original. `POST /api/pil/wipe` is the
forget-me path. Every function swallows errors and returns a safe default — PIL failure
degrades to *do nothing*.

---

## 🔐 Security posture

Agent-2 is built for security research, which puts two obligations on it: the tools have to be
genuinely capable, and the agent has to be genuinely constrained. Those are separate systems
with separate owners.

**Who may reach the web surface** — `server/auth.py`. One decision function, `decide()`,
installed as a single `before_request`, so an app that has the API has the guard. Modes come
from `AGENT2_WEB_AUTH`: `auto` (loopback trusted, everything else needs the token) · `always`
· `off`. The Socket.IO handshake does **not** pass through Flask's `before_request`, so
`sockets.on_connect` asks `socket_allowed()` itself — `run_raw_command` is a socket event, so
guarding `/api/*` alone would lock every reader and leave the shell open. Origin is checked on
unsafe methods *before* identity, because loopback trust cannot tell your own tab from a page
you happened to visit. The access token is memory-only; `web_sessions` stores SHA-256
**digests**, so a copied `agent2.db` grants nothing.

**What an identity may do** — `core/permissions.py`. Roles: `owner` (default) · `operator`
(may drive the agent, may not touch credentials or MCP endpoints) · `viewer` (read).
`AGENT2_WEB_ROLE` describes web clients; `AGENT2_DENY_CAPS` subtracts from **this process,
CLI included**. An unknown role falls back to `viewer`, never `owner`; an unmapped mutating
`/api/` route falls to `destructive`. Both fallbacks point at the strict end.

**How a credential is stored** — `core/secrets.py`. Credentials persist as `a2s:`
**references**, never values, in exactly three columns. Backends in order: OS keyring (only if
a probe round-trip actually succeeds) → AES-256-GCM or a stdlib BLAKE2b+HMAC construction →
announced plaintext. `resolve()` passes a non-reference through unchanged, so every legacy row
keeps working, and `seal()` reads back what it wrote rather than let a caller persist a
dangling reference. The master key defaults to `~/.agent2/secret.key`, **outside** the DB
directory — a key beside `agent2.db` travels with every copy of it. Honest scope: this defends
against exposure of the database *alone*, and `describe()` says so in its own payload.

**Masking is a constant.** `••••••••`, its length not derived from the secret. The old
`k[:6]…k[-4:]` form printed most of a ten-character ZAP key.

**There is deliberately no “skip TLS verification” switch.** Quietly trusting any certificate
on a security tool's control channel is exactly the silent downgrade this project refuses to
ship.

### Security testing

`run_command` supports the real toolchain — `nmap`, `nikto`, `gobuster`, `ffuf`, `sqlmap`,
`hydra`, `metasploit`, `searchsploit`, `theharvester`, `binwalk`, `strings`, `volatility`.
When Burp is connected, `burp_*` tools drive proxy history, Repeater, Intruder, Scanner and
the site map.

Both command kill ceilings default **off**, and only the stuck *report* is on at 20 seconds.
A security scan is supposed to sit silent for twenty minutes; a default timeout would kill a
legitimate `nmap -p-` mid-sweep and hand the model a truncated result it cannot recognise as
truncated.

---

## ⚙️ Configuration

All state is in `agent2.db` — never a `.env` file. `config.DB` is the one source of truth for
its path. Environment variables cover only the decisions that must be made before the database
is open; there are 61 of them, documented in full at
[**Environment variables**](https://agent2.is-best.net/docs/env/). The ones most people set:

| Variable | Default | Effect |
|----------|---------|--------|
| `AGENT2_DB` | project root | Where `agent2.db` lives (`/data` in Docker) |
| `AGENT2_LOG_DIR` | `<db dir>/logs` | Folder for **all** logs |
| `AGENT2_HOST` / `AGENT2_PORT` | `0.0.0.0` / `1311` | Bind address / *preferred* port — the server never hard-fails on a busy port, so read the port it reports |
| `AGENT2_WEB_AUTH` | `auto` | `auto` \| `always` \| `off`. Env only, because a stored “off” is a persistent silent downgrade |
| `AGENT2_WEB_TOKEN` | per-process | Access token for remote clients. **Never written to disk** |
| `AGENT2_WEB_ROLE` | `owner` | Capabilities for web clients: `owner` \| `operator` \| `viewer` |
| `AGENT2_DENY_CAPS` | — | Capabilities subtracted from this whole process, CLI included |
| `AGENT2_CMD_TIMEOUT` | `0` (off) | Execution ceiling, seconds. Off by design |
| `AGENT2_CMD_STUCK_SEC` | `20` | Silence after which a command is **reported** (never killed) |
| `AGENT2_MODEL_ROUTING` | — (⇒ off) | `off` \| `default_only` \| `always`. Empty by default so the stored setting can win |
| `AGENT2_SECRET_KEY_FILE` | `~/.agent2/secret.key` | Master key location, deliberately outside the DB directory |
| `BURP_MCP_URL` / `ZAP_MCP_URL` | `:9876` / `:8282` | MCP endpoints — the **fallback**; a stored `mcp_config` row wins |

Other limits worth knowing: `MAX_CTX_MESSAGES=40`, `MAX_TOOL_OUTPUT=6000`,
`MAX_AGENT_ITERS=80`, `AGENT2_MAX_FILE_SIZE` 100 MB.

---

## 🖥 CLI commands

Twenty-nine slash commands. The full table, with the grammars that have real depth, is at
[**Slash commands**](https://agent2.is-best.net/docs/cli-commands/). The families worth knowing here:

```
/mcp                                  ephemeral menu of every MCP server
/mcp <server> connect|disconnect|list|status|config
/mcp connect | /mcp disconnect        every server at once
/mcp health                           ✓/✗ per server, then [R]etry · [S]ettings

/workspace · /cd <dir>                where the agent is rooted
/tasks · /pause · /resume             background work, paused and picked up again
/offline                              which subsystems may reach the network
/history · /clearhistory              transcript · wipe it

/model [name|auto]                    switch model (auto = Agent-2 picks per turn)
/model routing [off|default_only|always]
/model caps [<model>] [field=value …] show or correct a capability record
/model rank [<model>|all] [force]     ask a model to classify an unrecognised id
/mode [fast|pro|thinking]

/settings (/config)                   one menu onto every settings area
/rules                                activate/deactivate injected rules
/keys · /addapi                       key status and usage · add a Gemini key
/theme · /color                       colour theme · accent colour
/memory · /addmem <text>              list memories · save one
/clear · /shrink · /clearhistory      clear screen · summarize · wipe history
/load                                 CLI only — the last conversation from THIS directory
/scan [path] · /run <cmd> · /read <file> · /search <query>
```

Auto-connect is **per project**, and it is set implicitly — `connect` / `disconnect` and the
bare `/mcp` menu write it through `set_auto_connect()`; there is no `/mcp auto` sub-command,
though `POST /api/mcp/<key>/auto` exists for the web surface.

`/mcp`'s parser contains **no literal server name** — it resolves through `registry.get()`,
so a third bridge appears in the grammar by being registered. `/mcp config` with no server
**asks** instead of picking one, because a guessed target would silently edit the wrong
server's endpoint. `/burp` is retired from the help table but still forwards to `/mcp burp …`
and prints where it went — a command is never silently removed.

---

## 🌐 API surface

- **67 HTTP endpoints** — 63 in `agent2/server/routes.py`, 4 in `agent2/server/auth.py`.
  Full reference: [**REST API**](https://agent2.is-best.net/docs/rest-api/).
- **29 server→client Socket.IO events, 10 client→server handlers.** Full reference:
  [**Socket.IO events**](https://agent2.is-best.net/docs/socket-api/).

Highlights: `GET /api/health` (aggregate, `200`/`503` + `problems`, counters only — and a
*disabled* MCP server is not a problem), `GET /api/mcp` (every server, never a credential),
`GET /api/commands`, `GET /api/sync`, `GET /api/models`, `GET /api/platform`.

`/api/burp*` is gone: the four bespoke Burp routes were removed once `/api/mcp` reached
parity. Do not add a per-server route set back — that is the drift `/api/mcp` exists to end.

---

## 🏗 Architecture

```
User message
  → server/routes.py | server/sockets.py
  → core/scheduler.submit()      bounded pool; direct thread when disabled,
                                 rejects a full backlog with a visible message
  → agent.py:run_agent()         (or llm/provider_agent.py for custom providers)
      → pil.learn_from_message() + pil.process_outgoing_prompt()
      → core/diffs.capture_for()    snapshot BEFORE a file-writing tool runs
      → tools.dispatch_tool()       local tools
      → terminal.stream_command()   run_command (shell)
      → integrations.registry       resolve()s every MCP tool
      → fileintel.EXECUTOR          detect / run / convert / search
  → Socket.IO stream → browser  /  Rich output → CLI
```

| Entry point | Role |
|-------------|------|
| `run.py` | Universal launcher — venv, deps, keys, update, Docker. **Also the package manifest** |
| `install.py` | Pipe-safe network installer; never interactive (`curl \| python`) |
| `agent2cli.py` | Rich/prompt_toolkit CLI *(default)* |
| `agent2web.py` | Flask + Socket.IO server |
| `agent2dual.py` | Web on a daemon thread, CLI as a foreground **child process** |
| `agent2_docker.py` | Docker runtime (+ `docker-entrypoint.sh`) |

The `agent2/` package: `server/` (browser surface), `cli/`, `llm/`, `integrations/`, `core/`,
`fileintel/`, plus top-level `config.py`, `database.py`, `agent.py`, `tools.py`, `terminal.py`.
The old flat paths (`agent2.keys`, `agent2.routes`, …) remain as shims resolving to the **same
module object and singletons** — `agent2.keys.rotator is agent2.llm.keys.rotator`.

Storage: **22 SQLite tables, 18 migrations**, WAL mode, a pooled connection layer, and a
checkpoint thread. Nothing outside `database.py` may `import sqlite3`.

Full map: [**Architecture**](https://agent2.is-best.net/docs/architecture/).

---

## 🧪 Tests

```bash
python -m pytest .github/tests/                 # 1602 tests
python -m pytest .github/tests/test_config.py   # single file
```

**1602 tests across 37 test modules**, all under `.github/tests/` (the root `test/` directory is empty).
Many are **sabotage-verified** — the test was proven to fail against a deliberate break, not
assumed to work. Treat a test that looks trivial as suspect only after reading it: two
*tautology traps* — assertions that compared a thing to itself and stayed green while the
behaviour was deleted — have been found and fixed in this repository.

Some decisions are documented but deliberately **not** asserted, because no writer can produce
the state that would distinguish them. Document the invariant; don't assert on an unreachable
state.

---

## 🐳 Docker

```bash
python run.py --docker      # build the image and install the global `agent2` command
agent2 status | logs | stop | restart | update | uninstall
```

`AGENT2_DB` redirects the database to a mounted volume (`/data`), and `AGENT2_LOG_DIR`
defaults beside the database so logs land on the same volume. `BURP_MCP_URL` defaults to
`http://host.docker.internal:9876` inside a container. Set `SECRET_KEY` for sessions that
survive a restart.

`AGENT2_MODE` selects the entrypoint: `web` | `cli` | `dual` (`both` is a legacy alias). The
detached compose service stays `web` — a background container has no TTY to host a CLI.
`agent2 cli` / `agent2 dual` run a one-off interactive container, and `dual` brings the
detached web half up first so both share the data volume rather than fighting over the
published port.

Because the secrets master key defaults **outside** the DB directory, a container wants
`AGENT2_SECRET_MASTER_KEY` (or a mounted key file) rather than the key travelling inside the
data volume with the ciphertext.

---

## 🩺 Troubleshooting

| Symptom | Cause |
|---------|-------|
| The web server never starts and prints nothing | On Windows, printing a banner before `reconfigure(encoding="utf-8", errors="replace")` raises `UnicodeEncodeError` on the box-drawing glyphs when stdout is redirected — and dies *before it binds a port* |
| It is not on port 1311 | `find_free_port()` prefers 1311 and falls back. Read the port the server reports |
| A remote browser gets 401/403 | `AGENT2_WEB_AUTH=auto` trusts loopback only. A 403 on an unsafe method is the Origin check, which runs *before* identity — behind a proxy set `AGENT2_WEB_ORIGINS` to the exact origin (`*` is dropped) |
| The browser can read but every action is refused | `AGENT2_WEB_ROLE`. An unknown value falls back to `viewer`, so a typo looks like a working lockdown |
| A command hangs forever | Both kill ceilings default off by design; only the stuck report is on. Use the stuck prompt, or set the ceilings deliberately |
| Ctrl+C seemed to kill my session | It does not — it stops the command. Two presses inside the double-tap window is the deliberate exit |
| A CLI change never shows up in the browser | Each long-lived surface must start its own `SyncPoller`; a resource missing from `RESOURCES` never crosses at all |
| Keys stopped working after copying `agent2.db` | The master key lives at `~/.agent2/secret.key`, outside the DB directory. That is the feature |
| Diffs missing for a file the agent changed | Two documented blind spots: `run_command` and `fileintel` writes produce no diff |

More, with the code behind each: [**Troubleshooting**](https://agent2.is-best.net/docs/troubleshoot/).

---

## 📚 Documentation

The full documentation is 39 pages at [**agent2.is-best.net/docs**](https://agent2.is-best.net/docs/) — served statically,
no build step. Every subsystem page states the rule **and the bug the rule prevents**.

| | |
|-|-|
| [What Agent-2 is](https://agent2.is-best.net/docs/overview/) · [Install](https://agent2.is-best.net/docs/install/) · [First session](https://agent2.is-best.net/docs/quickstart/) · [Docker](https://agent2.is-best.net/docs/docker/) | Start here |
| [API keys](https://agent2.is-best.net/docs/api-keys/) · [Models & modes](https://agent2.is-best.net/docs/models/) · [Capabilities](https://agent2.is-best.net/docs/model-capabilities/) · [Routing](https://agent2.is-best.net/docs/routing/) · [Providers](https://agent2.is-best.net/docs/providers/) · [Workspaces](https://agent2.is-best.net/docs/workspaces/) · [Environment](https://agent2.is-best.net/docs/env/) | Configure |
| [The CLI](https://agent2.is-best.net/docs/cli/) · [Menus](https://agent2.is-best.net/docs/menus/) · [Web UI](https://agent2.is-best.net/docs/web-ui/) · [Dual mode](https://agent2.is-best.net/docs/dual/) · [Diffs](https://agent2.is-best.net/docs/diffs/) | Surfaces |
| [The 17 tools](https://agent2.is-best.net/docs/tools/) · [Context broker](https://agent2.is-best.net/docs/context/) · [Git awareness](https://agent2.is-best.net/docs/git-state/) · [Memory & rules](https://agent2.is-best.net/docs/memory/) · [Tasks](https://agent2.is-best.net/docs/tasks/) · [Commands](https://agent2.is-best.net/docs/commands/) · [PIL](https://agent2.is-best.net/docs/pil/) · [File Intelligence](https://agent2.is-best.net/docs/fileintel/) | The agent |
| [Security testing](https://agent2.is-best.net/docs/security-testing/) · [Burp & ZAP](https://agent2.is-best.net/docs/mcp/) · [Web auth](https://agent2.is-best.net/docs/web-auth/) · [Capabilities](https://agent2.is-best.net/docs/authorization/) · [Secrets](https://agent2.is-best.net/docs/secrets/) | Security |
| [Slash commands](https://agent2.is-best.net/docs/cli-commands/) · [REST API](https://agent2.is-best.net/docs/rest-api/) · [Socket.IO](https://agent2.is-best.net/docs/socket-api/) · [Database](https://agent2.is-best.net/docs/database/) · [Sync](https://agent2.is-best.net/docs/sync/) · [Logging](https://agent2.is-best.net/docs/logging/) · [Architecture](https://agent2.is-best.net/docs/architecture/) · [Troubleshooting](https://agent2.is-best.net/docs/troubleshoot/) | Reference |

Also in this repository: [`FEATURES.md`](FEATURES.md) (what it can do, feature by feature),
[`USAGE.md`](USAGE.md) (how to drive it), [`CLAUDE.md`](CLAUDE.md) (the invariant index for
contributors), and [**agent2.is-best.net/innovation**](https://agent2.is-best.net/innovation/) (what is new here and why).

---

## 🧭 Agent-2-Pro

Agent-2-Pro is a planned hosted edition. **Nothing described on the Pro page is implemented** —
the shipping product is the self-hosted build this README documents. It is marked as such
wherever it appears, because a document that describes an unbuilt feature in the present tense
costs a reader real time before they discover it.

---

## 🤝 Contributing

Read [`CLAUDE.md`](CLAUDE.md) first. It is an **index of rules and where their rationale
lives** — each hard-won invariant is pinned by a test and explained at length in the module
docstring of the file that owns it. `⚠️` marks one that has already been regressed once.

**Before changing a `⚠️` module, read its docstring, then its test.** Several rules are
load-bearing in non-obvious ways, and a few exist specifically to block a bug that produces
*no error* — just wrong output. If a rule looks redundant, the docstring says why it is not.

Then: run the suite, add a test that fails against a deliberate break of your change, and keep
each fact in exactly one place.

---

<p align="center">
  <sub>Built by <a href="https://github.com/aaravshah1311">Aarav Shah</a> ·
  <a href="https://aaravshah1311.is-great.net">aaravshah1311.is-great.net</a></sub>
</p>
