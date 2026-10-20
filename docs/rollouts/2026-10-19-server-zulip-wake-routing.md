# Server Zulip Wake Routing

AFC-411 picks up Claude handoff `4237229e-5f06-4aa5-8329-f43051bfce93`.  PR #722 adds Linux systemd user-service install/uninstall/status, retains launchd on macOS, and documents the interactive partition and authentication prerequisites.

## Verified

- 117 focused tests passed on macOS: `cd scripts && python3 -m unittest agent_sync.tests.test_router agent_sync.tests.test_attach agent_sync.tests.test_live agent_sync.tests.test_server_session_routing agent_sync.tests.test_systemd agent_sync.tests.test_listener_install`.
- 26 focused Linux tests passed; `systemd-analyze --user verify` accepted ordinary and escaped unit paths.
- Two new fake-Zulip integration cases prove session A receives its topic mention/reply while session B receives no live item, even when B reads the shared CLI inbox before or after routing.
- The existing server Claude lane guard passed 22 cases after plugin installation; `AGENT_SEAT=CLAUDE` and the PreToolUse guard remain present.
- `agent-sync@afc` 1.0.0 is installed and enabled from the existing fleet-cli runtime marketplace.
- The host user service is installed, validated and inactive; its private sample config enables no seats.  No persistent credential file was created, and no active chat was restarted.

The Linux fast-import check was contaminated by the host Datadog sitecustomize in its subprocess; the same complete focused suite passed on macOS.  This is separate from listener behavior.

## Remaining Gates

Current-head Codex review and required CI precede merging.  The server wrapper still uses its existing fleet-cli release; verify the merged module reaches that runtime before calling the wrapper installer.

`fleet-secrets doctor` reports no valid server session login or automation environment pair.  The original handoff requires owner approval for a persistent scoped Infisical machine identity.  The concrete service environment path is `/root/.agent-sync/listener.env`, mode 600, carrying the credential-provider variables rather than Zulip key values.  Restrict the identity to required CLAUDE bot reads; preserve other seats.

After approval and authentication, configure only CLAUDE with `creds = "infisical"` and `wake = "inbox"`, pin the owner, and verify two real interactive sessions in isolated scratch state with a sandbox topic.  Confirm A wakes, B remains idle, and CLI inbox reads do not affect A.  Enable `rewake_verified_cli` in live state only after that proof.  Headless wakes and Desktop verification remain separately gated.

The owner also suggested per-platform chat letters.  Proposed human labels are `Claude A`, `Claude B`, `Codex A`, and so on, allocated atomically and bound to full session IDs.  Wrap after Z using an unused letter so active sessions never collide.  This proposal is not implemented by PR #722.
