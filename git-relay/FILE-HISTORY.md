# Bounded file commit history

Default-off own-App contents:read `wf_file_history` reads exact-path commit and
parent SHA metadata from one repository at an explicit starting commit SHA.
Only that exact path filter is used; renames are not followed. No author, commit
message, raw content, arbitrary query, cross-repo search or history-complete claim.

Pages contain at most50commits, page1-1000, at most16parents per commit. Paths are
bounded to1,024characters/16components and encoded as one query value. Malformed
or oversized upstream pages are refused, never silently skipped or truncated.
`has_more` is conservative on a full page, not a provider completeness guarantee.
An empty page means no matching commits returned for that range, not that the file
never existed. The provider defines commit traversal and visibility.

Strict MCP receipts correlate path/head/page/count to requested arguments. All
calls use the principal's own scoped App and existing policy ceiling. This is a
read with no durable write outcome or replay behavior. Offline synthetic tests;
no history fetched live, configuration changed or deployment performed.

Verified official commit-list schema (sha, path, per_page and page):
https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.json
