#!/usr/bin/env bash
# Run the end-to-end walkthrough against the running stack.
#
# It runs in a container rather than on this machine because the gateway is
# only on internal networks: nothing outside those networks can reach it, and
# that is the property worth keeping. Arguments are passed to the walkthrough.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${here}"

EQUIVALENT_UID="$(id -u)"
EQUIVALENT_GID="$(id -g)"
export EQUIVALENT_UID EQUIVALENT_GID

exec docker compose run --rm walkthrough "$@"
