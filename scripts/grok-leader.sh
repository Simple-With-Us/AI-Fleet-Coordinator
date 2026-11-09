#!/bin/bash
# Shared Grok leader.  Shellular ACP, grok-acp serve, and TUI clients
# attach here so a phone/bot can list and prompt the same local chats.
# Socket: ~/.grok/leader.sock
# Do not bind :2419.  Do not start a second leader.
#
# Exit 75 (EX_TEMPFAIL) when another process already holds the socket so
# pm2 stop_exit_codes can park the job as stopped instead of crash-looping.
# Tracked copy: AI-Fleet-Coordinator/scripts/grok-leader.sh
set -euo pipefail

export GROK_DISABLE_AUTOUPDATER=1

SOCK="${HOME}/.grok/leader.sock"
LSOF="${LSOF:-/usr/sbin/lsof}"
if [ -S "$SOCK" ]; then
  held=0
  if [ -x "$LSOF" ] && "$LSOF" "$SOCK" >/dev/null 2>&1; then
    held=1
  elif command -v lsof >/dev/null 2>&1 && lsof "$SOCK" >/dev/null 2>&1; then
    held=1
  fi
  if [ "$held" -eq 1 ]; then
    echo "leader lock held at ${SOCK}; exiting 75 so pm2 does not restart-storm" >&2
    exit 75
  fi
fi

# Stay as the pm2 parent.  --always-approve is an `agent` flag (before `leader`).
# --no-exit-on-disconnect keeps the backend up when the phone drops.
exec /Users/jay/.grok/bin/grok agent --always-approve leader --no-exit-on-disconnect
