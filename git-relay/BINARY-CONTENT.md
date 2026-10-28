# Binary and content GitHub commands

New default-off MCP tools in the existing workflow gate (`gr_wf_*`). Own-App principal, per-seat action/repo allowlists and permission ceilings are unchanged.  No new credentials, flags, seats or permissions.

| Tool | Permission | What it does |
| --- | --- | --- |
| `wf_job_log` | actions:read | One Actions job log as a UTF-8 text range |
| `wf_run_logs` | actions:read | Run log archive (zip) as a base64 byte range |
| `wf_artifact_download` | actions:read | Artifact zip as a base64 byte range; refuses expired artifacts |
| `wf_release_asset_download` | contents:read | Uploaded asset as a base64 byte range |
| `wf_release_asset_upload_binary` | contents:write | New asset from up to 180 KiB binary (base64) |
| `wf_release_asset_update` | contents:write | Rename/relabel, fenced on `expected_name` |
| `wf_release_asset_delete` | contents:write | Delete by ID, fenced on `expected_name` |

Artifact deletion, workflow enable/disable, run delete and release object list/get/delete belong to other changes.

## Downloads
GitHub answers these with a 3xx to a signed storage URL. `GitHubClient.download_chunk` sends the seat's bearer to the fixed API origin only, never follows a redirect, validates the Location (https, no userinfo, default port, host under `.githubusercontent.com` or `.blob.core.windows.net`), then makes a second request with no Authorization header and a `Range`.  A 206 must match the requested start; a 200 is skipped only up to a 1 MiB offset and is refused beyond that if the server ignores Range.  Each call returns at most 256 KiB (`length`, default 256 KiB). For a job log, a tiny length may return up to three extra bytes to finish its first UTF-8 character and ensure forward progress.  Results carry `offset`, `next_offset`, `total` and `complete`, so the 1 MiB response bound holds and larger files are paged.  Reads hold no outcome row.  Returned text and bytes are untrusted data.

## Uploads
GitHub takes one POST per asset, so there is no chunked upload.  The cap is 180 KiB raw because the base64 body must fit the Worker's 256 KiB MCP body bound; the relay accepts a larger request body (256 KiB) for this one action only.  The decoded bytes are scanned for credential shapes (Latin-1, UTF-8 and UTF-16 views). Zip, gzip, bzip2 and xz are opened with an cumulative 8 MiB / 500-member bound across all archive members and recursion and scanned recursively one level; encrypted zips, nested archives, over-bound expansion, and zstd/7z/rar/compress are refused.  A credential encoded inside an unrecognized binary format is not detected: this is a shape scan, not a guarantee.  Uploads keep the release pre-read, the fixed uploads.github.com origin, no overwrite, and UNKNOWN-held, no-replay write outcomes.

## Delete and update
Both pre-read the asset and refuse unless its current name equals `expected_name`.  A transient pre-read failure is definitely unsent.  Delete succeeds only on an empty 204 response.  Update changes only `name` and `label` (never `state`), validates the name like uploads, secret-scans the label, and verifies the response.  Malformed or uncertain write outcomes stay held UNKNOWN with replay refused by the existing outcome store.

All tests are offline and synthetic.  Nothing was uploaded, downloaded or deleted live.
