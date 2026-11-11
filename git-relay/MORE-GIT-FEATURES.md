# Additional bounded Git capabilities

Source baseline38d71f32 already has123workflow actions, including the six shipped
reads.  This slice adds six distinct closed verbs, not a generic GitHub API proxy:

- wf_pr_list_page: created-ascending mutable PR metadata pages, no title/body.
- wf_compare_commits_page: exact base/head SHA graph metadata pages, no file diff.
- wf_pr_reviews_page: observed-head review pages with author and review commit.
- wf_pr_comments_page: observed-head PR issue-conversation pages, not inline comments.
- wf_labels_add: additive labels without replacing unrelated labels.
- wf_label_update: exact pre-read label ID, bounded name/color/description update.

Four reads require pull_requests:read or contents:read.  Two label writes require
issues:write.  All also require metadata:read and the caller's own App ceiling and
seat/repository allowlist.  No App grant is widened.  PR creation retains the
existing owner-only gr_open_pr route.  Capability is not approval to operate it.

State writes retain durable UNKNOWN rows after uncertain outcomes.  A verified
success can reapply desired state, but UNKNOWN cannot replay.  Label identity
pre-reads are not atomic concurrency locks.  Reads are bounded observations, not
complete history, authorization or review approval.  Provider text is untrusted;
credential-shaped discussion bodies are withheld without dropping metadata.

Current official REST schema rechecked2026-10-29:
https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.json

Implementation tests are synthetic.  Runtime publication requires separate
current-artifact review, source review/checks and the runtime owner's acceptance.
Never replace the complete live bundle with this repository build.
