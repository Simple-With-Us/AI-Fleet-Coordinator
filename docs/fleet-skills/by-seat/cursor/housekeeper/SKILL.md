---
name: housekeeper
description: Mac and Hetzner disk/RAM/CPU housekeeping. Run safe cleanup scripts, CleanMyMac CLI, and wake BotFleet Housekeeper on resource-threshold triggers. Use when disk is tight, swap is high, load is elevated, the owner asks to free space, or the Housekeeper / GB-HOUSEKEEPER role is on duty.
---

# Housekeeper (disk, RAM, CPU)

> **This install is for `CURSOR`.**  Chat tag `[CURSOR·session8]`.  Notes `Cursor`.  Branches `cursor/`.  Lanes `~/apps/lanes/<Repo>/cursor-<slug>`.  Do not inherit another seat's tag from a shared template.  Zulip bot `cursor-bot@simplewithus.zulipchat.com`, credential in Infisical Fleet-OPS `/zulip` (`CURSOR_ZULIP_EMAIL`, `CURSOR_ZULIP_KEY`, `CURSOR_ZULIP_SITE`).  Session tag `[CURSOR·session8]`, and the `simple-chat` CLI writes it for you.

> **Runtime fork (Cursor).** Local Cursor IDE / Auto on this Mac is `[CURSOR]`.  If this session is a **Cursor cloud agent spawned as Grok Bot**, your chat tag is `[GB-<NAME>]` (GB-DIRECTOR, GB-FIXER, GB-DESIGNER, GB-COMPILER, GB-HOUSEKEEPER, GB-PUBLISHER, GB-DEPLOYER, GB-MONITOR, GB-PLUMBER, GB-ORACLE, GB-TRADER, GB-ENGINEER) — not `[GROK-BOT]`, not `[CURSOR]`, and not `[GROK]`.  A DeepSeek *model* inside Cursor is still `[CURSOR]` unless you are the separate DeepSeek Harness seat (`[DSH]`).  Never `[MONET]`.


**Ownership split (2026-09-01).**  BF-Housekeeper (BotFleet) owns **this Mac**.  GB-HOUSEKEEPER (Grok Bot) owns the **Hetzner/Coolify host**.  Neither crosses.  During the Grok-Bot → BotFleet transition both must work, so they de-conflict through a shared lock rather than a schedule.

Act on regenerable waste.  Do not only report.  Ask before deleting anything that `npm ci` / a rebuild cannot restore.

## Decide whether to act at all

This is the most important step, and the one that was missing.  This is a **16 GiB** Mac; swap is the only mechanism converting RAM pressure into disk consumption, and swapfiles share the APFS container with user data.

