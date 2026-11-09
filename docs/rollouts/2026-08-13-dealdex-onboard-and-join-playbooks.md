# 2026-08-13 — DealDex fleet join + onboard playbooks

## Context & Objective

Owner asked to (1) link `~/Code/DealDex` to GitHub `DealDex`, (2) register
DealDex everywhere the fleet expects an app, and (3) write a reusable
procedure for onboarding new agents and new apps.

## Changes Made

- `fleet-apps.json` — canonical app + seat inventory (includes DealDex / `DD`).
- `docs/ONBOARDING-NEW-APP.md` + `scripts/onboard-new-app.sh`.
- `docs/ONBOARDING-NEW-AGENT.md` + `scripts/onboard-new-agent.sh`.
- `scripts/check-fleet-registry.py` — fails if a registered app is missing
  from digest/calendar/protocol/Slack acronyms/live boards.
- Registries: `FLEET-PROTOCOL.md`, `EFFORT-LOG-PROTOCOL.md`, `FLEET-UI-COPY.md`,
  `TEMPLATE-AGENTS.md`, digest + calendar `DEFAULT_REPOS`, Slack comment tags,
  `agent-logos/app-dd.png`, `setup-agent-lanes.sh` (grok + monet).
- Live machine mirrors under `/Users/jay/apps` updated in the same session
  (protocol, FLEET-PROTOCOL, quickstart, UI copy, ios-fleet, DEALDEX board).

## Decisions & Trade-offs

- Acronym **DD**, Slack `repo: DealDex`, worktree prefix `dealdex-`.
- Digest color `#b45309` (amber) so DD is distinct from ST/CT/UM.
- iOS fleet row added for `me.grok.dealdex` but marked **do not ship** until
  an App Store Connect record exists.
- New apps start on hosted GitHub Actions until a Coolify runner is assigned.

## Verification State

```bash
python3 scripts/check-fleet-registry.py
```

## Next Steps & Blockers

- Land DealDex `grok/fleet-onboard` (repo files) and this branch.
- Owner: Infisical / Coolify / ASC when DealDex is ready to ship.
- Remaining seats create `~/apps/dealdex-<seat>` when they start work.
