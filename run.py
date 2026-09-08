#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
Agent2 — Universal Launcher
Platforms: Windows (CMD/PowerShell) | macOS | Linux

Usage:
  python run.py               — setup + start
  python run.py --web         — setup + start Web Agent
  python run.py --cli         — setup + start CLI agent
  python run.py --dual        — setup + start DUAL mode (web in background, CLI in front)
  python run.py --addapi      — add another API key
  python run.py --update      — update to latest code (-up / -update also work)
  python run.py --reset       — wipe venv and reinstall
  python run.py --uninstall   — remove venv, keys, DB and global command
  python run.py -h            — Show this help menu

⚠️ THIS FILE IS THE PACKAGE MANIFEST, and is pinned by
.github/tests/test_run_setup.py — so importing it must stay side-effect free.

  * Every library the code can reach is installed, split across CORE_PACKAGES /
    FILEINTEL_PACKAGES / OPTIONAL_IMPORTS. `agent2/fileintel/` resolves its
    backends through `base.require("<module>", "<pip name>")` — a STRING handed
    to importlib, invisible to a grep-for-imports audit. The test AST-parses the
    real call sites and asserts BOTH directions, because an unrequired package
    is a download every user pays for.
  * The `require()` scan is asserted SEPARATELY from the comparison. An empty
    scan would make the coverage test vacuous — the tautology shape sabotage has
    found twice in this repo.
  * Every fileintel wheel stays in OPTIONAL_IMPORTS. `install_deps()` exits(1) on
    a required package it cannot install, so demoting one turns a flaky wheel
    build into a machine that cannot set Agent2 up at all.
  * requirements.txt must agree with these tables — Docker builds from the file
    and run.py from the lists, so disagreement means two different applications.
  * Platform-gated packages (docx2pdf, Windows-only) are asserted as GATED, not
    exempted, so deleting the `if IS_WIN:` append cannot masquerade as the
    exemption.

⚠️ THE GLOBAL LAUNCHER LIVES IN `~/.local/bin` ON EVERY OS
(what pip and pipx already use), as a `.bat` on Windows, deliberately not a
compiled `.exe`. `_candidate_bin_dirs()` still sweeps the legacy `~/.agent2/bin`:
a stale wrapper there points at a moved run.py and wins silently if it is earlier
on PATH. ⚠️ Its test CLEARS PATH FIRST — otherwise a machine with a pre-move
install passes even with the sweep deleted (caught by sabotage).
"""

import os, sys, re, subprocess, platform, shutil, time, threading, itertools
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────
ROOT      = Path(__file__).parent.resolve()
VENV      = ROOT / ".venv"
ENV_FILE  = ROOT / ".env"          # legacy — migrated into agent2.db, then retired
DB_FILE   = ROOT / "agent2.db"
APP_WEB   = ROOT / "agent2web.py"
APP_CLI   = ROOT / "agent2cli.py"
APP_DUAL  = ROOT / "agent2dual.py"

# ── Self-update / bootstrap source ─────────────────────────────────────────────
REPO_URL  = "https://github.com/aaravshah1311/Agent-2"
# Files/dirs that must SURVIVE an update (never wiped, never overwritten).
# ⚠️ `.env.env.migrated` is the LEGACY name and is listed on purpose: this
# installer no longer creates it, but a build shipped `_migrate_env_once()`
# renaming through `with_suffix(".env.migrated")`, which appends rather than
# replaces on a dotfile, so installs in the wild hold the user's legacy
# GEMINI_API_KEY under that name. `self_update()`'s prune step deletes any
# top-level item this set does not name — so leaving it out is silent credential
# loss on a machine that ran the old launcher. Honest limitation: the prune runs
# in the OLD process with the OLD set, so this protects an install that picked
# the fix up by `git pull` or a fresh `install.py`, not one whose very first
# `--update` carries it.
PRESERVE  = {
    "agent2.db", "agent2.db-wal", "agent2.db-shm", "agent2.db-journal",
    ".env", ".env.migrated", ".env.env.migrated",
}

OS_NAME = platform.system()   # Windows | Darwin | Linux
IS_WIN  = OS_NAME == "Windows"
IS_MAC  = OS_NAME == "Darwin"

VENV_PY  = VENV / ("Scripts/python.exe" if IS_WIN else "bin/python")
VENV_PIP = VENV / ("Scripts/pip.exe"    if IS_WIN else "bin/pip")

# (import_name, pip_name, display_label)
COMMON_PACKAGES = [
    ("google.genai",   "google-genai",   "google-genai"),
    ("mcp",            "mcp",            "MCP (Burp Suite bridge)"),
]

WEB_PACKAGES = [
    ("flask",          "flask",          "Flask"),
    ("flask_socketio", "flask-socketio", "Flask-SocketIO")
]

CLI_PACKAGES = [
    ("rich",           "rich",           "Rich (terminal UI)"),
    ("prompt_toolkit", "prompt_toolkit", "Prompt Toolkit (terminal input)"),
]

# File Intelligence System (agent2/fileintel/) — pure-Python / wheel-only libs
# for universal file processing. Installed on every run, but treated as OPTIONAL
# (see OPTIONAL_IMPORTS): each fileintel operation imports its library lazily and
# returns a clean "pip install X" hint if it's missing, so a wheel that fails to
# build must WARN and continue, never abort setup.
#
# ⚠️ THIS LIST MUST COVER EVERY `require(...)` CALL IN agent2/fileintel/.
# Those call sites resolve their library from a STRING via importlib, so they are
# invisible to any "grep for import" audit — five of them (xlrd, odfpy, rarfile,
# docx2pdf, pdf2image) were absent here and were therefore never installed: the
# feature simply reported "install X" forever, on a machine where setup claimed
# it had installed everything. `test_run_py_installs_every_fileintel_library`
# pins this list against the real call sites.
FILEINTEL_PACKAGES = [
    ("pypdf",              "pypdf",              "pypdf (PDF processing)"),
    ("docx",               "python-docx",        "python-docx (Word)"),
    ("openpyxl",           "openpyxl",           "openpyxl (Excel)"),
    ("pptx",               "python-pptx",        "python-pptx (PowerPoint)"),
    ("PIL",                "Pillow",             "Pillow (images)"),
    ("reportlab",          "reportlab",          "reportlab (→PDF rendering)"),
    ("markdown",           "markdown",           "Markdown (→HTML)"),
    ("bs4",                "beautifulsoup4",     "BeautifulSoup (HTML→text)"),
    ("charset_normalizer", "charset-normalizer", "charset-normalizer (encoding)"),
    ("py7zr",              "py7zr",              "py7zr (7z archives)"),
    ("pytesseract",        "pytesseract",        "pytesseract (OCR bridge)"),
    ("xlrd",               "xlrd",               "xlrd (legacy .xls)"),
    ("odf",                "odfpy",              "odfpy (OpenDocument)"),
    ("rarfile",            "rarfile",            "rarfile (RAR archives)"),
    ("pdf2image",          "pdf2image",          "pdf2image (PDF→image OCR)"),
]

# Installed on Windows only. docx2pdf drives Microsoft Word through COM, so it
# is useless — and its install is pure noise — anywhere else; the DOCX→PDF path
# already falls back to LibreOffice/reportlab on other platforms.
if IS_WIN:
    FILEINTEL_PACKAGES.append(
        ("docx2pdf",       "docx2pdf",           "docx2pdf (Word→PDF, needs Word)"))

# Import names that are NICE-TO-HAVE, not required. The CLI degrades gracefully
# when these are missing (see agent2cli.py: _RICH / _PTK fallbacks), and the
# fileintel libs each degrade to a runtime "install X" hint, so a failed install
# of any of these must WARN and continue — never abort the whole setup.
OPTIONAL_IMPORTS = {"rich", "prompt_toolkit"} | {imp for imp, _, _ in FILEINTEL_PACKAGES}

# ── Windows console fixes ──────────────────────────────────────────────────────
if IS_WIN:
    os.system("chcp 65001 >nul 2>&1")
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleMode(
            ctypes.windll.kernel32.GetStdHandle(-11), 7)
    except Exception: pass
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass

# ── ANSI helpers ───────────────────────────────────────────────────────────────
R  = "\033[0m";   B  = "\033[1m";  D  = "\033[2m"
GR = "\033[1;32m"; CY = "\033[1;36m"; YL = "\033[1;33m"
RD = "\033[1;31m"; MG = "\033[1;35m"; WH = "\033[1;37m"

g   = lambda t: f"{GR}{t}{R}"
y   = lambda t: f"{YL}{t}{R}"
r   = lambda t: f"{RD}{t}{R}"
c   = lambda t: f"{CY}{t}{R}"
w   = lambda t: f"{WH}{B}{t}{R}"
dim = lambda t: f"{D}{t}{R}"

# ── Layout helpers ─────────────────────────────────────────────────────────────
# One consistent visual grammar for every step, so setup reads as a single flow
# instead of a pile of ad-hoc prints:
#   section()  a titled rule that opens a step
#   item()     a status row inside a step  (ok / warn / err / info)
#   note()     an indented continuation line under an item
UI_W = 54

def section(title: str):
    print(f"\n  {CY}┌─ {WH}{B}{title}{R} {CY}{'─' * max(0, UI_W - len(title) - 4)}{R}")

def item(state: str, text: str, detail: str = ""):
    sym = {"ok": g("✓"), "warn": y("!"), "err": r("✗"),
           "info": c("·"), "run": c("»")}.get(state, c("·"))
    line = f"  {CY}│{R}  {sym}  {text}"
    if detail:
        line += f"  {dim(detail)}"
    print(line)

def note(text: str):
    print(f"  {CY}│{R}     {dim(text)}")

def endsection():
    print(f"  {CY}└{'─' * UI_W}{R}")

# ── Banner ─────────────────────────────────────────────────────────────────────
def banner(clear: bool = True):
    if clear:
        os.system("cls" if IS_WIN else "clear")
    print(f"{MG}")
    print(r"    _                    _   ____  ")
    print(r"   / \   __ _  ___ _ __ | |_|___ \ ")
    print(r"  / _ \ / _` |/ _ \ '_ \| __| __) |")
    print(r" / ___ \ (_| |  __/ | | | |_ / __/ ")
    print(r"/_/   \_\__, |\___|_| |_|\__|_____|")
    print(r"        |___/                      ")
    print(R)
    print(f"  {w('Autonomous Terminal Agent')}  {dim('v2.1')}")
    print(f"  {dim(OS_NAME + ' ' + platform.machine() + '  ·  Python ' + sys.version.split()[0])}")

# ── Spinner ────────────────────────────────────────────────────────────────────
SPIN = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"] if not IS_WIN else \
       ["-", "\\", "|", "/"]

# Animate only on a real terminal: '\r' doesn't erase when stdout is a file or a
# pipe, so a redirected setup log would fill with hundreds of spinner frames.
try:
    IS_TTY = sys.stdout.isatty()
except Exception:
    IS_TTY = False


def _spin_frames(label: str, done: threading.Event):
    """Shared spinner body for spin_run/spin_call."""
    if not IS_TTY:
        print(f"  {CY}│{R}  {c('»')}  {label} …", flush=True)
        done.wait()
        return
    for f in itertools.cycle(SPIN):
        if done.is_set():
            break
        print(f"\r  {CY}│{R}  {c(f)}  {label} …{' ' * 18}", end="", flush=True)
        time.sleep(0.08)


def _spin_end(label: str, ok: bool):
    sym = g("✓") if ok else r("✗")
    lead = "\r" if IS_TTY else ""
    print(f"{lead}  {CY}│{R}  {sym}  {label}{' ' * 18}", flush=True)

def spin_run(label: str, cmd: list) -> subprocess.CompletedProcess:
    """Run *cmd* under a spinner rendered as a section item. Collapses to a
    single ✓/✗ row when finished, so a long install leaves one tidy line."""
    done, box = threading.Event(), [None]
    def work():
        try:
            box[0] = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, encoding="utf-8", errors="replace")
        except Exception as e:
            box[0] = subprocess.CompletedProcess(cmd, 1, "", str(e))
        finally:
            done.set()
    threading.Thread(target=work, daemon=True).start()
    _spin_frames(label, done)
    res = box[0]
    _spin_end(label, bool(res and res.returncode == 0))
    return res


def spin_call(label: str, fn, done_label: str = None):
    """Run any callable under the same spinner grammar as spin_run().

    Exists so slow in-process work (the batched dependency probe) shows motion
    instead of a dead terminal. The worker thread is joined without a timeout, so
    unlike a timed input() it can never be orphaned.
    """
    done, box = threading.Event(), {}
    def work():
        try:
            box["val"] = fn()
        except Exception as e:
            box["err"] = e
        finally:
            done.set()
    t = threading.Thread(target=work, daemon=True)
    t.start()
    _spin_frames(label, done)
    t.join()
    ok = "err" not in box
    _spin_end(done_label or label, ok)
    if not ok:
        raise box["err"]
    return box.get("val")

# ── Step 1: Python ─────────────────────────────────────────────────────────────
def check_python():
    section("System")
    if sys.version_info < (3, 9):
        item("err", f"Python 3.9+ required (you have {sys.version.split()[0]})")
        endsection()
        sys.exit(1)
    item("ok", f"Python {sys.version.split()[0]}")
    item("ok", f"{OS_NAME} {platform.machine()}")
    endsection()

# ── Step 2: Venv ───────────────────────────────────────────────────────────────
def ensure_venv(reset=False) -> bool:
    """Make sure .venv exists. Returns True if it was created THIS run.

    A freshly-created venv has no packages, so callers use the return value to
    force a full dependency install even on a fast-path (flagged) launch.
    """
    # ── 🔒 Venv Reset Guard ──────────────────────────────────────────────────
    is_running_from_venv = str(VENV).lower() in sys.executable.lower()

    if reset:
        if is_running_from_venv:
            print(f"\n  {r('[ERR]')}  It couldn't be reset in env mode.")
            print(f"         Deactivate env or try in other terminal.\n")
            sys.exit(1)

        if VENV.exists():
            # Function to handle Windows read-only file locks
            def remove_readonly(func, path, excinfo):
                import stat
                try:
                    os.chmod(path, stat.S_IWRITE)
                    func(path)
                except Exception: pass

            section("Virtual Environment")
            try:
                print(f"  {CY}│{R}  {c('»')}  Removing old environment …", end="", flush=True)
                shutil.rmtree(VENV, onerror=remove_readonly)
                time.sleep(1)  # Wait for Windows file handles
                print(f"\r  {CY}│{R}  {g('✓')}  Old environment wiped          ")
            except Exception as e:
                print()
                item("err", f"Reset failed: {e}")
                endsection()
                sys.exit(1)

            for cache in ROOT.rglob("__pycache__"):
                shutil.rmtree(cache, ignore_errors=True)

            res = spin_run("Creating fresh virtual environment",
                           [sys.executable, "-m", "venv", str(VENV)])
            if res.returncode != 0:
                item("err", (res.stderr or "")[:300])
                endsection()
                sys.exit(1)
            endsection()
            return True

    # ── 🛠️ Venv Creation ─────────────────────────────────────────────────────
    if not VENV.exists():
        section("Virtual Environment")
        # Creation DOES use spin_run because it calls an external process
        res = spin_run("Creating fresh virtual environment",
                       [sys.executable, "-m", "venv", str(VENV)])
        if res.returncode != 0:
            item("err", (res.stderr or "")[:300])
            endsection()
            sys.exit(1)
        endsection()
        return True
    return False

# ── Step 3: Packages ───────────────────────────────────────────────────────────
def pkg_ok(name: str) -> bool:
    """Explicitly check if a package can be imported in the venv."""
    return subprocess.run(
        [str(VENV_PY), "-c", f"import {name}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    ).returncode == 0


# Probe run INSIDE the venv: try every name, print "1"/"0" per line in order.
# importlib.util.find_spec is used instead of a real import because it doesn't
# execute module bodies — much faster, and one heavy package (Pillow, pypdf)
# can't slow the whole scan down.
_PROBE_SRC = """
import sys, importlib.util
for n in sys.argv[1:]:
    try:
        ok = importlib.util.find_spec(n) is not None
    except Exception:
        ok = False
    sys.stdout.write("1" if ok else "0")
    sys.stdout.write("\\n")
