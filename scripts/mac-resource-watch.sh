#!/bin/bash
# Wrapper for launchd / agents.  Body is mac-resource-watch.py.
set -u
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${HOME}/.local/bin:${PATH:-}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PY="${SCRIPT_DIR}/mac-resource-watch.py"
if [ ! -f "$PY" ]; then
  PY="${HOME}/apps/mac-resource-watch.py"
fi
exec /usr/bin/python3 "$PY" "$@"
