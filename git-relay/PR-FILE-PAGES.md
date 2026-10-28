# Bounded PR file metadata pages

Default-off own-App pull_requests:read `wf_pr_files_page` reads one1-50file page,
page1-100, after and before matching the PR number and explicit expected headSHA.
The before/after observations are not an atomic snapshot: head can change away
and back, and base changes are not fenced. No proof of a complete immutable diff.

Only filename, previous filename for renames, blobSHA, change status and numeric
line counts return. No patch, file contents, raw URLs or arbitrary provider text.
Paths bounded1,024characters/4,096UTF-8bytes and credential-scanned; malformed
metadata/pages refused, not skipped or truncated. Full-page has_more is
conservative. history_complete is always false, including on an empty page:
GitHub caps this endpoint at3,000files, and endpoint visibility/limits still apply.

Useful for fleet PR triage and changed-file checks without loading patches.
Existing wf_pr_files stays compatible; this adds explicit paging/head observations,
not full compare pagination or source-content review. No writes/outcome holds,
live PR-file read, new permission grant, deployment or review trigger.

Official PR-file endpoint and3,000file limit verified:
https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.json
