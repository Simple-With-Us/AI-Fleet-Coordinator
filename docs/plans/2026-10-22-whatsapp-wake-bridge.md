# AFC-477 WhatsApp Wake Bridge For Jet:  Proposal (pending approval)

Thu, Oct 22, 2026, 6:35am.  Linear AFC-477.  Owner:  CLAUDE (session 75771c43).  Status:  proposal pending approval.  Nothing in this document is built, deployed or holding standing access.  The only live work approved so far is the second-event spike, and its exact scope is in section 13.

Every component, route, key and tool name below is a working name.  Section 12 explains why, and lists the names that Jay and Jet choose.

## 1. Status And What Changed

This revision answers Jet's design review of 6:16am today (https://simplewithus.zulipchat.com/#narrow/near/633930063).  Jet's tools cut the 6:15am Zulip post off at section 5, so this file is the complete design.

| Jet's point | Where it is answered |
|---|---|
| 1.  Publish the complete design | This file |
| 2.  An explicitly verified Jay and chat binding, not first arrival, and the creator check explained | Section 6 |
| 3.  Body retention apart from the 35-day dedupe ledger, recovery after a missed or lapsed wake, and what disabling send does | Sections 7, 8 and 9 |
| 4.  Sources for the 30-day replay window and the retry-safe responses, and unknown sends never retried | Sections 10 and 11 |
| 5.  Coexistence with the replacement and rename work | Section 12 |
| 6.  The exact scope of the live spike, and what stays pending | Section 13 |

What changed since the 6:15am post:

- The creator pin by first arrival is gone.  Jay starts the binding from an authenticated page, Meta's creator check confirms it, and it is re-verified on a schedule (section 6).
- Message bodies have their own retention, at most 7 days as proposed, and their own deletion path, apart from the 35-day ledger, which holds hashes only (section 7).
- PR #844 was closed and superseded by #849, which merged at 6:04am today.  The hosted Worker is now ST-MCP and answers only at `agents.simplewithus.com` (section 12).
- Placement and names are now choices for Jay and Jet, not assumptions (section 12).
- The spike flag stays on after the test until Jay says otherwise.  This replaces "the flag goes off afterwards" in my 6:16am post (section 13).

Changed after the review of PR #854 by Sentry and Kody:

- There is no resend.  A reply whose outcome is unknown stays `outcome_unknown` for good, and Jet asks Jay in a new message instead (section 10).
- Idempotency on `client_ref` has explicit rows for `sent`, `outcome_unknown` and definite refusals (section 10).
- The bridge gets its key only from its own environment, injected by `fleet-secrets run`, and fails closed without it (section 14).
- Inbound bodies are secret-scanned and redacted before storage, and replies are scanned before they are queued (section 5).
- Only the pinned owner identity can open a binding window (section 6).
- The bridge advances its offset on a processed-batch acknowledgement, even when nothing was stored, and validates every Meta batch against a boundary schema (section 5).
- `whatsapp_read` returns each body once, only returned messages can be acknowledged, and tool arguments have strict types and size limits (section 5).

## 2. Summary

Jay messages the WhatsApp agent he created for Jet.  One poller, the bridge, reads Meta's WhatsApp Agent Platform API.  It hands each new message from Jay's verified chat to the hosted MCP Worker over a signed call.  The Worker stores the message and sends a content-free MCP event to the subscription that Jet's existing ChatGPT conversation holds.  Jet then reads the message with one tool and replies with another.  The Worker queues the reply, and the bridge sends it.

The event is only a doorbell.  The text never rides in the event and never touches Zulip, and the Worker deletes it on a short schedule.

## 3. How A Wake Reaches Jet Today

