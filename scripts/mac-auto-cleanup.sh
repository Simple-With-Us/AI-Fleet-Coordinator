#!/bin/bash
# Mac Automated Maintenance Script
# Safely prunes developer caches, Xcode symbols, unavailable simulators,
# package manager caches, old agent logs, and triggers remote Hetzner Docker cleanup.
# Does not reap git worktrees or live ~/.grok/worktrees (disk-janitor owns that).

set -u

# begin operator inventory (AFC-504)
# The operator's own inventory file, outside this repository.  It carries the
# private deployment values this script needs -- the ssh target of the origin box
# among them -- so none of them is ever written into a tracked file.  The file is
# optional: with no file the script runs on whatever the environment gives it.
HOUSEKEEPER_ENV_FILE="${HOUSEKEEPER_ENV_FILE:-$HOME/.config/fleet/housekeeper.env}"
HOUSEKEEPER_INVENTORY=0
if [ -r "$HOUSEKEEPER_ENV_FILE" ]; then
  # shellcheck disable=SC1090
  # An unbound variable inside the operator's file must not abort the run, so
  # `set -u` is suspended for the source and restored right after it.
  set +u
  . "$HOUSEKEEPER_ENV_FILE"
  set -u
  HOUSEKEEPER_INVENTORY=1
  export HOUSEKEEPER_SSH_ALIAS
fi
# end operator inventory (AFC-504)

PRESSURE=0
for arg in "$@"; do
  case "$arg" in
    --pressure) PRESSURE=1 ;;
  esac
done
if [ "${MAC_CLEANUP_PRESSURE:-0}" = "1" ]; then
  PRESSURE=1
fi

# Re-entrant owner-token lock.  Two independent invokers reach this script --
# com.jay.mac-cleanup (StartInterval 14400) and mac-resource-watch's run_cleanup() --
# and it had no mutual exclusion, so both could sweep at once and thrash a 16G host.
#
# It must be RE-ENTRANT, not a plain lock: run_cleanup() spawns this script AND
# janitor.sh as children while the parent already holds the lock.  A shared plain lock
# would make both children exit 0 with "peer holds lock" -- cleanup would silently never
# run while reporting success.  Callers that already hold it export HOUSEKEEPER_LOCK_OWNER=1.
#
# The lock is the directory ~/.claude-disk-janitor/.housekeeper.lock and it stays EMPTY:
# it is a marker, not a workspace.  A holder that records who it is does it in the
# SIBLING file <lock>.owner, because the release is a plain rmdir and rmdir refuses
# a directory that has anything in it.
#
# Three rules make the takeover safe.  Every implementation of this lock has to agree
# on them, because they all share one lock directory:
#
#   * The staleness is decided AT THE CANONICAL PATH, before anything is moved.
#     Moving a live peer's lock out of the way leaves the canonical path briefly
#     absent, and a third reclaimer can mkdir it and sweep at the same time --
#     the double sweep this lock exists to prevent.
#   * A stale lock is taken over by MOVING it aside.  Checking that a lock is
#     stale and then removing it is two steps, and two reclaimers can interleave
#     them: both see the stale lock, one removes it and creates a fresh one, and
#     the other then removes that fresh, empty lock and creates its own, so both
#     believe they hold it.  A rename is atomic, so only one caller can win it.
#   * The release is OWNERSHIP CHECKED.  The lock remembers which directory it
#     created, by inode, and only removes that one, so a holder whose lock was
#     taken over as stale does not delete the new holder's lock on its way out.
#
# The peer in this repo is scripts/mac-resource-watch.py, which takes the same
# lock with the same move-aside takeover and the same ownership-checked
# release.  An equivalent lock helper in a sibling repository implements the
# same protocol.  They have to follow the same three rules above, because they
# all share one lock directory: a takeover that moves the lock aside rather
# than removing it, a release that only removes the lock it created, and the
# same 120 minute stale window.  An implementation that still removes the
# lock and recreates it can delete a fresh lock between a peer's staleness
# check and its recreation, and then two reclaimers sweep at once.
# The shell holder writes no owner file -- it recovers by the age rule -- so a peer
# that reads an owner file must fall back to the age rule when the file is older
# than the lock directory it sits beside.
HOUSEKEEPER_LOCK="$HOME/.claude-disk-janitor/.housekeeper.lock"
# The shared guard over this protocol.  See housekeeper_with_guard below.
HOUSEKEEPER_GUARD="$HOME/.claude-disk-janitor/.housekeeper.lock.guard"
HOUSEKEEPER_STALE=""
HOUSEKEEPER_LOCK_INODE=""
# 1 means the moved-aside copy holds a PEER's lock, so it must never be deleted.
HOUSEKEEPER_STALE_IS_PEER=0
# Where the guarded acquire section hands its state to the release.
HOUSEKEEPER_STATE_FILE=""

# Run a section of this script with the shared housekeeper guard held.
#
# Every implementation of this protocol -- this script, the Python watcher in
# this repo (scripts/mac-resource-watch.py) and an equivalent lock helper in a
# sibling repository -- serializes the critical sections of the lock with an
# exclusive flock on ONE file:
# ~/.claude-disk-janitor/.housekeeper.lock.guard.  It is a SIBLING of the
# lock directory and never inside it, because the lock is released with a
# plain rmdir and rmdir refuses a directory that has anything in it.  It is
# never deleted either:  deleting it would split the flock, and two processes
# could each hold a different inode and both believe they hold the guard.
#
# The critical section is the ownership validation, the stale takeover, the
# removal, the mkdir, the owner-file publication and every release path.  A
# takeover can replace the canonical lock while another implementation is
# validating it, so the validation and the removal have to be one decision
# against every implementation, not just against this script.
#
# The shell cannot hold an flock, and it cannot hold one across separate
# python3 calls either -- each would be a different process, and the lock
# would be released when that helper exits.  So each guarded section is ONE
# helper process:  it takes the guard, runs the section while the guard is
# held, and lets the guard go when it exits.
housekeeper_with_guard() {
    python3 - "$1" "$2" <<'PY'
import fcntl, os, subprocess, sys
guard, section = sys.argv[1], sys.argv[2]
try:
    os.makedirs(os.path.dirname(guard), exist_ok=True)
except FileExistsError:
    # The parent path exists as something that is not a directory:  the open
    # below reports it.
    pass
except OSError as exc:
    print("failed to take the housekeeper guard: %s" % exc, file=sys.stderr)
    sys.exit(70)
try:
    fd = os.open(guard, os.O_RDWR | os.O_CREAT, 0o600)
except OSError as exc:
    print("failed to take the housekeeper guard: %s" % exc, file=sys.stderr)
    sys.exit(70)
try:
    fcntl.flock(fd, fcntl.LOCK_EX)
    rc = subprocess.call(["/bin/bash", "-c", section])
finally:
    # Closing the descriptor releases the flock, on every path out of here.
    os.close(fd)
sys.exit(rc)
PY
}

