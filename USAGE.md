<!--
Author: Aarav Shah
Portfolio: aaravshah1311.is-great.net
github: github.com/aaravshah1311
-->

# 📖 Agent-2 — Usage Guide

**Agent-2** is a self-hosted autonomous AI agent powered by Google Gemini.
You give it a task in plain language; it **writes files, runs commands, reads its
own errors, and fixes them until the work is done** — then tells you what it did.

The same brain runs in three surfaces, all sharing one `agent2.db`:

| Surface | Command | Best for |
|---------|---------|----------|
| ⚡ **CLI** *(default)* | `agent2` | Terminal-native work, scripting, security testing |
| 🌐 **Web UI** | `agent2 web` | Workspaces, multi-tab terminals, file attachments |
| 🔀 **Dual** | `agent2 dual` | Web + terminal at once, both live on the same data |

---

## 🚀 Install & first run

```bash
# One-line install (any OS)
curl -fsSL https://raw.githubusercontent.com/aaravshah1311/Agent-2/main/install.py | python3 -

# …or clone and launch
git clone https://github.com/aaravshah1311/Agent-2.git && cd Agent-2
python run.py
```

On first launch, add a Gemini API key (free from
[aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey)):

```bash
python run.py --addapi      # or type /addapi inside the CLI
```

Keys are saved to `agent2.db` and **auto-rotate** when one hits its quota — add a
couple so long sessions never stall.

**What setup installs.** `run.py` builds a `.venv` and installs *everything* the
app can reach — the core (google-genai, MCP), both interfaces (Flask/Socket.IO,
Rich/prompt_toolkit) and all ~15 File Intelligence backends (pypdf,
python-docx, openpyxl, python-pptx, Pillow, reportlab, markdown,
beautifulsoup4, charset-normalizer, py7zr, pytesseract, xlrd, odfpy, rarfile,
pdf2image, plus docx2pdf on Windows). Anything missing on a later launch is
installed then too, so you never have to `pip install` by hand.

The File Intelligence libraries are **optional by design**: if one fails to
build, setup warns and carries on, and only the feature needing it reports a
`pip install X` hint. A cosmetic wheel can never block Agent2 from starting.

A handful of features also want a **system** binary that pip cannot provide —
`tesseract` (OCR), `ffmpeg` (media), `libreoffice` (Office→PDF), `unrar`, and
`poppler` (PDF→image). The Python side of each is installed for you; install the
binary with your OS package manager if you need that path.

**Where the `agent2` command goes.** After the first run a global launcher is
written to **`~/.local/bin`** on every OS — `agent2` on macOS/Linux,
`agent2.bat` on Windows. That's the same directory pip and pipx use for
per-user scripts, so it's often already on your `PATH`; `run.py` adds it if not.
If you installed an older build, its launcher lived in `~/.agent2/bin` — setup
finds and rewrites that copy too, so a stale one can't shadow the new one.

---

## ⚡ Your first task

Launch the CLI and describe what you want in plain language:

```bash
agent2
```

```
Build a Flask REST API for a todo list in ./demo-api — SQLite storage,
full CRUD, input validation, and a pytest suite. Then install the deps,
run the tests, and fix anything that fails until they all pass.
```

The agent will:

1. **Plan** the work as a live checklist (`update_todo`)
2. **Write** every file to disk (`write_file`)
3. **Install & run** in your shell (`run_command`)
4. **Read any failing test, fix the cause** (`multi_edit_files`), and re-run
5. **Report** what it built and how to run it — only once it's verified green

That loop — plan → build → run → read the error → fix → verify — is the core of
how Agent2 works. You don't approve each step; it finishes the whole task.

**Useful mid-task:**

- Press `Ctrl+C` (CLI) or the stop button (Web) to cancel a run at any time.
- Edit any earlier message to re-run the agent from that point.

---

## 🧠 What it can do

| Capability | How to use it |
|------------|---------------|
| **Build whole projects** | Describe the project; it creates the structure, every file, installs deps, and runs it |
| **Edit existing code** | "Add pagination to the /users endpoint" — it explores, then patches precisely across files |
| **Run & debug** | "Run the tests and fix the failures" — it iterates until green |
| **Process any file** | Drop in a PDF / spreadsheet / image / archive: "detect that file, then summarize it" (69 formats, 331 operations) |
| **Search the web** | "Look up the latest X" — DuckDuckGo, no extra key needed |
| **Persistent memory** | "Remember I prefer TypeScript" — recalled in future sessions, ranked by importance |
| **Security testing** | "Port scan 10.0.0.5", "run sqlmap on …" — nmap, gobuster, sqlmap, metasploit, and more |
| **Burp & ZAP** | `/mcp burp connect` or `/mcp zap connect` → drive Proxy, Repeater, Intruder, Scanner and the site map from the agent |

