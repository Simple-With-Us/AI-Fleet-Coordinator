# iOS agent build-loop policy

## Context & Objective

Owner asked to wire the iOS agent lessons into the fleet, then ruled: do not stand up or fix Xcode MCP.  Agents must be allowed to run `xcodebuild` via bash without asking and without explaining missing MCP.

## What landed

- `FLEET-PROTOCOL.md` § iOS agent build loop (also copied to `~/apps/FLEET-PROTOCOL.md`)
- `TEMPLATE-AGENTS.md` + `docs/ONBOARDING-NEW-APP.md` checklist
- `scripts/block-xcode-project-writes.py` + `.claude` hook template
- Machine copy: `/Users/jay/apps/ios-fleet/block-xcode-project-writes.py` + README pointer
- Per-app follow-ups (separate PRs): ST `ios/CLAUDE.md`, CT `clients/ios/CLAUDE.md`, UM `ios/CLAUDE.md`, DD `native/ios/CLAUDE.md`

## Verify

```bash
python3 scripts/block-xcode-project-writes.py --self-test
```

## Next

Land the four app PRs.  No Xcode MCP work.
