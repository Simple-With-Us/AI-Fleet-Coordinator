#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
fail() { echo "FAIL $*" >&2; exit 1; }
[ -f "$ROOT/mac-resource-watch.py" ] || fail "missing mac-resource-watch.py"
[ -f "$ROOT/mac-resource-watch.sh" ] || fail "missing mac-resource-watch.sh"
[ -f "$ROOT/launchd/com.jay.mac-resource-watch.plist" ] || fail "missing plist"
python3 -m py_compile "$ROOT/mac-resource-watch.py" || fail "py_compile"
bash -n "$ROOT/mac-resource-watch.sh" || fail "bash -n wrapper"
if ! grep -q 'BOTFLEET_HOUSEKEEPER_WEBHOOK' "$ROOT/mac-resource-watch.py"; then
  fail "watch must load Housekeeper webhook env"
fi
if ! grep -q 'GROK_BOT_HOUSEKEEPER_WEBHOOK' "$ROOT/mac-resource-watch.py"; then
  fail "watch must optionally load Grok Bot Housekeeper webhook env"
fi
if ! grep -q 'CLEAN_COOLDOWN_SEC' "$ROOT/mac-resource-watch.py"; then
  fail "cleanup must have its own cooldown (Codex #156 P1)"
fi
if ! grep -q 'SWAP_USED_GB' "$ROOT/mac-resource-watch.py"; then
  fail "swap hit must require absolute used GB, not store size"
fi
if ! grep -q 'X-Webhook-Event' "$ROOT/mac-resource-watch.py"; then
  fail "must send X-Webhook-Event for the :8800 receiver"
fi
if ! grep -q -- '--force' "$ROOT/mac-resource-watch.py"; then
  fail "--force is the only cleanup-cooldown bypass"
fi
if grep -nE 'print\(.*(secret|whsec_|TOKEN)' "$ROOT/mac-resource-watch.py"; then
  fail "must not print webhook secrets"
fi
if grep -q 'cleanmymac clean --force && cleanmymac optimize ram' "$ROOT/mac-resource-watch.py"; then
  fail "playbook must not tell Housekeeper to optimize ram"
