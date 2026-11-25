# CI red streak report (AFC-737)

Read-only org scan: lists **active** workflows in each **non-archived** repository whose
latest **completed** runs on the **default branch** are a failing streak (ignoring
`skipped`, `cancelled`, and `neutral` conclusions).

## Run

```sh
export GITHUB_TOKEN=…   # or GH_TOKEN, or FLEET_READ_TOKEN for all fleet-apps.json repos
python3 scripts/ci-red-report/__main__.py \
  --org Simple-With-Us \
  --json-out ci-red-report.json \
  --markdown-out ci-red-report.md
```

When `FLEET_READ_TOKEN` is set, the default repo list is every `apps[].repo` in `fleet-apps.json`
(on each repo’s default branch).  Without it, pass `--repo` or rely on the scheduled workflow
(limiting the scan to this repository and recording a `visibility_note` that names the missing
secret).

Exit code is always `0` unless `--fail-if-streak-ge N` is set and any reported streak is
`>= N`.

Scheduled copy: `.github/workflows/ci-red-report.yml` (daily + `workflow_dispatch`).

## JSON schema (version 1)

Top-level object:

| Field | Type | Description |
| --- | --- | --- |
| `schema_version` | `1` | Stable schema id for consumers |
| `generated_at` | string | UTC ISO-8601 timestamp |
| `org` | string | GitHub org scanned |
| `repos_scanned` | int | Non-archived repos considered |
| `workflows_scanned` | int | Active workflows examined |
| `red_count` | int | Rows in `red_workflows` |
| `visibility_note` | string | Explains token scope limits when relevant |
| `red_workflows` | array | See row object below |
| `errors` | array of string | Non-fatal scan errors (repo skipped, etc.) |

Each `red_workflows[]` row:

| Field | Type | Description |
| --- | --- | --- |
| `repo` | string | Repository name (not full name) |
| `workflow_name` | string | Display name |
| `workflow_path` | string | Path in repo (e.g. `.github/workflows/ci.yml`) |
| `streak_length` | int | Consecutive failing counted runs on default branch |
| `first_red_run_id` | int | Oldest run id in the streak |
| `first_red_run_url` | string | HTML URL for oldest failing run |
| `first_red_run_created_at` | string | UTC ISO-8601 |
| `first_red_commit_sha` | string | Head SHA of oldest failing run |
| `latest_run_id` | int | Newest failing run in streak |
| `latest_run_url` | string | HTML URL for latest failing run |
| `latest_run_created_at` | string | UTC ISO-8601 |
| `latest_commit_sha` | string | Head SHA of latest failing run |
| `failing_job_name` | string | Job name on latest run (empty if unknown) |
| `failing_step_name` | string | Step name on latest run (empty if unknown) |

### Counted run conclusions

- **Failure streak**: `failure`, `timed_out`, `startup_failure`, `action_required`
- **Streak stop / green**: `success`
- **Ignored** (not counted): `skipped`, `cancelled`, `neutral`

### Phantom workflows

A workflow whose file carries no default-branch trigger (a `workflow_call`-only or
`pull_request`-only reusable pushed to `main`) still registers one red run per push with
zero jobs behind it — GitHub runs the file reference, not a real suite (the Oct 2025
autofix-reusable class).  When a workflow shows a streak, the scanner reads its file from
the default branch and skips rows whose `on:` triggers are all in the known
cannot-run-on-default-branch denylist (`workflow_call`, `pull_request`,
`pull_request_target`).  Denylist on purpose: a trigger this module does not know about
(`merge_group`, a future event) keeps its red row.  The check also fails open: an
unreadable or unparseable file keeps its red row and records the read failure under
`errors`, so the filter can only remove noise, never hide a failure.

## Tests

```sh
cd scripts/ci-red-report && python3 -m unittest discover -s tests -t . -v
```

Fixtures under `tests/fixtures/` record GitHub JSON; tests never call the live API.
