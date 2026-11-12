# Commit-bound reviews and branch creation

Source candidate only.  Existing workflow gates remain default off: the broker flag, each seat's action/repository allowlist and App permission ceiling, and hosted workflow registration flag all still apply.  No credentials, seats, ceilings or flags are changed by this source slice.

- `wf_review_submit`: closed repo/number/commit_id/event/body arguments, events APPROVE, REQUEST_CHANGES or COMMENT; requires own App pull_requests:write and metadata:read.
- `wf_review_inline`: closed repo/number/commit_id/path/line/side/body arguments; LEFT or RIGHT side, bounded positive line; same own App permissions.
- `wf_branch_create`: closed repo/branch/sha arguments, contents:write and metadata:read; only creates `refs/heads/<branch>`, never overwrites, forces, updates or deletes an existing branch.

Reviews first read the PR head and refuse a mismatched commit.  This is not an atomic head lock: the head may advance after the read, but GitHub binds the review/comment to the explicit commit_id.  Review submission requires a nonempty body, including approval.  Bodies are secret-scanned and bounded at 32 KiB UTF-8 on the broker.  No pending-review creation, review dismissal, arbitrary reply target, multiline range or file-level inline comment is exposed.

Success requires a matching commit receipt with a positive nonboolean integer ID and a submitted review state, or a matching branch reference and object SHA.  Malformed successful write responses and uncertain write transport failures are held UNKNOWN by the existing durable outcome machinery, with replay refused.  A failed pre-read is definitely unsent and releases only this request's reservation.  Existing UNKNOWN records are never evicted or reclassified.  Audit records retain bounded IDs/counts, not review body content or bearer material.

Existing `wf_branches` lists branches and `wf_update_branch` updates a pull request branch with the expected-head fence.  General branch movement and deletion are deliberately not in this slice: GitHub's ordinary ref update/delete API does not offer the same expected-old-SHA atomic fence.  Release asset coverage remains separate.

API sources:
- https://docs.github.com/en/rest/pulls/reviews#create-a-review-for-a-pull-request
- https://docs.github.com/en/rest/pulls/comments#create-a-review-comment-for-a-pull-request
- https://docs.github.com/en/rest/git/refs#create-a-reference

No host runtime or live GitHub write qualification is claimed by offline tests.