---

## 🖥️ Choosing a surface

### CLI

```bash
agent2                      # start
```

Rich terminal UI with slash commands, `↑`/`↓` history, and offline ghost-text
suggestions as you type. Every CLI launch starts fresh — use `/load` to bring
back the last conversation from the current directory.

### Web UI

```bash
agent2 web                  # opens http://localhost:1311
```

Create a **workspace** (a project folder), then start chatting. You get multi-tab
live terminals, file attachments, message editing, and real-time streaming. If
port 1311 is busy, Agent2 auto-picks the next free one and prints it.

### Dual

```bash
agent2 dual                 # Web UI in the background + CLI here
```

Both surfaces share the same `agent2.db`, so a memory added in the browser shows
up in the CLI on its next turn, and vice versa.

---

## 🎛️ Models & modes

Switch anytime inside a session:

```
/model 3.5-flash     # 2.5-flash (default) · 2.5-flash-lite · 3.5-flash
                     # 3.5-flash-lite · 3.6-flash · 3.7-flash · auto
/mode  pro           # fast (2048 tokens) · pro (8192, default) · thinking (16384)
```

`thinking` works on **every** model group — it attaches an 8 000-token reasoning
budget. (The menu's own description still says "2.5/3.5 models only"; that string
is stale display copy, not the rule.)

`/model auto` hands the choice to the router, per turn. Routing is otherwise
**opt-in**: an explicit `/model` selection always wins, because both surfaces send
a model key every turn and a router that ran unasked could not tell your choice
from a default. Widen or narrow it with `/model routing [off|default_only|always]`.

If a model fails mid-turn, Agent2 hops to the next candidate **inside the same
turn** — your context, tokens, tool state and checkpoints all survive, because
restarting would replay completed tool calls. Two hops per turn by default, and a
model that keeps failing is cooled for a minute.

Want a different provider? Register any OpenAI- or Anthropic-compatible endpoint
with `/provider add` (or the **Providers** tab in the Web UI) — it appears in the
model picker as `custom:<id>`, with the same tools and the same events.

Inspect what each model can do with `/model caps`. It prints three values, and
`?` means **we do not know** — never "no".

---

## ⌨️ CLI command reference

All **36** commands. `/help` prints the same table, and `F1` opens it. The table
below and the help screen are the same list — `cli/render.SLASH_COMMANDS` is the
one declaration, which also feeds the `/` autocomplete.

| Command | Description |
|---------|-------------|
| `/help` | Show all commands |
| `/addapi` · `/keys` | Add a Gemini key · activate a key or provider with `↑/↓`. Keys, usage counters and the pin are shared with the Web UI — add a key in one and the other sees it |
| `/model [name\|auto]` · `/mode [name]` | Switch model · switch reasoning mode. `auto` lets Agent2 pick per turn |
| `/model routing [off\|default_only\|always]` | Show or set when automatic routing applies |
| `/model caps [<model>] [field=value …]` | Show or correct a model's capability record. Unknown prints `?`, never "no" |
| `/model rank [<model>\|all] [force]` | Ask a model to classify an unrecognised model id |
| `/provider [add\|list\|use\|del\|test]` | Manage custom model providers |
| `/mcp` | Ephemeral menu of every MCP server — `↑↓` · `Space` · `Enter` · `Esc` |
| `/mcp status` · `/mcp health` | Print every server · a `✓`/`✗` report with **[R]** Retry and **[S]** Settings |
| `/mcp <server> connect\|disconnect\|reconnect\|list\|status\|config\|health` | One server. `config` edits url · port · security key |
| `/mcp connect` · `/mcp disconnect` | Every server at once |
| `/settings` (`/config`) | One menu onto every settings area — model · mode · theme · colour · rules · skills · offline · MCP · keys |
| `/rules` | Activate or deactivate the rules injected into every system prompt |
| `/skills …` | Skills from `.agent2/skills/` — bare `/skills` opens the menu · `list` · `on` · `off` · `reset <name>` · `show <name>` · `last` · `reload` |
| `/workflow …` | Workflows from `.agent2/workflows/` — bare `/workflow` opens the menu · `list` · `new` · `edit` · `run` · `auto <goal>` · `delete` · `show` · `state` · `reload` |
| `/ultracode …` | The adaptive autonomous loop — `start <goal>` · `run` · `approve` · `state` · `cancel` · `policy` |
| `/offline` | Which subsystems may reach the network |
| `/theme` · `/color` | CLI colour theme · accent colour |
| `/workspace [path]` · `/cd <dir>` | Show or change where the agent is rooted. Switching cancels running tasks |
| `/init [about]` | Analyse **this workspace** and create or update `.agent2/agent2.md`. Free text after the command is your own description and outranks the model's guess |
| `/scan <path>` · `/read <file>` · `/run <cmd>` | Analyse a directory for this turn · read a file · run a shell command |
| `/search <query>` | Web search via DuckDuckGo |
| `/memory` · `/addmem <text>` | List saved memories · save one manually |
| `/tasks` · `/recovery …` | The persistent task list · what a killed run left behind (`scan` · `ack`\|`retry`\|`kill <kind> <id>`) |
| `/health` · `/metrics [reset]` | Is it working (`✓/⚠/✗/○` per subsystem) · how fast, how often, how big |
| `/load` | Load the last conversation from **this** directory |
| `/pause` · `/resume` | Pause this conversation · pick a previous one back up with a `↑/↓` picker across all projects |
| `/history` · `/clearhistory` | Show the last 10 messages · wipe the history |
| `/clear` · `/shrink` | Clear the screen · summarize and shrink the context |
| `/exit` | Quit |

