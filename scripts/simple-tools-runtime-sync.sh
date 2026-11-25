#!/bin/bash
# simple-tools-runtime-sync.sh  --  keep the simple-tools runtime checkout on AFC origin/main.
#
# Public-source updater approved by owner Nov 18, 2026.
# The `simple-chat` CLI runs from a
# dedicated managed checkout, never from the human integration tree ~/Code/AI-Fleet-Coordinator:
#
#   /Users/jay/apps/lanes/_managed/fleet/simple-tools-runtime     (detached worktree, locked)
#
# LaunchAgent com.jay.simple-tools-runtime-sync runs this every 120 seconds.  Each run:
#   1. retrieves only the fixed approved public source (60 second caps), and only the change
#      since the last run:  the stage is seeded with the objects this host already holds, so a
#      run costs a delta instead of the whole history;  until the operator's approval file
#      exists the run stays on the prior origin transport instead of holding with no fetch;
#   2. if origin/main differs from the runtime HEAD AND the worktree has no tracked changes, checks out
#      origin/main detached;
#   3. smoke-tests the new code (`simple-chat --version`); on failure it goes back to the old commit and
#      remembers the bad commit so it is not retried until origin/main moves again;
#   4. records plugin refresh advice without restarting the retired Mac listener, and runs the
#      unlettered-repair tick and the Mac GitOps drift report that the tracked copy carried.
# A dirty runtime worktree is logged and skipped.  It is never reset, cleaned or stashed.
#
# Pause:   touch ~/.simple-tools/runtime-sync.pause   (resume: rm it)
#          or  launchctl bootout gui/$(id -u)/com.jay.simple-tools-runtime-sync
# Log:     ~/apps/logs/simple-tools-runtime-sync.log   (no-op runs are silent)
# Tracked copy: scripts/simple-tools-runtime-sync.sh in AI-Fleet-Coordinator.  The live copy is a regular file
# at ~/apps/simple-tools-runtime-sync.sh, never a symlink into the checkout it updates (a checkout would
# rewrite the script while bash is reading it).
set -u

RUNTIME="${SIMPLE_TOOLS_RUNTIME:-/Users/jay/apps/lanes/_managed/fleet/simple-tools-runtime}"
STATE_DIR="$HOME/.simple-tools"
LOCK_DIR="$STATE_DIR/runtime-sync.lock"
PAUSE_FILE="$STATE_DIR/runtime-sync.pause"
BAD_FILE="$STATE_DIR/runtime-sync.bad-sha"
LOG="${SIMPLE_TOOLS_RUNTIME_SYNC_LOG:-$HOME/apps/logs/simple-tools-runtime-sync.log}"
GIT_TIMEOUT=60
PLUGIN_RE='^(plugins/simple-chat/|\.claude-plugin/)'

export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

log() { printf '%s  %s\n' "$(date '+%Y-%m-%d %I:%M:%S%p')" "$*" >> "$LOG"; }

# git with a hard time cap:  perl's alarm survives exec and kills a hung git.
rgit() { perl -e 'alarm shift; exec @ARGV' "$GIT_TIMEOUT" git -C "$RUNTIME" "$@"; }

