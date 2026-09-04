#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
Agent2 — one-line network installer
───────────────────────────────────
Designed to be run straight from a pipe:

    curl -fsSL https://raw.githubusercontent.com/aaravshah1311/Agent-2/main/install.py | python3 -

    # Windows PowerShell
    irm https://raw.githubusercontent.com/aaravshah1311/Agent-2/main/install.py | python -

What it does (stdlib only — nothing to pre-install):
  1. Verifies Python 3.9+, and reports whether venv/pip are usable.
  2. Locates git — optional. Without it, downloads a ZIP snapshot instead.
  3. Fetches the repo into ./Agent-2  (override with AGENT2_DIR).
  4. Hands off to run.py, which builds the .venv, installs deps, and starts Agent2.

IMPORTANT — why this file exists separately from run.py:
  When a script is delivered over a pipe, Python reads it from STDIN. That means
  `__file__` is unreliable and, crucially, STDIN is *consumed by the script*, so
  any interactive input() the launcher does will hit EOF. This installer therefore
  avoids `__file__` entirely and never prompts — it only fetches + delegates.
  run.py then prompts for the Gemini key on a real console (it opens Google AI
  Studio for you); you can also add one later with `agent2 --addapi`.

Environment overrides:
  AGENT2_DIR    target directory                (default: ./Agent-2)
  AGENT2_MODE   launch mode after install       cli | web | dual | none  (default: cli)
  AGENT2_REPO   git URL to clone                (default: official repo)
