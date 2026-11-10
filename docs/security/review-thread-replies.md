# Bounded review-thread reply reads

The existing thread inventory returns only the first two comments per thread.
Its thread cursor cannot page those replies. Empty COMMENTED review containers
and resolved thread flags do not establish what later inline replies say.

`wf_review_thread_comments` is a read-only own-App capability. It takes a repo,
PR number, exact expected head, thread node ID and optional comment cursor. A
fixed GraphQL query returns at most five comments. The returned thread must bind
to that exact repository and PR. The PR head is read before and after, but these
observations are not an atomic snapshot. Changed heads fail closed.

Each comment carries exact numeric comment/review/reply IDs, commit ID, observed
permalink, optional author login and untrusted text. These metadata bind records,
not user authority. No comment, author name, review state or thread resolution
supplies owner approval. The caller must read subsequent pages while has_more is
true; a missing/nonadvancing cursor, malformed identity or oversized page fails
closed. The body limit is64KiB each and128KiB per page. Deleted/missing required
review provenance is unverifiable, not silently complete.

The query uses fullDatabaseId with decimal-string/integer validation so current
64-bit provider IDs are not narrowed to GraphQL Int. No caller-supplied query,
mutation, personal token, new grant or review trigger. Source publication and
review are not installation or runtime-use clearance. This new capability is
not included in the1511 two-image matcher transaction.

## Separate grant and installation boundary

Production fleet and pinned seat profiles intentionally do not grant this new
action. Registration in policy.py is a recognized capability name, not a seat
grant. Actual use requires a separately reviewed, exact own-seat grant and
installation transaction. No profile or live policy is widened by this PR.

Bodies are filtered with the sibling secret/provider guards, including the loaded
bearer, and withheld bodies carry body_withheld plus the fixed redacted marker.
Malformed NUL/surrogate text fails closed. The final serialized receipt is capped
at256KiB after JSON escaping, independently of the raw body/page limits.
