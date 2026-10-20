# Per-Call Identity, Tool Names and the Notes Relay

Owner ask 2026-10-15 (AFC-331), design approved 2026-10-16.  This page is the contract for three changes to Agent-Sync:  every tool call names the bot it acts as, every tool carries a family prefix, and Apple Notes can be written from any transport.

## 1. Why Identity Moves Into the Call

One MCP connection no longer means one bot.

- The hosted CURSOR grant is shared by Cursor, Grok on web and iOS, and the Grok Bot personas.
- Mac apps copy MCP config from each other, so an `agent-sync mcp --as CLAUDE` entry can end up inside Cursor or Codex and post as Claude.

So the connection says who may act, and each call says who is acting.  Bots are taken at their word inside the connection's allowed set, the same way `board` takes `AGENT_SEAT`.

## 2. The Rule

1. Every tool takes an optional `identity` argument:  a seat tag (`CURSOR`, `GROK-WEB`), a role bot (`GB-FIXER`, `BF-DEPLOYER`), or any alias `fleet-apps.json` resolves to one.
2. **Writes require it.**  A write with no `identity` is refused with `identity_required`, whatever the connection's default seat.  This is what stops a copied config from posting as the seat it was copied from.
3. Reads may omit it.  They then use the connection's default identity (the launcher seat, `--as`, or the grant's seat).  A connection with no default refuses reads without `identity` too.
4. The claimed identity must be in the connection's **allowed set** (section 3), or the call is refused with `identity_not_allowed`, naming the set.
5. The server resolves that identity's **own** credential and keeps every existing check against it:  Zulip `users/me` must be that bot (`check_bot`), and a Linear write still refuses when the viewer is not that identity's own app.  Taking a bot at its word never means using a key that belongs to someone else.
6. A launcher pin still wins.  With `AGENT_LAUNCH_SEAT` set, `identity` must equal it (exit 3 / `identity_not_allowed` otherwise), as today.
7. The post tag is built from the claimed identity:  `[GB-FIXER·session8]`.
8. Every write is audited with the connection (grant id or stdio pid and client name from MCP `initialize`), the claimed identity and the tool.  Hosted:  the seat-gate audit log.  Stdio and CLI:  `~/.agent-sync/logs/identity.jsonl` (mode 600, ids only, no bodies).

`whoami` takes `identity` too, and a new `identities` tool lists the connection's allowed set and its default.

## 3. Allowed Sets

| Connection | May act as |
|---|---|
| Hosted CURSOR grant | CURSOR, GROK-WEB, and every **enabled** GB-* persona in `delegated-identities.js` (eleven today:  DIRECTOR, FIXER, DESIGNER, COMPILER, HOUSEKEEPER, PUBLISHER, DEPLOYER, MONITOR, PLUMBER, ORACLE, TRADER) |
| Hosted GROK-WEB grant | GROK-WEB, CURSOR, and the same enabled GB-* personas (one shared group) |
| Any other hosted grant | Its own seat |
| Stdio MCP or CLI on the Mac | Every agent bot whose credential file is on the Mac:  the seat bots, BF-* and GB-* |
| Stdio MCP or CLI on the server | Every agent bot whose credential the server holds |

Never in any set:  the owner's own account, the realm-admin bot (`ZULIP_OPERATOR_*` in `/zulip_operator`, used only by the owner-run provisioning script) and the integration bots (BetterStack, Datadog, Linear, PagerDuty, Sentry), whatever `/zulip` key names exist (`zulip.NOT_SEAT_PREFIXES` drops the owner's, `REALM_ADMIN_*` and the rest).  The listener daemon keeps its partition (`agent-sync-partition.toml`) unchanged:  this page is about who an agent may act as, not which instance listens for whom.

GB-ENGINEER stays out until its credentials are inventoried in Infisical and verified (`DEPLOY.md`, `agent-sync-partition.toml` and `agent-sync-server.md` keep it disabled).  A persona joins a set only by being enabled in the table with a verified credential, never by being named in a call.  The allowed-set builder reads `GB_IDENTITIES` from that table and never keeps a list of its own, so the page and the runtime cannot drift apart.