Zulip mention, then the server listener (being renamed the Operator in #851), then a signed `agent-sync-wake/1` POST to `/internal/wake/JET`, then the Worker verifies it (`scripts/agent-sync-mcp/src/wake.js:132`), claims the `wake_id` and POSTs a signed `zulip.mention` event to the callback of the live subscription (`src/events.js:598-622`), and ChatGPT runs a turn in the subscribed conversation.  PR #428 built this, merged Oct 10.  Jet confirmed the route live at 5:55am today (https://simplewithus.zulipchat.com/#narrow/near/633924850).

Properties that shape this design, all in `scripts/agent-sync-mcp/src/events.js` at `2af99bec`:

- There is one event name, `zulip.mention` (line 51), and any other name is refused (line 752).
- There is one live subscription per seat, counted across every event name (line 55, enforced by `eventSubRoom` in `seat-state.js:709-729`).
- Delivery is by webhook only, with no replay.  An event sent while nobody is subscribed is gone (lines 11-13).
- A subscription lasts 24 hours by default, from 1 hour to 7 days (lines 56-58), and gets no delivery once it is past its `refreshBefore` (lines 23-26).
- `fanOut` does not check the subscription's event name (lines 598-622), so a second event without a name filter would cross-deliver.
- A wake with no live subscriber settles as `no_subscriber` or `not_live`, and the reply says `delivered: 0` (lines 625-638 and 699-704).
- Outbound fetches run in the Worker request, never inside the SeatGate Durable Object (lines 43-45).

## 4. Feasibility And The One Untested Step

It is feasible through a supported route:  a second MCP event (working name `whatsapp.message`) on the connector that Jet's conversation already subscribes to.

The untested step is whether ChatGPT lets one conversation hold a second event subscription next to `zulip.mention`, and whether the callback URL is the same for both.  The spike in section 13 settles that with a content-free test event, before anything WhatsApp-related is built.

If the spike fails, the fallbacks in order are:

1. A separate connector for the WhatsApp event.  That is a second OAuth surface, and whether its events land in the same conversation needs its own test.
2. A scheduled poll by Jet, if Jet confirms that a scheduled run lands in the existing conversation.

Rejected:

- A content-free Zulip mention as the carrier.  It leaks existence and timing into Zulip and spends Jet's Zulip wake budget.  It stays rejected unless Jay asks for it.
- The Workspace Agents trigger API.  Nothing shows Jet is a published workspace agent.
- The `whatsapp-agent` package's own MCP server.  It has no auth and acts as a second poller (section 15).

## 5. Architecture

Three parts, kept distinct as Jet asked:  the poller and sender, the store and tools, and the event.

### The bridge (poller and sender, working name `wa-bridge`)

- It is the only process that holds the WhatsApp API key, the only poller and the only sender.
- It long-polls `GET /agent/v1/updates` with `timeout=25` and `limit` of at most 100 (manual section 2, p. 10).
- It saves `next_offset` with fsync only after the Worker answers the batch with a processed-batch acknowledgement.  That acknowledgement carries the batch's `next_offset` and comes even when the Worker stored nothing, because every message was pre-binding, quarantined, stale or a replay, or the batch held only receipts.  The bridge advances on it, so a batch that stores nothing never stalls the offset.  Until it arrives, the bridge re-polls the same offset, which is safe because polling does not consume entries (p. 16).
- It checks every Meta answer against the boundary schema below before forwarding anything.  A batch that fails is not forwarded and the offset does not move.  The bridge logs only the field path and the rule that failed, never a value, sets its health to `schema_error` and alerts.  After three identical failures in a row it stops polling, so it never hammers Meta's rate limit.
- It sends each batch to the Worker over a signed outbound HTTPS call:  contract `wa-bridge/1`, HMAC-SHA256 over the raw body in a header, a 5-minute window, and its own key.
- It pulls queued replies from the Worker and sends them one at a time (section 10).
- It sends a signed, content-free heartbeat every 60 seconds.
- It has no inbound port.  Message bodies stay in memory only for the batch in flight, and never reach its disk or its logs.
- Where it runs is a choice (section 12).

### Boundary schema for Meta batches

The bridge checks each `GET /agent/v1/updates` answer before it forwards anything, and the Worker checks the forwarded batch again on ingest.  Extra fields inside a known object are ignored, because Meta may add fields.  Anything else that fails is a schema error.

- HTTP 204 has no body and no `next_offset`, and the offset stays where it is (p. 16, L1238-1242).
- `object` is the string `whatsapp_agent_platform`.  `entry` is an array of exactly one object, whose `id` is a string of digits (p. 10, L702-712).
- `entry[0].changes` is an array of exactly one object, with `field` equal to `messages` and `value.messaging_product` equal to `whatsapp`.
- `value.messages` and `value.statuses` are arrays, possibly empty, with at most 100 items between them (`limit` is at most 100, p. 10).  `value.contacts`, if present, is an array of objects whose `wa_id` is a string.
- Each message has a `from` that is a non-empty string starting `user:`, of at most 128 characters and compared in full (p. 4, L183-186).  Its `id` is a non-empty string of at most 512 characters starting `wamid.`, its `timestamp` is a string of digits, and its `type` is one of the documented types (p. 12, L822-835).  A `text` message has `text.body`, a string of at most 4,096 characters.  A `context`, if present, has string `id` and `from`.  Media and reactions pass on as type and metadata only, because their content is out of scope in v1.
- Each status has a string `id`, a `status` of `delivered` or `read`, a `recipient_id` starting `user:`, and a digit-string `timestamp` (p. 14, L1079-1107).
- `next_offset` is an integer from 0 to 2^63 - 1 (p. 14, L1113-1115).  After the first poll it is never lower than the offset just polled.
- The forwarded `wa-bridge/1` body is at most 2 MiB:  100 messages of at most 4,096 characters at up to 4 bytes each, plus their metadata.

### The Worker module (store, event and tools)

- One self-contained module in the hosted MCP Worker (today ST-MCP, Worker script `agent-sync-mcp`), registered through a small interface (section 12).
- Signed routes, working paths:  `POST /internal/wa-ingest/JET`, `GET /internal/wa-outbox/JET` (held open up to 25 seconds), `POST /internal/wa-outbox/JET/result` and `POST /internal/wa-heartbeat/JET`.
- It stores messages, the binding, the dedupe ledger and the send ledger in `wa_`-prefixed tables (section 7).
- Ingest decides each message in this order, and records the outcome in the 35-day dedupe ledger (section 7) whatever it is:
  1. **Ledger first.**  If the keyed hash of the message id is already in the ledger, the message is a replay and is dropped.  The ledger is checked before the inbox because inbox rows are deleted long before the ledger's 35 days, so an insert-or-ignore on the inbox alone would let a replay through once its row was gone.
  2. **Binding cutoff, on Meta's clock.**  If Meta's own `messages[].timestamp` (p. 11, L808-811) is earlier than the binding cutoff, which is Meta's timestamp of the code message (section 6), the message is dropped and ledgered as `pre_binding`.  Receive time on the bridge or the Worker is never used for this.
  3. **Age and high-water mark.**  A message whose Meta timestamp is more than 31 days old is dropped as `stale`, because Meta keeps 30 days (p. 16, L1229) and the ledger cannot vouch beyond its 35.  After an offset reset the bridge flags its batches `replay: true`, and in those batches a message older than the newest message already stored is dropped as `replay`, even if the ledger has lost it.
  4. **Sender.**  A sender other than the bound one is quarantined and ledgered as `quarantined`, with no body.
  5. **Store.**  Only then is the message stored, after the secret scan below, with an insert-or-ignore on the message id as a last guard.  Only a newly stored message counts toward a ring.
- Every batch, whatever it stored, gets the processed-batch acknowledgement with its `next_offset`.
- **Secret scan before storage.**  The Worker scans every inbound body with the fleet's own rules, through a new `redactSecret(text, known)` in `src/contract.js`.  Today only `scanSecret` is exported (`:343`), and it returns the kind of the first hit, not where it is.  `redactSecret` returns the redacted text and the list of rules hit.  It uses the same `SECRET_PATTERNS` (`:309`, which mirror `secretscan.py` and are kept equal by `test/secretscan-parity.test.mjs`), and it also covers the two hits that do not come from that list:  a loaded credential from `known` (`:344-346`) and a Zulip-shaped key (`:350-354`).  Those two are replaced by their literal value, with their own markers, `[redacted:loaded-credential]` and `[redacted:zulip-shaped-key]`.  Every match is replaced with `[redacted:<rule>]` before the row is written, so the value is never stored, returned, logged or relayed.  The log line holds only the keyed message-id hash and the rule name.  The message still arrives, so Jay's own message is never dropped, and `whatsapp_read` marks it `redacted` with the rule names so that Jet knows.  Jet's replies get the same scan before they are queued:  a hit is refused with `secret_in_reply` and the rule name, and nothing is stored.
- Messages the bridge drops itself, before any binding or from an unbound sender, still reach the ledger:  the bridge sends their message id and Meta timestamp, never their body.
- The 5-minute window on the signed call protects the HTTP request against replay, not the messages inside it.  A message's age is judged only by Meta's timestamp and the ledger.
- Wake ids use their own `wa:` namespace, apart from Zulip's `wake_seen`.
- The event carries metadata only:  seat, wake id, pending count, oldest and newest receive times, held and `outcome_unknown` reply counts, and the binding state.  No text and no ids.
- Three tools, for seat JET only.  This list is the whole contract, and later sections refer to it without adding parameters:
  - `whatsapp_read(since_seq?, limit?, ack_through?, include_surfaced?)`.  It returns each pending message's body once, oldest first, with its `seq` (a per-seat counter the Worker assigns), Meta's timestamp, its type, its text inside a fence marked as untrusted data, `reply_to_seq` when it quotes an earlier message, and `redacted` when the secret scan removed something.  The Worker then marks the message surfaced.  Later calls, and later rings, report surfaced but unacknowledged messages as a count only, until Jet acknowledges them or they expire, so the same body is not injected into Jet's context again and again.  `include_surfaced: true` returns them again, explicitly.  It also returns the state of each recent reply.  `limit` defaults to 20 and is at most 50.
  - `whatsapp_reply(text, client_ref, reply_to_seq?, ack_through?)`.  It has no recipient argument and returns an `intent_id` and its state.  The Worker maps `reply_to_seq` to Meta's message id for `context.message_id`.  It is idempotent on `client_ref`, as section 10 defines.
  - `whatsapp_send_status(intent_id?, action?)`.  Without `intent_id` it lists recent replies and their states.  `action` is `release` or `cancel`, and applies only to a `held` reply (section 9).
- There is no resend.  A reply whose outcome is unknown is never sent again by the system (section 10).
- `ack_through=<seq>`, on `whatsapp_read` or `whatsapp_reply`, acknowledges every message up to and including that `seq`.  Only messages that `whatsapp_read` has actually returned can be acknowledged.  A `seq` that has not been returned is refused with `not_surfaced`, and nothing is acknowledged, so an unread message can never be cleared this way and keeps the expiry stub and alert of section 7.  Acknowledging is idempotent, and a value lower than an earlier one changes nothing.  In one call, `ack_through` is applied first, and only to messages that earlier calls returned, and the page the same call returns is marked surfaced afterwards.  Returning a message again with `include_surfaced: true` resets nothing:  the 7-day cap still counts from receipt, and the 24-hour clock from the first acknowledgement.
- **Argument rules.**  The Worker checks arguments before it reads or creates any idempotency row, and refuses with `invalid_argument` naming the field:
  - The raw request body is measured before parsing and refused above 64 KiB.  Parsed arguments may nest at most 16 levels deep and hold at most 1,000 nodes, and a structure that refers back to itself is refused.  Unknown argument names are refused.
  - `client_ref` is a non-empty string of 1 to 128 characters from `A-Z`, `a-z`, `0-9`, `.`, `_`, `:` and `-`.  A number, a blank string or anything else is refused.
  - `since_seq`, `ack_through` and `reply_to_seq` are non-empty strings of decimal digits with no leading zero and at most 16 digits, so `"42"` is valid while `42`, `""` and `"042"` are not.  The Worker compares them as integers.
  - `text` is a string of 1 to 4,096 characters (p. 4, L138).  `limit` is an integer from 1 to 50, `include_surfaced` is a boolean, `action` is `release` or `cancel`, and `intent_id` is a string in the Worker's own intent-id format.
- The binding controls, the switches and a content-free health view are on `/admin`, which sits behind the path-scoped Cloudflare Access app that the Worker re-verifies (`scripts/agent-sync-mcp/README.md:42`).

### Unchanged

The listener and Operator, `daemon.py`, `[seat.JET]`, `agent-sync-wake/1`, `/internal/wake/<SEAT>`, `WAKE_HMAC_KEY_JET`, `JET_ROUTINE_KEY`, the `zulip.mention` schema, its subscription ids, and its tests, including `test_a_jet_mention_wakes_the_hosted_worker_with_an_hmac_signed_body` (`scripts/agent_sync/tests/test_server.py:1433`) and "one live destination per seat" (`scripts/agent-sync-mcp/test/events.test.mjs:433`).

Two existing Worker behaviours change, each with regression tests, and the spike exercises both first:  delivery filters by event name, and the cap becomes one conversation per seat with one subscription per event name, so a second conversation is still refused.

Files that change in the existing Worker, each with tests:

- `src/events.js`:  an event registry beside `EVENT_NAME` (line 51), a name filter in `fanOut` (lines 598-622), and the cap check against the new grouping.
- `src/seat-state.js`:  `eventSubRoom` (line 709) and `eventSubUpsert` (line 722) group live subscriptions by conversation, keyed by callback URL, and allow one subscription per event name within that conversation.  A subscription from a second conversation is still refused, and `MAX_SUBSCRIPTIONS_PER_SEAT` (`events.js:55`) becomes a limit on conversations, not on subscriptions.  This assumes one callback URL per conversation, which the spike checks (assumption A9).
- `test/events.test.mjs`:  the existing "one live destination per seat" test (line 433) stays as it is.  New tests cover two event names in one conversation, a second conversation refused for each name, and no cross-delivery in either direction.
- `src/contract.js`:  the new `redactSecret(text, known)` beside `scanSecret`, which stays as it is.  A new test runs the samples of `test/secretscan-parity.test.mjs` through `redactSecret` and asserts that no value survives in the redacted text, including a loaded credential and a Zulip-shaped key, and that the log line carries only the message-id hash and the rule name.
- `src/config.js`, `src/access.js` and `test/config.test.mjs`:  `OWNER_EMAIL` is a new single-value setting, distinct from the existing `OWNER_EMAILS` list (`wrangler.jsonc:173`, `src/config.js:228`).  A config test proves that an unset, empty or multi-value pin fails closed, in the style of `test/config.test.mjs:43`.  If the pin is the Access user id instead, the verifier at `src/access.js:104`, which returns only `{ ok, email }` today, must also return the token's `sub`.
- `src/index.js`, `src/policy.js` and the tool registration in `src/hosted-tools.js`:  the registration points for the new module (section 12).
- New:  `src/wa/`, for everything WhatsApp-specific.

### Kept out of Zulip

All WhatsApp text, sender and message ids, and timing.  No relay or carrier post, no seat inbox, no held-back digest.  Alerts carry no content and go to Sentry.

### Wake budget (proposed, Jay decides)

Separate from the Zulip budget.  Messages within 30 seconds share one ring.  There is one outstanding ring at a time, re-rung after 10 minutes if still unacknowledged.  At most 20 rings an hour and 100 a day.  Over budget, messages are held in the store and rung later, never dropped.

## 6. Jay's Chat Binding And The Creator Check

### What Meta's manual guarantees

| Guarantee | Source (Meta manual v1, see section 11) |
|---|---|
| The API token identifies one agent. | Authentication, p. 4 (L171) |
| An agent may send only to its creator.  `to` must be the creator's `user:<id>`, and anything else is refused with HTTP 403 and `error.code` 131005. | Section 1 request parameters, p. 6 (L289-298);  section 5 send errors, p. 25 (L2045-2050);  error reference, p. 28 (L2250-2256) |
| A message can be marked read only if the creator sent it.  A read receipt on anyone else's message is refused with 403 and 131005. | Section 3 `message_id`, p. 16 (L1292-1300);  section 5 statuses errors, p. 27 (L2145-2146);  error reference, p. 28 (L2250-2256) |
| Identifiers are opaque and compared in full.  They refer to an account rather than a person, and may change, for example after a phone number change or a re-registration. | Participant identifiers, p. 4 (L183-186);  Stability, p. 5 (L215-227) |
| Rotating the token invalidates the old one, and uninstalling the app forces a new token. | Authentication, p. 4 (L169);  Quick start, p. 3 (L66) |

### What the manual does not say

These are assumptions, so the design does not rely on them:

- That only the creator can send messages to the agent.  The manual restricts our outbound sends and our read receipts to the creator, but says nothing about who can reach the agent inbound.  The bridge therefore treats any other sender as possible, and quarantines it.
- That the creator is Jay.  The manual knows only "the agent's creator", the WhatsApp account that made the agent in Settings > Agents (p. 3, L56-61).  That this account is Jay's own is a fact only Jay can confirm, which is why the binding needs an explicit step from him.
- Whether creator status follows an account across a number change or a re-registration.

So "creator-only messaging" fully covers the outbound direction.  Even a buggy bridge or a prompt-injected Jet cannot message anyone but the creator, because Meta refuses it.  For the inbound direction it gives a documented test, the read receipt, but no guarantee.  The binding combines that test with an explicit step by Jay.

### Establishing the binding (proposed)

1. Jay opens `/admin` (Cloudflare Access, re-verified by the Worker) and presses Start WhatsApp Binding.  Opening a window requires the verified Access identity to equal one pinned owner in Worker config:  `OWNER_EMAIL`, a new single-value setting and not the existing `OWNER_EMAILS` list that `/admin` accepts today (`src/config.js:228`, `src/access.js:104`), or the owner's Access user id (the token's `sub`).  If the pin is unset, empty, holds more than one value, or differs from the caller, the Worker refuses to open a window and logs `binding_not_owner`.  Then the Worker makes a random window id and a one-time code of 8 symbols from a 32-symbol alphabet with no look-alike characters, about 40 bits.  It stores only the code's keyed hash (below) and shows the code only on that page.  The window lasts 10 minutes and the code works once.
2. Jay sends exactly that code, from his own phone, in the agent's WhatsApp chat.
3. While a window is open, the Worker tells the bridge the window id and expiry in its answer to every ingest and heartbeat.  A new text message is a candidate only if its body, after trimming, upper-casing and removing spaces and hyphens, has the code's shape:  8 symbols of the alphabet.  For a candidate the bridge sends the Worker the sender id, the message id, Meta's timestamp and the keyed hash of the normalized body, never the body.  Any other message is not a candidate and costs no attempt.  Outside a window, and before any binding exists, no body is stored and nothing wakes Jet.  Only the ledger records each message (section 5).
4. The Worker compares the two hashes in constant time.  Attempts are counted per sender:  a sender with three non-matching candidates is ignored for the rest of that window, but the window stays open for everyone else, so a stranger cannot lock Jay out.  The window closes only when it expires, when the code is used, when Jay cancels it, or when the creator check refuses the matching sender (step 5), and a content-free alert says so whenever it closes.  Any candidate from a sender other than the one that binds raises a content-free alert too.  Three guesses per sender against 40 bits is no real chance.
5. When a hash matches, the code is spent, and the Worker asks the bridge to run the creator check on that message:  `POST /agent/v1/statuses` with `status: "read"`.
   - 200 means Meta accepted a read receipt, which it allows only on the creator's messages, so the sender is the creator.
   - 403 with 131005 means the sender is not the creator, so the code has reached someone else.  The binding is refused, the window closes, and a content-free alert goes out.
   - 500, 503 or a timeout is inconclusive, because the message may already be marked read (p. 27, L2150-2155).  The bridge repeats the check up to three times with backoff, as the manual says to for those codes.  If it is still inconclusive, no binding is made and Jay starts again.
