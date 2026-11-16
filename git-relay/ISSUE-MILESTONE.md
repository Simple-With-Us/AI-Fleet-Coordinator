# Exact issue milestone assignment

Default-off own-App issues:write `wf_issue_milestone_set` sets one repository
issue's milestone or removes it. Positive milestone_number assigns; explicit
zero removes, translated to GitHub's null. Missing or negative is refused.

Exact issue number/ID and target milestone number/ID pre-reads precede a fixed
issue PATCH. Pull requests are refused. No title/body/assignee change, arbitrary
fields, milestone creation/deletion, transfer or cross-repo lookup. These are
pre-reads, not atomic conditions; concurrent issue/milestone changes may race.

The successful receipt must retain the exact issue ID/number and requested
milestone ID/number, or explicit null for removal. GitHub may silently ignore a
milestone update when the account lacks push access; that is not reported as
success. Only numeric IDs return, no titles/bodies or additional provider text.

Transient pre-read failures are definitely unsent. Uncertain/malformed write
receipts remain UNKNOWN/non-retryable in the existing file-backed outcome store.
Only verified successes may reapply via existing state-setter rules; real relay
set/remove/set and persisted restart tests cover both paths. No live issue changed,
new permissions granted, deployment performed or review invoked for this source.

Official issue-update milestone schema verified:
https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.json
