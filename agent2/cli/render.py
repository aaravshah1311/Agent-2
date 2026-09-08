# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/cli/render.py
────────────────────
Everything the CLI draws: the banner, the help table, agent replies, tool-call
cards, plans, and the slash-command registry those are built from.

⚠️ EVERY PRINTER HAS TWO BODIES, AND BOTH ARE REACHABLE.
Rich is an optional dependency (`env._RICH`), so each function renders through
Rich when it is installed and through plain ANSI when it is not. The plain
branch is not dead code — it is what `run.py` shows while it is still installing
dependencies, and what a redirected stdout gets. A change to one branch that is
not mirrored in the other silently degrades that install.

⚠️ COLOURS ARE READ THROUGH `P` ON EVERY CALL.
`P.PU`, never a module-level `PU`. `/theme` repaints the shared palette object at
runtime; a name bound at import would freeze this module's colours forever with
no exception and no traceback. See `agent2/cli/theme.py`.

Layer: env / theme / models → render.
"""

import os
import re
import shutil
from datetime import datetime
from pathlib import Path

from agent2.cli.env import (
    IS_WIN,
    Markdown,
    OS_NAME,
    Panel,
    Table,
    Text,
    _con,
    _RICH,
    rbox,
)
from agent2.cli.models import SHELL_LABEL
from agent2.cli.theme import B, D, GR, P, R, RD, WH, YW


# ── Terminal width ─────────────────────────────────────────────────────────────
def tw() -> int:
    return min(shutil.get_terminal_size((100, 30)).columns, 120)


# ── Print helpers ──────────────────────────────────────────────────────────────
def hr(char="─", col=D):
    print(f"{col}{char * (tw() - 2)}{R}")

def status_line(msg: str, kind: str = "info"):
    sym  = {"info": "ℹ", "success": "✓", "warning": "⚠", "error": "✗"}.get(kind, "•")
    _col = {"info": P.CY, "success": GR, "warning": YW, "error": RD}.get(kind, D)
    if _RICH:
        style = {"info":"#60b8ff","success":"#3ddc84","warning":"#f0c060","error":"#ff5555"}.get(kind,"dim")
        _con.print(f"  [{style}]{sym}[/] {msg}")
    else:
        print(f"  {_col}{sym}{R} {msg}")

def print_banner():
    os.system("cls" if IS_WIN else "clear")
    if _RICH:
        title = Text()
        title.append("  ⚡ ", style="bold yellow")
        title.append("Agent 2 CLI", style=f"bold {P.ACCENT}")
        title.append(f"  {OS_NAME}/{SHELL_LABEL}", style="dim")
        _con.print(Panel(title, border_style="#1e1e30", padding=(0, 1)))
    else:
        w = min(tw(), 56)
        print(f"{P.PU}{'═' * w}{R}")
        print(f"{P.PU}{B}  ⚡ Agent 2 CLI{R}  {D}{OS_NAME}/{SHELL_LABEL}{R}")
        print(f"{P.PU}{'═' * w}{R}")
    print()

# ── Slash-command registry (single source for help + autocomplete) ─────────────
# (command_token, base_command, description). base_command is what the completer
# inserts and what the dispatcher matches; command_token is the display form.
SLASH_COMMANDS: list[tuple[str, str, str]] = [
    ("/help",            "/help",         "Show this help"),
    ("/addapi",          "/addapi",       "Add a Gemini API key (saved to agent2.db)"),
    ("/keys",            "/keys",         "Activate a key/provider with ↑/↓ (Gemini + custom; shows model id)"),
    ("/model [name]",    "/model",        "Pick a model with ↑/↓ (built-ins + custom providers)"),
    ("/mode [name]",     "/mode",         "Pick a mode with ↑/↓ (fast ⚡ | pro ★ | thinking 🧠)"),
    ("/theme",           "/theme",        "Pick a colour theme with ↑/↓ (purple, emerald, ocean, …)"),
    ("/color",           "/color",        "Set the accent colour with ↑/↓"),
    ("/settings",        "/settings",     "One menu onto every setting (model · mode · theme · rules · offline · MCP · keys)"),
    ("/rules",           "/rules",        "Activate/deactivate the rules injected into every prompt (↑↓ · Space · Enter · Esc)"),
    # ⚠️ ONE ENTRY, AND THE DESCRIPTION SAYS `.agent2/skills/` OUT LOUD. A skill is a
    # file the user wrote in their own checkout, and the first question about a menu of
    # switches is *switches over what*. It sits beside `/rules` because they are the
    # same shape — an ephemeral ON/OFF menu over text that reaches the prompt — and
    # deliberately NOT beside `/init`, which is the command that creates the folder.
    ("/skills …",        "/skills",       "Skills from .agent2/skills/ — bare /skills opens the menu (↑↓ · Space · Enter · Esc) · list | on | off | reset <name> | show <name> | last | reload"),
    # ⚠️ ONE ENTRY, AND IT IS NOT BESIDE `/skills` BY ACCIDENT — both live in
    # `.agent2/`, both are files a human writes, and a reader looking for one will
    # find the other. But the verbs differ in kind: `/skills` switches something on,
    # `/workflow` **runs** something and **writes** a file, so `[N]ew`/`[D]elete` are
    # named here rather than left to a menu a user has to open to discover.
    ("/workflow …",      "/workflow",     "Workflows from .agent2/workflows/ — bare /workflow opens the menu (New · Edit · Run · Delete) · list | new <name> | edit <name> | run <name> | auto <goal> | delete <name> | show <name> | state | reload"),
    # ⚠️ IT SITS BELOW `/workflow` BECAUSE IT IS THE SAME RUN, DRIVEN HARDER — an
    # UltraCode run is one `exec_workflows` row and N `agent_tasks` rows, so
    # `/workflow state` reports it too. What differs is who decides the next node:
    # `/workflow run` works a plan a human wrote, `/ultracode` re-plans its own when
    # a cycle ends badly. ⚠️ `start` and `run` are named as SEPARATE verbs on
    # purpose — planning writes task rows and executes nothing, and a description
    # that said "runs a goal" would hide the approval gate standing between them.
    ("/ultracode …",     "/ultracode",    "Adaptive autonomous loop — bare /ultracode reports and runs nothing · start <goal> (plans) | approve | run (executes) | cancel | state | policy"),
    ("/offline",         "/offline",      "Toggle offline PIL models (↑/↓ move · → toggle on/off · Esc done)"),
    ("/provider …",      "/provider",     "Manage custom API providers (add | list | use | del | test)"),
    # ⚠️ ONE MCP ENTRY, AND `/burp` IS DELIBERATELY NOT LISTED. It still works —
    # `agent2cli.cmd_burp` forwards to `/mcp burp` so nobody's muscle memory
    # breaks — but this table feeds both `/help` and the completer, and offering
    # a retired spelling there is how a deprecation never finishes.
    ("/mcp …",           "/mcp",          "MCP servers — bare /mcp opens the menu (↑↓ navigate · Space toggle · Enter apply · Esc cancel) · connect | disconnect | status | list · <server> config (url/port/key)"),
    ("/workspace [path]", "/workspace",   "Show or switch the active workspace (sandbox root; switching cancels running tasks)"),
    ("/cd <path>",       "/cd",           "Alias for /workspace — switch the active workspace"),
    # ⚠️ `/init` WRITES, and `/scan` does not — that is the whole distinction, and
    # it is why the two are listed together. `/scan <path>` analyses a directory
    # and feeds the MODEL its file contents for this turn; `/init` analyses the
    # WORKSPACE and creates `.agent2/agent2.md`, which every later turn then reads
    # as the project's own description. One is a turn's input, the other is a file
    # on disk — so the description says "create/update" out loud rather than
    # letting a user discover the write afterwards.
    ("/init [about]",    "/init",         "Analyze this workspace and create/update .agent2/agent2.md — what the project is, its features, stack, entry points, tests, commands. Add a line about it: /init this is a calculator"),
    ("/scan <path>",     "/scan",         "Scan and analyze an entire project directory"),
    ("/run <cmd>",       "/run",          "Run a shell command directly"),
    ("/read <file>",     "/read",         "Read a file's contents"),
    ("/search <query>",  "/search",       "Web search (DuckDuckGo)"),
    ("/memory",          "/memory",       "List all saved memories"),
    ("/addmem <text>",   "/addmem",       "Save a memory manually"),
    ("/tasks",           "/tasks",        "Show the current task list (persists across restarts)"),
    # ⚠️ Listed next to `/tasks` and nowhere near `/pause`\`/resume`, because that is
    # the confusion it must not cause: `/resume` picks a CONVERSATION, this reports
    # what the automatic crash scan found and decided about interrupted EXECUTION.
    ("/recovery …",      "/recovery",     "Crash recovery — what a killed run left behind (scan · ack|retry|kill <kind> <id>)"),
    # ⚠️ TWO ENTRIES, NOT ONE. `/health` answers "is it working" (the same verdicts
    # `GET /api/health` serves, from `core.health`) and `/metrics` answers "how
    # fast, how often, how big" (`core.metrics`). Folding them would put a
    # percentile next to a fault and invite a reader to treat one as the other.
    ("/health",          "/health",       "Subsystem health — ✓/⚠/✗/○ per subsystem, then what to do about it"),
    ("/metrics",         "/metrics",      "Agent metrics — latency, tokens, failures, queue wait, context size"),
    ("/history",         "/history",      "Show last 10 messages"),
    ("/shrink",          "/shrink",       "Summarize & shrink history to save tokens"),
    ("/clear",           "/clear",        "Clear the screen (keeps history)"),
    ("/load",            "/load",         "Load the last conversation from THIS directory into this session"),
    ("/pause",            "/pause",        "Pause the current conversation (resume later with /resume)"),
    ("/resume",           "/resume",       "Resume a previous conversation with ↑/↓ picker (all projects)"),
    ("/clearhistory",    "/clearhistory", "Clear the conversation history"),
    ("/exit",            "/exit",         "Quit  (also Ctrl+C)"),
]


def print_help():
    cmds = [(tok, desc) for tok, _base, desc in SLASH_COMMANDS]
    if _RICH:
        t = Table(show_header=True, header_style=f"bold {P.ACCENT}",
                  box=rbox.SIMPLE_HEAD, border_style="dim")
        t.add_column("Command",     style=P.ACCENT2, no_wrap=True)
        t.add_column("Description", style="#c4c4dc")
        for cmd, desc in cmds:
            t.add_row(cmd, desc)
        _con.print(t)
        _con.print(f"  [dim]Tip: type [/][{P.ACCENT2}]/[/][dim] to see suggestions; keep typing to filter. "
                   f"While the agent works, type a message + Enter to queue it.[/]")
    else:
        print(f"\n{P.PU}{B}  Commands:{R}")
        for cmd, desc in cmds:
            print(f"  {P.CY}{cmd:<28}{R}{D}{desc}{R}")
        print(f"  {D}Tip: type / for suggestions; keep typing to filter. "
              f"Type during a turn to queue the next message.{R}")
        print()

def print_agent_reply(text: str):
    """Render agent markdown reply."""
    if _RICH:
        hr_style = "#1e1e30"
        _con.rule(style=hr_style)
        _con.print(f"  [bold {P.ACCENT}]⚡ Agent 2[/]  [dim]{datetime.now().strftime('%H:%M')}[/]")
        _con.print()
        _con.print(Markdown(text), style="#c4c4dc")
        _con.print()
    else:
        hr()
        print(f"  {P.PU}{B}⚡ Agent 2{R}  {D}{datetime.now().strftime('%H:%M')}{R}")
        print()
        _render_markdown_plain(text)
        print()

def _render_markdown_plain(text: str):
    in_code = False
    for line in text.splitlines():
        if line.startswith("```"):
            in_code = not in_code
            if in_code:  print(f"  {D}┌{'─' * 50}{R}")
            else:        print(f"  {D}└{'─' * 50}{R}")
            continue
        if in_code:
            print(f"  {YW}│ {line}{R}"); continue
        if   line.startswith("# "):   print(f"\n  {WH}{B}{line[2:]}{R}")
        elif line.startswith("## "):  print(f"\n  {P.CY}{B}{line[3:]}{R}")
        elif line.startswith("### "): print(f"  {P.PU}{line[4:]}{R}")
        elif re.match(r"^[-*] ", line): print(f"  {D}•{R} {line[2:]}")
        elif re.match(r"^\d+\. ", line):
            n, rest = line.split(". ", 1); print(f"  {P.PU}{n}.{R} {rest}")
        else:
            line = re.sub(r"\*\*(.+?)\*\*", f"{WH}{B}\\1{R}", line)
            line = re.sub(r"`(.+?)`",        f"{YW}\\1{R}", line)
            print(f"  {line}")

def _short_path(p: str) -> str:
    """Show a path relative to the CWD when possible (e.g. agent2/agent.py)."""
    if not p or p == "?":
        return p or "?"
    try:
        rel = os.path.relpath(str(Path(p).expanduser()), os.getcwd())
        # If the relative path escapes upward a lot, fall back to just the name.
        if not rel.startswith(".." + os.sep) and rel != "..":
            return rel.replace(os.sep, "/")
        return Path(p).name
    except Exception:
        return Path(p).name


def print_tool_call(name: str, desc: str, detail: str = ""):
    # NOTE: `name` is carried for the call-site signature only — neither branch
    # below renders a per-tool icon. A `{tool: emoji}` table used to be built here
    # on every call and then never read; it was removed rather than wired up,
    # because wiring it up would change what the terminal prints.
    if _RICH:
        body = Text()
        body.append(f" {desc}", style="bold #f0c060")
        if detail: body.append(f"\n   $ {detail}", style="#f0c060")
        _con.print(Panel(body, border_style="#2a2a40", padding=(0, 1)))
    else:
        print(f"\n  {YW}▶ {desc}{R}")
        if detail: print(f"  {YW}$ {detail}{R}")

def print_plan(title: str, steps: list):
    if _RICH:
        body = Text()
        body.append(f"{title}\n\n", style="bold white")
        for i, s in enumerate(steps, 1):
            body.append(f"  {i}. ", style="bold #7c6af7")
            body.append(f"{s}\n",   style="#c4c4dc")
        _con.print(Panel(body, title="[bold #7c6af7]📋 Plan[/]",
                         border_style="#3a2a70", padding=(0, 1)))
    else:
        print(f"\n  {P.PU}{B}📋 {title}{R}")
        for i, s in enumerate(steps, 1):
            print(f"  {P.PU}{i}.{R} {s}")
        print()


# ── Health + metrics (Tasks 27 · 28) ──────────────────────────────────────────
# ⚠️ THESE TWO DRAW; THEY DECIDE NOTHING. `core.health.report()` owns every
# verdict and `core.metrics.report()` owns every number, because both payloads are
# also served over HTTP and a renderer that re-tested "is the scheduler healthy"
# would be a second opinion the JSON could not see. What lives here is the mark,
# the colour and the column widths — presentation, which is each surface's own.

# The four state words `core.health` and `integrations.registry` both report, and
# the mark each one gets. ⚠️ `off` IS NOT AN ERROR: both MCP bridges ship
# auto-connect off and the WAL checkpointer can be disabled on purpose, so a
# renderer that knew only ✓/✗ would print a cross at a user who switched something
# off deliberately.
_HEALTH_MARKS = {"ok": ("✓", GR, "#3ddc84"),
                 "warn": ("⚠", YW, "#f0c060"),
                 "fail": ("✗", RD, "#ff5555"),
                 "off":  ("○", D,  "dim")}


def render_health(report: dict) -> bool:
    """Draw Task 28's verdict list. Returns False when there is nothing to draw.

    Prints one row per subsystem — `✓ Database`, `⚠ Gemini Provider` — then the
    `problems` and `warnings` lists, which are the *actionable* half: a mark says
    which subsystem, the line says what to do about it.
    """
    rows = (report or {}).get("sections") or []
    if not rows:
        return False
    healthy = bool((report or {}).get("ok"))
    problems = list((report or {}).get("problems") or [])
    warnings = list((report or {}).get("warnings") or [])

    if _RICH:
        t = Table(show_header=True, header_style=f"bold {P.ACCENT}",
                  box=rbox.SIMPLE_HEAD, border_style="dim")
        t.add_column("", width=1, no_wrap=True)
        t.add_column("Subsystem", style=P.ACCENT2, no_wrap=True)
        t.add_column("State",     style="#c4c4dc")
        for r in rows:
            mark, _plain, style = _HEALTH_MARKS.get(str(r.get("state")),
                                                    ("?", D, "dim"))
            t.add_row(f"[{style}]{mark}[/]", str(r.get("label") or "?"),
                      str(r.get("text") or ""))
        _con.print(t)
        _con.print(f"  [{'#3ddc84' if healthy else '#ff5555'}]"
                   f"{'✓ healthy' if healthy else '✗ unhealthy'}[/]"
                   f"  [dim]· {len(problems)} problem(s) · {len(warnings)} warning(s)[/]")
    else:
        print()
        for r in rows:
            mark, col, _style = _HEALTH_MARKS.get(str(r.get("state")), ("?", D, ""))
            label = str(r.get("label") or "?")
            text = str(r.get("text") or "")
            print(f"  {col}{mark}{R} {P.CY}{label:<20}{R}{D}{text}{R}")
        print(f"  {GR if healthy else RD}{'✓ healthy' if healthy else '✗ unhealthy'}{R}"
              f"  {D}· {len(problems)} problem(s) · {len(warnings)} warning(s){R}")

    for line in problems:
        status_line(line, "error")
    for line in warnings:
        status_line(line, "warning")
    return True


def render_metrics(report: dict) -> bool:
    """Draw Task 27's measurements. Returns False when nothing was recorded.

    Three blocks, because they are three different kinds of fact: the observation
    series (count · avg · p50 · p95 · max), the plain counters, and the `borrowed`
    section — LLM latency/errors from `router.stats()` and permission denials from
    `permissions.counters()`, forwarded rather than re-measured.

    ⚠️ It prints `report()["scope"]` verbatim. The series are per-process and the
    LLM ledger is install-wide; a reader who does not know that will average two
    incompatible windows in their head, and in dual mode the two halves hold two
    disjoint registries (the limit `core/diffs.DiffStore` documents).
    """
    rep = report or {}
    series = rep.get("series") or {}
    counters = rep.get("counters") or {}
    borrowed = rep.get("borrowed") or {}
    meta = rep.get("meta") or {}
    signals = rep.get("signals") or {}
    if not meta.get("enabled", True):
        status_line("Metrics are disabled (AGENT2_METRICS=0) — nothing is recorded.",
                    "warning")
        return False

    def _unit(sig: str) -> str:
        return str((signals.get(sig) or {}).get("unit") or "")

    def _fmt(val, unit) -> str:
        if val is None:
            return "—"
        if unit == "ms":
            return f"{val:,.1f} ms"
        return f"{val:,.0f}"

    if series:
        if _RICH:
            t = Table(show_header=True, header_style=f"bold {P.ACCENT}",
                      box=rbox.SIMPLE_HEAD, border_style="dim")
            for col, just in (("Signal", "left"), ("Label", "left"), ("N", "right"),
                              ("avg", "right"), ("p50", "right"), ("p95", "right"),
                              ("max", "right")):
                t.add_column(col, justify=just,
                             style=P.ACCENT2 if col == "Signal" else "#c4c4dc",
                             no_wrap=col in ("Signal", "Label"))
            for sig in sorted(series):
                unit = _unit(sig)
                for label, s in sorted((series[sig] or {}).items()):
                    t.add_row(sig, label, f"{s.get('count', 0):,}",
                              _fmt(s.get("avg"), unit), _fmt(s.get("p50"), unit),
                              _fmt(s.get("p95"), unit), _fmt(s.get("max"), unit))
            _con.print(t)
        else:
            print()
            for sig in sorted(series):
                unit = _unit(sig)
                for label, s in sorted((series[sig] or {}).items()):
                    print(f"  {P.CY}{sig:<18}{R}{D}{label:<16}{R}"
                          f"n={s.get('count', 0):<6} avg={_fmt(s.get('avg'), unit):>11}"
                          f"  p95={_fmt(s.get('p95'), unit):>11}")
    else:
        status_line("No measurements recorded yet in this process.", "info")

    if counters:
        pairs = [f"{sig}.{lab}={n:,}"
                 for sig in sorted(counters)
                 for lab, n in sorted((counters[sig] or {}).items())]
        status_line("  ".join(pairs), "info")

    llm = borrowed.get("llm") or {}
    if llm.get("total"):
        status_line(f"LLM (whole install): {llm.get('total', 0):,} call(s), "
                    f"{llm.get('failed', 0):,} failure(s), "
                    f"{llm.get('fallbacks', 0):,} fallback(s), "
                    f"avg {llm.get('avg_latency_ms', 0):,} ms", "info")
    perms = borrowed.get("permissions") or {}
    if perms.get("denied"):
        status_line(f"Permission denials: {perms.get('denied', 0):,}", "warning")

    if meta.get("dropped"):
        status_line(f"{meta['dropped']:,} observation(s) dropped "
                    "(unknown signal, borrowed signal, or a non-finite value).",
                    "warning")
    if meta.get("folded"):
        status_line(f"{meta['folded']:,} observation(s) folded into "
                    f"'~other' — a signal passed {meta.get('max_series', 0)} labels.",
                    "warning")
    scope = rep.get("scope") or {}
    if scope:
        line = (f"scope: series = {scope.get('series', '?')}; "
                f"llm = {scope.get('llm', '?')}")
        if _RICH:
            _con.print(f"  [dim]{line}[/]")
        else:
            print(f"  {D}{line}{R}")
    return True


# ── Project scan (Task 29) ────────────────────────────────────────────────────
# ⚠️ IT DRAWS; IT DETECTS NOTHING. `core.projectscan.scan()` owns every finding,
# because the same payload is served by `GET /api/project` and read by Tasks 30/31
# when they write `.agent2/agent2.md` — a renderer that re-derived "is this a
# Python project" would be a second answer the file and the JSON could not see.

def _scan_rows(rep: dict) -> list[tuple[str, str]]:
    """The (label, value) rows both branches print. One derivation, two bodies."""
    langs = rep.get("languages") or []
    tests = rep.get("tests") or {}
    build = rep.get("build") or {}
    git = rep.get("git") or {}
    a2 = rep.get("agent2") or {}

    rows: list[tuple[str, str]] = [("Root", str(rep.get("root") or "?"))]

    if langs:
        rows.append(("Languages", ", ".join(
            f"{lang.get('name')} {lang.get('share', 0)}%" for lang in langs[:5])))
    else:
        rows.append(("Languages", "none recognised"))

    # ⚠️ Spelled with `+` rather than an f-string nested inside an f-string:
    # reusing the outer quote (`f"…{f' ({x})'}…"`) is Python 3.12 syntax and
    # `pyproject.toml` targets 3.11, where the tidier form is a SyntaxError — one
    # that takes the whole CLI down at import, not just this renderer.
    mgrs = [str(m.get("label")) + (f" ({m.get('lockfile')})" if m.get("lockfile") else "")
            for m in (rep.get("package_managers") or [])]
    rows.append(("Package managers", ", ".join(mgrs) if mgrs else "none detected"))

    fws = rep.get("frameworks") or []
    if fws:
        rows.append(("Frameworks", ", ".join(str(f.get("name")) for f in fws)))

    eps = rep.get("entry_points") or []
    if eps:
        rows.append(("Entry points", ", ".join(
            f"{e.get('path')} ({e.get('why')})" for e in eps[:4])))

    # ⚠️ PATHS AND REASONS, NOT PROSE. What those files SAY about themselves is
    # printed below (and written into `## Important Files`); this row is the fact a
    # user needs to judge the scan — which files it opened, and on what grounds.
    # `0 read` is deliberately printable: `AGENT2_INIT_NOTE_FILES=0` is a supported
    # choice, and a row that vanished would look like a subsystem that broke.
    notes = rep.get("file_notes") or []
    if notes:
        rows.append(("Key files", f"{len(notes)} read · " + ", ".join(
            f"{n.get('path')} ({n.get('why')})" for n in notes[:4])))

    # ⚠️ A runner list is printed only when one was PROVED. `projectscan._tests`
    # leaves it empty when a file name admits several runners, and inventing one
    # here would print a test command that collects nothing.
    trun = ", ".join(tests.get("runners") or []) or "runner unknown"
    rows.append(("Tests", f"{tests.get('files', 0)} file(s) · "
                          f"{', '.join(tests.get('dirs') or []) or 'no test tree'} · {trun}"))

    if build.get("systems"):
        rows.append(("Build", ", ".join(build["systems"])))
    if rep.get("docs"):
        rows.append(("Docs", ", ".join(rep["docs"])))
    if rep.get("config_files"):
        rows.append(("Config", ", ".join(rep["config_files"][:8])))

    # ⚠️ `unknown` IS NOT `clean`. `gitstate` returns as soon as one of its three
    # subprocesses succeeds, so "repo, no branch, no commits" is indistinguishable
    # from a clean checkout — and printing "clean" there would be a fabricated fact.
    if not git.get("repo"):
        rows.append(("Git", "not a git repository (or git unavailable)"))
    elif git.get("unknown"):
        rows.append(("Git", "repository detected — state unknown (git did not answer)"))
    else:
        rows.append(("Git", git.get("describe") or "clean"))

    rows.append(("Structure", ", ".join(
        f"{d.get('path')}/ ({d.get('files')})" for d in (rep.get("structure") or [])[:6])
        or "no sub-directories"))

    rows.append((".agent2", "present" if a2.get("present") else "not created yet"))
    return rows


def render_project_scan(report: dict) -> bool:
    """Draw Task 29's project analysis. Returns False when there is nothing to draw."""
    rep = report or {}
    if not rep.get("root"):
        return False

    rows = _scan_rows(rep)
    cmds = rep.get("commands") or {}
    cmd_rows = [(kind, c) for kind in ("install", "dev", "build", "test", "lint", "other")
                for c in (cmds.get(kind) or [])]
    conventions = list(rep.get("conventions") or [])
    # ⚠️ `summary` COMES FROM THE PAYLOAD. `projectscan._note_summary` owns "the first
    # sentence of a note" for this block, the browser's Project panel and the
    # `## Important Files` section a later turn reads as fact — three printers, one
    # trim, so none of them can describe a file differently from the others.
    notes = [n for n in (rep.get("file_notes") or []) if n.get("summary")]
    head = (f"{rep.get('name') or '?'} · {rep.get('files', 0):,} file(s) · "
            f"{rep.get('dirs', 0):,} dir(s) · {rep.get('elapsed_ms', 0):,.0f} ms")

    if _RICH:
        _con.print(f"  [bold {P.ACCENT}]{head}[/]")
        t = Table(show_header=True, header_style=f"bold {P.ACCENT}",
                  box=rbox.SIMPLE_HEAD, border_style="dim")
        t.add_column("Aspect", style=P.ACCENT2, no_wrap=True)
        t.add_column("Finding", style="#c4c4dc")
        for label, value in rows:
            t.add_row(label, value)
        _con.print(t)
        if cmd_rows:
            ct = Table(show_header=True, header_style=f"bold {P.ACCENT}",
                       box=rbox.SIMPLE_HEAD, border_style="dim")
            ct.add_column("Kind", style=P.ACCENT2, no_wrap=True)
            ct.add_column("Command", style="#f0c060", no_wrap=True)
            ct.add_column("From", style="dim")
            for kind, c in cmd_rows:
                ct.add_row(kind, str(c.get("cmd") or ""), str(c.get("from") or ""))
            _con.print(ct)
        if notes:
            _con.print(f"  [bold {P.ACCENT}]What those files say about themselves[/]")
            for n in notes:
                _con.print(f"  [{P.ACCENT2}]{n.get('path')}[/] [dim]— {n.get('summary')}"
                           f"{' (cut)' if n.get('truncated') else ''}[/]")
        for note in conventions:
            _con.print(f"  [dim]•[/] {note}")
    else:
        print()
        print(f"  {P.PU}{B}{head}{R}")
        for label, value in rows:
            print(f"  {P.CY}{label:<18}{R}{D}{value}{R}")
        if cmd_rows:
            print()
            for kind, c in cmd_rows:
                print(f"  {P.CY}{kind:<10}{R}{YW}{c.get('cmd') or ''!s:<38}{R}"
                      f"{D}{c.get('from') or ''}{R}")
        if notes:
            print()
            print(f"  {P.PU}{B}What those files say about themselves{R}")
            for n in notes:
                cut = " (cut)" if n.get("truncated") else ""
                print(f"  {P.CY}{n.get('path')}{R} {D}— {n.get('summary')}{cut}{R}")
        for note in conventions:
            print(f"  {D}• {note}{R}")

    if not cmd_rows:
        status_line("No runnable commands were declared by this project.", "info")
    # ⚠️ Both of these print, and both are the point of the module's ceilings: a
    # partial scan that reads as a complete one is what Task 31 would write into a
    # committed file. See `core/projectscan.py`'s docstring.
    if rep.get("truncated"):
        status_line(f"Scan stopped early ({rep.get('truncated_by') or 'limit'}) — "
                    "this description covers only part of the project. "
                    "Raise AGENT2_INIT_MAX_FILES / _MAX_DEPTH / _BUDGET_SEC to see the rest.",
                    "warning")
    for err in (rep.get("errors") or [])[:6]:
        status_line(f"{err.get('where') or '?'}: {err.get('error') or '?'} "
                    "— that part of the analysis is absent, not empty.", "warning")
    return True


