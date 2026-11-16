# 2026-09-13 — Register FX seat and HogHunter app

Board `22164b50`.  Branch `fx/registry-fx-hoghunter`.  Worktree `~/apps/fleet-fx-registry`.

## Why

`fleet-apps.json` is the seat inventory of record.  Skills already pin `AGENT_SEAT=FX` with `fx/` branches and `~/apps/<prefix>-fx` lanes, but the JSON had no FX row, so `check-fleet-registry.py` and digest/calendar scripts could not see the seat.  HogHunter is a real local-only Mac repo with CI and was also missing.

## What changed

- `fleet-apps.json` `seats[]`: FX (`notesName` Fx, `worktreeSuffix` fx, `branchPrefixes` `fx/`).  KIMI marked `retired: true`.
- `fleet-apps.json` `apps[]`: HogHunter (`HH`, local-only, no public host, `hasAppIcon` false).
- `FLEET-PROTOCOL.md` Agent Seat table and **Available (normal)** line (repo copy; live `~/apps/FLEET-PROTOCOL.md` updated in the same edit).
- Digest and calendar `DEFAULT_REPOS`, effort-log protocol, HogHunter live board file.
- `python3 scripts/check-fleet-registry.py` must pass.

## Verification

```bash
python3 scripts/check-fleet-registry.py
```

## Follow-ups

- Personal-Site `56fea494`: stop advertising Autorotate.Codes until DNS exists.
- HogHunter effort-log row is Completed only after this PR merges.
