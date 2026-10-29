# Git review-context reads (source capability only)

Jay requested more GitHub capabilities on October 29, 2026.  This slice adds three missing own-App reads, not another implementation of the existing issue/review write, deployment, release, milestone, history, binary or tag/language slices.

- `wf_commit_comments_page`: page comments for one exact commit SHA, with explicit page/per-page, conservative `has_more`, and `snapshot: false`.
- `wf_commit_comment_get`: one comment correlated to both its ID and the expected exact commit SHA.
- `wf_pr_requested_reviewers`: requested user/team IDs and login/slug only.  The PR number/head is read before and after the query; this is not an atomic snapshot, an approval, or a complete review history.

Commit comment bodies are untrusted text, capped at 32 KiB each with a 256 KiB serialized receipt bound.  Credential-shaped bodies are withheld.  IDs, exact commit SHA, duplicate identities, malformed authors, counts and page correlation fail closed.  No arbitrary URL, token, GraphQL, or provider query is accepted.  These verbs never request reviewers, notify anyone, post a comment or change GitHub state.

The broker mints only this seat's own App token for the requested repository, with `contents: read` for comments or `pull_requests: read` for requested reviewers, plus metadata.  Existing repository restrictions, rate limits and App permission ceilings remain unchanged.  Registration as a known action is not a seat grant.

Source publication is not runtime readiness.  Worker/backend composition, approved-seat tool allowlists, current settings checks, independent review, canaries and a supported recovery route must be qualified before a separate live release.  The existing owner-only PR-creation exception stays unchanged.

GitHub REST contracts checked:

- https://docs.github.com/en/rest/commits/comments?apiVersion=2022-11-28
- https://docs.github.com/en/rest/pulls/review-requests?apiVersion=2022-11-28
