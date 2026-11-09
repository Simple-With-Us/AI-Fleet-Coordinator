# 2026-08-20 — Monet fleet-skills pack (Desktop + this repo)

## Summary

Rewrote the Claude.app fleet-ops skill pack for the **MONET** account.  The
July 13 Desktop copies were Socratic.Trade-only and had drifted from live
topology and standing policy.

Operator upload folder: `~/Desktop/fleet-skills` (folders + zips).
Git copy: `docs/fleet-skills/` in this repo.

## Why

Monet's app skill library is account-scoped.  The old five skills taught
retired Coolify UUID `<OLD_ST_COOLIFY_APP_UUID>`, retired box IPs
(`<HETZNER_IP_RETIRED>`, `<ORACLE_IP_RETIRED>`), `COOLIFY_API_TOKEN` on the command
line, ST-only health, and no THE BOARD / Apple Notes / secret grep trap /
two-space copy / iOS loop.  After the 2026-08-07 Hetzner cutover and the
2026-08-19 board-first ruling, those instructions were actively wrong.

## Skills

Original five, fleet-wide:

- `land-lane` — Monet worktree, optional `land.sh`, always-commit
- `unstick-pr` — phantom vs real merge-tree, any repo
- `codex-triage` — all review bots, not only Codex
- `pickup-seat` — board + all live effort logs
- `deploy-verify` — current UUIDs/IPs, Vercel, PS non-auto-deploy

New:

- `session-start`, `board-ops`, `closeout`
- `secret-handoff`, `apple-notes`, `owner-copy`, `ios-ship`

## Files

- `docs/fleet-skills/**` (12 `SKILL.md` + zips + README)
- `docs/ONBOARDING-NEW-AGENT.md` (Monet upload step)
- `README.md` (pointer)
- `STATUS.md`
- Desktop mirror `~/Desktop/fleet-skills` (not git)

## Verification

Docs-only.  Spot-checked Coolify UUIDs against `~/apps/COOLIFY.md` (Hetzner
table).  Zip layout remains `<skill>/SKILL.md`.  Did not upload to Claude.app
from this seat — that is a manual Monet-login step.

## Follow-ups

- Owner: upload on the MONET login (README in the pack).
- Optional: same pack on the CLAUDE login (swap tag/prefix).
- ST `.claude/skills/` still holds the old ST-flavored CLI copies; leave
  them unless a ST lane wants the fleet pack there too.

## Addendum (same day)

Tightened four skills after a live per-app land/deploy pass: per-repo
verify commands, do not run coordinator `land.sh`, ST health UA, UM
`/api/ready?strict=1`, DealDex bundle `online.dealdex` / no TF until ASC
record, Monet standing lanes besides `trading-monet` usually need creating.
