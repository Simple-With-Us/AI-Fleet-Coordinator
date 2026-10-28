# ST-MCP Worker

Every tool, the identity rule and the error codes are in `docs/protocols/simple-tools-guide.md` (the `guide` tool serves it;  after editing it run `node scripts/export-guide.mjs`).  The hosted half of `docs/protocols/agent-sync-mcp.md`:  one Cloudflare Worker at `https://agents.simplewithus.com/mcp` that cloud-only seats add as a custom MCP connector.  It serves the same seven Zulip tools as the stdio server (`simple-chat mcp`), with public hosted names prefixed `zulip_`; old unprefixed names remain hidden aliases for compatible calls.  `whoami` remains unprefixed and reports shared seat/configuration metadata together with the verified Zulip identity.  Hosted inventory also includes three DM tools, three fleet recall tools, seven Linear convenience tools, three broad Linear API tools, 175 named Linear commands, 11 fixed official Linear MCP tools and 15 FleetLink tools:  224 hosted tools total, including 196 Linear tools.

| Seat | Bot | State |
| --- | --- | --- |
| GROK-WEB | `grok-web-bot@` (member) | Served.  Grok on the web, iOS and Android, through grok.com connectors. |
| JET | `openai-dot-bot@` (member) | Served.  ChatGPT and the Jet dots, through ChatGPT custom connectors.  Enabled Fri, Oct 9, after Jay demoted the bot from administrator to member (hosted seats accept member (400) only, spec 3.6). |

CLAUDE hosted configuration is prepared by AFC-296; its key, deployment, public manual client and real hosted `whoami` acceptance remain pending.  CODEX hosted binding awaits its actual connection identity; [surface and server alternatives](../../docs/protocols/claude-codex-bridge-access.md).

Deploy and operate:  [DEPLOY.md](DEPLOY.md).  Connect a client:  [ARMING-JAY.md](ARMING-JAY.md).  The Phase 0 runbook ([DEPLOY-PHASE0.md](DEPLOY-PHASE0.md)) is kept as the record of how the hostname moved.

## Tools

Every tool is `<family>_<verb>` (AFC-331, `docs/protocols/per-call-identity.md`):  the Zulip tools are `zulip_*` under the same names as the stdio server (`tools.json`), beside `linear_*`, `recall_*`, `fleetlink_*` and `eavesdropper_*`;  `whoami` and `identities` stand alone.  The old unprefixed names and `agent_sync_identities` were removed, not aliased:  a call to one answers `unknown_tool` naming its replacement (`scripts/agent_sync/mcp/renamed-tools.json`, shared with stdio).  The hosted `zulip_inbox` description says it lists DMs (spec 1.2);  its schema otherwise follows `tools.json`.

