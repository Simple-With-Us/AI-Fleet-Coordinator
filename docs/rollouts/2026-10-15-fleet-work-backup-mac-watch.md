# 2026-10-15 — fleet-work-backup: mac-process-watch + Infisical inventory (OPS-907)

## Context

Linear OPS-907 tracks the fleet-work-backup job (restic on B2 for unpushed git work and
`~/apps` files).  The implementation merged in AFC #454, #459, #460 and #482.  OPS-909 was
the #459 review-fix effort row;  it overlaps OPS-907 and is code-complete on `main`.

What remained in the coordinator repo was wiring so a live install stays on the Mac process
watch list and the Infisical inventory documents the four synced keys.

## Changes

- `scripts/mac-process-watch.sh`:  add `com.jay.fleet-work-backup` to `expect_scheduled` and
  `~/apps/fleet-work-backup/fleet_work_backup.py` to `expect_files` (same pattern as
  `com.jay.fleet-gdrive-backup`).
- `docs/MAC-LOCAL-PROCESSES.md`:  not edited in this PR (the row still names the
  legacy shared bucket key;  refreshing it trips the commit secret scan on the
  owner-confirmed bucket placeholder).  Update the live `~/apps/MAC-LOCAL-PROCESSES.md`
  and Apple Note at install time per README step 9.
- `INFISICAL.md`:  inventory table for the fleet-work-backup keys synced by
  `infisical-secrets-sync.sh`.

## Not in this PR (owner-run, OPS-907)

Live install on Jay's Mac:  scoped B2 key, Infisical values, `restic init`, recovery key,
LaunchAgent bootstrap, Removable Volumes grant, first dry run and `allow.txt` review.  See
`scripts/fleet-work-backup/README.md` install section.

## Verification

```bash
grep -q 'com.jay.fleet-work-backup com.jay.fleet-work-backup.plist' scripts/mac-process-watch.sh
grep -q fleet-work-backup/fleet_work_backup.py scripts/mac-process-watch.sh
bash scripts/test-infisical-secrets-sync.sh
python3 scripts/fleet-work-backup/test_fleet_work_backup.py
bash scripts/test-mac-process-watch-dump.sh
```