def render_project_doc(result: dict) -> bool:
    """Draw what `/init` wrote to `.agent2/` (Tasks 30–31).

    ⚠️ THE PRESERVED COUNT IS PRINTED EVEN WHEN IT IS THE ONLY LINE. "Kept N
    section(s) you wrote" is the sentence that tells a user their own prose
    survived a command whose name sounds like initialisation, and a renderer that
    printed it only when something changed would stay silent on exactly the run
    where the user is most likely to be worried about it.

    ⚠️ `changed: False` is reported as a success, not as a failure or a no-op:
    running `/init` twice on an unchanged project is *meant* to leave the file
    alone, and calling that "nothing happened" invites a user to go looking for
    the write that correctly did not occur.
    """
    res = result or {}
    if not res.get("doc") and not res.get("reason"):
        return False

    if not res.get("ok"):
        status_line(res.get("reason") or "could not write .agent2/", "warning")
        return True

    doc = res.get("doc") or ""
    added = list(res.get("added") or [])
    updated = list(res.get("updated") or [])
    preserved = list(res.get("preserved") or [])
    made = list(res.get("dirs_created") or []) + list(res.get("files_created") or [])

    if res.get("reason") == "dry run":
        verb = "would create" if res.get("created") else (
            "would update" if res.get("changed") else "is already current")
    elif res.get("created"):
        verb = "created"
    elif res.get("changed"):
        verb = "updated"
    else:
        verb = "already current"

    lines: list[tuple[str, str]] = [("Project doc", f"{doc} — {verb}")]
    if res.get("hint"):
        lines.append(("Stated purpose", str(res["hint"])))
    if made:
        lines.append(("Created", ", ".join(made)))
    if added:
        lines.append(("Sections added", ", ".join(added)))
    if updated:
        lines.append(("Sections refreshed", ", ".join(updated)))
    if preserved:
        lines.append(("Yours, untouched", ", ".join(preserved)))
    if res.get("bytes"):
        lines.append(("Size", f"{res['bytes']:,} bytes"))

    if _RICH:
        t = Table(show_header=False, box=rbox.SIMPLE, border_style="dim", pad_edge=False)
        t.add_column("k", style=P.ACCENT2, no_wrap=True)
        t.add_column("v", style="#c4c4dc")
        for label, value in lines:
            t.add_row(label, value)
        _con.print(t)
    else:
        print()
        for label, value in lines:
            print(f"  {P.CY}{label:<20}{R}{D}{value}{R}")

    if preserved:
        status_line(f"Kept {len(preserved)} section(s) you wrote — `/init` only "
                    "rewrites sections marked agent2:generated.", "info")
    # ⚠️ SAY WHEN NOBODY DESCRIBED THE PROJECT. Purpose, Features and Architecture
    # come from a model; with no key or no network they fall back to file counts and
    # placeholders, and a doc that looks authored but is not is exactly what a later
    # turn would read as fact. `describe_note` is why, in the module's own words.
    if res.get("describe_note"):
        status_line(f"Purpose/Features/Architecture were not written by a model "
                    f"({res['describe_note']}) — they say what is missing. "
                    "Fill them in, or re-run /init once a key is configured.",
                    "warning")
    elif res.get("described"):
        status_line("Described from the project's own evidence"
                    + (f" · {res['features']} feature(s) listed"
                       if res.get("features") else ""), "info")
    for err in (res.get("errors") or [])[:4]:
        status_line(str(err), "warning")
    if res.get("changed") and res.get("reason") != "dry run":
        status_line("Every later turn now reads this file as the project's own "
                    "description — edit it, and delete a marker to keep a section.",
                    "success")
    return True


