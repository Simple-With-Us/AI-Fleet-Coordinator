#!/bin/bash
# Optional My Machines worker so Cloud Agents can run tools on this Mac.
# Not part of the 14 always-on pm2 jobs.  Start only when you want
# CURSOR_BRIDGE_ON_MAC=1 Shellular chats to execute locally.
#
#   bash ~/apps/cursor-chat-surfaces/cursor-machine-worker.sh
set -euo pipefail
AGENT="${HOME}/.local/bin/agent"
if [[ ! -x "$AGENT" ]]; then
  echo "missing $AGENT" >&2
  exit 1
fi
NAME="${CURSOR_BRIDGE_MACHINE:-jay-mac}"
DIR="${CURSOR_WORKER_DIR:-$HOME/Code}"
exec "$AGENT" worker start --name "$NAME" --worker-dir "$DIR" --idle-release-timeout 0