`/burp …` still works and **forwards** to `/mcp burp …`, printing where it went —
it is retired from the help table but never silently removed. There is
deliberately **no `/mcp auto`**: auto-connect is written as a side effect of
`connect`/`disconnect` and of the bare `/mcp` menu, and the explicit setter
(`POST /api/mcp/<key>/auto`) is web-only.

**Keybindings — all nine:** `Ctrl+B` diff viewer · `Ctrl+P` command palette ·
`Ctrl+L` clear the screen · `Ctrl+R` reverse-search the input history · `Ctrl+T`
running and queued tasks · `Ctrl+K` cancel the current task · `Esc` close an
overlay or cancel the turn · `Tab` accept the autocomplete suggestion · `F1` this
help.

### `/init` — teaching Agent2 what your project is

`/init` walks the workspace and writes `.agent2/agent2.md`: what the project is,
its languages, package managers (each with the lockfile that proved it),
frameworks, entry points, tests and the runners actually proved, build system, dev
and test commands, and the notes its own source files carry about themselves. It
also creates `.agent2/skills/`.

Two things make it worth running early. The file is read back into **every later
prompt**, so the agent knows your architecture before it acts. And a rerun never
overwrites a section you took over — ownership is a marker on a section's first
body line, so deleting the marker makes that section yours forever.

```bash
/init this is a project of calculator   # your description outranks the model's guess
/init                                   # a rerun recovers what you typed the first time
```

`/scan <path>` is the other half and it writes nothing: it analyses one directory
and feeds that to the model for this turn. `/init` writes the file; `/scan`
answers a question.

### `/skills` — instruction files that apply per request

Drop a `SKILL.md` (or `AGENTS.md`, `GEMINI.md`, and seven other manifest names)
under `.agent2/skills/` and Agent2 picks the relevant ones **per message** — not
all of them into every prompt. At most four reach one prompt, inside a 6 000-char
share, and `/skills last` tells you exactly which applied and why each of the rest
did not.

`/skills` is a **block list, not a force list.** Switching something ON means
"stop blocking it", which restores automatic selection; switching it OFF beats
every signal including a message that names the skill. Nothing here ever edits a
skill file — your choice is a row in `agent2.db`, scoped to this project, because
a skill is usually somebody else's file in somebody else's repository.

### `/workflow` — a plan as a graph

Write `.agent2/workflows/<name>.yaml` (or `.json`) and `/workflow run <name>`
validates it, builds the DAG, **shows you the plan**, and only then runs it. Bare
`/workflow` executes nothing — it opens the menu.

```bash
/workflow list                  # what this project has, including files that will not run
/workflow show build-and-test   # nodes, order, problems
/workflow run build-and-test    # validate → build the DAG → show the plan → run
/workflow auto "add rate limiting to the login route"
/workflow state                 # the live run, the waves, what starts next
```

