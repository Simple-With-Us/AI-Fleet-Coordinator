# Cloudflare Workers Design

The design of every Worker on the Usage.Jays.Services account, and the guards that stop a code bug from running up a bill.  Refs AFC-617.

A past 2-3 hour glitch on another account cost $1,153 in D1 rows written.  The account has three goals: a bug cannot run up a bill on any capped metric, MCP can be used far more and alerts a bit more inside the Workers Free limits, and the design and the deploys come from git.

Hostnames other than the two public entry points below, Access policies, secret names and resource ids live in Fleet-OPS, not here.

## The Guards

| Layer | Where | What it stops | What it cannot stop |
| --- | --- | --- | --- |
| `limits.cpu_ms`, `limits.subrequests` | Each wrangler config | One invocation looping, including its Durable Object classes and alarms | Many invocations, an alarm chain (each alarm is a new invocation), rows written by one statement |
| `preview_urls = false` | Each wrangler config | An old version staying callable after a fix or a stop | Nothing further |
| Explicit `head_sampling_rate` | Each wrangler config | Log volume nobody chose | A log allowance reached by volume alone |
| Registry and CI check | `scripts/worker-registry.json`, `scripts/check-worker-guards.py` | A config that drops a guard, raises a ceiling, or names an unknown Worker | A by-hand deploy of a different tree, and any drift in the live settings |
| In-code budget guard | `budget-guard.mjs` beside each Worker's source, where its own change adds it | A loop inside one invocation or one isolate spending D1, KV, R2 or DO rows | A fan-out across colos |
| Account watchdog | Runs on the server, added by its own change | Account-wide daily burn, and it can alert while the account is over a cap | A runaway in the minutes before it polls |
| The Free plan | Account setting | Billing on every metric except R2, because over-limit calls fail | R2, which bills past its free allowance on any plan |

Limits are per invocation, and Cloudflare documents some per-isolate flexibility, so `cpu_ms` is a strong guard and not an exact one.  Subrequests count `fetch()` and calls to Cloudflare services such as KV, D1 and R2.  Without a limit a Paid Worker gets 10,000 subrequests and 30 seconds of CPU per invocation.

A limit in a config takes effect only when that config is deployed.  Merging it changes nothing live.  The live limits came from direct API uploads made on purpose to apply these caps, at about 7:42 PM on Nov 4, 2026, with `alert-ingress` raised to 200 ms and 50 subrequests at about 9:03 PM.  The current `alert-ingress` and `pr-conflict-watch` versions from those uploads have no `workers/tag`.  A deploy replaces the live values with its config's, and a config with no limits removes them: a `simple-tools-mcp` upload at about 8:25 PM the same evening carried no limits, which dropped its 300 ms and 200 subrequests.  So deploy only from a tree that has these limits, and read the live settings back afterwards.

## The CI Check

`python3 scripts/check-worker-guards.py` runs in the `test` job of `.github/workflows/ci.yml`.  It reads the files `git ls-files` lists and never talks to Cloudflare.  A tracked `wrangler.toml`, `wrangler.json` or `wrangler.jsonc`, or a named variant such as `wrangler.prod.toml`, fails when:

- `limits.cpu_ms` or `limits.subrequests` is missing, or a value is above its registry row
- `preview_urls` is not `false`
- `observability` has no `head_sampling_rate` at its top level or under `logs`, or the rate is above its registry row
- its `name` has no registry row, or its path is not listed under that name
- an `[env.*]` block breaks any of these with what it inherits, under the Worker name it deploys as, which is `<name>-<env>` unless the block sets `name`

A registry row fails when its status is not `auto`, `gated`, `frozen` or `retire`, its limits, sampling, health or deploy text is missing, a null config has no `no_config_reason`, or a config it names in this repository is not tracked.  An empty file listing fails too, so the check can never pass vacuously.

This check covers this repository.  Usage-Monitor's rows record its intended values, and nothing enforces them yet.

### Registry Status

| Status | Meaning |
| --- | --- |
| `auto` | Git is meant to deploy it on merge.  The row's `deploy` text says whether that works today. |
| `gated` | A deploy needs an explicit gate: an owner ruling, a release receipt, or a canary. |
| `frozen` | Never redeploy it.  It is kept for a stated reason. |
| `retire` | Stop and delete it once the owner decides.  Never redeploy it. |

### The Downgrade Switch

`account_plan` in the registry is the switch, `paid` today.  Set to `free`, the check accepts a config with no `limits.cpu_ms`, because the Free plan's own 10 ms applies, and caps `limits.subrequests` at `free_plan_subrequests`, which is 50.  Without this switch a check that insisted on these limits could block every deploy after the downgrade, because the documented Free maximum for subrequests is 50.  What Free does with `limits.cpu_ms` present has not been tested.

