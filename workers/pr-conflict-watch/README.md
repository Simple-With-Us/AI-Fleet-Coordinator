# pr-conflict-watch

Cloudflare Worker that receives GitHub `pull_request` and `check_suite`/`check_run` webhooks across all of jaywedgeworth22's active repos, and posts to the fleet's Zulip `#Alerts` stream the moment a PR's `mergeable_state` turns `dirty` (real merge conflict).  Posts again when a dirty PR clears.  `blocked` and `unstable` are ignored so CI churn does not burn the free-tier KV write budget.  Dedupes via a KV namespace so it only alerts on state *changes*, not every webhook delivery.

The conflict alert names the seat that owns the PR branch, so the seat hears about its own conflict instead of every seat watching `#Alerts` for it.  The seat comes from the branch prefix before the first `/` (`gb-deployer/afc-123-fix` → `@**GB-Deployer**`).  A branch whose prefix is not a known seat gets no mention, and the alert falls back to naming the author's login and branch.  Alerts never mention `@all` or `@everyone`.  A mention that a PR title, head ref, base ref or login carries is neutralized with a zero-width space before the message is built, so a PR cannot ping the stream; the seat mention the Worker adds itself stays intact.

This replaces the retired Slack `#agent-sync` channel, which no longer receives conflict alerts, and it replaces BF-Deployer's per-PR conflict watching.

## Deployed

Worker: `pr-conflict-watch` on the Usage.Jays.Services Cloudflare account (`3a9368057468d0909cafaa85df12d1b7`).
URL: `https://pr-conflict-watch.jays-services.workers.dev`

Registered as a repo webhook (events: `pull_request`, `check_suite`) on active non-archived/non-fork repos under jaywedgeworth22.  As of 2026-09-03 the one-file `ios-app-versions` repo is retired; the public iOS version manifest lives at `site/ios-versions.json` in this repo.

The webhook event list is unchanged by the Zulip move: `pull_request` and `check_suite` (plus `check_run`, which the Worker also accepts) still arrive exactly as before.

## Secrets: Infisical at runtime

The Worker's only secret binding is `INFISICAL_SERVICE_TOKEN`, a read-only Infisical service token (`st.…`).  Every other secret is read at runtime from Infisical, so a secret is rotated in Infisical and the Worker picks it up within a minute (the resolved config is cached in the isolate's memory for 60 s; it is never written to KV and never logged).  There is no env read for any other secret, no fallback and no default: when Infisical cannot answer, the Worker answers 503 and posts nothing.

| Binding | Kind | What it is |
|---|---|---|
| `INFISICAL_SERVICE_TOKEN` | Worker secret | the read-only Infisical service token |
| `PR_STATE` | KV namespace | the dedupe records |

### The Infisical folder

In the Infisical project **Fleet-OPS**, environment **prod**, create the folder **/pr-conflict-watch** with these five names:

| Name | What it is |
|---|---|
| `GITHUB_WEBHOOK_SECRET` | random secret shared with each repo's webhook config, used to verify `X-Hub-Signature-256` |
| `GITHUB_TOKEN` | read-only GitHub API calls to fetch PR `mergeable_state` |
| `ZULIP_SITE` | the Zulip site base URL, e.g. `https://simplewithus.zulipchat.com` |
| `ZULIP_BOT_EMAIL` | the email of the fleet alerts bot |
| `ZULIP_BOT_API_KEY` | that bot's API key, used for the Basic auth header on `POST /api/v1/messages` |

The Zulip values are the fleet alerts bot, the MA bot: the same pair lives at Fleet-OPS prod `/zulip` as `MA_ZULIP_SITE` / `MA_ZULIP_EMAIL` / `MA_ZULIP_KEY`, so copy the values across.  The GitHub values are the ones the Worker uses today.

Store plain values, not Infisical references: expanding a reference would need the service token to read `/zulip` as well, and the token is scoped to exactly `/pr-conflict-watch`.

### The service token

Create one read-only service token named `pr-conflict-watch-read`, scoped to exactly environment `prod` and folder `/pr-conflict-watch`.  The Worker discovers the project id from the token itself and checks the token's own scope and permissions at runtime: a token that is not read-only, or that is not scoped to `prod` + `/pr-conflict-watch`, is refused.

Bind it:

```bash
cd workers/pr-conflict-watch
wrangler secret put INFISICAL_SERVICE_TOKEN
```

Then remove the Worker secrets the Worker no longer reads:

```bash
cd workers/pr-conflict-watch
wrangler secret delete SLACK_BOT_TOKEN
wrangler secret delete GITHUB_WEBHOOK_SECRET
wrangler secret delete GITHUB_TOKEN
# and any ZULIP_* Worker secrets, if someone set them:
wrangler secret delete ZULIP_SITE
wrangler secret delete ZULIP_BOT_EMAIL
wrangler secret delete ZULIP_BOT_API_KEY
```

No secret value is ever written to this repo, to a log, or to a pull request.

### The config step comes first

Merging main does not deploy this Worker today:  `.github/workflows/worker-deploy.yml` builds only `agent-sync-mcp`, and `scripts/worker-targets.json` gives `pr-conflict-watch` no health probe, so the mapped deploy refuses it.  The deploy is the manual `wrangler deploy` below, and the Infisical folder and the service token must exist **before** it:  a live Worker without them answers `503` and posts nothing.

The Zulip bot is the fleet alerts bot that already posts to `#Alerts`, so it needs no new bot and no new stream subscription.  The same bot reads the fleet's alert topics, so the conflict alerts land beside the other fleet alerts.

## Message format

- Topic: `pr-conflicts <repo>`, e.g. `pr-conflicts BotFleet`.
- New conflict: ``PR [#<n> <title>](<url>) on `<repo>` has a merge conflict with `<base>`.  <mention> please rebase it.``
- Recovered: ``PR [#<n>](<url>) on `<repo>` no longer conflicts (now `<state>`).``

Sentences are separated by a no-break space and a normal space, the fleet's prose style.

## Redeploy

The deploy is manual (see above);  run the command below from this directory.

```bash
cd workers/pr-conflict-watch
CLOUDFLARE_API_TOKEN=... CLOUDFLARE_ACCOUNT_ID=3a9368057468d0909cafaa85df12d1b7 wrangler deploy
```

The only secret the Worker binds is the Infisical service token, so a deploy needs nothing else:

```bash
cd workers/pr-conflict-watch
wrangler secret put INFISICAL_SERVICE_TOKEN
```

If the secret needs rotating: `wrangler secret put INFISICAL_SERVICE_TOKEN` with the new token.  The GitHub webhook secret is not a Worker secret any more: rotate it in Infisical (`/pr-conflict-watch`, `GITHUB_WEBHOOK_SECRET`) and re-register each repo's webhook with the new value via `gh api repos/{owner}/{repo}/hooks/{hook_id}` PATCH.  The Worker picks the new value up within a minute, with no redeploy.

## Tests

```bash
node --test workers/pr-conflict-watch/*.test.js
```

The test files are named explicitly: the directory form (`node --test workers/pr-conflict-watch/`) resolves `index.js` as a single module and reports one passing test without running any test file, which leaves the gate vacuous (reproduced on Node 22 and 24).