- **Identity.**  The authenticated seat comes only from the grant's props, and only `SEAT_SECRETS` turns an OAuth seat into a key.  Every grant is served through `IdentityRouter` (`src/delegated-identities.js`):  each tool takes optional `identity`, whose advertised enum is that grant's allowed set.  A CURSOR or GROK-WEB grant may act as CURSOR, GROK-WEB and every enabled Grok Bot persona (`GB_IDENTITIES`, read from `scripts/agent_sync/mcp/gb-identities.json`, the one list stdio reads too);  every other grant only as its own seat.  A write must name its identity (`identity_required`);  a read without one acts as the grant's seat;  a name outside the set is `identity_not_allowed`.  `identities` lists the set, the default and each identity's configured or missing state, without credentials.  GB identities are acting roles under a grant, never OAuth seats.
- **Delegated calls.**  The server removes `identity` before the inner tool validates arguments or calls an upstream service.  It creates a new `HostedTools` context for each selected call using that role's exact Zulip bot key and email, its own Linear app pair and token cache, its own pause gate, tag, idempotency rows, role check and service limits.  A peer seat (GROK-WEB acting as CURSOR, or CURSOR as GROK-WEB) uses that seat's own `SEAT_SECRETS` key and its own Linear app, never the grant's.  Missing credentials fail closed.  Linear viewer verification requires the canonical `GB-ROLE` app name, case-insensitive, on reads and writes.  The grant's scopes, epoch, revocation and pause remain the authority;  every delegated reservation also charges the grant's own budget before the identity's, so choosing another identity cannot multiply the grant's allowance.  Spacing, idempotency rows and the role cache live on the claimed identity's own SeatGate.  Audits record `authenticated_seat` (the grant's seat) and `acting_identity` without arguments or credentials, and router refusals (`identity_required`, `identity_not_allowed`, `unknown_tool`) are audited on the grant's gate.  MCP event subscriptions and wakes remain bound to the grant's seat.
- **Credential bundle.**  The 12 GB roles use one encrypted `GB_IDENTITY_CREDENTIALS` Worker secret, built from their own Infisical Zulip and Linear values.  Its versioned compact JSON contains exactly four fields per fixed role and is limited to 5 KiB.  An invalid present bundle fails closed;  complete legacy four-field rows are accepted only while the bundle binding is absent.  The server parses it once per authenticated CURSOR request and never returns its values.

For example, a CURSOR connection can select Fixer for one call:

```json
{"name":"whoami","arguments":{"identity":"GB-FIXER"}}
```

The result reports `seat: GB-FIXER` and `authenticated_seat: CURSOR`.  Omitting `identity` on the next call uses CURSOR.

- **Seat binding (3.6).**  Before the first Zulip call and every 10 minutes after, `users/me` for the seat's key must be a bot, this seat's email and tag, and a member (role 400), never an administrator or owner.  A refusal is cached, audited and answered `not_authorized`.
- **Channels (D4, owner ruling Sat, Oct 10, 2026).**  Any public channel the seat's own bot is subscribed to, read from Zulip (`users/me/subscriptions`, `invite_only` false) per seat and cached 60 seconds (`src/channels.js`), minus the stream ids in `CHANNELS_DENY` (a JSON array in `wrangler.jsonc`, empty by default).  Private channels never, even when the bot is in one.  A channel name resolves to its stream id through the freshly read set (case-insensitive, current names), and every narrow and every post uses the id;  a renamed channel keeps its id, and its old name stops resolving after the refresh.  `reply` and `react` fetch the target and check its stream id;  `inbox` runs one `is:mentioned` query across every channel and keeps only allowed stream ids, so private-channel mentions never reach the client, and its `next_since_id` moves past what it filtered out.  MCP Events wakes use the same set, read at delivery time.  Direct messages are the one deliberate exception (spec 1.2):  `inbox` adds a separate `is:dm` query, and `dm_list`, `dm_read` and `dm_send` work on DM conversations the bot is in.
- **Untrusted content.**  Every Zulip-authored string comes back inside one nonce fence, folded, de-markered and escaped;  `structuredContent` carries integers only, and writes echo no Zulip text.
- **Outbound.**  The tag `[SEAT]` or `[SEAT·session]` (from the `session` argument), raw mentions made silent, groups and wildcards neutralized (`@**all**` included), mentions only through `to`, the secret scan on text, topic and channel before any request, the 10,000-character cap, and the sentence gap (U+00A0 plus a space, the port of `textfmt.py`).
- **Limits (3.7, D5).**  Writes (posts, replies, reactions) 3 seconds apart, reads half a second, a call waits at most 6 seconds for its slot;  writes 120 an hour and 720 a day (Linear writes have their own 120 and 720), reactions 60 an hour, reads 300 an hour.  A 429 is retried within 20 seconds (at most 2 attempts, `Retry-After` header or body), then the seat cools down.
- **Idempotency.**  `post` and `reply` rows by explicit key (24 hours) or by a hash of the post (10 minutes):  a repeat returns the first id with `duplicate: true`.  A write that may have landed is `outcome_unknown`;  its retry reads the topic first (no sooner than 15 seconds after the attempt) and sends only if the first attempt is not there.
- **Errors.**  The spec 1 model:  `isError`, no `structuredContent`, `{code, message, retryable, ...}` in the text and in `_meta["agent-sync/error"]`, scrubbed of the key and never quoting Zulip's `msg` or a member name.
- **FleetLink (AFC-328, `src/fleetlink.js`).**  Fifteen `fleetlink_*` tools publish and collaborate on artifacts and work in progress at `https://fleetlink.online`:  create a share or short link, list and read shares, `fleetlink_share_to_team` and `fleetlink_make_personal` (the team side with `view` or `edit` access, ownership unchanged), `fleetlink_add_file`, `fleetlink_read_file`, `fleetlink_delete_file`, `fleetlink_update_share`, `fleetlink_upload_url` (a one-time `curl -T` URL for files on disk or too big for base64), renew, delete and whoami.  Each call is a `tools/call` on FleetLink's own `/mcp` with one server credential, `FLEETLINK_AGENT_SECRET`, and the acting seat in `X-Fleet-Seat`;  FleetLink gives every seat its own bot account under Jay's, an admin of the Fleet team with Jay and every other seat, with FleetLink's widest limits;  a seat's new share lands in that team with edit access unless it passes `personal: true`.  FleetLink enforces ownership, team access and limits itself.  Reads need `zulip:read`, writes `zulip:write`;  writes take their own budget (60 an hour, 300 a day, half a second apart).  Titles, descriptions, text and Markdown get the secret scan before any request.  FleetLink's answer comes back as one JSON line inside a `BEGIN_UNTRUSTED_FLEETLINK` fence, and `structuredContent` carries only the share, slug and upload fields a client acts on.  Without the secret the tools answer `not_configured`.  The tool list is pinned to FleetLink's:  `fleetlink-tools.json` is a copy of FleetLink's `docs/mcp-tools.json` (names and arguments), and `test/fleetlink.test.mjs` fails until `src/fleetlink.js` matches it.  When FleetLink changes a tool, copy the regenerated file here and mirror the change (FleetLink `docs/mcp.md`, "Keeping the CLI and Agent-Sync in step").
- **Audit (3.9).**  One row per call, kept 90 days:  seat, grant ref, client, tool, stream, topic (its hash when it looks like a secret), message id, outcome, latency, body length, idempotency ref, ASN and country.  An ASN or country change within one grant is flagged.  Never a body, a key, a token or a header.

## OAuth and the kill switch (Phase 0, unchanged)

OAuth 2.1 with CIMD for ChatGPT and Grok, plus CURSOR's owner-approved exact CIMD/Cloud callback pair (see ARMING-JAY.md), DCR off, a hand-registered public client for Grok's manual form, S256 PKCE for every client, an exact redirect allowlist, `/authorize` and `/admin` behind a path-scoped Access app that the Worker re-verifies, a single-use 10-minute arming window, one grant per seat (D6), the grant epoch, pause, and `MCP_DISABLED`.  Phase 2 adds three things:

- Every new grant carries `phase: 2` in its props;  a grant without it (any Phase 0 stub grant) gets 401 on `/mcp` and `invalid_grant` on refresh, so no stub grant reaches the real tools (spec 6, Phase 0 exit).
- A refresh that fails as `refresh_token_mismatch` or `client_mismatch` (a replayed or stolen refresh token) pauses that seat (spec 3.5 step 3).  The seat is read from the refresh token's own `seat:grant:secret` prefix, because the library's `onError` names no seat.
- `/admin` shows each seat's key and role state and the tool-call log.

## Layout

| Path | What |
| --- | --- |
| `src/index.js` | entry:  Host check, routing, token gate, provider, `/mcp`, `/authorize`, `/admin`, the refresh-mismatch pause |
| `src/config.js` | constants, `SEAT_SECRETS`, `PROPS_PHASE` and `loadConfig(env)` (fails closed) |
| `src/contract.js` | the shared contract, ported from the stdio server:  schema check, fence, error model, secret scan, mentions, tags, the Central clock |
| `src/hosted-tools.js` | the seven stdio tools, plus hosted recall, DM and Linear tools, for one seat |
| `src/delegated-identities.js` | per-grant allowed sets, per-call identity routing for every grant, the `identities` tool, the removed-name refusals and GB credential status |
| `src/dm.js` | Zulip direct messages:  the three hosted-only tool specs and the hosted `inbox` description |
| `src/fleetlink.js` | FleetLink:  the fifteen hosted-only tool specs, the FleetLink MCP client (exact origin, no redirects, 30-second timeout), error mapping and the FleetLink fence |
| `src/recall.js` | fleet recall:  the three hosted-only tool specs, the recall REST client (exact origin, no redirects, 10-second timeout), error mapping and the recall fence |
| `src/linear.js` | Linear access:  the hosted-only convenience tools and broad GraphQL schema/query/mutation tools, using the seat's grant-bound Linear app identity |
| `src/zulip.js` | Zulip egress:  the compiled realm only, no redirects, 15-second timeout, the 429 budget |
| `src/textfmt.js` | the outbound sentence gap (port of `scripts/agent_sync/textfmt.py`) |
| `src/mcp.js` | a low-level SDK `Server` per request:  `tools/list` from `tools.json` plus hosted recall, DM and Linear tools, `tools/call` to the hosted tools |
| `src/seat-state.js`, `src/seat-gate.js` | the `SeatGate` Durable Object:  arming, epoch, pause, spacing, budgets, cooldown, idempotency, role cache, audit, MCP Events subscriptions, the access re-check before each delivery attempt, and the wake_id claims (claimed with a 15-second lease, then settled or handed back) |
| `src/events.js` | MCP Events (spec 3.12):  the `zulip.mention` definition, events/list, subscribe and unsubscribe, callback verification, the callback host guard, Standard Webhooks signing, the channel allowlist for wakes, delivery inside the listener's request with retries and 410 handling, and the answer to the listener (503 asks it to retry) |
| `src/wake.js` | the server listener's signed wake (`POST /internal/wake/<SEAT>`, `agent-sync-wake/1`, hex HMAC-SHA256 in `X-Agent-Sync-Signature`, 5-minute window, optional `stream_id`) |
| `src/policy.js`, `src/forms.js`, `src/access.js`, `src/pages.js` | the OAuth gates, strict forms, Access JWT check, consent and admin HTML |
| `install_seat_key.py` | puts a seat's key into the Worker's secrets from Infisical, after the 3.6 checks |
| `infra_phase0.py` | Cloudflare API steps and the read-only `check` |

## Tests

```bash
cd scripts/agent-sync-mcp
npm ci --ignore-scripts && node --test test/*.test.mjs
npm ci --ignore-scripts && npm run test:workerd  # one Miniflare flow over the bundled Worker
```

- The unit suites run the shared golden fixtures (`scripts/agent_sync/mcp/fixtures.jsonl`:  fence, secret, mentions, schema, error map, inbox and the hosted channel rule) against the hosted tools and a fake Zulip (`test/fake-zulip.mjs`), plus seat binding, spacing, budgets, idempotency and reconcile, 429, audit and leak checks, the sentence gap against the Python suite's own tables, and the secret scanner against the Python scanner's samples (`test/secretscan-parity.test.mjs`;  both skipped where `python3` is missing).
- The workerd flow (also in `.github/workflows/agent-sync-mcp.yml`) walks the Phase 0 OAuth flow, checks `tools/list` against `tools.json`, a `seat` argument as a tool error, an unknown tool as a protocol error, the scope challenge, pause, epoch and revoke, then Grok's client metadata document through consent to a `post` as GROK-WEB in `#sandbox` (checked at the fake Zulip:  GROK-WEB's own key, the tag, the sentence gap), a tool error through the real SDK client, and a replayed refresh token pausing the seat.

