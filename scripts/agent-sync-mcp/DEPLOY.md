# Deploy the agent-sync MCP Worker

The runbook for `https://agent-sync.jays.services` from Phase 2 on:  the seven tools, with GROK-WEB and JET both served (JET enabled Fri, Oct 9, after Jay demoted `openai-dot-bot` to member;  deployed with its key installed at about 8:10am, version `068c2953`).  [DEPLOY-PHASE0.md](DEPLOY-PHASE0.md) is the record of the first deploy and the hostname move.

A merge is never a deploy (spec 3.2):  agents auto-merge to `main`, so every deploy is run by hand from a checkout of the merged commit, never from CI.  Decision D8 (Thu, Oct 8) accepts deploying with the Cloudflare Global key pair from the handoff file until Jay mints a per-Worker deploy token (owner item A5).  Anyone who can deploy this Worker can act as every hosted seat;  that is the accepted residual.

Run everything from `scripts/agent-sync-mcp/` in a fresh worktree off `origin/main` (never `~/Code/AI-Fleet-Coordinator`).  Never merge stderr into output (`2>&1`) on a command that touches a credential.

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
  CLOUDFLARE_EMAIL="$(fleet-secrets get CLOUDFLARE_JAY_ACCOUNT_EMAIL)"
  CLOUDFLARE_API_KEY="$(fleet-secrets get CLOUDFLARE_JAY_API_KEY)"
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
python3 -I install_seat_key.py ECHO --apply        # ZULIP_KEY_ECHO
python3 -I install_seat_key.py WISE --apply        # ZULIP_KEY_WISE, from Infisical's ZULIP_WISE pair
```

Every seat's key comes from Infisical;  `--zuliprc` is gone (AFC-347).  WISE's Worker names are `ZULIP_KEY_WISE` and `ZULIP_EMAIL_WISE` since AFC-347.  Before that the Worker read `ZULIP_KEY_INSTINCT`, installed from Infisical's old `ZULIP_INSTINCT` pair, which Zulip now refuses, so every WISE call answered `not_authorized`.  Delete the old secret once WISE is verified (`wrangler secret delete ZULIP_KEY_INSTINCT`, with Step 1's credentials).

For each seat the script reads `ZULIP_<SEAT>_EMAIL` and `ZULIP_<SEAT>_API_KEY` (`ZULIP_GROK_WEB_*`, `ZULIP_JET_*`, `ZULIP_CLAUDE_*`) from Infisical (project "AI Fleet Coordinator", `prod`, `/zulip`) through the INFISICAL_AUTOMATION identity, and refuses unless the seat is in `HOSTED_SEATS`, the email equals the seat's configured Worker email binding (`ZULIP_EMAIL_<SEAT>` for every seat except CLAUDE, which uses the canonical `ZULIP_CLAUDE_EMAIL`), and Zulip's `users/me` for the key is that bot, a bot, and a member (role 400).  The value goes to wrangler on stdin and is never printed, logged or written to a file.  `wrangler secret put` deploys a new version at once.

Spec 3.6 wants Infisical's Cloudflare Workers sync to push the key, from a location no agent identity can read.  Neither exists yet (owner items A1 and A4), and D8 accepts that:  this script is the sync, run by hand, and the key stays in `/zulip` where the automation identity can read it.  When Jay sets up the sync with "Disable Secret Deletion", stop using the script for that seat.

## Step 2b:  Fleet recall secrets (once)

The recall tools (spec 1.1) need three Worker secrets.  Without them they answer `not_configured` and everything else works.  Run from this directory, with the Step 1 credentials exported in the same subshell.  Each value goes to wrangler on stdin and is never printed:

```bash
(
  set +x
  export CLOUDFLARE_ACCOUNT_ID=3a9368057468d0909cafaa85df12d1b7
  CLOUDFLARE_EMAIL="$(fleet-secrets get CLOUDFLARE_JAY_ACCOUNT_EMAIL)"
  CLOUDFLARE_API_KEY="$(fleet-secrets get CLOUDFLARE_JAY_API_KEY)"
  export CLOUDFLARE_EMAIL CLOUDFLARE_API_KEY
  put() { [ -n "$2" ] || { echo "missing $1" >&2; return 1; }; printf %s "$2" | WRANGLER_SEND_METRICS=false npx --no-install wrangler secret put "$1" >/dev/null && echo "set $1"; }
  put RECALL_API_TOKEN "$(fleet-secrets get RECALL_API_TOKEN)"
  put RECALL_ACCESS_CLIENT_ID "$(fleet-secrets get AGENTS_JAYS_SERVICES_ACCESS_SERVICE_TOKEN_CF_ACCESS_CLIENT_ID)"
  put RECALL_ACCESS_CLIENT_SECRET "$(fleet-secrets get AGENTS_JAYS_SERVICES_ACCESS_SERVICE_TOKEN_CF_ACCESS_CLIENT_SECRET)"
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
  CLOUDFLARE_EMAIL="$(fleet-secrets get CLOUDFLARE_JAY_ACCOUNT_EMAIL)"
  CLOUDFLARE_API_KEY="$(fleet-secrets get CLOUDFLARE_JAY_API_KEY)"
  export CLOUDFLARE_EMAIL CLOUDFLARE_API_KEY
  put() { [ -n "$2" ] || { echo "missing $1" >&2; return 1; }; printf %s "$2" | WRANGLER_SEND_METRICS=false npx --no-install wrangler secret put "$1" >/dev/null && echo "set $1"; }
  # Read the pair from wherever the owner keeps seat credentials (see below), then:
  # put LINEAR_GROK_WEB_CLIENT_ID     "<the client id>"
  # put LINEAR_GROK_WEB_CLIENT_SECRET "<the client secret>"
)
```

**Which Infisical project holds the pair.**  AI Fleet Coordinator, `prod`, `/linear`:  as of Thu, Oct 16, 2026 every hosted seat's pair is there (Infisical names `LINEAR_<SEAT>_CLIENT_ID` and `_SECRET`, hyphens as underscores), and the installs below read them from it.  The Worker secret names come from `LINEAR_SECRETS` in `src/linear.js` and match the Infisical names, except WISE:  its Infisical pair `LINEAR_WISE_CLIENT_ID`/`_SECRET` is installed on the Worker as `LINEAR_INSTINCT_CLIENT_ID`/`_SECRET`.  `docs/protocols/linear-migration.md` section 2 planned a separate **Fleet Linear Seats** project;  it is not where the hosted seats' pairs live.  Before a `put`, run `board whoami` as the seat with that pair and check the viewer is the seat's own app.

**Which seats are on.**  `LINEAR_SEATS` in `src/linear.js` lists the seats whose Linear tools answer at all:  GROK-WEB, CURSOR, JET, ECHO, WISE and CLAUDE (the last four on Thu, Oct 16, 2026, after `board whoami` confirmed each pair belongs to its own app).  Their pairs come from Infisical AI Fleet Coordinator `prod` `/linear` (`LINEAR_<SEAT>_CLIENT_ID`/`_SECRET`;  WISE's are installed under the Worker names `LINEAR_INSTINCT_*`).  Adding `ECHO` or `WISE` is a one-line code change and a redeploy, deliberately, so a seat cannot gain board access by accident.

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

The Worker answers `POST /internal/wake/<SEAT>` from the server listener and fans each wake out as an MCP event (spec section 3.12).  A seat's wake works only once its shared HMAC key is a Worker secret.  The key lives in Infisical Fleet-OPS as `JET_ROUTINE_KEY` (64 hex characters, made with `openssl rand -hex 32`;  Jay stores a new one there himself).  It is installed without printing it (same environment as Step 1):

```bash
fleet-secrets get JET_ROUTINE_KEY | tr -d '"\n' | WRANGLER_SEND_METRICS=false npx --no-install wrangler secret put WAKE_HMAC_KEY_JET >/dev/null && echo "set WAKE_HMAC_KEY_JET"
```

The listener half is Jay's step (ARMING-JAY.md, "Wake Jet", step 1):  two Coolify variables (`JET_ROUTINE_URL=https://agent-sync.jays.services/internal/wake/JET`, and `JET_ROUTINE_KEY` set to the same key), a scoped edit of the **live** `/data/listener.toml` (the volume keeps the copy seeded on first start, so the repo sample's new JET section never reaches it), and a restart.  Rotating the key means rerunning both.  Check:  `POST /internal/wake/JET` with no signature answers 401 (not 404, which would mean the secret is missing), and `infra_phase0.py check` probes it.  `EVENT_CALLBACK_HOSTS` in `wrangler.jsonc` lists the hosts ChatGPT may give as a callback.  A refused one shows on `/admin` as `callback_host_not_allowed`, and adding it is a var change and a deploy.

