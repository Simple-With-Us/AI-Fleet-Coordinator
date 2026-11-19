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
#
# Two rules about the token itself:
#
#   1. The bearer token never reaches an argument list.  The Authorization header goes to
#      curl on standard input with curl --config -, so no process table shows it.
#   2. The API base is pinned.  An inherited INFISICAL_API_BASE must never be able to send
#      this consumer's scoped token to another host.
#
# The value comes back in _INFISICAL_SECRET_VALUE and the reason in
# _INFISICAL_SECRET_STATUS:  "ok", "no-token", "keychain-locked", "token-unreadable",
# "project-id-unavailable", "read-refused", "absent", "read-failed" or "unproven".
INFISICAL_API_PINNED="https://app.infisical.com"

_infisical_secret() {
  local name="$1" folder="$2" account="$3"
  local token="" project="" body="" http_code="" store_status=0 curl_status=0
  _INFISICAL_SECRET_VALUE=""
  _INFISICAL_SECRET_STATUS="no-token"

  token="$(/usr/bin/security find-generic-password -s infisical-service-token -a "$account" -w 2>/dev/null)" || store_status=$?
  if [[ -z "$token" ]]; then
    case "$store_status" in
      44) _INFISICAL_SECRET_STATUS="no-token" ;;
      36) _INFISICAL_SECRET_STATUS="keychain-locked" ;;
      *) _INFISICAL_SECRET_STATUS="token-unreadable" ;;
    esac
    return 0
  fi

  project="${INFISICAL_FLEET_OPS_PROJECT_ID:-}"
  if [[ -z "$project" && -r "${HOME}/.simple-tools/fleet-ops-project-id" ]]; then
    project="$(/usr/bin/head -n 1 "${HOME}/.simple-tools/fleet-ops-project-id" 2>/dev/null | /usr/bin/tr -d '[:space:]')"
  fi
  if [[ -z "$project" ]]; then
    _INFISICAL_SECRET_STATUS="project-id-unavailable"
    return 0
  fi

  # The token goes into a curl config file, so it has to be one line of plain characters:
  # a quote, a backslash or a space would mangle the header, so the read refuses instead.
  if [[ ! "$token" =~ ^[A-Za-z0-9._~+/=-]+$ ]]; then
    _INFISICAL_SECRET_STATUS="token-unreadable"
    return 0
  fi

  body="$(/usr/bin/curl -sS --max-time 30 --config - \
    --write-out '%{http_code}' \
    "${INFISICAL_API_PINNED}/api/v3/secrets/raw/${name}?workspaceId=${project}&environment=prod&secretPath=${folder}&viewSecretValue=true&expandSecretReferences=false&recursive=false&include_imports=false" \
    2>/dev/null <<INFISICAL_HEADER
header = "Authorization: Bearer ${token}"
INFISICAL_HEADER
)" || curl_status=$?
  if [[ "$curl_status" -ne 0 || "${#body}" -lt 3 ]]; then
    _INFISICAL_SECRET_STATUS="read-failed"
    return 0
  fi
  http_code="${body: -3}"
  body="${body:0:${#body}-3}"
  case "$http_code" in
    200) ;;
    403) _INFISICAL_SECRET_STATUS="read-refused"; return 0 ;;
    404) _INFISICAL_SECRET_STATUS="absent"; return 0 ;;
    *) _INFISICAL_SECRET_STATUS="read-failed"; return 0 ;;
  esac

  _INFISICAL_SECRET_VALUE="$(printf '%s' "$body" | /usr/bin/python3 -c '
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
' "$name" "$folder" 2>/dev/null)" || _INFISICAL_SECRET_VALUE=""
  if [[ -z "$_INFISICAL_SECRET_VALUE" ]]; then
    _INFISICAL_SECRET_STATUS="unproven"
  else
    _INFISICAL_SECRET_STATUS="ok"
  fi
  return 0
}

cd "$(dirname "$0")"
if [[ "${SENATE_RELAY_REQUIRE:-0}" == "1" && -z "${SENATE_RELAY_SECRET:-}" ]]; then
  # The token registry names scout-runtime-ro for the Fleet-OPS prod /senate-relay read.
  _infisical_secret SENATE_RELAY_SECRET "%2Fsenate-relay" "scout-runtime-ro"
  secret="$_INFISICAL_SECRET_VALUE"
  if [[ -n "$secret" ]]; then
    export SENATE_RELAY_SECRET="$secret"
  else
    # Fail closed, and say which source is unavailable:  the store that holds the token,
    # the status it answered with, and the folder the name lives in.
    echo "senate-relay: SENATE_RELAY_SECRET is not available:  the scout-runtime-ro service-token store or the read was refused (status: ${_INFISICAL_SECRET_STATUS});  expected Infisical Fleet-OPS prod /senate-relay" >&2
    exit 1
  fi
fi
PORT="${1:-8899}"
exec /Users/jay/.deno/bin/deno run --allow-net --allow-env senate-relay.ts "$PORT"
