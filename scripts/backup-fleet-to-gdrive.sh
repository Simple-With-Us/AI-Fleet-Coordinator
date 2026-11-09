#!/usr/bin/env bash
# Thin wrapper.  Canonical implementation is backup-fleet-to-gdrive.py.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
exec /usr/bin/python3 "$HERE/backup-fleet-to-gdrive.py" "$@"