`python3 scripts/check-worker-guards.py --plan free` rehearses it without changing the file.  Today that flags one config: `scripts/agent-sync-mcp/wrangler.jsonc` at 200 subrequests.

## The Workers

Measured figures are from the audit of Nov 3-4, 2026.  CPU figures are a 14-day distribution.  Each Worker's status and limits are held in the registry, repeated here for reading.

### simple-tools-mcp

- **Purpose:** the hosted ST-MCP server.  Zulip, Linear, recall, Notes, FleetLink and Git tools for every seat.
- **Request flow:** `agents.simplewithus.com` reaches the Worker.  OAuth clients and bearer seats go through the SeatGate Durable Object for identity, budgets, spacing and audit, then to the upstream service.  `/authorize` and `/admin` sit behind Access.
- **Storage:** one KV namespace for OAuth grants; one SQLite Durable Object per seat.
- **Triggers:** fetch, and the SeatGate alarm, which re-arms at 1 second while work is due.
- **Consumers:** hosted clients over OAuth, and server seats through the stdio adapter and CLIs.
- **Source:** `scripts/agent-sync-mcp`.
- **Deploy:** by hand from `DEPLOY.md`.  Status `gated`.  Merge does not deploy it, and each deploy restarts the Durable Objects.
- **Limits:** 300 ms CPU, 200 subrequests, sampling 1.  Measured CPU p99 78.9 ms, p99.9 120 ms, about 5.5 subrequests per request.  The true per-invocation maximum is not exposed by analytics.  The largest fan-out read from the code is `GET /admin`: about 131 SeatGate calls across 8 hosted seats and 44 bearer rows, plus its Infisical reads.  Whether each call or each stub counts as a subrequest is not verified.  Past the limit its reads show as unavailable or unknown instead of failing the page.

### alert-ingress

- **Purpose:** one door for vendor alerts, turned into state and standing messages in Zulip.
- **Request flow:** the stateless Worker refuses bad routes and obviously bad auth, then hands everything to one named AlertWriter Durable Object, which serializes verifying, deciding, storing and posting.
- **Storage:** SQLite Durable Object rows.  The KV namespace is a frozen legacy source with no operations since Oct 30.
- **Triggers:** fetch, and a cron every 5 minutes that promotes incidents past their wait window.
- **Consumers:** Datadog, PagerDuty, Better Stack and any vendor with a route in its `README.md` post to it on `alert-ingress.jays-services.workers.dev`, which stays on.  The Usage Monitor `/ops` card reads `GET /status`.
- **Source:** `workers/alert-ingress`, design in its `README.md`.
- **Deploy:** `alert-workers-deploy.yml` is the intended path.  Status `auto`.  It has been failing on a stale credential receipt, and any `wrangler.toml` change needs a manual reconcile by design.
- **Limits:** 200 ms CPU, 50 subrequests, sampling 1.  Peak 36 ms CPU in the Durable Object.  A sweep spends one Zulip call per promoted incident, then one or more per touched topic, so about 17 to 25 incidents promoting in separate topics in one sweep can reach 50.  Past the limit `sendEventOnce` has already recorded `unknown` and never resends, yet the sweep still stores the incident as announced, so its log line is lost for good, and that topic's standing message stays stale until its next event.  That loss falls in a mass outage.  50 is the most the Free plan allows, so the real fix is in the code: stop promoting when the sweep's budget runs low, and leave the rest pending for the next sweep.

### pr-conflict-watch

- **Purpose:** tells the owning seat in Zulip when a pull request goes into conflict.
- **Request flow:** a GitHub webhook is verified, `pull_request`, `check_suite` and `check_run` events are read for pull request numbers, and up to four lookups run in `waitUntil`: a GitHub read, a KV read, and a Zulip post and KV write on a transition.
- **Storage:** one KV namespace with 14-day TTLs.
- **Triggers:** fetch only, on workers.dev.
- **Consumers:** Zulip topics in the alerts channel.  It never writes to GitHub.
- **Source:** `workers/pr-conflict-watch/zulip-cutover` is live.  The parent directory is a never-deployed variant, and `scripts/worker-targets.json` still names it.
- **Deploy:** status `gated`.  `alert-workers-deploy.yml` skips it until a verified-delivery receipt exists, so the last deploy was by hand.
- **State:** every GitHub call has returned 401 since Oct 21, so it has posted no alert since then, yet it still takes about 11,600 requests a day.  Retiring it or repairing it is an open owner decision.
- **Limits:** 50 ms CPU, 20 subrequests, sampling 0.1, with observability on.  CPU p99 about 10 ms.  One pull request costs at most 6 subrequests, so a delivery naming more than 3 slow pull requests can run out.  Each failure is caught and logged per pull request, and the next delivery looks again.

