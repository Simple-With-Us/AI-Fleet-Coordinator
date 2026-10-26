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

# AFC-504: the remote maintenance trigger must reach the host through the
# operator's own inventory, not through a target written into a published script.
#
# The ssh target is private deployment inventory: it arrives in the environment as
# HOUSEKEEPER_SSH_ALIAS, and the operator's own ssh config is what resolves it.
# A literal target in this script would ship the host name to every reader of a
# public repository, and a placeholder would reach the wrong host instead of the
# one the caller meant.
#
# The hop is skipped rather than refused when the variable is unset, because the
# hop is redundant: the host self-maintains through its own
# /etc/cron.daily/coolify-auto-maintenance, and the local cleanup that runs
# before it is the part that matters.
#
# A line is an ssh command when it still says ssh after its quoted runs are
# taken out, so a message that merely names ssh is not read as a command.  The
# target of a real command has to be the inventory variable itself, so a target
# hoisted into another variable is caught as well.
ssh_command_lines() {
  grep -nE 'ssh' "$SCRIPT" \
    | grep -vE '^[^:]+:[[:space:]]*#' \
    | while IFS= read -r line; do
        shape=$(printf '%s' "$line" | sed -e 's/"[^"]*"/QQ/g' -e "s/'[^']*'/QQ/g")
        case "$shape" in
          *ssh\ *) printf '%s\n' "$line" ;;
        esac
      done
}

