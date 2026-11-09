# 2026-08-16 — mac-process-watch restarts always-on jobs

## Context & Objective

Quitting iTerm2 and telling macOS to stop background processes killed the
pm2 God Daemon (responsibility tree).  `com.PM2` only resurrects at login
(`LaunchOnlyOnce`), and `com.jay.mac-process-watch` only logged `DOWN`.
Shellular and the other nine pm2 jobs stayed dead until a human ran
`pm2 resurrect`.  Owner asked the 120s watch to restart those always-on
jobs unless that was a bad idea.  It is a good idea with backoff.

## Changes Made

The 120s watch still checks the same 10 pm2 jobs and 5 launchd always-on
jobs.  It now restarts downs:

- pm2 daemon missing, or 3+ jobs missing: `pm2 resurrect` (uses
  `~/.pm2/dump.pm2`).  Does not call `pm2 jlist` first when the daemon is
  dead — that used to spawn an empty daemon.
- One or two pm2 jobs `stopped`/`errored`: `pm2 restart <name>`.
- One or two pm2 jobs missing: `pm2 start ~/apps/pm2-ecosystem.config.cjs --only <name>`
  so scout keeps the stdin `/dev/null` path.
- launchd always-on not-loaded: `launchctl bootstrap` of the known plist.
- launchd always-on loaded, no pid: `launchctl kickstart` (not `-k`).

Never restarts: disabled labels (`com.jay.shellular`, `com.jay.imessage-grok`,
retired launchd), scheduled/one-shot jobs, `com.PM2`, root `cloudflared`.
Backoff: 4 restarts per key per hour, then `SKIP`.
`MAC_PROCESS_WATCH_RESTART=0` is log-only.

Touched:

- Live: `~/apps/mac-process-watch.sh` (launchd runs this)
- Live: `~/apps/mac-status.sh` (footer)
- Live: `~/apps/MAC-LOCAL-PROCESSES.md`
- Tracked: `scripts/mac-process-watch.sh`
- Tracked: `docs/MAC-LOCAL-PROCESSES.md`
- This rollout note

## Decisions & Trade-offs

- Restart from the existing 120s watch instead of turning `com.PM2` into a
  KeepAlive daemon.  The watch already has the expected-job list and can
  repair both pm2 and launchd.  `com.PM2` stays login-only resurrect.
- Bulk `pm2 resurrect` when 3+ jobs are gone (the iTerm-quit case) instead
  of 10 individual starts.
- Do not `kickstart -k` (that would kill a healthy pid).  Do not enable a
  disabled label.
- Command chatter (pm2 tables) goes to
  `~/Library/Logs/mac-process-watch.cmd.log`, not the main name/status log.
- Usage-Monitor `mac-server-watchdog` is a heartbeat to
  usage.jays.services.  It does not restart these jobs.  No overlap.

## Verification State

```
bash -n ~/apps/mac-process-watch.sh
python3 -c 'print(sum(1 for b in open(".../mac-process-watch.sh","rb").read() if b>127))'
# 0 non-ascii
pm2 stop code-main-keeper
bash ~/apps/mac-process-watch.sh
# log: DOWN pm2:code-main-keeper status=stopped
# log: RESTART pm2:code-main-keeper ok
# pm2 status: code-main-keeper online
```

Healthy run with all jobs up: exit 0, no restart, no new state row.

## Next Steps & Blockers

None.  Next iTerm quit + "stop background" should self-heal within 120s
as long as `~/.pm2/dump.pm2` exists (it does; last saved at the 14:33 die).
If a job crash-loops, the watch stops after 4 tries an hour and keeps
logging `DOWN` + `SKIP`.
