# Review lifecycle GitHub commands

Four default-off own-App commands: wf_review_update (body only), wf_review_dismiss,
wf_review_pending_delete and wf_review_comment_reply.  All require pull_requests:write,
existing per-seat repo/action allowlists and ceiling.  No policy or credential changes.

Dismissal is a capability, never permission to dismiss findings or bypass review gates.  Pending-review deletion pre-reads PENDING, which is not an atomic fence.  GitHub remains
responsible for author/permission and pending-state checks.  Mutations return projected,
validated IDs and state; malformed or uncertain receipts stay UNKNOWN, without replay.  Bodies/messages are nonblank, secret-scanned and bounded at 32 KiB UTF-8.  No live review was edited, dismissed or deleted to test this source.
