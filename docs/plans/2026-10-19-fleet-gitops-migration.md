# Fleet GitOps:  inventory and ranked migration plan

Linear:  AFC-368.  Seat:  CLAUDE.  Written Mon, Oct 19, from a read-only sweep of the Mac (over SSH), the Hetzner server, and the AFC, FleetLink and Fleet-OPS repositories.  Nothing was changed, restarted or deployed.

## Goal

Jay's directive (Oct 19):  everything across the fleet should be GitOps wherever it fits and helps.  The desired state lives in git, a merge drives the deploy or the config change, and nothing is deployed by hand or left to drift.

Secrets stay in Infisical (AFC project, prod only).  Git holds key names, templates and the wiring;  a reconciler reads values from Infisical at apply time.  That is the same split `scripts/fleet_knobs` already uses.

This plan is public.  Host addresses, Coolify UUIDs, account ids and Infisical project ids stay in Fleet-OPS (`ATTACK-MAP.md`).

## Item 0 (in progress elsewhere)

The Hetzner server's fleet CLIs (`agent-sync`, `board`, `fleet-share`, `recall`, `lane`, `fleet-mode`) move from Mac-pushed release copies (`fleet-cli-sync.py`, `fleet-cli-release.py`) to a git checkout of AFC main.  The checkout moves on a GitHub merge signal, with an hourly safety check and a smoke test before it moves.  That work is under way in another session and is not re-filed here.  Items G1 and G2 build on its checkout.

What this sweep saw of the current state, for that session:  the pushed release on the server was 37 commits behind main (14 of them touching shipped paths), the `recall`, `lane` and `fleet-mode` wrappers were missing from `~/.local/bin`, and `code-main-sync.service` was failing because one repository's fetch fails and the unit exits on the first error.

## Already GitOps

These reconcile from git today and need no migration:

- Socratic.Trade, Congress.Trade, Usage-Monitor and the server agent-sync listener:  Coolify auto-deploy from `main` through the GitHub webhook, with watch paths.
- FleetLink API Worker:  `FleetLink/.github/workflows/worker-deploy.yml` on push (code only;  secrets stay manual), enabled since Oct 19.
- AFC digest site:  GitHub Pages from `fleet-activity-site.yml`.
- home.jays.services:  Fleet-OPS `vercel-deploy.yml` on push (`vercel build`, `vercel deploy --prebuilt --prod`, token from Infisical).
- Personal-Site and DealDex:  Vercel Git integration.
- On the Mac:  `agent-sync` and `board` (symlinks into the checkout that `com.jay.agent-sync-runtime-sync` keeps on main), `clutch-auto-update`, `fleet-handbook-sync`.
- Config already declared in git with a plan/check/apply tool:  Zulip linkifiers (`scripts/zulip_linkifiers.py`), GitHub rulesets (`scripts/apply-github-ruleset.py`, `audit-org-security.py`), the knob registry (`fleet_knobs inventory --check`), rate limits (`agent-sync-mcp/src/seat-state.js`), the listener partition (`docs/protocols/agent-sync-partition.toml`) and the board CLI catalog.

## Inventory

Columns:  how it deploys today, where its desired state lives, whether it drifts, and whether GitOps fits.

### Mac LaunchAgents