`/workflow auto <goal>` turns a sentence into a graph. It **draws the plan before
writing anything** — no run row, no task rows — and then asks; *Plan only* is the
default answer, so Esc means "you already showed me". `/workflow state` is also
where interrupted work is released: asking what is running un-sticks what a dead
process parked, and it **names the nodes** it released.

The filename is the workflow's name. A `name:` line inside the file that disagrees
is reported, and the filename wins. PyYAML is not a dependency, so the parser
declares the subset it reads and anything outside it is listed as unsupported
rather than guessed at — and a file written for an older schema still runs, with a
note saying it was upgraded on the way in.

### `/ultracode` — the autonomous loop

UNDERSTAND → INSPECT → DISCOVER SKILLS → PLAN → EXECUTE → OBSERVE → ANALYZE →
VERIFY, and on a failed verify: RE-PLAN → EXECUTE. Six verbs:

```bash
/ultracode start "make the CLI resume the last conversation by default"
/ultracode state      # the stage, the nodes, what is holding it
/ultracode approve    # release the gate
/ultracode run        # drive the loop
/ultracode cancel
/ultracode policy     # every ceiling, and whether approval is on
```

Bare `/ultracode` reports and runs nothing. An unknown first word is **refused,
never read as a goal** — `/ultracode fix the login bug` is indistinguishable from
a verb this build lacks, and guessing would turn a typo into a planner call and a
graph of task rows.

Two things are worth knowing before you use it. **Approval is on by default and
the gate is a graph node**, not a prompt: it is created paused with everything
depending on it, so the hold survives a crash and nothing can helpfully release it
for you. And **verification is a node too** — it asks the durable record whether
each step's commands exited zero and its writes actually landed, never a model.
"Done" is not verification.

> ⚠️ **`/ultracode run` is terminal-only, and the browser panel says so rather
> than hiding it.** Driving the loop needs a synchronous worker that owns a whole
> model turn, and a web request thread has neither the socket id nor the stream.
> Nothing is lost: the browser's next ordinary chat turn does that work, because
> the current node reaches the prompt either way. `work`, `replan` and `finalize`
> are verbs on **neither** surface — the driver owns all three and the order they
> run in.

### Reviewing what changed — `Ctrl+B`

Every file the agent wrote is echoed inline as a diff, and `Ctrl+B` opens the
whole session in one full-screen viewer: `f` whole file · `d`/`u` unified · `s`
side-by-side · `c` context · `e` collapse · `/` search (`n`/`p` to step) · `y`
copy · `w` write a patch · `←→` between files · `q` to close.

**Opening it writes nothing.** There is no approval gate anywhere in the diff
pipeline — `[A] Accept` is a review mark and says so. `r` is the one key that
touches disk, and it takes **two presses**: the first marks and arms, the second
reverts that file. Any other key cancels. On a truncated diff it refuses outright
and says why, because the reconstruction is partial.

### Stopping something — `Ctrl+C`

`Ctrl+C` cancels the **running command**, not your session. The REPL stays alive,
your history stays intact, and you can immediately type again. To actually exit,
press it twice in quick succession, or use `/exit`.

A cancel cleans six things: the subprocess, its children, the pipes, the task
checkpoint, the scheduler state and the command record.

### When a command goes quiet

After 20 seconds of silence a command is **reported** as stuck — never killed for
it. You get `[W]ait` (60 seconds more), `[R]etry` (a keypress, never automatic,
capped at 2) or `[K]ill`. Both kill ceilings are **off by default** on purpose: a
long scan or build cut off at 20 seconds returns a truncated result the model
cannot recognise as truncated. Turn them on with `AGENT2_CMD_TIMEOUT` and
`AGENT2_CMD_IDLE_TIMEOUT` if you want them.

---

## 🔒 Security

The web surface has a real trust model, and it defaults to something sensible for
a machine you own.

| `AGENT2_WEB_AUTH` | Behaviour |
|---|---|
| `auto` *(default)* | Loopback is trusted; every other origin needs the access token |
| `always` | Everyone needs the token, loopback included |
| `off` | No authentication. Only for a box nothing else can reach |

This is **env-only** on purpose — a stored "off" would be a persistent silent
downgrade with nothing on screen to reveal it.

- The access token is printed in the startup banner and lives **in memory only**
  (or set `AGENT2_WEB_TOKEN` yourself). It is never written to disk.
