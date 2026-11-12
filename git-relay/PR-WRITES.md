# Pull request writes

Three default-off own-App pull_requests:write commands: wf_pr_create, wf_pr_edit and
wf_pr_close (close only; branch deletion is a separately authorized follow-up, never
implicit). Each pins expected_head_sha - edit and close also expected_base_ref - as
a documented non-atomic pre-read: a foreign move between the pre-read and the write
is caught by the receipt comparison, which fails 502 and leaves the outcome UNKNOWN
and held. A held retry returns 409 with zero upstream calls; pending/UNKNOWN rows
never re-arm.

wf_pr_create is append-only: an identical resubmission returns the cached created
object and never mints a second PR. wf_pr_edit and wf_pr_close are desired-state
setters (server.py _REAPPLY_STATE_ACTIONS): an identical resubmission of a
known-success edit or close RE-DISPATCHES against the current upstream state. An
external rename is overwritten by the re-asserted title/body, and an external
reopen at the same pinned head/base is closed again. This is deliberate setter
behavior, not a read-only replay.

Authority contract for every caller: a prior one-shot success or a generic network
retry is NOT standing authority to re-assert that state later. Each re-dispatch
must carry the caller's CURRENT approved workflow intent. The pins detect foreign
moves; they do not detect external intent and supply no authority by themselves.
Monitoring and reconciliation must not blindly retry a known-ok setter after a
foreign change; re-assert only on a current intent decision.

Receipts are projected and validated (number/state/head/base/title/body); an
unverifiable or changed receipt is 502 with the outcome UNKNOWN. Seats, repo and
action allowlists and the permission ceiling apply unchanged. No live PR was
edited or closed to test this source.
