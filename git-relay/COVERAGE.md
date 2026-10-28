# Fleet GitHub command coverage

This is an implementation checklist, not a claim of complete rollout or of working
accounts. Source review and focused tests qualify code. No new account setup or
live verification is required to make this checklist honest.

## Existing and in flight

- Original PR open/edit/comment/reply/thread resolution/merge/workflow dispatch.
- Workflow source: PR list/files, contents/tree/branches/commits/code search,
  run/job/check reads, branch create, commit-bound reviews and inline comments,
  update branch, rerun/cancel/approve runs, single/multi-file commits, guarded merge.
- #1032: issue list/get/create/update/comment. Reconciled with release assets;
  current review and CI still pending.
- #1035: qualified release asset metadata list/get and small UTF-8 new uploads.
  This does not cover opaque binary, large downloads, replacement or deletion.
- #1041: PR get/reviews/inline-comment reads, issue comment reads, commit get,
  compare, branch get, repository get, labels, workflows, run get and artifact list.
- #1042: replace issue labels, request/remove reviewers, create/update releases.
- #1043: synthetic persistent-bot identity/configuration compatibility, separate
  from engine/login contexts. No actual App or account readiness claim.

## Remaining potentially relevant command families

- PR/issue comment edit and delete; review edit/dismiss; review thread listing.
- PR draft/ready, auto-merge enable/disable, merge queue and cross-repo search.
- Issue assignment, labels metadata, milestones, close/reopen/lock/transfer,
  issue relations and pull-request conversion where supported.
- Branch update/delete, tag refs, git blobs/trees/commits, compare/patch/diff,
  file deletion and rename, workflow-file changes with explicit required scope.
- Actions job/run log contents, artifacts download/delete, run deletion,
  workflow enable/disable and dispatch inputs.
- Release list/get/delete, full binary/large asset upload/download/delete/update,
  tag handling and generated release notes.
- Commit statuses, check run create/update, deployments and deployment statuses.
- Repository list/search/create/edit/archive, branch protection and rulesets,
  collaborators/teams, webhooks and relevant automation settings.
- Repository variables/secrets and environments: code must support authorized
  configuration without returning secret values or widening credentials.
- Projects/discussions/packages only where fleet tooling or workflow evidence
  makes them relevant; do not add unrelated GitHub services as filler.

## Evidence anchors

The checked-in `.github/workflows/auto-merge-reusable.yml` uses check runs,
reviews, issue comments, compare, CODEOWNERS, viewer lookup and auto-merge.
`scripts/hold-guard/guard_run.sh` uses labels and check-run create/update.
`scripts/ios-fleet/publish-ios-versions.sh` uses refs, git trees/commits, compare,
file writes, PR creation/edit and auto-merge. `scripts/pr_sweep/rules.md` records
review-thread reads, dismissal, reruns and normal merge. These show relevance,
not permission to execute the recorded actions now.

Every family keeps own-App identity, explicit repository/action/permission
ceilings, bounded inputs and durable write outcomes. UNKNOWN writes remain held
and are never replayed automatically. Implementing an operation is not approval
to run destructive operations, change credentials, spend money or bypass review.
