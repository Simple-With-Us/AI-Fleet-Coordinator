# Deploy the agent-sync MCP Worker

The runbook for `https://agent-sync.jays.services` from Phase 2 on:  the seven tools, GROK-WEB served, JET blocked until its bot is a member.  [DEPLOY-PHASE0.md](DEPLOY-PHASE0.md) is the record of the first deploy and the hostname move.

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

## Step 2:  Install the GROK-WEB key

```bash
python3 -I install_seat_key.py GROK-WEB            # checks only
python3 -I install_seat_key.py GROK-WEB --apply    # wrangler secret put ZULIP_KEY_GROK_WEB, value on stdin
```

The script reads `ZULIP_GROK_WEB_EMAIL` and `ZULIP_GROK_WEB_API_KEY` from Infisical (project "AI Fleet Coordinator", `prod`, `/zulip`) through the INFISICAL_AUTOMATION identity, and refuses unless the seat is in `HOSTED_SEATS`, the email equals `ZULIP_EMAIL_GROK_WEB`, and Zulip's `users/me` for the key is that bot, a bot, and a member (role 400).  The value goes to wrangler on stdin and is never printed, logged or written to a file.  `wrangler secret put` deploys a new version at once.

Spec 3.6 wants Infisical's Cloudflare Workers sync to push the key, from a location no agent identity can read.  Neither exists yet (owner items A1 and A4), and D8 accepts that:  this script is the sync, run by hand, and the key stays in `/zulip` where the automation identity can read it.  When Jay sets up the sync with "Disable Secret Deletion", stop using the script for that seat.

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

It covers the metadata documents (`issuer` equal to `authorization_servers[0]`, S256 only, `iss`, CIMD, no DCR), 401 with `resource_metadata` on an unauthenticated `initialize` and on `GET /mcp`, Access in front of `/authorize`, `/admin` and `/admin/action`, `/health`, 404 elsewhere, no workers.dev, the custom domain, and no tunnel rule or tunnel CNAME for the host.  Then open `/admin`:  GROK-WEB shows **Installed** under Zulip Key (the role line appears after the first tool call), JET is absent.

What only Jay can verify, because it needs his Access sign-in and consent:  the full OAuth round trip and the first real post.  [ARMING-JAY.md](ARMING-JAY.md) has the steps;  the workerd flow proves the same path against a fake Zulip.

## Re-enable JET

JET is left out until it is re-enabled.  `openai-dot-bot` was a realm administrator (role 200), and hosted seats accept member (400) only (spec 3.6;  the listener refuses admin keys too).

1. Done Fri, Oct 9:  Jay demoted `openai-dot-bot` to **member** (all bots are members now).  `install_seat_key.py` still checks the live role in step 3.
2. Add `JET` to `HOSTED_SEATS` in `wrangler.jsonc` (`"JET,GROK-WEB"`), open a PR, merge.
3. Deploy (step 1), then `python3 -I install_seat_key.py JET --apply`.  The script refuses while the live role is not 400.
4. Jay arms JET and connects ChatGPT (ARMING-JAY.md).

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
- **JET:**  the demotion of `openai-dot-bot` to member is done (Fri, Oct 9);  steps 2 to 4 above are still open.
