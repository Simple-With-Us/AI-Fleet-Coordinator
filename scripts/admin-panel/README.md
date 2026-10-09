# Fleet admin panel

One page at **https://admin.jays.services** that shows the state of every fleet app,
platform, and service, and lets you search the fleet RAG corpus.  It is a single
Cloudflare Worker with no build step, no npm dependencies, and no TypeScript.

The Worker holds every fleet API token, so the browser never sees a credential and
never talks to a fleet API directly.  That also means **the hostname must stay behind
Cloudflare Access** — the Worker proxies authenticated fleet APIs, so anyone who can
open the page can read The Board, the RAG corpus, Coolify, Sentry, and PagerDuty.
Access is the only thing standing in front of it.

## What each card shows

| Card | Source | What a row means |
|---|---|---|
| **Public Endpoints** | Direct HTTPS probe of 17 fleet surfaces, 8 s timeout, run in parallel | HTTP status and latency.  2xx and 3xx are up; a redirect to `cloudflareaccess.com` reads "Up, behind Access"; 401 and 403 read "Up, auth required"; a timeout is amber and reads "Timed out from Cloudflare". |
| **Fleet Recall** | `GET /health`, `GET /recall/stats`, and a canary `POST /recall/search` for `fleet mode` on `recall.jays.services` | Point count, collection name and status, embedder and reranker health, per-source breakdown, and the canary hits.  The search box at the top of the card queries the live corpus. |
| **The Board** | `GET /findings/stats` and `GET /findings?status=open,in_progress` on `mac.jays.services` | Open and in-progress counts by severity, plus the P0 and P1 rows that need attention. |
| **Coolify Applications** | `GET /api/v1/applications` and `/api/v1/servers` on `host.jays.services` | Container status (`running:healthy`, `exited:unhealthy`, …) per application, then server reachability. |
| **GitHub Repositories** | One Search API call for every open PR the owner has, then the Actions API for the latest run on `main`, per repo in `fleet-apps.json` | Open PR count and the conclusion of the most recent `main` run.  The row links to that run.  Past 100 open PRs the total stays exact but the per-repo split is capped, and the card says so. |
| **Vercel Projects** | `GET /v9/projects?limit=50`, retried per team when the personal scope is empty | Newest READY deployment when present (else `latestDeployments[0].readyState`) — READY is green, ERROR is red, everything else amber.  A **CANCELED** deployment is amber, not red: it is an abandoned or superseded build, and the production alias keeps serving the last READY one.  See "A canceled deploy is not a down service". |
| **Sentry Issues** | `GET /organizations/simple-with-us/issues/?query=is:unresolved&statsPeriod=24h` | Unresolved issue count grouped by project slug, with the newest title. |
| **PagerDuty Incidents** | `GET /incidents?statuses[]=triggered&statuses[]=acknowledged&limit=25&total=true` | Open incident titles and their service.  The headline uses `total`, not the page size, so a full page no longer reads as "exactly 25". |
| **Datadog Monitors** | `GET /api/v1/monitor?page_size=100` on the `us5` site | Monitor counts by `overall_state`, then the alerting and warning monitors by name. |

Every integration has its own timeout and its own error handling.  A missing secret
makes that one card read "Not configured"; a failing call puts the error text on
that one card.  Neither stops the rest of the page from rendering.

## `ok` and `state` are different questions

Every section answers both, and conflating them is what made the first version
paint healthy checks red.

| Field | Question | Values |
|---|---|---|
| `ok` | Did the API call work? | `true` / `false` |
| `state` | What did the answer say? | `up`, `warn`, `down`, `off`, `pending` |

`ok: true, state: "down"` is the normal case for a status page: the check ran fine
and found something broken.  The page colours the card by `state` and only shows a
red error block when `ok` is `false` — when `ok` is `true` and the check still had
something to report (a servers call that failed while the applications call
succeeded, say) it renders as a muted note instead.

## The subrequest budget

A Worker invocation may make 50 subrequests on the free plan.  The first version
fanned out to roughly 60 in one call and lost the recall canary to
`Too many subrequests by single Worker invocation`, with `/api/status` taking
12.8 seconds.  Two changes fixed it.

The page now asks for **one section per request**, all in parallel, and renders each
card the moment it lands.  Each section caches its own answer in the isolate for 30
seconds.  Worst case per section:

| Section | Subrequests |
|---|---|
| `endpoints` | 17 — one per entry in `ENDPOINTS` |
| `recall` | 3 — health, stats, canary search |
| `board` | 2 |
| `coolify` | 2 |
| `github` | 15 — one PR search plus one Actions call per repo (it was 24) |
| `vercel` | 6 — projects, teams, and up to four team-scoped calls |
| `sentry` | 1 |
| `pagerduty` | 1 |
| `datadog` | 1 |

Plain `GET /api/status` still answers for scripts and curl, but it runs the sections
**in order** against a 45-subrequest budget and defers whatever does not fit, marking
it `state: "pending"` (or serving the stale cached copy).  The next call picks those
up, because by then the expensive sections ahead of them are cached and cost nothing.
The response carries `subrequests` so you can see what a given call spent.

## Routes

| Route | Behaviour |
|---|---|
| `GET /` | The page, served from `public/` through the `ASSETS` binding. |
| `GET /api/status` | Every section, sequenced under the budget above.  Returns `{ checkedAt, overall, subrequests, sections }`. |
| `GET /api/status?section=…` | One section, with its own 30-second cache.  Returns `{ checkedAt, section, data }`.  This is what the page uses. |
| `GET /api/sections` | The section names, for anything that wants to enumerate them. |
| `GET /api/recall/search?q=…&limit=…` | Proxies the fleet RAG search and returns the hits.  Default `limit` 8, capped at 25. |

Every API response sets `Cache-Control: no-store`.  The page loads every section on
open, again every 60 seconds, and when the tab becomes visible.

## Timeouts

| Call | Timeout | Why |
|---|---|---|
| Endpoint probes | 8 s | Enough for a cold origin, short enough that the card is not held hostage. |
| Control-plane APIs | 12 s | Coolify, GitHub, Vercel, Sentry, PagerDuty, Datadog, the board. |
| `POST /recall/search` | 25 s | Hybrid search plus rerank is genuinely slow.  At 8 s the proxy and the canary both timed out on a query that answers fine in a terminal. |

The canary asks for 3 hits and the search box asks for 8.  Fewer hits is less work
for the reranker, which is where the time goes.

## Tunnel-backed hosts

`mac.jays.services`, `xcode.jays.services`, and `agent-sync.jays.services` reach the
Mac through a Cloudflare Tunnel.  They answer in well under a second from the Mac
and time out from Cloudflare's edge.  The panel cannot fix that route, so it reports
it honestly: a timeout is **amber** and says "Timed out from Cloudflare", not red
"down".  Only a genuine connection failure or a 5xx is red.

## Secrets

Set these from this directory before the first real check.  Every one of them is
optional in the sense that the page still renders without it — the matching card
just reads "Not configured".

```bash
wrangler secret put RECALL_API_TOKEN
wrangler secret put CF_ACCESS_CLIENT_ID
wrangler secret put CF_ACCESS_CLIENT_SECRET
wrangler secret put COOLIFY_TOKEN
wrangler secret put GITHUB_TOKEN
wrangler secret put VERCEL_TOKEN
wrangler secret put SENTRY_AUTH_TOKEN
wrangler secret put PAGERDUTY_API_KEY
wrangler secret put DD_API_KEY
wrangler secret put DD_APP_KEY
wrangler secret put MAC_COLLAB_TOKEN
```

Values in `~/.secrets/global-api-keys` are quote-wrapped.  Strip the quotes before
pasting, or the service answers 401 as if the token were revoked.

Two of those values had drifted and were repaired on 2026-09-29, because the
GitHub and Sentry cards were red for credential reasons rather than product
reasons:

| Worker secret | Was | Now | Symptom |
|---|---|---|---|
| `GITHUB_TOKEN` | the handoff `GITHUB_TOKEN` (HTTP 401) | handoff `GITHUB_ADMIN_PAT` | GitHub card `ok: false`, "PR search: HTTP 401 — Bad credentials" |
| `SENTRY_AUTH_TOKEN` | the handoff `SENTRY_AUTH_TOKEN` (HTTP 403, no org scope) | handoff `SENTRY_AUTH_TOKEN_FULLSCOPE` | Sentry card `ok: false`, "You do not have permission to perform this action" |

The handoff file's own `GITHUB_TOKEN` and `GITHUB_MCP_TOKEN` are both dead
(`401`) as of this writing, and its `SENTRY_AUTH_TOKEN` lacks the `simple-with-us`
org scope.  Do not re-paste those three into any app.  A credential that a card
reports as 401/403 is a *credential* problem — fix the token, not the card.
`SENTRY_ORG_TOKEN_MINIMAX` is also 403 on that org.