"""


def pkg_status(names) -> dict:
    """Which of *names* are importable in the venv — in ONE subprocess.

    The old code called pkg_ok() once per package: ~15 sequential interpreter
    launches, several seconds on Windows, with nothing on screen. That is the
    "lag" at the Dependencies step — it was never pip being slow, just process
    spawn cost with no feedback. One probe collapses it to a single launch.

    Falls back to per-package probing only if the batch call fails outright, so
    a weird interpreter state degrades to "slow but correct".
    """
    names = list(names)
    if not names:
        return {}
    try:
        res = subprocess.run([str(VENV_PY), "-c", _PROBE_SRC, *names],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             text=True, encoding="utf-8", errors="replace")
        lines = (res.stdout or "").split()
        if res.returncode == 0 and len(lines) == len(names):
            return {n: lines[i] == "1" for i, n in enumerate(names)}
    except Exception:
        pass
    return {n: pkg_ok(n) for n in names}

def _required_imports(mode: str) -> list[str]:
    """Import names a given mode CANNOT start without.

    Optional/cosmetic libs (rich, prompt_toolkit, the fileintel wheels) are
    excluded — they degrade gracefully at runtime, so a missing one must not
    trigger a full reinstall on every launch.
    """
    names = [imp for imp, _, _ in COMMON_PACKAGES if imp not in OPTIONAL_IMPORTS]
    if mode in ("web", "all"):
        names += [imp for imp, _, _ in WEB_PACKAGES if imp not in OPTIONAL_IMPORTS]
    if mode in ("cli", "all"):
        names += [imp for imp, _, _ in CLI_PACKAGES if imp not in OPTIONAL_IMPORTS]
    return names


def deps_present(mode: str) -> bool:
    """One fast probe: can the venv import everything this mode needs?

    This is the fast path used by `--cli` / `--web` / `--dual`, which must start
    immediately instead of re-verifying every package. Returning False sends the
    caller into the full, chatty install_deps() path.

    It shares pkg_status()'s single find_spec probe rather than executing real
    imports. Importing for real meant paying flask + google.genai + socketio's
    module-body cost (~10s on Windows) on EVERY launch just to learn they exist;
    find_spec answers the same question from metadata. A package that resolves
    but is genuinely broken still surfaces — the app reports the ImportError on
    startup, which is where a corrupt install belongs, not silently reinstalled
    behind a spinner on every run.
    """
    if not VENV_PY.exists():
        return False
    names = _required_imports(mode)
    if not names:
        return True
    st = pkg_status(names)
    return all(st.get(n) for n in names)


def install_deps(mode="web"):
    section("Dependencies")

    # Dual mode and the default run need every interface's deps. The
    # file-intelligence libs are included for every mode (the agent's file tools
    # are shared by Web and CLI) but treated as optional, so a failed wheel build
    # warns and continues rather than blocking startup.
    packages = _mode_packages(mode)

    # One batched probe instead of one subprocess per package. Shown under a
    # spinner so the step never looks frozen, even on a cold filesystem cache.
    status = spin_call(f"Checking {len(packages)} packages",
                       lambda: pkg_status([imp for imp, _, _ in packages]),
                       done_label=f"{len(packages)} packages checked")

    missing = [p for p in packages if not status.get(p[0])]
    verified = len(packages) - len(missing)

    if verified:
        item("ok", f"{verified} package(s) already present")

    # pip itself is only worth upgrading when something actually needs installing
    # — on a warm venv (the common case) this saved a multi-second no-op.
    if missing:
        spin_call("Preparing installer", lambda: subprocess.run(
            [str(VENV_PY), "-m", "pip", "install", "--upgrade", "pip"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
            done_label="Installer ready")

    installed = 0
    for idx, (imp, pip_name, label) in enumerate(missing, 1):
        optional = imp in OPTIONAL_IMPORTS
        if not _pip_install(pip_name, f"{label}  {D}[{idx}/{len(missing)}]{R}") \
                or not pkg_ok(imp):
            if optional:
                # These are non-essential: rich/prompt_toolkit are cosmetic (the
                # CLI has plain-text fallbacks) and the fileintel libs degrade to
                # a runtime "install X" hint. Warn loudly but keep going so a
                # flaky wheel build or network blip never blocks the whole install.
                item("warn", f"{label} unavailable", "feature degrades gracefully")
                note(f"install later:  {VENV_PY} -m pip install {pip_name}")
                continue
            item("err", f"{label} is required but could not be installed")
            note(f"install manually:  {VENV_PY} -m pip install {pip_name}")
            endsection()
            sys.exit(1)
        installed += 1

    if installed:
        item("ok", f"{installed} package(s) installed")
    endsection()


def _mode_packages(mode: str) -> list:
    """Every package a mode wants — required AND optional."""
    packages = COMMON_PACKAGES[:]
    if mode == "web":
        packages += WEB_PACKAGES
    elif mode == "cli":
        packages += CLI_PACKAGES
    else:
        packages += WEB_PACKAGES + CLI_PACKAGES
    return packages + FILEINTEL_PACKAGES


def heal_optional_deps(mode: str) -> None:
    """Quietly install anything missing on an otherwise-good fast path.

    The fast path exists to start instantly, so this must stay cheap: ONE batched
    probe (~0.2s) and, in the overwhelmingly common case where nothing is
    missing, zero output. When something IS missing we fix it rather than
    degrading — a user shouldn't have to discover that their CLI lost its colours
    and then run a repair command by hand.

    Failures here are never fatal: these packages all degrade gracefully at
    runtime, and blocking a launch over a cosmetic library would be worse than
    the missing library.
    """
    try:
        packages = _mode_packages(mode)
        status = pkg_status([imp for imp, _, _ in packages])
        missing = [p for p in packages if not status.get(p[0])]
        if not missing:
            return
        section("Repairing Dependencies")
        item("warn", f"{len(missing)} package(s) missing — installing")
        fixed = 0
        for imp, pip_name, label in missing:
            if _pip_install(pip_name, label) and pkg_ok(imp):
                fixed += 1
            else:
                item("warn", f"{label} unavailable", "feature degrades gracefully")
        if fixed:
            item("ok", f"{fixed} package(s) restored")
        endsection()
    except Exception:
        # Self-healing is a convenience; it must never be the reason a launch
        # fails. The required-import probe already gated us getting here.
        pass


def _pip_install(pip_name: str, label: str, attempts: int = 3) -> bool:
    """Install one package, retrying transient network/handshake failures.

    Returns True on success. Not --quiet on the final attempt so a real build
    error (e.g. a missing compiler) is visible instead of a silent exit.
    """
    for i in range(1, attempts + 1):
        quiet = ["--quiet"] if i < attempts else []
        # Escalate on each retry: a plain retry fixes a network blip, but a
        # half-written wheel in pip's HTTP cache survives one, and a package
        # with no matching wheel needs permission to fall back to an sdist.
        extra = []
        if i == 2:
            extra = ["--no-cache-dir"]
        elif i >= 3:
            extra = ["--no-cache-dir", "--prefer-binary"]
        cmd = [str(VENV_PY), "-m", "pip", "install", pip_name,
               "--disable-pip-version-check", "--timeout", "60"] + extra + quiet
        suffix = "" if i == 1 else f" (retry {i}/{attempts})"
        res = spin_run(f"Installing {label}{suffix}", cmd)
        if res.returncode == 0:
            return True
        err = (res.stderr or res.stdout or "").lower()
        transient = any(k in err for k in (
            "timed out", "timeout", "connection", "handshake", "ssl",
            "temporary failure", "read timed out", "eof occurred",
            "reset by peer", "retries exceeded", "network",
            # Not network faults, but all fixed by the escalated retry flags
            # above — so they deserve another attempt rather than an instant fail.
            "cache", "corrupt", "bad zip", "checksum", "hash mismatch",
            "incomplete", "no matching distribution", "could not build",
            "permission denied", "access is denied", "being used by another"))
        if i < attempts and transient:
            print(f"  {y('[..]')}  {dim('Network hiccup — retrying in a moment ...')}")
            time.sleep(2 * i)
            continue
        if i >= attempts:
            snippet = (res.stderr or res.stdout or "").strip().replace("\n", " ")[:300]
            if snippet:
                print(f"  {dim(snippet)}")
        if not transient:
            break
    return False

# ── Step 4: API-key storage (agent2.db — no .env) ──────────────────────────────
# run.py runs on the *system* Python before the venv exists, so it talks to the
# SQLite DB directly with the stdlib. The schema matches agent2/database.py.
import sqlite3


def _db_conn():
    c = sqlite3.connect(str(DB_FILE))
    c.row_factory = sqlite3.Row
    return c


def _ensure_keys_table():
    try:
        c = _db_conn()
        c.execute("""CREATE TABLE IF NOT EXISTS api_keys (
                        label      TEXT PRIMARY KEY,
                        api_key    TEXT UNIQUE,
                        name       TEXT,
                        active     INTEGER DEFAULT 1,
                        created_at TEXT DEFAULT(datetime('now'))
                     )""")
        c.commit(); c.close()
    except Exception as e:
        print(f"  {y('[!]')}  Could not open agent2.db: {e}")


def _migrate_env_once():
    """Import any legacy .env GEMINI_API_KEY* into the DB, then retire the file.

    ⚠️ **`with_name`, NEVER `with_suffix` — `.env` IS ALL SUFFIX AND HAS NO STEM.**
    `Path('.env').suffix` is `''` and `.stem` is `'.env'`, so
    `with_suffix('.env.migrated')` APPENDS and yields `.env.env.migrated`. That
    shipped, and the failure was silent in the one direction that costs a user
    something: `PRESERVE` names `.env.migrated`, `self_update()`'s prune step
    deletes every top-level item `PRESERVE` does not name, and the retired file
    still holds the key the user typed in — so the backup of their credential was
    removed by the next `--update`, reported only as a count of "stale item(s)".
    The state also latches: after the rename `.env` is gone, so this function
    returns at its existence guard forever and never gets a second chance to name
    the file correctly. The same trap was fixed once in `uninstall()`; this is the
    writer, which is the copy that decides what is actually on disk.
    """
    if not ENV_FILE.exists():
        return
    try:
        found = []
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                if k.strip().startswith("GEMINI_API_KEY"):
                    found.append(v.strip().strip('"').strip("'"))
        for k in found:
            _db_add_key(k)
        ENV_FILE.rename(ENV_FILE.with_name(".env.migrated"))
        if found:
            print(f"  {g('[OK]')}  Migrated {len(found)} key(s) from .env into agent2.db")
    except Exception:
        pass


def _load_keys() -> list[str]:
    _ensure_keys_table()
    placeholder = "your_gemini_api_key_here"
    keys, seen = [], set()
    try:
        c = _db_conn()
        for row in c.execute("SELECT api_key FROM api_keys ORDER BY created_at, label"):
            v = (row["api_key"] or "").strip()
            if v and v != placeholder and len(v) > 10 and v not in seen:
                keys.append(v); seen.add(v)
        c.close()
    except Exception:
        pass
    return keys


def _db_add_key(key: str) -> bool:
    """Insert one key into the DB (dedup + smallest free integer label)."""
    key = (key or "").strip().replace(" ", "").replace("\n", "")
    if len(key) < 15:
        return False
    _ensure_keys_table()
    try:
        c = _db_conn()
        exists = c.execute("SELECT 1 FROM api_keys WHERE api_key=?", (key,)).fetchone()
        if exists:
            c.close(); return False
        used = {r["label"] for r in c.execute("SELECT label FROM api_keys")}
        n = 1
        while str(n) in used:
            n += 1
        c.execute("INSERT INTO api_keys(label, api_key, name, active) VALUES(?,?,?,1)",
                  (str(n), key, f"Key {n}"))
        c.commit(); c.close()
        return True
    except Exception:
        return False


AISTUDIO_URL = "https://aistudio.google.com/app/apikey"


def _open_aistudio() -> bool:
    """Open Google AI Studio's key page so a first-time user doesn't have to
    hunt for the URL. Best-effort: headless boxes, WSL without an X server and
    SSH sessions have no browser, so a failure just leaves the printed link.
    Honours AGENT2_NO_BROWSER, same as the web/dual launch path."""
    if os.environ.get("AGENT2_NO_BROWSER"):
        return False
    try:
        import webbrowser
        # Only auto-open when a real browser is registered — otherwise
        # webbrowser falls back to a text browser or prints to our console
        # and scribbles over the setup UI.
        return bool(webbrowser.get()) and webbrowser.open(AISTUDIO_URL, new=2)
    except Exception:
        return False


def _prompt_key(num: int) -> str:
    while True:
        try:
            key = input(f"  {CY}│{R}  {CY}>>>{R} API key #{num}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print(); return ""
        if not key:
            return ""
        key = key.replace(" ", "").replace("\n", "")
        if len(key) < 15:
            item("warn", "Key too short — check and retry")
            continue
        return key

# ── Step 4a: Setup (first run, no keys) ───────────────────────────────────────
def has_keys() -> bool:
    """Cheap yes/no used by the fast path — never prints, never prompts."""
    _ensure_keys_table()
    _migrate_env_once()
    return bool(_load_keys())


def ensure_keys(force_add=False):
    """Show key status, and prompt for keys only when there are none (or --addapi).

    Two deliberate behaviours:
      · No keys at all → open Google AI Studio in the browser and wait for a
        paste. Without a key Agent2 cannot answer anything, so this is the one
        moment where interrupting the user is right.
      · First key just added → offer ONE failover key. Gemini free tier quotas
        are per-key, and the KeyRotator only has something to rotate to if a
        second key exists. Asked once, on first setup, never again on later
        startups (that old always-on question was answered "no" every time).
    Key VALUES are never printed; only a masked count, so a shoulder-surfer or a
    pasted terminal log can't leak them.
    """
    section("Gemini API Keys")
    _ensure_keys_table()
    _migrate_env_once()
    keys = _load_keys()

    if keys and not force_add:
        item("ok", f"{len(keys)} key(s) configured", "•••• hidden")
        if len(keys) == 1:
            note("python run.py --addapi   add a failover key for quota limits")
        else:
            note("python run.py --addapi   to add another")
        endsection()
        return

    first_run = not keys
    if first_run:
        item("warn", "No API key configured — Agent2 needs one to run")
        if _open_aistudio():
            item("ok", "Opened Google AI Studio in your browser")
        else:
            item("info", "Get a free key at:", AISTUDIO_URL)
        note("Sign in → 'Create API key' → copy it → paste below")
        note(f"link:  {AISTUDIO_URL}")
    else:
        note("Enter keys one at a time. Blank line to finish.")
    print()

    # first_run asks for exactly one key, then offers ONE failover key.
    # --addapi keeps looping until a blank line, as it always has.
    while len(keys) < 9:
        num = len(keys) + 1
        key = _prompt_key(num)
        if not key:
            break
        if key in keys:
            item("warn", "Duplicate — skipping")
            continue
        if not _db_add_key(key):
            item("warn", "Could not save key (duplicate or too short)")
            continue
        keys.append(key)
        item("ok", f"Key #{num} saved")

        if first_run:
            if len(keys) >= 2:
                break
            # Failover offer — Gemini's free quota is per-key and the
            # KeyRotator only has somewhere to rotate to if a 2nd key exists.
            note("Tip: a 2nd key doubles your free quota — Agent2 rotates to it")
            note("     automatically when the first one hits its limit.")
            try:
                more = ask_timed(f"  {CY}│{R}  {CY}>>>{R} Add a failover key now? "
                                 f"{D}[y/N]{R} ", timeout=15.0)
            except (EOFError, KeyboardInterrupt):
                more = None
            if (more or "").strip().lower() not in ("y", "yes"):
                break

    if len(keys) >= 9:
        note("Maximum of 9 keys reached.")

    if not keys:
        item("warn", "No keys — Agent2 will show setup instructions at runtime")
        note(f"add one later:  python run.py --addapi     ({AISTUDIO_URL})")
    else:
        stored = len(_load_keys())
        item("ok", f"{stored} key(s) stored in agent2.db")
        if stored == 1:
            note("python run.py --addapi   add a failover key anytime")
    endsection()

# ── Step 4b: /addapi  — add key interactively, persist to agent2.db ────────────
def add_api():
    banner()
    section("Add Gemini API Key")
    _ensure_keys_table()
    _migrate_env_once()
    keys = _load_keys()
    item("info", f"{len(keys)} key(s) currently stored", "•••• hidden")
    note("Free key:  https://aistudio.google.com/app/apikey")
    print()

    num = len(keys) + 1
    while num <= 9:
        key = _prompt_key(num)
        if not key:
            break
        key = key.replace(" ", "")
        if len(key) < 15:
            item("warn", "Too short"); continue
        if key in keys:
            item("warn", "Already saved"); continue
        if _db_add_key(key):
            keys.append(key)
            item("ok", f"Key #{num} saved to agent2.db")
            num += 1
        else:
            item("warn", "Could not save key"); continue
        if num > 9:
            note("Maximum of 9 keys reached.")
            break
        try:
            ans = input(f"  {CY}│{R}  {CY}>>>{R} Add another? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print(); break
        if ans != "y":
            break

    item("ok", f"Total keys in agent2.db: {len(_load_keys())}")
    endsection()

def uninstall():
    """Wipe everything: .venv, .env, agent2.db, __pycache__, and global command"""
    banner()
    print(f"  {r('[ WARNING ]')}  {w('This will delete all keys, data, the environment, and global commands.')}")
    try:
        ans = input(f"  {CY}>>>{R} Are you absolutely sure? [y/N]: ").strip().lower()
    except KeyboardInterrupt:
        print(f"\n  {g('[OK]')}  Uninstall aborted.")
        return

    if ans != 'y':
        print(f"\n  {g('[OK]')}  Uninstall aborted.")
        return

    # Files to wipe. ⚠️ BOTH retired-.env names are spelled out, and the pair is
    # the point. `ENV_FILE.with_suffix(".env.migrated")` looks like the obvious way
    # to name the sibling and yields `.env.env.migrated`, because `.env` is all
    # suffix and has no stem for `with_suffix` to replace — and BOTH this list and
    # `_migrate_env_once()` were written that way, so the two bugs cancelled and
    # uninstall deleted the right file by accident. Correcting only this half broke
    # that symmetry: it named a file the writer never created while leaving the one
    # it did. So the writer now produces `.env.migrated` (the name `PRESERVE` has
    # always claimed to protect) and the legacy spelling stays here, because an
    # install that ran the old launcher still has the user's legacy key under it and
    # "remove everything Agent2 put here" has to mean it. The WAL and SHM sidecars
    # are listed for the same reason: deleting `agent2.db` alone leaves SQLite's
    # journal behind for the next install to find.
    to_delete = [VENV, ENV_FILE, ROOT / ".env.migrated",
                 ROOT / ".env.env.migrated", DB_FILE,
                 *(DB_FILE.with_name(DB_FILE.name + s)
                   for s in ("-wal", "-shm", "-journal"))]

    for path in to_delete:
        if path.exists():
            try:
                if path.is_dir():
                    shutil.rmtree(path, onerror=lambda func, p, _: (os.chmod(p, 0o777), func(p)))
                else:
                    path.unlink()
                print(f"  {g('[OK]')}  Deleted: {dim(path.name)}")
            except Exception as e:
                print(f"  {y('[!]')}  Could not delete {path.name}: {e}")

    # Clean up global command everywhere it may live (primary + legacy + PATH),
    # so a stale wrapper can't linger after an uninstall.
    for bin_dir in _candidate_bin_dirs():
        for cmd in ["agent2", "agent2.bat", "agent2.cmd"]:
            cmd_path = bin_dir / cmd
            if cmd_path.exists():
                try:
                    cmd_path.unlink()
                    print(f"  {g('[OK]')}  Deleted global command: {dim(str(cmd_path))}")
                except Exception:
                    pass

    # Clean up python caches
    for cache in ROOT.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)

    print(f"\n  {MG}{B}Agent2 has been fully uninstalled.{R}\n")
    sys.exit(0)

# ── Self-update / bootstrap ────────────────────────────────────────────────────
def _rm_path(path: Path):
    """Delete a file or directory, defeating Windows read-only locks."""
    def _onerror(func, p, _exc):
        import stat
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except Exception:
            pass
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, onerror=_onerror)
    else:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except PermissionError:
            import stat
            os.chmod(path, stat.S_IWRITE)
            path.unlink()


def git_exe():
    """Locate a usable git executable, checking PATH then common install dirs."""
    found = shutil.which("git")
    if found:
        return found
    if IS_WIN:
        candidates = [
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "cmd" / "git.exe",
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Git" / "cmd" / "git.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Git" / "cmd" / "git.exe",
        ]
    else:
        candidates = [Path("/usr/bin/git"), Path("/usr/local/bin/git"), Path("/opt/homebrew/bin/git")]
    for cnd in candidates:
        if cnd and cnd.exists():
            return str(cnd)
    return None


def ensure_git(required: bool = True):
    """Return a path to git, installing it first if it is missing.

    If `required` is True (default) and git cannot be made available, print the
    manual-install hint and exit. If `required` is False, return None instead so
    the caller can fall back to a git-less path (e.g. ZIP download)."""
    print(f"\n  {w('[ Git ]')}\n")
    exe = git_exe()
    if exe:
        print(f"  {g('[OK]')}  git found  {dim(exe)}")
        return exe

    print(f"  {y('[!]')}  git not found — attempting automatic install ...")
    if IS_WIN:
        installer = None
        if shutil.which("winget"):
            installer = ["winget", "install", "--id", "Git.Git", "-e",
                         "--source", "winget", "--accept-source-agreements",
                         "--accept-package-agreements"]
        elif shutil.which("choco"):
            installer = ["choco", "install", "git", "-y"]
    elif IS_MAC:
        installer = ["brew", "install", "git"] if shutil.which("brew") else None
    else:
        if shutil.which("apt-get"):
            installer = ["sudo", "apt-get", "install", "-y", "git"]
        elif shutil.which("dnf"):
            installer = ["sudo", "dnf", "install", "-y", "git"]
        elif shutil.which("yum"):
            installer = ["sudo", "yum", "install", "-y", "git"]
        elif shutil.which("pacman"):
            installer = ["sudo", "pacman", "-S", "--noconfirm", "git"]
        else:
            installer = None

    if not installer:
        print(f"  {y('[!]')}  Could not find a package manager to install git.")
        if not required:
            return None
        print(f"  {r('[ERR]')}  git is required for this operation.")
        if IS_WIN:
            print(f"         {dim('Install git manually:')} {c('https://git-scm.com/download/win')}")
        elif IS_MAC:
            print(f"         {dim('Install Homebrew first:')} {c('https://brew.sh')}")
        else:
            print(f"         {dim('Install git with your distro package manager, then retry.')}")
        sys.exit(1)

    res = spin_run("Installing git", installer)
    if res.returncode != 0:
        print(f"  {y('[!]')}  git install failed: {dim((res.stderr or '')[:300])}")
        if not required:
            return None
        print(f"  {r('[ERR]')}  {dim('Install git manually and retry:')} {c('https://git-scm.com/downloads')}")
        sys.exit(1)

    exe = git_exe()
    if not exe:
        if not required:
            return None
        print(f"  {g('[OK]')}  git installed — but not visible in this session.")
        print(f"         {dim('Open a NEW terminal and run the command again.')}")
        sys.exit(0)
    print(f"  {g('[OK]')}  git installed  {dim(exe)}")
    return exe


def _clone_repo(git: str, dest: Path):
    """Shallow-clone REPO_URL into *dest*. Returns CompletedProcess."""
    return spin_run("Downloading latest code",
                    [git, "clone", "--depth", "1", REPO_URL, str(dest)])


# Branch fetched by the ZIP fallback (must match the repo's default branch).
REPO_BRANCH = "main"


def _zip_urls() -> list[str]:
    """Candidate ZIP endpoints for REPO_URL (codeload is fastest; github.com
    archive is the documented fallback). Both yield a single top-level folder."""
    slug = REPO_URL.rstrip("/").replace("https://github.com/", "").replace(".git", "")
    return [
        f"https://codeload.github.com/{slug}/zip/refs/heads/{REPO_BRANCH}",
        f"https://github.com/{slug}/archive/refs/heads/{REPO_BRANCH}.zip",
    ]


def _download_zip_snapshot(dest: Path) -> tuple[bool, str]:
    """Download the repo as a ZIP (no git needed) and extract it INTO *dest* so
    the layout matches a `git clone` (run.py at the top level). Retries transient
    network/handshake errors and tries each candidate URL. Returns (ok, error)."""
    import io, zipfile, tempfile
    import urllib.request, urllib.error, ssl

    ctx = ssl.create_default_context()
    last_err = ""
    for url in _zip_urls():
        for attempt in range(1, 4):
            box = {"data": None, "err": ""}

            def _fetch():
                try:
                    req = urllib.request.Request(
                        url, headers={"User-Agent": "Agent2-installer",
                                      "Accept": "application/zip"})
                    with urllib.request.urlopen(req, timeout=90, context=ctx) as r:
                        box["data"] = r.read()
                except urllib.error.HTTPError as e:
                    box["err"] = f"HTTP {e.code}"
                except Exception as e:                     # URLError, SSL, timeout, reset
                    box["err"] = str(getattr(e, "reason", e) or e)

            done = threading.Event()
            def _work():
                try: _fetch()
                finally: done.set()
            threading.Thread(target=_work, daemon=True).start()
            for f in itertools.cycle(SPIN):
                if done.is_set(): break
                tag = "" if attempt == 1 else f" (retry {attempt}/3)"
                print(f"\r  [{GR}{f}{R}]  Downloading ZIP snapshot{tag} ...", end="", flush=True)
                time.sleep(0.10)

            data, err = box["data"], box["err"]
            if data:
                print(f"\r  {g('[OK]')}  Downloaded ZIP snapshot ({len(data)//1024} KB)      ")
                try:
                    with zipfile.ZipFile(io.BytesIO(data)) as zf:
                        tmp = Path(tempfile.mkdtemp(prefix="agent2_zip_", dir=str(dest.parent)))
                        zf.extractall(tmp)
                        # The archive wraps everything in one top folder (e.g.
                        # Agent-2-main/). Move THAT folder's contents to dest.
                        tops = [p for p in tmp.iterdir() if p.is_dir()]
                        root = tops[0] if len(tops) == 1 else tmp
                        if not (root / "run.py").exists():
                            # Fallback: search for the folder that holds run.py.
                            hits = list(tmp.rglob("run.py"))
                            if hits:
                                root = hits[0].parent
                        shutil.move(str(root), str(dest))
                        shutil.rmtree(tmp, ignore_errors=True)
                    return (True, "")
                except Exception as e:
                    last_err = f"ZIP extract failed: {e}"
                    return (False, last_err)

            print(f"\r  {y('[!]')}  ZIP download failed{(': ' + err) if err else ''}        ")
            last_err = err or "download failed"
            low = last_err.lower()
            transient = any(k in low for k in (
                "timed out", "timeout", "handshake", "ssl", "reset",
                "connection", "temporary", "eof"))
            if attempt < 3 and transient:
                time.sleep(2 * attempt)
                continue
            break  # non-transient (e.g. HTTP 404) — try the next URL
    return (False, last_err or "could not download ZIP")


def fetch_snapshot(snapshot: Path) -> tuple[bool, str]:
    """Populate *snapshot* with a fresh copy of the repo, preferring git and
    falling back to a ZIP download when git is unavailable or the clone fails.
    Returns (ok, error). On success `snapshot` contains run.py at its top level.

    This owns ALL acquisition logic — it will try to auto-install git, then try
    a shallow clone, then a ZIP download — so a machine with no git (and no way
    to install it) still gets a working install."""
    git = ensure_git(required=False)   # prints the [ Git ] header; may auto-install
    if git:
        res = _clone_repo(git, snapshot)
        if res.returncode == 0 and _validate_snapshot(snapshot):
            return (True, "")
        print(f"  {y('[!]')}  git clone failed — falling back to ZIP download.")
        if snapshot.exists():
            _rm_path(snapshot)
    else:
        print(f"  {y('[!]')}  Proceeding without git — downloading a ZIP instead.")

    ok, err = _download_zip_snapshot(snapshot)
    if ok and _validate_snapshot(snapshot):
        return (True, "")
    return (False, err or "downloaded copy is incomplete")


def _validate_snapshot(src: Path) -> bool:
    """A fresh checkout is only trusted if it carries the launcher itself."""
    return (src / "run.py").exists()


def _skip_top_level(name: str) -> bool:
    """Items in ROOT that an update must never touch."""
    return name in PRESERVE or name in {".git", ".venv"}


def self_update():
    """Replace all local code with the latest from REPO_URL, preserving the DB.

    Strategy is fail-safe: download + validate a complete new copy FIRST, then
    overlay it on top of the current install, and only afterwards prune stale
    files. The app is never left in a half-deleted state — if the download step
    fails, nothing on disk has changed yet.
    """
    banner()
    print(f"  {w('[ Update Agent2 ]')}")
    print(f"  {dim('Source:')} {c(REPO_URL)}")
    print(f"  {dim('Target:')} {dim(str(ROOT))}")
    print(f"  {dim('Preserved:')} {g('agent2.db')} {dim('(your keys, data & settings)')}\n")

    # 1) Download into a temp staging dir (sibling of ROOT so os.replace is cheap).
    #    fetch_snapshot() handles git-or-ZIP acquisition and prints the [ Git ] step.
    import tempfile
    staging = Path(tempfile.mkdtemp(prefix="agent2_update_", dir=str(ROOT.parent)))
    snapshot = staging / "snapshot"
    try:
        ok, err = fetch_snapshot(snapshot)
        if not ok:
            print(f"  {r('[ERR]')}  Download failed: {dim(err[:300])}")
            print(f"  {g('[OK]')}  Nothing was changed. Your current install is intact.")
            return

        # Drop the cloned .git — we don't want to convert the user's install
        # into a checkout of the Agent-2 repo.
        clone_git = snapshot / ".git"
        if clone_git.exists():
            _rm_path(clone_git)

        new_names = {p.name for p in snapshot.iterdir()}

        # 2) Overlay: copy every new item over the current install. The DB and
        #    other preserved files are never overwritten.
        print(f"\n  {w('[ Applying update ]')}\n")
        for item in snapshot.iterdir():
            if item.name in PRESERVE:
                continue
            target = ROOT / item.name
            try:
                if target.exists():
                    _rm_path(target)
                if item.is_dir():
                    shutil.copytree(item, target)
                else:
                    shutil.copy2(item, target)
            except Exception as e:
                print(f"  {r('[ERR]')}  Failed writing {item.name}: {e}")
                print(f"  {y('[!]')}  Update aborted mid-apply — re-run {c('run.py --update')} to retry.")
                return
        print(f"  {g('[OK]')}  New code written")

        # 3) Prune: remove old top-level files that no longer exist upstream,
        #    but never touch the DB, .git, or .venv.
        removed = 0
        for item in ROOT.iterdir():
            if _skip_top_level(item.name):
                continue
            if item.name not in new_names:
                try:
                    _rm_path(item)
                    removed += 1
                except Exception as e:
                    print(f"  {y('[!]')}  Could not remove stale {item.name}: {e}")
        if removed:
            print(f"  {g('[OK]')}  Removed {removed} stale item(s)")

        # 4) Clear caches so the new code isn't shadowed by old bytecode.
        for cache in ROOT.rglob("__pycache__"):
            shutil.rmtree(cache, ignore_errors=True)
        print(f"  {g('[OK]')}  Cleared bytecode caches")

    finally:
        shutil.rmtree(staging, ignore_errors=True)

    print(f"\n  {MG}{B}Agent2 updated successfully.{R}")
    print(f"  {dim('Finalizing setup with the new version ...')}\n")
    time.sleep(1)

    # 5) Hand off to the freshly-downloaded launcher so it configures the venv
    #    and installs ITS dependencies (the in-memory functions are now stale).
    env = os.environ.copy()
    env["AGENT2_UPDATED"] = "1"          # marker for the new launcher, if it cares
    try:
        subprocess.run([sys.executable, str(ROOT / "run.py")], env=env)
    except KeyboardInterrupt:
        pass
    sys.exit(0)


def bootstrap_if_needed():
    """First-run bootstrap: if only run.py is present (no app code), download the
    full project into this same directory before setup continues."""
    if APP_CLI.exists() or APP_WEB.exists() or (ROOT / "agent2").is_dir():
        return  # already a full install — nothing to do

    banner()
    print(f"  {w('[ First-Run Bootstrap ]')}")
    print(f"  {dim('Only run.py detected — fetching the full Agent2 project.')}")
    print(f"  {dim('Source:')} {c(REPO_URL)}")
    print(f"  {dim('Target:')} {dim(str(ROOT))}\n")

    import tempfile
    staging = Path(tempfile.mkdtemp(prefix="agent2_boot_", dir=str(ROOT.parent)))
    snapshot = staging / "snapshot"
    try:
        ok, err = fetch_snapshot(snapshot)
        if not ok:
            print(f"  {r('[ERR]')}  Could not download the project: {dim(err[:300])}")
            print(f"         {dim('Check your connection and retry:')} {c('python run.py')}")
            sys.exit(1)

        clone_git = snapshot / ".git"
        if clone_git.exists():
            _rm_path(clone_git)

        print(f"\n  {w('[ Installing project ]')}\n")
        for item in snapshot.iterdir():
            # Never clobber a pre-existing run.py (the one we're running from) or
            # any preserved data file.
            if item.name == "run.py" or item.name in PRESERVE:
                continue
            target = ROOT / item.name
            try:
                if target.exists():
                    _rm_path(target)
                if item.is_dir():
                    shutil.copytree(item, target)
                else:
                    shutil.copy2(item, target)
            except Exception as e:
                print(f"  {r('[ERR]')}  Failed installing {item.name}: {e}")
                sys.exit(1)
        print(f"  {g('[OK]')}  Project files installed")
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    print(f"  {g('[OK]')}  Bootstrap complete — continuing setup ...\n")
    time.sleep(1)

# ── Step 5: Optional security tools ───────────────────────────────────────────
def check_tools():
    """Report which optional pentest tools are on PATH.

    Collapsed to two lines: these are genuinely optional and printing a hint for
    every missing one buried the rest of setup in yellow.
    """
    section("Optional Security Tools")
    tool_hints = {
        "nmap":    ("nmap",                 "brew install nmap",        "sudo apt install nmap"),
        "nikto":   ("nikto",               "brew install nikto",       "sudo apt install nikto"),
        "gobuster":("gobuster",            "brew install gobuster",    "sudo apt install gobuster"),
        "sqlmap":  ("sqlmap",              "pip install sqlmap",       "pip install sqlmap"),
        "hydra":   ("hydra",               "brew install hydra",       "sudo apt install hydra"),
    }
    found   = [t for t in tool_hints if shutil.which(t)]
    missing = [t for t in tool_hints if t not in found]

    if found:
        item("ok", f"{len(found)} available", ", ".join(found))
    if missing:
        item("info", f"{len(missing)} not installed", ", ".join(missing))
        hint_os = 0 if IS_WIN else (1 if IS_MAC else 2)
        note(f"e.g.  {tool_hints[missing[0]][hint_os]}")
    endsection()

# ── Step 5b: Global Command ───────────────────────────────────────────────────
def _global_bin_dir() -> Path:
    """A stable, per-user directory to hold the `agent2` launcher on any OS.

    ONE location on every platform: `~/.local/bin`. Windows used to get
    `~/.agent2/bin` instead, which meant the wrapper lived somewhere no other
    tool ever adds to PATH and somewhere a user looking for it would not think
    to check. `~/.local/bin` is already the convention pip, pipx and the Windows
    Store Python all use for per-user scripts. The old directory stays in
    `_candidate_bin_dirs()` so a wrapper left there by a previous install is
    still found and refreshed rather than being left to shadow this one.
    """
    return Path.home() / ".local" / "bin"


def _stable_python() -> str:
    """A Python interpreter that does NOT live inside this project's .venv.

    Used by the Docker wrapper, which must keep working even after --reset /
    --uninstall wipes .venv. Falls back to the current interpreter only if no
    system python is discoverable."""
    venv_key = str(VENV).rstrip("\\/").lower()
    # Prefer the interpreter running setup, if it isn't the venv one.
    if venv_key not in sys.executable.lower():
        return sys.executable
    # Otherwise probe common launchers on PATH.
    for name in (["py", "python", "python3"] if IS_WIN else ["python3", "python"]):
        found = shutil.which(name)
        if found and venv_key not in found.lower():
            return found
    return sys.executable  # last resort


def _dir_on_path(d: Path) -> bool:
    parts = os.environ.get("PATH", "").split(os.pathsep)
    dl = str(d).rstrip("\\/").lower()
    return any(p.rstrip("\\/").lower() == dl for p in parts if p)


def _add_to_path_windows(d: Path) -> str:
    """Append *d* to the persistent per-user PATH (via PowerShell). Returns
    'added' | 'exists' | 'failed'."""
    ds = str(d)
    ps = (
        "$ErrorActionPreference='Stop';"
        "$d=[Environment]::ExpandEnvironmentVariables($env:A2DIR);"
        "$p=[Environment]::GetEnvironmentVariable('PATH','User');"
        "if(-not $p){$p=''};"
        "$parts=$p.Split(';');"
        "if($parts -notcontains $d){"
        "  $new= if($p){$p.TrimEnd(';')+';'+$d}else{$d};"
        "  [Environment]::SetEnvironmentVariable('PATH',$new,'User');"
        "  'added' } else { 'exists' }"
    )
    try:
        env = os.environ.copy()
        env["A2DIR"] = ds
        res = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            capture_output=True, text=True, env=env, timeout=20)
        out = (res.stdout or "").strip().lower()
        return out if out in ("added", "exists") else "failed"
    except Exception:
        return "failed"


def _add_to_path_unix(d: Path) -> str:
    """Ensure *d* is exported on PATH from the user's shell rc files."""
    line = f'\n# Added by Agent2\nexport PATH="{d}:$PATH"\n'
    marker = "# Added by Agent2"
    touched = False
    for rc in (".bashrc", ".zshrc", ".profile"):
        p = Path.home() / rc
        try:
            existing = p.read_text(encoding="utf-8") if p.exists() else ""
            if marker in existing or str(d) in existing:
                continue
            with open(p, "a", encoding="utf-8") as f:
                f.write(line)
            touched = True
        except Exception:
            pass
    return "added" if touched else "exists"


