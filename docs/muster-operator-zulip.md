# Muster reads Zulip as the Operator

AFC-654.  Owner ruling:  Muster's Seats panel reads Zulip as the Operator's bot, never as an agent seat (it read as CLAUDE before) and never as Jay.

## The service

`simple-chat muster-service` is a narrow, read-only adapter in `scripts/simple_tools/muster_service.py`, modelled on `simple-chat handbook-service` (`docs/handbook-operator-notifications.md`).  The Operator is an administrator service bot, not a roster seat, so `--as OPERATOR` is not the route and does not work.

| Subcommand | Zulip request | Prints |
|---|---|---|
| `users [--limit N]` (default 500, at most 2000) | one `GET users` | every user, deactivated ones included, one JSON object per line in the `simple-chat users --json --inactive` shape, sorted by display name |
| `last-post --sender ADDRESS` | one `GET messages`, narrow `channels:public` plus `sender:ADDRESS`, anchor newest | the sender's newest public-channel message, one JSON object in the `simple-chat search --json` shape (`id`, `sender_id`, `channel`, `topic`, `dm`, `sender_email`, `sender_full_name`, `timestamp`, `content`), or nothing |

Before either read it sends one `GET users/me`.  Nothing else can be reached:  the service talks to Zulip only through a wrapper whose single method is `get`, and that method refuses every path but `users/me`, `users` and `messages`.  There is no post, edit, reaction, flag, direct-message, subscription or administrator operation.  Direct messages and private channels are outside the `last-post` narrow, and the body passed on is capped at 10,000 characters.  Muster reads the body only to tell whether it reports the seat at quota, and never shows it.

## Identity

1. It refuses to run in remote mode (`SIMPLE_CHAT_REMOTE=1`) or for a launched seat (`AGENT_LAUNCHER` or `AGENT_LAUNCH_SEAT`).
2. It reads the host's pins, and fails closed when any is missing (`pins-missing`) or malformed (`pins-unreadable`).  No id is committed:  a production user id and address are private inventory.
   - Operator user id:  `OPERATOR_ZULIP_USER_ID` from the environment, else `[daemon] operator_user_id` in the host's `listener.toml` (`~/.simple-tools/listener.toml`, or `SIMPLE_TOOLS_CONFIG`).
   - Operator address:  `OPERATOR_ZULIP_EMAIL` from the environment, else `[daemon] operator_email` in the same file.
   - Owner user id:  `[daemon] owner_user_id` in the same file, which `simple-chat daemon init` pins.
   Muster starts the service with only HOME and PATH in its environment, so on the Mac the pins come from `listener.toml`.
3. It reads the Operator's own three names from Fleet-OPS `/zulip_operator` (`ZULIP_OPERATOR_EMAIL`, `ZULIP_OPERATOR_API_KEY`, `ZULIP_OPERATOR_SITE`) through the fleet's Infisical reader, the same route as `handbook-service`, and checks the stored realm against the realm it reads (`SIMPLE_TOOLS_REALM`, else the fleet realm) before any authentication.  The stored address has to be the pinned address (`service_identity.operator_credentials`).
4. `users/me` has to be the pinned Operator bot owned by the pinned owner (`service_identity.operator_bot`), or nothing else is asked.

## Bounds

One `users/me` and one read per invocation, each request with a 10-second timeout and all of them inside a 12-second budget;  the vault read has the reader's own 15-second cap.  Muster makes one `users` call and one `last-post` call per seat it found a bot for, eight at a time at most, and stops each child after 30 seconds.

## Failure

Every failure prints one line on stderr and exits 3:  `muster-service: refused (CODE)`.  The code is one of `launched-or-remote`, `pins-missing`, `pins-unreadable`, `realm-invalid`, `credential-unreadable`, `realm-mismatch`, `identity-mismatch`, `credential-rejected`, `zulip-unreachable`, `zulip-error`, `malformed-response`, `bad-request` or `failed`.  Provider text, exception messages, argument values and credentials are never printed.  Muster maps the code to a plain sentence on the Seats panel ("Muster reads Zulip only as the Operator, and the Operator read is unavailable:  ...  No agent seat is read instead"), and an older `simple-chat` without the service reads as one that may predate it.  Muster never falls back to any seat.

## Host setup

The Mac's `listener.toml` must carry the three pins, and the Mac must be able to read `/zulip_operator`.  Access control is the host's named-secret read grant for that folder;  this adapter does not grant it.  The `simple-chat` runtime on the Mac must include this service before the new Muster build can read the panel.

Tests:  `cd scripts && python3 -m unittest simple_tools.tests.test_muster_service` (CI runs it with the rest of `simple_tools/tests`), and `SeatActivityTests` and `MusterSettingsTests` in the Muster macOS build.