# Retrieve exactly one approved public repository/ref in an isolated bare repo.
# Network Git sees no host Git config, credentials, SSH agent, hooks or proxy.
# Import is local-only; runtime origin URL/config is not rewritten or consulted.
fetch_public_runtime() (
  local stage sha
  stage="$(mktemp -d "${TMPDIR:-/tmp}/runtime-public.XXXXXX")" || exit 1
  trap 'rm -rf "$stage"' EXIT
  mkdir "$stage/home" || exit 1
  clean_git() {
    env -i HOME="$stage/home" PATH=/usr/bin:/bin:/usr/sbin:/sbin \
      GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null \
      GIT_TERMINAL_PROMPT=0 GCM_INTERACTIVE=never \
      perl -e 'alarm shift; exec @ARGV' 60 /usr/bin/git \
      -c credential.helper= -c http.followRedirects=false \
      -c protocol.allow=never -c protocol.https.allow=always \
      -c core.hooksPath=/dev/null "$@"
  }
  clean_git init --bare -q "$stage/repo" || exit 1
  # Seed the stage with the objects this host already holds:  a local copy, with no network and
  # no origin configuration consulted, so the retrieval below carries the change since the last
  # run instead of the whole history.  A fresh host has nothing to seed and fetches the history
  # once, as it did before.
  seed_local_objects() {
    env -i HOME="$stage/home" PATH=/usr/bin:/bin:/usr/sbin:/sbin \
      GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null \
      GIT_TERMINAL_PROMPT=0 GCM_INTERACTIVE=never \
      perl -e 'alarm shift; exec @ARGV' 60 /usr/bin/git \
      -c credential.helper= -c http.followRedirects=false \
      -c protocol.allow=never -c protocol.file.allow=always -c protocol.https.allow=always \
      -c core.hooksPath=/dev/null \
      -C "$stage/repo" fetch --no-tags --no-recurse-submodules -q "$RUNTIME" \
      "$1":refs/heads/approved-main >/dev/null 2>&1
  }
  seed_local_objects +refs/remotes/origin/main || seed_local_objects +HEAD || true
  clean_git -C "$stage/repo" fetch --no-tags --no-recurse-submodules -q \
    https://github.com/Simple-With-Us/AI-Fleet-Coordinator.git \
    +refs/heads/main:refs/heads/approved-main || exit 1
  sha="$(clean_git -C "$stage/repo" rev-parse --verify refs/heads/approved-main^{commit})" || exit 1
  case "$sha" in *[!0-9a-f]*|'') exit 1;; esac
  [ "${#sha}" -eq 40 ] || exit 1
  # This local object import cannot contact any remote; all submodule recursion
  # is disabled. Explicit allow is scoped to the generated temporary file path.
  env -i HOME="$stage/home" PATH=/usr/bin:/bin:/usr/sbin:/sbin \
    GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_TERMINAL_PROMPT=0 \
    perl -e 'alarm shift; exec @ARGV' 60 /usr/bin/git \
    -c core.hooksPath=/dev/null -c protocol.allow=never -c protocol.file.allow=always \
    -C "$RUNTIME" fetch --no-tags --no-recurse-submodules -q "$stage/repo" \
    +refs/heads/approved-main:refs/remotes/origin/main || exit 1
  [ "$(rgit rev-parse refs/remotes/origin/main)" = "$sha" ]
)

