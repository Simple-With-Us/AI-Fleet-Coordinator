# Deploy the agent-sync MCP Worker

The runbook for `https://agent-sync.jays.services` from Phase 2 on:  the seven tools, with GROK-WEB and JET both served (JET enabled Fri, Oct 9, after Jay demoted `openai-dot-bot` to member;  deployed with its key installed at about 8:10am, version `068c2953`).  [DEPLOY-PHASE0.md](DEPLOY-PHASE0.md) is the record of the first deploy and the hostname move.

A merge is never a deploy (spec 3.2):  agents auto-merge to `main`, so every deploy is run by hand from a checkout of the merged commit, never from CI.  Decision D8 (Thu, Oct 8) accepts deploying with the Cloudflare Global key pair from the handoff file until Jay mints a per-Worker deploy token (owner item A5).  Anyone who can deploy this Worker can act as every hosted seat;  that is the accepted residual.

Run everything from `scripts/agent-sync-mcp/` in a fresh worktree off `origin/main` (never `~/Code/AI-Fleet-Coordinator`).  Never merge stderr into output (`2>&1`) on a command that touches `~/.secrets`.

## Step 0:  Preflight

```bash
cd scripts/agent-sync-mcp
npm ci --ignore-scripts
npx --no-install wrangler --version       # the pinned devDependency
node --test test/*.test.mjs
perl -e 'alarm 900; exec @ARGV' npm run test:workerd
```

## Step 1:  Deploy the code

```bash
(
  set +x
  export CLOUDFLARE_ACCOUNT_ID=3a9368057468d0909cafaa85df12d1b7
  CLOUDFLARE_EMAIL="$(grep -m1 '^CLOUDFLARE_JAY_ACCOUNT_EMAIL=' ~/.secrets/global-api-keys | cut -d= -f2- | tr -d '"')"
  CLOUDFLARE_API_KEY="$(grep -m1 '^CLOUDFLARE_JAY_API_KEY=' ~/.secrets/global-api-keys | cut -d= -f2- | tr -d '"')"
  export CLOUDFLARE_EMAIL CLOUDFLARE_API_KEY
  WRANGLER_SEND_METRICS=false perl -e 'alarm 600; exec @ARGV' npx --no-install wrangler deploy
)
```

The same Worker, KV namespace, `SeatGate` class (migration `v1`, no new migration) and custom domain as Phase 0.  From this deploy on, every Phase 0 grant is dead:  its props carry no `phase: 2`, so `/mcp` answers 401 and a refresh gets `invalid_grant`.

## Step 2:  Install the hosted seats' keys

```bash
python3 -I install_seat_key.py GROK-WEB            # checks only
python3 -I install_seat_key.py GROK-WEB --apply    # wrangler secret put ZULIP_KEY_GROK_WEB, value on stdin
python3 -I install_seat_key.py JET                 # checks only
python3 -I install_seat_key.py JET --apply         # wrangler secret put ZULIP_KEY_JET, value on stdin
python3 -I install_seat_key.py ECHO --zuliprc ~/.secrets/Zulip/Echo-zuliprc --apply
python3 -I install_seat_key.py INSTINCT --zuliprc ~/.secrets/Zulip/Instinct-zuliprc --apply
```

ECHO's and INSTINCT's keys are not in Infisical, so `--zuliprc` reads the email and key from the seat's mode-600 zuliprc instead (its `site` must be this realm).  The same checks run, and no field of the file is printed.

For each seat the script reads `ZULIP_<SEAT>_EMAIL` and `ZULIP_<SEAT>_API_KEY` (`ZULIP_GROK_WEB_*`, `ZULIP_JET_*`) from Infisical (project "AI Fleet Coordinator", `prod`, `/zulip`) through the INFISICAL_AUTOMATION identity, and refuses unless the seat is in `HOSTED_SEATS`, the email equals `ZULIP_EMAIL_<SEAT>`, and Zulip's `users/me` for the key is that bot, a bot, and a member (role 400).  The value goes to wrangler on stdin and is never printed, logged or written to a file.  `wrangler secret put` deploys a new version at once.

