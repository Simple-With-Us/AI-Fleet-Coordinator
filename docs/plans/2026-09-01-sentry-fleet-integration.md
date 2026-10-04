# Sentry sponsored-account fleet integration

Plan.  Grok.  2026-09-01.  Board `6ac85c0e`.  Org `jays-services` (https://jays-services.sentry.io, region `us`).

This is analysis and a recommended operating model.  It is not a runtime change.  Competing in-flight Sentry PRs already exist; unstick those before starting a new expansion lane.

**Standing split landed 2026-09-01** (adoption report, not a second plan):
`docs/rollouts/2026-09-01-sentry-fleet-adoption.md` and AGENT-SYNC
Observability.  Binding: Personal-Site stays Datadog-only (no Sentry project);
CTS and fleet-ops have no project; Android SDK waits until those tracks ship;
do not run Datadog Session Replay and Sentry Session Replay on the
same page.

**Org extras landed 2026-09-01** (alerts/uptime/dashboard/metrics):
`docs/rollouts/2026-09-01-sentry-org-rollout.md`.  PagerDuty test page #86
resolved.  Workflow project filters are `detector_ids` (not `projectIds`).
Seer Slack trigger is on.  Uptime covers ST/CT/UM/DD/BF/CL/fleet plus
Autorotate's current Vercel host.

**Max-features lane 2026-09-04** (Jay+Designer): default full Sentry
surface per app.  Matrix, kill switches, and honor-list:
`docs/rollouts/2026-09-04-sentry-max-features.md`.  Do not silently skip
a product; omissions already listed to Designer stay until Designer says
otherwise.

## Verdict

Sentry is already the fleet's **application** observability system, and the sponsored account removes the old "don't spend quota" constraint.  The gap is not "add Sentry somewhere."  The gap is that we capture errors and some traces, then leave the rest of the product (alerts, Seer, release health, mobile symbols, AI traces, profiling, metrics) mostly unused, while Datadog / UptimeRobot / PagerDuty / Pushover overlap the paging and infra layers.

The highest-ROI move is not more SDKs.  It is: make the data we already send **actionable** (alerts + Seer + GitHub commits + dSYMs), then fill the real coverage holes (ContactLogo web SDK, Android, Personal-Site if it grows a runtime, Congress.Trade uptime, stale cron hygiene).

## What we have today (live, 2026-09-01)

### Org

- One org, one team (`jays-services`), owner `mail@jays.services`.
- Eight projects: `socratic-trade`, `congress-trade`, `usage-monitor`, `fleet-infra`, `dealdex`, `botfleet`, `autorotate`, `contactlogo`.
- No projects for Personal-Site, fleet-ops, or congress-trading-shared (library; errors should surface in consumers).
- Zero issue-alert rules.  Zero metric-alert rules.
- Dashboards are Sentry prebuilts only (AI Agents, Next.js, Mobile Vitals, Web Vitals, …).  No fleet-custom dashboard.
- Releases for `socratic-trade` are git SHAs from the Next.js bundler plugin.  Almost none have attached commits or deploys, so suspect-commit / "which release broke this" is half-wired.

### Product surface vs use

| Signal | Status | Notes |
|---|---|---|
| Errors | Partial | ST, CT, UM, DD, BF web.  iOS Cocoa on ST, BF, CL, AR (DD iOS recently patched).  CL web is a homemade envelope POST, not the SDK.  Autorotate has no web SDK. |
| Tracing | Partial | Next.js / React apps sample ~0.2.  CT Deno init exists.  No distributed trace across ST↔CT↔UM. |
| Logs | Partial | `enableLogs: true` on ST, UM, DD, BF.  Not a structured logging standard.  Datadog also takes logs. |
| Session Replay | Uneven | UM/DD/BF default 10% session + 100% on error, text/media masked.  ST **opt-in** (financial PII).  CT none. |
| Crons | Noisy | ST `scheduler-tick` is healthy.  UM scheduler + several CI crons + CT `watcher-cron` / `agreement-autopublish-cron` + `fleet-host-monitor` are in error or stale (last check-in July). |
| Uptime | One probe, failing | Only `https://socratictrade.com` (homepage).  Status `failed` at analysis time.  Health JSON lives at `/api/health`.  CT omitted from `fleet-sentry-monitor` because Cloudflare challenges non-browser UAs. |
| Profiling | Unused | Sponsored PAYG.  Would pay for ST gather/scan and CT cron CPU. |
| Application metrics | Almost unused | UM has `nodeRuntimeMetricsIntegration`.  No business KPIs (`queue.depth`, `ingest.lag`, `order.failed`). |
| User feedback | DealDex only | `feedbackIntegration` autoInject false. |
| AI / LLM monitoring | Unused | BotFleet is the obvious first `gen_ai.*` project.  ST OpenRouter / RAG failures today are generic errors (`SOCRATIC-TRADE-1X`, `-22`, `-27`). |
| Seer | Available, not in the loop | MCP tool `analyze_issue_with_seer` is connected.  Agents still guess from stack traces. |
| Releases / dSYM / source maps | Half | ST/UM `withSentryConfig` uploads when `SENTRY_AUTH_TOKEN` is present.  CT still has a Worker-era `sentry:sourcemaps` script.  iOS dSYM upload not a standard `ios-ship` step. |
| Snapshots (Apple) | Unused | Fits ContactLogo / ST / CT / DD UI regressions. |
| Alerts | None | Issues pile up unresolved.  Owner paging is UptimeRobot + Pushover + PagerDuty, not Sentry. |

### Per-app matrix

| App | Hosting | Web/server SDK | Mobile | CI → `fleet-infra` | Cron / uptime |
|---|---|---|---|---|---|
| ST | Coolify / Hetzner, Next.js | `@sentry/nextjs` errors+traces+logs; replay opt-in | Cocoa in `ios/SocraticTrade/SentryTelemetry.swift` | workflow + script | `scheduler-tick` OK; homepage uptime failing |
| CT | Coolify Deno-in-Docker | `@sentry/deno` (dummy retired) | no dedicated Sentry project split | workflow + script | `watcher-cron` stale since 2026-07-24; `agreement-autopublish-cron` stale since 2026-07-13 |
| UM | Coolify Next.js | `@sentry/nextjs` + replay on + runtime metrics | n/a | **missing** sentry-ci-report | `usage-monitor-scheduler` error |
| DD | Vercel + iOS | `@sentry/react` replay+feedback | Cocoa (pbxproj membership just landed) | missing | none |
| BF | web + iOS + Windows | `@sentry/react` replay | Cocoa | missing | none |
| CL | web + iOS + Android | homemade envelope (errors only) | Cocoa; **no Android SDK** | missing | none |
| AR | Apple-first | no web SDK | Cocoa | missing | none |
| PS | Vercel | Datadog only; Sentry explicitly unchanged | n/a | missing | none |
| CTS | library | n/a | n/a | missing (silent no-op historically) | n/a |
| AFC / Mac | pm2 | `fleet-sentry-monitor` → `fleet-infra` | n/a | missing on AFC repo | `fleet-host-monitor` last check-in 2026-07-13 (env `fleet`, not `production`) |

### Hottest live issues (freq, 14d)

These are the "is Sentry earning its keep?" test.  Most are cron/uptime noise or expected provider flakes, not new product bugs.

1. `CONGRESS-TRADE-1C` PDF XRef noise (supposed to be dropped; still 2864 events).
2. `FLEET-INFRA-C1` cron `ci-deploy-freshness` (356 events, still firing).
3. `SOCRATIC-TRADE-27` RAG embedding integrity rejection.
4. `FLEET-INFRA-C2` cron `ci-ios-testflight-ship`.
5. `SOCRATIC-TRADE-S` downtime for the **homepage** probe (likely CF/challenge or wrong URL).
6. `USAGE-MONITOR-1` cron `usage-monitor-scheduler`.
7. `SOCRATIC-TRADE-28` alpaca-account-insights connection failed.

Until alerts exist, agents only see these if they open Sentry or THE BOARD already has a copy.

### In-flight work (do not fork)

- ST PR #3141 `ag/sentry-observability-expansion` — DIRTY.
- ST PR #3146 `ag/sentry-logs-and-config-tuning` — DIRTY.
- CT PR #2282 `ag/sentry-cleanup-and-trace-defaults` — DIRTY.
- iOS Cocoa expansion on DD / BF / CL / AR (AG + Grok, some merged).
- Board `45418ca1` still says CT production Sentry is a dummy.  Code on main now binds `@sentry/deno`; the board row is stale.

Unstick those three PRs (or close as superseded) before opening another "add more Sentry" branch.

## Role split (binding recommendation)

Sentry is not a Datadog replacement and not an UptimeRobot replacement.

| System | Owns | Does not own |
|---|---|---|
| **Sentry** | App exceptions, native crashes, request traces, error-session replay, scheduled-job check-ins, release health, Seer RCA, mobile symbols, AI-agent traces | Host CPU/disk, Coolify container stats, Cloudflare WAF, owner phone paging |
| **Datadog** | Host / Docker / Coolify infra, fleet APM already paid, long-window ops dashboards | Product crash grouping, iOS dSYM, Seer |
| **UptimeRobot** | Owner-facing black-box uptime (phone) | App stack traces |
| **PagerDuty** | Human page for true P0 (prod down, live-trading broken) | Issue tracker |
| **Pushover** | Owner one-shot alarms already wired in CT liveness | Recurring issue noise |
| **Coolify / Vercel** | Deploy.  Must **emit** a Sentry release+deploy on success | Observability UI |
| **Cloudflare** | Edge / bot fight.  Must not look like an app outage | App errors |

Do **not** dual-ship high-volume logs to Sentry and Datadog.  Pick: Sentry logs for request-correlated debug (sampled, structured, trace-linked); Datadog for host and platform.  Replay stays Sentry-only (Datadog Replay is off by fleet policy).

Do **not** add a second Mac host monitor.  Extend `fleet-sentry-monitor`.

## What "better integration" means, by layer

### 1. Development (every seat)

- Sentry MCP stays connected on Mac Grok / Claude / Cursor.  Treat `search_issues` + `get_sentry_resource` as the first move on a production bug, before grepping.
- Default RCA path: `sentry-debug-issue` then Seer (`analyze_issue_with_seer`) when the stack is not enough.
- GitHub integration (owner OAuth click) so Seer can read the repo and suspect commits appear.
- `SENTRY_AUTH_TOKEN` in CI (already named in `~/.secrets`; Infisical per app) so source maps and dSYMs actually upload.  A release without artifacts is a pretty SHA with minified frames.
- iOS: dSYM upload in `ios-ship`.  Android (ContactLogo): official SDK, not a second homemade envelope.
- Feature work that adds a scheduled job **must** add a cron monitor slug.  Feature work that adds an LLM call **should** emit `gen_ai.*` spans (BotFleet first, then ST RAG / CT extract).

### 2. CI

- `sentry-ci-report.yml` is fleet-standard and currently only live on ST + CT.  Copy from `AI-Fleet-Coordinator/github-workflows-template/workflows/sentry-ci-report.yml` onto UM, DD, BF, CL, AR, AFC.  Personal-Site / CTS / fleet-ops do not get a Sentry *app* project; a `fleet-infra` CI reporter is optional and still fingerprints `[app, workflow]` only.
- Fingerprint stays `[ci-failure, app, workflow]` — never the branch or SHA (that minted ~85 zombie `fleet-infra` issues once).
- Pause or delete cron monitors whose workflows were renamed or whose schedule no longer matches (`ci-deploy-freshness`, `ci-ios-testflight-ship`, weekly security jobs that check in error, `ci-ios-testflight-ship-mac-runner` with empty environments).
- `getsentry/action-release` or the bundler plugin — **one** of them, not both — with `fetch-depth: 0` so commits attach.

### 3. Hosting / deploy

- Coolify (ST, CT, UM) and Vercel (DD, PS, BF web): on successful deploy, `sentry-cli releases deploys new` for environment `production` with the same version string the SDK tags (`SOURCE_COMMIT` / `CT_BUILD_SHA` / Vercel `VERCEL_GIT_COMMIT_SHA`).
- Infisical is still the DSN source of truth.  Client DSNs (`NEXT_PUBLIC_*` / `VITE_*`) are public-by-design; still do not hardcode them in Swift (ST iOS currently falls back to a baked ingest URL).
- Ad-blockers eat browser events.  ST/UM should turn on Sentry `tunnelRoute` once middleware is confirmed not to collide.
- Congress.Trade `/api/health` needs a probe that survives Cloudflare (browser UA, or a bypass for `fleet-sentry-monitor` / Sentry uptime).  Until then CT outages stay invisible to Sentry uptime.

### 4. Operating

- **Alerts (the actual hole).**  Create org workflows:
  - New or regressing issue, priority ≥ high, environment `production` → Slack `#agent-sync` (or a new `#sentry`).
  - Same + `app:socratic-trade` trading-path tags → PagerDuty critical.
  - Cron missed/failed for `scheduler-tick`, `usage-monitor-scheduler`, `fleet-host-monitor` → Slack.
  - Do not page on CI failures; those stay Sentry issues for agents.
- Hygiene: ignore-until-escalating on known provider flakes (Alpaca insights, OpenRouter blips) after they have a board row.  Resolve or ignore `CONGRESS-TRADE-1C` if the drop filter is live.
- Custom dashboard: "Fleet production" with error rate, crash-free, cron status, ST/CT/UM/DD/BF as widgets.  Prebuilts are unused.
- THE BOARD: optional later ingest of Sentry high-priority issues as `agent-report` rows so seats that do not open Sentry still see them.  Not phase 0.
- `fleet-host-monitor` environment is `fleet` while everything else is `production`, and its last check-in is July.  Fix the env + the check-in path, or the "dead monitor alerts by absence" design is itself dead.

### 5. Mobile / desktop

- Cocoa is in several `project.yml` files.  Next: dSYM, App Hang, crash-free sessions, **not** screenshots on ST/CT (financial / PII).  Screenshots may be OK on ContactLogo (logos, not money).
- ContactLogo Android has no Sentry SDK.  That is the largest mobile hole.
- BotFleet Windows: add the .NET / Electron SDK on the same `botfleet` project when the desktop build is the user-facing surface.
- Sentry Snapshots: worth it for ContactLogo (visual product) and ST/CT iOS chrome; skip until dSYMs work.

### 6. Privacy (do not weaken)

- ST: replay stays opt-in, `sendDefaultPii: false`, `redactForTelemetry`, no screenshots / view hierarchy.
- CT: same bar (filings, emails, Apple tokens).
- UM: usage numbers are the product; mask tokens, not counts.
- DD/BF/CL: keep `maskAllText` / `blockAllMedia` on replay.
- Never put `SENTRY_AUTH_TOKEN` or a secret DSN in chat.  Client ingest DSNs in JS/Swift are expected.

## Phased plan

### Phase 0 — Hygiene (days, no new SDKs)

Owner clicks (cannot be done by an agent):

1. Confirm sponsored plan includes Seer and raise error/span/replay quotas so we stop sampling like a free tier.
2. GitHub integration OAuth for `Simple-With-Us/*` (suspect commits + Seer code).
3. Slack + PagerDuty integrations in the Sentry org.

Agent work:

1. Unstick or close ST #3141, #3146 and CT #2282.
2. Disable or retarget stale cron monitors (`watcher-cron`, `agreement-autopublish-cron`, `fleet-host-monitor` env, failed CI crons).
3. Point the ST uptime monitor at `/api/health` (JSON `ok: true`), not the marketing homepage.
4. Create the three alert workflows above.
5. Ignore/resolve confirmed noise (`CONGRESS-TRADE-1C` if the filter shipped).
6. Mark board `45418ca1` stale if Deno Sentry is live in production (verify with a deliberate `captureMessage` from CT, not by reading the import map).

### Phase 1 — Close coverage holes

1. Replace ContactLogo web homemade envelope with `@sentry/browser` (or `@sentry/react`) on project `contactlogo`.
2. ContactLogo Android official SDK.
3. `sentry-ci-report` on UM, DD, BF, CL, AR, AFC.  Not a reason to mint a Personal-Site Sentry project.
4. Coolify/Vercel deploy → `sentry-cli deploys`.
5. iOS dSYM upload in the existing `ios-ship` path (ST, CT, DD, BF, CL, AR).  Size Analysis: upload the XCArchive from `~/apps/ios-fleet/ship-testflight.sh` via `sentry-cli` (TODO; 100 builds/month included; no new LaunchAgent).
6. Congress.Trade health probe that Cloudflare does not challenge.
7. Personal-Site stays Datadog-only.  No Sentry project, including a tiny unhandled-window-error project.

### Phase 2 — Use the sponsored product

1. Seer in the agent debug loop (skill, not a new daemon).
2. BotFleet `gen_ai.*` spans (model, tokens, conversation id).  Then ST RAG / CT extract.
3. Profiling at a low sample on ST server (gather/scan) and CT Deno cron.
4. A handful of application metrics: ST order/place failures, CT ingest lag, UM fetch-all duration, Coolify disk is **Datadog/Housekeeper**, not Sentry.
5. Error-only replay on CT admin (masked).  Leave ST replay opt-in.
6. One custom "Fleet production" dashboard.
7. User-feedback widget on ST/CT support / DD already has the integration.

### Phase 3 — Snapshots and extras (optional)

1. Sentry Snapshots for ContactLogo and ST/CT iOS.
2. `tunnelRoute` on Next.js apps.
3. Optional THE BOARD ingest of Sentry high-priority issues.
4. Size analysis / build distribution only if TestFlight flow is painful.

## What not to do

- Do not stand up a second host monitor, a second CI reporter shape, or a per-agent Sentry project.
- Do not send 100% traces.  0.2 (or 1.0 on error transactions) is enough even on a sponsored plan; volume still burns Seer context and human attention.
- Do not enable unmasked Session Replay on ST or CT.
- Do not replace UptimeRobot owner paging until Sentry → PagerDuty has been tested with a real page.
- Do not create Sentry projects per environment; use the `environment` tag (`production` / `preview` / `development`).
- Do not treat Coolify "running" or GitHub Actions green as proof the app is healthy.  That is why `fleet-sentry-monitor` exists.
- Do not hardcode DSNs in source as a "fallback."

## Suggested first implementation lane (after this plan lands)

One PR, one app, prove the loop:

`usage-monitor` — scheduler cron is already failing (`USAGE-MONITOR-1`).  Fix the check-in, add sentry-ci-report, add one high-priority issue alert to Slack, confirm Seer can see a real issue.  Then copy the pattern.

ST live-trading path is the wrong first lane (P0 product risk, dirty sibling PRs).

## Evidence

- Sentry MCP: org `jays-services`, 8 projects, 0 alert rules, monitors listed above, unresolved issues listed above, ST releases without deploys.
- Code: ST/UM `withSentryConfig`, ST/UM/DD/BF init files, CT `app/src/deno/sentry.ts`, CL homemade `web/src/observability/sentry.ts`, `fleet-sentry-monitor/monitor.py` `PROD_HEALTH_ENDPOINTS`, AGENT-SYNC.md Observability section.
- CI: `sentry-ci-report.yml` present on ST+CT only.
- Board: 45 Sentry-matching items; three DIRTY sentry PRs; stale CT dummy finding.
