# PR Autofix

When a review bot comments on an open pull request, this workflow asks Claude Code (through the MiniMax Anthropic endpoint) to make the smallest change that answers the comment, and pushes it to the PR branch as `autofix: address review comment [n/max]`.  It is one reusable workflow hosted here, with a thin caller in each repo.

Origin:  BotFleet's inline `autofix.yml`, which replaced `urcades/pr-autofix` (that action failed on every run with `Invalid format '0'`, see the header of the reusable file).  Board item 04b963b7.

## Layout

- `.github/workflows/pr-autofix-reusable.yml` is the shared workflow (`on: workflow_call`).  All the logic lives here.
- `.github/pr-autofix-trusted.yml` on the **default branch** pins model, turn budget, iteration cap, and the verify shell for this repo.
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
  cancel-in-progress: false

jobs:
  autofix:
    if: >
      (github.event.issue.pull_request || github.event.pull_request) &&
      (github.event.issue.state == 'open' || github.event.pull_request.state == 'open') &&
      (
        (github.event_name == 'pull_request_review' && contains(fromJSON('["bot", "Bot"]'), github.event.review.user.type)) ||
        (github.event_name != 'pull_request_review' && contains(fromJSON('["bot", "Bot"]'), github.event.comment.user.type))
      )
    permissions:
      contents: write
      pull-requests: read
      issues: write
      actions: read
    uses: Simple-With-Us/AI-Fleet-Coordinator/.github/workflows/pr-autofix-reusable.yml@<sha>
    secrets:
      MINIMAX_API_KEY: ${{ secrets.MINIMAX_API_KEY }}
      META_MODEL_API_TOKEN: ${{ secrets.META_MODEL_API_TOKEN }}
      USAGE_INGEST_TOKEN: ${{ secrets.USAGE_INGEST_TOKEN }}
      AUTOFIX_GITHUB_APP_PRIVATE_KEY: ${{ secrets.SWU_AUTOFIX_GITHUB_APP_PRIVATE_KEY }}
```

The `concurrency` group is shared on purpose by every autofix caller in a repo (another autofix bot uses the same group name), so pushes to one PR branch are serialized.  The group belongs in the caller only:  the reusable job must not repeat it, because a job that asks for the group its own run already holds is cancelled as a deadlock before any step runs.  That is what stopped every PR Autofix run from `2e671004` (#520) until the job-level copy was removed, so a caller pinned to any commit from `2e671004` up to that fix (BotFleet and DealDex were on `3fac4c3c`) needs a repin.  `cancel-in-progress` is `false` so one bot never cancels the other mid-push;  keep the group name and that setting exactly as shown.

`MINIMAX_API_KEY` is an org secret visible to every repo.  Pair it with org secret `SWU_AUTOFIX_GITHUB_APP_PRIVATE_KEY` and org/repo variable `SWU_AUTOFIX_GITHUB_APP_ID` so autofix pushes re-trigger CI (see below).  The job-level `permissions:` block caps the token the reusable receives, so the caller must grant `issues: write` for the run-marker comment and `actions: read` for the `meter` job's usage-artifact download;  a caller missing them fails at startup, when GitHub validates the called workflow's requested scopes against the caller's grants.  `META_MODEL_API_TOKEN` is only consumed when `SWU_AUTOFIX_MODEL_BASE_URL` points at `api.meta.ai`, and `USAGE_INGEST_TOKEN` only feeds the usage report (the run is unchanged without it), but the reusable can only read a secret the caller maps, so the template passes both through.  The caller-level `if:` keeps a human comment from starting a runner at all;  the reusable job repeats the same checks and also requires the PR to be open.

## Inputs

All optional, passed with `with:` on the calling job.

| Input | Default | Meaning |
|---|---|---|
| `allowed_bots` | `kody-ai[bot],chatgpt-codex-connector[bot],cursor[bot],claude[bot]` | Comma-separated logins whose comments may start a run. |
| `max_iterations` | `5` | Most autofix commits on one PR (overridden by trusted file on default branch). |
| `claude_code_version` | `2.1.295` | Version of `@anthropic-ai/claude-code` to install. |
| `model` | `MiniMax-M3.1-Flash` | MiniMax model id (overridden by trusted file on default branch). |
| `max_turns` | `60` | Claude Code turn budget (overridden by trusted file on default branch). |
| `verify_command` | empty | Shell run after the model and before push; trusted file may supply this instead. |

The allowlist is authoritative.  A comment from a bot that is not listed never starts a run, whatever the comment says.  (BotFleet's original also let any comment containing `error:` or `warning:` through, which let an unrelated deploy or error-tracking bot start a paid run;  that bypass is gone.)

## Trusted policy (default branch)

Add `.github/pr-autofix-trusted.yml` on the **default branch** of each repo that uses PR Autofix:

```yaml
model: MiniMax-M3.1-Flash
max_turns: 60
max_iterations: 5
verify_command: |
  npm test