# ── Skills (Phase 11, Tasks 32–36) ─────────────────────────────────────────────
# ⚠️ THE THREE STATE WORDS ARE THE SAME THREE `state.py` STORES, AND `auto` IS NOT
# `off`. A row with no choice recorded is *automatic* — it still applies when the
# request names it, `agent2.md` mentions it, or its own keywords match — and a
# renderer that printed that as "off" would tell a user their skill was disabled by
# the very screen they opened to check. Three states, three words, one mark each.
_SKILL_MARKS = {True: ("●", GR, "#3ddc84", "on"),
                False: ("○", RD, "#ff5555", "off"),
                None: ("·", D,  "dim",     "auto")}


def _skill_state_of(payload: dict, states: dict) -> bool | None:
    """The stored choice for this skill, as the tri-state `state.get()` returns."""
    val = (states or {}).get(str(payload.get("id") or ""))
    return val if val in (True, False) else None


def render_skills(catalog: dict, states: dict | None = None) -> bool:
    """Draw `/skills list` — every skill this project has, and its state.

    ⚠️ IT LISTS WHAT WAS FOUND, NOT WHAT A TURN SELECTED. The question a user opens
    this on is *why did my skill not fire*, and a table that showed only the four
    that reached the last prompt cannot answer it — that is `render_skill_selection`,
    which is a different screen for a different question.
    """
    cat = catalog or {}
    skills = list(cat.get("skills") or [])
    st = dict(states or {})
    root = str(cat.get("root") or ".agent2/skills")

    if not cat.get("enabled"):
        status_line("Skills are switched off in this process (AGENT2_SKILLS=0).", "warning")
    if not cat.get("exists"):
        status_line(f"No skills folder yet — {root} is created by /init. "
                    "Drop a SKILL.md (or any .md) in it and it is discovered.", "info")
        return True
    if not skills:
        status_line(f"{root} holds no readable skills yet.", "info")
        return True

    head = f"{len(skills)} skill(s) · {root}"
    rows = []
    for sk in skills:
        mark, col, style, word = _SKILL_MARKS[_skill_state_of(sk, st)]
        when = "always" if sk.get("always") else (
            ", ".join(list(sk.get("keywords") or [])[:4]) or "when named")
        rows.append((mark, col, style, word, str(sk.get("name") or sk.get("id") or "?"),
                     when, str(sk.get("origin") or ""), str(sk.get("rel") or "")))

    if _RICH:
        _con.print(f"  [bold {P.ACCENT}]{head}[/]")
        t = Table(show_header=True, header_style=f"bold {P.ACCENT}",
                  box=rbox.SIMPLE_HEAD, border_style="dim")
        t.add_column("", width=1, no_wrap=True)
        t.add_column("State",  style="#c4c4dc", no_wrap=True)
        t.add_column("Skill",  style=P.ACCENT2, no_wrap=True)
        t.add_column("Applies when", style="#c4c4dc")
        t.add_column("Format", style="dim", no_wrap=True)
        t.add_column("File",   style="dim")
        for mark, _col, style, word, name, when, origin, rel in rows:
            t.add_row(f"[{style}]{mark}[/]", word, name, when, origin, rel)
        _con.print(t)
    else:
        print()
        print(f"  {P.PU}{B}{head}{R}")
        for mark, col, _style, word, name, when, origin, rel in rows:
            print(f"  {col}{mark}{R} {D}{word:<5}{R}{P.CY}{name:<22}{R}"
                  f"{D}{when[:28]:<30}{origin:<12}{rel}{R}")

    if cat.get("truncated"):
        status_line(f"Discovery stopped early ({cat.get('truncated_by') or 'limit'}) — "
                    "this list covers only part of the folder. Raise "
                    "AGENT2_SKILLS_MAX / _MAX_DEPTH / _BUDGET_SEC to see the rest.",
                    "warning")
    if cat.get("skipped"):
        status_line(f"{cat['skipped']} folder(s) held no recognisable skill file "
                    "(SKILL.md, skill.md, AGENT.md, … or a single .md).", "info")
    for err in list(cat.get("errors") or [])[:4]:
        status_line(str(err), "warning")
    return True


