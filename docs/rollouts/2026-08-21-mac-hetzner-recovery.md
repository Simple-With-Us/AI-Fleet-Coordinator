# Mac + Hetzner recovery automation

Historical identifiers have been redacted. These records do not certify current deployment state.

Owner asked to automate recovery from the 2026-08-21 Mac collapse and for `host.jays.services`.

## Mac (launchd `com.jay.mac-process-watch` every 120s)

Already (Cursor, earlier today): poison dump → `pm2 start` ecosystem + `pm2 save`; `mac-collab-sync` in ecosystem.

Added (Grok):

- jlist-timeout kills God then `pm2_restore_bulk` (not raw `pm2 resurrect`)
- local HTTP 200 required for mac-collab, xcode-health, [retired identifier], senate-relay
- Shellular bounce on `ioreg` missing or Retrying with no Connected
- ecosystem PATH includes `/usr/sbin` (live already; now tracked)

Live: `~/apps/mac-process-watch.sh`.  Tracked: `scripts/mac-process-watch.sh`, `scripts/pm2-ecosystem.config.cjs`.

## Hetzner `host.jays.services`

Already: `congress-health-recover`, `box-disk-hygiene.timer`, `fleet-health-verify` cron (log-only), `fleet-deploy-guard@`, sqlite backups.

Added:

- `fleet-health-recover@socratic-app` and `@usage-monitor` — internal container health → docker restart + Coolify API, skip during deploy, 4/hour, Pushover
- `fleet-health-verify.sh` Pushover on FAIL (30 min)

Installed on the box 2026-08-21.  Units active.  No host reboot.

## Verify

```
bash -n ~/apps/mac-process-watch.sh
systemctl is-active fleet-health-recover@socratic-app fleet-health-recover@usage-monitor
```

Internal ST/UM `/api/health` via container IP returned 200 before enable.