### expenses-jays-services

- **Purpose:** a read cache of the Usage Monitor owner-expense ledger, with a small dashboard.
- **Request flow:** behind Access.  HTML and JSON reads from D1, and a bearer-gated manual sync.
- **Storage:** one D1 database.
- **Triggers:** fetch, and a cron every 15 minutes that re-reads the ledger.
- **Consumers:** the owner's browser.
- **Source and deploy:** Usage-Monitor `workers/expenses-site`, `worker-deploy.yml` in that repository, with its gate variable off, so deploys are by hand.  Status `auto`.
- **Limits:** 100 ms CPU, 200 subrequests, sampling 1.  CPU p99 29 ms.  Each sync rewrites every row today, 73,824 rows written a day, and the fix belongs in that repository.

### usage-monitor-receipt-inbox

- **Purpose:** receives receipt email, classifies it and keeps the evidence.
- **Request flow:** Email Routing hands mail to the Worker, which admits it through its Durable Object (100 messages and 100 MiB a day), parses and classifies it, stores the raw message in R2, and commits the index.  A small bearer-gated HTTP API serves Usage Monitor.
- **Storage:** one R2 bucket with a 180-day lifecycle rule; one SQLite Durable Object.
- **Triggers:** email, fetch and a Durable Object alarm.
- **Consumers:** the Usage Monitor app.
- **Source and deploy:** Usage-Monitor `workers/receipt-inbox`, `worker-deploy.yml` with its gate off.  Status `auto`.
- **Limits:** 200 ms CPU, 30 subrequests, sampling 1.  CPU p99 41 ms.

### usage-monitor-receipt-lifecycle-auditor

- **Purpose:** proves the R2 retention rule is still in place, so intake can fail closed when it is not.
- **Request flow:** no route.  The inbox reaches it over a service binding.
- **Storage:** one SQLite Durable Object.
- **Triggers:** a cron every 12 hours, and the service binding.
- **Consumers:** the receipt inbox.
- **Source and deploy:** Usage-Monitor `workers/receipt-lifecycle-auditor`, by hand with a package script.  Status `auto`, blocked on an approved CI probe.
- **Limits:** 50 ms CPU, 20 subrequests, sampling 1.  CPU p99 about 3 ms.

### start-jays-services

- **Purpose:** a former redirect and search-suggest proxy.  Another host now serves its hostname.
- **Request flow, storage, consumers:** none reach it.  No route, no custom domain, workers.dev off.  Static assets only.
- **Triggers:** fetch only.
- **Source:** `scripts/safari-start`, whose config still claims the old hostname, so never redeploy it as it stands.
- **Deploy:** none.  Status `retire`, pending the owner's decision.
- **Limits:** 10 ms CPU, 5 subrequests, sampling 0.1.

### admin-jays-services

- **Purpose:** the old admin panel, with a Vercel successor in Fleet-OPS.
- **Request flow:** behind Access, it fans out to vendor status APIs.  No invocation since Oct 15.
- **Storage and triggers:** none besides static assets and fetch.
- **Consumers:** none measured.  The hostname is still bound until its cutover.
- **Source:** **no wrangler config exists on main in any repository.**  The source was removed from this repository on Oct 19.
- **Deploy:** cannot be redeployed from main.  Status `retire`.
- **Limits:** the registry's 50 ms CPU and 100 subrequests can only be applied through the live settings.

### fleet-share-api

- **Purpose:** the legacy FleetLink share host, replaced by `fleetlink-api` on the FleetLink account.
- **Request flow:** none.  Its zones moved away, and the last invocation was Oct 12.
- **Storage:** one legacy R2 bucket, a stale subset of the FleetLink account's copy.
- **Triggers:** none live.
- **Consumers:** none measured.
- **Source:** the private `fleetlink-legacy` repository, not read for this document.
- **Deploy:** never.  Status `frozen`, because FleetLink's `AGENTS.md` forbids deleting it, so retiring it is the owner's decision.
- **Limits:** 50 ms CPU, 20 subrequests.

## Free-Tier Budget

The day resets at 6 PM Chicago (7 PM during daylight saving).  One buggy day on the shared 100,000 requests stops every Worker on the account, alert-ingress included, until the reset.  Peaks are from Cloudflare's day of Nov 3, 2026.

### Worker Requests, Cap 100,000 A Day

