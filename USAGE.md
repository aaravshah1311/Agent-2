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

All 29 commands. `/help` prints the same table, and `F1` opens it.

| Command | Description |
|---------|-------------|
| `/help` | Show all commands |
| `/addapi` · `/keys` | Add a Gemini key · show key status and usage. Keys, usage counters and the pin are shared with the Web UI — add a key in one and the other sees it |
| `/model [name\|auto]` · `/mode [name]` | Switch model · switch reasoning mode |
| `/model routing [off\|default_only\|always]` | Show or set when automatic routing applies |
| `/model caps [<model>] [field=value …]` | Show or correct a model's capability record |
| `/model rank [<model>\|all] [force]` | Ask a model to classify an unrecognised model id |
| `/provider [add\|list\|use\|del\|test]` | Manage custom model providers |
| `/mcp` | Ephemeral menu of every MCP server — `↑↓` · `Space` · `Enter` · `Esc` |
| `/mcp status` · `/mcp health` | Print every server · a `✓`/`✗` report with **[R]** Retry and **[S]** Settings |
| `/mcp <server> connect\|disconnect\|list\|status\|config` | One server. `config` edits url · port · security key |
| `/mcp connect` · `/mcp disconnect` | Every server at once |
| `/settings` (`/config`) | One menu onto every settings area — model · mode · theme · colour · rules · offline · MCP · keys |
| `/rules` | Activate or deactivate the rules injected into every system prompt |
| `/offline` | Which subsystems may reach the network |
| `/theme` · `/color` | CLI colour theme · accent colour |
| `/workspace` · `/cd <dir>` | Show or change where the agent is rooted |
| `/memory` · `/addmem <text>` | List saved memories · save one manually |
| `/scan [path]` · `/read <file>` · `/run <cmd>` | Scan a project · read a file · run a shell command |
| `/search <query>` | Web search via DuckDuckGo |
| `/tasks` · `/pause` · `/resume` | Background work: list it, pause it, pick it up again |
| `/load` | Reload the last conversation from **this** directory |
| `/history` · `/clearhistory` | Show the transcript · wipe it |
| `/clear` · `/shrink` | Clear the screen · summarize and shrink the context |
| `/exit` | Quit |

`/burp …` still works and **forwards** to `/mcp burp …`, printing where it went —
it is retired from the help table but never silently removed.

**Keybindings:** `Ctrl+B` diff viewer · `Ctrl+P` command palette · `Ctrl+T`
terminal · `F1` help · `Ctrl+L` clear.

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

The full table — all 61 variables with their real defaults — is in
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
server that hasn't handled a turn yet — those start on first use.

---

## 📚 Where to go next

| Read this | For |
|---|---|
| [`README.md`](README.md) | The short tour and the architecture map |
| [`FEATURES.md`](FEATURES.md) | The full inventory — every subsystem, every counted number, and what is *not* built |
| [**agent2.is-best.net/docs**](https://agent2.is-best.net/docs/) | 39 pages, each load-bearing rule stated together with the bug it prevents |
| [`CLAUDE.md`](CLAUDE.md) | The index of every invariant and the file that owns it — read this before changing one |

Two things worth knowing before you rely on anything here. First, several rules in
this system exist to block a bug that produces **no error at all** — just quietly
wrong output — so a rule that looks redundant usually is not; the module docstring
says why. Second, anything designed and not yet built is labelled as such
wherever it appears, including everything on the Agent-2-Pro page.
