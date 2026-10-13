# Zulip Wake Audit

Owner preference: Muse Spark 1.3 Contributor through `muse exec` is the first-choice tool-less router; MiniMax M3.1 Flash Preview through `mcode` is second.  The CLAUDE seat identity remains CLAUDE when its routing runtime changes.  A model substitution does not rename a seat or change another chat's model.

## Verified Coverage

- Mac listener: nine seat event queues connected.  CLAUDE was the only headless runtime; the other eight seats were inbox-only.  Inbox delivery alone does not prove a dormant chat resumes.
- Coolify listener: JET and eleven GB roles had HTTP routes ready.  MA, GROK-WEB, INSTINCT and ECHO remained inbox-only.  Jet acknowledged current event-driven deliveries; a later report of a delayed receipt still needs latency tracing.
- BotFleet: native Zulip was disabled with no bot mappings.  BF-2418 enabled all eleven existing roles using their existing Infisical credentials.  All eleven connected and received a dry-run mention; dry-run mode is now off.  A live Director mention reached its work queue and started a turn.  Director replied in Zulip (631398291), verifying the full path for that bot; the other ten have dry-run delivery evidence.
- Muse: the original shell/write/web disable flags still allowed disclosure of a synthetic outside-workspace read marker.  Explicit `exec -c 'run.toolset=[]'` plus disabled reminders passed the same read/write canary and the standard hostile-message test on versioned binary 1.4.7-R6101.1.  The exact executable is pinned; the auto-updating launcher is unsuitable as a version pin.  This is bounded test evidence.
- MiniMax: PR #509 remains a separate review lane.  Installed mcode 0.6.2 does not support its proposed lightweight flag, and the exact requested Flash Preview model identifier has not been verified.  Do not silently substitute a different MiniMax model.

## Direct Webhooks

Zulip outgoing-webhook bots can POST a DM or direct mention to an endpoint.  They do not forward every channel message.  The handler should acknowledge promptly and enqueue work; the bot posts a later reply through Zulip's API.

BotFleet already implements these wake semantics through per-bot Zulip event queues, with persistent message-id deduplication, busy-bot queuing, sender screening, and replies under the original role identity.  Its generic routine webhook endpoint is not a Zulip outgoing-webhook receiver.  Direct outgoing mode would need authenticated ingress, token-to-bot mapping, Zulip payload normalization, shared deduplication with the event consumer, and health/latency metrics.  Existing generic bot accounts must not be converted without checking fleet sender-policy compatibility.

Native event delivery is the enabled path.  Direct outgoing mode is an optional additional transport, not deployed by this audit.  Official references: https://zulip.com/help/outgoing-webhooks and https://zulip.com/help/bots-overview.

## Reusing Shellular Discovery

The installed Shellular runtime handles `AI_SESSION_LIST` by querying initialized ACP adapters that advertise `session/list`.  It returns session ID, title, workspace path and timestamps.  This can enumerate existing chats without sending a user prompt, although adapter startup can spawn a process or contact its service.

Use a shared inventory record keyed by `(host, platform, session_id)`, with title, workspace, updated timestamp, last successful discovery, supported capabilities and a live/busy indication.  Never treat a display title as identity.  A stale or unavailable adapter must preserve a last-known record marked stale, not make its chat appear newly eligible for creation.

Routing order:

1. Explicit chat ID and saved Zulip topic binding.
2. Deterministic seat/platform/host matching within discovered chats.
3. Tool-less semantic selection from a bounded list of known IDs only.
4. Hold ambiguity and ask for a choice; never invent a session ID or open a replacement chat automatically.

Read transcript excerpts only if metadata cannot resolve the target.  Shellular's `AI_SESSION_ATTACH` can use `session/load`; this is separate from prompting but adapter behavior varies.  Grok's live load can hang, so use its existing disk `peek` for summaries.  Cursor Cloud lacks ACP session-list support in the installed custom bridge; use the existing Cursor API listing helper instead.  Discovery must have per-adapter timeouts and isolated failures.

References in AFC: `scripts/grok-acp-runtime/leader-client.py` (metadata, peek, session list/load), `scripts/grok-acp-runtime/README.md`, and `scripts/cursor_chat_surfaces.py` (cloud and local listing).  The installed Shellular bundle is `~/apps/shellular-runtime/node_modules/shellular/dist/main.js`; custom adapter configuration is `~/.shellular/agents.json`.  Do not export adapter environment values into the registry.

Before dispatch, revalidate the selected record and preserve its recorded host and existing chat model.  Resume through that platform's adapter, and queue behind active work instead of interrupting it.  New-chat creation is a separate authenticated owner action; Jet's agreed default for a newly requested chat is Coolify, subject to supported platform/host availability.

## Remaining Work

AFC-305 tracks registry implementation and dormant-chat resume coverage.  This audit does not claim those exist.  AFC-306 tracks the current listener defect that can mark held messages surfaced after invalid output or a refused action; coordinate with the overlapping PR #509 lane.  BF-2418 tracks live BotFleet enablement and the reply receipt.  These gaps must remain explicit when reporting universal wake coverage.
