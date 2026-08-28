#!/usr/bin/env bash
# Open an interactive session in the agent container.
#
# `docker compose run` attaches this terminal's stdin and allocates a TTY by
# default, which is what the session needs; the stdin_open/tty settings in the
# compose file are there for the other way of starting it. Arguments given here
# are passed on to the session command.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${here}"

if [ ! -f .env ]; then
    echo "deploy/.env is missing. Copy .env.example to .env and set a token." >&2
    exit 1
fi

if ! docker image inspect equivalent-agent >/dev/null 2>&1; then
    echo "== building the agent image (first time; its base layer is large) =="
    docker compose build agent
fi

exec docker compose run --rm agent "$@"