"""

import os
import sys
import shutil
import subprocess
from pathlib import Path

REPO_URL = os.environ.get(
    "AGENT2_REPO", "https://github.com/aaravshah1311/Agent-2"
)
TARGET = Path(os.environ.get("AGENT2_DIR", "Agent-2")).expanduser().resolve()
MODE = os.environ.get("AGENT2_MODE", "cli").strip().lower()

IS_WIN = os.name == "nt"

# ── Console setup ───────────────────────────────────────────────────────────────
# When delivered over a pipe on Windows, stdout defaults to a legacy code page
# (cp1252) that cannot encode arrows/box characters and would crash mid-print.
# Force UTF-8 and enable ANSI colours where possible; degrade gracefully.
if IS_WIN:
    os.system("")  # enable ANSI on modern Windows terminals
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ── Colours (best-effort; degrade to plain text where ANSI is unsupported) ──────
R = "\033[0m"; B = "\033[1m"; D = "\033[2m"
GR = "\033[1;32m"; CY = "\033[1;36m"; YL = "\033[1;33m"; RD = "\033[1;31m"; MG = "\033[1;35m"


# ── Layout helpers ──────────────────────────────────────────────────────────────
# Same visual grammar as run.py (section / item / note) so the handoff between the
# two scripts reads as one continuous install rather than two different tools.
UI_W = 54


def section(title):
    print(f"\n  {CY}┌─ {B}{title}{R} {CY}{'─' * max(0, UI_W - len(title) - 4)}{R}")


def item(state, text, detail=""):
    sym = {"ok": f"{GR}✓{R}", "warn": f"{YL}!{R}", "err": f"{RD}✗{R}",
           "info": f"{CY}·{R}", "run": f"{CY}»{R}"}.get(state, f"{CY}·{R}")
    line = f"  {CY}│{R}  {sym}  {text}"
    if detail:
        line += f"  {D}{detail}{R}"
    print(line)


def note(text):
    print(f"  {CY}│{R}     {D}{text}{R}")


def endsection():
    print(f"  {CY}└{'─' * UI_W}{R}")


# ── Progress feedback ───────────────────────────────────────────────────────────
# A pipe install shows nothing for the whole clone, which reads as a hang on a
# slow connection. These give the long steps a heartbeat, matching run.py's
# spinner grammar so the handoff looks like one continuous install.
SPIN = ["-", "\\", "|", "/"] if IS_WIN else ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

try:
    IS_TTY = sys.stdout.isatty()
except Exception:
    IS_TTY = False


def spin_run(label, cmd, **kw):
    """Run *cmd* under a spinner, collapsing to one ✓/✗ row when it finishes.

    Animates only on a real terminal: '\\r' does not erase when stdout is a file
    or a pipe, so animating into a log would write hundreds of junk lines.
    """
    import threading
    import itertools
    import time

    box, done = {}, threading.Event()

    def work():
        try:
            box["res"] = subprocess.run(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", **kw)
        except Exception as e:                       # git vanished mid-run, etc.
            box["res"] = subprocess.CompletedProcess(cmd, 1, str(e))
        finally:
            done.set()

    t = threading.Thread(target=work, daemon=True)
    t.start()
    pad = " " * 18
    if IS_TTY:
        for f in itertools.cycle(SPIN):
            if done.is_set():
                break
            print(f"\r  {CY}│{R}  {CY}{f}{R}  {label} …{pad}", end="", flush=True)
            time.sleep(0.08)
    else:
        item("run", f"{label} …")
    t.join()
    res = box["res"]
    sym = f"{GR}✓{R}" if res.returncode == 0 else f"{RD}✗{R}"
    lead = "\r" if IS_TTY else ""
    print(f"{lead}  {CY}│{R}  {sym}  {label}{pad}", flush=True)
    return res


def die(msg, hint=""):
    item("err", msg)
    if hint:
        note(hint)
    endsection()
    sys.exit(1)


def banner():
    print(f"""{MG}
    _                    _   ____
   / \\   __ _  ___ _ __ | |_|___ \\
  / _ \\ / _` |/ _ \\ '_ \\| __| __) |
 / ___ \\ (_| |  __/ | | | |_ / __/
/_/   \\_\\__, |\\___|_| |_|\\__|_____|
        |___/{R}
  {B}Autonomous Terminal Agent{R}  {D}one-line installer{R}
  {D}github.com/aaravshah1311{R}
""")


def find_git():
    exe = shutil.which("git")
    if exe:
        return exe
    if IS_WIN:
        for c in (
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git/cmd/git.exe",
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Git/cmd/git.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Git/cmd/git.exe",
        ):
            if c.exists():
                return str(c)
    else:
        for c in ("/usr/bin/git", "/usr/local/bin/git", "/opt/homebrew/bin/git"):
            if Path(c).exists():
                return c
    return None


def _download_zip(dest):
    """Fetch the repo as a ZIP snapshot — the no-git fallback.

    git is the better path (it makes `run.py --update` a fast pull), but
    requiring it turned a one-line install into "go install git first, then come
    back". Locked-down machines, minimal containers and fresh Windows boxes very
    often have Python but no git, so we degrade instead of dying.
    """
    import io
    import ssl
    import zipfile
    import tempfile
    import urllib.request
    import urllib.error

    url = REPO_URL.rstrip("/") + "/archive/refs/heads/main.zip"
    try:
        ctx = ssl.create_default_context()
    except Exception:
        ctx = None

    item("run", "Downloading ZIP snapshot", "no git — using fallback")
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "Agent2-installer", "Accept": "application/zip"})
        with urllib.request.urlopen(req, timeout=90, context=ctx) as r:
            data = r.read()
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code} fetching {url}"
    except Exception as e:
        return str(getattr(e, "reason", e) or e)

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            tmp = Path(tempfile.mkdtemp(prefix="agent2_zip_", dir=str(dest.parent)))
            zf.extractall(tmp)
            # The archive wraps everything in one top folder (Agent-2-main/).
            tops = [p for p in tmp.iterdir() if p.is_dir()]
            src = tops[0] if len(tops) == 1 else tmp
            dest.mkdir(parents=True, exist_ok=True)
            for entry in src.iterdir():
                shutil.move(str(entry), str(dest / entry.name))
            shutil.rmtree(tmp, ignore_errors=True)
    except Exception as e:
        return f"could not unpack archive: {e}"

    item("ok", f"Repository downloaded  ({len(data) // 1024} KB)")
    return None


def main():
    banner()
    section("Checks")

    # 1) Python version -------------------------------------------------------
    if sys.version_info < (3, 9):
        die(f"Python 3.9+ required (you have {sys.version.split()[0]}).",
            "Install a newer Python from https://python.org/downloads")
    item("ok", f"Python {sys.version.split()[0]}")

    # 2) git — preferred, but no longer required (ZIP fallback below) ---------
    git = find_git()
    if git:
        item("ok", "git", git)
    else:
        item("warn", "git not found", "will download a ZIP snapshot instead")
        note("install git later for faster updates via `run.py --update`")

    # 3) venv + pip — run.py needs both to build the environment --------------
    missing = []
    for mod in ("venv", "ensurepip"):
        try:
            __import__(mod)
        except Exception:
            missing.append(mod)
    if missing:
        item("warn", f"python {'/'.join(missing)} missing",
             "run.py will try to recover")
        if not IS_WIN:
            note(f"if setup fails:  sudo apt install python3-venv python3-pip")
    else:
        item("ok", "venv + pip available")
    endsection()

    # 4) Target directory -----------------------------------------------------
    section("Download")
    if TARGET.exists() and any(TARGET.iterdir()):
        if (TARGET / "run.py").exists():
            item("info", "Agent2 is already here — updating instead", str(TARGET))
            endsection()
            _handoff(update=True)
            return
        die(f"Target directory is not empty: {TARGET}",
            "Move it aside or set AGENT2_DIR to an empty path, then re-run.")

    # Try git first (retried — a shallow clone dies on a flaky link), then fall
    # back to the ZIP snapshot. Only both failing is fatal.
    err = None
    if git:
        for attempt in (1, 2):
            suffix = "" if attempt == 1 else f" (retry {attempt}/2)"
            res = spin_run(f"Cloning repository{suffix}",
                           [git, "clone", "--depth", "1", REPO_URL, str(TARGET)])
            if res.returncode == 0:
                err = None
                break
            err = (res.stdout or "").strip()[:400]
            # A partial clone leaves a directory behind that would block a retry
            # and confuse the ZIP fallback's "not empty" check.
            if TARGET.exists():
                shutil.rmtree(TARGET, ignore_errors=True)
        if err:
            item("warn", "git clone failed", "trying ZIP download")
            note(err.replace("\n", " ")[:200])

    if err or not git:
        zip_err = _download_zip(TARGET)
        if zip_err:
            die(f"Download failed: {zip_err}",
                "Check your internet connection, then re-run this command.")

    if not (TARGET / "run.py").exists():
        die(f"Download finished but run.py is missing in {TARGET}.",
            "The archive looks incomplete — re-run this command.")
    endsection()

    _handoff(update=False)


def _handoff(update: bool):
    """Delegate to run.py so it creates the venv, installs deps, and launches."""
    run_py = TARGET / "run.py"
    if not run_py.exists():
        die(f"run.py not found in {TARGET} — the download looks incomplete.")

    if MODE == "none":
        section("Done")
        item("ok", "Agent2 is installed", str(TARGET))
        note(f"cd {TARGET}")
        note("python run.py          sets up .venv, installs deps, starts the CLI")
        note("python run.py --addapi add your free Gemini key")
        note("key:  https://aistudio.google.com/app/apikey")
        endsection()
        print()
        return

    flag = {"web": "--web", "dual": "--dual", "both": "--dual"}.get(MODE, "--cli")
    # If Agent2 is already present, genuinely pull the latest code (run.py --update
    # preserves agent2.db) instead of just relaunching — matches the message above.
    launch_args = ["--update"] if update else [flag]
    action = "Updating" if update else "Setting up"
    section(action)
    item("run", f"{action} Agent2 — handing off to run.py")
    note("builds a virtual environment and installs dependencies")
    note("first run takes a minute")
    endsection()

    # Our own stdin IS the install pipe — already consumed and at EOF. Handing
    # that to run.py would make its key prompt and mode menu read EOF instantly,
    # so a curl|python user could never paste a key. Reattach the real console
    # when there is one; run.py stays EOF-safe for the headless case.
    stdin = None
    try:
        if IS_WIN:
            stdin = open("CON", "r")
        elif os.path.exists("/dev/tty"):
            stdin = open("/dev/tty", "r")
    except Exception:
        stdin = None            # no controlling terminal (CI, Docker build, cron)

    try:
        subprocess.run([sys.executable, str(run_py), *launch_args],
                       cwd=str(TARGET), stdin=stdin)
    except KeyboardInterrupt:
        print(f"\n  {D}Stopped.{R}")
    except Exception as e:
        die(f"Could not start run.py: {e}",
            f"Run it manually:  cd {TARGET} && python run.py")
    finally:
        if stdin:
            try: stdin.close()
            except Exception: pass


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n  {D}Cancelled by user.{R}")
        sys.exit(0)
