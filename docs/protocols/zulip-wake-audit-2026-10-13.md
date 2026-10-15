# Zulip Wake Audit

Owner preference: Muse Spark 1.3 Contributor through `muse exec` is the first-choice tool-less router; MiniMax M3.1 Flash Preview through `mcode` is second.  The CLAUDE seat identity remains CLAUDE when its routing runtime changes.  A model substitution does not rename a seat or change another chat's model.

## Validation and Scope

Muse shell/write/web disable flags alone allowed disclosure of a synthetic outside-workspace read marker.  Explicit `exec -c 'run.toolset=[]'` plus disabled reminders passed the same read/write canary and the standard hostile-message test.  This is bounded evidence, not a universal containment proof.  Pin a tested versioned executable instead of an auto-updating launcher, and require a new test after the command or wake contract changes.

Live deployment inventory, credential-source configuration and message receipts are maintained outside repository source in the private fleet inventory and owner Completion note.  Do not infer active coverage from the supported adapters described here.

## Direct Webhooks

Zulip outgoing-webhook bots can POST a DM or direct mention to an endpoint.  They do not forward every channel message.  The handler should acknowledge promptly and enqueue work; the bot posts a later reply through Zulip's API.

BotFleet already implements these wake semantics through per-bot Zulip event queues, with persistent message-id deduplication, busy-bot queuing, sender screening, and replies under the original role identity.  Its generic routine webhook endpoint is not a Zulip outgoing-webhook receiver.  PR #1138 adds authenticated ingress, token-to-bot mapping, payload normalization, shared deduplication, and draining controls; the public receiver health check was still failing on 2026-10-14, so no bot identity is ready for cutover.  Follow [the migration runbook](zulip-outgoing-migration.md); do not deactivate identities before receiver health, credential consumers, and a real human-authored DM/mention plus reply are verified.

Native event delivery is the existing implementation.  Direct outgoing mode would be an additional transport, with native delivery retained for catch-up and followed topics.  Official references: https://zulip.com/help/outgoing-webhooks and https://zulip.com/help/bots-overview.

## Reusing Shellular Discovery

The installed Shellular runtime handles `AI_SESSION_LIST` by querying initialized ACP adapters that advertise `session/list`.  It returns session ID, title, workspace path and timestamps.  This can enumerate existing chats without sending a user prompt, although adapter startup can spawn a process or contact its service.

Use a shared inventory record keyed by `(host, platform, session_id)`, with title, workspace, updated timestamp, last successful discovery, supported capabilities and a live/busy indication.  Never treat a display title as identity.  A stale or unavailable adapter must preserve a last-known record marked stale, not make its chat appear newly eligible for creation.

Routing order:

1. Explicit chat ID and saved Zulip topic binding.
2. Deterministic seat/platform/host matching within discovered chats.
3. Tool-less semantic selection from a bounded list of known IDs only.
4. Hold ambiguity and ask for a choice; never invent a session ID or open a replacement chat automatically.

Read transcript excerpts only if metadata cannot resolve the target.  Shellular's `AI_SESSION_ATTACH` can use `session/load`; this is separate from prompting but adapter behavior varies.  Grok's live load can hang, so use its existing disk `peek` for summaries.  Cursor Cloud lacks ACP session-list support in the installed custom bridge; use the existing Cursor API listing helper instead.  Discovery must have per-adapter timeouts and isolated failures.

References in AFC: `scripts/grok-acp-runtime/leader-client.py` (metadata, peek, session list/load), `scripts/grok-acp-runtime/README.md`, and `scripts/cursor_chat_surfaces.py` (cloud and local listing).  Inspect the installed Shellular bundle and adapter configuration for current platform capabilities.  Do not export adapter environment values into the registry.

Before dispatch, revalidate the selected record and preserve its recorded host and existing chat model.  Resume through that platform's adapter, and queue behind active work instead of interrupting it.  New-chat creation is a separate authenticated owner action; Jet's agreed default for a newly requested chat is Coolify, subject to supported platform/host availability.

## Remaining Work

AFC-305 tracks registry implementation and dormant-chat resume coverage.  This audit does not claim those exist.  AFC-306 tracks the current listener defect that can mark held messages surfaced after invalid output or a refused action; coordinate with the overlapping PR #509 lane.  BF-2418 records native BotFleet enablement; BF-2419 tracks direct webhook ingress.  These gaps must remain explicit when reporting universal wake coverage.

## Existing-Chat Nudge Delivery

`agent-sync daemon chats` manages explicit per-seat topic bindings and the durable nudge queue.  A binding identifies a host, platform, session ID and workspace; titles are never routing keys.  Only an eligible wake on the exact topic enters this queue.  A bound nudge replaces the headless response for that message; unbound messages keep their existing routing.

