# Release asset source slice

Existing default-off workflow gates, own-App binding, per-seat action/repository allowlists and permission ceilings remain unchanged.  No host rollout, credentials, flags, seats or new permission grants are included.

- `wf_release_assets`: one bounded page of asset metadata for a release, contents:read plus metadata:read; complete is false when the page fills, not a promise of complete history.
- `wf_release_asset_get`: metadata by positive asset ID within the fixed repository, same read permissions.
- `wf_release_asset_upload`: create a new asset from at most 32 KiB secret-scanned UTF-8 text encoded as base64, contents:write plus metadata:read.  The name is a bounded simple filename, no paths or arbitrary upload URL.  No overwrite, automatic delete/retry, label patch or release creation.

The upload pre-read verifies the release ID and exact GitHub upload relation.  The client always pins https://uploads.github.com, sends raw bytes with application/octet-stream and the seat's own installation bearer, retains existing timeout/response bounds and refuses redirects.  A duplicate name is an upstream refusal, not a delete-and-retry.  Success requires an uploaded-state matching name and byte count with a positive nonboolean asset ID.  Malformed or uncertain write outcomes stay held UNKNOWN with replay refused.  A transport-failed pre-read is definitely unsent and releases only the current reservation.  No existing UNKNOWN records are cleared or reclassified.

This is deliberately partial asset coverage.  Binary downloads can redirect to signed asset URLs, and opaque binary uploads cannot be secret-scanned as text.  Neither is exposed by this slice.  Large binaries, downloads, delete/rename and replacing assets remain unimplemented, not silently delegated or treated as qualified.  Ordinary branch mutation/deletion is a separate unresolved safe-fence surface too.

API source: https://docs.github.com/en/rest/releases/assets#upload-a-release-asset

All tests are offline and synthetic.  No live GitHub asset was uploaded or deleted.
