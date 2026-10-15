# 2026-10-15 — fleet-health-recover Coolify container lookup (ST-9)

## Context & Objective

`fleet-health-recover@socratic-app` on the Hetzner host could not find the ST container when Coolify named it by application UUID instead of the literal `socratic-app` name.  That forced Coolify API restart loops during RTH (board ST-9 / `4ca246e5`).

## Changes Made

- `find_app_container` tries `coolify.resourceName` and `coolify.name`, then a **running-only** `name=${APP_UUID}` fallback (same pattern as `st-container-restart-monitor.sh`).
- Documented UUID fallback in `scripts/host/fleet-health-recover.d/socratic-app.env.example`.
- Added `--find-container` test hook (prints id, no restart).

**Files:** `scripts/host/fleet-health-recover.sh`, `scripts/host/fleet-health-recover.d/socratic-app.env.example`, `docs/rollouts/2026-10-15-fleet-health-recover-container-lookup.md`, `STATUS.md`.

## Decisions & Trade-offs

- Do not `docker start` on UUID name alone for exited containers (congress-health-recover lesson); exited path stays label-based only.
- Host deploy is owner-operated: copy script to `/usr/local/bin/` and restart the systemd unit after merge.

## Verification State

```bash
bash -n scripts/host/fleet-health-recover.sh
bash scripts/host/test-fleet-health-body.sh
# log() -> stderr so $(find_app_container) stays a single id (PR #562 review)
```

## Next Steps & Blockers

- Owner: install updated script on fleet host and re-enable `fleet-health-recover@socratic-app` once verified with `APP_UUID` set.

## Zero-Code Findings

None.
