# Kody defer

Agree with a Kody review finding and track the fix later, without losing the thread.  A person with write access replies `/defer` on the review comment.  The workflow opens a GitHub issue, replies `Deferred to #N.` on the thread, and resolves the thread when the token can.

The command is ignored unless it is its own line.  `/deferred`, a mid-line mention, and a `/defer` inside a code fence do not count.  Bots cannot defer.  Fork pull requests receive a read-only `GITHUB_TOKEN`, so this flow cannot write there.

## Usage

On the Kody thread, comment:

```
/defer
```

or, with a reason:

```
/defer the Mac cutover is still in progress
```

`/defer:` is also accepted.  The reason may be empty.

Critical findings are refused.  The reply is exactly:

```
Critical findings can't be deferred.  Fix this in the PR.
```

To also refuse high findings, pass `blocked-severities: critical,high`.  The refusal uses the same sentence with the severity name capitalized, for example `High findings can't be deferred.  Fix this in the PR.`

## Consumer workflow

Put this in `.github/workflows/kody-defer.yml` of the repo that receives Kody reviews.  A copy lives at `github-workflows-template/workflows/kody-defer.yml`.

```yaml
name: Kody defer

on:
  pull_request_review_comment:
    types: [created]

permissions:
  contents: read
  issues: write
  pull-requests: write

jobs:
  defer:
    if: contains(github.event.comment.body, '/defer')
    uses: Simple-With-Us/AI-Fleet-Coordinator/.github/workflows/kody-defer.yml@main
    with:
      blocked-severities: critical
```

This repository calls the same reusable workflow from `.github/workflows/kody-defer-caller.yml`.

Optional inputs:

| Input | Default | Meaning |
|---|---|---|
| `blocked-severities` | `critical` | Comma-separated list.  `critical,high` refuses both. |
| `tooling-ref` | `main` | Git ref of AI-Fleet-Coordinator that supplies `scripts/kody-defer`. |
| `dry-run` | `false` | Log the writes and do not perform them. |

The job checks out `scripts/kody-defer` from AI-Fleet-Coordinator at `tooling-ref`.  It does not run the pull request's copy of the script.  Comment text is never placed on a `run:` line.  `defer.mjs` reads `GITHUB_EVENT_PATH`.

## Labels

| Label | Color | Description |
|---|---|---|
| `kody-deferred` | `#D4A72C` | Agreed Kody finding, fix later |
| `kody-critical` | `#B60205` | Kody finding at critical severity |
| `kody-high` | `#D93F0B` | Kody finding at high severity |
| `kody-medium` | `#FBCA04` | Kody finding at medium severity |
| `kody-low` | `#C5DEF5` | Kody finding at low severity |

The issue gets `kody-deferred` plus `kody-<severity>` when the Kody badge has a severity.  The badge that counts is `severity_level-critical`, `severity_level-high`, `severity_level-medium`, or `severity_level-low`.

## What the issue contains

The title starts with `Deferred review: `.  The body keeps the cleaned finding (badge images and the Prompt for LLM `<details>` block removed), the file location as a blob permalink at the PR head SHA, the pull request link, the thread permalink, who deferred it, the reason (or `(none given)`), the head SHA, and the severity.

A hidden marker lets a later sweep find the file:

```
<!-- kody-defer thread=<threadId> root=<rootCommentId> path=<path> -->
```

## Token scope

`resolveReviewThread` needs a token that can resolve review threads.  The default `GITHUB_TOKEN` with `pull-requests: write` can do that in many repositories, and some setups still reject the mutation.  When the mutation fails, the workflow leaves the issue in place, keeps the `Deferred to #N.` reply, and adds a second reply that a person must resolve the thread manually.  It exits successfully.

Pull requests opened from forks get a read-only token.  The flow cannot create the issue or reply there.  Do not switch this workflow to `pull_request_target` to work around that.

## Idempotency

A thread that already has a `Deferred to #<n>` reply is left alone.  If that reply is missing but an open or closed `kody-deferred` issue already contains the thread marker, the workflow replies and resolves using that issue number instead of opening a second issue.  A repeated refusal is not posted again when the same refusal text already appears after the `/defer` comment.

Concurrency is one run at a time per pull request and root comment, so two deliveries of the same comment serialize.
