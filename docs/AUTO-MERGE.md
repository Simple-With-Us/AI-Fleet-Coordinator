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

## Credentials

Long-term identity is a single GitHub App installed org-wide (owner action,
not yet created); callers will mint installation tokens per run. Interim is
the org `GH_PAT` secret, whose rotation is owned by Infisical automation.
