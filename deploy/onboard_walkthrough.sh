#!/usr/bin/env bash
# Bring a code in from its bare baseline, against the running stack.
#
# The same container and network as walkthrough.sh, for the same reason: the
# gateway is only on internal networks, and nothing outside them can reach it.
# Arguments are passed to the walkthrough.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${here}"

EQUIVALENT_UID="$(id -u)"
EQUIVALENT_GID="$(id -g)"
export EQUIVALENT_UID EQUIVALENT_GID

exec docker compose run --rm onboard_walkthrough "$@"
