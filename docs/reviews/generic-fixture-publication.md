# Exact generic-fixture publication

The mechanism was introduced with an empty registry in PR1430. Each later entry
is a separate exact-file review, not a general file publication approval.
Ordinary writes keep their existing route and checks. Only `content_b64` in
`gr_wf_file_put` and `gr_wf_files_put` can use a registry entry, matched on exact
repository, destination path and SHA256 of the entire decoded UTF-8 file.
Messages, titles, other actions and all other fields always use the normal scan.
Missing support falls back to the normal scanner, never an unscanned route.

Every dedicated provider pattern, mixed-class Zulip-shaped key and loaded value
is refused even for an exact approved file. Only the two generic assignment
heuristics can be overridden by full-content review. This does not change the
server's dedicated scanner or its existing security-fixture attestations.

## Registry review and update boundary

A registry entry requires a separate normal code
review of an auditable full-content inspection record. A named inspector must
read the complete immutable proposed file, recompute its Git blob and SHA256,
inspect every flagged match with surrounding context and compare source
provenance and intended destination. The record names the inspector, date, exact
repository, path, bytes, hashes, match classifications and findings. Normal entry
review examines that record and enforcement; it must not be described as a
reviewer's full-payload inspection unless that reviewer actually had the bytes.
The exact packet must be retained for authorized inspection and all real
findings must be fixed before the entry can land. Review of this empty mechanism is not review
of any future entry. A caller cannot submit approval, a review ID or a hash to
change the registry at run time. Editing the registry itself remains an ordinary
source change with normal review and deployment gates.

The BotFleet PR1551 config test entry has a named full-content inspection record
in `botfleet-1551-config-fixture.md`. It is a local conflict-resolution candidate,
not an existing remote blob. That entry requires its own normal review and
release; PR1430's mechanism review did not approve it. No source encoding, fixture obfuscation,
path-prefix exemption or self-attestation is an acceptable publication route.

## Tests

Tests pin the release registry and mock additional synthetic approvals only
inside the test process. They cover changed bytes, repository/path moves, ordinary scan
strictness, dedicated/provider/loaded refusal, wrong action scope and message
refusal. No real fixture approval is installed by a test.
