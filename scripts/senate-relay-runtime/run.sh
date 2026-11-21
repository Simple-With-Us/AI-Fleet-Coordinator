#!/usr/bin/env bash
# Mac origin for scout.jays.services. Do not hairpin SENATE_RELAY_URL.
# SENATE_RELAY_SECRET (Infisical Fleet-OPS /senate-relay) is fetched only when SENATE_RELAY_REQUIRE=1.
# Production can start sending the header before this origin fail-closes.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
if [[ "${SENATE_RELAY_REQUIRE:-0}" == "1" && -z "${SENATE_RELAY_SECRET:-}" ]]; then
  if secret="$(/usr/bin/python3 -I "${SCRIPT_DIR}/../fleet_infisical_cli.py" get SENATE_RELAY_SECRET --project fleet-ops --path /senate-relay 2>/dev/null)" && [[ -n "$secret" ]]; then
    export SENATE_RELAY_SECRET="$secret"
  else
    # Fail closed, and say which source is unavailable:  the reader that holds the token
    # and the folder the name lives in.  No value and no provider detail.
    echo "senate-relay: SENATE_RELAY_SECRET is not available:  the shared Infisical reader could not return it from Infisical Fleet-OPS prod /senate-relay" >&2
    exit 1
  fi
fi
PORT="${1:-8899}"
exec /Users/jay/.deno/bin/deno run --allow-net --allow-env senate-relay.ts "$PORT"
