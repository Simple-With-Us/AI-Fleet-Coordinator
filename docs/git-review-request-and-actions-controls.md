# Pending review requests and Actions control coverage

Source capabilities, not permission to perform any particular mutation and not a runtime release.

## Pending requests

Jay clarified "Pending" on October 29: remove a pending request for review, not dismiss a review that has been submitted. Existing user-only `wf_reviewers_request`/`wf_reviewers_remove` remain compatible. The new contracts add exact expected PR-head identity, team support, bounded projected receipts and re-request support:

- `wf_review_requests_add`
- `wf_review_requests_remove`
- `wf_review_requests_rerequest`

These accept explicit user logins and/or team slugs, never arbitrary selectors. Add and re-request notify people. Remove uses only the requested-reviewers endpoint, never review deletion or dismissal. The PR number/head is checked before the write and against its response, but GitHub offers no atomic head precondition. An unverifiable post-write receipt means the write may have happened: reconcile, do not retry blindly. Only requested names and correlated identity are returned, not the upstream PR body or unrelated people. Tool availability never waives required review or allows removing a review gate.

## Actions gaps

- `wf_job_get`: exact job/run/head/attempt metadata, not runner metadata or logs.
- `wf_job_rerun`: rerun one completed job and its dependent jobs.
- `wf_run_rerun_all`: rerun a completed run, distinct from the existing failed-jobs-only rerun.
- `wf_run_force_cancel`: accepted force-cancel request for an unfinished run.
- `wf_run_logs_delete`: delete logs only for a completed run.

The Actions contracts require an exact run ID, head SHA and attempt; job operations additionally require the exact job ID. Metadata pre-reads are not atomic, and acceptance is not completion. Reruns keep debug logging off; no debugger option is exposed. Exact empty response status is required (201 rerun, 202 force cancel, 204 log deletion). No real jobs, requests or logs were mutated as test canaries. Fixture tests verify the fixed paths, identity fences, receipt projection and status contract.

All use the seat's own App and existing repository/permission ceilings: pull_requests write for review requests, actions read for job get, actions write for Actions mutations. Permission failures do not trigger owner-token fallback or widen App permissions. Runtime allowlists, deployment/recovery qualification and operation-specific authority remain separate.

## Coverage continuation

This is a substantive follow-on batch, not the whole GitHub API. Existing main already has issue/review writes, deployments, environments, releases/assets, milestones, files/branches/tags, workflow controls and merge queue; separate open slices cover variables, issue history, binary Git reads and three new comment/reviewer reads. Future inventory must keep known source, reviewed/merged source, live tools and permission-blocked endpoints separate. Repository administration, secrets and organization-level operations must never gain App permission by inference from API coverage goals.

Contracts checked:
- https://docs.github.com/en/rest/pulls/review-requests?apiVersion=2022-11-28
- https://docs.github.com/en/rest/actions/workflow-jobs?apiVersion=2022-11-28
- https://docs.github.com/en/rest/actions/workflow-runs?apiVersion=2022-11-28
