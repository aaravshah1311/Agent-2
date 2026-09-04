# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/core/gitstate.py
───────────────────────
THE repository reader — one declaration of "what does git say about this
directory". Read by the Context Broker's Git-state source (Task 21), the CLI
status bar, and `/init` (Task 29).

⚠️ EVERY FUNCTION HERE IS TOTAL, AND THAT IS THE WHOLE POINT.
`git` may be absent from PATH, the directory may not be a repository, the
repository may live on a dead network mount, and `git status` on a very large
working tree genuinely takes seconds. All of that must read as *"no git
information"* — never as an exception, and never as an unbounded wait. So every
subprocess carries a timeout, every failure path returns the empty snapshot, and
nothing here raises. A context source that could raise would turn "we could not
read the branch" into a failed agent turn, which is the exact class of bug the
PIL modules are written to avoid.

⚠️ THE SNAPSHOT IS CACHED PER DIRECTORY WITH A TTL, AND THAT IS LOAD-BEARING.
`broker.assemble()` runs once per agent turn, and a turn is often one of many in
a minute. Uncached, each one would fork three `git` processes — ~90 ms on a warm
local repo, multiple seconds on a cold mount, paid on the user's latency. Branch,
dirty count and HEAD change on human timescales, so `GIT_TTL` seconds of
staleness is invisible; the fork storm is not. `invalidate()` exists for the two
moments the staleness *is* visible: a workspace switch and a commit made by the
agent itself.

⚠️ NOT A GIT PORCELAIN WRAPPER. This reads state and nothing else — there is no
commit, no checkout, no fetch. Writing to a user's repository is `run_command`'s
job, where the permission gate and the diff viewer can see it happen.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time

# Seconds a cached snapshot stays valid. Small enough that a branch switch shows
# up on the next turn, large enough that a burst of turns forks git once.
GIT_TTL = 15.0

# Per-subprocess ceiling. `git status` on a pathological tree can block; the
# caller gets the empty snapshot rather than a stalled turn.
GIT_TIMEOUT = 2.5

# How many recent commits `snapshot()` carries. The model wants "what has been
# happening here", not the project's history.
LOG_COUNT = 3

_EMPTY: dict = {
    "repo": False,
    "root": "",
    "branch": "",
    "detached": False,
    "head": "",
    "subject": "",
    "when": "",
    "dirty": 0,
    "staged": 0,
    "unstaged": 0,
    "untracked": 0,
    "ahead": 0,
    "behind": 0,
    "upstream": "",
    "commits": [],
    "at": 0.0,
}

_cache: dict[str, dict] = {}
_lock = threading.Lock()

_AHEAD = re.compile(r"\bahead (\d+)")
_BEHIND = re.compile(r"\bbehind (\d+)")


def empty() -> dict:
    """A fresh copy of the "not a repository" snapshot.

    A copy rather than the module constant: callers annotate what they read, and
    a shared mutable default would let one of them corrupt every later reader.
    """
    return dict(_EMPTY, commits=[])


