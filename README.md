<picture>
  <source media="(prefers-color-scheme: dark)" srcset="agent-logos/app-afc-dark.webp">
  <img src="agent-logos/app-afc.png" alt="Muster app icon" width="96" height="96">
</picture>

# AI Fleet Coordinator

AI Fleet Coordinator (AFC) is the shared toolkit behind [Simple With Us](https://simplewithus.com/)'s AI coding fleet.  It brings coordination, repository workflows, configuration checks, and public activity reporting together across coding platforms, the Mac, and Linux servers.

This repository also contains **[Muster](muster/README.md)**, the native macOS app for fleet administration.  Muster gives the owner a place to inspect fleet health, services, coding seats, work, and configuration drift, with explicit machine targets for supported actions.

[AFC Overview](https://simplewithus.com/ai-fleet-coordinator/) · [Muster](https://simplewithus.com/muster/) · [Public Activity Feed](https://simple-with-us.github.io/AI-Fleet-Coordinator/)

## What Lives Here

| Component | Purpose | Start Here |
| --- | --- | --- |
| Muster | Native SwiftUI macOS 14+ fleet administration, with Mac and server views | [App, Prerequisites, And Build Instructions](muster/README.md) |
| Fleet Tools | Shared CLI and MCP tools for coordination, work tracking, notes, recall, and artifacts | [Fleet Tools Manual](FLEET-PROTOCOL.md) · [Tool Guide](docs/protocols/simple-tools-guide.md) |
| GitHub Relay | Audited repository and pull request operations under each seat's approved identity | [Relay](git-relay/README.md) |
| Fleet Configuration | App and seat registries, configuration checks, and isolated task lanes | [App Registry](fleet-apps.json) · [Seat Registry](fleet-seats.json) · [Lane Map](docs/protocols/lane-map.md) |
| Activity Feed And Calendars | Public repository work, grouped by day, project, and coding platform | [Live Feed](https://simple-with-us.github.io/AI-Fleet-Coordinator/) · [Generator](scripts/build-fleet-daily-digest.py) |
| Onboarding And Skills | Shared procedures for joining the fleet and carrying work through review | [New App](docs/ONBOARDING-NEW-APP.md) · [New Seat](docs/ONBOARDING-NEW-AGENT.md) · [Skills](docs/fleet-skills/README-add-in-app.md) |

Muster administers the fleet.  Individual products, including [BotFleet](https://github.com/Simple-With-Us/BotFleet), keep their own product controls and remain separate applications.

## Coordination Today

- **Linear tracks work.**  Issues, claims, findings, and closeouts live in the [Simple With Us workspace](https://linear.app/simple-with-us).
- **Zulip carries conversations.**  Active work uses `#Work-In-Progress` topics in the [fleet realm](https://simplewithus.zulipchat.com/), with one issue and subject per topic.
- **One shared Operator serves the platforms.**  Session registration, assignment, routing, and sweeping use shared tools; platform differences are confined to the delivery each platform requires.
- **Task lanes isolate changes.**  Each task uses an owned branch and worktree, then a reviewed pull request.
- **Infisical owns secrets and shared settings.**  Consumers use scoped service tokens through approved routes; credentials and private deployment inventory stay out of public documentation and artifacts.

The current app and seat inventory lives in [fleet-apps.json](fleet-apps.json) and [fleet-seats.json](fleet-seats.json).  The registries include active entries, aliases, and historical entries; their current fields determine status.

## Public Activity Feed

The [activity feed](https://simple-with-us.github.io/AI-Fleet-Coordinator/) is a public view of repository work, including merged pull requests and issues opened or closed.  The daily digest and per-commit calendar refresh every six hours through the existing GitHub Actions publishing workflow.

The feed uses an explicit public repository allowlist and checks current GitHub visibility before fetching activity.  Private repositories and internal Linear work are excluded from the public output.

| Artifact | Link |
| --- | --- |
| Daily Activity | [HTML Feed](https://simple-with-us.github.io/AI-Fleet-Coordinator/) |
| Markdown Digest | [digest.md](https://simple-with-us.github.io/AI-Fleet-Coordinator/digest.md) |
| Daily Calendar | [daily-digest.ics](https://simple-with-us.github.io/AI-Fleet-Coordinator/calendar/daily-digest.ics) |
| Per-Commit Calendar | [agent-activity.ics](https://simple-with-us.github.io/AI-Fleet-Coordinator/calendar/agent-activity.ics) |

To subscribe, copy a calendar link into Apple Calendar's **Add Subscription Calendar**, Google Calendar's **From URL**, or Outlook's calendar subscription feature.  For Google Calendar, add the subscription on the web to have it appear in the mobile app.

Published snapshots live on the `published/fleet-activity` branch.  The generated files on `main` are fixtures or historical snapshots, so use the live feed or published branch for current activity.

## Working On This Repository

Read [AGENTS.md](AGENTS.md) before making changes.  It contains the current working rules; [FLEET-PROTOCOL.md](FLEET-PROTOCOL.md) is the generated Fleet Tools Manual, built from the Fleet-OPS handbook.

For Muster development, follow its [prerequisites and build instructions](muster/README.md#prerequisites).  Native macOS builds require a Mac; Linux can run the portable tools and their applicable checks.

Useful entry points:

- [Fleet Tooling And Capabilities](docs/protocols/simple-tools-guide.md)
- [App Onboarding](docs/ONBOARDING-NEW-APP.md)
- [Seat Onboarding](docs/ONBOARDING-NEW-AGENT.md)
- [Muster Panels And Action Boundaries](muster/README.md#every-panel)
- [Activity Feed Generator](scripts/build-fleet-daily-digest.py)

For a local activity rebuild, run `scripts/build-agent-calendar.py` and `scripts/build-fleet-daily-digest.py` using the approved GitHub credential route for your environment.  The outputs are `site/index.html`, `site/digest.md`, and the calendar files; `scripts/ics_utils.py` handles RFC 5545 line folding.

## License

[Apache-2.0](LICENSE)
