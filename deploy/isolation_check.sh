#!/usr/bin/env bash
# Assert, from inside the agent's container, that the isolation the compose
# file describes is real. Run it there:
#
#   deploy/pi.sh /opt/isolation_check.sh
#
# Stops at the first failure. Every line says what was checked and what
# happened, so a failure reads as a finding rather than a stack trace.
set -u

fail() {
    echo "FAIL  $*"
    exit 1
}

pass() {
    echo "ok    $*"
}

: "${EQUIVALENT_GATEWAY_URL:?EQUIVALENT_GATEWAY_URL is not set}"
: "${EQUIVALENT_GATEWAY_TOKEN:?EQUIVALENT_GATEWAY_TOKEN is not set}"
: "${EQUIVALENT_REGION:?EQUIVALENT_REGION is not set}"

# 1. The gateway answers, with the token. The table is asked for by region:
#    the actions a session has are the actions of its region's phase.
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
    -H "Authorization: Bearer ${EQUIVALENT_GATEWAY_TOKEN}" \
    --get --data-urlencode "region=${EQUIVALENT_REGION}" \
    "${EQUIVALENT_GATEWAY_URL}/table")"
[ "${code}" = "200" ] || fail "the gateway's action table answered ${code}, not 200"
pass "the gateway answers on ${EQUIVALENT_GATEWAY_URL}"

# 2. The builder and the oracle are not reachable from here. There is no route
#    to them, so this is a name that does not resolve or a connection that
#    never completes -- not a rejected request.
for backend in builder:9090 oracle:7070; do
    if curl -s -o /dev/null --max-time 3 "http://${backend}/"; then
        fail "${backend} answered; the agent must share no network with it"
    fi
    pass "${backend} is unreachable"
done

# 3. The gateway's own directories are not mounted here.
for private in /ledger /repo; do
    [ -e "${private}" ] && fail "${private} exists here; only the gateway may hold it"
    pass "${private} is not present"
done

# 4. The model provider is reachable. Any HTTP status counts: what is being
#    checked is that the route out exists.
curl -sI --max-time 15 -o /dev/null https://api.anthropic.com \
    || fail "no route to the model provider"
pass "the model provider is reachable"

# 5. The working copy is writable -- this is where a session does its editing.
probe="/working/.isolation_check_probe"
touch "${probe}" || fail "/working is not writable"
rm -f "${probe}"
pass "/working is writable"

echo "all checks passed"
