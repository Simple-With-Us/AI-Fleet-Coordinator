# Codex Delivery On The Shared Wake Router

Refs AFC-694 and AFC-305.  The shared routing and durable queue contract is `simple_tools/wake_router.py` (AFC PR #1743).  Codex supplies `deliver(target, wake)` through `simple_tools/codex_wake.py` and uses the existing exact-thread native queue transport.

## Supported Target

The initial adapter delivers to an existing, loaded Codex Desktop chat on the Mac.  The target UUID, original working directory, archive flag and interactive ownership come from the read-only Codex store; the native transport verifies the same thread through the app-server before enqueueing.  Engine-owned chats and sub-agent threads are excluded.  Server targets return `unreachable` and remain queued until their age limit; they have no verified native delivery adapter in this slice.

A missing store, unavailable receipt probe, unloaded chat or unavailable workspace returns `busy`.  An archived or invalid target is refused.  A known native rejection can retry, while any uncertain outcome after enqueue is final `refused`, because the turn may already have started.

## Receipts

The native `receipt_state` distinguishes four observations:

| State | Evidence | Delivery Result |
| --- | --- | --- |
| `observed` | The exact prompt appears as actual user input in the target transcript | `sent`, acknowledgement `confirmed` |
| `queued` | The native queue contains the exact client id and text hash | `sent`, acknowledgement `unknown` |
| `absent` | The bounded queue/transcript probe succeeded without finding it | Eligible for one enqueue attempt |
| `unavailable` | The probe could not be completed | `busy`, no enqueue |

A new native enqueue acknowledgement is `sent` with `ack = unknown`.  The shared router records the handoff with `delivered = null`; that is not proof the chat ran a turn.  Unknown native acknowledgements are never automatically replayed.

## Service Entry Point

The runtime ships these two commands:

```sh
simple-chat codex-wake-router serve --config /path/to/codex-wake-config.json
simple-chat wake-acceptance --help
```

The router binds to loopback by default.  Its existing shared defaults leave delivery disabled.  The local state is under `~/.local/state/codex-wake`; `CODEX_WAKE_STATE` chooses its parent directory, `CODEX_WAKE_CONFIG` selects the config file and `CODEX_WAKE_KEY_FILE` selects the protected routine-key file.  The routine signing key is supplied through the fleet's scoped secret delivery path and must never be put in argv, logs or source.  Live activation still needs the reviewed runtime, the listener routine binding and the process inventory entry.

The service uses the shared registry API environment and router.  Selection tries letters, a unique session-id fragment, Linear issues, topic words and then recency.  The optional consultant is off by default; its absence falls through to recency.  The Operator sends no questions or replies.

Every delivered prompt carries the router's `picked`, `reason` and validated `link_note`, together with the exact session, letter and generation.  A recency note tells the receiving chat that the router is unsure the wake is meant for it.  Recency uses registry `last_seen_at`, falling back to `assigned_at` while the activity field is absent; that fallback reflects assignment order rather than actual recent activity.

## End-To-End Acceptance

Export source-message JSON, listener ledger JSONL, the final router receipt JSON and the target transcript as read-only snapshots.  Then correlate the intended identity explicitly:

```sh
simple-chat wake-acceptance \
  --wake-id w-example --message-id 12345 \
  --session-id 11111111-1111-4111-8111-111111111111 --generation 3 \
  --source source.json --listener listener.jsonl \
  --receipt receipt.json --transcript rollout.jsonl
```

A proven result requires a real supported mention, the listener's matching source trigger and final `done` state, a real final router handoff to the expected session and `picked_generation`, and the same wake id plus source message id in actual user input in that session's transcript.  Assistant quotations, a different transcript, a test receipt and queue acceptance alone cannot pass.  The helper emits fixed result codes and identifiers, with no message body, and never sends or replays a wake.

Exit 0 means the input arrived in the expected chat.  Exit 3 means incomplete or rejected evidence, and exit 2 means an invalid snapshot.  This proves prompt arrival; completion of the original requested work is a separate observation.

## Activation Record

The file-only LaunchAgent template is `scripts/launchd/com.jay.codex-wake-router.plist`.  Render its `__HOME__` and `__RUNTIME__` placeholders for the target Mac, create protected state and log directories, provision the approved routine key and authoritative settings, and validate the rendered plist before installing it.  The template stays on loopback port 8795 and does not enable delivery.  Its exit timeout exceeds the shared router's default graceful shutdown wait.

The runtime sync helper preserves the existing listener restart and sends SIGTERM to an already-loaded Codex router when shared runtime code changes.  It never loads or starts an absent Codex job.  The router settles running deliveries and keeps its durable state; launchd's KeepAlive starts the next process with the updated runtime.  The live sync helper is a regular deployed copy outside the checkout, so installing the router also requires deploying the reviewed sync-helper update.

Before activation, register the new router as Always-on in the Mac process inventory and refresh its Background Jobs Master List note.  Confirm the exact installed runtime head, the updated sync-helper copy, scoped registry environment, approved Infisical settings and key delivery.  The listener binding uses `CODEX_ROUTINE_URL` and `CODEX_ROUTINE_KEY` in Fleet-OPS `/zulip`; the listener owner provisions them after this adapter lands.  Shared router settings still need the authoritative fleet-knobs loader before activation; local config alone is insufficient for deployed knobs.

This source change does not itself establish activation.  Record the deployed head, listener routine, one real peer mention, final receipt and target-chat observation under AFC-694 before marking Codex delivery functioning.  Claude DD owns authoritative Codex archive release separately, and Claude CC owns registry-touch title reconciliation.