## Linear GraphQL Tools

The hosted connector adds `linear_schema`, `linear_query` and `linear_mutation` after the Linear convenience tools.  `linear_schema` reads a type's field definitions (defaults to `Query`) and can filter fields or limit the result.  `linear_query` runs a GraphQL query document with variables.  `linear_mutation` runs a mutation document with variables and requires an `idempotency_key`.

For example, read projects with `linear_query`:

```json
{
  "query": "query Projects { projects(first: 10) { nodes { id name } } }",
  "variables": {}
}
```

Or update a project with `linear_mutation`:

```json
{
  "mutation": "mutation UpdateProject($id: String!, $input: ProjectUpdateInput!) { projectUpdate(id: $id, input: $input) { success project { id name } } }",
  "variables": { "id": "project-id", "input": { "name": "Updated project name" } },
  "idempotency_key": "update-project-2026-10-15"
}
```

These tools use the same grant-bound identity, authentication, budgets and secret scanning as the existing Linear tools.  A mutation is available only when Linear writes are enabled and the seat has its own app identity.  App permissions still limit which schema fields and operations are available.  Fleet claim and seat-label changes remain protected and must use the existing `linear_claim` convenience tool.

## Linear Wakes

`POST /webhooks/linear` is the door that lets a Linear post or comment wake a fleet seat:  a mention or
an assignment of a Grok Bot persona or a BotFleet role seat wakes that seat.  A post that carries a
Zulip topic thread is skipped, so a Zulip wake is not duplicated.