if [ -n "$(ssh_command_lines | grep -v '"[$]HOUSEKEEPER_SSH_ALIAS')" ]; then
  fail "mac-auto-cleanup.sh must not ssh a literal host target (private deployment inventory)"
fi

if ! grep -q 'HOUSEKEEPER_SSH_ALIAS' "$SCRIPT"; then
  fail "mac-auto-cleanup.sh must reach the remote host through HOUSEKEEPER_SSH_ALIAS"
fi

if ! grep -q 'HOUSEKEEPER_SSH_ALIAS:-' "$SCRIPT"; then
  fail "mac-auto-cleanup.sh must read the remote target without tripping on an unset HOUSEKEEPER_SSH_ALIAS"
fi

# The hop fails closed.  A target that does not resolve is a configuration
# fault, so the script says which variable is missing and ends the run with a
# nonzero exit, instead of reporting a success the operator would read as
# "the remote maintenance ran".  A silent skip is the failure this rules out.
if ! grep -q 'HOUSEKEEPER_SSH_ALIAS is required' "$SCRIPT"; then
  fail "mac-auto-cleanup.sh must name HOUSEKEEPER_SSH_ALIAS when no target resolves"
fi

if ! grep -q 'Remote Hetzner maintenance trigger failed' "$SCRIPT"; then
  fail "mac-auto-cleanup.sh must report a failed connectivity check instead of skipping it"
fi

# The exit code that means "the local cleanup ran, only the remote hop failed"
# is the one the watcher reads as cleaned, so it is named in one place and both
# scripts agree on it.
if ! grep -q 'HOUSEKEEPER_HOP_EXIT=75' "$SCRIPT"; then
  fail "mac-auto-cleanup.sh must exit 75 when the local cleanup ran and only the remote hop failed"
fi
if ! grep -q 'CLEANUP_EXIT_CLEANED = (0, 75)' "$ROOT/mac-resource-watch.py"; then
  fail "mac-resource-watch.py must read the cleanup exit code 75 as cleaned"
fi

# The housekeeper lock must be taken over atomically.
#
# Checking that a lock is stale and then removing it is two steps, and two
# reclaimers can interleave them: both see the stale lock, one removes it and
# creates a fresh one, and the other then removes that fresh, empty lock and
# creates its own, so both believe they hold it.  The removal is therefore a
# rename, which only one caller can win.  An equivalent lock helper in a
# sibling repository does it this way too, and the implementations have to
# agree: they share one lock directory.
#
# The guard has to cover EVERY spelling of a removal, not just `rmdir`.  An
# `rm -rf "$HOUSEKEEPER_LOCK"` is the same racy takeover with a different
# spelling, and it passes a guard that only greps for `rmdir`.  The only
# removal that is allowed is the ownership-checked release, and it lives in
# housekeeper_release_section, which is where the inode check is.
RELEASE_BLOCK="$(sed -n '/^housekeeper_release_section() {/,/^}/p' "$SCRIPT")"
if [ -z "$RELEASE_BLOCK" ]; then
  fail "could not extract housekeeper_release_section from mac-auto-cleanup.sh"
fi
if ! printf '%s' "$RELEASE_BLOCK" | grep -q 'stat -f %i'; then
  fail "the release must check the lock's inode before it removes the lock"
fi
RELEASE_START="$(grep -n '^housekeeper_release_section() {' "$SCRIPT" | head -1 | cut -d: -f1)"
RELEASE_END="$(awk -v s="$RELEASE_START" 'NR>s && /^\}/{print NR; exit}' "$SCRIPT")"
CANONICAL_REMOVALS="$(awk -v s="$RELEASE_START" -v e="$RELEASE_END" 'NR<s || NR>e' "$SCRIPT" \
  | grep -nE '(^|[[:space:];|])(rm|rmdir)[[:space:]][^|;]*\$HOUSEKEEPER_LOCK([^A-Za-z_]|$)' \
  | grep -vE '^[0-9]+:[[:space:]]*#' || true)"
if [ -n "$CANONICAL_REMOVALS" ]; then
  fail "mac-auto-cleanup.sh must not remove the canonical housekeeper lock path outside the ownership-checked release"
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
MOVE_LINE="$(grep -nE 'housekeeper_rename_exclusive "\$HOUSEKEEPER_LOCK" "\$HOUSEKEEPER_STALE"' "$SCRIPT" | head -1 | cut -d: -f1)"
if [ -z "$STALE_CHECK_LINE" ] || [ -z "$MOVE_LINE" ]; then
  fail "mac-auto-cleanup.sh must check the lock's age and then move it aside"
fi
if [ "$STALE_CHECK_LINE" -ge "$MOVE_LINE" ]; then
  fail "mac-auto-cleanup.sh must check the lock's age at the canonical path BEFORE moving it aside"
fi

# A restore must not use mv -n.
#
# mv -n is NOT a no-replace primitive for directories on macOS.  When the
# destination already exists as a directory, mv moves the source INSIDE it, and
# -n does not stop that.  A restore that used it would nest the peer's lock
# inside the new holder's lock directory, and a lock directory with contents
# in it is one that rmdir refuses to release and that every later reclaimer
# refuses to take over.  The lock then leaks for the whole stale window.
#
# The restore is an atomic no-replace rename instead, so the move and the
# refusal are ONE decision.  When the canonical path is occupied the restore
# refuses and leaves the moved-aside copy where it is, because the
# alternative is clobbering a live holder.
if grep -q 'mv -n "\$HOUSEKEEPER_STALE"' "$SCRIPT"; then
  fail "mac-auto-cleanup.sh must not restore the lock with mv -n; on macOS it nests a directory inside an existing one"
fi

# The move aside must not use mv either.  mv nests a directory inside an
# existing destination directory, so a move aside that lands on a name another
# reclaimer already took puts the peer's lock inside that name instead of
# moving it, and the canonical path is left free for a third reclaimer.
if grep -qE '(^|[[:space:];|])mv[[:space:]]+"\$HOUSEKEEPER_(LOCK|STALE)"' "$SCRIPT"; then
  fail "mac-auto-cleanup.sh must move the lock with the atomic no-replace rename, not with mv"
fi

RESTORE_BLOCK="$(sed -n '/^housekeeper_restore_lock() {/,/^}/p' "$SCRIPT")"
if [ -z "$RESTORE_BLOCK" ]; then
  fail "could not extract housekeeper_restore_lock from mac-auto-cleanup.sh"
fi

# The restore moves the lock with the atomic no-replace rename, so the two
# run together:  the restore on its own would only prove that the helper is
# missing.
RENAME_BLOCK="$(sed -n '/^housekeeper_rename_exclusive() {/,/^}/p' "$SCRIPT")"
if [ -z "$RENAME_BLOCK" ]; then
  fail "could not extract housekeeper_rename_exclusive from mac-auto-cleanup.sh"
fi
RESTORE_WITH_MOVE="$RENAME_BLOCK"$'\n'"$RESTORE_BLOCK"

# The guard helper, the acquire section and the release section.
GUARD_BLOCK="$(sed -n '/^housekeeper_with_guard() {/,/^}/p' "$SCRIPT")"
if [ -z "$GUARD_BLOCK" ]; then
  fail "could not extract housekeeper_with_guard from mac-auto-cleanup.sh"
fi
ACQUIRE_BLOCK="$(sed -n '/^housekeeper_acquire_section() {/,/^}/p' "$SCRIPT")"
if [ -z "$ACQUIRE_BLOCK" ]; then
  fail "could not extract housekeeper_acquire_section from mac-auto-cleanup.sh"
fi

# The mkdir helper reports WHY the mkdir failed, so it is tested on its own.
MKDIR_BLOCK="$(sed -n '/^housekeeper_try_mkdir() {/,/^}/p' "$SCRIPT")"
if [ -z "$MKDIR_BLOCK" ]; then
  fail "could not extract housekeeper_try_mkdir from mac-auto-cleanup.sh"
fi

# The EXIT trap must not remove a moved-aside copy that holds a PEER's lock.
# The copy is only ever the peer's lock: this process moved the peer's lock
# aside and never owned it.  Deleting it in the trap frees the canonical path
# for a third reclaimer while that peer is still running, which is the double
# sweep this lock exists to prevent.
if ! grep -q 'HOUSEKEEPER_STALE_IS_PEER' "$SCRIPT"; then
  fail "mac-auto-cleanup.sh must mark a moved-aside copy as a peer's lock so the EXIT trap leaves it alone"
fi

# Only an EXISTING lock is peer contention.  A mkdir that fails for any other
# reason must not be reported as a successful skip.
if ! grep -q 'failed to create housekeeper lock' "$SCRIPT"; then
  fail "mac-auto-cleanup.sh must report a lock it could not create, not skip as if a peer held it"
fi

# The committed scripts must not name a private sibling repository or its
# internal source paths.  A test fixture is committed and indexed, so a
# private project name and the layout of its source tree end up in a public
# index.  The lock protocol only needs to say that an equivalent helper in a
# sibling repository implements the same rules.
#
# The forbidden strings are NOT committed here:  they are read from the
# environment, so this file carries neither the name nor the layout of a real
# private project.  A developer who wants the real check sets
# PRIVATE_REPO_GUARD, PRIVATE_TEST_PY_PATH and PRIVATE_TEST_SWIFT_PATH outside
# the repository; CI sets nothing and gets a synthetic sentinel, which still
# exercises both guards end to end instead of letting them silently pass.
PRIVATE_REPO="${PRIVATE_REPO_GUARD:-Synthetic-Private-Sibling-Repo}"
PRIVATE_PY_PATH="${PRIVATE_TEST_PY_PATH:-synthetic-private-repo/internal/module.py}"
PRIVATE_SWIFT_PATH="${PRIVATE_TEST_SWIFT_PATH:-synthetic-private-repo/internal/Module.swift}"
# The guard definitions are the one place these strings are named, so they are
# left out of the check on the fixtures themselves.
guard_lines() {
  cat "$@" | grep -vE '^[[:space:]]*PRIVATE_(REPO|PY_PATH|SWIFT_PATH)='
}
if grep -nF -e "$PRIVATE_PY_PATH" -e "$PRIVATE_SWIFT_PATH" \
    "$ROOT/mac-auto-cleanup.sh" "$ROOT/mac-resource-watch.py"; then
  fail "the committed housekeeper scripts must not name a private sibling repository's internal source paths"
fi
if grep -nF "$PRIVATE_REPO" "$ROOT/mac-resource-watch.py"; then
  fail "the committed housekeeper scripts must not name a private sibling repository"
fi
if guard_lines "$ROOT/test-mac-auto-cleanup.sh" "$ROOT/test-mac-resource-watch.sh" \
    | grep -nF -e "$PRIVATE_PY_PATH" -e "$PRIVATE_SWIFT_PATH" -e "$PRIVATE_REPO"; then
  fail "the committed housekeeper scripts must not name a private sibling repository"
fi
# A name split across two lines, or written with a space or a hyphen instead
# of run together, defeats a fixed-string grep -- and a committed script must
# not carry it that way either.  Both the haystack and the needle are stripped
# of whitespace, comment markers and the separators a name can be broken on,
# so every spelling and every split is caught without the guard having to name
# either half.
PRIVATE_REPO_SPELLED="$(printf '%s' "$PRIVATE_REPO" | tr -d '[:space:]#_-')"
# The reclaim-engine block below invokes a binary on this Mac, so its path
# stays; the lock protocol's prose above it must not name the repository.
if awk '/^# 2c\./{exit} {print}' "$ROOT/mac-auto-cleanup.sh" \
    | tr -d '[:space:]#_-' | grep -qF "$PRIVATE_REPO_SPELLED"; then
  fail "the housekeeper lock protocol's prose must not name a private sibling repository"
fi
if tr -d '[:space:]#_-' < "$ROOT/mac-resource-watch.py" | grep -qF "$PRIVATE_REPO_SPELLED"; then
  fail "the committed housekeeper scripts must not name a private sibling repository, split across lines"
fi
if guard_lines "$ROOT/test-mac-auto-cleanup.sh" "$ROOT/test-mac-resource-watch.sh" \
    | tr -d '[:space:]#_-' | grep -qF "$PRIVATE_REPO_SPELLED"; then
  fail "the committed housekeeper scripts must not name a private sibling repository, split across lines"
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

run_trap() {
  # $1 = HOME, $2 = the peer flag, $3 = the recorded lock inode.
  #
  # The trap runs the release section inside the guard helper, so the helper
  # and the section have to be in reach, and the section has to travel to the
  # child shell as an exported function.
  HOME="$1" \
  HOUSEKEEPER_GUARD="$1/.claude-disk-janitor/.housekeeper.lock.guard" \
  HOUSEKEEPER_LOCK="$1/.claude-disk-janitor/.housekeeper.lock" \
  HOUSEKEEPER_STALE="$1/.claude-disk-janitor/.housekeeper.lock.stale-1.2" \
  HOUSEKEEPER_STALE_IS_PEER="$2" HOUSEKEEPER_LOCK_INODE="$3" \
  bash -c "$GUARD_BLOCK"$'\n'"$RELEASE_BLOCK"$'\n''export -f housekeeper_release_section'$'\n''housekeeper_with_guard "$HOUSEKEEPER_GUARD" "housekeeper_release_section"' >/dev/null 2>&1 || true
}

# Splice a stub function into the lock block.
#
# The block exports its own functions into the guarded child process, so a
# stub has to be defined AFTER the block's own definition of the function it
# replaces and BEFORE that export.  Inserting the stub right in front of the
# export line does both, so the child process runs the stub and not the real
# helper.  $1 = the block, $2 = the stub source.
block_with_stub() {
  python3 - "$1" "$2" <<'PY'
import sys
block, stub = sys.argv[1], sys.argv[2]
out = []
for line in block.split("\n"):
    if line.startswith("export -f housekeeper_with_guard"):
        out.append(stub)
    out.append(line)
sys.stdout.write("\n".join(out))
PY
}

# The stubs themselves.  Each one reports the errno the real helper would
# report at the syscall, so the caller's errno -> outcome mapping is what
# the case exercises.
# 17 = EEXIST:  the path is held by a peer.
STUB_MKDIR_EEXIST='housekeeper_try_mkdir() {
  return 17
}'
# 13 = EACCES:  a real move failure (permissions, no space, a cross-device
# rename).  It is NOT a lost race.
STUB_RENAME_EACCES='housekeeper_rename_exclusive() {
  return 13
}'
# 2 = ENOENT:  the source is gone, so another caller won the race.
STUB_RENAME_ENOENT='housekeeper_rename_exclusive() {
  return 2
}'


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

