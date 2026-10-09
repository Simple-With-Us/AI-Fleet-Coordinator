#!/bin/bash
# Mac Automated Maintenance Script
# Safely prunes developer caches, Xcode symbols, unavailable simulators,
# package manager caches, old agent logs, and triggers remote Hetzner Docker cleanup.
# Does not reap git worktrees or live ~/.grok/worktrees (disk-janitor owns that).

set -u

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
HOUSEKEEPER_LOCK="$HOME/.claude-disk-janitor/.housekeeper.lock"
mkdir -p "$(dirname "$HOUSEKEEPER_LOCK")" 2>/dev/null || true
if [ "${HOUSEKEEPER_LOCK_OWNER:-0}" != "1" ]; then
    if ! mkdir "$HOUSEKEEPER_LOCK" 2>/dev/null; then
        # Steal a lock older than 2h -- same idiom as janitor.sh.
        if [ -n "$(find "$HOUSEKEEPER_LOCK" -maxdepth 0 -mmin +120 2>/dev/null)" ]; then
            rmdir "$HOUSEKEEPER_LOCK" 2>/dev/null || true
            mkdir "$HOUSEKEEPER_LOCK" 2>/dev/null || { echo "skipped, peer holds housekeeper lock"; exit 0; }
        else
            echo "skipped, peer holds housekeeper lock"; exit 0
        fi
    fi
    export HOUSEKEEPER_LOCK_OWNER=1
    trap 'rmdir "$HOUSEKEEPER_LOCK" 2>/dev/null || true' EXIT
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

# 5. Remote Hetzner Coolify maintenance trigger
if ssh -o ConnectTimeout=3 -o BatchMode=yes coolify "exit 0" 2>/dev/null; then
    echo "Triggering remote Hetzner Coolify maintenance..."
    ssh -o ConnectTimeout=5 coolify "/etc/cron.daily/coolify-auto-maintenance" >> /dev/null 2>&1 || true
fi

echo "[$(date)] Mac cleanup completed successfully."
