#!/usr/bin/env bash
# Thin wrapper.  Canonical implementation is sync-fleet-agent-config-to-gdrive.py.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
exec /usr/bin/python3 "$HERE/sync-fleet-agent-config-to-gdrive.py" "$@"
