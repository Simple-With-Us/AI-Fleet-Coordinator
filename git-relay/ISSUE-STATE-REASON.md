# Explicit issue state and reason

Default-off own-App issues:write `wf_issue_state_set` changes issue state with an
explicit reason. Closed allows completed/not_planned/duplicate; open requires
reopened. Exact issue ID/number pre-read refuses pull requests. No title/body,
labels, transfer, comments or arbitrary fields. Only numeric issue/state/reason
returns, not untrusted title/body text.

GitHub ignores state_reason unless state changes. A same-state different-reason
request is refused without a write, not hidden reopen/reclose. A same-state exact
reason is verified as already satisfied. For actual state changes the PATCH
receipt must match exact issue ID/number, state and reason. A silently ignored
reason is an unverifiable write, not success. Pre-read is not atomic CAS.

Existing durable UNKNOWN/no-replay holds survive a restart. Only verified
successes may reapply using the existing state-setter rules. Offline real-relay
close/reopen/close and lost-receipt/restart tests cover these paths. No live issue
closed, reopened or changed. No deployment, permission widening or review trigger.

Official state_reason enum and ignored-unless-state-changed behavior verified:
https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.json
