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

The owner-token open_pr follows the same rule with a provider read-back. After
the create answers, the relay GETs the pull request by number with the same
owner token and compares number, repository, base and head (the head owner too
for owner:branch and owner:repo:branch), draft when a draft was asked for, and
the page URL, which the create and the read-back must both give as
https://github.com/{repo}/pull/{number}. Only a confirmed pull request is
answered as {number, url, verified: true}. A failed read-back (transport error
or any HTTP status, 404 and 429 included) or a mismatch is 502 with the outcome
UNKNOWN and held, github_code open_pr_readback_failed or
open_pr_readback_mismatch (open_pr_receipt_invalid when the create answer had
no usable number), and no number in the answer. github_status is always 502
here, the relay's classification; the answer and the audit row also carry
readback_status (what the GET answered: the HTTP status, 0 for a transport
failure or unreadable body, 200 for a mismatch) and
readback_retry_after_seconds when GitHub named a wait.

The read-back is bounded so it fits inside the callers' own timeouts: at most
3 GETs, 3 seconds each, 8 seconds in total (github_client.py READBACK_*). A
transport failure, 404, 408, 429, 5xx, or a 403 with Retry-After is retried
after 0.5 then 1 second, or after Retry-After when that still fits the budget;
any other status, a mismatch, or a wait that does not fit ends it at once.

The budget is a real deadline: the body is read one raw receive at a time
(read1) and checked after each, so a read that trickles past it fails the
attempt as a transport failure (readback_status 0). At most one socket
operation (3 seconds) can overshoot; a peer trickling the response headers
before the body is not bounded by this deadline.
It also starts when open_pr starts, not after the create: the budget is the
smaller of 8 seconds and CALLER_BUDGET_SECONDS (30, the MCP wrapper's own wait)
less the time already spent and READBACK_MARGIN_SECONDS (4). When less than
READBACK_MIN_ATTEMPT_SECONDS (1) is left the read-back is skipped and the
answer is the same outcome UNKNOWN, held, with github_code
open_pr_readback_skipped and readback_status 0. A draft create whose own
receipt does not verify (another URL, or draft not true) is github_code
open_pr_receipt_mismatch, with the number in the audit row only as
unconfirmed_number. The read-back GET carries the same credential selection as
the create. The MCP wrapper's outcome_unknown answer carries github_code,
readback_status and readback_retry_after_seconds from this answer, and never a
number or URL.

The store records unknown, not ok: an identical retry returns 409 with zero
upstream calls, so it cannot open a duplicate pull request and cannot replay a
success that was never confirmed. Re-verifying on the retry was rejected: the
outcome store keeps no number for an unknown row, and a second create is the
one thing a held key exists to prevent. The consequence is that a read-back
that never recovers leaves a held row: it is never evicted, it counts toward
the store's capacity, and that exact request stays refused until an operator
reconciles it. For that the audit row (github_unknown, with the code) records
the number the create named as unconfirmed_number - a lead to check in the
repository, not a confirmed pull request - followed by duplicate_refused for
each held retry. The caller checks the repository for the pull request before
changing or repeating the request. No live PR was opened to test this source.