# Create the lock directory, and report WHY it could not be created.
#
# The mkdir has to be observed AT THE SYSCALL.  A shell "if ! mkdir" throws
# the reason away, and re-inspecting the path afterwards is a second, racy
# step: a peer can remove the lock directory between the failed mkdir and the
# [ -e ... ] test, and the run then concludes that the mkdir failed for some
# other reason and dies with "failed to create housekeeper lock" when the
# truth is that it lost a race with a peer that has since gone away.  A lost
# race is a skip, not a hard error.
#
# Exit status:  0 the lock is ours,  17 (EEXIST) a peer holds the path,
# anything else a real error (permissions, no space, a parent that is not a
# directory).
housekeeper_try_mkdir() {
    python3 - "$1" <<'PY'
import errno, os, sys
try:
    os.mkdir(sys.argv[1])
except FileExistsError:
    sys.exit(errno.EEXIST)      # a peer holds the path
except OSError as exc:
    sys.exit(exc.errno or 1)     # permissions, no space, anything else
sys.exit(0)
PY
}

# Move a path onto a destination that must be FREE, refusing to replace
# anything that is already there.
#
# mv -n is NOT that primitive on macOS.  When the destination already exists
# as a directory, mv moves the source INSIDE it -- and -n does not stop that --
# so "mv -n stale canonical" against an occupied canonical path nests the
# peer's lock inside the new holder's lock directory instead of refusing.
# The result is a lock directory with contents in it, which rmdir refuses to
# remove, so the lock leaks for the whole stale window and every later
# reclaimer refuses to take it over because it is no longer empty.
#
# The check and the rename are two steps as well: a third reclaimer can mkdir
# the canonical path in between, and the move then nests the copy INSIDE the
# new lock instead of refusing.  So the move and the refusal have to be ONE
# decision, which is what renamex_np with RENAME_EXCL is.
#
# This is the same primitive the Python side of this repo uses
# (scripts/mac-resource-watch.py, _rename_exclusive).  The shell cannot import
# it, so this is a copy of the same syscall, and THE TWO HAVE TO STAY IN
# AGREEMENT:  every implementation of this lock shares one lock directory.
#
# Exit status:  0 the move happened,  17 (EEXIST) the destination is held by a
# peer,  2 (ENOENT) the source is gone because another caller won the race,
# anything else a real failure (permissions, no space, a cross-device rename).
housekeeper_rename_exclusive() {
    python3 - "$1" "$2" <<'PY'
import ctypes, errno, os, sys

# The macOS no-replace rename flag.  The same value the Python side uses.
RENAME_EXCL = 0x00000004

src, dst = sys.argv[1], sys.argv[2]
try:
    libc = ctypes.CDLL(None, use_errno=True)
    fn = libc.renamex_np
except (OSError, AttributeError):
    fn = None
if fn is not None:
    fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    fn.restype = ctypes.c_int
    if fn(os.fsencode(src), os.fsencode(dst), RENAME_EXCL) == 0:
        sys.exit(0)
    err = ctypes.get_errno()
    sys.exit(errno.EEXIST if err == errno.EEXIST else (err or 1))
# Where renamex_np does not exist, check and then rename.  It still refuses an
# occupied destination, but the check and the move are two steps again.
if os.path.lexists(dst):
    sys.exit(errno.EEXIST)
try:
    os.rename(src, dst)
except OSError as exc:
    sys.exit(exc.errno or 1)
sys.exit(0)
PY
}

# Put a moved-aside lock back, atomically and without replacing anybody else's
# lock.
#
# The move and the refusal are one decision (see housekeeper_rename_exclusive),
# so there is no window in which a third reclaimer can take the canonical path
# and have the moved-aside copy nested inside its lock.  When the canonical
# path is occupied there is nothing safe to do: the moved-aside copy stays
# where it is and the caller is told, because the alternative is clobbering a
# holder.
#
# It does NOT decide whether the copy is a peer's lock:  only the caller knows
# what the re-check proved about the copy, and it is the caller that sets the
# peer flag.
housekeeper_restore_lock() {
    housekeeper_rename_exclusive "$HOUSEKEEPER_STALE" "$HOUSEKEEPER_LOCK"
    rc=$?
    if [ "$rc" = "0" ]; then
        return 0
    fi
    if [ "$rc" = "17" ]; then
        # EEXIST:  a peer holds the canonical path, so the copy cannot go back.
        echo "failed to restore housekeeper lock; a peer holds the canonical path" >&2
    elif [ "$rc" = "2" ]; then
        # ENOENT:  the moved-aside copy is gone, so there is nothing to put
        # back.  That is NOT peer contention on the canonical path -- the
        # canonical path is free and the copy cannot be restored -- so it is a
        # failure like any other, and the caller must not sweep on.
        echo "failed to restore housekeeper lock; the moved-aside copy is gone" >&2
    else
        # Any other errno is a real failure:  permissions, no space, a
        # cross-device rename.
        echo "failed to restore housekeeper lock" >&2
    fi
    echo "the moved-aside copy is at $HOUSEKEEPER_STALE" >&2
    return 1
}

# Record what the release needs, for the guarded acquire section.
#
# $1 = "held" when this process owns the lock.  The inode is recorded only
# for a lock this process owns:  a peer that holds the lock must not have it
# removed on the way out.  The peer flag is this section's own, and it is set
# only where the re-check proved the moved-aside copy is a LIVE peer's lock.
housekeeper_record_state() {
    local inode=""
    if [ "${1:-}" = "held" ]; then
        inode="$(stat -f %i "$HOUSEKEEPER_LOCK" 2>/dev/null || echo '')"
    fi
    [ -n "$HOUSEKEEPER_STATE_FILE" ] || return 0
    {
        echo "LOCK_INODE=$inode"
        echo "STALE=$HOUSEKEEPER_STALE"
        echo "PEER=$HOUSEKEEPER_STALE_IS_PEER"
    } > "$HOUSEKEEPER_STATE_FILE"
}

