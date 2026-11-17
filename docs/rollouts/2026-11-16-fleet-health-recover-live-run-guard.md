# 2026-11-16 — fleet-health-recover: /api/live probe and strategy-run guard for socratic-app (ST-4635)

Linear: https://linear.app/simple-with-us/issue/ST-4635

## Context & Objective

On 2026-11-16, `fleet-health-recover@socratic-app` restarted the ST container at 8:56:33am CT and again at 9:11:28am CT.  Each restart killed a strategy run in progress (8:46:59am CT, then its 9:01:54am CT retry), and the trading-liveness monitor raised `stale_last_completed_run`.  The probe was `/api/health`, which runs SQLite reads on the 22 GB `app.db`, and those reads went past the 12s curl limit.  Jay approved the change on 2026-11-16 at 6:25pm CT: "Yes, switch the watchdog to /api/live and have it wait while a run is in progress."

## Changes Made

- `scripts/host/fleet-health-recover.sh` has an opt-in run guard.  When `RUN_GUARD_DB` is set and a restart is due, the script runs a read-only `sqlite3` query (`-readonly`, `.timeout 2000`, wrapped in `timeout 10`).  It counts `strategy_runs` rows with `status='running'` that started within the last 3h, and unexpired `settings` `strategy_run_lock:*` leases.  If either count is nonzero, or the DB can't be read, the restart is deferred and a log line is written.  The deferral is capped at `RUN_GUARD_MAX_DEFER_SEC` (default 900) from the first deferral, after which the script restarts as before.  The deferral clock resets when health recovers or a restart goes through.
- New test hooks: `--run-guard-state <db>` and `--run-guard-defer <state> <since> <now>`.  Cases were added to `test-fleet-health-body.sh`.  The sqlite cases are skipped when there is no `sqlite3`.
- `socratic-app.env.example`: the probe is now `/api/live`, and it documents `RUN_GUARD_DB` and `RUN_GUARD_MAX_DEFER_SEC`.
- Leaving `RUN_GUARD_DB` unset keeps the old behaviour, so `usage-monitor` is unchanged.

## Host state (applied 2026-11-16 6:30pm CT)

- Backups: `/root/backups/fleet-health-recover-st4635-20261117T003012Z/` (script, `socratic-app.env`, unit, `SHA256SUMS`).  Sibling copies: `/usr/local/bin/fleet-health-recover.sh.bak-st4635-20261117T003012Z` and `/etc/fleet-health-recover.d/socratic-app.env.bak-st4635-20261117T003012Z`.
- The script was installed by atomic `mv`, so the running `usage-monitor` process kept its old inode.
- `/etc/fleet-health-recover.d/socratic-app.env`: `INTERNAL_HEALTH_URL` now ends in `/api/live`, and `RUN_GUARD_DB` and `RUN_GUARD_MAX_DEFER_SEC=900` were added.
- I ran `systemctl daemon-reload` and restarted the watchdog unit `fleet-health-recover@socratic-app` only.  The app container was not restarted.

## Rollback

```bash
B=/root/backups/fleet-health-recover-st4635-20261117T003012Z
cp -a $B/socratic-app.env /etc/fleet-health-recover.d/socratic-app.env
cp -a $B/fleet-health-recover.sh /usr/local/bin/fleet-health-recover.sh.new && mv -f /usr/local/bin/fleet-health-recover.sh.new /usr/local/bin/fleet-health-recover.sh
systemctl daemon-reload && systemctl restart fleet-health-recover@socratic-app.service
```

To roll back only the guard, delete the `RUN_GUARD_*` lines from the env file and restart the unit.

## Decisions & Trade-offs

- **An unreadable DB defers the restart.**  A freeze is exactly when a run is likely in progress, and the 15-minute cap bounds the wait.
- **Old `running` rows are ignored.**  Rows that started more than 3h ago are treated as orphans so they can't defer forever.
- **`/api/live` still freezes.**  It does no DB work, but it is served by the same Node event loop.  On 2026-11-16 at 6:27:50pm CT the Docker HEALTHCHECK, which also probes `/api/live`, timed out at 14s, and at 6:28:25pm CT the watchdog restarted the app while it was still on `/api/health`.  The guard is what protects runs.  The probe switch only removes the slow-DB false positives.

## Verification State

```bash
bash -n scripts/host/fleet-health-recover.sh
shellcheck -S warning scripts/host/fleet-health-recover.sh scripts/host/test-fleet-health-body.sh
bash scripts/host/test-fleet-health-body.sh   # on the host too: "ok  run-guard-state (sqlite3)"
bash fleet-health-recover.sh --run-guard-state <prod app.db>   # "idle" in 0.02s
```

## Next Steps & Blockers

- The app-side event-loop fix is in Socratic-Trade PR #5410.
- Pruning `api_health_log` is still the owner's call.

## Zero-Code Findings

- The Docker HEALTHCHECK on `/api/live` (5 retries) and the in-container liveness watchdog can still kill the app during a long freeze, independent of this unit.
