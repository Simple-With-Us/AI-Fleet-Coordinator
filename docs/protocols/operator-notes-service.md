# Operator Notes Service Identity

Operator is a deterministic service, not an agent roster seat.  The owner renames the existing administrator bot without changing its role or API key.

The listener permits OPERATOR with bot=Operator, creds=infisical and wake=inbox.  Credentials resolve through the approved named-secret reader and remain in memory.  No local credential-file or environment credential fallback is added.  General agent roster and wake authorization exclude OPERATOR.  Only the enabled inbox-only Notes consumer may use the existing administrator exception.

## Runtime identity settings

Set OPERATOR_ZULIP_USER_ID and OPERATOR_ZULIP_EMAIL from the approved service registry on the listener, hosted Worker, and every host running the Notes CLI.  Both settings are required for Operator acceptance.  The authenticated bot must match that ID and email and belong to the pinned owner.  Missing settings or a mismatched owner fail closed.  Tests use synthetic identities.

The listener also requires INFISICAL_FLEET_OPS_PROJECT_ID (the same Fleet-OPS project setting every fleet credential read uses) and ZULIP_SITE for the realm.  The Operator names live in Fleet-OPS /zulip_operator - verified present 2026-10-21;  an earlier revision read them from the AFC project, where they do not exist, which is why the credential read never worked.  Concrete project identifiers and secret-store coordinates belong in private deployment configuration.

The hosted Worker requires the same Operator identity settings when NOTES_RELAY_CONSUMER selects OPERATOR.  OWNER_USER_ID remains the pinned owner setting.  The consumer-sync script validates the consumer label only; it does not authenticate or grant authority to a bot.

## Deployment status

Source changes do not activate a listener or change hosted bindings.  Review before deployment, verify runtime identity settings and credential access, then obtain the deployment go.  Change the consumer selection and listener configuration together, preserve rollback state privately, and verify an actual Notes canary.  Keep token-broker activation and Notes relay acceptance separate.

## Owner-run maintenance

Provisioning and linkifier scripts now use the approved Operator credential store and ZULIP_OPERATOR_EMAIL, ZULIP_OPERATOR_API_KEY and ZULIP_OPERATOR_SITE.  The --admin flag still selects an administrator-capable service, not agent impersonation.  No legacy-name fallback remains.  Legacy aliases were removed separately with owner approval; this source change does not rotate keys.
