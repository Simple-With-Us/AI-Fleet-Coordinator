# Per-Call Identity, Tool Names and Apple Notes

Owner ask 2026-10-15 (AFC-331), design approved 2026-10-16.  This page is the contract for three changes to Agent-Sync:  every tool call names the bot it acts as, every tool carries a family prefix, and Apple Notes can be written from any transport.

## 1. Why Identity Moves Into the Call

One MCP connection no longer means one bot.

- The hosted CURSOR grant is shared by Cursor, Grok on web and iOS, and the Grok Bot personas.
- Mac apps copy MCP config from each other, so an `simple-chat mcp --as CLAUDE` entry can end up inside Cursor or Codex and post as Claude.

So the connection says who may act, and each call says who is acting.  Bots are taken at their word inside the connection's allowed set, the same way `simple-board` takes `AGENT_SEAT`.

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
| Hosted CURSOR grant | CURSOR, GROK-WEB, and every **enabled** GB-* persona in `delegated-identities.js` (twelve today:  DIRECTOR, FIXER, DESIGNER, COMPILER, HOUSEKEEPER, PUBLISHER, DEPLOYER, MONITOR, PLUMBER, ORACLE, TRADER, ENGINEER) |
| Hosted GROK-WEB grant | GROK-WEB, CURSOR, and the same enabled GB-* personas (one shared group) |
| Any other hosted grant | Its own seat |
| Stdio MCP (`simple-chat mcp`) on the Mac or the server | Its own seat only:  the seat whose own bearer (`~/.secrets/fleet-tools/<seat>`) the hosted gateway confirms at start and again before every call.  No seat, or a bearer that is missing, unsafe, revoked or another seat's, serves nothing.  Other bots this host holds keys for are refused as `identity_not_allowed` |
| CLI on the Mac or the server (`simple-chat identities` and the verbs) | Every agent bot whose credential the host holds:  the seat bots, BF-* and GB-* (unchanged) |

Never in any set:  the owner's own account, the realm-admin bot (`ZULIP_OPERATOR_*` in `/zulip_operator`, used only by the owner-run provisioning script) and the integration bots (BetterStack, Datadog, Linear, PagerDuty, Sentry), whatever `/zulip` key names exist (`zulip.NOT_SEAT_PREFIXES` drops the owner's, `REALM_ADMIN_*` and the rest).  The listener daemon keeps its partition (`agent-sync-partition.toml`) unchanged:  this page is about who an agent may act as, not which instance listens for whom.

GB-ENGINEER was enabled on 2026-10-22.  A persona joins a set only by being enabled in the table with a verified credential, never by being named in a call.  The allowed-set builder reads `GB_IDENTITIES` from that table and never keeps a list of its own, so the page and the runtime cannot drift apart.

### Which Connection a Client Gets

- **A Mac app that can run a local command** uses the stdio server (`simple-chat mcp --default-seat SEAT`).  It needs no OAuth and no arming, but it needs the seat's own private bearer file and a reachable gateway, and its allowed set is that one seat.
- **A client that only speaks remote MCP** (a web app, a phone, or a Mac app with no local command support) uses the hosted server.  The owner arms the seat in `/admin` before the client starts OAuth, as `scripts/agent-sync-mcp/ARMING-JAY.md` describes.  The armed seat becomes the grant's default identity and picks its allowed set from the table above, and each call still names the bot it acts as.

The hosted shared group lives in one table (`delegated-identities.js`), replacing `CURSOR_IDENTITIES`.  Its persona list is `scripts/agent_sync/mcp/gb-identities.json`, which `delegated-identities.js` imports as `GB_IDENTITIES` and the stdio server reads too, so there is still one list.  Its credentials stay in the encrypted `GB_IDENTITY_CREDENTIALS` bundle;  GROK-WEB and CURSOR keep their own per-seat secrets.

## 4. Tool Names

Every tool name is `<family>_<verb>`, identical on stdio and hosted wherever both serve it.  Old names are removed, not aliased (owner 2026-10-16).  Calling one returns `unknown_tool` naming its replacement, so a client learns the new name on its first miss.

