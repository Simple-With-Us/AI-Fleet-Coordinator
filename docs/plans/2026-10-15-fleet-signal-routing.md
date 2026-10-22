# Fleet signal routing: pairing every service with what it should talk to

Plan.  Claude.  Thu, Oct 15, 2026.  Linear AFC-327.  Related: OPS-454 (Sentry ownership and wiring), OPS-637 (Sentry agent-telemetry alerts), OPS-804 (Zulip cutover), ST-4531 (ST Slack callers), UM-1327 (UM Slack probe), CT-1708 (CT uptime workflow duplicate).

Owner ask: pair BetterStack, Sentry, and every other data or third-party service with whatever it should be integrated with, now that Slack is gone, including Zulip and AI agents.

This plan is built on three read-only passes run today: a repo inventory across the 19 repos on `origin/main`, a live API audit of Sentry, BetterStack, PagerDuty, Zulip, Linear and Datadog, and doc research on each pairing's native integration.  Nothing live was changed.

## What is true today

### Sentry (org slug `simple-with-us` on `us.sentry.io`)

The older rollout docs say `jays-services`.  That slug returns 404 now.  Use `simple-with-us`.

- 13 projects.  Integrations: PagerDuty, Datadog (us5), GitHub (two), Vercel, Cursor, Copilot; app installs Linear, linear-sentry-agent, botfleet-incident-monitor.  No Slack integration is installed.
- 26 workflows.  **Four still target Slack**, which has no integration, so they deliver nowhere:
  - `3930668` — org-wide production high-priority issues, Seer RCA and PR-ready notices, metric monitors `9700541`–`9700546` → Slack `#agent-sync`.
  - `3932162` — `scheduler-tick` and `usage-monitor-scheduler` cron failures → Slack `#agent-sync`.
  - `3645269` → Slack `#agentic-trading-app`.
  - `3645774` → Slack `#congress-trade`.
- `3930764` routes ST, UM, fleet-infra and BF issues plus four uptime monitors to PagerDuty.  This one works.
- 9 uptime monitors.  Five alert nobody: `dealdex.online`, `contactlogo.com`, `autorotate.vercel.app`, `host.jays.services/api/health`, Fleet Health Aggregator.
- 70 cron monitors, 31 in error, 25 active incidents.  68 have no workflow at all.  Nine active CI crons have not checked in for more than seven days, and the `*-effort-issues-sync` crons mirror effort logs that are archived.
- Four agent-telemetry workflows (Sub-Agent Overrun, API/tool-failure spike, MCP connection failures, Invoke Agent error rate) email the owner and are firing.

### BetterStack

- Uptime: 10 monitors (free-tier cap reached), 4 heartbeats (`id-renewal-watch`, `usage-monitor-env-sync`, `video-conversion-stall-watch`, `morning-pr-merge-list`).
- Notifications: email only, plus a Slack integration aimed at retired `#agent-sync`.  No escalation policy, no phone or SMS.
- Status page `status.simplewithus.com` reports `aggregate_state: downtime` while every monitored resource is operational.  The retired Fleet Board resource (`not_monitored`, downtime 10-13 to 10-15) is the likely cause.  The Simple-With-Us homepage reads that aggregate, so the public site probably says there is a disruption.
- Telemetry: only the sample "Onboarding" source.  Nothing ships logs.
- Overlap: congress.trade, socratictrade.com, contactlogo.com and usage.jays.services are probed by both BetterStack and Sentry Uptime.
- `BETTERSTACK_API_KEY` exists in the handoff file and no code uses it.

### PagerDuty

- One service, `jays.services`, default escalation to Jay.  Inbound: Cloudflare, Sentry (two), Slack Workflow Steps, Events API v2, Datadog.  Outbound: one webhook (BotFleet Fixer IR) to `agents.jays.services/hooks`.  No extensions, so nothing posts to Zulip.
- **59 open incidents** (39 triggered, 20 acknowledged), mostly `Cron failure: botfleet-*` and App Hang.  A real page would be lost in this.

### Datadog

