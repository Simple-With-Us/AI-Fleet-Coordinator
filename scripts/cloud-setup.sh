#!/usr/bin/env bash
# Canonical setup for a fresh, isolated checkout of AI-Fleet-Coordinator
# (Claude Code cloud/remote sandbox, Codespaces, or any throwaway clone).
# Idempotent — safe to re-run.
#
# Claude Code Cloud runs the Setup script from the PARENT of the clone
# (`/home/user`). A bare `bash scripts/cloud-setup.sh` fails with exit 127.
# Use the fleet locator in docs/CLAUDE-CODE-CLOUD-ENVIRONMENTS.md
# or: cd AI-Fleet-Coordinator && bash scripts/cloud-setup.sh
#
# What it does beyond the registry check: a cloud seat has no `fleet-secrets` (the Mac's credential source),
# so without help it hits the Cloudflare Access login page on recall.jays.services and
# concludes the fleet corpus is unreachable.  When the sandbox injects an Infisical
# machine identity into the ENVIRONMENT, this script provisions the three recall
# credentials into a 0600 env file and registers the remote MCP server — with header
# PLACEHOLDERS, never values.  With no identity it says so and still exits 0: this repo
# is docs/infra, and setup must not fail a sandbox.
#
#   bash scripts/cloud-setup.sh            # set up
#   bash scripts/cloud-setup.sh --check    # report names and booleans only
set -euo pipefail

cd "$(dirname "$0")/.."

HOME_DIR="${HOME:?HOME is not set}"
ENV_FILE="${FLEET_RECALL_ENV_FILE:-$HOME_DIR/.fleet-recall.env}"
CLAUDE_JSON="$HOME_DIR/.claude.json"
FETCH="scripts/fleet-infisical-fetch.py"
SERVER_NAME="fleet-recall"
MCP_URL="https://recall.jays.services/mcp"
KEYS=(RECALL_API_TOKEN CF_ACCESS_CLIENT_ID CF_ACCESS_CLIENT_SECRET)
IDENTITY_PREFIXES=(INFISICAL_SHARED INFISICAL_AUTOMATION)
DOC="docs/RECALL-ACCESS-CHECK.md"

MODE="setup"
if [[ "${1:-}" == "--check" ]]; then MODE="check"; fi

# ---------------------------------------------------------------- helpers

# identity_prefix -> echoes the prefix whose *_CLIENT_ID and *_CLIENT_SECRET are both set
# in the environment (names only; a value never reaches stdout).  Empty when there is none.
identity_prefix() {
  local p id sec
  for p in "${IDENTITY_PREFIXES[@]}"; do
    id="${p}_CLIENT_ID"; sec="${p}_CLIENT_SECRET"
    if [[ -n "${!id:-}" && -n "${!sec:-}" ]]; then printf '%s' "$p"; return 0; fi
  done
  return 0
}

# env_file_has <KEY> -> 0 when the env file assigns that key a non-empty value.
env_file_has() {
  [[ -f "$ENV_FILE" ]] && grep -Eq "^$1=.+$" "$ENV_FILE"
}

# resolvable <KEY> -> 0 when the key is in the environment or in the env file.
resolvable() {
  [[ -n "${!1:-}" ]] || env_file_has "$1"
}

mcp_registered() {
  python3 - "$CLAUDE_JSON" "$SERVER_NAME" "$MCP_URL" <<'PY'
import json, sys
path, name, url = sys.argv[1:4]
try:
    with open(path, encoding="utf-8") as fh:
        raw = fh.read()
    data = json.loads(raw) if raw.strip() else {}
except (OSError, json.JSONDecodeError):
    sys.exit(1)
entry = (data.get("mcpServers") or {}).get(name) if isinstance(data, dict) else None
sys.exit(0 if isinstance(entry, dict) and entry.get("url") == url else 1)
PY
}

# ---------------------------------------------------------------- --check

if [[ "$MODE" == "check" ]]; then
  echo "==> fleet recall access check (names and booleans only)"
  prefix="$(identity_prefix)"
  if [[ -n "$prefix" ]]; then
    echo "    infisical identity in environment: true ($prefix)"
  else
    echo "    infisical identity in environment: false"
  fi
  if [[ -f "$ENV_FILE" ]]; then
    mode="$(python3 -c 'import os,sys;print(oct(os.stat(sys.argv[1]).st_mode & 0o777)[2:])' "$ENV_FILE")"
    echo "    env file $ENV_FILE: present=true mode=$mode"
  else
    echo "    env file $ENV_FILE: present=false"
  fi
  for k in "${KEYS[@]}"; do
    if resolvable "$k"; then echo "    $k: resolvable=true"; else echo "    $k: resolvable=false"; fi
  done
  if mcp_registered; then
    echo "    mcp $SERVER_NAME in $CLAUDE_JSON: registered=true"
  else
    echo "    mcp $SERVER_NAME in $CLAUDE_JSON: registered=false"
  fi
  exit 0
fi

# ---------------------------------------------------------------- setup

echo "==> Python: $(python3 --version 2>/dev/null || echo 'not found')"
echo "==> Checking fleet registry"
python3 scripts/check-fleet-registry.py || echo "==> registry check skipped (ok if extra local-only files)"

