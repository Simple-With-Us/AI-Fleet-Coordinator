#!/bin/bash
# Compatibility entrypoint for the existing keeper schedule and log.
# Synchronization behavior lives only in fleet_lanes/code_main_sync.py.
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
SYNC="$SCRIPT_DIR/fleet_lanes/code_main_sync.py"
if [ ! -f "$SYNC" ]; then
  SYNC="$HOME/.local/share/fleet-cli/repo/scripts/fleet_lanes/code_main_sync.py"
fi
if [ ! -f "$SYNC" ]; then
  echo "code-main-keeper: shared code_main_sync.py is missing" >&2
  exit 1
fi

# Hosts supply their existing authorized relay configuration through the environment.
export FLEET_CODE_ROOT="${FLEET_CODE_ROOT:-${CODE_ROOT:-$HOME/Code}}"
LOG_DIR="${CODE_MAIN_KEEPER_LOG_DIR:-$HOME/apps/logs}"
mkdir -p "$LOG_DIR"
if [ "${1:-}" = "-n" ]; then
  shift
  set -- --dry-run "$@"
fi
exec python3 -I "$SYNC" "$@" >> "$LOG_DIR/code-main-keeper.log" 2>&1
