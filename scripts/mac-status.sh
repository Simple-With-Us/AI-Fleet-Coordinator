#!/bin/bash
# One-screen status for this Mac's always-on jobs.
# Usage: bash ~/apps/mac-status.sh

set -u

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
uid="$(id -u)"

bold "=== pm2 (always-on fleet jobs) ==="
if command -v pm2 >/dev/null 2>&1; then
  pm2 status
else
  echo "pm2 not on PATH"
fi
if [ -S "${HOME}/.grok/leader.sock" ] && /usr/sbin/lsof "${HOME}/.grok/leader.sock" >/dev/null 2>&1; then
  echo
  echo "note: ~/.grok/leader.sock is held by a live grok process (usually this TUI)."
  echo "      pm2 grok-leader should stay stopped.  Do not pm2 restart it."
fi

echo
bold "=== launchd (not in pm2) ==="
printf '%-8s %-8s %s\n' "PID" "OK" "LABEL"
launchctl list | awk '
  NR == 1 { next }
  $3 ~ /^(com\.jay\.|com\.jays\.|com\.congress\.|actions\.runner\.|homebrew\.|com\.cursor\.|com\.omnara\.|com\.ccpocket\.|pm2\.|com\.cloudflare\.|com\.PM2$)/ {
    pid = ($1 == "-" ? "idle" : $1)
    ok  = ($2 == "0" || $2 == "-" ? "ok" : "exit-" $2)
    note = ($3 == "com.jay.ios-ship-now" && $2 != "0" && $2 != "-" ? "  (login one-shot leftover; do not kickstart)" : "")
    printf "%-8s %-8s %s%s\n", pid, ok, $3, note
  }
'

echo
bold "=== brew services ==="
if command -v brew >/dev/null 2>&1; then
  brew services list
else
  echo "brew not on PATH"
fi

echo
bold "=== listeners ==="
lsof -nP -iTCP:8765,8791,8792,8899,8787,24543,2419,12419 -sTCP:LISTEN 2>/dev/null | awk '
  NR == 1 { next }
  { printf "%-12s pid=%-6s %s\n", $1, $2, $9 }
' | sort -u

echo
bold "=== down-watch (last 8) ==="
if [ -f "${HOME}/Library/Logs/mac-process-watch.log" ]; then
  tail -n 8 "${HOME}/Library/Logs/mac-process-watch.log"
else
  echo "(no watch log yet)"
fi

echo
echo "Command:  bash ~/apps/mac-status.sh"
echo "pm2 only: pm2 status"
echo "logs:     pm2 logs <name>     or   tail -f ~/.pm2/logs/<name>-error.log"
echo "downs:    tail -f ~/Library/Logs/mac-process-watch.log"
echo "restarts: same log (RESTART/SKIP/FAIL).  Off: MAC_PROCESS_WATCH_RESTART=0"
