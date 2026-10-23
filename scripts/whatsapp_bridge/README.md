# WhatsApp Wake Bridge For Jet

AFC-477 implementation, disabled until the approved rollout.  Claude confirmed in public Work-In-Progress message 634168889 that his AFC-477 work is the merged design and spike only, with no overlapping implementation and no named Worker deployment owner.

Jay's verified WhatsApp messages wake Jet automatically, without an importance classifier.  Text beginning exactly with `👀` is stored but does not enqueue a wake.  An interior emoji does not suppress anything.  Non-text messages produce metadata-only inbox entries and wakes; media downloading is outside this unit.

The poller runs independently of Operator.  It sends signed batches to the Worker, which stores private text and emits content-free `whatsapp.message` events in Jet's existing conversation.  No WhatsApp content goes to Zulip.  Each event says `source: authenticated_owner` or `source: operator`; Operator events explicitly carry `owner_authorized: false`.  The owner flag describes authenticated transport provenance, never blanket approval for arbitrary actions in message text.

## Source And Tests

- `bridge.py`: standard-library sole long-poller, local flock, SQLite FULL synchronous offset, bounded schema validation, HMAC ingestion, exponential backoff and explicit processed-batch acknowledgments.  Offsets retain all signed 64 bits and advance only on an exact string match.  Startup uses offset 0 and the Worker's verified binding cutoff suppresses history.
- `binding.py`: a supervised one-time-code plus Meta creator-check helper and temporary binding mode.  `python3 -m whatsapp_bridge.binding` temporarily owns the same poller lock, requires the long-running bridge to be stopped, and leaves the durable cursor unchanged.  Running its read-receipt check live requires action-time approval.
- `operator_wake.py`: an explicit, content-free Operator doorbell with its own HMAC key and stable request UUID.  The Operator can call `python3 -m whatsapp_bridge.operator_wake <request-uuid>` from `scripts/` after its binding is approved.  Reusing the UUID is idempotent; there is no automatic retry loop.
- `../agent-sync-mcp/src/wa/`: authenticated ingestion, private read/ack tool, transactional permanent replay tombstones, pending doorbells, content-free event delivery and body retention.

```sh
cd scripts
python3 -m unittest discover -s whatsapp_bridge/tests -t . -v
cd agent-sync-mcp
npm ci --ignore-scripts
npm test
npm run test:workerd
WRANGLER_SEND_METRICS=false npm run bundle
```

