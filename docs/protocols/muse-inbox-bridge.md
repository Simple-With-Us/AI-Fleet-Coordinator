# Muse Inbox Bridge (AFC-326)

The bridge is **off by default**.  The existing CLAUDE Muse wake, Mac inbox capture, server cloud
inboxes, JET event wake, and Grok Bot HTTP routines continue unchanged until each source seat is
explicitly cut over.  Deploying this code alone does not change CLAUDE's Muse pin or listener behavior.

## Source And Decision Path

1. The listener that owns a seat classifies a DM or mention and writes its ordinary durable seat
   inbox row.  With both `[muse_bridge] enabled = true` and that seat's `muse_bridge = true`, it does
   not start its original wake adapter for that row.
   Only rows marked during an enabled bridge capture enter the new path.  Older inbox rows remain
   visible but are not replayed automatically, preventing a second wake for work already handled.
2. The Mac bridge reads Mac inboxes directly.  For server seats it uses the existing ssh path at
   the operator's own inventory target (`HOUSEKEEPER_SSH_ALIAS`) and the one fixed Coolify
   application ID to call `agent_sync.muse_bridge export` inside the listener container.  Export
   sends at most eight classified rows per seat, with content capped at
   6,000 characters.  It reports rotation gaps instead of advancing over unseen rows.  The Mac
   persists a cursor and a decision under `(source seat, Zulip message ID)` before delivery.
3. One pinned, tool-less Muse call chooses one offered ID or holds.  Its separate bridge contract
   allows a fixed `native-routine` resource for an already configured server HTTP wake.  The normal
   CLAUDE wake contract and pin are unchanged.  The Mac refreshes an existing chat before queuing a
   nudge and checks the same seat for a private DM, low risk for a peer, source pause, exact host,
   platform, workspace, and wake capability.  A model cannot choose a command, endpoint, host, bot
   key, or arbitrary role.  Unknown, stale, read-only, and ambiguous destinations are held.
4. The Mac returns a source-qualified decision through `agent_sync.muse_bridge submit` over the same
   SSH path.  The server reopens its own inbox row, checks the seat, message ID, sequence, pause,
   loop guard, and configured routine.  A native approval is claimed durably before its original
   `SeatRunner` invokes the original routine with that seat's credentials and existing budget gate.
   A crash after claim or an accepted routine without a delivery confirmation is uncertain and is
   not retried automatically.  A chat or hold decision is a
   durable selection receipt; it cannot invoke a server routine.  `chat_selected` records server
   acknowledgment, while the Mac chat-nudge queue holds the separate delivery receipt.  A failure
   between those writes retries the same source-qualified selection without another model call.
   A definite refusal for one message (stale, paused, loop guard, destination gone, or the server
   answering `{"refused": ...}`) is recorded on the Mac decision as `delivery: refused` and the
   cursor moves past it;  only a transport failure leaves the cursor in place for a retry.

The Mac holds no server bot or routine credentials.  Muse receives the source seat as trusted
metadata in a bounded prompt, while its subprocess has no launcher seat and no tools.  The server
does the original seat's write.  A source-qualified key prevents the same Zulip message addressed
to two bots from collapsing into one decision or chat nudge.

## Activation Gate

Configuration is local and remains off in the shipped sample.  On the Mac, set
`[muse_bridge] enabled = true` with `remote_seats = ["ECHO"]` for the first scope.  On the server,
set `[muse_bridge] enabled = true` and `[seat.ECHO] muse_bridge = true` only after the Mac pull,
decision receipt, and recovery behavior have been checked.  Keep all other per-seat bridge switches
false.  A seat's old `wake` value remains configured; bridge mode suppresses its direct wake only
for that seat.  Do not cut over a server source before its Mac consumer and SSH path are healthy.

The bridge has its own pin at `listener/muse-bridge-pin.json`.  The pin binds the real versioned Muse
binary, fixed argv, output schema, this bridge contract, policy version, model, and effort.  The
operator must run the bridge canary and inspect the result, then pin the fresh candidate:

```sh
cd scripts
python3 -m agent_sync.muse_bridge_mac probe
python3 -m agent_sync.muse_bridge_mac pin
python3 -m agent_sync.muse_bridge_mac status
```

These commands are **not** part of deployment and were not run for this change.  If the binary,
contract, or schema changes, the bridge holds all new events until a fresh canary is pinned.  The
ordinary CLAUDE pin remains independent.  Inbox rows and uncertain decisions stay durable for
operator review; do not manually advance a gap cursor without reconciling the missing messages.

The canary needs working headless Muse auth: the pinned `muse-bin-*` build must run
non-interactively from a LaunchAgent context.  If `probe` reports `missing meta credentials`,
refresh the Meta login (`muse login`) or provision `META_API_KEY` for the listener context, then
re-run `probe` before `pin`.  The resolver prefers the newest versioned `muse-bin-*` build on the
wake PATH so the pin always names a real build, never the `muse` launcher shim.

## Runtime Sequence

One addressed inbox row travels exactly one path.  Every arrow spans a
durable write, so a restart resumes instead of redelivering.

```text
seat listener (Mac or server)          Mac bridge worker            server listener
classifies DM/mention                  (muse-bridge-mac loop)       (native dispatch tick)
writes seat-inbox row
[muse_bridge] + wake_worthy
        |                                    |                              |
        |  export (<=8 rows/seat,            |                              |
        |  6,000 chars, gap on               |                              |
        |  rotation loss)                    |                              |
        |----------------------------------->|                              |
        |  Mac persists cursor +             |                              |
        |  (source seat, msg ID)             |                              |
        |  decision BEFORE delivery          |                              |
        |                                    | one pinned tool-less         |
        |                                    | Muse call: exact             |
        |                                    | resource ID or hold          |
        |                                    |                              |
        |  submit(source seat,                |                              |
        |  msg ID, seq, kind)                |                              |
        |--------------------------------------------------------------->  |
        |                                    |                  server reopens
        |                                    |                  its own row,
        |                                    |                  claims native
        |                                    |                  durably, then
        |                                    |                  SeatRunner +
        |                                    |                  seat budget
        |                                    |                  chat_selected/
        |  Mac chat-nudge queue <-------------|  held receipts stay durable
        |  (source-qualified                   for operator review
        |  dedupe key)
```

## Scope And Gaps

Echo is the first server source.  An Echo DM reaches Muse, but today the catalog has no verified
Echo-owned wake-capable chat.  The DM ownership gate therefore produces a visible held decision;
it cannot be redirected to a GROK or CURSOR chat.  A public Echo mention may choose a listed,
fresh chat of another seat.  Adding an Echo-owned adapter is a separate delivery task.

The protocol is source-generic for the configured Mac and server seats.  Grok Bot personas can
offer their existing HTTP routine as a fixed delivery resource when explicitly cut over, keeping
their original role credentials, routine and listener budget.  JET, the remaining server cloud
seats, and other Mac seats require separate activation evidence.  The existing CLAUDE Muse path
continues as is unless deliberately moved.  BotFleet is a separate listener: its native
`ZulipSession.poll` → `hub.handleMessage` → persisted per-role queue → `dispatchNext/startTurn`
path needs a separate Muse admission adapter after classification and before `startTurn`.  This
change creates no second BotFleet Agent-Sync queue and does not use outgoing callbacks as its
primary intake.
