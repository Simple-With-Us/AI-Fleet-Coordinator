# Exact Public Package Integrity Reviews

The owner explicitly authorized a reviewed integrity exception on November 13, 2026: “Allow a reviewed integrity exception.”  This record covers the two complete CT lockfiles below, within AFC-786;  the dated section at the end records the app/deno.lock admission of November 19, 2026.  It grants no publication, credential, destination or deployment authority by itself; the source change must pass normal review, checks and merge before installation.

## Immutable Files

Repository: `Simple-With-Us/Congress.Trade`.

Path: `app/package-lock.json`.

| Version | Provenance | Git Blob | Whole-File SHA-256 |
| --- | --- | --- | --- |
| Before | Relay-read PR 3173 head `b2abe95f8109193a4a72c85b93ada6ef4f32362a` | `4e1c5c6d7c11a3913b88206ca5e72316d18c5675` | `72462e4c39a4a9a2ba6927214072e0f607565db11c3aa1be034e31eb197af7b9` |
| Final | Local immutable candidate `a0cb1fe7faaae284bb6ccb39fb81d5b7c30faab5`, pending publication | `a3e11c95ceebba9e8d9371ca81e19d7eafcaf4f6` | `eb44b787f36b8149d5173d84e39d23d5c2b235721cc8c6eab9a6970d5ade5bd1` |

Both files contain 157,881 UTF-8 bytes and 318 package entries, with lockfile version 3.  The before file was fetched through the seat's own relay at the exact current PR head; its Git blob was independently recomputed.  A local tracking ref was not treated as proof of remote content.

The complete structural difference is three string fields in `packages/node_modules/hono`: `version`, `resolved` and `integrity`.  Hono changes from 4.13.14 to 4.13.16.  The root manifest constraint remains `^4.13.13`; the manifest and Deno files match their remote blobs exactly:

| Unchanged File | Git Blob |
| --- | --- |
| `app/package.json` | `96921a437d3b5e4f85faabe28caa44e42c609820` |
| `app/deno.json` | `8597b4d3dc33a2555e14136f87b0b9d76e480c88` |
| `app/deno.lock` | `008f64360b60a1300a45960e01b21d4143b4a4d2` |

## Complete Content And Public Artifact Verification

Both complete files passed strict UTF-8 and JSON decoding, duplicate-key and nonfinite-number rejection, exact root-key/type checks, package-path and known npm field-type checks, and root dependency agreement with the unchanged manifest.  All 317 resolved URLs use HTTPS on `registry.npmjs.org`, without user information, query or fragment.  Every integrity value is canonical base64 for a recognized SHA-512 or SHA-1 digest of the correct length.

All decoded string keys and values were scanned, as well as the complete raw text.  Each file has exactly 13 Zulip-shaped matches; lexical JSON string intervals prove every match falls inside an unescaped package integrity value.  No other shared-scanner or relay-provider pattern matches were found.  These 13 package versions are unchanged between the files:

| Package | Version |
| --- | --- |
| `@jridgewell/resolve-uri` | 3.1.2 |
| `@protobufjs/codegen` | 2.0.5 |
| `@sentry/core` | 11.10.0 |
| `@typescript-eslint/scope-manager` | 8.72.1 |
| `@vitest/istanbul-lib-report` | 1.0.2 |
| `@vitest/spy` | 5.0.5 |
| `data-uri-to-buffer` | 4.0.1 |
| `data-urls` | 8.0.0 |
| `fast-deep-equal` | 3.1.3 |
| `lightningcss-win32-x64-msvc` | 1.33.0 |
| `minimatch` | 11.0.3 |
| `path-exists` | 4.0.0 |
| `shebang-command` | 2.0.0 |

Each of those 13 artifacts and both Hono versions were independently checked against npm version metadata and the actual tarball bytes: 15 verified artifacts and 5,398,232 downloaded bytes total.  Metadata name/version, integrity and tarball URL matched the lock; SHA-512 of each streamed tarball matched its integrity value.  Downloads were bounded to 2 MiB per metadata response and 32 MiB per tarball, with HTTPS redirects restricted to `registry.npmjs.org`.  Tarballs were hashed in memory, never saved, extracted or executed; integrity strings are deliberately not reproduced here.

## Admission Boundary

`scripts/simple_tools/secretscan.py::scan_file` and its byte-identical relay vendor contain a separate immutable public-integrity registry of these `(repository, path, whole-file SHA-256)` triples:  the two above and, after the dated review below, the app/deno.lock triple.  For an exact match, every nonempty loaded credential is refused first, including a short value and a value supplied through an iterator.  The unchanged strict scanner then runs; only its `a Zulip-shaped API key` result is admitted for these exact reviewed bytes.  Every other provider, dedicated or generic detector remains active.

The relay's `unreviewed_file_secret` still checks loaded values and its own provider patterns before reaching this subtype.  These files are not added to the legacy synthetic-fixture early-return registry.  General Python `scan()`, JavaScript `scanSecret()`, message scans, provider patterns and gitleaks settings are unchanged.  The existing generic-assignment fixture registry keeps its separate boundary.