| Item | Deploys today | Desired state | Drift | GitOps fit |
|---|---|---|---|---|
| agent-sync-runtime-sync | Hand-copied plist | AFC `scripts/launchd/` | Matches | Yes;  it is already the puller and the base for G7 |
| mac-process-watch | Hand-copied plist and script | AFC `scripts/launchd/`, `scripts/mac-process-watch.sh` | Live script differs (still mentions mac-collab) | Yes |
| fleet-gdrive-backup, grok-idle-unload, sim-idle-reaper, ios-testflight-freshness, weekly-fleet-mode-digest | Hand-copied plists | AFC `scripts/launchd/` | Plists match | Yes |
| needs-owner-notify, mac-facts-publish, github-outbox-bridge, fleet-work-backup | Not installed (owner steps pending) | AFC `scripts/launchd/` plus installers | Repo ahead of the Mac | Yes, owner-gated |
| mac-seat-watch | Not installed | AFC `scripts/launchd/` only | No row in `MAC-LOCAL-PROCESSES.md` | Unknown until documented |
| agent-sync-listener | Hand-made plist;  code is a symlink into the checkout | Code in git;  plist only on the Mac | No plist template | Partial |
| echo-health, claude-remote-control, claude-update-bounce, merge-shepherd, hetzner-backups-prune, mac-server-watchdog, the pm2 launcher | Hand-made plists and scripts | Only on the Mac | Untracked;  some scripts edited in place | Yes once sourced into a repo |
| reaper-watch | Hand-made, runs from `~/apps/_archive/` | Only on the Mac | Exits 2 | No;  retire or source it |
| ios-ship-now | Hand-made | Only on the Mac | Exits 1 | No auto-bootstrap ever;  template only |
| shellular-watchdog | Unknown | Only on the Mac | No doc row;  exits 1 | Unknown |
| infisical-secrets-sync | Hand-copied | AFC | Matches;  due to retire under OPS-818 | Retire, do not migrate |
| provider-knob-sync | Hand-copied | Socratic-Trade repo | Differs | Yes |
| BotFleet server and companion | BotFleet `scripts/launchd` | BotFleet repo | Start scripts differ from both repos;  server killed;  doc names the wrong label | Yes |
| HogHunter robotic-vacuum | Runs from the HogHunter tree | HogHunter `launchd/` | Differs | Yes |
| Usage collectors (about ten) | `Usage-Monitor/scripts/install-collectors.sh` copies, last from a lane | Usage-Monitor `*.plist.example` | Three loaded collectors have no doc row;  doc gives the wrong path;  one plist carries a token in its environment | Partial;  token out first |
| Dead plists (agy-acp, xcode-health, shellular) | Left on disk | None | Dead | Delete (G4) |

### Mac pm2 and CLIs

| Item | Deploys today | Desired state | Drift | GitOps fit |
|---|---|---|---|---|
| pm2 ecosystem (ten apps) | Hand-copied `~/apps/pm2-ecosystem.config.cjs` | AFC `scripts/pm2-ecosystem.config.cjs` | Comments only;  live pm2 shows duplicate `grok-leader` and `botfleet-mcp` entries | Yes |
| vision-worker | Hand-copied from Congress.Trade | Congress.Trade `services/vision-worker` | Was 31 commits behind in August | Yes |
| grok-leader, grok-acp, seat-mcp, agy-acp | AFC copy-installers | AFC `scripts/grok-acp-runtime`, `seat-mcp`, `agy-acp-runtime` | Not hashed;  `.bak` files | Yes |
| botfleet-mcp | Detached checkout | BotFleet | Errored and duplicated | Yes |
| clutch-web | Clone plus auto-update | Clutch | Clean | Done |
| shellular, code-main-keeper, xcode-health | Pinned npm or `~/apps` scripts | Only on the Mac | Untracked | Partial |
| `lane`, lane and fleet guard hooks | `fleet_lanes.install_tools apply` from a main sha | AFC | Low | Yes;  run on a timer (G2) |
| `fleet-secrets`, fleet-rag, recall MCP | Copy-installers | AFC | Not checked | Yes (G2) |
| `~/.claude/hooks` | Copies of AFC `scripts/hooks` | AFC | One differs;  two hooks are not in AFC | Partial (G2) |

### Hetzner server

| Item | Deploys today | Desired state | Drift | GitOps fit |
|---|---|---|---|---|
| fleet-housekeep, ensure-swap, fleet-health-recover@ | Hand `install` | AFC `scripts/host/` | Matches;  `.bak` and `.pre-*` clutter | Yes (G1) |
| box-disk-hygiene | Hand `install` | Congress.Trade `scripts/ops/` | Live timer runs every 30 minutes, the repo says 15 | Yes (G1) |
| congress-health-recover, fleet-deploy-guard@, ct-deploy-guard | Hand `install` | Congress.Trade `scripts/ops/` | All three live scripts are behind the repo (the deploy guard lacks the docs-only skip) | Yes (G1) |
| ct-deploy-overlap, ct-reattach-proxy cron | Hand `install` | Congress.Trade `scripts/ops/` | Matches | Yes (G1) |
| usage-monitor-replica-status | Hand `install` | Usage-Monitor `deploy/coolify/` | Drop-in has no repo copy | Yes (G1) |
| fleet-tools-doctor timer | Hand-written unit;  script from the checkout | Script in AFC;  unit nowhere | Untracked unit | Yes (G1) |
| code-main-sync timer | Hand-written | Nowhere | Failing;  skips Fleet-OPS (origin not under Simple-With-Us) | Yes;  overlaps item 0 |
| docker-user-firewall | Hand-written | Nowhere | Untracked;  see "Found in passing" | Yes, owner-reviewed (G1) |
| fleet-backups and fleet-qdrant cron | Hand-edited `/etc/cron.d` | Scripts in Congress.Trade and AFC;  cron lines nowhere, or differ (Qdrant retention knobs approved Oct 17) | Yes | Yes;  knobs to Infisical (G1) |
| litestream-l1-only, coolify-auto-maintenance, vps-storage-watch | Hand-written | Nowhere | Untracked | Yes (G1) |
| WireGuard to the residential proxy | Hand config | Rollout doc only | Holds a key | Partial;  template in git, key in Infisical (G1) |
| GitHub self-hosted runners, Datadog agent, Tailscale, Coolify itself | Vendor installers | Vendor | — | No;  vendor-managed |

