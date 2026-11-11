# 2026-09-01 — Mac resource watch + Housekeeper playbook

## Why

Disk free fell from ~60G to 45G overnight with 6–13G drops in a single 30-minute janitor tick.  The janitor only reclaimed under 50G free, so a spike ate the remaining headroom.  BotFleet Housekeeper was ask-only (`Ask before any change`) and `autoApprove: false`, so scheduled checks never cleaned.  RAM/swap was the larger crisis (load ~400, swap ~12–17G used).

## What landed

- `com.jay.mac-resource-watch` every 5 minutes: sample disk/swap/load, run `mac-auto-cleanup.sh --pressure` on a hit, POST BotFleet Housekeeper webhook (`resource.disk` / `resource.ram` / `resource.cpu`).  Cooldown 45 min.
- Janitor warn at 80G free, pressure at 65G.  Repos now include BotFleet, fleet-ops, botfleet-site.
- `mac-auto-cleanup.sh --pressure`: CleanMyMac purge, 3-day Grok session prune, pm2 log cap, vitest temp DB reap.
- `housekeeper` fleet skill on every seat including `GB-HOUSEKEEPER` (cloud hop via `drive-grok-tui`).  `mac-cleanup` stays omitted from Grok Bot.
- Live Housekeeper: autoApprove on, acting playbook, routines 09:00 / 15:00 / 21:00, webhook `Housekeeper resource pressure`.

## Verification

- `bash scripts/test-mac-auto-cleanup.sh` OK
- `bash scripts/test-disk-janitor-match.sh` OK
- `bash scripts/test-mac-resource-watch.sh` OK
- `python3 -m unittest scripts.test_fleet_skill_identity.CatalogAndShipBanTests.test_housekeeper_allowed_for_grok_bot` OK
- launchd `com.jay.mac-resource-watch` loaded; first sample `free=72.23G hits=['disk_free_gb', 'swap']`
- After immediate cache/CleanMyMac pass: ~72G free (was 64G during the session).  Load 1m down from ~400 to ~8.

## Follow-ups

- BotFleet in-app Resources tab (`grok/resource-triggers`) needs a BotFleet restart onto that PR before `/api/resource-triggers` answers.  Launchd webhook covers the Mac meanwhile.
- Remaining bulk is live `~/apps` worktrees and BotFleet workspace clones, not caches.  Janitor now starts at 80G.
