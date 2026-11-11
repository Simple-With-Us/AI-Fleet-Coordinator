# Commit-comment lifecycle and PR/release detail reads

Seven follow-on source capabilities, stacked on the separately reviewed three-read slice. This is not a live release or authority to mutate any particular object.

- Whole-commit comment create, exact ID/SHA update and delete. Update/delete require the SHA256 digest of the expected original UTF-8 body, checked before the write. GitHub does not offer an atomic compare-and-swap here; receipts explicitly say `atomic: false`, and a concurrent edit can race the write. Never use these as review-gate bypasses. Inline/path comments are not supported by the whole-commit create contract.
- PR commit graph pages: SHA/parent identities only, bounded pages, before/after expected-head observations. GitHub caps this endpoint at250 commits; `history_complete: false` remains even at the last page. It is not an atomic snapshot.
- Exact PR review metadata: ID/state/commit only, without body or URLs. A historical APPROVED review can target an older commit: the result is not an approval verdict for the current head.
- Latest release and exact-tag release lookup: identity/tag/draft/prerelease flags only, no release notes or untrusted raw provider receipt. Latest means GitHub's latest published non-prerelease endpoint, not semantic-version sorting.

Bodies persisted to GitHub are nonblank, UTF-8 bounded and credential-scanned. Raw upstream bodies never bypass the existing bounded comment projector. Returned IDs, commit and written body must match. Delete requires empty204. An error after mutation means the write may have happened: reconcile before retry. No real comments were created, edited or deleted as test canaries.

The broker uses only the seat's own App: contents read/write and pull_requests read as needed, within existing ceilings and repository allowlists. Creating a source capability does not widen App permissions, add new audiences or approve real mutations. Source review/CI and runtime publication/recovery remain separate.

Contracts checked:
- https://docs.github.com/en/rest/commits/comments?apiVersion=2022-11-28
- https://docs.github.com/en/rest/pulls/pulls?apiVersion=2022-11-28
- https://docs.github.com/en/rest/pulls/reviews?apiVersion=2022-11-28
- https://docs.github.com/en/rest/releases/releases?apiVersion=2022-11-28
