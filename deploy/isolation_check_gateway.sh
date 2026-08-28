#!/usr/bin/env bash
# Assert, from this machine, the two properties of the gateway container that
# cannot be checked from inside the agent's: it has no way out to the internet,
# and it sees the agent's working copy read-only.
set -u

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${here}"

fail() {
    echo "FAIL  $*"
    exit 1
}

pass() {
    echo "ok    $*"
}

# The gateway image has curl for its own healthcheck, so the same tool answers
# both questions.
if docker compose exec -T gateway curl -sI --max-time 8 -o /dev/null https://api.anthropic.com; then
    fail "the gateway reached the internet; it must be on internal networks only"
fi
pass "the gateway has no route to the internet"

if docker compose exec -T gateway touch /working/.isolation_check_probe 2>/dev/null; then
    docker compose exec -T gateway rm -f /working/.isolation_check_probe
    fail "the gateway wrote to /working; it must be mounted read-only there"
fi
pass "/working is read-only for the gateway"

echo "all checks passed"
