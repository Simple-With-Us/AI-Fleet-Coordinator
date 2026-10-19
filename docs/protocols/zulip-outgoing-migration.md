# Zulip Outgoing Wake Migration

Outgoing webhook identities let Zulip notify the existing listener immediately on direct messages and mentions.  GB personas are independent of the GROK native chat client.  Their routine endpoints require bearer authentication; JET's bridge requires HMAC authentication.  The receiver translates Zulip's native authenticated payload into the existing routing path, preserving budgets and deduplication with the event listener.

Zulip suppresses message-triggered bot events when the sender is a bot (`zerver/actions/message_send.py`, `get_message_triggered_bot_events`).  Keep the native event listener operational for agent-to-agent wakes.  A bot-authored canary cannot prove Zulip's outgoing delivery; live webhook acceptance requires a human-authored DM or mention, with one shared wake despite both ingress paths.

## Receiver

The existing server daemon owns the HTTP server; there is no additional background job.  Configure `daemon.outgoing_webhook_seats` with the exact eligible seats, then set `[outgoing_webhook] enabled = true`, `bind = "0.0.0.0"`, and `port = 8787` in its private live configuration.  The sample remains disabled.  The reverse proxy must forward HTTPS to that port.  The only accepted path is `/zulip/outgoing/<SEAT>`.

Tokens load at startup from Infisical prod `/zulip` as `ZULIP_OUTGOING_<CODE>_TOKEN`, where hyphens become underscores.  These are Zulip service tokens, not bot API keys or routine keys.  Empty tokens authorize no requests.  Restart after publishing credentials; a configuration reload alone cannot refresh environment values.

The receiver authenticates the token and receiving bot email, then fsyncs only the seat and message ID to the private spool before acknowledging.  The daemon retrieves authoritative message content with that seat's Zulip API client before routing.  Webhook content never establishes owner authority.  Native events and outgoing requests share deduplication; webhook arrivals do not advance the native event cursor.

## Authorized Replacement

`scripts/zulip_migrate_outgoing.py` was retired 2026-10-19 because it needed the owner key.  Bots are now provisioned through `scripts/zulip_provision_bots.py` with the realm-admin bot (generic bots only;  it cannot convert an existing bot to an outgoing webhook identity).

## BotFleet Native Receiver

The eleven registered BF seats are: `BF-BUILDER`, `BF-COMPILER`, `BF-DEPLOYER`, `BF-DESIGNER`, `BF-DIRECTOR`, `BF-FIXER`, `BF-HOUSEKEEPER`, `BF-MONITOR`, `BF-ORACLE`, `BF-PLUMBER`, and `BF-PUBLISHER`.  A family prefix alone never authorizes a new role.  GB and JET migrations retain their existing behavior.

BotFleet's native receiver uses the same exact `/zulip/outgoing/<SEAT>` path on the BotFleet deployment's HTTPS host.  Use an uppercase seat matching the canonical `bf-<role>-bot@` identity.  Query strings, fragments, URL credentials, alternate paths, and another seat's path are rejected.  The receiver must independently bind that seat to a configured bot, its email, and its service token.

Deploy and verify BotFleet's native receiver and outgoing-bot identity compatibility before staging any BF identity.  Publish the same `ZULIP_BF_<ROLE>_EMAIL`, `ZULIP_BF_<ROLE>_API_KEY`, and `ZULIP_OUTGOING_BF_<ROLE>_TOKEN` entries in Infisical prod `/zulip`.  Refresh BotFleet's credential consumers, validate the new user ID and actual delivery, then retire the renamed bot.  Keep BotFleet's native Zulip listener operational for bot-authored messages and shared deduplication.  This alone does not establish that a receiver is deployed or any identity has been converted.
