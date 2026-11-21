#!/bin/bash
# seat-mcp v1.  Streamable HTTP on 127.0.0.1:8793 only.
# Keep loopback only;  do not mint a tunnel or npx dsh.
# The token lives in Infisical Fleet-OPS /seat-mcp (SEAT_MCP_TOKEN).  It is read by the canonical isolated CLI;  never a file in the old secrets folder, never agents.json.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
JOBS="${HOME}/.seat-mcp/jobs"

mkdir -p "${JOBS}"
chmod 700 "${HOME}/.seat-mcp" "${JOBS}" 2>/dev/null || true

if [[ -z "${SEAT_MCP_TOKEN:-}" ]]; then
  if SEAT_MCP_TOKEN="$(/usr/bin/python3 -I "${SCRIPT_DIR}/fleet_infisical_cli.py" get SEAT_MCP_TOKEN --project fleet-ops --path /seat-mcp 2>/dev/null)"; then
    :
  else
    reader_status=$?
    case "$reader_status" in
      2) reason="configuration refused" ;;
      3) reason="missing" ;;
      4) reason="fetch or authentication failed" ;;
      5) reason="unresolved reference" ;;
      *) reason="reader failed (exit ${reader_status})" ;;
    esac
    echo "SEAT_MCP_TOKEN ${reason} in Infisical /seat-mcp" >&2
    exit 1
  fi
fi
if [[ -z "${SEAT_MCP_TOKEN}" ]]; then
  echo "SEAT_MCP_TOKEN empty in Infisical /seat-mcp" >&2
  exit 1
fi
export SEAT_MCP_TOKEN

export PYTHONUNBUFFERED=1
cd "$ROOT"
exec /opt/homebrew/bin/python3 -u -m seat_mcp
