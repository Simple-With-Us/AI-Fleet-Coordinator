# agent-sync MCP Worker

The hosted half of `docs/protocols/agent-sync-mcp.md`:  one Cloudflare Worker at `https://agent-sync.jays.services/mcp` that cloud-only seats add as a custom MCP connector.  It serves the same seven tools as the stdio server (`agent-sync mcp`), from the same `scripts/agent_sync/mcp/tools.json` (the hosted `inbox` also lists DMs, so its description differs), and each seat posts as its own Zulip bot.  It also serves three hosted-only Zulip direct-message tools (`dm_list`, `dm_read`, `dm_send`;  spec 1.2) and three hosted-only fleet recall tools (`recall_search`, `recall_stats`, `recall_contribute`;  spec 1.1).

| Seat | Bot | State |
| --- | --- | --- |
| GROK-WEB | `grok-web-bot@` (member) | Served.  Grok on the web, iOS and Android, through grok.com connectors. |
| JET | `openai-dot-bot@` (member) | Served.  ChatGPT and the Jet dots, through ChatGPT custom connectors.  Enabled Fri, Oct 9, after Jay demoted the bot from administrator to member (hosted seats accept member (400) only, spec 3.6). |

Deploy and operate:  [DEPLOY.md](DEPLOY.md).  Connect a client:  [ARMING-JAY.md](ARMING-JAY.md).  The Phase 0 runbook ([DEPLOY-PHASE0.md](DEPLOY-PHASE0.md)) is kept as the record of how the hostname moved.

## Tools

`whoami`, `topics`, `read_topic`, `inbox`, `post`, `reply`, `react`, exactly as `tools.json` defines them (spec section 1), except that the hosted `inbox` description says it lists DMs (spec 1.2);  its schema is the same.  `tools/list` serves that file, so both transports list the same schemas, and a test pins it.

- **Identity.**  The seat comes only from the grant's props, and only `SEAT_SECRETS` turns a seat into a key.  No schema takes a seat;  an extra argument is an `invalid_argument` tool error.
- **Seat binding (3.6).**  Before the first Zulip call and every 10 minutes after, `users/me` for the seat's key must be a bot, this seat's email and tag, and a member (role 400), never an administrator or owner.  A refusal is cached, audited and answered `not_authorized`.
- **Channels (D4).**  `#agent-sync` and `#sandbox`, pinned name to stream id in `CHANNELS`.  Every narrow and every post uses the id, never a name lookup.  `reply` and `react` fetch the target and check its stream;  `inbox` runs one stream-scoped `is:mentioned` query per channel and drops anything else, so private-channel mentions never reach the client.  Direct messages are the one deliberate exception (spec 1.2):  `inbox` adds a separate `is:dm` query, and `dm_list`, `dm_read` and `dm_send` work on DM conversations the bot is in.
- **Untrusted content.**  Every Zulip-authored string comes back inside one nonce fence, folded, de-markered and escaped;  `structuredContent` carries integers only, and writes echo no Zulip text.
- **Outbound.**  The tag `[SEAT]` or `[SEAT·session]` (from the `session` argument), raw mentions made silent, groups and wildcards neutralized (`@**all**` included), mentions only through `to`, the secret scan on text, topic and channel before any request, the 10,000-character cap, and the sentence gap (U+00A0 plus a space, the port of `textfmt.py`).
- **Limits (3.7, D5).**  Writes (posts, replies, reactions) 3 seconds apart, reads half a second, a call waits at most 6 seconds for its slot;  writes 20 an hour and 120 a day, reactions 60 an hour, reads 300 an hour.  A 429 is retried within 20 seconds (at most 2 attempts, `Retry-After` header or body), then the seat cools down.
- **Idempotency.**  `post` and `reply` rows by explicit key (24 hours) or by a hash of the post (10 minutes):  a repeat returns the first id with `duplicate: true`.  A write that may have landed is `outcome_unknown`;  its retry reads the topic first (no sooner than 15 seconds after the attempt) and sends only if the first attempt is not there.
- **Errors.**  The spec 1 model:  `isError`, no `structuredContent`, `{code, message, retryable, ...}` in the text and in `_meta["agent-sync/error"]`, scrubbed of the key and never quoting Zulip's `msg` or a member name.
- **Audit (3.9).**  One row per call, kept 90 days:  seat, grant ref, client, tool, stream, topic (its hash when it looks like a secret), message id, outcome, latency, body length, idempotency ref, ASN and country.  An ASN or country change within one grant is flagged.  Never a body, a key, a token or a header.

## OAuth and the kill switch (Phase 0, unchanged)

