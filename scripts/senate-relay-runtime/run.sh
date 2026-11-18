#!/usr/bin/env bash
# Mac origin for scout.jays.services. Do not hairpin SENATE_RELAY_URL.
# SENATE_RELAY_SECRET (Infisical Fleet-OPS /senate-relay, read through this origin's own scoped
# read-only service token) is fetched only when SENATE_RELAY_REQUIRE=1 so production can start
# sending the header before this origin fail-closes.
set -euo pipefail

# One secret from Infisical, through this consumer's own scoped read-only service token.
#
# The token lives in the owner's login keychain as one generic-password item per token:  the
# service is infisical-service-token and the account is the token's registry name.  The read
# is the raw-secret route, and the answer's own provenance (the name, the folder, the
# environment and the shared type) is checked before the value is used.  There is no shared
# reader, no machine identity and no cached copy:  a missing token, a locked keychain or a
# refused grant yields nothing.  The value is never logged.
_infisical_secret() {
  local name="$1" folder="$2" account="$3" token project value=""
  token="$(/usr/bin/security find-generic-password -s infisical-service-token -a "$account" -w 2>/dev/null || true)"
  project="${INFISICAL_FLEET_OPS_PROJECT_ID:-}"
  if [[ -z "$project" && -r "${HOME}/.simple-tools/fleet-ops-project-id" ]]; then
    project="$(/usr/bin/head -n 1 "${HOME}/.simple-tools/fleet-ops-project-id" 2>/dev/null | /usr/bin/tr -d '[:space:]')"
  fi
  if [[ -z "$token" || -z "$project" ]]; then
    return 0
  fi
  value="$(/usr/bin/curl -sS --max-time 30 \
    -H "Authorization: Bearer ${token}" \
    "${INFISICAL_API_BASE:-https://app.infisical.com}/api/v3/secrets/raw/${name}?workspaceId=${project}&environment=prod&secretPath=${folder}&viewSecretValue=true&expandSecretReferences=false&recursive=false&include_imports=false" \
    2>/dev/null | /usr/bin/python3 -c '
import json, sys
try:
    answer = json.load(sys.stdin)
except Exception:
    raise SystemExit(1)
secret = answer.get("secret") if isinstance(answer, dict) else None
if not isinstance(secret, dict):
    raise SystemExit(1)
if secret.get("secretKey") != sys.argv[1]:
    raise SystemExit(1)
if secret.get("environment") != "prod" or secret.get("type") != "shared":
    raise SystemExit(1)
if secret.get("secretPath") != sys.argv[2].replace("%2F", "/"):
    raise SystemExit(1)
value = secret.get("secretValue")
if not isinstance(value, str) or not value:
    raise SystemExit(1)
sys.stdout.write(value)
' "$name" "$folder" 2>/dev/null)" || value=""
  printf '%s' "$value"
}

cd "$(dirname "$0")"
if [[ "${SENATE_RELAY_REQUIRE:-0}" == "1" && -z "${SENATE_RELAY_SECRET:-}" ]]; then
  secret="$(_infisical_secret SENATE_RELAY_SECRET "%2Fsenate-relay" "senate-relay-ro")"
  if [[ -n "$secret" ]]; then
    export SENATE_RELAY_SECRET="$secret"
  else
    echo "senate-relay: SENATE_RELAY_SECRET missing in Infisical /senate-relay" >&2
  fi
fi
PORT="${1:-8899}"
exec /Users/jay/.deno/bin/deno run --allow-net --allow-env senate-relay.ts "$PORT"
