# 2026-09-17 — Mac launchd exit-1 + Datadog coverage (GROK)

## Why

AG chat asked GROK to resolve local Mac launchd exit-1 jobs (`fleet-gdrive-backup`, `ios-ship-now`, `botfleet-server`), explain why Datadog is not fully working for every app, and comment-only on ST R2 free-tier usage.

## What landed

- **gdrive backup:** repo zips already succeeded.  The job exited 1 on `rsync -a --delete` into Google Drive File Provider (`open: Operation not permitted` on `fleet-agent-config/gemini/skills`).  Mirror is now in-process `copy_mirror`.
- **botfleet-server:** launchd KeepAlive crash-looped (~10k) on missing `node_modules` while BotFleet.app still served `:8799`/`:8800` 200.  New start wrapper exits 0 when the harness is healthy; KeepAlive is `SuccessfulExit=false`; watch treats `:8799 /health` as UP.
- **ios-ship-now:** leftover login one-shot.  Last failure 2026-09-17 01:34 EEST was missing `*-grok-tf-runner` checkouts.  Live `ship-now-gui.sh` ships from `~/Code/*` and skips a missing root.  Do not kickstart this label.
- **Datadog:** coverage matrix in Fleet-OPS `docs/DATADOG-INTEGRATION-GUIDE.md`.  Free plan is host + LLM spans.  ST/CT/DealDex already ship logs+APM (conversion trap).  ST RUM app exists but is inactive.  Do not mint more RUM apps.

## Verify

```bash
python3 scripts/test_sync_fleet_agent_config.py
bash scripts/test-botfleet-server-start.sh
bash scripts/test-disk-janitor-match.sh
bash scripts/test-mac-process-watch-dump.sh
curl -sf -m 2 http://127.0.0.1:8799/health
python3 ~/apps/fleet-gdrive-backup/sync-fleet-agent-config-to-gdrive.py --list
```
