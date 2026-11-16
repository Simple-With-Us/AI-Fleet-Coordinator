# 2026-08-14 — Register GROK-BUILD as a standing seat

## Context & Objective

Grok Build TUI is a separate identity from Mac Grok.  DealDex already
listed it in `AGENTS.md` (`grok-build/` + `/workspace`) but
`fleet-apps.json` had no seat row, so `onboard-new-agent.sh` and the
inventory treated it as missing.

Owner asked this session to onboard via ONBOARDING-NEW-APP.md.  The app
side was already done (DealDex PR #1 + coordinator PR #23).  This change
is the agent-seat half.

## Changes Made

- `fleet-apps.json` seats: `GROK-BUILD` / Notes `Grok Build` / suffix
  `grok-build` / prefix `grok-build/`.
- Created `~/apps/dealdex-grok-build` from DealDex `origin/main`.
- Did **not** edit `FLEET-PROTOCOL.md`, `TEMPLATE-AGENTS.md`, or the
  ONBOARDING docs — GROK `->FLEET` keepout this turn.  Asked that seat
  to add the Agent Seat table row.

## Verification

```bash
python3 scripts/check-fleet-registry.py
```

Checker is app-scoped; seat append does not change its result.

## Next

- Mac Grok: add GROK-BUILD to the FLEET-PROTOCOL Agent Seat table and
  Available list.
- DealDex PR `grok-build/fleet-setup` for STATUS / effort / rollout.