- Free plan, keys valid (the MCP failure is the plugin's `not-setup` host, not the keys).
- 10 monitors, all default templates, notifying `@slack-agent-sync` or placeholder handles.  Two are in Alert (disk, load).

### Zulip

- Streams: agent-sync, alerts, builds, general, ideas, incident-response, issues, sandbox, trading.
- Incoming-webhook bots exist for Sentry, PagerDuty and Linear, but nothing is wired to them: no Sentry workflow targets the Sentry bot, PagerDuty has no extension, and Linear has no Zulip webhook.
- The only confirmed producer for `#alerts` is `heldalert.py`.  `#builds` and `#incident-response` have no producer.
- Integration bots never wake a seat (`docs/protocols/zulip-fleet-guide.md`).  `#alerts` reaches the owner, not agents.

### Other services

- **Pushover and Twilio** page the owner directly from host scripts, CT, ST and UM.  With PagerDuty and email that is four paging channels.
- **Code still posting to Slack:** `workers/pr-conflict-watch` (AFC), UM `ALERT_SLACK_WEBHOOK_URL`, Mac `cloud-runner-director --notify` and `provider-knob-sync`, CT/UM/CTS `scripts/codex-coordination.sh`, Autorotate `_ref/slack-sync.sh`.
- **`fleet-sentry-monitor` is not running** (no pm2 job, no process-list row) and its endpoint list is stale.  `botfleet-mcp` is in a pm2 crash loop (1,744 restarts) that nothing reports.
- **No alert wiring found** for Cloudflare, Vercel, Backblaze, Infisical, Coolify, Turso, Stripe, Resend, PostHog.
- **AI-agent path:** the only automated signal-to-agent route is BotFleet's webhook ingress.  Its hostname routing is unverified: `agents.jays.services` is documented as seat-mcp on `:8793`, while `botfleet-wake.jays.services` forwards to the ingress on `:8800`.

## Target shape

One owner per signal class, one place each class lands.

| Signal | Owner | Lands in | Pages? |
|---|---|---|---|
| Application errors, Seer RCA | Sentry | Zulip `#alerts`, topic = service; trading apps in `#trading` | Only prod high-priority on ST, CT, UM, BF |
| Black-box uptime and keyword health | BetterStack for the 10 public URLs; Sentry Uptime for the rest | Zulip `#alerts`; public status page | Yes, prod only |
| Heartbeats for Mac and host jobs | BetterStack heartbeats (6 free slots left) | Zulip `#alerts` | No |
| Cron and CI check-ins from code | Sentry Crons | Zulip `#alerts`, low priority | No |
| Host metrics | Datadog | Zulip `#alerts` | Disk and memory only |
| Paging | PagerDuty, single pager | PagerDuty → Zulip `#incident-response` | It is the pager |
| Deploys and builds | GitHub, Vercel, Coolify | Zulip `#builds` | No |
| Edge, DNS, Access | Cloudflare notifications | Zulip `#alerts` | No |
| Tracked work | Linear | Zulip `#issues` | No |
| Agent telemetry | Sentry agent-telemetry workflows | Zulip `#agent-sync` | No |

Rules the shape depends on:

- **Page only for production user-facing outages and the trading path.**  Crons, CI, App Hang and agent telemetry never page.
- **Sentry → Linear auto-issues only for new high-priority production issues**, using the installed Linear integration.  Never for crons or CI, or Linear floods.
- **BetterStack → Linear** (native, one-way) for confirmed incidents only.
- **Zulip URLs** use `?stream=<name>&topic=<service>` so every service has its own topic.  Without `stream`, posts go to the bot owner's DMs.

## Pairings, with the native method

| Pairing | Method | Notes |
|---|---|---|
| Sentry → Zulip | Zulip Sentry integration via Sentry Internal Integrations, each enabled as an alert-rule action | The webhook URL fixes the stream, so create one Internal Integration per destination: `#alerts`, `#trading`, `#agent-sync`.  The Sentry bot already exists.  Whether Seer RCA payloads render is unconfirmed; test one. |
| BetterStack → Zulip | BetterStack outgoing webhook with a `{"text": …}` template to Zulip `slack_incoming` | Untested.  Replace the dead Slack integration with it. |
| BetterStack ↔ Sentry | No native incident pairing either way | Split ownership instead of bridging (table above).  Optional: BetterStack incoming webhook can open incidents from a Sentry Internal Integration. |
| BetterStack → PagerDuty | Outgoing webhook to PagerDuty Events API v2 | Only if BetterStack keeps prod URLs and PagerDuty stays the pager.  The reverse (PagerDuty → BetterStack) is native but not needed. |
| PagerDuty → Zulip | Zulip PagerDuty integration via a Generic Webhooks v3 subscription | Bot already exists.  Target `#incident-response`. |
| Datadog → Zulip | Datadog webhook integration to Zulip (or `slack_incoming`) | Replace `@slack-agent-sync` in the provisioning scripts in Fleet-OPS. |
| GitHub → Zulip | Zulip GitHub and GitHub Actions integrations | `deployment_status`, `check_run`, failed workflow runs → `#builds`.  Filter with `only_events` to avoid noise. |
| Linear → Zulip | Zulip Linear integration | Bot already exists.  Target `#issues`. |
| Vercel → Zulip | No native integration; GitHub `deployment_status` covers it | Account webhooks need Pro; prefer the GitHub path. |
| Coolify → Zulip | Coolify's Slack-compatible notification channel → `slack_incoming` | Untested. |
| Cloudflare → Zulip | Cloudflare notification webhook destination → `slack_incoming` | Sends `{"text": …}`; should work on any plan.  Untested. |
| Sentry ↔ Linear | Installed native integration | Completing the Linear issue resolves the Sentry issue. |
| Alerts → AI agents | Sentry MCP, BetterStack MCP (`mcp.betterstack.com`), PagerDuty MCP; BotFleet ingress for push | BetterStack MCP has no read-only mode; use its `X-MCP-Tools-Only` allowlist.  Seer coding-agent handoff must respect the rule that no Codex fix is requested without the owner. |

## Phases

### Phase 0 — restore lost alerts (live config, owner OK needed)

1. Create three Sentry Internal Integrations for Zulip, one per stream (`#alerts`, `#trading`, `#agent-sync`), because each webhook URL is bound to one stream.  Re-point `3930668` and `3932162` to the `#alerts` one and `3645269` and `3645774` to the `#trading` one.
2. Attach the five unwatched Sentry uptime monitors to a workflow that posts to `#alerts`.  This includes `contactlogo.com`, which BetterStack also probes; it stays watched until Phase 2 decides whether its Sentry monitor is removed.
3. Add one low-priority workflow for the 68 unwatched crons → `#alerts`, no paging.
4. Route the four agent-telemetry workflows to the `#agent-sync` integration in addition to email.
5. Replace BetterStack's Slack integration with the Zulip webhook.
6. Remove the retired Fleet Board resource from `status.simplewithus.com` and delete or archive its monitor, then confirm the aggregate returns to operational.
7. Wire PagerDuty → Zulip `#incident-response` and Linear → Zulip `#issues` using the existing bots.

### Phase 1 — cut the noise

1. Triage the 59 open PagerDuty incidents: resolve the BotFleet cron and App Hang ones after checking each underlying monitor, and move those signals off the pager.
2. Narrow `3930764` so crons and App Hang never page.
3. Disable or delete the `*-effort-issues-sync` crons and workflows (the effort logs are archived) and the nine stale CI crons, after confirming each workflow is gone.
4. Replace Datadog's template monitors with disk, memory and load monitors for the Hetzner and Mac hosts that post to Zulip, and drop the placeholder handles.

### Phase 2 — pair and deduplicate (owner decisions first)

1. Split uptime ownership.  BetterStack keeps the 10 public URLs and adds keyword checks on ST `/api/health` (`"schedulerStale":false`, trading liveness).  Those signals died with UptimeRobot.  Confirm the free plan supports keyword monitors first.  Remove the four duplicate Sentry uptime monitors, or keep them if BetterStack is not upgraded.
2. Use BetterStack's six free heartbeat slots for the Mac and host jobs that matter most, replacing the dead `fleet-sentry-monitor` instead of reviving it.
3. GitHub, Vercel (through GitHub), Coolify and Cloudflare → `#builds` and `#alerts`.
4. Sentry → Linear auto-issue for new high-priority production issues only.
5. Decide BetterStack Telemetry: either ship Hetzner container logs to it or delete the sample source.  Datadog already covers ST, CT and DD logs, so the default is to delete it.

### Phase 3 — let agents act on alerts

1. Verify where `agents.jays.services/hooks` and `botfleet-wake.jays.services` actually land before building on either.
2. Pick one triage consumer (BotFleet Fixer or Monitor) that receives `#alerts` and `#incident-response` events, deduplicates, links or files the Linear issue, and posts its finding in the alert's topic.
3. Give seats read access to BetterStack and PagerDuty via MCP with tool allowlists, next to the existing Sentry MCP.
4. Evaluate the Seer coding-agent handoff for high-fixability issues only, with owner approval for Codex.

### Code clean-up (parallel, any seat)

- Move `workers/pr-conflict-watch` from Slack to Zulip.
- Remove UM `ALERT_SLACK_WEBHOOK_URL` delivery and the Slack and UptimeRobot platform probes (UM-1327).
- Move `cloud-runner-director --notify` and `provider-knob-sync` to `simple-chat post`.
- Delete the stale `codex-coordination.sh` and `slack-sync.sh` copies (ST is ST-4531).
- Fix the comments that still claim UptimeRobot coverage in ST, UM and CT.
- Investigate the `botfleet-mcp` pm2 crash loop.

## Decisions for the owner

1. **Single pager.**  Recommended: PagerDuty (free tier) as the only pager, with Pushover and Twilio kept for host scripts that run when everything else is down.  Alternative: BetterStack on-call at $29/month, which would let PagerDuty go.
2. **Uptime split.**  Recommended: BetterStack owns the 10 public URLs plus keyword checks; Sentry Uptime keeps everything else.  Alternative: upgrade BetterStack and move everything there.
3. **Status page.**  Is `status.simplewithus.com` meant to be public?  If yes, which apps belong on it (BotFleet, Autorotate and FleetLink are missing).

## Verification

Each phase ends with one synthetic test per new route (a Sentry test alert, a BetterStack test notification, a PagerDuty test incident that is then resolved), confirmed by reading the Zulip topic it should land in.