def render_skill_selection(sel: dict, *, header: str = "") -> bool:
    """Draw one selection — Task 36's *report which skills applied*, and why not.

    ⚠️ THE OMITTED HALF IS PRINTED WITH THE SAME WEIGHT AS THE APPLIED HALF. Both
    come from one pass in `select.choose()` precisely so a user can read the reason
    their own skill lost, and a screen that dropped that half would answer the easy
    question and hide the one people actually ask.
    """
    payload = sel or {}
    applied = list(payload.get("applied") or [])
    omitted = list(payload.get("omitted") or [])
    if not applied and not omitted:
        return False

    _why = {"disabled": "off in this project", "shadowed": "another skill owns the name",
            "cap": f"past the {payload.get('limit') or '?'}-skill limit",
            "chars": f"past the {payload.get('max_chars') or '?'}-char budget",
            "empty": "the file says nothing"}

    if header:
        status_line(header, "info")
    if _RICH:
        if applied:
            t = Table(show_header=True, header_style=f"bold {P.ACCENT}",
                      box=rbox.SIMPLE_HEAD, border_style="dim")
            t.add_column("#", width=2, style="dim", no_wrap=True)
            t.add_column("Applied", style=P.ACCENT2, no_wrap=True)
            t.add_column("Because", style="#c4c4dc")
            t.add_column("Chars",   style="dim", justify="right", no_wrap=True)
            for i, a in enumerate(applied, 1):
                t.add_row(str(i), str(a.get("name") or a.get("id") or "?"),
                          str(a.get("label") or a.get("reason") or ""),
                          f"{a.get('chars', 0):,}")
            _con.print(t)
        for o in omitted[:12]:
            _con.print(f"  [dim]○ {o.get('name') or o.get('id')} — "
                       f"{_why.get(str(o.get('why')), str(o.get('why') or '?'))}"
                       f"{' (' + str(o.get('by')) + ')' if o.get('by') else ''}[/]")
    else:
        print()
        for i, a in enumerate(applied, 1):
            print(f"  {D}{i}.{R} {P.CY}{a.get('name') or a.get('id')!s:<22}{R}"
                  f"{D}{a.get('label') or a.get('reason') or ''}"
                  f"  ({a.get('chars', 0):,} chars){R}")
        for o in omitted[:12]:
            print(f"  {D}○ {o.get('name') or o.get('id')} — "
                  f"{_why.get(str(o.get('why')), str(o.get('why') or '?'))}{R}")

    if not applied:
        status_line("No skill applied — nothing matched, or every match is switched "
                    "off. /skills on <name> pins one to every prompt.", "info")
    else:
        status_line(f"{len(applied)} skill(s) · {payload.get('chars', 0):,} of "
                    f"{payload.get('max_chars', 0):,} chars · "
                    f"{payload.get('considered', 0)} considered", "info")
    if len(omitted) > 12:
        status_line(f"…and {len(omitted) - 12} more omitted.", "info")
    for err in list(payload.get("errors") or [])[:4]:
        status_line(str(err), "warning")
    return True


