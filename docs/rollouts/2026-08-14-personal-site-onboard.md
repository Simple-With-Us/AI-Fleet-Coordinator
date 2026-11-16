# 2026-08-14 — Register Personal-Site in the fleet

## Context & Objective

Owner asked to onboard Personal-Site using ONBOARDING-NEW-APP / ONBOARDING-NEW-AGENT
and FLEET-PROTOCOL.

## Changes Made

- `fleet-apps.json` row: repo `Personal-Site`, acronym `PS`, board
  `BOTL-SITE-EFFORT-LOG.md`, prefix `personal`, Slack `Personal-Site`.
- Digest + calendar `DEFAULT_REPOS`, badge `PS`, color `#be123c`, legend chip.
- `FLEET-PROTOCOL.md` acronym + Slack `repo:` list (live `~/apps` kept in lockstep).
- `EFFORT-LOG-PROTOCOL.md` board registry + `AGENT-COORDINATION-QUICKSTART.md`.
- `FLEET-UI-COPY.md` binding list.
- `scripts/slack-sync.sh` canonical tag comment.
- Paired Personal-Site PR carries AGENTS, CI, effort-issues-sync, snapshot
  About-copy, and social-redirect docs.

## Verification State

```bash
python3 scripts/check-fleet-registry.py
```

## Next Steps & Blockers

- Merge this PR and the Personal-Site PR when CI is green.
- `FLEET_GITHUB_TOKEN` must include the private repo or digest/calendar skip it.
- Live personal Vercel project is not on the connected Vercel team (DealDex only).