The Fleet Recall card needs three of these together: `RECALL_API_TOKEN` for the
service bearer, and `CF_ACCESS_CLIENT_ID` plus `CF_ACCESS_CLIENT_SECRET` for the
Cloudflare Access service token in front of the host.  `GET /health` is public and
is checked even when the other two are missing.

Non-secret configuration lives in `wrangler.jsonc` under `vars`: `GITHUB_OWNER`,
`SENTRY_ORG`, `DD_SITE`, and the `RECALL_BASE` / `BOARD_BASE` / `COOLIFY_BASE`
hostnames.

## Deploy

```bash
cd scripts/admin-panel
wrangler deploy
```

The Worker is `admin-jays-services` on the **Usage.Jays.Services** Cloudflare
account, zone `jays.services`, bound to `admin.jays.services` as a custom domain.

`compatibility_date` is pinned to `2026-08-27` because that is the newest date the
workerd binary shipped with wrangler 4.125.0 accepts locally.  Raise it once the
local binary catches up.

## Local development

```bash
cd scripts/admin-panel
wrangler dev --port 8790
curl -s 'localhost:8790/api/status?section=endpoints' | head -c 600
curl -s 'localhost:8790/api/status?section=github'    | head -c 200   # "Not configured"
```

Local mode needs no Cloudflare login.  Without secrets, the Endpoints card returns
real probe results and every secret-backed card reports "Not configured", which is
the intended degraded state.

To reproduce the *real* cards, put the secrets in `.dev.vars` (one `NAME="value"`
per line).  **It is gitignored and must stay that way** — the Worker holds every
fleet API token, so a committed `.dev.vars` leaks the whole fleet:

```bash
umask 077
printf 'GITHUB_TOKEN="%s"\n' "$GITHUB_ADMIN_PAT" > .dev.vars
```

Check it is ignored before you ever run `git add -A` here:

```bash
git check-ignore -v scripts/admin-panel/.dev.vars
```

## A canceled deploy is not a down service

`pickVercelDeployment` prefers the newest `READY` deployment, but Vercel only
carries a short window in `latestDeployments`.  Once a project's recent production
deploys are **all** canceled, no `READY` entry is left to find, the helper falls
through to the newest canceled one, and the card used to paint the whole thing
red.  That was a false alarm: `botfleet.app` and `contactlogo.com` both answer
`200`, and the Endpoints card said so on the same page.

Only a build that actually **failed** is a service failure.  `ERROR` is red.
`CANCELED`, `BUILDING`, `QUEUED`, and `INITIALIZING` are amber, and the headline
separates them — "5 projects, 0 failed, 2 without a ready deployment" — so an
ambiguous row is never silently reported as an outage.

## A renamed repo silently loses its CI status

`apiJson` never follows redirects, and that is deliberate: a followed hop would
hand `CF-Access-Client-Id` / `CF-Access-Client-Secret` to whatever host the
`Location` header names, and each hop is an uncounted subrequest.

The cost is that GitHub answers a renamed repo with a `301`, so a stale `repo:`
in `APPS` produced a permanent `HTTP 301 … not followed` on that row and took
`ok` to `false` for the whole card.  `Socratic-Trade` shipped here as
`Socratic.Trade` and did exactly that.

`scripts/check-fleet-registry.py` now compares the inlined `APPS` array against
`fleet-apps.json` in both directions — a registry app missing from the panel, and
a panel repo that is not in the registry — so this fails a check instead of
quietly going red.  Run it after any repo rename.

## Keeping it in sync

`APPS` in `src/index.js` mirrors `apps[]` from `fleet-apps.json` at the repo root.
The Worker has no filesystem, so the registry is inlined, and that copy is checked
by `scripts/check-fleet-registry.py` (see above).  Add a row when an app is
onboarded, and add its public URL to `ENDPOINTS` in the same file.

Deliberate absences and retired probes:

- **`admin.jays.services` is never probed.**  A Worker probing its own hostname
  spends a subrequest to learn what it already knows.
- **Hog Hunter has no endpoint row.**  It is a local-only Mac app with no product
  domain.  CodeCaps is probed at its custom domain `codecaps.simplewithus.com`
  (the `gh-pages` branch carries that CNAME; the `github.io` path is
  case-sensitive and the lower-case spelling 404s), and has an `APPS` row
  now that it is in `fleet-apps.json`.
- **Scout is retired (2026-09-09).**  Do not probe `scout.jays.services`.
- **`autorotate.codes` is retired (NXDOMAIN).**  Probe `autorotate.vercel.app`
  instead; Autorotate remains in `APPS` for the GitHub card.