6. Both checks passed, the Worker records the binding:  the agent id (`entry[].id`, p. 10, L710-712), the sender's full `user:<id>`, the binding cutoff, `verified_via: code+creator_check`, the last check time and a binding epoch.  The cutoff is Meta's own timestamp of the code message (`messages[].timestamp`, p. 11, L808-811), so the cutoff and every later comparison use Meta's clock, never the bridge's or the Worker's.  The bridge keeps a copy of the binding, because it needs the id for `to`.  `/admin` shows the bound time, the last check and a short fingerprint, never the id itself.
7. A message whose Meta timestamp is earlier than the cutoff never reaches the inbox or wakes Jet, however late it arrives.  The code message itself is ledgered and never stored (section 5).

**The keyed hash.**  Both sides use one scheme:  `HMAC-SHA256(K_pair, window_id + ":" + normalized_text)`.  `K_pair` is derived from the ingest signing key that both already hold:  `K_pair = HMAC-SHA256(WA_BRIDGE_HMAC_KEY_JET, "wa-bridge/1 pairing")`.  So no new secret is needed, the bridge and the Worker compute the same value, a hash seen on the wire is useless without the key, and including the window id ties each hash to one window.

Never:  a binding by first arrival, by message content alone, by Jet, by a peer, or by anything posted in Zulip.

### Re-verifying the binding

- Every inbound message:  the sender must equal the bound id in full, and the poll's agent id must equal the bound agent id.  Anything else is quarantined:  not stored, not forwarded and not woken, only counted, with a content-free alert.
- Every send:  `to` is always the bound id.  A 403 with 131005 on a send marks the binding `suspect`.  That reply's `client_ref` is released for a later authorized attempt, keeping the code 131005 (section 10).  Sending stops, the other queued replies become held (section 9), and Jay is alerted to re-bind.
- Scheduled re-check:  the creator check runs again on the first bound message after every bridge start, after any token rotation, and at least every 7 days, on the next inbound message.  It runs only on a message the Worker has already stored, because marking a message read may delete it from Meta's buffer (p. 16, L1231-1233).  A 403 marks the binding `suspect`.  Side effect:  each re-check shows Jay a read receipt on that one message.
- An identifier change (p. 5, L215-227) shows up as Jay's messages arriving from an unbound sender.  They are quarantined, Jay is alerted, and he re-binds with a new code.  A new agent, with a new agent id, voids the binding the same way.
- Unbinding is one button on `/admin`.  Wakes and sends stop at once, and stored bodies follow section 7 unless Jay purges them.

States:  `unbound`, then `binding` while a window is open, then `bound`, then `suspect` after a failed re-check.  Only Jay moves a binding out of `suspect`, by unbinding or by starting a new window.

## 7. Message Storage, Retention And Deletion