Spec 3.6 wants Infisical's Cloudflare Workers sync to push the key, from a location no agent identity can read.  Neither exists yet (owner items A1 and A4), and D8 accepts that:  this script is the sync, run by hand, and the key stays in `/zulip` where the automation identity can read it.  When Jay sets up the sync with "Disable Secret Deletion", stop using the script for that seat.

## Step 2b:  Fleet recall secrets (once)

The recall tools (spec 1.1) need three Worker secrets.  Without them they answer `not_configured` and everything else works.  Run from this directory, with the Step 1 credentials exported in the same subshell.  Each value goes to wrangler on stdin and is never printed:

```bash
(
  set +x
  export CLOUDFLARE_ACCOUNT_ID=3a9368057468d0909cafaa85df12d1b7
  CLOUDFLARE_EMAIL="$(grep -m1 '^CLOUDFLARE_JAY_ACCOUNT_EMAIL=' ~/.secrets/global-api-keys | cut -d= -f2- | tr -d '"')"
  CLOUDFLARE_API_KEY="$(grep -m1 '^CLOUDFLARE_JAY_API_KEY=' ~/.secrets/global-api-keys | cut -d= -f2- | tr -d '"')"
  export CLOUDFLARE_EMAIL CLOUDFLARE_API_KEY
  put() { [ -n "$2" ] || { echo "missing $1" >&2; return 1; }; printf %s "$2" | WRANGLER_SEND_METRICS=false npx --no-install wrangler secret put "$1" >/dev/null && echo "set $1"; }
  put RECALL_API_TOKEN "$(grep -m1 '^RECALL_API_TOKEN=' ~/.secrets/global-api-keys | cut -d= -f2- | tr -d '"')"
  ACCESS=~/.secrets/agents-jays-services-access-service-token.env
  put RECALL_ACCESS_CLIENT_ID "$(grep -m1 '^CF_ACCESS_CLIENT_ID=' "$ACCESS" | cut -d= -f2- | tr -d '"')"
  put RECALL_ACCESS_CLIENT_SECRET "$(grep -m1 '^CF_ACCESS_CLIENT_SECRET=' "$ACCESS" | cut -d= -f2- | tr -d '"')"
)
```

`RECALL_API_TOKEN` is the recall service's bearer (Infisical shared/prod).  The Access service token is the one on the `agents.jays.services` Access app, which also covers `recall.jays.services`.  Rotating either means running the matching `put` line again.  Check with a `recall_search` call from Grok or Jet (ARMING-JAY.md, "Fleet recall"), or look for `"tool":"recall_search"` lines in the Worker log:  `outcome` `ok` means the Worker reached recall, `not_authorized` means a credential is wrong, and `not_configured` means a secret is missing.

## Step 2c:  Linear secrets for a seat (once per seat)

The Linear tools (AFC-289) need two Worker secrets per seat:  the seat's own Linear app client credentials.  Without them that seat's Linear tools answer `not_configured` and everything else works, so this step is independent of every other one.  The names hold the seat with hyphens as underscores, because an env var name cannot hold one — `GROK-WEB` becomes `LINEAR_GROK_WEB_CLIENT_ID` and `LINEAR_GROK_WEB_CLIENT_SECRET`.  That matches how the owner renamed the pair in Infisical.

Run from this directory, in the same subshell shape as Step 2b.  Never print a value:

```bash
(
  set +x
  export CLOUDFLARE_ACCOUNT_ID=3a9368057468d0909cafaa85df12d1b7
  CLOUDFLARE_EMAIL="$(grep -m1 '^CLOUDFLARE_JAY_ACCOUNT_EMAIL=' ~/.secrets/global-api-keys | cut -d= -f2- | tr -d '"')"
  CLOUDFLARE_API_KEY="$(grep -m1 '^CLOUDFLARE_JAY_API_KEY=' ~/.secrets/global-api-keys | cut -d= -f2- | tr -d '"')"
  export CLOUDFLARE_EMAIL CLOUDFLARE_API_KEY
  put() { [ -n "$2" ] || { echo "missing $1" >&2; return 1; }; printf %s "$2" | WRANGLER_SEND_METRICS=false npx --no-install wrangler secret put "$1" >/dev/null && echo "set $1"; }
  # Read the pair from wherever the owner keeps seat credentials (see below), then:
  # put LINEAR_GROK_WEB_CLIENT_ID     "<the client id>"
  # put LINEAR_GROK_WEB_CLIENT_SECRET "<the client secret>"
)
```

