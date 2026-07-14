#!/usr/bin/env bash
# Verify the Brev Secure Link target without printing credentials or job data.
set -Eeuo pipefail

readonly BASE_URL="${1:?Pass the Brev Secure Link URL, for example https://afb-<instance>.brev.dev}"

require_nonempty() {
  local variable_name="$1"
  if [[ -z "${!variable_name:-}" ]]; then
    printf 'Required Brev secret %s is not set.\n' "${variable_name}" >&2
    exit 1
  fi
}

require_nonempty AFB_TOOL_SERVER_TOKEN
command -v curl >/dev/null

health="$(curl --fail --silent --show-error --max-time 15 "${BASE_URL%/}/healthz")"
catalogue="$(curl --fail --silent --show-error --max-time 15 \
  --header "Authorization: Bearer ${AFB_TOOL_SERVER_TOKEN}" \
  "${BASE_URL%/}/v1/tools")"

if ! grep --quiet '"status": "ready"' <<<"${health}"; then
  printf 'The Brev health response did not report ready.\n' >&2
  exit 1
fi
if ! grep --quiet 'asset_programme_intake' <<<"${catalogue}" || ! grep --quiet 'asset_factory_start' <<<"${catalogue}"; then
  printf 'The Brev tool catalogue does not expose the guided factory entry points.\n' >&2
  exit 1
fi

printf 'Brev Secure Link health and guided factory tool catalogue are ready.\n'
