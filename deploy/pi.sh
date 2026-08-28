#!/usr/bin/env bash
# Open an interactive session in the agent container.
#
# `docker compose run` attaches this terminal's stdin and allocates a TTY by
# default, which is what the session needs; the stdin_open/tty settings in the
# compose file are there for the other way of starting it.
#
# An argument that begins with a dash is an option for the session and is added
# to the command below (`./pi.sh --list-models`). Anything else replaces the
# command outright, which is how the isolation check is run
# (`./pi.sh /opt/isolation_check.sh`).
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

# The same command as the image's default. It is spelled out here because an
# option added to it has to follow it, and the two have to agree.
session=(pi --extension /opt/pi-extension/src/extension.ts --session-dir /sessions)

if [ "$#" -gt 0 ] && [ "${1#-}" != "$1" ]; then
    exec docker compose run --rm agent "${session[@]}" "$@"
fi

exec docker compose run --rm agent "$@"
