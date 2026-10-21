#!/bin/bash
# Contract: mac-auto-cleanup must not force-reap live checkouts or simulators.
# #95 shipped three data-loss paths on a 4h launchd tick:
#   * rm -rf ~/.grok/worktrees/*  (in-session Grok checkouts)
#   * git worktree remove --force + grep -Fw "" on detached HEAD
#   * rm -rf CoreSimulator/Devices/*  (every simulator, not unavailable-only)
# Worktree retirement stays on disk-janitor (#93).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="${ROOT}/mac-auto-cleanup.sh"

fail() { echo "FAIL $*" >&2; exit 1; }

[ -f "$SCRIPT" ] || fail "missing $SCRIPT"

code_lines() { grep -nE "$1" "$SCRIPT" | grep -vE '^[^:]+:[[:space:]]*#' || true; }

if [ -n "$(code_lines 'CoreSimulator/Devices"/\*')" ]; then
  fail "must not rm -rf CoreSimulator/Devices/* (wipes every simulator)"
fi

if [ -n "$(code_lines 'simctl shutdown all')" ]; then
  fail "must not simctl shutdown all (kills every booted simulator on the 4h tick)"
fi

if [ -n "$(code_lines '\.grok/worktrees"/\*')" ]; then
  fail "must not rm -rf ~/.grok/worktrees/* (in-session Grok checkouts)"
fi

if [ -n "$(code_lines 'worktree remove --force')" ]; then
  fail "must not git worktree remove --force (deletes dirty trees)"
fi

if [ -n "$(code_lines 'grep -Fw')" ]; then
  fail "empty-string word match on detached HEAD matches every merged branch"
fi

if ! grep -q 'agent-sync-push' "$SCRIPT"; then
  fail "mac-auto-cleanup.sh must preserve agent-sync-push runtime"
fi

if ! grep -q 'is_git_worktree' "$SCRIPT" || ! grep -q 'wt_has_blocking_dirt' "$SCRIPT" || ! grep -q 'wt_is_active' "$SCRIPT"; then
  fail "mac-auto-cleanup.sh must enforce worktree, dirt, and idle checks before reaping dependencies"
fi

if ! grep -q 'cleanmymac' "$SCRIPT"; then
  fail "mac-auto-cleanup.sh must include cleanmymac CLI integration"
fi

if ! grep -q -- '--pressure' "$SCRIPT"; then
  fail "mac-auto-cleanup.sh must accept --pressure for resource-watch wakes"
fi

if ! grep -q 'name Journals' "$SCRIPT"; then
  fail "mac-auto-cleanup.sh must prune every PipelineStorage Journals dir, not only Urgent"
fi

# The housekeeper lock must be taken over atomically.
#
# Checking that a lock is stale and then removing it is two steps, and two
# reclaimers can interleave them: both see the stale lock, one removes it and
# creates a fresh one, and the other then removes that fresh, empty lock and
# creates its own, so both believe they hold it.  The removal is therefore a
# rename, which only one caller can win.  The Python lock
# (HogHunter scripts/vacuum/lock.py) and the Swift lock
# (HogHunter Sources/Storage/HousekeeperLock.swift) do it this way too, and
# the three have to agree: they share one lock directory.
#
# The guard has to cover EVERY spelling of a removal, not just `rmdir`.  An
# `rm -rf "$HOUSEKEEPER_LOCK"` is the same racy takeover with a different
# spelling, and it passes a guard that only greps for `rmdir`.  The only
# removal that is allowed is the ownership-checked release in the EXIT trap.
if [ -n "$(code_lines '(^|[[:space:];|])(rm|rmdir)[[:space:]][^|;]*\$HOUSEKEEPER_LOCK([^A-Za-z_]|$)' | grep -v 'trap ')" ]; then
  fail "mac-auto-cleanup.sh must not remove the canonical housekeeper lock path; take it over by moving it aside"
fi

if ! grep -q 'HOUSEKEEPER_STALE' "$SCRIPT"; then
  fail "mac-auto-cleanup.sh must take a stale housekeeper lock by moving it aside, so only one caller can win"
fi

# The staleness has to be decided AT THE CANONICAL PATH, before the lock is
# moved.  Moving a live peer's lock out of the way leaves the canonical path
# briefly absent, and a third reclaimer can mkdir it and sweep at the same
# time -- the double sweep this lock exists to prevent.  The invariant is
# therefore an ORDER, not just the presence of a staleness check.
STALE_CHECK_LINE="$(grep -nE 'find "\$HOUSEKEEPER_LOCK" -maxdepth 0 -mmin \+120' "$SCRIPT" | head -1 | cut -d: -f1)"
MOVE_LINE="$(grep -nE 'mv "\$HOUSEKEEPER_LOCK" "\$HOUSEKEEPER_STALE"' "$SCRIPT" | head -1 | cut -d: -f1)"
if [ -z "$STALE_CHECK_LINE" ] || [ -z "$MOVE_LINE" ]; then
  fail "mac-auto-cleanup.sh must check the lock's age and then move it aside"
fi
if [ "$STALE_CHECK_LINE" -ge "$MOVE_LINE" ]; then
  fail "mac-auto-cleanup.sh must check the lock's age at the canonical path BEFORE moving it aside"
fi

# A restore must be one atomic no-replace move.  A separate existence check
# and a plain `mv` let another owner create the lock path in between, after
# which the restore moves the peer's lock inside the new owner's lock and
# breaks its release.
if ! grep -q 'mv -n "\$HOUSEKEEPER_STALE" "\$HOUSEKEEPER_LOCK"' "$SCRIPT"; then
  fail "mac-auto-cleanup.sh must restore a moved-aside lock with one atomic no-replace move"
fi

# Only an EXISTING lock is peer contention.  A mkdir that fails for any other
# reason must not be reported as a successful skip.
if ! grep -q 'failed to create housekeeper lock' "$SCRIPT"; then
  fail "mac-auto-cleanup.sh must report a lock it could not create, not skip as if a peer held it"
fi

# Behavioural checks on the lock block itself.  The block is extracted from
# the script and run against a temporary HOME, so nothing real is touched.
LOCK_BLOCK="$(sed -n '/^HOUSEKEEPER_LOCK=/,/^fi$/p' "$SCRIPT")"
if [ -z "$LOCK_BLOCK" ]; then
  fail "could not extract the housekeeper lock block from mac-auto-cleanup.sh"
fi

LOCK_TEST_ROOT="$(mktemp -d)"
trap 'rm -rf "$LOCK_TEST_ROOT"' EXIT

run_lock_block() {
  # HOME points at a scratch tree; the block only touches the lock under it.
  # The block releases the lock on exit, so the outcome is reported from
  # inside the block's own lifetime, not after it.  The block's refusal paths
  # call `exit 0`, so they never reach the check below and print no LOCK_HELD
  # line at all -- that is how a refusal is recognised, together with the
  # 'skipped, peer holds housekeeper lock' message.
  HOME="$1" HOUSEKEEPER_LOCK_OWNER= bash -c "$LOCK_BLOCK"$'\n''[ -d "$HOUSEKEEPER_LOCK" ] && echo LOCK_HELD=yes'
}

run_lock_block_hold() {
  # The same block, but it keeps the lock for a moment after reporting, so a
  # concurrent reclaimer really does meet a held lock instead of a lock that
  # was already released.  The appended sleep runs before the EXIT trap, so
  # the lock is still held while it sleeps.
  HOME="$1" HOUSEKEEPER_LOCK_OWNER= bash -c "$LOCK_BLOCK"$'\n''[ -d "$HOUSEKEEPER_LOCK" ] && echo LOCK_HELD=yes'$'\n''sleep '"$2"
}

make_lock() {
  mkdir -p "$1/.claude-disk-janitor/.housekeeper.lock"
}

# 1. No lock: the block takes it.
H1="$LOCK_TEST_ROOT/case1"
mkdir -p "$H1"
OUT="$(run_lock_block "$H1" 2>&1 || true)"
if ! printf '%s' "$OUT" | grep -q 'LOCK_HELD=yes'; then
  fail "the housekeeper lock must be taken when it is free"
fi

# 2. A fresh lock held by a peer: refused, and the peer's lock is untouched.
H2="$LOCK_TEST_ROOT/case2"
mkdir -p "$H2"
make_lock "$H2"
OUT="$(run_lock_block "$H2" 2>&1 || true)"
if ! printf '%s' "$OUT" | grep -q 'skipped, peer holds housekeeper lock'; then
  fail "a fresh housekeeper lock must be refused with 'skipped, peer holds housekeeper lock'"
fi
if [ ! -d "$H2/.claude-disk-janitor/.housekeeper.lock" ]; then
  fail "a refused takeover must leave the peer's lock in place"
fi
if [ -n "$(find "$H2/.claude-disk-janitor" -maxdepth 1 -name '.housekeeper.lock.stale-*' 2>/dev/null)" ]; then
  fail "a refused takeover must not leave a moved-aside copy behind"
fi

# 3. A stale lock: taken over, and the moved-aside copy is gone.
H3="$LOCK_TEST_ROOT/case3"
mkdir -p "$H3"
make_lock "$H3"
touch -t 202001010000 "$H3/.claude-disk-janitor/.housekeeper.lock"
OUT="$(run_lock_block "$H3" 2>&1 || true)"
if ! printf '%s' "$OUT" | grep -q 'LOCK_HELD=yes'; then
  fail "a stale housekeeper lock must be taken over"
fi
if [ -n "$(find "$H3/.claude-disk-janitor" -maxdepth 1 -name '.housekeeper.lock.stale-*' 2>/dev/null)" ]; then
  fail "a taken-over stale lock must not leave a moved-aside copy behind"
fi

# 4. A stale lock with something in it is not the marker we made: put back.
H4="$LOCK_TEST_ROOT/case4"
mkdir -p "$H4"
make_lock "$H4"
echo "not ours" > "$H4/.claude-disk-janitor/.housekeeper.lock/leftover.txt"
touch -t 202001010000 "$H4/.claude-disk-janitor/.housekeeper.lock"
OUT="$(run_lock_block "$H4" 2>&1 || true)"
if ! printf '%s' "$OUT" | grep -q 'skipped, peer holds housekeeper lock'; then
  fail "a lock directory with contents in it must not be taken"
fi
if [ ! -f "$H4/.claude-disk-janitor/.housekeeper.lock/leftover.txt" ]; then
  fail "a lock directory with contents in it must be put back, not removed"
fi

# 5. Two reclaimers at once against one lock: exactly one of them may hold it.
#
# This is the property the whole lock exists for, so it has to be exercised
# with two real processes rather than asserted with a grep.  Both start at the
# same time against the same scratch HOME; the mkdir is atomic, so one wins
# the lock and the other must be refused.  The loser prints no LOCK_HELD line
# at all, because its refusal path exits before the check runs.
H5="$LOCK_TEST_ROOT/case5"
mkdir -p "$H5"
( run_lock_block_hold "$H5" 2 >"$H5/out1" 2>&1 ) &
P1=$!
( run_lock_block_hold "$H5" 2 >"$H5/out2" 2>&1 ) &
P2=$!
wait "$P1" "$P2"
HELD_COUNT="$(cat "$H5/out1" "$H5/out2" | grep -c 'LOCK_HELD=yes' || true)"
REFUSED_COUNT="$(cat "$H5/out1" "$H5/out2" | grep -c 'skipped, peer holds housekeeper lock' || true)"
if [ "$HELD_COUNT" != "1" ]; then
  fail "two concurrent reclaimers must produce exactly one LOCK_HELD=yes (got $HELD_COUNT)"
fi
if [ "$REFUSED_COUNT" != "1" ]; then
  fail "the losing reclaimer must be told a peer holds the lock (got $REFUSED_COUNT refusals)"
fi
if [ -n "$(find "$H5/.claude-disk-janitor" -maxdepth 1 -name '.housekeeper.lock.stale-*' 2>/dev/null)" ]; then
  fail "a concurrent takeover must not leave a moved-aside copy behind"
fi

# 6. A live peer's lock is never displaced from the canonical path.
#
# A stale-looking lock that is actually live must be left exactly where it is.
# The block decides staleness at the canonical path, so a lock that is fresh
# is refused without ever being moved.
H6="$LOCK_TEST_ROOT/case6"
mkdir -p "$H6"
make_lock "$H6"
BEFORE_INODE="$(stat -f %i "$H6/.claude-disk-janitor/.housekeeper.lock")"
OUT="$(run_lock_block "$H6" 2>&1 || true)"
if ! printf '%s' "$OUT" | grep -q 'skipped, peer holds housekeeper lock'; then
  fail "a live housekeeper lock must be refused"
fi
AFTER_INODE="$(stat -f %i "$H6/.claude-disk-janitor/.housekeeper.lock" 2>/dev/null || echo '')"
if [ "$BEFORE_INODE" != "$AFTER_INODE" ]; then
  fail "a refused takeover must leave the peer's lock in place, not replace it"
fi

bash -n "$SCRIPT" || fail "bash -n failed"

echo OK
