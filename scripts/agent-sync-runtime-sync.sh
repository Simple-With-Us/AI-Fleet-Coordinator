#!/bin/bash
# agent-sync-runtime-sync.sh  --  keep the agent-sync runtime checkout on AFC origin/main.
#
# The always-on Zulip listener (com.jay.agent-sync-listener) and the `agent-sync` CLI run from a
# dedicated managed checkout, never from the human integration tree ~/Code/AI-Fleet-Coordinator:
#
#   /Users/jay/apps/lanes/_managed/fleet/agent-sync-runtime     (detached worktree, locked)
#
# LaunchAgent com.jay.agent-sync-runtime-sync runs this every 120 seconds.  Each run:
#   1. fetches origin (60 second cap);
#   2. if origin/main differs from the runtime HEAD AND the worktree has no tracked changes, checks out
#      origin/main detached;
#   3. smoke-tests the new code (`agent-sync --version`); on failure it goes back to the old commit and
#      remembers the bad commit so it is not retried until origin/main moves again;
#   4. if the change touched scripts/agent_sync/, scripts/agent-sync, plugins/agent-sync/, .claude-plugin/
#      or docs/protocols/agent-sync-partition.toml, kickstarts the listener so it loads the new code.
# A dirty runtime worktree is logged and skipped.  It is never reset, cleaned or stashed.
#
# Pause:   touch ~/.agent-sync/runtime-sync.pause   (resume: rm it)
#          or  launchctl bootout gui/$(id -u)/com.jay.agent-sync-runtime-sync
# Log:     ~/apps/logs/agent-sync-runtime-sync.log   (no-op runs are silent)
# Tracked copy: scripts/agent-sync-runtime-sync.sh in AI-Fleet-Coordinator.  The live copy is a regular file
# at ~/apps/agent-sync-runtime-sync.sh, never a symlink into the checkout it updates (a checkout would
# rewrite the script while bash is reading it).
set -u

RUNTIME="${AGENT_SYNC_RUNTIME:-/Users/jay/apps/lanes/_managed/fleet/agent-sync-runtime}"
LISTENER_LABEL="${AGENT_SYNC_LISTENER_LABEL:-com.jay.agent-sync-listener}"
STATE_DIR="$HOME/.agent-sync"
LOCK_DIR="$STATE_DIR/runtime-sync.lock"
PAUSE_FILE="$STATE_DIR/runtime-sync.pause"
BAD_FILE="$STATE_DIR/runtime-sync.bad-sha"
LOG="${AGENT_SYNC_RUNTIME_SYNC_LOG:-$HOME/apps/logs/agent-sync-runtime-sync.log}"
GIT_TIMEOUT=60
RESTART_RE='^(scripts/agent_sync/|scripts/agent-sync$|plugins/agent-sync/|\.claude-plugin/|docs/protocols/agent-sync-partition\.toml$)'
PLUGIN_RE='^(plugins/agent-sync/|\.claude-plugin/)'

export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

log() { printf '%s  %s\n' "$(date '+%Y-%m-%d %I:%M:%S%p')" "$*" >> "$LOG"; }

# git with a hard time cap:  perl's alarm survives exec and kills a hung git.
rgit() { perl -e 'alarm shift; exec @ARGV' "$GIT_TIMEOUT" git -C "$RUNTIME" "$@"; }

main() {
  mkdir -p "$(dirname "$LOG")" "$STATE_DIR"
  [ -e "$PAUSE_FILE" ] && exit 0

  # One run at a time.  A lock older than 10 minutes belongs to a dead run.
  if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    if [ -n "$(find "$LOCK_DIR" -maxdepth 0 -mmin +10 2>/dev/null)" ]; then
      log "STALE-LOCK  removing a lock older than 10 minutes"
      rmdir "$LOCK_DIR" 2>/dev/null || rm -rf "$LOCK_DIR"
      mkdir "$LOCK_DIR" 2>/dev/null || exit 0
    else
      exit 0
    fi
  fi
  trap 'rmdir "$LOCK_DIR" 2>/dev/null' EXIT

  if ! git -C "$RUNTIME" rev-parse --git-dir >/dev/null 2>&1; then
    log "ERROR  $RUNTIME is not a git worktree"
    exit 1
  fi

  if ! rgit fetch -q origin; then
    log "FETCH-FAILED  git fetch origin did not finish; will retry next run"
    exit 0
  fi

  local old new
  old="$(rgit rev-parse HEAD 2>/dev/null)" || { log "ERROR  cannot read HEAD"; exit 1; }
  new="$(rgit rev-parse origin/main 2>/dev/null)" || { log "ERROR  cannot read origin/main"; exit 1; }
  [ "$old" = "$new" ] && exit 0

  if [ "$new" = "$(cat "$BAD_FILE" 2>/dev/null)" ]; then
    exit 0   # already tried, failed the smoke test and rolled back; wait for origin/main to move
  fi

  # Tracked changes only:  __pycache__ and other untracked files are expected here.
  if [ -n "$(rgit status --porcelain --untracked-files=no 2>/dev/null)" ]; then
    log "SKIP-DIRTY  runtime worktree has tracked changes; not moving ${old:0:7} -> ${new:0:7} (left untouched)"
    exit 0
  fi

  local changed
  changed="$(rgit diff --name-only "$old" "$new" 2>/dev/null)"

  if ! rgit checkout -q --detach origin/main; then
    log "CHECKOUT-FAILED  ${old:0:7} -> ${new:0:7}; left as it was"
    exit 1
  fi

  # Smoke test the new code before any restart can take the listener down with it.
  if ! perl -e 'alarm 60; exec @ARGV' /opt/homebrew/bin/python3 "$RUNTIME/scripts/agent-sync" --version >/dev/null 2>&1; then
    log "SMOKE-FAILED  agent-sync --version failed at ${new:0:7}; rolled back to ${old:0:7} and will not retry it"
    rgit checkout -q --detach "$old" || log "ROLLBACK-FAILED  could not return to ${old:0:7}"
    printf '%s\n' "$new" > "$BAD_FILE"
    exit 1
  fi
  rm -f "$BAD_FILE"

  log "UPDATED  ${old:0:7} -> ${new:0:7}  ($(printf '%s\n' "$changed" | grep -c .) files)"

  if printf '%s\n' "$changed" | grep -Eq "$PLUGIN_RE"; then
    log "PLUGIN-CHANGED  the Claude plugin source moved; refresh with: claude plugin marketplace update afc"
  fi

  if printf '%s\n' "$changed" | grep -Eq "$RESTART_RE"; then
    if launchctl kickstart -k "gui/$(id -u)/$LISTENER_LABEL" >/dev/null 2>&1; then
      log "RESTARTED  $LISTENER_LABEL (listener code changed)"
    else
      log "RESTART-FAILED  launchctl kickstart -k $LISTENER_LABEL failed (is it loaded?)"
    fi
  fi
}

main "$@"
exit $?