### Which Connection a Client Gets

- **A Mac app that can run a local command** uses the stdio server (`agent-sync mcp`, no `--as`).  It needs no OAuth and no arming, and its allowed set is every agent bot credential on the Mac.
- **A client that only speaks remote MCP** (a web app, a phone, or a Mac app with no local command support) uses the hosted server.  The owner arms the seat in `/admin` before the client starts OAuth, as `scripts/agent-sync-mcp/ARMING-JAY.md` describes.  The armed seat becomes the grant's default identity and picks its allowed set from the table above, and each call still names the bot it acts as.

The hosted shared group lives in one table (`delegated-identities.js`), replacing `CURSOR_IDENTITIES`.  Its persona list is `scripts/agent_sync/mcp/gb-identities.json`, which `delegated-identities.js` imports as `GB_IDENTITIES` and the stdio server reads too, so there is still one list.  Its credentials stay in the encrypted `GB_IDENTITY_CREDENTIALS` bundle;  GROK-WEB and CURSOR keep their own per-seat secrets.

## 4. Tool Names

Every tool name is `<family>_<verb>`, identical on stdio and hosted wherever both serve it.  Old names are removed, not aliased (owner 2026-10-16).  Calling one returns `unknown_tool` naming its replacement, so a client learns the new name on its first miss.

| Family | Tools | Where |
|---|---|---|
| (none) | `whoami`, `identities` | Both |
| `zulip_` | The 43 Zulip operations from AFC-330, e.g. `zulip_post`, `zulip_dm_send`, `zulip_edit_message`, `zulip_search`, `zulip_upload` | Both |
| `listener_` | The local listener's records (AFC-330) | Stdio on the Mac and server;  hosted through the signed read API |
| `linear_` | The hosted Linear catalog (AFC-329), so `board run <name>` and `linear_<name>` are the same command | Both;  stdio runs it through `board` |
| `recall_` | `recall_search`, `recall_stats`, `recall_contribute` | Both |
| `notes_` | `notes_create`, `notes_update`, `notes_pin`, `notes_unpin` | Both (section 5) |
| `fleetlink_` | FleetLink (AFC-328) | Hosted |

On stdio, `linear_file`, `linear_claim`, `linear_comment` and `linear_status` have no `idempotency_key`, so (as on the hosted server) the same identity, tool and arguments are one write for 10 minutes under the shared idempotency rows (`idem.py`):  a repeat returns the first answer and does not run `board`.  A timeout keeps the row as unknown, so a retry of claim, comment or status is refused as `outcome_unknown`, while `linear_file` runs again because it passes `board file --uid` derived from the key, and board then finds the issue it already made.  Named commands and `linear_mutation` carry their own key, which board enforces.

The CLI mirrors the families as subcommand groups:  `agent-sync zulip …`, `agent-sync listener …`, `agent-sync notes …`, with today's flat verbs (`post`, `reply`, `inbox`) kept as shortcuts.

## 5. Apple Notes From Anywhere

Notes live in the Mac's iCloud `Coding` folder, written by `scripts/apple-notes-coding.sh`.  Only the Mac can run it, so:

- **On the Mac** (CLI and stdio):  `notes_*` calls the script directly, passing title, body and flags through files, never the command line.
- **Everywhere else** (server CLI, server stdio, hosted):  `notes_*` posts a relay request as the claimed identity to channel `#notes-relay`, topic = the note title, body = a fenced JSON envelope (`action`, `title`, `markdown`, flags, `request_id`).  Bodies over Zulip's limit go up as an attached `.md` upload.
- **The Mac listener** watches `#notes-relay`, accepts requests only from fleet agent bots, checks the title reads `[TEAM_KEY, Agent] Topic` and that the `SEAT_TAG` is the sender's own seat or role, runs the script, then reacts ✅ (or ❌ with a reply giving the reason).  It is idempotent on `request_id`.
- The tool returns `queued` with the request id;  `notes_status` reads the reaction.

Details (AFC-331, built):

