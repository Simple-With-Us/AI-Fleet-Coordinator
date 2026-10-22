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
# The watch takes over a stale housekeeper lock the way mac-auto-cleanup.sh
# does (AFC-458):  staleness is judged at the lock path, so a live holder's
# lock is never moved, and a stale lock is MOVED aside rather than removed and
# made again, so two reclaimers cannot both win it.  A temporary STATE_DIR
# stands in for ~/.claude-disk-janitor;  nothing real is touched.
python3 - "$ROOT/mac-resource-watch.py" <<'PY' || fail "housekeeper lock takeover"
import atexit, importlib.util, os, shutil, sys, tempfile, time
from pathlib import Path
spec = importlib.util.spec_from_file_location("watch", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
os.environ.pop("HOUSEKEEPER_LOCK_OWNER", None)
mod.log = lambda msg: None
OLD = time.time() - 3 * 3600


def setup(name):
    state = Path(tempfile.mkdtemp(prefix=f"hk-{name}-"))
    atexit.register(shutil.rmtree, state, True)
    mod.STATE_DIR = state
    mod.HOUSEKEEPER_LOCK = state / ".housekeeper.lock"
    return state, mod.HOUSEKEEPER_LOCK


def copies(state):
    return sorted(p.name for p in state.glob(".housekeeper.lock.stale-*"))


renames = []
real_rename = os.rename


def logging_rename(src, dst):
    renames.append(str(src))
    return real_rename(src, dst)


mod.os.rename = logging_rename

# Free:  taken.
state, lock = setup("free")
assert mod._acquire_housekeeper_lock() is True
assert lock.is_dir()
mod._release_housekeeper_lock()
assert not lock.exists()

# Fresh peer lock:  refused, and never moved, not even to be checked.
state, lock = setup("fresh")
lock.mkdir()
renames.clear()
assert mod._acquire_housekeeper_lock() is False
assert lock.is_dir() and not renames and not copies(state), (renames, copies(state))

# Stale empty lock:  taken over by moving it aside, and no copy is left.
state, lock = setup("stale")
lock.mkdir()
os.utime(lock, (OLD, OLD))
renames.clear()
assert mod._acquire_housekeeper_lock() is True
assert lock.is_dir() and renames == [str(lock)] and not copies(state), (renames, copies(state))
assert time.time() - lock.stat().st_mtime < 60, "the taken-over lock must be a fresh one"

# Stale lock with something in it:  not a marker a holder makes, so refused,
# left in place and never moved.
state, lock = setup("contents")
lock.mkdir()
(lock / "leftover.txt").write_text("not ours")
os.utime(lock, (OLD, OLD))
renames.clear()
assert mod._acquire_housekeeper_lock() is False
assert (lock / "leftover.txt").is_file() and not renames

# A stale lock replaced by a peer's fresh one between the check and the move:
# the moved lock is fresh, so it is given back with its age, and no copy stays.
state, lock = setup("replaced")
lock.mkdir()
os.utime(lock, (OLD, OLD))
real_is_stale = mod._housekeeper_lock_is_stale
calls = []


def replaced_is_stale(path):
    calls.append(path)
    if len(calls) == 1:
        return real_is_stale(path)
    # The peer's takeover lands between this caller's check and its move.
    return False


mod._housekeeper_lock_is_stale = replaced_is_stale
assert mod._acquire_housekeeper_lock() is False
mod._housekeeper_lock_is_stale = real_is_stale
assert lock.is_dir() and not copies(state), copies(state)
assert abs(lock.stat().st_mtime - OLD) < 2, "a lock given back must keep its age"

# The holder sweeps empty moved-aside copies older than the stale window, and
# nothing else.
state, lock = setup("sweep")
old_empty = state / ".housekeeper.lock.stale-1.a"
fresh_empty = state / ".housekeeper.lock.stale-2.b"
old_full = state / ".housekeeper.lock.stale-3.c"
for d in (old_empty, fresh_empty, old_full):
    d.mkdir()
(old_full / "leftover.txt").write_text("not ours")
os.utime(old_empty, (OLD, OLD))
os.utime(old_full, (OLD, OLD))
assert mod._acquire_housekeeper_lock() is True
assert not old_empty.exists() and fresh_empty.is_dir() and (old_full / "leftover.txt").is_file()
mod._release_housekeeper_lock()

# Two reclaimers race for one stale lock, thirty times over:  exactly one may
# win each race.  They start together (each waits on the go pipe until both
# are forked), and a winner keeps the lock until both have answered, so a
# second winner would have to overlap the first rather than follow it.
mod.os.rename = real_rename
for trial in range(30):
    state, lock = setup(f"race{trial}")
    lock.mkdir()
    os.utime(lock, (OLD, OLD))
    go_r, go_w = os.pipe()
    res_r, res_w = os.pipe()
    rel_r, rel_w = os.pipe()
    pids = []
    for _ in range(2):
        pid = os.fork()
        if pid == 0:
            os.close(go_w)
            os.close(res_r)
            os.close(rel_w)
            os.read(go_r, 1)
            won = mod._acquire_housekeeper_lock()
            os.write(res_w, b"1" if won else b"0")
            if won:
                os.read(rel_r, 1)
                mod._release_housekeeper_lock()
            os._exit(0)
        pids.append(pid)
    for fd in (go_r, res_w, rel_r):
        os.close(fd)
    os.write(go_w, b"gg")
    results = b""
    while len(results) < 2:
        chunk = os.read(res_r, 2)
        if not chunk:
            break
        results += chunk
    os.write(rel_w, b"rr")
    for pid in pids:
        os.waitpid(pid, 0)
    for fd in (go_w, res_r, rel_w):
        os.close(fd)
    assert results.count(b"1") == 1 and len(results) == 2, (trial, results)
    assert not copies(state), (trial, copies(state))
print("housekeeper lock takeover ok")
PY
echo OK