# 7. The restore refuses an occupied canonical path, and does not nest.
#
# This is the mv -n hazard, exercised rather than asserted.  The canonical path
# is occupied by another holder and the moved-aside copy holds a peer's lock.
# The restore must refuse, must not place the copy inside the canonical
# directory, and must leave the copy where it is.
#
# The peer's lock is checked BY INODE:  an exit code alone would not catch a
# restore that replaced the peer's lock with the copy and still reported the
# failure.
H7="$LOCK_TEST_ROOT/case7"
mkdir -p "$H7/.claude-disk-janitor"
mkdir -p "$H7/.claude-disk-janitor/.housekeeper.lock"
mkdir -p "$H7/.claude-disk-janitor/.housekeeper.lock.stale-1.2"
PEER_INODE="$(stat -f %i "$H7/.claude-disk-janitor/.housekeeper.lock")"
OUT="$(HOME="$H7" \
  HOUSEKEEPER_LOCK="$H7/.claude-disk-janitor/.housekeeper.lock" \
  HOUSEKEEPER_STALE="$H7/.claude-disk-janitor/.housekeeper.lock.stale-1.2" \
  bash -c "$RESTORE_WITH_MOVE"$'\n''housekeeper_restore_lock' 2>&1 || true)"
if ! printf '%s' "$OUT" | grep -q 'failed to restore housekeeper lock'; then
  fail "a restore onto an occupied canonical path must report the failure"
fi
if [ -n "$(find "$H7/.claude-disk-janitor/.housekeeper.lock" -mindepth 1 -maxdepth 1 2>/dev/null)" ]; then
  fail "a refused restore must not nest the moved-aside copy inside the canonical lock"
fi
if [ ! -d "$H7/.claude-disk-janitor/.housekeeper.lock.stale-1.2" ]; then
  fail "a refused restore must leave the moved-aside copy where it is"
fi
AFTER_INODE="$(stat -f %i "$H7/.claude-disk-janitor/.housekeeper.lock" 2>/dev/null || echo '')"
if [ "$PEER_INODE" != "$AFTER_INODE" ]; then
  fail "a refused restore must leave the peer's lock directory exactly as it was"
fi

# 8. The EXIT trap leaves a moved-aside copy alone when it holds a peer's lock.
#
# The trap runs after a failed restore, and the copy at that point is a live
# peer's lock that this process displaced for a moment.  Removing it frees the
# canonical path for a third reclaimer while that peer is still running.
TRAP_CMD="$(grep -oE "trap '.*' EXIT" "$SCRIPT" | head -1 | sed "s/^trap '//; s/' EXIT$//")"
if [ -z "$TRAP_CMD" ]; then
  fail "could not extract the EXIT trap from mac-auto-cleanup.sh"
fi

H8="$LOCK_TEST_ROOT/case8"
mkdir -p "$H8/.claude-disk-janitor/.housekeeper.lock.stale-1.2"
run_trap "$H8" 1 ''
if [ ! -d "$H8/.claude-disk-janitor/.housekeeper.lock.stale-1.2" ]; then
  fail "the EXIT trap must not remove a moved-aside copy that holds a peer's lock"
fi

# 9. A dangling symlink at the canonical path is peer contention, not an error.
#
# mkdir fails with EEXIST for a dangling symlink, but [ -e ] follows the link
# and reports false.  A check that only asks [ -e ] therefore reads a lost
# reclaim race as a hard error and crashes the run instead of backing off.
H9="$LOCK_TEST_ROOT/case9"
mkdir -p "$H9/.claude-disk-janitor"
ln -s "$H9/.claude-disk-janitor/not-there" "$H9/.claude-disk-janitor/.housekeeper.lock"
OUT="$(run_lock_block "$H9" 2>&1 || true)"
if ! printf '%s' "$OUT" | grep -q 'skipped, peer holds housekeeper lock'; then
  fail "a dangling symlink at the canonical path must be treated as peer contention"
fi
if printf '%s' "$OUT" | grep -q 'failed to create housekeeper lock'; then
  fail "a dangling symlink at the canonical path must not be reported as a lock that could not be created"
fi

# 10. The no-replace move refuses an occupied destination.
#
# The move and the refusal have to be ONE decision.  A plain rename onto an
# existing EMPTY directory SUCCEEDS and replaces it, and the lock marker is an
# empty directory, so a move built on "check that the path is free, then
# rename" has a window in which a third reclaimer's fresh lock is silently
# replaced while that reclaimer keeps sweeping beside us.
#
# The helper reports the refusal as its own exit status (17, EEXIST), so the
# caller can tell it from a real failure without looking at the path again.
H10="$LOCK_TEST_ROOT/case10"
mkdir -p "$H10/src" "$H10/dst"
DST_INODE="$(stat -f %i "$H10/dst")"
rc=0
bash -c "$RENAME_BLOCK"$'\n''housekeeper_rename_exclusive "$1" "$2"' _ "$H10/src" "$H10/dst" >/dev/null 2>&1 || rc=$?
if [ "$rc" != "17" ]; then
  fail "the no-replace move must report 17 (EEXIST) for an occupied destination (got $rc)"
fi
if [ "$(stat -f %i "$H10/dst")" != "$DST_INODE" ]; then
  fail "the no-replace move must not replace an occupied destination"
fi
if [ ! -d "$H10/src" ]; then
  fail "a refused move must leave the source where it is"
fi

# 11. The no-replace move succeeds onto a free destination.
H11="$LOCK_TEST_ROOT/case11"
mkdir -p "$H11/src"
SRC_INODE="$(stat -f %i "$H11/src")"
rc=0
bash -c "$RENAME_BLOCK"$'\n''housekeeper_rename_exclusive "$1" "$2"' _ "$H11/src" "$H11/free" >/dev/null 2>&1 || rc=$?
if [ "$rc" != "0" ]; then
  fail "the no-replace move must report 0 for a free destination (got $rc)"
fi
if [ ! -d "$H11/free" ]; then
  fail "the no-replace move must move onto a free destination"
fi
if [ -e "$H11/src" ]; then
  fail "the no-replace move must leave no source behind"
fi
if [ "$(stat -f %i "$H11/free")" != "$SRC_INODE" ]; then
  fail "the no-replace move must MOVE the source, not create a new directory"
fi

# 12. The restore puts the moved-aside copy back on a free canonical path.
#
# The copy that the restore moves may hold a PEER's lock, so the restore has
# to be the same move, not a copy:  the peer's lock directory has to come
# back as the same directory.
H12="$LOCK_TEST_ROOT/case12"
mkdir -p "$H12/.claude-disk-janitor"
mkdir -p "$H12/.claude-disk-janitor/.housekeeper.lock.stale-1.2"
COPY_INODE="$(stat -f %i "$H12/.claude-disk-janitor/.housekeeper.lock.stale-1.2")"
rc=0
OUT="$(HOME="$H12" \
  HOUSEKEEPER_LOCK="$H12/.claude-disk-janitor/.housekeeper.lock" \
  HOUSEKEEPER_STALE="$H12/.claude-disk-janitor/.housekeeper.lock.stale-1.2" \
  bash -c "$RESTORE_WITH_MOVE"$'\n''housekeeper_restore_lock' 2>&1)" || rc=$?
if [ "$rc" != "0" ]; then
  fail "a restore onto a free canonical path must succeed (got $rc: $OUT)"
fi
if [ ! -d "$H12/.claude-disk-janitor/.housekeeper.lock" ]; then
  fail "a restore onto a free canonical path must put the lock back"
fi
if [ -e "$H12/.claude-disk-janitor/.housekeeper.lock.stale-1.2" ]; then
  fail "a restore onto a free canonical path must leave no copy behind"
fi
if [ "$(stat -f %i "$H12/.claude-disk-janitor/.housekeeper.lock")" != "$COPY_INODE" ]; then
  fail "the restore must MOVE the copy back, not create a new lock directory"
fi

# 13. The mkdir helper tells EEXIST from every other failure.
#
# The reason the mkdir failed has to be observed AT THE SYSCALL:  a peer
# holds the path (EEXIST) is a skip, and anything else is a real error.  The
# helper reports the two as different exit statuses so the caller never has
# to look at the path again.
H13="$LOCK_TEST_ROOT/case13"
mkdir -p "$H13"
rc=0
bash -c "$MKDIR_BLOCK"$'\n''housekeeper_try_mkdir "$1"' _ "$H13/free" >/dev/null 2>&1 || rc=$?
if [ "$rc" != "0" ]; then
  fail "the mkdir helper must report 0 when it created the lock (got $rc)"
