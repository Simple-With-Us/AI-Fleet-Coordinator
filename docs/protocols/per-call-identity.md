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

Never in any set:  the owner's own account (`Jay-zuliprc`) and the integration bots (BetterStack, Datadog, Linear, PagerDuty, Sentry).  The listener daemon keeps its partition (`agent-sync-partition.toml`) unchanged:  this page is about who an agent may act as, not which instance listens for whom.

GB-ENGINEER stays out until its credentials are inventoried in Infisical and verified (`DEPLOY.md`, `agent-sync-partition.toml` and `agent-sync-server.md` keep it disabled).  A persona joins a set only by being enabled in the table with a verified credential, never by being named in a call.  The allowed-set builder reads `GB_IDENTITIES` from that table and never keeps a list of its own, so the page and the runtime cannot drift apart.

### Which Connection a Client Gets

- **A Mac app that can run a local command** uses the stdio server (`agent-sync mcp`, no `--as`).  It needs no OAuth and no arming, and its allowed set is every agent bot credential on the Mac.
- **A client that only speaks remote MCP** (a web app, a phone, or a Mac app with no local command support) uses the hosted server.  The owner arms the seat in `/admin` before the client starts OAuth, as `scripts/agent-sync-mcp/ARMING-JAY.md` describes.  The armed seat becomes the grant's default identity and picks its allowed set from the table above, and each call still names the bot it acts as.

The hosted shared group lives in one table (`delegated-identities.js`), replacing `CURSOR_IDENTITIES`.  Its credentials stay in the encrypted `GB_IDENTITY_CREDENTIALS` bundle;  GROK-WEB and CURSOR keep their own per-seat secrets.

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

The CLI mirrors the families as subcommand groups:  `agent-sync zulip …`, `agent-sync listener …`, `agent-sync notes …`, with today's flat verbs (`post`, `reply`, `inbox`) kept as shortcuts.

## 5. Apple Notes From Anywhere

Notes live in the Mac's iCloud `Coding` folder, written by `scripts/apple-notes-coding.sh`.  Only the Mac can run it, so:

- **On the Mac** (CLI and stdio):  `notes_*` calls the script directly, passing title, body and flags through files, never the command line.
- **Everywhere else** (server CLI, server stdio, hosted):  `notes_*` posts a relay request as the claimed identity to channel `#notes-relay`, topic = the note title, body = a fenced JSON envelope (`action`, `title`, `markdown`, flags, `request_id`).  Bodies over Zulip's limit go up as an attached `.md` upload.
- **The Mac listener** watches `#notes-relay`, accepts requests only from fleet agent bots, checks the title starts `[APP, Agent]` and that `Agent` is the sender's own seat or role, runs the script, then reacts ✅ (or ❌ with a reply giving the reason).  It is idempotent on `request_id`.
- The tool returns `queued` with the request id;  `notes_status` reads the reaction.

## 6. Shell Parity on the Server

The server mirror (`fleet-cli-release.py` allowlist, pushed by `fleet-cli-sync.py`) gains `recall`, `lane` and `fleet-mode` alongside `agent-sync` and `board`, plus the docs `fleet-mode` prints.  `apple-notes-coding.sh` is not mirrored;  the server reaches Notes through the relay.  The tool-parity doctor (`scripts/fleet-tools-doctor.py`) checks the same list on both hosts.

## 7. The Guide

One guide, `docs/protocols/agent-sync-tools-guide.md`, written once the code lands:  every tool by family with a one-line purpose, how to pass `identity`, which connection each client uses and how to arm it, the old-to-new name table, the Notes relay, and the CLI equivalents.  Agents reach it four ways:

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
