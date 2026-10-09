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
| 3.3 endpoints | `/mcp`, `/oauth/token`, the two metadata documents, and a static `GET /health` (200 `{"ok":true}`, no data;  the fleet admin panel probes it);  everything else 404.  DCR is off (no registration endpoint) |
| 3.3 abuse limits | a `client_id` must be an exact allowlisted CIMD id or the shape the library generates for a hand-registered client (16 characters of `[A-Za-z0-9_-]`);  anything else is refused on `/authorize` and `/oauth/token` before the library could fetch it.  The token gate reads the request the way the library does:  its Content-Type test is the library's own expression, any `Authorization` that is not strict `Basic <base64>` is refused (the library splits the scheme on a space or tab, JS `\s` does not), every candidate id is checked, and the library receives a request rebuilt from the validated form.  Authorize refusals (Access email, time) and token refusals (counted per reason and id) go to separate tables in `/admin`;  `TOKEN_RATE_LIMITER` caps `/oauth/token` per IP |
| 3.3 Access | `/authorize` and `/admin` sit behind a path-scoped Access app, and the Worker re-verifies `Cf-Access-Jwt-Assertion` (certificates, `aud`, `iss`, `exp`, owner email).  Missing config fails closed |
| 3.3 401 | unauthenticated `/mcp`, `initialize` included, gets 401 with `resource_metadata` |
| 3.4 arming | `/admin` opens a single-use 10-minute window per seat;  outside it `/authorize` shows "No Connection Expected" and fetches nothing |
| 3.4 gate | exact redirect allowlist after a strict URL parse, PKCE S256 for every client, `resource` must be `/mcp`, scopes within `zulip:read zulip:write` |
| 3.4 consent | `beginConsent`/`approveConsent`/`denyConsent` (browser-bound, single-use handle), a no-script page with `default-src 'none'` and `frame-ancestors 'none'`, the seat re-validated against the stored request's redirect family on POST.  Every page and redirect carries `Referrer-Policy: same-origin`:  `no-referrer` makes browsers send `Origin: null` on form POSTs, which the same-origin check (correctly) refuses |
| 3.5 grant binding | the arming window is consumed and the epoch bumped atomically, then `completeAuthorization` with the seat in props, then every other grant of the seat is revoked (D6) |
| 3.5 token checks | `tokenExchangeCallback` re-checks seat, epoch and grant age (`invalid_grant`) and pause (`temporarily_unavailable`, grant kept);  every `/mcp` call re-checks the same and answers 401 `invalid_token` |
| 3.5 lifetimes | 1-hour access tokens, `refreshTokenTTL` 90 days, set explicitly |
| 3.10 kill switch | `/admin` Pause, Revoke All And Bump Epoch;  `MCP_DISABLED` = `"1"` makes `/mcp` answer 503 |
| 6 spike | the two tools are registered from checked-in JSON Schemas (`src/tools.phase0.json`) through `fromJsonSchema` with the cf-worker validator, and the workerd test asserts `tools/list` matches the file |

Not in Phase 0 (and the 6 spike is partial:  only the two Phase 0 schemas go through `createMcpHandler`;  the full seven-tool `post` schema, with its patterns, `maxItems`, defaults and `outputSchema`, is registered in the Phase 2 workerd test before real tools land):  Zulip calls, `tools.json` and the seven real tools, spacing and budgets, idempotency, the full audit schema, ASN and country flags, and pausing on refresh-token mismatch (logged only).

## Layout

| Path | What |
| --- | --- |
| `src/index.js` | entry:  Host check, routing, token gate, provider, `/mcp`, `/authorize`, `/admin` |
| `src/config.js` | constants and `loadConfig(env)` (fails closed) |
| `src/policy.js` | pure gates:  Host, redirect, client id, PKCE, resource, token form and header, bounded body read, same-origin |
| `src/forms.js` | zod schemas for the consent POST and the `/admin/action` POST (strict:  unknown keys, repeated keys, files and out-of-enum values are refused) |
| `src/access.js` | Access JWT verification (WebCrypto) |
| `src/seat-state.js` | arming, epoch, pause and audit logic over any key-value store |
| `src/seat-gate.js` | the `SeatGate` Durable Object (RPC wrapper over `seat-state.js`) |
| `src/pages.js` | consent, notice and admin HTML |
| `src/tool-logic.js`, `src/tools.phase0.json`, `src/mcp.js` | the two tools and the MCP handler |
| `infra_phase0.py` | Cloudflare API steps for the runbook, dry run by default |

## Tests

```bash
cd scripts/agent-sync-mcp
node --test test/*.test.mjs   # pure logic, no install needed (ci.yml runs this;  test/forms.test.mjs skips itself without zod)
npm ci --ignore-scripts && npm run test:workerd  # one Miniflare flow over the bundled Worker
```

