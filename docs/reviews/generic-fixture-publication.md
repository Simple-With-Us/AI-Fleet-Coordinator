# Exact generic-fixture publication

This change prepares an empty review registry, not a file publication approval.
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

The registry is empty in this change. An entry requires a separate normal code
review that can read the complete immutable proposed file, independently
recompute its Git blob and SHA256, inspect every flagged match with surrounding
context and compare the source provenance and intended destination. The review
must record the exact repository, path, bytes, hash and findings, and fix all real
findings before the entry can land. Review of this empty mechanism is not review
of any future entry. A caller cannot submit approval, a review ID or a hash to
change the registry at run time. Editing the registry itself remains an ordinary
source change with normal review and deployment gates.

The pending BotFleet PR1551 config test file is not approved by this change. It
is a local conflict-resolution candidate and is not an existing remote blob.
An independent full-content inspection and digest comparison are available,
but an auditable full-payload source accessible to the normal entry reviewer
must be established before adding it. No source encoding, fixture obfuscation,
path-prefix exemption or self-attestation is an acceptable publication route.

## Tests

Tests pin an empty registry and mock exact synthetic approvals only inside the
test process. They cover changed bytes, repository/path moves, ordinary scan
strictness, dedicated/provider/loaded refusal, wrong action scope and message
refusal. No real fixture approval is installed by a test.
