# 2026-08-16 — scheduled / on-trigger jobs actually fire

## Context & Objective

Owner: keep non-running processes operational when needed or triggered.
Always-on restarter already landed (#38).  Scheduled jobs are supposed to
stay idle between fires.  Two of them were idle forever: leftover mkdir
locks made every tick exit 0.

## Changes Made

- Cleared stale locks: disk-janitor (since 2026-08-11) and merge-shepherd
  (since 2026-07-14).  Both scripts now steal a lock older than 2h.
  Fired janitor once: new log line `2026-08-16 17:36`.
- Watch keeps scheduled launchd jobs **loaded** (bootstrap if missing).
  Does **not** kickstart idle timers.  Never bootstraps
  `com.jay.ios-ship-now` (RunAtLoad would ship TestFlight) or `com.PM2`.
- Watch steals those same stale locks and checks trigger script paths
  exist.
- pm2 daemon check uses `~/.pm2/pm2.pid` + `kill -0`.  `pgrep -f` on this
  Mac misses the God Daemon and was about to resurrect every 2 min.
- Hetzner daily cron retargeted to live host `<HETZNER_SERVER_ID>` / `nbg1-dc3`.
  HIT only on a cheaper 8-vCPU than current `cx43`.  curl `--max-time 20`.
  Old hel1 id `149429403` was deleted; the cron had been shouting a false
  HIT every morning.

Left alone on purpose: `com.jay.imessage-grok` (FDA disabled),
`com.jay.ios-ship-now` (one-shot at login, last exit 1 was a real ship
failure), vendor Homebrew autoupdate (exit 1 = Xcode 26.6 vs brew wanting
27), GoogleUpdater.wake (vendor, not loaded).

Touched:

- Live: `~/apps/mac-process-watch.sh`, `~/apps/check-hetzner-cx43.sh`,
  `~/apps/MAC-LOCAL-PROCESSES.md`
- Live: `~/.claude-disk-janitor/janitor.sh`, `~/.claude-merge-shepherd/run.sh`
- Tracked: `scripts/mac-process-watch.sh`, `scripts/check-hetzner-cx43.sh`,
  `docs/MAC-LOCAL-PROCESSES.md`, this note

## Decisions & Trade-offs

- Scheduled = loaded + idle.  Treating no-pid as DOWN would fire janitor /
  shepherd / cleanup every 2 minutes.
- Do not auto-enable imessage-grok.  KeepAlive would crash-loop without FDA.
- Hetzner script still reads `HCLOUD_TOKEN` from the handoff file the same
  way (value never printed).  Cron is the runtime path; do not run that
  grep from an agent transcript.

## Verification State

```
# locks cleared; janitor produced a fresh line
tail -1 ~/.claude-disk-janitor/janitor.log
# 2026-08-16 17:36  free=71G ...

# all agent-owned timers loaded
launchctl print gui/$(id -u)/com.jay.disk-janitor   # loaded, idle
# same for merge-shepherd, mac-process-watch, mac-server-watchdog,
# antigravity-usage-collector, mac-cleanup, provider-knob-sync, ios-ship-now

bash -n ~/apps/mac-process-watch.sh
bash ~/apps/mac-process-watch.sh   # exit 0, no false pm2 resurrect
```

Shepherd was started; it scans open ST PRs and can take minutes.  Next
30-min tick will finish if this session's run was cut short.

## Next Steps & Blockers

None required.  Owner still needs System Settings FDA for Xcode
`Python.app` before `com.jay.imessage-grok` can be enabled.  Homebrew
autoupdate will keep exiting 1 until Xcode is 27.
