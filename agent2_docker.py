# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2_docker.py
────────────────
The Docker-backed driver behind the global `agent2` command.

`run.py --docker` installs a tiny `agent2` wrapper on your PATH that simply calls
this script, so typing `agent2` in ANY terminal drives the container:

    agent2            → open an interactive CLI session (default)
    agent2 cli        → same as above
    agent2 web        → start the Web UI (builds on first run) and open the browser
    agent2 dual       → start the Web UI in the background AND open a CLI here
    agent2 stop       → stop the container (keeps your data volume)
    agent2 restart    → restart the container
    agent2 logs       → follow container logs
    agent2 status     → show container status
    agent2 update     → rebuild the image from the latest code
    agent2 uninstall  → stop and remove the container AND its data volume
    agent2 help       → show this help

Only the Python standard library is used, so this runs without any venv or
project dependencies — all it needs is Docker.
"""

import os
import sys
import time
import shutil
import subprocess
import webbrowser
import platform
from pathlib import Path
from urllib.request import urlopen

# On Windows a non-UTF-8 console (cp1252) crashes when printing the → / · glyphs
# used below. Force UTF-8 on stdout/stderr where the runtime supports it.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT     = Path(__file__).resolve().parent
COMPOSE  = ROOT / "docker-compose.yml"
PROJECT  = "agent2"                 # stable compose project name (cwd-independent)
SERVICE  = "agent2"
URL      = "http://localhost:1311"
IS_WIN   = os.name == "nt"
IS_MAC   = platform.system() == "Darwin"

# ── tiny ANSI helpers ────────────────────────────────────────────────────────
_C = sys.stdout.isatty()
def _p(code, s): return f"\033[{code}m{s}\033[0m" if _C else s
def g(s):  return _p("32", s)
def y(s):  return _p("33", s)
def r(s):  return _p("31", s)
def c(s):  return _p("36", s)
def dim(s):return _p("2",  s)


# ── docker plumbing ────────────────────────────────────────────────────────────
def _compose_cmd() -> list[str]:
    """Return the compose invocation prefix: `docker compose` (v2) or the legacy
    `docker-compose` (v1), whichever is available."""
    if shutil.which("docker"):
        try:
            subprocess.run(["docker", "compose", "version"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           check=True)
            return ["docker", "compose"]
        except Exception:
            pass
    if shutil.which("docker-compose"):
        return ["docker-compose"]
    return []


def _base() -> list[str]:
    """Full compose prefix bound to our file + project name."""
    return _compose_cmd() + ["-f", str(COMPOSE), "-p", PROJECT]


def _daemon_up() -> bool:
    try:
        subprocess.run(["docker", "info"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       check=True)
        return True
    except Exception:
        return False


def _docker_desktop_path():
    """Best-effort path to Docker Desktop, for auto-start on Win/Mac."""
    if IS_WIN:
        for env in ("ProgramFiles", "ProgramW6432"):
            base = os.environ.get(env)
            if base:
                p = Path(base) / "Docker" / "Docker" / "Docker Desktop.exe"
                if p.exists():
                    return p
    elif IS_MAC:
        p = Path("/Applications/Docker.app")
        if p.exists():
            return p
    return None


def _ensure_daemon() -> bool:
    """Make sure the Docker daemon is reachable, trying to start Docker Desktop
    once if it is installed. Returns True if the daemon is up."""
    if _daemon_up():
        return True

    app = _docker_desktop_path()
    if app is None:
        print(r("  [ERR] Docker isn't running and Docker Desktop wasn't found."))
        print(dim("        Start Docker, then run `agent2` again."))
        return False

    print(y("  [..] Docker isn't running — starting Docker Desktop..."))
    try:
        if IS_WIN:
            subprocess.Popen([str(app)], close_fds=True)
        elif IS_MAC:
            subprocess.Popen(["open", "-a", "Docker"])
    except Exception as e:
        print(r(f"  [ERR] Couldn't launch Docker Desktop: {e}"))
        return False

    for i in range(60):          # wait up to ~2 min
        if _daemon_up():
            print(g(f"  [OK] Docker is ready ({(i + 1) * 2}s)."))
            return True
        time.sleep(2)
    print(r("  [ERR] Docker didn't become ready in time. Try `agent2` again shortly."))
    return False


def _run(args: list[str]) -> int:
    """Run a compose command, streaming output. Returns exit code."""
    base = _base()
    if not base:
        print(r("  [ERR] Docker Compose not found. Install Docker Desktop first."))
        return 1
    return subprocess.run(base + args).returncode


# ── Image readiness (the Docker equivalent of run.py's dependency fast path) ───
#
# In the container, "libraries" are baked into the image at build time, so the
# per-launch check that matters here is "does the image exist". An explicit
# `agent2 cli` / `agent2 web` / `agent2 dual` is a request to START: it passes
# --no-build so compose does not re-resolve the build context on every launch.
# A bare `agent2` is the setup entry point and builds when the image is missing.
# Either way, if the image genuinely is not there we build it rather than failing
# ("if error rise").
FAST = False          # set in main() when an explicit subcommand was given


def _image_exists() -> bool:
    try:
        return subprocess.run(
            ["docker", "image", "inspect", "agent2:latest"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    except Exception:
        return False


def _build_args() -> list[str]:
    """`--no-build` on the fast path, but only once the image really exists."""
    if FAST and _image_exists():
        return ["--no-build"]
    if FAST:
        print(dim("  Image not built yet — building once, then future starts skip this."))
    return []


# ── actions ────────────────────────────────────────────────────────────────────
def _wait_and_open():
    """Poll the web server, then open the browser once it answers."""
    print(dim(f"  Waiting for {URL} ..."))
    for _ in range(30):
        try:
            with urlopen(URL, timeout=2) as resp:
                if resp.status == 200:
                    break
        except Exception:
            time.sleep(1)
    print(g(f"  [OK] Agent2 is up  ->  {URL}"))
    try:
        webbrowser.open(URL)
    except Exception:
        pass


def up(_args):
    if not _ensure_daemon():
        return 1
    if FAST and _image_exists():
        print(c("  Starting Agent2 Web UI ..."))
    else:
        print(c("  Starting Agent2 (building image on first run, please wait)..."))
    code = _run(["up", "-d"] + _build_args())
    if code != 0:
        print(r("  [ERR] Failed to start. See output above."))
        return code
    _wait_and_open()
    print(dim("  Tip: `agent2 stop` to stop  ·  `agent2 logs` to watch output"))
    return 0


def cli(_args):
    if not _ensure_daemon():
        return 1
    print(c("  Opening Agent2 CLI inside the container (Ctrl+D / /exit to leave)...\n"))
    return _run(["run", "--rm"] + _build_args()
                + ["-e", "AGENT2_MODE=cli", SERVICE])


def dual(_args):
    """Web UI detached in the background + an interactive CLI in this terminal.

    The detached `up` owns the published 1311 port and the browser session; the
    one-off container we attach to runs CLI-only against the SAME data volume,
    so the two halves stay in sync. Running agent2dual.py here instead would
    start a second web server that could not bind the already-published port.
    """
    if not _ensure_daemon():
        return 1
    print(c("  Starting Agent2 dual mode ..."))
    code = _run(["up", "-d"] + _build_args())
    if code != 0:
        print(r("  [ERR] Failed to start the web half. See output above."))
        return code
    _wait_and_open()
    print(c("\n  Web UI is running in the background."))
    print(dim("  Opening the CLI here — /exit leaves the CLI, the Web UI keeps running."))
    print(dim("  Stop everything with: agent2 stop\n"))
    # The image is guaranteed present by the `up` above, so the CLI half always
    # takes the no-build path regardless of how we were invoked.
    return _run(["run", "--rm", "--no-build",
                 "-e", "AGENT2_MODE=cli", SERVICE])


def stop(_args):
    return _run(["down"])


def restart(_args):
    _run(["down"])
    return up(_args)


def logs(_args):
    return _run(["logs", "-f"])


def status(_args):
    return _run(["ps"])


def update(_args):
    if not _ensure_daemon():
        return 1
    print(c("  Rebuilding Agent2 from the latest code..."))
    code = _run(["up", "-d", "--build"])
    if code == 0:
        _wait_and_open()
    return code


def uninstall(_args):
    print(y("  This stops the container and DELETES its data volume"))
    print(y("  (API keys, providers, memories, rules stored in Docker will be lost)."))
    try:
        ans = input("  Continue? [y/N]: ").strip().lower()
    except EOFError:
        ans = "n"
    if ans != "y":
        print(g("  Aborted."))
        return 0
    return _run(["down", "-v"])


def show_help(_args=None):
    print(__doc__.strip())
    return 0


ACTIONS = {
    # Bare `agent2` opens the CLI — the fastest path to a working session and
    # consistent with native `run.py`, where CLI is the default.
    "":          cli,
    "cli":       cli,
    "up":        up,
    "web":       up,
    "start":     up,
    "open":      up,
    "dual":      dual,
    "both":      dual,          # legacy alias, kept so old habits keep working
    "stop":      stop,
    "down":      stop,
    "restart":   restart,
    "logs":      logs,
    "status":    status,
    "ps":        status,
    "update":    update,
    "rebuild":   update,
    "uninstall": uninstall,
    "help":      show_help,
    "-h":        show_help,
    "--help":    show_help,
}


def main():
    if not COMPOSE.exists():
        print(r(f"  [ERR] docker-compose.yml not found next to {Path(__file__).name}."))
        print(dim(f"        Expected at: {COMPOSE}"))
        sys.exit(1)

    args = sys.argv[1:]
    cmd = (args[0] if args else "").lower()

    # An explicit start subcommand is a request to START, so skip the build
    # check. A bare `agent2` is the setup entry point and keeps the full path.
    # update/rebuild always build — that is the whole point of the command.
    global FAST
    FAST = cmd in ("cli", "web", "dual", "both", "up", "start", "open")

    action = ACTIONS.get(cmd)
    if action is None:
        print(r(f"  Unknown command: {cmd}"))
        show_help()
        sys.exit(2)
    sys.exit(action(args[1:]) or 0)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(dim("\n  Stopped."))
        sys.exit(130)