fi
if [ ! -d "$H13/free" ]; then
  fail "the mkdir helper must create the lock directory"
fi
mkdir -p "$H13/taken"
rc=0
bash -c "$MKDIR_BLOCK"$'\n''housekeeper_try_mkdir "$1"' _ "$H13/taken" >/dev/null 2>&1 || rc=$?
if [ "$rc" != "17" ]; then
  fail "the mkdir helper must report 17 (EEXIST) when a peer holds the path (got $rc)"
fi
# The parent is a FILE, so the mkdir fails with ENOTDIR and not with EEXIST.
: > "$H13/parent-is-a-file"
rc=0
bash -c "$MKDIR_BLOCK"$'\n''housekeeper_try_mkdir "$1"' _ "$H13/parent-is-a-file/lock" >/dev/null 2>&1 || rc=$?
if [ "$rc" = "0" ] || [ "$rc" = "17" ]; then
  fail "the mkdir helper must report a real error when the mkdir failed for another reason (got $rc)"
fi
# The parent does not exist at all.
rc=0
bash -c "$MKDIR_BLOCK"$'\n''housekeeper_try_mkdir "$1"' _ "$H13/no-such-parent/lock" >/dev/null 2>&1 || rc=$?
if [ "$rc" = "0" ] || [ "$rc" = "17" ]; then
  fail "the mkdir helper must report a real error when the parent directory is missing (got $rc)"
fi

# 14. A lock that could not be created for another reason is a hard error.
#
# The lock's parent is a FILE, so the mkdir fails with ENOTDIR.  That is not
# peer contention, and reporting it as "peer holds lock" would skip the
# cleanup silently.
H14="$LOCK_TEST_ROOT/case14"
mkdir -p "$H14"
: > "$H14/.claude-disk-janitor"
OUT="$(run_lock_block "$H14" 2>&1 || true)"
if ! printf '%s' "$OUT" | grep -q 'failed to create housekeeper lock'; then
  fail "a lock that could not be created for any reason other than a peer must be reported as an error"
fi
if printf '%s' "$OUT" | grep -q 'skipped, peer holds housekeeper lock'; then
  fail "a lock that could not be created for another reason must not be reported as peer contention"
fi

# 15. An EEXIST from the mkdir is a peer-held skip, and nothing is acquired.
#
# The lock is created by housekeeper_try_mkdir, a python3 os.mkdir helper, so
# the `mkdir` COMMAND is not on the path the code takes at all:  a shim on
# PATH for it never ran, and this case asserted nothing at all.  The errno has
# to be produced where the production code produces it -- at the mkdir
# syscall -- so the helper itself is stubbed to report EEXIST (17).
#
# The decision has to be made at the syscall:  a lock reported as held by a
# peer is NOT held by this process, so nothing may be acquired afterwards
# either.  Looking at the path again is the racy second step this protocol
# forbids, and a test that REQUIRES the lock to be taken after a failed
# mkdir would bless exactly that race.
H15="$LOCK_TEST_ROOT/case15"
mkdir -p "$H15"
BLOCK15="$(block_with_stub "$LOCK_BLOCK" "$STUB_MKDIR_EEXIST")"
OUT="$(HOME="$H15" HOUSEKEEPER_LOCK_OWNER= bash -c "$BLOCK15"$'\n''[ -d "$HOUSEKEEPER_LOCK" ] && echo LOCK_HELD=yes' 2>&1 || true)"
if ! printf '%s' "$OUT" | grep -q 'skipped, peer holds housekeeper lock'; then
  fail "an EEXIST mkdir result must be treated as peer contention"
fi
if printf '%s' "$OUT" | grep -q 'failed to create housekeeper lock'; then
  fail "an EEXIST mkdir result must not be reported as a lock that could not be created"
fi
if printf '%s' "$OUT" | grep -q 'LOCK_HELD=yes'; then
  fail "an EEXIST mkdir result must not be reacquired after re-inspecting the path"
fi

# 16. The shared guard over the housekeeper protocol.
#
# Every implementation of this protocol -- this script, the Python watcher in
# this repo and an equivalent lock helper in a sibling repository --
# serializes the critical sections of the lock with an exclusive flock on ONE
# file:  ~/.claude-disk-janitor/.housekeeper.lock.guard.  It is a SIBLING of
# the lock directory, and it is never deleted, because deleting it would
# split the flock.
if ! grep -q 'HOUSEKEEPER_GUARD="\$HOME/.claude-disk-janitor/.housekeeper.lock.guard"' "$SCRIPT"; then
  fail "mac-auto-cleanup.sh must take the shared guard at ~/.claude-disk-janitor/.housekeeper.lock.guard"
fi
WATCHER_GUARD="$(python3 - "$ROOT/mac-resource-watch.py" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("watch", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
print(mod._housekeeper_guard_path().name)
PY
)"
if [ "$WATCHER_GUARD" != ".housekeeper.lock.guard" ]; then
  fail "the watcher must take the SAME guard as mac-auto-cleanup.sh (got $WATCHER_GUARD); they share one lock directory"
fi
if ! grep -q 'fcntl.flock' "$SCRIPT"; then
  fail "the guard must be an exclusive flock"
fi