- **Free disk > 80G** → not a disk problem.  Report and stop.  Do not clean.  (80G is the reclaim engine's healthy band, `config/reclaim-policy.json` `bands.disk.healthyFreeGb`.)
- **Free < 80G** → run the playbook.  Pressure changes the **shape** of the run, never its **scope** (owner ruling 2026-10-02).

**Pressure shapes the work, it never shrinks it.**  The old rule — "swap ≥ 90% or load1 > 40 → cheap truncations only, do not run CleanMyMac" — was a guard whose threshold sat *inside* this host's normal operating band: load1 runs 15-30 here continuously from real fleet build work, so it was true almost always, and wedged to a near-permanent swap reading it meant cleaning almost never ran.  A guard whose threshold equals the normal state is an outage, not a guard.

Under pressure, **chunk** instead of cutting: two to four bounded chunks, fewer targets per chunk and a longer pause between them (2 targets and a 2x pause under pressure, 1 target and a 4x pause while thrashing), re-reading `df` and `uptime` between chunks.  Never key a skip on load1 alone — it is a lagging indicator that counts uninterruptible page-in as running.  Real thrash is high load1 with **cpu idle below 12%**.  The "cheap truncations only" tier is retired: CleanMyMac's **RAM** mode is the thing that must never run (see below), and its cleanup mode is part of the safe tier.

Deleting caches never reduces swap.  If the pressure is swap or load only, say so and stop — that is a statement about swap, not a licence to skip disk work when the disk is also low.

## Take the lock

Several schedulers reach the same playbook and they all take the **same** lock, so two of them never reclaim at once: `mac-cleanup` every 4h, `mac-resource-watch` every 5m, **Hog Hunter's robotic vacuum every 300s** (the primary automatic path since the older disk-janitor daemon was retired on 2026-10-11), the BotFleet routines at 09:00 / 15:00 / 21:00, the resource webhook, plus a live bot thread.  `mac-auto-cleanup.sh` takes a **re-entrant owner-token lock**; callers that already hold it export `HOUSEKEEPER_LOCK_OWNER=1` before spawning children, so the parent does not deadlock its own subprocesses.  The lock is a directory, and it is treated as stale after 120 minutes.  Its exact path is private deployment inventory: it lives in the operator's own inventory on that Mac, not in this pack.  The path arrives in the environment as `HOUSEKEEPER_LOCK`, the variable the shipped cleanup script exports to the children it spawns, so a caller that already holds the lock inherits the real path and a caller that does not have to supply it before it can take the lock at all.

```bash
export HOUSEKEEPER_LOCK_OWNER=1
: "${HOUSEKEEPER_LOCK:?set HOUSEKEEPER_LOCK to the lock path in the operator inventory before taking the lock}"
LOCK_DIR="$HOUSEKEEPER_LOCK"
release_locks() {
  rmdir "${LOCK_DIR:-}" 2>/dev/null
  if [ "${REMOTE_LOCK_HELD:-0}" = "1" ]; then
    ssh "$HOUSEKEEPER_SSH_ALIAS" "rmdir /run/fleet-housekeeper.lock" >/dev/null 2>&1
  fi
}
python3 - "$LOCK_DIR" <<'PY'
import os
import sys

try:
    os.mkdir(sys.argv[1])
except FileExistsError:
    raise SystemExit(75)
except OSError as exc:
    print("housekeeper lock: cannot create the lock directory: %s" % (exc.strerror or exc), file=sys.stderr)
    raise
PY
status=$?
case "$status" in
  0) ;;
  75) echo "skipped, peer holds lock"; exit 0 ;;
  *) exit "$status" ;;
esac
trap release_locks EXIT
```

Only `FileExistsError` means a peer holds the lock, so the lock is skipped.  Any other `OSError` —
a permission problem, a missing parent, a full disk — exits nonzero and says so, because a lock that
could not be taken is not a lock another seat holds.  The atomic `mkdir` runs in Python and not in the shell, so that only `EEXIST` is read as contention: a shell `mkdir ... ||` handler reads every error as contention and reports a lock that was never taken as a peer's lock.

The snippet also fails closed when `HOUSEKEEPER_LOCK` is unset.  A placeholder path would create a lock directory relative to the caller's working directory, so the schedulers would stop sharing one lock and could reclaim at the same time, and it would leave a stray directory behind in whatever directory the agent ran from.  Requiring the variable refuses the run instead of taking the wrong lock.

## Immediate commands

```bash
df -h / /System/Volumes/Data
sysctl vm.swapusage
uptime
bash /Users/jay/apps/mac-status.sh
python3 /Users/jay/apps/mac-resource-watch.py --once
```

Safe reclaim (idempotent):

```bash
python3 /Users/jay/apps/mac-resource-watch.py --once --no-webhook   # no --clean
bash /Users/jay/apps/mac-auto-cleanup.sh --pressure
bash "<the janitor script path>"   # exact path: the operator's private inventory on that Mac
cleanmymac clean --force
```

**Never run `cleanmymac optimize ram`.**  It purges resident pages into swapfiles on the same APFS container as your data — it converts RAM pressure directly into disk consumption.  Measured **+3.6 to +7.3 GB swap per run** on 2026-09-01, with two of three runs ending with *less* free disk than they started.  It is gated off in both scripts behind `RESOURCE_ALLOW_RAM_OPTIMIZE=1`; do not set that variable.

**Do not pass `--clean`** in a routine.  It means "clean even without a hit", not "ignore the cooldown"; `--force` is the only bypass and is for manual use.

`mac-auto-cleanup` already calls CleanMyMac when the CLI is on PATH.  **Hog Hunter's robotic vacuum** owns idle/merged worktree retirement now that the older disk-janitor daemon is retired; its janitor script can still be run by hand from the operator's private inventory.  Never `rm -rf` `CoreSimulator/Devices`, never `simctl shutdown all`, never wipe the Grok worktree directory.

## Thresholds that must wake this role

These are the live defaults for the resource-watch job (every 5 min):

| Signal | Wake when | Immediate action |
|---|---|---|
| Disk free | ≤ 25G warn, ≤ 15G critical | cleanup + janitor + CleanMyMac |
| Disk drop | ≥ 15G since last sample | same, even if still above 25G |
| Swap | used ≥ 90% **and** ≥ 32G absolute | report hogs; do **not** clean disk for a RAM problem |
| Load 1m | ≥ 65 (optional, on by default) | report hogs; do not SIGKILL `com.jay.claude-remote-control` |

These are the live defaults in the resource-watch script: `RESOURCE_DISK_WARN_GB`, `RESOURCE_DISK_CRIT_GB`, `RESOURCE_DISK_DROP_GB`, `RESOURCE_SWAP_USED_PCT`, `RESOURCE_SWAP_USED_GB`, `RESOURCE_LOAD1`.  They were retuned down deliberately: the old 80G warn line ran `mac-auto-cleanup.sh --pressure` every 5 minutes back to back, and two of three runs ended with **less** free disk than they started.  The reclaim engine's bands are separate and live in the reclaim engine's own policy file (its path is in the operator's private inventory on that Mac): healthy at 80G free, acute below 40G, critical below 25G.

The watch runs the safe scripts itself, then POSTs BotFleet Housekeeper webhook `resource.disk` / `resource.ram` / `resource.cpu` / `resource.pressure`.  Cooldown 4h for the webhook, 2h between cleanup runs, 30m when the disk is critical.  Webhook URL and secret live in Infisical Fleet-OPS `/botfleet-housekeeper-webhook` (read with `fleet-secrets get`, never print).

## BotFleet Housekeeper

The BotFleet housekeeper bot id is private resource inventory, so it is not in this pack: it lives in the operator's own inventory on that Mac.  Routines at 09:00 / 15:00 / 21:00 plus the resource webhook.  The local webhook receiver and the BotFleet server address are private deployment inventory: they live in the operator's own inventory on that Mac, not in this pack.

**Status of in-app resource triggers (verified 2026-09-01, do not assume otherwise).**  PR #65 added an Automations → Resources tab, but it is **not yet live on this host**: `~/.botfleet/resource-triggers.json` does not exist (zero triggers), and the packaged `/Applications/BotFleet.app` was built *before* #65 merged, so `GET /api/resource-triggers` returns 404.  Until BotFleet is rebuilt, the **launchd path is the one that actually works**: the resource-watch launchd job samples every 5 min independently of the GUI and POSTs the webhook.  Do not claim the Resources tab is wired up until `GET /api/resource-triggers` returns 200.

**Do not add an AppleScript monitor.**  It would require the app to be running, which is exactly the case launchd already covers.

When this wake includes a resource payload: start with that pressure.  Re-run the playbook.  Report before/after disk, swap, load, and what was deleted.

Ask before: user Documents/photos, secrets, live dirty worktrees, CoreSimulator Devices, in-session Grok worktrees, anything with `.janitor-keep`.  Do not persist TCPMSS.  Do not change sysctl or network settings.  Do not extra-ship.  Do not post to Zulip unless kicking grok at a repo or after a completed app update.

## Grok Bot Housekeeper (`GB-HOUSEKEEPER`)

Cloud cannot `df` this Mac.  Drive the Mac instead:

```bash
# seat-mcp / grok-drive: inject into an idle Mac Grok TUI
python3 ~/apps/grok-acp-runtime/grok-drive.py --help
```

Or hop via `https://agents.jays.services/mcp` (`drive-grok-tui` skill) and inject:

`Run python3 /Users/jay/apps/mac-resource-watch.py --once then report disk free, swap, load, and what cleanup did.`

Do not skip that hop and invent a cloud-side `du`.

## Hetzner — GB-HOUSEKEEPER's playbook

The host address is private deployment inventory, so it is not in this pack: it lives in the operator's own inventory on that Mac.  The ssh target below is private deployment inventory too: it arrives in the environment as `HOUSEKEEPER_SSH_ALIAS`, and the operator's own ssh config is what resolves it, which is how every other fleet document reaches the same box.  The snippet fails closed when `HOUSEKEEPER_SSH_ALIAS` is unset, because a placeholder target would reach the wrong host instead of the one the caller meant.

Take the lock or exit.  Sample and record before-numbers.

```bash
: "${HOUSEKEEPER_SSH_ALIAS:?set HOUSEKEEPER_SSH_ALIAS to the ssh target in the operator inventory before reaching the host}"
release_locks() {
  rmdir "${LOCK_DIR:-}" 2>/dev/null
  if [ "${REMOTE_LOCK_HELD:-0}" = "1" ]; then
    ssh "$HOUSEKEEPER_SSH_ALIAS" "rmdir /run/fleet-housekeeper.lock" >/dev/null 2>&1
  fi
}
REMOTE_LOCK_HELD=0
ssh "$HOUSEKEEPER_SSH_ALIAS" 'python3 -' <<'PY'
import os
import sys

try:
    os.mkdir("/run/fleet-housekeeper.lock")
except FileExistsError:
    raise SystemExit(75)
except OSError as exc:
    print("housekeeper lock: cannot create the remote lock directory: %s" % (exc.strerror or exc), file=sys.stderr)
    raise
PY
status=$?
case "$status" in
  0) REMOTE_LOCK_HELD=1 ;;
  75) echo "skipped, a peer holds the remote lock"; exit 0 ;;
  *) exit "$status" ;;
esac
trap release_locks EXIT
ssh "$HOUSEKEEPER_SSH_ALIAS" 'df -h /; free -h; swapon --show; uptime; docker system df'
ssh "$HOUSEKEEPER_SSH_ALIAS" 'docker image prune -af --filter "until=168h"; docker builder prune -af --filter "until=168h"; find /opt/actions-runner-ct-*/_diag -type f -name "*.log" -mtime +14 -delete; find /var/log/fleet-backup -name "sqlite-*.log" -mtime +14 -delete'
```

The remote lock is taken the way the Mac lock is taken.  The atomic `mkdir` runs in Python on the host, so only `FileExistsError` is read as contention; every other `OSError` is reported and re-raised, so the traceback keeps the original error and the shell fails closed instead of looking like contention.  The shell reads the host's exit status before anything else runs, so a peer that holds the lock stops the run before the sampling commands reach the box.  Both playbooks install the same `release_locks` function and the same `trap release_locks EXIT`, because bash keeps one trap per signal and a second `trap` would silently replace the first; the function releases the Mac lock directory and the remote lock directory, and `REMOTE_LOCK_HELD` is set only after the remote lock is really taken, so a run that stopped on a peer's lock never removes that peer's lock.  The prune commands run inside the locked block, so the lock is held from the first sample to the last deletion and the two playbooks can share one shell without leaking a lock.  A run killed hard enough that the trap never runs leaves the directory behind, and every later run then reports a held lock; remove it by hand once the box is idle.

Act without asking — regenerable only: the prune commands run on the host inside the locked block above, so the remote lock is held from the first sample to the last deletion.

**Never, on the server:**
- `docker system prune --volumes` in any form.  The five named volumes hold ~38 GB of **live production data**, including the 27G socratic app-data volume (with the 9.8G `app.db`) and 6.1G of Qdrant storage.
- `echo 3 > /proc/sys/vm/drop_caches`.  buff/cache is reclaimable by definition; dropping it forces cold re-reads under a 9.8G SQLite file.
- Any path under `…/_data/sec-artifacts` — it is the **live read-fallback corpus path** (`corpus-layout.ts:112-123`), not a stale cache.  Deleting it means re-downloading ~3,788 SEC filings.
- `/data/coolify/*` — Coolify's own state, and it has **no backup** (`/data/coolify/backups` is empty).
- `/data/backups/*/*.db` lacking a `.sha256` sidecar, or whose mtime is still advancing — in-progress or corrupt snapshots.
- `swapoff`/`swapon`, any sysctl, network, or TCPMSS change.

**Escalate, do not fix:** root disk above 80% used (it is ~38% today), any restart-looping container, and the runaway `sqlite3` backup loop (a `/data/backups/*.db` whose size is frozen while its mtime advances — report the pid, do not kill it).

Do not treat Coolify API status as disk truth — `df` the host.

**Gate the Mac→server hop.**  `mac-auto-cleanup.sh` SSHes into production and runs `/etc/cron.daily/coolify-auto-maintenance` gated only on reachability.  Mac disk pressure is not a reason to prune a remote box sitting at 38% used.

## Never leak argv

Never run bare `ps`, or `pgrep -l` / `-fl` / `-lf` — all of them print full command lines, and on this host ~50 processes carry live API keys in argv.  Use `pgrep -f <pattern>` for PIDs or `pgrep -c -f <pattern>` to count.  `~/.claude/hooks/secret-guard-pretooluse.py` blocks these, including inside `$( )` and backticks.  The same hook denies `od` / `xxd` / `hexdump` / `cut -c` / last-command `printf %s` of `$NAME` when NAME looks like KEY / TOKEN / SECRET / PASSWORD / DSN.