| Family | Tools | Where |
|---|---|---|
| (none) | `whoami`, `identities` | Both |
| `zulip_` | The 43 Zulip operations from AFC-330, e.g. `zulip_post`, `zulip_dm_send`, `zulip_edit_message`, `zulip_search`, `zulip_upload` | Both |
| `eavesdropper_` | The local listener's records (AFC-330) | Stdio on the Mac and server;  hosted through the signed read API |
| `linear_` | The hosted Linear catalog (AFC-329), so `simple-board run <name>` and `linear_<name>` are the same command | Both;  stdio runs it through `simple-board` |
| `recall_` | `recall_search`, `recall_stats`, `recall_contribute` | Both |
| `notes_` | `notes_create`, `notes_update`, `notes_pin`, `notes_unpin` | Both (section 5) |
| `fleetlink_` | FleetLink (AFC-328) | Hosted |

On stdio, `linear_file`, `linear_claim`, `linear_comment` and `linear_status` have no `idempotency_key`, so (as on the hosted server) the same identity, tool and arguments are one write for 10 minutes under the shared idempotency rows (`idem.py`):  a repeat returns the first answer and does not run `simple-board`.  A timeout keeps the row as unknown, so a retry of claim, comment or status is refused as `outcome_unknown`, while `linear_file` runs again because it passes `simple-board file --uid` derived from the key, and board then finds the issue it already made.  Named commands and `linear_mutation` carry their own key, which board enforces.

The CLI mirrors the families as subcommand groups:  `simple-chat zulip …`, `simple-chat listener …`, `simple-notes …`, with today's flat verbs (`post`, `reply`, `inbox`) kept as shortcuts.

## 5. Apple Notes From Anywhere

Notes live in the Mac's iCloud `Coding` folder, written by `scripts/apple-notes-coding.sh`, and only the Mac can run it.  The design is now `docs/protocols/operator-notes.md` (AFC-486, Thu, Oct 22, 2026):

- **One tool set.**  `notes_create`, `notes_update`, `notes_pin`, `notes_unpin`, `notes_read`, `notes_list`, `notes_status` and `notes_log`, the same names and schemas on both servers (`mcp/notes-tools.json`), and one `simple-notes` verb for each.
- **On the Mac** (CLI and stdio):  the tools run the script directly.  The body goes on standard input, or HTML through a mode-600 temporary file, never on the command line.
- **Everywhere else** (the server's CLIs and stdio, hosted ST-MCP):  the request goes to the Operator's notes queue over signed HTTPS, and a seatless Mac runner runs the same script.  The caller waits for the result.
- **Who may do what.**  A create's title carries the claimed identity's own agent name (the closed `[TEAM_KEY, Agent] Topic` grammar).  Update, pin, unpin, read and list work on any Coding note (owner, Oct 22), and an update of a note not titled with the caller's name never creates it.

The first design (AFC-331, Oct 15 to Oct 22) relayed requests through a public Zulip channel, `#notes-relay`, which the Mac listener consumed.  It was retired on Oct 22 at the owner's direction:  "I don't want notes going from Zulip to Apple notes."  The channel, the consumer (`notes_relay.py`), the `[notes_relay]` listener section, the `NOTES_RELAY_CONSUMER` knob and the Worker's relay code are gone.

## 6. Shell Parity on the Server

The server mirror (`fleet-cli-release.py` allowlist, pushed by `fleet-cli-sync.py`) gains `recall`, `lane` and `fleet-mode` alongside `simple-chat` and `simple-board`, plus the docs `fleet-mode` prints.  `apple-notes-coding.sh` is not mirrored;  the server reaches Notes through the Operator's notes queue (section 5).  The tool-parity doctor (`scripts/fleet-tools-doctor.py`) checks the same list on both hosts.

## 7. The Guide

One guide, `docs/protocols/simple-tools-guide.md` (written, AFC-331):  every tool by family with a one-line purpose, how to pass `identity`, which connection each client uses and how to arm it, the old-to-new name table, Apple Notes, and the CLI equivalents.  Agents reach it four ways:

- **MCP `instructions`.**  Both servers already send them at `initialize`, and most clients load them unasked.  They stay short:  the identity rule, the families, and where the guide is.
- **A `guide` tool** (unprefixed, beside `whoami` and `identities`) that returns the guide or one section of it, for clients that ignore `instructions`.
- **FleetLink**, at `https://fleetlink.online/simple-tools-guide/`, beside the Zulip and Linear guides, for agents with no MCP connection yet.
- **A skill**, `simple-tools-guide`, installed for every platform that takes skills.

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
