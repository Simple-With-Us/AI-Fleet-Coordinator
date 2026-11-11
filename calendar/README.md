# Fleet calendars + daily digest

Generated artifacts (do not hand-edit ICS; change the scripts/workflows):

| File | What it is |
|------|------------|
| [`agent-activity.ics`](./agent-activity.ics) | Timed VEVENTs per **commit** across verified public repos |
| [`daily-digest.ics`](./daily-digest.ics) | **All-day** VEVENT per day: merged PRs and issues opened/closed in public repos |

## Hosted site (GitHub Pages)

After the `Fleet daily digest + calendars` workflow runs:

```text
https://simple-with-us.github.io/AI-Fleet-Coordinator/
https://simple-with-us.github.io/AI-Fleet-Coordinator/digest.md
https://simple-with-us.github.io/AI-Fleet-Coordinator/calendar/daily-digest.ics
https://simple-with-us.github.io/AI-Fleet-Coordinator/calendar/agent-activity.ics
```

Raw-from-main mirrors (always available even before Pages is enabled):

```text
https://raw.githubusercontent.com/Simple-With-Us/AI-Fleet-Coordinator/main/calendar/agent-activity.ics
https://raw.githubusercontent.com/Simple-With-Us/AI-Fleet-Coordinator/main/calendar/daily-digest.ics
https://raw.githubusercontent.com/Simple-With-Us/AI-Fleet-Coordinator/main/site/digest.md
```

## Subscribe

**Daily outline (recommended for “what shipped today”):** paste the
`daily-digest.ics` HTTPS URL into Apple Calendar → Add Subscription Calendar,
or Google Calendar → From URL.

**Per-commit activity (busy day view):** use `agent-activity.ics`.

## Rebuild

```bash
export GITHUB_TOKEN="$(gh auth token)"
python3 scripts/build-agent-calendar.py
python3 scripts/build-fleet-daily-digest.py
```

Workflow: `.github/workflows/fleet-activity-site.yml` (every 6h + on script changes).