def _wrapper_names() -> list[str]:
    """Filenames the agent2 launcher may use on this platform."""
    return ["agent2.bat", "agent2.cmd"] if IS_WIN else ["agent2"]


def _candidate_bin_dirs() -> list:
    """Every dir an agent2 wrapper might live in: the primary, both legacy
    locations, and every directory currently on PATH. De-duplicated, order-stable.
    A stale wrapper in ANY of these can shadow the real one, so we rewrite them all."""
    dirs = [
        _global_bin_dir(),                  # ~/.local/bin on every OS
        Path.home() / ".agent2" / "bin",    # legacy (Windows installs before the move)
    ]
    for p in os.environ.get("PATH", "").split(os.pathsep):
        p = p.strip().strip('"')
        if p:
            dirs.append(Path(p))
    seen, out = set(), []
    for d in dirs:
        try:
            key = str(d).rstrip("\\/").lower()
        except Exception:
            continue
        if key and key not in seen:
            seen.add(key)
            out.append(d)
    return out


def _refresh_stale_wrappers(content: str, primary: Path) -> int:
    """Overwrite every EXISTING agent2 wrapper found outside *primary* so an old
    copy (e.g. one left in ~/.local/bin by a previous install, pointing at a moved
    or deleted run.py) can never shadow the freshly-installed command. Returns the
    count refreshed."""
    fixed = 0
    primary_key = str(primary).rstrip("\\/").lower()
    for d in _candidate_bin_dirs():
        if str(d).rstrip("\\/").lower() == primary_key:
            continue
        for name in _wrapper_names():
            wrapper = d / name
            try:
                if not wrapper.exists():
                    continue
                # Compare newline-insensitively so a wrapper we already wrote isn't
                # flagged stale just because of CRLF/LF translation.
                cur = wrapper.read_text(encoding="utf-8", errors="replace")
                if cur.replace("\r", "") == content.replace("\r", ""):
                    continue  # already correct
                wrapper.write_bytes(content.encode("utf-8"))  # exact bytes, no NL translation
                if not IS_WIN:
                    try: wrapper.chmod(0o755)
                    except Exception: pass
                print(f"  {g('[OK]')}  Refreshed stale launcher: {dim(str(wrapper))}")
                fixed += 1
            except Exception:
                pass
    return fixed


