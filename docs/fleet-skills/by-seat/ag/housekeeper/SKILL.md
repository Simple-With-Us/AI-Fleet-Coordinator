---
name: housekeeper
description: Mac and Hetzner disk/RAM/CPU housekeeping. Run safe cleanup scripts, CleanMyMac CLI, and wake BotFleet Housekeeper on resource-threshold triggers. Use when disk is tight, swap is high, load is elevated, the owner asks to free space, or the Housekeeper / GB-HOUSEKEEPER role is on duty.
---

# Housekeeper (disk, RAM, CPU)

> **This install is for `AG`.**  Chat tag `[AG·session8]`.  Notes `Antigravity`.  Branches `ag/`.  Worktrees `~/apps/<app>-antigravity`.  Do not inherit another seat's tag from a shared template.  Zulip bot `ag-bot@simplewithus.zulipchat.com`, credential file `~/.secrets/Zulip/AG-zuliprc` (mode 600).  Session tag `[AG·session8]`, and the `agent-sync` CLI writes it for you.


> **Shared `~/.claude/skills`.** Monet, Claude/Fable, and (when active) Renoir all load this directory.  Do not treat the word Monet in examples as proof of your seat.  Pin `AGENT_SEAT` from the logged-in account before a Zulip post or `board --by`:
> - Monet → `MONET`, Notes `Monet`, `ag/`, `~/apps/lanes/<prefix>/monet-<slug>`
> - Claude / Fable → `CLAUDE`, Notes `Claude`, `claude/`, `~/apps/lanes/<prefix>/claude-<slug>`
> - Renoir → `RENOIR` (retired), Notes `Renoir`, `renoir/`
> Codex, Cursor, Grok, Grok-Web, AG, Clutch, FX, MM, MC, and MA have their own skill dirs and must not take identity from here.


**Ownership split (2026-09-01).**  BF-Housekeeper (BotFleet) owns **this Mac**.  GB-HOUSEKEEPER (Grok Bot) owns the **Hetzner/Coolify host**.  Neither crosses.  During the Grok-Bot → BotFleet transition both must work, so they de-conflict through a shared lock rather than a schedule.

Act on regenerable waste.  Do not only report.  Ask before deleting anything that `npm ci` / a rebuild cannot restore.

## Decide whether to act at all

This is the most important step, and the one that was missing.  This is a **16 GiB** Mac; swap is the only mechanism converting RAM pressure into disk consumption, and swapfiles share the APFS container with user data.

- **Free disk > 80G** → not a disk problem.  Report and stop.  Do not clean.
- **load1 > 40 or swap ≥ 90%** → the machine is thrashing.  Heavy cleanup costs more than it returns (measured 2026-09-01: a run freed −1.21G and *added* +1.84G swap).  Cheap truncations only; do **not** run CleanMyMac.
- **Free < 80G and load1 < 40** → run the full playbook.

Deleting caches never reduces swap.  If the pressure is swap or load only, say so and stop.

## Take the lock

Six schedulers reach the same playbook (`mac-cleanup` 4h, `disk-janitor` 30m, `mac-resource-watch` 5m, three routines, one webhook, plus a live bot thread).  `mac-auto-cleanup.sh` now takes a **re-entrant owner-token lock**; callers that already hold it export `HOUSEKEEPER_LOCK_OWNER=1` before spawning children, so the parent does not deadlock its own subprocesses.

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

`mac-auto-cleanup` already calls CleanMyMac when the CLI is on PATH.  Disk janitor owns idle/merged worktree retirement.  Never `rm -rf` `CoreSimulator/Devices`, never `simctl shutdown all`, never wipe `~/.grok/worktrees`.

## Thresholds that must wake this role

These are the live defaults for `com.jay.mac-resource-watch` (every 5 min):

| Signal | Wake when | Immediate action |
|---|---|---|
| Disk free | ≤ 80G warn, ≤ 50G critical | cleanup + janitor + CleanMyMac |
| Disk drop | ≥ 6G since last sample | same, even if still above 80G |
| Swap | used ≥ 80% **and** ≥ 8G absolute | report hogs; do **not** clean disk for a RAM problem |
| Load 1m | ≥ 16 (optional, on by default) | report hogs; do not SIGKILL `com.jay.claude-remote-control` |

The watch runs the safe scripts itself, then POSTs BotFleet Housekeeper webhook `resource.disk` / `resource.ram` / `resource.cpu` / `resource.pressure`.  Cooldown 45 min.  Secret file `~/.secrets/botfleet-housekeeper-webhook.env` (never print).

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
