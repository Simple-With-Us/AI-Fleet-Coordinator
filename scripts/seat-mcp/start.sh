#!/bin/bash
# seat-mcp v1.  Streamable HTTP on 127.0.0.1:8793 only.
# Do not mint a tunnel.  Do not npx dsh.  The token lives in Infisical Fleet-OPS /seat-mcp (SEAT_MCP_TOKEN),
# read through the scoped reader with this consumer's own `seat-mcp-ro` service token (AFC-359);
# never a file in the old secrets folder, never agents.json.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
JOBS="${HOME}/.seat-mcp/jobs"
SCOPED_READER="${INFISICAL_SCOPED_READER:-${HOME}/apps/infisical-scoped/infisical-scoped.py}"

mkdir -p "${JOBS}"
chmod 700 "${HOME}/.seat-mcp" "${JOBS}" 2>/dev/null || true

# The scoped read runs under the system interpreter,  the same one the other readers
# use,  and it is checked first:  an interpreter that is not there is an interpreter
# failure,  not a missing secret.
PYTHON="/usr/bin/python3"
if [[ ! -x "$PYTHON" ]]; then
  echo "seat-mcp: ${PYTHON} is not executable on this host,  so the scoped reader cannot run" >&2
  exit 1
fi

if [[ -z "${SEAT_MCP_TOKEN:-}" ]]; then
  if ! SEAT_MCP_TOKEN="$("$PYTHON" "${SCOPED_READER}" get SEAT_MCP_TOKEN \
    --folder /seat-mcp --project fleet-ops --account seat-mcp-ro)"; then
    echo "missing SEAT_MCP_TOKEN in Infisical /seat-mcp:  the scoped reader at ${SCOPED_READER} could not read it with the seat-mcp-ro scoped service token" >&2
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
