---
name: deploy-verify
description: >-
  Verify production after a merge or deploy — per-app health URLs, Coolify vs Vercel vs library-tag, TestFlight freshness for iOS apps, backup continuity, and known failure classes.  Use after merging to main, after a Coolify/Vercel deploy, after a merge that touches an app's native iOS tree, when health looks stale, or when the owner asks if prod is up.  Do not treat Coolify API status as truth.
---

# Deploy verification (MONET)

> **Retired seat.**  Owner directive 2026-10-07: the Monet Claude account and app are no longer used, and `CLAUDE` is the only Claude seat.  Do not take work as MONET, do not leave MONET In Progress, and do not install this pack anywhere.  This catalog copy is inactive.


Read `/Users/jay/apps/COOLIFY.md` before poking the API.  Prefer **public health + `docker ps` on the box** over Coolify UI/API `status` (that field goes stale).

Operator host (live 2026-08-07+): Hetzner NBG1 **`<PROD_ORIGIN_IP>`** / `fleet-hetzner-nbg1`.  SSH `ssh -i ~/.ssh/hetzner root@<PROD_ORIGIN_IP>`.  Dashboard `https://host.jays.services`.

**Retired — do not use:** `<HETZNER_IP_RETIRED>`, `<ORACLE_IP_RETIRED>`, Oracle Tailscale `<TAILSCALE_IP_RETIRED>`, Coolify UUID `<OLD_ST_COOLIFY_APP_UUID>`, token name `COOLIFY_API_TOKEN` on the command line.

## Token split

| Key | Use |
|-----|-----|
| `COOLIFY_SERVER_STATS` | Read-only.  App server-stats panels.  Infisical metrics token. |
| `COOLIFY_AGENTS` | Full deploy/admin.  Agent ops only.  **Never** store this in Infisical as the app's `COOLIFY_API_TOKEN`. |

Load via env, not argv.  Names only: `fleet-secrets names`.  One value: `fleet-secrets get NAME`.  See `secret-handoff`.

If Infisical still has `COOLIFY_API_TOKEN` for metrics, it must equal `COOLIFY_SERVER_STATS`.

## Per-app how merge becomes prod

| App | Production | Mechanism | Health |
|-----|------------|-----------|--------|
| ST | https://socratictrade.com | Coolify auto-deploy on `main`.  UUID `<ST_COOLIFY_APP_UUID>` (confirm live).  Merge == live — do **not** also click Deploy. | `curl -fsS -A 'Mozilla/5.0 (compatible; fleet-coolify/1.0)' https://socratictrade.com/api/health` |
| CT | https://congress.trade | Coolify auto-deploy (`app/**` / `services/**`).  UUID `<CT_COOLIFY_APP_UUID>`.  Worker `ship.sh` / `deploy.yml` is leftover. | `curl -sA 'Mozilla/5.0' https://congress.trade/api/health` (bare curl historically 403) |
| UM | https://usage.jays.services | Coolify webhook.  UUID `<UM_COOLIFY_APP_UUID>`.  Render and the Oracle auto-deploy timer are retired. | `curl -fsS https://usage.jays.services/api/health` and `curl -fsS 'https://usage.jays.services/api/ready?strict=1'` |
| DealDex | https://dealdex.net | **Vercel** on merge.  Do not Coolify.  `dealdex.vercel.app` is a different Next.js site. | `curl -sI https://dealdex.net` |
| Personal-Site | https://jays.services | Vercel behind Cloudflare.  Production git deploys only when `site/` changed, at most once per hour.  Previews skipped.  Do not create a second Vercel project. | `curl -sI https://jays.services` |
| CTS | published tag `vX.Y.Z` | Library.  Announce in the Zulip work topic, then tag.  Consumers pin the tag. | n/a |
| AFC | GitHub Pages digest | `fleet-activity-site.yml`, not Coolify. | `https://simple-with-us.github.io/AI-Fleet-Coordinator/` (the old `mac.jays.services/health` check was retired 2026-10-19) |

Prefer live `GET /api/v1/applications` (via a helper that reads the token itself, or Coolify MCP) over memorized UUIDs.

The live sanctioned deploy paths are in `docs/protocols/production-deploys.md` (and this table + `~/apps/COOLIFY.md`, top Hetzner section only; the Oracle body below the cut is historical).  Do not look for a deploy-path table inside FLEET-PROTOCOL.md — that file is not in this repository.

