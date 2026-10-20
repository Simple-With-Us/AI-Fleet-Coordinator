# Operator Notes Service Identity

Operator is a deterministic service, not an agent roster seat. The existing Admin Zulip bot is renamed by the owner; the bot user ID stays 1217148. Its email is operator-bot@simplewithus.zulipchat.com. No role or API key change is part of this source change.

The Mac listener partition permits OPERATOR. The only supported configuration is bot=Operator, creds=infisical and wake=inbox. Credentials resolve from AI Fleet Coordinator prod /zulip as ZULIP_OPERATOR_EMAIL and ZULIP_OPERATOR_API_KEY, through the existing fleet-secrets runtime loader, held in memory. There is no file or environment fallback. General agent roster authorization still refuses OPERATOR. The listener additionally pins the exact bot ID and email before accepting its queue.

The existing PR 652 notes-only admin_allowed rule remains unchanged: administrator acceptance applies only to the configured enabled Notes relay consumer while inbox-only, never to model wakes. Ending that exception drops its credential on reload through existing logic.

The Notes consumer sync script permits OPERATOR explicitly, without adding it to fleet-apps.json. The hosted Worker trusts an Operator result only from the pinned bot ID AND email, not a display name or another bot with a similar local part. Former Admin-email results do not count once OPERATOR is selected.

## Not Activated

This PR changes source/tests only. It does not switch NOTES_RELAY_CONSUMER, a Worker binding, Mac listener.toml, or any runtime credential. Before activation: verify the renamed bot live, inspect current Mac config and an empty Notes spool, review the source, then obtain the deployment go. Switch the knob and Worker binding with the Mac config as one change window; preserve rollback state privately. Verify an actual Notes canary and matching Operator result before removing old credential-name aliases. Keep token broker activation and Notes relay acceptance evidence separate.

## Provisioning and linkifier migration

Both owner-run scripts now read Fleet-OPS prod /zulip_operator with ZULIP_OPERATOR_EMAIL, ZULIP_OPERATOR_API_KEY and ZULIP_OPERATOR_SITE. The --admin flag still selects the administrator-capable Operator service; it does not grant agent impersonation. No REALM_ADMIN name fallback remains. Existing env overrides use the same new names. AFC prod /zulip ZULIP_OPERATOR_EMAIL and ZULIP_OPERATOR_API_KEY remain the Notes service credentials.

Legacy cleanup is gated on merged source and verification that deployed/local copies do not still use the old names. Candidate removals: Fleet-OPS prod /zulipadmin REALM_ADMIN_ZULIP_EMAIL, REALM_ADMIN_ZULIP_KEY, REALM_ADMIN_ZULIP_SITE; Fleet-OPS prod root the same three reference aliases; AFC prod /zulip ZULIP_ADMIN_EMAIL and ZULIP_ADMIN_API_KEY. The root key is a reference to the folder key, not a different credential. No secret values or bot keys are rotated by this migration.
