# 2026-09-01 — Sentry sponsored-account fleet integration plan (Grok)

## Summary

Plan-only.  Live inventory of org `jays-services` against the sponsored Sentry product surface, plus a phased integration plan.  No SDK, DSN, alert, or monitor changes in this unit.

## Why

Owner asked how to better integrate Sentry across development, hosting, and operations now that the org is sponsored.  Current capture is errors + some traces/logs/replay/crons; alerts, Seer, release health, mobile symbols, AI traces, and most CI reporters are unused or noisy.

## Files

- `docs/plans/2026-09-01-sentry-fleet-integration.md` — canonical plan.
- `STATUS.md` — pointer.
- `docs/EFFORT-LOG.md` — this lane.

## Verification

- Sentry MCP `find_organizations` / `find_projects` / `find_monitors` / `find_uptime_monitors` / `find_alert_rules` / `search_issues` / `find_releases` on 2026-09-01.
- Repo grep for `@sentry`, `SENTRY_DSN`, Cocoa `SentryTelemetry.swift`, `sentry-ci-report.yml`.
- `simple-board list --search sentry`.

## Follow-ups

- Owner: GitHub + Slack + PagerDuty integrations in the Sentry org; confirm Seer on the sponsored plan.
- Agents: unstick ST #3141 / #3146 and CT #2282 before a new expansion PR.
- First implementation lane (separate claim): Usage Monitor scheduler cron + sentry-ci-report + one Slack alert.
