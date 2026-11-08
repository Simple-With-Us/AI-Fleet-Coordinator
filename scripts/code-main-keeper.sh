#!/bin/bash
# code-main-keeper: keeps the ~/Code mirror clones on origin/main, and tells the owner about everything else.  (AFC-618)
#
# ~/Code/<Repo> is a pure mirror.  The owner builds and reads from it;  no seat edits it, and all work happens in a lane
# (~/apps/lane new <app> <slug>).  This job is the mirror's only writer, and it writes in exactly one way:
#
#     git fetch origin          then          git merge --ff-only origin/main
#
# and only on a clone that is ALL of:  on the allowlist below, on `main`, with a clean working tree (no tracked change, no staged
# change, no untracked file), and whose HEAD is an ancestor of origin/main.  Anything else is logged and left exactly as found.
# It never resets, cleans, stashes, checks out, switches branches, prunes, deletes or discards anything, and a clone that is not on
# the allowlist is not even fetched.
#
# What it reports, one line each, to the log (`~/apps/logs/code-main-keeper.log`, unchanged):
#   SYNC   <repo>  ff-only +N -> <sha>                  moved
#   OK     <repo>  already at origin/main <sha>
#   HOLD   <repo>  <why>                                allowlisted, but not moved:  dirty, off main, local commits, fetch failed
#   NOTE   <repo>  <fact>                               not acted on:  dirty tree, branch, local-only branches, stashes,
#                                                       main ahead of origin/main, or `mirror only (not on the allowlist)`
#   STRAY-WORKTREE <name> ...                           a linked worktree or misplaced checkout directly under ~/Code (owner prunes)
#
# The allowlist is the fleet's own repos, the ones fleet-apps.json registers with an integration tree.  Anything else under ~/Code
# (a *-push clone, homebrew-tap, a scratch folder) is report only until someone adds it here on purpose.  The tests set
# CODE_MAIN_KEEPER_ALLOWLIST (space or newline separated names) to use a different list.
#
# Usage:  bash code-main-keeper.sh [--dry-run|-n]      (--dry-run logs what it would do and runs no fetch and no merge)
# Deploy:  the live copy is ~/apps/code-main-keeper.sh, run by pm2 through code-main-keeper-daemon.sh every 60 s.  Copy this file
# over it;  never edit the live copy in place.  Tests: scripts/fleet_lanes/tests/test_code_main_keeper.py.

set -uo pipefail

CODE_ROOT="${CODE_ROOT:-$HOME/Code}"
LOG_DIR="${CODE_MAIN_KEEPER_LOG_DIR:-$HOME/apps/logs}"
LOG="${LOG_DIR}/code-main-keeper.log"
STAMP="$(date '+%Y-%m-%d %H:%M:%S')"
DRY_RUN=0
if [ "${1:-}" = "--dry-run" ] || [ "${1:-}" = "-n" ]; then
  DRY_RUN=1
fi

# A status read must not take the index lock, so the keeper never races an agent's own git command.
export GIT_OPTIONAL_LOCKS=0

ALLOWLIST_DEFAULT=(
  "AI-Fleet-Coordinator"
  "Autorotate"
  "BotFleet"
  "Clutch"
  "CodeCaps"
  "Congress.Trade"
  "ContactLogo"
  "DealDex"
  "Fleet-OPS"
  "FleetLink"
  "HogHunter"
  "Personal-Site"
  "Simple-With-Us"
  "Socratic-Trade"
  "Usage-Monitor"
  "congress-trading-shared"
)
if [ -n "${CODE_MAIN_KEEPER_ALLOWLIST+x}" ]; then
  # shellcheck disable=SC2206  # names never hold a space or a glob;  a name that does is not on the list
  ALLOWLIST=(${CODE_MAIN_KEEPER_ALLOWLIST})
else
  ALLOWLIST=("${ALLOWLIST_DEFAULT[@]}")
fi

mkdir -p "$LOG_DIR"

log() {
  echo "$STAMP  $*" >> "$LOG"
}

on_allowlist() {
  local base="$1" n
  for n in "${ALLOWLIST[@]+"${ALLOWLIST[@]}"}"; do
    if [ "$base" = "$n" ]; then
      return 0
    fi
  done
  return 1
}

