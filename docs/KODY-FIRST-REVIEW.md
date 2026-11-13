# Kody first-review prompter

Owner request 2026-11-07 (iMessage): make Kody's *initial* review automatic across
every fleet repo by prompting it, rather than trying to stop later reviews. This
automation posts that comment once for each open PR that has never had a Kody
review, and then never again.

The November 7 note that global review cadence was already Manual is not what
Kodus does.  Follow-up reviews run on push.  The hosted setting that stops them is
in `docs/KODY-ONCE-PER-PR.md` ([AFC-735](https://linear.app/simple-with-us/issue/AFC-735)).

## How it works

Each repo carries a thin caller (`.github/workflows/kody-first-review.yml`, hourly
cron) that invokes the reusable workflow here
(`.github/workflows/kody-first-review-reusable.yml`). The caller runs with its own
repository `GITHUB_TOKEN`, so comments come from `github-actions[bot]` and the
automation physically cannot touch any other repository: no cross-repo credential
exists. AFC is public, so caller tokens can check out the tooling.

Each tick, for every open PR in the repo (drafts included, per the owner request):

1. Skip PRs younger than 30 minutes. Kodus' own automatic initial review (global
   setting, ON) gets that window to land first; the prompter is the backstop for the
   PRs it misses (bot-opened PRs, webhook misses, model-capacity skips).
2. Skip if Kody has ever commented or reviewed. A `Code Review Could Not Complete`
   comment counts as answered: failed reviews are never auto-retried, so a model
   outage cannot turn the schedule into retry spam. Failed PRs stay manual.
3. Skip if any comment already carries the request. The request counts only as its
   own line outside a ``` or ~~~ code fence (same parsing rule as kody-defer): a quoted or
   fenced mention is discussion and does not suppress the prompt.
4. Otherwise post `@kody start-review`.

The posted comment is the dedup record; no separate marker or state branch exists.

## Honest limits

- At-most-once per quiet window, not exactly-once. The list-check-post sequence races
  with Kodus' own automatic review and with people or agents commenting by hand; the
  30-minute grace keeps the window small. A lost race produces one extra trigger
  comment on an already-running or already-reviewed PR, which Kody answers with its
  normal in-progress/already-reviewed response. No duplicate reviews were observed in
  the 2026-11-07 manual sweep of 18 prompts, but that is an observation, not a queue
  guarantee.
- A completed review banner is a review outcome, not merge readiness: findings still
  need resolving and normal gates still apply.
- If Kody ever stops responding to `github-actions[bot]` comments (app webhook
  behavior is verified by the manual-dispatch test below, not by documentation), the
  prompter fails open: PRs stay unprompted, nothing else breaks.

## Rollout and verification

1. Merge the reusable + scripts here through normal gates (Kody review once, CI).
2. Add the caller to one repo, merge, and run `workflow_dispatch` (dry-run default)
   to see decisions read-only.
3. Run one live manual dispatch on that repo and confirm Kody answers a
   `github-actions[bot]` request. This proves the app-webhook path empirically.
4. Roll the caller out to the remaining repos.

Caller template: `docs/kody-first-review.caller.yml`, copied per repo; this repo's own
caller is `.github/workflows/kody-first-review.yml`.

## Manual requests

When a person or agent requests a review by hand, prefer command-first: put
`@kody start-review` as the literal first line of the comment, ahead of any seat
header or other text. The established contract is still own-line anywhere
(`lib.mjs`, `AUTO-MERGE.md`); command-first is a precaution, not a proven
requirement. In the 2026-11-11 traces (AFC #1766 and #1781), header-first
requests (a seat header line before the command) produced no Kody check or
review receipts, while the one command-first request produced the completed
review. Causation is unverified until webhook delivery evidence confirms it, so
do not treat an own-line request that is not first-line as invalid.
