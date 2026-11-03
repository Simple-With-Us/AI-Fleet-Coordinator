# Repository Actions settings and teams GitHub commands

Default-off own-App reads and writes for repository-level GitHub Actions
settings, plus the repository team list, on the existing workflow-verb
custody path.  Source contract:
https://docs.github.com/en/rest/actions/permissions (repository sections)
and https://docs.github.com/en/rest/repos/repos (List repository teams).

Teams read.  `wf_repo_teams` lists a bounded page of teams with access to
the repository, projected to id, slug, name, permission (admin, maintain,
push, triage, pull) and privacy (closed, secret or null);  any deviation
fails the read closed.

Actions settings.  Five get/set pairs, all requiring the administration
ceiling entry (read or write per verb):

- `wf_actions_permissions_get` / `wf_actions_permissions_set`:  whether
  Actions is enabled, the allowed-actions policy (all, local_only,
  selected) and whether full-length SHA pinning is required.
- `wf_actions_access_get` / `wf_actions_access_set`:  the access level
  for workflows outside the repository (none, user, organization).
- `wf_actions_retention_get` / `wf_actions_retention_set`:  artifact and
  log retention days.  A requested value above the upstream
  maximum_allowed_days (read first) is refused before any write.
- `wf_actions_fork_approval_get` / `wf_actions_fork_approval_set`:  the
  fork-PR contributor approval policy (first_time_contributors_new_to_github,
  first_time_contributors, all_external_contributors).
- `wf_actions_fork_workflows_get` / `wf_actions_fork_workflows_set`:
  private-fork PR workflow settings (run workflows, send write tokens,
  send secrets and variables, require approval).

Partial-write preservation.  Where the upstream PUT accepts optional
fields (permissions and fork workflow settings), the verb pre-reads the
current object, sends the caller's fields, then post-reads:  sent fields
must equal the request AND every omitted field must be unchanged, so a
silent upstream reset is UNKNOWN, never missed.  Fields the upstream PUT
documents as required (enabled for permissions,
run_workflows_from_fork_pull_requests for fork workflow settings) are
always sent:  the caller's value when given, the pre-read current value
when omitted, with the post-write check proving preservation.
Single-value settings (access level, approval policy) and retention days
write the full documented body and verify the fresh read, with retention
also proving the allowed maximum did not move.

Selected actions and default workflow permissions.  Both repository
routes exist upstream (GET/PUT .../permissions/selected-actions and
.../permissions/workflow) and are covered by
wf_actions_selected_actions_get/_set and
wf_actions_workflow_permissions_get/_set.  The selected-actions routes
are usable only while the repository allowed_actions policy is
"selected":  the set verb reads that policy first and refuses with a
409 and no mutation when it is anything else - it never flips the
policy implicitly.  A workflow-permissions PUT can be rejected by
organization policy (409);  the verb surfaces that status as-is and
never retries or works around it.  Both set verbs fill omitted optional
fields from the pre-read current value and prove preservation on the
post-read.

Dispatches.  wf_workflow_dispatch (POST .../actions/workflows/{id}/dispatches,
Actions write) requires the documented 200 carrying workflow_run_id and the
run URLs;  wf_repository_dispatch (POST .../dispatches, Contents write)
requires the documented 204.  Both refuse invalid input before any upstream
call:  workflow file names are a closed allowlist (no path segments), the
ref is the relay's git-ref validator, workflow inputs cap at the documented
25 properties with scalar values only, and repository-dispatch client_payload
caps at the documented 10 top-level properties and 64KB, with credential-shaped
strings refused in either payload.  The workflow-dispatch receipt links are
verified as this repository's actual run:  exact https hosts and paths bound
to the returned run id, with userinfo, query, fragment and non-default ports
rejected;  any parse fault or mismatch is UNKNOWN 502.  Both are triggers -
the run existing is the proof for workflow dispatch, while a 204 on
repository dispatch confirms acceptance of the event only, never a
downstream effect.  Triggering a workflow can deploy, spend or destroy:
schema validation proves shape only, never that a payload is harmless or
addressed to the right audience;  invocation authority must cover the
workflow, ref, event type and payload content, and these verbs create no
such authority.

Repository metadata.  wf_repo_hash_algorithm reads the documented
hash_algorithm enum (sha1 or sha256).

Deliberately not covered here.  `wf_repo_teams` returns one bounded
page with a complete flag, not a full inventory.  Retention writes
verify against a fresh read but are not a compare-and-swap:  a
concurrent policy change between pre-read and write is detected only
when it alters the compared fields.

Custody.  Every write is verified by a fresh read;  any post-write
projection or comparison fault is UNKNOWN (502), holding the outcome key
against repeated mutation.  Reads are anchored:  unexpected types,
enums or shapes fail closed.  Declaring capability grants no invocation
authority;  these settings change CI behavior and security posture
(notably send_secrets_and_variables), so each write needs its own
seat-level authority before use.
