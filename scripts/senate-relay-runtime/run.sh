#!/usr/bin/env bash
# Mac origin for scout.jays.services. Do not hairpin SENATE_RELAY_URL.
# SENATE_RELAY_SECRET (Infisical Fleet-OPS /senate-relay) is fetched only when SENATE_RELAY_REQUIRE=1.
# Production can start sending the header before this origin fail-closes.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
# The shared scoped reader is the one command every consumer outside this repository
# runs,  and the account is named here:  the read runs under this consumer's own
# grant and never under an ambient environment token.
SCOPED_READER="${INFISICAL_SCOPED_READER:-${HOME}/apps/infisical-scoped/infisical-scoped.py}"
TOKEN_ACCOUNT="scout-runtime-ro"
PYTHON="/usr/bin/python3"
if [[ ! -x "$PYTHON" ]]; then
  echo "senate-relay:  ${PYTHON} is not executable on this host,  so the scoped reader cannot run" >&2
  exit 1
fi
if [[ "${SENATE_RELAY_REQUIRE:-0}" == "1" && -z "${SENATE_RELAY_SECRET:-}" ]]; then
  if secret="$("$PYTHON" -I "${SCOPED_READER}" get SENATE_RELAY_SECRET \
    --folder /senate-relay --project fleet-ops --account "${TOKEN_ACCOUNT}" 2>/dev/null)" && [[ -n "$secret" ]]; then
    export SENATE_RELAY_SECRET="$secret"
  else
    reader_status=$?
    case "$reader_status" in
      2) reason="the command line was refused" ;;
      3) reason="the name is not in that folder" ;;
      4) reason="the grant refused the read or the token store was unavailable" ;;
      *) reason="reader failed (exit ${reader_status})" ;;
    esac
    # Fail closed, and say which source is unavailable:  the reader that holds the token,
    # the account that covers the folder,  and the folder the name lives in.  No value and
    # no provider detail.
    echo "senate-relay: SENATE_RELAY_SECRET is not available:  the scoped reader at ${SCOPED_READER} could not read it from Infisical Fleet-OPS prod /senate-relay with the ${TOKEN_ACCOUNT} scoped service token (${reason})" >&2
    exit 1
  fi
fi
PORT="${1:-8899}"
exec /Users/jay/.deno/bin/deno run --allow-net --allow-env senate-relay.ts "$PORT"
