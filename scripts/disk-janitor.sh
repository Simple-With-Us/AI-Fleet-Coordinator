#!/bin/bash
# Disk janitor — runs every 30 min via launchd (com.jay.disk-janitor).
# Philosophy: BRIEF health check each run; only REDUCE storage when space actually
# gets low, and only by deleting REGENERABLE things (caches, build output, deps on
# long-idle CLEAN worktrees). The ONE thing it removes that `npm ci` won't rebuild is
# an OLD, fully-merged, CLEAN, 7-day-idle git worktree (see the "retire" block) — and
# even that loses nothing in git: `git worktree remove` deletes only the checkout
# directory; the branch ref and its commits remain, so `git worktree add` restores it.
# It never touches a dirty or recently-active worktree, never a standing lane, and only
# ever clears .next/CACHE on prod/dev (never the whole prod build).
# Keep a specific worktree forever:  touch <worktree>/.janitor-keep
# Disable everything:  launchctl bootout gui/$(id -u)/com.jay.disk-janitor
# Log:      ~/.claude-disk-janitor/janitor.log
# Live install: ~/.claude-disk-janitor/janitor.sh (launchd com.jay.disk-janitor).
# After changing this tracked copy: cp scripts/disk-janitor.sh ~/.claude-disk-janitor/janitor.sh && chmod +x ~/.claude-disk-janitor/janitor.sh
# 2026-08-22: all fleet Code repos; standing-lane KEEP_RE; retired-KIMI seat /
# nested / tmp may reap when idle even if unmerged.  Never skip the idle check.
# Do not substring-match "kimi" (that reaps cursor/kimi-audit-def / ST #3044).
# 2026-09-13: `cleanmymac clean --force` ran ALL of clean's modules, including
# `junk` (system junk, which classes ~/Library/Logs user logs as junk with no
# regard for whether launchd still has the file open for append). That deleted
# the always-on com.jay.botfleet-server LaunchAgent's live
# ~/Library/Logs/botfleet/server.log out from under it on 2026-09-12 ~12:31 --
# every harness log line until the 20:38 restart went to an unlinked inode, and
# the desktop error page pointed at a file that no longer existed. Scoped the
# CLI call to the three modules that only ever touch regenerable caches/build
# artifacts/trash (never a file a running process still has open), and added
# ~/Library/Logs, ~/Library/Logs/botfleet, and ~/.botfleet to CleanMyMac's own
# ignore list (`cleanmymac ignore add`, belt-and-suspenders alongside dropping
# `junk`) so any future `clean junk` call -- from this script, from
# ~/apps/mac-auto-cleanup.sh's own unconditional `cleanmymac clean --force`, or
# from CleanMyMac's own background Smart Care agent -- skips them too.
# 2026-09-30 (Claude): memory/load pressure gate.  On a thrashing Mac (load1 > 40 or
# swap >= 90%) the run logs PRESSURE-SKIP, does only the cheap truncations, and exits --
# no git/gh/find-over-worktrees phase, no CleanMyMac.  See the gate below the lock.
# 2026-10-07 (lanes-guard, board a7dfde0e / 912034a0): REPOS = legacy list UNION fleet-apps.json; every repo root joins
# KEEP_RE; nothing under ~/apps/lanes is retired unless the lane doctor says SAFE-TO-REMOVE + MERGED + >= STALE_DAYS old;
# a checkout BORN < STALE_DAYS ago is never retired (0-ahead ancestry is not "merged" for a fresh lane); a process cwd,
# .janitor-keep, or a timed-out activity scan now protects from the dep-reap too.  See docs in the patch rationale.
# 2026-10-08 (lanes-guard review): a lane under ~/apps/lanes retires only on the doctor's cleaner contract (schema >= 2,
# fresh generated_at, cleaner_candidates, gh ok) bound to the same directory and the same HEAD; a failed, killed or
# cut-off lsof keeps EVERY checkout; a failed git status reads as dirt and ls-files rc 128 as tracked; the dep-reap no
# longer reaches into a checkout nested in a lane and re-checks cwd right before deleting; a checkout holding another
# one is never retired; paths compare by on-disk spelling, without letter case; an unknown free-space reading skips
# every free-gated phase instead of reading as 0 G.  The gates sit above the library-mode return and are covered by
# scripts/test-disk-janitor-lanes.sh (hermetic: fake HOME, fake gh, lsof and doctor).

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.local/bin:$HOME/.npm-global/bin"
# git must never block on a credential prompt (no tty under launchd) or a network stall:
# fail fast instead of hanging the whole tick behind the run-lock.
export GIT_TERMINAL_PROMPT=0 GIT_HTTP_LOW_SPEED_LIMIT=1000 GIT_HTTP_LOW_SPEED_TIME=15
HOME_DIR="$HOME"
DIR="$HOME_DIR/.claude-disk-janitor"
LOG="$DIR/janitor.log"
STATE="$DIR/state"
LOCK="$DIR/.lock"
DATA_VOL="/System/Volumes/Data"

# ---- thresholds (GiB free) ----
# 2026-09-01: overnight 6-13G drops in 30 min blew through the old 50G cliff
# (45G free, then recovered).  Start cache reclaim at 80G so a spike still
# has headroom.  Resource-watch (every 5 min) wakes Housekeeper on the same
# numbers plus RAM/CPU.
LOW_FREE=80        # below this -> clear regenerable caches + prod/dev build caches
PRESSURE_FREE=65   # below this -> ALSO reap .next/node_modules on CLEAN worktrees idle > IDLE_HRS
CRIT_FREE=${CRIT_FREE:-30}  # below this -> destructive `uv cache clean` allowed (still gated on in-use check)
DROP_ALERT=6       # free dropped at least this much since last run -> flag it
IDLE_HRS=4         # a worktree is "abandoned" (dep-reapable) after this many hours untouched
PM2_LOG_CAP_MB=50  # truncate any single pm2 log larger than this (pure waste, always)
JANITOR_MAX_LOAD=${JANITOR_MAX_LOAD:-250}         # HARD stop: load1 above this -> skip everything but cheap truncations
JANITOR_MAX_SWAP_PCT=${JANITOR_MAX_SWAP_PCT:-98}  # HARD stop: swap used% at/above this -> skip everything but cheap truncations
# 2026-10-02 (MINIMAX), board ce1b42b1.  The gate used to be binary, and that was a
# catch-22: refusing to clean is exactly what let the machine get too loaded to clean.
# The old load arm (40) and old swap arm (90) both sat AT this Mac's normal operating
# level, so every tick skipped and nothing was ever retired -- the pile-up the janitor
# exists to prevent was caused by the janitor standing down.  It is now GRADED:
#
#   hard  (load1 > 250 or swap >= 98): cheap truncations only.  Real crisis, the box
#         is thrashing and even bounded git work is counter-productive.
#   lean  (load1 >= 150 or swap >= 90): skip the read-only du probes, but STILL prune
#         and retire old merged worktrees, and still run the disk-pressure cache
#         sweeps.  Retirement is watchdog-bounded (30s fetch per repo, 30s per
#         remove), gated on clean + STALE_DAYS idle + actually merged, and cannot
#         lose a branch or a commit -- so it is the one phase worth running precisely
#         when the host is loaded.  This is the arm that broke the catch-22.
JANITOR_SOFT_LOAD=${JANITOR_SOFT_LOAD:-150}         # lean mode at/above this load1
JANITOR_SOFT_SWAP_PCT=${JANITOR_SOFT_SWAP_PCT:-90}  # lean mode at/above this swap used%

# ---- old-worktree retirement (removes the whole checkout dir; branch+commits survive) ----
REAP_WORKTREES=${REAP_WORKTREES:-1}   # master switch: 1 = retire old merged worktrees every run, 0 = off
STALE_DAYS=${STALE_DAYS:-7}           # a CLEAN, MERGED/gone worktree untouched this many days is retired
WT_REAP_DRYRUN=${WT_REAP_DRYRUN:-0}   # 1 = only LOG "WOULD-RETIRE ..." and remove nothing (for testing)

REPOS=(
  /Users/jay/Code/Socratic.Trade
  /Users/jay/Code/Congress.Trade
  /Users/jay/Code/Usage-Monitor
  /Users/jay/Code/congress-trading-shared
  /Users/jay/Code/DealDex
  /Users/jay/Code/Personal-Site
  /Users/jay/Code/Autorotate
  /Users/jay/Code/ContactLogo
  /Users/jay/Code/AI-Fleet-Coordinator
  /Users/jay/Code/BotFleet
  /Users/jay/Code/fleet-ops
  /Users/jay/Code/botfleet-site
)
# Standing lanes / primaries the janitor must never touch.  Code roots +
# unsuffixed seat checkouts (trading-grok, dealdex-claude, …) + runtimes.
# 2026-09-12: + ~/apps/botfleet-server, the detached checkout the always-on
# com.jay.botfleet-server LaunchAgent runs from.  It is clean and "idle" by
# construction (nothing edits it), so the pressure reap below deleted its
# node_modules and the next harness restart crash-looped on a missing package.
# Suffixed per-lane trees (trading-grok-litestream-cascade) remain reaped
# when merged+idle.  Retired-KIMI seat trees reap when idle (not on a "kimi"
# substring, and never by skipping the idle check).
KEEP_RE="^(/Users/jay/Code/Socratic.Trade|/Users/jay/Code/Congress.Trade|/Users/jay/Code/Usage-Monitor|/Users/jay/Code/congress-trading-shared|/Users/jay/Code/DealDex|/Users/jay/Code/Personal-Site|/Users/jay/Code/Autorotate|/Users/jay/Code/ContactLogo|/Users/jay/Code/AI-Fleet-Coordinator|/Users/jay/Code/BotFleet|/Users/jay/Code/fleet-ops|/Users/jay/Code/botfleet-site|/Users/jay/apps/[a-z0-9]+-(claude|codex|live|antigravity|cursor|monet|grok|grok-build|deepseek|minimax|mm)|/Users/jay/apps/(grok-acp-runtime|agy-acp-runtime|shellular-runtime|mac-collab|seat-mcp|KIMI-SALVAGE-2026-08-22|botfleet-server|agent-sync|agent-sync-push|clutch-runtime|xcode-health))$"

