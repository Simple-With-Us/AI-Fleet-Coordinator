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

## Merge queues

GitHub refuses `--delete-branch` when a repository has a merge queue enabled:
the flag flips the repository's delete-branch-on-merge setting, and a queue
cannot have both.  The armer arms again without the flag when the queue is
what refused it, so a repo that turns a queue on keeps arming without a
caller change.  The queue's own "delete head branches" setting owns branch
cleanup from that point on.  A caller can also turn the flag off directly
with the `delete_branch` input, which is what Socratic-Trade does (ST-4566).

Contract: `python3 scripts/test_auto_merge_reusable.py` runs the arm step's
own shell against a fake `gh`, so the retry is exercised rather than
described.

## Credentials

Long-term identity is a single GitHub App installed org-wide (owner action,
not yet created); callers will mint installation tokens per run. Interim is
the org `GH_PAT` secret, whose rotation is owned by Infisical automation.