def render_skill_detail(skill: dict, state: bool | None = None) -> bool:
    """Draw one skill as Agent2 UNDERSTOOD it — Task 33's normalization, visible.

    The point is the gap: what the file says, versus what a Claude/Codex/Antigravity
    header turned into after `normalize()`. `unparsed` is the field that matters —
    it is how many header lines this build did not recognise, i.e. exactly what a
    vendor-specific skill silently loses here.
    """
    sk = skill or {}
    if not sk.get("id"):
        return False
    _mark, _col, _style, word = _SKILL_MARKS[state if state in (True, False) else None]
    lines: list[tuple[str, str]] = [
        ("Skill", f"{sk.get('name') or sk.get('id')}  ({sk.get('id')})"),
        ("State", {"on": "on — pinned into every prompt in this project",
                   "off": "off — never selected in this project",
                   "auto": "auto — selected when it matches this request"}[word]),
        ("File", f"{sk.get('rel') or ''}  ({sk.get('size', 0):,} bytes)"),
        ("Format", f"{sk.get('origin') or '?'}"
                   + (f" · {sk['manifest']}" if sk.get("manifest") else "")),
    ]
    if sk.get("description"):
        lines.append(("Description", str(sk["description"])))
    if sk.get("keywords"):
        lines.append(("Keywords", ", ".join(sk["keywords"])))
    lines.append(("Applies", "always — every prompt" if sk.get("always")
                  else "when named or when its keywords match"))
    if sk.get("priority"):
        lines.append(("Priority", f"{sk['priority']} (within its tier only)"))
    for key, label in (("version", "Version"), ("author", "Author"),
                       ("model", "Model"), ("tools", "Tools")):
        val = sk.get(key)
        if val:
            lines.append((label, ", ".join(val) if isinstance(val, (list, tuple)) else str(val)))

    if _RICH:
        t = Table(show_header=False, box=rbox.SIMPLE, border_style="dim", pad_edge=False)
        t.add_column("k", style=P.ACCENT2, no_wrap=True)
        t.add_column("v", style="#c4c4dc")
        for label, value in lines:
            t.add_row(label, value)
        _con.print(t)
    else:
        print()
        for label, value in lines:
            print(f"  {P.CY}{label:<14}{R}{D}{value}{R}")

    if sk.get("body_truncated"):
        status_line(f"The file is longer than AGENT2_SKILLS_MAX_BYTES — the prompt gets "
                    f"the first {sk.get('size', 0):,} bytes, not the whole skill.", "warning")
    if sk.get("unparsed"):
        status_line(f"{sk['unparsed']} header line(s) are not fields this build knows — "
                    "they are ignored, and the file is untouched.", "info")
    return True


