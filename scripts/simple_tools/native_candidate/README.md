# Default-off Linux native alert candidate

This directory is isolated source. The installed router does not import or enable it. Neither committing it nor preparing its dependencies activates native delivery. The adapter change is a reviewed patch/candidate file, not an edit to active zcode_wake.py.

## Prepare a fresh private directory

Do not prepare in a shared or prepopulated symlinked directory. The source-derived schema and schema-provenance files are intentionally absent from publication after scanner refusal. No copy of them is hidden in the dependency archive.

    python prepare_dependencies.py
    node prepare_candidate.cjs /path/to/authorized/pinned/server-core.js

The source hash must match the pinned installed release. Missing/mismatched local generated artifacts fail closed before transport. PROVENANCE.md explains exact generation and verification; source review artifacts are not publication proof.

## Offline tests

    python -m unittest discover -v
    node test_flow.cjs
    node test_assembler.cjs
    node test_host.cjs
    node test_scope.cjs
    node test_socket_scope.cjs
    node test_schemas.cjs
    node test_packaging.cjs
    node verify_provenance.cjs /path/to/authorized/pinned/server-core.js

## Boundaries

One immutable durable intent, one dispatch then query/store-only uncertainty. Queue-only, no preempt/guide/cancel/config/model changes, no standalone fallback, no legacy replay, no Mac route. ACK/admission, native persistence, turn execution, owning-alert retrieval and semantic handling remain separate. Consumer reports handling unverified. Cleanup rejection/hang/never-returned-ACK limitations remain.

Current qualification proposal: QUALIFICATION-PLAN.md. Detailed prior source design/tests: REVISION1113.md. Packaging and private-group policy: REVISION1122.md, supplemented by REVISION1126.md. Those reports are evidence and limits, never live authority. Publication/default-off merge is not whole-chain repair or production adoption.


## Event-driven exact cap1 continuation

An explicit private spec may set `expires` to JSON null to await the first eligible
new genuine event without clock-based rearming or service churn. The numeric
window mode still enforces a maximum 3600-second interval. Null does not broaden
the target, refresh the activation epoch, change accounts or extend an individual
native delivery timeout. Every admission still requires fresh pending custody,
exact current registry generation, workspace and non-subagent checks. One durable
exclusive claim is consumed on reservation, including uncertainty. Restart never
redrives the reserved input; a changed spec cannot reset or adopt a consumed
claim. Distinct later events retain the existing route. An expired unused spec is
preserved as evidence when a separate continuation spec is selected.
