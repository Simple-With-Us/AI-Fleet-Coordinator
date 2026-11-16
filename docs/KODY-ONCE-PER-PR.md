# One Kody review per pull request (AFC-735)

The `Kody Code Review` check is not a GitHub Actions workflow.  It is a check run created by the Kodus GitHub App (`kody-ai`, app id 413034).  Nothing in this repo, and nothing in the caller workflows of Socratic-Trade, Congress.Trade, Usage-Monitor, CodeCaps, FleetLink, ContactLogo, or BotFleet, starts that check.  Changing a reusable workflow cannot stop the app from reviewing on `synchronize`.

Measured on [Socratic-Trade#5131](https://github.com/Simple-With-Us/Socratic-Trade/pull/5131) on 2026-11-12.  The pull request had 36 commits.  Twenty-seven of them carried a completed `Kody Code Review` check with conclusion `success`, title `Code Review Complete`, and a summary starting `Review finished successfully`.  Zero issue comments from anyone other than `kody-ai[bot]` contained `@kody start-review`.  One commit's check was conclusion `skipped`, title `Code Review Skipped`, summary `No New Commits (No changes detected since last review)`.  That is Kodus skipping an empty diff, and it is still the same app.

## The setting to change

Open [Code review → General at global scope](https://app.kodus.io/settings/code-review/global/general).  That is the URL Kody linked from its own configuration block on Socratic-Trade#5131.  In the sidebar, the scope picker at the top of the Code review group must say Global, then open General.

Set these three controls.  The keys are the `kodus-config.yml` names from the [Kodus general settings reference](https://docs.kodus.io/en/how_to_use/code_review/configs/general).

1. Leave Enable Automated Code Review on.  Key `automatedReviewActive: true`.  Kodus documents this as the initial review only.
2. Set Review Cadence to Manual.  Key `reviewCadence.type: manual`.  The other values are `automatic` (review every push; this is the live behavior) and `auto_pause` (still reviews pushes, and only pauses during a burst).  Manual means a follow-up review runs when someone comments `@kody start-review`.
3. Turn off Running on Draft Pull Requests.  Key `runOnDraft: false`.  Kodus then skips drafts until the pull request is marked ready.

Do not flip Review Cadence to Auto-pause.  That still spends a review on ordinary pushes.

## Why a committed config file does not do this

Kodus ignores `kodus-config.yml` until kodus Config File Overrides Web Preferences is on.  The key is `kodusConfigFileOverridesWebPreferences`, and it defaults to `false`.  The toggle exists only at repository and directory scope, not at global scope.  Committing the file, or setting the key inside the file, does not turn the toggle on.

Checked 2026-11-12. These files do not set `reviewCadence` and do not set the override key to true:

- `Simple-With-Us/Kodus-Config` `kodus-config.yml` and `AI-Fleet-Coordinator/kodus-config.yml`
- `Simple-With-Us/Fleet-OPS` `kodus-config.yml`
- `Simple-With-Us/BotFleet` `kodus-config.yml`
- No `kodus-config.yml` on the default branch of AI-Fleet-Coordinator, Socratic-Trade, Congress.Trade, Usage-Monitor, CodeCaps, FleetLink, or ContactLogo

Centralized config is off.  The Kodus-Config README records OPS-973: `kodus config centralized status` reports `enabled: false` and no repository, so a merge in that repo does not reach Kodus.  This run has no Kodus CLI login, and it does not add a secret to get one.

## What Manual cadence does not do

Manual cadence does not retry a failed or errored review by itself.  It does not watch a `kody-rereview` label.  The explicit request Kodus documents is the comment `@kody start-review`.

`scripts/kody-once/decide.mjs` is the decision table for the behavior this issue asked for, and `decide.test.mjs` covers it.  A first ready pull request (`opened`, `reopened`, or `ready_for_review`, not a draft) reviews.  A later `synchronize` skips when a prior check is a full receipt.  The last completed attempt being a failure, a timeout, a cancellation, or a success that lacks the receipt reviews again.  A `@kody start-review` line newer than the latest receipt, or a `labeled` event for `kody-rereview`, reviews again.  A label that is merely still present on a later push does not.  An in-flight check does not stack another request.

That module is not called from a workflow.  Posting `@kody start-review` on failure while cadence is still Automatic would add a second review on top of the one the app already starts.  Wire a caller only after Review Cadence is Manual.

## Required checks

`Kody Code Review` is not a required status check.  Read on 2026-11-12 from each repo's active `default-main-protection` ruleset:

| Repo | Required checks |
| --- | --- |
| AI-Fleet-Coordinator | `test`, `gitleaks` |
| Socratic-Trade | `verify`, `gitleaks` |
| Congress.Trade | `typecheck + test`, `gitleaks`, `xcodebuild (unsigned)` |
| Usage-Monitor | `verify`, `gitleaks` |
| CodeCaps | `gitleaks`, `iOS companion compile` |
| FleetLink | `test` |
| ContactLogo | `kit`, `web` |
| BotFleet | `typecheck + test (ubuntu-latest)`, `control-plane check + workerd tests + dry run`, `Classify CI scope`, `typecheck + test (macos-latest)`, `Swift tests + iOS build`, `lint` |

A later Kody check that is skipped or absent does not leave those checks pending.  This change does not edit a ruleset.  Do not add `Kody Code Review` as a required check while follow-up runs complete as `skipped`: a skipped conclusion does not satisfy a required status check, and a check run created by GitHub Actions under the same name is a different app.  The auto-merge gate already ignores a same-named check from any app other than `kody-ai`.

## Workflows that are not the trigger

These jobs exist and do something else.  `kody-fleet-autofix` proposes fixes after a review.  `kody-first-review` runs only in AI-Fleet-Coordinator, on an hourly schedule, and posts `@kody start-review` once for a pull request Kody has never answered.  It does not run on `synchronize`, and the other seven repos do not carry that caller.  `kody-defer` records a defer command.  None of them create the `Kody Code Review` check.
