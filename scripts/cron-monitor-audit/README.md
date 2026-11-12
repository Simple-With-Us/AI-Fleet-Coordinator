# Cron Monitor Audit

Read-only list of every Sentry cron monitor in org `simple-with-us`, matched to the fleet code that sends its check-ins.

Each monitor is one of four sorts.  Matched and healthy means a sender exists and the latest check-in is ok.  Matched but failing means a sender exists and the latest check-in is a real failure.  Orphan means no sender was found with enough confidence to propose a disable.  Uncertain means the scan could not decide, or a sender exists while the monitor is disabled.

The tool does not create, edit, or delete monitors.

## Run

From this directory:

```bash
python3 -m cron_monitor_audit --json report.json --markdown report.md
```

It sends `GET` to `https://us.sentry.io/api/0/organizations/simple-with-us/monitors/`.  The token is `SENTRY_AUTH_TOKEN_FULLSCOPE` (alerts:read or org:read).  `SENTRY_AUTH_TOKEN` returns HTTP 403 on that endpoint and is not accepted.  A missing token exits with status 2 and an HTTP 403 exits with status 3 (the token cannot list monitors).  A written report exits cleanly, including when orphans exist.

Fleet repos come from `fleet-apps.json`.  This checkout's workflows and scripts are scanned locally.  Other repos are read with `GET` from the GitHub API.  A token that returns 401 is dropped and the rest of the run stays anonymous.  Code search runs only when a token that can call `GET /search/code` is still present.  Without it, monitors that are not produced by a known `ci-<identity>-*` schedule stay uncertain.

Tests use fixtures and do not call Sentry or GitHub:

```bash
python3 -m unittest discover -s tests -t . -v
```

## Workflow

`.github/workflows/cron-monitor-audit.yml` runs weekly on Monday at 15:17 UTC, on `workflow_dispatch`, and on pull requests that touch this directory.  If `SENTRY_AUTH_TOKEN_FULLSCOPE` is not a repository secret, the job exits 0 and posts a notice naming that secret.  The job writes the markdown report to the GitHub Actions step summary.
