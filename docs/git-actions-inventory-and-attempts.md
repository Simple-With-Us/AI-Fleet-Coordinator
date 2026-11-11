# Actions inventory and exact attempts

Eight bounded read-only own-App capabilities fill missing Actions metadata paths:

- `wf_workflow_get`: numeric workflow ID, path and state.  Path is observed metadata, not a source-file authorization.
- `wf_artifact_get`: exact artifact ID, name, byte count, expired flag and nullable run/head association.  No download URL or archive contents.
- `wf_artifacts_page`: repository artifact metadata pages.
- `wf_caches_page`: repository cache ID/key/ref/size pages, not cache contents or download URLs.
- `wf_cache_usage`: exact repository-correlated active cache count/bytes.
- `wf_run_attempt_get`: exact run ID/head SHA/attempt, status and conclusion.
- `wf_run_attempt_jobs`: paged exact attempt jobs with run/head/attempt identity checked on every row.  No runner identities, labels, step text or logs.
- `wf_run_pending_deployments`: environment ID/name, wait timer and current-App approval-capability observation.  Exact current run/head/attempt is checked before and after.  It does not approve or reject anything and does not imply owner authority.

All pages are mutable observations, not atomic snapshots.  Full pages conservatively say `has_more`; that is not proof a next page exists.  IDs and counts are safe integers; pages1-1000 and1-50items only; projected receipts stay bounded.  Malformed identities, duplicate IDs, wrong heads/attempts, inconsistent completion status and credential-shaped metadata refuse.  Unknown artifact run association stays null, never invented.

All eight require only existing `actions:read` plus `metadata:read`, minted through the caller's own App after seat/repository/action and ceiling checks.  No App permission, identity, policy or runtime configuration widening.  No owner token except the existing PR-creation exception.  Runtime activation remains separate from source qualification.  Actual mutations, deployments, approvals, reruns and cache deletion are outside this read batch.

Official endpoint and schema sources:

- https://docs.github.com/en/rest/actions/workflow-runs?apiVersion=2022-11-28
- https://docs.github.com/en/rest/actions/workflow-jobs?apiVersion=2022-11-28
- https://docs.github.com/en/rest/actions/artifacts?apiVersion=2022-11-28
- https://docs.github.com/en/rest/actions/cache?apiVersion=2022-11-28
- https://docs.github.com/en/rest/actions/workflows?apiVersion=2022-11-28
- https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.json

Fixtures cover fixed paths/projection, malformed and cross-identity input/output, duplicates, page limits, secret-shaped names, exact attempt correlation, before/after run observations, own-App minimal permission and actual relay refusal before mint.  Hosted validators independently check receipt shape, identity and page arithmetic.
