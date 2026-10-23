---
name: housekeeper
description: Mac and Hetzner disk/RAM/CPU housekeeping. Run safe cleanup scripts, CleanMyMac CLI, and wake BotFleet Housekeeper on resource-threshold triggers. Use when disk is tight, swap is high, load is elevated, the owner asks to free space, or the Housekeeper / GB-HOUSEKEEPER role is on duty.
---

# Housekeeper (disk, RAM, CPU)

> **This install is for `MA`.**  Chat tag `[MA·session8]`.  Notes `Muse Assist`.  Branches `muse-assist/`.  No Mac lane (cloud VM):  use the durable VM checkout `muse-assist-<repo>`, one per repo.  Do not inherit another seat's tag from a shared template.  Zulip bot `muse-assist-bot@simplewithus.zulipchat.com`, credential in Infisical Fleet-OPS `/zulip` (`MA_ZULIP_EMAIL`, `MA_ZULIP_KEY`, `MA_ZULIP_SITE`).  Session tag `[MA·session8]`, and the `simple-chat` CLI writes it for you.

> **Cloud VM batch agent.** Muse Assist (`[MA]`, former tag `[MUSE]`) is the Meta Muse cloud VM batch compute and creative assistant dispatched via Mac/iOS apps.  Unmetered VM compute for multi-day heavy jobs (transcoding, large migrations).  Distinct from **Muse Code** (`[MC]`, branches `muse-code/`).  This catalog copy is reference-only — do not install to `~/.muse`.


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

Several schedulers reach the same playbook and they all take the **same** lock, so two of them never reclaim at once: `mac-cleanup` every 4h, `mac-resource-watch` every 5m, **Hog Hunter's robotic vacuum every 300s** (the primary automatic path since `com.jay.disk-janitor` was retired on 2026-10-11), the BotFleet routines at 09:00 / 15:00 / 21:00, the resource webhook, plus a live bot thread.  `mac-auto-cleanup.sh` takes a **re-entrant owner-token lock**; callers that already hold it export `HOUSEKEEPER_LOCK_OWNER=1` before spawning children, so the parent does not deadlock its own subprocesses.  The lock is the directory `~/.claude-disk-janitor/.housekeeper.lock`, and it is treated as stale after 120 minutes.