def install_global_command(docker: bool = False):
    print(f"\n  {w('[ Global Command ]')}\n")
    bin_dir = _global_bin_dir()

    try:
        bin_dir.mkdir(parents=True, exist_ok=True)
        # In Docker mode the wrapper drives agent2_docker.py; Docker owns the
        # runtime, so the wrapper must NOT depend on .venv (a Docker user may
        # never create one, and --reset/--uninstall would delete it and break
        # the command). We therefore pick a venv-independent interpreter. In
        # native mode we prefer the venv python so the command survives system
        # python changes. A bare `agent2` starts/opens the container (docker) or
        # opens the CLI (native); extra args pass straight through.
        target = (ROOT / "agent2_docker.py") if docker else (ROOT / "run.py")
        if docker:
            py = _stable_python()
        else:
            py = str(VENV_PY) if VENV_PY.exists() else sys.executable
        # Uniform behavior: a bare `agent2` behaves EXACTLY like a bare
        # `python run.py` (native → the web/CLI mode menu) or a bare
        # `agent2_docker.py` (docker → start + open the Web UI). No forced flag.
        default_args = ""

        if IS_WIN:
            cmd_path = bin_dir / "agent2.bat"
            content = (
                "@echo off\r\n"
                f'if "%~1"=="" (\r\n'
                f'    "{py}" "{target}"{default_args}\r\n'
                ") else (\r\n"
                f'    "{py}" "{target}" %*\r\n'
                ")\r\n"
            )
            cmd_path.write_bytes(content.encode("utf-8"))  # exact bytes, no NL translation
        else:
            cmd_path = bin_dir / "agent2"
            content = (
                "#!/usr/bin/env bash\n"
                "if [ $# -eq 0 ]; then\n"
                f'    exec "{py}" "{target}"{default_args}\n'
                "else\n"
                f'    exec "{py}" "{target}" "$@"\n'
                "fi\n"
            )
            cmd_path.write_bytes(content.encode("utf-8"))
            try:
                cmd_path.chmod(0o755)
            except Exception:
                pass

        print(f"  {g('[OK]')}  Command installed: {c('agent2')} {dim('(' + str(cmd_path) + ')')}")

        # Self-heal: rewrite any OTHER agent2 wrapper already on PATH (e.g. a stale
        # one in ~/.local/bin from an older install) so it can't shadow this one
        # by pointing at an old or deleted run.py.
        _refresh_stale_wrappers(content, primary=bin_dir)

        # Make sure the directory is actually on PATH so `agent2` works anywhere.
        if _dir_on_path(bin_dir):
            print(f"  {g('[OK]')}  {dim(str(bin_dir) + ' already on PATH')}")
        else:
            state = _add_to_path_windows(bin_dir) if IS_WIN else _add_to_path_unix(bin_dir)
            if state == "added":
                print(f"  {g('[OK]')}  Added to PATH: {dim(str(bin_dir))}")
                if IS_WIN:
                    print(f"         {dim('Open a NEW terminal, then just type:')} {c('agent2')}")
                else:
                    print(f"         {dim('Run:')} {c('source ~/.bashrc')}  {dim('(or open a new terminal), then type:')} {c('agent2')}")
            elif state == "exists":
                print(f"  {g('[OK]')}  {dim('PATH already configured — open a new terminal, then type:')} {c('agent2')}")
            else:
                print(f"  {y('[!]')}  Couldn't auto-update PATH. Add this dir manually:")
                print(f"         {dim(str(bin_dir))}")
    except Exception as e:
        print(f"  {y('[!]')}  Failed to install global command: {e}")


