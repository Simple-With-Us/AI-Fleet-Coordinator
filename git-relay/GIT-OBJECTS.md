# Bounded Git object commands

Four default-off own-App commands read a UTF-8 blob, create an unattached UTF-8 blob,
read a Git commit object, and read a nonrecursive tree.  Blob text maximum40KiB;
Git SHA-1 content identity verified on blob read/create.  Decoded text is returned
for normal response secret scanning, never opaque base64.  Commit messages maximum
32KiB and20 parents; trees maximum100 entries, report upstream truncated flag.
Larger or binary blobs and larger tree responses are refused, not silently cropped.

Explicit contents read/write ceilings and repo/action allowlists remain.  Creates
retain strict UNKNOWN/no-replay and idempotency; no configured grants or live writes.
No arbitrary author/signature, URL or query.  Blob creation changes no tree or ref.
Standalone Git tree/commit creation, binary blobs and recursive object pagination
remain unimplemented.  Existing bounded multi-file writes use their own git-data flow.
Schema: https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.json