fi
python3 - "$ROOT/mac-resource-watch.py" <<'PY' || fail "evaluate swap conjunct"
import importlib.util, sys
spec = importlib.util.spec_from_file_location("watch", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
# The samples ride the module's own thresholds, so the conjunct is still tested
# (percent alone does not fire, percent AND absolute used GB does) whatever the
# live defaults are.
sample = {
    "disk_free_gb": 200.0, "disk_used_pct": 20.0,
    "swap_total_gb": 64.0, "swap_used_gb": 1.0, "swap_used_pct": mod.SWAP_USED_PCT + 1.0,
    "load1": 1.0, "load5": 1.0, "load15": 1.0, "at": 0,
}
hits = mod.evaluate(sample, 200.0)
assert not any(h["metric"] == "swap" for h in hits), hits
sample["swap_used_gb"] = mod.SWAP_USED_GB + 1.0
hits = mod.evaluate(sample, 200.0)
assert any(h["metric"] == "swap" for h in hits), hits
print("evaluate ok")
PY
python3 - "$ROOT/mac-resource-watch.py" "$ROOT/mac-auto-cleanup.sh" <<'PY' || fail "housekeeper lock contract"
import importlib.util, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("watch", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
auto = Path(sys.argv[2]).read_text(encoding="utf-8")
# The watch and the cleanup script must take the SAME lock, or two schedulers
# reclaim at once.
assert mod.HOUSEKEEPER_LOCK == mod.STATE_DIR / ".housekeeper.lock", mod.HOUSEKEEPER_LOCK
assert ".claude-disk-janitor/.housekeeper.lock" in auto, "mac-auto-cleanup.sh takes another lock"
assert mod.HOUSEKEEPER_LOCK_STALE_SEC == 7200, mod.HOUSEKEEPER_LOCK_STALE_SEC
print("housekeeper lock ok")
PY
python3 - "$ROOT/mac-resource-watch.py" <<'PY' || fail "housekeeper lock behaviour"
import importlib.util, json, os, pathlib, shutil, subprocess, sys, tempfile, time
from pathlib import Path
spec = importlib.util.spec_from_file_location("watch", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
mod.log = lambda msg: None

scratch = Path(tempfile.mkdtemp(prefix="hk-lock-"))
try:
    state = scratch / ".claude-disk-janitor"
    lock = state / ".housekeeper.lock"
    mod.STATE_DIR = state
    mod.HOUSEKEEPER_LOCK = lock
    mod._HOUSEKEEPER_LOCK_INODE = None

    # 1. A free lock is taken.
    assert mod._acquire_housekeeper_lock() is True
    assert lock.is_dir(), "the lock directory must exist once it is held"

    # 2. The owner file sits BESIDE the lock, not inside it: the lock has to
    #    stay empty so the release rmdir works.
    owner = mod._housekeeper_owner_path()
    assert owner == state / ".housekeeper.lock.owner", owner
    assert owner.is_file(), "the holder must record who it is"
    assert not list(lock.iterdir()), "the lock directory must stay empty"

    # 3. A live peer's lock is refused and is not displaced.
    inode_before = lock.stat().st_ino
    assert mod._acquire_housekeeper_lock() is False, "a live lock must be refused"
    assert lock.stat().st_ino == inode_before, "a refused takeover must not replace the lock"

    # 4. The release removes the lock this process created.
    mod._release_housekeeper_lock()
    assert not lock.exists(), "the release must remove the lock"
    assert not owner.exists(), "the release must remove the owner file"

    # 5. A stale lock is taken over by moving it aside, and no copy is left.
    lock.mkdir(parents=True)
    stale = time.time() - (mod.HOUSEKEEPER_LOCK_STALE_SEC + 60)
    os.utime(lock, (stale, stale))
    assert mod._acquire_housekeeper_lock() is True, "a stale lock must be taken over"
    assert lock.is_dir(), "the canonical lock must exist after a takeover"
    leftovers = [p for p in state.iterdir() if p.name.startswith(".housekeeper.lock.stale-")]
    assert not leftovers, leftovers

    # 6. The release is ownership checked: a lock that was taken over as stale
    #    while this process was running is NOT ours to remove.
    mod._release_housekeeper_lock()
    lock.mkdir(parents=True)
    successor = lock.stat().st_ino
    mod._HOUSEKEEPER_LOCK_INODE = successor - 1  # as if ours had been replaced
    mod._release_housekeeper_lock()
    assert lock.is_dir(), "the release must not remove a lock this process does not own"

    # 7. A stale lock with contents in it is not ours to take.
    shutil.rmtree(lock)
    lock.mkdir(parents=True)
    (lock / "leftover.txt").write_text("not ours", encoding="utf-8")
    os.utime(lock, (stale, stale))
    assert mod._acquire_housekeeper_lock() is False, "a non-empty lock must not be taken"
    assert (lock / "leftover.txt").is_file(), "a non-empty lock must be put back"

    # 8. A restore onto an occupied canonical path is refused, not clobbered.
    #    os.rename onto an existing EMPTY directory succeeds on POSIX, and the
    #    lock marker is an empty directory, so a restore that renamed straight
    #    onto the canonical path would replace a third reclaimer's fresh lock
    #    while that reclaimer still believes it holds the lock.
    shutil.rmtree(lock)
    lock.mkdir(parents=True)
    peer_inode = lock.stat().st_ino
    aside = state / ".housekeeper.lock.stale-9.9"
    aside.mkdir(parents=True)
    assert mod._restore_housekeeper_lock(aside) is False, "a restore onto an occupied lock must be refused"
    assert lock.stat().st_ino == peer_inode, "a refused restore must not replace the peer's lock"
    assert aside.is_dir(), "a refused restore must leave the moved-aside copy where it is"

    # 9. A restore onto a free canonical path puts the copy back.
    shutil.rmtree(lock)
    assert mod._restore_housekeeper_lock(aside) is True, "a restore onto a free lock must succeed"
    assert lock.is_dir(), "the lock must be back at the canonical path"
    assert not aside.exists(), "the moved-aside copy must be gone after a restore"

    # 10. A malformed owner file is neither read as ours nor deleted.
    #     The file is shared state, so a shape it cannot be read into is left
    #     alone: a holder that cannot read it cannot know whose it is, and
    #     deleting it would let a peer fall back to the age rule and take over
    #     a lock that is still held.
    owner = mod._housekeeper_owner_path()
    owner.write_text("{not json", encoding="utf-8")
    mod._clear_housekeeper_owner(only_pid=os.getpid())
    assert owner.is_file(), "a malformed owner file must not be deleted"
    owner.write_text('{"pid": "not a number"}', encoding="utf-8")
    mod._clear_housekeeper_owner(only_pid=os.getpid())
    assert owner.is_file(), "an owner file with an unusable pid must not be deleted"
    owner.write_text('{"pid": %d}' % os.getpid(), encoding="utf-8")
    mod._clear_housekeeper_owner(only_pid=os.getpid())
    assert not owner.exists(), "an owner file that names us must be removed"

    # 11. The no-replace rename is ATOMIC, so there is no window between a
    #     check and a move for a peer to slip into.
    #
    #     os.rename onto an existing EMPTY directory SUCCEEDS and replaces it,
    #     so a restore built on "check that the path is free, then rename"
    #     has a window in which a third reclaimer's fresh lock is silently
    #     replaced while that reclaimer keeps sweeping beside us.  The move
    #     and the refusal have to be one decision.
    src = state / ".rename-src"
    src.mkdir()
    dst = state / ".rename-dst"
    dst.mkdir()
    occupied_inode = dst.stat().st_ino
    assert mod._rename_exclusive(src, dst) is False, "the no-replace rename must refuse an occupied path"
    assert dst.stat().st_ino == occupied_inode, "the occupied path must not be replaced"
    assert src.is_dir(), "the source must stay where it is"

    free = state / ".rename-free"
    assert mod._rename_exclusive(src, free) is True, "the no-replace rename must move onto a free path"
    assert free.is_dir(), "the move must have happened"
    assert not src.exists(), "the source must be gone after the move"

    # 12. The restore must not reach os.rename, and a peer that takes the
    #     canonical path in the window must not be clobbered.
    #
    #     os.rename onto an existing EMPTY directory SUCCEEDS and replaces it,
    #     so a restore built on "check that the path is free, then rename"
    #     has a window in which a third reclaimer's fresh lock is silently
    #     replaced while that reclaimer keeps sweeping beside us.  The move
    #     and the refusal have to be ONE decision.
    #
    #     The peer is injected at the moment the move happens, which is the
    #     only point at which the window exists.  The restore must never get
    #     there, so the spy must never fire.
    #
    #     The atomic no-replace rename is macOS-only, so this assertion holds
    #     where the atomic primitive exists.  The fallback elsewhere is a
    #     check followed by a rename, which still refuses an occupied
    #     destination but leaves the window open.  This script runs on the
    #     Mac, and the suite also runs on Linux in CI.
    shutil.rmtree(lock, ignore_errors=True)   # the canonical path starts free
    source = state / ".rename-source"
    source.mkdir()
    rename_calls = []
    peer_inode = {}
    real_rename = mod.os.rename
    def racy_rename(src_path, dst_path, *args, **kwargs):
        # A third reclaimer takes the canonical path in the window between
        # the check and the move.
        rename_calls.append((str(src_path), str(dst_path)))
        Path(dst_path).mkdir(parents=True, exist_ok=True)
        peer_inode["ino"] = Path(dst_path).stat().st_ino
        return real_rename(src_path, dst_path, *args, **kwargs)
    mod.os.rename = racy_rename
    try:
        mod._restore_housekeeper_lock(source)
    finally:
        mod.os.rename = real_rename
    if mod._renamex_np() is not None:
        assert not rename_calls, (
            "the restore must not go through os.rename: it replaces an empty "
            "directory, so a check-then-rename window clobbers a peer's lock "
            "(calls: %r)" % (rename_calls,))
        assert not peer_inode, "the restore must never displace a peer that takes the lock"

    # 13. The shared guard over the housekeeper protocol.
    #
    #     Every implementation of this protocol -- this watcher, the cleanup
    #     script in this repo, HogHunter's scripts/vacuum/lock.py and
    #     HogHunter's Sources/Storage/HousekeeperLock.swift -- serializes the
    #     critical sections of the lock with an exclusive flock on ONE file:
    #     ~/.claude-disk-janitor/.housekeeper.lock.guard, a SIBLING of the
    #     lock directory.  It is never inside the lock directory, because the
    #     lock is released with a plain rmdir and rmdir refuses a directory
    #     that has anything in it.  It is never deleted either:  deleting it
    #     would split the flock, and two processes could each hold a
    #     different inode and both believe they hold the guard.
    guard_getter = getattr(mod, "_housekeeper_guard_path", None)
    assert guard_getter is not None, (
        "the housekeeper protocol must have a shared guard; without one a "
        "takeover in another implementation can slip between a check and a "
        "mutation")
    guard_file = guard_getter()
    assert guard_file == state / ".housekeeper.lock.guard", guard_file
    assert guard_file.parent == lock.parent, "the guard must be a sibling of the lock"

    # 14. A clear BLOCKS while another process holds the guard.
    #
    #     The holder is a real second process, because the case that matters
    #     is a contender in another process.  It takes the guard and holds it
    #     for a moment; the clear must not touch the owner file until the
    #     holder lets go.  The check and the unlink are one decision, so a
    #     contender cannot replace the owner file in between and have the
    #     unlink delete the NEW holder's identity.
    holder_src = (
        "import fcntl, os, sys, time\n"
        "guard_path, ready = sys.argv[1], sys.argv[2]\n"
        "fd = os.open(guard_path, os.O_RDWR | os.O_CREAT, 0o600)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX)\n"
        "open(ready, 'w').write('held')\n"
        "time.sleep(3)\n"
        "fcntl.flock(fd, fcntl.LOCK_UN)\n"
        "os.close(fd)\n"
    )
    holder_ready = state / "holder-ready"
    holder = subprocess.Popen(
        [sys.executable, "-c", holder_src, str(guard_file), str(holder_ready)]
    )
    deadline = time.time() + 15
    while not holder_ready.exists() and time.time() < deadline:
        time.sleep(0.02)
    assert holder_ready.exists(), "the holder process must have taken the guard"
    owner = mod._housekeeper_owner_path()
    owner.write_text('{"pid": %d}' % os.getpid(), encoding="utf-8")
    started = time.time()
    mod._clear_housekeeper_owner(only_pid=os.getpid())
    elapsed = time.time() - started
    holder.wait(timeout=30)
    assert elapsed >= 1.0, (
        "the clear must wait for the shared guard; it finished in %.3fs while "
        "another process held it" % elapsed)

    # 15. A clear that races a NEW holder must not delete the new holder's
    #     owner file.
    #
    #     The contender is a separate process that writes the owner file the
    #     way a compliant writer does:  under the shared guard.  The read is
    #     instrumented so the contender's write lands exactly in the window
    #     between the check and the unlink, which is the window the finding is
    #     about.
    #
    #     With the guard in place the contender cannot write inside that
    #     window, so the new holder's file survives.  Without it the
    #     contender's write lands in the window and the unlink deletes the
    #     NEW holder's identity, which leaves the lock with no owner record
    #     at all and lets a later reader take it over on the age rule.
    contender_src = (
        "import fcntl, json, os, sys, time\n"
        "guard_path, owner_path, go_path, done_path = sys.argv[1:5]\n"
        "deadline = time.time() + 30\n"
        "while not os.path.exists(go_path) and time.time() < deadline:\n"
        "    time.sleep(0.02)\n"
        "fd = os.open(guard_path, os.O_RDWR | os.O_CREAT, 0o600)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX)\n"
        "try:\n"
        "    open(owner_path, 'w').write(json.dumps({'pid': 424242, 'host': 'contender'}))\n"
        "finally:\n"
        "    fcntl.flock(fd, fcntl.LOCK_UN)\n"
        "    os.close(fd)\n"
        "open(done_path, 'w').write('done')\n"
    )
    go_path = state / "contender-go"
    contender_done = state / "contender-done"
    owner.write_text('{"pid": %d}' % os.getpid(), encoding="utf-8")
    contender = subprocess.Popen(
        [
            sys.executable, "-c", contender_src,
            str(guard_file), str(owner), str(go_path), str(contender_done),
        ]
    )
    real_read_text = pathlib.Path.read_text

    def racing_read_text(self, *args, **kwargs):
        text = real_read_text(self, *args, **kwargs)
        if self == owner:
            go_path.write_text("go")
            deadline = time.time() + 3
            while not contender_done.exists() and time.time() < deadline:
                time.sleep(0.02)
        return text

    pathlib.Path.read_text = racing_read_text
    try:
        mod._clear_housekeeper_owner(only_pid=os.getpid())
    finally:
        pathlib.Path.read_text = real_read_text
    contender.wait(timeout=30)
    assert owner.is_file(), (
        "the clear must not delete a NEW holder's owner file: the contender "
        "wrote it inside the check-then-unlink window")
    assert json.loads(owner.read_text(encoding="utf-8"))["pid"] == 424242, (
        "the owner file that survives must be the NEW holder's")

    # 16. The release must never delete the guard file.
    shutil.rmtree(lock, ignore_errors=True)
    owner.unlink(missing_ok=True)
    assert mod._acquire_housekeeper_lock() is True
    assert guard_file.is_file(), "the guard file must exist while the lock is held"
    mod._release_housekeeper_lock()
    assert guard_file.is_file(), (
        "the guard file must never be deleted: two processes that delete it "
        "can hold locks on two different inodes at the same time")

    # 17. The release is ONE guarded decision.
    #
    #     The ownership check and the removal have to be one decision against
    #     every implementation of this protocol:  a takeover that replaces
    #     the canonical lock between the check and the removal would have the
    #     exiting holder delete the NEW holder's lock.  The guard is what
    #     makes it one decision, so the release has to wait for a process
    #     that holds the guard.
    shutil.rmtree(lock, ignore_errors=True)
    assert mod._acquire_housekeeper_lock() is True
    lock.mkdir(parents=True, exist_ok=True)
    ready2 = state / "release-holder-ready"
    holder2 = subprocess.Popen(
        [sys.executable, "-c", holder_src, str(guard_file), str(ready2)]
    )
    deadline = time.time() + 15
    while not ready2.exists() and time.time() < deadline:
        time.sleep(0.02)
    assert ready2.exists(), "the holder process must have taken the guard"
    started = time.time()
    mod._release_housekeeper_lock()
    elapsed = time.time() - started
    holder2.wait(timeout=30)
    assert elapsed >= 1.0, (
        "the release must wait for the shared guard; it finished in %.3fs while "
        "another process held it" % elapsed)
    # And a lock that is NOT the one this process created is never removed.
    shutil.rmtree(lock, ignore_errors=True)
    lock.mkdir(parents=True)
    replacement_inode = lock.stat().st_ino
    mod._HOUSEKEEPER_LOCK_INODE = replacement_inode - 1
    mod._release_housekeeper_lock()
    assert lock.is_dir(), "the release must not remove a lock this process does not own"
    assert lock.stat().st_ino == replacement_inode, (
        "a takeover that replaced the canonical lock must keep its own lock")

    # 18. A non-UTF-8 owner file does not escape the release path.
    #
    #     UnicodeDecodeError is a ValueError and NOT an OSError, so a read
    #     that only catches OSError raises out of the clear, out of the
    #     release and out of the cleanup's finally block:  the lock is never
    #     removed and the run dies with a traceback.  A file that cannot be
    #     decoded is left alone, like any other malformed one.
    owner = mod._housekeeper_owner_path()
    owner.write_bytes(b"\xff\xfe\x00not utf-8")
    mod._clear_housekeeper_owner(only_pid=os.getpid())
    assert owner.is_file(), "an owner file that cannot be decoded must not be deleted"
    shutil.rmtree(lock, ignore_errors=True)
    assert mod._acquire_housekeeper_lock() is True
    owner.write_bytes(b"\xff\xfe\x00not utf-8")
    mod._release_housekeeper_lock()
    assert not lock.exists(), (
        "a malformed owner file must not stop the release from removing the lock")

    # 19. A refused restore names the moved-aside copy.
    #
    #     The peer's lock stays orphaned at the moved-aside path when the
    #     restore is refused, and a copy nobody can name is a copy nobody
    #     ever cleans up.  The failure has to say where it is.
    messages = []
    mod.log = lambda msg: messages.append(str(msg))
    shutil.rmtree(lock, ignore_errors=True)
    lock.mkdir(parents=True)
    stale = time.time() - (mod.HOUSEKEEPER_LOCK_STALE_SEC + 60)
    os.utime(lock, (stale, stale))
    # The moved object can be a FRESH lock:  a peer replaced it between the
    # staleness check and the rename.  The re-check reads its age again, and
    # a fresh one belongs to a live peer, so the lock has to be put back.
    real_clock = mod.time
    calls = {"n": 0}

    class _BackwardsClock:
        def time(self):
            calls["n"] += 1
            # The first read sees the stale lock, the second one sees a lock
            # that is fresh again.
            if calls["n"] == 1:
                return real_clock.time()
            return real_clock.time() - (mod.HOUSEKEEPER_LOCK_STALE_SEC + 600)

    real_restore = mod._restore_housekeeper_lock
    mod._restore_housekeeper_lock = lambda aside: False
    mod.time = _BackwardsClock()
    try:
        assert mod._acquire_housekeeper_lock() is False, "a live lock must not be taken"
    finally:
        mod.time = real_clock
        mod._restore_housekeeper_lock = real_restore
    assert any(
        "failed to restore" in msg and ".housekeeper.lock.stale-" in msg
        for msg in messages
    ), (
        "a refused restore must name the moved-aside copy, or the orphaned "
        "peer lock accumulates invisibly (messages: %r)" % (messages,))

finally:
    shutil.rmtree(scratch, ignore_errors=True)
print("housekeeper lock behaviour ok")
PY
echo OK