| Worker | Peak | Allocation | How |
| --- | --- | --- | --- |
| simple-tools-mcp | 98,272 | 80,000 | Local handshakes in the adapter, fewer calls per tool use |
| pr-conflict-watch | 18,313 | 3,000 | Filter its webhook events.  0 if retired |
| alert-ingress | 745 | 2,500 | Includes 288 cron runs, and leaves room for more alerts |
| expenses-jays-services | 96 | 50 | Hourly cron |
| usage-monitor-receipt-inbox | 50 | 150 | |
| usage-monitor-receipt-lifecycle-auditor | 2 | 5 | |
| CI and watchdog probes | | 100 | |
| Reserve, never allocated | | 14,195 | Protects alerts |

80,000 is about 5 times the 14-day average of 15,487 requests a day for MCP, but only 0.81 of the Nov 3 peak.  10 times today's average cannot fit by trimming.  It needs at least half of MCP's HTTP traffic off the Worker.

### Other Daily Caps

| Metric | Cap | Peak | Plan |
| --- | --- | --- | --- |
| Durable Object requests | 100,000 | 315,348 (SeatGate) | SeatGate 85,000, AlertWriter 3,000, others 200, reserve 11,800 |
| Durable Object rows written | 100,000 | 174,762 (SeatGate) | Needs audit-write work.  The merged batching may barely lower it |
| Durable Object duration | 13,000 GB-s | about 3,300 (SeatGate, inferred) | Fine today.  10 times MCP would not be |
| D1 rows written | 100,000 | 73,824 steady (expenses) | Under 20,000 after the sync fix, with an expected 100-200 |
| D1 rows read | 5,000,000 | small | Fine |
| KV reads | 100,000 | 12,630 (pr-conflict-watch, before its 401s) | Its token repair would bring this back |
| KV writes | 1,000 | 490 (pr-conflict-watch, Oct 11) | Fine since its alert-on-dirty change |
| KV lists | 1,000 | 921 (MCP OAuth) | MCP 400, alert-ingress 200, reserve 400 |
| Crons | 5 | 3 | Fine |
| CPU per invocation | 10 ms | MCP p99 79 ms, expenses 29 ms, inbox 41 ms | Rehearse.  30 days show no CPU kills, and enforcement may change |
| Subrequests per invocation | 50 external | not measured | Rehearse, including `GET /admin` on simple-tools-mcp and an alert-ingress sweep with many promotions |
| Env vars per Worker | 64 | 62 (MCP) | Free up slots in order.  Secrets that look unused are the outage fallback |

### R2

R2 bills past 10 GB, 1 million Class A and 10 million Class B a month on any plan, so the Free plan is not a true spend cap while buckets are in use.  Measured use is about 5.3 GB, 52,000 Class A and 140,000 Class B over 30 days, so about $0 today.  One bucket is written from outside any Worker, so no Worker guard covers it.  Keeping R2 with a watchdog or removing it is the owner's decision.

### Logs

From Dec 1, 2026, Free gets 0.5 GB of Workers Logs a day, and ingestion then stops silently until the reset.  Paid gets 50 GB per cycle, then $0.25 per GB.  Measured use is about 31,000 events a day, with bytes per event not measured.  simple-tools-mcp stays at sampling 1, because its log lines are the only per-seat attribution.

## Downgrade Checklist

In order.  Do not skip ahead.

1. Deploy these limits at Paid values, with the watchdog and a deployer that will not undo a stop.
2. Fix the deploy pipeline, and ship the merged SeatGate work after testing its audit flush under a client disconnect in workerd.
3. Land the CPU, Durable Object, KV list, D1 and pr-conflict-watch fixes from the per-Worker work.
4. Rehearse: deploy `cpu_ms` 10 and `subrequests` 50 to 10% of each Worker, then 100%, and fix every failure.
5. Require 7 straight days with requests under 85,000, Durable Object requests under 85,000, KV lists under 700, D1 rows written under 50,000, and under 0.1% CPU exceeded.
6. Decide R2: remove it, or accept it with the watchdog.
7. On a scratch Worker, confirm what Free does with `limits.cpu_ms`, and confirm the rate-limit binding and service bindings work there.
8. Change configs for Free: remove `limits.cpu_ms` if refused, and bring `limits.subrequests` to 50 or less.  Set `account_plan` to `free` in the same pull request, and run `python3 scripts/check-worker-guards.py`.
9. The owner changes the plan in the dashboard.  The audit's API token could not read subscriptions.
10. Verify the three crons fire, each row's health proof passes, MCP tools answer, and a test alert lands.
11. Keep the watchdog, retuned for 80% early warning and R2.  Rolling back is upgrading again and setting `account_plan` back to `paid`.
