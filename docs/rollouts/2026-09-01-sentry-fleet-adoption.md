# 2026-09-01 — Sentry fleet adoption (standing split + hygiene)

## Summary

Implementation of the 2026-09-01 Sentry fleet adoption report as fleet-level
docs and CI/ship hygiene.  This is not another plan-only document.  Canonical
plan remains `docs/plans/2026-09-01-sentry-fleet-integration.md` (AFC #158).
This rollout records the standing split agents must follow, the CI fingerprint
rule, and the Size Analysis TODO on the existing TestFlight ship path.

Source: `/Users/jay/Desktop/sentry-fleet-adoption-report-2026-09-01.md`.
Board `42c563a6`.  Branch `grok/sentry-fleet-adoption`.  Worktree
`~/apps/fleet-grok-sentry-adopt`.

## Standing split (binding)

Org `jays-services` (https://jays-services.sentry.io).  Eight Sentry projects:
`socratic-trade`, `congress-trade`, `usage-monitor`, `fleet-infra`, `dealdex`,
`botfleet`, `autorotate`, `contactlogo`.

**No Sentry project.  Do not create one.**

| Surface | Why |
|---|---|
| Personal-Site (`jays.services`) | Datadog only.  Agents must stop assuming Sentry covers it.  A tiny unhandled-window-error Sentry project is **not** wanted. |
| congress-trading-shared | Library.  Consuming apps report. |
| fleet-ops | No runtime. |

**Android SDK:** iOS Cocoa only until Android tracks ship (DealDex, Autorotate,
ContactLogo).  Do not add a Sentry Android SDK "just in case."

**Seer:** enable only for Socratic.Trade and Congress.Trade after confirming
contributor billing counts Jay's GitHub user only.  Do **not** connect every
repo yet.  Agent bot PRs must not mint $40/mo Seer seats.

### Datadog vs Sentry (do not double-pay)

| Signal | Sentry | Datadog |
|---|---|---|
| App exceptions, crash-free, replay | yes | no |
| Trace-connected debug logs | sparse yes | full warehouse |
| Token/cost/usage | no (Usage Monitor owns it) | optional |
| Infra, host, Cloudflare, RUM product analytics | no | yes |
| Cron / uptime for *app* jobs | yes | no |
| AI agent traces | yes (Seer-connected) | Datadog LLM Obs only if you want evals |

Do **not** enable Datadog Session Replay and Sentry Session Replay on the same
page.

## What this unit lands

- AGENT-SYNC Observability section: standing split, Seer gate, overlap table,
  CI fingerprint rule, Size Analysis TODO.
- This rollout (implementation of the adoption report, not a second plan).
- Plan addendum pointing here.
- `github-workflows-template/workflows/sentry-ci-report.yml` comment: fingerprints
  stay `[app, workflow]` only.  Branch and SHA are tags, never fingerprint
  components.  AFC has no `scripts/sentry-ci-report.py`; the template comment
  was still teaching the old `[workflow, branch]` grouping.
- Size Analysis TODO on `~/apps/ios-fleet/ship-testflight.sh` and
  `~/apps/ios-fleet/README.md`: upload the XCArchive via `sentry-cli` /
  Fastlane `sentry_upload_build` after a successful archive (100 builds/month
  included).  Do **not** invent a new LaunchAgent.  Hook the existing ship
  path only.
- ONBOARDING-NEW-APP: PS / CTS / fleet-ops do not get Sentry app projects.
- STATUS + effort logs.

Personal-Site Datadog-only README/AGENTS copy lands in
`Simple-With-Us/Personal-Site` on `grok/sentry-datadog-only` (board
`ca3e27f0`).

## What this unit does not land

Runtime work from the adoption report stays other lanes:

- Pause/delete zombie cron monitors.
- Resolve stale Sentry issues.
- Prod DSN / Replay / `enableLogs` on ST, CT, UM, DealDex, BotFleet.
- Drop CT `@sentry/cloudflare`; rotate the CT DSN in `.prod.vars`.
- Web SDK for Autorotate / ContactLogo.
- Android SDK (explicitly deferred until those tracks ship).
- Seer GitHub app install (owner click; confirm contributor count = 1 first).

## CI reporter fingerprints

Keep fingerprints on `[app, workflow]` only (Sentry event fingerprint
`["ci-failure", APP, workflow_name]`).  Do not add branch or SHA.  Branch
names in merge-queue refs minted throwaway `fleet-infra` issues once already.

## Size Analysis TODO

After Cocoa TestFlight ships start producing crashes, the next ship-path
change is:

```
sentry-cli build upload "$ARCHIVE_PATH"
```

or Fastlane `sentry_upload_build`, from `~/apps/ios-fleet/ship-testflight.sh`
once the `.xcarchive` exists.  100 size-analysis builds/month are included.
Five iOS apps times about four TestFlight ships/month fits the included
quota.  No new LaunchAgent.

## Files

- `AGENT-SYNC.md` — standing split (also live `~/apps/AGENT-SYNC.md`).
- `docs/plans/2026-09-01-sentry-fleet-integration.md` — addendum.
- `docs/rollouts/2026-09-01-sentry-fleet-adoption.md` — this file.
- `github-workflows-template/workflows/sentry-ci-report.yml` — fingerprint comment.
- `docs/ONBOARDING-NEW-APP.md` — no-project surfaces.
- `docs/MAC-LOCAL-PROCESSES.md` + live `~/apps/MAC-LOCAL-PROCESSES.md` —
  Size Analysis TODO on the existing `ship-testflight.sh` row.
- `STATUS.md`, `docs/EFFORT-LOG.md`, live `~/apps/FLEET-INFRA-EFFORT-LOG.md`.
- Machine-side: `~/apps/ios-fleet/ship-testflight.sh`, `~/apps/ios-fleet/README.md`.

## Verification

- Repo grep: Personal-Site / CTS / fleet-ops called out as no Sentry project.
- Template yml comment no longer says `[workflow, branch]`.
- `ios-fleet` ship script has the Size Analysis TODO next to `ARCHIVE_PATH`.
- Personal-Site CI still greps `Earlier work included` in `static/index.html`.