# Whether the owner file proves the holder of a given lock directory is gone.
#
# This is what makes a lock whose release FAILED recoverable in one run
# instead of one stale window.  The watcher removes the lock directory before
# it clears the owner file (scripts/mac-resource-watch.py), so a removal that
# failed leaves BOTH behind:  without this check the surviving lock wedges
# every reclaimer for the full two hours, even though nobody holds it.
#
# It is the same rule the watcher applies, and it is deliberately
# conservative -- exit 0 (the holder is gone) ONLY when the file names a dead
# pid on THIS host, or a pid this host has since handed to somebody else, and
# was written after the lock directory it sits beside.  Anything else exits 1
# and the caller falls back to the age rule, which is what this script relies
# on for its own holder:  it writes no owner file.
#
# The argument is the lock directory to judge, which is the canonical lock at
# the first gate and the moved-aside copy at the second.  The owner file is a
# SIBLING of the canonical lock, so the second gate reads the same file.
housekeeper_owner_is_gone() {
    python3 - "$1" <<'PY'
import calendar, json, math, os, re, socket, subprocess, sys, time

lock_path = sys.argv[1]
# The moved-aside copy is named `<lock>.stale-<pid>.<uuid>`, so the owner file
# is always found under the CANONICAL name beside it -- the same file for
# both gates.
canonical = os.path.join(
    os.path.dirname(lock_path), os.path.basename(lock_path).split(".stale-")[0]
)
owner_path = canonical + ".owner"

# The exact shape of an owner record.  The Python watcher in this repo
# (scripts/mac-resource-watch.py) enforces the SAME shape, and the two
# implementations of this protocol have to agree:  a record one of them calls
# malformed and the other calls usable leaves them with conflicting ownership
# decisions about the same lock and the same file.
OWNER_KEYS = {"pid", "host", "acquired_at", "lock", "start"}

# The longest start token this protocol accepts.  Readers parse the fixed
# C-locale civil form for legacy local tokens; this bound keeps hostile input
# small, and both readers apply it at the same schema check.
OWNER_START_MAX = 128
OWNER_ACQUIRED_AT_FS_SKEW_SEC = 2.0
OWNER_ACQUIRED_AT_FUTURE_SKEW_SEC = 60.0


def process_start_token(pid, *, utc=False):
    """The start time of a pid, with UTC normalization available to readers.

    A pid alone does not name a process:  the kernel hands the same number to
    somebody else once the original exits, and a reused pid that is ALIVE
    looks exactly like a holder that is still running.  The start time tells
    the two apart.

    Writers remain on the current local form.  Readers request UTC while
    retaining C locale so new records have a stable cross-zone comparison.
    """
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C"}
    if utc:
        env["TZ"] = "UTC0"
    try:
        result = subprocess.run(
            ["/bin/ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True, text=True, timeout=10, check=False, env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def c_lstart_epoch(token):
    """Parse the fixed English civil timestamp emitted under LC_ALL=C."""
    match = re.fullmatch(
        r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun) "
        r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
        r"( {1,2}[1-9]| [12][0-9]| 3[01]) "
        r"([01][0-9]|2[0-3]):([0-5][0-9]):([0-5][0-9]) ([0-9]{4})",
        token,
    )
    if match is None:
        return None
    weekday, month_name, day_text, hour, minute, second, year = match.groups()
    months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    weekdays = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    try:
        civil = time.struct_time((
            int(year), months.index(month_name) + 1, int(day_text),
            int(hour), int(minute), int(second), 0, 0, 0,
        ))
        epoch = calendar.timegm(civil)
        parsed = time.gmtime(epoch)
        if (
            parsed[:6] != civil[:6]
            or parsed.tm_wday != weekdays.index(weekday)
        ):
            return None
        return float(epoch)
    except (ValueError, OverflowError):
        return None


def owner_acquired_at_is_sane(acquired_at, lock_mtime, owner_mtime):
    """Require acquired_at to agree with filesystem times and current time."""
    skew = OWNER_ACQUIRED_AT_FS_SKEW_SEC
    return (
        math.isfinite(acquired_at)
        and lock_mtime - skew <= acquired_at <= owner_mtime + skew
        and acquired_at <= time.time() + OWNER_ACQUIRED_AT_FUTURE_SKEW_SEC
    )


def owner_start_matches_current(start, current_utc, acquired_at, lock_mtime, owner_mtime):
    """Return same/different, or None when token evidence is unusable.

    Mirrors scripts/mac-resource-watch.py:  tokens more than 24 hours apart
    are a reused pid; within that window equality is only the fallback when
    the tokens or acquired_at cannot be checked.
    """
    exact = start == current_utc
    old_epoch = c_lstart_epoch(start)
    current_epoch = c_lstart_epoch(current_utc)
    if old_epoch is None or current_epoch is None:
        return True if exact else None
    if abs(old_epoch - current_epoch) > 24 * 60 * 60:
        return False
    if not owner_acquired_at_is_sane(acquired_at, lock_mtime, owner_mtime):
        return True if exact else None
    # The old token may match this process only if it began by publication
    # of the owner record; this rejects a later process reusing the pid.
    if current_epoch > acquired_at:
        return False
    return True


try:
    lock_mtime = os.stat(lock_path).st_mtime
    owner_mtime = os.stat(owner_path).st_mtime
except OSError:
    sys.exit(1)                      # no owner file, or no lock to weigh it against
if owner_mtime <= lock_mtime:
    sys.exit(1)                      # it describes an earlier holder, not this lock
try:
    with open(owner_path, encoding="utf-8") as handle:
        owner = json.load(handle)
except (OSError, ValueError):
    sys.exit(1)                      # unreadable or malformed: the age rule decides
if not isinstance(owner, dict) or set(owner) != OWNER_KEYS:
    sys.exit(1)                      # not the record this protocol publishes
host = owner["host"]
if not isinstance(host, str) or not host or host != socket.gethostname():
    sys.exit(1)                      # another host's pid says nothing about this one
pid = owner["pid"]
if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
    sys.exit(1)
acquired_at = owner["acquired_at"]
if isinstance(acquired_at, bool) or not isinstance(acquired_at, (int, float)):
    sys.exit(1)
try:
    finite = math.isfinite(acquired_at)
except OverflowError:
    sys.exit(1)                      # a hundreds-of-digits int is not a timestamp
if not finite:
    sys.exit(1)
lock = owner["lock"]
if not isinstance(lock, str) or not lock:
    sys.exit(1)
start = owner["start"]
if not isinstance(start, str) or not start or len(start) > OWNER_START_MAX:
    sys.exit(1)
try:
    os.kill(pid, 0)
except ProcessLookupError:
    sys.exit(0)                      # the holder is gone:  take the lock over
except OSError:
    # A permission error means the process exists and is merely not ours to
    # signal, so it is NOT proof that the holder is gone.  The check carries
    # on to the start time below, which is how the watcher reads it too:  a
    # pid owned by somebody else can be a reused pid just the same, and the
    # start time is what tells the holder from the impostor.
    pass
# The pid is ALIVE.  It is the holder only when it is the SAME process:  a
# reused pid looks exactly like a live holder, and believing it would leave a
# dead holder's lock in place until the whole stale window expires.
current = process_start_token(pid, utc=True)
if not current:
    sys.exit(1)                      # the start time could not be read:  the age rule decides
same_holder = owner_start_matches_current(
    start, current, acquired_at, lock_mtime, owner_mtime
)
sys.exit(0 if same_holder is False else 1)
PY
}

# The acquire section.  It runs inside the helper process with the shared
# guard held, so the mkdir, the staleness check, the takeover, the removal
# and the second mkdir are one decision against every implementation of this
# protocol.
#
# Exit status:  0 the lock is ours, 17 a peer holds it, 75 the moved-aside
# name was taken and the caller has to try another one, 76 a real error.
housekeeper_acquire_section() {
    # This section owns its own copy of the peer flag:  the release reads it
    # back from the state file.
    HOUSEKEEPER_STALE_IS_PEER=0
    housekeeper_try_mkdir "$HOUSEKEEPER_LOCK"
    rc=$?
    if [ "$rc" = "0" ]; then
        housekeeper_record_state held
        return 0
    fi
    if [ "$rc" != "17" ]; then
        # The mkdir failed for a reason other than EEXIST:  permissions, no
        # space, a parent that is not a directory.  That is a real error, and
        # reporting it as "peer holds lock" would skip the cleanup silently.
        housekeeper_record_state
        return 76
    fi
    # A peer's lock is there.  Decide staleness AT THE CANONICAL PATH first:
    # moving a live peer's lock aside would leave the canonical path briefly
    # absent, and a third reclaimer could mkdir it and sweep at the same
    # time.
    # A FRESH lock is still a live peer's unless its owner file proves the
    # holder is gone:  that is exactly what a failed removal leaves behind,
    # and waiting out the whole window for it is a two-hour wedge on a lock
    # nobody holds.
    if [ -z "$(find "$HOUSEKEEPER_LOCK" -maxdepth 0 -mmin +120 2>/dev/null)" ] &&
        ! housekeeper_owner_is_gone "$HOUSEKEEPER_LOCK"; then
        echo "skipped, peer holds housekeeper lock"
        housekeeper_record_state
        return 17
    fi
    # The lock must be EMPTY: it is a marker, not a workspace.  A lock
    # directory with something in it is not ours to take, and moving it
    # aside would displace a lock we do not own.
    if [ -n "$(find "$HOUSEKEEPER_LOCK" -mindepth 1 -maxdepth 1 2>/dev/null | head -1)" ]; then
        echo "skipped, peer holds housekeeper lock"
        housekeeper_record_state
        return 17
    fi
    # It is stale.  Take it by MOVING it aside: the move is atomic and it
    # refuses to clobber anything, so only one caller can win the takeover.
    housekeeper_rename_exclusive "$HOUSEKEEPER_LOCK" "$HOUSEKEEPER_STALE"
    rc=$?
    if [ "$rc" != "0" ]; then
        if [ "$rc" = "17" ]; then
            # The moved-aside name is taken:  the caller tries another one.
            return 75
        fi
        if [ "$rc" = "2" ]; then
            # ENOENT:  the source lock is gone, so another caller won the race
            # and released it.  A lost race is a skip, not an error.
            echo "skipped, peer holds housekeeper lock"
            housekeeper_record_state
            return 17
        fi
        # Every other errno is a real move failure -- permissions, no space, a
        # cross-device rename.  Reporting it as peer contention would let the
        # cleanup continue while the resource is still there, so it is a hard
        # error.
        echo "failed to move the stale housekeeper lock aside" >&2
        housekeeper_record_state
        return 76
    fi
    # The moved object may have been replaced between the check and the
    # rename; rename preserves mtime, so re-read its age.  The owner file has
    # to be weighed HERE as well as at the first gate:  this re-read would
    # otherwise put back a lock we already proved the holder had abandoned,
    # because the age it re-reads is the same fresh age the owner file
    # explained.
    if [ -z "$(find "$HOUSEKEEPER_STALE" -maxdepth 0 -mmin +120 2>/dev/null)" ] &&
        ! housekeeper_owner_is_gone "$HOUSEKEEPER_STALE"; then
        # Fresh after all: it belongs to a LIVE peer.  Put it back, and never
        # delete it:  this is the one path where the moved-aside copy holds a
        # live peer's lock, so this is where the peer flag is set.
        HOUSEKEEPER_STALE_IS_PEER=1
        housekeeper_record_state
        if ! housekeeper_restore_lock; then
            return 76
        fi
        echo "skipped, peer holds housekeeper lock"
        return 17
    fi
    if [ -n "$(find "$HOUSEKEEPER_STALE" -mindepth 1 -maxdepth 1 2>/dev/null | head -1)" ]; then
        # Not the empty marker we made.  Put it back.  The copy is not a live
        # peer's lock -- it was proven stale before the move -- so it stays
        # eligible for the EXIT cleanup.
        housekeeper_record_state
        if ! housekeeper_restore_lock; then
            return 76
        fi
        echo "skipped, peer holds housekeeper lock"
        return 17
    fi
    if ! rmdir "$HOUSEKEEPER_STALE" 2>/dev/null; then
        # The copy is ours to drop and it is empty, but the removal failed
        # anyway.  Put it back rather than sweep on a lock we do not hold.
        housekeeper_record_state
        if ! housekeeper_restore_lock; then
            return 76
        fi
        echo "skipped, peer holds housekeeper lock"
        return 17
    fi
    housekeeper_try_mkdir "$HOUSEKEEPER_LOCK"
    rc=$?
    if [ "$rc" != "0" ]; then
        if [ "$rc" = "17" ]; then
            # A peer took the canonical path in the window between the move
            # aside and here.  We do not hold the lock.
            echo "skipped, peer holds housekeeper lock"
            housekeeper_record_state
            return 17
        fi
        housekeeper_record_state
        return 76
    fi
    housekeeper_record_state held
    return 0
}

# The release section.  It runs inside the helper process with the shared
# guard held, so the ownership check and the removal are one decision:  a
# takeover can replace the canonical lock between the check and the removal,
# and the exiting holder would then remove the NEW holder's lock.
housekeeper_release_section() {
    # The moved-aside copy is ours to drop only when it does NOT hold a
    # peer's lock.  It is the copy this process emptied and took over, and an
    # interrupted takeover would otherwise orphan it for good.
    if [ "${HOUSEKEEPER_STALE_IS_PEER:-0}" != "1" ] && [ -n "$HOUSEKEEPER_STALE" ] && [ -e "$HOUSEKEEPER_STALE" ]; then
        rmdir "$HOUSEKEEPER_STALE" 2>/dev/null || true
    fi
    # The canonical lock is ours only if it is still the one we created.
    if [ -n "$HOUSEKEEPER_LOCK_INODE" ]; then
        if [ "$(stat -f %i "$HOUSEKEEPER_LOCK" 2>/dev/null || echo '')" = "$HOUSEKEEPER_LOCK_INODE" ]; then
            rmdir "$HOUSEKEEPER_LOCK" 2>/dev/null || true
        fi
    fi
}

mkdir -p "$(dirname "$HOUSEKEEPER_LOCK")" 2>/dev/null || true

# The trap is installed BEFORE the lock is taken, so an interrupted takeover
# cannot leave a moved-aside copy behind, and it only ever removes the lock
# this process created.
#
# It must not remove a moved-aside copy that holds a PEER's lock.  The copy is
# only ours when this process emptied it and took it over; in every other
# case it is a live peer's lock that we displaced for a moment, and deleting
# it frees the canonical path for a third reclaimer while that peer is still
# running -- the double sweep this lock exists to prevent.  The flag is set
# only where the re-check proved the copy is live, so an interrupted takeover
# -- a signal between the move and the removal -- still cleans its copy up.
#
# The release runs inside the guard, because the ownership check and the
# removal have to be one decision against every implementation of this
# protocol.
trap 'housekeeper_with_guard "$HOUSEKEEPER_GUARD" "housekeeper_release_section" >/dev/null 2>&1 || true' EXIT

# The guarded sections run in a child shell, so the functions and the state
# they read have to travel with them.
export -f housekeeper_with_guard housekeeper_try_mkdir housekeeper_rename_exclusive \
    housekeeper_restore_lock housekeeper_record_state housekeeper_acquire_section \
    housekeeper_release_section housekeeper_owner_is_gone
export HOUSEKEEPER_LOCK HOUSEKEEPER_GUARD HOUSEKEEPER_STALE HOUSEKEEPER_LOCK_INODE \
    HOUSEKEEPER_STALE_IS_PEER HOUSEKEEPER_STATE_FILE

if [ "${HOUSEKEEPER_LOCK_OWNER:-0}" != "1" ]; then
    # The state file carries the outcome of the guarded section back to this
    # shell:  the inode of the lock this process created, the moved-aside
    # copy, and whether that copy holds a live peer's lock.
    HOUSEKEEPER_STATE_FILE="$(mktemp "${TMPDIR:-/tmp}/housekeeper-state.XXXXXX" 2>/dev/null || echo '')"
    if [ -z "$HOUSEKEEPER_STATE_FILE" ]; then
        echo "failed to create the housekeeper lock state file" >&2
        exit 1
    fi
    export HOUSEKEEPER_STATE_FILE
    housekeeper_acquire_rc=0
    housekeeper_acquire_tries=0
    while :; do
        housekeeper_acquire_tries=$((housekeeper_acquire_tries + 1))
        # The moved-aside name is chosen HERE, so that the release knows where
        # an interrupted takeover left its copy even when the section never
        # got to report back.
        HOUSEKEEPER_STALE="$HOUSEKEEPER_LOCK.stale-$$.$RANDOM"
        export HOUSEKEEPER_STALE
        housekeeper_with_guard "$HOUSEKEEPER_GUARD" "housekeeper_acquire_section"
        housekeeper_acquire_rc=$?
        if [ "$housekeeper_acquire_rc" != "75" ] || [ "$housekeeper_acquire_tries" -ge 10 ]; then
            break
        fi
    done
    if [ -f "$HOUSEKEEPER_STATE_FILE" ]; then
        while IFS='=' read -r housekeeper_key housekeeper_value; do
            case "$housekeeper_key" in
                LOCK_INODE) HOUSEKEEPER_LOCK_INODE="$housekeeper_value" ;;
                STALE) HOUSEKEEPER_STALE="$housekeeper_value" ;;
                PEER) HOUSEKEEPER_STALE_IS_PEER="$housekeeper_value" ;;
            esac
        done < "$HOUSEKEEPER_STATE_FILE"
        rm -f "$HOUSEKEEPER_STATE_FILE"
        HOUSEKEEPER_STATE_FILE=""
    fi
    case "$housekeeper_acquire_rc" in
        0) ;;
        # A peer holds the lock:  nothing to do, and nothing to report as an
        # error.
        17) exit 0 ;;
        *)
            # The lock could not be taken for any reason other than a peer
            # holding it.  That is a real error, and reporting it as "peer
            # holds lock" would skip the cleanup silently.
            echo "failed to create housekeeper lock" >&2
            exit 1
            ;;
    esac
    # The lock is ours:  say so, so that the children this script spawns know
    # it is already held and do not try to take it again.
    export HOUSEKEEPER_LOCK_OWNER=1
    export HOUSEKEEPER_LOCK_INODE HOUSEKEEPER_STALE HOUSEKEEPER_STALE_IS_PEER