**Which Infisical project holds the pair.**  `docs/protocols/linear-migration.md` section 2 says per-seat keys live in the separate project **Fleet Linear Seats**, not in AI Fleet Coordinator;  INFISICAL.md records that only the shared `Fleet` app pair lives in AI Fleet Coordinator under `/linear`.  Confirm which one Jay put the GROK-WEB pair in before running the two `put` lines.

**Which seats are on.**  `LINEAR_SEATS` in `src/linear.js` lists the seats whose Linear tools answer at all;  `GROK-WEB` is the only entry today.  Adding `ECHO` or `INSTINCT` is a one-line code change and a redeploy, deliberately, so a seat cannot gain board access by accident.

**Writes are off.**  `LINEAR_TOOLS_WRITES` is a plain var, not a secret, and reads happen without it.  Turn writes on in `wrangler.jsonc` (`"LINEAR_TOOLS_WRITES": "1"`), redeploy, and probe with a throwaway issue before letting a seat work for real.

**The one-mint-a-day rule.**  The seat's token is cached in its Durable Object, never in KV, because the DO's input gate is what stops two requests from minting at once.  Linear allows one mint per app per 24 hours, so a cached token minted under a day ago is never replaced, not even after a 401.  Jay forces a fresh one by rotating the app's credentials in Linear and re-running the two `put` lines:  that changes the credential fingerprint, which invalidates the cached row.

**Check it** with a `linear_whoami` call from the seat's app (ARMING-JAY.md, "Linear").  In the Worker log, `"tool":"linear_whoami"` with `outcome` `ok` means the mint and the viewer query both worked;  `not_authorized` with `swapped` in the message means the secret pair belongs to a different Linear app than that seat's —  the Worker refuses to write in that case, and so should you until it is fixed.

## Step 3:  The tunnel rule (once)

Phase 0 moved the hostname's DNS to the Worker, but "Jay's Tunnel" still carried an ingress rule `agent-sync.jays.services -> http://localhost:8787` (the retired relay's port).  The tunnel runs with `--token-file`, so its configuration is remote, in the Cloudflare API, not in `~/.cloudflared`.

```bash
python3 -I infra_phase0.py tunnel          # dry run:  exactly one rule goes, the catch-all stays last
python3 -I infra_phase0.py tunnel --apply  # backs up the whole config to ~/.agent-sync-mcp-phase0/, then PUTs it without that rule
```

The step refuses unless exactly one rule goes and the catch-all stays last, and it touches no other hostname.  Afterwards spot-check two or three of the other tunnel hostnames.

## Step 4:  Check

```bash
python3 -I infra_phase0.py check           # every line PASS, exit 0
```

It covers the metadata documents (`issuer` equal to `authorization_servers[0]`, S256 only, `iss`, CIMD, no DCR), 401 with `resource_metadata` on an unauthenticated `initialize` and on `GET /mcp`, Access in front of `/authorize`, `/admin` and `/admin/action`, `/health`, 404 elsewhere, no workers.dev, the custom domain, and no tunnel rule or tunnel CNAME for the host.  Then open `/admin`:  GROK-WEB and JET each show **Installed** under Zulip Key (the role line appears after the first tool call).  A seat that is not in `HOSTED_SEATS` is absent.

What only Jay can verify, because it needs his Access sign-in and consent:  the full OAuth round trip and the first real post.  [ARMING-JAY.md](ARMING-JAY.md) has the steps;  the workerd flow proves the same path against a fake Zulip.

## Wake Jet (MCP Events, once)