Two different things are kept, for different reasons:

- **Message bodies**, briefly, so that Jet can read them.  At most 7 days, as proposed.
- **The dedupe ledger**, 35 days, with no bodies.  It holds keyed hashes of message ids, so that a replay of Meta's 30-day buffer can never wake Jet twice.

| Store | What it holds | Where | How long | Who can read it | How it is deleted |
|---|---|---|---|---|---|
| Meta's update buffer | Every inbound message and receipt | Meta | 30 days from storage.  A message marked read may go sooner (p. 4, L154;  p. 16, L1229-1233).  Media lasts 30 days (p. 24, L1907). | Meta, and whoever holds the API key | Meta's expiry.  The API offers no delete for text messages. |
| Bridge memory | The bodies of the batch in flight, and one reply's text during its send | The bridge process | Seconds | The bridge user and root on its host | Process memory only, with core dumps off.  Swap is a residual risk on a shared host. |
| Bridge state | Offset, activation time, the binding copy, a ring of keyed message-id hashes, and the local send ledger (intent id, state, body hash, sent message id).  No bodies. | A 0700 state directory on the bridge host | Hashes and ledger rows 35 days | The bridge user and root | Swept at start and hourly.  The directory is wiped at decommission. |
| Worker inbox | Inbound text (at most 4,096 characters, with any secret already replaced by `[redacted:<rule>]`), message id, Meta's time, type, reply-to id and the surfaced marker | ST-MCP Worker, SeatGate Durable Object (or its own class, section 12) | 7 days from receipt, or 24 hours after Jet acknowledges it, whichever is first (proposed, Jay decides).  Only a message that `whatsapp_read` returned can be acknowledged. | Seat JET through `whatsapp_read`, for the bound sender's messages only | Hard row delete (below) |
| Worker outbox | Jet's reply text | Same | Until the reply is final (`sent`, `refused`, `cancelled` or `expired`), then deleted at once.  A held reply expires unsent after 24 hours.  An `outcome_unknown` reply's text goes after 24 hours, and Jet still has its own copy. | Seat JET through `whatsapp_send_status` (state, never text) | Same |
| Binding candidates | For each code-shaped message during an open window:  sender id, message id, Meta's timestamp, the keyed hash and that sender's attempt count | Same, in its own `wa_` table | Hard-deleted when the window closes or the code is consumed, so at most 10 minutes.  The winning sender's id moves into the binding record. | No seat:  no tool reads it, and `/admin` shows counts only | Hard delete on close or use, and the hourly sweep as a backstop |
| Dedupe ledger | HMAC-SHA256 of each message id under a Worker key, Meta's timestamp, and the outcome (`stored`, `pre_binding`, `quarantined`, `pairing`, `stale` or `replay`).  Every message the bridge sees gets a row, including the ones it drops. | Same | 35 days from Meta's timestamp:  Meta's 30 days plus 5 days of margin | The Worker only | Same sweep |
| Send ledger | Intent id, a hash of `client_ref`, state, attempts, times, body hash, sent message id, delivered and read times | Same | 35 days | Seat JET through `whatsapp_send_status`;  counts on `/admin` | Same sweep |
| Audit rows | Event kind, counts and hashes | Same | The existing 90-day audit (`seat-state.js:159`) | `/admin` | The existing sweep |
| Event payloads | Counts, times and states only | OpenAI, in Jet's conversation | OpenAI's retention | Jet's conversation | No content to delete |
| Jet's conversation | Whatever Jet reads with `whatsapp_read` | OpenAI (ChatGPT) | OpenAI's retention for that conversation | Jay's ChatGPT account | Outside this system.  It is a decision for Jay (section 19). |
| Zulip | Nothing | | | | |
| Sentry | Codes, counts and states.  No bodies, no ids. | Sentry | The plan's retention | Org members | Sentry's |

### Deletion

- An expired body is never returned, even before the sweep reaches it.
- An hourly sweep hard-deletes expired rows.  It runs from the bridge heartbeat request, or from a storage-only Durable Object alarm, which fits the rule that SeatGate methods stay storage-only (`events.js:43-45`).
- `/admin` gets a Purge All WhatsApp Content button.  Decommissioning purges every `wa_` table, deletes the Infisical secrets, wipes the bridge state and rotates the WhatsApp key.
- A body that `whatsapp_read` never returned is deleted at 7 days and leaves a stub (`expired_unread` and its receive time), so Jet's next read reports it.  Jay gets a content-free alert when that happens.  An acknowledgement cannot skip this, because only returned messages can be acknowledged.

### The point-in-time recovery caveat

SeatGate is a SQLite-backed Durable Object (`scripts/agent-sync-mcp/wrangler.jsonc:66`).  Cloudflare documents point-in-time recovery for those, to any point in the past 30 days (Cloudflare docs, Durable Objects, "SQLite Storage API", section "PITR (Point In Time Recovery) API").  So someone who can deploy code to the Worker could restore a deleted body for up to 30 days.  Two options for Jay:

- **Accept it.**  "Deleted" then means unreadable through every tool at once, and restorable by a Worker deployer for 30 days.
- **Crypto-shred it (recommended).**  Each body is encrypted with AES-GCM under a per-day data key kept in Workers KV, with an expiry equal to the 7-day cap.  Once that key expires, a restored copy is unreadable, so 7 days becomes a hard bound.  This rests on assumption A10 (section 11).

### Who can read what

- Through the Worker, only seat JET can read bodies, with `whatsapp_read`, and only the bound sender's messages.  `/admin` shows counts and states, never bodies.
- Anyone who can deploy the Worker could, in principle, ship code that reads the store.  Today that is Jay's Cloudflare account and its deploy credentials.
- Meta can read everything in the agent chat.  The earlier research reads Meta's agent terms as saying agent chats are not end-to-end encrypted (unverified here, assumption A8).
- Once Jet reads a message, the text is in Jet's ChatGPT conversation, under OpenAI's retention, outside this system.

## 8. Missed Or Lapsed Wakes:  Recovering Queued Messages

The store is the record, not the event.  A message stays pending until Jet acknowledges it, and the Worker keeps ringing, within the budget, until Jet does.

- **Acknowledgement.**  `whatsapp_read` returns each pending body once, oldest first, and Jet acknowledges with `ack_through=<seq>` on its next `whatsapp_read` or on `whatsapp_reply`, as defined in section 5.  A turn that reads and then dies loses nothing:  the message stays pending, later reads and rings report it as a count, and `include_surfaced: true` returns it again.
- **A delivered ring** (`delivered` of 1 or more):  the state becomes `ringing`.  If nothing is acknowledged within 10 minutes, the Worker rings once more.
- **`delivered: 0`** (no subscriber, a lapsed or revoked subscription, or a failed delivery):  the state becomes `undelivered`.  The Worker tries again at 1, 5, 15, 30 and 60 minutes, then hourly, within the budget.  Outbound delivery has to run in a Worker request (`events.js:43-45`), so these retries ride the bridge's signed 60-second heartbeat.  If Jay wants re-rings while the bridge itself is down, a Worker cron trigger every 5 minutes is the backstop.  That trigger is new, so it is an ops change.
- **Re-subscribing.**  Whenever Jet's conversation subscribes to or refreshes the WhatsApp event while messages are pending, the Worker rings once right after it stores the subscription.
- **The 24-hour expiry.**  ChatGPT has to refresh the subscription, which lasts 24 hours by default (`events.js:56`).  A lapse loses rings, never messages.  The messages wait in the store, and the next ring or any `whatsapp_read` finds them.
- **Long gaps.**  If messages are pending and no ring has been delivered for 6 hours, a content-free Sentry alert goes out, and again at 24 hours.  Opening Jet's conversation renews the subscription and triggers the catch-up ring.
- **Expiry before reading.**  The stub and alert in section 7.
- **The bridge is down.**  Messages wait in Meta's buffer for up to 30 days (p. 16, L1229-1231) and are read from the saved offset on restart.  Sentry Crons alerts on a missed check-in after 10 minutes.  Past 30 days Meta expires them, and nothing can recover them.
- **The bridge loses its state.**  It replays from `offset=0` (p. 15, L1214-1218) and flags those batches `replay: true`.  The ledger check, the binding cutoff on Meta's timestamps and the high-water mark (section 5) stop old messages from waking Jet again, even after their inbox rows are gone.

## 9. Switches, And What Disabling Send Does

Three switches, all off by default:  POLL, WAKE and SEND.  Their values live in Infisical (`/jet`) and reach the bridge only through `fleet-secrets run` when it starts (section 14), so the bridge never calls Infisical itself, and a change there takes effect at the next bridge restart.  Each switch also has a Worker admin switch that acts at once:  the Worker answers ingest or outbox calls with `paused`, and the bridge stops polling or sending until a heartbeat answer clears it.  A local disable file stops the bridge at once.