# 17. The release is ONE guarded decision.
#
# The ownership check and the removal have to be one decision against every
# implementation of this protocol:  a takeover that replaces the canonical
# lock between the check and the removal would have the exiting holder delete
# the NEW holder's lock.  The guard is what makes it one decision, so the
# release has to wait for a process that holds it.
H17="$LOCK_TEST_ROOT/case17"
mkdir -p "$H17/.claude-disk-janitor"
python3 - "$H17/.claude-disk-janitor/.housekeeper.lock.guard" "$H17/ready" <<'PY' &
import fcntl, os, sys, time
guard, ready = sys.argv[1], sys.argv[2]
fd = os.open(guard, os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
open(ready, "w").write("held")
time.sleep(3)
PY
HOLDER_PID=$!
while [ ! -f "$H17/ready" ]; do sleep 0.05; done
START="$SECONDS"
run_trap "$H17" 0 ''
ELAPSED=$((SECONDS - START))
wait "$HOLDER_PID"
if [ "$ELAPSED" -lt 1 ]; then
  fail "the release must wait for the shared guard; it finished in ${ELAPSED}s while another process held it"
fi

# And a lock that is NOT the one this process created is never removed, even
# under the guard.
mkdir -p "$H17/.claude-disk-janitor/.housekeeper.lock"
PEER_INODE="$(stat -f %i "$H17/.claude-disk-janitor/.housekeeper.lock")"
run_trap "$H17" 0 "$((PEER_INODE - 1))"
AFTER_INODE="$(stat -f %i "$H17/.claude-disk-janitor/.housekeeper.lock" 2>/dev/null || echo '')"
if [ "$AFTER_INODE" != "$PEER_INODE" ]; then
  fail "the release must not remove a lock this process does not own"
fi

# 18. The moved-aside copy is cleaned up when it is ours, and kept when it
# holds a live peer's lock.
#
# The peer flag used to be set before every restore attempt, which made the
# EXIT trap's cleanup clause unreachable:  an interrupted takeover -- a signal
# between the move and the removal -- orphaned the copy for good.  The flag
# is now set only where the re-check proved the copy is a LIVE peer's lock.
H18="$LOCK_TEST_ROOT/case18"
mkdir -p "$H18/.claude-disk-janitor"
mkdir -p "$H18/.claude-disk-janitor/.housekeeper.lock.stale-1.2"
run_trap "$H18" 0 ''
if [ -d "$H18/.claude-disk-janitor/.housekeeper.lock.stale-1.2" ]; then
  fail "the EXIT trap must remove a moved-aside copy that is ours"
fi
H18B="$LOCK_TEST_ROOT/case18b"
mkdir -p "$H18B/.claude-disk-janitor"
mkdir -p "$H18B/.claude-disk-janitor/.housekeeper.lock.stale-1.2"
run_trap "$H18B" 1 ''
if [ ! -d "$H18B/.claude-disk-janitor/.housekeeper.lock.stale-1.2" ]; then
  fail "the EXIT trap must not remove a moved-aside copy that holds a peer's lock"
fi
# The flag is set only where the re-check proves the copy is live, so it must
# not be set anywhere earlier in the acquire section.
PEER_FLAG_LINE="$(printf '%s\n' "$ACQUIRE_BLOCK" | grep -n 'HOUSEKEEPER_STALE_IS_PEER=1' | head -1 | cut -d: -f1)"
LIVE_PROOF_LINE="$(printf '%s\n' "$ACQUIRE_BLOCK" | grep -n 'Fresh after all' | head -1 | cut -d: -f1)"
if [ -z "$PEER_FLAG_LINE" ] || [ -z "$LIVE_PROOF_LINE" ]; then
  fail "the acquire section must set the peer flag where the re-check proves the copy is live"
fi
if [ "$PEER_FLAG_LINE" -lt "$LIVE_PROOF_LINE" ]; then
  fail "the peer flag must be set only where the re-check proves the moved copy is a LIVE peer's lock"
fi

# 19. A refused restore names the moved-aside copy.
#
# The copy stays where it is when the canonical path is taken, and a copy
# nobody can name is a copy nobody ever cleans up.
H19="$LOCK_TEST_ROOT/case19"
mkdir -p "$H19/bin" "$H19/.claude-disk-janitor"
mkdir -p "$H19/.claude-disk-janitor/.housekeeper.lock"
touch -t 202001010000 "$H19/.claude-disk-janitor/.housekeeper.lock"
cat > "$H19/bin/find" <<'SH'
#!/bin/bash
# The age checks are counted:  the first one sees a stale lock, the second
# one sees a lock that is fresh again -- a peer replaced it between the
# staleness check and the move -- and that peer also takes the canonical
# path, so the restore has to refuse.
has_age=0
for a in "$@"; do
  if [ "$a" = "+120" ]; then has_age=1; fi
done
if [ "$has_age" = "0" ]; then
  exec /bin/find "$@"
fi
n="$(cat "$FIND_COUNT_FILE" 2>/dev/null || echo 0)"
n=$((n + 1))
echo "$n" > "$FIND_COUNT_FILE"
if [ "$n" = "1" ]; then
  echo "$1"
  exit 0
fi
mkdir -p "$CANONICAL_LOCK_PATH"
exit 0
SH
chmod +x "$H19/bin/find"
OUT="$(PATH="$H19/bin:$PATH" FIND_COUNT_FILE="$H19/count" \
  CANONICAL_LOCK_PATH="$H19/.claude-disk-janitor/.housekeeper.lock" \
  HOME="$H19" HOUSEKEEPER_LOCK_OWNER= bash -c "$LOCK_BLOCK" 2>&1 || true)"
if ! printf '%s' "$OUT" | grep -q 'the moved-aside copy is at'; then
  fail "a refused restore must say where the moved-aside copy is"
fi
if ! printf '%s' "$OUT" | grep -q '.housekeeper.lock.stale-'; then
  fail "a refused restore must name the moved-aside copy's path"
fi

# 20. A dangling symlink at the canonical path is peer contention.
#
# [ -e ] follows a symlink, so a DANGLING symlink at the canonical path
# reads as absent, and a check that asks only [ -e ] turns a lost reclaim
# race into a hard error.  The reason the path is taken has to come from the
# mkdir's own exit status, where a dangling symlink is EEXIST like any other
# taken path.
#
# The symlink below is STALE, so it cannot pass on the age rule alone:  the
# block has to walk the whole takeover, find the removal impossible and put
# the copy back.
if printf '%s\n' "$ACQUIRE_BLOCK" | grep -qE '\[ *(! *)?-[eL][[:space:]]+"\$HOUSEKEEPER_LOCK"'; then
  fail "the acquire section must not decide peer contention by inspecting the lock path"
fi
if ! printf '%s\n' "$ACQUIRE_BLOCK" | grep -q '= "17"'; then
  fail "the acquire section must classify a taken path from the mkdir's own exit status"
fi
H20="$LOCK_TEST_ROOT/case20"
mkdir -p "$H20/.claude-disk-janitor"
ln -s "$H20/.claude-disk-janitor/not-there" "$H20/.claude-disk-janitor/.housekeeper.lock"
touch -h -t 202001010000 "$H20/.claude-disk-janitor/.housekeeper.lock"
OUT="$(run_lock_block "$H20" 2>&1 || true)"
if ! printf '%s' "$OUT" | grep -q 'skipped, peer holds housekeeper lock'; then
  fail "a stale dangling symlink at the canonical path must be treated as peer contention"
fi
if printf '%s' "$OUT" | grep -q 'failed to create housekeeper lock'; then
  fail "a stale dangling symlink at the canonical path must not be reported as a lock that could not be created"
fi

# 21. A move-aside that failed for a real reason is a hard error.
#
# The move reports WHY it failed:  17 (EEXIST) is a destination that is
# already taken, 2 (ENOENT) is a source another caller already took, and
# every other errno is a genuine failure -- permissions, no space, a
# cross-device rename.  Only a lost race may be reported as peer contention:
# a permissions failure reported as "a peer holds the lock" lets the cleanup
# continue while the resource is still there, and the failure disappears.
H21="$LOCK_TEST_ROOT/case21"
mkdir -p "$H21"
make_lock "$H21"
touch -t 202001010000 "$H21/.claude-disk-janitor/.housekeeper.lock"
BLOCK21="$(block_with_stub "$LOCK_BLOCK" "$STUB_RENAME_EACCES")"
OUT="$(HOME="$H21" HOUSEKEEPER_LOCK_OWNER= bash -c "$BLOCK21"$'\n''[ -d "$HOUSEKEEPER_LOCK" ] && echo LOCK_HELD=yes' 2>&1 || true)"
if printf '%s' "$OUT" | grep -q 'skipped, peer holds housekeeper lock'; then
  fail "a move-aside that failed with a real errno must not be reported as peer contention"
fi
if ! printf '%s' "$OUT" | grep -q 'failed to move the stale housekeeper lock aside'; then
  fail "a move-aside that failed with a real errno must be a hard error"
fi
if printf '%s' "$OUT" | grep -q 'LOCK_HELD=yes'; then
  fail "a move-aside that failed must not leave the lock acquired"
fi

# 22. A move-aside whose source is gone is a lost race, not an error.
#
# ENOENT means the lock directory vanished between the staleness check and
# the move:  another caller won the race and released it.  That is a skip,
# and the lock must not be acquired afterwards either.
H22="$LOCK_TEST_ROOT/case22"
mkdir -p "$H22"
make_lock "$H22"
touch -t 202001010000 "$H22/.claude-disk-janitor/.housekeeper.lock"
BLOCK22="$(block_with_stub "$LOCK_BLOCK" "$STUB_RENAME_ENOENT")"
OUT="$(HOME="$H22" HOUSEKEEPER_LOCK_OWNER= bash -c "$BLOCK22"$'\n''[ -d "$HOUSEKEEPER_LOCK" ] && echo LOCK_HELD=yes' 2>&1 || true)"
if ! printf '%s' "$OUT" | grep -q 'skipped, peer holds housekeeper lock'; then
  fail "a move-aside that lost the race must be reported as peer contention"
fi
if printf '%s' "$OUT" | grep -q 'failed to move the stale housekeeper lock aside'; then
  fail "a move-aside that lost the race must not be a hard error"
fi
if printf '%s' "$OUT" | grep -q 'LOCK_HELD=yes'; then
  fail "a move-aside that lost the race must not acquire the lock afterwards"
fi
if [ ! -d "$H22/.claude-disk-janitor/.housekeeper.lock" ]; then
  fail "a refused takeover must leave the lock it did not create in place"
fi

# 23. A FRESH lock whose owner file names a dead pid is taken over at once.
#
# The watcher removes the lock directory before it clears the owner file, so a
# removal that FAILED leaves both behind.  If nothing here reads the owner
# file, that surviving lock wedges every reclaimer in this repo for the full
# two-hour stale window even though nobody holds it -- which is the exact
# wedge the preserved owner file exists to remove.
OWNER_GONE_BLOCK="$(sed -n '/^housekeeper_owner_is_gone() {/,/^}/p' "$SCRIPT")"
if [ -z "$OWNER_GONE_BLOCK" ]; then
  fail "could not extract housekeeper_owner_is_gone from mac-auto-cleanup.sh"
fi

# A pid that has definitely exited, so "the holder is gone" is a fact here
# rather than a hope.
DEAD_PID="$(python3 -c 'import subprocess, sys
p = subprocess.Popen([sys.executable, "-c", "pass"])
p.wait()
print(p.pid)')"
if [ -z "$DEAD_PID" ]; then
  fail "could not obtain an exited pid for the housekeeper owner check"
fi

# The owner file must be NEWER than the lock directory it sits beside, and the
# lock itself has to stay FRESH -- a lock older than the stale window would be
# taken over by the age rule and prove nothing about the owner file.  The
# stamps are set explicitly rather than left to write order, and both land
# seconds apart from now, so the lock is well inside the 120 minute window.
stamp_owner_lock() {
  python3 -c 'import os, sys, time
lock, owner, owner_offset = sys.argv[1:4]
now = time.time()
os.utime(lock, (now - 5, now - 5))
os.utime(owner, (now + float(owner_offset), now + float(owner_offset)))' \
    "$1/.claude-disk-janitor/.housekeeper.lock" \
    "$1/.claude-disk-janitor/.housekeeper.lock.owner" "$2"
}

make_owner_lock() {
  # $1 = HOME, $2 = pid to record, $3 = host to record, $4 = owner mtime
  # offset in seconds from the lock's, defaulting to newer than the lock,
  # $5 = the process start token to record, defaulting to the one that pid
  # actually has.
  #
  # The record carries the schema this protocol publishes EXACTLY, because
  # both implementations check the shape strictly and a partial record would
  # be refused as malformed rather than consulted.
  make_lock "$1"
  python3 -c 'import json, os, subprocess, sys
home, pid, host = sys.argv[1:4]
start = sys.argv[4] if len(sys.argv) > 4 else ""
if not start:
    # The start time of the process the record names, read the way the
    # implementations read it.  A pid that has already exited has no start
    # time any more, and the record keeps a synthetic one:  a dead holder is
    # recognised by its pid, not by its start time.
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C"}
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", pid],
            capture_output=True, text=True, timeout=10, check=False, env=env,
        )
        start = result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        start = ""
if not start:
    start = "Mon Jan  1 00:00:00 2024"
with open(os.path.join(home, ".claude-disk-janitor", ".housekeeper.lock.owner"), "w") as fh:
    json.dump({
        "pid": int(pid),
        "host": host,
        "acquired_at": 1700000000.0,
        "lock": os.path.join(home, ".claude-disk-janitor", ".housekeeper.lock"),
        "start": start,
    }, fh)' "$1" "$2" "$3" "${5:-}"
  stamp_owner_lock "$1" "${4:-5}"
}

