# agent-sync MCP Worker (Phase 0 stub)

The hosted half of `docs/protocols/agent-sync-mcp.md`:  one Cloudflare Worker at `https://agent-sync.jays.services/mcp` that cloud-only seats (JET in ChatGPT, GROK-WEB on grok.com) add as a custom MCP connector.  Phase 0 proves the OAuth path end to end with two tools that hold no Zulip key:

- `hello` (read-only) returns `{seat, scopes, client_id}` from the grant.
- `hello_write` is marked as a write, so the client asks for confirmation the way it will for a real post, and returns an acknowledgment.  It posts nothing.

Deploy:  [DEPLOY-PHASE0.md](DEPLOY-PHASE0.md).  Connect and exit criteria:  [ARMING-JAY.md](ARMING-JAY.md).

## What Phase 0 implements

| Spec | Here |
| --- | --- |
| 3.1, 3.2 OAuth 2.1 server, KV, Durable Object | `@cloudflare/workers-oauth-provider` `OAuthProvider` with `apiRoute: "/mcp"`, `OAUTH_KV`, `SeatGate` (SQLite) |
| 3.2 hostnames | `workers_dev: false`, `preview_urls: false`, custom domain only;  the outer `fetch` answers 404 to any Host but `agent-sync.jays.services`;  `createMcpHandler` gets `allowedHostnames` |
| 3.3 endpoints | `/mcp`, `/oauth/token`, the two metadata documents;  everything else 404.  DCR is off (no registration endpoint) |
| 3.3 abuse limits | a URL-shaped `client_id` that is not an allowlisted CIMD id is refused on `/authorize` and `/oauth/token` before the library could fetch it;  refusals are logged to `/admin`;  `TOKEN_RATE_LIMITER` caps `/oauth/token` per IP |
| 3.3 Access | `/authorize` and `/admin` sit behind a path-scoped Access app, and the Worker re-verifies `Cf-Access-Jwt-Assertion` (certificates, `aud`, `iss`, `exp`, owner email).  Missing config fails closed |
| 3.3 401 | unauthenticated `/mcp`, `initialize` included, gets 401 with `resource_metadata` |
| 3.4 arming | `/admin` opens a single-use 10-minute window per seat;  outside it `/authorize` shows "No Connection Expected" and fetches nothing |
| 3.4 gate | exact redirect allowlist after a strict URL parse, PKCE S256 for every client, `resource` must be `/mcp`, scopes within `zulip:read zulip:write` |
| 3.4 consent | `beginConsent`/`approveConsent`/`denyConsent` (browser-bound, single-use handle), a no-script page with `default-src 'none'` and `frame-ancestors 'none'`, the seat re-validated against the stored request's redirect family on POST |
| 3.5 grant binding | the arming window is consumed and the epoch bumped atomically, then `completeAuthorization` with the seat in props, then every other grant of the seat is revoked (D6) |
| 3.5 token checks | `tokenExchangeCallback` re-checks seat, epoch and grant age (`invalid_grant`) and pause (`temporarily_unavailable`, grant kept);  every `/mcp` call re-checks the same and answers 401 `invalid_token` |
| 3.5 lifetimes | 1-hour access tokens, `refreshTokenTTL` 90 days, set explicitly |
| 3.10 kill switch | `/admin` Pause, Revoke All And Bump Epoch;  `MCP_DISABLED` = `"1"` makes `/mcp` answer 503 |
| 6 spike | the two tools are registered from checked-in JSON Schemas (`src/tools.phase0.json`) through `fromJsonSchema` with the cf-worker validator, and the workerd test asserts `tools/list` matches the file |

Not in Phase 0:  Zulip calls, `tools.json` and the seven real tools, spacing and budgets, idempotency, the full audit schema, ASN and country flags, and pausing on refresh-token mismatch (logged only).

## Layout