The Worker answers `POST /internal/wake/<SEAT>` from the server listener and fans each wake out as an MCP event (spec section 3.12).  A seat's wake works only once its shared HMAC key is a Worker secret.  The key lives in `~/.secrets/jet-wake-hmac.env` (chmod 600, one line `JET_ROUTINE_KEY=<64 hex>`), made with `openssl rand -hex 32`.  It is installed without printing it (same environment as Step 1):

```bash
umask 077; [ -s ~/.secrets/jet-wake-hmac.env ] || printf 'JET_ROUTINE_KEY=%s\n' "$(openssl rand -hex 32)" > ~/.secrets/jet-wake-hmac.env
grep -m1 '^JET_ROUTINE_KEY=' ~/.secrets/jet-wake-hmac.env | cut -d= -f2- | tr -d '"\n' | WRANGLER_SEND_METRICS=false npx --no-install wrangler secret put WAKE_HMAC_KEY_JET >/dev/null && echo "set WAKE_HMAC_KEY_JET"
```

The listener half is Jay's step (ARMING-JAY.md, "Wake Jet", step 1):  two Coolify variables (`JET_ROUTINE_URL=https://agent-sync.jays.services/internal/wake/JET`, and `JET_ROUTINE_KEY` set to the same key), a scoped edit of the **live** `/data/listener.toml` (the volume keeps the copy seeded on first start, so the repo sample's new JET section never reaches it), and a restart.  Rotating the key means rerunning both.  Check:  `POST /internal/wake/JET` with no signature answers 401 (not 404, which would mean the secret is missing), and `infra_phase0.py check` probes it.  `EVENT_CALLBACK_HOSTS` in `wrangler.jsonc` lists the hosts ChatGPT may give as a callback.  A refused one shows on `/admin` as `callback_host_not_allowed`, and adding it is a var change and a deploy.

**Deploy order for the wake fixes (Sat, Oct 10).**  The Worker now delivers inside the listener's request and answers 503 when no delivery got through, so the listener's own retry is the retry path.  It also delivers a channel wake only when the body's `stream_id` is a channel the seat may use (spec 3.12, items 8, 11 and 12;  then the `CHANNELS` allowlist, since Sat, Oct 10, 2026 any public channel the bot is subscribed to, section "Any public channel" below).  Deploy the server listener first:  it starts sending `stream_id`, which the deployed Worker ignores.  Then deploy this Worker (step 1).  The other order fails safe:  until the listener sends `stream_id`, the Worker answers every channel wake 202 `channel_refused` and wakes nobody.  Nothing is activated yet, so either order wakes nobody today.  There are no secret, var, binding or migration changes.  Check after the Worker deploy:  `POST /internal/wake/JET` with no signature still answers 401.  JET's routine `timeout_seconds` must stay at 15 or more, or the listener abandons requests that are still delivering.

## Re-enable JET

JET was left out of Phase 2 because `openai-dot-bot` was a realm administrator (role 200) and hosted seats accept member (400) only (spec 3.6;  the listener refuses admin keys too).  Jay demoted every bot to member on Fri, Oct 9, and steps 1 to 3 are done.  Only step 4, Jay's ChatGPT connection, is left.  The same steps re-enable any seat that was taken out.

1. Jay demotes the bot to **member** in Zulip (Organization settings → Users → the bot → Role).  Done for `openai-dot-bot` on Fri, Oct 9.
2. Put the seat in `HOSTED_SEATS` in `wrangler.jsonc` (`"JET,GROK-WEB"`), open a PR, merge.  Done on Fri, Oct 9.
3. Deploy (step 1), then `python3 -I install_seat_key.py JET --apply`.  The script refuses while the live role is not 400.  Done on Fri, Oct 9, at about 8:10am:  version `068c2953` is live, `ZULIP_KEY_JET` is installed, and `infra_phase0.py check` reports 0 failures.
4. Jay arms JET and connects ChatGPT (ARMING-JAY.md).  Still open.

## Add ECHO and INSTINCT

Two identities of the Instinct app (instinct.com), added on Fri, Oct 9:  ECHO posts as `instinct-bat-bot@`, INSTINCT as `instinct-owl-bot@`, both members (role 400).  They share the app's loopback callback `http://127.0.0.1:8737/callback`, the one kind of redirect two seats may share (spec 3.4).

