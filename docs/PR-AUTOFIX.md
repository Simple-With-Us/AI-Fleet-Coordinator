# PR Autofix

When a review bot comments on an open pull request, this workflow asks Claude Code (through the MiniMax Anthropic endpoint) to make the smallest change that answers the comment, and pushes it to the PR branch as `autofix: address review comment [n/max]`.  It is one reusable workflow hosted here, with a thin caller in each repo.

Origin:  BotFleet's inline `autofix.yml`, which replaced `urcades/pr-autofix` (that action failed on every run with `Invalid format '0'`, see the header of the reusable file).  Board item 04b963b7.

## Layout

- `.github/workflows/pr-autofix-reusable.yml` is the shared workflow (`on: workflow_call`).  All the logic lives here.
- `.github/workflows/autofix.yml` is this repo's caller, using the local `./` path.
- Every other fleet repo adds the caller below.  This repository is public, so private repos can call the reusable workflow with no access setting.

## Caller For Other Repos

Save as `.github/workflows/autofix.yml` and pin `<sha>` to a merge commit on this repo's `main`:

```yaml
name: PR Autofix

on:
  issue_comment:
    types: [created]
  pull_request_review:
    types: [submitted]
  pull_request_review_comment:
    types: [created]

permissions: {}

concurrency:
  group: pr-autofix-${{ github.repository }}-${{ github.event.issue.number || github.event.pull_request.number }}
  cancel-in-progress: true

jobs:
  autofix:
    if: >
      (github.event.issue.pull_request || github.event.pull_request) &&
      contains(fromJSON('["bot", "Bot"]'), github.event.comment.user.type || github.event.review.user.type || '')
    permissions:
      contents: write
      pull-requests: read
    uses: Simple-With-Us/AI-Fleet-Coordinator/.github/workflows/pr-autofix-reusable.yml@<sha>
    secrets:
      MINIMAX_API_KEY: ${{ secrets.MINIMAX_API_KEY }}
```

The `concurrency` group is shared on purpose by every autofix caller in a repo (another autofix bot uses the same group name), so pushes to one PR branch are serialized.  `cancel-in-progress` is `false` so one bot never cancels the other mid-push;  keep the group name and that setting exactly as shown.

`MINIMAX_API_KEY` is an org secret visible to every repo.  The caller-level `if:` keeps a human comment from starting a runner at all;  the reusable job repeats the same checks and also requires the PR to be open.

## Inputs

All optional, passed with `with:` on the calling job.

| Input | Default | Meaning |
|---|---|---|
| `allowed_bots` | `kody-ai[bot],chatgpt-codex-connector[bot],cursor[bot],claude[bot]` | Comma-separated logins whose comments may start a run. |
| `max_iterations` | `5` | Most autofix commits on one PR. |
| `claude_code_version` | `2.1.295` | Version of `@anthropic-ai/claude-code` to install. |

The allowlist is authoritative.  A comment from a bot that is not listed never starts a run, whatever the comment says.  (BotFleet's original also let any comment containing `error:` or `warning:` through, which let an unrelated deploy or error-tracking bot start a paid run;  that bypass is gone.)

## Per-Repo Override

A `.autofix.yml` at the root of the PR branch overrides the inputs:

```yaml
allowed_bots:
  - kody-ai[bot]
  - chatgpt-codex-connector[bot]
max_iterations: 3
```

Only a non-empty `allowed_bots` list or a numeric `max_iterations` replaces the input.  The file is read from the checked-out PR branch, so it is only trusted because forks are skipped.

## What It Skips

- PRs that are closed, merged, or from a fork (`isCrossRepository`), since there is no branch to push to.
- Empty comment bodies, authors not on the allowlist, and PRs already at the iteration cap.  The cap counts commits on the branch whose subject starts with `autofix:` that the default branch does not have.
- Changes under `.github/workflows`, which `GITHUB_TOKEN` may not push.  They are left out of the commit so the rest of the fix still lands.

The comment text only ever goes to a file, never to a step output or an environment variable.

## Known Side Effect: No CI On The Autofix Push

A push made with `GITHUB_TOKEN` does not trigger other workflows.  After an autofix commit, the new head shows no checks, or checks stuck at `action_required`, until a human or an agent pushes again.  Agents must re-push (an empty commit is enough) or re-run the checks before arming auto-merge, and must not read "no checks" on an autofix head as green.

## Not Covered Here

This does not touch the Kody DeepSeek reusable in `congress-trading-shared` (board 99e559ea) or `codex-autofix`.  Those keep running as they are.
