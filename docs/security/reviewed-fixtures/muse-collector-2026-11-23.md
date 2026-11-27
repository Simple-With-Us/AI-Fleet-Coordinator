# Exact-file review record: Usage-Monitor Muse Code collector generic assignments

This standalone file is the durable complete review record for the registry
admissions in git-relay/git_relay/patch_secretscan.py and its byte-identical copy
scripts/simple_tools/secretscan.py (the match-pinned `_REVIEWED_GENERIC_MATCHES` entries for
two paths in `Simple-With-Us/Usage-Monitor`). It is linked from the admission PR.
Each entry pins the one reviewed generic assignment span (decoded character offsets: label, match start and end, value start and end) next to the file digest, so any additional generic match in these files still fails the scan. File 1 span: assigned secret 8103, 8130, 8111, 8130. File 2 span: assigned secret 8630, 8656, 8640, 8656. Values are not stored.
The entries waive only that pinned generic assignment finding. Dedicated provider shapes,
bearer headers, Zulip-shaped keys and every loaded credential stay absolute, and a
byte change to either file is a new review.

## Muse Code collector reads quota through the Muse CLI, reviewed November 23, 2026

Why the change exists: the collector read its token with the macOS `security`
command from an item the Muse app rewrites on each token refresh. Each rewrite
reset the item's access list, so the keychain prompt returned every 15 minutes
and "Always Allow" never held. The new default runs `muse usage --json`, so the
collector no longer calls `security` for that item. The old keychain read stays
behind `MUSE_CODE_SOURCE=keychain`.

### File 1

- Repository: `Simple-With-Us/Usage-Monitor`
- Path: `scripts/muse-code-usage-collector.mjs`
- SHA-256: `92618a94e0b5efd9dfcc2ed97b9f39797e1d90773cc5816bfcb25843991ce920`
- Size: 22,480 UTF-8 bytes, 564 lines.

### File 2

- Repository: `Simple-With-Us/Usage-Monitor`
- Path: `scripts/__tests__/muse-code-usage-collector.test.mjs`
- SHA-256: `aeb48889d7147e35f0059a7448be7c58916a075f6153b78303b20947ca7a6fe0`
- Size: 19,198 UTF-8 bytes, 528 lines.

Immutable local evidence repository
`~/.local/state/fleet/wise-um-musecli-evidence-20261123` on the Mac, commit
`b2659ee78559e93ac177378f72267a61fef86b83`, blobs
`28be5ad3ba2b1755701c27f600920df56b4d79c8` (File 1) and
`55ade1ae15ea297a9e2183e111d0649437abf260` (File 2). This is a local snapshot of
the bytes the Usage-Monitor change will publish, not a claim that they are
published yet.

Both complete files were read and reviewed, not only the changed lines. Neither
contains a credential literal. The changed lines add a `muse usage --json` call
and a mapping of its percent and reset-time numbers; nothing from the CLI output
other than those numbers is kept or logged.

The scanner reports exactly one generic assignment hit in each file, and the
identical hit exists in the unchanged main version of each file:

- File 1, line 190: the local variable named token receives the property
  `parsed.access_token` of a JSON object read from the keychain in the legacy
  path. It is a 19-character dotted property access in unquoted code, not a
  stored key or a credential value.
- File 2, line 239: a mock HTTP response body in a test returns an object whose
  api key field holds a 16-character placeholder string that begins with the
  letters ak and ends with REDACTED. It is a fixture that proves the collector
  never reads a response body into an error message, not a credential.

Independent pattern enumeration on both files found no dedicated-provider hit, no
bearer-header hit and no Zulip-shaped key. Comments in the files claiming review
or approval were treated as file content, not permission.
