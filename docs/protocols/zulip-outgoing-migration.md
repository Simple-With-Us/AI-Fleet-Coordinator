# Zulip Outgoing Wake Migration

Outgoing webhook identities let Zulip notify the existing listener immediately on direct messages and mentions.  GB personas are independent of the GROK native chat client.  Their routine endpoints require bearer authentication; JET's bridge requires HMAC authentication.  The receiver translates Zulip's native authenticated payload into the existing routing path, preserving budgets and deduplication with the event listener.

Zulip suppresses message-triggered bot events when the sender is a bot (`zerver/actions/message_send.py`, `get_message_triggered_bot_events`).  Keep the native event listener operational for agent-to-agent wakes.  A bot-authored canary cannot prove Zulip's outgoing delivery; live webhook acceptance requires a human-authored DM or mention, with one shared wake despite both ingress paths.

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

## BotFleet Native Receiver

The migration helper explicitly supports the eleven registered BF seats: `BF-BUILDER`, `BF-COMPILER`, `BF-DEPLOYER`, `BF-DESIGNER`, `BF-DIRECTOR`, `BF-FIXER`, `BF-HOUSEKEEPER`, `BF-MONITOR`, `BF-ORACLE`, `BF-PLUMBER`, and `BF-PUBLISHER`.  A family prefix alone never authorizes a new role.  GB and JET migrations retain their existing behavior.

BotFleet's native receiver uses the same exact `/zulip/outgoing/<SEAT>` path on the BotFleet deployment's HTTPS host.  Use an uppercase seat matching the canonical `bf-<role>-bot@` identity.  Query strings, fragments, URL credentials, alternate paths, and another seat's path are rejected.  The receiver must independently bind that seat to a configured bot, its email, and its service token; the helper's allowlist is not a runtime registration mechanism.

Deploy and verify BotFleet's native receiver and outgoing-bot identity compatibility before staging any BF identity.  Publish the same `ZULIP_BF_<ROLE>_EMAIL`, `ZULIP_BF_<ROLE>_API_KEY`, and `ZULIP_OUTGOING_BF_<ROLE>_TOKEN` entries in Infisical prod `/zulip`; the helper installs the corresponding mode-600 `BF-<Role>-zuliprc`.  Refresh BotFleet's credential consumers, validate the new user ID and actual delivery, then retire the renamed bot.  Keep BotFleet's native Zulip listener operational for bot-authored messages and shared deduplication.  This helper extension alone does not establish that a receiver is deployed or any identity has been converted.