The first transport is `grok-leader`, through the shared Grok leader's existing `session/resume` and `session/prompt` methods.  It requires a live Grok process, the exact workspace, and idle session state.  Busy, permission-waiting, disconnected and unknown sessions remain pending.  It never invokes session creation, cancellation, a model change, or an additional agent owner.  A correlated prompt response or the exact nudge in the chat's saved real-user history confirms transport acceptance.  A timeout without either receipt remains uncertain and is never retried automatically.  The worker checks uncertain deliveries for later saved receipts.  Acceptance does not prove that the agent subsequently read or answered Zulip.

The private per-seat `chat-nudges.json` stores bindings, message-ID deduplication and delivery state.  Restarting after an interrupted send marks it uncertain.  Removing or changing a binding does not redirect already queued work to a different chat.  The explicit cancel command retires a held or uncertain item without resending it; it cannot cancel a prompt already being dispatched.  Pause controls, loop protection and the seat's wake-count limits still apply.  Local nudge delivery costs no model tokens; downstream chat spend is unknown and is not capped by the listener's USD budget.

Shellular's installed CLI has no supported local prompt API.  Its private `AI_PROMPT` queue is reached through an already-paired app and encrypted relay.  The fleet listener does not recreate that pairing protocol or import Shellular's private manager.  Other platform transports and dormant-chat resume remain explicit coverage gaps under AFC-305; listing a chat is not proof that its existing owner accepts injected messages.

Local administration (keep the caller's own seat in `--as`; `--target-seat` selects the delivery identity):

```sh
agent-sync --as CODEX daemon chats list --target-seat GROK
agent-sync --as CODEX daemon chats bind --target-seat GROK \
  --channel agent-sync --topic 'ISSUE-123 Work Topic' \
  --session-id '<discovered-session-id>' --cwd '<discovered-workspace>'
agent-sync --as CODEX daemon chats status --target-seat GROK
agent-sync --as CODEX daemon chats cancel --target-seat GROK --item-id '<held-item-id>'
agent-sync --as CODEX daemon chats unbind --target-seat GROK \
  --channel agent-sync --topic 'ISSUE-123 Work Topic'
```

AFC-307 separately tracks the older shared Grok prompt helper's uncorrelated `queued: true` result.  The new nudge transport bypasses that acceptance path.

Topic renames and channel moves migrate explicit chat bindings and pending nudge text.  Conflicting destination bindings remain intact and affected old pending nudges are held for operator review.  Unknown or in-flight payloads remain unchanged for exact receipt reconciliation.  Stale owner backlog retains the normal once-per-topic/day suppression.

## Muse Chat Catalog And Selection

The tool-less Muse router first receives metadata for up to 24 relevant records from a bounded read-only catalog, with a 24,000-character context ceiling.  If metadata is insufficient, `chat_inspect` requests one exact readable record and the listener supplies up to four recent user/assistant messages for one final decision.  A wake permits at most two Muse calls, with no recursive inspection.  The collector scans native stores without starting another ACP owner, excludes tool output and reasoning, and scrubs text before sending it to Muse.  Every direct message or explicit bot mention can initiate this Muse routing path, from Jay or a peer.  Trigger text is untrusted input and may rank bounded metadata, but it cannot grant transcript access or override deterministic safety gates.  Muse may request one excerpt for either sender; Muse makes the relevance and safety judgment, while the listener enforces exact listed/read-capable targets and fixed bounds.  Peer-triggered chat selection still requires Muse to return `risk: low`.

The output field `chat_target` selects one opaque ID from that exact snapshot.  The listener requires a Muse wake, refreshes the record, rejects stale/read-only/unknown targets, verifies unchanged host/platform/workspace, and queues one fixed Zulip reminder.  Peer-triggered selection additionally requires `risk: low`; a private DM can target only a chat belonging to the addressed seat.  The model cannot choose the prompt text, create a chat, change its model, or select a host.  A simultaneous seat route is rejected to prevent duplicate handoffs.  Busy targets stay queued behind existing work; uncertain sends require receipts or explicit cancellation.

Supported stores and transports are distinct.  Claude and Codex native logs provide bounded text, Cursor local provides metadata, and verified native Muse/MiniMax/Fx stores provide the capabilities advertised by each reader.  Grok shared-leader and Cursor Cloud existing-agent runs are the callable wake transports.  Other records are read-only until an existing-owner delivery API is verified.  Missing adapters remain visible as unavailable instead of disappearing or being recreated.

`agent-sync --as CODEX daemon chats catalog --limit 120` lists the bounded catalog.  `agent-sync --as CODEX daemon chats peek --chat-id <opaque-id>` reads up to four recent messages from an exact discovered record.  Catalog and excerpts stay local/in memory; no transcript is added to public source or the durable wake queue.  Cursor Cloud queue status/cancellation use `--target-seat CURSOR`; Grok uses `--target-seat GROK`.

The changed wake schema and contract invalidate the previous Muse pin.  Run the standard hostile-message contract test and review its result before pinning the new contract.  A synthetic positive-selection canary should also verify that the model returns a known wake-capable ID despite hostile transcript text, without dispatching that test selection.