# ── Workflows (Task 39) ───────────────────────────────────────────────────────

# ⚠️ A WORD THE PAYLOAD ALREADY CHOSE GETS A MARK HERE — IT IS NEVER COMPUTED.
# `_HEALTH_MARKS`' rule, for its reason: `runner.NodeState.phase` and `.state` are both
# derived from the `agent_tasks` row by the one reader that owns it, and a renderer that
# re-decided "is this node done" from `status` + `progress` would be a second answer to
# a question `runner.state_for()` already answers — visible only to somebody holding the
# payload and the screen side by side.
#
# ⚠️ IT IS TOTAL OVER **BOTH** VOCABULARIES, AND THAT IS NOT REDUNDANCY (Phase D3).
# A node payload carries two words: the five historical `runner.PHASE_*` phases that
# `GET /api/workflows` and `public/script.js` have read since Task 39, and the nine
# `dag.model.STATES` the engine actually stores. Four spellings are shared, so the extra
# rows are the five the phase table cannot express — and `_PHASE_FOR` folds `cancelled`
# and `skipped` into `failed` and `paused` into `blocked`, which is right for a
# five-word summary and wrong on a screen: a run somebody **cancelled** would be printed
# as a failure to diagnose, and a node a human **paused** as one waiting on an upstream.
# Choosing which of the two words to show is presentation; deriving either is not.
_WF_MARKS = {"done":      ("✓", GR, "#3ddc84"),
             "completed": ("✓", GR, "#3ddc84"),
             "failed":    ("✗", RD, "#ff5555"),
             "skipped":   ("⚠", YW, "#f1fa8c"),
             "cancelled": ("–", D,  "dim"),
             "running":   ("▸", YW, "#f1fa8c"),
             "paused":    ("•", YW, "#f1fa8c"),
             "ready":     ("○", WH, "#c4c4dc"),
             "blocked":   ("·", D,  "dim"),
             "pending":   ("…", D,  "dim")}


def _wf_mark(word: str) -> tuple[str, str, str]:
    """A phase **or** state word → its mark. An unknown word is dim, never a tick."""
    return _WF_MARKS.get(str(word or ""), ("?", D, "dim"))


def _wf_word(node: dict) -> str:
    """The word to print for one node — the engine's state when the payload carries
    one, else the historical phase.

    ⚠️ A FALLBACK, NOT A CHOICE PER SURFACE. `state` arrived in Phase D3 and `phase`
    predates it, so a payload built by an older reader (or hand-built by a test) still
    prints something true rather than an empty column — and both words come from the
    same row, so the two can never disagree about a node, only about how coarsely it is
    described.
    """
    return str(node.get("state") or node.get("phase") or "")


def render_workflows(catalog: dict) -> bool:
    """Draw `/workflow list` — the plans this project has, and which of them run.

    ⚠️ IT LISTS EVERY FILE, INCLUDING THE BROKEN ONES, and says what is wrong with
    each. `Catalog.runnable()` is the shorter list and it is the wrong screen: the
    question a user opens this on is *why can I not run my workflow*, and a table
    that quietly omitted the file with the cycle in it cannot answer that.
    """
    cat = catalog or {}
    files = list(cat.get("workflows") or [])
    root = str(cat.get("root") or ".agent2/workflows")

    if not cat.get("enabled"):
        status_line("Workflows are switched off in this process (AGENT2_WORKFLOWS=0) — "
                    "files are still listed, but none may be run.", "warning")
    if not cat.get("exists"):
        status_line(f"No workflows folder yet — {root}. "
                    "`/workflow new <name>` creates it and seeds a plan you can edit.",
                    "info")
        return True
    if not files:
        status_line(f"{root} holds no workflow files yet — "
                    "`/workflow new <name>` writes one.", "info")
        return True

    head = f"{len(files)} workflow(s) · {cat.get('runnable', 0)} runnable · {root}"
    rows = []
    for wf in files:
        ok = bool(wf.get("ok"))
        mark, col, style = (("✓", GR, "#3ddc84") if ok else ("✗", RD, "#ff5555"))
        note = str(wf.get("summary") or ("" if ok else "will not run"))
        sch = str(wf.get("schema") or "?")
        if wf.get("upgraded_from") is not None and wf.get("upgraded_from"):
            sch = f"{wf['upgraded_from']}→{sch}"
        rows.append((mark, col, style, str(wf.get("name") or "?"),
                     f"{wf.get('count', 0)}", sch, str(wf.get("rel") or ""), note))

    if _RICH:
        _con.print(f"  [bold {P.ACCENT}]{head}[/]")
        t = Table(show_header=True, header_style=f"bold {P.ACCENT}",
                  box=rbox.SIMPLE_HEAD, border_style="dim")
        t.add_column("", width=1, no_wrap=True)
        t.add_column("Workflow", style=P.ACCENT2, no_wrap=True)
        t.add_column("Nodes", style="#c4c4dc", justify="right", no_wrap=True)
        t.add_column("Schema", style="dim", no_wrap=True)
        t.add_column("File", style="dim", no_wrap=True)
        t.add_column("Verdict", style="#c4c4dc")
        for mark, _col, style, name, count, sch, rel, note in rows:
            t.add_row(f"[{style}]{mark}[/]", name, count, sch, rel, note)
        _con.print(t)
    else:
        print()
        print(f"  {P.PU}{B}{head}{R}")
        for mark, col, _style, name, count, sch, rel, note in rows:
            print(f"  {col}{mark}{R} {P.CY}{name:<20}{R}{D}{count:>3} nodes  "
                  f"schema {sch:<6}{rel:<24}{note[:40]}{R}")

    if cat.get("truncated"):
        status_line(f"Discovery stopped early ({cat.get('truncated_by') or 'limit'}) — "
                    "this list covers only part of the folder. Raise "
                    "AGENT2_WORKFLOW_MAX_FILES / _MAX_BYTES / _BUDGET_SEC.", "warning")
    for err in list(cat.get("errors") or [])[:4]:
        status_line(str(err), "warning")
    return True


