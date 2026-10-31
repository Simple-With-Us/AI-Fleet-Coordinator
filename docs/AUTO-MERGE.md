# Fleet auto-merge (reusable)

One shared auto-merge armer lives here; each repo keeps a thin caller.
Replaces the per-repo `auto-merge-prs.yml` copies that drifted apart in the
personal-account days.

## Caller template

```yaml
name: Auto-merge PRs
on:
  pull_request:
    types: [opened, synchronize, reopened, labeled, ready_for_review, unlabeled]

permissions:
  contents: write
  pull-requests: write

jobs:
  auto-merge:
    uses: Simple-With-Us/AI-Fleet-Coordinator/.github/workflows/auto-merge-reusable.yml@<PINNED_SHA>
    with:
      expected_merge_actor: "<APP-SLUG>[bot]" # exact independently verified bot login
      require_codeowners_gate: false   # ST: true (money paths need a human merge)
      # gate_script: scripts/pr-touches-codeowners-paths.sh
      # self_guard_paths: .github/CODEOWNERS,.github/workflows/auto-merge-prs.yml,scripts/pr-touches-codeowners-paths.sh
      # restrict_to_pattern: "dependabot|congress-trading-shared"   # dep-bump lanes only
      # hold_labels: "do-not-automerge,needs-human-merge"
      # kody_check_name: "Kody Code Review"   # fleet reviewer check-run name
      # review_gate: effective   # explicit opt-in: current-head review + findings
      #                          # dispositioned (also set require_current_approval: false);
      #                          # unset = legacy require_current_approval contract (strict
      #                          # APPROVED gate by default; explicit false = no review gate)
      # review_request_pattern: '@kody[ \t]+start-review'   # headless review-start probe
    secrets:
      merge_token: ${{ needs.bot-token.outputs.token }} # existing approved App-token route
```

## Rules that do not move

- Arming never uses `GITHUB_TOKEN`: a bot-attributed merge dispatches no
  workflow events, so main would get no CI and no ship run.
- No `merge_token` means a loud no-op, never a silent fallback.
- The token must match the explicit `expected_merge_actor` bot login via
  authenticated GraphQL `viewer.login`. Missing configuration, a personal
  account, another bot, or an unverifiable response prevents arming.
- Disarming uses only `GITHUB_TOKEN`, verifies `github-actions[bot]`, and reads
  back the final armed state. It never retries with `merge_token`. A refused
  or unreadable disarm is an error, not a reported success.
- The token is probed with an authenticated API call before arming; a dead
  token fails the run with "token present but rejected" instead of a bare
  401 mid-step (ST-4393).
- The gate script and CODEOWNERS are read from the base branch, never the
  PR head.
- Hold labels and ineligible PRs actively disable any already-armed merge.
- Owner rule 2026-10-21: a PR is never merged while a Kodus review is running
  on its head commit or an autofix run (PR Autofix, Claude Code Autofix, or
  Kody fleet autofix) is queued or in progress involving it.  Autofix lanes
  fire on `issue_comment` and `check_run` events, so their runs attach to the
  default branch, never the PR head branch - the gate finds them through the
  per-PR marker comments the lanes post (each carries its run id) and through
  repo-wide active runs whose `pull_requests` list the PR.  Both are classify
  gates, so tripping one also disables an already-armed merge, and every API
  lookup fails closed.  The check-run name is the `kody_check_name` input
  (default `Kody Code Review`).  Re-arming happens on the next pull_request
  event after the review or run settles.
- Gate lookups use `merge_token` when the caller passes it, falling back to
  `GITHUB_TOKEN`.  The called workflow's `GITHUB_TOKEN` is capped by the
  caller's permissions, so a caller that wants the fallback to work also
  grants its workflow `checks: read`, `actions: read` and `issues: read`.