main() {
  mkdir -p "$(dirname "$LOG")" "$STATE_DIR"
  [ -e "$PAUSE_FILE" ] && exit 0

  # Explicit operator approval is required before enabling this fixed public-source mode, and
  # the mode is not a fallback when caller-authenticated simple-git refuses.  Until the approval
  # file exists the job stays on the transport it used before, so a host without the file keeps
  # updating instead of standing still behind main with nothing but a log line.  That fallback
  # disables every interactive path git and ssh can take, because a launchd run has no terminal
  # to answer a prompt on and a stalled run holds the whole alarm on every tick.
  local retrieval="public-source"
  if [ ! -f "$STATE_DIR/runtime-public-source.approved" ]; then
    retrieval="origin"
    log "PUBLIC-SOURCE-NOT-APPROVED  using the prior origin transport until the operator approves it"
    fetch_public_runtime() { GIT_TERMINAL_PROMPT=0 GCM_INTERACTIVE=never GIT_SSH_COMMAND='ssh -o BatchMode=yes -o StrictHostKeyChecking=yes' rgit -c credential.helper= fetch -q origin; }
  fi

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

  # Unlettered-repair tick:  replay the start of this Mac's chats still showing "??: " - an app-driven
  # chat's hook never runs, so only a scheduled tick reaches it.  platform-letter's own stamp throttles the
  # pass to hourly;  a failure here never blocks the sync, and the 30s alarm keeps even a stalled tick well
  # under the sync's 120s cadence (the pass itself starts no new replay after REPAIR_PASS_SECONDS).  stdout is
  # dropped;  stderr reaches the launchd log.
  if [ -x "$HOME/.local/bin/platform-letter" ]; then
    perl -e 'alarm 30; exec @ARGV' "$HOME/.local/bin/platform-letter" repair --location mac >/dev/null || true
  fi

  # Mac GitOps G7 (AFC-375): optional check-only drift report against deploy/mac/manifest.toml.
  # Never loads or unloads LaunchAgents; apply is only via mac_gitops reconcile --apply by hand.
  if [ -e "$STATE_DIR/mac-gitops-check.enabled" ]; then
    if [ -f "$RUNTIME/deploy/mac/manifest.toml" ] && [ -f "$RUNTIME/scripts/mac-gitops-reconcile.py" ]; then
      if ! perl -e 'alarm 60; exec @ARGV' /opt/homebrew/bin/python3 -I \
          "$RUNTIME/scripts/mac-gitops-reconcile.py" \
          --manifest "$RUNTIME/deploy/mac/manifest.toml" \
          --repo-root "$RUNTIME" \
          --skip-excluded-report >/dev/null 2>&1; then
        log "MAC-GITOPS-DRIFT  deploy/mac/manifest.toml differs from this Mac; run mac_gitops reconcile (check-only) for detail"
      fi
    fi
  fi

  # Code-only Coolify CLI sync uses this same tick, lock and pause.  Run before the
  # local fetch/no-change exits so failed sends retry and a changed Mac revision
  # reaches Coolify on the next tick.  Updates never register wrappers or MCP.
  if [ -e "$STATE_DIR/coolify-cli-sync.enabled" ]; then
    if [ ! -f "$RUNTIME/scripts/fleet-cli-sync.py" ]; then
      log "COOLIFY-CLI-SYNC-MISSING  published pusher is not available yet; will retry next run"
    elif ! perl -e 'alarm 95; exec @ARGV' /opt/homebrew/bin/python3 \
        "$RUNTIME/scripts/fleet-cli-sync.py" --source-repo "$RUNTIME" >/dev/null 2>&1; then
      log "COOLIFY-CLI-SYNC-FAILED  code push did not complete; will retry next run"
    fi
  fi

  if ! git -C "$RUNTIME" rev-parse --git-dir >/dev/null 2>&1; then
    log "ERROR  $RUNTIME is not a git worktree"
    exit 1
  fi

  if ! fetch_public_runtime; then
    if [ "$retrieval" = "public-source" ]; then
      log "FETCH-FAILED  fixed public-source retrieval failed; no credential fallback"
    else
      log "FETCH-FAILED  git fetch origin did not finish; will retry next run"
    fi
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
  local tracked_status
  tracked_status="$(rgit status --porcelain --untracked-files=no 2>/dev/null)" || {
    log "STATUS-FAILED  cannot establish tracked cleanliness; left untouched"
    exit 1
  }
  if [ -n "$tracked_status" ]; then
    log "SKIP-DIRTY  runtime worktree has tracked changes; not moving ${old:0:7} -> ${new:0:7} (left untouched)"
    exit 0
  fi

  if ! rgit merge-base --is-ancestor "$old" "$new"; then
    log "NON-FAST-FORWARD  main is not a descendant of the installed commit; left untouched"
    exit 1
  fi
  local changed
  changed="$(rgit diff --name-only "$old" "$new" 2>/dev/null)" || {
    log "DIFF-FAILED  cannot establish changed paths; left untouched"
    exit 1
  }

  # Checkout the validated object, never a mutable remote-tracking name.
  if ! rgit checkout -q --detach "$new"; then
    log "CHECKOUT-FAILED  ${old:0:7} -> ${new:0:7}; left as it was"
    exit 1
  fi

  # Smoke test the new code before any restart can take the listener down with it.
  if ! perl -e 'alarm 60; exec @ARGV' /opt/homebrew/bin/python3 "$RUNTIME/scripts/simple-chat" --version >/dev/null 2>&1; then
    log "SMOKE-FAILED  simple-chat --version failed at ${new:0:7}; rolled back to ${old:0:7} and will not retry it"
    rgit checkout -q --detach "$old" || log "ROLLBACK-FAILED  could not return to ${old:0:7}"
    printf '%s\n' "$new" > "$BAD_FILE"
    exit 1
  fi
  rm -f "$BAD_FILE"

  log "UPDATED  ${old:0:7} -> ${new:0:7}  ($(printf '%s\n' "$changed" | grep -c .) files)"

  if printf '%s\n' "$changed" | grep -Eq "$PLUGIN_RE"; then
    log "PLUGIN-CHANGED  the Claude plugin source moved; refresh with: claude plugin marketplace update afc"
  fi

  # The Mac listener is retired. Updating shared CLI code never loads,
  # restarts or re-enables it. Hosted listener deployment has its own owner.

}

main "$@"
exit $?
