# Handbook checker notifications as Operator

Source preparation only.  This does not deploy the listener or change the ordinary agent roster.

The Mac checker uses `simple-chat handbook-service post|read`, not `--as OPERATOR`.  Operator is an existing administrator service bot, not an agent seat.  The adapter reads its named credential from Fleet-OPS `/zulip_operator`, checks the stored realm before authentication, then verifies authenticated bot ID, email and owner.  It refuses launched seats and remote mode.  No admin API, DM, subscription or privilege operation is exposed.

Posts are restricted to `jaywedgeworth22/Fleet-OPS` in Work-In-Progress / AFC handbook changes.  The normal secret scanner and text formatter run before the send.  Caller mentions are neutralized; only the pinned owner can be mentioned.  The service label identifies the deterministic sender.  A delivery ledger returns a previously successful ID and refuses to replay an uncertain send; an unknown outcome requires reconciliation before another attempt.

Read returns only messages in that same stream/topic.  The former Claude-authored owner-approval posts remain readable as historical context; their sender and any owner reaction do not gate a handbook change.  The Operator notice reports the change, while the checker continues to enforce its mechanical build, privacy, dirty-tree, install and CI/review safety holds.

## Qualification and install

1. Review and merge the adapter and checker source.  Install the simple-chat runtime from its qualified source before selecting the checker transport.
2. Set checker `notification_service: operator`, `operator_user_id`, `operator_email`, `owner_user_id` and `zulip_site` from verified live service metadata.  No key belongs in checker config.  Missing pins fail closed, with no Claude fallback.
3. Use the existing reviewed merged-source checker installer.  Preserve state.json and its notice records, message IDs and timestamps.  Do not reset historical records, patch the installed manifest, or redeploy the Operator listener for this change.
4. Verify the next naturally required notice's actual sender and that it is posted to the fixed topic.  Do not manufacture a production notice for a test.

The explicit `legacy-seat` checker transport exists for migration/regression coverage, not as an automatic fallback.  Pending historical direct-message notices cannot be read by the topic-only Operator adapter; preserve and reconcile their records during cutover, but do not treat their missing owner reactions as an approval gate.  Existing state, message IDs and timestamps remain intact.

The environment guards avoid accidental misuse, not malicious local processes.  Actual access control is the host's named-secret read grant for `/zulip_operator`; this adapter does not grant it.  Successful notice records remain available for deduplication.  Uncertain sends are reconciled read-only by the full fingerprint marker and pinned sender in this topic; a landed message returns its ID without reposting.  If no positive match is found, the adapter stays blocked for investigation, never timer-replays.  Its dedicated durable ledger does not prune unresolved entries when unrelated messages are sent.

A crash between reservation and send can leave a notice pending without a landed copy.  This deliberately remains blocked.  Investigation must distinguish a provably unsent process failure from a request whose outcome is unknown; topic-history absence alone does not authorize deletion of the ledger or replay.  This adapter exposes no release action.  Escalate the fingerprint and process evidence to the checker owner for a scoped recovery decision; never clear the whole ledger.  All server 5xx and rate-limit outcomes remain unknown, as do timeouts that may have sent.
