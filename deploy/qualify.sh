#!/usr/bin/env bash
# Build the trusted executor image and record its boundary qualification.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
report="${SKATEBOARD_QUALIFICATION_REPORT:-${here}/state/builder-qualification.json}"
preflight_args=(--require-gpu)

if [[ "${1:-}" == "--cpu-only" ]]; then
  preflight_args=()
  shift
fi
if [[ "$#" -ne 0 ]]; then
  echo "usage: $0 [--cpu-only]" >&2
  exit 64
fi

mkdir -p "$(dirname "${report}")"
if [[ "${#preflight_args[@]}" -eq 0 ]]; then
  docker build -f "${here}/../services/builder/Dockerfile" \
    -t skateboard/builder "${here}/.."
  docker volume create equivalent_builder_work >/dev/null
  set +e
  docker run --rm --name skateboard-builder-qualification \
    --read-only --cap-drop ALL \
    --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add FOWNER \
    --security-opt no-new-privileges:true \
    --tmpfs /tmp:rw,noexec,nosuid,nodev,size=256m,mode=1777 \
    --tmpfs /run:rw,noexec,nosuid,nodev,size=16m,mode=755 \
    --mount type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock \
    --mount type=volume,src=equivalent_builder_work,dst=/work \
    -e SKATEBOARD_JOB_IMAGE=skateboard/builder \
    -e SKATEBOARD_WORK_VOLUME=equivalent_builder_work \
    --entrypoint python3 skateboard/builder \
    -m services.builder.preflight >"${report}"
else
  docker compose -f "${here}/docker-compose.yml" up -d --build builder
  set +e
  docker compose -f "${here}/docker-compose.yml" exec -T builder \
    python3 -m services.builder.preflight "${preflight_args[@]}" >"${report}"
fi
status=$?
set -e
python3 -m json.tool "${report}"
echo "qualification record: ${report}"
exit "${status}"