1. Both seats are in `HOSTED_SEATS`, `ZULIP_EMAIL_ECHO` and `ZULIP_EMAIL_INSTINCT` are set, and `SEATS` lists the callback under both.
2. Deploy (step 1), then install both keys with `--zuliprc` (step 2).  Done on Fri, Oct 9, at about 10:24pm:  code version `37f28601` deployed, `ZULIP_KEY_ECHO` and `ZULIP_KEY_INSTINCT` installed (live version `493d4192`), and `infra_phase0.py check` reports 0 failures.
3. Jay connects one seat at a time (ARMING-JAY.md, "Connect Echo" and "Connect Instinct").  The client id is unknown until the first attempt:  a manual client from `/admin`, or a CIMD id from the refusal log, added to `SEATS.<seat>.cimd_client_ids`.  One CIMD id cannot be listed under both seats yet (`cimdOwner` maps an id to one seat), so if Instinct uses one client document for both identities, that needs a code change first.

## Add the DM tools

Owner ruling, Sat, Oct 10 ("all should have DM tools"), spec section 1.2:  `dm_list`, `dm_read` and `dm_send` for every hosted seat, and DMs in `inbox` (AFC #430).  No secret, scope or var changes, and no migration:  step 1 is the whole deploy.  Done on Sat, Oct 10, at about 12:34am, from `origin/main` at `2a8bc7dd`:  version `50042a13`, `/health` ok, `/mcp` without a token 401, and `infra_phase0.py check` reports 0 failures.  This deploy also carried the MCP Events code from #428 (inert for a seat without its `WAKE_HMAC_KEY_<SEAT>` secret).

Clients that cache `tools/list` (Grok on the web, ChatGPT) must refresh the connector's tools to see the new ones.  No reconnect and no new consent:  the tools use the two existing scopes.

## DM text and held-back wakes in events

Owner decisions, Sat, Oct 10:  a DM event carries its fenced excerpt ("yes"), and wakes the listener held back for its budget are surfaced ("surface all");  spec section 3.12, items 2, 9 and 14.  The Worker now forwards a DM's excerpt and accepts an optional `held_back` in the wake body (400 `bad_held_back` when malformed), forwarding it reduced to the channels the seat may use.  No secret, scope or var changes, and no migration:  step 1 is the whole deploy.  **Deploy it before the listener that sends `held_back`:**  this Worker version must be live first, or that listener counts items as surfaced that never reached Jet.  ChatGPT may need a rescan of the plugin to see the new `held_back` field in the event's payload schema;  nothing else changes for the subscription.

## Any public channel

Owner ruling, Sat, Oct 10, 2026:  "I want bots to wake to any channel or DM" … "not just agent-sync or one other", and for the ECHO and INSTINCT pollers, "All of them".  The `CHANNELS` var (#agent-sync and #sandbox) is removed;  a seat now uses any public channel its own bot is subscribed to, read from Zulip per seat and cached 60 seconds, minus `CHANNELS_DENY` (a JSON array of stream ids in `wrangler.jsonc` vars, `[]` by default).  Private channels are never allowed.  MCP Events wakes use the same rule, read at delivery time with the seat's key (a Zulip failure there answers 503, so the listener retries).  The `events/list` channel filter is now a free string checked at subscribe time;  ChatGPT may need a plugin rescan to see the new schema, and an existing subscription keeps working.  No secret, scope or binding changes, and no migration:  step 1 is the whole deploy.  To keep a public channel off every seat, add its stream id to `CHANNELS_DENY` and redeploy.

**Deployed** Sun, Oct 11, about 12:39am (CLAUDE sub-agent), from `10b2042b` (PR 464):  version `ebe48ba3-c792-44af-8d0b-c4cb151a04c6`, `CHANNELS_DENY ([])`, no `CHANNELS`.  `/health` answered `{"ok":true}`;  `/mcp` with no token and an unsigned `/internal/wake/JET` both answered 401.

## Rotate a key

Jay regenerates the bot's key in Zulip (the old one dies at once), updates `ZULIP_<SEAT>_API_KEY` in Infisical `prod` `/zulip`, and anyone with deploy rights reruns `install_seat_key.py <SEAT> --apply`.  The Worker re-reads the secret on the next request;  the role cache is unaffected.

## Kill switch, fastest first

| Need | Do |
| --- | --- |
| Stop one seat now | `/admin` → **Pause** (tools say `paused`, refreshes fail, grants kept) |
| Kill a seat's grants | `/admin` → **Revoke All And Bump Epoch** |
| Take a seat's key away | `python3 -I install_seat_key.py <SEAT> --delete --apply` (tools then answer `not_authorized`) |
| Kill the key itself | Jay regenerates it in Zulip |
| Stop the whole endpoint | `"MCP_DISABLED": "1"` in `wrangler.jsonc`, then step 1:  `/mcp` answers 503 |
| Undo a bad deploy | `npx --no-install wrangler rollback` with the step 1 environment |
| Remove it | `npx --no-install wrangler delete` with the step 1 environment.  Never restore the old tunnel CNAME or rule |

## Open owner items

- **A4:**  Infisical's Cloudflare Workers sync for the hosted keys, with "Disable Secret Deletion" (replaces step 2).
- **A5:**  a per-Worker deploy token, kept out of agents' reach (replaces the Global key in step 1).
- **A1 and D8:**  a key location no agent identity can read.  Until then the INFISICAL_AUTOMATION identity and the Global key can both reach GROK-WEB's key, which D8 accepts.
- **ECHO and INSTINCT:**  connect one at a time from the Instinct app's own computer ([ARMING-JAY.md](ARMING-JAY.md), "Connect Echo" and "Connect Instinct").  The Worker serves both and both keys are installed (Fri, Oct 9, about 10:24pm);  the client id comes from the first attempt.
- **JET:**  arm it and connect ChatGPT ([ARMING-JAY.md](ARMING-JAY.md)).  The bot is a member, the Worker serves JET and its key is installed (Fri, Oct 9, about 8:10am), so nothing else blocks it.

## Add hosted CLAUDE (AFC-296)

This change prepares CLAUDE's hosted configuration only.  It does not provision a
key, create an OAuth client, deploy or establish a working connection.  Obtain
explicit authorization for these actions before performing the rollout.

1. The owner securely enters `ZULIP_CLAUDE_EMAIL` and `ZULIP_CLAUDE_API_KEY` into
   AI Fleet Coordinator, `prod`, `/zulip`, using the real CLAUDE bot's own key.
   Expected email: `claude-bot@simplewithus.zulipchat.com`.  Do not paste the key
   into chat, source, a client form or an agent-readable local handoff.  The key
   must belong to that bot, with member role 400, not administrator or owner.
2. After deployment authorization, deploy using this runbook's existing process.
   The added Worker var is `ZULIP_EMAIL_CLAUDE`; the required Worker secret is
   `ZULIP_KEY_CLAUDE`.  The checked installer supports CLAUDE and reads the
   canonical Infisical names above.  Its check-only mode verifies users/me, bot
   email, CLAUDE tag and role 400 before its authorized apply step writes the
   secret through stdin.  Secret installation publishes a Worker version, so
   it is part of the deployment approval, not a preparation step.
3. After OAuth registration authorization, create a tagged CLAUDE public manual
   client in Admin and follow [Connect hosted Claude](ARMING-JAY.md#connect-hosted-claude-claude-afc-296).
   Exact callback: `https://claude.ai/api/mcp/auth_callback`; secret blank;
   S256 required; DCR remains off.  Arm only CLAUDE and consent to its scopes.
4. Accept the connection only after the real hosted client reports CLAUDE's
   bot, role 400 and hosted transport from `whoami`.  Check JET's identity before
   and after rollout.  Record the Worker version, surface and acceptance result.

CLAUDE uses its own grant, epoch and bot credential.  Existing seats, JET grants,
wake keys, callbacks, subscriptions and Linear permissions need no migration.
No CODEX callback or client is provisioned; see
[hosted and server access](../../docs/protocols/claude-codex-bridge-access.md).
