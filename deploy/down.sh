#!/usr/bin/env bash
# Stop the services. With --reset, also discard the repository, the ledger,
# the working copy, and the session files, after asking. The login in
# state/pi-home is never touched.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${here}"

reset=0
for arg in "$@"; do
    case "${arg}" in
        --reset) reset=1 ;;
        *) echo "usage: down.sh [--reset]" >&2; exit 2 ;;
    esac
done

docker compose down

if [ "${reset}" = "1" ]; then
    echo
    echo "This deletes, under ${here}/state:"
    echo "  repo/      the gateway's git repository, including every region branch"
    echo "  ledger/    every claim and every request ever recorded"
    echo "  working/   the agent's working copy, including unsubmitted edits"
    echo "  sessions/  the session transcripts"
    echo "It keeps pi-home/ (the login) and seed/ (the baseline)."
    printf 'Delete them? [y/N] '
    read -r answer
    case "${answer}" in
        y|Y)
            rm -rf state/repo state/ledger state/working state/sessions
            echo "removed. run up.sh to start again from the baseline."
            ;;
        *)
            echo "nothing was removed."
            ;;
    esac
fi