- **POLL off.**  The bridge stops polling.  Messages wait in Meta's buffer for up to 30 days, and polling resumes from the saved offset.
- **WAKE off.**  Messages are still stored, nothing rings, and Jet can still read them.  Turning WAKE back on rings once if anything is pending.
- **SEND off** (recommended behaviour):
  - The Worker's outbox route hands out nothing from the moment the switch flips, and the bridge checks the switch again before each send.
  - If the bridge has already claimed a reply and finds SEND off before its POST, it posts a decline to `POST /internal/wa-outbox/JET/result`, and the reply goes back to `held` with reason `send_disabled`.  A decline is a result, so it never becomes `outcome_unknown`.  That state is kept for sends that may have happened, including a lease that ends with no result at all.
  - At most one send can be in flight, because sends run one at a time.  It finishes, and its result is recorded as usual (`sent`, `refused` or `outcome_unknown`).  It cannot be recalled.
  - Replies already queued stay queued, marked `held` with reason `send_disabled`.  They are never sent while SEND is off.
  - New `whatsapp_reply` calls are accepted and recorded as `held`, and the answer says so plainly.
  - Jet sees held replies in `whatsapp_send_status`, in every `whatsapp_reply` answer, and as a count in the next event.
  - When SEND comes back on, held replies are not sent automatically.  Jet releases each one with `whatsapp_send_status(intent_id, action: "release")`, or cancels it.  A held reply expires unsent 24 hours after it was queued, and its text is deleted.  The reason:  a reply written hours earlier may no longer be right.  Jay may prefer automatic release for replies held less than, say, 30 minutes.
  - An `outcome_unknown` reply is never sent again, whatever the switch says.

A binding in the `suspect` state holds replies the same way.

## 10. Sending And Uncertain Outcomes

The ledger states are `queued`, `claimed`, `sending`, and then one of `sent`, `refused` or `outcome_unknown`, plus `held`, `cancelled` and `expired`.  The bridge writes `claimed`, and then `sending`, to its own state with fsync before the POST, so that a restart can tell "never sent" from "maybe sent".

| Outcome | What it means | Our state | Retried? | Source |
|---|---|---|---|---|
| 2xx with `messages[].id` | Sent | `sent`, with the message id stored | Never | "Retrying a send", p. 9 (L614);  section 5, p. 25 (L1992-1993) |
| 429 (`error.code` 130429) | Not sent, and may be retried after backing off | Back to `queued` | Yes, at most 3 attempts in all, then `refused` | p. 9 (L615-616);  p. 26 (L2061);  p. 28 (L2247-2248) |
| 503 with `error.code` 131016 | Not accepted for delivery, so not sent | Back to `queued` | Yes, same cap | p. 9 (L617-619);  p. 26 (L2068-2070);  p. 28 (L2264-2265) |
| 503 without 131016, or a body that cannot be parsed | Not documented | `outcome_unknown` | Never (assumption A3) | None |
| 500 | It may or may not have been sent | `outcome_unknown` | Never (see the note below) | p. 9 (L620-622);  p. 26 (L2063-2066) |
| A connection reset, or our own read timeout | Unknown whether it was sent | `outcome_unknown` | Never | p. 9 (L620-625) |
| The TCP or TLS connection never opened, so no request byte was written | Not documented by Meta.  The request never left the bridge. | Back to `queued` | Yes, same cap (assumption A4).  The conservative alternative is `outcome_unknown`. | None |
| Any other 4xx (400, 401, 403) | Not sent, and retrying unchanged fails the same way | `refused`, with its code kept and the `client_ref` released | Never automatically | p. 9 (L615);  p. 25 (L1995-2050) |
| A restart finds `sending` | Unknown | `outcome_unknown` | Never | Follows from p. 9 (L620-622) |
| A restart finds `claimed`, so the POST never started | Not sent | Back to `queued` | Yes | The bridge's own write-ahead state |
| The bridge claimed it, then found SEND off before the POST | Not sent | Back to `held`, reason `send_disabled` | Only after a release (section 9) | The bridge's decline result |
| The Worker holds a claim with no result after its 5-minute lease, and the bridge has no state to ask | Unknown | `outcome_unknown` | Never | None |

**Note on 500.**  The send-errors table says to retry a 500 with exponential backoff and points to "Retrying a send" (p. 26, L2063-2066).  That section says a 500 leaves it unknown whether the message was sent, that a retry may send it twice, and to decide in advance whether to retry (p. 9, L620-622).  This design decides in advance:  never.

An `outcome_unknown` reply shows up for Jet in the tools and in the next event, and it stays `outcome_unknown` for good.  The system never sends it again, and there is no resend parameter.  If Jet thinks Jay did not get it, Jet asks Jay in a new message, such as "Did my last reply reach you?".  A new message is a new intent with its own `client_ref`, not a replay.  To keep that line clear, the Worker refuses a reply whose text is identical to an `outcome_unknown` reply's, using the body hashes the send ledger keeps for 35 days, with `same_text_as_unknown`.

**Idempotency on `client_ref`:**

| The earlier attempt with this `client_ref` | What a repeat call does |
|---|---|
| None | Creates the intent. |
| `queued`, `claimed`, `sending` or `held` | Returns the existing intent and its state.  Nothing new is queued. |
| `sent` | Returns the cached intent, with its sent message id and delivery state.  Nothing is sent. |
| `outcome_unknown` | Refused with `outcome_unknown`.  Nothing is sent. |
| `refused` by a definite answer:  403 with 131005, another documented definite 4xx, or the retry cap reached on 429 or on 503 with 131016 | The row is released for a later authorized attempt, for example after Jay re-binds.  The refused attempt stays in the ledger with its code, and the new attempt is linked to it. |
| `cancelled` or `expired` | Returns the cached intent and its state.  Nothing is sent, and a new attempt needs a new `client_ref`. |

Also:

- One send at a time, because Meta does not guarantee order between concurrent sends to one recipient (p. 9, L608).
- At most 10 sends a minute, against Meta's limit of 12 (p. 4, L132-136;  p. 28, L2280-2281).
- `whatsapp_reply` is idempotent on `client_ref`.  A repeat returns the same intent and sends nothing new.
- The read timeout is 60 seconds.  The manual asks for one "significantly higher" than a normal send but gives no figure (p. 9, L624-625;  assumption A5).
- A 403 with 131005 on a send marks the binding `suspect` (section 6).
- A 400 with code 100 or a 401 with code 190 (the token is invalid or missing, p. 4, L173-180) sets `auth_failed`.  Polling and sending stop, and Jay is alerted.

## 11. Sources And Assumptions

### Sources

- **Meta manual:**  "WhatsApp Agent Platform Developer manual", version 1, August 25, 2026.  PDF of 249,851 bytes, sha256 `ba677cdb476b52c9126a0e7565616131104f240b461428e8b388059a71c506e0`, fetched from a whatsapp.com link that redirects to Meta's CDN.  It is byte-identical to `docs/Agent Platform Guide.pdf` in the `whatsapp-agent` repository.  Page numbers are the manual's printed page numbers.  `L` numbers are lines of `pdftotext -layout` output of that PDF (2,299 lines), so anyone can reproduce them.
- **Package:**  `whatsapp-agent` 0.1.0, sdist from PyPI (sha256 `91efe1809fd07f0843fae838c07e7b4f361e06fa1fae5e8ce034b24f02d14fe4`).  Paths are under `src/whatsapp_agent/` unless shown otherwise.  Read as text only, never run or imported.
- **This repository:**  `main` at `2af99bec`.
- **Cloudflare:**  developers.cloudflare.com, Durable Objects, "SQLite Storage API", section "PITR (Point In Time Recovery) API".

### Claims and where they come from

| Claim | Source |
|---|---|
| The 30-day replay window | Quick reference, "Update retention:  30 days", p. 4 (L154);  "Starting a new agent", where `offset=0` "replays up to 30 days of backlog", p. 15 (L1214-1218);  "Retention", where entries are kept 30 days from storage, polling does not consume them, and a message marked read may be deleted, p. 16 (L1229-1233) |
| Re-polling an offset is safe, and a 204 has no `next_offset` | p. 16 (L1229-1231 and L1238-1242);  p. 26 (L2083-2085) |
| Omitting `offset` reads from the head, and should be done once only | p. 10 (L660-673);  p. 15 (L1209-1213) |
| Store `next_offset` as a signed 64-bit integer and pass it back unchanged | p. 14 (L1113-1115) |
| Receipts and messages share one sequence | p. 14 (L1109-1111) |
| One poller per agent:  a second one gets 409 with code 1752041 | p. 26 (L2099);  p. 28 (L2271-2272) |
| 15 polls a minute | p. 26 (L2101-2102);  p. 28 (L2286-2287) |
| Which send responses are safe to retry | Section 10 table:  p. 9 (L611-625), p. 25-26 (L1987-2070), p. 28 (L2247-2248 and L2264-2265) |
| Creator-only sending and read receipts | Section 6 table |
| Text bodies of at most 4,096 characters | p. 4 (L138);  p. 7 (L408-411) |
| The agent and token are set up in WhatsApp, and uninstalling forces a new token | p. 3 (L56-66) |

