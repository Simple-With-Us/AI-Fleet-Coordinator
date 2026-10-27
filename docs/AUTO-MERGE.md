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
      require_codeowners_gate: false   # ST: true (money paths need a human merge)
      # gate_script: scripts/pr-touches-codeowners-paths.sh
      # self_guard_paths: .github/CODEOWNERS,.github/workflows/auto-merge-prs.yml,scripts/pr-touches-codeowners-paths.sh
      # restrict_to_pattern: "dependabot|congress-trading-shared"   # dep-bump lanes only
      # hold_labels: "do-not-automerge,needs-human-merge"
      # kody_check_name: "Kody Code Review"   # fleet reviewer check-run name
    secrets:
      merge_token: ${{ secrets.GH_PAT || secrets.SHEPHERD_TOKEN }}
```

## Rules that do not move

- Arming never uses `GITHUB_TOKEN`: a bot-attributed merge dispatches no
  workflow events, so main would get no CI and no ship run.
- No `merge_token` means a loud no-op, never a silent fallback.
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

- Current-head approval: by default (`require_current_approval: true`) a PR is
  armed only while `approval_reviewer` (default `kody-ai[bot]`) has an APPROVED
  review on the PR's current head commit.  A skipped review check, a COMMENTED
  review, an approval on an older commit, a later CHANGES_REQUESTED, or a failed
  lookup all disarm.  A new push makes the old approval stale.  Dependency-bump
  lanes (`restrict_to_pattern` set) are exempt.  Callers pin the reusable by SHA,
  so the gate reaches a repo when its pin is bumped.

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

Long-term identity is a single GitHub App installed org-wide (owner action,
not yet created); callers will mint installation tokens per run. Interim is
the org `GH_PAT` secret, whose rotation is owned by Infisical automation.
