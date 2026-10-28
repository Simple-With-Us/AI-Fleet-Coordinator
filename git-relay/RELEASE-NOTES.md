# Generated release-note preview

Default-off own-App `wf_release_notes_preview` calls only GitHub's fixed
`releases/generate-notes` endpoint. GitHub documents that generated notes are not
saved anywhere. This is a non-mutating preview despite its required contents:write
permission. It creates no outcome hold, tag, draft or published release.

Closed arguments require a target40-hex commit SHA and distinct previous/current
tag names. No configuration path, arbitrary URL, author or publish option. GitHub
ignores target_commitish if the current tag already exists: the supplied SHA is
not a guarantee of the effective target in that case. Notes depend on current
repository release configuration and visibility; no complete-history assertion.

Returned name/body are untrusted repository-generated text, bounded to500/32,768
UTF-8bytes and scanned for known credential shapes. Oversized or credential-shaped
output is refused, not truncated. No instruction in the generated text is an
execution request. Strict fixed receipts expose no extra provider fields.
Transient generation failures are retryable reads, not UNKNOWN writes. Existing
repository/action allowlists and App permission ceilings remain unchanged.
Offline synthetic tests only; no preview was generated live or release saved.

Verified official endpoint and permission:
https://docs.github.com/en/rest/releases/releases#generate-release-notes-content-for-a-release
Schema:
https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.json