### Coolify

| Item | Deploys today | Desired state | Drift | GitOps fit |
|---|---|---|---|---|
| ST, CT, UM, agent-sync listener | Auto-deploy from main | Each repo;  app settings in the Coolify DB | Matches the docs | Done for code;  settings in G11 |
| echo-mail-relay | Auto-deploy from a feature branch (`instinct/echo-mail-relay`) on an sslip.io host | AFC branch | Not main | Partial (G11) |
| recall-api | Compose kept in Coolify;  downloads a tarball of a legacy repo slug at start | AFC `scripts/fleet-recall-service/` | Live compose differs from the repo (Python version);  code changes only on restart | Yes (G6) |
| qdrant, tei embeddings, tei reranker | Compose kept in Coolify | Samples in docs only | No canonical compose | Partial (G11) |
| Congress.Trade Traefik route and TLS files | Hand-placed;  cron rewrites the route each minute | Congress.Trade script | Out of band | Partial (G11) |
| botfleet-vps-shared | Hand `docker run` | None | Not Coolify-managed | No until it gets a compose |

### Cloudflare Workers

| Item | Deploys today | Desired state | Drift | GitOps fit |
|---|---|---|---|---|
| agent-sync-mcp | By hand, `wrangler deploy` from a fresh worktree.  `DEPLOY.md` says never from CI, because agents auto-merge to main and the deployer can act as every hosted seat (decision D8). | `scripts/agent-sync-mcp/wrangler.jsonc`;  secrets put by hand from Infisical | High:  main runs ahead of live;  secrets not reconciled | Partial;  needs Jay's call (G5) |
| admin-jays-services | By hand | `scripts/admin-panel/wrangler.jsonc`;  ten secrets by hand | High;  secret drift repaired Sep 29 | Yes (G5, G12) |
| pr-conflict-watch | By hand;  webhooks registered by hand | `workers/pr-conflict-watch/` | Still posts to Slack, retired Oct 7 | No;  retire or rebuild (G4) |
| start-jays-services | By hand;  idle since Sep 26 | `scripts/safari-start/wrangler.jsonc` still claims a hostname Vercel now serves | High | No;  retire (G4) |
| fleet-share-api (legacy) | By hand | `fleetlink-legacy` repo | May still hold routes or storage after the Oct 15 cutover | No;  retire (G4) |
| fleetlink-api | CI on merge | FleetLink | Low for code;  secrets manual | Done for code (G12 for secrets) |

`worker-deploy.yml` in AFC is built (keep-vars, SHA receipt, drain cron) but every run is skipped:  the repository has no `WORKER_DEPLOY_ENABLED` variable.

### Web, DNS and edge

| Item | Deploys today | Desired state | Drift | GitOps fit |
|---|---|---|---|---|
| AFC `vercel.json`, `vercel-ignore.sh`, `api/`, `public/` | Not deployed from AFC at all | Stale copies;  `vercel.json` names the old project | High;  AGENTS.md still calls `public/` the home.jays.services output | Pick one source (G3) |
| home.jays.services / start.jays.services | Fleet-OPS CI on push | Fleet-OPS;  SSO and redirect in the Vercel dashboard | Low;  the Oct 11 deploy was a CLI deploy from a dirty checkout | Done for code;  settings partial |
| Cloudflare zones, records, Access apps, WAF rules, tunnel ingress | By hand in the dashboard or API | Nowhere;  Fleet-OPS `docs/domains.json` is an observed inventory, not desired state | High;  Access apps removed by an unknown hand | Yes, with care (G16) |