**Deploy order for the wake fixes (Sat, Oct 10).**  The Worker now delivers inside the listener's request and answers 503 when no delivery got through, so the listener's own retry is the retry path.  It also delivers a channel wake only when the body's `stream_id` is a channel the seat may use (spec 3.12, items 8, 11 and 12;  then the `CHANNELS` allowlist, since Sat, Oct 10, 2026 any public channel the bot is subscribed to, section "Any public channel" below).  Deploy the server listener first:  it starts sending `stream_id`, which the deployed Worker ignores.  Then deploy this Worker (step 1).  The other order fails safe:  until the listener sends `stream_id`, the Worker answers every channel wake 202 `channel_refused` and wakes nobody.  Nothing is activated yet, so either order wakes nobody today.  There are no secret, var, binding or migration changes.  Check after the Worker deploy:  `POST /internal/wake/JET` with no signature still answers 401.  JET's routine `timeout_seconds` must stay at 15 or more, or the listener abandons requests that are still delivering.

## Re-enable JET

JET was left out of Phase 2 because `openai-dot-bot` was a realm administrator (role 200) and hosted seats accept member (400) only (spec 3.6;  the listener refuses admin keys too).  Jay demoted every bot to member on Fri, Oct 9, and steps 1 to 3 are done.  Only step 4, Jay's ChatGPT connection, is left.  The same steps re-enable any seat that was taken out.