There is no caller approval flag, configurable registry, path-prefix exemption, SRI syntax exemption, diff-only scan or runtime registry lookup.  A changed byte, different repository or different path loses admission and takes the normal strict scan.  A new lockfile requires a new complete content review; existing Git history alone grants nothing.

Normal full-file publication uses these gates already: the managed `simple-git` CLI decodes each file and calls `scan_file`; the hosted workflow front validates and forwards the request; the relay scans every complete decoded file before any provider write.  CAS patch publication is not needed for this repair and its provenance scanner is unchanged.  Normal identity, authorization, schema, size, path, default-branch, current-head and message checks still apply.

## Validation And Release

Synthetic tests cover exact admission, strict general scanning and redaction, destination/byte drift, existing fleet-token samples and dedicated/generic detectors, short and iterator-loaded credentials, CLI decoded-file scoping, relay-specific provider shapes, both file-action preflights and real `wf_files_put` dispatch with a fake provider.  Refused requests make no provider calls; an admitted fake-provider write carries the complete unchanged reviewed payload.  A vendor equality assertion prevents client/relay scanner drift, and existing Python/JavaScript parity tests remain required.

After normal source review and merge, use the existing fleet CLI release/update mechanism and the approved pinned relay deployment transaction with its backup, smoke checks and rollback.  Verify the actual qualified deployment entrypoint before installation; a deployment document is not proof of a live qualified runner.  Do not patch a live scanner or registry to unblock publication, alter credentials or service settings, or retry the previously refused CT payload before the reviewed release is installed and verified.

Kodus-Config's `AGENTS.md` (blob `a48ea625234d8f7c48910e04f3300b72219c4c44`) forbids an allowlist in its `.gitleaks.toml`; its README (blob `1fd3cd08e319d0ef93f1e31b053c6c2227062470`) explains why whole-folder/provider suppression was removed.  This change alters neither repository's gitleaks configuration nor any provider detector.  The owner's explicit exception is confined to the reviewed release-code boundary described above.

## Congress.Trade app/deno.lock, reviewed November 19, 2026

- Repository: `Simple-With-Us/Congress.Trade`
- Path: `app/deno.lock`
- SHA-256: `faf4d3d6281fb4c94f0933043612483fd2f5463c469d0b5bb413365d8530a084`
- Size: 97,957 UTF-8 bytes.
- Provenance: renovate dependency-update candidate bytes held locally by the
  publishing worker on the Mac;  these are the exact bytes the publication
  route carries.  Baseline:  main `app/deno.lock`, git blob
  `1eac82267150a7d3c1a35c7032dfbeca20db7065`, SHA-256
  `b5198e827aebc3e7cf9e480d1f0aeea8e42b4c47de5a06695cae7a380ba6f3b2`, fetched
  through the seat's own relay.

The complete file was parsed strictly (UTF-8, duplicate-key and
nonfinite-number rejection) and reviewed in full, not only the changed lines.
Root keys are exactly version, specifiers, npm, remote, redirects and
workspace:  29 specifiers, 392 npm entries, 35 remote entries, 3 redirects.
Every npm integrity value is canonical base64 for a recognized SHA-256,
SHA-384 or SHA-512 digest of the correct length;  every remote value is a
lowercase 64-hex digest;  all 41 URLs use HTTPS on deno.land without user
information.  The complete dependency delta against main is one package:
@aws-sdk/client-s3 3.1167.0 to 3.1168.0;  no remote or redirect change.

The strict scanner's only result on the complete bytes is a Zulip-shaped API
key:  exactly 16 matches, and lexical JSON string intervals prove every match
falls inside an npm integrity value.  No assigned-secret, dedicated-provider
or bearer-header pattern matched.  The 16 implicated npm artifacts plus the
changed @aws-sdk/client-s3@3.1168.0 were independently checked against npm
version metadata and actual tarball bytes:  metadata name, version, integrity
and tarball URL matched the lock, and SHA-512 of each streamed tarball matched
its integrity value:  17 verified artifacts and 24,302,925 downloaded bytes
total, bounded as before, hashed in memory, never saved, extracted or
executed;  integrity strings are deliberately not reproduced here.

The admission boundary is unchanged:  the registry gains exactly one more
(repository, path, whole-file SHA-256) triple, for these reviewed bytes only.
Nonempty loaded credentials refuse first, the unchanged strict scanner runs,
and only its a Zulip-shaped API key result is admitted for these exact bytes;
every other detector stays active.  A changed byte, a different repository or
a different path loses admission;  a regenerated lockfile requires a new
complete content review.  The registry length pins in the git-relay and
scripts test suites now expect three admissions.  This record is an
independent source review;  it is not an owner approval.  Normal source
review, checks and merge must finish before the scanner is installed;  do not
patch a live registry to unblock publication.

