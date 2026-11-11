# Bounded Git object commands

Four default-off own-App commands read a UTF-8 blob, create an unattached UTF-8 blob,
read a Git commit object, and read a nonrecursive tree.  Blob text maximum40KiB;
Git SHA-1 content identity verified on blob read/create.  Decoded text is scanned for
credential-shaped content and refused before return; never opaque base64.  Commit messages maximum
32KiB and20 parents; trees maximum100 entries, report upstream truncated flag.
Larger or binary blobs and larger tree responses are refused, not silently cropped.

Explicit contents read/write ceilings and repo/action allowlists remain.  Creates
retain strict UNKNOWN/no-replay and idempotency; no configured grants or live writes.
No arbitrary author/signature, URL or query.  Blob creation changes no tree or ref.
Standalone Git tree/commit creation, binary blobs and recursive object pagination
remain unimplemented.  Existing bounded multi-file writes use their own git-data flow.
Schema: https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.json

Blob-create requests use the shared durable Relay outcome store before outbound I/O. Lost responses persist UNKNOWN and refuse re-dispatch after restart; verified successes replay their stored receipt. No per-verb second ledger exists. Blob text and returned commit messages are scanned before persistence or return. SHA-1 here is Git content addressing with usedforsecurity=False, not a security primitive. Lone surrogate blob input refuses with fixed text before encoding or writing. Capability is not permission to execute a write.
