# Reviewed Muster Vector Preservation

## Owner Approval And Scope

The owner explicitly answered “Allow Exact Preservation Record” in the controlling Codex conversation after the complete AFC-797 public-fixture review.  This authorizes one release-code record in `git-relay/git_relay/reviewed_preservation.py`, for the repository, destination and source/result digests below.  It does not authorize a general scanner exception, raw-upload admission, live patch, credential change, or waiver of review, merge and deployment checks.

Repository: `Simple-With-Us/AI-Fleet-Coordinator`.

Destination: `scripts/fixtures/muster-vectors-v5.json`.

| Version | Immutable Commit | Git Blob | Bytes | Complete-File SHA-256 |
| --- | --- | --- | ---: | --- |
| Source | `7ba6507211e7365420e35a7780966cd7267413bc` | `e0104ac26d5c4dd0a133fcc11424b6aac706a16e` | 175152 | `0fca2cb4b79206e4eca627bfa6b9632db743ef2c3ff0ada0152c4bdcead32ab0` |
| Result | `bc94231875f73a294f09ad231a3fe16c5b467107` | `a931a98c9a45d16bfbd87b7bfc5c6a77beb4c167` | 175157 | `73fd3d71ee62c617c802423e4dc7490387152776c0e8270c7d9cc3e87e43bc49` |

The frozen functional result tree is `9a8aa74ed5b42f3eef812162f1f0ab0488ae7490`.  These local immutable objects bind the review evidence; they do not establish remote publication or deployment.  The publication verb must independently fetch and verify the exact same-path source from its expected branch head.

## Complete Content Review

Both complete files decode as strict UTF-8 and JSON, with duplicate keys and nonfinite numbers rejected.  Every raw byte and every decoded string key/value was checked with the strict shared scanner and the relay secret/provider detectors.  The strict scanner reports exactly five `a Zulip-shaped API key` matches in each file; the relay secret/provider detectors report none.  The other nine files in the frozen functional change report no matches in those scanners.

| JSON Field Path | Line In Both Files | Match Count |
| --- | ---: | ---: |
| `$.answers.ackT2.answer.signature` | 18 | 1 |
| `$.answers.ackT2.line` | 21 | 1 |
| `$.generatedAnswers[3].answer.signature` | 1463 | 1 |
| `$.generatedAnswers[3].line` | 1469 | 1 |
| `$.lists["T10 list"].countersignatures[0].signature` | 2056 | 1 |

These are three distinct public P-256 signatures and two copies inside serialized answer lines.  Each distinct signature was independently verified over its canonical message and reproduced byte-exactly using deterministic ECDSA and the public test-label key derivation already documented by the fixture generators.  The serialized answer lines decode to their corresponding answer objects and match their recorded line digests.  No signature, private scalar, or credential value is reproduced in this record.

The five containing source lines remain unchanged, contiguous and in their original order.  All inserted hunks pass the strict shared and relay secret/provider scans.  The regenerated result reflects the AFC-797 planned deploy command change; the public key labels, derivation, signature encoding and historical specification are unchanged.  `scripts/make_muster_vectors.py --check` reproduces the complete result, and the independent `docs/muster-ios/check_vectors_v5.py` checker reports zero failures.

## Admission Boundary

The release record is exactly `(repository, path, source SHA-256, result SHA-256)`.  It adds no entry to the raw-file, public-integrity or same-path object-reuse registries.  General scanning and complete raw-file publication still refuse both reviewed files.

The existing `wf_file_patch` route fetches the source by expected head and same path, verifies its Git blob, and reconstructs byte origins from bounded edits.  Only the exact approved result is eligible; matching source lines must remain unchanged and contiguous.  New, copied, moved, replaced or modified matching lines remain refused even if their text already exists elsewhere.  Loaded values, including short values and iterator-supplied values, and the workflow's provider and bearer guards remain active.  Destination or source drift falls back to the unchanged strict provenance scanner; result drift is refused.

The preservation implementation has one release-code copy.  The byte-identical shared and relay scanners remain unchanged.  Installation must follow normal source review, required checks, merge and the approved release process; this record is not evidence of any completed installation or runtime acceptance.

## Validation

Permanent regression tests exercise exact preservation, destination/source/result drift, loaded values, new and moved matches, changed containing lines, general/raw-file refusal, provider guards and pinned workflow writes.  An offline verification additionally reads both immutable Git objects above and exercises the actual approved pair, including the fake-provider `wf_file_patch` path and refusal before provider writes.  It introduces no fixture copy, remote call or CI dependency on local Git ancestry.