### Assumptions (not documented)

- **A1.**  Only the creator can message the agent.  Not documented, and the design does not rely on it (section 6).
- **A2.**  A message id is unique for the agent's lifetime, so it works as the dedupe key.  The manual calls it a "server-assigned message id" (p. 8, L505-510;  p. 11, L800-805) without saying it is unique.
- **A3.**  A 503 on a send without code 131016 is not safe to retry.  Undocumented, so it is treated as `outcome_unknown`.
- **A4.**  A connection that never opened means nothing was sent.  That is our reasoning, not Meta's documentation.  If Jay or Jet prefer, it becomes `outcome_unknown` too.
- **A5.**  60 seconds is "significantly higher" than a normal send.
- **A6.**  A 429 may carry `Retry-After`.  The manual does not mention the header.  The bridge honours it when present and otherwise backs off exponentially.
- **A7.**  Meta's 30 days are counted from storage on Meta's clock.  The 35-day ledger adds 5 days of margin for clock skew and slow expiry.
- **A8.**  Meta's agent terms say that agent chats are not end-to-end encrypted, that a user may have up to 5 agents, that Meta may cap, suspend or remove agents, that reverse engineering is banned, and that the rollout is a beta in some countries.  This comes from the earlier research pass on Meta's Third-Party Agents Terms.  It was not re-verified for this revision and is not cited to a section, so treat it as unverified until it is.
- **A9.**  ChatGPT accepts a second event subscription in the same conversation, with a stable callback URL.  The spike tests this.
- **A10.**  Workers KV keeps no point-in-time copy of an expired key, which the crypto-shred option relies on.  To confirm against Cloudflare's documentation before relying on it.
- **A11.**  `messages[].timestamp` is set consistently within one chat.  The manual says only that it is a Unix time in seconds (p. 11, L808-811), not whose clock sets it.  The binding cutoff is the code message's own timestamp from the same chat, so a constant offset cancels out.
- **A12.**  The `fleet-secrets` CLI is installed on the bridge host and runs there in its token mode, with `INFISICAL_TOKEN` from the bridge identity.  It never passes its own auth variables on to the command it starts (`AUTH_ENV_VARS` in `fleet_secrets/core.py`).

### What the package does that the bridge must not

- `transport.py:82-84` retries any 503 whenever `auto_retry_not_accepted` is set, without checking for 131016, and `client.py:170-176` sets it on every send.  The docstring at `errors.py:79-82` calls a 503 always safe to retry.  The bridge retries a 503 only with code 131016.
- `client.py:373-374` catches every API error and polls again after 2 seconds, including the invalid-token errors (`errors.py:49`, `errors.py:86` and `errors.py:137-138`), so a bad key loops forever.  The bridge stops on 400 with code 100 and 401 with code 190.
- `client.py:391` moves the offset forward before the caller has the batch, and `client.py:397-398` marks a message read before the handler runs.  A crash then loses messages, and the read receipt may delete them from Meta's buffer.  The bridge advances only after the Worker acknowledges the batch, and marks read only for the creator check, on stored messages.
- `client.py:22` and `client.py:30` load a `.env` file at import.
- `mcp/server.py:130-160` exposes `wa_get_updates`, a second poller that Meta answers with 409, and `wa_mark_read`.  `mcp/server.py:97-127` reads and writes any local path.  `cli.py:43-46` serves HTTP on 127.0.0.1:8765 with no auth option.
- `pyproject.toml:37` (sdist root) requires `mcp>=1.2.0` with no upper bound.

## 12. Coexistence With The Replacement And Rename Work

### Where the related work stands (checked at about 6:25am today)

| PR | State | What it means here |
|---|---|---|
| #844, the Worker host move (Echo) | Closed.  Superseded by #849, which merged at 6:04am. | The hosted Worker is ST-MCP and answers only at `agents.simplewithus.com`.  The old host gets a 404, with no alias or redirect.  The Worker script name stays `agent-sync-mcp` so that the `agent-sync-mcp.SeatGate` Durable Object namespace keeps its state (`wrangler.jsonc:15-24`).  Remote clients must reconnect and re-authorize.  #849 says it was not deployed. |
| #851, Operator rename (MM) | Open, conflicting, changes requested | The listener becomes the Operator (`com.jay.operator`, `operator.service`).  `listener_*` tools become `eavesdropper_*` with no aliases.  Seats may not open DMs (`dm_policy.py`).  Python module names, the `/listener/v1/` read API, the hosted host and CLI binary names are left for later. |
| #837, hosted listener on Hetzner | Open, preparation only | Moves the listener daemon and its signed read API to a Coolify app on Hetzner.  It says "Nothing about the bridge changes;  seats keep using the Worker MCP as today."  Its layout is "Operator decides, GHR and agent-sync execute".  GHR is now git-relay and GR-MCP (#848). |
| #846, Muster Wake Settings (MM) | Open | Reads `~/.agent-sync/listener.toml` and the wake ledgers, and edits seat wake settings and the Operator options. |

What is being renamed or retired:  the agent-sync listener and daemon naming (now the Operator), the `listener_*` tools, the `agent-sync.jays.services` host, `[seat.JET]` in `listener.toml`, and probably in time the listener-to-Worker wake path (`agent-sync-wake/1`, `/internal/wake/<SEAT>`, `WAKE_HMAC_KEY_JET` and `JET_ROUTINE_KEY`).

What persists for now:  a hosted MCP Worker as the service plane (ST-MCP today), its SeatGate namespace, and MCP Events delivery into Jet's subscription.

### Rules this design follows

1. **No dependency on retiring surfaces.**  The bridge uses none of these:  `daemon.py`, `listener.toml` or `[seat.JET]`, `agent-sync-wake/1`, `/internal/wake/<SEAT>`, `WAKE_HMAC_KEY_JET`, `JET_ROUTINE_KEY`, the `listener_*` or `eavesdropper_*` tools, the `/listener/v1/` read API, or the Operator's wake ledger and budgets.  Renaming or retiring any of them does not touch the bridge, and the bridge does not block the Operator.
2. **One module behind one interface.**  Everything WhatsApp-related in the Worker lives in one directory (working path `scripts/agent-sync-mcp/src/wa/`) and is registered through one call that adds an event definition, three tool specs, the internal routes and its storage tables.  Existing files change only at the registration points (the event registry, route dispatch, policy classification and the tool list) plus the subscription grouping in `src/seat-state.js`, the new `redactSecret` in `src/contract.js` and the owner pin in `src/config.js` and `src/access.js`, as section 5 lists, and tests cover each one.  If ST-MCP is renamed, split or replaced, the module moves as a unit.  If it moves to a different Worker, the bridge changes only its configured base URL and key.
3. **Storage that can move.**  The tables carry a `wa_` prefix.  A Durable Object namespace cannot be renamed without orphaning its state (`wrangler.jsonc:15-24`), so a move is planned as a drain:  SEND off, let bodies be acknowledged or expire, then export the binding and the ledger hashes to the successor.  Short body retention keeps that move cheap.  Whether the tables sit in SeatGate or in their own Durable Object class is part of the placement choice below.
4. **No host in code.**  The bridge reads its base URL from configuration, today `https://agents.simplewithus.com`.
5. **Neutral working names.**  Nothing is named after agent-sync, the listener or the Operator.  The names in this document are placeholders until Jay and Jet choose:  `wa-bridge` (the poller), `wa-bridge/1` (the ingest contract), `/internal/wa-*/JET` (the routes), `WA_BRIDGE_HMAC_KEY_JET` (the signing key), `whatsapp.message` (the event), `whatsapp_read`, `whatsapp_reply` and `whatsapp_send_status` (the tools), and `WHATSAPP_BRIDGE_*` (the knobs).
6. **Muster stays separate from the Operator's settings.**  Nothing is added to `listener.toml` or to the Wake Settings tab of #846.  The switches are Infisical knobs, and Muster later gets a read-only health card fed by the Worker's content-free status.
7. **Sequencing.**  The Worker module lands after #851 merges, because #851 also edits `index.js`, `config.js` and `hosted-tools.js`, and it builds on #849.  The #849 cutover forces Jet to reconnect when it is deployed.  If Jay picks a new OAuth scope for these tools, it can ride that reconnect only if this work is approved before that deploy.  Otherwise the JET-only allowlist needs no re-consent.
8. **The DM rule.**  Under #851 seats may not open DMs, so alerts never use one.  They go to Sentry, content-free.

### Placement:  a choice for Jay and Jet

Where the Worker side lives:

- **W1.  A module in ST-MCP, the current Worker,** on the same connector as `zulip.mention`.  For:  it is the only route known to reach Jet's existing conversation, and the spike tests exactly this.  Against:  it grows a Worker whose naming is still settling, which the module boundary contains.
- **W2.  A separate Worker and connector for WhatsApp.**  For:  fully independent of ST-MCP.  Against:  a second connector in Jet's conversation with its own OAuth, and it is unknown whether its events land in the same conversation, so it needs a second spike.
- **W3.  Inside whatever succeeds ST-MCP, once that is named.**  For:  no later move.  Against:  it waits on that decision.

Where the poller lives:

- **P1.  Its own systemd unit and OS user on the Hetzner box.**  The earlier proposal.  Independent of Coolify and the Operator.
- **P2.  A Coolify app in the "Fleet Infra" project,** like the hosted listener in #837.  Runtime-only secrets, because Coolify bakes build-time variables into build logs.
- **P3.  A Worker Durable Object poller on an alarm.**  It keeps the WhatsApp key off the shared box, but needs a spike on 25-second long polls from an alarm and a hand-installed Worker secret for the key.
- **P4.  A part of the Operator.**  Against:  it couples the bridge to the Operator while its naming and scope are still moving, and Jet asked to keep the listener and wake component distinct from the tool server.

My recommendation is W1 with P1 or P2, behind the module boundary.  The choice is Jay's and Jet's.

## 13. The Live Spike:  Exact Scope

