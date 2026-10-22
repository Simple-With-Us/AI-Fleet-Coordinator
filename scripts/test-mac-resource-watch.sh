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
if tr -d '[:space:]#_-' < "$ROOT/mac-resource-watch.py" | grep -qF "$PRIVATE_REPO_SPELLED"; then
  fail "the committed housekeeper scripts must not name a private sibling repository, split across lines"
fi
if guard_lines "$ROOT/test-mac-auto-cleanup.sh" "$ROOT/test-mac-resource-watch.sh" \
    | tr -d '[:space:]#_-' | grep -qF "$PRIVATE_REPO_SPELLED"; then
  fail "the committed housekeeper scripts must not name a private sibling repository, split across lines"
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

    def owner_record(pid, **overrides):
        """An owner record in the shape the protocol publishes.

        The owner file is shared state, so the reader accepts the declared
        shape and nothing else:  these four keys, these types, these bounds.
        """
        record = {
            "pid": pid,
            "host": "test-host",
            "acquired_at": 1700000000.0,
            "lock": str(lock),
        }
        record.update(overrides)
        return record

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
    # A record that is not in the declared shape is not an owner record
    # either, even when its pid is ours: a holder that cannot read the record
    # cannot know that it describes it.
    owner.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
    mod._clear_housekeeper_owner(only_pid=os.getpid())
    assert owner.is_file(), "an owner file outside the declared shape must not be deleted"
    owner.write_text(json.dumps(owner_record(os.getpid())), encoding="utf-8")
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
    #     script in this repo and an equivalent lock helper in a sibling
    #     repository -- serializes the critical sections of the lock with an
    #     exclusive flock on ONE file:
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
    owner.write_text(json.dumps(owner_record(os.getpid())), encoding="utf-8")
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
        "guard_path, owner_path, go_path, done_path, lock_path = sys.argv[1:6]\n"
        "deadline = time.time() + 30\n"
        "while not os.path.exists(go_path) and time.time() < deadline:\n"
        "    time.sleep(0.02)\n"
        "fd = os.open(guard_path, os.O_RDWR | os.O_CREAT, 0o600)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX)\n"
        "try:\n"
        "    record = {'pid': 424242, 'host': 'contender',"
        " 'acquired_at': 1700000000.0, 'lock': lock_path}\n"
        "    open(owner_path, 'w').write(json.dumps(record))\n"
        "finally:\n"
        "    fcntl.flock(fd, fcntl.LOCK_UN)\n"
        "    os.close(fd)\n"
        "open(done_path, 'w').write('done')\n"
    )
    go_path = state / "contender-go"
    contender_done = state / "contender-done"
    owner.write_text(json.dumps(owner_record(os.getpid())), encoding="utf-8")
    contender = subprocess.Popen(
        [
            sys.executable, "-c", contender_src,
            str(guard_file), str(owner), str(go_path), str(contender_done), str(lock),
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

    # 20. The owner record must match the declared schema EXACTLY.
    #
    #     The owner file is shared state that any process can write, and its
    #     parsed shape is handed to the lock-ownership logic.  A record that
    #     is missing a key, carries an unknown key, or holds a value of the
    #     wrong type or out of bounds is not an owner record, and accepting
    #     one lets a malformed file decide who holds the lock.
    #
    #     A malformed record is REJECTED with None and never with an
    #     exception: a raise here would escape the release path and leak the
    #     lock.
    parse = mod._parse_housekeeper_owner
    good = owner_record(4242)
    assert parse(json.dumps(good)) == good, "a record in the declared shape must be accepted"

    def rejects(record, why):
        assert parse(json.dumps(record)) is None, why

    rejects({"pid": 4242}, "a record that is missing a key must be rejected")
    rejects({"pid": 4242, "host": "h", "acquired_at": 1.0}, "a record that is missing a key must be rejected")
    rejects(dict(good, extra="x"), "a record with an unknown key must be rejected")
    rejects(dict(good, pid=0), "a pid of zero must be rejected")
    rejects(dict(good, pid=-1), "a negative pid must be rejected")
    rejects(dict(good, pid=True), "a boolean pid must be rejected")
    rejects(dict(good, pid=1.5), "a non-integer pid must be rejected")
    rejects(dict(good, host=""), "an empty host must be rejected")
    rejects(dict(good, host=17), "a non-string host must be rejected")
    rejects(dict(good, acquired_at="now"), "a non-numeric acquired_at must be rejected")
    rejects(dict(good, acquired_at=True), "a boolean acquired_at must be rejected")
    rejects(dict(good, acquired_at=float("nan")), "a NaN acquired_at must be rejected")
    rejects(dict(good, acquired_at=float("inf")), "an infinite acquired_at must be rejected")
    rejects(dict(good, lock=""), "an empty lock must be rejected")
    rejects(dict(good, lock=None), "a non-string lock must be rejected")
    for raw in ("", "null", "42", '"a string"', "{}", '[1, 2, 3]', "{not json",
                '{"pid": true}', '{"pid": 1, "host": null, "acquired_at": 0, "lock": "x"}'):
        assert parse(raw) is None, "a malformed record must be rejected, not accepted: %r" % raw

    # 21. The owner record is written ATOMICALLY.
    #
    #     A write that truncates the owner file in place can leave partial
    #     JSON behind after a crash, and the owner record is the safety
    #     metadata the lock-ownership logic reads.  The write goes to a temp
    #     file in the same directory, is fsynced, renamed over the owner
    #     file, and the directory is fsynced too.
    owner = mod._housekeeper_owner_path()
    owner.write_text(json.dumps(owner_record(111)), encoding="utf-8")
    replace_calls = []
    fsync_calls = []
    real_replace = mod.os.replace
    real_fsync = mod.os.fsync

    def spy_replace(src, dst, *args, **kwargs):
        replace_calls.append((str(src), str(dst), os.stat(src).st_mode & 0o777))
        return real_replace(src, dst, *args, **kwargs)

    def spy_fsync(fd):
        fsync_calls.append(fd)
        return real_fsync(fd)

    mod.os.replace = spy_replace
    mod.os.fsync = spy_fsync
    try:
        mod._write_housekeeper_owner()
    finally:
        mod.os.replace = real_replace
        mod.os.fsync = real_fsync
    assert replace_calls, (
        "the owner record must be written with an atomic replace, not in "
        "place: a write that truncates the file can leave partial JSON "
        "behind")
    src, dst, tmp_mode = replace_calls[-1]
    assert dst == str(owner), replace_calls
    assert os.path.dirname(src) == str(state), (
        "the temp file must be in the same directory as the owner file")
    assert tmp_mode == 0o600, "the temp file must be private, got %o" % tmp_mode
    assert not os.path.exists(src), "the temp file must be gone after the write"
    assert len(fsync_calls) >= 2, (
        "the temp file and its directory must both be fsynced (got %d)"
        % len(fsync_calls))
    assert owner.stat().st_mode & 0o777 == 0o600, (
        "the owner record must be private, got %o" % (owner.stat().st_mode & 0o777))
    assert json.loads(owner.read_text(encoding="utf-8"))["pid"] == os.getpid()

    # And a write that FAILS must leave the previous record intact:  the
    # file is replaced atomically, so a failure cannot leave a truncated
    # owner file behind, and the temp file must not survive it.
    owner.write_text(json.dumps(owner_record(111)), encoding="utf-8")

    def boom(src, dst, *args, **kwargs):
        raise OSError(28, "No space left on device")

    mod.os.replace = boom
    try:
        mod._write_housekeeper_owner()
    finally:
        mod.os.replace = real_replace
    assert json.loads(owner.read_text(encoding="utf-8"))["pid"] == 111, (
        "a failed write must not destroy the previous owner record")
    leftovers = [p.name for p in state.iterdir() if p.name.endswith(".tmp")]
    assert not leftovers, (
        "a failed write must not leave a temp file behind: %r" % (leftovers,))

    # 22. The guard's state directory is PRIVATE, and a symlinked one is
    #     refused.
    #
    #     The guard and the owner record are shared state that must not be
    #     world readable, and a symlinked state directory can redirect the
    #     shared guard somewhere else entirely.
    saved_lock = mod.HOUSEKEEPER_LOCK
    saved_umask = os.umask(0o022)
    try:
        private_state = scratch / "private-state"
        mod.HOUSEKEEPER_LOCK = private_state / ".housekeeper.lock"
        with mod._housekeeper_guard():
            pass
        assert private_state.is_dir(), "the guard must create its state directory"
        private_mode = private_state.stat().st_mode & 0o777
        assert private_mode == 0o700, (
            "the state directory must be created private, got %o" % private_mode)

        # An EXISTING directory keeps its own permissions:  it is not ours to
        # change.
        kept_state = scratch / "kept-state"
        kept_state.mkdir()
        os.chmod(kept_state, 0o755)
        mod.HOUSEKEEPER_LOCK = kept_state / ".housekeeper.lock"
        with mod._housekeeper_guard():
            pass
        kept_mode = kept_state.stat().st_mode & 0o777
        assert kept_mode == 0o755, (
            "an existing state directory's permissions must be left "
            "unchanged, got %o" % kept_mode)

        # A SYMLINKED state directory can redirect the shared guard, so it is
        # refused.
        real_state = scratch / "real-state"
        real_state.mkdir()
        linked_state = scratch / "linked-state"
        os.symlink(real_state, linked_state)
        mod.HOUSEKEEPER_LOCK = linked_state / ".housekeeper.lock"
        refused = False
        try:
            with mod._housekeeper_guard():
                pass
        except OSError:
            refused = True
        assert refused, "a symlinked housekeeper state directory must be refused"
        assert not (real_state / ".housekeeper.lock.guard").exists(), (
            "a refused state directory must not be used for the guard")

        # The refusal above is its OWN exception type, so the acquire can tell
        # it apart from a genuine I/O failure.  A symlinked state directory is
        # still a clean "skip the cleanup", and not a crash.
        mod.HOUSEKEEPER_LOCK = linked_state / ".housekeeper.lock"
        assert mod._acquire_housekeeper_lock() is False, (
            "a symlinked state directory must skip the cleanup, not raise")
        mod.HOUSEKEEPER_LOCK = lock

        # A GENUINE failure -- permissions, a full disk, a read-only
        # filesystem -- is not "a peer holds the lock".  It must reach the
        # caller, or a broken install is reported as a busy lock for ever.
        real_acquire = mod._acquire_housekeeper_lock_locked
        def boom_locked():
            raise PermissionError(13, "Permission denied")
        mod._acquire_housekeeper_lock_locked = boom_locked
        propagated = False
        try:
            mod._acquire_housekeeper_lock()
        except PermissionError:
            propagated = True
        finally:
            mod._acquire_housekeeper_lock_locked = real_acquire
        assert propagated, (
            "a genuine OSError from the acquire must propagate, not be "
            "reported as an unavailable lock")

        # A failure AFTER the mkdir must not leave the lock behind.
        # run_cleanup returns before its try/finally, so nothing else would
        # release it, and every peer would read it as a live lock until it
        # goes stale -- the machine would lose reclaim for the whole window.
        orphan_state = scratch / "orphan-state"
        mod.STATE_DIR = orphan_state
        mod.HOUSEKEEPER_LOCK = orphan_state / ".housekeeper.lock"
        mod._HOUSEKEEPER_LOCK_INODE = None
        def created_then_failed():
            mod.HOUSEKEEPER_LOCK.mkdir(parents=True, exist_ok=True)
            # The inode is recorded immediately after the mkdir, so the
            # failure path knows the lock is ours to drop.
            mod._HOUSEKEEPER_LOCK_INODE = mod._lock_inode()
            raise RuntimeError("failed after the lock was created")
        mod._acquire_housekeeper_lock_locked = created_then_failed
        try:
            mod._acquire_housekeeper_lock()
        except RuntimeError:
            pass
        else:
            raise AssertionError("the post-mkdir failure must propagate")
        assert not mod.HOUSEKEEPER_LOCK.exists(), (
            "a lock created before a failure must not be orphaned: every peer "
            "would skip reclaim until it went stale")
        assert mod._HOUSEKEEPER_LOCK_INODE is None, (
            "the recorded inode must be cleared when the lock is dropped")
        mod._acquire_housekeeper_lock_locked = real_acquire
        mod.STATE_DIR = state
    finally:
        os.umask(saved_umask)
        mod.HOUSEKEEPER_LOCK = saved_lock

    # 23. The acquire path READS the owner record.
    #
    #     The release removes the lock directory before it clears the owner
    #     file, so a removal that FAILED leaves both behind.  Preserving the
    #     record is only worth something if somebody consumes it:  without
    #     this, the surviving lock still wedges every reclaimer for the full
    #     stale window, and the two-hour wedge this PR targets would only be
    #     removed for the external peer that reads the record.
    #
    #     An abandoned lock is reclaimed at once instead of waiting out the
    #     window, and every case where the record cannot speak for the lock
    #     falls back to the age rule so a live holder is never displaced.
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    host = mod.socket.gethostname()

    assert mod._pid_alive(os.getpid()) is True, "this process must look alive"
    assert mod._pid_alive(gone.pid) is False, "an exited pid must not look alive"

    def fresh_lock(record, owner_older=False):
        """A FRESH lock, well inside the stale window, plus an owner record."""
        shutil.rmtree(lock, ignore_errors=True)
        owner.unlink(missing_ok=True)
        lock.mkdir(parents=True)
        lock_mtime = time.time() - 5
        os.utime(lock, (lock_mtime, lock_mtime))
        if record is not None:
            raw = (json.dumps(record) if isinstance(record, dict) else record)
            owner.write_text(raw, encoding="utf-8")
            offset = -60 if owner_older else 60
            owner_mtime = lock_mtime + offset
            os.utime(owner, (owner_mtime, owner_mtime))

    # The abandoned lock is reclaimed at once, without the age rule.
    fresh_lock(owner_record(gone.pid, host=host))
    assert mod._acquire_housekeeper_lock() is True, (
        "a fresh lock whose owner record names a dead pid must be taken over "
        "at once; falling back to the age rule here is the two-hour wedge on "
        "a lock nobody holds")
    mod._release_housekeeper_lock()

    # A live holder's lock is never displaced.
    fresh_lock(owner_record(os.getpid(), host=host))
    assert mod._acquire_housekeeper_lock() is False, (
        "a live holder's owner record must keep its lock")

    # The age rule decides whenever the record cannot speak for the lock.
    fresh_lock(None)
    assert mod._acquire_housekeeper_lock() is False, (
        "no owner record means the age rule decides; the shell holder writes "
        "none at all")
    fresh_lock("{ this is not json")
    assert mod._acquire_housekeeper_lock() is False, (
        "a malformed owner record must fall back to the age rule")
    fresh_lock(owner_record(gone.pid, host=host), owner_older=True)
    assert mod._acquire_housekeeper_lock() is False, (
        "an owner record older than the lock directory it sits beside "
        "describes an earlier holder, so the age rule decides")
    fresh_lock(owner_record(gone.pid, host="some-other-host"))
    assert mod._acquire_housekeeper_lock() is False, (
        "another host's pid says nothing about a lock on this one")
    shutil.rmtree(lock, ignore_errors=True)
    owner.unlink(missing_ok=True)

finally:
    shutil.rmtree(scratch, ignore_errors=True)
print("housekeeper lock behaviour ok")
PY
echo OK