| Path | What |
| --- | --- |
| `src/index.js` | entry:  Host check, routing, token gate, provider, `/mcp`, `/authorize`, `/admin` |
| `src/config.js` | constants and `loadConfig(env)` (fails closed) |
| `src/policy.js` | pure gates:  Host, redirect, CIMD, PKCE, resource, token form, same-origin |
| `src/access.js` | Access JWT verification (WebCrypto) |
| `src/seat-state.js` | arming, epoch, pause and audit logic over any key-value store |
| `src/seat-gate.js` | the `SeatGate` Durable Object (RPC wrapper over `seat-state.js`) |
| `src/pages.js` | consent, notice and admin HTML |
| `src/tool-logic.js`, `src/tools.phase0.json`, `src/mcp.js` | the two tools and the MCP handler |
| `infra_phase0.py` | Cloudflare API steps for the runbook, dry run by default |

## Tests

```bash
cd scripts/agent-sync-mcp
node --test test/*.test.mjs   # pure logic, no install needed;  CI runs this
npm ci --ignore-scripts && npm run test:workerd  # one Miniflare flow over the bundled Worker
```

The workerd flow covers:  foreign Host and workers.dev 404, metadata `issuer` equal to `authorization_servers[0]` byte for byte, the 401 on an unauthenticated `initialize`, Access refusal, the unarmed notice with no CIMD fetch, unlisted CIMD refused before any fetch, admin CSRF, consent with `iss` on the redirect, the PKCE code exchange with ChatGPT's real CIMD document, `tools/list` against `tools.phase0.json`, a `seat` argument refused, the scope challenge, pause, two parallel approvals leaving one grant, a reused handle, a cross-family seat, an epoch bump alone killing a grant, revoke, and Grok's first attempt landing in the refusal log.

## Versions and the two-week rule

The fleet pins packages released at least two weeks before use.  On Fri, Oct 9 that means on or before Thu, Sep 24.  `package-lock.json` was resolved with `npm install --before=2026-09-25`, so transitive packages follow the same rule.

| Package | Pinned | Published | Newer, not yet eligible |
| --- | --- | --- | --- |
| `@cloudflare/workers-oauth-provider` | 1.1.0 | Sep 24, 2026 | 1.2.0, 1.2.1 (Sep 28), 1.2.2 (Oct 6), 1.2.3 (Oct 7) |
| `agents` | 0.24.0 | Sep 18, 2026 | 0.25.0, 0.26.0 (Oct 2), 0.27.0 (Oct 7) |
| `@modelcontextprotocol/server` | 2.0.0 | Jul 27, 2026 | exact peer of agents 0.24.0 |
| `@modelcontextprotocol/client` | 2.0.0 | Jul 27, 2026 | exact peer of agents 0.24.0 |
| `@modelcontextprotocol/sdk` | 1.30.0 | Jul 27, 2026 | exact peer of agents 0.24.0 |
| `zod` | 4.6.5 | Sep 13, 2026 | none |
| `wrangler` (dev) | 4.139.0 | Sep 24, 2026 | 4.140.0 and later |

The spec names `workers-oauth-provider` v1.2.3.  1.1.0 covers everything Phase 0 needs:  CIMD (`clientIdMetadataDocumentEnabled`), `resourceMetadata`, `iss` on every authorization redirect, S256-only PKCE, the consent helpers, and `invalid_grant` revoking the grant.  ChatGPT's CIMD document offers `none` and `private_key_jwt`;  1.1.0 negotiates `none`, which the 1.2.2 changelog confirms "keeps working".  The 1.2.x features we do without, and how:

- `env` in `tokenExchangeCallback` (1.2.0):  one provider is built per `env` object, and the callback closes over it.
- `describeConsent` (1.2.0):  the page builds the same facts from `parseAuthRequest` and `lookupClient`.
- A blank `client_secret=` treated as omitted (1.2.2):  the token gate drops it before the library sees it, so a Grok manual form that sends an empty secret still authenticates as `none`.
- The 1.2.0 redirect policy:  our exact allowlist is stricter.

Move to 1.2.3 once it is eligible (Wed, Oct 21) or if Phase 0 shows a client the workarounds do not cover.
