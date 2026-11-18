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

## The route from the server

The router on the server has no Codex Desktop, so a delivery that stayed there would answer
every wake with a store it does not have.  The seat's platform therefore has two halves, and
the platform table names both.

- `router_platform` is the server's platform for seat CODEX.  A chat on the Mac goes over the
  one fixed ssh route in `claude_wake.deliver_remote`, exactly as the other Mac-hosted platforms
  do.  A chat on the router's own host is refused, because that host has no Codex Desktop
  either, and any other host is refused too.
- `deliver_request` is the Mac-side handler that `simple-chat wake-deliver` dispatches to.  It
  re-checks the request, re-reads the store, and hands the wake to the native transport.

The adapter's `local_host` defaults to `server`, never to `mac`:  a host that has not named
itself does not claim to be the Mac, so a delivery cannot land in a local store the host does
not have.

## The pinned owner

The authority to enqueue a prompt into a Codex thread is granted only when the target's own
creator is the account the deployment pins.  The Codex store carries the authenticated account
of every thread in its `creator_user_id` column, so the target's owner is read from the
thread's own row, and the deployment pins that account for this seat in the one local file
the service reads, `~/.local/state/wake-router/config.json`:

    { "pinned_profile": "<the Codex profile this seat may read>", "pinned_owner_account": "<the account this seat may wake for>" }

An account id is private inventory, so none is committed:  the host writes the pin and the
runtime reads it.  The delivery compares the thread's own creator with the pinned account
before anything is enqueued, and refuses:

- `no-pinned-owner` when the deployment pins no owner account at all,
- `owner-unreadable` when the pin file cannot be read, or when the thread's own creator cannot
  be read,
- `owner-mismatch` when the thread's creator and the pinned account differ.

The profile check still runs, and it is its own code:  the account says whose chats may be
woken, and the profile says which store this seat may read them from.  A thread in another
profile is refused (`profile-mismatch`), a delivery with no profile pin is refused
(`profile-unpinned`), and a store that cannot be read is refused (`profile-unreadable`).
Every one of these refusals happens before the enqueue, so each one is a `NEVER_RAN` code and
its receipt says `delivered: false`.

The two pins are host facts, so they live in the shared local file with the other host
facts, and the delivery reads them from there:  one file, one place the deployment writes
them, and the same file the service reads.  The file is read once per change of that file,
so a delivery attempt never costs a parse of the deployment's configuration.

## The cutover

The cutover was done in this order, and the sample
`scripts/simple_tools/server/listener.toml` now routes CODEX at the one router with the one
bearer the router checks.  A seat moves to the router only after the replacement has been
reviewed, is live, and has been verified with a real wake, and the working legacy binding
is kept until all three of those hold.

The three gates, and what each of them answered on the day of the cutover.

- Reviewed:  the platform module landed on main in pull request #2284 (squash commit
  `e4cde609b`) after Kody's code review, and every finding on it was answered, fixed and
  resolved before the merge.
- Live:  the router's release on the server carries `codex_wake.py`, `/health/CODEX`
  answers `healthy` with `delivery` on, and CODEX is in `WAKE_ROUTER_DELIVERY_SEATS`.
- Verified with a real wake:  a canary wake posted to the router's front with the
  router's own bearer was picked by its letter, and the seat's own path answered it over
  the ssh route (`route: ssh:codex-desktop`).

The legacy binding that the sample's change removed was the seat's inbox capture on the
server, and it had delivered nothing:  the server's wake ledger holds no wake for the seat at all, so
nothing that worked was removed with it.  The live block is moved to the sample's block by the
apply, and that apply is the cutover:  it writes the sample's block for every seat it manages,
this seat and ZC among them, so the first apply after the image carries the new sample moves
both seats to the router whatever the operator meant to move.  The three gates above are
therefore checked before that apply runs and not after it, and the seat stays on the binding
it has for as long as no apply has moved it.

The steps, in this order.

1.  Write the pinned profile and the pinned owner account into the local settings file, as
    above.  Before the live listener's block is changed, mint `CODEX_ROUTINE_URL` in
    AFC prod `/zulip` with the value `https://<server tailnet host>:8795/wake/CODEX`, the
    router's front for this seat.  The container is restarted after step 3 has changed
    the block, so the new name and the new binding are in force together:  the environment
    is read once at the container's start, so a reload alone would leave the routine
    without its URL and every wake routine-unready.
2.  Deploy the router release that carries `codex_wake.py` to the server, and prove the seat
    answers there:  `curl -s https://<server tailnet host>:8795/health/CODEX` answers
    `healthy`, and the seat is in `WAKE_ROUTER_DELIVERY_SEATS`.
3.  Only then deploy the image that carries the new sample and run
    `apply_live_config.py --apply`, which is the step that performs the cutover.  The apply
    reads the sample from the image's own copy of it, so an image that still carries the old
    sample writes the old block, and the image comes first.  The sample declares the binding
    this seat takes:  `wake = "http"` with
    `routine = { url_env = "CODEX_ROUTINE_URL", key_env = "WAKE_ROUTER_KEY", method = "POST", auth = "bearer", header = "Authorization", timeout_seconds = 15 }`,
    and the apply writes the sample's block into the live file for every seat it manages, this
    seat and ZC among them, so one apply moves both of them to the router.  A hand edit of the
    live file is not the way to do it:  the next apply writes the sample's block over it.  The
    container is restarted with the apply:  the restart carries the name minted in step 1 and
    the new binding together, and a reload alone would keep the old value.
4.  Prove it end to end:  mention @CODEX in a test topic, and the router's receipt
    (`/receipt/CODEX/<wake id>`) shows the picked session and a delivered or an explicitly
    unsettled outcome.

What the cutover proved, on the day it was done.  The canary wake was picked by its
letter, and the delivery took the ssh route to the Mac (`route: ssh:codex-desktop`), where
the Mac-side handler answered it.  What the delivery proves next depends on the target:
a chat the store does not show as an
interactive chat is refused with `not-interactive-chat` before anything is enqueued, and that
refusal is the platform's own contract, not a routing failure.  A chat that the Codex
Desktop app has loaded is the only target that can take the wake, so the delivery proof
needs that app running with the chat open.

## The bounded JSON

Every reader of untrusted JSON in the fleet now shares one bound:  `wake_router.bounded_json`
holds a value to sixteen levels of depth and a thousand nodes, and the wake-acceptance tool
applies it to the snapshot it parses and to every JSONL row it reads.  A byte cap alone does
not bound the work the parser and the walker do.

## Deployment Record

This source slice does not establish deployment.  AA owns shared dispatch, the server router and authoritative fleet-knobs settings; BB owns listener migration, credential binding and cursor handoff; CC owns hosted Muster reads and the migration checklist.  Do not move a seat or retire the existing Mac listener independently.  Register the installed Mac helper as On-demand in the process inventory and refresh its Notes copy when it is actually deployed.

Record exact reviewed/deployed heads, a real source mention, listener wake id, selected identity, final receipt and target-chat observation under AFC-694.  Claude DD owns authoritative Codex archive release, and Claude CC owns persisted title reconciliation.  No Mac Codex router service or port is installed by this change.
