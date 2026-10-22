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

# The housekeeper lock must be taken over atomically (AFC-458).
#
# Checking that a lock is stale and then removing it is two steps, and two
# reclaimers can interleave them: both see the stale lock, one removes it and
# creates a fresh one, and the other then removes that fresh, empty lock and
# creates its own, so both believe they hold it.  The removal is therefore a
# rename, which only one caller can win.  scripts/mac-resource-watch.py takes
# the same lock the same way (its test is scripts/test-mac-resource-watch.sh),
# and HogHunter's Swift lock does too: they share one lock directory.
#
# No removal of the lock path itself, in any spelling, except the release.
if [ -n "$(code_lines '(^|[^[:alnum:]_])(rm|rmdir)([[:space:]]+-[[:alnum:]]+)*[[:space:]]+"?\$\{?HOUSEKEEPER_LOCK\}?([^_[:alnum:]]|$)' | grep -v 'trap ')" ]; then
  fail "mac-auto-cleanup.sh must not remove the housekeeper lock and create it again; take it over by moving it aside"
fi

if ! grep -q 'HOUSEKEEPER_STALE' "$SCRIPT"; then
  fail "mac-auto-cleanup.sh must take a stale housekeeper lock by moving it aside, so only one caller can win"
fi

# Behavioural checks on the lock block itself.  The block is extracted from
# the script and run against a temporary HOME, so nothing real is touched.
LOCK_BLOCK="$(sed -n '/^HOUSEKEEPER_LOCK=/,/^fi$/p' "$SCRIPT")"
if [ -z "$LOCK_BLOCK" ]; then
  fail "could not extract the housekeeper lock block from mac-auto-cleanup.sh"
fi

LOCK_TEST_ROOT="$(mktemp -d)"
trap 'rm -rf "$LOCK_TEST_ROOT"' EXIT

# mv is a stub that logs every call before it runs the real one, so a test can
# see whether the block ever moved a lock it should have left alone.
STUB_BIN="$LOCK_TEST_ROOT/bin"
mkdir -p "$STUB_BIN"
REAL_MV="$(command -v mv)"
cat > "$STUB_BIN/mv" <<STUB
#!/bin/bash
printf '%s\n' "\$*" >> "\$MV_LOG"
exec "$REAL_MV" "\$@"
STUB
chmod +x "$STUB_BIN/mv"

run_lock_block() {
  # HOME points at a scratch tree; the block only touches the lock under it.
  # The block releases the lock on exit, so whether it holds the lock is
  # reported from inside its own lifetime.  Every refusal ends in `exit 0`
  # before that line, so a refusal prints the skip message and no LOCK_HELD
  # line at all.  When a second argument is given, a holder keeps the lock
  # until that file appears (ten seconds at most).
  local release="${2:-}"
  HOME="$1" HOUSEKEEPER_LOCK_OWNER='' PATH="$STUB_BIN:$PATH" MV_LOG="$1/mv.log" RELEASE="$release" \
    bash -c "$LOCK_BLOCK"$'\n''[ -d "$HOUSEKEEPER_LOCK" ] && echo LOCK_HELD=yes'$'\n''n=0; while [ -n "$RELEASE" ] && [ ! -e "$RELEASE" ] && [ "$n" -lt 200 ]; do sleep 0.05; n=$((n + 1)); done'
}

make_lock() {
  mkdir -p "$1/.claude-disk-janitor/.housekeeper.lock"
}

stale_copies() {
  find "$1/.claude-disk-janitor" -maxdepth 1 -name '.housekeeper.lock.stale-*' 2>/dev/null
}

lock_was_moved() {
  [ -f "$1/mv.log" ] && grep -q '\.housekeeper\.lock ' "$1/mv.log"
}

# 1. No lock: the block takes it.
H1="$LOCK_TEST_ROOT/case1"
mkdir -p "$H1"
OUT="$(run_lock_block "$H1" 2>&1 || true)"
if ! printf '%s' "$OUT" | grep -q 'LOCK_HELD=yes'; then
  fail "the housekeeper lock must be taken when it is free"
fi

# 2. A fresh lock held by a peer: refused, and the peer's lock is never moved,
#    not even for a moment, because while it is gone any caller can take it.
H2="$LOCK_TEST_ROOT/case2"
mkdir -p "$H2"
make_lock "$H2"
OUT="$(run_lock_block "$H2" 2>&1 || true)"
if ! printf '%s' "$OUT" | grep -q 'skipped, peer holds housekeeper lock'; then
  fail "a fresh housekeeper lock must be refused with 'skipped, peer holds housekeeper lock'"
fi
if printf '%s' "$OUT" | grep -q 'LOCK_HELD='; then
  fail "a refused takeover must not report the lock as held"
fi
if [ ! -d "$H2/.claude-disk-janitor/.housekeeper.lock" ]; then
  fail "a refused takeover must leave the peer's lock in place"
fi
if lock_was_moved "$H2"; then
  fail "a fresh housekeeper lock must be judged at its own path, never moved aside to be checked"
fi
if [ -n "$(stale_copies "$H2")" ]; then
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
if ! lock_was_moved "$H3"; then
  fail "a stale housekeeper lock must be taken over by moving it aside"
fi
if [ -n "$(stale_copies "$H3")" ]; then
  fail "a taken-over stale lock must not leave a moved-aside copy behind"
fi

# 4. A stale lock with something in it is not the marker a holder makes: it
#    is refused, left where it is, and never moved.
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
  fail "a lock directory with contents in it must be left in place, not removed"
fi
if lock_was_moved "$H4"; then
  fail "a lock directory with contents in it must not be moved"
fi

# 5. Two reclaimers race for one stale lock, ten times over: exactly one may
#    win each race.  A winner keeps the lock until both have answered, so a
#    second winner would have to overlap the first rather than follow it.
for trial in 1 2 3 4 5 6 7 8 9 10; do
  H5="$LOCK_TEST_ROOT/case5-$trial"
  mkdir -p "$H5"
  make_lock "$H5"
  touch -t 202001010000 "$H5/.claude-disk-janitor/.housekeeper.lock"
  run_lock_block "$H5" "$H5/release" > "$H5/out.a" 2>&1 &
  run_lock_block "$H5" "$H5/release" > "$H5/out.b" 2>&1 &
  n=0
  until { [ -s "$H5/out.a" ] && [ -s "$H5/out.b" ]; } || [ "$n" -ge 200 ]; do
    sleep 0.05
    n=$((n + 1))
  done
  touch "$H5/release"
  wait
  HELD="$(cat "$H5/out.a" "$H5/out.b" | grep -c 'LOCK_HELD=yes' || true)"
  if [ "$HELD" != "1" ]; then
    fail "two reclaimers racing for one stale lock must leave exactly one holder, got $HELD (trial $trial)"
  fi
  if [ -n "$(stale_copies "$H5")" ]; then
    fail "a race for a stale lock must not leave a moved-aside copy behind (trial $trial)"
  fi
done

# 6. A mkdir failure that is not EEXIST is not contention: it fails loudly
#    instead of passing for a peer's lock.  Here the lock's parent is a file.
H6="$LOCK_TEST_ROOT/case6"
mkdir -p "$H6"
: > "$H6/.claude-disk-janitor"
set +e
OUT="$(run_lock_block "$H6" 2>&1)"
RC=$?
set -e
if [ "$RC" -eq 0 ]; then
  fail "a housekeeper lock that cannot be created must exit nonzero"
fi
if ! printf '%s' "$OUT" | grep -q 'cannot create the housekeeper lock'; then
  fail "a housekeeper lock that cannot be created must say so on stderr"
fi
if printf '%s' "$OUT" | grep -q 'skipped, peer holds housekeeper lock'; then
  fail "a housekeeper lock that cannot be created must not be reported as a peer's lock"
fi

# 7. The holder sweeps empty moved-aside copies older than the stale window,
#    which a takeover cut short leaves behind, and nothing else.
H7="$LOCK_TEST_ROOT/case7"
D7="$H7/.claude-disk-janitor"
mkdir -p "$D7/.housekeeper.lock.stale-1.1" "$D7/.housekeeper.lock.stale-2.2" "$D7/.housekeeper.lock.stale-3.3"
echo "not ours" > "$D7/.housekeeper.lock.stale-3.3/leftover.txt"
touch -t 202001010000 "$D7/.housekeeper.lock.stale-1.1" "$D7/.housekeeper.lock.stale-3.3"
OUT="$(run_lock_block "$H7" 2>&1 || true)"
if ! printf '%s' "$OUT" | grep -q 'LOCK_HELD=yes'; then
  fail "the housekeeper lock must be taken when it is free"
fi
if [ -e "$D7/.housekeeper.lock.stale-1.1" ]; then
  fail "an empty moved-aside copy older than the stale window must be swept"
fi
if [ ! -d "$D7/.housekeeper.lock.stale-2.2" ]; then
  fail "a fresh moved-aside copy must not be swept: it may be a live holder's lock"
fi
if [ ! -f "$D7/.housekeeper.lock.stale-3.3/leftover.txt" ]; then
  fail "a moved-aside copy with contents in it must not be swept"
fi

bash -n "$SCRIPT" || fail "bash -n failed"

echo OK