```bash
export HOUSEKEEPER_LOCK_OWNER=1
mkdir ~/.claude-disk-janitor/.housekeeper.lock 2>/dev/null || { echo "skipped, peer holds lock"; exit 0; }
trap 'rmdir ~/.claude-disk-janitor/.housekeeper.lock 2>/dev/null' EXIT
```

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
bash /Users/jay/.claude-disk-janitor/janitor.sh
cleanmymac clean --force
```

**Never run `cleanmymac optimize ram`.**  It purges resident pages into swapfiles on the same APFS container as your data — it converts RAM pressure directly into disk consumption.  Measured **+3.6 to +7.3 GB swap per run** on 2026-09-01, with two of three runs ending with *less* free disk than they started.  It is gated off in both scripts behind `RESOURCE_ALLOW_RAM_OPTIMIZE=1`; do not set that variable.

**Do not pass `--clean`** in a routine.  It means "clean even without a hit", not "ignore the cooldown"; `--force` is the only bypass and is for manual use.

`mac-auto-cleanup` already calls CleanMyMac when the CLI is on PATH.  **Hog Hunter's robotic vacuum** owns idle/merged worktree retirement now that `com.jay.disk-janitor` is retired; `~/.claude-disk-janitor/janitor.sh` still exists and can be run by hand.  Never `rm -rf` `CoreSimulator/Devices`, never `simctl shutdown all`, never wipe `~/.grok/worktrees`.

## Thresholds that must wake this role

These are the live defaults for `com.jay.mac-resource-watch` (every 5 min):

| Signal | Wake when | Immediate action |
|---|---|---|
| Disk free | ≤ 25G warn, ≤ 15G critical | cleanup + janitor + CleanMyMac |
| Disk drop | ≥ 15G since last sample | same, even if still above 25G |
| Swap | used ≥ 90% **and** ≥ 32G absolute | report hogs; do **not** clean disk for a RAM problem |
| Load 1m | ≥ 65 (optional, on by default) | report hogs; do not SIGKILL `com.jay.claude-remote-control` |

These are the live defaults in `~/apps/mac-resource-watch.py`: `RESOURCE_DISK_WARN_GB`, `RESOURCE_DISK_CRIT_GB`, `RESOURCE_DISK_DROP_GB`, `RESOURCE_SWAP_USED_PCT`, `RESOURCE_SWAP_USED_GB`, `RESOURCE_LOAD1`.  They were retuned down deliberately: the old 80G warn line ran `mac-auto-cleanup.sh --pressure` every 5 minutes back to back, and two of three runs ended with **less** free disk than they started.  The reclaim engine's bands are separate and live in `~/Code/HogHunter/config/reclaim-policy.json`: healthy at 80G free, acute below 40G, critical below 25G.

The watch runs the safe scripts itself, then POSTs BotFleet Housekeeper webhook `resource.disk` / `resource.ram` / `resource.cpu` / `resource.pressure`.  Cooldown 4h for the webhook, 2h between cleanup runs, 30m when the disk is critical.  Webhook URL and secret live in Infisical Fleet-OPS `/botfleet-housekeeper-webhook` (read with `fleet-secrets get`, never print).

## BotFleet Housekeeper

Bot id `d43849b8-5eeb-452b-ac4e-ed4724343838`.  Routines at 09:00 / 15:00 / 21:00 plus the resource webhook.  Local webhook receiver `127.0.0.1:8800`.  BotFleet server `127.0.0.1:8799`.

**Status of in-app resource triggers (verified 2026-09-01, do not assume otherwise).**  PR #65 added an Automations → Resources tab, but it is **not yet live on this host**: `~/.botfleet/resource-triggers.json` does not exist (zero triggers), and the packaged `/Applications/BotFleet.app` was built *before* #65 merged, so `GET /api/resource-triggers` returns 404.  Until BotFleet is rebuilt, the **launchd path is the one that actually works**: `com.jay.mac-resource-watch` samples every 5 min independently of the GUI and POSTs the webhook.  Do not claim the Resources tab is wired up until `GET /api/resource-triggers` returns 200.

**Do not add an AppleScript monitor.**  It would require the app to be running, which is exactly the case launchd already covers.

When this wake includes a resource payload: start with that pressure.  Re-run the playbook.  Report before/after disk, swap, load, and what was deleted.

Ask before: user Documents/photos, secrets, live dirty worktrees, CoreSimulator Devices, in-session `~/.grok/worktrees`, anything with `.janitor-keep`.  Do not persist TCPMSS.  Do not change sysctl or network settings.  Do not extra-ship.  Do not post to Zulip unless kicking grok at a repo or after a completed app update.

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

GB-HOUSEKEEPER **owns** this host and does **not** clean the Mac (it may read Mac metrics to correlate, and must say so rather than acting).  Host `167.233.254.55`, ssh alias `coolify`.

Take the lock or exit.  Sample and record before-numbers.

```bash
ssh coolify 'mkdir /run/fleet-housekeeper.lock 2>/dev/null || exit 9'   # 9 = peer holds it, stop
ssh coolify 'df -h /; free -h; swapon --show; uptime; docker system df'
```

Act without asking — regenerable only:

```bash
docker image   prune -af --filter "until=168h"
docker builder prune -af --filter "until=168h"
find /opt/actions-runner-ct-*/_diag -type f -name '*.log' -mtime +14 -delete
find /var/log/fleet-backup -name 'sqlite-*.log' -mtime +14 -delete
```

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