# The malformed-owner case needs the same mtime relationship, so what refuses
# it is the age rule and not an owner file that is merely too old.
make_bad_owner_lock() {
  make_lock "$1"
  echo '{ not json' > "$1/.claude-disk-janitor/.housekeeper.lock.owner"
  stamp_owner_lock "$1" 5
}

check_owner_gone() {
  # $1 = HOME, $2 = expected answer: yes means the holder is provably gone and
  # the lock may be taken over.
  local answer=no
  if HOME="$1" bash -c "$OWNER_GONE_BLOCK"$'\n''housekeeper_owner_is_gone "$HOME/.claude-disk-janitor/.housekeeper.lock"'; then
    answer=yes
  fi
  if [ "$answer" != "$2" ]; then
    fail "the housekeeper owner check answered '$answer' where '$2' was expected ($1)"
  fi
}

H23="$LOCK_TEST_ROOT/case23"
mkdir -p "$H23"
make_owner_lock "$H23" "$DEAD_PID" "$(hostname)"
check_owner_gone "$H23" yes
OUT="$(run_lock_block "$H23" 2>&1 || true)"
if ! printf '%s' "$OUT" | grep -q 'LOCK_HELD=yes'; then
  fail "a fresh lock whose owner file names a dead pid must be taken over at once; falling back to the age rule is a two-hour wedge on a lock nobody holds"
fi

# Every case where the owner file cannot speak for the lock falls back to the
# age rule, so a live holder is never displaced.
H23A="$LOCK_TEST_ROOT/case23-live"
mkdir -p "$H23A"
make_owner_lock "$H23A" "$$" "$(hostname)"
check_owner_gone "$H23A" no
OUT="$(run_lock_block "$H23A" 2>&1 || true)"
if ! printf '%s' "$OUT" | grep -q 'skipped, peer holds housekeeper lock'; then
  fail "a live holder's owner file must keep its lock"
fi

H23B="$LOCK_TEST_ROOT/case23-none"
mkdir -p "$H23B"
make_lock "$H23B"
check_owner_gone "$H23B" no
OUT="$(run_lock_block "$H23B" 2>&1 || true)"
if ! printf '%s' "$OUT" | grep -q 'skipped, peer holds housekeeper lock'; then
  fail "with no owner file the age rule must decide, because this script's own holder writes none"
fi

H23C="$LOCK_TEST_ROOT/case23-bad"
mkdir -p "$H23C"
make_bad_owner_lock "$H23C"
check_owner_gone "$H23C" no

# An owner file OLDER than the lock directory it sits beside describes an
# earlier holder, not this lock.
H23D="$LOCK_TEST_ROOT/case23-older"
mkdir -p "$H23D"
make_owner_lock "$H23D" "$DEAD_PID" "$(hostname)" -10
check_owner_gone "$H23D" no

H23E="$LOCK_TEST_ROOT/case23-other-host"
mkdir -p "$H23E"
make_owner_lock "$H23E" "$DEAD_PID" "some-other-host"
check_owner_gone "$H23E" no

# A LIVE pid whose start time does not match the record:  the kernel has
# handed that number to somebody else since the record was written, so the
# holder is gone even though the pid is alive.  Without the start time this
# case looks exactly like a live holder, and the lock waits out the whole
# stale window for a holder that is not there.
H23F="$LOCK_TEST_ROOT/case23-reused-pid"
mkdir -p "$H23F"
make_owner_lock "$H23F" "$$" "$(hostname)" 5 "Mon Jan  1 00:00:00 2024"
check_owner_gone "$H23F" yes
OUT="$(run_lock_block "$H23F" 2>&1 || true)"
if ! printf '%s' "$OUT" | grep -q 'LOCK_HELD=yes'; then
  fail "a reused pid must not keep a dead holder's lock until the stale window expires"
fi

# 24. The two implementations of the owner record AGREE.
#
# The owner file is shared state and TWO implementations read it:  the Python
# watcher (scripts/mac-resource-watch.py) and the embedded reader above.  They
# reached opposite conclusions about the same file -- the shell took a lock
# over that the watcher called malformed -- so this runs one table of records
# through both and fails when they disagree.
#
# The table is the whole shape of the protocol:  a good record, a record with
# a missing key, a record with an unknown key, records with values of the
# wrong type, a record for another host, a record older than the lock beside
# it, a record naming a dead pid, a record naming a live pid, and a record
# naming a live pid whose start time does not match -- the reused pid.  It
# also carries the two ways the readers used to disagree on a record that is
# well formed in every field but one:  a start token longer than the bound
# the watcher enforces, and a pid that belongs to another user, so signalling
# it fails with EPERM.
OWNER_GONE_FILE="$LOCK_TEST_ROOT/owner-gone.sh"
printf '%s\n' "$OWNER_GONE_BLOCK" > "$OWNER_GONE_FILE"
python3 - "$LOCK_TEST_ROOT/agreement" "$OWNER_GONE_FILE" "$ROOT/mac-resource-watch.py" <<'PY' || fail "the two owner readers disagreed"
import importlib.util, json, os, pathlib, shutil, socket, subprocess, sys, time

