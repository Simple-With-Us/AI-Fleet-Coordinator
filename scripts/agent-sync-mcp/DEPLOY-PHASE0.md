# Deploy Phase 0:  Runbook

Ordered steps to put the Phase 0 stub on `https://agent-sync.jays.services`.  The stub holds no Zulip key, so spec 3.2 allows today's credential (the Global key) for it.  Every Cloudflare change below is Jay-approved work for the parent session, run by hand, never from CI and never on merge.

Everything runs from `scripts/agent-sync-mcp/` in a checkout of the merged commit (a fresh worktree off `origin/main`, never `~/Code/AI-Fleet-Coordinator`).  `infra_phase0.py` loads the Global key inside its own process from `~/.secrets/global-api-keys` and never prints it.  Every step is a dry run until `--apply`.  Do not merge stderr into output (`2>&1`) on any command that touches `~/.secrets`.

## What holds the hostname today (read-only probe, Fri, Oct 9)

| Thing | State |
| --- | --- |
| DNS in zone `jays.services` (`f04fde898ac72c5087b8579844c2d474`, account Usage.Jays.Services) | `CNAME agent-sync.jays.services -> 6fa2a97c-b4f8-420d-94ae-bd9858aff4b6.cfargotunnel.com`, proxied, record id `d3c670eb8be9fab6578019ee3bf1ea5d`, comment "Repoint to healthy Mac tunnel Jay's Tunnel" |
| Cloudflare Tunnel "Jay's Tunnel" (`6fa2a97c-…`, healthy, 13 ingress rules) | rule `agent-sync.jays.services -> http://localhost:8787` (the retired `agent-sync-push` service).  Something on the Mac still answers on that port:  `GET /` is 404 and `GET /post` is 405 |
| Worker routes and Worker custom domains | none for this hostname |
| Access apps | none cover this hostname.  `agents.jays.services` (`1e1a5fc4`) allows `mail@jays.services` and `jaywedgeworth22@gmail.com` through identity provider `76030d10-ec5b-46ff-ba9d-a4412cd755ce`, 24-hour session |

What must change for the custom domain to bind:  the CNAME must go.  Cloudflare refuses a Worker custom domain on a hostname that already has a DNS record, and wrangler creates the domain's own record when it binds.  The tunnel rule does not block the bind;  once DNS stops pointing at the tunnel it is dead config, and removing it (step 8) is optional cleanup.

## Step 0:  Preflight

```bash
sysctl vm.swapusage                      # this Mac is memory-tight;  wait if swap is nearly full
cd scripts/agent-sync-mcp
npm ci --ignore-scripts                  # lockfile resolved with --before=2026-09-25;  no install scripts (tested this way)
npx --no-install wrangler --version      # must print 4.139.0
node --test test/*.test.mjs              # pure suites
perl -e 'alarm 600; exec @ARGV' npm run test:workerd   # optional:  the Miniflare flow (about 300 MB)
python3 -I infra_phase0.py status        # read-only;  should match the table above
```

## Step 1:  Access app for the consent and admin paths

```bash
python3 -I infra_phase0.py access          # prints the payload, changes nothing
python3 -I infra_phase0.py access --apply
```

It creates the self-hosted app "agent-sync.jays.services consent and admin" with two destinations, `agent-sync.jays.services/authorize` and `agent-sync.jays.services/admin` (Access covers each path and everything under it), a 15-minute session (spec 3.3), app launcher hidden, the same identity provider list as `1e1a5fc4`, and one allow policy, "Jay emails only", for `mail@jays.services` and `jaywedgeworth22@gmail.com`.  That email list must stay equal to `OWNER_EMAILS` in `wrangler.jsonc` (a test pins it), or a login that passes Access fails the Worker's own check.

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