# Retired-KIMI seat, nested agent scratch, or /tmp.  Not a substring:
# branch cursor/kimi-audit-def (ST #3044, owner-kept) must not match.
janitor_is_retired_kimi_or_scratch() {
  local wt="$1" br="${2#refs/heads/}"
  case "$br" in
    kimi/*|KIMI/*) return 0 ;;
  esac
  case "$wt" in
    */.claude/worktrees/*|*/.grok/worktrees/*|/private/tmp/*|/tmp/*) return 0 ;;
  esac
  local base="${wt##*/}"
  case "$base" in
    *-kimi|*-kimi-*)
      case "$base" in
        *-claude-*|*-codex-*|*-live-*|*-antigravity-*|*-cursor-*|*-monet-*|*-grok-*|*-grok-build-*|*-deepseek-*|*-minimax-*|*-mm-*)
          return 1 ;;
      esac
      return 0 ;;
  esac
  return 1
}

# Squash-safe GitHub owner/repo from `git remote get-url origin`.
# ssh, https, and ssh:// forms all collapse to owner/repo.
janitor_github_repo() {
  local url
  url=$(git -C "$1" remote get-url origin 2>/dev/null) || return 1
  url=${url%.git}
  url=${url#git@github.com:}
  url=${url#https://github.com/}
  url=${url#http://github.com/}
  url=${url#ssh://git@github.com/}
  case "$url" in
    */*) printf '%s\n' "$url"; return 0 ;;
    *) return 1 ;;
  esac
}

# True when GitHub has a MERGED PR whose head is this branch AND whose head sha is this checkout's HEAD (or HEAD is an
# ancestor of it).  Squash-merge rewrites SHAs so merge-base --is-ancestor against main is the WRONG test (board
# 059f65b3); but a branch NAME alone is not enough either -- a reused name made a fresh lane look merged (board 912034a0).
# Bounded to 15s so a hung `gh` cannot pin the launchd tick.
janitor_pr_merged() {
  local wt="$1" br="${2#refs/heads/}" sha="$3" repo oids oid
  [ -n "$br" ] && [ "$br" != "HEAD" ] && [ -n "$sha" ] || return 1
  repo=$(janitor_github_repo "$wt") || return 1
  oids=$(python3 -c '
import subprocess, sys
repo, br = sys.argv[1], sys.argv[2]
try:
    r = subprocess.run(
        ["gh", "pr", "list", "--repo", repo, "--head", br,
         "--state", "merged", "--json", "headRefOid", "--jq", ".[].headRefOid"],
        capture_output=True, text=True, timeout=15,
    )
except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
    sys.exit(1)
if r.returncode != 0:
    sys.exit(1)
print((r.stdout or "").strip())
' "$repo" "$br" 2>/dev/null) || return 1
  for oid in $oids; do
    [ "$oid" = "$sha" ] && return 0
    git -C "$wt" merge-base --is-ancestor "$sha" "$oid" 2>/dev/null && return 0
  done
  return 1
}

# ---- Definitions only, shared by the run below and by scripts/test-disk-janitor-lanes.sh ----
# Library mode (JANITOR_LIB_ONLY=1) returns right after this block.  Nothing here creates a directory, takes the
# lock, writes a log or runs a phase: it only defines functions and plain variables.

# 2026-09-30 (Claude): kill a process AND its descendants.  Killing only a stuck holder's
# pid (or a watchdog's direct child) re-parented its git/find/gh children to launchd, where
# they kept running and kept hammering a thrashing Mac.  Freeze each node before listing its
# children so a busy loop cannot spawn the next child mid-walk, then kill deepest first.
# No `ps`: pgrep -P lists children by pid only.
janitor_kill_tree() {
  local p="$1" c
  [ -n "$p" ] && [ "$p" != "$$" ] && [ "$p" != "1" ] || return 0
  kill -STOP "$p" 2>/dev/null || return 0     # already gone (or not ours) -> nothing to do
  for c in $(pgrep -P "$p" 2>/dev/null); do janitor_kill_tree "$c"; done
  kill -9 "$p" 2>/dev/null || true
}

# Phase heartbeat helper. Writes a marker line to janitor.log AND updates
# $LOCK/heartbeat so the next launchd tick can detect a stuck live holder
# (BF-FIXER 2026-09-25 follow-up to BF-HOUSEKEEPER's janitor hang report).
janitor_heartbeat() {
  # BF-HOUSEKEEPER 2026-10-07: the PRESSURE-SKIP path runs the cheap truncations
  # BEFORE `free` is assigned (janitor_read_free), so $free was unset here and
  # every phase line logged a useless `free=G` — which reads exactly like the sensor
  # bug fixed above and costs real diagnostic time. Resolve on demand, then cache.
  # 2026-10-08 (lanes-guard review): probe at most once, and an unknown reading logs `free=?G`, never a fake 0.
  local _f="${free:-}" _k
  if [ -z "$_f" ]; then
    if [ -z "${JANITOR_FREE_PROBED:-}" ]; then
      JANITOR_FREE_PROBED=1
      _k=$(freek)
      [ -n "$_k" ] && free=$(gib "$_k")
    fi
    _f="${free:-?}"
  fi
  printf '%s  PHASE %s pid=%s free=%sG\n' \
    "$(date '+%Y-%m-%d %H:%M')" "$1" "$$" "$_f" >> "$LOG"
  printf '%s\n' "$(date +%s)" > "$LOCK/heartbeat" 2>/dev/null || true
}

# Phase watchdog: run the given command in the background and SIGKILL it after
# N seconds (macOS has no GNU `timeout`, exit 127). Prints a timeout line to the
# log and returns 124 so the caller can branch. Use only on phases where a hang
# is the worst case (BF-HOUSEKEEPER 2026-09-25). Refreshes $LOCK/heartbeat
# every 5s of waiting so the next launchd tick sees a live (not stuck) holder
# (BF-FIXER 2026-09-25 follow-up).
janitor_watchdog() {
  # usage: janitor_watchdog <seconds> <label> -- <cmd...>
  local secs="$1" label="$2"; shift 2
  [ "$1" = "--" ] && shift
  local tmp_out
  tmp_out=$(mktemp -t jwd 2>/dev/null || echo "/tmp/jwd.$$.$RANDOM")
  "$@" >"$tmp_out" 2>&1 &
  local wd_pid=$!
  local waited=0
  while kill -0 "$wd_pid" 2>/dev/null && [ "$waited" -lt "$secs" ]; do
    sleep 1; waited=$((waited + 1))
    # Refresh lock heartbeat every 5s so the next launchd tick doesn't mistake
    # this run for stuck (we're inside a bounded watchdog, NOT a hang).
    if [ $((waited % 5)) -eq 0 ]; then
      printf '%s\n' "$(date +%s)" > "$LOCK/heartbeat" 2>/dev/null || true
    fi
  done
  if kill -0 "$wd_pid" 2>/dev/null; then
    janitor_kill_tree "$wd_pid"   # 2026-09-30 (Claude): was kill -9 of the direct child only; `sh -c find` left its find orphaned
    printf '%s  WATCHDOG killed phase=%s after %ss (pid=%s)\n' \
      "$(date '+%Y-%m-%d %H:%M')" "$label" "$secs" "$wd_pid" >> "$LOG"
    rm -f "$tmp_out"
    return 124
  fi
  wait "$wd_pid" 2>/dev/null
  local rc=$?
  cat "$tmp_out"
  rm -f "$tmp_out"
  return $rc
}

# Free KiB on the data volume.  Prints NOTHING (and returns 1) when it cannot tell: never a made-up 0.
freek() {
  # BF-FIXER 2026-09-25 follow-up: bound df so a hung `df` (NFS / SMB timeout, dead
  # APFS snapshot) can't wedge a tick.
  # BF-HOUSEKEEPER 2026-10-05: the bound was 5s and the timeout returned 0 KiB.
  # Under load `df | tail | awk` can exceed 5s, so ONE timeout made freek() report
  # "0G free" for the rest of the run — below CRIT_FREE, which opened the destructive
  # lowfree branches on a disk that actually had 76G free, and wrote 0 into $STATE so
  # `reclaimed` reported phantom wins. Now: 30s bound, and a failed probe falls back
  # to the last known-good value in $STATE instead of 0. Wedge protection is kept.
  # 2026-10-08 (lanes-guard review): with no positive prior (first run, deleted or poisoned $STATE) this still
  # echoed 0, and 0 opened lowfree, the dep-reap and the CRIT `uv cache clean` tier at once.  Now it prints nothing.
  local out prior
  out=$(janitor_watchdog 30 "freek" -- bash -c "df -k '$DATA_VOL' 2>/dev/null | tail -1 | awk '{print \$4}'") || out=""
  # janitor_watchdog `cat`s the child's captured stdout, so a df/awk error string
  # (or a multiline capture) can land in $out. Sanitise to bare digits FIRST: an
  # empty or non-numeric $out used to reach gib(), which emitted nothing at all and
  # produced `free=G` in every log line plus a broken `[ "$free" -lt "$CRIT_FREE" ]`.
  out=$(printf '%s' "$out" | tr -dc '0-9')
  if [ -z "$out" ] || [ "$out" -eq 0 ] 2>/dev/null; then
    # Probe failed or returned nothing usable. Reuse the last real reading
    # rather than 0: a stale-but-true number never crosses CRIT_FREE on its own.
    prior=$(sed -n 's/^free=\([0-9][0-9]*\)G*$/\1/p' "$STATE" 2>/dev/null | tail -1)
    prior=$(printf '%s' "$prior" | tr -dc '0-9')
    if [ -n "$prior" ] && [ "$prior" -gt 0 ] 2>/dev/null; then
      printf '%s\n' "$((prior * 1024 * 1024))"   # GiB -> KiB, freek's contract
      return 0
    fi
    return 1   # unknown: the caller skips every free-gated phase (janitor_read_free)
  else
    echo "$out"
  fi
}
# BF-HOUSEKEEPER 2026-10-07: gib() was unguarded. Any non-numeric argument (a df/awk
# error string that janitor_watchdog `cat`s through) made $(( )) raise a syntax error
# and print NOTHING, so callers got an empty string -- which logged as `free=G` and broke
# every `[ "$free" -lt "$CRIT_FREE" ]` compare. Coerce to digits, then to 0, so this
# function can never emit an empty or non-numeric value.  Never pass it an unknown reading: "" is 0 here.
gib() { local n; n=$(printf '%s' "${1:-}" | tr -dc '0-9'); echo $(( ${n:-0} / 1024 / 1024 )); }
# Sets free_k (KiB, empty = unknown), free (GiB) and FREE_KNOWN.  Unknown logs FREE-UNKNOWN once and leaves FREE_KNOWN=0,
# which every free-gated phase (lowfree, CRIT, pressure dep-reap) checks first, so a failed probe can never open them.
janitor_read_free() {
  JANITOR_FREE_PROBED=1
  free_k=$(freek)
  if [ -n "$free_k" ]; then
    FREE_KNOWN=1; free=$(gib "$free_k")
  else
    FREE_KNOWN=0; free=""
    printf '%s  FREE-UNKNOWN df failed and %s has no prior reading: lowfree, CRIT and pressure phases skipped this tick\n' \
      "$(date '+%Y-%m-%d %H:%M')" "$STATE" >> "$LOG"
  fi
}

# Porcelain output minus UNTRACKED generated build junk (node_modules/.next/build receipts/logs).
# Empty output = safe to treat the worktree as clean for retirement/dep-reap purposes. Any tracked
# modification (' M', 'A ', etc.) or non-generated untracked file still blocks. Added 2026-07-19
# (owner-directed): dozens of long-dead worktrees carried only untracked node_modules/.next and so
# never qualified as clean, pinning ~50M-1G each forever.
# 2026-10-08 (lanes-guard review): a FAILED git status (corrupt index, killed under memory pressure, broken gitfile)
# printed nothing and so read as clean, and the dep-reap then deleted a tracked vendored node_modules with its edits.
wt_blocking_dirt() {
  local out
  out=$(git -C "$1" status --porcelain 2>/dev/null) || { echo "git-status-failed"; return 0; }
  [ -n "$out" ] || return 0
  printf '%s\n' "$out" | grep -vE '^\?\? (node_modules/|\.next/|next-env\.d\.ts$|tsconfig\.tsbuildinfo$|\.DS_Store$|[^ ]*\.log$|data/app\.db(-wal|-shm)?$)'
}

# ==== lanes-guard BEGIN  (board a7dfde0e Lane Map; board 912034a0 worktree vanish) ====
# Layout v2 (docs/protocols/lane-map.md, owner 2026-10-09): ~/apps/lanes/<Repo>/<seat>-<slug>, lanes/<Repo>/review-pr-<n>,
# lanes/<Repo>/<slug>-<hex> (Claude desktop), lanes/_codex/<slug>/<Repo>; the pre-v2 lanes/<prefix>/..., lanes/_review/...
# and lanes/_managed/... still count until the migration moves them.  The test below is only "under $LANES_ROOT", so
# no code change was needed for v2.  Nothing under $LANES_ROOT is ever retired on the legacy tests (0-ahead ancestry,
# [gone] upstream, scratch pattern).  It retires only when a FRESH lane doctor report lists it in cleaner_candidates
# with this checkout's CURRENT HEAD (the cleaner contract, scripts/fleet_lanes/README.md), on top of the janitor's
# own birth, dirt, idle, inner-checkout and live lsof gates.  A missing, stale, future-dated, malformed or schema-1
# report, or a failed lsof, means KEEP (fail closed).
LANES_ROOT=${LANES_ROOT:-/Users/jay/apps/lanes}
FLEET_APPS_JSON=${FLEET_APPS_JSON:-/Users/jay/Code/AI-Fleet-Coordinator/fleet-apps.json}
FLEET_APPS_CACHE="$DIR/fleet-apps.json.cache"
AFC_SCRIPTS=${AFC_SCRIPTS:-/Users/jay/Code/AI-Fleet-Coordinator/scripts}
LANE_REPORT=${LANE_REPORT:-$DIR/lane-doctor.json}
LANE_REPORT_MAX_MIN=${LANE_REPORT_MAX_MIN:-180}
LANE_DOCTOR_FAIL="$DIR/lane-doctor.fail"          # stamp: last refresh timed out or failed
LANE_DOCTOR_RETRY_MIN=${LANE_DOCTOR_RETRY_MIN:-360}  # do not retry a failed refresh for this long (at this load a 300 s retry every tick is the harm)
LANE_DOCTOR_EXTRA_ARGS=${LANE_DOCTOR_EXTRA_ARGS:-}   # tests only, e.g. "--home X --tmp-root Y"
JANITOR_LSOF_SECS=${JANITOR_LSOF_SECS:-90}           # watchdog for one `lsof -d cwd` snapshot
JANITOR_SCAN_SECS=${JANITOR_SCAN_SECS:-15}           # watchdog for one dep-reap activity scan

# The on-disk spelling of a directory: symlinks resolved AND the letter case the volume stores.  git records a worktree
# in the case it was typed (`git worktree add ~/Apps/lanes/...` is listed as Apps), lsof prints the on-disk case, and
# bash 3.2's builtin `pwd -P` keeps the typed case, so this must be the external /bin/pwd.  rc 1 = cannot enter it.
janitor_canon() { ( cd -- "$1" 2>/dev/null && /bin/pwd -P ); }

# 0 = under $LANES_ROOT, by git's spelling or the on-disk one, compared without letter case (on a case-insensitive
# volume ~/Apps/lanes IS ~/apps/lanes, and a false "nested" only adds protection).
janitor_is_nested_lane() {
  local p root
  if [ "${LANES_ROOT_CANON_FOR:-}" != "$LANES_ROOT" ]; then
    LANES_ROOT_CANON_FOR=$LANES_ROOT
    LANES_ROOT_LC=$(printf '%s\n%s\n' "${LANES_ROOT%/}" "$(janitor_canon "$LANES_ROOT")" | tr '[:upper:]' '[:lower:]')
  fi
  while IFS= read -r p; do
    [ -n "$p" ] || continue
    while IFS= read -r root; do
      [ -n "$root" ] || continue
      case "$p" in "$root"/*) return 0 ;; esac
    done <<EOF
$LANES_ROOT_LC
EOF
  done <<EOF
$(printf '%s\n%s\n' "$1" "$(janitor_canon "$1")" | tr '[:upper:]' '[:lower:]')
EOF
  return 1
}

# 0 = KEEP_RE protects this checkout by git's spelling or by the on-disk one.  Case-insensitive: on this volume
# Code/Fleet-OPS and Code/fleet-ops are one directory, and a wider match only keeps more.
janitor_keep_match() {
  printf '%s\n%s\n' "$1" "$(janitor_canon "$1")" | grep -qiE "$KEEP_RE"
}

# Repo roots from the registry (codeRoot/codeDir), cached so a missing/ broken AFC checkout cannot shrink the set.
janitor_registry_repos() {
  if python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$FLEET_APPS_JSON" 2>/dev/null; then
    cp "$FLEET_APPS_JSON" "$FLEET_APPS_CACHE.tmp" 2>/dev/null && mv "$FLEET_APPS_CACHE.tmp" "$FLEET_APPS_CACHE"
  fi
  [ -s "$FLEET_APPS_CACHE" ] || return 0
  python3 - "$FLEET_APPS_CACHE" 2>/dev/null <<'PY'
import json, os, sys
d = json.load(open(sys.argv[1]))
root = d.get("codeRoot") or "/Users/jay/Code"
for a in d.get("apps", []):
    name = a.get("codeDir") or a.get("repo")
    if name:
        print(os.path.join(root, name))
PY
}

# UNION of the legacy list and the registry, de-duplicated by on-disk path (Socratic.Trade is a symlink, and a
# registry entry may differ from the directory only in letter case).
janitor_extend_repos() {
  local r rp seen=" " out=()
  for r in "${REPOS[@]}" $(janitor_registry_repos); do
    rp=$(janitor_canon "$r") || continue
    [ -e "$rp/.git" ] || continue
    case "$seen" in *" $rp "*) continue ;; esac
    seen="$seen$rp "; out+=("$rp")
  done
  [ "${#out[@]}" -gt 0 ] && REPOS=("${out[@]}")
}

# Every repo root (as typed AND as git reports it: Code/Fleet-OPS vs fleet-ops) joins KEEP_RE, so widening REPOS
# can never expose an integration tree to the retire or dep-reap phases.
janitor_protect_primaries() {
  local r p esc extra=""
  for r in "${REPOS[@]}"; do
    for p in "$r" "$(git -C "$r" worktree list --porcelain 2>/dev/null | awk '/^worktree /{sub(/^worktree /,""); print; exit}')"; do
      [ -n "$p" ] || continue
      esc=$(printf '%s' "$p" | sed 's/[][\\.*^$+?(){}|]/\\&/g')
      extra="${extra}|${esc}"
    done
  done
  [ -n "$extra" ] && KEEP_RE="${KEEP_RE%)\$}${extra})\$"
  return 0
}

# True (0) when the checkout dir OR its git admin dir was BORN less than STALE_DAYS ago, or birth time is unreadable.
# Birth time, not mtime: a clone/rsync/cp -c can carry old mtimes into a brand-new lane.
janitor_wt_too_young() {
  local wt="$1" gd b_dir b_adm now_s min_s=$(( STALE_DAYS * 86400 ))
  now_s=$(date +%s)
  b_dir=$(stat -f %B "$wt" 2>/dev/null) || return 0
  [ "${b_dir:-0}" -gt 0 ] 2>/dev/null || return 0
  [ $(( now_s - b_dir )) -lt "$min_s" ] && return 0
  gd=$(git -C "$wt" rev-parse --absolute-git-dir 2>/dev/null) || return 0
  b_adm=$(stat -f %B "$gd" 2>/dev/null) || return 0
  [ "${b_adm:-0}" -gt 0 ] 2>/dev/null || return 0
  [ $(( now_s - b_adm )) -lt "$min_s" ] && return 0
  return 1
}

# HEAD is reachable from some branch, remote-tracking ref or tag, so removing the checkout cannot orphan a commit.
janitor_head_reachable() {
  [ -n "$(git -C "$1" for-each-ref --contains HEAD --count=1 --format=x refs/heads refs/remotes refs/tags 2>/dev/null)" ]
}

# 0 = another checkout (a .git file or dir) sits inside this one, e.g. <lane>/.claude/worktrees/<session> from a
# Claude Code session started in the lane.  It is usually gitignored, so the outer lane reads clean, but
# `git worktree remove` deletes it with everything uncommitted in it, and its own .janitor-keep, age and dirt are never
# consulted.  Cannot look (unreadable dir, find error) = 0 too.
janitor_has_inner_checkout() {
  local hit
  hit=$(cd -- "$1" 2>/dev/null && find . \( -name node_modules -o -name .next -o -name .turbo -o -path ./.git \) -prune \
          -o -name .git -print -quit 2>/dev/null) || return 0
  [ -n "$hit" ]
}

# One bounded `lsof -d cwd -Fn`.  CWD_SNAP_OK=1 only when lsof exited 0 AND printed at least one cwd.  stderr is kept
# out of the snapshot (the watchdog merges it into stdout), and a killed (rc 137 under jetsam), timed-out (124) or
# failed run is no snapshot at all: a cut-off listing silently drops the processes it never printed.  rc 0 only,
# stricter than the doctor's 0-or-1, because an lsof that reports an error may have skipped processes.  A failure
# is logged, so a gate that stays closed tick after tick is visible.
janitor_cwd_snapshot() {
  local rc
  CWD_SNAP_DONE=1; CWD_SNAP_OK=0; CWD_SNAP="$LOCK/cwd-snapshot"
  janitor_watchdog "$JANITOR_LSOF_SECS" "lsof-cwd" -- sh -c 'exec lsof -d cwd -Fn 2>/dev/null' > "$CWD_SNAP.raw" 2>/dev/null
  rc=$?
  if [ "$rc" = 0 ] && sed -n 's/^n//p' "$CWD_SNAP.raw" > "$CWD_SNAP" 2>/dev/null && [ -s "$CWD_SNAP" ]; then
    CWD_SNAP_OK=1
  else
    printf '%s  CWD-SNAPSHOT-FAILED rc=%s: every checkout counts as busy until the next snapshot\n' \
      "$(date '+%Y-%m-%d %H:%M')" "$rc" >> "$LOG"
  fi
  rm -f "$CWD_SNAP.raw"
}
# 0 = busy: some process has this checkout (or a directory inside it) as its cwd, by git's spelling or the on-disk
# one, without letter case.  Takes the phase's snapshot on first use.  No usable snapshot = EVERY checkout is busy
# (fail closed, flat lanes too): the next tick retries, and argv-only pgrep cannot see a shell parked in a lane.
janitor_wt_busy() {
  local c
  [ -n "${CWD_SNAP_DONE:-}" ] || janitor_cwd_snapshot
  [ "${CWD_SNAP_OK:-0}" = 1 ] && [ -r "${CWD_SNAP:-}" ] || return 0
  c=$(janitor_canon "$1") || return 0
  JW_A="$1" JW_B="$c" awk 'BEGIN { a = tolower(ENVIRON["JW_A"]); b = tolower(ENVIRON["JW_B"]) }
    { l = tolower($0) }
    l == a || l == b || index(l, a "/") == 1 || index(l, b "/") == 1 { f = 1; exit }
    END { exit !f }' "$CWD_SNAP"
}
# 0 = busy: some process names this checkout in its argv.  The path is regex-escaped (a lane called claude-c++-fix made
# pgrep exit 2), and any answer but a clean "no match" (rc 1) counts as busy.
janitor_argv_busy() {
  pgrep -f "$(printf '%s' "$1" | sed 's/[][\\.*^$+?(){}|]/\\&/g')" >/dev/null 2>&1
  [ "$?" != 1 ]
}

# Minutes since the file's mtime; 999999 if it does not exist, -1 if it is dated in the future.
janitor_file_age_min() {
  local m d
  m=$(stat -f %m "$1" 2>/dev/null) || { echo 999999; return; }
  d=$(( $(date +%s) - m ))
  if [ "$d" -lt 0 ]; then echo -1; else echo $(( d / 60 )); fi
}
# Refresh the saved doctor report at most once per run: outside lean mode, only when it is stale or future-dated, and
# never within LANE_DOCTOR_RETRY_MIN of a failed refresh.
janitor_lane_report_refresh() {
  local age_min fail_min
  [ -n "${LANE_REPORT_REFRESHED:-}" ] && return 0
  LANE_REPORT_REFRESHED=1
  age_min=$(janitor_file_age_min "$LANE_REPORT")
  fail_min=$(janitor_file_age_min "$LANE_DOCTOR_FAIL")
  if { [ "$age_min" -gt "$LANE_REPORT_MAX_MIN" ] || [ "$age_min" -lt 0 ]; } && [ "${lean:-0}" != 1 ] \
     && [ -f "$AFC_SCRIPTS/fleet_lanes/doctor.py" ] \
     && { [ "$fail_min" -gt "$LANE_DOCTOR_RETRY_MIN" ] || [ "$fail_min" -lt 0 ]; }; then
    janitor_heartbeat "lane-doctor"
    # -B: never write __pycache__ into the human integration tree.
    # shellcheck disable=SC2086
    janitor_watchdog 300 "lane-doctor" -- bash -c "cd '$AFC_SCRIPTS' && exec python3 -B -m fleet_lanes.doctor --json --gh-limit 1000 --write '$LANE_REPORT' $LANE_DOCTOR_EXTRA_ARGS >/dev/null" >/dev/null 2>&1
    if [ "$?" = 0 ]; then rm -f "$LANE_DOCTOR_FAIL"; else : > "$LANE_DOCTOR_FAIL"; fi
  fi
}
# 0 = the saved doctor report lets this lane under $LANES_ROOT retire NOW.  Usage: janitor_lane_may_retire <wt> <sha>.
# The cleaner contract (scripts/fleet_lanes/README.md): schema >= 2, lsof "ok", generated_at within
# LANE_REPORT_MAX_MIN and not in the future (and the file mtime too), act only on cleaner_candidates, joined to its
# checkouts entry by realpath, gh_repos[owner_repo] "ok".  On top of the contract: the entry must be THIS directory
# (device + inode, so letter case, symlinks and a newline in some other path cannot steer the join), its head_sha
# must be the HEAD git reports now (a commit made after the report approves nothing), and pr_state MERGED only.
janitor_lane_may_retire() {
  local wt="$1" sha="$2" age_min
  [ -n "$sha" ] || return 1
  janitor_lane_report_refresh
  age_min=$(janitor_file_age_min "$LANE_REPORT")
  [ "$age_min" -ge 0 ] 2>/dev/null && [ "$age_min" -le "$LANE_REPORT_MAX_MIN" ] || return 1
  python3 - "$LANE_REPORT" "$wt" "$sha" "$STALE_DAYS" "$LANE_REPORT_MAX_MIN" >/dev/null 2>&1 <<'PY'
import datetime, json, os, sys
report, wt, sha = sys.argv[1], sys.argv[2], sys.argv[3]
min_days, max_min = float(sys.argv[4]), float(sys.argv[5])

def num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)

with open(report) as fh:
    r = json.load(fh)
if not isinstance(r, dict) or not num(r.get("schema")) or r["schema"] < 2 or r.get("lsof") != "ok":
    sys.exit(1)
ga = r.get("generated_at")
if not isinstance(ga, str):
    sys.exit(1)
if ga.endswith("Z"):
    ga = ga[:-1] + "+00:00"   # /usr/bin/python3 is 3.9: fromisoformat does not take "Z"
t = datetime.datetime.fromisoformat(ga)
if t.tzinfo is None:
    sys.exit(1)
age = (datetime.datetime.now(datetime.timezone.utc) - t).total_seconds()
if not 0 <= age <= max_min * 60:
    sys.exit(1)
cands, rows, gh = r.get("cleaner_candidates"), r.get("checkouts"), r.get("gh_repos")
if not isinstance(cands, list) or not isinstance(rows, list) or not isinstance(gh, dict):
    sys.exit(1)
me = os.stat(wt)
hits = 0
for cand in cands:
    if not isinstance(cand, str) or any(ch in cand for ch in "\n\r\0"):
        continue
    try:
        st = os.stat(cand)
    except OSError:
        continue
    if (st.st_dev, st.st_ino) != (me.st_dev, me.st_ino):
        continue
    match = [c for c in rows if isinstance(c, dict) and c.get("realpath") == cand]
    if len(match) != 1:
        continue
    c = match[0]
    if (c.get("kind") == "LINKED-WORKTREE" and c.get("registered") is True
            and c.get("safety") == "SAFE-TO-REMOVE" and c.get("pr_state") == "MERGED"
            and c.get("head_sha") == sha
            and c.get("janitor_keep") is False and c.get("active") is False and c.get("cwd_procs") == []
            and not c.get("read_errors") and c.get("tool_cache") is not True
            and num(c.get("lane_age_days")) and c["lane_age_days"] >= min_days
            and num(c.get("idle_days")) and c["idle_days"] >= min_days
            and isinstance(c.get("owner_repo"), str) and gh.get(c["owner_repo"]) == "ok"):
        hits += 1
sys.exit(0 if hits == 1 else 1)
PY
}

# Retire OLD, merged, CLEAN, idle linked worktrees (the checkout dir only; branch and commits stay).  Reads REPOS,
# KEEP_RE, STALE_DAYS, WT_REAP_DRYRUN, LOG, now; sets n_reap.  The fetch/prune that keeps origin/main fresh runs
# before this in the main flow.  Every gate below is a reason to KEEP; see the criteria list above the call.
janitor_retire_worktrees() {
  local wt sha br locked lane_gate base merged href brn r rc stale_min=$(( STALE_DAYS * 1440 ))
  n_reap=0
  CWD_SNAP_DONE=""
  while IFS=$'\t' read -r wt sha br locked; do
    [ -n "$wt" ] && [ -d "$wt" ] || continue
    janitor_keep_match "$wt" && continue                                            # standing lane / primary (either spelling)
    [ -n "$locked" ] && continue                                                    # git-locked worktree
    [ -e "$wt/.janitor-keep" ] && continue                                          # explicit opt-out
    janitor_wt_too_young "$wt" && continue                                          # BORN < STALE_DAYS ago -> keep (0-ahead is not "merged" for a fresh lane)
    [ -n "$(wt_blocking_dirt "$wt")" ] && continue                                  # real dirt (or git status failed) -> keep
    # Always require idle.  Skipping this (force_stale) deleted a checkout
    # on the next 30-min tick after a clean commit — including ST #3044
    # (branch cursor/kimi-audit-def) and in-session .claude/.grok worktrees.
    [ -n "$(find "$wt" -type f -not -path '*/.git' -not -path '*/.git/*' \
              -not -path '*/node_modules/*' -not -path '*/.next/*' \
              -mmin -$stale_min -print -quit 2>/dev/null)" ] && continue            # active within STALE_DAYS -> keep
    janitor_has_inner_checkout "$wt" && continue                                    # a checkout inside it would go with it
    janitor_wt_busy "$wt" && continue                                               # some process has it as cwd (or lsof failed) -> keep
    lane_gate=0
    if janitor_is_nested_lane "$wt"; then                                           # ~/apps/lanes/**: doctor-gated, fail closed
      janitor_lane_may_retire "$wt" "$sha" || continue
      lane_gate=1
    fi
    base=$(git -C "$wt" rev-parse --abbrev-ref origin/HEAD 2>/dev/null); base=${base:-origin/main}
    merged=no
    git -C "$wt" merge-base --is-ancestor HEAD "$base" 2>/dev/null && merged=yes
    # Squash-merge rewrites SHAs: ancestry fails even when the PR is MERGED.
    # `gh pr list --head` is the authoritative check (board 059f65b3).
    if [ "$merged" = no ] && janitor_pr_merged "$wt" "$br" "$sha"; then
      merged=yes
    fi
    if [ "$merged" = no ]; then
      href=$(git -C "$wt" symbolic-ref -q HEAD 2>/dev/null)
      [ -n "$href" ] && [ "$(git -C "$wt" for-each-ref --format='%(upstream:track)' "$href" 2>/dev/null)" = "[gone]" ] && merged=yes
    fi
    # Retired-KIMI seat / nested scratch / tmp: eligible when idle even if
    # unmerged.  Seat match only — not substring "kimi".
    if [ "$merged" = no ] && janitor_is_retired_kimi_or_scratch "$wt" "$br" && janitor_head_reachable "$wt"; then
      merged=yes
    fi
    [ "$lane_gate" = 1 ] && merged=yes                                              # doctor bound to this dir + this HEAD (lanes/ only path to retire)
    [ "$merged" = yes ] || continue                                                 # unmerged & not gone -> keep
    # Right before removing: a FRESH cwd snapshot (the first one may predate a 300 s doctor refresh) and HEAD unmoved.
    janitor_cwd_snapshot
    janitor_wt_busy "$wt" && continue
    [ "$(git -C "$wt" rev-parse HEAD 2>/dev/null)" = "$sha" ] || continue
    brn=${br#refs/heads/}
    if [ "${WT_REAP_DRYRUN:-0}" = "1" ]; then
      printf '%s  WOULD-RETIRE worktree %s (branch=%s)\n' "$now" "$wt" "${brn:-detached}" >> "$LOG"
      n_reap=$(( n_reap + 1 )); continue
    fi
    for r in "${REPOS[@]}"; do
      # Bound git worktree remove at 30s per worktree (BF-HOUSEKEEPER 2026-09-25).
      # A hung `rm -rf` inside a worktree's checkout dir can pin the tick forever;
      # the watchdog kills the git and logs a timeout, leaving the next tick to retry.
      janitor_watchdog 30 "wt-retire-rm" -- git -C "$r" worktree remove "$wt" 2>/dev/null
      rc=$?
      if [ "$rc" = "0" ]; then
        n_reap=$(( n_reap + 1 ))
        printf '%s  RETIRED worktree %s (branch=%s, merged/gone)\n' "$now" "$wt" "${brn:-detached}" >> "$LOG"
        break
      elif [ "$rc" = "124" ]; then
        printf '%s  RETIRE-SKIP worktree %s (watchdog timeout)\n' "$now" "$wt" >> "$LOG"
        break
      fi
    done
  done < <(
    for r in "${REPOS[@]}"; do
      git -C "$r" worktree list --porcelain 2>/dev/null | awk '
        /^worktree /{ l=$0; sub(/^worktree /,"",l); wt=l; sha=""; br=""; lk="" }
        /^HEAD /{ sha=$2 }
        /^branch /{ l=$0; sub(/^branch /,"",l); br=l }
        /^locked/{ lk="locked" }
        /^$/{ if(wt!=""){ print wt"\t"sha"\t"br"\t"lk; wt="" } }
        END{ if(wt!=""){ print wt"\t"sha"\t"br"\t"lk } }'
    done | sort -u
  )
}

# Reap node_modules/.next/.turbo of long-idle CLEAN checkouts (reversible: `npm ci` rebuilds; source and branch stay).
# Every "cannot tell" is a skip: a failed git status, a killed or failed activity scan, a failed lsof, a pgrep error,
# a failed rev-parse or ls-files.  Reads REPOS, KEEP_RE, IDLE_HRS, STALE_DAYS.
janitor_dep_reap_worktrees() {
  local idle_min=$(( IDLE_HRS * 60 ))
  CWD_SNAP_DONE=""   # a fresh lsof cwd snapshot for this phase
  { for r in "${REPOS[@]}"; do git -C "$r" worktree list --porcelain 2>/dev/null | awk '/^worktree /{sub(/^worktree /,""); print}'; done; } | sort -u | while IFS= read -r wt; do
    [ -d "$wt" ] || continue
    janitor_keep_match "$wt" && continue
    [ -e "$wt/.janitor-keep" ] && continue                                   # explicit opt-out (was ignored by this phase)
    janitor_is_nested_lane "$wt" && janitor_wt_too_young "$wt" && continue   # fresh lane under ~/apps/lanes keeps its deps
    [ -n "$(wt_blocking_dirt "$wt")" ] && continue                           # real dirt (or git status failed) -> skip
    janitor_argv_busy "$wt" && continue                                      # a process names it in argv (or pgrep failed)
    janitor_wt_busy "$wt" && continue                                        # a process has it as cwd (or lsof failed)
    # BF-HOUSEKEEPER 2026-09-25: bound the per-worktree activity scan at 15s.
    # A huge worktree (especially one with deep node_modules trees excluded via
    # -not -path) used to pin the tick here for minutes with 0 CPU.
    activity=$(janitor_watchdog "$JANITOR_SCAN_SECS" "wt-activity-scan" -- sh -c 'find "$1" -type f -not -path "*/.git/*" -not -path "*/node_modules/*" -not -path "*/.next/*" -mmin -"$2" -print -quit 2>/dev/null' _ "$wt" "$idle_min")
    act_rc=$?                                  # must be the first statement after the assignment above
    [ "$act_rc" = 0 ] || continue              # killed (124) or find error (an unreadable dir may hold the fresh file) = ACTIVE
    if [ -n "$activity" ]; then continue; fi  # active -> skip
    top=$(git -C "$wt" rev-parse --show-toplevel 2>/dev/null) || continue
    # Right before deleting: a FRESH cwd snapshot and argv check (the phase snapshot is minutes old by now).
    janitor_cwd_snapshot
    janitor_wt_busy "$wt" && continue
    janitor_argv_busy "$wt" && continue
    while IFS= read -r d; do
      # find also walks into a checkout nested in this one (<lane>/.claude/worktrees/<session>, gitignored here): its
      # deps, its tracked vendored files and its own .janitor-keep belong to IT.  Only reap dirs of this checkout.
      dtop=$(git -C "$(dirname "$d")" rev-parse --show-toplevel 2>/dev/null) || continue
      [ "$dtop" = "$top" ] || continue
      rel="${d#"$wt"/}"
      # A dir literally named node_modules/.next/.turbo can still hold TRACKED files
      # (e.g. a vendored dep committed under app/vendor/node_modules) -- git status
      # ignores it when unmodified, so wt_blocking_dirt above never sees it. A blind
      # rm -rf here once deleted a tracked vendored tree in a Congress.Trade worktree
      # (2026-09-08). Refuse anything git still tracks under this path.  Only rc 1 means
      # "not tracked": rc 128 (corrupt index, git killed) used to read as untracked too.
      git -C "$wt" ls-files --error-unmatch -- "$rel" >/dev/null 2>&1
      [ "$?" = 1 ] || continue
      # Bound the rm at 60s per dep-dir (BF-HOUSEKEEPER 2026-09-25). A large
      # node_modules tree can be many GB; if rm itself wedges on a slow disk
      # we kill it and leave the dir for the next tick.
      janitor_watchdog 60 "wt-dep-rm" -- rm -rf -- "$d"
    done < <(find "$wt" -type d \( -name node_modules -o -name .next -o -name .turbo \) -prune 2>/dev/null)
  done
}
# ==== lanes-guard END ====

if [ "${JANITOR_LIB_ONLY:-0}" = "1" ]; then
  return 0 2>/dev/null || exit 0
fi

mkdir -p "$DIR"
# True when <pid> is a live janitor process.  After a 2h-old lock the pid may have been
# recycled; never tree-kill an unrelated process that merely inherited the number.
janitor_is_holder() {
  kill -0 "$1" 2>/dev/null && pgrep -f 'janitor\.sh' 2>/dev/null | grep -qx "$1"
}
# single-run lock with PID-aware + heartbeat self-heal (BF-HOUSEKEEPER 2026-09-25).
# mtime-only steal left a wedged pid 42941 wedging every tick from 18:22 onward
# (log silent since 15:39, ~0 CPU, lock held). Round 1 added kill -0 + 2h age.
# Round 2 (BF-FIXER 2026-09-25 follow-up): lead 78392 held the lock for 1h+
# at 0.05s CPU with child 79875 → grandchild 80247 still alive -- PID alive
# AND lock_age < 2h, so round 1 self-heal bowed out and let the wedge persist
# for 5 hours (every 30-min tick exited 0 at the lock block, log silent).
# Fix: $LOCK/heartbeat (epoch seconds, updated by janitor_heartbeat before each
# major phase) detects live-but-stuck holders. If existing_pid is alive but
# $LOCK/heartbeat is older than HEARTBEAT_STUCK_SECS, the holder is wedged --
# steal and log it. 90s covers any healthy single run; the always-pmlogs +
# always-wtprune + always-tmptestdb phases each heartbeat, so an early-phase
# wedge is caught within ~90s by the next launchd tick instead of 2h.
if ! mkdir "$LOCK" 2>/dev/null; then
  existing_pid=""
  [ -f "$LOCK/pid" ] && existing_pid=$(cat "$LOCK/pid" 2>/dev/null | tr -d '[:space:]')
  lock_age=$(( $(date +%s) - $(stat -f %m "$LOCK" 2>/dev/null || echo 0) ))
  hb_age=$lock_age
  [ -f "$LOCK/heartbeat" ] && hb_age=$(( $(date +%s) - $(cat "$LOCK/heartbeat" 2>/dev/null | tr -d '[:space:]') ))
  steal=0; steal_reason=""
  if [ -n "$existing_pid" ] && ! kill -0 "$existing_pid" 2>/dev/null; then
    steal=1; steal_reason="stale pid $existing_pid (dead), lock_age=${lock_age}s"
  elif [ -n "$existing_pid" ] && [ "$hb_age" -gt 90 ]; then
    steal=1; steal_reason="stuck live pid $existing_pid (heartbeat age ${hb_age}s > 90s), lock_age=${lock_age}s"
  elif [ "$lock_age" -gt 7200 ]; then
    steal=1; steal_reason="lock age ${lock_age}s > 7200s"
  fi
  if [ "$steal" = "1" ]; then
    printf '%s  LOCK self-heal: %s, stealing lock\n' \
      "$(date '+%Y-%m-%d %H:%M')" "$steal_reason" >> "$LOG"
    # Try a graceful kill of the wedged holder (round-1 evidence: even SIGTERM
    # to a stuck child doesn't always release the lock dir, but a final SIGKILL
    # on the holder is safe -- we own the next tick).
    # 2026-09-30 (Claude): kill the holder's whole process tree, not just its pid -- a bare
    # kill of the holder orphans its git status/find/gh children to launchd, where they keep
    # running.  Skipped when the pid is no longer a janitor process (recycled pid).
    if [ -n "$existing_pid" ] && janitor_is_holder "$existing_pid"; then
      janitor_kill_tree "$existing_pid"
    fi
    rm -rf "$LOCK" 2>/dev/null || true
    mkdir "$LOCK" 2>/dev/null || exit 0
  else
    exit 0
  fi
fi
printf '%s\n' "$$" > "$LOCK/pid"
printf '%s\n' "$(date +%s)" > "$LOCK/heartbeat"
trap 'rm -rf "$LOCK" 2>/dev/null' EXIT

# First heartbeat so a silent log between acquire and "pmlogs" provably means
# a hang inside freek/duk (now bounded) -- not a missing heartbeat path.
janitor_heartbeat "lock-acquired"

# Cheap, pure-waste, regenerable truncations: one bounded directory each, no fan-out over
# repos or worktrees, no `du` of big trees, no network.  Shared by the normal run (below)
# and by the PRESSURE-SKIP path, which runs nothing else.  Appends to the global $actions.
janitor_cheap_truncations() {
  # --- cap runaway pm2 logs ---
  janitor_heartbeat "pmlogs"
  local n_trunc n_tdb UT
  n_trunc=$(find "$HOME_DIR/.pm2/logs" -type f -name '*.log' -size +${PM2_LOG_CAP_MB}M 2>/dev/null | wc -l | tr -d ' ')
  if [ "${n_trunc:-0}" -gt 0 ]; then
    find "$HOME_DIR/.pm2/logs" -type f -name '*.log' -size +${PM2_LOG_CAP_MB}M -exec sh -c ': > "$1"' _ {} \; 2>/dev/null
    actions="${actions}pm2logs "
  fi
  # --- reap leftover vitest temp SQLite DBs (pure waste; grew to 130 GB once) ---
  # Every `npm test` run writes per-test-file temp DBs (agentic-*.db/-wal/-shm) into the
  # user temp dir and never deletes them; the fleet runs the suite constantly. 6h age
  # filter keeps any live/recent test run untouched. getconf resolves the per-login temp
  # dir, so this works on both the CLAUDE and MONET macOS accounts.
  janitor_heartbeat "tmptestdb"
  UT="$(getconf DARWIN_USER_TEMP_DIR 2>/dev/null | sed 's:/*$::')"
  if [ -n "$UT" ] && [ -d "$UT" ]; then
    n_tdb=$(find "$UT" -maxdepth 1 -name 'agentic-*' -mmin +360 2>/dev/null | wc -l | tr -d ' ')
    if [ "${n_tdb:-0}" -gt 0 ]; then
      find "$UT" -maxdepth 1 -name 'agentic-*' -mmin +360 -delete 2>/dev/null
      actions="${actions}tmp-testdb(${n_tdb}) "
    fi
  fi
}

# 2026-09-30 (Claude): memory/load pressure gate, evaluated right after the lock is taken.
# This 16 GiB Mac sat at load 200-990 with swap 90-98% full for days, and every 30-min tick
# under LOW_FREE/PRESSURE_FREE ran the full git fetch x12 plus per-worktree git status,
# find, merge-base and gh fan-out back to back (one tick ran 3h07m with repeated WATCHDOG
# kills) -- the janitor was adding to the thrash it exists to relieve.  Same rule as the
# housekeeper skill: load1 > JANITOR_MAX_LOAD or swap >= JANITOR_MAX_SWAP_PCT means
# thrashing, so do the cheap truncations only and skip every git/gh/find-over-worktrees
# phase, the du buckets, and all CleanMyMac calls.  The next tick re-evaluates, so full
# runs resume on their own once the Mac settles.  Nothing here reads the repos.
janitor_load1() { sysctl -n vm.loadavg 2>/dev/null | awk '{print $2}'; }
# vm.swapusage: "total = 10240.00M  used = 9401.44M  free = 838.56M  (encrypted)" -> used%
# (units can be K/M/G; total=0 means no swap -> 0%).
janitor_swap_pct() {
  sysctl -n vm.swapusage 2>/dev/null | awk '
    function mb(v,   u, n) { u = substr(v, length(v)); n = substr(v, 1, length(v) - 1) + 0
      if (u == "G") n *= 1024; else if (u == "K") n /= 1024; return n }
    { for (i = 1; i <= NF; i++) { if ($i == "total") t = mb($(i + 2)); if ($i == "used") u = mb($(i + 2)) } }
    END { if (t > 0) printf "%d\n", (u * 100) / t + 0.5; else print 0 }'
}
load1=$(janitor_load1); load1=${load1:-0}
swap_pct=$(janitor_swap_pct); swap_pct=${swap_pct:-0}
# Hard stop: genuine crisis, cheap truncations only, exit (EXIT trap releases the lock).
if awk -v l="$load1" -v m="$JANITOR_MAX_LOAD" 'BEGIN { exit !(l + 0 > m + 0) }' \
   || [ "$swap_pct" -ge "$JANITOR_MAX_SWAP_PCT" ] 2>/dev/null; then
  printf '%s  PRESSURE-SKIP load=%s swap=%s%% (cheap truncations only)\n' \
    "$(date '+%Y-%m-%d %H:%M')" "$load1" "$swap_pct" >> "$LOG"
  actions=""
  janitor_cheap_truncations
  [ -n "$actions" ] && printf '%s  PRESSURE-SKIP action=%s\n' "$(date '+%Y-%m-%d %H:%M')" "$actions" >> "$LOG"
  tail -n 500 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG"
  exit 0   # EXIT trap releases the lock
fi

# Lean mode (2026-10-02, MINIMAX): elevated but not critical.  Do NOT stand down --
# that is the catch-22.  Keep the reclaiming phases (worktree prune + retirement) and
# drop only the read-heavy du buckets and the cache sweeps, which add I/O to a host
# that is already loaded without freeing anything themselves.  See the thresholds
# block above for the full rationale.
lean=0
if awk -v l="$load1" -v m="$JANITOR_SOFT_LOAD" 'BEGIN { exit !(l + 0 >= m + 0) }' \
   || [ "$swap_pct" -ge "$JANITOR_SOFT_SWAP_PCT" ] 2>/dev/null; then
  lean=1
  printf '%s  LEAN-MODE load=%s swap=%s%% (du probes skipped; all reclaim phases still run)\n' \
    "$(date '+%Y-%m-%d %H:%M')" "$load1" "$swap_pct" >> "$LOG"
fi

now="$(date '+%Y-%m-%d %H:%M')"

duk() { local k; k=$(du -sk "$1" 2>/dev/null | awk '{print $1}'); echo "${k:-0}"; }  # KiB, 0 if absent

janitor_read_free   # free_k, free, FREE_KNOWN (0 = df failed with no prior reading: free-gated phases skipped)
prev_free=$(sed -n 's/^free=//p' "$STATE" 2>/dev/null); prev_free=${prev_free:-$free}
delta=$(( free - prev_free )); [ "$FREE_KNOWN" = 1 ] || delta=0
# STALE_DAYS floor is 7 days even under low disk pressure.  See docs/HOUSEKEEPER.md.

# cheap bucket sizes (bounded dirs only — keeps the run brief).
# BF-FIXER 2026-09-25 follow-up: each `du -sk` runs through `janitor_watchdog`
# (10s budget) so a stuck filesystem or a giant symlink loop in ~/.npm /
# ~/.cache/uv can't pin the tick before any phase heartbeat is written.
# A timeout returns 0 KiB for that bucket and the rest of the tick continues.
janitor_duk() {
  # usage: janitor_duk <secs> <label> <path>
  local secs="$1" label="$2" path="$3" out
  out=$(janitor_watchdog "$secs" "$label" -- du -sk "$path" 2>/dev/null) || out=""
  if [ -z "$out" ]; then echo 0; else echo "$out" | awk '{print $1}'; fi
}
# 2026-10-02 (MINIMAX): in lean mode these five `du` probes are skipped and reported
# as 0.  They are read-only size probes -- they free nothing themselves, and on a
# loaded host five 10s sweeps are exactly the I/O the lean arm exists to avoid.
# Every phase that actually reclaims still runs, including lowfree/pressure, because
# those are gated on free disk rather than on load or swap -- under real disk
# pressure we WANT them to run, loaded or not.  See the LEAN-MODE note at the gate.
if [ "$lean" = "1" ]; then
  npm_k=0; uv_k=0; livec_k=0; codexc_k=0; pm2_k=0
else
  npm_k=$(janitor_duk 10 "duk-npm"    "$HOME_DIR/.npm")
  uv_k=$(janitor_duk 10 "duk-uv"     "$HOME_DIR/.cache/uv")
  livec_k=$(janitor_duk 10 "duk-livec" /Users/jay/apps/trading-live/.next/cache)
  codexc_k=$(janitor_duk 10 "duk-codexc" /Users/jay/apps/trading-codex/.next/cache)
  pm2_k=$(janitor_duk 10 "duk-pm2"    "$HOME_DIR/.pm2/logs")
fi

actions=""

# --- ALWAYS (cheap, pure waste): cap runaway pm2 logs + reap leftover vitest temp DBs ---
janitor_cheap_truncations

janitor_extend_repos      # REPOS = legacy list UNION fleet-apps.json (realpath de-duplicated)
janitor_protect_primaries # every repo root, as typed and as git reports it, joins KEEP_RE
# --- ALWAYS: tidy stale worktree registry ---
janitor_heartbeat "wtprune"
for r in "${REPOS[@]}"; do git -C "$r" worktree prune 2>/dev/null; done

# --- ALWAYS: retire OLD, fully-merged, CLEAN, idle worktrees (removes the checkout dir only) ---
# `git worktree remove` deletes ONLY the working directory. The branch ref and every commit it
# points to REMAIN in the repo, so nothing tracked is ever lost — at most a re-`git worktree add`
# is needed to get the checkout back. A worktree qualifies for retirement only if ALL hold:
#   * not a standing lane / primary repo (KEEP_RE) and not a locked worktree
#   * no <worktree>/.janitor-keep opt-out marker
#   * working tree CLEAN ignoring generated junk — no tracked modifications and no untracked
#     files beyond node_modules/.next/build receipts/logs (see wt_blocking_dirt)
#   * no non-generated file modified within STALE_DAYS (genuinely "old" / idle)
#   * its HEAD is already contained in origin's default branch (merge-base --is-ancestor), OR
#     GitHub has a MERGED PR for this head (squash-safe; ancestry lies after squash), OR
#     its upstream is [gone] (branch pushed then deleted on origin — the squash-merge signature)
#   * BORN (checkout dir and git admin dir) at least STALE_DAYS ago, no checkout nested inside it, no process
#     cwd in it (fresh lsof right before removal; a failed lsof keeps everything), HEAD unmoved since listing
#   * under ~/apps/lanes: ONLY a fresh lane doctor report bound to this directory and this HEAD (lanes-guard)
if [ "${REAP_WORKTREES:-0}" = "1" ]; then
  janitor_heartbeat "wt-retire-start"
  # Light refresh: fetch ONLY origin/main (keeps merge-base accurate) + prune stale
  # remote-tracking refs (marks squash-merged branches' upstreams [gone]). Avoids the
  # heavy all-branch `fetch --prune` that made ticks crawl on the big repo.
  # BF-FIXER 2026-09-25 follow-up: bound each fetch at 30s through the same
  # watchdog. Round-1 evidence: lead 78392 sat at 1h+ 0.05s CPU hung in a git
  # fetch on a slow network -- GIT_HTTP_LOW_SPEED_LIMIT fires on slow TRICKLE
  # but not on a stalled TCP socket. Total fetch budget: 12 repos x 30s = 6
  # min worst case (we still get merges from the repos that DID succeed).
  for r in "${REPOS[@]}"; do
    # Two separate watchdogs so each git is the direct child of the watchdog
    # (no bash-subshell-pid mismatch on SIGKILL). Prune is local + fast, so
    # a single 5s budget is plenty.
    janitor_watchdog 30 "wt-fetch"  -- git -C "$r" fetch origin main -q
    # BF-HOUSEKEEPER 2026-10-05: was 5s while the sibling `wt-fetch` above gets
    # 30s. `git remote prune` walks every remote ref, so on a big repo it
    # routinely exceeded 5s and the watchdog killed+retried it every launchd tick
    # (~6min) forever — pure I/O burn during swap episodes. Match wt-fetch.
    janitor_watchdog 30 "wt-prune"  -- git -C "$r" remote prune origin
  done
  janitor_retire_worktrees   # per-worktree gates and removal; sets n_reap (defined above the library-mode return)
  [ "$n_reap" -gt 0 ] && actions="${actions}wt-retire(${n_reap}) "
  janitor_heartbeat "wt-retire-done n_reap=${n_reap}"
fi

# --- LOW FREE: clear regenerable caches + prod/dev build caches ---
if [ "$FREE_KNOWN" = 1 ] && [ "$free" -lt "$LOW_FREE" ]; then
  janitor_heartbeat "lowfree-start"
  HOGHUNTER_CLEAN="/Users/jay/Code/HogHunter/scripts/hoghunter-clean"
  if [ -x "$HOGHUNTER_CLEAN" ]; then
    # 2026-09-30 (BF-HOUSEKEEPER): this ran the CleanMyMac CLI's dev/ai/trash
    # modules.  CleanMyMac is uninstalled -- a stale `cleanmymac-cli` row still
    # sits in `brew list`, but there is no Cellar dir and `command -v` exits 1,
    # so `command -v cleanmymac` was false and this whole block was a no-op
    # that still logged as if it had cleaned something.
    #
    # The Hog Hunter engine subsumes those three modules and adds the ones the
    # CLI never had.  Two of its rules matter most here:
    #   * `logs` TRUNCATES IN PLACE rather than unlinking.  The 2026-09-13 bug
    #     below (a live launchd log deleted mid-write) is structurally
    #     impossible now: truncating preserves the inode and the fd.
    #   * `snapshots` prunes stale APFS local snapshots, which on this Mac have
    #     been the single largest reclaim available.
    #
    # Tier gating is the engine's own: it reads swap/load/disk and drops to
    # safe-only when the band is tight.  `--band=full` here because this branch
    # only runs below LOW_FREE; the engine still refuses the semi-safe tier
    # itself if swap or load is elevated.  Bounded at 300s via the watchdog.
    janitor_watchdog 300 "hoghunter-clean" -- "$HOGHUNTER_CLEAN" --clean --band=full
    actions="${actions}hoghunter "

    # 2026-10-04 (MINIMAX, board e30a863e): CleanMyMac CLI is a FREE PUBLIC BETA
    # (MacPaw, v1.0.0) and is installed again, so the dev/ai/trash sweep is
    # restored next to the Hog Hunter engine rather than instead of it.  They
    # are complementary: Hog Hunter owns log truncation, APFS snapshot pruning
    # and the graded band; the CLI walks 21 dev cache trees (Homebrew, npm,
    # Yarn, pnpm, pip, Cargo, Go, CocoaPods, Docker, VS Code, JetBrains, Maven,
    # Gradle, Poetry, uv, Bun, Deno, mise) plus AI-tool junk.
    #
    # `--force` is REQUIRED here and is not a shortcut.  The review step is not
    # a safety gate in a non-interactive context: run without `--force` and with
    # stdin closed, the CLI scans, renders its review screen, reads EOF and
    # proceeds to delete anyway.  Verified directly on this Mac 2026-10-04
    # (3 items, 37 KB) with no TTY.  So the "review then confirm" model does not
    # exist for launchd, and `--force` is what makes the behaviour explicit
    # rather than accidentally interactive.  If that ever changes, this comment
    # is the thing to re-check.
    #
    # `junk` stays excluded on purpose: the 2026-09-13 bug was `junk` unlinking
    # a log that launchd still held open, after which the daemon wrote to an
    # unlinked inode and freed nothing.  The CLI has no in-place log truncation
    # rule, so Hog Hunter's `logs` rule remains the only safe path for logs.
    #
    # `optimize ram` stays structurally banned and is not even offered here.
    # It purges resident pages into swapfiles on the same APFS container as user
    # data, converting RAM pressure into disk consumption.  Measured on this Mac
    # during this change: swap was already at 94% used (13.4 of 14 GB) with the
    # data volume at 97%.  Running it under those conditions makes the disk
    # problem worse, not better.
    if command -v cleanmymac >/dev/null 2>&1; then
      for cmm_mod in dev ai trash; do
        janitor_watchdog 120 "cleanmymac-$cmm_mod" -- cleanmymac clean "$cmm_mod" --force
      done
      actions="${actions}cleanmymac "
    else
      printf '%s  CMM-SKIP cleanmymac not on PATH\n' "$(date '+%Y-%m-%d %H:%M')" >> "$LOG"
    fi
  fi
  rm -rf "$HOME_DIR/.npm/_cacache" 2>/dev/null
  # uv cache: never rm -rf the whole tree. Several MCP servers (Hetzner, alpaca, fmp,
  # sentry, pinecone, uptimerobot, coolify) are launched via `uvx`, which keeps that
  # process's venv under .cache/uv/archive-v0/<hash>/ for as long as it runs. Wiping
  # the cache out from under a live one strands it (2026-09-08: Hetzner MCP lost its
  # CA bundle mid-session; alpaca/fmp died CONNECTION_CLOSED). Even `uv cache prune`
  # (reachability-based) can drop an in-use archive-v0 entry a running tool has not
  # yet lazy-imported from, so the busy check below gates BOTH prune and the more
  # destructive `uv cache clean` (CRIT_FREE only) -- neither runs while anything
  # holds the cache.
  uv_cache="$HOME_DIR/.cache/uv"
  if [ -d "$uv_cache" ]; then
    uv_branch=none
    # BF-HOUSEKEEPER 2026-09-25: replace `lsof +D "$uv_cache"` (recursive; known
    # to hang >60s on big trees like node_modules) with per-entry `lsof <exact
    # path>` against each archive-v0 entry. The live MCP venvs Housekeeper listed
    # (Hetzner, alpaca, fmp, sentry, pinecone, uptimerobot, coolify) live there.
    # Bound each lsof at 4s; a hung scan counts as "busy" so we skip safely.
    lsof_n=0
    if [ -d "$uv_cache/archive-v0" ]; then
      while IFS= read -r entry; do
        [ -e "$entry" ] || continue
        lsof_one="$(mktemp -t uvlsof1 2>/dev/null || echo "/tmp/uvlsof1.$$")"
        ( lsof "$entry" -t > "$lsof_one" 2>/dev/null ) &
        lsof_pid=$!
        lsof_waited=0
        while kill -0 "$lsof_pid" 2>/dev/null && [ "$lsof_waited" -lt 4 ]; do
          sleep 1; lsof_waited=$((lsof_waited + 1))
        done
        if kill -0 "$lsof_pid" 2>/dev/null; then
          kill -9 "$lsof_pid" 2>/dev/null
          printf '%s  uv cache lsof entry timed out after 4s, assuming busy: %s\n' "$now" "$entry" >> "$LOG"
          rm -f "$lsof_one"
          lsof_n=$((lsof_n + 1))   # treat as busy
          continue
        fi
        n=$(wc -l < "$lsof_one" 2>/dev/null | tr -d ' ')
        rm -f "$lsof_one"
        lsof_n=$((lsof_n + ${n:-0}))
      done < <(find "$uv_cache/archive-v0" -maxdepth 1 -mindepth 1 2>/dev/null)
    fi
    uv_busy=$(( $(pgrep -f 'cache/uv/archive-v0' 2>/dev/null | wc -l | tr -d ' ') + ${lsof_n:-0} ))
    if [ "${uv_busy:-0}" -gt 0 ]; then
      printf '%s  uv cache in use by %s processes, skipped\n' "$now" "$uv_busy" >> "$LOG"
      uv_branch=skip-busy
    elif command -v uv &>/dev/null; then
      uv cache prune >/dev/null 2>&1 && { actions="${actions}uv-prune "; uv_branch=prune; }
      if [ "$free" -lt "$CRIT_FREE" ]; then
        uv cache clean >/dev/null 2>&1 && { actions="${actions}uv-clean "; uv_branch="${uv_branch}+clean"; }
      fi
    elif [ "$free" -lt "$CRIT_FREE" ]; then
      rm -rf "$uv_cache" 2>/dev/null && { actions="${actions}uv-rm-legacy "; uv_branch=rm-legacy-no-cli; }
    fi
    printf '%s  uv-cache branch=%s free=%sG crit=%sG\n' "$now" "$uv_branch" "$free" "$CRIT_FREE" >> "$LOG"
  fi
  rm -rf "$HOME_DIR/Library/Caches/ms-playwright/"* 2>/dev/null
  rm -rf "$HOME_DIR/.cache/chrome-devtools-mcp" 2>/dev/null
  rm -rf "$HOME_DIR/Library/Developer/CoreSimulator/Caches"/* 2>/dev/null
  rm -rf /Users/jay/apps/trading-live/.next/cache 2>/dev/null
  rm -rf /Users/jay/apps/trading-codex/.next/cache 2>/dev/null
  actions="${actions}caches "
  janitor_heartbeat "lowfree-done"
fi

# --- PRESSURE: reap deps on long-idle CLEAN worktrees (reversible; keeps source+branch) ---
if [ "$FREE_KNOWN" = 1 ] && [ "$free" -lt "$PRESSURE_FREE" ]; then
  janitor_heartbeat "pressure-start"
  # BF-FIXER 2026-09-25 follow-up: bound the fetch loop at 30s/repo (same
  # rationale as wt-retire-start). Without this a single stalled network
  # fetch pinned the launchd tick for an hour+.
  for r in "${REPOS[@]}"; do
    janitor_watchdog 30 "pressure-fetch" -- git -C "$r" fetch origin main -q
  done
  janitor_dep_reap_worktrees   # per-worktree gates and reap (defined above the library-mode return)
  actions="${actions}idle-dep-reap "
  janitor_heartbeat "pressure-done"
fi

[ -z "$actions" ] && actions="none"
after_k=$(freek); after=$(gib "$after_k")
reclaimed=0
[ -n "$after_k" ] && [ -n "$free_k" ] && reclaimed=$(( (after_k - free_k) / 1024 / 1024 ))
[ -n "$after_k" ] || after="?"   # unknown, never a fake 0 (gib "" is 0)

note=""
[ "$delta" -le "-$DROP_ALERT" ] && note=" DROP(${delta}G since last)"
printf '%s  free=%sG(Δ%+dG) npm=%sG uv=%sG live$=%sG codex$=%sG pm2=%sM  action=%s reclaimed=%sG%s\n' \
  "$now" "${free:-?}" "$delta" "$(gib $npm_k)" "$(gib $uv_k)" "$(gib $livec_k)" "$(gib $codexc_k)" "$(( pm2_k/1024 ))" "$actions" "$reclaimed" "$note" >> "$LOG"

# persist state + cap log to last 500 lines.  Only a real reading: an unknown one written as free=0 poisoned the
# fallback in freek() for every later tick.
[ -n "$after_k" ] && printf 'free=%s\nts=%s\n' "$after" "$now" > "$STATE"
tail -n 500 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG"

# BF-HOUSEKEEPER 2026-09-25: terminal heartbeat so a silent log between PHASE
# markers is provably a hang, not a quiet run.
printf '%s  PHASE done pid=%s free=%sG(Δ%+dG) action=%s reclaimed=%sG%s\n' \
  "$(date '+%Y-%m-%d %H:%M')" "$$" "$after" "$delta" "$actions" "$reclaimed" "$note" >> "$LOG"
