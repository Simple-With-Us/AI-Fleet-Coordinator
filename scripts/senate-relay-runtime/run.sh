#!/usr/bin/env bash
# Mac origin for scout.jays.services. Do not hairpin SENATE_RELAY_URL.
# SENATE_RELAY_SECRET (Infisical Fleet-OPS /senate-relay, read through this origin's own scoped
# read-only service token) is fetched only when SENATE_RELAY_REQUIRE=1 so production can start
# sending the header before this origin fail-closes.
set -euo pipefail

# The shared scoped reader is the one implementation of a scoped Infisical read:
# the owner's shared client, the consumer's own service token, and the grant that
# token actually holds.  It pins the API base,  checks the grant against the
# folder asked for,  and checks the answer's own provenance,  so no token ever
# reaches an argument list and no inherited API base can redirect it.
INFISICAL_SCOPED_READER="${INFISICAL_SCOPED_READER:-/Users/jay/apps/infisical-scoped/infisical-scoped.py}"

# One value on standard output.  The reader's own reason goes to standard error
# and its exit code says whether the name was absent (3), the grant refused the
# read (4) or the usage was wrong (2).
_scoped_secret() {
  local name="$1" folder="$2" account="$3"
  [[ -x "$INFISICAL_SCOPED_READER" ]] || {
    echo "the shared scoped reader is not installed at ${INFISICAL_SCOPED_READER}" >&2
    return 4
  }
  /usr/bin/python3 "$INFISICAL_SCOPED_READER" get "$name" \
    --folder "$folder" --project fleet-ops --account "$account"
}

cd "$(dirname "$0")"
if [[ "${SENATE_RELAY_REQUIRE:-0}" == "1" && -z "${SENATE_RELAY_SECRET:-}" ]]; then
  # The token registry names scout-runtime-ro for the Fleet-OPS prod /senate-relay read.
  secret="" status=4
  secret="$(_scoped_secret SENATE_RELAY_SECRET "/senate-relay" "scout-runtime-ro")" || status=$?
  if [[ -n "$secret" ]]; then
    export SENATE_RELAY_SECRET="$secret"
  else
    # Fail closed, and say which source is unavailable:  the store that holds the token,
    # the status it answered with, and the folder the name lives in.
    echo "senate-relay: SENATE_RELAY_SECRET is not available:  the scout-runtime-ro service-token store or the read was refused (status: ${status});  expected Infisical Fleet-OPS prod /senate-relay" >&2
    exit 1
  fi
fi
PORT="${1:-8899}"
exec /Users/jay/.deno/bin/deno run --allow-net --allow-env senate-relay.ts "$PORT"
