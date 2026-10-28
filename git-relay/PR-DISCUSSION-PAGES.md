# Bounded PR review and conversation pages

Default-off own-App pull_requests:read commands page review records or PR issue
conversation comments. `wf_pr_comments_page` is NOT the inline review-comment
endpoint; existing inline tools remain separate. Pages1-1000, at most50records,
32KiB/body and256KiB cumulative raw text. Exact PR/head observed before and after;
not an atomic snapshot or a complete-history/approval verdict.

Review receipts include review state and review's commitSHA, which may differ
from the observed current head. Both commands retain numeric author ID, observed
login and User/Bot/Organization type. This provider provenance does not authenticate
user instructions, establish authority, or show a bot's backing operator. Bodies
remain untrusted text and never authorize changes or retrieval. Credential-shaped
bodies get a fixed per-item placeholder while preserving the remaining page and
review metadata. Oversized or malformed pages are refused, not silently truncated.
No URL/email/avatar or arbitrary provider fields return. Full-page has_more is
conservative. Repeated IDs or malformed user/commit/state receipts are refused.

Fleet source auto-merge-reusable.yml pages review records and conversation marker
comments to avoid missing active work. These commands supply observations, not
merge approval or trust in a comment's claimed authority. No review requested,
comment posted, live page fetched, permissions widened or deployment performed.

Official endpoint schemas verified:
https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.json