def render_workflow_detail(payload: dict) -> bool:
    """Draw `/workflow show <name>` — the file as Agent2 UNDERSTOOD it.

    The point is the same as `render_skill_detail`'s: the gap between what the author
    wrote and what this build read. `unparsed`/`notes` is the field that matters —
    PyYAML is not a dependency, so a shape outside `loader.SUBSET` is *counted*
    rather than guessed at, and this is where a user sees which line was skipped.
    """
    wf = payload or {}
    if not wf.get("name"):
        return False
    gr = dict(wf.get("graph") or {})
    defn = dict(wf.get("definition") or {})
    lines: list[tuple[str, str]] = [
        ("Workflow", str(wf.get("name"))),
        ("File", f"{wf.get('rel') or ''}  ({wf.get('size', 0):,} bytes"
                 f", {wf.get('format') or 'yaml'})"),
        ("Runs", "yes" if wf.get("ok") else "no — see below"),
        ("Nodes", str(wf.get("count", 0))),
        ("Schema", str(wf.get("schema") or "?")
                   + (f"  (written for {wf['upgraded_from']}, upgraded on read)"
                      if wf.get("upgraded_from") else "")),
    ]
    if defn.get("description"):
        lines.append(("Description", str(defn["description"])))
    levels = list(gr.get("levels") or [])
    if levels:
        lines.append(("Order", "  →  ".join(
            "(" + ", ".join(str(n) for n in lv) + ")" for lv in levels[:8])
            + ("  →  …" if len(levels) > 8 else "")))
    if wf.get("declared_name"):
        lines.append(("Declared name", f"{wf['declared_name']} — the FILENAME wins"))

    if _RICH:
        t = Table(show_header=False, box=rbox.SIMPLE, border_style="dim", pad_edge=False)
        t.add_column("k", style=P.ACCENT2, no_wrap=True)
        t.add_column("v", style="#c4c4dc")
        for label, value in lines:
            t.add_row(label, value)
        _con.print(t)
    else:
        print()
        for label, value in lines:
            print(f"  {P.CY}{label:<14}{R}{D}{value}{R}")

    nodes = list(defn.get("nodes") or [])
    if nodes:
        if _RICH:
            t = Table(show_header=True, header_style=f"bold {P.ACCENT}",
                      box=rbox.SIMPLE_HEAD, border_style="dim")
            t.add_column("#", width=2, style="dim", no_wrap=True)
            t.add_column("Node", style=P.ACCENT2, no_wrap=True)
            t.add_column("Needs", style="dim", no_wrap=True)
            t.add_column("Does", style="#c4c4dc")
            for i, nd in enumerate(nodes, 1):
                t.add_row(str(i), str(nd.get("id") or "?"),
                          ", ".join(nd.get("needs") or []) or "—",
                          str(nd.get("title") or nd.get("instruction") or "")[:70])
            _con.print(t)
        else:
            for i, nd in enumerate(nodes, 1):
                print(f"  {D}{i}.{R} {P.CY}{nd.get('id'):<16}{R}"
                      f"{D}needs {', '.join(nd.get('needs') or []) or '—':<18}"
                      f"{str(nd.get('title') or '')[:44]}{R}")

    for p in list(wf.get("problems") or [])[:5]:
        status_line(str(p.get("message") or p.get("code")), "error")
    for p in list(gr.get("problems") or [])[:5]:
        status_line(str(p.get("message") or p.get("code")), "error")
    for w in list(wf.get("warnings") or [])[:4] + list(gr.get("warnings") or [])[:4]:
        status_line(str(w.get("message") or w.get("code")), "warning")
    if wf.get("unparsed"):
        status_line(f"{wf['unparsed']} line(s) are outside the YAML subset this build "
                    "reads — they were skipped, not guessed at. /workflow describe "
                    "lists the shapes that are supported.", "info")
    for note in list(wf.get("notes") or [])[:5]:
        status_line(str(note), "info")
    return True


def render_workflow_run(state: dict, *, header: str = "") -> bool:
    """Draw a run — every node, the state it is in, and which one the next turn will do.

    ⚠️ THE WORDS COME FROM THE PAYLOAD. `runner.state_for()` re-derives them from the
    `agent_tasks` rows on every read, which is what makes a killed run report what is
    true *now*; a renderer that cached or recomputed them would print the progress the
    run had at the moment it died.
    """
    st = state or {}
    nodes = list(st.get("nodes") or [])
    if not st.get("exists") and not nodes:
        return False

    if header:
        status_line(header, "info")
    cur = str(st.get("current") or "")
    done, total = st.get("done", 0), st.get("total", len(nodes))
    head = (f"{st.get('name') or '?'} · {done}/{total} settled"
            f"{' · ' + str(st.get('status')) if st.get('status') else ''}")

    if _RICH:
        _con.print(f"  [bold {P.ACCENT}]{head}[/]")
        t = Table(show_header=True, header_style=f"bold {P.ACCENT}",
                  box=rbox.SIMPLE_HEAD, border_style="dim")
        t.add_column("", width=1, no_wrap=True)
        t.add_column("Node", style=P.ACCENT2, no_wrap=True)
        t.add_column("State", style="#c4c4dc", no_wrap=True)
        t.add_column("Title", style="#c4c4dc")
        t.add_column("Waiting on", style="dim")
        for nd in nodes:
            word = _wf_word(nd)
            mark, _col, style = _wf_mark(word)
            nid = str(nd.get("node") or "?")
            t.add_row(f"[{style}]{mark}[/]", nid + ("  ←" if nid == cur else ""),
                      word, str(nd.get("title") or "")[:44],
                      ", ".join(nd.get("blocked_by") or []) or "")
        _con.print(t)
    else:
        print()
        print(f"  {P.PU}{B}{head}{R}")
        for nd in nodes:
            word = _wf_word(nd)
            mark, col, _style = _wf_mark(word)
            nid = str(nd.get("node") or "?")
            title = str(nd.get("title") or "")[:40]
            print(f"  {col}{mark}{R} {P.CY}{nid:<16}{R}{D}{word:<10}{title:<42}"
                  f"{', '.join(nd.get('blocked_by') or [])}{R}"
                  + (f" {YW}← next{R}" if nid == cur else ""))

    if st.get("error"):
        status_line(str(st["error"]), "error")
    failed = [str(n.get("node")) for n in nodes if n.get("phase") == "failed"]
    if failed:
        status_line("Failed node(s): " + ", ".join(failed)
                    + " — a node downstream of a failure still becomes ready, because "
                      "tasks.ready() releases on settled, not on succeeded.", "warning")
    return True


def _plan_row(label: str, body: str) -> None:
    """One `label  body` line of a plan, in whichever renderer is available.

    ⚠️ **THE GUTTER IS GUARANTEED, NOT A SIDE EFFECT OF THE COLUMN WIDTH.** This was
    `f"{label:<8}"` alone, and a field width only pads a label SHORTER than the field:
    `render_dag_plan`'s three labels are `Wave n`/`Next`/`Held`, so they always fit and
    the separation looked like a property of the format string. `render_dynamic_draft`'s
    label is `"1. " + a node id`, and a dynamic plan folds its ids out of the goal
    sentence — so the very first real plan printed
    `1. build-a-small-calculatorbuild a small calculator`, two facts run together with
    no error, no traceback and every existing assertion (`node.node in text`) still
    green. Padding to the column when it fits keeps those three rows byte-identical.
    """
    cell = f"{label:<8}" if len(label) < 8 else f"{label}  "
    if _RICH:
        _con.print(f"  [{P.ACCENT2}]{cell}[/][#c4c4dc]{body}[/]")
    else:
        print(f"  {P.CY}{cell}{R}{body}")


def render_dag_plan(payload: dict, *, header: str = "") -> bool:
    """Draw the **plan**: the waves a graph may run in, what may start now, and what
    held every other runnable node. Writes nothing and decides nothing.

    ⚠️ **FEATURE-AGNOSTIC, AND DELIBERATELY NOT `render_workflow_*`.** It reads the
    keys *any* `dag` run payload carries — `width`, `waves`, `next`, `held` — rather
    than a workflow object, because the spec's *Validate → Build → **Display plan** →
    Execute* step belongs to Workflow, Dynamic Workflow and UltraCode alike. A renderer
    per consumer is how three surfaces start describing one scheduler differently.

    ⚠️ **EVERY WORD IS THE SCHEDULER'S OWN.** The waves are
    `dag.store.GraphState.waves`, the slots are `schedule.plan_next().slots`, and a
    hold prints its `HOLD_CODES` code plus the `detail` the scheduler wrote —
    `schedule.py`'s rule that a surface *"renders from `HOLD_CODES` rather than a
    string it invents"*, enforced here by having no table that could drift from it.

    ⚠️ **AN EMPTY `next` IS SAID OUT LOUD, NEVER LEFT BLANK.** Nothing runnable is
    three different situations — the graph is finished, a ceiling binds, or an upstream
    closed the branch — and the held lines are the only place a user can tell which.
    """
    pay = payload or {}
    waves = [[str(n) for n in (w or [])] for w in (pay.get("waves") or [])]
    nxt = [str(n) for n in (pay.get("next") or [])]
    held = [h for h in (pay.get("held") or []) if isinstance(h, dict)]
    if not waves and not nxt and not held:
        return False

    total = pay.get("total", sum(len(w) for w in waves))
    width = pay.get("width", max((len(w) for w in waves), default=0))
    if header:
        status_line(header, "info")
    head = (f"Plan · {total} node(s) · {len(waves)} wave(s) · "
            f"up to {width} at once")
    if _RICH:
        _con.print(f"  [bold {P.ACCENT}]{head}[/]")
    else:
        print(f"  {P.PU}{B}{head}{R}")

    for i, wave in enumerate(waves[:8], 1):
        body = ", ".join(wave[:8])
        if len(wave) > 8:
            body += f", +{len(wave) - 8} more"
        _plan_row(f"Wave {i}", body)
    if len(waves) > 8:
        status_line(f"…and {len(waves) - 8} further wave(s) — `/workflow show <name>` "
                    "prints the whole order.", "info")

    _plan_row("Next", ", ".join(nxt[:8]) + (f", +{len(nxt) - 8} more" if len(nxt) > 8 else "")
                      if nxt else "nothing may start now")
    for hold in held[:6]:
        detail = str(hold.get("detail") or "")
        _plan_row("Held", f"{hold.get('node') or '?'} — {hold.get('code') or '?'}"
                          + (f" ({detail})" if detail else ""))
    if len(held) > 6:
        status_line(f"…and {len(held) - 6} further held node(s).", "info")
    return True


