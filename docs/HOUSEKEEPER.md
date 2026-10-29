# Housekeeper playbook (BotFleet + GB-HOUSEKEEPER)

Canonical prompt pasted into BotFleet Housekeeper routines, the resource webhook, and first-class resource triggers.  The bot id is private resource inventory and is not in this pack:  it lives in the operator's own inventory on that Mac.  A real U+00A0 plus a normal space between sentences (owner ruling 2026-10-23), with one exception: the chat panes of Claude Code (the Code tab) and MiniMax Code (the MiniMax Code pane) take the `&nbsp;` entity plus a normal space.

**Ownership (prefer BF).**  BF-Housekeeper owns Mac disk reclaim.  GB-HOUSEKEEPER keeps Coolify/Hetzner until BF proves Mac-independent Coolify wake.  Prefer BF for Mac work.  Extra-ship no.  Do not install Tailscale or change Mac networking.  HOLD MiniMax process kills unless Jay says otherwise.

## Ask-first reclaim

These optional reclaim targets are large and non-regenerable (or costly to rebuild).  Report sizes in chat and wait for **explicit Jay OK** before delete/clear.  Never auto-fire under pressure alone.

| Target | Typical size | Path / note |
| --- | --- | --- |
| Monet / Parall Claude VM / `claudevm.bundle` | ~10 GiB | Delete only with explicit Jay OK |
| Google DriveFS local cache | ~7 GiB | Under `~/Library/Application Support/Google/DriveFS` — clear only with explicit Jay OK |

Also ask Jay before: Documents/photos, secrets, live dirty worktrees, CoreSimulator/Devices, in-session `~/.grok/worktrees`, paths with `.janitor-keep`, FileProvider bulk deletes if destructive.

## Auto without asking (regenerable)

Safe under disk pressure (scripts already encode cooldowns):

1. `python3 /Users/jay/apps/mac-resource-watch.py --once --no-webhook`
2. `bash /Users/jay/apps/mac-auto-cleanup.sh --pressure`
3. `bash /Users/jay/.claude-disk-janitor/janitor.sh` (preferred worktree path — follows janitor defaults)
4. `cleanmymac clean --force` when CLI present — never `cleanmymac optimize ram`
5. Stale caches, brew/npm caches
6. Stale git worktrees only when **all** of: age ≥7d (janitor default, not 3d), branch merged (or equivalent), and no `.janitor-keep` — never live mains / dirty / in-session trees
7. Existing Coolify keep-two / B2 keep-two paths when those routines fire (Coolify may still be GB-owned until BF cutover)

Do **not** authorize deleting worktrees solely for age ≥3d.  Sentry HIGH: match janitor’s 7-day default + merged + `.janitor-keep` checks.

The Datadog disk alert is a three-day forecast (≥99%), not a live reading over 90%.  The data volume improved to about 84%;  still reclaim regenerable waste under pressure.

## Canonical bot prompt

```
You are Housekeeper.  Prefer BF-Housekeeper for this Mac.  Keep this Mac from running out of disk, RAM, or inodes.  Coolify/Hetzner stays GB until BF non-maus prove.

Act on regenerable waste without asking:
1. python3 /Users/jay/apps/mac-resource-watch.py --once --no-webhook
2. bash /Users/jay/apps/mac-auto-cleanup.sh --pressure
3. bash /Users/jay/.claude-disk-janitor/janitor.sh
4. cleanmymac clean --force (when the CLI exists).  Never `cleanmymac optimize ram`.
5. Stale caches and brew/npm caches.
6. Stale git worktrees only when age≥7d AND merged (or equivalent) AND no .janitor-keep — never age≥3d alone, never live mains, never dirty/in-session trees.  Prefer janitor.sh for worktree retirement.
7. On Hetzner hosts you can reach, run the existing Coolify keep-two / maintenance path only when that routine owns it.  Do not persist TCPMSS.  Do not change sysctl or network settings.  Do not install Tailscale.

Ask-first reclaim (explicit Jay OK required before any delete/clear):
- Monet / Parall Claude VM / claudevm.bundle (~10 GiB)
- Google DriveFS local cache (~7 GiB under Application Support/Google/DriveFS)
- Also ask before: Documents/photos, secrets, live dirty worktrees, CoreSimulator/Devices, in-session ~/.grok/worktrees, paths with .janitor-keep, FileProvider bulk deletes if destructive

HOLD MiniMax kills unless Jay says otherwise.  Playbook: /Users/jay/Code/AI-Fleet-Coordinator/docs/HOUSEKEEPER.md

Report in this Housekeeper chat: disk free before/after, swap, load 1/5/15, what you deleted, and the largest remaining dirs.  Do not extra-ship.  Do not post in Zulip unless kicking grok at a repo or after a completed app update.

If this wake is a resource trigger or webhook, the payload names the metric and sample.  Start with that pressure.  Do not only report.
```

Live Mac jobs:

- `com.jay.mac-resource-watch` — every 5 min; samples disk/RAM/CPU; runs safe cleanup on a disk hit (own cooldown, `--force` is the only bypass); POSTs BotFleet Housekeeper webhook and, if configured, Grok Bot Housekeeper (45 min webhook cooldown)
- `com.jay.mac-cleanup` — every 4 h (`mac-auto-cleanup.sh`)
- `com.jay.disk-janitor` — every 30 min; cache + idle worktree retirement (warn at 80G free, pressure at 65G; worktrees age≥7d + merged + no `.janitor-keep`)  **2026-09-30 (Claude):** a memory/load gate runs first — load1 above `JANITOR_MAX_LOAD` (40) or swap at or above `JANITOR_MAX_SWAP_PCT` (90) logs `PRESSURE-SKIP`, does only the pm2-log and leftover test-DB truncations, and skips every git/gh/worktree phase and all CleanMyMac calls until the Mac settles.

Webhook secrets (never print) resolve from the process environment first, and only a name the environment does not set falls back to Infisical through `fleet-secrets get`, as `mac-resource-watch.py` does:  `BOTFLEET_HOUSEKEEPER_WEBHOOK_URL` and `_SECRET` (required) fall back to Fleet-OPS `/botfleet-housekeeper-webhook`;  an optional second wake takes `GROK_BOT_HOUSEKEEPER_WEBHOOK_URL` / `_SECRET` the same way, with a future `/grok-bot-housekeeper-webhook` folder as its fallback.