Steady state: ST/CT/UM auto-deploy via Coolify webhook; DealDex via Vercel; AFC digest via Pages; CTS via tag.  Announce-then-deploy (~10 min, one deployer) only for a **manual** Coolify/API click.  Do not double-trigger: Coolify cancel is unreliable.

Native iOS apps do not deploy through Coolify or Vercel.  Their production is TestFlight, verified in [section 5](#5-ios-apps-and-testflight-freshness).

## 1. App health (always start here)

ST fields are nested under `.checks` (camelCase):

```bash
curl -fsS -A 'Mozilla/5.0 (compatible; fleet-coolify/1.0)' https://socratictrade.com/api/health \
  | jq '.ok, .checks.db, .checks.schedulerAgeSeconds'
```

Expect `ok: true`, `db` ok, scheduler age small if a scheduler exists.  Root may 307 to `/login`.

CT: send a browser User-Agent.  A workflow red X on health is often a **false** Cloudflare challenge, not a failed deploy.

## 2. Coolify API (optional, often 403)

`host.jays.services` has sat behind a Cloudflare allowlist.  A 403 from your laptop is **not** a production outage.

Do not interpolate the token into a command you will see.  Prefer Coolify MCP or a wrapper that reads `COOLIFY_AGENTS` itself.  Expect `status: "finished"` only as a hint — then confirm public health and:

```bash
ssh -i ~/.ssh/hetzner root@<PROD_ORIGIN_IP> 'docker ps --format "{{.Names}} {{.Status}}"'
```

**Zombie:** a deploy stuck `in_progress` blocks the queue (`concurrent_builds` serializes).  Post the deployment id in the app's Zulip work topic (`simple-chat post --topic "<ID> <subject>" …`) if you can see it from the box.

**Silent freeze (ST #2545 class):** webhook 200 + healthy `/api/health` on an **old** sha.  Standing watch: ST `.github/workflows/deploy-freshness.yml`.  Do not hand-trigger; inspect the queue.

## 3. Backups (ST/CT/UM SQLite)

Litestream (or the host `/usr/local/sbin/fleet-sqlite-backup.sh`) is the DB path.  Prefer `/api/health` storage fields over browsing R2.

ST:

```bash
curl -s https://socratictrade.com/api/health \
  | jq '.checks.storage | {litestreamAgeSeconds, litestreamStatus, litestreamState, litestreamDegradedReasons}'
```

`storageDegraded: true` or `stale`/`stopped` → escalate immediately in the Zulip work topic + with a Linear comment.

Litestream 0.5.12 is the pin after 0.5.14 leaked TCP sockets (2026-07-10).  Do not "upgrade" it casually.

Host scripts (cron): `fleet-sqlite-backup.sh`, `fleet-health-verify.sh`, `fleet-backup-verify-weekly.sh`.  Hetzner server backups are the outer layer — not sufficient alone for trading DB RPO.

## 4. Box checks

```bash
ssh -i ~/.ssh/hetzner root@<PROD_ORIGIN_IP>
df -h / | grep -E 'Use%|^/dev'
ps aux | grep -E 'nixpacks|coolify' | grep -v grep
```

Disk hygiene timer already prunes above high watermarks.  Sustained pressure or a stuck build is a wedged queue — Coolify container logs, not a second deploy click.

Do not revert raised `tcp_mem` sysctl just because a deploy succeeded.

## 5. iOS apps and TestFlight freshness

Health URLs say nothing about a native app.  After a merge that touches an app's native iOS tree, the deploy is **not verified** until the TestFlight freshness check reports that app current.  A merged PR, a green `ios-build.yml` compile check, and a healthy API do not count.

```bash
node ~/apps/ios-fleet/ios_testflight_freshness.mjs --app <key>    # or --repo <Repo>;  --json for the report
```

Exit 0 means current, so the deploy is verified.  Exit 1 means not yet: `SHIPPING` or `PROCESSING` is a ship already in flight, so re-run it later; `BEHIND` names the missing commit and says why the automatic catch-up did not run.  Exit 2 means the check itself failed.  App keys and native paths live in AFC `scripts/ios-fleet/testflight-apps.json`, and the full guide is AFC `docs/ios-testflight-freshness.md`.  This post-merge check is report-only, so never pass `--ship-behind` here.  The LaunchAgent `com.jay.ios-testflight-freshness` runs the catch-up ships every two hours under the fleet gates.  Quote the verdict in the closeout.

"Touches the native iOS tree" means what the app's own ship workflow lists under `paths:`.  A merge that matches none of those paths leaves TestFlight where it was, which is current; say so in the closeout instead of shipping.

**When the check says behind**, look before you change anything:

1. **Ship workflow run.**  `gh run list --repo Simple-With-Us/<repo> --workflow <file> --limit 5`.  A run for the merge SHA that failed: read the failing step (archive, signing, upload, ensure-tf-ready).  No run at all: a merge made by `github-actions[bot]` with `GITHUB_TOKEN` fires no `push` workflows, so only the cron backstop or a `workflow_dispatch` ships it.
2. **Runner.**  A run stuck queued has no runner.  Read the job's `runs-on`: most ship jobs use hosted `macos-latest`, Clutch and BotFleet use `xcode-27`.  Do not turn on a Mac PR-check runner (banned in FLEET-PROTOCOL.md § CI Runners) and do not run a local archive or `ship-testflight.sh` from a seat to catch up.  Re-ship through the repo's workflow.
3. **Rate gate.**  A skip for the per-app minimum interval or for an unchanged git HEAD exits 0 and looks green, so it is not a failure and not a reason to force anything.  Read the live interval from `DEFAULT_MIN_INTERVAL_SEC` in `~/apps/ios-fleet/ship-testflight.sh`, because the README table has disagreed with the script.  Never set `IOS_TF_MIN_INTERVAL_SEC` as a standing workflow value.  Never force a ship past the gate (FLEET-PROTOCOL.md); a per-run override is for when the owner says so for that run, preferably as a one-off `workflow_dispatch`.
4. **Compile check.**  A red `ios-build.yml` on main usually means the archive fails too.  That is a code fix on main, not a ship fix.

Per-app quirks (read the workflow on main before trusting this list):

- **ST, CT, UM, BotFleet, Clutch** ship from `ios-ship.yml`.  UM ships two apps, Usage Client Monitor and Usage Local Monitor, so check both.  Most have a cron backstop; Clutch does not.
- **DealDex** `ios-ship.yml` was disabled by hand for a signing hold (last scheduled success 2026-09-27).  `ios-ship-cron-park.yml` only sends a Sentry heartbeat and never uploads, so its green runs prove nothing.  Re-enabling is the owner's call.
- **Autorotate** was retired from TestFlight on 2026-10-10 and the freshness check does not list it.  Its `testflight.yml` is dispatch-only with a signing-ready attestation; do not attest signing you have not verified.
- **ContactLogo** `ios-testflight.yml` is the fleet pipeline (CL-204, 2026-10-17): push to `main` on `Apps/ContactLogoiOS/**`, `Sources/ContactLogoKit/**` or `project.yml`, a path-gated twice-hourly catch-up, and `workflow_dispatch`.  It needs no repository variable (`CL_IOS_TESTFLIGHT_ENABLED` and `CL_APPLE_ID` are retired) and takes no required dispatch input.

**Rules that matter** (FLEET-PROTOCOL.md § Releases, Versioning, and Brand Assets):

- What to Test is mandatory on every upload and is published to App Store Connect, never dry-logged.  Use the FLEET-PROTOCOL.md Appendix C template, a real U+00A0 plus a normal space between sentences, and the owner's 12-hour clock with am or pm and no zone abbreviation.  No seat, agent, or tool names, ever.
- The marketing version and the build number are both the repo's commit count, `1.0.<commit_count>` and `<commit_count>`, so the pair always describes one commit and no version string is ever reused.  UTC `YYYYMMDDHHmm` build stamps are retired.
- The owner authorized TestFlight uploads.  App Store review submission and public release are not authorized; they stay an owner action.  Do not change external TestFlight groups or testers.

## 6. After verify

Move the Linear issue to **Deployed** (`simple-board status <id> deployed --resolution "..."`) only when you say how you verified; for an iOS app that includes the freshness verdict from section 5. `closeout` skill.  Rollback: Coolify previous deployment / Vercel rollback — not Mac pm2, not `trading-publish.sh` (retired).

## Canon

- `/Users/jay/apps/COOLIFY.md`
- `~/apps/ios-fleet/README.md` — TestFlight ship, the rate gate, and the freshness script
- `docs/protocols/production-deploys.md` — sanctioned deploy paths and Coolify tokens
- `/Users/jay/apps/FLEET-PROTOCOL.md` — Releases, Versioning, and Brand Assets (owner copy)
- ST `docs/rollouts/2026-08-07-hetzner-fleet-cutover.md`
- ST `docs/rollouts/2026-07-10-auto-deploy-on.md` (mechanism; UUIDs/IPs in that note may be pre-cutover)
- Skills: `secret-handoff`, `closeout`, `land-lane`