The step refuses anything but a `*.cfargotunnel.com` CNAME.  From here until step 5 finishes the hostname does not resolve, which only affects the retired service.  Rollback:  recreate the CNAME from the backup file in the dashboard.

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
python3 -I infra_phase0.py check           # every line must say PASS
```

What it checks, with the curl equivalent for a by-hand look:

| Check | Expect |
| --- | --- |
| `curl -s https://agent-sync.jays.services/.well-known/oauth-authorization-server` | 200;  `issuer` is `https://agent-sync.jays.services` with no trailing slash;  `authorization_endpoint` `/authorize`;  `token_endpoint` `/oauth/token`;  `code_challenge_methods_supported` `["S256"]`;  `authorization_response_iss_parameter_supported` true;  `client_id_metadata_document_supported` true;  no `registration_endpoint` |
| `curl -s https://agent-sync.jays.services/.well-known/oauth-protected-resource/mcp` | 200;  `resource` `https://agent-sync.jays.services/mcp`;  `authorization_servers[0]` byte-equal to `issuer` |
| `curl -si -X POST https://agent-sync.jays.services/mcp -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"check","version":"0"}}}'` | 401 with `WWW-Authenticate: Bearer resource_metadata="https://agent-sync.jays.services/.well-known/oauth-protected-resource/mcp"` |
| `curl -si https://agent-sync.jays.services/mcp` | 401 |
| `curl -si https://agent-sync.jays.services/authorize` and `/admin` | 302 to `https://silent-frost-37e0.cloudflareaccess.com/…` (Access, before the Worker) |
| `curl -si -X POST https://agent-sync.jays.services/admin/action -d action=arm` | stopped by Access (302 to the team domain, or an Access 401/403), never the Worker's own "Sign-In Required" page:  proves the `/admin` destination covers subpaths |
| `curl -si https://agent-sync.jays.services/` and `/oauth/register` and `/post` | 404 |
| workers.dev | `https://agent-sync-mcp.<account subdomain>.workers.dev` does not serve this Worker (the script looks up the subdomain) |
| custom domain | bound to service `agent-sync-mcp` |

Wrong Host:  the Worker answers 404 to any Host but `agent-sync.jays.services`.  At the edge no other hostname routes to it (custom domain only, workers.dev and previews off), so this cannot be probed from outside;  `npm run test:workerd` pins it by sending the loopback Host and a foreign URL and expecting 404.

Then Jay opens `https://agent-sync.jays.services/admin`, signs in through Access, and should see JET and GROK-WEB, both "Not armed", epoch 0, no grants.

## Step 7:  Hand over to Jay

Send Jay [ARMING-JAY.md](ARMING-JAY.md).  Phase 0 results get recorded in `docs/protocols/agent-sync-mcp.md` section 6.

## Step 8 (optional, later):  Drop the stale tunnel rule

```bash
python3 -I infra_phase0.py tunnel          # shows 13 -> 12 rules and the rule it removes
python3 -I infra_phase0.py tunnel --apply  # backs up the whole config, then PUTs it without that rule
```

This rewrites the whole configuration of a live tunnel that carries 12 other hostnames, so do it on its own, after the deploy is verified, and only with Jay's go-ahead.  The step refuses unless exactly one rule goes and the catch-all stays last.  Also find what still listens on the Mac's port 8787, read-only:  `lsof -nP -iTCP:8787 -sTCP:LISTEN`.

## Rollback and kill switch

| Need | Do |
| --- | --- |
| Stop one seat now | `/admin` → Pause (tools say paused, refreshes fail, grants kept) |
| Kill a seat's grants | `/admin` → Revoke All And Bump Epoch |
| Stop the whole endpoint | set `"MCP_DISABLED": "1"` in `wrangler.jsonc` and redeploy (step 5):  `/mcp` answers 503 |
| Remove it | `npx --no-install wrangler delete` with the step 5 environment (removes the Worker and its custom domain) |

Before Phase 2 puts any Zulip key in this Worker, spec 3.2 requires a per-Worker deploy token minted by Jay (A5), and every Phase 0 grant revoked with every epoch bumped (ARMING-JAY.md, exit criteria).
