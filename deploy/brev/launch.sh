#!/usr/bin/env bash
# Start the Asset Factory Blueprint HTTP control plane on a Brev instance.
set -Eeuo pipefail

readonly REPOSITORY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
readonly COMPOSE_FILE="${REPOSITORY_ROOT}/deploy/brev/compose.yaml"
readonly COMPOSE_PROJECT_DIRECTORY="${REPOSITORY_ROOT}/deploy/brev"
readonly SERVICE="asset-factory"
readonly PORT="${AFB_BREV_PORT:-8181}"

require_nonempty() {
  local variable_name="$1"
  if [[ -z "${!variable_name:-}" ]]; then
    printf 'Required Brev secret %s is not set.\n' "${variable_name}" >&2
    exit 1
  fi
}

require_minimum_bytes() {
  local variable_name="$1"
  local minimum_bytes="$2"
  local actual_bytes
  actual_bytes="$(printf '%s' "${!variable_name}" | wc -c | tr -d ' ')"
  if (( actual_bytes < minimum_bytes )); then
    printf 'Brev secret %s must contain at least %s bytes.\n' "${variable_name}" "${minimum_bytes}" >&2
    exit 1
  fi
}

require_nonempty AFB_TOOL_SERVER_TOKEN
require_nonempty AFB_TOOL_SERVER_APPROVAL_SECRET
require_minimum_bytes AFB_TOOL_SERVER_TOKEN 32
require_minimum_bytes AFB_TOOL_SERVER_APPROVAL_SECRET 32

if [[ "${AFB_TOOL_SERVER_TOKEN}" == "${AFB_TOOL_SERVER_APPROVAL_SECRET}" ]]; then
  printf 'AFB_TOOL_SERVER_TOKEN and AFB_TOOL_SERVER_APPROVAL_SECRET must be different values.\n' >&2
  exit 1
fi

command -v docker >/dev/null
docker compose version >/dev/null
command -v curl >/dev/null

if [[ "$(id -u)" == "0" ]]; then
  printf 'Run the Brev setup command as its non-root instance user.\n' >&2
  exit 1
fi
export AFB_BREV_UID="${AFB_BREV_UID:-$(id -u)}"
export AFB_BREV_GID="${AFB_BREV_GID:-$(id -g)}"

for directory in projects artifacts library/downloads .cache/afb; do
  install -d -m 0750 "${REPOSITORY_ROOT}/${directory}"
done

docker compose --project-directory "${COMPOSE_PROJECT_DIRECTORY}" --file "${COMPOSE_FILE}" build --pull "${SERVICE}"
docker compose --project-directory "${COMPOSE_PROJECT_DIRECTORY}" --file "${COMPOSE_FILE}" up --detach --remove-orphans "${SERVICE}"

for _ in $(seq 1 30); do
  if curl --fail --silent --show-error --max-time 3 "http://127.0.0.1:${PORT}/healthz" >/dev/null; then
    printf 'Asset Factory Blueprint is ready on port %s.\n' "${PORT}"
    exit 0
  fi
  sleep 1
done

docker compose --project-directory "${COMPOSE_PROJECT_DIRECTORY}" --file "${COMPOSE_FILE}" logs --tail 100 "${SERVICE}" >&2
printf 'Asset Factory Blueprint did not become healthy on port %s.\n' "${PORT}" >&2
exit 1
