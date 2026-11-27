# Integrated source publication packet 11:13 CST

Supersedes1110 and earlier supplements. Entry points and tests are all in one packet. Default-off candidate. No real native endpoint, ingress-token read, native subscription, model request, send, deployment or activation occurred. No legacy unknown/persisted attempt was replayed. Source work is complete for review, not adopted.

## Final1110 correction

Consumer now verifies current binding again immediately after validating the evidence-file binding/key and before projecting or publishing its report. A regression flips ownership during the evidence read and requires reconciliation=unverified while preserving stored historical proof. observed_at is the current reconciliation observation time, never the original fetch time.

## Changes since accepted1105 source cleanup review

Portable provenance:
- Bundled MIT-licensed Acorn8.15.0 removes the absent absolute parser dependency. Extraction takes SOURCE and OUTPUT_DIRECTORY arguments.
- schema-provenance.json records243 named declarations, exact UTF16 source offsets and source-slice hashes, pinned full source hash and generated-file hash. scope-provenance.json covers the exact installed scope/helpers/socket hook the same way.
- verify_provenance.cjs checks generated hashes, and when given the pinned source checks every declaration slice and byte-identical regeneration in a temporary directory. This passed here using the actual captured release source, not a reconstructed stub. PROVENANCE.md gives portable invocations and discloses weaker hash-only mode.
- The full source bundle is not included because it contains unrelated embedded application prompts/code. Independent regeneration requires the pinned server-core.js available in the qualified review environment. Generated declarations and manifests are included. No claim that a source-hash comment alone proves provenance.

Operational evidence consumer candidate:
- native_consumer.Consumer reads only existing private native-command journals. It validates each full frozen command intent and uses its original binding/key/text. It calls Driver.reconcile_only, never dispatches a new command and never mutates router receipts.
- step is record-budgeted (default32,max128), with an in-memory lexical cursor. run is explicitly invoked, round-bounded (default12) and waits300s between rounds by default. Nothing schedules or runs at import; no production consumer is installed. Later service wiring is an activation change, not silently part of this source task.
- Query plus read-only owning-store evidence is consumed into a compact report: admission, native persistence, turn started/completed, owning alert fetched, handling always unverified. Query outcome remains separate from later store evidence and original unknown router outcome.
- Positive evidence is merged under a private lock and file+directory fsync. Later partial/unverified reads cannot erase admitted/persisted/completed/fetched proof; report sets read_incomplete. Conflicting immutable native message/command/alert/tool-part references are flagged, preserving old references rather than silently replacing them.
- Native persisted user message ID, command ID, alert ID, assistant fetch message ID and actual fetch part ID are retained for audit, with no raw alert content in compact reports. Fetch response remains untrusted data, never authority. Unknown/error/truncated/unfamiliar response shape remains unverified. Retrieval is never semantic handling.
- Evidence-save failure reports reconciliation unverified, not stale proof as a new successful read, and does not reopen dispatch. Current binding is checked before query and after store report access. Changed target binding and corrupt journal fail closed.

## Previously integrated and retained

Exact assembler/helpers plus complete/fragment source wire schemas; full installed snapshot/row/delta/hello/subscribe/ACK/query schema closure, restricted queue-only effect schemas; post-verification readiness attempt and assembly checks; replacement snapshot absence reset/null/malformed/duplicate/regressing refusal; exact target binding and authoritative router freshness; immutable durable intent-before-effect; one dispatch then query only; all candidate returns remain unknown/native-unconfirmed. Mac remains excluded due configured browser ambient executor and no approved omission.

Source protocol1MiB physical,16MiB logical,1024fragments,32MiB staged,30s assembly. Candidate transport64MiB total/2MiB pending and10s readiness remain conservative independent caps. Large/slow cold initialization can consume the single slot without a delivered alert. That uncertainty never permits redrive.

## Final offline rerun

-106 Python unittest executions passed, including inherited repeated scenarios. Do not label105 distinct tests.
-29 authenticated loopback flow scenarios passed. Actual pinned binary framing; production full source snapshot/delta/result schemas. Server-side legacy envelope fixture still has disclosed partial optional schema dependencies, distinct from production route restrictions.
-16 exact assembler scenarios passed.
-11 host-custody fixtures passed.
-10 exact installed scope and2 exact installed socket-close-hook fixtures passed.
-Restricted schema suite, Python compilation and Node syntax checks passed.
-Generated-file hash, every original source declaration slice and portable byte-identical regeneration passed.

Commands: python -m unittest discover -v; node test_flow.cjs; node test_assembler.cjs; node test_host.cjs; node test_scope.cjs; node test_socket_scope.cjs; node test_schemas.cjs; node verify_provenance.cjs SOURCE.

## Exact remaining qualification proposal, requires separate scope

1. Independent source review of this packet, especially provenance/regeneration and consumer monotonic merge/query-only behavior. Source acceptance is not activation.
2. Fresh read-only Linux qualification of exact release source/executable hashes, every runtime/state ancestor custody, current process UID/exact argv/start/generation and source-owned loopback listener inode/UID. Do not read the token or connect native endpoint in this phase without explicit scope. Mac excluded.
3. Explicitly scoped application qualification on an exact existing cold persisted session. Native initialize is normal and may start runtime/use existing policy/provider/workspace; do not invent active-only restriction. Verify realistic snapshot sizes/timing against current caps. Any qualifying model/input commitment needs its actual authority. No diagnostic prompt or real alert is implied by this proposal.
4. On separately authorized new genuine event only, cap1 and postcutover authoritative custody; freeze command/intent before effect; verify idle queue starts normally, busy queue enqueues, held stays held. Never startNow/guide/preempt/cancel. No standalone fallback. Legacy unknown/persisted commands remain query/store-only.
5. Separately authorized disconnect/restart qualification must observe actual native route/resource lifetimes, native input retention and post-restart query results. Candidate child-crash/fsync fixtures and exact installed close callback are not actual OS-crash proof.
6. Keep installed cleanup limitations visible: rejected remote unsubscribe means attempted/local cleanup only, not proven remote removal. A never-settling flow-state promise can stall installed dispose before listener removal; fixtures prove immediate rejection, not a hang. Never-returned subscription ACK fixture eventually rejects the base call and cannot prove cleanup of a remote ID never returned. Need source deadline evidence or bounded resource monitoring during real qualification. Do not patch shared runtime just to claim perfection.
7. Wire the candidate bounded consumer under the production service only after separate configuration/activation approval. Verify later query/store reads and durable compact evidence, including partial reads/conflicts and storage failure. Router unknown stays unknown. Owning fetch requires exact native tool/identity/input/response/ancestry; semantic handled requires separate grounded result, never inference from retrieval.
8. Whole-chain acceptance needs actual new genuine alert, exact owning native input/turn/retrieval and any explicitly required handling outcome. Source merges/offline fixtures/accepted ACK/native persistence alone are not that outcome.

## Monitoring

Existing30min exception-only monitor remains active. Last producer status16:47:28Z healthy (10:52read), ledger202/pendingempty/held21/unknown17 unchanged. Server scan unchanged7206/latest1795514823035, no tracked owning fetch. Mac11:01 read failed before authentication, last successful09:51 scan228/latest1795474504426. Scan gap is not native account-health diagnosis. No new genuine event or end-to-end completion established.
