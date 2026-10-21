# Autofix Dispatcher (trial)

Owner ruling 2026-10-21: a scheduled dispatcher replaces the tag-only gate as a trial.
One autofix run at a time across the fleet, the oldest untried PR first, every
bot-started run cc's Jay, and a running cost tally is kept.

## What it does

`.github/workflows/autofix-dispatcher.yml` runs hourly (`37 * * * *`) when the org
variable `SWU_AUTOFIX_DISPATCHER_ENABLED` is `true`.  Each tick:

1. Checks every fleet repo for queued or in-progress `autofix.yml` /
   `claude-code-autofix.yml` runs.  Any active run ends the tick: one at a time.
2. Scans open, non-draft PRs in the caller repos for review findings (a nonempty
   review comment from `kody-ai[bot]`, `chatgpt-codex-connector[bot]`, or
   `sentry[bot]`) with no `swu-autofix-run: bot=...` marker comment from any bot.
   The reusable lowercases the login and strips `[bot]` when it writes the
   marker, so swu-echo[bot]'s marker reads `swu-autofix-run: bot=swu-echo`.
3. Picks the oldest by `created_at` and posts `@pr-autofix ... cc @jaywedgeworth22`
   as `swu-echo[bot]`.  The repo's normal pr-autofix caller starts the run and leaves
   the tried marker; the tag consumes swu-echo's one run on that PR under the
   one-run-per-bot-per-PR owner rule.
4. Appends the dispatch to `autofix-dispatcher/tally.jsonl` on the
   `autofix-dispatcher-state` branch.  Every tick also reconciles up to 20 of
   the oldest unreconciled entries: run id from the marker comment, conclusion,
   and `total_cost_usd` from the run log.  A tick that learns anything saves
   even when a later step fails.

The tag gate still decides what counts as a request:  `@pr-autofix` must be the
first token of the first non-blank line after fenced code and quoted text are
stripped.  A tag mentioned mid-sentence, inside a quote, or inside a fence is
discussion and never starts a run.  The dispatcher only adds untried-PR pickups
on top.

## Boundaries

- Never dispatches while any fleet autofix run is queued or in progress (checked
  again immediately before the tag is posted).
- Skips drafts and any PR with an existing autofix marker from any bot.
- Repo list is explicit in `dispatcher.mjs`: every pr-autofix caller whose effective
  `allowed_bots` includes `swu-echo[bot]`.  homebrew-tap's caller names a list without
  it and FleetLink has no pr-autofix caller, so neither is scanned.
- Disable by setting `SWU_AUTOFIX_DISPATCHER_ENABLED` to anything but `true` (or
  deleting the variable).  Manual runs: Actions → Autofix Dispatcher → Run workflow
  with `dry_run` on to see the pick without spending.

## Tally

The state branch holds one JSON object per line: repo, PR, dispatch comment, run id,
conclusion, and cost in USD when the run has completed.  The workflow log prints the
running total each tick.  Deleting the branch resets the tally, not the per-PR markers.
