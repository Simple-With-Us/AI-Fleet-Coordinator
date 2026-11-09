# 2026-09-18 — Backup fleet GitHub repositories Sentry cron margin

Sentry **FLEET-INFRA-CH** (`Cron failure: ci-AI-Fleet-Coordinator-backup-fleet-github-reposi`).
Branch `cursor/backup-repos-cron-margin`.

## What

`CHECKIN_MARGIN_OVERRIDES["Backup fleet GitHub repositories"] = 600` in
`scripts/sentry-ci-report.py`.  Cron and backup job unchanged.

## Why

Sentry expects a check-in at 07:00Z (`0 7 * * *`) with a 15-minute margin.
GitHub starts `backup-repos.yml` 4–6.5h late on `ubuntu-latest`, then the
job succeeds in ~50s.  Miss at 07:15Z; late OK ~11:05–13:35Z auto-resolves
until the next day.  Worst recent: 2026-09-14 13:34Z (~6h 34m).  All
retained scheduled runs are `success`.  No 2026-09-18 `schedule` run exists
yet at the 07:15Z miss.

Reporter already sends `in_progress` on `workflow_run` `requested`.  That
cannot cover a run GitHub has not created yet.

Same GitHub `schedule` delivery class as ST #3194 / FLEET-INFRA-C1, #3387 /
C3, #3389 / BY, #3390 / C0, Autorotate #219 / CD, UM #1491 / CF, CT #2501 /
23, and DealDex #328 / CG.

Do not copy 600 onto 30-min macos iOS-ship crons (FLEET-INFRA-CC / DA / CX).
Those drop ticks.

## After merge

Do not resolve FLEET-INFRA-CH on merge.  The 600-minute config only upserts
on the next scheduled check-in (~11:05–13:35Z).  Then ignore/resolve CH.

Do not `workflow_dispatch` the backup to verify.