```

The workflow loads this file through the GitHub API at `ref=<default branch>` before checking out the PR head.  Model, turn budget, iteration cap, and verify commands never come from the PR branch.

## Per-Repo bot allowlist

A `.autofix.yml` at the root of the PR branch may override **only** `allowed_bots`:

```yaml
allowed_bots:
  - kody-ai[bot]
  - chatgpt-codex-connector[bot]
```

Fork PRs are skipped, so this file is not a trust boundary for secrets;  it must not change model, turns, iteration cap, or verify commands.

## Fail-closed push gate

The workflow refuses to push when:

- Claude Code exits unsuccessfully or diagnostics report a non-`success` result.
- The scoped `verify_command` fails.
- The PR `headRefOid` changed after the run started (another writer landed commits).

Sanitized diagnostics (no raw transcripts) are uploaded as a short-lived artifact on every attempted fix, matching the fleet Claude Code autofix reusable.

## CI on autofix commits

Pushes made with the workflow `GITHUB_TOKEN` do not trigger other workflows.  When `SWU_AUTOFIX_GITHUB_APP_PRIVATE_KEY` and `SWU_AUTOFIX_GITHUB_APP_ID` are configured, the reusable workflow mints a GitHub App installation token and pushes with that instead so required checks run on the new head.  The mint step is gated on the org variable `SWU_AUTOFIX_GITHUB_APP_ID`, not on the secret, because GitHub rejects workflow startup when a step `if` references `secrets`.  A preceding shell step reads the private key from the environment and skips mint when the variable is set but the secret is empty, so the run falls back to `GITHUB_TOKEN` push with a notice.  Without the App, the push still lands but logs a warning that CI must be re-triggered manually.

## Usage Metering

Each attempted fix reports its token usage to Usage Monitor (UM-1593), so the fleet can see what autofix spends.  The model job keeps only the counters of Claude Code's final result message (tokens, model, duration, turns) and hands them to a separate `meter` job as a one-day artifact.  The `meter` job posts one v2 usage event (`producerId: ci-autofix`, `harness: claude-code-ci`, repo, PR number, run id) through the pinned fleet reporting action.  The ingest credential exists only in that job, because the model job runs Claude Code with Bash and could rewrite any file on its runner.  Reporting is `continue-on-error`, exits 0 on every failure, and also covers runs whose push the gate then refuses.

The reusable declares an optional secret, `USAGE_INGEST_TOKEN`, which callers forward like `MINIMAX_API_KEY`:  `USAGE_INGEST_TOKEN: ${{ secrets.USAGE_INGEST_TOKEN }}`.  Without it the `meter` job does nothing.  A caller must pin a SHA that declares the secret before it forwards it, because GitHub rejects a secret the called workflow does not declare.

## What It Skips

- PRs that are closed, merged, or from a fork (`isCrossRepository`), since there is no branch to push to.
- Empty comment bodies, authors not on the allowlist, and PRs already at the iteration cap.  The cap counts commits on the branch whose subject starts with `autofix:` that the default branch does not have.
- Changes under `.github/workflows`, which `GITHUB_TOKEN` may not push.  They are left out of the commit so the rest of the fix still lands.

The comment text only ever goes to a file, never to a step output or an environment variable.

## Fleet Autofix Coordination

The fleet runs three autofix bots (this PR Autofix, the Claude Code Autofix reusable, and Kody), and every caller uses the same workflow-level concurrency group, `pr-autofix-<owner/repo>-<pr>` with `cancel-in-progress: false`, so only one of them pushes to a PR at a time:

- PR Autofix, this workflow.
- Claude Code Autofix, the Claude Code Autofix reusable (anthropics/claude-code-action on MiniMax; the filename is left over from its DeepSeek "Codex Autofix" days).
- Kody autofix, the Kody reusable (MiniMax M3.1 Flash (`MiniMax-M3.1-Flash`) through MiniMax’s direct OpenAI-compatible endpoint, `https://api.minimax.io/v1`, using `MINIMAX_API_KEY`, enabled fleet-wide 2026-10-11 by the org variable `SWU_KODY_AUTOFIX_ENABLED`; board 99e559ea).

GitHub keeps one running and one pending run per group, and a newer pending run replaces the older one.

Codex's own fix feature runs in Codex's cloud, outside this queue.  Agents never ask Codex to fix a PR unless Jay agrees first (FLEET-PROTOCOL.md, [Landing Work](../FLEET-PROTOCOL.md#landing-work-commit-pr-review-merge-deploy)).  Asking Codex to *review* a PR stays allowed;  review findings are fixed by these fleet autofix bots or by the seat that owns the PR.
