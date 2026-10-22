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
import importlib.util, os, shutil, sys, tempfile, time
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
finally:
    shutil.rmtree(scratch, ignore_errors=True)
print("housekeeper lock behaviour ok")
PY
echo OK
