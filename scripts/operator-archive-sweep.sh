#!/bin/sh
# operator-archive-sweep.sh:  the scheduled run of `simple-chat letter sweep-archived`.
#
# Archiving a Claude chat in the desktop app fires no hook that frees its Operator letter, so the
# archive sweep (claude_letter.sweep_archived, AFC-694) closes the open registration of every chat
# the app has archived.  This wrapper is what com.jay.operator-archive-sweep runs each hour.  Every
# run does a DRY RUN first and logs what it would close;  the apply pass runs only when
# OPERATOR_ARCHIVE_SWEEP_APPLY=1 (the installer's --apply sets it), and only when the dry run
# named no more than OPERATOR_ARCHIVE_SWEEP_MAX chats (default 25), because a count that large
# means the app's archive flag is being read wrongly, not that 25 chats were archived in an hour.
#
# Output:  one log line per chat (letter, first 8 characters of the session id, host;  never a title).
# Exit:    0 when the sweep ran clean (or has nothing to sweep);  otherwise the sweep's own code
#          (3 no registry, 5 unreadable registrations, 1 a close failed), or 4 for the cap.
# Environment (all optional):  SIMPLE_CHAT_BIN, OPERATOR_ARCHIVE_SWEEP_LOG.
set -u

BIN="${SIMPLE_CHAT_BIN:-$HOME/.local/bin/simple-chat}"
LOG="${OPERATOR_ARCHIVE_SWEEP_LOG:-$HOME/.local/state/operator/archive-sweep.log}"
APPLY="${OPERATOR_ARCHIVE_SWEEP_APPLY:-0}"
MAX="${OPERATOR_ARCHIVE_SWEEP_MAX:-25}"

umask 077
mkdir -p "$(dirname "$LOG")" 2>/dev/null
stamp() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }
log() { printf '%s  %s\n' "$(stamp)" "$1" >>"$LOG"; }

# A chat-agent seat must not leak into the sweep (it is the registry's OPERATOR principal that
# closes, through the shared registry env the CLI sources itself).
unset AGENT_SEAT AGENT_LAUNCH_SEAT AGENT_LAUNCHER

case "$MAX" in '' | *[!0-9]*) MAX=25 ;; esac

log "dry run"
dry=$("$BIN" letter sweep-archived 2>&1)
code=$?
printf '%s\n' "$dry" | while IFS= read -r line; do log "  $line"; done
if [ "$code" -ne 0 ]; then
  log "dry run failed with exit $code"
  exit "$code"
fi

if [ "$APPLY" != "1" ]; then
  log "apply off (OPERATOR_ARCHIVE_SWEEP_APPLY is not 1):  dry run only"
  exit 0
fi

would=$(printf '%s\n' "$dry" | grep -c '^would close ')
if [ "$would" -gt "$MAX" ]; then
  log "refusing to apply:  $would chats would close, more than the cap of $MAX"
  exit 4
fi
if [ "$would" -eq 0 ]; then
  log "nothing archived:  no apply needed"
  exit 0
fi

log "apply"
out=$("$BIN" letter sweep-archived --apply 2>&1)
code=$?
printf '%s\n' "$out" | while IFS= read -r line; do log "  $line"; done
log "apply finished with exit $code"
exit "$code"