# Every immediate child of CODE_ROOT that is the primary root of a git work tree.
discover_repos() {
  REPOS=()
  local d top top_r d_r
  for d in "$CODE_ROOT"/*; do
    [ -d "$d" ] || continue
    git -C "$d" rev-parse --is-inside-work-tree >/dev/null 2>&1 || continue
    top="$(git -C "$d" rev-parse --show-toplevel 2>/dev/null || true)"
    [ -n "$top" ] || continue
    top_r="$(cd "$top" 2>/dev/null && pwd -P)" || continue
    d_r="$(cd "$d" 2>/dev/null && pwd -P)" || continue
    [ "$top_r" = "$d_r" ] || continue
    # A linked worktree (`.git` is a file) is a stray, reported below, never a mirror.
    [ -d "$d/.git" ] || continue
    REPOS+=("$d")
  done
}

# Report only.  A linked worktree or a checkout whose toplevel is somewhere else does not belong directly under ~/Code.
detect_stray_worktrees() {
  STRAY=()
  local d base parent_gitdir branch top top_r d_r
  for d in "$CODE_ROOT"/*; do
    [ -d "$d" ] || continue
    base="$(basename "$d")"
    git -C "$d" rev-parse --is-inside-work-tree >/dev/null 2>&1 || continue
    if [ -f "$d/.git" ]; then
      parent_gitdir="$(sed -n 's/^gitdir: //p' "$d/.git" 2>/dev/null | head -1)"
      branch="$(git -C "$d" rev-parse --abbrev-ref HEAD 2>/dev/null || echo 'detached')"
      log "STRAY-WORKTREE  $base  linked-worktree -> ${parent_gitdir:-unknown}  branch=${branch}  (owner review: git -C <parent> worktree remove --force $d)"
      STRAY+=("$d")
      continue
    fi
    top="$(git -C "$d" rev-parse --show-toplevel 2>/dev/null || true)"
    if [ -n "$top" ] && [ "$top" != "$d" ]; then
      top_r="$(cd "$top" 2>/dev/null && pwd -P)" || continue
      d_r="$(cd "$d" 2>/dev/null && pwd -P)" || continue
      if [ "$top_r" != "$d_r" ]; then
        branch="$(git -C "$d" rev-parse --abbrev-ref HEAD 2>/dev/null || echo 'detached')"
        log "STRAY-WORKTREE  $base  non-primary-toplevel ($top)  branch=${branch}  (owner review: rm -rf $d)"
        STRAY+=("$d")
      fi
    fi
  done
}

# Count of paths `git status` lists, untracked included.  A mirror with anything in it is not clean.
dirty_count() {
  git -C "$1" status --porcelain 2>/dev/null | grep -c . || true
}

# Branches under refs/heads that origin does not have, other than main: work that exists nowhere but this clone.
local_only_branches() {
  local d="$1" b out=""
  while IFS= read -r b; do
    [ -n "$b" ] || continue
    [ "$b" = "main" ] && continue
    if ! git -C "$d" show-ref --verify --quiet "refs/remotes/origin/$b" 2>/dev/null; then
      out="${out:+$out }$b"
    fi
  done < <(git -C "$d" for-each-ref --format='%(refname:short)' refs/heads 2>/dev/null)
  echo "$out"
}

# What the owner should know about a clone, whatever happens to it.  Acts on nothing.
report_facts() {
  local d="$1" name="$2" cur="$3" n branches stashes
  n="$(dirty_count "$d")"
  if [ "${n:-0}" -gt 0 ]; then
    log "NOTE  $name  dirty tree ($n path(s)); left alone"
  fi
  if [ "$cur" != "main" ]; then
    log "NOTE  $name  on '${cur:-detached}', not main; left alone"
  fi
  branches="$(local_only_branches "$d")"
  if [ -n "$branches" ]; then
    # A count and the first five names:  a busy clone holds hundreds, and a line of them buries the log.
    set -- $branches
    n=$#
    if [ "$n" -gt 5 ]; then
      log "NOTE  $name  $n local-only branch(es): $1 $2 $3 $4 $5 ..."
    else
      log "NOTE  $name  $n local-only branch(es): $branches"
    fi
  fi
  stashes="$(git -C "$d" stash list 2>/dev/null | grep -c . || true)"
  if [ "${stashes:-0}" -gt 0 ]; then
    log "NOTE  $name  $stashes stash(es)"
  fi
}

sync_one() {
  local d="$1" name cur
  name="$(basename "$d")"
  cur="$(git -C "$d" branch --show-current 2>/dev/null || true)"

  if ! on_allowlist "$name"; then
    log "NOTE  $name  mirror only (not on the allowlist); not fetched, not moved"
    report_facts "$d" "$name" "$cur"
    return 0
  fi

  report_facts "$d" "$name" "$cur"

  if [ "$cur" != "main" ]; then
    log "HOLD  $name  on '${cur:-detached}', not main"
    return 0
  fi
  if [ "$(dirty_count "$d")" -gt 0 ]; then
    log "HOLD  $name  dirty tree"
    return 0
  fi

  if [ "$DRY_RUN" -eq 1 ]; then
    log "DRY   $name  would fetch origin and ff-only main"
    return 0
  fi

  if ! git -C "$d" fetch origin --quiet 2>/dev/null; then
    log "HOLD  $name  git fetch failed"
    return 0
  fi
  if ! git -C "$d" rev-parse --verify --quiet origin/main >/dev/null 2>&1; then
    log "HOLD  $name  no origin/main"
    return 0
  fi

  local head remote ahead behind
  head="$(git -C "$d" rev-parse HEAD)"
  remote="$(git -C "$d" rev-parse origin/main)"
  if [ "$head" = "$remote" ]; then
    log "OK    $name  already at origin/main $(git -C "$d" rev-parse --short HEAD)"
    return 0
  fi
  if ! git -C "$d" merge-base --is-ancestor HEAD origin/main 2>/dev/null; then
    ahead="$(git -C "$d" rev-list --count origin/main..HEAD 2>/dev/null || echo '?')"
    log "HOLD  $name  local main has $ahead commit(s) not on origin/main; not touched"
    return 0
  fi

  behind="$(git -C "$d" rev-list --count HEAD..origin/main 2>/dev/null || echo '?')"
  if git -C "$d" merge --ff-only origin/main >/dev/null 2>&1; then
    log "SYNC  $name  ff-only +$behind -> $(git -C "$d" rev-parse --short HEAD)"
  else
    log "HOLD  $name  merge --ff-only refused; not touched"
  fi
}

# Low scheduling priority so interactive work wins.
if command -v renice >/dev/null 2>&1; then
  renice 15 $$ >/dev/null 2>&1 || true
fi

discover_repos
detect_stray_worktrees

log "---- run start dry_run=$DRY_RUN repos=${#REPOS[@]} allowlist=${#ALLOWLIST[@]} stray=${#STRAY[@]} root=$CODE_ROOT ----"
if [ "${#REPOS[@]}" -eq 0 ]; then
  log "WARN  no repos discovered under $CODE_ROOT"
else
  for repo in "${REPOS[@]}"; do
    sync_one "$repo"
  done
fi
log "---- run end ----"

exit 0
