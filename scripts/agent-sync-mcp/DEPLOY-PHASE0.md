# Deploy Phase 0:  Runbook

Ordered steps to put the Phase 0 stub on `https://agent-sync.jays.services`.  The stub holds no Zulip key, so spec 3.2 allows today's credential (the Global key) for it.  Every Cloudflare change below is Jay-approved work for the parent session, run by hand, never from CI and never on merge.

Everything runs from `scripts/agent-sync-mcp/` in a checkout of the merged commit (a fresh worktree off `origin/main`, never `~/Code/AI-Fleet-Coordinator`).  `infra_phase0.py` loads the Global key inside its own process from `~/.secrets/global-api-keys` and never prints it.  Every step is a dry run until `--apply`.  Do not merge stderr into output (`2>&1`) on any command that touches `~/.secrets`.

## What holds the hostname today (read-only probe, Fri, Oct 9)

| Thing | State |
| --- | --- |
| DNS in zone `jays.services` (`f04fde898ac72c5087b8579844c2d474`, account Usage.Jays.Services) | `CNAME agent-sync.jays.services -> 6fa2a97c-b4f8-420d-94ae-bd9858aff4b6.cfargotunnel.com`, proxied, record id `d3c670eb8be9fab6578019ee3bf1ea5d`, comment "Repoint to healthy Mac tunnel Jay's Tunnel" |
| Cloudflare Tunnel "Jay's Tunnel" (`6fa2a97c-…`, healthy, 13 ingress rules) | rule `agent-sync.jays.services -> http://localhost:8787`.  **This is live, not dead config.**  The retired `agent-sync-push` is not what answers:  a read-only `lsof` on Fri, Oct 9 shows a `node` process (pid 51779) listening on `127.0.0.1:8787` whose working directory is `/Users/jay/Code/Congress.Trade` (8787 is `wrangler dev`'s default port).  Through the proxied CNAME the internet reaches it with no Access:  `GET /` is 404 and `GET /post` is 405.  That exposure predates this work and ends when step 4 deletes the CNAME |
| Worker routes and Worker custom domains | none for this hostname |
| Access apps | none cover this hostname.  `agents.jays.services` (`1e1a5fc4`) allows `mail@jays.services` and `jaywedgeworth22@gmail.com` through identity provider `76030d10-ec5b-46ff-ba9d-a4412cd755ce` (type One-time PIN), 24-hour session.  The new app allows `mail@jays.services` only (spec 3.3) |

What must change for the custom domain to bind:  the CNAME must go.  Cloudflare refuses a Worker custom domain on a hostname that already has a DNS record, and wrangler creates the domain's own record when it binds.  The tunnel rule does not block the bind, but once DNS stops pointing at the tunnel it is a dead rule aimed at a local dev port, and it comes back to life if anyone re-adds a CNAME.  Removing it (step 7) is a required step, done right after the deploy is verified.

**Do not wait for the deploy to close the exposure.**  If Jay wants it closed now, either stop the node process on port 8787 (tell the Congress.Trade owner first) or run step 4 early.  The hostname then does not resolve until step 5, which only affects a service nobody should be using.

## Step 0:  Preflight

```bash
sysctl vm.swapusage                      # this Mac is memory-tight;  wait if swap is nearly full
cd scripts/agent-sync-mcp
npm ci --ignore-scripts                  # lockfile resolved with --before=2026-09-25;  no install scripts (tested this way)
npx --no-install wrangler --version      # must print 4.139.0
node --test test/*.test.mjs              # pure suites
perl -e 'alarm 600; exec @ARGV' npm run test:workerd   # optional:  the Miniflare flow (about 300 MB)
python3 -I infra_phase0.py status        # read-only;  should match the table above, and names the process on the tunnel's local port
lsof -nP -iTCP:8787 -sTCP:LISTEN         # read-only;  who is publishing that port today
```

## Step 1:  Access app for the consent and admin paths

```bash
python3 -I infra_phase0.py access          # prints the payload, changes nothing
python3 -I infra_phase0.py access --apply
```

It creates the self-hosted app "agent-sync.jays.services consent and admin" with two destinations, `agent-sync.jays.services/authorize` and `agent-sync.jays.services/admin` (Access covers each path and everything under it), a 15-minute session (spec 3.3), app launcher hidden, the same identity provider list as `1e1a5fc4`, and one allow policy, "Jay emails only", for `mail@jays.services` and nobody else (spec 3.3).  The identity provider is One-time PIN, so every address on the policy is an admin credential for this server:  it can arm a seat, approve a consent, create Grok clients and revoke.  That list must stay equal to `OWNER_EMAILS` in `wrangler.jsonc` (a test pins both), or a login that passes Access fails the Worker's own check.  Adding a second address is an owner decision to record in the spec, not a config tweak.

If the API ignores the inline policy, the script warns and the app has no allow policy, which nobody can pass (fails closed).  Then add "Jay emails only" in the dashboard, or `POST /accounts/<id>/access/apps/<app id>/policies`.

Record the printed app id and `aud`.  The app can exist before the Worker does.

## Step 2:  KV namespace

```bash
python3 -I infra_phase0.py kv --apply      # creates agent-sync-mcp-OAUTH_KV, or reports the existing one
```

## Step 3:  Fill the two ids in wrangler.jsonc

```bash
python3 -I infra_phase0.py write-config    # shows the KV id and Access AUD it will write
python3 -I infra_phase0.py write-config --apply
git diff wrangler.jsonc                    # exactly two lines change
```

Neither value is a secret (the AUD rides in every Access JWT;  the KV id is an identifier).  Commit them in a follow-up PR so the next deploy is reproducible.  Until `ACCESS_AUD` holds a real 64-hex tag, the Worker answers 403 on `/authorize` and `/admin`.

## Step 4:  Remove the tunnel CNAME

```bash
python3 -I infra_phase0.py dns             # shows the record it would delete
python3 -I infra_phase0.py dns --apply     # backs it up to ~/.agent-sync-mcp-phase0/dns-<id>.json (mode 600), then deletes it
```

The step refuses anything but a `*.cfargotunnel.com` CNAME.  From here until step 5 finishes the hostname does not resolve.

**The backup is a record, never a rollback.**  Recreating this CNAME would publish the Mac's port 8787 again, with no Access.  To undo a bad deploy, roll the Worker back to its previous version (`npx --no-install wrangler rollback`, or redeploy the previous commit);  to take the endpoint down, use `MCP_DISABLED` or `wrangler delete` (see the table at the end).

## Step 5:  Deploy

```bash
cd scripts/agent-sync-mcp
(
  set +x
  export CLOUDFLARE_ACCOUNT_ID=3a9368057468d0909cafaa85df12d1b7
  CLOUDFLARE_EMAIL="$(grep -m1 '^CLOUDFLARE_JAY_ACCOUNT_EMAIL=' ~/.secrets/global-api-keys | cut -d= -f2- | tr -d '"')"
  CLOUDFLARE_API_KEY="$(grep -m1 '^CLOUDFLARE_JAY_API_KEY=' ~/.secrets/global-api-keys | cut -d= -f2- | tr -d '"')"
  export CLOUDFLARE_EMAIL CLOUDFLARE_API_KEY
  WRANGLER_SEND_METRICS=false perl -e 'alarm 600; exec @ARGV' npx --no-install wrangler deploy
)
```

`npx --no-install` runs the pinned devDependency, wrangler 4.139.0.  The deploy uploads `agent-sync-mcp`, creates the `SeatGate` SQLite class (migration `v1`), binds `OAUTH_KV` and `TOKEN_RATE_LIMITER`, and binds the custom domain `agent-sync.jays.services`, creating its DNS record.  No workers.dev or preview URL is created.  If wrangler says the hostname already has DNS records, step 4 did not run.  `npm run deploy` refuses on purpose.

## Step 6:  Post-deploy checks (read-only)

```bash
python3 -I infra_phase0.py check           # right after the deploy:  exactly one FAIL line is expected
```

At this point `check` ends with "1 failure(s)" and exit code 1, and that is correct:  the line `no stale tunnel ingress rule for this host` stays FAIL until step 7 removes the tunnel rule.  Any other FAIL means the deploy is wrong.  The rerun at the end of step 7 must say PASS on every line and exit 0.

What it checks, with the curl equivalent for a by-hand look:

| Check | Expect |
| --- | --- |
| `curl -s https://agent-sync.jays.services/.well-known/oauth-authorization-server` | 200;  `issuer` is `https://agent-sync.jays.services` with no trailing slash;  `authorization_endpoint` `/authorize`;  `token_endpoint` `/oauth/token`;  `code_challenge_methods_supported` `["S256"]`;  `authorization_response_iss_parameter_supported` true;  `client_id_metadata_document_supported` true;  no `registration_endpoint` |
| `curl -s https://agent-sync.jays.services/.well-known/oauth-protected-resource/mcp` | 200;  `resource` `https://agent-sync.jays.services/mcp`;  `authorization_servers[0]` byte-equal to `issuer` |
| `curl -si -X POST https://agent-sync.jays.services/mcp -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"check","version":"0"}}}'` | 401 with `WWW-Authenticate: Bearer resource_metadata="https://agent-sync.jays.services/.well-known/oauth-protected-resource/mcp"` |
| `curl -si https://agent-sync.jays.services/mcp` | 401 |
| `curl -si https://agent-sync.jays.services/authorize` and `/admin` | 302 to `https://silent-frost-37e0.cloudflareaccess.com/…` (Access, before the Worker) |
| `curl -si -X POST https://agent-sync.jays.services/admin/action -d action=arm` | stopped by Access (302 to the team domain, or an Access 401/403), never the Worker's own "Sign-In Required" page:  proves the `/admin` destination covers subpaths |
| `curl -s https://agent-sync.jays.services/health` | 200 `{"ok":true}` (the fleet admin panel's Agent Sync card probes this URL;  without it the card goes red) |
| `curl -si https://agent-sync.jays.services/` and `/oauth/register` and `/post` | 404 |
| workers.dev | `https://agent-sync-mcp.<account subdomain>.workers.dev` does not serve this Worker (the script looks up the subdomain) |
| custom domain | bound to service `agent-sync-mcp` |
| tunnel | no ingress rule left for this host (FAIL until step 7), and no `cfargotunnel.com` CNAME (passes once step 4 ran) |

Wrong Host:  the Worker answers 404 to any Host but `agent-sync.jays.services`.  At the edge no other hostname routes to it (custom domain only, workers.dev and previews off), so this cannot be probed from outside;  `npm run test:workerd` pins it by sending the loopback Host and a foreign URL and expecting 404.

Then Jay opens `https://agent-sync.jays.services/admin`, signs in through Access, and should see JET and GROK-WEB, both "Not armed", epoch 0, no grants.

## Step 7:  Drop the stale tunnel rule (required, right after step 6)

```bash
python3 -I infra_phase0.py tunnel          # shows 13 -> 12 rules and the rule it removes
python3 -I infra_phase0.py tunnel --apply  # backs up the whole config, then PUTs it without that rule
python3 -I infra_phase0.py check           # now every line, the tunnel ones included, must say PASS
```

This rewrites the whole configuration of a live tunnel that carries 12 other hostnames, so do it on its own and only with Jay's go-ahead.  The step refuses unless exactly one rule goes and the catch-all stays last.  It is required, not optional:  a rule that aims a public hostname at a local dev port is one re-added CNAME away from being published again.  Tell the Congress.Trade owner what was listening on port 8787.

## Step 8:  Hand over to Jay

Send Jay [ARMING-JAY.md](ARMING-JAY.md).  Phase 0 results get recorded in `docs/protocols/agent-sync-mcp.md` section 6.

**The first Approve is the proof of the Referrer-Policy fix.**  The unit tests pin the policy and the Fetch Standard's Origin rule, and the workerd flow derives the Origin from the page's policy, but nobody has driven a live browser.  Jay's first Arm click and first Approve click, in Safari or Chrome, are that test.  If either answers "Request Refused", read the refusal:  an `Origin: null` means a page lost its `Referrer-Policy: same-origin`, and the fix is the page header, never a looser Origin check.

**Watch items for the first connections**, all visible in Workers Logs and `/admin`:

- ChatGPT's first token request.  The library's metadata advertises `none`, `client_secret_basic` and `client_secret_post`, so ChatGPT's `private_key_jwt` option is not chosen.  If a token request still arrives with a `client_assertion` and no `client_id`, it fails with `invalid_client`;  the fix is the 1.2.2 library (eligible Tue, Oct 20), which needs Jay's age-rule approval only if it must go sooner.
- Whether any client sends a browser `Origin` to `/mcp` (logged as `mcp_origin_refused`).
- That the Workers Logs rows carry no `cf-access-jwt-assertion` or cookie (`invocation_logs` is off, so only our own lines should appear).
- The rate limiter namespace id `4711` in `wrangler.jsonc` must be unique among the account's rate limiters.  A read-only scan on Fri, Oct 9 found 7 Workers on Usage.Jays.Services and none with a rate limiter binding;  rerun it if other Workers deployed since (`GET /accounts/<id>/workers/scripts/<name>/settings`, look for bindings of type `ratelimit`).

## Rollback and kill switch

| Need | Do |
| --- | --- |
| Stop one seat now | `/admin` → Pause (tools say paused, refreshes fail, grants kept) |
| Kill a seat's grants | `/admin` → Revoke All And Bump Epoch |
| Stop the whole endpoint | set `"MCP_DISABLED": "1"` in `wrangler.jsonc` and redeploy (step 5):  `/mcp` answers 503 |
| Remove it | `npx --no-install wrangler delete` with the step 5 environment (removes the Worker; check that the custom domain went with it, and delete it in the dashboard if not).  Never restore the old tunnel CNAME |
| Undo a bad deploy | `npx --no-install wrangler rollback` (previous Worker version) with the step 5 environment |

Before Phase 2 puts any Zulip key in this Worker, spec 3.2 requires a per-Worker deploy token minted by Jay (A5), and every Phase 0 grant revoked with every epoch bumped (ARMING-JAY.md, exit criteria).