1. Jay demotes the bot to **member** in Zulip (Organization settings → Users → the bot → Role).  Done for `openai-dot-bot` on Fri, Oct 9.
2. Put the seat in `HOSTED_SEATS` in `wrangler.jsonc` (`"JET,GROK-WEB"`), open a PR, merge.  Done on Fri, Oct 9.
3. Deploy (step 1), then `python3 -I install_seat_key.py JET --apply`.  The script refuses while the live role is not 400.  Done on Fri, Oct 9, at about 8:10am:  version `068c2953` is live, `ZULIP_KEY_JET` is installed, and `infra_phase0.py check` reports 0 failures.
4. Jay arms JET and connects ChatGPT (ARMING-JAY.md).  Still open.

## Add ECHO and WISE

Two identities of the Instinct app (instinct.com), added on Fri, Oct 9:  ECHO posts as `instinct-bat-bot@`, WISE as `instinct-owl-bot@`, both members (role 400).  They share the app's loopback callback `http://127.0.0.1:8737/callback`, the one kind of redirect two seats may share (spec 3.4).

1. Both seats are in `HOSTED_SEATS`, `ZULIP_EMAIL_ECHO` and `ZULIP_EMAIL_WISE` are set, and `SEATS` lists the callback under both.
2. Deploy (step 1), then install both keys with `--zuliprc` (step 2).  Done on Fri, Oct 9, at about 10:24pm:  code version `37f28601` deployed, `ZULIP_KEY_ECHO` and `ZULIP_KEY_WISE` installed (live version `493d4192`), and `infra_phase0.py check` reports 0 failures.
3. Jay connects one seat at a time (ARMING-JAY.md, "Connect Echo" and "Connect Wise").  The client id is unknown until the first attempt:  a manual client from `/admin`, or a CIMD id from the refusal log, added to `SEATS.<seat>.cimd_client_ids`.  One CIMD id cannot be listed under both seats yet (`cimdOwner` maps an id to one seat), so if Instinct uses one client document for both identities, that needs a code change first.

## Add the DM tools

