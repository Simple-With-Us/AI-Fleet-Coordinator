# 2026-09-01 — Sentry org extras (sponsored account, extra-high)

## Summary

Org-side rollout after the Week 1-4 adoption PRs.  This unit is Sentry
organization configuration (alerts, uptime, crons, dashboard, metric monitors),
not another SDK PR.  Board `31bd2e3a`.  Branch `grok/sentry-org-rollout`.
Worktree `~/apps/fleet-grok-sentry-org`.  Follows plan
`docs/plans/2026-09-01-sentry-fleet-integration.md` and adoption
`docs/rollouts/2026-09-01-sentry-fleet-adoption.md`.

Personal-Site stays Datadog-only.  UptimeRobot still owns owner-facing black-box
uptime.  PagerDuty owns the phone page.

## Test page

Labeled PagerDuty incident **#86** (`Q1WHG1NFASS46N`) on service `jays.services`
(`PA87S7M`) was triggered as "TEST PAGE — ignore" then resolved.  That is the
Sentry → PagerDuty path check the plan required before replacing UptimeRobot
paging.  Open incident **#79** (Namecheap usage snapshot) was left alone.

## Project filters (the trap)

Classic issue-alert `POST /projects/{org}/{project}/rules/` is **gone** (HTTP
410).  Workflow `PUT` with `projects` / `projectIds` is **400**.  The live
scoping field is **`detector_ids`**: each project's Issue Stream detector plus
uptime / cron / metric detectors.

Do not invent a second org-wide PagerDuty workflow.  Scope `3930764` with
`detector_ids`.

## What landed (live org `jays-services`)

### Alerts

| Workflow | Id | Scope | Action |
|---|---|---|---|
| Fleet high-priority production issues → #Work-In-Progress | 3930668 | org-wide `environment=production`; also Seer `rca_completed` / `pr_ready_for_review` | Slack `#Work-In-Progress` |
| P0 production → PagerDuty (ST/UM/fleet/BF issues + prod uptime) | 3930764 | detector_ids: ST/UM/fleet/BF issue streams + ST/UM/CT/fleet/BF uptime | PagerDuty `jays.services` critical |
| Live scheduler cron failures → #Work-In-Progress | 3932162 | `scheduler-tick`, `usage-monitor-scheduler` | Slack `#Work-In-Progress` |

CT **error-stream** is intentionally **not** on PagerDuty (PDF XRef noise).  CT
**uptime** (`congress.trade /api/health`) **is** on PagerDuty because a site
outage is a real page.

### Uptime (HTTP, 60s unless noted)

Already healthy: ST `/api/health`, CT `/api/health` (browser UA), UM `/api/health`.

Added:

- `dealdex.online/` (dealdex)
- `botfleet.app/` (botfleet)
- `contactlogo.com/` (contactlogo)
- `mac.jays.services/health` (fleet-infra)
- `usage.jays.services/api/ready?strict=1` (usage-monitor, 300s)
- `autorotate.vercel.app/` (autorotate, 300s) until `autorotate.codes` DNS exists

Dashboard: **Fleet production** id `9917821`
(https://jays-services.sentry.io/dashboard/9917821/).  Prebuilts favorited:
Next.js Overview, Mobile Vitals, AI Agents Overview, Frontend Session Health.

### Metric monitors (Slack via 3930668)

Error-count spikes in 1h: ST 80, UM 30, fleet-infra 20, BotFleet 25, DealDex 25.
ST span `failure_rate()` > 15% / 1h.  Detector ids `9700541`–`9700546`.

### Cron hygiene

Disabled (were `production:error` and not earning keep):
`ci-ios-testflight-ship`, `ci-congress-trade-security`, `ci-security`.

Left disabled from Week 1-4: `watcher-cron`, `agreement-autopublish-cron`,
`fleet-host-monitor`, `ci-ios-testflight-ship-mac-runner`,
`usage-monitor-garage-backup`.

Healthy and kept: `scheduler-tick`, `usage-monitor-scheduler`, deploy-freshness
and the other OK CI crons.

### Inbound filter

Congress.Trade project option `filters:error_messages` now includes `XRef` /
`Invalid XRef` / `PDF` so CONGRESS-TRADE-1C class noise is dropped at ingest.

### Seer

Org setup already acknowledged with Autofix + Scanner quota.  Slack workflow
now fires on Seer RCA complete and PR ready.  Do not mint extra Seer *user*
seats for bot GitHub accounts.

## What this unit does not land

SDK leftovers stay other lanes (spawned 2026-09-01):

- UM `sentry-ci-report.yml` (ST/CT already have it).
- CT iOS Cocoa.
- Hardcoded DSN hygiene on BotFleet / ContactLogo / Autorotate / DealDex plist.
- `sentry-cli releases deploys new` after Coolify/Vercel.
- dSYM / Size Analysis on `~/apps/ios-fleet/ship-testflight.sh`.
- Android SDK (deferred until those tracks ship).

## Verification

- PagerDuty REST: create incident 201 `#86`, PUT resolved 200.
- Sentry workflow GET 3930764 `detectorIds` matches the ST/UM/fleet/BF + uptime
  list.
- `find_uptime_monitors` lists the new URLs at `uptimeStatus=ok`.
- Dashboard POST 201 id 9917821.
- Metric detectors GET `workflowIds: [3930668]`.
- Cron PUT status `disabled` 200 for the three CI monitors.

## Follow-ups

1. Confirm the test page actually rang the phone (owner).  Incident is already
   resolved.
2. Point Autorotate uptime at `autorotate.codes` once DNS exists; disable the
   vercel.app probe then.
3. Land remaining SDK lanes above.  Do not fight DIRTY peer sentry PRs.
4. Workflow engine `projectIds` is a 400.  Always `detector_ids`.
