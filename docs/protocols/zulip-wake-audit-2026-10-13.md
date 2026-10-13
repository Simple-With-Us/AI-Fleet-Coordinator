# Zulip Wake Audit

Owner preference: Muse Spark 1.3 Contributor through `muse exec` is the first-choice tool-less router; MiniMax M3.1 Flash Preview through `mcode` is second.  The CLAUDE seat identity remains CLAUDE when its routing runtime changes.  A model substitution does not rename a seat or change another chat's model.

## Validation and Scope

Muse shell/write/web disable flags alone allowed disclosure of a synthetic outside-workspace read marker.  Explicit `exec -c 'run.toolset=[]'` plus disabled reminders passed the same read/write canary and the standard hostile-message test.  This is bounded evidence, not a universal containment proof.  Pin a tested versioned executable instead of an auto-updating launcher, and require a new test after the command or wake contract changes.

Live deployment inventory, credential-source configuration and message receipts are maintained outside repository source in the private fleet inventory and owner Completion note.  Do not infer active coverage from the supported adapters described here.

## Direct Webhooks

Zulip outgoing-webhook bots can POST a DM or direct mention to an endpoint.  They do not forward every channel message.  The handler should acknowledge promptly and enqueue work; the bot posts a later reply through Zulip's API.

BotFleet already implements these wake semantics through per-bot Zulip event queues, with persistent message-id deduplication, busy-bot queuing, sender screening, and replies under the original role identity.  Its generic routine webhook endpoint is not a Zulip outgoing-webhook receiver.  Direct outgoing mode would need authenticated ingress, token-to-bot mapping, Zulip payload normalization, shared deduplication with the event consumer, and health/latency metrics.  Existing generic bot accounts must not be converted without checking fleet sender-policy compatibility.

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

The first transport is `grok-leader`, through the shared Grok leader's existing `session/resume` and `session/prompt` methods.  It requires a live Grok process, the exact workspace, and idle session state.  Busy, permission-waiting, disconnected and unknown sessions remain pending.  It never invokes session creation, cancellation, a model change, or an additional agent owner.  A correlated prompt response confirms transport acceptance; a timeout remains uncertain and is never retried automatically.  Acceptance does not prove that the agent subsequently read or answered Zulip.

The private per-seat `chat-nudges.json` stores bindings, message-ID deduplication and delivery state.  Restarting after an interrupted send marks it uncertain.  Removing or changing a binding does not redirect already queued work to a different chat.  Pause controls, loop protection and the seat's wake-count limits still apply.  Local nudge delivery costs no model tokens; downstream chat spend is unknown and is not capped by the listener's USD budget.

Shellular's installed CLI has no supported local prompt API.  Its private `AI_PROMPT` queue is reached through an already-paired app and encrypted relay.  The fleet listener does not recreate that pairing protocol or import Shellular's private manager.  Other platform transports and dormant-chat resume remain explicit coverage gaps under AFC-305; listing a chat is not proof that its existing owner accepts injected messages.

Local administration (keep the caller's own seat in `--as`; `--target-seat` selects the delivery identity):

```sh
agent-sync --as CODEX daemon chats list --target-seat GROK
agent-sync --as CODEX daemon chats bind --target-seat GROK \
  --channel agent-sync --topic 'ISSUE-123 Work Topic' \
  --session-id '<discovered-session-id>' --cwd '<discovered-workspace>'
agent-sync --as CODEX daemon chats status --target-seat GROK
agent-sync --as CODEX daemon chats unbind --target-seat GROK \
  --channel agent-sync --topic 'ISSUE-123 Work Topic'
```

AFC-307 separately tracks the older shared Grok prompt helper's uncorrelated `queued: true` result.  The new nudge transport bypasses that acceptance path.