- Review gates.  `require_current_approval` (default `true`) keeps the legacy
  strict contract: the fleet reviewer must have an APPROVED review on the
  current head, and an explicit `false` remains the opt-out with no review
  gate.  `review_gate` is unset by default and changes nothing; its only
  supported value, `effective`, opts into the gate below.  Any other value
  fails closed, and `require_current_approval: true` (the default) always wins
  over it - opting in also means setting `require_current_approval: false`.  A
  green CI run or a
  successful Kody check-run is never proof that findings were addressed (AFC
  #1207: the Kody check was green while a valid HIGH review comment was still
  open), so the gate reads the review record and thread state, not check
  conclusions, and every lookup fails closed.
  - `effective`: a full-review receipt proves a whole-code Kody review
    completed on the PR's exact head: a check-run created by the
    authenticated Kody GitHub App (BOTH `kody_app_slug`, default `kody-ai`,
    and `kody_app_id`, default `413034` - the numeric app id observed in live
    fleet check data; slugs can be renamed, ids cannot) under
    the exact `kody_check_name`, completed with conclusion SUCCESS, whose
    output carries Kody's unambiguous full-review verdict (title "Code
    Review Complete" + summary starting "Review finished successfully" -
    the license/ignore/manual skip outputs never carry it), and whose
    started_at is no earlier than the newest review request, so a run that
    began before the request and finished after it cannot answer it and no
    ack can advance the boundary (AFC #1207 ack 5724125545).  A newer
    queued/in_progress Kody run blocks.  Neutral, skipped, failure and
    error conclusions are never a source completion.  Separately, the
    fleet reviewer (`approval_reviewer`) has a review record on the current
    head - an additional consistency requirement, never provenance.  The
    check's success conclusion is never treated as proof findings were
    addressed.  No reviewer's CHANGES_REQUESTED is outstanding under
    the fleet's effective-decision contract (only a later APPROVED by the SAME
    reviewer supersedes; COMMENTED never clears a request; a dismissed review
    stops being decisive, so a dismissal unwinds that review's verdict), every
    review thread is resolved, and no review request is newer than the
    receipt (a request without a newer receipt fails closed - an ack cannot
    defeat the wait).  A new push makes the old review stale and disarms
    until re-review.
  - `approval`: the legacy strict gate - the fleet reviewer's latest decisive
    review on the current head must be APPROVED.  This is what omitted inputs
    resolve to, and `require_current_approval: true` always forces it.
  - No review gate: the pre-existing explicit opt-out is unchanged -
    `require_current_approval: false` with `review_gate` unset.  There is no
    `review_gate: off`.
  - Resolved threads are not proof of fixes: fake resolutions are reopened by
    the review-handling process.  The gate's job is the negative proof - an
    unresolved thread means findings are not dispositioned.
  Callers pin the reusable by SHA, so the gate reaches a repo when its pin is
  bumped.

## Arming boundary

Classification and arming are separate moments.  The arm job's first step
re-reads everything the classification relied on and aborts on ANY drift, API,
parse or pagination uncertainty: the PR is still open, the exact head matches
the classified head, no hold label appeared, the newest review request is
unchanged, no Kody run is queued/in_progress, the classified receipt check-run
is re-read and re-validated live - same app identity (`kody_app_slug` AND
`kody_app_id`), same head SHA, completed SUCCESS with the verdict
title/summary, and a started_at at or after the newest review request as
re-read at the boundary - and the formal review record is re-read with its
decisive state re-derived from the live list (an outstanding CHANGES_REQUESTED
aborts; under the approval gate, a dismissed or superseded head approval aborts;
under the effective gate, the current-head fleet-reviewer record being
dismissed or removed aborts, even with zero open threads),
no autofix run became active (marker comments and repo-wide run listings, both
paginated), and every review-thread page is still resolved.  The request and
thread re-checks run only where classification evaluated them: under the
legacy no-gate contract (`require_current_approval: false`, `review_gate`
unset) classification never reads requests or threads, so the boundary skips
those re-checks too.  An abort also
runs `gh pr merge --disable-auto` best-effort, and the disarm job runs on arm
failure as well as on classification rejection, so a failed arm never leaves
a previously armed auto-merge in place.  Every merge invocation pins the
expected head with `gh pr merge --match-head-commit` (verified against the
installed CLI's help), closing the seconds between the boundary check and the
arm command; GitHub refuses the arm if the head moved.

## Merge queues

GitHub refuses `--delete-branch` when a repository has a merge queue enabled:
the flag asks GitHub to delete the head branch when the pull request merges,
and a merge queue cannot accept that request on the auto-merge.  The armer
arms again without the flag when the queue is what refused it, so a repo that
turns a queue on keeps arming without a caller change.  Branch cleanup in a
queued repo is the repository-level "Automatically delete head branches"
setting (API `delete_branch_on_merge`) under Settings > General > Pull
Requests — the merge queue's own panel exposes merge method, build
concurrency, status check timeout and merge limits, not a branch-deletion
control — so that setting must be enabled for a merged head branch to
disappear; the armer never sets it, so a queue repo has to have it on
already.  A caller can also turn the flag off directly with the `delete_branch`
input, which is what Socratic-Trade does (ST-4566).

Contract: `python3 scripts/test_auto_merge_reusable.py` runs the arm step's
own shell against a fake `gh`, so the retry is exercised rather than
described.

## Credentials

Before adopting this workflow revision, qualify the caller's existing approved
App-token route and pass its exact bot login as `expected_merge_actor`. The
illustrative `bot-token` job above is not provided by this workflow. Do not
replace it with a personal PAT, create secrets, grant permissions, or copy a
short-lived token into a repository secret to make adoption pass.

Migration order: establish the approved bot mint route and its repository scope,
verify the token's GraphQL principal and write capabilities without an unintended
merge, pair caller input/token wiring with the pinned reusable revision, then
observe normal classification, review/check gates and queue operation. Validate
repository Actions-token disarm capability separately. Do not roll out a new pin
before caller readiness is established; missing inputs fail closed and would
interrupt unattended merging. Old pinned revisions do not acquire this guard.

This is enforcement for this reusable armer and disarmer only. Other workflows,
local clients, owner credentials and account settings remain separate surfaces.
`python3 scripts/test_merge_principal_guard.py` executes both actual shell paths
against controlled identity and state responses; it does not prove live caller
credentials or permissions.
