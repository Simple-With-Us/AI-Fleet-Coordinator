#!/bin/bash
# seat-mcp v1.  Streamable HTTP on 127.0.0.1:8793 only.
# Do not mint a tunnel.  Do not npx dsh.  The token lives in Infisical Fleet-OPS /seat-mcp (SEAT_MCP_TOKEN),
# read through `fleet-secrets` (AFC-359);  never a file in the old secrets folder, never agents.json.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
JOBS="${HOME}/.seat-mcp/jobs"
FLEET_SECRETS="${FLEET_SECRETS:-${HOME}/.local/bin/fleet-secrets}"

mkdir -p "${JOBS}"
chmod 700 "${HOME}/.seat-mcp" "${JOBS}" 2>/dev/null || true

if [[ -z "${SEAT_MCP_TOKEN:-}" ]]; then
  SEAT_MCP_TOKEN="$("$FLEET_SECRETS" get SEAT_MCP_TOKEN --path /seat-mcp)" \
    || { echo "missing SEAT_MCP_TOKEN in Infisical /seat-mcp" >&2; exit 1; }
fi
if [[ -z "${SEAT_MCP_TOKEN}" ]]; then
  echo "SEAT_MCP_TOKEN empty in Infisical /seat-mcp" >&2
  exit 1
fi
export SEAT_MCP_TOKEN

export PYTHONUNBUFFERED=1
cd "$ROOT"
exec /opt/homebrew/bin/python3 -u -m seat_mcp
