#!/bin/bash
# agy-acp over stdio-to-ws, loopback only.
# Packaged stdio-to-ws has no --host.  bind-loopback.cjs (node -r) forces
# AGY_ACP_BIND (default 127.0.0.1) so npm i cannot restore :::8765.
# Child is agy-acp-turbo.sh (same turbo policy as Shellular).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
export AGY_ACP_BIND="${AGY_ACP_BIND:-127.0.0.1}"
case "$AGY_ACP_BIND" in
  127.0.0.1|localhost|::1) ;;
  *)
    echo "agy-acp: AGY_ACP_BIND must be loopback, got: $AGY_ACP_BIND" >&2
    exit 1
    ;;
esac
BIND_SHIM="$ROOT/bind-loopback.cjs"
TURBO="$ROOT/agy-acp-turbo.sh"
if [[ ! -f "$BIND_SHIM" || ! -x "$TURBO" ]]; then
  echo "agy-acp: missing bind-loopback.cjs or agy-acp-turbo.sh" >&2
  exit 1
fi
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
exec /opt/homebrew/bin/node -r "$BIND_SHIM" \
  "$ROOT/node_modules/.bin/stdio-to-ws" \
  --persist --grace-period 300 --port "${AGY_ACP_PORT:-8765}" \
  "$TURBO"