- Exchange it for a session with `POST /api/auth/login`; `POST /api/auth/rotate`
  mints a new token and revokes **every** session.
- Sessions are stored as SHA-256 **digests**, so a copied `agent2.db` grants
  nothing.
- Per-IP rate limits: 600 requests a minute, 10 login attempts per five minutes.

**Restricting what a client may do.** Two knobs, scoped differently:

```bash
AGENT2_WEB_ROLE=viewer agent2 web      # the browser may read, and nothing else
AGENT2_DENY_CAPS=exec,fs.delete agent2 # no shell, no deletes — CLI included
```

`AGENT2_WEB_ROLE` describes **web clients** (`owner` · `operator` · `viewer`);
`AGENT2_DENY_CAPS` subtracts from **this whole process**, the CLI included. An
unknown role falls back to `viewer`, never to `owner`.

**Binding.** The default bind is `0.0.0.0`. For a laptop, pin it to loopback:

```bash
AGENT2_HOST=127.0.0.1 agent2 web       # local-only, recommended
```

**Do not expose the port to the public internet.** Authentication is a lock on a
door, not a reason to put the door outside.

**Your credentials.** API keys, provider keys and the ZAP security key are stored
as `a2s:` **references**, never as values, and the master key lives in
`~/.agent2/secret.key` — deliberately **outside** the database directory, because
a key sitting beside `agent2.db` travels with every copy of it. Every display path
shows a constant `••••••••` whose length tells you nothing about the secret. This
protects the database *alone*; it is not full-disk encryption, and
`GET /api/platform` says so honestly.

---

## 🧩 Burp Suite & OWASP ZAP

Both bridges speak MCP, both are **off by default**, and both live behind one
command.

```
/mcp                          # menu: every server, Space to toggle, Enter to apply
/mcp burp connect             # Burp Suite MCP — http://127.0.0.1:9876
/mcp zap connect              # OWASP ZAP MCP  — http://127.0.0.1:8282
/mcp zap config               # edit the endpoint, the port, or the security key
/mcp health                   # ✓/✗ per server, then [R] Retry · [S] Settings
```

Once connected, that server's tools join the same agent loop as everything else —
`burp_*` for proxy history, Repeater, Intruder, Scanner and the site map; `zap_*`
for ZAP's own. You do not call them; you describe the work.

Auto-connect is remembered **per project**: connecting in one checkout does not
arm the bridge in another. Where the server lives and what key reaches it, by
contrast, is per machine — you configure the port once, not once per repository.
The stored key never appears in `status()` and never leaves the module except on
the wire.

Switching projects never disconnects a live bridge. Auto-connect governs
*starting* a session, and tearing down an in-flight scan to enforce a preference
about starting one would be a destructive act nobody asked for.

> **Authorized use only.** Scanning or testing systems you do not own, or are not
> authorized to test, is illegal in most jurisdictions. Point this at your own lab,
> a deliberately vulnerable target, or a signed scope.

---

## 🧾 Memory & rules

Two different things, both persistent, both reaching the model on every turn.

**Memories** are facts. Say "remember I prefer TypeScript" and the agent saves one
itself, or add one by hand:

```
/addmem Prefer pytest over unittest in this repo
/memory                       # list what is saved
```

**Rules** are standing instructions — they are injected into the system prompt
verbatim, every turn, until you deactivate them:

```
/rules                        # ↑↓ · Space to toggle · Enter to apply · Esc
```

Both are shared across every surface through one `agent2.db`, so a memory added in
the browser reaches the CLI on its next turn. Memories dedupe on exact content,
and the Web UI can prune the least valuable while sparing anything you marked
important.

---

## ⏳ Long-running work

```
/tasks                        # what is running, with progress
/pause · /resume              # stop and pick up again
```

Long work is **checkpointed**, so a resume never re-runs what already finished —
and a crash or a closed terminal is recoverable rather than lost. In the Web UI,
`GET /api/recovery` offers the same thing on reconnect, and the CLI asks you on
startup when it finds interrupted work.