def docker_ok() -> bool:
    """True if the docker CLI exists AND the daemon is reachable."""
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info"],
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode == 0
    except Exception:
        return False


def _docker_installer_cmd(target_os: str):
    """Return the package-manager command to install Docker for *target_os*
    ('Windows' | 'Darwin' | 'Linux'), or None if no known manager is present."""
    if target_os == "Windows":
        if shutil.which("winget"):
            return ["winget", "install", "--id", "Docker.DockerDesktop", "-e",
                    "--source", "winget", "--accept-source-agreements",
                    "--accept-package-agreements"]
        if shutil.which("choco"):
            return ["choco", "install", "docker-desktop", "-y"]
        return None
    if target_os == "Darwin":
        if shutil.which("brew"):
            return ["brew", "install", "--cask", "docker"]
        return None
    # Linux — prefer distro package managers.
    if shutil.which("apt-get"):
        return ["sudo", "apt-get", "install", "-y", "docker.io"]
    if shutil.which("dnf"):
        return ["sudo", "dnf", "install", "-y", "docker"]
    if shutil.which("yum"):
        return ["sudo", "yum", "install", "-y", "docker"]
    if shutil.which("pacman"):
        return ["sudo", "pacman", "-S", "--noconfirm", "docker"]
    return None


def _docker_manual_hint(target_os: str) -> str:
    if target_os == "Windows":
        return "https://www.docker.com/products/docker-desktop/  (needs WSL2)"
    if target_os == "Darwin":
        return "https://www.docker.com/products/docker-desktop/  (or: brew install --cask docker)"
    return "https://docs.docker.com/engine/install/  (then: sudo systemctl enable --now docker)"


