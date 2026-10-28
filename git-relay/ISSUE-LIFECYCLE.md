# Issue lifecycle source extension

Five issue operations extend the existing default-off workflow surface. They use the caller's own GitHub App, not an owner token or a raw-token fallback.

| Action | Permission | Result |
|---|---|---|
| wf_issue_list | issues:read | Bounded summaries, excluding pull requests; complete=false |
| wf_issue_get | issues:read | One issue summary |
| wf_issue_create | issues:write | Create with title and body |
| wf_issue_update | issues:write | Change title, body or open/closed state |
| wf_issue_comment | issues:write | Add a comment |

Every action also needs metadata:read. A seat's existing allowlist and ceiling must independently cover it. This source change does not change either, turn on GIT_RELAY_ENABLE_WORKFLOW_VERBS or GR_WORKFLOW_TOOLS, or deploy a service. Existing seventeen workflow actions are retained, not duplicated under alternate names. The held job-log work is unchanged.

Inputs are closed-shape, bounded and secret-scanned before policy or a durable outcome reservation. Issues and pull requests share GitHub's issue endpoints, so get/update/comment refuse pull-request targets. Updates do not claim atomic compare-and-swap protection. Labels, assignees, milestones, repository administration and cross-seat credentials are not added.

Writes use the existing outcome store and audit path. An unreadable or malformed success response after a write is unknown, not a definite refusal; an identical write stays held and is never automatically replayed. Successful writes return bounded identifiers, state and links, without persisting body text in audit details. No issue is created or changed by the offline tests.

Qualification and host rollout remain separate. Review, current-head CI, an exact source handoff and the host owner's scoped execution checks are required before claiming these actions work in a deployed context.