OAuth 2.1 with CIMD for ChatGPT and Grok, DCR off, a hand-registered public client for Grok's manual form, S256 PKCE for every client, an exact redirect allowlist, `/authorize` and `/admin` behind a path-scoped Access app that the Worker re-verifies, a single-use 10-minute arming window, one grant per seat (D6), the grant epoch, pause, and `MCP_DISABLED`.  Phase 2 adds three things:

- Every new grant carries `phase: 2` in its props;  a grant without it (any Phase 0 stub grant) gets 401 on `/mcp` and `invalid_grant` on refresh, so no stub grant reaches the real tools (spec 6, Phase 0 exit).
- A refresh that fails as `refresh_token_mismatch` or `client_mismatch` (a replayed or stolen refresh token) pauses that seat (spec 3.5 step 3).  The seat is read from the refresh token's own `seat:grant:secret` prefix, because the library's `onError` names no seat.
- `/admin` shows each seat's key and role state and the tool-call log.

## Layout

| Path | What |
| --- | --- |
| `src/index.js` | entry:  Host check, routing, token gate, provider, `/mcp`, `/authorize`, `/admin`, the refresh-mismatch pause |
| `src/config.js` | constants, `SEAT_SECRETS`, `PROPS_PHASE` and `loadConfig(env)` (fails closed) |
| `src/contract.js` | the shared contract, ported from the stdio server:  schema check, fence, error model, secret scan, mentions, tags, the Central clock |
| `src/hosted-tools.js` | the seven tools, plus the recall and DM tools, for one seat |
| `src/dm.js` | Zulip direct messages:  the three hosted-only tool specs and the hosted `inbox` description |
| `src/recall.js` | fleet recall:  the three hosted-only tool specs, the recall REST client (exact origin, no redirects, 10-second timeout), error mapping and the recall fence |
| `src/zulip.js` | Zulip egress:  the compiled realm only, no redirects, 15-second timeout, the 429 budget |
| `src/textfmt.js` | the outbound sentence gap (port of `scripts/agent_sync/textfmt.py`) |
| `src/mcp.js` | a low-level SDK `Server` per request:  `tools/list` from `tools.json` plus the recall and DM tools, `tools/call` to the hosted tools |
| `src/seat-state.js`, `src/seat-gate.js` | the `SeatGate` Durable Object:  arming, epoch, pause, spacing, budgets, cooldown, idempotency, role cache, audit, MCP Events subscriptions, the access re-check before each delivery attempt, and the wake_id claims (claimed with a 15-second lease, then settled or handed back) |
| `src/events.js` | MCP Events (spec 3.12):  the `zulip.mention` definition, events/list, subscribe and unsubscribe, callback verification, the callback host guard, Standard Webhooks signing, the channel allowlist for wakes, delivery inside the listener's request with retries and 410 handling, and the answer to the listener (503 asks it to retry) |
| `src/wake.js` | the server listener's signed wake (`POST /internal/wake/<SEAT>`, `agent-sync-wake/1`, hex HMAC-SHA256 in `X-Agent-Sync-Signature`, 5-minute window, optional `stream_id`) |
| `src/policy.js`, `src/forms.js`, `src/access.js`, `src/pages.js` | the OAuth gates, strict forms, Access JWT check, consent and admin HTML |
| `install_seat_key.py` | puts a seat's key into the Worker's secrets from Infisical, after the 3.6 checks |
| `infra_phase0.py` | Cloudflare API steps and the read-only `check` |

## Tests

```bash
cd scripts/agent-sync-mcp
node --test test/*.test.mjs                      # no install needed (ci.yml runs this)
npm ci --ignore-scripts && npm run test:workerd  # one Miniflare flow over the bundled Worker
```

- The unit suites run the shared golden fixtures (`scripts/agent_sync/mcp/fixtures.jsonl`:  fence, secret, mentions, schema, error map, inbox and the hosted allowlist) against the hosted tools and a fake Zulip (`test/fake-zulip.mjs`), plus seat binding, spacing, budgets, idempotency and reconcile, 429, audit and leak checks, and the sentence gap against the Python suite's own tables (skipped where `python3` is missing).
- The workerd flow (also in `.github/workflows/agent-sync-mcp.yml`) walks the Phase 0 OAuth flow, checks `tools/list` against `tools.json`, a `seat` argument as a tool error, an unknown tool as a protocol error, the scope challenge, pause, epoch and revoke, then Grok's client metadata document through consent to a `post` as GROK-WEB in `#sandbox` (checked at the fake Zulip:  GROK-WEB's own key, the tag, the sentence gap), a tool error through the real SDK client, and a replayed refresh token pausing the seat.

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
