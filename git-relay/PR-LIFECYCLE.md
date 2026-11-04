# PR lifecycle candidate

`edit_pr` retains title/body edits and adds strict `state: open|closed` with
`expected_head_sha` and `expected_updated_at`. State cannot be combined with
text edits. State operations only support unmerged PRs with independently
verified same-repository head/base IDs and branch OIDs. Fork PR state changes
are refused, not silently performed through another credential.

`open_pr` accepts optional boolean `draft`, sent atomically on creation when
true. Omitted/false omits the provider field, preserving request behavior.
Creation checks exact requested title/body in initial and fresh receipts.
Success replay requires the full request digest and current open PR receipt.
A different creation request for a recorded branch pair conflicts; it is not
silently answered using an earlier request's result.

## Journal upgrade and activation

This is NOT rolling-upgrade compatible with older journal writers.
The custodian must stop and drain all old relay processes and direct journal
or reconciliation writers; preserve the exact durable journal and backup;
then start only upgraded writers. Reconciliation must run offline or honor
`<journal>.transaction.lock`. Never clear old UNKNOWN rows to enable rollout.

All upgraded requests sharing the journal serialize under the same process
and cross-process transaction locks. Reads are therefore also serialized.
Latency and concurrency under actual host load require custodian qualification.
No production configuration, seat, grant or credential changes are included.

Legacy journal hashes lack resource attribution. Any pending/UNKNOWN row
conservatively fences creation, state changes, text edits, draft and ready
until reconciliation. This broad availability impact is intentional and must
be surfaced before activation. No unrelated UNKNOWN is automatically cleared.
An existing unresolved PR225 draft/ready outcome remains held.

After qualification, publication and deployment through the normal custodian
route are still required. Runtime1291 holds are not cleared by this patch.
Verify actual installed simple-git whoami/list/show with the caller's own
identity. Do not use arbitrary PR toggles as tests; verify an authorized real
operation through fresh reads when an actual work need exists.
