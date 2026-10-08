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
import os, glob, shutil, re, subprocess, time

KEEP_RE = re.compile(
    r"^/Users/jay/apps/[a-z0-9]+-(claude|codex|live|antigravity|cursor|monet|grok|grok-build|deepseek|minimax|mm)$|"
    r"^/Users/jay/apps/(botfleet-server|agent-sync|agent-sync-push|grok-acp-runtime|agy-acp-runtime|shellular-runtime|mac-collab|senate-relay-runtime|scout-runtime|dsh-runtime|clutch-runtime|seat-mcp|KIMI-SALVAGE-.*)$|"
    r"^/Users/jay/Code/.*$"
)

IDLE_SEC = 4 * 3600  # 4 hours idle threshold

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
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in {".git", "node_modules", ".next", ".turbo"}]
        for f in files:
            p = os.path.join(root, f)
            try:
                if now - os.path.getmtime(p) < idle_sec:
                    return True
            except OSError:
                continue
    return False

for wt in glob.glob('/Users/jay/apps/*'):
    if not os.path.isdir(wt) or KEEP_RE.match(wt):
        continue
    if os.path.exists(os.path.join(wt, ".janitor-keep")):
        continue
    if not is_git_worktree(wt):
        continue
    if wt_has_blocking_dirt(wt):
        continue
    if wt_is_active(wt):
        continue
    # Skip if any process is actively running inside this worktree
    try:
        pids = subprocess.run(["pgrep", "-f", wt], capture_output=True, timeout=3).stdout.strip()
        if pids:
            continue
    except Exception:
        pass
    for sub in ['node_modules', '.next', '.turbo']:
        target = os.path.join(wt, sub)
        if not os.path.isdir(target):
            continue
        # A dir literally named node_modules/.next/.turbo can still hold TRACKED
        # files (e.g. a vendored dep committed under app/vendor/node_modules) --
        # git status ignores it when unmodified, so wt_has_blocking_dirt above
        # never sees it. A blind rmtree here once deleted a tracked vendored
        # tree in a Congress.Trade worktree (2026-09-08). Refuse anything git
        # still tracks under this path.
        try:
            tracked = subprocess.run(
                ["git", "-C", wt, "ls-files", "--error-unmatch", "--", sub],
                capture_output=True, timeout=5,
            ).returncode == 0
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