### SaaS configuration

| Item | Set up today | Desired state | Drift | GitOps fit |
|---|---|---|---|---|
| Linear teams, seat apps, webhooks, integrations | Jay in the UI (app actors cannot hold `admin`) | Prose in `linear-migration.md`;  roster in `fleet-apps.json` | Docs disagree on the Linear→Zulip webhook | Check only;  apply stays Jay's |
| Linear workflow states and flat labels | By hand and on demand | Prose | Unaudited across 16 teams | Yes (G13) |
| Linear projects and templates | Ad hoc | Linear | — | No;  work items, not config |
| Zulip channels, subscriptions | UI | Prose;  `zulip_provision_bots.py` subscribes 5 of 9 channels | Yes | Yes (G9) |
| Zulip bots | `zulip_provision_bots.py`, owner-run | Hard-coded `ROSTER` plus `fleet-apps.json`:  two lists | Yes;  about 25 deactivated wake bots | Partial;  creation stays owner-run (G9) |
| Zulip outgoing webhooks | `zulip_migrate_outgoing.py`, operator-run | Private `listener.toml` | Yes | Partial;  manual by design |
| Zulip user groups and realm settings | Owner in the UI | Doc table | Unknown | Check only (some need the owner role) |
| GitHub rulesets and repo settings | Scripts, run by hand | AFC `ORG-BASELINE.md` and scripts;  Fleet-OPS `GITHUB-FLEET-SETTINGS.md` | Two audit scripts with different repo lists | Yes (G10) |
| GitHub Actions secrets | Per-repo copies | Names in `ORG-BASELINE.md` | Duplicated | Partial;  names in git, values never |
| Per-repo AGENTS.md, workflow templates, Renovate | Copied once at onboarding | AFC `TEMPLATE-AGENTS.md`, `github-workflows-template/`, per-repo `renovate.json` | High | Yes (G8) |
| Infisical projects, folders, identities, access | UI | Prose only | Yes (docs disagree on where Linear seat keys live) | Yes, audit first (G15) |
| Sentry alert rules and integrations | UI | Sentry | No Sentry route to the Zulip bot | Partial (G14) |
| Datadog monitors and dashboards | Fleet-OPS provisioning scripts, run by hand | Fleet-OPS scripts | Monitors still notify a retired Slack target | Yes (G14) |
| Claude Code cloud environments | claude.ai UI with a pasted script | Script in git;  environment only in the UI | Stale environments recorded | No for the environment (no API);  yes for the script |

### Agent and tool configs

| Item | Set up today | Desired state | Drift | GitOps fit |
|---|---|---|---|---|
| Skills | `install-fleet-skills.py`, run by hand, into ten tool homes | AFC `docs/fleet-skills` | Yes;  nothing runs it on the server | Yes (G2) |
| Rules blocks in CLAUDE.md, AGENTS.md and peers | `fleet_lanes.install_rules` marker blocks;  the rest hand-edited | Managed blocks in git | Yes | Partial;  blocks only, never whole files (G2) |
| Hook registrations in each tool's settings | `install-fleet-rag.sh --hooks`, `install_tools`, hand edits | Hook code in git;  registrations only on the host | Each host differs | Yes (G2) |
| MCP registrations | `fleet_lanes.install_mcp`, by hand | Installers in git;  live in each tool's config | Detected hourly by `fleet-tools-doctor` | Yes (G2) |
| Mac `listener.toml` | Hand-edited | Only on the Mac (the server copy is tracked) | Yes | Yes (G7) |
| Telemetry producer snippets | Generated, then copied by hand | Git plus Infisical | Likely | Partial by design |

## Ranked migration plan

