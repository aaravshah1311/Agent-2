# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311
#
# Agent2 container image.
#
# agent2web.py enforces execution inside a .venv at the project root (it will
# os.execv into .venv/bin/python otherwise), so we build that exact venv into
# the image at /app/.venv. This keeps the runtime contract identical to a bare
# `python run.py --web` install — no code branching for "is this Docker?".

FROM python:3.12-slim

# Keep Python snappy and unbuffered so container logs stream in real time.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VENV_DIR=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

# git: run.py's --update path and some tooling expect it. curl: healthcheck.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Build the venv that agent2web.py / agent2cli.py expect at /app/.venv.
RUN python -m venv /app/.venv \
    && /app/.venv/bin/python -m pip install --upgrade pip

# Install deps first (better layer caching — only re-runs when deps change).
COPY requirements.txt ./
RUN /app/.venv/bin/pip install -r requirements.txt

# App source. .dockerignore keeps the host .venv, DB and caches out.
COPY . .

# Persistent state (agent2.db) lives here, backed by a named volume so keys,
# providers, memories and rules survive rebuilds and can be shared per-device.
ENV AGENT2_DB=/data
RUN mkdir -p /data
VOLUME ["/data"]

# Web UI port (agent2web.py binds 0.0.0.0:1311).
EXPOSE 1311

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://localhost:1311/ >/dev/null || exit 1

# Entrypoint picks cli (default), web, or both via AGENT2_MODE, and runs inside
# the venv Python so the in-app venv guard is already satisfied. Note the compose
# service overrides AGENT2_MODE to "web": a detached container has no TTY for a
# CLI, so `agent2 cli` / `agent2 dual` run one-off interactive containers instead.
# We normalise line
# endings / strip any BOM at build time (the repo may be edited on Windows) and
# invoke it via `bash` explicitly so a stray shebang byte can't break exec.
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN sed -i '1s/^\xEF\xBB\xBF//' /usr/local/bin/docker-entrypoint.sh \
    && sed -i 's/\r$//' /usr/local/bin/docker-entrypoint.sh \
    && chmod +x /usr/local/bin/docker-entrypoint.sh
ENTRYPOINT ["bash", "/usr/local/bin/docker-entrypoint.sh"]
