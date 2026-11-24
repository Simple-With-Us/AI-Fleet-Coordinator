# Reviewed Fixture Pairs Preservation

## Owner Approval And Scope

On November 21, 2026, through the Codex fast-forward handoff for AFC-793, Jay
approved adding only the two exact AFC-793 fixture preservation records. 
This authorizes exactly two release-code tuples in
`git-relay/git_relay/reviewed_preservation.py`, for the repository,
destinations and source/result digests below.  It does not authorize any
broader registry entry, a general scanner exception, raw-upload admission,
live patch, credential change, or waiver of review, merge and deployment
checks.

Repository: `Simple-With-Us/AI-Fleet-Coordinator`.

| Destination | Complete Source SHA-256 | Complete Result SHA-256 |
| --- | --- | --- |
| `scripts/fleet_rag/tests/test_recall.py` (renewed Nov 23, 2026; the Nov 21 base drifted when OPS-1102 #2473 touched the file — the prior tuple is retired, not extended) | `8a6911c37d2f27d75be7193552b1b9402c4f4f5f3777c2ff350aded37ab6b32e` | `de6b309011f0d0f043dfb209584d71989f6a599128d7eaa3ae5703d54dc5aa31` |
| `scripts/fleet_rag/tests/test_ingest.py` | `e970e451e414d3efd963abed5d544f0713fe2b2934901c032fe8b01898085d2e` | `1eaa804f7514122721888bd9f49c41a4088d827a28855391739d97c334008634` |

Each source digest was verified against the exact `origin/main` Git blob at
commit `284db6972df4a51722045a385a8d9306de0d4dc2`
(`git cat-file blob origin/main:<path> | sha256sum`) before the tuples were
recorded.  Each result digest is the separately reviewed AFC-793 branch image
of the same file; the result bytes are published through the relay, not added
here.

## Fixture Review

Both test files embed synthetic credential-shaped fixtures (`ghp_…`,
`xoxb-…` and similar families) that the fleet-recall tests feed through
deterministic generators; the literals are synthetic test material, not live
credentials, and the relay's own scanner must keep refusing them everywhere
else.  The two tuples admit exactly the pinned source→result transitions
produced by the reviewed AFC-793 change, nothing broader: the server still
fetches the pinned same-path base itself, reconstructs byte origins from
bounded edits, and refuses new, copied, moved or replaced matching lines.

## Admission Boundary

The release records are exactly `(repository, path, source SHA-256, result
SHA-256)` tuples.  They add no entry to the raw-file, public-integrity or
same-path object-reuse registries.  General scanning and complete raw-file
publication still refuse both reviewed files.  The existing `wf_file_patch`
route, provider and bearer guards, and loaded-value refusals remain fully
active; destination, source or result drift falls back to refusal. 
Installation follows normal source review, required checks, merge and the
approved release process; this record is not evidence of any completed
installation or runtime acceptance.  See also
[Reviewed Muster Vector Preservation](reviewed-muster-vector-preservation.md)
for the separate AFC-797 record, and
`git-relay/deploy/preservation-pairs-2426/` for the reviewed host-transaction
preparation that installs the resulting module.