def ensure_docker(target_os: str) -> bool:
    """Make sure Docker is installed. Auto-installs via the platform package
    manager when missing (mirrors ensure_git). Returns True if the docker CLI is
    available afterward (daemon may still need a manual first-start on Win/Mac)."""
    print(f"\n  {w('[ Docker ]')}\n")
    if shutil.which("docker"):
        exe = shutil.which("docker")
        if docker_ok():
            print(f"  {g('[OK]')}  Docker installed and running  {dim(exe)}")
        else:
            print(f"  {g('[OK]')}  Docker installed  {dim(exe)}")
            print(f"  {y('[!]')}  {dim('Daemon not running yet — the agent2 command will start it.')}")
        return True

    print(f"  {y('[!]')}  Docker not found — attempting automatic install ...")
    installer = _docker_installer_cmd(target_os)
    if not installer:
        print(f"  {r('[ERR]')}  No supported package manager found to install Docker.")
        print(f"         {dim('Install Docker manually:')} {c(_docker_manual_hint(target_os))}")
        print(f"         {dim('then re-run:')} {c('python run.py --docker')}")
        return False

    res = spin_run("Installing Docker", installer)
    if res.returncode != 0:
        print(f"  {y('[!]')}  Docker install failed: {dim((res.stderr or res.stdout or '')[:300])}")
        print(f"         {dim('Install Docker manually:')} {c(_docker_manual_hint(target_os))}")
        return False

    if shutil.which("docker"):
        print(f"  {g('[OK]')}  Docker installed  {dim(shutil.which('docker'))}")
        print(f"         {dim('You may need to start Docker Desktop / the docker service once.')}")
        return True

    # Installed, but not yet visible on PATH in this session (very common on
    # Windows Docker Desktop — needs a new shell / a reboot for WSL2).
    print(f"  {g('[OK]')}  Docker installed — {dim('not visible in this session yet.')}")
    print(f"         {dim('Open a NEW terminal (or reboot), then run:')} {c('python run.py --docker')}")
    return False


