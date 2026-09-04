#!/usr/bin/env bash
# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311
#
# dockerinstall.sh — macOS / Linux one-shot Docker installer for Agent2.
#
# This is a thin entry point: it locates a Python interpreter and hands off to
# `run.py --docker`, which is the SINGLE source of truth for the whole Docker
# flow (pick OS -> ensure/auto-install Docker -> install the global `agent2`
# command -> first build/start). Running this script gives the EXACT same result
# as `python run.py --docker`.
#
# Usage (from anywhere):
#   bash docker/dockerinstall.sh
#   # or, once executable:
#   ./docker/dockerinstall.sh

set -euo pipefail

# Project root = the parent of this script's folder (docker/..).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RUN_PY="$ROOT/run.py"

printf '\n  == Agent2 Docker Installer (macOS/Linux) ==\n\n'

if [ ! -f "$RUN_PY" ]; then
    printf '  [ERR] run.py not found at %s\n' "$RUN_PY" >&2
    printf '        Run this script from inside the Agent2 project.\n' >&2
    exit 1
fi

# Find a Python interpreter: prefer python3, then python.
PY=""
for cand in python3 python; do
    if command -v "$cand" >/dev/null 2>&1; then
        PY="$(command -v "$cand")"
        break
    fi
done

if [ -z "$PY" ]; then
    printf '  [ERR] Python 3.9+ was not found on PATH.\n' >&2
    printf '        Install it (e.g. `sudo apt install python3` / `brew install python`) and retry.\n' >&2
    exit 1
fi

printf '  [OK] Python: %s\n' "$PY"
printf '  [>>] Handing off to run.py --docker ...\n\n'

# Hand off. run.py --docker owns everything from here (incl. auto-installing
# Docker via the platform package manager and installing the global `agent2`
# command).
exec "$PY" "$RUN_PY" --docker