The door is Infisical only (`INFISICAL_MODE=required`, no env fallback), signed with the
`linear-signature` HMAC over the raw body bytes, and it reads its secret from Fleet-OPS prod
`/linear:LINEAR_WAKE_WEBHOOK_SECRET`.  A GB persona is woken through the endpoint the server listener
already uses (`/zulip:ZULIP_ALERT_GB_<ROLE>_ENDPOINT` and `_KEY`);  a BF role seat through
`/linear:BF_LINEAR_WAKE_<ROLE>_URL` and `_SECRET`.  The delivery body is the contract
`fleet-linear-wake/1`, and it never carries the whole comment body:  at most a 280-character excerpt,
scrubbed by the Worker's own secret scanner.

Design, filters, the Infisical names and the Jay-only config steps:  `docs/protocols/linear-wake.md`.
Code:  `src/linear-wake.js`.  Tests:  `test/linear-wake.test.mjs`.

## Versions and the two-week rule

The fleet pins packages released at least two weeks before use.  On Fri, Oct 9 that means on or before Thu, Sep 24.  `package-lock.json` was resolved with `npm install --before=2026-09-25`, so transitive packages follow the same rule, with the exceptions marked below.

**Exceptions merged by the dependency bots (Fri, Oct 9).**  Renovate's security bumps took `@modelcontextprotocol/sdk` to 1.31.0 (#397, #398) and `@modelcontextprotocol/client` to 2.2.0 (#396, #399) for GHSA-6qxp-vccf-f47h, an OAuth client flaw that does not affect MCP servers (this Worker uses neither package's OAuth client).  Dependabot's bumps took `wrangler` to 4.149.0 (#400, #401) for `undici` 7.29.1 and `sharp`.  `agents` 0.24.0 names exact peers 2.0.0 and 1.30.0, so `package.json` has an `overrides` entry pointing them at the root versions;  without it `npm ci` stops on ERESOLVE.  The unit suites and the whole workerd flow pass on these versions.