def ask_timed(prompt: str, timeout: float = 10.0) -> "str | None":
    """Read one line with a timeout, WITHOUT ever orphaning a reader on stdin.

    Returns the stripped line, or None if the timeout expired / there is no
    console to read from.

    Why not a daemon thread around input(): a join() timeout does not cancel
    the thread — it stays parked inside input(), still holding its claim on the
    console. We then launch the CLI as a child sharing that same stdin, and the
    two fight over every keystroke; prompt_toolkit paints its UI and then never
    sees a key, so the CLI looks frozen. Whatever happens in here, stdin must be
    free by the time we return.

    The return annotation is quoted on purpose: run.py must import on the SYSTEM
    python (3.9+, per check_python) before any venv exists, and a bare
    `str | None` is evaluated at def time — TypeError before 3.10.
    """
    # No console to read (piped, redirected, service, CI) — take the default
    # immediately rather than blocking a launch that nobody can answer.
    try:
        if sys.stdin is None or not sys.stdin.isatty():
            return None
    except Exception:
        return None

    sys.stdout.write(prompt)
    sys.stdout.flush()

    if IS_WIN:
        import msvcrt
        buf = ""
        deadline = time.monotonic() + timeout
        while True:
            if msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch in ("\r", "\n"):
                    print()
                    return buf.strip()
                if ch == "\x03":                     # Ctrl-C
                    raise KeyboardInterrupt
                if ch == "\x08":                     # Backspace
                    if buf:
                        buf = buf[:-1]
                        sys.stdout.write("\b \b"); sys.stdout.flush()
                    continue
                if ch in ("\x00", "\xe0"):           # arrow/function key: eat scan code
                    msvcrt.getwch()
                    continue
                buf += ch
                sys.stdout.write(ch); sys.stdout.flush()
                # They're mid-answer — don't yank the prompt out from under them.
                deadline = time.monotonic() + timeout
                continue
            if time.monotonic() >= deadline:
                print()
                return None
            time.sleep(0.03)

    import select
    try:
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
    except Exception:
        # select() unusable on this stdin — block once on the MAIN thread. Slow
        # is fine; an abandoned background reader is not.
        return (sys.stdin.readline() or "").strip() or None
    if not ready:
        print()
        return None
    line = sys.stdin.readline()
    if not line:                                     # EOF
        return None
    return line.strip()


def drain_stdin() -> None:
    """Discard anything typed after we stopped listening, so a late keystroke
    doesn't get injected into the CLI's prompt as if the user had typed it."""
    try:
        if sys.stdin is None or not sys.stdin.isatty():
            return
        if IS_WIN:
            import msvcrt
            while msvcrt.kbhit():
                msvcrt.getwch()
        else:
            import termios
            termios.tcflush(sys.stdin, termios.TCIFLUSH)
    except Exception:
        pass


def _read_key(timeout: float):
    """One keypress from a real console. Returns 'up'|'down'|'enter'|a character,
    or None on timeout. Never leaves a reader parked on stdin (see ask_timed)."""
    if IS_WIN:
        import msvcrt
        deadline = time.monotonic() + timeout
        while True:
            if msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch in ("\x00", "\xe0"):            # extended: arrows etc.
                    code = msvcrt.getwch()
                    return {"H": "up", "P": "down"}.get(code, "other")
                if ch in ("\r", "\n"):
                    return "enter"
                if ch == "\x03":
                    raise KeyboardInterrupt
                return ch
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.02)

    import select, termios, tty
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        if not ready:
            return None
        ch = sys.stdin.read(1)
        if ch == "\x1b":                              # CSI escape sequence
            ready, _, _ = select.select([sys.stdin], [], [], 0.05)
            if not ready:
                return "esc"
            if sys.stdin.read(1) != "[":
                return "other"
            return {"A": "up", "B": "down"}.get(sys.stdin.read(1), "other")
        if ch in ("\r", "\n"):
            return "enter"
        if ch == "\x03":
            raise KeyboardInterrupt
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def select_menu(title, options, timeout=10.0, default=0):
    """Arrow-key selector. options = [(label, detail, value), ...].

    Up/Down (or k/j) move, Enter confirms, 1-9 jump straight to an entry. Returns
    the chosen value, or the default's value on timeout. Falls back to the typed
    numeric prompt when there's no console to drive (piped stdin, CI, redirect),
    so scripted launches keep working exactly as before.
    """
    no_tty = False
    try:
        no_tty = sys.stdin is None or not sys.stdin.isatty()
    except Exception:
        no_tty = True

    if no_tty:
        return options[default][2]

    idx = default
    lines = len(options) + 1        # options + the hint row

    def draw(first=False):
        if not first:
            # Jump back to the top of the block we drew last time and repaint.
            sys.stdout.write(f"\033[{lines}A")
        for i, (label, detail, _) in enumerate(options):
            if i == idx:
                row = f"  {CY}│{R}  {CY}❯{R}  {WH}{B}{label}{R}"
            else:
                row = f"  {CY}│{R}     {label}"
            if detail:
                row += f"  {dim(detail)}"
            sys.stdout.write("\033[2K" + row + "\n")
        sys.stdout.write("\033[2K" + f"  {CY}│{R}     "
                         f"{dim('↑↓ move  ·  Enter select  ·  1-9 jump')}"
                         f"  {dim('(' + options[default][0] + f' in {int(timeout)}s)')}\n")
        sys.stdout.flush()

    section(title)
    draw(first=True)

    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            endsection()
            return None                                # caller reports timeout
        key = _read_key(min(remaining, 0.5))
        if key is None:
            continue
        if key in ("up", "k"):
            idx = (idx - 1) % len(options)
        elif key in ("down", "j"):
            idx = (idx + 1) % len(options)
        elif key == "enter":
            endsection()
            return options[idx][2]
        elif key.isdigit() and key != "0" and int(key) <= len(options):
            # Jump + confirm in one keystroke. Repaint first so the highlight
            # lands on what was actually chosen before the block is closed.
            idx = int(key) - 1
            draw()
            endsection()
            return options[idx][2]
        elif key in ("esc", "q"):
            endsection()
            raise KeyboardInterrupt
        else:
            continue
        # Any movement means they're engaged — don't time out mid-decision.
        deadline = time.monotonic() + timeout
        draw()


def choose_target_os() -> str:
    """Ask which OS the Docker runtime targets, defaulting to the detected one.
    Returns 'Windows' | 'Darwin' | 'Linux'. The resulting `agent2` command and
    its behavior are identical on every platform — only the installer path and
    PATH wiring differ."""
    options = [("Windows", "Windows"), ("macOS", "Darwin"), ("Linux", "Linux")]
    detected = OS_NAME if OS_NAME in ("Windows", "Darwin", "Linux") else "Linux"
    default_idx = next(i for i, (_, v) in enumerate(options) if v == detected)

    try:
        picked = select_menu("Target OS", [
            (label, c("detected") if val == detected else "", val)
            for label, val in options
        ], default=default_idx)
    except KeyboardInterrupt:
        picked = None
    if picked is None:
        picked = detected
    print(f"  {g('[OK]')}  Target OS: {c(dict((v, l) for l, v in options)[picked])}")
    return picked