root = pathlib.Path(sys.argv[1])
block = pathlib.Path(sys.argv[2]).read_text(encoding="utf-8")
spec = importlib.util.spec_from_file_location("watch", sys.argv[3])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

shell_command = block + '\nhousekeeper_owner_is_gone "$HOME/.claude-disk-janitor/.housekeeper.lock"'
env = {"HOME": "", "PATH": os.environ.get("PATH", "/usr/bin:/bin")}

def start_token(pid):
    """The start time of a pid, read the way the protocol reads it.

    The table reads it ITSELF rather than asking the implementation, so the
    records it builds pin the format the protocol publishes:  a change to
    the implementation's own reading would show up here as a mismatch
    instead of quietly changing both sides at once.
    """
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C"}
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True, text=True, timeout=10, check=False, env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


# A pid that belongs to another user cannot be signalled, so os.kill raises
# EPERM for it.  The readers have to agree on what that means:  the process
# exists, so the check carries on to the start time, exactly as the watcher
# does.  The stub reaches the embedded reader through sitecustomize, which
# Python imports at startup, so the reader under test knows nothing about
# it.  The watcher runs in THIS process, so the same stub is installed here.
STUB_DIR = root / "eperm-stub"
STUB_DIR.mkdir(parents=True, exist_ok=True)
(STUB_DIR / "sitecustomize.py").write_text(
    'import os\n'
    'target = os.environ.get("MM_FAKE_EPERM_PID")\n'
    'if target:\n'
    '    real = os.kill\n'
    '    def kill(pid, sig):\n'
    '        if int(pid) == int(target):\n'
    '            raise PermissionError(13, "Permission denied")\n'
    '        return real(pid, sig)\n'
    '    os.kill = kill\n',
    encoding="utf-8",
)
_real_kill = os.kill
_eperm_pid = None


def _kill(pid, sig):
    if _eperm_pid is not None and int(pid) == _eperm_pid:
        raise PermissionError(13, "Permission denied")
    return _real_kill(pid, sig)


os.kill = _kill

host = socket.gethostname()
live_pid = os.getpid()
dead = subprocess.Popen([sys.executable, "-c", "pass"])
dead.wait()
assert start_token(live_pid), "the live pid must have a readable start token"


def record(rec_pid, lock_path, **overrides):
    base = {
        "pid": rec_pid,
        "host": host,
        "acquired_at": 1700000000.0,
        "lock": lock_path,
        "start": start_token(rec_pid) or "Mon Jan  1 00:00:00 2024",
    }
    base.update(overrides)
    return base


