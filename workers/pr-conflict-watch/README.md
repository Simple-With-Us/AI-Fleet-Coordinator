# pr-conflict-watch

Cloudflare Worker that receives GitHub `pull_request` and `check_suite`/`check_run` webhooks across all of jaywedgeworth22's active repos, and posts to the fleet's Zulip `#Alerts` stream the moment a PR's `mergeable_state` turns `dirty` (real merge conflict).  Posts again when a dirty PR clears.  `blocked` and `unstable` are ignored so CI churn does not burn the free-tier KV write budget.  Dedupes via a KV namespace so it only alerts on state *changes*, not every webhook delivery.

The conflict alert names the seat that owns the PR branch, so the seat hears about its own conflict instead of every seat watching `#Alerts` for it.  The seat comes from the branch prefix before the first `/` (`gb-deployer/afc-123-fix` → `@**GB-Deployer**`).  A branch whose prefix is not a known seat gets no mention, and the alert falls back to naming the author's login and branch.  Alerts never mention `@all` or `@everyone`.

This replaces the retired Slack `#agent-sync` channel, which no longer receives conflict alerts, and it replaces BF-Deployer's per-PR conflict watching.

## Deployed

Worker: `pr-conflict-watch` on the Usage.Jays.Services Cloudflare account (`3a9368057468d0909cafaa85df12d1b7`).
URL: `https://pr-conflict-watch.jays-services.workers.dev`

Registered as a repo webhook (events: `pull_request`, `check_suite`) on active non-archived/non-fork repos under jaywedgeworth22.  As of 2026-09-03 the one-file `ios-app-versions` repo is retired; the public iOS version manifest lives at `site/ios-versions.json` in this repo.

The webhook event list is unchanged by the Zulip move: `pull_request` and `check_suite` (plus `check_run`, which the Worker also accepts) still arrive exactly as before.

## Secrets (Worker secrets, not in this repo)

- `GITHUB_WEBHOOK_SECRET` -- random secret shared with each repo's webhook config, used to verify `X-Hub-Signature-256`.
- `GITHUB_TOKEN` -- read-only GitHub API calls to fetch PR `mergeable_state`.
- `ZULIP_SITE` -- the Zulip site base URL, e.g. `https://simplewithus.zulipchat.com`.
- `ZULIP_BOT_EMAIL` -- the email of the fleet alerts bot.
- `ZULIP_BOT_API_KEY` -- that bot's API key, used for the Basic auth header on `POST /api/v1/messages`.

The values come from Infisical, which is the only source of secrets for the fleet.  They are pushed into the Worker's secrets at deploy time with `wrangler secret put <NAME>`; no secret value is ever written to this repo, to a log, or to a pull request.

The Zulip bot is the fleet alerts bot that already posts to `#Alerts`, so it needs no new bot and no new stream subscription.  The same bot reads the fleet's alert topics, so the conflict alerts land beside the other fleet alerts.

## Message format

- Topic: `pr-conflicts <repo>`, e.g. `pr-conflicts BotFleet`.
- New conflict: ``PR [#<n> <title>](<url>) on `<repo>` has a merge conflict with `<base>`.  <mention> please rebase it.``
- Recovered: ``PR [#<n>](<url>) on `<repo>` no longer conflicts (now `<state>`).``

Sentences are separated by a no-break space and a normal space, the fleet's prose style.

## Redeploy

The deploy is a manual post-merge step; it does not run on merge.

```bash
cd workers/pr-conflict-watch
CLOUDFLARE_API_TOKEN=... CLOUDFLARE_ACCOUNT_ID=3a9368057468d0909cafaa85df12d1b7 wrangler deploy
```

Secrets are set at deploy time, not in this repo:

```bash
cd workers/pr-conflict-watch
wrangler secret put GITHUB_WEBHOOK_SECRET
wrangler secret put GITHUB_TOKEN
wrangler secret put ZULIP_SITE
wrangler secret put ZULIP_BOT_EMAIL
wrangler secret put ZULIP_BOT_API_KEY
```

The old Slack secret is no longer read and should be removed once the Zulip deploy is live:

```bash
cd workers/pr-conflict-watch
wrangler secret delete SLACK_BOT_TOKEN
```

If secrets need rotating: `wrangler secret put <NAME>`, then re-register each repo's webhook with the new `GITHUB_WEBHOOK_SECRET` via `gh api repos/{owner}/{repo}/hooks/{hook_id}` PATCH.

## Tests

```bash
node --test workers/pr-conflict-watch/*.test.js
```

The test files are named explicitly: the directory form (`node --test workers/pr-conflict-watch/`) resolves `index.js` as a single module and reports one passing test without running any test file, which leaves the gate vacuous (reproduced on Node 22 and 24).