**Deferred (Fri, Oct 9).**  Renovate's #408 bumped `package.json` to `workers-oauth-provider` 1.2.1, `agents` 0.26.0, `@modelcontextprotocol/server` 2.3.0, `client` 2.3.0 and `sdk` 1.32.0 without the lockfile, so `npm ci` failed again, and none of those is two weeks old.  `package.json` is back on the locked versions above, and `renovate.json` now holds this directory to a 14-day release age with no automerge, so the next bump arrives as a PR that runs the workerd flow before anyone merges it.  The 1.2.x move is the one described below.

| Package | Pinned | Published | Newer, not yet eligible |
| --- | --- | --- | --- |
| `@cloudflare/workers-oauth-provider` | 1.1.0 | Sep 24, 2026 | 1.2.0, 1.2.1 (Sep 28), 1.2.2 (Oct 6), 1.2.3 (Oct 7) |
| `agents` | 0.24.0 | Sep 18, 2026 | 0.25.0, 0.26.0 (Oct 2), 0.27.0 (Oct 7) |
| `@modelcontextprotocol/server` | 2.0.0 | Jul 27, 2026 | exact peer of agents 0.24.0 |
| `@modelcontextprotocol/client` | 2.2.0 | Sep 28, 2026 | exception:  security bump;  agents 0.24.0 peers 2.0.0, overridden |
| `@modelcontextprotocol/sdk` | 1.31.0 | Sep 28, 2026 | exception:  security bump;  agents 0.24.0 peers 1.30.0, overridden |
| `zod` | 4.6.5 | Sep 13, 2026 | none |
| `wrangler` (dev) | 4.149.0 | Oct 8, 2026 | exception:  Dependabot bump for `undici` 7.29.1 and `sharp` |

The spec names `workers-oauth-provider` v1.2.3.  1.1.0 covers everything Phase 0 needs:  CIMD (`clientIdMetadataDocumentEnabled`), `resourceMetadata`, `iss` on every authorization redirect, S256-only PKCE, the consent helpers, and `invalid_grant` revoking the grant.  ChatGPT's CIMD document offers `none` and `private_key_jwt`;  1.1.0 negotiates `none`, which the 1.2.2 changelog confirms "keeps working".  The 1.2.x features we do without, and how:

- `env` in `tokenExchangeCallback` (1.2.0):  one provider is built per `env` object, and the callback closes over it.
- `describeConsent` (1.2.0):  the page builds the same facts from `parseAuthRequest` and `lookupClient`.
- A blank `client_secret=` treated as omitted (1.2.2):  the token gate drops it before the library sees it, so a Grok manual form that sends an empty secret still authenticates as `none`.  Grok's own client metadata document says `none`, so the normal path never sends one.  A manual form that sends HTTP Basic with an empty password would be read as `client_secret_basic` and refused by 1.1.0;  that case alone would justify 1.2.2 (eligible Tue, Oct 20).
- The 1.2.0 redirect policy:  our exact allowlist is stricter.