def shell_verdict(home, eperm_pid=None):
    run_env = {**env, "HOME": str(home)}
    if eperm_pid is not None:
        run_env["PYTHONPATH"] = str(STUB_DIR)
        run_env["MM_FAKE_EPERM_PID"] = str(eperm_pid)
    result = subprocess.run(
        ["bash", "-c", shell_command],
        env=run_env,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def watcher_verdict(home):
    lock = pathlib.Path(home) / ".claude-disk-janitor" / ".housekeeper.lock"
    mod.STATE_DIR = lock.parent
    mod.HOUSEKEEPER_LOCK = lock
    try:
        mtime = lock.stat().st_mtime
    except OSError:
        return False
    return mod._housekeeper_owner_is_gone(mtime)


def write_case(home, payload, owner_newer=True):
    state = home / ".claude-disk-janitor"
    state.mkdir(parents=True, exist_ok=True)
    lock = state / ".housekeeper.lock"
    lock.mkdir(exist_ok=True)
    now = time.time()
    os.utime(lock, (now - 5, now - 5))
    owner = state / ".housekeeper.lock.owner"
    owner.write_text(
        payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8"
    )
    offset = 5 if owner_newer else -10
    os.utime(owner, (now + offset, now + offset))


# The last element of a case is the pid the EPERM stub applies to, or None
# for a case that signs normally.
CASES = [
    ("a dead pid", True, lambda lock: record(dead.pid, lock), True, None),
    ("a live pid", False, lambda lock: record(live_pid, lock), True, None),
    ("a reused pid", True,
     lambda lock: record(live_pid, lock, start="Mon Jan  1 00:00:00 2024"), True, None),
    ("a missing key", False,
     lambda lock: {k: v for k, v in record(dead.pid, lock).items() if k != "start"}, True, None),
    ("an unknown key", False,
     lambda lock: dict(record(dead.pid, lock), unexpected="x"), True, None),
    ("a pid that is a string", False,
     lambda lock: record(dead.pid, lock, pid="1"), True, None),
    ("a pid that is a bool", False,
     lambda lock: record(dead.pid, lock, pid=True), True, None),
    ("an empty start", False,
     lambda lock: record(dead.pid, lock, start=""), True, None),
    ("an overlong start", False,
     lambda lock: record(dead.pid, lock, start="x" * 200), True, None),
    ("another host", False,
     lambda lock: record(dead.pid, lock, host="some-other-host"), True, None),
    ("an owner older than the lock", False,
     lambda lock: record(dead.pid, lock), False, None),
    ("a file that is not json", False, lambda lock: "{ this is not json", True, None),
    # A live pid that belongs to another user cannot be signalled, so the
    # check has to treat EPERM as "the process exists" and go on to the
    # start time.  The start time here does not match, so the holder is gone
    # and both readers have to say so.
    ("a pid that is not ours to signal", True,
     lambda lock: record(live_pid, lock, start="Mon Jan  1 00:00:00 2024"), True,
     live_pid),
]

problems = []
for name, expected, build, owner_newer, eperm_pid in CASES:
    home = root / name.replace(" ", "-")
    shutil.rmtree(home, ignore_errors=True)
    home.mkdir(parents=True)
    lock = home / ".claude-disk-janitor" / ".housekeeper.lock"
    write_case(home, build(str(lock)), owner_newer)
    _eperm_pid = eperm_pid
    shell = shell_verdict(home, eperm_pid)
    watcher = watcher_verdict(home)
    _eperm_pid = None
    if shell != watcher:
        problems.append("%s: the shell said %s and the watcher said %s" % (name, shell, watcher))
    elif shell != expected:
        problems.append("%s: both said %s where %s was expected" % (name, shell, expected))
if problems:
    raise SystemExit("; ".join(problems))
print("both owner readers agree on every case")
PY

# AFC-504: the remote hop fails closed.  Every way it can go wrong -- a missing
# target, a failed connectivity check, a failed maintenance command -- ends the
# run with a nonzero exit that names the missing variable or the failed check,
# and with exit code 75, which is the code that means the local cleanup ran and
# only the remote hop failed.  A hop that skips in silence is the failure this
# rules out.
#
# The block is run on its own with a stubbed ssh, so the check is about what the
# script decides and never reaches a real host.
HOP_ROOT="$LOCK_TEST_ROOT/hop"
mkdir -p "$HOP_ROOT"
HOP_BLOCK="$HOP_ROOT/block.sh"
sed -n '/^# 5\. Remote Hetzner Coolify maintenance trigger/,/^# end remote Hetzner maintenance trigger/p' \
  "$SCRIPT" > "$HOP_BLOCK"
if [ ! -s "$HOP_BLOCK" ]; then
  fail "the remote hop block is missing from the script"
fi

# The stub counts its calls and exits with the code the case asked for, so a
# probe that succeeds and a maintenance command that fails are two different
# hosts rather than one.  It also writes a diagnostic to stderr, because the
# failure message has to carry ssh's own reason and not only its exit code.
cat > "$HOP_ROOT/ssh-stub" <<'STUB'
#!/bin/bash
n=$(cat "$HOP_CASE/count" 2>/dev/null || echo 0)
n=$((n + 1))
echo "$n" > "$HOP_CASE/count"
echo "$@" >> "$HOP_CASE/calls"
codes=($HOP_CODES)
index=$((n - 1))
if [ "$index" -ge "${#codes[@]}" ]; then index=$(( ${#codes[@]} - 1 )); fi
echo "ssh-stub diagnostic for call $n" >&2
exit "${codes[$index]}"
STUB
chmod +x "$HOP_ROOT/ssh-stub"

hop_run() {
  # $1 the case name, $2 the exit codes (one per call, the last one repeating),
  # $3 the alias value ("unset" leaves it out), $4 the inventory flag
  HOP_CASE="$HOP_ROOT/$1"
  rm -rf "$HOP_CASE"
  mkdir -p "$HOP_CASE/bin"
  cp "$HOP_ROOT/ssh-stub" "$HOP_CASE/bin/ssh"
  if [ "$3" = "unset" ]; then
    env -u HOUSEKEEPER_SSH_ALIAS PATH="$HOP_CASE/bin:$PATH" \
      HOUSEKEEPER_INVENTORY="$4" HOP_CASE="$HOP_CASE" HOP_CODES="$2" \
      bash "$HOP_BLOCK" > "$HOP_CASE/out" 2> "$HOP_CASE/err"
  else
    env PATH="$HOP_CASE/bin:$PATH" HOUSEKEEPER_SSH_ALIAS="$3" \
      HOUSEKEEPER_INVENTORY="$4" HOP_CASE="$HOP_CASE" HOP_CODES="$2" \
      bash "$HOP_BLOCK" > "$HOP_CASE/out" 2> "$HOP_CASE/err"
  fi
}

# A reachable host: the hop runs and the maintenance command is the one asked
# for.
HOP_STATUS=0
hop_run reachable 0 target 1 || HOP_STATUS=$?
if [ "$HOP_STATUS" != "0" ]; then
  fail "a reachable host must let the remote hop succeed, it exited $HOP_STATUS"
fi
if ! grep -q 'Triggering remote Hetzner Coolify maintenance' "$HOP_ROOT/reachable/out"; then
  fail "a reachable host must say that it is triggering the remote maintenance"
fi
if ! grep -q 'coolify-auto-maintenance' "$HOP_ROOT/reachable/calls"; then
  fail "a reachable host must run the remote maintenance command"
fi

# An unreachable host: a real fault, so it says so and fails the run with the
# code that means the local cleanup ran.
HOP_STATUS=0
hop_run unreachable 255 target 1 || HOP_STATUS=$?
if [ "$HOP_STATUS" != "75" ]; then
  fail "a failed connectivity check must fail the run with the hop-failed code 75, it exited $HOP_STATUS"
fi
if ! grep -q 'Remote Hetzner maintenance trigger failed' "$HOP_ROOT/unreachable/err"; then
  fail "a failed connectivity check must say so on stderr"
fi
if ! grep -q 'connectivity check' "$HOP_ROOT/unreachable/err"; then
  fail "a failed connectivity check must say which check failed"
fi
if ! grep -q 'ssh-stub diagnostic' "$HOP_ROOT/unreachable/err"; then
  fail "the failure message must carry ssh's own reason, not only its exit code"
fi

# A reachable host whose maintenance command fails: also a real fault, and the
# status of the command is not thrown away.
HOP_STATUS=0
hop_run maintenance-fails "0 255" target 1 || HOP_STATUS=$?
if [ "$HOP_STATUS" != "75" ]; then
  fail "a failed maintenance command must fail the run with the hop-failed code 75, it exited $HOP_STATUS"
fi
if ! grep -q 'the maintenance command exited' "$HOP_ROOT/maintenance-fails/err"; then
  fail "a failed maintenance command must say so on stderr"
fi
if ! grep -q 'ssh-stub diagnostic' "$HOP_ROOT/maintenance-fails/err"; then
  fail "the failure message must carry ssh's own reason, not only its exit code"
fi

# An inventory file that names no target: a misconfiguration, so it says so and
# fails the run.
HOP_STATUS=0
hop_run misconfigured 0 unset 1 || HOP_STATUS=$?
if [ "$HOP_STATUS" != "75" ]; then
  fail "an inventory file with no target must fail the run with the hop-failed code 75, it exited $HOP_STATUS"
fi
if ! grep -q 'HOUSEKEEPER_SSH_ALIAS is required' "$HOP_ROOT/misconfigured/err"; then
  fail "an inventory file with no target must name HOUSEKEEPER_SSH_ALIAS"
fi

# Neither an inventory file nor the variable: the target does not resolve, so
# the hop fails closed and says which variable is missing.  It never reaches for
# a host it cannot name, and it never reports success.
HOP_STATUS=0
hop_run no-target 0 unset 0 || HOP_STATUS=$?
if [ "$HOP_STATUS" != "75" ]; then
  fail "a hop with no target must fail the run with the hop-failed code 75, it exited $HOP_STATUS"
fi
if ! grep -q 'HOUSEKEEPER_SSH_ALIAS is required' "$HOP_ROOT/no-target/err"; then
  fail "a hop with no target must name HOUSEKEEPER_SSH_ALIAS"
fi
if [ -s "$HOP_ROOT/no-target/calls" ]; then
  fail "a hop with no target must not reach for a host"
fi

# AFC-504: the script reads the operator's own inventory file, so a private
# target never has to be written into a tracked file and launchd still finds it.
# The block is run on its own with a temporary home, so the check is about what
# the script reads and never reaches a real host.
INV_BLOCK="$HOP_ROOT/inventory.sh"
sed -n '/^# begin operator inventory (AFC-504)/,/^# end operator inventory (AFC-504)/p' \
  "$SCRIPT" > "$INV_BLOCK"
if [ ! -s "$INV_BLOCK" ]; then
  fail "the operator inventory block is missing from the script"
fi

INV_HOME="$HOP_ROOT/home"
mkdir -p "$INV_HOME/.config/fleet"
printf 'HOUSEKEEPER_SSH_ALIAS=target\n' > "$INV_HOME/.config/fleet/housekeeper.env"

inv_read() {
  # $1 the home directory, $2 the file the readback goes to
  env -u HOUSEKEEPER_SSH_ALIAS -u HOUSEKEEPER_INVENTORY HOME="$1" bash -c \
    '. "$1"; printf "%s\n%s\n" "$HOUSEKEEPER_INVENTORY" "$HOUSEKEEPER_SSH_ALIAS"' \
    _ "$INV_BLOCK" > "$2" 2>&1
}

inv_read "$INV_HOME" "$HOP_ROOT/readback"
if [ "$(sed -n 1p "$HOP_ROOT/readback")" != "1" ]; then
  fail "an operator inventory file must set HOUSEKEEPER_INVENTORY to 1"
fi
if [ "$(sed -n 2p "$HOP_ROOT/readback")" != "target" ]; then
  fail "an operator inventory file must supply HOUSEKEEPER_SSH_ALIAS to the script"
fi

INV_EMPTY="$HOP_ROOT/empty-home"
mkdir -p "$INV_EMPTY"
inv_read "$INV_EMPTY" "$HOP_ROOT/readback-empty"
if [ "$(sed -n 1p "$HOP_ROOT/readback-empty")" != "0" ]; then
  fail "with no operator inventory file the script must not claim to have one"
fi

# A half-written inventory file must not abort the run.  An unbound variable
# inside the operator's file is fatal in bash while `set -u` is on, so the block
# suspends it for the source and restores it right after.
INV_BAD="$HOP_ROOT/bad-home"
mkdir -p "$INV_BAD/.config/fleet"
printf 'HOUSEKEEPER_SSH_ALIAS="$BOX"\n' > "$INV_BAD/.config/fleet/housekeeper.env"
BAD_STATUS=0
env -u HOUSEKEEPER_SSH_ALIAS -u HOUSEKEEPER_INVENTORY HOME="$INV_BAD" bash -c '
set -u
. "$1"
printf "readback:%s\n" "${HOUSEKEEPER_INVENTORY:-unset}"
printf "%s" "$DEFINITELY_NOT_SET"
' _ "$INV_BLOCK" > "$HOP_ROOT/readback-bad" 2>&1 || BAD_STATUS=$?
if ! grep -q 'readback:1' "$HOP_ROOT/readback-bad"; then
  fail "an unbound variable in the operator inventory file must not abort the run"
fi
if [ "$BAD_STATUS" = "0" ]; then
  fail "the operator inventory block must restore set -u after it sources the file"
fi

bash -n "$SCRIPT" || fail "bash -n failed"

echo OK
