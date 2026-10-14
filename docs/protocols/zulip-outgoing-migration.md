# Zulip Outgoing Wake Migration

Outgoing webhook identities let Zulip notify the existing listener immediately on direct messages and mentions.  GB personas are independent of the GROK native chat client.  Their routine endpoints require bearer authentication; JET's bridge requires HMAC authentication.  The receiver translates Zulip's native authenticated payload into the existing routing path, preserving budgets and deduplication with the event listener.

## Receiver

The existing server daemon owns the HTTP server; there is no additional background job.  Configure `daemon.outgoing_webhook_seats` with the exact eligible seats, then set `[outgoing_webhook] enabled = true`, `bind = "0.0.0.0"`, and `port = 8787` in its private live configuration.  The sample remains disabled.  The reverse proxy must forward HTTPS to that port.  The only accepted path is `/zulip/outgoing/<SEAT>`.

Tokens load at startup from Infisical prod `/zulip` as `ZULIP_OUTGOING_<CODE>_TOKEN`, where hyphens become underscores.  These are Zulip service tokens, not bot API keys or routine keys.  Empty tokens authorize no requests.  Restart after publishing credentials; a configuration reload alone cannot refresh environment values.

The receiver authenticates the token and receiving bot email, then fsyncs only the seat and message ID to the private spool before acknowledging.  The daemon retrieves authoritative message content with that seat's Zulip API client before routing.  Webhook content never establishes owner authority.  Native events and outgoing requests share deduplication; webhook arrivals do not advance the native event cursor.

## Authorized Replacement

`scripts/zulip_migrate_outgoing.py` is an on-demand administrative helper.  It requires the owner's explicit authorization, an owner credential file with mode 600, a private mode-700 journal directory, a canonical seat email, and the deployed HTTPS endpoint.  Dry-run is the default; every mutation requires `--apply`.

1. Deploy receiver and identity compatibility first.  Confirm unauthenticated requests cannot route messages.  Confirm all credential consumers and whether any bot is actively running.
2. Run `stage` for one seat.  The helper journals the old identity, renames it, creates its outgoing replacement with the canonical email, obtains the separate service token, and restores subscriptions.  The old identity remains available for recovery.  Renaming breaks its former email login until consumers are updated; stage and publish are one attended maintenance operation.
3. Run `publish` with `INFISICAL_CLIENT_ID`, `INFISICAL_CLIENT_SECRET`, and `INFISICAL_PROJECT_ID` supplied privately.  The helper writes and readback-verifies `ZULIP_<CODE>_EMAIL`, `ZULIP_<CODE>_API_KEY`, and `ZULIP_OUTGOING_<CODE>_TOKEN` in prod `/zulip`, then atomically installs the local zuliprc.  Resume the same journal after any uncertain write; do not create another replacement.
4. Refresh each consumer.  The server listener reads Infisical at startup.  A GB routine's own reply credential must be verified separately.  JET's hosted MCP Worker also uses `ZULIP_KEY_JET` and `ZULIP_EMAIL_JET`; its separate wake HMAC is unchanged.  Update Mac listener outgoing-seat eligibility so migrated bots can still wake Mac seats.
5. Verify the new `users/me` identity, channel posting, a real DM or mention through Zulip, one accepted wake, and a reply from the new bot ID.  Verify event-listener convergence does not produce a second wake.
6. Only then run `retire --consumers-verified` to deactivate the renamed identity.  Keep the private recovery journal outside the repository.

BotFleet retains its native listener until its own authenticated inbound webhook receiver exists.  Replacing identities alone cannot make an unsupported platform receive a webhook.  Engineer registration is coordinated with the existing seat-registration change; an allowlist entry alone does not enable a listener seat.