The exact Meta manual referenced by the design was re-read, with SHA-256 `ba677cdb476b52c9126a0e7565616131104f240b461428e8b388059a71c506e0`.  It confirms the Bearer header, updates endpoint, `user:` identifiers, signed 64-bit offsets, 30-day replay window, 204 idle response, creator check on statuses, and a competing poller producing 409.  Reference copy: [Meta Developer Manual](https://github.com/abdullah3509/whatsapp-agent/blob/main/docs/Agent%20Platform%20Guide.pdf).

## State And Failure Semantics

The bridge persists only its offset, never message bodies or credentials.  It locks the state directory locally and stops on HTTP 409 rather than fighting a poller elsewhere.  Credential/refusal errors 400, 401, 403 and 404 stop it.  Network failures and 5xx retry the same offset; 429 respects numeric Retry-After, up to one hour per wait.  Three consecutive schema failures stop polling.  Redirects are refused.  The local `disabled` file stops it within the current request timeout (40 seconds), including during backoff.

The Worker atomically stores each message, its dedupe tombstone and its pending wake.  Replayed batches acknowledge the original offset even when zero messages are stored.  A paused seat still stores messages and delivers nothing.  Missing/lapsed subscribers or failed callbacks leave pending wakes for the next signed poll heartbeat.  Stable event ids let the supported MCP receiver deduplicate a retry after an ambiguous callback acknowledgment.  Bursts of the same provenance are coalesced into a doorbell.  The Worker leases delivery for 30 seconds; a crash leaves a reclaimable pending wake.

`whatsapp_read` is Jet-only, reusing its existing read scope and enclosing text as untrusted data.  It surfaces at most 50 messages per call, returning each once unless `include_surfaced` is true.  `ack_through` is a canonical digit string and cannot acknowledge a message an earlier call has not surfaced.  The inbox uses bounded pages and durable surface/ack checkpoints.  Bodies are hidden at seven days, or after acknowledgment; the hourly paginated alarm physically clears them at seven days or 24 hours after acknowledgment.  Content-free tombstones, acknowledgement checkpoints and expiry stubs are retained, so body deletion or losing local poller state cannot ring old messages again.  Physical deletion can lag by the hourly sweep, plus pagination and platform alarm delays.  This is ordinary storage deletion, not crypto-shredding or removal from provider backups.

There is no new daily or hourly wake quota.  Meta's 30-day buffer, the callback's subscription expiry (24 hours by default in this connector), callback service limits, Worker resource/binding limits and deployment approval are real operational constraints.  A functioning bridge retries pending deliveries on its next poll; bridge downtime delays recovery.  No Worker cron or persistent service is installed by this change.

This unit implements receiving, waking and reading.  It deliberately exposes no WhatsApp reply/send tool and performs no outbound message sends, so it cannot blindly retry an ambiguous outbound message.  The design's reply outbox, send ledger, media fetching and admin binding UI remain separate work under AFC-477.

## Approval And Activation Checklist

Nothing below was executed as part of implementation.  Do not create credentials or activate a credential-bearing process until the parent obtains action-time approval for the exact action.

1. Assign the hand-deploy owner for the existing ST-MCP Worker and approve poller placement.  Preserve the existing Jet OAuth grant, private conversation and all Zulip subscriptions; do not reconnect or change authentication as a workaround.  Existing spike flags and keys stay untouched.
2. Approve the retained-body policy or change it before activation.  Approve the dedicated least-privilege Universal Auth runtime identity, pinned release directory and state directory.  Source settings and credentials from AFC Infisical `prod` `/jet`, with no shared automation identity or token-mode workaround.  Rotation of the WhatsApp API key and stopping every other poller are separate coordinated actions.
3. Approve provisioning two independent secrets: `WA_BRIDGE_HMAC_KEY_JET` for the poller and `WA_OPERATOR_HMAC_KEY_JET` for Operator, each at least 32 characters and distinct from every existing wake, routine, health and spike key.  Install the corresponding Worker secrets only after source-of-truth provisioning is approved.  Never give Operator the bridge key.
4. Approve a supervised binding ceremony.  The owner starts a ten-minute window, sends a one-time `JET-` plus 16 uppercase hex code in the intended chat, and, with the regular poller stopped, the approved operator invokes `python3 -m whatsapp_bridge.binding` from `scripts/`.  The runtime requires `WHATSAPP_API_KEY`, `WA_STATE_DIRECTORY`, `WA_BINDING_CODE`, `WA_BINDING_OPENED_AT` and `WA_BINDING_EXPIRES_AT`; it owns the same lock and polls only for that supervised window.  Its stdout is the private binding JSON, for the approved provisioning channel only.  `binding.verify_candidate` checks the code and timestamp, then makes Meta's creator check with a read receipt.  Only a confirmed `success: true` yields the binding.  Consume the ceremony code and install the resulting JSON as `WA_OWNER_BINDING_JET` through the approved deployment channel.  Do not label a first-arrival message as owner-authenticated.  There is no automatic admin UI or re-verification scheduler in this unit; binding changes and periodic creator re-verification remain supervised operations.
5. Provision Worker switches `WHATSAPP_BRIDGE_ENABLED=on` and `WHATSAPP_BRIDGE_WAKE_ENABLED=off` for initial storage-only acceptance, after checking available Worker binding capacity.  Hand-deploy the merged code per `../agent-sync-mcp/DEPLOY.md`; merge alone does not deploy.  New state stays in the existing Jet SeatGate, with no new class or migration.
6. Jet subscribes to `whatsapp.message` with exactly the callback of its existing `zulip.mention` subscription.  A new callback/conversation is refused in both the precheck and atomic upsert.  The spike subscription need not change.
7. Approve activation of exactly one pinned poller with environment `WHATSAPP_API_KEY`, `WA_BRIDGE_HMAC_KEY_JET`, `WA_WORKER_URL`, `WA_STATE_DIRECTORY`, `WA_AGENT_HASH_JET` (from the verified binding) and `WHATSAPP_BRIDGE_ENABLED=on`.  `WA_WORKER_URL` must be an HTTPS origin.  The runtime needs outbound HTTPS only.  Add its process inventory and health monitoring when its host and service are approved; no process is registered as installed today.
8. After stored-message acceptance, separately approve `WHATSAPP_BRIDGE_WAKE_ENABLED=on` and Operator's distinct key binding.  Test a received WhatsApp owner message causing an actual Jet turn, then a separate leading-👀 message remaining readable without a turn.  Test an Operator doorbell with false owner authorization, an existing Zulip wake, restart/replay, and the disable switch.  Record receipts and timings without private text.  Only these live receipts establish completion.

Switching the Worker bridge flag off refuses ingestion; the poller stops on that 404 and requires an intentional restart after re-enabling.  Switching only the wake flag off keeps storage and offset acknowledgments active.  No subscription, credential, service, network/security policy or production setting was changed by this code task.