def _run(args: list[str], cwd: str) -> str | None:
    """Run a git command and return its stdout, or None on any failure.

    Swallows everything on purpose — see the module docstring. `git` missing,
    exit code 128 ("not a git repository"), a timeout and a decode error are all
    the same answer to the caller: we do not know.
    """
    try:
        out = subprocess.run(
            ["git", *args],
            capture_output=True, text=True, timeout=GIT_TIMEOUT, cwd=cwd,
            # Windows: keep a console window from flashing on every read.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        return None
    if out.returncode != 0:
        return None
    return out.stdout or ""


def branch_now(cwd: str | None = None) -> str:
    """The current branch by one uncached `git` call, or "" outside a repo.

    Deliberately uncached and deliberately *one* call: `cli/statusbar.py` owns its
    own never-block / serve-stale policy for the render path and only needs this
    one field, so it must not pay for `snapshot()`'s two extra subprocesses.
    """
    try:
        cwd = cwd or os.getcwd()
    except Exception:
        return ""
    out = _run(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    return (out or "").strip()


def is_repo(cwd: str | None = None) -> bool:
    """Whether *cwd* is inside a git working tree. Reads the cached snapshot."""
    return bool(snapshot(cwd).get("repo"))


def _parse_status(text: str, snap: dict) -> None:
    """Fold `git status --porcelain=v1 -b` output into *snap*.

    One pass over the lines: the `## ` header carries branch, upstream and
    ahead/behind, and every other line is one path with a two-character state.
    Doing this from one subprocess is why ahead/behind costs nothing extra.
    """
    for line in text.splitlines():
        if line.startswith("## "):
            head = line[3:].strip()
            # "main...origin/main [ahead 1, behind 2]" | "HEAD (no branch)"
            bracket = head.find(" [")
            if bracket >= 0:
                tail = head[bracket:]
                ahead = _AHEAD.search(tail)
                behind = _BEHIND.search(tail)
                snap["ahead"] = int(ahead.group(1)) if ahead else 0
                snap["behind"] = int(behind.group(1)) if behind else 0
                head = head[:bracket].strip()
            if "..." in head:
                local, _, upstream = head.partition("...")
                snap["branch"] = local.strip()
                snap["upstream"] = upstream.strip()
            else:
                snap["branch"] = head.strip()
            if snap["branch"].startswith("HEAD (") or snap["branch"] == "HEAD":
                snap["detached"] = True
            continue
        if len(line) < 2:
            continue
        x, y = line[0], line[1]
        if x == "?" and y == "?":
            snap["untracked"] += 1
        else:
            if x not in (" ", "?"):
                snap["staged"] += 1
            if y not in (" ", "?"):
                snap["unstaged"] += 1
        snap["dirty"] += 1


def _read(cwd: str) -> dict:
    """Three git calls into one snapshot. Never raises."""
    snap = empty()
    snap["at"] = time.time()

    top = _run(["rev-parse", "--show-toplevel"], cwd)
    if top is None:
        return snap                      # not a repo, or no git — the empty answer
    snap["repo"] = True
    snap["root"] = (top or "").strip().splitlines()[0] if (top or "").strip() else ""

    status = _run(["status", "--porcelain=v1", "-b"], cwd)
    if status:
        try:
            _parse_status(status, snap)
        except Exception:
            pass                          # a weird porcelain line must not lose the repo

    # `%h|%s|%cr` — short hash, subject, relative date. `-z` is not used because
    # a subject may contain the separator and the count is small either way.
    log = _run(["log", f"-{LOG_COUNT}", "--pretty=format:%h\x1f%s\x1f%cr"], cwd)
    if log:
        for row in log.splitlines():
            bits = row.split("\x1f")
            if len(bits) != 3:
                continue
            snap["commits"].append(
                {"hash": bits[0], "subject": bits[1][:200], "when": bits[2]})
    if snap["commits"]:
        snap["head"] = snap["commits"][0]["hash"]
        snap["subject"] = snap["commits"][0]["subject"]
        snap["when"] = snap["commits"][0]["when"]
    return snap


def snapshot(cwd: str | None = None, *, refresh: bool = False) -> dict:
    """Cached git state for *cwd* (default: the process directory).

    Returns a fresh dict each call, so a caller may annotate its copy without
    poisoning the cache. Outside a repository every field is the empty default
    and `repo` is False — callers branch on that, never on a missing key.
    """
    try:
        key = os.path.normcase(os.path.abspath(cwd or os.getcwd()))
    except Exception:
        return empty()

    now = time.time()
    if not refresh:
        with _lock:
            hit = _cache.get(key)
            if hit is not None and (now - hit.get("at", 0.0)) < GIT_TTL:
                return dict(hit, commits=list(hit.get("commits") or []))

    snap = _read(key)
    with _lock:
        _cache[key] = snap
    return dict(snap, commits=list(snap.get("commits") or []))


def invalidate(cwd: str | None = None) -> None:
    """Drop the cached snapshot for *cwd*, or every one when *cwd* is None."""
    try:
        with _lock:
            if cwd is None:
                _cache.clear()
                return
            _cache.pop(os.path.normcase(os.path.abspath(cwd)), None)
    except Exception:
        pass


def describe(cwd: str | None = None) -> str:
    """One human line: `main ↑1 · 3 modified, 1 untracked`, or "" outside a repo.

    Shared by the broker's prompt block and any surface that wants the same
    sentence — a second formatter would drift the moment a field is added.
    """
    snap = snapshot(cwd)
    if not snap.get("repo"):
        return ""
    bits: list[str] = [snap.get("branch") or "(detached)"]
    if snap.get("ahead"):
        bits.append(f"↑{snap['ahead']}")
    if snap.get("behind"):
        bits.append(f"↓{snap['behind']}")
    head = " ".join(bits)

    work: list[str] = []
    if snap.get("staged"):
        work.append(f"{snap['staged']} staged")
    if snap.get("unstaged"):
        work.append(f"{snap['unstaged']} modified")
    if snap.get("untracked"):
        work.append(f"{snap['untracked']} untracked")
    return f"{head} · {', '.join(work)}" if work else f"{head} · clean"