echo "==> Fleet recall credentials"
PREFIX="$(identity_prefix)"
HAVE_CREDS=0
if [[ -z "$PREFIX" ]]; then
  echo "    no Infisical machine identity in the environment"
  echo "    (looked for ${IDENTITY_PREFIXES[0]}_CLIENT_ID/_CLIENT_SECRET and ${IDENTITY_PREFIXES[1]}_CLIENT_ID/_CLIENT_SECRET)"
elif env_file_has "${KEYS[0]}" && env_file_has "${KEYS[1]}" && env_file_has "${KEYS[2]}"; then
  echo "    identity: true ($PREFIX)"
  echo "    $ENV_FILE: unchanged (${KEYS[*]})"
  HAVE_CREDS=1
else
  umask 077
  if python3 "$FETCH" --env-file "$ENV_FILE" "${KEYS[@]}"; then
    echo "    $ENV_FILE: written 0600 (names only: ${KEYS[*]})"
    HAVE_CREDS=1
  else
    echo "    could not provision credentials from Infisical — see $DOC"
  fi
fi

echo "==> Remote MCP server '$SERVER_NAME'"
python3 - "$CLAUDE_JSON" "$SERVER_NAME" "$MCP_URL" "$(date +%Y%m%d-%H%M%S)" <<'PY'
"""Register the remote fleet-recall MCP server in ~/.claude.json.

Placeholders only — the three header values stay in the env file, never in this config.
Preserves every existing key; backs the file up, then temp-file + rename.
"""
import json, os, shutil, sys, tempfile

path, name, url, ts = sys.argv[1:5]
entry = {
    "url": url,
    "headers": {
        "Authorization": "Bearer ${RECALL_API_TOKEN}",
        "CF-Access-Client-Id": "${CF_ACCESS_CLIENT_ID}",
        "CF-Access-Client-Secret": "${CF_ACCESS_CLIENT_SECRET}",
    },
}

existed = os.path.exists(path)
if existed:
    with open(path, encoding="utf-8") as fh:
        raw = fh.read()
    try:
        data = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        print("    skipped-invalid-json: " + path); sys.exit(0)
else:
    data = {}
if not isinstance(data, dict):
    print("    skipped-not-object: " + path); sys.exit(0)

servers = data.get("mcpServers")
if servers is None:
    servers = {}
if not isinstance(servers, dict):
    print("    skipped-bad-mcpServers: " + path); sys.exit(0)

current = servers.get(name)
if current == entry:
    print("    unchanged: " + path); sys.exit(0)
if current is not None:
    # Someone registered a different fleet-recall by hand; leave it alone.
    print("    skipped-foreign: " + path); sys.exit(0)

servers[name] = entry
data["mcpServers"] = servers
os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
if existed:
    shutil.copy2(path, f"{path}.bak-fleet-recall-{ts}")
fd, tmp = tempfile.mkstemp(prefix=".fleet-recall.", dir=os.path.dirname(path) or ".")
with os.fdopen(fd, "w", encoding="utf-8") as fh:
    json.dump(data, fh, indent=2, ensure_ascii=False)
    fh.write("\n")
if existed:
    try:
        os.chmod(tmp, os.stat(path).st_mode & 0o777)
    except OSError:
        pass
else:
    os.chmod(tmp, 0o600)
os.replace(tmp, path)
print("    added: " + path + " (headers are ${...} placeholders, no values)")
PY

echo "==> Setup complete."
echo "    This repo is fleet infra/docs, not a long-running app."
echo "    Verify: python3 scripts/check-fleet-registry.py"
echo "    Recheck: bash scripts/cloud-setup.sh --check"
echo
if [[ "$HAVE_CREDS" == "1" ]]; then
  echo "    Load the credentials before starting your agent:"
  echo "        set -a; . $ENV_FILE; set +a"
  echo "    Self-test (the service, then an authenticated search):"
  echo "        curl -sS https://recall.jays.services/health"
  echo "        curl -sS https://recall.jays.services/recall/search \\"
  echo '          -H "Authorization: Bearer $RECALL_API_TOKEN" \'
  echo '          -H "CF-Access-Client-Id: $CF_ACCESS_CLIENT_ID" \'
  echo '          -H "CF-Access-Client-Secret: $CF_ACCESS_CLIENT_SECRET" \'
  echo "          -H 'Content-Type: application/json' \\"
  echo "          -d '{\"query\":\"how do we rotate the Coolify token\",\"limit\":2}'"
  echo "    A 302 to a Cloudflare login means the Access headers did not reach the edge;"
  echo "    a 401 means Access passed and the bearer is wrong.  Details in $DOC."
else
  echo "    No recall credentials were provisioned, so the fleet corpus is NOT reachable"
  echo "    from this sandbox yet.  A 302 to a Cloudflare login page is the symptom."
  echo "    Fix: inject an Infisical machine identity into this sandbox's environment as"
  echo "    ${IDENTITY_PREFIXES[0]}_CLIENT_ID / ${IDENTITY_PREFIXES[0]}_CLIENT_SECRET and re-run this"
  echo "    script — through a private path, NOT the cloud environment-variables dialog,"
  echo "    which anyone who can open it can read.  Manual fallback: $DOC."
fi
