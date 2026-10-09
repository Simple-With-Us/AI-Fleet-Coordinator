---
name: mac-cleanup
description: Optimize Mac workstation and Hetzner Coolify disk space, prune merged git worktrees across all apps, clean Xcode symbols/simulators, and flush package caches. Use when checking disk health or optimizing local storage.
---

> **Read the `housekeeper` skill first when you are acting on live resource pressure.**  It carries the decide-whether-to-act gate, the shared re-entrant lock, the Mac/Hetzner ownership split, and the never-delete list.  Two rules that override anything below:
> - **Never run `cleanmymac optimize ram`.**  It purges resident pages into swapfiles on the same APFS container as user data, converting RAM pressure into disk consumption (measured +3.6-7.3G swap per run, 2026-09-01).
> - **Free disk > 80G means it is not a disk problem.**  Deleting caches cannot reduce swap.  Report and stop.


# Mac & Hetzner Disk Cleanup Skill (ALL AGENTS)

> **This install is for `CLAUDE`.**  Chat tag `[CLAUDE·session8]`.  Notes `Claude`.  Branches `claude/`.  Worktrees `~/apps/<app>-claude`.  Do not inherit another seat's tag from a shared template.  Zulip bot `claude-bot@simplewithus.zulipchat.com`, credential file `~/.secrets/Zulip/Claude-zuliprc` (mode 600).  Session tag `[CLAUDE·session8]`, and the `agent-sync` CLI writes it for you.


Optimize local workstation storage and remote Coolify server disk usage by running the unified automated maintenance sweep.

## How to Run the Cleanup

### 1. Direct Command (All Platforms)
Any agent (Antigravity, Grok, Claude, Cursor, Codex) or operator can execute the unified cleanup script directly:

```bash
bash /Users/jay/apps/mac-auto-cleanup.sh
```

Or using the CLI shortcut (available in `zsh` and `bash`):
```bash
mac-auto-cleanup
```

### 2. What It Cleans (Safe & Idempotent)

1. **Merged Git Worktrees**:
   - Owned by `com.jay.disk-janitor` (clean + idle + no `--force`).  This script does not reap worktrees or wipe `~/.grok/worktrees`.
2. **Xcode Developer Caches**:
   - Cleans `~/Library/Developer/Xcode/iOS DeviceSupport` symbols.
   - Cleans `~/Library/Developer/Xcode/DerivedData` build caches.
   - Prunes unavailable simulators only (`xcrun simctl delete unavailable`).  Never `rm -rf` `CoreSimulator/Devices`.
3. **Package Manager Caches**:
   - `npm cache clean --force` and `~/.npm/_npx`
   - `pnpm store prune`
   - `yarn cache clean`
   - `brew cleanup -s`
4. **CleanMyMac CLI Cleanup & RAM Optimization**:
   - `cleanmymac clean --force` (automatically cleans system junk, dev junk, AI tool caches, and trash bins).
   - ~~`cleanmymac optimize ram`~~ — **removed 2026-09-01.**  It purges resident pages, which on a 16G host pushes them into swapfiles on the same APFS container as user data: it converts RAM pressure into disk consumption.  Measured +3.6-7.3G swap per run, with 2 of 3 runs ending with *less* free disk.  Gated off in both scripts behind `RESOURCE_ALLOW_RAM_OPTIMIZE=1`; do not set it.
5. **Spotlight PipelineStorage Journals**:
   - Truncates every `Journals` dir under `~/Library/Metadata/CoreSpotlight/DocumentProcessing/PipelineStorage` (not only the empty Urgent pipeline).
6. **Agent Session Transcripts**:
   - Prunes Grok session transcripts older than 7 days (`~/.grok/sessions`).
   - Prunes old Codex archived sessions (`~/.codex/archived_sessions`).
   - Prunes old Antigravity brain conversation folders older than 7 days (`~/.gemini/antigravity/brain`).
7. **Remote Hetzner Coolify Server (`167.233.254.55`)**:
   - Automatically triggers remote Docker cleanup (`docker builder prune -af --filter "until=72h"` and `docker system prune -af --volumes`).

Pass `--pressure` (or `MAC_CLEANUP_PRESSURE=1`) for the tighter path: CleanMyMac purge, 3-day Grok session prune, pm2 log cap.  Resource-watch uses that flag.

## Scheduled Automation
- **Mac LaunchAgent**: `~/Library/LaunchAgents/com.jay.mac-cleanup.plist` runs automatically every 4 hours (`StartInterval: 14400`).
- **Resource watch**: `~/Library/LaunchAgents/com.jay.mac-resource-watch.plist` every 5 minutes.  Samples disk/RAM/CPU, runs this script on a hit, and POSTs BotFleet Housekeeper.  See the `housekeeper` skill.
- **Hetzner Cron**: `/etc/cron.d/coolify-maintenance` runs automatically every 4 hours.

## Quick Status Check
To view live disk space and system process health:
```bash
ms
# or
bash ~/apps/mac-status.sh
```