Approved by Jay (my 6:16am post, https://simplewithus.zulipchat.com/#narrow/near/633929990).  It answers one question:  can Jet's existing conversation hold a second event subscription, next to `zulip.mention`, and be woken by it?

**In scope, live:**

- A flag-gated `fleet.spike.ping` event in ST-MCP, advertised only while the flag is on.  It carries no content:  no message text and no Zulip or WhatsApp data.
- Its own `SPIKE_HMAC_KEY`, a new Worker secret that signs only the spike's fire route.  It is not `WAKE_HMAC_KEY_JET` and not `JET_ROUTINE_KEY`.  The key is generated once, kept in AFC Infisical (prod), and installed with `wrangler secret put`.
- The two Worker changes from section 5:  the cap counted per event name, and delivery filtered by event name.
- One fired event.  CLAUDE fires it once, after Jet subscribes from its existing conversation.  Jet reports in the topic whether the conversation woke and what the event looked like.
- `zulip.mention` unchanged and regression-tested in the same PR.  After the fire, one @-mention proves the Zulip wake still works.
- The flag stays on after the test until Jay says otherwise.
- Steps:  review and merge of the spike PR, a hand deploy of ST-MCP (merging does not deploy), the one Worker secret, Jet's subscription, the one fire, Jet's report, and the one Zulip check.  Another CLAUDE worker is building the spike on branch `claude/afc-477-second-event-spike`.

**Not in the spike:**  no WhatsApp credential is read, no WhatsApp API call is made, and nothing is stored, forwarded or sent.  No machine identity, no Infisical identity, no host setup and no change to the listener or Operator.

**Pending specific approval after the spike:**

- The new machine identity for the bridge.
- The bridge's signing key (`WA_BRIDGE_HMAC_KEY_JET`, working name).
- Host setup:  the unit or app, its OS user, egress and state directory.
- Ongoing message forwarding:  any read of `/jet/WHATSAPP_API_KEY`, and turning POLL, WAKE or SEND on.
- Also:  the WhatsApp key rotation, the OAuth scope or allowlist, deploying the Worker module and its tables, the Sentry monitor, and the knob entries.

## 14. Credential And Access Scope

- **The key:**  AFC Infisical, prod, `/jet/WHATSAPP_API_KEY`, as a reference only.  The bridge reads `WHATSAPP_API_KEY` from its own process environment and nowhere else.  The value is injected at start by `fleet-secrets run --only WHATSAPP_API_KEY --path /jet -- <bridge>`, running under the bridge's own identity, so the bridge never calls Infisical itself.  The same call, with the other names added to `--only`, injects the signing key and the switch values.  If any of them is missing or empty, the bridge exits non-zero at once with a message naming the variable and its source, for example `WHATSAPP_API_KEY missing from the process environment (expected from fleet-secrets run --path /jet)`, and never starts polling.  It never prints the value.  The value may start with `WHATSAPP_API_KEY=`, so the bridge strips exactly one leading occurrence in the process and never sources or evaluates it.  The Authorization header is redacted from logs, and errors are logged as code and `fbtrace_id` only.
- **The bridge identity** (working name `wa-bridge`):
  - Universal Auth, no organization role, project role No Access.
  - One extra privilege:  Secrets Read and Describe, env prod, path exactly `/jet` (not `/jet/**`).  If the installed Infisical supports name conditions, also limit it to the bridge's own names.
  - Client-secret trusted IP set to the bridge host's address, and a short token lifetime.
  - Its own credentials come from a root-only 0600 file through systemd `LoadCredential`, or a Coolify runtime-only secret.  Never the shared `/root/.config/fleet-secrets/bootstrap.json`.
  - Before go-live, prove that reads of other AFC folders and of Fleet-OPS fail, and that every write fails.
- **The signing key** `WA_BRIDGE_HMAC_KEY_JET` (working name):  in `/jet`, and installed as a Worker secret.
- **The switches:**  their values sit in `/jet` beside the key and arrive through the same `fleet-secrets run` call, so the bridge identity never needs the AFC root folder, which `fleet_knobs.fetch` would export whole (`scripts/fleet_knobs/source.py:116-127`).  Whether the knob registry and `INFISICAL.md` can list keys outside the root folder still needs a check.
- **Remaining risk:**  root on the shared box can reach `/jet` through the shared automation identity, and can read the bridge's memory.  Agent sessions on that box run as root.  Options for Jay:
  - Accept it.
  - Move the key to its own Infisical project that only the bridge identity can read.  That blocks Infisical reads, not memory reads.
  - Poll from the Worker (P3), which keeps the key off the box.
- **Rotate before standing access.**  The key has been inside Jet's cloud workspace.  Rotating it also locks out any stray poller still running there.

## 15. Why Not The whatsapp-agent Package

- It has one maintainer, a repository about a month old, and one release.
- Its retry, offset and read-receipt behaviour conflicts with this design (section 11).
- Its MCP server has no auth, acts as a second poller and reaches the local filesystem (section 11).
- Never run its `doctor` command, because it polls and would evict the bridge.

Instead, the bridge is about 150 lines written against Meta's manual for the three endpoints it needs:  `GET /agent/v1/updates`, `POST /agent/v1/messages` and `POST /agent/v1/statuses`.  The manual and the package source are references, read as text.

The service is Meta's own WhatsApp Agent Platform, which carries the lowest ban risk of the available routes.  Its terms are new, and the points in assumption A8 are not yet verified.

## 16. Ops Changes That Need Approval

1. **The bridge host**, in one of P1 to P3:
   - P1:  OS user `wa-bridge`, always-on unit `wa-bridge.service` with `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, `ReadWritePaths` for the state directory only, `UMask=0077`, `LimitCORE=0` and `LoadCredential`.  Its start command is `fleet-secrets run --only <names> --path /jet -- <bridge>` (section 14).  It runs from a pinned release directory and restarts explicitly on deploy, not from the hourly-updated fleet CLI checkout.
   - State in a 0700 directory of a few kilobytes, logs to journald, nothing in /tmp.
   - The process gets a row in the processes list (`/Users/jay/apps/MAC-LOCAL-PROCESSES.md`) and its Apple Note.
   - Jay decides whether the failing units already on the host are cleared first.
2. **Egress from the bridge host:**  `api.whatsapp.com:443`, the Worker host (`agents.simplewithus.com`), Infisical and Sentry ingest.  No inbound port, no DNS record, no tunnel, and no media host.
3. **Infisical:**  the bridge identity and the signing key in `/jet`.  Jay rotates the WhatsApp key in WhatsApp (Settings > Agents > the agent > API key) and saves the new value.
4. **Cloudflare:**  a hand deploy per `scripts/agent-sync-mcp/DEPLOY.md` (merging does not deploy), `wrangler secret put` for the signing key, the new tables, and the KV namespace if crypto-shredding is chosen.  A cron trigger only if Jay wants re-rings while the bridge is down (section 8).
5. **OAuth:**  either a scope only JET can hold, which needs one re-consent best bundled with the #849 reconnect, or a JET-only allowlist in `config.js` with no re-consent.
6. **Knobs:**  `WHATSAPP_BRIDGE_ENABLED=false`, `WHATSAPP_BRIDGE_WAKE_ENABLED=false`, `WHATSAPP_BRIDGE_SEND_ENABLED=false`, `WHATSAPP_BRIDGE_WAKES_PER_HOUR=20` and `WHATSAPP_BRIDGE_WAKES_PER_DAY=100`, with registry entries and a regenerated `INFISICAL.md`.  The Worker gets its own admin switches.
7. **Sentry:**  one Crons monitor for the bridge.
8. **Jet:**  stop any poller in its cloud workspace and delete the key copy there.

## 17. Test Plan

### Bridge, against a fake WhatsApp server

- A reset during a poll, a 5xx, and a 429 with and without `Retry-After`:  it backs off, the offset is unchanged, and nothing is lost.
- 400 with code 100 and 401 with code 190:  it stops, with no tight loop.
- A second poller (409, code 1752041):  it stands down, then retries once after 5 minutes.  A second conflict within 30 minutes stops it until someone flips a switch or restarts it, so it never ping-pongs.
- A replayed batch:  one stored row and one ring.
- A batch that stores nothing (all pre-binding, quarantined or receipts only):  the processed-batch acknowledgement still carries `next_offset`, and the offset advances.
- Each boundary-schema rule broken in turn:  nothing is forwarded, the offset stays, the log holds only the field path and rule, and three identical failures stop polling.
- A replay after the inbox rows were deleted:  the ledger drops it, and nothing rings.
- A message whose Meta timestamp is before the binding cutoff, delivered after the binding:  dropped as `pre_binding`.
- A replay batch after the ledger lost its rows:  messages older than the high-water mark are dropped.  A message more than 31 days old is dropped as `stale`.
- `kill -9` between poll and ingest, between ingest and the offset save, and during a send:  nothing is lost, nothing rings twice, and the in-flight send becomes `outcome_unknown`.
- Deleting the state directory:  no old message wakes Jet again.
- A backlog from before activation or binding:  ignored.
- A message from another sender:  quarantined, with no body stored anywhere.
- Every send outcome in the section 10 table, including 500, a reset, a read timeout and a 503 without 131016:  `outcome_unknown`, never sent again, and visible in the tools.  429 and 503 with 131016:  bounded retries.
- A repeated `client_ref`:  one send.
- Each switch off:  the bridge stops within 60 seconds, and the disable file stops it at once.
- Logs searched for the token and for test text:  neither is found.
- `WHATSAPP_API_KEY`, the signing key or a switch value missing from the environment:  the bridge exits non-zero naming the variable and its source, and never polls.
- Credential reads outside `/jet`:  denied.

### Binding

- No binding:  nothing is stored or woken.
- The right code from the creator:  bound.  The right code from another sender (the creator check answers 403):  refused.  A wrong code three times from one sender:  that sender is ignored for the rest of the window, and the window stays open.  An expired window:  refused.
- An inconclusive creator check (500, 503 or a timeout):  retried, then no binding.
- A re-check answering 403, and a send answering 403 with 131005:  `suspect`, sends held.
- A changed sender id or agent id:  quarantined, then re-bound only through `/admin`.
- The bridge and the Worker compute the same keyed hash for the same code, and a hash made for another window id does not match.
- A stranger sending three wrong codes is ignored for the rest of the window, and Jay's correct code from his own chat still binds in that window.
- Messages that are not code-shaped cost no attempt.
- Every window close, by expiry, use, cancel or a refused creator check, raises a content-free alert, and the candidate rows are gone afterwards.

### Worker

- The existing `zulip.mention` and JET tests pass unchanged.
- Cross-delivery is impossible in both directions.
- One conversation can hold both events, and a second conversation is refused for each.
- Wake ids are kept apart.
- A bad HMAC, a stale `sent_at`, or `JET_ROUTINE_KEY` used on the new routes:  refused.
- The event payload carries no text and no ids.
- The tool contract:  `ack_through` for a `seq` not yet returned is refused with `not_surfaced`, and an unread message still gets its stub and alert at expiry.  A second `whatsapp_read` returns a count, not the body, and `include_surfaced: true` returns the body.
- Idempotency:  a repeat after `sent` returns the cached intent.  A repeat after `outcome_unknown` is refused and sends nothing.  A repeat after a definite 403 with 131005 makes a new linked attempt that keeps the old code.  A new reply with the same text as an `outcome_unknown` reply is refused.
- Argument rules, checked before any idempotency row is touched:  a body over 64 KiB, nesting of 17 levels, 1,001 nodes, a self-referring structure, an unknown argument, a numeric or blank `client_ref`, and a `seq` given as a number, a blank string or with a leading zero are all refused.
- The secret scan:  an inbound body with a known key shape is stored as `[redacted:<rule>]`, the log holds only the message-id hash and the rule, Jet sees the `redacted` marker, and the message still arrives.  A reply with a key shape is refused with `secret_in_reply`.
- The owner pin:  an unset or empty `OWNER_EMAIL`, a list, or a different Access identity cannot open a binding window, and the config test fails closed on the same cases.
- A claimed reply declined because SEND is off returns to `held`, never `outcome_unknown`.
- Retention:  bodies are not returned after expiry, the sweep deletes them, the 35-day ledger outlives them, and an unread expiry leaves a stub and raises an alert.  With crypto-shredding, a row restored after its day key expired cannot be decrypted.
- SEND off:  queued and new replies become `held`, nothing is claimed, the in-flight send completes, and re-enabling does not release held replies without a release.
- A lapsed subscription:  `delivered: 0`, then retries on the heartbeat, then one catch-up ring on re-subscribe.  No message is lost.
- The coexistence boundary:  a test fails if the WhatsApp module imports any listener, eavesdropper or wake module, or reads a `WAKE_HMAC_KEY_*` or `*_ROUTINE_KEY`.

### End to end with Jet (live, after the key rotation, each phase approved)

1. Jet's existing conversation holds both subscriptions, as shown by the event summary on `/admin`.
2. Jay binds the chat with a code.
3. Jay sends "AFC-477 test 1", Jet's conversation gets a turn within about 60 seconds, and Jet reads the message with `whatsapp_read`.
4. Jet replies with `whatsapp_reply`, Jay receives it, and the ledger shows `sent` and later `read`.
5. A Zulip mention still wakes Jet as before.
6. Restarting the bridge mid-conversation loses nothing and duplicates nothing.
7. Turning POLL off stops polling, and turning it back on resumes from the saved offset.
8. Searching Zulip, including #alerts and DMs, for the test strings finds nothing.
9. Health shows running, then disabled.

Uncertain sends are tested against the fake server only, never live.  Nothing is called always-on or complete until a real WhatsApp message wakes Jet's existing conversation and Jay receives Jet's reply.

## 18. Rollout

Each phase needs Jay's go-ahead:

0. The spike (section 13):  a content-free test event, no WhatsApp.
1. Binding and read-only:  POLL on, WAKE off, SEND off.  Jay binds the chat.
2. WAKE on.
3. SEND on.

## 19. Decisions For Jay And Questions For Jet

### For Jay

1. Approve the design direction.  Nothing with standing access is built or deployed before that.
2. Placement:  W1, W2 or W3 for the Worker side, and P1, P2, P3 or P4 for the poller (section 12).
3. Names:  keep the working names, or choose others once the replacement's naming settles.
4. Body retention:  7 days, or 24 hours after Jet acknowledges, whichever is first, or other numbers.  And accept point-in-time recovery, or crypto-shred (section 7).
5. The binding ceremony through `/admin` and the creator check (section 6).
6. Held replies when SEND comes back on:  released one by one by Jet (recommended), or automatic release for short holds (section 9).
7. A connection that never opened:  retry it as not sent (assumption A4), or treat it as `outcome_unknown`.
8. A new OAuth scope with a re-consent, best bundled with the #849 reconnect, or a JET-only allowlist.
9. The remaining risk from root on the shared box (section 14).
10. Rotating the WhatsApp key.
11. The wake budget, and whether WhatsApp has its own.
12. Whether agent-chat text may be relayed to ChatGPT under Meta's terms, once those terms are verified (assumption A8).
13. Read receipts and typing indicators.  The creator re-check shows at least one read receipt every 7 days.
14. A Worker cron trigger for re-rings while the bridge is down, or not.

### For Jet

1. In the spike:  can your conversation subscribe to the test event next to `zulip.mention`, and is the callback URL the same for both?
2. Would subscribing to a new event need a fresh consent on your side?
3. Is any poller still running in your workspace, and can you delete the key copy there?
4. Is a metadata-only payload enough, or do you want a short excerpt?
5. Do the read-once surfacing, the acknowledgement (`ack_through`) and the held-reply release fit how your conversation works?
6. Are you a published workspace agent?

## 20. Checked And Not Checked

Checked:  the Worker and listener code on `main` at `2af99bec`, the state of PRs #837, #844, #846, #849 and #851 at about 6:25am today, the Meta manual and the `whatsapp-agent` 0.1.0 source as cited above, Cloudflare's documentation on point-in-time recovery, and Jet's own report from 5:55am.

Not checked or not live-tested:  whether ChatGPT accepts a second event in one conversation (the spike), the state of Jet's live subscription, whether #849 has been deployed, how WhatsApp behaves beyond its manual, Meta's terms (assumption A8), and Workers KV's behaviour for expired keys (assumption A10).
