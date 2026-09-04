#!/usr/bin/env bash
# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311
#
# Launches Agent2 inside the container. The image already built /app/.venv and
# put it first on PATH, so `python` here is the venv interpreter — which is what
# agent2web.py / agent2cli.py require (they self-restart into .venv otherwise).
set -euo pipefail

MODE="${AGENT2_MODE:-cli}"

# There is no browser inside the container, and no display to open one on. The
# host-side `agent2` command is what opens the tab; a container-side attempt
# would just log a failure on every start.
export AGENT2_NO_BROWSER=1

# Allow `docker run ... <anything>` to override the launch entirely.
if [ "$#" -gt 0 ]; then
    exec python "$@"
fi

case "$MODE" in
    web)
        echo ">> Agent2 Web UI  ->  http://localhost:1311"
        exec python /app/agent2web.py
        ;;
    cli)
        exec python /app/agent2cli.py
        ;;
    dual|both)
        # `both` stays accepted as a silent alias so older compose files and
        # shell history keep working after the rename to `dual`.
        echo ">> Agent2 DUAL MODE"
        echo ">>   Web UI  ->  http://localhost:1311  (background)"
        echo ">>   CLI     ->  this terminal (needs -it; use \`agent2 dual\`)"
        exec python /app/agent2dual.py
        ;;
    *)
        echo "Unknown AGENT2_MODE='$MODE' (expected 'web', 'cli' or 'dual')." >&2
        exit 1
        ;;
esac
