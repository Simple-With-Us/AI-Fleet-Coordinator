# Datadog vs Sentry (do not double-pay)

> Moved from `AGENT-SYNC.md` on 2026-09-01 (Plan B slice 2, doc diet).  Still binding for every agent on every platform.  Canonical pointer stays in AGENT-SYNC.md; this file is the full text and is ingested into the fleet-agents corpus (`recall`).

| Signal | Sentry | Datadog |
|---|---|---|
| App exceptions, crash-free, replay | yes | no |
| Trace-connected debug logs | sparse yes | full warehouse |
| Token/cost/usage | no (Usage Monitor owns it) | optional |
| Infra, host, Cloudflare, RUM product analytics | no | yes |
| Cron / uptime for *app* jobs | yes | no |
| AI agent traces | yes (Seer-connected) | Datadog LLM Obs only if you want evals |

Do **not** enable Datadog Session Replay and Sentry Session Replay on the same page.

Fleet infrastructure telemetry goes to Sentry project **`fleet-infra`** (org `simple-with-us`);
app-runtime errors stay in the app projects listed above. Conventions:

- **Tag every event** with `agent:<YOUR-TAG>` and `app:<repo>`; fingerprint deliberately
  (condition + subject, e.g. `["pm2-crash-loop","trading-codex"]`) so persisting conditions
  dedup instead of spamming. Severity: production down = error; degraded/budget = warning.
- **DSN access**: Mac sessions read `~/apps/fleet-sentry-monitor/.env`
  (`SENTRY_FLEET_DSN` — never print it). CI contexts use the repo secret `SENTRY_FLEET_DSN`.
  Cloud sessions without Mac FS: use the Sentry MCP connector if your session has it, or the
  repo secret via a workflow; if neither, report the condition in #agent-sync and a Mac-side
  agent forwards it.
- **Do NOT duplicate the singletons**: ONE host monitor per machine (pm2 `fleet-sentry-monitor`
  on the Mac: pm2 crash-loops/down, disk/WAL, gh rate budgets, Claude.app stats, self-watching
  check-in `fleet-host-monitor`); ONE CI reporter per repo (`.github/workflows/sentry-ci-report.yml`,
  additive `workflow_run` file: every workflow failure -> Sentry issue; scheduled workflows ->
  cron check-ins slug `ci-<workflow-slug>` so silently-stopped jobs alert by absence).
- **CI reporter fingerprints**: `scripts/sentry-ci-report.py` fingerprints stay
  `[app, workflow]` only (`["ci-failure", APP, workflow_name]`).  Branch and SHA
  are tags, never fingerprint components.  Putting the branch in the fingerprint
  minted throwaway `fleet-infra` issues off merge-queue refs.
- **New repos**: add the additive `sentry-ci-report.yml` (copy from
  `AI-Fleet-Coordinator/github-workflows-template/workflows/`) as part of
  bootstrap, after reserving on the board.  Do **not** mint a Sentry *app*
  project for Personal-Site, congress-trading-shared, or fleet-ops.  Long-running
  per-agent background jobs you own get their own cron monitor (slug
  `<agent>-<job>`, upsert on check-in).
- **Size Analysis TODO**: `~/apps/ios-fleet/ship-testflight.sh` should upload the
  XCArchive via `sentry-cli` / Fastlane `sentry_upload_build` after a successful
  archive (100 builds/month included).  Do not invent a new LaunchAgent.  Hook
  the existing ship path only.
- **Codex host coverage**: the singleton Mac monitor also records Codex Desktop
  process/session breadcrumbs. Treat old Codex OTEL config in `~/.codex/config.toml`
  as legacy unless a collector is intentionally installed; do not alert on that
  remnant. Do not create a second Codex monitor; extend `fleet-sentry-monitor`
  instead.

