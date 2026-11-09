# 2026-09-04 — Sentry Max Features (Fleet Matrix)

Jay + Designer.  Fresh ACP.  Board `af1ab6e9`.  Branch
`grok/sentry-max-features-fleet`.  Worktree
`~/apps/fleet-grok-sentry-max-features`.

Default is the full Sentry product surface on every app that has a project:
Issues, Performance, Replay, Feedback, Crons, Metrics, Logs, Profiling,
Releases, Alerts, Seer, mobile.  Kill switches are sample rates and
explicit `*_ENABLED=false` flags — never a silent code skip.

AFC itself is telemetry/docs for overall dev-platform setup (not a product
Sentry target).  Ignore Litestream.

## Designer rulings (2026-09-04 UPDATE)

| Surface | Rule |
|---|---|
| Personal-Site | **OMIT** Sentry (approved, no PR).  Datadog only. |
| congress-trading-shared | **OMIT** (approved).  Library; consumers report. |
| fleet-ops | **OMIT** (approved).  No runtime. |
| ST / CT **web** Replay | Error **100%** / session **10%** (CHANGED from session 0%).  Mask-all. |
| Seer Autofix | **ENABLE BotFleet only.**  Hold on every other app.  RCA / Slack `rca_completed` stays. |
| Android | **ENABLE** for apps with Android tracks (DealDex, Autorotate, ContactLogo). |

Listed, not silent: CT **iOS** session Replay stays 0% (filings PII bar;
error Replay on).  Autorotate web + Android session Replay stays 0%
(secrets app; error Replay on).  BotFleet Windows has no Sentry SDK
(desktop is not a second project).  Deno has no native profiler equivalent
to `@sentry/profiling-node`.

Prefer Sentry over Datadog for exceptions, replay, and traces.  Do not enable
Datadog Session Replay on the same page.

## Per-app matrix (present vs add)

Legend: **P** = already on main.  **Add** = this lane.  **Omit** = Designer.
**N/A** = no surface.

| App | Issues | Perf | Replay session | Replay error | Feedback | Crons | Metrics | Logs | Profiling | Releases | Alerts | Seer | Mobile |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| ST | P | P 0.2 | **Add 10%** (was 0%; follow-up `grok/sentry-max-replay-10`) | P 100% | P | P `scheduler-tick` | P | P | P server; P iOS 0.1 | P | P org | RCA on; Autofix hold | P Cocoa; Android N/A |
| CT | P Deno | P 0.2 | **Add web 10%** (was 0%) | P 100% | P | P `withMonitor` | P count on warn | P | Deno native N/A; P iOS 0.1 | P SHA | P org (errors not on PD) | Autofix hold | P Cocoa; iOS session 0% |
| UM | P | P 0.2 | P 10% | P 100% | P | P scheduler | P | P | P server | P | P org | Autofix hold | no Cocoa |
| DD | P | P 0.2 | P 10% | P 100% | P | CI reporter P | hop spans P | P | P iOS 0.1; **Add** Android 0.1 | P | P org | Autofix hold | P Cocoa; **Add** Android Replay |
| BF | P | P 0.2 | P 10% | P 100% | P | CI reporter P | gen_ai P | P | P Node session | P | P org | **Add Autofix** (`always`) | P Cocoa; Windows N/A |
| AR | P | P 0.2 | P 0% (secrets) | P 100% | P | P rotation | P | P | P iOS 0.1; **Add** Android 0.1 | P | P org | Autofix hold | P Cocoa; **Add** Android error Replay |
| CL | P | P 0.2 | P 10% | P 100% | P | N/A | P `logo.match` | P | P iOS 0.1; **Add** Android 0.1 | P | P org | Autofix hold | P Cocoa; **Add** Android Replay |
| PS | **Omit** | — | — | — | — | — | — | — | — | — | — | — | — |
| CTS | **Omit** | — | — | — | — | — | — | — | — | — | — | — | — |
| OPS | **Omit** | — | — | — | — | — | — | — | — | — | — | — | — |
| fleet-infra | P | N/A | N/A | N/A | N/A | P host/CI | N/A | P | N/A | P | P org | Autofix hold | N/A |

## Kill switches

| Flag | Default | Effect |
|---|---|---|
| `NEXT_PUBLIC_SENTRY_REPLAY_ENABLED` / `VITE_SENTRY_REPLAY_ENABLED` / `SENTRY_BROWSER_ENABLED` | on | `false`/`0`/`off`/`no` disables Replay |
| `*_REPLAY_SESSION_SAMPLE_RATE` | ST/CT/UM/DD/BF/CL web **0.1**; AR web **0**; CT iOS **0** | Override without a code change |
| `*_REPLAY_ERROR_SAMPLE_RATE` | **1.0** | Error Replay |
| `NEXT_PUBLIC_SENTRY_FEEDBACK_ENABLED` / `VITE_SENTRY_FEEDBACK_ENABLED` / `SENTRY_FEEDBACK_ENABLED` | **on** | `false` hides the widget |
| `SENTRY_TRACES_SAMPLE_RATE` | **0.2** | Performance |
| `SENTRY_PROFILE_SESSION_SAMPLE_RATE` / iOS/Android `profilesSampleRate` | **1** server session / **0.1** mobile | `0` disables Profiling |
| DSN unset | off | Entire SDK inert |

## What this unit does not do

- No Coolify, mint, merge, `--force-ship`, extra-ship, CloudAgent, Composer, on-demand.
- No Personal-Site / CTS / fleet-ops Sentry project.
- No Seer Autofix on apps other than BotFleet.
- AFC is not a product Sentry target.

## App PRs

One PR per app on `grok/sentry-max-features` (ST Replay-10 follow-up on
`grok/sentry-max-replay-10`), plus this AFC docs PR.

## Verification

- Repo grep: Personal-Site / CTS / fleet-ops still called out as no project.
- ST follow-up: session sample default `"0.1"`, error Replay on unless falsy flag.
- CT web: `clampRate(..., 0.1)`; iOS session Replay remains 0.
- BF project: `autofixAutomationTuning=always`.
- Android: DD/AR/CL native init includes Replay + profiling.