Owner ruling, Sat, Oct 10 ("all should have DM tools"), spec section 1.2:  `dm_list`, `dm_read` and `dm_send` for every hosted seat, and DMs in `inbox` (AFC #430).  No secret, scope or var changes, and no migration:  step 1 is the whole deploy.  Done on Sat, Oct 10, at about 12:34am, from `origin/main` at `2a8bc7dd`:  version `50042a13`, `/health` ok, `/mcp` without a token 401, and `infra_phase0.py check` reports 0 failures.  This deploy also carried the MCP Events code from #428 (inert for a seat without its `WAKE_HMAC_KEY_<SEAT>` secret).

Clients that cache `tools/list` (Grok on the web, ChatGPT) must refresh the connector's tools to see the new ones.  No reconnect and no new consent:  the tools use the two existing scopes.

## DM text and held-back wakes in events

Owner decisions, Sat, Oct 10:  a DM event carries its fenced excerpt ("yes"), and wakes the listener held back for its budget are surfaced ("surface all");  spec section 3.12, items 2, 9 and 14.  The Worker now forwards a DM's excerpt and accepts an optional `held_back` in the wake body (400 `bad_held_back` when malformed), forwarding it reduced to the channels the seat may use.  No secret, scope or var changes, and no migration:  step 1 is the whole deploy.  **Deploy it before the listener that sends `held_back`:**  this Worker version must be live first, or that listener counts items as surfaced that never reached Jet.  ChatGPT may need a rescan of the plugin to see the new `held_back` field in the event's payload schema;  nothing else changes for the subscription.

## Any public channel

Owner ruling, Sat, Oct 10, 2026:  "I want bots to wake to any channel or DM" … "not just agent-sync or one other", and for the ECHO and WISE pollers, "All of them".  The `CHANNELS` var (#agent-sync and #sandbox) is removed;  a seat now uses any public channel its own bot is subscribed to, read from Zulip per seat and cached 60 seconds, minus `CHANNELS_DENY` (a JSON array of stream ids in `wrangler.jsonc` vars, `[]` by default).  Private channels are never allowed.  MCP Events wakes use the same rule, read at delivery time with the seat's key (a Zulip failure there answers 503, so the listener retries).  The `events/list` channel filter is now a free string checked at subscribe time;  ChatGPT may need a plugin rescan to see the new schema, and an existing subscription keeps working.  No secret, scope or binding changes, and no migration:  step 1 is the whole deploy.  To keep a public channel off every seat, add its stream id to `CHANNELS_DENY` and redeploy.

**Deployed** Sun, Oct 11, about 12:39am (CLAUDE sub-agent), from `10b2042b` (PR 464):  version `ebe48ba3-c792-44af-8d0b-c4cb151a04c6`, `CHANNELS_DENY ([])`, no `CHANNELS`.  `/health` answered `{"ok":true}`;  `/mcp` with no token and an unsigned `/internal/wake/JET` both answered 401.

## The rest of Zulip (AFC-330)

Owner, Thu, Oct 15:  "I'd like Agent-Sync and whatever local agents use to be able to do all tasks supported by Zulip."  33 more `zulip_*` tools (spec section 1.6) come from `tools.json` by construction, implemented in `src/zulip-full.js`.  No secret, scope or var changes:  step 1 is the whole deploy.  Clients that cache `tools/list` (Grok on the web, ChatGPT) refresh the connector's tools to see them.  Smoke test from a connected seat after the deploy:  `zulip_channels`, then `zulip_search` with `{"query": "deploy", "channel": "sandbox"}`, then `zulip_draft_create` and `zulip_draft_delete` in #sandbox (a draft is never sent).  **Not deployed yet.**

## Listener read API (AFC-330)

The `listener_status`, `listener_inbox`, `listener_owner_queue`, `listener_wakes` and `listener_held_back` tools (spec section 1.7) read the server listener's signed read API with the seat's `WAKE_HMAC_KEY_<SEAT>`.  Until both halves are set up they answer `not_configured` and nothing else changes.

1. The listener half first:  `docs/protocols/agent-sync-server.md`, "The Read API", steps 1 to 5 (redeploy, keys, `[listener_api]`, the proxy route, the checks).
2. Set the var in `wrangler.jsonc`:  `"LISTENER_API_URL": "https://listener.jays.services"` (an https origin with no path;  anything else reads as unset).  Open a PR and merge it.
3. Make sure each seat that should read its records has `WAKE_HMAC_KEY_<SEAT>` (JET already does;  the others only if Jay chose to add them in the server doc's step 2):
   ```bash
   fleet-secrets get JET_ROUTINE_KEY | tr -d '"\n' | WRANGLER_SEND_METRICS=false npx --no-install wrangler secret put WAKE_HMAC_KEY_JET >/dev/null && echo "set WAKE_HMAC_KEY_JET"
   ```
4. Deploy (step 1).  From a connected JET chat, `listener_status` answers counts;  a 401 there means the two keys differ or the clocks are more than five minutes apart, a 404 that the listener has no key for the seat.  **Not deployed yet.**

## Notes, recall and the guide (AFC-331)

`notes_create`, `notes_update`, `notes_pin`, `notes_unpin` and `notes_status` (`src/notes.js`) relay Apple Notes requests to `#notes-relay`;  the Mac listener does the work (`docs/protocols/per-call-identity.md`, section 5).  Before the first call: the owner creates the public channel `#notes-relay`, every bot that relays is subscribed to it (the channel rule needs that), and the Mac's `listener.toml` has `[notes_relay] enabled = true`.  No secret or var changes.  The `guide` tool serves `src/guide-text.js`, generated from `docs/protocols/agent-sync-tools-guide.md`:  after editing that page run `node scripts/export-guide.mjs` (a test fails when it is stale).  `recall_*` specs are shared with the stdio server through `scripts/agent_sync/mcp/recall-tools.json`:  after changing `src/recall.js` run `node scripts/export-recall-tools.mjs`.  Clients that cache `tools/list` refresh the connector to see the new tools.  `notes_status` counts a check mark, a cross mark or a refusal reply only from the relay consumer's bot, named by the fleet knob `NOTES_RELAY_CONSUMER`.  It is a tunable, so it lives in Infisical (not in `wrangler.jsonc`):  **the owner creates the key `NOTES_RELAY_CONSUMER` with the value `CLAUDE` in the AI Fleet Coordinator project, `prod`, root path `/`** (INFISICAL.md, "Fleet knobs").  The Mac reads it through `fleet_knobs` (default `CLAUDE` when Infisical is unreachable with no last-known-good copy, logged as `default CLAUDE (unreachable:...)`);  the Worker gets it as a binding of the same name.  Copy it at deploy, after the key exists:  `python3 -I sync_notes_consumer.py` checks (prints the seat name, writes nothing to Infisical), `python3 -I sync_notes_consumer.py --apply` runs `wrangler secret put NOTES_RELAY_CONSUMER` with the value on stdin.  Rerun it whenever the key changes.  **Rotating the consumer:**  only the current consumer's marks and replies are trusted, on purpose, so a former consumer cannot forge a result.  Rotate it only with an empty spool (no request queued or half-run), and expect the status of any request sent before the rotation to read `queued` afterwards.  Until it has run, `notes_status` answers `not_configured` and trusts no reaction.  One more binding counts against the Worker's limit (see the FleetLink note below).  **Live since Sun, Oct 18, 2026:**  the knob is `CLAUDE` in Infisical, the Worker binding is installed, `#notes-relay` exists with every fleet agent bot subscribed (38, created by the realm-admin bot), and the Mac listener's `[notes_relay]` is on (`notes-relay-started`, consumer from Infisical).  Create, pin and unpin through the relay were verified end to end that afternoon.

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
- **ECHO and WISE:**  connect one at a time from the Instinct app's own computer ([ARMING-JAY.md](ARMING-JAY.md), "Connect Echo" and "Connect Wise").  The Worker serves both and both keys are installed (Fri, Oct 9, about 10:24pm);  the client id comes from the first attempt.
- **JET:**  arm it and connect ChatGPT ([ARMING-JAY.md](ARMING-JAY.md)).  The bot is a member, the Worker serves JET and its key is installed (Fri, Oct 9, about 8:10am), so nothing else blocks it.


## Add Cursor Cloud (AFC-294)

Jay approved the CURSOR hosted seat, its exact Cloud callback, bounded client
registration, tests and deployment on 2026-10-11.  The live preflight at version
`22c6b6d6-9db4-435b-b6a0-f8dbf7645c35` had no `ZULIP_KEY_CURSOR` binding.
This task does not read or copy CURSOR's existing bot key.  Jay approved the
exact published CIMD client plus Cloud callback pair on 2026-10-14 by supplying
the refused-request entry.  The allowlist remains bound to CURSOR and that one
exact pair.

1. Merge and deploy the CURSOR code/config through steps 0–1.  Preserve the live
   OAuth KV namespace, SeatGate bindings/migrations, all existing secrets,
   `LINEAR_TOOLS_WRITES=0`, and Jet's grants, epochs, subscription and wakes.
2. **Secure owner handoff:** Jay provisions CURSOR's own bot credential through
   the Cloudflare dashboard's encrypted Secret field, named `ZULIP_KEY_CURSOR`,
   on account `3a9368057468d0909cafaa85df12d1b7`, Worker `agent-sync-mcp`.
   Maintain the corresponding source of truth in AI Fleet Coordinator Infisical
   `prod` `/zulip` as `ZULIP_CURSOR_API_KEY` and `ZULIP_CURSOR_EMAIL`.
   Never send the value, a screenshot of it, or a token through chat.  Do not
   rotate an existing key just for this integration.  Do not create another bot.
   For an owner-operated installation from that store, the existing verifier is:
   `python3 -I install_seat_key.py CURSOR --apply` (not run by CODEX for this task).
   It checks the configured email, own-seat bot identity and member role before
   writing through stdin; the hosted Worker repeats those checks at runtime.
3. Follow **Connect Cursor Cloud** in ARMING-JAY.md.  Either the manual public
   client or the exact CURSOR CIMD pair may use the HTTPS Cloud callback.  The
   CIMD callback is fixed by Cursor and must not be edited.  Complete Access and
   consent through a fresh Cursor-originated flow; do not replay an authorize URL.
   Public client IDs may be
   copied; bot keys, Access PINs, codes, and tokens must remain in secure inputs.
4. Record the deployed version and the actual Cursor-side `whoami` result.
   Keep AFC-294 pending if the key, interactive consent, or live identity check
   is unfinished.  Unit/workerd fake-Zulip tests do not prove browser consent
   or production identity.  Verify from the intended Cursor surface.

No Cursor wake key or event callback host is added; this connection does not
modify the listener or Jet's ongoing subscription.  Hosted Linear provisioning
for CURSOR is outside this request and remains unconfigured.

## Add hosted CLAUDE (AFC-296)

This change prepares CLAUDE's hosted configuration only.  It does not provision a
key, create an OAuth client, deploy or establish a working connection.  Obtain
explicit authorization for these actions before performing the rollout.

1. Reuse the owner-confirmed existing `ZULIP_CLAUDE_EMAIL` and
   `ZULIP_CLAUDE_API_KEY` entries in AI Fleet Coordinator, `prod`, `/zulip`.
   Worker provisioning is unconfirmed; use the real CLAUDE bot's own key.
   Expected email: `claude-bot@simplewithus.zulipchat.com`.  Do not paste the key
   into chat, source, a client form or an agent-readable local handoff.  The key
   must belong to that bot, with member role 400, not administrator or owner.
2. After deployment authorization, deploy using this runbook's existing process.
   The added Worker var is `ZULIP_CLAUDE_EMAIL`; the required Worker secret is
   `ZULIP_CLAUDE_API_KEY`.  The checked installer supports CLAUDE and reads the
   canonical Infisical names above.  Its check-only mode verifies users/me, bot
   email, CLAUDE tag and role 400 before its authorized apply step writes the
   secret through stdin.  Secret installation publishes a Worker version, so
   it is part of the deployment approval, not a preparation step.

   **Owner-operated commands, from `scripts/agent-sync-mcp/`:** using the existing
   canonical entries in step 1, first run the check-only command:

   ```bash
   python3 -I install_seat_key.py CLAUDE
   ```

   Only if every identity and member-role check passes and deployment is
   authorized, run the apply command:

   ```bash
   python3 -I install_seat_key.py CLAUDE --apply
   ```

   These commands read the canonical store inside the owner-operated installer.
   Agents do not read the secret, copy it to a local file or run this handoff.
   The apply command sends the value to Wrangler through stdin and publishes a
   Worker version; never run it as a check-only preparation step.
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


## Cursor Shared-Agent Budgets (AFC-312)

The owner authorized higher CURSOR limits for approximately 40 agents.
Store `AGENT_SYNC_CURSOR_TOOL_BUDGETS` in AFC Infisical `prod` `/zulip` first,
then provision the same Worker binding through stdin.  See INFISICAL.md for
approved limits.  Deploy the merged budget code before expecting the setting
to take effect.  It is read and validated with the existing startup config;
no runtime Infisical request is made per tool call.  Cursor Linear operations
use distinct counters and pacing; existing exhausted Zulip counters are retained.
Verify production CURSOR audit outcomes for both reads and writes after deployment.

## Per-Call Identity (AFC-322, AFC-331)

Every grant offers per-call `identity` selection (`docs/protocols/per-call-identity.md`).  A CURSOR or GROK-WEB grant may act as CURSOR, GROK-WEB and the twelve enabled Grok Bot personas;  every other grant only as its own seat.  `identities` (formerly `agent_sync_identities`) reports the set with Zulip and Linear configured or missing states.  The personas are the `enabled` list in `scripts/agent_sync/mcp/gb-identities.json`:  `GB-DIRECTOR`, `GB-FIXER`, `GB-DESIGNER`, `GB-COMPILER`, `GB-HOUSEKEEPER`, `GB-PUBLISHER`, `GB-DEPLOYER`, `GB-MONITOR`, `GB-PLUMBER`, `GB-ORACLE` and `GB-TRADER`.  `GB-ENGINEER` is disabled and `BF-*` identities belong to BotFleet, so neither appears.  Writes must name their identity;  a client that omits it gets `identity_required`, so the Cursor and Grok connector instructions must say to pass it.  No OAuth client, callback, token, grant, scope or `HOSTED_SEATS` entry is added for a GB identity.

The source remains the 24 `ZULIP_GB_<ROLE>_EMAIL` and `ZULIP_GB_<ROLE>_API_KEY` values in the **AI Fleet Coordinator** Infisical project, `prod` `/zulip`, plus the 24 `LINEAR_GB_<ROLE>_CLIENT_ID` and `LINEAR_GB_<ROLE>_CLIENT_SECRET` values in that project's `prod` `/linear`.  The earlier separate **Fleet Linear Seats** project guidance does not apply to these GB pairs.  Each role's email must equal `<role>-grok-bot@simplewithus.zulipchat.com`;  its Zulip key must authenticate that member bot (role 400), and its Linear viewer must name the selected `GB-ROLE` app.

Install **one** encrypted Worker secret named `GB_IDENTITY_CREDENTIALS`.  Its JSON object has `v: 1` and an `r` object keyed by exactly the twelve roles above (ENGINEER joined on 2026-10-22:  the Worker refuses a bundle whose role count differs from the enabled list, so the next manual deploy must install a twelve-role bundle first);  each role value is the four-string array `[email, zulipKey, linearClientId, linearClientSecret]`.  The UTF-8 payload is capped at 5 KiB.  Build it only in memory from the 48 verified Infisical source values and pass it to `wrangler secret put GB_IDENTITY_CREDENTIALS` on stdin;  never print or save the payload.  The Worker parses the bundle once per authenticated CURSOR or GROK-WEB request, so the one existing `GB_IDENTITY_CREDENTIALS` secret serves both grants;  nothing new is installed for GROK-WEB.  A present malformed or incomplete bundle fails closed;  legacy per-role bindings are considered only when the bundle binding is absent, and then only complete four-field rows are used.  Do not mix credentials between sources or roles.  This single binding stays below the Free plan's 64 binding limit where 44 separate bindings could not fit.

`identities` exposes configured or missing status, never values.  The shared `LINEAR_TOOLS_WRITES` switch still controls writes.  All selected tool calls charge the grant's own budget and the identity's own gate.  A GROK-WEB grant acting as CURSOR uses `ZULIP_KEY_CURSOR` and CURSOR's Linear pair, and CURSOR acting as GROK-WEB uses `ZULIP_KEY_GROK_WEB` and GROK-WEB's Linear pair, so both seats must stay in `HOSTED_SEATS` with their keys installed for the peer path to work (it answers `not_configured` otherwise).  The deployment and production acceptance check are separate from this code change.

At deploy (AFC-331):  the tool names change (`post` is now `zulip_post`, and so on) and the old names answer `unknown_tool`, so after the deploy Jay refreshes the connector's tools in each client that caches the list (Grok on the web, Cursor, ChatGPT), and agents re-read their instructions.  No new secret, binding or variable is needed.

## FleetLink (AFC-328)

The `fleetlink_*` tools need one Worker secret, `FLEETLINK_AGENT_SECRET`:  `fls_` plus 64 hex.  FleetLink holds the same value as its own Worker secret `AGENT_SYNC_SECRET` (FleetLink `docs/multi-user.md`, "Agent Accounts").  Without it the FleetLink tools answer `not_configured` and everything else works.  The value lives in Infisical, project "AI Fleet Coordinator", `prod`, `/fleetlink`, as `FLEETLINK_AGENT_SECRET` (INFISICAL.md).  It was made and installed on both Workers on Thu, Oct 15, 2026.  To reinstall it here, read it with the INFISICAL_AUTOMATION identity and pipe it straight in, with the Step 1 credentials exported in the same subshell.  Never print it:

```bash
(
  set +x
  # fetch prints only the value, to the pipe:  universal-auth login, then
  # GET /v3/secrets/raw/FLEETLINK_AGENT_SECRET?workspaceId=<project>&environment=prod&secretPath=/fleetlink
  fetch_fleetlink_secret | WRANGLER_SEND_METRICS=false npx --no-install wrangler secret put FLEETLINK_AGENT_SECRET >/dev/null && echo "set FLEETLINK_AGENT_SECRET"
)
```

On FleetLink's side, the same value goes to `AGENT_SYNC_SECRET` on the `fleetlink` Worker.  Its deploy (`FLEETLINK_SECRETS=keep`) keeps the secret, and `wrangler secret put AGENT_SYNC_SECRET` or the Workers secrets API replaces it.  Rotate by installing a new value on both sides, FleetLink first:  until this Worker has the new value too, the tools answer `not_authorized` with HTTP 401.

**Seat tokens.**  A seat that has its own FleetLink bot token signs in with it instead of the server credential, so it works under that token's limits:  the fleet's tokens are minted to follow Jay's limits as they change (FleetLink `limits_from: "manager"`).  The tokens live in Infisical `/fleetlink` as `FLEETLINK_TOKEN_<SEAT>` (hyphens as underscores), and the Worker reads them from one secret, `FLEETLINK_SEAT_TOKENS`, a JSON object of seat to token (`{"JET": "fla_…", "GB-FIXER": "fla_…"}`), because the Worker is near its binding limit.  Build it in memory from Infisical for every hosted seat and GB identity, and pipe it to `wrangler secret put FLEETLINK_SEAT_TOKENS`;  never print it.  A malformed bundle is ignored whole and every seat falls back to `FLEETLINK_AGENT_SECRET`.  Re-run it after minting a token for a new seat.

Check with `fleetlink_whoami` from any seat:  `user.id` is `usr_agent_<seat>` and `auth.kind` is `agent`.  In the Worker log, `"tool":"fleetlink_whoami"` with `outcome` `ok` means the credential works.  Jay is the fleet owner (FleetLink's `FLEET_OWNER_EMAIL` secret, or "Make Me The Fleet Owner" in its admin console):  every seat's bot account sits under his account and he owns the Fleet team.
