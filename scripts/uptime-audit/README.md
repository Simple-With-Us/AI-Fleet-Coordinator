# Uptime Coverage Audit

Read-only check that every reviewed public fleet host has a Sentry uptime monitor, and that monitors do not point at hosts outside that list.

The inventory is `expected-hosts.yaml`.  Certain rows are documented health URLs.  Uncertain rows are hosts or paths this repo does not fully establish.  Excluded rows are retired, private, or not production, so a monitor there is reported on its own and is not called a retired host.

## Run

From this directory, with `SENTRY_AUTH_TOKEN` set in the environment:

```bash
python3 -m uptime_audit --json report.json --markdown report.md
```

The tool sends `GET` to the org uptime list on `https://us.sentry.io`.  It does not create, edit, or delete monitors.

Exit 0 when the report is written.  `--fail-on-uncovered` exits 1 when a certain health URL has no active monitor.  Uncertain rows do not fail that flag.  A missing token or an API error exits 2.

Tests use fixtures and do not call Sentry:

```bash
python3 -m unittest discover -s tests -t . -v
```

## Workflow

`.github/workflows/uptime-audit.yml` runs weekly and on `workflow_dispatch`.  If `SENTRY_AUTH_TOKEN` is not configured, the job exits 0 and posts a notice.