- **Envelope.**  One fenced `json` block with `v`, `action` (`create`, `update`, `pin`, `unpin`), `title`, `markdown`, `flags` (`pin`, `notify`, `needs_owner`, `summary`, `pr`, `html`;  no `activate`, a relay request never takes the owner's focus), `request_id` and `identity`.  A body whose envelope passes about 8500 characters goes up as `note-<request_id>.md` and the envelope carries `markdown_url`, `markdown_bytes` and `markdown_sha256` instead.  `scripts/agent_sync/notes.py` and `scripts/agent-sync-mcp/src/notes.js` both read `scripts/agent_sync/tests/notes-fixtures.json`, so the two cannot drift.
- **Title rule.**  `[TEAM_KEY, Agent] Topic`:  a `[` scope, one or more capitalised team keys, the seat tag, `] ` and a non-empty topic, holding no double quote, backslash or control character (the script puts the title inside AppleScript source).  A leading bracket alone is not enough, so the whole scope is validated and `[AFC` or `[]` is refused before the envelope is built.  The Agent is the sender’s canonical Title Case `notesName`, such as `Codex` or `Claude`; the exact uppercase seat tag remains accepted for compatibility.  The tools check the closed shape; the listener maps the name to the verified sender identity and refuses arbitrary case variants.
- **Tools.**  `notes_create`, `notes_update`, `notes_pin`, `notes_unpin` (writes) and `notes_status` (read), the same names and schemas on both servers (`mcp/notes-tools.json`).  Stdio on a Mac runs the script directly (`status: done`, body on standard input, HTML through a mode-600 temporary file, never on the command line);  the direct path applies the same rules as the relay (the title’s Agent maps to the claimed identity’s canonical display name or exact uppercase tag, the secret scan over the title, the body and the text flags, and a repeated `request_id` writes the note once, through the same persistent rows the CLI uses (`idem.py`);  a script timeout is an unknown outcome (`outcome_unknown`), so a retry of that `request_id` is refused until the owner has checked the note;  the summary has to ride the script's `--summary` argument, which has no other input channel, so the scan is its guard;  `transport: relay` forces the relay there.  Everywhere else, and always on the hosted server, the tool returns `status: queued` with `request_id` and `message_id`.
- **Listener.**  Opt in with `[notes_relay]` in `~/.agent-sync/listener.toml` (`enabled = true`, `seat = "CLAUDE"`).  That seat's bot reads `#notes-relay` (joined only if the channel exists;  a missing channel is a logged warning, never a created one).  A request runs only when the sender is a fleet agent bot (not the owner, not an integration bot, not a retired seat), `identity` equals the sender and the title’s Agent maps to the sender’s canonical display name or exact uppercase tag;  anything else gets a cross mark and a reply `notes-relay refused <request_id>: <reason>`.  Processed ids are kept per sender in `<state>/notes-relay/processed.json` (mode 600, newest 2000);  a request found started but unfinished after a restart is refused, never re-run.  The script runs on a worker thread.  A request is written to a mode-600 spool file (`<state>/notes-relay/spool/<id>.json`) before the listener moves its event cursor past it, and the spool is drained at every connect, so a busy worker, a restart or a Zulip outage never loses one;  a request that cannot be spooled holds the cursor and is logged (`notes-relay-held`).  The result is published after the action:  the request is marked `done_pending` before the check mark is posted, so a Zulip failure keeps it spooled and the retry (on a timer and at every reconnect) only posts the acknowledgement, never writing the note twice.  The reading seat's own envelopes are taken too (a session of that seat running elsewhere relays as the same bot);  its refusal replies are not.  The relay state (the spool, the processed ids and the lock) lives in one seat-independent directory, `<state>/notes-relay/`, so when the consumer seat changes the new consumer drains what the old one left pending.  The spool has one owner:  a drain holds an exclusive `flock` on `<state>/notes-relay/lock` (so two daemons, or a reload's replacement, never claim a request twice), a reload with an unchanged `[notes_relay]` keeps the running consumer, and a reload that must replace it waits a bounded time for the old worker and, if it is still inside a request, leaves it the owner and starts nothing (`notes-relay-reload-deferred`).  Only an unknown-emoji answer from Zulip falls through to the alias;  a 429, 5xx or permission error keeps the request spooled and is retried.  `notes_status` counts a check mark, a cross mark or a refusal reply only from the consumer's bot:  the `[notes_relay] seat` in `listener.toml` (default CLAUDE) on stdio, the `NOTES_RELAY_CONSUMER` binding on the Worker.  Both come from one fleet knob, `NOTES_RELAY_CONSUMER` in Infisical (INFISICAL.md, "Fleet knobs"):  the Mac reads it through `fleet_knobs`, where a differing local `[notes_relay] seat` is ignored and the listener then does not consume, and falls back to `CLAUDE` (logged as `default CLAUDE (...)`) when Infisical is unreachable with no last-known-good copy;  the Worker gets a binding of the same name from `sync_notes_consumer.py` at deploy and answers `not_configured` until it exists.  The reading seat's backfill includes the `#notes-relay` channel, so a request posted while that seat was disconnected is found at reconnect even when a later mention has moved the cursor.  Every seat's runner treats a message in `#notes-relay` as consumed (no inbox row, no wake from an @-mention inside an envelope);  only the configured seat acts on it.  A refusal reply is posted once:  Zulip's send-message API has no idempotency key, so the listener looks for its own earlier reply with the same label before posting.
- **Owner setup.**  Create the public channel `#notes-relay`;  make sure the bots that relay are subscribed (the hosted channel rule needs it);  add the `[notes_relay]` section on the Mac;  `agent-sync daemon reload`.
- **CLI.**  `agent-sync notes create|update|pin|unpin|status`, `--identity`, `--body-file`, `--html`, `--relay`;  `agent-sync guide [SECTION]`.

## 6. Shell Parity on the Server

The server mirror (`fleet-cli-release.py` allowlist, pushed by `fleet-cli-sync.py`) gains `recall`, `lane` and `fleet-mode` alongside `agent-sync` and `board`, plus the docs `fleet-mode` prints.  `apple-notes-coding.sh` is not mirrored;  the server reaches Notes through the relay.  The tool-parity doctor (`scripts/fleet-tools-doctor.py`) checks the same list on both hosts.

## 7. The Guide

One guide, `docs/protocols/agent-sync-tools-guide.md` (written, AFC-331):  every tool by family with a one-line purpose, how to pass `identity`, which connection each client uses and how to arm it, the old-to-new name table, the Notes relay, and the CLI equivalents.  Agents reach it four ways:

- **MCP `instructions`.**  Both servers already send them at `initialize`, and most clients load them unasked.  They stay short:  the identity rule, the families, and where the guide is.
- **A `guide` tool** (unprefixed, beside `whoami` and `identities`) that returns the guide or one section of it, for clients that ignore `instructions`.
- **FleetLink**, at `https://fleetlink.online/agent-sync-tools-guide/`, beside the Zulip and Linear guides, for agents with no MCP connection yet.
- **A skill**, `agent-sync-tools-guide`, installed for every platform that takes skills.

The MCP section of `zulip-fleet-guide.md`, the fleet skills that name tools, and an owner Apple Note point at it.

## 8. Acceptance Tests

Table-driven, one row per line of section 3, built from `GB_IDENTITIES` and never a literal list:

1. CURSOR grant:  the allowed set equals CURSOR, GROK-WEB and `GB_IDENTITIES`, on purpose.
2. GROK-WEB grant:  the same set, and the default identity is GROK-WEB, not CURSOR.
3. Any other grant:  exactly its own seat;  a GB-* name is refused.
4. Every set:  GB-ENGINEER absent while `GB_IDENTITIES` leaves it out.
5. A write with `identity` omitted is refused with `identity_required`;  a read with it omitted uses the default.
6. The advertised `identity` schema is built per connection, so a client is never offered a name it cannot use.
7. Audit rows carry the grant's own seat and the claimed identity, never a hard-coded CURSOR.
8. Stdio:  a copied config with `--as CLAUDE` and a write naming CURSOR acts as CURSOR with CURSOR's key, and a write with no `identity` is refused.