fi

echo "[$(date)] Starting Mac automated cleanup${PRESSURE:+ (pressure)}..."

# 1. Clean Xcode iOS DeviceSupport symbols, DerivedData, and Simulator Devices
if [ -d "$HOME/Library/Developer/Xcode/iOS DeviceSupport" ]; then
    echo "Pruning Xcode iOS DeviceSupport..."
    rm -rf "$HOME/Library/Developer/Xcode/iOS DeviceSupport"/* 2>/dev/null || true
fi

if [ -d "$HOME/Library/Developer/Xcode/DerivedData" ]; then
    echo "Pruning Xcode DerivedData..."
    rm -rf "$HOME/Library/Developer/Xcode/DerivedData"/* 2>/dev/null || true
fi

if [ -d "$HOME/Library/Developer/CoreSimulator/Caches" ]; then
    echo "Pruning CoreSimulator Caches..."
    rm -rf "$HOME/Library/Developer/CoreSimulator/Caches"/* 2>/dev/null || true
fi

# Never rm -rf Devices/*.  That deletes every simulator (installed apps,
# container data, in-progress ios-debug / TestFlight archives).  2026-08-12
# left CoreSimulator live because iOS needs it.  Only drop unavailable runtimes.
if command -v xcrun &>/dev/null; then
    echo "Pruning unavailable simulators..."
    # Never `simctl shutdown all`.  com.jay.mac-cleanup is StartInterval 14400
    # plus RunAtLoad, so that would kill every booted Simulator (ios-debug
    # --console, in-progress XCUITest, live previews) four times a day.
    # `delete unavailable` already no-ops on a booted unavailable device.
    xcrun simctl delete unavailable 2>/dev/null || true
fi

# 2. Package Managers Caches
# `npm cache clean --force` and `pnpm store prune` both touch content-addressed
# stores that a live `npm install`/`pnpm install` in some worktree is reading from
# mid-run.  Skip on this tick rather than risk pulling a package out from under an
# install; the next 4h tick (or pressure wake) catches it once things are idle.
if command -v npm &>/dev/null; then
    if pgrep -f 'npm (install|ci|update|prune)' >/dev/null 2>&1; then
        echo "npm install in progress, skipped cache clean"
    else
        echo "Pruning NPM cache..."
        npm cache clean --force 2>/dev/null || true
        rm -rf "$HOME/.npm/_npx" 2>/dev/null || true
    fi
fi

if command -v pnpm &>/dev/null; then
    if pgrep -f 'pnpm (install|update|add|import|dlx|link)' >/dev/null 2>&1; then
        echo "pnpm install in progress, skipped store prune"
    else
        echo "Pruning PNPM store..."
        pnpm store prune 2>/dev/null || true
    fi
fi

if command -v yarn &>/dev/null; then
    echo "Pruning Yarn cache..."
    yarn cache clean 2>/dev/null || true
fi

if command -v brew &>/dev/null; then
    echo "Running Homebrew cleanup..."
    brew cleanup -s 2>/dev/null || true
fi

# 2c. Hog Hunter reclaim engine.
#
# 2026-09-30 (BF-HOUSEKEEPER): this block ran the CleanMyMac CLI.  CleanMyMac is
# uninstalled -- `brew list` still shows a `cleanmymac-cli` entry but there is no
# Cellar dir and `command -v cleanmymac` exits 1, so every one of those calls was
# a silent no-op.  The four rules below that actually fired (junk, dev, ai,
# trash) had no local replacement, which is why disk drifted for months.
#
# The engine does strictly more than the CLI did, and the history above still
# applies: it NEVER deletes a log file that a running process holds open -- the
# `logs` rule TRUNCATES IN PLACE instead, which is the thing the CLI got wrong
# (deleting a live log leaves the daemon writing to an unlinked inode, so the
# space is not actually freed until that daemon restarts).  It also adds the
# SHRINK operations the CLI never had: APFS local snapshot pruning, SQLite WAL
# checkpointing, and tail-truncation.
#
# There is no RAM optimization and there never should be.  `optimize ram` purges
# resident pages into swapfiles on the same APFS container as user data, which
# converts RAM pressure into disk consumption -- measured +3.6 to +7.3G swap per
# run.  The engine has no such command; the ban is structural, not a flag.
#
# Tier gating lives in the engine, not here: it reads swap/load/disk itself and
# drops to the safe-only tier when the band is tight.  `--band=full` on a
# --pressure run lets it take the semi-safe tier (snapshots, device support,
# simulator runtimes) when -- and only when -- the band is calm.
HOGHUNTER_CLEAN="/Users/jay/Code/HogHunter/scripts/hoghunter-clean"
if [ -x "$HOGHUNTER_CLEAN" ]; then
    HH_BAND="cheap"
    [ "$PRESSURE" = "1" ] && HH_BAND="full"
    echo "Running Hog Hunter reclaim engine (band=$HH_BAND)..."
    "$HOGHUNTER_CLEAN" --clean --band="$HH_BAND" 2>&1 | sed 's/^/  /' || true
else
    echo "Hog Hunter reclaim engine not found at $HOGHUNTER_CLEAN -- skipping."
fi

# Cap runaway pm2 logs (always cheap).
if [ -d "$HOME/.pm2/logs" ]; then
    echo "Capping oversized pm2 logs..."
    find "$HOME/.pm2/logs" -type f -name '*.log' -size +50M -exec sh -c ': > "$1"' _ {} \; 2>/dev/null || true
fi
if [ -f "$HOME/.pm2/pm2.log" ]; then
    python3 - <<'PY'
import os
p = os.path.expanduser("~/.pm2/pm2.log")
try:
    if os.path.getsize(p) > 50 * 1024 * 1024:
        open(p, "w").close()
except OSError:
    pass
PY
fi

# Leftover vitest temp SQLite (grew to 130G once).
UT="$(getconf DARWIN_USER_TEMP_DIR 2>/dev/null | sed 's:/*$::')"
if [ -n "$UT" ] && [ -d "$UT" ]; then
    echo "Pruning stale vitest temp DBs..."
    find "$UT" -maxdepth 1 -name 'agentic-*' -mmin +360 -delete 2>/dev/null || true
fi

# 2b. Spotlight / Apple Intelligence PipelineStorage journals.
# The old path only cleared LSSR5EventsandordersUrgent/Journals (empty).
# 2026-09-01: Background/Embedding/Keyphrase/FullEmbedding Journals were
# ~8.5G logical each (~68G listed, ~9G APFS blocks after clones).  Truncate
# every Journals dir under PipelineStorage.  Regenerable.
SPOT_PIPE="$HOME/Library/Metadata/CoreSpotlight/DocumentProcessing/PipelineStorage"
if [ -d "$SPOT_PIPE" ]; then
    echo "Pruning Spotlight PipelineStorage journals..."
    killall knowledgeconstructiond corespotlightd mds_stores 2>/dev/null || true
    find "$SPOT_PIPE" -type d -name Journals -prune -exec rm -rf {} + 2>/dev/null || true
    find "$SPOT_PIPE" -type d -name HistoricalReports -prune -exec rm -rf {} + 2>/dev/null || true
    # Recreate Journals so daemons can reopen without mkdir races.
    find "$SPOT_PIPE" -mindepth 1 -maxdepth 1 -type d -exec mkdir -p {}/Journals \; 2>/dev/null || true
    rm -f "$SPOT_PIPE/StateStore.db" "$SPOT_PIPE/StateStore.db-wal" "$SPOT_PIPE/StateStore.db-shm"
fi

# 3. Agent caches.  Do not wipe ~/.grok/worktrees — those are live Grok
# checkouts.  com.jay.disk-janitor already retires idle nested scratch.
echo "Pruning agent archived sessions..."
rm -rf "$HOME/.codex/archived_sessions"/* 2>/dev/null || true
rm -rf "$HOME/.npm/_npx" 2>/dev/null || true

# Prune Grok sessions older than 7 days (3 days under pressure).
if [ -d "$HOME/.grok/sessions" ]; then
    GROK_SESSION_DAYS=7
    [ "$PRESSURE" = "1" ] && GROK_SESSION_DAYS=3
    echo "Pruning Grok sessions older than ${GROK_SESSION_DAYS} days..."
    GROK_SESSION_DAYS="$GROK_SESSION_DAYS" python3 - "$HOME/.grok/sessions" <<'PY'
import os, shutil, sys, time
from pathlib import Path
root = Path(sys.argv[1])
now = time.time()
cutoff = int(os.environ.get("GROK_SESSION_DAYS", "7")) * 86400
removed = 0
for dirpath, dirnames, filenames in os.walk(root, topdown=False):
    p = Path(dirpath)
    name = p.name
    if not (name.startswith("019") and len(name) >= 20):
        continue
    names = set(filenames)
    if "updates.jsonl" not in names and "chat_history.jsonl" not in names:
        continue
    try:
        newest = max((os.path.getmtime(os.path.join(dirpath, f)) for f in filenames), default=0)
    except OSError:
        continue
    if now - newest > cutoff:
        shutil.rmtree(p, ignore_errors=True)
        removed += 1
print(f"removed {removed} old grok sessions")
PY
fi

# Safe cleanup of old brain directories in Antigravity keeping current directory if present
if [ -d "$HOME/.gemini/antigravity/brain" ]; then
    find "$HOME/.gemini/antigravity/brain" -mindepth 1 -maxdepth 1 -type d -mtime +7 -exec rm -rf {} + 2>/dev/null || true
fi

# 3b. Reap node_modules and .next from suffixed/inactive feature worktrees in ~/apps/
# Only reap from verified git worktrees that are clean (ignoring build junk) and idle (>4h).
# Explicitly preserve standing runtimes like agent-sync-push, agy-acp-runtime, etc.
if [ "$PRESSURE" = "1" ]; then
echo "Pruning duplicate build caches on suffixed feature worktrees (pressure mode)..."
python3 <<'PY'
import os, glob, json, shutil, re, subprocess, time, fcntl

# Case-insensitive: this volume is, so Code/Fleet-OPS and Code/fleet-ops are one directory, and a wider match only keeps
# more.  Matched against the path as found AND its on-disk spelling (see canon below).
KEEP_RE = re.compile(
    r"^/Users/jay/apps/[a-z0-9]+-(claude|codex|live|antigravity|cursor|monet|grok|grok-build|deepseek|minimax|mm)$|"
    r"^/Users/jay/apps/(botfleet-server|agent-sync|agent-sync-push|grok-acp-runtime|agy-acp-runtime|shellular-runtime|mac-collab|senate-relay-runtime|scout-runtime|dsh-runtime|clutch-runtime|seat-mcp|KIMI-SALVAGE-.*)$|"
    r"^/Users/jay/Code/.*$",
    re.IGNORECASE,
)

IDLE_SEC = 4 * 3600  # 4 hours idle threshold

# --- lanes-guard (board a7dfde0e / 912034a0) -------------------------------------------------------------
# The old loop globbed only /Users/jay/apps/* (flat), so it never saw ~/apps/lanes/<Repo>/<lane> (layout v2) or the
# pre-v2 lanes/<prefix>/<lane>, lanes/_review/..., lanes/_managed/<tool>/..., lanes/_codex/<slug>/<Repo>, or
# ~/.codex/worktrees/<slug>/<Repo>.  Enumerate every linked
# worktree git knows about instead, keep the flat glob for legacy lanes, and protect fresh / live lanes.
# Every "cannot tell" keeps the deps: a failed lsof, pgrep or git call, an unreadable directory.
HOME_DIR = os.path.expanduser("~")
LANES_ROOT = "/Users/jay/apps/lanes/"
MIN_AGE_SEC = 7 * 86400   # a lane under ~/apps/lanes younger than this keeps its deps
FLEET_APPS = [
    "/Users/jay/Code/AI-Fleet-Coordinator/fleet-apps.json",
    os.path.join(HOME_DIR, ".claude-disk-janitor", "fleet-apps.json.cache"),  # written by janitor.sh
]

def registry_repos():
    for src in FLEET_APPS:
        try:
            d = json.load(open(src))
        except Exception:
            continue
        root = d.get("codeRoot") or "/Users/jay/Code"
        return [os.path.join(root, a.get("codeDir") or a.get("repo"))
                for a in d.get("apps", []) if a.get("codeDir") or a.get("repo")]
    return []

def registered_worktrees():
    """Linked worktrees at ANY depth under ~/apps or ~/.codex/worktrees, from git's own registry."""
    found = set()
    for repo in registry_repos():
        if not os.path.isdir(repo):
            continue
        try:
            res = subprocess.run(["git", "-C", repo, "worktree", "list", "--porcelain"],
                                 capture_output=True, text=True, timeout=20)
        except Exception:
            continue
        for line in res.stdout.splitlines():
            if line.startswith("worktree "):
                p = line[len("worktree "):]
                if p.startswith("/Users/jay/apps/") or p.startswith(HOME_DIR + "/.codex/worktrees/"):
                    found.add(p)
    return found

def canon(path):
    """The on-disk spelling (symlinks resolved, the letter case the volume stores) via F_GETPATH, or None.  git keeps the
    case a worktree path was typed in and lsof prints the stored one; os.path.realpath keeps the typed case, and
    chdir()+getcwd() would park this process's own cwd inside the lane."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return None
    try:
        buf = fcntl.fcntl(fd, getattr(fcntl, "F_GETPATH", 50), b"\0" * 1024)
        return os.fsdecode(buf.split(b"\0", 1)[0]) or None
    except OSError:
        return None
    finally:
        os.close(fd)

_ROOTS = None
def is_nested_lane(path, cpath=None):
    """Under LANES_ROOT by either spelling, without letter case (a false "nested" only adds protection)."""
    global _ROOTS
    if _ROOTS is None:
        c = canon(LANES_ROOT.rstrip("/"))
        _ROOTS = {LANES_ROOT.casefold()} | ({c.casefold().rstrip("/") + "/"} if c else set())
    return any(p and p.casefold().startswith(r) for p in (path, cpath) for r in _ROOTS)

def born_recently(path):
    """The checkout dir OR its git admin dir was BORN < MIN_AGE_SEC ago.  Both, because `rsync -a seed/ lane/` or
    `cp -Rp` re-times the checkout dir and APFS moves its birth back with it; the admin dir under the main repo's
    .git/worktrees keeps the real date.  Unreadable or a failed git call = fresh."""
    try:
        now = time.time()
        if now - os.stat(path).st_birthtime < MIN_AGE_SEC:
            return True
        res = subprocess.run(["git", "-C", path, "rev-parse", "--absolute-git-dir"],
                             capture_output=True, text=True, timeout=10)
        gd = res.stdout.strip()
        if res.returncode != 0 or not gd:
            return True
        return now - os.stat(gd).st_birthtime < MIN_AGE_SEC
    except Exception:
        return True

_CWDS = None
_CWDS_TAKEN = False
def cwd_paths(fresh=False):
    """Casefolded cwd of every process, or None when lsof did not finish cleanly: a non-zero rc (a killed lsof leaves
    a cut-off listing), a timeout, or no cwd at all.  None makes every checkout busy."""
    global _CWDS, _CWDS_TAKEN
    if fresh or not _CWDS_TAKEN:
        _CWDS_TAKEN = True
        try:
            res = subprocess.run(["lsof", "-d", "cwd", "-Fn"], capture_output=True, timeout=90)
            out = res.stdout.decode("utf-8", "surrogateescape")
            paths = [l[1:].casefold() for l in out.splitlines() if l.startswith("n")]
            _CWDS = paths if (res.returncode == 0 and paths) else None
        except Exception:
            _CWDS = None
    return _CWDS

def cwd_busy(path, cpath, fresh=False):
    """A process has the checkout (or a directory inside it) as its cwd, or lsof failed."""
    paths = cwd_paths(fresh)
    if paths is None:
        return True
    for p in {path.casefold(), (cpath or path).casefold()}:
        if any(x == p or x.startswith(p + "/") for x in paths):
            return True
    return False

def argv_busy(path):
    """A process names the checkout in its argv.  The path is regex-escaped for pgrep, and anything but a clean
    "no match" (rc 1), including a timeout on a loaded host, counts as busy."""
    pat = re.sub(r"([][\\.*^$+?(){}|])", r"\\\1", path)
    try:
        res = subprocess.run(["pgrep", "-f", pat], capture_output=True, timeout=10)
    except Exception:
        return True
    return res.returncode != 1

def is_git_worktree(path: str) -> bool:
    git_dir = os.path.join(path, ".git")
    if not (os.path.isdir(git_dir) or os.path.isfile(git_dir)):
        return False
    try:
        res = subprocess.run(
            ["git", "-C", path, "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return res.returncode == 0 and res.stdout.strip() == "true"
    except Exception:
        return False

def wt_has_blocking_dirt(path: str) -> bool:
    try:
        res = subprocess.run(
            ["git", "-C", path, "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if res.returncode != 0:
            return True
        for line in res.stdout.splitlines():
            if re.match(
                r"^\?\? (node_modules/|\.next/|\.turbo/|next-env\.d\.ts$|tsconfig\.tsbuildinfo$|\.DS_Store$|[^ ]*\.log$|data/app\.db(-wal|-shm)?$)",
                line,
            ):
                continue
            return True
        return False
    except Exception:
        return True

def wt_is_active(path: str, idle_sec: float = IDLE_SEC) -> bool:
    now = time.time()
    errors = []   # an unreadable directory may hold the one fresh file: count it as active
    for root, dirs, files in os.walk(path, onerror=errors.append):
        dirs[:] = [d for d in dirs if d not in {".git", "node_modules", ".next", ".turbo"}]
        for f in files:
            p = os.path.join(root, f)
            try:
                if now - os.path.getmtime(p) < idle_sec:
                    return True
            except OSError:
                continue
    return bool(errors)

for wt in sorted(set(glob.glob('/Users/jay/apps/*')) | registered_worktrees()):
    # A symlink in ~/apps can point into the ~/Code integration tree: KEEP_RE matched the link name, and rmtree
    # (which only refuses a link as the FINAL component) then emptied the real tree's node_modules.
    if os.path.islink(wt) or not os.path.isdir(wt):
        continue
    cwt = canon(wt)
    if cwt is None or KEEP_RE.match(wt) or KEEP_RE.match(cwt):
        continue
    if os.path.exists(os.path.join(wt, ".janitor-keep")):
        continue
    if is_nested_lane(wt, cwt) and born_recently(wt):
        continue
    if not is_git_worktree(wt):
        continue
    if wt_has_blocking_dirt(wt):
        continue
    if wt_is_active(wt):
        continue
    # Skip if any process is actively running inside this worktree (argv), or has its cwd there (pgrep -f only sees
    # argv).  lsof failed -> every checkout is kept.
    if argv_busy(wt) or cwd_busy(wt, cwt):
        continue
    # Right before deleting: a fresh lsof and pgrep (the first snapshot is from the start of the run).
    if cwd_busy(wt, cwt, fresh=True) or argv_busy(wt):
        continue
    for sub in ['node_modules', '.next', '.turbo']:
        target = os.path.join(wt, sub)
        if not os.path.isdir(target):
            continue
        # A dir literally named node_modules/.next/.turbo can still hold TRACKED
        # files (e.g. a vendored dep committed under app/vendor/node_modules) --
        # git status ignores it when unmodified, so wt_has_blocking_dirt above
        # never sees it. A blind rmtree here once deleted a tracked vendored
        # tree in a Congress.Trade worktree (2026-09-08). Refuse anything git
        # still tracks under this path.  Only rc 1 means "not tracked"; rc 128
        # (corrupt index, git killed) must not read as untracked.
        try:
            tracked = subprocess.run(
                ["git", "-C", wt, "ls-files", "--error-unmatch", "--", sub],
                capture_output=True, timeout=5,
            ).returncode != 1
        except Exception:
            tracked = True  # unknown -> assume tracked, do not delete
        if tracked:
            continue
        shutil.rmtree(target, ignore_errors=True)
PY
fi

# 4. Worktrees are owned by com.jay.disk-janitor (clean + idle, never forced).
# The #95 reaper treated detached HEAD as merged (empty-string word match
# hits every branch already in main) and then force-deleted the checkout,
# including in-session trees #93 was just fixed to keep.

# 5. Remote Hetzner Coolify maintenance trigger  (AFC-504)
# The ssh target is private deployment inventory: it arrives in the environment as
# HOUSEKEEPER_SSH_ALIAS, and the operator's own ssh config is what resolves it.
# With no target the hop is skipped rather than guessed at, because a placeholder
# target would reach the wrong host.  The hop is redundant with the host's own
# /etc/cron.daily/coolify-auto-maintenance, so skipping it costs no maintenance:
# the host still self-maintains on its own schedule.
#
# The remote hop is not optional once the script reaches it.  The target comes
# from the operator's own inventory, and a target that does not resolve is a
# configuration fault the operator has to see, so the hop fails closed and says
# which variable is missing.  It is never skipped in silence.
#
# Exit code 75 means the local cleanup ran and only the remote hop failed.
# The watcher reads that code as "cleaned", so a failed hop never sends it
# round a second time on an already pruned Mac.
HOUSEKEEPER_HOP_EXIT=75
if [ -z "${HOUSEKEEPER_SSH_ALIAS:-}" ]; then
    if [ "$HOUSEKEEPER_INVENTORY" = "1" ]; then
        echo "HOUSEKEEPER_SSH_ALIAS is required: the operator's inventory file is present but names no ssh target for the remote Hetzner maintenance trigger." >&2
    else
        echo "HOUSEKEEPER_SSH_ALIAS is required: no operator inventory file names an ssh target for the remote Hetzner maintenance trigger." >&2
    fi
    exit "$HOUSEKEEPER_HOP_EXIT"
fi
if ssh_err=$(ssh -o ConnectTimeout=3 -o BatchMode=yes "$HOUSEKEEPER_SSH_ALIAS" "exit 0" 2>&1); then
    echo "Triggering remote Hetzner Coolify maintenance..."
    if ssh_err=$(ssh -o ConnectTimeout=5 "$HOUSEKEEPER_SSH_ALIAS" "/etc/cron.daily/coolify-auto-maintenance" 2>&1); then
        :
    else
        maintenance_status=$?
        echo "Remote Hetzner maintenance trigger failed: the maintenance command exited $maintenance_status: $ssh_err" >&2
        exit "$HOUSEKEEPER_HOP_EXIT"
    fi
else
    status=$?
    echo "Remote Hetzner maintenance trigger failed: the ssh connectivity check to the operator's inventory target exited $status: $ssh_err" >&2
    exit "$HOUSEKEEPER_HOP_EXIT"
fi
# end remote Hetzner maintenance trigger

echo "[$(date)] Mac cleanup completed successfully."
