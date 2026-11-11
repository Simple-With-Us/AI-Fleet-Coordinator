#!/usr/bin/env bash
# Mac origin for scout.jays.services. Do not hairpin SENATE_RELAY_URL.
# SENATE_RELAY_SECRET (Infisical Fleet-OPS /senate-relay, read with `fleet-secrets`) is fetched only when
# SENATE_RELAY_REQUIRE=1 so production can start sending the header before this origin fail-closes.
set -euo pipefail
cd "$(dirname "$0")"
if [[ "${SENATE_RELAY_REQUIRE:-0}" == "1" && -z "${SENATE_RELAY_SECRET:-}" ]]; then
  FLEET_SECRETS="${FLEET_SECRETS:-${HOME}/.local/bin/fleet-secrets}"
  if secret="$("$FLEET_SECRETS" get SENATE_RELAY_SECRET --path /senate-relay)" && [[ -n "$secret" ]]; then
    export SENATE_RELAY_SECRET="$secret"
  else
    echo "senate-relay: SENATE_RELAY_SECRET missing in Infisical /senate-relay" >&2
  fi
fi
PORT="${1:-8899}"
exec /Users/jay/.deno/bin/deno run --allow-net --allow-env senate-relay.ts "$PORT"