Ranked by benefit against risk and effort.  B, R and E are benefit, risk and effort (H, M, L).  Each item is one Linear issue, all in the Linear project [Fleet GitOps](https://linear.app/simple-with-us/project/fleet-gitops-272cdd72e951).

| Rank | Item | B | R | E | Linear |
|---|---|---|---|---|---|
| 1 | G1  Hetzner host units, cron and scripts in git, applied by a check-then-apply reconciler | H | L | M | AFC-369 |
| 2 | G2  Agent configs (skills, rules blocks, hooks, MCP) reconciled on every host from main | H | L | M | AFC-370 |
| 3 | G3  One source for home.jays.services;  remove AFC's stale Vercel copies | M | L | L | AFC-371 |
| 4 | G4  Retire hand-deployed leftovers (three Workers, dead plists, server `.bak` files, Slack targets) | M | L | L | AFC-372 |
| 5 | G5  Workers deploy on merge:  admin panel first, agent-sync-mcp on Jay's call | H | M | L | AFC-373 |
| 6 | G6  recall-api built from its Dockerfile, auto-deployed from AFC main | M | M | M | AFC-374 |
| 7 | G7  Mac LaunchAgents and pm2 from a manifest, reconciled by runtime-sync | H | M | H | AFC-375 |
| 8 | G8  Per-repo templates propagated by sync pull requests | H | L | M | AFC-376 |
| 9 | G9  Zulip channels, subscriptions and bot roster declared and reconciled | M | L | L | AFC-377 |
| 10 | G10  GitHub org baseline enforced on a schedule from one repo list | M | L | M | AFC-378 |
| 11 | G11  Coolify service composes and app settings exported to git, with a drift check | M | M | M | AFC-379 |
| 12 | G12  Worker secrets synced from Infisical on deploy | M | M | M | AFC-380 |
| 13 | G13  Linear workflow states and flat labels declared and reconciled | M | L | M | AFC-381 |
| 14 | G14  Datadog and Sentry config applied from git | L | L | M | OPS-974 |
| 15 | G15  Infisical folder, identity and access structure audited against a file | M | M | M | OPS-975 |
| 16 | G16  DNS and Cloudflare edge as code:  export, drift check, then apply on merge | H | H | H | OPS-976 |

### G1  Hetzner host config in git

Add `deploy/hetzner/` to AFC:  every unit, timer, drop-in and `/etc/cron.d` file, and a manifest that names each script's source (AFC `scripts/host/`, Congress.Trade `scripts/ops/`, Usage-Monitor `deploy/coolify/`).  A `check` mode prints a diff against the live files;  `apply` installs, runs `daemon-reload` and restarts only what changed.  It runs from item 0's checkout on the same merge signal, check-only at first.  Brings in the seven untracked artifacts, the three stale Congress.Trade scripts, the timer interval drift and the Qdrant retention knobs (to Infisical), and clears the `.bak` files.  The WireGuard config becomes a template with its key in Infisical.

Done:  `check` reports no drift on the server, and a merge that changes a unit reaches the server without a hand step.

### G2  Agent configs on every host

The installers exist (`install-fleet-skills.py`, `fleet_lanes.install_rules`, `install_tools`, `install_mcp`, `install-fleet-rag.sh --hooks`).  Wrap them in one `fleet-agent-config reconcile` that runs `verify` on each host after the checkout moves, and `apply` for the managed blocks and registrations.  Owner-owned files (`~/.claude/CLAUDE.md`, `~/AGENTS.md`) stay report-only.  Extend `fleet-tools-doctor` to report the result.

Done:  the Mac and the server report the same skill set, hooks and MCP registrations as main, and a skill change on main reaches both hosts without a hand run.

### G3  home.jays.services

The live site deploys from Fleet-OPS.  AFC's `vercel.json`, `vercel-ignore.sh`, `api/` and `public/` are stale copies, yet AGENTS.md and `tests/deployment-boundary.test.cjs` describe them as the deployment.  `refresh-rag-snapshot.yml` commits `site-snapshot.json` to AFC, where nothing ships it.  Pick one source, delete the other copy, move the snapshot refresh to the source repo, and fix AGENTS.md.

Done:  one repository holds the site, and its merge is the only way the site changes.

### G4  Retire leftovers

Delete the `start-jays-services` Worker or its route (it claims a hostname Vercel serves), confirm the legacy `fleet-share-api` Worker holds no route or storage and delete it, and either retire `pr-conflict-watch` or rebuild it with a health probe and code-defined webhooks (it still posts to Slack).  On the Mac, remove the dead plists and the archived `reaper-watch`.  On the server, remove the `.bak` and `.pre-*` copies.  Point Datadog monitors away from the retired Slack target.

Done:  nothing deployed is without a source, and nothing points at Slack.

### G5  Workers deploy on merge

`worker-deploy.yml` is ready and skipped, and its gate is not safe to flip as built:  the single repo-wide `WORKER_DEPLOY_ENABLED` variable gates a matrix that today lists only `agent-sync-mcp`, whose CI deploys `DEPLOY.md` forbids because a deploy credential can act as every hosted seat.  Before any variable is set, make the gate per Worker - one variable per Worker, named from the Worker id in `scripts/worker-targets.json` with the hyphens replaced by underscores (`admin-jays-services` gives `WORKER_DEPLOY_ENABLED_ADMIN_JAYS_SERVICES`), so the gate key and the `matrix.worker` value passed to `deploy-mapped-worker.py` derive from that one id - with `agent-sync-mcp` out of the matrix until Jay's owner gate in D8 is met.  Give the admin panel its own wrangler (install it under `scripts/admin-panel/` or invoke `npx wrangler`) rather than the `scripts/agent-sync-mcp/node_modules` binary every target's command currently names, and scope the `agent-sync-mcp` install and test steps to the `agent-sync-mcp` matrix entry instead of running them for every entry.  Scope the trigger to each Worker's paths as well, so a merge that touches none of a Worker's files does not redeploy it.  Then mint the Cloudflare token scoped to that one Worker and hold it in Infisical, and only then enable the gate and prove one merge-driven deploy with its health probe:  the deploy job reads that scoped token instead of the fleet-wide `CLOUDFLARE_FLEET_API_TOKEN`, which it must stop fetching, because a merge-driven job running merged code with a fleet-wide credential lets a merge deploy any Worker.  The required review covers the deploy inputs as well as the Worker's own paths - `scripts/worker-targets.json` and `.github/workflows/worker-deploy.yml` - and the job refuses any target whose `--config` path falls outside that reviewed set.  That scoping is what would answer D8.

Done:  a merge that touches a Worker's paths deploys it and no other merge does, and the deployed SHA matches main.

### G6  recall-api

The service downloads a tarball of a legacy repository slug at start and runs a compose kept only in Coolify, which no longer matches `scripts/fleet-recall-service/`.  Make it a Coolify Dockerfile application on AFC main with watch paths on `scripts/fleet-recall-service/` and `scripts/fleet_rag/`, so a merge redeploys it.  Settings stay in Infisical under the existing contract.

Done:  recall's running commit equals the last merge that touched its paths.

### G7  Mac LaunchAgents and pm2

Extend `com.jay.agent-sync-runtime-sync` (it already fetches main, smoke-tests, rolls back, remembers bad commits and honours a pause marker) into a manifest-driven reconciler.  A `deploy/mac/manifest.toml` lists each unit:  source repo, ref and path, a plist template (HOME and PATH filled in, never a secret), the reload action (`launchctl bootout` and `bootstrap`, or `pm2 startOrReload --only`), a smoke test, and flags for owner-gated and never-bootstrap units (`ios-ship-now`, anything that needs Full Disk Access).  Units owned by other repos call that repo's installer at a pinned ref.  Report-only first, through `mac-process-watch`.

Before apply:  move the token out of the usage-collector plist, pull the Mac-only scripts into a repo, render the Mac `listener.toml` from git, and add the missing `MAC-LOCAL-PROCESSES.md` rows (shellular-watchdog, three collectors, BotFleet companion, mac-seat-watch) and fix the collector paths.

Done:  every LaunchAgent and pm2 job on the Mac matches its manifest entry, and a merge reaches the Mac without a hand copy.

### G8  Per-repo templates

`TEMPLATE-AGENTS.md`, `github-workflows-template/` and each repo's `renovate.json` are copied once at onboarding and drift.  A scheduled AFC workflow opens a sync pull request in each `fleet-apps.json` repository when a managed block or template differs (the repo still reviews and merges it).  Add a shared Renovate preset.

Done:  every fleet repository's managed blocks match AFC, or has an open sync PR.

### G9  Zulip

Declare channels, their settings and the subscription set in one file next to the linkifier target, take the bot roster from `fleet-apps.json` instead of `zulip_provision_bots.py`'s own list, and give it the linkifier tool's plan/check/apply shape.  Bot creation and key rotation stay owner-run.  User groups and realm settings get check-only.

Done:  `--check` shows no difference between the file and the realm.

### G10  GitHub org baseline

Run `audit-org-security.py` and the ruleset check on a schedule, merge the Fleet-OPS audit into it, and read the repository list from `fleet-apps.json` (also for `agent-calendar.yml`).  Apply needs a credential that can write rulesets, which does not exist today.

Done:  a weekly run reports no drift, or opens an issue naming it.

### G11  Coolify services and settings

Export each Coolify service compose (Qdrant, both TEI services) and each application's non-secret settings (source, branch, build pack, watch paths, auto-deploy) into Fleet-OPS, and add a drift check through the Coolify API.  Move echo-mail-relay to main.  Bring the Congress.Trade Traefik route and TLS files under that repo's deploy.  Apply through the API comes after the check has run clean for a while.

Done:  the check runs on a schedule and reports no difference.

### G12  Worker secrets from Infisical

Workers' secrets are put by hand with `wrangler secret put`.  Add a sync step to the deploy job that reads the Worker's folder in Infisical and puts only what changed, by key name, never printing values.  This is owner items A1 and A4 in the agent-sync-mcp docs.

Done:  rotating a key in Infisical reaches the Worker on its next deploy with no hand step.

### G13  Linear workflow states and labels

Declare the workflow states and the flat workspace labels in a file, check every team against it, and apply what the app scope allows (states and labels).  Teams, apps, webhooks and integrations stay Jay's;  the check reports them.  Settle which doc is right about the Linear→Zulip webhook.

Done:  `--check` passes for all teams.

### G14  Datadog and Sentry

Run the Fleet-OPS Datadog provisioning scripts on merge (they are idempotent and have `--dry-run`), and declare Sentry alert rules and their Zulip routing in the same way.

Done:  monitors and alert rules change only through a merge.

### G15  Infisical structure

Declare the expected folders, machine identities and their access in Fleet-OPS, and audit live against it (names and memberships only).  Consider the Infisical Terraform provider for apply later.  Values never enter git.

Done:  the audit runs on a schedule and reports no difference.

### G16  DNS and Cloudflare edge

Export the zones, records, Access applications, WAF rules and tunnel ingress into Fleet-OPS with octoDNS, dnscontrol or Terraform, run a drift check first, then move to apply on merge one zone at a time.  This is the riskiest item:  a bad apply takes sites down, so it goes last and in small steps.

Done:  a merge is the only way a DNS record or Access app changes, and the check shows no drift.

## Decisions for Jay

1. **agent-sync-mcp deploys from CI (G5).**  Lift the `DEPLOY.md` "never from CI" rule once a Cloudflare token scoped to that one Worker exists and its paths need review?
2. **Deploy credentials in CI (G5, G12, G16).**  Which scoped Cloudflare tokens to mint (per Worker;  DNS edit per zone), kept in Infisical and read only by the deploy jobs.
3. **Reconcilers that restart things (G1, G7).**  May they restart a changed unit on their own once check-only has run clean, or should they stay report-only?
4. **Owner-owned files (G2).**  Keep `~/.claude/CLAUDE.md` and `~/AGENTS.md` report-only, as assumed here?
5. **home.jays.services (G3).**  Fleet-OPS or AFC as the one source.
6. **Retiring Workers (G4).**  OK to delete `start-jays-services`, the legacy `fleet-share-api` and `pr-conflict-watch`?
7. **A credential that can write GitHub rulesets (G10).**  None exists today.
8. **An Infisical admin identity for the structure audit (G15),** and whether to adopt the Terraform provider.
9. **DNS tool and order (G16).**
10. **pm2 auth (G7).**  pm2 runs in the launchd System session, where `fleet-secrets` cannot reach the login keychain (already open under OPS-818).

## Not a GitOps fit

- **Secret values.**  They stay in Infisical;  git holds names and wiring only.
- **Linear team and app creation, Zulip bot creation, Zulip owner-role settings.**  Owner-only operations;  they get check-only.
- **Claude Code cloud environments.**  No API;  only their setup script is in git.
- **Apple Notes, Linear projects and templates.**  Content and work items, not config.
- **Vendor control planes:**  Coolify itself, the Datadog agent install, GitHub runners, Tailscale, agent binaries that update themselves.
- **App Store releases.**  TestFlight already ships from CI;  release to the store stays manual by policy.
- **`ios-ship-now`.**  Must never be bootstrapped automatically.

## Found in passing

- **The server's `DOCKER-USER` drop rule for port 8000 has never matched a packet.**  DOCKER-USER sees packets after DNAT, so the rule probably needs `--ctorigdstport`.  Filed separately as OPS-977, since it is a firewall question, not a migration.
- **Item 0 state** (above):  stale release, missing wrappers, failing `code-main-sync`.  Passed to the session doing item 0.
- **`MAC-LOCAL-PROCESSES.md` is missing rows** and gives the wrong collector path;  folded into G7.
