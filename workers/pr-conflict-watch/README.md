# pr-conflict-watch

Cloudflare Worker that receives GitHub `pull_request` and `check_suite`/`check_run` webhooks across all of jaywedgeworth22's active repos, and posts to #agent-sync Slack the moment a PR's `mergeable_state` turns `dirty` (real conflict), `blocked`, or `unstable`. Posts again when a flagged PR recovers. Dedupes via a KV namespace so it only alerts on state *changes*, not every webhook delivery.

## Delivery and retries

KV records the last successfully notified bad state.  Alerts update that marker,
and recovery notices remove it, only after Slack returns both a successful HTTP
status and JSON `ok: true`.  HTTP/API errors, invalid responses, and network
failures reject the background task without advancing the marker.  A subsequent
webhook event or manual redelivery can retry against the PR's current state.

The webhook still returns HTTP 200 before background processing finishes; there
is no automatic retry queue in this Worker.  KV deduplication is best-effort,
not exactly-once: concurrent deliveries, stale KV reads, or a KV failure after
Slack accepts a message can produce duplicates.  Closed PRs clear their marker
without a recovery notice, as before.

## Tests

Run `node --test workers/pr-conflict-watch/index.test.mjs` from the repository
root with Node 24.  Tests use signed synthetic webhooks and mocked GitHub,
Slack, and KV; no live messages or credentials are needed.  CI runs this suite.

## Deployed

Worker: `pr-conflict-watch` on the Usage.Jays.Services Cloudflare account (`3a9368057468d0909cafaa85df12d1b7`).
URL: `https://pr-conflict-watch.jays-services.workers.dev`

Registered as a repo webhook (events: `pull_request`, `check_suite`) on active non-archived/non-fork repos under jaywedgeworth22.  As of 2026-09-03 the one-file `ios-app-versions` repo is retired; the public iOS version manifest lives at `site/ios-versions.json` in this repo.

## Secrets (Worker secrets, not in this repo)

- `GITHUB_WEBHOOK_SECRET` -- random secret shared with each repo's webhook config, used to verify `X-Hub-Signature-256`.
- `GITHUB_TOKEN` -- read-only GitHub API calls to fetch PR `mergeable_state`.
- `SLACK_BOT_TOKEN` -- posts to `#agent-sync` (channel `C0BEZDJDNKV`) via `chat.postMessage`.

## Redeploy

```bash
cd workers/pr-conflict-watch
CLOUDFLARE_API_TOKEN=... CLOUDFLARE_ACCOUNT_ID=3a9368057468d0909cafaa85df12d1b7 wrangler deploy
```

If secrets need rotating: `wrangler secret put <NAME>`, then re-register each repo's webhook with the new `GITHUB_WEBHOOK_SECRET` via `gh api repos/{owner}/{repo}/hooks/{hook_id}` PATCH.
