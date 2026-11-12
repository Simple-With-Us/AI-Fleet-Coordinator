# iOS Debug vs TestFlight (autonomous console)

## Context & Objective

Owner asked when to run an Xcode-installed Debug build instead of TestFlight, and to do that kind of debug as autonomously as possible (they will help if blocked).

## What landed

- `FLEET-PROTOCOL.md` § iOS agent build loop — four-path rule (simulator default, device logs-only, device Debug install, Xcode Run last resort).
- On-demand helper `~/apps/ios-fleet/ios-debug.sh` (tracked `scripts/ios-debug.sh`): simulator `simctl launch --console` + `log stream`; device `devicectl` launch/screenshot + `log collect`.
- `TEMPLATE-AGENTS.md`, `docs/ONBOARDING-NEW-AGENT.md`, `docs/fleet-skills/ios-ship/SKILL.md`, live `~/.grok/skills/ios-ship/SKILL.md`, `docs/MAC-LOCAL-PROCESSES.md` + live list.
- `~/apps/ios-fleet/README.md` usage.

`--target device --install-debug` replaces TestFlight for that bundle until the owner reinstalls.  `--logs-only` does not.

## Verify

```bash
bash /Users/jay/apps/ios-fleet/ios-debug.sh --help
bash /Users/jay/apps/ios-fleet/ios-debug.sh --list
bash /Users/jay/apps/ios-fleet/ios-debug.sh socratic --dry-run
bash /Users/jay/apps/ios-fleet/ios-debug.sh socratic --target device --logs-only --dry-run
```

## Next

Per-app `ios/CLAUDE.md` one-liners can point here on the next iOS touch.  Do not add a daemon.