def render_dynamic_draft(payload: dict, *, header: str = "") -> bool:
    """Draw a `dynamic.Draft` — the plan a goal produced, before anything runs.

    ⚠️ **IT DERIVES NOTHING, AND `render_dag_plan` COULD NOT DO THIS JOB.** That
    renderer reads a *run*'s `waves`/`next`/`held`, which a draft has not got: nothing
    is instantiated yet, so there are no rows for the scheduler to plan over. What a
    draft has is `plan` — `dag.store.plan()`'s own `NodeView` list, already in
    dependency order — so this prints that list and the four accounting facts beside
    it, every one of them a key `Draft.to_payload()` chose.

    ⚠️ **`source` IS PRINTED WHENEVER IT IS NOT THE MODEL'S, AND `note` WITH IT.** A
    one-step plan is what `_ask()`'s four degradations produce, and it is *usable* —
    but a user who cannot tell "the model planned one step" from "no model answered"
    will read a working refusal as a stupid planner. `dynamic.py` names which of the
    four happened; this is the surface that shows it.

    ⚠️ **`renames` AND `dropped` ARE REPORTED, NEVER SILENT.** A model that spelled
    an id two ways, or depended on a step it never wrote, produces a graph that is
    not quite the one it described — and `_acyclic` already dropped the edge. Printing
    it is the difference between a plan a human can trust and one they cannot check.
    """
    pay = payload or {}
    nodes = [n for n in (pay.get("plan") or []) if isinstance(n, dict)]
    if not nodes and not pay.get("reason"):
        return False

    if header:
        status_line(header, "info")
    if not pay.get("ok"):
        status_line(str(pay.get("reason") or "The goal could not be planned."), "error")
        return True

    mode = str(pay.get("mode") or "")
    head = (f"Plan · {pay.get('steps', len(nodes))} node(s) · mode {mode or '?'}"
            + (f" · {pay.get('name')}" if pay.get("name") else ""))
    if _RICH:
        _con.print(f"  [bold {P.ACCENT}]{head}[/]")
    else:
        print(f"  {P.PU}{B}{head}{R}")

    for i, node in enumerate(nodes[:12], 1):
        title = str(node.get("title") or node.get("node") or "?")
        needs = [str(n) for n in (node.get("needs") or [])]
        after = f"  after {', '.join(needs[:4])}" if needs else ""
        _plan_row(f"{i}. {node.get('node') or '?'}", f"{title[:72]}{after}")
    if len(nodes) > 12:
        status_line(f"…and {len(nodes) - 12} further node(s).", "info")

    if str(pay.get("source") or "") != "model":
        why = str(pay.get("note") or "")
        tail = f" — {why}." if why else "."
        status_line(f"Planned from the goal itself, not by a model{tail}", "warning")
    for ren in (pay.get("renames") or [])[:4]:
        if isinstance(ren, dict):
            status_line(f"  Renamed `{ren.get('from')}` → `{ren.get('to')}` "
                        "(one id per node).", "info")
    for gone in (pay.get("dropped") or [])[:4]:
        if isinstance(gone, dict):
            status_line(f"  `{gone.get('step')}` no longer waits on "
                        f"`{gone.get('needs')}` — that step was never planned.", "warning")
    if pay.get("truncated"):
        status_line(f"Clipped at {pay.get('truncated_by') or 'a ceiling'} — "
                    "the plan is shorter than the model wrote.", "warning")
    for prob in (pay.get("problems") or [])[:4]:
        said = prob.get("message") or prob.get("code") if isinstance(prob, dict) else prob
        status_line(f"  {said}", "error")
    if pay.get("started"):
        status_line(f"Started — round {pay.get('rounds')} of "
                    f"{pay.get('max_rounds')}, {pay.get('rounds_left')} re-plan(s) left.",
                    "success")
    elif pay.get("runnable"):
        status_line("Nothing was written — this is the plan only.", "info")
    return True


# ⚠️ A VERDICT WORD THE PAYLOAD ALREADY CHOSE GETS A MARK HERE — `_HEALTH_MARKS`' rule
# for the third time in this file, and for its reason. `core.verify` decides; every
# surface maps. Note `unconfirmed` is `⚠`, never `✗`: *no measurable evidence* is not
# *contradicted*, and a node whose whole job was to read and reason records nothing —
# printing a cross at it is how a report stops being read (`health.py`'s `off`/`warn`).
_VERIFY_MARKS = {"confirmed":    ("✓", GR, "#3ddc84"),
                 "contradicted": ("✗", RD, "#ff5555"),
                 "unsuccessful": ("✗", RD, "#ff5555"),
                 "unconfirmed":  ("⚠", YW, "#f1fa8c"),
                 "open":         ("·", D,  "dim")}


def render_verification(report: dict, *, header: str = "") -> bool:
    """Draw a `core.verify.Report` — what the durable ledgers say each unit actually did.

    ⚠️ **THE VERDICT IS THE PAYLOAD'S; THIS PRINTS IT.** `verified` is the one boolean
    that licenses the word *Complete*, and it is computed in `core.verify` from
    `problems` **alone** — so a renderer that concluded "nothing looks wrong ⇒ verified"
    would be the second answer the spec's *"'Done' is not verification"* forbids.

    ⚠️ **`problems` AND `warnings` STAY TWO LISTS.** `health.py`'s rule: a warning is
    reported *beside* the verdict and may never influence it. Unconfirmed work is
    normal — a reasoning node leaves no ledger row — and folding the two would make
    every ordinary run read as a failure.
    """
    rep = report or {}
    findings = list(rep.get("findings") or [])
    problems = [str(p) for p in (rep.get("problems") or [])]
    warnings = [str(w) for w in (rep.get("warnings") or [])]
    if not findings and not problems and not warnings:
        return False

    if header:
        status_line(header, "info")
    counts = rep.get("counts") or {}
    tally = " · ".join(f"{n} {word}" for word, n in counts.items() if n)
    verdict = ("verified" if rep.get("verified") else
               "unverified" if rep.get("complete") else "still running")
    head = f"Verification · {verdict}" + (f" · {tally}" if tally else "")
    if _RICH:
        _con.print(f"  [bold {P.ACCENT}]{head}[/]")
    else:
        print(f"  {P.PU}{B}{head}{R}")

    for fnd in findings[:12]:
        mark, col, style = _VERIFY_MARKS.get(str(fnd.get("verdict") or ""),
                                             ("?", D, "dim"))
        ref = str(fnd.get("ref") or "?")[:14]
        word = str(fnd.get("verdict") or "")
        title = str(fnd.get("title") or "")[:38]
        if _RICH:
            _con.print(f"  [{style}]{mark}[/] [{P.ACCENT2}]{ref:<15}[/]"
                       f"[dim]{word:<13}[/][#c4c4dc]{title}[/]")
        else:
            print(f"  {col}{mark}{R} {P.CY}{ref:<15}{R}{D}{word:<13}{title}{R}")
    if len(findings) > 12:
        status_line(f"…and {len(findings) - 12} further unit(s).", "info")

    for line in problems[:6]:
        status_line(line, "error")
    if len(problems) > 6:
        status_line(f"…and {len(problems) - 6} further problem(s).", "error")
    for line in warnings[:4]:
        status_line(line, "warning")
    if len(warnings) > 4:
        status_line(f"…and {len(warnings) - 4} further warning(s).", "info")
    return True


