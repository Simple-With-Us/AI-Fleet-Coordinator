# Operator Service Identity

Operator is a deterministic service, not an agent roster seat.  The owner renamed the existing administrator bot without changing its role or API key.

This page once also made the Operator bot the consumer of the Zulip `#notes-relay` channel.  That relay is retired (AFC-486, Thu, Oct 22, 2026:  "I don't want notes going from Zulip to Apple notes").  Apple Notes from the server and cloud seats now go through the Operator's notes queue over signed HTTPS, which uses no Zulip identity at all:  `docs/protocols/operator-notes.md`.

The listener permits OPERATOR with bot=Operator, creds=infisical and wake=inbox.  Credentials resolve through the approved named-secret reader and remain in memory.  No local credential-file or environment credential fallback is added.  General agent roster and wake authorization exclude OPERATOR.  Only the OPERATOR seat may hold the administrator key (`daemon.admin_allowed`).

## Runtime identity settings

Set OPERATOR_ZULIP_USER_ID and OPERATOR_ZULIP_EMAIL from the approved service registry on the listener and every host that checks the Operator bot.  Both settings are required for Operator acceptance.  The authenticated bot must match that ID and email and belong to the pinned owner.  Missing settings or a mismatched owner fail closed.  Tests use synthetic identities.

The listener also requires INFISICAL_FLEET_OPS_PROJECT_ID (the same Fleet-OPS project setting every fleet credential read uses) and ZULIP_SITE for the realm.  The Operator names live in Fleet-OPS /zulip_operator - verified present 2026-10-21;  an earlier revision read them from the AFC project, where they do not exist, which is why the credential read never worked.  Concrete project identifiers and secret-store coordinates belong in private deployment configuration.

## Owner-run maintenance

Provisioning and linkifier scripts now use the approved Operator credential store and ZULIP_OPERATOR_EMAIL, ZULIP_OPERATOR_API_KEY and ZULIP_OPERATOR_SITE.  The --admin flag still selects an administrator-capable service, not agent impersonation.  No legacy-name fallback remains.  Legacy aliases were removed separately with owner approval; this source change does not rotate keys.