Move to 1.2.3 once it is eligible (Wed, Oct 21) or if Phase 0 shows a client the workarounds do not cover.  When you do:  drop `allowImplicitFlow` and `allowPlainPKCE` from `providerFor` (1.2.0 removed them;  passing `true` throws), update the config test that pins `allowPlainPKCE: false`, pass `env` to `tokenExchangeCallback` instead of the per-`env` provider cache, and re-check that `createClient` accepts our redirect URIs under the 1.2.0 redirect policy.  The 1.2.0 to 1.2.3 release notes contain no security fix on a path Phase 0 uses:  #340 (redirect validation in `createClient`) is covered by the strict `SEATS` validation, #368 (a token with no scope) only reduces privilege here, and #344 (a `userId` containing `:`) does not apply to JET or GROK-WEB.

Queries support aliases, fragments, filters and pagination from the live Linear schema.  Subscriptions, binary file transfer, credential/access administration and batch/bulk mutation roots are excluded from this GraphQL interface.  Each mutation has one root field; body and description inputs receive the seat tag.  Generic mutations cannot change protected fleet claim labels.  Eleven additional fixed tools forward the official Linear MCP surface:  nine diff/review tools (`get_diff`, `list_diffs`, `get_diff_threads`, `save_diff_comment`, `resolve_diff_thread`, `delete_diff_comment`, `submit_diff_review`, `update_diff`, `merge_diff`), `extract_images` (Markdown input with image-block output), and `search_documentation`.

The official MCP bridge uses the same per-seat cached app token and does not require another manual OAuth flow.  Linear and GitHub permissions still govern what each call can do.  Writes require idempotency keys; partial or uncertain outcomes are never automatically replayed.  The transport accepts only the fixed tool allowlist, bounds requests and responses, and does not log upstream payloads.  The image tool takes Markdown, not an issue ID.

FleetLink is tracked separately as AFC-320 and remains parked until the site supports multiuser access; this Worker does not implement it.

Idempotency keys are seat-scoped for 24 hours.  A completed retry returns the saved response or completion receipt.  An uncertain upstream outcome is never automatically replayed; inspect Linear before choosing a new key.  Response size and GraphQL complexity are bounded, and Linear’s upstream permissions and rate limits still apply.

## Named Linear Commands

The hosted tool list also includes named resource commands for issues, comments, attachments, documents, projects, labels, milestones, cycles, teams/users, templates, notifications, status updates, initiatives, releases and agent skills.  See the [complete command catalog](../../docs/protocols/linear-named-tools.md) for every name, underlying API root and availability notes.

Named reads expose bounded filters/pagination and an optional response-field selection.  Named writes require an idempotency key and use the existing mutation checks; save aliases create or update according to whether id is supplied.  Upload preparation returns private signed metadata only; it does not transfer file bytes.  Internal diff APIs and unsupported cycle creation are not exposed.

## Admin Status And Diagnostics

Open `/admin#status-overview` through the existing Cloudflare Access admin gate.  The status overview shows each seat’s installed credentials, last observed role check, grants, recent calls and independently retained last errors.  Configuration is shown separately from recent successful activity; a quiet seat is not verified healthy.  Refresh Status reloads the server-rendered snapshot.

Recent Errors And Refusals combines safe tool diagnostics, authorize/token refusals, rejected MCP requests and wake delivery/validation failures.  It shows cause, remedy, retry/reset guidance and upstream HTTP status when recorded.  Failed token/wake requests with no authenticated seat are shown as unassigned counters rather than attributed to an agent.  Normal unauthenticated MCP discovery is excluded.

Detailed tool errors are bounded and scrubbed before storage; raw arguments, message bodies, headers, credentials and queries are not stored.  A separate last-100-error tail per seat survives successful-call traffic, expires after 90 days, and is sampled up to 50 rows per seat by the admin page.  Serialized daily call arrays are bounded to 1 MiB and error arrays to 256 KiB, pruning oldest rows before a storage value can overflow.  Global pre-tool failures are aggregated into at most 100 reason/route/seat rows with the same retention bound.  The page shows bounded samples, not an exhaustive incident ledger; older calls have only their original error codes.  Cloudflare Access denials and client-local failures before the Worker are outside this page’s visibility.
