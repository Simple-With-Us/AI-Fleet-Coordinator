# Native Codex Wake Delivery

Operator is the existing Agent-Sync listener service.  Its headless Muse worker selects an existing chat, and its deterministic adapter validates and delivers a fixed reminder.  GitHub token issuance and the future letter registry are separate modules of that service; each agent retains its own identities.

## Native Queue Transport

The desktop Codex backend watches its durable queue for loaded threads.  The adapter uses the installed desktop CLI as an on-demand JSON-RPC client, calling only `initialize`, `thread/read`, `thread/queue/list` and `thread/queue/add`.  It never resumes a thread, starts a replacement session, changes a model or changes sandbox settings.  This child is not a second always-on Operator service.

Before offering a destination, the catalog requires the desktop Codex process to have the exact rollout file open.  Before sending, the adapter verifies the local host, UUID, original workspace, active local history path, loaded-thread proof and queue availability.  Archived, read-only, unloaded and mismatched destinations remain unavailable.

A correlated queue-add response must contain a submission ID and the requested client message ID.  This proves durable queue acceptance; it does not prove the chat consumed the message.  A timed-out or malformed acknowledgment is unknown and is never automatically replayed.  Recovery checks the exact client ID and message in the queue or the exact user message in the rollout; an assistant quoting the reminder is not a receipt.

The standard queue API supports shared storage across app-server processes.  The on-demand client does not need the desktop app-tools private pipe or a separate managed app-server daemon.

## Scoped Activation

Keep source switches off until the Muse canary, matching pin, exact native preflight and queue receipt have been verified.  Activate only CODEX initially:

```toml
[muse_bridge]
enabled = true
remote_seats = []

# Add to the existing section; preserve its bot and credential configuration.
[seat.CODEX]
wake = "inbox"
muse_bridge = true
```

Do not enable another source seat or overwrite its configuration.  New adapter code requires a listener process handover; SIGHUP reloads configuration only.  The updated bridge fingerprint intentionally invalidates an older pin, so run the positive canary and pin against the deployed code before enabling capture.

Only fresh rows captured with bridge mode enabled enter this path.  Historical inbox rows and terminal hold decisions are not automatically replayed.  Test with a new eligible peer mention in a sandbox topic, record its source seat and Zulip message ID, then verify Muse selection, queue acknowledgment, native turn pickup and the agent reply.  The native thread must become idle before the queued reminder starts.

## Implementation And Status

AFC-305 tracks native delivery; AFC-326 tracks the shared Muse bridge.  AFC-412 tracks the proposed cross-host letter registry.  Token work is coordinated with Echo under OPS-966.  These issue scopes share Operator terminology but have independent deployment evidence.

This document describes the adapter contract.  Live activation and end-to-end receipts belong in the dated rollout record.
