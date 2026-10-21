# Autofix Dispatcher (trial)

Owner ruling 2026-10-21: a scheduled dispatcher replaces the tag-only gate as a trial.
One autofix run at a time across the fleet, the oldest untried PR first, every
bot-started run cc's Jay, and a running cost tally is kept.

## What it does

`.github/workflows/autofix-dispatcher.yml` runs hourly (`37 * * * *`) when the org
variable `SWU_AUTOFIX_DISPATCHER_ENABLED` is `true`.  Each tick:

1. Checks every fleet repo for queued or in-progress `autofix.yml` /
   `claude-code-autofix.yml` runs.  Any active run ends the tick: one at a time.
2. Scans open, non-draft, same-repo PRs in the caller repos for review findings (a
   nonempty review comment from `kody-ai[bot]`, `chatgpt-codex-connector[bot]`, or
   `sentry[bot]`) with no `swu-autofix-run: bot=...` marker comment from any bot.  A PR
   that already carries this script's tag comment is never picked again: the reusable
   writes no marker when it declines a tag (a fork PR, the handbook mirror branch, a
   narrowed `allowed_bots`), so without that rule the declined PR would stay the oldest
   candidate and block every PR behind it.  Fork PRs and the handbook mirror branch are
   not scanned at all.
   The reusable lowercases the login and strips `[bot]` when it writes the
   marker, so swu-echo[bot]'s marker reads `swu-autofix-run: bot=swu-echo`.
3. Picks the oldest by `created_at` and posts `@pr-autofix ... cc @jaywedgeworth22`
   as `swu-echo[bot]`.  The repo's normal pr-autofix caller starts the run and leaves
   the tried marker; the tag consumes swu-echo's one run on that PR under the
   one-run-per-bot-per-PR owner rule.  The reusable builds the run's prompt from the
   tag comment plus the PR diff, not from the review threads, so the tag comment names
   the findings (the PR's review comments, read with
   `gh api repos/<owner>/<repo>/pulls/<n>/comments --paginate`) and tells the run to fix
   the ones still real at the head.  It points at them instead of copying them, so a
   new review round is never stale and no reviewer `@mention` is replayed.
4. Appends the dispatch to `autofix-dispatcher/tally.jsonl` on the
   `autofix-dispatcher-state` branch.  A tag comment whose row was never saved (the
   tick died between the two) is recorded on the next tick from the comment itself.
   Every tick also reconciles up to 20 unreconciled entries, never-checked first and
   then the one checked longest ago (`checked_at`), so an entry whose run never
   resolves cannot starve the ones behind it: run id from the marker comment,
   conclusion, and `total_cost_usd` from the run log.  A tick that learns anything
   saves even when a later step fails, but a tally that could not be read is never
   written back.  A reconcile lookup that fails (a deleted run) is a warning and does
   not block a dispatch; a failure in the tally read or the scans does, and the tick
   exits 1 without acting on partial data.

The tag gate itself is unchanged — bots and Jay can still start runs by tagging
`@pr-autofix` directly.  The dispatcher only adds untried-PR pickups on top.

## Boundaries

- Never dispatches while any fleet autofix run is queued or in progress (checked
  again immediately before the tag is posted).
- Skips drafts and any PR with an existing autofix marker from any bot.
- Repo list is explicit in `dispatcher.mjs`: every pr-autofix caller whose effective
  `allowed_bots` includes `swu-echo[bot]`.  homebrew-tap's caller names a list without
  it and FleetLink has no pr-autofix caller, so neither is scanned.
- Disable by setting `SWU_AUTOFIX_DISPATCHER_ENABLED` to anything but `true` (or
  deleting the variable).  Manual runs: Actions → Autofix Dispatcher → Run workflow
  with `dry_run` on to see the pick without spending.  A dry run does not read the
  tally, so it prints no running total.

## Tally

The state branch holds one JSON object per line: repo, PR, dispatch comment, run id,
conclusion, and cost in USD when the run has completed.  The workflow log prints the
running total each tick.  Deleting the branch resets the tally, not the per-PR markers.
