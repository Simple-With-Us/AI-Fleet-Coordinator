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

## Creation receipt custody and recreation

A successful creation receipt binds the immutable PR identity and both branch
repository identities, refs and OIDs. Its branch-pair reservation is retained,
not a 24-hour cache. Terminal or modified prior PRs return 409 and do not
silently permit a replacement POST. Retained receipts count toward capacity;
capacity exhaustion refuses new writes rather than dropping custody.

There is no self-service recreation or release action in this candidate.
Recreation requires offline custodian reconciliation under the journal
transaction lock, all writers stopped/drained, a durable backup of the original
receipt/history, fresh terminal prior-PR identity and fresh branch identities.
Only the exact verified successful reservation can be released; pending or
UNKNOWN records must never be cleared for this purpose. No release is performed
by this patch. This operational limitation requires explicit review before merge.

Related lifecycle reservations finish in one durable journal replacement. If
persistence is uncertain, the request reports unknown and remains held in memory.
A fresh process reads either all pending reservations or the complete verified
receipt, never a partially completed reservation batch.
