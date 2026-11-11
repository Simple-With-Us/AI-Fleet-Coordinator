# Codex Delivery Behind The Server Wake Router

Refs AFC-694 and AFC-305.  The shared server router owns capture, target selection, the durable wake queue, retry ordering and final receipts.  Codex supplies a one-shot Mac delivery function using the existing exact-thread native transport.  The obsolete Mac-router PR #1753 is closed; its branch preserves the earlier implementation and review evidence.

## Supported Target

The delivery adapter reaches an existing, loaded Codex Desktop chat on the Mac.  A read-only Codex store supplies candidate UUID/cwd/archive metadata; the native transport independently checks that exact UUID and cwd, writable ownership, active history and loaded state before enqueue.  Engine-owned and subagent chats cannot receive a wake.  Delivery into Codex sessions hosted on the server is unsupported by this adapter.

Unavailable native/store/receipt/workspace state is busy before send.  Archived, invalid or stale registry targets are refused before enqueue.  A possibly-enqueued send is terminal with an unknown result; it must never trigger an automatic replay.  The shared server queue remains responsible for bounded retries of proven pre-send unavailability.

## One-Shot Delivery Boundary

AA owns the fixed platform dispatch table under simple-chat wake-deliver, its SSH sender and forced-command wrapper.  The incoming platform value selects only a compiled-in known function; it never chooses a module, executable or path.  Codex's deliver_request function accepts a parsed request, while its bounded stdin wrapper can be used by the dispatcher.  This module starts no router, listener, port, durable queue or background job and needs no Zulip credential or routine signing key on the Mac.

The Codex wire request carries the server-selected identity and annotated wake:

```json
{
  "platform": "codex",
  "target": {
    "session_id": "11111111-1111-4111-8111-111111111111",
    "letter": "BC",
    "host": "mac",
    "generation": 3
  },
  "wake": {
    "wake_id": "wake-example",
    "seat": "CODEX",
    "message_id": 12345,
    "picked": "letter",
    "reason": "addressed-letter",
    "link_note": "the router-owned allowlisted selection note"
  }
}
```

This is a shape example, not a sendable fixture: link_note must be a real WR.link_note_ok-validated router note.  Reject oversized input before JSON parsing, malformed identity fields and non-boolean receipt evidence.  The Mac resolves the selected letter and checks exact session, host, positive integer generation and open state again immediately before native send.  The response echoes the exact wake id, session id and generation with the structured Delivery result; the server must verify that correlation before accepting it.

Remote candidate exclusion remains a separate integration item.  A server cannot read the Mac Codex store directly.  A delivery refusal prevents an engine/subagent enqueue, but it does not by itself let the server skip that candidate and select another interactive chat.  Do not claim that selection behavior until the shared remote exclusion contract and its tests land.

The shared router's SSH preflight must work with the forced-command key.  A wrapper owned by AA permits the exact no-op preflight and exact wake-deliver command, refusing every other original command.  The Mac helper performs delivery only; existing notes and archive services retain their separate owners.

## Receipts

The native receipt probe distinguishes four observations:

| State | Evidence | Delivery Result |
| --- | --- | --- |
| observed | Exact prompt is actual user input in the target transcript | sent, acknowledgement confirmed |
| queued | Native queue has the exact client id and text hash | sent, acknowledgement unknown |
| absent | Bounded queue/transcript probe succeeded without finding it | Eligible for one enqueue attempt |
| unavailable | Probe could not be completed | busy, no enqueue |

A new native enqueue is sent with ack unknown.  The server records delivered null until actual transcript arrival is proven.  Every prompt includes picked, reason, the validated link_note, and exact session/letter/generation.  Recency selection tells the chat the router is unsure; assigned_at fallback is assignment order rather than proof of recent activity.  The Operator never posts, asks or answers.

## End-To-End Acceptance

The runtime ships the read-only wake-acceptance helper:

```sh
simple-chat wake-acceptance \
  --wake-id wake-example --message-id 12345 \
  --session-id 11111111-1111-4111-8111-111111111111 --generation 3 \
  --source source.json --listener listener.jsonl \
  --receipt receipt.json --transcript rollout.jsonl
```

Use source-message JSON, server listener ledger JSONL, final router receipt JSON and the target transcript as read-only snapshots.  Passing requires a real supported mention, matching listener trigger and final done state, a real final router handoff to the expected session and picked_generation, and the same wake id/source message id in actual user input in that session.  Assistant quotations, test receipts, another transcript, HTTP 202 and queue acceptance alone cannot pass.  The helper never sends or replays a wake.

Exit 0 means prompt arrival in the expected chat, exit 3 means incomplete or rejected evidence, and exit 2 means invalid snapshot input.  Completion of the requested work is a separate observation.  Cover busy, duplicate, restart/failure, archive/reopen and letter reuse before declaring support functioning.

## Deployment Record

This source slice does not establish deployment.  AA owns shared dispatch, the server router and authoritative fleet-knobs settings; BB owns listener migration, credential binding and cursor handoff; CC owns hosted Muster reads and the migration checklist.  Do not move a seat or retire the existing Mac listener independently.  Register the installed Mac helper as On-demand in the process inventory and refresh its Notes copy when it is actually deployed.

Record exact reviewed/deployed heads, a real source mention, listener wake id, selected identity, final receipt and target-chat observation under AFC-694.  Claude DD owns authoritative Codex archive release, and Claude CC owns persisted title reconciliation.  No Mac Codex router service or port is installed by this change.
