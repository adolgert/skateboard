#!/usr/bin/env bash
# Prepare the state directories, seed the baseline, and start the services the
# gateway needs. Safe to run again: it creates what is missing and leaves
# everything that already exists alone. It removes nothing.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${here}/.." && pwd)"
cd "${here}"

if [ ! -f .env ]; then
    echo "deploy/.env is missing. Copy .env.example to .env and set a token." >&2
    exit 1
fi
# shellcheck disable=SC1091
. ./.env

# The gateway and the walkthrough run as this account, so that everything
# they write under state/ is readable here without changing ownership.
EQUIVALENT_UID="$(id -u)"
EQUIVALENT_GID="$(id -g)"
export EQUIVALENT_UID EQUIVALENT_GID

region="${EQUIVALENT_REGION:-ch04:step}"
# A region id spelled as a directory name: the gateway files each region's
# ledger under its id with the colon replaced by a dash.
region_dir="${region//:/-}"

mkdir -p state/repo state/ledger state/working state/sessions state/seed state/pi-home/agent

echo "== baseline seed =="
python3 "${here}/seed.py" state/seed

# The agent's working copy starts as the baseline. Only when it is empty: a
# working copy with anything in it is the person's session in progress.
if [ -z "$(ls -A state/working 2>/dev/null)" ]; then
    echo "== working copy starts from the baseline =="
    cp -r state/seed/. state/working/
fi

echo "== images =="
# The agent's image is deliberately not built here: it is a long build on a
# very large base. pi.sh builds it the first time it is needed.
docker compose build gateway builder oracle

echo "== services =="
docker compose up -d builder oracle gateway

echo "== waiting for the gateway =="
gateway_state=unknown
for _ in $(seq 1 60); do
    container="$(docker compose ps -q gateway)"
    if [ -n "${container}" ]; then
        gateway_state="$(docker inspect -f '{{.State.Health.Status}}' "${container}" 2>/dev/null || echo unknown)"
    fi
    [ "${gateway_state}" = "healthy" ] && break
    sleep 2
done
if [ "${gateway_state}" != "healthy" ]; then
    echo "the gateway did not become healthy (last state: ${gateway_state})" >&2
    echo "logs: docker compose -f ${here}/docker-compose.yml logs gateway" >&2
    exit 1
fi

baseline="$(git -C state/repo rev-parse main)"

# The same configuration the gateway reads, with the paths of this machine
# rather than the container's mount points, so the ledger command line and the
# gateway describe one deployment and not two.
cat > state/gateway.host.yaml <<YAML
# Written by up.sh. The gateway reads deploy/gateway.yaml, in the container's
# terms; this is the same deployment seen from this machine. Edit gateway.yaml
# and re-run up.sh rather than editing this file.
version: 1
paths:
  repo: ${here}/state/repo
  ledger_root: ${here}/state/ledger
  working_copy: ${here}/state/working
  datasets_root: ${repo_root}/demo/orchestrator/datasets
  strategies: ${repo_root}/equivalent/strategy/files
regions:
  "${region}":
    spec_path: notes/regions/ch04-step.sese.yaml
    strategy: stdpar_managed
    visible_dataset: visible
YAML

echo
echo "baseline commit ${baseline}"
echo "ledger          ${here}/state/ledger/${baseline}/${region_dir}"
echo
echo "read it with:   ledger status --config ${here}/state/gateway.host.yaml --region-id ${region}"
echo "end-to-end:     ${here}/walkthrough.sh"
echo "a session:      ${here}/pi.sh"
