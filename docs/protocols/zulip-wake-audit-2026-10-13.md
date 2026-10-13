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