The workerd flow (also run in CI by `.github/workflows/agent-sync-mcp.yml` on changes under this directory) covers:  foreign Host and workers.dev 404, `/health`, metadata `issuer` equal to `authorization_servers[0]` byte for byte, the 401 on an unauthenticated `initialize`, Access refusal, the unarmed notice with no CIMD fetch, unlisted CIMD refused before any fetch, admin CSRF, consent with `iss` on the redirect, the PKCE code exchange with ChatGPT's real CIMD document, `tools/list` against `tools.phase0.json`, a `seat` argument refused, the scope challenge, pause, two parallel approvals leaving one grant, a reused handle, a cross-family seat, an epoch bump alone killing a grant, revoke, the deny path, an expired arming window, Grok's published client metadata document (consent, a `none` exchange, `hello` as GROK-WEB, refresh), the manual-form fallback with a blank `client_secret` and a foreign redirect landing in the refusal log with the Access email, parser-differential tricks on `/oauth/token` fetching nothing, and a browser-style Origin derived from each page's `Referrer-Policy`.

## Versions and the two-week rule

The fleet pins packages released at least two weeks before use.  On Fri, Oct 9 that means on or before Thu, Sep 24.  `package-lock.json` was resolved with `npm install --before=2026-09-25`, so transitive packages follow the same rule, with one exception:  the two Renovate security bumps below.

**Security exception (Fri, Oct 9).**  Renovate merged `@modelcontextprotocol/sdk` 1.31.0 (#397) and `@modelcontextprotocol/client` 2.2.0 (#396) for GHSA-6qxp-vccf-f47h, an OAuth client flaw that sends stored credentials to an authorization server the MCP server names.  Both were published Mon, Sep 28, so they are under two weeks old and become eligible Mon, Oct 12.  The advisory does not affect MCP servers, and this Worker uses neither package's OAuth client, but the bumps changed `package.json` without the lockfile, so `npm ci` failed on every run.  The lockfile now carries them (re-resolved with `--before=2026-09-29`, which adds only `@modelcontextprotocol/core` 2.2.0 under the client).  `agents` 0.24.0 names exact peers 2.0.0 and 1.30.0, so `package.json` has an `overrides` entry that points its peers at the root versions;  without it `npm ci` stops on ERESOLVE.  The whole workerd flow passes with the override.

| Package | Pinned | Published | Newer, not yet eligible |
| --- | --- | --- | --- |
| `@cloudflare/workers-oauth-provider` | 1.1.0 | Sep 24, 2026 | 1.2.0, 1.2.1 (Sep 28), 1.2.2 (Oct 6), 1.2.3 (Oct 7) |
| `agents` | 0.24.0 | Sep 18, 2026 | 0.25.0, 0.26.0 (Oct 2), 0.27.0 (Oct 7) |
| `@modelcontextprotocol/server` | 2.0.0 | Jul 27, 2026 | exact peer of agents 0.24.0 |
| `@modelcontextprotocol/client` | 2.2.0 | Sep 28, 2026 | security exception (above);  agents 0.24.0 peers 2.0.0, overridden |
| `@modelcontextprotocol/sdk` | 1.31.0 | Sep 28, 2026 | security exception (above);  agents 0.24.0 peers 1.30.0, overridden |
| `zod` | 4.6.5 | Sep 13, 2026 | none |
| `wrangler` (dev) | 4.139.0 | Sep 24, 2026 | 4.140.0 and later |

The spec names `workers-oauth-provider` v1.2.3.  1.1.0 covers everything Phase 0 needs:  CIMD (`clientIdMetadataDocumentEnabled`), `resourceMetadata`, `iss` on every authorization redirect, S256-only PKCE, the consent helpers, and `invalid_grant` revoking the grant.  ChatGPT's CIMD document offers `none` and `private_key_jwt`;  1.1.0 negotiates `none`, which the 1.2.2 changelog confirms "keeps working".  The 1.2.x features we do without, and how:

- `env` in `tokenExchangeCallback` (1.2.0):  one provider is built per `env` object, and the callback closes over it.
- `describeConsent` (1.2.0):  the page builds the same facts from `parseAuthRequest` and `lookupClient`.
- A blank `client_secret=` treated as omitted (1.2.2):  the token gate drops it before the library sees it, so a Grok manual form that sends an empty secret still authenticates as `none`.  Grok's own client metadata document says `none`, so the normal path never sends one.  A manual form that sends HTTP Basic with an empty password would be read as `client_secret_basic` and refused by 1.1.0;  that case alone would justify 1.2.2 (eligible Tue, Oct 20).
- The 1.2.0 redirect policy:  our exact allowlist is stricter.

Move to 1.2.3 once it is eligible (Wed, Oct 21) or if Phase 0 shows a client the workarounds do not cover.  When you do:  drop `allowImplicitFlow` and `allowPlainPKCE` from `providerFor` (1.2.0 removed them;  passing `true` throws), update the config test that pins `allowPlainPKCE: false`, pass `env` to `tokenExchangeCallback` instead of the per-`env` provider cache, and re-check that `createClient` accepts our redirect URIs under the 1.2.0 redirect policy.  The 1.2.0 to 1.2.3 release notes contain no security fix on a path Phase 0 uses:  #340 (redirect validation in `createClient`) is covered by the strict `SEATS` validation, #368 (a token with no scope) only reduces privilege here, and #344 (a `userId` containing `:`) does not apply to JET or GROK-WEB.