Nothing is killed for being slow. See [when a command goes
quiet](#when-a-command-goes-quiet) above.

---

## 🧬 Ghost text, offline

As you type in the CLI you will see grey suggestions. Those come from the Personal
Intelligence Layer, and they are **entirely local**: a trie over your own
vocabulary, an n-gram table and phrase prefixes, all in `agent2.db`. No network
call, no model call, no retraining.

It learns passively from what you accept and ignore. Two further modules —
grammar correction and prompt improvement — exist and are **off by default**;
turn them on from `/settings` if you want them. Everything here degrades to doing
nothing on any error, and `POST /api/pil/wipe` is a real "forget me".

---

## 🗂️ Files Agent2 creates

Everything lives next to the project — there is no `.env`. The only thing outside
the project folder is the global launcher in `~/.local/bin`.

| Path | What it is |
|------|------------|
| `agent2.db` | **All** state: keys, settings, chats, memories, rules, providers. Back this up and you've backed up everything |
| `agent2.db-wal` · `agent2.db-shm` | SQLite's write-ahead log and its shared-memory index. Normal, expected, and managed for you — a background pass folds the `-wal` back and truncates it during a lull. Don't delete them while Agent2 is running |
| `logs/` | All log files in one folder: `agent2.log` (audit trail, rotating 5 MB × 3) and `agent2-web.log` (web console) |
| `.venv/` | The virtual environment `run.py` builds |
| `~/.local/bin/agent2` | The global launcher (`agent2.bat` on Windows). Removed by `--uninstall` |

Move the database and logs elsewhere with `AGENT2_DB` and `AGENT2_LOG_DIR` — handy
for putting both on a mounted volume in Docker.

---

## ⚙️ Tuning (optional)

Sensible defaults; nothing here needs setting.

| Var | Default | Why you'd change it |
|-----|---------|---------------------|
| `AGENT2_HOST` | `0.0.0.0` | Set `127.0.0.1` for local-only. **Recommended** — see the security note above |
| `AGENT2_PORT` | `1311` | Pin the web port instead of letting it auto-pick |
| `AGENT2_NO_BROWSER` | — | Set `1` on a headless or remote box so it doesn't try to open a browser |
| `AGENT2_LOG_LEVEL` | `info` | `debug` to see every socket frame and static request |
| `AGENT2_DB_POOL` | `8` | Raise it if you drive many concurrent turns; `pool_stats()` reuse-vs-created tells you if it's right |
| `AGENT2_DB_POOL_IDLE` | `300` | Seconds before an unused DB connection is closed. `0` keeps them all warm |
| `AGENT2_MAX_CONCURRENT_TURNS` | `8` | How many agent turns may run at once. Lower it on a small box; `0` removes the limit entirely |
| `AGENT2_MAX_QUEUED_TURNS` | `64` | How many turns may wait. Past this a message is refused outright instead of joining an endless backlog |
| `AGENT2_WEB_AUTH` | `auto` | `always` to require the token even on loopback; `off` only on a box nothing else can reach |
| `AGENT2_WEB_ROLE` | `owner` | `operator` or `viewer` to restrict what **web clients** may do |
| `AGENT2_DENY_CAPS` | — | Capabilities subtracted from **this whole process**, CLI included — e.g. `exec,fs.delete` |
| `AGENT2_CMD_TIMEOUT` · `_IDLE_TIMEOUT` | `0` · `0` | Both **off** by design. Set them only if you want a hard kill ceiling |
| `AGENT2_CMD_STUCK_SEC` | `20` | Silence after which a command is *reported* as stuck. It is never killed for this |
| `AGENT2_MODEL_ROUTING` | — | `default_only` or `always` to let the router pick a model. Empty so the stored setting wins |
| `AGENT2_FALLBACK_MAX_HOPS` | `2` | Model switches allowed **per turn** after a failure |
| `AGENT2_SECRET_KEY_FILE` | `~/.agent2/secret.key` | Where the master key lives. Keep it out of the database directory |
| `AGENT2_LOG_DIR` | `<db dir>/logs` | Put every log somewhere else — a mounted volume, for instance |

### Bounding the graph family

Skills, workflows, the DAG, dynamic planning and UltraCode each have a master
switch and their own ceilings. Every one of them is **reported when it engages** —
a truncated answer never reads as a complete one.

| Var | Default | Why you'd change it |
|-----|---------|---------------------|
| `AGENT2_SKILLS` | `1` | `0` for an empty catalog and a skills block that collects nothing. `/skills` still reads the folder and names the switch, rather than showing a list nothing will use |
| `AGENT2_SKILLS_IN_PROMPT` | `4` | How many skills may reach **one** prompt. This is the number "not every skill in every prompt" is measured against |
| `AGENT2_SKILLS_MAX_CHARS` | `6000` | The share skills may ask for *before* the context budget sees them, so one enormous skill cannot arrive having already displaced the conversation |
| `AGENT2_WORKFLOWS` | `1` | `0` and no run may be instantiated; the `workflow_state` context block collects nothing |
| `AGENT2_WORKFLOW_MAX_NODES` | `64` | Nodes one workflow may declare. **Refused, never truncated** — a graph missing its last node is a graph whose dependencies no longer close |
| `AGENT2_WORKFLOW_STATE_CHARS` | `1200` | What the live-run context block may spend. Small on purpose: the turn needs the *current* node, and the rest are named rather than inlined |
| `AGENT2_DAG_MAX_NODES` | `512` | The outer wall for any consumer. A workflow is still bounded by its own 64 |
| `AGENT2_DAG_MAX_MUTATIONS` | `64` | Nodes a **live** graph may gain after it was declared. `0` is a supported answer — *plan once and never invent more work* |
| `AGENT2_DAG_MAX_WORKERS` | `4` | Threads the pump runs nodes on. `0` is supported: nodes run inline, one at a time |
| `AGENT2_DAG_MAX_RUNNING` | `8` | Nodes in flight at once, whatever kind. Derived from the rows, so it counts the other process's claims in dual mode |
| `AGENT2_DAG_MAX_COMMANDS` · `_MCP_CALLS` | `2` · `2` | Shell nodes · MCP calls in flight together. Small deliberately — eight commands racing for one terminal makes output unreadable, and a bridge is one instance of somebody else's program |
| `AGENT2_DAG_MAX_MODEL_CALLS` | `200` | The **lifetime** spend of one run on model-backed nodes. Charged from attempt counts, so a retry is charged and a crash-resumed run gets no fresh allowance |
| `AGENT2_DAG_MAX_ATTEMPTS` | `2` | Times one node may be started, retries included. Bounded **and** classified — whether repeating *this* operation is safe at all is a separate question, asked first |
| `AGENT2_DAG_NODE_TIMEOUT` | `0` (off) | Off for `AGENT2_CMD_TIMEOUT`'s reason: a node may legitimately be a 40-minute build |
| `AGENT2_DYNAMIC_WORKFLOW` | `1` | `0` turns off the **planner** only. A workflow file you wrote still runs — this is a statement about who may author a graph |
| `AGENT2_DYNAMIC_MAX_STEPS` | `24` | Steps one generated plan may contain. **Clipped and reported**, the opposite of a file's node ceiling, because refusing would throw away a usable plan for being wordy |
| `AGENT2_DYNAMIC_MAX_ROUNDS` | `3` | Times one run may be handed back to a planner. Not the same ceiling as mutations: twenty single-node additions sit far under 64 while being exactly the runaway it exists to stop |
| `AGENT2_ULTRACODE` | `1` | `0` turns off the autonomous **driver**. `/workflow run` and `/workflow auto` still work |
| `AGENT2_ULTRACODE_MAX_CYCLES` | `6` | Execute→verify→re-plan cycles one run may spend. A cycle that fixes something without asking a planner spends no round at all |
| `AGENT2_ULTRACODE_BUDGET_SEC` | `0` (off) | Off by default, and checked **between cycles** — reported, never enforced by killing a worker mid-write |
| `AGENT2_ULTRACODE_APPROVAL` | `1` | `0` builds the graph without the approval gate. It does **not** widen what a node may do — every action still goes through the capability gate when it runs |
| `AGENT2_VERIFY_MAX_ROWS` | `500` | The one knob verification has. There is deliberately **no `AGENT2_VERIFY=0`**: verification off would not make Agent2 quieter, it would make it credulous |
| `AGENT2_RESUME` | `off` | `last` to continue the previous conversation on every launch. Off by default because an unasked resume rewrites a transcript you never meant to open |
| `AGENT2_CONTEXT_ISOLATION` | `project` | `off` to pool memories and rules across every checkout instead of scoping them here |
| `AGENT2_METRICS` | `1` | `0` and every measurement point becomes a single boolean test |

The full table — all **116** variables with their real defaults — is in
[the documentation](https://agent2.is-best.net/docs/env/).

---

## 📌 Troubleshooting

| Problem | Fix |
|---------|-----|
| `No API keys configured` | `python run.py --addapi` or `/addapi` in the CLI |
| Key quota exhausted | Keys rotate automatically in **both** the CLI and the Web UI — load is spread round-robin, an exhausted key is skipped, and if every key is exhausted they are all revived once and retried. Add more with `--addapi`; check status with `/keys` |
| Model returns an empty response | Switch to `2.5-flash`: `/model 2.5-flash` |
| Port 1311 already in use | Agent2 auto-picks a free port; pin one with `AGENT2_PORT=8123` |
| `python` not found on Windows | Use `py run.py` |
| `agent2` command not found | The launcher is in `~/.local/bin` — open a new terminal so the `PATH` change takes effect, or run `python run.py` directly |
| A file operation says `pip install X` | Rare — setup installs every Python backend. It means the wheel failed to build (setup would have warned): rerun `python run.py`, or install that one package into `.venv` yourself |
| OCR / media / Office→PDF unavailable | Those need a **system** binary pip can't ship: `tesseract`, `ffmpeg`, `libreoffice`, `unrar`, or `poppler`. Install it with your OS package manager |
| Broken venv / import errors | `python run.py --reset` — wipes and reinstalls cleanly |
| Want a clean slate | `python run.py --uninstall`, then `python run.py` |
| A `/mcp` server shows `✗` | It is enabled and unreachable — start Burp or ZAP, or check the endpoint with `/mcp <server> config`. A server that is simply **off** is reported as fine, not as a failure |
| The CLI and the browser disagree about a setting | Both poll for the other's changes. `POST /api/sync/poll` forces a check; `GET /api/sync` shows what each side last saw |
| A change in one surface never arrives | Check `listener_errors` at `GET /api/sync`. A subscriber that raises is swallowed and **counted**, so a rising number is the symptom |
| Ghost text does nothing in the browser | Expected. The prediction endpoint works, but no shipped front end wires it up yet — the CLI is the only surface with ghost text |

### Checking whether it's healthy

In Web or dual mode, `GET /api/health` reports whether the moving parts are
actually working:

```
curl http://localhost:1311/api/health
```

`200` with `"ok": true` means healthy. `503` means something genuinely broke,
and the `problems` list says what — an unreachable database, a schema version
that doesn't match the code, or a full turn queue. It returns counters only (no
keys, no chat text, no file paths), so it is safe to point a monitor at.

Two things it deliberately does **not** call unhealthy: an optimization you
switched off on purpose (`AGENT2_WAL_CHECKPOINT_SEC=0`,
`AGENT2_MAX_CONCURRENT_TURNS=0`), and a worker pool showing zero workers on a
server that hasn't handled a turn yet — those start on first use. A disabled
subsystem reports `○ off`, which is not a lesser `⚠ warn`; a cross printed at a
deliberate choice is how an alert stops being read.

In the CLI, `/health` prints the same verdicts — one `✓/⚠/✗/○` row per subsystem,
then the `problems` and `warnings` lines, which are the actionable half. There is
one assembly behind both surfaces, so the terminal and a monitor cannot disagree
about what "healthy" means. Fourteen subsystems are covered (the list expands to
sixteen rows, one per MCP server and one each for Gemini and custom providers):

```
/health          # is it working
/metrics         # how fast, how often, how big
/metrics reset   # clear this process's window — only when asked
```

`/metrics` is a **separate command on purpose.** It answers a different question —
thirteen signals with count, average, p50, p95 and max: model latency and tokens,
tool latency and failures, command duration, queue wait, task and workflow
duration, memory retrieval, context size, MCP latency, permission denials. Folding
it into `/health` would put a percentile next to a fault and invite you to read one
as the other.

Three of those thirteen are **borrowed rather than measured here** — model latency,
model errors and permission denials belong to the router and the permission gate,
which record them durably and install-wide. The other ten are in-memory and
per-process, which the payload's `scope` field states: in dual mode "since when"
means "since this process started".

---

## 📚 Where to go next

| Read this | For |
|---|---|
| [`README.md`](README.md) | The short tour and the architecture map |
| [`FEATURES.md`](FEATURES.md) | The full inventory — every subsystem, every counted number, and what is *not* built |
| [**agent2.is-best.net/docs**](https://agent2.is-best.net/docs/) | 45 pages, each load-bearing rule stated together with the bug it prevents |
| [`CLAUDE.md`](CLAUDE.md) | The index of every invariant and the file that owns it — read this before changing one |

Two things worth knowing before you rely on anything here. First, several rules in
this system exist to block a bug that produces **no error at all** — just quietly
wrong output — so a rule that looks redundant usually is not; the module docstring
says why. Second, anything designed and not yet built is labelled as such
wherever it appears, including everything on the Agent-2-Pro page.
