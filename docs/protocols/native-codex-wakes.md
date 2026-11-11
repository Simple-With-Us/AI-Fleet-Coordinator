# Native Codex Wake Delivery

Operator is the existing Simple-Tools listener service.  Its headless Muse worker selects an existing chat, and its deterministic adapter validates and delivers a fixed reminder.  GitHub token issuance and the future letter registry are separate modules of that service; each agent retains its own identities.

## Native Queue Transport

The desktop Codex backend watches its durable queue for loaded threads.  The adapter uses the installed desktop CLI as an on-demand JSON-RPC client, calling only `initialize`, `thread/read`, `thread/queue/list` and `thread/queue/add`.  It never resumes a thread, starts a replacement session, changes a model or changes sandbox settings.  This child is not a second always-on Operator service.

Before offering a destination, the catalog requires the desktop Codex process to have the exact rollout file open.  Before sending, the adapter verifies the local host, UUID, original workspace, active local history path, loaded-thread proof and queue availability.  Archived, read-only, unloaded and mismatched destinations remain unavailable.

A correlated queue-add response must contain a submission ID and the requested client message ID.  This proves durable queue acceptance; it does not prove the chat consumed the message.  A timed-out or malformed acknowledgment is unknown and is never automatically replayed.  A well-formed correlated server rejection is held for explicit retry without blocking later destination traffic; the known capacity rejection is deferred automatically.  Recovery checks the exact client ID and message in the queue or the exact user message in the rollout; an assistant quoting the reminder is not a receipt.

The standard queue API supports shared storage across app-server processes.  The on-demand client does not need the desktop app-tools private pipe or a separate managed app-server daemon.

## Scoped Activation

The activation this section used to describe ran through the Muse inbox bridge (`[muse_bridge] enabled = true` with `[seat.CODEX] muse_bridge = true`).  The bridge was deleted on Nov 10, 2026, on the owner's instruction, so that route no longer exists:  both keys are now ignored with a warning that says they can be deleted.  The native queue transport above stays, and the listener's own chat nudges still use it.

## Implementation And Status

AFC-305 tracks native delivery; AFC-326 tracked the shared Muse bridge, deleted on Nov 10, 2026.  AFC-412 tracks the proposed cross-host letter registry.  Token work is coordinated with Echo under OPS-966.  These issue scopes share Operator terminology but have independent deployment evidence.

This document describes the adapter contract.  Live activation and end-to-end receipts belong in the dated rollout record.
