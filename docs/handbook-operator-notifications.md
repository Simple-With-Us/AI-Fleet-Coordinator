# Handbook checker notifications as Operator

Source preparation only.  This does not deploy the listener or change the ordinary agent roster.

The Mac checker uses `simple-chat handbook-service post|read`, not `--as OPERATOR`.  Operator is an existing administrator service bot, not an agent seat.  The adapter reads its named credential from Fleet-OPS `/zulip_operator`, checks the stored realm before authentication, then verifies authenticated bot ID, email and owner.  It refuses launched seats and remote mode.  No admin API, DM, subscription or privilege operation is exposed.

Posts are restricted to `jaywedgeworth22/Fleet-OPS` in Work-In-Progress / AFC handbook changes.  The normal secret scanner and text formatter run before the send.  Caller mentions are neutralized; only the pinned owner can be mentioned.  The service label identifies the deterministic sender.  A delivery ledger returns a previously successful ID and refuses to replay an uncertain send; an unknown outcome requires reconciliation before another attempt.

Read returns only messages in that same stream/topic.  The former Claude-authored approval posts remain readable.  Their sender does not approve anything: the checker still requires the pinned owner's check reaction on the exact head's recorded message.

## Qualification and install

1. Review and merge the adapter and checker source.  Install the simple-chat runtime from its qualified source before selecting the checker transport.
2. Set checker `notification_service: operator`, `operator_user_id`, `operator_email`, `owner_user_id` and `zulip_site` from verified live service metadata.  No key belongs in checker config.  Missing pins fail closed, with no Claude fallback.
3. Use the existing reviewed merged-source checker installer.  Preserve state.json and its approvals, message IDs and timestamps.  Do not reset approval records, patch the installed manifest, or redeploy the Operator listener for this change.
4. Verify the next naturally required notice's actual sender and verify an owner's reaction still propagates for its exact head.  Do not manufacture a production approval ask for a test.

The explicit `legacy-seat` checker transport exists for migration/regression coverage, not as an automatic fallback.  Pending historical direct-message notices cannot be read by the topic-only Operator adapter; reconcile them before cutover rather than waiving their approval.  Current already-approved records stand.

The environment guards avoid accidental misuse, not malicious local processes.  Actual access control is the host's named-secret read grant for `/zulip_operator`; this adapter does not grant it.  Successful exact-body results do not expire because approval asks carry the exact head.  Uncertain sends are reconciled read-only by the full fingerprint marker and pinned sender in this topic; a landed message returns its ID without reposting.  If no positive match is found, the adapter stays blocked for investigation, never timer-replays.  Its dedicated durable ledger does not prune unresolved entries when unrelated messages are sent.

A crash between reservation and send can leave a notice pending without a landed copy.  This deliberately remains blocked.  Investigation must distinguish a provably unsent process failure from a request whose outcome is unknown; topic-history absence alone does not authorize deletion of the ledger or replay.  This adapter exposes no release action.  Escalate the fingerprint and process evidence to the checker owner for a scoped recovery decision; never clear the whole ledger.  All server 5xx and rate-limit outcomes remain unknown, as do timeouts that may have sent.
