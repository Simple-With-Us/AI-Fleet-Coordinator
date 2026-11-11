# Exact-SHA compare graph pages

Default-off own-App contents:read `wf_compare_commits_page` reads1-50commit graph
records per page1-1000 between explicit base/head40-hexSHAs. It returns merge-base,
provider status/counts and commit/parentSHAs only. Base identity, bounded numeric
counts, unique commits and at most16parents are validated. No ref expressions,
cross-repo syntax, commit messages/authors, signature payloads, patches or files.

This does NOT fix complete changed-file coverage: GitHub returns files only on
the first compare page, capped300files for the entire comparison. Paginating
commits cannot prove a complete file diff. This command intentionally omits files.
Full-page has_more is conservative, not a whole-history claim. Graph status/counts
are provider observations, not merge safety or approval. No signature verification
assertion. Existing response-byte bounds may refuse otherwise valid large pages.

Fleet relevance: iOS publish workflow uses merge-base/compare graph metadata.
Separate PR-file paging is bounded and also has provider limits. No live compare
fetched, new credentials/grants, deployment or review trigger. Source only.
Official compare paging/300file limit verified:
https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.json
