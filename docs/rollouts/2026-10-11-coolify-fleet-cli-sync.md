# Coolify fleet CLI sync (OPS-933)

CODEX on the existing Coolify host has Codex installed but lacks the fleet CLIs and local MCP registration.  This change distributes only published runtime code from the Mac managed checkout and reuses the existing two-minute updater and SSH authorization.

## Changes

- `scripts/fleet-cli-release.py`: bounded manifest validation, isolated smoke checks, immutable releases, atomic activation, configuration-preserving initial installation, and pause/rollback controls.
- `scripts/fleet-cli-sync.py`: published-commit and origin validation, Git blob extraction, bounded strict SSH transport, verified receipts, and durable retry state.
- `scripts/agent-sync-runtime-sync.sh`: an opt-in, time-bounded hook inside the existing updater lock.
- Focused offline test suites, CI coverage, and the operator protocol in `docs/protocols/coolify-fleet-cli-sync.md`.

## Verification

- Receiver suite: 18 tests passed, followed by a passing focused test for fixed, sanitized timeout-versus-launch diagnostics.  Wrapper identity checks passed after adding isolated interpreter flags.
- Pusher suite: 20 tests passed before the final SSH interpreter and check-timestamp changes; all 8 affected transport tests then passed with the final hardening and check timestamps.
- Actual-runtime integration installs the real allowlist into a disposable home and checks CLI behavior, missing-auth refusal, and the real MCP dispatcher/catalogue without an authenticated tools object.  Generated deployment-wrapper execution is a Linux-only test and is mandatory in Ubuntu CI; Mac runs retain static isolation-flag checks.
- An initial real-runtime smoke failed under heavy Mac resource pressure.  Instrumented reruns passed all four cases at the original 12-second bounds; no timeout was increased.  Diagnostics now identify the failing case and distinguish timeout from a process-start error without revealing captured output.
- Actual-runtime run on Mac: 6 tests, 5 passed and 1 explicit Linux-wrapper execution skip.  The helper copied into the lane was checked for byte equality and valid syntax.
- Publication and live installation are separate gates.  This source rollout records no completed deployment yet.

## Boundaries and follow-up

No credential, token cache, chat state, handbook, user configuration, or untracked file enters a release bundle.  No new service or listener is installed, no active Codex session is restarted, and wake ownership is unchanged.  Existing CODEX and JET app identities remain separate.

Server-local CODEX Linear and Zulip credential files were absent in the assessment.  Their secure provisioning and any subsequent authenticated smoke checks require a separate approved handoff.  Cross-host real-chat routing, verified owner authority, model availability and duplicate-safe creation are a dedicated follow-up, not part of this installer.