def docker_setup():
    """Wire up the `agent2` command as a Docker driver, then start the container.

    Skips the native venv/deps/keys setup entirely — Docker owns the runtime.
    Steps: pick target OS → ensure Docker (auto-install if missing) → install the
    global `agent2` command → first build/start. Afterwards `agent2` behaves the
    same on every platform.
    """
    banner()
    print(f"\n  {w('[ Docker Setup ]')}\n")

    compose = ROOT / "docker-compose.yml"
    driver  = ROOT / "agent2_docker.py"
    if not compose.exists() or not driver.exists():
        print(r("  [ERR]  Missing docker-compose.yml or agent2_docker.py at the project root."))
        print(f"         {dim('Run')} {c('python run.py --update')} {dim('to fetch the latest files.')}")
        sys.exit(1)

    target_os = choose_target_os()

    # Ensure Docker is present (auto-install via package manager if missing).
    if not ensure_docker(target_os):
        # Docker couldn't be made available in THIS session. Still install the
        # command so it's ready once Docker is up / a new terminal is opened.
        install_global_command(docker=True)
        print(f"\n  {y('[!]')}  Docker isn't ready yet, so I didn't start a container.")
        print(f"         {dim('Once Docker is running, just type')} {c('agent2')}{dim('.')}")
        return

    # Install the global `agent2` command pointing at the Docker driver.
    install_global_command(docker=True)

    # Kick off the first build/start now so `agent2` is instantly usable.
    print(f"\n  {w('[ First Start ]')}  {dim('(building the image — first run only)')}\n")
    try:
        subprocess.run([_stable_python(), str(driver), "up"])
    except KeyboardInterrupt:
        print(f"\n  {dim('Stopped.')}")

    print(f"\n  {g('══════════════════════════════════════════════')}")
    print(f"  {g('>>>')}  Done. From now on, just type {c('agent2')} in any terminal.")
    print(f"  {dim('       agent2        interactive CLI session (default)')}")
    print(f"  {dim('       agent2 web    start + open the Web UI')}")
    print(f"  {dim('       agent2 dual   Web UI in background + CLI here')}")
    print(f"  {dim('       agent2 stop   stop the container')}")
    print(f"  {dim('       agent2 help   all commands')}")
    if IS_WIN:
        print(f"  {y('       (open a NEW terminal so PATH refreshes)')}")
    else:
        print(f"  {y('       (open a new terminal or run: source ~/.bashrc)')}")
    print(f"  {g('══════════════════════════════════════════════')}\n")


# ── Step 6: Launch ─────────────────────────────────────────────────────────────
def launch_web():
    print(f"\n  {c('»')}  {w('Agent2 Web UI')}  {dim('starting …')}")
    try:
        subprocess.run([str(VENV_PY), str(APP_WEB)])
    except KeyboardInterrupt:
        print(f"\n  {dim('Stopped.')}")

def launch_cli():
    if not APP_CLI.exists():
        print(r(f"\n  [ERR]  agent2cli.py not found at {APP_CLI}"))
        print(f"  {dim('  Place agent2cli.py in the same folder as run.py.')}")
        sys.exit(1)
    print(f"\n  {c('»')}  {w('Agent2 CLI')}  {dim('starting …')}")
    try:
        subprocess.run([str(VENV_PY), str(APP_CLI)])
    except KeyboardInterrupt:
        print(f"\n  {dim('Stopped.')}")

def launch_dual():
    """Dual mode — Web UI in a background thread, CLI in the foreground."""
    if not APP_DUAL.exists():
        print(r(f"\n  [ERR]  agent2dual.py not found at {APP_DUAL}"))
        print(f"  {dim('  Run')} {c('python run.py --update')} {dim('to fetch it.')}")
        sys.exit(1)
    # agent2dual.py prints its own dual-mode summary (URL, port, log location)
    # once the web half has actually bound — duplicating it here would be wrong
    # as often as right, since the port can shift when 1311 is busy.
    print(f"\n  {c('»')}  {w('Agent2 dual mode')}  {dim('starting …')}")
    try:
        subprocess.run([str(VENV_PY), str(APP_DUAL)])
    except KeyboardInterrupt:
        print(f"\n  {dim('Stopped.')}")

# ── Main ───────────────────────────────────────────────────────────────────────
def show_help():
    banner()
    print(f"  {w('Usage:')}  python run.py [options]\n")
    print(f"  {c('--cli')}       Setup + Start CLI Agent {c('(default)')}")
    print(f"  {c('--web')}       Setup + Start Web UI")
    print(f"  {c('--dual')}      Setup + Start DUAL — Web UI in the background,")
    print(f"               CLI in this terminal (shared agent2.db)")
    print(f"  {c('--docker')}    Switch to the Docker runtime: pick OS, auto-install")
    print(f"               Docker if missing, install the global {c('agent2')} command")
    print(f"  {c('--addapi')}    Add a new Gemini API key")
    print(f"  {c('--update, -up')} Update to the latest code (keeps your agent2.db)")
    print(f"  {c('--reset')}     Wipe .venv and reinstall packages")
    print(f"  {c('--uninstall')} Full cleanup (deletes DB, .env, and .venv)")
    print(f"  {c('--debug')}     Show a full traceback if setup errors out")
    print(f"  {c('--help, -h')}  Show this help menu\n")
    print(f"  {dim('Native run.py NEVER uses Docker — only --docker does.')}\n")
    sys.exit(0)

def main():
    args = sys.argv[1:]
    
    # ── Command Handlers ─────────────────────────────────────────────────────
    if "--help" in args or "-h" in args:
        show_help()

    if "--uninstall" in args:
        uninstall()

    # Self-update: -up / -update / --up / --update
    if any(a in ("-up", "-update", "--up", "--update") for a in args):
        self_update()

    # First-run bootstrap: if only run.py exists, fetch the full project first.
    bootstrap_if_needed()

    # ── Docker setup: install the `agent2` command as a Docker driver ──────────
    if "--docker" in args:
        docker_setup()
        return

    do_reset  = "--reset"  in args
    do_addapi = "--addapi" in args
    do_cli    = "--cli"    in args
    do_web    = "--web"    in args
    # --both is kept as a silent alias so old scripts, docs and shell history
    # keep working after the rename to --dual.
    do_dual   = "--dual"   in args or "--both" in args

    # A "flagged" launch (--cli / --web / --dual) is a request to START, not to
    # re-run setup. Those take the fast path: one batched import probe instead of
    # per-package verification, no key listing, no tool scan, no global-command
    # rewrite. A bare `python run.py` (or `agent2`) is the setup entry point and
    # still does the full, chatty check.
    flagged = do_cli or do_web or do_dual

    mode = "all"
    if do_cli:
        mode = "cli"
    elif do_web:
        mode = "web"
    # do_dual / bare stay "all" — dual mode needs web AND CLI dependencies.

    # ── Setup Sequence ───────────────────────────────────────────────────────
    # A flagged launch goes straight to the app, which prints its own banner —
    # showing run.py's ASCII art first would just push it off-screen.
    if not flagged:
        banner()

    if do_addapi:
        check_python()
        ensure_venv(do_reset)
        install_deps(mode)
        ensure_keys(force_add=True)
        return

    if flagged and not do_reset and VENV.exists() and deps_present(mode):
        # ── Fast path ────────────────────────────────────────────────────────
        # Everything this mode needs is already importable, so start immediately:
        # no per-package verification, no key listing, no tool scan, no global
        # command rewrite. Keys are prompted for ONLY if there are none at all.
        #
        # deps_present() only covers REQUIRED imports, so an optional package
        # (rich, prompt_toolkit, a fileintel wheel) that failed to install once
        # would stay missing forever and silently degrade the UI. Heal it here
        # instead of making the user notice and ask.
        heal_optional_deps(mode)
        if not has_keys():
            ensure_keys()
    else:
        # ── Full setup ───────────────────────────────────────────────────────
        # A bare `agent2`, an explicit --reset, or a flagged launch whose probe
        # failed ("if error rise" — missing venv or missing packages, e.g. the
        # very first run via install.py, which hands off with --cli).
        if flagged:
            banner()
        check_python()
        ensure_venv(do_reset)
        install_deps(mode)
        ensure_keys()
        check_tools()
        install_global_command()

    # ── Mode Selection ───────────────────────────────────────────────────────
    if do_dual:
        launch_dual()
    elif do_cli:
        launch_cli()
    elif do_web:
        launch_web()
    else:
        # Normal mode: arrow-key selection with a 10s timeout.
        # CLI is the default — it's the fastest path to a working session and
        # needs no browser.
        try:
            choice = select_menu("Start", [
                ("CLI",   "this terminal",                  "cli"),
                ("Web UI", "browser",                       "web"),
                ("Dual",  "web in background + CLI here",   "dual"),
                ("Exit",  "",                               "exit"),
            ])
        except KeyboardInterrupt:
            print(f"\n  {dim('Goodbye.')}")
            sys.exit(0)
        # The launchers below hand this terminal to a child process, so stdin
        # must be fully ours to give away — see ask_timed().
        drain_stdin()

        if choice is None:
            print(f"  {y('[timeout]')}  No response. Launching {g('CLI')}...")
            time.sleep(0.6)
            launch_cli()
        elif choice == "cli":
            launch_cli()
        elif choice == "web":
            launch_web()
        elif choice == "dual":
            launch_dual()
        elif choice == "exit":
            print(f"\n  {dim('Goodbye.')}")
            sys.exit(0)

if __name__ == "__main__":
    # ── Failsafe: nothing below main() should ever crash with a raw traceback.
    # SystemExit passes through (it carries our intended exit code); Ctrl+C exits
    # cleanly; any other unexpected error is reported in a friendly, actionable
    # way instead of a stack dump.
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n  {dim('Stopped by user (Ctrl+C).')}")
        sys.exit(130)
    except SystemExit:
        raise
    except Exception as e:
        print(f"\n  {r('[ERR]')}  Something went wrong: {dim(str(e))}")
        print(f"         {dim('This is unexpected. Try:')} {c('python run.py --update')}  "
              f"{dim('or re-run with a clean env:')} {c('python run.py --reset')}")
        # Only show the full traceback when explicitly asked (debugging aid).
        if os.environ.get("AGENT2_DEBUG") or "--debug" in sys.argv:
            import traceback
            print(dim("\n--- traceback (AGENT2_DEBUG) ---"))
            traceback.print_exc()
        sys.exit(1)
