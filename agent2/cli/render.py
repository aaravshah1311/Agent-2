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
    ("/offline",         "/offline",      "Toggle offline PIL models (↑/↓ move · → toggle on/off · Esc done)"),
    ("/provider …",      "/provider",     "Manage custom API providers (add | list | use | del | test)"),
    # ⚠️ ONE MCP ENTRY, AND `/burp` IS DELIBERATELY NOT LISTED. It still works —
    # `agent2cli.cmd_burp` forwards to `/mcp burp` so nobody's muscle memory
    # breaks — but this table feeds both `/help` and the completer, and offering
    # a retired spelling there is how a deprecation never finishes.
    ("/mcp …",           "/mcp",          "MCP servers — bare /mcp opens the menu (↑↓ navigate · Space toggle · Enter apply · Esc cancel) · connect | disconnect | status | list · <server> config (url/port/key)"),
    ("/workspace [path]", "/workspace",   "Show or switch the active workspace (sandbox root; switching cancels running tasks)"),
    ("/cd <path>",       "/cd",           "Alias for /workspace — switch the active workspace"),
    ("/scan <path>",     "/scan",         "Scan and analyze an entire project directory"),
    ("/run <cmd>",       "/run",          "Run a shell command directly"),
    ("/read <file>",     "/read",         "Read a file's contents"),
    ("/search <query>",  "/search",       "Web search (DuckDuckGo)"),
    ("/memory",          "/memory",       "List all saved memories"),
    ("/addmem <text>",   "/addmem",       "Save a memory manually"),
    ("/tasks",           "/tasks",        "Show the current task list (persists across restarts)"),
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
